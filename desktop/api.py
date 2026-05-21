"""desktop/api.py — JsApi exposed to the pywebview frontend.

All Sub2Context-backed methods live here. Frontend calls them via
`window.pywebview.api.<method>(...)`. The Sub2Context instance is cached
per-process so the Edge CDP token isn't re-fetched on every call; relay
switching (P4) will replace `self.ctx` to point at a new domain.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import threading
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
