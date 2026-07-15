"""Unit tests for Codex channel menu helpers (recent list / ordering)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_inject(tmp_home: Path):
    # Isolate CODEX_HOME so tests never touch the developer's real slots file.
    import os

    os.environ["CODEX_HOME"] = str(tmp_home / ".codex")
    os.environ["CODEX_PROVIDER_HOME"] = str(tmp_home / ".codex")
    loader = importlib.machinery.SourceFileLoader(
        "sub2cli_inject_menu", str(ROOT / "sub2cli-inject")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RecentSlotsHelpersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".codex").mkdir()
        self.mod = load_inject(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_touch_recent_is_mru_and_capped(self):
        data = {"slots": {}, "recent_slots": []}
        for name in ["a", "b", "c", "a"]:
            self.mod.touch_recent_slot(data, name, limit=3)
        self.assertEqual(["a", "c", "b"], data["recent_slots"])

    def test_prune_drops_missing_slots(self):
        data = {
            "slots": {"a": {}, "b": {}},
            "recent_slots": ["a", "gone", "b", "gone2"],
        }
        self.mod.prune_recent_slots(data)
        self.assertEqual(["a", "b"], data["recent_slots"])

    def test_ordered_slot_names_current_then_recent_then_alpha(self):
        data = {
            "current": "zeta",
            "recent_slots": ["beta", "alpha", "gone"],
            "slots": {
                "alpha": {"mode": "oauth"},
                "beta": {"mode": "relay"},
                "zeta": {"mode": "oauth"},
                "gamma": {"mode": "relay"},
            },
        }
        self.assertEqual(
            ["zeta", "beta", "alpha", "gamma"],
            self.mod.ordered_slot_names(data),
        )

    def test_format_slot_menu_label_marks_current(self):
        label = self.mod.format_slot_menu_label(
            "babata",
            {
                "mode": "relay",
                "display_name": "Codex - babata",
                "base_url": "https://relay.example/v1",
                "protocol": "responses",
            },
            current="babata",
        )
        self.assertTrue(label.startswith("*"))
        self.assertIn("babata", label)
        self.assertIn("relay.example", label)

    def test_cmd_menu_options_exclude_status(self):
        """Main menu must not expose redundant '当前状态'."""
        source = (ROOT / "sub2cli-inject").read_text(encoding="utf-8")
        # Locate cmd_menu body roughly
        start = source.index("def cmd_menu()")
        end = source.index("\ndef run_internal_command", start)
        body = source[start:end]
        self.assertIn("切换渠道", body)
        self.assertIn("新增 API", body)
        self.assertIn("新增账号", body)
        self.assertNotIn("当前状态", body)
        self.assertIn('("退出', body)


class MenuAddRelayValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        (self.home / ".codex").mkdir()
        self.mod = load_inject(self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_menu_add_relay_rejects_empty_key(self):
        inputs = iter(
            [
                "https://relay.example/v1",  # url
                "",  # slot name default
                "",  # display
                "",  # model
            ]
        )
        with (
            patch.object(self.mod, "prompt_line", side_effect=lambda *a, **k: next(inputs)),
            patch.object(self.mod.getpass, "getpass", return_value=""),
            patch.object(self.mod, "pause"),
            patch.object(self.mod, "clear_screen"),
            patch.object(self.mod, "cmd_relay") as relay,
        ):
            self.mod.menu_add_relay()
        relay.assert_not_called()


if __name__ == "__main__":
    unittest.main()
