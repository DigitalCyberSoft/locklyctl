# Phone apps (Android, iOS)

The phone apps are built with [BeeWare Briefcase](https://briefcase.readthedocs.io)
from the config in the repo's `pyproject.toml`. They contain:

- `locklyctl/`: the same Python package as the desktop app, unchanged
- `mobile/locklyctl_app/`: a few lines of [Toga](https://toga.readthedocs.io)
  that open a web view

When the app starts, it runs locklyctl's localhost bridge
(`locklyctl/gui/server.py`) on `127.0.0.1` with a random port and a one-time
token, then loads the desktop page from it. Anything else on the phone that
finds the port is refused without the token.

On Android, pycryptodome comes from Chaquopy's package index. It has no iOS
build, so the iOS app uses locklyctl's pure-Python ciphers
(`locklyctl/_purecrypto.py`). CI runs the whole test suite
with them as well (`LOCKLYCTL_PURE_CRYPTO=1`), and they're checked byte for
byte against pycryptodome.

## Get a build

GitHub Actions builds both on every push to `master` (the *Build apps*
workflow):

- `locklyctl-android.apk`: a debug-signed APK you can install directly.
- `locklyctl-ios-simulator.zip`: runs in the iOS Simulator on a Mac (unzip,
  then drag `locklyctl.app` onto a running Simulator).

## Build it yourself

You need Python 3.13 and Briefcase, run from the repo root:

```bash
python3.13 -m venv .venv-mobile && . .venv-mobile/bin/activate
pip install briefcase==0.4.7
```

**Android** (any OS). Briefcase downloads a JDK and the Android SDK the first
time, and asks you to accept Google's SDK licences.

```bash
briefcase run android                          # emulator or USB-connected phone
briefcase package android -p debug-apk         # → dist/*.apk
```

**iOS** (a Mac with Xcode).

```bash
briefcase run iOS                              # Simulator
```

To run it on your iPhone, open the generated Xcode project
(`build/locklyctl_app/ios/xcode/locklyctl.xcodeproj`), choose your Apple ID
team under *Signing & Capabilities*, select your phone and press Run. A free
Apple ID works, but the app stops opening after 7 days until you run it from
Xcode again; a paid developer account lasts a year and allows TestFlight.

## Icons

`mobile/icons/` holds every size Briefcase asks for, rendered from one SVG by
`python3 mobile/icons/make_icons.py` (needs Google Chrome).
