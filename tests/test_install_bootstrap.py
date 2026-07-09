import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class InstallBootstrapTests(unittest.TestCase):
    def test_direct_bootstrap_writes_codex_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            home.mkdir()
            codex_home.mkdir()
            (codex_home / "auth.json").write_text('{"old": true}\n')
            (codex_home / "config.toml").write_text('model = "old"\n')

            env = os.environ.copy()
            for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME", "SUB2CLI_CODEX_APP"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_FORCE_DIRECT_CONFIG": "1",
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example///",
                    "SUB2CLI_API_KEY": 'sk-test"quote\\slash',
                    "SUB2CLI_API_MODEL": "gpt-test",
                }
            )

            result = subprocess.run(
                ["sh", str(ROOT / "install.sh")],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertEqual(
                0,
                result.returncode,
                msg=f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}",
            )
            self.assertTrue((dest / "sub2cli").exists())
            self.assertTrue((dest / "sub2cli-inject").exists())

            auth = json.loads((codex_home / "auth.json").read_text())
            self.assertEqual(
                {"OPENAI_API_KEY": 'sk-test"quote\\slash', "auth_mode": "apikey"},
                auth,
            )

            config = (codex_home / "config.toml").read_text()
            self.assertIn('model = "gpt-test"', config)
            self.assertIn('model_provider = "OpenAI"', config)
            self.assertIn('base_url = "https://relay.example/v1"', config)
            self.assertIn('wire_api = "responses"', config)

            backups = list((codex_home / "provider-switch-backups").glob("install-api-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual('{"old": true}\n', (backups[0] / "auth.json").read_text())
            self.assertEqual('model = "old"\n', (backups[0] / "config.toml").read_text())


if __name__ == "__main__":
    unittest.main()
