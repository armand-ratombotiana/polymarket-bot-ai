// public/sw.js — W11-8 PWA service worker.
//
// Caches the Polymarket Pro app shell (HTML / CSS / JS / static assets) so
// the workstation is usable when the network drops. API requests under
// `/api/` are intentionally NEVER cached — they are real-time trading
// endpoints (positions, prices, order book) and returning stale data could
// cause incorrect trading decisions. The fetch handler lets every `/api/`
// request fall through to the network unconditionally.
//
// Cache versioning: bump `CACHE_NAME` when shipping a new app shell so the
// `activate` handler deletes the old cache and the next load repopulates
// fresh assets.
//
// Safety notes:
//  - Only `status === 200` responses are cached (no opaque / error bodies).
//  - Cross-origin requests (e.g. Polymarket CLOB API on clob.polymarket.com)
//    are NOT in the app shell — they go straight to the network.
//  - When the cache misses AND the network is down, document navigations
//    fall back to the cached "/" so the user sees the dashboard shell
//    rather than Chrome's `ERR_INTERNET_DISCONNECTED` page.

const CACHE_NAME = 'polymarket-pro-v1'
const APP_SHELL = [
  '/',
  '/manifest.json',
  '/icon.svg',
]

// Install: pre-cache the minimal app shell.
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)),
  )
  // Activate immediately — don't make the user close & reopen every tab
  // to pick up the new SW. The fresh SW takes over on the next navigation.
  self.skipWaiting()
})

// Activate: evict any caches from prior versions, then claim all open
// clients so they pick up the new SW without a manual reload.
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => {
      return Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      )
    }),
  )
  self.clients.claim()
})

// Fetch: network-first for /api/, cache-first for everything else.
self.addEventListener('fetch', (event) => {
  const request = event.request
  // Only intercept GET — POST / PUT / DELETE mutations must reach the API.
  if (request.method !== 'GET') return

  let url
  try {
    url = new URL(request.url)
  } catch {
    return // Malformed URL — let the browser handle it.
  }

  // Never cache API requests. Real-time trading data must always be live.
  if (url.pathname.startsWith('/api/')) {
    return
  }

  // Cache-first for static assets & same-origin navigations.
  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached
      return fetch(request)
        .then((response) => {
          // Only cache successful, same-origin responses. Opaque (CORS)
          // responses are `status: 0` and shouldn't be cached — they'd
          // mask real errors.
          if (
            response.status === 200 &&
            (response.type === 'basic' || response.type === 'default')
          ) {
            const clone = response.clone()
            caches.open(CACHE_NAME).then((cache) => cache.put(request, clone))
          }
          return response
        })
        .catch(() => {
          // Offline fallback: if a document navigation failed, serve the
          // cached root shell so the user sees the dashboard (which will
          // show the OfflineIndicator banner once it boots).
          if (request.destination === 'document') {
            return caches.match('/')
          }
          // For other asset types, just let the request fail naturally —
          // the browser will show its own broken-resource UI.
          return undefined
        })
    }),
  )
})
