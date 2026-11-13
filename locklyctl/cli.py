"""Interactive menu CLI for Lockly locks.

Unofficial. Built on protocol primitives from LocklyHA (MIT). Core actions
(login, list, unlock, lock, status, guest PINs, access log) use
hardware-verified code paths; Welcome Mode, Rain Mode and enrolment are
code-traced and marked EXPERIMENTAL at the point of use.
"""
from __future__ import annotations

import argparse
import getpass
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import features as F
from . import protocol as P
from . import config
from .const import CONNECTIONS
from .transport import LocklyClient, LocklyError

_LOG = logging.getLogger("locklyctl")

# ── small terminal helpers ───────────────────────────────────────────────────

def _c(code: str, s: str) -> str:
    # No colour when asked (NO_COLOR) or when output is piped / captured.
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"

def bold(s): return _c("1", s)
def dim(s): return _c("2", s)
def green(s): return _c("32", s)
def red(s): return _c("31", s)
def yellow(s): return _c("33", s)
def cyan(s): return _c("36", s)


def banner() -> None:
    print(bold("\n  locklyctl — unofficial Lockly control") )
    print(dim("  not affiliated with Lockly. your keys stay on this machine.\n"))


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{prompt}{suffix}: ").strip()
    return val or (default or "")


def confirm(prompt: str, default: bool = False) -> bool:
    d = "Y/n" if default else "y/N"
    val = input(f"{prompt} ({d}): ").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")


def pause() -> None:
    input(dim("\n  press Enter to continue… "))


def _numbered_menu(title: str, options: list[tuple[str, str]]) -> str:
    """Fallback: numbered menu read over plain stdin (non-TTY, or on error)."""
    print(bold(f"\n  {title}"))
    for i, (_key, label) in enumerate(options, 1):
        print(f"   {cyan(str(i))}. {label}")
    while True:
        raw = input("  > ").strip().lower()
        if raw in ("q", "0"):
            return "quit"
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        for key, _ in options:
            if raw == key:
                return key
        print(red("  invalid choice"))


def _read_key() -> str:
    """Read one keypress in cbreak mode; map arrows/enter/esc to tokens."""
    import select, termios, tty
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)

    def more(timeout: float = 0.05) -> bool:
        return bool(select.select([fd], [], [], timeout)[0])

    try:
        tty.setcbreak(fd)
        # Read bytes straight from the fd: sys.stdin buffers ahead, which would
        # hide the rest of an escape sequence from select().
        ch = os.read(fd, 1).decode(errors="ignore")
        if ch == "\x1b":                      # bare Esc, or an escape sequence
            seq = ""
            while more() and len(seq) < 8:    # drain the whole sequence
                seq += os.read(fd, 1).decode(errors="ignore")
                if seq[-1:].isalpha() or seq[-1:] == "~":
                    break
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left",
                    "OA": "up", "OB": "down", "OC": "right", "OD": "left",
                    }.get(seq, "esc" if not seq else "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":                      # Ctrl-C
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def menu(title: str, options: list[tuple[str, str]]) -> str:
    """Arrow-key menu (up/down/enter; q or Esc backs out). Returns the chosen
    key. Falls back to a numbered menu when stdin isn't an interactive TTY."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()) or os.environ.get("TERM") == "dumb":
        return _numbered_menu(title, options)
    try:
        idx = 0
        print(bold(f"\n  {title}") + dim("   (↑/↓, Enter; q to go back)"))
        rendered = False
        while True:
            if rendered:                      # move cursor up to redraw in place
                sys.stdout.write(f"\033[{len(options)}A")
            for i, (_key, label) in enumerate(options):
                cursor = cyan("▸ ") if i == idx else "  "
                line = f"  {cursor}{bold(label) if i == idx else label}"
                sys.stdout.write("\r\033[K" + line + "\n")
            sys.stdout.flush()
            rendered = True
            k = _read_key()
            if k == "up":
                idx = (idx - 1) % len(options)
            elif k == "down":
                idx = (idx + 1) % len(options)
            elif k == "enter":
                return options[idx][0]
            elif k in ("q", "esc", "left"):
                return "quit"
            elif k.isdigit() and 1 <= int(k) <= len(options):
                return options[int(k) - 1][0]
    except KeyboardInterrupt:
        raise
    except Exception:
        # Any terminal quirk → safe fallback rather than a broken screen.
        return _numbered_menu(title, options)


# ── lock display helpers ──────────────────────────────────────────────────────

def _status_badge(client: LocklyClient, lock: dict) -> str:
    """Short status/battery from this session's cache (no network)."""
    e = client.status_cache.get(lock["ID"])
    if not e and F.remote_route(lock) == "bluetooth":
        return dim("Bluetooth only (no hub)")
    if not e:
        return dim("— status unknown")
    if not e.get("reachable", True):
        return yellow("asleep / unreachable")
    state = green("locked") if e.get("is_locked") else red("UNLOCKED")
    bits = [state]
    if e.get("battery_percent") is not None:
        pct = e["battery_percent"]
        bits.append((red if pct <= 20 else yellow if pct <= 40 else green)(f"{pct}%"))
    elif e.get("low_battery") or e.get("battery_invalid"):
        bits.append(yellow("low batt"))
    if e.get("door_sensor_open"):
        bits.append(yellow("door open"))
    if e.get("cached"):
        bits.append(dim("(hub cache)"))
    return "  ".join(bits)


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _pad(s: str, width: int) -> str:
    """Left-justify by visible width (colour codes take no columns)."""
    return s + " " * max(0, width - len(_ANSI_RE.sub("", s)))


def lock_label(client: LocklyClient, lock: dict) -> str:
    name = lock.get("na") or lock.get("blename") or lock.get("ID", "?")
    model = lock.get("mod") or lock.get("lockType") or "?"
    return f"{name:<14} {_pad(_status_badge(client, lock), 28)} {dim(f'[{model}]')}"


def choose_lock(client: LocklyClient) -> dict | None:
    if not client.locks:
        print(red("  no locks on this account"))
        return None
    while True:
        opts = [(str(i), lock_label(client, lk)) for i, lk in enumerate(client.locks)]
        opts.append(("refresh", cyan("↻ Refresh all statuses")
                     + dim(" (wakes every lock — they beep)")))
        opts.append(("quit", "Quit"))
        pick = menu("Locks", opts)
        if pick in ("quit", ""):
            return None
        if pick == "refresh":
            n = len(client.locks)
            print(dim(f"  waking and querying {n} locks in parallel…"))
            client.refresh_all_status(client.locks)
            reachable = sum(1 for v in client.status_cache.values() if v.get("reachable"))
            print(dim(f"  {reachable}/{n} responded (the rest were asleep)"))
            continue
        if pick.isdigit():
            return client.locks[int(pick)]


# ── actions ───────────────────────────────────────────────────────────────────

def do_status(client: LocklyClient, lock: dict) -> None:
    silent = confirm("  use silent cached status (no beep)?", default=True)
    st = client.cached_status(lock) if silent else client.query_status(lock)
    if not st:
        if silent:
            print(yellow("  no cached status (hub too old / no hub). try a live query."))
        else:
            print(red("  no response from lock"))
        return
    print_status(st)


VIA_TEXT = {"hub": "its hub", "server": "Lockly's server", "bluetooth": "Bluetooth from this computer"}


def print_status(st: dict) -> None:
    locked = st.get("is_locked")
    print(f"  state: {green('LOCKED') if locked else red('UNLOCKED') if locked is not None else '?'}")
    if st.get("door_sensor_open") is not None:
        print(f"  door:  {'open' if st['door_sensor_open'] else 'closed'}")
    if st.get("firmware_version"):
        print(f"  fw:    {st['firmware_version']}")
    if st.get("battery_percent") is not None:
        print(f"  batt:  {st['battery_percent']}%")
    if st.get("low_battery") or st.get("battery_invalid"):
        print(yellow("  battery low"))
    fix = st.get("clock_fix")
    if fix:
        print(green(f"  clock: was {F.clock_offset_text(fix['was'])}; set it") if fix["fixed"] else
              red(f"  clock: {F.clock_offset_text(st.get('clock_offset_s'))}; {fix['detail']}"))
    elif "clock_offset_s" in st:
        off = st["clock_offset_s"]
        print(yellow(f"  clock: {F.clock_offset_text(off)}") if off is None or abs(off) >= F.CLOCK_WARN_S
              else "  clock: right")
    if st.get("via"):
        print(dim(f"  via:   {VIA_TEXT.get(st['via'], st['via'])}"
                  + ("; Lockly's server says its Wi-Fi is offline" if st.get("offline") else "")))


def do_unlock(client: LocklyClient, lock: dict) -> None:
    if not confirm(f"  unlock {bold(lock.get('na') or lock['ID'])}?", default=False):
        return
    ok, detail = client.unlock(lock)
    print(green("  unlocked ✓") if ok else red(f"  failed: {detail}"))


def do_lock(client: LocklyClient, lock: dict) -> None:
    ok, detail = client.lock(lock)
    print(green("  locked ✓") if ok else red(f"  failed: {detail}"))


def do_history(client: LocklyClient, lock: dict) -> None:
    since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
    res = client.get_history(lock, since)
    if not res:
        print(yellow("  no history returned"))
        return
    events, _ = res
    if not events:
        print(dim("  no events in the last 7 days"))
        return
    print(bold(f"\n  last {min(len(events), 20)} events:"))
    for ev in sorted(events, key=lambda e: e.get("tm") or 0)[-20:]:
        t = _fmt_event_time(ev.get("tm") or "")
        print(f"   {dim(f'{t:>16}')}  {P.describe_open_type(ev.get('co')):<22} {ev.get('na') or ''}")


def _fmt_event_time(t) -> str:
    """Epoch seconds/ms → local 'YYYY-MM-DD HH:MM'; anything else verbatim."""
    try:
        n = int(t)
    except (TypeError, ValueError):
        return str(t)
    if n > 10**11:                        # milliseconds
        n //= 1000
    if not 10**9 <= n < 10**10:           # not a plausible epoch — leave it
        return str(t)
    return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M")


def do_list_guests(client: LocklyClient, lock: dict) -> None:
    guests = client.list_guests(lock)
    if guests is None:
        print(red("  could not list guests"))
        return
    if not guests:
        print(dim("  no guest credentials"))
        return
    print(bold("\n  guests:"))
    for g in guests:
        print(f"   #{g.get('userAcuId')}  {g.get('name','?'):<18} pin={g.get('pw','?')} "
              f"{dim(str(g.get('startTime','')) + '→' + str(g.get('endTime','')))}")


def do_add_guest(client: LocklyClient, lock: dict) -> None:
    name = ask("  guest name")
    pin = ask("  PIN (4–8 digits)")
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        print(red("  PIN must be 4–8 digits"))
        return
    permanent = confirm("  permanent (no expiry)?", default=False)
    if permanent:
        start_ms = int(time.time() * 1000)
        end_ms = start_ms + 100 * 365 * 24 * 3600 * 1000
    else:
        raw = ask("  valid for how many hours", "24")
        if not (raw.isdigit() and int(raw) > 0):
            print(red("  hours must be a positive whole number"))
            return
        hours = int(raw)
        start_ms = int(time.time() * 1000)
        end_ms = start_ms + hours * 3600 * 1000
    try:
        body = client.add_guest_pin(lock, name, pin, start_ms, end_ms, permanent=permanent)
    except LocklyError as e:
        print(red(f"  failed: {e}"))
        return
    print(green(f"  created guest '{name}' (userAcuId={body.get('userAcuId')})"))
    print(yellow("  note: whether the PIN activates on the lock immediately is unverified; test it."))


def do_delete_guest(client: LocklyClient, lock: dict) -> None:
    guests = client.list_guests(lock) or []
    if not guests:
        print(dim("  no guests to delete"))
        return
    do_list_guests(client, lock)
    raw = ask("  userAcuId to delete (q to cancel)")
    if not raw.isdigit():
        return
    if client.delete_guest(lock, int(raw)):
        print(green("  deleted ✓"))
    else:
        print(red("  delete failed"))


def _run_experimental(client: LocklyClient, lock: dict, what: str, build,
                      *, needs_nonce: bool = True, send: bool | None = None) -> bool:
    """Dry run by default: ``build(nonce)`` is shown with a placeholder nonce
    and nothing is sent. A real send fetches a fresh nonce (one status query)
    immediately before transmitting, so think-time can't stale it. ``send``
    None asks; True/False decides without asking. Returns False only when a
    real send failed."""
    if send is None:
        send = confirm(f"  actually send {what} to the lock? (no = show frame only)",
                       default=False)
    if not send:
        print(dim(f"  [dry-run] {what}: {build('0' * 32)}"))
        return True
    ok, detail = client.send_built(lock, build, needs_nonce=needs_nonce)
    print(green(f"  ✓ {detail}") if ok else red(f"  {what} not accepted: {detail}"))
    return ok


def do_welcome_mode(client: LocklyClient, lock: dict) -> None:
    model = lock.get("mod") or "this lock"
    form = F.welcome_form(lock)
    print(yellow("\n  Welcome Mode: the 8-slot form is tested on a Visage; the single-slot"
                 "\n  form is checked against the app but not yet tried on a lock."))
    if form is None:
        form = menu(f"Which Welcome Mode does {model} use?", [
            (F.WELCOME_9B, "8-slot schedule (0x9B) — Visage, G25 models"),
            (F.WELCOME_46, "single schedule (0x46) — Touch and older models"),
            ("quit", "Cancel"),
        ])
        if form == "quit":
            return
    else:
        print(dim(f"  {model} uses the "
                  + ("8-slot 0x9B" if form == F.WELCOME_9B else "single-slot 0x46")
                  + " form"))
    opts = [("query", "Show what the lock has now"), ("set", "Set a schedule")]
    if form == F.WELCOME_46:
        opts.append(("always", "Always on (stay unlocked)"))
    opts += [("off", "Turn off"), ("quit", "Cancel")]
    action = menu("Welcome Mode", opts)
    if action == "quit":
        return

    week, begin, end = 0, (0, 0), (0, 0)
    if action == "always":
        print(yellow("  the door will stay unlocked around the clock until turned off."))
    elif action == "set":
        try:
            week = F.week_byte(ask("  days (daily / weekdays / once / mon,tue,…)", "daily").split(","))
            begin = F.parse_hhmm(ask("  start HH:MM (lock's local time)", "08:00"))
            end = F.parse_hhmm(ask("  end HH:MM", "18:00"))
        except ValueError as e:
            print(red(f"  {e}"))
            return
        if end <= begin and not confirm(
                "  end is not after start (overnight windows are untested) — continue?"):
            return
        if form == F.WELCOME_9B:
            print(yellow("  note: this writes all 8 slots — any other schedules on the lock are cleared."))
    what, build, needs_nonce = F.welcome_plan(lock, form, action, week, begin, end)
    _run_experimental(client, lock, what, build, needs_nonce=needs_nonce,
                      send=True if action == "query" else None)


def do_rain_mode(client: LocklyClient, lock: dict) -> None:
    print(yellow("\n  Rain Mode: tested on a Visage; other models not yet tried."))
    on = menu("Rain Mode", [("query", "show whether it's on"),
                            ("on", "turn ON (disable keypad in rain)"),
                            ("off", "turn OFF"), ("quit", "cancel")])
    if on == "quit":
        return
    if on == "query":
        _run_experimental(client, lock, "Rain Mode query",
                          lambda n: F.build_rain_mode_query(lock, n), send=True)
        return
    _run_experimental(client, lock, f"Rain Mode {on.upper()}",
                      lambda n: F.build_rain_mode_set(lock, n, on == "on"))


def do_enrol(client: LocklyClient, lock: dict) -> None:
    """Users & enrolment. Pick or create a user first, then act on that user."""
    while True:
        choice = menu(f"Users — {lock.get('na') or lock['ID']}", [
            ("owner", "The account owner (you)"),
            ("existing", "Select an existing user"),
            ("create", "Create a new user"),
            ("direct", "Direct-to-lock user slots " + yellow("(experimental)")),
            ("probe", "Face-enrolment relay probe " + dim("(diagnostic)")),
            ("back", "Back"),
        ])
        if choice in ("back", "quit"):
            return
        try:
            if choice == "probe":
                do_face_probe(client, lock)
                pause()
                continue
            if choice == "direct":
                do_direct_slots(client, lock)
                continue
            user = _resolve_user(client, lock, choice)
            if user is None:
                continue
            _user_action_menu(client, lock, user)
        except LocklyError as e:
            print(red(f"  error: {e}"))
            pause()


def do_direct_slots(client: LocklyClient, lock: dict) -> None:
    """User slots on the lock itself, bypassing the cloud user list: push a PIN
    (0x4B), list slots (0x50), delete a slot (0x4D). Code-traced only; every
    write is a dry run unless confirmed."""
    while True:
        choice = menu(f"Direct-to-lock slots — {lock.get('na') or lock['ID']}", [
            ("pin", "Push a PIN to a user slot (0x4B)"),
            ("list", "List user slots (0x50)"),
            ("delete", "Delete a user slot (0x4D)"),
            ("back", "Back"),
        ])
        if choice in ("back", "quit"):
            return
        print(yellow("\n  ⚠ EXPERIMENTAL — these frames are code-traced, not hardware-verified."))
        print(dim("  for slot-based locks (e.g. PGD7Y); Visage/Vision locks manage users differently."))
        print(dim("  the cloud user list is not updated; the app may not show the change."))
        try:
            if choice == "pin":
                name = ask("  name for the slot", "Staff")
                pin = ask("  PIN (4–8 digits)")
                slot = ask("  slot number")
                if not (pin.isdigit() and 4 <= len(pin) <= 8) or not slot.isdigit():
                    print(red("  PIN must be 4–8 digits and the slot a number"))
                else:
                    dry = not confirm("  actually send? (no = show frame only)", default=False)
                    ok, detail = client.set_user_pin_ble(lock, pin, int(slot), name, dry_run=dry)
                    print((green if ok else red)(f"  {detail}"))
            elif choice == "list":
                for page in (1, 2, 3):
                    ok, detail = client.list_users_ble(lock, page)
                    print((green if ok else red)(f"  page {page}: {detail}"))
                    if not ok or "no user slots" in detail:
                        break
            elif choice == "delete":
                slot = ask("  slot number to delete")
                if not slot.isdigit():
                    print(red("  slot must be a number"))
                elif confirm(f"  delete slot {slot} on the lock?", default=False):
                    dry = not confirm("  actually send? (no = show frame only)", default=False)
                    ok, detail = client.delete_user_ble(lock, int(slot), dry_run=dry)
                    print((green if ok else red)(f"  {detail}"))
        except LocklyError as e:
            print(red(f"  error: {e}"))
        pause()


def _resolve_user(client: LocklyClient, lock: dict, choice: str) -> dict | None:
    """Return a user context {userAcuId, adminId, name, owner} or None."""
    admin_id = int(lock.get("adminAcuId", 0) or 0)
    if choice == "owner":
        return {"userAcuId": 0, "adminId": 0, "name": "Owner (you)", "owner": True}
    if choice == "existing":
        users = client.list_guests(lock) or []
        if not users:
            print(dim("  no sub-users on this lock yet — create one first"))
            pause()
            return None
        opts = [(str(u.get("userAcuId")), f"{u.get('name','?')}  "
                 + dim(f"#{u.get('userAcuId')}")) for u in users] + [("back", "Back")]
        pick = menu("Select a user", opts)
        if pick in ("back", "quit"):
            return None
        u = next(x for x in users if str(x.get("userAcuId")) == pick)
        return {"userAcuId": int(pick), "adminId": admin_id,
                "name": u.get("name", "?"), "owner": False}
    if choice == "create":
        name = ask("  new user's name", "Staff")
        pin = ask("  PIN for them (4–8 digits; they can also use a face)")
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            print(red("  PIN must be 4–8 digits")); pause(); return None
        body = client.add_guest_pin(lock, name, pin, 0, 0, permanent=True)
        uid = int(body.get("userAcuId") or 0)
        print(green(f"  created '{name}' (userAcuId={uid})"))
        pause()
        return {"userAcuId": uid, "adminId": admin_id, "name": name, "owner": False}
    return None


def _user_action_menu(client: LocklyClient, lock: dict, user: dict) -> None:
    while True:
        label = f"{user['name']}" + ("" if user["owner"] else f"  (userAcuId={user['userAcuId']})")
        opts = [("face", "Enrol a FACE " + yellow("(experimental)"))]
        if not F.supports_face(lock):
            opts = [("face", "Enrol a face " + dim("(this lock has no face ID)"))]
        opts += [("finger", "Enrol a fingerprint " + yellow("(experimental)"))]
        if not user["owner"]:
            opts += [("delete", "Delete this user")]
        opts += [("back", "Back to users")]
        choice = menu(f"User: {label}", opts)
        if choice in ("back", "quit"):
            return
        try:
            if choice == "face":
                _enrol_face_for(client, lock, user)
            elif choice == "finger":
                print(yellow("\n  ⚠ EXPERIMENTAL — fingerprint capture is not fully traced."))
                print("  The person places their finger on the lock when prompted.")
                code = ask("  fingerprint code (from Lockly's getFpCode; a number)", "0")
                if not code.isdigit():
                    print(red("  the code must be a number")); pause(); continue
                dry = not confirm("  actually send? (no = show frame only)", default=False)
                ok, detail = client.fingerprint_capture_ble(
                    lock, user["userAcuId"], user["adminId"], int(code), dry_run=dry)
                print((green if ok else red)(f"  {detail}"))
            elif choice == "delete":
                if confirm(f"  delete {user['name']}?", default=False):
                    print(green("  deleted ✓") if client.delete_guest(lock, user["userAcuId"])
                          else red("  delete failed"))
                    return
        except LocklyError as e:
            print(red(f"  error: {e}"))
        pause()


def _enrol_face_for(client: LocklyClient, lock: dict, user: dict) -> None:
    """Run the face-capture flow for an already-selected user."""
    if not F.supports_face(lock):
        print(yellow(f"  {lock.get('mod') or 'this lock'} has no facial recognition."))
        return
    once = F.face_directions_for(lock) == [F.FACE_DIRECTION_ONCE]
    print(yellow("\n  ⚠ EXPERIMENTAL — face frames are code-traced."))
    print(dim(f"  {lock.get('mod')} uses {'a single capture' if once else 'a 5-direction sweep'}."))
    print(f"  {bold(user['name'])} must stand at the lock's camera now.")
    name = ask("  face label", user["name"])
    dry = not confirm("  actually send to the lock? (no = show frames only)", default=False)

    def on_prompt(_d, text):
        print("   " + cyan(text))

    ok, detail = client.enrol_face(
        lock, user_acu_id=user["userAcuId"], admin_id=user["adminId"],
        user_type=None, face_name=name, on_prompt=on_prompt, dry_run=dry)
    print((green if ok else red)("  " + detail))
    if not ok and not dry:
        print(dim("  transport: " + client.last_transport_trail()))


def _explain_transport_failure(lock: dict, client: LocklyClient) -> None:
    """Interpret a transport failure for the user based on the lock + trail."""
    hub = lock.get("hubid") or ""
    trail = client.last_transport_trail()
    if "3005" in trail or "device offline" in trail:
        if hub and not hub.startswith("PGH260"):
            print(yellow("    This looks like a classic hub (PGH200/220): it is NOT on"))
            print(yellow("    the MQTT channel, so '3005 device offline' there is expected."))
            print(yellow("    The real question is why the senddata relay failed — see the"))
            print(yellow("    'senddata → cod=…' above. Re-run with --debug for detail."))
        else:
            print(yellow("    '3005 device offline' means the lock isn't reachable on the"))
            print(yellow("    broker right now (asleep / off Wi-Fi). A status query wakes it,"))
            print(yellow("    so just retry — each command re-wakes it first."))
    if "930" in trail:
        print(yellow("    cod=930: the hub says this lock isn't bound to it (or it's a"))
        print(yellow("    Wi-Fi-native lock with no hub). That blocks the relay entirely."))
    print(dim(f"    lock hubid={hub or '(none)'}  model={lock.get('mod') or '?'}"))


def do_diagnose(client: LocklyClient, lock: dict) -> None:
    """Report exactly which transports work for this lock, for troubleshooting."""
    print(bold(f"\n  Diagnosing {lock.get('na') or lock['ID']}"))
    print(f"   model={lock.get('mod') or '?'}  hubid={lock.get('hubid') or '(none)'}  "
          f"iotdm={lock.get('iotdm') or '(none)'}")
    print(dim("   silent cached status (hub cache, no beep)…"))
    cached = client.cached_status(lock)
    print("   cached: " + (green("ok — hub is reachable via REST") if cached
                           else red("none (hub too old, or not reachable)")))
    print(dim("   live status query (exercises the command transport)…"))
    st = client.query_status(lock)
    print("   live:   " + (green("ok — commands can reach this lock") if st
                          else red("failed — commands cannot reach this lock")))
    print(dim("   transport: " + client.last_transport_trail()))
    if not st:
        _explain_transport_failure(lock, client)


def do_face_probe(client: LocklyClient, lock: dict) -> None:
    """Does the lock ENTER face-capture over the hub/MQTT relay? (No real face
    is enrolled.) The stop (0x17) is an exact, safe frame; the start (0x7D) is
    best-effort — we just watch whether the lock reacts, then always stop."""
    print(yellow("\n  ⚠ EXPERIMENTAL face-enrolment relay probe"))
    print("  This tests whether the app's 'Bluetooth only' restriction is real,")
    print("  or whether the lock accepts face enrolment over the hub/MQTT relay.")
    print("  Nobody's face is enrolled. You just watch the lock.")
    print(dim("  Stand where you can see/hear the lock (camera light, 'position"))
    print(dim("  your face' voice prompt). The probe always sends a STOP to exit."))

    # Step 1: the safe anchor — a stop can't strand anything.
    print(bold("\n  Step 1 — relay a STOP (0x17, exact frame):"))
    dry = not confirm("    send it? (no = show frame only)", default=False)
    ok, detail = client.stop_face_enrolment(lock, dry_run=dry)
    print("    " + (green if ok else red)(detail))
    if dry:
        print(dim("    (dry run — nothing sent)"))
        return
    if not ok:
        # Only an accepted ACK proves anything; anything else is a transport fail.
        print(red("    the relay did NOT deliver the command — not a result about"))
        print(red("    face enrolment, just that the frame never reached the lock."))
        print(dim("    transport: " + client.last_transport_trail()))
        _explain_transport_failure(lock, client)
        return
    print(green("    the lock accepted a relayed sensor command — strong sign the"))
    print(green("    'Bluetooth only' gate is app-side, not the lock's."))

    # Step 2: try to make it ENTER capture; observe; always stop afterward.
    if not confirm("\n  Step 2 — try to make the lock ENTER face capture now?", default=False):
        return
    print(dim("    watch the lock for ~5 seconds after sending…"))
    ok, detail = client.start_face_enrolment(lock, dry_run=False)
    print("    start " + (green if ok else red)(detail))
    input(dim("    did the lock enter capture (camera/voice)? press Enter to STOP… "))
    ok2, detail2 = client.stop_face_enrolment(lock, dry_run=False)
    print("    stop  " + (green if ok2 else yellow)(detail2))
    print(bold("\n  Result to tell the developer:"))
    print("   • stop delivered: " + ("yes" if ok2 else "no"))
    print("   • did the lock visibly enter capture on start? (your observation)")
    print(dim("   If yes → face enrolment over the relay is viable; I'll nail the"))
    print(dim("   0x7D binding fields and build the full 5-direction flow."))


# ── per-lock submenu ───────────────────────────────────────────────────────────

def lock_menu(client: LocklyClient, lock: dict) -> None:
    while True:
        print(bold(f"\n  ▸ {lock.get('na') or lock['ID']}"))
        choice = menu("Action", [
            ("status", "Status"),
            ("diagnose", "Connection test (which transports work)"),
            ("unlock", "Unlock"),
            ("lock", "Lock"),
            ("history", "Access log (last 7 days)"),
            ("guests", "List guest PINs"),
            ("addguest", "Add guest PIN (remote)"),
            ("delguest", "Delete guest PIN"),
            ("enrol", "Remote enrolment (add/list/delete users)"),
            ("welcome", "Welcome Mode schedule"),
            ("rain", "Rain Mode"),
            ("back", "Back to locks"),
        ])
        try:
            if choice in ("back", "quit"):
                return
            {
                "status": do_status, "diagnose": do_diagnose,
                "unlock": do_unlock, "lock": do_lock,
                "history": do_history, "guests": do_list_guests,
                "addguest": do_add_guest, "delguest": do_delete_guest,
                "enrol": do_enrol,
                "welcome": do_welcome_mode, "rain": do_rain_mode,
            }[choice](client, lock)
        except LocklyError as e:
            print(red(f"  error: {e}"))
        except Exception as e:  # noqa: BLE001
            print(red(f"  unexpected error: {e}"))
        pause()


# ── top level ──────────────────────────────────────────────────────────────────

def login_flow(*, prompt: bool = True, quiet: bool = False) -> LocklyClient | None:
    """Sign in: silently with the saved credential, else (if ``prompt``) by
    asking for the password. ``quiet`` sends status chatter to stderr so a
    command's stdout stays clean for scripts."""
    def say(msg: str) -> None:
        print(msg, file=sys.stderr if quiet else sys.stdout)

    cfg = config.load_config()
    email = cfg.get("email")
    pw_hash = config.load_credential(cfg)

    # 1) Stay logged in like the app: if we stored the credential, re-authenticate
    #    silently — no password prompt.
    if email and pw_hash:
        client = LocklyClient(email)
        try:
            client.login_with_hash(pw_hash)
            client.load_devices()
            client.connection = config.connection_mode(cfg)
            say(green(f"  signed in as {email} — {len(client.locks)} lock(s)"))
            if not cfg.get("pw_in_keyring") and config._keyring():
                config._save_credential(cfg, email, pw_hash)   # migrate file → keyring
                say(dim("  (moved the saved login into the system keyring)"))
            return client
        except LocklyError:
            say(dim("  saved login was rejected (password changed?)"
                    + (" — signing in again" if prompt else "")))

    if not prompt:
        say(red("  no saved login — run `locklyctl` once and choose to stay logged in"))
        return None

    # 2) Password login.
    email = ask("  Lockly email", email)
    password = getpass.getpass("  Lockly password: ")
    client = LocklyClient(email)
    try:
        client.login(password)
        client.load_devices()
    except LocklyError as e:
        say(red(f"  {e}"))
        return None
    if confirm("  stay logged in on this computer, like the app?", default=True):
        where = config._save_credential(cfg, email, client.pw_hash)
        say(dim("  (stored your login credential in "
                + ("the system keyring" if where == "keyring"
                   else "~/.locklyctl/config.json, readable only by you")
                + "; clear it any time with --logout)"))
    else:
        config._save_credential(cfg, email, None)
    client.connection = config.connection_mode(cfg)
    say(green(f"  logged in — {len(client.locks)} lock(s) found"))
    return client


def main(argv: list[str] | None = None) -> int:
    from . import __version__
    from . import commands
    parser = argparse.ArgumentParser(
        prog="locklyctl",
        description="Unofficial control for Lockly locks. With no command, opens "
                    "the interactive menus.",
        epilog="Commands sign in with the login saved by the menus. Experimental "
               "commands only print the frame unless --send is given.")
    parser.add_argument("--debug", action="store_true",
                        help="print protocol-level logging to the console")
    parser.add_argument("--logout", action="store_true",
                        help="forget the saved login credential and exit")
    parser.add_argument("--connection", choices=CONNECTIONS,
                        help="how to reach locks: auto (hub or Lockly's server, then Bluetooth from "
                             "this computer for a status check that got no answer), remote, or "
                             "bluetooth (status checks only); default: the saved setting, else auto")
    parser.add_argument("--version", action="version", version=f"locklyctl {__version__}")
    commands.add_subcommands(parser)
    args = parser.parse_args(argv)
    config._setup_logging(debug=args.debug)
    if args.logout:
        cfg = config.load_config()
        config._save_credential(cfg, cfg.get("email", ""), None)
        print(dim("  saved login cleared"))
        return 0
    if args.command == "gui":
        from .gui.app import main as gui_main
        return gui_main((["--debug"] if args.debug else [])
                        + (["--browser"] if getattr(args, "browser", False) else []))
    if args.command:
        return commands.run(args)
    banner()
    print(dim(f"  log: {config._log_path()}\n"))
    try:
        client = login_flow()
        if client is None:
            return 1
        if args.connection:
            client.connection = args.connection
        if client.prime_cached_status(client.locks):
            print(dim("  statuses from the hub cache (silent); ↻ Refresh for live"))
        try:
            while True:
                lock = choose_lock(client)
                if lock is None:
                    break
                lock_menu(client, lock)
        finally:
            client.close()
    except (EOFError, KeyboardInterrupt):
        print(dim("\n  interrupted"))
        return 130
    print(dim("\n  bye\n"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
