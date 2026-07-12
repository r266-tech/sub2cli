from __future__ import annotations

import contextlib
import ctypes
import fcntl
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .core import ReconcileError, redact


KEYCHAIN_SERVICE = "com.r266.sub2cli.upstream-reconciler"
ERR_SEC_ITEM_NOT_FOUND = -25300


def _security_framework() -> tuple[Any, Any]:
    try:
        security = ctypes.CDLL(
            "/System/Library/Frameworks/Security.framework/Security"
        )
        core_foundation = ctypes.CDLL(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
        )
    except OSError as exc:  # pragma: no cover - this CLI is macOS-only
        raise ReconcileError("keychain_unavailable", "macOS Keychain is unavailable") from exc

    security.SecKeychainFindGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainFindGenericPassword.restype = ctypes.c_int32
    security.SecKeychainAddGenericPassword.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    security.SecKeychainAddGenericPassword.restype = ctypes.c_int32
    security.SecKeychainItemModifyAttributesAndData.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    security.SecKeychainItemModifyAttributesAndData.restype = ctypes.c_int32
    security.SecKeychainItemFreeContent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    security.SecKeychainItemFreeContent.restype = ctypes.c_int32
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    core_foundation.CFRelease.restype = None
    return security, core_foundation


def _keychain_find(account: str, *, include_item: bool = False) -> tuple[int, str | None, int | None]:
    security, core_foundation = _security_framework()
    service_bytes = KEYCHAIN_SERVICE.encode("utf-8")
    account_bytes = account.encode("utf-8")
    length = ctypes.c_uint32()
    data = ctypes.c_void_p()
    item = ctypes.c_void_p()
    status = security.SecKeychainFindGenericPassword(
        None,
        len(service_bytes),
        service_bytes,
        len(account_bytes),
        account_bytes,
        ctypes.byref(length),
        ctypes.byref(data),
        ctypes.byref(item),
    )
    value: str | None = None
    if status == 0:
        try:
            value = ctypes.string_at(data, length.value).decode("utf-8")
        finally:
            security.SecKeychainItemFreeContent(None, data)
    item_value = int(item.value) if item.value else None
    if item_value and not include_item:
        core_foundation.CFRelease(item)
        item_value = None
    return int(status), value, item_value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "sub2cli" / "upstream-reconciler.json"


def default_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return root / "sub2cli" / "upstream-reconciler"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def atomic_write_json(path: Path, value: Any) -> None:
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def load_json(path: Path, default: Any) -> Any:
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except (OSError, json.JSONDecodeError) as exc:
        raise ReconcileError("invalid_local_state", f"cannot read {path.name}") from exc


def append_audit(path: Path, event: dict[str, Any]) -> None:
    ensure_private_dir(path.parent)
    payload = {"at": utc_now(), **redact(event)}
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def keychain_get(account: str, *, required: bool = True) -> str | None:
    status, value, _ = _keychain_find(account)
    if status == 0:
        return value
    if required:
        context: dict[str, str] = {}
        if account.startswith("provider:"):
            parts = account.split(":", 2)
            if len(parts) >= 2 and parts[1]:
                context["provider_id"] = parts[1]
        elif account.startswith("target:"):
            context["provider_id"] = "target"
        raise ReconcileError(
            "credential_missing",
            f"local credential {account} is not enrolled",
            next_action="run enroll-edge while the target sites are logged in in Edge",
            context=context,
        )
    return None


def keychain_set(account: str, value: str) -> None:
    if not value:
        raise ReconcileError("credential_missing", f"refusing to store an empty credential for {account}")
    current = keychain_get(account, required=False)
    if current == value:
        return
    security, core_foundation = _security_framework()
    status, _, item = _keychain_find(account, include_item=True)
    value_bytes = value.encode("utf-8")
    value_buffer = ctypes.create_string_buffer(value_bytes)
    if status == 0 and item:
        try:
            result = security.SecKeychainItemModifyAttributesAndData(
                ctypes.c_void_p(item), None, len(value_bytes), value_buffer
            )
        finally:
            core_foundation.CFRelease(ctypes.c_void_p(item))
    elif status == ERR_SEC_ITEM_NOT_FOUND:
        service_bytes = KEYCHAIN_SERVICE.encode("utf-8")
        account_bytes = account.encode("utf-8")
        created_item = ctypes.c_void_p()
        result = security.SecKeychainAddGenericPassword(
            None,
            len(service_bytes),
            service_bytes,
            len(account_bytes),
            account_bytes,
            len(value_bytes),
            value_buffer,
            ctypes.byref(created_item),
        )
        if created_item.value:
            core_foundation.CFRelease(created_item)
    else:
        result = status
        if item:
            core_foundation.CFRelease(ctypes.c_void_p(item))
    if result != 0:
        raise ReconcileError("keychain_write_failed", f"could not store local credential {account}")
    if keychain_get(account, required=False) != value:
        raise ReconcileError("keychain_write_failed", f"could not verify local credential {account}")


@contextlib.contextmanager
def exclusive_lock(state_dir: Path) -> Iterator[None]:
    ensure_private_dir(state_dir)
    path = state_dir / "reconcile.lock"
    handle = path.open("a+")
    os.chmod(path, 0o600)
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReconcileError("lock_busy", "another reconciliation run is already active") from exc
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
