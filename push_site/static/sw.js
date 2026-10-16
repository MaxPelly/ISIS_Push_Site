// Service worker: shows pushed notifications and opens their feed entry.
// No offline cache: the app is only useful online, and nothing goes stale.

self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (e) {
    // Not ours or malformed: still show something, as browsers require for every push.
  }
  event.waitUntil(self.registration.showNotification(data.title || "ISIS Beam Monitor", {
    body: data.body || "",
    tag: data.id,  // a repeat of the same notification replaces it
    icon: "/static/icon-192.png",
    data: { id: data.id || "" },
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = "/#n=" + encodeURIComponent(event.notification.data.id);
  event.waitUntil((async () => {
    // In an open page, or a new one if there's none (client undefined) or the browser
    // won't focus it, so a tap always shows the site.
    const [client] = await self.clients.matchAll({ type: "window" });
    try {
      await client.focus();
    } catch (e) {
      return self.clients.openWindow(url);
    }
    return client.navigate(url).catch(() => {});  // shown, if not scrolled to the notification
  })());
});

async function fetchOk(url, options) {
  const resp = await fetch(url, options);
  if (!resp.ok) throw new Error(`${url}: HTTP ${resp.status}`);
  return resp;
}

// The browser replaced the subscription (e.g. it expired): register the new
// one, subscribing again only if the browser didn't already.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    let sub = event.newSubscription;
    if (!sub) {
      const { key } = await (await fetchOk("/api/vapid-key")).json();
      const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/"));
      sub = await self.registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: Uint8Array.from(raw, (c) => c.charCodeAt(0)),
      });
    }
    await fetchOk("/api/subscription", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub),
    });
  })());
});
