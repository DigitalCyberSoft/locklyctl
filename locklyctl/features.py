"""Frame builders for features traced from the Lockly app but NOT present in
LocklyHA: Welcome Mode (stay-unlocked schedule) and Rain Mode.

These are 🔎 code-traced only — decoded from app 3.3.4, never sent to real
hardware. They are built on the same verified ``_assemble_fields`` / ``_aes_wrap``
primitives as unlock, so the framing is sound; what is unproven is the field
*content* (order, widths, placeholders). The CLI gates them behind an explicit
"experimental" confirmation. See docs: ble/commands/welcome_mode, rain_mode.
"""
from __future__ import annotations

import re
from datetime import timedelta as _td, timezone as _tz

from . import protocol as P

# ── weekday byte ────────────────────────────────────────────────────────────
# bit0=Mon .. bit4=Fri, bit5=Sat, bit6=Sun, bit7=one-time.
WEEK_DAILY = 0x7F
WEEK_WEEKDAYS = 0x1F
WEEK_ONCE = 0x80
_DAY_BITS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def week_byte(days: list[str]) -> int:
    """Build the weekday bitmask from names like ['mon','tue'] (or ['daily'])."""
    days = [d.strip().lower() for d in days if d.strip()]
    if not days or "daily" in days:
        return WEEK_DAILY
    if days == ["weekdays"]:
        return WEEK_WEEKDAYS
    if "once" in days:
        return WEEK_ONCE
    val = 0
    for d in days:
        b = _DAY_BITS.get(d[:3])
        if b is None:
            raise ValueError(f"unknown day: {d}")
        val |= 1 << b
    return val


def parse_hhmm(s: str) -> tuple[int, int]:
    """'8', '8:30', '08:30' → (8, 30). Raises ValueError outside 00:00–23:59."""
    parts = s.strip().split(":")
    if len(parts) > 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"not a time: {s!r} (use HH:MM)")
    h, m = int(parts[0]), int(parts[1]) if len(parts) == 2 else 0
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time out of range: {s!r}")
    return h, m


def _enc(lock: dict) -> tuple[str, str, bytes]:
    mc, uuid_ = str(lock["mc"]), lock["ID"]
    enc_mc = P.encrypt_master_code(mc, uuid_)
    return enc_mc, f"{len(enc_mc) // 2:d}", P.derive_aes_key(mc, uuid_)


# ── Welcome Mode: 8-slot (0x9B) — Visage and other isSupport8Segment models ──
_WM_FLAG_SET = "03"      # set welcome
_WM_FLAG_QUERY = "04"    # query welcome
_WM_MODE_CUSTOM = "01"
_SEG_INVALID = "00FFFFFFFF"


def _segment(week: int, bh: int, bm: int, eh: int, em: int) -> str:
    return f"{week:02X}{bh:02X}{bm:02X}{eh:02X}{em:02X}"


def build_welcome8_set(lock: dict, nonce: str, schedules: list[dict],
                       *, enable: bool = True, sync_log: bool = True) -> str:
    """0x9B set. ``schedules`` is a list (max 8) of
    {week:int, begin:(h,m), end:(h,m)}. Writes the full 8-slot table."""
    enc_mc, mc_len, key = _enc(lock)
    segs = []
    for s in schedules[:8]:
        bh, bm = s["begin"]; eh, em = s["end"]
        segs.append(_segment(s["week"], bh, bm, eh, em))
    segs += [_SEG_INVALID] * (8 - len(segs))
    configurator = (
        f"{8:02X}" + "".join(segs)
        + f"{(0 if sync_log else 1):02X}" + f"{(1 if enable else 0):02X}"
    )
    raw = P._assemble_fields("9B", mc_len, enc_mc, _WM_FLAG_SET, _WM_MODE_CUSTOM,
                             configurator, (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_welcome8_query(lock: dict, nonce: str) -> str:
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("9B", mc_len, enc_mc, _WM_FLAG_QUERY, _WM_MODE_CUSTOM,
                             (nonce or "").upper())
    return P._aes_wrap(raw, key)


# ── Welcome Mode: single-slot (0x46) — AlwaysOpenModeCmd ─────────────────────
# AlwaysOpenModeCmd.getData: HexUtils.c("46", mcLen, encMc, mode, week, start,
# end, nonce) — mode 1 set / 2 query. Verified against app 3.3.4.

def _hhmm_bytes(h: int, m: int) -> str:
    """DataUtils.a(HHmm): each 2-digit decimal pair → one byte (08:30 → 081E)."""
    return f"{h:02X}{m:02X}"


def _welcome46(lock: dict, nonce: str, mode: str, week: str, start: str, end: str) -> str:
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("46", mc_len, enc_mc, mode, week, start, end,
                             (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_welcome46_set(lock: dict, nonce: str, week: int, begin: tuple[int, int],
                        end: tuple[int, int]) -> str:
    """0x46 set a single schedule (start/end in the lock's local time)."""
    return _welcome46(lock, nonce, "1", f"{week:02X}", _hhmm_bytes(*begin), _hhmm_bytes(*end))


def build_welcome46_always_on(lock: dict, nonce: str) -> str:
    """getAlwaysData: every day, start/end FFFF."""
    return _welcome46(lock, nonce, "1", "7F", "FFFF", "FFFF")


def build_welcome46_off(lock: dict, nonce: str) -> str:
    """getCloseData: week 0 with the literal placeholder times 1111 / 1212
    (raw hex, not run through DataUtils.a)."""
    return _welcome46(lock, nonce, "1", "0", "1111", "1212")


def build_welcome46_query(lock: dict, nonce: str) -> str:
    """getQueryData: mode 2 with the app's placeholder week/times."""
    return _welcome46(lock, nonce, "2", "7F", "1111", "1212")


# ── which Welcome Mode form a lock takes ─────────────────────────────────────
# isSupport8SegmentWelcomeMode → 0x9B; isSupportWelcomeMode → 0x46. Partial
# lists from BluetoothBeanManager (docs: ble/commands/welcome_mode); firmware
# minimums are not enforced here. Exact match wins over prefix match because
# e.g. PGD728F (0x46) is a prefix of PGD728FG25 (0x9B).
WELCOME_9B = "9B"
WELCOME_46 = "46"
_WELCOME8_MODELS = {"PGK728WRHK", "PGK7SWH", "PGK7SWHK", "PGK728WHK", "PGK628WH",
                    "PGK798HK", "PGD628FG25", "PGD728FG25", "PGD728FNG25",
                    "PGD728FN21", "PGD238T", "PGD7AWG25", "PGD7YWG25", "PGD238L"}
_WELCOME46_MODELS = {"PGD829AF", "PGD829AFN", "PGD829AFHK", "PGD829KNF",
                     "PGD829FNUS", "PGD829K7F", "PGD829KHT", "PGD678FS", "PGD228FN",
                     "PGI302FC", "PGD7S", "PGD728F", "PGD728FN", "PGD728FNP",
                     "PGD628F", "PGD628FC", "PGD6S",
                     # isSupportWelcomeMode: isPGD7Y && fw ≥ 1.02.35 (lock type 55)
                     "PGD7Y"}


def welcome_form(lock: dict) -> str | None:
    """WELCOME_9B, WELCOME_46, or None when the model isn't in either list."""
    model = str(lock.get("mod") or "").upper()
    if model in _WELCOME8_MODELS:
        return WELCOME_9B
    if model in _WELCOME46_MODELS:
        return WELCOME_46
    best, form = 0, None
    for models, f in ((_WELCOME8_MODELS, WELCOME_9B), (_WELCOME46_MODELS, WELCOME_46)):
        for m in models:
            if model.startswith(m) and len(m) > best:
                best, form = len(m), f
    return form


def welcome_plan(lock: dict, form: str, action: str, week: int = 0,
                 begin: tuple[int, int] = (0, 0), end: tuple[int, int] = (0, 0)):
    """(what, build, needs_nonce) for a Welcome Mode action: 'set', 'off',
    'query', or 'always' (0x46 only). Shared by the menu, the ``welcome``
    command and the GUI. Every form carries the lock's nonce."""
    if form == WELCOME_9B:
        if action == "always":
            raise ValueError("'always' is only available on single-slot (0x46) locks")
        if action == "query":
            return "Welcome Mode query", lambda n: build_welcome8_query(lock, n), True
        sched = [] if action == "off" else [{"week": week, "begin": begin, "end": end}]
        what = "Welcome Mode OFF" if action == "off" else "Welcome Mode schedule"
        return what, lambda n: build_welcome8_set(lock, n, sched, enable=action != "off"), True
    if action == "query":
        return "Welcome Mode query", lambda n: build_welcome46_query(lock, n), True
    if action == "off":
        return "Welcome Mode OFF", lambda n: build_welcome46_off(lock, n), True
    if action == "always":
        return "Welcome Mode ALWAYS ON", lambda n: build_welcome46_always_on(lock, n), True
    return ("Welcome Mode schedule",
            lambda n: build_welcome46_set(lock, n, week, begin, end), True)


# ── Rain Mode (0x56) ─────────────────────────────────────────────────────────

def build_rain_mode_set(lock: dict, nonce: str, on: bool) -> str:
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("56", mc_len, enc_mc, "1", "1" if on else "0",
                             (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_rain_mode_query(lock: dict, nonce: str) -> str:
    enc_mc, mc_len, key = _enc(lock)
    # RainModeActivity queries with a3(2, 0): action 2, state 0.
    raw = P._assemble_fields("56", mc_len, enc_mc, "2", "0", (nonce or "").upper())
    return P._aes_wrap(raw, key)


_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def describe_week(week: int) -> str:
    if week == WEEK_DAILY:
        return "every day"
    if week == WEEK_WEEKDAYS:
        return "Mon–Fri"
    if week & WEEK_ONCE:
        return "once"
    days = [n for i, n in enumerate(_DAY_NAMES) if week & (1 << i)]
    return ",".join(days) or "no days"


def _seg_text(week: int, bh: int, bm: int, eh: int, em: int) -> str:
    return f"{describe_week(week)} {bh:02d}:{bm:02d}–{eh:02d}:{em:02d}"


def parse_welcome8(d: str) -> dict:
    """0A9B payload: status flag mode N N×segment sync toggle (segments unused = 00FFFFFFFF)."""
    out = {"status": d[0:2], "segments": [], "enabled": None}
    if len(d) < 8:
        return out
    n = int(d[6:8], 16)
    pos = 8
    for _ in range(min(n, 8)):
        seg = d[pos:pos + 10]
        pos += 10
        if len(seg) < 10 or seg.upper() == _SEG_INVALID:
            continue
        w, bh, bm, eh, em = (int(seg[i:i + 2], 16) for i in range(0, 10, 2))
        out["segments"].append({"week": w, "begin": (bh, bm), "end": (eh, em)})
    if len(d) >= pos + 4:
        out["enabled"] = d[pos + 2:pos + 4] == "01"
    return out


def check_reply(ack_hex: str | None, lock: dict) -> tuple[bool, str]:
    """(accepted, plain-English detail) for a lock reply, interpreted by the
    command byte in the reply itself. A short, unencrypted frame is the lock
    refusing the command (one error byte)."""
    if not ack_hex:
        return False, "no reply from the lock"
    h = ack_hex.upper()
    cmd = h[14:16]
    d = decrypt_ack(h, lock)
    if d is None:
        code = h[16:18]
        return False, f"lock refused command 0x{cmd}: error {code} — {P.describe_ble_error(code)}"
    if cmd == "9B":
        w = parse_welcome8(d)
        if w["status"] != "00":
            return False, f"lock answered 0x9B with status {w['status']}"
        if not w["segments"]:
            return True, "Welcome Mode: no schedules set"
        state = "on" if w["enabled"] else "off" if w["enabled"] is False else "?"
        return True, (f"Welcome Mode {state}: "
                      + "; ".join(_seg_text(s["week"], *s["begin"], *s["end"]) for s in w["segments"]))
    if cmd == "46":
        w, bh, bm, eh, em = (int(d[i:i + 2], 16) for i in range(0, 10, 2))
        if w == 0:
            return True, "Welcome Mode off"
        if (bh, bm, eh, em) == (0xFF, 0xFF, 0xFF, 0xFF):
            return True, "Welcome Mode always on (stays unlocked)"
        return True, "Welcome Mode: " + _seg_text(w, bh, bm, eh, em)
    if cmd == "56":
        return True, "Rain Mode " + ("on" if d[0:2] == "01" else "off" if d[0:2] == "00" else f"state {d[0:2]}")
    if cmd == "4D":                       # status is the second byte
        return (d[2:4] == "00"), f"delete {'accepted' if d[2:4] == '00' else 'refused, status ' + d[2:4]}"
    if cmd == "92":
        return (d[0:2] == "00"), f"activation {'accepted' if d[0:2] == '00' else 'refused, status ' + d[0:2]}"
    if cmd == "50":
        users = parse_user_list(d)
        return True, (f"{len(users)} user slot(s): " + ", ".join(
            f"#{u['user_no']} ({u['pins']} PIN, {u['fingerprints']} fp, {u['rfids']} card)" for u in users)
            if users else "no user slots on this page")
    if cmd == "4B":
        return True, "PIN accepted for the slot"
    if cmd == "17":
        return True, "capture stopped"
    if cmd == "73":
        r = parse_access_user_reply(d)
        if r["status"] != "00":
            return False, f"lock refused the user change (status {r['status']})"
        return True, ("user saved on the lock" + (f" in slot {r['pwd_id']}" if "pwd_id" in r else ""))
    if cmd == "74":
        return (d[0:2] == "00"), ("credential removed" if d[0:2] == "00" else f"refused, status {d[0:2]}")
    if cmd == "9F":
        r = parse_access_user_list(d)
        return True, f"page {r['page']} of {r['pages']}: {len(r['users'])} of {r['total']} users"
    if cmd == "77":
        r = parse_sensor_list(d)
        return (r["status"] == "00"), f"{len(r['sensors'])} sensor(s)"
    return True, f"lock accepted 0x{cmd} (reply {d[:16]}…)"


def decrypt_ack(ack_hex: str, lock: dict) -> str | None:
    """Decrypted payload hex of an encrypted ACK; None for a short (rejection)
    frame or anything unparseable. For inspecting replies not yet modelled."""
    try:
        payload = bytes.fromhex(ack_hex[16:-2])
        if not payload or len(payload) % 16:
            return None
        key = P.derive_aes_key(str(lock["mc"]), lock["ID"])
        return P.AES.new(key, P.AES.MODE_ECB).decrypt(payload).hex().upper()
    except Exception:  # noqa: BLE001
        return None


def ack_ok(ack_hex: str, lock: dict) -> bool:
    """A generic 'did the lock accept it' check: long (encrypted) ACK whose first
    decrypted byte is 0x00, vs a short 0C-type rejection frame."""
    if not ack_hex:
        return False
    ok, _ = P._ack_reports_success(ack_hex, str(lock["mc"]), lock["ID"])
    return ok


# ── Remote enrolment (user PINs over BLE, fingerprints) ──────────────────────
# Field layouts verified against app 3.3.4 (com.pg.res.data.ble.cmd). Two user
# models exist: classic *slot* locks (e.g. PGD7Y) take 0x4B/0x50/0x4D and
# fingerprints via 0x74; *access-user* locks (Visage PGK728WRHK, Vision
# PGK798HK) take PINs via 0x73 and fingerprints/faces via 0x7D + 0x92.

_SENSOR_FINGERPRINT = 1
_SENSOR_RFID = 2
_SENSOR_FACE = 3
_UNLIMITED = "FFFFFFFFFF"


def _le2(n: int) -> str:
    """getCmdLenString(i): two bytes little-endian, hex (e.g. 5 -> '0500')."""
    return (int(n) & 0xFFFF).to_bytes(2, "little").hex().upper()


def _le4(n: int) -> str:
    """getSensorCode: the integer as four bytes little-endian."""
    return (int(n) & 0xFFFFFFFF).to_bytes(4, "little").hex().upper()


def _user_id(user_id: int, admin_id: int = 0) -> str:
    """Cmd.getUserId(userId, adminId) = intToLittleHex(userId) + intToLittleHex(adminId),
    where intToLittleHex keeps the first two LE bytes — so 2 + 2 bytes."""
    return _le2(user_id) + _le2(admin_id)


def _from_le(h: str) -> int:
    return int.from_bytes(bytes.fromhex(h), "little") if h else 0


def _name20(name: str) -> tuple[str, str]:
    """Return (len_byte_hex, 20-byte name hex). DataUtils.e of a byte[20]."""
    raw = name.encode("utf-8")[:20]
    padded = raw + b"\x00" * (20 - len(raw))
    return f"{len(raw):02X}", padded.hex().upper()


def _time_field(value: str | None) -> str:
    """start/end field: '' or '0' -> unlimited sentinel; else DataUtils.a(value)."""
    if not value or value == "0":
        return _UNLIMITED
    # DataUtils.a: each 2 decimal digits -> 1 hex byte of that decimal value.
    s = "".join(ch for ch in value if ch.isdigit())
    if len(s) % 2:
        s = "0" + s
    return "".join(f"{int(s[i:i+2]):02X}" for i in range(0, len(s), 2))


def build_set_user_pin_cmd(lock: dict, nonce: str, pin: str, user_no: int,
                           name: str, *, start: str | None = None,
                           end: str | None = None) -> str:
    """SetPwdWithUserNoCmd 0x4B — set a PIN for a user slot (permanent by default).

    Field order (HexUtils.c, SetPwdWithUserNoCmd.getData):
      4B mc_len enc_mc d(pin) userNo(2-LE) start end nameLen name(20B) nonce
    """
    enc_mc, mc_len, key = _enc(lock)
    name_len, name_hex = _name20(name)
    raw = P._assemble_fields(
        "4B", mc_len, enc_mc,
        P._expand_digits(str(pin)),
        _le2(user_no),
        _time_field(start), _time_field(end),
        name_len, name_hex,
        (nonce or "").upper(),
    )
    return P._aes_wrap(raw, key)


def parse_set_user_pin_ack(ack_hex: str, lock: dict) -> bool:
    """0A4B with a decrypted first byte of 0x00 == accepted."""
    return ack_ok(ack_hex, lock)


def build_list_users_cmd(lock: dict, nonce: str, page: int = 1) -> str:
    """QueryUserListCmd 0x50 — one page (1–3; the app clamps anything else to 1)."""
    if page not in (1, 2, 3):
        page = 1
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("50", mc_len, enc_mc, f"{page:X}", (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_user_list(d: str) -> list[dict]:
    """0A50 payload: count(1) then count × [userNo(LE2) pins(1) fingerprints(1) rfids(1)]."""
    users = []
    count = int(d[0:2], 16) if len(d) >= 2 else 0
    pos = 2
    for _ in range(count):
        rec = d[pos:pos + 10]
        if len(rec) < 10:
            break
        users.append({"user_no": _from_le(rec[0:4]), "pins": int(rec[4:6], 16),
                      "fingerprints": int(rec[6:8], 16), "rfids": int(rec[8:10], 16)})
        pos += 10
    return users


DELETE_ALL = "4"     # getDeleteAllData: the user and all their credentials
DELETE_PIN = "1"     # getDeletePwdData: only the PIN


def build_delete_user_cmd(lock: dict, nonce: str, user_no: int,
                          kind: str = DELETE_ALL) -> str:
    """DeleteWithUserNoCmd 0x4D: 4D mcLen encMc <kind> cmdLen(0) cmdLen(userNo) nonce.
    (Kinds 2/3 delete one fingerprint/RFID: cmdLen(sensorId) in place of 0.)"""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("4D", mc_len, enc_mc, kind,
                             _le2(0), _le2(user_no), (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_add_fingerprint_cmd(lock: dict, nonce: str, user_id: int, admin_id: int,
                              sensor_code: int, *, user_type: int = 0x24,
                              time_window: str = "FF" * 10) -> str:
    """NewUserSensor125Cmd 0x7D for a fingerprint (access-user locks):
      7d mcLen encMc faceDir(00) userId(2+2) timeWindow(10) userType sensorType(1)
      sensorCode(LE4) nonce
    One frame per finger press; the app repeats it until the reply's status is
    03, then activates with 0x92."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields(
        "7d", mc_len, enc_mc, "00",
        _user_id(user_id, admin_id), time_window,
        f"{user_type:02X}", f"{_SENSOR_FINGERPRINT:X}", _le4(sensor_code),
        (nonce or "").upper(),
    )
    return P._aes_wrap(raw, key)


def build_activate_sensor_cmd(lock: dict, nonce: str, user_id: int, admin_id: int,
                              sensor_id: int, sensor_code: int, *,
                              sensor_type: int = _SENSOR_FINGERPRINT) -> str:
    """UserSensorActivator146Cmd 0x92 — activate a captured fingerprint/face:
      92 mcLen encMc sensorType userId(2+2) sensorId(LE2) sensorCode(LE4) nonce"""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields(
        "92", mc_len, enc_mc, f"{sensor_type:X}",
        _user_id(user_id, admin_id), _le2(sensor_id), _le4(sensor_code),
        (nonce or "").upper(),
    )
    return P._aes_wrap(raw, key)


# ── Access users (Visage PGK728WRHK, Vision PGK798HK) ─────────────────────────
# Verified against app 3.3.4 / Lockly Home 1.6.2. These locks keep one record
# per user: a slot (pwdId, assigned by the lock), user/admin ids (low 16 bits of
# the cloud's userAcuId/adminAcuId), a role byte, a validity window, at most one
# PIN, and fingerprints/faces/cards hanging off it. All of these travel over
# hub/MQTT like unlock.

# AccessUser115 userType byte (UserType.java): bit0 admin, bit1 made by owner,
# bit2 made by admin, bit4 time-limited, bit5 permanent/one-time, bit6 recurring.
UT_OWNER = 0x21
UT_FAMILY = 0x22            # family/staff created by the owner
UT_GUEST_PERIOD = 0x12      # guest with a validity period (owner-created)
UT_GUEST_WEEKLY = 0x42      # recurring weekly guest (owner-created)
UT_GUEST_ONCE = 0x32        # one-time guest (owner-created)
PWD_ID_ANY = 0xFF           # let the lock pick the slot

SENSOR_FINGERPRINT = 1
SENSOR_RFID = 2
SENSOR_FACE = 3


def _dec_bytes(digits: str) -> str:
    """DataUtils.a: each two decimal digits → one byte of that value."""
    return "".join(f"{int(digits[i:i + 2]):02X}" for i in range(0, len(digits), 2))


def access_date_permanent() -> str:
    return "FF" * 10


def access_date_period(start, end) -> str:
    """Validity window: yyMMddHHmm start + end, in the lock's local time
    (``start``/``end`` are datetimes already in the lock's zone)."""
    return _dec_bytes(start.strftime("%y%m%d%H%M")) + _dec_bytes(end.strftime("%y%m%d%H%M"))


def access_date_weekly(week: int, begin: tuple[int, int], end: tuple[int, int]) -> str:
    """Recurring window (CycleDateUtils): week mask (bit0 = Monday), start H M,
    end H M (00:00 end sent as 24:00), then five zero bytes."""
    eh, em = end
    if (eh, em) == (0, 0):
        eh = 24
    return f"{week & 0x7F:02X}{begin[0]:02X}{begin[1]:02X}{eh:02X}{em:02X}" + "00" * 5


# Models whose users are "access users" (BluetoothBeanManager.isSupportAccessUser;
# only the ones confirmed here).
ACCESS_USER_MODELS = {"PGK728WRHK", "PGK798HK"}


# Locks with their own Wi-Fi (reached over Lockly's MQTT channel). Anything
# else needs a hub; with no hub recorded it can only be reached over Bluetooth.
WIFI_MODELS = {"PGK728WRHK", "PGK798HK"}


def remote_route(lock: dict) -> str:
    """How Lockly's cloud reaches this lock: 'hub', 'wifi' or 'bluetooth' (none)."""
    if lock.get("hubid"):
        return "hub"
    if str(lock.get("mod") or "").upper() in WIFI_MODELS:
        return "wifi"
    return "bluetooth"


def is_access_user_lock(lock: dict) -> bool:
    return str(lock.get("mod") or "").upper() in ACCESS_USER_MODELS


def lock_tz(lock: dict) -> _tz | None:
    """The lock's UTC offset from qrylknew's ``tz`` ("+8:00"), if readable."""
    m = re.match(r"^\s*([+-])(\d{1,2}):?(\d{2})?\s*$", str(lock.get("tz") or ""))
    if not m:
        return None
    delta = _td(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return _tz(-delta if m.group(1) == "-" else delta)



def build_access_user_set(lock: dict, nonce: str, *, user_type: int, pwd_id: int,
                          user_id: int, admin_id: int = 0, date: str | None = None,
                          pin: str = "", multi_verify: int = 1) -> str:
    """AccessUser115Cmd 0x73 mode 1 — write/refresh a user record on the lock.
      73 mcLen encMc 01 userType LE2(pwdId) UID(user,admin) date(10)
         hex(len(pin)) d(pin) str(multiVerify) nonce
    An empty ``pin`` removes the PIN but keeps the user (deletePasscodeData)."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields(
        "73", mc_len, enc_mc, "1", f"{user_type:X}", _le2(pwd_id),
        _user_id(user_id, admin_id), date or access_date_permanent(),
        f"{len(pin):X}", P._expand_digits(pin) if pin else "",
        str(int(multi_verify)), (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_access_user_delete(lock: dict, nonce: str, *, user_type: int, pwd_id: int,
                             user_id: int, admin_id: int = 0) -> str:
    """AccessUser115Cmd 0x73 mode 2 — delete a user (and everything on it)."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("73", mc_len, enc_mc, "2", f"{user_type:X}", _le2(pwd_id),
                             _user_id(user_id, admin_id), (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_access_user_reply(d: str) -> dict:
    """0A73 payload: status, then (when byte 1 is 01) type pwdId userId adminId
    start(5) end(5) pinLen pin-digits."""
    out = {"status": d[0:2]}
    if len(d) >= 40 and d[2:4] == "01":
        out.update(user_type=int(d[4:6], 16), pwd_id=_from_le(d[6:10]),
                   user_id=_from_le(d[10:14]), admin_id=_from_le(d[14:18]))
    return out


def build_access_user_list(lock: dict, nonce: str, pos: int = 1, *,
                           caller_user: int = 0, caller_admin: int = 0) -> str:
    """AccessUser159Cmd 0x9F — one page of the lock's user table (read-only).
    The only lock-side list that includes face counts."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("9F", mc_len, enc_mc, f"{pos:X}",
                             _user_id(caller_user, caller_admin), (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_access_user_list(d: str) -> dict:
    """0A9F payload: sum pos total(LE2) count(LE2), then per user infoLen +
      pwdId userId adminId pinLen pin  fp(LE2) rfid(LE2) mv(1) userType(1)
      assocUser(LE2) assocAdmin(LE2) multiAuth(1) hostage(1) face(LE2)
    Field order after the PIN confirmed on a Visage (2026-10-31) against
    the cloud's per-user fingerprint/face counts."""
    out = {"pages": int(d[0:2], 16), "page": int(d[2:4], 16),
           "total": _from_le(d[4:8]), "users": []}
    count = _from_le(d[8:12])
    rest = d[12:]
    for _ in range(count):
        if len(rest) < 2:
            break
        n = int(rest[0:2], 16) * 2
        e, rest = rest[2:2 + n], rest[2 + n:]
        if len(e) < 26:
            break
        o = 14 + int(e[12:14], 16) * 2           # start of the fields after the PIN
        pin = "".join(e[i + 1] for i in range(14, o, 2))
        t = e[o:]
        out["users"].append({
            "pwd_id": _from_le(e[0:4]), "user_id": _from_le(e[4:8]),
            "admin_id": _from_le(e[8:12]), "pin": pin,
            "fingerprints": _from_le(t[0:4]), "cards": _from_le(t[4:8]),
            "multi_verify": int(t[8:10] or "0", 16), "user_type": int(t[10:12] or "0", 16),
            "faces": _from_le(t[24:28]) if len(t) >= 28 else 0})
    return out


def build_access_user_detail(lock: dict, nonce: str, *, user_id: int, admin_id: int = 0) -> str:
    """AccessUserDetail118Cmd 0x76 — one user's record as the lock holds it,
    including the validity window (read-only): 76 mcLen encMc 01 FFFF UID nonce."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("76", mc_len, enc_mc, "1", "FFFF", _user_id(user_id, admin_id),
                             (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_access_user_detail(d: str) -> dict:
    """0A76 payload: status, 1 skipped byte, pwdId userId adminId pinLen pin
    userType date(10) mv fp rfid [face]."""
    out = {"status": d[0:2]}
    if d[0:2] != "00" or len(d) < 36:
        return out
    o = 18 + int(d[16:18], 16) * 2
    out.update(pwd_id=_from_le(d[4:8]), user_id=_from_le(d[8:12]), admin_id=_from_le(d[12:16]),
               user_type=int(d[o:o + 2], 16), date=d[o + 2:o + 22].upper())
    return out


def build_sensor_list(lock: dict, nonce: str, *, pwd_id: int, user_id: int,
                      admin_id: int = 0, sensor_type: int = SENSOR_FINGERPRINT,
                      pos: int = 1) -> str:
    """AccessUserSensorList119Cmd 0x77 — one user's fingerprints/cards/faces (read-only)."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("77", mc_len, enc_mc, f"{pos:X}", _le2(pwd_id),
                             _user_id(user_id, admin_id), f"{sensor_type:X}",
                             (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_sensor_list(d: str) -> dict:
    """0A77 payload: status sum pos pwdId userId adminId type max(LE2) then
    N × [sensorId LE2, sensorCode LE4]."""
    out = {"status": d[0:2], "sensors": []}
    if d[0:2] != "00" or len(d) < 24:
        return out
    out.update(pages=int(d[2:4], 16), page=int(d[4:6], 16), pwd_id=_from_le(d[6:10]),
               sensor_type=int(d[18:20], 16), capacity=_from_le(d[20:24]))
    rest = d[24:]
    while len(rest) >= 12:
        sid, code = _from_le(rest[0:4]), _from_le(rest[4:12])
        rest = rest[12:]
        if code:                  # a zero-code tail entry is padding, not a sensor
            out["sensors"].append({"sensor_id": sid, "code": code})
    return out


def build_delete_sensor(lock: dict, nonce: str, *, pwd_id: int, user_id: int,
                        admin_id: int = 0, sensor_type: int, sensor_code: int) -> str:
    """NewUserSensor116Cmd 0x74 mode 2 — remove one fingerprint/card/face from a user."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("74", mc_len, enc_mc, "2", _le2(pwd_id),
                             _user_id(user_id, admin_id), f"{sensor_type:X}",
                             _le4(sensor_code), (nonce or "").upper())
    return P._aes_wrap(raw, key)


# ── Face-enrolment relay probe (experimental) ────────────────────────────────
# Goal (per the "we only need state-entry + exit" insight): learn whether the
# lock will ENTER face-capture when the start frame is relayed over hub/MQTT,
# and exit cleanly. The STOP frame (0x17) is built exactly from source
# (StopNewUserSensorRepositoryImpl: getData(bean, str(sensorType.type))); the
# START frame (0x7D) has best-effort values for the binding fields the app
# derives from a full AccessUser (time window, user type, sensor code) — every
# guessed field is a named argument so it can be tuned from observed behaviour.

FACE_DIR_FRONT = 1
FACE_DIR_RIGHT = 2
FACE_DIR_LEFT = 4
FACE_DIR_DOWN = 8
FACE_DIR_UP = 16
_SENSOR_TYPE_FACE = 3


def build_stop_sensor_cmd(lock: dict, nonce: str, sensor_type: int = _SENSOR_TYPE_FACE) -> str:
    """StopSensorCmd 0x17 — EXACT. ``HexUtils.c("17", mcLen, enc_mc, <type>, nonce)``
    where <type> is str(sensorType.getType()); 3 == FACE_ID. A stop cannot strand
    the lock, so this is the safe anchor/exit for the probe."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("17", mc_len, enc_mc, f"{sensor_type:X}", (nonce or "").upper())
    return P._aes_wrap(raw, key)


def build_face_start_cmd(lock: dict, nonce: str, *, direction: int = FACE_DIR_FRONT,
                         face_code: int = 0) -> str:
    """A FRONT face capture for the owner, used by the relay probe only to see
    whether the lock enters capture. Same layout as build_face_capture_cmd."""
    return build_face_capture_cmd(lock, nonce, direction, face_code=face_code)


# ── Full face enrolment (0x7D), from the verified trace ──────────────────────
# Field order (NewUserSensor125Cmd.getData):
#   7d mcLen enc_mc  dir  userId(2+2)  timeWindow(10B)  userType  sensorType(3)
#      sensorCode(4B LE faceCode)  nonce
# userId = LE2(userId) ++ LE2(adminId) (Cmd.getUserId); owner enrolling self → both 0.
# timeWindow unlimited = "FF"*10. userType: owner 0x21, family/staff ~0x24.
# Capture set is chosen by lock capability, not by role.

FACE_DIRECTIONS_FULL = [1, 4, 2, 16, 8]   # FRONT, LEFT, RIGHT, UP, DOWN (trace order)
FACE_DIRECTION_ONCE = 128                 # ONLY_ONCE (single-capture locks)
FACE_DIR_NAME = {1: "look straight ahead", 4: "turn LEFT", 2: "turn RIGHT",
                 16: "look UP", 8: "look DOWN", 128: "look at the lock"}
USER_TYPE_OWNER = 0x21
USER_TYPE_FAMILY = 0x24
_TIME_UNLIMITED_10B = "FF" * 10
_SENSOR_TYPE_FACE_ID = 3

# Models with facial recognition at all (BluetoothBean.isSupportFaceRecognition).
# Face enrolment is refused on anything else (e.g. the Vision PGK798HK has no
# face ID). Verified from the app.
FACE_CAPABLE_MODELS = {"PGK728WRHK"}     # isSupportFaceRecognition in app 3.3.4

# Of the face-capable models, those whose enrolment is a single ONLY_ONCE
# capture (isSupportRecordFaceOnlyOnce); the rest use the 5-direction sweep.
_RECORD_ONCE_MODELS = {"PGK728WRHK"}     # firmware-gated in the app; threshold untraced


def supports_face(lock: dict) -> bool:
    model = str(lock.get("mod") or "").upper()
    return any(model.startswith(m) for m in FACE_CAPABLE_MODELS)


def face_directions_for(lock: dict) -> list[int]:
    """The capture set this lock needs (single vs five), by model capability."""
    model = str(lock.get("mod") or "").upper()
    if any(model.startswith(m) for m in _RECORD_ONCE_MODELS):
        return [FACE_DIRECTION_ONCE]
    return list(FACE_DIRECTIONS_FULL)


def build_face_capture_cmd(lock: dict, nonce: str, direction: int, *,
                           user_acu_id: int = 0, admin_id: int = 0,
                           face_code: int = 0, user_type: int = USER_TYPE_OWNER,
                           time_window: str = _TIME_UNLIMITED_10B) -> str:
    """One 0x7D face-capture frame for a given FaceDirection."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields(
        "7d", mc_len, enc_mc,
        f"{direction:X}",
        _user_id(user_acu_id, admin_id),
        time_window,
        f"{user_type:X}",
        f"{_SENSOR_TYPE_FACE_ID:X}",
        _le4(face_code),
        (nonce or "").upper(),
    )
    return P._aes_wrap(raw, key)


def parse_face_capture_ack(ack_hex: str, lock: dict) -> dict:
    """Return {ok, status, done, detail}. done == the lock reported FINAL_STATUS
    '03' (this capture accepted). A short 0C frame is a rejection."""
    if not ack_hex:
        return {"ok": False, "status": None, "done": False, "detail": "no reply from the lock"}
    h = ack_hex.upper()
    try:
        payload = bytes.fromhex(h[16:-2])
        if len(payload) == 0 or len(payload) % 16 != 0:
            code = h[16:18]
            return {"ok": False, "status": code, "done": False,
                    "detail": f"lock rejected it ({code}: {P.describe_ble_error(code)})"}
        d = P.AES.new(P.derive_aes_key(str(lock["mc"]), lock["ID"]),
                      P.AES.MODE_ECB).decrypt(payload).hex().upper()
        status = d[0:2]
        done = status == "03"
        # parseCmd: [4:8] pwdId, [40:44] sensorId, [44:52] sensor code (all LE).
        return {"ok": True, "status": status, "done": done,
                "pwd_id": _from_le(d[4:8]), "sensor_id": _from_le(d[40:44]),
                "sensor_code": _from_le(d[44:52]),
                "detail": f"status={status}" + (" (final/accepted)" if done else " (in progress)")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": None, "done": False, "detail": f"unparseable: {e}"}
