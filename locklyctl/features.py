"""Frame builders for features traced from the Lockly app but NOT present in
LocklyHA: Welcome Mode (stay-unlocked schedule) and Rain Mode.

These are 🔎 code-traced only — decoded from app 3.3.4, never sent to real
hardware. They are built on the same verified ``_assemble_fields`` / ``_aes_wrap``
primitives as unlock, so the framing is sound; what is unproven is the field
*content* (order, widths, placeholders). The CLI gates them behind an explicit
"experimental" confirmation. See docs: ble/commands/welcome_mode, rain_mode.
"""
from __future__ import annotations

import calendar
import re
import zlib
from datetime import datetime as _dt, timedelta as _td, timezone as _tz

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
    """0x9B set. ``schedules`` is a list (max 8) of {week:int, begin:(h,m),
    end:(h,m)}, optionally with per-slot ``enabled`` (default ``enable``) and
    ``sync`` (0/1; default 0 when ``sync_log``). Writes the full 8-slot table.

    The two bytes after the slots are bitmasks with slot 0 in bit 0: the app
    joins one bit per slot, last slot first, and parses that as binary
    (SyncToggleState.b + Strings.f, app 3.3.4)."""
    enc_mc, mc_len, key = _enc(lock)
    segs, sync_bits, on_bits = [], 0, 0
    for i, s in enumerate(schedules[:8]):
        bh, bm = s["begin"]; eh, em = s["end"]
        segs.append(_segment(s["week"], bh, bm, eh, em))
        if s.get("enabled", enable):
            on_bits |= 1 << i
        if s.get("sync", 0 if sync_log else 1):
            sync_bits |= 1 << i
    segs += [_SEG_INVALID] * (8 - len(segs))
    configurator = f"{8:02X}" + "".join(segs) + f"{sync_bits:02X}" + f"{on_bits:02X}"
    raw = P._assemble_fields("9B", mc_len, enc_mc, _WM_FLAG_SET, _WM_MODE_CUSTOM,
                             configurator, (nonce or "").upper())
    return P._aes_wrap(raw, key)


# ── Keep unlocked for a while (one-time 0x9B slots) ──────────────────────────
# Seen on a Visage (2026-11-12): a one-time slot (week 0x80) with auto entry
# whose window contains the current time opens the door within ~20 s, locks it
# again at the end, and the lock then switches that slot off by itself, so it
# never repeats. One-time slots carry no date, and the lock drops weekday bits
# from them (0x90 was stored and run as 0x80), so a window past midnight needs
# a second one-time slot for tomorrow's part; that one must not contain the
# current time, or it would run straight away instead of tomorrow. Seen on the
# same lock on 2026-11-13: a slot for 00:00-00:02 written at 23:33 left the door locked,
# had it open by 00:01 and locked again by 00:03, and was then switched off.
# Not seen yet: whether the door locks for the minute between 23:59 and 00:00.
SYNC_AUTO_ENTRY = 1          # WelcomeModeRequest.AUTO_ENTRY_WELCOME_MODE: unlocks by itself
SYNC_FIRST_ENTRY = 0         # MANUAL_ENTRY: stays locked until someone opens it once
KEEP_UNLOCKED_PAST_MIDNIGHT = True    # False: windows stop at 23:59 lock time


def plan_keep_unlocked(start, minutes: int) -> list[dict]:
    """The one-time slots that keep a door unlocked from ``start`` (a datetime in
    the lock's time zone; seconds are dropped) for ``minutes``."""
    if not 1 <= minutes < 24 * 60:
        raise ValueError("Choose between 1 minute and 23 hours 59 minutes.")
    begin = start.replace(second=0, microsecond=0)
    end = begin + _td(minutes=minutes)
    if end.date() != begin.date() and (end.hour, end.minute) == (0, 0):
        end -= _td(minutes=1)                   # "until midnight" is until 23:59
    slot = lambda b, e: {"week": WEEK_ONCE, "begin": b, "end": e, "enabled": True,
                         "sync": SYNC_AUTO_ENTRY}
    if end.date() == begin.date():
        return [slot((begin.hour, begin.minute), (end.hour, end.minute))]
    if not KEEP_UNLOCKED_PAST_MIDNIGHT:
        raise ValueError(f"The lock can only do this until 23:59 tonight (its time is "
                         f"{begin:%H:%M}).")
    if (end.hour, end.minute) >= (begin.hour, begin.minute):   # would run today instead
        raise ValueError("Past midnight, the most is just under 24 hours.")
    return [slot((begin.hour, begin.minute), (23, 59)), slot((0, 0), (end.hour, end.minute))]


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
    if week & WEEK_ONCE:                 # the app sends 0x80 alone; day bits are untested
        return "once" + (f" ({describe_week(week & 0x7F)})" if week & 0x7F else "")
    days = [n for i, n in enumerate(_DAY_NAMES) if week & (1 << i)]
    return ",".join(days) or "no days"


def _seg_text(week: int, bh: int, bm: int, eh: int, em: int) -> str:
    return f"{describe_week(week)} {bh:02d}:{bm:02d}–{eh:02d}:{em:02d}"


def parse_welcome8(d: str) -> dict:
    """0A9B payload: status flag mode N N×segment sync toggle (segments unused
    = 00FFFFFFFF). sync and toggle are per-slot bitmasks, indexed by position
    among the valid segments, as the app reads them back
    (EnergySavingWelcomeModeEntity, app 3.3.4). ``enabled`` is whether any
    slot is on (None when the reply has no toggle byte)."""
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
        if bh > 23 or bm > 59 or eh > 23 or em > 59:       # TimeSegment.isValid
            continue
        out["segments"].append({"week": w, "begin": (bh, bm), "end": (eh, em)})
    if len(d) >= pos + 4:
        sync, toggle = int(d[pos:pos + 2], 16), int(d[pos + 2:pos + 4], 16)
        for i, s in enumerate(out["segments"]):
            s["sync"], s["enabled"] = sync >> i & 1, bool(toggle >> i & 1)
        out["enabled"] = toggle != 0
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
        states = {s.get("enabled") for s in w["segments"]}
        if len(states) > 1:                                # some slots on, some off
            return True, "Welcome Mode: " + "; ".join(
                _seg_text(s["week"], *s["begin"], *s["end"]) + (" (on)" if s["enabled"] else " (off)")
                for s in w["segments"])
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


# Locks whose history the app reads from Lockly's event log instead of
# getlkhist: isSupportQueryLockLogByMQTT = isSupportWiFiLowEnergy() &&
# isClientIdNotEmpty() (BluetoothBean.java:3561, app 3.3.4). The app's
# low-energy list (BluetoothBeanManager.java:3629) has more models; only the
# Visage has been read live (2026-11-13), so only it is listed.
EVENT_LOG_MODELS = frozenset({"PGK728WRHK"})


def uses_event_log(lock: dict) -> bool:
    return str(lock.get("mod") or "").upper() in EVENT_LOG_MODELS and bool(lock.get("clientId"))


# ── firmware: which check the app uses (FirmwareUpgradeCheckerRepositoryImpl.b,
# app 3.3.4). Multi-part Wi-Fi locks ask fwver/msh/new and update over MQTT;
# camera locks ask fwver/new; the rest ask lockly/syspara and are updated by
# the phone over Bluetooth. PGD238T joins the multi-part list from firmware
# 1.01.01; it's left out rather than guessed.
FW_MULTI_PART = "multi"
FW_CAMERA = "camera"
FW_SINGLE = "single"
_MULTI_PART_MODELS = {"PGK7SWH", "PGK7SWHK", "PGK728WHK", "PGK728WRHK", "PGD728FN25", "PGK6SWH",
                      "PGD679LEW", "PGK628WH", "PGD628FG25", "PGK728WKL", "PGD728FG25", "PGI301",
                      "PGD7AWG25", "PGD7YWG25"}
_CAMERA_MODELS = {"PGK798HK"}            # isCameraLock: only the model checked live
# isSupportSuotaUpgrade: the Bluetooth chip is flashed by the phone, not over MQTT
_SUOTA_MODELS = {"PGK728WHK", "PGK728WRHK", "PGK7SWHK"}
# Lock types the multi-part check sends as "model" when no live status is at
# hand (seen live: a PGK728WRHK reports 105).
_KNOWN_LOCK_TYPES = {"PGK728WRHK": 105}


def firmware_check_kind(lock: dict) -> str:
    model = str(lock.get("mod") or "").upper()
    if model in _MULTI_PART_MODELS:
        return FW_MULTI_PART
    return FW_CAMERA if model in _CAMERA_MODELS else FW_SINGLE


def bluetooth_chip_needs_phone(lock: dict) -> bool:
    return str(lock.get("mod") or "").upper() in _SUOTA_MODELS


def version_lower(current: str | None, offered: str | None) -> bool:
    """HexUtils.o (app 3.3.4): True when both are set, the same length, and
    ``current`` is lower at the first dot-separated field that differs."""
    if not current or not offered or len(current) != len(offered):
        return False
    cur, new = current.split("."), offered.split(".")
    if not all(p.isdigit() for p in cur + new):
        return False
    for a, b in zip(cur, new):
        if int(a) != int(b):
            return int(a) < int(b)
    return False


def version_at_least(current: str | None, offered: str | None) -> bool:
    """After an install: ``current`` is a real version, comparable with
    ``offered`` the way HexUtils.o compares, and not lower."""
    if not current or not offered or current == "0.00.00" or len(current) != len(offered):
        return False
    cur, new = current.split("."), offered.split(".")
    if len(cur) != len(new) or not all(p.isdigit() for p in cur + new):
        return False
    return [int(p) for p in cur] >= [int(p) for p in new]


# ── firmware install over MQTT (multi-part locks), as app 3.3.4 does it ──────
# One firmwareUpgradeRequest of type MULTIPLE names every part to install; the
# lock fetches the files from downloadUrl and reports progress until "final".
# Sources: FirmwareUpgradeCheckerResponse (parts), GetOtaUpdateBean
# .getNeedUpgradeFirmwareList (which parts), LowWiFiEnergyUpgradeFirmwareArgs
# and UpgradeFirmwareArgs (args), FirmwareUpgradeRequestData (request).
FW_INSTALL_PARTS = (            # part, offer key prefix, args slot, UpgradeFirmwareFlag bit
    ("wifi", "hub", "three", 4),
    ("ble", "ble", "four", 8),
    ("front", "front", "first", 1),
    ("back", "back", "second", 2),
    ("screen", "screen", "five", 16),
)
FW_MIN_BATTERY = 31             # BluetoothBean.isBatteryLow: 30 % or less is refused
FW_STAGES = {"download": "Downloading", "transfer": "Transferring", "install": "Installing",
             "final": "Finished"}


def firmware_reply_parts(reply: dict) -> tuple[list[dict], list[str]]:
    """The parts a fwver/msh/new reply offers, in the app's order, and what's
    wrong with it. A part counts only with a version, URL, file name and size
    (FirmwareUpgradeCheckerResponse); one whose size isn't a number is named
    in the problems instead (the app's JSON parser would reject the reply)."""
    parts, problems = [], []
    for part, prefix, _slot, _bit in FW_INSTALL_PARTS:
        ver, url, name, size = (reply.get(prefix + k) for k in ("Ver", "Url", "Name", "Size"))
        if not (ver and url and name and size is not None):
            continue
        if not str(size).isdigit():
            problems.append(f"the {part} part's size is {size!r}, not a number")
            continue
        parts.append({"part": part, "version": str(ver), "name": str(name), "size": int(size),
                      "crc": reply.get(prefix + "Crc"), "md5": reply.get(prefix + "Md5")})
    return parts, problems


def firmware_versions_on_lock(status: dict) -> dict:
    """Each part's version as the app reads it from a status reply: the front
    board is the main version (HostLockFragment sets it as the lock's version,
    FirmwareUpgradeUtils.getCurrentVersion reads it back); the screen has none."""
    return {"front": status.get("firmware_version"), "back": status.get("sub_version"),
            "wifi": status.get("wifi_version"), "ble": status.get("ble_version")}


def firmware_display_versions(lock: dict, status: dict, kind: str) -> dict:
    """The lock's version for each part an offer can name, to show beside it."""
    on_lock = {**firmware_versions_on_lock(status),
               "main": status.get("firmware_version") or str(lock.get("fwv") or "")}
    if kind != FW_MULTI_PART:
        on_lock.pop("front")            # front = main version is the multi-part app's reading only
    return on_lock


def firmware_parts_to_install(parts: list[dict], on_lock: dict, phone_does_ble: bool) -> list[dict]:
    """The parts the app sends over MQTT: those whose version on the lock is known
    and lower than offered. With SUOTA the Bluetooth chip is left to the phone."""
    return [p for p in parts
            if not (phone_does_ble and p["part"] == "ble")
            and on_lock.get(p["part"]) not in (None, "", "0.00.00")
            and version_lower(on_lock.get(p["part"]), p["version"])]


def firmware_long_version(version: str | None) -> int:
    """FirmwareUpgradeUtils.toLongVersion: drop the dots, add "00", read as hex."""
    return 0 if version is None else int(version.replace(".", "") + "00", 16)


def build_firmware_upgrade(upgrade_id: str, device_id: str, download_url: str,
                           parts: list[dict]) -> dict:
    """The firmwareUpgradeRequest payload for a MULTIPLE upgrade, as
    createLowWiFiEnergyFirmwareUpgradeRequestData builds it (version "0", crc32 0,
    needReboot false, no md5) and Gson writes it (nulls left out). The args CRC
    is the CRC-32 of the offer's CRC text, as Crc32Utils.a computes it."""
    by_part = {p["part"]: p for p in parts}
    args: dict = {"flag": sum(bit for part, _prefix, _slot, bit in FW_INSTALL_PARTS if part in by_part)}
    for part, _prefix, slot, _bit in FW_INSTALL_PARTS:
        p = by_part.get(part, {})
        if p.get("md5") is not None:
            args[f"{slot}_md5"] = p["md5"]
        if p.get("name") is not None:
            args[f"{slot}_file_name"] = p["name"]
        args[f"{slot}_file_size"] = p.get("size", 0)
        args[f"{slot}_file_ver"] = firmware_long_version(p.get("version"))
        if p.get("crc"):
            args[f"{slot}_file_crc32"] = zlib.crc32(p["crc"].encode())
    return {"id": upgrade_id, "deviceId": device_id, "type": "MULTIPLE",
            "downloadUrl": download_url[:-1] if download_url.endswith("/") else download_url,
            "crc32": 0, "version": 0, "needReboot": False, "args": args}


def describe_firmware_progress(payload: dict) -> str:
    """'Installing 40 %' from a firmwareUpgradeProgress payload."""
    stage = str(payload.get("stage") or "")
    label = FW_STAGES.get(stage, stage.capitalize() or "Working")
    progress = payload.get("progress")
    return label if stage == "final" or progress is None else f"{label} {progress} %"


def is_access_user_lock(lock: dict) -> bool:
    return str(lock.get("mod") or "").upper() in ACCESS_USER_MODELS


def lock_tz(lock: dict) -> _tz | None:
    """The lock's UTC offset from qrylknew's ``tz`` ("+8:00"), if readable."""
    m = re.match(r"^\s*([+-])(\d{1,2}):?(\d{2})?\s*$", str(lock.get("tz") or ""))
    if not m:
        return None
    delta = _td(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
    return _tz(-delta if m.group(1) == "-" else delta)


def lock_now(lock: dict) -> _dt:
    """Now as the lock's wall clock should read it (naive), in its own time
    zone when that's known, otherwise this computer's local time."""
    tz = lock_tz(lock)
    return _dt.now(tz).replace(tzinfo=None) if tz else _dt.now()


# A status reply's clock this far from now is worth a warning: scheduled
# users are let in by the lock's clock, so a wrong one can refuse them.
CLOCK_WARN_S = 120


def clock_offset_text(offset_s: int | None) -> str:
    """A clock offset in words: "no valid time", "3 min behind", "5 hours ahead"."""
    if offset_s is None:
        return "no valid time"
    mins = round(abs(offset_s) / 60)
    size = f"{round(mins / 60)} hours" if mins >= 120 else f"{mins} min" if mins else f"{abs(offset_s)} s"
    return f"{size} {'behind' if offset_s < 0 else 'ahead'}"


def lock_clock(lock: dict, status: dict) -> tuple[_dt | None, bool]:
    """The lock's own clock from a status reply (QueryLockStatusCmd reads it
    from data[18:30]) as an aware datetime, and whether the reply carried one.
    (None, True) is a date that can't be real, such as all zeros: a lock that
    has no valid time."""
    raw = str(status.get("lock_clock") or "")
    if len(raw) < 12:
        return None, False
    y, mo, d, h, mi, s = (int(raw[i:i + 2], 16) for i in range(0, 12, 2))
    if not (1 <= mo <= 12 and 1 <= d <= calendar.monthrange(2000 + y, mo)[1]
            and h < 24 and mi < 60 and s < 60):
        return None, True
    return _dt(2000 + y, mo, d, h, mi, s, tzinfo=lock_tz(lock) or _dt.now().astimezone().tzinfo), True



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


# ── Wi-Fi over Bluetooth: the app's Wi-Fi setup screen (WLE), app 3.3.4 ──────
#
# 0xAE ScanWiFiList174Cmd asks the lock which networks it sees; 0x99
# ConfiguratorWiFi153Cmd saves a network (mode 0) or asks how joining it went
# (mode 1, the same fields again: ConfiguratorWiFiDataSource.c). Both carry the
# nonce from a status check. The server, port and heartbeat come from Lockly
# (getHeartbeatTime), as ConfiguratorWiFiRepositoryImpl.f fetches them.

WIFI_RESULT = {0: "saved", 1: "the lock couldn't save it", 2: "the lock couldn't connect to it",
               3: "connected", 4: "wrong Wi-Fi password"}          # ConfiguratorWiFiResponse


def _padded_text_hex(text: str, size: int) -> str:
    """Strings.h: each character as a byte, cut or zero-padded to ``size`` bytes."""
    data = bytes(ord(ch) & 0xFF for ch in text)
    return data[:size].ljust(size, b"\0").hex()


def wifi_password_bytes(lock: dict, status: dict) -> int:
    """32, or 64 on a Visage whose back board is 1.00.73 up to 3.00.00 or
    3.00.03 and later (BluetoothBean.isSupportMaxWifiPassword)."""
    back = status.get("sub_version")
    if str(lock.get("mod") or "").upper() in ("PGK728WRHK", "PGK728WHK", "PGK7SWHK") and back:
        if (version_at_least(back, "1.00.73") and version_lower(back, "3.00.00")) or version_at_least(back, "3.00.03"):
            return 64
    return 32


def build_wifi_scan(lock: dict, nonce: str, position: int = 0xFF) -> str:
    """0xAE: scan time 0, list position (255 starts a scan), then 1
    (ScanWiFiList174Cmd.getRequestData)."""
    enc_mc, mc_len, key = _enc(lock)
    raw = P._assemble_fields("AE", mc_len, enc_mc, "0", f"{position:x}", "1", (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_wifi_scan(d: str) -> dict:
    """0AAE payload: result (00 done, 02 still scanning), total, position, then
    entries [rssi(1) router type(1) SSID] separated by 0A."""
    out = {"result": d[:2], "total": 0, "position": 0, "networks": []}
    if d[:2] != "00" or len(d) < 6:
        return out
    out["total"], out["position"] = int(d[2:4], 16), int(d[4:6], 16)
    body = d[6:].rstrip("0")
    body += "0" * (len(body) % 2)
    entry = ""
    for i in range(0, len(body) + 2, 2):
        b = body[i:i + 2]
        if b in ("0A", "0a", "") and len(entry) >= 4:
            out["networks"].append({"rssi": int(entry[0:2], 16), "router_type": int(entry[2:4], 16),
                                    "ssid": bytes.fromhex(entry[4:]).decode("utf-8", "replace")})
            entry = ""
        elif b:
            entry += b
    return out


def build_wifi_setup(lock: dict, nonce: str, *, ssid: str, password: str, host: str, port: int,
                     heartbeat_min: int, country: str, router_type: int, hidden: bool = False,
                     query: bool = False, password_bytes: int = 32, scan_support: bool = True) -> str:
    """0x99 ConfiguratorWiFi153Cmd.getData. Raises ValueError for a network
    name or password the field can't hold."""
    ssid_hex = ssid.encode("utf-8").hex()
    if len(ssid_hex) > 64:
        raise ValueError("Wi-Fi name is longer than 32 bytes.")
    if len(password) > password_bytes:
        raise ValueError(f"This lock takes Wi-Fi passwords up to {password_bytes} characters.")
    enc_mc, mc_len, key = _enc(lock)
    fields = ["99", mc_len, enc_mc, ssid_hex.ljust(64, "0"), _padded_text_hex(password, password_bytes),
              _padded_text_hex(host, 128), _padded_text_hex(country, 2), _padded_text_hex(str(port), 4),
              "1" if query else "0", f"{(int(scan_support) << 7) | (heartbeat_min & 0x7F):x}"]
    if scan_support:
        fields += [f"{router_type:x}", "1" if hidden else "0"]
    raw = P._assemble_fields(*fields, (nonce or "").upper())
    return P._aes_wrap(raw, key)


def parse_wifi_setup(d: str) -> dict:
    """0A99 payload: 00 then a result code (WIFI_RESULT), and with 3 the lock's
    client id (length-prefixed text); 02 means the lock is still working on it."""
    out = {"result": d[:2], "code": None, "text": "", "client_id": ""}
    if d[:2] == "02":
        out["text"] = "working on it"
    elif d[:2] == "00" and len(d) >= 4:
        out["code"] = int(d[2:4], 16)
        out["text"] = WIFI_RESULT.get(out["code"], f"result {out['code']}")
        if out["code"] == 3 and len(d) >= 6:
            n = int(d[4:6], 16)
            out["client_id"] = bytes.fromhex(d[6:6 + 2 * n]).decode("ascii", "replace")
    else:
        out["text"] = f"the lock refused it ({d[:2] or 'no data'})"
    return out
