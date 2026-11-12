# PyInstaller spec for the locklyctl desktop app.
#   pip install '.[gui,keyring]' pyinstaller
#   pyinstaller --noconfirm packaging/locklyctl-gui.spec
# Output: dist/locklyctl/ (Windows, Linux) or dist/locklyctl.app (macOS).
import os
import sys

from PyInstaller.utils.hooks import collect_submodules

from locklyctl import __version__

root = os.path.abspath(os.path.join(SPECPATH, ".."))
pkg = os.path.join(root, "locklyctl")

a = Analysis(
    [os.path.join(root, "packaging", "gui_entry.py")],
    pathex=[root],
    datas=[
        (os.path.join(pkg, "certs"), "locklyctl/certs"),
        (os.path.join(pkg, "gui", "web"), "locklyctl/gui/web"),
    ],
    # keyring picks its backend at run time; paho is imported lazily.
    hiddenimports=collect_submodules("keyring.backends") + ["paho.mqtt.client"],
    excludes=["tkinter"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="locklyctl",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="locklyctl")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="locklyctl.app",
        bundle_identifier="io.dcs.locklyctl",
        version=__version__,
        info_plist={"NSHighResolutionCapable": True, "CFBundleDisplayName": "locklyctl"},
    )
