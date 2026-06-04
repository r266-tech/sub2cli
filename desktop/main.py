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
            # Assign unconditionally (not setdefault): inside the bundle the
            # shipped cacert is authoritative. A stale inherited SSL_CERT_FILE /
            # REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE from the user's shell (often a
            # Homebrew/conda path absent on this machine) would otherwise win and
            # break all TLS (relay add / login / update checks) with an opaque
            # SSLError while the correct cacert sits unused.
            os.environ["SSL_CERT_FILE"] = cand
            os.environ["REQUESTS_CA_BUNDLE"] = cand
            os.environ["CURL_CA_BUNDLE"] = cand
            return


_ensure_ca_bundle()

# NOTE: webview / api / the sibling sub2cli script are NOT imported at module
# top level. They are the riskiest startup work (native cocoa backend, the
# importlib SourceFileLoader of a bundled no-extension script) and a failure
# here on a broken/incomplete bundle would raise during module execution —
# before the __main__ crash handler runs — giving a GUI .app a silent death
# with no log. They are loaded in _bootstrap() inside the guarded path instead.
webview = None  # type: ignore[assignment]  # set by _bootstrap()
JsApi = None  # type: ignore[assignment]      # set by _bootstrap()
sub2cli_lib = None  # set by _bootstrap()

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
        # Create at 0600 from the start (argv/python version may be sensitive);
        # a kill between create and a post-hoc chmod would otherwise leave it
        # group/world-readable.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"sub2cli desktop crash @ {time.ctime()}\n")
            f.write(f"pid={os.getpid()} platform={sys.platform}\n")
            f.write(f"argv={sys.argv}\n")
            f.write(f"python={sys.version}\n\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
        return path
    except Exception:
        return None


# ---- paths (PyInstaller .app + onedir + dev all supported) ----

def _resource_roots() -> list[str]:
    """All plausible roots for bundled data files, in priority order.

    PyInstaller version/config differences place collected datas under different
    locations (Contents/Resources, Contents/Frameworks, or sys._MEIPASS). Probe
    all of them rather than committing to one, so a toolchain change doesn't
    silently break asset resolution (which previously diverged from api.py and
    crashed at import time per H7).
    """
    roots: list[str] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    exe_dir = os.path.dirname(os.path.abspath(sys.executable))
    if exe_dir.endswith("/Contents/MacOS"):
        contents = os.path.dirname(exe_dir)
        roots.append(os.path.join(contents, "Resources"))
        roots.append(os.path.join(contents, "Frameworks"))
    # de-dup preserving order
    seen: set[str] = set()
    return [r for r in roots if r and not (r in seen or seen.add(r))]


def _find_bundled(*relparts: str) -> str | None:
    """Return the first existing path for a bundled asset across resource roots."""
    for root in _resource_roots():
        cand = os.path.join(root, *relparts)
        if os.path.exists(cand):
            return cand
    return None


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
RESOURCE_DIR = next(iter(_resource_roots()), None)

# sub2cli script: bundled into pyscripts/ subdir (avoids EXE name collision).
# Probe every resource root; fall back to the dev repo layout last.
SUB2CLI_PATH = (
    _find_bundled("pyscripts", "sub2cli")
    or os.path.join(REPO_ROOT, "sub2cli")
)
UI_DIR = (
    _find_bundled("ui")
    or os.path.join(SCRIPT_DIR, "ui")
)


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


def _bootstrap() -> None:
    """Perform the fragile startup imports/loads inside the crash-handled path.

    Populates the module globals webview / JsApi / sub2cli_lib. Any failure here
    (missing native backend, misplaced bundled sub2cli script) propagates to the
    __main__ handler which writes a crash log AND shows a native alert, instead
    of dying silently at import time.
    """
    global webview, JsApi, sub2cli_lib
    import webview as _webview  # native cocoa backend
    from api import JsApi as _JsApi
    webview = _webview
    JsApi = _JsApi
    sub2cli_lib = _load_sub2cli_lib()


def _primary_window_rect() -> tuple[int, int, int, int]:
    """Return a visible, centered fallback rect for the main window."""
    screen = (webview.screens or [None])[0]
    screen_x = int(getattr(screen, "x", 0) or 0)
    screen_y = int(getattr(screen, "y", 0) or 0)
    screen_w = int(getattr(screen, "width", WINDOW_WIDTH) or WINDOW_WIDTH)
    screen_h = int(getattr(screen, "height", WINDOW_HEIGHT) or WINDOW_HEIGHT)
    width = min(WINDOW_WIDTH, max(WINDOW_MIN_SIZE[0], screen_w - 80))
    height = min(WINDOW_HEIGHT, max(WINDOW_MIN_SIZE[1], screen_h - 80))
    x = screen_x + max(20, (screen_w - width) // 2)
    y = screen_y + max(20, (screen_h - height) // 2)
    return x, y, width, height


def _restore_main_window(window: webview.Window) -> None:
    """Defensively undo bad macOS window restoration state.

    We have seen LaunchServices restore the app to a tiny off-screen frame
    after replacing the unsigned bundle in /Applications. Keeping this in the
    app startup path makes ordinary user launches recover without manual window
    hunting.
    """
    x, y, width, height = _primary_window_rect()
    time.sleep(0.25)
    try:
        import AppKit  # type: ignore
        from AppKit import NSApp  # type: ignore
        from Foundation import NSMakeRect, NSOperationQueue  # type: ignore

        def _apply() -> None:
            rect = NSMakeRect(float(x), float(y), float(width), float(height))
            for native in NSApp.windows():
                try:
                    native.deminiaturize_(None)
                    native.setFrame_display_(rect, True)
                    native.orderFrontRegardless()
                    native.makeKeyAndOrderFront_(None)
                except Exception:
                    pass
            try:
                NSApp.activateIgnoringOtherApps_(True)
            except Exception:
                pass

        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
        time.sleep(0.4)
        NSOperationQueue.mainQueue().addOperationWithBlock_(_apply)
    except Exception:
        pass
    try:
        window.restore()
    except Exception:
        pass
    try:
        window.resize(width, height)
    except Exception:
        pass
    try:
        window.move(x, y)
    except Exception:
        pass
    try:
        window.show()
    except Exception:
        pass


def main() -> int:
    smoke = "--smoke" in sys.argv

    _bootstrap()  # fragile imports/loads, inside the crash-handled path
    _set_app_icon_early()
    api = JsApi()
    x, y, width, height = _primary_window_rect()
    window = webview.create_window(
        "sub2cli",
        url=f"file://{os.path.join(UI_DIR, 'index.html')}",
        js_api=api,
        width=width,
        height=height,
        x=x,
        y=y,
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

    webview.start(_restore_main_window, args=(window,), debug=False)
    return 0


def _show_native_alert(message: str) -> None:
    """Best-effort GUI alert so a crashed .app (no terminal) isn't silent."""
    try:
        import subprocess
        # Keep it short; escape double quotes for AppleScript.
        text = message.replace('"', "'")[:900]
        subprocess.run(
            ["osascript", "-e",
             f'display alert "sub2cli 启动失败" message "{text}" as critical'],
            timeout=20,
            capture_output=True,
        )
    except Exception:
        pass


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
        # A bundled .app has no terminal; surface the failure visibly.
        _show_native_alert(msg)
        raise
