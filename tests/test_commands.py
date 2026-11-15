"""The command line and the terminal menu in the app's groups: the grouped
--help, the keep / activity / people commands, the welcome command, and the
one way a person is removed. Stand-in client, no network, no keyring."""
import argparse
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from locklyctl import cli, commands, config, oac
from locklyctl import protocol as P
from locklyctl.transport import LocklyClient, LocklyError
from test_gui_api import B2, MC, UUID, FakeClient


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_keyring", lambda: None)
    return tmp_path


def _args(**kw) -> argparse.Namespace:
    return argparse.Namespace(**kw)


def _plain(frame: str) -> str:
    return P.AES.new(P.derive_aes_key(MC, B2), P.AES.MODE_ECB).decrypt(bytes.fromhex(frame)[6:-2]).hex().upper()


# ── --help, by group ────────────────────────────────────────────────────────

def test_every_command_is_in_one_group_and_has_a_handler(capsys):
    parser = argparse.ArgumentParser(prog="locklyctl", epilog="Commands sign in with the saved login.")
    commands.add_subcommands(parser)
    names = [n for _, group in commands.GROUPS for n in group]
    assert len(names) == len(set(names)) == 22
    assert set(names) - {"gui"} == set(commands._HANDLERS)          # gui is opened by cli.main itself
    text = parser.format_help()
    for title, group in commands.GROUPS:
        assert f"\n{title}:\n" in text
        for name in group:
            assert f"\n  {name} " in text, name
    # every day first, the locks' own upkeep after it
    assert text.index("Doors:") < text.index("People:") < text.index("Maintenance:") < text.index("This computer:")
    assert text.rstrip().endswith("Commands sign in with the saved login.")
    with pytest.raises(SystemExit):
        parser.parse_args(["keep", "--help"])
    assert "Keep a Visage or Vision unlocked" in capsys.readouterr().out


def test_a_command_in_no_group_is_caught():
    with pytest.raises(AssertionError, match="no group"):
        commands.grouped_help({"list": "x"}, 80)


# ── keep ────────────────────────────────────────────────────────────────────

def test_keep_opens_a_door_so_it_asks(monkeypatch, capsys):
    c = FakeClient("a@b")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(commands.UsageError, match="--yes"):
        commands._keep(c, _args(lock="1204", hours=2.0, end=False, yes=False))
    assert c.sent == []
    for bad in (dict(hours=None, end=False), dict(hours=2.0, end=True)):
        with pytest.raises(commands.UsageError):
            commands._keep(c, _args(lock="1204", yes=True, **bad))
    with pytest.raises(commands.UsageError, match="8-slot"):         # a PGD7Y has no one-time slots
        commands._keep(c, _args(lock="2A Office", hours=1.0, end=False, yes=True))
    assert commands._keep(c, _args(lock="1204", hours=1.5, end=False, yes=True)) == 0
    assert "unlocked until" in capsys.readouterr().out and len(c.sent) == 1
    rec = config.load_config()["keep_unlocked"][B2]
    assert len(rec["slots"]) in (1, 2)                               # one slot, or two past the lock's midnight
    # ending it takes its slots off the lock, then locks
    assert commands._keep(c, _args(lock="1204", hours=None, end=True, yes=False)) == 0
    assert B2 not in config.load_config()["keep_unlocked"] and "locked" in capsys.readouterr().out


# ── activity, people ────────────────────────────────────────────────────────

def test_activity(capsys):
    c = FakeClient("a@b")
    assert commands._activity(c, _args(lock="2A Office", json=False)) == 0
    out = capsys.readouterr().out
    assert "Dana" in out and len(out.strip().splitlines()) == 3
    assert commands._activity(c, _args(lock="2A Office", json=True)) == 0
    events = json.loads(capsys.readouterr().out)
    assert [e["who"] for e in events] == ["", "", "Dana"] and set(events[0]) == {"time", "what", "who"}


def test_people_never_prints_a_pin(capsys):
    c = FakeClient("a@b")
    assert commands._people(c, _args(lock="2A Office", json=False)) == 0
    out = capsys.readouterr().out
    assert "Owner" in out and "Dana" in out and "1234" not in out    # the owner's PIN is in the stand-in's record
    assert commands._people(c, _args(lock="2A Office", json=True)) == 0
    raw = capsys.readouterr().out
    rows = json.loads(raw)
    assert "1234" not in raw and all("pin" not in r for r in rows)
    assert rows[0]["person"] == "Owner" and rows[0]["hasPin"] is True and rows[1]["faces"] == 1
    # with no lock named: everyone, door by door
    assert commands._people(c, _args(lock=None, json=True)) == 0
    raw = capsys.readouterr().out
    people = json.loads(raw)
    assert "1234" not in raw and {p["name"] for p in people} == {"Owner", "Dana", "Dana R"}
    assert commands._people(c, _args(lock=None, json=False)) == 0
    assert "1234" not in capsys.readouterr().out
    config.stash_suspended(B2, 5, {"user_type": 0x12, "date": "00" * 10})
    commands._people(c, _args(lock="1204", json=False))
    assert "paused" in capsys.readouterr().out


# ── offline codes ───────────────────────────────────────────────────────────

def _oac_args(**kw):
    return _args(**{**dict(lock="1204", name=None, list=False, anyway=False, json=False), **kw})


def _first_code():
    from test_oac import VECTORS
    return VECTORS[1]["codes"].split(",")[0]          # FakeLockly's seed, at its position


def test_an_offline_code_from_the_command_line(capsys):
    c = FakeClient("a@b")
    assert commands._offline_code(c, _oac_args(name="Sam")) == 0
    out = capsys.readouterr().out
    assert oac.shown(_first_code()) in out and "for Sam at 1204, one use" in out
    assert "1. Double-tap the Lockly logo" in out and "can't be deleted" in out
    # past the app's daily count it stops and says how to go on
    for _ in range(oac.MANY_A_DAY - 1):
        commands._offline_code(c, _oac_args(name="Sam"))
    capsys.readouterr()
    assert commands._offline_code(c, _oac_args(name="Sam")) == 1
    assert "--anyway" in capsys.readouterr().err
    assert commands._offline_code(c, _oac_args(name="Sam", anyway=True)) == 0
    # a code that works, but whose position Lockly didn't save, is shown and said, and the exit says so
    c.oac_server.answers["oac/usage/save"] = {"cod": "910"}
    assert commands._offline_code(c, _oac_args(name="Sam", anyway=True)) == 1
    captured = capsys.readouterr()
    assert "the code works" in captured.err and "one use" in captured.out


def test_the_offline_code_list_never_prints_a_code(capsys):
    c = FakeClient("a@b")
    c._rest_ok = lambda endpoint, req: {"cod": "200", "obj": [
        {"id": 1, "acctname": "Sam", "pwType": "OFFONE", "password": "4071"}]}
    assert commands._offline_code(c, _oac_args(list=True)) == 0
    out = capsys.readouterr().out
    assert "Sam" in out and "one use" in out and "4071" not in out and "40 71" not in out
    assert commands._offline_code(c, _oac_args(list=True, json=True)) == 0
    raw = capsys.readouterr().out
    assert json.loads(raw) == [{"name": "Sam", "once": True}] and "4071" not in raw


def test_offline_code_usage_errors():
    c = FakeClient("a@b")
    for kw in ({}, {"name": "Sam", "list": True}):
        with pytest.raises(commands.UsageError, match="--name NAME"):
            commands._offline_code(c, _oac_args(**kw))
    for kw in ({"name": "Sam"}, {"list": True}):           # a Touch: no offline codes
        with pytest.raises(commands.UsageError, match="Visage or Vision"):
            commands._offline_code(c, _oac_args(lock="2A Office", **kw))
    assert c.oac_server is None                              # Lockly wasn't asked


# ── welcome ─────────────────────────────────────────────────────────────────

def _welcome_args(**kw):
    base = dict(lock="1204", action="set", days="sat", start="09:00", end="12:00", form=None, send=False, entry="first")
    return _args(**{**base, **kw})


def test_the_welcome_command_runs(capsys):
    # It raised AttributeError (commands called cli.welcome_plan, which is features.welcome_plan)
    # from the day welcome_plan moved, with no test on the command.
    c = FakeClient("a@b")
    assert commands._welcome(c, _welcome_args()) == 0
    assert "[dry-run] Welcome Mode schedule: A1B2C3D4" in capsys.readouterr().out and c.sent == []
    assert commands._welcome(c, _welcome_args(send=True)) == 0
    assert "0301" + "08" + "2009000C00" + "00FFFFFFFF" * 7 + "00" + "01" in _plain(c.sent[-1][0])    # first entry
    assert commands._welcome(c, _welcome_args(send=True, entry="auto")) == 0
    assert "0301" + "08" + "2009000C00" + "00FFFFFFFF" * 7 + "01" + "01" in _plain(c.sent[-1][0])    # by itself
    assert commands._welcome(c, _welcome_args(action="query")) == 0
    with pytest.raises(commands.UsageError):
        commands._welcome(c, _welcome_args(action="always"))          # single-slot locks only
    with pytest.raises(commands.UsageError):
        commands._welcome(c, _welcome_args(start="12:00", end="09:00"))


def test_the_welcome_command_keeps_a_running_window(capsys):
    c = FakeClient("a@b")
    commands._keep(c, _args(lock="1204", hours=1.0, end=False, yes=True))
    week, bh, bm, eh, em = config.load_config()["keep_unlocked"][B2]["slots"][0]
    slot = f"{week:02X}{bh:02X}{bm:02X}{eh:02X}{em:02X}"
    c.welcome_table = "00040108" + "8007001500" + slot + "00FFFFFFFF" * 6 + "02" + "02"     # as the lock now has it
    assert commands._welcome(c, _welcome_args(send=True)) == 0
    assert "0301" + "08" + "2009000C00" + slot + "00FFFFFFFF" * 6 + "02" + "03" in _plain(c.sent[-1][0])


# ── removing a person: one way, for every surface ───────────────────────────

class Client(LocklyClient):
    """The real remove_user over recorded calls."""
    def __init__(self, table, users):
        super().__init__("tester@example.test")
        self.calls, self._table, self._users, self.cloud_ok = [], table, users, True

    def list_users(self, lock): return self._users
    def lock_user_table(self, lock): self.calls.append("table"); return self._table
    def delete_access_user(self, lock, **kw): self.calls.append(("lock+cloud", kw))
    def _rest_ok(self, endpoint, req): self.calls.append((endpoint, req)); return {}
    def delete_guest(self, lock, user_acu_id): self.calls.append(("cloud", user_acu_id)); return self.cloud_ok


VISAGE = {"ID": B2, "mod": "PGK728WRHK", "mc": MC}
TOUCH = {"ID": UUID, "mod": "PGD7Y", "mc": MC}
USERS = [{"userAcuId": 0, "type": "OWNER"}, {"userAcuId": 7, "type": "GUEST"}]


def test_remove_user_goes_to_the_lock_first_where_it_keeps_people():
    c = Client([{"pwd_id": 4, "user_id": 7, "user_type": 0x42}], USERS)
    assert c.remove_user(VISAGE, 7) == "lock"
    assert c.calls == ["table", ("lock+cloud", {"user_acu_id": 7, "pwd_id": 4, "user_type": 0x42})]
    # someone the lock never got (a draft in Lockly's list) is removed there
    c = Client([], USERS)
    assert c.remove_user(VISAGE, 7) == "cloud"
    assert c.calls == ["table", ("acu/delete", {"adminAcuId": 0, "dv": B2, "userAcuId": 7})]
    # a lock that keeps nobody on itself only has Lockly's record, and isn't woken
    c = Client([], USERS)
    assert c.remove_user(TOUCH, 7) == "cloud" and c.calls == [("cloud", 7)]
    c.cloud_ok = False
    with pytest.raises(LocklyError, match="didn't delete"):
        c.remove_user(TOUCH, 7)
    # the owner is never removed, on either kind
    for lock in (VISAGE, TOUCH):
        c = Client([{"pwd_id": 0, "user_id": 0, "user_type": 0x21}], USERS)
        with pytest.raises(LocklyError, match="owner"):
            c.remove_user(lock, 0)
        assert c.calls == []


# ── the terminal menu ───────────────────────────────────────────────────────

class MenuClient(FakeClient):
    def list_guests(self, lock):
        return self.list_users(lock)


def _drive(monkeypatch, choices, answers=(), confirms=()):
    """Run the menus with scripted choices; returns the menus that were shown, as (title, option keys)."""
    seen, choices, answers, confirms = [], list(choices), list(answers), list(confirms)

    def menu(title, options):
        seen.append((title, [k for k, _ in options]))
        return choices.pop(0)
    monkeypatch.setattr(cli, "menu", menu)
    monkeypatch.setattr(cli, "pause", lambda: None)
    monkeypatch.setattr(cli, "ask", lambda prompt, default=None: answers.pop(0))
    monkeypatch.setattr(cli, "confirm", lambda prompt, default=False: confirms.pop(0))
    return seen


def test_the_lock_menu_is_grouped(monkeypatch, capsys):
    c = MenuClient("a@b")
    visage, touch = c.locks[1], c.locks[0]
    seen = _drive(monkeypatch, ["people", "back", "schedule", "back", "maintenance", "back", "list", "back", "back"])
    cli.lock_menu(c, visage)
    assert seen[0] == ("Action", ["status", "unlock", "lock", "keep", "history", "people", "schedule",
                                  "maintenance", "list", "back"])
    assert seen[1] == ("People and PINs", ["guests", "addguest", "offline", "delguest", "enrol", "back"])
    assert seen[3] == ("Schedule", ["welcome", "rain", "back"])
    assert seen[5] == ("Maintenance", ["diagnose", "back"])
    assert seen[7] == ("Door list", ["hide", "up", "down", "back"])
    assert "locklyctl firmware, wifi, timezone" in capsys.readouterr().out
    # a single-schedule lock has no one-time slots, so no Keep unlocked; and no offline codes
    seen = _drive(monkeypatch, ["people", "back", "back"])
    cli.lock_menu(c, touch)
    assert "keep" not in seen[0][1]
    assert seen[1] == ("People and PINs", ["guests", "addguest", "delguest", "enrol", "back"])


def test_the_menu_removes_a_person_the_way_the_app_does(monkeypatch, capsys):
    c = MenuClient("a@b")
    c.lock_user_table = lambda lock: [{"pwd_id": 6, "user_id": 5, "user_type": 0x12, "pin": ""}]
    _drive(monkeypatch, ["people", "delguest", "back", "back"], answers=["5"])
    cli.lock_menu(c, c.locks[1])                                     # the Visage
    assert c.calls[-1] == ("delete", 5, 6, 0x12)                     # its own record, not only Lockly's
    assert "removed from the lock, then from Lockly's record" in capsys.readouterr().out


def test_an_offline_code_from_the_menu(monkeypatch, capsys):
    c = MenuClient("a@b")
    _drive(monkeypatch, ["people", "offline", "back", "back"], answers=["Sam"])
    cli.lock_menu(c, c.locks[1])                                     # the Visage
    out = capsys.readouterr().out
    assert oac.shown(_first_code()) in out and "for Sam, one use" in out
    assert c.oac_server.endpoints() == ["oac/get", "oac/saveName", "oac/usage/save"]
    # with no name nothing is asked of Lockly
    _drive(monkeypatch, ["people", "offline", "back", "back"], answers=[""])
    cli.lock_menu(c, c.locks[1])
    assert "a name is needed" in capsys.readouterr().out and len(c.oac_server.calls) == 3


def test_keep_unlocked_from_the_menu(monkeypatch, capsys):
    c = MenuClient("a@b")
    _drive(monkeypatch, ["keep", "back"], answers=["2"], confirms=[True])
    cli.lock_menu(c, c.locks[1])
    assert len(c.sent) == 1 and "unlocked until" in capsys.readouterr().out
    # a window is running now: the menu first offers to end it; declined, it asks for a new one
    _drive(monkeypatch, ["keep", "back"], answers=["40"], confirms=[False])      # 40 h: more than the lock can do
    cli.lock_menu(c, c.locks[1])
    assert len(c.sent) == 1 and "1 to 23" in capsys.readouterr().out
    _drive(monkeypatch, ["keep", "back"], confirms=[True])           # accepted: its slots come off, and it locks
    cli.lock_menu(c, c.locks[1])
    assert len(c.sent) == 2 and "ended, and locked" in capsys.readouterr().out
    assert B2 not in config.load_config()["keep_unlocked"]


def test_the_page_is_not_offered_the_command_lines_way_in():
    from locklyctl.gui.api import Api, over_client
    from locklyctl.gui.server import exposed_methods
    offered = exposed_methods(Api(client_factory=FakeClient))
    assert "over_client" not in offered and "for_client" not in offered
    # what the page does call, old and new
    assert {"session", "unlock", "lock", "capabilities", "door_access", "suspend_person", "resume_person",
            "unlock_app", "app_lock", "set_app_lock", "remove_app_lock", "lock_now", "erase_saved_data"} <= offered
    c = FakeClient("a@b")
    assert over_client(c)._c() is c
