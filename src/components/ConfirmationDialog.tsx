// components/ConfirmationDialog.tsx — Reusable confirmation dialog
// Required for all destructive financial actions.
'use client'

import { useEffect, useRef } from 'react'

type Severity = 'danger' | 'warning' | 'info'

interface ConfirmationDialogProps {
  open: boolean
  severity?: Severity
  title: string
  /** Plain-language description of exactly what will happen */
  description: string
  /** Optional impact summary (e.g., "This will cancel 5 open orders") */
  impact?: string
  confirmLabel?: string
  cancelLabel?: string
  onConfirm: () => void
  onCancel: () => void
  /** Set true while the action is executing */
  loading?: boolean
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

export default function ConfirmationDialog({
  open,
  severity = 'danger',
  title,
  description,
  impact,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  onConfirm,
  onCancel,
  loading = false,
}: ConfirmationDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null)
  const cancelBtnRef = useRef<HTMLButtonElement>(null)

  // Focus management: focus cancel button on open (safer default)
  useEffect(() => {
    if (open) {
      // Defer to allow animation
      const t = setTimeout(() => cancelBtnRef.current?.focus(), 50)
      return () => clearTimeout(t)
    }
  }, [open])

  // Keyboard: Escape cancels
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onCancel()
      }
    }
    window.addEventListener('keydown', handler, true)
    return () => window.removeEventListener('keydown', handler, true)
  }, [open, onCancel])

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

  if (!open) return null

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onCancel() }}
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

        {/* Impact summary */}
        {impact && (
          <div className="modal-body" style={{ paddingTop: '12px', paddingBottom: '12px' }}>
            <div
              className={`banner-${severity === 'danger' ? 'danger' : severity === 'warning' ? 'warning' : 'info'}`}
              style={{ fontSize: '12.5px' }}
              role="note"
            >
              <span aria-hidden="true">{ICONS[severity]}</span>
              {impact}
            </div>
          </div>
        )}

        {/* Actions */}
        <div className="modal-footer">
          <button
            ref={cancelBtnRef}
            onClick={onCancel}
            className="btn btn-ghost"
            disabled={loading}
            aria-label={cancelLabel}
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            className={`btn ${CONFIRM_COLORS[severity]}`}
            disabled={loading}
            aria-label={confirmLabel}
          >
            {loading ? (
              <>
                <span className="spinner" aria-hidden="true" />
                Processing…
              </>
            ) : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}
