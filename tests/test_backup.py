"""Backups (locklyctl/backup.py): the sealed format shared with locklyctl-mqtt,
taking one from Lockly (people read from the locks themselves) or from a
server, restoring into a server, and the backup / restore commands."""
import argparse
import base64
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

from locklyctl import backup as B
from locklyctl import commands as CMD
from locklyctl import config
from locklyctl import features as F
from locklyctl import view as V
from locklyctl.transport import LocklyClient, LocklyError

PASS = "correct horse battery staple"

# locklyctl-mqtt's internal/backup test document sealed with PASS, salt 00..0f,
# iv 10..1f and n = 2^10. Its Go tests pin the same bytes: the two
# implementations seal the same document to exactly this, and each opens the
# other's. A change here is a change to the shared format (its CONTRACT.md 4).
GOLDEN = """{
  "format": "locklyctl-backup",
  "version": 1,
  "encrypted": {
    "kdf": "scrypt",
    "n": 1024,
    "r": 8,
    "p": 1,
    "salt": "AAECAwQFBgcICQoLDA0ODw==",
    "cipher": "aes-256-ctr+hmac-sha256",
    "iv": "EBESExQVFhcYGRobHB0eHw==",
    "mac": "fgQrDSimTlWEaTIk3A2trr6TDKQCe8E9d/ddoppo/es=",
    "data": "Uny4v/6rNa3nGKHm6mlgOXyqxdSg0hLExBHGbOqsg0l+7350PH2077m6AQGIZXyLHiOrm8dWLGnga7Anm1eMmOmiKO1qSTnaKRJtTFptb7ixLDVsdBIgH9oTSt+RpFlbNKLCf6NvrVn0JTHSvo3e/SWr4nC9kQP3X7ACpzdb3GPvBmEMdN+OpNXPSds1Mr4CpXAupHxiVrk56vE6yr5wNMsihSlyWq6UhOrNquRul7Uqq5G998Xw2oURi6TpsWwgZCkcCa5WUofqWDZZOcomnhuclTfQUAf5wdfOow/uTqbOlqZxHTwQvIiauKjhyaX93n2su/XU/aquOZXcFOLn9u4JMQV2T/eyILk5VPM2O2tHu5U9m2tBZFf/rQxjq84GvX8S3ssdAyFhUoRLTUqnOT06CinZb6R15fleSvWSk42WoTlWI6gML/iHxbV/ccRsjkjKC1+jMJPdAV3zhBgMEAw0QrXAK+PHPrfwJIKgU5ULwogtviqBWAVrYBzzQSp0dUjv21+yPLH7yczOGOD1eKntydX+a8Y5bIxDyJYbM7AKzfRGJH1igHSHw7kbcseBqC76/JwiwcW0nXW1n3d9Svva+Hn1yvvVEPRYJDhuoioU9zumGE171c1A4whcpOVMnOnb722XN2hDZMhx/M3T0kV97uP+ZVM6pLQhToKpLt9iDgPeDfUlihqyYlYR6poghnO76fnmjIIMX1oX8Fvb9CHRXBce/oPhzzFhNYIRqsOJRz3yaWhMZt7/svhg3RNi3l4vu4n8jeCFbaGkC4MdQRjx2kqrvzC7acbAQB5NjESo+a1O1vnQdvhWbXKkB43Pk97VWV6xMl15i69PC3JiMfWAi3sXejY6gtdNaIJDbNhoaOeGXcaaumQNvqY5ZAZfNKntLDS58VqkMvck71WrFoYoh3hv5KHwryKtu0LTAL81pyYFpN0zAbbrqYingtnrB4xzQx1F4Z5hz4DHA08dv7kmctGig1FfOkbBzIcykACjtUxjV6Up2w1gGSBO77kSiGZ0/LfQyrVpoezZs8ddkKjbZcv/4j4bIyffSXrc0VBmodY="
  }
}"""


def _fixed_random():
    chunks = iter([bytes(range(16)), bytes(range(16, 32))])
    return lambda k: next(chunks)


def _doc():
    return B.open_backup(GOLDEN, PASS)


# ── the format ─────────────────────────────────────────────────────────────

def test_golden_vector_shared_with_locklyctl_mqtt():
    doc = _doc()
    front = doc["locks"][0]
    assert (front["deviceId"], front["mc"], front["name"], front["hidden"]) == \
        ("aa00000000000000000000b1", "12345678", "Front", True)
    assert front["mqtt"] == {"clientId": "cid-1", "username": "u1", "password": "p1"}
    guest, away = front["people"]
    assert (guest["pin"], guest["date"], guest["enabled"], guest["multiVerify"]) == \
        ("246800", "1F090011000000000000", True, 1)
    assert away["enabled"] is False and away["dateMode"] == "period"
    assert B.seal(doc, PASS, n=1 << 10, random=_fixed_random()).decode() == GOLDEN


def test_seal_and_open():
    doc = _doc()
    sealed = B.seal(doc, "another passphrase")              # fresh randomness, the full work factor
    assert B.is_encrypted(sealed) and not B.is_encrypted(B.plain(doc)) and not B.is_encrypted(b"not json")
    assert json.loads(sealed)["encrypted"]["n"] == B.SEAL_N
    for secret in (b"246800", b"135790", b"12345678", b"Weekday guest"):
        assert secret not in sealed
    assert B.open_backup(sealed, "another passphrase") == doc
    assert B.open_backup(B.plain(doc)) == doc
    with pytest.raises(B.PassphraseNeeded):
        B.open_backup(sealed)
    with pytest.raises(B.WrongPassphrase):
        B.open_backup(sealed, "wrong")
    with pytest.raises(B.BackupError):
        B.seal(doc, "")


def _edit(sealed, fn):
    outer = json.loads(sealed)
    fn(outer["encrypted"])
    return json.dumps(outer)


def _flip(b64):
    b = bytearray(base64.b64decode(b64))
    b[len(b) // 2] ^= 1
    return base64.b64encode(bytes(b)).decode()


def test_tampering_is_detected():
    edits = {
        "ciphertext": lambda e: e.update(data=_flip(e["data"])),
        "salt": lambda e: e.update(salt=_flip(e["salt"])),
        "iv": lambda e: e.update(iv=_flip(e["iv"])),
        "work n": lambda e: e.update(n=1 << 11),
        "mac": lambda e: e.update(mac=_flip(e["mac"])),
    }
    for name, edit in edits.items():
        with pytest.raises(B.WrongPassphrase):
            B.open_backup(_edit(GOLDEN, edit), PASS)


def test_bad_parameters_are_refused_before_any_key_work():
    edits = {
        "n too big": lambda e: e.update(n=1 << 21),
        "n not a power of two": lambda e: e.update(n=1000),
        "r too big": lambda e: e.update(r=64),
        "1 GiB of scrypt memory": lambda e: e.update(n=1 << 20, r=8),
        "other cipher": lambda e: e.update(cipher="aes-256-gcm"),
        "short salt": lambda e: e.update(salt=base64.b64encode(b"short").decode()),
        "not base64": lambda e: e.update(iv="%%%"),
        "n not a number": lambda e: e.update(n="1024"),
    }
    for name, edit in edits.items():
        with pytest.raises(B.BackupError) as err:
            B.open_backup(_edit(GOLDEN, edit), PASS)
        assert not isinstance(err.value, B.WrongPassphrase), name


def test_header_checks():
    for data in ("not json", '{"format":"something-else","version":1,"locks":[]}', "[]"):
        with pytest.raises(B.BackupError):
            B.open_backup(data)
    with pytest.raises(B.BackupError, match="version 2"):
        B.open_backup('{"format":"locklyctl-backup","version":2,"locks":[]}')


# ── taking a backup from Lockly ─────────────────────────────────────────────

def _le(v):
    return f"{v & 0xFF:02X}{v >> 8 & 0xFF:02X}"


def _detail(pwd, uid, user_type, date):
    """A 0A76 payload as features.parse_access_user_detail reads it."""
    return "00" + "00" + _le(pwd) + _le(uid) + _le(0) + "00" + f"{user_type:02X}" + date + "00" * 4


FRONT, PATIO, SPARE, NOKEY = (f"aa{i:022x}" for i in (0xa1, 0xb2, 0xc3, 0xd4))
WEEKDAYS_9_TO_5 = "1F090011000000000000"
NOV_TO_JAN = "1A0B0E091E1B01021205"      # 2026-11-14 09:30 to 2027-01-02 18:05


class FakeLockly:
    """Signed in to Lockly: a Visage with three people (one suspended with its
    schedule saved, one without), a hub lock, a Visage that won't answer and
    one with no master code."""
    server = None

    def __init__(self):
        self.email = "owner@example.test"
        self.locks = [
            {"ID": FRONT, "na": "Front", "mc": "111", "mod": "PGK728WRHK", "tz": "+8:00", "blename": "PGK728WRHK01"},
            {"ID": PATIO, "na": "Patio", "mc": "222", "mod": "PGD7Y", "hubid": "PGH200X"},
            {"ID": SPARE, "na": "Spare", "mc": "333", "mod": "PGK728WRHK", "tz": "+8:00"},
            {"ID": NOKEY, "na": "No key", "mc": "", "mod": "PGK728WRHK"},
        ]
        self.table = [
            {"pwd_id": 3, "user_id": 9, "pin": "246800", "multi_verify": 1},
            {"pwd_id": 4, "user_id": 10, "pin": "135790", "multi_verify": 0},
            {"pwd_id": 5, "user_id": 11, "pin": "", "multi_verify": 1},
        ]
        suspended = F.access_date_suspended()
        self.details = {9: _detail(3, 9, 0x42, WEEKDAYS_9_TO_5), 10: _detail(4, 10, 0x12, suspended),
                        11: _detail(5, 11, 0x12, suspended)}
        self.read = []

    def list_users(self, lock):
        return [{"userAcuId": 9, "name": "Sam", "lastName": "Taylor"}, {"userAcuId": 10, "name": "Dana"}]

    def lock_user_table(self, lock):
        if lock["ID"] == SPARE:
            raise LocklyError("the lock didn't answer")
        return self.table

    def exchange_built(self, lock, build):
        uid = int(build("00").split(":")[1])                 # the stand-in builder names the user
        self.read.append((lock["ID"], uid))
        return "ACK", self.details[uid]


@pytest.fixture
def lockly(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    monkeypatch.setattr(F, "build_access_user_detail", lambda lock, nonce, *, user_id, admin_id=0: f"detail:{user_id}")
    c = FakeLockly()
    config.stash_suspended(FRONT, 10, {"user_type": 0x12, "date": NOV_TO_JAN})   # what `suspend` saved
    V.set_hidden(c, c.locks[1], True)
    return c


def test_from_lockly_reads_people_from_the_locks(lockly):
    doc, notes = B.from_lockly(lockly)
    assert doc["source"] == {"kind": "lockly", "account": "owner@example.test"}
    assert [lk["deviceId"] for lk in doc["locks"]] == [FRONT, PATIO, SPARE]
    front, patio, spare = doc["locks"]
    assert (front["mc"], front["name"], front["tz"], front["sortIndex"], front["hidden"]) == ("111", "Front", "+8:00", 1, False)
    assert patio["hidden"] is True and patio["people"] == [] and spare["people"] == []
    sam, dana, eleven = front["people"]
    assert sam == {"name": "Sam Taylor", "pwdId": 3, "userId": 9, "adminId": 0, "pin": "246800", "userType": 0x42,
                   "date": WEEKDAYS_9_TO_5, "multiVerify": 1, "enabled": True, "dateMode": "weekly", "weekMask": 0x1F}
    # suspended by `suspend`: the saved schedule comes back, and it stays off
    start = int(datetime(2026, 11, 14, 9, 30, tzinfo=timezone(timedelta(hours=8))).timestamp())
    assert (dana["enabled"], dana["date"], dana["userType"], dana["dateMode"], dana["startAt"]) == \
        (False, NOV_TO_JAN, 0x12, "period", start)
    assert dana["multiVerify"] == 0                          # kept exactly
    # suspended with nothing saved: stays suspended (never widened)
    assert (eleven["name"], eleven["enabled"], eleven["date"]) == ("User 11", False, F.access_date_suspended())
    left_out = [text for missing, text in notes if missing]
    assert len(left_out) == 2 and any("Spare" in t and "couldn't be read" in t for t in left_out) \
        and any("No key" in t for t in left_out)
    assert any("Patio" in t and "the lock only" in t for missing, t in notes if not missing)
    assert any("user 11 is suspended" in t for _, t in notes)
    assert {uid for _, uid in lockly.read} == {9, 10, 11}
    assert B.open_backup(B.seal(doc, PASS, n=1 << 10), PASS) == doc      # and it seals


# ── a private server ───────────────────────────────────────────────────────

def _server_client(monkeypatch, tmp_path, reply):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("owner@example.test", server="https://srv.example")
    calls = []
    c._post = lambda endpoint, body, **kw: calls.append((endpoint, body)) or reply(endpoint)
    return c, calls


def _imported(endpoint, conflicts=()):
    if endpoint == "backup/export":
        return {"cod": "200", "backup": _doc()}
    return {"cod": "200", "locksAdded": 2, "locksUpdated": 0, "locksRemoved": 0, "peopleAdded": 2,
            "peopleUpdated": 0, "peopleRemoved": 0, "conflicts": list(conflicts)}


def test_server_backup_and_restore(monkeypatch, tmp_path):
    c, calls = _server_client(monkeypatch, tmp_path, _imported)
    assert B.from_server(c) == _doc()
    r = B.restore_to_server(c, _doc(), replace=True)
    assert calls[-1] == ("backup/import", {"backup": _doc(), "replace": True})
    assert r["locksAdded"] == 2 and r["conflicts"] == []
    c._post = lambda *a, **k: {"cod": "909", "msg": "backup version 2 isn't supported"}
    with pytest.raises(LocklyError, match="version 2"):
        B.restore_to_server(c, _doc(), replace=False)
    with pytest.raises(B.BackupError, match="--server"):
        B.restore_to_server(LocklyClient("owner@example.test"), _doc(), replace=False)


# ── the commands ───────────────────────────────────────────────────────────

def _pass_file(tmp_path, text=PASS):
    p = tmp_path / "passphrase"
    p.write_text(text + "\n")
    return str(p)


def test_backup_command_writes_a_sealed_owner_only_file(monkeypatch, tmp_path, capsys):
    c, _ = _server_client(monkeypatch, tmp_path, _imported)
    out = tmp_path / "backup.json"
    args = argparse.Namespace(file=str(out), no_encrypt=False, passphrase_file=_pass_file(tmp_path), yes=False)
    assert CMD._backup(c, args) == CMD.EXIT_OK
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    assert B.is_encrypted(out.read_bytes()) and B.open_backup(out.read_bytes(), PASS) == _doc()
    with pytest.raises(CMD.UsageError, match="already exists"):           # never overwrites
        CMD._backup(c, args)
    plain_out = tmp_path / "plain.json"
    CMD._backup(c, argparse.Namespace(file=str(plain_out), no_encrypt=True, passphrase_file=None, yes=False))
    assert "isn't encrypted" in capsys.readouterr().out and not B.is_encrypted(plain_out.read_bytes())


def test_backup_command_from_lockly(lockly, tmp_path, capsys):
    out = tmp_path / "lockly.json"
    args = argparse.Namespace(file=str(out), no_encrypt=False, passphrase_file=_pass_file(tmp_path), yes=False)
    with pytest.raises(CMD.UsageError, match="--yes"):                    # wakes locks: asks, or needs --yes
        CMD._backup(lockly, args)
    assert not out.exists() and lockly.read == []
    args.yes = True
    assert CMD._backup(lockly, args) == CMD.EXIT_FAIL                     # a Visage's people weren't read
    written = capsys.readouterr().out
    assert "3 lock(s) and 3 person(s)" in written and "couldn't be read" in written
    assert B.open_backup(out.read_bytes(), PASS)["source"]["kind"] == "lockly"


def test_restore_command(monkeypatch, tmp_path, capsys):
    c, calls = _server_client(monkeypatch, tmp_path, lambda ep: _imported(ep, conflicts=[FRONT]))
    sealed = tmp_path / "golden.json"
    sealed.write_text(GOLDEN)
    args = argparse.Namespace(file=str(sealed), replace=False, passphrase_file=_pass_file(tmp_path), yes=False)
    assert CMD._restore(c, args) == CMD.EXIT_FAIL                         # one lock was another account's
    out = capsys.readouterr().out
    assert "skipped lock " + FRONT in out and "Nothing was sent to the locks" in out
    assert calls[-1][1]["replace"] is False
    with pytest.raises(CMD.UsageError, match="--yes"):
        CMD._restore(c, argparse.Namespace(**{**vars(args), "replace": True}))
    with pytest.raises(CMD.UsageError, match="--server"):
        CMD._restore(LocklyClient("owner@example.test"), args)
    with pytest.raises(B.WrongPassphrase):
        CMD._restore(c, argparse.Namespace(**{**vars(args), "passphrase_file": _pass_file(tmp_path, "nope")}))
