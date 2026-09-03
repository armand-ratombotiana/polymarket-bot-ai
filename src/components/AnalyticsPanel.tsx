// components/AnalyticsPanel.tsx — Institutional Performance Analytics
'use client'

import { useEffect, useState, useCallback, useRef, memo } from 'react'
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
  // S3 — extended KPI metrics (additive)
  avg_win: number | null
  avg_loss: number | null
  expectancy: number | null
  sharpe_ratio: number | null
}

const STRATEGY_LABELS: Record<string, string> = {
  mm_avellaneda_stoikov: 'Avellaneda-Stoikov MM',
  arb_binary_dutch_book: 'Dutch-Book Arb',
  ml_random_forest_quant: 'RF Quant Ensemble',
}

// W9-6 — wrapped in React.memo. The component takes no props, so React.memo
// with default shallow compare would never re-render. That's incorrect
// here: the panel self-polls every 4s and updates its own state. React.memo
// on a no-prop component is a no-op (only useful to skip when the parent
// re-renders). It IS valuable when this panel is rendered as a child of a
// frequently-re-rendering parent (the command-center grid re-renders on
// every snapshot tick from useBot), so we wrap it to short-circuit those
// parent-driven re-renders. Internal state updates (data/loading) still
// trigger re-renders normally.
function AnalyticsPanel() {
  const [data, setData] = useState<Analytics | null>(null)
  const [loading, setLoading] = useState(true)

  // W9-6 — useCallback for the fetcher so the polling effect can list it
  // as a stable dependency (effect only re-runs on mount). The fetcher
  // references no closures except `setData`/`setLoading` which are stable.
  const fetchAnalytics = useCallback(async () => {
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
  }, [])

  // W9-6 — pause polling when the tab is hidden to avoid burning API
  // quota and CPU on background tabs. Resumes immediately on visibility.
  // The ref holds the current interval id; visibilitychange clears it
  // and restarts it when the tab becomes visible again.
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)

  useEffect(() => {
    const start = () => {
      // immediate first fetch on start (covers both initial mount + resume)
      fetchAnalytics()
      if (intervalRef.current) clearInterval(intervalRef.current)
      intervalRef.current = setInterval(fetchAnalytics, 4000)
    }
    const stop = () => {
      if (intervalRef.current) {
        clearInterval(intervalRef.current)
        intervalRef.current = null
      }
    }
    const onVis = () => {
      if (document.hidden) {
        stop()
      } else {
        start()
      }
    }
    start()
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [fetchAnalytics])

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

  // Determine trend arrow from CI midpoint vs 50%
  const ciMid = data.win_rate_ci_low != null && data.win_rate_ci_high != null
    ? (data.win_rate_ci_low + data.win_rate_ci_high) / 2
    : data.win_rate
  const trendArrow = ciMid > 0.505 ? '▲' : ciMid < 0.495 ? '▼' : '▶'
  const trendColor = ciMid > 0.505 ? 'text-green-400' : ciMid < 0.495 ? 'text-red-400' : 'text-[#7e8aaa]'

  const activeStrats = data.active_strategies ?? []

  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">📊 Performance Analytics</span>
          <span className="badge badge-amber text-[9.5px]">
            {data.mode?.toUpperCase() || 'PAPER'}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className={`mono text-xs font-bold ${trendColor}`}>
            {trendArrow}
          </span>
          <span className="mono text-xs text-green-400 font-bold">
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

      {/* Active Strategies Strip */}
      {activeStrats.length > 0 && (
        <div className="px-3 pt-2.5 flex flex-wrap gap-1.5">
          <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold tracking-wider self-center">Active:</span>
          {activeStrats.map((s) => (
            <span key={s} className="badge badge-green text-[9px]">
              ● {STRATEGY_LABELS[s] ?? s.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      )}

      <div className="p-3 grid grid-cols-2 gap-2 text-[11px]">
        {/* Win Rate + Wilson CI */}
        <div className="kpi-card">
          <span className="kpi-label">Win Rate (Wilson 95% CI)</span>
          <span className="kpi-value text-[#dde1ed]">{winRatePct}%</span>
          <span className="kpi-sub">
            {ciLowPct && ciHighPct ? `[${ciLowPct}% – ${ciHighPct}%] (n=${n})` : `n=${n}`}
          </span>
        </div>

        {/* Profit Factor */}
        <div className="kpi-card">
          <span className="kpi-label">Profit Factor</span>
          <span className="kpi-value text-[#60a5fa]">
            {typeof data.profit_factor === 'number'
              ? data.profit_factor.toFixed(2)
              : data.profit_factor === 'Infinity'
              ? '∞'
              : '—'}
          </span>
          <span className="kpi-sub">Gross wins / Gross losses</span>
        </div>

        {/* Total Trades & Volume */}
        <div className="kpi-card">
          <span className="kpi-label">Trades / Volume</span>
          <span className="kpi-value text-[#dde1ed]">{data.total_trades} trades</span>
          <span className="kpi-sub text-[#22d3ee]">{fmtUsd(data.total_volume_usdc)} vol</span>
        </div>

        {/* Max Drawdown */}
        <div className="kpi-card">
          <span className="kpi-label">Max Drawdown</span>
          <span className="kpi-value text-[#f87171]">
            {fmtUsd(data.max_drawdown_dollars)} ({fmtPct(data.max_drawdown_pct)})
          </span>
          <span className="kpi-sub">Peak: {fmtUsd(data.peak_equity)}</span>
        </div>

        {/* Realized P&L */}
        <div className="kpi-card">
          <span className="kpi-label">Realized P&amp;L</span>
          <span className={`kpi-value ${data.realized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.realized_pnl)}
          </span>
          <span className="kpi-sub">Closed positions today</span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Unrealized P&amp;L</span>
          <span className={`kpi-value ${data.unrealized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.unrealized_pnl)}
          </span>
          <span className="kpi-sub">Mark-to-mid open book</span>
        </div>

        {/* S3 — Expectancy / Trade */}
        <div className="kpi-card">
          <span className="kpi-label">Expectancy / Trade</span>
          <span
            className={`kpi-value ${
              (data.expectancy ?? 0) >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'
            }`}
          >
            {data.expectancy != null ? fmtPnl(data.expectancy) : '—'}
          </span>
          <span className="kpi-sub">Positive = profitable system</span>
        </div>

        {/* S3 — Avg Win / Avg Loss */}
        <div className="kpi-card">
          <span className="kpi-label">Avg Win / Avg Loss</span>
          <span className="kpi-value flex items-baseline gap-1">
            <span className="text-[#4ade80]">
              {data.avg_win != null ? fmtUsd(data.avg_win) : '—'}
            </span>
            <span className="text-[#7e8aaa] text-[10px]">/</span>
            <span className="text-[#f87171]">
              {data.avg_loss != null ? fmtUsd(data.avg_loss) : '—'}
            </span>
          </span>
          <span className="kpi-sub">Asymmetry check</span>
        </div>

        {/* S3 — Sharpe Ratio */}
        <div className="kpi-card">
          <span className="kpi-label">Sharpe Ratio</span>
          <span
            className={`kpi-value ${
              data.sharpe_ratio == null
                ? 'text-[#dde1ed]'
                : data.sharpe_ratio >= 1
                ? 'text-[#4ade80]'
                : data.sharpe_ratio >= 0
                ? 'text-[#60a5fa]'
                : 'text-[#f87171]'
            }`}
          >
            {data.sharpe_ratio != null ? data.sharpe_ratio.toFixed(2) : '—'}
          </span>
          <span className="kpi-sub">Risk-adjusted return</span>
        </div>
      </div>
    </div>
  )
}

// W9-6 — React.memo (no props, default shallow compare). Skips
// re-renders triggered purely by parent re-renders (e.g. useBot snapshot
// updates that don't affect this panel). Internal state updates
// (data/loading) still re-render normally because they originate inside
// the component.
export default memo(AnalyticsPanel)