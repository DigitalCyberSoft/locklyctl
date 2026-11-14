"""Sealing locklyctl's own stored data with a passcode (the app lock).

With an app passcode set, the config file (the saved sign-in, saved Wi-Fi
passwords, favorites, the door list's order, suspended people's schedules) is
kept encrypted: scrypt turns the passcode into two keys, the JSON is encrypted
with AES-256-CTR and authenticated with HMAC-SHA256 (encrypt-then-MAC), the
construction backups use (backup.py). The keys stay in memory while the app
is unlocked; the passcode itself is not kept.

What this does and doesn't protect is the passcode's strength: scrypt makes
each guess cost time and memory (N below), which stops nobody who copies the
file from trying every 4-digit PIN. A long passcode is what does that.

This module is only the envelope. config.py decides when it is used.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct

from ._crypto import AES

FORMAT = "locklyctl-config"
VERSION = 1
# 64 MiB and about 0.1 s a guess on a 2025 desktop core. Stored in the file,
# so it can be raised later without breaking a sealed config.
N, R, P = 1 << 16, 8, 1
MIN_LENGTH, MAX_LENGTH = 4, 128
_KDF = "scrypt"
_CIPHER = "aes-256-ctr+hmac-sha256"
_MAC_LABEL = b"locklyctl-config/1"
_MAX_SCRYPT_MEM = 256 << 20          # 128 * n * r bytes


class VaultError(Exception):
    """The sealed config can't be read or written."""


class WrongPasscode(VaultError):
    def __init__(self) -> None:
        super().__init__("Wrong passcode.")


def available() -> str:
    """Why an app passcode can't be used here, or "" when it can. It needs
    scrypt, which Python has when it's built with OpenSSL 1.1 or later."""
    return "" if hasattr(hashlib, "scrypt") else "This Python has no scrypt (it isn't built with OpenSSL)."


def check_passcode(passcode: str) -> None:
    """Raise ValueError for a passcode that can't be used."""
    if not isinstance(passcode, str) or len(passcode) < MIN_LENGTH:
        raise ValueError(f"The passcode needs at least {MIN_LENGTH} characters.")
    if len(passcode) > MAX_LENGTH:
        raise ValueError(f"The passcode can be at most {MAX_LENGTH} characters.")


def strength(passcode: str) -> str:
    """"short" for a passcode someone with a copy of the file could find by
    trying them all (digits only and under 10 of them, or under 8 characters);
    else "long". A rule of thumb for the page's advice, not a measurement."""
    if passcode.isdigit():
        return "short" if len(passcode) < 10 else "long"
    return "short" if len(passcode) < 8 else "long"


def _params_ok(n, r, p) -> bool:
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (n, r, p)):
        return False
    return (1 << 10 <= n <= 1 << 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
            and 128 * n * r <= _MAX_SCRYPT_MEM)


def derive(passcode: str, salt: bytes, n: int = N, r: int = R, p: int = P) -> tuple[bytes, bytes]:
    """The encryption key and the MAC key for this passcode and salt."""
    dk = hashlib.scrypt(passcode.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        maxmem=128 * n * r + (16 << 20), dklen=64)
    return dk[:32], dk[32:]


def ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CTR: the counter block starts at iv and counts up as a 128-bit
    big-endian number (as Go's cipher.NewCTR does). Built on the block cipher
    alone, since the pure-Python AES used where pycryptodome isn't has no CTR."""
    if not data:
        return b""
    start = int.from_bytes(iv, "big")
    blocks = b"".join(((start + i) % (1 << 128)).to_bytes(16, "big") for i in range((len(data) + 15) // 16))
    stream = AES.new(key, AES.MODE_ECB).encrypt(blocks)[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")).to_bytes(len(data), "big")


def _mac(key: bytes, n: int, r: int, p: int, salt: bytes, iv: bytes, ct: bytes) -> bytes:
    """Covers the parameters, salt and iv too, so none can be swapped."""
    msg = _MAC_LABEL + b"\x00" + struct.pack(">III", n, r, p) + salt + iv + ct
    return hmac.new(key, msg, hashlib.sha256).digest()


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def is_sealed(data: bytes | str) -> bool:
    """Whether this is a sealed config (rather than the plain JSON one)."""
    try:
        outer = json.loads(data)
    except ValueError:
        return False
    return isinstance(outer, dict) and outer.get("format") == FORMAT and outer.get("encrypted") is not None


def header(data: bytes | str) -> dict:
    """The sealed file's salt and scrypt parameters: {"salt", "n", "r", "p"}.
    Raises VaultError for a file this can't read; the parameters are checked
    before any key work, so a hostile file can't ask for gigabytes."""
    try:
        outer = json.loads(data)
        env = outer["encrypted"]
        if outer.get("format") != FORMAT or outer.get("version") != VERSION:
            raise VaultError(f"The saved data is in a format this version can't read "
                             f"({outer.get('format')!r} version {outer.get('version')!r}).")
        if env.get("kdf") != _KDF or env.get("cipher") != _CIPHER:
            raise VaultError("The saved data is encrypted in a way this version can't read.")
        n, r, p = env.get("n"), env.get("r"), env.get("p")
        if not _params_ok(n, r, p):
            raise VaultError(f"The saved data's encryption parameters are out of range (n={n} r={r} p={p}).")
        salt = base64.b64decode(env["salt"], validate=True)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise VaultError(f"The saved data is damaged: {e}") from e
    if len(salt) < 16:
        raise VaultError("The saved data is damaged: its salt is too short.")
    return {"salt": salt, "n": n, "r": r, "p": p}


def seal(plain: bytes, keys: tuple[bytes, bytes], head: dict, *, random=os.urandom) -> bytes:
    """Encrypt ``plain`` under keys already derived for ``head`` (header's
    shape). Each call takes a fresh iv, so saving often doesn't reuse one."""
    enc_key, mac_key = keys
    salt, n, r, p = head["salt"], head["n"], head["r"], head["p"]
    iv = random(16)
    ct = ctr(enc_key, iv, plain)
    env = {"kdf": _KDF, "n": n, "r": r, "p": p, "salt": _b64(salt), "cipher": _CIPHER,
           "iv": _b64(iv), "mac": _b64(_mac(mac_key, n, r, p, salt, iv, ct)), "data": _b64(ct)}
    return json.dumps({"format": FORMAT, "version": VERSION, "encrypted": env}, indent=2).encode("utf-8")


def unseal(data: bytes | str, keys: tuple[bytes, bytes]) -> bytes:
    """Decrypt a sealed config with keys derived for its header. Raises
    WrongPasscode when the MAC doesn't match: the wrong passcode, or a file
    that was changed."""
    head = header(data)
    enc_key, mac_key = keys
    try:
        env = json.loads(data)["encrypted"]
        iv, tag, ct = (base64.b64decode(env[k], validate=True) for k in ("iv", "mac", "data"))
    except (KeyError, TypeError, ValueError) as e:
        raise VaultError(f"The saved data is damaged: {e}") from e
    if len(iv) != 16 or len(tag) != 32:
        raise VaultError("The saved data is damaged: wrong iv or mac length.")
    if not hmac.compare_digest(tag, _mac(mac_key, head["n"], head["r"], head["p"], head["salt"], iv, ct)):
        raise WrongPasscode()
    return ctr(enc_key, iv, ct)


def new_head(*, random=os.urandom) -> dict:
    """A fresh salt with the current parameters, for a new passcode."""
    return {"salt": random(16), "n": N, "r": R, "p": P}
