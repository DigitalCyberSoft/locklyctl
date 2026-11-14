"""The private locklyctl-mqtt server mode (--server) and the Wi-Fi MQTT
override, per locklyctl-mqtt/CONTRACT.md. No real server is contacted: the
HTTP session and the Bluetooth collaborators are faked."""
import contextlib

import pytest

from locklyctl.transport import LocklyClient, LocklyError


# -- a fake requests.Session for --server mode --------------------------------

class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.headers = data, status, {}

    def json(self):
        return self._data


class _FakeSession:
    """Routes a POST by the path suffix of its URL. A route value may be a dict
    or a callable(body) -> dict."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []            # list of (url, body, headers)

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return _Resp(resp(json) if callable(resp) else resp)
        return _Resp({}, status=404)

    def call_to(self, suffix):
        return next(c for c in self.calls if c[0].endswith(suffix))


def _server_client(routes):
    c = LocklyClient("owner@example.test", server="https://srv.example:8443/")
    c._session = _FakeSession(routes)
    return c, c._session


def test_server_url_is_normalised():
    assert LocklyClient("a@b", server="https://srv.example:8443/").server == "https://srv.example:8443"
    assert LocklyClient("a@b").server is None


def test_server_login_sends_plain_json_and_reads_body_token():
    c, sess = _server_client({"/api/login": {"cod": "200", "token": "TOK", "uid": 1}})
    c.login_with_hash("abc123")
    assert c.jwt == "TOK"
    url, body, _headers = sess.call_to("/api/login")
    assert url == "https://srv.example:8443/api/login"
    assert body == {"email": "owner@example.test", "password": "abc123"}
    assert "para" not in body and "appType" not in body        # no Lockly envelope


def test_server_login_failure_raises():
    c, _ = _server_client({"/api/login": {"cod": "401"}})
    with pytest.raises(LocklyError):
        c.login_with_hash("abc123")


def test_server_load_devices_reads_arr_and_sends_bearer():
    lock = {"ID": "aa00bb0011223344", "mc": "11112222", "mod": "PGK728WRHK",
            "blename": "PGK728WRHK000009", "tz": "+8:00"}
    c, sess = _server_client({
        "/api/login": {"cod": "200", "token": "TOK"},
        "/api/qrylknew": {"cod": "200", "arr": [lock]},
    })
    c.login_with_hash("h")
    assert c.load_devices() == [lock]
    assert c.locks == [lock]
    _url, body, headers = sess.call_to("/api/qrylknew")
    assert body == {}                                          # plain, no acct/para
    assert headers.get("Authorization") == "Bearer TOK"


def test_server_exchange_relays_through_senddata():
    c, sess = _server_client({
        "/api/login": {"cod": "200", "token": "T"},
        "/api/senddata": lambda b: {"cod": "200", "ack": "0A3300" + b["cmd"][-2:]},
    })
    c.login_with_hash("h")
    ack = c.exchange_frame({"ID": "aa00bb"}, "A1B2C3D4FF")
    assert ack == "0A3300FF"
    assert c._tl.via == "server"
    _url, body, _h = sess.call_to("/api/senddata")
    assert body == {"deviceId": "aa00bb", "cmd": "A1B2C3D4FF"}


def test_server_senddata_failure_returns_none_and_records_cod():
    c, _ = _server_client({
        "/api/login": {"cod": "200", "token": "T"},
        "/api/senddata": {"cod": "3005"},
    })
    c.login_with_hash("h")
    assert c.exchange_frame({"ID": "x"}, "AABB") is None
    assert c.last_cod == "3005"


def test_server_getheartbeat_is_plain_json():
    c, sess = _server_client({
        "/api/login": {"cod": "200", "token": "T"},
        "/api/getHeartbeatTime": {"cod": "200", "mqttConfigs": {"host": "h", "port": 8883},
                                  "heartbeat": 300, "clientId": "cid"},
    })
    c.login_with_hash("h")
    body = c._post_para_des3("getHeartbeatTime", {"deviceId": "x", "model": "PGK728WRHK"})
    assert body["mqttConfigs"] == {"host": "h", "port": 8883}
    _url, sent, _h = sess.call_to("/api/getHeartbeatTime")
    assert sent == {"deviceId": "x", "model": "PGK728WRHK"}    # no para, no COMMON_BODY


def test_lockly_path_still_wraps_para_when_no_server():
    c = LocklyClient("a@b")                                    # no --server
    captured = {}
    c._post = lambda ep, body, **kw: captured.update(ep=ep, body=body) or {"cod": "200"}
    c._post_para_rsa("login", {"acct": "a@b", "pw": "h"}, auth=False)
    assert "para" in captured["body"] and "appType" in captured["body"]


# -- Wi-Fi MQTT override ------------------------------------------------------

def _wifi_harness(monkeypatch, heartbeat_body):
    """A client whose Bluetooth Wi-Fi collaborators are stubbed; returns
    (client, captured) where captured gets the host/port/heartbeat that reach
    the 0x99 builder."""
    from locklyctl import features as F
    c = LocklyClient("a@b")
    c._post_para_des3 = lambda ep, obj, **kw: dict(heartbeat_body)
    captured = {}

    def fake_build(lock, nonce, *, ssid, password, host, port, heartbeat_min,
                   country, router_type, query, password_bytes, **kw):
        captured.update(host=host, port=port, heartbeat=heartbeat_min)
        return "FRAME"

    monkeypatch.setattr(F, "build_wifi_setup", fake_build)
    monkeypatch.setattr(c, "_wifi_scan",
                        lambda lock, link, nonce: [{"ssid": "Net", "rssi": -50, "router_type": 3}])
    monkeypatch.setattr(c, "exchange_frame", lambda lock, frame, **kw: "ACK")
    monkeypatch.setattr(c, "_read_until_done", lambda *a, **k: {"code": 3, "text": "connected"})

    @contextlib.contextmanager
    def fake_session(lock):
        yield (object(), {"ble_nonce": "00" * 16})

    monkeypatch.setattr(c, "_bluetooth_session", fake_session)
    return c, captured


def test_wifi_override_writes_given_host_and_port(monkeypatch):
    c, captured = _wifi_harness(monkeypatch, {"cod": "200",
                                              "mqttConfigs": {"host": "lockly.example", "port": 8884},
                                              "heartbeat": 300})
    r = c.bluetooth_wifi_setup({"ID": "x", "mod": "PGK728WRHK"}, "Net", "pw", country="US",
                               on_event=lambda e: None, mqtt_host="private.example", mqtt_port=1884)
    assert r["code"] == 3
    assert captured["host"] == "private.example" and captured["port"] == 1884


def test_wifi_override_host_only_keeps_server_port(monkeypatch):
    c, captured = _wifi_harness(monkeypatch, {"cod": "200",
                                              "mqttConfigs": {"host": "lockly.example", "port": 8884},
                                              "heartbeat": 300})
    c.bluetooth_wifi_setup({"ID": "x", "mod": "PGK728WRHK"}, "Net", "pw", country="US",
                           on_event=lambda e: None, mqtt_host="private.example")
    assert captured["host"] == "private.example" and captured["port"] == 8884


def test_wifi_override_proceeds_when_server_settings_unavailable(monkeypatch):
    c, captured = _wifi_harness(monkeypatch, {"cod": "943"})   # getHeartbeatTime failed
    c.bluetooth_wifi_setup({"ID": "x", "mod": "PGK728WRHK"}, "Net", "pw", country="US",
                           on_event=lambda e: None, mqtt_host="h.example", mqtt_port=1884)
    assert captured["host"] == "h.example" and captured["port"] == 1884
    assert captured["heartbeat"] == 5                          # sane default keepalive


def test_wifi_without_override_raises_when_server_settings_unavailable(monkeypatch):
    c, _ = _wifi_harness(monkeypatch, {"cod": "943"})
    with pytest.raises(LocklyError):
        c.bluetooth_wifi_setup({"ID": "x", "mod": "PGK728WRHK"}, "Net", "pw", country="US",
                               on_event=lambda e: None)
