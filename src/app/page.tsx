// app/page.tsx — Polymarket Pro Trading Workstation
'use client'

import { useEffect, useState, useCallback, useRef } from 'react'
import { useBot } from '@/hooks/useBot'
import { useAudio } from '@/hooks/useAudio'
import Sidebar, { NavSection } from '@/components/Sidebar'
import TopStatusBar from '@/components/TopStatusBar'
import ConfirmationDialog from '@/components/ConfirmationDialog'

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

        <main className="main-content" role="main">
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
          <div className="page-area">

            {/* ── 1. Command Center ──────────────────────────────────── */}
            {activeSection === 'command' && (
              <div className="command-center-layout">
                <div style={{ gridArea: 'risk', minHeight: 0 }}>
                  <RiskStatusPanel />
                </div>
                <div style={{ gridArea: 'market', minHeight: 0, overflow: 'hidden' }}>
                  <MarketsPanel
                    books={snapshot.order_books}
                    onSelectMarket={(tokenId, slug) => setChartMarket({ tokenId, slug })}
                    priceFlashes={priceFlashes}
                  />
                </div>
                <div style={{ gridArea: 'pos', minHeight: 0, overflow: 'hidden' }}>
                  <PositionsPanel
                    positions={snapshot.positions}
                    dailyPnl={snapshot.daily_pnl}
                    onSelectMarket={(m) => setChartMarket(m)}
                    onClosePosition={closePosition}
                    priceFlashes={priceFlashes}
                  />
                </div>
                <div style={{ gridArea: 'orders', minHeight: 0, overflow: 'hidden' }}>
                  <OrdersPanel
                    orders={snapshot.open_orders}
                    onCancel={cancelOrder}
                    onCancelAll={() => setConfirmCancelAll(true)}
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
            )}

            {/* ── 2. Markets — Live Books ─────────────────────────────── */}
            {activeSection === 'markets-books' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <MarketsPanel
                  books={snapshot.order_books}
                  onSelectMarket={(tokenId, slug) => setChartMarket({ tokenId, slug })}
                  priceFlashes={priceFlashes}
                />
              </div>
            )}

            {/* ── 3. Markets — Screener ──────────────────────────────── */}
            {activeSection === 'markets-screener' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <MarketScreener
                  onSelectMarket={(tokenId, slug) => setChartMarket({ tokenId, slug })}
                  onQuickTrade={(tokenId, slug) => setSelectedMarket({ tokenId, slug })}
                />
              </div>
            )}

            {/* ── 4. Portfolio — Positions ───────────────────────────── */}
            {activeSection === 'portfolio-positions' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <PositionsPanel
                  positions={snapshot.positions}
                  dailyPnl={snapshot.daily_pnl}
                  onSelectMarket={(m) => setChartMarket(m)}
                  onClosePosition={closePosition}
                  priceFlashes={priceFlashes}
                />
              </div>
            )}

            {/* ── Portfolio — Orders ─────────────────────────────────── */}
            {activeSection === 'portfolio-orders' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <OrdersPanel
                  orders={snapshot.open_orders}
                  onCancel={cancelOrder}
                  onCancelAll={() => setConfirmCancelAll(true)}
                />
              </div>
            )}

            {/* ── Portfolio — Trades ─────────────────────────────────── */}
            {activeSection === 'portfolio-trades' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <TradesPanel trades={snapshot.recent_trades} />
              </div>
            )}

            {/* ── 5. Strategies — Registry ──────────────────────────── */}
            {activeSection === 'strategies-registry' && (
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden' }}>
                  <StrategyMatrix />
                </div>
                <div style={{ overflow: 'auto' }} className="scrollbar-thin">
                  <LeaderboardPanel />
                </div>
              </div>
            )}

            {/* ── 6. Strategies — Arbitrage ─────────────────────────── */}
            {activeSection === 'strategies-arbitrage' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <ArbitrageMatrixView onSelectMarket={(m) => setChartMarket(m)} />
              </div>
            )}

            {/* ── 7. Intelligence — Deep Analysis ───────────────────── */}
            {activeSection === 'intelligence-analysis' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                {/* W13 — One-click Trade button on each "Top Alpha Opportunities" row
                    mounts the DepthChartModal (depth book + trade ticket) for that
                    market. Mirrors the MarketsPanel onSelectMarket wiring pattern. */}
                <DeepAnalysisView
                  onOpenChart={(m) => setChartMarket(m)}
                  onSelectMarket={(tokenId, slug) => setSelectedMarket({ tokenId, slug })}
                />
              </div>
            )}

            {/* ── Intelligence — AI/ML Engine ────────────────────────── */}
            {activeSection === 'intelligence-aiml' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <AIMLCommandCenter />
              </div>
            )}

            {/* ── Intelligence — Copilot ─────────────────────────────── */}
            {activeSection === 'intelligence-copilot' && (
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden' }}>
                  <AICopilotPanel onSelectMarket={(m) => setChartMarket(m)} />
                </div>
                <div style={{ overflow: 'auto', display: 'flex', flexDirection: 'column', gap: '10px' }} className="scrollbar-thin">
                  <EquityCurve />
                  <MLPanel snapshotMl={snapshot?.ml} />
                </div>
              </div>
            )}

            {/* ── 8. Analytics — Performance ────────────────────────── */}
            {activeSection === 'analytics-performance' && (
              <div className="workstation-split-layout">
                <div style={{ overflow: 'hidden', display: 'flex', flexDirection: 'column', gap: '10px' }}>
                  <EquityCurve />
                  <AnalyticsPanel />
                </div>
                <div style={{ overflow: 'auto' }} className="scrollbar-thin">
                  <LeaderboardPanel />
                </div>
              </div>
            )}

            {/* ── Analytics — Backtest Lab ───────────────────────────── */}
            {activeSection === 'analytics-backtest' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <BacktestLabView />
              </div>
            )}

            {/* ── System — Health ────────────────────────────────────── */}
            {activeSection === 'system-health' && (
              <div style={{ height: '100%', overflow: 'auto' }} className="scrollbar-thin">
                <SystemHealthView />
              </div>
            )}

            {/* ── System — Data Explorer ─────────────────────────────── */}
            {activeSection === 'system-database' && (
              <div style={{ height: '100%', overflow: 'hidden' }}>
                <DatabaseExplorerView />
              </div>
            )}
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
