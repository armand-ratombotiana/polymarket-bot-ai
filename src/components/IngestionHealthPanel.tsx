// components/IngestionHealthPanel.tsx — Data Ingestion Health Panel (W31-5)
//
// Single-screen operational dashboard for data ingestion health: live source
// status (CLOB / Gamma / WebSocket), throughput / latency / freshness
// metrics, data-quality scores, dead-letter queue, data-gap timeline, and
// market coverage. Mirrors the visual language of DatabaseStatusPanel.tsx
// + ObservabilityPanel.tsx (dark `#13161e` card surface, `#1f2335` borders,
// `#dde1ed` primary text) and uses shadcn/ui primitives per the W31-5 spec.
//
// Backend contract (mirrors the W31-5 endpoints added to
// ``mini-services/polymarket-bot/api/server.py``):
//
//   GET /api/ingestion/health
//     → {
//         sources: Array<{
//           id: string;                  // 'clob' | 'gamma' | 'websocket'
//           name: string;                // human label
//           status: 'connected' | 'disconnected' | 'reconnecting';
//           last_event_at: number | null;  // epoch seconds, null if never
//           events_per_second: number;
//           failed_records: number;
//           error_rate: number;          // 0.0–1.0
//         }>,
//         metrics: {
//           total_events: number;
//           events_per_minute: number;
//           avg_latency_ms: number;      // event-arrival → processing
//           data_freshness_seconds: number;
//           throughput_trend: number[];  // recent EPS samples (oldest→newest)
//           events_per_minute_trend: number[];
//         },
//         generated_at: number,
//       }
//
//   GET /api/ingestion/quality
//     → {
//         overall_score: number,         // 0–100
//         validation_pass_rate: number,  // 0–1
//         duplicate_rate: number,        // 0–1
//         stale_rate: number,             // 0–1
//         invalid_records: number,
//         checks?: Array<{ name: string; status: string; detail: string }>,
//         generated_at: number,
//       }
//
//   GET /api/ingestion/dead-letter
//     → {
//         depth: number,
//         recent: Array<{
//           id: string;
//           source: string;
//           timestamp: number;
//           payload_summary: string;
//           error: string;
//           retries: number;
//         }>,
//         error_breakdown: Array<{ reason: string; count: number }>,
//         generated_at: number,
//       }
//
//   POST /api/ingestion/dead-letter/retry
//     → { success: boolean; retried: number; message: string; attempted_at: number }
//
//   GET /api/ingestion/coverage
//     → {
//         markets_tracked: number,
//         markets_recent: number,        // updated within freshness window
//         markets_stale: number,        // older than freshness window
//         coverage_pct: number,         // 0–100
//         stale_markets: Array<{ token_id: string; slug: string; last_update: number }>,
//         generated_at: number,
//       }
//
//   GET /api/ingestion/gaps
//     → {
//         gaps: Array<{
//           id: string;
//           source: string;
//           start: number;
//           end: number;
//           duration_seconds: number;
//           affected_markets: string[];
//         }>,
//         generated_at: number,
//       }
//
// All five endpoints are polled on a 15-second cadence (matches the
// DatabaseStatusPanel polling rhythm). Polling pauses when the document
// is hidden and resumes immediately on tab regain.

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { Sparkline as RechartsSparkline } from '@/components/charts'
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Database,
  Gauge,
  Inbox,
  Layers,
  PlugZap,
  Radio,
  RefreshCw,
  Server,
  Unplug,
  XCircle,
  Zap,
  TrendingUp,
} from 'lucide-react'

// ───────────────────────────────────────────────────────────────────────────
// Types — mirror the JSON shapes documented above
// ───────────────────────────────────────────────────────────────────────────

export type SourceStatus = 'connected' | 'disconnected' | 'reconnecting'

export interface IngestionSource {
  id: string
  name: string
  status: SourceStatus
  last_event_at: number | null
  events_per_second: number
  failed_records: number
  error_rate: number
}

export interface IngestionMetrics {
  total_events: number
  events_per_minute: number
  avg_latency_ms: number
  data_freshness_seconds: number
  throughput_trend: number[]
  events_per_minute_trend?: number[]
}

export interface IngestionHealthPayload {
  sources: IngestionSource[]
  metrics: IngestionMetrics
  generated_at: number
}

export interface IngestionQualityPayload {
  overall_score: number
  validation_pass_rate: number
  duplicate_rate: number
  stale_rate: number
  invalid_records: number
  checks?: Array<{ name: string; status: string; detail: string }>
  generated_at: number
}

export interface DeadLetterItem {
  id: string
  source: string
  timestamp: number
  payload_summary: string
  error: string
  retries: number
}

export interface DeadLetterBreakdownEntry {
  reason: string
  count: number
}

export interface DeadLetterPayload {
  depth: number
  recent: DeadLetterItem[]
  error_breakdown: DeadLetterBreakdownEntry[]
  generated_at: number
}

export interface DeadLetterRetryResult {
  success: boolean
  retried: number
  message: string
  attempted_at: number
}

export interface CoverageMarket {
  token_id: string
  slug: string
  last_update: number
}

export interface CoveragePayload {
  markets_tracked: number
  markets_recent: number
  markets_stale: number
  coverage_pct: number
  stale_markets: CoverageMarket[]
  generated_at: number
}

export interface IngestionGap {
  id: string
  source: string
  start: number
  end: number
  duration_seconds: number
  affected_markets: string[]
}

export interface GapsPayload {
  gaps: IngestionGap[]
  generated_at: number
}

// ───────────────────────────────────────────────────────────────────────────
// Constants — endpoints + polling cadence
// ───────────────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 15_000

const HEALTH_ENDPOINT = '/api/ingestion/health'
const QUALITY_ENDPOINT = '/api/ingestion/quality'
const DEAD_LETTER_ENDPOINT = '/api/ingestion/dead-letter'
const DEAD_LETTER_RETRY_ENDPOINT = '/api/ingestion/dead-letter/retry'
const COVERAGE_ENDPOINT = '/api/ingestion/coverage'
const GAPS_ENDPOINT = '/api/ingestion/gaps'

// ───────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ───────────────────────────────────────────────────────────────────────────

function formatRelativeTime(epoch: number | null | undefined): string {
  if (!epoch) return '—'
  const diff = Date.now() / 1000 - epoch
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function formatSeconds(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  if (s < 3600) return `${(s / 60).toFixed(1)}m`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
}

function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  if (ms < 1) return `${ms.toFixed(2)}ms`
  return `${ms.toFixed(0)}ms`
}

function formatCount(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  return Math.round(n).toLocaleString()
}

function formatPct(p: number | null | undefined, digits = 1): string {
  if (p === null || p === undefined || !Number.isFinite(p)) return '—'
  return `${p.toFixed(digits)}%`
}

function formatRate(n: number | null | undefined): string {
  if (n === null || n === undefined || !Number.isFinite(n)) return '—'
  if (n < 10) return n.toFixed(2)
  if (n < 100) return n.toFixed(1)
  return n.toFixed(0)
}

function formatDuration(s: number | null | undefined): string {
  if (s === null || s === undefined || !Number.isFinite(s)) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  if (s < 3600) return `${(s / 60).toFixed(1)}m`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
}

// Colour picker for overall quality score (0–100): green ≥ 90, amber ≥ 75, red otherwise.
function scoreColor(score: number): string {
  if (!Number.isFinite(score)) return 'text-[#7e8aaa]'
  if (score >= 90) return 'text-green-400'
  if (score >= 75) return 'text-amber-400'
  return 'text-red-400'
}

// ───────────────────────────────────────────────────────────────────────────
// Sub-components
// ───────────────────────────────────────────────────────────────────────────

interface KpiCardProps {
  label: string
  value: string
  sub?: string
  valueClass?: string
  icon?: typeof Database
  'data-testid'?: string
}

function KpiCard({ label, value, sub, valueClass, icon: Icon, ...rest }: KpiCardProps) {
  return (
    <div className="kpi-card" data-testid={rest['data-testid']}>
      <span className="kpi-label flex items-center gap-1.5">
        {Icon && <Icon size={11} aria-hidden="true" />}
        {label}
      </span>
      <span className={`kpi-value ${valueClass ?? ''}`}>{value}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  )
}

interface SourceStatusBadgeProps {
  status: SourceStatus
}

function SourceStatusBadge({ status }: SourceStatusBadgeProps) {
  const variant: 'success' | 'destructive' | 'warning' =
    status === 'connected'
      ? 'success'
      : status === 'disconnected'
        ? 'destructive'
        : 'warning'
  const label =
    status === 'connected'
      ? 'Connected'
      : status === 'disconnected'
        ? 'Disconnected'
        : 'Reconnecting'
  return (
    <Badge
      variant={variant}
      className="px-2 py-1 text-[9.5px] gap-1.5"
      data-testid={`source-status-${status}`}
    >
      {status === 'connected' ? (
        <CheckCircle2 size={11} aria-hidden="true" />
      ) : status === 'disconnected' ? (
        <XCircle size={11} aria-hidden="true" />
      ) : (
        <RefreshCw size={11} className="animate-spin" aria-hidden="true" />
      )}
      {label}
    </Badge>
  )
}

interface SourceCardProps {
  source: IngestionSource
}

function SourceCard({ source }: SourceCardProps) {
  const errorRatePct = (source.error_rate ?? 0) * 100
  const errorRateColor =
    errorRatePct < 1
      ? 'text-green-400'
      : errorRatePct < 5
        ? 'text-amber-400'
        : 'text-red-400'
  return (
    <Card
      className="bg-[#0e1015] border-[#1f2335] py-0 gap-0"
      data-testid={`source-card-${source.id}`}
    >
      <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
        <CardTitle className="text-xs font-bold text-[#dde1ed] flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            {source.id === 'websocket' ? (
              <Radio size={12} className="text-cyan-400" aria-hidden="true" />
            ) : source.id === 'gamma' ? (
              <TrendingUp size={12} className="text-cyan-400" aria-hidden="true" />
            ) : (
              <Server size={12} className="text-cyan-400" aria-hidden="true" />
            )}
            {source.name}
          </span>
          <SourceStatusBadge status={source.status} />
        </CardTitle>
      </CardHeader>
      <CardContent className="px-3 py-3 grid grid-cols-2 gap-2.5 text-xs">
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
            Last Event
          </div>
          <div className="mono text-[#dde1ed]" data-testid={`source-last-event-${source.id}`}>
            {formatRelativeTime(source.last_event_at)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
            Events / sec
          </div>
          <div className="mono text-cyan-400" data-testid={`source-eps-${source.id}`}>
            {formatRate(source.events_per_second)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
            Failed Records
          </div>
          <div
            className={`mono ${
              source.failed_records === 0 ? 'text-green-400' : 'text-amber-400'
            }`}
            data-testid={`source-failed-${source.id}`}
          >
            {formatCount(source.failed_records)}
          </div>
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
            Error Rate
          </div>
          <div
            className={`mono ${errorRateColor}`}
            data-testid={`source-error-rate-${source.id}`}
          >
            {formatPct(errorRatePct, 2)}
          </div>
        </div>
      </CardContent>
    </Card>
  )
}

interface ErrorStateProps {
  message: string
  onRetry: () => void
  retrying?: boolean
}

function ErrorState({ message, onRetry, retrying }: ErrorStateProps) {
  return (
    <div className="error-state p-8" role="alert">
      <AlertTriangle
        className="error-state-icon text-[#f87171]"
        size={28}
        aria-hidden="true"
      />
      <div className="error-state-title">Ingestion health endpoint unavailable</div>
      <div className="error-state-desc">{message}</div>
      <Button
        variant="outline"
        size="sm"
        onClick={onRetry}
        className="mt-2"
        disabled={retrying}
        aria-label="Retry ingestion health fetch"
      >
        <RefreshCw size={14} className={retrying ? 'animate-spin' : ''} />
        {retrying ? 'Retrying…' : 'Retry'}
      </Button>
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="skeleton h-12 w-full rounded-md" />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="skeleton h-20 w-full rounded-md" />
        ))}
      </div>
      <div className="skeleton h-48 w-full rounded-md" />
      <div className="skeleton h-32 w-full rounded-md" />
    </div>
  )
}

interface SectionCardProps {
  icon: typeof Database
  iconClass: string
  title: string
  badge?: React.ReactNode
  children: React.ReactNode
  'data-testid'?: string
}

function SectionCard({
  icon: Icon,
  iconClass,
  title,
  badge,
  children,
  ...rest
}: SectionCardProps) {
  return (
    <Card
      className="bg-[#0e1015] border-[#1f2335] py-0 gap-0"
      data-testid={rest['data-testid']}
    >
      <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
        <CardTitle className="text-xs font-bold text-[#dde1ed] flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            <Icon size={12} className={iconClass} aria-hidden="true" />
            {title}
          </span>
          {badge}
        </CardTitle>
      </CardHeader>
      <CardContent className="px-3 py-3">{children}</CardContent>
    </Card>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Main panel
// ───────────────────────────────────────────────────────────────────────────

export default function IngestionHealthPanel() {
  const [health, setHealth] = useState<IngestionHealthPayload | null>(null)
  const [quality, setQuality] = useState<IngestionQualityPayload | null>(null)
  const [deadLetter, setDeadLetter] = useState<DeadLetterPayload | null>(null)
  const [coverage, setCoverage] = useState<CoveragePayload | null>(null)
  const [gaps, setGaps] = useState<GapsPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [dlqRetryResult, setDlqRetryResult] = useState<DeadLetterRetryResult | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  // Fetch all five endpoints concurrently. Errors from any individual
  // endpoint are surfaced as a single banner (mirrors the
  // DatabaseStatusPanel pattern: the panel keeps rendering whatever it
  // has, but the operator sees *why* the latest poll failed).
  const fetchAll = useCallback(async () => {
    try {
      const [healthRes, qualityRes, dlqRes, coverageRes, gapsRes] = await Promise.all([
        apiFetch(HEALTH_ENDPOINT),
        apiFetch(QUALITY_ENDPOINT),
        apiFetch(DEAD_LETTER_ENDPOINT),
        apiFetch(COVERAGE_ENDPOINT),
        apiFetch(GAPS_ENDPOINT),
      ])
      if (!healthRes.ok) {
        throw new Error(`GET ${HEALTH_ENDPOINT} → ${healthRes.status} ${healthRes.statusText}`)
      }
      const [h, q, d, c, g] = await Promise.all([
        healthRes.json() as Promise<IngestionHealthPayload>,
        qualityRes.ok
          ? (qualityRes.json() as Promise<IngestionQualityPayload>)
          : Promise.resolve(null),
        dlqRes.ok
          ? (dlqRes.json() as Promise<DeadLetterPayload>)
          : Promise.resolve(null),
        coverageRes.ok
          ? (coverageRes.json() as Promise<CoveragePayload>)
          : Promise.resolve(null),
        gapsRes.ok
          ? (gapsRes.json() as Promise<GapsPayload>)
          : Promise.resolve(null),
      ])
      setHealth(h)
      setQuality(q)
      setDeadLetter(d)
      setCoverage(c)
      setGaps(g)
      setLastUpdated(Date.now())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 15s polling, paused when document hidden.
  // Mirrors the visibility-aware pattern used by ObservabilityPanel +
  // DatabaseStatusPanel: the tick re-checks `document.hidden` so a
  // visibility flip between events still short-circuits.
  useEffect(() => {
    fetchAll()
    let timer: ReturnType<typeof setInterval> | null = null
    const startPolling = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchAll()
      }, POLL_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) {
        stopPolling()
      } else {
        fetchAll()
        startPolling()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    startPolling()
    return () => {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
      stopPolling()
    }
  }, [fetchAll])

  // Dead-letter retry: POSTs to the retry endpoint, surfaces the result
  // inline, then re-fetches the dead-letter payload so the operator sees
  // the post-retry queue depth without waiting for the next poll tick.
  const handleDlqRetry = useCallback(async () => {
    setRetrying(true)
    setDlqRetryResult(null)
    try {
      const r = await apiFetch(DEAD_LETTER_RETRY_ENDPOINT, { method: 'POST' })
      if (r.ok) {
        const json = (await r.json()) as DeadLetterRetryResult
        setDlqRetryResult(json)
        // Re-fetch the dead-letter payload so the queue depth updates.
        const dlqRes = await apiFetch(DEAD_LETTER_ENDPOINT)
        if (dlqRes.ok) {
          setDeadLetter(await dlqRes.json())
        }
      } else {
        setDlqRetryResult({
          success: false,
          retried: 0,
          message: `POST ${DEAD_LETTER_RETRY_ENDPOINT} → ${r.status} ${r.statusText}`,
          attempted_at: Date.now() / 1000,
        })
      }
    } catch (e) {
      setDlqRetryResult({
        success: false,
        retried: 0,
        message: e instanceof Error ? e.message : String(e),
        attempted_at: Date.now() / 1000,
      })
    } finally {
      setRetrying(false)
    }
  }, [])

  const handleManualRefresh = useCallback(() => {
    fetchAll()
  }, [fetchAll])

  // ── Derived display values ──────────────────────────────────────────────

  const metrics = health?.metrics ?? null
  const sources = health?.sources ?? []
  const dlqRecent = deadLetter?.recent ?? []
  const errorBreakdown = deadLetter?.error_breakdown ?? []
  const gapList = gaps?.gaps ?? []
  const staleMarkets = coverage?.stale_markets ?? []
  const maxBreakdownCount = useMemo(
    () => errorBreakdown.reduce((m, e) => Math.max(m, e.count), 0),
    [errorBreakdown],
  )

  // ── Render: loading / error / data ──────────────────────────────────────

  if (loading && !health) {
    return (
      <div
        className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden"
        role="status"
        aria-live="polite"
        aria-label="Loading ingestion health…"
      >
        <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center gap-2 bg-[#0e1015]/80">
          <span className="spinner" aria-hidden="true" />
          <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
            Loading Ingestion Health…
          </span>
        </div>
        <LoadingSkeleton />
      </div>
    )
  }

  if (error && !health) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4">
        <ErrorState message={error} onRetry={handleManualRefresh} />
      </div>
    )
  }

  return (
    <div
      className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin"
      data-testid="ingestion-health-panel"
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-2">
        <div>
          <div className="flex items-center gap-2">
            <PlugZap size={18} className="text-cyan-400" aria-hidden="true" />
            <span className="text-sm font-bold text-[#dde1ed]">
              Data Ingestion Health
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            Source connectivity · throughput · data quality · dead-letter queue · gap detection · market coverage
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-dim text-[9.5px]" data-testid="poll-badge">15s poll</span>
          {lastUpdated && (
            <span className="text-[10px] text-[#7e8aaa] mono">
              updated {formatRelativeTime(Math.floor(lastUpdated / 1000))}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={handleManualRefresh}
            className="h-7 px-2 text-xs border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450]"
            aria-label="Refresh ingestion health"
            disabled={retrying}
          >
            <RefreshCw size={12} className={retrying ? 'animate-spin' : ''} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Transient fetch error banner (only shown once we have prior data) ── */}
      {error && health && (
        <div
          className="banner-danger text-xs py-2 px-3 flex items-center justify-between"
          role="alert"
        >
          <span className="flex items-center gap-1.5">
            <AlertTriangle size={12} aria-hidden="true" />
            <span>{error}</span>
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={handleManualRefresh}
            className="h-6 px-2 text-[10px] border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450]"
            aria-label="Retry ingestion health fetch"
          >
            <RefreshCw size={10} className={retrying ? 'animate-spin' : ''} />
            Retry
          </Button>
        </div>
      )}

      {/* ── KPI cards: ingestion metrics ───────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <KpiCard
          label="Total Events"
          value={formatCount(metrics?.total_events)}
          sub="since startup"
          valueClass="text-cyan-400"
          icon={Activity}
          data-testid="kpi-total-events"
        />
        <KpiCard
          label="Events / min"
          value={formatCount(metrics?.events_per_minute)}
          sub="rolling 60s"
          valueClass="text-green-400"
          icon={TrendingUp}
          data-testid="kpi-events-per-minute"
        />
        <KpiCard
          label="Avg Latency"
          value={formatLatency(metrics?.avg_latency_ms)}
          sub="event → processing"
          valueClass={
            !metrics
              ? 'text-[#7e8aaa]'
              : metrics.avg_latency_ms < 100
                ? 'text-green-400'
                : metrics.avg_latency_ms < 500
                  ? 'text-amber-400'
                  : 'text-red-400'
          }
          icon={Gauge}
          data-testid="kpi-avg-latency"
        />
        <KpiCard
          label="Data Freshness"
          value={formatSeconds(metrics?.data_freshness_seconds)}
          sub="age of latest data"
          valueClass={
            !metrics
              ? 'text-[#7e8aaa]'
              : metrics.data_freshness_seconds < 60
                ? 'text-green-400'
                : metrics.data_freshness_seconds < 300
                  ? 'text-amber-400'
                  : 'text-red-400'
          }
          icon={Clock}
          data-testid="kpi-data-freshness"
        />
      </div>

      {/* ── Throughput sparkline ────────────────────────────────────────── */}
      {metrics && metrics.throughput_trend && metrics.throughput_trend.length > 0 && (
        <SectionCard
          icon={Activity}
          iconClass="text-cyan-400"
          title="Throughput Trend"
          badge={
            <span className="text-[10px] text-[#7e8aaa] font-normal mono">
              {metrics.throughput_trend.length} samples · events/sec
            </span>
          }
          data-testid="throughput-card"
        >
          <div className="flex flex-col gap-2">
            <RechartsSparkline
              data={metrics.throughput_trend}
              color="#22d3ee"
              width="100%"
              height={48}
              showLastDot
            />
            <div className="flex justify-between text-[10px] text-[#7e8aaa] mono">
              <span>min: {Math.min(...metrics.throughput_trend).toFixed(2)}</span>
              <span>max: {Math.max(...metrics.throughput_trend).toFixed(2)}</span>
              <span>last: {metrics.throughput_trend[metrics.throughput_trend.length - 1].toFixed(2)}</span>
            </div>
          </div>
        </SectionCard>
      )}

      {/* ── Source health grid ──────────────────────────────────────────── */}
      <SectionCard
        icon={Server}
        iconClass="text-cyan-400"
        title="Source Health"
        badge={
          <span className="text-[10px] text-[#7e8aaa] font-normal mono">
            {sources.length} sources
          </span>
        }
        data-testid="source-health-card"
      >
        {sources.length === 0 ? (
          <div className="empty-state py-6">
            <Unplug className="empty-state-icon" size={24} aria-hidden="true" />
            <div className="empty-state-title">No ingestion sources reported</div>
            <div className="empty-state-desc">
              The backend has not registered any data sources (CLOB / Gamma / WebSocket).
              This is normal at startup before the poller / WS client connects.
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5">
            {sources.map((s) => (
              <SourceCard key={s.id} source={s} />
            ))}
          </div>
        )}
      </SectionCard>

      {/* ── Data quality scores ─────────────────────────────────────────── */}
      <SectionCard
        icon={CheckCircle2}
        iconClass="text-green-400"
        title="Data Quality Scores"
        badge={
          quality ? (
            <Badge
              variant={
                quality.overall_score >= 90
                  ? 'success'
                  : quality.overall_score >= 75
                    ? 'warning'
                    : 'destructive'
              }
              className="text-[9.5px] px-2 py-0.5"
              data-testid="quality-score-badge"
            >
              {quality.overall_score.toFixed(1)}%
            </Badge>
          ) : undefined
        }
        data-testid="quality-card"
      >
        {!quality ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <AlertTriangle size={14} aria-hidden="true" />
            Quality endpoint unavailable — falling back to no-data state.
          </div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5 text-xs">
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                Overall Score
              </div>
              <div
                className={`font-bold mono ${scoreColor(quality.overall_score)}`}
                data-testid="quality-overall"
              >
                {quality.overall_score.toFixed(1)}%
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                Validation Pass
              </div>
              <div
                className="font-bold mono text-green-400"
                data-testid="quality-validation"
              >
                {formatPct(quality.validation_pass_rate * 100)}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                Duplicate Rate
              </div>
              <div
                className={`font-bold mono ${
                  quality.duplicate_rate < 0.01
                    ? 'text-green-400'
                    : quality.duplicate_rate < 0.05
                      ? 'text-amber-400'
                      : 'text-red-400'
                }`}
                data-testid="quality-duplicate"
              >
                {formatPct(quality.duplicate_rate * 100, 2)}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                Stale Rate
              </div>
              <div
                className={`font-bold mono ${
                  quality.stale_rate < 0.05
                    ? 'text-green-400'
                    : quality.stale_rate < 0.20
                      ? 'text-amber-400'
                      : 'text-red-400'
                }`}
                data-testid="quality-stale"
              >
                {formatPct(quality.stale_rate * 100, 2)}
              </div>
            </div>
            <div>
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                Invalid Records
              </div>
              <div
                className={`font-bold mono ${
                  quality.invalid_records === 0
                    ? 'text-green-400'
                    : quality.invalid_records < 50
                      ? 'text-amber-400'
                      : 'text-red-400'
                }`}
                data-testid="quality-invalid"
              >
                {formatCount(quality.invalid_records)}
              </div>
            </div>
          </div>
        )}
      </SectionCard>

      {/* ── Dead-letter queue ───────────────────────────────────────────── */}
      <SectionCard
        icon={Inbox}
        iconClass="text-amber-400"
        title="Dead-Letter Queue"
        badge={
          <span className="text-[10px] text-[#7e8aaa] font-normal mono">
            depth: {formatCount(deadLetter?.depth)}
          </span>
        }
        data-testid="dead-letter-card"
      >
        <div className="space-y-3">
          {/* Error breakdown bar */}
          {errorBreakdown.length > 0 && (
            <div data-testid="dlq-breakdown">
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-1.5">
                Error Reasons Breakdown
              </div>
              <div className="space-y-1.5">
                {errorBreakdown.map((e, i) => (
                  <div key={`${e.reason}-${i}`} className="flex items-center gap-2 text-xs">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center justify-between gap-2 mb-0.5">
                        <span className="text-[#dde1ed] truncate" title={e.reason}>
                          {e.reason}
                        </span>
                        <span className="mono text-amber-400 shrink-0">{formatCount(e.count)}</span>
                      </div>
                      <div className="h-1.5 bg-[#1f2335] rounded-full overflow-hidden">
                        <div
                          className="h-full bg-amber-500/60 rounded-full"
                          style={{
                            width: `${maxBreakdownCount > 0 ? (e.count / maxBreakdownCount) * 100 : 0}%`,
                          }}
                        />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* Retry button + result banner */}
          <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-[#1f2335]">
            <Button
              variant="outline"
              size="sm"
              onClick={handleDlqRetry}
              disabled={retrying || (deadLetter?.depth ?? 0) === 0}
              className="h-7 px-3 text-xs border-[#1f2335] text-[#dde1ed] hover:bg-[#1f2335] hover:text-white"
              aria-label="Retry dead-letter queue"
              data-testid="dlq-retry-button"
            >
              {retrying ? (
                <RefreshCw size={12} className="animate-spin" />
              ) : (
                <Zap size={12} className="text-amber-400" />
              )}
              {retrying ? 'Retrying…' : 'Retry All'}
            </Button>
            {dlqRetryResult && (
              <span
                role="status"
                aria-live="polite"
                className={`text-[11px] mono ${
                  dlqRetryResult.success ? 'text-green-400' : 'text-red-400'
                }`}
                data-testid="dlq-retry-result"
              >
                {dlqRetryResult.success ? '✓' : '✗'}{' '}
                {dlqRetryResult.message} · retried: {dlqRetryResult.retried}
              </span>
            )}
          </div>

          {/* Recent failed records table */}
          {dlqRecent.length === 0 ? (
            <div className="text-xs text-green-400 py-3 flex items-center gap-2">
              <CheckCircle2 size={14} aria-hidden="true" />
              No failed records in the dead-letter queue.
            </div>
          ) : (
            <div className="max-h-72 overflow-y-auto scrollbar-thin">
              <Table>
                <TableHeader>
                  <TableRow className="border-[#1f2335] hover:bg-transparent">
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Timestamp
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Source
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Payload
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Error
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2 text-right">
                      Retries
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {dlqRecent.map((item, i) => (
                    <TableRow
                      key={`${item.id}-${i}`}
                      className="border-[#1f2335] hover:bg-[#13161e]/60"
                      data-testid={`dlq-row-${i}`}
                    >
                      <TableCell className="mono text-[10px] text-[#7e8aaa] px-2 py-1.5">
                        {formatRelativeTime(item.timestamp)}
                      </TableCell>
                      <TableCell className="px-2 py-1.5">
                        <Badge
                          variant="secondary"
                          className="text-[9px] px-1.5 py-0"
                        >
                          {item.source}
                        </Badge>
                      </TableCell>
                      <TableCell className="mono text-xs text-[#dde1ed] px-2 py-1.5 max-w-[200px] truncate" title={item.payload_summary}>
                        {item.payload_summary}
                      </TableCell>
                      <TableCell className="text-xs text-red-300 px-2 py-1.5 max-w-[260px] truncate" title={item.error}>
                        {item.error}
                      </TableCell>
                      <TableCell className="mono text-xs text-amber-400 px-2 py-1.5 text-right">
                        {item.retries}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      </SectionCard>

      {/* ── Data gaps ───────────────────────────────────────────────────── */}
      <SectionCard
        icon={AlertTriangle}
        iconClass="text-amber-400"
        title="Data Gaps"
        badge={
          <span className="text-[10px] text-[#7e8aaa] font-normal mono">
            {gapList.length} detected
          </span>
        }
        data-testid="gaps-card"
      >
        {gapList.length === 0 ? (
          <div className="text-xs text-green-400 py-3 flex items-center gap-2">
            <CheckCircle2 size={14} aria-hidden="true" />
            No data gaps detected in the active window.
          </div>
        ) : (
          <div className="space-y-2">
            {gapList.map((gap, i) => (
              <div
                key={`${gap.id}-${i}`}
                className="bg-[#13161e] p-2.5 rounded border border-[#1f2335] text-xs"
                data-testid={`gap-row-${i}`}
              >
                <div className="flex flex-wrap justify-between items-center gap-2 mb-1.5">
                  <div className="flex items-center gap-2">
                    <Badge variant="secondary" className="text-[9px] px-1.5 py-0">
                      {gap.source}
                    </Badge>
                    <span className="mono text-[#7e8aaa] text-[10px]">
                      {formatRelativeTime(gap.start)} → {formatRelativeTime(gap.end)}
                    </span>
                  </div>
                  <Badge
                    variant={gap.duration_seconds > 300 ? 'destructive' : 'warning'}
                    className="text-[9px] px-1.5 py-0"
                  >
                    {formatDuration(gap.duration_seconds)}
                  </Badge>
                </div>
                {gap.affected_markets.length > 0 && (
                  <div className="flex flex-wrap gap-1 mt-1">
                    {gap.affected_markets.slice(0, 8).map((m, j) => (
                      <span
                        key={`${m}-${j}`}
                        className="mono text-[9px] text-[#7e8aaa] bg-[#1f2335] px-1.5 py-0.5 rounded"
                      >
                        {m}
                      </span>
                    ))}
                    {gap.affected_markets.length > 8 && (
                      <span className="mono text-[9px] text-[#7e8aaa]">
                        +{gap.affected_markets.length - 8} more
                      </span>
                    )}
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </SectionCard>

      {/* ── Coverage ───────────────────────────────────────────────────── */}
      <SectionCard
        icon={Layers}
        iconClass="text-cyan-400"
        title="Market Coverage"
        badge={
          coverage ? (
            <Badge
              variant={
                coverage.coverage_pct >= 90
                  ? 'success'
                  : coverage.coverage_pct >= 70
                    ? 'warning'
                    : 'destructive'
              }
              className="text-[9.5px] px-2 py-0.5"
              data-testid="coverage-pct-badge"
            >
              {coverage.coverage_pct.toFixed(1)}%
            </Badge>
          ) : undefined
        }
        data-testid="coverage-card"
      >
        {!coverage ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <AlertTriangle size={14} aria-hidden="true" />
            Coverage endpoint unavailable.
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5 text-xs">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Markets Tracked
                </div>
                <div className="font-bold mono text-cyan-400" data-testid="coverage-tracked">
                  {formatCount(coverage.markets_tracked)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Recent Data
                </div>
                <div className="font-bold mono text-green-400" data-testid="coverage-recent">
                  {formatCount(coverage.markets_recent)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Stale Data
                </div>
                <div
                  className={`font-bold mono ${
                    coverage.markets_stale === 0
                      ? 'text-green-400'
                      : coverage.markets_stale < 10
                        ? 'text-amber-400'
                        : 'text-red-400'
                  }`}
                  data-testid="coverage-stale"
                >
                  {formatCount(coverage.markets_stale)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Coverage %
                </div>
                <div
                  className={`font-bold mono ${
                    coverage.coverage_pct >= 90
                      ? 'text-green-400'
                      : coverage.coverage_pct >= 70
                        ? 'text-amber-400'
                        : 'text-red-400'
                  }`}
                  data-testid="coverage-pct"
                >
                  {coverage.coverage_pct.toFixed(1)}%
                </div>
              </div>
            </div>
            {staleMarkets.length > 0 && (
              <div className="pt-2 border-t border-[#1f2335]">
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-1.5">
                  Stale Markets ({Math.min(staleMarkets.length, 10)} of {staleMarkets.length})
                </div>
                <div className="max-h-48 overflow-y-auto scrollbar-thin space-y-1">
                  {staleMarkets.slice(0, 10).map((m, i) => (
                    <div
                      key={`${m.token_id}-${i}`}
                      className="flex items-center justify-between gap-2 text-[11px] bg-[#13161e] px-2 py-1 rounded border border-[#1f2335]"
                    >
                      <span className="mono text-[#dde1ed] truncate" title={m.slug}>
                        {m.slug || m.token_id}
                      </span>
                      <span className="mono text-amber-400 shrink-0">
                        {formatRelativeTime(m.last_update)}
                      </span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </SectionCard>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      <div className="text-[10px] text-[#7e8aaa] mono text-center pt-1">
        Generated at {formatRelativeTime(Math.floor((lastUpdated ?? 0) / 1000))} · endpoints:{' '}
        <span className="text-cyan-400">{HEALTH_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{QUALITY_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{DEAD_LETTER_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{COVERAGE_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{GAPS_ENDPOINT}</span>
      </div>
    </div>
  )
}
