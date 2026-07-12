from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence
from unittest import mock

from upstream_reconciler.cli import _parser, main
from upstream_reconciler.core import ReconcileError
from upstream_reconciler.maintenance import (
    prepare_maintenance,
    promote_maintenance,
    verify_maintenance,
)
from upstream_reconciler.store import load_json


OBSERVATION = {
    "code": "schema_changed",
    "context": {
        "provider_id": "provider-1",
        "endpoint": "/keys",
        "schema_fingerprint": "a" * 64,
    },
}
NOW = datetime(2026, 7, 12, 12, 34, 56, tzinfo=UTC)


class GateRunner:
    def __init__(self, *, plan: dict[str, Any] | None = None):
        self.plan = plan or {"actions": [], "resources": [{}, {}]}
        self.commands: list[str] = []
        self.reconciler_argv: list[list[str]] = []
        self.git_commands: list[list[str]] = []

    def __call__(
        self,
        args: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(value) for value in args]
        if argv[0] == "git":
            self.git_commands.append(argv)
            return subprocess.run(argv, **kwargs)
        if argv[0] == sys.executable:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if Path(argv[0]).name == "sub2cli-reconcile-upstreams":
            self.reconciler_argv.append(argv)
            command = next(
                value
                for value in ("doctor", "plan", "apply", "status")
                if value in argv[1:]
            )
            self.commands.append(command)
            payload: dict[str, Any]
            if command == "doctor":
                payload = {"ok": True}
            elif command == "plan":
                payload = self.plan
            elif command == "apply":
                payload = {"ok": True, "run_id": "apply-1"}
            else:
                payload = {"pending_recovery": False, "active": 2}
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")
        raise AssertionError(f"unexpected command: {argv!r}")


class CrashAfterSuccessfulPushRunner(GateRunner):
    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    def __call__(
        self,
        args: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        argv = [str(value) for value in args]
        if argv[:2] == ["git", "push"] and not self.crashed:
            self.git_commands.append(argv)
            completed = subprocess.run(argv, **kwargs)
            if completed.returncode != 0:
                return completed
            self.crashed = True
            raise KeyboardInterrupt("simulated process loss after successful push")
        return super().__call__(args, **kwargs)


class PlanMutatingRunner(GateRunner):
    def __call__(
        self,
        args: Sequence[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        result = super().__call__(args, **kwargs)
        argv = [str(value) for value in args]
        if Path(argv[0]).name == "sub2cli-reconcile-upstreams" and "plan" in argv:
            clients = Path(str(kwargs["cwd"])) / "upstream_reconciler" / "clients.py"
            clients.write_text(
                clients.read_text(encoding="utf-8") + "PLAN_MUTATION = True\n",
                encoding="utf-8",
            )
        return result


class UpstreamMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.origin = self.root / "origin.git"
        self.state = self.root / "state"
        self.config = self.root / "private-config.json"
        self.config.write_text('{"version": 1}\n', encoding="utf-8")
        self.repo.mkdir()
        (self.repo / "upstream_reconciler").mkdir()
        (self.repo / "tests").mkdir()
        (self.repo / "upstream_reconciler" / "clients.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        (self.repo / "tests" / "test_upstream_reconciler.py").write_text(
            "import unittest\n\nclass Baseline(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
        )
        self._git(self.repo, "init", "-b", "main")
        self._git(self.repo, "config", "user.name", "Maintenance Test")
        self._git(self.repo, "config", "user.email", "maintenance@example.invalid")
        self._git(self.repo, "add", ".")
        self._git(self.repo, "commit", "-m", "initial")
        subprocess.run(
            ["git", "init", "--bare", str(self.origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        self._git(self.repo, "remote", "add", "origin", str(self.origin))
        self._git(self.repo, "push", "-u", "origin", "main")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _git(self, cwd: Path, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _prepare(
        self,
        observations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        values = iter(observations or [OBSERVATION, OBSERVATION])
        return prepare_maintenance(
            config_path=self.config,
            repo=self.repo,
            state_dir=self.state,
            observer=lambda: next(values),
            status_reader=lambda: {"pending_recovery": False, "active": 2},
            now=NOW,
        )

    def _edit_allowed_repair(self, worktree: Path) -> None:
        clients = worktree / "upstream_reconciler" / "clients.py"
        clients.write_text(
            clients.read_text(encoding="utf-8") + "NEW_SCHEMA_FIELD = 'items'\n",
            encoding="utf-8",
        )
        test = worktree / "tests" / "test_upstream_reconciler.py"
        test.write_text(
            test.read_text(encoding="utf-8")
            + (
                "\nclass SchemaRegression(unittest.TestCase):\n"
                "    def test_new_schema_items(self):\n"
                "        self.assertEqual('items', 'items')\n"
            ),
            encoding="utf-8",
        )

    def _manifest(self, gate_id: str) -> dict[str, Any]:
        return load_json(self.state / "maintenance" / f"{gate_id}.json", {})

    def test_prepare_requires_two_identical_safe_schema_observations(self) -> None:
        mismatch = json.loads(json.dumps(OBSERVATION))
        mismatch["context"]["schema_fingerprint"] = "b" * 64
        with self.assertRaises(ReconcileError) as caught:
            self._prepare([OBSERVATION, mismatch])
        self.assertEqual(caught.exception.code, "maintenance_gate_failed")

        http_error = json.loads(json.dumps(OBSERVATION))
        http_error["context"]["http_status"] = 404
        with self.assertRaises(ReconcileError) as caught:
            self._prepare([http_error, http_error])
        self.assertEqual(caught.exception.code, "maintenance_gate_failed")
        self.assertFalse((self.state / "maintenance-worktrees").exists())

    def test_prepare_defaults_to_the_module_canonical_repo(self) -> None:
        values = iter([OBSERVATION, OBSERVATION])
        module_path = self.repo / "upstream_reconciler" / "maintenance.py"
        with mock.patch(
            "upstream_reconciler.maintenance.__file__", str(module_path)
        ):
            prepared = prepare_maintenance(
                config_path=self.config,
                state_dir=self.state,
                observer=lambda: next(values),
                status_reader=lambda: {
                    "pending_recovery": False,
                    "active": 2,
                },
                now=NOW,
            )
        self.assertEqual(
            Path(prepared["worktree"]).parent,
            (self.state / "maintenance-worktrees").resolve(),
        )
        manifest = self._manifest(prepared["gate_id"])
        self.assertEqual(Path(manifest["repo"]), self.repo.resolve())

    def test_prepare_blocks_while_an_active_gate_needs_recovery(self) -> None:
        directory = self.state / "maintenance"
        directory.mkdir(parents=True)
        (directory / "existing.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "gate_id": "existing",
                    "phase": "push_pending",
                    "provider_id": "provider-1",
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(ReconcileError) as caught:
            self._prepare()
        self.assertEqual(caught.exception.code, "maintenance_gate_failed")
        self.assertEqual(caught.exception.context["gate_id"], "existing")

    def test_verify_rejects_any_non_allowlisted_path(self) -> None:
        prepared = self._prepare()
        worktree = Path(prepared["worktree"])
        self._edit_allowed_repair(worktree)
        (worktree / "README.md").write_text("not allowed\n", encoding="utf-8")

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
            )

        self.assertEqual(
            self._manifest(prepared["gate_id"])["phase"],
            "verify_failed",
        )

    def test_verify_rejects_secret_patterns_before_commands_or_apply(self) -> None:
        prepared = self._prepare()
        worktree = Path(prepared["worktree"])
        self._edit_allowed_repair(worktree)
        clients = worktree / "upstream_reconciler" / "clients.py"
        synthetic_secret = "sk-" + "abcdefghijklmnopqrstuvwxyz"
        clients.write_text(
            clients.read_text(encoding="utf-8")
            + f"LEAK = '{synthetic_secret}'\n",
            encoding="utf-8",
        )
        runner = GateRunner()

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=runner,
            )

        self.assertEqual(runner.commands, [])

    def test_verify_rejects_private_config_drift_before_apply(self) -> None:
        prepared = self._prepare()
        self._edit_allowed_repair(Path(prepared["worktree"]))
        self.config.write_text('{"version": 2}\n', encoding="utf-8")
        runner = GateRunner()

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=runner,
            )

        self.assertEqual(runner.commands, [])
        self.assertEqual(
            self._manifest(prepared["gate_id"])["phase"], "verify_failed"
        )

    def test_verify_requires_a_real_added_regression_assertion(self) -> None:
        prepared = self._prepare()
        worktree = Path(prepared["worktree"])
        clients = worktree / "upstream_reconciler" / "clients.py"
        clients.write_text(
            clients.read_text(encoding="utf-8") + "NEW_SCHEMA_FIELD = 'items'\n",
            encoding="utf-8",
        )
        test = worktree / "tests" / "test_upstream_reconciler.py"
        test.write_text(
            test.read_text(encoding="utf-8")
            + "\nclass EmptyRegression(unittest.TestCase):\n    pass\n",
            encoding="utf-8",
        )
        runner = GateRunner()

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=runner,
            )
        self.assertEqual(runner.commands, [])

    def test_verify_rechecks_content_after_doctor_and_plan(self) -> None:
        prepared = self._prepare()
        self._edit_allowed_repair(Path(prepared["worktree"]))
        runner = PlanMutatingRunner()

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=runner,
            )
        self.assertEqual(runner.commands, ["doctor", "plan"])
        self.assertNotIn("apply", runner.commands)

    def test_verify_rejects_destructive_plan_before_apply(self) -> None:
        prepared = self._prepare()
        self._edit_allowed_repair(Path(prepared["worktree"]))
        runner = GateRunner(
            plan={
                "resources": [{}, {}],
                "actions": [{"kind": "delete_upstream_key"}],
            }
        )

        with self.assertRaises(ReconcileError):
            verify_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=runner,
            )

        self.assertEqual(runner.commands, ["doctor", "plan"])
        self.assertNotIn("apply", runner.commands)
        self.assertEqual(
            self._manifest(prepared["gate_id"])["phase"],
            "verify_failed",
        )

    def test_prepare_verify_promote_runs_one_apply_and_non_force_push(self) -> None:
        prepared = self._prepare()
        worktree = Path(prepared["worktree"])
        self._edit_allowed_repair(worktree)
        runner = GateRunner()

        verified = verify_maintenance(
            prepared["gate_id"],
            config_path=self.config,
            state_dir=self.state,
            runner=runner,
        )
        self.assertEqual(verified["phase"], "verified")
        self.assertEqual(runner.commands, ["doctor", "plan", "apply", "status"])
        self.assertEqual(runner.commands.count("apply"), 1)
        apply_argv = next(argv for argv in runner.reconciler_argv if "apply" in argv)
        self.assertIn("--maintenance-safe", apply_argv)
        floor_index = apply_argv.index("--min-active-resources")
        self.assertEqual(apply_argv[floor_index + 1], "2")

        promoted = promote_maintenance(
            prepared["gate_id"],
            state_dir=self.state,
            runner=runner,
        )
        candidate = verified["candidate_sha"]
        self.assertEqual(promoted["commit"], candidate)
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), candidate)
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(self.origin), "rev-parse", "main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            candidate,
        )
        pushes = [argv for argv in runner.git_commands if argv[1] == "push"]
        self.assertEqual(len(pushes), 1)
        self.assertFalse(any(value.startswith("--force") for value in pushes[0]))
        self.assertEqual(
            self._manifest(prepared["gate_id"])["phase"],
            "promoted",
        )

    def test_promote_resumes_after_process_loss_following_successful_push(self) -> None:
        prepared = self._prepare()
        self._edit_allowed_repair(Path(prepared["worktree"]))
        verify_runner = GateRunner()
        verified = verify_maintenance(
            prepared["gate_id"],
            config_path=self.config,
            state_dir=self.state,
            runner=verify_runner,
        )
        candidate = verified["candidate_sha"]

        crash_runner = CrashAfterSuccessfulPushRunner()
        with self.assertRaises(KeyboardInterrupt):
            promote_maintenance(
                prepared["gate_id"],
                config_path=self.config,
                state_dir=self.state,
                runner=crash_runner,
            )
        self.assertEqual(
            self._manifest(prepared["gate_id"])["phase"], "push_pending"
        )
        manifest = self._manifest(prepared["gate_id"])
        self.assertEqual(
            self._git(self.repo, "rev-parse", "HEAD"), manifest["base_sha"]
        )

        resume_runner = GateRunner()
        promoted = promote_maintenance(
            prepared["gate_id"],
            config_path=self.config,
            state_dir=self.state,
            runner=resume_runner,
        )
        self.assertEqual(promoted["commit"], candidate)
        self.assertEqual(self._git(self.repo, "rev-parse", "HEAD"), candidate)
        self.assertFalse(
            any(argv[1] == "push" for argv in resume_runner.git_commands)
        )

    def test_promote_rejects_origin_race_without_push(self) -> None:
        prepared = self._prepare()
        self._edit_allowed_repair(Path(prepared["worktree"]))
        runner = GateRunner()
        verify_maintenance(
            prepared["gate_id"],
            config_path=self.config,
            state_dir=self.state,
            runner=runner,
        )

        other = self.root / "other"
        subprocess.run(
            ["git", "clone", "--branch", "main", str(self.origin), str(other)],
            check=True,
            capture_output=True,
            text=True,
        )
        self._git(other, "config", "user.name", "Other Writer")
        self._git(other, "config", "user.email", "other@example.invalid")
        (other / "EXTERNAL.md").write_text("moved\n", encoding="utf-8")
        self._git(other, "add", "EXTERNAL.md")
        self._git(other, "commit", "-m", "move origin")
        external_sha = self._git(other, "rev-parse", "HEAD")
        self._git(other, "push", "origin", "main")

        with self.assertRaises(ReconcileError):
            promote_maintenance(
                prepared["gate_id"],
                state_dir=self.state,
                runner=runner,
            )

        pushes = [argv for argv in runner.git_commands if argv[1] == "push"]
        self.assertEqual(pushes, [])
        self.assertEqual(
            subprocess.run(
                ["git", "--git-dir", str(self.origin), "rev-parse", "main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            external_sha,
        )

    def test_cli_exposes_only_explicit_maintenance_phases(self) -> None:
        status = _parser().parse_args(["maintenance", "status"])
        self.assertEqual(status.maintenance_phase, "status")
        self.assertFalse(hasattr(status, "gate_id"))

        parsed = _parser().parse_args(
            ["maintenance", "verify", "--gate-id", "schema-example"]
        )
        self.assertEqual(parsed.command, "maintenance")
        self.assertEqual(parsed.maintenance_phase, "verify")
        self.assertEqual(parsed.gate_id, "schema-example")

        with mock.patch(
            "upstream_reconciler.cli.run_maintenance_phase",
            return_value={"ok": True, "phase": "prepared"},
        ) as run_phase, mock.patch("upstream_reconciler.cli._emit") as emit:
            self.assertEqual(main(["maintenance", "prepare"]), 0)
        run_phase.assert_called_once_with(
            "prepare",
            gate_id=None,
            config_path=None,
            notify_on_failure=False,
        )
        emit.assert_called_once_with({"ok": True, "phase": "prepared"})


if __name__ == "__main__":
    unittest.main()
