"""Offline structural tests for frame builders — no network, no lock."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from locklyctl import protocol as P
from locklyctl import features as F

MC, UUID, HC = "12345678", "aa0011223344556677889900", "123456"
LOCK = {"mc": MC, "ID": UUID, "hc": HC, "mod": "PGD728F", "hubid": "PGH220X"}


def _set_home(value):
    """Point locklyctl's config dir at ``value``; None restores the default."""
    if value is None:
        os.environ.pop("LOCKLYCTL_HOME", None)
    else:
        os.environ["LOCKLYCTL_HOME"] = value


def _check_frame(hexstr):
    b = bytes.fromhex(hexstr)
    assert b[:4] == bytes([0xA1, 0xB2, 0xC3, 0xD4]), "bad magic"
    length = b[4] + b[5] * 256
    assert length == len(b), f"length field {length} != {len(b)}"
    assert P.crc8_lockly(b[:-1]) == b[-1], "bad CRC"
    return b


def test_status_and_unlock():
    _check_frame(P.build_query_status_cmd(MC, UUID))
    _check_frame(P.build_unlock_cmd(MC, UUID, HC, "00112233445566778899AABBCCDDEEFF"))


def test_welcome_and_rain():
    n = "00112233445566778899AABBCCDDEEFF"
    _check_frame(F.build_welcome8_set(LOCK, n, [{"week": F.WEEK_DAILY, "begin": (8, 0), "end": (18, 30)}]))
    _check_frame(F.build_welcome46_set(LOCK, n, F.WEEK_WEEKDAYS, (8, 0), (18, 0)))
    _check_frame(F.build_rain_mode_set(LOCK, n, True))


def test_enrolment_frames():
    n = "00112233445566778899AABBCCDDEEFF"
    # PIN user 0x4B — check the traced field layout is present in the plaintext
    frame = F.build_set_user_pin_cmd(LOCK, n, "1234", 5, "Jane Doe")
    _check_frame(frame)
    _check_frame(F.build_list_users_cmd(LOCK, n, 0))
    _check_frame(F.build_delete_user_cmd(LOCK, n, 5))
    _check_frame(F.build_add_fingerprint_cmd(LOCK, n, 0, 0, 7))
    _check_frame(F.build_activate_sensor_cmd(LOCK, n, 0, 0, 3, 7))


def test_week_byte():
    assert F.week_byte(["daily"]) == 0x7F
    assert F.week_byte(["weekdays"]) == 0x1F
    assert F.week_byte(["mon", "wed", "fri"]) == 0b0010101


def test_helpers():
    assert F._le2(5) == "0500"
    assert F._name20("AB")[0] == "02"
    assert len(F._name20("AB")[1]) == 40   # 20 bytes
    assert F._time_field(None) == "FFFFFFFFFF"
    assert P._expand_digits("1234") == "01020304"


def test_face_frames():
    n = "00112233445566778899AABBCCDDEEFF"
    VISAGE = {"mc": MC, "ID": UUID, "mod": "PGK728WRHK"}
    # capture frame for each direction a Visage uses (single) + a 5-dir model
    for d in F.face_directions_for(VISAGE):
        _check_frame(F.build_face_capture_cmd(VISAGE, n, d, user_acu_id=42,
                                              admin_id=1, face_code=7,
                                              user_type=F.USER_TYPE_FAMILY))
    _check_frame(F.build_face_start_cmd(VISAGE, n))
    _check_frame(F.build_stop_sensor_cmd(VISAGE, n))
    assert F.supports_face(VISAGE)
    assert not F.supports_face({"mod": "PGK798HK"})   # Vision has no face ID
    assert F.face_directions_for(VISAGE) == [F.FACE_DIRECTION_ONCE]
    assert F.face_directions_for({"mod": "PBL827R"}) == F.FACE_DIRECTIONS_FULL
    assert not F.supports_face({"mod": "PGD7Y"})


def test_face_ack_parse():
    from Crypto.Cipher import AES
    LK = {"mc": MC, "ID": UUID}
    key = P.derive_aes_key(MC, UUID)
    ct = AES.new(key, AES.MODE_ECB).encrypt(bytes.fromhex("03" + "00" * 15))
    pl = bytes.fromhex("0A7D") + ct
    fr = bytes([0xA1, 0xB2, 0xC3, 0xD4, (len(pl) + 8) % 256, (len(pl) + 8) // 256]) + pl
    fr += bytes([P.crc8_lockly(fr)])
    r = F.parse_face_capture_ack(fr.hex(), LK)
    assert r["ok"] and r["done"] and r["status"] == "03", r


def test_welcome_form():
    assert F.welcome_form({"mod": "PGK728WRHK"}) == F.WELCOME_9B     # Visage
    assert F.welcome_form({"mod": "PGD728F"}) == F.WELCOME_46        # Touch
    assert F.welcome_form({"mod": "PGD628F"}) == F.WELCOME_46
    # PGD728F is a prefix of PGD728FG25, which is a 9B model
    assert F.welcome_form({"mod": "PGD728FG25"}) == F.WELCOME_9B
    assert F.welcome_form({"mod": "PGD728FNP-X"}) == F.WELCOME_46    # longest prefix
    assert F.welcome_form({"mod": "PGD7Y"}) == F.WELCOME_46          # the user's "Touch" locks
    assert F.welcome_form({"mod": "PGD7YWG25"}) == F.WELCOME_9B
    assert F.welcome_form({"mod": "ZZZ"}) is None
    assert F.welcome_form({}) is None


def test_welcome8_per_slot_bits():
    """The sync and toggle bytes after the 8 slots are per-slot bitmasks, slot 0
    = bit 0 (SyncToggleState.b + Strings.f in app 3.3.4), both ways."""
    n = "00112233445566778899AABBCCDDEEFF"
    slots = [{"week": F.WEEK_WEEKDAYS, "begin": (8, 0), "end": (18, 0)},
             {"week": F.WEEK_ONCE, "begin": (14, 0), "end": (16, 0), "enabled": False},
             {"week": F.WEEK_DAILY, "begin": (9, 0), "end": (10, 0), "sync": 1}]
    frame = _check_frame(F.build_welcome8_set(LOCK, n, slots))
    plain = P.AES.new(P.derive_aes_key(MC, UUID), P.AES.MODE_ECB).decrypt(frame[6:-2]).hex().upper()
    table = "08" + "1F08001200" + "800E001000" + "7F09000A00" + "00FFFFFFFF" * 5 + "04" + "05"
    assert "0301" + table + n in plain                    # flag 03 set, mode 01 custom
    one = _check_frame(F.build_welcome8_set(LOCK, n, slots[:1]))       # the single-slot case
    plain = P.AES.new(P.derive_aes_key(MC, UUID), P.AES.MODE_ECB).decrypt(one[6:-2]).hex().upper()
    assert "0301" + "08" + "1F08001200" + "00FFFFFFFF" * 7 + "00" + "01" + n in plain
    LK = {"mc": MC, "ID": UUID}
    w = F.parse_welcome8("00" + "04" + "01" + table)
    assert [(s["enabled"], s["sync"]) for s in w["segments"]] == [(True, 0), (False, 0), (True, 1)]
    ok, d = F.check_reply(_reply("9B", "00" + "04" + "01" + table), LK)
    assert ok and d == ("Welcome Mode: Mon–Fri 08:00–18:00 (on); once 14:00–16:00 (off); "
                        "every day 09:00–10:00 (on)"), d
    assert F.describe_week(F.WEEK_ONCE | 0x04) == "once (Wed)"


def test_firmware_offer_shapes():
    """fwver/msh/new, lockly/syspara and a 920 answer, shaped as Lockly sent
    them on 2026-11-12 (versions and file names made up)."""
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    c.des3_key = bytes(range(24))           # any non-degenerate 3DES key
    replies = {
        "fwver/msh/new": {"cod": "200", "verNum": "1.14.40", "title": "New Firmware",
                          "content": "Changes:<br><br>1. Fixed <b>bugs</b>",
                          "frontVer": "1.00.99", "frontUrl": "u", "frontName": "f.bin", "frontSize": "87",
                          "backVer": "1.00.80", "backUrl": "u", "backName": "b.bin", "backSize": "35",
                          "hubVer": "2.02.80", "hubUrl": "u", "hubName": "h.img", "hubSize": "1180",
                          "bleVer": "1.14.30", "bleUrl": "u", "bleName": "l.img", "bleSize": "280",
                          "screenVer": "", "screenUrl": "", "screenName": "", "screenSize": None},
        "lockly/syspara": {"cod": "200", "para": "firmware_ver", "value": "2.02.80", "notify": "N"},
        "fwver/new": {"cod": "920", "msg": "920"},
    }
    sent = {}
    c._post = lambda ep, body, **kw: sent.setdefault(ep, body) and replies[ep]
    o = c.firmware_offer({"ID": UUID, "mod": "PGK728WRHK"})
    assert o["release"] == "1.14.40" and o["notes"] == "Changes:\n1. Fixed bugs"
    assert [(p["part"], p["version"], p["size_kb"]) for p in o["parts"]] == [
        ("front", "1.00.99", 87), ("back", "1.00.80", 35), ("wifi", "2.02.80", 1180), ("ble", "1.14.30", 280)]
    assert sent["fwver/msh/new"]["mod"] == "PGK728WRHK" and "para" in sent["fwver/msh/new"]
    o = c.firmware_offer({"ID": UUID, "mod": "PGD7Y"})
    assert o["kind"] == F.FW_SINGLE and o["parts"] == [{"part": "main", "version": "2.02.80", "size_kb": None}]
    assert sent["lockly/syspara"]["para"] == "firmware_ver" and sent["lockly/syspara"]["did"] == UUID
    assert c.firmware_offer({"ID": UUID, "mod": "PGK798HK"})["release"] is None       # 920: nothing newer
    assert F.version_lower("1.00.72", "1.00.76") and not F.version_lower("1.14.24", "1.00.95")
    assert not F.version_lower("3.2.02.02", "2.02.72")                               # lengths differ


def test_plan_keep_unlocked():
    from datetime import datetime
    at = lambda h, m: datetime(2026, 11, 12, h, m, 30)
    slot = lambda b, e: {"week": 0x80, "begin": b, "end": e, "enabled": True, "sync": F.SYNC_AUTO_ENTRY}
    assert F.plan_keep_unlocked(at(14, 5), 60) == [slot((14, 5), (15, 5))]
    assert F.plan_keep_unlocked(at(23, 0), 60) == [slot((23, 0), (23, 59))]       # until midnight
    def refused(*args):
        try:
            F.plan_keep_unlocked(*args)
        except ValueError:
            return True
        return False
    old = F.KEEP_UNLOCKED_PAST_MIDNIGHT
    try:
        F.KEEP_UNLOCKED_PAST_MIDNIGHT = False                # windows stop at 23:59
        assert refused(at(20, 0), 600) and refused(at(14, 0), 0) and refused(at(14, 0), 24 * 60)
        F.KEEP_UNLOCKED_PAST_MIDNIGHT = True
        assert refused(at(14, 0), 0) and refused(at(14, 0), 24 * 60)
        assert F.plan_keep_unlocked(at(20, 0), 600) == [slot((20, 0), (23, 59)), slot((0, 0), (6, 0))]
        assert F.plan_keep_unlocked(at(20, 0), 1439)[-1]["end"] == (19, 59)
    finally:
        F.KEEP_UNLOCKED_PAST_MIDNIGHT = old


def test_format_version():
    assert P.format_version("00241401") == "1.14.24"      # BCD, bytes 3.2.1
    assert P.format_version("00950001") == "1.00.95"
    assert P.format_version("00720202") == "2.02.72"


def test_status_retries_only_fast_failures():
    """A lock the broker calls offline (3005) or a hub relay timeout (942) is
    asked again, twice at most; a 20 s timeout or a refused hub (930) is not."""
    from locklyctl.transport import LocklyClient
    for mqtt_code, cod, calls in (("3005", None, 3), (None, "942", 3), ("timeout", None, 1),
                                  (None, "930", 1)):
        c = LocklyClient("a@b")
        c.ASLEEP_RETRY_DELAYS = (0, 0)
        sent = []

        def fake(lock, frame, directive=None, c=c, sent=sent, mqtt_code=mqtt_code, cod=cod):
            sent.append(frame)
            c._tl.mqtt_code, c._tl.last_cod = mqtt_code, cod
            return None
        c.exchange_frame = fake
        assert c.query_status(LOCK) is None and c.status_cache[UUID]["reachable"] is False
        assert len(sent) == calls, (mqtt_code, cod, len(sent))


def test_welcome_off_frames():
    n = "00112233445566778899AABBCCDDEEFF"
    _check_frame(F.build_welcome46_off(LOCK, n))
    _check_frame(F.build_welcome46_always_on(LOCK, n))
    _check_frame(F.build_welcome46_query(LOCK, n))
    _check_frame(F.build_rain_mode_query(LOCK, n))
    _check_frame(F.build_welcome8_set(LOCK, n, [], enable=False))


def test_remote_route():
    assert F.remote_route({"mod": "PGD7Y", "hubid": "PGH200X"}) == "hub"
    assert F.remote_route({"mod": "PGD7Y"}) == "bluetooth"
    assert F.remote_route({"mod": "PGK728WRHK"}) == "wifi"


def test_parse_hhmm():
    assert F.parse_hhmm("8") == (8, 0)
    assert F.parse_hhmm("08:30") == (8, 30)
    assert F.parse_hhmm(" 23:59 ") == (23, 59)
    for bad in ("", "24:00", "12:60", "8:3:0", "ab", "-1:00", "8:"):
        try:
            F.parse_hhmm(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad!r}")


def test_week_byte_whitespace():
    assert F.week_byte([" Mon", "WED "]) == 0b0000101
    assert F.week_byte([" weekdays "]) == 0x1F


def test_cli_helpers():
    from locklyctl import cli
    assert cli._pad("\033[32mok\033[0m", 5).endswith("ok\033[0m   ")
    assert cli._fmt_event_time("not a time") == "not a time"
    assert cli._fmt_event_time(42) == "42"
    t = cli._fmt_event_time(1_700_000_000_000)          # ms
    assert t == cli._fmt_event_time(1_700_000_000) and len(t) == 16


def test_save_config_permissions():
    import tempfile
    from pathlib import Path
    from locklyctl import config
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("LOCKLYCTL_HOME")
        _set_home(d)
        try:
            config.save_config({"email": "a@b", "pw_hash": "x"})
            p = Path(d) / "config.json"
            if os.name == "posix":              # Windows has no Unix modes
                assert p.stat().st_mode & 0o777 == 0o600
                assert p.parent.stat().st_mode & 0o777 == 0o700
            assert config.load_config()["pw_hash"] == "x"
        finally:
            _set_home(old)


def _jwt(exp):
    import base64, json
    b = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"h.{b}.s"


def test_session_renewal():
    import time
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    calls = []
    c.login_with_hash = lambda h: (calls.append("login"), setattr(c, "jwt", _jwt(time.time() + 86400)))
    c.load_devices = lambda: calls.append("devices")
    c.pw_hash = "h"
    c.jwt = _jwt(time.time() + 86400)                   # fresh: nothing happens
    c._ensure_session()
    assert calls == []
    c.jwt = _jwt(time.time() + 10)                      # about to expire: renew
    c._ensure_session()
    assert calls == ["login", "devices"]
    c.jwt, c.pw_hash = _jwt(time.time() + 10), None     # no stored hash: leave it
    c._ensure_session()
    assert calls == ["login", "devices"]


def test_transport_trail_is_per_thread():
    import threading
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    c._tl.trail = ["main"]
    seen = []
    t = threading.Thread(target=lambda: seen.append(c.last_transport_trail()))
    t.start(); t.join()
    assert seen == ["(none)"] and c.last_transport_trail() == "main"


def test_decrypt_ack():
    from Crypto.Cipher import AES
    LK = {"mc": MC, "ID": UUID}
    ct = AES.new(P.derive_aes_key(MC, UUID), AES.MODE_ECB).encrypt(bytes(range(16)))
    pl = bytes.fromhex("0A50") + ct
    fr = bytes([0xA1, 0xB2, 0xC3, 0xD4, len(pl) + 7, 0]) + pl
    fr += bytes([P.crc8_lockly(fr)])
    assert F.decrypt_ack(fr.hex(), LK) == bytes(range(16)).hex().upper()
    assert F.decrypt_ack("A1B2C3D409000C0B00", LK) is None       # short rejection


def test_welcome_plan():
    n = "00112233445566778899AABBCCDDEEFF"
    for form, action, nonce in ((F.WELCOME_9B, "set", True), (F.WELCOME_9B, "off", True),
                                (F.WELCOME_9B, "query", True), (F.WELCOME_46, "query", True),
                                (F.WELCOME_46, "set", True), (F.WELCOME_46, "off", True),
                                (F.WELCOME_46, "always", True)):
        what, build, needs_nonce = F.welcome_plan(LOCK, form, action, 0x1F, (8, 0), (18, 0))
        assert needs_nonce is nonce, (form, action)
        _check_frame(build(n))
    try:
        F.welcome_plan(LOCK, F.WELCOME_9B, "always")
    except ValueError:
        pass
    else:
        raise AssertionError("9B 'always' accepted")


def test_find_lock():
    from locklyctl.commands import find_lock, UsageError
    locks = [{"ID": "AA11", "na": "Front Door"}, {"ID": "BB22", "na": "Front Gate"},
             {"ID": "CC33", "na": "Office"}]
    assert find_lock(locks, "office")["ID"] == "CC33"
    assert find_lock(locks, "OFF")["ID"] == "CC33"            # unique prefix
    assert find_lock(locks, "bb22")["ID"] == "BB22"           # by ID
    assert find_lock(locks, "front door")["ID"] == "AA11"
    for bad in ("front", "garage"):                           # ambiguous / missing
        try:
            find_lock(locks, bad)
        except UsageError:
            continue
        raise AssertionError(bad)


def test_subcommand_parsing():
    import argparse
    from locklyctl import commands
    p = argparse.ArgumentParser()
    commands.add_subcommands(p)
    a = p.parse_args(["welcome", "Office", "set", "--days", "weekdays", "--start", "9"])
    assert (a.command, a.action, a.days, a.start, a.send) == ("welcome", "set", "weekdays", "9", False)
    a = p.parse_args(["unlock", "Office", "-y"])
    assert a.yes
    assert p.parse_args([]).command is None


def test_unlock_needs_yes_without_tty():
    import io
    from locklyctl import commands
    class A: lock = "Office"; yes = False
    class Client:
        locks = [{"ID": "CC33", "na": "Office"}]
        def unlock(self, lock): raise AssertionError("must not unlock")
    old = sys.stdin
    sys.stdin = io.StringIO("")
    try:
        commands._unlock(Client(), A())
    except commands.UsageError:
        pass
    else:
        raise AssertionError("unlocked without --yes")
    finally:
        sys.stdin = old


def test_dry_runs_do_not_wake_the_lock():
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    def boom(*a, **k): raise AssertionError("dry run touched the lock")
    c._nonce_and_caps = c.exchange_frame = boom
    for ok, detail in (c.set_user_pin_ble(LOCK, "1234", 5, "Jane", dry_run=True),
                       c.delete_user_ble(LOCK, 5, dry_run=True),
                       c.fingerprint_capture_ble(LOCK, 0, 0, 7, dry_run=True),
                       c.stop_face_enrolment(LOCK, dry_run=True),
                       c.start_face_enrolment(LOCK, dry_run=True)):
        assert ok and "dry-run" in detail


def test_credential_storage():
    import tempfile
    from locklyctl import config
    class FakeKeyring:
        def __init__(self): self.store = {}
        def set_password(self, s, u, p): self.store[(s, u)] = p
        def get_password(self, s, u): return self.store.get((s, u))
        def delete_password(self, s, u): self.store.pop((s, u))
    kr = FakeKeyring()
    real = config._keyring
    with tempfile.TemporaryDirectory() as d:
        old = os.environ.get("LOCKLYCTL_HOME")
        _set_home(d)
        try:
            config._keyring = lambda: None                       # no keyring → file
            assert config._save_credential({}, "a@b", "H1") == "file"
            assert config.load_credential(config.load_config()) == "H1"
            config._keyring = lambda: kr                         # keyring → not in file
            assert config._save_credential(config.load_config(), "a@b", "H2") == "keyring"
            cfg = config.load_config()
            assert "pw_hash" not in cfg and cfg["pw_in_keyring"]
            assert config.load_credential(cfg) == "H2"
            assert config._save_credential(cfg, "a@b", None) == "none"   # logout
            assert kr.store == {} and config.load_credential(config.load_config()) is None
        finally:
            config._keyring = real
            _set_home(old)


def _plain(cmd_hex, mc=MC, uuid=UUID):
    """Decrypted payload of a frame we built (to check field layout)."""
    from Crypto.Cipher import AES
    b = bytes.fromhex(cmd_hex)
    return AES.new(P.derive_aes_key(mc, uuid), AES.MODE_ECB).decrypt(b[6:-2]).hex().upper()  # head(4) len(2) … type(1) crc(1)


def test_welcome46_layout_matches_app():
    n = "00112233445566778899AABBCCDDEEFF"
    enc = P.encrypt_master_code(MC, UUID)
    head = "46" + f"{len(enc) // 2:02d}" + enc.upper()
    # AlwaysOpenModeCmd: mode week start end nonce; off = 01 00 1111 1212
    assert _plain(F.build_welcome46_off(LOCK, n)).startswith(head + "01" + "00" + "1111" + "1212" + n)
    assert _plain(F.build_welcome46_set(LOCK, n, 0x1F, (8, 30), (17, 5))).startswith(
        head + "01" + "1F" + "081E" + "1105" + n)
    assert _plain(F.build_welcome46_query(LOCK, n)).startswith(head + "02" + "7F" + "1111" + "1212" + n)
    assert _plain(F.build_rain_mode_query(LOCK, n)).startswith("56" + f"{len(enc) // 2:02d}" + enc.upper() + "02" + "00" + n)


def _reply(cmd, plain_hex, mc=MC, uuid=UUID):
    from Crypto.Cipher import AES
    pt = bytes.fromhex(plain_hex)
    pt += b"\x00" * (-len(pt) % 16)
    pl = bytes.fromhex("0A" + cmd) + AES.new(P.derive_aes_key(mc, uuid), AES.MODE_ECB).encrypt(pt)
    fr = bytes([0xA1, 0xB2, 0xC3, 0xD4, (len(pl) + 7) % 256, (len(pl) + 7) // 256]) + pl
    return (fr + bytes([P.crc8_lockly(fr)])).hex().upper()


def test_check_reply():
    LK = {"mc": MC, "ID": UUID}
    seg = "1F" + "0800" + "1200"
    ok, d = F.check_reply(_reply("9B", "00" + "04" + "01" + "08" + seg + "00FFFFFFFF" * 7 + "00" + "01"), LK)
    assert ok and d == "Welcome Mode on: Mon–Fri 08:00–18:00", d
    ok, d = F.check_reply(_reply("9B", "00" + "04" + "01" + "08" + "00FFFFFFFF" * 8 + "00" + "00"), LK)
    assert ok and d == "Welcome Mode: no schedules set", d
    assert F.check_reply(_reply("9B", "05"), LK)[0] is False
    assert F.check_reply(_reply("46", "00" + "11111212"), LK) == (True, "Welcome Mode off")
    assert F.check_reply(_reply("46", "7FFFFFFFFF"), LK)[1].startswith("Welcome Mode always on")
    assert F.check_reply(_reply("46", "41" + "0900" + "0A1E"), LK) == (True, "Welcome Mode: Mon,Sun 09:00–10:30")
    assert F.check_reply(_reply("56", "01"), LK) == (True, "Rain Mode on")
    rejected = "A1B2C3D409000C5602"
    ok, d = F.check_reply(rejected + f"{P.crc8_lockly(bytes.fromhex(rejected)):02X}", LK)
    assert not ok and "0x56" in d and "error 02" in d, d
    assert F.check_reply(None, LK) == (False, "no reply from the lock")


def test_enrolment_layouts_match_app():
    n = "00112233445566778899AABBCCDDEEFF"
    enc = P.encrypt_master_code(MC, UUID).upper()
    pre = lambda cmd: cmd + f"{len(enc) // 2:02d}" + enc
    # DeleteWithUserNoCmd.getDeleteAllData: kind 4, cmdLen(0), cmdLen(userNo)
    assert _plain(F.build_delete_user_cmd(LOCK, n, 5)).startswith(pre("4D") + "04" + "0000" + "0500" + n)
    assert _plain(F.build_delete_user_cmd(LOCK, n, 5, F.DELETE_PIN)).startswith(pre("4D") + "01" + "0000" + "0500" + n)
    # QueryUserListCmd: page 1..3, anything else → 1
    assert _plain(F.build_list_users_cmd(LOCK, n, 0)).startswith(pre("50") + "01" + n)
    assert _plain(F.build_list_users_cmd(LOCK, n, 3)).startswith(pre("50") + "03" + n)
    # getUserId = LE2(user) + LE2(admin); time window 10 bytes; sensorCode LE4
    fp = _plain(F.build_add_fingerprint_cmd(LOCK, n, 42, 1, 7))
    assert fp.startswith(pre("7D") + "00" + "2A000100" + "FF" * 10 + "24" + "01" + "07000000" + n), fp
    face = _plain(F.build_face_capture_cmd(LOCK, n, F.FACE_DIRECTION_ONCE, user_acu_id=42, admin_id=1, face_code=7))
    assert face.startswith(pre("7D") + "80" + "2A000100" + "FF" * 10 + "21" + "03" + "07000000" + n), face
    # UserSensorActivator146Cmd: type userId sensorId(LE2) sensorCode(LE4) nonce
    act = _plain(F.build_activate_sensor_cmd(LOCK, n, 42, 1, 3, 7, sensor_type=3))
    assert act.startswith(pre("92") + "03" + "2A000100" + "0300" + "07000000" + n), act


def test_user_list_and_enrolment_replies():
    LK = {"mc": MC, "ID": UUID}
    assert F.parse_user_list("02" + "0100" + "010100" + "0500" + "010000" + "00") == [
        {"user_no": 1, "pins": 1, "fingerprints": 1, "rfids": 0},
        {"user_no": 5, "pins": 1, "fingerprints": 0, "rfids": 0}]
    ok, d = F.check_reply(_reply("50", "01" + "0500" + "010000" + "00"), LK)
    assert ok and d.startswith("1 user slot(s): #5"), d
    assert F.check_reply(_reply("4D", "00" + "00"), LK) == (True, "delete accepted")
    assert F.check_reply(_reply("4D", "00" + "07"), LK)[0] is False
    assert F.check_reply(_reply("92", "00"), LK) == (True, "activation accepted")
    r = F.parse_face_capture_ack(_reply("7D", "03" + "00" + "0900" + "0" * 32 + "0300" + "07000000"), LK)
    assert r["done"] and r["pwd_id"] == 9 and r["sensor_id"] == 3 and r["sensor_code"] == 7, r


def test_access_user_layouts_match_app():
    from datetime import datetime
    n = "00112233445566778899AABBCCDDEEFF"
    enc = P.encrypt_master_code(MC, UUID).upper()
    pre = lambda cmd: cmd + f"{len(enc) // 2:02d}" + enc
    # 0x73 mode 1: 01 type LE2(pwdId) UID date(10) len(pin) d(pin) str(mv) nonce
    f = _plain(F.build_access_user_set(LOCK, n, user_type=F.UT_FAMILY, pwd_id=F.PWD_ID_ANY,
                                       user_id=7, pin="2468"))
    assert f.startswith(pre("73") + "01" + "22" + "FF00" + "07000000" + "FF" * 10
                        + "04" + "02040608" + "01" + n), f
    # empty PIN → length 00, digits dropped (deletePasscodeData)
    f = _plain(F.build_access_user_set(LOCK, n, user_type=F.UT_FAMILY, pwd_id=3, user_id=7))
    assert f.startswith(pre("73") + "01" + "22" + "0300" + "07000000" + "FF" * 10 + "00" + "01" + n), f
    f = _plain(F.build_access_user_delete(LOCK, n, user_type=F.UT_FAMILY, pwd_id=3, user_id=7))
    assert f.startswith(pre("73") + "02" + "22" + "0300" + "07000000" + n), f
    # validity window: yyMMddHHmm as decimal-valued bytes; weekly: mask H M H M + 5 zero bytes
    assert F.access_date_period(datetime(2026, 10, 31, 14, 5), datetime(2026, 11, 2, 9, 0)) == \
        "1A0A1F0E05" + "1A0B020900"
    assert F.access_date_weekly(31, (9, 0), (17, 59)) == "1F0900113B" + "00" * 5
    assert F.access_date_weekly(127, (0, 0), (0, 0)) == "7F00001800" + "00" * 5
    assert _plain(F.build_access_user_detail(LOCK, n, user_id=7)).startswith(pre("76") + "01" + "FFFF" + "07000000" + n)
    det = F.parse_access_user_detail("00" + "01" + "0300" + "0700" + "0000" + "02" + "0102" + "42"
                                     + "400300033B" + "0000000000" + "01" + "0000" + "0000")
    assert (det["pwd_id"], det["user_id"], det["user_type"], det["date"]) == (3, 7, 0x42, "400300033B0000000000"), det
    # read-only lists and sensor delete
    assert _plain(F.build_access_user_list(LOCK, n, 2)).startswith(pre("9F") + "02" + "00000000" + n)
    assert _plain(F.build_sensor_list(LOCK, n, pwd_id=3, user_id=7, sensor_type=F.SENSOR_FACE)).startswith(
        pre("77") + "01" + "0300" + "07000000" + "03" + n)
    assert _plain(F.build_delete_sensor(LOCK, n, pwd_id=3, user_id=7, sensor_type=F.SENSOR_FINGERPRINT,
                                        sensor_code=9)).startswith(
        pre("74") + "02" + "0300" + "07000000" + "01" + "09000000" + n)


def test_access_user_parsers():
    LK = {"mc": MC, "ID": UUID}
    # one user: pwdId 3, userId 7, admin 0, PIN 2468, fp 2, card 0, mv 1, type 0x22, …, face 1
    # (field order as a real Visage sends it)
    entry = "0300" + "0700" + "0000" + "04" + "02040608" + "0200" + "0000" + "01" + "22" + "FFFF" + "FFFF" + "00" + "00" + "0100"
    page = "01" + "01" + "0100" + "0100" + f"{len(entry) // 2:02X}" + entry
    r = F.parse_access_user_list(page)
    assert r["total"] == 1 and r["users"] == [{"pwd_id": 3, "user_id": 7, "admin_id": 0, "pin": "2468",
                                              "fingerprints": 2, "faces": 1, "cards": 0,
                                              "multi_verify": 1, "user_type": 0x22}], r
    ok, d = F.check_reply(_reply("9F", page), LK)
    assert ok and d == "page 1 of 1: 1 of 1 users", d
    sens = F.parse_sensor_list("00" + "01" + "01" + "0300" + "0700" + "0000" + "03" + "0A00"
                               + "0100" + "2A000000" + "000000000000")
    assert sens["sensors"] == [{"sensor_id": 1, "code": 42}] and sens["capacity"] == 10, sens
    ok, d = F.check_reply(_reply("73", "00" + "01" + "22" + "0500" + "0700" + "0000" + "FF" * 10 + "00"), LK)
    assert ok and d == "user saved on the lock in slot 5", d
    assert F.check_reply(_reply("73", "05"), LK)[0] is False
    assert F.check_reply(_reply("74", "00"), LK) == (True, "credential removed")
    # a no-PIN guest record exactly as a Visage returned it
    real = "0500" + "1900" + "0000" + "00" + "000000000042FFFFFFFF00000000"
    u = F.parse_access_user_list("01" + "01" + "0100" + "0100" + f"{len(real) // 2:02X}" + real)["users"][0]
    assert (u["pwd_id"], u["user_id"], u["pin"], u["fingerprints"], u["faces"], u["user_type"]) == (5, 25, "", 0, 0, 0x42), u


FW_REPLY = {   # fwver/msh/new, made-up files; the screen entry has no URL, so it isn't a part
    "cod": "200", "verNum": "1.14.34", "downloadUrl": "https://files.example.com/fw/",
    "hubVer": "2.02.72", "hubUrl": "https://files.example.com/fw/w.img", "hubName": "w.img",
    "hubSize": "1180", "hubCrc": "123456789", "hubMd5": "",
    "bleVer": "1.14.28", "bleUrl": "https://files.example.com/fw/b.img", "bleName": "b.img",
    "bleSize": "280", "bleCrc": "a",
    "frontVer": "1.00.95", "frontUrl": "https://files.example.com/fw/f.bin", "frontName": "f.bin",
    "frontSize": "87", "frontCrc": "abc", "frontMd5": "0123456789abcdef0123456789abcdef",
    "backVer": "1.00.76", "backUrl": "https://files.example.com/fw/k.bin", "backName": "k.bin",
    "backSize": "35", "backCrc": "",
    "screenVer": "1.00.01", "screenName": "s.bin", "screenSize": "9"}


def test_firmware_install_request():
    parts, problems = F.firmware_reply_parts(FW_REPLY)
    assert [p["part"] for p in parts] == ["wifi", "ble", "front", "back"] and problems == []   # app's order
    on_lock = F.firmware_versions_on_lock({"firmware_version": "1.00.87", "sub_version": "1.00.72",
                                           "wifi_version": "1.02.70", "ble_version": "1.14.21"})
    send = F.firmware_parts_to_install(parts, on_lock, phone_does_ble=True)
    assert [p["part"] for p in send] == ["wifi", "front", "back"]                 # Bluetooth: the phone's
    assert [p["part"] for p in F.firmware_parts_to_install(parts, on_lock, False)] == ["wifi", "ble", "front", "back"]
    # not sent: a part the lock gives no version for, or 0.00.00, or one that's current
    assert F.firmware_parts_to_install(parts, {**on_lock, "front": None, "back": "0.00.00", "wifi": "2.02.72"},
                                       True) == []
    # file_ver: dots dropped, "00" added, read as hex; file_crc32: CRC-32 of the CRC text
    # (standard check values: "123456789" → CBF43926, "abc" → 352441C2)
    assert F.build_firmware_upgrade("u-1", "dev-1", FW_REPLY["downloadUrl"], send) == {
        "id": "u-1", "deviceId": "dev-1", "type": "MULTIPLE", "downloadUrl": "https://files.example.com/fw",
        "crc32": 0, "version": 0, "needReboot": False,
        "args": {"flag": 1 + 2 + 4,                                                # front, back, Wi-Fi
                 "first_md5": "0123456789abcdef0123456789abcdef", "first_file_name": "f.bin",
                 "first_file_size": 87, "first_file_ver": 0x1009500, "first_file_crc32": 0x352441C2,
                 "second_file_name": "k.bin", "second_file_size": 35, "second_file_ver": 0x1007600,
                 "three_md5": "", "three_file_name": "w.img", "three_file_size": 1180,
                 "three_file_ver": 0x2027200, "three_file_crc32": 0xCBF43926,
                 "four_file_size": 0, "four_file_ver": 0, "five_file_size": 0, "five_file_ver": 0}}
    assert F.firmware_long_version("0") == 0 and F.firmware_long_version(None) == 0
    parts, problems = F.firmware_reply_parts({**FW_REPLY, "backSize": "35k"})
    assert "back" not in [p["part"] for p in parts] and problems == ["the back part's size is '35k', not a number"]
    assert F.version_at_least("1.00.76", "1.00.76") and F.version_at_least("1.00.80", "1.00.76")
    assert not F.version_at_least("1.00.72", "1.00.76") and not F.version_at_least("0.00.00", "0.00.00")
    assert not F.version_at_least("1.0.76", "1.00.76") and not F.version_at_least(None, "1.00.76")
    assert F.describe_firmware_progress({"stage": "install", "progress": 40}) == "Installing 40 %"
    assert F.describe_firmware_progress({"stage": "final", "progress": 100}) == "Finished"


def test_follow_firmware_upgrade():
    import json
    from types import SimpleNamespace
    from locklyctl.transport import LocklyError, MqttChannel
    ch = MqttChannel("a@b.c", "jwt")                 # never connected: publish is replaced below
    sent = []

    def run(messages, idle_timeout=5.0, rc=0):
        def publish(topic, data, qos):
            sent.append((topic, json.loads(data), qos))
            for m in messages:                       # the broker's answers, as paho delivers them
                ch._on_message(None, None, SimpleNamespace(payload=json.dumps(m).encode()))
            return SimpleNamespace(rc=rc)            # paho's MQTTMessageInfo
        ch._client = SimpleNamespace(publish=publish)
        seen = []
        result = ch.follow_firmware_upgrade({"id": "u-1", "deviceId": "dev-1"}, seen.append, idle_timeout)
        return result, seen

    def msg(name, rid="r", **payload):
        return {"header": {"name": name, "requestId": rid}, "payload": payload}

    result, seen = run([msg("firmwareUpgradeProgress", upgradeId="other", stage="final", progress=100),
                        msg("firmwareUpgradeResponse", rid="u-1", code=0),
                        msg("firmwareUpgradeProgress", upgradeId="u-1", stage="download", progress=50),
                        msg("firmwareUpgradeProgress", upgradeId="u-1", stage="final", progress=100)])
    assert [s["stage"] for s in seen] == ["download", "final"] and result["stage"] == "final"
    topic, envelope, qos = sent[-1]
    assert (topic, qos, envelope["header"]["name"], envelope["header"]["requestId"]) == \
        ("server", 2, "firmwareUpgradeRequest", "u-1")
    assert envelope["payload"] == {"id": "u-1", "deviceId": "dev-1"}
    for messages, rc, cod in (([msg("firmwareUpgradeFailure", upgradeId="u-1", stage="install",
                                    errorMessage="crc error")], 0, "failure"),
                              ([msg("firmwareUpgradeResponse", rid="u-1", code=3005)], 0, "3005"),
                              ([], 0, "timeout"),
                              ([], 4, "publish")):                 # 4: paho's MQTT_ERR_NO_CONN
        try:
            run(messages, idle_timeout=0.05, rc=rc)
        except LocklyError as e:
            assert e.cod == cod, (e.cod, cod)
        else:
            raise AssertionError(f"no error for {cod}")
    assert ch._listeners == {}                       # nothing left listening


def test_paging_log_starts_at_position_1():
    """PagingLogCmdDataSource.java:119 starts at 1. Live on 2026-11-13 a Visage
    answered position 0 with its totals and no records, position 1 with all 50."""
    from locklyctl.capabilities import resolve_capabilities
    n = "00112233445566778899AABBCCDDEEFF"
    enc = P.encrypt_master_code(MC, UUID)
    caps = resolve_capabilities({"mod": "PGK728WRHK"}, 105)
    open_range = "FFFFFF" + "FFFFFF"                 # no start or end date
    assert _plain(P.build_paging_log_cmd(MC, UUID, nonce=n, caps=caps)).startswith(
        "94" + f"{len(enc) // 2:02d}" + enc.upper() + open_range + "0100" + "FFFFFFFFFF" * 2 + n)


def test_event_log_request_and_paging():
    """lockEventLogQueryRequest as MqttLockLogApiImpl sends it (app 3.3.4),
    paged by offset += 100 while havingMore. Replies are shaped as Lockly sent
    them on 2026-11-13 (timestamps in seconds); the values are made up."""
    from locklyctl.const import EVENT_LOG_BASE
    from locklyctl.transport import LocklyClient, LocklyError
    c = LocklyClient("a@b")

    def item(n):
        return {"eventId": n, "eventType": "92", "lockName": "x", "lockUserName": "Dana",
                "lockUserFirstName": "", "lockUserLastName": "", "time": "261113080605",
                "timestamp": 1794557165 + n, "userId": "7"}
    pages = [{"header": {"name": "lockEventLogQueryResponse"},
              "payload": {"havingMore": True, "items": [item(n) for n in range(100)]}},
             {"header": {"name": "lockEventLogQueryResponse"},
              "payload": {"havingMore": False, "items": [item(100)]}}]
    sent = []
    c._post = lambda ep, body, **kw: (sent.append((ep, body, kw)), pages[len(sent) - 1])[1]
    ev = c.get_event_log({"ID": UUID}, 1794441600000, 1794528000000)
    assert len(ev) == 101
    assert ev[0] == {"tm": 1794557165000, "co": "92", "na": "Dana", "pid": "7", "id": 0}
    ep, body, kw = sent[0]
    assert ep == "v1/proto/handler" and kw["base"] == EVENT_LOG_BASE
    assert (body["header"]["namespace"], body["header"]["name"]) == ("com.lockly", "lockEventLogQueryRequest")
    assert body["payload"] == {"deviceId": UUID, "startTime": "20261112000000", "endTime": "20261113000000",
                               "offset": 0, "limit": 100}           # UTC, yyyyMMddHHmmss (DateTimesKt)
    assert [b["payload"]["offset"] for _, b, _ in sent] == [0, 100]

    # Anything but a lockEventLogQueryResponse is an error, not an empty log.
    c._post = lambda ep, body, **kw: {"header": {"name": "error"}, "payload": None}
    try:
        c.get_event_log({"ID": UUID}, 0)
    except LocklyError as e:
        assert e.cod == "eventlog"
    else:
        raise AssertionError("an error reply read as an empty log")


def test_history_includes_secondary_users():
    """getlkhist's sul list is shown with el, as UnlockRecordFragment.C4 does."""
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    c._post_para_des3 = lambda ep, req, **kw: {"cod": "200", "el": [{"co": "92"}], "sul": [{"co": "4"}],
                                               "LAST_EVENT_SYNC_TIME": 5}
    assert c.get_history({"ID": UUID}, 1) == ([{"co": "92"}, {"co": "4"}], 5)


def test_uses_event_log():
    assert F.uses_event_log({"mod": "PGK728WRHK", "clientId": "th_x"})
    assert not F.uses_event_log({"mod": "PGK728WRHK", "clientId": ""})     # no Wi-Fi client id
    assert not F.uses_event_log({"mod": "PGD7Y", "clientId": "th_x"})


def test_status_failure_records_offline_only_for_3005():
    """3005 is Lockly's server saying the device is offline (seen for two
    Visages on 2026-11-13); other failures don't claim that."""
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    c.ASLEEP_RETRY_DELAYS = ()
    for code, offline in (("3005", True), (None, False)):
        def no_reply(lock, frame, directive=None, code=code):
            c._tl.mqtt_code, c._tl.last_cod = code, None
            return None
        c.exchange_frame = no_reply
        assert c.query_status({"ID": UUID, "mc": MC}) is None
        assert c.status_cache[UUID]["reachable"] is False and c.status_cache[UUID]["offline"] is offline


def test_status_query_sends_the_lock_time_and_reads_its_clock():
    """The status query carries a time (QueryLockStatusCmd.getData, isAddTime)
    and the reply's data[18:30] is the lock's own clock (QueryLockStatusCmd:213).
    The bytes are a Visage's real answer on 2026-11-13: 2026-11-13 19:52:21."""
    from datetime import datetime, timedelta, timezone
    enc = P.encrypt_master_code(MC, UUID)
    assert _plain(P.build_query_status_cmd(MC, UUID, datetime(2026, 11, 13, 19, 52, 21))).startswith(
        "1E" + f"{len(enc) // 2:02d}" + enc.upper() + "1A0B0D133415" + "01")
    lock = {"mc": MC, "ID": UUID, "tz": "+8:00"}
    st = P.parse_ack(_reply("1E", "01000E0B" + "A0" + "46FF46FF" + "1A0B0D133415" + "00" * 3 + "69" + "00" * 13),
                     MC, UUID)
    assert st["lock_clock"] == "1a0b0d133415"
    assert F.lock_clock(lock, st) == (datetime(2026, 11, 13, 19, 52, 21, tzinfo=timezone(timedelta(hours=8))), True)
    assert F.lock_clock(lock, {"lock_clock": "000000000000"}) == (None, True)      # no valid time
    assert F.lock_clock(lock, {}) == (None, False)                               # didn't say
    assert abs((F.lock_now({"tz": "+0:00"}) - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()) < 2


def test_query_status_records_the_clock_offset():
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    lock = {"mc": MC, "ID": UUID, "tz": "+8:00"}

    def answer_with(clock_hex):
        return lambda lk, frame, directive=None: _reply(
            "1E", "01000E0B" + "A0" + "46FF46FF" + clock_hex + "00" * 3 + "69" + "00" * 13)
    c.exchange_frame = answer_with(P._timestamp_hex(F.lock_now(lock)))
    assert abs(c.query_status(lock)["clock_offset_s"]) <= 2
    c.exchange_frame = answer_with("000000000000")
    st = c.query_status(lock)
    assert "clock_offset_s" in st and st["clock_offset_s"] is None


def _status_reply():
    """A status reply in the shape a Visage sends (41 bytes), clock 2026-11-13 19:52:21."""
    return _reply("1E", "01000E0B" + "A0" + "46FF46FF" + "1A0B0D133415" + "00" * 3 + "69" + "00" * 13)


def test_ble_frame_assembler():
    """Notification chunks joined as BLELoader.E/w do (app 3.3.4)."""
    from locklyctl.ble import FrameAssembler
    reply = bytes.fromhex(_status_reply())
    a = FrameAssembler()
    assert [a.feed(reply[i:i + 20]) for i in range(0, len(reply), 20)] == [None, None, reply]   # 20+20+1
    assert FrameAssembler().feed(b"\x01\x02" + reply) == reply          # junk before the header is cut
    bad = reply[:-1] + bytes([reply[-1] ^ 0xFF])
    assert FrameAssembler().feed(bad) is None                            # CRC fails
    noise = bytearray(reply)
    noise[6:8] = bytes.fromhex("0AFF")
    noise[-1] = P.crc8_lockly(bytes(noise[:-1]))
    a = FrameAssembler()
    assert a.feed(bytes(noise)) is None and a.feed(reply) == reply       # 0AFF skipped, next one taken


def test_ble_link_exchange_with_a_fake_lock():
    import asyncio
    from locklyctl import ble as BLE
    reply = bytes.fromhex(_status_reply())
    frame = bytes.fromhex(P.build_query_status_cmd(MC, UUID))
    link = BLE.BleLink("PGK728WRHK0000")
    written = []

    class FakeClient:
        async def write_gatt_char(self, uuid, data, response):
            written.append((uuid, bytes(data), response))
            if sum(len(d) for _, d, _ in written) == len(frame):      # whole command in: answer in chunks
                for i in range(0, len(reply), 20):
                    link._on_notify(None, bytearray(reply[i:i + 20]))

    async def setup():
        link._frames = asyncio.Queue()
    link._run(setup(), 5)
    link._client = FakeClient()
    try:
        assert link.exchange(frame.hex()) == reply.hex().upper()
        assert all(u == BLE.WRITE and len(d) <= BLE.CHUNK for u, d, _ in written)
        assert b"".join(d for _, d, _ in written) == frame
    finally:
        link._client = None
        link.close()


def test_bluetooth_status_goes_over_the_link():
    from locklyctl import ble as BLE
    from locklyctl.transport import LocklyClient
    c = LocklyClient("a@b")
    lock = {"mc": MC, "ID": UUID, "tz": "+8:00", "blename": "PGK728WRHK0000"}
    opened = []

    class FakeLink:
        def __init__(self, name):
            opened.append(name)
        def connect(self):
            pass
        def exchange(self, frame_hex):
            return _status_reply()
        def close(self):
            opened.append("closed")
    real = (BLE.BleLink, BLE.available)
    BLE.BleLink, BLE.available = FakeLink, (lambda: "")
    try:
        st = c.bluetooth_status(lock)
    finally:
        BLE.BleLink, BLE.available = real
    assert opened == ["PGK728WRHK0000", "closed"]
    assert st["lock_clock"] == "1a0b0d133415" and "clock_offset_s" in st
    assert c.last_transport_trail() == "Bluetooth → ACK"
    assert getattr(c._tl, "ble", None) is None                    # the link is only for that one check


def test_firmware_install_falls_back_to_bluetooth():
    from locklyctl.transport import LocklyClient, LocklyError
    parts, _ = F.firmware_reply_parts(FW_REPLY)
    old = {"front": "1.00.87", "back": "1.00.72", "wifi": "1.02.70", "ble": "1.14.21"}
    send = F.firmware_parts_to_install(parts, old, phone_does_ble=True)
    lock = {"mc": MC, "ID": UUID, "tz": "+8:00", "blename": "PGK728WRHK0000"}
    after = {"firmware_version": "1.00.95", "sub_version": "1.00.76", "wifi_version": "2.02.72",
             "ble_version": "1.14.21", "clock_offset_s": -1}

    class Channel:
        def follow_firmware_upgrade(self, payload, progress, idle_timeout):
            progress({"stage": "final", "progress": 100})
            return {"stage": "final"}
    c = LocklyClient("a@b")
    c.FW_SETTLE_S = c.FW_BLE_AFTER_S = c.FW_VERIFY_EVERY_S = 0
    c._mqtt_channel = lambda: Channel()
    c.query_status = lambda lk: None                             # its Wi-Fi isn't back
    asked = []
    c.bluetooth_status = lambda lk: asked.append(lk["ID"]) or after
    events = []
    out = c.firmware_install(lock, send, FW_REPLY["downloadUrl"], events.append, bluetooth=True)
    assert asked == [UUID] and out["versions"]["wifi"] == "2.02.72" and out["clockOffset"] == -1
    assert events[-1]["stage"] == "done" and events[-1]["clockOffset"] == -1

    c.FW_VERIFY_S = 0                                            # without Bluetooth it can only wait
    try:
        c.firmware_install(lock, send, FW_REPLY["downloadUrl"], events.append)
    except LocklyError as e:
        assert "hasn't come back online" in str(e) and asked == [UUID]
    else:
        raise AssertionError("a lock that never answered was reported installed")


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn(); print("PASS", fn.__name__)
        except Exception:
            failed += 1; print("FAIL", fn.__name__); traceback.print_exc()
    print(f"\n{len(fns)-failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
