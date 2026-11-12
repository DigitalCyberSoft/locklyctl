"""MCP server: let an AI assistant (Claude Desktop, Claude Code, …) read and
run your Lockly doors through locklyctl.

    locklyctl-mcp                    # read-only, plus locking doors
    locklyctl-mcp --allow-writes     # also manage people, PINs and schedules
    locklyctl-mcp --allow-unlock     # also unlock doors

It speaks MCP over stdio and signs in with the login saved by ``locklyctl`` or
the desktop app (sign in there once with "remember me"). Tools that a flag
doesn't allow are not offered at all, so the assistant never sees them.

Existing PINs are never returned. A PIN this server creates is returned once,
in the reply to the call that created it.
"""
from __future__ import annotations

import argparse
import secrets
import sys
import threading
from datetime import datetime
from typing import Any, Literal

import anyio

try:                                            # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import ToolAnnotations
except ImportError:                             # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server
    from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations

from . import __version__, config
from .gui.api import Api

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
RISKY = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

INSTRUCTIONS = """\
Controls the user's Lockly smart locks (real doors in an occupied building).
Refer to doors by the name list_doors shows (or its unit, like "3C").
check_door and the read_* tools wake the lock, which beeps; list_doors does not.
Doors whose connection is "bluetooth" have no hub and can't be reached remotely.
Never unlock a door, or change who can get in, unless the user clearly asked for it."""

_PRIVATE = {"pin", "recordKey", "weekData", "startTime", "endTime", "timeType", "status",
            "doorUnit", "doorRest", "doorModel", "doorRoute"}


def _when(ts) -> str | None:
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds") if ts else None


def _door(v: dict) -> dict:
    return {"id": v["id"], "name": v["name"], "model": v["model"],
            "connection": v.get("route") or "", "state": v["state"],
            "battery": v["battery"], "lowBattery": v["lowBattery"], "doorOpen": v["doorOpen"],
            "checkedAt": _when(v["checkedAt"]), "statusFromHubCache": v["cached"]}


class Doors:
    """The signed-in Api plus name lookups. Methods block; tools run them in threads."""

    def __init__(self, api: Api | None = None) -> None:
        self.api = api or Api()
        self._lock = threading.Lock()
        self._ready = False

    def _signed_in(self) -> Api:
        with self._lock:
            if not self._ready:
                s = self.api.session()
                if not s.get("signedIn"):
                    raise ToolError(s.get("message") or
                                    "Not signed in. Run `locklyctl` (or the desktop app) once, "
                                    "sign in and choose to remember the login.")
                self._ready = True
        return self.api

    def views(self) -> list[dict]:
        return self._signed_in().locks()

    def find(self, door: str) -> dict:
        views, want = self.views(), (door or "").strip().casefold()
        for match in (lambda v: v["id"].casefold() == want, lambda v: v["name"].casefold() == want,
                      lambda v: v["unit"].casefold() == want,
                      lambda v: want in v["name"].casefold()):
            hits = [v for v in views if match(v)]
            if len(hits) == 1:
                return hits[0]
            if hits:
                raise ToolError(f"{door!r} matches several doors: "
                                + ", ".join(v["name"] for v in hits) + ". Use the full name.")
        raise ToolError(f"No door called {door!r}. Doors: " + ", ".join(v["name"] for v in views))

    def remote(self, door: str) -> dict:
        v = self.find(door)
        if v.get("route") == "bluetooth":
            raise ToolError(f"{v['name']} has no hub, so it can only be reached over Bluetooth.")
        return v

    def person_on(self, door: dict, person: str) -> dict:
        want = (person or "").strip().casefold()
        out = self._signed_in().people()
        hits = [d for p in out["people"] for d in p["doors"] if d["lockId"] == door["id"]
                and want in (str(d["userId"]), p["name"].casefold(), p["email"].casefold(),
                             d["person"].casefold())]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise ToolError(f"Several people on {door['name']} match {person!r}; "
                            f"use their userId: " + ", ".join(f"{d['person']} ({d['userId']})" for d in hits))
        raise ToolError(f"Nobody called {person!r} has access to {door['name']}.")

    def call(self, fn, *args) -> Any:
        """Run an Api call, turning its errors into messages the assistant can read."""
        self._signed_in()
        try:
            return fn(*args)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - LocklyError, ValueError, network
            raise ToolError(str(e) or e.__class__.__name__) from e


def _clean_access(d: dict) -> dict:
    return {k: v for k, v in d.items() if k not in _PRIVATE}


def _access_arg(kind: str, days: list[str] | None, start: str | None, end: str | None,
                date_from: str | None, date_to: str | None) -> dict:
    if kind == "always":
        return {"kind": "always"}
    if kind == "weekly":
        return {"kind": "weekly", "days": days or [], "start": start or "", "end": end or ""}
    return {"kind": "dates", "from": date_from or "", "to": date_to or ""}


def build_server(doors: Doors | None = None, *, allow_writes: bool = False,
                 allow_unlock: bool = False):
    doors = doors or Doors()
    try:
        mcp = _Server("locklyctl", instructions=INSTRUCTIONS, version=__version__)
    except TypeError:                           # mcp 1.x's FastMCP has no version
        mcp = _Server("locklyctl", instructions=INSTRUCTIONS)

    async def run(fn, *args):
        return await anyio.to_thread.run_sync(lambda: doors.call(fn, *args))

    # ── read ─────────────────────────────────────────────────────────────

    @mcp.tool(annotations=READ)
    async def list_doors() -> list[dict]:
        """All doors with their last known state. Doesn't wake any lock: state
        comes from the hub's cache (or the last check) and may be stale."""
        return [_door(v) for v in await anyio.to_thread.run_sync(doors.views)]

    @mcp.tool(annotations=READ)
    async def check_door(door: str) -> dict:
        """Live status of one door: locked/unlocked, battery, door sensor. Wakes
        the lock (it beeps)."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        return _door(await run(doors.api.check, v["id"]))

    @mcp.tool(annotations=READ)
    async def door_activity(door: str, limit: int = 30) -> list[dict]:
        """Who opened a door and how, newest first, over the last 7 days."""
        v = await anyio.to_thread.run_sync(doors.find, door)
        return (await run(doors.api.activity, v["id"]))[:max(1, min(limit, 200))]

    @mcp.tool(annotations=READ)
    async def list_people(door: str | None = None, name: str | None = None) -> dict:
        """Everyone with access, merged across doors, with what they can use on
        each door (PIN yes/no, fingerprints, faces, cards) and when. Filter by a
        door and/or part of a name or email. Reads Lockly's cloud; no lock is woken."""
        v = await anyio.to_thread.run_sync(doors.find, door) if door else None
        out = await run(doors.api.people)
        people = []
        for p in out["people"]:
            if name and name.casefold() not in (p["name"] + " " + p["email"]).casefold():
                continue
            ds = [_clean_access(d) for d in p["doors"] if not v or d["lockId"] == v["id"]]
            if ds:
                people.append({"name": p["name"], "email": p["email"], "isOwner": p["isOwner"],
                               "doors": ds})
        return {"people": people, "doorsThatFailedToLoad": out["failedDoors"]}

    @mcp.tool(annotations=READ)
    async def read_door_modes(door: str) -> dict:
        """Read Welcome Mode (auto-unlock schedule) and Rain Mode from the lock
        itself. Wakes the lock."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        welcome = await run(doors.api.welcome_state, v["id"], None)
        rain = await run(doors.api.rain_state, v["id"])
        return {"door": v["name"], "welcomeMode": welcome.get("detail"), "rainMode": rain.get("detail")}

    # ── lock (always allowed: it only secures a door) ────────────────────

    @mcp.tool(annotations=WRITE)
    async def lock_door(door: str) -> dict:
        """Lock a door."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        r = await run(doors.api.lock, v["id"])
        return {"ok": r["ok"], "detail": r["detail"], "door": _door(r["lock"])}

    if allow_unlock:
        @mcp.tool(annotations=RISKY)
        async def unlock_door(door: str) -> dict:
            """Unlock a door. Only when the user explicitly asked to unlock this door."""
            v = await anyio.to_thread.run_sync(doors.remote, door)
            r = await run(doors.api.unlock, v["id"])
            return {"ok": r["ok"], "detail": r["detail"], "door": _door(r["lock"])}

    if not allow_writes:
        return mcp

    # ── people & PINs ────────────────────────────────────────────────────

    @mcp.tool(annotations=WRITE)
    async def grant_access(door: str, name: str,
                           kind: Literal["always", "weekly", "dates"] = "always",
                           days: list[str] | None = None, start: str | None = None,
                           end: str | None = None, date_from: str | None = None,
                           date_to: str | None = None, pin: str | None = None) -> dict:
        """Give a person a PIN on a Visage/Vision door (written to the lock and
        Lockly's cloud). kind "always"; "weekly" with days (mon..sun) and
        start/end "HH:MM"; or "dates" with date_from/date_to "YYYY-MM-DDTHH:MM".
        Times are the door's local time. Without a pin, a random 6-digit PIN
        is made and returned once."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        new_pin = pin or f"{secrets.randbelow(10**6):06d}"
        r = await run(doors.api.add_to_door, v["id"], name, new_pin,
                      _access_arg(kind, days, start, end, date_from, date_to))
        return {"ok": True, "door": v["name"], "userId": r["userId"], "slot": r["slot"],
                "pin": new_pin}

    @mcp.tool(annotations=WRITE)
    async def change_pin(door: str, person: str, pin: str | None = None) -> dict:
        """Change a person's PIN on one door (name, email or userId). Without
        a pin, a random 6-digit PIN is made and returned once."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        d = await anyio.to_thread.run_sync(doors.person_on, v, person)
        new_pin = pin or f"{secrets.randbelow(10**6):06d}"
        await run(doors.api.change_pin, v["id"], d["userId"], new_pin)
        return {"ok": True, "door": v["name"], "person": d["person"], "pin": new_pin}

    @mcp.tool(annotations=RISKY)
    async def revoke_access(door: str, person: str) -> dict:
        """Take a person off one door entirely (PIN, fingerprints, faces, cards).
        Faces can't be copied back; they must be enrolled again at the lock."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        d = await anyio.to_thread.run_sync(doors.person_on, v, person)
        await run(doors.api.remove_from_door, v["id"], d["userId"])
        return {"ok": True, "door": v["name"], "removed": d["person"]}

    # ── modes ────────────────────────────────────────────────────────────

    @mcp.tool(annotations=RISKY)
    async def set_welcome_mode(door: str, action: Literal["set", "off", "always"],
                               days: list[str] | None = None, start: str | None = None,
                               end: str | None = None) -> dict:
        """Welcome Mode keeps a door unlocked on a schedule. "set" with days
        (mon..sun) and start/end "HH:MM"; "off"; or "always" (never locks)."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        r = await run(doors.api.welcome, v["id"], None, action, days or [], start or "", end or "", True)
        return {"ok": r["ok"], "door": v["name"], "detail": r.get("detail")}

    @mcp.tool(annotations=WRITE)
    async def set_rain_mode(door: str, on: bool) -> dict:
        """Rain Mode turns the keypad off so rain can't press keys."""
        v = await anyio.to_thread.run_sync(doors.remote, door)
        r = await run(doors.api.rain, v["id"], on, True)
        return {"ok": r["ok"], "door": v["name"], "detail": r.get("detail")}

    return mcp


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="locklyctl-mcp", description=__doc__.split("\n\n")[0])
    ap.add_argument("--allow-writes", action="store_true",
                    help="also offer tools that change people, PINs and door modes")
    ap.add_argument("--allow-unlock", action="store_true", help="also offer unlock_door")
    ap.add_argument("--debug", action="store_true", help="protocol logging on stderr")
    a = ap.parse_args(argv)
    config._setup_logging(debug=a.debug)              # stderr + file; stdout is MCP's
    build_server(allow_writes=a.allow_writes, allow_unlock=a.allow_unlock).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
