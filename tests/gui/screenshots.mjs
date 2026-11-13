// Render the README screenshots from the mock data (no account, no locks):
//   python3 -m http.server 8765 --bind 127.0.0.1 &      # from the repo root
//   node tests/gui/screenshots.mjs [chrome-binary]
// Drives headless Chrome over the DevTools protocol so phone shots get real
// mobile emulation (headless windows can't be narrower than 500px).
// Also fails if any page is wider than its screen, which would scroll sideways,
// or if a door plate's status runs past the plate.
import { spawn } from "node:child_process";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const chrome = process.argv[2] || "google-chrome";
const base = "http://127.0.0.1:8765/locklyctl/gui/web/index.html?mock=/tests/gui/mock.js";
const out = new URL("../../docs/screenshots/", import.meta.url).pathname;
const desktop = { width: 1280, height: 800, deviceScaleFactor: 1, mobile: false };
const phone = { width: 390, height: 844, deviceScaleFactor: 2, mobile: true };
const shots = [
  ["desktop-doors", desktop, "", "light"],
  ["desktop-people", desktop, "person-detail", "light"],
  ["desktop-schedules", desktop, "schedules", "dark"],
  ["phone-doors", phone, "", "light"],
  ["phone-door", phone, "guests", "light"],
  ["phone-people", phone, "person-detail", "dark"],
];

// Visible elements poking past the right edge, ignoring content inside a
// sideways-scrolling strip (the tabs), which is meant to scroll. Scroll
// containers clip, and on a phone overflow widens the layout viewport, so
// neither scrollWidth nor innerWidth can be trusted: compare to the screen.
const overflow = (width) => `JSON.stringify([...document.querySelectorAll("body *")].filter((e) => {
  if (!e.offsetParent || e.getBoundingClientRect().right <= ${width} + 1) return false;
  return !e.closest(".tabs");
}).slice(0, 6).map((e) => e.tagName.toLowerCase() + (e.id ? "#" + e.id : "") + (e.className ? "." + String(e.className).split(" ")[0] : "")))`;

// Plate status lines that run into the plate's padding, which the screen-edge
// check above can't see (a plate is narrower than the screen).
const cramped = `JSON.stringify([...document.querySelectorAll(".plate-word")].filter((w) => {
  if (!w.offsetParent) return false;            // not shown (e.g. the People view is open)
  const p = w.closest(".plate"), cs = getComputedStyle(p);
  return w.getBoundingClientRect().right > p.getBoundingClientRect().right - parseFloat(cs.paddingRight) - parseFloat(cs.borderRightWidth) + 0.5;
}).slice(0, 6).map((w) => w.closest(".plate").getAttribute("aria-label")))`;

const profile = mkdtempSync(join(tmpdir(), "locklyctl-shots-"));
const proc = spawn(chrome, ["--headless", "--disable-gpu", "--hide-scrollbars", "--remote-debugging-port=0",
  `--user-data-dir=${profile}`, "about:blank"], { stdio: ["ignore", "ignore", "pipe"] });
const wsUrl = await new Promise((resolve, reject) => {
  let buf = "";
  proc.stderr.on("data", (d) => {
    buf += d;
    const m = buf.match(/DevTools listening on (ws:\/\/\S+)/);
    if (m) resolve(m[1]);
  });
  proc.on("exit", () => reject(new Error("Chrome exited:\n" + buf)));
});

const browser = new WebSocket(wsUrl);
await new Promise((r) => browser.addEventListener("open", r, { once: true }));
let nextId = 0;
const pending = new Map();
const events = [];
browser.addEventListener("message", (e) => {
  const msg = JSON.parse(e.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
  } else if (msg.method) events.push(msg);
});
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const id = ++nextId;
  pending.set(id, { resolve, reject });
  browser.send(JSON.stringify({ id, method, params, sessionId }));
});
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let failed = false;
try {
  for (const [name, view, scene, scheme] of shots) {
    const { targetId } = await send("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
    const s = (m, p) => send(m, p, sessionId);
    await s("Page.enable");
    await s("Emulation.setDeviceMetricsOverride", view);
    if (view.mobile) await s("Emulation.setTouchEmulationEnabled", { enabled: true });
    await s("Emulation.setEmulatedMedia", { features: [{ name: "prefers-color-scheme", value: scheme }] });
    await s("Page.navigate", { url: `${base}&scene=${scene}` });
    await sleep(2500);                          // the mock's scripted clicks run at 700ms+
    const { result } = await s("Runtime.evaluate", {
      expression: overflow(view.width), returnByValue: true });
    const wide = JSON.parse(result.value);
    if (wide.length) { console.error(`${name}: wider than the screen: ${wide.join(", ")}`); failed = true; }
    const tight = JSON.parse((await s("Runtime.evaluate", { expression: cramped, returnByValue: true })).result.value);
    if (tight.length) { console.error(`${name}: runs past its plate: ${tight.join("; ")}`); failed = true; }
    const { data } = await s("Page.captureScreenshot", { format: "png" });
    writeFileSync(join(out, `${name}.png`), Buffer.from(data, "base64"));
    console.log(`${name}.png  ${view.width}x${view.height}@${view.deviceScaleFactor}x  ${scheme}`);
    await send("Target.closeTarget", { targetId });
  }
} finally {
  browser.close();
  proc.kill();
  await sleep(300);
  rmSync(profile, { recursive: true, force: true });
}
process.exit(failed ? 1 : 0);
