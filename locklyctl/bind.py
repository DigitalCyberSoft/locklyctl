"""Setting up a new (or reset) lock from this computer: the frames and the
account record of the LOCKLY app's own setup (app 3.3.4,
common/BindLockManager.java), as LocklyClient.add_lock sends them over
Bluetooth.

The app's steps for a Visage, in order (sources under com/pg/res/):
  1. a status check in the old format with a master code of zeros, to wake
     the lock (BindLockManager.w0; it carries on even without a reply);
  2. 01 01, SetMasterCodeCmd: the Activation Card's 8-digit code and a random
     8 digits; the lock answers with its new master code and device ID,
     masked with the random digits (B0);
  3. 01 04, StartNewMasterCodeCmd: puts that master code in force (C0);
  4. a status check in the old format with the new master code (r0);
  5. initialCode/add on Lockly's server (p0); a normal status check (m0);
  6. the time zone, 0x33, after a status check (F0);
  7. backac: the lock's record on the account (P), then the admin PIN
     (SetPwdActivity, SetHostPwdCmd 01 02) and updateDevice with it.
locklyctl gives the PIN before the record and sends the record once, PIN and
name included. Steps the app takes only for other models (checkDid and the
factory reset it can lead to, the PIN at setup for locks without a screen,
the new eBadge random) don't apply to the Visage; the eBadge manager step
(EbadgeManagerUtil, which the app runs and carries on past whatever it
returns), rooms and access codes aren't ported.

These frames are masked only with the card's code (01 01) or with digits the
same frame carries (the reply), so anyone who records the Bluetooth traffic
of a setup can work out the lock's keys, with the app too: set a lock up
where nobody is listening. The code, master code and PIN are never logged.
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime

from . import protocol as P

# Locks this setup is ported for: Bluetooth name prefix -> lock type the
# status check must report. Other models take other steps (see above).
MODELS = {"PGK728WRHK": 105}

_CARD = re.compile(r"SN:([A-Z0-9]{10,30})\s+AC:(\d{8})")      # VCardUtils.java:46


def card(text: str) -> tuple[str, str]:
    """(serial number or "", 8-digit code) from the Activation Card's QR text
    ("SN:... AC:12345678") or from the 8 digits alone. Raises ValueError."""
    t = text.strip()
    m = _CARD.search(t)
    if m:
        return m.group(1), m.group(2)
    if re.fullmatch(r"\d{8}", t):
        return "", t
    raise ValueError("The Activation Code is the card's 8 digits (or its QR text, SN:... AC:...).")


def model_for(ble_name: str) -> str | None:
    """The model a lock's Bluetooth name says it is, when setup is ported for it."""
    return next((m for m in MODELS if ble_name.upper().startswith(m)), None)


def random_digits() -> str:
    """The 8 random digits for 01 01 (RandomUtils.i(8) in the app)."""
    return "".join(secrets.choice("0123456789") for _ in range(8))


def _digit_bytes(digits: str) -> bytes:
    """HexUtils.d: each digit as one byte ("12" -> 01 02)."""
    return bytes(int(c) for c in digits)


def _xor(data: bytes, key: bytes) -> bytes:
    """HexUtils.i: XOR with the key, repeated."""
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def _frame(masked: bytes, type_byte: int) -> str:
    return P.build_ble_frame(masked, type_byte).hex().upper()


def build_old_status(mc_hex: str, now: datetime) -> str:
    """QueryLockStatusCmd.getData(String, int, String) (:385-396) with type 1:
    09, the master code's length in bytes, the master code, the time; XOR the
    master code's bytes. With "000000" it's the wake-up of step 1 (the mask is
    then all zeros); with P.encrypt_master_code(mc, device ID), step 4."""
    raw = bytes.fromhex(P._assemble_fields("9", f"{len(mc_hex) // 2:d}", mc_hex, P._timestamp_hex(now)))
    return _frame(_xor(raw, bytes.fromhex(mc_hex)), 1)


def old_status_ok(ack_hex: str | None) -> bool:
    """QueryLockStatusCmd.isCommandRight (:646): the reply's type is 0A09 or 0A1E."""
    return bool(ack_hex) and ack_hex[12:16].upper() in ("0A09", "0A1E")


def build_set_master_code(code: str, rnd: str) -> str:
    """01 01, SetMasterCodeCmd.getData (:51-62): 01 01 08, the code's digits,
    the random digits; XOR the code's digits; frame type 3."""
    raw = bytes.fromhex("010108") + _digit_bytes(code) + _digit_bytes(rnd)
    return _frame(_xor(raw, _digit_bytes(code)), 3)


# SetMasterCodeCmd's refusals (raw frame hex [16:18], getErrorType).
NOT_RESET, WRONG_CODE = "F8", ("FF", "FC", "F3")


def parse_set_master_code(ack_hex: str | None, rnd: str) -> dict:
    """The reply to 01 01 (SetMasterCodeCmd.isCommandRight, parseCmd :82-126):
    {"ok": True, "mc", "device_id"}, or {"ok": False, "error"}: "F8" the lock
    isn't reset, "FF"/"FC"/"F3" the code is wrong, "" no usable reply."""
    if not ack_hex:
        return {"ok": False, "error": ""}
    raw = bytes.fromhex(ack_hex)
    if len(raw) >= 20:
        # Lowercase, as the app's hex is (Cmd.byteTo2BitString16, Integer.toHexString)
        # and as the account lists device IDs.
        d = _xor(raw[6:-1], _digit_bytes(rnd)).hex()
        if d.startswith("0a0101") and len(d) >= 46:
            return {"ok": True, "mc": d[6:22][1::2], "device_id": d[22:46]}
    return {"ok": False, "error": ack_hex[16:18].upper() if len(raw) < 20 else ""}


def build_start_master_code(code: str, mc: str, device_id: str) -> str:
    """01 04, StartNewMasterCodeCmd.getData: 01 04 08, the master code's
    digits, the device ID; XOR the code's digits; frame type 3."""
    raw = bytes.fromhex("010408") + _digit_bytes(mc) + bytes.fromhex(device_id)
    return _frame(_xor(raw, _digit_bytes(code)), 3)


def start_master_code_ok(ack_hex: str | None) -> bool:
    """StartNewMasterCodeCmd: an unmasked reply, type 0A0104 and status 00."""
    return bool(ack_hex) and ack_hex[12:18].upper() == "0A0104" and ack_hex[18:20] == "00"


def build_set_admin_pin(lock: dict, nonce: str, pin: str) -> str:
    """SetHostPwdCmd.getData for an AES lock without isSupport75Cmd (the
    Visage): 01 02, the master code's length, the encrypted master code, the
    PIN's digits, the nonce."""
    mc, uuid_ = str(lock["mc"]), lock["ID"]
    enc_mc = P.encrypt_master_code(mc, uuid_)
    raw = P._assemble_fields("1", "2", f"{len(enc_mc) // 2:d}", enc_mc, P._expand_digits(pin), (nonce or "").upper())
    return P._aes_wrap(raw, P.derive_aes_key(mc, uuid_))


def admin_pin_ok(ack_hex: str | None) -> tuple[bool, str]:
    """SetHostPwdCmd's verdict: type 0A0102 is set, and so is refusal FD, which
    the app counts as success (the lock already has that PIN, onErrorCmd)."""
    if not ack_hex:
        return False, "no reply"
    h = ack_hex.upper()
    if h[12:18] == "0A0102":
        return True, "set"
    if h[16:18] == "FD":
        return True, "it already had that PIN"
    return False, f"error {h[16:18]} ({P.describe_ble_error(h[16:18])})"


def account_tz(minutes: int) -> str:
    """A UTC offset the way the account keeps it: "+8:00", "-3:30"."""
    h, m = divmod(abs(minutes), 60)
    return f"{'+' if minutes >= 0 else '-'}{h}:{m:02d}"


def account_record(*, device_id: str, mc: str, pin: str, name: str, model: str, ble_name: str,
                   tz: str, status: dict, serial: str = "") -> dict:
    """The lock's record for backac, with the fields of
    BackupLockBean.getBackupLockBean(MyLockerBean) (:1039-1108) that a Visage
    has at setup, under the names the account lists locks with (qrylknew).
    audio 1 as BindLockManager.h0 sets it; the rest are left to Lockly."""
    return {"ID": device_id, "mc": mc, "hc": pin, "na": name, "tz": tz, "mod": model, "blename": ble_name,
            "fwv": status.get("firmware_version") or "", "lkmver": status.get("firmware_version") or "",
            "lksver": status.get("sub_version") or "", "serialNum": serial, "audio": 1, "pinCrazy": 0,
            "secureLightInd": 0, "powerSavingModel": 0, "paymentType": 0}
