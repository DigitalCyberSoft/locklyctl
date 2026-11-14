"""The phone app: the desktop app's page in a native web view.

Python runs inside the app (Chaquopy on Android, the BeeWare Python framework
on iOS). It starts locklyctl's localhost bridge (locklyctl/gui/server.py) on
a random port with a per-launch token and points a Toga WebView at it, so the
phone runs the same page and the same Api as the desktop app.

Settings and the remembered login live in the app's private data folder.
Phones have no keyring, so the login is stored as a file there; the OS
sandbox keeps other apps out of it.
"""
from __future__ import annotations

import os

import toga


class LocklyctlApp(toga.App):
    def startup(self) -> None:
        os.environ.setdefault("LOCKLYCTL_HOME", str(self.paths.data))
        from locklyctl import config
        from locklyctl.gui.server import Bridge

        config._setup_logging()
        self.bridge = Bridge().start()
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = toga.WebView(url=self.bridge.url, flex=1)
        self.main_window.show()

    def on_exit(self) -> bool:
        # Not while a firmware install runs: stopping the bridge would cut it
        # off midway. The install's page says the app stays open until it ends.
        if self.bridge.api.installing():
            return False
        self.bridge.stop()
        return True


def main() -> LocklyctlApp:
    return LocklyctlApp("locklyctl", "com.digitalcybersoft.locklyctl_app")
