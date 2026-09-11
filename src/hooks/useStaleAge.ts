// hooks/useStaleAge.ts — W41-3 Reusable staleness tracker.
//
// Returns the current age (in seconds) of a data snapshot whose
// `lastUpdated` epoch ms is supplied. Updates on a 5s interval so the
// panel's StaleIndicator can re-render as data ages past 30s / 120s.
//
// The interval is torn down on unmount. When `lastUpdated` is null
// (no data yet), the hook returns null so the caller can skip
// rendering the indicator.
//
// Why 5s: a 1s tick would cause 30 panels × 1 render/s = 30 renders/s
// of header churn for no perceptual gain — StaleIndicator's thresholds
// are 30s and 120s, so 5s granularity is well below the smallest
// meaningful change.

'use client'

import { useEffect, useState } from 'react'

export function useStaleAge(lastUpdated: number | null, tickMs = 5000): number | null {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    // Only arm the interval when there's something to age. Avoids
    // burning renders on panels that haven't fetched their first
    // snapshot yet.
    if (lastUpdated == null) return
    // Sync `now` immediately so the first render after `lastUpdated`
    // flips non-null doesn't carry a stale `now` from a previous
    // mount or a long-idle period.
    setNow(Date.now())
    const t = setInterval(() => setNow(Date.now()), tickMs)
    return () => clearInterval(t)
  }, [lastUpdated, tickMs])
  if (lastUpdated == null) return null
  return Math.max(0, (now - lastUpdated) / 1000)
}
