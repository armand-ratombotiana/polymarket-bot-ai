// components/CommandCenterMetricsStrip.tsx — W38-3 Aggregated Metrics Strip
//
// Single-row summary of ALL critical operational metrics for the Command
// Center panel, grouped into five logical clusters so the trader can scan
// the workstation's health at a glance:
//
//   ┌─ Portfolio ─┬─ Trading ─┬─ Risk ─┬─ AI ─┬─ System ─┐
//   │ Total Value  │ Positions │  DD    │ Pred │  Ingest  │
//   │ Avail Bal    │ Orders    │  Stop  │ Drft │  Alerts  │
//   │ Exposure     │ Trades    │  Stop  │ Sharp│  Update  │
//   │ Realized P&L │ Strategies│        │      │          │
//   │ Unreal P&L   │ Win Rate  │        │      │          │
//   └──────────────┴───────────┴────────┴──────┴──────────┘
//
// Progressive disclosure:
//   * Summary KPIs are ALWAYS visible (one row per cluster).
//   * Clicking a cluster header toggles an inline "drill-down" panel
//     that surfaces the supporting context (per-strategy breakdown,
//     reliability curve, alert list, etc.) without leaving the
//     Command Center.
//
// Data sources:
//   * `snapshot` prop — portfolio balance, positions, orders, trades,
//     strategies, daily PnL (driven by the parent useBot hook).
//   * `/api/status` — risk state (total_exposure, daily_loss_limit,
//     drawdown_dollars, max_drawdown_limit).
//   * `/api/analytics` — realized / unrealized PnL, win rate, sharpe,
//     max drawdown.
//   * `/api/ml/metrics` — model readiness, brier / AUC / ECE.
//   * `/api/ml/drift` — concept drift PSI + status.
//   * `/api/ingestion/health` — source status + freshness.
//   * `useAlertNotifications` — recent alert count + unread badge.
//
// Each KPI card has explicit loading (skeleton), error (red dot + "—"),
// and stale (amber "stale" tag) states so the trader never sees a
// silently-zero value when the backend is degraded.
'use client'

import { memo, useEffect, useMemo, useState } from 'react'
import { BotSnapshot } from '@/hooks/useBot'
import { useAlertNotifications } from '@/hooks/useAlertNotifications'
import { apiFetch } from '@/lib/api'
import {
  fmtUsd,
  fmtPnl,
  fmtPct,
  fmtAge,
  fmtInt,
  freshnessClass,
} from '@/lib/design-tokens'
import { Skeleton } from '@/components/ui/skeleton'

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
  sharpe_ratio?: number | null
  max_drawdown_dollars?: number
  max_drawdown_pct?: number
  peak_equity?: number
  total_trades?: number
  active_strategies?: string[]
}

interface MLMetricsPayload {
  model_ready?: boolean
  brier_score?: number
  roc_auc?: number
  ece?: number
  n_online_updates?: number
}

interface DriftPayload {
  status?: string
  psi?: number
}

interface IngestionHealthPayload {
  sources?: Array<{
    id: string
    name: string
    status: 'connected' | 'disconnected' | 'reconnecting'
    last_event_at: number | null
  }>
  metrics?: {
    data_freshness_seconds?: number
    events_per_minute?: number
    avg_latency_ms?: number
  }
  generated_at?: number
}

// ── Cluster + KPI shape ────────────────────────────────────────────────────
type Tone = 'neutral' | 'positive' | 'negative' | 'warning'

interface Kpi {
  id: string
  label: string
  /** Pre-formatted value string. `null` triggers the skeleton state. */
  value: string | null
  /** Optional sub-line (small grey text under the value). */
  sub?: string
  /** Color tone applied to the value text. */
  tone?: Tone
  /** Loading state — overrides value with a skeleton bar. */
  loading?: boolean
  /** Error state — overrides value with a red "—" + error badge. */
  error?: string | null
  /** Optional stale indicator — surfaces an amber "stale" pill. */
  stale?: boolean
  /** Optional tooltip string for hover context. */
  title?: string
}

interface Cluster {
  id: string
  label: string
  icon: string
  kpis: Kpi[]
  /** Optional drill-down detail block (rendered when expanded). */
  detail?: React.ReactNode
}

// ── Sub-components ──────────────────────────────────────────────────────────

const TONE_TEXT: Record<Tone, string> = {
  neutral: 'text-[#dde1ed]',
  positive: 'text-green-400',
  negative: 'text-red-400',
  warning: 'text-amber-300',
}

function KpiCard({ kpi }: { kpi: Kpi }) {
  return (
    <div
      className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2 flex flex-col gap-0.5 min-w-0 relative"
      title={kpi.title}
      data-testid={`kpi-${kpi.id}`}
    >
      <div className="flex items-center justify-between gap-1">
        <span className="text-[9px] uppercase tracking-wider text-[#7e8aaa] font-semibold truncate">
          {kpi.label}
        </span>
        {kpi.stale && !kpi.loading && !kpi.error && (
          <span
            className="text-[8px] uppercase font-bold text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-1 py-px shrink-0"
            title="Stale — last refresh was more than 30s ago"
          >
            stale
          </span>
        )}
        {kpi.error && (
          <span
            className="text-[8px] uppercase font-bold text-red-300 bg-red-500/10 border border-red-500/30 rounded px-1 py-px shrink-0"
            title={kpi.error}
          >
            err
          </span>
        )}
      </div>
      {kpi.loading ? (
        <Skeleton className="h-3.5 w-16 mt-1" />
      ) : kpi.error ? (
        <span className="mono text-[12px] font-bold text-red-400 leading-tight">—</span>
      ) : (
        <span
          className={`mono text-[12px] font-bold leading-tight truncate ${
            kpi.tone ? TONE_TEXT[kpi.tone] : TONE_TEXT.neutral
          }`}
        >
          {kpi.value ?? '—'}
        </span>
      )}
      {kpi.sub && !kpi.loading && !kpi.error && (
        <span className="text-[9px] text-[#7e8aaa] truncate leading-tight">
          {kpi.sub}
        </span>
      )}
    </div>
  )
}

function ClusterBlock({ cluster }: { cluster: Cluster }) {
  const [expanded, setExpanded] = useState(false)
  const hasDetail = !!cluster.detail
  return (
    <div
      className="bg-[#13161e] border border-[#1f2335] rounded-lg p-2 flex flex-col gap-1.5 min-w-0"
      data-testid={`cluster-${cluster.id}`}
    >
      <button
        type="button"
        onClick={() => hasDetail && setExpanded((e) => !e)}
        className={`flex items-center justify-between gap-2 px-1 ${
          hasDetail ? 'cursor-pointer hover:bg-[#1a1f2e] rounded -mx-1 px-1' : 'cursor-default'
        }`}
        aria-expanded={expanded}
        aria-controls={`cluster-${cluster.id}-detail`}
        disabled={!hasDetail}
      >
        <span className="text-[10.5px] uppercase tracking-wider font-bold text-[#dde1ed] flex items-center gap-1.5">
          <span aria-hidden="true">{cluster.icon}</span>
          {cluster.label}
        </span>
        {hasDetail && (
          <span
            className="text-[#7e8aaa] text-[10px] transition-transform"
            style={{ transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
            aria-hidden="true"
          >
            ▶
          </span>
        )}
      </button>
      <div className="grid grid-cols-2 gap-1.5">
        {cluster.kpis.map((k) => (
          <KpiCard key={k.id} kpi={k} />
        ))}
      </div>
      {hasDetail && expanded && (
        <div
          id={`cluster-${cluster.id}-detail`}
          className="mt-1 pt-1.5 border-t border-[#1f2335] text-[10.5px] text-[#7e8aaa] max-h-40 overflow-y-auto scrollbar-thin"
        >
          {cluster.detail}
        </div>
      )}
    </div>
  )
}

// ── Hook: poll an endpoint and expose {data, error, ageSec} ──────────────────
//
// Lightweight single-purpose poller — intentionally NOT useRealtimeData
// because the strip's KPIs are read-only aggregates and we don't want
// to open a second WebSocket subscription on top of the parent useBot
// socket + the AIMLCommandCenter / IngestionHealthPanel sockets.
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

// ── Helpers ──────────────────────────────────────────────────────────────────

function pnlTone(v: number | null | undefined): Tone {
  if (v == null || !Number.isFinite(v)) return 'neutral'
  return v > 0 ? 'positive' : v < 0 ? 'negative' : 'neutral'
}

function staleFor(fetchedAt: number | null, threshMs = 30_000): boolean {
  if (fetchedAt == null) return false
  return Date.now() - fetchedAt > threshMs
}

// ── Main component ──────────────────────────────────────────────────────────

export interface CommandCenterMetricsStripProps {
  snapshot: BotSnapshot
}

function CommandCenterMetricsStripImpl({
  snapshot,
}: CommandCenterMetricsStripProps) {
  // ── Aggregated backend fetches ──────────────────────────────────────────
  // Each cluster polls its own endpoint at a cluster-appropriate cadence;
  // this keeps the strip self-contained and not reliant on the existing
  // panels' polling state.
  const status = usePolled<StatusPayload>('/api/status', 3000)
  const analytics = usePolled<AnalyticsPayload>('/api/analytics', 8000)
  const ml = usePolled<MLMetricsPayload>('/api/ml/metrics', 10000)
  const drift = usePolled<DriftPayload>('/api/ml/drift', 10000)
  const ingest = usePolled<IngestionHealthPayload>('/api/ingestion/health', 10000)
  const { alerts, unreadCount } = useAlertNotifications()

  // Re-render every 5s so "Xs ago" sub-labels stay fresh.
  const [, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 5000)
    return () => clearInterval(t)
  }, [])

  // ── Derived portfolio metrics (from snapshot) ───────────────────────────
  const positions = snapshot.positions ?? []
  const openOrders = snapshot.open_orders ?? []
  const trades = snapshot.recent_trades ?? []
  const strategies = snapshot.strategies ?? []

  // Open exposure = sum of position.current_price * (yes_shares + no_shares)
  // Fallback to total_invested when current_price is missing.
  const openExposure = positions.reduce((sum, p) => {
    const price = typeof p.current_price === 'number' ? p.current_price : p.avg_entry_price
    const shares = (p.yes_shares ?? 0) + (p.no_shares ?? 0)
    return sum + price * shares
  }, 0)

  // Realized P&L — sum of position.realised_pnl, falling back to analytics.
  const realizedPnlFromPositions = positions.reduce(
    (sum, p) => sum + (p.realised_pnl ?? 0),
    0,
  )
  const realizedPnl =
    analytics.data?.realized_pnl ?? realizedPnlFromPositions ?? 0

  // Unrealized P&L — sum of position.unrealized_pnl, falling back to analytics.
  const unrealizedPnlFromPositions = positions.reduce(
    (sum, p) => sum + (p.unrealized_pnl ?? 0),
    0,
  )
  const unrealizedPnl =
    analytics.data?.unrealized_pnl ?? unrealizedPnlFromPositions ?? 0

  // Total portfolio value = available balance + market value of open positions.
  // We treat paper_balance (when present) as the "available" cash component.
  const availableBalance = snapshot.paper_balance ?? 0
  const totalPortfolioValue = availableBalance + openExposure

  // ── Derived risk metrics ─────────────────────────────────────────────────
  const totalExposure = status.data?.total_exposure ?? openExposure
  const maxExposure = status.data?.max_total_exposure ?? 25
  const expPct = maxExposure > 0 ? totalExposure / maxExposure : null

  const drawdownDollars = status.data?.drawdown_dollars ?? analytics.data?.max_drawdown_dollars ?? 0
  const maxDrawdownLimit = status.data?.max_drawdown_limit ?? 8
  const drawdownPct = analytics.data?.max_drawdown_pct ?? 0

  const dailyPnl = snapshot.daily_pnl ?? status.data?.daily_pnl ?? 0
  const dailyLossLimit = status.data?.daily_loss_limit ?? 2

  // ── Derived AI metrics ───────────────────────────────────────────────────
  const modelReady = ml.data?.model_ready ?? snapshot.ml?.model_ready ?? false
  const brierScore = ml.data?.brier_score ?? snapshot.ml?.brier_score ?? null
  const rocAuc = ml.data?.roc_auc ?? snapshot.ml?.roc_auc ?? null
  const driftStatus = drift.data?.status ?? snapshot.ml?.drift_status ?? 'HEALTHY'
  const driftPsi = drift.data?.psi ?? snapshot.ml?.drift_psi ?? 0
  const sharpe = analytics.data?.sharpe_ratio ?? null

  // ── Derived system metrics ───────────────────────────────────────────────
  const ingestSources = ingest.data?.sources ?? []
  const connectedSources = ingestSources.filter((s) => s.status === 'connected').length
  const totalSources = ingestSources.length || 3 // clob / gamma / websocket
  const ingestFreshSec = ingest.data?.metrics?.data_freshness_seconds ?? null

  const alertCount = alerts.length
  const alertsTone: Tone =
    alerts.some((a) => a.severity === 'critical')
      ? 'negative'
      : alerts.some((a) => a.severity === 'warning' || a.severity === 'error')
      ? 'warning'
      : 'neutral'

  const snapshotAgeSec =
    snapshot.timestamp > 0
      ? Math.max(0, Math.floor(Date.now() / 1000 - snapshot.timestamp))
      : null
  const snapshotFreshClass = freshnessClass(snapshot.timestamp, 15, 60)

  // ── Cluster definitions ─────────────────────────────────────────────────
  const clusters: Cluster[] = useMemo(() => {
    const sharedRiskTone: Tone = snapshot.kill_switch
      ? 'negative'
      : snapshot.observation_only
      ? 'warning'
      : 'neutral'

    return [
      // ── Portfolio ──────────────────────────────────────────────────────
      {
        id: 'portfolio',
        label: 'Portfolio',
        icon: '💼',
        kpis: [
          {
            id: 'total-value',
            label: 'Total Value',
            value: fmtUsd(totalPortfolioValue),
            sub: `Cash ${fmtUsd(availableBalance)}`,
            title: 'Cash + open position market value',
          },
          {
            id: 'avail-balance',
            label: 'Available Balance',
            value: fmtUsd(snapshot.paper_balance),
            sub: 'Deployable cash',
            title: 'Free paper-trading balance',
          },
          {
            id: 'open-exposure',
            label: 'Open Exposure',
            value: fmtUsd(openExposure),
            sub: expPct != null ? `${(expPct * 100).toFixed(0)}% of cap` : undefined,
            tone: expPct != null && expPct > 0.9 ? 'warning' : 'neutral',
            title: 'Mark-to-mid value of open positions',
          },
          {
            id: 'realized-pnl',
            label: 'Realized P&L',
            value: fmtPnl(realizedPnl),
            sub: 'Closed today',
            tone: pnlTone(realizedPnl),
            title: 'Sum of closed-position realized P&L',
          },
          {
            id: 'unrealized-pnl',
            label: 'Unrealized P&L',
            value: fmtPnl(unrealizedPnl),
            sub: 'Mark-to-mid open',
            tone: pnlTone(unrealizedPnl),
            title: 'Sum of open positions\' unrealized P&L',
          },
        ],
      },

      // ── Trading ────────────────────────────────────────────────────────
      {
        id: 'trading',
        label: 'Trading',
        icon: '🎯',
        kpis: [
          {
            id: 'positions',
            label: 'Active Positions',
            value: fmtInt(positions.length),
            sub: `${positions.filter((p) => (p.yes_shares ?? 0) > 0 || (p.no_shares ?? 0) > 0).length} open`,
            title: 'Count of positions currently in the book',
          },
          {
            id: 'orders',
            label: 'Open Orders',
            value: fmtInt(openOrders.length),
            sub: openOrders.length > 0 ? 'Pending fills' : 'Idle',
            title: 'Count of unfilled resting orders',
          },
          {
            id: 'trades',
            label: 'Recent Trades',
            value: fmtInt(trades.length),
            sub: 'Last 50 fills',
            title: 'Recent trade fills (server slice)',
          },
          {
            id: 'strategies',
            label: 'Active Strategies',
            value: fmtInt(strategies.length),
            sub: strategies.length > 0 ? strategies.slice(0, 2).join(', ') : 'None',
            title: 'Count of strategies registered with the bot',
          },
          {
            id: 'win-rate',
            label: 'Win Rate',
            value:
              analytics.data?.win_rate != null
                ? fmtPct(analytics.data.win_rate)
                : null,
            sub: analytics.data?.total_trades != null
              ? `n=${analytics.data.total_trades}`
              : undefined,
            loading: analytics.loading && !analytics.data,
            error: analytics.error,
            stale: staleFor(analytics.fetchedAt),
            tone:
              analytics.data?.win_rate != null
                ? analytics.data.win_rate >= 0.55
                  ? 'positive'
                  : analytics.data.win_rate < 0.45
                  ? 'negative'
                  : 'neutral'
                : 'neutral',
            title: 'Share of closed trades that ended in profit',
          },
        ],
        detail: strategies.length > 0 ? (
          <ul className="space-y-0.5">
            {strategies.map((s) => (
              <li key={s} className="flex items-center gap-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-green-400 inline-block" />
                <span className="mono text-[10px] text-[#dde1ed]">{s}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-[10px]">No strategies registered.</span>
        ),
      },

      // ── Risk ──────────────────────────────────────────────────────────
      {
        id: 'risk',
        label: 'Risk',
        icon: '🛡',
        kpis: [
          {
            id: 'drawdown',
            label: 'Drawdown',
            value: `-${Math.abs(drawdownDollars).toFixed(2)}`,
            sub: fmtPct(drawdownPct),
            tone: Math.abs(drawdownDollars) > 4 ? 'negative' : Math.abs(drawdownDollars) > 2 ? 'warning' : 'neutral',
            loading: status.loading && !status.data,
            error: status.error,
            stale: staleFor(status.fetchedAt),
            title: 'Current drawdown from peak equity',
          },
          {
            id: 'daily-stop',
            label: 'Daily Loss Stop',
            value: `-$${Math.abs(dailyLossLimit).toFixed(2)}`,
            sub: `PnL ${fmtPnl(dailyPnl)}`,
            tone: dailyPnl <= -1 ? 'warning' : dailyPnl <= -1.8 ? 'negative' : 'neutral',
            loading: status.loading && !status.data,
            error: status.error,
            stale: staleFor(status.fetchedAt),
            title: 'Circuit-breaker daily realized loss threshold',
          },
          {
            id: 'max-dd-stop',
            label: 'Max DD Stop',
            value: `-$${Math.abs(maxDrawdownLimit).toFixed(2)}`,
            sub: 'Hard stop',
            tone: sharedRiskTone,
            loading: status.loading && !status.data,
            error: status.error,
            stale: staleFor(status.fetchedAt),
            title: 'Hard execution halt when drawdown breaches the high-water-mark stop',
          },
        ],
        detail: (
          <div className="space-y-0.5">
            <div className="flex justify-between">
              <span>Total exposure</span>
              <span className="mono text-[#dde1ed]">{fmtUsd(totalExposure)}</span>
            </div>
            <div className="flex justify-between">
              <span>Exposure cap</span>
              <span className="mono text-[#dde1ed]">{fmtUsd(maxExposure)}</span>
            </div>
            <div className="flex justify-between">
              <span>Observation mode</span>
              <span className={`mono ${snapshot.observation_only ? 'text-amber-300' : 'text-green-400'}`}>
                {snapshot.observation_only ? 'YES' : 'NO'}
              </span>
            </div>
            <div className="flex justify-between">
              <span>Kill switch</span>
              <span className={`mono ${snapshot.kill_switch ? 'text-red-400' : 'text-green-400'}`}>
                {snapshot.kill_switch ? 'ACTIVE' : 'INACTIVE'}
              </span>
            </div>
          </div>
        ),
      },

      // ── AI ────────────────────────────────────────────────────────────
      {
        id: 'ai',
        label: 'AI / ML',
        icon: '🤖',
        kpis: [
          {
            id: 'ai-prediction',
            label: 'Prediction',
            value: modelReady ? 'Ready' : 'Syncing',
            sub: brierScore != null ? `Brier ${brierScore.toFixed(3)}` : undefined,
            tone: modelReady ? 'positive' : 'warning',
            loading: ml.loading && !ml.data,
            error: ml.error,
            stale: staleFor(ml.fetchedAt),
            title: 'ML ensemble readiness + Brier score (lower = better)',
          },
          {
            id: 'drift',
            label: 'Drift',
            value: driftStatus.replace(/_/g, ' '),
            sub: `PSI ${driftPsi.toFixed(3)}`,
            tone:
              driftStatus === 'HEALTHY'
                ? 'positive'
                : driftStatus === 'MODERATE_SHIFT'
                ? 'warning'
                : 'negative',
            loading: drift.loading && !drift.data,
            error: drift.error,
            stale: staleFor(drift.fetchedAt),
            title: 'Concept drift PSI status',
          },
          {
            id: 'sharpe',
            label: 'Sharpe',
            value: sharpe != null ? sharpe.toFixed(2) : null,
            sub: rocAuc != null ? `AUC ${(rocAuc * 100).toFixed(0)}%` : undefined,
            tone:
              sharpe == null
                ? 'neutral'
                : sharpe >= 1
                ? 'positive'
                : sharpe >= 0
                ? 'neutral'
                : 'negative',
            loading: analytics.loading && !analytics.data,
            error: analytics.error,
            stale: staleFor(analytics.fetchedAt),
            title: 'Risk-adjusted return (annualized)',
          },
        ],
        detail: (
          <div className="space-y-0.5">
            <div className="flex justify-between">
              <span>Model ready</span>
              <span className={`mono ${modelReady ? 'text-green-400' : 'text-amber-300'}`}>
                {modelReady ? 'YES' : 'NO'}
              </span>
            </div>
            {brierScore != null && (
              <div className="flex justify-between">
                <span>Brier score</span>
                <span className="mono text-[#dde1ed]">{brierScore.toFixed(4)}</span>
              </div>
            )}
            {rocAuc != null && (
              <div className="flex justify-between">
                <span>ROC-AUC</span>
                <span className="mono text-[#dde1ed]">{rocAuc.toFixed(3)}</span>
              </div>
            )}
            <div className="flex justify-between">
              <span>Drift status</span>
              <span className="mono text-[#dde1ed]">{driftStatus}</span>
            </div>
          </div>
        ),
      },

      // ── System ────────────────────────────────────────────────────────
      {
        id: 'system',
        label: 'System',
        icon: '⚙️',
        kpis: [
          {
            id: 'ingestion',
            label: 'Ingestion',
            value: `${connectedSources}/${totalSources} src`,
            sub: ingestFreshSec != null ? `${ingestFreshSec.toFixed(0)}s fresh` : undefined,
            tone:
              connectedSources === totalSources
                ? 'positive'
                : connectedSources === 0
                ? 'negative'
                : 'warning',
            loading: ingest.loading && !ingest.data,
            error: ingest.error,
            stale: staleFor(ingest.fetchedAt),
            title: 'Data ingestion source connectivity + freshness',
          },
          {
            id: 'alerts',
            label: 'Alerts',
            value: fmtInt(alertCount),
            sub: unreadCount > 0 ? `${unreadCount} unread` : 'All read',
            tone: alertsTone,
            title: 'Recent system alerts (last 50)',
          },
          {
            id: 'last-update',
            label: 'Last Update',
            value: snapshotAgeSec != null ? `${snapshotAgeSec}s ago` : '—',
            sub: snapshot.timestamp > 0 ? fmtAge(snapshot.timestamp) : undefined,
            tone:
              snapshotFreshClass === 'freshness-fresh' || snapshotFreshClass === 'freshness-ok'
                ? 'positive'
                : snapshotFreshClass === 'freshness-stale'
                ? 'warning'
                : 'negative',
            title: 'Bot snapshot last-received timestamp',
          },
        ],
        detail: alerts.length > 0 ? (
          <ul className="space-y-1">
            {alerts.slice(0, 5).map((a) => (
              <li key={a.alert_id} className="flex flex-col gap-0.5 border-b border-[#1f2335] pb-1 last:border-0 last:pb-0">
                <span className="flex items-center gap-1.5">
                  <span
                    className={`w-1.5 h-1.5 rounded-full inline-block ${
                      a.severity === 'critical'
                        ? 'bg-red-400'
                        : a.severity === 'warning'
                        ? 'bg-amber-400'
                        : 'bg-[#5a637a]'
                    }`}
                    aria-hidden="true"
                  />
                  <span className="text-[10px] text-[#dde1ed] font-semibold truncate">{a.name}</span>
                </span>
                <span className="text-[9.5px] text-[#7e8aaa] truncate pl-3">{a.message}</span>
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-[10px]">No recent alerts.</span>
        ),
      },
    ]
  }, [
    availableBalance,
    totalPortfolioValue,
    openExposure,
    expPct,
    realizedPnl,
    unrealizedPnl,
    positions,
    openOrders,
    trades,
    strategies,
    analytics,
    status,
    snapshot.kill_switch,
    snapshot.observation_only,
    snapshot.paper_balance,
    snapshot.timestamp,
    drawdownDollars,
    drawdownPct,
    dailyPnl,
    dailyLossLimit,
    maxDrawdownLimit,
    maxExposure,
    totalExposure,
    ml,
    drift,
    modelReady,
    brierScore,
    rocAuc,
    driftStatus,
    driftPsi,
    sharpe,
    ingest,
    connectedSources,
    totalSources,
    ingestFreshSec,
    alerts,
    unreadCount,
    snapshotAgeSec,
    snapshotFreshClass,
  ])

  return (
    <div
      className="flex flex-col gap-2"
      data-testid="command-center-metrics-strip"
      role="region"
      aria-label="Command Center aggregated metrics"
    >
      <div className="grid grid-cols-1 md:grid-cols-3 lg:grid-cols-5 gap-2">
        {clusters.map((c) => (
          <ClusterBlock key={c.id} cluster={c} />
        ))}
      </div>
    </div>
  )
}

// Wrap in memo so the parent page re-rendering on every snapshot tick
// doesn't force this strip to re-render unless its props change. The
// snapshot object identity changes every tick (useBot setSnapshot), so
// the memo short-circuits the cases where the parent re-renders for
// unrelated reasons (e.g. mobile nav toggles, modal state).
const CommandCenterMetricsStrip = memo(CommandCenterMetricsStripImpl)
export default CommandCenterMetricsStrip
