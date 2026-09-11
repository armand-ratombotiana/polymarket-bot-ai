// components/LeaderboardPanel.tsx — Strategy leaderboard ranked by
// reproducible risk-adjusted net performance.
//
// W22-5 — Migrated from the self-managed 6-second REST polling loop to
// the hybrid `useRealtimeData` hook. The panel now:
//   1. REST-prefetches /api/leaderboard on mount.
//   2. Subscribes to the `metrics` WS channel for live push updates.
//      The `metrics` channel pushes the full BotSnapshot, whose shape
//      doesn't match the LeaderboardResponse `{ ranked: StrategyRow[] }`
//      the panel renders. To avoid clobbering the typed state with
//      mismatched data, the hook is given a `validate` predicate that
//      drops any payload missing the `ranked` array.
//   3. Falls back to polling /api/leaderboard every 10s when the WS
//      isn't connected.
//   4. Renders a "● Live" / "⟳ Polling" badge so the trader can tell at
//      a glance whether the rankings are real-time or lagged.
'use client'

import { useState, useEffect } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'

interface StrategyRow {
  strategy: string
  fills: number
  closed_trades: number
  net_pnl: number
  win_rate: number
  profit_factor: number | null
  open_exposure: number
  max_drawdown: number
  risk_adjusted_score: number
}

interface LeaderboardResponse {
  ranked?: StrategyRow[]
}

// W22-5 — type guard for the metrics WS channel. The channel pushes
// the full BotSnapshot by default; only payloads that look like a
// LeaderboardResponse (have the `ranked` array) are accepted. When the
// payload doesn't match, the data state is left untouched and the REST
// polling continues to drive the displayed rankings.
function isLeaderboardPayload(d: unknown): boolean {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return Array.isArray(obj.ranked)
}

export default function LeaderboardPanel() {
  const { data, isLoading, isRealtime, error } = useRealtimeData<LeaderboardResponse>(
    '/api/leaderboard',
    {
      wsChannel: 'metrics',
      pollInterval: 10000,
      validate: isLeaderboardPayload,
    },
  )

  const rows: StrategyRow[] = data?.ranked ?? []

  // W22-1 backwards-compat — surface fetch failures via a dismissable
  // inline banner so the trader knows the leaderboard fetch failed and
  // the rankings are stale. useRealtimeData exposes the latest error
  // string (or null when the last fetch succeeded); we mirror it into
  // local state so we can track dismissal independently. The banner
  // re-appears whenever a fresh error arrives.
  const wrappedError = error ? `Leaderboard: ${error}` : null
  const [dismissedError, setDismissedError] = useState<string | null>(null)
  useEffect(() => {
    if (wrappedError && wrappedError !== dismissedError) {
      setDismissedError(null)
    }
  }, [wrappedError, dismissedError])
  const showError = wrappedError && wrappedError !== dismissedError
  const dismissError = () => setDismissedError(wrappedError)
  const errorBanner = showError && (
    <div
      className="banner-danger text-[10.5px] mx-3 mt-2 py-1.5 px-2.5 flex items-center justify-between"
      role="alert"
    >
      <span className="flex items-center gap-1.5">
        <AlertTriangle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
        <span>{wrappedError}</span>
      </span>
      <button
        onClick={dismissError}
        className="hover:underline text-xs flex items-center gap-0.5 shrink-0"
        aria-label="Dismiss leaderboard error"
      >
        <X className="w-3 h-3" aria-hidden="true" /> Dismiss
      </button>
    </div>
  )

  if (isLoading && rows.length === 0) {
    return (
      <div className="card p-3 flex flex-col justify-between bg-[#13161e] border border-[#1f2335]">
        <div className="card-header pb-1.5 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">🏆 Strategy Leaderboard</span>
          <span className="badge badge-dim text-[9.5px]">Risk-Adjusted</span>
        </div>
        {errorBanner}
        <div className="flex items-center justify-center py-6 text-xs text-[#7e8aaa]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading leaderboard…
        </div>
      </div>
    )
  }

  if (rows.length === 0) {
    return (
      <div className="card p-3 flex flex-col justify-between bg-[#13161e] border border-[#1f2335]">
        <div className="card-header pb-1.5 border-b border-[#1f2335] flex justify-between items-center">
          <div className="flex items-center gap-2">
            <span className="card-title text-xs font-bold text-[#dde1ed]">🏆 Strategy Leaderboard</span>
            {isRealtime ? (
              <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
            ) : (
              <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
            )}
          </div>
          <span className="badge badge-dim text-[9.5px]">Risk-Adjusted</span>
        </div>
        {errorBanner}
        <div className="flex flex-col items-center justify-center py-6 text-xs text-[#7e8aaa] text-center">
          <span className="text-xl mb-1" aria-hidden="true">🏆</span>
          <span>No closed trades yet</span>
          <span className="text-[10px] text-[#3e4560] mt-0.5">Rankings populate as strategies close positions</span>
        </div>
      </div>
    )
  }

  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">🏆 Strategy Leaderboard</span>
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>
        <span className="badge badge-amber text-[9.5px]">Ranked by Score</span>
      </div>
      {errorBanner}
      <div className="p-2.5 space-y-1.5">
        {rows.map((r, i) => {
          const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i + 1}.`
          return (
            <div
              key={r.strategy}
              className="flex items-center justify-between text-xs bg-[#0e1015] px-2.5 py-1.5 rounded border border-[#1f2335] hover:border-blue-500/30 transition-colors"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="w-5 text-center text-xs font-bold shrink-0">
                  {medal}
                </span>
                <span className="truncate font-semibold text-[#dde1ed] text-[11px]">{r.strategy}</span>
              </div>
              <div className="flex items-center gap-2.5 shrink-0">
                <span className="text-[10px] text-[#7e8aaa] mono">{r.closed_trades}W</span>
                <span className="mono text-[11px] text-cyan-300 font-medium">
                  {(r.win_rate * 100).toFixed(0)}%
                </span>
                {r.profit_factor !== null ? (
                  <span className="badge badge-blue text-[9px]">PF {r.profit_factor.toFixed(2)}</span>
                ) : (
                  <span className="badge badge-dim text-[9px]">PF —</span>
                )}
                <span className={`mono text-[10px] ${r.max_drawdown < 0 ? 'text-red-400' : 'text-[#7e8aaa]'}`}>
                  DD ${r.max_drawdown.toFixed(2)}
                </span>
                <span className={`mono text-[10px] font-medium ${r.net_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {r.net_pnl >= 0 ? '+' : '-'}${Math.abs(r.net_pnl).toFixed(2)}
                </span>
                <span className={`mono font-bold text-xs ${r.risk_adjusted_score >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {r.risk_adjusted_score >= 0 ? '+' : ''}{r.risk_adjusted_score.toFixed(2)}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
