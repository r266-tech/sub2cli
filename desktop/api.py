"""desktop/api.py — JsApi exposed to the pywebview frontend.

All Sub2Context-backed methods live here. Frontend calls them via
`window.pywebview.api.<method>(...)`. The Sub2Context instance is cached
per-process so the Edge CDP token isn't re-fetched on every call; relay
switching (P4) will replace `self.ctx` to point at a new domain.
"""
from __future__ import annotations

import glob
import importlib.machinery
import importlib.util
import json
import os
import plistlib
import re
import base64
import errno
import fcntl
import select
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import keyring
import requests

try:
    import Security  # type: ignore[import-not-found]
except Exception:  # noqa: BLE001 - optional outside the bundled macOS app env
    Security = None  # type: ignore[assignment]


KEYCHAIN_SERVICE = "sub2cli"
KEYCHAIN_CREDS_SERVICE = "sub2cli:creds"
KEYCHAIN_CUSTOM_API_SERVICE = "sub2cli:custom-api"
GITHUB_REPO = "r266-tech/sub2cli"
INJECT_PLAN_TIMEOUT = 90
INJECT_APPLY_TIMEOUT = 180
INJECT_ROLLBACK_TIMEOUT = 90
CODEX_LOGIN_TIMEOUT = 300
CODEX_AUTH_REFRESH_TIMEOUT = 25
CODEX_PROVIDER_HOME_PATH = Path(os.environ.get("CODEX_PROVIDER_HOME", str(Path.home()))).expanduser()
CODEX_HOME_PATH = Path(os.environ.get("CODEX_HOME", str(CODEX_PROVIDER_HOME_PATH / ".codex"))).expanduser()
PROVIDER_SLOTS_PATH = CODEX_HOME_PATH / "provider-slots.json"
ROUTE_POOL_LOG_PATH = CODEX_HOME_PATH / "sub2cli-responses-proxy.log"
CODEX_APP_SUPPORT_PATH = CODEX_PROVIDER_HOME_PATH / "Library" / "Application Support"
CODEXBAR_MANAGED_ACCOUNTS_PATH = (
    CODEX_APP_SUPPORT_PATH / "CodexBar" / "managed-codex-accounts.json"
)
CODEXBAR_MANAGED_HOMES_PATH = CODEX_APP_SUPPORT_PATH / "CodexBar" / "managed-codex-homes"
CODEX_LOCK_PATH = CODEX_HOME_PATH / ".sub2cli-inject.lock"
INJECT_BACKUP_ROOT = CODEX_HOME_PATH / "provider-switch-backups"
AUTO_ROLLBACK_MARKER = ".auto-rollback-done"
UPDATE_CACHE_DIR = Path.home() / "Library" / "Caches" / "sub2cli" / "updates"
UPDATE_LOG_PATH = Path.home() / "Library" / "Logs" / "sub2cli-updater.log"
UPDATE_DOWNLOAD_MAX_BYTES = 300 * 1024 * 1024
APP_SUPPORT_DIR = CODEX_PROVIDER_HOME_PATH / "Library" / "Application Support" / "sub2cli"
RELAY_CREDS_PATH = APP_SUPPORT_DIR / "relay-credentials.json"
RELAY_CREDS_KEY_PATH = APP_SUPPORT_DIR / "relay-credentials.key"

CODEX_CLI_FALLBACKS = (
    "~/bin/codex",
    "~/.local/bin/codex",
    "~/.npm-global/bin/codex",
    "~/.volta/bin/codex",
    "~/.bun/bin/codex",
    "~/Library/pnpm/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
)
CODEX_CLI_FALLBACK_GLOBS = (
    "~/.nvm/versions/node/*/bin/codex",
    "~/.local/share/fnm/node-versions/*/installation/bin/codex",
)
CODEX_APP_BUNDLED_CLI = "/Applications/Codex.app/Contents/Resources/codex"


# Cross-THREAD exclusion within this process. pywebview dispatches each JsApi
# method on its own worker thread, so the previous class-global `_depth` counter
# (mutated with no synchronization) let a second thread observe depth>0 and take
# the "nested" branch WITHOUT ever acquiring the flock — two threads mutating
# ~/.codex concurrently (lost updates). This RLock serializes all callers before
# they touch the flock and gives correct same-thread reentrancy. The flock still
# provides cross-PROCESS exclusion; this RLock provides cross-THREAD exclusion.
_CODEX_STATE_RLOCK = threading.RLock()


class CodexStateLock:
    """Shared advisory lock for mutations under CODEX_HOME.

    Two layers: a per-process re-entrant threading.RLock (cross-thread) wrapping
    a flock on CODEX_LOCK_PATH (cross-process). `_depth` is only read/written
    while the RLock is held, so it is safe.
    """

    _depth = 0

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._fd: int | None = None
        self._nested = False
        self._holds_rlock = False

    def __enter__(self) -> "CodexStateLock":
        # Block other threads first (reentrant for this thread). Honor timeout so
        # a wedged holder can't hang the UI forever.
        if not _CODEX_STATE_RLOCK.acquire(timeout=self.timeout):
            raise RuntimeError(
                f"Codex 配置正在被本进程的另一个线程修改 ({CODEX_LOCK_PATH})"
            )
        self._holds_rlock = True
        try:
            if CodexStateLock._depth > 0:
                # Same-thread reentry (RLock guarantees we are that thread).
                CodexStateLock._depth += 1
                self._nested = True
                return self
            CODEX_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(str(CODEX_LOCK_PATH), os.O_CREAT | os.O_WRONLY, 0o600)
            deadline = time.time() + self.timeout
            while True:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.ftruncate(self._fd, 0)
                    os.write(self._fd, f"pid={os.getpid()} t={int(time.time())}\n".encode())
                    CodexStateLock._depth = 1
                    return self
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES):
                        os.close(self._fd)
                        self._fd = None
                        raise
                    if time.time() >= deadline:
                        os.close(self._fd)
                        self._fd = None
                        raise RuntimeError(
                            f"Codex 配置正在被另一个 sub2cli 进程修改 ({CODEX_LOCK_PATH})"
                        )
                    time.sleep(0.1)
        except BaseException:
            # Anything failed after acquiring the RLock — release it so we never
            # leak the cross-thread lock.
            self._holds_rlock = False
            _CODEX_STATE_RLOCK.release()
            raise

    def __exit__(self, *_exc: object) -> None:
        try:
            if self._nested:
                CodexStateLock._depth = max(0, CodexStateLock._depth - 1)
                return
            CodexStateLock._depth = 0
            if self._fd is not None:
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._fd)
                    self._fd = None
        finally:
            if self._holds_rlock:
                self._holds_rlock = False
                _CODEX_STATE_RLOCK.release()


def _is_executable_file(path: str) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _candidate_codex_paths() -> list[str]:
    paths: list[str] = []
    path_hit = shutil.which("codex")
    if path_hit:
        paths.append(path_hit)
    paths.extend(os.path.expanduser(p) for p in CODEX_CLI_FALLBACKS)
    for pattern in CODEX_CLI_FALLBACK_GLOBS:
        paths.extend(sorted(glob.glob(os.path.expanduser(pattern))))
    paths.append(CODEX_APP_BUNDLED_CLI)

    deduped: list[str] = []
    seen: set[str] = set()
    for path in paths:
        real = os.path.realpath(path)
        if real not in seen:
            deduped.append(path)
            seen.add(real)
    return deduped


def _find_codex_cli() -> tuple[str | None, str]:
    """Return (path, source), where source is path/common/app-bundled/missing."""
    path_hit = shutil.which("codex")
    if path_hit and _is_executable_file(path_hit):
        return path_hit, "path"

    for path in _candidate_codex_paths():
        if path == path_hit or path == CODEX_APP_BUNDLED_CLI:
            continue
        if _is_executable_file(path):
            return path, "common"

    if _is_executable_file(CODEX_APP_BUNDLED_CLI):
        return CODEX_APP_BUNDLED_CLI, "app-bundled"
    return None, "missing"


def _codex_version(codex_path: str) -> str:
    try:
        r = subprocess.run(
            [codex_path, "--version"],
            capture_output=True, text=True, timeout=3,
        )
        return (r.stdout or r.stderr).strip().split("\n")[0] or "?"
    except Exception:
        return "?"


def _read_app_version() -> str:
    """Read CFBundleShortVersionString from the running .app's Info.plist.

    Falls back to 'dev' when running outside a bundle.
    """
    import sys as _sys
    exe_dir = os.path.dirname(os.path.abspath(_sys.executable))
    if not exe_dir.endswith("/Contents/MacOS"):
        return "dev"
    plist_path = os.path.join(os.path.dirname(exe_dir), "Info.plist")
    try:
        import plistlib
        with open(plist_path, "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString", "?")
    except Exception:
        return "?"


def _parse_semver(s: str) -> tuple[int, ...]:
    s = (s or "").lstrip("v").strip()
    parts = []
    for p in s.split("."):
        # tolerate suffixes like "0.1.1-rc1"
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def _running_app_bundle_path() -> Path | None:
    exe = Path(sys.executable).resolve()
    if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
        app_path = exe.parent.parent.parent
        if app_path.suffix == ".app":
            return app_path
    return None


def _fetch_latest_release(timeout: int = 6) -> tuple[dict | None, str | None]:
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
            headers={"Accept": "application/vnd.github+json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"网络错误: {type(exc).__name__}"
    if r.status_code == 404:
        return None, "尚无 release"
    if r.status_code != 200:
        return None, f"GitHub HTTP {r.status_code}"
    try:
        return r.json(), None
    except ValueError:
        return None, "响应非 JSON"


def _release_dmg_asset(release: dict | None) -> dict | None:
    assets = (release or {}).get("assets") or []
    candidates = [
        a for a in assets
        if isinstance(a, dict)
        and str(a.get("name") or "").lower().endswith(".dmg")
        and a.get("browser_download_url")
    ]
    if not candidates:
        return None
    preferred = [a for a in candidates if "sub2cli" in str(a.get("name") or "").lower()]
    return (preferred or candidates)[0]


def _read_bundle_version(app_path: Path) -> str | None:
    plist_path = app_path / "Contents" / "Info.plist"
    try:
        with plist_path.open("rb") as fh:
            version = plistlib.load(fh).get("CFBundleShortVersionString")
        return str(version or "").strip() or None
    except Exception:
        return None


def _app_from_mounted_dmg(mount_point: Path) -> Path | None:
    for candidate in mount_point.iterdir():
        if candidate.name == "sub2cli.app" and candidate.is_dir():
            return candidate
    apps = sorted(
        [p for p in mount_point.glob("*.app") if p.is_dir()],
        key=lambda p: (p.name != "sub2cli.app", p.name.lower()),
    )
    return apps[0] if apps else None


def _download_release_asset(url: str, dest: Path, *, timeout: int = 20) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            total = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > UPDATE_DOWNLOAD_MAX_BYTES:
                        raise RuntimeError("下载文件超过 300MB，已停止")
                    fh.write(chunk)
        os.replace(tmp, dest)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def _hdiutil_attach(dmg_path: Path) -> Path:
    proc = subprocess.run(
        [
            "hdiutil",
            "attach",
            str(dmg_path),
            "-nobrowse",
            "-readonly",
            "-plist",
        ],
        capture_output=True,
        text=False,
        timeout=60,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"hdiutil attach 失败: {stderr.strip() or proc.returncode}")
    body = plistlib.loads(proc.stdout)
    for entity in body.get("system-entities") or []:
        mount_point = entity.get("mount-point")
        if mount_point:
            return Path(mount_point)
    raise RuntimeError("DMG 未返回 mount-point")


def _hdiutil_detach(mount_point: Path) -> None:
    subprocess.run(
        ["hdiutil", "detach", str(mount_point), "-quiet"],
        capture_output=True,
        timeout=30,
        check=False,
    )


def _stage_update_app_from_dmg(dmg_path: Path, latest: str) -> Path:
    stage_root = UPDATE_CACHE_DIR / f"stage-{latest}-{os.getpid()}-{time.time_ns()}"
    staged_app = stage_root / "sub2cli.app"
    mount_point: Path | None = None
    try:
        mount_point = _hdiutil_attach(dmg_path)
        source_app = _app_from_mounted_dmg(mount_point)
        if source_app is None:
            raise RuntimeError("DMG 中没有找到 .app")
        bundled_version = _read_bundle_version(source_app)
        if bundled_version != latest:
            raise RuntimeError(f"DMG app 版本不匹配: {bundled_version or '?'} != {latest}")
        if staged_app.exists():
            shutil.rmtree(staged_app)
        staged_app.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_app, staged_app, symlinks=True)
    finally:
        if mount_point is not None:
            _hdiutil_detach(mount_point)
    return staged_app


def _write_update_script(*, staged_app: Path, target_app: Path, latest: str) -> Path:
    UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    script_path = UPDATE_CACHE_DIR / f"install-{latest}-{os.getpid()}-{time.time_ns()}.sh"
    backup_app = UPDATE_CACHE_DIR / f"backup-{latest}-{os.getpid()}-{time.time_ns()}.app"
    target_parent = target_app.parent
    script = f"""#!/bin/sh
set -eu
LOG={shlex.quote(str(UPDATE_LOG_PATH))}
exec >>"$LOG" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sub2cli updater start latest={latest}"
STAGED_APP={shlex.quote(str(staged_app))}
TARGET_APP={shlex.quote(str(target_app))}
BACKUP_APP={shlex.quote(str(backup_app))}
TARGET_PARENT={shlex.quote(str(target_parent))}
mkdir -p "$TARGET_PARENT"
osascript -e 'tell application id "com.r266-tech.sub2cli" to quit' || true
for i in 1 2 3 4 5 6 7 8 9 10; do
  if pgrep -f "$TARGET_APP/Contents/MacOS/sub2cli" >/dev/null 2>&1; then
    sleep 1
  else
    break
  fi
done
rm -rf "$BACKUP_APP"
if [ -d "$TARGET_APP" ]; then
  mv "$TARGET_APP" "$BACKUP_APP"
fi
if cp -R "$STAGED_APP" "$TARGET_APP"; then
  xattr -dr com.apple.quarantine "$TARGET_APP" >/dev/null 2>&1 || true
  rm -rf "$BACKUP_APP"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] sub2cli updater installed $TARGET_APP"
  open "$TARGET_APP" || true
  exit 0
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] sub2cli updater copy failed"
rm -rf "$TARGET_APP"
if [ -d "$BACKUP_APP" ]; then
  mv "$BACKUP_APP" "$TARGET_APP"
  open "$TARGET_APP" || true
fi
exit 1
"""
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o755)
    return script_path


_relay_cred_lock = threading.RLock()
_relay_cred_key_bytes: bytes | None = None


def _ensure_private_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _relay_cred_secret_key() -> bytes:
    global _relay_cred_key_bytes
    if _relay_cred_key_bytes is not None:
        return _relay_cred_key_bytes
    with _relay_cred_lock:
        if _relay_cred_key_bytes is not None:
            return _relay_cred_key_bytes
        if RELAY_CREDS_KEY_PATH.exists():
            key = base64.urlsafe_b64decode(RELAY_CREDS_KEY_PATH.read_bytes().strip())
        else:
            key = os.urandom(32)
            _ensure_private_file(RELAY_CREDS_KEY_PATH, base64.urlsafe_b64encode(key))
        _relay_cred_key_bytes = key
        return _relay_cred_key_bytes


def _relay_cred_encode(value: str) -> str:
    data = value.encode("utf-8")
    key = _relay_cred_secret_key()
    stream = (key * ((len(data) // len(key)) + 1))[:len(data)]
    encoded = bytes(a ^ b for a, b in zip(data, stream))
    return base64.urlsafe_b64encode(encoded).decode("ascii")


def _relay_cred_decode(value: str) -> str | None:
    try:
        data = base64.urlsafe_b64decode(str(value).encode("ascii"))
        key = _relay_cred_secret_key()
        stream = (key * ((len(data) // len(key)) + 1))[:len(data)]
        decoded = bytes(a ^ b for a, b in zip(data, stream))
        return decoded.decode("utf-8")
    except Exception:
        return None


def _relay_cred_load() -> dict:
    if not RELAY_CREDS_PATH.exists():
        return {"version": 1, "items": {}}
    try:
        data = json.loads(RELAY_CREDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "items": {}}
    if not isinstance(data, dict):
        return {"version": 1, "items": {}}
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _relay_cred_save(data: dict) -> None:
    _ensure_private_file(
        RELAY_CREDS_PATH,
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _relay_cred_key(domain: str, email: str) -> str:
    return base64.urlsafe_b64encode(f"{domain}|{email}".encode("utf-8")).decode("ascii")


def _relay_cred_item(domain: str, email: str) -> dict:
    data = _relay_cred_load()
    item = (data.get("items") or {}).get(_relay_cred_key(domain, email))
    return item if isinstance(item, dict) else {}


def _relay_cred_get_secret(domain: str, email: str, field: str) -> str | None:
    with _relay_cred_lock:
        raw = _relay_cred_item(domain, email).get(field)
        if not raw:
            return None
        return _relay_cred_decode(str(raw))


def _relay_cred_set_secret(domain: str, email: str, field: str, value: str) -> bool:
    if not domain or not email or not value:
        return False
    with _relay_cred_lock:
        try:
            data = _relay_cred_load()
            items = data.setdefault("items", {})
            key = _relay_cred_key(domain, email)
            item = items.setdefault(key, {})
            item.update({"domain": domain, "email": email, "updated_at": int(time.time())})
            item[field] = _relay_cred_encode(value)
            _relay_cred_save(data)
            return True
        except Exception:
            return False


def _relay_cred_delete(domain: str, email: str) -> None:
    with _relay_cred_lock:
        data = _relay_cred_load()
        items = data.get("items") or {}
        items.pop(_relay_cred_key(domain, email), None)
        data["items"] = items
        _relay_cred_save(data)


def _relay_cred_delete_field(domain: str, email: str, field: str) -> None:
    with _relay_cred_lock:
        data = _relay_cred_load()
        items = data.get("items") or {}
        key = _relay_cred_key(domain, email)
        item = items.get(key)
        if isinstance(item, dict):
            item.pop(field, None)
            if not any(item.get(secret_field) for secret_field in ("password", "token")):
                items.pop(key, None)
            else:
                item["updated_at"] = int(time.time())
        data["items"] = items
        _relay_cred_save(data)


def _kc_creds_set(domain: str, email: str, password: str, *, allow_prompt: bool = False) -> bool:
    """Return True when relay login credentials are persisted locally.

    Callers MUST check this before reporting success — a swallowed failure means
    the credential isn't persisted and is silently lost on next launch.
    """
    if _relay_cred_set_secret(domain, email, "password", password):
        return True
    username = f"{domain}|{email}"
    if not allow_prompt:
        return _security_set_password(KEYCHAIN_CREDS_SERVICE, username, password)
    try:
        keyring.set_password(KEYCHAIN_CREDS_SERVICE, username, password)
        return True
    except Exception:  # noqa: BLE001 - keyring backend may be unavailable
        return False


def _security_status(result: Any) -> int:
    if isinstance(result, tuple):
        return int(result[0])
    return int(result)


def _security_get_password(service: str, username: str) -> str | None:
    """Read a Keychain item without allowing macOS to present an auth dialog."""
    if Security is None:
        return None
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: service,
        Security.kSecAttrAccount: username or "",
        Security.kSecReturnData: True,
        Security.kSecUseAuthenticationUI: Security.kSecUseAuthenticationUIFail,
    }
    try:
        status, data = Security.SecItemCopyMatching(query, None)
    except Exception:  # noqa: BLE001
        return None
    if int(status) != int(Security.errSecSuccess) or data is None:
        return None
    try:
        return bytes(data).decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def _security_set_password(service: str, username: str, password: str) -> bool:
    """Write/update a Keychain item without allowing macOS auth UI."""
    if Security is None:
        return False
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: service,
        Security.kSecAttrAccount: username or "",
        Security.kSecUseAuthenticationUI: Security.kSecUseAuthenticationUIFail,
    }
    try:
        status = _security_status(
            Security.SecItemUpdate(query, {Security.kSecValueData: password.encode("utf-8")})
        )
        if status == int(Security.errSecSuccess):
            return True
        if status != int(Security.errSecItemNotFound):
            return False
        add_query = dict(query)
        add_query[Security.kSecValueData] = password.encode("utf-8")
        status = _security_status(Security.SecItemAdd(add_query, None))
        return status == int(Security.errSecSuccess)
    except Exception:  # noqa: BLE001
        return False


def _security_delete_password(service: str, username: str) -> bool:
    """Delete a Keychain item without allowing macOS auth UI."""
    if Security is None:
        return False
    query = {
        Security.kSecClass: Security.kSecClassGenericPassword,
        Security.kSecAttrService: service,
        Security.kSecAttrAccount: username or "",
        Security.kSecUseAuthenticationUI: Security.kSecUseAuthenticationUIFail,
    }
    try:
        status = _security_status(Security.SecItemDelete(query))
    except Exception:  # noqa: BLE001
        return False
    return status in (int(Security.errSecSuccess), int(Security.errSecItemNotFound))


def _kc_creds_get(domain: str, email: str, *, allow_prompt: bool = False) -> str | None:
    cached = _relay_cred_get_secret(domain, email, "password")
    if cached:
        return cached
    username = f"{domain}|{email}"
    if not allow_prompt:
        legacy = _security_get_password(KEYCHAIN_CREDS_SERVICE, username)
    else:
        legacy = keyring.get_password(KEYCHAIN_CREDS_SERVICE, username)
    if legacy:
        _relay_cred_set_secret(domain, email, "password", legacy)
    return legacy


def _kc_creds_delete(domain: str, email: str, *, allow_prompt: bool = False) -> None:
    try:
        _relay_cred_delete_field(domain, email, "password")
        username = f"{domain}|{email}"
        if not allow_prompt:
            _security_delete_password(KEYCHAIN_CREDS_SERVICE, username)
            return
        keyring.delete_password(KEYCHAIN_CREDS_SERVICE, username)
    except Exception:
        pass


def _sub2_login(domain: str, email: str, password: str, timeout: int = 10) -> tuple[str | None, str]:
    """POST /api/v1/auth/login {email, password} → (token, error_message).

    On success returns (token, ""). On failure returns (None, human msg).
    """
    api_base = sub2cli_lib._api_base_for_domain(domain)
    try:
        r = requests.post(
            f"{api_base}/auth/login",
            json={"email": email, "password": password},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        return None, f"网络错误: {exc}"
    try:
        body = r.json()
    except ValueError:
        return None, f"HTTP {r.status_code} (响应非 JSON: {r.text[:100]})"
    if r.status_code != 200:
        return None, body.get("message") or body.get("reason") or f"HTTP {r.status_code}"
    data = body.get("data") or {}
    # Sub2API token field name varies — try common shapes.
    token = data.get("token") or data.get("access_token") or data.get("auth_token")
    if not token and isinstance(body.get("token"), str):
        token = body["token"]
    if not token:
        return None, f"登录响应里没找到 token (data keys: {list(data.keys())})"
    return token, ""


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)


def _mask_api_key(api_key: str) -> str:
    api_key = api_key or ""
    if len(api_key) <= 12:
        return "***"
    return f"{api_key[:8]}...{api_key[-4:]}"


def _is_loopback_relay_host(host: str) -> bool:
    """True for localhost / 127.0.0.0/8 / ::1 — where cleartext http is benign."""
    h = (host or "").split(":")[0].strip("[]").lower()
    return h in ("localhost", "::1") or h.startswith("127.")


def _mask_secret_text(text: Any, api_key: str | None = None) -> str:
    masked = "" if text is None else str(text)
    if api_key:
        masked = masked.replace(api_key, _mask_api_key(api_key))
    masked = re.sub(r"sk-[A-Za-z0-9._-]{12,}", lambda m: _mask_api_key(m.group(0)), masked)
    masked = re.sub(
        r"(?i)(access|refresh|id)_token[^,}\n]*",
        lambda m: f"{m.group(1)}_token:<redacted>",
        masked,
    )
    # Relay/OAuth bearer tokens are opaque or JWT, not sk- prefixed; mask both a
    # `Bearer <token>` header form and any bare JWT so they don't leak into UI
    # error dialogs or logs from Codex CLI stderr.
    masked = re.sub(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}", "Bearer <redacted>", masked)
    masked = re.sub(
        r"eyJ[A-Za-z0-9_-]{6,}\.eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}",
        "<jwt:redacted>",
        masked,
    )
    return masked


def _comparable_api_base_url(value: str | None) -> str:
    value = (value or "").strip().rstrip("/")
    if value.lower().endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value.lower()


def _coerce_proc_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _read_available_pipe_text(pipe: Any, limit: int = 4000) -> str:
    try:
        fd = pipe.fileno()
        old_flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, old_flags | os.O_NONBLOCK)
    except Exception:
        return ""
    chunks: list[bytes] = []
    total = 0
    try:
        while total < limit:
            try:
                data = os.read(fd, min(4096, limit - total))
            except BlockingIOError:
                break
            if not data:
                break
            chunks.append(data)
            total += len(data)
    except Exception:
        return b"".join(chunks).decode(errors="replace")
    finally:
        try:
            fcntl.fcntl(fd, fcntl.F_SETFL, old_flags)
        except Exception:
            pass
    return b"".join(chunks).decode(errors="replace")


def _mask_result_texts(result: dict[str, Any] | None, api_key: str | None = None) -> dict[str, Any] | None:
    if not result:
        return result
    masked = dict(result)
    if "stdout" in masked:
        masked["stdout"] = _mask_secret_text(masked.get("stdout"), api_key)
    if "stderr" in masked:
        masked["stderr"] = _mask_secret_text(masked.get("stderr"), api_key)
    if "error" in masked and api_key:
        masked["error"] = _mask_secret_text(masked.get("error"), api_key)
    return masked


def _extract_rollback_backup(stdout: str | None) -> str | None:
    m = re.search(r"sub2cli-inject rollback ([^\s]+)", stdout or "")
    return m.group(1).strip() if m else None


def _inject_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    for key in list(env):
        if key.startswith("_PYI_"):
            env.pop(key, None)
    return env


def _rollback_inject_backup(inject_bin: str, backup_name: str | None) -> dict[str, Any] | None:
    if not backup_name:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._-]+", backup_name):
        return {
            "ok": False,
            "error": "备份名非法, 未自动回滚",
            "backup_name": backup_name,
        }
    try:
        proc = subprocess.run(
            [inject_bin, "rollback", backup_name],
            capture_output=True,
            text=True,
            env=_inject_subprocess_env(),
            timeout=INJECT_ROLLBACK_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "error": f"自动回滚超过 {INJECT_ROLLBACK_TIMEOUT} 秒仍未完成",
            "backup_name": backup_name,
            "stdout": "",
            "stderr": "",
            "returncode": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"自动回滚失败: {type(exc).__name__}: {exc}",
            "backup_name": backup_name,
        }
    return {
        "ok": proc.returncode == 0,
        "backup_name": backup_name,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }


def _inject_cli_auto_rollback_result(backup_name: str | None) -> dict[str, Any] | None:
    if not backup_name or not re.fullmatch(r"[A-Za-z0-9._-]+", backup_name):
        return None
    marker = INJECT_BACKUP_ROOT / backup_name / AUTO_ROLLBACK_MARKER
    if not marker.exists():
        return None
    try:
        detail = marker.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        detail = ""
    suffix = f" {detail}" if detail else ""
    return {
        "ok": True,
        "backup_name": backup_name,
        "stdout": f"sub2cli-inject already auto-rolled back.{suffix}",
        "stderr": "",
        "returncode": 0,
        "source": "inject-cli",
    }


def _rollback_inject_backup_if_needed(inject_bin: str, backup_name: str | None) -> dict[str, Any] | None:
    return _inject_cli_auto_rollback_result(backup_name) or _rollback_inject_backup(inject_bin, backup_name)


def _home_path(path: str | Path | None) -> str:
    if path is None:
        return "(none)"
    p = str(path)
    if p in ("", "None"):
        return "(none)"
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def _missing_value(value: Any) -> str:
    if value is None or value == "":
        return "(未设置)"
    return str(value)


def _diff_rows_to_lines(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    lines: list[dict[str, str]] = []
    for row in rows:
        label = row.get("label", "")
        before = row.get("before", "")
        after = row.get("after", "")
        if before == after:
            lines.append({"kind": "context", "text": f"  {label}: {after}"})
        else:
            lines.append({"kind": "minus", "text": f"- {label}: {before}"})
            lines.append({"kind": "plus", "text": f"+ {label}: {after}"})
    return lines


def _read_json_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_json_file(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    # Create the tmp at 0600 from the start (O_EXCL) so the secret bytes are
    # never world-readable in the window before chmod; fsync before rename for
    # crash durability.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _decode_jwt_claims(token: str | None) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        if len(payload) > 8192:
            return {}
        payload += "=" * ((4 - len(payload) % 4) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload.encode()))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _codex_auth_identity(auth_path: str | None) -> dict[str, Any]:
    if not auth_path:
        return {}
    data = _read_json_file(Path(auth_path).expanduser())
    tokens = data.get("tokens") if isinstance(data, dict) else {}
    claims = _decode_jwt_claims((tokens or {}).get("id_token"))
    auth_meta = claims.get("https://api.openai.com/auth") or {}
    last_refresh = data.get("last_refresh") if isinstance(data, dict) else None
    if not last_refresh and claims.get("iat"):
        try:
            last_refresh = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(claims["iat"])))
        except Exception:
            last_refresh = None
    return {
        "email": claims.get("email"),
        "name": claims.get("name"),
        "account_id": auth_meta.get("chatgpt_account_id") or claims.get("chatgpt_account_id"),
        "plan_type": auth_meta.get("chatgpt_plan_type") or claims.get("plan_type"),
        "last_refresh": last_refresh,
        "issued_at": claims.get("iat"),
        "expires_at": claims.get("exp"),
        "auth_mode": data.get("auth_mode") if isinstance(data, dict) else None,
    }


def _format_plan_label(plan_type: str | None) -> str:
    if not plan_type:
        return "未知套餐"
    mapping = {
        "free": "Free",
        "plus": "Plus",
        "pro": "Pro",
        "prolite": "Pro 5x",
        "team": "Team",
        "enterprise": "Enterprise",
    }
    return mapping.get(plan_type.lower(), plan_type)


def _read_text_preview(path: str, api_key: str) -> str:
    p = Path(path).expanduser()
    if not p.exists():
        return "(new file)"
    try:
        return _mask_secret_text(p.read_text()[:1600], api_key)
    except Exception as exc:
        return f"(无法读取: {type(exc).__name__})"


def _toml_section_values(path: Path, section: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text()
    except Exception:
        return values
    in_section = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = line.strip("[]") == section
            continue
        if not in_section or "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _extract_plan_field(plan_text: str, name: str) -> str | None:
    m = re.search(rf"^\s*{re.escape(name)}:\s*(.+?)\s*$", plan_text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _parse_inject_plan_changes(plan_text: str, api_key: str) -> list[dict[str, Any]]:
    """Turn sub2cli-inject's dry-run text into UI-ready diff blocks."""
    changes: list[dict[str, Any]] = []
    slot = _extract_plan_field(plan_text, "slot")
    base_url = _extract_plan_field(plan_text, "base_url")
    model = _extract_plan_field(plan_text, "model")
    slots_path = PROVIDER_SLOTS_PATH
    slots_data = _read_json_file(slots_path)

    if slot:
        existing = ((slots_data.get("slots") or {}).get(slot) or {})
        rows = [
            {
                "label": "current",
                "before": _missing_value(slots_data.get("current")),
                "after": slot,
            },
            {
                "label": f"slots.{slot}.mode",
                "before": _missing_value(existing.get("mode")),
                "after": "relay",
            },
            {
                "label": f"slots.{slot}.base_url",
                "before": _missing_value(existing.get("base_url")),
                "after": _missing_value(base_url),
            },
            {
                "label": f"slots.{slot}.model",
                "before": _missing_value(existing.get("model")),
                "after": _missing_value(model),
            },
            {
                "label": f"slots.{slot}.api_key",
                "before": _mask_secret_text(_missing_value(existing.get("api_key")), api_key),
                "after": _mask_api_key(api_key),
            },
        ]
        changes.append({
            "path": "~/.codex/provider-slots.json",
            "detail": "新增或更新当前中转的 API 渠道",
            "rows": rows,
            "diff": _diff_rows_to_lines(rows),
        })

    lines = plan_text.splitlines()
    i = 0
    codex_app_rows: list[dict[str, str]] = []
    while i < len(lines):
        line = lines[i]
        symlink = re.match(r"\s*\d+\.\s+symlink:\s+(.+?)\s*$", line)
        write = re.match(r"\s*\d+\.\s+write:\s+(.+?)\s+\((.+?)\)\s*$", line)
        patch = re.match(r"\s*\d+\.\s+patch:\s+(.+?)\s+(\[.+?\]|\(.+?\))\s*$", line)
        normalize = re.match(
            r"\s*\d+\.\s+normalize sessions → provider=(\S+)\s+\(DB rows:\s*(\d+),\s*rollout headers:\s*(\d+)\)",
            line,
        )
        quit_app = re.match(r"\s*\d+\.\s+quit Codex App\s*$", line)
        launch_app = re.match(r"\s*\d+\.\s+launch Codex App(?:\s+\[(.+?)\])?\s*$", line)

        if symlink:
            path = symlink.group(1).strip()
            from_value = None
            to_value = None
            if i + 1 < len(lines):
                m = re.match(r"\s*from:\s*(.*?)\s*$", lines[i + 1])
                from_value = m.group(1) if m else None
            if i + 2 < len(lines):
                m = re.match(r"\s*to:\s*(.*?)\s*$", lines[i + 2])
                to_value = m.group(1) if m else None
            rows = [{
                "label": "symlink target",
                "before": _home_path(from_value),
                "after": _home_path(to_value),
            }]
            changes.append({
                "path": _home_path(path),
                "detail": "切换符号链接目标",
                "rows": rows,
                "diff": _diff_rows_to_lines(rows),
            })
            i += 3
            continue

        if write:
            path = write.group(1).strip()
            rows = [{
                "label": "content",
                "before": _read_text_preview(path, api_key),
                "after": json.dumps(
                    {"OPENAI_API_KEY": _mask_api_key(api_key), "auth_mode": "apikey"},
                    ensure_ascii=False,
                ),
            }]
            changes.append({
                "path": _home_path(path),
                "detail": write.group(2).strip(),
                "rows": rows,
                "diff": [
                    {"kind": "minus", "text": f"- content: {rows[0]['before']}"},
                    {"kind": "plus", "text": f"+ content: {rows[0]['after']}"},
                ],
            })
            i += 1
            continue

        if patch:
            path = patch.group(1).strip()
            section = patch.group(2).strip().strip("[]")
            old_values = _toml_section_values(Path(path).expanduser(), section)
            new_base_url = None
            new_model = None
            j = i + 1
            while j < len(lines):
                field = re.match(r"\s*(base_url|model)\s*=\s*(.+?)\s*$", lines[j])
                if not field:
                    break
                if field.group(1) == "base_url":
                    new_base_url = field.group(2).strip()
                else:
                    new_model = field.group(2).strip()
                j += 1
            rows = []
            if new_base_url is not None:
                rows.append({
                    "label": f"{section}.base_url",
                    "before": _missing_value(old_values.get("base_url")),
                    "after": new_base_url,
                })
            if new_model is not None:
                rows.append({
                    "label": f"{section}.model",
                    "before": _missing_value(old_values.get("model")),
                    "after": new_model,
                })
            changes.append({
                "path": _home_path(path),
                "detail": f"更新 {section} 配置",
                "rows": rows,
                "diff": _diff_rows_to_lines(rows),
            })
            i = j
            continue

        if normalize:
            provider, db_rows, rollout_rows = normalize.groups()
            rows = [
                {"label": "state_5.sqlite rows", "before": f"{db_rows} rows need update", "after": f"provider={provider}"},
                {"label": "rollout headers", "before": f"{rollout_rows} headers need update", "after": f"provider={provider}"},
            ]
            changes.append({
                "path": "Codex 历史 session",
                "detail": "归一历史会话 provider, 让旧会话继续走当前渠道",
                "rows": rows,
                "diff": _diff_rows_to_lines(rows),
            })
            i += 1
            continue

        if quit_app:
            codex_app_rows.append({"label": "Codex App", "before": "running", "after": "quit"})
            i += 1
            continue

        if launch_app:
            note = launch_app.group(1)
            before = "not running" if note == "not running" else "after config write"
            codex_app_rows.append({"label": "Codex App", "before": before, "after": "launch"})
            i += 1
            continue

        i += 1

    if codex_app_rows:
        changes.append({
            "path": "Codex App",
            "detail": "重新打开以加载新配置",
            "rows": codex_app_rows,
            "diff": _diff_rows_to_lines(codex_app_rows),
        })
    return changes


def _parse_use_plan_changes(plan_text: str, slot: str) -> list[dict[str, Any]]:
    """Parse the stable parts of `sub2cli-inject use <slot> --dry-run`."""
    data = _read_json_file(PROVIDER_SLOTS_PATH)
    slots = data.get("slots") or {}
    target = slots.get(slot) or {}
    rows = [
        {
            "label": "current",
            "before": _missing_value(data.get("current")),
            "after": slot,
        },
        {
            "label": f"slots.{slot}.mode",
            "before": _missing_value(target.get("mode")),
            "after": "oauth",
        },
        {
            "label": "auth.json",
            "before": "当前 Codex 登录文件",
            "after": _home_path(target.get("auth_file")),
        },
        {
            "label": "App profile",
            "before": "当前 Codex App profile",
            "after": _home_path(target.get("app_profile_dir")),
        },
    ]
    changes = [{
        "path": "~/.codex/provider-slots.json",
        "detail": "切换到已保存的官方 Codex 账号渠道",
        "rows": rows,
        "diff": _diff_rows_to_lines(rows),
    }]
    if "当前状态一致" in plan_text or "无需修改" in plan_text:
        changes[0]["detail"] = "当前已是这个官方账号, 无需修改"
    elif "Codex App" in plan_text or "launch" in plan_text:
        changes.append({
            "path": "Codex App",
            "detail": "重新打开以加载官方账号配置",
            "rows": [{"label": "Codex App", "before": "当前状态", "after": "restart / launch"}],
            "diff": [{"kind": "plus", "text": "+ Codex App restart / launch"}],
        })
    return changes


def _official_slot_name(data: dict) -> str | None:
    slots = data.get("slots") or {}
    preferred = (data.get("preferred_official_slot") or "").strip()
    if preferred and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", preferred):
        if preferred not in slots or (slots.get(preferred) or {}).get("mode") == "oauth":
            return preferred
    current = data.get("current")
    if current in slots and slots[current].get("mode") == "oauth":
        return current
    history = data.get("app_history_slot")
    if history in slots and slots[history].get("mode") == "oauth":
        return history
    for name, cfg in slots.items():
        if cfg.get("mode") == "oauth":
            return name
    return None


def _serialize_codex_account(name: str, cfg: dict, *, current: str | None, official: str | None) -> dict:
    auth_file = cfg.get("auth_file")
    source_auth_file = cfg.get("_source_auth_file") or auth_file
    ident = _codex_auth_identity(source_auth_file or auth_file)
    email = ident.get("email") or ""
    display = cfg.get("display_name") or f"Codex - {name}"
    identity_key = (ident.get("account_id") or email or name or "").strip().lower()
    return {
        "slot": name,
        "display_name": display,
        "email": email or display.replace("Codex - ", ""),
        "name": ident.get("name") or "",
        "account_id": ident.get("account_id"),
        "identity_key": identity_key,
        "plan_type": ident.get("plan_type"),
        "plan_label": _format_plan_label(ident.get("plan_type")),
        "last_refresh": ident.get("last_refresh"),
        "issued_at": ident.get("issued_at"),
        "expires_at": ident.get("expires_at"),
        "auth_mode": ident.get("auth_mode"),
        "auth_file": _home_path(auth_file),
        "target_auth_file": _home_path(auth_file),
        "source_auth_file": _home_path(source_auth_file),
        "app_profile_dir": _home_path(cfg.get("app_profile_dir")),
        "source_label": cfg.get("_source_label") or "sub2cli slots",
        "source_kind": cfg.get("_source_kind") or "slot",
        "managed_home": _home_path(cfg.get("_managed_home")) if cfg.get("_managed_home") else "",
        "is_registered": cfg.get("_is_registered", True),
        "is_current_provider": name == current,
        "is_current_official": name == official,
    }


def _codex_identity_key(account: dict) -> str:
    key = (account.get("identity_key") or account.get("account_id") or account.get("email") or "").strip().lower()
    return key or f"slot:{account.get('slot', '')}"


def _identity_key_from_ident(ident: dict) -> str:
    return (ident.get("account_id") or ident.get("email") or "").strip().lower()


def _registered_slot_identity_key(slots: dict, slot: str) -> str:
    cfg = slots.get(slot) or {}
    if cfg.get("mode") != "oauth":
        return ""
    return _identity_key_from_ident(_codex_auth_identity(cfg.get("auth_file")))


def _can_reuse_registered_slot(slots: dict, slot: str, identity_key: str) -> bool:
    registered_key = _registered_slot_identity_key(slots, slot)
    return not registered_key or not identity_key or registered_key == identity_key


def _slot_from_auth_file(path: Path) -> str | None:
    match = re.fullmatch(r"auth\.([A-Za-z0-9][A-Za-z0-9_-]{0,30})\.json", path.name)
    if not match:
        return None
    return match.group(1).lower()


def _safe_slot_slug(value: str | None, fallback: str = "account") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    if not text or not re.match(r"^[a-z0-9]", text):
        text = fallback
    return text[:31]


def _slot_base_for_email(email: str | None, label: str | None = None) -> str:
    normalized_label = (label or "").strip()
    if normalized_label and normalized_label.lower() not in {"personal", "default"}:
        return _safe_slot_slug(normalized_label)
    if email and "@" in email:
        domain = email.split("@", 1)[1].split(".", 1)[0]
        if domain:
            return _safe_slot_slug(domain)
    if email:
        return _safe_slot_slug(email.split("@", 1)[0])
    return "account"


def _unique_slot(base: str, used: set[str]) -> str:
    slot = _safe_slot_slug(base)
    if slot not in used:
        used.add(slot)
        return slot
    for i in range(2, 100):
        suffix = f"-{i}"
        candidate = f"{slot[:31 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError("too many duplicate Codex account slots")


def _expanded_path(value: str | None) -> Path | None:
    if not value or value == "(none)":
        return None
    return Path(str(value).replace("~", str(Path.home()), 1)).expanduser()


def _source_is_newer(candidate: dict, existing: dict) -> bool:
    return int(candidate.get("issued_at") or 0) > int(existing.get("issued_at") or 0)


def _merge_codex_account(accounts_by_key: dict[str, dict], candidate: dict) -> None:
    key = _codex_identity_key(candidate)
    existing = accounts_by_key.get(key)
    if not existing:
        accounts_by_key[key] = candidate
        return

    source_labels = []
    for label in (existing.get("source_label"), candidate.get("source_label")):
        for part in str(label or "").split(" + "):
            part = part.strip()
            if part and part not in source_labels:
                source_labels.append(part)

    if candidate.get("is_registered") and not existing.get("is_registered"):
        base, extra = candidate, existing
    else:
        base, extra = existing, candidate

    if _source_is_newer(extra, base):
        base["source_auth_file"] = extra.get("source_auth_file") or base.get("source_auth_file")
        base["source_kind"] = extra.get("source_kind") or base.get("source_kind")
        base["last_refresh"] = extra.get("last_refresh") or base.get("last_refresh")
        base["issued_at"] = extra.get("issued_at") or base.get("issued_at")
        base["expires_at"] = extra.get("expires_at") or base.get("expires_at")

    base["source_label"] = " + ".join(source_labels) if source_labels else base.get("source_label", "")
    base["is_current_provider"] = bool(base.get("is_current_provider") or extra.get("is_current_provider"))
    base["is_current_official"] = bool(base.get("is_current_official") or extra.get("is_current_official"))
    base["is_registered"] = bool(base.get("is_registered") or extra.get("is_registered"))
    if not base.get("managed_home") and extra.get("managed_home"):
        base["managed_home"] = extra["managed_home"]
    accounts_by_key[key] = base


def _raw_codex_auth_accounts(
    *,
    used_slots: set[str],
    current: str | None,
    official: str | None,
    registered_slots: set[str],
    slots: dict,
) -> list[dict]:
    accounts: list[dict] = []
    for path in sorted(CODEX_HOME_PATH.glob("auth.*.json")):
        slot = _slot_from_auth_file(path)
        if not slot:
            continue
        ident = _codex_auth_identity(str(path))
        if not ident.get("email"):
            continue
        identity_key = _identity_key_from_ident(ident)
        if slot in registered_slots and _can_reuse_registered_slot(slots, slot, identity_key):
            target_slot = slot
        else:
            target_slot = _unique_slot(slot, used_slots)
        if target_slot not in used_slots:
            used_slots.add(target_slot)
        cfg = {
            "display_name": f"Codex - {ident.get('email')}",
            "mode": "oauth",
            "auth_file": str(path),
            "app_profile_dir": str(CODEX_APP_SUPPORT_PATH / f"Codex.{target_slot}"),
            "_source_auth_file": str(path),
            "_source_label": "~/.codex auth file",
            "_source_kind": "auth-file",
            "_is_registered": target_slot in registered_slots,
        }
        accounts.append(_serialize_codex_account(target_slot, cfg, current=current, official=official))
    return accounts


def _live_codex_auth_accounts(
    *,
    used_slots: set[str],
    current: str | None,
    official: str | None,
    registered_slots: set[str],
    slots: dict,
) -> list[dict]:
    """Discover the standard live ~/.codex/auth.json OAuth login.

    A fresh Codex install normally has auth.json only, without any
    auth.<slot>.json or provider-slots.json. Treat it as an importable official
    account instead of requiring this machine's private pre-registration.
    """
    auth_path = CODEX_HOME_PATH / "auth.json"
    if not auth_path.exists() or auth_path.is_symlink():
        return []
    ident = _codex_auth_identity(str(auth_path))
    if not ident.get("email") or ident.get("auth_mode") == "apikey":
        return []

    preferred = (official or "").strip()
    identity_key = _identity_key_from_ident(ident)
    if preferred in registered_slots and _can_reuse_registered_slot(slots, preferred, identity_key):
        target_slot = preferred
    elif current in registered_slots and _can_reuse_registered_slot(slots, current or "", identity_key):
        target_slot = current or ""
    else:
        if preferred and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", preferred) and preferred not in slots:
            target_slot = preferred
            used_slots.add(target_slot)
        else:
            target_slot = _unique_slot(_slot_base_for_email(ident.get("email")), used_slots)
    existing = (slots.get(target_slot) or {}) if target_slot in slots else {}
    cfg = {
        "display_name": existing.get("display_name") or f"Codex - {ident.get('email')}",
        "mode": "oauth",
        "auth_file": existing.get("auth_file") or str(CODEX_HOME_PATH / f"auth.{target_slot}.json"),
        "app_profile_dir": existing.get("app_profile_dir") or str(CODEX_APP_SUPPORT_PATH / f"Codex.{target_slot}"),
        "_source_auth_file": str(auth_path),
        "_source_label": "~/.codex/auth.json",
        "_source_kind": "live-auth",
        "_is_registered": target_slot in registered_slots,
    }
    return [_serialize_codex_account(target_slot, cfg, current=current, official=official)]


def _codexbar_managed_accounts(
    *,
    used_slots: set[str],
    current: str | None,
    official: str | None,
) -> list[dict]:
    data = _read_json_file(CODEXBAR_MANAGED_ACCOUNTS_PATH)
    raw_accounts = data.get("accounts") if isinstance(data, dict) else []
    accounts: list[dict] = []
    for entry in raw_accounts or []:
        managed_home = entry.get("managedHomePath")
        auth_path = Path(managed_home or "").expanduser() / "auth.json"
        if not managed_home or not auth_path.exists():
            continue
        ident = _codex_auth_identity(str(auth_path))
        email = ident.get("email") or entry.get("email")
        if not email:
            continue
        base = _slot_base_for_email(email, entry.get("workspaceLabel"))
        target_slot = _unique_slot(base, used_slots)
        display = f"Codex - {email}"
        if entry.get("workspaceLabel"):
            display = f"{entry['workspaceLabel']} · {email}"
        cfg = {
            "display_name": display,
            "mode": "oauth",
            "auth_file": str(CODEX_HOME_PATH / f"auth.{target_slot}.json"),
            "app_profile_dir": str(CODEX_APP_SUPPORT_PATH / f"Codex.{target_slot}"),
            "_source_auth_file": str(auth_path),
            "_source_label": "CodexBar managed account",
            "_source_kind": "codexbar-managed",
            "_managed_home": managed_home,
            "_is_registered": False,
        }
        accounts.append(_serialize_codex_account(target_slot, cfg, current=current, official=official))
    return accounts


def _discover_codex_accounts(data: dict | None = None, official_override: str | None = None) -> dict:
    data = data or _read_json_file(PROVIDER_SLOTS_PATH)
    slots = data.get("slots") or {}
    current = data.get("current")
    official = official_override or _official_slot_name(data)
    registered_slots = {name for name, cfg in slots.items() if (cfg or {}).get("mode") == "oauth"}
    used_slots = set(slots.keys())
    accounts_by_key: dict[str, dict] = {}

    for name, cfg in slots.items():
        if (cfg or {}).get("mode") != "oauth":
            continue
        _merge_codex_account(
            accounts_by_key,
            _serialize_codex_account(name, cfg, current=current, official=official),
        )

    for account in _raw_codex_auth_accounts(
        used_slots=used_slots,
        current=current,
        official=official,
        registered_slots=registered_slots,
        slots=slots,
    ):
        _merge_codex_account(accounts_by_key, account)

    for account in _live_codex_auth_accounts(
        used_slots=used_slots,
        current=current,
        official=official,
        registered_slots=registered_slots,
        slots=slots,
    ):
        _merge_codex_account(accounts_by_key, account)

    for account in _codexbar_managed_accounts(
        used_slots=used_slots,
        current=current,
        official=official,
    ):
        _merge_codex_account(accounts_by_key, account)

    accounts = list(accounts_by_key.values())
    if official and not any(account.get("slot") == official for account in accounts):
        official = None
    if not official and accounts:
        official = accounts[0].get("slot")
    for account in accounts:
        account["is_current_official"] = account.get("slot") == official

    current_provider = slots.get(current) or {}
    return {
        "ok": True,
        "accounts": accounts,
        "current": current,
        "current_mode": current_provider.get("mode"),
        "current_official": next((a for a in accounts if a.get("slot") == official), None),
        "slots_path": _home_path(PROVIDER_SLOTS_PATH),
        "codexbar_accounts_path": _home_path(CODEXBAR_MANAGED_ACCOUNTS_PATH),
    }


def _copy_auth_material(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    tmp = target.with_name(f"{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        finally:
            pass
        raise


def _path_within(path: Path, root: Path) -> bool:
    try:
        resolved = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except OSError:
        return False
    return resolved == resolved_root or resolved_root in resolved.parents


def _safe_remove_file(path: Path, root: Path) -> bool:
    if not _path_within(path, root):
        return False
    if path.is_file() or path.is_symlink():
        path.unlink()
        return True
    return False


def _safe_remove_tree(path: Path, root: Path) -> bool:
    if not _path_within(path, root):
        return False
    if path.exists() and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
        return True
    return False


def _safe_remove_codex_auth_file(path: Path | None) -> bool:
    if not path or path.name == "auth.json":
        return False
    if not re.fullmatch(r"auth\.[A-Za-z0-9][A-Za-z0-9_-]{0,30}\.json", path.name):
        return False
    return _safe_remove_file(path, CODEX_HOME_PATH)


def _remove_codexbar_managed_account(account: dict, removed: list[str]) -> bool:
    data = _read_json_file(CODEXBAR_MANAGED_ACCOUNTS_PATH)
    raw_accounts = data.get("accounts") if isinstance(data, dict) else []
    if not isinstance(raw_accounts, list):
        return False

    managed_home = _expanded_path(account.get("managed_home"))
    email = (account.get("email") or "").strip().lower()
    account_id = (account.get("account_id") or "").strip().lower()
    before = len(raw_accounts)
    kept = []
    removed_homes: list[Path] = []
    for entry in raw_accounts:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        entry_home = Path(entry.get("managedHomePath") or "").expanduser()
        entry_email = (entry.get("email") or "").strip().lower()
        entry_ids = {
            (entry.get("providerAccountID") or "").strip().lower(),
            (entry.get("workspaceAccountID") or "").strip().lower(),
        }
        matches_home = bool(managed_home and entry_home == managed_home)
        matches_identity = bool(
            (account_id and account_id in entry_ids)
            or (email and entry_email and email == entry_email)
        )
        if matches_home or matches_identity:
            if entry_home:
                removed_homes.append(entry_home)
            continue
        kept.append(entry)

    if len(kept) == before:
        return False

    data["accounts"] = kept
    _write_json_file(CODEXBAR_MANAGED_ACCOUNTS_PATH, data)
    removed.append(_home_path(CODEXBAR_MANAGED_ACCOUNTS_PATH))
    for home in removed_homes:
        try:
            if _safe_remove_tree(home, CODEXBAR_MANAGED_HOMES_PATH):
                removed.append(_home_path(home))
        except OSError:
            pass
    return True


def _ensure_codex_account_slot(slot: str) -> tuple[dict | None, str | None]:
    snapshot = _discover_codex_accounts(official_override=slot)
    account = next((a for a in snapshot.get("accounts", []) if a.get("slot") == slot), None)
    if not account:
        return None, f"未找到官方账号: {slot}"
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", slot):
        return None, "slot 非法"

    source = _expanded_path(account.get("source_auth_file") or account.get("auth_file"))
    target = _expanded_path(account.get("target_auth_file") or account.get("auth_file"))
    if not source or not source.exists():
        return None, f"账号 auth 文件不存在: {account.get('source_auth_file') or account.get('auth_file')}"
    if not target:
        target = CODEX_HOME_PATH / f"auth.{slot}.json"
    try:
        with CodexStateLock():
            data = _read_json_file(PROVIDER_SLOTS_PATH)
            data.setdefault("version", 1)
            slots = data.setdefault("slots", {})
            existing = slots.get(slot) or {}
            if existing and existing.get("mode") != "oauth":
                return None, f"slot {slot!r} 已存在但不是官方账号渠道"
            # Identity guard: never overwrite a slot that already holds a
            # DIFFERENT account's auth (account_id/email mismatch). Slot names
            # derive from email and are only made unique at discovery time, so a
            # stale slot or two managed homes mapping to the same slug could
            # otherwise write account A's auth onto a slot labeled account B.
            if existing.get("auth_file"):
                existing_ident = _codex_auth_identity(existing.get("auth_file"))
                existing_key = _identity_key_from_ident(existing_ident)
                source_key = _identity_key_from_ident(_codex_auth_identity(str(source)))
                if existing_key and source_key and existing_key != source_key:
                    return None, (
                        f"slot {slot!r} 已绑定其他账号 ({existing_ident.get('email') or existing_key})，"
                        f"拒绝覆盖"
                    )
            if source.resolve(strict=False) != target.resolve(strict=False):
                _copy_auth_material(source, target)
            slots[slot] = {
                "display_name": existing.get("display_name") or account.get("display_name") or f"Codex - {slot}",
                "mode": "oauth",
                "auth_file": str(target),
                "app_profile_dir": existing.get("app_profile_dir") or str(CODEX_APP_SUPPORT_PATH / f"Codex.{slot}"),
            }
            data["preferred_official_slot"] = slot
            _write_json_file(PROVIDER_SLOTS_PATH, data)
    except Exception as exc:
        return None, f"注册官方账号槽位失败: {type(exc).__name__}: {exc}"
    refreshed = _discover_codex_accounts(official_override=slot)
    return next((a for a in refreshed.get("accounts", []) if a.get("slot") == slot), account), None


def _codex_account_import_plan_changes(data: dict, account: dict, target: str) -> list[dict[str, Any]]:
    slots = data.get("slots") or {}
    rows = [
        {"label": "current", "before": _missing_value(data.get("current")), "after": target},
        {"label": f"slots.{target}.mode", "before": _missing_value((slots.get(target) or {}).get("mode")), "after": "oauth"},
        {"label": f"auth.{target}.json", "before": _missing_value((slots.get(target) or {}).get("auth_file")), "after": account.get("source_auth_file") or account.get("auth_file") or ""},
        {"label": "auth.json", "before": "当前 Codex 登录文件", "after": account.get("target_auth_file") or account.get("auth_file") or ""},
    ]
    return [{
        "path": "~/.codex/provider-slots.json",
        "detail": "注册已发现的官方账号槽位, 然后切换到该账号",
        "rows": rows,
        "diff": _diff_rows_to_lines(rows),
    }, {
        "path": account.get("target_auth_file") or account.get("auth_file") or f"~/.codex/auth.{target}.json",
        "detail": "同步官方账号 OAuth 登录文件",
        "rows": [{"label": "source", "before": "(未同步)", "after": account.get("source_label") or "auth file"}],
        "diff": [{"kind": "plus", "text": f"+ source: {account.get('source_label') or 'auth file'}"}],
    }, {
        "path": "Codex App",
        "detail": "写入后按 sub2cli-inject 逻辑重启/打开 Codex App",
        "rows": [{"label": "Codex App", "before": "当前状态", "after": "restart / launch"}],
        "diff": [{"kind": "plus", "text": "+ Codex App restart / launch"}],
    }]


def _run_isolated_codex_login(slot: str) -> tuple[Path | None, dict | None, str, str | None]:
    codex_path, _source = _find_codex_cli()
    if not codex_path:
        return None, None, "", "找不到 codex CLI, 无法启动登录"

    login_home = CODEX_HOME_PATH / "sub2cli-account-homes" / slot
    try:
        if login_home.exists() or login_home.is_symlink():
            removed = (
                _safe_remove_tree(login_home, CODEX_HOME_PATH / "sub2cli-account-homes")
                or _safe_remove_file(login_home, CODEX_HOME_PATH / "sub2cli-account-homes")
            )
            if not removed:
                return None, None, "", f"隔离登录目录异常, 请手动清理: {_home_path(login_home)}"
        login_home.mkdir(parents=True, exist_ok=True)
        # OAuth tokens land here transiently; pin 0700 so they aren't readable
        # via an inherited-default-mode directory.
        os.chmod(login_home, 0o700)
    except Exception as exc:
        return None, None, "", f"准备隔离登录目录失败: {type(exc).__name__}: {exc}"
    env = os.environ.copy()
    env["CODEX_HOME"] = str(login_home)
    if not env.get("PATH"):
        # No inherited PATH (e.g. launched from a GUI bundle). Build a fallback
        # from the codex binary's own dir + standard system dirs, instead of
        # hardcoding Homebrew/Intel paths that don't exist on every Mac.
        fallback_dirs = [os.path.dirname(codex_path)] if codex_path else []
        fallback_dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        seen: set[str] = set()
        env["PATH"] = ":".join(d for d in fallback_dirs if d and not (d in seen or seen.add(d)))
    try:
        proc = subprocess.run(
            [codex_path, "login"],
            capture_output=True,
            text=True,
            timeout=CODEX_LOGIN_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        output = ((exc.stdout or "") + "\n" + (exc.stderr or "")).strip()
        auth_path = login_home / "auth.json"
        ident = _codex_auth_identity(str(auth_path)) if auth_path.exists() else {}
        if ident.get("email"):
            return auth_path, ident, output, None
        return None, None, output, f"登录超过 {CODEX_LOGIN_TIMEOUT} 秒未完成"
    except Exception as exc:
        return None, None, "", f"启动 codex login 失败: {type(exc).__name__}: {exc}"

    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    auth_path = login_home / "auth.json"
    ident = _codex_auth_identity(str(auth_path)) if auth_path.exists() else {}
    if proc.returncode != 0 and not ident.get("email"):
        return None, None, output, f"codex login 返回 {proc.returncode}"
    if not auth_path.exists() or not ident.get("email"):
        return None, None, output, "codex login 完成, 但没有生成可识别的官方账号 auth.json"
    return auth_path, ident, output, None


def _codex_rpc_snapshot(
    auth_path: str | None = None,
    timeout: int = 8,
    *,
    refresh_token: bool = False,
    persist_refreshed_auth: bool = False,
) -> dict[str, Any] | None:
    codex_path, _source = _find_codex_cli()
    if not codex_path:
        return None
    cmd = [codex_path, "-s", "read-only", "-a", "untrusted", "app-server"]
    proc = None
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    temp_auth_path: Path | None = None
    source_path: Path | None = None
    try:
        env = os.environ.copy()
        if auth_path:
            temp_dir = tempfile.TemporaryDirectory()
            temp_home = Path(temp_dir.name)
            src = Path(auth_path).expanduser()
            if not src.exists():
                return {"error": f"auth file missing: {_home_path(src)}", "account": {}, "rate_limits": {}}
            source_path = src
            shutil.copy2(src, temp_home / "auth.json")
            temp_auth_path = temp_home / "auth.json"
            env["CODEX_HOME"] = str(temp_home)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        requests = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "sub2cli",
                        "title": "sub2cli",
                        "version": _read_app_version(),
                    },
                },
            },
            {"method": "initialized"},
            {"id": 2, "method": "account/read", "params": {"refreshToken": refresh_token}},
        ]
        expected_ids = {2}
        if not refresh_token:
            requests.append({"id": 3, "method": "account/rateLimits/read"})
            expected_ids.add(3)
        assert proc.stdin is not None
        for msg in requests:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()

        responses: dict[int, dict] = {}
        deadline = time.time() + timeout
        assert proc.stdout is not None
        while time.time() < deadline and not (expected_ids <= set(responses)):
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if not ready:
                continue
            line = proc.stdout.readline()
            if not line:
                break
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload.get("id"), int):
                responses[payload["id"]] = payload
        stderr = ""
        if proc.stderr is not None:
            stderr = _read_available_pipe_text(proc.stderr)
        missing = [str(i) for i in expected_ids if i not in responses]
        errors = [f"{i}: {responses[i].get('error')}" for i in expected_ids if (responses.get(i) or {}).get("error")]
        if missing or errors:
            parts = []
            if missing:
                parts.append(f"missing response id(s): {', '.join(missing)}")
            if errors:
                parts.append("; ".join(errors))
            if stderr:
                parts.append(_mask_secret_text(stderr.strip()))
            return {"error": "; ".join(parts), "account": {}, "rate_limits": {}}
        account = (responses.get(2) or {}).get("result") or {}
        limits = (responses.get(3) or {}).get("result") or {}
        if refresh_token and temp_auth_path and source_path:
            live_account = account.get("account") or {}
            if not live_account.get("email"):
                detail = (
                    _mask_secret_text(stderr.strip())
                    if stderr
                    else "Codex CLI 未返回有效官方账号，refresh token 可能已失效"
                )
                return {"error": detail, "account": account, "rate_limits": limits}
            if persist_refreshed_auth:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                proc = None
                _copy_auth_material(temp_auth_path, source_path)
            account["_refreshed_auth"] = True
            account["_refreshed_auth_path"] = _home_path(source_path)
        return {"account": account, "rate_limits": limits}
    except Exception:
        return None
    finally:
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if temp_dir:
            temp_dir.cleanup()


def _refresh_codex_auth_file(auth_path: Path) -> tuple[bool, str | None, dict[str, Any] | None]:
    snapshot = _codex_rpc_snapshot(
        str(auth_path),
        timeout=CODEX_AUTH_REFRESH_TIMEOUT,
        refresh_token=True,
        persist_refreshed_auth=True,
    )
    if snapshot is None:
        return False, "无法启动 Codex CLI 校验该官方账号，请确认 codex CLI 可用", None
    if snapshot.get("error"):
        return False, (
            "该官方账号的 CLI 登录态已失效，请重新登录后再配置。"
            f"Codex 返回: {_mask_secret_text(snapshot.get('error'))}"
        ), snapshot
    account = ((snapshot.get("account") or {}).get("account") or {})
    refreshed_ident = _codex_auth_identity(str(auth_path))
    if not account.get("email") and not refreshed_ident.get("email"):
        return False, "该官方账号没有返回可识别身份，请重新登录后再配置", snapshot
    return True, None, snapshot


def _resource_dir() -> str | None:
    """Where bundled data lives. See desktop/main.py:_resource_dir for spec."""
    import sys as _sys
    exe_dir = os.path.dirname(os.path.abspath(_sys.executable))
    if exe_dir.endswith("/Contents/MacOS"):
        return os.path.join(os.path.dirname(exe_dir), "Resources")
    if hasattr(_sys, "_MEIPASS"):
        return getattr(_sys, "_MEIPASS")
    return None


def _app_icon_path() -> str | None:
    """Locate the app icon for runtime NSApplication icon updates."""
    candidates = []
    res = _resource_dir()
    if res:
        candidates.append(os.path.join(res, "sub2cli.icns"))
    candidates.append(os.path.join(SCRIPT_DIR, "assets", "sub2cli.icns"))
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _find_sub2cli() -> str:
    """Locate sub2cli script — PyInstaller resource dir, then repo root."""
    candidates = []
    res = _resource_dir()
    if res:
        candidates.append(os.path.join(res, "pyscripts", "sub2cli"))
    candidates.append(os.path.join(REPO_ROOT, "sub2cli"))
    for p in candidates:
        if os.path.isfile(p):
            return p
    return candidates[-1]  # fall through to load-time error


SUB2CLI_PATH = _find_sub2cli()


def _load_sub2cli_lib():
    """Load sibling sub2cli (no .py extension) via explicit SourceFileLoader.

    importlib's default extension-based loader inference returns spec.loader=None
    for files without a recognized extension, so we pass it explicitly.
    """
    loader = importlib.machinery.SourceFileLoader("sub2cli_lib", SUB2CLI_PATH)
    spec = importlib.util.spec_from_loader("sub2cli_lib", loader)
    if spec is None:
        raise RuntimeError(f"cannot build spec for {SUB2CLI_PATH}")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sub2cli_lib = _load_sub2cli_lib()
Sub2Context = sub2cli_lib.Sub2Context
TokenError = sub2cli_lib.TokenError


def _mask_key(k: str) -> str:
    return sub2cli_lib.mask_key(k)


# ---- relay token cache (local first, Keychain fallback) ----

def _kc_username(domain: str, email: str) -> str:
    return f"{domain}|{email}"


def _kc_set(domain: str, email: str, token: str, *, allow_prompt: bool = False) -> bool:
    """Return True when the relay token is persisted locally (see _kc_creds_set)."""
    if _relay_cred_set_secret(domain, email, "token", token):
        return True
    username = _kc_username(domain, email)
    if not allow_prompt:
        return _security_set_password(KEYCHAIN_SERVICE, username, token)
    try:
        keyring.set_password(KEYCHAIN_SERVICE, username, token)
        return True
    except Exception:  # noqa: BLE001
        return False


def _kc_get(domain: str, email: str, *, allow_prompt: bool = False) -> str | None:
    cached = _relay_cred_get_secret(domain, email, "token")
    if cached:
        return cached
    username = _kc_username(domain, email)
    if not allow_prompt:
        legacy = _security_get_password(KEYCHAIN_SERVICE, username)
    else:
        legacy = keyring.get_password(KEYCHAIN_SERVICE, username)
    if legacy:
        _relay_cred_set_secret(domain, email, "token", legacy)
    return legacy


def _kc_delete(domain: str, email: str, *, allow_prompt: bool = False) -> None:
    try:
        _relay_cred_delete_field(domain, email, "token")
        username = _kc_username(domain, email)
        if not allow_prompt:
            _security_delete_password(KEYCHAIN_SERVICE, username)
            return
        keyring.delete_password(KEYCHAIN_SERVICE, username)
    except Exception:
        # keyring's PasswordDeleteError + macOS variants — swallow on best-effort
        pass


def _try_relogin_with_saved_creds(ctx: Any, cfg: dict) -> bool:
    """If the current relay has saved email+password, try logging in.

    Sets ctx token + writes the new token on success. Returns True iff
    a token was obtained. Tries current first, then any saved email.
    """
    ad = (cfg.get("accounts") or {}).get(ctx.domain) or {}
    candidates: list[str] = []
    cur = ad.get("current")
    if cur:
        candidates.append(cur)
    for a in ad.get("saved") or []:
        e = a.get("email")
        if e and e not in candidates:
            candidates.append(e)
    for email in candidates:
        pw = _kc_creds_get(ctx.domain, email)
        if not pw:
            continue
        token, _err = _sub2_login(ctx.domain, email, pw)
        if token:
            _kc_set(ctx.domain, email, token, allow_prompt=False)
            ctx.set_token(token)
            return True
    return False


def _auth_error_text(value: object) -> bool:
    text = str(value or "").lower()
    return any(
        token in text
        for token in (
            "401",
            "403",
            "unauthorized",
            "forbidden",
            "token",
            "auth",
            "登录",
            "未登录",
            "过期",
        )
    )


class _AutoReloginContext:
    """Sub2Context proxy that replays relay API calls once after saved-creds login.

    Sub2Context intentionally returns empty values for many failed API reads, so
    the desktop layer treats an empty management response as a possible stale
    token and retries once when saved credentials are available.
    """

    def __init__(self, ctx: Any, cfg: dict) -> None:
        self._ctx = ctx
        self._cfg = cfg

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    @property
    def token(self) -> str:
        return self._ctx.token

    def set_token(self, token: str) -> None:
        self._ctx.set_token(token)

    def clear_token(self) -> None:
        self._ctx.clear_token()

    def _call(self, method: str, *args: Any, stale: Any = None, **kwargs: Any) -> Any:
        fn = getattr(self._ctx, method)
        result = fn(*args, **kwargs)
        is_stale = stale(result) if stale else False
        if is_stale and _try_relogin_with_saved_creds(self._ctx, self._cfg):
            result = fn(*args, **kwargs)
        return result

    def fetch_user(self) -> dict | None:
        return self._call("fetch_user", stale=lambda result: not result)

    def fetch_keys(self) -> list[dict]:
        return self._call("fetch_keys", stale=lambda result: not result)

    def fetch_settings(self) -> dict | None:
        return self._call("fetch_settings", stale=lambda result: result is None)

    def fetch_groups(self) -> list[dict]:
        return self._call("fetch_groups", stale=lambda result: not result)

    def fetch_subscriptions(self) -> list[dict]:
        return self._call("fetch_subscriptions", stale=lambda result: not result)

    def fetch_history(self, limit: int = 10) -> list[dict]:
        return self._call("fetch_history", limit, stale=lambda result: not result)

    def create_key(self, name: str = "sub2cli") -> dict | None:
        return self._call("create_key", name, stale=lambda result: result is None)

    def update_key_group(self, key_id: int, group_id: int) -> tuple[bool, str]:
        return self._call(
            "update_key_group",
            key_id,
            group_id,
            stale=lambda result: (
                isinstance(result, tuple)
                and len(result) >= 2
                and not bool(result[0])
                and _auth_error_text(result[1])
            ),
        )


def _accounts_for_domain(cfg: dict, domain: str) -> dict:
    """Mutable accounts entry for domain in cfg.

    Schema: {"current": email|None, "saved": [{email, last_verified}, ...]}
    Lives at cfg["accounts"][domain] — separate from cfg["relays"][domain]
    so sub2cli_lib.save_config() (which overwrites relays[domain] with
    RELAY_FIELDS only) doesn't clobber it.
    """
    accounts = cfg.setdefault("accounts", {})
    return accounts.setdefault(domain, {"current": None, "saved": []})


# ---- custom OpenAI-compatible APIs (user-supplied url + key) ----
#
# Unlike relays (Sub2API instances with login/token machinery), a custom API is
# just a bare OpenAI-compatible endpoint: a base_url + api_key the user pastes
# in. Metadata (id / name / base_url / model_columns) lives at cfg["custom_apis"]
# — a top-level list that sub2cli_lib.save_config() never touches (it only
# rewrites cfg["relays"][domain]). The api_key itself is kept in Keychain, never
# in the plaintext config, mirroring how relay tokens/creds are stored.

def _custom_apis(cfg: dict) -> list[dict]:
    apis = cfg.get("custom_apis")
    if not isinstance(apis, list):
        apis = []
        cfg["custom_apis"] = apis
    return apis


def _find_custom_api(cfg: dict, api_id: str) -> dict | None:
    for entry in _custom_apis(cfg):
        if isinstance(entry, dict) and entry.get("id") == api_id:
            return entry
    return None


def _kc_custom_api_set(api_id: str, api_key: str) -> bool:
    return _security_set_password(KEYCHAIN_CUSTOM_API_SERVICE, api_id, api_key)


def _kc_custom_api_get(api_id: str) -> str | None:
    return _security_get_password(KEYCHAIN_CUSTOM_API_SERVICE, api_id)


def _kc_custom_api_delete(api_id: str) -> None:
    try:
        _security_delete_password(KEYCHAIN_CUSTOM_API_SERVICE, api_id)
    except Exception:
        pass


def _custom_api_id_slug(value: str | None, fallback: str = "api") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "-", text).strip("-_")
    if not text or not re.match(r"^[a-z0-9]", text):
        text = fallback
    return text[:24]


def _custom_api_base_slug(base_url: str, name: str = "") -> str:
    if name.strip():
        return _custom_api_id_slug(name)
    host = ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(base_url).netloc or "").split(":")[0]
    except Exception:
        host = ""
    host = host.replace("www.", "")
    return _custom_api_id_slug(host or base_url)


def _unique_custom_api_id(base: str, used: set[str]) -> str:
    slug = _custom_api_id_slug(base)
    if slug not in used:
        used.add(slug)
        return slug
    for i in range(2, 1000):
        suffix = f"-{i}"
        candidate = f"{slug[:24 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    fallback = f"{slug[:18]}-{os.urandom(3).hex()}"
    used.add(fallback)
    return fallback


# ---- route pools (priority-ordered local failover/fallback targets) ----
#
# Saved pool metadata keeps only route references: relay key ids or custom API
# ids, endpoint/model/protocol/policy, and user-facing labels. API keys are
# resolved only when applying the pool, then passed to sub2cli-inject through a
# short-lived 0600 temp JSON file that is deleted immediately after use.

def _route_pools(cfg: dict) -> list[dict]:
    pools = cfg.get("route_pools")
    if not isinstance(pools, list):
        pools = []
        cfg["route_pools"] = pools
    return pools


def _mark_route_pool_config_changed(cfg: dict) -> None:
    cfg[sub2cli_lib.CONFIG_MUTATED_FIELDS_KEY] = list(sub2cli_lib.ROUTE_POOL_FIELDS)


def _find_route_pool(cfg: dict, pool_id: str) -> dict | None:
    for entry in _route_pools(cfg):
        if isinstance(entry, dict) and entry.get("id") == pool_id:
            return entry
    return None


def _route_pool_id_slug(value: str | None, fallback: str = "pool") -> str:
    text = (value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_.-]+", "-", text).strip("-_.")
    if not text or not re.match(r"^[a-z0-9]", text):
        text = fallback
    return text[:48]


def _unique_route_pool_id(base: str, used: set[str]) -> str:
    slug = _route_pool_id_slug(base, "pool")
    if slug not in used:
        used.add(slug)
        return slug
    for i in range(2, 1000):
        suffix = f"-{i}"
        candidate = f"{slug[:48 - len(suffix)]}{suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    fallback = f"{slug[:40]}-{os.urandom(3).hex()}"
    used.add(fallback)
    return fallback


def _route_pool_policy(policy: dict | None = None) -> dict:
    raw = policy if isinstance(policy, dict) else {}

    def _int(name: str, default: int, min_value: int, max_value: int) -> int:
        try:
            value = int(raw.get(name, default))
        except (TypeError, ValueError):
            value = default
        return max(min_value, min(max_value, value))

    def _cooldowns(default: list[int]) -> list[int]:
        value = raw.get("cooldown_seconds", default)
        items = value if isinstance(value, list) else [value]
        out: list[int] = []
        for item in items:
            try:
                out.append(max(5, min(3600, int(item))))
            except (TypeError, ValueError):
                continue
        return out or default

    return {
        "fail_consecutive": _int("fail_consecutive", 2, 1, 20),
        "recovery_successes": _int("recovery_successes", 2, 1, 20),
        "cooldown_seconds": _cooldowns([60, 120, 300]),
        "min_dwell_seconds": _int("min_dwell_seconds", 60, 0, 3600),
        "probe_interval_seconds": _int("probe_interval_seconds", 90, 5, 3600),
        "current_probe_interval_seconds": _int("current_probe_interval_seconds", 0, 0, 3600),
        "rate_limit_cooldown_seconds": _int("rate_limit_cooldown_seconds", 120, 1, 7200),
    }


def _route_pool_log_lines(limit: int = 120) -> list[str]:
    try:
        if not ROUTE_POOL_LOG_PATH.exists():
            return []
        lines = ROUTE_POOL_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        lines = [line for line in lines if _is_route_pool_event_log(line)]
        return lines[-max(1, min(500, int(limit))):]
    except Exception as exc:  # noqa: BLE001
        return [f"[WARN] route pool log read failed: {type(exc).__name__}: {exc}"]


def _is_route_pool_event_log(line: str) -> bool:
    return bool(re.search(r"\bpool (?:route|monitor)\b", str(line), flags=re.IGNORECASE))


def _sanitize_route_pool_route(route: dict, index: int) -> dict | None:
    if not isinstance(route, dict):
        return None
    source_type = str(route.get("source_type") or route.get("type") or "").strip().lower()
    if source_type not in {"relay", "custom"}:
        return None
    route_id = _route_pool_id_slug(route.get("id") or route.get("label") or f"route-{index}", f"route-{index}")
    label = str(route.get("label") or route.get("name") or route_id).strip() or route_id
    try:
        priority = int(route.get("priority", index * 10))
    except (TypeError, ValueError):
        priority = index * 10
    protocol = str(route.get("protocol") or ("chat" if source_type == "custom" else "responses")).strip().lower()
    if protocol not in {"responses", "chat"}:
        protocol = "chat" if source_type == "custom" else "responses"
    clean = {
        "id": route_id,
        "label": label[:120],
        "source_type": source_type,
        "priority": max(1, min(100000, priority)),
        "protocol": protocol,
        "model": str(route.get("model") or "").strip(),
        "notes": str(route.get("notes") or "").strip()[:240],
    }
    if source_type == "relay":
        key_id = route.get("key_id")
        if key_id in (None, ""):
            return None
        relay_domain = str(route.get("relay_domain") or "").strip()
        if relay_domain:
            relay_domain = sub2cli_lib._normalize_domain(relay_domain)
        clean.update({
            "relay_domain": relay_domain,
            "key_id": key_id,
            "key_name": str(route.get("key_name") or "").strip(),
            "key_masked": str(route.get("key_masked") or "").strip(),
            "group_id": route.get("group_id"),
            "group_name": str(route.get("group_name") or "").strip(),
            "group_rate": route.get("group_rate"),
            "endpoint_name": str(route.get("endpoint_name") or "").strip(),
            "base_url": str(route.get("base_url") or "").strip(),
        })
    else:
        api_id = str(route.get("custom_api_id") or route.get("source_id") or "").strip()
        if not api_id:
            return None
        clean.update({
            "custom_api_id": api_id,
            "custom_api_name": str(route.get("custom_api_name") or "").strip(),
            "base_url": str(route.get("base_url") or "").strip(),
        })
    return clean


def _sanitize_route_pool(pool: dict, *, existing_id: str | None = None, used_ids: set[str] | None = None) -> dict:
    if not isinstance(pool, dict):
        pool = {}
    name = str(pool.get("name") or pool.get("id") or "连接池").strip() or "连接池"
    used = used_ids if used_ids is not None else set()
    requested_id = str(pool.get("id") or existing_id or name).strip()
    pool_id = existing_id or _unique_route_pool_id(requested_id or name, used)
    routes = []
    seen_routes: set[str] = set()
    for idx, route in enumerate(pool.get("routes") or [], 1):
        clean = _sanitize_route_pool_route(route, idx)
        if not clean:
            continue
        rid = clean["id"]
        if rid in seen_routes:
            rid = _unique_route_pool_id(rid, seen_routes)
            clean["id"] = rid
        seen_routes.add(rid)
        routes.append(clean)
    routes.sort(key=lambda item: int(item.get("priority", 99999)))
    return {
        "id": pool_id,
        "name": name[:80],
        "description": str(pool.get("description") or "").strip()[:240],
        "model": str(pool.get("model") or "").strip(),
        "policy": _route_pool_policy(pool.get("policy") if isinstance(pool.get("policy"), dict) else {}),
        "routes": routes,
        "updated_at": int(time.time()),
        "created_at": int(pool.get("created_at") or time.time()),
    }


def _mask_many_secret_text(text: str, secrets: list[str]) -> str:
    masked = text
    for secret in secrets:
        if secret:
            masked = _mask_secret_text(masked, secret)
    return masked


def _mask_many_result_texts(result: dict | None, secrets: list[str]) -> dict | None:
    if not isinstance(result, dict):
        return result
    masked = dict(result)
    for key in ("stdout", "stderr", "error", "plan_text"):
        if key in masked and isinstance(masked[key], str):
            masked[key] = _mask_many_secret_text(masked[key], secrets)
    if isinstance(masked.get("auto_rollback"), dict):
        masked["auto_rollback"] = _mask_many_result_texts(masked["auto_rollback"], secrets)
    return masked


class JsApi:
    """Thread-safe-ish wrapper around Sub2Context for the pywebview bridge.

    pywebview invokes each js_api method in a worker thread, so we serialize
    Sub2Context mutations behind a lock (the requests session is otherwise
    not safe for concurrent use across threads).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._group_test_lock = threading.Lock()
        self._ctx: Any | None = None
        self._cfg: dict | None = None
        self._user: dict | None = None
        self._default_key: dict | None = None
        self._codex_key: dict | None = None
        self._default_ep: dict | None = None
        self._models: list[str] = []
        # cache of {domain: (token, email)} captured by probe_relay so the
        # subsequent add_relay can use it without re-reading Edge.
        self._pending_tokens: dict[str, tuple[str, str | None]] = {}

    # ---- internal helpers ----

    def _ensure_ctx(self, *, use_keychain: bool = True) -> tuple[Any, dict]:
        """Load config and create Sub2Context if not already cached.

        Bootstrap can pass use_keychain=False so app startup is not blocked by
        macOS SecItemCopyMatching if a keychain item is stale or locked.
        """
        if self._ctx is not None and self._cfg is not None:
            return self._ctx, self._cfg
        cfg = sub2cli_lib.load_config()
        if not cfg or not cfg.get("domain"):
            # cfg can exist without a relay domain (e.g. only custom APIs saved),
            # so guard on domain too — Sub2Context(None) would otherwise crash.
            raise RuntimeError(
                "尚未配置 — 请先在终端运行 ./sub2cli 跑首次配置向导, "
                "或在桌面 GUI 后续版本中走内置 wizard (P3)。"
            )
        ctx = Sub2Context(cfg["domain"])
        if use_keychain:
            ad = (cfg.get("accounts") or {}).get(ctx.domain) or {}
            current_email = ad.get("current")
            if current_email:
                saved_token = _kc_get(ctx.domain, current_email)
                if saved_token:
                    ctx.set_token(saved_token)
        self._ctx = _AutoReloginContext(ctx, cfg)
        self._cfg = cfg
        return self._ctx, cfg

    @staticmethod
    def _serialize_endpoint(ep: dict) -> dict:
        return {
            "name": ep.get("name", "?"),
            "endpoint": ep.get("endpoint", ""),
            "description": ep.get("description", ""),
        }

    @staticmethod
    def _serialize_key(k: dict, *, reveal: bool = False) -> dict:
        raw_key = k.get("key", "") or ""
        g = k.get("group") or {}
        return {
            "id": k.get("id"),
            "name": k.get("name", ""),
            "key_masked": _mask_key(raw_key),
            "key": raw_key if reveal else None,
            "status": k.get("status", "?"),
            "group_id": k.get("group_id"),
            "group_name": g.get("name"),
            "group_rate": g.get("rate_multiplier"),
            "is_test_key": k.get("name") == sub2cli_lib.TEST_KEY_NAME,
            "is_codex_key": bool(k.get("_is_codex_key")),
        }

    @staticmethod
    def _relay_site_label(domain: str) -> str:
        return (
            sub2cli_lib._normalize_domain(domain)
            .replace("https://", "")
            .replace("http://", "")
            .rstrip("/")
        )

    @staticmethod
    def _relay_saved_emails(cfg: dict, domain: str) -> list[str]:
        ad = (cfg.get("accounts") or {}).get(domain) or {}
        emails: list[str] = []
        current = ad.get("current")
        if current:
            emails.append(current)
        for account in ad.get("saved") or []:
            email = account.get("email") if isinstance(account, dict) else None
            if email and email not in emails:
                emails.append(email)
        return emails

    def _relay_ctx_for_domain(self, cfg: dict, domain: str) -> Any:
        """Build an authenticated context for a saved relay without switching cfg."""
        current = cfg.get("domain")
        if current == domain and self._ctx is not None:
            ctx, _cfg = self._ensure_ctx()
            return ctx
        ctx = Sub2Context(domain)
        for email in self._relay_saved_emails(cfg, domain):
            token = _kc_get(domain, email)
            if token:
                ctx.set_token(token)
                return _AutoReloginContext(ctx, cfg)
        if _try_relogin_with_saved_creds(ctx, cfg):
            return _AutoReloginContext(ctx, cfg)
        raise RuntimeError("需要先登录该中转站")

    def _route_pool_relay_source(self, cfg: dict, domain: str, *, load: bool = False) -> dict:
        relays = cfg.get("relays") or {}
        relay_cfg = relays.get(domain) or {}
        source = {
            "domain": domain,
            "site": self._relay_site_label(domain),
            "is_current": domain == cfg.get("domain"),
            "loaded": False,
            "keys": [],
            "groups": [],
            "endpoints": [],
            "current_endpoint_url": relay_cfg.get("default_endpoint_url") or "",
            "current_endpoint_name": relay_cfg.get("default_endpoint_name") or "",
            "error": "",
        }
        if not load:
            return source
        try:
            ctx = self._relay_ctx_for_domain(cfg, domain)
            keys = ctx.fetch_keys()
            codex_key = sub2cli_lib.find_codex_key(keys, relay_cfg)
            keys = self._mark_codex_key(keys, codex_key)
            groups = ctx.fetch_groups()
            settings = ctx.fetch_settings() or {}
            endpoints = sub2cli_lib.collect_endpoints(settings, fallback_site=ctx.domain)
            source.update({
                "loaded": True,
                "keys": [self._serialize_key(k) for k in keys],
                "groups": [self._serialize_group(g) for g in groups],
                "endpoints": [self._serialize_endpoint(e) for e in endpoints],
                "current_endpoint_url": relay_cfg.get("default_endpoint_url") or "",
                "current_endpoint_name": relay_cfg.get("default_endpoint_name") or "",
                "error": "",
            })
        except Exception as exc:  # noqa: BLE001
            source["error"] = f"需先登录或刷新: {exc}"
        return source

    @staticmethod
    def _same_key_id(left: Any, right: Any) -> bool:
        return str(left) == str(right)

    @staticmethod
    def _mark_codex_key(keys: list[dict], codex_key: dict | None) -> list[dict]:
        if not codex_key:
            return keys
        codex_id = codex_key.get("id")
        for key in keys:
            key["_is_codex_key"] = str(key.get("id")) == str(codex_id)
        return keys

    @staticmethod
    def _serialize_group(g: dict) -> dict:
        return {
            "id": g.get("id"),
            "name": g.get("name", "?"),
            "rate_multiplier": g.get("rate_multiplier"),
            "description": g.get("description"),
        }

    @staticmethod
    def _serialize_subscription(s: dict) -> dict:
        group = s.get("group") or {}
        return {
            "id": s.get("id"),
            "status": s.get("status"),
            "starts_at": s.get("starts_at"),
            "expires_at": s.get("expires_at"),
            "updated_at": s.get("updated_at"),
            "group_id": s.get("group_id") or group.get("id"),
            "group_name": group.get("name") or s.get("group_name") or "?",
            "group_status": group.get("status"),
            "rate_multiplier": group.get("rate_multiplier"),
            "daily_usage_usd": s.get("daily_usage_usd"),
            "weekly_usage_usd": s.get("weekly_usage_usd"),
            "monthly_usage_usd": s.get("monthly_usage_usd"),
            "daily_window_start": s.get("daily_window_start"),
            "weekly_window_start": s.get("weekly_window_start"),
            "monthly_window_start": s.get("monthly_window_start"),
            "daily_limit_usd": group.get("daily_limit_usd"),
            "weekly_limit_usd": group.get("weekly_limit_usd"),
            "monthly_limit_usd": group.get("monthly_limit_usd"),
        }

    @staticmethod
    def _serialize_custom_api(entry: dict, *, current_id: str | None = None, key: str | None = None) -> dict:
        api_id = entry.get("id")
        if key is None:
            key = _kc_custom_api_get(api_id) or ""
        return {
            "id": api_id,
            "name": entry.get("name") or entry.get("id") or "?",
            "base_url": entry.get("base_url", ""),
            "key_masked": _mask_api_key(key) if key else "(未保存)",
            "has_key": bool(key),
            "model_columns": entry.get("model_columns") or [],
            "is_current": api_id == current_id,
        }

    def _import_edge_account_for_domain(self, domain: str) -> dict:
        with self._lock:
            cfg = sub2cli_lib.load_config()
            if not cfg:
                return {"ok": False, "error": "尚未配置"}
            relays = cfg.get("relays") or {}
            if domain not in relays:
                return {"ok": False, "error": f"未保存的 relay: {domain}"}
            ctx = Sub2Context(domain)
            try:
                token = ctx.token
            except TokenError as exc:
                return {"ok": False, "error": str(exc), "needs_login": True, "domain": domain}
            ctx.set_token(token)
            user = ctx.fetch_user()
            if not user or not user.get("email"):
                return {"ok": False, "error": "/auth/me 拿不到 email", "needs_login": True, "domain": domain}
            email = user["email"]
            _kc_set(domain, email, token)
            ad = _accounts_for_domain(cfg, domain)
            existing = next((a for a in ad["saved"] if a.get("email") == email), None)
            now = int(time.time())
            if existing:
                existing["last_verified"] = now
            else:
                ad["saved"].append({"email": email, "last_verified": now})
            ad["current"] = email
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
            if self._ctx is not None and self._ctx.domain == domain:
                self._ctx.set_token(token)
                self._cfg = cfg
                self._user = user
            return {"ok": True, "email": email, "domain": domain}

    # ---- exposed bridge methods ----

    def hello(self) -> dict:
        return {
            "app": "sub2cli desktop",
            "version": _read_app_version(),
            "sub2cli_module_path": SUB2CLI_PATH,
            "default_config_path": sub2cli_lib.default_config_path(),
            "default_domain": sub2cli_lib.DEFAULT_DOMAIN,
            "config_exists": os.path.exists(sub2cli_lib.default_config_path()),
        }

    def check_update(self) -> dict:
        """Check GitHub Releases for a newer version than the running .app.

        Returns {ok, current, latest, has_update, html_url, download_url,
        asset_name, error?}. Network failures and missing releases return
        ok=True with has_update=False so the frontend can silently swallow them.
        """
        current = _read_app_version()
        body, error = _fetch_latest_release()
        if error or not body:
            return {"ok": True, "current": current, "latest": None,
                    "has_update": False, "html_url": None,
                    "download_url": None, "asset_name": None,
                    "error": error or "读取 release 失败"}
        tag = (body.get("tag_name") or "").lstrip("v")
        html_url = body.get("html_url")
        asset = _release_dmg_asset(body)
        has_update = False
        if tag and current not in ("dev", "?"):
            try:
                has_update = _parse_semver(tag) > _parse_semver(current)
            except Exception:
                has_update = False
        return {
            "ok": True,
            "current": current,
            "latest": tag or None,
            "has_update": has_update,
            "html_url": html_url,
            "download_url": asset.get("browser_download_url") if asset else None,
            "asset_name": asset.get("name") if asset else None,
            "release_name": body.get("name"),
            "can_install": bool(has_update and asset and asset.get("browser_download_url")),
        }

    def install_update(self) -> dict:
        """Download the latest DMG, stage its .app, then hand off replacement.

        The running process cannot safely replace its own .app bundle. This
        method prepares the update and launches a tiny detached shell script
        that quits sub2cli, swaps the bundle, and reopens the app.
        """
        current = _read_app_version()
        release, error = _fetch_latest_release(timeout=12)
        if error or not release:
            return {"ok": False, "error": error or "读取 release 失败"}
        latest = (release.get("tag_name") or "").lstrip("v")
        if not latest:
            return {"ok": False, "error": "latest release 缺少 tag"}
        if current not in ("dev", "?") and _parse_semver(latest) <= _parse_semver(current):
            return {"ok": False, "error": f"当前已是最新版本 v{current}"}
        asset = _release_dmg_asset(release)
        if not asset:
            return {"ok": False, "error": f"v{latest} release 没有可下载 DMG"}
        download_url = asset.get("browser_download_url")
        if not download_url:
            return {"ok": False, "error": "release DMG 缺少下载 URL"}
        target_app = _running_app_bundle_path() or Path("/Applications/sub2cli.app")
        if target_app.name != "sub2cli.app":
            return {"ok": False, "error": f"当前 app 路径不支持自动更新: {target_app}"}
        try:
            UPDATE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            asset_name = str(asset.get("name") or f"sub2cli-{latest}.dmg")
            if "/" in asset_name or asset_name.startswith("."):
                asset_name = f"sub2cli-{latest}.dmg"
            dmg_path = UPDATE_CACHE_DIR / asset_name
            _download_release_asset(download_url, dmg_path, timeout=30)
            staged_app = _stage_update_app_from_dmg(dmg_path, latest)
            script_path = _write_update_script(
                staged_app=staged_app,
                target_app=target_app,
                latest=latest,
            )
            subprocess.Popen(
                ["nohup", str(script_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": True,
            "current": current,
            "latest": latest,
            "asset_name": asset_name,
            "message": "已下载更新，正在退出并替换 sub2cli.app",
            "log_path": str(UPDATE_LOG_PATH),
        }

    def customize_chrome(self) -> dict:
        """Force dark appearance + transparent titlebar + full-size content
        view on the main NSWindow so the native white title bar disappears
        and our dark #11141a header reaches the very top of the window.
        Also set the runtime NSApplication icon so Stage Manager and app
        switcher thumbnails use the same icon as the Dock.

        Called from JS at pywebviewready time — by then NSWindow is fully
        materialized, avoiding the race we hit when binding to events.shown.
        """
        try:
            import AppKit  # type: ignore
            from AppKit import NSApp, NSAppearance, NSColor, NSImage  # type: ignore
            from Foundation import NSMakeRect, NSMakeSize, NSOperationQueue  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"AppKit import failed: {exc}"}

        def _apply() -> None:
            NSWindowStyleMaskFullSizeContentView = 1 << 15
            NSWindowTitleHidden = 1
            NSViewWidthSizable = getattr(AppKit, "NSViewWidthSizable", 2)
            NSViewHeightSizable = getattr(AppKit, "NSViewHeightSizable", 16)
            bg = NSColor.colorWithSRGBRed_green_blue_alpha_(
                17.0 / 255.0, 20.0 / 255.0, 26.0 / 255.0, 1.0
            )
            clear = NSColor.clearColor()
            try:
                dark = NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua")
            except Exception:
                dark = None
            icon_path = _app_icon_path()
            if icon_path:
                try:
                    icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
                    if icon is not None:
                        NSApp.setApplicationIconImage_(icon)
                except Exception:
                    pass

            def _class_name(obj: Any) -> str:
                try:
                    return str(obj.className())
                except Exception:
                    return str(type(obj))

            def _is_titlebar_view(obj: Any) -> bool:
                name = _class_name(obj).lower()
                return "titlebar" in name or "title bar" in name

            def _is_visual_effect_view(obj: Any) -> bool:
                try:
                    return bool(obj.isKindOfClass_(AppKit.NSVisualEffectView))
                except Exception:
                    return _class_name(obj) == "NSVisualEffectView"

            def _clear_view_background(view: Any) -> None:
                try:
                    if view.respondsToSelector_("setBackgroundColor:"):
                        view.setBackgroundColor_(clear)
                except Exception:
                    pass
                try:
                    view.setWantsLayer_(True)
                    layer = view.layer()
                    if layer is not None:
                        layer.setBackgroundColor_(clear.CGColor())
                except Exception:
                    pass

            def _sanitize_titlebar_tree(view: Any, in_titlebar: bool = False) -> None:
                is_titlebar = in_titlebar or _is_titlebar_view(view)
                if is_titlebar:
                    _clear_view_background(view)

                # pywebview sets the native titlebar view to system
                # windowBackgroundColor in its non-frameless path. On macOS 13+
                # that can leave an NSVisualEffectView/vibrancy strip above
                # the WKWebView even after fullSizeContentView is enabled.
                # Hide only the visual-effect leaf; keeping the titlebar
                # container itself preserves native traffic lights.
                if is_titlebar and _is_visual_effect_view(view):
                    try:
                        view.setHidden_(True)
                    except Exception:
                        pass

                try:
                    children = list(view.subviews())
                except Exception:
                    children = []
                for child in children:
                    _sanitize_titlebar_tree(child, is_titlebar)

            def _show_native_buttons(w: Any) -> None:
                """Show + nudge traffic lights down-right so they sit centered
                in our 56px header instead of clinging to the top edge."""
                # NSWindow coord is bottom-up; default button y ~= contentHeight - 16
                # We want ~20 from the top of the window in our 56px header strip.
                target_y_from_top = 23
                try:
                    content = w.contentView()
                    content_h = content.bounds().size.height if content else None
                except Exception:
                    content_h = None
                base_x = 13.0  # default ~7; nudge right a touch
                gap = 20.0
                positions = [
                    (base_x, target_y_from_top),
                    (base_x + gap, target_y_from_top),
                    (base_x + 2 * gap, target_y_from_top),
                ]
                for idx, button_id in enumerate((
                    getattr(AppKit, "NSWindowCloseButton", 0),
                    getattr(AppKit, "NSWindowMiniaturizeButton", 1),
                    getattr(AppKit, "NSWindowZoomButton", 2),
                )):
                    try:
                        button = w.standardWindowButton_(button_id)
                        if button is None:
                            continue
                        button.setHidden_(False)
                        button.setEnabled_(True)
                        if content_h is not None:
                            x, y_from_top = positions[idx]
                            # button's superview is the titlebar; setFrameOrigin uses
                            # superview's bottom-up coord. titlebar height ~= 28px,
                            # so y from titlebar bottom = titlebar_h - y_from_top.
                            try:
                                tbar = button.superview()
                                tbar_h = tbar.bounds().size.height if tbar else 28.0
                            except Exception:
                                tbar_h = 28.0
                            new_origin = (x, tbar_h - y_from_top - 14)  # 14 = button h
                            try:
                                button.setFrameOrigin_(new_origin)
                            except Exception:
                                pass
                    except Exception:
                        pass

            def _is_main_native_window(w: Any) -> bool:
                try:
                    return str(w.title() or "") == "sub2cli"
                except Exception:
                    return False

            def _ensure_visible_frame(w: Any) -> None:
                try:
                    screen = w.screen() or AppKit.NSScreen.mainScreen()
                    visible = screen.visibleFrame()
                    frame = w.frame()
                except Exception:
                    return

                min_w = 960.0
                min_h = 680.0
                target_w = min(1180.0, max(min_w, visible.size.width - 80.0))
                target_h = min(820.0, max(min_h, visible.size.height - 80.0))
                center_x = frame.origin.x + frame.size.width / 2.0
                center_y = frame.origin.y + frame.size.height / 2.0
                offscreen = (
                    center_x < visible.origin.x
                    or center_x > visible.origin.x + visible.size.width
                    or center_y < visible.origin.y
                    or center_y > visible.origin.y + visible.size.height
                )
                too_small = frame.size.width < min_w or frame.size.height < min_h
                if not offscreen and not too_small:
                    return

                x = visible.origin.x + max(20.0, (visible.size.width - target_w) / 2.0)
                y = visible.origin.y + max(20.0, (visible.size.height - target_h) / 2.0)
                try:
                    w.setFrame_display_(NSMakeRect(x, y, target_w, target_h), True)
                    w.makeKeyAndOrderFront_(None)
                    NSApp.activateIgnoringOtherApps_(True)
                except Exception:
                    pass

            def _sanitize_window(w: Any) -> None:
                try:
                    content = w.contentView()
                    frame_view = content.superview() if content is not None else None
                except Exception:
                    content = None
                    frame_view = None

                if frame_view is not None:
                    try:
                        content.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
                        content.setFrame_(frame_view.bounds())
                    except Exception:
                        pass

                    try:
                        subviews = list(frame_view.subviews())
                    except Exception:
                        subviews = []
                    for view in subviews:
                        if _is_titlebar_view(view):
                            _sanitize_titlebar_tree(view, True)

                    # pywebview 6.2.1 explicitly paints the frame view's last
                    # subview in non-frameless windows. Clear it as a direct
                    # override in case the private class name changes.
                    if subviews:
                        last = subviews[-1]
                        if content is None or last != content:
                            _clear_view_background(last)
                            _sanitize_titlebar_tree(last, True)

                _show_native_buttons(w)
                _ensure_visible_frame(w)

            for w in NSApp.windows():
                try:
                    if not _is_main_native_window(w):
                        try:
                            w.orderOut_(None)
                        except Exception:
                            pass
                        continue
                    if dark is not None:
                        w.setAppearance_(dark)
                    w.setContentMinSize_(NSMakeSize(960.0, 680.0))
                    w.setContentMaxSize_(NSMakeSize(1280.0, 860.0))
                    w.setBackgroundColor_(bg)
                    w.setTitlebarAppearsTransparent_(True)
                    w.setStyleMask_(w.styleMask() | NSWindowStyleMaskFullSizeContentView)
                    w.setTitleVisibility_(NSWindowTitleHidden)
                    # also kill the 1px separator under the titlebar
                    try:
                        w.setTitlebarSeparatorStyle_(0)  # NSTitlebarSeparatorStyleNone
                    except Exception:
                        pass
                    _sanitize_window(w)
                except Exception:
                    pass

        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
            NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def start_drag(self) -> dict:
        """Start a native NSWindow drag for the currently-processed mouse event.

        WKWebView doesn't honor `-webkit-app-region: drag` (Chromium-only),
        so the JS side intercepts header mousedown and calls this to hand off
        to the AppKit drag machinery. Uses NSApp.currentEvent() which, when
        dispatched immediately on the main queue, still references the same
        mouseDown event that triggered the JS handler.
        """
        try:
            from AppKit import NSApp  # type: ignore
            from Foundation import NSOperationQueue  # type: ignore
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"AppKit import: {exc}"}

        def _apply() -> None:
            evt = NSApp.currentEvent()
            if evt is None:
                return
            for w in NSApp.windows():
                try:
                    if w.isKeyWindow():
                        w.performWindowDragWithEvent_(evt)
                        return
                except Exception:
                    pass
            # fallback: first window
            wins = NSApp.windows()
            if wins:
                try:
                    wins[0].performWindowDragWithEvent_(evt)
                except Exception:
                    pass

        try:
            NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def open_url(self, url: str) -> dict:
        """Open a URL in the user's default browser (used for release links)."""
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "URL 非法"}
        try:
            subprocess.run(["open", url], check=False)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def list_relays(self) -> list[str]:
        cfg = sub2cli_lib.load_config()
        if not cfg:
            return []
        return list((cfg.get("relays") or {}).keys())

    def bootstrap(self) -> dict:
        """First load: account + test key + Codex key + default endpoint + lists."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc), "needs_setup": True}
            try:
                # touch token (lazy fetch from Edge CDP)
                _ = ctx.token
            except TokenError as exc:
                if not _try_relogin_with_saved_creds(ctx, cfg):
                    return {
                        "ok": False,
                        "error": str(exc),
                        "needs_login": True,
                        "domain": cfg.get("domain"),
                    }
            user = ctx.fetch_user()
            if not user:
                if _try_relogin_with_saved_creds(ctx, cfg):
                    user = ctx.fetch_user()
                if not user:
                    return {
                        "ok": False,
                        "error": "/auth/me 失败, token 可能失效, 请重新登录",
                        "needs_login": True,
                        "domain": cfg.get("domain"),
                    }
            self._user = user
            cfg, default_key, default_ep = sub2cli_lib.ensure_defaults(
                ctx, cfg, verbose=False
            )
            self._cfg = cfg
            self._default_key = default_key
            self._default_ep = default_ep

            keys = ctx.fetch_keys()
            codex_key = sub2cli_lib.find_codex_key(keys, cfg)
            if codex_key:
                cfg_changed = (
                    cfg.get("codex_key_id") != codex_key.get("id")
                    or cfg.get("codex_key_name") != codex_key.get("name")
                )
                cfg["codex_key_id"] = codex_key.get("id")
                cfg["codex_key_name"] = codex_key.get("name")
                if cfg_changed:
                    sub2cli_lib.save_config(cfg, ctx.config_path)
            keys = self._mark_codex_key(keys, codex_key)
            self._codex_key = codex_key
            settings = ctx.fetch_settings() or {}
            eps = sub2cli_lib.collect_endpoints(settings, fallback_site=ctx.domain)
            groups = ctx.fetch_groups()
            subscriptions = ctx.fetch_subscriptions()
            ep_url = (cfg or {}).get("default_endpoint_url") or sub2cli_lib.normalize_v1(ctx.domain)
            api_key = (default_key or {}).get("key", "") if default_key else ""
            group_models, models, model_error = sub2cli_lib.probe_group_models(
                ctx,
                cfg,
                keys,
                groups,
                key=default_key,
                timeout=8,
            ) if api_key else ({}, [], "未设置测试专用 key")
            if not models and api_key:
                models, model_error = sub2cli_lib.fetch_models(ep_url, api_key, timeout=8)
            model_columns = sub2cli_lib.normalize_group_model_columns(
                (cfg or {}).get("group_model_columns"),
                models,
            )
            if cfg.get("group_model_columns") != model_columns:
                cfg["group_model_columns"] = model_columns
                sub2cli_lib.save_config(cfg, ctx.config_path)
                self._cfg = cfg
            self._models = models
        return {
            "ok": True,
            "domain": cfg.get("domain"),
            "site": ctx.site,
            "user": {
                "email": user.get("email"),
                "status": user.get("status"),
                "balance": user.get("balance"),
                "concurrency": user.get("concurrency"),
            },
            "default_key": self._serialize_key(default_key) if default_key else None,
            "codex_key": self._serialize_key(codex_key) if codex_key else None,
            "default_endpoint": self._serialize_endpoint(default_ep) if default_ep else None,
            "endpoints": [self._serialize_endpoint(e) for e in eps],
            "keys": [self._serialize_key(k) for k in keys],
            "groups": [self._serialize_group(g) for g in groups],
            "models": models,
            "group_models": {str(k): v for k, v in group_models.items()},
            "model_error": model_error,
            "group_model_columns": model_columns,
            "subscriptions": [self._serialize_subscription(s) for s in subscriptions],
            "config_path": ctx.config_path,
        }

    def refresh(self) -> dict:
        """Re-fetch everything; equivalent to bootstrap without re-creating ctx."""
        with self._lock:
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None
        return self.bootstrap()

    def reveal_default_key(self) -> dict:
        """Return the full test API key (unmask). UI must opt-in."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            k = sub2cli_lib.find_default_key(keys, cfg)
            if not k:
                return {"ok": False, "error": "未设置测试专用 key"}
            return {"ok": True, "key": k.get("key", ""), "name": k.get("name")}

    def reveal_key(self, key_id: int) -> dict:
        """Return one full API key by id after an explicit UI action."""
        with self._lock:
            try:
                ctx, _cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            try:
                target_key_id = int(key_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"key id 无效: {key_id}"}
            keys = ctx.fetch_keys()
            k = sub2cli_lib.find_key_by_id(keys, target_key_id)
            if not k:
                return {"ok": False, "error": f"key 不存在: {target_key_id}"}
            return {
                "ok": True,
                "id": k.get("id"),
                "name": k.get("name"),
                "key": k.get("key", ""),
            }

    def ping_endpoint(self, base_url: str, with_auth: bool = False) -> dict:
        """Probe /v1/models on an endpoint. Optional auth uses the test key."""
        api_key: str | None = None
        if with_auth:
            with self._lock:
                try:
                    ctx, cfg = self._ensure_ctx()
                except RuntimeError as exc:
                    return {"ok": False, "error": str(exc)}
                keys = ctx.fetch_keys()
                k = sub2cli_lib.find_default_key(keys, cfg)
                if k:
                    api_key = k.get("key")
        return sub2cli_lib.probe_endpoint(base_url, api_key=api_key)

    def set_default_endpoint(self, name: str) -> dict:
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            settings = ctx.fetch_settings() or {}
            eps = sub2cli_lib.collect_endpoints(settings, fallback_site=ctx.domain)
            chosen = sub2cli_lib.find_endpoint(eps, name)
            if not chosen:
                return {"ok": False, "error": f"端点不存在: {name}"}
            cfg["default_endpoint_name"] = chosen.get("name")
            cfg["default_endpoint_url"] = chosen.get("endpoint")
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._default_ep = chosen
        return {"ok": True, "default_endpoint": self._serialize_endpoint(chosen)}

    def set_default_key(self, name: str) -> dict:
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            chosen = sub2cli_lib.find_key_by_name(keys, name)
            if not chosen:
                return {"ok": False, "error": f"key 不存在: {name}"}
            cfg["codex_key_name"] = chosen.get("name")
            cfg["codex_key_id"] = chosen.get("id")
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._codex_key = chosen
            keys = self._mark_codex_key(keys, chosen)
        return {
            "ok": True,
            "codex_key": self._serialize_key(chosen),
            "keys": [self._serialize_key(k) for k in keys],
        }

    def set_codex_key(self, key_id: int) -> dict:
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            try:
                target_key_id = int(key_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"key id 无效: {key_id}"}
            keys = ctx.fetch_keys()
            chosen = sub2cli_lib.find_key_by_id(keys, target_key_id)
            if not chosen:
                return {"ok": False, "error": f"key 不存在: {target_key_id}"}
            cfg["codex_key_id"] = chosen.get("id")
            cfg["codex_key_name"] = chosen.get("name")
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._codex_key = chosen
            keys = self._mark_codex_key(keys, chosen)
        return {
            "ok": True,
            "codex_key": self._serialize_key(chosen),
            "keys": [self._serialize_key(k) for k in keys],
        }

    def set_default_group(self, group_id: int) -> dict:
        """Move the dedicated test key to group_id without running probes."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            keys, default_key = sub2cli_lib.ensure_test_key(ctx, keys)
            if not default_key:
                return {"ok": False, "error": "无法创建测试专用 key, 无法切换分组"}
            try:
                target_group_id = int(group_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": f"分组 id 无效: {group_id}"}
            if default_key.get("group_id") != target_group_id:
                ok, err = ctx.update_key_group(int(default_key["id"]), target_group_id)
                if not ok:
                    return {"ok": False, "error": f"切分组失败: {err}"}
                keys = ctx.fetch_keys()
                default_key = sub2cli_lib.find_key_by_name(keys, sub2cli_lib.TEST_KEY_NAME)
                if not default_key:
                    return {"ok": False, "error": "切完分组后测试 key 丢失"}
            cfg["default_key_id"] = default_key.get("id")
            cfg["default_key_name"] = default_key.get("name")
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._default_key = default_key
            codex_key = self._codex_key or sub2cli_lib.find_codex_key(keys, cfg)
            keys = self._mark_codex_key(keys, codex_key)
        return {
            "ok": True,
            "default_key": self._serialize_key(default_key),
            "codex_key": self._serialize_key(codex_key) if codex_key else None,
            "keys": [self._serialize_key(k) for k in keys],
        }

    def update_key_group(self, key_id: int, group_id: int) -> dict:
        """Move an existing API key to a selected group."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            try:
                target_key_id = int(key_id)
                target_group_id = int(group_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "key id 或分组 id 无效"}
            keys = ctx.fetch_keys()
            current = sub2cli_lib.find_key_by_id(keys, target_key_id)
            if not current:
                return {"ok": False, "error": f"key 不存在: {target_key_id}"}
            if current.get("group_id") != target_group_id:
                ok, err = ctx.update_key_group(target_key_id, target_group_id)
                if not ok:
                    return {"ok": False, "error": f"切分组失败: {err}"}
                keys = ctx.fetch_keys()
                current = sub2cli_lib.find_key_by_id(keys, target_key_id)
                if not current:
                    return {"ok": False, "error": "切完分组后 key 丢失"}
            default_key = sub2cli_lib.find_default_key(keys, cfg)
            if default_key and self._same_key_id(default_key.get("id"), current.get("id")):
                self._default_key = current
            codex_key = sub2cli_lib.find_codex_key(keys, cfg)
            keys = self._mark_codex_key(keys, codex_key)
            self._codex_key = codex_key
        return {
            "ok": True,
            "key": self._serialize_key(current),
            "codex_key": self._serialize_key(codex_key) if codex_key else None,
            "keys": [self._serialize_key(k) for k in keys],
        }

    def _resolve_inject_bin(self) -> str | None:
        """Find sub2cli-inject binary; checks .app resources, repo, PATH."""
        candidates: list[str | None] = []
        res = _resource_dir()
        if res:
            candidates.append(os.path.join(res, "pyscripts", "sub2cli-inject-bundle"))
        candidates.append(os.path.join(REPO_ROOT, "sub2cli-inject"))
        candidates.extend([
            os.path.expanduser("~/.local/bin/sub2cli-inject"),
            shutil.which("sub2cli-inject"),
            shutil.which("codex-provider"),
        ])
        for p in candidates:
            if p and os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    def _current_default_inject_target(self) -> tuple[str, str, str, list[str]] | None:
        """Return (url, api_key, label, models) for the selected Codex key+endpoint.

        Returns None if not configured. label is human-readable summary.
        """
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError:
                return None
            keys = ctx.fetch_keys()
            k = sub2cli_lib.find_codex_key(keys, cfg)
            if k:
                self._codex_key = k
            ep = self._default_ep or {}
            url = (cfg or {}).get("default_endpoint_url") or ep.get("url") or ""
            if not k or not url:
                return None
            api_key = k.get("key") or ""
            g = k.get("group") or {}
            label = (
                f"key={k.get('name')} · 分组 {g.get('name','?')} "
                f"({g.get('rate_multiplier')}x) · 线路 "
                f"{(cfg or {}).get('default_endpoint_name','?')}"
            )
            groups = ctx.fetch_groups()
            _group_models, models, _model_error = sub2cli_lib.probe_group_models(
                ctx,
                cfg,
                keys,
                groups,
                key=k,
                timeout=8,
            )
            if not models:
                models, _model_error = sub2cli_lib.fetch_models(url, api_key, timeout=8)
            self._models = models
            return url, api_key, label, models

    def _current_default_relay_target(self) -> tuple[str, str, str] | None:
        """Return (url, api_key, label) without probing models."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError:
                return None
            keys = ctx.fetch_keys()
            k = sub2cli_lib.find_codex_key(keys, cfg)
            if k:
                self._codex_key = k
            ep = self._default_ep or {}
            url = (cfg or {}).get("default_endpoint_url") or ep.get("endpoint") or ep.get("url") or ""
            if not k or not url:
                return None
            api_key = k.get("key") or ""
            g = k.get("group") or {}
            label = (
                f"key={k.get('name')} · 分组 {g.get('name','?')} "
                f"({g.get('rate_multiplier')}x) · 线路 "
                f"{(cfg or {}).get('default_endpoint_name','?')}"
            )
            return url, api_key, label

    def relay_config_status(self) -> dict:
        """Return whether Codex is already configured to the selected relay target."""
        target = self._current_default_relay_target()
        if not target:
            return {"ok": False, "error": "未设置 Codex 配置用 key 或端点"}
        url, api_key, label = target
        data = _read_json_file(PROVIDER_SLOTS_PATH)
        current_slot = data.get("current")
        slots = data.get("slots") or {}
        current = slots.get(current_slot) or {}
        same_base = (
            current.get("mode") == "relay"
            and _comparable_api_base_url(current.get("base_url")) == _comparable_api_base_url(url)
        )
        slot_key = str(current.get("api_key") or "")
        same_key = not slot_key or not api_key or slot_key == api_key
        return {
            "ok": True,
            "already_current": bool(same_base and same_key),
            "label": label,
            "target_base_url": _comparable_api_base_url(url),
            "current_slot": current_slot,
            "current_display": current.get("display_name") or current_slot,
            "current_base_url": _comparable_api_base_url(current.get("base_url")),
        }

    def _inject_models_args(self, models: list[str]) -> tuple[list[str], tempfile.NamedTemporaryFile | None]:
        clean_models = [str(m).strip() for m in models if str(m or "").strip()]
        if not clean_models:
            return [], None
        tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix="-sub2cli-models.json", delete=False)
        json.dump(clean_models, tmp, ensure_ascii=False)
        tmp.flush()
        tmp.close()
        return ["--models-json", tmp.name], tmp

    def _run_inject_add_api(
        self,
        url: str,
        api_key: str,
        models: list[str],
        *,
        dry_run: bool,
        model: str | None = None,
        protocol: str = "responses",
        slot: str | None = None,
        display: str | None = None,
    ) -> dict:
        """Shared core for `sub2cli-inject add-api <url> <key>` (relay + custom API).

        dry_run=True  → {ok, plan_text, changes} on success, else {ok: False, error, ...}.
        dry_run=False → {ok, stdout, stderr, returncode, backup_name,
                         rollback_command, auto_rollback}, with best-effort
                         auto-rollback on timeout/failure.
        All secrets are masked before returning. Callers add label/command.
        """
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            # Preserve the original per-mode wording exactly (plan was more verbose).
            hint = " (应在 repo 根 或 ~/.local/bin)" if dry_run else ""
            return {"ok": False, "error": f"找不到 sub2cli-inject 二进制{hint}"}
        model_args, model_tmp = self._inject_models_args(models)
        cmd = [inject_bin, "add-api", url, "--api-key-stdin", "--skip-check", "--protocol", protocol, *model_args]
        if slot:
            cmd.extend(["--name", slot])
        if display:
            cmd.extend(["--display", display])
        if model:
            cmd.extend(["--model", model])
        if dry_run:
            cmd.append("--dry-run")
        timeout = INJECT_PLAN_TIMEOUT if dry_run else INJECT_APPLY_TIMEOUT
        try:
            proc = subprocess.run(
                cmd,
                input=api_key,
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            if dry_run:
                return {
                    "ok": False,
                    "error": f"dry-run 超过 {timeout} 秒仍未完成; 未写入任何 Codex 配置。",
                    "stdout": "",
                    "stderr": "",
                    "timeout": timeout,
                }
            stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _coerce_proc_text(getattr(exc, "stderr", None))
            backup_name = _extract_rollback_backup(stdout)
            rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name) if backup_name else None
            return {
                "ok": False,
                "error": f"配置超过 {timeout} 秒仍未完成",
                "stdout": _mask_secret_text(stdout, api_key),
                "stderr": _mask_secret_text(stderr, api_key),
                "returncode": None,
                "backup_name": backup_name,
                "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
                "auto_rollback": _mask_result_texts(rollback_result, api_key),
            }
        except Exception as exc:
            # Original plan path prefixed "调用 inject 失败: "; apply path did not.
            prefix = "调用 inject 失败: " if dry_run else ""
            return {"ok": False, "error": _mask_secret_text(f"{prefix}{type(exc).__name__}: {exc}", api_key)}
        finally:
            if model_tmp is not None:
                try:
                    os.unlink(model_tmp.name)
                except OSError:
                    pass

        if dry_run:
            if proc.returncode != 0:
                return {
                    "ok": False,
                    "error": f"inject --dry-run 返回 {proc.returncode}",
                    "stderr": _mask_secret_text(proc.stderr, api_key),
                    "stdout": _mask_secret_text(proc.stdout, api_key),
                }
            return {
                "ok": True,
                "plan_text": _mask_secret_text(proc.stdout, api_key),
                "changes": _parse_inject_plan_changes(proc.stdout, api_key),
            }

        backup_name = _extract_rollback_backup(proc.stdout)
        rollback_result = None
        if proc.returncode != 0 and backup_name:
            rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name)
        return {
            "ok": proc.returncode == 0,
            "stdout": _mask_secret_text(proc.stdout, api_key),
            "stderr": _mask_secret_text(proc.stderr, api_key),
            "returncode": proc.returncode,
            "backup_name": backup_name,
            "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
            "auto_rollback": _mask_result_texts(rollback_result, api_key),
        }

    def _run_inject_add_pool(self, pool: dict, routes: list[dict], secrets: list[str]) -> dict:
        """Apply a saved route pool through `sub2cli-inject add-pool`.

        `routes` already contains resolved api_key values. The saved pool in
        sub2cli config never stores them.
        """
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        if not routes:
            return {"ok": False, "error": "连接池至少需要一条 route"}
        payload = {
            "policy": _route_pool_policy(pool.get("policy") if isinstance(pool.get("policy"), dict) else {}),
            "routes": routes,
        }
        tmp: tempfile.NamedTemporaryFile | None = None
        try:
            tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix="-sub2cli-routes.json", delete=False)
            json.dump(payload, tmp, ensure_ascii=False)
            tmp.flush()
            tmp.close()
            slot = _route_pool_id_slug(pool.get("id") or pool.get("name") or "pool")
            display = f"Codex - {pool.get('name') or slot} route pool"
            cmd = [inject_bin, "add-pool", slot, "--routes-json", tmp.name, "--display", display, "--no-restart"]
            model = str(pool.get("model") or "").strip()
            if model:
                cmd.extend(["--model", model])
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    env=_inject_subprocess_env(),
                    timeout=INJECT_APPLY_TIMEOUT,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
                stderr = _coerce_proc_text(getattr(exc, "stderr", None))
                backup_name = _extract_rollback_backup(stdout)
                rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name) if backup_name else None
                return _mask_many_result_texts({
                    "ok": False,
                    "error": f"配置连接池超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                    "stdout": stdout,
                    "stderr": stderr,
                    "returncode": None,
                    "backup_name": backup_name,
                    "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
                    "auto_rollback": rollback_result,
                }, secrets)
            except Exception as exc:
                return {"ok": False, "error": _mask_many_secret_text(f"{type(exc).__name__}: {exc}", secrets)}

            backup_name = _extract_rollback_backup(proc.stdout)
            rollback_result = None
            if proc.returncode != 0 and backup_name:
                rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name)
            return _mask_many_result_texts({
                "ok": proc.returncode == 0,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "returncode": proc.returncode,
                "backup_name": backup_name,
                "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
                "auto_rollback": rollback_result,
            }, secrets)
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass

    def _route_pool_candidates(self, cfg: dict) -> dict:
        """Build current selectable route sources for the GUI."""
        relay_keys: list[dict] = []
        relay_endpoints: list[dict] = []
        relay_domain = cfg.get("domain")
        relays = cfg.get("relays") or {}
        if relay_domain and relay_domain not in relays:
            relays = {**relays, relay_domain: {f: cfg.get(f) for f in sub2cli_lib.RELAY_FIELDS}}
        route_domains = {
            str(route.get("relay_domain") or "").strip()
            for pool in _route_pools(cfg)
            if isinstance(pool, dict)
            for route in (pool.get("routes") or [])
            if isinstance(route, dict) and route.get("source_type") == "relay"
        }
        route_domains.discard("")
        relay_sources = [
            self._route_pool_relay_source(
                cfg,
                domain,
                load=(domain == relay_domain or domain in route_domains),
            )
            for domain in relays.keys()
        ]
        try:
            current_source = next((source for source in relay_sources if source.get("domain") == relay_domain), None)
            if current_source:
                relay_keys = current_source.get("keys") or []
                relay_endpoints = current_source.get("endpoints") or []
        except Exception:
            relay_keys = []
            relay_endpoints = []
        current_endpoint = cfg.get("default_endpoint_url") or ""
        custom_apis = [
            self._serialize_custom_api(e, current_id=cfg.get("current_custom_api"))
            for e in _custom_apis(cfg)
            if isinstance(e, dict) and e.get("id")
        ]
        return {
            "relay_domain": relay_domain,
            "relay_sources": relay_sources,
            "relay_keys": relay_keys,
            "relay_endpoints": relay_endpoints,
            "current_endpoint_url": current_endpoint,
            "current_endpoint_name": cfg.get("default_endpoint_name") or "",
            "custom_apis": custom_apis,
        }

    def route_pool_relay_source(self, domain: str) -> dict:
        """Load keys/endpoints for one saved relay selected in the route pool UI."""
        domain = sub2cli_lib._normalize_domain(domain or "")
        cfg = sub2cli_lib.load_config() or {}
        relays = cfg.get("relays") or {}
        if domain not in relays:
            return {"ok": False, "error": f"未保存的中转站: {domain}"}
        source = self._route_pool_relay_source(cfg, domain, load=True)
        return {"ok": not bool(source.get("error")), "source": source, "error": source.get("error") or ""}

    def route_pool_update_key_group(self, domain: str, key_id: int, group_id: int) -> dict:
        """Move a relay key to a group from the route-pool table."""
        domain = (domain or "").strip()
        with self._lock:
            cfg = sub2cli_lib.load_config() or {}
            relays = cfg.get("relays") or {}
            if domain not in relays and domain != cfg.get("domain"):
                return {"ok": False, "error": f"未保存的中转站: {domain}"}
            try:
                target_key_id = int(key_id)
                target_group_id = int(group_id)
            except (TypeError, ValueError):
                return {"ok": False, "error": "key id 或分组 id 无效"}
            try:
                ctx = self._relay_ctx_for_domain(cfg, domain)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "error": f"无法读取中转 {domain}: {exc}"}
            keys = ctx.fetch_keys()
            current = sub2cli_lib.find_key_by_id(keys, target_key_id)
            if not current:
                return {"ok": False, "error": f"key 不存在: {target_key_id}"}
            if current.get("group_id") != target_group_id:
                ok, err = ctx.update_key_group(target_key_id, target_group_id)
                if not ok:
                    return {"ok": False, "error": f"切分组失败: {err}"}
                keys = ctx.fetch_keys()
                current = sub2cli_lib.find_key_by_id(keys, target_key_id)
                if not current:
                    return {"ok": False, "error": "切完分组后 key 丢失"}
            source = self._route_pool_relay_source(cfg, domain, load=True)
        return {
            "ok": True,
            "key": self._serialize_key(current),
            "source": source,
            "error": source.get("error") or "",
        }

    def list_route_pools(self) -> dict:
        """Saved route pools and current selectable route sources."""
        cfg = sub2cli_lib.load_config() or {}
        pools = [
            _sanitize_route_pool(e, existing_id=e.get("id"))
            for e in _route_pools(cfg)
            if isinstance(e, dict) and e.get("id")
        ]
        current_id = cfg.get("current_route_pool")
        if current_id and not any(p["id"] == current_id for p in pools):
            current_id = pools[0]["id"] if pools else None
        return {
            "ok": True,
            "pools": pools,
            "current_id": current_id,
            "candidates": self._route_pool_candidates(cfg),
            "status": self.route_pool_status(),
        }

    def save_route_pool(self, pool: dict) -> dict:
        """Create/update a saved route pool and hot-apply it to the Codex slot."""
        with self._lock:
            cfg = sub2cli_lib.load_config() or {}
            submitted_routes = (pool or {}).get("routes") if isinstance(pool, dict) else []
            submitted_count = len(submitted_routes) if isinstance(submitted_routes, list) else 0
            pools = _route_pools(cfg)
            requested_id = str((pool or {}).get("id") or "").strip()
            existing = _find_route_pool(cfg, requested_id) if requested_id else None
            used = {e.get("id") for e in pools if isinstance(e, dict) and e.get("id")}
            if existing:
                clean = _sanitize_route_pool(pool, existing_id=existing.get("id"))
                clean["created_at"] = existing.get("created_at") or clean["created_at"]
                index = pools.index(existing)
                pools[index] = clean
            else:
                clean = _sanitize_route_pool(pool, used_ids=used)
                pools.append(clean)
            if submitted_count and len(clean.get("routes") or []) != submitted_count:
                return {
                    "ok": False,
                    "error": f"保存失败: {submitted_count} 条 route 中只有 {len(clean.get('routes') or [])} 条有效，请检查来源/名称/分组是否已加载",
                }
            cfg["current_route_pool"] = clean["id"]
            routes, secrets, error = self._resolve_route_pool_routes(cfg, clean)
            if error:
                return {"ok": False, "error": error}
            apply_result = self._run_inject_add_pool(clean, routes, secrets)
            if not apply_result.get("ok"):
                apply_result.setdefault("error", "连接池热生效失败，未保存")
                return apply_result
            _mark_route_pool_config_changed(cfg)
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        listing = self.list_route_pools()
        listing["saved_id"] = clean["id"]
        listing["applied"] = True
        listing["apply_stdout"] = apply_result.get("stdout", "")
        listing["apply_stderr"] = apply_result.get("stderr", "")
        listing["backup_name"] = apply_result.get("backup_name")
        listing["rollback_command"] = apply_result.get("rollback_command")
        return listing

    def _resolve_route_pool_routes(self, cfg: dict, pool: dict) -> tuple[list[dict], list[str], str | None]:
        routes: list[dict] = []
        secrets: list[str] = []
        relay_cache: dict[str, tuple[Any, dict, list[dict]]] = {}

        def _relay_ctx(domain: str) -> tuple[Any, dict, list[dict]] | None:
            if domain in relay_cache:
                return relay_cache[domain]
            relays = cfg.get("relays") or {}
            relay_cfg = cfg if domain == cfg.get("domain") else (relays.get(domain) or {})
            ctx = self._relay_ctx_for_domain(cfg, domain or cfg.get("domain") or "")
            keys = ctx.fetch_keys()
            relay_cache[domain or ctx.domain] = (ctx, relay_cfg, keys)
            return relay_cache[domain or ctx.domain]

        for idx, route in enumerate(pool.get("routes") or [], 1):
            source_type = route.get("source_type")
            if source_type == "relay":
                domain = str(route.get("relay_domain") or cfg.get("domain") or "").strip()
                try:
                    relay = _relay_ctx(domain)
                except Exception as exc:  # noqa: BLE001
                    return [], secrets, f"无法读取中转 {domain}: {exc}"
                _ctx, relay_cfg, keys = relay
                try:
                    key_id = int(route.get("key_id"))
                except (TypeError, ValueError):
                    return [], secrets, f"route {route.get('label') or idx} 的 key id 无效"
                key = sub2cli_lib.find_key_by_id(keys, key_id)
                key_name = str(route.get("key_name") or "").strip()
                if not key and key_name:
                    key = sub2cli_lib.find_key_by_name(keys, key_name)
                if not key:
                    return [], secrets, f"找不到 relay key: {key_name or key_id}"
                api_key = key.get("key") or ""
                base_url = route.get("base_url") or relay_cfg.get("default_endpoint_url") or sub2cli_lib.normalize_v1(_ctx.domain)
                group = key.get("group") or {}
                label = route.get("label") or f"{key.get('name') or key_id} · {group.get('name') or 'default'}"
                resolved = {
                    "id": _route_pool_id_slug(route.get("id") or f"relay-{idx}", f"relay-{idx}"),
                    "label": label,
                    "source_type": "relay",
                    "priority": int(route.get("priority") or idx * 10),
                    "base_url": base_url,
                    "api_key": api_key,
                    "protocol": route.get("protocol") or "responses",
                    "model": route.get("model") or pool.get("model") or "",
                    "group": route.get("group_name") or group.get("name"),
                    "group_id": route.get("group_id") or group.get("id"),
                    "endpoint": route.get("endpoint_name") or relay_cfg.get("default_endpoint_name"),
                }
                routes.append(resolved)
                secrets.append(api_key)
                continue

            if source_type == "custom":
                api_id = str(route.get("custom_api_id") or "").strip()
                entry = _find_custom_api(cfg, api_id)
                if not entry:
                    return [], secrets, f"找不到自定义 API: {api_id}"
                api_key = _kc_custom_api_get(api_id) or ""
                if not api_key:
                    return [], secrets, f"Keychain 里没有自定义 API 的 Key: {api_id}"
                base_url = entry.get("base_url") or route.get("base_url") or ""
                label = route.get("label") or entry.get("name") or api_id
                routes.append({
                    "id": _route_pool_id_slug(route.get("id") or f"custom-{idx}", f"custom-{idx}"),
                    "label": label,
                    "source_type": "custom",
                    "priority": int(route.get("priority") or idx * 10),
                    "base_url": base_url,
                    "api_key": api_key,
                    "protocol": route.get("protocol") or "chat",
                    "model": route.get("model") or pool.get("model") or "",
                    "notes": route.get("notes") or "custom-api",
                })
                secrets.append(api_key)
                continue

            return [], secrets, f"未知 route 类型: {source_type}"

        routes.sort(key=lambda item: int(item.get("priority", 99999)))
        return routes, secrets, None

    def route_pool_config_apply(self, pool_id: str) -> dict:
        """Configure Codex to a saved route pool."""
        pool_id = (pool_id or "").strip()
        if not pool_id:
            return {"ok": False, "error": "缺少连接池 id"}
        with self._lock:
            cfg = sub2cli_lib.load_config() or {}
            pool = _find_route_pool(cfg, pool_id)
            if not pool:
                return {"ok": False, "error": f"未保存的连接池: {pool_id}"}
            clean_pool = _sanitize_route_pool(pool, existing_id=pool_id)
            if not clean_pool.get("routes"):
                return {"ok": False, "error": "连接池至少需要一条 route"}
            routes, secrets, error = self._resolve_route_pool_routes(cfg, clean_pool)
            if error:
                return {"ok": False, "error": error}
            result = self._run_inject_add_pool(clean_pool, routes, secrets)
            if result.get("ok"):
                cfg["current_route_pool"] = clean_pool["id"]
                _mark_route_pool_config_changed(cfg)
                sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
                result["pool_id"] = clean_pool["id"]
        return result

    def route_pool_status(self) -> dict:
        """Best-effort snapshot from the local pool proxy."""
        url = "http://127.0.0.1:18765/poolz"
        logs = _route_pool_log_lines()
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=0.8) as resp:
                body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body)
            return {"ok": True, "snapshot": data, "logs": logs}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "logs": logs}

    def route_pool_restart_proxy(self) -> dict:
        """Restart only the local route-pool proxy process."""
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        try:
            proc = subprocess.run(
                [inject_bin, "restart-proxy"],
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False,
                "error": "重启连接池代理超过 30 秒仍未完成",
                "stdout": _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None)),
                "stderr": _coerce_proc_text(getattr(exc, "stderr", None)),
                "status": self.route_pool_status(),
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "status": self.route_pool_status()}
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "status": self.route_pool_status(),
        }

    def inject_plan(self) -> dict:
        """Dry-run: call sub2cli-inject add-api <url> <key> --dry-run.

        Returns {ok, plan_text, label, command, changes}. The plan_text is the
        raw stdout of the inject binary so the modal can display it verbatim.
        """
        target = self._current_default_inject_target()
        if not target:
            return {
                "ok": False,
                "error": "未设置 Codex 配置用 key 或端点；请先确认当前中转有可用 key，并选择端点。",
            }
        url, api_key, label, models = target
        result = self._run_inject_add_api(url, api_key, models, dry_run=True)
        if result.get("ok"):
            result["label"] = label
            result["command"] = "sub2cli-inject add-api <url> --api-key-stdin --skip-check (dry-run)"
        return result

    def inject_apply(self) -> dict:
        """Real inject: call sub2cli-inject add-api <url> <key> for current Codex key.

        Returns {ok, stdout, stderr, backup_name, auto_rollback}.
        """
        target = self._current_default_inject_target()
        if not target:
            return {"ok": False, "error": "未设置 Codex 配置用 key 或端点"}
        url, api_key, _label, models = target
        return self._run_inject_add_api(url, api_key, models, dry_run=False)

    def inject_rollback(self, backup_name: str) -> dict:
        """Undo the most recent GUI injection by restoring its backup."""
        backup_name = (backup_name or "").strip()
        if not backup_name:
            return {"ok": False, "error": "缺少备份名"}
        if not re.fullmatch(r"[A-Za-z0-9._-]+", backup_name):
            return {"ok": False, "error": "备份名非法"}
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        try:
            proc = subprocess.run(
                [inject_bin, "rollback", backup_name],
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_ROLLBACK_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"回滚超过 {INJECT_ROLLBACK_TIMEOUT} 秒仍未完成",
                "stdout": "",
                "stderr": "",
                "returncode": None,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    def probe_relay(self, url: str) -> dict:
        """Inspect a candidate relay URL to decide what credentials the user needs.

        Steps (best-effort, each isolated so one failure doesn't kill the rest):
          1. Normalize URL + GET /api/v1/settings/public to confirm it's a Sub2API site
          2. Try Edge CDP — if there's a logged-in tab for this domain, capture
             a fresh token + email, cache for the following add_relay() call
          3. Detect Cloudflare Turnstile by attempting a dummy /auth/login —
             the error message distinguishes turnstile from invalid creds

        Returns: { ok, is_sub2api, has_edge_session, edge_email, turnstile, domain, error? }
        The actual edge token is NOT returned (cached server-side for security).
        """
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "URL 必填"}
        try:
            domain = sub2cli_lib._normalize_domain(url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"URL 不合法: {exc}"}

        # Warn (don't hard-block — localhost self-hosted relays use http) when a
        # bearer token would be sent over cleartext http to a non-loopback host.
        insecure_warning = None
        _parsed = urlparse(domain)
        if _parsed.scheme == "http" and not _is_loopback_relay_host(_parsed.hostname or ""):
            insecure_warning = (
                f"{_parsed.hostname} 使用明文 http，relay token 将以明文发送，建议改用 https"
            )

        probe = Sub2Context(domain)
        is_sub2api, sub_msg = probe.settings_reachable_anonymous()
        if not is_sub2api:
            return {
                "ok": False,
                "is_sub2api": False,
                "domain": domain,
                "error": f"无法连接 {domain} 的 Sub2API: {sub_msg}",
            }

        # Step 2: Edge CDP token (best-effort)
        edge_email: str | None = None
        has_edge_session = False
        try:
            token = probe.token
            probe.set_token(token)
            user = probe.fetch_user()
            if user and user.get("email"):
                edge_email = user["email"]
                has_edge_session = True
                with self._lock:
                    self._pending_tokens[domain] = (token, edge_email)
        except Exception:
            pass

        # Step 3: Turnstile detection via dummy POST /auth/login
        turnstile = False
        _, err = _sub2_login(domain, "_probe@sub2cli.invalid", "_probe_wrong_pw", timeout=8)
        if err and ("turnstile" in err.lower() or "captcha" in err.lower()):
            turnstile = True

        return {
            "ok": True,
            "is_sub2api": True,
            "has_edge_session": has_edge_session,
            "edge_email": edge_email,
            "turnstile": turnstile,
            "domain": domain,
            "insecure_warning": insecure_warning,
        }

    def add_relay(self, url: str, email: str = "", password: str = "") -> dict:
        """Add a new relay to config. Optionally log in with email+password.

        Steps:
          1. Normalize URL; probe /api/v1/settings/public to confirm it's a Sub2API instance.
          2. Write cfg["relays"][domain] with empty RELAY_FIELDS.
          3. If email+password provided: POST /auth/login -> token. Store creds + token locally. Register account.
          4. Switch active domain to the new relay and re-bootstrap.
        """
        url = (url or "").strip()
        if not url:
            return {"ok": False, "error": "URL 必填"}
        try:
            domain = sub2cli_lib._normalize_domain(url)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"URL 不合法: {exc}"}
        # Probe to confirm it looks like a Sub2API instance.
        probe = Sub2Context(domain)
        ok, msg = probe.settings_reachable_anonymous()
        if not ok:
            return {"ok": False, "error": f"无法连接 {domain} 的 Sub2API: {msg}"}

        email = (email or "").strip()
        password = password or ""
        token: str | None = None
        # If user filled email+password: do explicit login and save creds for auto-relogin.
        # If user left both blank: try the token captured by a prior probe_relay (Edge session).
        save_creds = False
        if email or password:
            if not (email and password):
                return {"ok": False, "error": "填了 email/password 就两个都得填"}
            token, err = _sub2_login(domain, email, password)
            if not token:
                if "turnstile" in err.lower() or "captcha" in err.lower():
                    return {"ok": False, "error": (
                        f"{domain} 启用了人机验证 (Cloudflare Turnstile), 无法直接用账号密码登录。\n"
                        "请先在浏览器登录该站点, 然后回来留空 email/密码再点添加, "
                        "app 会自动从 Edge 读 token 导入。"
                    )}
                return {"ok": False, "error": f"登录失败: {err}"}
            save_creds = True
        else:
            with self._lock:
                pending = self._pending_tokens.pop(domain, None)
            if pending:
                token, p_email = pending
                email = p_email or ""

        with self._lock:
            cfg = sub2cli_lib.load_config() or {"domain": domain, "relays": {}}
            relays = cfg.setdefault("relays", {})
            if domain not in relays:
                relays[domain] = {f: None for f in sub2cli_lib.RELAY_FIELDS}
            cfg["domain"] = domain
            for f in sub2cli_lib.RELAY_FIELDS:
                cfg[f] = (relays[domain] or {}).get(f)

            credential_warning = None
            if token and email:
                if not _kc_set(domain, email, token):
                    credential_warning = (
                        "token 未能写入 sub2cli 本地凭据缓存（下次启动可能需要重新登录）"
                    )
                if save_creds and not _kc_creds_set(domain, email, password):
                    credential_warning = (
                        "登录凭据未能写入 sub2cli 本地凭据缓存（自动续登将不可用）"
                    )
                ad = _accounts_for_domain(cfg, domain)
                if not any(a.get("email") == email for a in ad["saved"]):
                    ad["saved"].append({"email": email, "last_verified": int(time.time())})
                ad["current"] = email

            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
            self._ctx = None
            self._cfg = None
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None

        result = self.bootstrap()
        if credential_warning and isinstance(result, dict):
            result["credential_warning"] = credential_warning
        return result

    def switch_relay(self, domain: str) -> dict:
        """Change active relay. Rebuilds Sub2Context, re-runs bootstrap.

        If the new relay's bootstrap fails (e.g. needs_login), the previous
        relay is restored as active so the UI stays consistent with cfg.
        """
        cfg = sub2cli_lib.load_config()
        if not cfg:
            return {"ok": False, "error": "尚未配置"}
        relays = cfg.get("relays") or {}
        if domain not in relays:
            return {"ok": False, "error": f"未保存的 relay: {domain}"}

        prev_domain = cfg.get("domain")
        prev_fields = {f: cfg.get(f) for f in sub2cli_lib.RELAY_FIELDS}

        relay = relays[domain] or {}
        cfg["domain"] = domain
        for f in sub2cli_lib.RELAY_FIELDS:
            cfg[f] = relay.get(f)
        sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        with self._lock:
            self._ctx = None
            self._cfg = None
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None

        result = self.bootstrap()
        if result.get("ok") or prev_domain is None or prev_domain == domain:
            return result
        if result.get("needs_login"):
            imported = self._import_edge_account_for_domain(domain)
            if imported.get("ok"):
                retry_cfg = sub2cli_lib.load_config() or cfg
                retry_relays = retry_cfg.get("relays") or {}
                retry_cfg["domain"] = domain
                for f in sub2cli_lib.RELAY_FIELDS:
                    retry_cfg[f] = (retry_relays.get(domain) or {}).get(f)
                sub2cli_lib.save_config(retry_cfg, sub2cli_lib.default_config_path())
                with self._lock:
                    self._ctx = None
                    self._cfg = None
                    self._user = None
                    self._default_key = None
                    self._codex_key = None
                    self._default_ep = None
                retry_result = self.bootstrap()
                if retry_result.get("ok"):
                    retry_result["recovered_login"] = True
                    retry_result["recovered_email"] = imported.get("email")
                    return retry_result

        # Rollback to previous relay so sidebar / cfg stay consistent.
        rollback = sub2cli_lib.load_config() or cfg
        rollback["domain"] = prev_domain
        for f, v in prev_fields.items():
            rollback[f] = v
        sub2cli_lib.save_config(rollback, sub2cli_lib.default_config_path())
        with self._lock:
            self._ctx = None
            self._cfg = None
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None
        return {
            "ok": False,
            "error": result.get("error", "切换失败"),
            "needs_login": result.get("needs_login", False),
            "domain": domain,
            "reverted_to": prev_domain,
        }

    def switch_relay_fast(self, domain: str) -> dict:
        """Change active relay in local config only.

        Sidebar clicks use this to make selection instant; the UI then refreshes
        account/key/group data in the background via refresh().
        """
        with self._lock:
            cfg = sub2cli_lib.load_config()
            if not cfg:
                return {"ok": False, "error": "尚未配置"}
            relays = cfg.get("relays") or {}
            if domain not in relays:
                return {"ok": False, "error": f"未保存的 relay: {domain}"}

            relay = relays[domain] or {}
            cfg["domain"] = domain
            for f in sub2cli_lib.RELAY_FIELDS:
                cfg[f] = relay.get(f)
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
            self._ctx = None
            self._cfg = None
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None
        return {
            "ok": True,
            "domain": domain,
            "site": self._relay_site_label(domain),
        }

    def remove_relay(self, domain: str) -> dict:
        """Remove a relay from cfg + clear its local/legacy credential entries.

        If the removed relay was the active one, switches to any remaining relay
        (or returns needs_setup if none left).
        """
        domain = (domain or "").strip()
        if not domain:
            return {"ok": False, "error": "domain 必填"}
        cfg = sub2cli_lib.load_config()
        if not cfg:
            return {"ok": False, "error": "尚未配置"}
        relays = cfg.get("relays") or {}
        if domain not in relays:
            return {"ok": False, "error": f"未保存的 relay: {domain}"}

        # Clear local cache and legacy Keychain entries for every saved account under this relay.
        ad = (cfg.get("accounts") or {}).get(domain) or {}
        for a in ad.get("saved") or []:
            email = a.get("email")
            if email:
                _kc_delete(domain, email)
                _kc_creds_delete(domain, email)

        with self._lock:
            relays.pop(domain, None)
            (cfg.get("accounts") or {}).pop(domain, None)
            was_current = (cfg.get("domain") == domain)
            if was_current:
                remaining = list(relays.keys())
                if remaining:
                    new_domain = remaining[0]
                    cfg["domain"] = new_domain
                    for f in sub2cli_lib.RELAY_FIELDS:
                        cfg[f] = (relays[new_domain] or {}).get(f)
                else:
                    cfg["domain"] = None
                    for f in sub2cli_lib.RELAY_FIELDS:
                        cfg[f] = None
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
            self._ctx = None
            self._cfg = None
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None

        if was_current:
            if cfg.get("domain"):
                result = self.bootstrap()
                result["removed"] = domain
                result["switched_to"] = cfg["domain"]
                return result
            return {"ok": False, "needs_setup": True, "removed": domain,
                    "error": "已删除最后一个 relay, 请点 + 新增中转"}
        return {"ok": True, "removed": domain}

    # ---- accounts (local credential-cache backed) ----

    def list_accounts(self) -> dict:
        """Saved accounts for the current relay + which one is active."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc), "accounts": [], "current": None}
            ad = _accounts_for_domain(cfg, ctx.domain)
            return {
                "ok": True,
                "accounts": list(ad.get("saved", []) or []),
                "current": ad.get("current"),
                "domain": ctx.domain,
            }

    def import_edge_account(self) -> dict:
        """Read fresh CDP token for current relay, identify user via /auth/me,
        store token in the local credential cache, register in cfg.accounts.

        After this, the active ctx will use the saved token, so subsequent
        bootstraps don't need Edge open.
        """
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            domain = ctx.domain
        result = self._import_edge_account_for_domain(domain)
        if result.get("ok"):
            with self._lock:
                self._ctx = None
                self._cfg = None
                self._user = None
                self._default_key = None
                self._codex_key = None
                self._default_ep = None
        return result

    def import_edge_account_for_relay(self, domain: str) -> dict:
        domain = (domain or "").strip()
        if not domain:
            return {"ok": False, "error": "domain 必填"}
        try:
            domain = sub2cli_lib._normalize_domain(domain)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"URL 不合法: {exc}"}
        return self._import_edge_account_for_domain(domain)

    def add_account(self, email: str, password: str) -> dict:
        """Add a new account to the CURRENT relay via POST /auth/login.

        Stores token + creds in the local credential cache, registers the account,
        activates it.
        """
        email = (email or "").strip()
        password = password or ""
        if not email:
            return {"ok": False, "error": "email 必填"}
        if not password:
            return {"ok": False, "error": "密码必填"}
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
        token, err = _sub2_login(ctx.domain, email, password)
        if not token:
            if "turnstile" in err.lower() or "captcha" in err.lower():
                return {"ok": False, "error": (
                    f"{ctx.domain} 启用了人机验证 (Cloudflare Turnstile), 无法直接登录。\n"
                    "请先在浏览器登录该账号, 然后回来在中转列表点 \"+ 新增中转\" 并留空 email/密码 即可。"
                )}
            return {"ok": False, "error": f"登录失败: {err}"}
        with self._lock:
            credential_warning = None
            if not _kc_set(ctx.domain, email, token):
                credential_warning = "token 未能写入 sub2cli 本地凭据缓存（下次启动可能需要重新登录）"
            if not _kc_creds_set(ctx.domain, email, password):
                credential_warning = "登录凭据未能写入 sub2cli 本地凭据缓存（自动续登将不可用）"
            ad = _accounts_for_domain(cfg, ctx.domain)
            if not any(a.get("email") == email for a in ad["saved"]):
                ad["saved"].append({"email": email, "last_verified": int(time.time())})
            else:
                for a in ad["saved"]:
                    if a.get("email") == email:
                        a["last_verified"] = int(time.time())
            ad["current"] = email
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            ctx.set_token(token)
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None
        result = self.bootstrap()
        if credential_warning and isinstance(result, dict):
            result["credential_warning"] = credential_warning
        return result

    def switch_account(self, email: str) -> dict:
        """Activate a saved account.

        Semantics (per V's request): "logout + login again" — if we have the
        password in the local credential cache we POST /auth/login to get a
        FRESH token rather than reusing the cached one. Fallback to a cached
        token only when password isn't saved (e.g. legacy Edge-imported accounts).
        """
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            ad = _accounts_for_domain(cfg, ctx.domain)
            if not any(a.get("email") == email for a in ad.get("saved", [])):
                return {"ok": False, "error": f"账号 {email} 不在已保存列表"}

            token: str | None = None
            pw = _kc_creds_get(ctx.domain, email)
            if pw:
                token, err = _sub2_login(ctx.domain, email, pw)
                if not token:
                    return {"ok": False, "error": f"登录失败: {err}"}
                _kc_set(ctx.domain, email, token)
            else:
                token = _kc_get(ctx.domain, email)
                if not token:
                    return {"ok": False, "error": f"{email} 没保存密码且本地没有 token, 请重新添加账号"}

            ctx.set_token(token)
            ad["current"] = email
            for a in ad["saved"]:
                if a.get("email") == email:
                    a["last_verified"] = int(time.time())
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._user = None
            self._default_key = None
            self._codex_key = None
            self._default_ep = None
        return self.bootstrap()

    def delete_account(self, email: str) -> dict:
        """Remove an account from local credentials and cfg."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            _kc_delete(ctx.domain, email)
            _kc_creds_delete(ctx.domain, email)
            ad = _accounts_for_domain(cfg, ctx.domain)
            ad["saved"] = [a for a in ad.get("saved", []) if a.get("email") != email]
            if ad.get("current") == email:
                ad["current"] = None
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
        return {"ok": True}

    def list_relays_full(self) -> dict:
        """Return all saved relays + which one is current."""
        cfg = sub2cli_lib.load_config()
        if not cfg:
            return {"ok": False, "error": "尚未配置", "relays": [], "current": None}
        relays = cfg.get("relays") or {}
        current = cfg.get("domain", "")
        items = []
        for domain, r in relays.items():
            r = r or {}
            items.append({
                "domain": domain,
                "site": (sub2cli_lib._normalize_domain(domain)
                         .replace("https://", "").replace("http://", "")
                         .rstrip("/")),
                "codex_key_name": r.get("codex_key_name"),
                "default_key_name": r.get("codex_key_name") or r.get("default_key_name"),
                "default_endpoint_name": r.get("default_endpoint_name"),
                "is_current": (domain == current),
            })
        return {"ok": True, "relays": items, "current": current}

    def list_codex_accounts(self) -> dict:
        """Return official Codex accounts from sub2cli slots and known auth stores."""
        data = _read_json_file(PROVIDER_SLOTS_PATH)
        return _discover_codex_accounts(data)

    def select_codex_account(self, slot: str) -> dict:
        """Select the official account used as the app history / official target.
        """
        slot = (slot or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", slot):
            return {"ok": False, "error": "slot 非法"}
        try:
            with CodexStateLock():
                data = _read_json_file(PROVIDER_SLOTS_PATH)
                accounts = _discover_codex_accounts(data, official_override=slot)
                account = next((a for a in accounts.get("accounts", []) if a.get("slot") == slot), None)
                if not account:
                    return {"ok": False, "error": f"不是已保存的官方账号: {slot}"}
                data.setdefault("version", 1)
                data.setdefault("slots", data.get("slots") or {})
                data["preferred_official_slot"] = slot
                _write_json_file(PROVIDER_SLOTS_PATH, data)
        except Exception as exc:
            return {"ok": False, "error": f"保存官方账号选择失败: {type(exc).__name__}: {exc}"}
        return _discover_codex_accounts(official_override=slot)

    def add_codex_account(self, slot: str, display: str = "") -> dict:
        """Login an isolated official Codex account and add it as a switchable slot."""
        slot = (slot or "").strip().lower()
        display = (display or "").strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", slot):
            return {"ok": False, "error": "slot 需为小写字母/数字/_/-, 最长 31 位"}

        login_auth, ident, output, error = _run_isolated_codex_login(slot)
        if error:
            return {"ok": False, "error": error, "stdout": output}
        assert login_auth is not None
        assert ident is not None

        try:
            with CodexStateLock():
                data = _read_json_file(PROVIDER_SLOTS_PATH)
                data.setdefault("version", 1)
                slots = data.setdefault("slots", {})
                existing = slots.get(slot) or {}
                if existing and existing.get("mode") != "oauth":
                    return {"ok": False, "error": f"slot {slot!r} 已存在但不是官方账号渠道"}
                if existing:
                    existing_ident = _codex_auth_identity(existing.get("auth_file"))
                    existing_key = (existing_ident.get("account_id") or existing_ident.get("email") or "").strip().lower()
                    new_key = (ident.get("account_id") or ident.get("email") or "").strip().lower()
                    if existing_key and new_key and existing_key != new_key:
                        return {
                            "ok": False,
                            "error": f"slot {slot!r} 已属于 {existing_ident.get('email') or existing_key}, 请换一个账号标识",
                        }

                target_auth = CODEX_HOME_PATH / f"auth.{slot}.json"
                _copy_auth_material(login_auth, target_auth)
                slots[slot] = {
                    "display_name": display or existing.get("display_name") or f"Codex - {ident.get('email') or slot}",
                    "mode": "oauth",
                    "auth_file": str(target_auth),
                    "app_profile_dir": existing.get("app_profile_dir") or str(CODEX_APP_SUPPORT_PATH / f"Codex.{slot}"),
                }
                data["app_history_slot"] = slot
                data["preferred_official_slot"] = slot
                _write_json_file(PROVIDER_SLOTS_PATH, data)
        except Exception as exc:
            return {"ok": False, "error": f"保存登录文件失败: {type(exc).__name__}: {exc}"}
        data = _read_json_file(PROVIDER_SLOTS_PATH)
        accounts = _discover_codex_accounts(data, official_override=slot)
        accounts["stdout"] = output
        accounts["login_home"] = _home_path(login_auth.parent)
        return accounts

    def remove_codex_account(self, slot: str) -> dict:
        """Remove a saved official Codex account slot and its local auth copy."""
        slot = (slot or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", slot):
            return {"ok": False, "error": "slot 非法"}

        removed: list[str] = []
        try:
            with CodexStateLock():
                data = _read_json_file(PROVIDER_SLOTS_PATH)
                slots = data.setdefault("slots", {})
                snapshot = _discover_codex_accounts(data, official_override=slot)
                account = next((a for a in snapshot.get("accounts", []) if a.get("slot") == slot), None)
                existing = slots.get(slot)
                if data.get("current") == slot or (account and account.get("is_current_provider")):
                    return {"ok": False, "error": "该官方账号当前正在 Codex 使用中，请先切到中转或其他账号后再删除。"}

                provider_changed = False
                if existing:
                    if existing.get("mode") != "oauth":
                        return {"ok": False, "error": f"slot {slot!r} 不是官方账号"}
                    auth_path = _expanded_path(existing.get("auth_file"))
                    slots.pop(slot, None)
                    provider_changed = True
                    try:
                        if _safe_remove_codex_auth_file(auth_path):
                            removed.append(_home_path(auth_path))
                    except OSError:
                        pass
                elif not account:
                    return {"ok": False, "error": f"未找到官方账号: {slot}"}

                source_kind = (account or {}).get("source_kind") or ""
                removed_source = False
                if source_kind == "codexbar-managed" or (account or {}).get("managed_home"):
                    removed_source = _remove_codexbar_managed_account(account or {}, removed)
                elif source_kind == "auth-file":
                    try:
                        source_auth_path = _expanded_path((account or {}).get("source_auth_file"))
                        if _safe_remove_codex_auth_file(source_auth_path):
                            removed.append(_home_path(source_auth_path))
                            removed_source = True
                    except OSError:
                        pass
                elif source_kind == "live-auth" and not existing:
                    return {"ok": False, "error": "该账号来自当前 ~/.codex/auth.json，不能从官号列表直接删除。请先切到其他渠道或退出 Codex 登录。"}
                elif not existing:
                    return {"ok": False, "error": f"该账号来自 {source_kind or '未知来源'}，不能删除。"}
                if not existing and not removed_source:
                    return {"ok": False, "error": f"未能删除官方账号来源: {source_kind or '未知来源'}"}

                replacement = next(
                    (name for name, cfg in slots.items() if (cfg or {}).get("mode") == "oauth"),
                    None,
                )
                if data.get("preferred_official_slot") == slot:
                    if replacement:
                        data["preferred_official_slot"] = replacement
                    else:
                        data.pop("preferred_official_slot", None)
                    provider_changed = True
                if data.get("app_history_slot") == slot:
                    data["app_history_slot"] = replacement
                    provider_changed = True
                if provider_changed:
                    _write_json_file(PROVIDER_SLOTS_PATH, data)
                login_home = CODEX_HOME_PATH / "sub2cli-account-homes" / slot
                try:
                    if _safe_remove_tree(login_home, CODEX_HOME_PATH / "sub2cli-account-homes"):
                        removed.append(_home_path(login_home))
                except OSError:
                    pass
        except Exception as exc:
            return {"ok": False, "error": f"删除官方账号失败: {type(exc).__name__}: {exc}"}

        accounts = _discover_codex_accounts()
        accounts["removed"] = removed
        return accounts

    def codex_account_usage(self, slot: str = "") -> dict:
        """Return local account identity and live Codex usage when this slot is active."""
        accounts = self.list_codex_accounts()
        if not accounts.get("ok"):
            return accounts
        target = (slot or "").strip() or ((accounts.get("current_official") or {}).get("slot") or "")
        account = next((a for a in accounts.get("accounts", []) if a.get("slot") == target), None)
        if not account:
            return {"ok": False, "error": "未找到官方账号"}
        auth_path = account.get("source_auth_file") or account.get("auth_file")
        expanded = _expanded_path(auth_path)
        snapshot = _codex_rpc_snapshot(
            None if account.get("is_current_provider") else (str(expanded) if expanded else None)
        )
        return {"ok": True, "account": account, "snapshot": snapshot}

    def codex_account_config_plan(self, slot: str = "") -> dict:
        """Dry-run official Codex account switch via sub2cli-inject use <slot>."""
        accounts = self.list_codex_accounts()
        target = (slot or "").strip() or ((accounts.get("current_official") or {}).get("slot") or "")
        if not target:
            return {"ok": False, "error": "没有已保存的官方账号"}
        account = next((a for a in accounts.get("accounts", []) if a.get("slot") == target), None)
        if not account:
            return {"ok": False, "error": f"未保存的官方账号: {target}"}
        if not account.get("is_registered"):
            data = _read_json_file(PROVIDER_SLOTS_PATH)
            return {
                "ok": True,
                "plan_text": "DRY-RUN: 将注册已发现的官方账号槽位并切换到该账号\n",
                "label": f"{account.get('email') or account.get('display_name') or target} · {account.get('plan_label') or 'Codex'}",
                "command": f"sub2cli-inject use {target} (auto-register + dry-run)",
                "slot": target,
                "changes": _codex_account_import_plan_changes(data, account, target),
            }
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        # Preview the SAME subcommand apply will run (add-account ... --dry-run),
        # not `use`. cmd_oauth --dry-run exercises the same JSON/email/apikey
        # validation as the real apply (it only skips the token refresh), so a
        # clean plan now reliably predicts a clean apply.
        source = _expanded_path(account.get("source_auth_file") or account.get("auth_file"))
        if not source or not source.exists():
            return {"ok": False, "error": f"账号 auth 文件不存在: {account.get('source_auth_file') or account.get('auth_file')}"}
        plan_cmd = [inject_bin, "add-account", target, "--auth-file", str(source), "--dry-run"]
        display = (account.get("display_name") or "").strip()
        if display:
            plan_cmd.extend(["--display", display])
        try:
            proc = subprocess.run(
                plan_cmd,
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_PLAN_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"dry-run 超过 {INJECT_PLAN_TIMEOUT} 秒仍未完成; 未写入任何 Codex 配置。",
            }
        except Exception as exc:
            return {"ok": False, "error": f"调用 add-account --dry-run 失败: {type(exc).__name__}: {exc}"}
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"add-account --dry-run 返回 {proc.returncode}",
                "stderr": proc.stderr,
                "stdout": proc.stdout,
            }
        return {
            "ok": True,
            "plan_text": proc.stdout,
            "label": f"{account.get('email') or account.get('display_name') or target} · {account.get('plan_label') or 'Codex'}",
            "command": f"sub2cli-inject add-account {target} --auth-file … (dry-run)",
            "slot": target,
            "changes": _parse_use_plan_changes(proc.stdout, target),
        }

    def codex_account_config_apply(self, slot: str = "") -> dict:
        """Switch Codex to a saved official OAuth slot."""
        accounts = self.list_codex_accounts()
        target = (slot or "").strip() or ((accounts.get("current_official") or {}).get("slot") or "")
        if not target:
            return {"ok": False, "error": "没有已保存的官方账号"}
        account = next((a for a in accounts.get("accounts", []) if a.get("slot") == target), None)
        if not account:
            return {"ok": False, "error": f"未保存的官方账号: {target}"}
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        source = _expanded_path(account.get("source_auth_file") or account.get("auth_file"))
        if not source or not source.exists():
            return {"ok": False, "error": f"账号 auth 文件不存在: {account.get('source_auth_file') or account.get('auth_file')}"}
        cmd = [inject_bin, "add-account", target, "--auth-file", str(source)]
        display = (account.get("display_name") or "").strip()
        if display:
            cmd.extend(["--display", display])
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _coerce_proc_text(getattr(exc, "stderr", None))
            backup_name = _extract_rollback_backup(stdout)
            rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name) if backup_name else None
            return {
                "ok": False,
                "error": f"配置超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": None,
                "backup_name": backup_name,
                "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
                "auto_rollback": rollback_result,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        backup_name = _extract_rollback_backup(proc.stdout)
        rollback_result = None
        if proc.returncode != 0 and backup_name:
            rollback_result = _rollback_inject_backup_if_needed(inject_bin, backup_name)
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "backup_name": backup_name,
            "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
            "auto_rollback": rollback_result,
        }

    def check_health(self) -> dict:
        """5 environment + state checks for the 一键检测 panel.

        Returns {ok: bool, checks: [{name, ok, severity, message, fix_hint}]}
        severity: ok | warn | err
        """
        checks: list[dict] = []

        # 1. Codex App installed?
        app_path = Path("/Applications/Codex.app")
        if app_path.is_dir():
            version = "?"
            try:
                import plistlib
                info_plist = app_path / "Contents" / "Info.plist"
                with open(info_plist, "rb") as f:
                    version = plistlib.load(f).get("CFBundleShortVersionString", "?")
            except Exception:
                pass
            checks.append({
                "name": "Codex App",
                "ok": True,
                "severity": "ok",
                "message": f"已安装 ({version})",
                "fix_hint": None,
            })
        else:
            checks.append({
                "name": "Codex App",
                "ok": False,
                "severity": "warn",
                "message": "未在 /Applications/Codex.app 找到",
                "fix_hint": "从 https://openai.com/codex/ 安装 Codex; 若装在非默认位置可忽略",
            })

        # 2. codex CLI. Finder-launched macOS apps often do not inherit the
        # user's shell PATH, so check common install locations before warning.
        codex_path, codex_source = _find_codex_cli()
        if codex_path:
            cli_version = _codex_version(codex_path)
            if codex_source == "path":
                severity = "ok"
                message = f"PATH 可用: {codex_path} ({cli_version})"
                fix_hint = None
            elif codex_source == "common":
                severity = "warn"
                message = f"已安装但不在当前 GUI PATH: {codex_path} ({cli_version})"
                fix_hint = "桌面配置 Codex App 不受影响；若要在终端用 codex，请检查 shell PATH"
            else:
                severity = "warn"
                message = f"Codex App 自带 runtime 可用: {codex_path} ({cli_version})"
                fix_hint = "未找到独立终端 codex 命令；只用桌面配置 Codex App 可忽略"
            checks.append({
                "name": "codex CLI",
                "ok": True,
                "severity": severity,
                "message": message,
                "fix_hint": fix_hint,
            })
        else:
            checks.append({
                "name": "codex CLI",
                "ok": False,
                "severity": "warn",
                "message": "未找到独立终端 codex 命令",
                "fix_hint": "桌面配置 Codex App 不受影响；需要终端 CLI 时再安装或检查 shell PATH",
            })

        # 3. Edge CDP 9222 + 当前 relay 的登录 tab
        cdp_ok = False
        cdp_sev = "err"
        cdp_msg = ""
        cdp_hint: str | None = (
            "Edge 启动加 --remote-debugging-port=9222 (推荐 LaunchAgent 守护)"
        )
        site_for_check: str | None = None
        try:
            with self._lock:
                if self._ctx is None:
                    try:
                        self._ensure_ctx()
                    except RuntimeError:
                        pass
                if self._ctx is not None:
                    site_for_check = self._ctx.site
        except Exception:
            pass
        try:
            with urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=2) as resp:
                tabs = json.load(resp)
            if site_for_check:
                tab = next(
                    (t for t in tabs if t.get("type") == "page" and site_for_check in t.get("url", "")),
                    None,
                )
                if tab:
                    cdp_ok = True
                    cdp_sev = "ok"
                    cdp_msg = f"CDP 通, {site_for_check} tab 已登录"
                    cdp_hint = None
                else:
                    cdp_ok = False
                    cdp_sev = "warn"
                    cdp_msg = f"CDP 通, 但找不到 {site_for_check} 的登录 tab"
                    cdp_hint = f"在 Edge 打开 https://{site_for_check} 并登录"
            else:
                cdp_ok = True
                cdp_sev = "ok"
                cdp_msg = f"CDP 通 ({len(tabs)} 个 tab; 未配置 relay, 跳过 tab 匹配)"
                cdp_hint = None
        except Exception as exc:
            cdp_msg = f"127.0.0.1:9222 不可达 ({type(exc).__name__})"
        checks.append({
            "name": "Edge CDP",
            "ok": cdp_ok,
            "severity": cdp_sev,
            "message": cdp_msg,
            "fix_hint": cdp_hint,
        })

        # 4. Codex 配置用 key + 端点 可达
        try:
            with self._lock:
                try:
                    ctx, cfg = self._ensure_ctx()
                except RuntimeError as exc:
                    checks.append({
                        "name": "Codex key + 端点",
                        "ok": False,
                        "severity": "warn",
                        "message": str(exc),
                        "fix_hint": None,
                    })
                    ctx = None
            if ctx is not None:
                keys = ctx.fetch_keys()
                default_key = sub2cli_lib.find_codex_key(keys, cfg)
                default_ep_url = (cfg or {}).get("default_endpoint_url") or ""
                if not default_key or not default_ep_url:
                    checks.append({
                        "name": "Codex key + 端点",
                        "ok": False,
                        "severity": "warn",
                        "message": "未设置 Codex 配置用 key 或端点",
                        "fix_hint": "确认当前中转有可用 key，并选择端点",
                    })
                else:
                    probe = sub2cli_lib.probe_endpoint(
                        default_ep_url, api_key=default_key.get("key")
                    )
                    if probe.get("ok"):
                        checks.append({
                            "name": "Codex key + 端点",
                            "ok": True,
                            "severity": "ok",
                            "message": (
                                f"{default_ep_url}  "
                                f"{probe.get('latency_ms')}ms · {probe.get('summary')}"
                            ),
                            "fix_hint": None,
                        })
                    else:
                        checks.append({
                            "name": "Codex key + 端点",
                            "ok": False,
                            "severity": "err",
                            "message": f"探测失败 (status={probe.get('status')}): {probe.get('summary')}",
                            "fix_hint": "切换端点，或在连接池里选择其他 key",
                        })
        except Exception as exc:
            checks.append({
                "name": "Codex key + 端点",
                "ok": False,
                "severity": "err",
                "message": f"检查抛错: {type(exc).__name__}: {exc}",
                "fix_hint": None,
            })

        overall_ok = all(c["ok"] for c in checks)
        return {"ok": overall_ok, "checks": checks}

    def save_group_model_columns(self, columns: list[str]) -> dict:
        """Persist user-selected model columns for group tests."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            keys, default_key = sub2cli_lib.ensure_test_key(ctx, keys)
            ep_url = (cfg or {}).get("default_endpoint_url") or sub2cli_lib.normalize_v1(ctx.domain)
            api_key = (default_key or {}).get("key", "") if default_key else ""
            groups = ctx.fetch_groups()
            group_models, models, model_error = sub2cli_lib.probe_group_models(
                ctx,
                cfg,
                keys,
                groups,
                key=default_key,
                timeout=8,
            ) if api_key else ({}, [], "未设置测试专用 key")
            if not models and api_key:
                models, model_error = sub2cli_lib.fetch_models(ep_url, api_key, timeout=8)
            model_columns = sub2cli_lib.normalize_group_model_columns(columns, models)
            cfg["group_model_columns"] = model_columns
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
        return {
            "ok": True,
            "models": models,
            "group_models": {str(k): v for k, v in group_models.items()},
            "model_error": model_error,
            "group_model_columns": model_columns,
        }

    def test_group(
        self,
        group_id: int,
        columns: list[str] | None = None,
        restore: bool = True,
    ) -> dict:
        """Temporarily move the dedicated test key, probe models, then restore.

        The relay stores a key's group server-side. Keep one backend transaction
        around switch -> requests -> restore so overlapping UI clicks or model
        discovery cannot move the test key mid-probe and misattribute results.
        """
        try:
            target_group_id = int(group_id)
        except (TypeError, ValueError):
            return {"ok": False, "error": f"分组 id 无效: {group_id}"}

        with self._group_test_lock:
            with self._lock:
                try:
                    ctx, cfg = self._ensure_ctx()
                except RuntimeError as exc:
                    return {"ok": False, "error": str(exc)}
                keys = ctx.fetch_keys()
                keys, default_key = sub2cli_lib.ensure_test_key(ctx, keys)
                if not default_key:
                    return {"ok": False, "error": "无法创建测试专用 key, 无法测试"}
                try:
                    test_key_id = int(default_key["id"])
                except (KeyError, TypeError, ValueError):
                    return {"ok": False, "error": "测试专用 key 缺少有效 id"}

                original_group_id = default_key.get("group_id")
                ep_url = (
                    (cfg or {}).get("default_endpoint_url")
                    or sub2cli_lib.normalize_v1(ctx.domain)
                )
                codex_key = self._codex_key or sub2cli_lib.find_codex_key(keys, cfg)

                if (
                    cfg.get("default_key_id") != default_key.get("id")
                    or cfg.get("default_key_name") != default_key.get("name")
                ):
                    cfg["default_key_id"] = default_key.get("id")
                    cfg["default_key_name"] = default_key.get("name")
                    sub2cli_lib.save_config(cfg, ctx.config_path)
                    self._cfg = cfg

                if default_key.get("group_id") != target_group_id:
                    ok, err = ctx.update_key_group(test_key_id, target_group_id)
                    if not ok:
                        return {"ok": False, "error": f"切分组失败: {err}"}
                    keys = ctx.fetch_keys()
                    default_key = sub2cli_lib.find_key_by_id(keys, test_key_id)
                    if not default_key:
                        return {"ok": False, "error": "切完分组后测试 key 丢失"}
                api_key = default_key.get("key", "")
                model_columns = sub2cli_lib.normalize_group_model_columns(
                    columns if columns is not None else (cfg or {}).get("group_model_columns"),
                    [],
                )

                results = {}
                try:
                    for model in model_columns:
                        try:
                            results[model] = sub2cli_lib.test_model(
                                ep_url,
                                api_key,
                                model=model,
                                text_probe="responses",
                            )
                        except Exception as exc:  # noqa: BLE001 - keep row-level result
                            results[model] = {
                                "ok": False,
                                "status": "err",
                                "latency_ms": 0,
                                "summary": f"{type(exc).__name__}: {exc}",
                            }
                finally:
                    restore_error = None
                    if restore and original_group_id is not None:
                        try:
                            keys = ctx.fetch_keys()
                            current_key = sub2cli_lib.find_key_by_id(keys, test_key_id)
                            if (
                                current_key
                                and current_key.get("group_id") != original_group_id
                            ):
                                ok, err = ctx.update_key_group(
                                    test_key_id,
                                    int(original_group_id),
                                )
                                if not ok:
                                    restore_error = err or "restore failed"
                        except Exception as exc:  # noqa: BLE001 - report but keep results
                            restore_error = f"{type(exc).__name__}: {exc}"
                    keys = ctx.fetch_keys()
                    default_key = (
                        sub2cli_lib.find_key_by_id(keys, test_key_id)
                        or default_key
                    )
                    codex_key = self._codex_key or sub2cli_lib.find_codex_key(keys, cfg)
                    keys = self._mark_codex_key(keys, codex_key)
                    self._default_key = default_key
                    self._codex_key = codex_key

                return {
                    "ok": True,
                    "group_id": target_group_id,
                    "results": results,
                    "restored": not restore or restore_error is None,
                    "restore_error": restore_error,
                    "default_key": self._serialize_key(default_key),
                    "codex_key": self._serialize_key(codex_key) if codex_key else None,
                    "keys": [self._serialize_key(k) for k in keys],
                }

    # ---- custom OpenAI-compatible APIs ----

    def list_custom_apis(self) -> dict:
        """Saved custom APIs + which one is current (for sidebar + dashboard)."""
        cfg = sub2cli_lib.load_config() or {}
        current_id = cfg.get("current_custom_api")
        apis = [
            self._serialize_custom_api(e, current_id=current_id)
            for e in _custom_apis(cfg)
            if isinstance(e, dict) and e.get("id")
        ]
        if current_id and not any(a["id"] == current_id for a in apis):
            current_id = apis[0]["id"] if apis else None
        return {"ok": True, "apis": apis, "current_id": current_id}

    def probe_custom_api(self, url: str, api_key: str = "") -> dict:
        """Connectivity test for a candidate custom endpoint: GET /v1/models.

        Returns {ok, base_url, models, model_count, summary} on success, or
        {ok: False, reachable, base_url, error} so the modal can distinguish a
        dead host from a reachable-but-rejected key.
        """
        url = (url or "").strip()
        api_key = (api_key or "").strip()
        if not url:
            return {"ok": False, "error": "URL 必填"}
        base_url = sub2cli_lib._normalize_domain(url)
        models, err = sub2cli_lib.fetch_models(base_url, api_key or None, timeout=8)
        if err and not models:
            reachable = err.startswith("HTTP 401") or err.startswith("HTTP 403")
            return {
                "ok": False,
                "reachable": reachable,
                "base_url": base_url,
                "error": (f"可达但鉴权失败 ({err}), 检查 API Key" if reachable
                          else f"连不通: {err}"),
            }
        return {
            "ok": True,
            "base_url": base_url,
            "models": models,
            "model_count": len(models),
            "summary": f"连通 · {len(models)} 个模型" if models else "连通 (该端点 /models 为空)",
        }

    def add_custom_api(self, url: str, api_key: str = "", name: str = "") -> dict:
        """Validate connectivity, then persist a custom API (key → Keychain)."""
        url = (url or "").strip()
        api_key = (api_key or "").strip()
        name = (name or "").strip()
        if not url:
            return {"ok": False, "error": "URL 必填"}
        if not api_key:
            return {"ok": False, "error": "API Key 必填"}
        base_url = sub2cli_lib._normalize_domain(url)
        # connectivity gate: must reach /models with the supplied key
        models, err = sub2cli_lib.fetch_models(base_url, api_key, timeout=8)
        if err and not models:
            return {"ok": False, "error": f"无法连通: {err}", "base_url": base_url}
        with self._lock:
            cfg = sub2cli_lib.load_config() or {}
            apis = _custom_apis(cfg)
            used = {e.get("id") for e in apis if isinstance(e, dict)}
            api_id = _unique_custom_api_id(_custom_api_base_slug(base_url, name), used)
            if not _kc_custom_api_set(api_id, api_key):
                return {"ok": False, "error": "无法把 API Key 写入 Keychain"}
            entry = {
                "id": api_id,
                "name": name or _custom_api_base_slug(base_url, name),
                "base_url": base_url,
                "model_columns": sub2cli_lib.normalize_group_model_columns(None, models),
                "created_at": int(time.time()),
            }
            apis.append(entry)
            cfg["current_custom_api"] = api_id
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        listing = self.list_custom_apis()
        listing["added_id"] = api_id
        listing["models"] = models
        return listing

    def remove_custom_api(self, api_id: str) -> dict:
        """Drop a custom API from cfg + delete its Keychain key."""
        api_id = (api_id or "").strip()
        if not api_id:
            return {"ok": False, "error": "id 必填"}
        with self._lock:
            cfg = sub2cli_lib.load_config()
            if not cfg:
                return {"ok": False, "error": "尚未配置"}
            apis = _custom_apis(cfg)
            if not _find_custom_api(cfg, api_id):
                return {"ok": False, "error": f"未保存的自定义 API: {api_id}"}
            apis[:] = [e for e in apis if e.get("id") != api_id]
            if cfg.get("current_custom_api") == api_id:
                cfg["current_custom_api"] = apis[0].get("id") if apis else None
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        _kc_custom_api_delete(api_id)
        listing = self.list_custom_apis()
        listing["removed"] = api_id
        return listing

    def select_custom_api(self, api_id: str) -> dict:
        """Mark a custom API as the current one (for highlight + config target)."""
        api_id = (api_id or "").strip()
        with self._lock:
            cfg = sub2cli_lib.load_config()
            if not cfg:
                return {"ok": False, "error": "尚未配置"}
            if not _find_custom_api(cfg, api_id):
                return {"ok": False, "error": f"未保存的自定义 API: {api_id}"}
            cfg["current_custom_api"] = api_id
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        return self.list_custom_apis()

    def update_custom_api_columns(self, api_id: str, columns: list[str]) -> dict:
        """Persist the user-selected model test columns for a custom API."""
        with self._lock:
            cfg = sub2cli_lib.load_config()
            if not cfg:
                return {"ok": False, "error": "尚未配置"}
            entry = _find_custom_api(cfg, api_id)
            if not entry:
                return {"ok": False, "error": f"未保存的自定义 API: {api_id}"}
            model_columns = sub2cli_lib.normalize_group_model_columns(columns, [])
            entry["model_columns"] = model_columns
            sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        return {"ok": True, "id": api_id, "model_columns": model_columns}

    def refresh_custom_api_models(self, api_id: str) -> dict:
        """Re-read /models for a saved custom API (populates the model picker)."""
        cfg = sub2cli_lib.load_config() or {}
        entry = _find_custom_api(cfg, api_id)
        if not entry:
            return {"ok": False, "error": f"未保存的自定义 API: {api_id}"}
        api_key = _kc_custom_api_get(api_id) or ""
        models, err = sub2cli_lib.fetch_models(entry.get("base_url", ""), api_key or None, timeout=8)
        return {
            "ok": True,
            "models": models,
            "model_error": err,
            "model_columns": entry.get("model_columns") or [],
        }

    def test_custom_api_model(self, api_id: str, model: str) -> dict:
        """Run a single chat/image probe against a saved custom API + model."""
        cfg = sub2cli_lib.load_config() or {}
        entry = _find_custom_api(cfg, api_id)
        if not entry:
            return {"ok": False, "error": f"未保存的自定义 API: {api_id}"}
        model = (model or "").strip()
        if not model:
            return {"ok": False, "error": "model 必填"}
        api_key = _kc_custom_api_get(api_id) or ""
        if not api_key:
            return {"ok": False, "error": "Keychain 里没有这个自定义 API 的 Key"}
        result = sub2cli_lib.test_model(entry.get("base_url", ""), api_key, model=model)
        return {"ok": True, "model": model, "result": result}

    def _custom_api_inject_target(self, api_id: str) -> tuple[str, str, str, list[str], str] | None:
        """Return (base_url, api_key, label, models, selected_model) for configuring Codex."""
        cfg = sub2cli_lib.load_config() or {}
        entry = _find_custom_api(cfg, api_id)
        if not entry:
            return None
        base_url = entry.get("base_url", "")
        api_key = _kc_custom_api_get(api_id) or ""
        if not base_url or not api_key:
            return None
        saved_models = [str(m).strip() for m in (entry.get("model_columns") or []) if str(m or "").strip()]
        fetched_models, _err = sub2cli_lib.fetch_models(base_url, api_key, timeout=8)
        models = sub2cli_lib.merge_model_lists(saved_models, fetched_models)
        if not models:
            models = saved_models
        selected_model = saved_models[0] if saved_models else (models[0] if models else "")
        label = f"自定义 API · {entry.get('name') or api_id} · {base_url}"
        return base_url, api_key, label, models, selected_model

    def custom_api_config_plan(self, api_id: str) -> dict:
        """Dry-run configuring Codex to a saved custom API."""
        target = self._custom_api_inject_target(api_id)
        if not target:
            return {"ok": False, "error": "未找到自定义 API 或缺少 Key"}
        url, api_key, label, models, selected_model = target
        display = f"Codex - {api_id} custom API"
        result = self._run_inject_add_api(
            url,
            api_key,
            models,
            dry_run=True,
            model=selected_model,
            protocol="chat",
            slot=api_id,
            display=display,
        )
        if result.get("ok"):
            result["label"] = label
            result["command"] = "sub2cli-inject add-api <url> --api-key-stdin --skip-check (dry-run)"
        return result

    def custom_api_config_apply(self, api_id: str) -> dict:
        """Configure Codex to a saved custom API (url + key)."""
        target = self._custom_api_inject_target(api_id)
        if not target:
            return {"ok": False, "error": "未找到自定义 API 或缺少 Key"}
        url, api_key, _label, models, selected_model = target
        return self._run_inject_add_api(
            url,
            api_key,
            models,
            dry_run=False,
            model=selected_model,
            protocol="chat",
            slot=api_id,
            display=f"Codex - {api_id} custom API",
        )
