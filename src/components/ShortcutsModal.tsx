// components/ShortcutsModal.tsx — Keyboard Shortcuts Cheatsheet
'use client'

import { useEffect, useRef } from 'react'

interface Props {
  isOpen: boolean
  onClose: () => void
}

const SHORTCUTS = [
  { key: '1', action: 'Command Center Dashboard' },
  { key: '2', action: 'Live Books & Markets Desk' },
  { key: '3', action: 'Prediction Market Screener' },
  { key: '4', action: 'Portfolio Positions' },
  { key: '5', action: 'Strategy Registry & Gating' },
  { key: '6', action: 'Arbitrage Scanner' },
  { key: '7', action: 'Deep Intelligence Forecaster' },
  { key: '8', action: 'Performance Analytics' },
  { key: 'K', action: 'Toggle Kill Switch / Emergency Halt' },
  { key: 'C', action: 'Open Strategy & Risk Configuration' },
  { key: 'Esc', action: 'Close active modal / drawer' },
  { key: '?', action: 'Open this shortcuts cheatsheet' },
]

export default function ShortcutsModal({ isOpen, onClose }: Props) {
  const modalRef = useRef<HTMLDivElement>(null)
  const closeBtnRef = useRef<HTMLButtonElement>(null)

  // W9-7 — Restore focus to the trigger element (the last focused element
  // before the modal opened). Captured on open via `lastActiveRef` and
  // restored on cleanup. Mirrors the pattern used in ConfirmationDialog.
  const lastActiveRef = useRef<HTMLElement | null>(null)

  // W9-7 — Escape closes the modal. Same handler as before, kept for
  // backward compatibility (the global handler in page.tsx also dispatches
  // Escape, but this one is scoped to this component so the modal is
  // self-contained if reused elsewhere).
  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        onClose()
      }
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

  // W9-7 — On open: capture the currently focused element (the trigger
  // button in TopStatusBar) so we can restore focus on close, then move
  // focus into the modal close button so the user has an obvious
  // starting point. On close: restore focus to the trigger.
  useEffect(() => {
    if (isOpen) {
      if (typeof document !== 'undefined' && document.activeElement instanceof HTMLElement) {
        lastActiveRef.current = document.activeElement
      }
      // Defer focus to allow the modal to mount before we steal focus
      const t = setTimeout(() => closeBtnRef.current?.focus(), 50)
      return () => clearTimeout(t)
    } else {
      // Restore focus to the trigger when closing
      lastActiveRef.current?.focus?.()
      lastActiveRef.current = null
    }
  }, [isOpen])

  // W9-7 — Focus trap: keep Tab focus cycling within the modal while open.
  // Mirrors the ConfirmationDialog pattern: queries all focusable elements
  // inside the dialog and wraps focus at the boundaries.
  useEffect(() => {
    if (!isOpen) return
    const el = modalRef.current
    if (!el) return
    const focusableSelectors =
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
    const trap = (e: KeyboardEvent) => {
      if (e.key !== 'Tab') return
      const focusables = Array.from(el.querySelectorAll<HTMLElement>(focusableSelectors))
      if (focusables.length === 0) return
      const first = focusables[0]
      const last = focusables[focusables.length - 1]
      if (e.shiftKey) {
        if (document.activeElement === first) {
          e.preventDefault()
          last?.focus()
        }
      } else {
        if (document.activeElement === last) {
          e.preventDefault()
          first?.focus()
        }
      }
    }
    el.addEventListener('keydown', trap)
    return () => el.removeEventListener('keydown', trap)
  }, [isOpen])

  if (!isOpen) return null

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
      role="presentation"
    >
      <div
        ref={modalRef}
        className="modal"
        style={{ maxWidth: '440px' }}
        role="dialog"
        aria-modal="true"
        aria-labelledby="shortcuts-title"
      >
        <div className="modal-header">
          <div className="flex items-center gap-2">
            <span aria-hidden="true">⌨️</span>
            <h2 id="shortcuts-title" className="text-sm font-bold text-[#dde1ed]">
              Workstation Keyboard Shortcuts
            </h2>
          </div>
          <button
            ref={closeBtnRef}
            onClick={onClose}
            className="modal-close"
            aria-label="Close shortcuts modal"
          >
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <div className="modal-body space-y-2 max-h-[70vh] overflow-y-auto scrollbar-thin">
          {SHORTCUTS.map((s) => (
            <div
              key={s.key}
              className="flex justify-between items-center bg-[#0e1015] px-3 py-2 rounded text-xs border border-[#1f2335]"
            >
              <span className="text-[#dde1ed]">{s.action}</span>
              <kbd
                className="bg-[#13161e] text-cyan-400 border border-[#1f2335] px-2 py-0.5 rounded mono font-bold text-[11px]"
                aria-label={`Shortcut key ${s.key}`}
              >
                {s.key}
              </kbd>
            </div>
          ))}
        </div>

        <div className="modal-footer justify-center">
          <button onClick={onClose} className="btn btn-primary btn-sm">
            Got it (Esc)
          </button>
        </div>
      </div>
    </div>
  )
}
