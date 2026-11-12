"""MCP server tools, called in-process against the GUI tests' fake client."""
import asyncio
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
pytest.importorskip("mcp")

from locklyctl import config
from locklyctl.gui.api import Api
from locklyctl.mcp_server import Doors, ToolError, build_server
from test_gui_api import B2, UUID, FakeClient, _set_home


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as d:
        old, real_kr = os.environ.get("LOCKLYCTL_HOME"), config._keyring
        _set_home(d)
        config._keyring = lambda: None
        config.save_config({"email": "owner@example.com", "pw_hash": "H"})
        try:
            yield d
        finally:
            _set_home(old)
            config._keyring = real_kr


def _server(**flags):
    doors = Doors(Api(client_factory=FakeClient))
    return doors, build_server(doors, **flags)


def _names(server):
    return {t.name for t in asyncio.run(server.list_tools())}


def _call(server, tool, **args):
    """(is_error, value) — value is the tool's JSON result, or the error text.
    In-process, a tool's ToolError is raised; over MCP it becomes is_error."""
    try:
        r = asyncio.run(server.call_tool(tool, args))
    except ToolError as e:
        return True, str(e)
    if isinstance(r, tuple):                       # mcp 1.x: (content, structured)
        r = r[1] if r[1] is not None else r[0]
        return False, r.get("result", r) if isinstance(r, dict) else r
    text = r.content[0].text if r.content else ""
    if r.is_error:
        return True, text
    sc = r.structured_content
    return False, sc.get("result", sc) if isinstance(sc, dict) else json.loads(text)


def test_tools_offered_by_flag(home):
    reads = {"list_doors", "check_door", "door_activity", "list_people", "read_door_modes", "lock_door"}
    writes = {"grant_access", "change_pin", "revoke_access", "turn_off_welcome_mode", "set_rain_mode"}
    assert _names(_server()[1]) == reads
    assert _names(_server(allow_unlock=True)[1]) == reads | {"unlock_door"}
    assert _names(_server(allow_writes=True)[1]) == reads | writes
    # a schedule that keeps a door unlocked is an unlock: it needs both flags
    assert _names(_server(allow_writes=True, allow_unlock=True)[1]) == (
        reads | writes | {"unlock_door", "set_welcome_mode"})


def test_doors_and_lookup(home):
    _, s = _server()
    err, doors = _call(s, "list_doors")
    assert not err and [d["name"] for d in doors] == ["2A Office", "1204"]
    assert doors[0]["connection"] == "hub" and "pin" not in doors[0]
    err, msg = _call(s, "lock_door", door="2a")            # by unit, any case
    assert not err and msg["ok"] and msg["door"]["state"] == "locked"
    err, msg = _call(s, "lock_door", door="garage")
    assert err and "No door called 'garage'" in msg and "2A Office" in msg


def test_people_never_include_pins(home):
    _, s = _server()
    err, out = _call(s, "list_people")
    assert not err
    blob = json.dumps(out)
    assert '"pin"' not in blob and "1234" not in blob
    assert {p["name"] for p in out["people"]} >= {"Owner", "Dana"}
    err, out = _call(s, "list_people", door="1204", name="dana")
    assert not err and len(out["people"]) == 1 and out["people"][0]["doors"][0]["lockId"] == B2


def test_grant_access_makes_a_pin(home):
    doors, s = _server(allow_writes=True)
    err, out = _call(s, "grant_access", door="2A Office", name="Visitor", kind="weekly",
                     days=["mon", "fri"], start="09:00", end="17:00")
    assert not err and out["userId"] == 40 and len(out["pin"]) == 6 and out["pin"].isdigit()
    op, lock_id, kw = doors.api._client.calls[-1]
    assert (op, lock_id, kw["name"], kw["pin"], kw["time_type"]) == ("create", UUID, "Visitor", out["pin"], "WEEK")
    err, msg = _call(s, "grant_access", door="2A Office", name="Visitor", pin="12")
    assert err and "4 to 8 digits" in msg


def test_revoke_finds_person_on_door(home):
    doors, s = _server(allow_writes=True)
    err, out = _call(s, "revoke_access", door="2A Office", person="dana")
    assert not err and out["removed"] == "Dana"
    assert doors.api._client.calls[-1] == ("delete", 3, 4, 0x42)
    err, msg = _call(s, "revoke_access", door="2A Office", person="owner")
    assert err and "owner can't be removed" in msg


def test_not_signed_in(home):
    config.save_config({})
    _, s = _server()
    err, msg = _call(s, "list_doors")
    assert err and "Not signed in" in msg
