// components/LeaderboardPanel.tsx
// Strategy leaderboard ranked by reproducible risk-adjusted net performance.
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

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

export default function LeaderboardPanel() {
  const [rows, setRows] = useState<StrategyRow[]>([])

  useEffect(() => {
    const fetchLeaderboard = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/leaderboard`)
        if (res.ok) {
          const data = await res.json()
          setRows(data.ranked ?? [])
        }
      } catch {}
    }

    fetchLeaderboard()
    const timer = setInterval(fetchLeaderboard, 6000)
    return () => clearInterval(timer)
  }, [])

  if (rows.length === 0) {
    return (
      <div className="card p-3 flex flex-col justify-between bg-[#13161e] border border-[#1f2335]">
        <div className="card-header pb-1.5 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">🏆 Strategy Leaderboard</span>
          <span className="badge badge-dim text-[9.5px]">Risk-Adjusted</span>
        </div>
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
        <span className="card-title text-xs font-bold text-[#dde1ed]">🏆 Strategy Leaderboard</span>
        <span className="badge badge-amber text-[9.5px]">Ranked by Score</span>
      </div>
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