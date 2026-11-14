"""Wi-Fi passwords for giving a lock its network (``locklyctl wifi`` and the
GUI's Wi-Fi section), so each is typed once.

A network's password is looked for, in order:

1. the one locklyctl saved for it: in the OS keyring (Secret Service,
   Keychain, Credential Locker) when there is a working one, else in the
   owner-only config file (on a phone, the app's private folder), as the
   Lockly login is (config._save_credential);
2. this computer's own saved network (from_system, which reads a password
   only when one is about to be used; known() reads none):
   - Linux: NetworkManager (nmcli), which gives a saved password to a user
     allowed to change that connection;
   - macOS: the keychain's "AirPort network password" item for the network
     (`security`), which macOS gives after asking for an administrator's
     password;
   - Windows: the network's WLAN profile (`netsh wlan export profile
     key=clear`, XML, so not tied to the system language), whose password is
     in clear only when Windows allows it for this user; otherwise none.
   Phones have no such access.

order() puts the networks a lock sees in the order to offer them: those
locklyctl saved a password for (a lock has joined each), the one this computer
is connected to (connected()), others this computer has saved, then the rest,
each group strongest first. It suggests only the first of the first two kinds.
Only networks from the lock's own scan are offered: its Wi-Fi chip (a Renesas
DA16200 on the Visage) has a 2.4 GHz radio only, so each of them broadcasts
on 2.4 GHz. A 5 GHz-only network this computer is on never appears, so it's
never offered either.

A password is saved once a lock has joined the network with it (the lock's
result 3), and a saved one the lock calls wrong (result 4) is forgotten
(after_setup). Passwords are never logged, printed, or sent to the GUI's page.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import config

_LOG = logging.getLogger("locklyctl")
_KEYRING_SERVICE = "locklyctl-wifi"
PLATFORM = sys.platform                 # which system store from_system asks; tests set it
NMCLI, SECURITY, NETSH, NETWORKSETUP = "nmcli", "security", "netsh", "networksetup"
TIMEOUT_S = {"darwin": 120.0, "win32": 30.0}    # macOS waits while an administrator's password is typed
TIMEOUT_DEFAULT_S = 10.0
_SYSTEM_ERRORS = (OSError, subprocess.SubprocessError, ET.ParseError)


def saved(ssid: str) -> tuple[str | None, str]:
    """The password locklyctl saved for ``ssid``, and why it couldn't be read
    when one is recorded but unreadable ("" otherwise)."""
    cfg = config.load_config()
    if ssid in (cfg.get("wifi_in_keyring") or []):
        kr = config._keyring()
        if kr is None:
            return None, "the password saved in the keyring can't be read: no keyring is available now"
        try:
            return kr.get_password(_KEYRING_SERVICE, ssid), ""
        except kr.errors.KeyringError as e:     # locked, or the keyring service went away
            return None, f"the password saved in the keyring can't be read ({e})"
    return (cfg.get("wifi_passwords") or {}).get(ssid), ""


def save(ssid: str, password: str) -> str:
    """Save ``password`` for ``ssid``: 'keyring', or 'file' when no keyring works."""
    cfg = config.load_config()
    in_keyring = [s for s in (cfg.get("wifi_in_keyring") or []) if s != ssid]
    in_file = {s: p for s, p in (cfg.get("wifi_passwords") or {}).items() if s != ssid}
    kr = config._keyring()
    where = "file"
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, ssid, password)
            where = "keyring"
        except kr.errors.KeyringError as e:
            _LOG.warning("keyring unavailable (%s), saving the Wi-Fi password in the config file", e)
    if where == "keyring":
        in_keyring.append(ssid)
    else:
        in_file[ssid] = password
    config.save_config({**cfg, "wifi_in_keyring": in_keyring, "wifi_passwords": in_file})
    return where


def forget(ssid: str) -> bool:
    """Forget the password saved for ``ssid``. Returns whether one was saved."""
    cfg = config.load_config()
    in_keyring = list(cfg.get("wifi_in_keyring") or [])
    in_file = dict(cfg.get("wifi_passwords") or {})
    had = ssid in in_keyring or ssid in in_file
    if ssid in in_keyring:
        kr = config._keyring()
        if kr is not None:
            try:
                kr.delete_password(_KEYRING_SERVICE, ssid)
            except kr.errors.PasswordDeleteError:   # already gone from the keyring
                pass
    config.save_config({**cfg, "wifi_in_keyring": [s for s in in_keyring if s != ssid],
                        "wifi_passwords": {s: p for s, p in in_file.items() if s != ssid}})
    return had


def system_name() -> str:
    """Where this computer keeps Wi-Fi passwords, in words."""
    return {"darwin": "the macOS keychain", "win32": "Windows"}.get(PLATFORM, "NetworkManager")


def source_text(source: str) -> str:
    return "saved by locklyctl" if source == "saved" else f"saved on this computer ({system_name()})"


def _run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """A system tool, without a shell or (Windows) a console window. On Windows
    the arguments are joined as written: netsh wants name="..." quoting, which
    subprocess's own quoting would escape."""
    windows = os.name == "nt"
    return subprocess.run(" ".join(args) if windows else args, capture_output=True, text=True,
                          encoding="oem" if windows else None, errors="replace",    # console output's code page
                          timeout=timeout or TIMEOUT_S.get(PLATFORM, TIMEOUT_DEFAULT_S),
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _checked(args: list[str]) -> str:
    r = _run(args)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, args[:2])
    return r.stdout


# -- Linux: NetworkManager --------------------------------------------------------

def _nm_networks() -> dict[str, list[str]]:
    """NetworkManager's saved Wi-Fi connections: {ssid: [connection UUID]}. No
    secrets are read. Empty when there's no NetworkManager."""
    if shutil.which(NMCLI) is None:
        return {}
    out: dict[str, list[str]] = {}
    for row in _checked([NMCLI, "-t", "-f", "UUID,TYPE", "connection", "show"]).splitlines():
        uuid, _, kind = row.partition(":")
        if kind == "802-11-wireless":
            ssid = _checked([NMCLI, "--escape", "no", "-g", "802-11-wireless.ssid", "connection", "show",
                             uuid]).rstrip("\n")
            out.setdefault(ssid, []).append(uuid)
    return out


def _nm_password(ssid: str) -> tuple[str | None, str]:
    why = ""
    for uuid in _nm_networks().get(ssid, []):
        values = _checked([NMCLI, "--escape", "no", "-s", "-g",
                           "802-11-wireless-security.key-mgmt,802-11-wireless-security.psk",
                           "connection", "show", uuid]).rstrip("\n").split("\n")
        if values[0] not in ("wpa-psk", "sae"):
            why = why or "this computer has the network saved, but not with a WPA password"
        elif len(values) == 2 and values[1]:
            return values[1], ""
        else:
            why = "NetworkManager has this network saved but didn't give its password"
    return None, why


# -- macOS: the keychain -------------------------------------------------------------

_AIRPORT = "AirPort network password"   # the keychain item kind macOS saves Wi-Fi passwords as


def _mac_has(ssid: str) -> bool:
    """Whether the keychain has the network: its item's attributes only, no password."""
    if shutil.which(SECURITY) is None:
        return False
    return _run([SECURITY, "find-generic-password", "-D", _AIRPORT, "-a", ssid],
                TIMEOUT_DEFAULT_S).returncode == 0


def _mac_password(ssid: str) -> tuple[str | None, str]:
    if shutil.which(SECURITY) is None:
        return None, ""
    r = _run([SECURITY, "find-generic-password", "-D", _AIRPORT, "-a", ssid, "-w"])
    if r.returncode == 0 and r.stdout.rstrip("\n"):
        return r.stdout.rstrip("\n"), ""
    if r.returncode == 44:                  # errSecItemNotFound: the keychain doesn't have it
        return None, ""
    return None, (f"the macOS keychain didn't give the password (security exited {r.returncode}; it asks "
                  "for an administrator's password, which may have been refused)")


# -- Windows: WLAN profiles ------------------------------------------------------------

_PSK_AUTH = ("WPAPSK", "WPA2PSK", "WPA3SAE")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _win_profile(xml: bytes) -> dict:
    """A WLAN profile's name, SSID, authentication and key, from netsh's XML."""
    root = ET.fromstring(xml)
    out = {"name": "", "ssid": "", "auth": "", "key": "", "protected": ""}
    for el in root:
        if _local(el.tag) == "name":
            out["name"] = el.text or ""
    for el in root.iter():
        tag = _local(el.tag)
        if tag == "SSID" and not out["ssid"]:
            out["ssid"] = next((c.text or "" for c in el if _local(c.tag) == "name"), "")
        elif tag in ("authentication", "keyMaterial", "protected"):
            out[{"authentication": "auth", "keyMaterial": "key", "protected": "protected"}[tag]] = el.text or ""
    return out


def _win_profiles(folder: str, *, name: str | None = None, clear: bool = False) -> list[dict]:
    args = [NETSH, "wlan", "export", "profile"]
    args += [f'name="{name}"'] if name else []
    args += ["key=clear"] if clear else []
    _checked(args + [f'folder="{folder}"'])
    return [_win_profile(p.read_bytes()) for p in sorted(Path(folder).glob("*.xml"))]


def _win_networks() -> dict[str, str]:
    """Windows' saved Wi-Fi profiles: {ssid: profile name}, keys left encrypted."""
    if shutil.which(NETSH) is None:
        return {}
    with tempfile.TemporaryDirectory(prefix="locklyctl-") as folder:
        return {p["ssid"]: p["name"] for p in _win_profiles(folder) if p["ssid"]}


def _win_password(ssid: str) -> tuple[str | None, str]:
    profile = _win_networks().get(ssid)
    if not profile:
        return None, ""
    if '"' in profile:
        return None, "Windows has this network saved under a name netsh can't be given"
    # The exported file holds the password in clear; the folder goes when this ends.
    with tempfile.TemporaryDirectory(prefix="locklyctl-") as folder:
        for p in _win_profiles(folder, name=profile, clear=True):
            if p["ssid"] != ssid:
                continue
            if p["auth"] not in _PSK_AUTH:
                return None, "this computer has the network saved, but not with a WPA password"
            if p["key"] and p["protected"].lower() == "false":
                return p["key"], ""
    return None, ("Windows has this network saved but didn't give its password (it may only give it "
                  "to a program run as an administrator)")


# -- whichever this computer uses ------------------------------------------------------------

def _system_has(ssids: list[str]) -> set[str]:
    if PLATFORM == "darwin":
        return {s for s in ssids if _mac_has(s)}
    if PLATFORM == "win32":
        return set(_win_networks()) & set(ssids)
    return set(_nm_networks()) & set(ssids)     # Linux; phones have no nmcli, so none


def from_system(ssid: str) -> tuple[str | None, str]:
    """This computer's saved password for ``ssid`` (WPA/WPA3 personal), and
    why there's none when it has the network but no password came back ("" when
    it doesn't have it). On macOS this asks for an administrator's password."""
    try:
        if PLATFORM == "darwin":
            return _mac_password(ssid)
        if PLATFORM == "win32":
            return _win_password(ssid)
        return _nm_password(ssid)
    except _SYSTEM_ERRORS as e:
        return None, f"{system_name()} couldn't be asked ({type(e).__name__})"


def known(ssids: list[str]) -> dict[str, str]:
    """For each of ``ssids``: "saved" (by locklyctl), "system" (this computer
    has it) or "". Reads no password, so asks for none."""
    cfg = config.load_config()
    mine = set(cfg.get("wifi_in_keyring") or []) | set(cfg.get("wifi_passwords") or {})
    try:
        system = _system_has([s for s in ssids if s not in mine])
    except _SYSTEM_ERRORS as e:
        _LOG.info("%s couldn't be asked for saved networks: %s", system_name(), type(e).__name__)
        system = set()
    return {s: "saved" if s in mine else "system" if s in system else "" for s in ssids}


def _nm_connected() -> set[str]:
    if shutil.which(NMCLI) is None:
        return set()
    rows = _checked([NMCLI, "-t", "--escape", "no", "-f", "ACTIVE,SSID", "device", "wifi", "list", "--rescan", "no"])
    return {ssid for active, _, ssid in (r.partition(":") for r in rows.splitlines()) if active == "yes" and ssid}


def _mac_connected() -> set[str]:
    """macOS 15 and later reportedly hide the network's name from programs
    without location access (community reports, not checked here); anything
    but a "Current Wi-Fi Network: NAME" line counts as not known."""
    if shutil.which(NETWORKSETUP) is None:
        return set()
    ports = _checked([NETWORKSETUP, "-listallhardwareports"]).splitlines()
    devices = [ports[i + 1].split(":", 1)[1].strip() for i, line in enumerate(ports[:-1])
               if line.strip() in ("Hardware Port: Wi-Fi", "Hardware Port: AirPort")
               and ports[i + 1].startswith("Device:")]
    out = set()
    for device in devices:
        line = _run([NETWORKSETUP, "-getairportnetwork", device], TIMEOUT_DEFAULT_S).stdout.strip()
        prefix = "Current Wi-Fi Network: "
        if line.startswith(prefix) and line[len(prefix):] not in ("", "<redacted>"):
            out.add(line[len(prefix):])
    return out


def _win_connected() -> set[str]:
    if shutil.which(NETSH) is None:
        return set()
    out = set()
    for line in _checked([NETSH, "wlan", "show", "interfaces"]).splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip() == "SSID" and value.strip():          # not "BSSID"; shown only when connected
            out.add(value.strip())
    return out


def connected() -> set[str]:
    """The Wi-Fi network(s) this computer is connected to, as far as it says."""
    try:
        if PLATFORM == "darwin":
            return _mac_connected()
        if PLATFORM == "win32":
            return _win_connected()
        return _nm_connected()
    except _SYSTEM_ERRORS as e:
        _LOG.info("%s couldn't say which network this computer is on: %s", system_name(), type(e).__name__)
        return set()


def order(ssids: list[str]) -> list[dict]:
    """``ssids`` (the networks a lock sees, strongest first) in the order to
    offer them: [{"ssid", "known", "connected", "suggested"}]. First the
    networks locklyctl saved a password for (a lock joined each), then the one
    this computer is connected to, then others it has saved, then the rest;
    each group keeps signal order. Only a network of the first two kinds is
    suggested, and only the first one."""
    knowns, here = known(ssids), connected()
    tier = lambda s: 0 if knowns[s] == "saved" else 1 if s in here else 2 if knowns[s] == "system" else 3  # noqa: E731
    ranked = sorted(ssids, key=tier)
    first = ranked[0] if ranked and tier(ranked[0]) <= 1 else None
    return [{"ssid": s, "known": knowns[s], "connected": s in here, "suggested": s == first} for s in ranked]


def resolve(ssid: str) -> tuple[str | None, str, str]:
    """The password to use for ``ssid``: (password, source "saved" or
    "system", ""), or (None, "", why none was found, possibly "")."""
    password, why_saved = saved(ssid)
    if password:
        return password, "saved", ""
    password, why_system = from_system(ssid)
    if password:
        return password, "system", ""
    return None, "", "; ".join(w for w in (why_saved, why_system) if w)


def after_setup(ssid: str, password: str, source: str, code: int | None) -> str:
    """Keep a password the lock joined with, drop a saved one it called wrong.
    ``source``: "saved", "system", or how it was given ("typed", "file").
    Returns what was done, in words ("" when nothing)."""
    if code == 3 and source not in ("saved", "system"):
        where = save(ssid, password)
        return f"Saved the password for {ssid} " + ("in the keyring." if where == "keyring"
                                                    else "in locklyctl's config file (only you can read it).")
    if code == 4 and source == "saved":
        forget(ssid)
        return f"The lock says the saved password for {ssid} is wrong, so it's forgotten; give the new one."
    if code == 4 and source == "system":
        return f"The lock says this computer's saved password for {ssid} is wrong; type the right one."
    return ""
