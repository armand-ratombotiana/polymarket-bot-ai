// components/CommandCenterDashboard.tsx — W39-3 Redesigned Command Center
//
// Replaces the prior single-strip metrics layout with a clear, professional
// five-row trading dashboard hierarchy:
//
//   ┌───────────────────────────────────────────────────────────────────────┐
//   │ 1. System status bar (Backend · WS · Fresh · Risk · Kill · AI · TS)   │
//   ├───────────────────────────────────────────────────────────────────────┤
//   │ 2. Top bar — Balance  ·  Available  ·  Exposure      (3 large KPIs)   │
//   ├───────────────────────────────────────────────────────────────────────┤
//   │ 3. P&L row — Realized · Unrealized · Daily · Win % · DD (5 med KPIs)  │
//   ├───────────────────────────────────────────────────────────────────────┤
//   │ 4. Risk bar — Risk status · Kill switch · Max exposure used           │
//   ├───────────────────┬───────────────────┬───────────────┬───────────────┤
//   │ 5. Main grid      │                   │               │               │
//   │   Active Positions │   Order Books     │ Recent Trades │  Sidebar:     │
//   │   (left)          │   (center)         │ (right)       │  EquityCurve  │
//   │                   │                   │               │  Analytics    │
//   │                   │                   │               │  MLPanel      │
//   └───────────────────┴───────────────────┴───────────────┴───────────────┘
//
// Each KPI card uses the new <KpiCard> primitive — label (uppercase, small,
// dimmed), value (large bold tabular-nums), sub-text (trend %, timestamp,
// context), color tone (green/red/amber), loading skeleton, and stale
// indicator all live in CSS utility classes declared in globals.css.
//
// Data sources:
//   * `snapshot` prop — paper_balance, positions, daily_pnl, kill_switch
//     (driven by the parent useBot hook).
//   * `/api/status` — total_exposure, max_total_exposure, daily_loss_limit,
//     drawdown_dollars, max_drawdown_limit.
//   * `/api/analytics` — realized_pnl, unrealized_pnl, win_rate, total_trades,
//     max_drawdown_pct.
//
// The main-grid panels (MarketsPanel, PositionsPanel, TradesPanel, sidebar)
// are received as ReactNode props so the parent page.tsx retains ownership
// of the per-panel event handlers (cancel order, close position, open chart)
// and the panels' own useRealtimeData WS subscriptions stay singletons.
'use client'

import { memo, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { BotSnapshot, ConnectionStatus } from '@/hooks/useBot'
import { apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct, fmtInt } from '@/lib/design-tokens'
import { KpiCard, type KpiTone } from '@/components/KpiCard'
import CommandCenterHealthBar from '@/components/CommandCenterHealthBar'

// ── API response shapes ────────────────────────────────────────────────────
interface StatusPayload {
  total_exposure?: number
  max_total_exposure?: number
  daily_loss_limit?: number
  drawdown_dollars?: number
  max_drawdown_limit?: number
  daily_pnl?: number
  kill_switch?: boolean
  observation_only?: boolean
}

interface AnalyticsPayload {
  realized_pnl?: number
  unrealized_pnl?: number
  win_rate?: number
  total_trades?: number
  max_drawdown_dollars?: number
  max_drawdown_pct?: number
  peak_equity?: number
  sharpe_ratio?: number | null
}

// ── Polling hook ──────────────────────────────────────────────────────────
// Lightweight single-purpose poller — intentionally not useRealtimeData
// because the dashboard KPIs are read-only aggregates and we don't want
// to open a second WebSocket subscription on top of the parent useBot socket
// + the panel-level sockets. Mirrors the same hook used (but not exported)
// by CommandCenterMetricsStrip.
function usePolled<T>(
  endpoint: string | null,
  intervalMs: number,
): {
  data: T | null
  error: string | null
  loading: boolean
  fetchedAt: number | null
} {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [fetchedAt, setFetchedAt] = useState<number | null>(null)

  useEffect(() => {
    if (!endpoint) {
      setLoading(false)
      return
    }
    let cancelled = false
    const tick = async () => {
      try {
        const res = await apiFetch(endpoint)
        if (!res.ok) {
          if (!cancelled) {
            setError(`HTTP ${res.status}`)
            setLoading(false)
          }
          return
        }
        const json = (await res.json()) as T
        if (!cancelled) {
          setData(json)
          setError(null)
          setLoading(false)
          setFetchedAt(Date.now())
        }
      } catch (e) {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : 'Network error')
          setLoading(false)
        }
      }
    }
    tick()
    const t = setInterval(tick, intervalMs)
    return () => {
      cancelled = true
      clearInterval(t)
    }
  }, [endpoint, intervalMs])

  return { data, error, loading, fetchedAt }
}

// ── Helpers ────────────────────────────────────────────────────────────────
function pnlTone(v: number | null | undefined): KpiTone {
  if (v == null || !Number.isFinite(v)) return 'neutral'
  return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'
}

function staleFor(fetchedAt: number | null, threshMs = 30_000): boolean {
  if (fetchedAt == null) return false
  return Date.now() - fetchedAt > threshMs
}

// Risk-posture derivation — mirrors the CommandCenterHealthBar's logic so
// the risk bar's "Risk Status" KPI matches the system status bar's Risk
// Level indicator.
function deriveRiskStatus(
  snapshot: BotSnapshot,
  dataAgeSec: number | null,
): { label: string; tone: KpiTone; sub: string } {
  if (snapshot.kill_switch) {
    return { label: 'Critical', tone: 'negative', sub: 'Kill switch active' }
  }
  if (snapshot.observation_only) {
    return { label: 'Caution', tone: 'warning', sub: 'Observation only' }
  }
  if (snapshot.daily_pnl <= -1.0) {
    return { label: 'Caution', tone: 'warning', sub: 'Loss near stop' }
  }
  if (dataAgeSec != null && dataAgeSec > 60) {
    return { label: 'Caution', tone: 'warning', sub: 'Data stale' }
  }
  return { label: 'Normal', tone: 'positive', sub: 'Trading enabled' }
}

// ── Inline sub-component: Max Exposure card (with progress bar) ──────────
// Uses the same KpiCard shell (.kpi-card / .kpi-label / .kpi-value / .kpi-sub
// CSS classes) but layers a thin progress bar beneath the value so the
// trader can see at a glance how close we are to the configured exposure cap.
function MaxExposureCard({
  used,
  cap,
  loading,
  error,
  stale,
}: {
  used: number
  cap: number
  loading: boolean
  error: string | null
  stale: boolean
}) {
  const pct = cap > 0 ? Math.min(100, (used / cap) * 100) : 0
  const tone: KpiTone =
    pct > 90 ? 'negative' : pct > 70 ? 'warning' : 'neutral'
  const barClass =
    tone === 'negative'
      ? 'bg-red-400'
      : tone === 'warning'
      ? 'bg-amber-400'
      : 'bg-green-400'
  return (
    <div
      className="kpi-card"
      data-testid="kpi-max-exposure"
      data-kpi-id="max-exposure"
      role="group"
      aria-label="Max exposure used"
      title={`Used ${fmtUsd(used)} of ${fmtUsd(cap)} cap (${pct.toFixed(0)}%)`}
    >
      <div className="kpi-label">
        <span className="truncate">Max Exposure</span>
        {stale && !loading && !error && (
          <span className="kpi-stale-pill" aria-label="stale">
            stale
          </span>
        )}
        {error && !loading && (
          <span
            className="kpi-error-pill"
            title={error}
            aria-label="error"
          >
            err
          </span>
        )}
      </div>
      {loading ? (
        <span
          className="kpi-skeleton kpi-skeleton-md"
          role="status"
          aria-live="polite"
        />
      ) : error ? (
        <span className="kpi-value kpi-value-md kpi-tone-negative">—</span>
      ) : (
        <>
          <span
            className={`kpi-value kpi-value-md ${
              tone === 'negative'
                ? 'kpi-tone-negative'
                : tone === 'warning'
                ? 'kpi-tone-warning'
                : 'kpi-tone-neutral'
            }`}
          >
            {fmtUsd(used)}
          </span>
          <div className="kpi-sub">
            <div className="flex-1 h-1 bg-[#1f2335] rounded-full overflow-hidden min-w-[40px]">
              <div
                className={`h-full rounded-full transition-all ${barClass}`}
                style={{ width: `${pct}%` }}
              />
            </div>
            <span>
              {pct.toFixed(0)}% of {fmtUsd(cap, 0)}
            </span>
          </div>
        </>
      )}
    </div>
  )
}

// ── Props ─────────────────────────────────────────────────────────────────
export interface CommandCenterDashboardProps {
  snapshot: BotSnapshot
  status: ConnectionStatus
  wsConnected: boolean
  /** Click handler for the kill switch KPI in the risk bar. */
  onKillSwitch?: () => void
  /** Active positions panel — rendered in the main grid left column. */
  positions: ReactNode
  /** Order books panel — rendered in the main grid center column. */
  orderBooks: ReactNode
  /** Recent trades panel — rendered in the main grid right column. */
  recentTrades: ReactNode
  /** Sidebar stack — EquityCurve + Analytics + ML panel. */
  sidebar: ReactNode
}

// ── Main component ────────────────────────────────────────────────────────
function CommandCenterDashboardImpl({
  snapshot,
  status,
  wsConnected,
  onKillSwitch,
  positions,
  orderBooks,
  recentTrades,
  sidebar,
}: CommandCenterDashboardProps) {
  // ── Polled backend aggregates ──────────────────────────────────────────
  const statusData = usePolled<StatusPayload>('/api/status', 3000)
  const analytics = usePolled<AnalyticsPayload>('/api/analytics', 8000)

  // Re-render every 5s so "Xs ago" sub-labels stay fresh.
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 5000)
    return () => clearInterval(t)
  }, [])

  // ── Derived portfolio metrics ──────────────────────────────────────────
  const positionsArr = snapshot.positions ?? []
  const openOrders = snapshot.open_orders ?? []
  const trades = snapshot.recent_trades ?? []
  const strategies = snapshot.strategies ?? []

  // Mark-to-mid exposure from open positions.
  const openExposure = positionsArr.reduce((sum, p) => {
    const price =
      typeof p.current_price === 'number' ? p.current_price : p.avg_entry_price
    const shares = (p.yes_shares ?? 0) + (p.no_shares ?? 0)
    return sum + price * shares
  }, 0)

  // Realized P&L — prefer analytics' realized_pnl; fall back to sum of
  // position.realised_pnl (the snapshot's source of truth).
  const realizedPnlFromPositions = positionsArr.reduce(
    (sum, p) => sum + (p.realised_pnl ?? 0),
    0,
  )
  const realizedPnl =
    analytics.data?.realized_pnl ?? realizedPnlFromPositions ?? 0

  // Unrealized P&L — prefer analytics; fall back to per-position sum.
  const unrealizedPnlFromPositions = positionsArr.reduce(
    (sum, p) => sum + (p.unrealized_pnl ?? 0),
    0,
  )
  const unrealizedPnl =
    analytics.data?.unrealized_pnl ?? unrealizedPnlFromPositions ?? 0

  // Total portfolio value = available cash + open exposure.
  const availableBalance = snapshot.paper_balance ?? 0
  const totalPortfolioValue = availableBalance + openExposure

  // ── Risk metrics ─────────────────────────────────────────────────────────
  const totalExposure = statusData.data?.total_exposure ?? openExposure
  const maxExposure = statusData.data?.max_total_exposure ?? 25
  const expPct = maxExposure > 0 ? totalExposure / maxExposure : null
  const exposureTone: KpiTone =
    expPct != null && expPct > 0.9 ? 'warning' : 'neutral'

  const drawdownDollars =
    statusData.data?.drawdown_dollars ??
    analytics.data?.max_drawdown_dollars ??
    0
  const maxDrawdownLimit = statusData.data?.max_drawdown_limit ?? 8
  const drawdownPct = analytics.data?.max_drawdown_pct ?? 0
  const drawdownTone: KpiTone =
    Math.abs(drawdownDollars) > maxDrawdownLimit * 0.8
      ? 'negative'
      : Math.abs(drawdownDollars) > maxDrawdownLimit * 0.5
      ? 'warning'
      : 'neutral'

  const dailyPnl = snapshot.daily_pnl ?? statusData.data?.daily_pnl ?? 0
  const dailyLossLimit = statusData.data?.daily_loss_limit ?? 2
  const dailyPnlTone: KpiTone =
    dailyPnl > 0
      ? 'positive'
      : dailyPnl < 0
      ? dailyPnl <= -dailyLossLimit * 0.8
        ? 'negative'
        : 'warning'
      : 'neutral'

  // ── Risk-status derivation (mirrors health bar) ─────────────────────────
  const dataAgeSec =
    snapshot.timestamp > 0
      ? Math.max(0, Math.floor(Date.now() / 1000 - snapshot.timestamp))
      : null
  const riskStatus = deriveRiskStatus(snapshot, dataAgeSec)

  // ── Win rate (from analytics, with loading + error) ─────────────────────
  const winRate = analytics.data?.win_rate ?? null
  const winRateTone: KpiTone =
    winRate != null
      ? winRate >= 0.55
        ? 'positive'
        : winRate < 0.45
        ? 'negative'
        : 'neutral'
      : 'neutral'

  // ── Cell wrappers (consistent styling + grid-area routing) ─────────────
  const cellClass = 'min-h-0 min-w-0 overflow-hidden'
  const sidebarStyle: CSSProperties = {
    gridArea: 'sidebar',
    minHeight: 0,
    overflow: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 'var(--space-2)',
  }

  // ── Stats summary for context (also surfaces counts to screen readers) ──
  // Computed once per render; values are tiny (counts of arrays already in
  // memory) so the cost is negligible.
  const summaryCounts = useMemo(() => {
    return {
      positions: positionsArr.length,
      orders: openOrders.length,
      trades: trades.length,
      strategies: strategies.length,
    }
  }, [positionsArr, openOrders, trades, strategies])

  return (
    <div className="command-center-layout">
      {/* ── 1. System status bar (top) ───────────────────────────────── */}
      <div style={{ gridArea: 'system', minHeight: 0 }}>
        <CommandCenterHealthBar
          snapshot={snapshot}
          status={status}
          wsConnected={wsConnected}
        />
      </div>

      {/* ── 2. Top bar — 3 large hero KPIs ─────────────────────────────── */}
      <div
        style={{ gridArea: 'topbar', minHeight: 0 }}
        className="grid grid-cols-1 sm:grid-cols-3 gap-2"
        role="region"
        aria-label="Top bar — portfolio headline metrics"
      >
        <KpiCard
          id="balance"
          size="lg"
          label="Balance"
          value={fmtUsd(totalPortfolioValue)}
          tone="neutral"
          sub={`Cash ${fmtUsd(availableBalance)}`}
          title="Total portfolio value = available cash + open position market value"
          interactive
        />
        <KpiCard
          id="available"
          size="lg"
          label="Available"
          value={fmtUsd(snapshot.paper_balance)}
          tone="neutral"
          sub="Deployable cash"
          title="Free paper-trading balance"
          interactive
        />
        <KpiCard
          id="exposure"
          size="lg"
          label="Exposure"
          value={fmtUsd(openExposure)}
          tone={exposureTone}
          sub={
            expPct != null
              ? `${(expPct * 100).toFixed(0)}% of $${maxExposure.toFixed(0)} cap`
              : '—'
          }
          title="Mark-to-mid value of open positions vs configured exposure cap"
          interactive
        />
      </div>

      {/* ── 3. P&L row — 5 medium KPIs ─────────────────────────────────── */}
      <div
        style={{ gridArea: 'pnl', minHeight: 0 }}
        className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2"
        role="region"
        aria-label="P&L row — realized, unrealized, daily, win rate, drawdown"
      >
        <KpiCard
          id="realized-pnl"
          label="Realized P&L"
          value={fmtPnl(realizedPnl)}
          tone={pnlTone(realizedPnl)}
          sub="Closed today"
          loading={analytics.loading && !analytics.data}
          error={analytics.error}
          stale={staleFor(analytics.fetchedAt)}
          title="Sum of closed-position realized P&L"
        />
        <KpiCard
          id="unrealized-pnl"
          label="Unrealized P&L"
          value={fmtPnl(unrealizedPnl)}
          tone={pnlTone(unrealizedPnl)}
          sub="Mark-to-mid open"
          loading={analytics.loading && !analytics.data}
          error={analytics.error}
          stale={staleFor(analytics.fetchedAt)}
          title="Sum of open positions' unrealized P&L"
        />
        <KpiCard
          id="daily-pnl"
          label="Daily P&L"
          value={fmtPnl(dailyPnl)}
          tone={dailyPnlTone}
          sub={`Stop −$${Math.abs(dailyLossLimit).toFixed(2)}`}
          title="Intraday realized + unrealized P&L vs the configured daily loss stop"
        />
        <KpiCard
          id="win-rate"
          label="Win Rate"
          value={winRate != null ? fmtPct(winRate) : null}
          tone={winRateTone}
          sub={
            analytics.data?.total_trades != null
              ? `n=${fmtInt(analytics.data.total_trades)}`
              : '—'
          }
          loading={analytics.loading && !analytics.data}
          error={analytics.error}
          stale={staleFor(analytics.fetchedAt)}
          title="Share of closed trades that ended in profit"
        />
        <KpiCard
          id="drawdown"
          label="Drawdown"
          value={`−$${Math.abs(drawdownDollars).toFixed(2)}`}
          tone={drawdownTone}
          sub={fmtPct(drawdownPct)}
          loading={statusData.loading && !statusData.data}
          error={statusData.error}
          stale={staleFor(statusData.fetchedAt)}
          title="Current drawdown from peak equity vs hard stop"
        />
      </div>

      {/* ── 4. Risk bar — 3 KPIs (risk status, kill switch, max exposure) */}
      <div
        style={{ gridArea: 'risk', minHeight: 0 }}
        className="grid grid-cols-1 sm:grid-cols-3 gap-2"
        role="region"
        aria-label="Risk bar — status, kill switch, max exposure"
      >
        <KpiCard
          id="risk-status"
          label="Risk Status"
          value={riskStatus.label}
          tone={riskStatus.tone}
          sub={riskStatus.sub}
          title="Composite risk posture derived from kill switch, observation mode, daily P&L and data freshness"
        />
        {/* Kill switch is interactive — when onKillSwitch is provided, the
            card becomes a clickable affordance to toggle the switch. We wrap
            the KpiCard in a <button> so the semantics + keyboard nav are
            preserved without leaking interactivity into the presentational
            KpiCard primitive. */}
        {onKillSwitch ? (
          <button
            type="button"
            onClick={onKillSwitch}
            className="text-left p-0 bg-transparent border-0 cursor-pointer focus-visible:outline-2 focus-visible:outline-[var(--border-focus)] focus-visible:outline-offset-2 rounded-lg"
            aria-label={
              snapshot.kill_switch
                ? 'Resume trading — deactivate kill switch'
                : 'Halt trading — activate kill switch'
            }
            title={
              snapshot.kill_switch
                ? 'Click to resume trading'
                : 'Click to halt trading'
            }
          >
            <KpiCard
              id="kill-switch"
              label="Kill Switch"
              value={snapshot.kill_switch ? 'ON' : 'Off'}
              tone={snapshot.kill_switch ? 'negative' : 'positive'}
              sub={
                snapshot.kill_switch_durable
                  ? 'Durable — held across restarts'
                  : 'Volatile — clears on restart'
              }
              interactive
              title={
                snapshot.kill_switch
                  ? 'Kill switch active — click to resume trading'
                  : 'Kill switch inactive — click to halt trading'
              }
            />
          </button>
        ) : (
          <KpiCard
            id="kill-switch"
            label="Kill Switch"
            value={snapshot.kill_switch ? 'ON' : 'Off'}
            tone={snapshot.kill_switch ? 'negative' : 'positive'}
            sub={
              snapshot.kill_switch_durable
                ? 'Durable — held across restarts'
                : 'Volatile — clears on restart'
            }
            title={
              snapshot.kill_switch
                ? 'Kill switch active — all trading halted'
                : 'Kill switch inactive — trading enabled'
            }
          />
        )}
        <MaxExposureCard
          used={totalExposure}
          cap={maxExposure}
          loading={statusData.loading && !statusData.data}
          error={statusData.error}
          stale={staleFor(statusData.fetchedAt)}
        />
      </div>

      {/* ── 5. Main grid — positions | order books | trades | sidebar ──── */}
      <div
        style={{ gridArea: 'pos' }}
        className={cellClass}
        role="region"
        aria-label={`Active positions (${summaryCounts.positions})`}
      >
        {positions}
      </div>
      <div
        style={{ gridArea: 'orders' }}
        className={cellClass}
        role="region"
        aria-label={`Order books — ${summaryCounts.orders} open orders`}
      >
        {orderBooks}
      </div>
      <div
        style={{ gridArea: 'trades' }}
        className={cellClass}
        role="region"
        aria-label={`Recent trades (${summaryCounts.trades})`}
      >
        {recentTrades}
      </div>
      <div
        style={sidebarStyle}
        className="scrollbar-thin"
        role="region"
        aria-label="Analytics sidebar — equity curve, analytics, ML"
      >
        {sidebar}
      </div>
    </div>
  )
}

// Wrap in `memo` so the parent's 2s snapshot re-renders don't cascade into
// dashboard re-renders when the displayed values haven't actually changed.
// ReactNode props (positions, orderBooks, etc.) are compared by reference —
// the parent should keep them stable (wrapped in their own `memo` panels).
export const CommandCenterDashboard = memo(CommandCenterDashboardImpl)

export default CommandCenterDashboard
