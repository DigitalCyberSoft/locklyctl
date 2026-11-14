"""Serve the GUI over a private localhost HTTP bridge.

The phone apps (and ``locklyctl-gui --browser``) can't use pywebview's
JavaScript bridge, so they load the same page from this server and call the
same :class:`~locklyctl.gui.api.Api` methods with ``POST /api/<method>``.

Anything else on the device can reach 127.0.0.1 too, so every API call must
carry a random per-launch token. The page receives it in the URL fragment
(never sent to the server, never logged) and returns it in a header. The Host
header is checked as well, so a web page using DNS rebinding can't reach the
API from a browser.
"""
from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .api import Api

_LOG = logging.getLogger("locklyctl.gui.server")
WEB_DIR = Path(__file__).parent / "web"
TOKEN_HEADER = "X-Locklyctl-Token"
_MAX_BODY = 64 * 1024


def exposed_methods(api: Api) -> set[str]:
    """Public Api methods the page may call (``close`` is the app's, not the page's)."""
    return {n for n in dir(api) if not n.startswith("_") and n != "close" and callable(getattr(api, n))}


class Bridge:
    """A running bridge: ``url`` opens the app; ``stop()`` shuts it down."""

    def __init__(self, api: Api | None = None, port: int = 0) -> None:
        self.api = api or Api()
        self.token = secrets.token_urlsafe(32)
        self._methods = exposed_methods(self.api)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/index.html#token={self.token}"

    def start(self) -> "Bridge":
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="locklyctl-bridge", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self.api.close()

    def _handler(self):
        bridge = self
        hosts = None    # filled lazily: the port is only known after binding

        class Handler(BaseHTTPRequestHandler):
            server_version = "locklyctl"
            sys_version = ""

            def log_message(self, fmt, *args):          # keep tokens/paths out of stderr
                _LOG.debug("bridge: " + fmt, *args)

            def _host_ok(self) -> bool:
                nonlocal hosts
                if hosts is None:
                    hosts = {f"127.0.0.1:{bridge.port}", f"localhost:{bridge.port}"}
                return self.headers.get("Host", "") in hosts

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, value) -> None:
                self._send(code, json.dumps(value).encode(), "application/json")

            def do_GET(self):  # noqa: N802 - http.server API
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                rel = self.path.split("?", 1)[0].split("#", 1)[0].lstrip("/") or "index.html"
                target = (WEB_DIR / rel).resolve()
                if not target.is_relative_to(WEB_DIR.resolve()) or not target.is_file():
                    return self._send(404, b"not found", "text/plain")
                ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype.endswith("javascript"):
                    ctype += "; charset=utf-8"
                self._send(200, target.read_bytes(), ctype)

            def do_POST(self):  # noqa: N802
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                if not hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), bridge.token):
                    return self._json(403, {"error": "This page isn't allowed to use locklyctl."})
                name = self.path.removeprefix("/api/")
                if not self.path.startswith("/api/") or name not in bridge._methods:
                    return self._json(404, {"error": f"Unknown action: {name}"})
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > _MAX_BODY:
                        return self._json(413, {"error": "Request too large."})
                    args = json.loads(self.rfile.read(length) or b"[]")
                    if not isinstance(args, list):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"error": "Bad request."})
                try:
                    result = getattr(bridge.api, name)(*args)
                except Exception as e:  # noqa: BLE001 - the page shows the message
                    _LOG.info("bridge: %s failed: %s", name, e)
                    return self._json(200, {"error": str(e) or e.__class__.__name__})
                self._json(200, {"result": result})

        return Handler


def main(argv: list[str] | None = None) -> int:
    """``locklyctl-gui --browser``: serve the app and open it in the default browser."""
    import webbrowser
    bridge = Bridge()
    print(f"locklyctl is running at {bridge.url}\nKeep this window open; press Ctrl+C to stop.")
    webbrowser.open(bridge.url)
    from .. import guard
    guard.hold_signals_while_installing()       # Ctrl+C during a firmware install takes several presses
    try:
        bridge.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0
