// components/AnalyticsPanel.tsx — Institutional Performance Analytics
//
// W15-5 — Migrated from a self-managed 4-second REST polling loop to
// the hybrid `useRealtimeData` hook. The panel now:
//   1. REST-prefetches /api/analytics on mount.
//   2. Subscribes to the `metrics` WS channel for live push updates.
//      Note: the `metrics` channel pushes the full BotSnapshot, whose
//      shape doesn't match the Analytics object the panel renders. To
//      avoid clobbering the typed state with mismatched data, the hook
//      is given a `validate` predicate that drops any payload missing
//      the `equity` field. When the backend eventually pushes Analytics
//      objects over the metrics channel, the validator will accept them.
//   3. Falls back to polling /api/analytics every 10s when the WS isn't
//      connected.
//   4. Renders a "● Live" / "⟳ Polling" badge so the trader can tell at
//      a glance whether the KPIs are real-time or lagged.
'use client'

import { memo } from 'react'
import { fmtUsd, fmtPnl, fmtPct } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'

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

// W15-5 — type guard for the metrics WS channel. The channel is
// specified by the task as `metrics`, whose canonical payload is a
// BotSnapshot (mode / kill_switch / order_books / etc.) — that's NOT
// the Analytics shape this panel renders. We accept only payloads
// that look like Analytics (have the `equity` numeric field); the
// REST polling continues to drive the displayed KPIs in the meantime.
function isAnalyticsPayload(d: unknown): boolean {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return typeof obj.equity === 'number' && typeof obj.win_rate === 'number'
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
  // W15-5 — hybrid REST + WS subscription. Replaces the previous 4s
  // self-managed setInterval + visibilitychange listener (the
  // useRealtimeData hook handles both concerns generically).
  const { data, isLoading, isRealtime } = useRealtimeData<Analytics>(
    '/api/analytics',
    {
      wsChannel: 'metrics',
      pollInterval: 10000, // was 4s; relaxed to 10s with WS live updates
      validate: isAnalyticsPayload,
    },
  )

  if (isLoading && !data) {
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
          {/* W15-5 — Live / Polling badge. Reflects the underlying
              useRealtimeData transport state. */}
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
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