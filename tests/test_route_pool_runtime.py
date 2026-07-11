import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib import error as urlerror
from urllib import request as urlrequest


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
            # Unit tests must never control the user's real Codex/ChatGPT App.
            "quit_codex": lambda: None,
            "launch_codex": lambda: None,
            "stop_codex_cli_app_server": lambda: False,
        }
        original = {name: getattr(sub2cli_inject, name) for name in replacements}
        for name, value in replacements.items():
            setattr(sub2cli_inject, name, value)
        codex_home.mkdir(parents=True, exist_ok=True)
        app_support.mkdir(parents=True, exist_ok=True)
        self.addCleanup(tmp.cleanup)
        self.addCleanup(lambda: [setattr(sub2cli_inject, name, value) for name, value in original.items()])
        return home, codex_home, app_support

    def write_chatgpt_auth(self, path: Path, account_id: str = "acct-local"):
        sub2cli_inject.atomic_write_json(path, {
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "test-access-token",
                "refresh_token": "test-refresh-token",
                "id_token": "test-id-token",
                "account_id": account_id,
            },
        })

    def test_switch_plan_includes_session_normalization_when_provider_tags_drift(self):
        _home, codex_home, app_support = self.with_isolated_codex_home()
        profile = app_support / "Codex.local"
        profile.mkdir()
        sub2cli_inject.atomic_symlink(sub2cli_inject.APP_PROFILE, profile)

        relay_auth = codex_home / "auth.relay.json"
        sub2cli_inject.write_apikey_auth(relay_auth, "sk-relay")
        local_auth = codex_home / "auth.local.json"
        self.write_chatgpt_auth(local_auth)
        sub2cli_inject.copy_auth_atomic(local_auth)
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
            "active_oauth_slot": "local",
            "preferred_official_slot": "local",
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
        local_auth = codex_home / "auth.local.json"
        self.write_chatgpt_auth(local_auth)
        sub2cli_inject.copy_auth_atomic(local_auth)
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
            "active_oauth_slot": "local",
            "preferred_official_slot": "local",
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

        original_refresh = sub2cli_inject.refresh_model_catalog_for_cfg
        original_ensure = sub2cli_inject.ensure_protocol_proxy
        original_healthy = sub2cli_inject.protocol_proxy_healthy
        sub2cli_inject.refresh_model_catalog_for_cfg = lambda _cfg, *, refresh: [sub2cli_inject.DEFAULT_MODEL]
        sub2cli_inject.ensure_protocol_proxy = lambda: False
        sub2cli_inject.protocol_proxy_healthy = lambda: True
        try:
            rc = sub2cli_inject.cmd_switch("relay", no_restart=True)
        finally:
            sub2cli_inject.refresh_model_catalog_for_cfg = original_refresh
            sub2cli_inject.ensure_protocol_proxy = original_ensure
            sub2cli_inject.protocol_proxy_healthy = original_healthy

        self.assertEqual(0, rc)
        self.assertIn(
            "requires_openai_auth = true",
            sub2cli_inject.CONFIG_TOML.read_text(encoding="utf-8"),
        )
        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            providers = [row[0] for row in conn.execute("SELECT model_provider FROM threads")]
        self.assertEqual([sub2cli_inject.RELAY_PROVIDER], providers)
        self.assertIn('"model_provider":"OpenAI"', session_path.read_text(encoding="utf-8"))
        self.assertTrue(any(sub2cli_inject.BACKUP_ROOT.glob("*switch-to-relay/thread-provider-manifest.json")))

    def test_switch_relay_without_oauth_uses_apikey_compatibility(self):
        _home, codex_home, app_support = self.with_isolated_codex_home()
        local_profile = app_support / "Codex.local"
        local_profile.mkdir()
        relay_profile = app_support / "Codex.relay"
        relay_profile.mkdir()
        sub2cli_inject.atomic_symlink(sub2cli_inject.APP_PROFILE, local_profile)
        relay_auth = codex_home / "auth.relay.json"
        sub2cli_inject.atomic_write_json(sub2cli_inject.SLOTS_FILE, {
            "version": 1,
            "current": "local",
            "app_history_slot": "local",
            "slots": {
                "local": {
                    "display_name": "Codex local",
                    "mode": "oauth",
                    "auth_file": str(codex_home / "auth.local.json"),
                    "app_profile_dir": str(local_profile),
                },
                "relay": {
                    "display_name": "Codex relay",
                    "mode": "relay",
                    "auth_file": str(relay_auth),
                    "app_profile_dir": str(relay_profile),
                    "base_url": "https://relay.example/v1",
                    "api_key": "sk-upstream",
                    "protocol": "responses",
                    "model": "gpt-5.6-sol",
                    "model_source": "manual",
                    "models": ["gpt-5.6-sol"],
                    "supports_service_tier": False,
                },
            },
        })

        with patch.object(
            sub2cli_inject,
            "refresh_model_catalog_for_cfg",
            return_value=["gpt-5.6-sol"],
        ), patch.object(sub2cli_inject, "ensure_protocol_proxy", return_value=False), \
             patch.object(sub2cli_inject, "protocol_proxy_healthy", return_value=True):
            rc = sub2cli_inject.cmd_switch("relay", no_restart=True)

        self.assertEqual(0, rc)
        live_auth = json.loads(sub2cli_inject.AUTH_JSON.read_text(encoding="utf-8"))
        self.assertEqual("apikey", live_auth["auth_mode"])
        self.assertEqual(sub2cli_inject.LEGACY_POOL_PLACEHOLDER_API_KEY, live_auth["OPENAI_API_KEY"])
        saved = json.loads(sub2cli_inject.SLOTS_FILE.read_text(encoding="utf-8"))
        self.assertNotIn("active_oauth_slot", saved)
        config = sub2cli_inject.CONFIG_TOML.read_text(encoding="utf-8")
        self.assertIn("experimental_bearer_token", config)
        self.assertIn("requires_openai_auth = false", config)

    def test_chatgpt_auth_marker_requires_nonempty_tokens(self):
        self.assertFalse(sub2cli_inject.auth_payload_is_chatgpt({"auth_mode": "chatgpt"}))
        self.assertTrue(sub2cli_inject.auth_payload_is_chatgpt({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "token"},
        }))

    def test_current_oauth_slot_outranks_stale_active_pointer(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        auth_a = codex_home / "auth.a.json"
        auth_b = codex_home / "auth.b.json"
        self.write_chatgpt_auth(auth_a, "account-a")
        self.write_chatgpt_auth(auth_b, "account-b")
        sub2cli_inject.copy_auth_atomic(auth_a)
        data = {
            "current": "a",
            "active_oauth_slot": "b",
            "slots": {
                "a": {"mode": "oauth", "auth_file": str(auth_a)},
                "b": {"mode": "oauth", "auth_file": str(auth_b)},
            },
        }
        self.assertEqual("a", sub2cli_inject.resolve_active_oauth_slot(data))

    def test_live_identity_match_outranks_mismatched_current_oauth_slot(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        auth_a = codex_home / "auth.a.json"
        auth_b = codex_home / "auth.b.json"
        self.write_chatgpt_auth(auth_a, "account-a")
        self.write_chatgpt_auth(auth_b, "account-b")
        sub2cli_inject.copy_auth_atomic(auth_b)
        data = {
            "current": "a",
            "active_oauth_slot": "a",
            "slots": {
                "a": {"mode": "oauth", "auth_file": str(auth_a)},
                "b": {"mode": "oauth", "auth_file": str(auth_b)},
            },
        }
        before_a = auth_a.read_bytes()
        resolved = sub2cli_inject.resolve_active_oauth_slot(data)
        self.assertEqual("b", resolved)
        sub2cli_inject.flush_auth_to_slot(sub2cli_inject.oauth_slot_auth_path(data, resolved))
        self.assertEqual(before_a, auth_a.read_bytes())

    def test_hintless_live_oauth_does_not_trust_stale_current_pointer(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        auth_a = codex_home / "auth.a.json"
        sub2cli_inject.atomic_write_json(auth_a, {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "saved-opaque-token"},
        })
        sub2cli_inject.atomic_write_json(sub2cli_inject.AUTH_JSON, {
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "different-opaque-token"},
        })
        data = {
            "current": "a",
            "active_oauth_slot": "a",
            "slots": {"a": {"mode": "oauth", "auth_file": str(auth_a)}},
        }

        self.assertIsNone(sub2cli_inject.resolve_active_oauth_slot(data))
        sub2cli_inject.copy_auth_atomic(auth_a)
        self.assertEqual("a", sub2cli_inject.resolve_active_oauth_slot(data))

    def test_backup_directories_and_saved_auth_are_private(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        auth_a = codex_home / "auth.a.json"
        self.write_chatgpt_auth(auth_a, "account-a")
        os.chmod(auth_a, 0o644)
        backup_dir = sub2cli_inject.new_backup_dir("private-modes")
        data = {
            "slots": {"a": {"mode": "oauth", "auth_file": str(auth_a)}},
        }

        backups = sub2cli_inject.backup_existing_slot_auth_files(backup_dir, data)
        backup_path = Path(next(iter(backups.values())))

        self.assertEqual(0o700, sub2cli_inject.BACKUP_ROOT.stat().st_mode & 0o777)
        self.assertEqual(0o700, backup_dir.stat().st_mode & 0o777)
        self.assertEqual(0o700, backup_path.parent.stat().st_mode & 0o777)
        self.assertEqual(0o600, backup_path.stat().st_mode & 0o777)

    def test_failed_oauth_registry_write_removes_unregistered_credential(self):
        home, codex_home, app_support = self.with_isolated_codex_home()
        local_auth = codex_home / "auth.local.json"
        self.write_chatgpt_auth(local_auth, "account-local")
        sub2cli_inject.copy_auth_atomic(local_auth)
        baseline = {
            "version": 1,
            "current": "local",
            "slots": {
                "local": {
                    "mode": "oauth",
                    "auth_file": str(local_auth),
                    "app_profile_dir": str(app_support / "Codex.local"),
                },
            },
        }
        sub2cli_inject.atomic_write_json(sub2cli_inject.SLOTS_FILE, baseline)
        imported = home / "imported-auth.json"
        self.write_chatgpt_auth(imported, "account-new")

        with patch.object(sub2cli_inject, "decode_auth_email", return_value="new@example.com"), \
             patch.object(sub2cli_inject, "refresh_oauth_auth_file", return_value=(True, "")), \
             patch.object(sub2cli_inject, "ensure_initialized", return_value=None), \
             patch.object(sub2cli_inject, "save_slots", side_effect=OSError("disk full")), \
             patch.object(sub2cli_inject, "quit_codex", return_value=None), \
             patch.object(sub2cli_inject, "stop_codex_cli_app_server", return_value=None):
            with self.assertRaises(OSError):
                sub2cli_inject.cmd_oauth(
                    "new",
                    auth_file=str(imported),
                    no_switch=True,
                    no_restart=True,
                )

        self.assertFalse((codex_home / "auth.new.json").exists())
        self.assertTrue(imported.exists())
        self.assertEqual(
            baseline,
            json.loads(sub2cli_inject.SLOTS_FILE.read_text(encoding="utf-8")),
        )

    def test_legacy_rollback_without_path_baseline_keeps_saved_credentials(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        auth_a = codex_home / "auth.a.json"
        auth_b = codex_home / "auth.b.json"
        self.write_chatgpt_auth(auth_a, "account-a")
        self.write_chatgpt_auth(auth_b, "account-b")
        self.write_chatgpt_auth(sub2cli_inject.AUTH_JSON, "account-a")
        backup_dir = sub2cli_inject.new_backup_dir("legacy-schema-two")
        auth_backup = backup_dir / "auth.json"
        auth_backup.write_bytes(sub2cli_inject.AUTH_JSON.read_bytes())
        sub2cli_inject.atomic_write_json(backup_dir / sub2cli_inject.SNAPSHOT_FILENAME, {
            "schema": 2,
            "auth_json_existed": True,
            "auth_json_backup": str(auth_backup),
        })

        with patch.object(sub2cli_inject, "quit_codex", return_value=None), \
             patch.object(sub2cli_inject, "stop_codex_cli_app_server", return_value=None):
            rc = sub2cli_inject.restore_from_backup(backup_dir)

        self.assertEqual(0, rc)
        self.assertTrue(auth_a.exists())
        self.assertTrue(auth_b.exists())

    def test_state_db_is_not_modified_when_rollback_backup_fails(self):
        _home, _codex_home, _app_support = self.with_isolated_codex_home()
        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            conn.execute("CREATE TABLE threads (model_provider TEXT NOT NULL)")
            conn.execute("INSERT INTO threads (model_provider) VALUES (?)", (sub2cli_inject.OAUTH_PROVIDER,))
        backup_dir = sub2cli_inject.new_backup_dir("backup-failure")
        original = sub2cli_inject.backup_state_db
        sub2cli_inject.backup_state_db = lambda *_args, **_kwargs: None
        try:
            stats = sub2cli_inject.normalize_state_db(
                sub2cli_inject.RELAY_PROVIDER,
                backup_dir=backup_dir,
                dry_run=False,
            )
        finally:
            sub2cli_inject.backup_state_db = original
        with sqlite3.connect(sub2cli_inject.STATE_DB) as conn:
            provider = conn.execute("SELECT model_provider FROM threads").fetchone()[0]
        self.assertEqual(sub2cli_inject.OAUTH_PROVIDER, provider)
        self.assertTrue(stats["errors"])

    def test_rollout_target_durability_error_remains_rollbackable(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        path = codex_home / "sessions" / "rollout-test.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"type":"session_meta","payload":{"id":"test","model_provider":"openai"}}\n',
            encoding="utf-8",
        )
        backup_dir = sub2cli_inject.new_backup_dir("rollout-fsync-failure")
        original_fsync = sub2cli_inject.os.fsync
        original_atomic_write = sub2cli_inject.atomic_write_text
        audit_ready = False

        def write_audit(*args, **kwargs):
            nonlocal audit_ready
            result = original_atomic_write(*args, **kwargs)
            audit_ready = True
            return result

        def fail_target_fsync(fd):
            if audit_ready:
                raise OSError("target fsync failed")
            return original_fsync(fd)

        sub2cli_inject.atomic_write_text = write_audit
        sub2cli_inject.os.fsync = fail_target_fsync
        try:
            stats = sub2cli_inject.normalize_rollouts(
                sub2cli_inject.RELAY_PROVIDER,
                backup_dir=backup_dir,
                dry_run=False,
            )
        finally:
            sub2cli_inject.os.fsync = original_fsync
            sub2cli_inject.atomic_write_text = original_atomic_write
        self.assertEqual(1, stats["changed"])
        self.assertEqual(1, stats["errors"])
        self.assertIn('"model_provider":"OpenAI"', path.read_text(encoding="utf-8"))
        restored = sub2cli_inject.restore_rollouts_from_audit(
            backup_dir / "rollout-provider-changes.jsonl"
        )
        self.assertEqual(1, restored["reverted"])
        self.assertIn('"model_provider":"openai"', path.read_text(encoding="utf-8"))

    def test_rollout_audit_failure_does_not_modify_target(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        path = codex_home / "sessions" / "rollout-test.jsonl"
        path.parent.mkdir(parents=True)
        original = '{"type":"session_meta","payload":{"id":"test","model_provider":"openai"}}\n'
        path.write_text(original, encoding="utf-8")
        backup_dir = sub2cli_inject.new_backup_dir("audit-failure")
        with patch.object(sub2cli_inject, "atomic_write_text", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                sub2cli_inject.normalize_rollouts(
                    sub2cli_inject.RELAY_PROVIDER,
                    backup_dir=backup_dir,
                    dry_run=False,
                )
        self.assertEqual(original, path.read_text(encoding="utf-8"))

    def test_restore_state_db_copy_failure_keeps_live_database(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        backup = codex_home / "backup.sqlite"
        live = sub2cli_inject.STATE_DB
        backup.write_bytes(b"backup-db")
        live.write_bytes(b"live-db")
        with patch.object(sub2cli_inject.shutil, "copyfile", side_effect=OSError("copy failed")):
            with self.assertRaises(OSError):
                sub2cli_inject.restore_state_db_atomic(backup, live)
        self.assertEqual(b"live-db", live.read_bytes())

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

    def test_sanitize_rewrites_codex_ultra_effort_to_max(self):
        body = sub2cli_inject.sanitize_responses_body_for_upstream(
            {
                "model": "gpt-5.6-sol",
                "input": "hi",
                "reasoning": {"effort": "ultra", "summary": "auto"},
            },
            log=False,
        )
        self.assertEqual("max", body["reasoning"]["effort"])
        self.assertEqual("auto", body["reasoning"]["summary"])

    def test_sanitize_leaves_api_efforts_alone(self):
        for effort in ("low", "medium", "high", "xhigh", "max"):
            body = sub2cli_inject.sanitize_responses_body_for_upstream(
                {"reasoning": {"effort": effort}},
                log=False,
            )
            self.assertEqual(effort, body["reasoning"]["effort"])

    def test_request_route_response_rewrites_ultra_before_upstream(self):
        seen = []

        class FakeResp:
            def __init__(self):
                self.headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"id":"r1","status":"completed","output":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            payload = json.loads(req.data.decode("utf-8"))
            seen.append(payload)
            return FakeResp()

        original = sub2cli_inject.fresh_urlopen
        sub2cli_inject.fresh_urlopen = fake_urlopen
        try:
            body, ctype = sub2cli_inject.request_route_response(
                {
                    "id": "r1",
                    "base_url": "https://relay.example/v1",
                    "api_key": "sk-test",
                    "protocol": "responses",
                    "model": "gpt-5.6-sol",
                },
                {
                    "model": "gpt-5.6-sol",
                    "input": "hi",
                    "reasoning": {"effort": "ultra"},
                },
                timeout=5,
            )
        finally:
            sub2cli_inject.fresh_urlopen = original

        self.assertEqual(1, len(seen))
        self.assertEqual("max", seen[0]["reasoning"]["effort"])
        self.assertEqual("gpt-5.6-sol", seen[0]["model"])
        self.assertIn("application/json", ctype)
        self.assertTrue(body)

    def test_best_default_model_prefers_sol_over_siblings(self):
        models = [
            "gpt-5.5",
            "gpt-5.6-luna",
            "gpt-5.6-sol-pro",
            "gpt-5.6-sol",
            "gpt-5.6-terra",
        ]
        self.assertEqual("gpt-5.6-sol", sub2cli_inject.best_default_model(models))
        self.assertEqual("gpt-5.6-sol", sub2cli_inject.DEFAULT_MODEL)

    def test_best_default_model_prefers_newer_major_over_old_sol(self):
        models = ["gpt-5.6-sol", "gpt-6", "gpt-6-sol", "gpt-7"]
        self.assertEqual("gpt-7", sub2cli_inject.best_default_model(models))
        self.assertEqual(
            "gpt-6-sol",
            sub2cli_inject.best_default_model(["gpt-5.6-sol", "gpt-6-sol", "gpt-6-luna"]),
        )

    def test_legacy_route_model_string_preserves_manual_pin(self):
        route = {"id": "r1", "model": "gpt-5.5", "base_url": "https://x", "api_key": "sk"}
        self.assertEqual("manual", sub2cli_inject.route_model_mode(route))
        self.assertEqual(
            "gpt-5.5",
            sub2cli_inject.resolve_route_model(route, request_model="gpt-5.6-sol"),
        )

    def test_manual_route_pins_model(self):
        route = {
            "id": "r1",
            "model": "gpt-5.5",
            "model_source": "manual",
            "base_url": "https://x",
            "api_key": "sk",
        }
        self.assertEqual("manual", sub2cli_inject.route_model_mode(route))
        self.assertEqual(
            "gpt-5.5",
            sub2cli_inject.resolve_route_model(route, request_model="gpt-5.6-sol"),
        )

    def test_request_route_response_auto_keeps_client_model(self):
        seen = []

        class FakeResp:
            def __init__(self):
                self.headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"id":"r1","status":"completed","output":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            seen.append(json.loads(req.data.decode("utf-8")))
            return FakeResp()

        original = sub2cli_inject.fresh_urlopen
        sub2cli_inject.fresh_urlopen = fake_urlopen
        try:
            sub2cli_inject.request_route_response(
                {
                    "id": "r1",
                    "base_url": "https://relay.example/v1",
                    "api_key": "sk-test",
                    "protocol": "responses",
                    "model": "gpt-5.5",
                    "model_source": "auto",
                },
                {
                    "model": "gpt-7-sol",
                    "input": "hi",
                    "reasoning": {"effort": "max"},
                },
                timeout=5,
            )
        finally:
            sub2cli_inject.fresh_urlopen = original

        self.assertEqual(1, len(seen))
        self.assertEqual("gpt-7-sol", seen[0]["model"])

    def test_request_route_response_forwards_manual_model_pin(self):
        seen = []

        class FakeResp:
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"id":"r1","status":"completed","output":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def fake_urlopen(req, timeout=0):
            seen.append(json.loads(req.data.decode("utf-8")))
            return FakeResp()

        route = {
            "id": "r1",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-test",
            "protocol": "responses",
            "model": "gpt-5.5",
            "model_source": "manual",
        }
        with patch.object(sub2cli_inject, "fresh_urlopen", side_effect=fake_urlopen):
            sub2cli_inject.request_route_response(
                route,
                {"model": "gpt-7-sol", "input": "hi"},
                timeout=5,
            )

        self.assertEqual("gpt-5.5", seen[0]["model"])

    def test_pool_choose_only_routes_that_advertise_model(self):
        runtime = sub2cli_inject.RoutePoolRuntime()
        cfg = {
            "protocol": "pool",
            "policy": {"fail_consecutive": 2, "recovery_successes": 2, "min_dwell_seconds": 0, "cooldown_seconds": [30]},
            "routes": [
                {
                    "id": "r1",
                    "priority": 10,
                    "_order": 1,
                    "base_url": "https://one.example/v1",
                    "api_key": "sk-1",
                    "protocol": "responses",
                },
                {
                    "id": "r2",
                    "priority": 20,
                    "_order": 2,
                    "base_url": "https://two.example/v1",
                    "api_key": "sk-2",
                    "protocol": "responses",
                },
            ],
        }
        runtime._model_catalogs = {
            "r1": {"models": ["gpt-5.1", "gpt-5.2", "gpt-5.3"], "updated": 1e18, "error": ""},
            "r2": {"models": ["gpt-5.3", "gpt-5.4", "gpt-5.5"], "updated": 1e18, "error": ""},
        }

        route, err = runtime.choose(cfg, model="gpt-5.2")
        self.assertIsNone(err)
        self.assertEqual("r1", route["id"])

        route, err = runtime.choose(cfg, model="gpt-5.5")
        self.assertIsNone(err)
        self.assertEqual("r2", route["id"])

        route, err = runtime.choose(cfg, model="gpt-5.3")
        self.assertIsNone(err)
        self.assertEqual("r1", route["id"])  # higher priority among both

        route, err = runtime.choose(cfg, model="gpt-5.3", exclude_ids={"r1"})
        self.assertIsNone(err)
        self.assertEqual("r2", route["id"])  # failover only to routes that have it

        route, err = runtime.choose(cfg, model="gpt-5.5", exclude_ids={"r2"})
        self.assertIsNone(route)
        self.assertIn("no usable route for model", err)

        route, err = runtime.choose(cfg, model="gpt-9")
        self.assertIsNone(route)
        self.assertIn("no route supports model", err)

    def test_manual_route_only_advertises_and_routes_its_pin(self):
        runtime = sub2cli_inject.RoutePoolRuntime()
        pinned = {
            "id": "pinned",
            "priority": 10,
            "_order": 1,
            "base_url": "https://pinned.example/v1",
            "api_key": "sk-pinned",
            "protocol": "responses",
            "model": "gpt-a",
            "model_source": "manual",
        }
        fallback = {
            "id": "fallback",
            "priority": 20,
            "_order": 2,
            "base_url": "https://fallback.example/v1",
            "api_key": "sk-fallback",
            "protocol": "responses",
            "model_source": "auto",
        }
        cfg = {"protocol": "pool", "routes": [pinned, fallback]}
        runtime._model_catalogs = {
            "pinned": {
                "models": ["gpt-a", "gpt-b"],
                "updated": 1e18,
                "error": "",
            },
            "fallback": {"models": ["gpt-b"], "updated": 1e18, "error": ""},
        }

        with patch.object(
            sub2cli_inject,
            "live_models_for_route",
            side_effect=AssertionError("manual route must not fetch /models"),
        ):
            self.assertEqual(["gpt-a"], runtime.models_for_route(pinned, refresh=False))
            route, err = runtime.choose(cfg, model="gpt-b")
            models = runtime.aggregate_models(cfg, refresh=False)

        self.assertIsNone(err)
        self.assertEqual("fallback", route["id"])
        self.assertEqual(["gpt-b", "gpt-a"], models)

    def test_route_fingerprint_change_clears_health_and_catalog_state(self):
        runtime = sub2cli_inject.RoutePoolRuntime()
        old = {
            "id": "primary",
            "base_url": "https://old.example/v1",
            "api_key": "sk-old",
            "protocol": "responses",
        }
        runtime._sync([old])
        runtime._state("primary")["blocked"] = True
        runtime._model_catalogs["primary"] = {
            "models": ["gpt-old"],
            "updated": 1e18,
            "error": "",
        }
        runtime._current_route_id = "primary"

        changed_url = {**old, "base_url": "https://new.example/v1"}
        runtime._sync([changed_url])
        self.assertNotIn("primary", runtime._states)
        self.assertNotIn("primary", runtime._model_catalogs)
        self.assertIsNone(runtime._current_route_id)

        runtime._state("primary")["blocked"] = True
        runtime._model_catalogs["primary"] = {"models": ["gpt-new"], "updated": 1e18, "error": ""}
        changed_key = {**changed_url, "api_key": "sk-rotated"}
        runtime._sync([changed_key])
        self.assertNotIn("primary", runtime._states)
        self.assertNotIn("primary", runtime._model_catalogs)

    def test_aggregate_models_unions_route_catalogs(self):
        runtime = sub2cli_inject.RoutePoolRuntime()
        cfg = {
            "protocol": "pool",
            "routes": [
                {"id": "r1", "priority": 10, "_order": 1, "base_url": "https://a", "api_key": "sk", "protocol": "responses"},
                {"id": "r2", "priority": 20, "_order": 2, "base_url": "https://b", "api_key": "sk", "protocol": "responses"},
            ],
        }
        runtime._model_catalogs = {
            "r1": {"models": ["gpt-5.1", "gpt-5.3"], "updated": 1e18, "error": ""},
            "r2": {"models": ["gpt-5.3", "gpt-5.5"], "updated": 1e18, "error": ""},
        }
        models = runtime.aggregate_models(cfg, refresh=False)
        self.assertEqual(["gpt-5.5", "gpt-5.3", "gpt-5.1"], models)

    def test_add_pool_discovers_each_route_catalog_once(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        routes_path = codex_home / "routes.json"
        routes_path.write_text(json.dumps({
            "routes": [
                {"id": "a", "base_url": "https://a.example/v1", "api_key": "sk-a"},
                {"id": "b", "base_url": "https://b.example/v1", "api_key": "sk-b"},
            ],
        }), encoding="utf-8")
        calls = []

        def fake_models(route, *, timeout):
            calls.append(route["id"])
            return ["gpt-5.6-sol"]

        original_runtime = sub2cli_inject.ROUTE_POOL_RUNTIME
        sub2cli_inject.ROUTE_POOL_RUNTIME = sub2cli_inject.RoutePoolRuntime()
        try:
            with patch.object(sub2cli_inject, "live_models_for_route", side_effect=fake_models), \
                 patch.object(sub2cli_inject, "ensure_protocol_proxy", return_value=False), \
                 patch.object(sub2cli_inject, "protocol_proxy_healthy", return_value=True):
                rc = sub2cli_inject.cmd_pool(
                    "test-pool",
                    routes_json=str(routes_path),
                    no_restart=True,
                )
        finally:
            sub2cli_inject.ROUTE_POOL_RUNTIME = original_runtime
        self.assertEqual(0, rc)
        self.assertEqual(["a", "b"], calls)

    def test_normalize_pool_json_preserves_legacy_route_model_pin(self):
        routes, _policy, models = sub2cli_inject.normalize_pool_json({
            "routes": [{
                "id": "relay-1",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-relay",
                "protocol": "responses",
                "model": "gpt-5.5",
            }],
        })
        self.assertEqual("manual", routes[0]["model_source"])
        self.assertEqual("gpt-5.5", routes[0]["model"])

    def test_normalize_pool_json_clears_explicit_auto_route_model(self):
        routes, _policy, models = sub2cli_inject.normalize_pool_json({
            "routes": [{
                "id": "auto",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-test",
                "model": "gpt-5.5",
                "model_source": "auto",
            }],
        })
        self.assertEqual("auto", routes[0]["model_source"])
        self.assertEqual("", routes[0]["model"])
        self.assertEqual([], models)

    def test_invalid_effort_400_is_client_error_not_failover(self):
        kind = sub2cli_inject.classify_upstream_status(
            400,
            b'{"error":{"message":"Invalid value: \'ultra\'. Supported values are: \'xhigh\'."}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("client_error", kind)

    def test_unsupported_service_tier_allows_route_failover(self):
        kind = sub2cli_inject.classify_upstream_status(
            400,
            b'{"error":{"message":"Unsupported parameter: service_tier"}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("unsupported_capability", kind)

    def test_image_generation_disabled_403_does_not_block_route(self):
        kind = sub2cli_inject.classify_upstream_status(
            403,
            b'{"error":{"message":"Image generation is not enabled for this group"}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("unsupported_capability", kind)
        cfg = pool_cfg()
        route = cfg["routes"][0]
        runtime = sub2cli_inject.RoutePoolRuntime()
        runtime.record_failure(
            route,
            cfg,
            kind=kind,
            status=403,
            detail="HTTP 403 Forbidden: Image generation is not enabled for this group",
        )
        snap = runtime.snapshot(cfg)["states"][route["id"]]
        self.assertFalse(snap["blocked"])
        self.assertEqual(0, snap["cooldown_remaining"])

    def test_generic_unsupported_parameter_is_client_error(self):
        kind = sub2cli_inject.classify_upstream_status(
            400,
            b'{"error":{"message":"invalid request: unsupported parameter input_mode"}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("client_error", kind)

    def test_unsupported_model_is_retryable_on_another_route(self):
        kind = sub2cli_inject.classify_upstream_status(
            400,
            b'{"error":{"message":"requested model is unsupported"}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("retryable", kind)

    def test_missing_response_id_404_is_client_error(self):
        kind = sub2cli_inject.classify_upstream_status(
            404,
            b'{"error":{"message":"No response found with id resp_missing"}}',
            "application/json",
            route={"source_type": "relay"},
        )
        self.assertEqual("client_error", kind)

    def test_missing_endpoint_404_is_retryable_on_another_route(self):
        for body in (
            b'{"error":{"message":"Not Found"}}',
            b'{"detail":"Not Found"}',
            b'{"message":"Not Found"}',
            b'{"error":"Not Found"}',
            b'{"status":404,"title":"Not Found"}',
            b'{"error":{"message":"The requested URL was not found"}}',
            b'Cannot POST /v1/responses/compact',
            b'<html><title>404 Not Found</title></html>',
            b'404 page not found',
        ):
            with self.subTest(body=body):
                kind = sub2cli_inject.classify_upstream_status(
                    404,
                    body,
                    "application/json",
                    route={"source_type": "relay"},
                )
                self.assertEqual("retryable", kind)

    def test_service_tier_rejection_wording_is_consistent_for_http_and_sse(self):
        messages = [
            "service_tier priority is not supported",
            "Invalid service_tier",
            "unknown parameter service_tier",
            "service tier priority unavailable",
            "Invalid value 'priority'. Supported values are 'default'. param service_tier",
        ]
        route = {"id": "relay", "source_type": "relay", "api_key": "sk-test"}
        for message in messages:
            with self.subTest(message=message):
                payload = json.dumps({"error": {"message": message}}).encode("utf-8")
                self.assertEqual(
                    "unsupported_capability",
                    sub2cli_inject.classify_upstream_status(
                        400,
                        payload,
                        "application/json",
                        route=route,
                    ),
                )
                sse = (
                    "event: response.failed\n"
                    f"data: {json.dumps({'response': {'status': 'failed', 'error': {'message': message}}})}\n\n"
                ).encode("utf-8")
                with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                    sub2cli_inject._prime_native_response_stream(
                        route,
                        io.BytesIO(sse),
                        "text/event-stream",
                    )
                self.assertEqual("unsupported_capability", raised.exception.kind)

    def test_models_envelope_prefers_nested_model_ids(self):
        payload = {
            "name": "default",
            "data": [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}],
        }
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.5"],
            sub2cli_inject.parse_models_payload(payload),
        )

    def test_normalize_pool_json_preserves_explicit_source_type(self):
        routes, _policy, _models = sub2cli_inject.normalize_pool_json({
            "routes": [{
                "id": "relay-primary",
                "source_type": "relay",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-relay",
                "protocol": "responses",
            }],
        })

        self.assertEqual("relay", routes[0]["source_type"])

    def test_normalize_base_url_keeps_openai_api_base_with_v1(self):
        self.assertEqual(
            "https://relay.example/v1",
            sub2cli_inject.normalize_base_url("https://relay.example"),
        )
        self.assertEqual(
            "https://relay.example/v1",
            sub2cli_inject.normalize_base_url("https://relay.example/v1"),
        )

    def test_probe_relay_uses_single_v1_models_path(self):
        calls = []
        original = sub2cli_inject.fresh_urlopen

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return b'{"data":[]}'

        def fake_fresh(req, *, timeout):
            calls.append((req.full_url, timeout))
            return FakeResponse()

        sub2cli_inject.fresh_urlopen = fake_fresh
        try:
            ok, message = sub2cli_inject.probe_relay("https://relay.example/v1", "sk-test", timeout=5)
        finally:
            sub2cli_inject.fresh_urlopen = original

        self.assertTrue(ok)
        self.assertIn("/v1/models", message)
        self.assertEqual([("https://relay.example/v1/models", 5)], calls)

    def test_patch_config_adds_v1_for_legacy_root_slot(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example",
            protocol="responses",
        )

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn(f'base_url = "{sub2cli_inject.PROTOCOL_PROXY_BASE_URL}"', text)
        self.assertIn("requires_openai_auth = true", text)
        self.assertIn("[features]", text)
        self.assertIn("enable_request_compression = false", text)
        self.assertIn("sub2cli-request-compression-original=missing-table", text)
        token = sub2cli_inject.proxy_bearer_token(create=False)
        self.assertTrue(token)
        self.assertIn(f'experimental_bearer_token = "{token}"', text)
        self.assertNotIn('base_url = "https://relay.example/v1"', text)

    def test_patch_config_marks_pure_relay_as_not_requiring_openai_login(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "base_url": "https://relay.example/v1",
            "model": sub2cli_inject.DEFAULT_MODEL,
            "model_source": "manual",
            "models": [sub2cli_inject.DEFAULT_MODEL],
            "supports_service_tier": False,
        }
        sub2cli_inject.write_apikey_auth(
            sub2cli_inject.AUTH_JSON,
            sub2cli_inject.LEGACY_POOL_PLACEHOLDER_API_KEY,
        )

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
            slot_cfg=cfg,
            requires_openai_auth=False,
        )

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertIn("requires_openai_auth = false", text)
        self.assertIn("experimental_bearer_token", text)
        self.assertTrue(sub2cli_inject.config_clean_for_slot(cfg))

    def test_patch_config_restores_request_compression_after_oauth_switch(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"
        config_path.write_text(
            "[features]\n"
            "enable_request_compression = true\n"
            "remote_plugin = false\n",
            encoding="utf-8",
        )

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
        )
        relay_text = config_path.read_text(encoding="utf-8")
        self.assertIn("sub2cli-request-compression-original=line-", relay_text)
        self.assertIn("enable_request_compression = false", relay_text)
        self.assertIn("remote_plugin = false", relay_text)

        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        oauth_text = config_path.read_text(encoding="utf-8")
        self.assertNotIn("sub2cli-request-compression-original", oauth_text)
        self.assertIn("enable_request_compression = true", oauth_text)
        self.assertIn("remote_plugin = false", oauth_text)

    def test_patch_config_accepts_terminal_features_header_without_newline(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"
        config_path.write_text("[features] # user comment", encoding="utf-8")

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
        )

        relay_text = config_path.read_text(encoding="utf-8")
        parsed = __import__("tomllib").loads(relay_text)
        self.assertFalse(parsed["features"]["enable_request_compression"])
        self.assertEqual(1, relay_text.count("[features]"))

        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        oauth_text = config_path.read_text(encoding="utf-8")
        self.assertEqual(1, oauth_text.count("[features]"))
        self.assertIn("[features] # user comment", oauth_text)
        self.assertNotIn("sub2cli-request-compression-original", oauth_text)
        self.assertNotIn("enable_request_compression", oauth_text)
        __import__("tomllib").loads(oauth_text)

    def test_patch_config_restores_spaced_features_table_and_comments(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"
        config_path.write_text(
            "[ features ]\n"
            "enable_request_compression = true # user setting\n"
            "remote_plugin = false\n",
            encoding="utf-8",
        )

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
        )
        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        restored = config_path.read_text(encoding="utf-8")

        self.assertIn("[ features ]", restored)
        self.assertIn("enable_request_compression = true # user setting", restored)
        self.assertIn("remote_plugin = false", restored)
        self.assertNotIn("sub2cli-request-compression-original", restored)

    def test_patch_config_restores_dotted_feature_and_custom_top_level_values(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"
        config_path.write_text(
            'features.enable_request_compression = true # keep dotted\n'
            'model_catalog_json = "/custom/catalog.json" # keep catalog\n'
            'disable_response_storage = false # keep storage\n',
            encoding="utf-8",
        )

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
        )
        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        restored = config_path.read_text(encoding="utf-8")

        self.assertIn("features.enable_request_compression = true # keep dotted", restored)
        self.assertIn('model_catalog_json = "/custom/catalog.json" # keep catalog', restored)
        self.assertIn("disable_response_storage = false # keep storage", restored)
        self.assertNotIn("sub2cli-model-catalog-original", restored)
        self.assertNotIn("sub2cli-disable-response-storage-original", restored)

    def test_direct_oauth_patch_preserves_unmanaged_top_level_values(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"
        config_path.write_text(
            'model_catalog_json = "/custom/catalog.json" # user owned\n'
            'disable_response_storage = false # user owned\n',
            encoding="utf-8",
        )

        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        restored = config_path.read_text(encoding="utf-8")

        self.assertIn('model_catalog_json = "/custom/catalog.json" # user owned', restored)
        self.assertIn("disable_response_storage = false # user owned", restored)

    def test_patch_config_removes_injected_compression_default_on_oauth(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        config_path = codex_home / "config.toml"

        sub2cli_inject.patch_config(
            mode="relay",
            model=sub2cli_inject.DEFAULT_MODEL,
            relay_base_url="https://relay.example/v1",
            protocol="responses",
        )
        sub2cli_inject.patch_config(mode="oauth", model=sub2cli_inject.DEFAULT_MODEL)
        oauth_text = config_path.read_text(encoding="utf-8")
        self.assertNotIn("sub2cli-request-compression-original", oauth_text)
        self.assertNotIn("enable_request_compression", oauth_text)

    def test_patch_config_writes_model_catalog_json_for_relay(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        # Seed an official-style cache entry so enrichment path is exercised.
        (codex_home / "models_cache.json").write_text(
            json.dumps({
                "models": [{
                    "slug": "gpt-5.6-sol",
                    "display_name": "GPT-5.6-Sol",
                    "supported_in_api": True,
                    "visibility": "list",
                    "default_reasoning_level": "low",
                    "supported_reasoning_levels": [
                        {"effort": "low", "description": "low"},
                        {"effort": "ultra", "description": "ultra"},
                    ],
                    "service_tiers": [
                        {"id": "priority", "name": "Fast", "description": "1.5x"},
                    ],
                    "additional_speed_tiers": ["fast"],
                }],
            }),
            encoding="utf-8",
        )

        sub2cli_inject.patch_config(
            mode="relay",
            model="gpt-5.6-sol",
            relay_base_url="https://relay.example/v1",
            protocol="pool",
            model_ids=["gpt-5.1", "gpt-5.3", "gpt-5.5", "gpt-5.6-sol", "gpt-image-1"],
            slot_cfg={
                "protocol": "pool",
                "routes": [{
                    "id": "catalog-response-route",
                    "base_url": "https://relay.example/v1",
                    "api_key": "sk-relay",
                    "protocol": "responses",
                    "supports_service_tier": True,
                }],
            },
        )

        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        catalog_path = codex_home / "sub2cli-model-catalog.json"
        self.assertIn(f'model_catalog_json = "{catalog_path}"', text)
        self.assertTrue(catalog_path.is_file())
        payload = json.loads(catalog_path.read_text(encoding="utf-8"))
        slugs = [m["slug"] for m in payload["models"]]
        # Image models filtered; pool union kept.
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.5", "gpt-5.3", "gpt-5.1"],
            slugs,
        )
        sol = next(m for m in payload["models"] if m["slug"] == "gpt-5.6-sol")
        self.assertTrue(sol["supported_in_api"])
        efforts = [level["effort"] for level in sol["supported_reasoning_levels"]]
        self.assertIn("ultra", efforts)
        self.assertEqual(["fast"], sol.get("additional_speed_tiers"))
        # Unknown ids stay parser-valid and listable without inheriting
        # reasoning/Fast/tool claims from a different official model.
        m51 = next(m for m in payload["models"] if m["slug"] == "gpt-5.1")
        self.assertTrue(str(m51.get("base_instructions") or "").strip())
        self.assertTrue(m51.get("supported_in_api"))
        self.assertEqual("list", m51.get("visibility"))
        efforts51 = [level["effort"] for level in m51["supported_reasoning_levels"]]
        self.assertEqual([], efforts51)

        # OAuth switch must clear the catalog override.
        sub2cli_inject.patch_config(mode="oauth", model="gpt-5.6-sol")
        text = (codex_home / "config.toml").read_text(encoding="utf-8")
        self.assertNotIn("model_catalog_json", text)

    def test_catalog_entry_forces_api_visibility_for_chatgpt_only_models(self):
        official = {
            "gpt-5.3-codex-spark": {
                "slug": "gpt-5.3-codex-spark",
                "display_name": "GPT-5.3-Codex-Spark",
                "supported_in_api": False,
                "visibility": "list",
                "supported_reasoning_levels": [{"effort": "high", "description": "h"}],
                "service_tiers": [],
                "additional_speed_tiers": [],
                "base_instructions": "You are Codex.",
            }
        }
        entry = sub2cli_inject.catalog_entry_for_model("gpt-5.3-codex-spark", official)
        self.assertTrue(entry["supported_in_api"])
        self.assertEqual("list", entry["visibility"])
        self.assertTrue(entry.get("base_instructions"))

    def test_mixed_pool_advertises_fast_when_one_route_supports_it(self):
        cfg = {
            "protocol": "pool",
            "routes": [
                {
                    "id": "responses-fast",
                    "base_url": "https://responses.example/v1",
                    "api_key": "sk-responses",
                    "protocol": "responses",
                    "supports_service_tier": True,
                },
                {
                    "id": "chat-standard",
                    "base_url": "https://chat.example/v1",
                    "api_key": "sk-chat",
                    "protocol": "chat",
                },
            ],
        }
        self.assertTrue(sub2cli_inject.model_supports_service_tier(cfg, "gpt-5.6-sol"))

    def test_service_tier_capability_is_scoped_to_verified_model(self):
        route = {
            "id": "verified-one-model",
            "base_url": "https://responses.example/v1",
            "api_key": "sk-responses",
            "protocol": "responses",
            "models": ["gpt-fast", "gpt-standard"],
            "service_tier_models": ["gpt-fast"],
        }
        cfg = {"protocol": "pool", "routes": [route]}

        self.assertTrue(sub2cli_inject.route_supports_service_tier(route, "gpt-fast"))
        self.assertFalse(sub2cli_inject.route_supports_service_tier(route, "gpt-standard"))
        self.assertTrue(sub2cli_inject.model_supports_service_tier(cfg, "gpt-fast"))
        self.assertFalse(sub2cli_inject.model_supports_service_tier(cfg, "gpt-standard"))

        runtime = sub2cli_inject.RoutePoolRuntime()
        runtime._model_catalogs["verified-one-model"] = {
            "models": ["gpt-fast", "gpt-standard"],
            "updated": 1e18,
            "error": "",
        }
        self.assertEqual(
            [route],
            runtime.routes_for_model(
                cfg,
                "gpt-fast",
                refresh=False,
                service_tier="priority",
            ),
        )
        self.assertEqual(
            [],
            runtime.routes_for_model(
                cfg,
                "gpt-standard",
                refresh=False,
                service_tier="priority",
            ),
        )

    def test_chat_route_cannot_force_service_tier_support(self):
        route = {
            "protocol": "chat",
            "supports_service_tier": True,
        }
        self.assertFalse(sub2cli_inject.route_supports_service_tier(route))

    def test_synthesize_unknown_model_includes_required_base_instructions(self):
        official = {
            "gpt-5.5": {
                "slug": "gpt-5.5",
                "display_name": "GPT-5.5",
                "supported_in_api": True,
                "visibility": "list",
                "base_instructions": "Official base instructions body.",
                "supported_reasoning_levels": [{"effort": "high", "description": "h"}],
                "service_tiers": [{"id": "priority", "name": "Fast", "description": "1.5x"}],
                "additional_speed_tiers": ["fast"],
            }
        }
        entry = sub2cli_inject.synthesize_model_catalog_entry("gpt-5.2-pro", official=official)
        self.assertEqual("gpt-5.2-pro", entry["slug"])
        self.assertEqual("Official base instructions body.", entry["base_instructions"])
        self.assertTrue(entry["supported_in_api"])
        # No official cache at all still validates.
        bare = sub2cli_inject.synthesize_model_catalog_entry("custom-model-x", official={})
        self.assertTrue(str(bare.get("base_instructions") or "").strip())

    def test_fresh_default_model_keeps_reasoning_but_fast_requires_route_proof(self):
        entry = sub2cli_inject.synthesize_model_catalog_entry(
            sub2cli_inject.DEFAULT_MODEL,
            official={},
        )
        efforts = [level["effort"] for level in entry["supported_reasoning_levels"]]
        self.assertEqual(["low", "medium", "high", "xhigh", "max", "ultra"], efforts)

        cfg = {
            "protocol": "responses",
            "supports_service_tier": False,
        }
        constrained = sub2cli_inject.constrain_catalog_entry_for_routes(
            entry,
            cfg,
            sub2cli_inject.DEFAULT_MODEL,
        )
        self.assertEqual([], constrained["service_tiers"])
        self.assertEqual([], constrained["additional_speed_tiers"])

    def test_live_pool_catalog_drops_stale_stored_models(self):
        cfg = {
            "mode": "relay",
            "protocol": "pool",
            "model": "gpt-old",
            "model_source": "auto",
            "models": ["gpt-old"],
            "routes": [{
                "id": "primary",
                "base_url": "https://relay.example/v1",
                "api_key": "sk-test",
                "protocol": "responses",
                "model_source": "auto",
            }],
        }
        with patch.object(
            sub2cli_inject.ROUTE_POOL_RUNTIME,
            "models_for_route",
            return_value=["gpt-new"],
        ):
            catalog = sub2cli_inject.relay_model_catalog_for_slot(cfg, refresh=True)

        self.assertEqual(["gpt-new"], catalog["models"])
        self.assertEqual("gpt-new", catalog["default_model"])

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

    def test_relay_source_capacity_error_still_retries_as_official_issue(self):
        route = {
            "id": "primary",
            "source_type": "relay",
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

    def test_relay_source_balance_error_payload_is_blocked(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example",
            "api_key": "sk-relay",
            "protocol": "responses",
        }
        calls = []
        original = sub2cli_inject.fresh_urlopen
        original_delays = sub2cli_inject.OPENAI_TRANSIENT_RETRY_DELAYS

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return b'{"error":{"message":"insufficient balance"}}'

        def fake_fresh(req, *, timeout):
            calls.append(req)
            return FakeResponse()

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
        self.assertEqual("blocked", raised.exception.kind)
        self.assertIn(b"insufficient balance", raised.exception.body)

    def test_single_relay_route_preserves_source_type_for_business_errors(self):
        route = sub2cli_inject.single_route_from_slot({
            "display_name": "single",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-relay",
            "protocol": "responses",
            "model": "gpt-5.6-sol",
        })
        self.assertEqual("relay", route["source_type"])
        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject.raise_for_upstream_success_error(
                route,
                b'{"error":{"message":"insufficient balance"}}',
                "application/json",
            )
        self.assertEqual("blocked", raised.exception.kind)

    def test_relay_subscription_daily_quota_payload_is_blocked(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example",
            "api_key": "sk-relay",
            "protocol": "responses",
            "group": "乐于助人卡",
            "group_id": 35,
        }

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject.raise_for_upstream_success_error(
                route,
                '{"message":"分组 乐于助人卡 订阅日卡用量已打满"}'.encode("utf-8"),
                "application/json",
            )

        self.assertEqual("blocked", raised.exception.kind)
        self.assertEqual(403, raised.exception.status)

    def test_relay_balance_payload_is_blocked(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example",
            "api_key": "sk-relay",
            "protocol": "responses",
        }
        kind = sub2cli_inject.classify_upstream_status(
            403,
            "用户额度不足, 剩余额度: ¥-0.008726".encode("utf-8"),
            "text/plain",
            route=route,
        )

        self.assertEqual("blocked", kind)

    def test_relay_502_retries_same_route_before_returning_retryable(self):
        route = {
            "id": "primary",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        calls = []
        original = sub2cli_inject.fresh_urlopen
        original_delays = sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS

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
        sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS = (0, 0)
        try:
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_response_with_openai_retries(
                    route,
                    {"model": "gpt-test", "input": "hi"},
                    timeout=3,
                )
        finally:
            sub2cli_inject.fresh_urlopen = original
            sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS = original_delays

        self.assertEqual(3, len(calls))
        self.assertEqual("retryable", raised.exception.kind)
        self.assertEqual(502, raised.exception.status)

    def test_relay_http_429_is_rate_limit_not_terminal_client_error(self):
        route = {
            "id": "primary",
            "source_type": "relay",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        kind = sub2cli_inject.classify_upstream_status(
            429,
            b'{"error":{"message":"Too Many Requests"}}',
            "application/json",
            route=route,
        )

        self.assertEqual("rate_limit", kind)

    def test_relay_wrapped_429_payload_is_rate_limit(self):
        route = {
            "id": "primary",
            "source_type": "relay",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        kind = sub2cli_inject.classify_upstream_status(
            502,
            b'{"error":{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}',
            "application/json",
            route=route,
        )

        self.assertEqual("rate_limit", kind)

    def test_relay_top_level_wrapped_429_payload_is_rate_limit(self):
        route = {
            "id": "primary",
            "source_type": "relay",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }
        kind = sub2cli_inject.classify_upstream_status(
            502,
            b'{"message":"exceeded retry limit, last status: 429 Too Many Requests"}',
            "application/json",
            route=route,
        )

        self.assertEqual("rate_limit", kind)

    def test_relay_success_error_wrapped_429_payload_is_rate_limit(self):
        route = {
            "id": "primary",
            "source_type": "relay",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject.raise_for_upstream_success_error(
                route,
                b'{"error":{"message":"exceeded retry limit, last status: 429 Too Many Requests"}}',
                "application/json",
            )

        self.assertEqual("rate_limit", raised.exception.kind)
        self.assertEqual(429, raised.exception.status)

    def test_relay_success_top_level_429_payload_is_rate_limit(self):
        route = {
            "id": "primary",
            "source_type": "relay",
            "base_url": "https://primary.example",
            "api_key": "sk-primary",
            "protocol": "responses",
        }

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject.raise_for_upstream_success_error(
                route,
                b'{"message":"exceeded retry limit, last status: 429 Too Many Requests"}',
                "application/json",
            )

        self.assertEqual("rate_limit", raised.exception.kind)
        self.assertEqual(429, raised.exception.status)

    def test_response_failed_sse_top_level_429_payload_is_rate_limit(self):
        raw = (
            'event: error\n'
            'data: {"message":"exceeded retry limit, last status: 429 Too Many Requests","type":"error"}\n\n'
            'data: [DONE]\n\n'
        ).encode("utf-8")
        message = sub2cli_inject.detect_upstream_rate_limit_error(raw, "text/event-stream")
        self.assertEqual("exceeded retry limit, last status: 429 Too Many Requests", message)

    def test_pool_fails_over_after_relay_source_rate_limit(self):
        cfg = pool_cfg()
        cfg["routes"][0]["source_type"] = "relay"
        cfg["routes"][1]["source_type"] = "relay"
        calls = []
        original_request = sub2cli_inject.request_route_response_with_openai_retries
        original_runtime = sub2cli_inject.ROUTE_POOL_RUNTIME

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, *, timeout):
            calls.append(route["id"])
            if route["id"] == "primary":
                raise sub2cli_inject.UpstreamRouteError(
                    route=route,
                    status=429,
                    body=b'{"error":{"message":"Too Many Requests"}}',
                    content_type="application/json",
                    kind="rate_limit",
                    detail="HTTP 429 Too Many Requests",
                )
            return b'{"id":"resp_ok","object":"response","status":"completed","output":[]}', "application/json"

        sub2cli_inject.request_route_response_with_openai_retries = fake_request
        sub2cli_inject.ROUTE_POOL_RUNTIME = sub2cli_inject.RoutePoolRuntime()
        try:
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi"},
            )
        finally:
            sub2cli_inject.request_route_response_with_openai_retries = original_request
            sub2cli_inject.ROUTE_POOL_RUNTIME = original_runtime

        self.assertEqual(["primary", "fallback"], calls)
        self.assertEqual(1, len(handler.sent))
        self.assertEqual(200, handler.sent[0][0])
        self.assertIn(b"resp_ok", handler.sent[0][1])

    def test_pool_fails_over_when_route_rejects_verified_service_tier(self):
        cfg = pool_cfg()
        for route in cfg["routes"]:
            route["source_type"] = "relay"
            route["supports_service_tier"] = True
        calls = []

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, *, timeout):
            calls.append(route["id"])
            if route["id"] == "primary":
                raise sub2cli_inject.UpstreamRouteError(
                    route=route,
                    status=400,
                    body=b'{"error":{"message":"Unsupported parameter: service_tier"}}',
                    content_type="application/json",
                    kind="unsupported_capability",
                    detail="HTTP 400 Unsupported parameter: service_tier",
                )
            return b'{"id":"resp_ok","status":"completed","output":[]}', "application/json"

        runtime = sub2cli_inject.RoutePoolRuntime()
        with patch.object(
            sub2cli_inject,
            "request_route_response_with_openai_retries",
            side_effect=fake_request,
        ), patch.object(sub2cli_inject, "ROUTE_POOL_RUNTIME", runtime):
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi", "service_tier": "priority"},
            )

        self.assertEqual(["primary", "fallback"], calls)
        self.assertEqual(200, handler.sent[0][0])
        self.assertEqual(0, runtime._state("primary")["failures"])

    def test_pool_fails_over_after_relay_source_blocked(self):
        cfg = pool_cfg()
        cfg["routes"][0]["source_type"] = "relay"
        cfg["routes"][1]["source_type"] = "relay"
        calls = []
        original_request = sub2cli_inject.request_route_response_with_openai_retries
        original_runtime = sub2cli_inject.ROUTE_POOL_RUNTIME

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, *, timeout):
            calls.append(route["id"])
            if route["id"] == "primary":
                raise sub2cli_inject.UpstreamRouteError(
                    route=route,
                    status=403,
                    body=b'{"message":"subscription daily quota exhausted"}',
                    content_type="application/json",
                    kind="blocked",
                    detail="subscription daily quota exhausted",
                )
            return b'{"id":"resp_ok","object":"response","status":"completed","output":[]}', "application/json"

        sub2cli_inject.request_route_response_with_openai_retries = fake_request
        sub2cli_inject.ROUTE_POOL_RUNTIME = sub2cli_inject.RoutePoolRuntime()
        try:
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi"},
            )
        finally:
            sub2cli_inject.request_route_response_with_openai_retries = original_request
            sub2cli_inject.ROUTE_POOL_RUNTIME = original_runtime

        self.assertEqual(["primary", "fallback"], calls)
        self.assertEqual(1, len(handler.sent))
        self.assertEqual(200, handler.sent[0][0])
        self.assertIn(b"resp_ok", handler.sent[0][1])

    def test_pool_fails_over_after_retryable_route_exhausts_short_retries(self):
        cfg = pool_cfg()
        cfg["routes"][0]["source_type"] = "relay"
        cfg["routes"][1]["source_type"] = "relay"
        calls = []
        original_request = sub2cli_inject.request_route_response
        original_delays = sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS
        original_runtime = sub2cli_inject.ROUTE_POOL_RUNTIME

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, *, timeout):
            calls.append(route["id"])
            if route["id"] == "primary":
                raise sub2cli_inject.UpstreamRouteError(
                    route=route,
                    status=None,
                    body=b"",
                    content_type="application/json",
                    kind="retryable",
                    detail="TimeoutError: timed out",
                )
            return b'{"id":"resp_ok","object":"response","status":"completed","output":[]}', "application/json"

        sub2cli_inject.request_route_response = fake_request
        sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS = (0,)
        sub2cli_inject.ROUTE_POOL_RUNTIME = sub2cli_inject.RoutePoolRuntime()
        try:
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi"},
            )
        finally:
            sub2cli_inject.request_route_response = original_request
            sub2cli_inject.ROUTE_TRANSIENT_RETRY_DELAYS = original_delays
            sub2cli_inject.ROUTE_POOL_RUNTIME = original_runtime

        self.assertEqual(["primary", "primary", "fallback"], calls)
        self.assertEqual(1, len(handler.sent))
        self.assertEqual(200, handler.sent[0][0])
        self.assertIn(b"resp_ok", handler.sent[0][1])

    def test_relay_wrapped_network_error_is_retryable(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example",
            "api_key": "sk-relay",
            "protocol": "responses",
        }

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject.raise_for_upstream_success_error(
                route,
                b'{"error":{"message":"network error: upstream reset"}}',
                "application/json",
            )

        self.assertEqual("retryable", raised.exception.kind)

    def test_pool_does_not_fail_over_after_relay_source_request_error(self):
        cfg = pool_cfg()
        cfg["routes"][0]["source_type"] = "relay"
        cfg["routes"][1]["source_type"] = "relay"
        calls = []
        original_request = sub2cli_inject.request_route_response_with_openai_retries
        original_runtime = sub2cli_inject.ROUTE_POOL_RUNTIME

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, *, timeout):
            calls.append(route["id"])
            if route["id"] == "primary":
                error_body = b'{"error":{"message":"invalid request: unsupported parameter"}}'
                raise sub2cli_inject.UpstreamRouteError(
                    route=route,
                    status=400,
                    body=error_body,
                    content_type="application/json",
                    kind=sub2cli_inject.classify_upstream_status(
                        400,
                        error_body,
                        route=route,
                    ),
                    detail="HTTP 400: invalid request: unsupported parameter",
                )
            return b'{"id":"resp_ok","object":"response","status":"completed","output":[]}', "application/json"

        sub2cli_inject.request_route_response_with_openai_retries = fake_request
        sub2cli_inject.ROUTE_POOL_RUNTIME = sub2cli_inject.RoutePoolRuntime()
        try:
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi"},
            )
        finally:
            sub2cli_inject.request_route_response_with_openai_retries = original_request
            sub2cli_inject.ROUTE_POOL_RUNTIME = original_runtime

        self.assertEqual(["primary"], calls)
        self.assertEqual(1, len(handler.sent))
        self.assertEqual(400, handler.sent[0][0])
        self.assertIn(b"invalid request", handler.sent[0][1])

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

        log_path.write_text("old\n", encoding="utf-8")
        os.chmod(log_path, 0o644)
        sub2cli_inject.proxy_log(
            "pool route failure Bearer sk-secret-example-123456789"
        )
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("pool route failure", text)
        self.assertNotIn("sk-secret-example-123456789", text)
        self.assertEqual(0o600, log_path.stat().st_mode & 0o777)

    def test_proxy_get_endpoints_require_matching_local_bearer(self):
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "single",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream",
            "model": "gpt-5.6-sol",
        }
        models_body = b'{"object":"list","data":[]}'
        server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg), \
                 patch.object(
                     sub2cli_inject,
                     "fetch_route_models",
                     return_value=(models_body, "application/json"),
                 ) as fetch_models:
                for path in ("/poolz", "/v1/models"):
                    for header in (None, "Bearer wrong"):
                        headers = {"Authorization": header} if header else {}
                        request = urlrequest.Request(base_url + path, headers=headers)
                        with self.assertRaises(urlerror.HTTPError) as raised:
                            urlrequest.urlopen(request, timeout=3)
                        self.assertEqual(401, raised.exception.code)

                    request = urlrequest.Request(
                        base_url + path,
                        headers={"Authorization": "Bearer local-secret"},
                    )
                    with urlrequest.urlopen(request, timeout=3) as response:
                        self.assertEqual(200, response.status)
                fetch_models.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_proxy_post_requires_matching_local_bearer(self):
        calls = []
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "single",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream",
            "model": "gpt-5.6-sol",
        }

        def fake_request(route, body, *, timeout):
            calls.append((route, body, timeout))
            return b'{"id":"ok","status":"completed","output":[]}', "application/json"

        server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        body = json.dumps({"model": "gpt-5.6-sol", "input": "hi"}).encode("utf-8")
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg), \
                 patch.object(sub2cli_inject, "request_route_response_with_openai_retries", side_effect=fake_request):
                for header in (None, "Bearer wrong"):
                    headers = {"Content-Type": "application/json"}
                    if header:
                        headers["Authorization"] = header
                    with self.assertRaises(urlerror.HTTPError) as raised:
                        urlrequest.urlopen(urlrequest.Request(url, data=body, headers=headers), timeout=3)
                    self.assertEqual(401, raised.exception.code)
                req = urlrequest.Request(
                    url,
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer local-secret",
                    },
                )
                with urlrequest.urlopen(req, timeout=3) as response:
                    self.assertEqual(200, response.status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        self.assertEqual(1, len(calls))

    def test_proxy_decodes_zstd_request_body_before_routing(self):
        if sub2cli_inject._stdlib_zstd is not None:
            compress = sub2cli_inject._stdlib_zstd.compress
        elif sub2cli_inject._third_party_zstd is not None:
            compress = sub2cli_inject._third_party_zstd.ZstdCompressor().compress
        else:
            self.skipTest("zstd decoder is not installed")

        calls = []
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "single",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream",
            "model": "gpt-5.6-sol",
        }

        def fake_request(route, body, *, timeout):
            calls.append((route, body, timeout))
            return b'{"id":"ok","status":"completed","output":[]}', "application/json"

        server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
        decoded = {
            "model": "gpt-5.6-sol",
            "input": "x" * 96_000,
            "stream": False,
        }
        encoded = compress(json.dumps(decoded).encode("utf-8"))
        request = urlrequest.Request(
            url,
            data=encoded,
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "zstd",
                "Authorization": "Bearer local-secret",
            },
        )
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg), \
                 patch.object(sub2cli_inject, "request_route_response_with_openai_retries", side_effect=fake_request):
                with urlrequest.urlopen(request, timeout=3) as response:
                    self.assertEqual(200, response.status)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(1, len(calls))
        self.assertEqual(decoded, calls[0][1])

    def test_proxy_rejects_unsupported_request_content_encoding(self):
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "single",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream",
            "model": "gpt-5.6-sol",
        }
        server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        request = urlrequest.Request(
            f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
            data=b"not-brotli",
            headers={
                "Content-Type": "application/json",
                "Content-Encoding": "br",
                "Authorization": "Bearer local-secret",
            },
        )
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg), \
                 patch.object(sub2cli_inject, "request_route_response_with_openai_retries") as upstream:
                with self.assertRaises(urlerror.HTTPError) as raised:
                    urlrequest.urlopen(request, timeout=3)
                self.assertEqual(415, raised.exception.code)
                payload = json.loads(raised.exception.read().decode("utf-8"))
                self.assertEqual("unsupported_content_encoding", payload["error"]["type"])
                upstream.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_zstd_decoder_rejects_body_above_decoded_limit(self):
        if sub2cli_inject._stdlib_zstd is not None:
            compress = sub2cli_inject._stdlib_zstd.compress
        elif sub2cli_inject._third_party_zstd is not None:
            compress = sub2cli_inject._third_party_zstd.ZstdCompressor().compress
        else:
            self.skipTest("zstd decoder is not installed")

        encoded = compress(b"x" * 4096)
        with patch.object(sub2cli_inject, "PROXY_MAX_DECODED_REQUEST_BYTES", 1024):
            with self.assertRaises(sub2cli_inject.ProxyRequestError) as raised:
                sub2cli_inject.decode_proxy_request_body(encoded, "zstd")
        self.assertEqual(413, raised.exception.status)
        self.assertEqual("request_too_large", raised.exception.error_type)

    def test_third_party_zstd_fallback_rejects_invalid_and_oversized_bodies(self):
        zstd = sub2cli_inject._third_party_zstd
        if zstd is None:
            self.skipTest("third-party zstd decoder is not installed")

        raw = b"x" * 4096
        known_size = zstd.ZstdCompressor().compress(raw)
        unknown_size = zstd.ZstdCompressor(write_content_size=False).compress(raw)
        with patch.object(sub2cli_inject, "_stdlib_zstd", None):
            self.assertEqual(
                raw,
                sub2cli_inject.decode_proxy_request_body(known_size, "zstd"),
            )
            self.assertEqual(
                raw,
                sub2cli_inject.decode_proxy_request_body(unknown_size, "zstd"),
            )
            for invalid in (
                known_size[:-1],
                unknown_size[:-1],
                known_size + b"trailing",
            ):
                with self.assertRaises(sub2cli_inject.ProxyRequestError) as raised:
                    sub2cli_inject.decode_proxy_request_body(invalid, "zstd")
                self.assertEqual(400, raised.exception.status)
            with patch.object(sub2cli_inject, "PROXY_MAX_DECODED_REQUEST_BYTES", 1024):
                for encoded in (known_size, unknown_size):
                    with self.assertRaises(sub2cli_inject.ProxyRequestError) as raised:
                        sub2cli_inject.decode_proxy_request_body(encoded, "zstd")
                    self.assertEqual(413, raised.exception.status)

    def test_zstd_decoder_accepts_concatenated_frames(self):
        if sub2cli_inject._stdlib_zstd is not None:
            compress = sub2cli_inject._stdlib_zstd.compress
        elif sub2cli_inject._third_party_zstd is not None:
            compress = sub2cli_inject._third_party_zstd.ZstdCompressor().compress
        else:
            self.skipTest("zstd decoder is not installed")

        encoded = compress(b'{"model":"gpt-5.6-sol",') + compress(b'"input":"hi"}')
        expected = b'{"model":"gpt-5.6-sol","input":"hi"}'
        self.assertEqual(
            expected,
            sub2cli_inject.decode_proxy_request_body(encoded, "zstd"),
        )
        if sub2cli_inject._third_party_zstd is not None:
            with patch.object(sub2cli_inject, "_stdlib_zstd", None):
                self.assertEqual(
                    expected,
                    sub2cli_inject.decode_proxy_request_body(encoded, "zstd"),
                )

    def test_stdlib_zstd_decoder_handles_many_small_frames(self):
        zstd = sub2cli_inject._stdlib_zstd
        if zstd is None:
            self.skipTest("stdlib zstd decoder is not installed")

        encoded = zstd.compress(b"") * 5000 + zstd.compress(b'{"ok":true}')
        self.assertEqual(
            b'{"ok":true}',
            sub2cli_inject.decode_proxy_request_body(encoded, "zstd"),
        )

    def test_proxy_streams_native_responses_before_upstream_completes(self):
        _home, codex_home, _app_support = self.with_isolated_codex_home()
        first_chunk = (
            b'event: response.created\n'
            b'data: {"type":"response.created","response":{"id":"resp_streaming"}}\n\n'
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
        )
        final_chunk = b'event: response.completed\ndata: {"status":"completed"}\n\n'
        upstream_started = threading.Event()
        allow_completion = threading.Event()
        upstream_completed = threading.Event()
        upstream_secret = "sk-upstream-stream-secret-123456789"
        seen = {}

        class UpstreamHandler(sub2cli_inject.BaseHTTPRequestHandler):
            def log_message(self, _fmt, *_args):
                return

            def do_POST(self):
                seen["path"] = self.path
                seen["authorization"] = self.headers.get("Authorization")
                length = int(self.headers.get("Content-Length") or "0")
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Upstream-Secret", upstream_secret)
                self.end_headers()
                self.wfile.write(first_chunk)
                self.wfile.flush()
                upstream_started.set()
                allow_completion.wait(2)
                self.wfile.write(final_chunk)
                self.wfile.flush()
                upstream_completed.set()

        upstream_server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            UpstreamHandler,
        )
        upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
        upstream_thread.start()
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "streaming upstream",
            "base_url": f"http://127.0.0.1:{upstream_server.server_address[1]}/v1",
            "api_key": upstream_secret,
            "model": "gpt-5.6-sol",
        }
        proxy_server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        proxy_thread.start()
        request_body = json.dumps({
            "model": "gpt-5.6-sol",
            "input": "hi",
            "stream": True,
        }).encode("utf-8")
        request = urlrequest.Request(
            f"http://127.0.0.1:{proxy_server.server_address[1]}/v1/responses",
            data=request_body,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": "Bearer local-secret",
            },
        )
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg):
                with urlrequest.urlopen(request, timeout=5) as response:
                    self.assertTrue(upstream_started.wait(1))
                    self.assertEqual("text/event-stream", response.headers.get("Content-Type"))
                    self.assertIsNone(response.headers.get("X-Upstream-Secret"))
                    self.assertFalse(upstream_completed.is_set(), "proxy buffered until upstream completion")
                    self.assertEqual(first_chunk, response.read(len(first_chunk)))
                    self.assertFalse(upstream_completed.is_set(), "proxy buffered the first SSE chunk")
                    allow_completion.set()
                    tail = response.read()
                    self.assertTrue(tail.startswith(final_chunk))
                    self.assertIn(b"event: response.completed", tail)
                    self.assertIn(b'"type":"response.completed"', tail)
        finally:
            allow_completion.set()
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=3)
            upstream_server.shutdown()
            upstream_server.server_close()
            upstream_thread.join(timeout=3)

        self.assertTrue(upstream_completed.is_set())
        self.assertEqual("/v1/responses", seen["path"])
        self.assertEqual(f"Bearer {upstream_secret}", seen["authorization"])
        log_text = (
            (codex_home / "sub2cli-responses-proxy.log").read_text(encoding="utf-8")
            if (codex_home / "sub2cli-responses-proxy.log").exists()
            else ""
        )
        self.assertNotIn(upstream_secret, log_text)

    def test_proxy_preserves_native_responses_and_compact_paths(self):
        paths = []
        response_body = b'{"id":"ok","status":"completed","output":[]}'

        class UpstreamHandler(sub2cli_inject.BaseHTTPRequestHandler):
            def log_message(self, _fmt, *_args):
                return

            def do_POST(self):
                paths.append(self.path)
                length = int(self.headers.get("Content-Length") or "0")
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

        upstream_server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            UpstreamHandler,
        )
        upstream_thread = threading.Thread(target=upstream_server.serve_forever, daemon=True)
        upstream_thread.start()
        cfg = {
            "mode": "relay",
            "protocol": "responses",
            "display_name": "native upstream",
            "base_url": f"http://127.0.0.1:{upstream_server.server_address[1]}/v1",
            "api_key": "sk-upstream-path-secret-123456789",
            "model": "gpt-5.6-sol",
        }
        proxy_server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        proxy_thread = threading.Thread(target=proxy_server.serve_forever, daemon=True)
        proxy_thread.start()
        request_body = json.dumps({"model": "gpt-5.6-sol", "input": "hi"}).encode("utf-8")
        try:
            with patch.object(sub2cli_inject, "expected_proxy_bearer_token", return_value="local-secret"), \
                 patch.object(sub2cli_inject, "_active_proxy_slot", return_value=cfg):
                for path in ("/v1/responses", "/v1/responses/compact"):
                    request = urlrequest.Request(
                        f"http://127.0.0.1:{proxy_server.server_address[1]}{path}",
                        data=request_body,
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": "Bearer local-secret",
                        },
                    )
                    with urlrequest.urlopen(request, timeout=3) as response:
                        self.assertEqual(response_body, response.read())
        finally:
            proxy_server.shutdown()
            proxy_server.server_close()
            proxy_thread.join(timeout=3)
            upstream_server.shutdown()
            upstream_server.server_close()
            upstream_thread.join(timeout=3)

        self.assertEqual(["/v1/responses", "/v1/responses/compact"], paths)

    def test_native_stream_buffers_json_success_error_for_classification(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream-secret-123456789",
            "protocol": "responses",
        }

        class FakeResponse:
            headers = {"Content-Type": "application/json"}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return b'{"error":{"message":"insufficient balance"}}'

        with patch.object(sub2cli_inject, "fresh_urlopen", return_value=FakeResponse()):
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_stream_with_openai_retries(
                    route,
                    {"model": "gpt-5.6-sol", "input": "hi", "stream": True},
                    timeout=3,
                )

        self.assertEqual("blocked", raised.exception.kind)
        self.assertNotIn(route["api_key"], raised.exception.detail)

    def test_native_stream_buffers_non_2xx_for_classification(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream-secret-123456789",
            "protocol": "responses",
        }
        error_body = b'{"error":{"message":"insufficient balance"}}'

        def fake_urlopen(req, *, timeout):
            raise urlerror.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                {"Content-Type": "application/json"},
                io.BytesIO(error_body),
            )

        with patch.object(sub2cli_inject, "fresh_urlopen", side_effect=fake_urlopen):
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_stream_with_openai_retries(
                    route,
                    {"model": "gpt-5.6-sol", "input": "hi", "stream": True},
                    timeout=3,
                )

        self.assertEqual("blocked", raised.exception.kind)
        self.assertEqual(403, raised.exception.status)
        self.assertEqual(error_body, raised.exception.body)
        self.assertNotIn(route["api_key"], raised.exception.detail)

    def test_native_stream_classifies_first_failed_sse_before_commit(self):
        route = {
            "id": "relay-primary",
            "source_type": "relay",
            "base_url": "https://relay.example/v1",
            "api_key": "sk-upstream-secret-123456789",
            "protocol": "responses",
        }
        raw = (
            'event: response.failed\n'
            'data: {"response":{"status":"failed","error":{"message":"insufficient balance"}}}\n\n'
        ).encode("utf-8")

        class FakeResponse(io.BytesIO):
            headers = {"Content-Type": "text/event-stream"}

        response = FakeResponse(raw)
        with patch.object(sub2cli_inject, "fresh_urlopen", return_value=response):
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_stream_with_openai_retries(
                    route,
                    {"model": "gpt-5.6-sol", "input": "hi", "stream": True},
                    timeout=3,
                )

        self.assertEqual("blocked", raised.exception.kind)
        self.assertTrue(response.closed)

    def test_native_stream_prelude_read_failure_is_retryable(self):
        route = {"id": "primary", "source_type": "relay", "api_key": "sk-test"}

        class TimeoutStream:
            def readline(self, _limit):
                raise TimeoutError("upstream stalled")

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject._prime_native_response_stream(
                route,
                TimeoutStream(),
                "text/event-stream",
            )

        self.assertEqual("retryable", raised.exception.kind)
        self.assertNotIn("upstream stalled", raised.exception.detail)

    def test_native_stream_control_only_eof_is_retryable(self):
        route = {"id": "primary", "source_type": "relay", "api_key": "sk-test"}
        stream = io.BytesIO(
            b'event: response.created\ndata: {"response":{"status":"in_progress"}}\n\n'
        )

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject._prime_native_response_stream(
                route,
                stream,
                "text/event-stream",
            )

        self.assertEqual("retryable", raised.exception.kind)

    def test_native_stream_large_control_events_before_payload_are_ok(self):
        """Codex echoes large instructions inside response.created/in_progress.

        A 64 KiB shared prelude budget truncates those control frames and false
        -positives a healthy stream as 502. Per-event priming must survive.
        """
        route = {"id": "primary", "source_type": "relay", "api_key": "sk-test"}
        huge = "x" * 100_000
        created = (
            "event: response.created\n"
            f'data: {{"type":"response.created","response":{{"status":"in_progress","instructions":"{huge}"}}}}\n\n'
        ).encode("utf-8")
        progress = (
            "event: response.in_progress\n"
            f'data: {{"type":"response.in_progress","response":{{"status":"in_progress","instructions":"{huge}"}}}}\n\n'
        ).encode("utf-8")
        payload = b'event: response.output_text.delta\ndata: {"delta":"ok"}\n\n'
        self.assertGreater(len(created) + len(progress), 64 * 1024)

        prefix, eof = sub2cli_inject._prime_native_response_stream(
            route,
            io.BytesIO(created + progress + payload),
            "text/event-stream",
        )

        self.assertIn(b"event: response.output_text.delta", prefix)
        self.assertIn(b'"delta":"ok"', prefix)
        self.assertFalse(eof)

    def test_streaming_pool_fails_over_after_service_tier_sse_rejection(self):
        cfg = pool_cfg()
        for route in cfg["routes"]:
            route["source_type"] = "relay"
            route["supports_service_tier"] = True
        opened = []
        unsupported = (
            'event: response.failed\n'
            'data: {"response":{"status":"failed","error":{"message":"Unsupported parameter: service_tier"}}}\n\n'
        ).encode("utf-8")
        success = b'event: response.output_text.delta\ndata: {"delta":"ok"}\n\n'

        class FakeResponse(io.BytesIO):
            headers = {"Content-Type": "text/event-stream"}

        def fake_open(route, body, **_kwargs):
            opened.append(route["id"])
            raw = unsupported if route["id"] == "primary" else success
            return FakeResponse(raw), "text/event-stream"

        class FakeHandler:
            def __init__(self):
                self.result = None
                self.sent = []

            def _send_route_stream(self, result, **_kwargs):
                self.result = result

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        runtime = sub2cli_inject.RoutePoolRuntime()
        with patch.object(runtime, "refresh_route_models", return_value=[]), \
             patch.object(sub2cli_inject, "ROUTE_POOL_RUNTIME", runtime), \
             patch.object(sub2cli_inject, "_open_native_responses_response", side_effect=fake_open):
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {
                    "model": "gpt-test",
                    "input": "hi",
                    "stream": True,
                    "service_tier": "priority",
                },
            )

        self.assertEqual(["primary", "fallback"], opened)
        self.assertIsNotNone(handler.result)
        self.assertEqual(success, handler.result.body)
        self.assertEqual(0, runtime._state("primary")["failures"])

    def test_model_not_found_sse_is_retryable_before_stream_commit(self):
        route = {"id": "primary", "source_type": "relay", "api_key": "sk-test"}
        stream = io.BytesIO(
            b'event: response.failed\n'
            b'data: {"response":{"status":"failed","error":{"message":"Model gpt-test not found"}}}\n\n'
        )

        with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
            sub2cli_inject._prime_native_response_stream(
                route,
                stream,
                "text/event-stream",
            )

        self.assertEqual("retryable", raised.exception.kind)
        self.assertEqual(404, raised.exception.status)

    def test_midstream_upstream_reset_records_pool_route_failure(self):
        cfg = pool_cfg()
        route = cfg["routes"][0]
        route["source_type"] = "relay"

        class ResetStream:
            def read1(self, _size):
                raise ConnectionResetError("upstream reset")

            def close(self):
                return None

        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.close_connection = False

            def send_response(self, _status):
                return None

            def send_header(self, _name, _value):
                return None

            def end_headers(self):
                return None

        runtime = sub2cli_inject.RoutePoolRuntime()
        result = sub2cli_inject.RouteStreamResponse(
            content_type="text/event-stream",
            body=b'event: response.output_text.delta\ndata: {"delta":"first"}\n\n',
            stream=ResetStream(),
        )
        with patch.object(sub2cli_inject, "ROUTE_POOL_RUNTIME", runtime):
            for _ in range(2):
                sub2cli_inject.ResponsesProxyHandler._send_route_stream(
                    FakeHandler(),
                    result,
                    route=route,
                    cfg=cfg,
                )

        state = runtime._state("primary")
        self.assertEqual(2, state["failures"])
        self.assertGreater(state["open_until"], 0)
        self.assertIn("ConnectionResetError", state["last_error"])

    def test_split_terminal_event_then_reset_counts_as_complete(self):
        cfg = pool_cfg()
        route = cfg["routes"][0]
        route["source_type"] = "relay"

        class SplitTerminalStream:
            def __init__(self):
                self.chunks = iter([
                    b"event: response.comple",
                    b'ted\ndata: {"type":"response.completed","response":{"id":"resp_split","status":"completed"}}\n\n',
                ])

            def read1(self, _size):
                try:
                    return next(self.chunks)
                except StopIteration as exc:
                    raise ConnectionResetError("closed after terminal") from exc

            def close(self):
                return None

        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.close_connection = False

            def send_response(self, _status):
                return None

            def send_header(self, _name, _value):
                return None

            def end_headers(self):
                return None

        runtime = sub2cli_inject.RoutePoolRuntime()
        result = sub2cli_inject.RouteStreamResponse(
            content_type="text/event-stream",
            body=b'event: response.output_text.delta\ndata: {"delta":"first"}\n\n',
            stream=SplitTerminalStream(),
        )
        handler = FakeHandler()
        with patch.object(sub2cli_inject, "ROUTE_POOL_RUNTIME", runtime):
            complete = sub2cli_inject.ResponsesProxyHandler._send_route_stream(
                handler,
                result,
                route=route,
                cfg=cfg,
            )

        self.assertTrue(complete)
        self.assertEqual(1, handler.wfile.getvalue().count(b"event: response.completed"))
        self.assertEqual(0, runtime._state("primary")["failures"])

    def test_native_stream_done_marker_is_completed_for_codex(self):
        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()

            def _send(self, _status, body, _content_type):
                self.wfile.write(body)

            def send_response(self, _status):
                return None

            def send_header(self, _name, _value):
                return None

            def end_headers(self):
                return None

        raw = (
            b'event: response.created\n'
            b'data: {"type":"response.created","response":{"id":"resp_done"}}\n\n'
            b'event: response.output_text.delta\n'
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
            b'data: [DONE]\n\n'
        )
        handler = FakeHandler()
        complete = sub2cli_inject.ResponsesProxyHandler._send_route_stream(
            handler,
            sub2cli_inject.RouteStreamResponse(
                content_type="text/event-stream",
                body=raw,
            ),
        )

        sent = handler.wfile.getvalue()
        self.assertTrue(complete)
        self.assertIn(b"event: response.completed", sent)
        self.assertIn(b'"type":"response.completed"', sent)
        self.assertIn(b'"id":"resp_done"', sent)

    def test_streamed_done_marker_is_completed_for_codex(self):
        class DoneStream:
            def __init__(self):
                self.chunks = iter([b"data: [DONE]\n\n", b""])

            def read1(self, _size):
                return next(self.chunks)

            def close(self):
                return None

        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.close_connection = False

            def send_response(self, _status):
                return None

            def send_header(self, _name, _value):
                return None

            def end_headers(self):
                return None

        handler = FakeHandler()
        complete = sub2cli_inject.ResponsesProxyHandler._send_route_stream(
            handler,
            sub2cli_inject.RouteStreamResponse(
                content_type="text/event-stream",
                body=(
                    b'event: response.created\n'
                    b'data: {"type":"response.created","response":{"id":"resp_streamed_done"}}\n\n'
                    b'event: response.output_text.delta\n'
                    b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                ),
                stream=DoneStream(),
            ),
        )

        sent = handler.wfile.getvalue()
        self.assertTrue(complete)
        self.assertEqual(1, sent.count(b"event: response.completed"))
        self.assertIn(b'"id":"resp_streamed_done"', sent)

    def test_compact_rejects_chat_conversion_without_calling_upstream(self):
        route = {
            "id": "chat-only",
            "base_url": "https://chat.example/v1",
            "api_key": "sk-chat",
            "protocol": "chat",
        }
        with patch.object(sub2cli_inject, "fresh_urlopen") as upstream:
            with self.assertRaises(sub2cli_inject.UpstreamRouteError) as raised:
                sub2cli_inject.request_route_response(
                    route,
                    {"model": "gpt-test", "input": "hi"},
                    timeout=3,
                    compact=True,
                )

        self.assertEqual(501, raised.exception.status)
        upstream.assert_not_called()

    def test_compact_pool_skips_chat_route_for_native_fallback(self):
        cfg = pool_cfg()
        cfg["routes"][0]["protocol"] = "chat"
        calls = []

        class FakeHandler:
            def __init__(self):
                self.sent = []

            def _send(self, status, body, content_type):
                self.sent.append((status, body, content_type))

            def _send_json(self, status, payload):
                self.sent.append((status, json.dumps(payload).encode("utf-8"), "application/json"))

        def fake_request(route, body, **kwargs):
            calls.append((route["id"], kwargs))
            return b'{"id":"compact_ok","status":"completed"}', "application/json"

        runtime = sub2cli_inject.RoutePoolRuntime()
        with patch.object(
            sub2cli_inject,
            "request_route_response_with_openai_retries",
            side_effect=fake_request,
        ), patch.object(sub2cli_inject, "ROUTE_POOL_RUNTIME", runtime):
            handler = FakeHandler()
            sub2cli_inject.ResponsesProxyHandler._send_pool_response(
                handler,
                cfg,
                {"model": "gpt-test", "input": "hi"},
                compact=True,
            )

        self.assertEqual(["fallback"], [route_id for route_id, _kwargs in calls])
        self.assertTrue(calls[0][1]["compact"])
        self.assertEqual(200, handler.sent[0][0])

    def test_proxy_health_advertises_compatible_schema(self):
        server = sub2cli_inject.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            sub2cli_inject.ResponsesProxyHandler,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urlrequest.urlopen(
                f"http://127.0.0.1:{server.server_address[1]}/healthz",
                timeout=3,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(sub2cli_inject.PROXY_SCHEMA_VERSION, payload["proxy_schema"])
                self.assertTrue(payload["auth_required"])
                self.assertEqual(os.getpid(), payload["pid"])
                self.assertEqual(server.server_address[1], payload["port"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_proxy_health_rejects_same_schema_from_foreign_instance(self):
        class FakeHealthResponse:
            status = 200
            headers = {"Server": "sub2cli-responses-proxy/0.2"}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, *_args):
                return json.dumps({
                    "proxy_schema": sub2cli_inject.PROXY_SCHEMA_VERSION,
                    "auth_required": True,
                    "instance_id": "other-instance",
                }).encode("utf-8")

        with patch.object(sub2cli_inject, "fresh_urlopen", return_value=FakeHealthResponse()), \
             patch.object(sub2cli_inject, "proxy_instance_fingerprint", return_value="this-instance"):
            health = sub2cli_inject.protocol_proxy_health(18765)

        self.assertTrue(health["recognized"])
        self.assertFalse(health["owned"])
        self.assertFalse(health["compatible"])

    def test_restart_proxy_refuses_to_stop_foreign_instance(self):
        foreign = {
            "reachable": True,
            "recognized": True,
            "owned": False,
            "compatible": False,
            "payload": {"instance_id": "other-instance"},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_health", return_value=foreign), \
             patch.object(sub2cli_inject, "stop_protocol_proxy") as stop, \
             patch.object(sub2cli_inject, "ensure_protocol_proxy") as ensure:
            with self.assertRaises(SystemExit):
                sub2cli_inject.cmd_restart_proxy(18765)

        stop.assert_not_called()
        ensure.assert_not_called()

    def test_ensure_proxy_refuses_recognized_legacy_instance_without_ownership(self):
        legacy = {
            "reachable": True,
            "recognized": True,
            "owned": False,
            "compatible": False,
            "payload": {"proxy_schema": 2, "auth_required": True},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_state", return_value=(legacy, None)), \
             patch.object(sub2cli_inject, "stop_protocol_proxy") as stop, \
             patch.object(sub2cli_inject.subprocess, "Popen") as popen:
            started = sub2cli_inject.ensure_protocol_proxy(18765)

        self.assertFalse(started)
        stop.assert_not_called()
        popen.assert_not_called()

    def test_ensure_proxy_allows_cold_onefile_start_longer_than_twelve_seconds(self):
        missing = {
            "reachable": False,
            "recognized": False,
            "owned": False,
            "compatible": False,
            "payload": {},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_state", return_value=(missing, None)), \
             patch.object(sub2cli_inject, "protocol_proxy_pids", return_value=[]), \
             patch.object(sub2cli_inject, "open_secure_proxy_log", unittest.mock.mock_open()), \
             patch.object(sub2cli_inject.subprocess, "Popen"), \
             patch.object(sub2cli_inject.time, "monotonic", side_effect=[0.0, 13.0]), \
             patch.object(sub2cli_inject.time, "sleep"), \
             patch.object(sub2cli_inject, "protocol_proxy_healthy", return_value=True):
            self.assertTrue(sub2cli_inject.ensure_protocol_proxy(18765))

    def test_restart_proxy_refuses_recognized_legacy_instance_without_ownership(self):
        legacy = {
            "reachable": True,
            "recognized": True,
            "owned": False,
            "compatible": False,
            "payload": {"proxy_schema": 2, "auth_required": True},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_state", return_value=(legacy, None)), \
             patch.object(sub2cli_inject, "stop_protocol_proxy") as stop, \
             patch.object(sub2cli_inject, "ensure_protocol_proxy") as ensure:
            with self.assertRaises(SystemExit):
                sub2cli_inject.cmd_restart_proxy(18765)

        stop.assert_not_called()
        ensure.assert_not_called()

    def test_legacy_proxy_schema_with_auth_requires_bearer_challenge(self):
        health = {
            "reachable": True,
            "recognized": True,
            "owned": False,
            "compatible": False,
            "payload": {"proxy_schema": 2, "auth_required": True},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_pids", return_value=[123]), \
             patch.object(sub2cli_inject, "_legacy_proxy_pool_pid", return_value=123), \
             patch.object(sub2cli_inject, "_legacy_proxy_process_matches", return_value=True), \
             patch.object(sub2cli_inject, "_legacy_proxy_bearer_challenge", return_value=False):
            self.assertFalse(sub2cli_inject.legacy_protocol_proxy_owned(18765, health))
        with patch.object(sub2cli_inject, "protocol_proxy_pids", return_value=[123]), \
             patch.object(sub2cli_inject, "_legacy_proxy_pool_pid", return_value=123), \
             patch.object(sub2cli_inject, "_legacy_proxy_process_matches", return_value=True), \
             patch.object(sub2cli_inject, "_legacy_proxy_bearer_challenge", return_value=True):
            self.assertTrue(sub2cli_inject.legacy_protocol_proxy_owned(18765, health))

    def test_verified_schema_less_legacy_proxy_is_migrated(self):
        legacy = {
            "reachable": True,
            "recognized": True,
            "owned": False,
            "compatible": False,
            "payload": {"ok": True},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_state", return_value=(legacy, (123, "owned"))), \
             patch.object(sub2cli_inject, "stop_protocol_proxy", return_value=True) as stop, \
             patch.object(sub2cli_inject, "protocol_proxy_healthy", return_value=True), \
             patch.object(sub2cli_inject.subprocess, "Popen") as popen, \
             patch.object(sub2cli_inject, "open_secure_proxy_log", unittest.mock.mock_open()):
            started = sub2cli_inject.ensure_protocol_proxy(18765)

        self.assertTrue(started)
        stop.assert_called_once_with(18765, owned_process=(123, "owned"))
        popen.assert_called_once()

    def test_proxy_state_rejects_pid_reuse_during_health_proof(self):
        health = {
            "reachable": True,
            "recognized": True,
            "owned": True,
            "compatible": True,
            "payload": {"pid": 111, "port": 18765},
        }
        with patch.object(sub2cli_inject, "protocol_proxy_pids", return_value=[111]), \
             patch.object(sub2cli_inject, "protocol_proxy_health", return_value=health), \
             patch.object(
                 sub2cli_inject,
                 "proxy_process_identity",
                 side_effect=["owned-before-proof", "reused-after-proof"],
             ):
            observed_health, proof = sub2cli_inject.protocol_proxy_state(18765)

        self.assertIs(health, observed_health)
        self.assertIsNone(proof)

    def test_proxy_stop_does_not_follow_rebound_port_or_reused_pid(self):
        identities = {111: "owned-start owned-command"}

        def fake_identity(pid):
            return identities.get(pid)

        def fake_kill(pid, sig):
            self.assertEqual(111, pid)
            self.assertEqual(signal.SIGTERM, sig)
            # The old process exits and the PID is immediately reused. A new
            # listener on the same port must never become a kill target.
            identities[111] = "reused-start foreign-command"
            identities[222] = "foreign-listener"

        with patch.object(sub2cli_inject, "process_group_pids_for", return_value=[111]), \
             patch.object(sub2cli_inject, "proxy_process_identity", side_effect=fake_identity), \
             patch.object(sub2cli_inject, "protocol_proxy_pids", side_effect=AssertionError("must not rescan port")), \
             patch.object(sub2cli_inject.os, "kill", side_effect=fake_kill) as kill:
            self.assertTrue(sub2cli_inject.stop_protocol_proxy(
                18765,
                owned_process=(111, "owned-start owned-command"),
                timeout=0.01,
            ))

        kill.assert_called_once_with(111, signal.SIGTERM)

    def test_proxy_stop_rejects_changed_ownership_before_signal(self):
        with patch.object(sub2cli_inject, "proxy_process_identity", return_value="reused-process"), \
             patch.object(sub2cli_inject.os, "kill") as kill:
            self.assertFalse(sub2cli_inject.stop_protocol_proxy(
                18765,
                owned_process=(111, "original-owned-process"),
            ))
        kill.assert_not_called()

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

    def test_bundle_main_match_ignores_embedded_resource_paths(self):
        bundle = Path("/Applications/Codex.app")
        main = "/Applications/Codex.app/Contents/MacOS/Codex"
        self.assertTrue(sub2cli_inject._command_runs_bundle_main(main, bundle))
        self.assertTrue(sub2cli_inject._command_runs_bundle_main(f"{main} --flag", bundle))
        self.assertFalse(sub2cli_inject._command_runs_bundle_main(
            "/usr/bin/python3 /Applications/Codex.app/Contents/Resources/app-server",
            bundle,
        ))
        self.assertFalse(sub2cli_inject._command_runs_bundle_main(
            f"/bin/zsh -c {main}",
            bundle,
        ))

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
