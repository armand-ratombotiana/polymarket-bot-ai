// components/PositionsPanel.tsx
'use client'

import { Position } from '@/hooks/useBot'

interface Props {
  positions: Position[]
  dailyPnl: number
}

function PnlCell({ v }: { v: number }) {
  if (Math.abs(v) < 0.001) return <span className="pnl-zero">$0.00</span>
  return (
    <span className={v >= 0 ? 'pnl-positive' : 'pnl-negative'}>
      {v >= 0 ? '+' : ''}${Math.abs(v).toFixed(2)}
    </span>
  )
}

export default function PositionsPanel({ positions, dailyPnl }: Props) {
  const totalInvested = positions.reduce((s, p) => s + p.total_invested, 0)
  const totalRPnl = positions.reduce((s, p) => s + p.realised_pnl, 0)

  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header">
        <span className="card-title">💰 Positions</span>
        <div className="flex items-center gap-3 text-[11px]">
          <span className="text-[#4a5068]">Invested</span>
          <span className="mono text-[#e8eaf0]">${totalInvested.toFixed(2)}</span>
          <span className="text-[#4a5068]">R-PnL</span>
          <PnlCell v={totalRPnl} />
        </div>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1">
        {positions.length === 0 ? (
          <div className="flex items-center justify-center h-24 text-[#4a5068] text-xs">
            No open positions
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Market</th>
                <th>YES Shares</th>
                <th>Avg Entry</th>
                <th>Invested</th>
                <th>R-P&L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map(p => (
                <tr key={p.token_id}>
                  <td>
                    <span className="text-[#e8eaf0] truncate block max-w-[140px]" title={p.slug}>{p.slug}</span>
                  </td>
                  <td className="mono text-[#e8eaf0]">{p.yes_shares.toFixed(2)}</td>
                  <td className="mono text-[#8b91a8]">{p.avg_entry_price.toFixed(4)}</td>
                  <td className="mono text-[#8b91a8]">${p.total_invested.toFixed(2)}</td>
                  <td><PnlCell v={p.realised_pnl} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {/* Daily PnL footer */}
      <div className="border-t border-[#1a1d2a] px-4 py-2 flex items-center justify-between">
        <span className="text-[11px] text-[#4a5068]">Daily P&amp;L</span>
        <PnlCell v={dailyPnl} />
      </div>
    </div>
  )
}
