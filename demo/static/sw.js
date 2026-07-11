// Service worker (Part 7 C7): caches the app shell (HTML/CSS/JS/icons)
// so the PWA still loads with no connectivity -- complementing, not
// duplicating, this project's existing per-room offline durability
// (the IndexedDB-persisted ops outbox in common.js, which already lets
// an open room keep accepting edits through a dropped connection).
// This layer is about the *shell loading at all* when the network is
// down at page-load time, which the outbox alone can't help with.
//
// Deliberately network-first, falling back to cache only on failure --
// an actively-developed app where stale JS could desync from the
// server's own WS protocol expectations must never let the cache win
// over a real network response just because it's faster. Never
// touches `/api/*`, `/ws/*` (unreachable via fetch anyway), or
// non-GET requests -- room data is always live, never cached.

const CACHE_NAME = "crdt-cad-shell-v1";
const SHELL_ASSETS = [
  "/",
  "/2d",
  "/3d",
  "/static/common.js",
  "/static/sketch.js",
  "/static/mesh3d.js",
  "/static/home.js",
  "/static/styles.css",
  "/static/tokens.css",
  "/static/icons.svg",
  "/static/favicon.svg",
  "/static/manifest.json",
  "/static/icon-192.png",
  "/static/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) =>
      // addAll fails the whole install if any single asset 404s --
      // Promise.allSettled instead so one missing/renamed file doesn't
      // block every other asset from being cached.
      Promise.allSettled(SHELL_ASSETS.map((url) => cache.add(url)))
    )
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((names) =>
      Promise.all(names.filter((n) => n !== CACHE_NAME).map((n) => caches.delete(n)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/ws/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(request, clone));
        }
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || Response.error()))
  );
});
