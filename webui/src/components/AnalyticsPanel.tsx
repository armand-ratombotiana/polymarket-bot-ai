// components/AnalyticsPanel.tsx
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface Analytics {
  total_trades: number
  winning_trades: number
  losing_trades: number
  win_rate: number
  total_volume_usdc: number
  realized_pnl: number
  open_exposure: number
  active_strategies: string[]
}

export default function AnalyticsPanel() {
  const [data, setData] = useState<Analytics | null>(null)

  useEffect(() => {
    const fetchAnalytics = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await fetch(`${apiUrl}/api/analytics`)
        if (res.ok) {
          setData(await res.json())
        }
      } catch {}
    }

    fetchAnalytics()
    const timer = setInterval(fetchAnalytics, 4000)
    return () => clearInterval(timer)
  }, [])

  if (!data) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#4a5068]">
        Loading metrics…
      </div>
    )
  }

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <span className="card-title">📊 Performance Analytics</span>
        <span className="text-[11px] text-green-400 font-semibold mono">
          {(data.win_rate * 100).toFixed(0)}% Win Rate
        </span>
      </div>
      <div className="p-3 grid grid-cols-2 gap-2 text-[11px]">
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Total Trades</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">{data.total_trades}</span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Volume Traded</span>
          <span className="mono font-semibold text-cyan-400 text-[13px]">
            ${data.total_volume_usdc.toLocaleString('en', { minimumFractionDigits: 1 })}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Open Exposure</span>
          <span className="mono font-semibold text-amber-400 text-[13px]">
            ${data.open_exposure.toFixed(2)}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Realised P&amp;L</span>
          <span className={`mono font-semibold text-[13px] ${data.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
            {data.realized_pnl >= 0 ? '+' : ''}${data.realized_pnl.toFixed(2)}
          </span>
        </div>
      </div>
    </div>
  )
}
