// src/app/error.tsx — W10-3
// Next.js App Router error boundary (route-segment-level). Catches errors
// that escape the in-tree <ErrorBoundary> (src/components/ErrorBoundary.tsx)
// — e.g. errors thrown by Server Components above the layout, or errors
// thrown during route-segment rendering that React's tree boundary does
// not capture because they happen at the App Router level.
//
// Per Next.js docs: an `error.tsx` file MUST be a Client Component
// (`'use client'`) and MUST accept `{ error, reset }` props. `reset` is
// provided by the App Router and re-renders the segment from scratch.
//
// Coexists with the in-tree ErrorBoundary in src/app/layout.tsx — that one
// catches errors thrown in the React tree below the layout (client panel
// crashes); this one catches errors Next.js itself surfaces for the route.
'use client'

import { useEffect } from 'react'

interface ErrorProps {
  error: Error & { digest?: string }
  reset: () => void
}

export default function Error({ error, reset }: ErrorProps) {
  // Mirror the in-tree boundary's logging behavior so a single crash shows
  // up consistently in the dev console regardless of which boundary caught
  // it. `error.digest` is a stable hash Next.js attaches to server-side
  // errors so they can be correlated in server logs.
  useEffect(() => {
    console.error('[app/error.tsx] Route-segment error caught:', error)
  }, [error])

  const handleReload = () => {
    if (typeof window !== 'undefined') {
      window.location.reload()
    }
  }

  return (
    <div
      className="error-page"
      role="alertdialog"
      aria-labelledby="app-error-title"
      aria-describedby="app-error-desc"
    >
      <div className="error-page-icon" aria-hidden="true">⚠</div>
      <h2 id="app-error-title" className="error-page-title">
        Something went wrong
      </h2>
      <p id="app-error-desc" className="error-page-message">
        {error?.message ?? 'An unexpected error occurred while rendering this page.'}
      </p>
      {error?.digest && (
        <p className="error-page-digest mono">
          Error ID: {error.digest}
        </p>
      )}
      <div className="error-page-actions">
        <button
          type="button"
          className="btn btn-primary"
          onClick={reset}
        >
          ↻ Try again
        </button>
        <button
          type="button"
          className="btn btn-ghost"
          onClick={handleReload}
        >
          ⟳ Reload page
        </button>
      </div>
    </div>
  )
}
