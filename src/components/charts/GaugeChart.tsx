// components/charts/GaugeChart.tsx — Radial gauge for utilization / balance.
//
// Renders a 0–100% radial gauge using Recharts' RadialBarChart. The dial
// sweeps from 12 o'clock clockwise (270° span) and color-codes the value
// by threshold (green/amber/red) — but the caller can override the color.
//
// Used by the CapitalAllocatorPanel for "Capital Utilization" and is generic
// enough for any "X% of Y" KPI.
'use client'

import { useMemo } from 'react'
import {
  RadialBarChart,
  RadialBar,
  PolarAngleAxis,
  ResponsiveContainer,
} from 'recharts'
import {
  chartTheme,
  utilizationColor,
} from './theme'

export interface GaugeChartProps {
  /** Value 0–100. Values outside the range are clamped. */
  value: number
  /** Optional label rendered above the value (e.g. "DEPLOYED"). */
  label?: string
  /** Optional sublabel rendered below the value (e.g. "$5.23 / $200"). */
  sublabel?: string
  /** Override the arc color. Default: threshold-based (green/amber/red). */
  color?: string
  /** Container height in px. Default 180. */
  height?: number
  /** Start angle (12 o'clock = 90). Default 90. */
  startAngle?: number
  /** End angle. Default -270 (i.e. 270° sweep clockwise). */
  endAngle?: number
  /** Render the big center text. Default true. */
  showCenterText?: boolean
}

interface GaugeDatum {
  name: string
  value: number
  fill: string
}

export default function GaugeChart({
  value,
  label,
  sublabel,
  color,
  height = 180,
  startAngle = 90,
  endAngle = -270,
  showCenterText = true,
}: GaugeChartProps) {
  const clamped = Math.max(0, Math.min(100, Number.isFinite(value) ? value : 0))
  const arcColor = color ?? utilizationColor(clamped)

  const data: GaugeDatum[] = useMemo(
    () => [{ name: 'value', value: clamped, fill: arcColor }],
    [clamped, arcColor],
  )

  return (
    <div
      className="flex flex-col items-center justify-center relative"
      style={{ height }}
    >
      <ResponsiveContainer width="100%" height={height}>
        <RadialBarChart
          cx="50%"
          cy="50%"
          innerRadius="68%"
          outerRadius="100%"
          barSize={10}
          data={data}
          startAngle={startAngle}
          endAngle={endAngle}
        >
          {/* PolarAngleAxis with domain [0, 100] makes the bar fill
              proportional to `value`. Without it, Recharts would scale
              to the data's max. */}
          <PolarAngleAxis
            type="number"
            domain={[0, 100]}
            angleAxisId={0}
            tick={false}
          />
          <RadialBar
            background={{ fill: '#1f2335' }}
            dataKey="value"
            cornerRadius={8}
            isAnimationActive={true}
            animationDuration={500}
          />
        </RadialBarChart>
      </ResponsiveContainer>

      {showCenterText && (
        <div
          className="absolute inset-0 flex flex-col items-center justify-center pointer-events-none"
          aria-hidden="true"
        >
          <span
            className="mono font-extrabold"
            style={{
              color: arcColor,
              fontSize: '26px',
              lineHeight: 1.1,
            }}
          >
            {clamped.toFixed(1)}%
          </span>
          {label && (
            <span
              className="font-semibold uppercase tracking-wide"
              style={{
                color: chartTheme.axis,
                fontSize: '9.5px',
                marginTop: '2px',
              }}
            >
              {label}
            </span>
          )}
          {sublabel && (
            <span
              className="mono"
              style={{
                color: chartTheme.axis,
                fontSize: '10px',
                marginTop: '4px',
              }}
            >
              {sublabel}
            </span>
          )}
        </div>
      )}
    </div>
  )
}
