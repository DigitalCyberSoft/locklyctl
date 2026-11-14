"""Non-interactive subcommands, for scripts and cron, in the app's own groups
(GROUPS, which --help lists them by).

  Doors
    locklyctl list [--all] [--json]
    locklyctl status LOCK [--live] [--json]
    locklyctl lock LOCK
    locklyctl unlock LOCK [--yes]
    locklyctl keep LOCK HOURS [--yes]
    locklyctl keep LOCK --end
    locklyctl activity LOCK [--json]
    locklyctl welcome LOCK set --days weekdays --start 08:00 --end 18:00 [--entry first|auto] [--send]
    locklyctl welcome LOCK off|always [--send]
    locklyctl welcome LOCK query
    locklyctl rain LOCK query|on|off [--send]
  People
    locklyctl people [LOCK] [--json]
    locklyctl suspend LOCK USER [--dry-run] [--yes]
    locklyctl resume LOCK USER [--dry-run] [--yes]
  Maintenance
    locklyctl timezone [LOCK ...]
    locklyctl timezone LOCK ... --sync [--yes]
    locklyctl wifi LOCK [--ssid NAME [--password-file F | --ask-password] [--yes]]
    locklyctl wifi --ssid NAME --forget
    locklyctl firmware LOCK [--install [--yes]] [--bluetooth-chip [--yes]]
    locklyctl add [BLUETOOTH_NAME --code CODE --name NAME [--pin-file F] [--yes]]
    locklyctl backup FILE [--no-encrypt] [--passphrase-file F] [--yes]
    locklyctl --server URL restore FILE [--replace] [--passphrase-file F] [--yes]
  This computer
    locklyctl gui
    locklyctl hide LOCK ...
    locklyctl unhide LOCK ...
    locklyctl move LOCK (--up [N] | --down [N] | --top | --bottom | --to POSITION)

LOCK is a lock's name (case-insensitive; a unique prefix will do) or its ID.
They sign in with the credential saved by the interactive menu. Experimental
commands (welcome, rain) are dry runs that print the frame unless --send is
given. Unlock, keep and firmware --install ask for confirmation, or need --yes
when nothing can be asked.
Exit status: 0 ok, 1 the lock or cloud failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import textwrap

from . import backup as B
from . import ble as BLE
from . import cli as C
from . import features as F
from . import guard as G
from . import view as V
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


# The commands as --help lists them: the app's groups. Doors and People are for
# every day, Maintenance is for the locks themselves (anything that needs
# Bluetooth or someone at the door, or changes a lock's firmware, network or
# clock), and the last group belongs to this computer, not to a lock.
GROUPS = (
    ("Doors", ("list", "status", "lock", "unlock", "keep", "activity", "welcome", "rain")),
    ("People", ("people", "suspend", "resume")),
    ("Maintenance", ("timezone", "wifi", "firmware", "add", "backup", "restore")),
    ("This computer", ("gui", "hide", "unhide", "move")),
)


def grouped_help(helps: dict[str, str], width: int) -> str:
    """The commands under their group, each with its help, wrapped to ``width``."""
    assert sorted(helps) == sorted(n for _, names in GROUPS for n in names), "a command is in no group, or in two"
    pad = max(map(len, helps)) + 2
    lines = []
    for title, names in GROUPS:
        lines.append(f"{title}:")
        for name in names:
            lines.append(textwrap.fill(helps[name], width=width, initial_indent=f"  {name:<{pad}}",
                                       subsequent_indent=" " * (pad + 2)))
        lines.append("")
    return "\n".join(lines)


def add_subcommands(parser: argparse.ArgumentParser) -> None:
    # No per-command help on the subparsers themselves: argparse would list them
    # flat, in the order written. They're listed by group in the epilog instead.
    sub = parser.add_subparsers(dest="command", metavar="COMMAND",
                                help="run one action and exit (omit for the menus); the commands are listed below")
    helps: dict[str, str] = {}

    def cmd(name: str, help: str, **kw) -> argparse.ArgumentParser:
        helps[name] = help
        return sub.add_parser(name, description=help[:1].upper() + help[1:] + ".", **kw)

    g = cmd("gui", "open the desktop app (needs the 'gui' extra)")
    g.add_argument("--browser", action="store_true",
                   help="open it in your web browser instead (no 'gui' extra needed)")

    p = cmd("list", "list locks with their silent hub-cached status")
    p.add_argument("--all", action="store_true", help="include hidden locks (marked as hidden)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    hide_note = ("listings leave hidden locks out; commands that name a lock, and bulk "
                 "actions, still reach it")
    p = cmd("hide", f"leave locks out of listings ({hide_note})")
    p.add_argument("lock", nargs="+")
    p = cmd("unhide", "show hidden locks in listings again")
    p.add_argument("lock", nargs="+")
    p = cmd("move", "move a lock up or down in the listings (hidden locks keep their place)")
    p.add_argument("lock")
    where = p.add_mutually_exclusive_group(required=True)
    where.add_argument("--up", type=int, nargs="?", const=1, metavar="N", help="up N places (default 1)")
    where.add_argument("--down", type=int, nargs="?", const=1, metavar="N", help="down N places (default 1)")
    where.add_argument("--top", action="store_true", help="first in the listing")
    where.add_argument("--bottom", action="store_true", help="last in the listing")
    where.add_argument("--to", type=int, metavar="POSITION", help="this place in the listing, from 1")

    p = cmd("status", "a lock's status (silent hub cache by default)")
    p.add_argument("lock")
    p.add_argument("--live", action="store_true",
                   help="query the lock itself (wakes it — it beeps)")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = cmd("lock", "lock a lock")
    p.add_argument("lock")

    p = cmd("unlock", "unlock a lock")
    p.add_argument("lock")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = cmd("keep", "keep a Visage or Vision unlocked from now for under 24 hours (a one-time Welcome Mode "
                    "window the lock ends by itself), or end one early with --end")
    p.add_argument("lock")
    p.add_argument("hours", nargs="?", type=float, help="how long, in hours")
    p.add_argument("--end", action="store_true", help="end the window now and lock the door")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = cmd("activity", "a lock's access log for the last 7 days, newest first")
    p.add_argument("lock")
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = cmd("people", "who can open a lock, or with no lock named, everyone across all of them "
                      "(from Lockly's records: no lock is woken)")
    p.add_argument("lock", nargs="?")
    p.add_argument("--json", action="store_true", help="machine-readable output (PINs are left out)")

    p = cmd("welcome", "Welcome Mode schedule (experimental)")
    p.add_argument("lock")
    p.add_argument("action", choices=["query", "set", "off", "always"])
    p.add_argument("--entry", choices=["first", "auto"], default="first",
                   help="8-slot locks: how the hours start. first (default, as the Lockly app): the door "
                        "stays locked until someone opens it once; auto: it unlocks by itself")
    p.add_argument("--days", default="daily",
                   help="daily, weekdays, once, or e.g. mon,wed,fri (default: daily)")
    p.add_argument("--start", default="08:00", help="HH:MM, lock's local time")
    p.add_argument("--end", default="18:00", help="HH:MM, lock's local time")
    p.add_argument("--form", choices=[F.WELCOME_9B, F.WELCOME_46],
                   help="force the 8-slot (9B) or single-slot (46) command")
    p.add_argument("--send", action="store_true",
                   help="actually transmit (default: print the frame only)")

    p = cmd("rain", "Rain Mode (experimental)")
    p.add_argument("lock")
    p.add_argument("state", choices=["query", "on", "off"])
    p.add_argument("--send", action="store_true",
                   help="actually transmit (default: print the frame only)")

    p = cmd("wifi", "the lock's Wi-Fi over Bluetooth from this computer: see the networks "
                                     "it sees, or give it one (as the app's Wi-Fi setup does)")
    p.add_argument("lock", nargs="?")
    p.add_argument("--ssid", help="the network to give the lock; without it, list what the lock sees")
    p.add_argument("--password-file", help="read the Wi-Fi password from this file (else the saved one, "
                                           "this computer's, or it's asked for)")
    p.add_argument("--ask-password", action="store_true", help="type the password even if one is saved")
    p.add_argument("--forget", action="store_true", help="forget the password saved for --ssid, then stop")
    p.add_argument("--country", help="two-letter country code for Wi-Fi rules (default: this computer's)")
    p.add_argument("--mqtt-host", help="write this MQTT host into the lock instead of the one the server "
                                       "returns (aim the lock at a private locklyctl-mqtt server)")
    p.add_argument("--mqtt-port", type=int, help="write this MQTT port into the lock instead of the "
                                                 "returned one")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = cmd("timezone", "each lock's time zone in the Lockly account, next to this "
                                        "computer's; --sync gives locks theirs, then the time")
    p.add_argument("lock", nargs="*", help="locks to show (default: all) or to --sync")
    p.add_argument("--sync", action="store_true",
                   help="give each named lock its time zone (the account's, else this computer's), as the "
                        "app does after setup and firmware updates, then the time (wakes it)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = cmd("add", "set up a new or reset lock over Bluetooth from this computer and put it "
                                   "on your account, as the app does; without a name, list the locks nearby "
                                   "that aren't on it")
    p.add_argument("ble_name", nargs="?", metavar="BLUETOOTH_NAME", help="the lock's Bluetooth name, from the list")
    p.add_argument("--code", help="the Activation Card's 8 digits, or its QR text (SN:... AC:...); asked if left out")
    p.add_argument("--name", help="the door's name on your account; asked if left out")
    p.add_argument("--pin-file", help="read the admin PIN (6 or more digits) from this file; asked if left out")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p = cmd("firmware", "newer firmware Lockly offers, next to what the lock "
                                        "runs (wakes it)")
    p.add_argument("lock")
    p.add_argument("--bluetooth-chip", action="store_true",
                   help="install the Bluetooth chip's firmware over Bluetooth from this computer "
                        "(the lock must be in range)")
    p.add_argument("--install", action="store_true",
                   help="install it over Wi-Fi (multi-part locks such as the Visage), then confirm it")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    for name, verb in (("suspend", "suspend"), ("resume", "restore")):
        p = cmd(name, f"{verb} an access user on a Visage without touching their PIN, "
                                      "fingerprint or face (suspend moves their window into the far future; "
                                      "resume puts the saved schedule back)")
        p.add_argument("lock")
        p.add_argument("user", help="the user's name (case-insensitive) or numeric id")
        p.add_argument("--dry-run", action="store_true", help="print the frame and the saved schedule, send nothing")
        p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")

    p = cmd("backup", "write this account's locks (with their master codes) and the people on "
                                      "them (with their PINs) to FILE, sealed with a passphrase. Signed in to "
                                      "Lockly, each Visage's people are read from the lock itself (it wakes)")
    p.add_argument("file")
    p.add_argument("--no-encrypt", action="store_true", help="write it unencrypted (it holds master codes and PINs)")
    p.add_argument("--passphrase-file", help="read the passphrase from this file (else it's asked for)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before waking the locks")
    p = cmd("restore", "bring a backup into the private server signed in with --server: merged "
                                       "by default, so nothing there is deleted; nothing is sent to the locks")
    p.add_argument("file")
    p.add_argument("--replace", action="store_true", help="make the account exactly the backup's: locks and "
                                                          "people that aren't in it are deleted from the server")
    p.add_argument("--passphrase-file", help="read the passphrase from this file (else it's asked for)")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask before --replace")

    width = max(60, min(shutil.get_terminal_size().columns, 100) - 2)
    parser.formatter_class = argparse.RawDescriptionHelpFormatter
    parser.epilog = grouped_help(helps, width) + textwrap.fill(parser.epilog or "", width=width)


def run(args: argparse.Namespace) -> int:
    if args.command == "wifi" and getattr(args, "forget", False):
        return _forget_wifi(args)                   # no Lockly sign-in needed
    try:
        client = C.login_flow(prompt=sys.stdin.isatty(), quiet=True,
                              server=getattr(args, "server", None))
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
    except (LocklyError, B.BackupError) as e:
        print(C.red(f"  error: {e}"))
        return EXIT_FAIL
    except (EOFError, KeyboardInterrupt):
        return EXIT_FAIL
    finally:
        client.close()


def _list(client: LocklyClient, args) -> int:
    shown = V.listed(client, include_hidden=args.all)
    hidden = V.hidden_ids(client)
    client.prime_cached_status(shown)
    if args.json:
        print(json.dumps([{"name": _name(lk), "id": lk.get("ID"), "model": lk.get("mod"),
                           "hubid": lk.get("hubid") or None, "hidden": lk.get("ID") in hidden,
                           "status": client.status_cache.get(lk["ID"])}
                          for lk in shown], indent=2))
        return EXIT_OK
    for lk in shown:
        mark = C.dim("  (hidden)") if lk.get("ID") in hidden else ""
        print(f"{C.lock_label(client, lk)}  {C.dim(lk['ID'])}{mark}")
    left_out = len(client.locks) - len(shown)
    if left_out:                     # on stderr, so stdout stays one line per lock
        print(C.dim(f"  {left_out} hidden: list --all shows {'it' if left_out == 1 else 'them'}, "
                    "unhide LOCK brings one back"), file=sys.stderr)
    return EXIT_OK


def _hide(client: LocklyClient, args, hidden: bool = True) -> int:
    for lk in [find_lock(client.locks, q) for q in args.lock]:
        V.set_hidden(client, lk, hidden)
        print(C.green(f"  {_name(lk)}: " + ("hidden from listings" if hidden else "shown in listings again")))
    return EXIT_OK


def _unhide(client: LocklyClient, args) -> int:
    return _hide(client, args, hidden=False)


def _move(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    for flag in ("up", "down", "to"):
        n = getattr(args, flag)
        if n is not None and n < 1:
            raise UsageError(f"--{flag} needs a number from 1")
    if args.top:
        new = V.move(client, lock, to=1)
    elif args.bottom:
        new = V.move(client, lock, to=len(client.locks))
    elif args.to is not None:
        new = V.move(client, lock, to=args.to)
    else:
        new = V.move(client, lock, by=-args.up if args.up is not None else args.down)
    hidden = V.hidden_ids(client)
    listing = [lk for lk in new if lk.get("ID") == lock.get("ID") or lk.get("ID") not in hidden]
    place = next(i for i, lk in enumerate(listing) if lk.get("ID") == lock.get("ID")) + 1
    print(C.green(f"  {_name(lock)}: {place} of {len(listing)} in the listing"))
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
    send = True if args.action == "query" else args.send
    try:
        if args.action == "set":
            week = F.week_byte(args.days.split(","))
            begin, end = F.parse_hhmm(args.start), F.parse_hhmm(args.end)
            if end <= begin:
                raise UsageError("--end must be after --start (overnight windows are untested)")
        # A set or an off rewrites the lock's 8 slots: a "keep unlocked" window
        # that's running is read from the lock and written back (as the app does).
        keep = (_api(client)._kept_slots(lock)
                if send and form == F.WELCOME_9B and args.action in ("set", "off") else [])
        what, build, needs_nonce = F.welcome_plan(
            lock, form, args.action, week, begin, end, keep=keep,
            sync=F.SYNC_AUTO_ENTRY if args.entry == "auto" else F.SYNC_FIRST_ENTRY)
    except ValueError as e:
        raise UsageError(str(e)) from e
    if args.action == "set" and form == F.WELCOME_9B:
        print(C.yellow("  note: this replaces the lock's other weekly schedules; a running keep-unlocked "
                       "window stays."), file=sys.stderr)
    ok = C._run_experimental(client, lock, what, build, needs_nonce=needs_nonce, send=send)
    return EXIT_OK if ok else EXIT_FAIL


def _api(client: LocklyClient):
    """The app's own shaping of people, activity and keep-unlocked windows,
    over this command's signed-in client."""
    from .gui.api import Api
    return Api.for_client(client)


def _keep(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    api = _api(client)
    if args.end:
        if args.hours is not None:
            raise UsageError("keep takes HOURS or --end, not both")
        r = api.end_keep_unlocked(lock["ID"])
        print(C.green(f"  {_name(lock)}: window ended, locked ✓") if r["ok"] else C.red(f"  not locked: {r['detail']}"))
        return EXIT_OK if r["ok"] else EXIT_FAIL
    if args.hours is None:
        raise UsageError("keep needs HOURS (or --end)")
    if not args.yes:                    # it opens a door, as unlock does
        if not sys.stdin.isatty():
            raise UsageError("keep needs --yes when it can't ask for confirmation")
        if not C.confirm(f"  keep {C.bold(_name(lock))} unlocked for {args.hours:g} h? "
                         "it opens within about 20 seconds", default=False):
            return EXIT_OK
    try:
        r = api.keep_unlocked(lock["ID"], round(args.hours * 60))
    except ValueError as e:             # not an 8-slot lock, no hub, or too long
        raise UsageError(str(e)) from e
    print(C.green(f"  {_name(lock)}: unlocked until {r['until']}, lock time ✓"))
    return EXIT_OK


def _activity(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    events = _api(client).activity(lock["ID"])
    if args.json:
        print(json.dumps(events, indent=2))
    elif not events:
        print(C.dim("  nothing in the last 7 days (for locks other than a Visage, Lockly's log only has what "
                    "was uploaded to it)"))
    else:
        for ev in events:
            print(f"  {ev['time']}  {ev['what']:<26} {ev['who']}")
    return EXIT_OK


# What a person's row never shows on the command line: their PIN, and fields only the app's page uses.
_ROW_PRIVATE = ("pin", "recordKey", "weekData", "startTime", "endTime", "timeType", "status",
                "doorUnit", "doorRest", "doorRoute", "doorAccessUsers")


def _row_text(row: dict) -> str:
    creds = [n for n, has in (("PIN", row["hasPin"]), (f"{row['fingerprints']} fingerprint(s)", row["fingerprints"]),
                              (f"{row['faces']} face(s)", row["faces"]), (f"{row['cards']} card(s)", row["cards"])) if has]
    state = C.red("  paused") if row["suspended"] else ""
    return f"{row['role']:<7} {row['access']}{state}  {C.dim(', '.join(creds) or 'no credentials')}"


def _people(client: LocklyClient, args) -> int:
    api = _api(client)
    clean = lambda row: {k: v for k, v in row.items() if k not in _ROW_PRIVATE}
    if args.lock:
        rows = api.door_access(find_lock(client.locks, args.lock)["ID"])
        if args.json:
            print(json.dumps([clean(r) for r in rows], indent=2))
        else:
            for r in rows:
                print(f"  {r['person']:<22} {_row_text(r)}")
        return EXIT_OK
    out = api.people()
    if args.json:
        print(json.dumps([{"name": p["name"], "email": p["email"], "doors": [clean(d) for d in p["doors"]]}
                          for p in out["people"]], indent=2))
    else:
        for p in out["people"]:
            print(C.bold(f"  {p['name']}") + (C.dim(f"  {p['email']}") if p["email"] else ""))
            for d in p["doors"]:
                print(f"    {d['doorName']:<20} {_row_text(d)}")
    for name in out["failedDoors"]:
        print(C.red(f"  couldn't read the people on {name}"), file=sys.stderr)
    return EXIT_FAIL if out["failedDoors"] else EXIT_OK


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
    app = ", Lockly app" if offer["kind"] == F.FW_CAMERA else ", Lockly app at the door"
    for p in offer["parts"]:
        known = on_lock.get(p["part"])
        newer = F.version_lower(known, p["version"])
        by_phone = not multi or (p["part"] == "ble" and F.bluetooth_chip_needs_phone(lock))
        print(f"    {p['part']:<7} {known or '?':>8} → {p['version']:<9} "
              + (C.yellow("newer") if newer else C.dim("current" if known else "not reported"))
              + C.dim(app if by_phone else ", over Wi-Fi"))
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
                     "this computer is on it" if o["connected"]
                     else f"this computer is on {o['twin_of']}" if o["twin_of"] else "",
                     "suggested" if o["suggested"] else ""]
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
                                    on_event=progress, mqtt_host=getattr(args, "mqtt_host", None),
                                    mqtt_port=getattr(args, "mqtt_port", None))
    ok = r["code"] == 3
    print((C.green if ok else C.red)(f"  {_name(lock)}: {r['text']}"))
    note = W.after_setup(args.ssid, password, source, r["code"])
    if note:
        print(C.dim(f"  {note}"))
    return EXIT_OK if ok else EXIT_FAIL


def _timezone(client: LocklyClient, args) -> int:
    locks = [find_lock(client.locks, q) for q in args.lock]
    if not args.sync:
        here = F.computer_utc_offset_minutes()
        print(f"  this computer: {F.utc_offset_text(here)}")
        for lk in locks or client.locks:
            zone = F.lock_utc_offset_minutes(lk)
            note = ("not set in the account: the lock gets this computer's" if zone is None
                    else "differs from this computer's" if zone != here else "")
            print(f"  {_name(lk):<18} {F.utc_offset_text(zone):<9}" + (C.yellow(f"  {note}") if note else "")
                  + ("" if F.takes_time_zone(lk) else C.dim("  (its model doesn't take a time zone)")))
        return EXIT_OK
    if not locks:
        raise UsageError("timezone --sync needs the locks to give their time zone to")
    for lk in locks:
        if not F.takes_time_zone(lk):
            raise UsageError(f"{_name(lk)} ({lk.get('mod') or '?'}) doesn't take a time zone this way")
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("timezone --sync needs --yes when it can't ask for confirmation")
        zones = ", ".join(f"{_name(lk)} {F.utc_offset_text(F.time_zone_hours(lk)[0] * 60)}" for lk in locks)
        if not C.confirm(f"  give {zones} (each wakes and beeps)?", default=False):
            return EXIT_OK
    failed = 0
    for lk in locks:
        try:
            r = client.set_time_zone(lk)
        except (BLE.BluetoothError, LocklyError) as e:          # this lock failed; the next may not
            r = {"ok": False, "text": str(e), "hours": 0, "source": "", "via": None}
        zone = F.utc_offset_text(r["hours"] * 60) + (" (this computer's)" if r["source"] == "computer" else "")
        print(C.green(f"  {_name(lk)}: {zone}, then the time" + (" over Bluetooth" if r["via"] == "bluetooth"
                                                                  else "") + " ✓")
              if r["ok"] else C.red(f"  {_name(lk)}: {r['text']}"))
        failed += not r["ok"]
    return EXIT_FAIL if failed else EXIT_OK


def _add(client: LocklyClient, args) -> int:
    if not args.ble_name:
        try:
            found = client.nearby_new_locks()
        except BLE.BluetoothError as e:
            print(C.red(f"  {e}"))
            return EXIT_FAIL
        if not found:
            print("  no lock that isn't on your account is in Bluetooth range")
        for d in found:
            print(f"  {d['rssi']:>4} dBm  {d['name']}"
                  + ("" if d["model"] else C.dim("  (setting up this model isn't ported)")))
        return EXIT_OK
    import getpass
    ask = sys.stdin.isatty()
    if not ask and not (args.code and args.name and args.pin_file and args.yes):
        raise UsageError("add needs --code, --name, --pin-file and --yes when it can't ask")
    code = args.code or getpass.getpass("  Activation Code (8 digits on the card): ")
    name = args.name or input("  Door name: ").strip()
    if args.pin_file:
        with open(args.pin_file) as f:
            pin = f.read().strip()
    else:
        pin = getpass.getpass("  Admin PIN for the lock (6 or more digits): ")
        if getpass.getpass("  The same PIN again: ") != pin:
            raise UsageError("the two PINs differ")
    if not args.yes and not C.confirm(f"  set up {C.bold(args.ble_name)} as {name!r} on your account? "
                                      "(keep this computer next to it)", default=False):
        return EXIT_OK

    def progress(event: dict) -> None:
        print(C.dim(f"    {event['text']}"), flush=True)
    try:
        r = client.add_lock(args.ble_name, code, name=name, pin=pin, on_event=progress)
    except BLE.BluetoothError as e:
        print(C.red(f"  {e}"))
        return EXIT_FAIL
    print(C.green(f"  {_name(r['lock'])}: set up and on your account ✓"))
    if not r["zone"]:
        print(C.yellow(f"  It didn't take the time zone; run: locklyctl timezone {_name(r['lock'])!r} --sync"))
    if not r["registered"]:
        print(C.dim("  Lockly didn't record the Activation Code (initialCode/add); the lock works without it."))
    return EXIT_OK


def _resolve_user(client: LocklyClient, lock: dict, query: str) -> int:
    """A user's numeric id from a name (case-insensitive, via the cloud list) or
    a number given directly."""
    q = str(query).strip()
    if q.isdigit():
        return int(q)
    matches = [u for u in (client.list_users(lock) or [])
               if str(u.get("name") or "").strip().lower() == q.lower()]
    ids = sorted({int(u["userAcuId"]) for u in matches if u.get("userAcuId") is not None})
    if len(ids) == 1:
        return ids[0]
    if not ids:
        raise UsageError(f"no user matches {query!r} on {_name(lock)} (give the numeric id, or an exact name)")
    raise UsageError(f"{query!r} matches several users on {_name(lock)}: ids {ids}")


def _suspend(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    user_id = _resolve_user(client, lock, args.user)
    if args.dry_run:
        r = client.suspend_access_user(lock, user_id, dry_run=True)
        print(f"  [dry-run] suspend user {user_id} on {_name(lock)}")
        print(f"    would save: type=0x{r['stash']['user_type']:02X} date={r['stash']['date']}")
        print(f"    frame: {r['frame']}")
        return EXIT_OK
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("suspend needs --yes when it can't ask for confirmation")
        if not C.confirm(f"  suspend user {user_id} on {C.bold(_name(lock))}?", default=False):
            return EXIT_OK
    r = client.suspend_access_user(lock, user_id)
    before, after = r["before"], r["after"]
    if after == before:
        print(C.green(f"  user {user_id} suspended ✓") + C.dim(
            f"  (kept: faces {after['faces']}, fingerprints {after['fingerprints']}, cards {after['cards']})"))
        return EXIT_OK
    print(C.red(f"  user {user_id} suspended but credentials changed: {before} -> {after}"))
    return EXIT_FAIL


def _resume(client: LocklyClient, args) -> int:
    lock = find_lock(client.locks, args.lock)
    user_id = _resolve_user(client, lock, args.user)
    if args.dry_run:
        r = client.resume_access_user(lock, user_id, dry_run=True)
        print(f"  [dry-run] resume user {user_id} on {_name(lock)} (schedule from the {r['source']})")
        print(f"    would restore: type=0x{r['restore']['user_type']:02X} date={r['restore']['date']}")
        print(f"    frame: {r['frame']}")
        return EXIT_OK
    if not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("resume needs --yes when it can't ask for confirmation")
        if not C.confirm(f"  resume user {user_id} on {C.bold(_name(lock))}?", default=False):
            return EXIT_OK
    r = client.resume_access_user(lock, user_id)
    if r["source"] == "cloud":
        print(C.yellow("  no saved schedule; reconstructed from the cloud record (may not match the exact "
                       "prior window)"))
    print(C.green(f"  user {user_id} resumed ✓") + C.dim(
        f"  (type=0x{r['restore']['user_type']:02X} date={r['restore']['date']})"))
    return EXIT_OK


def _passphrase(args, *, confirm: bool) -> str:
    if args.passphrase_file:
        with open(args.passphrase_file, encoding="utf-8") as f:
            phrase = f.read().rstrip("\r\n")
        if not phrase:
            raise UsageError("the passphrase file is empty")
        return phrase
    if not sys.stdin.isatty():
        raise UsageError("needs --passphrase-file when it can't ask for the passphrase")
    import getpass
    phrase = getpass.getpass("  Backup passphrase: ")
    if not phrase:
        raise UsageError("the passphrase is empty")
    if confirm and getpass.getpass("  The same passphrase again: ") != phrase:
        raise UsageError("the two passphrases differ")
    return phrase


def _backup(client: LocklyClient, args) -> int:
    if os.path.exists(args.file):
        raise UsageError(f"{args.file} already exists: choose another name")
    # Asked first, so nothing is woken if the passphrase step is abandoned.
    phrase = None if args.no_encrypt else _passphrase(args, confirm=True)
    if client.server:
        doc, notes = B.from_server(client), []
    else:
        visages = [lk for lk in client.locks if str(lk.get("mod") or "").upper() in F.ACCESS_USER_MODELS]
        if visages and not args.yes:
            if not sys.stdin.isatty():
                raise UsageError("backup needs --yes when it can't ask (it wakes each Visage to read its people)")
            if not C.confirm(f"  read the people on {len(visages)} lock(s) from the locks themselves? "
                             "Each one wakes and beeps.", default=True):
                return EXIT_OK
        doc, notes = B.from_lockly(client, progress=lambda name: print(C.dim(f"  reading {name}…"), flush=True))
    data = B.plain(doc) if args.no_encrypt else B.seal(doc, phrase)
    try:
        B.write_new(args.file, data)
    except FileExistsError:
        raise UsageError(f"{args.file} already exists: choose another name") from None
    people = sum(len(lk.get("people") or []) for lk in doc["locks"])
    print(C.green(f"  {len(doc['locks'])} lock(s) and {people} person(s) written to {args.file}"
                  + ("" if args.no_encrypt else ", encrypted")))
    if args.no_encrypt:
        print(C.yellow("  It isn't encrypted and holds master codes and PINs: keep it private and out of "
                       "version control."))
    for missing, text in notes:
        print((C.yellow if missing else C.dim)(f"  {text}"))
    return EXIT_FAIL if any(missing for missing, _ in notes) else EXIT_OK


def _restore(client: LocklyClient, args) -> int:
    if not client.server:
        raise UsageError("restore brings a backup into a private server: add --server URL (Lockly's own "
                         "service can't take one)")
    with open(args.file, "rb") as f:
        data = f.read()
    doc = B.open_backup(data, _passphrase(args, confirm=False) if B.is_encrypted(data) else None)
    if args.replace and not args.yes:
        if not sys.stdin.isatty():
            raise UsageError("restore --replace needs --yes when it can't ask")
        if not C.confirm("  make this account exactly the backup's? Locks and people that aren't in it are "
                         "deleted from the server.", default=False):
            return EXIT_OK
    r = B.restore_to_server(client, doc, replace=args.replace)
    print(C.green(f"  locks: {r['locksAdded']} added, {r['locksUpdated']} updated, {r['locksRemoved']} removed; "
                  f"people: {r['peopleAdded']} added, {r['peopleUpdated']} updated, {r['peopleRemoved']} removed"))
    for dev in r["conflicts"]:
        print(C.yellow(f"  skipped lock {dev}: another account on that server has it"))
    no_login = sum(1 for lk in doc["locks"] if not lk.get("mqtt"))
    if no_login:
        why = " (Lockly doesn't hand those out)" if (doc.get("source") or {}).get("kind") == "lockly" else ""
        print(C.dim(f"  {no_login} lock(s) came without an MQTT login{why}: the server needs one for each "
                    "before that lock can connect to it."))
    print(C.dim("  Nothing was sent to the locks: they already hold these records."))
    return EXIT_FAIL if r["conflicts"] else EXIT_OK


_HANDLERS = {"list": _list, "hide": _hide, "unhide": _unhide, "move": _move,
             "keep": _keep, "activity": _activity, "people": _people,
             "backup": _backup, "restore": _restore,
             "status": _status, "lock": _lock, "unlock": _unlock,
             "welcome": _welcome, "rain": _rain, "firmware": _firmware, "wifi": _wifi, "timezone": _timezone,
             "add": _add, "suspend": _suspend, "resume": _resume}
