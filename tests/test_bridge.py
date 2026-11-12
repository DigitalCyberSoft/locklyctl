"""The localhost bridge used by the phone apps and --browser: token, Host
check, method allow-list and static files. Real sockets on 127.0.0.1 only."""
import http.client
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl.gui.server import Bridge, TOKEN_HEADER, exposed_methods


class FakeApi:
    closed = False

    def session(self): return {"signedIn": False, "email": ""}
    def check(self, lock_id): return {"id": lock_id}
    def unlock(self, lock_id): raise ValueError("Hold to unlock.")
    def close(self): FakeApi.closed = True
    def _secret(self): return "no"


def _req(bridge, method, path, body=None, token=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", bridge.port, timeout=5)
    headers = {"Host": host or f"127.0.0.1:{bridge.port}"}
    if token is not None:
        headers[TOKEN_HEADER] = token
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def test_bridge():
    b = Bridge(FakeApi()).start()
    try:
        assert b.url.startswith(f"http://127.0.0.1:{b.port}/index.html#token=")
        assert exposed_methods(b.api) == {"session", "check", "unlock"}
        # the page and its assets load without a token
        status, html = _req(b, "GET", "/")
        assert status == 200 and b"locklyctl" in html
        assert _req(b, "GET", "/app.js")[0] == 200
        assert _req(b, "GET", "/../server.py")[0] == 404
        assert _req(b, "GET", "/%2e%2e/server.py")[0] == 404
        # API calls need the token and our own Host
        assert _req(b, "POST", "/api/check", ["x"])[0] == 403
        assert _req(b, "POST", "/api/check", ["x"], token="wrong")[0] == 403
        assert _req(b, "POST", "/api/check", ["x"], token=b.token, host="evil.example:80")[0] == 403
        status, out = _req(b, "POST", "/api/check", ["4d00"], token=b.token)
        assert status == 200 and json.loads(out) == {"result": {"id": "4d00"}}
        # errors come back as messages; private and lifecycle methods stay hidden
        assert json.loads(_req(b, "POST", "/api/unlock", ["x"], token=b.token)[1]) == {"error": "Hold to unlock."}
        for name in ("_secret", "close", "nope"):
            assert _req(b, "POST", f"/api/{name}", [], token=b.token)[0] == 404, name
        assert _req(b, "POST", "/api/check", {"a": 1}, token=b.token)[0] == 400
    finally:
        b.stop()
    assert FakeApi.closed
