"""Config file, logging and the saved login credential.

Shared by the CLI and the GUI. Everything lives in ~/.locklyctl (owner-only),
or in $LOCKLYCTL_HOME when that is set.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

from .const import CONFIG_DIR_NAME, CONFIG_FILE_NAME

_LOG = logging.getLogger("locklyctl")


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


def load_config() -> dict:
    p = _config_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    """Write the config owner-only from the first byte (it can hold the login
    credential), via a temp file + rename so a crash never truncates it."""
    p = _config_path()
    p.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)       # also tighten a dir made by an older version
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)            # in case tmp pre-existed with wider perms
    os.replace(tmp, p)


# The saved credential is the SHA-256 password hash, which the login API takes
# directly — so it is password-equivalent. It goes in the OS keyring (Secret
# Service / Keychain / Credential Locker) when the optional `keyring` package
# has a working backend, else in the owner-only config file.
_KEYRING_SERVICE = "locklyctl"


def _keyring():
    """The keyring module if a real backend is available, else None."""
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


def _save_credential(cfg: dict, email: str, pw_hash: str | None) -> str:
    """Store (or, with None, forget) the credential. Returns where it went:
    'keyring', 'file' or 'none'."""
    old_email = cfg.get("email", "")
    # "token" is a session JWT stored by older versions; nothing reads it now.
    cfg = {k: v for k, v in cfg.items() if k not in ("pw_hash", "pw_in_keyring", "token")}
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
            save_config({**cfg, "email": email, "pw_in_keyring": True})
            return "keyring"
        except Exception:  # noqa: BLE001 - locked/unavailable → file fallback
            _LOG.warning("keyring unavailable, storing login in the config file", exc_info=True)
    save_config({**cfg, "email": email, "pw_hash": pw_hash})
    return "file" if pw_hash else "none"
