// app/page.tsx — Main Dashboard (with Strategy Controls, ML & Analytics)
'use client'

import { useEffect, useState } from 'react'
import { useBot } from '@/hooks/useBot'
import Header from '@/components/Header'
import MarketsPanel from '@/components/MarketsPanel'
import PositionsPanel from '@/components/PositionsPanel'
import OrdersPanel from '@/components/OrdersPanel'
import TradesPanel from '@/components/TradesPanel'
import EventLog from '@/components/EventLog'
import MLPanel from '@/components/MLPanel'
import AnalyticsPanel from '@/components/AnalyticsPanel'

export default function Dashboard() {
  const { snapshot, status, activateKillSwitch, deactivateKillSwitch, cancelAllOrders, cancelOrder } = useBot()
  const [uptime, setUptime] = useState(0)
  const [startTime] = useState(Date.now())

  useEffect(() => {
    const t = setInterval(() => setUptime(Math.floor((Date.now() - startTime) / 1000)), 1000)
    return () => clearInterval(t)
  }, [startTime])

  const isKilled = snapshot.kill_switch

  return (
    <div className="flex flex-col h-screen overflow-hidden relative">
      {isKilled && (
        <div className="bg-red-600/90 text-white text-center text-xs font-semibold py-1.5 tracking-wide animate-pulse z-50">
          🛑 KILL SWITCH ACTIVE — All trading halted. Click Resume to re-enable.
        </div>
      )}

      <Header
        mode={snapshot.mode}
        killSwitch={isKilled}
        dailyPnl={snapshot.daily_pnl}
        paperBalance={snapshot.paper_balance}
        strategies={snapshot.strategies}
        status={status}
        onKillSwitch={activateKillSwitch}
        onDeactivate={deactivateKillSwitch}
        onCancelAll={cancelAllOrders}
        uptime={uptime}
      />

      {/* 3-column, 2-row grid */}
      <div
        className="flex-1 overflow-hidden p-3 grid gap-3"
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
          <MarketsPanel books={snapshot.order_books} />
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

        {/* Right sidebar: Analytics + ML panels */}
        <div style={{ gridArea: 'sidebar' }} className="min-h-0 overflow-auto scrollbar-thin flex flex-col gap-3">
          <AnalyticsPanel />
          <MLPanel />
        </div>
      </div>

      {/* Disconnected overlay (only if completely no data received) */}
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
              Connecting to Polymarket Bot API on port 8080…
            </p>
            <div className="flex items-center gap-1.5 text-[11px] text-amber-400">
              <span className="status-dot bg-amber-400 animate-pulse" />
              Fetching live markets…
            </div>
          </div>
        </div>
      )}

      {status === 'connecting' && snapshot.order_books.length === 0 && (
        <div className="absolute bottom-3 right-3 z-30">
          <div className="flex items-center gap-1.5 bg-[#111318] border border-[#252836] rounded-md px-3 py-1.5 text-[11px] text-amber-400">
            <span className="status-dot bg-amber-400 animate-pulse" />
            Connecting to bot…
          </div>
        </div>
      )}
    </div>
  )
}
