"""Direct Bluetooth LE link to a lock, the way the LOCKLY app talks to one.

App 3.3.4 (BLELoader, FastBle): find the lock by its advertised name (the
cloud's ``blename``), write a command frame to characteristic fff1 in 20-byte
chunks, and read the reply from fff4 notifications. The frames are the same
ones locklyctl relays through a hub or Lockly's MQTT channel; there's no
pairing or key exchange. Needs the optional ``bleak`` package and an adapter.

Built from the app's code on 2026-11-13; not yet run against a lock (none was
in range of the machine it was written on).
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading

from . import protocol as P

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"     # BLELoader.java:282-292
WRITE = "0000fff1-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000fff4-0000-1000-8000-00805f9b34fb"
CHUNK = 20                       # FastBle's split size (BleManager.f24464j), never raised
HEAD = bytes.fromhex("A1B2C3D4")
SCAN_S = 10.0                    # the app scans for 10-15 s
REPLY_S = 8.0                    # BLELoader gives a command 8 s before "No Response"


class BluetoothError(Exception):
    """Bluetooth isn't usable here, or the lock couldn't be reached over it."""


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
    from bleak import BleakScanner
    from bleak.exc import BleakError

    async def scan():
        return await BleakScanner.find_device_by_filter(lambda d, adv: _name_of(d, adv) == name, timeout=timeout)
    try:
        return asyncio.run(scan()) is not None
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

    def connect(self, timeout: float = SCAN_S) -> None:
        """Find the lock by name and connect. Raises BluetoothError."""
        self._run(self._connect(timeout), timeout + 20)

    async def _connect(self, timeout: float) -> None:
        from bleak import BleakClient, BleakScanner
        from bleak.exc import BleakError
        try:
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: _name_of(d, adv) == self.name, timeout=timeout)
            if device is None:
                raise BluetoothError(f"The lock isn't in Bluetooth range of this computer ({timeout:.0f} s scan).")
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
