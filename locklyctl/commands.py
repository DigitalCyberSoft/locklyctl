"""Non-interactive subcommands, for scripts and cron.

    locklyctl gui
    locklyctl list [--json]
    locklyctl status LOCK [--live] [--json]
    locklyctl lock LOCK
    locklyctl unlock LOCK [--yes]
    locklyctl welcome LOCK set --days weekdays --start 08:00 --end 18:00 [--send]
    locklyctl welcome LOCK off|always [--send]
    locklyctl welcome LOCK query
    locklyctl rain LOCK query|on|off [--send]
    locklyctl firmware LOCK [--install [--yes]] [--bluetooth-chip [--yes]]
    locklyctl wifi LOCK [--ssid NAME [--password-file F | --ask-password] [--yes]]
    locklyctl wifi --ssid NAME --forget

LOCK is a lock's name (case-insensitive; a unique prefix will do) or its ID.
They sign in with the credential saved by the interactive menu. Experimental
commands (welcome, rain) are dry runs that print the frame unless --send is
given. Unlock and firmware --install ask for confirmation, or need --yes when
nothing can be asked.
Exit status: 0 ok, 1 the lock or cloud failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import ble as BLE
from . import cli as C
from . import features as F
from . import guard as G
from . import wifi_passwords as W
from .transport import LocklyClient, LocklyError

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


class UsageError(Exception):
    pass


def _name(lock: dict) -> str:
    return str(lock.get("na") or lock.get("blename") or lock.get("ID", "?"))


def find_lock(locks: list[dict], query: str) -> dict:
    """Match by ID, then exact name, then unique name prefix (all case-insensitive)."""
    q = query.strip().lower()
    for lk in locks:
        if str(lk.get("ID", "")).lower() == q:
            return lk
    exact = [lk for lk in locks if _name(lk).lower() == q]
    matches = exact or [lk for lk in locks if _name(lk).lower().startswith(q)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        known = ", ".join(sorted(_name(lk) for lk in locks)) or "(none)"
        raise UsageError(f"no lock matches {query!r}; locks: {known}")
    raise UsageError(f"{query!r} matches several locks: "
                     + ", ".join(sorted(_name(lk) for lk in matches)))


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="command", metavar="COMMAND",
                                help="run one action and exit (omit for the menus)")

    g = sub.add_parser("gui", help="open the desktop app (needs the 'gui' extra)")
    g.add_argument("--browser", action="store_true",
                   help="open it in your web browser instead (no 'gui' extra needed)")

    p = sub.add_parser("list", help="list locks with their silent hub-cached status")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("status", help="a lock's status (silent hub cache by default)")
    p.add_argument("lock")
    p.add_argument("--live", action="store_true",
                   help="query the lock itself (wakes it — it beeps)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("lock", help="lock a lock")
    p.add_argument("lock")

    p = sub.add_parser("unlock", help="unlock a lock")
    p.add_argument("lock")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = sub.add_parser("welcome", help="Welcome Mode schedule (experimental)")
    p.add_argument("lock")
    p.add_argument("action", choices=["query", "set", "off", "always"])
    p.add_argument("--days", default="daily",
                   help="daily, weekdays, once, or e.g. mon,wed,fri (default: daily)")
    p.add_argument("--start", default="08:00", help="HH:MM, lock's local time")
    p.add_argument("--end", default="18:00", help="HH:MM, lock's local time")
    p.add_argument("--form", choices=[F.WELCOME_9B, F.WELCOME_46],
                   help="force the 8-slot (9B) or single-slot (46) command")
    p.add_argument("--send", action="store_true",
                   help="actually transmit (default: print the frame only)")

    p = sub.add_parser("rain", help="Rain Mode (experimental)")
    p.add_argument("lock")
    p.add_argument("state", choices=["query", "on", "off"])
    p.add_argument("--send", action="store_true",
                   help="actually transmit (default: print the frame only)")

    p = sub.add_parser("wifi", help="the lock's Wi-Fi over Bluetooth from this computer: see the networks "
                                     "it sees, or give it one (as the app's Wi-Fi setup does)")
    p.add_argument("lock", nargs="?")
    p.add_argument("--ssid", help="the network to give the lock; without it, list what the lock sees")
    p.add_argument("--password-file", help="read the Wi-Fi password from this file (else the saved one, "
                                           "this computer's, or it's asked for)")
    p.add_argument("--ask-password", action="store_true", help="type the password even if one is saved")
    p.add_argument("--forget", action="store_true", help="forget the password saved for --ssid, then stop")
    p.add_argument("--country", help="two-letter country code for Wi-Fi rules (default: this computer's)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("firmware", help="newer firmware Lockly offers, next to what the lock "
                                        "runs (wakes it)")
    p.add_argument("lock")
    p.add_argument("--bluetooth-chip", action="store_true",
                   help="install the Bluetooth chip's firmware over Bluetooth from this computer "
                        "(the lock must be in range)")
    p.add_argument("--install", action="store_true",
                   help="install it over Wi-Fi (multi-part locks such as the Visage), then confirm it")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")


def run(args: argparse.Namespace) -> int:
    if args.command == "wifi" and getattr(args, "forget", False):
        return _forget_wifi(args)                   # no Lockly sign-in needed
    try:
        client = C.login_flow(prompt=sys.stdin.isatty(), quiet=True)
    except (EOFError, KeyboardInterrupt):
        return EXIT_FAIL
    if client is None:
        return EXIT_FAIL
    if getattr(args, "connection", None):
        client.connection = args.connection
    try:
        return _HANDLERS[args.command](client, args)
    except UsageError as e:
        print(f"locklyctl: {e}", file=sys.stderr)
        return EXIT_USAGE
    except LocklyError as e:
        print(C.red(f"  error: {e}"))
        return EXIT_FAIL
    except (EOFError, KeyboardInterrupt):
        return EXIT_FAIL
    finally:
        client.close()


def _list(client: LocklyClient, args) -> int:
    client.prime_cached_status(client.locks)
    if args.json:
        print(json.dumps([{"name": _name(lk), "id": lk.get("ID"), "model": lk.get("mod"),
                           "hubid": lk.get("hubid") or None,
                           "status": client.status_cache.get(lk["ID"])}
                          for lk in client.locks], indent=2))
    else:
        for lk in client.locks:
            print(f"{C.lock_label(client, lk)}  {C.dim(lk['ID'])}")
    return EXIT_OK


def _status(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    st = client.query_status(lock) if args.live else client.cached_status(lock)
    if not st:
        ble = (client.status_cache.get(lock["ID"]) or {}).get("bluetooth") if args.live else ""
        print(C.red("  no response from the lock" + (f" (Bluetooth: {ble})" if ble else "")) if args.live else
              C.yellow("  no cached status for this lock (no hub, or hub too old) — try --live"),
              file=sys.stderr)
        return EXIT_FAIL
    if args.json:
        print(json.dumps({"name": _name(lock), "id": lock["ID"], "live": args.live, **st},
                         indent=2, default=str))
    else:
        C.print_status(st)
    return EXIT_OK


def _lock(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    ok, detail = client.lock(lock)
    print(C.green(f"  {_name(lock)}: locked ✓") if ok else C.red(f"  failed: {detail}"))
    return EXIT_OK if ok else EXIT_FAIL


def _unlock(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("unlock needs --yes when it can't ask for confirmation")
        if not C.confirm(f"  unlock {C.bold(_name(lock))}?", default=False):
            return EXIT_OK
    ok, detail = client.unlock(lock)
    print(C.green(f"  {_name(lock)}: unlocked ✓") if ok else C.red(f"  failed: {detail}"))
    return EXIT_OK if ok else EXIT_FAIL


def _welcome(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    form = args.form or F.welcome_form(lock)
    if form is None:
        raise UsageError(f"don't know which Welcome Mode {lock.get('mod') or 'this lock'} "
                         "uses — pass --form 9B or --form 46")
    week, begin, end = 0, (0, 0), (0, 0)
    try:
        if args.action == "set":
            week = F.week_byte(args.days.split(","))
            begin, end = F.parse_hhmm(args.start), F.parse_hhmm(args.end)
            if end <= begin:
                raise UsageError("--end must be after --start (overnight windows are untested)")
        what, build, needs_nonce = C.welcome_plan(lock, form, args.action, week, begin, end)
    except ValueError as e:
        raise UsageError(str(e)) from e
    if args.action == "set" and form == F.WELCOME_9B:
        print(C.yellow("  note: this writes all 8 slots — other schedules on the lock are cleared."),
              file=sys.stderr)
    ok = C._run_experimental(client, lock, what, build, needs_nonce=needs_nonce,
                             send=True if args.action == "query" else args.send)
    return EXIT_OK if ok else EXIT_FAIL


def _rain(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    if args.state == "query":
        ok = C._run_experimental(client, lock, "Rain Mode query",
                                 lambda n: F.build_rain_mode_query(lock, n), send=True)
    else:
        ok = C._run_experimental(client, lock, f"Rain Mode {args.state.upper()}",
                                 lambda n: F.build_rain_mode_set(lock, n, args.state == "on"),
                                 send=args.send)
    return EXIT_OK if ok else EXIT_FAIL


def _firmware(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    if args.bluetooth_chip:
        return _bluetooth_chip(client, lock, args)
    plan = client.firmware_plan(lock)
    offer = plan["offer"]
    if not offer["release"]:
        print(C.green(f"  {_name(lock)}: nothing newer from Lockly"))
        return EXIT_OK
    multi = offer["kind"] == F.FW_MULTI_PART
    on_lock = F.firmware_display_versions(lock, plan["status"], offer["kind"])
    print(f"  {_name(lock)}: Lockly offers {'release ' if multi else ''}{offer['release']}")
    for p in offer["parts"]:
        current = on_lock.get(p["part"]) or "?"
        newer = F.version_lower(on_lock.get(p["part"]), p["version"])
        by_phone = not multi or (p["part"] == "ble" and F.bluetooth_chip_needs_phone(lock))
        print(f"    {p['part']:<7} {current:>8} → {p['version']:<9} "
              + (C.yellow("newer") if newer else C.dim("current"))
              + C.dim(", Lockly app at the door" if by_phone else ", over Wi-Fi"))
    send = ", ".join(f"{p['part']} {p['version']}" for p in plan["send"])
    if not args.install:
        print(C.dim(f"  --install sends over Wi-Fi: {send}") if send and not plan["blocked"]
              else C.dim(f"  {plan['blocked']}"))
        return EXIT_OK
    if plan["blocked"]:
        print(C.red(f"  can't install: {plan['blocked']}"))
        return EXIT_FAIL
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("firmware --install needs --yes when it can't ask for confirmation")
        print(C.yellow("  The lock downloads and installs it itself, then restarts; leave the door "
                       "alone until this finishes."))
        print(C.yellow(_HELD))
        if not C.confirm(f"  install {send} on {C.bold(_name(lock))}?", default=False):
            return EXIT_OK
    last = [""]

    def progress(event: dict) -> None:
        if event.get("warning"):
            print(C.yellow(f"    {event['warning']}"), flush=True)
        if event["text"] != last[0]:
            last[0] = event["text"]
            print(C.dim(f"    {event['text']}"), flush=True)

    result = client.firmware_install(lock, plan["send"], plan["downloadUrl"], progress, files=plan["files"])
    print(C.green(f"  {_name(lock)}: installed, the lock reports "
                  + ", ".join(f"{p['part']} {result['versions'].get(p['part'])}" for p in plan["send"]) + " ✓"))
    return EXIT_OK


_HELD = (f"  The files are downloaded and checked first. Once the install starts, Ctrl+C has to be "
         f"pressed {G.FORCE_PRESSES} times to stop it, and this computer is kept awake.")


def _bluetooth_chip(client: LocklyClient, lock: dict, args) -> int:
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("firmware --bluetooth-chip needs --yes when it can't ask for confirmation")
        print(C.yellow("  This rewrites the lock's Bluetooth chip over Bluetooth; keep this computer "
                       "next to the door until it finishes."))
        print(C.yellow(_HELD))
        if not C.confirm(f"  update the Bluetooth chip of {C.bold(_name(lock))}?", default=False):
            return EXIT_OK
    shown = {"text": "", "pct": -10}

    def progress(event: dict) -> None:
        if event.get("warning"):
            print(C.yellow(f"    {event['warning']}"), flush=True)
        pct = event.get("progress")
        if event["stage"] == "install" and pct is not None:
            if pct < shown["pct"] + 10 and pct != 100:
                return                                   # every 10 %, not every block
            shown["pct"] = pct
        if event["text"] != shown["text"]:
            shown["text"] = event["text"]
            print(C.dim(f"    {event['text']}"), flush=True)
    try:
        r = client.bluetooth_chip_update(lock, progress)
    except BLE.BluetoothError as e:
        print(C.red(f"  {e}"))
        return EXIT_FAIL
    print(C.green(f"  {_name(lock)}: Bluetooth chip {r['before']} → {r['after']} ✓"))
    return EXIT_OK


def _country() -> str:
    """This computer's country, as the app sends the phone's (LocalesKt.a; US
    when unknown)."""
    import locale
    tag = (locale.getlocale()[0] or "")
    return tag.split("_")[1][:2].upper() if "_" in tag else "US"


def _forget_wifi(args) -> int:
    if not args.ssid:
        print("locklyctl: wifi --forget needs --ssid", file=sys.stderr)
        return EXIT_USAGE
    print(f"  Forgot the saved password for {args.ssid}." if W.forget(args.ssid)
          else f"  No password was saved for {args.ssid}.")
    return EXIT_OK


def _wifi_password(ssid: str, args) -> tuple[str, str]:
    """(password, source): --password-file, else the saved one or this
    computer's (wifi_passwords.resolve) unless --ask-password, else typed."""
    if args.password_file:
        with open(args.password_file, encoding="utf-8") as f:
            return f.read().rstrip("\r\n"), "file"
    if not args.ask_password:
        if W.PLATFORM == "darwin" and W.known([ssid])[ssid] == "system":
            print(C.dim("  macOS asks for an administrator's password to read it from the keychain"))
        password, source, why = W.resolve(ssid)
        if password:
            print(C.dim(f"  using the password {W.source_text(source)} for {ssid}"))
            return password, source
        if why:
            print(C.dim(f"  {why}"))
    if not sys.stdin.isatty():
        raise UsageError(f"wifi --ssid needs --password-file when it can't ask (no password is saved for {ssid})")
    import getpass
    return getpass.getpass(f"  Wi-Fi password for {ssid}: "), "typed"


def _wifi(client: LocklyClient, args) -> int:
    if not args.lock:
        raise UsageError("wifi needs a lock (or --ssid NAME --forget)")
    lock = find_lock(client.locks, args.lock)
    if not args.ssid:
        best: dict[str, dict] = {}
        for n in sorted(client.bluetooth_wifi_scan(lock), key=lambda n: n["rssi"]):
            if n["ssid"]:
                best.setdefault(n["ssid"], n)                  # strongest of each name
        for o in W.order(list(best)):                          # saved, this computer's, then signal
            n = best[o["ssid"]]
            marks = [{"saved": "password saved", "system": "saved on this computer"}.get(o["known"], ""),
                     "this computer is on it" if o["connected"] else "", "suggested" if o["suggested"] else ""]
            mark = ", ".join(m for m in marks if m)
            print(f"  -{n['rssi']:<3} dBm  type {n['router_type']}  {n['ssid']}" + (f"  ({mark})" if mark else ""))
        return EXIT_OK
    password, source = _wifi_password(args.ssid, args)
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("wifi --ssid needs --yes when it can't ask for confirmation")
        if not C.confirm(f"  give {C.bold(_name(lock))} the Wi-Fi network {args.ssid}?", default=False):
            return EXIT_OK
    last = [""]

    def progress(event: dict) -> None:
        if event["text"] != last[0]:
            last[0] = event["text"]
            print(C.dim(f"    {event['text']}"), flush=True)
    r = client.bluetooth_wifi_setup(lock, args.ssid, password, country=(args.country or _country()).upper(),
                                    on_event=progress)
    ok = r["code"] == 3
    print((C.green if ok else C.red)(f"  {_name(lock)}: {r['text']}"))
    note = W.after_setup(args.ssid, password, source, r["code"])
    if note:
        print(C.dim(f"  {note}"))
    return EXIT_OK if ok else EXIT_FAIL


_HANDLERS = {"list": _list, "status": _status, "lock": _lock, "unlock": _unlock,
             "welcome": _welcome, "rain": _rain, "firmware": _firmware, "wifi": _wifi}
