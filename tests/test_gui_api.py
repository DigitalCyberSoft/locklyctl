"""GUI backend tests — the Api class with a fake client; no window, no network."""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl import config
from locklyctl import features as F
from locklyctl.gui.api import Api, split_unit
from locklyctl.transport import LocklyClient, LocklyError

MC, UUID = "12345678", "aa0011223344556677889900"
B2 = "bb0011223344556677889900"


def _set_home(value):
    """Point locklyctl's config dir at ``value``; None restores the default."""
    if value is None:
        os.environ.pop("LOCKLYCTL_HOME", None)
    else:
        os.environ["LOCKLYCTL_HOME"] = value


class FakeClient:
    def __init__(self, email):
        self.email = email
        self.pw_hash = None
        self.status_cache = {}
        self.sent = []
        self.fw_installs = []
        self.locks = [
            {"ID": UUID, "na": "2A Office", "mod": "PGD7Y", "hubid": "PGH200X", "mc": MC},
            {"ID": B2, "na": "1204", "mod": "PGK728WRHK", "mc": MC},
        ]

    def login(self, password):
        if password != "right":
            raise LocklyError("login failed: wrong password")
        self.pw_hash = "H"

    def login_with_hash(self, h):
        if h != "H":
            raise LocklyError("login failed")
        self.pw_hash = h

    def load_devices(self): return self.locks
    def prime_cached_status(self, locks): return 0
    def close(self): pass

    warmed = None

    def connect_device_channel(self):
        self.warmed.set()
        return True

    def list_users(self, lock):
        if lock["ID"] == UUID:
            return [{"userAcuId": 0, "name": "Owner", "email": "o@x", "type": "OWNER", "timeType": "NO_LIMIT",
                     "pw": "1234", "pwStatus": "Y", "fptCount": 2, "faceCount": 1, "rfidCount": 0, "disabled": "N"},
                    {"userAcuId": 3, "name": "Dana", "lastName": "", "email": "", "type": "GUEST", "timeType": "WEEK",
                     "weekData": "31", "startTime": 1745974800000, "endTime": 1746007140000,
                     "fptCount": 0, "faceCount": 1, "rfidCount": 0, "disabled": "N", "multipleVerific": 1}]
        return [{"userAcuId": 0, "name": "Owner", "email": "O@X", "type": "OWNER", "timeType": "NO_LIMIT",
                 "fptCount": 0, "faceCount": 0, "rfidCount": 0, "disabled": "N"},
                {"userAcuId": 5, "name": "Dana", "lastName": "R", "email": "", "type": "GUEST", "timeType": "NO_LIMIT",
                 "fptCount": 1, "faceCount": 0, "rfidCount": 0, "disabled": "Y"}]

    calls = None

    def _log(self, *a):
        if self.calls is None:
            self.calls = []
        self.calls.append(a)

    def lock_user_table(self, lock):
        return [{"pwd_id": 0, "user_id": 0, "user_type": 0x21, "pin": "1234"},
                {"pwd_id": 4, "user_id": 3, "user_type": 0x42, "pin": ""}] if lock["ID"] == UUID else []

    def create_access_user(self, lock, **kw):
        self._log("create", lock["ID"], kw)
        return {"userAcuId": 40, "pwdId": 7, "crdntlId": 9}

    def change_access_user_pin(self, lock, user, pin, *, user_type, date):
        self._log("pin", user["acuIndex"], pin, user_type, date)

    def delete_access_user(self, lock, *, user_acu_id, pwd_id, user_type):
        self._log("delete", user_acu_id, pwd_id, user_type)

    def _rest_ok(self, endpoint, req):
        self._log("rest", endpoint, req)
        return {}

    def get_history(self, lock, since_ms):
        """getlkhist's event shape, as Lockly returned it for a Visage (2026-11-12)."""
        return [{"tm": 1794464102000, "co": "92", "na": "Dana", "pid": 7, "id": 1, "logVer": 0},
                {"tm": 1794464133000, "co": "999", "na": "", "pid": 65535, "id": 3, "logVer": 0},
                {"tm": 1794464119000, "co": "117", "na": "", "pid": 65535, "id": 2, "logVer": 0}], since_ms

    def get_event_log(self, lock, since_ms):
        """get_event_log's shape: Lockly's event log reshaped like getlkhist (2026-11-13)."""
        return [{"tm": 1794557162000, "co": "92", "na": "Dana", "pid": "7", "id": 1},
                {"tm": 1794557165000, "co": "42", "na": "", "pid": "", "id": 2}]

    def query_status(self, lock):
        return {"is_locked": True, "lock_type": 105, "firmware_version": "1.14.24",
                "sub_version": "1.00.72", "wifi_version": "2.02.72", "ble_version": "1.14.24"}

    # Lockly's raw firmware answers (made-up files); the client's own code
    # turns them into the offer and the install plan.
    def _firmware_reply(self, lock, lock_type=None):
        if F.firmware_check_kind(lock) == F.FW_SINGLE:
            return {"cod": "200", "value": "2.02.80"}
        reply = {"cod": "200", "verNum": "1.14.34", "downloadUrl": "https://files.example.com/fw/"}
        for prefix, ver, size in (("front", "1.00.95", "87"), ("back", "1.00.76", "35"),
                                  ("hub", "2.02.72", "1180"), ("ble", "1.14.28", "280")):
            reply.update({prefix + "Ver": ver, prefix + "Url": f"https://files.example.com/fw/{prefix}.bin",
                          prefix + "Name": f"{prefix}.bin", prefix + "Size": size, prefix + "Crc": "abc"})
        return reply

    firmware_offer = LocklyClient.firmware_offer
    firmware_plan = LocklyClient.firmware_plan
    _offer_from_reply = LocklyClient._offer_from_reply
    _FW_PARTS = LocklyClient._FW_PARTS
    fw_install_error = None

    def firmware_install(self, lock, send, download_url, on_event, bluetooth=False):
        self.fw_bluetooth = bluetooth
        on_event({"stage": "install", "progress": 50, "text": "Installing 50 %"})
        if self.fw_install_error:
            raise self.fw_install_error
        self.fw_installs.append((lock["ID"], [(p["part"], p["version"]) for p in send], download_url))
        return {"versions": {p["part"]: p["version"] for p in send}}

    # 0A9B as a Visage sent it: one one-time slot 07:00-21:00, switched off, first entry
    welcome_table = "00" + "04" + "01" + "08" + "8007001500" + "00FFFFFFFF" * 7 + "00" + "00"

    def exchange_built(self, lock, build):
        build("AB" * 16)
        return "ACK", self.welcome_table

    def lock(self, lock): return True, "lock ACK"
    def unlock(self, lock): return False, "lock rejected the command: FF — wrong password"

    def send_built(self, lock, build, needs_nonce=True):
        self.sent.append((build("AB" * 16), needs_nonce))
        return True, "lock ACK"


def _api_in_tmp_home(fn):
    def wrapper():
        with tempfile.TemporaryDirectory() as d:
            old, real_kr = os.environ.get("LOCKLYCTL_HOME"), config._keyring
            _set_home(d)
            config._keyring = lambda: None
            try:
                fn(Api(client_factory=FakeClient))
            finally:
                _set_home(old)
                config._keyring = real_kr
    wrapper.__name__ = fn.__name__
    return wrapper


def test_split_unit():
    assert split_unit("2A Office") == ("2A", "Office")
    assert split_unit("1204") == ("1204", "")
    assert split_unit("3c vision") == ("3C", "vision")
    assert split_unit("Garage") == ("", "Garage")
    assert split_unit("") == ("", "")


@_api_in_tmp_home
def test_session_and_sign_in(api):
    assert api.session()["signedIn"] is False            # nothing saved yet
    try:
        api.sign_in("a@b", "wrong", True)
    except LocklyError:
        pass
    else:
        raise AssertionError("bad password accepted")
    assert api.sign_in("a@b", "right", True)["storedIn"] == "file"
    assert Api(client_factory=FakeClient).session()["signedIn"] is True   # saved login works
    api.sign_out()
    assert Api(client_factory=FakeClient).session()["signedIn"] is False


@_api_in_tmp_home
def test_lock_views_and_actions(api):
    api.sign_in("a@b", "right", False)
    views = {v["id"]: v for v in api.locks()}
    office = views[UUID]
    assert (office["unit"], office["rest"], office["state"]) == ("2A", "Office", "unknown")
    assert office["welcomeForm"] == F.WELCOME_46 and views[B2]["welcomeForm"] == F.WELCOME_9B
    r = api.lock(UUID)
    assert r["ok"] and r["lock"]["state"] == "locked"
    r = api.unlock(UUID)                                   # rejected → state unchanged
    assert not r["ok"] and "wrong password" in r["detail"] and r["lock"]["state"] == "locked"
    try:
        api.lock("nope")
    except LocklyError:
        pass
    else:
        raise AssertionError("unknown lock accepted")


@_api_in_tmp_home
def test_keep_unlocked(api):
    import locklyctl.gui.api as A
    from datetime import datetime
    from locklyctl import protocol as P

    class Now(datetime):                       # a fixed clock, far enough ahead to be "not over yet"
        @classmethod
        def now(cls, tz=None):
            return datetime(2099, 1, 1, 14, 5, 30, tzinfo=tz)
    real, A.datetime = A.datetime, Now
    try:
        api.sign_in("a@b", "right", False)
        c = api._client
        r = api.keep_unlocked(B2, 60)
        assert r["until"] == "15:05" and r["lock"]["keepUnlockedUntil"] == "15:05"
        frame, _ = c.sent[-1]
        plain = P.AES.new(P.derive_aes_key(MC, B2), P.AES.MODE_ECB).decrypt(bytes.fromhex(frame)[6:-2]).hex().upper()
        # the door's own slot kept as it was (off, first entry); the new one-time slot on, auto entry
        assert "0301" + "08" + "8007001500" + "800E050F05" + "00FFFFFFFF" * 6 + "02" + "02" in plain
        assert config.load_config()["keep_unlocked"][B2]["slots"] == [[0x80, 14, 5, 15, 5]]
        # later, once the lock has run and switched it off, it's cleared to make room
        c.welcome_table = "00040108" + "8007001500" + "800E050F05" + "00FFFFFFFF" * 6 + "02" + "00"
        api.keep_unlocked(B2, 30)
        plain = P.AES.new(P.derive_aes_key(MC, B2), P.AES.MODE_ECB).decrypt(bytes.fromhex(c.sent[-1][0])[6:-2]).hex().upper()
        assert "0301" + "08" + "8007001500" + "800E050E23" + "00FFFFFFFF" * 6 in plain
        # a new window replaces one from here that's still running; past the
        # lock's midnight it takes a second slot from 00:00
        c.welcome_table = "00040108" + "8007001500" + "800E050E23" + "00FFFFFFFF" * 6 + "02" + "02"
        r = api.keep_unlocked(B2, 600)
        assert r["until"] == "00:05 tomorrow"
        plain = P.AES.new(P.derive_aes_key(MC, B2), P.AES.MODE_ECB).decrypt(bytes.fromhex(c.sent[-1][0])[6:-2]).hex().upper()
        assert "0301" + "08" + "8007001500" + "800E05173B" + "8000000005" + "00FFFFFFFF" * 5 + "06" + "06" in plain
        assert config.load_config()["keep_unlocked"][B2]["slots"] == [[0x80, 14, 5, 23, 59], [0x80, 0, 0, 0, 5]]
        try:
            api.keep_unlocked(UUID, 60)                    # a PGD7Y: single-slot Welcome Mode
        except ValueError:
            pass
        else:
            raise AssertionError("single-slot lock accepted")
        r = api.end_keep_unlocked(B2)
        assert r["ok"] and B2 not in config.load_config()["keep_unlocked"]
    finally:
        A.datetime = real


@_api_in_tmp_home
def test_firmware_check(api):
    api.sign_in("a@b", "right", False)
    r = api.firmware(B2)                                     # a Visage
    parts = {p["name"]: p for p in r["parts"]}
    assert r["release"] == "1.14.34" and r["lockFirmware"] == "1.14.24"
    assert parts["Back board"]["newer"] and parts["Bluetooth chip"]["newer"]
    assert not parts["Wi-Fi module"]["newer"]                           # same version
    assert parts["Front board"]["current"] == "1.14.24" and not parts["Front board"]["newer"]
    assert parts["Bluetooth chip"]["byPhone"] and not parts["Back board"]["byPhone"]
    assert "battery" in r["install"]["blocked"]              # this status reports none
    r = api.firmware(UUID)                                   # a PGD7Y: Bluetooth only
    assert [(p["name"], p["newer"], p["byPhone"]) for p in r["parts"]] == [("Lock firmware", True, True)]
    assert r["install"] == {"parts": [], "blocked": "This lock's firmware is installed from a phone "
                                                    "over Bluetooth: use the Lockly app."}


@_api_in_tmp_home
def test_firmware_install(api):
    import time
    api.sign_in("a@b", "right", False)
    c = api._client
    reported = FakeClient.query_status(c, None)
    c.query_status = lambda lock: {**reported, "battery_percent": 80}
    r = api.firmware(B2)
    # the back board is the one part behind that goes over Wi-Fi (Bluetooth is the phone's)
    assert r["install"] == {"blocked": "", "parts": [
        {"part": "back", "name": "Back board", "version": "1.00.76", "current": "1.00.72"}]}

    def finished():
        for _ in range(200):
            st = api.firmware_install_status(B2)
            if not st["running"]:
                return st
            time.sleep(0.01)
        raise AssertionError("install didn't finish")

    try:
        api.firmware_install(B2, [{"part": "back", "version": "1.00.70"}])   # not what's offered now
    except ValueError as e:
        assert "changed since the check" in str(e)
    else:
        raise AssertionError("a changed plan was installed")
    assert c.fw_installs == []
    api.firmware_install(B2, r["install"]["parts"])
    st = finished()
    assert st["ok"] and st["versions"] == {"back": "1.00.76"} and st["text"] == "Installing 50 %"
    assert [(p["part"], p["state"], p["now"]) for p in st["parts"]] == [("back", "done", "1.00.76")]
    assert c.fw_installs == [(B2, [("back", "1.00.76")], "https://files.example.com/fw/")]
    assert c.fw_bluetooth is False                          # not asked to use Bluetooth

    c.fw_install_error = LocklyError("The lock reported a failure while installing: crc", "failure")
    api.firmware_install(B2, r["install"]["parts"])
    st = finished()
    assert st["ok"] is False and st["error"].endswith("crc") and len(c.fw_installs) == 1
    assert [p["state"] for p in st["parts"]] == ["unconfirmed"]          # it never said

    c.query_status = lambda lock: {**reported, "battery_percent": 30}           # isBatteryLow
    assert "30 %" in api.firmware(B2)["install"]["blocked"]
    try:
        api.firmware_install(B2, r["install"]["parts"])
    except ValueError as e:
        assert "30 %" in str(e)
    else:
        raise AssertionError("installed on a low battery")


@_api_in_tmp_home
def test_activity_reads_getlkhist_fields(api):
    api.sign_in("a@b", "right", False)
    rows = api.activity(B2)
    assert [(r["what"], r["who"]) for r in rows] == [          # newest first, by tm
        ("Event 999", ""), ("Door sensor closing", ""), ("Unlocked via Visage ID", "Dana")]
    assert all(len(r["time"]) == 16 and r["time"][4] == "-" for r in rows)   # "YYYY-MM-DD HH:MM"


@_api_in_tmp_home
def test_activity_reads_the_event_log_for_a_wifi_visage(api):
    """A Visage with a Wi-Fi client id reads Lockly's event log, as the app
    does; the fake's getlkhist would give different rows."""
    api.sign_in("a@b", "right", False)
    api._client.locks[1]["clientId"] = "th_test"
    assert [v["eventLog"] for v in api.locks()] == [False, True]
    rows = api.activity(B2)
    assert [(r["what"], r["who"]) for r in rows] == [("Auto lock", ""), ("Unlocked via Visage ID", "Dana")]



@_api_in_tmp_home
def test_offline_is_what_lockly_said(api):
    api.sign_in("a@b", "right", False)
    api._client.status_cache[B2] = {"reachable": False, "ts": 1, "offline": True}
    api._client.status_cache[UUID] = {"reachable": False, "ts": 1}
    views = {v["id"]: v for v in api.locks()}
    assert (views[B2]["state"], views[B2]["offline"]) == ("asleep", True)
    assert (views[UUID]["state"], views[UUID]["offline"]) == ("asleep", False)


@_api_in_tmp_home
def test_view_reports_the_lock_clock(api):
    api.sign_in("a@b", "right", False)
    c = api._client
    c.status_cache[B2] = {"reachable": True, "ts": 1, "is_locked": True, "clock_offset_s": -7300}
    c.status_cache[UUID] = {"reachable": True, "ts": 1, "is_locked": True}
    views = {v["id"]: v for v in api.locks()}
    assert (views[B2]["clockKnown"], views[B2]["clockOffset"]) == (True, -7300)
    assert (views[UUID]["clockKnown"], views[UUID]["clockOffset"]) == (False, None)


@_api_in_tmp_home
def test_firmware_job_follows_each_part_and_the_clock(api):
    """Each part's state as the install reports progress, the restart and the
    versions the lock gives back; the thread's body, run synchronously."""
    api.sign_in("a@b", "right", False)
    c = api._client
    part = lambda key, name, new, old: {"part": key, "name": name, "version": new, "current": old,
                                        "state": "sending", "now": ""}
    job = {"running": True, "ok": None, "stage": "start", "progress": None, "away": None, "text": "",
           "error": "", "versions": {}, "clockKnown": False, "clockOffset": None,
           "parts": [part("front", "Front board", "1.00.95", "1.00.87"), part("back", "Back board", "1.00.76", "1.00.72")]}
    seen = []

    def install(lock, send, url, on_event, bluetooth=False):
        for ev in ({"stage": "install", "progress": 50, "text": "Installing 50 %"},
                   {"stage": "verify", "progress": None, "away": 0, "text": "The lock is restarting"},
                   {"stage": "verify", "progress": None, "versions": {"front": "1.00.95", "back": "1.00.72"},
                    "clockOffset": -3, "text": "The lock still reports Back board 1.00.72"}):
            on_event(ev)
            seen.append([p["state"] for p in job["parts"]])
        raise LocklyError("After the upgrade the lock still reports Back board 1.00.72.", "verify")
    c.firmware_install = install
    api._run_firmware_install(c.locks[1], {"send": [], "downloadUrl": ""}, job)
    assert seen == [["sending", "sending"], ["restarting", "restarting"], ["done", "behind"]]
    assert [(p["state"], p["now"]) for p in job["parts"]] == [("done", "1.00.95"), ("behind", "1.00.72")]
    assert (job["ok"], job["running"], job["clockKnown"], job["clockOffset"]) == (False, False, True, -3)


@_api_in_tmp_home
def test_connection_setting(api):
    api.sign_in("a@b", "right", False)
    assert api.connection()["mode"] == "auto" and api._client.connection == "auto"     # the default
    assert api.set_connection("bluetooth")["mode"] == "bluetooth" and api._client.connection == "bluetooth"
    try:
        api.set_connection("wifi")
    except ValueError:
        pass
    else:
        raise AssertionError("an unknown setting was saved")
    assert config.load_config()["connection"] == "bluetooth"
    api.sign_in("a@b", "right", False)                      # a new session takes the saved setting
    assert api._client.connection == "bluetooth"


@_api_in_tmp_home
def test_view_reports_the_route_and_the_clock_fix(api):
    api.sign_in("a@b", "right", False)
    c = api._client
    c.status_cache[B2] = {"reachable": True, "ts": 1, "is_locked": True, "via": "bluetooth", "offline": True,
                          "clock_offset_s": 0, "clock_fix": {"was": -10800, "fixed": True, "detail": ""}}
    c.status_cache[UUID] = {"reachable": False, "ts": 1, "offline": False,
                            "bluetooth": "The lock isn't in Bluetooth range of this computer."}
    views = {v["id"]: v for v in api.locks()}
    b2, other = views[B2], views[UUID]
    assert (b2["state"], b2["via"], b2["wifiOffline"], b2["offline"]) == ("locked", "bluetooth", True, False)
    assert b2["clockFix"] == {"was": -10800, "fixed": True, "detail": ""}
    assert (other["state"], other["wifiOffline"], other["clockFix"]) == ("asleep", False, None)
    assert other["bluetoothNote"] == "The lock isn't in Bluetooth range of this computer."


@_api_in_tmp_home
def test_remote_only_install_never_uses_bluetooth(api):
    import time
    api.sign_in("a@b", "right", False)
    c = api._client
    reported = FakeClient.query_status(c, None)
    c.query_status = lambda lock: {**reported, "battery_percent": 80}
    parts = api.firmware(B2)["install"]["parts"]
    for mode, used in (("remote", False), ("auto", True)):
        api.set_connection(mode)
        api.firmware_install(B2, parts, True)               # this computer can reach it over Bluetooth
        for _ in range(200):
            if not api.firmware_install_status(B2)["running"]:
                break
            time.sleep(0.01)
        assert c.fw_bluetooth is used, mode


@_api_in_tmp_home
def test_bluetooth_chip_install_runs_in_the_background(api):
    import time
    from locklyctl import ble as BLE
    api.sign_in("a@b", "right", False)
    c = api._client
    calls = []

    def update(lock, on_event):
        calls.append(lock["ID"])
        on_event({"stage": "install", "progress": 50, "text": "Sent 1 of 2 blocks"})
        return {"before": "1.14.24", "after": "1.14.28"}
    c.bluetooth_chip_update = update
    real = BLE.available
    BLE.available = lambda: ""
    try:
        reported = FakeClient.query_status(c, None)
        c.query_status = lambda lock: {**reported, "battery_percent": 80}         # back board 1.00.72: behind
        chip = [p for p in api.firmware(B2)["parts"] if p["name"] == "Bluetooth chip"][0]
        assert chip["newer"] and not chip["byBluetooth"]                          # the chip goes last
        c.query_status = lambda lock: {**reported, "sub_version": "1.00.76", "battery_percent": 80}
        chip = [p for p in api.firmware(B2)["parts"] if p["name"] == "Bluetooth chip"][0]
        assert chip["byBluetooth"] and chip["newer"]
        api.bluetooth_chip_install(B2)
        for _ in range(200):
            st = api.firmware_install_status(B2)
            if not st["running"]:
                break
            time.sleep(0.01)
        assert st["ok"] and calls == [B2] and [(p["part"], p["state"], p["now"]) for p in st["parts"]] == [
            ("ble", "done", "1.14.28")]
        BLE.available = lambda: "Bluetooth needs the optional 'bleak' package."
        assert not [p for p in api.firmware(B2)["parts"] if p["name"] == "Bluetooth chip"][0]["byBluetooth"]
        try:
            api.bluetooth_chip_install(B2)
        except ValueError as e:
            assert "bleak" in str(e) and calls == [B2]
        else:
            raise AssertionError("started without Bluetooth")
    finally:
        BLE.available = real


@_api_in_tmp_home
def test_bluetooth_wifi_scan_and_setup(api):
    from locklyctl import ble as BLE
    api.sign_in("a@b", "right", False)
    c = api._client
    c.bluetooth_wifi_scan = lambda lock: [{"ssid": "Net A", "rssi": 62, "router_type": 3},
                                          {"ssid": "Net A", "rssi": 51, "router_type": 3},
                                          {"ssid": "", "rssi": 40, "router_type": 0},
                                          {"ssid": "Guest", "rssi": 70, "router_type": 3}]
    given = []
    c.bluetooth_wifi_setup = lambda lock, ssid, pw, country, on_event: given.append((ssid, pw, country)) or {
        "code": 3, "text": "connected"}
    real = BLE.available
    BLE.available = lambda: ""
    try:
        assert api.bluetooth_wifi_scan(B2) == [{"ssid": "Net A", "rssi": -51, "routerType": 3},
                                              {"ssid": "Guest", "rssi": -70, "routerType": 3}]
        r = api.bluetooth_wifi_setup(B2, "Net A", "pw-1", "ph")
        assert r["ok"] and given == [("Net A", "pw-1", "PH")]
    finally:
        BLE.available = real


@_api_in_tmp_home
def test_bluetooth_check_and_set_time(api):
    from locklyctl import ble as BLE
    api.sign_in("a@b", "right", False)
    c = api._client
    real = BLE.available
    BLE.available = lambda: "Bluetooth needs the optional 'bleak' package."
    try:
        assert api.bluetooth_check(B2) == {"available": False, "inRange": False,
                                           "detail": "Bluetooth needs the optional 'bleak' package."}
    finally:
        BLE.available = real
    BLE.available = lambda: ""                              # as on a computer with bleak, whatever this one has
    try:
        c.bluetooth_in_range = lambda lock: True
        assert api.bluetooth_check(B2)["inRange"] is True
        c.bluetooth_status = lambda lock: c.status_cache.update(
            {lock["ID"]: {"reachable": True, "ts": 1, "is_locked": True, "clock_offset_s": 0}}) or {"is_locked": True}
        v = api.bluetooth_set_time(B2)
        assert (v["clockKnown"], v["clockOffset"], v["state"]) == (True, 0, "locked")
        c.bluetooth_status = lambda lock: None
        try:
            api.bluetooth_set_time(B2)
        except LocklyError as e:
            assert "didn't answer over Bluetooth" in str(e)
        else:
            raise AssertionError("no answer was reported as set")
    finally:
        BLE.available = real

@_api_in_tmp_home
def test_favorites(api):
    api.sign_in("a@b", "right", False)
    c = api._client
    c.warmed = threading.Event()
    assert not any(v["favorite"] for v in api.locks())
    assert api.set_favorite(UUID, True)["favorite"] is True
    assert not c.warmed.is_set()                  # a hub lock doesn't use the device channel
    api.set_favorite(B2, True)
    assert c.warmed.wait(2)                       # a Wi-Fi favorite connects it ahead of time
    assert config.load_config()["favorites"] == [UUID, B2]     # kept in the order they were added
    again = Api(client_factory=FakeClient)        # survives a restart
    again.sign_in("a@b", "right", False)
    again._client.warmed = threading.Event()
    assert {v["id"] for v in again.locks() if v["favorite"]} == {UUID, B2}
    assert api.set_favorite(UUID, False)["favorite"] is False
    assert config.load_config()["favorites"] == [B2]
    c.locks.append({"ID": "cc0011223344556677889900", "na": "5J", "mod": "PGD7Y", "mc": MC})
    for lock_id, err in (("cc0011223344556677889900", ValueError), ("nope", LocklyError)):
        try:
            api.set_favorite(lock_id, True)    # no hub: can't be opened from here at all
        except err:
            continue
        raise AssertionError(lock_id)
    assert config.load_config()["favorites"] == [B2]


@_api_in_tmp_home
def test_welcome_preview_and_send(api):
    api.sign_in("a@b", "right", False)
    client = api._client
    r = api.welcome(UUID, "46", "set", ["mon", "fri"], "08:00", "17:30", False)
    assert r["sent"] is False and r["frame"].startswith("A1B2C3D4") and client.sent == []
    r = api.welcome(UUID, "46", "off", [], "", "", True)
    assert r["sent"] and r["ok"] and client.sent[-1][1] is True      # 0x46 carries the nonce too
    assert api.welcome(UUID, None, "query", [], "", "", False)["form"] == F.WELCOME_46   # PGD7Y → 46
    r = api.welcome(B2, None, "set", ["sat"], "9:00", "12:00", True)
    assert r["form"] == F.WELCOME_9B and client.sent[-1][1] is True
    for bad in ((UUID, "46", "set", ["mon"], "18:00", "08:00"),     # end before start
                (B2, None, "always", [], "", "")):                # 'always' is 0x46-only
        try:
            api.welcome(*bad, False)
        except ValueError:
            continue
        raise AssertionError(bad)


@_api_in_tmp_home
def test_add_guest_validation(api):
    api.sign_in("a@b", "right", False)
    for args in (("", "1234", "24h"), ("Cleaner", "12", "24h"), ("Cleaner", "1234", "forever")):
        try:
            api.add_guest(UUID, *args)
        except ValueError:
            continue
        raise AssertionError(args)


@_api_in_tmp_home
def test_people_merge_across_doors(api):
    from locklyctl.gui.api import describe_access, lock_tz
    api.sign_in("a@b", "right", False)
    r = api.people()
    by = {p["name"]: p for p in r["people"]}
    assert len(by["Owner"]["doors"]) == 2                    # same email, any case → one person
    assert set(by) == {"Owner", "Dana", "Dana R"}            # different names stay apart
    from locklyctl.gui.api import person_access
    door = {"id": "x", "name": "d", "unit": "", "rest": "", "model": ""}
    k1 = person_access({"name": "Sam  Taylor ", "email": ""}, door)["_key"]
    k2 = person_access({"name": "Sam", "lastName": "Taylor", "email": ""}, door)["_key"]
    assert k1 == k2 == "name:sam taylor"
    dana = by["Dana"]["doors"][0]
    assert dana["twoStep"] and dana["faces"] == 1 and dana["role"] == "guest"
    assert not by["Dana R"]["doors"][0]["enabled"]
    # merging is local: Dana R shows under Dana, and can be separated again
    r = api.merge_people([by["Dana R"]["key"]], by["Dana"]["key"])
    by = {p["name"]: p for p in r["people"]}
    assert "Dana R" not in by and len(by["Dana"]["doors"]) == 2 and by["Dana"]["mergedKeys"]
    r = api.separate_person(by["Dana"]["key"])
    assert {p["name"] for p in r["people"]} == {"Owner", "Dana", "Dana R"}
    tz = lock_tz({"tz": "+8:00"})
    assert describe_access({"timeType": "WEEK", "weekData": "31", "startTime": 1745974800000,
                            "endTime": 1746007140000}, tz) == "Mon–Fri 09:00–17:59"
    assert describe_access({"timeType": "WEEK", "weekData": "63", "startTime": 0, "endTime": 0}, tz) == "Mon–Sat"
    assert lock_tz({"tz": "-5:30"}).utcoffset(None).total_seconds() == -19800


@_api_in_tmp_home
def test_editing_access(api):
    api.sign_in("a@b", "right", False)
    c = api._client
    # weekly guest → GUEST/WEEK cloud record, 0x42 on the lock, week+HHMM date field
    api.add_to_door(UUID, "Dana", "2468", {"kind": "weekly", "days": ["mon", "fri"], "start": "09:00", "end": "17:30"})
    kw = c.calls[-1][2]
    assert (kw["role"], kw["time_type"], kw["user_type"], kw["week"]) == ("GUEST", "WEEK", F.UT_GUEST_WEEKLY, 0b10001)
    assert kw["date"] == "110900111E" + "00" * 5
    api.add_to_door(UUID, "Sam", "", {"kind": "always"})
    assert c.calls[-1][2]["role"] == "FAMILY" and c.calls[-1][2]["date"] == "FF" * 10
    api.add_to_door(UUID, "Pat", "1357", {"kind": "dates", "from": "2026-11-01T08:00", "to": "2026-11-03T20:00"})
    kw = c.calls[-1][2]
    assert kw["time_type"] == "MORE_LIMIT" and kw["date"] == "1A0B010800" + "1A0B031400"
    for bad in ({"kind": "weekly", "days": [], "start": "09:00", "end": "10:00"},
                {"kind": "dates", "from": "2026-11-03T08:00", "to": "2026-11-01T08:00"}, {"kind": "?"}):
        try:
            api.add_to_door(UUID, "X", "1234", bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    # PIN change uses the slot and role byte the *lock* reports, not the cloud's
    api.change_pin(UUID, 3, "8642")
    assert c.calls[-1][:4] == ("pin", 4, "8642", 0x42)
    # the owner can't be changed or removed here
    for fn in (lambda: api.change_pin(UUID, 0, "1111"), lambda: api.remove_from_door(UUID, 0)):
        try:
            fn()
        except ValueError:
            continue
        raise AssertionError("owner edited")
    api.remove_from_door(UUID, 3)
    assert c.calls[-1] == ("delete", 3, 4, 0x42)
    # a cloud-only draft (not in the lock's table) is removed from the cloud only
    c.lock_user_table = lambda lock: []
    api.remove_from_door(B2, 5)
    assert c.calls[-1][:2] == ("rest", "acu/delete")


def test_not_signed_in():
    try:
        Api(client_factory=FakeClient).locks()
    except LocklyError:
        return
    raise AssertionError("worked without signing in")
