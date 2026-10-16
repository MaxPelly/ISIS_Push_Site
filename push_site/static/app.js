// The page: join with an invite, turn push on or off, pick topics and quiet
// hours, and read recent notifications. Text is only ever set with
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
let signedIn = false;  // since /api/me (or joining) succeeded, even before the app is shown
let topics = [];  // from /api/topics: [{id, label, instrument}]
let feedTab = "all", feedLoaded = false;
const TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const STANDALONE = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
// iOS Home Screen apps may not get Safari's cookies, so iPhones join from inside the installed app.
const INSTALL_FIRST = IOS && !STANDALONE;

// The service worker registration, or null if push can't work in this browser.
const worker = ("serviceWorker" in navigator && "PushManager" in window && "Notification" in window)
  ? navigator.serviceWorker.register("/sw.js").then(() => navigator.serviceWorker.ready).catch(() => null)
  : Promise.resolve(null);
let vapidKey = null;  // fetched ahead of time, so subscribing follows the tap as closely as possible

class ApiError extends Error {
  constructor(status, text) {
    super(text || `HTTP ${status}`);
    this.status = status;
  }
}

async function api(method, path, body) {
  const options = { method, headers: {} };
  if (method !== "GET") {  // the server only accepts changes as JSON from this origin
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body || {});
  }
  const resp = await fetch(path, options);
  if (!resp.ok) throw new ApiError(resp.status, await resp.text());
  return resp.status === 204 ? null : resp.json();
}

function el(tag, props = {}, ...children) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function message(text) {
  $("message").textContent = text || "";
  $("message").hidden = !text;
}

function hashParam(name) {
  return new URLSearchParams(location.hash.slice(1)).get(name);
}

function keyBytes(base64url) {
  const raw = atob(base64url.replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

function sameBytes(a, b) {
  return a.length === b.length && a.every((byte, i) => byte === b[i]);
}

// --- Joining ---------------------------------------------------------------

async function join(invite) {
  try {
    await api("POST", "/api/redeem", { invite });
    history.replaceState(null, "", "/");  // keep the used invite out of the address bar
    $("join").hidden = true;
    message("");
    await showApp(await api("GET", "/api/me"));
  } catch (e) {
    message(e.status === 403 ? "This invite is invalid, used up or expired. Ask for a new one." : e.message);
  }
}

function showJoin(invite) {
  $("join").hidden = false;
  $("join-ios").hidden = !INSTALL_FIRST;
  $("join-text").textContent = invite
    ? "You've been invited to get ISIS beam notifications on this device."
    : "To get notifications here, open or paste the invite link you were sent.";
  $("join-button").hidden = !invite || INSTALL_FIRST;
  $("join-paste").hidden = INSTALL_FIRST;  // joining in Safari would use up the invite outside the app
  $("join-button").onclick = () => join(invite);
}

function joinPasted(event) {
  event.preventDefault();
  const text = $("invite-link").value.trim();
  let invite = text;
  try {
    invite = new URLSearchParams(new URL(text).hash.slice(1)).get("invite") || text;
  } catch (e) {
    // Not a link: take it as the invite itself.
  }
  join(invite);
}

// --- Push on this device ---------------------------------------------------

async function refreshPush() {
  for (const id of ["push-on", "push-off", "install-help"]) $(id).hidden = true;
  const status = $("push-status");
  const registration = await worker;
  if (!registration) {
    $("install-help").hidden = !INSTALL_FIRST;
    status.textContent = INSTALL_FIRST ? "Install the app to get notifications." : "This browser can't receive push notifications.";
    return;
  }
  if (Notification.permission === "denied") {
    status.textContent = "Notifications are blocked for this site in your browser or system settings.";
    return;
  }
  vapidKey = vapidKey || keyBytes((await api("GET", "/api/vapid-key")).key);
  let sub = await registration.pushManager.getSubscription();
  const subKey = sub && sub.options.applicationServerKey;
  if (sub && subKey && !sameBytes(new Uint8Array(subKey), vapidKey)) {
    // The site's key changed: the old subscription can't be pushed to any more.
    await sub.unsubscribe();
    sub = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: vapidKey });
  }
  status.textContent = sub ? "Notifications are on." : "Notifications are off.";
  $(sub ? "push-off" : "push-on").hidden = false;
  if (sub) await api("PUT", "/api/subscription", sub.toJSON());  // keep the server in step
}

async function pushOn() {
  try {
    // Asked first, straight from the tap, as iOS requires.
    if ((await Notification.requestPermission()) === "granted") {
      const registration = await worker;
      vapidKey = vapidKey || keyBytes((await api("GET", "/api/vapid-key")).key);  // if the page's fetch failed
      await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: vapidKey });
    }
    await refreshPush();  // also sends the subscription to the server
  } catch (e) {
    message(`Couldn't turn on notifications: ${e.message}`);
  }
}

async function pushOff() {
  try {
    const sub = await (await worker).pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
    await api("DELETE", "/api/subscription");
    await refreshPush();
  } catch (e) {
    message(`Couldn't turn off notifications: ${e.message}`);
  }
}

// --- Preferences -----------------------------------------------------------

// The topics, and the feed's tabs: All, each fixed topic, then Experiments (every instrument's).
async function loadTopics() {
  topics = await api("GET", "/api/topics");
  const tabs = [["all", "All"], ...topics.filter((t) => !t.instrument).map((t) => [t.id, t.id]), ["runs", "Experiments"]];
  $("tabs").replaceChildren(...tabs.map(([id, label]) => el("button", {
    type: "button", textContent: label, onclick: () => showTab(id),
  })));
  $("tabs").querySelectorAll("button").forEach((b, i) => { b.dataset.tab = tabs[i][0]; });
  showTab(feedTab);
}

async function loadPrefs() {
  const prefs = await api("GET", "/api/prefs");
  // If the topics couldn't be loaded, at least this device's own, so saving keeps them.
  const offered = topics.length ? topics : prefs.topics.map((id) => ({ id, label: id }));
  $("topics").replaceChildren(el("legend", { textContent: "Topics" }), ...offered.map((topic) => el("label", {},
    el("input", { type: "checkbox", name: "topic", value: topic.id, checked: prefs.topics.includes(topic.id) }),
    ` ${topic.label}`)));
  const quiet = prefs.quiet;
  $("quiet-on").checked = !!quiet;
  if (quiet) {
    $("quiet-start").value = quiet.start;
    $("quiet-end").value = quiet.end;
    $("quiet-warnings").checked = quiet.warnings_only;
  }
  $("quiet-tz").textContent = `Times are in ${quiet ? quiet.timezone : TIMEZONE}.`;
}

async function savePrefs(event) {
  event.preventDefault();
  const topics = [...document.querySelectorAll("input[name=topic]:checked")].map((input) => input.value);
  const quiet = $("quiet-on").checked ? {
    start: $("quiet-start").value, end: $("quiet-end").value, timezone: TIMEZONE, warnings_only: $("quiet-warnings").checked,
  } : null;
  try {
    await api("PUT", "/api/prefs", { topics, quiet });
    $("prefs-status").textContent = "Saved.";
    $("quiet-tz").textContent = `Times are in ${TIMEZONE}.`;
  } catch (e) {
    $("prefs-status").textContent = `Not saved: ${e.message}`;
  }
}

// --- Feed ------------------------------------------------------------------

function feedItem(n, focused) {
  const item = el("li", { className: `${n.severity}${focused ? " focus" : ""}` },
    el("div", { className: "title", textContent: `${n.emoji} ${n.title}`.trim() }));
  const topic = topics.find((t) => t.id === n.topic);
  if (n.topic) item.dataset.tab = topic && !topic.instrument ? n.topic : "runs";
  const when = new Date(n.timestamp);
  if (n.timestamp && !isNaN(when)) item.append(el("time", { dateTime: n.timestamp, textContent: when.toLocaleString() }));
  if (n.text) item.append(el("p", { textContent: n.text }));
  if (n.facts.length) {
    item.append(el("dl", {}, ...n.facts.flatMap(([key, value]) => [el("dt", { textContent: key }), el("dd", { textContent: value })])));
  }
  if (n.url && /^https?:\/\//.test(n.url)) {
    item.append(el("a", { href: n.url, textContent: n.url_label || "Open", target: "_blank", rel: "noopener noreferrer" }));
  }
  return item;
}

async function loadFeed(scrollToFocus) {
  const focus = hashParam("n");
  const items = await api("GET", "/api/feed");  // all the site keeps, so each tab has its history
  $("feed").replaceChildren(...items.map((n) => feedItem(n, n.id === focus)));
  feedLoaded = true;
  showTab(scrollToFocus && focus ? "all" : feedTab);  // so a tapped notification isn't filtered out
  if (scrollToFocus) document.querySelector(".feed .focus")?.scrollIntoView({ block: "center" });
}

// Shows only the feed items under `tab`, saying so if there are none (once the feed has loaded).
function showTab(tab) {
  feedTab = tab;
  $("tabs").querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tab === tab)));
  let shown = 0;
  for (const item of $("feed").children) {
    item.hidden = tab !== "all" && item.dataset.tab !== tab;
    shown += !item.hidden;
  }
  $("feed-empty").textContent = $("feed").children.length ? "Nothing recent." : "Nothing yet.";
  $("feed-empty").hidden = !feedLoaded || shown > 0;
}

// --- Status ----------------------------------------------------------------

const SVG = "http://www.w3.org/2000/svg";
const STATUS_POLL = 5000;  // ms between reads of /api/status while the page is visible
let statusTimer = null;

function time(iso) {
  const when = new Date(iso);
  return iso && !isNaN(when) ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
}

function powerClass(power) {
  return `power-${["high", "medium", "low", "off"].includes(power) ? power : "unknown"}`;
}

// The last 24h as a line, one polyline per stretch at one power, coloured by it.
function sparkline(history) {
  const values = history.values;
  const top = Math.max(1, ...values.map(([current]) => current ?? 0));
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${Math.max(values.length - 1, 1)} 20`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  let line = null, power = null;
  values.forEach(([current, p], i) => {
    if (current === null) {
      line = null;
      return;
    }
    const point = `${i},${(20 - (current / top) * 19).toFixed(2)}`;
    if (!line || p !== power) {
      const previous = line && line.getAttribute("points").split(" ").pop();
      line = document.createElementNS(SVG, "polyline");
      line.setAttribute("class", powerClass(p));
      line.setAttribute("points", previous ? `${previous} ${point}` : point);  // joined to the last stretch
      svg.append(line);
      power = p;
    } else {
      line.setAttribute("points", `${line.getAttribute("points")} ${point}`);
    }
  });
  return svg;
}

function beamRow(beam) {
  const current = beam.current === null ? "—" : `${beam.current.toFixed(1)} µA`;
  const since = beam.since ? ` since ${time(beam.since)}` : "";
  return el("div", { className: `beam ${powerClass(beam.power)}` },
    el("span", { className: "name", textContent: beam.name }),
    el("span", { className: "now" }, el("b", { textContent: beam.power }), ` ${current}${since}`),
    sparkline(beam.history));
}

// The instruments whose run titles are shown in full, so they stay so as the status refreshes.
const openTitles = new Set();

// A long run title is cut short; tapping it shows it all, and tapping again cuts it short.
function runTitle(inst) {
  if (!inst.title) return el("span", { className: "title" });  // keeps its place in the grid
  const title = el("button", { type: "button", className: "title" }, el("span", { textContent: inst.title }));
  title.dataset.instrument = inst.name;
  const show = (open) => {
    title.classList.toggle("open", open);
    title.setAttribute("aria-expanded", String(open));
  };
  show(openTitles.has(inst.name));
  title.onclick = () => {
    if (!openTitles.delete(inst.name)) openTitles.add(inst.name);
    show(openTitles.has(inst.name));
  };
  return title;
}

function instrumentRow(inst, power) {
  // An indeterminate bar (no value) without counts, e.g. while its IOC is down, or a target.
  const progress = inst.target > 0 && inst.counts !== null
    ? el("progress", { max: inst.target, value: Math.max(inst.counts, 0) }) : el("progress", { max: 1 });
  const state = inst.run_state.toLowerCase() || "unknown";
  const counts = inst.counts === null ? "—" : inst.counts.toFixed(1);
  const target = inst.target === null ? "" : ` / ${inst.auto ? "≈" : ""}${inst.target} µA·h`;
  return el("div", { className: `instrument ${powerClass(power)}` },
    el("span", { className: "name", textContent: inst.name, title: `${inst.beam} beam` }),
    el("span", { textContent: inst.run_number ? `${inst.run_number} ${state}` : state }),
    runTitle(inst),
    el("span", {}, progress, el("span", { className: "counts", textContent: `${counts}${target}` })));
}

// When the status shown came, on this device's clock (from the age the server gives, so
// a phone whose clock is off still shows the right time).
let statusReceived = null;

function clock(when) {
  return when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

// Shows `render(...row)` for each row, replacing only those whose data changed, so
// a text selection or a screen reader's place in the others survives a refresh.
function updateRows(container, rows, render) {
  if (container.children.length !== rows.length) container.replaceChildren(...rows.map(() => el("div")));
  [...container.children].forEach((node, i) => {
    const key = JSON.stringify(rows[i]);
    if (node.key === key) return;
    const row = render(...rows[i]);
    row.key = key;
    node.replaceWith(row);
  });
}

async function loadStatus() {
  const { status, received_at: receivedAt, age: ageSeconds, stale } = await api("GET", "/api/status");
  $("status-none").hidden = !!status;
  if (!status) return;
  statusReceived = Date.now() - ageSeconds * 1000;
  const powers = Object.fromEntries(status.beams.map((b) => [b.name, b.power]));
  updateRows($("beams"), status.beams.map((beam) => [beam]), beamRow);
  const focused = document.activeElement.dataset.instrument;  // a run title, kept focused if its row changes
  updateRows($("instruments"), status.instruments.map((inst) => [inst, powers[inst.beam]]), instrumentRow);
  if (focused && !document.activeElement.dataset.instrument) {
    $("instruments").querySelector(`[data-instrument="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  }
  if ($("mcr").textContent !== status.mcr.news) $("mcr").textContent = status.mcr.news;
  $("status").classList.toggle("stale", stale);
  const age = $("status-age");
  age.classList.toggle("offline", stale);
  const received = new Date(receivedAt * 1000);
  age.textContent = stale ? `Monitor offline since ${received.toLocaleString()}`
    : `Updated at ${clock(new Date(statusReceived))}`;
}

// A failed read leaves the status shown, but says how old it is.
function statusFailed(e) {
  if (e.status === 401) {  // this device was removed or signed out: back to joining
    location.replace("/");
    return;
  }
  $("status").classList.add("stale");
  const age = $("status-age");
  age.classList.add("offline");
  age.textContent = statusReceived === null ? "Couldn't read the status."
    : `Couldn't update; last updated at ${clock(new Date(statusReceived))}.`;
}

// Reads the status now and every STATUS_POLL while the page is visible.
function pollStatus() {
  clearTimeout(statusTimer);
  if (document.hidden || $("app").hidden) return;
  loadStatus().catch(statusFailed).finally(() => { statusTimer = setTimeout(pollStatus, STATUS_POLL); });
}

// --- Start -----------------------------------------------------------------

async function showApp(me) {
  signedIn = true;
  $("join").hidden = true;
  $("label").textContent = me.label;
  // The feed's tabs and the preferences need them; without, it still shows the rest.
  await loadTopics().catch((e) => message(`Couldn't load the topics: ${e.message}`));
  $("app").hidden = false;
  pollStatus();
  await Promise.all([refreshPush().catch((e) => message(e.message)), loadPrefs(), loadFeed(true)]);
}

async function forget() {
  if (!confirm("Remove this device? You'll need a new invite to get notifications here again.")) return;
  try {
    const registration = await worker;
    if (registration) await (await registration.pushManager.getSubscription())?.unsubscribe();
    await api("DELETE", "/api/me");
    location.replace("/");
  } catch (e) {
    message(`Couldn't remove this device: ${e.message}`);
  }
}

async function start() {
  $("join-paste").onsubmit = joinPasted;
  $("push-on").onclick = pushOn;
  $("push-off").onclick = pushOff;
  $("prefs").onsubmit = savePrefs;
  $("forget").onclick = forget;
  // An invite or notification link opened while the page is already open only changes the #hash.
  addEventListener("hashchange", () => {
    if (signedIn) {
      if (hashParam("invite")) history.replaceState(null, "", "/");  // already joined
      loadFeed(true).catch(() => {});
    } else {
      showJoin(hashParam("invite"));
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && signedIn) loadFeed(false).catch(() => {});
    pollStatus();  // and stops polling while hidden
  });

  let me = null;
  try {
    me = await api("GET", "/api/me");
  } catch (e) {
    if (e.status !== 401) throw e;
  }
  if (me) {
    if (hashParam("invite")) history.replaceState(null, "", "/");  // already joined
    await showApp(me);
  } else {
    showJoin(hashParam("invite"));
  }
}

start().catch((e) => message(`Something went wrong: ${e.message}`));
