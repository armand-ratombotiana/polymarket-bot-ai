// hooks/useRealtimeData.ts
// Hybrid data hook — REST prefetch + WebSocket live updates + polling fallback.
//
// W11-4 — Drop-in replacement for the ad-hoc "fetch on mount + setInterval"
// pattern that ~30 panels currently duplicate. The contract:
//   1. On mount, fire a REST GET against `endpoint` to populate the
//      initial state synchronously (no flash of empty content).
//   2. In parallel, the embedded useWebSocket() opens a connection to the
//      bot's /ws endpoint. When a message arrives with `msg.channel ===
//      wsChannel`, swap in `msg.data` as the new state.
//   3. If the WS is NOT connected (still handshaking, mid-reconnect, or
//      permanently failed after maxReconnectAttempts), fall back to
//      polling `endpoint` every `pollInterval` ms. The moment the WS
//      connects, the polling interval is torn down (the effect deps on
//      `isConnected`, so a WS reconnect re-runs the effect and clears
//      the interval).
//   4. When the tab is hidden, skip the polling tick — no point burning
//      backend quota on tabs nobody is looking at. The WS itself is
//      already paused by useWebSocket's visibilitychange handler.
//
// The hook does NOT cancel in-flight REST requests on unmount — `cancelled`
// guards the setState calls, so a late response after unmount is silently
// dropped (same pattern as useBot's fetchRestSnapshot).
//
// Why this is better than the existing 2s poll:
// - When WS is healthy: zero polling traffic. /api/snapshot is only hit
//   once on mount. (30 panels × 0.5 req/s = 15 req/s saved.)
// - When WS degrades: transparent fallback to polling — the UI keeps
//   updating without any user-visible glitch.
// - When the tab is hidden: zero traffic of any kind.
'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { useWebSocket } from './useWebSocket'
import { apiFetch } from '@/lib/api'

export interface UseRealtimeDataOptions {
  /** If provided, subscribe to this WS channel (msg.channel === wsChannel). */
  wsChannel?: string
  /** Fallback polling interval in ms (default 10s). */
  pollInterval?: number
  /** Optional initial data so the first render isn't `null`. */
  initialData?: unknown
  /**
   * Optional predicate — if it returns false, the WS message's payload is
   * dropped (data state is left untouched). Useful when the subscribed
   * channel pushes a different shape than the REST endpoint (e.g. the
   * `metrics` channel pushes a full BotSnapshot, but the consumer fetched
   * `/api/analytics` which returns an Analytics object). Without this
   * guard, the mismatched payload would clobber the typed state with
   * fields the render code doesn't expect. (W15-5)
   */
  validate?: (data: unknown) => boolean
}

export interface UseRealtimeDataResult<T> {
  data: T | null
  isLoading: boolean
  error: string | null
  /** true when the WS is live and pushing updates; false when polling. */
  isRealtime: boolean
  /**
   * W41-3 — Epoch milliseconds of the last successful data update
   * (initial REST fetch, WS push, or polling fallback). `null` until
   * the first successful update. Panels use this to compute staleness
   * (e.g. show a "stale" pill when data is older than 30s).
   */
  lastUpdated: number | null
  /**
   * W41-3 — Imperatively re-run the initial REST fetch. Used by panel
   * error-state retry buttons. Calling this clears the error state and
   * flips `isLoading` back to true until the fetch resolves.
   */
  refetch: () => void
}

export function useRealtimeData<T>(
  endpoint: string,
  options: UseRealtimeDataOptions = {},
): UseRealtimeDataResult<T> {
  const { wsChannel, pollInterval = 10000, initialData, validate } = options
  const [data, setData] = useState<T | null>(
    (initialData as T | undefined) ?? null,
  )
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  // W41-3 — lastUpdated tracks when data was last refreshed. Surfaces
  // to panels so they can render a StaleIndicator when data ages past
  // the freshness threshold.
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  // W41-3 — retryToken is bumped by `refetch()` to force the initial-
  // fetch effect to re-run. The effect's deps include retryToken so a
  // retry triggers a fresh REST fetch + clears the error state.
  const [retryToken, setRetryToken] = useState(0)
  const wsConnectedRef = useRef(false)

  // Initial REST fetch — runs once on mount (and again if `endpoint`
  // changes, which is rare). Also re-runs when `retryToken` changes so
  // the panel's retry button can trigger a fresh fetch. The `cancelled`
  // flag guards setState after unmount so we never trigger a React
  // "setState on unmounted component" warning.
  useEffect(() => {
    let cancelled = false
    const fetchData = async () => {
      try {
        const res = await apiFetch(endpoint)
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const json = await res.json()
        if (!cancelled) {
          setData(json as T)
          setError(null)
          setLastUpdated(Date.now())
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (!cancelled) setIsLoading(false)
      }
    }
    fetchData()
    return () => {
      cancelled = true
    }
  }, [endpoint, retryToken])

  // W41-3 — imperative retry. Bumps retryToken so the initial-fetch
  // effect re-runs. Re-enables the loading gate so panels can show
  // their loading state while the retry is in-flight.
  const refetch = useCallback(() => {
    setIsLoading(true)
    setError(null)
    setRetryToken((t) => t + 1)
  }, [])

  // WebSocket for real-time updates. The useWebSocket hook owns the
  // connection lifecycle (connect / reconnect / pause-on-hidden); we
  // just supply the onMessage handler that filters by channel.
  // W15-5 — when the caller provides a `validate` predicate, we drop
  // payloads that don't match the expected shape instead of clobbering
  // the typed state. This lets a panel subscribe to a channel whose
  // payload type doesn't 1:1 mirror the REST response (e.g. the
  // `metrics` channel pushes BotSnapshot, but AnalyticsPanel fetched
  // /api/analytics whose body is the Analytics object).
  // W41-3 — also stamps `lastUpdated` so panels can compute staleness.
  const { isConnected } = useWebSocket({
    onMessage: (raw) => {
      // The backend pushes JSON objects with `{ channel, data }`. The
      // shape is loose (data is per-channel) so we narrow via runtime
      // property checks instead of a Zod schema — channels we don't
      // subscribe to are silently dropped.
      const msg = raw as { channel?: string; data?: unknown }
      if (wsChannel && msg && msg.channel === wsChannel) {
        if (validate && !validate(msg.data)) return
        setData(msg.data as T)
        setError(null)
        setLastUpdated(Date.now())
      }
    },
    onConnect: () => {
      wsConnectedRef.current = true
    },
    onDisconnect: () => {
      wsConnectedRef.current = false
    },
  })

  // Fallback polling — only runs when the WS is NOT connected. The
  // effect re-runs whenever `isConnected` flips, so:
  //   - WS connects  → effect cleanup clears the existing interval.
  //   - WS drops     → effect re-runs, starts a new interval.
  // The `document.hidden` check inside the interval callback (not in the
  // deps array) means we skip individual ticks when the tab is hidden
  // without tearing down the interval — cheaper than re-creating the
  // interval on every visibilitychange.
  useEffect(() => {
    if (isConnected) return // WS is working, no need to poll
    if (typeof document !== 'undefined' && document.hidden) return // Tab hidden, skip

    const interval = setInterval(async () => {
      if (typeof document !== 'undefined' && document.hidden) return
      try {
        const res = await apiFetch(endpoint)
        if (res.ok) {
          const json = await res.json()
          setData(json as T)
          setError(null)
          setLastUpdated(Date.now())
        }
      } catch {
        // Silent fail on background poll — the next tick will retry,
        // and a persistent failure will eventually flip `error` via the
        // initial-fetch effect when the endpoint changes.
      }
    }, pollInterval)

    return () => clearInterval(interval)
  }, [endpoint, isConnected, pollInterval])

  return { data, isLoading, error, isRealtime: isConnected, lastUpdated, refetch }
}
