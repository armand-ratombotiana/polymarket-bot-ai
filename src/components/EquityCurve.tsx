// components/EquityCurve.tsx — Real-Time Equity Curve Chart
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct, colors } from '@/lib/design-tokens'
import { EquityCurveChart } from '@/components/charts'

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

  // Calculate min/max for footer display (kept for the summary line below
  // the chart; the chart itself computes its own Y-domain via Recharts).
  const baseline = 100.0
  const allValues = [...points.map((p) => p.equity), baseline]
  const minEq = Math.min(...allValues)
  const maxEq = Math.max(...allValues)

  // W14 — drawdown from peak (running peak-to-trough excursion).
  let runningPeak = -Infinity
  const drawdowns = points.map((p) => {
    runningPeak = Math.max(runningPeak, p.equity)
    return runningPeak > 0 ? (p.equity - runningPeak) / runningPeak : 0
  })
  const maxDrawdown = drawdowns.reduce((m, d) => Math.min(m, d), 0)
  const maxDrawdownPct = Math.abs(maxDrawdown) // 0..1 magnitude for display

  const isProfit = currentPnl >= 0

  // Map to EquityCurveChart input shape — includes the precomputed drawdown
  // per timestamp so the chart's red overlay matches W14's contract.
  const chartData = points.map((p, i) => ({
    timestamp: p.timestamp,
    equity: p.equity,
    drawdown: drawdowns[i],
  }))

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

      {/* W13-9 — Recharts AreaChart via the shared EquityCurveChart.
          Replaces the hand-rolled SVG. Keeps the gradient fill, drawdown
          overlay band, baseline reference line, and hover tooltip. */}
      <div className="flex-1 flex items-center justify-center py-1 relative">
        <EquityCurveChart
          data={chartData}
          height={85}
          baseline={baseline}
          showDrawdown
          formatX={(ts) => new Date(ts).toISOString().slice(14, 19)}
          formatY={(eq) => `$${eq.toFixed(2)}`}
        />
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
