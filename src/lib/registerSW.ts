// src/lib/registerSW.ts — W11-8 PWA service-worker registration helper.
//
// Registers `/sw.js` (see public/sw.js) so the Polymarket Pro dashboard
// can be installed as a standalone PWA and survive transient network
// drops. The registration is deferred to the browser's `load` event so it
// never contends with first-paint critical-path fetches.
//
// This module is safe to import from server components — the `'use client'`
// directive plus the `typeof window === 'undefined'` guard make it a no-op
// on the server.
//
// NOTE: `'serviceWorker' in navigator` is intentionally wrapped in a
// try/catch — in some sandboxed iframes (e.g. the sandbox preview used to
// demo this project) the SW API exists but registration throws a Security
// Error. We log and move on rather than crash the mount.

'use client'

export function registerServiceWorker(): void {
  if (typeof window === 'undefined') return
  if (!('serviceWorker' in navigator)) return

  const register = () => {
    // Wrap in try/catch — some sandboxed environments (e.g. the sandbox
    // preview iframe) have `'serviceWorker' in navigator` truthy but
    // `.register()` throws synchronously with a SecurityError. We catch
    // and log rather than crash the React mount that called us.
    try {
      navigator.serviceWorker
        .register('/sw.js')
        .then((registration) => {
          // Log success at debug level — useful for diagnosing "the PWA
          // didn't install" issues without spamming the console.
          console.debug('[SW] registered scope:', registration.scope)
        })
        .catch((err: unknown) => {
          // Registration failed (e.g. SecurityError from sandboxed iframe,
          // 404 on /sw.js). Don't throw; the app still works online-only,
          // which is strictly better than crashing the layout.
          console.error('[SW] registration failed:', err)
        })
    } catch (err: unknown) {
      // Synchronous throw from `.register()` (rare but possible in
      // sandboxed contexts). Same treatment as the Promise rejection.
      console.error('[SW] registration threw synchronously:', err)
    }
  }

  if (document.readyState === 'complete') {
    // Page already finished loading — register immediately.
    register()
  } else {
    window.addEventListener('load', register)
  }
}
