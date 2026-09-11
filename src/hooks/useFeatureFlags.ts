// hooks/useFeatureFlags.ts
// W12-1 — Frontend feature-flag hook. Fetches the list of flags from the
// backend `/api/flags` endpoint, caches them in React state, and polls
// every 60 seconds so an operator toggling a flag in the dashboard sees
// the change reflected in the UI within a minute (without a full page
// reload or a redeploy).
//
// Contract:
//   const { flags, isEnabled, isLoading } = useFeatureFlags()
//   if (isEnabled('live_trading')) { ... }
//
// `isEnabled(key)` returns `false` for unknown keys AND while the
// initial fetch is in-flight (fail-safe — a missing flag never enables
// a feature). After the first successful fetch the cached value is used
// synchronously so renders are not gated on a network round-trip.
//
// The polling interval is intentionally coarse (60 s) — flags are
// expected to change at human cadence (operator flips one in the
// dashboard, observes the effect), not at market cadence. A faster poll
// would burn backend quota for no perceptible UX gain.
'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { apiFetch } from '@/lib/api'

export interface FeatureFlag {
  key: string
  enabled: boolean
  description: string
  config: Record<string, unknown>
  updated_at: number
}

interface FlagsResponse {
  flags: FeatureFlag[]
  count: number
}

const POLL_INTERVAL_MS = 60_000

/**
 * useFeatureFlags — fetch + cache the backend feature-flag list.
 *
 * Returns:
 *   - `flags`: array of FeatureFlag objects (empty until first fetch).
 *   - `isEnabled(key)`: synchronous lookup against the cached flags.
 *       Returns false for unknown keys or while the initial fetch is
 *       pending (fail-safe).
 *   - `isLoading`: true until the first successful fetch resolves.
 *   - `refresh()`: imperatively re-fetch (used after a flag mutation
 *       to avoid waiting up to 60 s for the next poll).
 *
 * @param pollIntervalMs optional override for the polling cadence
 *   (defaults to 60 s). Exposed primarily for tests; production callers
 *   should leave the default.
 */
export function useFeatureFlags(opts?: { pollIntervalMs?: number }) {
  const pollIntervalMs = opts?.pollIntervalMs ?? POLL_INTERVAL_MS
  const [flags, setFlags] = useState<FeatureFlag[]>([])
  const [isLoading, setIsLoading] = useState(true)
  // Ref mirror of `flags` so `isEnabled` can read the current set
  // without being a dependency of the callback (stable identity).
  const flagsRef = useRef<Record<string, FeatureFlag>>({})

  const fetchFlags = useCallback(async () => {
    try {
      const res = await apiFetch('/api/flags')
      if (res.ok) {
        const data = (await res.json()) as FlagsResponse
        const list = Array.isArray(data?.flags) ? data.flags : []
        setFlags(list)
        const next: Record<string, FeatureFlag> = {}
        for (const f of list) next[f.key] = f
        flagsRef.current = next
      }
    } catch {
      // Network error — keep the stale cache (fail-open on the cache,
      // fail-closed on the individual isEnabled check). The next poll
      // will retry.
    } finally {
      setIsLoading(false)
    }
  }, [])

  useEffect(() => {
    // Initial fetch.
    fetchFlags()
    // 60s poll.
    const id = setInterval(fetchFlags, pollIntervalMs)
    // Visibility-aware: pause the poll when the tab is hidden so we
    // don't burn backend quota on a tab nobody is looking at. The
    // poll resumes (and immediately re-fetches) when the tab becomes
    // visible again.
    let intervalId: ReturnType<typeof setInterval> | null = id
    const onVis = () => {
      if (typeof document === 'undefined') return
      if (document.hidden) {
        if (intervalId) {
          clearInterval(intervalId)
          intervalId = null
        }
      } else if (!intervalId) {
        fetchFlags()
        intervalId = setInterval(fetchFlags, pollIntervalMs)
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis)
    }
    return () => {
      if (intervalId) clearInterval(intervalId)
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis)
      }
    }
  }, [fetchFlags, pollIntervalMs])

  const isEnabled = useCallback((key: string): boolean => {
    const f = flagsRef.current[key]
    return Boolean(f?.enabled)
  }, [])

  const refresh = useCallback(() => {
    return fetchFlags()
  }, [fetchFlags])

  return { flags, isEnabled, isLoading, refresh }
}
