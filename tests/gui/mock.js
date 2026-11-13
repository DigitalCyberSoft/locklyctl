// Browser stand-in for locklyctl/gui/api.py, for designing the GUI without a
// lock or a Lockly account. Serve the repo root and open
//   http://localhost:8000/locklyctl/gui/web/index.html?mock=/tests/gui/mock.js
// Optional &scene=signin|schedules|guests|dark-detail scripts a starting state.

const scene = new URLSearchParams(location.search).get("scene") || "";
const now = Date.now() / 1000;
const mk = (id, name, unit, rest, model, hub, state, battery, extra = {}) => ({
  id, name, unit, rest, model, hub, state, battery, lowBattery: battery != null && battery < 20,
  doorOpen: null, firmware: "", cached: false, checkedAt: state === "unknown" ? null : now - 240,
  welcomeForm: model === "PGD7Y" ? "46" : "9B", faceCapable: model === "PGK728WRHK",
  route: hub ? "hub" : model === "PGD7Y" ? "bluetooth" : "wifi", eventLog: model === "PGK728WRHK", favorite: false,
  keepUnlockedUntil: null, keepMaxMinutes: 1439, clockKnown: false, clockOffset: null, ...extra,
});

const locks = [
  mk("4d00", "2A Office", "2A", "Office", "PGD7Y", "PGH200U00000001T", "locked", 82, { doorOpen: false, firmware: "1.02.51" }),
  mk("3200", "3 Server", "3", "Server", "PGD7Y", "PGH200U00000002T", "locked", 64),
  mk("3900", "4C", "4C", "", "PGD7Y", "", "unknown", null),
  mk("4c00", "3B Patio", "3B", "Patio", "PGD7Y", "", "asleep", null),
  mk("2d32", "3A", "3A", "", "PGK728WRHK", "", "unlocked", 47, { keepUnlockedUntil: "18:30" }),
  mk("4300", "2C Spare", "2C", "Spare", "PGK728WRHK", "", "unknown", null),
  mk("1e00", "3C Vision", "3C", "Vision", "PGK798HK", "", "locked", 91),
  mk("2d3e", "2A Front", "2A", "Front", "PGK728WRHK", "", "locked", 18, { favorite: true }),
  mk("1c00", "4C New", "4C", "New", "PGK728WRHK", "", "unknown", null, { clockKnown: true, clockOffset: null }),
  mk("3000", "1204", "1204", "", "PGK728WRHK", "", "asleep", null, { favorite: true }),
  mk("2600", "1206", "1206", "", "PGK728WRHK", "", "unknown", null),
  mk("263e", "3C", "3C", "", "PGK728WRHK", "", "unknown", null),
  mk("4500", "2C", "2C", "", "PGK728WRHK", "", "unknown", null),
];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const find = (id) => locks.find((l) => l.id === id);
let fwPolls = 0, fwParts = [];             // the mock firmware install's progress
let connection = "auto";                   // Api.connection's saved setting

window.pywebview = {
  api: {
    async session() { await wait(200); return scene === "signin" ? { signedIn: false, email: "owner@example.com" } : { signedIn: true, email: "owner@example.com" }; },
    async sign_in(email) { await wait(400); return { signedIn: true, email }; },
    async sign_out() { return { signedIn: false, email: "owner@example.com" }; },
    async locks() { await wait(150); return locks.map((l) => ({ ...l })); },
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
    async bluetooth_wifi_scan() { await wait(800); return [{ ssid: "Building Wi-Fi", rssi: -51, routerType: 3 }, { ssid: "Guest", rssi: -62, routerType: 3 }]; },
    async bluetooth_wifi_setup(id, ssid) { await wait(1500); return { ok: true, text: "connected", lock: { ...find(id) } }; },
    async connection() { return { mode: connection, bluetooth: "" }; },
    async set_connection(mode) { await wait(100); connection = mode; return { mode, bluetooth: "" }; },
    async bluetooth_check() { await wait(600); return { available: true, inRange: false, detail: "The lock isn't in Bluetooth range of this computer." }; },
    async bluetooth_set_time(id) { await wait(900); Object.assign(find(id), { clockKnown: true, clockOffset: -1 }); return { ...find(id) }; },
    async bluetooth_chip_install(id) {
      await wait(400); fwPolls = 0;
      fwParts = [{ part: "ble", name: "Bluetooth chip", version: "1.14.28", current: "1.14.24", state: "sending", now: "" }];
      return this.firmware_install_status(id);
    },
    async firmware_install(id, parts) {
      await wait(400); fwPolls = 0;
      fwParts = parts.map((p) => ({ part: p.part, name: p.name, version: p.version, current: p.current, state: "sending", now: "" }));
      return this.firmware_install_status(id);
    },
    async firmware_install_status() {      // sending, restarting, one part in and one not yet, then done
      await wait(150);
      const all = (state, now) => fwParts.map((p) => ({ ...p, state, now: now ? now(p) : "" }));
      const base = { running: true, ok: null, away: null, error: "", versions: {}, clockKnown: false, clockOffset: null };
      const steps = [
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
    async guests() { await wait(200); return [{ id: 7, name: "Cleaner", pin: "482913", start: "2026-10-30 09:00", end: "2026-10-31 09:00" }, { id: 9, name: "Dana", pin: "7710", start: "2026-01-02 10:00", end: "2125-12-09 10:00" }]; },
    async add_guest() { await wait(300); return { ok: true, id: 11 }; },
    async delete_guest() { await wait(300); return { ok: true }; },
    async welcome(id, form, action, days, start, end, send) { await wait(300); return send ? { ok: true, sent: true, detail: "" } : { ok: true, sent: false, form, frame: "A1B2C3D447E91B706D1C93326E3B5BAF4F14CE7A506C7A10E92138DC72D8BE0EBFAD7197C495B84368AF1090C46A2459A5" }; },
    async rain(id, on, send) { await wait(300); return send ? { ok: false, sent: true, detail: "lock rejected the command: 0C — unsupported" } : { ok: true, sent: false, frame: "A1B2C3D4AF3D71357E0C90F7D1C586542CE28A0D735F6C4A" }; },
    async welcome_state() { await wait(400); return { ok: true, sent: true, detail: "Welcome Mode off" }; },
    async rain_state() { await wait(400); return { ok: true, sent: true, detail: "Rain Mode off" }; },
    async people() {
      await wait(300);
      const d = (lockId, doorName, unit, rest, model, role, access, extra = {}) => ({ lockId, doorName, doorUnit: unit, doorRest: rest, doorModel: model,
        userId: 1, role, access, enabled: true, status: "Y", hasPin: false, pin: "", fingerprints: 0, faces: 0, cards: 0, twoStep: false, recordKey: "", ...extra });
      return { failedDoors: [], people: [
        { key: "owner@example.com", name: "Alex Owner", email: "owner@example.com", mergedKeys: [], doors: [
          d("2d3e", "2A Front", "2A", "Front", "PGK728WRHK", "owner", "Always", { hasPin: true, fingerprints: 1, faces: 1 }),
          d("4d00", "2A Office", "2A", "Office", "PGD7Y", "owner", "Always", { hasPin: true }),
          d("263e", "3C", "3C", "", "PGK728WRHK", "owner", "Always", { hasPin: true, fingerprints: 2, faces: 1 }),
          d("1e00", "3C Vision", "3C", "Vision", "PGK798HK", "owner", "Always", { hasPin: true, fingerprints: 1 }) ] },
        { key: "name:dana", name: "Dana", email: "", mergedKeys: ["name:dana r"], doors: [
          d("263e", "3C", "3C", "", "PGK728WRHK", "guest", "Mon–Fri 09:00–17:59", { faces: 1 }),
          d("3000", "1204", "1204", "", "PGK728WRHK", "guest", "Every day", { faces: 1, twoStep: true }),
          d("2600", "1206", "1206", "", "PGK728WRHK", "guest", "2026-08-20 00:00 to 2026-08-22 23:59", { hasPin: true, enabled: false }) ] },
        { key: "name:cleaner", name: "Cleaner", email: "", mergedKeys: [], doors: [
          d("4d00", "2A Office", "2A", "Office", "PGD7Y", "guest", "Mon–Sat 07:00–20:59", { hasPin: true }) ] },
      ] };
    },
    async merge_people() { return this.people(); },
    async door_access_detail() { await wait(250); return { hasPin: true,
      fingerprints: [{ crdntlId: 1, slot: 1, code: 11, name: "Right thumb" }, { crdntlId: 2, slot: 2, code: 12, name: "Left index" }],
      faces: [{ crdntlId: 3, slot: 1, code: 1001, name: "Alex" }], cards: [] }; },
    async change_pin() { await wait(400); return { ok: true }; },
    async remove_from_door() { await wait(400); return { ok: true }; },
    async remove_credential() { await wait(400); return { ok: true }; },
    async add_to_door() { await wait(400); return { ok: true, userId: 40, slot: 7 }; },
    async separate_person() { return this.people(); },
    async diagnose(id) { await wait(500); return { model: find(id).model, hub: find(id).hub, cachedOk: false, liveOk: true, trail: "senddata → ACK", lock: { ...find(id) } }; },
  },
};

// Scripted starting states for screenshots.
const click = (sel) => document.querySelector(sel)?.click();
setTimeout(() => {
  if (scene === "schedules") { click('.plate[aria-label^="4C,"]'); click("#tab-schedules"); }
  if (scene === "guests") { click('.plate[aria-label^="2A Office"]'); click("#tab-guests"); }
  if (scene === "dark-detail") { click('.plate[aria-label^="3A"]'); }
  if (scene.startsWith("firmware")) { click('.plate[aria-label^="2A Front"]'); click("#tab-connection"); setTimeout(() => click("#btn-firmware"), 200); }
  if (scene === "firmware-install") { setTimeout(() => { click(".fw-install button"); setTimeout(() => click("#confirm-ok"), 300); }, 1200); }
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
  if (scene === "preview") {
    click('.plate[aria-label^="4C,"]'); click("#tab-schedules");
    setTimeout(() => {
      const sel = document.querySelector("#w-form-row select");
      sel.value = "46"; sel.dispatchEvent(new Event("change"));
      document.querySelector('#welcome-form button[value="preview"]').click();
    }, 200);
  }
  if (scene === "people") { click("#view-people"); setTimeout(() => click(".person-row:nth-child(1), .people-list li:nth-child(2) .person-row"), 500); }
  if (scene === "person-detail") { click("#view-people"); setTimeout(() => { click(".people-list li:nth-child(2) .person-row"); setTimeout(() => click(".row-actions .btn"), 300); }, 500); }
  if (scene === "new-person") { click("#view-people"); setTimeout(() => { click("#add-person"); const f = document.getElementById("grant-form"); f.elements.name.value = "Dana R"; f.querySelector("input[value=weekly]").click(); f.querySelector("input[value=weekly]").dispatchEvent(new Event("change", { bubbles: true })); }, 600); }
  if (scene === "confirm") { click('.plate[aria-label^="1204"]'); click("#check-all"); }
}, 700);
