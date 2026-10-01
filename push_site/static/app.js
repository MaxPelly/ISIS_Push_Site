// The page: join with an invite, turn push on or off, pick topics and quiet
// hours, and read recent notifications. Text is only ever set with
// textContent, never as HTML.
"use strict";

const $ = (id) => document.getElementById(id);
const TOPIC_NAMES = { TS1: "TS1 beam", TS2: "TS2 beam", Muons: "Muon beam", MCR: "MCR news", Summary: "Daily beam summary" };
const TIMEZONE = Intl.DateTimeFormat().resolvedOptions().timeZone;
const IOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const STANDALONE = matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
const PUSH_SUPPORTED = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;

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

// --- Joining ---------------------------------------------------------------

function showJoin(invite) {
  $("join").hidden = false;
  $("join-ios").hidden = !(IOS && !STANDALONE);
  if (!invite) {
    $("join-text").textContent = "To get notifications here, open the invite link you were sent.";
    return;
  }
  $("join-text").textContent = "You've been invited to get ISIS beam notifications on this device.";
  $("join-button").hidden = false;
  $("join-button").onclick = async () => {
    try {
      await api("POST", "/api/redeem", { invite });
      history.replaceState(null, "", "/");  // keep the used invite out of the address bar
      $("join").hidden = true;
      message("");
      await showApp(await api("GET", "/api/me"));
    } catch (e) {
      message(e.status === 403 ? "This invite is invalid, used up or expired. Ask for a new one." : e.message);
    }
  };
}

// --- Push on this device ---------------------------------------------------

async function refreshPush() {
  for (const id of ["push-on", "push-off", "install-help"]) $(id).hidden = true;
  const status = $("push-status");
  if (!PUSH_SUPPORTED) {
    $("install-help").hidden = !IOS;
    status.textContent = IOS ? "Install the app to get notifications." : "This browser can't receive push notifications.";
    return;
  }
  if (Notification.permission === "denied") {
    status.textContent = "Notifications are blocked for this site in your browser or system settings.";
    return;
  }
  const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
  status.textContent = sub ? "Notifications are on." : "Notifications are off.";
  $(sub ? "push-off" : "push-on").hidden = false;
  if (sub) await api("PUT", "/api/subscription", sub.toJSON()).catch(() => {});  // keep the server in step
}

async function pushOn() {
  try {
    if ((await Notification.requestPermission()) === "granted") {
      const { key } = await api("GET", "/api/vapid-key");
      const registration = await navigator.serviceWorker.ready;
      const sub = await registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
      await api("PUT", "/api/subscription", sub.toJSON());
    }
  } catch (e) {
    message(`Couldn't turn on notifications: ${e.message}`);
  }
  await refreshPush();
}

async function pushOff() {
  try {
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
    await api("DELETE", "/api/subscription");
  } catch (e) {
    message(`Couldn't turn off notifications: ${e.message}`);
  }
  await refreshPush();
}

// --- Preferences -----------------------------------------------------------

async function loadPrefs() {
  const [offered, prefs] = await Promise.all([api("GET", "/api/topics"), api("GET", "/api/prefs")]);
  // Keep topics this device chose that aren't offered any more, so saving doesn't drop them.
  const topics = [...offered, ...prefs.topics.filter((t) => !offered.includes(t))];
  $("topics").replaceChildren(el("legend", { textContent: "Topics" }), ...topics.map((topic) => el("label", {},
    el("input", { type: "checkbox", name: "topic", value: topic, checked: prefs.topics.includes(topic) }),
    ` ${TOPIC_NAMES[topic] || `${topic} runs`}`)));
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
  if (n.timestamp) {
    item.append(el("time", { dateTime: n.timestamp, textContent: new Date(n.timestamp).toLocaleString() }));
  }
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

async function loadFeed() {
  const focus = hashParam("n");
  const items = await api("GET", "/api/feed?limit=50");
  $("feed").replaceChildren(...(items.length ? items.map((n) => feedItem(n, n.id === focus))
    : [el("li", { textContent: "Nothing yet." })]));
  document.querySelector(".feed .focus")?.scrollIntoView({ block: "center" });
}

// --- Start -----------------------------------------------------------------

async function showApp(me) {
  $("app").hidden = false;
  $("label").textContent = me.label;
  await Promise.all([refreshPush(), loadPrefs(), loadFeed()]);
}

async function forget() {
  if (!confirm("Remove this device? You'll need a new invite to get notifications here again.")) return;
  try {
    if (PUSH_SUPPORTED) await (await (await navigator.serviceWorker.ready).pushManager.getSubscription())?.unsubscribe();
    await api("DELETE", "/api/me");
    location.replace("/");
  } catch (e) {
    message(`Couldn't remove this device: ${e.message}`);
  }
}

async function start() {
  if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js");
  $("push-on").onclick = pushOn;
  $("push-off").onclick = pushOff;
  $("prefs").onsubmit = savePrefs;
  $("forget").onclick = forget;
  // An invite or notification link opened while the page is already open only changes the #hash.
  addEventListener("hashchange", () => {
    if (!$("app").hidden) loadFeed().catch(() => {});
    else showJoin(hashParam("invite"));
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !$("app").hidden) loadFeed().catch(() => {});
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
