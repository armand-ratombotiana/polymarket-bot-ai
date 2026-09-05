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
//
// W35-3 — Real-time migration. The /api/ingestion/health endpoint now
// streams over the `system` WS channel via useRealtimeData; the other
// four endpoints (quality, dead-letter, coverage, gaps) keep their
// 15-second REST polling. The panel surfaces a "● Live" / "⟳ Polling"
// badge reflecting the underlying WS transport state, a live
// events-per-second sparkline that appends a sample every time a new
// health payload arrives (WS or poll), and a scrolling "live error
// feed" that prepends each new dead-letter item as it appears.

'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { useRealtimeData } from '@/hooks/useRealtimeData'
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
  Power,
  Radio,
  RefreshCw,
  RotateCw,
  Server,
  Settings,
  ShieldCheck,
  Play,
  Square,
  Trash2,
  Unplug,
  XCircle,
  Zap,
  TrendingUp,
} from 'lucide-react'
import ConfirmationDialog from './ConfirmationDialog'

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
  // W38-6 — Schema-validation fields (optional so the panel tolerates
  // a pre-W38-6 backend that hasn't yet shipped the
  // /api/ingestion/quality schema_version / schema_drift_detected /
  // rejected_records additions). When absent, the Schema Validation
  // Status card renders a graceful "unavailable" placeholder rather
  // than fabricating plausible-looking numbers.
  schema_version?: number | string | null
  schema_drift_detected?: boolean
  rejected_records?: number
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
// W38-6 — Operational visibility extension types.
//
// The panel now also consumes three additional read-only endpoints:
// reliability, backfill status, pipeline status. The shapes mirror
// the JSON contract documented in the W34-5 reliability tracker, the
// W32-3 backfill engine, and the W32-3 pipeline-control status
// snapshot. Every field is optional on the consumer side so the panel
// gracefully tolerates a partial / missing payload (it falls back to
// "—" placeholders rather than crashing).
// ───────────────────────────────────────────────────────────────────────────

export interface ReliabilityWindowMap {
  '24h'?: number
  '7d'?: number
  '30d'?: number
  [key: string]: number | undefined
}

export interface ReliabilitySnapshot {
  source: string
  score: number
  status: 'healthy' | 'degraded' | 'unreliable' | 'unknown' | string
  uptime_pct: ReliabilityWindowMap
  avg_latency_ms: ReliabilityWindowMap
  error_rate: ReliabilityWindowMap
  rate_limit_hits: ReliabilityWindowMap
  gap_count: ReliabilityWindowMap
  recent_events?: Array<{
    timestamp: number
    success: boolean
    latency_ms: number
    error: string
  }>
  score_inputs?: {
    success_rate: number
    latency_consistency: number
    gap_frequency_score: number
    error_recovery_score: number
  }
}

export interface ReliabilityPayload {
  count: number
  sources: Record<string, ReliabilitySnapshot>
  avg_score: number
  source_filter?: string | null
  generated_at: number
}

export interface BackfillRun {
  id: number
  type: string
  started_at: number
  ended_at: number
  total_processed: number
  total_added: number
  total_skipped: number
  total_errors: number
  error_message: string
}

export interface BackfillCheckpoint {
  type: string
  last_offset: number
  last_token_id: string
  last_run_at: number
  completed: boolean
}

export interface BackfillStatusPayload {
  runs: BackfillRun[]
  checkpoints: Record<string, BackfillCheckpoint | null>
  engine_stats?: {
    target_rps?: number
    current_interval?: number
    consecutive_rate_limits?: number
    concurrency?: number
    page_size?: number
    max_pages?: number
  }
  generated_at: number
}

export interface PipelineStatusPayload {
  running: boolean
  ws_running: boolean
  ws_reconnect_count: number
  ws_subscribed_tokens: number
  rest_running: boolean
  rest_tracked_tokens: number
  pipeline_stats?: Record<string, unknown>
  raw_vault_stats?: Record<string, unknown>
  last_started_at: number | null
  last_stopped_at: number | null
  generated_at: number
}

// W38-6 — Operational action result. Every action button (Start /
// Stop / Retry / Launch Backfill / Clear DLQ / Replay Events) posts to
// its respective WRITE endpoint and surfaces the result inline so the
// operator sees the post-action outcome without waiting for the next
// poll tick.
export interface ActionResult {
  ok: boolean
  message: string
  attempted_at: number
}

// Discriminated union of the five confirmation dialogs the panel can
// raise. Each variant carries just enough state for the dialog body +
// the onConfirm handler to execute the underlying POST / DELETE.
type ConfirmDialogState =
  | { kind: 'start' }
  | { kind: 'stop' }
  | { kind: 'retry-failed' }
  | { kind: 'launch-backfill' }
  | { kind: 'clear-dlq' }
  | { kind: 'replay-events' }

// ───────────────────────────────────────────────────────────────────────────
// W35-3 — Live error feed entry.
//
// Mirrors the dead-letter recent item shape but is owned by the panel:
// each new dead-letter item that arrives (via REST poll OR WS push on
// the `system` channel) is prepended to `errorFeed` if its `id` hasn't
// been seen before. The feed is capped at `LIVE_ERROR_FEED_MAX_ROWS`
// entries (older entries fall off the bottom of the scrolling tape).
// ───────────────────────────────────────────────────────────────────────────

export interface LiveErrorEvent {
  id: string
  timestamp: number
  source: string
  message: string
  retries: number
}

// ───────────────────────────────────────────────────────────────────────────
// Constants — endpoints + polling cadence
// ───────────────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 15_000

// W35-3 — Live throughput sparkline sample cap. We retain the last N
// events-per-second samples so the sparkline trends smoothly without
// unbounded memory growth. Each new health payload (REST or WS) appends
// one sample; the sparkline renders the rolling window.
const LIVE_EPS_MAX_SAMPLES = 30

// W35-3 — Live error feed row cap. Mirrors the trade-tape cap pattern
// (TradeTape.maxRows) — newest errors stay at the top, older ones
// scroll off the bottom of the tape.
const LIVE_ERROR_FEED_MAX_ROWS = 50

// W35-3 — WS channel that carries ingestion-health push updates. The
// backend's broadcast layer (mini-services/polymarket-bot/ws/server.py)
// wraps health snapshots in `{ channel: 'system', data: <health> }`.
const INGESTION_WS_CHANNEL = 'system'

const HEALTH_ENDPOINT = '/api/ingestion/health'
const QUALITY_ENDPOINT = '/api/ingestion/quality'
const DEAD_LETTER_ENDPOINT = '/api/ingestion/dead-letter'
const DEAD_LETTER_RETRY_ENDPOINT = '/api/ingestion/dead-letter/retry'
const COVERAGE_ENDPOINT = '/api/ingestion/coverage'
const GAPS_ENDPOINT = '/api/ingestion/gaps'

// W38-6 — Operational visibility + control endpoints. Three read-only
// status endpoints + five WRITE control endpoints. The reads join the
// existing 15s poll cycle in fetchAll; the writes fire on operator
// action and never poll.
const RELIABILITY_ENDPOINT = '/api/ingestion/reliability'
const BACKFILL_STATUS_ENDPOINT = '/api/ingestion/backfill/status'
const PIPELINE_STATUS_ENDPOINT = '/api/ingestion/pipeline/status'
const PIPELINE_START_ENDPOINT = '/api/ingestion/pipeline/start'
const PIPELINE_STOP_ENDPOINT = '/api/ingestion/pipeline/stop'
const BACKFILL_MARKETS_ENDPOINT = '/api/ingestion/backfill/markets'
const REPLAY_ENDPOINT = '/api/ingestion/replay'

// W38-6 — Source filter used by the Replay Events control. The
// backend's POST /api/ingestion/replay?source=... requires a non-empty
// source string (empty source → scanned=0). 'clob' is the primary
// REST ingestion source today (Gamma feeds the metadata layer, WS is
// dormant per the KD-08 / KD-24 / D5 decision documented in
// api/server.py's pipeline-start route).
const DEFAULT_REPLAY_SOURCE = 'clob'

// W38-6 — Cap on the number of recent backfill runs rendered in the
// Backfill Progress card. Mirrors the W32-3 status endpoint's default
// ``limit=20`` so the panel doesn't fetch more than the backend will
// return by default.
const BACKFILL_STATUS_LIMIT = 20

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

// W38-6 — Colour picker for reliability score (0–100). Mirrors the
// scoreColor convention but with a wider green band (>95), an amber
// band (80–95), and red below 80 — matches the W34-5 ReliabilityStatus
// HEALTHY / DEGRADED / UNRELIABLE thresholds documented in
// ``ingestion/reliability.py``.
function reliabilityScoreColor(score: number): string {
  if (!Number.isFinite(score)) return 'text-[#7e8aaa]'
  if (score > 95) return 'text-green-400'
  if (score >= 80) return 'text-amber-400'
  return 'text-red-400'
}

// W38-6 — Reliability status → badge variant. Mirrors the source-status
// badge convention (green for healthy, amber for degraded, red for
// unreliable, neutral for unknown).
function reliabilityStatusVariant(
  status: string,
): 'success' | 'warning' | 'destructive' | 'secondary' {
  if (status === 'healthy') return 'success'
  if (status === 'degraded') return 'warning'
  if (status === 'unreliable') return 'destructive'
  return 'secondary'
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
  // W35-3 — Real-time migration. /api/ingestion/health now flows through
  // useRealtimeData so the panel updates the moment the backend pushes
  // a new health snapshot on the `system` WS channel. The hook also
  // owns the REST prefetch + 15s polling fallback for that endpoint, so
  // the panel no longer fetches `/api/ingestion/health` directly — the
  // remaining four endpoints (quality, dead-letter, coverage, gaps)
  // stay on the visibility-aware 15s REST poll below.
  const {
    data: healthData,
    isLoading: healthLoading,
    error: healthError,
    isRealtime,
  } = useRealtimeData<IngestionHealthPayload>(HEALTH_ENDPOINT, {
    wsChannel: INGESTION_WS_CHANNEL,
    pollInterval: POLL_INTERVAL_MS,
  })
  const health = healthData ?? null

  const [quality, setQuality] = useState<IngestionQualityPayload | null>(null)
  const [deadLetter, setDeadLetter] = useState<DeadLetterPayload | null>(null)
  const [coverage, setCoverage] = useState<CoveragePayload | null>(null)
  const [gaps, setGaps] = useState<GapsPayload | null>(null)
  // W38-6 — Operational visibility state: per-source reliability
  // snapshots, recent backfill runs, and the live pipeline state.
  const [reliability, setReliability] = useState<ReliabilityPayload | null>(null)
  const [backfillStatus, setBackfillStatus] = useState<BackfillStatusPayload | null>(null)
  const [pipelineStatus, setPipelineStatus] = useState<PipelineStatusPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [dlqRetryResult, setDlqRetryResult] = useState<DeadLetterRetryResult | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  // W38-6 — Confirmation dialog + action state. Each operational
  // button click opens a ConfirmationDialog (per the W38-8 dialog
  // contract). On confirm, the dispatcher fires the appropriate
  // WRITE endpoint and surfaces the inline result.
  const [confirmDialog, setConfirmDialog] = useState<ConfirmDialogState | null>(null)
  const [actionPending, setActionPending] = useState(false)
  const [lastActionResult, setLastActionResult] = useState<ActionResult | null>(null)

  // W35-3 — Live throughput sparkline buffer. Every time a new health
  // payload arrives (REST prefetch, REST poll, or WS push), the effect
  // below appends the latest total events-per-second across all sources.
  // The rolling window is capped at LIVE_EPS_MAX_SAMPLES so the
  // sparkline trends smoothly without unbounded memory growth.
  const [liveEPSHistory, setLiveEPSHistory] = useState<number[]>([])

  // W35-3 — Live error feed buffer. Each new dead-letter item (by id)
  // is prepended to the tape; older entries scroll off the bottom past
  // LIVE_ERROR_FEED_MAX_ROWS. The `seenErrorIdsRef` ref dedupes across
  // poll / WS pushes — the REST snapshot of the DLQ returns the same
  // N most-recent items every poll, so without dedupe the tape would
  // re-fill with the same rows every 15 s.
  const [errorFeed, setErrorFeed] = useState<LiveErrorEvent[]>([])
  const seenErrorIdsRef = useRef<Set<string>>(new Set())

  // Fetch the remaining ingestion endpoints (everything except
  // /api/ingestion/health, which now flows through useRealtimeData).
  // Errors from any individual endpoint are surfaced as a single banner
  // (mirrors the DatabaseStatusPanel pattern: the panel keeps rendering
  // whatever it has, but the operator sees *why* the latest poll
  // failed).
  //
  // W38-6 — extended to also fetch reliability + backfill status +
  // pipeline status (the three new read-only operational surfaces).
  // Each fetch resolves independently — a single endpoint failure does
  // NOT block the others (Promise.all + per-response ok check).
  const fetchAll = useCallback(async () => {
    try {
      const [
        qualityRes,
        dlqRes,
        coverageRes,
        gapsRes,
        reliabilityRes,
        backfillRes,
        pipelineRes,
      ] = await Promise.all([
        apiFetch(QUALITY_ENDPOINT),
        apiFetch(DEAD_LETTER_ENDPOINT),
        apiFetch(COVERAGE_ENDPOINT),
        apiFetch(GAPS_ENDPOINT),
        apiFetch(RELIABILITY_ENDPOINT),
        apiFetch(`${BACKFILL_STATUS_ENDPOINT}?limit=${BACKFILL_STATUS_LIMIT}`),
        apiFetch(PIPELINE_STATUS_ENDPOINT),
      ])
      const [q, d, c, g, r, b, p] = await Promise.all([
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
        reliabilityRes.ok
          ? (reliabilityRes.json() as Promise<ReliabilityPayload>)
          : Promise.resolve(null),
        backfillRes.ok
          ? (backfillRes.json() as Promise<BackfillStatusPayload>)
          : Promise.resolve(null),
        pipelineRes.ok
          ? (pipelineRes.json() as Promise<PipelineStatusPayload>)
          : Promise.resolve(null),
      ])
      setQuality(q)
      setDeadLetter(d)
      setCoverage(c)
      setGaps(g)
      setReliability(r)
      setBackfillStatus(b)
      setPipelineStatus(p)
      setLastUpdated(Date.now())
      setError(null)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 15s polling for the four non-health endpoints,
  // paused when document hidden. /api/ingestion/health polling is owned
  // by useRealtimeData (which already short-circuits polling when the
  // WS is connected and pauses ticks when the tab is hidden).
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

  // W35-3 — Live throughput sparkline sampler. Fires on every new
  // `healthData` payload (REST prefetch, REST poll, OR WS push) and
  // appends a sample = Σ(source.events_per_second). The effect deps on
  // the object identity of `healthData` — useRealtimeData issues a
  // fresh object on every WS / poll update, so the effect re-runs
  // exactly once per new snapshot.
  useEffect(() => {
    if (!healthData) return
    const totalEps = (healthData.sources ?? []).reduce(
      (sum, s) => sum + (Number.isFinite(s.events_per_second) ? s.events_per_second : 0),
      0,
    )
    setLiveEPSHistory((prev) => {
      const next = [...prev, totalEps]
      return next.length > LIVE_EPS_MAX_SAMPLES
        ? next.slice(next.length - LIVE_EPS_MAX_SAMPLES)
        : next
    })
  }, [healthData])

  // W35-3 — Live error feed collector. Inspects the latest dead-letter
  // recent items and prepends any we haven't seen before (by id). The
  // dedupe ref persists across renders, so the same DLQ snapshot
  // arriving again via the 15s poll does NOT re-populate the tape.
  // Newest entries sort to the top of the tape so the operator sees
  // the most recent failure first.
  useEffect(() => {
    if (!deadLetter?.recent || deadLetter.recent.length === 0) return
    const newErrors: LiveErrorEvent[] = []
    // Walk oldest → newest so the final prepend order keeps newest at
    // the top of the tape after we prepend in sequence.
    for (const item of deadLetter.recent) {
      if (item && !seenErrorIdsRef.current.has(item.id)) {
        seenErrorIdsRef.current.add(item.id)
        newErrors.push({
          id: item.id,
          timestamp: item.timestamp,
          source: item.source,
          message: item.error,
          retries: item.retries,
        })
      }
    }
    if (newErrors.length === 0) return
    setErrorFeed((prev) => {
      const merged = [...newErrors.reverse(), ...prev]
      return merged.length > LIVE_ERROR_FEED_MAX_ROWS
        ? merged.slice(0, LIVE_ERROR_FEED_MAX_ROWS)
        : merged
    })
  }, [deadLetter])

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

  // W39-1 — Operational action dispatcher. Triggered by the
  // ConfirmationDialog's onConfirm callback. Maps each dialog `kind` to
  // its corresponding WRITE endpoint (start / stop / retry / launch
  // backfill / clear DLQ / replay), posts the request, surfaces the
  // result inline via `lastActionResult`, then refreshes the panel so
  // the operator sees the post-action state without waiting for the
  // next 15s poll tick. The dialog stays open (loading=true) while the
  // request is in flight; it closes in the finally block once the
  // result has been recorded.
  const handleConfirmAction = useCallback(
    async (kind: ConfirmDialogState['kind']) => {
      setActionPending(true)
      setLastActionResult(null)
      let res: Response | null = null
      try {
        switch (kind) {
          case 'start':
            res = await apiFetch(PIPELINE_START_ENDPOINT, { method: 'POST' })
            break
          case 'stop':
            res = await apiFetch(PIPELINE_STOP_ENDPOINT, { method: 'POST' })
            break
          case 'retry-failed':
            res = await apiFetch(DEAD_LETTER_RETRY_ENDPOINT, { method: 'POST' })
            break
          case 'launch-backfill':
            res = await apiFetch(`${BACKFILL_MARKETS_ENDPOINT}?resume=true`, {
              method: 'POST',
            })
            break
          case 'clear-dlq':
            res = await apiFetch(DEAD_LETTER_ENDPOINT, { method: 'DELETE' })
            break
          case 'replay-events':
            res = await apiFetch(`${REPLAY_ENDPOINT}?source=${DEFAULT_REPLAY_SOURCE}`, {
              method: 'POST',
            })
            break
        }
        if (!res) {
          setLastActionResult({
            ok: false,
            message: `Unknown action: ${kind}`,
            attempted_at: Date.now() / 1000,
          })
          return
        }
        const ok = res.ok
        let body: { message?: string } = {}
        if (ok) {
          try {
            body = (await res.json()) as { message?: string }
          } catch {
            // Non-JSON response (e.g. 204 No Content) — fall back to a
            // generic success message.
            body = {}
          }
        }
        setLastActionResult({
          ok,
          message: ok
            ? body.message ?? `${kind} succeeded`
            : `${kind} failed: HTTP ${res.status} ${res.statusText}`,
          attempted_at: Date.now() / 1000,
        })
        // Re-fetch the dead-letter queue if the action touched it so
        // the operator sees the post-action queue depth immediately.
        if (kind === 'retry-failed' || kind === 'clear-dlq') {
          try {
            const dlqRes = await apiFetch(DEAD_LETTER_ENDPOINT)
            if (dlqRes.ok) setDeadLetter(await dlqRes.json())
          } catch {
            // Best-effort refresh — the next 15s poll will reconcile.
          }
        }
        // Always re-fetch the full operational state so the pipeline /
        // backfill / reliability surfaces reflect the post-action truth.
        fetchAll()
      } catch (e) {
        setLastActionResult({
          ok: false,
          message: e instanceof Error ? e.message : String(e),
          attempted_at: Date.now() / 1000,
        })
      } finally {
        setActionPending(false)
        setConfirmDialog(null)
      }
    },
    [fetchAll],
  )

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
  // W35-3 — Combine the fetchAll error and the useRealtimeData error
  // into a single banner source so a WS / health-fetch failure is
  // surfaced alongside the other-endpoint failures.
  const combinedError = error ?? healthError

  // ── Render: loading / error / data ──────────────────────────────────────

  if ((loading || healthLoading) && !health) {
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

  if (combinedError && !health) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4">
        <ErrorState message={combinedError} onRetry={handleManualRefresh} />
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
          {/* W35-3 — Live / Polling badge. Reflects the useRealtimeData
              transport state: "● Live" when the WS is connected and
              pushing `system` channel updates, "⟳ Polling" when the
              WS is handshaking / mid-reconnect / permanently failed
              (the hook falls back to 15s REST polling in that case). */}
          {isRealtime ? (
            <Badge
              variant="success"
              className="text-[9.5px] py-0.5"
              data-testid="realtime-badge"
            >
              ● Live
            </Badge>
          ) : (
            <Badge
              variant="warning"
              className="text-[9.5px] py-0.5"
              data-testid="poll-badge"
            >
              ⟳ Polling
            </Badge>
          )}
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
      {combinedError && health && (
        <div
          className="banner-danger text-xs py-2 px-3 flex items-center justify-between"
          role="alert"
        >
          <span className="flex items-center gap-1.5">
            <AlertTriangle size={12} aria-hidden="true" />
            <span>{combinedError}</span>
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

      {/* ── W35-3 — Live throughput sparkline ────────────────────────────── */}
      {/* Distinct from the backend-provided `throughput_trend` above: this
          sparkline is owned by the panel and appends one sample every
          time a new health payload arrives (REST prefetch, REST poll,
          OR WS push). It gives the operator a sense of how quickly the
          panel itself is receiving updates — a flat line means the WS
          is healthy and the rate is steady; a gap means a poll was
          skipped (tab hidden) or the WS stalled. */}
      <SectionCard
        icon={Radio}
        iconClass={isRealtime ? 'text-green-400' : 'text-amber-400'}
        title="Live Throughput"
        badge={
          <span
            className="text-[10px] text-[#7e8aaa] font-normal mono"
            data-testid="live-throughput-badge"
          >
            {liveEPSHistory.length}/{LIVE_EPS_MAX_SAMPLES} samples ·{' '}
            {isRealtime ? 'ws' : 'poll'} · Σ EPS
          </span>
        }
        data-testid="live-throughput-card"
      >
        {liveEPSHistory.length === 0 ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <span className="spinner" aria-hidden="true" />
            Waiting for first health snapshot…
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <RechartsSparkline
              data={liveEPSHistory}
              color={isRealtime ? '#22c55e' : '#f59e0b'}
              width="100%"
              height={48}
              showLastDot
            />
            <div
              className="flex justify-between text-[10px] text-[#7e8aaa] mono"
              data-testid="live-throughput-stats"
            >
              <span>
                min: {Math.min(...liveEPSHistory).toFixed(2)}
              </span>
              <span>
                max: {Math.max(...liveEPSHistory).toFixed(2)}
              </span>
              <span>
                last:{' '}
                {liveEPSHistory[liveEPSHistory.length - 1].toFixed(2)}
              </span>
            </div>
          </div>
        )}
      </SectionCard>

      {/* ── W35-3 — Live error feed ──────────────────────────────────────── */}
      {/* Scrolling tape of ingestion errors (newest at top, like a trade
          tape). Each new dead-letter item by id is prepended; older
          entries scroll off the bottom past
          LIVE_ERROR_FEED_MAX_ROWS. */}
      <SectionCard
        icon={AlertTriangle}
        iconClass="text-red-400"
        title="Live Error Feed"
        badge={
          <span
            className="text-[10px] text-[#7e8aaa] font-normal mono"
            data-testid="live-error-feed-count"
          >
            {errorFeed.length} event{errorFeed.length === 1 ? '' : 's'}
          </span>
        }
        data-testid="live-error-feed-card"
      >
        {errorFeed.length === 0 ? (
          <div
            className="text-xs text-green-400 py-3 flex items-center gap-2"
            data-testid="live-error-feed-empty"
          >
            <CheckCircle2 size={14} aria-hidden="true" />
            No ingestion errors observed yet.
          </div>
        ) : (
          <div
            className="max-h-64 overflow-y-auto scrollbar-thin"
            data-testid="live-error-feed-body"
            role="log"
            aria-live="off"
            aria-relevant="additions"
          >
            <ul className="divide-y divide-[#1f2335]/50">
              {errorFeed.map((e, i) => (
                <li
                  key={`${e.id}-${i}`}
                  className="px-2 py-1.5 flex items-start gap-2 text-xs hover:bg-[#13161e]/60 transition-colors"
                  data-testid="live-error-feed-row"
                  data-source={e.source}
                >
                  <span className="mono text-[10px] text-[#7e8aaa] shrink-0 w-16">
                    {formatRelativeTime(e.timestamp)}
                  </span>
                  <Badge
                    variant="secondary"
                    className="text-[9px] px-1.5 py-0 shrink-0"
                  >
                    {e.source}
                  </Badge>
                  <span
                    className="text-red-300 truncate flex-1"
                    title={e.message}
                  >
                    {e.message}
                  </span>
                  {e.retries > 0 && (
                    <span className="mono text-[9px] text-amber-400 shrink-0">
                      ×{e.retries}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </SectionCard>

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
          <div className="grid-kpi text-xs">
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

      {/* ── W39-7 — Pipeline Status ────────────────────────────────── */}
      {/* Live snapshot of the ingestion pipeline's running state + the
          Start / Stop controls. Split out from the old monolithic
          "Operational Controls" card (W39-1) so the pipeline state has
          its own scannable header instead of being buried inside a
          multi-purpose card. The Start / Stop buttons open the
          confirmation dialog (POST /api/ingestion/pipeline/{start,stop});
          the result surfaces in the action-result banner at the bottom
          of the Operational Controls card below. */}
      <SectionCard
        icon={Power}
        iconClass={
          pipelineStatus?.running ? 'text-green-400' : 'text-amber-400'
        }
        title="Pipeline Status"
        badge={
          pipelineStatus ? (
            <Badge
              variant={pipelineStatus.running ? 'success' : 'warning'}
              className="px-2 py-1 text-[9.5px] gap-1.5"
              data-testid="pipeline-running-badge"
            >
              {pipelineStatus.running ? (
                <CheckCircle2 size={11} aria-hidden="true" />
              ) : (
                <XCircle size={11} aria-hidden="true" />
              )}
              {pipelineStatus.running ? 'Running' : 'Stopped'}
            </Badge>
          ) : undefined
        }
        data-testid="pipeline-status-card"
      >
        {!pipelineStatus ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <AlertTriangle size={14} aria-hidden="true" />
            Pipeline status endpoint unavailable.
          </div>
        ) : (
          <div className="space-y-3">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5 text-xs">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  WS Loop
                </div>
                <div
                  className={`font-bold mono ${
                    pipelineStatus.ws_running ? 'text-green-400' : 'text-red-400'
                  }`}
                  data-testid="pipeline-ws-state"
                >
                  {pipelineStatus.ws_running ? 'Up' : 'Down'}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  REST Loop
                </div>
                <div
                  className={`font-bold mono ${
                    pipelineStatus.rest_running ? 'text-green-400' : 'text-red-400'
                  }`}
                  data-testid="pipeline-rest-state"
                >
                  {pipelineStatus.rest_running ? 'Up' : 'Down'}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  WS Reconnects
                </div>
                <div className="font-bold mono text-cyan-400">
                  {formatCount(pipelineStatus.ws_reconnect_count)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Tracked Tokens
                </div>
                <div className="font-bold mono text-cyan-400">
                  {formatCount(pipelineStatus.rest_tracked_tokens)}
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-[#1f2335]">
              <Button
                size="sm"
                variant="outline"
                disabled={pipelineStatus.running === true || actionPending}
                onClick={() => setConfirmDialog({ kind: 'start' })}
                className="h-7 px-3 text-xs border-[#1f2335] text-green-400 hover:bg-green-500/10 hover:text-green-300 hover:border-green-500/30 disabled:opacity-40"
                data-testid="btn-start-pipeline"
              >
                <Play size={12} aria-hidden="true" />
                Start
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={!pipelineStatus.running || actionPending}
                onClick={() => setConfirmDialog({ kind: 'stop' })}
                className="h-7 px-3 text-xs border-[#1f2335] text-amber-400 hover:bg-amber-500/10 hover:text-amber-300 hover:border-amber-500/30 disabled:opacity-40"
                data-testid="btn-stop-pipeline"
              >
                <Square size={12} aria-hidden="true" />
                Stop
              </Button>
              <span className="text-[10px] text-[#7e8aaa] mono ml-auto">
                started {formatRelativeTime(pipelineStatus.last_started_at)} ·{' '}
                stopped {formatRelativeTime(pipelineStatus.last_stopped_at)}
              </span>
            </div>
          </div>
        )}
      </SectionCard>

      {/* ── W39-7 — Source Reliability ─────────────────────────────────── */}
      {/* Per-source reliability snapshots (W34-5 ReliabilityStatus tracker).
          Each row shows the source name + status badge (uses
          reliabilityStatusVariant) and the composite score (uses
          reliabilityScoreColor). The avg score appears as the card's
          header badge. Empty state shows a placeholder when the backend
          has not recorded any reliability windows yet. */}
      <SectionCard
        icon={ShieldCheck}
        iconClass="text-cyan-400"
        title="Source Reliability"
        badge={
          reliability && reliability.count > 0 ? (
            <span
              className={`font-bold mono text-xs ${reliabilityScoreColor(
                reliability.avg_score,
              )}`}
              data-testid="reliability-avg-score"
            >
              avg {reliability.avg_score.toFixed(1)}
            </span>
          ) : (
            <span className="text-[10px] text-[#7e8aaa] font-normal mono">
              {reliability?.count ?? 0} source{(reliability?.count ?? 0) === 1 ? '' : 's'}
            </span>
          )
        }
        data-testid="reliability-card"
      >
        {!reliability || reliability.count === 0 ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <AlertTriangle size={14} aria-hidden="true" />
            No reliability snapshots reported. The backend's reliability
            tracker has not recorded any source windows yet.
          </div>
        ) : (
          <div
            className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5"
            data-testid="reliability-grid"
          >
            {Object.values(reliability.sources)
              .slice(0, 6)
              .map((s) => (
                <div
                  key={s.source}
                  className="bg-[#13161e] p-2.5 rounded border border-[#1f2335] text-xs"
                  data-testid={`reliability-row-${s.source}`}
                >
                  <div className="flex items-center justify-between gap-2 mb-1.5">
                    <span
                      className="mono text-[#dde1ed] truncate"
                      title={s.source}
                    >
                      {s.source}
                    </span>
                    <Badge
                      variant={reliabilityStatusVariant(s.status)}
                      className="px-1.5 py-0 text-[9px]"
                    >
                      {s.status}
                    </Badge>
                  </div>
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[10px] uppercase tracking-wider text-[#7e8aaa]">
                      Score
                    </span>
                    <span
                      className={`font-bold mono ${reliabilityScoreColor(s.score)}`}
                    >
                      {s.score.toFixed(1)}
                    </span>
                  </div>
                </div>
              ))}
          </div>
        )}
      </SectionCard>

      {/* ── W39-7 — Backfill Progress ─────────────────────────────────── */}
      {/* Recent backfill runs (W32-3 engine) + Launch Backfill button (POST
          /api/ingestion/backfill/markets?resume=true). Mirrors the DLQ
          card's recent-records pattern: type / started / processed / added
          / errors, capped at 3 rows. Empty state shows a placeholder when
          no runs have been recorded yet. */}
      <SectionCard
        icon={Database}
        iconClass="text-cyan-400"
        title="Backfill Progress"
        badge={
          backfillStatus ? (
            <span className="text-[10px] text-[#7e8aaa] font-normal mono">
              {backfillStatus.runs.length} run{backfillStatus.runs.length === 1 ? '' : 's'}
            </span>
          ) : undefined
        }
        data-testid="backfill-card"
      >
        {!backfillStatus ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <AlertTriangle size={14} aria-hidden="true" />
            Backfill status endpoint unavailable.
          </div>
        ) : backfillStatus.runs.length === 0 ? (
          <div className="text-xs text-[#7e8aaa] py-3 flex items-center gap-2">
            <Database size={14} aria-hidden="true" />
            No backfill runs recorded yet.
          </div>
        ) : (
          <div className="space-y-3">
            <div className="space-y-1">
              <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-1.5">
                Recent Runs ({Math.min(backfillStatus.runs.length, 3)} of{' '}
                {backfillStatus.runs.length})
              </div>
              {backfillStatus.runs.slice(0, 3).map((run) => (
                <div
                  key={run.id}
                  className="flex items-center justify-between gap-2 text-[10px] bg-[#13161e] px-2 py-1.5 rounded border border-[#1f2335]"
                  data-testid={`backfill-row-${run.id}`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <Badge
                      variant="secondary"
                      className="text-[9px] px-1.5 py-0 shrink-0"
                    >
                      {run.type}
                    </Badge>
                    <span className="mono text-[#7e8aaa] shrink-0">
                      {formatRelativeTime(run.started_at)}
                    </span>
                  </div>
                  <span
                    className={`mono shrink-0 ${
                      run.total_errors === 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                  >
                    {formatCount(run.total_added)} added ·{' '}
                    {formatCount(run.total_errors)} err
                  </span>
                </div>
              ))}
            </div>
            <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-[#1f2335]">
              <Button
                size="sm"
                variant="outline"
                disabled={actionPending}
                onClick={() => setConfirmDialog({ kind: 'launch-backfill' })}
                className="h-7 px-3 text-xs border-[#1f2335] text-cyan-400 hover:bg-cyan-500/10 hover:text-cyan-300 hover:border-cyan-500/30 disabled:opacity-40"
                data-testid="btn-launch-backfill"
              >
                <Zap size={12} aria-hidden="true" />
                Launch Backfill
              </Button>
            </div>
          </div>
        )}
      </SectionCard>

      {/* ── W39-7 — Operational Controls ──────────────────────────────── */}
      {/* Destructive / heavy-write actions that don't have a natural home
          in the read-only cards above. Each button opens the
          ConfirmationDialog (per the W38-8 dialog contract) and routes
          through `handleConfirmAction` → the WRITE endpoints. The inline
          last-action-result banner surfaces the post-action outcome so
          the operator sees success / failure without waiting for the
          next 15s poll tick. */}
      <SectionCard
        icon={Settings}
        iconClass="text-cyan-400"
        title="Operational Controls"
        data-testid="operational-controls-card"
      >
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button
              size="sm"
              variant="outline"
              disabled={
                actionPending || retrying || (deadLetter?.depth ?? 0) === 0
              }
              onClick={() => setConfirmDialog({ kind: 'retry-failed' })}
              className="h-7 px-3 text-xs border-[#1f2335] text-amber-400 hover:bg-amber-500/10 hover:text-amber-300 hover:border-amber-500/30 disabled:opacity-40"
              data-testid="btn-retry-failed"
            >
              <RefreshCw size={12} aria-hidden="true" />
              Retry Failed
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={actionPending || (deadLetter?.depth ?? 0) === 0}
              onClick={() => setConfirmDialog({ kind: 'clear-dlq' })}
              className="h-7 px-3 text-xs border-[#1f2335] text-red-400 hover:bg-red-500/10 hover:text-red-300 hover:border-red-500/30 disabled:opacity-40"
              data-testid="btn-clear-dlq"
            >
              <Trash2 size={12} aria-hidden="true" />
              Clear DLQ
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={actionPending}
              onClick={() => setConfirmDialog({ kind: 'replay-events' })}
              className="h-7 px-3 text-xs border-[#1f2335] text-cyan-400 hover:bg-cyan-500/10 hover:text-cyan-300 hover:border-cyan-500/30 disabled:opacity-40"
              data-testid="btn-replay-events"
            >
              <RotateCw size={12} aria-hidden="true" />
              Replay Events
            </Button>
            {actionPending && (
              <span
                className="text-[10px] text-[#7e8aaa] mono flex items-center gap-1.5 ml-auto"
                data-testid="action-pending-indicator"
              >
                <RefreshCw
                  size={10}
                  className="animate-spin"
                  aria-hidden="true"
                />
                executing…
              </span>
            )}
          </div>

          {/* Last action result — surfaced inline so the operator sees
              the post-action outcome without waiting for the next poll. */}
          {lastActionResult && (
            <div
              className={`px-3 py-2 rounded text-xs mono border flex items-center justify-between gap-2 ${
                lastActionResult.ok
                  ? 'bg-green-500/10 border-green-500/30 text-green-300'
                  : 'bg-red-500/10 border-red-500/30 text-red-300'
              }`}
              data-testid="last-action-result"
              role="status"
              aria-live="polite"
            >
              <span className="flex items-center gap-2 min-w-0">
                {lastActionResult.ok ? (
                  <CheckCircle2 size={12} aria-hidden="true" className="shrink-0" />
                ) : (
                  <AlertTriangle size={12} aria-hidden="true" className="shrink-0" />
                )}
                <span className="font-bold shrink-0">
                  {lastActionResult.ok ? 'OK' : 'FAIL'}
                </span>
                <span className="truncate" title={lastActionResult.message}>
                  {lastActionResult.message}
                </span>
              </span>
              <button
                type="button"
                onClick={() => setLastActionResult(null)}
                className="text-[#7e8aaa] hover:text-white transition-colors shrink-0 ml-2"
                aria-label="Dismiss action result"
              >
                ✕
              </button>
            </div>
          )}
        </div>
      </SectionCard>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      <div className="text-[10px] text-[#7e8aaa] mono text-center pt-1">
        Generated at {formatRelativeTime(Math.floor((lastUpdated ?? 0) / 1000))} · endpoints:{' '}
        <span className="text-cyan-400">{HEALTH_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{QUALITY_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{DEAD_LETTER_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{COVERAGE_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{GAPS_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{RELIABILITY_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{BACKFILL_STATUS_ENDPOINT}</span>,{' '}
        <span className="text-cyan-400">{PIPELINE_STATUS_ENDPOINT}</span>
      </div>

      {/* ── W38-6 — Confirmation dialog for the operational actions ── */}
      {confirmDialog && (
        <ConfirmationDialog
          open
          severity={
            confirmDialog.kind === 'clear-dlq'
              ? 'danger'
              : confirmDialog.kind === 'stop' || confirmDialog.kind === 'replay-events'
                ? 'warning'
                : 'info'
          }
          title={
            confirmDialog.kind === 'start'
              ? 'Start Ingestion?'
              : confirmDialog.kind === 'stop'
                ? 'Stop Ingestion?'
                : confirmDialog.kind === 'retry-failed'
                  ? 'Retry Failed Records?'
                  : confirmDialog.kind === 'launch-backfill'
                    ? 'Launch Backfill?'
                    : confirmDialog.kind === 'clear-dlq'
                      ? 'Clear Dead-Letter Queue?'
                      : 'Replay Events?'
          }
          description={
            confirmDialog.kind === 'start'
              ? 'This will flip the pipeline running flag to True and kick off the WS + REST ingestion sources.'
              : confirmDialog.kind === 'stop'
                ? 'This will flip the pipeline running flag to False and call stop() on the WS + REST ingestion sources. New ingestion events will pause until the pipeline is restarted.'
                : confirmDialog.kind === 'retry-failed'
                  ? 'This will POST to /api/ingestion/dead-letter/retry — every pending record in the dead-letter queue will be marked retried.'
                  : confirmDialog.kind === 'launch-backfill'
                    ? 'This will POST to /api/ingestion/backfill/markets?resume=true — the metadata backfill resumes from the last persisted checkpoint as a background asyncio task.'
                    : confirmDialog.kind === 'clear-dlq'
                      ? 'This will DELETE every record currently in the dead-letter queue. The rows are permanently removed (no audit trail). Use this only for records you have already reviewed.'
                      : `This will POST to /api/ingestion/replay?source=${DEFAULT_REPLAY_SOURCE} — every CLOB record in the raw vault will be re-fed through the ingestion pipeline. The dedup layer prevents duplicates from reaching downstream storage.`
          }
          impact={
            confirmDialog.kind === 'clear-dlq'
              ? `This will permanently delete ${
                  deadLetter?.depth ?? 0
                } record(s) from the dead-letter queue.`
              : confirmDialog.kind === 'stop'
                ? 'Ingestion will pause — no new market / trade / book updates will be processed until the pipeline is restarted.'
                : confirmDialog.kind === 'replay-events'
                  ? 'Replay runs inline — the POST blocks until every record is re-processed (default cap 1000 records).'
                  : undefined
          }
          confirmLabel={
            confirmDialog.kind === 'start'
              ? 'Start Ingestion'
              : confirmDialog.kind === 'stop'
                ? 'Stop Ingestion'
                : confirmDialog.kind === 'retry-failed'
                  ? 'Retry Failed Records'
                  : confirmDialog.kind === 'launch-backfill'
                    ? 'Launch Backfill'
                    : confirmDialog.kind === 'clear-dlq'
                      ? 'Clear Dead-Letter Queue'
                      : 'Replay Events'
          }
          cancelLabel="Cancel"
          onConfirm={() => void handleConfirmAction(confirmDialog.kind)}
          onCancel={() => setConfirmDialog(null)}
          loading={actionPending}
        />
      )}
    </div>
  )
}
