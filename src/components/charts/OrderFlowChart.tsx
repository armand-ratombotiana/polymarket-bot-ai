// components/charts/OrderFlowChart.tsx — Real-time order-flow visualization.
//
// Renders the per-trade buy/sell volume as diverging bars (buys grow up
// from the zero line in green, sells grow down in red) with a cumulative
// delta line overlaid on a secondary Y-axis. The delta line traces
// net buying pressure (cumulative buy_vol − cumulative sell_vol) —
// a rising green line means the market is being lifted by aggressive
// buyers; a falling red line means aggressive sellers are pressing it
// down.
//
//      ▲ volume
//      │  ▮       ╱─── cumulative delta
//      │ ▮▮▮     ╱
//      ┼─────────●───────► time
//      │ ▮▮    ╲
//      │  ▮     ╲───
//      │ sells (red)
//
// Data shape: each `FlowTrade` is one printed trade. The chart
// downsamples to the configured maxBars (default 60) by keeping the
// most-recent N trades within the time window — older trades scroll
// off the left edge.
//
// Time window: 30s / 1m / 5m. Trades whose `timestamp` is older than
// `now − windowMs` are filtered out before render. `now` defaults to
// `Date.now()` but can be overridden via prop (used by the test suite
// for deterministic snapshots).
//
// Tooltip shows: timestamp, side, size, price, delta (cumulative at
// that point in time).

'use client'

import { useMemo } from 'react'
import {
  ComposedChart,
  Bar,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
} from 'recharts'
import {
  chartTheme,
  tooltipStyle,
  axisProps,
  gridProps,
  tooltipCursor,
} from './theme'

/** Side enum — matches the bot's Trade.side field ('BUY' | 'SELL'). */
export type FlowSide = 'BUY' | 'SELL'

/** One printed trade in the order flow. */
export interface FlowTrade {
  /** Unix milliseconds. */
  timestamp: number
  /** Trade direction. */
  side: FlowSide
  /** Trade size (USDC or shares — same units the bot emits). */
  size: number
  /** Execution price (probability 0..1 for prediction markets). */
  price: number
}

/** Aggregated row the chart renders. Built by `buildChartData` below. */
export interface FlowRow {
  /** Original timestamp (ms) — used as the X-axis domain. */
  ts: number
  /** Display label for the X-axis tick (HH:MM:SS). */
  label: string
  /** Buy volume at this tick (positive; 0 if the trade was a sell). */
  buyVol: number
  /** Sell volume at this tick (negative for diverging-bar layout; 0 if buy). */
  sellVol: number
  /** Cumulative delta = sum(buyVol) − sum(sellVol) up to + including this row. */
  delta: number
  /** Original trade metadata surfaced in the tooltip. */
  side: FlowSide
  size: number
  price: number
}

/** Supported time-window presets. */
export type TimeWindow = '30s' | '1m' | '5m'

const WINDOW_MS: Record<TimeWindow, number> = {
  '30s': 30_000,
  '1m': 60_000,
  '5m': 300_000,
}

export interface OrderFlowChartProps {
  /** Recent trades, oldest-first or newest-first — the chart sorts internally. */
  trades: FlowTrade[]
  /** Time window. Trades older than now−window are filtered out. Default '1m'. */
  window?: TimeWindow
  /** Maximum bars to render (most-recent N within the window). Default 60. */
  maxBars?: number
  /** Container height in px. Default 240. */
  height?: number
  /** Override `Date.now()` — used by tests for deterministic output. */
  now?: number
  /** Override the buy color. */
  buyColor?: string
  /** Override the sell color. */
  sellColor?: string
  /** Override the delta-line color. */
  deltaColor?: string
  /** Show the cumulative-delta line + right axis. Default true. */
  showDeltaLine?: boolean
  /** Show the zero reference line. Default true. */
  showZeroLine?: boolean
  /** Optional className for the outer wrapper. */
  className?: string
}

function formatTimeLabel(ts: number): string {
  const d = new Date(ts)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

function defaultFormatPrice(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

/**
 * Build the chart dataset:
 *  1. Filter to trades within `now − windowMs`.
 *  2. Sort ascending by timestamp (oldest on the left, newest right).
 *  3. Cap to `maxBars` (drop oldest beyond the cap).
 *  4. Compute per-row buy/sell volume (sellVol stored as negative so
 *     the diverging Bar renders below the zero line) and the
 *     cumulative delta.
 */
export function buildChartData(
  trades: FlowTrade[],
  windowMs: number,
  now: number,
  maxBars: number,
): FlowRow[] {
  const cutoff = now - windowMs
  const inWindow = trades.filter(
    (t) => Number.isFinite(t.timestamp) && t.timestamp >= cutoff && t.timestamp <= now + 1_000,
  )
  inWindow.sort((a, b) => a.timestamp - b.timestamp)

  const capped =
    inWindow.length > maxBars ? inWindow.slice(inWindow.length - maxBars) : inWindow

  let cumulative = 0
  return capped.map((t) => {
    const isBuy = t.side === 'BUY'
    const vol = Number.isFinite(t.size) ? Math.max(0, t.size) : 0
    if (isBuy) cumulative += vol
    else cumulative -= vol
    return {
      ts: t.timestamp,
      label: formatTimeLabel(t.timestamp),
      buyVol: isBuy ? vol : 0,
      sellVol: isBuy ? 0 : -vol,
      delta: cumulative,
      side: t.side,
      size: t.size,
      price: t.price,
    }
  })
}

interface FlowTooltipProps {
  active?: boolean
  payload?: Array<{ payload: FlowRow }>
}

function FlowTooltip({ active, payload }: FlowTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const row = payload[0].payload
  const sideColor = row.side === 'BUY' ? chartTheme.colors.success : chartTheme.colors.danger
  const d = new Date(row.ts)
  return (
    <div style={tooltipStyle}>
      <div style={{ opacity: 0.6, fontSize: 10, marginBottom: 2 }}>
        {d.toLocaleTimeString()}
      </div>
      <div style={{ color: sideColor, fontSize: 11, fontWeight: 600, marginBottom: 2 }}>
        {row.side}
      </div>
      <div style={{ fontSize: 11, color: chartTheme.tooltip.text }}>
        size <strong>{row.size.toFixed(2)}</strong>
      </div>
      <div style={{ fontSize: 11, color: chartTheme.tooltip.text }}>
        price <strong>{defaultFormatPrice(row.price)}</strong>
      </div>
      <div
        style={{
          fontSize: 11,
          color:
            row.delta >= 0 ? chartTheme.colors.success : chartTheme.colors.danger,
        }}
      >
        delta <strong>{row.delta >= 0 ? '+' : ''}{row.delta.toFixed(2)}</strong>
      </div>
    </div>
  )
}

export default function OrderFlowChart({
  trades,
  window: timeWindow = '1m',
  maxBars = 60,
  height = 240,
  now,
  buyColor = chartTheme.colors.success,
  sellColor = chartTheme.colors.danger,
  deltaColor = chartTheme.colors.warning,
  showDeltaLine = true,
  showZeroLine = true,
  className,
}: OrderFlowChartProps) {
  const effectiveNow = now ?? Date.now()
  const windowMs = WINDOW_MS[timeWindow]

  const data = useMemo(
    () => buildChartData(trades, windowMs, effectiveNow, maxBars),
    [trades, windowMs, effectiveNow, maxBars],
  )

  // Y-domain: max(|buy|, |sell|) across the window, with 10% headroom.
  const { volMax, deltaMin, deltaMax } = useMemo(() => {
    let vm = 0
    let dmin = 0
    let dmax = 0
    for (const r of data) {
      if (r.buyVol > vm) vm = r.buyVol
      if (-r.sellVol > vm) vm = -r.sellVol
      if (r.delta < dmin) dmin = r.delta
      if (r.delta > dmax) dmax = r.delta
    }
    return { volMax: vm * 1.1 || 1, deltaMin: dmin, deltaMax: dmax }
  }, [data])

  if (data.length === 0) {
    return (
      <div
        style={{ height }}
        className={`flex items-center justify-center text-[11px] text-[#7e8aaa] ${className ?? ''}`}
        data-testid="order-flow-chart-empty"
        role="status"
      >
        No order flow in the last {timeWindow}
      </div>
    )
  }

  return (
    <div
      className={`relative ${className ?? ''}`}
      style={{ height }}
      data-testid="order-flow-chart"
    >
      <ResponsiveContainer width="100%" height={height}>
        <ComposedChart
          data={data}
          margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
        >
          <CartesianGrid {...gridProps} />
          <XAxis
            {...axisProps}
            dataKey="label"
            // Cap tick density so a 60-bar window doesn't render 60 overlapping ticks.
            tickCount={Math.min(6, data.length)}
            minTickGap={20}
          />
          {/* Left Y-axis = volume (buys positive, sells negative). */}
          <YAxis
            {...axisProps}
            yAxisId="vol"
            orientation="left"
            width={44}
            domain={[-volMax, volMax]}
            allowDataOverflow
            tickFormatter={(v: number) => (Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : v.toFixed(0))}
          />
          {/* Right Y-axis = cumulative delta. */}
          {showDeltaLine && (
            <YAxis
              {...axisProps}
              yAxisId="delta"
              orientation="right"
              width={48}
              domain={[
                deltaMin < 0 ? deltaMin * 1.1 : 0,
                deltaMax > 0 ? deltaMax * 1.1 : 1,
              ]}
              allowDataOverflow
              tickFormatter={(v: number) => `${v >= 0 ? '+' : ''}${v.toFixed(0)}`}
            />
          )}
          <Tooltip
            content={<FlowTooltip />}
            cursor={tooltipCursor}
          />
          {showZeroLine && (
            <ReferenceLine
              yAxisId="vol"
              y={0}
              stroke={chartTheme.colors.muted}
              strokeOpacity={0.45}
              strokeDasharray="2 3"
            />
          )}
          {/* Buy volume bars (green, growing up from 0). */}
          <Bar
            yAxisId="vol"
            dataKey="buyVol"
            fill={buyColor}
            fillOpacity={0.85}
            isAnimationActive={false}
            name="Buys"
            radius={[1, 1, 0, 0]}
          />
          {/* Sell volume bars (red, growing down from 0 — values pre-negated). */}
          <Bar
            yAxisId="vol"
            dataKey="sellVol"
            fill={sellColor}
            fillOpacity={0.85}
            isAnimationActive={false}
            name="Sells"
            radius={[0, 0, 1, 1]}
          />
          {/* Cumulative delta line. */}
          {showDeltaLine && (
            <Line
              yAxisId="delta"
              type="monotone"
              dataKey="delta"
              stroke={deltaColor}
              strokeWidth={1.8}
              dot={false}
              isAnimationActive={false}
              name="Cumulative Δ"
            />
          )}
        </ComposedChart>
      </ResponsiveContainer>
    </div>
  )
}
