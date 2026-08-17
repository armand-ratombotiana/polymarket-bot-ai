// components/AnalyticsPanel.tsx — Institutional Performance Analytics
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct } from '@/lib/design-tokens'

interface Analytics {
  equity: number
  realized_pnl: number
  unrealized_pnl: number
  net_pnl: number
  total_trades: number
  winning_trades: number
  losing_trades: number
  closed_trades: number
  open_trades: number
  win_rate: number
  win_rate_ci_low: number | null
  win_rate_ci_high: number | null
  profit_factor: number | string | null
  max_drawdown_dollars: number
  max_drawdown_pct: number
  total_volume_usdc: number
  open_exposure: number
  open_position_count: number
  pending_order_capital: number
  risk_utilization: number
  mode: string
  data_freshness_seconds: number
  peak_equity: number
  active_strategies: string[]
}

export default function AnalyticsPanel() {
  const [data, setData] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchAnalytics = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/analytics`)
        if (res.ok) {
          setData(await res.json())
        }
      } catch {
      } finally {
        setLoading(false)
      }
    }

    fetchAnalytics()
    const timer = setInterval(fetchAnalytics, 4000)
    return () => clearInterval(timer)
  }, [])

  if (loading && !data) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#7e8aaa]">
        <span className="spinner mr-2" aria-hidden="true" />
        Loading analytics metrics…
      </div>
    )
  }

  if (!data) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#7e8aaa]">
        Analytics data unavailable
      </div>
    )
  }

  const n = data.closed_trades ?? (data.winning_trades + data.losing_trades)
  const isSmallSample = n < 10
  const winRatePct = (data.win_rate * 100).toFixed(1)
  const ciLowPct = data.win_rate_ci_low != null ? (data.win_rate_ci_low * 100).toFixed(1) : null
  const ciHighPct = data.win_rate_ci_high != null ? (data.win_rate_ci_high * 100).toFixed(1) : null

  return (
    <div className="card flex flex-col">
      <div className="card-header flex justify-between items-center">
        <div className="flex items-center gap-2">
          <span className="card-title">📊 Performance Analytics</span>
          <span className="badge badge-amber text-[9.5px]">
            {data.mode?.toUpperCase() || 'PAPER'}
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="mono text-xs text-green-400 font-semibold">
            {winRatePct}% Win Rate
          </span>
        </div>
      </div>

      {/* Small sample warning */}
      {isSmallSample && (
        <div className="banner-warning text-[10.5px] mx-3 mt-2 py-1.5 px-2.5" role="alert">
          <span aria-hidden="true">⚠️</span>
          <span>
            Small sample ({n} closed trades). Win rate CI is broad [{ciLowPct ?? '0.0'}% – {ciHighPct ?? '100.0'}%].
          </span>
        </div>
      )}

      <div className="p-3 grid grid-cols-2 gap-2 text-[11px]">
        {/* Win Rate + Wilson CI */}
        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Win Rate (Wilson 95% CI)</span>
          <span className="mono font-semibold text-[#dde1ed] text-[13px]">
            {winRatePct}%
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5 mono">
            {ciLowPct && ciHighPct ? `[${ciLowPct}% – ${ciHighPct}%] (n=${n})` : `n=${n}`}
          </span>
        </div>

        {/* Profit Factor */}
        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Profit Factor</span>
          <span className="mono font-semibold text-[#60a5fa] text-[13px]">
            {typeof data.profit_factor === 'number'
              ? data.profit_factor.toFixed(2)
              : data.profit_factor === 'Infinity'
              ? '∞'
              : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">
            Gross wins / Gross losses
          </span>
        </div>

        {/* Total Trades & Volume */}
        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Trades / Volume</span>
          <span className="mono font-semibold text-[#dde1ed] text-[13px]">
            {data.total_trades} trades
          </span>
          <span className="text-[9.5px] text-[#22d3ee] block mt-0.5 mono">
            {fmtUsd(data.total_volume_usdc)} vol
          </span>
        </div>

        {/* Max Drawdown */}
        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Max Drawdown</span>
          <span className="mono font-semibold text-[#f87171] text-[13px]">
            {fmtUsd(data.max_drawdown_dollars)} ({fmtPct(data.max_drawdown_pct)})
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5 mono">
            Peak: {fmtUsd(data.peak_equity)}
          </span>
        </div>

        {/* Realized vs Unrealized P&L */}
        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Realized P&L</span>
          <span className={`mono font-semibold text-[13px] ${data.realized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.realized_pnl)}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">
            Closed positions today
          </span>
        </div>

        <div className="bg-[#0e1015] p-2 rounded border border-[#1f2335]">
          <span className="text-[#7e8aaa] block text-[10px] uppercase font-semibold">Unrealized P&L</span>
          <span className={`mono font-semibold text-[13px] ${data.unrealized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.unrealized_pnl)}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">
            Mark-to-mid open book
          </span>
        </div>
      </div>
    </div>
  )
}
