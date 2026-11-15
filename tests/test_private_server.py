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


# -- the address, plain http, and what a private server doesn't serve ---------

def test_server_url_checks_the_address():
    from locklyctl.transport import server_url
    assert server_url(" HTTPS://Locks.Example.NET:8443/ ") == "https://locks.example.net:8443"
    assert server_url("http://127.0.0.1:8080") == "http://127.0.0.1:8080"
    assert server_url("https://[::1]:8443/base/") == "https://[::1]:8443/base"
    for bad in ("", "locks.example.net", "locks.example.net:8443", "ftp://locks.example.net", "https://",
                "https://locks.example.net:99999", "https://locks.example.net:0", "https://locks.example.net:x",
                "https://user:pw@locks.example.net", "https://locks.example.net/?a=1", "https://locks example.net"):
        with pytest.raises(ValueError):
            server_url(bad)


def test_plain_http_is_flagged_except_on_this_device():
    from locklyctl.transport import unencrypted
    assert unencrypted("http://192.168.1.20:8080") and unencrypted("http://locks.lan:8080")
    assert not unencrypted("https://locks.example.net")
    assert not any(unencrypted(u) for u in ("http://127.0.0.1:8080", "http://localhost:8080",
                                            "http://[::1]:8080", "http://app.localhost"))


def test_a_route_the_server_lacks_says_so():
    c, _ = _server_client({"/api/login": {"cod": "200", "token": "TOK"}})
    c.login_with_hash("abc123")
    with pytest.raises(LocklyError) as e:                        # oac/getNameList: Lockly's own
        c.offline_codes({"ID": "aa00112233445566778899cc", "mod": "PGK728WRHK"})
    assert e.value.cod == "404" and "Lockly cloud feature" in str(e.value)


def test_signing_in_where_no_server_answers():
    c, _ = _server_client({})
    with pytest.raises(LocklyError) as e:
        c.login_with_hash("abc123")
    assert "No locklyctl-mqtt server answers at https://srv.example:8443" in str(e.value)


def test_no_lockly_device_channel_on_a_private_server():
    c, _ = _server_client({"/api/login": {"cod": "200", "token": "TOK"}})
    c.login_with_hash("abc123")
    assert c.connect_device_channel() is False and c._mqtt is None   # Lockly's broker never hears of it


def test_mqtt_override_is_what_the_lock_holds_whole():
    from locklyctl import features as F
    assert F.mqtt_override(" locks.example.net ", "8883") == ("locks.example.net", 8883)
    assert F.mqtt_override("", None) == (None, None) and F.mqtt_override(None, 1883) == (None, 1883)
    assert F.mqtt_override("a" * 128, 9999) == ("a" * 128, 9999)
    for host, port in (("https://locks.example.net", None), ("locks.example.net:8883", None), ("a" * 129, None),
                       ("locks.example.net", 18830), ("locks.example.net", 0), ("locks.example.net", "8a")):
        with pytest.raises(ValueError):
            F.mqtt_override(host, port)
    # and the frame builder refuses what it would otherwise cut (Strings.h cuts to the field)
    lock = {"ID": "aa00112233445566778899cc", "mc": "12345678", "mod": "PGK728WRHK"}
    kw = dict(ssid="Net", password="pw", heartbeat_min=5, country="US", router_type=3)
    F.build_wifi_setup(lock, "00" * 16, host="locks.example.net", port=8883, **kw)
    with pytest.raises(ValueError):
        F.build_wifi_setup(lock, "00" * 16, host="locks.example.net", port=18830, **kw)
    with pytest.raises(ValueError):
        F.build_wifi_setup(lock, "00" * 16, host="h" * 129, port=8883, **kw)


# -- the saved sign-in keeps its server, and goes nowhere else ----------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    from locklyctl import config
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_keyring", lambda: None)
    return config


def test_the_saved_login_keeps_its_server(home):
    config = home
    config._save_credential(config.load_config(), "a@b", "H", server="https://srv.example")
    cfg = config.load_config()
    assert config.saved_server(cfg) == "https://srv.example" and config.load_credential(cfg) == "H"
    config._save_credential(cfg, "a@b", "H")                    # moved (keyring and file): server kept
    assert config.saved_server(config.load_config()) == "https://srv.example"
    config._save_credential(config.load_config(), "a@b", "H", server=None)    # now Lockly's
    assert config.saved_server(config.load_config()) is None
    config._save_credential(config.load_config(), "a@b", "H", server="https://srv.example")
    config._save_credential(config.load_config(), "a@b", None)               # forgotten: its server too
    cfg = config.load_config()
    assert config.saved_server(cfg) is None and config.load_credential(cfg) is None and cfg["email"] == "a@b"


class _Client:
    """A stand-in LocklyClient for login_flow that records where a credential went."""
    sent = []

    def __init__(self, email, server=None):
        self.email, self.server, self.locks = email, server, []

    def login_with_hash(self, pw_hash):
        _Client.sent.append((self.server, pw_hash))
        if pw_hash != "H":
            raise LocklyError("login failed")

    def load_devices(self):
        return []


def test_a_saved_login_is_never_sent_to_another_server(home, monkeypatch):
    from locklyctl import cli
    config = home
    monkeypatch.setattr(cli, "LocklyClient", _Client)
    _Client.sent = []
    config._save_credential(config.load_config(), "a@b", "H")             # saved for Lockly
    assert cli.login_flow(prompt=False, quiet=True, server="https://elsewhere.example") is None
    assert _Client.sent == []                                              # nothing went there
    assert cli.login_flow(prompt=False, quiet=True).server is None and _Client.sent == [(None, "H")]
    config._save_credential(config.load_config(), "a@b", "H", server="https://srv.example")
    _Client.sent = []
    assert cli.login_flow(prompt=False, quiet=True).server == "https://srv.example"   # no --server: its own
    assert cli.login_flow(prompt=False, quiet=True, server="https://srv.example").server == "https://srv.example"
    assert _Client.sent == [("https://srv.example", "H")] * 2
    assert cli.login_flow(prompt=False, quiet=True, server="https://other.example") is None
    assert len(_Client.sent) == 2                                          # nor to a third one


def test_a_half_typed_server_address_is_refused(capsys):
    from locklyctl import cli
    with pytest.raises(SystemExit) as e:
        cli.main(["--server", "locks.example.net", "list"])
    assert e.value.code == 2 and "isn't a server address" in capsys.readouterr().err


def test_lockly_only_features_are_refused_before_anything_is_sent():
    """Firmware and setting a lock up are Lockly cloud features; on a private
    server they're refused before the lock is woken or given a master code."""
    c, sess = _server_client({"/api/login": {"cod": "200", "token": "TOK"}})
    c.login_with_hash("abc123")
    woke = []
    c.query_status = lambda lock, **kw: woke.append(lock) or {}
    lock = {"ID": "aa00112233445566778899cc", "mod": "PGK728WRHK", "mc": "12345678"}
    for name, call in (("firmware_plan", lambda: c.firmware_plan(lock)),
                       ("firmware_offer", lambda: c.firmware_offer(lock)),
                       ("add_lock", lambda: c.add_lock("PGK728WRHK004321", "12345678", name="Front", pin="123456"))):
        with pytest.raises(LocklyError) as e:
            call()
        assert e.value.cod == "404" and "Lockly cloud feature" in str(e.value), name
    assert woke == [] and [u for u, _, _ in sess.calls if not u.endswith("/api/login")] == []


def test_a_locked_keyring_still_names_the_saved_server(home, monkeypatch):
    """The credential can't be read (a locked keyring): signing in again goes to
    the server the sign-in was kept for, not to Lockly."""
    from locklyctl import cli
    from locklyctl.gui.api import Api
    config = home
    config._save_credential(config.load_config(), "a@b", "H", server="https://srv.example")
    monkeypatch.setattr(config, "load_credential", lambda cfg: None)
    assert Api(client_factory=_Client).session() == {"signedIn": False, "email": "a@b",
                                                       "server": "https://srv.example"}
    monkeypatch.setattr(cli, "LocklyClient", _Client)
    _Client.sent = []
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: (_Client.sent.append(("asked", prompt)), "a@b")[1])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "H")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(_Client, "login", lambda self, pw: _Client.sent.append((self.server, "login")), raising=False)
    c = cli.login_flow(prompt=True, quiet=True)
    assert c.server == "https://srv.example" and ("asked", "  email on https://srv.example") in _Client.sent
