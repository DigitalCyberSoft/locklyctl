"""Backups: an account's locks (with their master codes) and the people on
them (with their PINs) in one JSON document, optionally sealed with a
passphrase, to move between Lockly, locklyctl and locklyctl-mqtt servers.

The format is shared with locklyctl-mqtt (its CONTRACT.md section 4, Go
package internal/backup): each reads what the other writes. Sealed means
scrypt, then AES-256-CTR and HMAC-SHA256 (encrypt-then-MAC). Not AES-GCM:
the pure-Python crypto used where pycryptodome isn't has the AES block
cipher but no GCM, and this needs only that plus the standard library.

A backup taken from Lockly reads each access-user lock's people from the lock
itself (0x9F, then 0x76 per person): the slot, PIN and exact schedule a server
pushes back. Lockly's cloud list has the names but no reliable slot numbers,
and pushing a record to the wrong slot could overwrite someone else's.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import struct
from datetime import datetime, timezone

from . import ble as BLE
from . import config
from . import features as F
from . import protocol as P
from . import view as V
from ._crypto import AES
from .transport import LocklyError

FORMAT = "locklyctl-backup"
VERSION = 1
SEAL_N, SEAL_R, SEAL_P = 1 << 15, 8, 1
_KDF = "scrypt"
_CIPHER = "aes-256-ctr+hmac-sha256"
_MAC_LABEL = b"locklyctl-backup/1"
_MAX_SCRYPT_MEM = 256 << 20          # 128 * n * r bytes; Seal uses 32 MiB
_DEVICE_ID = re.compile(r"^[0-9a-f]{24}$")


class BackupError(Exception):
    """A backup can't be read, written or taken."""


class PassphraseNeeded(BackupError):
    def __init__(self) -> None:
        super().__init__("this backup is encrypted: a passphrase is needed")


class WrongPassphrase(BackupError):
    def __init__(self) -> None:
        super().__init__("wrong passphrase, or the backup file was changed")


# ── the sealed envelope ─────────────────────────────────────────────────────

def _params_ok(n, r, p) -> bool:
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (n, r, p)):
        return False
    return (1 << 10 <= n <= 1 << 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
            and 128 * n * r <= _MAX_SCRYPT_MEM)


def _keys(passphrase: str, salt: bytes, n: int, r: int, p: int) -> tuple[bytes, bytes]:
    dk = hashlib.scrypt(passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                        maxmem=_MAX_SCRYPT_MEM + (16 << 20), dklen=64)
    return dk[:32], dk[32:]


def _ctr(key: bytes, iv: bytes, data: bytes) -> bytes:
    """AES-256-CTR: the counter block starts at iv and counts up as a 128-bit
    big-endian number (as Go's cipher.NewCTR does)."""
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


def seal(doc: dict, passphrase: str, *, n: int = SEAL_N, random=os.urandom) -> bytes:
    """Encrypt a backup document with a passphrase."""
    if not passphrase:
        raise BackupError("the passphrase is empty")
    plain = json.dumps(doc, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    salt, iv = random(16), random(16)
    enc_key, mac_key = _keys(passphrase, salt, n, SEAL_R, SEAL_P)
    ct = _ctr(enc_key, iv, plain)
    env = {"kdf": _KDF, "n": n, "r": SEAL_R, "p": SEAL_P, "salt": _b64(salt), "cipher": _CIPHER,
           "iv": _b64(iv), "mac": _b64(_mac(mac_key, n, SEAL_R, SEAL_P, salt, iv, ct)), "data": _b64(ct)}
    return json.dumps({"format": FORMAT, "version": VERSION, "encrypted": env}, indent=2).encode("utf-8")


def plain(doc: dict) -> bytes:
    """A backup document as plain JSON. It holds master codes and PINs."""
    return json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8")


def is_encrypted(data: bytes | str) -> bool:
    try:
        outer = json.loads(data)
    except ValueError:               # not JSON at all: open_backup says so
        return False
    return isinstance(outer, dict) and outer.get("encrypted") is not None


def _check_header(d) -> None:
    if not isinstance(d, dict) or d.get("format") != FORMAT:
        raise BackupError(f"not a locklyctl backup (format {d.get('format') if isinstance(d, dict) else None!r})")
    if d.get("version") != VERSION:
        raise BackupError(f"backup version {d.get('version')} isn't supported (this reads version {VERSION})")


def _unseal(env, passphrase: str) -> bytes:
    if not isinstance(env, dict) or env.get("kdf") != _KDF or env.get("cipher") != _CIPHER:
        raise BackupError("unsupported backup encryption")
    n, r, p = env.get("n"), env.get("r"), env.get("p")
    if not _params_ok(n, r, p):          # before any key work: a hostile file can't ask for 4 GiB
        raise BackupError(f"backup encryption parameters out of range (n={n} r={r} p={p})")
    try:
        salt, iv, tag, ct = (base64.b64decode(env[k], validate=True) for k in ("salt", "iv", "mac", "data"))
    except (KeyError, TypeError, ValueError) as e:
        raise BackupError(f"damaged backup: {e}") from e
    if len(salt) < 16 or len(iv) != 16 or len(tag) != 32:
        raise BackupError("damaged backup: wrong salt, iv or mac length")
    enc_key, mac_key = _keys(passphrase, salt, n, r, p)
    if not hmac.compare_digest(tag, _mac(mac_key, n, r, p, salt, iv, ct)):
        raise WrongPassphrase()
    return _ctr(enc_key, iv, ct)


def open_backup(data: bytes | str, passphrase: str | None = None) -> dict:
    """Read a backup, plain or sealed. A sealed one needs its passphrase."""
    try:
        outer = json.loads(data)
    except ValueError as e:
        raise BackupError(f"not a locklyctl backup (not JSON: {e})") from e
    _check_header(outer)
    if outer.get("encrypted") is None:
        return outer
    if not passphrase:
        raise PassphraseNeeded()
    try:
        doc = json.loads(_unseal(outer["encrypted"], passphrase))
    except ValueError as e:              # decrypted but not JSON: shouldn't pass the MAC
        raise BackupError(f"damaged backup: {e}") from e
    _check_header(doc)
    return doc


def write_new(path: str, data: bytes) -> None:
    """Write a file that must not exist yet, owner-only from the first byte.
    Raises FileExistsError if it does."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


# ── taking a backup ─────────────────────────────────────────────────────────

def _describe_date(date_hex: str, lock: dict) -> dict:
    """dateMode / startAt / endAt / weekMask for an exact 0x73 date. They only
    describe it: the exact date is what a server pushes."""
    b = bytes.fromhex(date_hex)
    if b == b"\xff" * 10:
        return {"dateMode": "permanent"}
    if b[5:] == bytes(5):                # weekly: mask, start H M, end H M, five zeros
        return {"dateMode": "weekly", "weekMask": b[0] & 0x7F}
    tz = F.lock_tz(lock) or timezone.utc
    try:                                 # period: yyMMddHHmm twice, each byte a decimal value
        start = datetime(2000 + b[0], b[1], b[2], b[3], b[4], tzinfo=tz)
        end = datetime(2000 + b[5], b[6], b[7], b[8], b[9], tzinfo=tz)
    except ValueError:                   # datetime is the only validator; the exact date still rules
        return {"dateMode": "permanent"}
    return {"dateMode": "period", "startAt": int(start.timestamp()), "endAt": int(end.timestamp())}


def _people_from_lock(client, lock: dict) -> tuple[list[dict], list[str]]:
    """Everyone on an access-user lock as the lock holds them, named from
    Lockly's cloud. Wakes the lock. Raises LocklyError if the lock can't be read."""
    notes = []
    try:
        cloud = {int(u.get("userAcuId") or 0): u for u in client.list_users(lock) or []}
    except LocklyError as e:
        cloud = {}
        notes.append(f"names weren't read from Lockly ({e}); people are named by their user id")
    people, seen = [], set()
    for entry in client.lock_user_table(lock):
        uid = int(entry.get("user_id") or 0)
        if uid and uid in seen:
            notes.append(f"user {uid} is listed twice by the lock; kept once")
            continue
        seen.add(uid)
        _ack, d = client.exchange_built(lock, lambda n, uid=uid: F.build_access_user_detail(lock, n, user_id=uid))
        det = F.parse_access_user_detail(d) if d else {}
        if det.get("status") != "00" or not det.get("date"):
            raise LocklyError(f"the lock didn't report user {uid}")
        user_type, date, enabled = int(det["user_type"]), det["date"].upper(), True
        if date == F.access_date_suspended().upper():
            enabled = False
            stash = config.get_suspended(lock["ID"], uid)
            if stash:                    # the schedule `suspend` replaced
                user_type, date = int(stash["user_type"]), str(stash["date"]).upper()
            else:
                notes.append(f"user {uid} is suspended and the schedule it had isn't saved on this "
                             "computer, so it stays suspended")
        u = cloud.get(uid) or {}
        name = re.sub(r"\s+", " ", " ".join(str(x) for x in (u.get("name"), u.get("lastName")) if x)).strip()
        people.append({"name": name or f"User {uid}", "pwdId": int(entry.get("pwd_id") or 0), "userId": uid,
                       "adminId": int(det.get("admin_id") or 0), "pin": str(entry.get("pin") or ""),
                       "userType": user_type, "date": date,
                       "multiVerify": int(entry.get("multi_verify") or 0),   # kept exactly, 0 included
                       "enabled": enabled, **_describe_date(date, lock)})
    return people, notes


def from_lockly(client, *, progress=None) -> tuple[dict, list[tuple[bool, str]]]:
    """A backup of a Lockly account: every lock with its master code, in the
    lists' order with hidden ones marked; for access-user locks (Visage,
    Vision) the people on them, read from each lock (wakes it). Returns the
    document and notes as (something was left out, text)."""
    notes: list[tuple[bool, str]] = []
    hidden = V.hidden_ids(client)
    locks = []
    for lk in V.ordered(client):
        label = str(lk.get("na") or lk.get("blename") or lk.get("ID"))
        dev = str(lk.get("ID") or "").lower()
        if not _DEVICE_ID.match(dev) or not lk.get("mc"):
            notes.append((True, f"{label}: left out (no usable device id or master code from Lockly)"))
            continue
        entry = {"deviceId": dev, "mc": str(lk["mc"]), "name": str(lk.get("na") or ""),
                 "bleName": str(lk.get("blename") or ""), "model": str(lk.get("mod") or ""),
                 "tz": str(lk.get("tz") or ""), "fwv": str(lk.get("fwv") or ""),
                 "hidden": lk.get("ID") in hidden, "sortIndex": len(locks) + 1, "people": []}
        if str(lk.get("mod") or "").upper() not in F.ACCESS_USER_MODELS:
            notes.append((False, f"{label}: the lock only; its users aren't access users (0x73), "
                                 "which is all a server pushes"))
        else:
            if progress:
                progress(label)
            try:
                entry["people"], more = _people_from_lock(client, lk)
                notes += [(False, f"{label}: {n}") for n in more]
            except (LocklyError, BLE.BluetoothError) as e:
                notes.append((True, f"{label}: the lock only; its people couldn't be read ({e})"))
        locks.append(entry)
    doc = {"format": FORMAT, "version": VERSION,
           "createdAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "source": {"kind": "lockly", "account": client.email}, "locks": locks}
    return doc, notes


def _server_call(client, endpoint: str, body: dict, what: str) -> dict:
    if not client.server:
        raise BackupError(f"{what} needs a private locklyctl-mqtt server: sign in with --server URL")
    reply = client._post(endpoint, body)
    if str(reply.get("cod")) != "200":
        raise LocklyError(f"{what} failed: {reply.get('msg') or P.describe_cod(reply.get('cod'))}",
                          str(reply.get("cod")))
    return reply


def from_server(client) -> dict:
    """The signed-in account's backup from a locklyctl-mqtt server (no lock is woken)."""
    doc = _server_call(client, "backup/export", {}, "taking the backup").get("backup")
    _check_header(doc)
    return doc


def restore_to_server(client, doc: dict, *, replace: bool) -> dict:
    """Bring a backup into the signed-in account on a locklyctl-mqtt server:
    merged by device id, or with ``replace`` made exactly the backup's."""
    _check_header(doc)
    reply = _server_call(client, "backup/import", {"backup": doc, "replace": bool(replace)}, "restoring")
    keys = ("locksAdded", "locksUpdated", "locksRemoved", "peopleAdded", "peopleUpdated", "peopleRemoved")
    return {**{k: int(reply.get(k) or 0) for k in keys}, "conflicts": list(reply.get("conflicts") or [])}
