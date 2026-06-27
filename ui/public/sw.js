/*
 * ARIA - Service Worker
 *
 * Phase: UI / PWA
 * Purpose: Provide PWA installability and a basic offline app shell.
 *
 * Strategy: network-first for navigation/static GETs, falling back to cache
 * when offline. API and SSE/streaming requests are NEVER intercepted — they
 * always hit the network untouched so the fetch-based SSE chat keeps working.
 */

const CACHE_NAME = 'aria-shell-v1'

// Minimal app shell precached on install.
const APP_SHELL = ['/', '/icon-192.png', '/icon-512.png']

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => cache.addAll(APP_SHELL))
      .catch(() => {
        /* best-effort precache; don't block install on failure */
      })
      .then(() => self.skipWaiting())
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((key) => key !== CACHE_NAME)
            .map((key) => caches.delete(key))
        )
      )
      .then(() => self.clients.claim())
  )
})

// Returns true if this request must bypass the service worker entirely.
function shouldBypass(request) {
  // Only handle GET; let POST/PUT/etc. (incl. SSE POST streams) pass through.
  if (request.method !== 'GET') return true

  // Never intercept API calls.
  const url = new URL(request.url)
  if (url.pathname.includes('/api/')) return true

  // Never intercept EventSource / SSE / streaming requests.
  const accept = request.headers.get('accept') || ''
  if (accept.includes('text/event-stream')) return true

  // Only handle same-origin requests; let cross-origin pass through.
  if (url.origin !== self.location.origin) return true

  return false
}

self.addEventListener('fetch', (event) => {
  const { request } = event

  if (shouldBypass(request)) {
    // Do not call respondWith — request goes straight to the network.
    return
  }

  // Network-first: try network, cache successful responses, fall back to cache.
  event.respondWith(
    fetch(request)
      .then((response) => {
        if (response && response.ok && response.type === 'basic') {
          const copy = response.clone()
          caches
            .open(CACHE_NAME)
            .then((cache) => cache.put(request, copy))
            .catch(() => {})
        }
        return response
      })
      .catch(async () => {
        const cached = await caches.match(request)
        if (cached) return cached
        // For navigations, fall back to the cached app shell root.
        if (request.mode === 'navigate') {
          const shell = await caches.match('/')
          if (shell) return shell
        }
        throw new Error('Network error and no cache available')
      })
  )
})
