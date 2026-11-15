"""Offline codes: the code against the app's own answers, the seed's checks,
and the order the client asks Lockly in. No network or lock."""
import json
import logging
from pathlib import Path

import pytest
import requests

from locklyctl import config, oac
from locklyctl.gui.api import over_client
from locklyctl.transport import LocklyClient, LocklyError

# Codes the LOCKLY app 3.3.4's own methods gave for made-up seeds (see "source"
# in the file). The app's code was run outside this repo; only its answers are here.
VECTORS = json.loads((Path(__file__).parent / "oac_vectors.json").read_text())["cases"]

VISAGE = {"ID": "aa00112233445566778899cc", "mod": "PGK728WRHK", "na": "Test Door", "mc": "12345678"}
TOUCH = {"ID": "aa00112233445566778899dd", "mod": "PGD7Y", "na": "Side Door", "mc": "12345678"}


def _pairs(text, i_key="i", o_key="o"):
    return [{i_key: int(k), o_key: v} for k, v in (p.split(":", 1) for p in text.split(","))]


def _reply(case):
    """An oac/get reply holding a vector's seed, as the server shapes one."""
    return {"cod": "200",
            "oac": {"pairDate": 1700000000000, "pairTimeZone": "+8:00"},
            "oacnew": {"listA": _pairs(case["listA"]), "listC": _pairs(case["listC"]),
                       "numberC": case["numberC"]},
            "data": {"codeX1List": _pairs(case["codeX1List"]), "numD": case["numD"],
                     "codeXList": [{"idx": k, "obj": f"{k}{k}{k}{k}"} for k in range(10)],
                     "codeYList": [{"idx": k, "obj": f"{9 - k}{k}{k}{k}"} for k in range(10)],
                     "numB": 3, "numC": 1, "updatedTime": 1700000000000}}


@pytest.mark.parametrize("case", VECTORS, ids=[f"{c['kind']} from {c['numD']}" for c in VECTORS])
def test_codes_match_the_apps_own(case):
    seed = oac.from_reply(_reply(case))
    codes = []
    for _ in range(case["count"]):
        codes.append(oac.code(seed))
        seed = oac.after(seed)
    assert ",".join(codes) == case["codes"]
    assert ",".join(seed.b) == case["bAfter"]
    assert seed.index == case["indexAfter"]


def test_list_b_turns_over_after_position_99():
    seed = oac.from_reply(_reply(VECTORS[0]))
    seed = oac.after(type(seed)(**{**seed.__dict__, "index": 99}))
    assert seed.index == 0
    assert seed.b == tuple(f"{(int(v) + 37) % 100:02d}" for v in oac.from_reply(_reply(VECTORS[0])).b)
    past = oac.after(type(seed)(**{**seed.__dict__, "index": 150}))
    assert (past.index, past.b) == (151, seed.b)          # as the app: it only turns over at exactly 99


def test_a_seed_never_shows_its_lists():
    seed = oac.from_reply(_reply(VECTORS[1]))
    assert repr(seed) == "Seed(index=42)"
    assert VECTORS[1]["listA"].split(",")[0].split(":")[1] not in repr(seed)


@pytest.mark.parametrize("change, says", [
    (lambda r: r.pop("oacnew"), "no offline code seed"),
    (lambda r: r["data"].pop("codeX1List"), "no list B"),
    (lambda r: r["oacnew"]["listA"].pop(), "99 entries in list A, not 100"),
    (lambda r: r["oacnew"]["listC"].append({"i": 10, "o": "123"}), "11 entries in list C"),
    (lambda r: r["oacnew"]["listA"][3].update(o="4a"), "isn't digits"),
    (lambda r: r["oacnew"]["listA"][3].update(o=""), "isn't digits"),
    (lambda r: r["oacnew"]["listC"][0].pop("i"), "without a position"),
    (lambda r: r["oacnew"].update(numberC="x"), "isn't a number"),
    (lambda r: r["data"].update(numD=None), "isn't a number"),
])
def test_a_seed_that_isnt_whole_is_refused(change, says):
    reply = _reply(VECTORS[0])
    change(reply)
    with pytest.raises(oac.OacError, match=says):
        oac.from_reply(reply)


def test_request_bodies():
    seed = oac.after(oac.from_reply(_reply(VECTORS[0])))
    body = oac.usage_body(seed, "owner@example.test", VISAGE["ID"], 1234)
    assert body["numD"] == 1 and (body["numB"], body["numC"]) == (3, 1)
    assert body["codeX1List"][:2] == [{"i": 0, "o": seed.b[0]}, {"i": 1, "o": seed.b[1]}]
    assert body["codeXList"][2] == {"idx": 2, "obj": "2222"} and len(body["codeYList"]) == 10
    assert (body["acct"], body["dv"], body["uploadTime"]) == ("owner@example.test", VISAGE["ID"], 1234)
    assert oac.record_body(VISAGE["ID"], "Sam", "1234") == {
        "dv": VISAGE["ID"], "endate": 0, "na": "Sam", "oacType": "oac", "pw": "1234",
        "pwType": "OFFONE", "stdate": 0}
    assert oac.shown("4071") == "40 71"


def test_records_and_models():
    body = {"cod": "200", "obj": [{"id": 5, "acctname": "Sam", "pwType": "OFFONE", "password": "4071",
                                   "startTime": 0, "endTime": 0, "pwdId": 2, "deviceId": VISAGE["ID"]}]}
    assert oac.records(body) == [{"id": 5, "name": "Sam", "once": True, "code": "4071"}]
    assert oac.records({"cod": "200", "obj": []}) == []
    assert oac.supported(VISAGE) and oac.supported({"mod": "PGK798HK"}) and not oac.supported(TOUCH)


# ── the client: what it asks Lockly, in which order ─────────────────────────

class FakeLockly:
    """Answers the client's oac/* calls; ``answers`` overrides one endpoint."""

    def __init__(self, case=VECTORS[1], **answers):
        self.reply, self.calls, self.answers = _reply(case), [], answers

    def __call__(self, endpoint, body, **kw):
        self.calls.append((endpoint, body))
        answer = self.answers.get(endpoint)
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        if answer is not None:
            return answer
        if endpoint == "oac/get":
            return self.reply
        if endpoint == "oac/saveName":
            return {"cod": "200", "pwdid": 77}
        if endpoint == "oac/getNameList":
            return {"cod": "200", "obj": []}
        return {"cod": "200", "updatedTime": 1}

    def endpoints(self):
        return [e for e, _ in self.calls]


def _client(monkeypatch, tmp_path, server):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    c = LocklyClient("owner@example.test")
    c.locks = [VISAGE, TOUCH]
    c._post_para_des3 = server
    return c


def test_making_a_code_records_it_then_moves_on(monkeypatch, tmp_path, caplog):
    server = FakeLockly()
    c = _client(monkeypatch, tmp_path, server)
    caplog.set_level(logging.DEBUG)
    r = c.make_offline_code(VISAGE, "  Sam   (Saturday) ")
    want = VECTORS[1]["codes"].split(",")[0]
    assert r == {"code": want, "position": 42, "recordId": 77, "counterSaved": True, "problem": None}
    assert server.endpoints() == ["oac/get", "oac/saveName", "oac/usage/save"]
    record, usage = server.calls[1][1], server.calls[2][1]
    assert (record["na"], record["pw"], record["pwType"]) == ("Sam (Saturday)", want, "OFFONE")
    assert usage["numD"] == 43 and usage["dv"] == VISAGE["ID"]
    assert want not in caplog.text                     # the code isn't logged


def test_the_monthly_limit_uses_nothing_up(monkeypatch, tmp_path):
    server = FakeLockly(**{"oac/saveName": {"cod": "8001", "msg": "limit"}})
    c = _client(monkeypatch, tmp_path, server)
    with pytest.raises(LocklyError, match="reached the limit") as e:
        c.make_offline_code(VISAGE, "Sam")
    assert e.value.cod == "8001"
    assert "oac/usage/save" not in server.endpoints()


def test_a_refused_record_shows_no_code(monkeypatch, tmp_path):
    server = FakeLockly(**{"oac/saveName": {"cod": "500"}})
    c = _client(monkeypatch, tmp_path, server)
    with pytest.raises(LocklyError, match="oac/saveName failed"):
        c.make_offline_code(VISAGE, "Sam")
    assert "oac/usage/save" not in server.endpoints()


def test_a_dropped_connection_is_tried_again_then_said(monkeypatch, tmp_path):
    drop = requests.ConnectionError("reset")
    server = FakeLockly(**{"oac/usage/save": [drop, drop, drop]})
    c = _client(monkeypatch, tmp_path, server)
    r = c.make_offline_code(VISAGE, "Sam")
    assert server.endpoints().count("oac/usage/save") == 3
    assert r["counterSaved"] is False and "couldn't be reached" in r["problem"]
    assert r["code"] == VECTORS[1]["codes"].split(",")[0]       # recorded, so it's still given


def test_a_refused_move_on_isnt_tried_again(monkeypatch, tmp_path):
    server = FakeLockly(**{"oac/usage/save": [requests.Timeout("slow"), {"cod": "910"}]})
    c = _client(monkeypatch, tmp_path, server)
    r = c.make_offline_code(VISAGE, "Sam")
    assert server.endpoints().count("oac/usage/save") == 2
    assert r["counterSaved"] is False and "oac/usage/save failed" in r["problem"]


def test_what_is_refused_before_asking_lockly(monkeypatch, tmp_path):
    server = FakeLockly()
    c = _client(monkeypatch, tmp_path, server)
    with pytest.raises(LocklyError, match="Visage or Vision"):
        c.make_offline_code(TOUCH, "Sam")
    with pytest.raises(LocklyError, match="who the code is for"):
        c.make_offline_code(VISAGE, "   ")
    assert server.calls == []


def test_a_door_without_a_seed(monkeypatch, tmp_path):
    c = _client(monkeypatch, tmp_path, FakeLockly(**{"oac/get": {"cod": "920"}}))
    with pytest.raises(LocklyError, match="no offline code seed"):
        c.make_offline_code(VISAGE, "Sam")
    broken = FakeLockly()
    broken.reply["oacnew"]["listA"].pop()
    c = _client(monkeypatch, tmp_path, broken)
    with pytest.raises(LocklyError, match="entries in list A"):
        c.make_offline_code(VISAGE, "Sam")
    assert "oac/saveName" not in broken.endpoints()


# ── the Api: the app's daily count, per door ─────────────────────────────────

def test_the_api_asks_from_the_apps_daily_count(monkeypatch, tmp_path):
    server = FakeLockly()
    api = over_client(_client(monkeypatch, tmp_path, server))
    for n in range(1, oac.MANY_A_DAY + 1):
        r = api.make_offline_code(VISAGE["ID"], "Sam")
        assert r["made"] and r["madeToday"] == n and r["shown"] == oac.shown(r["code"])
        assert r["steps"] == list(oac.STEPS)
    asked = api.make_offline_code(VISAGE["ID"], "Sam")
    assert asked == {"made": False, "madeToday": oac.MANY_A_DAY, "ask": oac.MANY_TEXT}
    assert server.endpoints().count("oac/get") == oac.MANY_A_DAY      # nothing was asked of Lockly
    assert api.make_offline_code(VISAGE["ID"], "Sam", True)["made"]
    assert config.offline_codes_today(VISAGE["ID"]) == oac.MANY_A_DAY + 1
    assert config.offline_codes_today(TOUCH["ID"]) == 0
    listed = api.offline_codes(VISAGE["ID"])
    assert listed["codes"] == [] and listed["madeToday"] == oac.MANY_A_DAY + 1
    with pytest.raises(ValueError, match="Visage or Vision"):
        api.offline_codes(TOUCH["ID"])


def test_the_daily_count_starts_again_each_day(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCKLYCTL_HOME", str(tmp_path))
    monkeypatch.setattr(config, "_today", lambda: "2026-11-14")
    assert config.count_offline_code("dev") == 1 and config.count_offline_code("dev") == 2
    monkeypatch.setattr(config, "_today", lambda: "2026-11-15")
    assert config.offline_codes_today("dev") == 0
    assert config.count_offline_code("dev") == 1
