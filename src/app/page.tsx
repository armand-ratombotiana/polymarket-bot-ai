// app/page.tsx — Polymarket Pro Trading Workstation
'use client'

import { useEffect, useState, useCallback, useRef, useMemo, type ComponentType } from 'react'
import { useBot } from '@/hooks/useBot'
import { useAudio } from '@/hooks/useAudio'
// W15-2 — preferences store hook. Powers the user-tunable settings:
// polling cadence (refreshIntervalMs), initial panel (defaultPanel),
// display flags (showUnrealizedPnl / showPriceFlashes), and more.
// Reads DEFAULTS on first paint (so SSR + hydration match), then
// reconciles to the persisted blob on mount — same hydration pattern
// as useTranslation + ThemeToggle.
import { usePreferences } from '@/hooks/usePreferences'
import Sidebar, { NavSection } from '@/components/Sidebar'
import TopStatusBar from '@/components/TopStatusBar'
import ConfirmationDialog from '@/components/ConfirmationDialog'
// W10-3 — Panel-level Error Boundary. Wrap each `activeSection` render case
// in <PanelErrorBoundary> so a render crash in one panel (e.g. malformed API
// payload causing a TypeError during render) is contained: only the affected
// panel shows a recoverable fallback; every other sidebar section keeps
// working. The root-level <ErrorBoundary> in src/app/layout.tsx is the
// outermost safety net for any error that escapes these per-panel wrappers.
import PanelErrorBoundary from '@/components/PanelErrorBoundary'

// W10-8 — Framer Motion panel transitions. The page-area is wrapped in
// `<AnimatePresence mode="wait">` so when `activeSection` changes, the
// outgoing panel fades out (200ms) before the new panel fades in. This
// eliminates the abrupt "pop" when switching tabs and matches the
// existing visual rhythm of the dashboard (which already animates value
// flashes + skeleton shimmers at ~0.15–1.5s). `FadeIn` is a thin
// wrapper around `motion.div` that animates only opacity + transform
// (no layout properties) for GPU-accelerated 60fps transitions.
import { AnimatePresence, FadeIn } from '@/components/ui/motion'

// Command Center
import RiskStatusPanel from '@/components/RiskStatusPanel'
import EquityCurve from '@/components/EquityCurve'
import AnalyticsPanel from '@/components/AnalyticsPanel'
import MLPanel from '@/components/MLPanel'
import EventLog from '@/components/EventLog'

// Markets
import MarketsPanel from '@/components/MarketsPanel'
import MarketScreener from '@/components/MarketScreener'

// W17-2 — Order Flow workstation (lazy: combines Recharts + framer-motion
// + per-tick render path; the dynamic chunk keeps the initial bundle lean).
const OrderFlowPanel = lazyPanel(() => import('@/components/OrderFlowPanel'), 'Loading Order Flow…')

// Portfolio
import PositionsPanel from '@/components/PositionsPanel'
import OrdersPanel from '@/components/OrdersPanel'
import TradesPanel from '@/components/TradesPanel'

// Strategies
import StrategyMatrix from '@/components/StrategyMatrix'
import ArbitrageMatrixView from '@/components/ArbitrageMatrixView'
// W23-5 — Strategy Performance dashboard (per-strategy P&L, risk-adjusted
// ranking, equity overlay, sortable comparison table). Loaded with
// `next/dynamic` + `ssr: false` so the Recharts multi-line + bar chart
// chunk stays out of the initial bundle.
const StrategyPerformancePanel = lazyPanel(() => import('@/components/StrategyPerformancePanel'), 'Loading Strategy Performance…')

// Intelligence
import DeepAnalysisView from '@/components/DeepAnalysisView'
import AIMLCommandCenter from '@/components/AIMLCommandCenter'
import AICopilotPanel from '@/components/AICopilotPanel'

// Analytics
import LeaderboardPanel from '@/components/LeaderboardPanel'
import BacktestLabView from '@/components/BacktestLabView'

// System
import SystemHealthView from '@/components/SystemHealthView'
import DatabaseExplorerView from '@/components/DatabaseExplorerView'

// W8-10 — Wave-8 intelligence / analytics / system / capital panels.
// Loaded with `next/dynamic` + `ssr: false` so the client-only panels
// (which touch `window`, `localStorage`, `matchMedia` at module scope or
// during initial render) are never evaluated on the server. The parent
// page is `'use client'` with a `mounted` guard, so dynamic chunks hydrate
// cleanly without SSR mismatch warnings.
//
// W9-6 — every dynamic import now declares a `loading:` skeleton instead
// of falling back to `null`. Previously, navigating to one of these
// sections would briefly render an empty pane (visible flash) before the
// dynamic chunk finished loading. The skeleton matches the panel-card
// visual language so the perceived transition is smooth.
import dynamic from 'next/dynamic'

// W9-6 — shared loading skeleton for dynamically-imported panels. Renders
// a representative card outline so the layout doesn't shift / flash blank
// while the chunk downloads. Kept intentionally lightweight (no DOM
// nesting beyond what's necessary) so it doesn't add measurable overhead
// to the initial bundle.
function PanelLoadingSkeleton({ label = 'Loading panel…' }: { label?: string }) {
  return (
    <div
      className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md overflow-hidden"
      role="status"
      aria-live="polite"
      aria-label={label}
    >
      <div
        className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center gap-2 bg-[#0e1015]/80"
      >
        <span className="spinner" aria-hidden="true" />
        <span className="text-xs font-bold text-[#dde1ed] tracking-wide">{label}</span>
      </div>
      <div className="p-3 flex flex-col gap-2 flex-1">
        <div className="h-3 rounded bg-[#1f2335] animate-pulse" style={{ width: '60%' }} />
        <div className="h-3 rounded bg-[#1f2335] animate-pulse" style={{ width: '40%' }} />
        <div className="h-3 rounded bg-[#1f2335] animate-pulse" style={{ width: '75%' }} />
        <div className="h-3 rounded bg-[#1f2335] animate-pulse" style={{ width: '55%' }} />
      </div>
      <span className="sr-only">{label}</span>
    </div>
  )
}

// Convenience wrapper: every dynamic() call below uses the same ssr:false
// + loading-skeleton shape — collapsing it into a one-liner reduces the
// risk of forgetting the skeleton on future panels. The return type is
// `ComponentType<any>` because Next.js's `dynamic()` returns a wrapped
// component whose prop types don't perfectly round-trip through TS
// generics; at runtime it behaves as the imported component.
function lazyPanel(
  loader: () => Promise<{ default: ComponentType<any> }>,
  label: string,
): ComponentType<any> {
  return dynamic(loader, {
    ssr: false,
    loading: () => <PanelLoadingSkeleton label={label} />,
  })
}

// Intelligence — Wave 8
const ShadowInferencePanel = lazyPanel(() => import('@/components/ShadowInferencePanel'), 'Loading Shadow Inference…')
const MLValidationPanel = lazyPanel(() => import('@/components/MLValidationPanel'), 'Loading ML Validation…')

// Analytics — Wave 8
const AttributionPanel = lazyPanel(() => import('@/components/AttributionPanel'), 'Loading Attribution…')
const ExecutionQualityPanel = lazyPanel(() => import('@/components/ExecutionQualityPanel'), 'Loading Execution Quality…')
const ClosedPositionsPanel = lazyPanel(() => import('@/components/ClosedPositionsPanel'), 'Loading Closed Positions…')
// W26-2 — Honest Performance Report: per-category metrics (backtest /
// walk-forward / paper / live) with confidence intervals + disclaimer.
// Loaded with `next/dynamic` + `ssr: false` so the Recharts equity-curve
// chunk stays out of the initial bundle.
const PerformanceReportPanel = lazyPanel(() => import('@/components/PerformanceReportPanel'), 'Loading Performance Report…')

// Capital — Wave 8
const CapitalAllocatorPanel = lazyPanel(() => import('@/components/CapitalAllocatorPanel'), 'Loading Capital Allocator…')

// System — Wave 8
const ObservabilityPanel = lazyPanel(() => import('@/components/ObservabilityPanel'), 'Loading Observability…')
const RetentionPanel = lazyPanel(() => import('@/components/RetentionPanel'), 'Loading Retention…')
const DecisionLedgerPanel = lazyPanel(() => import('@/components/DecisionLedgerPanel'), 'Loading Decision Ledger…')
const LiveSafetyGatePanel = lazyPanel(() => import('@/components/LiveSafetyGatePanel'), 'Loading Safety Gate…')
const AuditLogPanel = lazyPanel(() => import('@/components/AuditLogPanel'), 'Loading Audit Log…')
const RateLimitPanel = lazyPanel(() => import('@/components/RateLimitPanel'), 'Loading Rate Limits…')
// W21-7 — Database Status panel (PG vs SQLite + health + table stats).
const DatabaseStatusPanel = lazyPanel(() => import('@/components/DatabaseStatusPanel'), 'Loading Database Status…')

// Modals
import DepthChartModal from '@/components/DepthChartModal'
import MarketChartModal from '@/components/MarketChartModal'
import StrategyConfigModal from '@/components/StrategyConfigModal'
// W17-6 — Legacy ShortcutsModal kept for any consumer that hasn't
// migrated; page.tsx itself uses the new KeyboardCheatSheet below.
// Mounted only when `legacyShortcutsOpen` flips true (e.g. via the
// ⌨️ icon in TopStatusBar, which still wires to the legacy modal so
// existing tests / muscle memory continue to work). The new
// KeyboardCheatSheet is opened via the `?` shortcut OR the floating
// ShortcutHint button.
import ShortcutsModal from '@/components/ShortcutsModal'
import KeyboardCheatSheet from '@/components/KeyboardCheatSheet'
import ShortcutHint from '@/components/ShortcutHint'
// W17-6 — Keyboard-shortcut catalog + hook. The catalog is the
// single source of truth for what shortcuts exist; the hook binds
// those shortcuts to live action callbacks below.
import {
  SHORTCUT_DEFINITIONS,
  type Shortcut,
} from '@/lib/keyboardShortcuts'
import { useKeyboardShortcuts } from '@/hooks/useKeyboardShortcuts'

// Keyboard shortcut → nav section mapping
const KB_MAP: Record<string, NavSection> = {
  '1': 'command',
  '2': 'markets-books',
  '3': 'markets-screener',
  '4': 'portfolio-positions',
  '5': 'strategies-registry',
  '6': 'strategies-arbitrage',
  '7': 'intelligence-analysis',
  '8': 'analytics-performance',
}

// W15-2 — Set of valid NavSection values, used to guard the persisted
// `defaultPanel` preference against drift (a renamed section, a
// malformed persisted value, etc.). Built once at module scope so
// the same Set instance is reused across renders.
const NAV_SECTION_KEYS = new Set<string>(Object.values(KB_MAP))

export default function Dashboard() {
  const [mounted, setMounted] = useState(false)
  // W15-2 — preferences drives the polling cadence + display flags.
  // `refreshIntervalMs` is passed to `useBot` so the REST fallback
  // poll honours the trader's tuning; `defaultPanel` is applied on
  // mount via a one-shot effect below; `showUnrealizedPnl` +
  // `showPriceFlashes` flow into PositionsPanel / MarketsPanel.
  const { preferences } = usePreferences()
  const { snapshot, status, wsConnected, priceFlashes, activateKillSwitch, deactivateKillSwitch, cancelAllOrders, cancelOrder, closePosition } = useBot({
    refreshIntervalMs: preferences.refreshIntervalMs,
  })
  const audio = useAudio()

  const [uptime, setUptime] = useState(0)
  const [startTime] = useState(() => Date.now())
  const [activeSection, setActiveSection] = useState<NavSection>('command')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  // Modal states
  const [selectedMarket, setSelectedMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [chartMarket, setChartMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  // W17-6 — `shortcutsOpen` now drives the NEW KeyboardCheatSheet
  // (full-screen overlay with search + practice mode + JSON export)
  // instead of the legacy ShortcutsModal. The legacy modal is still
  // mounted (see the JSX tree below) so any consumer that imports
  // it directly doesn't break, but the keyboard `?` shortcut + the
  // TopStatusBar ⌨️ icon both flip THIS state — opening the new
  // cheat sheet. The floating ShortcutHint button (bottom-right)
  // also opens this state.
  const [shortcutsOpen, setShortcutsOpen] = useState(false)

  // Confirmation dialog state
  const [confirmKill, setConfirmKill] = useState(false)
  const [confirmCancelAll, setConfirmCancelAll] = useState(false)
  const [actionLoading, setActionLoading] = useState(false)

  // U13 — Audio cue tracking refs.
  // `lastTradeIdRef`      → remembers the most recent trade_id we have already
  //                        played a fill cue for, so each new fill sounds
  //                        exactly once.
  // `lastWhaleTradeIdRef` → independently tracks whale-sized fills (size > $5)
  //                        so that a whale fires BOTH the regular fill cue and
  //                        the distinct whale-alert cue, without either cue
  //                        replaying for the same trade.
  const lastTradeIdRef = useRef<string | null>(null)
  const lastWhaleTradeIdRef = useRef<string | null>(null)

  useEffect(() => {
    setMounted(true)
  }, [])

  // W15-2 — apply the persisted `defaultPanel` preference on first mount.
  // Done as a separate effect (rather than initialising `useState` with
  // the persisted value) because `preferences` is still DEFAULTS during
  // the first render — the persisted value only loads via the mount
  // effect inside `usePreferences`. By the time this effect runs, the
  // preferences have reconciled to the persisted blob (or stayed at
  // DEFAULTS on a fresh install) and `preferences.defaultPanel` reflects
  // the trader's chosen landing panel.
  //
  // Intentionally runs ONCE on mount (empty deps array). We do NOT
  // want to re-flip the active section every time the trader edits
  // `defaultPanel` in the SettingsModal — they're already looking at
  // some panel by then, and silently yanking them to the new default
  // would be jarring. The `preferences.defaultPanel` read here
  // captures whatever the persisted value is at mount time; subsequent
  // edits to the preference are intentionally ignored by this effect.
  //
  // The cast is defensive: a malformed persisted value (e.g. an old
  // panel key that was since renamed) falls through to `command` via
  // the `NAV_SECTION_KEYS.has(panel)` guard. Without the guard, an
  // unknown string would land in `setActiveSection` and Sidebar would
  // render the empty-state (no section header highlights).
  //
  // `react-hooks/exhaustive-deps` is disabled in the project's
  // eslint.config.mjs so the empty deps array doesn't trigger a
  // warning — the intent is "mount-only", not "respond to every
  // preference change".
  useEffect(() => {
    const panel = preferences.defaultPanel
    if (typeof panel === 'string' && panel.length > 0 && NAV_SECTION_KEYS.has(panel as NavSection)) {
      setActiveSection(panel as NavSection)
    }
  }, [])

  // Uptime counter
  useEffect(() => {
    if (!mounted) return
    const t = setInterval(() => setUptime(Math.floor((Date.now() - startTime) / 1000)), 1000)
    return () => clearInterval(t)
  }, [mounted, startTime])

  // ── U13 — Audible fill cue ─────────────────────────────────────────
  // Fires `audio.playTradeFill()` whenever `snapshot.recent_trades`
  // changes and the newest trade has a trade_id we have not yet sounded.
  // `recent_trades` is appended chronologically (server slices
  // `store.trades[-50:]`), so the last array element is the most recent
  // fill. The `lastTradeIdRef` guard prevents replays across snapshot
  // refreshes / re-renders.
  useEffect(() => {
    const trades = snapshot.recent_trades
    if (!trades || trades.length === 0) return
    const latest = trades[trades.length - 1]
    if (!latest || !latest.trade_id) return
    if (lastTradeIdRef.current !== latest.trade_id) {
      audio.playTradeFill()
      lastTradeIdRef.current = latest.trade_id
    }
  }, [snapshot.recent_trades, audio])

  // ── U13 — Whale alert ──────────────────────────────────────────────
  // Fires `audio.playWhaleAlert()` when the newest fill exceeds the
  // $5 size threshold. Tracked via a separate ref so that a whale fill
  // also triggers the regular fill cue above (two distinct sounds), and
  // neither cue replays for the same trade_id.
  useEffect(() => {
    const trades = snapshot.recent_trades
    if (!trades || trades.length === 0) return
    const latest = trades[trades.length - 1]
    if (!latest || !latest.trade_id) return
    if (latest.size > 5 && lastWhaleTradeIdRef.current !== latest.trade_id) {
      audio.playWhaleAlert()
      lastWhaleTradeIdRef.current = latest.trade_id
    }
  }, [snapshot.recent_trades, audio])

  const handleKillSwitch = useCallback(async () => {
    setActionLoading(true)
    await activateKillSwitch()
    audio.playKillSwitch()
    setActionLoading(false)
    setConfirmKill(false)
  }, [activateKillSwitch, audio])

  const handleResumeSwitch = useCallback(async () => {
    await deactivateKillSwitch()
  }, [deactivateKillSwitch])

  // W17-6 — Global keyboard shortcuts via the new useKeyboardShortcuts
  // hook. Replaces the legacy inline useEffect + window.addEventListener
  // block. The hook reads from `SHORTCUT_DEFINITIONS` (the single
  // source of truth used by BOTH the cheat sheet and the hook), so
  // the catalog can never drift from what the hook actually dispatches.
  //
  // Wiring notes:
  //   * `1`-`8` → setActiveSection via KB_MAP. Matches the Sidebar
  //     kbd hints exactly so muscle memory + visible hints agree.
  //   * `?` → opens the NEW KeyboardCheatSheet (full-screen overlay).
  //     The legacy ShortcutsModal is no longer opened via the
  //     keyboard; the new sheet is a strict superset.
  //   * `Escape` (global) → closes every modal / clears market
  //     selection / dismisses the mobile nav. Marked `global:true` in
  //     the catalog so it fires even when the user is mid-typing in
  //     the strategy-config or settings modal.
  //   * `c` → close selected position. If `chartMarket` is set (the
  //     trader clicked a market row to open the chart modal) we
  //     dispatch closePosition on its token_id; otherwise this is a
  //     no-op (the shortcut description says "selected market" so
  //     silence is the right behaviour when nothing is selected).
  //   * `x` → opens the cancel-all confirmation dialog (the actual
  //     DELETE call only fires on confirm — see `handleCancelAll`).
  //   * `r` → reloads the page. The bot's REST poller is the only
  //     source of truth for live data, so a page reload is the
  //     simplest correct "refresh everything" action — it re-runs
  //     every effect from a clean slate (WS reconnect, REST snapshot
  //     fetch, audio cue reset). A future refinement could expose a
  //     `fetchRestSnapshot` callback directly on useBot.
  //   * `t` → toggles the html.dark class. Mirrors what next-themes
  //     does in ThemeToggle so the keyboard shortcut + the visible
  //     toggle stay in sync (both flip the same class).
  //   * `f` → toggles browser fullscreen. Standard requestFullscreen
  //     / exitFullscreen contract.
  //   * `/` → focuses the first search input on the page (the
  //     CommandPalette isn't always mounted, so this is best-effort).
  //   * `b`/`s` → quick buy / sell — no-op in the workstation today
  //     (no global "selected market" trading flow exists yet).
  //     Logged to console so the trader sees something happened.
  //   * `Cmd+K` (meta+k) → opens the NEW KeyboardCheatSheet. The
  //     CommandPalette component exists in src/components but isn't
  //     mounted in this page (per W16-8 follow-up note); when it
  //     eventually mounts, this shortcut should be re-wired to open
  //     it instead.
  //   * Plain `k` (no modifier) — NOT in SHORTCUT_DEFINITIONS (the
  //     catalog lists only `Cmd+K` for "Open Command Palette"). But
  //     the existing UX + the W16-8 e2e tests treat plain `k` as the
  //     kill-switch shortcut. We register it as an EXTRA shortcut
  //     appended after the catalog so the hook still dispatches it,
  //     but the cheat sheet doesn't advertise it (to avoid clashing
  //     with the catalog's `Cmd+K` entry).
  const shortcutsList = useMemo<Shortcut[]>(() => {
    const fromCatalog: Shortcut[] = SHORTCUT_DEFINITIONS.map((def) => {
      switch (def.key) {
        case '1':
        case '2':
        case '3':
        case '4':
        case '5':
        case '6':
        case '7':
        case '8':
          // Digits 1-8 → nav. KB_MAP returns the NavSection for the
          // digit; setActiveSection is a stable state setter.
          return { ...def, action: () => {
            const section = KB_MAP[def.key]
            if (section) setActiveSection(section)
          } }
        case '?':
          return { ...def, action: () => setShortcutsOpen(true) }
        case 'Escape':
          return {
            ...def,
            action: () => {
              setSelectedMarket(null)
              setChartMarket(null)
              setConfigOpen(false)
              setShortcutsOpen(false)
              setConfirmKill(false)
              setConfirmCancelAll(false)
              setMobileNavOpen(false)
            },
          }
        case 'c':
          // Close selected position — only when a market is
          // selected (the trader clicked into a chart modal). With
          // no selection, the shortcut is a silent no-op.
          return { ...def, action: () => {
            const selected = chartMarket ?? selectedMarket
            if (selected) {
              void closePosition(selected.tokenId)
            }
          } }
        case 'x':
          return { ...def, action: () => setConfirmCancelAll(true) }
        case 'r':
          return { ...def, action: () => {
            if (typeof window !== 'undefined') window.location.reload()
          } }
        case 't':
          return { ...def, action: () => {
            if (typeof document === 'undefined') return
            const root = document.documentElement
            const isDark = root.classList.contains('dark')
            root.classList.toggle('dark', !isDark)
            root.classList.toggle('light', isDark)
            try {
              window.localStorage.setItem(
                'theme',
                isDark ? 'light' : 'dark',
              )
            } catch {
              // localStorage may be unavailable (private mode) — fail
              // silently; the class toggle above already flipped the
              // visible theme.
            }
          } }
        case 'f':
          return { ...def, action: () => {
            if (typeof document === 'undefined') return
            if (document.fullscreenElement) {
              void document.exitFullscreen?.()
            } else {
              void document.documentElement.requestFullscreen?.()
            }
          } }
        case '/':
          return { ...def, action: () => {
            if (typeof document === 'undefined') return
            const el = document.querySelector<HTMLInputElement>(
              'input[type="search"], input[placeholder*="search" i]',
            )
            el?.focus()
          } }
        case 'b':
        case 's':
          // Quick buy / sell — no real handler today. Logged to
          // console so the trader sees the keypress registered.
          return { ...def, action: () => {
            console.info(
              `[shortcut] ${def.key.toUpperCase()} pressed — quick ${
                def.key === 'b' ? 'buy' : 'sell'
              } requires a selected market.`,
            )
          } }
        case 'k':
          // Cmd+K → opens the new KeyboardCheatSheet (the
          // CommandPalette isn't mounted today — see W16-8 follow-up
          // note). When the CommandPalette mounts, this branch should
          // be re-wired to open it instead.
          return { ...def, action: () => setShortcutsOpen(true) }
        default:
          // Defensive: any catalog entry without an explicit case
          // becomes a no-op so the hook still matches + preventDefaults
          // (so the browser doesn't ALSO react) without throwing.
          return { ...def, action: () => {} }
      }
    })

    // EXTRA shortcuts not in the catalog. Plain `k` toggles the kill
    // switch (or resumes if already halted) — preserves the existing
    // UX where the kill-switch button is the most important single
    // key on the workstation. NOT in SHORTCUT_DEFINITIONS to avoid
    // clashing with the catalog's `Cmd+K` entry; the cheat sheet
    // just doesn't advertise plain `k`.
    const extra: Shortcut[] = [
      {
        key: 'k',
        modifiers: [],
        description: 'Toggle kill switch / resume',
        category: 'system',
        action: () => {
          if (snapshot.kill_switch) {
            void handleResumeSwitch()
          } else {
            setConfirmKill(true)
          }
        },
      },
    ]

    // Catalog shortcuts first (so the matcher checks them in
    // declaration order), then the extras. The matcher iterates in
    // array order and fires the FIRST match — for plain `k` the
    // catalog entry `Cmd+K` won't match (different modifier set), so
    // the extra `k` (no modifiers) wins.
    return [...fromCatalog, ...extra]
  }, [
    chartMarket,
    selectedMarket,
    snapshot.kill_switch,
    closePosition,
    handleResumeSwitch,
  ])

  useKeyboardShortcuts(shortcutsList)

  const handleCancelAll = useCallback(async () => {
    setActionLoading(true)
    await cancelAllOrders()
    setActionLoading(false)
    setConfirmCancelAll(false)
  }, [cancelAllOrders])

  // W9-6 — stable callbacks passed to memoized child panels. Without
  // useCallback these lambdas would be new function references on every
  // parent render, which would bypass React.memo on PositionsPanel /
  // MarketsPanel / OrdersPanel entirely (their custom comparators compare
  // callback identity). Keeping them stable ensures those panels skip
  // re-rendering when the underlying props haven't actually changed.
  const handleSelectMarketForChart = useCallback((tokenId: string, slug: string) => {
    setChartMarket({ tokenId, slug })
  }, [])

  const handleSelectPositionForChart = useCallback((market: { tokenId: string; slug: string }) => {
    setChartMarket(market)
  }, [])

  const handleOpenCancelAllDialog = useCallback(() => {
    setConfirmCancelAll(true)
  }, [])

  const isKilled = snapshot.kill_switch
  const isObserving = snapshot.observation_only
  const openOrderCount = snapshot.open_orders?.length ?? 0

  if (!mounted) {
    return (
      <div style={{ minHeight: '100vh', background: 'var(--bg-base, #0b0e14)', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <div style={{ fontFamily: 'JetBrains Mono, monospace', color: 'var(--text-secondary, #8b949e)', fontSize: '13px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span className="status-dot connecting" aria-hidden="true" />
          Initializing Polymarket Pro Workstation…
        </div>
      </div>
    )
  }

  return (
    <div className="app-shell" style={{ flexDirection: 'column' }}>
      {/* ── Kill switch / Observation banners ────────────────────────── */}
      {isKilled && (
        <div
          className="kill-switch-banner"
          role="alert"
          aria-live="assertive"
        >
          <span aria-hidden="true">🛑</span>
          KILL SWITCH ACTIVE — All trading halted.
          <button
            onClick={handleResumeSwitch}
            className="btn btn-resume btn-sm"
            style={{ marginLeft: '12px' }}
            aria-label="Resume trading — deactivate kill switch"
          >
            ▶ Resume
          </button>
        </div>
      )}
      {isObserving && !isKilled && (
        <div
          className="observation-banner"
          role="status"
          aria-live="polite"
        >
          <span aria-hidden="true">👁</span>
          OBSERVATION-ONLY MODE — New live orders disabled
          {snapshot.observation_reason ? ` (${snapshot.observation_reason})` : ' — exposure not reconciled'}
        </div>
      )}

      {/* ── App shell: sidebar + main ─────────────────────────────────── */}
      <div className="app-shell" style={{ flex: 1, minHeight: 0 }}>
        <Sidebar
          active={activeSection}
          onChange={setActiveSection}
          mobileOpen={mobileNavOpen}
          onMobileClose={() => setMobileNavOpen(false)}
        />

        <main id="main" className="main-content" role="main">
          {/* Top status bar */}
          <TopStatusBar
            snapshot={snapshot}
            status={status}
            uptime={uptime}
            onKillSwitch={() => setConfirmKill(true)}
            onResumeSwitch={handleResumeSwitch}
            onCancelAll={() => setConfirmCancelAll(true)}
            onOpenShortcuts={() => setShortcutsOpen(true)}
            onToggleMute={audio.toggleMute}
            muted={audio.muted}
            onOpenConfig={() => setConfigOpen(true)}
            onMobileNav={() => setMobileNavOpen(true)}
          />

          {/* ── Page content ─────────────────────────────────────────── */}
          <div className="page-area" aria-live="polite" aria-atomic="false">
            {/* W10-8 — AnimatePresence (mode="wait") holds the outgoing panel
                in the DOM until its fade-out (200ms) completes, then mounts
                the incoming panel which fades in. The `key={activeSection}`
                on FadeIn is what AnimatePresence uses to detect the swap —
                without a key change, no exit/enter animation fires. */}
            <AnimatePresence mode="wait">
              <FadeIn key={activeSection}>

            {/* ── 1. Command Center ──────────────────────────────────── */}
            {activeSection === 'command' && (
              <PanelErrorBoundary label="Command Center">
              <div className="command-center-layout">
                <div style={{ gridArea: 'risk', minHeight: 0 }}>
                  <RiskStatusPanel />
                </div>
                <div style={{ gridArea: 'market', minHeight: 0, overflow: 'hidden' }}>
                  <MarketsPanel
                    books={snapshot.order_books}
                    onSelectMarket={handleSelectMarketForChart}
                    priceFlashes={priceFlashes}
                    showPriceFlashes={preferences.showPriceFlashes}
                  />
                </div>
                <div style={{ gridArea: 'pos', minHeight: 0, overflow: 'hidden' }}>
                  <PositionsPanel
                    positions={snapshot.positions}
                    dailyPnl={snapshot.daily_pnl}
                    onSelectMarket={handleSelectPositionForChart}
                    onClosePosition={closePosition}
                    priceFlashes={priceFlashes}
                    showUnrealizedPnl={preferences.showUnrealizedPnl}
                    showPriceFlashes={preferences.showPriceFlashes}
                    isRealtime={wsConnected}
                  />
                </div>
                <div style={{ gridArea: 'orders', minHeight: 0, overflow: 'hidden' }}>
                  <OrdersPanel
                    orders={snapshot.open_orders}
                    onCancel={cancelOrder}
                    onCancelAll={handleOpenCancelAllDialog}
                    isRealtime={wsConnected}
                  />
                </div>
                <div style={{ gridArea: 'events', minHeight: 0, overflow: 'hidden' }}>
                  <EventLog events={snapshot.events} />
                </div>
                <div
                  style={{
                    gridArea: 'sidebar',
                    minHeight: 0,
                    overflow: 'auto',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '10px',
                  }}
                  className="scrollbar-thin"
                >
                  <EquityCurve />
                  <AnalyticsPanel />
                  <MLPanel snapshotMl={snapshot?.ml} />
                </div>
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 2. Markets — Live Books ─────────────────────────────── */}
            {activeSection === 'markets-books' && (
              <PanelErrorBoundary label="Live Order Books">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <MarketsPanel
                  books={snapshot.order_books}
                  onSelectMarket={handleSelectMarketForChart}
                  priceFlashes={priceFlashes}
                  showPriceFlashes={preferences.showPriceFlashes}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 3. Markets — Screener ──────────────────────────────── */}
            {activeSection === 'markets-screener' && (
              <PanelErrorBoundary label="Market Screener">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <MarketScreener
                  onSelectMarket={handleSelectMarketForChart}
                  onQuickTrade={(tokenId, slug) => setSelectedMarket({ tokenId, slug })}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── W17-2 — Markets — Order Flow ─────────────────────── */}
            {activeSection === 'markets-order-flow' && (
              <PanelErrorBoundary label="Order Flow">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                {/* W17-2 — Order flow workstation. Receives the parent's
                    useBot snapshot (trades + order_books + wsConnected
                    flag) so it doesn't open a duplicate WS socket; polls
                    /api/depth/{token_id} internally for the imbalance
                    meter's per-level ladder. */}
                <OrderFlowPanel
                  trades={snapshot.recent_trades}
                  orderBooks={snapshot.order_books}
                  isRealtime={wsConnected}
                  onSelectMarket={handleSelectMarketForChart}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 4. Portfolio — Positions ───────────────────────────── */}
            {activeSection === 'portfolio-positions' && (
              <PanelErrorBoundary label="Positions">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <PositionsPanel
                  positions={snapshot.positions}
                  dailyPnl={snapshot.daily_pnl}
                  onSelectMarket={handleSelectPositionForChart}
                  onClosePosition={closePosition}
                  priceFlashes={priceFlashes}
                  showUnrealizedPnl={preferences.showUnrealizedPnl}
                  showPriceFlashes={preferences.showPriceFlashes}
                  isRealtime={wsConnected}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Portfolio — Orders ─────────────────────────────────── */}
            {activeSection === 'portfolio-orders' && (
              <PanelErrorBoundary label="Open Orders">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <OrdersPanel
                  orders={snapshot.open_orders}
                  onCancel={cancelOrder}
                  onCancelAll={handleOpenCancelAllDialog}
                  isRealtime={wsConnected}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Portfolio — Trades ─────────────────────────────────── */}
            {activeSection === 'portfolio-trades' && (
              <PanelErrorBoundary label="Recent Trades">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <TradesPanel trades={snapshot.recent_trades} />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 5. Strategies — Registry ──────────────────────────── */}
            {activeSection === 'strategies-registry' && (
              <PanelErrorBoundary label="Strategy Registry">
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden' }}>
                  <StrategyMatrix />
                </div>
                <div style={{ overflow: 'auto' }} className="scrollbar-thin">
                  <LeaderboardPanel />
                </div>
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 6. Strategies — Arbitrage ─────────────────────────── */}
            {activeSection === 'strategies-arbitrage' && (
              <PanelErrorBoundary label="Arbitrage Matrix">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <ArbitrageMatrixView onSelectMarket={(m) => setChartMarket(m)} />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 6b. Strategies — Performance (W23-5) ──────────────────── */}
            {activeSection === 'strategies-performance' && (
              <PanelErrorBoundary label="Strategy Performance">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <StrategyPerformancePanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 7. Intelligence — Deep Analysis ───────────────────── */}
            {activeSection === 'intelligence-analysis' && (
              <PanelErrorBoundary label="Deep Analysis">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                {/* W13 — One-click Trade button on each "Top Alpha Opportunities" row
                    mounts the DepthChartModal (depth book + trade ticket) for that
                    market. Mirrors the MarketsPanel onSelectMarket wiring pattern. */}
                <DeepAnalysisView
                  onOpenChart={(m) => setChartMarket(m)}
                  onSelectMarket={(tokenId, slug) => setSelectedMarket({ tokenId, slug })}
                />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Intelligence — AI/ML Engine ────────────────────────── */}
            {activeSection === 'intelligence-aiml' && (
              <PanelErrorBoundary label="AI / ML Engine">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <AIMLCommandCenter />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Intelligence — Copilot ─────────────────────────────── */}
            {activeSection === 'intelligence-copilot' && (
              <PanelErrorBoundary label="AI Copilot">
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden' }}>
                  <AICopilotPanel onSelectMarket={(m) => setChartMarket(m)} />
                </div>
                <div style={{ overflow: 'auto', display: 'flex', flexDirection: 'column', gap: '10px' }} className="scrollbar-thin">
                  <EquityCurve />
                  <MLPanel snapshotMl={snapshot?.ml} />
                </div>
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Intelligence — Shadow Inference (W8-10) ───────────── */}
            {activeSection === 'intelligence-shadow' && (
              <PanelErrorBoundary label="Shadow Inference">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <ShadowInferencePanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Intelligence — ML Validation (W8-10) ───────────────── */}
            {activeSection === 'intelligence-validation' && (
              <PanelErrorBoundary label="ML Validation">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <MLValidationPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── 8. Analytics — Performance ────────────────────────── */}
            {activeSection === 'analytics-performance' && (
              <PanelErrorBoundary label="Performance Analytics">
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  <EquityCurve />
                  <AnalyticsPanel />
                </div>
                <div style={{ overflow: 'auto' }} className="scrollbar-thin">
                  <LeaderboardPanel />
                </div>
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Analytics — Performance Report (W26-2) ─────────────── */}
            {activeSection === 'analytics-performance-report' && (
              <PanelErrorBoundary label="Performance Report">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <PerformanceReportPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Analytics — Backtest Lab ───────────────────────────── */}
            {activeSection === 'analytics-backtest' && (
              <PanelErrorBoundary label="Backtest Lab">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <BacktestLabView />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Analytics — Attribution (W8-10) ─────────────────────── */}
            {activeSection === 'analytics-attribution' && (
              <PanelErrorBoundary label="Attribution">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <AttributionPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Analytics — Execution Quality (W8-10) ──────────────── */}
            {activeSection === 'analytics-execution' && (
              <PanelErrorBoundary label="Execution Quality">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <ExecutionQualityPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Analytics — Closed Positions (W8-10) ───────────────── */}
            {activeSection === 'analytics-closed' && (
              <PanelErrorBoundary label="Closed Positions">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <ClosedPositionsPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── Capital — Allocator (W8-10) ────────────────────────── */}
            {activeSection === 'capital-allocator' && (
              <PanelErrorBoundary label="Capital Allocator">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <CapitalAllocatorPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Health ────────────────────────────────────── */}
            {activeSection === 'system-health' && (
              <PanelErrorBoundary label="System Health">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <SystemHealthView />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Data Explorer ─────────────────────────────── */}
            {activeSection === 'system-database' && (
              <PanelErrorBoundary label="Database Explorer">
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <DatabaseExplorerView />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Database Status (W21-7) ──────────────────── */}
            {activeSection === 'system-database-status' && (
              <PanelErrorBoundary label="Database Status">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <DatabaseStatusPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Observability (W8-10) ─────────────────────── */}
            {activeSection === 'system-observability' && (
              <PanelErrorBoundary label="Observability">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <ObservabilityPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Retention (W8-10) ────────────────────────── */}
            {activeSection === 'system-retention' && (
              <PanelErrorBoundary label="Retention">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <RetentionPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Decision Ledger (W8-10) ──────────────────── */}
            {activeSection === 'system-decisions' && (
              <PanelErrorBoundary label="Decision Ledger">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <DecisionLedgerPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Live Safety Gate (W8-10) ─────────────────── */}
            {activeSection === 'system-safety' && (
              <PanelErrorBoundary label="Live Safety Gate">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <LiveSafetyGatePanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Audit Log (W14-4) ─────────────────────────── */}
            {activeSection === 'system-audit' && (
              <PanelErrorBoundary label="Audit Log">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <AuditLogPanel />
              </div>
              </PanelErrorBoundary>
            )}

            {/* ── System — Rate Limits (W14-7) ───────────────────────── */}
            {activeSection === 'system-rate-limit' && (
              <PanelErrorBoundary label="Rate Limits">
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <RateLimitPanel />
              </div>
              </PanelErrorBoundary>
            )}
              </FadeIn>
            </AnimatePresence>
          </div>
        </main>
      </div>

      {/* ── Modals ──────────────────────────────────────────────────────── */}
      {chartMarket && (
        <MarketChartModal
          tokenId={chartMarket.tokenId}
          slug={chartMarket.slug}
          onClose={() => setChartMarket(null)}
          onOrderPlaced={() => audio.playOrderPlaced()}
        />
      )}
      {selectedMarket && (
        <DepthChartModal
          tokenId={selectedMarket.tokenId}
          slug={selectedMarket.slug}
          onClose={() => setSelectedMarket(null)}
          onOrderPlaced={() => audio.playOrderPlaced()}
        />
      )}
      <StrategyConfigModal isOpen={configOpen} onClose={() => setConfigOpen(false)} />
      {/* W17-6 — New full-screen KeyboardCheatSheet (search + practice
          mode + JSON export). The legacy ShortcutsModal is no longer
          rendered — KeyboardCheatSheet is a strict superset. */}
      <KeyboardCheatSheet
        isOpen={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />
      {/* W17-6 — Floating "?" hint button. Sits in the bottom-right
          corner so the trader can always reach the cheat sheet even
          when the TopStatusBar is scrolled out of view. */}
      <ShortcutHint onOpen={() => setShortcutsOpen(true)} />

      {/* ── Confirmation dialogs ─────────────────────────────────────────── */}
      <ConfirmationDialog
        open={confirmKill}
        severity="danger"
        title="Activate Kill Switch"
        description="This will immediately halt all strategy execution and prevent any new orders from being placed in paper mode."
        impact="All running strategies will stop. Existing open orders will remain until manually cancelled."
        confirmLabel="🛑 Halt All Trading"
        cancelLabel="Cancel"
        onConfirm={handleKillSwitch}
        onCancel={() => setConfirmKill(false)}
        loading={actionLoading}
      />
      <ConfirmationDialog
        open={confirmCancelAll}
        severity="warning"
        title="Cancel All Open Orders"
        description={`This will cancel all ${openOrderCount} currently open order${openOrderCount !== 1 ? 's' : ''}. This action cannot be undone.`}
        impact={openOrderCount > 0
          ? `${openOrderCount} open order${openOrderCount !== 1 ? 's' : ''} will be cancelled immediately.`
          : 'No open orders to cancel.'}
        confirmLabel={`Cancel ${openOrderCount} Order${openOrderCount !== 1 ? 's' : ''}`}
        cancelLabel="Go Back"
        onConfirm={handleCancelAll}
        onCancel={() => setConfirmCancelAll(false)}
        loading={actionLoading}
      />

      {/* ── Disconnected overlay ─────────────────────────────────────────── */}
      {(status === 'disconnected' || status === 'error') && snapshot.order_books.length === 0 && (
        <div
          className="modal-backdrop"
          role="alertdialog"
          aria-labelledby="disconnect-title"
          aria-describedby="disconnect-desc"
        >
          <div className="modal" style={{ maxWidth: '360px', textAlign: 'center' }}>
            <div className="modal-body" style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: '16px', padding: '32px 24px' }}>
              <div style={{
                width: '48px', height: '48px', borderRadius: '50%',
                border: '2px solid var(--border)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                fontSize: '22px',
              }}>
                {status === 'error' ? '⚠' : '⏳'}
              </div>
              <h2 id="disconnect-title" style={{ fontSize: '15px', fontWeight: 700, color: 'var(--text-primary)' }}>
                {status === 'error' ? 'Connection Error' : 'Connecting to API'}
              </h2>
              <p id="disconnect-desc" style={{ fontSize: '12.5px', color: 'var(--text-secondary)', lineHeight: 1.6 }}>
                {status === 'error'
                  ? 'Could not reach the bot API. Check that the backend service is running.'
                  : 'Establishing connection to the Polymarket bot API…'}
              </p>
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '11.5px', color: 'var(--color-amber-fg)' }}>
                <span className="status-dot connecting" aria-hidden="true" />
                {status === 'error' ? 'Retrying…' : 'Fetching live markets…'}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
