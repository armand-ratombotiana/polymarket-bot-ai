// components/OrdersPanel.tsx — Live Working Orders & Execution Queue Panel
//
// W15-5 — Migrated from `useBot`'s 2-second REST polling to the hybrid
// `useRealtimeData` hook. Subscribes to the `orders` WS channel; falls
// back to polling /api/orders every 5s when the WS isn't connected.
// Renders a "● Live" / "⟳ Polling" badge so the trader can tell at a
// glance whether the working-orders list is real-time or lagged.
//
// Backwards-compat: callers MAY still pass `orders` as a prop (page.tsx
// still threads useBot's snapshot through; existing tests pass it too).
// When provided, the prop overrides the fetched data — the WS
// subscription still runs so `isRealtime` stays accurate.
'use client'

import { useMemo, memo } from 'react'
import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtUsd } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'

interface OrdersApiResponse {
  orders: Order[]
}

interface Props {
  /**
   * Optional override for the orders list. When provided, the panel
   * uses this directly and skips rendering the useRealtimeData result
   * (the WS subscription still runs so the Live/Polling badge reflects
   * the actual transport state). When omitted, the panel self-fetches
   * via useRealtimeData('/api/orders', { wsChannel: 'orders' }).
   */
  orders?: Order[]
  onCancel: (orderId: string) => void
  onCancelAll?: () => void
  /**
   * Optional override for the realtime indicator. When omitted, the
   * panel derives the badge state from useRealtimeData's `isRealtime`.
   */
  isRealtime?: boolean
}

// W9-6 — wrapped in React.memo. Props: `orders` (new array on every
// snapshot — memo won't skip many renders by itself), `onCancel` (must
// be stable in parent — useCallback in useBot), `onCancelAll` (must be
// stable in parent — useCallback in page.tsx). When all three are stable
// AND the orders array reference is unchanged, the table skips re-render.
function OrdersPanel({ orders: ordersOverride, onCancel, onCancelAll, isRealtime: isRealtimeOverride }: Props) {
  // W15-5 — hybrid REST + WS subscription. Always invoked (Rules of
  // Hooks forbid conditional calls), even when the caller passes an
  // `orders` override — the WS subscription still drives `isRealtime`
  // so the Live/Polling badge accurately reflects the transport state.
  const {
    data: fetched,
    isLoading,
    isRealtime: wsIsRealtime,
  } = useRealtimeData<OrdersApiResponse>('/api/orders', {
    wsChannel: 'orders',
    pollInterval: 5000, // was 2s under useBot's REST poll; relaxed to 5s
  })

  const orders = ordersOverride ?? fetched?.orders ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

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
          {/* W15-5 — Live / Polling badge. Reflects the underlying
              useRealtimeData transport state. */}
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
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

      {/* W15-5 — loading state. Only surfaces on the FIRST fetch AND when
          no `orders` override was passed. Once the initial REST fetch
          returns, the table renders even when the WS is still
          handshaking — the Live/Polling badge in the header conveys the
          transport lag instead of blanking the panel. */}
      {isLoading && orders.length === 0 ? (
        <div className="flex items-center justify-center py-12 text-xs text-[#7e8aaa]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading working orders…
        </div>
      ) : (
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
      )}
    </div>
  )
}

// W9-6 — React.memo with shallow compare is sufficient because all three
// props (orders array, onCancel, onCancelAll) are reference-compared.
// `onCancel` and `onCancelAll` MUST be stable in the parent for memo to
// skip renders — see useBot.ts (cancelOrder) and page.tsx (handleCancelAll).
//
// W15-5 — `isRealtime` is a primitive boolean, diffed inline. The
// useRealtimeData hook lives inside the component and triggers normal
// React re-renders on its own state changes, so memo on the prop surface
// doesn't interfere with WS-driven re-renders.
export default memo(OrdersPanel)
