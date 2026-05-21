#!/usr/bin/env python3
"""sub2cli desktop — pywebview GUI over Sub2Context.

最小可运行骨架:
- 加载同仓库的 ../sub2cli 作为 sub2cli_lib (单文件无 .py 扩展, 走 importlib)
- pywebview 打开 ui/index.html, 用 js_api 暴露 JsApi 给前端
- --smoke: 启动 ~1s 后自杀, 用于 CI / packaging spike 验证

主面板 / 一键检测 / 一键注入 等真实逻辑在后续 phase 接入 (P2-P4).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import threading
import time

import webview

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
SUB2CLI_PATH = os.path.join(REPO_ROOT, "sub2cli")
UI_DIR = os.path.join(SCRIPT_DIR, "ui")


def _load_sub2cli_lib():
    """Load the sibling sub2cli script (no .py extension) as a module.

    Python's importlib default loader inference is extension-based, so a file
    named "sub2cli" (no .py) gives a spec with loader=None. We pass an
    explicit SourceFileLoader.
    """
    loader = importlib.machinery.SourceFileLoader("sub2cli_lib", SUB2CLI_PATH)
    spec = importlib.util.spec_from_loader("sub2cli_lib", loader)
    if spec is None:
        raise RuntimeError(f"cannot build spec for {SUB2CLI_PATH}")
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


sub2cli_lib = _load_sub2cli_lib()


class JsApi:
    """Methods exposed to the frontend via window.pywebview.api.<name>().

    Keep methods cheap + side-effect-free for P1; P2-P4 will add the real
    Sub2Context-backed endpoints (fetch_user/keys/groups, dry-run inject, etc).
    """

    def hello(self) -> dict:
        return {
            "app": "sub2cli desktop",
            "version": "0.1.0-p1",
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


def main() -> int:
    smoke = "--smoke" in sys.argv

    api = JsApi()
    window = webview.create_window(
        "sub2cli",
        url=f"file://{os.path.join(UI_DIR, 'index.html')}",
        js_api=api,
        width=960,
        height=640,
        min_size=(720, 480),
    )

    if smoke:
        def _quit_soon() -> None:
            time.sleep(1.0)
            try:
                window.destroy()
            except Exception:
                pass

        threading.Thread(target=_quit_soon, daemon=True).start()

    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
