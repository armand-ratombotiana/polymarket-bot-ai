// components/OrdersPanel.tsx — Live Working Orders & Execution Queue Panel
'use client'

import { useMemo, memo } from 'react'
import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtUsd } from '@/lib/design-tokens'

interface Props {
  orders: Order[]
  onCancel: (orderId: string) => void
  onCancelAll?: () => void
}

// W9-6 — wrapped in React.memo. Props: `orders` (new array on every
// snapshot — memo won't skip many renders by itself), `onCancel` (must
// be stable in parent — useCallback in useBot), `onCancelAll` (must be
// stable in parent — useCallback in page.tsx). When all three are stable
// AND the orders array reference is unchanged, the table skips re-render.
function OrdersPanel({ orders, onCancel, onCancelAll }: Props) {
  const totalOpenExposure = useMemo(() => {
    return orders.reduce((acc, o) => acc + o.price * (o.size - (o.size_matched ?? 0)), 0)
  }, [orders])

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-xl overflow-hidden">
      {/* Header */}
      <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center justify-between bg-[#0e1015]/80">
        <div className="flex items-center gap-2.5">
          <span className="card-title text-xs font-bold text-[#dde1ed] flex items-center gap-1.5">
            📋 Working Orders ({orders.length})
          </span>
          {orders.length > 0 && (
            <span className="text-[10.5px] text-[#7e8aaa] mono hidden sm:inline-block">
              Open Capital: <strong className="text-cyan-300 font-semibold">{fmtUsd(totalOpenExposure)}</strong>
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {orders.length > 0 && onCancelAll && (
            <button
              onClick={onCancelAll}
              className="btn btn-danger btn-xs font-bold shadow-sm"
              aria-label="Cancel all working orders"
            >
              Cancel All ({orders.length})
            </button>
          )}
        </div>
      </div>

      {/* Orders Table */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {orders.length === 0 ? (
          <div className="empty-state py-12">
            <span className="empty-state-icon" aria-hidden="true">📋</span>
            <span className="empty-state-title">No working limit orders</span>
            <span className="empty-state-desc">
              Active market making &amp; arbitrage quoting loops will place limit orders in the matching engine.
            </span>
          </div>
        ) : (
          <table className="data-table text-xs w-full" role="table" aria-label="Working limit orders">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="min-w-[190px] text-left">Market Contract</th>
                <th scope="col" className="text-center">Side</th>
                <th scope="col" className="text-right">Price</th>
                <th scope="col" className="text-right">Shares (Filled)</th>
                <th scope="col" className="text-left">Strategy</th>
                <th scope="col" className="text-center">Age</th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {orders.map((o) => {
                const info = formatHierarchicalMarket(o.slug)
                const matched = o.size_matched ?? 0
                const fillPct = o.size > 0 ? Math.min(100, Math.round((matched / o.size) * 100)) : 0
                const isBuy = o.side === 'BUY'

                return (
                  <tr key={o.order_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2.5 max-w-[220px]">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-[9.5px] text-cyan-400 font-bold uppercase tracking-wider truncate">
                          {info.category.icon} {info.eventTitle}
                        </span>
                        <span className="text-[#dde1ed] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                          {info.question}
                        </span>
                      </div>
                    </td>

                    {/* Side */}
                    <td className="text-center">
                      <span
                        className={`badge text-[9.5px] font-black tracking-wider uppercase px-2 py-0.5 ${
                          isBuy ? 'badge-green bg-green-500/15 text-green-400 border-green-500/30' : 'badge-red bg-red-500/15 text-red-400 border-red-500/30'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>

                    {/* Price */}
                    <td className="mono text-right font-bold text-cyan-400">
                      {fmtPrice(o.price)}
                    </td>

                    {/* Fill Progress & Size */}
                    <td className="mono text-right font-medium text-[#dde1ed]">
                      <div>
                        <span>{o.size.toFixed(1)}</span>
                        {matched > 0 && (
                          <span className="text-[10px] text-green-400 ml-1">({matched.toFixed(1)})</span>
                        )}
                      </div>
                      {matched > 0 && (
                        <div className="w-full bg-[#1f2335] h-1 rounded-full overflow-hidden mt-1">
                          <div className="bg-green-400 h-full rounded-full" style={{ width: `${fillPct}%` }} />
                        </div>
                      )}
                    </td>

                    {/* Strategy Tag */}
                    <td>
                      <span className="text-[9.5px] text-[#7e8aaa] mono bg-[#0e1015] px-1.5 py-0.5 rounded border border-[#1f2335] font-semibold">
                        {o.strategy}
                      </span>
                    </td>

                    {/* Age */}
                    <td className="mono text-[#7e8aaa] text-[10.5px] text-center">{fmtAge(o.created_at)}</td>

                    {/* Action */}
                    <td className="text-right">
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="btn btn-danger btn-xs font-bold shadow-sm hover:shadow-red-500/20"
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

// W9-6 — React.memo with shallow compare is sufficient because all three
// props (orders array, onCancel, onCancelAll) are reference-compared.
// `onCancel` and `onCancelAll` MUST be stable in the parent for memo to
// skip renders — see useBot.ts (cancelOrder) and page.tsx (handleCancelAll).
export default memo(OrdersPanel)