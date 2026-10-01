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
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if ("focus" in client) {
        await client.focus();
        return client.navigate(url);
      }
    }
    return self.clients.openWindow(url);
  })());
});

// The browser replaced the subscription (e.g. it expired): register the new one.
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil((async () => {
    const { key } = await (await fetch("/api/vapid-key")).json();
    const raw = atob(key.replace(/-/g, "+").replace(/_/g, "/"));
    const sub = await self.registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: Uint8Array.from(raw, (c) => c.charCodeAt(0)),
    });
    await fetch("/api/subscription", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(sub),
    });
  })());
});
