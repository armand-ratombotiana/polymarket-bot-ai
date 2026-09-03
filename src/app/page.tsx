// app/page.tsx — Polymarket Pro Trading Workstation
'use client'

import { useEffect, useState, useCallback, useRef, type ComponentType } from 'react'
import { useBot } from '@/hooks/useBot'
import { useAudio } from '@/hooks/useAudio'
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

// Portfolio
import PositionsPanel from '@/components/PositionsPanel'
import OrdersPanel from '@/components/OrdersPanel'
import TradesPanel from '@/components/TradesPanel'

// Strategies
import StrategyMatrix from '@/components/StrategyMatrix'
import ArbitrageMatrixView from '@/components/ArbitrageMatrixView'

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

// Capital — Wave 8
const CapitalAllocatorPanel = lazyPanel(() => import('@/components/CapitalAllocatorPanel'), 'Loading Capital Allocator…')

// System — Wave 8
const ObservabilityPanel = lazyPanel(() => import('@/components/ObservabilityPanel'), 'Loading Observability…')
const RetentionPanel = lazyPanel(() => import('@/components/RetentionPanel'), 'Loading Retention…')
const DecisionLedgerPanel = lazyPanel(() => import('@/components/DecisionLedgerPanel'), 'Loading Decision Ledger…')
const LiveSafetyGatePanel = lazyPanel(() => import('@/components/LiveSafetyGatePanel'), 'Loading Safety Gate…')

// Modals
import DepthChartModal from '@/components/DepthChartModal'
import MarketChartModal from '@/components/MarketChartModal'
import StrategyConfigModal from '@/components/StrategyConfigModal'
import ShortcutsModal from '@/components/ShortcutsModal'

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

export default function Dashboard() {
  const [mounted, setMounted] = useState(false)
  const { snapshot, status, priceFlashes, activateKillSwitch, deactivateKillSwitch, cancelAllOrders, cancelOrder, closePosition } = useBot()
  const audio = useAudio()

  const [uptime, setUptime] = useState(0)
  const [startTime] = useState(() => Date.now())
  const [activeSection, setActiveSection] = useState<NavSection>('command')
  const [mobileNavOpen, setMobileNavOpen] = useState(false)

  // Modal states
  const [selectedMarket, setSelectedMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [chartMarket, setChartMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
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

  // Global keyboard shortcuts
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      if (e.metaKey || e.ctrlKey || e.altKey) return

      if (KB_MAP[e.key]) {
        setActiveSection(KB_MAP[e.key])
      } else if (e.key === '?') {
        setShortcutsOpen(p => !p)
      } else if (e.key === 'c' || e.key === 'C') {
        setConfigOpen(p => !p)
      } else if (e.key === 'k' || e.key === 'K') {
        if (snapshot.kill_switch) {
          handleResumeSwitch()
        } else {
          setConfirmKill(true)
        }
      } else if (e.key === 'Escape') {
        setSelectedMarket(null)
        setChartMarket(null)
        setConfigOpen(false)
        setShortcutsOpen(false)
        setConfirmKill(false)
        setConfirmCancelAll(false)
        setMobileNavOpen(false)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [snapshot.kill_switch, handleResumeSwitch])

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
                  />
                </div>
                <div style={{ gridArea: 'pos', minHeight: 0, overflow: 'hidden' }}>
                  <PositionsPanel
                    positions={snapshot.positions}
                    dailyPnl={snapshot.daily_pnl}
                    onSelectMarket={handleSelectPositionForChart}
                    onClosePosition={closePosition}
                    priceFlashes={priceFlashes}
                  />
                </div>
                <div style={{ gridArea: 'orders', minHeight: 0, overflow: 'hidden' }}>
                  <OrdersPanel
                    orders={snapshot.open_orders}
                    onCancel={cancelOrder}
                    onCancelAll={handleOpenCancelAllDialog}
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
      <ShortcutsModal isOpen={shortcutsOpen} onClose={() => setShortcutsOpen(false)} />

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
