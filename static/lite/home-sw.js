// Service worker for notifications only. It deliberately has no fetch
// handler: it never touches page loads or requests.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()));

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { data = { body: event.data ? event.data.text() : '' }; }
  event.waitUntil(self.registration.showNotification(data.title || 'Reminder', {
    body: data.body || '', tag: data.tag || undefined, renotify: Boolean(data.tag),
    icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png', data: { url: data.url || '/home' },
  }));
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/home';
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((open) => {
    for (const client of open) {
      if (new URL(client.url).pathname.startsWith('/home')) {
        if (client.navigate) client.navigate(url);
        return client.focus();
      }
    }
    return self.clients.openWindow(url);
  }));
});
