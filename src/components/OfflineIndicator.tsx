// src/components/OfflineIndicator.tsx — W11-8 PWA offline banner.
//
// Shows a sticky top banner whenever `navigator.onLine === false` so the
// trader knows that data is stale (last cached values) and any new trades
// will queue locally until connectivity returns. The banner auto-hides
// when the browser fires the `online` event.
//
// Implementation notes:
//  - `navigator.onLine` is famously unreliable for detecting *real*
//    connectivity (it only flips when the OS reports the network went
//    down). Treat this as "the OS thinks we're offline" — the offline
//    banner is a UX affordance, not a guarantee that data is stale.
//  - We don't poll the backend heartbeat endpoint to keep this component
//    self-contained; the dashboard's other panels (e.g. TopStatusBar) are
//    already polling and will visually show their own "data stale"
//    indicators when their fetches start failing.
//  - The banner is `position: sticky` so it overlays the dashboard
//    chrome without pushing the layout (which would cause a jarring
//    reflow when connectivity flaps).

'use client'

import { useEffect, useState } from 'react'

export default function OfflineIndicator() {
  // SSR-safe initial state — assume online until the client can read
  // navigator.onLine. This avoids a hydration mismatch where the server
  // renders `null` (offline) and the client renders the banner (or vice
  // versa). We sync the real value in a useEffect after mount.
  const [isOffline, setIsOffline] = useState(false)

  useEffect(() => {
    if (typeof navigator === 'undefined') return
    // Sync the real value on mount — covers the case where the user
    // opened the PWA while already offline.
    setIsOffline(!navigator.onLine)

    const handleOnline = () => setIsOffline(false)
    const handleOffline = () => setIsOffline(true)

    window.addEventListener('online', handleOnline)
    window.addEventListener('offline', handleOffline)

    return () => {
      window.removeEventListener('online', handleOnline)
      window.removeEventListener('offline', handleOffline)
    }
  }, [])

  if (!isOffline) return null

  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="offline-indicator"
      data-testid="offline-indicator"
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 9999,
        width: '100%',
        background: '#7c2d12',
        color: '#fed7aa',
        borderBottom: '1px solid #9a3412',
        padding: '0.5rem 1rem',
        fontSize: '0.875rem',
        fontWeight: 500,
        textAlign: 'center',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        gap: '0.5rem',
      }}
    >
      <span aria-hidden="true">⚠</span>
      <span>
        You are offline — showing last cached market data. New trades will
        queue locally and retry when connectivity returns.
      </span>
    </div>
  )
}
