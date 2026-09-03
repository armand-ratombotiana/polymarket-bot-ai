// components/charts/MarketDepthChart.tsx — Order-book depth visualization.
//
// Renders the cumulative size of bid and ask orders as a stepped area
// chart, mirroring the classic "market depth" view used on trading
// terminals:
//
//      ▲ cumulative size
//      │      ╱──╮ asks (red)
//      │  ╱──╯  │
//      │ ╱      │
//      │bids(green)
//      └──────────────► price
//                 ▲
//                mid
//
// The bids series grows left-to-right from the lowest bid price up to
// best_bid; the asks series grows right-to-left from the lowest ask up
// to best_ask. The mid-price sits in the spread valley between them.
//
// Data shape (matches the GET /api/depth/{token_id} response):
//   bids: [{ price, size, total }, ...]  (ascending price, total = cumulative)
//   asks: [{ price, size, total }, ...]  (ascending price, total = cumulative)
//
// Each series is normalized to its own price axis (bids on left, asks on
// right) so the two sides don't need to share a price domain — they
// naturally diverge around the spread. The cumulative "total" field is
// plotted as the area; the per-level "size" is surfaced in the tooltip.
//
// Tooltip shows:
//   • Price level (3dp)
//   • Size at this level
//   • Cumulative total
//   • Side (BID / ASK)
//
// Visual:
//   • Bid area: chartTheme.colors.success (green), gradient fill 0.35 → 0.0
//   • Ask area: chartTheme.colors.danger  (red),   gradient fill 0.35 → 0.0
//   • Mid reference line: dashed amber line at the mid-price
//   • Spread chip overlay: top-right "Spread: X.XX¢" badge
//
// Theme: reads from `./theme.ts`. All colors can be overridden per-call.
// Responsive: wraps in ResponsiveContainer with width="100%".

'use client'

import { useMemo } from 'react'
import {
  AreaChart,
  Area,
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

export interface DepthLevel {
  /** Price (probability 0..1, or any positive numeric). */
  price: number
  /** Order size at this price level. */
  size: number
  /** Cumulative size from best price outward to this level. */
  total: number
}

export interface MarketDepthChartProps {
  /** Cumulative bid ladder, ascending by price (best_bid first → lowest). */
  bids: DepthLevel[]
  /** Cumulative ask ladder, ascending by price (best_ask first → highest). */
  asks: DepthLevel[]
  /** Mid price (for the reference line). Optional. */
  mid?: number | null
  /** Best bid price — used to anchor the bid-area's right edge. */
  bestBid?: number | null
  /** Best ask price — used to anchor the ask-area's left edge. */
  bestAsk?: number | null
  /** Pre-computed bid-ask spread (best_ask − best_bid). Optional. */
  spread?: number | null
  /** Container height in px. Default 260. */
  height?: number
  /** Override the bid color (default chartTheme.colors.success). */
  bidColor?: string
  /** Override the ask color (default chartTheme.colors.danger). */
  askColor?: string
  /** Show the mid-price reference line. Default true. */
  showMidLine?: boolean
  /** Show the spread chip overlay in the top-right corner. Default true. */
  showSpreadChip?: boolean
  /** Format the price axis tick. Default: 3dp probability. */
  formatPrice?: (v: number) => string
  /** Format the size axis tick. Default: rounded integer. */
  formatSize?: (v: number) => string
  /** Optional className for the outer wrapper. */
  className?: string
}

function defaultFormatPrice(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

function defaultFormatSize(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(1)}k`
  return v.toFixed(0)
}

// Normalise the bid & ask ladders into a single Recharts-friendly array.
//
// Each row carries the price (X), the cumulative bid total at that price
// (bidTotal), and the cumulative ask total at that price (askTotal). We
// interleave the two series into one dataset so a single AreaChart can
// render both areas overlaid on the same price axis.
//
// Bids are plotted "right-to-left" by flipping their price axis so the
// best_bid is the rightmost point of the green area; asks are plotted
// "left-to-right" with best_ask as the leftmost point of the red area.
// In practice we use a shared ascending price axis and rely on the two
// areas to render their own segments — gaps between bid prices and ask
// prices appear as the spread valley.
interface DepthRow {
  price: number
  bidTotal: number | null
  askTotal: number | null
  bidSize: number | null
  askSize: number | null
}

function buildChartData(bids: DepthLevel[], asks: DepthLevel[]): DepthRow[] {
  // Collect every unique price point from both sides.
  const priceSet = new Set<number>()
  for (const b of bids) priceSet.add(b.price)
  for (const a of asks) priceSet.add(a.price)
  const prices = Array.from(priceSet).sort((a, b) => a - b)

  // Build lookup tables: price → { size, total } for each side.
  const bidMap = new Map<number, DepthLevel>()
  for (const b of bids) bidMap.set(b.price, b)
  const askMap = new Map<number, DepthLevel>()
  for (const a of asks) askMap.set(a.price, a)

  // For each price point on the X axis, look up the bid/ask cumulative
  // totals. If the price level doesn't exist on one side, we leave the
  // total as null (Recharts will break the area line, which is the
  // correct visual — no orders at that price).
  return prices.map((price) => {
    const b = bidMap.get(price)
    const a = askMap.get(price)
    return {
      price,
      bidTotal: b ? b.total : null,
      askTotal: a ? a.total : null,
      bidSize: b ? b.size : null,
      askSize: a ? a.size : null,
    }
  })
}

interface DepthTooltipProps {
  active?: boolean
  payload?: Array<{ payload: DepthRow }>
}

function DepthTooltip({ active, payload }: DepthTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const row = payload[0].payload
  return (
    <div style={tooltipStyle}>
      <div style={{ opacity: 0.6, fontSize: 10, marginBottom: 2 }}>
        Price: <strong style={{ color: chartTheme.tooltip.text }}>
          {defaultFormatPrice(row.price)}
        </strong>
      </div>
      {row.bidSize != null && (
        <div style={{ color: chartTheme.colors.success, fontSize: 11 }}>
          BID · size {row.bidSize.toFixed(1)} · cum {row.bidTotal?.toFixed(0)}
        </div>
      )}
      {row.askSize != null && (
        <div style={{ color: chartTheme.colors.danger, fontSize: 11 }}>
          ASK · size {row.askSize.toFixed(1)} · cum {row.askTotal?.toFixed(0)}
        </div>
      )}
      {row.bidSize == null && row.askSize == null && (
        <div style={{ opacity: 0.5, fontSize: 11 }}>
          Spread — no orders at this price
        </div>
      )}
    </div>
  )
}

// Stable gradient IDs so multiple MarketDepthCharts on one page don't
// clash. Hashing the color string keeps them deterministic per color.
function hashString(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  }
  return Math.abs(h)
}

export default function MarketDepthChart({
  bids,
  asks,
  mid = null,
  bestBid = null,
  bestAsk = null,
  spread = null,
  height = 260,
  bidColor = chartTheme.colors.success,
  askColor = chartTheme.colors.danger,
  showMidLine = true,
  showSpreadChip = true,
  formatPrice = defaultFormatPrice,
  formatSize = defaultFormatSize,
  className,
}: MarketDepthChartProps) {
  const chartData = useMemo(() => buildChartData(bids, asks), [bids, asks])

  const bidGradientId = `bid-grad-${hashString(bidColor)}`
  const askGradientId = `ask-grad-${hashString(askColor)}`

  // Y-domain: max cumulative size across both sides, with a small headroom.
  const maxTotal = useMemo(() => {
    let m = 0
    for (const r of chartData) {
      if (r.bidTotal != null && r.bidTotal > m) m = r.bidTotal
      if (r.askTotal != null && r.askTotal > m) m = r.askTotal
    }
    return m * 1.1 || 1
  }, [chartData])

  if (chartData.length === 0 || (bids.length === 0 && asks.length === 0)) {
    return (
      <div
        style={{ height }}
        className={`flex items-center justify-center text-[11px] text-[#7e8aaa] ${className ?? ''}`}
        data-testid="depth-chart-empty"
      >
        No order book depth available
      </div>
    )
  }

  // Spread chip — top-right overlay badge.
  const spreadCents = spread != null ? spread * 100 : null

  return (
    <div
      className={`relative ${className ?? ''}`}
      style={{ height }}
      data-testid="market-depth-chart"
    >
      {/* Spread chip overlay */}
      {showSpreadChip && spreadCents != null && (
        <div
          className="absolute top-2 right-3 z-10 text-[10px] mono px-2 py-0.5 rounded border border-[#1f2335] bg-[#0e1015]"
          style={{
            color:
              spreadCents >= 3 ? chartTheme.colors.warning : chartTheme.colors.muted,
          }}
          aria-label={`Spread ${spreadCents.toFixed(2)} cents`}
          data-testid="depth-chart-spread-chip"
        >
          Spread {spreadCents.toFixed(2)}¢
        </div>
      )}

      <ResponsiveContainer width="100%" height={height}>
        <AreaChart
          data={chartData}
          margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
        >
          <defs>
            <linearGradient id={bidGradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={bidColor} stopOpacity={0.35} />
              <stop offset="100%" stopColor={bidColor} stopOpacity={0.02} />
            </linearGradient>
            <linearGradient id={askGradientId} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={askColor} stopOpacity={0.35} />
              <stop offset="100%" stopColor={askColor} stopOpacity={0.02} />
            </linearGradient>
          </defs>

          <CartesianGrid {...gridProps} />
          <XAxis
            {...axisProps}
            dataKey="price"
            tickFormatter={formatPrice}
            // Use a few ticks to keep the X-axis readable.
            tickCount={Math.min(8, chartData.length)}
            minTickGap={20}
            type="number"
            domain={['dataMin', 'dataMax']}
            allowDataOverflow
          />
          <YAxis
            {...axisProps}
            tickFormatter={formatSize}
            width={48}
            domain={[0, maxTotal]}
            allowDataOverflow
          />
          <Tooltip
            content={<DepthTooltip />}
            cursor={tooltipCursor}
          />

          {/* Mid-price reference line. */}
          {showMidLine && mid != null && Number.isFinite(mid) && (
            <ReferenceLine
              x={mid}
              stroke={chartTheme.colors.warning}
              strokeDasharray="4 3"
              strokeWidth={1}
              strokeOpacity={0.7}
              label={{
                value: `mid ${formatPrice(mid)}`,
                position: 'top',
                fill: chartTheme.colors.warning,
                fontSize: 9,
              }}
              ifOverflow="extendDomain"
            />
          )}

          {/* Best bid reference dot. */}
          {bestBid != null && Number.isFinite(bestBid) && (
            <ReferenceLine
              x={bestBid}
              stroke={bidColor}
              strokeOpacity={0.5}
              strokeDasharray="2 2"
              strokeWidth={0.8}
              ifOverflow="extendDomain"
            />
          )}
          {/* Best ask reference dot. */}
          {bestAsk != null && Number.isFinite(bestAsk) && (
            <ReferenceLine
              x={bestAsk}
              stroke={askColor}
              strokeOpacity={0.5}
              strokeDasharray="2 2"
              strokeWidth={0.8}
              ifOverflow="extendDomain"
            />
          )}

          {/* Bid area (green, left side of mid). */}
          <Area
            type="step"
            dataKey="bidTotal"
            stroke={bidColor}
            strokeWidth={1.6}
            fill={`url(#${bidGradientId})`}
            isAnimationActive={true}
            animationDuration={400}
            connectNulls={false}
            dot={false}
            activeDot={{ r: 3, fill: bidColor, stroke: '#0e1015', strokeWidth: 1 }}
            name="Bids"
          />

          {/* Ask area (red, right side of mid). */}
          <Area
            type="step"
            dataKey="askTotal"
            stroke={askColor}
            strokeWidth={1.6}
            fill={`url(#${askGradientId})`}
            isAnimationActive={true}
            animationDuration={400}
            connectNulls={false}
            dot={false}
            activeDot={{ r: 3, fill: askColor, stroke: '#0e1015', strokeWidth: 1 }}
            name="Asks"
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}
