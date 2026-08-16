// components/LeaderboardPanel.tsx
// Strategy leaderboard ranked by reproducible risk-adjusted net performance.
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

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
        const res = await fetch(`${apiUrl}/api/leaderboard`)
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
      <div className="card p-3 flex items-center justify-center text-xs text-[#4a5068]">
        No strategy results yet.
      </div>
    )
  }

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <span className="card-title">🏆 Strategy Leaderboard</span>
        <span className="text-[11px] text-[#4a5068]">risk-adjusted</span>
      </div>
      <div className="p-3 space-y-1.5">
        {rows.map((r, i) => (
          <div key={r.strategy} className="flex items-center justify-between text-[11px]">
            <div className="flex items-center gap-2 min-w-0">
              <span className={`w-4 text-center mono ${i === 0 ? 'text-amber-400' : 'text-[#4a5068]'}`}>
                {i + 1}
              </span>
              <span className="truncate text-[#e8eaf0]">{r.strategy}</span>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              <span className="text-[#4a5068]">{r.closed_trades}W</span>
              <span className="mono text-[#4a5068]">
                {(r.win_rate * 100).toFixed(0)}%
              </span>
              <span className={`mono font-semibold ${r.risk_adjusted_score >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {r.risk_adjusted_score.toFixed(2)}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}