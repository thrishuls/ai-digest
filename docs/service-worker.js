// AI Daily — service worker (PWA, mid-level)
//
// Strategy
//   * Network-first for HTML/JSON: always fetch fresh; fall back to cache only
//     when the network is unreachable. This is what makes "auto-update on new
//     digest" work without needing to rotate CACHE_VERSION daily — every online
//     visit fetches today's index.html and overwrites the cached copy.
//   * Cache-first for icon.svg and manifest.json: they rarely change, so serve
//     from cache for speed and revalidate in the background.
//
// To force a global cache wipe (e.g., after renaming an asset), bump
// CACHE_VERSION below; the next visit's `activate` event will purge old caches.

const CACHE_VERSION = 'ai-daily-v1';
const PRECACHE = [
  './',
  './index.html',
  './manifest.json',
  './icon.svg',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_VERSION).then((cache) => cache.addAll(PRECACHE))
  );
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_VERSION)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;

  const url = new URL(req.url);
  // Don't intercept cross-origin (e.g., Google Fonts) — let the browser handle it.
  if (url.origin !== self.location.origin) return;

  const isStaticAsset =
    url.pathname.endsWith('/icon.svg') ||
    url.pathname.endsWith('/manifest.json');

  if (isStaticAsset) {
    // Cache-first.
    event.respondWith(
      caches.match(req).then((hit) => {
        if (hit) return hit;
        return fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
          return res;
        });
      })
    );
    return;
  }

  // Network-first for everything else (HTML, dated archives, anything new).
  event.respondWith(
    fetch(req)
      .then((res) => {
        // Only cache successful basic responses.
        if (res && res.status === 200 && res.type === 'basic') {
          const copy = res.clone();
          caches.open(CACHE_VERSION).then((c) => c.put(req, copy));
        }
        return res;
      })
      .catch(() =>
        caches.match(req).then((hit) => hit || caches.match('./'))
      )
  );
});
