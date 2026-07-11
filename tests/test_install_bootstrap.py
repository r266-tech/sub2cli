import json
import os
from pathlib import Path
import select
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest

try:
    import fcntl
    import pty
    import termios
except ImportError:  # Windows CI imports this module before applying -k filters.
    fcntl = None
    pty = None
    termios = None


ROOT = Path(__file__).resolve().parents[1]


class InstallBootstrapTests(unittest.TestCase):
    def test_readme_keeps_stable_main_bootstrap_contract(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        shell_script = (ROOT / "install.sh").read_text(encoding="utf-8")
        start_marker = "<!-- stable-bootstrap-contract:start -->"
        end_marker = "<!-- stable-bootstrap-contract:end -->"

        self.assertEqual(1, readme.count(start_marker))
        self.assertEqual(1, readme.count(end_marker))
        contract = readme.split(start_marker, 1)[1].split(end_marker, 1)[0]

        self.assertIn(
            "https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.sh",
            contract,
        )
        self.assertIn(
            "https://raw.githubusercontent.com/r266-tech/sub2cli/main/install.ps1",
            contract,
        )
        self.assertIn("SUB2CLI_API_URL", contract)
        self.assertIn("SUB2CLI_API_KEY", contract)
        self.assertNotRegex(contract, r"/v\d+\.\d+\.\d+/install\.(?:sh|ps1)")
        self.assertNotIn("SUB2CLI_REF=", contract)
        self.assertIn('REF="${SUB2CLI_REF:-main}"', shell_script)

    def test_installers_default_to_current_sol_model(self):
        shell_script = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell_script = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn('SUB2CLI_API_MODEL:-gpt-5.6-sol', shell_script)
        self.assertIn('$DefaultModel = "gpt-5.6-sol"', powershell_script)
        self.assertIn('SUB2CLI_CHATGPT_APP', shell_script)
        self.assertLess(
            shell_script.index('"/Applications/ChatGPT.app"'),
            shell_script.index('"/Applications/Codex.app"'),
        )

    def test_default_bootstrap_writes_apikey_config_even_with_python310(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            home.mkdir()
            codex_home.mkdir()
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
                "echo 'sub2cli-inject unexpectedly invoked' >&2\n"
                "exit 91\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            env = os.environ.copy()
            for key in (
                "CODEX_HOME",
                "CODEX_PROVIDER_HOME",
                "SUB2CLI_API_SKIP_CHECK",
                "SUB2CLI_CODEX_APP",
                "SUB2CLI_FORCE_DIRECT_CONFIG",
            ):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
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
            self.assertIn("mode: direct ~/.codex config", result.stdout)
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
            self.assertIn('requires_openai_auth = true', config)

            backups = list((codex_home / "provider-switch-backups").glob("install-api-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(0o700, stat.S_IMODE(backups[0].stat().st_mode))
            self.assertEqual([], list(backups[0].iterdir()))

    def test_direct_bootstrap_refuses_existing_config_or_pool_state(self):
        cases = {
            "empty-config": {
                "config.toml": b"",
            },
            "nonempty-config": {
                "config.toml": b'model = "keep"\n[mcp_servers.keep]\ncommand = "keep"\n',
            },
            "provider-slots": {
                "provider-slots.json": b'{"current":"keep","route_pools":{"keep":{}}}\n',
            },
            "empty-provider-slots": {
                "provider-slots.json": b"",
            },
            "malformed-provider-slots": {
                "provider-slots.json": b"{not-json\n",
            },
        }

        for case_name, state_files in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                home = tmp_root / "home"
                dest = tmp_root / "bin"
                codex_home = home / ".codex"
                codex_home.mkdir(parents=True)
                auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
                (codex_home / "auth.json").write_bytes(auth_before)
                for relative_path, content in state_files.items():
                    (codex_home / relative_path).write_bytes(content)

                env = os.environ.copy()
                for key in (
                    "CODEX_HOME",
                    "CODEX_PROVIDER_HOME",
                    "SUB2CLI_API_MODEL",
                    "SUB2CLI_CODEX_APP",
                    "SUB2CLI_FORCE_DIRECT_CONFIG",
                ):
                    env.pop(key, None)
                env.update(
                    {
                        "HOME": str(home),
                        "SUB2CLI_INSTALL_DIR": str(dest),
                        "SUB2CLI_API_NO_RESTART": "1",
                        "SUB2CLI_API_URL": "https://relay.example",
                        "SUB2CLI_API_KEY": "sk-test",
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

                self.assertNotEqual(0, result.returncode)
                self.assertIn("sub2cli-inject", result.stderr)
                self.assertIn("add-api", result.stderr)
                self.assertEqual(auth_before, (codex_home / "auth.json").read_bytes())
                for relative_path, content in state_files.items():
                    self.assertEqual(content, (codex_home / relative_path).read_bytes())
                if "config.toml" not in state_files:
                    self.assertFalse((codex_home / "config.toml").exists())
                self.assertFalse((codex_home / "provider-switch-backups").exists())

    def test_direct_bootstrap_stops_before_overwrite_when_backup_step_fails(self):
        for failing_command in ("cp", "chmod"):
            with self.subTest(command=failing_command), tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                home = tmp_root / "home"
                dest = tmp_root / "bin"
                codex_home = home / ".codex"
                fake_bin = tmp_root / "fake-bin"
                codex_home.mkdir(parents=True)
                fake_bin.mkdir()
                auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
                auth_json = codex_home / "auth.json"
                auth_json.write_bytes(auth_before)
                fake_command = fake_bin / failing_command
                if failing_command == "cp":
                    fail_condition = (
                        'if [ "${1:-}" = "$SUB2CLI_TEST_FAIL_COPY_SOURCE" ]; then exit 73; fi\n'
                    )
                else:
                    fail_condition = 'if [ "${1:-}" = "600" ]; then exit 73; fi\n'
                fake_command.write_text(
                    "#!/bin/sh\n"
                    + fail_condition
                    + 'exec "$SUB2CLI_TEST_REAL_COMMAND" "$@"\n',
                    encoding="utf-8",
                )
                fake_command.chmod(0o755)

                env = os.environ.copy()
                for key in (
                    "CODEX_HOME",
                    "CODEX_PROVIDER_HOME",
                    "SUB2CLI_API_MODEL",
                    "SUB2CLI_CODEX_APP",
                    "SUB2CLI_FORCE_DIRECT_CONFIG",
                ):
                    env.pop(key, None)
                env.update(
                    {
                        "HOME": str(home),
                        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                        "SUB2CLI_INSTALL_DIR": str(dest),
                        "SUB2CLI_API_NO_RESTART": "1",
                        "SUB2CLI_API_URL": "https://relay.example",
                        "SUB2CLI_API_KEY": "sk-test",
                        "SUB2CLI_TEST_FAIL_COPY_SOURCE": str(auth_json),
                        "SUB2CLI_TEST_REAL_COMMAND": shutil.which(failing_command)
                        or f"/bin/{failing_command}",
                    }
                )

                result = subprocess.run(
                    ["sh", str(ROOT / "install.sh")],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    timeout=20,
                )

                self.assertNotEqual(0, result.returncode)
                self.assertIn(b"backup", result.stderr.lower())
                self.assertEqual(auth_before, auth_json.read_bytes())
                self.assertFalse((codex_home / "config.toml").exists())

    def test_direct_bootstrap_config_race_preserves_concurrent_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            codex_home.mkdir(parents=True)
            fake_bin.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            auth_json = codex_home / "auth.json"
            config_toml = codex_home / "config.toml"
            auth_json.write_bytes(auth_before)

            fake_ln = fake_bin / "ln"
            fake_ln.write_text(
                "#!/bin/sh\n"
                'if [ "${2:-}" = "$SUB2CLI_TEST_CONFIG" ]; then\n'
                "  /bin/cp \"$SUB2CLI_TEST_RACE_SOURCE\" \"$SUB2CLI_TEST_CONFIG\"\n"
                "  exit 73\n"
                "fi\n"
                'exec "$SUB2CLI_TEST_REAL_LN" "$@"\n',
                encoding="utf-8",
            )
            fake_ln.chmod(0o755)
            race_source = tmp_root / "concurrent-config.toml"
            config_before = b'model = "concurrent-owner"\n'
            race_source.write_bytes(config_before)

            env = os.environ.copy()
            for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_CONFIG": str(config_toml),
                    "SUB2CLI_TEST_RACE_SOURCE": str(race_source),
                    "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

            self.assertNotEqual(0, result.returncode)
            self.assertIn("appeared concurrently", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertEqual(config_before, config_toml.read_bytes())
            self.assertEqual([], list(codex_home.glob("*.stage.*")))
            self.assertEqual([], list(codex_home.glob("*.commit.*")))

    def test_direct_bootstrap_auth_commit_failure_rolls_back_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            codex_home.mkdir(parents=True)
            fake_bin.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            auth_json = codex_home / "auth.json"
            auth_json.write_bytes(auth_before)

            fake_ln = fake_bin / "ln"
            fake_ln.write_text(
                "#!/bin/sh\n"
                'if [ "${2:-}" = "$SUB2CLI_TEST_AUTH" ]; then exit 73; fi\n'
                'exec "$SUB2CLI_TEST_REAL_LN" "$@"\n',
                encoding="utf-8",
            )
            fake_ln.chmod(0o755)

            env = os.environ.copy()
            for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_AUTH": str(auth_json),
                    "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

            self.assertNotEqual(0, result.returncode)
            self.assertIn("auth.json appeared concurrently", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertEqual([], list(codex_home.glob("*.stage.*")))
            self.assertEqual([], list(codex_home.glob("*.commit.*")))

    def test_direct_bootstrap_post_commit_pool_race_restores_auth_and_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            codex_home.mkdir(parents=True)
            fake_bin.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            auth_json = codex_home / "auth.json"
            slots_json = codex_home / "provider-slots.json"
            auth_json.write_bytes(auth_before)

            fake_ln = fake_bin / "ln"
            fake_ln.write_text(
                "#!/bin/sh\n"
                '"$SUB2CLI_TEST_REAL_LN" "$@" || exit $?\n'
                'if [ "${2:-}" = "$SUB2CLI_TEST_AUTH" ]; then\n'
                '  /bin/cp "$SUB2CLI_TEST_POOL_SOURCE" "$SUB2CLI_TEST_SLOTS"\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_ln.chmod(0o755)
            pool_source = tmp_root / "concurrent-provider-slots.json"
            pool_before = b'{"current":"concurrent-owner"}\n'
            pool_source.write_bytes(pool_before)

            env = os.environ.copy()
            for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_AUTH": str(auth_json),
                    "SUB2CLI_TEST_SLOTS": str(slots_json),
                    "SUB2CLI_TEST_POOL_SOURCE": str(pool_source),
                    "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

            self.assertNotEqual(0, result.returncode)
            self.assertIn("was rolled back", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertEqual(pool_before, slots_json.read_bytes())
            self.assertEqual([], list(codex_home.glob("*.stage.*")))
            self.assertEqual([], list(codex_home.glob("*.commit.*")))
            self.assertEqual([], list(codex_home.glob("*.rollback.*")))

    def test_direct_bootstrap_preserves_auth_refreshed_after_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            codex_home.mkdir(parents=True)
            fake_bin.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}\n'
            auth_concurrent = b'{"auth_mode":"chatgpt","tokens":{"access_token":"refreshed"}}\n'
            auth_json = codex_home / "auth.json"
            config_toml = codex_home / "config.toml"
            auth_json.write_bytes(auth_before)
            concurrent_source = tmp_root / "concurrent-auth.json"
            concurrent_source.write_bytes(auth_concurrent)

            fake_ln = fake_bin / "ln"
            fake_ln.write_text(
                "#!/bin/sh\n"
                '"$SUB2CLI_TEST_REAL_LN" "$@" || exit $?\n'
                'if [ "${2:-}" = "$SUB2CLI_TEST_CONFIG" ]; then\n'
                '  /bin/cp "$SUB2CLI_TEST_AUTH_SOURCE" "$SUB2CLI_TEST_AUTH"\n'
                "fi\n",
                encoding="utf-8",
            )
            fake_ln.chmod(0o755)

            env = os.environ.copy()
            for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_AUTH": str(auth_json),
                    "SUB2CLI_TEST_AUTH_SOURCE": str(concurrent_source),
                    "SUB2CLI_TEST_CONFIG": str(config_toml),
                    "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

            self.assertNotEqual(0, result.returncode)
            self.assertIn("auth.json changed concurrently", result.stderr)
            self.assertEqual(auth_concurrent, auth_json.read_bytes())
            self.assertFalse(config_toml.exists())
            self.assertEqual([], list(codex_home.glob("*.stage.*")))
            self.assertEqual([], list(codex_home.glob("*.original.*")))

    def test_direct_bootstrap_preserves_in_place_config_or_auth_race(self):
        for target_name in ("config", "auth"):
            with self.subTest(target=target_name), tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                home = tmp_root / "home"
                dest = tmp_root / "bin"
                codex_home = home / ".codex"
                fake_bin = tmp_root / "fake-bin"
                codex_home.mkdir(parents=True)
                fake_bin.mkdir()
                auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"old"}}\n'
                auth_concurrent = b'{"auth_mode":"chatgpt","tokens":{"access_token":"concurrent"}}\n'
                config_concurrent = b'model = "concurrent-owner"\n'
                auth_json = codex_home / "auth.json"
                config_toml = codex_home / "config.toml"
                auth_json.write_bytes(auth_before)
                race_source = tmp_root / "concurrent-state"
                race_source.write_bytes(
                    config_concurrent if target_name == "config" else auth_concurrent
                )
                race_target = config_toml if target_name == "config" else auth_json

                fake_ln = fake_bin / "ln"
                fake_ln.write_text(
                    "#!/bin/sh\n"
                    '"$SUB2CLI_TEST_REAL_LN" "$@" || exit $?\n'
                    'if [ "${2:-}" = "$SUB2CLI_TEST_RACE_TARGET" ]; then\n'
                    '  /bin/cp "$SUB2CLI_TEST_RACE_SOURCE" "$SUB2CLI_TEST_RACE_TARGET"\n'
                    "fi\n",
                    encoding="utf-8",
                )
                fake_ln.chmod(0o755)

                env = os.environ.copy()
                for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                    env.pop(key, None)
                env.update(
                    {
                        "HOME": str(home),
                        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                        "SUB2CLI_INSTALL_DIR": str(dest),
                        "SUB2CLI_API_NO_RESTART": "1",
                        "SUB2CLI_API_URL": "https://relay.example",
                        "SUB2CLI_API_KEY": "sk-test",
                        "SUB2CLI_TEST_RACE_TARGET": str(race_target),
                        "SUB2CLI_TEST_RACE_SOURCE": str(race_source),
                        "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

                self.assertNotEqual(0, result.returncode)
                if target_name == "config":
                    self.assertEqual(config_concurrent, config_toml.read_bytes())
                    self.assertEqual(auth_before, auth_json.read_bytes())
                else:
                    self.assertEqual(auth_concurrent, auth_json.read_bytes())
                    self.assertFalse(config_toml.exists())
                self.assertEqual([], list(codex_home.rglob("*.stage.*")))
                self.assertEqual([], list(codex_home.rglob("*.commit.*")))
                self.assertEqual([], list(codex_home.rglob("auth.original")))

    def test_direct_bootstrap_signal_after_auth_commit_rolls_back_pair(self):
        for signal_name in ("TERM", "INT"):
            with self.subTest(signal=signal_name), tempfile.TemporaryDirectory() as tmp:
                tmp_root = Path(tmp)
                home = tmp_root / "home"
                dest = tmp_root / "bin"
                codex_home = home / ".codex"
                fake_bin = tmp_root / "fake-bin"
                codex_home.mkdir(parents=True)
                fake_bin.mkdir()
                auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
                auth_json = codex_home / "auth.json"
                config_toml = codex_home / "config.toml"
                auth_json.write_bytes(auth_before)

                fake_ln = fake_bin / "ln"
                fake_ln.write_text(
                    "#!/bin/sh\n"
                    '"$SUB2CLI_TEST_REAL_LN" "$@" || exit $?\n'
                    'if [ "${2:-}" = "$SUB2CLI_TEST_AUTH" ]; then\n'
                    '  kill -"$SUB2CLI_TEST_SIGNAL" "$PPID"\n'
                    "fi\n",
                    encoding="utf-8",
                )
                fake_ln.chmod(0o755)

                env = os.environ.copy()
                for key in ("CODEX_HOME", "CODEX_PROVIDER_HOME"):
                    env.pop(key, None)
                env.update(
                    {
                        "HOME": str(home),
                        "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                        "SUB2CLI_INSTALL_DIR": str(dest),
                        "SUB2CLI_API_NO_RESTART": "1",
                        "SUB2CLI_API_URL": "https://relay.example",
                        "SUB2CLI_API_KEY": "sk-test",
                        "SUB2CLI_TEST_AUTH": str(auth_json),
                        "SUB2CLI_TEST_SIGNAL": signal_name,
                        "SUB2CLI_TEST_REAL_LN": shutil.which("ln") or "/bin/ln",
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

                self.assertNotEqual(0, result.returncode)
                self.assertEqual(auth_before, auth_json.read_bytes())
                self.assertFalse(config_toml.exists())
                self.assertEqual([], list(codex_home.glob("*.stage.*")))
                self.assertEqual([], list(codex_home.glob("*.original.*")))

    @unittest.skipUnless(
        os.name == "posix" and fcntl is not None and pty is not None and termios is not None,
        "requires a POSIX controlling terminal",
    )
    def test_interactive_key_prompt_sigint_restores_terminal_echo(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            home.mkdir()
            master_fd, slave_fd = pty.openpty()
            original_attrs = termios.tcgetattr(master_fd)

            def attach_controlling_tty():
                os.setsid()
                fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

            env = os.environ.copy()
            for key in (
                "CODEX_HOME",
                "CODEX_PROVIDER_HOME",
                "SUB2CLI_API_KEY",
            ):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                }
            )

            proc = subprocess.Popen(
                ["sh", str(ROOT / "install.sh")],
                cwd=ROOT,
                env=env,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                preexec_fn=attach_controlling_tty,
                close_fds=True,
            )
            output = bytearray()
            try:
                deadline = time.time() + 10
                while b"API key (hidden):" not in output and time.time() < deadline:
                    ready, _, _ = select.select([master_fd], [], [], 0.2)
                    if ready:
                        output.extend(os.read(master_fd, 4096))
                self.assertIn(b"API key (hidden):", output)
                echo_deadline = time.time() + 2
                hidden_attrs = termios.tcgetattr(master_fd)
                while hidden_attrs[3] & termios.ECHO and time.time() < echo_deadline:
                    time.sleep(0.02)
                    hidden_attrs = termios.tcgetattr(master_fd)
                self.assertFalse(hidden_attrs[3] & termios.ECHO)

                os.killpg(proc.pid, signal.SIGINT)
                returncode = proc.wait(timeout=10)
                self.assertNotEqual(0, returncode)

                restored_attrs = termios.tcgetattr(master_fd)
                self.assertEqual(
                    bool(original_attrs[3] & termios.ECHO),
                    bool(restored_attrs[3] & termios.ECHO),
                )
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                os.close(master_fd)
                os.close(slave_fd)

    def test_install_without_api_url_preserves_existing_codex_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            codex_home.mkdir(parents=True)
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            config_before = b'model = "keep"\n'
            (codex_home / "auth.json").write_bytes(auth_before)
            (codex_home / "config.toml").write_bytes(config_before)

            env = os.environ.copy()
            for key in (
                "CODEX_HOME",
                "CODEX_PROVIDER_HOME",
                "SUB2CLI_API_KEY",
                "SUB2CLI_API_MODEL",
                "SUB2CLI_API_URL",
            ):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "SUB2CLI_INSTALL_DIR": str(dest),
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
            self.assertEqual(auth_before, (codex_home / "auth.json").read_bytes())
            self.assertEqual(config_before, (codex_home / "config.toml").read_bytes())
            self.assertFalse((codex_home / "provider-switch-backups").exists())

    def test_direct_bootstrap_restarts_chatgpt_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            home = tmp_root / "home"
            dest = tmp_root / "bin"
            codex_home = home / ".codex"
            fake_bin = tmp_root / "fake-bin"
            chatgpt_app = home / "Applications" / "ChatGPT.app"
            trace = tmp_root / "commands.log"
            codex_home.mkdir(parents=True)
            fake_bin.mkdir()
            chatgpt_app.mkdir(parents=True)

            for command in ("osascript", "open", "sleep"):
                executable = fake_bin / command
                executable.write_text(
                    "#!/bin/sh\n"
                    f"printf '{command}' >> \"$SUB2CLI_TEST_TRACE\"\n"
                    "for arg in \"$@\"; do printf '\\t%s' \"$arg\" >> \"$SUB2CLI_TEST_TRACE\"; done\n"
                    "printf '\\n' >> \"$SUB2CLI_TEST_TRACE\"\n",
                    encoding="utf-8",
                )
                executable.chmod(0o755)

            env = os.environ.copy()
            for key in (
                "CODEX_HOME",
                "CODEX_PROVIDER_HOME",
                "SUB2CLI_API_NO_RESTART",
                "SUB2CLI_CODEX_APP",
                "SUB2CLI_FORCE_DIRECT_CONFIG",
                "SUB2CLI_NO_RESTART",
            ):
                env.pop(key, None)
            env.update(
                {
                    "HOME": str(home),
                    "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
                    "SUB2CLI_INSTALL_DIR": str(dest),
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_CODEX_APP": str(chatgpt_app),
                    "SUB2CLI_TEST_TRACE": str(trace),
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
            self.assertEqual(
                [
                    'osascript\t-e\ttell application id "com.openai.codex" to quit',
                    "sleep\t1",
                    f"open\t{chatgpt_app}",
                ],
                trace.read_text(encoding="utf-8").splitlines(),
            )

    def test_windows_powershell_bootstrap_writes_codex_config(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            codex_home = tmp_root / ".codex"
            codex_home.mkdir()
            auth_before = '{"old": true}\n'
            (codex_home / "auth.json").write_text(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example///",
                    "SUB2CLI_API_KEY": 'sk-test"quote\\slash',
                    "SUB2CLI_API_MODEL": "gpt-test",
                }
            )

            command = [
                shell,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "install.ps1"),
            ]
            result = subprocess.run(
                command,
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
            self.assertIn('requires_openai_auth = true', config)

            backups = list((codex_home / "provider-switch-backups").glob("install-api-*"))
            self.assertEqual(1, len(backups))
            self.assertEqual(auth_before, (backups[0] / "auth.json").read_text())
            self.assertFalse((backups[0] / "config.toml").exists())

    def test_windows_restart_failure_keeps_successful_configuration(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_root = Path(tmp)
            codex_home = tmp_root / ".codex"
            bad_app = tmp_root / "ChatGPT.exe"
            codex_home.mkdir()
            bad_app.write_text("not executable", encoding="utf-8")
            bad_app.chmod(0o600)

            env = os.environ.copy()
            for key in ("SUB2CLI_API_NO_RESTART", "SUB2CLI_NO_RESTART"):
                env.pop(key, None)
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_CHATGPT_APP": str(bad_app),
                }
            )
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ROOT / "install.ps1"),
                ],
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
            self.assertIn("ChatGPT API configured", result.stdout)
            self.assertTrue((codex_home / "auth.json").exists())
            self.assertTrue((codex_home / "config.toml").exists())

    def test_windows_bootstrap_refuses_existing_config_or_pool_state(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        cases = {
            "empty-config": {
                "config.toml": b"",
            },
            "nonempty-config": {
                "config.toml": b'model = "keep"\n[mcp_servers.keep]\ncommand = "keep"\n',
            },
            "provider-slots": {
                "provider-slots.json": b'{"current":"keep","route_pools":{"keep":{}}}\n',
            },
            "empty-provider-slots": {
                "provider-slots.json": b"",
            },
            "malformed-provider-slots": {
                "provider-slots.json": b"{not-json\n",
            },
        }
        for case_name, state_files in cases.items():
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as tmp:
                codex_home = Path(tmp) / ".codex"
                codex_home.mkdir()
                auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
                (codex_home / "auth.json").write_bytes(auth_before)
                for relative_path, content in state_files.items():
                    (codex_home / relative_path).write_bytes(content)

                env = os.environ.copy()
                env.update(
                    {
                        "CODEX_HOME": str(codex_home),
                        "SUB2CLI_API_NO_RESTART": "1",
                        "SUB2CLI_API_URL": "https://relay.example",
                        "SUB2CLI_API_KEY": "sk-test",
                    }
                )
                result = subprocess.run(
                    [
                        shell,
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ROOT / "install.ps1"),
                    ],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=20,
                )

                self.assertNotEqual(0, result.returncode)
                self.assertIn("Open ChatGPT Settings", result.stderr)
                self.assertEqual(auth_before, (codex_home / "auth.json").read_bytes())
                for relative_path, content in state_files.items():
                    self.assertEqual(content, (codex_home / relative_path).read_bytes())
                if "config.toml" not in state_files:
                    self.assertFalse((codex_home / "config.toml").exists())
                self.assertFalse((codex_home / "provider-switch-backups").exists())

    def test_windows_bootstrap_backup_copy_failure_preserves_live_files(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            (codex_home / "auth.json").write_bytes(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                }
            )
            result = subprocess.run(
                [
                    shell,
                    "-NoProfile",
                    "-Command",
                    'function global:Copy-Item { throw "injected backup copy failure" }; '
                    ". $env:SUB2CLI_TEST_INSTALL_SCRIPT",
                ],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("injected backup copy failure", result.stderr)
            self.assertEqual(auth_before, (codex_home / "auth.json").read_bytes())
            self.assertFalse((codex_home / "config.toml").exists())

    def test_windows_bootstrap_rechecks_after_interactive_key_window(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            config_toml = codex_home / "config.toml"
            config_before = b'model = "created-during-key-read"'

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                    "SUB2CLI_TEST_RACE_CONFIG": str(config_toml),
                }
            )
            wrapper = (
                "$script:Sub2CliGetItemCalls = 0; "
                "function global:Get-Item { "
                "[CmdletBinding()] param([string]$LiteralPath, [switch]$Force); "
                "$script:Sub2CliGetItemCalls += 1; "
                "try { Microsoft.PowerShell.Management\\Get-Item "
                "-LiteralPath $LiteralPath -Force:$Force -ErrorAction Stop } "
                "finally { if ($script:Sub2CliGetItemCalls -eq 2) { "
                "[IO.File]::WriteAllBytes($env:SUB2CLI_TEST_RACE_CONFIG, "
                "[Text.Encoding]::UTF8.GetBytes('model = \"created-during-key-read\"')) "
                "} } }; . $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("Existing config.toml", result.stderr)
            self.assertEqual(config_before, config_toml.read_bytes())
            self.assertFalse((codex_home / "auth.json").exists())
            self.assertFalse((codex_home / "provider-switch-backups").exists())

    def test_windows_bootstrap_config_commit_race_preserves_concurrent_config(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            config_concurrent = (
                f'model = "concurrent-writer"{os.linesep}'.encode("utf-8")
            )
            auth_json = codex_home / "auth.json"
            config_toml = codex_home / "config.toml"
            auth_json.write_bytes(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                    "SUB2CLI_TEST_RACE_CONFIG": str(config_toml),
                }
            )
            wrapper = (
                "$script:Injected = $false; "
                "function global:Move-Item { [CmdletBinding()] param("
                "[string]$LiteralPath,[string]$Destination,[switch]$Force); "
                "if (-not $script:Injected -and "
                "$Destination -eq $env:SUB2CLI_TEST_RACE_CONFIG) { "
                "$script:Injected = $true; "
                "[IO.File]::WriteAllText($Destination, "
                "'model = \"concurrent-writer\"' + [Environment]::NewLine, "
                "[Text.UTF8Encoding]::new($false)) }; "
                "Microsoft.PowerShell.Management\\Move-Item @PSBoundParameters }; "
                ". $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertEqual(config_concurrent, config_toml.read_bytes())
            self.assertFalse((codex_home / "provider-slots.json").exists())
            self.assertEqual([], list(codex_home.glob("*.stage")))
            self.assertEqual([], list(codex_home.glob("*.original")))
            self.assertEqual([], list(codex_home.glob("*.rollback")))

    def test_windows_bootstrap_auth_commit_failure_rolls_back_config(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            auth_json = codex_home / "auth.json"
            auth_json.write_bytes(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                    "SUB2CLI_TEST_AUTH_PATH": str(auth_json),
                }
            )
            wrapper = (
                "$script:Injected = $false; "
                "function global:Move-Item { [CmdletBinding()] param("
                "[string]$LiteralPath,[string]$Destination,[switch]$Force); "
                "if (-not $script:Injected -and "
                "$Destination -eq $env:SUB2CLI_TEST_AUTH_PATH -and "
                "$LiteralPath -like '*.stage') { "
                "$script:Injected = $true; throw 'injected auth commit failure' }; "
                "Microsoft.PowerShell.Management\\Move-Item @PSBoundParameters }; "
                ". $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("injected auth commit failure", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse((codex_home / "provider-slots.json").exists())
            self.assertEqual([], list(codex_home.glob("*.stage")))
            self.assertEqual([], list(codex_home.glob("*.original")))
            self.assertEqual([], list(codex_home.glob("*.rollback")))

    def test_windows_bootstrap_post_commit_pool_race_rolls_back_only_own_files(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            pool_concurrent = (
                f'{{"current":"concurrent","route_pools":{{}}}}{os.linesep}'.encode("utf-8")
            )
            auth_json = codex_home / "auth.json"
            slots_json = codex_home / "provider-slots.json"
            auth_json.write_bytes(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                    "SUB2CLI_TEST_AUTH_PATH": str(auth_json),
                    "SUB2CLI_TEST_POOL_PATH": str(slots_json),
                }
            )
            wrapper = (
                "$script:Injected = $false; "
                "function global:Move-Item { [CmdletBinding()] param("
                "[string]$LiteralPath,[string]$Destination,[switch]$Force); "
                "Microsoft.PowerShell.Management\\Move-Item @PSBoundParameters; "
                "if (-not $script:Injected -and "
                "$Destination -eq $env:SUB2CLI_TEST_AUTH_PATH -and "
                "$LiteralPath -like '*.stage') { "
                "$script:Injected = $true; "
                "[IO.File]::WriteAllText($env:SUB2CLI_TEST_POOL_PATH, "
                "'{\"current\":\"concurrent\",\"route_pools\":{}}' + "
                "[Environment]::NewLine, [Text.UTF8Encoding]::new($false)) } }; "
                ". $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("provider-slots.json", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertEqual(pool_concurrent, slots_json.read_bytes())
            self.assertEqual([], list(codex_home.glob("*.stage")))
            self.assertEqual([], list(codex_home.glob("*.original")))
            self.assertEqual([], list(codex_home.glob("*.rollback")))

    def test_windows_bootstrap_post_commit_config_change_is_preserved(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            auth_before = b'{"auth_mode":"chatgpt","tokens":{"access_token":"keep"}}\n'
            config_concurrent = (
                f'model = "concurrent-after-auth"{os.linesep}'.encode("utf-8")
            )
            auth_json = codex_home / "auth.json"
            config_toml = codex_home / "config.toml"
            auth_json.write_bytes(auth_before)

            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                    "SUB2CLI_TEST_AUTH_PATH": str(auth_json),
                    "SUB2CLI_TEST_CONFIG_PATH": str(config_toml),
                }
            )
            wrapper = (
                "$script:Injected = $false; "
                "function global:Move-Item { [CmdletBinding()] param("
                "[string]$LiteralPath,[string]$Destination,[switch]$Force); "
                "Microsoft.PowerShell.Management\\Move-Item @PSBoundParameters; "
                "if (-not $script:Injected -and "
                "$Destination -eq $env:SUB2CLI_TEST_AUTH_PATH -and "
                "$LiteralPath -like '*.stage') { "
                "$script:Injected = $true; "
                "[IO.File]::WriteAllText($env:SUB2CLI_TEST_CONFIG_PATH, "
                "'model = \"concurrent-after-auth\"' + [Environment]::NewLine, "
                "[Text.UTF8Encoding]::new($false)) } }; "
                ". $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("config.toml changed", result.stderr)
            self.assertEqual(auth_before, auth_json.read_bytes())
            self.assertEqual(config_concurrent, config_toml.read_bytes())
            self.assertFalse((codex_home / "provider-slots.json").exists())
            self.assertEqual([], list(codex_home.glob("*.stage")))
            self.assertEqual([], list(codex_home.glob("*.original")))
            self.assertEqual([], list(codex_home.glob("*.rollback")))

    def test_windows_bootstrap_fails_closed_when_state_check_errors(self):
        shell = shutil.which("pwsh") or shutil.which("powershell")
        if not shell:
            self.skipTest("PowerShell is not installed")

        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            env = os.environ.copy()
            env.update(
                {
                    "CODEX_HOME": str(codex_home),
                    "SUB2CLI_API_NO_RESTART": "1",
                    "SUB2CLI_API_URL": "https://relay.example",
                    "SUB2CLI_API_KEY": "sk-test",
                    "SUB2CLI_TEST_INSTALL_SCRIPT": str(ROOT / "install.ps1"),
                }
            )
            wrapper = (
                "function global:Get-Item { [CmdletBinding()] "
                "param([string]$LiteralPath, [switch]$Force); "
                'Write-Error "injected Get-Item I/O failure" }; '
                ". $env:SUB2CLI_TEST_INSTALL_SCRIPT"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", wrapper],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertIn("injected Get-Item I/O failure", result.stderr)
            self.assertFalse((codex_home / "auth.json").exists())
            self.assertFalse((codex_home / "config.toml").exists())
            self.assertFalse((codex_home / "provider-switch-backups").exists())

    def test_windows_bootstrap_targets_chatgpt_and_legacy_codex(self):
        script = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn('Get-Process -Name "ChatGPT", "Codex"', script)
        self.assertIn('Where-Object { $_.Name -in @("ChatGPT", "Codex") }', script)
        self.assertIn('"shell:AppsFolder\\$($StartApp.AppID)"', script)
        self.assertIn('requires_openai_auth = true', script)
        self.assertIn('provider-slots.json', script)
        self.assertIn(
            'Move-Item -LiteralPath $ConfigStage -Destination $ConfigToml', script
        )
        self.assertNotIn(
            'Move-Item -LiteralPath $ConfigStage -Destination $ConfigToml -Force',
            script,
        )
        self.assertIn('throw $FailureException', script)
        self.assertNotIn('exit $ExitCode', script)


if __name__ == "__main__":
    unittest.main()
