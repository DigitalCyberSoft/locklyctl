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

// el.replaceChildren with h's rule for kids: the DOM's own turns null into the text "null".
function fill(el, ...kids) {
  el.replaceChildren(...kids.flat().filter((kid) => kid != null && kid !== false));
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
// "Offline" when Lockly's server said so, rather than the lock not answering.
const stateWord = (v) => (stateOf(v) === "asleep" && v.offline ? "Offline" : STATE_WORD[stateOf(v)]);
const DAYS = [["mon", "Mon"], ["tue", "Tue"], ["wed", "Wed"], ["thu", "Thu"], ["fri", "Fri"], ["sat", "Sat"], ["sun", "Sun"]];

let api = null;
let locks = [];             // every door in the list's order (Api.locks), hidden ones included
let currentId = null;
// Editing the door list (favorites, order, hiding): it lists hidden doors too, so one can be put back.
let editing = false;
const shownLocks = () => locks.filter((v) => !v.hidden);
let activeTab = "activity";
let activeView = "doors";
let toastTimer = null;

// What this build and device can do (Api.capabilities), so the page offers only that.
let caps = { device: "computer", bluetooth: "", commandLine: false, version: "", logPath: "" };
const here = () => (caps.device === "phone" ? "this phone" : "this computer");
const Here = () => here().replace(/^t/, "T");
// Why a job that needs Bluetooth isn't offered here, or "" when Bluetooth can be tried.
// A source install can add it (the reason says how); the packaged and phone apps can't yet.
const noBluetooth = () => (!caps.bluetooth ? "" : caps.commandLine ? caps.bluetooth
  : `The ${caps.device === "phone" ? "phone app" : "packaged app"} has no Bluetooth yet: `
    + "use locklyctl from source on a computer with Bluetooth, next to the door.");

// Where this session signed in (Api.session / sign_in): Lockly, or a private locklyctl-mqtt
// server, which serves the locks' own commands but not Lockly's cloud features.
let account = { email: "", server: "", plainHttp: false };
const cloud = () => (account.server ? "your server" : "Lockly's server");
const Cloud = () => (account.server ? "Your server" : "Lockly's server");
// Plain http to another machine (the page's copy of transport.unencrypted, for the warning as it's typed).
function plainHttp(url) {
  const m = /^http:\/\/(\[[^\]]*\]|[^/:?#]*)/i.exec((url || "").trim());
  if (!m) return false;
  const host = m[1].toLowerCase().replace(/^\[|\]$/g, "");
  return !(host === "localhost" || host.endsWith(".localhost") || host === "::1" || /^127\./.test(host));
}
const PLAIN_TEXT = "This address isn't https: your password and every door's master code would cross the "
  + "network readable. Use https unless the network is yours alone.";

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
  for (const id of ["boot", "unlock", "signin", "app"]) $(id).hidden = id !== screen;
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
  if (i < 0) return;        // an answer that came in after signing out, or into another account
  locks[i] = view;
  renderPlates();
  renderFavs();
  renderMaint();
  if (view.id === currentId) renderDoor();
}

// The lock lets people on a schedule in by its own clock, so a wrong one is
// worth saying: off by 2 minutes or more (features.CLOCK_WARN_S), or no time.
const CLOCK_WARN_S = 120;
function offsetWords(offset) {
  const min = Math.round(Math.abs(offset) / 60);
  return `${min >= 120 ? `${Math.round(min / 60)} hours` : `${min} min`} ${offset < 0 ? "behind" : "ahead"}`;
}
function clockText(known, offset) {
  if (!known) return null;
  if (offset == null) return "The lock has no valid time";
  if (Math.abs(offset) < CLOCK_WARN_S) return null;
  return `The lock's clock is ${offsetWords(offset)}`;
}
// A wrong clock the last check tried to fix with a second check (clock_fix).
function clockFixText(fix) {
  if (!fix) return null;
  const was = fix.was == null ? "had no valid time" : `was ${offsetWords(fix.was)}`;
  return fix.fixed ? `Its clock ${was}; the check set it.` : `Its clock ${was}, and ${fix.detail}.`;
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
  caps = { ...caps, ...(await call("capabilities")) };
  document.documentElement.dataset.device = caps.device;
  let s;
  try {
    s = await call("session");
  } catch (e) {
    s = { signedIn: false, message: e.message };
  }
  afterSession(s);
}

// Where a session (Api.session, or the one unlock_app returns) leads: the unlock
// screen while the app lock holds, else the app, else signing in.
function afterSession(s) {
  if (s.locked) return showUnlock(s.wait);
  if (s.signedIn) return enterApp(s);
  showSignin(s.email, s.message, s.server);
}

// The "Sign in to: Lockly / A private server" choice, on the sign-in screen and in Settings
// (prefix "signin" or "switch"): the address field shows for a server, with a warning for plain http.
function setWhere(prefix, server) {
  const f = $(`${prefix}-form`);
  f.where.value = server ? "server" : "lockly";
  if (server !== undefined) f.server.value = server || "";
  showWhere(prefix);
}

function showWhere(prefix) {
  const f = $(`${prefix}-form`);
  const toServer = f.where.value === "server";
  $(`${prefix}-server-field`).hidden = !toServer;
  const warn = $(`${prefix}-plain`);
  warn.hidden = !(toServer && plainHttp(f.server.value));
  warn.textContent = warn.hidden ? "" : PLAIN_TEXT;
  if (prefix === "signin") {
    $("signin-about").textContent = toServer
      ? "Sign in to your own locklyctl-mqtt server, with the account it has for you."
      : "Sign in with your Lockly account.";
  }
}

for (const prefix of ["signin", "switch"]) {
  $(`${prefix}-where`).addEventListener("change", () => {
    showWhere(prefix);
    if ($(`${prefix}-form`).where.value === "server") $(`${prefix}-form`).server.focus();
  });
  $(`${prefix}-form`).server.addEventListener("input", () => showWhere(prefix));
}

// What sign_in gets from one of those forms: the server's address, or "" for Lockly.
function whereTo(f, errorId) {
  if (f.where.value !== "server") return "";
  const server = f.server.value.trim();
  if (!server) {
    $(errorId).textContent = "Enter the server's address, like https://locks.example.net.";
    f.server.focus();
    return null;
  }
  return server;
}

// ── the app lock: a passcode that seals what locklyctl saved on this device ──

function showUnlock(wait, message) {
  const f = $("unlock-form");
  f.passcode.value = "";
  $("unlock-error").textContent = message
    || (wait ? `Too many wrong passcodes. Try again in ${wait < 90 ? `${wait} s` : `${Math.ceil(wait / 60)} min`}.` : "");
  show("unlock");
  f.passcode.focus();
}

$("unlock-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  $("unlock-error").textContent = "";
  await busy(f.querySelector("button[type=submit]"), async () => {
    try {
      const s = await call("unlock_app", f.passcode.value);
      f.passcode.value = "";
      afterSession(s);
    } catch (e) {
      f.passcode.value = "";
      $("unlock-error").textContent = e.message;
      f.passcode.focus();
    }
  });
});

$("unlock-erase").addEventListener("click", async () => {
  const ok = await confirmDialog("Erase what's saved on this device?",
    "The saved sign-in, favorites, the door list's order and saved Wi-Fi passwords on this device are deleted, and "
    + "can't be brought back. Your locks and your Lockly account aren't touched. You'll sign in again with your "
    + "Lockly email and password.", "Erase");
  if (!ok) return;
  try {
    const s = await call("erase_saved_data");
    showSignin(s.email);
  } catch (e) { $("unlock-error").textContent = e.message; }
});

// Settings, App lock: off (set a passcode), or on (change it, lock now, turn it off).
async function renderAppLock() {
  const f = $("applock-form");
  let st;
  try { st = await call("app_lock"); } catch (e) { $("applock-about").textContent = e.message; f.hidden = true; return; }
  f.hidden = !!st.available;
  $("applock-about").textContent = st.available
    ? `An app passcode can't be used on this device. ${st.available}`
    : st.on ? "On. locklyctl asks for the passcode when it starts, and what it saves on this device is sealed with it."
      : "Ask for a passcode when locklyctl starts on this device. The passcode seals what the app saves here "
        + "(the sign-in, saved Wi-Fi passwords, favorites, the door list's order): without it the file can't be read.";
  $("applock-current-field").hidden = !st.on;
  $("applock-new-label").textContent = st.on ? "New passcode" : "Passcode";
  $("applock-set").textContent = st.on ? "Change passcode" : "Turn on";
  $("applock-now").hidden = $("applock-off").hidden = !st.on;
  f.dataset.on = st.on ? "1" : "";
  f.reset();
  $("applock-error").textContent = "";
}

$("applock-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const on = f.dataset.on === "1";
  const err = (text) => { $("applock-error").textContent = text; };
  err("");
  if (f.passcode.value.length < 4) return err("The passcode needs at least 4 characters.");
  if (f.passcode.value !== f.again.value) return err("The two passcodes aren't the same.");
  if (on && !f.current.value) return err("Enter the current passcode to change it.");
  await busy(f.querySelector("#applock-set"), async () => {
    try {
      const r = await call("set_app_lock", f.passcode.value, on ? f.current.value : "");
      await renderAppLock();
      if (r.leftInKeyring.length) {
        toast(`The app lock is on, but a copy of ${r.leftInKeyring.join(", ")} couldn't be removed from the system keyring: remove it there.`, true);
      } else {
        toast((on ? "Passcode changed." : "The app lock is on.")
          + (r.strength === "short" ? " This passcode is short: it won't stop someone who copies the saved file." : ""));
      }
    } catch (e) { err(e.message); }
  });
});

$("applock-off").addEventListener("click", async (ev) => {
  const f = $("applock-form");
  if (!f.current.value) { $("applock-error").textContent = "Enter the current passcode to turn the app lock off."; f.current.focus(); return; }
  const ok = await confirmDialog("Turn the app lock off?",
    "locklyctl will open without a passcode, and what it saves on this device won't be sealed.", "Turn off");
  if (!ok) return;
  await busy(ev.currentTarget, async () => {
    try {
      await call("remove_app_lock", f.current.value);
      await renderAppLock();
      toast("The app lock is off.");
    } catch (e) { $("applock-error").textContent = e.message; }
  });
});

$("applock-now").addEventListener("click", async () => {
  try {
    const r = await call("lock_now");
    forgetSession();
    showUnlock(r.wait);
  } catch (e) { toast(e.message, true); }
});

// Signing out and locking both end the session: the next one has its own doors and
// people, and a locked or signed-out page keeps none of the last one's.
function forgetSession() {
  locks = []; currentId = null; editing = false;
  people = []; personKey = null; peopleLoaded = false; newPersonMode = false;
  renderPlates();
  renderFavs();
  renderDoor();
  renderMaint();
  renderPerson();
  $("people-list").replaceChildren();
  $("people-search").value = "";
  for (const id of ["panel-activity", "access-list"]) $(id).replaceChildren();
  resetSchedules();
  resetJobs();
  resetOac();
  closeSwitch();
  account = { email: "", server: "", plainHttp: false };
  $("who").textContent = "";
}

// ``server``: where a saved sign-in that no longer works was for, so trying again goes there.
function showSignin(email, message, server) {
  const f = $("signin-form");
  if (email) f.email.value = email;
  setWhere("signin", server || "");
  $("signin-error").textContent = message || "";
  show("signin");
  (email ? f.password : f.email).focus();
}

$("signin-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  $("signin-error").textContent = "";
  const server = whereTo(f, "signin-error");
  if (server === null) return;
  await busy(f.querySelector("button[type=submit]"), async () => {
    try {
      const s = await call("sign_in", f.email.value, f.password.value, f.remember.checked, server);
      f.password.value = "";
      await enterApp(s);
    } catch (e) {
      $("signin-error").textContent = e.message;
    }
  });
});

// ── Settings, Account: where this session is signed in, and signing in somewhere else ──

function renderAccount() {
  $("who").textContent = account.email;
  $("who-where").textContent = account.server ? ` on ${account.server}, a private server` : " on Lockly";
  $("who-plain").hidden = !account.plainHttp;
  $("who-plain").textContent = account.plainHttp
    ? "This server is reached over plain http: the password and every door's master code cross the network readable."
    : "";
  const consoleNote = $("who-console");
  consoleNote.hidden = !account.server;
  consoleNote.replaceChildren(...(account.server ? [
    "People, PINs and schedules on this server are kept in its own console, at ",
    h("code", {}, `${account.server}/`), ". Firmware and offline codes are Lockly's, so they aren't offered here."] : []));
}

function openSwitch() {
  const f = $("switch-form");
  f.hidden = false;
  $("switch-open").hidden = true;
  $("switch-error").textContent = "";
  f.email.value = account.email;
  f.password.value = "";
  f.server.value = account.server;                        // the same server, for another account on it
  f.where.value = account.server ? "lockly" : "server";   // the usual reason to switch: the other place
  showWhere("switch");
  (f.where.value === "server" ? f.server : f.password).focus();
}

function closeSwitch() {
  const f = $("switch-form");
  f.hidden = true;
  f.password.value = "";
  $("switch-open").hidden = false;
}

$("switch-open").addEventListener("click", openSwitch);
$("switch-cancel").addEventListener("click", closeSwitch);

$("switch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  $("switch-error").textContent = "";
  const server = whereTo(f, "switch-error");
  if (server === null) return;
  await busy($("switch-submit"), async () => {
    let s;
    try {
      s = await call("sign_in", f.email.value, f.password.value, f.remember.checked, server);
    } catch (e) {
      $("switch-error").textContent = e.message;         // the doors in use stay as they were
      return;
    }
    forgetSession();
    await enterApp(s);
    toast(`Signed in as ${s.email} on ${s.server || "Lockly"}.`);
  });
});

// How doors are reached: one setting for this device (Api.connection), in Settings.
// Without Bluetooth here there is nothing to choose, so it's said in a line instead.
function showConnection(c) {
  for (const r of $("conn-mode").querySelectorAll("input")) r.checked = r.value === c.mode;
  const why = noBluetooth();
  $("conn-mode").hidden = !!why && c.mode !== "bluetooth";
  $("conn-remote").textContent = account.server ? "Your server" : "Hub or Lockly's server";
  const remote = account.server ? "your server" : "the hub or Lockly's server";
  $("conn-about").textContent = why
    ? `Doors are reached through ${remote}.`
    : `How ${here()} reaches every door. Auto uses ${remote}, and when a door doesn't answer a `
      + `status check that way, tries it over Bluetooth from ${here()}. Only status checks go over Bluetooth so far.`;
  $("conn-note").textContent = why ? `Bluetooth isn't used here. ${why}` : "";
}

$("conn-mode").addEventListener("change", async (ev) => {
  try {
    showConnection(await call("set_connection", ev.target.value));
  } catch (e) {
    toast(e.message, true);
    call("connection").then(showConnection, (err) => toast(err.message, true));
  }
});

// ``s``: the session (Api.session or sign_in): who, and on which server ("" or missing: Lockly).
async function enterApp(s) {
  account = { email: s.email, server: s.server || "", plainHttp: !!s.plainHttp };
  renderAccount();
  show("app");
  showView("doors");
  renderAbout();
  renderAppLock();
  call("connection").then(showConnection, (e) => { $("conn-note").textContent = e.message; });
  $("plates").replaceChildren(h("li", { class: "muted" }, "Loading doors…"));
  try {
    locks = await call("locks");
  } catch (e) {
    $("plates").replaceChildren(h("li", { class: "form-error" }, e.message));
    return;
  }
  // Api.locks returns them in the list's order: by name until a door is moved.
  renderFavs();
  if (!locks.length) {
    $("plates").replaceChildren(h("li", { class: "muted" }, account.server
      ? "No locks on this account on the server yet. Add them in its console, or restore a backup into it."
      : "No locks on this account. Add them in the Lockly app first."));
    return;
  }
  renderPlates();
  const first = shownLocks()[0] || locks[0];
  selectLock(current() ? currentId : first.id);
  // Installs still running from before this page was (re)loaded: follow them again.
  call("firmware_jobs").then((ids) => {
    for (const id of ids) {
      const v = locks.find((l) => l.id === id);
      if (v && pending.get(id) !== "firmware") followInstall(v, () => call("firmware_install_status", id));
    }
  }, (e) => toast(e.message, true));
}

// The window asked to close while an install runs (gui/app.py keeps it open).
window.closeRefused = (running) => {
  toast(`locklyctl stays open until the firmware install has finished: ${running.join("; ")}.`, true);
};
window.addEventListener("beforeunload", (ev) => {
  if ([...pending.values()].includes("firmware")) { ev.preventDefault(); ev.returnValue = ""; }
});

$("sign-out").addEventListener("click", async () => {
  const ok = await confirmDialog("Sign out?",
    `This also forgets the saved sign-in, so you'll need your ${account.server ? "password for the server"
      : "Lockly password"} next time.`, "Sign out");
  if (!ok) return;
  let s;
  try {
    s = await call("sign_out");
  } catch (e) {
    toast(e.message, true);
    return;
  }
  forgetSession();
  showSignin(s.email);
});

$("check-all").addEventListener("click", async (ev) => {
  const doors = locks.filter((v) => v.route !== "bluetooth");
  const left = locks.length - doors.length;
  const hid = doors.filter((v) => v.hidden).length;     // hidden doors are still checked
  const ok = await confirmDialog(`Check all ${doors.length} doors?`,
    "Each lock wakes up to answer, and beeps. Locks that are asleep may not answer."
    + (left ? ` ${left} ${left === 1 ? "door has no hub, so it's" : "doors have no hub, so they're"} left out.` : "")
    + (hid ? ` ${hid} of them ${hid === 1 ? "is" : "are"} hidden from the door list.` : ""),
    "Check all doors");
  if (!ok) return;
  await busy(ev.currentTarget, async () => {
    toast(`Checking ${doors.length} doors…`);
    reportChecks(await checkDoors(doors), "doors");
  });
});

// ── plates ─────────────────────────────────────────────────────────────

function renderPlates() {
  const edit = $("edit-list");
  edit.setAttribute("aria-pressed", String(editing));
  edit.textContent = editing ? "Done" : "Edit list";
  $("plates").classList.toggle("editing", editing);
  if (editing) {                // every door, hidden ones too, each a row with its list tools
    $("plates").replaceChildren(...locks.map(editRow));
    renderRetry();
    return;
  }
  const shown = shownLocks();
  const nHidden = locks.length - shown.length;
  if (locks.length && !shown.length) {
    $("plates").replaceChildren(h("li", { class: "muted list-note" },
      "Every door is hidden. “Edit list” brings one back."));
    renderRetry();
    return;
  }
  const items = shown.map((v) => {
    const num = v.unit || v.name;
    const batt = v.battery != null ? `${v.battery}%` : v.lowBattery ? "Low battery" : null;
    const doing = pending.get(v.id);
    const word = doing ? DOING_SHORT[doing] : stateWord(v);
    return h("li", {},
      h("button", {
        class: "plate", type: "button", "data-state": stateOf(v),
        "aria-busy": doing ? "true" : null,
        "aria-current": v.id === currentId ? "true" : "false",
        "aria-label": `${doorLabel(v)}, ${word}${v.favorite ? ", favorite" : ""}`,
        onclick: () => selectLock(v.id, true),
      },
        h("span", { class: "plate-num" + (v.unit ? "" : " wordy") }, num),
        h("span", { class: "plate-rest" }, v.unit ? v.rest : ""),
        h("span", { class: "plate-state" },
          h("span", { class: "plate-word" }, doing ? icon(v.route) : stateIcon(stateOf(v)), word),
          batt && h("span", { class: "plate-batt" + (v.lowBattery || (v.battery != null && v.battery <= 20) ? " low" : "") }, batt)),
        v.favorite && h("span", { class: "plate-star", "aria-hidden": "true" }, icon("favorite-on"))));
  });
  if (nHidden) {
    items.push(h("li", { class: "muted list-note" },
      `${plural(nHidden, "door is", "doors are")} hidden. “Edit list” shows ${nHidden === 1 ? "it" : "them"}.`));
  }
  $("plates").replaceChildren(...items);
  renderRetry();
}

// Doors that didn't answer and can be asked again from here.
const asleepDoors = () => locks.filter((v) => v.state === "asleep" && v.route !== "bluetooth");

function renderRetry() {
  const n = asleepDoors().length;
  $("retry-asleep").hidden = !n || editing;
  $("retry-asleep").replaceChildren(icon("check"), `Retry ${n} asleep`);
}

$("retry-asleep").addEventListener("click", async (ev) => {
  const doors = asleepDoors();
  if (!doors.length) return;
  await busy(ev.currentTarget, async () => {
    toast(`Asking ${doors.length} ${doors.length === 1 ? "door" : "doors"} again…`);
    reportChecks(await checkDoors(doors), "");
  });
});

// Ask doors for their live status, CHECK_AT_ONCE at a time. Each door shows
// "Connecting…" from the start and updates as soon as its own answer is in.
// Doors with no hub, and doors busy with something else, are skipped.
const CHECK_AT_ONCE = 6;      // as many at once as LocklyClient.refresh_all_status asks
async function checkDoors(doors) {
  const queue = doors.filter((v) => v.route !== "bluetooth" && !pending.has(v.id));
  const asked = queue.length;
  queue.forEach((v) => pending.set(v.id, "check"));
  renderPlates();
  renderFavs();
  if (queue.some((v) => v.id === currentId)) renderDoor();
  const fresh = [];
  const failed = [];
  const worker = async () => {
    for (let v = queue.shift(); v; v = queue.shift()) {
      let view = null;
      try { view = await call("check", v.id); }
      catch (e) { failed.push(`${doorLabel(v)}: ${e.message}`); }
      pending.delete(v.id);
      if (view) { fresh.push(view); updateLock(view); } else refreshControls(v.id);
    }
  };
  await Promise.all(Array.from({ length: Math.min(CHECK_AT_ONCE, asked) }, worker));
  return { asked, fresh, failed };
}

function reportChecks(r, noun) {
  if (!r.asked) { toast("Those doors are busy with something else right now."); return; }
  if (r.failed.length) {
    toast(r.failed.length === 1 ? r.failed[0] : `${r.failed.length} doors couldn't be asked. ${r.failed[0]}`, true);
    return;
  }
  const answered = r.fresh.filter((l) => l.state === "locked" || l.state === "unlocked").length;
  toast(`${answered} of ${r.asked}${noun ? " " + noun : ""} answered.`, answered === 0);
}

// ── favorites: one tap to lock or unlock, without opening the door ─────

const pending = new Map();    // lock id → what's being done to it while that's out (a DOING key)
const DOING = { lock: "Locking…", unlock: "Unlocking…", check: "Connecting…", keep: "Setting it to stay unlocked…",
  firmware: "Updating firmware…" };
const DOING_SHORT = { ...DOING, keep: "Setting…", firmware: "Updating…" };   // short enough for a plate

function renderFavs() {
  const favs = locks.filter((v) => v.favorite && !v.hidden);     // hiding a door takes it off here too
  $("favs").hidden = editing || !favs.length;
  $("fav-list").replaceChildren(...favs.map((v) => {
    const st = stateOf(v);
    const off = pending.has(v.id) || v.route === "bluetooth";
    return h("li", { class: "fav", "data-state": st, "aria-busy": pending.has(v.id) ? "true" : null },
      h("button", {
        class: "fav-door", type: "button", "aria-current": v.id === currentId ? "true" : "false",
        onclick: () => selectLock(v.id, true),
      },
        h("span", { class: "fav-name" }, doorLabel(v)),
        h("span", { class: "fav-state" }, DOING[pending.get(v.id)] || stateWord(v))),
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
      toast(`Connecting to ${label}…`);
      const view = await call("check", v.id);
      updateLock(view);
      const fix = clockFixText(view.clockFix);
      toast((view.state !== "asleep"
        ? `${label}: ${STATE_WORD[view.state]}${view.via === "bluetooth" ? " (over Bluetooth)" : ""}.`
        : (view.offline ? `${Cloud()} says ${label} is offline.` : `${label} didn't answer.`)
          + (view.bluetoothNote ? ` Bluetooth: ${view.bluetoothNote}` : ""))
        + (fix ? ` ${fix}` : ""),
        view.state === "asleep" || (view.clockFix && !view.clockFix.fixed));
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

// After a confirm dialog: the door may have got busy while it was open.
function busyNow(v) {
  if (!pending.has(v.id)) return false;
  toast(`${doorLabel(v)} is busy (${DOING[pending.get(v.id)]}). Try again when that's done.`, true);
  return true;
}

function refreshControls(id) {
  renderPlates();
  renderFavs();
  if (id === currentId) renderDoor();
}

// On a phone a view shows its list or one item, not both (style.css): this says which.
function setPane(ws, pane) {
  $(ws).dataset.pane = pane;
  if (pane === "detail") $(ws).querySelector(".door").scrollTop = 0;
}
for (const b of document.querySelectorAll(".back")) {
  b.addEventListener("click", () => setPane(b.closest(".workspace").id, "list"));
}

// The door being worked on, in Doors and in Maintenance alike.
// connect: it was chosen in the door list, so also ask it for its live status.
function selectLock(id, connect = false) {
  const changed = id !== currentId;
  currentId = id;
  renderPlates();
  renderFavs();
  renderDoor();
  renderMaint();
  if (changed) {
    resetSchedules();
    resetJobs();
    resetOac();
    loadTab(activeTab);
  }
  const v = current();
  if (connect) setPane("doors-ws", "detail");
  if (connect && v && !recentlyOverBluetooth(v)) {
    checkDoors([v]).then((r) => { if (r.failed.length) toast(r.failed[0], true); });
  }
}

// A door that answered over Bluetooth a moment ago isn't asked again just
// because it was clicked: each ask is a new Bluetooth connection (the lock
// drops an idle one after 6-8 s) and a beep. "Check now" still asks.
const BLUETOOTH_FRESH_S = 60;
function recentlyOverBluetooth(v) {
  return v.via === "bluetooth" && !v.cached && v.checkedAt != null
    && Date.now() / 1000 - v.checkedAt < BLUETOOTH_FRESH_S;
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

  const st = $("d-state");
  const doing = pending.get(v.id);
  st.dataset.state = doing ? "busy" : v.route === "bluetooth" ? "unknown" : v.state;
  st.replaceChildren(...(doing ? [icon(v.route), DOING[doing]]
    : [stateIcon(stateOf(v)), v.route === "bluetooth" ? "No hub: only reachable over Bluetooth"
      : v.state === "asleep" ? (v.offline ? `Offline: ${cloud()} says its Wi-Fi isn't connected`
        : "Didn't answer (asleep or offline)")
        : v.state === "unknown" ? "Status not checked yet" : STATE_WORD[v.state]]));

  const facts = [];
  if (v.battery != null) facts.push(h("li", { class: v.battery <= 20 ? "warn" : "" }, icon("battery"), `Battery ${v.battery}%`));
  else if (v.lowBattery) facts.push(h("li", { class: "warn" }, icon("battery"), "Battery low"));
  if (v.doorOpen != null) facts.push(h("li", { class: v.doorOpen ? "warn" : "" }, v.doorOpen ? "Door open" : "Door closed"));
  const since = sinceText(v.checkedAt);
  if (since) facts.push(h("li", {}, v.cached ? `From the hub, ${since}`
    : `Checked ${since}${v.via === "bluetooth" ? " over Bluetooth" : ""}`));
  if (v.wifiOffline) facts.push(h("li", { class: "warn" }, `Wi-Fi offline: ${cloud()} says so, and ${here()} reached it over Bluetooth`));
  if (v.state === "asleep" && v.bluetoothNote) facts.push(h("li", {}, `Bluetooth: ${v.bluetoothNote}`));
  const clock = clockText(v.clockKnown, v.clockOffset);
  const fix = clockFixText(v.clockFix);
  if (fix) facts.push(h("li", { class: v.clockFix.fixed ? "" : "warn" }, fix));
  else if (clock) facts.push(h("li", { class: "warn" }, clock));
  if (v.firmware) facts.push(h("li", {}, `Firmware ${v.firmware}`));
  $("d-facts").replaceChildren(...facts);
  renderKeep(v);
}

function renderTimeZone(v) {
  $("tz-box").hidden = !v.takesTimeZone;
  const where = account.server ? "your server's account" : "the Lockly account";
  $("tz-about").textContent = "The lock also keeps a time zone of its own, which the Lockly app gives it when it's "
    + `set up and after a firmware update. This gives it the one in ${where}, then the time. It wakes the lock.`;
  $("tz-note").textContent = v.timeZone === "not set"
    ? `${where.replace(/^t/, "T").replace(/^y/, "Y")} has no time zone for this lock, so it gets ${here()}'s (${v.computerTimeZone}).`
    : `In ${where}: ${v.timeZone}. ${Here()}: ${v.computerTimeZone}.`
      + (v.timeZone !== v.computerTimeZone ? " They differ: the lock gets the account's." : "");
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
  if (!ok || busyNow(v)) return;
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

// ── the door list: favorites, your own order, and hiding doors ─────────

$("edit-list").addEventListener("click", () => {
  editing = !editing;
  renderPlates();
  renderFavs();
});

// One door in the edit list. Every door is listed, so a move steps past hidden ones too.
function editRow(v, i, all) {
  const name = doorLabel(v);
  const noRoute = v.route === "bluetooth";
  const cantFav = noRoute && !v.favorite;      // a door with no hub can't be opened from a favorite
  const tool = (key, attrs, ...kids) => h("button", { type: "button", "data-tool": key, ...attrs }, ...kids);
  return h("li", { class: "edit-row" + (v.hidden ? " is-hidden" : ""), "data-id": v.id },
    h("span", { class: "edit-name" }, name, v.hidden ? h("span", { class: "muted" }, " (hidden)") : null),
    h("span", { class: "edit-tools" },
      tool("fav", { class: "btn btn-quiet icon-only", "aria-pressed": String(!!v.favorite), disabled: cantFav,
        "aria-label": `Favorite: ${name}`,
        title: cantFav ? "This door has no hub, so it can only be opened over Bluetooth."
          : "Favorites sit at the top of the list with Lock and Unlock.",
        onclick: () => editDoor(v, "fav", "set_favorite", !v.favorite) }, icon(v.favorite ? "favorite-on" : "favorite")),
      tool("up", { class: "btn btn-quiet icon-only", disabled: i === 0, "aria-label": `Move ${name} up`,
        title: "Move up", onclick: () => moveDoor(v, -1, "up") }, "↑"),
      tool("down", { class: "btn btn-quiet icon-only", disabled: i === all.length - 1, "aria-label": `Move ${name} down`,
        title: "Move down", onclick: () => moveDoor(v, 1, "down") }, "↓"),
      tool("hide", { class: "btn btn-quiet", "aria-pressed": String(!!v.hidden),
        title: v.hidden ? "Put this door back in the door list." : "Leave this door out of the door list. “Check all” still checks it.",
        onclick: () => editDoor(v, "hide", "set_hidden", !v.hidden) }, v.hidden ? "Show" : "Hide")));
}

// The list is drawn again after each change, so put the keyboard back on the tool that made it.
function refocus(id, tool) {
  const el = document.querySelector(`.edit-row[data-id="${CSS.escape(id)}"] [data-tool="${tool}"]`);
  if (el && !el.disabled) el.focus();
}

async function editDoor(v, tool, method, on) {
  try {
    const view = await call(method, v.id, on);
    updateLock(view);
    toast(method === "set_favorite"
      ? (view.favorite ? `${doorLabel(v)} is in your favorites.` : `${doorLabel(v)} removed from favorites.`)
      : (view.hidden ? `${doorLabel(v)} is hidden from the door list.` : `${doorLabel(v)} is back in the door list.`));
  } catch (e) { toast(e.message, true); }
  refocus(v.id, tool);
}

async function moveDoor(v, by, tool) {
  try {
    locks = await call("move_lock", v.id, by, true);
    renderPlates();
    renderFavs();
    renderMaint();
  } catch (e) { toast(e.message, true); }
  refocus(v.id, tool);
}

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

const TABS = ["activity", "access", "schedules"];

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
  else if (name === "access") loadAccess();
  else if (name === "schedules") setupSchedules();
}

async function loadActivity() {
  const id = currentId;
  const panel = $("panel-activity");
  panel.replaceChildren(h("p", { class: "muted" }, "Loading the last 7 days…"));
  try {
    const events = await call("activity", id);
    if (id !== currentId) return;
    if (!events.length) {
      const v = current();
      panel.replaceChildren(h("p", { class: "muted" }, v && v.eventLog
        ? `No events in the last 7 days. This lock sends its events to ${account.server ? "your server" : "Lockly"} `
          + "over its Wi-Fi, so none arrive while it's offline."
        : account.server ? "Your server has no record of this door in the last 7 days."
        : "Lockly's cloud has no record of this door in the last 7 days. That doesn't mean nobody used it: "
          + "for this lock the cloud only has what was uploaded to it."));
      return;
    }
    panel.replaceChildren(h("table", { class: "log" },
      h("tbody", {}, events.slice(0, 200).map((ev) =>
        h("tr", {}, h("td", {}, ev.time), h("td", {}, ev.what), h("td", {}, ev.who))))));
  } catch (e) {
    if (id === currentId) panel.replaceChildren(h("p", { class: "form-error" }, e.message));
  }
}

// ── access: the people on this door ────────────────────────────────────
// The same rows and changes as the People view, for one door. The lock isn't woken to list them.

async function loadAccess() {
  const id = currentId;
  const list = $("access-list");
  $("access-slot").hidden = !!account.server;            // the grant form may sit here from before
  if (account.server) {
    loadOac();                                           // hides itself: offline codes are Lockly's
    list.replaceChildren(serverPeopleNote());
    return;
  }
  mountGrant("door");
  loadOac();
  list.replaceChildren(h("p", { class: "muted" }, "Loading who can open this door…"));
  try {
    const rows = await call("door_access", id);
    if (id !== currentId) return;
    fill(list, rows.length
      ? accessTable("Person", rows.map((d) => ({ p: { name: d.person }, d, reload: doorAccessChanged,
          first: h("span", { class: "who" }, d.person) })))
      : h("p", { class: "muted" }, "Nobody is on this door yet."));
  } catch (e) {
    if (id === currentId) list.replaceChildren(h("p", { class: "form-error" }, e.message));
  }
}

// After a change made from a door's Access tab: show it, and have People load afresh next time.
async function doorAccessChanged() {
  peopleLoaded = false;
  await loadAccess();
}

// ── offline codes: one-use codes the lock checks by itself (Visage, Vision) ──
// A code made here is shown once in #oac-made; the ones on record stay hidden until Show.

function resetOac() {
  $("oac").hidden = true;
  $("oac-error").textContent = "";
  $("oac-form").who.value = "";
  const made = $("oac-made");
  made.hidden = true;
  made.replaceChildren();
  $("oac-list").replaceChildren();
}

async function loadOac() {
  const id = currentId;
  const v = current();
  $("oac").hidden = !(v && v.offlineCodes);
  if (!v || !v.offlineCodes) return;
  const list = $("oac-list");
  list.replaceChildren(h("p", { class: "muted" }, "Loading the codes on record…"));
  try {
    const r = await call("offline_codes", id);
    if (id !== currentId) return;
    fill(list, r.codes.length
      ? [h("h3", {}, "On record"), h("ul", { class: "cred-list" }, r.codes.map(oacItem))]
      : h("p", { class: "muted" }, "No offline codes made for this door yet."));
  } catch (e) {
    if (id === currentId) list.replaceChildren(h("p", { class: "form-error" }, e.message));
  }
}

function oacItem(c) {
  const val = h("span", { class: "pin-val" }, "hidden");
  const show = h("button", { class: "btn btn-quiet", type: "button", "aria-pressed": "false" }, "Show");
  show.addEventListener("click", () => {
    const on = show.getAttribute("aria-pressed") !== "true";
    show.setAttribute("aria-pressed", String(on));
    show.textContent = on ? "Hide" : "Show";
    val.textContent = on ? c.shown : "hidden";
  });
  return h("li", {}, h("span", {}, c.name || "No name", c.once ? h("span", { class: "muted" }, " · one use") : null),
    h("span", { class: "pin-edit" }, val, show));
}

function oacMessage(v, r) {
  return [`${r.name}, here is your one-use code for ${doorLabel(v)}: ${r.code}`, "",
    ...r.steps.map((s, i) => `${i + 1}. ${s}`), "", r.note].join("\n");
}

function showOac(v, r) {
  const box = $("oac-made");
  const text = oacMessage(v, r);
  const area = h("textarea", { class: "oac-text", readonly: true, rows: String(text.split("\n").length + 1),
    "aria-label": "Message for the guest" });
  area.value = text;
  const copy = h("button", { class: "btn btn-quiet", type: "button" }, "Copy message");
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Message copied.");
    } catch (e) {
      area.select();
      toast("Couldn't copy here, so the message is selected: copy it by hand.", true);
    }
  });
  const done = h("button", { class: "btn btn-quiet", type: "button" }, "Done");
  done.addEventListener("click", () => { box.hidden = true; box.replaceChildren(); });
  fill(box,
    h("p", { class: "muted" }, `For ${r.name}, one use:`),
    h("p", { class: "oac-code" }, r.shown),
    r.counterSaved ? null : h("p", { class: "caution" },
      `${r.problem}. The code works, but Lockly didn't move on to the next one, so the next code made for this door, here or in the Lockly app, would be this same code.`),
    h("ol", { class: "oac-steps" }, r.steps.map((s) => h("li", {}, s))),
    h("p", { class: "fine" }, r.note),
    area,
    h("div", { class: "row" }, copy, done));
  box.hidden = false;
}

$("oac-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const id = currentId;
  const v = current();
  const err = $("oac-error");
  err.textContent = "";
  const who = f.who.value.trim();
  if (!who) {
    err.textContent = "Say who the code is for.";
    f.who.focus();
    return;
  }
  await busy($("oac-make"), async () => {
    try {
      let r = await call("make_offline_code", id, who, false);
      if (!r.made) {
        const ok = await confirmDialog("Make another code today?",
          `${r.madeToday} offline codes were made for this door from here today. Lockly says: "${r.ask}" A code made while earlier ones are still unused may not work.`,
          "Make it");
        if (!ok) return;
        r = await call("make_offline_code", id, who, true);
      }
      if (id !== currentId) return;
      f.who.value = "";
      showOac(v, r);
      loadOac();
    } catch (e) {
      if (id === currentId) err.textContent = e.message;
    }
  });
});

// ── schedule: Welcome Mode hours and Rain Mode ─────────────────────────

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
  if (!row.childElementCount && !v.welcomeForm) {
    row.append(h("label", { class: "field" },
      h("span", {}, `Schedule type for ${v.model || "this lock"} (not known for this model)`),
      h("select", { name: "form", onchange: syncWelcome },
        h("option", { value: "" }, "Choose…"),
        h("option", { value: "9B" }, "8-slot schedule (Visage and newer models)"),
        h("option", { value: "46" }, "Single schedule (Touch and older models)"))));
  }
  syncWelcome();
}

// The 8-slot form is tried on a Visage; the single-schedule form isn't yet, so only it
// carries the caution and a Preview that shows the command without sending it.
function syncWelcome() {
  const f = $("welcome-form");
  const form = welcomeForm();
  const always = $("w-always-opt");
  always.hidden = form !== "46";
  if (always.hidden && f.elements.action.value === "always") f.querySelector("input[value=set]").checked = true;
  $("w-hours").hidden = f.elements.action.value !== "set";
  $("w-entry").hidden = form !== "9B";
  $("w-caution").hidden = form === "9B";
  $("w-preview").hidden = form === "9B";
  $("w-slot-note").textContent = form === "9B"
    ? "Sending replaces the door's other weekly hours with these. A “Keep unlocked” window that's running stays."
    : "";
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
  const entry = form === "9B" ? f.elements.entry.value : "first";
  $("welcome-error").textContent = "";
  if (!ev.submitter) return;             // Enter in a field: sending to a lock takes the button
  if (!form) { $("welcome-error").textContent = "Choose the schedule type first."; return; }
  if (action === "set" && !days.length) { $("welcome-error").textContent = "Pick at least one day."; return; }
  if (send) {
    const hours = `${f.start.value} to ${f.end.value}`;
    const what = action === "off" ? "turn Welcome Mode off"
      : action === "always" ? "keep the door unlocked around the clock"
        : form !== "9B" ? `keep the door unlocked ${hours}`
          : entry === "auto" ? `unlock the door by itself at ${f.start.value} and lock it again at ${f.end.value}`
            : `keep the door unlocked ${hours}, from the first time someone opens it`;
    const ok = await confirmDialog(`Send to ${doorLabel(v)}?`,
      `This asks the lock to ${what}.` + (form === "46" ? " This form hasn't been tried on a real lock yet." : ""), "Send to lock");
    if (!ok) return;
  }
  await busy([...f.querySelectorAll("button")], async () => {
    try {
      const r = await call("welcome", v.id, form, action, days, f.start.value, f.end.value, send, entry);
      showOutput($("welcome-out"), r);
    } catch (e) { $("welcome-error").textContent = e.message; }
  });
});

$("rain-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const v = current();
  const on = f.on.value === "1";
  $("rain-error").textContent = "";
  if (!ev.submitter) return;             // Enter in a field: sending to a lock takes the button
  const ok = await confirmDialog(`Send to ${doorLabel(v)}?`,
    `This turns Rain Mode ${on ? "on" : "off"}.`, "Send to lock");
  if (!ok) return;
  await busy([...f.querySelectorAll("button")], async () => {
    try {
      showOutput($("rain-out"), await call("rain", v.id, on, true));
    } catch (e) { $("rain-error").textContent = e.message; }
  });
});

// ── maintenance: each door's health, and the jobs that keep a lock reachable ─
// The list is drawn from what's already known (the last status checks and the
// Lockly account): opening this view wakes no lock. Each job says what it wakes.

const ROUTE_WORD = { hub: "Hub", wifi: "Wi-Fi", bluetooth: "No hub" };

function clockWord(v) {
  if (!v.clockKnown) return ["", ""];
  if (v.clockOffset == null) return ["not set", "warn"];
  return Math.abs(v.clockOffset) < CLOCK_WARN_S ? ["right", ""] : [offsetWords(v.clockOffset), "warn"];
}

function renderMaint() {
  // A value not known yet (nothing has asked the lock) is a dot to the eye and "not known" to a screen reader.
  const cell = (label, text, cls = "") => (text
    ? h("span", { class: `m-cell ${cls}`.trim(), "data-label": label }, text)
    : h("span", { class: "m-cell none", "data-label": label },
      h("span", { "aria-hidden": "true" }, "·"), h("span", { class: "sr-only" }, "not known")));
  $("maint-rows").replaceChildren(...locks.map((v) => {
    const [clock, clockCls] = clockWord(v);
    const lowBatt = v.lowBattery || (v.battery != null && v.battery <= 20);
    const down = v.state === "asleep" && v.route !== "bluetooth";      // a door with no hub is never asked
    return h("li", {}, h("button", {
      class: "maint-row", type: "button", "aria-current": v.id === currentId ? "true" : "false",
      onclick: () => { selectLock(v.id); setPane("maint-ws", "detail"); },
    },
      h("span", { class: "m-name" }, doorLabel(v), v.hidden ? h("span", { class: "muted" }, " (hidden)") : null),
      cell("Reached", ROUTE_WORD[v.route] + (down ? (v.offline ? ", offline" : ", no answer") : ""), down ? "warn" : ""),
      cell("Battery", v.battery != null ? `${v.battery}%` : v.lowBattery ? "low" : "", lowBatt ? "warn" : ""),
      cell("Clock", clock, clockCls),
      cell("Time zone", v.takesTimeZone ? v.timeZone : "", v.timeZone === "not set" ? "warn" : ""),
      cell("Firmware", v.firmware)));
  }));
  renderMaintDoor();
}

// The jobs for the chosen door: only what this door has and this build can do.
// A job that can't run here is said in a line, not offered as a button that fails.
function renderMaintDoor() {
  const v = current();
  $("maint-empty").hidden = !!v;
  $("maint-body").hidden = !v;
  $("maint-cli").hidden = !caps.commandLine;
  if (!v) return;
  $("m-name").textContent = doorLabel(v);
  $("m-sub").replaceChildren([v.model, v.hub && `hub ${v.hub}`].filter(Boolean).join(", ") + ". ",
    h("span", { class: "route" }, icon(v.route), `Reached ${ROUTE_TEXT[v.route]}.`));
  const remote = v.route !== "bluetooth";
  const why = noBluetooth();
  $("diag-about").hidden = $("diag-on").hidden = !remote;
  $("diag-none").hidden = remote;
  $("ble-on").hidden = !!why;
  $("ble-none").hidden = !why;
  $("ble-none").textContent = why ? `Setting the clock over Bluetooth isn't available here. ${why}` : "";
  renderTimeZone(v);
  $("job-wifi").hidden = !v.hasWifi;
  $("wifi-on").hidden = !!why;
  $("wifi-none").hidden = !why;
  $("wifi-none").textContent = why ? `Wi-Fi setup goes over Bluetooth, which isn't available here. ${why}` : "";
  $("wifi-about").textContent = account.server
    ? "Give the lock a Wi-Fi network over Bluetooth, next to the door. Your server supplies the MQTT server it "
      + "connects to, unless another is given below."
    : "Give the lock a Wi-Fi network over Bluetooth, next to the door, as the Lockly app's Wi-Fi setup does. "
      + "Lockly supplies the server it connects to; to move the lock to a private server, give that server's "
      + "MQTT host and port below.";
  $("wifi-mqtt-about").textContent = "For a private locklyctl-mqtt server: the host and port its MQTT listener has "
    + "(8883 with TLS, 1883 without; a lock takes up to 4 digits). Leave both empty to use the one "
    + `${account.server ? "your server" : "Lockly"} gives.`;
  // Firmware comes from Lockly: a private server has none to offer (Api: firmwareKind is null there).
  $("fw-on").hidden = !!account.server;
  $("fw-none").hidden = !account.server;
  $("fw-none").textContent = account.server
    ? "Firmware comes from Lockly's servers, so it isn't offered while signed in to a private server." : "";
}

// A different door: the last door's results don't belong to it.
function resetJobs() {
  for (const id of ["diag-out", "ble-out", "tz-out", "wifi-out", "fw-out"]) $(id).replaceChildren();
  const f = $("wifi-form");
  f.hidden = true;
  f.mqttHost.value = f.mqttPort.value = "";
  $("wifi-mqtt").open = false;
}


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

// ── Bluetooth, from this device to a lock next to it ──────────────────

$("btn-ble-time").addEventListener("click", async (ev) => {
  const v = current();
  const out = $("ble-out");
  out.replaceChildren(h("p", { class: "muted" }, "Looking for the lock over Bluetooth…"));
  await busy(ev.currentTarget, async () => {
    try {
      const view = await call("bluetooth_set_time", v.id);
      updateLock(view);
      const clock = clockText(view.clockKnown, view.clockOffset);
      const fix = clockFixText(view.clockFix);
      out.replaceChildren(h("p", { class: clock ? "form-error" : "ok" },
        clock ? `Reached it over Bluetooth, but ${clock.charAt(0).toLowerCase()}${clock.slice(1)}.`
          : "Reached it over Bluetooth and gave it the time. Its clock is right."),
        fix ? h("p", { class: "fine" }, fix) : null);
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

// ── time zone, given to the lock as the app does after setup or an update ─

$("btn-tz").addEventListener("click", async (ev) => {
  const v = current();
  const out = $("tz-out");
  out.replaceChildren(h("p", { class: "muted" }, "Giving the lock its time zone…"));
  await busy(ev.currentTarget, async () => {
    try {
      const r = await call("set_time_zone", v.id);
      updateLock(r.lock);
      out.replaceChildren(h("p", { class: r.ok ? "ok" : "form-error" }, r.ok
        ? `Gave it ${r.zone}${r.source === "computer" ? ` (${here()}'s)` : ""}${r.via === "bluetooth" ? " over Bluetooth" : ""}, then the time.`
        : `It didn't take it: ${r.text}.`));
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

// ── Wi-Fi, given to the lock over Bluetooth ────────────────────────────

// Per network: "saved" (locklyctl has its password), "system" (this device
// has the network saved) or "", and whether this device is on it, or on the
// same router's other band name. Passwords themselves never reach the page.
let wifiKnown = {}, wifiHere = {};
const wifiMark = (known) => ({ saved: "password saved", system: `saved on ${here()}` })[known];

function wifiLabel(o) {
  if (!o.value) return o.dataset.label;
  const marks = [wifiMark(wifiKnown[o.value]), wifiHere[o.value]].filter(Boolean);
  return o.dataset.label + marks.map((m) => ` · ${m}`).join("");
}

function showWifiPassword() {
  const f = $("wifi-form"), k = wifiKnown[f.ssid.value] || "";
  for (const o of f.ssid.options) o.textContent = wifiLabel(o);
  f.password.placeholder = k ? "Leave empty to use the saved password" : "";
  $("wifi-pw-note").textContent = !f.ssid.value ? `Networks a lock has joined before, and the one ${here()} is on, come first.`
    : k === "saved" ? "locklyctl has this network's password saved."
    : k === "system" ? `${Here()} has this network saved: leave the password empty to use it `
      + "(on a Mac, macOS asks for an administrator's password first)."
      : "The password is saved once the lock has joined the network with it.";
  $("wifi-forget").hidden = k !== "saved";
}
$("wifi-form").ssid.addEventListener("change", showWifiPassword);

$("wifi-forget").addEventListener("click", async (ev) => {
  const ssid = $("wifi-form").ssid.value;
  await busy(ev.currentTarget, async () => {
    try {
      const r = await call("forget_wifi_password", ssid);
      wifiKnown[ssid] = "";
      showWifiPassword();
      toast(r.forgotten ? `Forgot the saved password for ${ssid}.` : `No password was saved for ${ssid}.`);
    } catch (e) { toast(e.message, true); }
  });
});

$("btn-wifi-scan").addEventListener("click", async (ev) => {
  const v = current();
  const out = $("wifi-out"), form = $("wifi-form");
  out.replaceChildren(h("p", { class: "muted" }, "Asking the lock over Bluetooth which networks it sees…"));
  form.hidden = true;
  await busy(ev.currentTarget, async () => {
    try {
      // In the order to offer them (saved, this device's, then signal); one
      // is preselected only when suggested, else the user picks.
      const nets = await call("bluetooth_wifi_scan", v.id);
      wifiKnown = Object.fromEntries(nets.map((n) => [n.ssid, n.known || ""]));
      wifiHere = Object.fromEntries(nets.map((n) => [n.ssid,
        n.connected ? `${here()} is on it` : n.twinOf ? `${here()} is on ${n.twinOf}` : ""]));
      const pick = nets.find((n) => n.suggested);
      form.ssid.replaceChildren(
        ...(pick ? [] : [h("option", { value: "", disabled: true, "data-label": "Choose a network" })]),
        ...nets.map((n) => h("option", { value: n.ssid, "data-label": `${n.ssid} (${n.rssi} dBm)` })));
      form.ssid.value = pick ? pick.ssid : "";
      form.hidden = !nets.length;
      showWifiPassword();
      out.replaceChildren(h("p", { class: "muted" }, nets.length ? "" : "The lock sees no networks."));
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

$("wifi-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const v = current();
  const f = ev.currentTarget, out = $("wifi-out");
  const host = f.mqttHost.value.trim(), port = f.mqttPort.value.trim();
  const given = account.server ? "your server's" : "Lockly's";
  const aim = host || port ? `${host || `${given} host`}:${port || `${given} port`}` : "";
  const ok = await confirmDialog(`Give ${doorLabel(v)} the network ${f.ssid.value}?`,
    `This replaces the Wi-Fi network saved on the lock. Keep ${here()} next to the door; it takes up to a minute and a half.`
    + (aim ? ` It also points the lock at the MQTT server ${aim} instead of the one ${account.server ? "your server"
      : "Lockly"} gives, so ${account.server ? "your server" : "Lockly's app and server"} can't reach it until it's `
      + "given Wi-Fi again without it. Whether a lock takes a server other than Lockly's hasn't been tried on a "
      + "lock yet: try it on a door you can reach over Bluetooth." : ""),
    "Give it");
  if (!ok) return;
  out.replaceChildren(h("p", { class: "muted" }, `Saving ${f.ssid.value} on the lock and waiting for it to join…`));
  await busy(f.querySelector("button[type=submit]"), async () => {
    try {
      const ssid = f.ssid.value;
      const r = await call("bluetooth_wifi_setup", v.id, ssid, f.password.value, "", host, port);
      f.password.value = "";
      updateLock(r.lock);
      wifiKnown[ssid] = r.known;
      showWifiPassword();
      const mqtt = r.mqttHost || r.mqttPort
        ? `It was given the MQTT server ${r.mqttHost || `${given} host`}:${r.mqttPort || `${given} port`}. `
          + "A check shows whether it reaches it." : "";
      fill(out, h("p", { class: r.ok ? "ok" : "form-error" }, r.ok ? `Connected to ${ssid}.` : `It didn't connect: ${r.text}.`),
        mqtt ? h("p", { class: "fine" }, mqtt) : null,
        r.note ? h("p", { class: "fine" }, r.note) : null);
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
        h("td", { class: "muted" }, p.byBluetooth ? `Bluetooth, from ${here()}`
          : p.byApp ? "Lockly app" : p.byPhone ? "Lockly app, at the door" : "Over Wi-Fi")));
      const install = r.install;
      const byPhone = r.parts.filter((p) => p.newer && p.byPhone && !p.byBluetooth).map((p) => p.name);
      const chip = r.parts.find((p) => p.newer && p.byBluetooth);
      fill(out,
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
            h("button", { class: "btn btn-ink", type: "button", onclick: (ev) => installFirmware(v, install, ev.currentTarget) },
              icon("firmware"), "Install over Wi-Fi"))
          : (r.kind === "multi" && install.blocked ? h("p", { class: "fine" }, install.blocked) : null),
        byPhone.length ? h("p", { class: "fine" }, `${byPhone.join(", ")}: install with the Lockly app, at the door.`)
          : null,
        chip && !noBluetooth() ? h("div", { class: "fw-install" },
          h("p", {}, `Over Bluetooth from ${here()} this sends Bluetooth chip ${chip.offered}.`),
          h("button", { class: "btn btn-ink", type: "button", onclick: () => installBluetoothChip(v, chip) },
            icon("bluetooth"), "Install over Bluetooth")) : null,
        chip && noBluetooth() ? h("p", { class: "fine" },
          `Bluetooth chip ${chip.offered} is sent over Bluetooth, which isn't available here. ${noBluetooth()}`) : null);
    } catch (e) { out.replaceChildren(h("p", { class: "form-error" }, e.message)); }
  });
});

// Each part's row. Its files are downloaded and checked here first; then, as
// the lock's progress messages don't say which part it's on, every part reads
// "Sending" until it restarts and reports its versions.
const FW_PART_STATE = { files: "Getting the files", sending: "Sending", restarting: "Waiting for the lock",
  done: "Installed", behind: "Not installed", unconfirmed: "Not confirmed", notSent: "Not sent" };

async function installFirmware(v, install, btn) {
  const list = install.parts.map((p) => `${p.name} ${p.current || "?"} → ${p.version}`).join(", ");
  // The restart can leave the lock without its clock if its Wi-Fi doesn't come
  // back, and then only Bluetooth can give it the time: say which applies here.
  let ble = { inRange: false, detail: noBluetooth() ? "no Bluetooth here" : "" };
  if (!noBluetooth()) {
    const looking = h("p", { class: "muted" }, `Checking whether ${here()} can reach it over Bluetooth…`);
    $("fw-out").append(looking);
    await busy(btn, async () => {
      try { ble = await call("bluetooth_check", v.id); } catch (e) { ble.detail = e.message; }
    });
    looking.remove();
  }
  const bleNote = ble.inRange
    ? ` ${Here()} can reach it over Bluetooth: if its Wi-Fi doesn't come back after the restart, `
      + "it gives the lock the time over Bluetooth."
    : ` Warning: ${here()} can't reach it over Bluetooth (${(ble.detail || "not in range").replace(/\.$/, "")}). If its Wi-Fi `
      + "doesn't come back after the restart, its clock can't be set from here, and people on a schedule may "
      + "not get in until someone gives it the time over Bluetooth at the door, for example with the Lockly app.";
  const ok = await confirmDialog(`Install new firmware on ${doorLabel(v)}?`,
    `${list}. The lock downloads the files and installs them itself, then restarts. Leave the door alone `
    + "until this says it's done; it can take several minutes." + bleNote, "Install");
  if (!ok || busyNow(v)) return;
  await followInstall(v, () => call("firmware_install", v.id, install.parts, !!ble.inRange));
}

// The Bluetooth chip's own update, sent over Bluetooth from this device.
async function installBluetoothChip(v, chip) {
  const ok = await confirmDialog(`Update the Bluetooth chip on ${doorLabel(v)}?`,
    `Bluetooth chip ${chip.current || "?"} → ${chip.offered}, sent from ${here()} over Bluetooth. `
    + `Keep ${here()} next to the door until this says it's done; it takes several minutes.`, "Update");
  if (!ok || busyNow(v)) return;
  await followInstall(v, () => call("bluetooth_chip_install", v.id));
}

// Shows an install's progress until it ends; ``start`` begins it and returns its first status.
async function followInstall(v, start) {
  pending.set(v.id, "firmware");
  refreshControls(v.id);
  const show = (st) => {
    if (v.id !== currentId || activeView !== "maint") return;
    const clock = st.clockKnown ? clockText(true, st.clockOffset) : null;
    fill($("fw-out"),
      h("p", {}, `${doorLabel(v)}:`),
      h("ul", { class: "fw-parts" }, st.parts.map((p) =>
        h("li", { "data-state": p.state },
          h("span", { class: "fw-part-name" }, p.name),
          h("span", { class: "fw-part-ver" }, `${p.current || "?"} → ${p.version}`),
          h("span", { class: "fw-part-state" }, FW_PART_STATE[p.state] || p.state,
            p.state === "behind" && p.now ? ` (still ${p.now})` : null)))),
      h("p", { class: st.ok === false ? "form-error" : (st.ok ? "ok" : "muted") },
        st.ok === false ? st.error : (st.ok ? "Installed, and the lock reports the new versions." : st.text)),
      st.warning ? h("p", { class: "form-error" }, st.warning) : null,
      st.running && !st.parts.some((p) => p.state === "files")
        ? h("p", { class: "muted" }, "Started: locklyctl stays open and nothing else goes to this door "
          + "until it has finished.") : null,
      st.clockKnown ? h("p", { class: clock ? "form-error" : "ok" },
        clock ? `${clock}: people on a schedule may not get in until it's set.` : "The lock's clock is right.") : null);
  };
  try {
    let st = await start();
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

// Doors and People are for every day; Maintenance is for the locks themselves;
// Settings is for this app. Choosing the view you're in goes back to its list (phones).
const VIEWS = { doors: "doors-ws", people: "people-ws", maint: "maint-ws", settings: "settings-ws" };

function showView(view) {
  if (view === activeView && !$(VIEWS[view]).hidden && $(VIEWS[view]).dataset.pane) setPane(VIEWS[view], "list");
  activeView = view;
  for (const [name, ws] of Object.entries(VIEWS)) {
    $(ws).hidden = name !== view;
    $(`view-${name}`).setAttribute("aria-pressed", String(name === view));
  }
  if (view === "doors" && current() && activeTab === "access") loadAccess();   // People may have changed it
  if (view === "people") {
    mountGrant("people");
    if (peopleLoaded) renderPerson(); else loadPeople();
  }
  if (view === "maint") renderMaint();
}
for (const name of Object.keys(VIEWS)) $(`view-${name}`).addEventListener("click", () => showView(name));

function renderAbout() {
  $("about-version").textContent = `locklyctl ${caps.version}`.trim();
  // On a phone the log is inside the app's own storage, where nothing else can open it.
  $("about-log").textContent = caps.device === "computer" && caps.logPath ? `Log file: ${caps.logPath}` : "";
}

// On a private server, people, PINs and schedules live in its own console (locklyctl-mqtt's
// management API): the page says where, instead of asking Lockly's cloud for them.
function serverPeopleNote() {
  return h("p", { class: "muted" }, "People, PINs and schedules on a private server are kept in its own console, at ",
    h("code", {}, `${account.server}/`), ". Add, change and remove them there.");
}

async function loadPeople() {
  for (const id of ["add-person", "people-search"]) $(id).closest(".field, button").hidden = !!account.server;
  if (account.server) {
    people = []; personKey = null; peopleLoaded = true;
    renderPeople();
    renderPerson();
    return;
  }
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
  if (account.server) {                    // kept by the server's console, not Lockly's cloud
    $("people-list").replaceChildren(h("li", {}, serverPeopleNote()));
    return;
  }
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
      onclick: () => {
        newPersonMode = false; $("p-merge").hidden = false; personKey = p.key;
        renderPeople(); renderPerson(); setPane("people-ws", "detail");
      },
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

  $("p-doors").hidden = false;
  fill($("p-doors"), accessTable("Door", p.doors.map((d) => ({
    p, d, reload: () => reloadPeople(p.key),
    first: h("div", { class: "door-cell" },
      d.doorUnit ? h("span", { class: "mini-unit" }, d.doorUnit) : null,
      h("span", {}, d.doorUnit ? d.doorRest : d.doorName, " ", h("span", { class: "muted" }, d.doorModel))),
  }))));
  if (grantFor === "people") setupGrant(p);

  // Merge controls: suggest records with the same name first
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

// A person's doors (People) or a door's people (its Access tab), one row each:
// ``items`` are {first: the first cell, p: the person, d: their access on one door,
// reload: what to show again after a change}.
function accessTable(firstHead, items) {
  return h("table", { class: "access-table" },
    h("thead", {}, h("tr", {}, h("th", { scope: "col" }, firstHead), h("th", { scope: "col" }, "Role"),
      h("th", { scope: "col" }, "When"), h("th", { scope: "col" }, "Can unlock with"),
      h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Changes")))),
    h("tbody", {}, items.map(({ first, p, d, reload }) => {
      const creds = credSummary(d);
      return h("tr", {},
        h("td", {}, first),
        h("td", { class: d.role === "owner" ? "role-owner" : "" }, d.role.charAt(0).toUpperCase() + d.role.slice(1)),
        h("td", {}, d.access, d.suspended && h("div", { class: "off" }, "Paused"),
          !d.enabled && h("div", { class: "off" }, "Switched off")),
        h("td", {}, h("div", { class: "creds" },
          creds.length ? creds.map((c) => h("span", { class: "cred" }, c)) : h("span", { class: "cred none" }, "Nothing yet"),
          d.twoStep && h("span", { class: "cred" }, "Needs two"))),
        h("td", {}, rowActions(p, d, reload)));
    })));
}

function rowActions(p, d, reload) {
  const cell = h("div", { class: "row-actions" });
  const detailsBtn = h("button", { class: "btn btn-quiet", type: "button", "aria-expanded": "false" }, "Details");
  const pinBtn = h("button", { class: "btn btn-quiet", type: "button" }, d.hasPin ? "Change PIN" : "Add PIN");
  const pauseBtn = h("button", { class: "btn btn-quiet", type: "button",
    title: d.suspended ? "Let them in again, on the schedule they had."
      : "Stop them getting in for now. Their PIN, fingerprints and face stay on the lock." },
    d.suspended ? "Resume" : "Pause");
  const removeBtn = h("button", { class: "btn btn-quiet btn-danger", type: "button" }, "Remove");
  const all = [detailsBtn, pinBtn, pauseBtn, removeBtn];

  detailsBtn.addEventListener("click", () => toggleDetails(detailsBtn, p, d, reload));
  pinBtn.addEventListener("click", () => {
    const input = h("input", { type: "password", inputmode: "numeric", maxlength: "8", "aria-label": `New PIN for ${p.name} on ${d.doorName}`, autocomplete: "off" });
    const save = h("button", { class: "btn btn-ink", type: "button" }, "Save PIN");
    const cancel = h("button", { class: "btn btn-quiet", type: "button" }, "Cancel");
    const editor = h("div", { class: "pin-edit" }, input, save, cancel);
    const back = [...cell.childNodes];
    cell.replaceChildren(editor);
    input.focus();
    const close = () => { cell.replaceChildren(...back); pinBtn.focus(); };
    cancel.addEventListener("click", close);
    const submit = async () => {
      await busy([save, cancel], async () => {
        toast(`Writing the new PIN to ${d.doorName}…`);
        try {
          await call("change_pin", d.lockId, d.userId, input.value);
          toast(`New PIN saved on ${d.doorName}.`);
          await reload();
        } catch (e) { toast(e.message, true); }
      });
    };
    save.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); if (e.key === "Escape") close(); });
  });
  pauseBtn.addEventListener("click", async () => {
    const ok = d.suspended
      ? await confirmDialog(`Let ${p.name} into ${d.doorName} again?`,
        "They get in on the schedule they had. The lock wakes, so it beeps.", "Resume")
      : await confirmDialog(`Pause ${p.name} on ${d.doorName}?`,
        "They can't open this door until you resume them. Their PIN, fingerprints and face stay on the lock, "
        + "and other doors aren't affected. The lock wakes, so it beeps.", "Pause");
    if (!ok) return;
    await busy(all, async () => {
      toast(`${d.suspended ? "Resuming" : "Pausing"} ${p.name} on ${d.doorName}…`);
      try {
        if (d.suspended) {
          const r = await call("resume_person", d.lockId, d.userId);
          toast(r.source === "cloud"
            ? `${p.name} can open ${d.doorName} again. Their schedule was rebuilt from Lockly's record, as none was saved on this device: check it.`
            : `${p.name} can open ${d.doorName} again.`, r.source === "cloud");
        } else {
          const r = await call("suspend_person", d.lockId, d.userId);
          toast(r.kept ? `${p.name} is paused on ${d.doorName}.`
            : `${p.name} is paused on ${d.doorName}, but the lock reports different fingerprints or faces for them now: check Details.`, !r.kept);
        }
        await reload();
      } catch (e) { toast(e.message, true); }
    });
  });
  removeBtn.addEventListener("click", async () => {
    const ok = await confirmDialog(`Remove ${p.name} from ${d.doorName}?`,
      "Their PIN, fingerprints and face on this door stop working. Other doors aren't affected.", "Remove from door");
    if (!ok) return;
    await busy(all, async () => {
      toast(`Removing ${p.name} from ${d.doorName}…`);
      try {
        // A Visage or Vision keeps people on the lock itself; other locks only have Lockly's record.
        await call(d.doorAccessUsers ? "remove_from_door" : "delete_guest", d.lockId, d.userId);
        toast(`${p.name} removed from ${d.doorName}.`);
        await reload();
      } catch (e) { toast(e.message, true); }
    });
  });
  // The owner's PIN and membership belong to the Lockly app; only their
  // fingerprints and faces are managed here.
  if (d.doorRoute === "bluetooth") {           // no hub: nothing can reach this lock remotely
    for (const b of [pinBtn, pauseBtn, removeBtn]) { b.disabled = true; b.title = "This door has no hub, so it can only be changed over Bluetooth."; }
  } else if (!d.doorAccessUsers) {             // people aren't kept on this lock: no record there to change
    pinBtn.disabled = true;
    pinBtn.title = "This lock model's PINs can't be changed from here yet: remove the person and add them again.";
  }
  cell.append(...(d.role === "owner" ? [detailsBtn]
    : [detailsBtn, pinBtn, d.doorAccessUsers ? pauseBtn : null, removeBtn].filter(Boolean)));
  return cell;
}

async function toggleDetails(btn, p, d, reload) {
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
            await reload();
          } catch (e) { toast(e.message, true); }
        });
      });
      return h("li", {}, h("span", {}, c.name || label.charAt(0).toUpperCase() + label.slice(1), " ",
        h("span", { class: "muted" }, `${label}, slot ${c.slot ?? "?"}`)), rm);
    };
    // The PIN as Lockly's record has it, shown only when asked for.
    const pinItem = () => {
      const val = h("span", { class: "pin-val" }, "hidden");
      const show = h("button", { class: "btn btn-quiet", type: "button", "aria-pressed": "false" }, "Show");
      show.addEventListener("click", () => {
        const on = show.getAttribute("aria-pressed") !== "true";
        show.setAttribute("aria-pressed", String(on));
        show.textContent = on ? "Hide" : "Show";
        val.textContent = on ? d.pin : "hidden";
      });
      return h("li", {}, h("span", {}, "PIN ", val), show);
    };
    const items = [
      d.pin ? pinItem() : null,
      ...det.fingerprints.map((c) => item("fingerprint", "fingerprint", c)),
      ...det.faces.map((c) => item("face", "face", c)),
      ...det.cards.map((c) => item("card", "card", c)),
    ].filter(Boolean);
    td.replaceChildren(items.length ? h("ul", { class: "cred-list" }, items)
      : h("p", { class: "muted" }, det.hasPin ? "Only a PIN on this door." : "No PIN, fingerprints, faces or cards on this door."));
  } catch (e) { td.replaceChildren(h("p", { class: "form-error" }, e.message)); }
}

// ── giving access: one form, in People (any door) and in a door's Access tab (that door) ──

let newPersonMode = false;
let grantFor = "people";              // where the form is now: "people" or "door"
const HOURS_KEY = { 4: "4h", 24: "24h", 72: "72h", 168: "1w" };     // Api.add_guest's durations

(() => {
  const box = $("grant-days");
  DAYS.forEach(([key, label], i) => box.append(
    h("label", {}, h("input", { type: "checkbox", name: "gday", value: key, checked: i < 5 }), h("span", {}, label))));
  $("grant-kind").addEventListener("change", syncGrant);
  $("grant-form").elements.door.addEventListener("change", syncGrant);
})();

// Put the form where it's used now, set up for that place.
function mountGrant(where) {
  grantFor = where;
  $(where === "door" ? "access-slot" : "grant-slot").append($("grant-form"));
  if (where === "door") setupGrant(null);
}

function setupGrant(p) {
  const f = $("grant-form");
  const forDoor = grantFor === "door";
  const have = new Set(p ? p.doors.map((d) => d.lockId) : []);
  const options = forDoor ? [current()].filter(Boolean) : locks.filter((l) => !have.has(l.id));
  f.elements.door.replaceChildren(...options.map((l) => h("option", { value: l.id, disabled: l.route === "bluetooth" },
    doorLabel(l) + (l.route === "bluetooth" ? " (no hub: Bluetooth only)" : ""))));
  const firstReachable = options.find((l) => l.route !== "bluetooth");
  if (firstReachable) f.elements.door.value = firstReachable.id;
  const naming = forDoor || newPersonMode;
  $("grant-name-field").hidden = !naming;
  $("grant-door-field").hidden = forDoor;
  $("grant-title").textContent = forDoor ? "Give someone this door" : newPersonMode ? "Add a person" : "Give access to another door";
  $("grant-submit").textContent = naming ? "Add person" : "Give access";
  $("grant-submit").disabled = !firstReachable;
  $("grant-error").textContent = firstReachable ? ""
    : forDoor ? "This door has no hub, so people can only be added to it over Bluetooth."
      : options.length ? "The doors left have no hub, so people can only be added to them over Bluetooth."
        : "This person can already open every door.";
  const knownPin = p && (p.doors.find((d) => d.pin) || {}).pin;
  f.elements.pin.value = knownPin || "";
  if (naming) f.elements.name.value = "";
  syncGrant();
}

// A Visage or Vision keeps people on the lock itself and takes every kind of schedule.
// Other locks only have Lockly's record: always, or for a while.
function syncGrant() {
  const f = $("grant-form");
  const door = locks.find((l) => l.id === f.elements.door.value);
  const onLock = !door || door.accessUsers;
  for (const r of f.querySelectorAll("input[name=kind]")) {
    const offered = onLock || r.value === "always" || r.value === "hours";
    r.closest("label").hidden = !offered;
    if (!offered && r.checked) f.querySelector("input[name=kind][value=always]").checked = true;
  }
  const k = f.elements.kind.value;
  $("grant-hours").hidden = k !== "hours";
  $("grant-weekly").hidden = k !== "weekly";
  $("grant-dates").hidden = k !== "dates";
  $("grant-fine").textContent = onLock
    ? "The PIN is written to the lock straight away. Faces and fingerprints stay with the lock they were enrolled on; enrol them again at a new door."
    : "This lock doesn't keep people on the lock itself: Lockly's cloud takes the PIN straight away, and whether the lock "
      + "picks it up at once isn't confirmed, so try it at the door.";
}

$("add-person").addEventListener("click", () => {
  newPersonMode = true;
  personKey = null;
  renderPeople();
  $("person-empty").hidden = true;
  $("person-body").hidden = false;
  $("p-name").textContent = "New person";
  $("p-sub").textContent = "Choose a door and how they get in. You can add more doors afterwards.";
  $("p-doors").hidden = true;
  $("p-merge").hidden = true;
  mountGrant("people");
  setupGrant(null);
  setPane("people-ws", "detail");
  $("grant-form").elements.name.focus();
});

$("grant-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.currentTarget;
  const forDoor = grantFor === "door";
  const p = forDoor ? null : people.find((x) => x.key === personKey);
  const name = forDoor || newPersonMode ? f.elements.name.value.trim() : (p && p.name);
  const kind = f.elements.kind.value;
  const access = { kind };
  if (kind === "weekly") {
    access.days = [...f.querySelectorAll("input[name=gday]:checked")].map((x) => x.value);
    access.start = f.elements.start.value; access.end = f.elements.end.value;
  } else if (kind === "dates") {
    access.from = f.elements.from.value; access.to = f.elements.to.value;
  } else if (kind === "hours") {
    access.hours = Number(f.elements.hours.value);
  }
  const lockId = f.elements.door.value;
  const door = locks.find((l) => l.id === lockId);
  const pin = f.elements.pin.value.trim();
  $("grant-error").textContent = "";
  if (!name) { $("grant-error").textContent = "Give the person a name."; return; }
  if (!door) { $("grant-error").textContent = "Choose a door."; return; }
  if (!door.accessUsers && !pin) { $("grant-error").textContent = "On this lock a person needs a PIN."; return; }
  const ok = await confirmDialog(`Give ${name} access to ${doorLabel(door)}?`,
    !door.accessUsers ? "Lockly's cloud takes the PIN straight away. Try it at the door."
      : pin ? "The PIN starts working on that door as soon as the lock confirms it."
        : "They'll have no PIN on that door until you add one.",
    "Give access");
  if (!ok) return;
  await busy($("grant-submit"), async () => {
    toast(`Adding ${name} to ${doorLabel(door)}…`);
    try {
      if (door.accessUsers) await call("add_to_door", lockId, name, pin, access);
      else await call("add_guest", lockId, name, pin, kind === "always" ? "permanent" : HOURS_KEY[access.hours]);
      toast(`${name} can now open ${doorLabel(door)}.`);
      if (forDoor) { await doorAccessChanged(); return; }
      newPersonMode = false;
      $("p-merge").hidden = false;
      await reloadPeople(p ? p.key : `name:${name.toLowerCase()}`);
    } catch (e) { $("grant-error").textContent = e.message; }
  });
});

// A backend that can't even say what it can do: say so, rather than sit at "Signing in…".
boot().catch((e) => { $("boot-msg").textContent = e.message; $("boot").removeAttribute("aria-busy"); });
