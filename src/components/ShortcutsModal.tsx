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

  useEffect(() => {
    if (!isOpen) return
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [isOpen, onClose])

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
          <button onClick={onClose} className="modal-close" aria-label="Close shortcuts modal">
            ✕
          </button>
        </div>

        <div className="modal-body space-y-2 max-h-[70vh] overflow-y-auto scrollbar-thin">
          {SHORTCUTS.map((s) => (
            <div
              key={s.key}
              className="flex justify-between items-center bg-[#0e1015] px-3 py-2 rounded text-xs border border-[#1f2335]"
            >
              <span className="text-[#dde1ed]">{s.action}</span>
              <kbd className="bg-[#13161e] text-cyan-400 border border-[#1f2335] px-2 py-0.5 rounded mono font-bold text-[11px]">
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
