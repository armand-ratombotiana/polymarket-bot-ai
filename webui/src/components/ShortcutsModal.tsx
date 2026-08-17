// components/ShortcutsModal.tsx — Keyboard Shortcuts Cheatsheet
'use client'

interface Props {
  isOpen: boolean
  onClose: () => void
}

const SHORTCUTS = [
  { key: '1 – 8', action: 'Switch workstation tabs (Trading Desk, Arb Matrix, Strategies, AI/ML, Deep Analysis, Timescale DB, Backtest, Copilot)' },
  { key: 'K', action: 'Toggle Kill Switch / Emergency halt' },
  { key: 'C', action: 'Open Strategy & Risk Configuration drawer' },
  { key: 'Esc', action: 'Close any active modal or drawer' },
  { key: '?', action: 'Open this keyboard shortcuts cheatsheet' },
]

export default function ShortcutsModal({ isOpen, onClose }: Props) {
  if (!isOpen) return null

  return (
    <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50 p-4 backdrop-blur-sm select-none">
      <div className="bg-[#111318] border border-[#252836] rounded-xl w-full max-w-md overflow-hidden shadow-2xl">
        <div className="px-5 py-3 border-b border-[#252836] flex justify-between items-center bg-[#161822]">
          <div className="flex items-center gap-2">
            <span>⌨️</span>
            <h3 className="text-sm font-bold text-[#e8eaf0]">Keyboard Shortcuts</h3>
          </div>
          <button onClick={onClose} className="text-[#8b91a8] hover:text-white text-base">
            ✕
          </button>
        </div>

        <div className="p-4 space-y-2.5 max-h-[70vh] overflow-y-auto scrollbar-thin">
          {SHORTCUTS.map((s) => (
            <div key={s.key} className="flex justify-between items-center bg-[#161822] px-3 py-2 rounded text-xs border border-[#252836]">
              <span className="text-[#8b91a8]">{s.action}</span>
              <kbd className="bg-[#111318] text-cyan-400 border border-[#252836] px-2 py-0.5 rounded mono font-bold text-[11px]">
                {s.key}
              </kbd>
            </div>
          ))}
        </div>

        <div className="p-3 bg-[#161822] border-t border-[#252836] text-center">
          <button onClick={onClose} className="btn btn-primary px-5 py-1 text-xs">
            Got it
          </button>
        </div>
      </div>
    </div>
  )
}
