"""Offline codes: one-use codes a Visage or Vision checks by itself.

Lockly's server keeps a seed for each lock (``oac/get``), and the lock was
given the same seed over Bluetooth when it was added. A code is digit
arithmetic over the seed at a position, so making one needs no contact with
the lock, and a door that's offline still takes it. After each code the
position moves on (``oac/usage/save``), and after every 100 codes one of the
seed's lists turns over, as the lock's copy does.

This is the app's "short one-time" code, the only kind it makes for these
models (LOCKLY app 3.3.4: ``OACManager.s`` makes the code, the share step in
``OACShareActivity.q3`` moves the position on). tests/test_oac.py checks this
against codes the app's own methods produced for made-up seeds.

The seed is what every future code comes from, so it's never logged, printed
or saved here; a Seed's repr shows only its position.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# Models whose seeds are on the account (oac/get answered for each,
# 2026-11-15). The app makes only one-use codes for both
# (BluetoothBean.isOnlySupportOneTimeOAC) and has no rule for the PGD7Y.
MODELS = frozenset({"PGK728WRHK", "PGK798HK"})

# The app asks before a fifth code in one day from the same phone, because
# codes made in a row and not used may stop newer ones working
# (OACShareActivity.m3 asks when its count is 3 or more, and A3 sets the count
# to 0 at the day's first code). Its warning, word for word:
MANY_A_DAY = 4
MANY_TEXT = "Activate the issued OAC access code before activating a new one."

# Lockly's answer when the account's plan has made all the codes it may this
# month: cod 8001 from oac/saveName (strings.xml offline_access_limit). The app
# can also ask first (deviceCheck, PRODUCT_LIMIT), but only behind a remote
# switch (PayManager.isPayEnable); for this account that check answered 910
# while the same endpoint's device check answered 200 (2026-11-15), so
# locklyctl relies on saveName's answer.
LIMIT_CODE = "8001"
LIMIT_TEXT = ("The number of OAC you can create this month has reached the limit, "
              "come back next month. You can also upgrade your subscription now to create more OAC.")

# How a guest enters one, from Lockly's own guest page for these locks
# (apgd.lockly.com/#/shareOac, the page the app's share link opens).
STEPS = (
    "Double-tap the Lockly logo button. The four circles on the keypad flash: it's ready for the code.",
    "Enter the code, then press the logo.",
    "Enter the code again, then press the logo. A new code has to be entered twice the first time.",
)
STEPS_NOTE = ("Enter it within a minute of the circles flashing. If the door doesn't open, "
              "do the steps again. The code works once and can't be deleted or changed.")

# Each list's length as the app makes them (OACManager.w0); s() reads A and B
# at positions 0-99 and C at 0-9.
_SIZES = {"list A": 100, "list B": 100, "list C": 10}

# OACManager.A0: the second digit picks how the four are reordered.
_ORDER = ((0, 1, 3, 2), (2, 1, 0, 3), (2, 1, 3, 0), (3, 1, 0, 2), (3, 1, 2, 0))


class OacError(ValueError):
    """Lockly's seed for a lock is missing a piece or isn't in the expected form."""


@dataclass(frozen=True)
class Seed:
    """One lock's seed and position, as oac/get returns them."""
    a: tuple[str, ...] = field(repr=False)        # oacnew.listA
    b: tuple[str, ...] = field(repr=False)        # data.codeX1List, turned over every 100 codes
    c: tuple[str, ...] = field(repr=False)        # oacnew.listC
    turn: int = field(repr=False)                 # oacnew.numberC: how far B turns over
    index: int                                    # data.numD: the next code's position
    # The app's older kind of code; sent back unchanged when the position is saved.
    x: tuple[str, ...] = field(repr=False)        # data.codeXList
    y: tuple[str, ...] = field(repr=False)        # data.codeYList
    num_b: int = field(repr=False)
    num_c: int = field(repr=False)


def supported(lock: dict) -> bool:
    return str(lock.get("mod") or "").upper() in MODELS


def _entries(items, i_key: str, o_key: str, name: str) -> list[str]:
    """A list's values in position order (OACManager.o and p sort by index)."""
    if not isinstance(items, list) or not items:
        raise OacError(f"Lockly's seed has no {name}.")
    pairs = []
    for it in items:
        if not isinstance(it, dict) or i_key not in it or o_key not in it:
            raise OacError(f"Lockly's seed has a {name} entry without a position or a value.")
        pairs.append((_int(it[i_key], name), str(it[o_key])))
    pairs.sort(key=lambda p: p[0])                  # stable, as the app's bubble sort is
    values = [o for _, o in pairs]
    if not all(o.isascii() and o.isdigit() for o in values):
        raise OacError(f"Lockly's seed has a {name} entry that isn't digits.")
    return values


def _int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, str)) or not str(v).lstrip("-").isdigit():
        raise OacError(f"Lockly's seed has a {name} value that isn't a number.")
    return int(v)


def _sized(values: list[str], name: str) -> tuple[str, ...]:
    if len(values) != _SIZES[name]:
        raise OacError(f"Lockly's seed has {len(values)} entries in {name}, not {_SIZES[name]}.")
    return tuple(values)


def from_reply(body: dict) -> Seed:
    """The seed from an oac/get reply, filled the way the app fills its copy
    (OacUtil.m: listA and codeX1List through z0, listC through y0)."""
    new, data = body.get("oacnew"), body.get("data")
    if not isinstance(new, dict) or not isinstance(data, dict):
        raise OacError("Lockly's server has no offline code seed for this lock.")
    z0 = lambda o: "0" + o if len(o) == 1 else o
    y0 = lambda o: "0" + o if len(o) == 1 else "00" + o if len(o) == 2 else o
    return Seed(
        a=_sized([z0(o) for o in _entries(new.get("listA"), "i", "o", "list A")], "list A"),
        b=_sized([z0(o) for o in _entries(data.get("codeX1List"), "i", "o", "list B")], "list B"),
        c=_sized([y0(o) for o in _entries(new.get("listC"), "i", "o", "list C")], "list C"),
        turn=_int(new.get("numberC"), "turn-over"),
        index=_int(data.get("numD"), "position"),
        x=tuple(_entries(data.get("codeXList"), "idx", "obj", "list X")),
        y=tuple(_entries(data.get("codeYList"), "idx", "obj", "list Y")),
        num_b=_int(data.get("numB"), "count B"),
        num_c=_int(data.get("numC"), "count C"),
    )


def code(seed: Seed) -> str:
    """The code at the seed's position (OACManager.s with L, u and A0)."""
    i = seed.index % 100
    ab = seed.a[i] + seed.b[i]
    head, last = ab[:-1], int(ab[-1])
    c = seed.c[-1] if last == 0 else seed.c[last - 1]
    c = "00" + c if len(c) == 1 else "0" + c if len(c) == 2 else c
    if len(c) < len(head) or len(head) < 3:
        raise OacError("Lockly's seed has an entry too short to make a code from.")
    pre = "".join(str((int(h) + int(k)) % 10) for h, k in zip(head, c)) + str(last)
    return "".join(pre[k] for k in _ORDER[int(pre[1]) % 5])


def after(seed: Seed) -> Seed:
    """The seed once its code is given out: the next position, and list B
    turned over after position 99 (OACShareActivity.q3, OACManager.q0)."""
    if seed.index == 99:
        return replace(seed, b=tuple(f"{(int(v) + seed.turn) % 100:02d}" for v in seed.b), index=0)
    return replace(seed, index=seed.index + 1)


def shown(code: str) -> str:
    """A code the way the app shows it, in two halves ("12 34")."""
    half = len(code) // 2
    return f"{code[:half]} {code[half:]}"


def record_body(lock_id: str, name: str, code: str) -> dict:
    """oac/saveName: who a one-use code is for, so the Lockly app lists it
    (UploadPwdDataRequest with oacType "oac", no dates: pwType OFFONE)."""
    return {"dv": lock_id, "endate": 0, "na": name, "oacType": "oac", "pw": code,
            "pwType": "OFFONE", "stdate": 0}


def usage_body(seed: Seed, acct: str, lock_id: str, now_ms: int) -> dict:
    """oac/usage/save for a seed already moved on by after() (UpdateOacRequest)."""
    return {
        "acct": acct, "dv": lock_id, "uploadTime": now_ms,
        "numB": seed.num_b, "numC": seed.num_c,
        "codeXList": [{"idx": k, "obj": v} for k, v in enumerate(seed.x)],
        "codeYList": [{"idx": k, "obj": v} for k, v in enumerate(seed.y)],
        "codeX1List": [{"i": k, "o": v} for k, v in enumerate(seed.b)],
        "numD": seed.index,
    }


def records(body: dict) -> list[dict]:
    """The codes Lockly has on record for a lock (oac/getNameList's ``obj``),
    oldest first as the server lists them. ``code`` is the code itself."""
    out = []
    for r in body.get("obj") or []:
        out.append({"id": r.get("id"), "name": r.get("acctname") or "",
                    "once": r.get("pwType") == "OFFONE", "code": r.get("password") or ""})
    return out
