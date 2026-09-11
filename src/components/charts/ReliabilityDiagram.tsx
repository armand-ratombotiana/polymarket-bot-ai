// components/charts/ReliabilityDiagram.tsx — Calibration curve for ML model.
//
// Plots predicted probability (bin center) on X vs empirical frequency on Y,
// with a diagonal reference line representing perfect calibration.
// Scatter points are colored by |delta| (green ≤ 0.03, amber ≤ 0.08, red
// otherwise) — mirroring the dashboard's existing convention.
//
// Designed to replace the inline `CalibrationPlot` SVG in MLValidationPanel.
'use client'

import { useMemo } from 'react'
import {
  ComposedChart,
  Scatter,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  ReferenceLine,
  ResponsiveContainer,
  CartesianGrid,
  ZAxis,
  Cell,
} from 'recharts'
import { chartTheme, tooltipStyle, axisProps, gridProps } from './theme'

export interface ReliabilityPoint {
  predicted: number
  actual: number
  count?: number
}

export interface ReliabilityDiagramProps {
  /** Typically 10 bins from /api/ml/metrics reliability_curve. */
  data: ReliabilityPoint[]
  height?: number
  /** Show the perfect-calibration diagonal. Default true. */
  showDiagonal?: boolean
  /** Format X-axis tick (predicted prob). Default: 2-decimal. */
  formatX?: (v: number) => string
  /** Format Y-axis tick (empirical freq). Default: 2-decimal. */
  formatY?: (v: number) => string
  /** Override the scatter color (ignores per-point delta coloring). */
  pointColor?: string
  /** Override the connecting polyline color. Default: chartTheme.colors.info. */
  lineColor?: string
}

function defaultFormat(v: number): string {
  return Number.isFinite(v) ? v.toFixed(2) : '—'
}

function deltaColor(predicted: number, actual: number): string {
  const d = Math.abs(predicted - actual)
  if (d < 0.03) return chartTheme.colors.success
  if (d < 0.08) return chartTheme.colors.warning
  return chartTheme.colors.danger
}

interface TooltipPayloadItem {
  payload: ReliabilityPoint & { x?: number; y?: number }
}

interface ReliabilityTooltipProps {
  active?: boolean
  payload?: TooltipPayloadItem[]
}

function ReliabilityTooltip({ active, payload }: ReliabilityTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const raw = payload[0].payload
  const predicted = raw.predicted ?? raw.x ?? 0
  const actual = raw.actual ?? raw.y ?? 0
  const delta = predicted - actual
  const sign = delta >= 0 ? '+' : '−'
  return (
    <div style={tooltipStyle}>
      <div style={{ fontWeight: 600, marginBottom: 2 }}>
        Predicted: {predicted.toFixed(3)}
      </div>
      <div style={{ marginBottom: 2 }}>
        Actual: {actual.toFixed(3)}
      </div>
      <div style={{ fontSize: 11, opacity: 0.85 }}>
        Δ: {sign}
        {Math.abs(delta).toFixed(3)}
      </div>
      {raw.count != null && (
        <div style={{ fontSize: 11, opacity: 0.7 }}>n: {raw.count}</div>
      )}
    </div>
  )
}

export default function ReliabilityDiagram({
  data,
  height = 220,
  showDiagonal = true,
  formatX = defaultFormat,
  formatY = defaultFormat,
  pointColor,
  lineColor = chartTheme.colors.info,
}: ReliabilityDiagramProps) {
  const chartData = useMemo(
    () =>
      data.map((d) => ({
        predicted: Number.isFinite(d.predicted) ? d.predicted : 0,
        actual: Number.isFinite(d.actual) ? d.actual : 0,
        count: d.count,
      })),
    [data],
  )

  if (chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className="flex items-center justify-center text-[11px] text-[#7e8aaa]"
      >
        No reliability data
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart
        data={chartData}
        margin={{ top: 8, right: 12, left: 0, bottom: 8 }}
      >
        <CartesianGrid {...gridProps} />
        <XAxis
          type="number"
          dataKey="predicted"
          name="Predicted"
          domain={[0, 1]}
          ticks={[0, 0.25, 0.5, 0.75, 1]}
          {...axisProps}
          tickFormatter={formatX}
        />
        <YAxis
          type="number"
          dataKey="actual"
          name="Actual"
          domain={[0, 1]}
          ticks={[0, 0.25, 0.5, 0.75, 1]}
          {...axisProps}
          tickFormatter={formatY}
          width={48}
        />
        <ZAxis type="number" dataKey="count" range={[40, 200]} name="n" />
        <Tooltip
          content={<ReliabilityTooltip />}
          cursor={{ stroke: chartTheme.colors.info, strokeDasharray: '3 3', strokeOpacity: 0.45 }}
        />

        {showDiagonal && (
          <ReferenceLine
            segment={[
              { x: 0, y: 0 },
              { x: 1, y: 1 },
            ]}
            stroke={chartTheme.colors.muted}
            strokeDasharray="3 3"
            strokeOpacity={0.6}
            ifOverflow="extendDomain"
          />
        )}

        {/* Connecting polyline so the calibration curve reads as a path. */}
        <Line
          type="monotone"
          dataKey="actual"
          stroke={lineColor}
          strokeWidth={1.5}
          dot={false}
          isAnimationActive={false}
        />

        {/* Calibration points — colored by |delta| (green/amber/red). */}
        <Scatter
          dataKey="actual"
          data={chartData}
          fill={pointColor ?? chartTheme.colors.info}
          isAnimationActive={false}
        >
          {pointColor == null &&
            chartData.map((d, i) => (
              <Cell key={`pt-${i}`} fill={deltaColor(d.predicted, d.actual)} />
            ))}
        </Scatter>
      </ComposedChart>
    </ResponsiveContainer>
  )
}
