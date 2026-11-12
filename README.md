# locklyctl

**Unofficial** control of Lockly smart locks from your computer, your phone or
an AI assistant, with no Lockly app needed. It talks to Lockly's cloud the same
way the official app does.

- **Desktop app** for Windows, macOS and Linux
- **Phone app** for Android and iOS
- **Command line**, with menus or as single commands for scripts and cron
- **MCP server**, so Claude and other AI assistants can check and run your doors

> Not affiliated with, authorised by, or endorsed by Lockly Security Inc. Use it
> only with your own account and locks. Your credentials and the keys derived
> from them stay on your device.

<p>
  <img src="docs/screenshots/desktop-doors.png" alt="Desktop app: every door as a numbered plate with its state and battery; the selected door shows Lock, Hold to unlock and its access log" width="100%">
</p>
<p>
  <img src="docs/screenshots/phone-doors.png" alt="Phone app: the door plates above the selected door's controls" width="32%">
  <img src="docs/screenshots/phone-people.png" alt="Phone app, dark mode: one person's access across three doors, with their fingerprints and face" width="32%">
  <img src="docs/screenshots/phone-door.png" alt="Phone app: a door's status and guest PINs" width="32%">
</p>
<p>
  <img src="docs/screenshots/desktop-people.png" alt="People view: everyone with access, and for the selected person, every door they can open, when, and with what" width="49%">
  <img src="docs/screenshots/desktop-schedules.png" alt="Dark mode: a door's Welcome Mode schedule" width="49%">
</p>

<sub>Screenshots use made-up sample data (`tests/gui/mock.js`). Rendered with
`node tests/gui/screenshots.mjs`.</sub>

## What it does

| Feature | Status |
|---|---|
| Sign in, list doors, status (silent hub cache, or live) | ✅ |
| Lock / unlock | ✅ verified protocol (LocklyHA, real hardware) |
| Access log (last 7 days) | ✅ |
| People: everyone's access across all doors, merged by person | ✅ reads cloud and lock; merging is kept on your device |
| Visage/Vision: add a person with a PIN (always, set hours, or between dates), change PIN, remove from a door | ✅ tested on a Visage: created, checked on the lock and the cloud, changed, deleted |
| Visage/Vision: remove one fingerprint, face or card | ⚠️ checked against the app and lock replies; not yet run on a lock |
| Guest PIN codes: add, list, delete | ✅ on Visage/Vision written to the lock; PGD7Y unverified |
| Welcome Mode, 8-slot `0x9B` (Visage, Vision): read, set, off | ✅ tested on a Visage (set, read back, restore) |
| Welcome Mode, single-slot `0x46` (PGD7Y and other Touch models) | ✅ read works; ⚠️ set/off checked against the app, not yet sent to a lock |
| Rain Mode `0x56`: read, on, off | ✅ tested on a Visage (on, read back, off) |
| Slot locks (e.g. PGD7Y): PIN `0x4B`, list `0x50`, delete `0x4D` | ⚠️ checked against the app; not yet working on a lock |
| Face enrolment `0x7D` + `0x92` (Visage), fingerprint capture `0x7D` | ⚠️ checked against the app; needs someone at the lock, not yet tried |

Commands reach a lock through its hub (`senddata` relay) or, for Wi-Fi locks
like the Visage, Lockly's MQTT device channel. Locks with no hub are listed but
can only be reached over Bluetooth, which locklyctl doesn't do yet.

## Desktop app

Every door is a plate showing its state. Select one to lock it, *hold to
unlock* (press and hold until the button fills, so a stray click can't open a
door), and see its access log, guest PINs, Welcome/Rain Mode and a connection
test. Star a door to pin it to **Favorites** at the top of the list, where
Lock and Unlock are a single tap each, without the hold. The **People** view
shows everyone who can get in, across every door, and lets you give, change
and take away access.

- **Windows / macOS:** download `locklyctl-windows.zip` or `locklyctl-macos.zip`
  from the latest [*Build apps*](../../actions/workflows/build.yml) run, or from
  a [release](../../releases). The builds are unsigned: on macOS right-click
  `locklyctl.app` → *Open* the first time; on Windows choose *More info* → *Run
  anyway* if SmartScreen warns. The macOS build is for Apple silicon.
- **From source, any OS:** `./gui.sh` sets everything up on first run and opens
  the app. By hand: `pip install -e '.[gui,keyring]'`, then `locklyctl-gui`.
  On Linux this uses pywebview's Qt backend, or WebKitGTK if PyGObject is
  installed.
- **In your web browser:** `locklyctl-gui --browser` serves the app on
  `127.0.0.1` behind a one-time token and opens it, with no pywebview needed.

## Phone app (Android, iOS)

The same app on your phone. It runs locklyctl's Python code inside the app
(BeeWare Briefcase and Toga) and shows the desktop page in the phone's web
view.

- **Android:** download `locklyctl-android.apk` from the latest *Build apps*
  run or a release, open it on the phone and allow installing from that
  source.
- **iOS:** download `locklyctl-ios-unsigned.ipa` and install it with
  Sideloadly or AltStore (Windows or macOS), which sign it with your Apple ID.
  A free Apple ID works, but the app then has to be reinstalled every 7 days.
  No Mac needed; see [mobile/README.md](mobile/README.md).

Phones have no keyring, so a remembered sign-in is kept in the app's private
storage, which other apps can't read.

## AI assistants (MCP)

`locklyctl-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io)
server. Sign in once with `locklyctl` (choose to stay signed in), then add it
to your assistant. For Claude Code:

```bash
pip install -e '.[mcp,keyring]'
claude mcp add locklyctl -- "$(command -v locklyctl-mcp)"
```

For Claude Desktop, add this to `claude_desktop_config.json`:

```json
{ "mcpServers": { "locklyctl": { "command": "/full/path/to/locklyctl-mcp" } } }
```

By default it can only look and lock: `list_doors`, `check_door`,
`door_activity`, `list_people`, `read_door_modes` and `lock_door`. Two flags add
more:

| Flag | Adds |
|---|---|
| `--allow-writes` | `grant_access`, `change_pin`, `revoke_access`, `set_welcome_mode`, `set_rain_mode` |
| `--allow-unlock` | `unlock_door` |

Tools a flag doesn't allow aren't offered at all, so the assistant never sees
them. Existing PINs are never returned; a PIN the server creates is returned
once, in the reply that created it.

## Command line

```bash
./run.sh                    # sets up a venv on first run, then opens the menus
```

or `pip install -e '.[keyring]'` and run `locklyctl`. Requires Python 3.10+.
Sign in, then pick a door and an action from the menus. If you stay signed in,
the credential goes to the OS keyring (with the `keyring` extra), otherwise to
`~/.locklyctl/config.json` (owner-only). It's equivalent to your password, so
use `--logout` on shared machines. `--debug` prints protocol logging.

Once a login is saved, single actions run without the menus:

```bash
locklyctl list [--json]                 # doors + silent hub-cached status
locklyctl status "Office" [--live] [--json]
locklyctl lock "Office"
locklyctl unlock "Office" --yes         # --yes required when nothing can ask
locklyctl welcome "Front" set --days weekdays --start 08:00 --end 18:00 --send
locklyctl welcome "Front" off --send
locklyctl rain "Office" on --send
```

A door is named by its name (case-insensitive, a unique prefix is fine) or ID.
`welcome` and `rain` only print the frame unless `--send` is given. Exit
status: 0 ok, 1 the lock or cloud failed, 2 usage error.

## Building

Every push to `master` runs [`.github/workflows/build.yml`](.github/workflows/build.yml):
tests (including a run with the iOS app's pure-Python ciphers), then the
Windows and macOS apps (PyInstaller), the Android APK, and the iOS Simulator
app and unsigned iPhone `.ipa` (Briefcase, then `xcodebuild`). Pushing a `v*`
tag also attaches them to a release.

By hand:

```bash
pip install '.[gui,keyring]' pyinstaller && pyinstaller --noconfirm packaging/locklyctl-gui.spec
pip install briefcase && briefcase package android -p debug-apk    # needs the Android SDK
```

To work on the page (`locklyctl/gui/web`: plain HTML/CSS/JS, no build step)
without a lock, serve the repo root with `python -m http.server` and open
`http://localhost:8000/locklyctl/gui/web/index.html?mock=/tests/gui/mock.js`.

## How it works / provenance

The cryptography, BLE frame building and response parsing in `protocol.py`
come from [Forcky's LocklyHA](https://github.com/Forcky/LocklyHA) (MIT, see
`LICENSE.LocklyHA`), where they were validated on real hardware. The REST/MQTT
transport, the user-management, Welcome and Rain Mode frames (`features.py`,
decoded from the Lockly Android app and tested where the table above says so),
the apps and the MCP server are new.

Recent Lockly apps insist on Bluetooth for adding users, yet a lock accepts
these commands over its hub or MQTT just like unlock, which is what lets
locklyctl manage people remotely.

Protocol reference: <https://digitalcybersoft.github.io/lockly_api_docs/>.

## Safety

- A lock's master code and UUID together can forge any command. locklyctl only
  holds them in memory while it runs.
- Commands marked ⚠️ are correctly framed but their contents aren't proven on
  a lock. A wrong field is most likely rejected, but try them on a door you
  can physically reach first.

## Licence

MIT (new code). `protocol.py` and `certs/` derive from LocklyHA (MIT). The apps
bundle the Archivo font (SIL OFL 1.1). See `NOTICE`.
