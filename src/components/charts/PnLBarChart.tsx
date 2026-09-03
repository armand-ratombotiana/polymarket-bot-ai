// components/charts/PnLBarChart.tsx — Bar chart for P&L attribution.
//
// Renders positive bars in green and negative bars in red, with a zero
// reference line so the eye can instantly spot contributing vs detracting
// buckets. Designed for the AttributionPanel dimension waterfall and the
// per-bucket breakdown view.
//
// Each datum: { name: string, value: number }. Optional `color` per datum
// overrides the default green/red sign coloring.
'use client'

import { useMemo } from 'react'
import {
  BarChart,
  Bar,
  Cell,
  XAxis,
  YAxis,
  Tooltip,
  ResponsiveContainer,
  ReferenceLine,
  CartesianGrid,
} from 'recharts'
import {
  chartTheme,
  tooltipStyle,
  axisProps,
  gridProps,
  tooltipCursor,
  pnlColor,
} from './theme'

export interface PnLBarDatum {
  name: string
  value: number
  /** Optional per-bar color override. */
  color?: string
  /** Optional secondary value shown in tooltip (e.g. count, %). */
  sub?: string
}

export interface PnLBarChartProps {
  data: PnLBarDatum[]
  height?: number
  /** Layout: 'horizontal' (default) or 'vertical' (rotated 90°). */
  layout?: 'horizontal' | 'vertical'
  /** Show a zero reference line. Default true. */
  showZeroLine?: boolean
  /** Format the value axis tick. Default: 2-decimal signed. */
  formatValue?: (v: number) => string
  /** Format the tooltip value. Default: ±$X.XX. */
  formatTooltip?: (d: PnLBarDatum) => React.ReactNode
  /** Override the default success color (positive bars). */
  successColor?: string
  /** Override the default danger color (negative bars). */
  dangerColor?: string
}

function defaultFormatValue(v: number): string {
  const sign = v < 0 ? '−' : ''
  return `${sign}${Math.abs(v).toFixed(2)}`
}

function defaultFormatTooltip(d: PnLBarDatum): React.ReactNode {
  const sign = d.value < 0 ? '−' : ''
  return (
    <div style={tooltipStyle}>
      <div style={{ fontWeight: 600, marginBottom: 2 }}>{d.name}</div>
      <div>
        {sign}${Math.abs(d.value).toFixed(2)}
      </div>
      {d.sub && (
        <div style={{ fontSize: 11, opacity: 0.7 }}>{d.sub}</div>
      )}
    </div>
  )
}

interface TooltipPayloadItem {
  payload: PnLBarDatum
}

interface PnLTooltipProps {
  active?: boolean
  payload?: TooltipPayloadItem[]
  formatTooltip?: (d: PnLBarDatum) => React.ReactNode
}

function PnLTooltip({ active, payload, formatTooltip }: PnLTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const d = payload[0].payload
  if (formatTooltip) return <>{formatTooltip(d)}</>
  return <>{defaultFormatTooltip(d)}</>
}

export default function PnLBarChart({
  data,
  height = 240,
  layout = 'horizontal',
  showZeroLine = true,
  formatValue = defaultFormatValue,
  formatTooltip,
  successColor = chartTheme.colors.success,
  dangerColor = chartTheme.colors.danger,
}: PnLBarChartProps) {
  const chartData = useMemo(
    () => data.map((d) => ({ ...d, value: Number.isFinite(d.value) ? d.value : 0 })),
    [data],
  )

  if (chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center text-[11px] text-[#7e8aaa]"
      >
        No data
      </div>
    )
  }

  const isVertical = layout === 'vertical'

  return (
    <ResponsiveContainer width="100%" height={height}>
      <BarChart
        data={chartData}
        layout={isVertical ? 'vertical' : 'horizontal'}
        margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
      >
        <CartesianGrid {...gridProps} horizontal={!isVertical} vertical={isVertical} />
        {isVertical ? (
          <>
            <XAxis
              type="number"
              {...axisProps}
              tickFormatter={formatValue}
            />
            <YAxis
              type="category"
              dataKey="name"
              {...axisProps}
              width={120}
              tick={{ fill: chartTheme.axis, fontSize: 10 }}
            />
          </>
        ) : (
          <>
            <XAxis
              type="category"
              dataKey="name"
              {...axisProps}
              interval={0}
              tick={{ fill: chartTheme.axis, fontSize: 9, angle: -25, textAnchor: 'end' }}
              height={48}
            />
            <YAxis
              type="number"
              {...axisProps}
              tickFormatter={formatValue}
              width={56}
            />
          </>
        )}
        <Tooltip
          content={<PnLTooltip formatTooltip={formatTooltip} />}
          cursor={{ ...tooltipCursor, fill: 'rgba(255,255,255,0.04)' }}
        />
        {showZeroLine && (
          <ReferenceLine y={0} stroke={chartTheme.colors.muted} strokeOpacity={0.6} />
        )}
        <Bar
          dataKey="value"
          radius={isVertical ? [0, 3, 3, 0] : [3, 3, 0, 0]}
          maxBarWidth={48}
          isAnimationActive={true}
          animationDuration={400}
        >
          {chartData.map((d, i) => (
            <Cell
              key={`bar-${i}`}
              fill={
                d.color ??
                (d.value > 0
                  ? successColor
                  : d.value < 0
                    ? dangerColor
                    : chartTheme.colors.muted)
              }
            />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}

// Re-export the helper for callers that want it (e.g. legends).
export { pnlColor }
