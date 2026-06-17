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
        self.load_patch.start()
        self.save_patch.start()
        self.path_patch.start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.load_patch.stop)
        self.addCleanup(self.save_patch.stop)
        self.addCleanup(self.path_patch.stop)

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


if __name__ == "__main__":
    unittest.main()
