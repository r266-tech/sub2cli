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
import re
import base64
import errno
import fcntl
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
GITHUB_REPO = "r266-tech/sub2cli"
INJECT_PLAN_TIMEOUT = 90
INJECT_APPLY_TIMEOUT = 180
INJECT_ROLLBACK_TIMEOUT = 90
CODEX_LOGIN_TIMEOUT = 300
CODEX_AUTH_REFRESH_TIMEOUT = 25
CODEX_PROVIDER_HOME_PATH = Path(os.environ.get("CODEX_PROVIDER_HOME", str(Path.home()))).expanduser()
CODEX_HOME_PATH = Path(os.environ.get("CODEX_HOME", str(CODEX_PROVIDER_HOME_PATH / ".codex"))).expanduser()
PROVIDER_SLOTS_PATH = CODEX_HOME_PATH / "provider-slots.json"
CODEX_APP_SUPPORT_PATH = CODEX_PROVIDER_HOME_PATH / "Library" / "Application Support"
CODEXBAR_MANAGED_ACCOUNTS_PATH = (
    CODEX_APP_SUPPORT_PATH / "CodexBar" / "managed-codex-accounts.json"
)
CODEX_LOCK_PATH = CODEX_HOME_PATH / ".sub2cli-inject.lock"
UPDATE_CACHE_DIR = Path.home() / "Library" / "Caches" / "sub2cli" / "updates"
UPDATE_LOG_PATH = Path.home() / "Library" / "Logs" / "sub2cli-updater.log"

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


class CodexStateLock:
    """Shared advisory lock for mutations under CODEX_HOME."""

    _depth = 0

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._fd: int | None = None
        self._nested = False

    def __enter__(self) -> "CodexStateLock":
        if CodexStateLock._depth > 0:
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
                    raise RuntimeError(f"Codex 配置正在被另一个 sub2cli 进程修改 ({CODEX_LOCK_PATH})")
                time.sleep(0.1)

    def __exit__(self, *_exc: object) -> None:
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


def _kc_creds_set(domain: str, email: str, password: str, *, allow_prompt: bool = False) -> None:
    username = f"{domain}|{email}"
    if not allow_prompt:
        _security_set_password(KEYCHAIN_CREDS_SERVICE, username, password)
        return
    keyring.set_password(KEYCHAIN_CREDS_SERVICE, username, password)


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
    username = f"{domain}|{email}"
    if not allow_prompt:
        return _security_get_password(KEYCHAIN_CREDS_SERVICE, username)
    return keyring.get_password(KEYCHAIN_CREDS_SERVICE, username)


def _kc_creds_delete(domain: str, email: str, *, allow_prompt: bool = False) -> None:
    try:
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
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _decode_jwt_claims(token: str | None) -> dict:
    if not token or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload.encode()))
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
    login_home.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["CODEX_HOME"] = str(login_home)
    env["PATH"] = os.environ.get(
        "PATH",
        "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    )
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


# ---- macOS Keychain wrappers (sub2cli service) ----

def _kc_username(domain: str, email: str) -> str:
    return f"{domain}|{email}"


def _kc_set(domain: str, email: str, token: str, *, allow_prompt: bool = False) -> None:
    username = _kc_username(domain, email)
    if not allow_prompt:
        _security_set_password(KEYCHAIN_SERVICE, username, token)
        return
    keyring.set_password(KEYCHAIN_SERVICE, username, token)


def _kc_get(domain: str, email: str, *, allow_prompt: bool = False) -> str | None:
    username = _kc_username(domain, email)
    if not allow_prompt:
        return _security_get_password(KEYCHAIN_SERVICE, username)
    return keyring.get_password(KEYCHAIN_SERVICE, username)


def _kc_delete(domain: str, email: str, *, allow_prompt: bool = False) -> None:
    try:
        username = _kc_username(domain, email)
        if not allow_prompt:
            _security_delete_password(KEYCHAIN_SERVICE, username)
            return
        keyring.delete_password(KEYCHAIN_SERVICE, username)
    except Exception:
        # keyring's PasswordDeleteError + macOS variants — swallow on best-effort
        pass


def _try_relogin_with_saved_creds(ctx: Any, cfg: dict) -> bool:
    """If the current relay has any saved email+password in Keychain, try logging in.

    Sets ctx token + writes new token to Keychain on success. Returns True iff
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


def _accounts_for_domain(cfg: dict, domain: str) -> dict:
    """Mutable accounts entry for domain in cfg.

    Schema: {"current": email|None, "saved": [{email, last_verified}, ...]}
    Lives at cfg["accounts"][domain] — separate from cfg["relays"][domain]
    so sub2cli_lib.save_config() (which overwrites relays[domain] with
    RELAY_FIELDS only) doesn't clobber it.
    """
    accounts = cfg.setdefault("accounts", {})
    return accounts.setdefault(domain, {"current": None, "saved": []})


class JsApi:
    """Thread-safe-ish wrapper around Sub2Context for the pywebview bridge.

    pywebview invokes each js_api method in a worker thread, so we serialize
    Sub2Context mutations behind a lock (the requests session is otherwise
    not safe for concurrent use across threads).
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
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
        if not cfg:
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
        self._ctx = ctx
        self._cfg = cfg
        return ctx, cfg

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
            "daily_limit_usd": group.get("daily_limit_usd"),
            "weekly_limit_usd": group.get("weekly_limit_usd"),
            "monthly_limit_usd": group.get("monthly_limit_usd"),
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
        }

    def install_update(self) -> dict:
        """Disabled for unsigned releases; the UI opens GitHub Releases instead."""
        return {
            "ok": False,
            "error": "当前发布为 unsigned app，自动替换 .app 容易触发 Gatekeeper/权限问题；请从 release 页面手动下载并按 README 安装。",
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
            from Foundation import NSMakeSize, NSOperationQueue  # type: ignore
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

            for w in NSApp.windows():
                try:
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
                ctx, cfg = self._ensure_ctx(use_keychain=False)
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

    def inject_plan(self) -> dict:
        """Dry-run: call sub2cli-inject add-api <url> <key> --dry-run.

        Returns {ok, plan_text, label, slot_hint, command}. The plan_text is
        the raw stdout of the inject binary so the modal can display it
        verbatim — we don't try to re-parse the inject's plan format here.
        """
        target = self._current_default_inject_target()
        if not target:
            return {
                "ok": False,
                "error": "未设置 Codex 配置用 key 或端点; 先在 API Keys 点“选用”。",
            }
        url, api_key, label, models = target
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {
                "ok": False,
                "error": "找不到 sub2cli-inject 二进制 (应在 repo 根 或 ~/.local/bin)",
            }
        model_args, model_tmp = self._inject_models_args(models)
        try:
            proc = subprocess.run(
                [inject_bin, "add-api", url, "--api-key-stdin", "--skip-check", *model_args, "--dry-run"],
                input=api_key,
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_PLAN_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"dry-run 超过 {INJECT_PLAN_TIMEOUT} 秒仍未完成; 未写入任何 Codex 配置。",
                "stdout": "",
                "stderr": "",
                "timeout": INJECT_PLAN_TIMEOUT,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": _mask_secret_text(f"调用 inject 失败: {type(exc).__name__}: {exc}", api_key),
            }
        finally:
            if model_tmp is not None:
                try:
                    os.unlink(model_tmp.name)
                except OSError:
                    pass
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
            "label": label,
            "command": f"sub2cli-inject add-api <url> --api-key-stdin --skip-check (dry-run)",
            "changes": _parse_inject_plan_changes(proc.stdout, api_key),
        }

    def inject_apply(self) -> dict:
        """Real inject: call sub2cli-inject add-api <url> <key> for current Codex key.

        Returns {ok, stdout, stderr, backup_name, auto_rollback}.
        """
        target = self._current_default_inject_target()
        if not target:
            return {"ok": False, "error": "未设置 Codex 配置用 key 或端点"}
        url, api_key, _label, models = target
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        model_args, model_tmp = self._inject_models_args(models)
        try:
            proc = subprocess.run(
                [inject_bin, "add-api", url, "--api-key-stdin", "--skip-check", *model_args],
                input=api_key,
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _coerce_proc_text(getattr(exc, "stderr", None))
            backup_name = _extract_rollback_backup(stdout)
            rollback_result = _rollback_inject_backup(inject_bin, backup_name) if backup_name else None
            return {
                "ok": False,
                "error": f"配置超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                "stdout": _mask_secret_text(stdout, api_key),
                "stderr": _mask_secret_text(stderr, api_key),
                "returncode": None,
                "backup_name": backup_name,
                "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
                "auto_rollback": _mask_result_texts(rollback_result, api_key),
            }
        except Exception as exc:
            return {"ok": False, "error": _mask_secret_text(f"{type(exc).__name__}: {exc}", api_key)}
        finally:
            if model_tmp is not None:
                try:
                    os.unlink(model_tmp.name)
                except OSError:
                    pass
        backup_name = _extract_rollback_backup(proc.stdout)
        rollback_result = None
        if proc.returncode != 0 and backup_name:
            rollback_result = _rollback_inject_backup(inject_bin, backup_name)
        return {
            "ok": proc.returncode == 0,
            "stdout": _mask_secret_text(proc.stdout, api_key),
            "stderr": _mask_secret_text(proc.stderr, api_key),
            "returncode": proc.returncode,
            "backup_name": backup_name,
            "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
            "auto_rollback": _mask_result_texts(rollback_result, api_key),
        }

    def ensure_codex_app(self) -> dict:
        """Keep the current Codex App process on the enhanced sub2cli launch path."""
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        try:
            proc = subprocess.run(
                [inject_bin, "ensure-app"],
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _coerce_proc_text(getattr(exc, "stderr", None))
            return {
                "ok": False,
                "error": f"刷新 Codex App 增强超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                "stdout": stdout,
                "stderr": stderr,
                "returncode": None,
            }
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        output = f"{proc.stdout}\n{proc.stderr}"
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "restarted": "正在重新打开" in output or "已重新打开" in output,
        }

    def restore_codex_app(self) -> dict:
        """Restart Codex App without sub2cli runtime enhancements."""
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        try:
            proc = subprocess.run(
                [inject_bin, "restore-app"],
                capture_output=True,
                text=True,
                env=_inject_subprocess_env(),
                timeout=INJECT_APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _coerce_proc_text(getattr(exc, "stdout", None) or getattr(exc, "output", None))
            stderr = _coerce_proc_text(getattr(exc, "stderr", None))
            return {
                "ok": False,
                "error": f"恢复原生 Codex App 超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                "stdout": stdout,
                "stderr": stderr,
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
        }

    def add_relay(self, url: str, email: str = "", password: str = "") -> dict:
        """Add a new relay to config. Optionally log in with email+password.

        Steps:
          1. Normalize URL; probe /api/v1/settings/public to confirm it's a Sub2API instance.
          2. Write cfg["relays"][domain] with empty RELAY_FIELDS.
          3. If email+password provided: POST /auth/login → token. Store creds + token in Keychain. Register account.
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

            if token and email:
                _kc_set(domain, email, token)
                if save_creds:
                    _kc_creds_set(domain, email, password)
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

        return self.bootstrap()

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

    def remove_relay(self, domain: str) -> dict:
        """Remove a relay from cfg + clear all its Keychain entries (tokens + creds).

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

        # Clear Keychain entries (tokens + creds) for every saved account under this relay.
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

    # ---- accounts (Keychain-backed) ----

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
        store token in macOS Keychain, register in cfg.accounts.

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

        Stores token + creds in Keychain, registers the account, activates it.
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
            _kc_set(ctx.domain, email, token)
            _kc_creds_set(ctx.domain, email, password)
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
        return self.bootstrap()

    def switch_account(self, email: str) -> dict:
        """Activate a saved account.

        Semantics (per V's request): "logout + login again" — if we have the
        password in Keychain we POST /auth/login to get a FRESH token rather
        than reusing the cached one. Fallback to cached Keychain token only
        when password isn't saved (e.g. legacy Edge-imported accounts).
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
                    return {"ok": False, "error": f"{email} 没保存密码且 Keychain 无 token, 请重新添加账号"}

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
        """Remove an account from Keychain and cfg."""
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
        snapshot = _codex_rpc_snapshot(None if account.get("is_current_provider") else (auth_path or "").replace("~", str(Path.home()), 1))
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
        try:
            proc = subprocess.run(
                [inject_bin, "use", target, "--dry-run"],
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
            return {"ok": False, "error": f"调用 use --dry-run 失败: {type(exc).__name__}: {exc}"}
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"use --dry-run 返回 {proc.returncode}",
                "stderr": proc.stderr,
                "stdout": proc.stdout,
            }
        return {
            "ok": True,
            "plan_text": proc.stdout,
            "label": f"{account.get('email') or account.get('display_name') or target} · {account.get('plan_label') or 'Codex'}",
            "command": f"sub2cli-inject use {target} (dry-run)",
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
        refresh_ok, refresh_error, refresh_snapshot = _refresh_codex_auth_file(source)
        if not refresh_ok:
            return {
                "ok": False,
                "error": refresh_error or "该官方账号登录态已失效，请重新登录后再配置",
                "stdout": "",
                "stderr": "",
                "returncode": None,
                "auth_refresh": refresh_snapshot,
            }
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
            rollback_result = _rollback_inject_backup(inject_bin, backup_name) if backup_name else None
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
            rollback_result = _rollback_inject_backup(inject_bin, backup_name)
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
                "fix_hint": "从 https://codex.io 下载安装; 若装在非默认位置可忽略",
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
                        "fix_hint": "在 API Keys 点“选用”，并选择端点",
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
                            "fix_hint": "切端点或在 API Keys 换 key",
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

    def test_group(self, group_id: int, columns: list[str] | None = None) -> dict:
        """Switch the dedicated test key to group_id and run selected model columns."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            keys, default_key = sub2cli_lib.ensure_test_key(ctx, keys)
            if not default_key:
                return {"ok": False, "error": "无法创建测试专用 key, 无法测试"}
            ep_url = (cfg or {}).get("default_endpoint_url") or sub2cli_lib.normalize_v1(ctx.domain)

            codex_key = self._codex_key or sub2cli_lib.find_codex_key(keys, cfg)
            if default_key.get("group_id") != group_id:
                ok, err = ctx.update_key_group(int(default_key["id"]), int(group_id))
                if not ok:
                    return {"ok": False, "error": f"切分组失败: {err}"}
                # refresh
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
            else:
                keys = self._mark_codex_key(keys, codex_key)
            api_key = default_key.get("key", "")
            model_columns = sub2cli_lib.normalize_group_model_columns(
                columns if columns is not None else (cfg or {}).get("group_model_columns"),
                [],
            )

        # API calls outside lock — long-running, don't block other reads
        results = {}
        for model in model_columns:
            results[model] = sub2cli_lib.test_model(ep_url, api_key, model=model)
        return {
            "ok": True,
            "group_id": group_id,
            "results": results,
            "default_key": self._serialize_key(default_key),
            "codex_key": self._serialize_key(codex_key) if codex_key else None,
            "keys": [self._serialize_key(k) for k in keys],
        }
