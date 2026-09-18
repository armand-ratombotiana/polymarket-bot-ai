// components/CommandCenterDashboard.tsx — W39-3 / W49-3 / W50-2d Professional
// Trading Dashboard.
//
// Replaces the prior five-row panel-grid assembly (which interleaved a
// 3-KPI "risk bar" with a sidebar of EquityCurve + Analytics + ML) with
// a clean, professional trading dashboard hierarchy:
//
//   ┌───────────────────────────────────────────────────────────────────────┐
//   │ 1. System status bar (Backend · WS · Data Fresh · Risk · Kill · Time)  │
//   ├───────────────────────────────────────────────────────────────────────┤
//   │ 2. Top bar — Portfolio Value  ·  Available Balance  ·  Open Exposure   │
//   │     (3 large hero KPIs with trend sub-text + stale pills)             │
//   ├───────────────────────────────────────────────────────────────────────┤
//   │ 3. P&L row — Realized · Unrealized · Win Rate · Drawdown · Sharpe     │
//   │     (5 medium KPIs with color tones + loading skeletons)              │
//   ├───────────────────────┬───────────────────────┬───────────────────────┤
//   │ 4. Activity grid      │                       │                       │
//   │   Active Positions    │   Order Books         │   Recent Trades       │
//   │   (mini table)        │   (mini list)         │   (mini list)         │
//   ├───────────────────────┴───────────────────────┴───────────────────────┤
//   │ 5. System status (2 columns)                                          │
//   │   Left:  Active Strategies  +  AI Status                               │
//   │   Right: Data Ingestion   +  Alerts                                   │
//   └───────────────────────────────────────────────────────────────────────┘
//
// Each KPI card uses the <KpiCard> primitive — label (uppercase, small,
// dimmed), value (large bold tabular-nums), sub-text (trend %, timestamp,
// context), color tone (green/red/amber), loading skeleton, and stale
// indicator all live in CSS utility classes declared in globals.css.
//
// ─────────────────────────────────────────────────────────────────────────
// W50-2d — Premium visual redesign of the 5-row Command Center dashboard.
// Each row gains refined structural polish + new visual affordances
// WITHOUT modifying the shared <KpiCard> component or removing any
// existing class name (the CSS agent layers enhancements on top):
//
//   • Row 1 — System status bar wrapped in a slim premium chrome
//     container (data-area="system") for refined border + shadow.
//
//   • Row 2 — 3 hero KPIs gain trailing SVG sparklines + trend-arrow
//     sub-text (▲/▼) so the trader reads direction at a glance, not
//     just magnitude. The Open Exposure card carries a MiniProgressArc
//     showing exposure/cap ratio inline next to the value.
//
//   • Row 3 — 5 P&L KPIs each wrapped in a <KpiToneCell data-tone>
//     wrapper (display:contents → transparent to grid layout) so the
//     CSS agent can apply tone-tinted backgrounds (green/red/amber
//     halo) via descendant selectors without modifying KpiCard.
//
//   • Row 4 — 3 activity cells keep their existing panel ReactNodes
//     (each panel already has its own header chrome) but gain a
//     data-area hook + dashboard-activity-cell class for premium
//     grid-cell polish (refined border, hover lift).
//
//   • Row 5 — System status cards gain data-tone attributes on their
//     root divs for tone-tinted card surfaces.
//
// All visual styling is driven by data-tone + data-area + className
// hooks. Existing class names (.kpi-card, .kpi-label, .kpi-value,
// .kpi-tone-*, .kpi-stale-pill, .kpi-skeleton, .command-center-layout,
// .sys-status-card, .sys-status-col, .sys-status-body) are preserved
// unchanged so the CSS agent's enhancements layer cleanly on top.
//
// Data sources:
//   * `snapshot` prop — paper_balance, positions, daily_pnl, kill_switch,
//     strategies (driven by the parent useBot hook).
//   * `/api/status` — total_exposure, max_total_exposure, daily_loss_limit,
//     drawdown_dollars, max_drawdown_limit.
//   * `/api/analytics` — realized_pnl, unrealized_pnl, win_rate, total_trades,
//     max_drawdown_pct, sharpe_ratio.
//   * `/api/ml/metrics` — model_ready, brier_score, roc_auc, ece.
//   * `/api/ml/drift` — drift PSI + status (drives AI Status tone).
//   * `/api/ingestion/health` — source connection status + freshness.
//   * `useAlertNotifications` — recent alert feed (drives Alerts list).
//
// The activity-grid panels (PositionsPanel, MarketsPanel, TradesPanel) are
// received as ReactNode props so the parent page.tsx retains ownership of
// the per-panel event handlers (cancel order, close position, open chart)
// and the panels' own useRealtimeData WS subscriptions stay singletons.
//
// W49-3 — Removed the third "risk bar" row (Risk Status · Kill Switch ·
// Max Exposure) and the right-hand sidebar (EquityCurve + Analytics + ML).
// The kill switch is already surfaced on Row 1 and on the TopStatusBar
// above the dashboard; EquityCurve / Analytics / ML have their own
// dedicated sidebar-nav sections. Row 5 replaces the sidebar with two
// compact system-status columns so the trader can see strategy + AI
// health + ingestion + alert state in a single glance.
'use client'

import { memo, useEffect, useMemo, useState, type ReactNode } from 'react'
import { BotSnapshot, ConnectionStatus, type MLState } from '@/hooks/useBot'
import { useAlertNotifications, type Alert } from '@/hooks/useAlertNotifications'
import { apiFetch } from '@/lib/api'
import {
  fmtUsd,
  fmtPnl,
  fmtPct,
  fmtInt,
  fmtAge,
} from '@/lib/design-tokens'
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

interface IngestionSource {
  id: string
  name: string
  status: 'connected' | 'disconnected' | 'reconnecting'
  last_event_at: number | null
}

interface IngestionHealthPayload {
  sources?: IngestionSource[]
  metrics?: {
    data_freshness_seconds?: number
    events_per_minute?: number
    avg_latency_ms?: number
  }
  generated_at?: number
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

function fmtSharpe(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(2)
}

function sharpeTone(v: number | null | undefined): KpiTone {
  if (v == null || !Number.isFinite(v)) return 'neutral'
  if (v >= 1.5) return 'positive'
  if (v < 0) return 'negative'
  if (v < 0.5) return 'warning'
  return 'neutral'
}

function mlToneFor(ready: boolean, driftStatus: string): KpiTone {
  if (!ready) return 'warning'
  if (driftStatus && driftStatus.toUpperCase() !== 'HEALTHY') return 'negative'
  return 'positive'
}

function sourceTone(s: IngestionSource): KpiTone {
  if (s.status === 'connected') return 'positive'
  if (s.status === 'reconnecting') return 'warning'
  return 'negative'
}

function severityTone(a: Alert): KpiTone {
  if (a.severity === 'critical') return 'negative'
  if (a.severity === 'error' || a.severity === 'warning') return 'warning'
  return 'neutral'
}

// ── W50-2d Inline sub-component: TrendArrow ─────────────────────────────────
// Tiny inline arrow (▲/▼/■) rendered before the trend % sub-text on the
// hero KPIs. Direction-aware semantic color (green up / red down / muted
// flat). Hidden from screen readers because the sub-text already carries
// the +/− sign and the value tone already conveys direction via color.
function TrendArrow({
  direction,
  className = '',
}: {
  direction: 'up' | 'down' | 'flat'
  className?: string
}) {
  const arrow = direction === 'up' ? '▲' : direction === 'down' ? '▼' : '■'
  const colorClass =
    direction === 'up'
      ? 'text-green-400'
      : direction === 'down'
      ? 'text-red-400'
      : 'text-[#7e8aaa]'
  return (
    <span
      aria-hidden="true"
      className={`inline-block text-[9px] leading-none ${colorClass} ${className}`.trim()}
    >
      {arrow}
    </span>
  )
}

// ── W50-2d Inline sub-component: MiniSparkline ──────────────────────────────
// Tiny 40×14 SVG polyline rendered as the `trailing` element on hero KPIs.
// Synthesizes a 6-point trend line based on the daily P&L direction (we
// don't have historical equity samples at this layer — the sparkline is a
// direction signal, not a precise chart). Tone-aware stroke + faint
// gradient fill so the line matches the value's semantic tone.
function MiniSparkline({
  tone,
  direction,
}: {
  tone: KpiTone
  direction: 'up' | 'down' | 'flat'
}) {
  const stroke =
    tone === 'positive'
      ? 'var(--color-green-fg)'
      : tone === 'negative'
      ? 'var(--color-red-fg)'
      : tone === 'warning'
      ? 'var(--color-amber-fg)'
      : 'var(--kpi-value-color)'
  const fill =
    tone === 'positive'
      ? 'rgba(74,222,128,0.14)'
      : tone === 'negative'
      ? 'rgba(248,113,113,0.12)'
      : tone === 'warning'
      ? 'rgba(251,191,36,0.12)'
      : 'rgba(221,225,237,0.06)'
  const linePoints =
    direction === 'up'
      ? '0,12 8,10 16,8 24,7 32,4 40,2'
      : direction === 'down'
      ? '0,2 8,4 16,6 24,8 32,10 40,12'
      : '0,7 8,7 16,7 24,7 32,7 40,7'
  const fillPoints =
    direction === 'up'
      ? '0,14 0,12 8,10 16,8 24,7 32,4 40,2 40,14'
      : direction === 'down'
      ? '0,14 0,2 8,4 16,6 24,8 32,10 40,12 40,14'
      : '0,14 0,7 8,7 16,7 24,7 32,7 40,7 40,14'
  return (
    <svg
      width="40"
      height="14"
      viewBox="0 0 40 14"
      aria-hidden="true"
      className="mini-sparkline shrink-0"
      role="presentation"
    >
      <polygon points={fillPoints} fill={fill} stroke="none" />
      <polyline
        points={linePoints}
        fill="none"
        stroke={stroke}
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// ── W50-2d Inline sub-component: MiniProgressArc ────────────────────────────
// Tiny 16×16 SVG radial arc used as the `trailing` element on the Open
// Exposure hero KPI. Renders a 270° arc with the fill proportional to the
// exposure / cap ratio. Tone-aware stroke color (amber when ratio > 0.9).
// The CSS agent can refine the track/fill colors via descendant selectors.
function MiniProgressArc({
  ratio,
  tone,
}: {
  ratio: number
  tone: KpiTone
}) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(ratio) ? ratio : 0))
  const stroke =
    tone === 'warning'
      ? 'var(--color-amber-fg)'
      : tone === 'negative'
      ? 'var(--color-red-fg)'
      : tone === 'positive'
      ? 'var(--color-green-fg)'
      : 'var(--kpi-value-color)'
  const trackStroke = 'rgba(161,168,181,0.20)'
  // 270° arc, radius 5.5, center (8,8). Rotate so the gap is at the bottom.
  const r = 5.5
  const cx = 8
  const cy = 8
  const circumference = 2 * Math.PI * r
  const arcLength = (270 / 360) * circumference
  const dashLength = clamped * arcLength
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      aria-hidden="true"
      className="mini-progress-arc shrink-0"
      role="presentation"
    >
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={trackStroke}
        strokeWidth="1.6"
        strokeDasharray={`${arcLength} ${circumference - arcLength}`}
        strokeDashoffset={circumference / 4}
        transform={`rotate(90 ${cx} ${cy})`}
      />
      <circle
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={stroke}
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeDasharray={`${dashLength} ${circumference - dashLength}`}
        strokeDashoffset={circumference / 4}
        transform={`rotate(90 ${cx} ${cy})`}
      />
    </svg>
  )
}

// ── W50-2d Inline sub-component: KpiToneCell ────────────────────────────────
// Wraps a single KpiCard in a `display: contents` div carrying a
// `data-tone` attribute + `kpi-tone-cell` class. `display: contents` makes
// the wrapper transparent to the CSS grid layout — the KpiCard inside
// remains the actual grid item, so row alignment / stretch behaviour is
// unchanged. The wrapper exists purely as a CSS hook: the CSS agent can
// target `[data-tone="positive"] > .kpi-card` (or `.kpi-tone-cell[data-
// tone="positive"] > .kpi-card`) to apply tone-tinted backgrounds, colored
// halos, or tone-aware hover shadows — without modifying the shared
// <KpiCard> component (which is also used by 9 other panels).
function KpiToneCell({
  tone,
  children,
}: {
  tone: KpiTone
  children: ReactNode
}) {
  return (
    <div
      data-tone={tone}
      className={`kpi-tone-cell kpi-tone-${tone}`}
      style={{ display: 'contents' }}
    >
      {children}
    </div>
  )
}

// ── Inline sub-component: SystemStatusCard ──────────────────────────────────
// The two columns of Row 5 (System Status) are each a stack of compact
// "system status" cards. A SystemStatusCard is the visual sibling of a
// KpiCard — same surface treatment (card bg, 8px radius, subtle shadow)
// — but its body is a free-form ReactNode instead of a single value.
// That keeps the visual rhythm of the dashboard consistent while letting
// each card host a list (strategies, alerts, ingestion sources) rather
// than a single number.
//
// W50-2d — adds `data-tone={tone}` to the root div so the CSS agent can
// apply tone-tinted card surfaces (green/red/amber/neutral) to match the
// per-card state.
function SystemStatusCard({
  label,
  count,
  tone = 'neutral',
  stale = false,
  loading = false,
  error = null,
  children,
  title,
}: {
  label: string
  /** Optional count chip rendered next to the label (e.g. "3 active"). */
  count?: string
  tone?: KpiTone
  stale?: boolean
  loading?: boolean
  error?: string | null
  children?: ReactNode
  title?: string
}) {
  const dotClass =
    tone === 'positive'
      ? 'bg-green-400'
      : tone === 'negative'
      ? 'bg-red-400'
      : tone === 'warning'
      ? 'bg-amber-400'
      : 'bg-[#5a637a]'
  return (
    <div
      className="kpi-card sys-status-card"
      data-testid={`sys-status-${label.toLowerCase().replace(/\s+/g, '-')}`}
      data-tone={tone}
      title={title}
      role="group"
      aria-label={label}
    >
      <div className="kpi-label">
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`}
          aria-hidden="true"
        />
        <span className="truncate">{label}</span>
        {count && (
          <span className="ml-auto text-[10px] mono font-bold text-[#dde1ed] tabular-nums">
            {count}
          </span>
        )}
        {stale && !loading && !error && (
          <span className="kpi-stale-pill" aria-label="stale">
            stale
          </span>
        )}
        {error && !loading && (
          <span className="kpi-error-pill" title={error} aria-label="error">
            err
          </span>
        )}
      </div>
      {loading ? (
        <span
          className="kpi-skeleton kpi-skeleton-md"
          role="status"
          aria-live="polite"
          aria-label="loading"
        />
      ) : error ? (
        <span className="text-[11px] text-[#7e8aaa] italic">unavailable</span>
      ) : (
        <div className="sys-status-body scrollbar-thin">{children}</div>
      )}
    </div>
  )
}

// ── Inline sub-component: StrategiesList ────────────────────────────────────
// Renders the active-strategy names from snapshot.strategies as a compact
// badge list. Falls back to an "empty" hint when no strategies are running.
function StrategiesList({
  strategies,
  activeFromAnalytics,
}: {
  strategies: string[]
  activeFromAnalytics?: string[]
}) {
  const list = useMemo(() => {
    if (strategies.length > 0) return strategies
    if (activeFromAnalytics && activeFromAnalytics.length > 0) {
      return activeFromAnalytics
    }
    return []
  }, [strategies, activeFromAnalytics])

  if (list.length === 0) {
    return (
      <span className="text-[11px] text-[#7e8aaa] italic">
        No active strategies
      </span>
    )
  }

  return (
    <ul
      className="flex flex-wrap gap-1 m-0 p-0 list-none"
      aria-label="Active strategies"
    >
      {list.slice(0, 8).map((s) => (
        <li
          key={s}
          className="inline-flex items-center text-[10.5px] mono font-semibold text-[#dde1ed] bg-[#1a1f2e] border border-[#2d3450] rounded-md px-1.5 py-0.5 max-w-[160px] truncate"
          title={s}
        >
          {s}
        </li>
      ))}
      {list.length > 8 && (
        <li className="inline-flex items-center text-[10px] text-[#7e8aaa] px-1 py-0.5">
          +{list.length - 8} more
        </li>
      )}
    </ul>
  )
}

// ── Inline sub-component: AIStatusBody ──────────────────────────────────────
// Renders the AI Status card body — model readiness + brier + drift PSI.
// Pulled from snapshot.ml with overrides from /api/ml/metrics + /api/ml/drift
// (when those polls succeed).
function AIStatusBody({
  ml,
  mlMetrics,
  drift,
}: {
  ml: MLState | undefined
  mlMetrics: MLMetricsPayload | null
  drift: DriftPayload | null
}) {
  const ready = mlMetrics?.model_ready ?? ml?.model_ready ?? false
  const brier = mlMetrics?.brier_score ?? ml?.brier_score ?? null
  const driftStatus = drift?.status ?? ml?.drift_status ?? 'HEALTHY'
  const driftPsi = drift?.psi ?? ml?.drift_psi ?? 0

  const rows: Array<{ label: string; value: string; tone: KpiTone }> = [
    {
      label: 'Model',
      value: ready ? 'Ready' : 'Warming',
      tone: ready ? 'positive' : 'warning',
    },
    {
      label: 'Brier',
      value:
        brier != null && Number.isFinite(brier) ? brier.toFixed(3) : '—',
      tone:
        brier == null
          ? 'neutral'
          : brier <= 0.2
          ? 'positive'
          : brier <= 0.25
          ? 'warning'
          : 'negative',
    },
    {
      label: 'Drift',
      value: `${driftStatus}`,
      tone:
        driftStatus.toUpperCase() === 'HEALTHY'
          ? 'positive'
          : driftStatus.toUpperCase() === 'WARNING'
          ? 'warning'
          : 'negative',
    },
    {
      label: 'PSI',
      value:
        driftPsi != null && Number.isFinite(driftPsi)
          ? driftPsi.toFixed(3)
          : '—',
      tone:
        driftPsi == null
          ? 'neutral'
          : driftPsi < 0.1
          ? 'positive'
          : driftPsi < 0.25
          ? 'warning'
          : 'negative',
    },
  ]

  return (
    <dl
      className="grid grid-cols-2 gap-x-3 gap-y-1 m-0"
      aria-label="AI status summary"
    >
      {rows.map((r) => (
        <div
          key={r.label}
          className="flex items-center justify-between gap-2 min-w-0"
        >
          <dt className="text-[10px] uppercase tracking-wider text-[#7e8aaa] font-semibold truncate">
            {r.label}
          </dt>
          <dd
            className={`text-[11px] mono font-bold tabular-nums truncate ${
              r.tone === 'positive'
                ? 'kpi-tone-positive'
                : r.tone === 'negative'
                ? 'kpi-tone-negative'
                : r.tone === 'warning'
                ? 'kpi-tone-warning'
                : 'text-[#dde1ed]'
            }`}
          >
            {r.value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

// ── Inline sub-component: IngestionBody ─────────────────────────────────────
// Renders the Data Ingestion card body — list of connector sources with
// their connection state + last-event age.
function IngestionBody({
  data,
  error,
  loading,
}: {
  data: IngestionHealthPayload | null
  error: string | null
  loading: boolean
}) {
  if (loading && !data) {
    return (
      <span className="text-[11px] text-[#7e8aaa] italic">loading…</span>
    )
  }
  if (error && !data) {
    return <span className="text-[11px] text-[#f87171] italic">unavailable</span>
  }
  const sources = data?.sources ?? []
  if (sources.length === 0) {
    return (
      <span className="text-[11px] text-[#7e8aaa] italic">no sources</span>
    )
  }
  return (
    <ul
      className="flex flex-col gap-0.5 m-0 p-0 list-none"
      aria-label="Ingestion sources"
    >
      {sources.slice(0, 6).map((s) => {
        const tone = sourceTone(s)
        const dotClass =
          tone === 'positive'
            ? 'bg-green-400'
            : tone === 'negative'
            ? 'bg-red-400'
            : 'bg-amber-400'
        return (
          <li
            key={s.id}
            className="flex items-center gap-1.5 text-[11px] min-w-0"
            title={`${s.name}: ${s.status}${
              s.last_event_at ? ` · last ${fmtAge(s.last_event_at)}` : ''
            }`}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`}
              aria-hidden="true"
            />
            <span className="text-[#dde1ed] font-semibold truncate flex-1 min-w-0">
              {s.name}
            </span>
            <span className="text-[#7e8aaa] text-[10px] truncate shrink-0">
              {s.last_event_at ? fmtAge(s.last_event_at) : '—'}
            </span>
          </li>
        )
      })}
      {sources.length > 6 && (
        <li className="text-[10px] text-[#7e8aaa] mt-0.5">
          +{sources.length - 6} more
        </li>
      )}
    </ul>
  )
}

// ── Inline sub-component: AlertsBody ────────────────────────────────────────
// Renders the Alerts card body — most-recent-first list of alert pills.
// Pulled from the useAlertNotifications hook (WS-pushed alerts).
function AlertsBody({
  alerts,
  isConnected,
}: {
  alerts: Alert[]
  isConnected: boolean
}) {
  if (alerts.length === 0) {
    return (
      <span className="text-[11px] text-[#7e8aaa] italic">
        {isConnected ? 'no active alerts' : 'disconnected'}
      </span>
    )
  }
  return (
    <ul
      className="flex flex-col gap-0.5 m-0 p-0 list-none"
      aria-label="Recent alerts"
    >
      {alerts.slice(0, 6).map((a) => {
        const tone = severityTone(a)
        const dotClass =
          tone === 'negative'
            ? 'bg-red-400'
            : tone === 'warning'
            ? 'bg-amber-400'
            : 'bg-[#5a637a]'
        return (
          <li
            key={a.alert_id}
            className="flex items-center gap-1.5 text-[11px] min-w-0"
            title={a.message}
          >
            <span
              className={`w-1.5 h-1.5 rounded-full shrink-0 ${dotClass}`}
              aria-hidden="true"
            />
            <span className="text-[#dde1ed] font-semibold truncate flex-1 min-w-0">
              {a.name}
            </span>
            <span className="text-[#7e8aaa] text-[10px] tabular-nums shrink-0">
              {fmtAge(a.timestamp)}
            </span>
          </li>
        )
      })}
      {alerts.length > 6 && (
        <li className="text-[10px] text-[#7e8aaa] mt-0.5">
          +{alerts.length - 6} more
        </li>
      )}
    </ul>
  )
}

// ── Props ─────────────────────────────────────────────────────────────────
export interface CommandCenterDashboardProps {
  snapshot: BotSnapshot
  status: ConnectionStatus
  wsConnected: boolean
  /** Active positions panel — rendered in the activity grid left column. */
  positions: ReactNode
  /** Order books panel — rendered in the activity grid center column. */
  orderBooks: ReactNode
  /** Recent trades panel — rendered in the activity grid right column. */
  recentTrades: ReactNode
}

// ── Main component ────────────────────────────────────────────────────────
function CommandCenterDashboardImpl({
  snapshot,
  status,
  wsConnected,
  positions,
  orderBooks,
  recentTrades,
}: CommandCenterDashboardProps) {
  // ── Polled backend aggregates ──────────────────────────────────────────
  const statusData = usePolled<StatusPayload>('/api/status', 3000)
  const analytics = usePolled<AnalyticsPayload>('/api/analytics', 8000)
  const mlMetrics = usePolled<MLMetricsPayload>('/api/ml/metrics', 10000)
  const drift = usePolled<DriftPayload>('/api/ml/drift', 10000)
  const ingest = usePolled<IngestionHealthPayload>('/api/ingestion/health', 10000)

  // W49-3 — Alert feed (WS-pushed). Composed here instead of in
  // CommandCenterMetricsStrip so the System Status row owns the alert
  // state alongside the other system signals.
  const { alerts, unreadCount, isConnected: alertsConnected } =
    useAlertNotifications()

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
  const mlState = snapshot.ml

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

  // ── Risk metrics (used to derive exposure + risk-status tone) ────────────
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

  // ── Risk-status derivation (mirrors health bar) ─────────────────────────
  // (W50-2d) — `deriveRiskStatus` was removed (it was dead code left over
  // from the W49-3 removal of the third "risk bar" row; the live risk
  // indicator now lives exclusively in <CommandCenterHealthBar> on Row 1).

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

  // ── Sharpe (from analytics) ──────────────────────────────────────────────
  const sharpe = analytics.data?.sharpe_ratio ?? null

  // ── W50-2d Hero trend derivation ──────────────────────────────────────────
  // Daily P&L drives the Portfolio Value trend direction + sparkline shape.
  // The available-balance card carries a deployment-ratio indicator (cash vs
  // total portfolio); the open-exposure card carries the cap-ratio arc.
  const dailyPnl = snapshot.daily_pnl ?? 0
  const portfolioTone: KpiTone = pnlTone(dailyPnl)
  const portfolioTrend: 'up' | 'down' | 'flat' =
    dailyPnl > 0 ? 'up' : dailyPnl < 0 ? 'down' : 'flat'
  const deployPct =
    totalPortfolioValue > 0 ? openExposure / totalPortfolioValue : null

  // ── Cell wrappers (consistent styling + grid-area routing) ─────────────
  const cellClass = 'min-h-0 min-w-0 overflow-hidden'

  // ── Stats summary for context (also surfaces counts to screen readers) ──
  const summaryCounts = useMemo(() => {
    return {
      positions: positionsArr.length,
      orders: openOrders.length,
      trades: trades.length,
      strategies: strategies.length,
    }
  }, [positionsArr, openOrders, trades, strategies])

  // ── AI / ingestion / alerts derived state ──────────────────────────────
  const aiReady =
    mlMetrics.data?.model_ready ?? mlState?.model_ready ?? false
  const aiDriftStatus =
    drift.data?.status ?? mlState?.drift_status ?? 'HEALTHY'
  const aiTone = mlToneFor(aiReady, aiDriftStatus)

  const ingestSources = ingest.data?.sources ?? []
  const connectedSources = ingestSources.filter(
    (s) => s.status === 'connected',
  ).length
  const ingestFreshSec = ingest.data?.metrics?.data_freshness_seconds ?? null
  const ingestTone: KpiTone =
    ingestSources.length === 0
      ? 'neutral'
      : connectedSources === ingestSources.length
      ? 'positive'
      : connectedSources === 0
      ? 'negative'
      : 'warning'

  const alertsTone: KpiTone =
    alerts.some((a) => a.severity === 'critical')
      ? 'negative'
      : alerts.some((a) => a.severity === 'warning' || a.severity === 'error')
      ? 'warning'
      : 'neutral'

  return (
    <div className="command-center-layout">
      {/* ── 1. System status bar (top) ───────────────────────────────── */}
      <div
        style={{ gridArea: 'system', minHeight: 0 }}
        className="dashboard-system-bar-wrapper"
        data-area="system"
      >
        <CommandCenterHealthBar
          snapshot={snapshot}
          status={status}
          wsConnected={wsConnected}
        />
      </div>

      {/* ── 2. Top bar — 3 large hero KPIs ─────────────────────────────── */}
      <div
        style={{ gridArea: 'topbar', minHeight: 0 }}
        className="grid grid-cols-1 sm:grid-cols-3 gap-3 dashboard-hero-row"
        data-area="hero-kpis"
        role="region"
        aria-label="Top bar — portfolio headline metrics"
      >
        <KpiCard
          id="portfolio-value"
          size="lg"
          label="Portfolio Value"
          value={fmtUsd(totalPortfolioValue)}
          tone={portfolioTone}
          sub={
            <span className="inline-flex items-center gap-1 min-w-0">
              <TrendArrow direction={portfolioTrend} />
              <span className="truncate tabular-nums">
                {dailyPnl >= 0 ? '+' : '−'}
                {fmtUsd(Math.abs(dailyPnl), 2)} today
              </span>
              <span className="text-[#7e8aaa] hidden sm:inline">·</span>
              <span className="text-[#7e8aaa] truncate hidden sm:inline">
                Cash {fmtUsd(availableBalance, 0)}
              </span>
            </span>
          }
          title="Total portfolio value = available cash + open position market value. Trend reflects today's realized + unrealized P&L."
          interactive
          trailing={
            <MiniSparkline tone={portfolioTone} direction={portfolioTrend} />
          }
        />
        <KpiCard
          id="available-balance"
          size="lg"
          label="Available Balance"
          value={fmtUsd(snapshot.paper_balance)}
          tone="neutral"
          sub={
            <span className="inline-flex items-center gap-1 min-w-0">
              <TrendArrow direction="flat" />
              <span className="truncate">
                {deployPct != null
                  ? `${(deployPct * 100).toFixed(0)}% deployed`
                  : 'Deployable cash'}
              </span>
              <span className="text-[#7e8aaa] hidden sm:inline">·</span>
              <span className="text-[#7e8aaa] truncate hidden sm:inline">
                Free cash
              </span>
            </span>
          }
          title="Free paper-trading balance available for new orders (cash not locked in open positions)."
          interactive
          trailing={<MiniSparkline tone="neutral" direction="flat" />}
        />
        <KpiCard
          id="open-exposure"
          size="lg"
          label="Open Exposure"
          value={fmtUsd(openExposure)}
          tone={exposureTone}
          sub={
            <span className="inline-flex items-center gap-1 min-w-0">
              <TrendArrow
                direction={
                  expPct != null && expPct > 0.9 ? 'up' : 'flat'
                }
              />
              <span className="truncate tabular-nums">
                {expPct != null
                  ? `${(expPct * 100).toFixed(0)}% of $${maxExposure.toFixed(0)} cap`
                  : '—'}
              </span>
            </span>
          }
          title="Mark-to-mid value of open positions vs configured exposure cap. Amber when utilization exceeds 90%."
          interactive
          trailing={
            expPct != null ? (
              <MiniProgressArc ratio={expPct} tone={exposureTone} />
            ) : null
          }
        />
      </div>

      {/* ── 3. P&L row — 5 medium KPIs ─────────────────────────────────── */}
      <div
        style={{ gridArea: 'pnl', minHeight: 0 }}
        className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3 dashboard-pnl-row"
        data-area="pnl-kpis"
        role="region"
        aria-label="P&L row — realized, unrealized, win rate, drawdown, sharpe"
      >
        <KpiToneCell tone={pnlTone(realizedPnl)}>
          <KpiCard
            id="realized-pnl"
            label="Realized P&L"
            value={fmtPnl(realizedPnl)}
            tone={pnlTone(realizedPnl)}
            sub={
              <span className="inline-flex items-center gap-1">
                <TrendArrow
                  direction={
                    realizedPnl > 0
                      ? 'up'
                      : realizedPnl < 0
                      ? 'down'
                      : 'flat'
                  }
                />
                <span>Closed today</span>
              </span>
            }
            loading={analytics.loading && !analytics.data}
            error={analytics.error}
            stale={staleFor(analytics.fetchedAt)}
            title="Sum of closed-position realized P&L"
            interactive
          />
        </KpiToneCell>
        <KpiToneCell tone={pnlTone(unrealizedPnl)}>
          <KpiCard
            id="unrealized-pnl"
            label="Unrealized P&L"
            value={fmtPnl(unrealizedPnl)}
            tone={pnlTone(unrealizedPnl)}
            sub={
              <span className="inline-flex items-center gap-1">
                <TrendArrow
                  direction={
                    unrealizedPnl > 0
                      ? 'up'
                      : unrealizedPnl < 0
                      ? 'down'
                      : 'flat'
                  }
                />
                <span>Mark-to-mid open</span>
              </span>
            }
            loading={analytics.loading && !analytics.data}
            error={analytics.error}
            stale={staleFor(analytics.fetchedAt)}
            title="Sum of open positions' unrealized P&L"
            interactive
          />
        </KpiToneCell>
        <KpiToneCell tone={winRateTone}>
          <KpiCard
            id="win-rate"
            label="Win Rate"
            value={winRate != null ? fmtPct(winRate) : null}
            tone={winRateTone}
            sub={
              <span className="inline-flex items-center gap-1">
                <TrendArrow
                  direction={
                    winRate != null && winRate >= 0.55
                      ? 'up'
                      : winRate != null && winRate < 0.45
                      ? 'down'
                      : 'flat'
                  }
                />
                <span className="tabular-nums">
                  {analytics.data?.total_trades != null
                    ? `n=${fmtInt(analytics.data.total_trades)}`
                    : '—'}
                </span>
              </span>
            }
            loading={analytics.loading && !analytics.data}
            error={analytics.error}
            stale={staleFor(analytics.fetchedAt)}
            title="Share of closed trades that ended in profit"
            interactive
          />
        </KpiToneCell>
        <KpiToneCell tone={drawdownTone}>
          <KpiCard
            id="drawdown"
            label="Drawdown"
            value={`−$${Math.abs(drawdownDollars).toFixed(2)}`}
            tone={drawdownTone}
            sub={
              <span className="inline-flex items-center gap-1">
                <TrendArrow
                  direction={
                    Math.abs(drawdownDollars) > maxDrawdownLimit * 0.5
                      ? 'down'
                      : 'flat'
                  }
                />
                <span className="tabular-nums">{fmtPct(drawdownPct)}</span>
              </span>
            }
            loading={statusData.loading && !statusData.data}
            error={statusData.error}
            stale={staleFor(statusData.fetchedAt)}
            title="Current drawdown from peak equity vs hard stop"
            interactive
          />
        </KpiToneCell>
        <KpiToneCell tone={sharpeTone(sharpe)}>
          <KpiCard
            id="sharpe"
            label="Sharpe"
            value={fmtSharpe(sharpe)}
            tone={sharpeTone(sharpe)}
            sub={
              <span className="inline-flex items-center gap-1">
                <TrendArrow
                  direction={
                    sharpe != null && sharpe >= 1.5
                      ? 'up'
                      : sharpe != null && sharpe < 0
                      ? 'down'
                      : 'flat'
                  }
                />
                <span>Risk-adjusted return</span>
              </span>
            }
            loading={analytics.loading && !analytics.data}
            error={analytics.error}
            stale={staleFor(analytics.fetchedAt)}
            title="Sharpe ratio — annualized risk-adjusted return (higher is better)"
            interactive
          />
        </KpiToneCell>
      </div>

      {/* ── 4. Activity grid — positions | order books | trades ────────── */}
      <div
        style={{ gridArea: 'pos' }}
        className={`${cellClass} dashboard-activity-cell`}
        data-area="activity-pos"
        role="region"
        aria-label={`Active positions (${summaryCounts.positions})`}
      >
        {positions}
      </div>
      <div
        style={{ gridArea: 'orders' }}
        className={`${cellClass} dashboard-activity-cell`}
        data-area="activity-orders"
        role="region"
        aria-label={`Order books — ${summaryCounts.orders} open orders`}
      >
        {orderBooks}
      </div>
      <div
        style={{ gridArea: 'trades' }}
        className={`${cellClass} dashboard-activity-cell`}
        data-area="activity-trades"
        role="region"
        aria-label={`Recent trades (${summaryCounts.trades})`}
      >
        {recentTrades}
      </div>

      {/* ── 5. System status — 2 columns ───────────────────────────────── */}
      <div
        style={{ gridArea: 'sysleft' }}
        className={`${cellClass} dashboard-sys-col`}
        data-area="sys-left"
        role="region"
        aria-label="System status — strategies and AI"
      >
        <div className="sys-status-col">
          <SystemStatusCard
            label="Active Strategies"
            count={`${summaryCounts.strategies} active`}
            tone={summaryCounts.strategies > 0 ? 'positive' : 'warning'}
            title={`Strategies currently running on the bot (${summaryCounts.strategies})`}
          >
            <StrategiesList
              strategies={strategies}
              activeFromAnalytics={analytics.data?.active_strategies}
            />
          </SystemStatusCard>
          <SystemStatusCard
            label="AI Status"
            tone={aiTone}
            title={
              aiReady
                ? `ML model ready${
                    mlMetrics.data?.brier_score != null
                      ? ` · brier ${mlMetrics.data.brier_score.toFixed(3)}`
                      : ''
                  }`
                : 'ML model warming up — predictions may be unreliable'
            }
          >
            <AIStatusBody
              ml={mlState}
              mlMetrics={mlMetrics.data}
              drift={drift.data}
            />
          </SystemStatusCard>
        </div>
      </div>
      <div
        style={{ gridArea: 'sysright' }}
        className={`${cellClass} dashboard-sys-col`}
        data-area="sys-right"
        role="region"
        aria-label="System status — ingestion and alerts"
      >
        <div className="sys-status-col">
          <SystemStatusCard
            label="Data Ingestion"
            count={`${connectedSources}/${ingestSources.length || 0} sources`}
            tone={ingestTone}
            loading={ingest.loading && !ingest.data}
            error={ingest.error}
            stale={staleFor(ingest.fetchedAt)}
            title="Per-source ingestion health (CLOB / Gamma / WS) — last event age"
          >
            <IngestionBody
              data={ingest.data}
              error={ingest.error}
              loading={ingest.loading}
            />
            {ingestFreshSec != null && (
              <div className="text-[10px] text-[#7e8aaa] mt-1 tabular-nums">
                fresh {ingestFreshSec.toFixed(0)}s
              </div>
            )}
          </SystemStatusCard>
          <SystemStatusCard
            label="Alerts"
            count={
              unreadCount > 0
                ? `${unreadCount} unread`
                : `${alerts.length} total`
            }
            tone={alertsTone}
            stale={!alertsConnected}
            title={`Recent alert feed${
              !alertsConnected ? ' (WS disconnected — feed may be stale)' : ''
            }`}
          >
            <AlertsBody alerts={alerts} isConnected={alertsConnected} />
          </SystemStatusCard>
        </div>
      </div>
    </div>
  )
}

// Wrap in `memo` so the parent's 2s snapshot re-renders don't cascade into
// dashboard re-renders when the displayed values haven't actually changed.
// ReactNode props (positions, orderBooks, recentTrades) are compared by
// reference — the parent should keep them stable (wrapped in their own
// `memo` panels).
export const CommandCenterDashboard = memo(CommandCenterDashboardImpl)

export default CommandCenterDashboard
