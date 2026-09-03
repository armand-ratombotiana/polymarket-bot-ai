// src/components/SWRegister.tsx — W11-8 PWA service-worker registration.
//
// Tiny client-only wrapper that calls `registerServiceWorker()` exactly
// once on mount. Mounting this in the root layout (see src/app/layout.tsx)
// ensures the SW is registered for every route in the app without needing
// to thread a useEffect through every page component.
//
// Renders `null` — has no visual footprint.

'use client'

import { useEffect } from 'react'
import { registerServiceWorker } from '@/lib/registerSW'

export default function SWRegister() {
  useEffect(() => {
    registerServiceWorker()
  }, [])

  return null
}
