import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest


_tmp_home = tempfile.TemporaryDirectory()
os.environ["CODEX_PROVIDER_HOME"] = _tmp_home.name
os.environ["CODEX_HOME"] = str(Path(_tmp_home.name) / ".codex")

ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("sub2cli_inject", str(ROOT / "sub2cli-inject"))
spec = importlib.util.spec_from_loader(loader.name, loader)
sub2cli_inject = importlib.util.module_from_spec(spec)
loader.exec_module(sub2cli_inject)


def pool_cfg():
    return {
        "mode": "relay",
        "protocol": "pool",
        "policy": {
            "fail_consecutive": 2,
            "recovery_successes": 2,
            "min_dwell_seconds": 0,
            "cooldown_seconds": [30],
        },
        "routes": [
            {
                "id": "primary",
                "label": "primary",
                "priority": 10,
                "base_url": "https://primary.example",
                "api_key": "sk-primary",
                "protocol": "responses",
                "_order": 1,
            },
            {
                "id": "fallback",
                "label": "fallback",
                "priority": 20,
                "base_url": "https://fallback.example",
                "api_key": "sk-fallback",
                "protocol": "responses",
                "_order": 2,
            },
        ],
    }


def four_route_pool_cfg():
    cfg = pool_cfg()
    cfg["policy"] = {
        **cfg["policy"],
        "probe_interval_seconds": 5,
    }
    cfg["routes"] = [
        {
            "id": f"route-{idx}",
            "label": f"route {idx}",
            "priority": idx * 10,
            "base_url": f"https://route-{idx}.example",
            "api_key": f"sk-route-{idx}",
            "protocol": "responses",
            "_order": idx,
        }
        for idx in range(1, 5)
    ]
    return cfg


class RoutePoolRuntimeTests(unittest.TestCase):
    def with_isolated_codex_home(self):
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name)
        codex_home = home / ".codex"
        app_support = home / "Library" / "Application Support"
        replacements = {
            "HOME": home,
            "CODEX_HOME": codex_home,
            "APP_SUPPORT": app_support,
            "APP_PROFILE": app_support / "Codex",
            "AUTH_JSON": codex_home / "auth.json",
            "CONFIG_TOML": codex_home / "config.toml",
            "STATE_DB": codex_home / "state_5.sqlite",
            "SESSION_DIRS": [codex_home / "sessions", codex_home / "archived_sessions"],
            "SLOTS_FILE": codex_home / "provider-slots.json",
            "BACKUP_ROOT": codex_home / "provider-switch-backups",
            "LOCK_FILE": codex_home / ".sub2cli-inject.lock",
            "PROTOCOL_PROXY_LOG": codex_home / "sub2cli-responses-proxy.log",
            "CODEX_APP_SERVER_CONTROL_SOCKET": codex_home / "app-server-control" / "app-server-control.sock",
        }
        original = {name: getattr(sub2cli_inject, name) for name in replacements}
        for name, value in replacements.items():
            setattr(sub2cli_inject, name, value)
        codex_home.mkdir(parents=True, exist_ok=True)
        app_support.mkdir(parents=True, exist_ok=True)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: [setattr(sub2cli_inject, name, value) for name, value in original.items()])
        return home, codex_home, app_support

    def test_switch_plan_includes_session_normalization_when_provider_tags_drift(self):
        _home, codex_home, app_support = self.with_isolated_codex_home()
        profile = app_support / "Codex.local"
        profile.mkdir()
        sub2cli_inject.atomic_symlink(sub2cli_inject.APP_PROFILE, profile)

        relay_auth = codex_home / "auth.relay.json"
        sub2cli_inject.write_apikey_auth(relay_auth, "sk-relay")
        sub2cli_inject.copy_auth_atomic(relay_auth)
        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
        )

        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            conn.execute("CREATE TABLE threads (model_provider TEXT NOT NULL)")
            conn.execute("INSERT INTO threads (model_provider) VALUES (?)", (sub2cli_inject.OAUTH_PROVIDER,))

        session_path = sub2cli_inject.SESSION_DIRS[0] / "rollout-test.jsonl"
        session_path.parent.mkdir(parents=True)
        session_path.write_text(
            '{"type":"session_meta","payload":{"id":"test","model_provider":"openai"}}\n',
            encoding="utf-8",
        )

        data = {
            "version": 1,
            "current": "relay",
            "app_history_slot": "local",
            "slots": {
                "local": {
                    "display_name": "Codex local",
                    "mode": "oauth",
                    "auth_file": str(codex_home / "auth.local.json"),
                    "app_profile_dir": str(profile),
                },
                "relay": {
                    "display_name": "Codex relay",
                    "mode": "relay",
                    "auth_file": str(relay_auth),
                    "app_profile_dir": str(app_support / "Codex.relay"),
                    "base_url": "https://relay.example/v1",
                    "model": sub2cli_inject.DEFAULT_MODEL,
                    "model_source": "manual",
                    "models": [sub2cli_inject.DEFAULT_MODEL],
                    "protocol": "responses",
                    "api_key": "sk-relay",
                },
            },
        }

        self.assertTrue(sub2cli_inject.sessions_need_normalize(sub2cli_inject.RELAY_PROVIDER))
        self.assertFalse(sub2cli_inject.slot_is_clean(data, "relay"))
        plan = sub2cli_inject.compute_switch_plan(data, "relay")
        norm_actions = [action for action in plan["actions"] if action["type"] == "normalize_sessions"]
        self.assertEqual(1, len(norm_actions))
        self.assertEqual(sub2cli_inject.RELAY_PROVIDER, norm_actions[0]["target_provider"])
        self.assertEqual(1, norm_actions[0]["state_db_changes"])
        self.assertEqual(1, norm_actions[0]["rollout_changes"])

    def test_switch_normalizes_sessions_without_restarting_codex_app(self):
        _home, codex_home, app_support = self.with_isolated_codex_home()
        profile = app_support / "Codex.local"
        profile.mkdir()
        relay_profile = app_support / "Codex.relay"
        relay_profile.mkdir()
        sub2cli_inject.atomic_symlink(sub2cli_inject.APP_PROFILE, profile)

        relay_auth = codex_home / "auth.relay.json"
        sub2cli_inject.write_apikey_auth(relay_auth, "sk-relay")
        sub2cli_inject.copy_auth_atomic(relay_auth)
        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
        )

        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            conn.execute("CREATE TABLE threads (model_provider TEXT NOT NULL)")
            conn.execute("INSERT INTO threads (model_provider) VALUES (?)", (sub2cli_inject.OAUTH_PROVIDER,))

        session_path = sub2cli_inject.SESSION_DIRS[0] / "rollout-test.jsonl"
        session_path.parent.mkdir(parents=True)
        session_path.write_text(
            '{"type":"session_meta","payload":{"id":"test","model_provider":"openai"}}\n',
            encoding="utf-8",
        )

        sub2cli_inject.atomic_write_json(codex_home / "provider-slots.json", {
            "version": 1,
            "current": "relay",
            "app_history_slot": "local",
            "slots": {
                "local": {
                    "display_name": "Codex local",
                    "mode": "oauth",
                    "auth_file": str(codex_home / "auth.local.json"),
                    "app_profile_dir": str(profile),
                },
                "relay": {
                    "display_name": "Codex relay",
                    "mode": "relay",
                    "auth_file": str(relay_auth),
                    "app_profile_dir": str(relay_profile),
                    "base_url": "https://relay.example/v1",
                    "model": sub2cli_inject.DEFAULT_MODEL,
                    "model_source": "manual",
                    "models": [sub2cli_inject.DEFAULT_MODEL],
                    "protocol": "responses",
                    "api_key": "sk-relay",
                },
            },
        })

        rc = sub2cli_inject.cmd_switch("relay", no_restart=True)

        self.assertEqual(0, rc)
        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            providers = [row[0] for row in conn.execute("SELECT model_provider FROM threads")]
        self.assertEqual([sub2cli_inject.RELAY_PROVIDER], providers)
        self.assertIn('"model_provider":"OpenAI"', session_path.read_text(encoding="utf-8"))
        self.assertTrue(any(sub2cli_inject.BACKUP_ROOT.glob("*switch-to-relay/thread-provider-manifest.json")))

    def test_manual_normalize_sessions_updates_root_and_sqlite_state_dbs(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        sqlite_state = codex_home / "sqlite" / "state_5.sqlite"
        sqlite_state.parent.mkdir(parents=True)

        for db_path in (sub2cli_inject.STATE_DB, sqlite_state):
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE threads (model_provider TEXT NOT NULL)")
                conn.execute("INSERT INTO threads (model_provider) VALUES (?)", (sub2cli_inject.OAUTH_PROVIDER,))

        preview = sub2cli_inject.normalize_sessions(sub2cli_inject.RELAY_PROVIDER, dry_run=True)
        self.assertEqual(2, preview["state_db"]["changed"])
        self.assertEqual(2, len(preview["state_db"]["paths"]))

        stats = sub2cli_inject.normalize_sessions(sub2cli_inject.RELAY_PROVIDER, dry_run=False)
        self.assertEqual(2, stats["state_db"]["changed"])
        self.assertEqual(2, len(stats["state_db"]["backups"]))

        for db_path in (sub2cli_inject.STATE_DB, sqlite_state):
            with sqlite3.connect(db_path) as conn:
                providers = [row[0] for row in conn.execute("SELECT model_provider FROM threads")]
            self.assertEqual([sub2cli_inject.RELAY_PROVIDER], providers)

    def test_failover_after_consecutive_failures(self):
        cfg = pool_cfg()
        runtime = sub2cli_inject.RoutePoolRuntime()

        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("primary", route["id"])

        runtime.record_failure(route, cfg, kind="retryable", status=500, detail="HTTP 500")
        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("primary", route["id"])

        runtime.record_failure(route, cfg, kind="retryable", status=500, detail="HTTP 500")
        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("fallback", route["id"])

    def test_recovery_probe_threshold_preempts_lower_priority(self):
        cfg = pool_cfg()
        runtime = sub2cli_inject.RoutePoolRuntime()
        primary = cfg["routes"][0]

        route, _ = runtime.choose(cfg)
        runtime.record_failure(route, cfg, kind="retryable", status=500, detail="HTTP 500")
        runtime.record_failure(route, cfg, kind="retryable", status=500, detail="HTTP 500")

        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("fallback", route["id"])

        runtime.record_probe_success(primary, cfg)
        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("fallback", route["id"])

        runtime.record_probe_success(primary, cfg)
        route, err = runtime.choose(cfg)
        self.assertIsNone(err)
        self.assertEqual("primary", route["id"])

    def test_probe_route_uses_codex_response_request_path(self):
        calls = []
        original = sub2cli_inject.request_route_response

        def fake_request(route, body, *, timeout):
            calls.append((route, body, timeout))
            return b"{}", "application/json"

        sub2cli_inject.request_route_response = fake_request
        try:
            sub2cli_inject.probe_route({"id": "r1", "model": "gpt-test"}, timeout=3)
        finally:
            sub2cli_inject.request_route_response = original

        self.assertEqual(1, len(calls))
        _route, body, timeout = calls[0]
        self.assertEqual("gpt-test", body["model"])
        self.assertEqual("ping", body["input"])
        self.assertEqual(1, body["max_output_tokens"])
        self.assertEqual(3, timeout)

    def test_openai_capacity_error_retries_same_route_before_returning(self):
        route = {
            "id": "primary",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        calls = []
        original = sub2cli_inject.fresh_urlopen
        original_delays = sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS

        class FakeResponse:
            def __init__(self, body):
                self._body = body
                self.headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return self._body

        def fake_fresh(req, *, timeout):
            calls.append(req)
            if len(calls) == 1:
                return FakeResponse(
                    b'{"error":{"message":"Selected model is at capacity. Please try a different model."}}'
                )
            return FakeResponse(b'{"id":"resp_ok","object":"response","status":"completed","output":[]}')

        sub2cli_inject.fresh_urlopen = fake_fresh
        sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS = (0,)
        try:
            body, content_type = sub2cli_inject.request_route_response_with_openai_retries(
                route,
                {"model": "gpt-test", "input": "hi"},
                timeout=3,
            )
        finally:
            sub2cli_inject.fresh_urlopen = original
            sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS = original_delays

        self.assertEqual(2, len(calls))
        self.assertEqual("application/json", content_type)
        self.assertIn(b'"resp_ok"', body)

    def test_relay_502_does_not_retry_same_route_wrapper(self):
        route = {
            "id": "primary",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        calls = []
        original = sub2cli_inject.fresh_urlopen
        original_delays = sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS

        def fake_fresh(req, *, timeout):
            calls.append(req)
            raise sub2cli_inject.urlerror.HTTPError(
                req.full_url,
                502,
                "Bad Gateway",
                {"Content-Type": "text/plain"},
                None,
            )

        sub2cli_inject.fresh_urlopen = fake_fresh
        sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS = (0, 0)
        try:
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_response_with_openai_retries(
                    route,
                    {"model": "gpt-test", "input": "hi"},
                    timeout=3,
                )
        finally:
            sub2cli_inject.fresh_urlopen = original
            sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS = original_delays

        self.assertEqual(1, len(calls))
        self.assertEqual("retryable", raised.exception.kind)
        self.assertEqual(502, raised.exception.status)

    def test_response_failed_sse_capacity_error_is_openai_retryable(self):
        raw = (
            'event: response.failed\n'
            'data: {"response":{"status":"failed","error":{"message":"Selected model is at capacity. Please try a different model."}}}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")
        message = sub2cli_inject.detect_openai_transient_success_error(raw, "text/event-stream")
        self.assertEqual("Selected model is at capacity. Please try a different model.", message)

    def test_upstream_requests_use_fresh_urlopen_instead_of_global_urlopen(self):
        calls = []
        original = sub2cli_inject.fresh_urlopen
        original_urlopen = sub2cli_inject.urlrequest.urlopen

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return b'{"data":[]}'

        def fake_fresh(req, *, timeout):
            calls.append((req, timeout))
            return FakeResponse()

        def stale_global_urlopen(*_args, **_kwargs):
            raise AssertionError("global urlopen should not be used for upstream relay calls")

        sub2cli_inject.fresh_urlopen = fake_fresh
        sub2cli_inject.urlrequest.urlopen = stale_global_urlopen
        try:
            models, error = sub2cli_inject.fetch_relay_models("https://relay.example", "sk-test")
            body, content_type = sub2cli_inject.fetch_route_models({
                "id": "r1",
                "base_url": "https://relay.example",
                "api_key": "sk-test",
            })
        finally:
            sub2cli_inject.fresh_urlopen = original
            sub2cli_inject.urlrequest.urlopen = original_urlopen

        self.assertEqual([], models)
        self.assertIsNone(error)
        self.assertEqual(b'{"data":[]}', body)
        self.assertEqual("application/json", content_type)
        self.assertEqual(2, len(calls))

    def test_pool_policy_accepts_scalar_cooldown(self):
        policy = sub2cli_inject.pool_policy({"policy": {"cooldown_seconds": 60}})
        self.assertEqual([60], policy["cooldown_seconds"])

    def test_proxy_access_log_is_suppressed_but_pool_events_remain(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        log_path = codex_home / "sub2cli-responses-proxy.log"

        sub2cli_inject.ResponsesProxyHandler.log_message(
            object(),
            '"GET /poolz HTTP/1.1" 200 -',
        )
        self.assertFalse(log_path.exists())

        sub2cli_inject.proxy_log("pool route active (none) -> primary (primary)")
        self.assertIn("pool route active", log_path.read_text(encoding="utf-8"))

    def test_monitor_only_probes_routes_ahead_of_current(self):
        cfg = four_route_pool_cfg()
        runtime = sub2cli_inject.RoutePoolRuntime()
        calls = []
        original = sub2cli_inject.probe_route

        def fake_probe(route, *, timeout):
            calls.append(route["id"])

        sub2cli_inject.probe_route = fake_probe
        try:
            runtime._activate(cfg["routes"][2], 0.0)
            runtime.probe_due_routes(cfg)
        finally:
            sub2cli_inject.probe_route = original

        self.assertEqual(["route-1", "route-2"], calls)

    def test_monitor_probes_all_routes_without_current(self):
        cfg = four_route_pool_cfg()
        runtime = sub2cli_inject.RoutePoolRuntime()
        calls = []
        original = sub2cli_inject.probe_route

        def fake_probe(route, *, timeout):
            calls.append(route["id"])

        sub2cli_inject.probe_route = fake_probe
        try:
            runtime.probe_due_routes(cfg)
        finally:
            sub2cli_inject.probe_route = original

        self.assertEqual(["route-1", "route-2", "route-3", "route-4"], calls)

    def test_proxy_stop_expands_pyinstaller_parent_child_process_group(self):
        original_run = sub2cli_inject.subprocess.run

        def fake_run(cmd, **_kwargs):
            if cmd[:3] == ["ps", "-axo", "pid=,pgid=,command="]:
                return SimpleNamespace(stdout="\n".join([
                    "100 100 /path/sub2cli-inject-bundle responses-proxy --port 18765",
                    "101 100 /path/sub2cli-inject-bundle responses-proxy --port 18765",
                    "102 100 /bin/zsh unrelated",
                    "200 200 /path/other responses-proxy --port 9999",
                ]))
            raise AssertionError(f"unexpected command: {cmd}")

        sub2cli_inject.subprocess.run = fake_run
        try:
            self.assertEqual([100, 101], sub2cli_inject.process_group_pids_for([101]))
        finally:
            sub2cli_inject.subprocess.run = original_run

    def test_app_server_pid_scan_is_scoped_to_current_control_socket(self):
        tmp = tempfile.TemporaryDirectory()
        original_socket = sub2cli_inject.CODEX_APP_SERVER_CONTROL_SOCKET
        original_run = sub2cli_inject.subprocess.run
        socket_path = Path(tmp.name) / ".codex" / "app-server-control" / "app-server-control.sock"
        socket_path.parent.mkdir(parents=True)
        socket_path.write_text("")
        other_socket = Path(tmp.name) / "other" / "app-server-control.sock"

        def fake_run(cmd, **_kwargs):
            if cmd[0] == "lsof":
                return SimpleNamespace(stdout="")
            if cmd[0] == "ps":
                return SimpleNamespace(stdout="\n".join([
                    f"111 app-server --listen unix://{other_socket}",
                    f"222 app-server --listen unix://{socket_path}",
                    f"333 app-server-broker --listen unix://{socket_path}",
                ]))
            raise AssertionError(f"unexpected command: {cmd}")

        sub2cli_inject.CODEX_APP_SERVER_CONTROL_SOCKET = socket_path
        sub2cli_inject.subprocess.run = fake_run
        try:
            self.assertEqual([222], sub2cli_inject.codex_cli_app_server_control_pids())
        finally:
            sub2cli_inject.subprocess.run = original_run
            sub2cli_inject.CODEX_APP_SERVER_CONTROL_SOCKET = original_socket
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
