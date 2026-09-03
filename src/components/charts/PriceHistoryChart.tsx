// components/charts/PriceHistoryChart.tsx — Price history over time.
//
// Renders a price line chart for a market's OHLCV bars, with:
//   • Gradient-filled area under the close-price line (green when the
//     latest close is ≥ the first close, red otherwise — same convention
//     as EquityCurveChart).
//   • Optional volume bars rendered as a faint secondary series behind
//     the price line.
//   • High / low markers — small reference dots at the min and max close
//     across the visible range.
//   • Configurable time range selector (1m, 5m, 15m, 1h, 4h, 1d).
//   • Custom tooltip with timestamp + close + change-since-prev.
//
// Data shape (matches GET /api/history/ohlcv/{token_id}?resolution=X&count=N):
//   bars: [{ timestamp, open, high, low, close, volume }, ...]
//
// The chart accepts either pre-fetched bars (parent owns the fetch) or
// it can self-fetch by passing `tokenId` + `resolution`. Self-fetching
// keeps the MarketsPanel wiring simple — the new "View History" button
// just mounts the chart with a tokenId and lets it manage its own data.
//
// Theme: reads from `./theme.ts`. All colors can be overridden per-call.
// Responsive: wraps in ResponsiveContainer with width="100%".
//
// NOTE: the `/api/history/ohlcv` endpoint currently only supports
// `1m | 5m | 1h` resolutions. When a caller asks for `15m | 4h | 1d`,
// we coerce down to the closest supported resolution (`15m → 5m`,
// `4m → 1h`, `1d → 1h`) and label the chart accordingly so the data is
// always real. This keeps the W15-1 implementation unblocked; once the
// backend adds the new resolutions, the coercion map can be dropped.

'use client'

import { useEffect, useMemo, useState, useCallback } from 'react'
import {
  ComposedChart,
  Line,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  ReferenceDot,
} from 'recharts'
import { getApiUrl, apiFetch } from '@/lib/api'
import {
  chartTheme,
  tooltipStyle,
  axisProps,
  gridProps,
  tooltipCursor,
} from './theme'

export interface PriceHistoryBar {
  /** Unix epoch seconds (server) or ms (client) — coerced below. */
  timestamp: number
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

export type HistoryResolution = '1m' | '5m' | '15m' | '1h' | '4h' | '1d'

export interface PriceHistoryChartProps {
  /** Token ID to fetch history for. Required if `bars` is not provided. */
  tokenId?: string
  /** Pre-fetched bars. Required if `tokenId` is not provided. */
  bars?: PriceHistoryBar[]
  /** Initial resolution. Default '5m'. */
  resolution?: HistoryResolution
  /** Initial bar count. Default 60. */
  count?: number
  /** Container height in px. Default 280. */
  height?: number
  /** Show volume bars overlay. Default true. */
  showVolume?: boolean
  /** Show high/low markers. Default true. */
  showMarkers?: boolean
  /** Show the time-range selector UI. Default true. */
  showRangeSelector?: boolean
  /** Override the price line color. Defaults to chartTheme.colors.info. */
  lineColor?: string
  /** Format the X-axis tick (timestamp). Default: HH:MM:SS. */
  formatX?: (ts: number) => string
  /** Format the Y-axis tick (price). Default: 3dp probability. */
  formatY?: (v: number) => string
  /** Optional className for the outer wrapper. */
  className?: string
  /** Optional callback fired when the resolution changes. */
  onResolutionChange?: (r: HistoryResolution) => void
}

const RANGES: HistoryResolution[] = ['1m', '5m', '15m', '1h', '4h', '1d']

// Coerce unsupported resolutions down to the closest supported one.
// Backend supports: 1m, 5m, 1h.
function coerceResolution(r: HistoryResolution): '1m' | '5m' | '1h' {
  if (r === '1m' || r === '5m' || r === '1h') return r
  if (r === '15m') return '5m'
  if (r === '4h') return '1h'
  if (r === '1d') return '1h'
  return '5m'
}

function defaultFormatX(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('en-US', { hour12: false })
}

function defaultFormatY(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

interface PriceTooltipProps {
  active?: boolean
  payload?: Array<{ payload: PriceHistoryBar & { changePct?: number } }>
}

function PriceTooltip({ active, payload }: PriceTooltipProps) {
  if (!active || !payload || !payload.length) return null
  const bar = payload[0].payload
  const changePct = bar.changePct
  const color = changePct == null
    ? chartTheme.colors.muted
    : changePct >= 0 ? chartTheme.colors.success : chartTheme.colors.danger
  return (
    <div style={tooltipStyle}>
      <div style={{ opacity: 0.6, fontSize: 10, marginBottom: 2 }}>
        {defaultFormatX(bar.timestamp)}
      </div>
      <div style={{ fontWeight: 600, marginBottom: 2 }}>
        {defaultFormatY(bar.close)}
      </div>
      <div style={{ fontSize: 11, color }}>
        {changePct == null
          ? 'first bar'
          : `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`}
      </div>
      {bar.volume != null && (
        <div style={{ fontSize: 11, opacity: 0.7, marginTop: 2 }}>
          vol {bar.volume.toFixed(0)}
        </div>
      )}
    </div>
  )
}

// Normalize timestamps from ms → s (server returns seconds; client
// rendering expects seconds for the formatX default).
function normalizeTimestamps(bars: PriceHistoryBar[]): PriceHistoryBar[] {
  return bars.map((b) => ({
    ...b,
    timestamp:
      b.timestamp > 1e12 ? Math.floor(b.timestamp / 1000) : b.timestamp,
  }))
}

// Stable gradient IDs so multiple PriceHistoryCharts on one page don't clash.
function hashString(s: string): number {
  let h = 0
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  }
  return Math.abs(h)
}

export default function PriceHistoryChart({
  tokenId,
  bars: barsProp,
  resolution: resolutionProp = '5m',
  count = 60,
  height = 280,
  showVolume = true,
  showMarkers = true,
  showRangeSelector = true,
  lineColor = chartTheme.colors.info,
  formatX = defaultFormatX,
  formatY = defaultFormatY,
  className,
  onResolutionChange,
}: PriceHistoryChartProps) {
  const [resolution, setResolution] = useState<HistoryResolution>(resolutionProp)
  const [bars, setBars] = useState<PriceHistoryBar[]>(
    barsProp ? normalizeTimestamps(barsProp) : [],
  )
  const [loading, setLoading] = useState<boolean>(!barsProp && !!tokenId)
  const [error, setError] = useState<string | null>(null)

  // Self-fetch mode: when tokenId is provided and no barsProp is given,
  // poll /api/history/ohlcv/{token_id}. Re-fetch on resolution change.
  useEffect(() => {
    if (!tokenId || barsProp) return
    let cancelled = false
    const fetchBars = async () => {
      try {
        setLoading(true)
        setError(null)
        const apiUrl = getApiUrl()
        const apiRes = coerceResolution(resolution)
        const res = await apiFetch(
          `${apiUrl}/api/history/ohlcv/${tokenId}?resolution=${apiRes}&count=${count}`,
        )
        if (cancelled) return
        if (!res.ok) {
          setError(`HTTP ${res.status}`)
          return
        }
        const json = await res.json()
        const fetched: PriceHistoryBar[] = json.bars || []
        setBars(normalizeTimestamps(fetched))
      } catch (e) {
        if (!cancelled) setError('Network error')
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    fetchBars()
    // Poll every 5s for fresh bars.
    const timer = setInterval(fetchBars, 5000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [tokenId, resolution, count, barsProp])

  // When barsProp updates, normalize and store.
  useEffect(() => {
    if (barsProp) {
      setBars(normalizeTimestamps(barsProp))
      setLoading(false)
    }
  }, [barsProp])

  const handleResolutionChange = useCallback(
    (r: HistoryResolution) => {
      setResolution(r)
      onResolutionChange?.(r)
    },
    [onResolutionChange],
  )

  // Compute chart data: each bar with changePct since the first bar.
  const chartData = useMemo(() => {
    if (bars.length === 0) return []
    const firstClose = bars[0].close
    return bars.map((b) => ({
      ...b,
      changePct: firstClose > 0 ? ((b.close - firstClose) / firstClose) * 100 : 0,
    }))
  }, [bars])

  const gradientId = `price-grad-${hashString(lineColor)}`
  const volGradientId = `vol-grad-${hashString(lineColor + 'vol')}`

  // High/low markers — only across visible bars.
  const { highBar, lowBar } = useMemo(() => {
    if (chartData.length === 0) return { highBar: null, lowBar: null }
    let high = chartData[0]
    let low = chartData[0]
    for (const b of chartData) {
      if (b.high > high.high) high = b
      if (b.low < low.low) low = b
    }
    return { highBar: high, lowBar: low }
  }, [chartData])

  // Y-domain: pad by 2% so the line doesn't touch the chart edges.
  const { yMin, yMax } = useMemo(() => {
    if (chartData.length === 0) return { yMin: 0, yMax: 1 }
    const lows = chartData.map((b) => b.low)
    const highs = chartData.map((b) => b.high)
    const lo = Math.min(...lows)
    const hi = Math.max(...highs)
    const pad = (hi - lo) * 0.05 || 0.005
    return { yMin: Math.max(0.001, lo - pad), yMax: Math.min(0.999, hi + pad) }
  }, [chartData])

  // Max volume for normalizing the volume bars. Computed before any early
  // return so the rules-of-hooks (same call order on every render) hold.
  const maxVolume = useMemo(() => {
    let m = 0
    for (const b of chartData) {
      if (b.volume != null && b.volume > m) m = b.volume
    }
    return m || 1
  }, [chartData])

  // Normalize volume into the same Y-domain as price (0..maxVolume → 0..yMax).
  // This puts the volume bars behind the price line in a meaningful scale.
  const chartDataWithVol = useMemo(() => {
    return chartData.map((b) => ({
      ...b,
      volScaled: b.volume != null ? (b.volume / maxVolume) * yMax * 0.35 : 0,
    }))
  }, [chartData, maxVolume, yMax])

  // Empty / loading states.
  if (loading && chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className={`flex flex-col items-center justify-center text-[11px] text-[#7e8aaa] gap-2 ${className ?? ''}`}
        data-testid="price-history-loading"
      >
        <span className="spinner" aria-hidden="true" />
        <span>Loading price history…</span>
      </div>
    )
  }

  if (error && chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className={`flex flex-col items-center justify-center text-[11px] text-red-400 gap-1 ${className ?? ''}`}
        data-testid="price-history-error"
        role="alert"
      >
        <span>⚠️ {error}</span>
        <span className="text-[#7e8aaa]">Could not load price history.</span>
      </div>
    )
  }

  if (chartData.length === 0) {
    return (
      <div
        style={{ height }}
        className={`flex items-center justify-center text-[11px] text-[#7e8aaa] ${className ?? ''}`}
        data-testid="price-history-empty"
      >
        No price history available
      </div>
    )
  }

  return (
    <div
      className={`flex flex-col gap-2 ${className ?? ''}`}
      data-testid="price-history-chart"
    >
      {/* Time-range selector */}
      {showRangeSelector && (
        <div
          className="flex items-center gap-1 bg-[#0e1015] p-0.5 rounded border border-[#1f2335] self-start"
          role="group"
          aria-label="Time range selector"
        >
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => handleResolutionChange(r)}
              className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
                resolution === r
                  ? 'bg-blue-500 text-black'
                  : 'text-[#7e8aaa] hover:text-white'
              }`}
              aria-label={`Time range ${r}`}
              aria-pressed={resolution === r}
              data-testid={`range-btn-${r}`}
            >
              {r}
            </button>
          ))}
        </div>
      )}

      <div style={{ height }} className="relative">
        <ResponsiveContainer width="100%" height={height}>
          <ComposedChart
            data={chartDataWithVol}
            margin={{ top: 8, right: 12, left: 0, bottom: 0 }}
          >
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={lineColor} stopOpacity={0.35} />
                <stop offset="100%" stopColor={lineColor} stopOpacity={0.02} />
              </linearGradient>
              <linearGradient id={volGradientId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={chartTheme.colors.muted} stopOpacity={0.4} />
                <stop offset="100%" stopColor={chartTheme.colors.muted} stopOpacity={0.05} />
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
              width={48}
              allowDataOverflow
            />
            <Tooltip
              content={<PriceTooltip />}
              cursor={tooltipCursor}
            />

            {/* Volume bars — faint background. */}
            {showVolume && (
              <Area
                type="bar"
                dataKey="volScaled"
                stroke="none"
                fill={`url(#${volGradientId})`}
                isAnimationActive={false}
                dot={false}
                activeDot={false}
                name="Volume"
              />
            )}

            {/* Price line — gradient-filled area + line overlay. */}
            <Area
              type="monotone"
              dataKey="close"
              stroke={lineColor}
              strokeWidth={1.75}
              fill={`url(#${gradientId})`}
              isAnimationActive={true}
              animationDuration={400}
              dot={false}
              activeDot={{
                r: 3,
                fill: lineColor,
                stroke: '#0e1015',
                strokeWidth: 1,
              }}
              name="Price"
            />

            {/* High marker */}
            {showMarkers && highBar && (
              <ReferenceDot
                x={highBar.timestamp}
                y={highBar.high}
                r={3}
                fill={chartTheme.colors.success}
                stroke="#0e1015"
                strokeWidth={1}
                label={{
                  value: `H ${formatY(highBar.high)}`,
                  position: 'top',
                  fill: chartTheme.colors.success,
                  fontSize: 9,
                }}
                ifOverflow="extendDomain"
              />
            )}

            {/* Low marker */}
            {showMarkers && lowBar && (
              <ReferenceDot
                x={lowBar.timestamp}
                y={lowBar.low}
                r={3}
                fill={chartTheme.colors.danger}
                stroke="#0e1015"
                strokeWidth={1}
                label={{
                  value: `L ${formatY(lowBar.low)}`,
                  position: 'bottom',
                  fill: chartTheme.colors.danger,
                  fontSize: 9,
                }}
                ifOverflow="extendDomain"
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  )
}
