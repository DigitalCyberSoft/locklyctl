// Browser stand-in for locklyctl/gui/api.py, for designing the GUI without a
// lock or a Lockly account. Serve the repo root and open
//   http://localhost:8000/locklyctl/gui/web/index.html?mock=/tests/gui/mock.js
// Optional &scene=signin|door|access|offline|offline-many|schedules|edit|people|person-detail|maint|firmware|settings
// scripts a starting state; &device=phone is the phone app (no Bluetooth, "this phone").

const params = new URLSearchParams(location.search);
const scene = params.get("scene") || "";
const device = params.get("device") === "phone" ? "phone" : "computer";
const now = Date.now() / 1000;
const mk = (id, name, unit, rest, model, hub, state, battery, extra = {}) => ({
  id, name, unit, rest, model, hub, state, battery, lowBattery: battery != null && battery < 20,
  doorOpen: null, firmware: "", cached: false, checkedAt: state === "unknown" ? null : now - 240,
  welcomeForm: model === "PGD7Y" ? "46" : "9B", faceCapable: model === "PGK728WRHK",
  route: hub ? "hub" : model === "PGD7Y" ? "bluetooth" : "wifi", eventLog: model === "PGK728WRHK", favorite: false,
  keepUnlockedUntil: null, keepMaxMinutes: 1439, clockKnown: false, clockOffset: null,
  timeZone: "UTC+8", computerTimeZone: "UTC+8", takesTimeZone: model === "PGK728WRHK", hidden: false,
  hasWifi: model === "PGK728WRHK", accessUsers: model !== "PGD7Y", offlineCodes: model !== "PGD7Y",
  firmwareKind: model === "PGK728WRHK" ? "multi" : model === "PGK798HK" ? "camera" : "single", ...extra,
});

const locks = [
  mk("4d00", "2A Office", "2A", "Office", "PGD7Y", "PGH200U00000001T", "locked", 82, { doorOpen: false, firmware: "1.02.51" }),
  mk("3200", "3 Server", "3", "Server", "PGD7Y", "PGH200U00000002T", "locked", 64, { firmware: "1.02.51" }),
  mk("3900", "4C", "4C", "", "PGD7Y", "", "unknown", null),
  mk("4c00", "3B Patio", "3B", "Patio", "PGD7Y", "", "asleep", null),
  mk("2d32", "3A", "3A", "", "PGK728WRHK", "", "unlocked", 47, { keepUnlockedUntil: "18:30", firmware: "1.00.95",
    clockKnown: true, clockOffset: 2 }),
  mk("4300", "2C Spare", "2C", "Spare", "PGK728WRHK", "", "unknown", null),
  mk("1e00", "3C Vision", "3C", "Vision", "PGK798HK", "PGH200U00000003T", "locked", 91, { firmware: "1.14.16" }),
  mk("2d3e", "2A Front", "2A", "Front", "PGK728WRHK", "", "locked", 18, { favorite: true, firmware: "1.00.87",
    clockKnown: true, clockOffset: -11040 }),
  mk("1c00", "4C New", "4C", "New", "PGK728WRHK", "", "unknown", null, { clockKnown: true, clockOffset: null }),
  mk("3000", "1204", "1204", "", "PGK728WRHK", "", "asleep", null, { favorite: true }),
  mk("2600", "1206", "1206", "", "PGK728WRHK", "", "unknown", null),
  mk("263e", "3C", "3C", "", "PGK728WRHK", "", "unknown", null),
  mk("4500", "2C", "2C", "", "PGK728WRHK", "", "unknown", null),
];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const find = (id) => locks.find((l) => l.id === id);
// The door list's order, as locklyctl/view.py keeps it: by name until a door is
// moved, then the saved order (doors it doesn't know go last).
let order = null;
const ordered = () => {
  if (!order) return [...locks].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  const pos = (l) => { const i = order.indexOf(l.id); return i < 0 ? Infinity : i; };
  return [...locks].sort((a, b) => pos(a) - pos(b));
};
let fwPolls = 0, fwParts = [];             // the mock firmware install's progress
let connection = "auto";                   // Api.connection's saved setting

// Everyone on every door, as Api.people and Api.door_access shape them. Pausing and removing change it.
const access = (lockId, userId, person, role, when, extra = {}) => {
  const l = find(lockId);
  return { lockId, doorName: l.name, doorUnit: l.unit, doorRest: l.rest, doorModel: l.model, doorRoute: l.route,
    doorAccessUsers: l.accessUsers, userId, person, role, access: when, enabled: true, status: "Y", suspended: false,
    hasPin: false, pin: "", fingerprints: 0, faces: 0, cards: 0, twoStep: false, recordKey: "", ...extra };
};
let roster = [];
// Offline codes on record per door (made-up), and how many "this computer" made today.
// &scene=offline-many starts at the app's daily count, so making one asks first.
const OAC_STEPS = [
  "Double-tap the Lockly logo button. The four circles on the keypad flash: it's ready for the code.",
  "Enter the code, then press the logo.",
  "Enter the code again, then press the logo. A new code has to be entered twice the first time.",
];
const OAC_NOTE = "Enter it within a minute of the circles flashing. If the door doesn't open, do the steps again. The code works once and can't be deleted or changed.";
const oacShown = (c) => `${c.slice(0, 2)} ${c.slice(2)}`;
const oacRecords = { "2d3e": [{ id: 9101, name: "Plumber", once: true, code: "4071" }] };
const oacToday = { "2d3e": scene === "offline-many" ? 4 : 0 };
// The app lock: &scene=locked starts locked (the passcode is 2468); &scene=applock has one set.
let passcode = ["locked", "applock"].includes(scene) ? "2468" : "", unlocked = scene !== "locked", wrong = 0;

window.pywebview = {
  api: {
    async capabilities() {
      return { device, bluetooth: device === "phone" ? "Bluetooth needs the optional 'bleak' package (pip install 'locklyctl[bluetooth]')." : "",
        commandLine: device === "computer", version: "0.3.0", logPath: "/home/owner/.locklyctl/locklyctl.log" };
    },
    async session() {
      await wait(200);
      if (!unlocked) return { signedIn: false, locked: true, wait: 0, email: "" };
      return scene === "signin" ? { signedIn: false, email: "owner@example.com" } : { signedIn: true, email: "owner@example.com" };
    },
    async unlock_app(code) {
      await wait(250);                     // the key derivation takes a moment
      if (wrong >= 5) throw new Error("Too many wrong passcodes. Try again in 30 s.");
      if (code !== passcode) { wrong++; throw new Error("Wrong passcode."); }
      unlocked = true; wrong = 0;
      return this.session();
    },
    async app_lock() { return { on: !!passcode, available: "" }; },
    async set_app_lock(code, current) {
      await wait(250);
      if (passcode && current !== passcode) throw new Error("Wrong passcode.");
      passcode = code;
      return { on: true, strength: /^\d{1,9}$/.test(code) || code.length < 8 ? "short" : "long", leftInKeyring: [] };
    },
    async remove_app_lock(current) { await wait(250); if (current !== passcode) throw new Error("Wrong passcode."); passcode = ""; return { on: false }; },
    async lock_now() { unlocked = false; return { locked: true, wait: 0 }; },
    async erase_saved_data() { passcode = ""; unlocked = true; return { signedIn: false, email: "" }; },
    async sign_in(email) { await wait(400); return { signedIn: true, email }; },
    async sign_out() { return { signedIn: false, email: "owner@example.com" }; },
    async locks() { await wait(150); return ordered().map((l) => ({ ...l })); },
    async set_hidden(id, on) { await wait(120); Object.assign(find(id), { hidden: !!on }); return { ...find(id) }; },
    async move_lock(id, by, includeHidden) {        // view.move: step past the doors the list shows
      await wait(120);
      const full = ordered(), target = find(id);
      const frame = full.filter((l) => includeHidden || !l.hidden || l === target);
      const i = frame.indexOf(target), j = Math.max(0, Math.min(frame.length - 1, i + by));
      if (j !== i) {
        const rest = frame.filter((l) => l !== target), others = full.filter((l) => l !== target);
        others.splice(j < rest.length ? others.indexOf(rest[j]) : others.indexOf(rest[rest.length - 1]) + 1, 0, target);
        order = others.map((l) => l.id);
      }
      return ordered().map((l) => ({ ...l }));
    },
    async check(id) {                      // doors answer at different speeds; some stay asleep
      const l = find(id), n = parseInt(id, 16);
      await wait(500 + (n % 7) * 350);
      if (l.state === "unknown" || l.state === "asleep") l.state = n % 3 ? "locked" : "asleep";
      l.offline = l.state === "asleep" && l.route === "wifi";   // what Lockly's server says (3005)
      if (l.offline && connection === "auto" && n % 2 === 0) {  // next to this computer: Bluetooth answers
        Object.assign(l, { state: "locked", via: "bluetooth", wifiOffline: true, clockKnown: true, clockOffset: -1,
          clockFix: { was: -11040, fixed: true, detail: "" } });
      } else if (l.offline && connection === "auto") {
        l.bluetoothNote = "The lock isn't in Bluetooth range of this computer (10 s scan).";
      }
      return { ...l, cached: false, checkedAt: Date.now() / 1000 };
    },
    async lock(id) { await wait(700); Object.assign(find(id), { state: "locked" }); return { ok: true, detail: "", lock: { ...find(id) } }; },
    async unlock(id) { await wait(700); Object.assign(find(id), { state: "unlocked" }); return { ok: true, detail: "", lock: { ...find(id) } }; },
    async keep_unlocked(id, minutes) { await wait(500); const until = new Date(Date.now() + minutes * 60000).toTimeString().slice(0, 5); Object.assign(find(id), { keepUnlockedUntil: until, state: "unlocked" }); return { ok: true, until, detail: "", lock: { ...find(id) } }; },
    async end_keep_unlocked(id) { await wait(500); Object.assign(find(id), { keepUnlockedUntil: null, state: "locked" }); return { ok: true, detail: "", lock: { ...find(id) } }; },
    async firmware(id) { await wait(500); return { kind: "multi", release: "1.14.34", title: "New firmware", notes: "Changes in release 1.14.34:\n1. Fixed bugs", answered: true, lockFirmware: "1.00.87", lock: { ...find(id) }, parts: [
      { name: "Front board", current: "1.00.87", offered: "1.00.95", newer: true, sizeKb: 87, byPhone: false },
      { name: "Back board", current: "1.00.72", offered: "1.00.76", newer: true, sizeKb: 35, byPhone: false },
      { name: "Wi-Fi module", current: "2.02.72", offered: "2.02.72", newer: false, sizeKb: 1180, byPhone: false },
      { name: "Bluetooth chip", current: "1.14.24", offered: "1.14.28", newer: true, sizeKb: 280, byPhone: true, byBluetooth: true }],
      install: { blocked: "", parts: [
        { part: "front", name: "Front board", version: "1.00.95", current: "1.00.87" },
        { part: "back", name: "Back board", version: "1.00.76", current: "1.00.72" }] } }; },
    async bluetooth_wifi_scan() {
      await wait(800);
      return [{ ssid: "Building Wi-Fi", rssi: -51, routerType: 3, known: "system", connected: true,
        twinOf: "", suggested: true },
        { ssid: "Guest", rssi: -62, routerType: 3, known: "", connected: false, twinOf: "", suggested: false }];
    },
    async bluetooth_wifi_setup(id, ssid, password) {
      await wait(1500);
      return { ok: true, text: "connected", note: password ? `Saved the password for ${ssid} in the keyring.` : "",
        known: password ? "saved" : "system", lock: { ...find(id) } };
    },
    async forget_wifi_password() { await wait(100); return { forgotten: true }; },
    async connection() { return { mode: connection, bluetooth: "" }; },
    async set_connection(mode) { await wait(100); connection = mode; return { mode, bluetooth: "" }; },
    async bluetooth_check() { await wait(600); return { available: true, inRange: false, detail: "The lock isn't in Bluetooth range of this computer." }; },
    async set_time_zone(id) { await wait(900); return { ok: true, text: "time zone set", zone: "UTC+8", source: "account", via: "server", lock: { ...find(id) } }; },
    async bluetooth_set_time(id) { await wait(900); Object.assign(find(id), { clockKnown: true, clockOffset: -1 }); return { ...find(id) }; },
    async bluetooth_chip_install(id) {
      await wait(400); fwPolls = 0;
      fwParts = [{ part: "ble", name: "Bluetooth chip", version: "1.14.28", current: "1.14.24", state: "files", now: "" }];
      return this.firmware_install_status(id);
    },
    async firmware_install(id, parts) {
      await wait(400); fwPolls = 0;
      fwParts = parts.map((p) => ({ part: p.part, name: p.name, version: p.version, current: p.current, state: "files", now: "" }));
      return this.firmware_install_status(id);
    },
    async firmware_jobs() { return []; },
    async installing() { return []; },
    async firmware_install_status() {      // files, sending, restarting, one part in and one not yet, then done
      await wait(150);
      const all = (state, now) => fwParts.map((p) => ({ ...p, state, now: now ? now(p) : "" }));
      const base = { running: true, ok: null, away: null, error: "", warning: "", versions: {}, clockKnown: false, clockOffset: null };
      const steps = [
        { stage: "download", progress: null, text: "front 1.00.95: downloaded and checked (88348 bytes)", parts: all("files") },
        { stage: "download", progress: 40, text: "Downloading 40 %", parts: all("sending") },
        { stage: "install", progress: 70, text: "Installing 70 %", parts: all("sending") },
        { stage: "verify", progress: null, away: 20, text: "Waiting for the lock to come back online (0 min so far)", parts: all("restarting") },
        { stage: "verify", progress: null, text: "The lock still reports Back board 1.00.72", clockKnown: true, clockOffset: -2,
          parts: fwParts.map((p, i) => ({ ...p, state: i ? "behind" : "done", now: i ? p.current : p.version })) },
      ];
      const step = steps[fwPolls++];
      return step ? { ...base, ...step }
        : { ...base, running: false, ok: true, stage: "done", progress: 100, text: "Installed and confirmed on the lock",
          clockKnown: true, clockOffset: -2, parts: all("done", (p) => p.version) };
    },
    async set_favorite(id, on) { await wait(150); Object.assign(find(id), { favorite: !!on }); return { ...find(id) }; },
    async activity() {
      await wait(200);
      return [
        { time: "2026-10-30 18:42", what: "Fingerprint", who: "Dana" },
        { time: "2026-10-30 17:05", what: "App / remote", who: "owner@example.com" },
        { time: "2026-10-30 09:12", what: "PIN code", who: "Cleaner" },
        { time: "2026-10-29 21:30", what: "Auto-lock", who: "" },
      ];
    },
    async door_access(id) { await wait(200); return roster.filter((r) => r.lockId === id).map((r) => ({ ...r }))
      .sort((x, y) => (x.role !== "owner") - (y.role !== "owner") || x.person.localeCompare(y.person)); },
    async suspend_person(id, userId) { await wait(500); roster.find((r) => r.lockId === id && r.userId === userId).suspended = true; return { ok: true, kept: true }; },
    async resume_person(id, userId) { await wait(500); roster.find((r) => r.lockId === id && r.userId === userId).suspended = false; return { ok: true, source: "stash" }; },
    async add_guest(id, name, pin, duration) {
      await wait(300);
      roster.push(access(id, 60 + roster.length, name, "guest", duration === "permanent" ? "Always" : "Until 2026-11-01 09:00", { hasPin: true, pin }));
      return { ok: true, id: 11 };
    },
    async delete_guest(id, userId) { await wait(300); roster = roster.filter((r) => !(r.lockId === id && r.userId === userId)); return { ok: true }; },
    async welcome(id, form, action, days, start, end, send) { await wait(300); return send ? { ok: true, sent: true, detail: "" } : { ok: true, sent: false, form, frame: "A1B2C3D447E91B706D1C93326E3B5BAF4F14CE7A506C7A10E92138DC72D8BE0EBFAD7197C495B84368AF1090C46A2459A5" }; },
    async rain(id, on, send) { await wait(300); return send ? { ok: false, sent: true, detail: "lock rejected the command: 0C — unsupported" } : { ok: true, sent: false, frame: "A1B2C3D4AF3D71357E0C90F7D1C586542CE28A0D735F6C4A" }; },
    async welcome_state() { await wait(400); return { ok: true, sent: true, detail: "Welcome Mode off" }; },
    async rain_state() { await wait(400); return { ok: true, sent: true, detail: "Rain Mode off" }; },
    async people() {
      await wait(300);
      const by = new Map();
      for (const r of roster) {
        const key = r.person === "Alex Owner" ? "owner@example.com" : `name:${r.person.toLowerCase()}`;
        if (!by.has(key)) by.set(key, { key, name: r.person, email: r.person === "Alex Owner" ? key : "", mergedKeys: r.person === "Dana" ? ["name:dana r"] : [], doors: [] });
        by.get(key).doors.push({ ...r });
      }
      return { failedDoors: [], people: [...by.values()] };
    },
    async merge_people() { return this.people(); },
    async door_access_detail() { await wait(250); return { hasPin: true,
      fingerprints: [{ crdntlId: 1, slot: 1, code: 11, name: "Right thumb" }, { crdntlId: 2, slot: 2, code: 12, name: "Left index" }],
      faces: [{ crdntlId: 3, slot: 1, code: 1001, name: "Alex" }], cards: [] }; },
    async change_pin() { await wait(400); return { ok: true }; },
    async remove_from_door(id, userId) { await wait(400); roster = roster.filter((r) => !(r.lockId === id && r.userId === userId)); return { ok: true }; },
    async remove_credential() { await wait(400); return { ok: true }; },
    async add_to_door(id, name, pin, acc) {
      await wait(400);
      const when = { always: "Always", weekly: "Mon–Fri 09:00–17:59", dates: "2026-11-01 08:00 to 2026-11-03 20:00", hours: "Until 2026-11-01 09:00" }[acc.kind];
      roster.push(access(id, 60 + roster.length, name, acc.kind === "always" ? "family" : "guest", when, { hasPin: !!pin, pin }));
      return { ok: true, userId: 40, slot: 7 };
    },
    async separate_person() { return this.people(); },
    async offline_codes(id) {
      await wait(250);
      return { codes: (oacRecords[id] || []).map((c) => ({ ...c, shown: oacShown(c.code) })),
        madeToday: oacToday[id] || 0, steps: OAC_STEPS, note: OAC_NOTE };
    },
    async make_offline_code(id, name, anyway) {
      await wait(500);
      if ((oacToday[id] || 0) >= 4 && !anyway) {
        return { made: false, madeToday: oacToday[id], ask: "Activate the issued OAC access code before activating a new one." };
      }
      const code = String(1000 + Math.floor(Date.now() / 7) % 9000);
      (oacRecords[id] = oacRecords[id] || []).push({ id: 9200 + oacRecords[id].length, name, once: true, code });
      oacToday[id] = (oacToday[id] || 0) + 1;
      return { made: true, code, shown: oacShown(code), name, counterSaved: true, problem: null,
        madeToday: oacToday[id], steps: OAC_STEPS, note: OAC_NOTE };
    },
    async diagnose(id) { await wait(500); return { model: find(id).model, hub: find(id).hub, cachedOk: false, liveOk: true, trail: "senddata → ACK", lock: { ...find(id) } }; },
  },
};

roster = [
  access("2d3e", 0, "Alex Owner", "owner", "Always", { hasPin: true, pin: "246810", fingerprints: 1, faces: 1 }),
  access("4d00", 0, "Alex Owner", "owner", "Always", { hasPin: true, pin: "246810" }),
  access("263e", 0, "Alex Owner", "owner", "Always", { hasPin: true, pin: "246810", fingerprints: 2, faces: 1 }),
  access("1e00", 0, "Alex Owner", "owner", "Always", { hasPin: true, pin: "246810", fingerprints: 1 }),
  access("263e", 21, "Dana", "guest", "Mon–Fri 09:00–17:59", { faces: 1 }),
  access("3000", 22, "Dana", "guest", "Every day", { faces: 1, twoStep: true }),
  access("2600", 23, "Dana", "guest", "2026-08-20 00:00 to 2026-08-22 23:59", { hasPin: true, pin: "7710", suspended: true }),
  access("2d3e", 24, "Dana", "guest", "Mon–Fri 09:00–17:59", { hasPin: true, pin: "7710", faces: 1 }),
  access("4d00", 31, "Cleaner", "guest", "Mon–Sat 07:00–20:59", { hasPin: true, pin: "482913" }),
  access("2d3e", 32, "Cleaner", "guest", "Until 2026-10-31 09:00", { hasPin: true, pin: "482913" }),
];

// Scripted starting states for screenshots.
const click = (sel) => document.querySelector(sel)?.click();
setTimeout(() => {
  const door = (label) => click(`.plate[aria-label^="${label}"]`);
  if (scene === "door") door("2A Front");
  if (scene === "access" || scene === "access-touch") { door(scene === "access" ? "2A Front" : "2A Office"); click("#tab-access"); }
  if (scene === "offline" || scene === "offline-many") {
    door("2A Front");
    click("#tab-access");
    setTimeout(() => {
      document.getElementById("oac-form").who.value = "Sam (Saturday)";
      click("#oac-make");
    }, 600);
  }
  if (scene === "schedules") { door("3A"); click("#tab-schedules"); }
  if (scene === "schedules-touch") { door("2A Office"); click("#tab-schedules"); }
  if (scene === "edit") click("#edit-list");
  if (scene === "maint" || scene.startsWith("firmware")) {
    click("#view-maint");
    setTimeout(() => { click(".maint-rows li:nth-child(2) .maint-row"); if (scene !== "maint") setTimeout(() => click("#btn-firmware"), 200); }, 200);
  }
  if (scene === "firmware-install") { setTimeout(() => { click(".fw-install button"); setTimeout(() => click("#confirm-ok"), 300); }, 1400); }
  if (scene === "settings" || scene === "applock") click("#view-settings");
  // Clicking a door connects to it first; Unlock can be held once that's done.
  const whenUnlockable = (fn) => { const b = document.getElementById("btn-unlock"); b.disabled ? setTimeout(() => whenUnlockable(fn), 100) : fn(b); };
  if (scene === "hold") {
    click('.plate[aria-label^="1204"]');
    whenUnlockable((b) => b.dispatchEvent(new PointerEvent("pointerdown", { button: 0, pointerId: 1, bubbles: true })));
  }
  if (scene === "hold-cancel") {
    click('.plate[aria-label^="1204"]');
    whenUnlockable((b) => {
      b.dispatchEvent(new PointerEvent("pointerdown", { button: 0, pointerId: 1, bubbles: true }));
      setTimeout(() => b.dispatchEvent(new PointerEvent("pointerup", { button: 0, pointerId: 1, bubbles: true })), 400);
    });
  }
  if (scene === "preview") {            // the single-schedule form: not tried on a lock yet, so it has Preview
    click('.plate[aria-label^="2A Office"]'); click("#tab-schedules");
    setTimeout(() => click("#w-preview"), 200);
  }
  if (scene === "people") { click("#view-people"); setTimeout(() => click(".person-row:nth-child(1), .people-list li:nth-child(2) .person-row"), 500); }
  if (scene === "person-detail") { click("#view-people"); setTimeout(() => { click(".people-list li:nth-child(2) .person-row"); setTimeout(() => click(".access-table tbody tr:nth-child(4) .row-actions .btn"), 300); }, 500); }
  if (scene === "new-person") { click("#view-people"); setTimeout(() => { click("#add-person"); const f = document.getElementById("grant-form"); f.elements.name.value = "Dana R"; f.querySelector("input[value=weekly]").click(); f.querySelector("input[value=weekly]").dispatchEvent(new Event("change", { bubbles: true })); }, 600); }
  if (scene === "confirm") { click('.plate[aria-label^="1204"]'); click("#check-all"); }
}, 700);
