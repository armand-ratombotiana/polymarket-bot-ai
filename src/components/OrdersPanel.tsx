// components/OrdersPanel.tsx — Live Working Orders & Execution Queue Panel
//
// W49-5 — Operational-clarity redesign of the working-orders table.
//   Builds on the W39-5 status-badge + fill-progress redesign and
//   applies the W49-5 spec:
//
//   • Header KPI strip — surfaces Open count + Capital exposed as a
//     visually distinct right-aligned cluster (same shape as the
//     Positions panel's Exposure/Realized/Daily strip).
//
//   • Ghost Cancel button — replaced the previous `btn-danger` filled
//     red Cancel button with a ghost-styled button (transparent bg,
//     thin border, red text on hover). The spec calls for "Ghost
//     button with confirmation" because cancellation is reversible
//     (just re-quote); the previous filled-red style signalled
//     "irreversible destructive" which is misleading.
//
//   • Cancel All — promoted to a more prominent red-tinted button
//     ("CANCEL ALL (N)") and now supports a double-confirmation
//     flow when `requireConfirmation=true`:
//       1. First dialog — "Cancel all N working orders?" warning +
//          impact summary (capital exposed + average fill rate).
//       2. Second dialog — "Are you absolutely sure?" explicit
//          re-confirmation + the standard "This action cannot be
//          undone" risk warning.
//     Default behaviour (`requireConfirmation=false`) calls
//     `onCancelAll` directly on click — preserves the existing
//     test contract (`expect(onCancelAll).toHaveBeenCalledTimes(1)`
//     after a single click).
//
//   • Per-order Cancel — same `requireConfirmation` flow as before
//     (W39-5): when true, the click opens an inline
//     ConfirmationDialog; when false, calls onCancel directly.
//     The button styling is updated to ghost per the spec.
//
//   • Status badges — preserved unchanged from W39-5:
//       PENDING=amber, OPEN=blue, FILLED=green, CANCELLED=gray,
//       REJECTED=red.
//
//   • Fill progress bar — preserved unchanged.
//
//   • Age column — preserved ("3m ago" relative format with absolute
//     ISO timestamp via title attribute).
//
// W15-5 (unchanged transport) — the panel still subscribes to the
// `orders` WS channel and falls back to polling /api/orders every 5s
// when the WS isn't connected. "● Live" / "⟳ Polling" badge reflects
// the actual transport state.
//
// Backwards-compat: callers MAY still pass `orders` as a prop.
'use client'

import { useMemo, useState, useCallback, memo } from 'react'
import { Order } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtUsd, fmtTimeAbs } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { useStaleAge } from '@/hooks/useStaleAge'
import { Badge } from '@/components/ui/badge'
import { ErrorState, StaleIndicator } from '@/components/ui/states'
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
   * W39-5/W49-5 — when true, clicking a per-order Cancel button OR the
   * Cancel All button opens an inline ConfirmationDialog before
   * invoking the handler. Cancel All uses a double-confirmation flow
   * (two sequential dialogs). Defaults to `false` so existing tests
   * (which assert onCancel / onCancelAll is called directly on click)
   * keep their behaviour. page.tsx opts in to confirmation for
   * production safety.
   */
  requireConfirmation?: boolean
}

// W39-5/W49-5 — status badge visual map. PENDING/OPEN share the
// working-state palette but PENDING tints amber (awaiting match-engine
// acceptance) while OPEN tints blue (resting on the book, awaiting
// fill).
const STATUS_BADGE: Record<DisplayStatus, { label: string; cls: string }> = {
  PENDING:   { label: 'PENDING',   cls: 'bg-amber-500/15 text-amber-300 border-amber-500/30' },
  OPEN:      { label: 'OPEN',      cls: 'bg-blue-500/15 text-blue-300 border-blue-500/30' },
  FILLED:    { label: 'FILLED',    cls: 'bg-green-500/15 text-green-400 border-green-500/30' },
  CANCELLED: { label: 'CANCELLED', cls: 'bg-gray-500/15 text-gray-400 border-gray-500/30' },
  REJECTED:  { label: 'REJECTED',  cls: 'bg-red-500/15 text-red-400 border-red-500/30' },
}

// W39-5/W49-5 — derive a display status when the snapshot doesn't
// expose `order.status`. We can only distinguish FILLED /
// partial-OPEN / OPEN from size_matched — PENDING / REJECTED /
// CANCELLED require backend signalling and fall back to OPEN.
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
    error,
    lastUpdated,
    refetch,
  } = useRealtimeData<OrdersApiResponse>('/api/orders', {
    wsChannel: 'orders',
    pollInterval: 5000,
  })

  const orders = ordersOverride ?? fetched?.orders ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  // W41-3 — compute the data's age so we can surface a StaleIndicator
  // in the header when the snapshot is older than 30s. Skipped when
  // the caller provides an orders override.
  const age = useStaleAge(ordersOverride == null ? lastUpdated : null)

  // W39-5/W49-5 — token id of the order the trader is currently
  // confirming a Cancel on. When non-null, the inline
  // ConfirmationDialog renders.
  const [confirmCancelOrderId, setConfirmCancelOrderId] = useState<string | null>(null)

  // W49-5 — Cancel All double-confirmation state machine.
  //   0 = no dialog open
  //   1 = first dialog ("Cancel all N orders?")
  //   2 = second dialog ("Are you absolutely sure?")
  // Default `requireConfirmation=false` keeps the state at 0 and
  // calls onCancelAll directly on click — preserves the existing
  // test contract (`expect(onCancelAll).toHaveBeenCalledTimes(1)`).
  const [cancelAllStep, setCancelAllStep] = useState<0 | 1 | 2>(0)

  const totalOpenExposure = useMemo(() => {
    return orders.reduce((acc, o) => acc + o.price * (o.size - (o.size_matched ?? 0)), 0)
  }, [orders])

  // W49-5 — KPI strip aggregates: open-count (non-terminal orders) +
  // total open capital + average fill rate across the visible set.
  // Open count is shown when at least one order is non-terminal; fill
  // rate degrades gracefully when all orders have size_matched=0.
  const openCount = useMemo(
    () => orders.filter((o) => {
      const status = deriveDisplayStatus(o)
      return status === 'PENDING' || status === 'OPEN'
    }).length,
    [orders],
  )
  const totalSize = useMemo(() => orders.reduce((acc, o) => acc + o.size, 0), [orders])
  const totalMatched = useMemo(
    () => orders.reduce((acc, o) => acc + (o.size_matched ?? 0), 0),
    [orders],
  )
  const avgFillPct = totalSize > 0 ? Math.round((totalMatched / totalSize) * 100) : 0

  // W39-5/W49-5 — the order currently pending Cancel confirmation.
  // Looked up by order_id so the dialog can render an order-specific
  // impact summary.
  const confirmingOrder = useMemo(
    () => (confirmCancelOrderId ? orders.find((o) => o.order_id === confirmCancelOrderId) ?? null : null),
    [confirmCancelOrderId, orders],
  )

  // W39-5/W49-5 — per-order Cancel handler. When
  // `requireConfirmation` is true, the click opens the inline
  // ConfirmationDialog (which then calls onCancel on confirm). When
  // false, the click calls onCancel directly — preserves the legacy
  // direct-call behaviour that the existing tests assert.
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

  // W49-5 — Cancel All click handler. When `requireConfirmation` is
  // true, kicks off the double-confirmation flow (step 1 → step 2 →
  // onCancelAll). When false, calls onCancelAll directly — preserves
  // the existing test contract.
  const handleCancelAllClick = useCallback(() => {
    if (requireConfirmation) {
      setCancelAllStep(1)
    } else {
      onCancelAll?.()
    }
  }, [requireConfirmation, onCancelAll])

  // W49-5 — step 1 → step 2 (the user confirmed the first warning;
  // now show the explicit re-confirmation dialog).
  const handleConfirmCancelAllStep1 = useCallback(() => {
    setCancelAllStep(2)
  }, [])

  // W49-5 — step 2 → onCancelAll (the user explicitly re-confirmed;
  // now actually invoke the batch cancel).
  const handleConfirmCancelAllStep2 = useCallback(() => {
    onCancelAll?.()
    setCancelAllStep(0)
  }, [onCancelAll])

  // W49-5 — escape hatches for either step's Cancel button.
  const handleCancelCancelAll = useCallback(() => {
    setCancelAllStep(0)
  }, [])

  // W39-5/W49-5 — pre-compute the impact summary string for the
  // per-order dialog so the trader sees exactly what cancelling will
  // do before confirming.
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

  // W49-5 — Cancel All impact summary. Surfaced in the first dialog
  // so the trader sees the aggregate blast radius before confirming.
  const cancelAllImpact = useMemo(() => {
    if (orders.length === 0) return ''
    return [
      `Orders: ${orders.length}`,
      `Open capital: ${fmtUsd(totalOpenExposure)}`,
      `Avg fill rate: ${avgFillPct}%`,
    ].join(' · ')
  }, [orders.length, totalOpenExposure, avgFillPct])

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-xl overflow-hidden">
      {/* Header — title + KPI strip + Cancel All */}
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
          {/* W41-3 — StaleIndicator renders as an inline amber/red pill
              when the fetched snapshot is older than 30s. Hidden while
              fresh (<30s) so the header doesn't accumulate noise. Skipped
              when the caller provides an orders override. */}
          {age !== null && <StaleIndicator age={age} />}
        </div>

        {/* W49-5 — KPI strip. Two cards (Open count, Capital exposed)
            clustered on the right side of the header. Each card has
            the same shape as the Positions panel's KPI strip: tiny
            uppercase label + bold color-coded value. Hidden when no
            orders exist (avoids showing "Open: 0 / Capital: $0.00"
            in the empty state — the empty-state placeholder already
            communicates "nothing here"). */}
        {orders.length > 0 && (
          <div className="flex items-center gap-2 text-xs">
            <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5" title="Non-terminal working orders (PENDING + OPEN)">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Open:</span>
              <span className="mono font-bold text-blue-300 text-xs">{openCount}</span>
              <span className="text-[9.5px] text-[#5a637a]">/ {orders.length}</span>
            </div>

            <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5" title="Total capital exposed across all working orders">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Capital:</span>
              <span className="mono font-bold text-cyan-400 text-xs">{fmtUsd(totalOpenExposure)}</span>
            </div>
          </div>
        )}

        <div className="flex items-center gap-2">
          {/* W49-5 — Cancel All promoted to a prominent button. The
              double-confirmation flow (when `requireConfirmation=true`)
              is handled by `handleCancelAllClick` + the two
              ConfirmationDialogs rendered at the bottom of the panel. */}
          {orders.length > 0 && onCancelAll && (
            <button
              onClick={handleCancelAllClick}
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
      ) : error && ordersOverride == null && orders.length === 0 ? (
        // W41-3 — Error state. Rendered only when the initial REST fetch
        // failed AND no override was supplied. Includes a Retry button
        // that calls the hook's refetch().
        <ErrorState
          message="Working orders unavailable"
          detail={error}
          onRetry={refetch}
          retryLabel="Retry"
        />
      ) : (
        <div className="overflow-auto scrollbar-thin flex-1 table-container">
          {orders.length === 0 ? (
            // W49-5 — polished empty state (larger icon, more padding).
            <div className="empty-state py-12">
              <span className="empty-state-icon text-4xl" aria-hidden="true">📋</span>
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
                  // W39-5/W49-5 — derive the display status (prefers
                  // backend `o.status` when available; falls back to
                  // size-based heuristic otherwise).
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

                      {/* W39-5/W49-5 — Status badge column. */}
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

                      {/* Fill Progress & Size — W39-5/W49-5: the
                          progress bar is rendered for any OPEN/partial
                          order (matched > 0 AND matched < size). */}
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

                      {/* W39-5/W49-5 — Age in relative format ("3m ago").
                          The title attribute carries the absolute ISO
                          timestamp for hover + screen-reader context. */}
                      <td className="mono text-[#7e8aaa] text-[10.5px] text-center" title={`Created: ${fmtTimeAbs(o.created_at)}`}>
                        {fmtAge(o.created_at)}
                      </td>

                      {/* Action — W49-5: Cancel is hidden for terminal
                          states (FILLED / CANCELLED / REJECTED) where
                          cancellation is a no-op. For non-terminal
                          states the button renders in the spec's
                          "ghost" style — transparent bg, thin border,
                          red text on hover — signalling that the
                          action is reversible (just re-quote) rather
                          than irreversibly destructive. */}
                      <td className="text-right">
                        {isTerminal ? (
                          <span className="text-[10px] text-[#3e4560] uppercase tracking-wider font-semibold" aria-label={`Order ${displayStatus.toLowerCase()} — no cancel action`}>
                            {displayStatus === 'FILLED' ? '✓ Filled' : displayStatus === 'CANCELLED' ? '— Cancelled' : '✕ Rejected'}
                          </span>
                        ) : (
                          <button
                            onClick={() => handleCancelClick(o.order_id)}
                            className="btn btn-ghost btn-xs font-bold border border-[#1f2335] text-[#7e8aaa] hover:text-red-300 hover:border-red-500/50 hover:bg-red-500/5 transition-colors"
                            aria-label={`Cancel order ${o.order_id}`}
                            title="Cancel this order"
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

      {/* W39-5/W49-5 — per-order Cancel confirmation dialog. Rendered
          inline so the panel can drive its own impact summary from
          the live order snapshot without threading every order through
          the parent. */}
      <ConfirmationDialog
        open={confirmCancelOrderId !== null && confirmingOrder !== null}
        severity="warning"
        title="Cancel Order?"
        description={confirmDescription}
        impact={confirmImpact}
        riskWarning="This action cannot be undone. Cancelling a partial-fill order forfeits the resting portion of your book priority — on thin markets, re-entering at the same price may require waiting for the next quote refresh."
        confirmLabel="✕ Cancel Order"
        cancelLabel="Keep Order"
        onConfirm={handleConfirmCancel}
        onCancel={handleCancelDialogClose}
      />

      {/* W49-5 — Cancel All double-confirmation flow. Two sequential
          dialogs:
            1. Warning + impact summary (N orders + capital exposed +
               avg fill rate).
            2. Explicit re-confirmation with the "cannot be undone"
               risk warning. */}
      <ConfirmationDialog
        open={cancelAllStep === 1}
        severity="warning"
        title={`Cancel all ${orders.length} working orders?`}
        description="This sends a batch cancel to the matching engine for every working order in your book. Partially-filled orders will keep their fills; only the resting (unmatched) portion is cancelled."
        impact={cancelAllImpact}
        riskWarning="This action cannot be undone. Re-quoting the same book may require waiting for the next strategy refresh — on volatile markets the mid may have moved by then."
        confirmLabel="Continue"
        cancelLabel="Keep Orders"
        onConfirm={handleConfirmCancelAllStep1}
        onCancel={handleCancelCancelAll}
      />
      <ConfirmationDialog
        open={cancelAllStep === 2}
        severity="danger"
        title="Are you absolutely sure?"
        description="This is the final confirmation. Clicking 'Cancel All' will immediately submit batch-cancellation for every resting order in your book. The fills already on the tape remain — only the resting quotes are removed."
        impact={cancelAllImpact}
        riskWarning="This action cannot be undone. After cancellation, your strategies will resume quoting on the next tick (typically 1–5 seconds). During that gap you have zero market presence."
        confirmLabel="✕ Cancel All Orders"
        cancelLabel="Back"
        onConfirm={handleConfirmCancelAllStep2}
        onCancel={handleCancelCancelAll}
      />
    </div>
  )
}

// W9-6 — React.memo with shallow compare is sufficient because all props
// are reference-compared. `onCancel` / `onCancelAll` MUST be stable in the
// parent for memo to skip renders.
//
// W39-5/W49-5 — `requireConfirmation` is a primitive boolean, diffed
// inline so the parent flipping the preference re-renders the panel.
export default memo(OrdersPanel)
