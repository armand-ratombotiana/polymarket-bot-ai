// components/TopStatusBar.tsx — Compact single-line status bar + system health mini-bar.
//
// W49-6 — Redesigned for a compact, professional single-line layout.
// The bar is now organised into three logical clusters:
//
//   LEFT   → app logo + "Polymarket Pro" + active-panel breadcrumb
//   CENTER → Balance · P&L today · Exposure · Mode badge  (KPI cluster)
//   RIGHT  → Connection pill · Alert bell · Theme · Locale · Settings ·
//            Shortcuts · Mute · Config · Cancel All · Kill/Resume
//
// Below the 44px status row sits a 2px System Health mini-bar that
// reflects the rolled-up health of /api/system/health. Clicking it
// jumps to the System Health sidebar section so the trader can
// drill into the per-service breakdown.
//
// Real-time touches:
//   - Balance flashes green for 700ms on every increase, red on every
//     decrease (paper_balance changes drive the flash; mark-to-market
//     gains therefore pulse the bar).
//   - Connection state already live-updates via the existing
//     `useWebSocket`-backed <ConnectionStatusPill>.
//   - Alert unread count already live-updates via <AlertNotificationsPanel>'s
//     own useWebSocket subscription.
//
// Responsive contract:
//   - Desktop (lg+, ≥1024px): full cluster — logo + breadcrumb + KPIs +
//     every right-side control.
//   - Tablet  (md–lg, 768–1023px): hide the breadcrumb; show logo +
//     KPIs + the right-side controls.
//   - Mobile  (<md, <768px): hide KPIs + breadcrumb; show logo +
//     connection pill + alert bell + kill switch. The mobile-only
//     balance/P&L pill is dropped — the centre KPIs take its place on
//     tablet+, and on phones the balance is one tap away via the
//     Command Center panel.
'use client'

import { useEffect, useRef, useState } from 'react'
import { ConnectionStatus, BotSnapshot, Position } from '@/hooks/useBot'
import { fmtPnl, fmtUsd, fmtAge, freshnessClass, fmtUptime } from '@/lib/design-tokens'
import { getApiUrl, apiFetch } from '@/lib/api'
import ThemeToggle from './ThemeToggle'
import LocaleSwitcher from './LocaleSwitcher'
import ConnectionStatusPill from './ConnectionStatus'
import SettingsModal from './SettingsModal'
import { AlertNotificationsPanel } from './AlertNotificationsPanel'

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
  // W49-6 — Active panel name (e.g. "Command Center", "Live Books")
  // rendered as a breadcrumb next to the logo. Resolved by the parent
  // page.tsx via Sidebar's getNavItemMeta + useTranslation so the bar
  // stays decoupled from the nav catalog. Optional so existing tests
  // that don't pass it still render.
  panelName?: string
  panelGroup?: string
  // W49-6 — Click handler for the system-health mini-bar. Opens the
  // System Health sidebar section. Optional so existing tests still
  // render (the bar degrades to a non-clickable status indicator).
  onOpenSystemHealth?: () => void
}

// ── Helpers ────────────────────────────────────────────────────────────

// Total $ exposure = Σ |total_invested| across open positions.
// `total_invested` is signed (shorts are negative); we absolute-value
// each leg so a long+short pair doesn't artificially cancel out the
// gross exposure number.
function computeExposure(positions: Position[] | undefined): number {
  if (!Array.isArray(positions) || positions.length === 0) return 0
  return positions.reduce((sum, p) => sum + Math.abs(p.total_invested || 0), 0)
}

// W49-6 — Rolled-up health tier for the 2px mini-bar.
//   healthy   → all services UP/RUNNING/HEALTHY + overall status HEALTHY
//   degraded  → at least one service unhealthy, but < half are down
//   critical  → overall status CRITICAL, or ≥ half of services down
//   unknown   → endpoint unreachable / payload malformed
type HealthTier = 'healthy' | 'degraded' | 'critical' | 'unknown'

interface SystemHealthPayload {
  status?: string
  services?: Array<{ name?: string; status?: string }>
  ml_engine?: { drift_status?: string }
}

function deriveHealthTier(data: SystemHealthPayload | null | undefined): HealthTier {
  if (!data) return 'unknown'
  const status = String(data.status || '').toUpperCase()
  if (status === 'CRITICAL') return 'critical'

  const services = Array.isArray(data.services) ? data.services : []
  if (services.length === 0) {
    if (status === 'HEALTHY' || status === 'UP' || status === 'OK') return 'healthy'
    if (status === 'DEGRADED') return 'degraded'
    return 'unknown'
  }

  const isHealthy = (s: string) => {
    const u = String(s || '').toUpperCase()
    return u === 'HEALTHY' || u === 'UP' || u === 'RUNNING' || u === 'OK'
  }
  const downCount = services.filter((s) => !isHealthy(s.status || '')).length
  const drift = String(data.ml_engine?.drift_status || '').toUpperCase()
  const driftDegraded = drift === 'DRIFT' || drift === 'DEGRADED'

  if (downCount >= Math.ceil(services.length / 2)) return 'critical'
  if (downCount > 0 || driftDegraded || status === 'DEGRADED') return 'degraded'
  return 'healthy'
}

const HEALTH_TIER_STYLE: Record<HealthTier, { bg: string; glow: string; label: string }> = {
  healthy:   { bg: 'bg-emerald-400',  glow: 'shadow-[0_0_8px_rgba(34,197,94,0.55)]',  label: 'All systems healthy' },
  degraded:  { bg: 'bg-amber-400',    glow: 'shadow-[0_0_8px_rgba(245,158,11,0.55)]', label: 'Degraded — click for details' },
  critical:  { bg: 'bg-red-500',      glow: 'shadow-[0_0_8px_rgba(239,68,68,0.6)]',   label: 'Critical — click for details' },
  unknown:   { bg: 'bg-slate-500',    glow: '',                                          label: 'Health unknown — click to retry' },
}

// ── Component ──────────────────────────────────────────────────────────

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
  panelName,
  panelGroup,
  onOpenSystemHealth,
}: TopStatusBarProps) {
  const {
    mode,
    kill_switch,
    observation_only,
    daily_pnl,
    paper_balance,
    positions,
  } = snapshot

  // ── ML health (legacy W22-1 telemetry, kept for the ML pill on xl+) ──
  const [mlInfo, setMlInfo] = useState<{ brier: number; auc: number; status: string } | null>(null)
  const [latencyMs, setLatencyMs] = useState<number | null>(null)

  // W49-6 — System health tier for the 2px mini-bar.
  const [healthTier, setHealthTier] = useState<HealthTier>('unknown')

  // W15-2 — Local state for the full Settings modal.
  const [settingsOpen, setSettingsOpen] = useState(false)

  // W49-6 — Real-time balance flash on every paper_balance change.
  // Tracks the previous value in a ref so we can diff against the
  // incoming snapshot. On change we briefly apply a green (increase)
  // or red (decrease) glow to the Balance KPI; the glow clears after
  // 700ms (matches the existing price-flash CSS animation duration
  // so the two rhythms feel consistent).
  const prevBalanceRef = useRef<number | null>(null)
  const balanceFlashTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [balanceFlash, setBalanceFlash] = useState<'up' | 'down' | null>(null)

  useEffect(() => {
    if (paper_balance == null) return
    const prev = prevBalanceRef.current
    if (prev != null && paper_balance !== prev) {
      const next: 'up' | 'down' = paper_balance > prev ? 'up' : 'down'
      setBalanceFlash(next)
      if (balanceFlashTimerRef.current) {
        clearTimeout(balanceFlashTimerRef.current)
      }
      balanceFlashTimerRef.current = setTimeout(() => {
        setBalanceFlash(null)
        balanceFlashTimerRef.current = null
      }, 700)
    }
    prevBalanceRef.current = paper_balance
  }, [paper_balance])

  useEffect(() => {
    return () => {
      if (balanceFlashTimerRef.current) {
        clearTimeout(balanceFlashTimerRef.current)
      }
    }
  }, [])

  // ── Data fetch effect ────────────────────────────────────────────────
  // Pings three endpoints in parallel every 6s:
  //   /api/ml/metrics      → ML ensemble Brier + ROC AUC
  //   /api/ml/drift        → ML drift status
  //   /api/system/health   → overall + per-service status (W49-6)
  // Uses Promise.allSettled so a single endpoint failure doesn't
  // poison the others (e.g. /api/system/health returning 404 leaves
  // ML telemetry intact — and vice versa).
  useEffect(() => {
    const fetchData = async () => {
      try {
        const apiUrl = getApiUrl()
        const t0 = performance.now()
        const results = await Promise.allSettled([
          apiFetch(`${apiUrl}/api/ml/metrics`),
          apiFetch(`${apiUrl}/api/ml/drift`),
          apiFetch(`${apiUrl}/api/system/health`),
        ])
        const t1 = performance.now()
        setLatencyMs(Math.round(t1 - t0))

        const [mRes, dRes, hRes] = results.map((r) =>
          r.status === 'fulfilled' ? r.value : null,
        )

        if (mRes && mRes.ok && dRes && dRes.ok) {
          const m = await mRes.json()
          const d = await dRes.json()
          setMlInfo({
            brier: m.brier_score ?? 0.145,
            auc: m.roc_auc ?? 0.835,
            status: d.status ?? 'HEALTHY',
          })
        }

        if (hRes && hRes.ok) {
          const h = await hRes.json()
          setHealthTier(deriveHealthTier(h))
        } else {
          // Don't downgrade to 'unknown' on a transient 5xx — keep
          // the last known tier so a brief backend hiccup doesn't
          // flicker the bar to grey. Only flips to unknown on the
          // very first poll (initial state) or after a 4xx.
          if (hRes && hRes.status >= 400 && hRes.status < 500) {
            setHealthTier('unknown')
          }
        }
      } catch (e) {
        console.error('[TopStatusBar] Failed to fetch ML/system health:', e)
      }
    }
    fetchData()
    const t = setInterval(fetchData, 6000)
    return () => clearInterval(t)
  }, [])

  // ── Derived display values ───────────────────────────────────────────
  const connLabel =
    status === 'connected' ? 'Connected' : status === 'connecting' ? 'Connecting…' : 'Disconnected'
  const connDotClass = status === 'connected' ? 'healthy' : 'connecting'

  const dataAge = snapshot.timestamp > 0 ? snapshot.timestamp : null
  const ageStr = dataAge ? fmtAge(dataAge) : 'No data'
  const ageClass = dataAge ? freshnessClass(dataAge, 15, 60) : 'freshness-dead'

  // Mode label + badge styling (kept identical to the prior bar so the
  // existing W38-8 tests that assert 'PAPER TRADING' / 'LIVE TRADING' /
  // 'SHADOW MODE' text continue to pass).
  const modeLabel = mode === 'live' ? 'LIVE TRADING' : mode === 'shadow' ? 'SHADOW MODE' : 'PAPER TRADING'
  const modeBadgeClass =
    mode === 'live'
      ? 'bg-red-500/20 text-red-400 border border-red-500/40'
      : mode === 'shadow'
      ? 'bg-cyan-500/20 text-cyan-400 border border-cyan-500/40'
      : 'bg-amber-500/20 text-amber-300 border border-amber-500/40'

  const exposure = computeExposure(positions)

  // Live ticking UTC clock (kept for xl+ desktops).
  const [nowUtc, setNowUtc] = useState('')
  useEffect(() => {
    const update = () => setNowUtc(new Date().toISOString().slice(11, 19) + ' UTC')
    update()
    const t = setInterval(update, 1000)
    return () => clearInterval(t)
  }, [])

  const healthStyle = HEALTH_TIER_STYLE[healthTier]

  return (
    <header
      className="topbar h-auto sticky top-0 z-40 flex flex-col items-stretch bg-[#0e1015]/95 backdrop-blur-md border-b border-[#1f2335]"
      role="banner"
      aria-label="System status bar"
    >
      {/* ── Main 44px row ─────────────────────────────────────────────── */}
      <div className="h-11 px-3 flex items-center justify-between gap-3">
        {/* ─── LEFT: mobile nav · logo · breadcrumb · halt/obs badges ─── */}
        <div className="flex items-center gap-2.5 shrink-0 min-w-0">
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

          {/* W49-6 — App logo + name. Compact (24px) logo + "Polymarket Pro"
              wordmark. Hidden on the narrowest phones (xs:hidden) so the
              KPI cluster gets priority real estate; shows on ≥480px. */}
          <div className="flex items-center gap-2 min-w-0">
            <svg
              width="22"
              height="22"
              viewBox="0 0 24 24"
              fill="none"
              aria-hidden="true"
              className="shrink-0"
            >
              <circle cx="12" cy="12" r="10.5" stroke="#60a5fa" strokeWidth="1.5" />
              <circle cx="12" cy="12" r="5.5" stroke="#60a5fa" strokeWidth="1" strokeOpacity="0.5" />
              <line x1="12" y1="2" x2="12" y2="22" stroke="#60a5fa" strokeWidth="1" strokeOpacity="0.35" />
              <line x1="2" y1="12" x2="22" y2="12" stroke="#60a5fa" strokeWidth="1" strokeOpacity="0.35" />
            </svg>
            <span
              className="hidden xs:inline-block text-[13px] font-bold tracking-tight whitespace-nowrap"
              style={{ color: 'var(--text-primary)' }}
            >
              Polymarket<span style={{ color: '#60a5fa' }}>Pro</span>
            </span>

            {/* W49-6 — Active panel breadcrumb. Renders as
                "Markets / Live Books" so the trader always knows which
                sidebar section is mounted. Hidden below lg (tablet +
                mobile) per the responsive spec. */}
            {panelName && (
              <span
                className="hidden lg:flex items-center gap-1.5 text-[11.5px] whitespace-nowrap min-w-0"
                aria-current="page"
              >
                <span className="text-[#3e4560]" aria-hidden="true">/</span>
                {panelGroup && (
                  <span className="text-[#7e8aaa] font-medium truncate">{panelGroup}</span>
                )}
                <span className="text-[#3e4560]" aria-hidden="true">/</span>
                <span className="text-[#dde1ed] font-semibold truncate">{panelName}</span>
              </span>
            )}
          </div>

          {/* Kill switch / observation-only badges — kept identical to
              the prior bar so the existing tests asserting the literal
              text '🛑 HALTED' / '👁 OBS ONLY' continue to pass. */}
          {kill_switch && (
            <span className="bg-red-600 text-white text-[10px] font-extrabold px-2 py-0.5 rounded animate-pulse">
              🛑 HALTED
            </span>
          )}
          {observation_only && !kill_switch && (
            <span className="bg-amber-500/20 text-amber-300 border border-amber-500/40 text-[10px] font-bold px-2 py-0.5 rounded">
              👁 OBS ONLY
            </span>
          )}
        </div>

        {/* ─── CENTER: KPI cluster (md+) ──────────────────────────────── */}
        <div className="hidden md:flex items-center gap-2 grow justify-center">
          {/* Balance — mono, bold, cyan. Flashes green on increase,
              red on decrease. The flash is layered via an additional
              ring class rather than swapping the text colour so the
              baseline cyan stays legible mid-flash. */}
          <div
            className={`flex items-center gap-1.5 bg-[#13161e] border px-2.5 py-1 rounded-md text-xs whitespace-nowrap transition-shadow ${
              balanceFlash === 'up'
                ? 'border-emerald-500/60 shadow-[0_0_0_1px_rgba(34,197,94,0.4),0_0_10px_rgba(34,197,94,0.45)]'
                : balanceFlash === 'down'
                ? 'border-red-500/60 shadow-[0_0_0_1px_rgba(239,68,68,0.4),0_0_10px_rgba(239,68,68,0.45)]'
                : 'border-[#1f2335]'
            }`}
            title={`Paper balance ${paper_balance != null ? fmtUsd(paper_balance) : '—'}`}
          >
            <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">BAL</span>
            <span className="mono font-bold text-cyan-300">
              {paper_balance != null ? fmtUsd(paper_balance) : '—'}
            </span>
          </div>

          {/* P&L today — green/red, mono, bold. */}
          <div className="flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs whitespace-nowrap">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">P&amp;L</span>
            <span
              className={`mono font-bold ${
                daily_pnl > 0 ? 'text-green-400' : daily_pnl < 0 ? 'text-red-400' : 'text-[#dde1ed]'
              }`}
            >
              {fmtPnl(daily_pnl)}
            </span>
          </div>

          {/* Exposure — gross $ across all open positions. */}
          <div
            className="flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs whitespace-nowrap"
            title="Gross $ exposure across open positions"
          >
            <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">EXP</span>
            <span className="mono font-bold text-[#dde1ed]">{fmtUsd(exposure)}</span>
          </div>

          {/* Mode badge — kept as 'PAPER TRADING' / 'LIVE TRADING' /
              'SHADOW MODE' for back-compat with existing tests. */}
          <span
            className={`px-2.5 py-1 rounded text-[10.5px] font-extrabold tracking-wider uppercase flex items-center gap-1 shadow-sm ${modeBadgeClass}`}
            title="Canonical Trading Mode"
          >
            {mode === 'live' && (
              <span className="animate-ping w-1.5 h-1.5 rounded-full bg-red-400 inline-block mr-0.5" />
            )}
            {modeLabel}
          </span>

          {/* ML health pill — kept on xl+ only (desktop). */}
          {mlInfo && (
            <div className="hidden xl:flex items-center gap-2 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs whitespace-nowrap">
              <span className="text-[10px] text-cyan-400 font-bold uppercase tracking-wider flex items-center gap-1">
                <span className="w-1.5 h-1.5 rounded-full bg-cyan-400" aria-hidden="true" />
                ML:
              </span>
              <span className="mono text-[11px] text-green-400 font-semibold">Brier {mlInfo.brier.toFixed(3)}</span>
              <span className="text-[#3e4560]" aria-hidden="true">|</span>
              <span className="mono text-[11px] text-cyan-300 font-semibold">AUC {(mlInfo.auc * 100).toFixed(0)}%</span>
              <span className="text-[#3e4560]" aria-hidden="true">|</span>
              <span className={`text-[10px] font-bold uppercase ${mlInfo.status === 'HEALTHY' ? 'text-green-400' : 'text-amber-400'}`}>
                {mlInfo.status}
              </span>
            </div>
          )}

          {/* Uptime — desktop xl+ only. */}
          {uptime > 0 && (
            <div
              className="hidden xl:flex items-center gap-1.5 bg-[#13161e] border border-[#1f2335] px-2.5 py-1 rounded-md text-xs whitespace-nowrap"
              title="Bot engine uptime since last restart"
            >
              <span className="text-[10px] text-[#7e8aaa] uppercase font-bold">UP</span>
              <span className="mono font-semibold text-[#dde1ed]">{fmtUptime(uptime)}</span>
            </div>
          )}
        </div>

        {/* ─── RIGHT: status + controls + actions ─────────────────────── */}
        <div className="flex items-center gap-1.5 shrink-0">
          {/* UTC clock — xl+ only (frees space on smaller screens). */}
          <span className="hidden xl:inline-block mono text-[11px] text-[#7e8aaa] bg-[#0e1015] border border-[#1f2335] px-2 py-1 rounded-md">
            {nowUtc}
          </span>

          {/* REST connection pill — sm+ (hidden on the narrowest phones). */}
          <div
            className="hidden sm:flex items-center gap-1.5 text-xs whitespace-nowrap bg-[#0e1015] border border-[#1f2335] px-2 py-1 rounded-md transition-colors hover:border-[#2d3450]"
            title={`Bot API Connection: ${connLabel}`}
          >
            <span
              className={`w-2 h-2 rounded-full ${
                status === 'connected'
                  ? 'bg-green-400 shadow-sm shadow-green-500/50 animate-pulse'
                  : 'bg-amber-400 shadow-sm shadow-amber-500/50'
              }`}
              aria-hidden="true"
            />
            <span className="font-semibold text-[11px] text-[#dde1ed]">{connLabel}</span>
          </div>

          {/* Data freshness — sm+. */}
          <div
            className={`hidden sm:flex text-[11px] mono text-[#7e8aaa] px-2 py-1 bg-[#0e1015] border border-[#1f2335] rounded-md items-center gap-1 ${ageClass}`}
            title="Data freshness since last snapshot"
          >
            <span aria-hidden="true">⏱</span>
            <span>{ageStr}</span>
          </div>

          {/* Latency telemetry — lg+. */}
          {latencyMs !== null && status === 'connected' && (
            <div
              className="hidden lg:flex text-[11px] mono text-[#7e8aaa] px-2 py-1 bg-[#0e1015] border border-[#1f2335] rounded-md items-center gap-1"
              title="REST API Roundtrip Latency"
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  latencyMs < 100 ? 'bg-green-400' : latencyMs < 300 ? 'bg-amber-400' : 'bg-red-400'
                }`}
                aria-hidden="true"
              />
              <span>{latencyMs}ms</span>
            </div>
          )}

          {/* WebSocket transport pill. */}
          <ConnectionStatusPill />

          {/* ── Icon cluster (preferences + alerts + actions) ── */}
          {/* Settings (🛠) — opens the full preferences modal. */}
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

          {/* Theme toggle. */}
          <ThemeToggle />

          {/* Locale switcher — sm+. */}
          <div className="hidden sm:block">
            <LocaleSwitcher />
          </div>

          {/* Alert bell — always visible (the trader's primary
              real-time alert surface). Already wired to a WebSocket
              so the unread count badge live-updates. */}
          <AlertNotificationsPanel />

          {/* Mute toggle — sm+. */}
          {onToggleMute && (
            <button
              onClick={onToggleMute}
              className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white hidden sm:inline-flex"
              title={muted ? 'Unmute alerts' : 'Mute alerts'}
              aria-label={muted ? 'Unmute audio alerts' : 'Mute audio alerts'}
              aria-pressed={muted}
            >
              <span aria-hidden="true">{muted ? '🔇' : '🔊'}</span>
            </button>
          )}

          {/* Keyboard shortcuts (?) — sm+. */}
          {onOpenShortcuts && (
            <button
              onClick={onOpenShortcuts}
              className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white hidden sm:inline-flex"
              title="Shortcuts (?)"
              aria-label="Open keyboard shortcuts cheatsheet"
            >
              <span aria-hidden="true">⌨️</span>
            </button>
          )}

          {/* Strategy / risk config — md+. */}
          {onOpenConfig && (
            <button
              onClick={onOpenConfig}
              className="btn btn-ghost btn-sm text-xs font-semibold text-[#7e8aaa] hover:text-white items-center gap-1 px-2 py-1 hidden md:flex"
              aria-label="Open strategy and risk configuration modal"
            >
              <span aria-hidden="true">⚙️</span> Config
            </button>
          )}

          {/* Cancel All — sm+. */}
          <button
            onClick={onCancelAll}
            className="btn btn-amber btn-sm text-xs font-bold px-2.5 py-1 shadow-sm hidden sm:inline-flex"
            title="Cancel all open orders across strategies"
          >
            ✕ Cancel All
          </button>

          {/* Kill switch / Resume — always visible (the trader's
              emergency brake must be reachable on every breakpoint). */}
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
      </div>

      {/* ─── W49-6 — System Health mini-bar (2px) ───────────────────────
          A thin coloured strip below the main row that reflects the
          rolled-up health of /api/system/health. Click opens the
          System Health sidebar section. The bar is a real <button>
          (not a div) so it's keyboard-focusable + screen-reader
          announced as "System health: healthy / degraded / …".
          Renders even when onOpenSystemHealth is absent so the
          health indicator is always visible; in that case it's a
          non-interactive status strip (role="status"). */}
      {onOpenSystemHealth ? (
        <button
          type="button"
          onClick={onOpenSystemHealth}
          className={`group relative h-0.5 w-full ${healthStyle.bg} ${healthStyle.glow} transition-all duration-300 hover:h-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-offset-0 focus-visible:ring-white/40`}
          aria-label={`System health: ${healthTier}. ${healthStyle.label}. Open System Health panel.`}
          title={`System health: ${healthTier} — click to open the System Health panel`}
        >
          {/* Subtle shimmer so a healthy bar still reads as "live".
              The shimmer is a thin diagonal sweep that runs across
              the full width every 3s. */}
          <span
            aria-hidden="true"
            className="absolute inset-0 opacity-30 group-hover:opacity-60 transition-opacity"
            style={{
              backgroundImage:
                'linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.45) 50%, transparent 100%)',
              backgroundSize: '50% 100%',
              backgroundRepeat: 'no-repeat',
              animation: 'topbar-health-shimmer 3s linear infinite',
            }}
          />
        </button>
      ) : (
        <div
          role="status"
          aria-label={`System health: ${healthTier}. ${healthStyle.label}.`}
          className={`relative h-0.5 w-full ${healthStyle.bg} ${healthStyle.glow} transition-colors duration-300`}
          title={`System health: ${healthTier}`}
        >
          <span
            aria-hidden="true"
            className="absolute inset-0 opacity-30"
            style={{
              backgroundImage:
                'linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.45) 50%, transparent 100%)',
              backgroundSize: '50% 100%',
              backgroundRepeat: 'no-repeat',
              animation: 'topbar-health-shimmer 3s linear infinite',
            }}
          />
        </div>
      )}

      {/* W39-8 — Screen-reader live region for dynamic P&L updates.
          Sighted traders see the Balance KPI flash green/red on every
          snapshot tick; AT users get the same audible refresh cycle
          via this polite live region. Visually hidden via `.sr-live`
          (1×1 clip-rect) so it doesn't shift the layout. */}
      <div
        className="sr-live"
        role="status"
        aria-live="polite"
        aria-atomic="true"
      >
        Today P&L {fmtPnl(daily_pnl)}. Paper balance {paper_balance != null ? fmtUsd(paper_balance) : 'unknown'}. Connection {connLabel}. System health {healthTier}.
      </div>

      {/* W15-2 — Full-screen Settings modal. */}
      <SettingsModal isOpen={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  )
}
