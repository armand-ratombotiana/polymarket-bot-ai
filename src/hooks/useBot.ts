// hooks/useBot.ts
// Central hook — connects to the bot API WebSocket with automatic REST fallback.
'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { getApiUrl, getAuthedWsUrl, authHeaders } from '@/lib/api'

export interface OrderBook {
  token_id: string
  slug: string
  best_bid: number | null
  best_ask: number | null
  mid: number | null
  spread: number | null
  updated_at: number
}

export interface Order {
  order_id: string
  token_id: string
  slug: string
  side: 'BUY' | 'SELL'
  price: number
  size: number
  size_matched: number
  strategy: string
  paper: boolean
  created_at: number
  // W39-5 — optional order lifecycle status. When absent, the UI derives
  // a display status from `size_matched` / `size` (matched === size →
  // FILLED; matched > 0 → OPEN/partial; matched === 0 → OPEN).
  status?: 'PENDING' | 'OPEN' | 'FILLED' | 'CANCELLED' | 'REJECTED'
}

export interface Position {
  token_id: string
  slug: string
  yes_shares: number
  no_shares?: number
  avg_entry_price: number
  total_invested: number
  realised_pnl: number
  // S1 — live mark-to-market fields (optional, additive).
  // Populated by snapshot when the backend exposes current_price + unrealized_pnl.
  current_price?: number
  unrealized_pnl?: number
  // W39-5 — optional metadata fields used by the redesigned positions table:
  //   • strategy   — the strategy that opened the position (rendered as a badge)
  //   • opened_at  — epoch-seconds timestamp when the position was opened
  //                  (rendered as a "Time held" column with "3h 24m" format)
  //   • risk_status — explicit risk-engine classification (when absent, the
  //                  UI derives a display status from unrealized_pnl magnitude).
  strategy?: string
  opened_at?: number
  risk_status?: 'healthy' | 'warning' | 'danger'
}

export interface Trade {
  trade_id: string
  token_id?: string
  slug: string
  side: 'BUY' | 'SELL'
  price: number
  size: number
  pnl: number
  strategy: string
  paper: boolean
  timestamp: number
  // W39-5 — optional execution-quality + audit fields surfaced by the
  // redesigned trades table:
  //   • fee          — USDC fee paid on this fill (rendered as "Fee" column)
  //   • slippage_bps — slippage vs. the quoted mid, in basis points
  //                    (rendered as "Slippage" column with bps suffix)
  //   • decision_id  — links the fill to the Decision Ledger stage-chain
  //                    (rendered as an audit-trail link icon)
  fee?: number
  slippage_bps?: number
  decision_id?: string
}

export interface MLState {
  model_ready: boolean
  brier_score: number
  roc_auc: number
  ece: number
  n_updates: number
  drift_status: string
  drift_psi: number
  drift_brier: number | null
  drift_ewma_brier: number | null
  adaptive_weights: { rf: number; gb: number; sgd: number; lgbm: number }
  meta_learner_warm: boolean
  training_source: string
}

export interface BotSnapshot {
  type: string
  timestamp: number
  mode: 'paper' | 'live' | 'shadow' | 'backtest' | string
  kill_switch: boolean
  kill_switch_durable: boolean
  observation_only: boolean
  observation_reason: string
  daily_pnl: number
  paper_balance: number | null
  strategies: string[]
  order_books: OrderBook[]
  open_orders: Order[]
  positions: Position[]
  recent_trades: Trade[]
  events: string[]
  ml?: MLState
}

const DEFAULT_SNAPSHOT: BotSnapshot = {
  type: 'snapshot',
  timestamp: 0,
  mode: 'paper',
  kill_switch: false,
  kill_switch_durable: false,
  observation_only: false,
  observation_reason: '',
  daily_pnl: 0,
  paper_balance: null,
  strategies: [],
  order_books: [],
  open_orders: [],
  positions: [],
  recent_trades: [],
  events: [],
}

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

// W15-2 — Optional runtime config for useBot. Currently only the REST
// polling cadence is configurable; `autoRefresh=false` is honoured at
// the call-site (page.tsx) by simply not invoking the hook with a
// polling interval. Both fields default to the prior hardcoded
// values so existing call sites are unaffected.
export interface UseBotOptions {
  /** REST fallback polling interval, ms. Defaults to 2000. The
   *  WebSocket connection is unaffected — this only gates the
   *  setInterval fallback that fires when the WS is down or has
   *  not yet connected. */
  refreshIntervalMs?: number
}

export function useBot(opts?: UseBotOptions) {
  // W15-2 — preferences-driven polling cadence. Defaults to 2s so
  // every existing call site (and existing tests that don't pass
  // options) keeps the historical behaviour.
  const refreshIntervalMs = opts?.refreshIntervalMs ?? 2000
  // W15-5 — heartbeat cadence when the WS is healthy. The WS pushes
  // every state change, but a silent socket death (NAT timeout, server
  // restart without a clean close frame) would leave us stuck without
  // an `onclose` event. A heartbeat poll every 10s reconciles the
  // snapshot via REST so we can detect such stalls. The interval fires
  // every `Math.max(1, round(10000 / refreshIntervalMs))` ticks when
  // the WS is up; when the WS is down, every tick fires (the original
  // 2s polling fallback behaviour).
  const heartbeatTicksPerCycle = Math.max(1, Math.round(10000 / refreshIntervalMs))
  const [snapshot, setSnapshot] = useState<BotSnapshot>(DEFAULT_SNAPSHOT)
  const [status, setStatus] = useState<ConnectionStatus>('connecting')
  // W15-5 — exposed to callers so they can drive Live/Polling badges
  // without having to track the WS lifecycle themselves. The ref is
  // the source of truth (synchronously readable inside the WS event
  // handlers); the state mirrors it for re-render purposes.
  const [wsConnected, setWsConnected] = useState(false)
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const restPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const isWsConnectedRef = useRef(false)
  // W15-5 — counts ticks since the last heartbeat fire. Reset to 0
  // whenever the WS drops (so the first poll after a WS outage is
  // immediate, not delayed by a partial heartbeat cycle).
  const heartbeatTickRef = useRef(0)

  // U11 — Price-flash tracking state.
  // prevMidsRef holds the last-seen mid price per token_id so each incoming
  // snapshot can be diffed against the prior mid. flashTimersRef holds the
  // per-token 500ms clear timers so a re-triggered flash refreshes the clear
  // window rather than firing early. priceFlashes is the public state that
  // components consume to apply .price-up / .price-down CSS classes.
  const prevMidsRef = useRef<Record<string, number>>({})
  const flashTimersRef = useRef<Record<string, ReturnType<typeof setTimeout>>>({})
  const [priceFlashes, setPriceFlashes] = useState<Record<string, 'up' | 'down'>>({})

  // Direct REST fetch to populate data immediately or on fallback.
  //
  // W9-6 — Stale-while-revalidate behaviour: this fetcher never sets a
  // "loading" flag. It always preserves the current `snapshot` state in
  // the UI while the network request is in-flight, then atomically swaps
  // in the new snapshot on success. This avoids a flash of empty content
  // on every 2s poll (the previous data remains visible until the new
  // data arrives). The only state that can change is `status` — and even
  // that only flips on the FIRST successful connect (guarded by
  // `isWsConnectedRef`); subsequent polls leave `status` untouched so the
  // status bar doesn't flicker.
  //
  // The fallback composite fetch already batches all six sub-endpoints via
  // `Promise.all` — they all run concurrently rather than sequentially,
  // so worst-case latency is `max(t_book, t_status, t_events, t_orders,
  // t_positions, t_trades)` rather than the sum.
  const fetchRestSnapshot = useCallback(async () => {
    const apiUrl = getApiUrl()
    try {
      const res = await fetch(`${apiUrl}/api/snapshot`, { headers: authHeaders() })
      if (res.ok) {
        const data = await res.json()
        setSnapshot(data)
        if (!isWsConnectedRef.current) {
          setStatus('connected')
        }
        return true
      }
    } catch {
      // If /api/snapshot is not yet available, fallback to composite fetch
      try {
        const [booksRes, statusRes, eventsRes, ordersRes, posRes, tradesRes] = await Promise.all([
          fetch(`${apiUrl}/api/orderbooks`, { headers: authHeaders() }).catch(() => null),
          fetch(`${apiUrl}/api/status`, { headers: authHeaders() }).catch(() => null),
          fetch(`${apiUrl}/api/events`, { headers: authHeaders() }).catch(() => null),
          fetch(`${apiUrl}/api/orders`, { headers: authHeaders() }).catch(() => null),
          fetch(`${apiUrl}/api/positions`, { headers: authHeaders() }).catch(() => null),
          fetch(`${apiUrl}/api/trades`, { headers: authHeaders() }).catch(() => null),
        ])

        const booksData = booksRes?.ok ? await booksRes.json() : { order_books: [] }
        const statusData = statusRes?.ok ? await statusRes.json() : {}
        const eventsData = eventsRes?.ok ? await eventsRes.json() : { events: [] }
        const ordersData = ordersRes?.ok ? await ordersRes.json() : { orders: [] }
        const posData = posRes?.ok ? await posRes.json() : { positions: [] }
        const tradesData = tradesRes?.ok ? await tradesRes.json() : { trades: [] }

        setSnapshot({
          type: 'snapshot',
          timestamp: Date.now() / 1000,
          mode: statusData.mode || 'paper',
          kill_switch: Boolean(statusData.kill_switch),
          kill_switch_durable: Boolean(statusData.kill_switch_durable),
          observation_only: Boolean(statusData.observation_only),
          observation_reason: statusData.observation_reason || '',
          daily_pnl: statusData.daily_pnl || 0,
          paper_balance: statusData.paper_balance ?? 100,
          strategies: statusData.strategies || [],
          order_books: booksData.order_books || [],
          open_orders: ordersData.orders || [],
          positions: posData.positions || [],
          recent_trades: tradesData.trades || [],
          events: eventsData.events || [],
        })
        if (!isWsConnectedRef.current) {
          setStatus('connected')
        }
        return true
      } catch {
        if (!isWsConnectedRef.current) {
          setStatus('disconnected')
        }
      }
    }
    return false
  }, [])

  const connectRef = useRef<() => void>(() => {})

  const connect = useCallback(() => {
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return
    }

    const wsUrl = getAuthedWsUrl()
    try {
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        isWsConnectedRef.current = true
        // W15-5 — mirror to state so consumers (page.tsx, ConnectionStatus)
        // can re-render their Live/Polling badges when the transport flips.
        setWsConnected(true)
        // Reset the heartbeat counter so the first heartbeat fires 10s
        // after a fresh connect rather than mid-cycle.
        heartbeatTickRef.current = 0
        setStatus('connected')
      }

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as BotSnapshot
          if (data && data.order_books) {
            setSnapshot(data)
          }
        } catch {}
      }

      ws.onerror = () => {
        isWsConnectedRef.current = false
        setWsConnected(false)
        // Let fallback polling handle data
      }

      ws.onclose = () => {
        isWsConnectedRef.current = false
        setWsConnected(false)
        // Reset the heartbeat counter so the first REST poll after the
        // WS drop fires immediately rather than waiting for the next
        // heartbeat cycle.
        heartbeatTickRef.current = 0
        if (retryRef.current) clearTimeout(retryRef.current)
        retryRef.current = setTimeout(() => {
          connectRef.current()
        }, 3000)
      }
    } catch {
      isWsConnectedRef.current = false
      setWsConnected(false)
    }
  }, [])

  connectRef.current = connect

  useEffect(() => {
    // 1. Fetch data immediately on mount via REST
    fetchRestSnapshot()

    // 2. Start WebSocket connection
    connect()

    // 3. Keep REST polling every `refreshIntervalMs` as a reliable fallback/refresh
    //    (W15-2 — interval is configurable via preferences, default 2s).
    //    W15-5 — when the WS is healthy, only fire a heartbeat every
    //    `heartbeatTicksPerCycle` ticks (~10s) instead of every tick. This
    //    catches silent socket deaths (NAT timeouts, server restarts
    //    without a clean close frame) without re-introducing the 2s polling
    //    load that the WS was supposed to eliminate. When the WS is down,
    //    every tick fires (the original 2s polling fallback behaviour).
    restPollRef.current = setInterval(() => {
      if (isWsConnectedRef.current) {
        heartbeatTickRef.current = (heartbeatTickRef.current + 1) % heartbeatTicksPerCycle
        if (heartbeatTickRef.current !== 0) return
        fetchRestSnapshot()
      } else {
        fetchRestSnapshot()
      }
    }, refreshIntervalMs)

    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (retryRef.current) clearTimeout(retryRef.current)
      if (restPollRef.current) clearInterval(restPollRef.current)
    }
  }, [connect, fetchRestSnapshot, refreshIntervalMs, heartbeatTicksPerCycle])

  // U11 — Derive price-flash directions on each new order_books snapshot.
  // For every token whose mid price moved relative to the prior snapshot,
  // record 'up' or 'down' in priceFlashes and (re)schedule a 500ms clear.
  // The first snapshot after mount establishes the baseline (no flashes),
  // because there is no previous mid to diff against.
  useEffect(() => {
    const books = snapshot.order_books
    if (!Array.isArray(books) || books.length === 0) return

    const prevMids = prevMidsRef.current
    const nextMids: Record<string, number> = {}
    const newFlashes: Record<string, 'up' | 'down'> = {}

    for (const book of books) {
      const tokenId = book.token_id
      const mid = book.mid
      if (typeof tokenId !== 'string' || typeof mid !== 'number' || Number.isNaN(mid)) {
        continue
      }
      nextMids[tokenId] = mid
      const prevMid = prevMids[tokenId]
      if (typeof prevMid === 'number' && !Number.isNaN(prevMid)) {
        if (mid > prevMid) {
          newFlashes[tokenId] = 'up'
        } else if (mid < prevMid) {
          newFlashes[tokenId] = 'down'
        }
      }
    }

    // Persist the latest mids as the new baseline for the next diff.
    prevMidsRef.current = nextMids

    const tokenIds = Object.keys(newFlashes)
    if (tokenIds.length === 0) return

    // Merge new flashes into existing state — preserves overlapping flashes
    // from a prior snapshot that are still within their 500ms window, while
    // overwriting the direction for tokens that just ticked again.
    setPriceFlashes((prev) => {
      const merged = { ...prev }
      for (const tokenId of tokenIds) {
        merged[tokenId] = newFlashes[tokenId]
      }
      return merged
    })

    // (Re)schedule the 500ms clear per token. Re-triggered flashes refresh
    // the clear window so the CSS class persists for a full 500ms after
    // the most recent tick.
    for (const tokenId of tokenIds) {
      const existing = flashTimersRef.current[tokenId]
      if (existing) clearTimeout(existing)
      flashTimersRef.current[tokenId] = setTimeout(() => {
        setPriceFlashes((prev) => {
          if (!(tokenId in prev)) return prev
          const next = { ...prev }
          delete next[tokenId]
          return next
        })
        delete flashTimersRef.current[tokenId]
      }, 500)
    }
  }, [snapshot.order_books])

  // U11 — On unmount, clear any pending flash timers to avoid
  // setState-after-unmount warnings / leaked timers.
  useEffect(() => {
    return () => {
      const timers = flashTimersRef.current
      for (const tokenId of Object.keys(timers)) {
        const timer = timers[tokenId]
        if (timer) clearTimeout(timer)
      }
    }
  }, [])

  // Actions
  // W9-6 — wrap mutation actions in useCallback. Without useCallback these
  // functions get new identities on every render, which means:
  //   1. Any child component receiving them as props (PositionsPanel's
  //      onClosePosition, OrdersPanel's onCancel) bypasses React.memo on
  //      every parent re-render — defeating the memoization we added to
  //      those panels.
  //   2. Effects in this hook that list them as deps re-run on every
  //      snapshot (none currently do, but it's a footgun for future edits).
  // The callbacks only close over `fetchRestSnapshot` (already useCallback-
  // wrapped above) and stable imports, so the dependency array is stable.
  const activateKillSwitch = useCallback(async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/kill-switch/activate`, { method: 'POST', headers: authHeaders() }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])

  const deactivateKillSwitch = useCallback(async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/kill-switch/deactivate`, { method: 'POST', headers: authHeaders() }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])

  const cancelAllOrders = useCallback(async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/orders`, { method: 'DELETE', headers: authHeaders() }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])

  const cancelOrder = useCallback(async (orderId: string) => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/orders/${orderId}`, { method: 'DELETE', headers: authHeaders() }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])

  // S1 — Close a single position by token_id. POSTs to the backend close
  // endpoint and refreshes the snapshot. Failures are swallowed so the UI
  // remains responsive; the next REST poll will reconcile state.
  // W9-6 — wrapped in useCallback for stable identity (see comment above).
  const closePosition = useCallback(async (tokenId: string) => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/positions/${tokenId}/close`, { method: 'POST', headers: authHeaders() }).catch(() => {})
    fetchRestSnapshot()
  }, [fetchRestSnapshot])

  // W9-6 — Visibility-aware REST polling.
  // Background polling (the 2s `setInterval`) is the WebSocket fallback.
  // When the document is hidden (user switched tabs / minimized the window),
  // we pause the interval so we don't:
  //   - Burn backend quota on tabs nobody is looking at.
  //   - Trigger React re-render storms on every tick (the parent
  //     `app-shell` re-renders on every snapshot — a hidden tab doing this
  //     30 times a minute wastes CPU).
  // The WebSocket stays open; if the server pushes critical updates
  // (kill_switch, fills), the snapshot still updates — only the REST
  // polling is gated.
  useEffect(() => {
    if (typeof document === 'undefined') return
    const onVis = () => {
      if (document.hidden) {
        if (restPollRef.current) {
          clearInterval(restPollRef.current)
          restPollRef.current = null
        }
      } else {
        // Tab became visible — immediately fetch (in case state changed
        // while hidden) and resume the polling cadence.
        // W15-2 — uses the preferences-driven `refreshIntervalMs` so a
        // trader who dialled the polling down to e.g. 5s sees that
        // cadence preserved across visibility changes.
        // W15-5 — same heartbeat logic as the mount-time interval: when
        // the WS is healthy, only fire every Nth tick (~10s); when the
        // WS is down, fire every tick.
        if (!restPollRef.current) {
          fetchRestSnapshot()
          restPollRef.current = setInterval(() => {
            if (isWsConnectedRef.current) {
              heartbeatTickRef.current = (heartbeatTickRef.current + 1) % heartbeatTicksPerCycle
              if (heartbeatTickRef.current !== 0) return
              fetchRestSnapshot()
            } else {
              fetchRestSnapshot()
            }
          }, refreshIntervalMs)
        }
      }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [fetchRestSnapshot, refreshIntervalMs, heartbeatTicksPerCycle])

  return {
    snapshot,
    status,
    // W15-5 — true when the WebSocket is open and pushing BotSnapshot
    // updates. Callers use this to drive Live/Polling badges (e.g.
    // page.tsx threads it into PositionsPanel / OrdersPanel as the
    // `isRealtime` prop, so the badge reflects the bot's transport
    // state rather than the panel's own WS subscription).
    wsConnected,
    priceFlashes,
    activateKillSwitch,
    deactivateKillSwitch,
    cancelAllOrders,
    cancelOrder,
    closePosition,
  }
}
