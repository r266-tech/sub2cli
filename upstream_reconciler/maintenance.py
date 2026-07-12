from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from .core import ReconcileError
from .store import (
    atomic_write_json,
    default_config_path,
    default_state_dir,
    ensure_private_dir,
    load_json,
)


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Observer = Callable[[], dict[str, Any]]
StatusReader = Callable[[], dict[str, Any]]

SAFE_SCHEMA_ENDPOINTS = {
    "/groups/available",
    "/subscriptions/active",
    "/groups/rates",
    "/groups/available + /subscriptions/active + /groups/rates",
    "/keys",
    "/api/token/",
    "/api/user/self/groups",
}
ALLOWED_REPAIR_FILES = {
    "upstream_reconciler/clients.py",
    "tests/test_upstream_reconciler.py",
}
ALLOWED_FIXTURE_PREFIX = "tests/fixtures/upstream_schema/"
ACTIVE_GATE_PHASES = {
    "prepared",
    "verifying",
    "applying",
    "verified",
    "push_pending",
}
FORBIDDEN_PLAN_ACTIONS = {
    "delete_upstream_key",
    "confirm_upstream_key_absent",
    "quarantine_missing_resource",
}
AUTH_CODES = {
    "auth_required",
    "credential_missing",
    "interactive_auth_required",
}
SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._-]{20,}"),
    re.compile(
        r"(?i)\b(?:password|secret|token|cookie)\b\s*[:=]\s*[\"'][^\"'\n]{12,}[\"']"
    ),
    re.compile(r"/Users/[^/]+/(?:\.config|\.local/state|code/babata/\.env)"),
)


def _gate_error(
    message: str,
    *,
    phase: str,
    context: dict[str, Any] | None = None,
) -> ReconcileError:
    return ReconcileError(
        "maintenance_gate_failed",
        message,
        next_action="leave production unchanged and inspect the maintenance gate result",
        context={"phase": phase, **(context or {})},
    )


def _call(
    runner: CommandRunner,
    args: Sequence[str],
    *,
    cwd: Path,
    timeout: float = 120,
) -> subprocess.CompletedProcess[str]:
    return runner(
        list(args),
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _git(
    runner: CommandRunner,
    repo: Path,
    *args: str,
    phase: str,
    timeout: float = 120,
) -> str:
    completed = _call(runner, ["git", *args], cwd=repo, timeout=timeout)
    if completed.returncode != 0:
        raise _gate_error(
            "git maintenance check failed",
            phase=phase,
            context={"git_operation": args[0] if args else "unknown"},
        )
    return completed.stdout.strip()


def _json_payload(text: str, *, phase: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _gate_error("maintenance command returned invalid JSON", phase=phase) from exc
    if not isinstance(value, dict):
        raise _gate_error("maintenance command returned a non-object", phase=phase)
    return value


def _config_identity(
    config_path: Path | None,
    *,
    phase: str,
) -> tuple[Path, str]:
    path = (config_path or default_config_path()).expanduser().resolve()
    try:
        if not path.is_file():
            raise OSError("not a regular file")
        raw = path.read_bytes()
    except OSError as exc:
        raise _gate_error("private config is unavailable", phase=phase) from exc
    if len(raw) > 2_000_000:
        raise _gate_error("private config is unexpectedly large", phase=phase)
    return path, hashlib.sha256(raw).hexdigest()


def _assert_config_identity(
    config_path: Path | None,
    manifest: dict[str, Any],
    *,
    phase: str,
) -> Path:
    path, digest = _config_identity(config_path, phase=phase)
    if str(path) != manifest.get("config_path") or digest != manifest.get(
        "config_sha256"
    ):
        raise _gate_error("private config changed during maintenance", phase=phase)
    return path


def _invoke_reconciler(
    repo: Path,
    command: Sequence[str],
    *,
    config_path: Path | None,
    runner: CommandRunner,
    phase: str,
    timeout: float = 180,
) -> dict[str, Any]:
    argv = [str(repo / "sub2cli-reconcile-upstreams")]
    if config_path is not None:
        argv.extend(["--config", str(config_path)])
    argv.extend(command)
    completed = _call(runner, argv, cwd=repo, timeout=timeout)
    stream = completed.stdout if completed.returncode == 0 else completed.stderr
    payload = _json_payload(stream, phase=phase)
    if completed.returncode == 0:
        return payload
    error = payload.get("error")
    if isinstance(error, dict):
        raise ReconcileError(
            str(error.get("code") or "maintenance_command_failed"),
            str(error.get("message") or "maintenance command failed"),
            next_action=(
                str(error["next_action"]) if error.get("next_action") else None
            ),
            context=error.get("context") if isinstance(error.get("context"), dict) else None,
        )
    raise _gate_error("maintenance command failed", phase=phase)


def _default_observer(
    repo: Path,
    *,
    config_path: Path | None,
    runner: CommandRunner,
) -> dict[str, Any]:
    try:
        _invoke_reconciler(
            repo,
            ["doctor"],
            config_path=config_path,
            runner=runner,
            phase="prepare",
        )
    except ReconcileError as exc:
        return {"code": exc.code, "context": exc.context}
    raise _gate_error(
        "doctor did not reproduce a schema change",
        phase="prepare",
    )


def _normalize_observation(value: dict[str, Any]) -> dict[str, str]:
    error = value.get("error") if isinstance(value.get("error"), dict) else value
    code = str(error.get("code") or "")
    context = error.get("context") if isinstance(error.get("context"), dict) else {}
    if code in AUTH_CODES:
        provider_id = str(context.get("provider_id") or "unknown")
        raise ReconcileError(
            code,
            "authenticated schema observation is unavailable",
            next_action="repair the enrolled login outside autonomous maintenance",
            context={"provider_id": provider_id},
        )
    if code != "schema_changed":
        raise _gate_error(
            "only schema_changed may enter autonomous maintenance",
            phase="prepare",
            context={"observed_code": code or "missing"},
        )
    provider_id = str(context.get("provider_id") or "")
    endpoint = str(context.get("endpoint") or "")
    fingerprint = str(context.get("schema_fingerprint") or "")
    if (
        not provider_id
        or not provider_id.replace("-", "").isalnum()
        or endpoint not in SAFE_SCHEMA_ENDPOINTS
        or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
        or context.get("http_status") in (404, 405)
    ):
        raise _gate_error(
            "schema observation is not eligible for autonomous repair",
            phase="prepare",
            context={"provider_id": provider_id or "unknown"},
        )
    return {
        "provider_id": provider_id,
        "endpoint": endpoint,
        "schema_fingerprint": fingerprint,
    }


def _status_lines(runner: CommandRunner, repo: Path) -> list[str]:
    output = _git(
        runner,
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        phase="repository",
    )
    return [line for line in output.splitlines() if line]


def _assert_main_clean(
    runner: CommandRunner,
    repo: Path,
    *,
    expected_sha: str | None = None,
) -> str:
    branch = _git(runner, repo, "branch", "--show-current", phase="repository")
    if branch != "main":
        raise _gate_error("maintenance must start from main", phase="repository")
    sha = _git(runner, repo, "rev-parse", "HEAD", phase="repository")
    if expected_sha is not None and sha != expected_sha:
        raise _gate_error("local main moved during maintenance", phase="repository")
    dirty: list[str] = []
    for line in _status_lines(runner, repo):
        status = line[:2]
        path = line[3:]
        if status == "??" and (
            path == ".playwright-cli" or path.startswith(".playwright-cli/")
        ):
            continue
        dirty.append(path)
    if dirty:
        raise _gate_error(
            "tracked or unrelated worktree changes block maintenance",
            phase="repository",
            context={"dirty_count": len(dirty)},
        )
    return sha


def _origin_fingerprint(
    runner: CommandRunner,
    repo: Path,
    *,
    phase: str,
) -> str:
    fetch_urls = _git(
        runner,
        repo,
        "remote",
        "get-url",
        "--all",
        "origin",
        phase=phase,
    )
    push_urls = _git(
        runner,
        repo,
        "remote",
        "get-url",
        "--push",
        "--all",
        "origin",
        phase=phase,
    )
    mirror = _call(
        runner,
        ["git", "config", "--bool", "--get", "remote.origin.mirror"],
        cwd=repo,
    )
    if mirror.returncode not in (0, 1):
        raise _gate_error("origin configuration is invalid", phase=phase)
    if mirror.returncode == 0 and mirror.stdout.strip() == "true":
        raise _gate_error("mirror pushes are not allowed", phase=phase)
    payload = json.dumps(
        [fetch_urls.splitlines(), push_urls.splitlines()],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_path(state_dir: Path, gate_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", gate_id):
        raise _gate_error("invalid maintenance gate id", phase="manifest")
    return state_dir / "maintenance" / f"{gate_id}.json"


def _load_manifest(state_dir: Path, gate_id: str) -> tuple[Path, dict[str, Any]]:
    path = _manifest_path(state_dir, gate_id)
    value = load_json(path, None)
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise _gate_error("maintenance manifest is missing or invalid", phase="manifest")
    return path, value


def maintenance_status(*, state_dir: Path | None = None) -> dict[str, Any]:
    state_root = (state_dir or default_state_dir()).resolve()
    directory = state_root / "maintenance"
    active: list[dict[str, Any]] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            value = load_json(path, None)
            if not isinstance(value, dict) or value.get("schema") != 1:
                raise _gate_error(
                    "maintenance manifest is invalid",
                    phase="status",
                )
            phase = str(value.get("phase") or "")
            if phase not in ACTIVE_GATE_PHASES:
                continue
            active.append(
                {
                    "gate_id": str(value.get("gate_id") or path.stem),
                    "phase": phase,
                    "provider_id": str(value.get("provider_id") or "unknown"),
                    "branch": str(value.get("branch") or ""),
                    "worktree": str(value.get("worktree") or ""),
                }
            )
    return {"ok": True, "active_gates": active}


def _validated_manifest_paths(
    state_dir: Path,
    gate_id: str,
    manifest: dict[str, Any],
) -> tuple[Path, Path]:
    match = re.fullmatch(
        r"schema-(\d{8}T\d{12}Z)-([0-9a-f]{8})",
        gate_id,
    )
    if not match or manifest.get("gate_id") != gate_id:
        raise _gate_error("maintenance manifest identity is invalid", phase="manifest")
    expected_branch = f"codex/upstream-schema-repair-{match.group(1)}"
    if manifest.get("branch") != expected_branch:
        raise _gate_error("maintenance branch identity is invalid", phase="manifest")
    for field in ("base_sha", "origin_sha"):
        value = str(manifest.get(field) or "")
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
            raise _gate_error("maintenance commit identity is invalid", phase="manifest")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(manifest.get("origin_fingerprint") or ""),
    ):
        raise _gate_error("maintenance origin identity is invalid", phase="manifest")
    config_manifest_path = Path(str(manifest.get("config_path") or ""))
    if not config_manifest_path.is_absolute() or not re.fullmatch(
        r"[0-9a-f]{64}",
        str(manifest.get("config_sha256") or ""),
    ):
        raise _gate_error("maintenance config identity is invalid", phase="manifest")
    if str(manifest.get("schema_fingerprint") or "")[:8] != match.group(2):
        raise _gate_error("maintenance fingerprint identity is invalid", phase="manifest")
    baseline = manifest.get("baseline_active")
    if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
        raise _gate_error("maintenance baseline is invalid", phase="manifest")

    repo = Path(str(manifest.get("repo") or "")).resolve()
    worktree = Path(str(manifest.get("worktree") or "")).resolve()
    worktree_root = (state_dir / "maintenance-worktrees").resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise _gate_error("maintenance repository is unavailable", phase="manifest")
    if (
        not worktree.is_dir()
        or worktree.parent != worktree_root
        or worktree.name != gate_id
        or not (worktree / ".git").is_file()
    ):
        raise _gate_error("maintenance worktree is outside the gate", phase="manifest")
    return repo, worktree


def prepare_maintenance(
    config_path: Path | None = None,
    *,
    repo: Path | None = None,
    state_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
    observer: Observer | None = None,
    status_reader: StatusReader | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    # The scheduled agent itself runs in an ephemeral worktree. The absolute
    # reconciler entrypoint still imports this module from canonical main, which
    # is the repository the gate must compare and from which it creates its own
    # dedicated repair worktree.
    root = (repo or Path(__file__).resolve().parents[1]).resolve()
    state_root = (state_dir or default_state_dir()).resolve()
    active = maintenance_status(state_dir=state_root)["active_gates"]
    if active:
        first = active[0]
        raise _gate_error(
            "an unfinished maintenance gate must be resolved first",
            phase="prepare",
            context={
                "gate_id": first["gate_id"],
                "gate_phase": first["phase"],
                "active_gate_count": len(active),
            },
        )
    resolved_config, config_sha256 = _config_identity(config_path, phase="prepare")
    base_sha = _assert_main_clean(runner, root)
    _git(runner, root, "fetch", "--quiet", "origin", "main", phase="prepare")
    origin_sha = _git(
        runner, root, "rev-parse", "refs/remotes/origin/main", phase="prepare"
    )
    if base_sha != origin_sha:
        raise _gate_error("local main must equal origin/main", phase="prepare")
    origin_fingerprint = _origin_fingerprint(runner, root, phase="prepare")

    observe = observer or (
        lambda: _default_observer(root, config_path=resolved_config, runner=runner)
    )
    first = _normalize_observation(observe())
    second = _normalize_observation(observe())
    if first != second:
        raise _gate_error(
            "schema observations did not match",
            phase="prepare",
            context={"provider_id": first["provider_id"]},
        )

    read_status = status_reader or (
        lambda: _invoke_reconciler(
            root,
            ["status"],
            config_path=resolved_config,
            runner=runner,
            phase="prepare",
        )
    )
    status = read_status()
    if status.get("pending_recovery") is not False:
        raise _gate_error("pending recovery blocks maintenance", phase="prepare")
    baseline_active = status.get("active")
    if isinstance(baseline_active, bool) or not isinstance(baseline_active, int):
        raise _gate_error("status active count is invalid", phase="prepare")

    if _assert_main_clean(runner, root, expected_sha=base_sha) != origin_sha:
        raise _gate_error("repository changed during observation", phase="prepare")
    if _origin_fingerprint(runner, root, phase="prepare") != origin_fingerprint:
        raise _gate_error("origin configuration changed during observation", phase="prepare")
    current_config, current_config_sha256 = _config_identity(
        resolved_config,
        phase="prepare",
    )
    if current_config != resolved_config or current_config_sha256 != config_sha256:
        raise _gate_error("private config changed during observation", phase="prepare")
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    gate_id = f"schema-{stamp}-{first['schema_fingerprint'][:8]}"
    branch = f"codex/upstream-schema-repair-{stamp}"
    worktree_root = state_root / "maintenance-worktrees"
    ensure_private_dir(worktree_root)
    worktree = worktree_root / gate_id
    _git(
        runner,
        root,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        base_sha,
        phase="prepare",
    )
    manifest = {
        "schema": 1,
        "gate_id": gate_id,
        "phase": "prepared",
        "created_at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "repo": str(root),
        "worktree": str(worktree),
        "branch": branch,
        "base_sha": base_sha,
        "origin_sha": origin_sha,
        "origin_fingerprint": origin_fingerprint,
        "config_path": str(resolved_config),
        "config_sha256": config_sha256,
        "provider_id": first["provider_id"],
        "endpoint": first["endpoint"],
        "schema_fingerprint": first["schema_fingerprint"],
        "baseline_active": baseline_active,
        "verify_attempted": False,
        "apply_attempted": False,
    }
    manifest_path = _manifest_path(state_root, gate_id)
    atomic_write_json(manifest_path, manifest)
    return {
        "ok": True,
        "phase": "prepared",
        "gate_id": gate_id,
        "provider_id": first["provider_id"],
        "endpoint": first["endpoint"],
        "branch": branch,
        "worktree": str(worktree),
    }


def _allowed_repair_path(path: str) -> bool:
    if path in ALLOWED_REPAIR_FILES:
        return True
    return path.startswith(ALLOWED_FIXTURE_PREFIX) and path.endswith(".json")


def _changed_paths(runner: CommandRunner, worktree: Path) -> tuple[set[str], set[str]]:
    unstaged = set(
        filter(
            None,
            _git(
                runner,
                worktree,
                "diff",
                "--name-only",
                "--no-renames",
                phase="verify",
            ).splitlines(),
        )
    )
    staged = set(
        filter(
            None,
            _git(
                runner,
                worktree,
                "diff",
                "--cached",
                "--name-only",
                "--no-renames",
                phase="verify",
            ).splitlines(),
        )
    )
    untracked = set(
        filter(
            None,
            _git(
                runner,
                worktree,
                "ls-files",
                "--others",
                "--exclude-standard",
                phase="verify",
            ).splitlines(),
        )
    )
    return unstaged | staged | untracked, staged


def _assert_allowed_changes(runner: CommandRunner, worktree: Path) -> list[str]:
    changed, staged = _changed_paths(runner, worktree)
    if staged:
        raise _gate_error("pre-staged changes are not allowed", phase="verify")
    if not changed or "upstream_reconciler/clients.py" not in changed:
        raise _gate_error("adapter repair is missing", phase="verify")
    if "tests/test_upstream_reconciler.py" not in changed:
        raise _gate_error("focused regression evidence is missing", phase="verify")
    rejected = [path for path in changed if not _allowed_repair_path(path)]
    if rejected:
        raise _gate_error(
            "repair changed a non-allowlisted file",
            phase="verify",
            context={"rejected_count": len(rejected)},
        )
    tracked = set(
        filter(
            None,
            _git(runner, worktree, "ls-files", phase="verify").splitlines(),
        )
    )
    if not ALLOWED_REPAIR_FILES.issubset(tracked):
        raise _gate_error("required repair files are not tracked", phase="verify")
    if any(
        path.startswith(ALLOWED_FIXTURE_PREFIX) and path in tracked
        for path in changed
    ):
        raise _gate_error("schema fixtures must be new files", phase="verify")
    for path in changed:
        candidate = worktree / path
        if not candidate.exists() or candidate.is_symlink() or not candidate.is_file():
            raise _gate_error("repair files must be regular files", phase="verify")
    return sorted(changed)


def _added_text(runner: CommandRunner, worktree: Path, changed: Sequence[str]) -> str:
    diff = _git(
        runner,
        worktree,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        "--",
        *changed,
        phase="verify",
    )
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    tracked = set(
        filter(
            None,
            _git(runner, worktree, "ls-files", phase="verify").splitlines(),
        )
    )
    total = len(diff.encode("utf-8"))
    for path in changed:
        if path in tracked:
            continue
        raw = (worktree / path).read_text(encoding="utf-8")
        total += len(raw.encode("utf-8"))
        added.append(raw)
    if total > 2_000_000:
        raise _gate_error("repair diff is too large", phase="verify")
    return "\n".join(added)


def _assert_secret_free(runner: CommandRunner, worktree: Path, changed: Sequence[str]) -> None:
    text = _added_text(runner, worktree, changed)
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        raise _gate_error("secret-pattern scan rejected the repair", phase="verify")


def _assert_regression_evidence(runner: CommandRunner, worktree: Path) -> None:
    diff = _git(
        runner,
        worktree,
        "diff",
        "--no-ext-diff",
        "--unified=0",
        "--",
        "tests/test_upstream_reconciler.py",
        phase="verify",
    )
    added = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    removed = [
        line[1:]
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    if any(re.search(r"^\s*def\s+test_", line) for line in removed):
        raise _gate_error("repair removes an existing regression test", phase="verify")
    if not any(re.search(r"^\s*def\s+test_", line) for line in added):
        raise _gate_error("repair must add a focused regression test", phase="verify")
    if not any(
        re.search(
            r"(?:self\.assert|assertRaises|pytest\.raises|^\s*assert\s+)",
            line,
        )
        for line in added
    ):
        raise _gate_error("focused regression test has no assertion", phase="verify")


def _repair_content_digest(worktree: Path, changed: Sequence[str]) -> str:
    digest = hashlib.sha256()
    total = 0
    for path in sorted(changed):
        raw = (worktree / path).read_bytes()
        total += len(raw)
        if total > 4_000_000:
            raise _gate_error("repair content is too large", phase="verify")
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw)
        digest.update(b"\0")
    return digest.hexdigest()


def _run_checked(
    runner: CommandRunner,
    args: Sequence[str],
    *,
    cwd: Path,
    phase: str,
    timeout: float,
) -> None:
    completed = _call(runner, args, cwd=cwd, timeout=timeout)
    if completed.returncode != 0:
        raise _gate_error(
            "maintenance verification command failed",
            phase=phase,
            context={"check": Path(args[0]).name if args else "unknown"},
        )


def _assert_plan_safe(plan: dict[str, Any], *, baseline_active: int) -> None:
    actions = plan.get("actions")
    resources = plan.get("resources")
    if not isinstance(actions, list) or not isinstance(resources, list):
        raise _gate_error("repair plan is invalid", phase="verify")
    kinds = {
        str(item.get("kind"))
        for item in actions
        if isinstance(item, dict) and item.get("kind")
    }
    if kinds & FORBIDDEN_PLAN_ACTIONS or any(
        kind.startswith("quarantine") for kind in kinds
    ):
        raise _gate_error("repair plan contains a destructive action", phase="verify")
    if len(resources) < baseline_active:
        raise _gate_error("repair plan reduces active resources", phase="verify")


def verify_maintenance(
    gate_id: str,
    config_path: Path | None = None,
    *,
    state_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    state_root = (state_dir or default_state_dir()).resolve()
    manifest_path, manifest = _load_manifest(state_root, gate_id)
    if (
        manifest.get("phase") != "prepared"
        or manifest.get("verify_attempted")
        or manifest.get("apply_attempted")
    ):
        raise _gate_error("maintenance verify is not eligible", phase="verify")
    manifest["verify_attempted"] = True
    manifest["phase"] = "verifying"
    atomic_write_json(manifest_path, manifest)
    try:
        repo, worktree = _validated_manifest_paths(state_root, gate_id, manifest)
        resolved_config = _assert_config_identity(
            config_path,
            manifest,
            phase="verify",
        )
        base_sha = str(manifest["base_sha"])
        origin_sha = str(manifest["origin_sha"])
        _assert_main_clean(runner, repo, expected_sha=base_sha)
        if Path(
            _git(runner, repo, "rev-parse", "--show-toplevel", phase="verify")
        ).resolve() != repo:
            raise _gate_error("maintenance repository root changed", phase="verify")
        if _origin_fingerprint(runner, repo, phase="verify") != manifest.get(
            "origin_fingerprint"
        ):
            raise _gate_error("origin configuration changed", phase="verify")
        _git(runner, repo, "fetch", "--quiet", "origin", "main", phase="verify")
        current_origin = _git(
            runner,
            repo,
            "rev-parse",
            "refs/remotes/origin/main",
            phase="verify",
        )
        if current_origin != origin_sha:
            raise _gate_error("origin/main changed before verification", phase="verify")
        if _git(runner, worktree, "rev-parse", "HEAD", phase="verify") != base_sha:
            raise _gate_error("repair branch was committed outside the gate", phase="verify")
        if Path(
            _git(
                runner,
                worktree,
                "rev-parse",
                "--show-toplevel",
                phase="verify",
            )
        ).resolve() != worktree:
            raise _gate_error("repair worktree root changed", phase="verify")
        branch = _git(runner, worktree, "branch", "--show-current", phase="verify")
        if branch != manifest.get("branch"):
            raise _gate_error("repair worktree branch changed", phase="verify")

        changed = _assert_allowed_changes(runner, worktree)
        _assert_secret_free(runner, worktree, changed)
        _assert_regression_evidence(runner, worktree)
        repair_digest = _repair_content_digest(worktree, changed)
        python_files = sorted(
            str(path.relative_to(worktree))
            for path in (worktree / "upstream_reconciler").glob("*.py")
        )
        _run_checked(
            runner,
            [sys.executable, "-m", "py_compile", *python_files],
            cwd=worktree,
            phase="verify",
            timeout=180,
        )
        _run_checked(
            runner,
            [sys.executable, "-m", "unittest", "tests.test_upstream_reconciler"],
            cwd=worktree,
            phase="verify",
            timeout=300,
        )
        _run_checked(
            runner,
            [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
            cwd=worktree,
            phase="verify",
            timeout=900,
        )
        _git(runner, worktree, "diff", "--check", phase="verify")
        changed_after = _assert_allowed_changes(runner, worktree)
        if changed_after != changed:
            raise _gate_error("verification changed the repair diff", phase="verify")
        _assert_secret_free(runner, worktree, changed)

        _assert_config_identity(resolved_config, manifest, phase="verify")
        _invoke_reconciler(
            worktree,
            ["doctor"],
            config_path=resolved_config,
            runner=runner,
            phase="verify",
        )
        _assert_config_identity(resolved_config, manifest, phase="verify")
        plan = _invoke_reconciler(
            worktree,
            ["plan"],
            config_path=resolved_config,
            runner=runner,
            phase="verify",
        )
        _assert_plan_safe(plan, baseline_active=int(manifest["baseline_active"]))
        _assert_config_identity(resolved_config, manifest, phase="verify")

        changed_after_plan = _assert_allowed_changes(runner, worktree)
        if changed_after_plan != changed:
            raise _gate_error("doctor or plan changed the repair paths", phase="verify")
        _assert_secret_free(runner, worktree, changed)
        _assert_regression_evidence(runner, worktree)
        if _repair_content_digest(worktree, changed) != repair_digest:
            raise _gate_error("doctor or plan changed repair content", phase="verify")

        _git(runner, worktree, "add", "--", *changed, phase="verify")
        staged = set(
            filter(
                None,
                _git(
                    runner,
                    worktree,
                    "diff",
                    "--cached",
                    "--name-only",
                    "--no-renames",
                    phase="verify",
                ).splitlines(),
            )
        )
        if staged != set(changed):
            raise _gate_error("staged repair differs from allowlist", phase="verify")
        _git(runner, worktree, "diff", "--cached", "--check", phase="verify")
        message = f"fix(upstream): adapt {manifest['provider_id']} management schema"
        _git(runner, worktree, "commit", "-m", message, phase="verify")
        candidate_sha = _git(runner, worktree, "rev-parse", "HEAD", phase="verify")
        committed_paths = set(
            filter(
                None,
                _git(
                    runner,
                    worktree,
                    "diff",
                    "--name-only",
                    f"{base_sha}..{candidate_sha}",
                    phase="verify",
                ).splitlines(),
            )
        )
        if committed_paths != set(changed) or _repair_content_digest(
            worktree, changed
        ) != repair_digest:
            raise _gate_error("repair commit differs from verified content", phase="verify")
        manifest["candidate_sha"] = candidate_sha
        manifest["apply_attempted"] = True
        manifest["phase"] = "applying"
        atomic_write_json(manifest_path, manifest)

        _assert_config_identity(resolved_config, manifest, phase="verify")
        apply_result = _invoke_reconciler(
            worktree,
            [
                "apply",
                "--yes",
                "--maintenance-safe",
                "--min-active-resources",
                str(manifest["baseline_active"]),
            ],
            config_path=resolved_config,
            runner=runner,
            phase="verify",
            timeout=600,
        )
        _assert_config_identity(resolved_config, manifest, phase="verify")
        status = _invoke_reconciler(
            worktree,
            ["status"],
            config_path=resolved_config,
            runner=runner,
            phase="verify",
        )
        if status.get("pending_recovery") is not False:
            raise _gate_error("apply left pending recovery", phase="verify")
        active = status.get("active")
        if isinstance(active, bool) or not isinstance(active, int) or active < int(
            manifest["baseline_active"]
        ):
            raise _gate_error("apply reduced active resources", phase="verify")
        if _status_lines(runner, worktree):
            raise _gate_error("repair worktree is dirty after commit", phase="verify")
        manifest["phase"] = "verified"
        manifest["verified_at"] = datetime.now(UTC).isoformat()
        manifest["apply_run_id"] = apply_result.get("run_id")
        atomic_write_json(manifest_path, manifest)
        return {
            "ok": True,
            "phase": "verified",
            "gate_id": gate_id,
            "candidate_sha": candidate_sha,
            "branch": manifest["branch"],
        }
    except ReconcileError as exc:
        manifest["phase"] = "verify_failed"
        manifest["failure_code"] = exc.code
        atomic_write_json(manifest_path, manifest)
        raise


def _remote_main_sha(runner: CommandRunner, repo: Path) -> str:
    remote = _git(
        runner,
        repo,
        "ls-remote",
        "--heads",
        "origin",
        "refs/heads/main",
        phase="promote",
    )
    value = remote.split(maxsplit=1)[0] if remote else ""
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        raise _gate_error("remote main read-back is invalid", phase="promote")
    return value


def promote_maintenance(
    gate_id: str,
    config_path: Path | None = None,
    *,
    state_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    state_root = (state_dir or default_state_dir()).resolve()
    manifest_path, manifest = _load_manifest(state_root, gate_id)
    phase = str(manifest.get("phase") or "")
    if (
        phase not in ("verified", "push_pending")
        or not manifest.get("candidate_sha")
        or manifest.get("verify_attempted") is not True
        or manifest.get("apply_attempted") is not True
    ):
        raise _gate_error("maintenance promotion is not eligible", phase="promote")
    try:
        repo, worktree = _validated_manifest_paths(state_root, gate_id, manifest)
        resolved_config = (
            config_path
            if config_path is not None
            else Path(str(manifest["config_path"]))
        )
        _assert_config_identity(resolved_config, manifest, phase="promote")
        base_sha = str(manifest["base_sha"])
        origin_sha = str(manifest["origin_sha"])
        candidate_sha = str(manifest["candidate_sha"])
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", candidate_sha):
            raise _gate_error("candidate commit identity is invalid", phase="promote")
        local_sha = _assert_main_clean(runner, repo)
        allowed_local = {base_sha} if phase == "verified" else {base_sha, candidate_sha}
        if local_sha not in allowed_local:
            raise _gate_error("local main moved during promotion", phase="promote")
        if _origin_fingerprint(runner, repo, phase="promote") != manifest.get(
            "origin_fingerprint"
        ):
            raise _gate_error("origin configuration changed", phase="promote")
        _assert_config_identity(resolved_config, manifest, phase="promote")
        if _git(runner, worktree, "rev-parse", "HEAD", phase="promote") != candidate_sha:
            raise _gate_error("verified repair commit changed", phase="promote")
        if _status_lines(runner, worktree):
            raise _gate_error("verified repair worktree is dirty", phase="promote")
        count = _git(
            runner,
            worktree,
            "rev-list",
            "--count",
            f"{base_sha}..{candidate_sha}",
            phase="promote",
        )
        if count != "1":
            raise _gate_error("repair must contain exactly one commit", phase="promote")
        commit_paths = set(
            filter(
                None,
                _git(
                    runner,
                    worktree,
                    "diff",
                    "--name-only",
                    f"{base_sha}..{candidate_sha}",
                    phase="promote",
                ).splitlines(),
            )
        )
        if (
            not ALLOWED_REPAIR_FILES.issubset(commit_paths)
            or any(not _allowed_repair_path(path) for path in commit_paths)
        ):
            raise _gate_error("repair commit escaped the allowlist", phase="promote")

        remote_sha = _remote_main_sha(runner, repo)
        if remote_sha not in (origin_sha, candidate_sha):
            raise _gate_error("origin/main changed before promotion", phase="promote")
        if _origin_fingerprint(runner, repo, phase="promote") != manifest.get(
            "origin_fingerprint"
        ):
            raise _gate_error("origin configuration changed", phase="promote")
        _assert_config_identity(resolved_config, manifest, phase="promote")

        if remote_sha == origin_sha:
            manifest["phase"] = "push_pending"
            manifest["push_attempted"] = True
            manifest["push_started_at"] = datetime.now(UTC).isoformat()
            atomic_write_json(manifest_path, manifest)
            try:
                _git(
                    runner,
                    repo,
                    "push",
                    "--porcelain",
                    "origin",
                    f"{candidate_sha}:refs/heads/main",
                    phase="promote",
                    timeout=300,
                )
            except ReconcileError:
                # A lost push response is ambiguous. Read back before deciding;
                # if read-back is also unavailable, the durable push_pending
                # phase makes the next invocation resume safely.
                remote_sha = _remote_main_sha(runner, repo)
                if remote_sha != candidate_sha:
                    raise
            else:
                remote_sha = _remote_main_sha(runner, repo)

        if remote_sha != candidate_sha:
            raise _gate_error("remote main read-back failed", phase="promote")
        local_sha = _assert_main_clean(runner, repo)
        if local_sha == base_sha:
            _git(runner, repo, "merge", "--ff-only", candidate_sha, phase="promote")
        elif local_sha != candidate_sha:
            raise _gate_error("local main changed after push", phase="promote")
        manifest["phase"] = "promoted"
        manifest["promoted_at"] = datetime.now(UTC).isoformat()
        atomic_write_json(manifest_path, manifest)
        return {
            "ok": True,
            "phase": "promoted",
            "gate_id": gate_id,
            "commit": candidate_sha,
            "branch": "main",
        }
    except ReconcileError as exc:
        if manifest.get("phase") != "push_pending":
            manifest["phase"] = "promote_failed"
        manifest["failure_code"] = exc.code
        atomic_write_json(manifest_path, manifest)
        raise


def _notify_gate_failure(
    error: ReconcileError,
    *,
    config_path: Path | None,
    phase: str,
    runner: CommandRunner,
) -> dict[str, Any]:
    config = load_json(config_path or default_config_path(), None)
    notifications = config.get("notifications") if isinstance(config, dict) else None
    command = notifications.get("telegram_command") if isinstance(notifications, dict) else None
    if not isinstance(command, list) or not command or any(
        not isinstance(item, str) or not item for item in command
    ):
        return {"status": "not_configured"}
    provider_id = str(error.context.get("provider_id") or "unknown")
    message = (
        "⚠️ sub2cli schema maintenance gate stopped\n"
        f"phase: {phase}\n"
        f"provider: {provider_id}\n"
        f"reason: {error.code}\n"
        "The gate stopped safely; any uncertain push will be read back before retry."
    )
    text_flag = notifications.get("telegram_text_flag", "--text")
    argv = [*command, *([str(text_flag), message] if text_flag else [message])]
    try:
        completed = _call(
            runner,
            argv,
            cwd=Path.cwd(),
            timeout=float(notifications.get("timeout_seconds", 20)),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"status": "failed"}
    return {"status": "sent" if completed.returncode == 0 else "failed"}


def run_maintenance_phase(
    phase: str,
    *,
    gate_id: str | None = None,
    config_path: Path | None = None,
    notify_on_failure: bool = False,
    runner: CommandRunner = subprocess.run,
) -> dict[str, Any]:
    try:
        if phase == "status":
            return maintenance_status()
        if phase == "prepare":
            return prepare_maintenance(config_path, runner=runner)
        if phase == "verify" and gate_id:
            return verify_maintenance(gate_id, config_path, runner=runner)
        if phase == "promote" and gate_id:
            return promote_maintenance(gate_id, config_path, runner=runner)
        raise _gate_error("invalid maintenance phase arguments", phase=phase)
    except ReconcileError as exc:
        if notify_on_failure and exc.code not in AUTH_CODES:
            result = _notify_gate_failure(
                exc,
                config_path=config_path,
                phase=phase,
                runner=runner,
            )
            exc.context.setdefault("maintenance_notification", result.get("status"))
        raise
