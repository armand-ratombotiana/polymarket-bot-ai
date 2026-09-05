// components/OrdersPanel.tsx — Live Working Orders & Execution Queue Panel
//
// W15-5 — Migrated from `useBot`'s 2-second REST polling to the hybrid
// `useRealtimeData` hook. Subscribes to the `orders` WS channel; falls
// back to polling /api/orders every 5s when the WS isn't connected.
// Renders a "● Live" / "⟳ Polling" badge so the trader can tell at a
// glance whether the working-orders list is real-time or lagged.
//
// W39-5 — Redesigned for clearer execution-state signalling:
//   • Per-order status badge (PENDING=amber, OPEN=blue, FILLED=green,
//     CANCELLED=gray, REJECTED=red). When the snapshot doesn't expose
//     `order.status`, the panel derives a display status from
//     `size_matched` / `size` (matched === size → FILLED; matched > 0 →
//     OPEN/partial; matched === 0 → OPEN). PENDING/REJECTED/CANCELLED
//     cannot be inferred from size alone and fall back to OPEN.
//   • Fill % progress bar shown for every OPEN/partial order (matched > 0
//     AND matched < size), with the numeric % label adjacent to the bar.
//   • "Cancel" button restyled with a confirmation step. When
//     `requireConfirmation` is true (page.tsx opts in for production),
//     clicking Cancel opens an inline ConfirmationDialog with the order's
//     impact summary ("Side: BUY, Price: $0.42, Size: 25 (5 filled)") +
//     risk warning before invoking onCancel. When false (default), the
//     click calls onCancel directly — preserves the test contract.
//   • "Cancel All" still routed through the parent's onCancelAll callback
//     (page.tsx shows the existing batch ConfirmationDialog for that).
//   • Creation time rendered in relative format ("3m ago") with the
//     absolute ISO timestamp surfaced via the title attribute for hover
//     + screen-reader context.
//
// Backwards-compat: callers MAY still pass `orders` as a prop (page.tsx
// still threads useBot's snapshot through; existing tests pass it too).
'use client'

import { useMemo, useState, useCallback, memo } from 'react'
import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtUsd, fmtTimeAbs } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'
import ConfirmationDialog from './ConfirmationDialog'

interface OrdersApiResponse {
  orders: Order[]
}

type DisplayStatus = 'PENDING' | 'OPEN' | 'FILLED' | 'CANCELLED' | 'REJECTED'

interface Props {
  orders?: Order[]
  onCancel: (orderId: string) => void
  onCancelAll?: () => void
  isRealtime?: boolean
  /**
   * W39-5 — when true, clicking a per-order Cancel button opens an
   * inline ConfirmationDialog before invoking onCancel. Defaults to
   * `false` so existing tests (which assert onCancel is called directly
   * on click) keep their behaviour. page.tsx opts in to confirmation
   * for production safety.
   */
  requireConfirmation?: boolean
}

// W39-5 — status badge visual map. PENDING/OPEN share the working-state
// palette but PENDING tints amber (awaiting match-engine acceptance)
// while OPEN tints blue (resting on the book, awaiting fill).
const STATUS_BADGE: Record<DisplayStatus, { label: string; cls: string }> = {
  PENDING:   { label: 'PENDING',   cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  OPEN:      { label: 'OPEN',      cls: 'bg-blue-500/15 text-blue-300 border-blue-500/30' },
  FILLED:    { label: 'FILLED',    cls: 'bg-green-500/15 text-green-400 border-green-500/30' },
  CANCELLED: { label: 'CANCELLED', cls: 'bg-gray-500/15 text-gray-400 border-gray-500/30' },
  REJECTED:  { label: 'REJECTED',  cls: 'bg-red-500/15 text-red-400 border-red-500/30' },
}

// W39-5 — derive a display status when the snapshot doesn't expose
// `order.status`. We can only distinguish FILLED / partial-OPEN / OPEN
// from size_matched — PENDING / REJECTED / CANCELLED require backend
// signalling and fall back to OPEN.
function deriveDisplayStatus(o: Order): DisplayStatus {
  if (o.status) return o.status
  const matched = o.size_matched ?? 0
  if (matched >= o.size && o.size > 0) return 'FILLED'
  return 'OPEN'
}

function OrdersPanel({
  orders: ordersOverride,
  onCancel,
  onCancelAll,
  isRealtime: isRealtimeOverride,
  requireConfirmation = false,
}: Props) {
  const {
    data: fetched,
    isLoading,
    isRealtime: wsIsRealtime,
  } = useRealtimeData<OrdersApiResponse>('/api/orders', {
    wsChannel: 'orders',
    pollInterval: 5000,
  })

  const orders = ordersOverride ?? fetched?.orders ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  // W39-5 — token id of the order the trader is currently confirming a
  // Cancel on. When non-null, the inline ConfirmationDialog renders.
  const [confirmCancelOrderId, setConfirmCancelOrderId] = useState<string | null>(null)

  const totalOpenExposure = useMemo(() => {
    return orders.reduce((acc, o) => acc + o.price * (o.size - (o.size_matched ?? 0)), 0)
  }, [orders])

  // W39-5 — the order currently pending Cancel confirmation. Looked up
  // by order_id so the dialog can render an order-specific impact summary.
  const confirmingOrder = useMemo(
    () => (confirmCancelOrderId ? orders.find((o) => o.order_id === confirmCancelOrderId) ?? null : null),
    [confirmCancelOrderId, orders],
  )

  // W39-5 — Cancel handler. When `requireConfirmation` is true, the click
  // opens the inline ConfirmationDialog (which then calls onCancel on
  // confirm). When false, the click calls onCancel directly — preserves
  // the legacy direct-call behaviour that the existing tests assert.
  const handleCancelClick = useCallback(
    (orderId: string) => {
      if (requireConfirmation) {
        setConfirmCancelOrderId(orderId)
      } else {
        onCancel(orderId)
      }
    },
    [requireConfirmation, onCancel],
  )

  const handleConfirmCancel = useCallback(() => {
    if (confirmCancelOrderId) {
      onCancel(confirmCancelOrderId)
    }
    setConfirmCancelOrderId(null)
  }, [confirmCancelOrderId, onCancel])

  const handleCancelDialogClose = useCallback(() => {
    setConfirmCancelOrderId(null)
  }, [])

  // W39-5 — pre-compute the impact summary string for the dialog so the
  // trader sees exactly what cancelling will do before confirming.
  const confirmImpact = useMemo(() => {
    if (!confirmingOrder) return ''
    const matched = confirmingOrder.size_matched ?? 0
    const remaining = confirmingOrder.size - matched
    const remainingValue = confirmingOrder.price * remaining
    return [
      `Side: ${confirmingOrder.side}`,
      `Price: ${fmtPrice(confirmingOrder.price)}`,
      `Size: ${confirmingOrder.size.toFixed(1)}`,
      matched > 0 ? `(${matched.toFixed(1)} filled, ${remaining.toFixed(1)} resting)` : '(0 filled)',
      `Open capital: ${fmtUsd(remainingValue)}`,
    ].join(' · ')
  }, [confirmingOrder])

  const confirmDescription = useMemo(() => {
    if (!confirmingOrder) return ''
    const info = formatHierarchicalMarket(confirmingOrder.slug)
    return `Cancel the ${confirmingOrder.side} order on ${info.fullLabel}? This sends a cancel to the matching engine — the order will stop resting on the book immediately.`
  }, [confirmingOrder])

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-xl overflow-hidden">
      {/* Header */}
      <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center justify-between bg-[#0e1015]/80">
        <div className="flex items-center gap-2.5">
          <span className="card-title text-xs font-bold text-[#dde1ed] flex items-center gap-1.5">
            📋 Working Orders ({orders.length})
          </span>
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
                  <th scope="col" className="text-center">Status</th>
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
                  // W39-5 — derive the display status (prefers backend
                  // `o.status` when available; falls back to size-based
                  // heuristic otherwise). Used both for the status badge
                  // and for deciding whether to render the fill % bar.
                  const displayStatus = deriveDisplayStatus(o)
                  const isFilled = displayStatus === 'FILLED'
                  const isCancelled = displayStatus === 'CANCELLED'
                  const isRejected = displayStatus === 'REJECTED'
                  const isTerminal = isFilled || isCancelled || isRejected
                  const showFillBar = !isTerminal && matched > 0

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

                      {/* W39-5 — Status badge column. The badge reflects
                          the order's lifecycle state (PENDING/OPEN/
                          FILLED/CANCELLED/REJECTED), tinted per the
                          STATUS_BADGE map above. */}
                      <td className="text-center">
                        <span
                          className={`inline-block px-2 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border ${
                            STATUS_BADGE[displayStatus].cls
                          }`}
                          title={`Status: ${displayStatus}`}
                        >
                          {STATUS_BADGE[displayStatus].label}
                        </span>
                      </td>

                      {/* Price */}
                      <td className="mono text-right font-bold text-cyan-400">
                        {fmtPrice(o.price)}
                      </td>

                      {/* Fill Progress & Size — W39-5: the progress bar is
                          rendered for any OPEN/partial order (matched > 0
                          AND matched < size). The numeric % label sits
                          adjacent to the bar so the trader can read it at
                          a glance without hovering. */}
                      <td className="mono text-right font-medium text-[#dde1ed]">
                        <div>
                          <span>{o.size.toFixed(1)}</span>
                          {matched > 0 && (
                            <span className="text-[10px] text-green-400 ml-1">({matched.toFixed(1)})</span>
                          )}
                          {showFillBar && (
                            <span className="text-[9.5px] text-[#7e8aaa] ml-1">{fillPct}%</span>
                          )}
                        </div>
                        {showFillBar && (
                          <div className="w-full bg-[#1f2335] h-1 rounded-full overflow-hidden mt-1" role="progressbar" aria-valuenow={fillPct} aria-valuemin={0} aria-valuemax={100} aria-label={`Fill progress: ${fillPct}%`}>
                            <div className="bg-green-400 h-full rounded-full transition-all" style={{ width: `${fillPct}%` }} />
                          </div>
                        )}
                      </td>

                      {/* Strategy Tag */}
                      <td>
                        <span className="text-[9.5px] text-[#7e8aaa] mono bg-[#0e1015] px-1.5 py-0.5 rounded border border-[#1f2335] font-semibold">
                          {o.strategy}
                        </span>
                      </td>

                      {/* W39-5 — Age in relative format ("3m ago"). The
                          title attribute carries the absolute ISO
                          timestamp for hover + screen-reader context. */}
                      <td className="mono text-[#7e8aaa] text-[10.5px] text-center" title={`Created: ${fmtTimeAbs(o.created_at)}`}>
                        {fmtAge(o.created_at)}
                      </td>

                      {/* Action — W39-5: Cancel is hidden for terminal
                          states (FILLED / CANCELLED / REJECTED) where
                          cancellation is a no-op. For non-terminal states
                          the button renders with destructive styling. */}
                      <td className="text-right">
                        {isTerminal ? (
                          <span className="text-[10px] text-[#3e4560] uppercase tracking-wider font-semibold" aria-label={`Order ${displayStatus.toLowerCase()} — no cancel action`}>
                            {displayStatus === 'FILLED' ? '✓ Filled' : displayStatus === 'CANCELLED' ? '— Cancelled' : '✕ Rejected'}
                          </span>
                        ) : (
                          <button
                            onClick={() => handleCancelClick(o.order_id)}
                            className="btn btn-danger btn-xs font-bold shadow-sm hover:shadow-red-500/20"
                            aria-label={`Cancel order ${o.order_id}`}
                          >
                            Cancel
                          </button>
                        )}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      {/* W39-5 — per-order Cancel confirmation dialog. Rendered inline so
          the panel can drive its own impact summary from the live order
          snapshot without threading every order through the parent. */}
      <ConfirmationDialog
        open={confirmCancelOrderId !== null && confirmingOrder !== null}
        severity="warning"
        title="Cancel Order?"
        description={confirmDescription}
        impact={confirmImpact}
        riskWarning="Cancelling a partial-fill order forfeits the resting portion of your book priority. On thin markets, re-entering at the same price may require waiting for the next quote refresh."
        confirmLabel="✕ Cancel Order"
        cancelLabel="Keep Order"
        onConfirm={handleConfirmCancel}
        onCancel={handleCancelDialogClose}
      />
    </div>
  )
}

// W9-6 — React.memo with shallow compare is sufficient because all props
// are reference-compared. `onCancel` / `onCancelAll` MUST be stable in the
// parent for memo to skip renders.
//
// W39-5 — `requireConfirmation` is a primitive boolean, diffed inline so
// the parent flipping the preference re-renders the panel.
export default memo(OrdersPanel)
