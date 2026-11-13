"""Direct Bluetooth LE link to a lock, the way the LOCKLY app talks to one.

App 3.3.4 (BLELoader, FastBle): find the lock by its advertised name (the
cloud's ``blename``), write a command frame to characteristic fff1 in 20-byte
chunks, and read the reply from fff4 notifications. The frames are the same
ones locklyctl relays through a hub or Lockly's MQTT channel; there's no
pairing or key exchange. Needs the optional ``bleak`` package and an adapter.

Built from the app's code on 2026-11-13. First run against a lock the same
evening: a Visage found by name at -74 dBm, connected in 6.4 s, and answered
two status checks.
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import time

from . import protocol as P

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"     # BLELoader.java:282-292
WRITE = "0000fff1-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
CHUNK = 20                       # FastBle's split size (BleManager.f24464j), never raised
HEAD = bytes.fromhex("A1B2C3D4")
SCAN_S = 10.0                    # the app scans for 10-15 s
REPLY_S = 8.0                    # BLELoader gives a command 8 s before "No Response"
MISS_TTL_S = 60.0                # how long a whole scan that didn't hear a lock is trusted


class BluetoothError(Exception):
    """Bluetooth isn't usable here, or the lock couldn't be reached over it."""


class NotInRange(BluetoothError):
    """No device with the lock's name was heard."""


# One adapter: scans and connects take turns. Every name heard in any scan is
# remembered, and so is when the last whole scan (one that ran its full time
# without finding what it looked for) started: a lock it didn't hear isn't
# scanned for again for MISS_TTL_S when the caller allows that shortcut, so
# checking several offline doors costs one scan, not one each.
_SCAN_LOCK = threading.Lock()
_heard: dict[str, float] = {}
_whole_scan_at: float | None = None


def recently_missed(name: str) -> bool:
    """Whether a whole scan in the last MISS_TTL_S ran without hearing ``name``."""
    at = _whole_scan_at
    return (at is not None and time.monotonic() - at < MISS_TTL_S
            and _heard.get(name, float("-inf")) < at)


async def _find(name: str, timeout: float):
    """The device advertising ``name``, or None; records every name heard."""
    global _whole_scan_at
    from bleak import BleakScanner
    started = time.monotonic()

    def match(d, adv) -> bool:
        seen = _name_of(d, adv)
        if seen:
            _heard[seen] = time.monotonic()
        return seen == name
    device = await BleakScanner.find_device_by_filter(match, timeout=timeout)
    if device is None:
        _whole_scan_at = started
    return device


def available() -> str:
    """Why Bluetooth can't be used on this computer, or "" when it can be tried."""
    if importlib.util.find_spec("bleak") is None:
        return "Bluetooth needs the optional 'bleak' package (pip install 'locklyctl[bluetooth]')."
    return ""


class _NoBleak(Exception):
    """Stands in for bleak's error class when bleak isn't installed (never raised)."""


def _bleak_error() -> type[Exception]:
    """bleak's error class, or one nothing raises when bleak isn't installed, so
    the frame handling below works with any client object."""
    if importlib.util.find_spec("bleak") is None:
        return _NoBleak
    from bleak.exc import BleakError
    return BleakError


class FrameAssembler:
    """Joins notification chunks into whole frames, as BLELoader.E/w do: a chunk
    holding the header starts a new frame (from its last header, as the app does
    for HomeKit locks such as the Visage), others are appended; a frame is done
    when its length field is reached and its CRC checks. Frames whose type is
    0AFF ("invalid command" noise) are skipped, and the wait goes on."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> bytes | None:
        at = chunk.rfind(HEAD)
        self._buf = chunk[at:] if at >= 0 else self._buf + chunk
        if len(self._buf) < 6 or not self._buf.startswith(HEAD):
            return None
        need = self._buf[4] + self._buf[5] * 256
        if len(self._buf) < need:
            return None
        frame, self._buf = self._buf[:need], b""
        if P.crc8_lockly(frame[:-1]) != frame[-1] or frame[6:8].hex().upper() == "0AFF":
            return None
        return frame


def _name_of(device, adv) -> str:
    return ((adv.local_name if adv else None) or device.name or "").strip()


def in_range(name: str, timeout: float = SCAN_S) -> bool:
    """Whether a device advertising this name is heard within ``timeout``.
    Listens only; nothing is sent to the lock. Raises BluetoothError when
    Bluetooth can't be used (no adapter, adapter off)."""
    from bleak.exc import BleakError
    try:
        with _SCAN_LOCK:
            return asyncio.run(_find(name, timeout)) is not None
    except BleakError as e:
        raise BluetoothError(f"Bluetooth: {e}") from e


class BleLink:
    """One connection to one lock, driven from ordinary threads: bleak runs on
    an event loop of its own in a daemon thread."""

    def __init__(self, name: str) -> None:
        if not name:
            raise BluetoothError("This lock has no Bluetooth name in Lockly's records.")
        self.name = name
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="locklyctl-ble", daemon=True)
        self._thread.start()
        self._client = None
        self._frames: asyncio.Queue | None = None
        self._asm = FrameAssembler()
        self._respond = True
        self._busy = threading.Lock()

    def _run(self, coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # -- connect / disconnect ------------------------------------------------

    def connect(self, timeout: float = SCAN_S, *, skip_if_missed: bool = False) -> None:
        """Find the lock by name and connect. With ``skip_if_missed``, a lock a
        whole scan didn't hear in the last MISS_TTL_S isn't scanned for again.
        Raises NotInRange when it isn't heard, BluetoothError otherwise."""
        with _SCAN_LOCK:
            if skip_if_missed and recently_missed(self.name):
                raise NotInRange("The lock isn't in Bluetooth range of this computer "
                                 f"(not heard in a scan {time.monotonic() - _whole_scan_at:.0f} s ago).")
            self._run(self._connect(timeout), timeout + 20)

    async def _connect(self, timeout: float) -> None:
        from bleak import BleakClient
        from bleak.exc import BleakError
        try:
            device = await _find(self.name, timeout)
            if device is None:
                raise NotInRange(f"The lock isn't in Bluetooth range of this computer ({timeout:.0f} s scan).")
            self._client = BleakClient(device, timeout=10.0)
            await self._client.connect()
            self._frames = asyncio.Queue()
            await self._client.start_notify(NOTIFY, self._on_notify)
            # Android writes without response when the characteristic allows it
            # (the app never sets a write type); bleak needs it said.
            props = self._client.services.get_characteristic(WRITE).properties
            self._respond = "write-without-response" not in props
            await asyncio.sleep(0.1)               # the app waits 100 ms before writing
        except BleakError as e:                    # adapter missing, connect refused, ...
            raise BluetoothError(f"Bluetooth: {e}") from e

    def _on_notify(self, _sender, data: bytearray) -> None:
        frame = self._asm.feed(bytes(data))
        if frame is not None and self._frames is not None:
            self._frames.put_nowait(frame)

    def close(self) -> None:
        """Disconnect, then stop and close this link's event loop and thread."""
        if self._client is not None:
            client, self._client = self._client, None
            self._run(self._disconnect(client), 15)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)
        if not self._thread.is_alive():
            self._loop.close()

    @staticmethod
    async def _disconnect(client) -> None:
        try:
            await client.disconnect()
        except _bleak_error():                     # already gone: nothing left to close
            pass

    # -- one command ---------------------------------------------------------

    def exchange(self, frame_hex: str, timeout: float = REPLY_S) -> str | None:
        """Send one frame and return the lock's reply as hex, or None if none
        came within ``timeout``. One command at a time, as BLERequest does."""
        if self._client is None:
            raise BluetoothError("Not connected over Bluetooth.")
        with self._busy:
            reply = self._run(self._exchange(bytes.fromhex(frame_hex), timeout), timeout + 10)
        return reply.hex().upper() if reply else None

    async def _exchange(self, frame: bytes, timeout: float) -> bytes | None:
        while not self._frames.empty():            # anything left from before isn't the answer
            self._frames.get_nowait()
        try:
            for i in range(0, len(frame), CHUNK):
                await self._client.write_gatt_char(WRITE, frame[i:i + CHUNK], response=self._respond)
        except _bleak_error() as e:
            raise BluetoothError(f"Bluetooth write failed: {e}") from e
        task = asyncio.ensure_future(self._frames.get())
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            task.cancel()
            return None
        return task.result()
