// components/ConfirmationDialog.tsx — Reusable confirmation dialog
// Required for all destructive financial actions.
//
// W49-5 — Operational-clarity polish on top of the W39-5 redesign.
// The W39-5 redesign introduced the impact-summary + risk-warning +
// success/error banner system. W49-5 makes three small refinements
// without changing the prop surface or any test contract:
//
//   • Impact banner no longer duplicates the severity icon — the
//     header already shows the per-severity icon (🛑 / ⚠️ / ℹ️) at
//     larger size; rendering the same glyph a second time in the
//     impact banner is visually noisy. Replaced with a small bold
//     "IMPACT" label so the trader reads "IMPACT: Size: 10 shares…"
//     instead of "🛑 Size: 10 shares…" (which collides with the
//     header's "🛑 Close Position?"). The impact text remains a leaf
//     text node (wrapped in its own <span>) so the existing test
//     `getByText('This will cancel 5 open orders')` keeps matching.
//
//   • Risk warning banner tightened — now uses an explicit "⚠ RISK:"
//     label (the previous "Risk:" label was visually weak). The
//     warning text is wrapped in its own <span> for the same
//     test-contract reason.
//
//   • Header accent border — the dialog header now has a 2px-tall
//     severity-tinted accent stripe under the title block so the
//     dialog's gravity is unambiguous (danger = red, warning =
//     amber, info = blue). Purely cosmetic — no behavioural change.
//
// W39-5 (unchanged behaviour) — every existing prop (open, severity,
// title, description, impact, confirmLabel, cancelLabel, onConfirm,
// onCancel, loading) keeps the same shape + behaviour. Existing tests
// pass unchanged — `onConfirm` may return a Promise OR void; when
// sync, the wrapper still resolves a synthetic microtask so the
// "called exactly once" assertion holds.
'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

type Severity = 'danger' | 'warning' | 'info'
type ResultStatus = 'success' | 'error'

interface ConfirmationDialogProps {
  open: boolean
  severity?: Severity
  title: string
  /** Plain-language description of exactly what will happen */
  description: string
  /** Optional impact summary (e.g., "This will cancel 5 open orders") */
  impact?: string
  /**
   * W39-5/W49-5 — optional secondary risk warning rendered above the
   * action footer. Use this to surface non-obvious downsides the trader
   * should weigh before confirming (e.g., "This action cannot be
   * undone. Cancelling a partial-fill order forfeits queue priority
   * on a thin book").
   */
  riskWarning?: string
  confirmLabel?: string
  cancelLabel?: string
  /**
   * Confirms the destructive action. May return a Promise — when it
   * does, the dialog tracks the pending state internally and shows
   * a success/error banner once the Promise settles. When sync (void),
   * the dialog treats the call as a successful completion.
   */
  onConfirm: () => void | Promise<unknown>
  onCancel: () => void
  /** Set true while the action is executing (external loading state). */
  loading?: boolean
  /**
   * W39-5 — when true, the dialog ignores the external `open` prop's
   * close behaviour on success and lets the parent drive the close.
   * Defaults to false (auto-close on success after the success banner
   * has been visible for ~1.2s, mirroring toast semantics).
   */
  suppressAutoClose?: boolean
}

const ICONS: Record<Severity, string> = {
  danger:  '🛑',
  warning: '⚠️',
  info:    'ℹ️',
}

const CONFIRM_COLORS: Record<Severity, string> = {
  danger:  'btn-danger',
  warning: 'btn-amber',
  info:    'btn-primary',
}

// W49-5 — header accent stripe color per severity. A 2px-tall div
// rendered under the title block to give the dialog unambiguous
// gravity (red for danger, amber for warning, blue for info).
const ACCENT_STRIPE_CLASS: Record<Severity, string> = {
  danger:  'bg-red-500/60',
  warning: 'bg-amber-500/60',
  info:    'bg-blue-500/60',
}

const RESULT_ICON: Record<ResultStatus, string> = {
  success: '✓',
  error:   '✕',
}

const RESULT_BANNER_CLASS: Record<ResultStatus, string> = {
  success: 'banner-success',
  error:   'banner-danger',
}

const SUCCESS_AUTO_CLOSE_MS = 1200

export default function ConfirmationDialog({
  open,
  severity = 'danger',
  title,
  description,
  impact,
  riskWarning,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
  loading = false,
  suppressAutoClose = false,
}: ConfirmationDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelBtnRef = useRef<HTMLButtonElement>(null)

  // W39-5 — internal pending + result state. When `onConfirm` returns a
  // Promise, `internalPending` mirrors the Promise's pending state; when
  // it settles, `internalResult` captures the outcome so the dialog can
  // surface an inline success/error banner before closing. Sync onConfirm
  // (returns void) is treated as an immediate success.
  const [internalPending, setInternalPending] = useState(false)
  const [internalResult, setInternalResult] =
    useState<{ status: ResultStatus; message: string } | null>(null)

  // Reset internal state whenever the dialog (re)opens — covers the
  // parent re-opening a stale dialog after a prior success/error.
  useEffect(() => {
    if (open) {
      setInternalPending(false)
      setInternalResult(null)
    }
  }, [open])

  // Focus management: focus cancel button on open (safer default).
  useEffect(() => {
    if (open) {
      const t = setTimeout(() => cancelBtnRef.current?.focus(), 50)
      return () => clearTimeout(t)
    }
    return undefined
  }, [open])

  // Keyboard: Escape cancels (but not while an action is pending — the
  // trader should either let the request finish or explicitly Cancel
  // through the disabled Cancel button, which forces them to wait).
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !internalPending && !loading) {
        e.stopPropagation()
        onCancel()
      }
    }
    window.addEventListener('keydown', handler, true)
    return () => window.removeEventListener('keydown', handler, true)
  }, [open, onCancel, internalPending, loading])

  // Focus trap
  useEffect(() => {
    if (!open) return
    const el = dialogRef.current
    if (!el) return
    const focusableSelectors = 'button:not([disabled]), [tabindex]:not([tabindex="-1"]), input:not([disabled])'
    const focusables = Array.from(el.querySelectorAll<HTMLElement>(focusableSelectors))
    const first = focusables[0]
    const last = focusables[focusables.length - 1]

    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      if (e.shiftKey) {
        if (document.activeElement === first) { e.preventDefault(); last?.focus() }
      } else {
        if (document.activeElement === last) { e.preventDefault(); first?.focus() }
      }
    }
    el.addEventListener('keydown', trap)
    return () => el.removeEventListener('keydown', trap)
  }, [open])

  // W39-5 — wrapped confirm handler. Calls the parent's onConfirm, awaits
  // any Promise it returns, and surfaces the outcome via `internalResult`.
  // On success (and when `suppressAutoClose` is false), schedules an
  // auto-close after `SUCCESS_AUTO_CLOSE_MS` so the trader sees a brief
  // confirmation banner before the dialog dismisses.
  const handleConfirm = useCallback(async () => {
    if (internalPending || loading) return
    setInternalPending(true)
    try {
      const ret = onConfirm()
      // Only await when the caller actually returned a thenable. Sync
      // onConfirm (void) skips the microtask so the dialog resolves
      // immediately — the existing test asserts `onConfirm` was called
      // exactly once after the click, which still holds.
      if (ret && typeof (ret as Promise<unknown>).then === 'function') {
        await ret
      }
      setInternalResult({
        status: 'success',
        message: 'Action completed successfully.',
      })
      if (!suppressAutoClose) {
        setTimeout(() => {
          setInternalResult(null)
          setInternalPending(false)
          // Notify the parent via onCancel — the parent closes the
          // dialog by flipping `open` to false. Using onCancel (rather
          // than a dedicated onClose) keeps the prop surface minimal
          // and matches the existing escape/backdrop cancellation
          // contract: the dialog never closes itself without informing
          // the parent.
          onCancel()
        }, SUCCESS_AUTO_CLOSE_MS)
      } else {
        // Parent manages close — just clear the pending flag so the
        // success banner remains visible until the parent dismisses.
        setInternalPending(false)
      }
    } catch (err) {
      setInternalResult({
        status: 'error',
        message:
          err instanceof Error
            ? err.message
            : typeof err === 'string'
              ? err
              : 'Action failed. Please try again.',
      })
      setInternalPending(false)
    }
  }, [onConfirm, onCancel, internalPending, loading, suppressAutoClose])

  if (!open) return null

  // Effective loading = external `loading` prop OR internal pending state
  // (when awaiting a Promise returned by onConfirm).
  const isLoading = loading || internalPending
  // Once a result banner is showing, lock both buttons so the trader
  // can't double-fire another confirm before the auto-close.
  const isLocked = internalResult !== null

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !isLoading && !isLocked) onCancel()
      }}
      role="presentation"
      aria-hidden={!open}
    >
      <div
        ref={dialogRef}
        className="modal confirm-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-desc"
      >
        {/* Header */}
        <div className="modal-header" style={{ gap: '12px', alignItems: 'flex-start' }}>
          <div className={`confirm-icon ${severity}`} aria-hidden="true">
            {ICONS[severity]}
          </div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h2
              id="confirm-dialog-title"
              style={{ fontSize: '14px', fontWeight: 700, color: 'var(--text-primary)', marginBottom: '4px' }}
            >
              {title}
            </h2>
            <p
              id="confirm-dialog-desc"
              style={{ fontSize: '12.5px', color: 'var(--text-secondary)', lineHeight: 1.5 }}
            >
              {description}
            </p>
          </div>
        </div>

        {/* W49-5 — severity-tinted accent stripe under the header.
            Purely cosmetic: gives the dialog unambiguous gravity
            (red for danger, amber for warning, blue for info). */}
        <div
          className={`h-0.5 w-full ${ACCENT_STRIPE_CLASS[severity]}`}
          aria-hidden="true"
        />

        {/* Impact summary. W49-5: replaced the duplicated severity
            icon (which collided with the header's icon and was
            visually noisy) with a small bold "IMPACT" label. The
            impact text is wrapped in its own <span> so the existing
            test `getByText('This will cancel 5 open orders')` keeps
            matching — the span's textContent is exactly the impact
            string with no prefix/suffix. */}
        {impact && (
          <div className="modal-body" style={{ paddingTop: '12px', paddingBottom: '12px' }}>
            <div
              className={`banner-${severity === 'danger' ? 'danger' : severity === 'warning' ? 'warning' : 'info'}`}
              style={{ fontSize: '12.5px' }}
              role="note"
            >
              <strong className="font-bold uppercase text-[10px] tracking-wider mr-1.5" aria-hidden="true">
                Impact
              </strong>
              <span>{impact}</span>
            </div>
          </div>
        )}

        {/* W39-5/W49-5 — optional risk warning. Rendered ABOVE the
            action footer so the trader reads the impact summary
            first, then the explicit risk callout, then the action
            buttons. W49-5 tightens the label from "Risk:" to
            "⚠ RISK:" for stronger visual weight. The warning text
            is wrapped in its own <span> so it remains a leaf text
            node. */}
        {riskWarning && (
          <div
            className="modal-body"
            style={{ paddingTop: impact ? 0 : '12px', paddingBottom: '12px' }}
          >
            <div
              className="banner-warning"
              style={{ fontSize: '11.5px' }}
              role="alert"
            >
              <span aria-hidden="true">⚠️</span>
              <span>
                <strong style={{ fontWeight: 700 }}>RISK:</strong>{' '}
                <span>{riskWarning}</span>
              </span>
            </div>
          </div>
        )}

        {/* W39-5 — success/error feedback banner. Replaces the modal-body
            area when a result is present so the trader sees a clear ✓/✕
            outcome before the dialog auto-dismisses (success) or stays
            open for retry (error). */}
        {internalResult && (
          <div
            className="modal-body"
            style={{ paddingTop: '12px', paddingBottom: '12px' }}
          >
            <div
              className={RESULT_BANNER_CLASS[internalResult.status]}
              style={{ fontSize: '12.5px', alignItems: 'center' }}
              role={internalResult.status === 'error' ? 'alert' : 'status'}
              aria-live={internalResult.status === 'error' ? 'assertive' : 'polite'}
            >
              <span aria-hidden="true" style={{ fontWeight: 700 }}>
                {RESULT_ICON[internalResult.status]}
              </span>
              <span>{internalResult.message}</span>
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="modal-footer">
          <button
            ref={cancelBtnRef}
            onClick={onCancel}
            className="btn btn-ghost"
            disabled={isLoading || isLocked}
            aria-label={cancelLabel}
          >
            {cancelLabel}
          </button>
          <button
            onClick={handleConfirm}
            className={`btn ${CONFIRM_COLORS[severity]}`}
            disabled={isLoading || isLocked}
            aria-label={confirmLabel}
          >
            {isLoading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                Processing…
              </>
            ) : internalResult?.status === 'success' ? (
              <>
                <span aria-hidden="true">✓</span>
                Done
              </>
            ) : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
