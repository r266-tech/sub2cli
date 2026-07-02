import importlib.machinery
import importlib.util
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
            "custom_apis": [{
                "id": "newapi",
                "name": "newapi",
                "base_url": "https://newapi.example",
                "protocol": "responses",
                "model_columns": ["gpt-5.5"],
            }],
            "route_pools": [{
                "id": "default-pool",
                "name": "连接池",
                "routes": [{
                    "id": "custom-newapi",
                    "source_type": "custom",
                    "custom_api_id": "newapi",
                    "priority": 10,
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
        self.assertEqual("responses", captured["routes"][0]["protocol"])
        self.assertEqual(["sk-newapi"], captured["secrets"])

    def test_add_custom_api_prefers_native_responses_when_probe_passes(self):
        api = desktop_api.JsApi()
        with patch.object(desktop_api.sub2cli_lib, "fetch_models", return_value=(["gpt-5.5"], None)), \
             patch.object(desktop_api.sub2cli_lib, "test_model", return_value={"ok": True, "summary": "responses ok"}) as test_model, \
             patch.object(desktop_api, "_detect_custom_api_provider_kind", return_value=("openai-compatible", None, None)), \
             patch.object(desktop_api, "_kc_custom_api_set", return_value=True), \
             patch.object(desktop_api, "_kc_custom_api_get", return_value="sk-newapi"):
            result = api.add_custom_api("https://newapi.example", "sk-newapi", "newapi")

        self.assertTrue(result["ok"])
        self.assertEqual("responses", result["protocol"])
        test_model.assert_called_once()
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("responses", cfg["custom_apis"][0]["protocol"])

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
        self.assertIn("protocol_error", result)
        cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("chat", cfg["custom_apis"][0]["protocol"])

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
