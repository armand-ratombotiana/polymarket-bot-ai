// components/charts/Sparkline.tsx — Tiny inline sparkline for metric cards.
//
// Minimal: no axes, no tooltip, no grid — just the line. Designed to render
// inside a 60×24 (default) box next to a KPI value to give an at-a-glance
// trend.
//
// Accepts an array of numbers (oldest → newest). When the data has < 2
// samples, the chart renders a flat dashed line so the KPI card layout
// doesn't collapse.
'use client'

import { useMemo } from 'react'
import { LineChart, Line, ResponsiveContainer, YAxis } from 'recharts'
import { chartTheme } from './theme'

export interface SparklineProps {
  data: number[]
  /** Line color. Default: chartTheme.colors.info (#06b6d4). */
  color?: string
  /** Container width in px (or "100%" to fill parent). Default 60. */
  width?: number | string
  /** Container height in px. Default 24. */
  height?: number
  /** Stroke width. Default 1.4. */
  strokeWidth?: number
  /** Show a filled dot at the last data point. Default true. */
  showLastDot?: boolean
  /** Optional className applied to the outer wrapper. */
  className?: string
}

export default function Sparkline({
  data,
  color = chartTheme.colors.info,
  width = 60,
  height = 24,
  strokeWidth = 1.4,
  showLastDot = true,
  className,
}: SparklineProps) {
  // Normalise into Recharts-friendly shape: [{ i, v }, ...]
  const chartData = useMemo(
    () => data.map((v, i) => ({ i, v: Number.isFinite(v) ? v : 0 })),
    [data],
  )

  // Custom dot renderer: only draw a circle at the last data point.
  const lastDotRenderer = (props: {
    cx?: number
    cy?: number
    index?: number
  }) => {
    if (!showLastDot) return <g key="empty" />
    const { cx, cy, index } = props
    if (
      cx == null ||
      cy == null ||
      index == null ||
      index !== chartData.length - 1
    ) {
      return <g key={`dot-${index ?? 'none'}`} />
    }
    return (
      <circle
        key={`dot-${index}`}
        cx={cx}
        cy={cy}
        r={1.8}
        fill={color}
        strokeWidth={0}
      />
    )
  }

  // When we don't have enough points, render a static dashed baseline.
  if (!data || data.length < 2) {
    return (
      <div
        className={className}
        style={{
          width: typeof width === 'number' ? `${width}px` : width,
          height: `${height}px`,
        }}
        aria-hidden="true"
      >
        <svg
          width="100%"
          height={height}
          viewBox={`0 0 100 ${height}`}
          preserveAspectRatio="none"
        >
          <line
            x1={0}
            y1={height / 2}
            x2={100}
            y2={height / 2}
            stroke="#3e4560"
            strokeWidth={1}
            strokeDasharray="2 2"
          />
        </svg>
      </div>
    )
  }

  return (
    <div
      className={className}
      style={{
        width: typeof width === 'number' ? `${width}px` : width,
        height: `${height}px`,
      }}
      aria-hidden="true"
    >
      <ResponsiveContainer width="100%" height={height}>
        <LineChart
          data={chartData}
          margin={{ top: 1, right: 1, bottom: 1, left: 1 }}
        >
          {/* Hidden axis — needed for recharts to compute domain. */}
          <YAxis
            dataKey="v"
            domain={['dataMin', 'dataMax']}
            hide
            tick={false}
            axisLine={false}
            tickLine={false}
          />
          <Line
            type="monotone"
            dataKey="v"
            stroke={color}
            strokeWidth={strokeWidth}
            dot={lastDotRenderer}
            activeDot={false}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}
