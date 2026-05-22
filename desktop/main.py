#!/usr/bin/env python3
"""sub2cli desktop — pywebview GUI over Sub2Context.

加载 sub2cli (sibling 文件无 .py 扩展, 走 importlib.machinery.SourceFileLoader),
pywebview 开 ui/index.html, JsApi 桥到 Sub2Context.

打包 (PyInstaller --onedir 出 .app bundle) 时, sys._MEIPASS 是 bundle 解压点,
UI_DIR / SUB2CLI_PATH 路径会自动指向 bundle 内部位置.

--smoke: 启动 ~1s 自杀, CI / packaging 验证用.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
import threading
import time
import traceback


def _ensure_ca_bundle() -> None:
    """Inside the PyInstaller .app, certifi's `where()` points at
    Contents/Frameworks/certifi/cacert.pem but the file is shipped under
    Contents/Resources/certifi/cacert.pem. Fix the env so `requests`
    (used by add_relay / add_account login + GitHub checks) finds it.
    """
    exe = os.path.abspath(sys.executable)
    macos_dir = os.path.dirname(exe)
    if not macos_dir.endswith("/Contents/MacOS"):
        return
    contents_dir = os.path.dirname(macos_dir)
    for sub in ("Resources", "Frameworks"):
        cand = os.path.join(contents_dir, sub, "certifi", "cacert.pem")
        if os.path.exists(cand):
            os.environ.setdefault("SSL_CERT_FILE", cand)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", cand)
            os.environ.setdefault("CURL_CA_BUNDLE", cand)
            return


_ensure_ca_bundle()

import webview

from api import JsApi

WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 820
WINDOW_MIN_SIZE = (960, 680)

# ---- crash log ----

CRASH_LOG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "sub2cli",
    "crash-logs",
)


def _write_crash_log(exc: BaseException) -> str | None:
    try:
        os.makedirs(CRASH_LOG_DIR, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(CRASH_LOG_DIR, f"crash-{ts}-{os.getpid()}.log")
        with open(path, "w") as f:
            f.write(f"sub2cli desktop crash @ {time.ctime()}\n")
            f.write(f"pid={os.getpid()} platform={sys.platform}\n")
            f.write(f"argv={sys.argv}\n")
            f.write(f"python={sys.version}\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        os.chmod(path, 0o600)
        return path
    except Exception:
        return None


# ---- paths (PyInstaller .app + onedir + dev all supported) ----

def _resource_dir() -> str | None:
    """Where bundled data files live.

    .app bundle (Contents/MacOS/sub2cli): -> Contents/Resources
    onedir / onefile non-bundle:          -> sys._MEIPASS
    dev (no bundle):                      -> None (caller falls back)
    """
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if exe_dir.endswith("/Contents/MacOS"):
        return os.path.join(os.path.dirname(exe_dir), "Resources")
    if hasattr(sys, "_MEIPASS"):
        return getattr(sys, "_MEIPASS")
    return None


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
RESOURCE_DIR = _resource_dir()

# sub2cli script: bundled into pyscripts/ subdir (avoids EXE name collision)
if RESOURCE_DIR:
    SUB2CLI_PATH = os.path.join(RESOURCE_DIR, "pyscripts", "sub2cli")
    if not os.path.exists(SUB2CLI_PATH):
        SUB2CLI_PATH = os.path.join(REPO_ROOT, "sub2cli")
    UI_DIR = os.path.join(RESOURCE_DIR, "ui")
    if not os.path.exists(UI_DIR):
        UI_DIR = os.path.join(SCRIPT_DIR, "ui")
else:
    SUB2CLI_PATH = os.path.join(REPO_ROOT, "sub2cli")
    UI_DIR = os.path.join(SCRIPT_DIR, "ui")


def _set_app_icon_early() -> None:
    """Set the runtime app icon before the first NSWindow is shown."""
    candidates = []
    if RESOURCE_DIR:
        candidates.append(os.path.join(RESOURCE_DIR, "sub2cli.icns"))
    candidates.append(os.path.join(SCRIPT_DIR, "assets", "sub2cli.icns"))
    icon_path = next((p for p in candidates if os.path.exists(p)), None)
    if not icon_path:
        return
    try:
        from AppKit import NSApplication, NSImage  # type: ignore
        app = NSApplication.sharedApplication()
        icon = NSImage.alloc().initWithContentsOfFile_(icon_path)
        if icon is not None:
            app.setApplicationIconImage_(icon)
    except Exception:
        pass


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


def main() -> int:
    smoke = "--smoke" in sys.argv

    _set_app_icon_early()
    api = JsApi()
    window = webview.create_window(
        "sub2cli",
        url=f"file://{os.path.join(UI_DIR, 'index.html')}",
        js_api=api,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=WINDOW_MIN_SIZE,
        text_select=True,
    )

    if smoke:
        def _quit_soon() -> None:
            time.sleep(1.0)
            try:
                window.destroy()
            except Exception:
                pass
            os._exit(0)

        threading.Thread(target=_quit_soon, daemon=True).start()

    webview.start(debug=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as _exc:
        log_path = _write_crash_log(_exc)
        msg = "sub2cli desktop 崩溃: " + repr(_exc)
        if log_path:
            msg += f"\n  日志: {log_path}"
        print(msg, file=sys.stderr)
        raise
