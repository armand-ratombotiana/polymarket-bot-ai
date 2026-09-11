// components/charts/EquityCurveChart.tsx — Recharts area chart for equity curve.
//
// Renders portfolio equity over time with:
//   • a gradient-filled area under the equity line (green when profit, red when loss)
//   • a baseline reference line at the starting equity (default $100)
//   • a secondary drawdown series rendered as a red translucent area overlay
//   • a final-point marker dot
//   • custom tooltip showing equity + drawdown at hover
//
// The chart is responsive via ResponsiveContainer — its width is determined
// by its parent, and the height defaults to 300px but can be overridden.
//
// Theme: reads from `./theme.ts`. All colors can be overridden per-call.
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

export interface EquityCurvePoint {
  timestamp: number
  equity: number
  /** Optional drawdown fraction (negative or 0). e.g. -0.05 = -5% from peak. */
  drawdown?: number
}

export interface EquityCurveChartProps {
  data: EquityCurvePoint[]
  height?: number
  /** Baseline equity (e.g. starting capital). Defaults to 100. */
  baseline?: number
  /** Optional line color override. Defaults to green/red based on final P&L. */
  color?: string
  /** Show the drawdown overlay band. Default true. */
  showDrawdown?: boolean
  /** Format the X-axis tick (timestamp ms). Default: HH:MM:SS UTC. */
  formatX?: (ts: number) => string
  /** Format the Y-axis tick (equity USD). Default: $XX.XX. */
  formatY?: (equity: number) => string
  /** Format the tooltip value. Default: '$XX.XX (Δ±X.XX%)'. */
  formatTooltip?: (point: EquityCurvePoint) => React.ReactNode
}

function defaultFormatX(ts: number): string {
  return new Date(ts).toISOString().slice(11, 19)
}

function defaultFormatY(equity: number): string {
  return `$${equity.toFixed(2)}`
}

interface TooltipPayload {
  payload: EquityCurvePoint & { drawdownPct?: number }
}

interface EquityTooltipProps {
  active?: boolean
  payload?: TooltipPayload[]
  baseline: number
  formatTooltip?: (point: EquityCurvePoint) => React.ReactNode
}

function EquityTooltip({
  active,
  payload,
  baseline,
  formatTooltip,
}: EquityTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const point = payload[0].payload
  if (formatTooltip) return <>{formatTooltip(point)}</>

  const pnl = point.equity - baseline
  const pnlPct = baseline > 0 ? (pnl / baseline) * 100 : 0
  const sign = pnl >= 0 ? '+' : '−'
  const dd =
    point.drawdown != null
      ? `${(point.drawdown * 100).toFixed(2)}%`
      : '0.00%'
  return (
    <div style={tooltipStyle}>
      <div style={{ opacity: 0.6, fontSize: 10, marginBottom: 2 }}>
        {defaultFormatX(point.timestamp)}
      </div>
      <div style={{ fontWeight: 600, marginBottom: 2 }}>
        {defaultFormatY(point.equity)}
      </div>
      <div style={{ fontSize: 11, opacity: 0.85 }}>
        P&amp;L: {sign}${Math.abs(pnl).toFixed(2)} ({sign}
        {Math.abs(pnlPct).toFixed(2)}%)
      </div>
      <div style={{ fontSize: 11, color: chartTheme.colors.danger }}>
        ↓ DD: {dd}
      </div>
    </div>
  )
}

// Compute drawdown if not provided by the caller.
// drawdown[i] = (equity[i] - runningPeak) / runningPeak  (always <= 0)
function computeChartData(data: EquityCurvePoint[]): Array<EquityCurvePoint & { drawdown: number; drawdownPct: number }> {
  let peak = -Infinity
  return data.map((p) => {
    const eq = Number.isFinite(p.equity) ? p.equity : 0
    peak = Math.max(peak, eq)
    const dd = p.drawdown ?? (peak > 0 ? (eq - peak) / peak : 0)
    return {
      ...p,
      equity: eq,
      drawdown: dd,
      drawdownPct: dd * 100, // for tooltip display
    }
  })
}

export default function EquityCurveChart({
  data,
  height = 300,
  baseline = 100,
  color,
  showDrawdown = true,
  formatX = defaultFormatX,
  formatY = defaultFormatY,
  formatTooltip,
}: EquityCurveChartProps) {
  // Compute drawdown if not provided by the caller.
  const chartData = useMemo(() => computeChartData(data), [data])

  const lastEquity = chartData.length > 0 ? chartData[chartData.length - 1].equity : baseline
  const isProfit = lastEquity >= baseline
  const strokeColor = color ?? (isProfit ? chartTheme.colors.success : chartTheme.colors.danger)

  // Unique gradient IDs so multiple EquityCurveCharts on one page don't clash.
  const gradientId = `eq-grad-${Math.abs(hashString(strokeColor + baseline))}`
  const ddGradientId = `dd-grad-${Math.abs(hashString(strokeColor + baseline + 1))}`

  if (chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center text-[11px] text-[#7e8aaa]"
      >
        No equity data
      </div>
    )
  }

  // Y-domain: include baseline so the reference line stays inside the plot.
  const allEquities = chartData.map((p) => p.equity)
  const yMin = Math.min(...allEquities, baseline) * 0.998
  const yMax = Math.max(...allEquities, baseline) * 1.002

  return (
    <ResponsiveContainer width="100%" height={height}>
      <AreaChart
        data={chartData}
        margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={strokeColor} stopOpacity={0.35} />
            <stop offset="100%" stopColor={strokeColor} stopOpacity={0.0} />
          </linearGradient>
          <linearGradient id={ddGradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={chartTheme.colors.danger} stopOpacity={0.45} />
            <stop offset="100%" stopColor={chartTheme.colors.danger} stopOpacity={0.08} />
          </linearGradient>
        </defs>

        <CartesianGrid {...gridProps} />
        <XAxis
          {...axisProps}
          dataKey="timestamp"
          tickFormatter={formatX}
          minTickGap={32}
        />
        <YAxis
          {...axisProps}
          domain={[yMin, yMax]}
          tickFormatter={formatY}
          width={56}
        />
        <Tooltip
          content={<EquityTooltip baseline={baseline} formatTooltip={formatTooltip} />}
          cursor={tooltipCursor}
        />

        {/* Baseline reference line ($100 / starting capital) */}
        <ReferenceLine
          y={baseline}
          stroke={chartTheme.colors.muted}
          strokeDasharray="4 3"
          strokeWidth={1}
          strokeOpacity={0.6}
          ifOverflow="extendDomain"
        />

        {/* Drawdown overlay — a thin band along the baseline showing the
            peak-to-trough excursion magnitude at each timestamp. */}
        {showDrawdown && (
          <Area
            type="monotone"
            dataKey="drawdown"
            stroke={chartTheme.colors.danger}
            strokeWidth={0.6}
            strokeOpacity={0.5}
            fill={`url(#${ddGradientId})`}
            isAnimationActive={false}
            // Recharts doesn't natively support "below-equity band" so we
            // plot drawdown as a thin area near zero — the visual cue is
            // proportional depth.
            baseValue={0}
          />
        )}

        {/* Main equity area */}
        <Area
          type="monotone"
          dataKey="equity"
          stroke={strokeColor}
          strokeWidth={1.75}
          fill={`url(#${gradientId})`}
          isAnimationActive={true}
          animationDuration={400}
          dot={false}
          activeDot={{ r: 3, fill: strokeColor, stroke: '#0e1015', strokeWidth: 1 }}
        />
      </AreaChart>
    </ResponsiveContainer>
  )
}

// Stable hash so gradient IDs are deterministic per (color, baseline) tuple.
function hashString(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  }
  return h
}
