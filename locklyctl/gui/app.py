"""Desktop window for locklyctl (pywebview).

Runs the page in gui/web inside the platform's own web view — WebView2 on
Windows, WKWebView on macOS, WebKitGTK or Qt on Linux — with the Python Api
behind it. Start with ``locklyctl-gui`` or ``python -m locklyctl.gui``;
add ``--browser`` to open it in your web browser instead (see server.py).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .. import config
from .api import Api

WEB_DIR = Path(__file__).parent / "web"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    debug = "--debug" in args or os.environ.get("LOCKLYCTL_GUI_DEBUG") == "1"
    config._setup_logging(debug=debug)
    if "--browser" in args:                   # no pywebview needed
        from .server import main as serve
        return serve()
    try:
        import webview
    except ImportError:
        print("The GUI needs pywebview: pip install 'locklyctl[gui]'", file=sys.stderr)
        return 1
    api = Api()
    window = webview.create_window(
        "locklyctl", url=(WEB_DIR / "index.html").as_uri(), js_api=api,
        width=1180, height=780, min_size=(860, 600), background_color="#E4E7E2")
    window.events.closing += lambda: _may_close(window, api)
    window.events.closed += api.close
    # Windows and macOS show the .exe's or .app's own icon (packaging/locklyctl.ico, .icns).
    icon = str(WEB_DIR / "icon.png") if sys.platform.startswith("linux") else None
    webview.start(debug=debug, gui=_linux_backend(), icon=icon)
    return 0


def _may_close(window, api: Api) -> bool:
    """The window's closing handler: False keeps it open while a firmware
    install runs (pywebview 6 cancels the close when a handler returns False),
    and the page says why. Quitting then would cut the install off midway."""
    running = api.installing()
    if not running:
        return True
    import threading
    script = f"window.closeRefused && window.closeRefused({json.dumps(running)})"
    # Not on this (the GUI) thread: evaluate_js waits for the page.
    threading.Thread(target=window.evaluate_js, args=(script,), daemon=True).start()
    return False


def _linux_backend() -> str | None:
    """On Linux, go straight to Qt when GTK's Python bindings (gi) aren't
    importable, instead of letting pywebview print a traceback trying GTK."""
    if not sys.platform.startswith("linux"):
        return None
    import importlib.util
    return "qt" if importlib.util.find_spec("gi") is None else None


if __name__ == "__main__":
    raise SystemExit(main())
