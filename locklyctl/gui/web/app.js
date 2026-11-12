"use strict";
// locklyctl GUI. Talks to locklyctl/gui/api.py through window.pywebview.api.
// No framework, no build step. Text from the cloud (lock and guest names) is
// only ever set via textContent, never parsed as HTML.

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// An icon from the sprite at the top of index.html; decorative, so screen readers skip it.
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "i");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

const STATE_WORD = { locked: "Locked", unlocked: "Unlocked", asleep: "Asleep", unknown: "Not checked", bluetooth: "Bluetooth only" };
const ROUTE_TEXT = { hub: "through its hub", wifi: "over its own Wi-Fi", bluetooth: "only over Bluetooth" };
const STATE_ICON = { locked: "locked", unlocked: "unlocked", asleep: "asleep", bluetooth: "bluetooth" };
const stateIcon = (st) => (STATE_ICON[st] ? icon(STATE_ICON[st]) : null);
const stateOf = (v) => (v.route === "bluetooth" ? "bluetooth" : v.state);
const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];

let api = null;
let locks = [];
let currentId = null;
let activeTab = "activity";
let toastTimer = null;

// ── plumbing ───────────────────────────────────────────────────────────

async function connect() {
  if (window.pywebview && window.pywebview.api) return window.pywebview.api;
  const mock = new URLSearchParams(location.search).get("mock");   // browser dev only
  if (mock) { await import(mock); return window.pywebview.api; }
  // Phone apps and --browser: locklyctl/gui/server.py, authorised by a
  // per-launch token passed in the URL fragment.
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  if (token) return httpBridge(token);
  return new Promise((resolve) =>
    window.addEventListener("pywebviewready", () => resolve(window.pywebview.api), { once: true }));
}

function httpBridge(token) {
  return new Proxy({}, {
    // "then" must stay undefined, or awaiting the proxy would call the server.
    get: (_, name) => (name === "then" || typeof name === "symbol") ? undefined : async (...args) => {
      let res;
      try {
        res = await fetch(`/api/${name}`, {
          method: "POST", body: JSON.stringify(args),
          headers: { "Content-Type": "application/json", "X-Locklyctl-Token": token },
        });
      } catch {
        throw new Error("locklyctl stopped running. Restart the app.");
      }
      const out = await res.json().catch(() => ({ error: `locklyctl answered ${res.status}` }));
      if ("error" in out) throw new Error(out.error);
      return out.result;
    },
  });
}

async function call(name, ...args) {
  try {
    return await api[name](...args);
  } catch (e) {
    throw new Error((e && e.message) || String(e));
  }
}

function show(screen) {
  for (const id of ["boot", "signin", "app"]) $(id).hidden = id !== screen;
}

function toast(text, bad = false) {
  const t = $("toast");
  t.textContent = text;
  t.classList.toggle("bad", bad);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 7000 : 4000);
}

function confirmDialog(title, body, okLabel) {
  const d = $("confirm");
  $("confirm-title").textContent = title;
  $("confirm-body").textContent = body;
  $("confirm-ok").textContent = okLabel;
  d.returnValue = "";
  d.showModal();
  return new Promise((resolve) =>
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true }));
}

async function busy(buttons, fn) {
  const list = [].concat(buttons).filter(Boolean);
  list.forEach((b) => (b.disabled = true));
  try { return await fn(); } finally { list.forEach((b) => (b.disabled = false)); }
}

const current = () => locks.find((l) => l.id === currentId) || null;
const doorLabel = (v) => (v.unit ? `${v.unit}${v.rest ? " " + v.rest : ""}` : v.name);

function updateLock(view) {
  const i = locks.findIndex((l) => l.id === view.id);
  if (i >= 0) locks[i] = view; else locks.push(view);
  renderPlates();
  renderFavs();
  if (view.id === currentId) renderDoor();
}

function sinceText(ts) {
  if (!ts) return null;
  const mins = Math.round((Date.now() / 1000 - ts) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// ── sign in ────────────────────────────────────────────────────────────

async function boot() {
  api = await connect();
  let s;
  try {
    s = await call("session");
  } catch (e) {
    s = { signedIn: false, message: e.message };
  }
  if (s.signedIn) return enterApp(s.email);
  showSignin(s.email, s.message);
}

function showSignin(email, message) {
  const f = $("signin-form");
  if (email) f.email.value = email;
  $("signin-error").textContent = message || "";
  show("signin");
  (email ? f.password : f.email).focus();
}

$("signin-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  $("signin-error").textContent = "";
  await busy(f.querySelector("button"), async () => {
    try {
      const s = await call("sign_in", f.email.value, f.password.value, f.remember.checked);
      f.password.value = "";
      await enterApp(s.email);
    } catch (e) {
      $("signin-error").textContent = e.message;
    }
  });
});

async function enterApp(email) {
  $("who").textContent = email;
  show("app");
  $("plates").replaceChildren(h("li", { class: "muted" }, "Loading doors…"));
  try {
    locks = await call("locks");
  } catch (e) {
    $("plates").replaceChildren(h("li", { class: "form-error" }, e.message));
    return;
  }
  locks.sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
  renderFavs();
  if (!locks.length) {
    $("plates").replaceChildren(h("li", { class: "muted" }, "No locks on this account. Add them in the Lockly app first."));
    return;
  }
  renderPlates();
  selectLock(current() ? currentId : locks[0].id);
}

$("sign-out").addEventListener("click", async () => {
  const ok = await confirmDialog("Sign out?",
    "This also forgets the saved sign-in, so you'll need your Lockly password next time.", "Sign out");
  if (!ok) return;
  const s = await call("sign_out");
  locks = []; currentId = null;
  renderFavs();
  showSignin(s.email);
});

$("check-all").addEventListener("click", async (ev) => {
  const ok = await confirmDialog(`Check all ${locks.length} doors?`,
    "Each lock wakes up to answer, and beeps. Locks that are asleep may not answer.", "Check all doors");
  if (!ok) return;
  await busy(ev.currentTarget, async () => {
    toast(`Checking ${locks.length} doors…`);
    try {
      const fresh = await call("check_all");
      fresh.forEach(updateLock);
      const answered = fresh.filter((l) => l.state === "locked" || l.state === "unlocked").length;
      toast(`${answered} of ${fresh.length} doors answered.`);
    } catch (e) { toast(e.message, true); }
  });
});

// ── plates ─────────────────────────────────────────────────────────────

function renderPlates() {
  const items = locks.map((v) => {
    const num = v.unit || v.name;
    const batt = v.battery != null ? `${v.battery}%` : v.lowBattery ? "Low battery" : null;
    return h("li", {},
      h("button", {
        class: "plate", type: "button", "data-state": stateOf(v),
        "aria-current": v.id === currentId ? "true" : "false",
        "aria-label": `${doorLabel(v)}, ${STATE_WORD[stateOf(v)]}${v.favorite ? ", favorite" : ""}`,
        onclick: () => selectLock(v.id),
      },
        h("span", { class: "plate-num" + (v.unit ? "" : " wordy") }, num),
        h("span", { class: "plate-rest" }, v.unit ? v.rest : ""),
        h("span", { class: "plate-state" },
          h("span", { class: "plate-word" }, stateIcon(stateOf(v)), STATE_WORD[stateOf(v)]),
          batt && h("span", { class: "plate-batt" + (v.lowBattery || (v.battery != null && v.battery <= 20) ? " low" : "") }, batt)),
        v.favorite && h("span", { class: "plate-star", "aria-hidden": "true" }, icon("favorite-on"))));
  });
  $("plates").replaceChildren(...items);
  renderRetry();
}

// Doors that didn't answer and can be asked again from here.
const asleepDoors = () => locks.filter((v) => v.state === "asleep" && v.route !== "bluetooth");

function renderRetry() {
  const n = asleepDoors().length;
  $("retry-asleep").hidden = !n || !$("people-ws").hidden;
  $("retry-asleep").replaceChildren(icon("check"), `Retry ${n} asleep`);
}

$("retry-asleep").addEventListener("click", async (ev) => {
  const ids = asleepDoors().map((v) => v.id);
  if (!ids.length) return;
  await busy(ev.currentTarget, async () => {
    toast(`Asking ${ids.length} ${ids.length === 1 ? "door" : "doors"} again…`);
    try {
      const fresh = await call("check_doors", ids);
      fresh.forEach(updateLock);
      const answered = fresh.filter((l) => l.state === "locked" || l.state === "unlocked").length;
      toast(`${answered} of ${fresh.length} answered.`, answered === 0);
    } catch (e) { toast(e.message, true); }
  });
});

// ── favorites: one tap to lock or unlock, without opening the door ─────

const pending = new Map();    // lock id → "lock" | "unlock" | "check" while it's out
const DOING = { lock: "Locking…", unlock: "Unlocking…", check: "Checking…", keep: "Setting it to stay unlocked…",
  firmware: "Updating firmware…" };

function renderFavs() {
  const favs = locks.filter((v) => v.favorite);
  $("favs").hidden = !favs.length;
  $("fav-list").replaceChildren(...favs.map((v) => {
    const st = stateOf(v);
    const off = pending.has(v.id) || v.route === "bluetooth";
    return h("li", { class: "fav", "data-state": st },
      h("button", {
        class: "fav-door", type: "button", "aria-current": v.id === currentId ? "true" : "false",
        onclick: () => selectLock(v.id),
      },
        h("span", { class: "fav-name" }, doorLabel(v)),
        h("span", { class: "fav-state" }, DOING[pending.get(v.id)] || STATE_WORD[st])),
      h("span", { class: "fav-actions" },
        st === "asleep" && h("button", { class: "btn btn-quiet icon-only", type: "button", disabled: off,
          "aria-label": `Check ${doorLabel(v)} again`, title: "Ask it again", onclick: () => runCommand(v, "check") },
          icon("check")),
        h("button", { class: "btn btn-ink", type: "button", disabled: off, "aria-label": `Lock ${doorLabel(v)}`,
          onclick: () => runCommand(v, "lock") }, icon("locked"), "Lock"),
        h("button", { class: "btn btn-open", type: "button", disabled: off, "aria-label": `Unlock ${doorLabel(v)}`,
          onclick: () => runCommand(v, "unlock") }, icon("unlocked"), "Unlock")));
  }));
}

// Lock, unlock or check one door. Its buttons (here and in the door panel)
// stay disabled until the lock has answered.
async function runCommand(v, kind) {
  if (pending.has(v.id)) return;
  const label = doorLabel(v);
  pending.set(v.id, kind);
  refreshControls(v.id);
  try {
    if (kind === "check") {
      toast(`Waking ${label}…`);
      const view = await call("check", v.id);
      updateLock(view);
      toast(view.state === "asleep" ? `${label} didn't answer.` : `${label}: ${STATE_WORD[view.state]}.`,
        view.state === "asleep");
    } else {
      toast(`${kind === "lock" ? "Locking" : "Unlocking"} ${label}…`);
      const r = await call(kind, v.id);
      updateLock(r.lock);
      toast(r.ok ? `${label} ${kind}ed.` : `${label} didn't ${kind}: ${r.detail}`, !r.ok);
    }
  } catch (e) { toast(e.message, true); }
  finally {
    pending.delete(v.id);
    refreshControls(v.id);
  }
}

function refreshControls(id) {
  renderFavs();
  if (id === currentId) renderDoor();
}

function selectLock(id) {
  const changed = id !== currentId;
  currentId = id;
  renderPlates();
  renderFavs();
  renderDoor();
  if (changed) {
    resetSchedules();
    loadTab(activeTab);
  }
}

// ── the door panel ─────────────────────────────────────────────────────

function renderDoor() {
  const v = current();
  $("door-empty").hidden = !!v;
  $("door-body").hidden = !v;
  if (!v) return;
  $("d-unit").textContent = v.unit;
  $("d-name").textContent = v.unit ? (v.rest || `Door ${v.unit}`) : v.name;
  $("d-model").replaceChildren([v.model, v.hub && `hub ${v.hub}`].filter(Boolean).join(", ") + (v.route ? ". " : ""),
    v.route ? h("span", { class: "route" }, icon(v.route), `Reached ${ROUTE_TEXT[v.route]}.`) : null);
  const noRoute = v.route === "bluetooth";
  for (const id of ["btn-lock", "btn-unlock", "btn-check"]) $(id).disabled = noRoute || pending.has(v.id);
  const fav = $("btn-fav");
  fav.setAttribute("aria-pressed", String(!!v.favorite));
  fav.replaceChildren(h("span", { class: "star" }, icon(v.favorite ? "favorite-on" : "favorite")), "Favorite");
  fav.disabled = noRoute && !v.favorite;     // a door with no hub can't be opened from a favorite
  fav.title = noRoute && !v.favorite ? "This door has no hub, so it can only be opened over Bluetooth." : "";

  const st = $("d-state");
  st.dataset.state = v.state;
  st.replaceChildren(stateIcon(stateOf(v)), v.route === "bluetooth" ? "No hub — only reachable over Bluetooth"
    : v.state === "asleep" ? "Didn't answer (asleep or offline)"
      : v.state === "unknown" ? "Status not checked yet" : STATE_WORD[v.state]);
  st.dataset.state = v.route === "bluetooth" ? "unknown" : v.state;

  const facts = [];
  if (v.battery != null) facts.push(h("li", { class: v.battery <= 20 ? "warn" : "" }, icon("battery"), `Battery ${v.battery}%`));
  else if (v.lowBattery) facts.push(h("li", { class: "warn" }, icon("battery"), "Battery low"));
  if (v.doorOpen != null) facts.push(h("li", { class: v.doorOpen ? "warn" : "" }, v.doorOpen ? "Door open" : "Door closed"));
  const since = sinceText(v.checkedAt);
  if (since) facts.push(h("li", {}, v.cached ? `From the hub, ${since}` : `Checked ${since}`));
  if (v.firmware) facts.push(h("li", {}, `Firmware ${v.firmware}`));
  $("d-facts").replaceChildren(...facts);
  renderKeep(v);
}

// ── keep unlocked for a while: one-time Welcome Mode slots the lock runs ─

const KEEP_PRESETS = [[60, "1 h"], [120, "2 h"], [240, "4 h"], [480, "8 h"], [720, "12 h"]];
const hoursText = (min) => `${min / 60} ${min === 60 ? "hour" : "hours"}`;

function renderKeep(v) {
  const box = $("keep");
  box.hidden = v.welcomeForm !== "9B" || v.route === "bluetooth";
  if (box.hidden) return;
  const off = pending.has(v.id);
  const tooLate = "That would run past midnight at the lock, which it can't do.";
  const chips = KEEP_PRESETS.map(([min, label]) => h("button", {
    class: "btn btn-quiet", type: "button", disabled: off || min > v.keepMaxMinutes,
    title: min > v.keepMaxMinutes ? tooLate : null, onclick: () => keepUnlocked(v, min),
  }, label));
  const hours = h("input", { class: "keep-hours", type: "number", min: "1", max: "23", step: "1", value: "3",
    inputmode: "numeric", "aria-label": "Hours to keep it unlocked" });
  const custom = h("span", { class: "keep-custom" }, hours, h("span", {}, "h"),
    h("button", { class: "btn btn-quiet", type: "button", disabled: off, onclick: () => {
      const n = Number(hours.value);
      if (!Number.isInteger(n) || n < 1 || n > 23) { toast("Choose 1 to 23 hours.", true); return; }
      if (n * 60 > v.keepMaxMinutes) { toast(tooLate, true); return; }
      keepUnlocked(v, n * 60);
    } }, "Keep unlocked"));
  $("keep-chips").replaceChildren(...chips, custom);
  $("keep-note").replaceChildren(...(v.keepUnlockedUntil
    ? [`Kept unlocked until ${v.keepUnlockedUntil}, lock time. `,
       h("button", { class: "btn btn-quiet", type: "button", disabled: off, onclick: () => endKeep(v) },
         "End now and lock")]
    : ["The lock runs it itself, so it works with this app closed. "
       + "The door's other Welcome Mode schedules stay as they are."]));
}

async function keepUnlocked(v, minutes) {
  const ok = await confirmDialog(`Keep ${doorLabel(v)} unlocked for ${hoursText(minutes)}?`,
    "It opens within about 20 seconds and locks again by itself when the time is up, even with this app closed.",
    "Keep unlocked");
  if (!ok || pending.has(v.id)) return;
  pending.set(v.id, "keep");
  refreshControls(v.id);
  toast(`Setting ${doorLabel(v)} to stay unlocked…`);
  try {
    const r = await call("keep_unlocked", v.id, minutes);
    updateLock(r.lock);
    toast(`${doorLabel(v)} stays unlocked until ${r.until}.`);
  } catch (e) { toast(e.message, true); }
  finally { pending.delete(v.id); refreshControls(v.id); }
}

async function endKeep(v) {
  if (pending.has(v.id)) return;
  pending.set(v.id, "lock");
  refreshControls(v.id);
  toast(`Ending it and locking ${doorLabel(v)}…`);
  try {
    const r = await call("end_keep_unlocked", v.id);
    updateLock(r.lock);
    toast(r.ok ? `${doorLabel(v)} locked.` : `${doorLabel(v)} didn't lock: ${r.detail}`, !r.ok);
  } catch (e) { toast(e.message, true); }
  finally { pending.delete(v.id); refreshControls(v.id); }
}

$("btn-lock").addEventListener("click", () => { const v = current(); if (v) runCommand(v, "lock"); });
$("btn-check").addEventListener("click", () => { const v = current(); if (v) runCommand(v, "check"); });

$("btn-fav").addEventListener("click", async (ev) => {
  const v = current();
  if (!v) return;
  ev.currentTarget.disabled = true;
  try {
    const view = await call("set_favorite", v.id, !v.favorite);
    updateLock(view);
    toast(view.favorite ? `${doorLabel(v)} is in your favorites.` : `${doorLabel(v)} removed from favorites.`);
  } catch (e) { toast(e.message, true); }
  finally { renderDoor(); }
});

// Hold to unlock: nothing is sent unless the button is held for the full fill.
(() => {
  const btn = $("btn-unlock");
  const HOLD_MS = 900;
  let timer = null;

  const start = () => {
    if (btn.disabled || timer || !current()) return;
    btn.classList.remove("done");
    // force a style flush so the fill animates from empty each time
    void btn.offsetWidth;
    btn.classList.add("holding");
    timer = setTimeout(fire, HOLD_MS);
  };
  const cancel = () => {
    if (!timer) return;
    clearTimeout(timer);
    timer = null;
    btn.classList.remove("holding");
  };
  const fire = async () => {
    timer = null;
    btn.classList.remove("holding");
    btn.classList.add("done");
    const v = current();
    if (v) await runCommand(v, "unlock");
    btn.classList.remove("done");
  };

  btn.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    try { btn.setPointerCapture(e.pointerId); } catch { /* synthetic or inactive pointer */ }
    start();
  });
  btn.addEventListener("pointerup", cancel);
  btn.addEventListener("pointercancel", cancel);
  btn.addEventListener("lostpointercapture", cancel);
  btn.addEventListener("keydown", (e) => {
    if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); start(); }
  });
  btn.addEventListener("keyup", (e) => { if (e.key === " " || e.key === "Enter") cancel(); });
  btn.addEventListener("blur", cancel);
  btn.addEventListener("click", (e) => e.preventDefault());
})();

// ── tabs ───────────────────────────────────────────────────────────────

const TABS = ["activity", "guests", "schedules", "connection"];

function selectTab(name, focus = false) {
  activeTab = name;
  for (const t of TABS) {
    const tab = $(`tab-${t}`);
    const on = t === name;
    tab.setAttribute("aria-selected", on ? "true" : "false");
    tab.tabIndex = on ? 0 : -1;
    $(`panel-${t}`).hidden = !on;
    if (on && focus) tab.focus();
  }
  loadTab(name);
}

TABS.forEach((t, i) => {
  const tab = $(`tab-${t}`);
  tab.addEventListener("click", () => selectTab(t));
  tab.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowRight" ? 1 : e.key === "ArrowLeft" ? -1 : 0;
    if (step) { e.preventDefault(); selectTab(TABS[(i + step + TABS.length) % TABS.length], true); }
  });
});

function loadTab(name) {
  if (!current()) return;
  if (name === "activity") loadActivity();
  else if (name === "guests") loadGuests();
  else if (name === "schedules") setupSchedules();
  else if (name === "connection") { $("diag-out").replaceChildren(); $("fw-out").replaceChildren(); }
}

async function loadActivity() {
  const id = currentId;
  const panel = $("panel-activity");
  panel.replaceChildren(h("p", { class: "muted" }, "Loading the last 7 days…"));
  try {
    const events = await call("activity", id);
    if (id !== currentId) return;
    if (!events.length) {
      panel.replaceChildren(h("p", { class: "muted" }, "Nobody used this door in the last 7 days."));
      return;
    }
    panel.replaceChildren(h("table", { class: "log" },
      h("tbody", {}, events.slice(0, 200).map((ev) =>
        h("tr", {}, h("td", {}, ev.time), h("td", {}, ev.what), h("td", {}, ev.who))))));
  } catch (e) {
    if (id === currentId) panel.replaceChildren(h("p", { class: "form-error" }, e.message));
  }
}

// ── guests ─────────────────────────────────────────────────────────────

async function loadGuests() {
  const id = currentId;
  const list = $("guest-list");
  list.replaceChildren(h("p", { class: "muted" }, "Loading guest PINs…"));
  try {
    const guests = await call("guests", id);
    if (id !== currentId) return;
    if (!guests.length) {
      list.replaceChildren(h("p", { class: "muted" }, "No guest PINs on this door."));
      return;
    }
    const farFuture = new Date().getFullYear() + 50;
    list.replaceChildren(...guests.map((g) => {
      const permanent = !g.end || Number(g.end.slice(0, 4)) > farFuture;
      return h("div", { class: "guest" },
        h("div", {},
          h("p", { class: "guest-name" }, g.name || "Unnamed"),
          h("p", { class: "guest-meta" }, `PIN ${g.pin || "hidden"}`,
            permanent ? ", no end date" : `, until ${g.end}`)),
        h("button", { class: "btn btn-quiet", type: "button", onclick: (ev) => deleteGuest(g, ev.currentTarget) },
          "Delete"));
    }));
  } catch (e) {
    if (id === currentId) list.replaceChildren(h("p", { class: "form-error" }, e.message));
  }
}

async function deleteGuest(g, btn) {
  const ok = await confirmDialog(`Delete ${g.name || "this guest"}'s PIN?`,
    "The PIN stops working on this door.", "Delete PIN");
  if (!ok) return;
  await busy(btn, async () => {
    try {
      await call("delete_guest", currentId, g.id);
      toast("Guest PIN deleted.");
      loadGuests();
    } catch (e) { toast(e.message, true); }
  });
}

$("guest-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  $("guest-error").textContent = "";
  await busy(f.querySelector("button[type=submit]"), async () => {
    try {
      await call("add_guest", currentId, f.elements.name.value, f.pin.value, f.duration.value);
      toast(`Guest PIN added for ${f.elements.name.value.trim()}.`);
      f.reset();
      loadGuests();
    } catch (e) { $("guest-error").textContent = e.message; }
  });
});

// ── schedules (experimental) ───────────────────────────────────────────

(() => {
  const box = $("w-days");
  DAYS.forEach(([key, label], i) => box.append(
    h("label", {}, h("input", { type: "checkbox", name: "day", value: key, checked: i < 5 }), h("span", {}, label))));
})();

function welcomeForm() {
  const v = current();
  if (v && v.welcomeForm) return v.welcomeForm;
  const sel = document.querySelector("#w-form-row select");
  return sel ? sel.value || null : null;
}

function resetSchedules() {
  for (const id of ["welcome-out", "rain-out"]) $(id).hidden = true;
  for (const id of ["welcome-error", "rain-error"]) $(id).textContent = "";
  $("w-form-row").replaceChildren();
}

function setupSchedules() {
  const v = current();
  const row = $("w-form-row");
  if (!row.childElementCount) {
    if (v.welcomeForm) {
      row.append(h("p", { class: "muted" }, v.welcomeForm === "9B"
        ? `${v.model || "This lock"} uses the 8-slot schedule (command 9B).`
        : `${v.model || "This lock"} uses the single schedule (command 46).`));
    } else {
      row.append(h("label", { class: "field" },
        h("span", {}, `Schedule type for ${v.model || "this lock"} (not known for this model)`),
        h("select", { name: "form", onchange: syncWelcome },
          h("option", { value: "" }, "Choose…"),
          h("option", { value: "9B" }, "8-slot schedule (Visage and newer models)"),
          h("option", { value: "46" }, "Single schedule (Touch and older models)"))));
    }
  }
  syncWelcome();
}

function syncWelcome() {
  const f = $("welcome-form");
  const form = welcomeForm();
  const always = $("w-always-opt");
  always.hidden = form !== "46";
  if (always.hidden && f.elements.action.value === "always") f.querySelector("input[value=set]").checked = true;
  $("w-hours").hidden = f.elements.action.value !== "set";
  $("w-slot-note").textContent = form === "9B"
    ? "Sending replaces every schedule slot on the lock with this one." : "";
}
$("w-action").addEventListener("change", syncWelcome);

function showOutput(el, r) {
  el.hidden = false;
  el.classList.toggle("bad", r.sent && !r.ok);
  el.textContent = !r.sent
    ? `Preview only. Nothing was sent.\n\n${r.frame}`
    : r.ok ? `The lock answered: ${r.detail}`
      : `The lock didn't accept it.\n${r.detail}`;
}

async function readFromLock(btn, out, errEl, fn) {
  errEl.textContent = "";
  out.hidden = false;
  out.textContent = "Asking the lock… (it wakes and beeps)";
  await busy(btn, async () => {
    try { showOutput(out, await fn()); }
    catch (e) { out.hidden = true; errEl.textContent = e.message; }
  });
}

$("w-read").addEventListener("click", (ev) => {
  const form = welcomeForm();
  if (!form) { $("welcome-error").textContent = "Choose the schedule type first."; return; }
  readFromLock(ev.currentTarget, $("welcome-out"), $("welcome-error"),
    () => call("welcome_state", currentId, form));
});

$("r-read").addEventListener("click", (ev) =>
  readFromLock(ev.currentTarget, $("rain-out"), $("rain-error"), () => call("rain_state", currentId)));

$("welcome-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const send = ev.submitter && ev.submitter.value === "send";
  const v = current();
  const form = welcomeForm();
  const action = f.elements.action.value;
  const days = [...f.querySelectorAll("input[name=day]:checked")].map((d) => d.value);
  $("welcome-error").textContent = "";
  if (!form) { $("welcome-error").textContent = "Choose the schedule type first."; return; }
  if (action === "set" && !days.length) { $("welcome-error").textContent = "Pick at least one day."; return; }
  if (send) {
    const what = action === "off" ? "turn Welcome Mode off"
      : action === "always" ? "keep the door unlocked around the clock"
        : `keep the door unlocked ${f.start.value}–${f.end.value}`;
    const ok = await confirmDialog(`Send to ${doorLabel(v)}?`,
      `This asks the lock to ${what}.` + (form === "46" ? " This form hasn't been tried on a real lock yet." : ""), "Send to lock");
    if (!ok) return;
  }
  await busy([...f.querySelectorAll("button")], async () => {
    try {
      const r = await call("welcome", v.id, form, action, days, f.start.value, f.end.value, send);
      showOutput($("welcome-out"), r);
    } catch (e) { $("welcome-error").textContent = e.message; }
  });
});

$("rain-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const send = ev.submitter && ev.submitter.value === "send";
  const v = current();
  const on = f.on.value === "1";
  $("rain-error").textContent = "";
  if (send) {
    const ok = await confirmDialog(`Send to ${doorLabel(v)}?`,
      `This turns Rain Mode ${on ? "on" : "off"}.`, "Send to lock");
    if (!ok) return;
  }
  await busy([...f.querySelectorAll("button")], async () => {
    try {
      showOutput($("rain-out"), await call("rain", v.id, on, send));
    } catch (e) { $("rain-error").textContent = e.message; }
  });
});

// ── connection ─────────────────────────────────────────────────────────

$("btn-diagnose").addEventListener("click", async (ev) => {
  const v = current();
  const out = $("diag-out");
  out.replaceChildren(h("p", { class: "muted" }, "Testing… this can take up to a minute."));
  await busy(ev.currentTarget, async () => {
    try {
      const r = await call("diagnose", v.id);
      updateLock(r.lock);
      const yes = (ok, good, badText) => h("span", { class: ok ? "ok" : "bad" }, ok ? good : badText);
      out.replaceChildren(h("dl", { class: "diag" },
        h("dt", {}, "Hub"), h("dd", {}, r.hub || "None (connects on its own)"),
        r.hub && h("dt", {}, "Hub status cache"),
        r.hub && h("dd", {}, yes(r.cachedOk, "Answering", "Not available (older hub firmware, or hub offline)")),
        h("dt", {}, "Commands"), h("dd", {}, yes(r.liveOk, "Reach the lock", "Don't reach the lock")),
        h("dt", {}, "Route tried"), h("dd", {}, r.trail)));
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

// ── firmware ───────────────────────────────────────────────────────────

$("btn-firmware").addEventListener("click", async (ev) => {
  const v = current();
  const out = $("fw-out");
  out.replaceChildren(h("p", { class: "muted" }, "Asking Lockly, and the lock for its versions…"));
  await busy(ev.currentTarget, async () => {
    try {
      const r = await call("firmware", v.id);
      if (r.lock.id === currentId) updateLock(r.lock);
      if (!r.release) {
        out.replaceChildren(h("p", { class: "ok" },
          "Up to date" + (r.lockFirmware ? `: firmware ${r.lockFirmware}.` : ".")));
        return;
      }
      const rows = r.parts.map((p) => h("tr", {},
        h("td", {}, p.name),
        h("td", {}, p.current || h("span", { class: "muted" }, "not reported")),
        h("td", { class: p.newer ? "bad" : "" }, p.offered, p.newer ? " (newer)" : ""),
        h("td", { class: "muted" }, p.byPhone ? "Lockly app, at the door" : "Over Wi-Fi")));
      const install = r.install;
      const byPhone = r.parts.filter((p) => p.newer && p.byPhone).map((p) => p.name);
      out.replaceChildren(
        h("p", {}, `Lockly offers ${r.kind === "multi" ? "release " : "firmware "}${r.release}`
          + (r.answered ? "." : ". The lock didn't answer, so its own versions are missing.")),
        h("table", { class: "log fw-table" },
          h("thead", {}, h("tr", {}, h("th", {}, "Part"), h("th", {}, "On the lock"), h("th", {}, "Newest"),
            h("th", {}, "Installed by"))),
          h("tbody", {}, rows)),
        r.notes ? h("p", { class: "fine fw-notes" }, r.notes) : null,
        install.parts.length && !install.blocked
          ? h("div", { class: "fw-install" },
            h("p", {}, "Over Wi-Fi this sends " + install.parts.map((p) => `${p.name} ${p.version}`).join(", ") + "."),
            h("button", { class: "btn btn-ink", type: "button", onclick: () => installFirmware(v, install) },
              icon("firmware"), "Install over Wi-Fi"))
          : (r.kind === "multi" && install.blocked ? h("p", { class: "fine" }, install.blocked) : null),
        byPhone.length ? h("p", { class: "fine" }, `${byPhone.join(", ")}: install with the Lockly app, at the door.`)
          : null);
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

async function installFirmware(v, install) {
  const list = install.parts.map((p) => `${p.name} ${p.current || "?"} → ${p.version}`).join(", ");
  const ok = await confirmDialog(`Install new firmware on ${doorLabel(v)}?`,
    `${list}. The lock downloads the files and installs them itself, then restarts. Leave the door alone `
    + "until this says it's done; it can take several minutes.", "Install");
  if (!ok || pending.has(v.id)) return;
  pending.set(v.id, "firmware");
  refreshControls(v.id);
  const show = (st) => {
    if (v.id !== currentId || activeTab !== "connection") return;
    $("fw-out").replaceChildren(
      h("p", {}, `${doorLabel(v)}: ${st.parts.map((p) => `${p.name} ${p.version}`).join(", ")}`),
      h("p", { class: st.ok === false ? "form-error" : (st.ok ? "ok" : "muted") },
        st.ok === false ? st.error : (st.ok ? "Installed, and the lock reports the new versions." : st.text)));
  };
  try {
    let st = await call("firmware_install", v.id, install.parts);
    while (st.running) {
      show(st);
      await new Promise((resolve) => setTimeout(resolve, 2000));
      st = await call("firmware_install_status", v.id);
    }
    show(st);
    toast(st.ok ? `${doorLabel(v)}: firmware installed.` : `${doorLabel(v)}: ${st.error}`, !st.ok);
  } catch (e) {
    if (v.id === currentId) $("fw-out").replaceChildren(h("p", { class: "form-error" }, e.message));
    toast(e.message, true);
  } finally {
    pending.delete(v.id);
    refreshControls(v.id);
  }
}

// ── people ─────────────────────────────────────────────────────────────

let people = [];
let personKey = null;
let peopleLoaded = false;

function showView(view) {
  const isPeople = view === "people";
  $("doors-ws").hidden = isPeople;
  $("people-ws").hidden = !isPeople;
  $("view-doors").setAttribute("aria-pressed", String(!isPeople));
  $("view-people").setAttribute("aria-pressed", String(isPeople));
  $("check-all").hidden = isPeople;
  renderRetry();
  if (isPeople && !peopleLoaded) loadPeople();
}
$("view-doors").addEventListener("click", () => showView("doors"));
$("view-people").addEventListener("click", () => showView("people"));

async function loadPeople() {
  $("people-list").replaceChildren(h("li", { class: "muted" }, "Loading people from every door…"));
  try {
    applyPeople(await call("people"));
    peopleLoaded = true;
  } catch (e) {
    $("people-list").replaceChildren(h("li", { class: "form-error" }, e.message));
  }
}

function applyPeople(r) {
  people = r.people;
  if (r.failedDoors && r.failedDoors.length) toast(`Couldn't load people for: ${r.failedDoors.join(", ")}`, true);
  if (!people.find((p) => p.key === personKey)) personKey = null;
  renderPeople();
  renderPerson();
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

function credSummary(d) {
  const parts = [];
  if (d.hasPin) parts.push("PIN");
  if (d.fingerprints) parts.push(plural(d.fingerprints, "fingerprint", "fingerprints"));
  if (d.faces) parts.push(plural(d.faces, "face", "faces"));
  if (d.cards) parts.push(plural(d.cards, "card", "cards"));
  return parts;
}

function totals(p) {
  const t = { pin: 0, fingerprints: 0, faces: 0, cards: 0 };
  for (const d of p.doors) {
    if (d.hasPin) t.pin++;
    t.fingerprints += d.fingerprints; t.faces += d.faces; t.cards += d.cards;
  }
  return t;
}

function renderPeople() {
  const q = $("people-search").value.trim().toLowerCase();
  const shown = people.filter((p) => !q || p.name.toLowerCase().includes(q) || (p.email || "").toLowerCase().includes(q)
    || p.doors.some((d) => d.doorName.toLowerCase().includes(q)));
  if (!shown.length) {
    $("people-list").replaceChildren(h("li", { class: "muted" }, q ? "Nobody matches that." : "Nobody has access to these doors yet."));
    return;
  }
  $("people-list").replaceChildren(...shown.map((p) => {
    const t = totals(p);
    const bits = [];
    if (t.faces) bits.push(plural(t.faces, "face", "faces"));
    if (t.fingerprints) bits.push(plural(t.fingerprints, "fingerprint", "fingerprints"));
    if (t.pin) bits.push(t.pin === 1 ? "1 PIN" : `${t.pin} PINs`);
    if (t.cards) bits.push(plural(t.cards, "card", "cards"));
    return h("li", {}, h("button", {
      type: "button", class: "person-row", "aria-current": p.key === personKey ? "true" : "false",
      onclick: () => { newPersonMode = false; $("p-merge").hidden = false; personKey = p.key; renderPeople(); renderPerson(); },
    },
      h("span", { class: "person-name" }, p.name),
      h("span", { class: "person-count" }, plural(p.doors.length, "door", "doors")),
      h("span", { class: "person-creds" }, bits.join(", ") || "No credentials")));
  }));
}
$("people-search").addEventListener("input", renderPeople);

function renderPerson() {
  const p = people.find((x) => x.key === personKey);
  $("person-empty").hidden = !!p;
  $("person-body").hidden = !p;
  if (!p) return;
  $("p-name").textContent = p.name;
  const roles = new Set(p.doors.map((d) => d.role));
  $("p-sub").textContent = [p.email, `Can open ${plural(p.doors.length, "door", "doors")}`,
    roles.has("owner") ? "Account owner" : null].filter(Boolean).join(". ") + ".";

  document.querySelector(".access-table").hidden = false;
  $("p-doors").replaceChildren(...p.doors.flatMap((d) => {
    const creds = credSummary(d);
    const row = h("tr", {},
      h("td", {}, h("div", { class: "door-cell" },
        h("span", { class: "mini-unit" }, d.doorUnit || "—"),
        h("span", {}, d.doorRest || (d.doorUnit ? "" : d.doorName), " ", h("span", { class: "muted" }, d.doorModel)))),
      h("td", { class: d.role === "owner" ? "role-owner" : "" }, d.role.charAt(0).toUpperCase() + d.role.slice(1)),
      h("td", {}, d.access, !d.enabled && h("div", { class: "off" }, "Switched off")),
      h("td", {}, h("div", { class: "creds" },
        creds.length ? creds.map((c) => h("span", { class: "cred" }, c)) : h("span", { class: "cred none" }, "Nothing yet"),
        d.twoStep && h("span", { class: "cred" }, "Needs two"))),
      h("td", {}, rowActions(p, d)));
    return [row];
  }));
  setupGrant(p);

  // Merge controls — suggest records with the same name first
  const norm = (n) => n.toLowerCase().replace(/\s+/g, " ").trim();
  const others = people.filter((x) => x.key !== p.key);
  const sameName = others.filter((o) => norm(o.name) === norm(p.name));
  const sel = h("select", { id: "merge-target", "aria-label": "Person to merge in" },
    h("option", { value: "" }, "Choose someone…"),
    others.map((o) => h("option", { value: o.key }, `${o.name} (${plural(o.doors.length, "door", "doors")})`)));
  const suggestions = sameName.map((o) => h("p", {},
    `Also listed: ${o.name} on ${plural(o.doors.length, "door", "doors")}` +
    ` (${o.doors.map((d) => d.doorName).join(", ")}). `,
    h("button", {
      class: "btn btn-quiet", type: "button", onclick: async (ev) => {
        await busy(ev.currentTarget, async () => {
          try { applyPeople(await call("merge_people", [o.key], p.key)); toast("Merged."); }
          catch (e) { toast(e.message, true); }
        });
      },
    }, "Merge them")));
  $("p-merge-row").replaceChildren(...suggestions,
    h("label", { class: "field" }, h("span", {}, `Show as ${p.name}`), sel),
    h("button", {
      class: "btn btn-quiet", type: "button", onclick: async (ev) => {
        if (!sel.value) return;
        await busy(ev.currentTarget, async () => {
          try { applyPeople(await call("merge_people", [sel.value], p.key)); toast("Merged."); }
          catch (e) { toast(e.message, true); }
        });
      },
    }, "Merge"));
  $("p-merged").replaceChildren(p.mergedKeys && p.mergedKeys.length ? h("p", { class: "muted" },
    `Includes ${plural(p.mergedKeys.length, "other record", "other records")} you merged. `,
    h("button", {
      class: "btn btn-quiet", type: "button", onclick: async () => {
        try { applyPeople(await call("separate_person", p.key)); toast("Separated."); }
        catch (e) { toast(e.message, true); }
      },
    }, "Separate them again")) : "");
}

// ── editing access ─────────────────────────────────────────────────────

async function reloadPeople(keepKey) {
  try {
    applyPeople(await call("people"));
    if (keepKey && people.find((x) => x.key === keepKey)) { personKey = keepKey; renderPeople(); renderPerson(); }
  } catch (e) { toast(e.message, true); }
}

function rowActions(p, d) {
  const cell = h("div", { class: "row-actions" });
  const detailsBtn = h("button", { class: "btn btn-quiet", type: "button", "aria-expanded": "false" }, "Details");
  const pinBtn = h("button", { class: "btn btn-quiet", type: "button" }, d.hasPin ? "Change PIN" : "Add PIN");
  const removeBtn = h("button", { class: "btn btn-quiet btn-danger", type: "button" }, "Remove");

  detailsBtn.addEventListener("click", () => toggleDetails(detailsBtn, p, d));
  pinBtn.addEventListener("click", () => {
    const input = h("input", { type: "password", inputmode: "numeric", maxlength: "8", "aria-label": `New PIN for ${p.name} on ${d.doorName}`, autocomplete: "off" });
    const save = h("button", { class: "btn btn-ink", type: "button" }, "Save PIN");
    const cancel = h("button", { class: "btn btn-quiet", type: "button" }, "Cancel");
    const editor = h("div", { class: "pin-edit" }, input, save, cancel);
    cell.replaceChildren(editor);
    input.focus();
    cancel.addEventListener("click", () => renderPerson());
    const submit = async () => {
      await busy([save, cancel], async () => {
        toast(`Writing the new PIN to ${d.doorName}…`);
        try {
          await call("change_pin", d.lockId, d.userId, input.value);
          toast(`New PIN saved on ${d.doorName}.`);
          await reloadPeople(p.key);
        } catch (e) { toast(e.message, true); }
      });
    };
    save.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") renderPerson(); });
  });
  removeBtn.addEventListener("click", async () => {
    const ok = await confirmDialog(`Remove ${p.name} from ${d.doorName}?`,
      "Their PIN, fingerprints and face on this door stop working. Other doors aren't affected.", "Remove from door");
    if (!ok) return;
    await busy([removeBtn, pinBtn, detailsBtn], async () => {
      toast(`Removing ${p.name} from ${d.doorName}…`);
      try {
        await call("remove_from_door", d.lockId, d.userId);
        toast(`${p.name} removed from ${d.doorName}.`);
        await reloadPeople(p.key);
      } catch (e) { toast(e.message, true); }
    });
  });
  // The owner's PIN and membership belong to the Lockly app; only their
  // fingerprints and faces are managed here.
  if (d.doorRoute === "bluetooth") {           // no hub: nothing can reach this lock remotely
    for (const b of [pinBtn, removeBtn]) { b.disabled = true; b.title = "This door has no hub, so it can only be changed over Bluetooth."; }
  }
  cell.append(...(d.role === "owner" ? [detailsBtn] : [detailsBtn, pinBtn, removeBtn]));
  return cell;
}

async function toggleDetails(btn, p, d) {
  const tr = btn.closest("tr");
  const next = tr.nextElementSibling;
  if (next && next.classList.contains("detail-row")) {
    next.remove(); btn.setAttribute("aria-expanded", "false"); return;
  }
  btn.setAttribute("aria-expanded", "true");
  const td = h("td", { colspan: "5" }, h("p", { class: "muted" }, "Loading…"));
  tr.after(h("tr", { class: "detail-row" }, td));
  try {
    const det = await call("door_access_detail", d.lockId, d.userId);
    const item = (kind, label, c) => {
      const rm = h("button", { class: "btn btn-quiet btn-danger", type: "button" }, "Remove");
      rm.addEventListener("click", async () => {
        const ok = await confirmDialog(`Remove this ${label} from ${p.name}?`,
          `${c.name || label} stops opening ${d.doorName}. To use it again it has to be enrolled at the lock.`, `Remove ${label}`);
        if (!ok) return;
        await busy(rm, async () => {
          try {
            await call("remove_credential", d.lockId, d.userId, kind, c.crdntlId, c.code);
            toast(`${label.charAt(0).toUpperCase() + label.slice(1)} removed.`);
            await reloadPeople(p.key);
          } catch (e) { toast(e.message, true); }
        });
      });
      return h("li", {}, h("span", {}, c.name || label.charAt(0).toUpperCase() + label.slice(1), " ",
        h("span", { class: "muted" }, `${label}, slot ${c.slot ?? "?"}`)), rm);
    };
    const items = [
      ...det.fingerprints.map((c) => item("fingerprint", "fingerprint", c)),
      ...det.faces.map((c) => item("face", "face", c)),
      ...det.cards.map((c) => item("card", "card", c)),
    ];
    td.replaceChildren(items.length ? h("ul", { class: "cred-list" }, items)
      : h("p", { class: "muted" }, det.hasPin ? "Only a PIN on this door." : "No PIN, fingerprints, faces or cards on this door."));
  } catch (e) { td.replaceChildren(h("p", { class: "form-error" }, e.message)); }
}

let newPersonMode = false;
(() => {
  const box = $("grant-days");
  DAYS.forEach(([key, label], i) => box.append(
    h("label", {}, h("input", { type: "checkbox", name: "gday", value: key, checked: i < 5 }), h("span", {}, label))));
  $("grant-kind").addEventListener("change", () => {
    const k = $("grant-form").elements.kind.value;
    $("grant-weekly").hidden = k !== "weekly";
    $("grant-dates").hidden = k !== "dates";
  });
})();

function setupGrant(p) {
  const f = $("grant-form");
  const have = new Set(p ? p.doors.map((d) => d.lockId) : []);
  const options = locks.filter((l) => !have.has(l.id));
  f.elements.door.replaceChildren(...options.map((l) => h("option", { value: l.id, disabled: l.route === "bluetooth" },
    doorLabel(l) + (l.route === "bluetooth" ? " (no hub — Bluetooth only)" : ""))));
  const firstReachable = options.find((l) => l.route !== "bluetooth");
  if (firstReachable) f.elements.door.value = firstReachable.id;
  $("grant-name-field").hidden = !newPersonMode;
  $("grant-title").textContent = newPersonMode ? "Add a person" : "Give access to another door";
  $("grant-submit").textContent = newPersonMode ? "Add person" : "Give access";
  $("grant-submit").disabled = !options.length;
  $("grant-error").textContent = options.length ? "" : "This person can already open every door.";
  const knownPin = p && (p.doors.find((d) => d.pin) || {}).pin;
  f.elements.pin.value = knownPin || "";
}

$("add-person").addEventListener("click", () => {
  newPersonMode = true;
  personKey = null;
  renderPeople();
  $("person-empty").hidden = true;
  $("person-body").hidden = false;
  $("p-name").textContent = "New person";
  $("p-sub").textContent = "Choose a door and how they get in. You can add more doors afterwards.";
  $("p-doors").replaceChildren();
  document.querySelector(".access-table").hidden = true;
  $("p-merge").hidden = true;
  setupGrant(null);
  $("grant-form").elements.name.focus();
});

$("grant-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const p = people.find((x) => x.key === personKey);
  const name = newPersonMode ? f.elements.name.value.trim() : (p && p.name);
  const kind = f.elements.kind.value;
  const access = { kind };
  if (kind === "weekly") {
    access.days = [...f.querySelectorAll("input[name=gday]:checked")].map((x) => x.value);
    access.start = f.elements.start.value; access.end = f.elements.end.value;
  } else if (kind === "dates") {
    access.from = f.elements.from.value; access.to = f.elements.to.value;
  }
  const lockId = f.elements.door.value;
  const door = locks.find((l) => l.id === lockId);
  $("grant-error").textContent = "";
  if (!name) { $("grant-error").textContent = "Give the person a name."; return; }
  const ok = await confirmDialog(`Give ${name} access to ${door ? doorLabel(door) : "this door"}?`,
    f.elements.pin.value ? "The PIN starts working on that door as soon as the lock confirms it." : "They'll have no PIN on that door until you add one.",
    "Give access");
  if (!ok) return;
  await busy($("grant-submit"), async () => {
    toast(`Adding ${name} to ${door ? doorLabel(door) : "the door"}…`);
    try {
      await call("add_to_door", lockId, name, f.elements.pin.value, access);
      toast(`${name} can now open ${door ? doorLabel(door) : "the door"}.`);
      newPersonMode = false;
      $("p-merge").hidden = false;
      const key = p ? p.key : `name:${name.toLowerCase()}`;
      await reloadPeople(key);
    } catch (e) { $("grant-error").textContent = e.message; }
  });
});

boot();
