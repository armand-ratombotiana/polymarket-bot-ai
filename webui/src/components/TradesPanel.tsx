// components/TradesPanel.tsx — Recent Trade Executions Feed
'use client'

import { Trade } from '@/hooks/useBot'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

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
                <th>Market Contract</th>
                <th>Side</th>
                <th>Price</th>
                <th>Size</th>
                <th>P&amp;L</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {trades.slice(0, 50).map((t) => {
                const title = formatMarketTitle(t.slug)
                const cat = getCategoryBadge('', t.slug)
                return (
                  <tr key={t.trade_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="max-w-[130px]">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs shrink-0">{cat.icon}</span>
                        <span className="text-[#e8eaf0] font-semibold block truncate text-[11px]" title={title}>
                          {title}
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
                    <td className="mono text-cyan-400 font-semibold">
                      ${t.price.toFixed(3)}
                    </td>
                    <td className="mono text-[#e8eaf0]">
                      {t.size.toFixed(1)}
                    </td>
                    <td
                      className={`mono font-bold ${
                        t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#8b91a8]'
                      }`}
                    >
                      {t.pnl !== 0 ? `${t.pnl > 0 ? '+' : ''}$${t.pnl.toFixed(2)}` : '—'}
                    </td>
                    <td className="mono text-[#4a5068] text-[10px]">{age(t.timestamp)}</td>
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
