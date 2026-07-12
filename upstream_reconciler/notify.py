from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .core import ReconcileError
from .store import atomic_write_json, default_config_path, default_state_dir, load_json


DEFAULT_AUTH_CODES = {
    "auth_required",
    "credential_missing",
    "interactive_auth_required",
}


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _load_notification_config(config_path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    notifications = value.get("notifications") if isinstance(value, dict) else None
    return notifications if isinstance(notifications, dict) else None


def notify_reconcile_error(
    error: ReconcileError,
    *,
    config_path: Path | None = None,
    state_dir: Path | None = None,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    path = config_path or default_config_path()
    config = _load_notification_config(path)
    if not config:
        return {"status": "not_configured"}
    enabled_codes = config.get("error_codes", sorted(DEFAULT_AUTH_CODES))
    allowed_codes = (
        {str(value) for value in enabled_codes}
        if isinstance(enabled_codes, list)
        else set()
    )
    cause_code = str(error.context.get("cause_code") or "")
    effective_code = cause_code if cause_code in allowed_codes else error.code
    if effective_code not in allowed_codes:
        return {"status": "not_applicable"}
    command = config.get("telegram_command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(value, str) or not value for value in command)
    ):
        return {"status": "not_configured"}

    current = (now or datetime.now(UTC)).astimezone(UTC)
    provider_id = str(error.context.get("provider_id") or "unknown")
    fingerprint = hashlib.sha256(
        json.dumps([effective_code, provider_id], separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    root = state_dir or default_state_dir()
    dedupe_path = root / "notification-state.json"
    try:
        state = load_json(dedupe_path, {"schema": 1, "sent": {}})
    except ReconcileError:
        state = {"schema": 1, "sent": {}}
    if not isinstance(state, dict):
        state = {"schema": 1, "sent": {}}
    sent = state.setdefault("sent", {})
    if not isinstance(sent, dict):
        sent = {}
        state["sent"] = sent
    try:
        dedupe_hours = float(config.get("dedupe_hours", 24))
    except (TypeError, ValueError):
        dedupe_hours = 24
    last_sent = _parse_time(sent.get(fingerprint))
    if last_sent is not None and current - last_sent < timedelta(
        hours=max(1, dedupe_hours)
    ):
        return {"status": "deduplicated", "provider_id": provider_id}

    message = (
        "⚠️ sub2cli 上游自动化需要人工登录\n"
        f"上游: {provider_id}\n"
        f"类型: {effective_code}\n"
        f"时间: {current.isoformat()}\n"
        "请检查密码是否变化，或网站是否新增验证码 / 2FA。"
    )
    text_flag = config.get("telegram_text_flag", "--text")
    message_args = [str(text_flag), message] if text_flag else [message]
    try:
        completed = runner(
            [*command, *message_args],
            check=False,
            capture_output=True,
            text=True,
            timeout=float(config.get("timeout_seconds", 20)),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return {"status": "failed", "provider_id": provider_id}
    if completed.returncode != 0:
        return {"status": "failed", "provider_id": provider_id}
    sent[fingerprint] = current.isoformat()
    state["schema"] = 1
    atomic_write_json(dedupe_path, state)
    return {"status": "sent", "provider_id": provider_id}
