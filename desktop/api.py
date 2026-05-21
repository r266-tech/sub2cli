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
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path
from typing import Any


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SUB2CLI_PATH = os.path.join(REPO_ROOT, "sub2cli")


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

    # ---- internal helpers ----

    def _ensure_ctx(self) -> tuple[Any, dict]:
        """Load config and create Sub2Context if not already cached."""
        if self._ctx is not None and self._cfg is not None:
            return self._ctx, self._cfg
        cfg = sub2cli_lib.load_config()
        if not cfg:
            raise RuntimeError(
                "尚未配置 — 请先在终端运行 ./sub2cli 跑首次配置向导, "
                "或在桌面 GUI 后续版本中走内置 wizard (P3)。"
            )
        ctx = Sub2Context(cfg["domain"])
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
            "version": "0.2.0-p2",
            "sub2cli_module_path": SUB2CLI_PATH,
            "default_config_path": sub2cli_lib.default_config_path(),
            "default_domain": sub2cli_lib.DEFAULT_DOMAIN,
            "config_exists": os.path.exists(sub2cli_lib.default_config_path()),
        }

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
                return {
                    "ok": False,
                    "error": str(exc),
                    "needs_login": True,
                    "domain": cfg.get("domain"),
                }
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
        """Find sub2cli-inject binary; mirrors sub2cli._resolve_inject_bin."""
        candidates = [
            os.path.join(REPO_ROOT, "sub2cli-inject"),
            os.path.expanduser("~/.local/bin/sub2cli-inject"),
            shutil.which("sub2cli-inject"),
            shutil.which("codex-provider"),
        ]
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
            keys = ctx.fetch_keys()
            default_name = (cfg or {}).get("default_key_name", "")
            k = sub2cli_lib.find_key_by_name(keys, default_name)
            url = (cfg or {}).get("default_endpoint_url") or ""
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
                capture_output=True, text=True, timeout=20,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": f"调用 inject 失败: {type(exc).__name__}: {exc}",
            }
        if proc.returncode != 0:
            return {
                "ok": False,
                "error": f"inject --dry-run 返回 {proc.returncode}",
                "stderr": proc.stderr,
                "stdout": proc.stdout,
            }
        return {
            "ok": True,
            "plan_text": proc.stdout,
            "label": label,
            "command": f"sub2cli-inject add-api <url> <key> --skip-check (dry-run)",
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
                capture_output=True, text=True, timeout=60,
            )
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }

    def switch_relay(self, domain: str) -> dict:
        """Change active relay. Rebuilds Sub2Context, re-runs bootstrap."""
        cfg = sub2cli_lib.load_config()
        if not cfg:
            return {"ok": False, "error": "尚未配置"}
        relays = cfg.get("relays") or {}
        if domain not in relays:
            return {"ok": False, "error": f"未保存的 relay: {domain}"}
        # Reload cfg with target relay's default_* values
        relay = relays[domain] or {}
        cfg["domain"] = domain
        for f in sub2cli_lib.RELAY_FIELDS:
            cfg[f] = relay.get(f)
        sub2cli_lib.save_config(cfg, sub2cli_lib.default_config_path())
        with self._lock:
            self._ctx = None  # force re-create on next ensure
            self._cfg = None
            self._user = None
            self._default_key = None
            self._default_ep = None
        return self.bootstrap()

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

        # 4. ~/.codex slot symlink 健康
        codex_home = Path(os.path.expanduser("~/.codex"))
        auth_json = codex_home / "auth.json"
        slot_file = codex_home / "provider-slots.json"
        app_support_codex = Path(os.path.expanduser("~/Library/Application Support/Codex"))

        slot_issues: list[str] = []
        if not codex_home.exists():
            slot_issues.append("~/.codex 不存在")
        if codex_home.exists() and not slot_file.exists():
            slot_issues.append("provider-slots.json 不存在")
        if auth_json.exists() and not auth_json.is_symlink():
            slot_issues.append("~/.codex/auth.json 是真文件 (该是 symlink)")
        if app_support_codex.exists() and not app_support_codex.is_symlink():
            slot_issues.append("~/Library/Application Support/Codex 是真目录 (该是 symlink)")

        if slot_issues:
            checks.append({
                "name": "~/.codex slot",
                "ok": False,
                "severity": "warn",
                "message": "; ".join(slot_issues),
                "fix_hint": "运行 sub2cli-inject init (会把现状包成第一个 slot + 建 symlink)",
            })
        elif slot_file.exists():
            try:
                with open(slot_file) as f:
                    slots_data = json.load(f)
                current = slots_data.get("current") or "(无)"
                n = len(slots_data.get("slots", {}))
                checks.append({
                    "name": "~/.codex slot",
                    "ok": True,
                    "severity": "ok",
                    "message": f"current={current}, {n} 个 slot, symlinks OK",
                    "fix_hint": None,
                })
            except Exception as exc:
                checks.append({
                    "name": "~/.codex slot",
                    "ok": False,
                    "severity": "warn",
                    "message": f"读 provider-slots.json 失败: {exc}",
                    "fix_hint": None,
                })
        else:
            checks.append({
                "name": "~/.codex slot",
                "ok": True,
                "severity": "warn",
                "message": "尚未初始化 (没用过 sub2cli-inject)",
                "fix_hint": "sub2cli-inject init",
            })

        # 5. 默认 key + 端点 可达
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
