/* Baysoko PWA Service Worker */
const CACHE_VERSION = 'baysoko-pwa-v1';
const STATIC_CACHE = `${CACHE_VERSION}-static`;
const RUNTIME_CACHE = `${CACHE_VERSION}-runtime`;

const PRECACHE_URLS = [
  '/',
  '/?source=pwa',
  '/static/manifest.json',
  '/static/icons/icon-192x192.png',
  '/static/icons/icon-512x512.png',
  '/static/css/styles.css',
  '/static/js/agent_widget.js',
  '/static/js/global-ajax-handler.js',
  '/static/offline.html'
];

self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(STATIC_CACHE).then((cache) => cache.addAll(PRECACHE_URLS)).catch(() => {})
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.map((key) => {
        if (!key.startsWith(CACHE_VERSION)) return caches.delete(key);
        return null;
      }))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  const isSameOrigin = url.origin === self.location.origin;
  const isStatic = isSameOrigin && url.pathname.startsWith('/static/');

  if (isStatic) {
    event.respondWith(
      caches.match(req).then((cached) => {
        const fetchPromise = fetch(req).then((response) => {
          const copy = response.clone();
          caches.open(RUNTIME_CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
          return response;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    );
    return;
  }

  // Network-first for HTML/navigation; fallback to offline page if needed.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req).then((response) => {
        const copy = response.clone();
        caches.open(RUNTIME_CACHE).then((cache) => cache.put(req, copy)).catch(() => {});
        return response;
      }).catch(() => caches.match(req).then((cached) => cached || caches.match('/static/offline.html')))
    );
    return;
  }

  // Default: try cache first, then network.
  event.respondWith(
    caches.match(req).then((cached) => cached || fetch(req))
  );
});
