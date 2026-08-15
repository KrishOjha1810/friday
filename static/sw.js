/* Friday's service worker: the part that is awake when the page is not.
 *
 * It exists for one job. An agent is blocked, or somebody needs you in Slack,
 * and your phone is in your pocket with the screen off. Everything else Friday
 * does assumes a tab is open on a Mac you are sitting at.
 *
 * The payload arrives already decrypted by the browser (the server encrypts it
 * for this device specifically), so nothing here has to be trusted with keys.
 */

self.addEventListener('install', (e) => {
  // Take over immediately: waiting for every tab to close before a new worker
  // activates means a fix ships and does nothing until tomorrow.
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(self.clients.claim());
});

self.addEventListener('push', (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch (err) {
    data = {};
  }
  const title = data.title || 'Friday';
  const body = data.body || 'Something needs you.';
  // `tag` collapses repeats: an agent that asks the same thing twice replaces
  // its own notification rather than stacking a second one.
  const opts = {
    body: body,
    tag: data.tag || 'friday',
    renotify: false,
    data: { url: data.url || '/' },
    icon: '/icon.png',
    badge: '/icon.png',
  };
  event.waitUntil(self.registration.showNotification(title, opts));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({
      type: 'window', includeUncontrolled: true,
    });
    // Focus a tab that is already open rather than opening a fifth one.
    for (const c of all) {
      if ('focus' in c) {
        try { await c.focus(); } catch (e) { /* keep trying the rest */ }
        if ('navigate' in c && target !== '/') {
          try { await c.navigate(target); } catch (e) { /* not fatal */ }
        }
        return;
      }
    }
    if (self.clients.openWindow) await self.clients.openWindow(target);
  })());
});
