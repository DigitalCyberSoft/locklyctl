# locklyctl

An **unofficial**, menu-driven command-line tool for controlling Lockly smart
locks from a computer — no phone required. It talks to Lockly's cloud with the
same protocol the official app uses.

> Not affiliated with, authorised by, or endorsed by Lockly Security Inc. Use it
> only with your own account and locks. Your credentials and the derived keys
> stay on your machine.

## What it does

| Feature | Status |
|---|---|
| Log in, list locks | ✅ works |
| Unlock / Lock | ✅ verified protocol (LocklyHA, real hardware) |
| Status (silent cached, or live) | ✅ |
| Access log (last 7 days) | ✅ |
| People view (desktop app): everyone's access across all doors, merged by person | ✅ reads cloud + lock; merging is local to this computer |
| Add a person to a door with a PIN (always), change PIN, remove from a door — Visage/Vision | ✅ tested on a Visage (create, verify on lock and cloud, change PIN, delete) |
| Add a person with set hours or between dates; remove one fingerprint/face/card — Visage/Vision | ⚠️ frames checked against the app and lock replies; not yet run on a lock |
| Guest PIN codes — add (remote), list, delete | ✅ on Visage/Vision now written to the lock (earlier versions only saved them in the cloud); PGD7Y unverified |
| Remote enrolment — add permanent PIN user (cloud → hub) | ✅ uses the verified `acu/save` path |
| Welcome Mode, 8-slot `0x9B` (Visage, Vision): read, set, off | ✅ tested on a Visage (set, read back, restore) |
| Welcome Mode, single-slot `0x46` (PGD7Y and other Touch models): read, set, always-on, off | ⚠️ frames checked field by field against the app; not yet tried on a lock |
| Rain Mode `0x56`: read, on, off | ✅ tested on a Visage (on, read back, off) |
| User slots direct to lock: PIN `0x4B`, list `0x50`, delete `0x4D` | ⚠️ checked against the app; these are for slot-based locks (e.g. PGD7Y). A Visage ignores them — it manages users with a different command (`0x73`, not built yet) |
| Face enrolment `0x7D` + `0x92` + cloud registration (Visage only) | ⚠️ checked against the app; needs someone at the lock, not yet tried |
| Fingerprint capture `0x7D` (Visage/Vision) | ⚠️ frame checked against the app; the code lookup (`getFpCode`) and cloud registration aren't built yet |

Transport is automatic: locks behind a classic hub use the `senddata` relay;
Wi-Fi-native (Visage) and Matter-hub locks use Lockly's MQTT device channel.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[keyring]'
locklyctl
```

or just `./run.sh`, which does the same on first run. Requires Python 3.10+.
Dependencies: `requests`, `pycryptodome`, `paho-mqtt`; the optional `keyring`
extra stores the saved login in the OS keyring.

## Use

Run `locklyctl`, enter your Lockly email and password, then pick a lock and an
action from the menus. If you choose to stay logged in, the login credential is
kept in the OS keyring (with the `keyring` extra and a working backend), else in
`~/.locklyctl/config.json` (owner-only); the session renews itself before the
token expires. The credential is password-equivalent, so `--logout` on shared
machines. `locklyctl --debug` prints protocol-level logging,
`--logout` forgets the saved login, `--help` lists the options.

The lock list starts with each hub lock's cached status (silent, no wake);
*Refresh all statuses* queries every lock live, which wakes them.

### Commands (scripts, cron)

Once you've saved a login from the menus, single actions run without them:

```bash
locklyctl list [--json]                 # locks + silent hub-cached status
locklyctl status "Office" [--live] [--json]
locklyctl lock "Office"
locklyctl unlock "Office" --yes         # --yes required when nothing can ask
locklyctl welcome "Front" set --days weekdays --start 08:00 --end 18:00 --send
locklyctl welcome "Front" off --send
locklyctl rain "Office" on --send
```

A lock is named by its name (case-insensitive, unique prefix fine) or ID.
Experimental commands (`welcome`, `rain`) only print the frame unless `--send`
is given; `welcome` picks the command form by model, or takes `--form 9B|46`.
Exit status: 0 ok, 1 the lock/cloud failed, 2 usage error. Messages go to
stderr, results to stdout.

## Desktop app

A window version of the same tool: every door as a plate showing its state,
lock and *hold to unlock* (press and hold until the button fills, so a stray
click can't open a door), access log, guest PINs, Welcome/Rain Mode (preview by
default) and a connection test. It uses the same sign-in as the menus.

- **Windows / macOS:** download `locklyctl-windows.zip` or `locklyctl-macos.zip`
  from the *Desktop app* workflow run in the Actions tab (or from a release when a
  `v*` tag is pushed). The builds are unsigned: on macOS right-click
  `locklyctl.app` → *Open* the first time; on Windows choose *More info* → *Run
  anyway* if SmartScreen warns. The macOS build is for Apple silicon.
- **From source, any OS:** `pip install -e '.[gui,keyring]'`, then
  `locklyctl gui` (or `locklyctl-gui`). From a checkout, `./gui.sh` does the
  setup on first run and opens the app. On Linux the extra
  pulls in pywebview's Qt backend; if your system has WebKitGTK and PyGObject,
  pywebview uses those instead.

The page lives in `locklyctl/gui/web` (plain HTML/CSS/JS, no build step) and
calls `locklyctl/gui/api.py`. To work on it in a browser without a lock, serve
the repo root (`python -m http.server`) and open
`http://localhost:8000/locklyctl/gui/web/index.html?mock=/tests/gui/mock.js`.

To build the bundle yourself: `pip install '.[gui,keyring]' pyinstaller &&
pyinstaller --noconfirm packaging/locklyctl-gui.spec`.

## How it works / provenance

The cryptography, BLE frame building and response parsing (`protocol.py`) are
reused verbatim from [Forcky's LocklyHA](https://github.com/Forcky/LocklyHA)
(MIT — see `LICENSE.LocklyHA`), where they are validated on real hardware. The
synchronous REST/MQTT transport and the menu are new. The Welcome Mode / Rain
Mode frame builders (`features.py`) were decoded from the Lockly Android app and
are **not** hardware-tested — the tool flags them as experimental and asks
before sending.

The remote-enrolment feature matters because recent Lockly apps force Bluetooth
for adding users, even though the lock accepts these commands over the hub/MQTT
just like unlock. The cloud `acu/save` path adds a permanent PIN user remotely
and is the reliable route; the direct `0x4B` PIN push, user list/delete and
fingerprint capture are traced from the app and offered as experimental (dry-run
by default — they print the frame and ask before transmitting). The protocol is
written up in `docs` / the companion docs site under *BLE → enrolment*.

Protocol reference: the companion docs at
<https://digitalcybersoft.github.io/lockly_api_docs/>.

## Safety

- A lock's master code + UUID together can forge any command; this tool holds
  them only in memory during a session.
- "Experimental" actions build correctly-framed commands with unverified
  contents. A wrong field is most likely rejected by the lock, but test on one
  lock you can physically reach before relying on it.

## Licence

MIT (new code). Bundled `protocol.py` and `certs/` derive from LocklyHA (MIT).
The desktop app bundles the Archivo font (SIL OFL 1.1). See `NOTICE`.
