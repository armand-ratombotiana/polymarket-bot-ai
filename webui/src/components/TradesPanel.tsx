// components/TradesPanel.tsx — Recent Trade Executions Feed with Redesigned Institutional Table UX
'use client'

import { Trade } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'

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
  return (
    <div className="card h-full flex flex-col p-3.5 bg-[#161822] border border-[#252836]">
      {/* Header */}
      <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚡ Recent Executions ({trades.length})
          </span>
          <span className="badge badge-dim text-[10px]">Audit Stream</span>
        </div>
      </div>

      {/* Trades List */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {trades.length === 0 ? (
          <div className="flex items-center justify-center h-20 text-[#4a5068] text-xs">
            No executed trades in this session
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th className="min-w-[180px]">Market Contract</th>
                <th>Side</th>
                <th className="text-right">Price</th>
                <th className="text-right">Size</th>
                <th className="text-right">P&amp;L</th>
                <th className="text-right">Time</th>
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 50).map((t) => {
                const info = formatHierarchicalMarket(t.slug)
                return (
                  <tr key={t.trade_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2 max-w-[200px]">
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
                      <span
                        className={`badge text-[9px] font-bold ${
                          t.side === 'BUY' ? 'badge-green' : 'badge-red'
                        }`}
                      >
                        {t.side}
                      </span>
                    </td>
                    <td className="mono text-right text-cyan-400 font-bold">
                      ${t.price.toFixed(3)}
                    </td>
                    <td className="mono text-right font-medium text-[#e8eaf0]">
                      {t.size.toFixed(1)}
                    </td>
                    <td
                      className={`mono text-right font-bold ${
                        t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#8b91a8]'
                      }`}
                    >
                      {t.pnl !== 0 ? `${t.pnl > 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '—'}
                    </td>
                    <td className="mono text-right text-[#8b91a8] text-[10px]">{age(t.timestamp)}</td>
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
