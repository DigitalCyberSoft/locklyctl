"""Config file, logging, the saved login credential and the app lock.

Shared by the CLI and the GUI. Everything lives in ~/.locklyctl (owner-only),
or in $LOCKLYCTL_HOME when that is set.

The app lock: with an app passcode set (set_passcode), the config file is kept
sealed (vault.py) and nothing can read or write it until unlock() has been
given the passcode in this process. load_config and save_config raise Locked
until then, so no caller can mistake sealed data for an empty config and
write over it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

from . import vault
from .const import CONFIG_DIR_NAME, CONFIG_FILE_NAME, CONNECTIONS

_LOG = logging.getLogger("locklyctl")
WIFI_KEYRING_SERVICE = "locklyctl-wifi"      # where wifi_passwords.py keeps saved Wi-Fi passwords


class Locked(vault.VaultError):
    """The saved data is sealed with the app passcode and isn't unlocked here."""
    def __init__(self) -> None:
        super().__init__("locklyctl is locked: its saved data is sealed with the app passcode.")


class TooManyTries(vault.VaultError):
    """Wrong passcodes in a row: no more are taken for ``wait`` seconds."""
    def __init__(self, wait: int) -> None:
        self.wait = wait
        super().__init__(f"Too many wrong passcodes. Try again in {_wait_text(wait)}.")


def _wait_text(seconds: int) -> str:
    return f"{seconds} s" if seconds < 90 else f"{math.ceil(seconds / 60)} min"


# The app lock's keys while this process is unlocked, the salt and scrypt
# parameters they were made for, and the last sealed file read or written with
# its JSON (so a read doesn't decrypt again until the file changes).
_state = threading.RLock()
_keys: tuple[bytes, bytes] | None = None
_head: dict | None = None
_cache: tuple[bytes, str] | None = None


def _config_dir() -> Path:
    override = os.environ.get("LOCKLYCTL_HOME")
    return Path(override) if override else Path.home() / CONFIG_DIR_NAME


def _config_path() -> Path:
    return _config_dir() / CONFIG_FILE_NAME


def _log_path() -> Path:
    return _config_dir() / "locklyctl.log"


def _setup_logging(debug: bool = False) -> None:
    """Console at WARNING (or DEBUG with --debug); always a rotating DEBUG file
    log at ~/.locklyctl/locklyctl.log. Secrets (password, JWT) are never logged;
    frames are AES-sealed, so frame hex in the log reveals nothing usable."""
    root = logging.getLogger("locklyctl")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    if sys.stderr is not None:      # a windowed (frozen) GUI app has no console
        console = logging.StreamHandler()
        console.setLevel(logging.DEBUG if debug else logging.WARNING)
        console.setFormatter(fmt)
        root.addHandler(console)

    try:
        from logging.handlers import RotatingFileHandler
        p = _log_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(p, maxBytes=512_000, backupCount=3)
        fileh.setLevel(logging.DEBUG)
        fileh.setFormatter(fmt)
        root.addHandler(fileh)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
        root.debug("── locklyctl %s started ──", __import__("locklyctl").__version__)
    except Exception as e:  # file logging is best-effort, never fatal
        root.warning("file logging unavailable: %s", e)


def _read_raw() -> bytes | None:
    try:
        return _config_path().read_bytes()
    except FileNotFoundError:
        return None


def _ours(raw: bytes) -> bool:
    """Whether the sealed file ``raw`` is one this process holds the keys for.
    False too after another process changed the passcode (a new salt)."""
    if _keys is None:
        return False
    head = vault.header(raw)
    return all(head[k] == _head[k] for k in ("salt", "n", "r", "p"))


def load_config() -> dict:
    """The config. Raises Locked when it's sealed with the app passcode and
    this process hasn't unlocked it, and vault.VaultError when it's sealed
    and damaged: an empty config there would be saved over the sealed one."""
    global _cache
    raw = _read_raw()
    if raw is None:
        return {}
    if not vault.is_sealed(raw):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    with _state:
        if not _ours(raw):
            raise Locked()
        stamp = hashlib.sha256(raw).digest()
        if _cache is None or _cache[0] != stamp:
            _cache = (stamp, vault.unseal(raw, _keys).decode("utf-8"))
        return json.loads(_cache[1])


def connection_mode(cfg: dict) -> str:
    """The saved connection setting (const.CONNECTIONS); "auto" when unset."""
    mode = cfg.get("connection")
    return mode if mode in CONNECTIONS else "auto"


def save_config(cfg: dict) -> None:
    """Write the config owner-only from the first byte (it can hold the login
    credential), via a temp file + rename so a crash never truncates it.
    Sealed when the app lock is on. Raises Locked rather than write over
    sealed data this process can't read."""
    global _keys, _head, _cache
    with _state:
        raw = _read_raw()
        if raw is not None and vault.is_sealed(raw):
            if not _ours(raw):
                raise Locked()
        elif _keys is not None:
            # Plain (or gone) while we hold keys: the app lock was turned off, or the
            # data erased, from another locklyctl. Sealing it again would undo that.
            _keys = _head = _cache = None
        _write(cfg, _keys, _head)


def _write(cfg: dict, keys: tuple[bytes, bytes] | None, head: dict | None) -> None:
    """The config to disk: sealed under ``keys`` (made for ``head``), or plain."""
    global _cache
    p = _config_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)       # also tighten a dir made by an older version
    text = json.dumps(cfg, indent=2)
    data = vault.seal(text.encode("utf-8"), keys, head) if keys else text.encode("utf-8")
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chmod(tmp, 0o600)            # in case tmp pre-existed with wider perms
    os.replace(tmp, p)
    _cache = (hashlib.sha256(data).digest(), text) if keys else None


# ── the app lock ─────────────────────────────────────────────────────────────
# A passcode that seals the config. Wrong passcodes are counted in a small file
# beside it, so the wait survives a restart of the app: after FREE_TRIES wrong
# ones in a row each further one doubles a wait that starts at 30 s (15 min at
# most). That slows someone trying passcodes in the app; it does nothing
# against someone who copies the sealed file and tries them elsewhere.
FREE_TRIES = 5
_FIRST_WAIT_S, _MAX_WAIT_S = 30, 900


def sealed() -> bool:
    """Whether the app lock is on (the config on disk is sealed)."""
    raw = _read_raw()
    return raw is not None and vault.is_sealed(raw)


def locked() -> bool:
    """Whether the config is sealed and this process hasn't unlocked it."""
    raw = _read_raw()
    if raw is None or not vault.is_sealed(raw):
        return False
    with _state:
        return not _ours(raw)


def _tries_path() -> Path:
    return _config_dir() / "unlock-tries.json"


def _tries() -> dict:
    try:
        rec = json.loads(_tries_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def unlock_wait() -> int:
    """Seconds until another passcode is taken (0 when one is taken now)."""
    until = _tries().get("until")
    return max(0, math.ceil(until - time.time())) if isinstance(until, (int, float)) else 0


def _wrong_passcode() -> None:
    rec = _tries()
    fails = (rec.get("fails") if isinstance(rec.get("fails"), int) else 0) + 1
    wait = 0 if fails < FREE_TRIES else min(_FIRST_WAIT_S * 2 ** (fails - FREE_TRIES), _MAX_WAIT_S)
    path = _tries_path()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"fails": fails, "until": time.time() + wait}, f)


def _keys_for(passcode: str, raw: bytes) -> tuple[tuple[bytes, bytes], dict, str]:
    """The keys this passcode gives for the sealed file ``raw``, its header and
    its JSON. Raises TooManyTries, or vault.WrongPasscode (and counts it)."""
    wait = unlock_wait()
    if wait:
        raise TooManyTries(wait)
    head = vault.header(raw)
    keys = vault.derive(passcode or "", head["salt"], head["n"], head["r"], head["p"])
    try:
        text = vault.unseal(raw, keys).decode("utf-8")
    except vault.WrongPasscode:
        _wrong_passcode()
        raise
    _tries_path().unlink(missing_ok=True)
    return keys, head, text


def unlock(passcode: str) -> None:
    """Unlock the sealed config for this process. Nothing to do when the app
    lock is off. Raises vault.WrongPasscode or TooManyTries."""
    global _keys, _head, _cache
    raw = _read_raw()
    if raw is None or not vault.is_sealed(raw):
        return
    keys, head, text = _keys_for(passcode, raw)
    with _state:
        _keys, _head, _cache = keys, head, (hashlib.sha256(raw).digest(), text)


def passcode_from_file(path: str) -> str:
    """The app passcode kept in a file, for runs that can't ask for it (cron,
    the MCP server): the file's first line. Anyone who can read that file can
    open the saved data, so it belongs somewhere only you can read."""
    with open(path, encoding="utf-8") as f:
        return f.readline().rstrip("\r\n")


def lock() -> None:
    """Forget the app lock's keys: the config is sealed to this process again."""
    global _keys, _head, _cache
    with _state:
        _keys = _head = _cache = None


def set_passcode(new: str, current: str | None = None) -> list[str]:
    """Turn the app lock on, or change its passcode (``current`` is then
    required, even while unlocked). Turning it on moves the saved sign-in and
    Wi-Fi passwords out of the OS keyring into the config being sealed: the
    desktop session opens the keyring without the passcode. Returns what
    couldn't be taken out of the keyring afterwards ([] when all went)."""
    global _keys, _head
    why = vault.available()
    if why:
        raise vault.VaultError(why)
    vault.check_passcode(new)
    with _state:
        raw = _read_raw()
        kr, moved = None, []
        if raw is not None and vault.is_sealed(raw):
            cfg = json.loads(_keys_for(current or "", raw)[2])
        else:
            cfg, kr, moved = _secrets_into_config(load_config())
        head = vault.new_head()
        keys = vault.derive(new, head["salt"], head["n"], head["r"], head["p"])
        _write(cfg, keys, head)
        _keys, _head = keys, head
    return _delete_from_keyring(kr, moved)


def remove_passcode(current: str) -> None:
    """Turn the app lock off: the config is written plain again (owner-only).
    The sign-in goes back to the OS keyring, where there is one, the next
    time the app signs in."""
    global _keys, _head, _cache
    with _state:
        raw = _read_raw()
        if raw is None or not vault.is_sealed(raw):
            return
        cfg = json.loads(_keys_for(current or "", raw)[2])
        _keys = _head = _cache = None
        _write(cfg, None, None)


def erase() -> None:
    """Delete this device's saved data: the config (sealed or not) and the
    count of wrong passcodes. For a forgotten passcode: nothing can open the
    sealed data without it. Locks and the Lockly account are not touched."""
    global _keys, _head, _cache
    with _state:
        _keys = _head = _cache = None
        for path in (_config_path(), _config_path().with_suffix(".tmp"), _tries_path()):
            path.unlink(missing_ok=True)


def _secrets_into_config(cfg: dict):
    """(``cfg`` with the saved sign-in and Wi-Fi passwords taken from the OS
    keyring into it, the keyring, the (service, name) entries to delete from
    it once that config is written). A keyring that can't be read raises:
    better not to turn the lock on than to seal a config that lost its sign-in."""
    kr = _keyring()
    if kr is None:
        return cfg, None, []
    cfg, moved = dict(cfg), []
    try:
        if cfg.pop("pw_in_keyring", None):
            email = cfg.get("email", "")
            pw_hash = kr.get_password(_KEYRING_SERVICE, email)
            if pw_hash:
                cfg["pw_hash"] = pw_hash
                moved.append((_KEYRING_SERVICE, email))
        wifi = dict(cfg.get("wifi_passwords") or {})
        for ssid in cfg.get("wifi_in_keyring") or []:
            password = kr.get_password(WIFI_KEYRING_SERVICE, ssid)
            if password:
                wifi[ssid] = password
                moved.append((WIFI_KEYRING_SERVICE, ssid))
    except kr.errors.KeyringError as e:
        raise vault.VaultError(f"The app lock wasn't turned on: the keyring that holds the saved sign-in "
                               f"couldn't be read ({e}).") from e
    if cfg.get("wifi_in_keyring"):
        cfg["wifi_in_keyring"], cfg["wifi_passwords"] = [], wifi
    return cfg, kr, moved


def _delete_from_keyring(kr, entries: list[tuple[str, str]]) -> list[str]:
    """Delete keyring entries that are now in the sealed config. Returns the
    names of any that couldn't be deleted, for the caller to tell the user:
    those secrets can then still be read without the passcode."""
    left = []
    for service, name in entries:
        try:
            kr.delete_password(service, name)
        except kr.errors.KeyringError as e:
            _LOG.warning("couldn't delete %s from the keyring: %s", service, e)
            left.append(name)
    return left


# Suspended access users: a lock_id:user_id -> {"user_type", "date"} map in the
# owner-only config, so `resume` can restore the exact schedule a `suspend`
# replaced with a far-future window. The date is a schedule, not a secret, but
# it stays in the owner-only config all the same.
def _suspended_key(lock_id: str, user_id: int) -> str:
    return f"{lock_id}:{user_id}"


def stash_suspended(lock_id: str, user_id: int, record: dict) -> None:
    cfg = load_config()
    users = dict(cfg.get("suspended_users") or {})
    users[_suspended_key(lock_id, user_id)] = record
    save_config({**cfg, "suspended_users": users})


def get_suspended(lock_id: str, user_id: int) -> dict | None:
    return (load_config().get("suspended_users") or {}).get(_suspended_key(lock_id, user_id))


def pop_suspended(lock_id: str, user_id: int) -> dict | None:
    cfg = load_config()
    users = dict(cfg.get("suspended_users") or {})
    record = users.pop(_suspended_key(lock_id, user_id), None)
    save_config({**cfg, "suspended_users": users})
    return record


# Offline codes made from this computer today, per door: lock_id -> {"day",
# "count"}. The app keeps the same count per phone to ask before too many in
# one day (oac.MANY_A_DAY); codes made elsewhere aren't in it.
def _today() -> str:
    return time.strftime("%Y-%m-%d")


def offline_codes_today(lock_id: str) -> int:
    made = (load_config().get("offline_codes_made") or {}).get(lock_id) or {}
    return int(made.get("count") or 0) if made.get("day") == _today() else 0


def count_offline_code(lock_id: str) -> int:
    cfg = load_config()
    made = dict(cfg.get("offline_codes_made") or {})
    n = offline_codes_today(lock_id) + 1
    made[lock_id] = {"day": _today(), "count": n}
    save_config({**cfg, "offline_codes_made": made})
    return n


# The saved credential is the SHA-256 password hash, which the login API takes
# directly — so it is password-equivalent. It goes in the OS keyring (Secret
# Service / Keychain / Credential Locker) when the optional `keyring` package
# has a working backend, else in the owner-only config file.
_KEYRING_SERVICE = "locklyctl"


def _keyring():
    """The keyring module if a real backend is available, else None. None too
    while the app lock is on: secrets then go in the sealed config, which the
    passcode guards, not in the OS keyring, which the desktop session opens."""
    if _keys is not None:
        return None
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # noqa: BLE001 - not installed / broken install
        return None
    try:
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
    except Exception:  # noqa: BLE001
        return None
    return keyring


def load_credential(cfg: dict) -> str | None:
    if cfg.get("pw_in_keyring"):
        kr = _keyring()
        try:
            return kr.get_password(_KEYRING_SERVICE, cfg.get("email", "")) if kr else None
        except Exception:  # noqa: BLE001
            _LOG.warning("could not read the login from the keyring", exc_info=True)
            return None
    return cfg.get("pw_hash")


_KEEP = object()        # _save_credential: keep the private server saved with the login


def saved_server(cfg: dict) -> str | None:
    """The private locklyctl-mqtt server the saved sign-in belongs to, or None
    for Lockly's. A saved credential is only ever sent to this one."""
    return cfg.get("server") or None


def _save_credential(cfg: dict, email: str, pw_hash: str | None, server=_KEEP) -> str:
    """Store (or, with None, forget) the credential and where it signs in:
    ``server`` is a private server's URL, None for Lockly, or left out to keep
    the one saved with it (moving the credential between the keyring and the
    file). A forgotten credential takes its server with it, so the next
    sign-in starts at Lockly. Returns where it went: 'keyring', 'file' or 'none'."""
    old_email = cfg.get("email", "")
    home = saved_server(cfg) if server is _KEEP else server
    home = {"server": home} if pw_hash and home else {}
    # "token" is a session JWT stored by older versions; nothing reads it now.
    cfg = {k: v for k, v in cfg.items() if k not in ("pw_hash", "pw_in_keyring", "token", "server")}
    kr = _keyring()
    if kr:
        for e in {old_email, email} - {""}:          # never leave a stale copy
            try:
                kr.delete_password(_KEYRING_SERVICE, e)
            except Exception:  # noqa: BLE001 - nothing stored
                pass
    if pw_hash and kr:
        try:
            kr.set_password(_KEYRING_SERVICE, email, pw_hash)
            save_config({**cfg, "email": email, "pw_in_keyring": True, **home})
            return "keyring"
        except Exception:  # noqa: BLE001 - locked/unavailable → file fallback
            _LOG.warning("keyring unavailable, storing login in the config file", exc_info=True)
    save_config({**cfg, "email": email, "pw_hash": pw_hash, **home})
    return "file" if pw_hash else "none"
