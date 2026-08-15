// components/OrdersPanel.tsx — Pro Open Orders Desk
'use client'

import { Order } from '@/hooks/useBot'

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
    <div className="card flex flex-col h-full min-h-0 bg-[#111318] border border-[#252836]">
      <div className="card-header flex justify-between items-center px-3 py-2 border-b border-[#252836]">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">📋 Active Open Orders</span>
          <span className="badge badge-blue text-[10px] mono font-semibold">{orders.length}</span>
        </div>
      </div>

      <div className="overflow-auto scrollbar-thin flex-1">
        {orders.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-28 text-[#4a5068] text-xs gap-1">
            <span>No active open orders.</span>
            <span className="text-[10px] text-[#3b4054]">Active market maker or signal orders will appear here.</span>
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Side</th>
                <th>Market</th>
                <th>Price</th>
                <th>Size / Filled</th>
                <th>Strategy</th>
                <th>Age</th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => {
                const fillPct = o.size > 0 ? Math.round((o.size_matched / o.size) * 100) : 0
                return (
                  <tr key={o.order_id} className="hover:bg-blue-500/5 transition-colors">
                    <td>
                      <span
                        className={`text-[10px] font-bold px-1.5 py-0.5 rounded ${
                          o.side === 'BUY'
                            ? 'bg-green-500/20 text-green-400 border border-green-500/40'
                            : 'bg-red-500/20 text-red-400 border border-red-500/40'
                        }`}
                      >
                        {o.side}
                      </span>
                    </td>
                    <td className="max-w-[130px]">
                      <span className="text-[#e8eaf0] font-medium truncate block" title={o.slug}>
                        {o.slug || o.token_id.slice(0, 12)}
                      </span>
                    </td>
                    <td className="mono font-semibold text-[#e8eaf0]">{o.price.toFixed(4)}</td>
                    <td>
                      <div className="flex flex-col gap-0.5">
                        <span className="mono text-[11px] text-[#8b91a8]">
                          {o.size_matched.toFixed(0)} / {o.size.toFixed(0)} sh
                        </span>
                        {fillPct > 0 && (
                          <div className="w-16 h-1 bg-[#252836] rounded-full overflow-hidden">
                            <div
                              className="h-full bg-cyan-400 rounded-full"
                              style={{ width: `${fillPct}%` }}
                            />
                          </div>
                        )}
                      </div>
                    </td>
                    <td>
                      <span className="text-[10px] text-[#8b91a8] bg-[#161822] px-1.5 py-0.5 rounded border border-[#252836] mono">
                        {o.strategy || 'manual'}
                      </span>
                    </td>
                    <td className="text-[#4a5068] mono text-[10px]">{age(o.created_at)}</td>
                    <td className="text-right">
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="text-[10px] text-amber-400 hover:text-white bg-amber-500/10 hover:bg-amber-500/30 px-2 py-0.5 rounded border border-amber-500/20"
                        title="Cancel this order"
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
