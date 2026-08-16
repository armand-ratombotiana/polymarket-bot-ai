// components/OrdersPanel.tsx — Live Working Orders Panel with Redesigned Institutional Table UX
'use client'

import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'

interface Props {
  orders: Order[]
  onCancel: (orderId: string) => void
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
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
                <th className="min-w-[180px]">Market Contract</th>
                <th>Side</th>
                <th className="text-right">Price</th>
                <th className="text-right">Size</th>
                <th>Strategy</th>
                <th>Age</th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const info = formatHierarchicalMarket(o.slug)
                return (
                  <tr key={o.order_id} className="hover:bg-blue-500/10 transition-colors">
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
                          o.side === 'BUY' ? 'badge-green' : 'badge-red'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>
                    <td className="mono text-right font-bold text-cyan-400">
                      ${o.price.toFixed(3)}
                    </td>
                    <td className="mono text-right font-medium text-[#e8eaf0]">
                      {o.size.toFixed(1)}
                    </td>
                    <td>
                      <span className="text-[10px] text-[#8b91a8] mono bg-[#111318] px-2 py-0.5 rounded border border-[#252836]">
                        {o.strategy}
                      </span>
                    </td>
                    <td className="mono text-[#8b91a8] text-[10px]">{age(o.created_at)}</td>
                    <td className="text-right">
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="btn btn-danger text-[10px] py-0.5 px-2 font-bold"
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
