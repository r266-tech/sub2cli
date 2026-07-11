import importlib.machinery
import importlib.util
import base64
import json
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.modules.setdefault("keyring", types.SimpleNamespace(
    set_password=lambda *_args, **_kwargs: None,
    get_password=lambda *_args, **_kwargs: None,
    delete_password=lambda *_args, **_kwargs: None,
))
loader = importlib.machinery.SourceFileLoader("desktop_api", str(ROOT / "desktop" / "api.py"))
spec = importlib.util.spec_from_loader(loader.name, loader)
desktop_api = importlib.util.module_from_spec(spec)
loader.exec_module(desktop_api)


class FakeRelayContext:
    domain = "https://relay.example"

    def __init__(self):
        self.updated = []
        self._keys = [
            {"id": 1, "name": "alpha", "key": "sk-alpha", "group_id": 10, "group": {"id": 10, "name": "g10", "rate_multiplier": 0.1}},
        ]

    def fetch_keys(self):
        return self._keys

    def fetch_groups(self):
        return [
            {"id": 10, "name": "g10", "rate_multiplier": 0.1},
            {"id": 20, "name": "g20", "rate_multiplier": 0.2},
        ]

    def fetch_settings(self):
        return {"endpoints": [{"name": "default", "endpoint": "https://relay.example/v1"}]}

    def update_key_group(self, key_id, group_id):
        self.updated.append((key_id, group_id))
        self._keys[0] = {
            **self._keys[0],
            "group_id": group_id,
            "group": {"id": group_id, "name": f"g{group_id}", "rate_multiplier": 0.2},
        }
        return True, ""


class ExpiringRelayContext:
    domain = "https://relay.example"
    site = "relay.example"

    def __init__(self):
        self.tokens = []
        self.fetch_key_calls = 0
        self.update_calls = 0

    def set_token(self, token):
        self.tokens.append(token)

    def fetch_keys(self):
        self.fetch_key_calls += 1
        if self.fetch_key_calls == 1:
            return []
        return [{"id": 1, "name": "alpha", "key": "sk-alpha", "group_id": 10, "group": {"id": 10, "name": "g10"}}]

    def update_key_group(self, key_id, group_id):
        self.update_calls += 1
        if self.update_calls == 1:
            return False, "HTTP 401"
        return True, ""


class FakeHttpResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or json.dumps(payload)

    def json(self):
        return self._payload


def fake_chatgpt_auth(account_id: str, email: str) -> dict:
    def encoded(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    claims = {
        "email": email,
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
    }
    token = f"{encoded({'alg': 'none'})}.{encoded(claims)}.sig"
    return {
        "auth_mode": "chatgpt",
        "tokens": {"id_token": token, "access_token": f"access-{account_id}"},
    }


class ResponsesCapabilityProbeTests(unittest.TestCase):
    def test_priority_echo_is_the_only_positive_service_tier_evidence(self):
        response = FakeHttpResponse(200, {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "service_tier": "priority",
        })
        with patch.object(desktop_api.sub2cli_lib.requests, "post", return_value=response) as post:
            result = desktop_api.sub2cli_lib.test_codex(
                "https://relay.example/v1",
                "sk-test",
                model="gpt-5.6-sol",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["supports_service_tier"])
        self.assertEqual("priority", post.call_args.kwargs["json"]["service_tier"])
        self.assertEqual(1, post.call_args.kwargs["json"]["max_output_tokens"])

    def test_incomplete_priority_response_is_valid_and_not_retried(self):
        response = FakeHttpResponse(200, {
            "id": "resp_test",
            "object": "response",
            "status": "incomplete",
            "service_tier": "priority",
            "incomplete_details": {"reason": "max_output_tokens"},
        })
        with patch.object(desktop_api.sub2cli_lib.requests, "post", return_value=response) as post:
            result = desktop_api.sub2cli_lib.test_codex(
                "https://relay.example/v1",
                "sk-test",
                model="gpt-5.6-sol",
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["supports_service_tier"])
        self.assertEqual(1, post.call_count)

    def test_explicit_service_tier_rejection_retries_plain_responses_probe(self):
        rejected = FakeHttpResponse(400, {
            "error": {"message": "Unsupported parameter: service_tier"},
        })
        accepted = FakeHttpResponse(200, {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "service_tier": "priority",
        })
        with patch.object(
            desktop_api.sub2cli_lib.requests,
            "post",
            side_effect=[rejected, accepted],
        ) as post:
            result = desktop_api.sub2cli_lib.test_codex(
                "https://relay.example/v1",
                "sk-test",
                model="gpt-5.6-sol",
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["supports_service_tier"])
        self.assertEqual(2, post.call_count)
        self.assertEqual("priority", post.call_args_list[0].kwargs["json"]["service_tier"])
        self.assertNotIn("service_tier", post.call_args_list[1].kwargs["json"])

    def test_http_200_service_tier_error_retries_plain_responses_probe(self):
        rejected = FakeHttpResponse(200, {
            "error": {"message": "Unsupported parameter: service_tier"},
        })
        accepted = FakeHttpResponse(200, {
            "id": "resp_test",
            "object": "response",
            "status": "completed",
        })
        with patch.object(
            desktop_api.sub2cli_lib.requests,
            "post",
            side_effect=[rejected, accepted],
        ) as post:
            result = desktop_api.sub2cli_lib.test_codex(
                "https://relay.example/v1",
                "sk-test",
                model="gpt-5.6-sol",
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["supports_service_tier"])
        self.assertEqual(2, post.call_count)
        self.assertNotIn("service_tier", post.call_args_list[1].kwargs["json"])


class Sub2CliConfigSaveTests(unittest.TestCase):
    def test_save_config_preserves_route_pool_fields_from_newer_disk_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "current_route_pool": "fresh-pool",
                "route_pools": [{"id": "fresh-pool", "routes": [{"id": "fresh"}]}],
            }), encoding="utf-8")
            stale_cfg = {
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "current_route_pool": "stale-pool",
                "route_pools": [{"id": "stale-pool", "routes": [{"id": "stale"}]}],
            }

            desktop_api.sub2cli_lib.save_config(stale_cfg, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("fresh-pool", saved["current_route_pool"])
            self.assertEqual("fresh-pool", saved["route_pools"][0]["id"])
            self.assertNotIn(desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY, saved)

    def test_save_config_allows_explicit_route_pool_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "current_route_pool": "old-pool",
                "route_pools": [{"id": "old-pool"}],
            }), encoding="utf-8")
            next_cfg = {
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "current_route_pool": "new-pool",
                "route_pools": [{"id": "new-pool"}],
                desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY: list(desktop_api.sub2cli_lib.ROUTE_POOL_FIELDS),
            }

            desktop_api.sub2cli_lib.save_config(next_cfg, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual("new-pool", saved["current_route_pool"])
            self.assertEqual("new-pool", saved["route_pools"][0]["id"])
            self.assertNotIn(desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY, saved)

    def test_save_config_preserves_newer_service_tier_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "relay_service_tier_capabilities": {"fresh": True},
            }), encoding="utf-8")
            stale_cfg = {
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "relay_service_tier_capabilities": {"stale": True},
            }

            desktop_api.sub2cli_lib.save_config(stale_cfg, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"fresh": True}, saved["relay_service_tier_capabilities"])

    def test_save_config_allows_explicit_service_tier_capability_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "relay_service_tier_capabilities": {"old": True},
            }), encoding="utf-8")
            next_cfg = {
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "relay_service_tier_capabilities": {"new": True},
                desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY: [
                    "relay_service_tier_capabilities",
                ],
            }

            desktop_api.sub2cli_lib.save_config(next_cfg, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual({"new": True}, saved["relay_service_tier_capabilities"])
            self.assertNotIn(desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY, saved)

    def test_service_tier_setter_merges_another_group_without_stale_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({
                "domain": "https://relay.example",
                "relays": {"https://relay.example": {}},
                "accounts": {
                    "https://relay.example": {"current": "v@example.com"},
                },
                "relay_service_tier_capabilities": {
                    "https://relay.example": {
                        "v@example.com": {
                            "https://relay.example/v1": {
                                "10": {"gpt-fast": True, "gpt-slow": False},
                            },
                        },
                    },
                },
            }), encoding="utf-8")
            cfg = desktop_api.sub2cli_lib.load_config(str(path))

            desktop_api._set_relay_service_tier_capabilities(
                cfg,
                "https://relay.example",
                "https://relay.example/v1",
                20,
                {"gpt-fast": False, "gpt-new": True},
            )
            desktop_api.sub2cli_lib.save_config(cfg, str(path))

            saved = json.loads(path.read_text(encoding="utf-8"))
            groups = (
                saved["relay_service_tier_capabilities"]
                ["https://relay.example"]
                ["v@example.com"]
                ["https://relay.example/v1"]
            )
            self.assertEqual({
                "10": {"gpt-fast": True, "gpt-slow": False},
                "20": {"gpt-fast": False, "gpt-new": True},
            }, groups)
            self.assertNotIn(desktop_api.sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY, saved)


class DesktopRoutePoolApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
        }), encoding="utf-8")
        self.load_patch = patch.object(
            desktop_api.sub2cli_lib,
            "load_config",
            side_effect=lambda _path=None: json.loads(self.config_path.read_text(encoding="utf-8")),
        )
        self.save_patch = patch.object(
            desktop_api.sub2cli_lib,
            "save_config",
            side_effect=lambda cfg, _path=None: self.config_path.write_text(json.dumps(cfg), encoding="utf-8"),
        )
        self.path_patch = patch.object(
            desktop_api.sub2cli_lib,
            "default_config_path",
            return_value=str(self.config_path),
        )
        self.creds_path = Path(self.tmp.name) / "relay-credentials.json"
        self.creds_key_path = Path(self.tmp.name) / "relay-credentials.key"
        self.creds_path_patch = patch.object(desktop_api, "RELAY_CREDS_PATH", self.creds_path)
        self.creds_key_path_patch = patch.object(desktop_api, "RELAY_CREDS_KEY_PATH", self.creds_key_path)
        self.cred_key_patch = patch.object(desktop_api, "_relay_cred_key_bytes", None)
        self.load_patch.start()
        self.save_patch.start()
        self.path_patch.start()
        self.creds_path_patch.start()
        self.creds_key_path_patch.start()
        self.cred_key_patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.save_patch.stop)
        self.addCleanup(self.path_patch.stop)
        self.addCleanup(self.creds_path_patch.stop)
        self.addCleanup(self.creds_key_path_patch.stop)
        self.addCleanup(self.cred_key_patch.stop)

    def test_save_route_pool_rejects_silent_route_drop(self):
        api = desktop_api.JsApi()
        result = api.save_route_pool({
            "id": "default-pool",
            "routes": [
                {
                    "id": "good",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 1,
                    "priority": 10,
                },
                {
                    "id": "bad",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "priority": 20,
                },
            ],
        })

        self.assertFalse(result["ok"])
        self.assertIn("只有 1 条有效", result["error"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("route_pools", cfg)

    def test_live_relay_oauth_identity_overrides_stale_account_pointers(self):
        codex_home = Path(self.tmp.name) / ".codex"
        app_support = Path(self.tmp.name) / "Application Support"
        codex_home.mkdir()
        app_support.mkdir()
        auth_a = codex_home / "auth.a.json"
        auth_b = codex_home / "auth.b.json"
        auth_a.write_text(json.dumps(fake_chatgpt_auth("acct-a", "a@example.com")))
        auth_b.write_text(json.dumps(fake_chatgpt_auth("acct-b", "b@example.com")))
        (codex_home / "auth.json").write_text(
            json.dumps(fake_chatgpt_auth("acct-b", "b@example.com"))
        )
        slots_path = codex_home / "provider-slots.json"
        slots_path.write_text(json.dumps({
            "current": "pool",
            "active_oauth_slot": "a",
            "preferred_official_slot": "a",
            "slots": {
                "pool": {"mode": "relay"},
                "a": {"mode": "oauth", "auth_file": str(auth_a)},
                "b": {"mode": "oauth", "auth_file": str(auth_b)},
            },
        }))

        with patch.object(desktop_api, "CODEX_HOME_PATH", codex_home), \
             patch.object(desktop_api, "PROVIDER_SLOTS_PATH", slots_path), \
             patch.object(desktop_api, "CODEX_APP_SUPPORT_PATH", app_support), \
             patch.object(desktop_api, "CODEXBAR_MANAGED_ACCOUNTS_PATH", app_support / "missing.json"), \
             patch.object(desktop_api, "CODEX_LOCK_PATH", codex_home / ".lock"):
            accounts = desktop_api._discover_codex_accounts()
            result = desktop_api.JsApi().remove_codex_account("b")

        self.assertEqual("b", accounts["current_official"]["slot"])
        self.assertFalse(result["ok"])
        self.assertIn("ChatGPT 身份", result["error"])
        self.assertTrue(auth_b.exists())
        self.assertIn("b", json.loads(slots_path.read_text())["slots"])

    def test_live_relay_identity_protects_hintless_saved_oauth_slot(self):
        codex_home = Path(self.tmp.name) / ".codex"
        app_support = Path(self.tmp.name) / "Application Support"
        codex_home.mkdir()
        app_support.mkdir()
        auth_b = codex_home / "auth.b.json"
        auth_b.write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "opaque-saved-access",
                "refresh_token": "opaque-saved-refresh",
            },
        }))
        (codex_home / "auth.json").write_text(
            json.dumps(fake_chatgpt_auth("acct-b", "b@example.com"))
        )
        slots_path = codex_home / "provider-slots.json"
        slots_path.write_text(json.dumps({
            "current": "pool",
            "active_oauth_slot": "b",
            "preferred_official_slot": "b",
            "slots": {
                "pool": {"mode": "relay"},
                "b": {"mode": "oauth", "auth_file": str(auth_b)},
            },
        }))

        with patch.object(desktop_api, "CODEX_HOME_PATH", codex_home), \
             patch.object(desktop_api, "PROVIDER_SLOTS_PATH", slots_path), \
             patch.object(desktop_api, "CODEX_APP_SUPPORT_PATH", app_support), \
             patch.object(desktop_api, "CODEXBAR_MANAGED_ACCOUNTS_PATH", app_support / "missing.json"), \
             patch.object(desktop_api, "CODEX_LOCK_PATH", codex_home / ".lock"):
            result = desktop_api.JsApi().remove_codex_account("b")

        self.assertFalse(result["ok"])
        self.assertIn("ChatGPT 身份", result["error"])
        self.assertTrue(auth_b.exists())
        self.assertIn("b", json.loads(slots_path.read_text())["slots"])

    def test_route_pool_update_key_group_updates_key_and_returns_groups(self):
        api = desktop_api.JsApi()
        fake_ctx = FakeRelayContext()
        with patch.object(api, "_relay_ctx_for_domain", return_value=fake_ctx):
            result = api.route_pool_update_key_group("https://relay.example", 1, 20)

        self.assertTrue(result["ok"])
        self.assertEqual([(1, 20)], fake_ctx.updated)
        self.assertEqual(20, result["key"]["group_id"])
        self.assertEqual("g20", result["key"]["group_name"])
        self.assertEqual([10, 20], [group["id"] for group in result["source"]["groups"]])

    def test_save_route_pool_normalizes_bare_relay_domain(self):
        api = desktop_api.JsApi()
        with patch.object(api, "_relay_ctx_for_domain", return_value=FakeRelayContext()), \
             patch.object(api, "_run_inject_add_pool", return_value={"ok": True, "stdout": "applied"}):
            result = api.save_route_pool({
                "id": "default-pool",
                "routes": [{
                    "id": "relay-bare-domain",
                    "source_type": "relay",
                    "relay_domain": "relay.example",
                    "key_id": 1,
                    "priority": 10,
                }],
            })

        self.assertTrue(result["ok"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        route = cfg["route_pools"][0]["routes"][0]
        self.assertEqual("https://relay.example", route["relay_domain"])
        self.assertTrue(result["applied"])

    def test_save_route_pool_hot_applies_saved_routes(self):
        api = desktop_api.JsApi()
        captured = {}

        def fake_apply(pool, routes, secrets):
            captured["pool"] = pool
            captured["routes"] = routes
            captured["secrets"] = secrets
            return {
                "ok": True,
                "stdout": "已更新 route pool",
                "backup_name": "add-pool-default-pool-1",
            }

        with patch.object(api, "_relay_ctx_for_domain", return_value=FakeRelayContext()), \
             patch.object(api, "_run_inject_add_pool", side_effect=fake_apply):
            result = api.save_route_pool({
                "id": "default-pool",
                "routes": [{
                    "id": "relay-hot",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 1,
                    "priority": 10,
                }],
            })

        self.assertTrue(result["ok"])
        self.assertTrue(result["applied"])
        self.assertEqual("add-pool-default-pool-1", result["backup_name"])
        self.assertEqual("default-pool", captured["pool"]["id"])
        self.assertEqual(["relay-hot"], [route["id"] for route in captured["routes"]])
        self.assertEqual(["sk-alpha"], captured["secrets"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("default-pool", cfg["current_route_pool"])

    def test_save_route_pool_does_not_persist_when_hot_apply_fails(self):
        api = desktop_api.JsApi()
        with patch.object(api, "_relay_ctx_for_domain", return_value=FakeRelayContext()), \
             patch.object(api, "_run_inject_add_pool", return_value={"ok": False, "error": "apply failed"}):
            result = api.save_route_pool({
                "id": "default-pool",
                "routes": [{
                    "id": "relay-hot",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 1,
                    "priority": 10,
                }],
            })

        self.assertFalse(result["ok"])
        self.assertEqual("apply failed", result["error"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("route_pools", cfg)
        self.assertNotIn("current_route_pool", cfg)

    def test_route_pool_status_sends_local_proxy_bearer(self):
        codex_home = Path(self.tmp.name) / ".codex"
        codex_home.mkdir()
        (codex_home / "sub2cli-proxy-token").write_text("local-secret\n", encoding="utf-8")
        seen = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self):
                return b'{"ok":true,"current_route":"primary"}'

        def fake_urlopen(request, timeout=0):
            seen["authorization"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return FakeResponse()

        with patch.object(desktop_api, "CODEX_HOME_PATH", codex_home), \
             patch.object(desktop_api, "_route_pool_log_lines", return_value=[]), \
             patch.object(desktop_api.urllib.request, "urlopen", side_effect=fake_urlopen):
            result = desktop_api.JsApi().route_pool_status()

        self.assertTrue(result["ok"])
        self.assertEqual("Bearer local-secret", seen["authorization"])
        self.assertEqual(0.8, seen["timeout"])

    def test_route_pool_relay_source_accepts_bare_domain(self):
        api = desktop_api.JsApi()
        fake_ctx = FakeRelayContext()
        with patch.object(api, "_relay_ctx_for_domain", return_value=fake_ctx):
            result = api.route_pool_relay_source("relay.example")

        self.assertTrue(result["ok"])
        self.assertEqual("https://relay.example", result["source"]["domain"])
        self.assertEqual(["alpha"], [key["name"] for key in result["source"]["keys"]])

    def test_route_pool_config_apply_falls_back_to_key_name_when_saved_key_id_is_stale(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "route_pools": [{
                "id": "default-pool",
                "name": "连接池",
                "routes": [{
                    "id": "relay-stale-id",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 999,
                    "key_name": "alpha",
                    "base_url": "https://relay.example/v1",
                    "priority": 10,
                }],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        fake_ctx = FakeRelayContext()
        captured = {}

        def fake_run(pool, routes, secrets):
            captured["pool"] = pool
            captured["routes"] = routes
            captured["secrets"] = secrets
            return {"ok": True, "stdout": "ok"}

        with patch.object(api, "_relay_ctx_for_domain", return_value=fake_ctx), \
             patch.object(api, "_run_inject_add_pool", side_effect=fake_run):
            result = api.route_pool_config_apply("default-pool")

        self.assertTrue(result["ok"])
        self.assertEqual("relay", captured["routes"][0]["source_type"])
        self.assertEqual("sk-alpha", captured["routes"][0]["api_key"])
        self.assertEqual(["sk-alpha"], captured["secrets"])

    def test_route_pool_custom_route_inherits_saved_api_protocol(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "custom_apis": [
                {
                    "id": "newapi",
                    "name": "newapi",
                    "base_url": "https://newapi.example",
                    "protocol": "responses",
                    "service_tier_capabilities": {
                        "gpt-fast": True,
                        "gpt-slow": False,
                    },
                    "model_columns": ["gpt-fast", "gpt-slow"],
                },
                {
                    "id": "no-priority",
                    "name": "no-priority",
                    "base_url": "https://no-priority.example",
                    "protocol": "responses",
                    "service_tier_capabilities": {"gpt-slow": False},
                    "model_columns": ["gpt-slow"],
                },
            ],
            "route_pools": [{
                "id": "default-pool",
                "name": "连接池",
                "routes": [{
                    "id": "custom-newapi",
                    "source_type": "custom",
                    "custom_api_id": "newapi",
                    "priority": 10,
                }, {
                    "id": "custom-explicit-false",
                    "source_type": "custom",
                    "custom_api_id": "newapi",
                    "priority": 20,
                    "supports_service_tier": False,
                }, {
                    "id": "custom-explicit-true",
                    "source_type": "custom",
                    "custom_api_id": "no-priority",
                    "priority": 30,
                    "supports_service_tier": True,
                }],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        captured = {}

        def fake_run(pool, routes, secrets):
            captured["routes"] = routes
            captured["secrets"] = secrets
            return {"ok": True, "stdout": "ok"}

        with patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-newapi"), \
             patch.object(api, "_run_inject_add_pool", side_effect=fake_run):
            result = api.route_pool_config_apply("default-pool")

        self.assertTrue(result["ok"])
        self.assertEqual(["responses"] * 3, [route["protocol"] for route in captured["routes"]])
        self.assertEqual(
            [["gpt-fast"], ["gpt-fast"], []],
            [route["service_tier_models"] for route in captured["routes"]],
        )
        self.assertNotIn("supports_service_tier", captured["routes"][0])
        self.assertFalse(captured["routes"][1]["supports_service_tier"])
        self.assertTrue(captured["routes"][2]["supports_service_tier"])
        self.assertEqual(["sk-newapi"] * 3, captured["secrets"])

    def test_add_custom_api_prefers_native_responses_when_probe_passes(self):
        api = desktop_api.JsApi()
        with patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=(["gpt-5.5"], None)), \
             patch.object(desktop_api.sub2cli_lib, "test_model", return_value={
                 "ok": True,
                 "summary": "responses ok",
                 "supports_service_tier": True,
             }) as test_model, \
             patch.object(desktop_api, "_detect_custom_api_provider_kind", return_value=("openai-compatible", None, None)), \
             patch.object(desktop_api, "_kc_custom_api_set", return_value=True), \
             patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-newapi"):
            result = api.add_custom_api("https://newapi.example", "sk-newapi", "newapi")

        self.assertTrue(result["ok"])
        self.assertEqual("responses", result["protocol"])
        self.assertEqual({"gpt-5.5": True}, result["service_tier_capabilities"])
        self.assertEqual(["gpt-5.5"], result["service_tier_models"])
        test_model.assert_called_once()
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("responses", cfg["custom_apis"][0]["protocol"])
        self.assertEqual(
            {"gpt-5.5": True},
            cfg["custom_apis"][0]["service_tier_capabilities"],
        )

    def test_add_custom_api_falls_back_to_chat_when_responses_probe_fails(self):
        api = desktop_api.JsApi()
        with patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=(["gpt-5.5"], None)), \
             patch.object(desktop_api.sub2cli_lib, "test_model", return_value={"ok": False, "summary": "not responses"}), \
             patch.object(desktop_api, "_detect_custom_api_provider_kind", return_value=("openai-compatible", None, None)), \
             patch.object(desktop_api, "_kc_custom_api_set", return_value=True), \
             patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-chat"):
            result = api.add_custom_api("https://chat.example", "sk-chat", "chat")

        self.assertTrue(result["ok"])
        self.assertEqual("chat", result["protocol"])
        self.assertEqual({}, result["service_tier_capabilities"])
        self.assertEqual([], result["service_tier_models"])
        self.assertIn("protocol_error", result)
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("chat", cfg["custom_apis"][0]["protocol"])
        self.assertEqual({}, cfg["custom_apis"][0]["service_tier_capabilities"])

    def test_custom_api_model_test_persists_only_successful_responses_capability(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "custom_apis": [{
                "id": "priority-api",
                "name": "priority-api",
                "base_url": "https://priority.example/v1",
                "protocol": "responses",
                "service_tier_capabilities": {},
            }],
            "route_pools": [{
                "id": "default-pool",
                "routes": [{
                    "id": "custom-inherit-after-probe",
                    "source_type": "custom",
                    "custom_api_id": "priority-api",
                    "priority": 10,
                }],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        with patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-priority"), \
             patch.object(desktop_api.sub2cli_lib, "test_model", side_effect=[{
                 "ok": True, "status": 200, "supports_service_tier": True,
             }, {
                 "ok": True, "status": 200, "supports_service_tier": False,
             }]):
            fast = api.test_custom_api_model("priority-api", "gpt-fast")
            slow = api.test_custom_api_model("priority-api", "gpt-slow")

        self.assertTrue(fast["ok"])
        self.assertTrue(slow["ok"])
        self.assertEqual({"gpt-fast": True}, fast["service_tier_capabilities"])
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            slow["service_tier_capabilities"],
        )
        self.assertEqual(["gpt-fast"], slow["service_tier_models"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            cfg["custom_apis"][0]["service_tier_capabilities"],
        )

        captured = {}
        with patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-priority"), \
             patch.object(api, "_run_inject_add_pool", side_effect=lambda _pool, routes, _secrets: (
                 captured.update({"routes": routes}) or {"ok": True, "stdout": "ok"}
             )):
            applied = api.route_pool_config_apply("default-pool")

        self.assertTrue(applied["ok"])
        self.assertEqual(["gpt-fast"], captured["routes"][0]["service_tier_models"])
        self.assertNotIn("supports_service_tier", captured["routes"][0])

        with patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-priority"), \
             patch.object(desktop_api.sub2cli_lib, "test_model", return_value={
                 "ok": False,
                 "status": 503,
                 "supports_service_tier": False,
             }):
            failed = api.test_custom_api_model("priority-api", "gpt-fast")

        self.assertTrue(failed["ok"])
        self.assertNotIn("service_tier_capabilities", failed)
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            cfg["custom_apis"][0]["service_tier_capabilities"],
        )

    def test_relay_route_inherits_only_matching_group_endpoint_capability(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {
                "https://relay.example": {
                    "default_endpoint_url": "https://relay.example/v1",
                },
            },
            "accounts": {
                "https://relay.example": {"current": "v@example.com"},
            },
            "relay_service_tier_capabilities": {
                "https://relay.example": {
                    "v@example.com": {
                        "https://relay.example/v1": {
                            "10": {"gpt-fast": True, "gpt-slow": False},
                        },
                        "https://other-endpoint.example/v1": {
                            "10": {"gpt-fast": False},
                        },
                    },
                },
            },
            "route_pools": [{
                "id": "default-pool",
                "routes": [{
                    "id": "relay-inherit",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 1,
                    "base_url": "https://relay.example/v1/",
                    "priority": 10,
                }, {
                    "id": "relay-explicit-false",
                    "source_type": "relay",
                    "relay_domain": "https://relay.example",
                    "key_id": 1,
                    "base_url": "https://relay.example/v1",
                    "priority": 20,
                    "supports_service_tier": False,
                }],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        captured = {}

        def fake_run(_pool, routes, _secrets):
            captured["routes"] = routes
            return {"ok": True, "stdout": "ok"}

        with patch.object(api, "_relay_ctx_for_domain", return_value=FakeRelayContext()), \
             patch.object(api, "_run_inject_add_pool", side_effect=fake_run):
            result = api.route_pool_config_apply("default-pool")

        self.assertTrue(result["ok"])
        self.assertEqual(
            [["gpt-fast"], ["gpt-fast"]],
            [route["service_tier_models"] for route in captured["routes"]],
        )
        self.assertNotIn("supports_service_tier", captured["routes"][0])
        self.assertFalse(captured["routes"][1]["supports_service_tier"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual({}, desktop_api._relay_service_tier_model_capabilities(
            cfg,
            "https://relay.example",
            "https://relay.example/v1",
            20,
        ))
        self.assertEqual({"gpt-fast": False}, desktop_api._relay_service_tier_model_capabilities(
            cfg,
            "https://relay.example",
            "https://other-endpoint.example/v1",
            10,
        ))
        cfg["accounts"]["https://relay.example"]["current"] = "other@example.com"
        self.assertEqual({}, desktop_api._relay_service_tier_model_capabilities(
            cfg,
            "https://relay.example",
            "https://relay.example/v1",
            10,
        ))

    def test_group_responses_probe_persists_relay_group_endpoint_capability(self):
        cfg = {
            "domain": "https://relay.example",
            "relays": {
                "https://relay.example": {
                    "default_endpoint_url": "https://relay.example/v1",
                },
            },
            "accounts": {
                "https://relay.example": {"current": "v@example.com"},
            },
            "default_key_id": 1,
            "default_key_name": "alpha",
            "default_endpoint_url": "https://relay.example/v1/",
            "group_model_columns": ["gpt-fast", "gpt-slow"],
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        api = desktop_api.JsApi()
        fake_ctx = FakeRelayContext()
        fake_ctx.config_path = str(self.config_path)
        with patch.object(api, "_ensure_ctx", return_value=(fake_ctx, cfg)), \
             patch.object(
                 desktop_api.sub2cli_lib,
                 "ensure_test_key",
                 return_value=(fake_ctx.fetch_keys(), fake_ctx.fetch_keys()[0]),
             ), \
             patch.object(
                 desktop_api.sub2cli_lib,
                 "test_model",
                 side_effect=lambda _url, _key, model, **_kwargs: {
                     "ok": True,
                     "status": 200,
                     "supports_service_tier": model == "gpt-fast",
                 },
             ):
            result = api.test_group(10, ["gpt-fast", "gpt-slow"], restore=False)

        self.assertTrue(result["ok"])
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            result["service_tier_capabilities"],
        )
        self.assertEqual(["gpt-fast"], result["service_tier_models"])
        self.assertEqual("v@example.com", result["service_tier_account"])
        self.assertEqual("https://relay.example/v1", result["service_tier_endpoint"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            saved["relay_service_tier_capabilities"]
            ["https://relay.example"]
            ["v@example.com"]
            ["https://relay.example/v1"]
            ["10"]
        )
        source = api._route_pool_relay_source(saved, "https://relay.example", load=False)
        self.assertEqual(
            {"gpt-fast": True, "gpt-slow": False},
            source["service_tier_capabilities"]["https://relay.example/v1"]["10"],
        )

    def test_route_pool_ui_new_routes_leave_capability_for_source_inheritance(self):
        source = (ROOT / "desktop" / "ui" / "app.js").read_text(encoding="utf-8")
        relay_add = source.split("async function addRelayRouteToPool()", 1)[1].split(
            "async function addSelectedRouteToPool()",
            1,
        )[0]
        custom_add = source.split("function addCustomRouteToPool()", 1)[1].split(
            "async function saveRoutePool(",
            1,
        )[0]
        self.assertNotIn("supports_service_tier", relay_add)
        self.assertNotIn("supports_service_tier", custom_add)

    def test_custom_api_candidate_distinguishes_unknown_from_verified_false(self):
        api = desktop_api.JsApi()
        unknown = api._serialize_custom_api({
            "id": "legacy",
            "base_url": "https://legacy.example/v1",
            "protocol": "responses",
        }, key="sk-legacy")
        verified_false = api._serialize_custom_api({
            "id": "verified",
            "base_url": "https://verified.example/v1",
            "protocol": "responses",
            "service_tier_capabilities": {"gpt-slow": False},
        }, key="sk-verified")

        self.assertNotIn("service_tier_capabilities", unknown)
        self.assertEqual({"gpt-slow": False}, verified_false["service_tier_capabilities"])
        self.assertEqual([], verified_false["service_tier_models"])

    def test_probe_relay_routes_openai_compatible_site_to_api_key_flow(self):
        api = desktop_api.JsApi()
        with patch.object(desktop_api.Sub2Context, "settings_reachable_anonymous", return_value=(False, "HTTP 404")), \
             patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=([], "HTTP 401")):
            result = api.probe_relay("https://newapi.example")

        self.assertTrue(result["ok"])
        self.assertFalse(result["is_sub2api"])
        self.assertTrue(result["is_openai_compatible"])
        self.assertEqual("https://newapi.example", result["domain"])

    def test_add_custom_api_records_new_api_usage_capability(self):
        api = desktop_api.JsApi()
        usage = {
            "object": "token_usage",
            "name": "codex",
            "total_granted": 1200,
            "total_used": 200,
            "total_available": 1000,
            "unlimited_quota": False,
            "model_limits": {"gpt-5.5": True},
            "model_limits_enabled": True,
            "expires_at": 0,
        }
        with patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=(["gpt-5.5"], None)), \
             patch.object(desktop_api.sub2cli_lib, "test_model", return_value={"ok": True}), \
             patch.object(desktop_api, "_detect_custom_api_provider_kind", return_value=("new-api", usage, None)), \
             patch.object(desktop_api, "_kc_custom_api_set", return_value=True), \
             patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-newapi"):
            result = api.add_custom_api("https://newapi.example", "sk-newapi", "newapi")

        self.assertTrue(result["ok"])
        self.assertEqual("new-api", result["provider_kind"])
        self.assertEqual(1000, result["usage"]["total_available"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("new-api", cfg["custom_apis"][0]["provider_kind"])
        self.assertEqual(200, cfg["custom_apis"][0]["usage"]["total_used"])

    def test_refresh_custom_api_models_updates_new_api_usage(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "custom_apis": [{
                "id": "newapi",
                "name": "newapi",
                "base_url": "https://newapi.example",
                "protocol": "responses",
                "model_columns": ["gpt-5.5"],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        usage = {
            "object": "token_usage",
            "name": "codex",
            "total_granted": 20,
            "total_used": 8,
            "total_available": 12,
            "unlimited_quota": False,
            "model_limits": {},
            "model_limits_enabled": False,
            "expires_at": 0,
        }
        with patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=(["gpt-5.5"], None)), \
             patch.object(desktop_api, "_detect_custom_api_provider_kind", return_value=("new-api", usage, None)), \
             patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-newapi"):
            result = api.refresh_custom_api_models("newapi")

        self.assertTrue(result["ok"])
        self.assertEqual("new-api", result["provider_kind"])
        self.assertEqual(12, result["usage"]["total_available"])
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("new-api", cfg["custom_apis"][0]["provider_kind"])
        self.assertEqual(8, cfg["custom_apis"][0]["usage"]["total_used"])

    def test_new_api_usage_endpoint_detects_provider_from_version_header(self):
        class FakeResponse:
            status_code = 401
            headers = {"X-New-Api-Version": "0.9-test"}

            def json(self):
                return {"success": False, "message": "bad key"}

        with patch.object(desktop_api.requests, "get", return_value=FakeResponse()) as request_get:
            kind, usage, usage_error = desktop_api._detect_custom_api_provider_kind("https://newapi.example/v1", "sk-test")

        self.assertEqual("new-api", kind)
        self.assertIsNone(usage)
        self.assertEqual("HTTP 401", usage_error)
        self.assertEqual("https://newapi.example/api/usage/token/", request_get.call_args.args[0])

    def test_new_api_usage_header_survives_bad_json(self):
        class FakeResponse:
            status_code = 200
            headers = {"X-New-Api-Version": "0.9-test"}

            def json(self):
                raise ValueError("bad json")

        with patch.object(desktop_api.requests, "get", return_value=FakeResponse()):
            kind, usage, usage_error = desktop_api._detect_custom_api_provider_kind("https://newapi.example", "sk-test")

        self.assertEqual("new-api", kind)
        self.assertIsNone(usage)
        self.assertIn("ValueError", usage_error)

    def test_list_route_pools_preloads_saved_route_relay_sources(self):
        self.config_path.write_text(json.dumps({
            "domain": "https://relay.example",
            "relays": {
                "https://relay.example": {},
                "https://other.example": {},
            },
            "route_pools": [{
                "id": "default-pool",
                "name": "连接池",
                "routes": [{
                    "id": "relay-other",
                    "source_type": "relay",
                    "relay_domain": "https://other.example",
                    "key_id": 1,
                    "key_name": "alpha",
                    "base_url": "https://other.example/v1",
                    "priority": 10,
                }],
            }],
        }), encoding="utf-8")
        api = desktop_api.JsApi()
        fake_ctx = FakeRelayContext()
        with patch.object(api, "_relay_ctx_for_domain", return_value=fake_ctx):
            result = api.list_route_pools()

        sources = {
            source["domain"]: source
            for source in result["candidates"]["relay_sources"]
        }
        self.assertTrue(sources["https://other.example"]["loaded"])
        self.assertEqual(["alpha"], [key["name"] for key in sources["https://other.example"]["keys"]])

    def test_relay_credentials_use_local_cache_before_keychain(self):
        with patch.object(desktop_api, "_security_set_password", side_effect=AssertionError("keychain write")), \
             patch.object(desktop_api, "_security_get_password", side_effect=AssertionError("keychain read")):
            self.assertTrue(desktop_api._kc_creds_set("https://relay.example", "v@example.com", "pw-1"))
            self.assertTrue(desktop_api._kc_set("https://relay.example", "v@example.com", "token-1"))
            self.assertEqual("pw-1", desktop_api._kc_creds_get("https://relay.example", "v@example.com"))
            self.assertEqual("token-1", desktop_api._kc_get("https://relay.example", "v@example.com"))

        raw = self.creds_path.read_text(encoding="utf-8")
        self.assertNotIn("pw-1", raw)
        self.assertNotIn("token-1", raw)

    def test_relay_credential_token_and_password_deletes_are_independent(self):
        with patch.object(desktop_api, "_security_delete_password", return_value=True), \
             patch.object(desktop_api, "_security_get_password", return_value=None):
            self.assertTrue(desktop_api._kc_creds_set("https://relay.example", "v@example.com", "pw-1"))
            self.assertTrue(desktop_api._kc_set("https://relay.example", "v@example.com", "token-1"))

            desktop_api._kc_delete("https://relay.example", "v@example.com")
            self.assertEqual("pw-1", desktop_api._kc_creds_get("https://relay.example", "v@example.com"))
            self.assertIsNone(desktop_api._kc_get("https://relay.example", "v@example.com"))

            self.assertTrue(desktop_api._kc_set("https://relay.example", "v@example.com", "token-2"))
            desktop_api._kc_creds_delete("https://relay.example", "v@example.com")
            self.assertIsNone(desktop_api._kc_creds_get("https://relay.example", "v@example.com"))
            self.assertEqual("token-2", desktop_api._kc_get("https://relay.example", "v@example.com"))

    def test_legacy_keychain_credentials_are_migrated_to_local_cache(self):
        with patch.object(desktop_api, "_security_get_password", return_value="legacy-pw"):
            self.assertEqual("legacy-pw", desktop_api._kc_creds_get("https://relay.example", "v@example.com"))

        with patch.object(desktop_api, "_security_get_password", side_effect=AssertionError("keychain read")):
            self.assertEqual("legacy-pw", desktop_api._kc_creds_get("https://relay.example", "v@example.com"))

    def test_auto_relogin_replays_empty_fetch_once(self):
        cfg = {
            "accounts": {
                "https://relay.example": {
                    "current": "v@example.com",
                    "saved": [{"email": "v@example.com"}],
                }
            }
        }
        ctx = ExpiringRelayContext()
        with patch.object(desktop_api, "_kc_creds_get", return_value="pw-1"), \
             patch.object(desktop_api, "_sub2_login", return_value=("token-2", "")) as login, \
             patch.object(desktop_api, "_kc_set", return_value=True):
            wrapped = desktop_api._AutoReloginContext(ctx, cfg)
            keys = wrapped.fetch_keys()

        self.assertEqual(["token-2"], ctx.tokens)
        self.assertEqual(2, ctx.fetch_key_calls)
        self.assertEqual(["alpha"], [key["name"] for key in keys])
        login.assert_called_once_with("https://relay.example", "v@example.com", "pw-1")

    def test_auto_relogin_fallback_updates_capability_account_identity(self):
        cfg = {
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "accounts": {
                "https://relay.example": {
                    "current": "a@example.com",
                    "saved": [
                        {"email": "a@example.com"},
                        {"email": "b@example.com"},
                    ],
                },
            },
            "relay_service_tier_capabilities": {
                "https://relay.example": {
                    "a@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": True}}},
                    "b@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": False}}},
                },
            },
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        ctx = ExpiringRelayContext()
        ctx.config_path = str(self.config_path)

        def fake_login(_domain, email, _password):
            return ("", "expired") if email == "a@example.com" else ("token-b", "")

        with patch.object(
            desktop_api,
            "_kc_creds_get",
            side_effect=lambda _domain, email: f"pw-{email}",
        ), patch.object(
            desktop_api,
            "_sub2_login",
            side_effect=fake_login,
        ) as login, patch.object(desktop_api, "_kc_set", return_value=True):
            wrapped = desktop_api._AutoReloginContext(ctx, cfg)
            keys = wrapped.fetch_keys()

        self.assertEqual(["alpha"], [key["name"] for key in keys])
        self.assertEqual("b@example.com", cfg["accounts"][ctx.domain]["current"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("b@example.com", saved["accounts"][ctx.domain]["current"])
        self.assertEqual(
            "b@example.com",
            desktop_api._relay_service_tier_account_key(saved, ctx.domain),
        )
        self.assertEqual({"gpt-fast": False}, desktop_api._relay_service_tier_model_capabilities(
            saved,
            ctx.domain,
            "https://relay.example/v1",
            10,
        ))
        self.assertEqual(
            ["a@example.com", "b@example.com"],
            [call.args[1] for call in login.call_args_list],
        )

    def test_explicit_account_switch_updates_runtime_capability_identity(self):
        cfg = {
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "accounts": {
                "https://relay.example": {
                    "current": "a@example.com",
                    "saved": [
                        {"email": "a@example.com"},
                        {"email": "b@example.com"},
                    ],
                },
            },
            "relay_service_tier_capabilities": {
                "https://relay.example": {
                    "a@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": True}}},
                    "b@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": False}}},
                },
            },
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        raw_ctx = ExpiringRelayContext()
        raw_ctx.config_path = str(self.config_path)
        wrapped = desktop_api._AutoReloginContext(raw_ctx, cfg)
        desktop_api._set_relay_authenticated_token(wrapped, "token-a", "a@example.com")
        api = desktop_api.JsApi()

        with patch.object(api, "_ensure_ctx", return_value=(wrapped, cfg)), \
             patch.object(desktop_api, "_kc_creds_get", return_value="pw-b"), \
             patch.object(desktop_api, "_sub2_login", return_value=("token-b", "")), \
             patch.object(desktop_api, "_kc_set", return_value=True), \
             patch.object(api, "bootstrap", return_value={"ok": True}):
            result = api.switch_account("b@example.com")

        self.assertTrue(result["ok"])
        self.assertEqual("b@example.com", cfg["accounts"][raw_ctx.domain]["current"])
        self.assertEqual(
            "b@example.com",
            desktop_api._relay_service_tier_account_key(cfg, raw_ctx.domain, wrapped),
        )
        self.assertEqual({"gpt-fast": False}, desktop_api._relay_service_tier_model_capabilities(
            cfg,
            raw_ctx.domain,
            "https://relay.example/v1",
            10,
            desktop_api._relay_service_tier_account_key(cfg, raw_ctx.domain, wrapped),
        ))
        self.assertEqual("b@example.com", raw_ctx._sub2cli_authenticated_email)
        self.assertEqual("b@example.com", wrapped._sub2cli_authenticated_email)

    def test_explicit_switch_then_auto_fallback_has_one_runtime_identity(self):
        cfg = {
            "domain": "https://relay.example",
            "relays": {"https://relay.example": {}},
            "accounts": {
                "https://relay.example": {
                    "current": "a@example.com",
                    "saved": [
                        {"email": "a@example.com"},
                        {"email": "b@example.com"},
                    ],
                },
            },
            "relay_service_tier_capabilities": {
                "https://relay.example": {
                    "a@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": True}}},
                    "b@example.com": {"https://relay.example/v1": {"10": {"gpt-fast": False}}},
                },
            },
        }
        self.config_path.write_text(json.dumps(cfg), encoding="utf-8")
        raw_ctx = ExpiringRelayContext()
        raw_ctx.config_path = str(self.config_path)
        wrapped = desktop_api._AutoReloginContext(raw_ctx, cfg)
        desktop_api._set_relay_authenticated_token(wrapped, "token-a", "a@example.com")
        api = desktop_api.JsApi()

        with patch.object(api, "_ensure_ctx", return_value=(wrapped, cfg)), \
             patch.object(desktop_api, "_kc_creds_get", return_value="pw-b"), \
             patch.object(desktop_api, "_sub2_login", return_value=("token-b", "")), \
             patch.object(desktop_api, "_kc_set", return_value=True), \
             patch.object(api, "bootstrap", return_value={"ok": True}):
            switched = api.switch_account("b@example.com")

        self.assertTrue(switched["ok"])

        def fallback_login(_domain, email, _password):
            return ("", "expired") if email == "b@example.com" else ("token-a2", "")

        with patch.object(
            desktop_api,
            "_kc_creds_get",
            side_effect=lambda _domain, email: f"pw-{email}",
        ), patch.object(
            desktop_api,
            "_sub2_login",
            side_effect=fallback_login,
        ), patch.object(desktop_api, "_kc_set", return_value=True):
            keys = wrapped.fetch_keys()

        self.assertEqual(["alpha"], [key["name"] for key in keys])
        self.assertEqual("a@example.com", cfg["accounts"][raw_ctx.domain]["current"])
        self.assertEqual(
            "a@example.com",
            desktop_api._relay_service_tier_account_key(cfg, raw_ctx.domain, wrapped),
        )
        self.assertEqual({"gpt-fast": True}, desktop_api._relay_service_tier_model_capabilities(
            cfg,
            raw_ctx.domain,
            "https://relay.example/v1",
            10,
            desktop_api._relay_service_tier_account_key(cfg, raw_ctx.domain, wrapped),
        ))
        self.assertNotIn("_sub2cli_authenticated_email", wrapped.__dict__)
        self.assertEqual("a@example.com", raw_ctx._sub2cli_authenticated_email)

    def test_auto_relogin_replays_auth_update_once(self):
        cfg = {
            "accounts": {
                "https://relay.example": {
                    "current": "v@example.com",
                    "saved": [{"email": "v@example.com"}],
                }
            }
        }
        ctx = ExpiringRelayContext()
        with patch.object(desktop_api, "_kc_creds_get", return_value="pw-1"), \
             patch.object(desktop_api, "_sub2_login", return_value=("token-2", "")), \
             patch.object(desktop_api, "_kc_set", return_value=True):
            wrapped = desktop_api._AutoReloginContext(ctx, cfg)
            ok, err = wrapped.update_key_group(1, 20)

        self.assertTrue(ok)
        self.assertEqual("", err)
        self.assertEqual(["token-2"], ctx.tokens)
        self.assertEqual(2, ctx.update_calls)


if __name__ == "__main__":
    unittest.main()
