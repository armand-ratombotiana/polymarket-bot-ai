// components/OrderFlowPanel.tsx — Order-flow workstation.
//
// Combines three sub-components into one real-time trading view:
//
//   ┌───────────────────────────────────────────────────────────┐
//   │ [token selector ▼]  [window 30s|1m|5m]   Δ +120  ◉ Live   │
//   ├───────────────────────────────────────────────────────────┤
//   │                                                           │
//   │            OrderFlowChart (buy/sell bars + Δ line)        │
//   │                                                           │
//   ├──────────────────────────┬────────────────────────────────┤
//   │                          │                                │
//   │  OrderBookImbalance      │       TradeTape (scrolling)    │
//   │  (bid↔ask divergent bar) │                                │
//   │                          │                                │
//   └──────────────────────────┴────────────────────────────────┘
//
// Data flow:
//   • `trades` + `orderBooks` come from the parent's useBot hook
//     (WS-with-polling-fallback — see src/hooks/useBot.ts). The panel
//     accepts them as props so it doesn't open a second WS socket.
//   • The depth ladder for the SELECTED token is polled separately via
//     `/api/depth/{token_id}` every 2s — same pattern as
//     DepthChartModal. This gives the OrderBookImbalance component
//     the per-level sizes it needs to compute bid/ask volume + best
//     bid/ask depth.
//   • `isRealtime` reflects the parent's WS connection state — when
//     false, the "Polling" badge is shown so the trader knows the
//     numbers may lag by up to `pollIntervalMs` (default 2s).
//
// Stats header shows:
//   • Cumulative Δ — net buy_vol − sell_vol over the visible window.
//   • Imbalance ratio — (bidVol − askVol) / (bidVol + askVol).
//   • Tape speed — trades/min over the last 60s.

'use client'

import { useEffect, useMemo, useState, useCallback } from 'react'
import { apiFetch, getApiUrl } from '@/lib/api'
import type { Trade, OrderBook } from '@/hooks/useBot'
import OrderFlowChart, {
  type FlowTrade,
  type TimeWindow,
} from './charts/OrderFlowChart'
import OrderBookImbalance, {
  type OrderBookImbalanceProps,
  computeImbalance,
} from './charts/OrderBookImbalance'
import TradeTape from './charts/TradeTape'

interface DepthLevel {
  price: number
  size: number
  total: number
}

interface DepthData {
  token_id: string
  bids: DepthLevel[]
  asks: DepthLevel[]
  mid: number | null
  spread: number | null
  best_bid: number | null
  best_ask: number | null
}

export interface OrderFlowPanelProps {
  /** Recent trades (oldest- or newest-first; chart sorts internally). */
  trades: Trade[]
  /** Live order books (used to populate the token selector). */
  orderBooks: OrderBook[]
  /** True when the parent's WS is live; false when polling. */
  isRealtime: boolean
  /** Optional callback invoked when the user picks a token via the chart. */
  onSelectMarket?: (tokenId: string, slug: string) => void
  /** Optional className for the outer wrapper. */
  className?: string
}

const WINDOW_OPTIONS: { value: TimeWindow; label: string }[] = [
  { value: '30s', label: '30s' },
  { value: '1m', label: '1m' },
  { value: '5m', label: '5m' },
]

/**
 * Convert a bot `Trade` into the chart-friendly `FlowTrade` shape.
 * Drops trades without a token_id match — we filter at the panel level
 * so the chart receives only the selected token's prints.
 */
function toFlowTrade(t: Trade): FlowTrade {
  return {
    timestamp: t.timestamp * 1000, // bot uses seconds; chart uses ms
    side: t.side,
    size: t.size,
    price: t.price,
  }
}

export default function OrderFlowPanel({
  trades,
  orderBooks,
  isRealtime,
  onSelectMarket,
  className,
}: OrderFlowPanelProps) {
  // Selected token — defaults to the first book's token_id. When the
  // parent passes an empty order_books array (bot just booted), the
  // selector shows a placeholder and the chart shows its empty state.
  const [selectedTokenId, setSelectedTokenId] = useState<string | null>(
    orderBooks[0]?.token_id ?? null,
  )
  const [timeWindow, setTimeWindow] = useState<TimeWindow>('1m')
  const [depth, setDepth] = useState<DepthData | null>(null)

  // Re-sync `selectedTokenId` when the parent's orderBooks list changes
  // (e.g. on first snapshot landing after the panel mounts, or when a
  // market goes offline and the bot drops it from the list).
  useEffect(() => {
    if (!selectedTokenId && orderBooks.length > 0) {
      setSelectedTokenId(orderBooks[0].token_id)
      return
    }
    if (selectedTokenId && !orderBooks.some((b) => b.token_id === selectedTokenId)) {
      // Selected market dropped — fall back to the first available.
      setSelectedTokenId(orderBooks[0]?.token_id ?? null)
    }
  }, [orderBooks, selectedTokenId])

  // Poll /api/depth/{token_id} every 2s for the selected market. The
  // gateway port is injected by apiFetch (see @/lib/api). We clear stale
  // depth on token switch so the imbalance meter doesn't briefly show
  // the previous market's numbers.
  useEffect(() => {
    if (!selectedTokenId) {
      setDepth(null)
      return
    }
    setDepth(null)
    let cancelled = false
    const fetchDepth = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/depth/${selectedTokenId}`)
        if (res.ok) {
          const json: DepthData = await res.json()
          if (!cancelled) setDepth(json)
        }
      } catch {
        // Silent fail — the next 2s tick will retry.
      }
    }
    fetchDepth()
    const timer = setInterval(fetchDepth, 2000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [selectedTokenId])

  // Filter trades to the selected token. Bot trades carry `token_id`
  // directly; if it's missing (rare), we skip the trade.
  const flowTrades: FlowTrade[] = useMemo(() => {
    if (!selectedTokenId) return []
    return trades
      .filter((t) => t.token_id === selectedTokenId)
      .map(toFlowTrade)
  }, [trades, selectedTokenId])

  // Cumulative delta over the visible window — used for the top stats bar.
  // Recomputed each render from `flowTrades` (cheap; O(n) over ≤ a few
  // hundred trades).
  const cumulativeDelta = useMemo(() => {
    let d = 0
    for (const t of flowTrades) {
      d += t.side === 'BUY' ? t.size : -t.size
    }
    return d
  }, [flowTrades])

  // Trades per minute over the last 60s — the "tape speed" stat.
  const tradesPerMin = useMemo(() => {
    const now = Date.now()
    let c = 0
    for (const t of flowTrades) {
      if (t.timestamp >= now - 60_000) c += 1
    }
    return c
  }, [flowTrades])

  // Aggregate the depth ladder into the totals the imbalance meter needs.
  const imbalanceInput: OrderBookImbalanceProps = useMemo(() => {
    const bids = depth?.bids ?? []
    const asks = depth?.asks ?? []
    const bidVolume = bids.reduce((s, l) => s + (l.size || 0), 0)
    const askVolume = asks.reduce((s, l) => s + (l.size || 0), 0)
    // Best bid = highest bid price; best ask = lowest ask price. The
    // depth ladder from the bot is sorted ascending by price, so:
    //   best bid size = last bid entry's size
    //   best ask size = first ask entry's size
    // But we use the best_bid/best_ask fields from the response
    // (already computed server-side) for the price chips.
    const bestBidSize = bids.length > 0 ? bids[bids.length - 1]?.size ?? null : null
    const bestAskSize = asks.length > 0 ? asks[0]?.size ?? null : null
    return {
      bidVolume,
      askVolume,
      bestBidSize,
      bestAskSize,
      mid: depth?.mid ?? null,
      bestBid: depth?.best_bid ?? null,
      bestAsk: depth?.best_ask ?? null,
      spread: depth?.spread ?? null,
    }
  }, [depth])

  const imbalanceRatio = useMemo(
    () => computeImbalance(imbalanceInput.bidVolume, imbalanceInput.askVolume),
    [imbalanceInput.bidVolume, imbalanceInput.askVolume],
  )

  const selectedBook = useMemo(
    () => orderBooks.find((b) => b.token_id === selectedTokenId) ?? null,
    [orderBooks, selectedTokenId],
  )

  const handleTokenChange = useCallback(
    (e: React.ChangeEvent<HTMLSelectElement>) => {
      const id = e.target.value || null
      setSelectedTokenId(id)
      if (id && selectedBook && onSelectMarket) {
        // Surface the selection to the parent so it can open the
        // depth modal / chart modal in a follow-up UX iteration.
        const book = orderBooks.find((b) => b.token_id === id)
        if (book) onSelectMarket(book.token_id, book.slug)
      }
    },
    [onSelectMarket, orderBooks, selectedBook],
  )

  // Cumulative-delta color — green when positive, red when negative,
  // muted when zero.
  const deltaColor =
    cumulativeDelta > 0
      ? 'text-green-400'
      : cumulativeDelta < 0
        ? 'text-red-400'
        : 'text-[#7e8aaa]'

  return (
    <div
      className={`flex flex-col gap-3 h-full ${className ?? ''}`}
      data-testid="order-flow-panel"
      role="region"
      aria-label="Order flow panel"
    >
      {/* Top control bar: token selector + window selector + stats */}
      <div
        className="card bg-[#13161e] border border-[#1f2335] shadow-md p-3"
        data-testid="order-flow-panel-header"
      >
        <div className="flex flex-wrap items-center gap-3 justify-between">
          {/* Token selector */}
          <div className="flex items-center gap-2 min-w-0">
            <label
              htmlFor="ofp-token-select"
              className="text-[10px] uppercase font-bold text-[#7e8aaa] flex-shrink-0"
            >
              Token
            </label>
            <select
              id="ofp-token-select"
              value={selectedTokenId ?? ''}
              onChange={handleTokenChange}
              className="bg-[#0e1015] border border-[#1f2335] text-[#dde1ed] text-xs rounded px-2 py-1 mono max-w-[260px] truncate focus:outline-none focus:border-blue-500"
              aria-label="Select market token"
              data-testid="order-flow-token-select"
            >
              {orderBooks.length === 0 && (
                <option value="">No markets available</option>
              )}
              {orderBooks.map((b) => (
                <option key={b.token_id} value={b.token_id}>
                  {b.slug || b.token_id.slice(0, 16)}
                </option>
              ))}
            </select>
          </div>

          {/* Window selector */}
          <div
            className="flex items-center gap-1"
            role="group"
            aria-label="Time window"
          >
            {WINDOW_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setTimeWindow(opt.value)}
                className={`px-2 py-1 rounded text-[10px] mono font-bold border transition-colors ${
                  timeWindow === opt.value
                    ? 'bg-blue-500/20 text-cyan-300 border-blue-500/50'
                    : 'bg-[#0e1015] text-[#7e8aaa] border-[#1f2335] hover:text-white'
                }`}
                aria-pressed={timeWindow === opt.value}
                data-testid={`order-flow-window-${opt.value}`}
              >
                {opt.label}
              </button>
            ))}
          </div>

          {/* Stats badges */}
          <div className="flex items-center gap-3 text-[10px]">
            <div className="bg-[#0e1015] border border-[#1f2335] rounded px-2 py-1">
              <span className="text-[#7e8aaa] uppercase">Δ </span>
              <span className={`mono font-bold ${deltaColor}`}>
                {cumulativeDelta >= 0 ? '+' : ''}{cumulativeDelta.toFixed(1)}
              </span>
            </div>
            <div className="bg-[#0e1015] border border-[#1f2335] rounded px-2 py-1">
              <span className="text-[#7e8aaa] uppercase">Imb </span>
              <span
                className={`mono font-bold ${
                  Math.abs(imbalanceRatio) < 0.1
                    ? 'text-amber-400'
                    : imbalanceRatio > 0
                      ? 'text-green-400'
                      : 'text-red-400'
                }`}
              >
                {(imbalanceRatio * 100).toFixed(0)}%
              </span>
            </div>
            <div className="bg-[#0e1015] border border-[#1f2335] rounded px-2 py-1">
              <span className="text-[#7e8aaa] uppercase">Tape </span>
              <span className="mono font-bold text-[#dde1ed]">
                {tradesPerMin}/min
              </span>
            </div>
            {/* Live / Polling badge */}
            <div
              className={`mono text-[10px] px-2 py-1 rounded border flex items-center gap-1.5 ${
                isRealtime
                  ? 'border-green-500/50 text-green-400 bg-green-500/10'
                  : 'border-amber-500/50 text-amber-400 bg-amber-500/10'
              }`}
              role="status"
              data-testid="order-flow-realtime-badge"
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  isRealtime ? 'bg-green-400 animate-pulse' : 'bg-amber-400'
                }`}
                aria-hidden="true"
              />
              {isRealtime ? 'LIVE' : 'POLL'}
            </div>
          </div>
        </div>
      </div>

      {/* Order flow chart — full width */}
      <div
        className="card bg-[#13161e] border border-[#1f2335] shadow-md p-3"
        data-testid="order-flow-chart-card"
      >
        <div className="text-[10px] font-bold uppercase text-[#7e8aaa] mb-1.5">
          Order Flow — buys vs sells + cumulative Δ
        </div>
        <OrderFlowChart
          trades={flowTrades}
          window={timeWindow}
          height={240}
        />
      </div>

      {/* Bottom grid: imbalance (left) + tape (right) */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 flex-1 min-h-0">
        <div
          className="card bg-[#13161e] border border-[#1f2335] shadow-md p-3"
          data-testid="order-flow-imbalance-card"
        >
          <div className="text-[10px] font-bold uppercase text-[#7e8aaa] mb-1.5">
            Bid / Ask Imbalance
          </div>
          <OrderBookImbalance {...imbalanceInput} />
        </div>
        <div
          className="card bg-[#13161e] border border-[#1f2335] shadow-md p-3"
          data-testid="order-flow-tape-card"
        >
          <div className="text-[10px] font-bold uppercase text-[#7e8aaa] mb-1.5">
            Time & Sales
          </div>
          <TradeTape trades={flowTrades} height={320} />
        </div>
      </div>
    </div>
  )
}
