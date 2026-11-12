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
  route: hub ? "hub" : model === "PGD7Y" ? "bluetooth" : "wifi", favorite: false, ...extra,
});

const locks = [
  mk("4d00", "2A Office", "2A", "Office", "PGD7Y", "PGH200U00000001T", "locked", 82, { doorOpen: false, firmware: "1.02.51" }),
  mk("3200", "3 Server", "3", "Server", "PGD7Y", "PGH200U00000002T", "locked", 64),
  mk("3900", "4C", "4C", "", "PGD7Y", "", "unknown", null),
  mk("4c00", "3B Patio", "3B", "Patio", "PGD7Y", "", "asleep", null),
  mk("2d32", "3A", "3A", "", "PGK728WRHK", "", "unlocked", 47),
  mk("4300", "2C Spare", "2C", "Spare", "PGK728WRHK", "", "unknown", null),
  mk("1e00", "3C Vision", "3C", "Vision", "PGK798HK", "", "locked", 91),
  mk("2d3e", "2A Front", "2A", "Front", "PGK728WRHK", "", "locked", 18, { favorite: true }),
  mk("1c00", "4C New", "4C", "New", "PGK728WRHK", "", "unknown", null),
  mk("3000", "1204", "1204", "", "PGK728WRHK", "", "locked", 73, { favorite: true }),
  mk("2600", "1206", "1206", "", "PGK728WRHK", "", "unknown", null),
  mk("263e", "3C", "3C", "", "PGK728WRHK", "", "unknown", null),
  mk("4500", "2C", "2C", "", "PGK728WRHK", "", "unknown", null),
];
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
const find = (id) => locks.find((l) => l.id === id);

window.pywebview = {
  api: {
    async session() { await wait(200); return scene === "signin" ? { signedIn: false, email: "owner@example.com" } : { signedIn: true, email: "owner@example.com" }; },
    async sign_in(email) { await wait(400); return { signedIn: true, email }; },
    async sign_out() { return { signedIn: false, email: "owner@example.com" }; },
    async locks() { await wait(150); return locks.map((l) => ({ ...l })); },
    async check_all() { await wait(800); return locks.map((l) => ({ ...l, state: l.state === "unknown" ? "asleep" : l.state, checkedAt: Date.now() / 1000 })); },
    async check(id) { await wait(600); return { ...find(id), checkedAt: Date.now() / 1000 }; },
    async lock(id) { await wait(700); Object.assign(find(id), { state: "locked" }); return { ok: true, detail: "", lock: { ...find(id) } }; },
    async unlock(id) { await wait(700); Object.assign(find(id), { state: "unlocked" }); return { ok: true, detail: "", lock: { ...find(id) } }; },
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
  if (scene === "hold") {
    click('.plate[aria-label^="1204"]');
    document.getElementById("btn-unlock").dispatchEvent(new PointerEvent("pointerdown", { button: 0, pointerId: 1, bubbles: true }));
  }
  if (scene === "hold-cancel") {
    click('.plate[aria-label^="1204"]');
    const b = document.getElementById("btn-unlock");
    b.dispatchEvent(new PointerEvent("pointerdown", { button: 0, pointerId: 1, bubbles: true }));
    setTimeout(() => b.dispatchEvent(new PointerEvent("pointerup", { button: 0, pointerId: 1, bubbles: true })), 400);
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
