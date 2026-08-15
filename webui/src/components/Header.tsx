// components/Header.tsx — Polymarket Pro Workstation Navigation Bar
'use client'

import { ConnectionStatus } from '@/hooks/useBot'

export type ActiveTab = 'terminal' | 'strategies' | 'aiml' | 'copilot' | 'screener'

interface HeaderProps {
  activeTab: ActiveTab
  onTabChange: (tab: ActiveTab) => void
  mode: 'paper' | 'live'
  killSwitch: boolean
  dailyPnl: number
  paperBalance: number | null
  strategies: string[]
  status: ConnectionStatus
  onKillSwitch: () => void
  onDeactivate: () => void
  onCancelAll: () => void
  onOpenConfig?: () => void
  uptime: number
}

function fmtUptime(s: number) {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}

function fmtPnl(v: number) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${Math.abs(v).toFixed(2)}`
}

export default function Header({
  activeTab, onTabChange, mode, killSwitch, dailyPnl, paperBalance,
  strategies, status, onKillSwitch, onDeactivate, onCancelAll, onOpenConfig, uptime,
}: HeaderProps) {
  const navTabs: Array<{ id: ActiveTab; label: string; icon: string; badge?: string }> = [
    { id: 'terminal', label: 'Trading Desk', icon: '📊' },
    { id: 'strategies', label: '50+ Strategies', icon: '⚡', badge: `${strategies.length} active` },
    { id: 'aiml', label: 'AI / ML Engine', icon: '🧠', badge: 'Calibrated' },
    { id: 'copilot', label: 'AI Copilot', icon: '🤖' },
    { id: 'screener', label: 'Screener', icon: '🔍' },
  ]

  return (
    <header className="h-14 bg-[#111318] border-b border-[#252836] flex items-center px-4 gap-4 shrink-0 select-none">
      {/* Logo */}
      <div className="flex items-center gap-2 mr-1">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
          <circle cx="11" cy="11" r="10" stroke="#3b82f6" strokeWidth="1.5" />
          <path d="M7 11h8M11 7v8" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round" />
        </svg>
        <span className="text-[14px] font-bold tracking-tight text-[#e8eaf0]">
          Polymarket<span className="text-blue-400">Pro 3.0</span>
        </span>
      </div>

      {/* Navigation Tabs */}
      <div className="flex items-center gap-1 bg-[#161822] p-1 rounded-lg border border-[#252836]">
        {navTabs.map((t) => (
          <button
            key={t.id}
            onClick={() => onTabChange(t.id)}
            className={`px-3 py-1 rounded text-xs font-semibold flex items-center gap-1.5 transition-all ${
              activeTab === t.id
                ? 'bg-blue-500 text-black shadow-sm font-bold'
                : 'text-[#8b91a8] hover:text-[#e8eaf0] hover:bg-[#252836]'
            }`}
          >
            <span>{t.icon}</span>
            <span>{t.label}</span>
            {t.badge && (
              <span
                className={`text-[9px] px-1 py-0.2 rounded font-mono ${
                  activeTab === t.id ? 'bg-black/20 text-black' : 'bg-blue-500/20 text-blue-400'
                }`}
              >
                {t.badge}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Mode badge */}
      <span className={`badge ${mode === 'paper' ? 'badge-amber' : 'badge-red'}`}>
        {mode === 'paper' ? '📄 Paper' : '⚡ Live'}
      </span>

      {/* Connection status */}
      <div className="hidden sm:flex items-center gap-1.5 text-[11px] text-[#8b91a8]">
        <span
          className="status-dot"
          style={{
            background: status === 'connected' ? '#22c55e' : status === 'connecting' ? '#f59e0b' : '#ef4444',
          }}
        />
        {status}
      </div>

      <div className="flex-1" />

      {/* Paper balance */}
      {paperBalance !== null && (
        <div className="text-[12px] hidden md:block">
          <span className="text-[#4a5068]">Balance </span>
          <span className="mono font-medium text-cyan-400">
            ${paperBalance.toLocaleString('en', { minimumFractionDigits: 2 })}
          </span>
        </div>
      )}

      {/* Daily P&L */}
      <div className="text-[12px]">
        <span className="text-[#4a5068]">Daily P&amp;L </span>
        <span className={`mono font-semibold ${dailyPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
          {fmtPnl(dailyPnl)}
        </span>
      </div>

      {/* Uptime */}
      <div className="hidden lg:block text-[11px] text-[#4a5068] mono">
        {fmtUptime(uptime)}
      </div>

      {/* Actions */}
      <div className="flex items-center gap-2">
        {onOpenConfig && (
          <button
            onClick={onOpenConfig}
            className="btn btn-ghost text-blue-400 hover:text-blue-300 border-blue-900/50 hover:border-blue-700 text-xs px-2.5 py-1.5"
            title="Configure Strategy & Risk Parameters"
          >
            ⚙️ Config
          </button>
        )}
        <button
          onClick={onCancelAll}
          className="btn btn-ghost text-amber-400 hover:text-amber-300 border-amber-900/50 hover:border-amber-700 text-xs px-2.5 py-1.5"
        >
          ✕ Cancel Orders
        </button>
        {killSwitch ? (
          <button onClick={onDeactivate} className="btn btn-success text-xs px-3 py-1.5">
            ▶ Resume
          </button>
        ) : (
          <button onClick={onKillSwitch} className="btn btn-danger text-xs px-3 py-1.5">
            🛑 Kill Switch
          </button>
        )}
      </div>

      {/* Kill switch pulse header line */}
      {killSwitch && (
        <div className="absolute top-0 left-0 right-0 h-0.5 bg-red-500 animate-pulse" />
      )}
    </header>
  )
}
