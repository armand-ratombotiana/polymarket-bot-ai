// components/PositionsPanel.tsx — Pro Portfolio Positions Desk
'use client'

import { useState } from 'react'
import { Position } from '@/hooks/useBot'
import { getApiUrl } from '@/lib/api'

interface Props {
  positions: Position[]
  dailyPnl: number
  onOrderPlaced?: () => void
}

export default function PositionsPanel({ positions, dailyPnl, onOrderPlaced }: Props) {
  const [closing, setClosing] = useState<string | null>(null)

  const activePositions = positions.filter((p) => p.yes_shares > 0 || p.total_invested > 0)
  const totalExposure = activePositions.reduce((acc, p) => acc + p.total_invested, 0)

  const handleClosePosition = async (p: Position) => {
    setClosing(p.token_id)
    try {
      const apiUrl = getApiUrl()
      await fetch(`${apiUrl}/api/trade`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id: p.token_id,
          price: Math.max(p.avg_entry_price * 0.98, 0.01),
          side: 'SELL',
          size_usdc: p.total_invested,
        }),
      })
      if (onOrderPlaced) onOrderPlaced()
    } catch {}
    setClosing(null)
  }

  return (
    <div className="card flex flex-col h-full min-h-0 bg-[#111318] border border-[#252836]">
      {/* Header */}
      <div className="card-header flex justify-between items-center px-3 py-2 border-b border-[#252836]">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">💰 Open Positions</span>
          <span className="badge badge-amber text-[10px] mono">
            ${totalExposure.toFixed(2)} Invested
          </span>
        </div>

        <span
          className={`mono text-xs font-bold ${
            dailyPnl >= 0 ? 'text-green-400' : 'text-red-400'
          }`}
        >
          P&amp;L: {dailyPnl >= 0 ? '+' : ''}${dailyPnl.toFixed(2)}
        </span>
      </div>

      {/* Table */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {activePositions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-28 text-[#4a5068] text-xs gap-1">
            <span>No open market positions.</span>
            <span className="text-[10px] text-[#3b4054]">
              Active YES/NO exposures will be tracked here.
            </span>
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Market</th>
                <th>YES Shares</th>
                <th>Avg Entry</th>
                <th>Total Cost</th>
                <th>Realized P&amp;L</th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {activePositions.map((p) => (
                <tr key={p.token_id} className="hover:bg-blue-500/5 transition-colors">
                  <td className="max-w-[130px]">
                    <span className="text-[#e8eaf0] font-medium truncate block" title={p.slug}>
                      {p.slug || p.token_id.slice(0, 12)}
                    </span>
                  </td>
                  <td className="mono text-cyan-400 font-semibold">{p.yes_shares.toFixed(1)} sh</td>
                  <td className="mono text-[#8b91a8]">${p.avg_entry_price.toFixed(4)}</td>
                  <td className="mono text-amber-400 font-medium">${p.total_invested.toFixed(2)}</td>
                  <td
                    className={`mono font-bold ${
                      p.realised_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {p.realised_pnl >= 0 ? '+' : ''}${p.realised_pnl.toFixed(2)}
                  </td>
                  <td className="text-right">
                    <button
                      onClick={() => handleClosePosition(p)}
                      disabled={closing === p.token_id}
                      className="text-[10px] uppercase font-semibold text-red-400 hover:text-white bg-red-500/10 hover:bg-red-500/30 px-2 py-0.5 rounded border border-red-500/20"
                    >
                      {closing === p.token_id ? '…' : 'Market Exit'}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
