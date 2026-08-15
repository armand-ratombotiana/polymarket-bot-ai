// components/OrdersPanel.tsx
'use client'

import { Order } from '@/hooks/useBot'

interface Props {
  orders: Order[]
  onCancel: (id: string) => void
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  return `${Math.floor(s / 60)}m`
}

export default function OrdersPanel({ orders, onCancel }: Props) {
  return (
    <div className="card flex flex-col min-h-0">
      <div className="card-header">
        <span className="card-title">📋 Open Orders</span>
        <span className="text-[11px] text-[#4a5068]">{orders.length} open</span>
      </div>
      <div className="overflow-auto scrollbar-thin flex-1">
        {orders.length === 0 ? (
          <div className="flex items-center justify-center h-20 text-[#4a5068] text-xs">
            No open orders
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Market</th>
                <th>Side</th>
                <th>Price</th>
                <th>Size</th>
                <th>Filled</th>
                <th>Strategy</th>
                <th>Age</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {orders.map(o => {
                const fillPct = o.size > 0 ? (o.size_matched / o.size) * 100 : 0
                return (
                  <tr key={o.order_id}>
                    <td className="mono text-[#4a5068] text-[11px]">{o.order_id.slice(-10)}</td>
                    <td>
                      <span className="text-[#e8eaf0] truncate block max-w-[120px]" title={o.slug}>{o.slug}</span>
                    </td>
                    <td>
                      <span className={`badge ${o.side === 'BUY' ? 'badge-green' : 'badge-red'}`}>
                        {o.side}
                      </span>
                    </td>
                    <td className="mono text-[#e8eaf0]">{o.price.toFixed(4)}</td>
                    <td className="mono text-[#8b91a8]">{o.size.toFixed(2)}</td>
                    <td>
                      <div className="flex items-center gap-1.5">
                        <div className="w-12 h-1 bg-[#252836] rounded-full overflow-hidden">
                          <div className="h-full bg-blue-500 rounded-full" style={{ width: `${fillPct}%` }} />
                        </div>
                        <span className="mono text-[#8b91a8] text-[11px]">{fillPct.toFixed(0)}%</span>
                      </div>
                    </td>
                    <td>
                      <span className="badge badge-dim">{o.strategy}{o.paper ? ' (P)' : ''}</span>
                    </td>
                    <td className="text-[#4a5068]">{age(o.created_at)}</td>
                    <td>
                      <button
                        onClick={() => onCancel(o.order_id)}
                        className="text-[11px] text-[#4a5068] hover:text-red-400 transition-colors px-1"
                        title="Cancel order"
                      >
                        ✕
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
