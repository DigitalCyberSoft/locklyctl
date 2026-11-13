"""The Python side of the GUI: methods the web page calls through pywebview.

Each public method is exposed to JavaScript as ``window.pywebview.api.<name>``
and runs on its own thread. Methods return plain JSON-able values and raise
on failure; the page shows the exception message. All lock work goes through
the same LocklyClient the CLI uses — this module only shapes data for the UI.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from .. import config
from .. import features as F
from .. import protocol as P
from ..features import lock_tz  # noqa: F401  (re-exported for callers)
from ..transport import LocklyClient, LocklyError

_LOG = logging.getLogger("locklyctl.gui")
_UNIT_RE = re.compile(r"^\s*(\d{1,4}[A-Za-z]?)\b\s*(.*)$")
_DURATIONS_H = {"4h": 4, "24h": 24, "72h": 72, "1w": 168}


def split_unit(name: str) -> tuple[str, str]:
    """'2A Office' → ('2A', 'Office'); '1204' → ('1204', ''); 'Garage' → ('', 'Garage')."""
    m = _UNIT_RE.match(name or "")
    return (m.group(1).upper(), m.group(2).strip()) if m else ("", (name or "").strip())


def _fmt_ms(ms) -> str | None:
    try:
        n = int(ms)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    if n > 10**11:
        n //= 1000
    try:
        return datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return None


_ROLES = {"OWNER": "owner", "ADMIN": "admin", "FAMILY": "family", "GUEST": "guest"}
_DAY_ABBR = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _days(mask: int) -> str:
    """Weekday bitmask (bit 0 = Monday) in words; runs of 3+ days as ranges."""
    if mask & 0x7F == 0x7F:
        return "Every day"
    if mask & 0x7F == 0x60:
        return "Weekends"
    on = [i for i in range(7) if mask & (1 << i)]
    parts, i = [], 0
    while i < len(on):
        j = i
        while j + 1 < len(on) and on[j + 1] == on[j] + 1:
            j += 1
        parts.append(f"{_DAY_ABBR[on[i]]}–{_DAY_ABBR[on[j]]}" if j - i >= 2
                     else ", ".join(_DAY_ABBR[k] for k in on[i:j + 1]))
        i = j + 1
    return ", ".join(parts) or "No days"


def _clock(ms, tz: timezone | None = None) -> str | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz).strftime("%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def describe_access(u: dict, tz: timezone | None = None) -> str:
    """A cloud user record's access window in words."""
    tt = u.get("timeType")
    if tt == "NO_LIMIT":
        return "Always"
    if tt == "WEEK":
        try:
            mask = int(u.get("weekData") or 0)
        except ValueError:
            mask = 0
        start, end = _clock(u.get("startTime"), tz), _clock(u.get("endTime"), tz)
        all_day = start == end or (start, end) == ("00:00", "23:59")
        window = f" {start}–{end}" if start and end and not all_day else ""
        return _days(mask) + window
    start, end = _fmt_ms(u.get("startTime")), _fmt_ms(u.get("endTime"))
    if start and end:
        return f"{start} to {end}"
    return tt.replace("_", " ").capitalize() if tt else "Unknown"


def person_access(u: dict, door: dict, tz: timezone | None = None) -> dict:
    """One person's access on one door, shaped for the People view."""
    # Collapse runs of spaces so "Sam  Taylor" and "Sam Taylor " group together.
    name = re.sub(r"\s+", " ", " ".join(x for x in (u.get("name"), u.get("lastName")) if x)).strip()
    email = (u.get("email") or "").strip().lower()
    return {
        "_key": email or f"name:{name.lower()}" if (email or name) else f"id:{door['id']}:{u.get('userAcuId')}",
        "person": name or email or "Unnamed",
        "lockId": door["id"], "doorName": door["name"], "doorUnit": door["unit"],
        "doorRest": door["rest"], "doorModel": door["model"], "doorRoute": door.get("route"),
        "userId": u.get("userAcuId"),
        "role": _ROLES.get(str(u.get("type")), str(u.get("type") or "").lower()),
        "access": describe_access(u, tz),
        "timeType": u.get("timeType"),
        "weekData": u.get("weekData"),
        "startTime": u.get("startTime"), "endTime": u.get("endTime"),
        "enabled": u.get("disabled") != "Y",
        "status": u.get("acuStatus"),
        "hasPin": bool(u.get("pw")) or u.get("pwStatus") == "Y",
        "pin": u.get("pw") or "",
        "fingerprints": int(u.get("fptCount") or 0),
        "faces": int(u.get("faceCount") or 0),
        "cards": int(u.get("rfidCount") or 0),
        "twoStep": str(u.get("multipleVerific")) == "1",
    }


def _parse_local(value: str, tz) -> datetime:
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"Not a date and time: {value!r}") from None
    return dt.replace(tzinfo=tz) if tz else dt


def _access_spec(access: dict, tz) -> dict:
    """GUI access choice → create_access_user arguments (role, cloud time
    type and times, lock role byte and date field)."""
    kind = (access or {}).get("kind", "always")
    if kind == "always":
        return {"role": "FAMILY", "time_type": "NO_LIMIT", "user_type": F.UT_FAMILY,
                "date": F.access_date_permanent()}
    if kind == "weekly":
        days = [d for d in (access.get("days") or []) if str(d).strip()]
        if not days:                     # week_byte([]) would mean "every day"
            raise ValueError("Pick at least one day.")
        week = F.week_byte(days)
        if week & F.WEEK_ONCE:
            raise ValueError("Pick days of the week.")
        begin, end = F.parse_hhmm(access.get("start", "")), F.parse_hhmm(access.get("end", ""))
        if end <= begin:
            raise ValueError("The end time must be after the start time.")
        today = datetime.now(tz)
        ms = lambda hm: int(today.replace(hour=hm[0], minute=hm[1], second=0, microsecond=0).timestamp() * 1000)
        return {"role": "GUEST", "time_type": "WEEK", "week": week, "start_ms": ms(begin),
                "end_ms": ms(end), "user_type": F.UT_GUEST_WEEKLY,
                "date": F.access_date_weekly(week, begin, end)}
    if kind == "dates":
        start, end = _parse_local(access.get("from"), tz), _parse_local(access.get("to"), tz)
        if end <= start:
            raise ValueError("The end must be after the start.")
        return {"role": "GUEST", "time_type": "MORE_LIMIT", "start_ms": int(start.timestamp() * 1000),
                "end_ms": int(end.timestamp() * 1000), "user_type": F.UT_GUEST_PERIOD,
                "date": F.access_date_period(start, end)}
    raise ValueError("Choose when this person can get in.")


def _lock_date(u: dict, tz) -> str:
    """The lock's 10-byte validity field for an existing cloud user record."""
    tt = u.get("timeType")
    if tt == "WEEK":
        conv = lambda ms: datetime.fromtimestamp(int(ms) / 1000, tz)
        s, e = conv(u.get("startTime") or 0), conv(u.get("endTime") or 0)
        return F.access_date_weekly(int(u.get("weekData") or 0), (s.hour, s.minute), (e.hour, e.minute))
    if tt in ("MORE_LIMIT", "ONE_LIMIT") and u.get("startTime") and u.get("endTime"):
        conv = lambda ms: datetime.fromtimestamp(int(ms) / 1000, tz)
        return F.access_date_period(conv(u["startTime"]), conv(u["endTime"]))
    return F.access_date_permanent()


def _keep_until(cfg: dict, lock: dict) -> str | None:
    """When a keep-unlocked window set from here ends, in the lock's own time
    ("18:30", "08:30 tomorrow"), or None once it's over."""
    until = ((cfg.get("keep_unlocked") or {}).get(lock["ID"]) or {}).get("until") or 0
    if until <= time.time():
        return None
    tz = lock_tz(lock)
    end, now = datetime.fromtimestamp(until, tz), datetime.now(tz)
    return end.strftime("%H:%M") + (" tomorrow" if end.date() > now.date() else "")


def _keep_max_minutes(lock: dict) -> int:
    """The longest keep-unlocked window this lock can take now: until 23:59 its
    time, unless windows past midnight are switched on."""
    if F.KEEP_UNLOCKED_PAST_MIDNIGHT:
        return 24 * 60 - 1
    now = datetime.now(lock_tz(lock))
    return 23 * 60 + 59 - (now.hour * 60 + now.minute)


def _favorite_ids(cfg: dict) -> list[str]:
    """Lock ids pinned as favorites, in the order they were added."""
    favs = cfg.get("favorites")
    return [f for f in favs if isinstance(f, str)] if isinstance(favs, list) else []


class Api:
    def __init__(self, client_factory=LocklyClient) -> None:
        self._factory = client_factory
        self._client: LocklyClient | None = None
        self._fw_jobs: dict[str, dict] = {}     # lock id → firmware install in progress or done
        self._fw_lock = threading.Lock()

    # ── session ──────────────────────────────────────────────────────────

    def session(self) -> dict:
        """Sign in silently with the saved login, if there is one."""
        cfg = config.load_config()
        email, pw_hash = cfg.get("email"), config.load_credential(cfg)
        if not (email and pw_hash):
            return {"signedIn": False, "email": email or ""}
        client = self._factory(email)
        try:
            client.login_with_hash(pw_hash)
            client.load_devices()
        except LocklyError as e:
            return {"signedIn": False, "email": email,
                    "message": f"Your saved sign-in no longer works ({e}). Sign in again."}
        except Exception as e:  # noqa: BLE001 - offline etc.
            return {"signedIn": False, "email": email,
                    "message": f"Couldn't reach Lockly: {e}"}
        if not cfg.get("pw_in_keyring") and config._keyring():
            config._save_credential(cfg, email, pw_hash)   # migrate file → keyring
        self._set_client(client)
        return {"signedIn": True, "email": email}

    def sign_in(self, email: str, password: str, remember: bool) -> dict:
        email = (email or "").strip()
        if not email or not password:
            raise ValueError("Enter your Lockly email and password.")
        client = self._factory(email)
        client.login(password)
        client.load_devices()
        cfg = config.load_config()
        where = config._save_credential(cfg, email, client.pw_hash if remember else None)
        self._set_client(client)
        return {"signedIn": True, "email": email, "storedIn": where}

    def sign_out(self) -> dict:
        """Forget the saved login and end the session."""
        cfg = config.load_config()
        config._save_credential(cfg, cfg.get("email", ""), None)
        self.close()
        return {"signedIn": False, "email": cfg.get("email", "")}

    def close(self) -> None:
        if self._client:
            try:
                self._client.close()
            finally:
                self._client = None

    def _set_client(self, client: LocklyClient) -> None:
        self.close()
        self._client = client

    # ── locks & status ───────────────────────────────────────────────────

    def _c(self) -> LocklyClient:
        if self._client is None:
            raise LocklyError("Not signed in.")
        return self._client

    def _lock(self, lock_id: str) -> dict:
        for lk in self._c().locks:
            if lk.get("ID") == lock_id:
                return lk
        raise LocklyError("That lock is no longer on this account.")

    def _view(self, lock: dict, cfg: dict | None = None) -> dict:
        cfg = config.load_config() if cfg is None else cfg
        name = str(lock.get("na") or lock.get("blename") or lock.get("ID", "?"))
        unit, rest = split_unit(name)
        e = self._c().status_cache.get(lock["ID"]) or {}
        if not e:
            state = "unknown"
        elif not e.get("reachable", True):
            state = "asleep"
        elif e.get("is_locked") is None:
            state = "unknown"
        else:
            state = "locked" if e["is_locked"] else "unlocked"
        return {
            "id": lock["ID"], "name": name, "unit": unit, "rest": rest,
            "model": lock.get("mod") or "", "hub": lock.get("hubid") or "",
            "state": state,
            "offline": state == "asleep" and bool(e.get("offline")),   # Lockly's server said so (3005)
            "battery": e.get("battery_percent"),
            "lowBattery": bool(e.get("low_battery") or e.get("battery_invalid")),
            "doorOpen": e.get("door_sensor_open"),
            "firmware": e.get("firmware_version") or str(lock.get("fwv") or ""),
            "cached": bool(e.get("cached")),
            "checkedAt": e.get("ts"),
            "welcomeForm": F.welcome_form(lock),
            "route": F.remote_route(lock),
            "eventLog": F.uses_event_log(lock),
            "faceCapable": F.supports_face(lock),
            "favorite": lock["ID"] in _favorite_ids(cfg),
            "keepUnlockedUntil": _keep_until(cfg, lock),
            "keepMaxMinutes": _keep_max_minutes(lock),
        }

    def locks(self) -> list[dict]:
        """All locks, with the silent hub-cached status where a hub has one."""
        c = self._c()
        cfg = config.load_config()
        self._warm_up(_favorite_ids(cfg))
        c.prime_cached_status(c.locks)
        return [self._view(lk, cfg) for lk in c.locks]

    def check(self, lock_id: str) -> dict:
        """Live status for one lock (wakes it, and it beeps). The GUI asks
        several doors at once with parallel calls, so each door's answer
        shows as soon as it's in."""
        lock = self._lock(lock_id)
        self._c().query_status(lock)
        return self._view(lock)

    def lock(self, lock_id: str) -> dict:
        lock = self._lock(lock_id)
        ok, detail = self._c().lock(lock)
        if ok:
            self._note_state(lock, locked=True)
        return {"ok": ok, "detail": detail, "lock": self._view(lock)}

    def unlock(self, lock_id: str) -> dict:
        lock = self._lock(lock_id)
        ok, detail = self._c().unlock(lock)
        if ok:
            self._note_state(lock, locked=False)
        return {"ok": ok, "detail": detail, "lock": self._view(lock)}

    def _note_state(self, lock: dict, *, locked: bool) -> None:
        """Record an acknowledged lock/unlock so the plate updates at once."""
        cache = self._c().status_cache
        entry = {k: v for k, v in (cache.get(lock["ID"]) or {}).items() if k != "cached"}
        cache[lock["ID"]] = {**entry, "reachable": True, "is_locked": locked, "ts": time.time()}

    # ── favorites: doors pinned to the top for one-tap lock/unlock ──────

    def set_favorite(self, lock_id: str, on: bool) -> dict:
        """Pin a door to (or unpin it from) the favorites. Stored only in this
        device's config; nothing on the lock or in Lockly's cloud changes."""
        lock = self._lock(lock_id)
        cfg = config.load_config()
        favs = [f for f in _favorite_ids(cfg) if f != lock_id]
        if on:
            if F.remote_route(lock) == "bluetooth":
                raise ValueError("This door has no hub, so it can only be opened over Bluetooth.")
            favs.append(lock_id)
        cfg["favorites"] = favs
        config.save_config(cfg)
        self._warm_up(favs)
        return self._view(lock, cfg)

    def _warm_up(self, favs: list[str]) -> None:
        """Connect Lockly's device channel in the background when a favorite
        is a Wi-Fi lock, so its first lock/unlock doesn't wait for it."""
        c = self._c()
        if any(F.remote_route(lk) == "wifi" for lk in c.locks if lk["ID"] in favs):
            threading.Thread(target=c.connect_device_channel, name="locklyctl-warm-up",
                             daemon=True).start()

    # ── activity & guests ────────────────────────────────────────────────

    def activity(self, lock_id: str) -> list[dict]:
        """The last 7 days, newest first, from where the app reads them: a
        Wi-Fi Visage's own event log, otherwise getlkhist. Cloud only."""
        lock = self._lock(lock_id)
        since = int((datetime.now(timezone.utc) - timedelta(days=7)).timestamp() * 1000)
        if F.uses_event_log(lock):
            events = self._c().get_event_log(lock, since)
        else:
            res = self._c().get_history(lock, since)
            if res is None:
                raise LocklyError("Lockly didn't return the access log.")
            events = res[0]
        # Both are {tm: epoch ms, co: event code, na: name, pid, id} (getlkhist
        # as seen live; get_event_log reshapes the event log to match).
        return [{"time": _fmt_ms(ev.get("tm")) or "", "what": P.describe_open_type(ev.get("co")),
                 "who": ev.get("na") or ""}
                for ev in sorted(events, key=lambda e: e.get("tm") or 0, reverse=True)]

    def guests(self, lock_id: str) -> list[dict]:
        lock = self._lock(lock_id)
        gs = self._c().list_guests(lock)
        if gs is None:
            raise LocklyError("Lockly didn't return the guest list.")
        return [{"id": g.get("userAcuId"), "name": g.get("name") or "",
                 "pin": g.get("pw") or "", "start": _fmt_ms(g.get("startTime")),
                 "end": _fmt_ms(g.get("endTime"))} for g in gs]

    def add_guest(self, lock_id: str, name: str, pin: str, duration: str) -> dict:
        lock = self._lock(lock_id)
        name, pin = (name or "").strip(), (pin or "").strip()
        if not name:
            raise ValueError("Give the guest a name.")
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValueError("The PIN must be 4 to 8 digits.")
        permanent = duration == "permanent"
        if not permanent and duration not in _DURATIONS_H:
            raise ValueError("Choose how long the PIN works.")
        start = int(time.time() * 1000)
        end = start + (100 * 365 * 24 if permanent else _DURATIONS_H[duration]) * 3600 * 1000
        body = self._c().add_guest_pin(lock, name, pin, start, end, permanent=permanent)
        return {"ok": True, "id": (body or {}).get("userAcuId")}

    def delete_guest(self, lock_id: str, user_id: int) -> dict:
        lock = self._lock(lock_id)
        if not self._c().delete_guest(lock, int(user_id)):
            raise LocklyError("Lockly didn't delete that guest PIN.")
        return {"ok": True}

    # ── people (all access a person has, across every door) ──────────────

    def people(self) -> list[dict]:
        """Everyone with access to any door, merged across doors by email (or
        by name when there's no email), each with their access per door.
        Cloud data only (acu/crdntl/list per lock) — no lock is woken."""
        from concurrent.futures import ThreadPoolExecutor
        c = self._c()

        def fetch(lk):
            try:
                return lk, c.list_users(lk)
            except Exception as e:  # noqa: BLE001 - one door must not hide the rest
                _LOG.warning("user list failed for %s: %s", lk.get("na"), e)
                return lk, None

        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(fetch, c.locks))

        cfg = config.load_config()
        merges = cfg.get("people_merges") or {}
        people: dict[str, dict] = {}
        failed = []
        for lk, users in results:
            door = self._view(lk, cfg)
            if users is None:
                failed.append(door["name"])
                continue
            for u in users:
                entry = person_access(u, door, lock_tz(lk))
                own_key = entry.pop("_key")
                key = merges.get(own_key, own_key)
                p = people.setdefault(key, {"key": key, "name": entry["person"],
                                            "email": u.get("email") or "", "doors": [],
                                            "mergedKeys": []})
                if own_key != key and own_key not in p["mergedKeys"]:
                    p["mergedKeys"].append(own_key)
                if own_key == key:                       # the target names the person
                    p["name"] = entry["person"]
                    p["email"] = u.get("email") or p["email"]
                entry["recordKey"] = own_key
                p["doors"].append(entry)
        out = sorted(people.values(), key=lambda p: (p["name"] or "~").lower())
        for p in out:
            p["doors"].sort(key=lambda d: d["doorName"].lower())
            p["isOwner"] = any(d["role"] == "owner" for d in p["doors"])
        return {"people": out, "failedDoors": failed}

    # -- editing a person's access on one door -------------------------------

    def _lock_record(self, lock: dict, user_id: int) -> dict | None:
        """The lock's own record for this user (slot, role byte, PIN), or None
        if the user exists only in the cloud. Wakes the lock."""
        return next((u for u in self._c().lock_user_table(lock)
                     if u["user_id"] == int(user_id) & 0xFFFF), None)

    def _cloud_record(self, lock: dict, user_id: int) -> dict:
        u = next((x for x in self._c().list_users(lock) if x.get("userAcuId") == int(user_id)), None)
        if u is None:
            raise LocklyError("That person is no longer on this door.")
        return u

    def door_access_detail(self, lock_id: str, user_id: int) -> dict:
        """Names and ids of one person's fingerprints, faces and cards on a door
        (cloud only — nothing is woken)."""
        lock = self._lock(lock_id)
        o = self._c().user_detail(lock, int(user_id))
        def items(key, code_key):
            return [{"crdntlId": m.get("crdntlId"), "slot": m.get("pid"),
                     "code": m.get(code_key), "name": m.get("name") or ""}
                    for m in (o.get(key) or [])]
        return {"fingerprints": items("fpList", "fpCode1"), "faces": items("faceList", "faceCode"),
                "cards": items("rfidList", "fpCode1"),
                "hasPin": bool((o.get("pwd") or {}).get("password"))}

    def add_to_door(self, lock_id: str, name: str, pin: str, access: dict) -> dict:
        """Give a person access to a door with a PIN. ``access`` is
        {"kind": "always"} | {"kind": "weekly", "days": [...], "start": "HH:MM", "end": "HH:MM"}
        | {"kind": "dates", "from": "YYYY-MM-DDTHH:MM", "to": "..."} (lock's local time)."""
        lock = self._lock(lock_id)
        name, pin = (name or "").strip(), (pin or "").strip()
        if not name:
            raise ValueError("Give the person a name.")
        if pin and not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValueError("The PIN must be 4 to 8 digits.")
        spec = _access_spec(access, lock_tz(lock))
        r = self._c().create_access_user(lock, name=name, pin=pin, **spec)
        return {"ok": True, "userId": r["userAcuId"], "slot": r["pwdId"]}

    def change_pin(self, lock_id: str, user_id: int, pin: str) -> dict:
        lock = self._lock(lock_id)
        pin = (pin or "").strip()
        if not (pin.isdigit() and 4 <= len(pin) <= 8):
            raise ValueError("The PIN must be 4 to 8 digits.")
        cloud = self._cloud_record(lock, user_id)
        if cloud.get("type") == "OWNER":
            raise ValueError("Change the owner's PIN in the Lockly app.")
        rec = self._lock_record(lock, user_id)
        if rec is None:
            raise LocklyError("This person isn't on the lock yet (only in Lockly's cloud).")
        cloud = {**cloud, "acuIndex": rec["pwd_id"]}
        self._c().change_access_user_pin(lock, cloud, pin, user_type=rec["user_type"],
                                         date=_lock_date(cloud, lock_tz(lock)))
        return {"ok": True}

    def remove_from_door(self, lock_id: str, user_id: int) -> dict:
        """Take a person off one door: lock record (0x73 delete) then cloud."""
        lock = self._lock(lock_id)
        cloud = self._cloud_record(lock, user_id)
        if cloud.get("type") == "OWNER":
            raise ValueError("The owner can't be removed from their own lock.")
        rec = self._lock_record(lock, user_id)
        if rec is None:                       # cloud-only draft: nothing on the lock
            self._c()._rest_ok("acu/delete", {"adminAcuId": 0, "dv": lock["ID"],
                                              "userAcuId": int(user_id)})
        else:
            self._c().delete_access_user(lock, user_acu_id=int(user_id), pwd_id=rec["pwd_id"],
                                         user_type=rec["user_type"])
        return {"ok": True}

    def remove_credential(self, lock_id: str, user_id: int, kind: str, crdntl_id: int,
                          code: int) -> dict:
        """Remove one fingerprint / face / card from a person on one door."""
        lock = self._lock(lock_id)
        sensor_type = {"fingerprint": F.SENSOR_FINGERPRINT, "face": F.SENSOR_FACE,
                       "card": F.SENSOR_RFID}[kind]
        rec = self._lock_record(lock, user_id)
        if rec is None:
            raise LocklyError("This person isn't on the lock yet (only in Lockly's cloud).")
        self._c().delete_sensor(lock, user_acu_id=int(user_id), pwd_id=rec["pwd_id"],
                                sensor_type=sensor_type, code=int(code), crdntl_id=crdntl_id)
        return {"ok": True}

    def merge_people(self, keys: list[str], into: str) -> dict:
        """Show several people as one (e.g. the same person spelled differently
        on different doors). Stored only in this computer's config; nothing on
        the locks or in Lockly's cloud changes."""
        cfg = config.load_config()
        merges = cfg.setdefault("people_merges", {})
        into = merges.get(into, into)
        for k in keys:
            if k and k != into:
                merges[k] = into
                for alias, target in list(merges.items()):   # re-point anything merged into k
                    if target == k:
                        merges[alias] = into
        config.save_config(cfg)
        return self.people()

    def separate_person(self, key: str) -> dict:
        """Undo merges into (or of) this person."""
        cfg = config.load_config()
        merges = cfg.get("people_merges") or {}
        cfg["people_merges"] = {a: t for a, t in merges.items() if t != key and a != key}
        config.save_config(cfg)
        return self.people()

    # ── keep a door unlocked for a while (one-time Welcome Mode slots) ────

    _SLOT_KEYS = ("week", "begin", "end", "enabled", "sync")

    def _welcome8_table(self, lock: dict) -> list[dict]:
        """The lock's 0x9B slots with their on/off and entry bits (wakes it)."""
        ack, d = self._c().exchange_built(lock, lambda n: F.build_welcome8_query(lock, n))
        w = F.parse_welcome8(d) if d else None
        if not w or w["status"] != "00" or any("enabled" not in s for s in w["segments"]):
            raise LocklyError("The lock didn't send its Welcome Mode schedule: "
                              + F.check_reply(ack, lock)[1])
        return [{k: s[k] for k in self._SLOT_KEYS} for s in w["segments"]]

    @staticmethod
    def _slot_key(s: dict) -> list[int]:
        return [s["week"], *s["begin"], *s["end"]]

    def keep_unlocked(self, lock_id: str, minutes: int) -> dict:
        """Keep a door unlocked from now for ``minutes`` with one-time Welcome
        Mode slots that the lock runs and then switches off by itself (8-slot
        locks). They replace any earlier window set from here; the door's other
        slots are written back unchanged. Wakes it."""
        lock = self._lock(lock_id)
        if F.welcome_form(lock) != F.WELCOME_9B:
            raise ValueError("Keeping a door unlocked for a while needs an 8-slot lock (Visage or Vision).")
        if F.remote_route(lock) == "bluetooth":
            raise ValueError("This door has no hub, so it can only be changed over Bluetooth.")
        start = datetime.now(lock_tz(lock))
        new = F.plan_keep_unlocked(start, int(minutes))
        cfg = config.load_config()
        mine = ((cfg.get("keep_unlocked") or {}).get(lock["ID"]) or {}).get("slots") or []
        # One-time slots this app added earlier go, whether the lock has run them
        # yet or not: left on, an earlier window's slot from 00:00 would still
        # open the door with nothing here tracking it. Anything else stays.
        table = [s for s in self._welcome8_table(lock) if self._slot_key(s) not in mine]
        if len(table) + len(new) > 8:
            raise LocklyError("All 8 Welcome Mode slots on this door are in use; "
                              "clear some in the Schedules tab first.")
        ok, detail = self._c().send_built(lock, lambda n: F.build_welcome8_set(lock, n, table + new))
        if not ok:
            raise LocklyError(detail)
        eh, em = new[-1]["end"]
        until = (start + timedelta(days=len(new) - 1)).replace(hour=eh, minute=em, second=0, microsecond=0)
        cfg.setdefault("keep_unlocked", {})[lock["ID"]] = {
            "until": int(until.timestamp()), "slots": [self._slot_key(s) for s in new]}
        config.save_config(cfg)
        return {"ok": True, "until": _keep_until(cfg, lock), "detail": detail,
                "lock": self._view(lock, cfg)}

    def end_keep_unlocked(self, lock_id: str) -> dict:
        """End a keep-unlocked window early: take its slots off the lock, then
        lock the door."""
        lock = self._lock(lock_id)
        cfg = config.load_config()
        rec = (cfg.get("keep_unlocked") or {}).get(lock["ID"])
        if rec:
            table = [s for s in self._welcome8_table(lock) if self._slot_key(s) not in rec["slots"]]
            ok, detail = self._c().send_built(lock, lambda n: F.build_welcome8_set(lock, n, table))
            if not ok:
                raise LocklyError(detail)
            cfg["keep_unlocked"].pop(lock["ID"], None)
            config.save_config(cfg)
        ok, detail = self._c().lock(lock)
        if ok:
            self._note_state(lock, locked=True)
        return {"ok": ok, "detail": detail, "lock": self._view(lock, cfg)}

    # ── experimental: Welcome Mode, Rain Mode ────────────────────────────

    def _experimental(self, lock: dict, build, needs_nonce: bool, send: bool) -> dict:
        if not send:
            return {"ok": True, "sent": False, "frame": build("0" * 32)}
        ok, detail = self._c().send_built(lock, build, needs_nonce=needs_nonce)
        return {"ok": ok, "sent": True, "detail": detail}

    def welcome(self, lock_id: str, form: str | None, action: str, days: list[str],
                start: str, end: str, send: bool) -> dict:
        lock = self._lock(lock_id)
        form = form or F.welcome_form(lock)
        if form not in (F.WELCOME_9B, F.WELCOME_46):
            raise ValueError("Choose which Welcome Mode this lock uses.")
        week, begin, finish = 0, (0, 0), (0, 0)
        if action == "set":
            week = F.week_byte(list(days or []))
            begin, finish = F.parse_hhmm(start), F.parse_hhmm(end)
            if finish <= begin:
                raise ValueError("The end time must be after the start time.")
        _what, build, needs_nonce = F.welcome_plan(lock, form, action, week, begin, finish)
        return {**self._experimental(lock, build, needs_nonce, send), "form": form}

    def rain(self, lock_id: str, on: bool, send: bool) -> dict:
        lock = self._lock(lock_id)
        return self._experimental(lock, lambda n: F.build_rain_mode_set(lock, n, bool(on)),
                                  True, send)

    def rain_state(self, lock_id: str) -> dict:
        """Read Rain Mode from the lock (read-only; wakes it)."""
        lock = self._lock(lock_id)
        return self._experimental(lock, lambda n: F.build_rain_mode_query(lock, n), True, True)

    def welcome_state(self, lock_id: str, form: str | None) -> dict:
        """Read the Welcome Mode schedule from the lock (read-only; wakes it)."""
        return self.welcome(lock_id, form, "query", [], "", "", True)

    # ── firmware ─────────────────────────────────────────────────────────

    _PART_NAMES = {"main": "Lock firmware", "front": "Front board", "back": "Back board",
                   "wifi": "Wi-Fi module", "ble": "Bluetooth chip", "screen": "Screen",
                   "camera": "Camera", "gateway": "Camera gateway"}

    def firmware(self, lock_id: str) -> dict:
        """Newer firmware Lockly offers for this lock, next to what the lock
        reports running, and what installing over Wi-Fi would send. Asks the
        lock for its versions, so it wakes (beeps)."""
        lock = self._lock(lock_id)
        with self._fw_lock:
            job = self._fw_jobs.get(lock["ID"]) or {}
            if job.get("running"):
                raise ValueError(f"A firmware install is running on this door: {job['text'] or 'starting'}.")
        plan = self._c().firmware_plan(lock)
        st, offer = plan["status"], plan["offer"]
        multi = offer["kind"] == F.FW_MULTI_PART
        on_lock = F.firmware_display_versions(lock, st, offer["kind"])
        parts = [{"name": self._PART_NAMES.get(p["part"], p["part"]), "offered": p["version"],
                  "current": on_lock.get(p["part"]) or "",
                  "newer": F.version_lower(on_lock.get(p["part"]), p["version"]),
                  "sizeKb": p["size_kb"],
                  "byPhone": not multi or (p["part"] == "ble" and F.bluetooth_chip_needs_phone(lock))}
                 for p in offer["parts"]]
        send = [{"part": p["part"], "name": self._PART_NAMES[p["part"]], "version": p["version"],
                 "current": on_lock.get(p["part"]) or ""} for p in plan["send"]]
        return {"kind": offer["kind"], "release": offer["release"], "title": offer["title"],
                "notes": offer["notes"], "parts": parts, "lockFirmware": on_lock["main"],
                "answered": bool(st), "install": {"parts": send, "blocked": plan["blocked"]},
                "lock": self._view(lock)}

    def firmware_install(self, lock_id: str, expected: list[dict]) -> dict:
        """Start installing over Wi-Fi what the last check showed (``expected``:
        its install parts). Checks the lock and Lockly again first and refuses
        if anything changed. Runs in the background: poll firmware_install_status."""
        lock = self._lock(lock_id)
        with self._fw_lock:
            if (self._fw_jobs.get(lock["ID"]) or {}).get("running"):
                raise ValueError("A firmware install is already running on this door.")
        plan = self._c().firmware_plan(lock)
        if plan["blocked"]:
            raise ValueError(plan["blocked"])
        now = [(p["part"], p["version"]) for p in plan["send"]]
        if now != [(e.get("part"), e.get("version")) for e in expected]:
            raise ValueError("What the lock runs or Lockly offers changed since the check. Check again.")
        job = {"running": True, "ok": None, "stage": "start", "progress": None,
               "text": "Sending the request", "error": "", "versions": {},
               "parts": [{"name": self._PART_NAMES[p["part"]], "version": p["version"]} for p in plan["send"]]}
        with self._fw_lock:
            if (self._fw_jobs.get(lock["ID"]) or {}).get("running"):
                raise ValueError("A firmware install is already running on this door.")
            self._fw_jobs[lock["ID"]] = job
        threading.Thread(target=self._run_firmware_install, args=(lock, plan, job),
                         name="locklyctl-firmware", daemon=True).start()
        return self.firmware_install_status(lock_id)

    def firmware_install_status(self, lock_id: str) -> dict:
        """Where the firmware install on this door is: {"running", "ok", "stage",
        "progress", "text", "error", "parts", "versions"}, or running False and
        ok None when none was started."""
        lock = self._lock(lock_id)
        with self._fw_lock:
            job = self._fw_jobs.get(lock["ID"])
            return dict(job) if job else {"running": False, "ok": None, "stage": "", "progress": None,
                                          "text": "", "error": "", "parts": [], "versions": {}}

    def _run_firmware_install(self, lock: dict, plan: dict, job: dict) -> None:
        """Thread entry point: runs the install and records how it ended."""
        def on_event(event: dict) -> None:
            with self._fw_lock:
                job.update(stage=event.get("stage"), progress=event.get("progress"),
                           text=event.get("text") or "")
        try:
            result = self._c().firmware_install(lock, plan["send"], plan["downloadUrl"], on_event)
        except (LocklyError, ValueError) as e:
            outcome = {"ok": False, "error": str(e)}
        except Exception as e:          # thread boundary: record it, or the page would wait forever
            _LOG.exception("firmware install on %s stopped", lock.get("ID"))
            outcome = {"ok": False, "error": f"It stopped with an unexpected error: {e}"}
        else:
            outcome = {"ok": True, "versions": result["versions"]}
        with self._fw_lock:
            job.update(running=False, **outcome)

    # ── connection test ──────────────────────────────────────────────────

    def diagnose(self, lock_id: str) -> dict:
        lock = self._lock(lock_id)
        c = self._c()
        cached = c.cached_status(lock) if lock.get("hubid") else None
        live = c.query_status(lock)
        return {"model": lock.get("mod") or "", "hub": lock.get("hubid") or "",
                "cachedOk": bool(cached), "liveOk": bool(live),
                "trail": c.last_transport_trail(), "lock": self._view(lock)}
