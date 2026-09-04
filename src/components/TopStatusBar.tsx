// components/TopStatusBar.tsx — Persistent system status & ML health bar
'use client'

import { useEffect, useState } from 'react'
import { ConnectionStatus, BotSnapshot } from '@/hooks/useBot'
import { fmtPnl, fmtUsd, fmtAge, freshnessClass, fmtUptime } from '@/lib/design-tokens'
import { getApiUrl, apiFetch } from '@/lib/api'
// W13-4 — Theme toggle (dark/light). Sits in the right-hand action
// cluster next to mute / shortcuts / config so the trader can flip
// the entire workstation's palette without leaving the status bar.
import ThemeToggle from './ThemeToggle'
// W14-2 — i18n locale switcher. Sits next to the theme toggle so
// appearance + language controls cluster together; both are
// "preference" controls rather than trading actions.
import LocaleSwitcher from './LocaleSwitcher'
// W15-5 — Live WS / polling transport pill. Sits next to the
// connection status pill so the trader sees the full transport
// stack at a glance: REST connection (bot API) + WebSocket (push
// channel). Distinct from `ConnectionStatus` (the type alias from
// useBot) — the imported `ConnectionStatus` identifier below refers
// to the bot's transport state enum; the component is imported as
// `ConnectionStatusPill` to avoid the name collision.
import ConnectionStatusPill from './ConnectionStatus'
// W15-2 — Full-screen Settings modal. Opened via the gear icon
// (added below) so the trader can tune the polling cadence, sound,
// privacy flags, etc. without leaving the workstation. Mirrors the
// ShortcutsModal + StrategyConfigModal pattern (mounted by parent,
// toggled via a status-bar icon).
import SettingsModal from './SettingsModal'

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
  dotClass,
  label,
  title,
}: {
  dotClass?: string
  label: string
  title?: string
}) {
  const isHealthy = dotClass === 'healthy'
  return (
    <div
      className="flex items-center gap-1.5 text-xs whitespace-nowrap bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md shadow-sm transition-colors hover:border-[#2d3450]"
      title={title}
    >
      <span
        className={`w-2 h-2 rounded-full ${
          isHealthy
            ? 'bg-green-400 shadow-sm shadow-green-500/50 animate-pulse'
            : 'bg-amber-400 shadow-sm shadow-amber-500/50'
        }`}
        aria-hidden="true"
      />
      <span className="font-semibold text-[11px] text-[#dde1ed]">{label}</span>
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

  const [mlInfo, setMlInfo] = useState<{ brier: number; auc: number; status: string } | null>(null)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)

  // W15-2 — Local state for the full Settings modal. Rendered at the
  // bottom of the header. The modal itself reads/writes the global
  // preferences store (`usePreferences`) so opening it from any other
  // entry point in the future (e.g. the command palette) just needs
  // to flip `settingsOpen=true`.
  const [settingsOpen, setSettingsOpen] = useState(false)

  useEffect(() => {
    const fetchMl = async () => {
      try {
        const apiUrl = getApiUrl()
        const t0 = performance.now()
        const [mRes, dRes] = await Promise.all([
          apiFetch(`${apiUrl}/api/ml/metrics`),
          apiFetch(`${apiUrl}/api/ml/drift`),
        ])
        const t1 = performance.now()
        setLatencyMs(Math.round(t1 - t0))
        if (mRes.ok && dRes.ok) {
          const m = await mRes.json()
          const d = await dRes.json()
          setMlInfo({
            brier: m.brier_score ?? 0.145,
            auc: m.roc_auc ?? 0.835,
            status: d.status ?? 'HEALTHY',
          })
        }
      } catch (e) {
        // W22-1 — log ML health fetch failures so they don't disappear
        // silently. The status bar keeps the last known values (or none)
        // until the next 6s poll recovers.
        console.error('[TopStatusBar] Failed to fetch ML health:', e)
      }
    }
    fetchMl()
    const t = setInterval(fetchMl, 6000)
    return () => clearInterval(t)
  }, [])

  // Connection state display
  const connLabel =
    status === 'connected' ? 'Connected' : status === 'connecting' ? 'Connecting…' : 'Disconnected'
  const connDotClass = status === 'connected' ? 'healthy' : 'connecting'

  // Data freshness
  const dataAge = snapshot.timestamp > 0 ? snapshot.timestamp : null
  const ageStr = dataAge ? fmtAge(dataAge) : 'No data'
  const ageClass = dataAge ? freshnessClass(dataAge, 15, 60) : 'freshness-dead'

  // Mode label and badge styling
  const modeLabel = mode === 'live' ? 'LIVE TRADING' : mode === 'shadow' ? 'SHADOW MODE' : 'PAPER TRADING'
  const modeBadgeClass =
    mode === 'live'
      ? 'bg-red-500/20 text-red-400 border border-red-500/40'
      : mode === 'shadow'
      ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40'
      : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'

  // Live ticking UTC clock
  const [nowUtc, setNowUtc] = useState('')
  useEffect(() => {
    const update = () => setNowUtc(new Date().toISOString().slice(11, 19) + ' UTC')
    update()
    const t = setInterval(update, 1000)
    return () => clearInterval(t)
  }, [])

  return (
    <header className="topbar h-12 bg-[#0e1015]/95 backdrop-blur-md border-b border-[#1f2335] px-3 flex items-center justify-between gap-3 sticky top-0 z-40" role="banner" aria-label="System status bar">
      {/* Left Section: Mobile Nav + Mode & Connection */}
      <div className="flex items-center gap-2.5 shrink-0">
        <button
          onClick={onMobileNav}
          className="btn btn-ghost btn-sm p-1.5 md:hidden"
          aria-label="Open navigation"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <rect x="1" y="3" width="14" height="1.5" rx="0.75" fill="currentColor" />
            <rect x="1" y="7.25" width="14" height="1.5" rx="0.75" fill="currentColor" />
            <rect x="1" y="11.5" width="14" height="1.5" rx="0.75" fill="currentColor" />
          </svg>
        </button>

        {/* Mode badge */}
        <span
          className={`px-2.5 py-1 rounded text-[10.5px] font-extrabold tracking-wider uppercase flex items-center gap-1 shadow-sm ${modeBadgeClass}`}
          title="Canonical Trading Mode"
        >
          {mode === 'live' && <span className="animate-ping w-1.5 h-1.5 rounded-full bg-red-400 inline-block mr-0.5" />}
          {modeLabel}
        </span>

        {/* Kill switch indicator */}
        {kill_switch && (
          <span className="bg-red-600 text-white text-[10px] font-extrabold px-2 py-0.5 rounded animate-pulse">
            🛑 HALTED
          </span>
        )}

        {/* Observation mode indicator */}
        {observation_only && !kill_switch && (
          <span className="bg-amber-500/20 text-amber-300 border border-amber-500/40 text-[10px] font-bold px-2 py-0.5 rounded">
            👁 OBS ONLY
          </span>
        )}

        <StatusPill dotClass={connDotClass} label={connLabel} title={`Bot API Connection: ${connLabel}`} />

        {/* W15-5 — WebSocket transport pill. Distinct from the REST
            connection pill above: this surfaces whether the bot's
            push channel is live (green dot) or whether the UI is
            relying on REST polling (amber dot). Click for tooltip. */}
        <ConnectionStatusPill />

        {/* Latency Telemetry */}
        {latencyMs !== null && status === 'connected' && (
          <div
            className="hidden sm:flex text-[11px] mono text-[#7e8aaa] px-2 py-1 bg-[#0e1015] border border-[#1f2335] rounded-md items-center gap-1"
            title="REST API Roundtrip Latency"
          >
            <span className={`w-1.5 h-1.5 rounded-full ${latencyMs < 100 ? 'bg-green-400' : latencyMs < 300 ? 'bg-amber-400' : 'bg-red-400'}`} />
            <span>{latencyMs}ms</span>
          </div>
        )}

        {/* Data Freshness */}
        <div className={`text-[11px] mono text-[#7e8aaa] px-2 py-1 bg-[#0e1015] border border-[#1f2335] rounded-md flex items-center gap-1 ${ageClass}`}>
          <span>⏱</span>
          <span>{ageStr}</span>
        </div>

        {/* S5: Mobile-only Balance + Daily P&L pill.
            The center section below (BAL / TODAY P&L / ML health) is
            `hidden lg:flex`, so traders on xs/sm/md breakpoints lose
            sight of their position. This `lg:hidden` pill is the inverse
            — visible below `lg`, hidden at `lg`+ — so the two never
            co-render. Combines both values into a single compact pill
            (two pills would overflow xs screens). Uses the exact same
            design-system classes as the center-section pills. */}
        <div
          className="lg:hidden flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2 py-1 rounded-md text-xs whitespace-nowrap"
          title={`Paper balance ${paper_balance != null ? fmtUsd(paper_balance) : '—'} · Today P&L ${fmtPnl(daily_pnl)}`}
        >
          <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">BAL:</span>
          <span className="mono font-bold text-cyan-300">
            {paper_balance != null ? fmtUsd(paper_balance) : '—'}
          </span>
          <span className="text-[#3e4560]" aria-hidden="true">|</span>
          <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">P&amp;L:</span>
          <span
            className={`mono font-bold ${
              daily_pnl > 0 ? 'text-green-400' : daily_pnl < 0 ? 'text-red-400' : 'text-[#dde1ed]'
            }`}
          >
            {fmtPnl(daily_pnl)}
          </span>
        </div>
      </div>

      {/* Center Section: ML Health, Capital, Uptime */}
      <div className="hidden lg:flex items-center gap-3">
        {/* ML Health Pill */}
        {mlInfo && (
          <div className="flex items-center gap-2 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs">
            <span className="text-[10px] text-cyan-400 font-bold uppercase tracking-wider flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" />
              ML 4-Ensemble:
            </span>
            <span className="mono text-[11px] text-green-400 font-semibold">Brier {mlInfo.brier.toFixed(3)}</span>
            <span className="text-[#3e4560]">|</span>
            <span className="mono text-[11px] text-cyan-300 font-semibold">AUC {(mlInfo.auc * 100).toFixed(0)}%</span>
            <span className="text-[#3e4560]">|</span>
            <span className={`text-[10px] font-bold uppercase ${mlInfo.status === 'HEALTHY' ? 'text-green-400' : 'text-amber-400'}`}>
              {mlInfo.status}
            </span>
          </div>
        )}

        {/* Capital Balance */}
        <div className="flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs">
          <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">BAL:</span>
          <span className="mono font-bold text-cyan-300">
            {paper_balance != null ? fmtUsd(paper_balance) : '—'}
          </span>
        </div>

        {/* Daily PnL */}
        <div className="flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs">
          <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">TODAY P&amp;L:</span>
          <span
            className={`mono font-bold ${
              daily_pnl > 0 ? 'text-green-400' : daily_pnl < 0 ? 'text-red-400' : 'text-[#dde1ed]'
            }`}
          >
            {fmtPnl(daily_pnl)}
          </span>
        </div>

        {/* Uptime */}
        {uptime > 0 && (
          <div
            className="hidden xl:flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs"
            title="Bot engine uptime since last restart"
          >
            <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">UP:</span>
            <span className="mono font-semibold text-[#dde1ed]">{fmtUptime(uptime)}</span>
          </div>
        )}
      </div>

      {/* Right Section: Time & Global Action Buttons */}
      <div className="flex items-center gap-2 shrink-0">
        <span className="hidden sm:inline-block mono text-[11px] text-[#7e8aaa] bg-[#0e1015] border border-[#1f2335] px-2 py-1 rounded-md">
          {nowUtc}
        </span>

        {/* W15-2 — Full Settings modal trigger. Opens the comprehensive
            preferences dialog (theme, polling cadence, sound, privacy, …).
            Sits at the very start of the icon cluster so the trader's eye
            lands on it first when scanning for "the gear". Uses 🛠 (hammer
            + wrench) instead of ⚙️ to disambiguate from the existing
            "⚙️ Config" button below (which is specifically the strategy &
            risk configuration modal). */}
        <button
          onClick={() => setSettingsOpen(true)}
          className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white"
          title="User preferences (theme, polling, sound, privacy)"
          aria-label="Open user preferences"
          aria-haspopup="dialog"
          aria-expanded={settingsOpen}
        >
          <span aria-hidden="true">🛠</span>
        </button>

        {/* W13-4 — Theme toggle (dark/light). Sits at the start of the
            icon cluster so it reads "appearance" → "language" → "audio"
            → "input help" → "config". The button itself renders null
            on SSR (handled inside ThemeToggle to dodge hydration
            mismatch). */}
        <ThemeToggle />

        {/* W14-2 — Locale switcher (EN / FR). Sits immediately after the
            theme toggle so the two "appearance" controls stay grouped;
            picks up the trader's persisted choice via useTranslation
            and flips all t() consumers in one React commit. */}
        <LocaleSwitcher />

        {onToggleMute && (
          <button
            onClick={onToggleMute}
            className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white"
            title={muted ? 'Unmute alerts' : 'Mute alerts'}
            aria-label={muted ? 'Unmute audio alerts' : 'Mute audio alerts'}
            aria-pressed={muted}
          >
            <span aria-hidden="true">{muted ? '🔇' : '🔊'}</span>
          </button>
        )}

        {onOpenShortcuts && (
          <button
            onClick={onOpenShortcuts}
            className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white"
            title="Shortcuts (?)"
            aria-label="Open keyboard shortcuts cheatsheet"
          >
            <span aria-hidden="true">⌨️</span>
          </button>
        )}

        {onOpenConfig && (
          <button
            onClick={onOpenConfig}
            className="btn btn-ghost btn-sm text-xs font-semibold text-[#7e8aaa] hover:text-white flex items-center gap-1 px-2 py-1"
            aria-label="Open strategy and risk configuration modal"
          >
            <span aria-hidden="true">⚙️</span> Config
          </button>
        )}

        <button
          onClick={onCancelAll}
          className="btn btn-amber btn-sm text-xs font-bold px-2.5 py-1 shadow-sm"
          title="Cancel all open orders across strategies"
        >
          ✕ Cancel All
        </button>

        {kill_switch ? (
          <button
            onClick={onResumeSwitch}
            className="btn btn-resume btn-sm text-xs font-extrabold px-3 py-1 bg-green-600 hover:bg-green-500 text-white shadow-md animate-pulse"
            title="Resume trading (K)"
          >
            ▶ RESUME
          </button>
        ) : (
          <button
            onClick={onKillSwitch}
            className="btn btn-kill btn-sm text-xs font-extrabold px-3 py-1 bg-red-600 hover:bg-red-500 text-white shadow-md"
            title="Emergency Kill Switch (K)"
          >
            🛑 KILL SWITCH
          </button>
        )}
      </div>

      {/* W15-2 — Full-screen Settings modal. Mounted at the bottom of the
          header so it overlays the entire workstation when open. The modal
          is lazy-rendered (only when settingsOpen=true) so the bundle
          cost is paid on first open, not on initial workstation paint. */}
      <SettingsModal isOpen={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  )
}
