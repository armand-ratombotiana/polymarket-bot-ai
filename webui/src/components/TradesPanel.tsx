// components/TradesPanel.tsx — Recent Trade Executions Feed
'use client'

import { Trade } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtPnl } from '@/lib/design-tokens'

interface Props {
  trades: Trade[]
}

export default function TradesPanel({ trades }: Props) {
  const displayedTrades = trades.slice(0, 50)
  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335]">
      {/* Header */}
      <div className="card-header pb-2 mb-1 border-b border-[#1f2335] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Recent Executions ({trades.length})
          </span>
          <span className="badge badge-dim text-[9.5px]">Audit Stream</span>
        </div>
        {trades.length > 50 && (
          <span className="text-[10px] text-[#7e8aaa] mono">
            Showing 50 of {trades.length}
          </span>
        )}
      </div>

      {/* Trades List */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {trades.length === 0 ? (
          <div className="empty-state py-4">
            <span className="empty-state-icon text-lg" aria-hidden="true">⚡</span>
            <span className="empty-state-title">No executed trades</span>
            <span className="empty-state-desc">
              Fills will appear here as orders match against live Polymarket books.
            </span>
          </div>
        ) : (
          <table className="data-table text-xs" role="table" aria-label="Recent trade execution log">
            <thead>
              <tr>
                <th scope="col" className="min-w-[180px]">Market Contract</th>
                <th scope="col">Side</th>
                <th scope="col" className="text-right">Price</th>
                <th scope="col" className="text-right">Shares</th>
                <th scope="col" className="text-right">P&amp;L</th>
                <th scope="col" className="text-right">Strategy</th>
                <th scope="col" className="text-right">Time</th>
              </tr>
            </thead>
            <tbody>
              {displayedTrades.map((t) => {
                const info = formatHierarchicalMarket(t.slug)
                return (
                  <tr key={t.trade_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2 max-w-[200px]">
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
                      <span
                        className={`badge text-[9px] font-bold ${
                          t.side === 'BUY' ? 'badge-green' : 'badge-red'
                        }`}
                      >
                        {t.side}
                      </span>
                    </td>
                    <td className="mono text-right text-cyan-400 font-bold">
                      {fmtPrice(t.price)}
                    </td>
                    <td className="mono text-right font-medium text-[#dde1ed]">
                      {t.size.toFixed(1)}
                    </td>
                    <td
                      className={`mono text-right font-bold ${
                        t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#7e8aaa]'
                      }`}
                    >
                      {t.pnl !== 0 ? fmtPnl(t.pnl) : '—'}
                    </td>
                    <td className="mono text-right text-[10px] text-[#7e8aaa]">
                      {t.strategy || 'manual'}
                    </td>
                    <td className="mono text-right text-[#7e8aaa] text-[10.5px]">{fmtAge(t.timestamp)}</td>
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
