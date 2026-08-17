// components/OrdersPanel.tsx — Live Working Orders Panel
'use client'

import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice } from '@/lib/design-tokens'

interface Props {
  orders: Order[]
  onCancel: (orderId: string) => void
  onCancelAll?: () => void
}

export default function OrdersPanel({ orders, onCancel, onCancelAll }: Props) {
  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335]">
      {/* Header */}
      <div className="card-header pb-2 mb-1 border-b border-[#1f2335] flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            📋 Open Orders ({orders.length})
          </span>
          <span className="badge badge-dim text-[9.5px]">Working Book Queue</span>
        </div>
        <div className="flex items-center gap-2">
          {orders.length > 0 && onCancelAll && (
            <button
              onClick={onCancelAll}
              className="btn btn-danger btn-xs"
              aria-label="Cancel all open orders"
            >
              Cancel All ({orders.length})
            </button>
          )}
        </div>
      </div>

      {/* Orders Table */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {orders.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state-icon" aria-hidden="true">📋</span>
            <span className="empty-state-title">No working limit orders</span>
            <span className="empty-state-desc">
              Active strategy quote loops will place limit orders in the CLOB matching engine.
            </span>
          </div>
        ) : (
          <table className="data-table text-xs" role="table" aria-label="Working limit orders">
            <thead>
              <tr>
                <th scope="col" className="min-w-[180px]">Market Contract</th>
                <th scope="col">Side</th>
                <th scope="col" className="text-right">Price</th>
                <th scope="col" className="text-right">Size (Filled)</th>
                <th scope="col">Strategy</th>
                <th scope="col">Age</th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const info = formatHierarchicalMarket(o.slug)
                const matched = o.size_matched ?? 0
                return (
                  <tr key={o.order_id} className="hover:bg-blue-500/10 transition-colors">
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
                          o.side === 'BUY' ? 'badge-green' : 'badge-red'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>
                    <td className="mono text-right font-bold text-cyan-400">
                      {fmtPrice(o.price)}
                    </td>
                    <td className="mono text-right font-medium text-[#dde1ed]">
                      {o.size.toFixed(1)} {matched > 0 && <span className="text-[10px] text-green-400">({matched.toFixed(1)})</span>}
                    </td>
                    <td>
                      <span className="text-[10px] text-[#7e8aaa] mono bg-[#0e1015] px-1.5 py-0.5 rounded border border-[#1f2335]">
                        {o.strategy}
                      </span>
                    </td>
                    <td className="mono text-[#7e8aaa] text-[10.5px]">{fmtAge(o.created_at)}</td>
                    <td className="text-right">
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="btn btn-danger btn-xs"
                        aria-label={`Cancel order ${o.order_id}`}
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
