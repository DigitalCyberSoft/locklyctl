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

LOCK is a lock's name (case-insensitive; a unique prefix will do) or its ID.
They sign in with the credential saved by the interactive menu. Experimental
commands (welcome, rain) are dry runs that print the frame unless --send is
given. Unlock asks for confirmation, or needs --yes when nothing can be asked.
Exit status: 0 ok, 1 the lock or cloud failed, 2 usage error.
"""
from __future__ import annotations

import argparse
import json
import sys

from . import cli as C
from . import features as F
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


def run(args: argparse.Namespace) -> int:
    try:
        client = C.login_flow(prompt=sys.stdin.isatty(), quiet=True)
    except (EOFError, KeyboardInterrupt):
        return EXIT_FAIL
    if client is None:
        return EXIT_FAIL
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
        print(C.red("  no response from the lock") if args.live else
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


_HANDLERS = {"list": _list, "status": _status, "lock": _lock, "unlock": _unlock,
             "welcome": _welcome, "rain": _rain}
