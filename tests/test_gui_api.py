"""GUI backend tests — the Api class with a fake client; no window, no network."""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl import config
from locklyctl import features as F
from locklyctl.gui.api import Api, split_unit
from locklyctl.transport import LocklyError

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
def test_favorites(api):
    api.sign_in("a@b", "right", False)
    c = api._client
    c.warmed = threading.Event()
    assert not any(v["favorite"] for v in api.locks())
    assert api.set_favorite(UUID, True)["favorite"] is True
    assert not c.warmed.is_set()                  # a hub lock doesn't use the device channel
    api.set_favorite(B2, True)
    assert c.warmed.wait(2)                       # a Wi-Fi favourite connects it ahead of time
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
