"""Keeping a firmware install going once it has started.

Once the first firmware write has gone to a lock, stopping helps no one: the
lock is fetching or installing its files, or its Bluetooth chip is half
written. From then until the install is confirmed or has failed
(``firmware_install``):

- other commands to that lock from other threads are refused (``blocking``),
  so a status check or a second Bluetooth connection can't get in the way;
- this computer is kept from sleeping, shutting down or suspending on a
  closed lid (systemd-inhibit on Linux, caffeinate on macOS,
  SetThreadExecutionState on Windows);
- on the main thread, Ctrl+C stops it only when pressed FORCE_PRESSES times
  within FORCE_WINDOW_S, and SIGTERM and SIGHUP are ignored, so a closed
  terminal doesn't end it either.

The GUI asks ``running()`` before it lets its window close or its user sign out.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

_LOG = logging.getLogger("locklyctl")

FORCE_PRESSES = 3           # Ctrl+C presses that stop an install anyway
FORCE_WINDOW_S = 5.0        # ... within this many seconds

_lock = threading.Lock()
_active: dict[str, dict] = {}       # lock ID -> {"what", "thread", "since"}


def running() -> list[str]:
    """What is being installed now, one line per lock."""
    with _lock:
        return [a["what"] for a in _active.values()]


def blocking(lock_id: str) -> str | None:
    """What another thread is installing on this lock, or None."""
    with _lock:
        a = _active.get(lock_id)
        return a["what"] if a and a["thread"] != threading.get_ident() else None


@contextmanager
def firmware_install(lock_id: str, what: str):
    """Hold the lock and this computer for an install that has started.
    Yields a warning ("" when everything is in place): why this computer may
    still sleep. Raises ValueError when an install already runs on that lock."""
    with _lock:
        if lock_id in _active:
            raise ValueError(f"A firmware install is already running: {_active[lock_id]['what']}.")
        _active[lock_id] = {"what": what, "thread": threading.get_ident(), "since": time.monotonic()}
    awake = _KeepAwake()
    held = _HeldSignals(what) if threading.current_thread() is threading.main_thread() else None
    try:
        if held:
            held.hold()
        yield awake.warning
    finally:
        if held:
            held.release()
        awake.release()
        with _lock:
            _active.pop(lock_id, None)


def hold_signals_while_installing() -> None:
    """For a process whose installs run on other threads (``locklyctl-gui
    --browser``): from now on Ctrl+C, SIGTERM and SIGHUP act as usual while
    no install runs, and as during an install while one does. Main thread only."""
    held = _HeldSignals(None)
    held.hold()


def _say(text: str) -> None:
    try:
        print(text, file=sys.stderr, flush=True)
    except OSError:             # the terminal is gone (SIGHUP); the install goes on
        pass


class _HeldSignals:
    """Ctrl+C, SIGTERM and SIGHUP while an install runs. ``what`` None: the
    process-wide form, which defers to the usual behaviour when none runs."""

    def __init__(self, what: str | None) -> None:
        self.what = what
        self.presses: list[float] = []
        self.old: dict[int, object] = {}

    def hold(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is not None:
                self.old[sig] = signal.signal(sig, self._handle)

    def release(self) -> None:
        for sig, handler in self.old.items():
            signal.signal(sig, handler)
        self.old.clear()

    def _handle(self, signum: int, frame) -> None:
        what = self.what or "; ".join(running())
        if not what:                            # process-wide form, nothing installing
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            signal.signal(signum, signal.SIG_DFL)
            os.kill(os.getpid(), signum)
            return
        if signum != signal.SIGINT:
            _say(f"\n  locklyctl got {signal.Signals(signum).name} and keeps going: a firmware install "
                 f"is running ({what}).")
            _LOG.info("ignored %s during a firmware install (%s)", signal.Signals(signum).name, what)
            return
        now = time.monotonic()
        self.presses = [t for t in self.presses if now - t < FORCE_WINDOW_S] + [now]
        left = FORCE_PRESSES - len(self.presses)
        if left <= 0:
            _LOG.info("firmware install stopped with Ctrl+C (%s)", what)
            _say("\n  Stopped. The lock may still be installing: check it with "
                 "`locklyctl firmware LOCK` before doing anything else to it.")
            raise KeyboardInterrupt
        _say(f"\n  A firmware install is running ({what}). Stopping now can leave the lock without "
             f"working firmware. Press Ctrl+C {left} more time{'s' if left > 1 else ''} within "
             f"{FORCE_WINDOW_S:.0f} s to stop anyway.")


class _KeepAwake:
    """Keeps this computer from sleeping until release(). ``warning`` says why
    it couldn't, when it couldn't."""

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    INHIBIT_WHY = "Installing firmware on a lock"

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.windows = False
        self.warning = ""
        if sys.platform == "win32":
            self._windows()
        elif sys.platform in ("android", "ios") or hasattr(sys, "getandroidapilevel"):
            self.warning = "Keep this phone awake and locklyctl open until the install finishes."
        elif sys.platform == "darwin":
            self._spawn(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
        else:
            self._spawn(["systemd-inhibit", "--what=sleep:shutdown:idle:handle-lid-switch",
                         "--who=locklyctl", f"--why={self.INHIBIT_WHY}", "--mode=block",
                         "sleep", "infinity"])

    def _windows(self) -> None:
        import ctypes
        # Per thread, so the install's own thread asks (kernel32 SetThreadExecutionState).
        if ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED):
            self.windows = True
        else:
            self.warning = "Windows didn't let locklyctl keep this computer awake: keep it awake yourself."

    def _spawn(self, cmd: list[str]) -> None:
        if shutil.which(cmd[0]) is None:
            self.warning = f"{cmd[0]} isn't on this computer, so it may sleep: keep it awake yourself."
            return
        try:
            # Its own session: Ctrl+C or a closed terminal reaches locklyctl, not it.
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, start_new_session=True)
        except OSError as e:
            self.warning = f"Couldn't keep this computer awake ({cmd[0]}: {e}): keep it awake yourself."
            return
        try:                    # systemd-inhibit exits at once when it's refused (polkit)
            self.proc.wait(0.3)
        except subprocess.TimeoutExpired:
            return
        err = (self.proc.stderr.read() or b"").decode(errors="replace").strip() if self.proc.stderr else ""
        self.warning = (f"Couldn't keep this computer awake ({cmd[0]} exited: {err or self.proc.returncode}): "
                        "keep it awake yourself.")
        self._close_pipe()
        self.proc = None

    def _close_pipe(self) -> None:
        if self.proc is not None and self.proc.stderr is not None:
            self.proc.stderr.close()

    def release(self) -> None:
        if self.windows:
            import ctypes
            ctypes.windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
            self.windows = False
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
            self._close_pipe()
            self.proc = None
