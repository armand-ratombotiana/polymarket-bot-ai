// components/OrdersPanel.tsx — Live Working Orders Panel
'use client'

import { Order } from '@/hooks/useBot'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

interface Props {
  orders: Order[]
  onCancel: (orderId: string) => void
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m`
}

export default function OrdersPanel({ orders, onCancel }: Props) {
  return (
    <div className="card h-full flex flex-col p-3.5 bg-[#161822] border border-[#252836]">
      {/* Header */}
      <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            📋 Open Orders ({orders.length})
          </span>
          <span className="badge badge-dim text-[10px]">Queue Active</span>
        </div>
        <span className="text-[10px] text-[#8b91a8] mono">
          {orders.length} active in book
        </span>
      </div>

      {/* Orders Table */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {orders.length === 0 ? (
          <div className="flex items-center justify-center h-28 text-[#4a5068] text-xs">
            No working limit orders in queue
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Market Contract</th>
                <th>Side</th>
                <th>Price</th>
                <th>Size</th>
                <th>Strategy</th>
                <th>Age</th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const title = formatMarketTitle(o.slug)
                const cat = getCategoryBadge('', o.slug)
                return (
                  <tr key={o.order_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="max-w-[140px]">
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
                          o.side === 'BUY' ? 'badge-green' : 'badge-red'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>
                    <td className="mono font-semibold text-cyan-400">
                      ${o.price.toFixed(3)}
                    </td>
                    <td className="mono text-[#e8eaf0]">
                      {o.size.toFixed(1)}
                    </td>
                    <td>
                      <span className="text-[10px] text-[#8b91a8] mono bg-[#111318] px-1.5 py-0.5 rounded border border-[#252836]">
                        {o.strategy}
                      </span>
                    </td>
                    <td className="mono text-[#4a5068] text-[10px]">{age(o.created_at)}</td>
                    <td className="text-right">
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="text-[10px] text-red-400 hover:text-red-300 hover:bg-red-500/10 px-2 py-0.5 rounded border border-red-500/20"
                      >
                        Cancel
                      </button>
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
