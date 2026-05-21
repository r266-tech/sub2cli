"""desktop/api.py — JsApi exposed to the pywebview frontend.

All Sub2Context-backed methods live here. Frontend calls them via
`window.pywebview.api.<method>(...)`. The Sub2Context instance is cached
per-process so the Edge CDP token isn't re-fetched on every call; relay
switching (P4) will replace `self.ctx` to point at a new domain.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import keyring
import requests


KEYCHAIN_SERVICE = "sub2cli"
KEYCHAIN_CREDS_SERVICE = "sub2cli:creds"
GITHUB_REPO = "r266-tech/sub2cli"
INJECT_PLAN_TIMEOUT = 90
INJECT_APPLY_TIMEOUT = 180
INJECT_ROLLBACK_TIMEOUT = 90


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


def _kc_creds_set(domain: str, email: str, password: str) -> None:
    keyring.set_password(KEYCHAIN_CREDS_SERVICE, f"{domain}|{email}", password)


def _kc_creds_get(domain: str, email: str) -> str | None:
    return keyring.get_password(KEYCHAIN_CREDS_SERVICE, f"{domain}|{email}")


def _kc_creds_delete(domain: str, email: str) -> None:
    try:
        keyring.delete_password(KEYCHAIN_CREDS_SERVICE, f"{domain}|{email}")
    except Exception:
        pass


def _sub2_login(domain: str, email: str, password: str, timeout: int = 10) -> tuple[str | None, str]:
    """POST /api/v1/auth/login {email, password} → (token, error_message).

    On success returns (token, ""). On failure returns (None, human msg).
    """
    api_base = domain.rstrip("/") + "/api/v1"
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
    return re.sub(r"sk-[A-Za-z0-9._-]{12,}", lambda m: _mask_api_key(m.group(0)), masked)


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
    slots_path = Path.home() / ".codex" / "provider-slots.json"
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


def _kc_set(domain: str, email: str, token: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, _kc_username(domain, email), token)


def _kc_get(domain: str, email: str) -> str | None:
    return keyring.get_password(KEYCHAIN_SERVICE, _kc_username(domain, email))


def _kc_delete(domain: str, email: str) -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, _kc_username(domain, email))
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
            _kc_set(ctx.domain, email, token)
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
        self._default_ep: dict | None = None
        # cache of {domain: (token, email)} captured by probe_relay so the
        # subsequent add_relay can use it without re-reading Edge.
        self._pending_tokens: dict[str, tuple[str, str | None]] = {}

    # ---- internal helpers ----

    def _ensure_ctx(self) -> tuple[Any, dict]:
        """Load config and create Sub2Context if not already cached.

        If a Keychain-stored account is marked current for the domain, its
        saved token is used. Otherwise ctx.token lazy-fetches from Edge CDP
        the first time it's accessed (P0 behavior).
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
        # If a saved account is marked current for this domain, prefer its
        # Keychain token over re-fetching from Edge CDP each launch.
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
        }

    @staticmethod
    def _serialize_group(g: dict) -> dict:
        return {
            "id": g.get("id"),
            "name": g.get("name", "?"),
            "rate_multiplier": g.get("rate_multiplier"),
            "description": g.get("description"),
        }

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

        Returns {ok, current, latest, has_update, html_url, error?}. Network
        failures and missing releases return ok=True with has_update=False so
        the frontend can silently swallow them.
        """
        current = _read_app_version()
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
                timeout=6,
            )
        except requests.RequestException as exc:
            return {"ok": True, "current": current, "latest": None,
                    "has_update": False, "html_url": None,
                    "error": f"网络错误: {type(exc).__name__}"}
        if r.status_code == 404:
            return {"ok": True, "current": current, "latest": None,
                    "has_update": False, "html_url": None,
                    "error": "尚无 release"}
        if r.status_code != 200:
            return {"ok": True, "current": current, "latest": None,
                    "has_update": False, "html_url": None,
                    "error": f"GitHub HTTP {r.status_code}"}
        try:
            body = r.json()
        except ValueError:
            return {"ok": True, "current": current, "latest": None,
                    "has_update": False, "html_url": None, "error": "响应非 JSON"}
        tag = (body.get("tag_name") or "").lstrip("v")
        html_url = body.get("html_url")
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
            "release_name": body.get("name"),
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
        """First load: account + default key + default endpoint + lists."""
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
            settings = ctx.fetch_settings() or {}
            eps = sub2cli_lib.collect_endpoints(settings, fallback_site=ctx.site)
            groups = ctx.fetch_groups()
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
            "default_endpoint": self._serialize_endpoint(default_ep) if default_ep else None,
            "endpoints": [self._serialize_endpoint(e) for e in eps],
            "keys": [self._serialize_key(k) for k in keys],
            "groups": [self._serialize_group(g) for g in groups],
            "config_path": ctx.config_path,
        }

    def refresh(self) -> dict:
        """Re-fetch everything; equivalent to bootstrap without re-creating ctx."""
        with self._lock:
            self._user = None
            self._default_key = None
            self._default_ep = None
        return self.bootstrap()

    def reveal_default_key(self) -> dict:
        """Return the full default API key (unmask). UI must opt-in."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            name = (cfg or {}).get("default_key_name")
            k = sub2cli_lib.find_key_by_name(keys, name) if name else None
            if not k:
                return {"ok": False, "error": "未设置默认 key"}
            return {"ok": True, "key": k.get("key", ""), "name": k.get("name")}

    def ping_endpoint(self, base_url: str, with_auth: bool = False) -> dict:
        """Probe /v1/models on an endpoint. Optional auth uses default key."""
        api_key: str | None = None
        if with_auth:
            with self._lock:
                try:
                    ctx, cfg = self._ensure_ctx()
                except RuntimeError as exc:
                    return {"ok": False, "error": str(exc)}
                keys = ctx.fetch_keys()
                name = (cfg or {}).get("default_key_name")
                k = sub2cli_lib.find_key_by_name(keys, name) if name else None
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
            eps = sub2cli_lib.collect_endpoints(settings, fallback_site=ctx.site)
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
            cfg["default_key_name"] = chosen.get("name")
            cfg["default_key_id"] = chosen.get("id")
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            self._default_key = chosen
        return {"ok": True, "default_key": self._serialize_key(chosen)}

    def _resolve_inject_bin(self) -> str | None:
        """Find sub2cli-inject binary; checks .app resources, repo, PATH."""
        candidates: list[str | None] = []
        res = _resource_dir()
        if res:
            try:
                local_repo_root = Path(res).resolve().parents[4]
                candidates.append(str(local_repo_root / "sub2cli-inject"))
            except IndexError:
                pass
        candidates.append(os.path.join(REPO_ROOT, "sub2cli-inject"))
        if res:
            candidates.append(os.path.join(res, "pyscripts", "sub2cli-inject-bundle"))
        candidates.extend([
            os.path.expanduser("~/.local/bin/sub2cli-inject"),
            shutil.which("sub2cli-inject"),
            shutil.which("codex-provider"),
        ])
        for p in candidates:
            if p and os.path.isfile(p) and os.access(p, os.X_OK):
                return p
        return None

    def _current_default_inject_target(self) -> tuple[str, str, str] | None:
        """Return (url, api_key, label) for the current default key+endpoint.

        Returns None if not configured. label is human-readable summary.
        """
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError:
                return None
            default_name = (cfg or {}).get("default_key_name", "")
            k = self._default_key
            if not k or (default_name and k.get("name") != default_name):
                keys = ctx.fetch_keys()
                k = sub2cli_lib.find_key_by_name(keys, default_name)
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
            return url, api_key, label

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
                "error": "未设置默认 key 或端点; 先在主面板设默认。",
            }
        url, api_key, label = target
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {
                "ok": False,
                "error": "找不到 sub2cli-inject 二进制 (应在 repo 根 或 ~/.local/bin)",
            }
        try:
            proc = subprocess.run(
                [inject_bin, "add-api", url, api_key, "--skip-check", "--dry-run"],
                capture_output=True, text=True, timeout=INJECT_PLAN_TIMEOUT,
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
            "command": f"sub2cli-inject add-api <url> <key> --skip-check (dry-run)",
            "changes": _parse_inject_plan_changes(proc.stdout, api_key),
        }

    def inject_apply(self) -> dict:
        """Real inject: call sub2cli-inject add-api <url> <key> for current default.

        Caller must show inject_plan() output first. Returns {ok, stdout, stderr}.
        """
        target = self._current_default_inject_target()
        if not target:
            return {"ok": False, "error": "未设置默认 key 或端点"}
        url, api_key, _label = target
        inject_bin = self._resolve_inject_bin()
        if not inject_bin:
            return {"ok": False, "error": "找不到 sub2cli-inject 二进制"}
        try:
            proc = subprocess.run(
                [inject_bin, "add-api", url, api_key, "--skip-check"],
                capture_output=True, text=True, timeout=INJECT_APPLY_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error": f"注入超过 {INJECT_APPLY_TIMEOUT} 秒仍未完成",
                "stdout": "",
                "stderr": "",
                "returncode": None,
                "backup_name": None,
                "rollback_command": None,
            }
        except Exception as exc:
            return {"ok": False, "error": _mask_secret_text(f"{type(exc).__name__}: {exc}", api_key)}
        backup_name = None
        m = re.search(r"sub2cli-inject rollback ([^\s]+)", proc.stdout or "")
        if m:
            backup_name = m.group(1).strip()
        return {
            "ok": proc.returncode == 0,
            "stdout": _mask_secret_text(proc.stdout, api_key),
            "stderr": _mask_secret_text(proc.stderr, api_key),
            "returncode": proc.returncode,
            "backup_name": backup_name,
            "rollback_command": f"sub2cli-inject rollback {backup_name}" if backup_name else None,
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
                capture_output=True, text=True, timeout=INJECT_ROLLBACK_TIMEOUT,
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
            self._default_ep = None

        result = self.bootstrap()
        if result.get("ok") or prev_domain is None or prev_domain == domain:
            return result

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
            # Fetch FRESH token from CDP (don't use any cached token).
            fresh = Sub2Context(
                ctx.domain, config_path=ctx.config_path,
                cdp_host=ctx.cdp_host, cdp_port=ctx.cdp_port,
            )
            try:
                token = fresh.token  # raises TokenError if CDP/login missing
            except TokenError as exc:
                return {"ok": False, "error": str(exc), "needs_login": True}
            user = fresh.fetch_user()
            if not user or not user.get("email"):
                return {"ok": False, "error": "/auth/me 拿不到 email"}
            email = user["email"]
            _kc_set(ctx.domain, email, token)
            ad = _accounts_for_domain(cfg, ctx.domain)
            existing = next((a for a in ad["saved"] if a.get("email") == email), None)
            now = int(time.time())
            if existing:
                existing["last_verified"] = now
            else:
                ad["saved"].append({"email": email, "last_verified": now})
            ad["current"] = email
            sub2cli_lib.save_config(cfg, ctx.config_path)
            self._cfg = cfg
            # Switch the active ctx to use this token going forward.
            ctx.set_token(token)
            self._user = user
        return {"ok": True, "email": email}

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
                "default_key_name": r.get("default_key_name"),
                "default_endpoint_name": r.get("default_endpoint_name"),
                "is_current": (domain == current),
            })
        return {"ok": True, "relays": items, "current": current}

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

        # 2. codex CLI in PATH?
        codex_path = shutil.which("codex")
        if codex_path:
            cli_version = "?"
            try:
                r = subprocess.run(
                    [codex_path, "--version"],
                    capture_output=True, text=True, timeout=3,
                )
                cli_version = (r.stdout or r.stderr).strip().split("\n")[0] or "?"
            except Exception:
                pass
            checks.append({
                "name": "codex CLI",
                "ok": True,
                "severity": "ok",
                "message": f"{codex_path} ({cli_version})",
                "fix_hint": None,
            })
        else:
            checks.append({
                "name": "codex CLI",
                "ok": False,
                "severity": "err",
                "message": "在 PATH 找不到 codex 命令",
                "fix_hint": "npm i -g @openai/codex 或检查当前 shell 的 PATH",
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

        # 4. 默认 key + 端点 可达
        try:
            with self._lock:
                try:
                    ctx, cfg = self._ensure_ctx()
                except RuntimeError as exc:
                    checks.append({
                        "name": "默认 key + 端点",
                        "ok": False,
                        "severity": "warn",
                        "message": str(exc),
                        "fix_hint": None,
                    })
                    ctx = None
            if ctx is not None:
                keys = ctx.fetch_keys()
                default_name = (cfg or {}).get("default_key_name", "")
                default_key = sub2cli_lib.find_key_by_name(keys, default_name)
                default_ep_url = (cfg or {}).get("default_endpoint_url") or ""
                if not default_key or not default_ep_url:
                    checks.append({
                        "name": "默认 key + 端点",
                        "ok": False,
                        "severity": "warn",
                        "message": "未设置默认 key 或端点",
                        "fix_hint": "在主面板的端点/分组 section 设默认",
                    })
                else:
                    probe = sub2cli_lib.probe_endpoint(
                        default_ep_url, api_key=default_key.get("key")
                    )
                    if probe.get("ok"):
                        checks.append({
                            "name": "默认 key + 端点",
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
                            "name": "默认 key + 端点",
                            "ok": False,
                            "severity": "err",
                            "message": f"探测失败 (status={probe.get('status')}): {probe.get('summary')}",
                            "fix_hint": "切端点或换 key (主面板)",
                        })
        except Exception as exc:
            checks.append({
                "name": "默认 key + 端点",
                "ok": False,
                "severity": "err",
                "message": f"检查抛错: {type(exc).__name__}: {exc}",
                "fix_hint": None,
            })

        overall_ok = all(c["ok"] for c in checks)
        return {"ok": overall_ok, "checks": checks}

    def test_group(self, group_id: int) -> dict:
        """Switch default key to group_id, run chat + image test, return both."""
        with self._lock:
            try:
                ctx, cfg = self._ensure_ctx()
            except RuntimeError as exc:
                return {"ok": False, "error": str(exc)}
            keys = ctx.fetch_keys()
            default_name = (cfg or {}).get("default_key_name", "")
            default_key = sub2cli_lib.find_key_by_name(keys, default_name)
            if not default_key:
                return {"ok": False, "error": "未设置默认 key, 无法测试"}
            ep_url = (cfg or {}).get("default_endpoint_url") or f"https://{ctx.site}/v1"

            if default_key.get("group_id") != group_id:
                ok, err = ctx.update_key_group(int(default_key["id"]), int(group_id))
                if not ok:
                    return {"ok": False, "error": f"切分组失败: {err}"}
                # refresh
                keys = ctx.fetch_keys()
                default_key = sub2cli_lib.find_key_by_name(keys, default_name)
                if not default_key:
                    return {"ok": False, "error": "切完分组后 key 丢失"}
                self._default_key = default_key
            api_key = default_key.get("key", "")

        # API calls outside lock — long-running, don't block other reads
        r_chat = sub2cli_lib.test_chat(ep_url, api_key, model="gpt-5.5")
        r_image = sub2cli_lib.test_image(ep_url, api_key, model="gpt-image-2")
        return {
            "ok": True,
            "group_id": group_id,
            "chat": r_chat,
            "image": r_image,
            "default_key": self._serialize_key(default_key),
        }
