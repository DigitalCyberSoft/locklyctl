"""The app lock: a passcode that seals the config (vault.py, config.py) and
that the GUI asks for when it starts (gui/api.py). No network, no keyring."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from locklyctl import config, vault
from locklyctl import wifi_passwords as W
from locklyctl.gui.api import Api
from test_gui_api import FakeClient, UUID


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    """A config dir of its own, no keyring, and scrypt at its cheapest so the
    suite stays quick (the real cost is checked without running it)."""
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_keyring", lambda: None)
    monkeypatch.setattr(vault, "N", 1 << 10)
    config.lock()
    yield tmp_path
    config.lock()


def _file(home) -> str:
    return (home / "config.json").read_text()


# ── the envelope ────────────────────────────────────────────────────────────

def test_the_real_cost_is_64_mib_a_guess():
    import importlib
    fresh = importlib.reload(importlib.import_module("locklyctl.vault"))
    try:
        assert (fresh.N, fresh.R, fresh.P) == (1 << 16, 8, 1) and 128 * fresh.N * fresh.R == 64 << 20
    finally:
        importlib.reload(fresh)         # the fixture's cheap N is put back by monkeypatch on teardown


def test_seal_and_unseal():
    head = vault.new_head()
    keys = vault.derive("correct horse", head["salt"], head["n"], head["r"], head["p"])
    plain = json.dumps({"email": "owner@example.com", "pw_hash": "H" * 64}).encode()
    sealed = vault.seal(plain, keys, head)
    assert vault.is_sealed(sealed) and not vault.is_sealed(plain) and not vault.is_sealed(b"not json")
    assert b"owner@example.com" not in sealed and b"HHHH" not in sealed
    assert vault.unseal(sealed, keys) == plain
    assert vault.header(sealed) == head
    # every save takes a new iv, so the same data never encrypts the same way twice
    assert vault.seal(plain, keys, head) != sealed
    # the wrong passcode, and a file someone changed, both fail the MAC
    wrong = vault.derive("correct horsf", head["salt"], head["n"], head["r"], head["p"])
    with pytest.raises(vault.WrongPasscode):
        vault.unseal(sealed, wrong)
    doc = json.loads(sealed)
    data = bytearray(__import__("base64").b64decode(doc["encrypted"]["data"]))
    data[0] ^= 1
    doc["encrypted"]["data"] = __import__("base64").b64encode(bytes(data)).decode()
    with pytest.raises(vault.WrongPasscode):
        vault.unseal(json.dumps(doc), keys)
    # the empty config seals too
    assert vault.unseal(vault.seal(b"", keys, head), keys) == b""


def test_a_hostile_file_cant_ask_for_gigabytes():
    head = vault.new_head()
    keys = vault.derive("pass", head["salt"], head["n"], head["r"], head["p"])
    doc = json.loads(vault.seal(b"{}", keys, head))
    for field, bad in (("n", 1 << 24), ("n", 1000), ("r", 64), ("p", 0), ("n", True), ("kdf", "pbkdf2"),
                       ("cipher", "aes-gcm"), ("salt", "AAAA"), ("salt", "not base64!")):
        broken = json.loads(json.dumps(doc))
        broken["encrypted"][field] = bad
        with pytest.raises(vault.VaultError):
            vault.header(json.dumps(broken))
    with pytest.raises(vault.VaultError):
        vault.header(json.dumps({**doc, "version": 2}))


def test_passcode_rules():
    for bad in ("", "123", "x" * 129, None, 1234):
        with pytest.raises(ValueError):
            vault.check_passcode(bad)
    vault.check_passcode("1234")
    assert vault.strength("123456") == "short" and vault.strength("hunter2") == "short"
    assert vault.strength("1234567890") == "long" and vault.strength("correct horse") == "long"


# ── the config, sealed ──────────────────────────────────────────────────────

def test_turning_the_lock_on_seals_the_config(home):
    config.save_config({"email": "owner@example.com", "pw_hash": "H" * 64, "favorites": ["aa"]})
    assert not config.sealed() and not config.locked() and "owner@example.com" in _file(home)
    assert config.set_passcode("2468") == []
    assert config.sealed() and not config.locked()                 # sealed on disk, open to this process
    text = _file(home)
    assert json.loads(text)["format"] == "locklyctl-config"
    assert "owner@example.com" not in text and "HHHH" not in text and "favorites" not in text
    assert config.load_config() == {"email": "owner@example.com", "pw_hash": "H" * 64, "favorites": ["aa"]}
    # a save while unlocked stays sealed, and reads back
    config.save_config({**config.load_config(), "connection": "remote"})
    assert "remote" not in _file(home) and config.load_config()["connection"] == "remote"
    assert oct((home / "config.json").stat().st_mode & 0o777) == "0o600"


def test_locked_means_nothing_reads_or_writes(home):
    config.save_config({"email": "owner@example.com", "pw_hash": "H" * 64})
    config.set_passcode("2468")
    before = _file(home)
    config.lock()                                                  # what a new run of the app starts as
    assert config.sealed() and config.locked()
    with pytest.raises(config.Locked):
        config.load_config()
    # the failure this guards against: a caller taking "can't read" for "empty" and saving over it
    with pytest.raises(config.Locked):
        config.save_config({"email": "someone@else"})
    with pytest.raises(config.Locked):
        config.stash_suspended("dev", 7, {"user_type": 0x42, "date": "00"})
    assert _file(home) == before
    with pytest.raises(vault.WrongPasscode):
        config.unlock("2469")
    assert config.locked()
    config.unlock("2468")
    assert not config.locked() and config.load_config()["email"] == "owner@example.com"


def test_wrong_passcodes_make_the_next_one_wait(home, monkeypatch):
    config.save_config({"email": "owner@example.com"})
    config.set_passcode("2468")
    config.lock()
    now = [1_800_000_000.0]
    monkeypatch.setattr(config.time, "time", lambda: now[0])
    for _ in range(config.FREE_TRIES - 1):                          # the first four cost nothing
        with pytest.raises(vault.WrongPasscode):
            config.unlock("0000")
        assert config.unlock_wait() == 0
    with pytest.raises(vault.WrongPasscode):                        # the fifth starts a 30 s wait
        config.unlock("0000")
    assert config.unlock_wait() == 30
    with pytest.raises(config.TooManyTries) as e:                   # even the right one waits
        config.unlock("2468")
    assert e.value.wait == 30 and config.locked()
    now[0] += 31
    with pytest.raises(vault.WrongPasscode):                        # and each further one doubles it
        config.unlock("0000")
    assert config.unlock_wait() == 60
    now[0] += 61
    config.unlock("2468")                                           # the right one clears the count
    assert not config.locked() and config.unlock_wait() == 0 and not (home / "unlock-tries.json").exists()
    # the wait is capped at 15 minutes
    config.lock()
    for _ in range(20):
        now[0] += 1000
        with pytest.raises(vault.WrongPasscode):
            config.unlock("0000")
    assert config.unlock_wait() == 900


def test_changing_and_removing_the_passcode(home):
    config.save_config({"email": "owner@example.com", "pw_hash": "H" * 64})
    config.set_passcode("2468")
    for wrong in (None, "", "0000"):                                # changing takes the current one, even unlocked
        with pytest.raises(vault.WrongPasscode):
            config.set_passcode("a longer passcode", wrong)
    config.set_passcode("a longer passcode", "2468")
    assert config.load_config()["pw_hash"] == "H" * 64             # still open to this process
    config.lock()
    with pytest.raises(vault.WrongPasscode):
        config.unlock("2468")
    config.unlock("a longer passcode")
    with pytest.raises(ValueError):
        config.set_passcode("abc", "a longer passcode")            # too short
    with pytest.raises(vault.WrongPasscode):
        config.remove_passcode("2468")
    assert config.sealed()
    config.remove_passcode("a longer passcode")
    assert not config.sealed() and json.loads(_file(home)) == {"email": "owner@example.com", "pw_hash": "H" * 64}


def test_erasing_a_locked_config(home):
    config.save_config({"email": "owner@example.com"})
    config.set_passcode("2468")
    config.lock()
    with pytest.raises(vault.WrongPasscode):
        config.unlock("0000")
    config.erase()
    assert not (home / "config.json").exists() and not (home / "unlock-tries.json").exists()
    assert not config.sealed() and config.load_config() == {}


def test_another_locklyctl_changing_the_lock(home):
    config.save_config({"email": "owner@example.com"})
    config.set_passcode("2468")
    # another process turns the lock off: this one must not seal the data again with its old keys
    (home / "config.json").write_text(json.dumps({"email": "owner@example.com", "connection": "remote"}))
    assert not config.locked() and config.load_config()["connection"] == "remote"
    config.save_config({**config.load_config(), "favorites": ["aa"]})
    assert not config.sealed() and json.loads(_file(home))["favorites"] == ["aa"]
    # another process changes the passcode: this one's keys no longer open it
    config.set_passcode("2468")
    mine = _file(home)
    config.set_passcode("another one", "2468")
    theirs = _file(home)
    (home / "config.json").write_text(mine)
    config.lock(); config.unlock("2468")                           # this process, on the old file
    (home / "config.json").write_text(theirs)                      # the other process's change lands
    assert config.locked()
    with pytest.raises(config.Locked):
        config.load_config()
    with pytest.raises(config.Locked):
        config.save_config({})
    assert _file(home) == theirs


def test_a_damaged_sealed_config_is_not_taken_for_an_empty_one(home):
    config.save_config({"email": "owner@example.com"})
    config.set_passcode("2468")
    doc = json.loads(_file(home))
    doc["encrypted"]["salt"] = "AAAA"
    (home / "config.json").write_text(json.dumps(doc))
    with pytest.raises(vault.VaultError):
        config.load_config()
    with pytest.raises(vault.VaultError):
        config.save_config({})


# ── the OS keyring, with the lock on ────────────────────────────────────────

class FakeKeyring:
    """What config._keyring returns: the keyring module's own surface."""
    class errors:
        class KeyringError(Exception):
            pass

        class PasswordDeleteError(KeyringError):
            pass

    def __init__(self, items=None, undeletable=()):
        self.items, self.undeletable = dict(items or {}), set(undeletable)

    def get_password(self, service, name):
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        if name in self.undeletable or (service, name) not in self.items:
            raise self.errors.PasswordDeleteError(name)
        del self.items[(service, name)]


def test_secrets_leave_the_keyring_for_the_sealed_config(home, monkeypatch):
    kr = FakeKeyring({("locklyctl", "owner@example.com"): "H" * 64, ("locklyctl-wifi", "Home"): "wifi-secret"})
    backend = lambda: None if config._keys is not None else kr    # config._keyring's own rule, over the fake
    monkeypatch.setattr(config, "_keyring", backend)
    config.save_config({"email": "owner@example.com", "pw_in_keyring": True, "wifi_in_keyring": ["Home"],
                        "wifi_passwords": {"Cafe": "other"}})
    assert config.load_credential(config.load_config()) == "H" * 64          # from the keyring, before
    assert config.set_passcode("2468") == []
    assert kr.items == {}                                                    # nothing left outside the seal
    cfg = config.load_config()
    assert cfg["pw_hash"] == "H" * 64 and "pw_in_keyring" not in cfg
    assert cfg["wifi_in_keyring"] == [] and cfg["wifi_passwords"] == {"Cafe": "other", "Home": "wifi-secret"}
    assert config.load_credential(cfg) == "H" * 64 and W.saved("Home") == ("wifi-secret", "")
    assert "wifi-secret" not in _file(home) and "HHHH" not in _file(home)
    # with the lock on, new secrets go in the sealed config too, not the keyring
    assert W.save("Office", "pw-3") == "file" and config._save_credential(config.load_config(), "owner@example.com", "N" * 64) == "file"
    assert kr.items == {} and "pw-3" not in _file(home)


def test_the_real_keyring_rule_stands_down_while_the_lock_is_on(home, monkeypatch):
    monkeypatch.undo()                                             # the real config._keyring, on a temp home
    monkeypatch.setenv("LOCKLYCTL_HOME", str(home))
    monkeypatch.setattr(vault, "N", 1 << 10)
    config.save_config({"email": "owner@example.com"})
    monkeypatch.setattr(config, "_secrets_into_config", lambda cfg: (cfg, None, []))   # never read a real keyring here
    config.set_passcode("2468")
    assert config._keyring() is None                              # whatever backend this machine has


def test_a_secret_the_keyring_wont_give_up_is_reported(home, monkeypatch):
    kr = FakeKeyring({("locklyctl", "owner@example.com"): "H" * 64}, undeletable={"owner@example.com"})
    monkeypatch.setattr(config, "_keyring", lambda: None if config._keys is not None else kr)
    config.save_config({"email": "owner@example.com", "pw_in_keyring": True})
    assert config.set_passcode("2468") == ["owner@example.com"]    # sealed, but a copy is still in the keyring
    assert config.load_config()["pw_hash"] == "H" * 64

    class Unreadable(FakeKeyring):
        def get_password(self, service, name):
            raise self.errors.KeyringError("locked")
    config.remove_passcode("2468")
    config.save_config({"email": "owner@example.com", "pw_in_keyring": True})
    monkeypatch.setattr(config, "_keyring", lambda: None if config._keys is not None else Unreadable())
    with pytest.raises(vault.VaultError, match="wasn't turned on"):
        config.set_passcode("2468")
    assert not config.sealed() and config.load_config()["pw_in_keyring"] is True    # left as it was


# ── the app: asking for the passcode when it starts ─────────────────────────

def _signed_in_api():
    api = Api(client_factory=FakeClient)
    api.sign_in("a@b", "right", True)                              # remembered: the credential is in the config
    return api


def test_the_app_starts_locked_and_opens_with_the_passcode(home):
    api = _signed_in_api()
    assert api.app_lock() == {"on": False, "available": ""}
    assert api.set_app_lock("2468") == {"on": True, "strength": "short", "leftInKeyring": []}
    assert api.app_lock()["on"] and api.session()["signedIn"]
    # a new run of the app: nothing is known until the passcode is given
    fresh = Api(client_factory=FakeClient)
    config.lock()
    s = fresh.session()
    assert s == {"signedIn": False, "locked": True, "wait": 0, "email": ""}
    for call in (fresh.locks, fresh.connection, lambda: fresh.sign_in("a@b", "right", True)):
        with pytest.raises(Exception):
            call()
    assert fresh.capabilities()["device"]                           # what the page needs to draw the unlock screen
    with pytest.raises(vault.WrongPasscode):
        fresh.unlock_app("0000")
    s = fresh.unlock_app("2468")
    assert s == {"signedIn": True, "email": "a@b"} and len(fresh.locks()) == 2
    # unlocking a DOOR is still its own call
    assert set(fresh.unlock(UUID)) == {"ok", "detail", "lock"}


def test_lock_now_and_erase(home):
    api = _signed_in_api()
    with pytest.raises(ValueError):
        api.lock_now()                                              # no passcode set
    with pytest.raises(ValueError):
        api.erase_saved_data()                                      # not locked
    api.set_app_lock("2468")
    with pytest.raises(ValueError):
        api.erase_saved_data()                                      # unlocked: change or turn off instead
    assert api.lock_now() == {"locked": True, "wait": 0}
    assert api._client is None and api.session()["locked"]
    assert api.unlock_app("2468")["signedIn"]
    api.lock_now()
    assert api.erase_saved_data() == {"signedIn": False, "email": ""}
    assert api.session() == {"signedIn": False, "email": ""} and not api.app_lock()["on"]


def test_changing_and_turning_off_from_the_app(home):
    api = _signed_in_api()
    api.set_app_lock("2468")
    with pytest.raises(vault.WrongPasscode):
        api.set_app_lock("a longer passcode", "")
    assert api.set_app_lock("a longer passcode", "2468")["strength"] == "long"
    with pytest.raises(vault.WrongPasscode):
        api.remove_app_lock("2468")
    assert api.remove_app_lock("a longer passcode") == {"on": False}
    assert json.loads(_file(home))["email"] == "a@b" and api.session()["signedIn"]


def test_too_many_tries_reach_the_page_as_a_wait(home):
    api = _signed_in_api()
    api.set_app_lock("2468")
    api.lock_now()
    for _ in range(config.FREE_TRIES):
        with pytest.raises(vault.WrongPasscode):
            api.unlock_app("0000")
    assert api.session()["wait"] == 30
    with pytest.raises(config.TooManyTries, match="Try again in 30 s"):
        api.unlock_app("2468")


# ── the terminal and the MCP server, with the lock on ───────────────────────

def _sealed_with_login(home):
    config.save_config({"email": "owner@example.com", "pw_hash": "H" * 64, "wifi_passwords": {"Home": "wifi-secret"}})
    config.set_passcode("2468")
    config.lock()
    path = home / "passcode"
    path.write_text("2468\n")
    return str(path)


def test_commands_open_the_saved_data_with_a_passcode_file(home, capsys, monkeypatch):
    from locklyctl import cli
    passcode_file = _sealed_with_login(home)
    monkeypatch.delenv("LOCKLYCTL_PASSCODE_FILE", raising=False)
    # nothing can ask (no terminal here): say how, change nothing
    before = _file(home)
    assert cli.main(["--logout"]) == 2
    assert "--passcode-file" in capsys.readouterr().err and _file(home) == before and config.locked()
    # a wrong passcode in the file: refused, counted, nothing changed
    wrong = home / "wrong"
    wrong.write_text("0000\n")
    assert cli.main(["--passcode-file", str(wrong), "--logout"]) == 1
    assert "Wrong passcode" in capsys.readouterr().err and _file(home) == before
    assert cli.main(["--passcode-file", str(home / "missing"), "--logout"]) == 1
    assert "No such file" in capsys.readouterr().err
    # the right one: the command runs, and what it saves stays sealed
    assert cli.main(["--passcode-file", passcode_file, "wifi", "--ssid", "Home", "--forget"]) == 0
    assert config.load_config()["wifi_passwords"] == {} and config.sealed() and "wifi-secret" not in _file(home)
    config.lock()
    monkeypatch.setenv("LOCKLYCTL_PASSCODE_FILE", passcode_file)             # the same, from the environment
    assert cli.main(["--logout"]) == 0
    assert config.load_config().get("pw_hash") is None and config.load_config()["email"] == "owner@example.com"
    assert config.sealed()


def test_the_terminal_asks_for_the_passcode(home, capsys, monkeypatch):
    from locklyctl import cli
    _sealed_with_login(home)
    monkeypatch.delenv("LOCKLYCTL_PASSCODE_FILE", raising=False)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    answers = iter(["1111", "2468"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(answers))
    assert cli.unlock_saved_data(None) == 0 and not config.locked()
    assert "wrong passcode" in capsys.readouterr().err
    config.lock()
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "0000")
    assert cli.unlock_saved_data(None) == 1 and config.locked()             # three wrong ones, then it stops
    # no lock: nothing is asked
    config.unlock("2468"); config.remove_passcode("2468")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: pytest.fail("asked without a lock"))
    assert cli.unlock_saved_data(None) == 0


def test_the_mcp_server_says_why_when_it_is_locked(home, monkeypatch):
    pytest.importorskip("mcp")
    from locklyctl import mcp_server as M
    passcode_file = _sealed_with_login(home)
    doors = M.Doors(Api(client_factory=FakeClient))
    with pytest.raises(M.ToolError, match="--passcode-file"):
        doors.views()
    config.unlock(config.passcode_from_file(passcode_file))                  # what its --passcode-file does at start
    monkeypatch.setattr(FakeClient, "login_with_hash", lambda self, h: setattr(self, "pw_hash", h))
    assert len(M.Doors(Api(client_factory=FakeClient)).views()) == 2
