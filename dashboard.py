from __future__ import annotations

import argparse
import asyncio
import re
import signal
import sys
import threading
import webbrowser
from urllib.parse import urlparse

from aiohttp import web

from relay.dashboard.server import ROOT, create_app

TITLE = "Relay"
EXTERNAL_HOSTS = {"discord.com", "www.discord.com"}


def say(msg: str, err: bool = False) -> None:
    # pythonw / windowed launches have no console, so stdout/stderr can be None
    stream = sys.stderr if err else sys.stdout
    if stream is not None:
        print(msg, file=stream)


class Api:
    """Methods the page can call through window.pywebview.api."""

    def __init__(self) -> None:
        self.window = None

    def open_external(self, url: str) -> bool:
        u = urlparse(str(url))
        if u.scheme == "https" and (u.hostname or "").lower() in EXTERNAL_HOSTS:
            return bool(webbrowser.open(url))
        return False

    def set_window_color(self, color: str) -> bool:
        """Match the macOS window and title bar to the page background."""
        if sys.platform != "darwin" or not re.fullmatch(r"#[0-9a-fA-F]{6}", str(color)):
            return False
        try:
            from AppKit import NSApp, NSColor
            from PyObjCTools import AppHelper
        except ImportError:
            return False
        r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
        AppHelper.callAfter(lambda: [w.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b, 1.0)) for w in NSApp.windows()])
        return True

    def save_text(self, filename: str, text: str) -> bool:
        import webview
        if self.window is None:
            return False
        kind = webview.FileDialog.SAVE if hasattr(webview, "FileDialog") else webview.SAVE_DIALOG
        safe = "".join(c for c in str(filename) if c.isalnum() or c in "-_. ") or "relay.log"
        picked = self.window.create_file_dialog(kind, save_filename=safe)
        if not picked:
            return False
        path = picked if isinstance(picked, str) else picked[0]
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(text))
        return True


class ServerThread(threading.Thread):
    """Runs the dashboard server on its own event loop."""

    def __init__(self, port: int, shell: str = "native") -> None:
        super().__init__(name="relay-dashboard", daemon=True)
        self.port = port
        self.shell = shell
        self.url: str | None = None
        self.error: BaseException | None = None
        self.ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as e:
            self.error = e
        finally:
            self.ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        app = create_app(ROOT, shell=self.shell)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", self.port)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            self.url = f"http://127.0.0.1:{port}/?t={app['token']}"
            self.ready.set()
            await self._stop.wait()
        finally:
            await runner.cleanup()  # also stops the bot this dashboard started

    def shutdown(self, timeout: float = 30.0) -> None:
        if self._loop and self._stop:
            self._loop.call_soon_threadsafe(self._stop.set)
        self.join(timeout)


def style_mac_window(window) -> None:
    """Dark appearance, a title bar that matches the window, and Relay's own Dock icon and name (macOS)."""
    try:
        from AppKit import NSApp, NSAppearance, NSBundle, NSColor, NSImage
        from PyObjCTools import AppHelper
    except ImportError:
        return

    def apply() -> None:
        try:
            NSApp.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
            for w in NSApp.windows():
                w.setTitlebarAppearsTransparent_(True)
                w.setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(29 / 255, 29 / 255, 29 / 255, 1.0))
                w.setTitleVisibility_(1)  # NSWindowTitleHidden: traffic lights only, still draggable
        except Exception:
            pass
        try:
            icon = ROOT / "assets" / "icon-mac.png"
            if icon.exists():
                NSApp.setApplicationIconImage_(NSImage.alloc().initWithContentsOfFile_(str(icon)))
            NSBundle.mainBundle().infoDictionary()["CFBundleName"] = "Relay"
        except Exception:
            pass

    AppHelper.callAfter(apply)


def set_windows_icon() -> None:
    """Use Relay's icon on the Windows title bar and taskbar."""
    try:
        import ctypes
        import time
        user32 = ctypes.windll.user32
        hwnd = 0
        for _ in range(40):
            hwnd = user32.FindWindowW(None, TITLE)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            return
        icon = str(ROOT / "assets" / "icon.ico")
        for kind, size in ((1, 32), (0, 16)):  # ICON_BIG, ICON_SMALL
            handle = user32.LoadImageW(None, icon, 1, size, size, 0x10)  # IMAGE_ICON, LR_LOADFROMFILE
            if handle:
                user32.SendMessageW(hwnd, 0x80, kind, handle)  # WM_SETICON
    except Exception:
        pass


def style_window(window) -> None:
    if sys.platform == "darwin":
        style_mac_window(window)
    elif sys.platform == "win32":
        set_windows_icon()


def run_native(port: int) -> int:
    try:
        import webview
    except ImportError:
        say("The app window needs pywebview. Install it with: pip install pywebview\n"
            "Opening in your browser instead.", err=True)
        return run_browser(port, True)

    srv = ServerThread(port, "native")
    srv.start()
    srv.ready.wait(15)
    if srv.error or not srv.url:
        say(f"Couldn't start the dashboard: {srv.error}", err=True)
        return 1

    if sys.platform == "win32":
        try:  # lets the taskbar group Relay under its own icon instead of Python's
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Relay.Desktop")
        except Exception:
            pass
    api = Api()
    kw = dict(js_api=api, width=1120, height=780, min_size=(820, 560), background_color="#1d1d1d")
    api.window = webview.create_window(TITLE, srv.url, **kw)
    try:
        webview.start(style_window, api.window)
    finally:
        srv.shutdown()
    return 0


async def _browser_main(port: int, open_browser: bool) -> None:
    app = create_app(ROOT)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    real = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{real}/?t={app['token']}"
    say(f"Relay dashboard running at {url}")
    say("Keep this window open. Press Ctrl+C to quit (this also stops the bot).")
    if open_browser:
        webbrowser.open(url)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()


def run_browser(port: int, open_browser: bool) -> int:
    try:
        asyncio.run(_browser_main(port, open_browser))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        say(f"Couldn't start the dashboard on port {port}: {e}. Try --port 8766.", err=True)
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Relay control panel (desktop app)")
    ap.add_argument("--port", type=int, default=0, help="local port (default: pick a free one)")
    ap.add_argument("--browser", action="store_true", help="open in your web browser instead of an app window")
    ap.add_argument("--no-browser", action="store_true", help="with --browser: just print the link")
    a = ap.parse_args()
    if a.browser or a.no_browser:
        return run_browser(a.port, not a.no_browser)
    return run_native(a.port)


if __name__ == "__main__":
    sys.exit(main())
