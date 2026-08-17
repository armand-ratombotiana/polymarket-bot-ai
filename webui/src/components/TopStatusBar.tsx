// components/TopStatusBar.tsx — Persistent system status bar
// Always visible; communicates mode, connectivity, freshness, and risk state.
'use client'

import { ConnectionStatus, BotSnapshot } from '@/hooks/useBot'
import { fmtPnl, fmtUsd, fmtAge, freshnessClass, fmtUptime } from '@/lib/design-tokens'

interface TopStatusBarProps {
  snapshot: BotSnapshot
  status: ConnectionStatus
  uptime: number
  onKillSwitch: () => void
  onResumeSwitch: () => void
  onCancelAll: () => void
  onOpenShortcuts?: () => void
  onToggleMute?: () => void
  muted?: boolean
  onOpenConfig?: () => void
  onMobileNav?: () => void
}

function StatusPill({
  dot,
  dotClass,
  label,
  title,
}: {
  dot?: boolean
  dotClass?: string
  label: string
  title?: string
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '5px',
        fontSize: '11px',
        color: 'var(--text-secondary)',
        whiteSpace: 'nowrap',
      }}
      title={title}
    >
      {dot !== false && (
        <span className={`status-dot ${dotClass ?? 'unknown'}`} aria-hidden="true" />
      )}
      {label}
    </div>
  )
}

export default function TopStatusBar({
  snapshot,
  status,
  uptime,
  onKillSwitch,
  onResumeSwitch,
  onCancelAll,
  onOpenShortcuts,
  onToggleMute,
  muted,
  onOpenConfig,
  onMobileNav,
}: TopStatusBarProps) {
  const { mode, kill_switch, observation_only, daily_pnl, paper_balance } = snapshot

  // Connection state display
  const connLabel =
    status === 'connected'    ? 'Connected'    :
    status === 'connecting'   ? 'Connecting…'  :
    status === 'disconnected' ? 'Disconnected' : 'Error'
  const connDotClass =
    status === 'connected'  ? 'healthy'   :
    status === 'connecting' ? 'connecting' :
    'unavailable'

  // Data freshness
  const dataAge = snapshot.timestamp > 0 ? snapshot.timestamp : null
  const ageStr = dataAge ? fmtAge(dataAge) : 'No data'
  const ageClass = dataAge ? freshnessClass(dataAge, 15, 60) : 'freshness-dead'

  // Mode label and class
  const modeLabel = mode === 'live' ? 'LIVE' : mode === 'shadow' ? 'SHADOW' : 'PAPER'
  const modeBadgeClass = mode === 'live' ? 'mode-badge-live' : mode === 'shadow' ? 'mode-badge-shadow' : 'mode-badge-paper'

  // Current UTC time (rendered client-only via snapshot timestamp for stability)
  const nowUtc = new Date().toISOString().slice(11, 19) + ' UTC'

  return (
    <header className="topbar" role="banner" aria-label="System status bar">
      {/* Mobile hamburger */}
      <button
        onClick={onMobileNav}
        className="btn btn-ghost btn-sm"
        style={{ display: 'none', padding: '4px' }}
        aria-label="Open navigation"
        id="mobile-nav-btn"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
          <rect x="1" y="3" width="14" height="1.5" rx="0.75" fill="currentColor"/>
          <rect x="1" y="7.25" width="14" height="1.5" rx="0.75" fill="currentColor"/>
          <rect x="1" y="11.5" width="14" height="1.5" rx="0.75" fill="currentColor"/>
        </svg>
      </button>

      {/* Mode badge — always visible */}
      <span
        className={`mode-badge ${modeBadgeClass}`}
        aria-label={`Trading mode: ${modeLabel}`}
        title="Current trading mode — operations run in this mode"
      >
        {mode === 'live'   && <span aria-hidden="true">⚡</span>}
        {mode === 'shadow' && <span aria-hidden="true">👁</span>}
        {mode === 'paper'  && <span aria-hidden="true">📄</span>}
        {modeLabel}
      </span>

      {/* Kill switch state */}
      {kill_switch && (
        <span
          className="badge badge-danger"
          aria-label="Kill switch active — all trading halted"
          aria-live="assertive"
        >
          🛑 HALTED
        </span>
      )}

      {/* Observation only */}
      {observation_only && !kill_switch && (
        <span
          className="badge badge-amber"
          aria-label="Observation-only mode — new orders disabled"
          title={snapshot.observation_reason || 'New orders disabled — exposure not reconciled'}
        >
          👁 OBS ONLY
        </span>
      )}

      {/* Divider */}
      <div style={{ width: '1px', height: '18px', background: 'var(--border)', flexShrink: 0 }} />

      {/* Connectivity */}
      <StatusPill
        dotClass={connDotClass}
        label={connLabel}
        title={`WebSocket / REST connection to bot API: ${connLabel}`}
      />

      {/* Data freshness */}
      <div
        className={ageClass}
        style={{ fontSize: '11px', whiteSpace: 'nowrap' }}
        title={`Last snapshot received ${ageStr}. Threshold: stale >15s, dead >60s`}
        aria-label={`Data freshness: ${ageStr}`}
      >
        ⏱ {ageStr}
      </div>

      {/* Divider */}
      <div style={{ width: '1px', height: '18px', background: 'var(--border)', flexShrink: 0 }} />

      {/* Balance — null shown as —, never $100 fallback */}
      <div
        style={{ fontSize: '11.5px', whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}
        title="Paper balance (USDC). This is simulated capital, not real funds."
      >
        <span style={{ color: 'var(--text-dim)', fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          BAL{' '}
        </span>
        <span className="mono" style={{
          color: paper_balance != null ? 'var(--color-cyan-fg)' : 'var(--text-dim)',
          fontSize: '12px',
        }}>
          {paper_balance != null ? fmtUsd(paper_balance) : '—'}
        </span>
      </div>

      {/* Daily P&L */}
      <div
        style={{ fontSize: '11.5px', whiteSpace: 'nowrap', color: 'var(--text-secondary)' }}
        title="Realized daily P&L in paper mode (USDC). Resets at UTC midnight."
      >
        <span style={{ color: 'var(--text-dim)', fontSize: '10px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
          P&L{' '}
        </span>
        <span
          className="mono"
          style={{
            color: daily_pnl > 0 ? 'var(--color-green-fg)' : daily_pnl < 0 ? 'var(--color-red-fg)' : 'var(--text-secondary)',
            fontWeight: 600,
            fontSize: '12px',
          }}
        >
          {fmtPnl(daily_pnl)}
        </span>
      </div>

      {/* Uptime */}
      <div
        className="mono"
        style={{ fontSize: '11px', color: 'var(--text-dim)', whiteSpace: 'nowrap' }}
        title="UI session uptime (not bot uptime)"
        aria-label={`UI session uptime: ${fmtUptime(uptime)}`}
      >
        {fmtUptime(uptime)}
      </div>

      {/* UTC clock */}
      <div
        style={{ fontSize: '11px', color: 'var(--text-dim)', whiteSpace: 'nowrap', fontFamily: 'JetBrains Mono, monospace' }}
        aria-label={`Current time: ${nowUtc}`}
      >
        {nowUtc}
      </div>

      {/* Spacer */}
      <div style={{ flex: 1 }} />

      {/* Action group */}
      <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
        {onToggleMute && (
          <button
            onClick={onToggleMute}
            className="btn btn-ghost btn-sm"
            title={muted ? 'Unmute audio alerts' : 'Mute audio alerts'}
            aria-label={muted ? 'Unmute audio alerts' : 'Mute audio alerts'}
          >
            {muted ? '🔇' : '🔊'}
          </button>
        )}

        {onOpenShortcuts && (
          <button
            onClick={onOpenShortcuts}
            className="btn btn-ghost btn-sm"
            title="Keyboard shortcuts (?)"
            aria-label="View keyboard shortcuts"
          >
            ⌨️
          </button>
        )}

        {onOpenConfig && (
          <button
            onClick={onOpenConfig}
            className="btn btn-ghost btn-sm"
            title="Configure strategy & risk parameters (C)"
            aria-label="Configure strategy and risk parameters"
          >
            ⚙️ Config
          </button>
        )}

        {/* Cancel all — requires confirmation (handled in parent via ConfirmationDialog) */}
        <button
          onClick={onCancelAll}
          className="btn btn-amber btn-sm"
          title="Cancel all open orders — requires confirmation"
          aria-label="Cancel all open orders"
        >
          ✕ Cancel Orders
        </button>

        {/* Kill switch / Resume — primary emergency control */}
        {kill_switch ? (
          <button
            onClick={onResumeSwitch}
            className="btn btn-resume btn-sm"
            aria-label="Resume trading — deactivate kill switch"
            title="Resume trading (K)"
          >
            ▶ Resume
          </button>
        ) : (
          <button
            onClick={onKillSwitch}
            className="btn btn-kill btn-sm"
            aria-label="Activate kill switch — halt all trading immediately"
            title="Kill switch — halt all trading (K)"
          >
            🛑 Kill Switch
          </button>
        )}
      </div>
    </header>
  )
}
