// components/PositionsPanel.tsx — Active Portfolio Positions & P&L with Institutional Table UX
'use client'

import { Position } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'

interface Props {
  positions: Position[]
  dailyPnl: number
}

function fmtPnl(v: number) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${Math.abs(v).toFixed(2)}`
}

export default function PositionsPanel({ positions, dailyPnl }: Props) {
  const totalInvested = positions.reduce((acc, p) => acc + p.total_invested, 0)
  const totalRealized = positions.reduce((acc, p) => acc + p.realised_pnl, 0)

  return (
    <div className="card h-full flex flex-col p-3.5 bg-[#161822] border border-[#252836]">
      {/* Header */}
      <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            💼 Open Positions ({positions.length})
          </span>
          <span className="badge badge-amber text-[10px]">$100 Operating Capital</span>
        </div>

        <div className="flex items-center gap-3 text-xs">
          <div>
            <span className="text-[#8b91a8]">Invested: </span>
            <span className="mono font-bold text-cyan-400">
              ${totalInvested.toFixed(2)}
            </span>
          </div>
          <div>
            <span className="text-[#8b91a8]">Realized: </span>
            <span
              className={`mono font-bold ${
                totalRealized >= 0 ? 'text-green-400' : 'text-red-400'
              }`}
            >
              {fmtPnl(totalRealized)}
            </span>
          </div>
        </div>
      </div>

      {/* Positions Table */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {positions.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-28 text-[#4a5068] text-xs">
            <span className="font-semibold">No active positions</span>
            <span className="text-[10px] text-[#4a5068] mt-0.5">Automated strategies will open positions when alpha exceeds threshold</span>
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th className="min-w-[200px]">Market Contract</th>
                <th>Outcome</th>
                <th className="text-right">Shares</th>
                <th className="text-right">Entry</th>
                <th className="text-right">Invested</th>
                <th className="text-right">Realized P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const info = formatHierarchicalMarket(p.slug)
                return (
                  <tr key={p.token_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2 max-w-[220px]">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-[9px] text-[#8b91a8] uppercase font-bold tracking-wider truncate">
                          {info.category.icon} {info.eventTitle}
                        </span>
                        <span className="text-[#e8eaf0] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                          {info.question}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className={`badge text-[9px] font-bold ${p.yes_shares > 0 ? 'badge-green' : 'badge-red'}`}>
                        {p.yes_shares > 0 ? 'YES' : 'NO'}
                      </span>
                    </td>
                    <td className="mono text-right font-semibold text-[#e8eaf0]">
                      {p.yes_shares.toFixed(1)}
                    </td>
                    <td className="mono text-right text-[#8b91a8]">
                      ${p.avg_entry_price.toFixed(3)}
                    </td>
                    <td className="mono text-right font-medium text-[#e8eaf0]">
                      ${p.total_invested.toFixed(2)}
                    </td>
                    <td
                      className={`mono text-right font-bold ${
                        p.realised_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}
                    >
                      {fmtPnl(p.realised_pnl)}
                    </td>
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
