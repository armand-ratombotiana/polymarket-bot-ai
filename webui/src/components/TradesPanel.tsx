// components/TradesPanel.tsx — Pro Trade Audit & Fills Log
'use client'

import { useState } from 'react'
import { Trade } from '@/hooks/useBot'

interface Props {
  trades: Trade[]
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

export default function TradesPanel({ trades }: Props) {
  const [filter, setFilter] = useState<'all' | 'wins' | 'losses'>('all')

  const filtered = trades.filter((t) => {
    if (filter === 'wins') return t.pnl > 0
    if (filter === 'losses') return t.pnl < 0
    return true
  })

  return (
    <div className="card flex flex-col h-full min-h-0 bg-[#111318] border border-[#252836]">
      <div className="card-header flex justify-between items-center px-3 py-2 border-b border-[#252836]">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">🔄 Recent Fills &amp; Trades</span>
          <span className="text-[10px] text-[#8b91a8] mono">({trades.length})</span>
        </div>

        <div className="flex items-center gap-1 bg-[#161822] p-0.5 rounded border border-[#252836]">
          {(['all', 'wins', 'losses'] as Array<'all' | 'wins' | 'losses'>).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              className={`px-2 py-0.5 rounded text-[10px] uppercase font-semibold transition-all ${
                filter === f ? 'bg-blue-500 text-black' : 'text-[#8b91a8] hover:text-white'
              }`}
            >
              {f}
            </button>
          ))}
        </div>
      </div>

      <div className="overflow-auto scrollbar-thin flex-1">
        {filtered.length === 0 ? (
          <div className="flex items-center justify-center h-24 text-[#4a5068] text-xs">
            No trade fills recorded yet.
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Side</th>
                <th>Market</th>
                <th>Price</th>
                <th>Size / Volume</th>
                <th>Realized P&amp;L</th>
                <th>Age</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((t) => {
                const vol = t.price * t.size
                return (
                  <tr key={t.trade_id} className="hover:bg-blue-500/5 transition-colors">
                    <td>
                      <span
                        className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                          t.side === 'BUY'
                            ? 'bg-green-500/20 text-green-400 border border-green-500/40'
                            : 'bg-red-500/20 text-red-400 border border-red-500/40'
                        }`}
                      >
                        {t.side}
                      </span>
                    </td>
                    <td className="max-w-[130px]">
                      <span className="text-[#e8eaf0] font-medium truncate block" title={t.slug}>
                        {t.slug || t.trade_id.slice(0, 12)}
                      </span>
                    </td>
                    <td className="mono font-semibold text-[#e8eaf0]">{t.price.toFixed(4)}</td>
                    <td className="mono text-[#8b91a8]">
                      {t.size.toFixed(1)} sh (${vol.toFixed(2)})
                    </td>
                    <td>
                      <span
                        className={`mono font-bold text-xs ${
                          t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#8b91a8]'
                        }`}
                      >
                        {t.pnl > 0 ? '+' : ''}${t.pnl.toFixed(2)}
                      </span>
                    </td>
                    <td className="text-[#4a5068] mono text-[10px]">{age(t.timestamp)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
