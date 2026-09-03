// components/EquityCurve.tsx — Real-Time Equity Curve Chart
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct, colors } from '@/lib/design-tokens'

interface EquityPoint {
  timestamp: number
  equity: number
  pnl: number
}

export default function EquityCurve() {
  const [points, setPoints] = useState<EquityPoint[]>([])
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  useEffect(() => {
    const fetchEquity = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/history/equity`)
        if (res.ok) {
          const json = await res.json()
          if (json.points && Array.isArray(json.points)) {
            setPoints(json.points)
            setLastUpdated(Date.now())
          }
        }
      } catch {
      } finally {
        setLoading(false)
      }
    }
    fetchEquity()
    const timer = setInterval(fetchEquity, 3000)
    return () => clearInterval(timer)
  }, [])

  const currentEquity = points.length > 0 ? points[points.length - 1].equity : null
  const currentPnl = points.length > 0 ? points[points.length - 1].pnl : 0.0

  if (loading && points.length === 0) {
    return (
      <div className="card p-3 flex flex-col justify-between min-h-[160px]">
        <div className="card-header pb-1">
          <span className="card-title">📈 Equity Curve</span>
          <span className="badge badge-dim text-[10px]">USDC · Paper</span>
        </div>
        <div className="flex-1 flex items-center justify-center text-xs text-[#4a5068]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading equity timeline…
        </div>
      </div>
    )
  }

  if (points.length < 2) {
    return (
      <div className="card p-3 flex flex-col justify-between min-h-[160px]">
        <div className="card-header pb-1 flex justify-between items-center">
          <span className="card-title">📈 Equity Curve</span>
          <div className="flex items-center gap-1.5">
            <span className="badge badge-amber text-[10px]">Paper</span>
            <span className="mono text-xs text-green-400 font-semibold">
              {currentEquity !== null ? fmtUsd(currentEquity) : '—'}
            </span>
          </div>
        </div>
        <div className="flex-1 flex flex-col items-center justify-center text-xs text-[#7e8aaa] text-center p-3">
          <span className="text-base mb-1" aria-hidden="true">⏱️</span>
          <span>Accumulating paper execution points…</span>
          <span className="text-[10px] text-[#4a5068] mt-1">Baseline: $100.00 Operating Capital</span>
        </div>
      </div>
    )
  }

  // Calculate SVG path with $100 baseline reference
  const baseline = 100.0
  const allValues = [...points.map((p) => p.equity), baseline]
  const minEq = Math.min(...allValues) * 0.998
  const maxEq = Math.max(...allValues) * 1.002
  const range = maxEq - minEq || 1

  const width = 300
  const height = 85
  const padding = 6

  const coords = points.map((p, i) => {
    const x = padding + (i / (points.length - 1)) * (width - 2 * padding)
    const y = height - padding - ((p.equity - minEq) / range) * (height - 2 * padding)
    return { x, y }
  })

  const baselineY = height - padding - ((baseline - minEq) / range) * (height - 2 * padding)

  const pathD = coords.reduce((acc, pt, i) => (i === 0 ? `M ${pt.x},${pt.y}` : `${acc} L ${pt.x},${pt.y}`), '')
  const areaD = `${pathD} L ${coords[coords.length - 1].x},${height} L ${coords[0].x},${height} Z`

  const isProfit = currentPnl >= 0
  const strokeColor = isProfit ? '#22c55e' : '#ef4444'

  // W14 — Drawdown from peak overlay.
  // drawdown[i] = (equity[i] - max(equity[0..i])) / max(equity[0..i])
  // Always <= 0; 0 means equity is at a new all-time-high.
  let runningPeak = -Infinity
  const drawdowns = points.map((p) => {
    runningPeak = Math.max(runningPeak, p.equity)
    return runningPeak > 0 ? (p.equity - runningPeak) / runningPeak : 0
  })
  // Most negative drawdown observed so far (worst peak-to-trough excursion).
  const maxDrawdown = drawdowns.reduce((m, d) => Math.min(m, d), 0)
  const maxDrawdownPct = Math.abs(maxDrawdown) // 0..1 magnitude for display

  // W14 — Map drawdown magnitude to vertical pixels below the equity line.
  // ~140px per unit drawdown means a 5% drawdown ~ 7px deep, 20% ~ 28px.
  // Clamped to the chart's bottom padding so the band never overflows.
  const ddPxScale = 140
  const drawdownBottom = coords.map((c, i) => ({
    x: c.x,
    y: Math.min(c.y + Math.abs(drawdowns[i]) * ddPxScale, height - padding),
  }))

  // W14 — Red filled area: top edge = equity line, bottom edge = drawdown-scaled.
  const ddTopPath = pathD // reuse equity polyline as the top of the band
  const ddBottomPathReversed = [...drawdownBottom]
    .reverse()
    .map((pt) => `L ${pt.x},${pt.y}`)
    .join(' ')
  const drawdownAreaD = `${ddTopPath} ${ddBottomPathReversed} Z`
  // Outline of the drawdown band's lower edge (subtle redFg stroke for legibility).
  const drawdownBottomPathD = drawdownBottom.reduce(
    (acc, pt, i) => (i === 0 ? `M ${pt.x},${pt.y}` : `${acc} L ${pt.x},${pt.y}`),
    ''
  )

  return (
    <div className="card p-3 flex flex-col justify-between min-h-[160px] bg-[#13161e] border border-[#1f2335] shadow-md">
      <div className="card-header pb-1 flex justify-between items-center">
        <div className="flex items-center gap-1.5">
          <span className="card-title text-xs font-bold text-[#dde1ed]">📈 Portfolio Equity</span>
          <span className="badge badge-amber text-[9.5px]">Paper</span>
        </div>
        <div className="flex items-center gap-2">
          <span className="mono text-xs font-semibold text-[#dde1ed]">
            {currentEquity !== null ? fmtUsd(currentEquity) : '—'}
          </span>
          <span className={`badge ${isProfit ? 'badge-green' : 'badge-red'} text-[10px]`}>
            {fmtPnl(currentPnl)}
          </span>
          {/* W14 — Current max drawdown label (red tokens). */}
          <span
            className={`badge ${maxDrawdownPct > 0 ? 'badge-red' : 'badge-dim'} text-[10px]`}
            title={`Max drawdown from peak (running). Worst peak-to-trough excursion so far.`}
            style={maxDrawdownPct > 0 ? { color: colors.redFg } : undefined}
          >
            ↓DD {fmtPct(maxDrawdownPct)}
          </span>
        </div>
      </div>

      {/* SVG Chart */}
      <div className="flex-1 flex items-center justify-center py-1 relative">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-full overflow-visible" role="img" aria-label="Portfolio equity curve chart">
          <defs>
            <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={strokeColor} stopOpacity="0.25" />
              <stop offset="100%" stopColor={strokeColor} stopOpacity="0.0" />
            </linearGradient>
            {/* W14 — Drawdown overlay gradient (red tokens). */}
            <linearGradient id="ddGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={colors.red} stopOpacity="0.45" />
              <stop offset="100%" stopColor={colors.red} stopOpacity="0.08" />
            </linearGradient>
          </defs>
          
          {/* Baseline reference line ($100) */}
          <line
            x1={padding}
            y1={baselineY}
            x2={width - padding}
            y2={baselineY}
            stroke="#3e4560"
            strokeDasharray="3 3"
            strokeWidth="1"
          />
          
          <path d={areaD} fill="url(#eqGrad)" />
          {/* W14 — Drawdown overlay: red filled area below the equity line,
               depth proportional to peak-to-trough drawdown magnitude. */}
          <path d={drawdownAreaD} fill="url(#ddGrad)" />
          <path
            d={drawdownBottomPathD}
            fill="none"
            stroke={colors.redFg}
            strokeWidth="0.6"
            strokeOpacity="0.55"
            strokeLinejoin="round"
          />
          <path d={pathD} fill="none" stroke={strokeColor} strokeWidth="1.75" strokeLinecap="round" />
          {coords.length > 0 && (
            <circle
              cx={coords[coords.length - 1].x}
              cy={coords[coords.length - 1].y}
              r="3"
              fill={strokeColor}
            />
          )}
        </svg>
      </div>

      <div className="flex justify-between items-center text-[10px] text-[#7e8aaa] pt-1 mono border-t border-[#1f2335]">
        <span>Base: $100.00</span>
        <span>Min: {fmtUsd(minEq)}</span>
        <span>Peak: {fmtUsd(maxEq)}</span>
        {lastUpdated && <span>{new Date(lastUpdated).toISOString().slice(14, 19)}</span>}
      </div>
    </div>
  )
}
