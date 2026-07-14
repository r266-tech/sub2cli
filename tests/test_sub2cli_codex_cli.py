import importlib.machinery
import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("sub2cli_cli", str(ROOT / "sub2cli"))
spec = importlib.util.spec_from_loader(loader.name, loader)
sub2cli_cli = importlib.util.module_from_spec(spec)
loader.exec_module(sub2cli_cli)


class CodexCliDispatchTests(unittest.TestCase):
    def run_codex(self, args):
        completed = subprocess.CompletedProcess([], 0)
        with (
            patch.object(sub2cli_cli, "_resolve_inject_bin", return_value="/tmp/sub2cli-inject"),
            patch.object(sub2cli_cli.subprocess, "run", return_value=completed) as run,
        ):
            result = sub2cli_cli.cmd_codex_cli(args)
        self.assertEqual(0, result)
        return run.call_args.args[0]

    def test_bare_codex_command_opens_channel_menu(self):
        self.assertEqual(["/tmp/sub2cli-inject"], self.run_codex([]))

    def test_api_alias_adds_and_switches_single_api(self):
        command = self.run_codex([
            "api",
            "https://api.example/v1",
            "--api-key-stdin",
            "--name",
            "direct",
        ])
        self.assertEqual("add-api", command[1])
        self.assertEqual("https://api.example/v1", command[2])
        self.assertIn("--api-key-stdin", command)

    def test_official_alias_registers_official_account(self):
        command = self.run_codex(["official", "work", "--auth-file", "/tmp/auth.json"])
        self.assertEqual(
            ["/tmp/sub2cli-inject", "add-account", "work", "--auth-file", "/tmp/auth.json"],
            command,
        )

    def test_switch_alias_uses_saved_api_or_official_slot(self):
        self.assertEqual(
            ["/tmp/sub2cli-inject", "use", "work"],
            self.run_codex(["switch", "work"]),
        )

    def test_channels_alias_lists_all_saved_targets(self):
        self.assertEqual(
            ["/tmp/sub2cli-inject", "list"],
            self.run_codex(["channels"]),
        )

    def test_bare_sub2cli_opens_codex_channel_menu_without_relay_login(self):
        with (
            patch.object(sys, "argv", ["sub2cli"]),
            patch.object(sub2cli_cli, "cmd_codex_cli", return_value=0) as codex,
            patch.object(sub2cli_cli, "load_config") as load_config,
        ):
            result = sub2cli_cli.main()

        self.assertEqual(0, result)
        codex.assert_called_once_with([])
        load_config.assert_not_called()


class DesktopConfigTargetMarkupTests(unittest.TestCase):
    def test_config_modal_exposes_single_api_as_peer_target(self):
        html = (ROOT / "desktop" / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="choice-custom"', html)
        self.assertIn('id="choice-custom-meta"', html)
        self.assertIn('id="btn-config-custom"', html)
        self.assertLess(html.index('id="btn-config-pool"'), html.index('id="btn-config-custom"'))
        self.assertLess(html.index('id="btn-config-custom"'), html.index('id="btn-config-official"'))


if __name__ == "__main__":
    unittest.main()
