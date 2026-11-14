"""Which locks the listings show, and in what order.

Hiding a lock only takes it out of listings: the ``list`` command, the menu's
lock picker and the app's door list. A command that names it, and bulk actions
such as checking every door, still reach it.

Signed in to a private locklyctl-mqtt server (``--server``), the server keeps
these preferences, so its web console and every computer agree (CONTRACT.md,
``/api/view/*``). Signed in to Lockly, they live in this computer's owner-only
config, per account; nothing on the locks or in Lockly's cloud changes.
"""
from __future__ import annotations

import re

from . import config
from . import protocol as P
from .transport import LocklyError

ACCOUNT_ORDER = "account"   # with no saved order: as the account lists them (the CLI)
NAME_ORDER = "name"         # with no saved order: by name, digits by value (the app)


def _id(lock: dict) -> str:
    return str(lock.get("ID", ""))


def _name(lock: dict) -> str:
    return str(lock.get("na") or lock.get("blename") or lock.get("ID", "?"))


def _natural(text: str) -> list:
    """Sort key that compares digit runs by value, so "2A" comes before "10B"
    (what the app's list did with localeCompare(..., {numeric: true}))."""
    return [int(t) if i % 2 else t.lower() for i, t in enumerate(re.split(r"(\d+)", text))]


def _local(client) -> dict:
    return dict((config.load_config().get("lock_view") or {}).get(client.email) or {})


def _save_local(client, prefs: dict) -> None:
    cfg = config.load_config()
    views = dict(cfg.get("lock_view") or {})
    views[client.email] = prefs
    config.save_config({**cfg, "lock_view": views})


def hidden_ids(client) -> set[str]:
    """Ids of the hidden locks (one config read for a whole listing)."""
    if client.server:
        return {_id(lk) for lk in client.locks if lk.get("hidden")}
    return {str(d) for d in _local(client).get("hidden") or []}


def is_hidden(client, lock: dict, hidden: set[str] | None = None) -> bool:
    return _id(lock) in (hidden_ids(client) if hidden is None else hidden)


def ordered(client, default: str = ACCOUNT_ORDER) -> list[dict]:
    """Every lock, hidden ones included, in display order. A saved order wins
    (a server always has one); without one, ``default`` decides. Locks a saved
    order doesn't know yet (added since) come after it, in account order."""
    locks = list(client.locks)
    if client.server:
        return sorted(locks, key=lambda lk: int(lk.get("sortIndex") or 0))
    order = _local(client).get("order") or []
    if not order:
        return sorted(locks, key=lambda lk: _natural(_name(lk))) if default == NAME_ORDER else locks
    pos = {str(d): i for i, d in enumerate(order)}
    return sorted(locks, key=lambda lk: pos.get(_id(lk), len(pos)))


def listed(client, *, include_hidden: bool = False, default: str = ACCOUNT_ORDER) -> list[dict]:
    """The locks a listing shows: display order, hidden ones left out unless asked."""
    hidden = hidden_ids(client)
    return [lk for lk in ordered(client, default) if include_hidden or _id(lk) not in hidden]


def _check(body: dict, what: str) -> None:
    if str(body.get("cod")) != "200":
        raise LocklyError(f"{what} failed: {P.describe_cod(body.get('cod'))}", str(body.get("cod")))


def set_hidden(client, lock: dict, hidden: bool) -> None:
    """Hide a lock from listings, or show it again."""
    if client.server:
        _check(client._post("view/hidden", {"deviceId": _id(lock), "hidden": bool(hidden)}),
               "hiding the lock" if hidden else "showing the lock")
        lock["hidden"] = bool(hidden)
        return
    prefs = _local(client)
    ids = [str(d) for d in prefs.get("hidden") or [] if str(d) != _id(lock)]
    if hidden:
        ids.append(_id(lock))
    _save_local(client, {**prefs, "hidden": ids})


def set_order(client, locks: list[dict]) -> None:
    """Save this display order (every lock, hidden ones included)."""
    ids = [_id(lk) for lk in locks]
    if client.server:
        _check(client._post("view/order", {"order": ids}), "reordering the locks")
        for i, lk in enumerate(locks):
            lk["sortIndex"] = i
        return
    _save_local(client, {**_local(client), "order": ids})


def _index(items: list[dict], lock: dict) -> int:
    return next(i for i, lk in enumerate(items) if lk is lock)


def move(client, lock: dict, *, by: int = 0, to: int | None = None,
         include_hidden: bool = False, default: str = ACCOUNT_ORDER) -> list[dict]:
    """Move a lock within its listing, save the new order and return it.

    ``by`` moves it that many places down (negative: up); ``to`` puts it at
    that 1-based place. Either is clamped to the listing. The listing is what
    the caller shows: without ``include_hidden``, hidden locks are skipped over
    and keep their place in the full order (the lock being moved counts even
    if it is hidden itself)."""
    full = ordered(client, default)
    target = next((lk for lk in full if _id(lk) == _id(lock)), None)
    if target is None:
        raise LocklyError("That lock is no longer on this account.")
    hidden = hidden_ids(client)
    frame = [lk for lk in full if include_hidden or lk is target or _id(lk) not in hidden]
    i = _index(frame, target)
    j = (to - 1) if to is not None else i + by
    j = max(0, min(len(frame) - 1, j))
    if j == i:
        return full
    rest = [lk for lk in frame if lk is not target]
    others = [lk for lk in full if lk is not target]
    # Before the lock that will follow it in the listing, or after the last one.
    at = _index(others, rest[j]) if j < len(rest) else _index(others, rest[-1]) + 1
    others.insert(at, target)
    set_order(client, others)
    return others
