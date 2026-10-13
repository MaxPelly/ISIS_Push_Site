// The page: join with an invite, turn push on or off, pick topics and quiet
// hours, and read recent notifications. Text is only ever set with
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const TOPIC_NAMES = { TS1: "TS1 beam", TS2: "TS2 beam", Muons: "Muon beam", MCR: "MCR news", Summary: "Daily beam summary" };
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

async function loadPrefs() {
  const [offered, prefs] = await Promise.all([api("GET", "/api/topics"), api("GET", "/api/prefs")]);
  // Keep topics this device chose that aren't offered any more, so saving doesn't drop them.
  const topics = [...offered, ...prefs.topics.filter((t) => !offered.includes(t))];
  $("topics").replaceChildren(el("legend", { textContent: "Topics" }), ...topics.map((topic) => el("label", {},
    el("input", { type: "checkbox", name: "topic", value: topic, checked: prefs.topics.includes(topic) }),
    ` ${Object.hasOwn(TOPIC_NAMES, topic) ? TOPIC_NAMES[topic] : `${topic} runs`}`)));
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
  if (n.topic) item.dataset.tab = Object.hasOwn(TOPIC_NAMES, n.topic) ? n.topic : "runs";  // the CSS shows it under that tab
  const when = new Date(n.timestamp);
  if (n.timestamp && !isNaN(when)) item.append(el("time", { dateTime: n.timestamp, textContent: when.toLocaleString() }));
  if (n.text) item.append(el("p", { textContent: n.text }));
  if (n.flavour) item.append(el("p", { className: "flavour", textContent: n.flavour }));
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
  const items = await api("GET", "/api/feed?limit=200");  // all the site keeps by default, so each tab has some history
  $("feed").replaceChildren(...items.map((n) => feedItem(n, n.id === focus)));  // the CSS says when it's empty
  if (scrollToFocus && focus) $("tab-all").checked = true;  // so the tapped notification isn't filtered out
  if (scrollToFocus) document.querySelector(".feed .focus")?.scrollIntoView({ block: "center" });
}

// --- Start -----------------------------------------------------------------

async function showApp(me) {
  $("app").hidden = false;
  $("label").textContent = me.label;
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
    if (!$("app").hidden) {
      if (hashParam("invite")) history.replaceState(null, "", "/");  // already joined
      loadFeed(true).catch(() => {});
    } else {
      showJoin(hashParam("invite"));
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("app").hidden) loadFeed(false).catch(() => {});
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
