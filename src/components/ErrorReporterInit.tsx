// components/ErrorReporterInit.tsx — W14-8 mounts the global error handlers.
//
// Renders nothing — purely a side-effect mount that installs the
// `error` / `unhandledrejection` / `beforeunload` window listeners
// from `lib/errorReporter`. Mounted in `app/layout.tsx` ABOVE the
// ErrorBoundary (so it survives a render crash and keeps capturing
// subsequent errors in the fallback UI).
//
// Why a separate component (rather than calling installErrorHandlers
// directly in a layout effect):
//   * Layout is a server component — it can't call useEffect.
//   * A client wrapper is the smallest unit of client JS that can
//     carry a useEffect, and tree-shaking keeps the cost tiny
//     (the reporter module is already in the client bundle via
//     ErrorBoundary's captureError import).
//
// Idempotency: `installErrorHandlers` does NOT dedupe listeners —
// calling it twice would double-report every error. This component
// guards against that by installing once per mount (Strict Mode in
// dev double-invokes effects, but `addEventListener` is a no-op for
// the same listener+type+capture tuple, so duplicate registrations
// are silently collapsed by the browser).
'use client'

import { useEffect } from 'react'
import { installErrorHandlers } from '@/lib/errorReporter'

export default function ErrorReporterInit() {
  useEffect(() => {
    installErrorHandlers()
  }, [])

  return null
}
