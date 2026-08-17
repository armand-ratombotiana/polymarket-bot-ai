// app/page.tsx — Polymarket Pro Enterprise Workstation
'use client'

import { useEffect, useState } from 'react'
import { useBot } from '@/hooks/useBot'
import { useAudio } from '@/hooks/useAudio'
import Header, { ActiveTab } from '@/components/Header'
import MarketsPanel from '@/components/MarketsPanel'
import PositionsPanel from '@/components/PositionsPanel'
import OrdersPanel from '@/components/OrdersPanel'
import TradesPanel from '@/components/TradesPanel'
import EventLog from '@/components/EventLog'
import MLPanel from '@/components/MLPanel'
import AnalyticsPanel from '@/components/AnalyticsPanel'
import EquityCurve from '@/components/EquityCurve'
import RiskStatusPanel from '@/components/RiskStatusPanel'
import LeaderboardPanel from '@/components/LeaderboardPanel'
import StrategyMatrix from '@/components/StrategyMatrix'
import AIMLCommandCenter from '@/components/AIMLCommandCenter'
import DeepAnalysisView from '@/components/DeepAnalysisView'
import ArbitrageMatrixView from '@/components/ArbitrageMatrixView'
import DatabaseExplorerView from '@/components/DatabaseExplorerView'
import BacktestLabView from '@/components/BacktestLabView'
import SystemHealthView from '@/components/SystemHealthView'
import AICopilotPanel from '@/components/AICopilotPanel'
import MarketScreener from '@/components/MarketScreener'
import DepthChartModal from '@/components/DepthChartModal'
import MarketChartModal from '@/components/MarketChartModal'
import StrategyConfigModal from '@/components/StrategyConfigModal'
import ShortcutsModal from '@/components/ShortcutsModal'

const TABS: ActiveTab[] = ['terminal', 'arbitrage', 'strategies', 'aiml', 'analysis', 'database', 'backtest', 'copilot', 'screener', 'health']

export default function Dashboard() {
  const { snapshot, status, activateKillSwitch, deactivateKillSwitch, cancelAllOrders, cancelOrder } = useBot()
  const audio = useAudio()
  const [uptime, setUptime] = useState(0)
  const [startTime] = useState(Date.now())
  const [activeTab, setActiveTab] = useState<ActiveTab>('terminal')

  // Modal States
  const [selectedMarket, setSelectedMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [chartMarket, setChartMarket] = useState<{ tokenId: string; slug: string } | null>(null)
  const [configOpen, setConfigOpen] = useState(false)
  const [shortcutsOpen, setShortcutsOpen] = useState(false)

  useEffect(() => {
    const t = setInterval(() => setUptime(Math.floor((Date.now() - startTime) / 1000)), 1000)
    return () => clearInterval(t)
  }, [startTime])

  // Global Keyboard Shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return

      if (e.key >= '1' && e.key <= '8') {
        const idx = parseInt(e.key) - 1
        if (TABS[idx]) setActiveTab(TABS[idx])
      } else if (e.key === '?') {
        setShortcutsOpen((prev) => !prev)
      } else if (e.key === 'c' || e.key === 'C') {
        setConfigOpen((prev) => !prev)
      } else if (e.key === 'k' || e.key === 'K') {
        if (snapshot.kill_switch) {
          deactivateKillSwitch()
        } else {
          activateKillSwitch()
          audio.playKillSwitch()
        }
      } else if (e.key === 'Escape') {
        setSelectedMarket(null)
        setChartMarket(null)
        setConfigOpen(false)
        setShortcutsOpen(false)
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [snapshot.kill_switch])

  const isKilled = snapshot.kill_switch
  const isObserving = snapshot.observation_only

  return (
    <div className="flex flex-col h-screen overflow-hidden relative bg-[#0b0c10]">
      {isKilled && (
        <div className="bg-red-600/90 text-white text-center text-xs font-semibold py-1.5 tracking-wide animate-pulse z-50">
          🛑 KILL SWITCH ACTIVE — All trading halted. Click Resume to re-enable.
        </div>
      )}
      {isObserving && (
        <div className="bg-amber-500/90 text-black text-center text-xs font-semibold py-1.5 tracking-wide z-50">
          👁 OBSERVATION-ONLY MODE — New live orders disabled until exposure is reconciled
          {snapshot.observation_reason ? ` (${snapshot.observation_reason})` : ''}
        </div>
      )}
      <Header
        activeTab={activeTab}
        onTabChange={setActiveTab}
        mode={snapshot.mode}
        killSwitch={isKilled}
        dailyPnl={snapshot.daily_pnl}
        paperBalance={snapshot.paper_balance}
        strategies={snapshot.strategies}
        status={status}
        onKillSwitch={() => {
          activateKillSwitch()
          audio.playKillSwitch()
        }}
        onDeactivate={deactivateKillSwitch}
        onCancelAll={cancelAllOrders}
        onOpenConfig={() => setConfigOpen(true)}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        muted={audio.muted}
        onToggleMute={audio.toggleMute}
        uptime={uptime}
      />

      {/* Main View Area */}
      <div className="flex-1 overflow-hidden p-3">
        {activeTab === 'terminal' && (
          <div
            className="h-full grid gap-3"
            style={{
              gridTemplateColumns: '1fr 1fr 320px',
              gridTemplateRows: '1fr 1fr',
              gridTemplateAreas: `
                "markets  positions  sidebar"
                "orders   events     sidebar"
              `,
            }}
          >
            <div style={{ gridArea: 'markets' }} className="min-h-0">
              <MarketsPanel
                books={snapshot.order_books}
                onSelectMarket={(tokenId, slug) => setChartMarket({ tokenId, slug })}
              />
            </div>

            <div style={{ gridArea: 'positions' }} className="min-h-0 flex flex-col gap-3">
              <div className="flex-1 min-h-0">
                <PositionsPanel positions={snapshot.positions} dailyPnl={snapshot.daily_pnl} />
              </div>
              <div style={{ flex: '0 0 auto', maxHeight: '42%' }} className="min-h-0">
                <TradesPanel trades={snapshot.recent_trades} />
              </div>
            </div>

            <div style={{ gridArea: 'orders' }} className="min-h-0">
              <OrdersPanel orders={snapshot.open_orders} onCancel={cancelOrder} />
            </div>

            <div style={{ gridArea: 'events' }} className="min-h-0">
              <EventLog events={snapshot.events} />
            </div>

            {/* Right sidebar: Risk Status + Equity Curve + Analytics + ML Panel */}
            <div style={{ gridArea: 'sidebar' }} className="min-h-0 overflow-auto scrollbar-thin flex flex-col gap-3">
              <RiskStatusPanel />
              <EquityCurve />
              <AnalyticsPanel />
              <MLPanel />
            </div>
          </div>
        )}

        {activeTab === 'arbitrage' && (
          <div className="h-full">
            <ArbitrageMatrixView />
          </div>
        )}

        {activeTab === 'strategies' && (
          <div className="h-full grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="md:col-span-2 h-full min-h-0">
              <StrategyMatrix />
            </div>
            <div className="h-full min-h-0 overflow-auto scrollbar-thin flex flex-col gap-3">
              <LeaderboardPanel />
            </div>
          </div>
        )}

        {activeTab === 'aiml' && (
          <div className="h-full">
            <AIMLCommandCenter />
          </div>
        )}

        {activeTab === 'analysis' && (
          <div className="h-full">
            <DeepAnalysisView />
          </div>
        )}

        {activeTab === 'database' && (
          <div className="h-full">
            <DatabaseExplorerView />
          </div>
        )}

        {activeTab === 'backtest' && (
          <div className="h-full">
            <BacktestLabView />
          </div>
        )}

        {activeTab === 'copilot' && (
          <div className="h-full grid grid-cols-1 md:grid-cols-3 gap-3">
            <div className="md:col-span-2 h-full min-h-0">
              <AICopilotPanel />
            </div>
            <div className="h-full min-h-0 flex flex-col gap-3">
              <EquityCurve />
              <MLPanel />
            </div>
          </div>
        )}

        {activeTab === 'screener' && (
          <div className="h-full">
            <MarketScreener
              onSelectMarket={(tokenId, slug) => setChartMarket({ tokenId, slug })}
              onQuickTrade={(tokenId, slug) => setSelectedMarket({ tokenId, slug })}
            />
          </div>
        )}

        {activeTab === 'health' && (
          <div className="h-full">
            <SystemHealthView />
          </div>
        )}
      </div>

      {/* Candlestick & Price History Modal */}
      {chartMarket && (
        <MarketChartModal
          tokenId={chartMarket.tokenId}
          slug={chartMarket.slug}
          onClose={() => setChartMarket(null)}
          onOrderPlaced={() => audio.playOrderPlaced()}
        />
      )}

      {/* Depth Chart & Quick Trade Modal */}
      {selectedMarket && (
        <DepthChartModal
          tokenId={selectedMarket.tokenId}
          slug={selectedMarket.slug}
          onClose={() => setSelectedMarket(null)}
          onOrderPlaced={() => audio.playOrderPlaced()}
        />
      )}

      {/* Strategy Configuration Modal */}
      <StrategyConfigModal
        isOpen={configOpen}
        onClose={() => setConfigOpen(false)}
      />

      {/* Keyboard Shortcuts Modal */}
      <ShortcutsModal
        isOpen={shortcutsOpen}
        onClose={() => setShortcutsOpen(false)}
      />

      {/* Disconnected overlay */}
      {(status === 'disconnected' || status === 'error') && snapshot.order_books.length === 0 && (
        <div className="absolute inset-0 bg-black/70 flex items-center justify-center z-40 backdrop-blur-sm">
          <div className="card p-8 flex flex-col items-center gap-4 max-w-xs text-center">
            <div className="w-12 h-12 rounded-full border-2 border-[#252836] flex items-center justify-center text-2xl">
              {status === 'error' ? '⚠' : '⏳'}
            </div>
            <h2 className="text-[15px] font-semibold text-[#e8eaf0]">
              {status === 'error' ? 'Connection Error' : 'Connecting'}
            </h2>
            <p className="text-[12px] text-[#8b91a8] leading-relaxed">
              Connecting to Polymarket Pro Bot API on port 8080…
            </p>
            <div className="flex items-center gap-1.5 text-[11px] text-amber-400">
              <span className="status-dot bg-amber-400 animate-pulse" />
              Fetching live markets…
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
