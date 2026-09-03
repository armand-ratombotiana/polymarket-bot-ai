// components/charts/OrderBookImbalance.tsx — Bid/ask imbalance meter.
//
// Renders the live bid vs ask volume as a divergent horizontal bar:
//
//   bid vol ◄─────────────┼─────────────► ask vol
//   (green, grows right→ │ ←left grows red)
//                       mid
//
// Both sides grow outward from the center divider (the mid price). The
// wider the green side, the more resting bid liquidity is sitting under
// the market; the wider the red side, the heavier the ask wall above.
// The numeric imbalance ratio `(bid − ask) / (bid + ask)` ranges
// from −1 (all asks) to +1 (all bids); the meter drives the centre
// tick left or right of true-centre based on this ratio.
//
// The component is intentionally lightweight (no Recharts dependency)
// — a pair of flex divs whose widths are percentages of the combined
// volume, plus a centred mid-price badge. This keeps re-render cost
// minimal at the WS tick rate (10–50 ms).
//
// Inputs:
//   • bidVolume: total size across the bid ladder (sum of sizes).
//   • askVolume: total size across the ask ladder.
//   • bestBidSize: depth at the top-of-book bid (best bid level).
//   • bestAskSize: depth at the top-of-book ask.
//   • mid: best-bid + best-ask midpoint.
//   • spread: best_ask − best_bid.
//   • priceFormat: optional formatter (default 3dp probability).

'use client'

import { useMemo } from 'react'
import { chartTheme } from './theme'

export interface OrderBookImbalanceProps {
  /** Total bid volume across the visible ladder. */
  bidVolume: number
  /** Total ask volume across the visible ladder. */
  askVolume: number
  /** Depth at the top-of-book bid (best bid level size). */
  bestBidSize?: number | null
  /** Depth at the top-of-book ask (best ask level size). */
  bestAskSize?: number | null
  /** Mid price (for the centre badge). */
  mid?: number | null
  /** Best bid price (for the chip on the bid side). */
  bestBid?: number | null
  /** Best ask price (for the chip on the ask side). */
  bestAsk?: number | null
  /** Spread (best_ask − best_bid). */
  spread?: number | null
  /** Format a price for display. Default: 3dp probability. */
  priceFormat?: (v: number) => string
  /** Override the bid color. */
  bidColor?: string
  /** Override the ask color. */
  askColor?: string
  /** Override the mid color. */
  midColor?: string
  /** Optional className for the outer wrapper. */
  className?: string
}

function defaultFormatPrice(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

function formatSize(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)}k`
  return v.toFixed(1)
}

/**
 * Compute the imbalance ratio `(bid − ask) / (bid + ask)`.
 * Returns 0 when both sides are zero (avoid divide-by-zero).
 * Range: [−1, +1]. Positive = bid-heavy, negative = ask-heavy.
 */
export function computeImbalance(bid: number, ask: number): number {
  if (!Number.isFinite(bid) || !Number.isFinite(ask)) return 0
  const denom = bid + ask
  if (denom <= 0) return 0
  const r = (bid - ask) / denom
  // Clamp to [−1, +1] — float error can drift slightly outside.
  if (r > 1) return 1
  if (r < -1) return -1
  return r
}

export default function OrderBookImbalance({
  bidVolume,
  askVolume,
  bestBidSize = null,
  bestAskSize = null,
  mid = null,
  bestBid = null,
  bestAsk = null,
  spread = null,
  priceFormat = defaultFormatPrice,
  bidColor = chartTheme.colors.success,
  askColor = chartTheme.colors.danger,
  midColor = chartTheme.colors.warning,
  className,
}: OrderBookImbalanceProps) {
  const imbalance = useMemo(
    () => computeImbalance(bidVolume, askVolume),
    [bidVolume, askVolume],
  )

  // Bar widths as percentage of the combined volume. When both sides
  // are zero we render a 50/50 split so the centre tick stays visible.
  const total = bidVolume + askVolume
  const bidPct = total > 0 ? (bidVolume / total) * 100 : 50
  const askPct = total > 0 ? (askVolume / total) * 100 : 50

  // Imbalance chip color: green when bid-heavy, red when ask-heavy,
  // amber when balanced (|imbalance| < 0.1).
  const imbalanceColor =
    Math.abs(imbalance) < 0.1
      ? chartTheme.colors.warning
      : imbalance > 0
        ? bidColor
        : askColor

  const spreadCents = spread != null ? spread * 100 : null

  return (
    <div
      className={`bg-[#0e1015] border border-[#1f2335] rounded p-3 ${className ?? ''}`}
      data-testid="order-book-imbalance"
      role="group"
      aria-label={`Order book imbalance: bid ${formatSize(bidVolume)}, ask ${formatSize(askVolume)}, imbalance ${imbalance.toFixed(2)}`}
    >
      {/* Header — imbalance ratio + spread + depth stats */}
      <div className="flex items-center justify-between mb-2 text-[10px] uppercase text-[#7e8aaa] font-bold">
        <span>Imbalance</span>
        <span className="mono">
          {spreadCents != null ? `spread ${spreadCents.toFixed(2)}¢` : 'spread —'}
        </span>
      </div>

      {/* Numeric imbalance ratio chip */}
      <div className="flex items-baseline justify-between mb-2">
        <div
          className="mono font-bold text-lg leading-none"
          style={{ color: imbalanceColor }}
          data-testid="imbalance-ratio"
        >
          {imbalance >= 0 ? '+' : ''}{(imbalance * 100).toFixed(1)}%
        </div>
        <div className="text-[10px] text-[#7e8aaa] mono">
          {imbalance > 0.1 ? 'bid-heavy' : imbalance < -0.1 ? 'ask-heavy' : 'balanced'}
        </div>
      </div>

      {/* Divergent horizontal bar — bid grows right→ from centre, ask grows ←left. */}
      <div
        className="relative w-full h-6 rounded overflow-hidden border border-[#1f2335] flex"
        data-testid="imbalance-bar"
        aria-hidden="true"
      >
        {/* Bid half (left) — green, width = bidPct% */}
        <div
          className="h-full flex items-center justify-start pl-2"
          style={{
            width: `${bidPct}%`,
            background: `linear-gradient(90deg, ${bidColor}cc 0%, ${bidColor}66 100%)`,
          }}
        >
          <span className="text-[9px] mono text-white/90 font-semibold">
            {formatSize(bidVolume)}
          </span>
        </div>
        {/* Ask half (right) — red, width = askPct% */}
        <div
          className="h-full flex items-center justify-end pr-2"
          style={{
            width: `${askPct}%`,
            background: `linear-gradient(270deg, ${askColor}cc 0%, ${askColor}66 100%)`,
          }}
        >
          <span className="text-[9px] mono text-white/90 font-semibold">
            {formatSize(askVolume)}
          </span>
        </div>
        {/* Centre tick — the mid price anchor */}
        <div
          className="absolute top-0 bottom-0 left-1/2 -translate-x-1/2 w-0.5"
          style={{ background: midColor, opacity: 0.85 }}
        />
      </div>

      {/* Mid-price badge centred under the bar */}
      {mid != null && Number.isFinite(mid) && (
        <div
          className="text-center mt-1.5 text-[11px] mono font-semibold"
          style={{ color: midColor }}
          data-testid="imbalance-mid"
        >
          mid {priceFormat(mid)}
        </div>
      )}

      {/* Depth at best bid/ask */}
      <div className="grid grid-cols-2 gap-2 mt-2 text-[10px]">
        <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1">
          <div className="text-[9px] uppercase text-[#7e8aaa]">Best Bid</div>
          <div className="mono font-semibold" style={{ color: bidColor }}>
            {bestBid != null ? priceFormat(bestBid) : '—'}
          </div>
          <div className="mono text-[9px] text-[#7e8aaa]">
            depth {bestBidSize != null ? formatSize(bestBidSize) : '—'}
          </div>
        </div>
        <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1">
          <div className="text-[9px] uppercase text-[#7e8aaa]">Best Ask</div>
          <div className="mono font-semibold" style={{ color: askColor }}>
            {bestAsk != null ? priceFormat(bestAsk) : '—'}
          </div>
          <div className="mono text-[9px] text-[#7e8aaa]">
            depth {bestAskSize != null ? formatSize(bestAskSize) : '—'}
          </div>
        </div>
      </div>
    </div>
  )
}
