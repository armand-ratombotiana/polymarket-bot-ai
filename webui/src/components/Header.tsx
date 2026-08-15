// components/Header.tsx
'use client'

import { useState } from 'react'
import { ConnectionStatus } from '@/hooks/useBot'
import { getApiUrl } from '@/lib/api'

interface HeaderProps {
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
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(sec).padStart(2,'0')}`
}

function fmtPnl(v: number) {
  const sign = v >= 0 ? '+' : ''
  return `${sign}$${Math.abs(v).toFixed(2)}`
}

const ALL_STRATEGIES = [
  { id: 'market_maker', label: 'Market Maker' },
  { id: 'arb_scanner', label: 'Arb Scanner' },
  { id: 'signal_trader', label: 'ML Signal' },
]

export default function Header({
  mode, killSwitch, dailyPnl, paperBalance, strategies, status,
  onKillSwitch, onDeactivate, onCancelAll, onOpenConfig, uptime,
}: HeaderProps) {
  const [toggling, setToggling] = useState<string | null>(null)

  const toggleStrategy = async (stratId: string) => {
    const isRunning = strategies.includes(stratId)
    setToggling(stratId)
    try {
      const apiUrl = getApiUrl()
      await fetch(`${apiUrl}/api/strategies/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_name: stratId, enabled: !isRunning }),
      })
    } catch {}
    setToggling(null)
  }

  return (
    <header className="h-14 bg-[#111318] border-b border-[#252836] flex items-center px-4 gap-3.5 shrink-0">
      {/* Logo */}
      <div className="flex items-center gap-2 mr-1">
        <svg width="22" height="22" viewBox="0 0 22 22" fill="none">
          <circle cx="11" cy="11" r="10" stroke="#3b82f6" strokeWidth="1.5"/>
          <path d="M7 11h8M11 7v8" stroke="#3b82f6" strokeWidth="1.5" strokeLinecap="round"/>
        </svg>
        <span className="text-[14px] font-semibold tracking-tight text-[#e8eaf0]">
          Polymarket<span className="text-blue-400">Bot 2.2</span>
        </span>
      </div>

      {/* Mode badge */}
      <span className={`badge ${mode === 'paper' ? 'badge-amber' : 'badge-red'}`}>
        {mode === 'paper' ? '📄 Paper' : '⚡ Live'}
      </span>

      {/* Connection status */}
      <div className="flex items-center gap-1.5 text-[11px] text-[#8b91a8]">
        <span
          className="status-dot"
          style={{
            background: status === 'connected' ? '#22c55e' : status === 'connecting' ? '#f59e0b' : '#ef4444',
          }}
        />
        {status}
      </div>

      {/* Interactive Strategy Toggles */}
      <div className="hidden lg:flex items-center gap-1.5 ml-2">
        {ALL_STRATEGIES.map((s) => {
          const active = strategies.includes(s.id)
          return (
            <button
              key={s.id}
              onClick={() => toggleStrategy(s.id)}
              disabled={toggling === s.id}
              className={`text-[10px] uppercase font-semibold px-2.5 py-1 rounded transition-all flex items-center gap-1 border ${
                active
                  ? 'bg-blue-500/20 text-blue-400 border-blue-500/40 hover:bg-blue-500/30'
                  : 'bg-[#1e2130]/50 text-[#606780] border-[#252836] hover:text-[#8b91a8]'
              }`}
              title={`Click to ${active ? 'pause' : 'start'} ${s.label}`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${active ? 'bg-blue-400 animate-pulse' : 'bg-[#4a5068]'}`} />
              {s.label}
            </button>
          )
        })}
      </div>

      <div className="flex-1" />

      {/* Paper balance */}
      {paperBalance !== null && (
        <div className="text-[12px]">
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
      <div className="hidden md:block text-[11px] text-[#4a5068] mono">
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

      {/* Kill switch indicator line */}
      {killSwitch && (
        <div className="absolute top-0 left-0 right-0 h-0.5 bg-red-500 animate-pulse" />
      )}
    </header>
  )
}
