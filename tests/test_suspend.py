"""Suspend/resume for access users: expire the window, keep PIN/finger/face,
restore the exact saved schedule. Unit level, no network or lock."""
import pytest

from locklyctl import config, features as F, commands
from locklyctl.commands import UsageError
from locklyctl.transport import LocklyClient, LocklyError

LOCK = {"ID": "aa00112233445566778899bb", "mc": "12345678", "mod": "PGK728WRHK",
        "tz": "+8:00", "na": "Test Door"}
# A fake user record as _read_access_user would return it (weekly, with a face).
REC = {"user_type": 0x42, "date": "7F000018000000000000", "pwd_id": 8, "pin": "4455",
       "multi_verify": 0, "faces": 1, "fingerprints": 0, "cards": 0}


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("tester@example.test")
    c._read_access_user = lambda lock, user_id: dict(REC)
    c._user_counts = lambda lock, user_id: {k: REC[k] for k in ("faces", "fingerprints", "cards")}
    pushes = []
    c._push_access_user = lambda lock, **kw: pushes.append(kw)
    return c, pushes


def test_suspended_window_bytes():
    # A future (pending) window, not a past one: the lock rejects a year-2000
    # window with 0x73 error E3 (proven live), while a future window denies and
    # is accepted even when the lock's clock runs slow.
    assert F.access_date_suspended() == "63010100006301010001"
    # a period in 2099: 2099-01-01 00:00 .. 00:01 (decimal yyMMddHHmm, hex-packed)
    assert F.access_date_suspended() == F.access_date_period_raw("9901010000", "9901010001")


def test_config_stash_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    assert config.get_suspended("dev", 7) is None
    config.stash_suspended("dev", 7, {"user_type": 0x42, "date": "7F000018000000000000"})
    assert config.get_suspended("dev", 7) == {"user_type": 0x42, "date": "7F000018000000000000"}
    popped = config.pop_suspended("dev", 7)
    assert popped == {"user_type": 0x42, "date": "7F000018000000000000"}
    assert config.get_suspended("dev", 7) is None


def test_suspend_expires_window_keeps_pin_and_counts(monkeypatch, tmp_path):
    c, pushes = _client(monkeypatch, tmp_path)
    r = c.suspend_access_user(LOCK, 31)
    assert len(pushes) == 1
    p = pushes[0]
    assert p["user_type"] == F.UT_GUEST_PERIOD        # 0x12
    assert p["date"] == F.access_date_suspended()
    assert p["pwd_id"] == 8
    assert p["pin"] == "4455"                          # PIN passed through, not emptied
    assert p["multi_verify"] == 0
    # the original schedule is stashed for an exact resume
    assert config.get_suspended(LOCK["ID"], 31) == {"user_type": 0x42, "date": "7F000018000000000000"}
    # before == after means no credential was dropped
    assert r["before"] == r["after"] == {"faces": 1, "fingerprints": 0, "cards": 0}


def test_resume_restores_exact_schedule_from_stash(monkeypatch, tmp_path):
    c, pushes = _client(monkeypatch, tmp_path)
    config.stash_suspended(LOCK["ID"], 31, {"user_type": 0x42, "date": "7F000018000000000000"})
    r = c.resume_access_user(LOCK, 31)
    assert r["source"] == "stash"
    p = pushes[0]
    assert p["user_type"] == 0x42
    assert p["date"] == "7F000018000000000000"         # exact bytes
    assert p["pin"] == "4455"
    assert config.get_suspended(LOCK["ID"], 31) is None  # stash cleared


def test_resume_cloud_fallback(monkeypatch, tmp_path):
    c, pushes = _client(monkeypatch, tmp_path)
    c._resume_from_cloud = lambda lock, user_id: {"user_type": 0x42, "date": "7F000018000000000000"}
    r = c.resume_access_user(LOCK, 31)                 # no stash present
    assert r["source"] == "cloud"
    assert pushes[0]["date"] == "7F000018000000000000"


def test_resume_without_stash_or_cloud_raises(monkeypatch, tmp_path):
    c, _ = _client(monkeypatch, tmp_path)
    c._resume_from_cloud = lambda lock, user_id: None
    with pytest.raises(Exception):
        c.resume_access_user(LOCK, 31)


def test_dry_run_sends_nothing(monkeypatch, tmp_path):
    c, pushes = _client(monkeypatch, tmp_path)
    s = c.suspend_access_user(LOCK, 31, dry_run=True)
    assert s["dry_run"] and s["frame"] and s["stash"] == {"user_type": 0x42, "date": "7F000018000000000000"}
    config.stash_suspended(LOCK["ID"], 31, {"user_type": 0x42, "date": "7F000018000000000000"})
    r = c.resume_access_user(LOCK, 31, dry_run=True)
    assert r["dry_run"] and r["frame"] and r["source"] == "stash"
    assert pushes == []                                # nothing pushed
    assert config.get_suspended(LOCK["ID"], 31) is not None  # dry run did not clear the stash


def test_resume_from_cloud_weekly_and_period(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("tester@example.test")
    c.list_users = lambda lock: [
        {"userAcuId": 31, "timeType": "WEEK", "weekData": "127", "type": "GUEST"},
        {"userAcuId": 32, "timeType": "MORE_LIMIT", "type": "GUEST",
         "startTime": 1766592000000, "endTime": 1766764800000}]
    weekly = c._resume_from_cloud(LOCK, 31)
    assert weekly["user_type"] == F.UT_GUEST_WEEKLY
    assert weekly["date"] == F.access_date_weekly(127, (0, 0), (0, 0))
    period = c._resume_from_cloud(LOCK, 32)
    assert period["user_type"] == F.UT_GUEST_PERIOD and len(period["date"]) == 20
    assert c._resume_from_cloud(LOCK, 999) is None


def test_resolve_user_by_name_and_id(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("tester@example.test")
    c.list_users = lambda lock: [
        {"userAcuId": 31, "name": "Alpha"}, {"userAcuId": 32, "name": "Beta"},
        {"userAcuId": 33, "name": "Beta"}]
    assert commands._resolve_user(c, LOCK, "31") == 31          # numeric, no cloud call needed
    assert commands._resolve_user(c, LOCK, "alpha") == 31       # case-insensitive name
    with pytest.raises(UsageError):
        commands._resolve_user(c, LOCK, "Beta")                 # ambiguous
    with pytest.raises(UsageError):
        commands._resolve_user(c, LOCK, "Nobody")               # missing


def test_a_second_suspend_is_refused(monkeypatch, tmp_path):
    # it would save the far-future window as the person's schedule, and resume would restore that
    c, pushes = _client(monkeypatch, tmp_path)
    c.suspend_access_user(LOCK, 31)
    with pytest.raises(LocklyError, match="already suspended"):
        c.suspend_access_user(LOCK, 31)
    assert len(pushes) == 1
    assert config.get_suspended(LOCK["ID"], 31) == {"user_type": 0x42, "date": "7F000018000000000000"}
