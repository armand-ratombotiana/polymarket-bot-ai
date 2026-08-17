// components/PositionsPanel.tsx — Active Portfolio Positions & P&L
'use client'

import { Position } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtPnl, fmtUsd } from '@/lib/design-tokens'

interface Props {
  positions: Position[]
  dailyPnl: number
}

export default function PositionsPanel({ positions, dailyPnl: _dailyPnl }: Props) {
  const totalInvested = positions.reduce((acc, p) => acc + p.total_invested, 0)
  const totalRealized = positions.reduce((acc, p) => acc + p.realised_pnl, 0)

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335]">
      {/* Header */}
      <div className="card-header pb-2 mb-1 border-b border-[#1f2335] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            💼 Open Positions ({positions.length})
          </span>
          <span className="badge badge-amber text-[9.5px]">Paper Portfolio</span>
        </div>

        <div className="flex items-center gap-3 text-xs">
          <div>
            <span className="text-[#7e8aaa]">Invested: </span>
            <span className="mono font-bold text-cyan-400">
              {fmtUsd(totalInvested)}
            </span>
          </div>
          <div>
            <span className="text-[#7e8aaa]">Realized: </span>
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
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {positions.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state-icon" aria-hidden="true">💼</span>
            <span className="empty-state-title">No open positions</span>
            <span className="empty-state-desc">
              Automated strategies (Market Maker, Arbitrage, Signal Trader) or manual trades will open positions here.
            </span>
          </div>
        ) : (
          <table className="data-table text-xs" role="table" aria-label="Portfolio open positions">
            <thead>
              <tr>
                <th scope="col" className="min-w-[180px]">Market Contract</th>
                <th scope="col">Outcome</th>
                <th scope="col" className="text-right">Shares</th>
                <th scope="col" className="text-right">Avg Entry</th>
                <th scope="col" className="text-right">Cost Basis</th>
                <th scope="col" className="text-right">Realized P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const info = formatHierarchicalMarket(p.slug)
                return (
                  <tr key={p.token_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2 max-w-[220px]">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-[9px] text-[#7e8aaa] uppercase font-bold tracking-wider truncate">
                          {info.category.icon} {info.eventTitle}
                        </span>
                        <span className="text-[#dde1ed] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                          {info.question}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className={`badge text-[9px] font-bold ${p.yes_shares > 0 ? 'badge-green' : 'badge-red'}`}>
                        {p.yes_shares > 0 ? 'YES' : 'NO'}
                      </span>
                    </td>
                    <td className="mono text-right font-semibold text-[#dde1ed]">
                      {p.yes_shares.toFixed(1)}
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">
                      ${p.avg_entry_price.toFixed(3)}
                    </td>
                    <td className="mono text-right font-medium text-[#dde1ed]">
                      {fmtUsd(p.total_invested)}
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
