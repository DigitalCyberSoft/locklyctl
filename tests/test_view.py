"""Hiding locks from listings and choosing their order (locklyctl/view.py),
signed in to Lockly (this computer's config) and to a private server, plus the
hide / unhide / move / list --all commands."""
import argparse
import json

import pytest

from locklyctl import commands as CMD
from locklyctl import config
from locklyctl import view as V
from locklyctl.transport import LocklyClient, LocklyError


def _locks(*names):
    return [{"ID": f"aa{i:022x}", "na": n} for i, n in enumerate(names, 1)]


def _lockly(monkeypatch, tmp_path, *names, email="owner@example.test"):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient(email)
    c.locks = _locks(*names)
    return c


def _server(monkeypatch, tmp_path, *names, reply="200"):
    """A --server client; its /api/view/* calls are recorded."""
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("owner@example.test", server="https://srv.example")
    c.locks = [{**lk, "hidden": False, "sortIndex": i} for i, lk in enumerate(_locks(*names), 1)]
    calls = []
    c._post = lambda endpoint, body, **kw: calls.append((endpoint, body)) or {"cod": reply}
    return c, calls


def names(locks):
    return [lk["na"] for lk in locks]


def by_name(c, name):
    return next(lk for lk in c.locks if lk["na"] == name)


# ── signed in to Lockly: this computer's config ─────────────────────────────

def test_hide_and_unhide_are_saved_per_account(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    V.set_hidden(c, by_name(c, "B"), True)
    assert names(V.listed(c)) == ["A", "C"]
    assert names(V.listed(c, include_hidden=True)) == ["A", "B", "C"]
    assert V.is_hidden(c, by_name(c, "B")) and not V.is_hidden(c, by_name(c, "A"))

    again = LocklyClient("owner@example.test")              # a later session sees it
    again.locks = _locks("A", "B", "C")
    assert names(V.listed(again)) == ["A", "C"]
    other = LocklyClient("someone@example.test")            # another account doesn't
    other.locks = _locks("A", "B", "C")
    assert names(V.listed(other)) == ["A", "B", "C"]

    V.set_hidden(c, by_name(c, "B"), False)
    assert names(V.listed(c)) == ["A", "B", "C"]
    V.set_hidden(c, by_name(c, "B"), False)                 # showing a shown lock is harmless
    assert config.load_config()["lock_view"]["owner@example.test"]["hidden"] == []


def test_default_order_is_the_accounts_or_by_name(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "10B", "2A", "1204", "2A Office")
    assert names(V.ordered(c)) == ["10B", "2A", "1204", "2A Office"]
    assert names(V.ordered(c, V.NAME_ORDER)) == ["2A", "2A Office", "10B", "1204"]


def test_move_steps_past_listed_locks_and_hidden_ones_keep_their_place(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    V.set_hidden(c, by_name(c, "B"), True)
    assert names(V.move(c, by_name(c, "A"), by=1)) == ["B", "C", "A"]     # past C, not hidden B
    assert names(V.listed(c)) == ["C", "A"]
    assert names(V.ordered(c)) == ["B", "C", "A"]                          # saved

    c2 = _lockly(monkeypatch, tmp_path / "2", "A", "B", "C")
    V.set_hidden(c2, by_name(c2, "B"), True)
    assert names(V.move(c2, by_name(c2, "C"), by=-1)) == ["C", "A", "B"]
    # with hidden locks shown, a move steps past them too
    assert names(V.move(c2, by_name(c2, "B"), by=-1, include_hidden=True)) == ["C", "B", "A"]


def test_move_to_a_place_and_clamping(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C", "D")
    assert names(V.move(c, by_name(c, "D"), to=1)) == ["D", "A", "B", "C"]
    assert names(V.move(c, by_name(c, "D"), to=99)) == ["A", "B", "C", "D"]
    assert names(V.move(c, by_name(c, "C"), by=-10)) == ["C", "A", "B", "D"]
    assert names(V.move(c, by_name(c, "C"), by=0)) == ["C", "A", "B", "D"]  # no-op


def test_a_hidden_lock_moves_among_the_listed_ones(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    V.set_hidden(c, by_name(c, "B"), True)
    assert names(V.move(c, by_name(c, "B"), by=-1)) == ["B", "A", "C"]


def test_saved_order_wins_and_new_locks_go_last(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    V.move(c, by_name(c, "C"), to=1, default=V.NAME_ORDER)
    assert names(V.ordered(c)) == ["C", "A", "B"]                    # whatever the default
    assert names(V.ordered(c, V.NAME_ORDER)) == ["C", "A", "B"]
    c.locks.append({"ID": "bb" + "0" * 22, "na": "D"})               # added to the account since
    assert names(V.ordered(c)) == ["C", "A", "B", "D"]


def test_moving_a_lock_no_longer_on_the_account_fails(monkeypatch, tmp_path):
    c = _lockly(monkeypatch, tmp_path, "A", "B")
    with pytest.raises(LocklyError):
        V.move(c, {"ID": "gone", "na": "X"}, by=1)


# ── signed in to a private server: the server keeps them ────────────────────

def test_server_hide_posts_and_marks_the_lock(monkeypatch, tmp_path):
    c, calls = _server(monkeypatch, tmp_path, "A", "B")
    V.set_hidden(c, by_name(c, "B"), True)
    assert calls == [("view/hidden", {"deviceId": by_name(c, "B")["ID"], "hidden": True})]
    assert by_name(c, "B")["hidden"] is True
    assert names(V.listed(c)) == ["A"]
    assert "lock_view" not in config.load_config()                   # nothing stored locally


def test_server_move_posts_the_full_order(monkeypatch, tmp_path):
    c, calls = _server(monkeypatch, tmp_path, "A", "B", "C")
    assert names(V.move(c, by_name(c, "A"), by=2)) == ["B", "C", "A"]
    assert calls == [("view/order", {"order": [by_name(c, n)["ID"] for n in ("B", "C", "A")]})]
    assert names(V.ordered(c)) == ["B", "C", "A"]                    # sortIndex updated in memory


def test_server_refusal_raises(monkeypatch, tmp_path):
    c, _ = _server(monkeypatch, tmp_path, "A", "B", reply="901")
    with pytest.raises(LocklyError):
        V.set_hidden(c, by_name(c, "A"), True)
    assert by_name(c, "A")["hidden"] is False                         # not marked on failure


# ── the commands ────────────────────────────────────────────────────────────

def _args(**kw):
    return argparse.Namespace(**kw)


def test_hide_list_unhide_commands(monkeypatch, tmp_path, capsys):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    assert CMD._hide(c, _args(lock=["b"])) == CMD.EXIT_OK            # case-insensitive, as find_lock
    capsys.readouterr()

    assert CMD._list(c, _args(all=False, json=True)) == CMD.EXIT_OK
    assert [x["name"] for x in json.loads(capsys.readouterr().out)] == ["A", "C"]

    CMD._list(c, _args(all=True, json=True))
    listed = json.loads(capsys.readouterr().out)
    assert [(x["name"], x["hidden"]) for x in listed] == [("A", False), ("B", True), ("C", False)]

    CMD._list(c, _args(all=False, json=False))
    out = capsys.readouterr()
    assert "B" not in out.out.split() and "1 hidden" in out.err       # the hint stays off stdout

    CMD._unhide(c, _args(lock=["B"]))
    capsys.readouterr()
    CMD._list(c, _args(all=False, json=True))
    assert [x["name"] for x in json.loads(capsys.readouterr().out)] == ["A", "B", "C"]


def test_move_command(monkeypatch, tmp_path, capsys):
    c = _lockly(monkeypatch, tmp_path, "A", "B", "C")
    none = dict(up=None, down=None, top=False, bottom=False, to=None)
    assert CMD._move(c, _args(lock="A", **{**none, "down": 1})) == CMD.EXIT_OK
    assert "2 of 3" in capsys.readouterr().out
    CMD._move(c, _args(lock="C", **{**none, "top": True}))
    CMD._move(c, _args(lock="B", **{**none, "bottom": True}))
    assert names(V.ordered(c)) == ["C", "A", "B"]
    with pytest.raises(CMD.UsageError):
        CMD._move(c, _args(lock="A", **{**none, "to": 0}))
