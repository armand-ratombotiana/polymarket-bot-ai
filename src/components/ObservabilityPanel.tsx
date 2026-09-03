// components/ObservabilityPanel.tsx — System Observability Dashboard (W8-8)
//
// Exposes the auto-collected system metrics backend (`core/observability.py`
// + `core/observability_collector.py`) — 23 metrics emitted every 30s across
// five canonical categories (DATA / BOT / EXECUTION / ML / SYSTEM).
//
// Visual language mirrors SystemHealthView.tsx (dark `#13161e` card surface,
// `#1f2335` borders, `#dde1ed` primary text) but layers in richer per-metric
// cards with sparklines, severity colour-coding, and a collapsible category
// section per source bucket. Polls `/api/observability` every 30s and pauses
// when the document is hidden.
'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Activity,
  Database,
  Bot,
  Cpu,
  Gauge,
  Search,
  ChevronDown,
  RefreshCw,
  AlertCircle,
  AlertTriangle,
  Inbox,
} from 'lucide-react'
import { Sparkline as RechartsSparkline } from '@/components/charts'

// ───────────────────────────────────────────────────────────────────────────
// Types — mirror the JSON shape returned by core/observability.py register_routes
// ───────────────────────────────────────────────────────────────────────────

/** Backend canonical categories + a generic `other`/`strategy` fallback. */
type MetricCategoryKey =
  | 'data_source'
  | 'bot'
  | 'execution'
  | 'ml'
  | 'system'
  | 'strategy'
  | 'other'

interface MetricEntry {
  value: number
  timestamp: number
  age_seconds: number
  metadata: Record<string, unknown> | null
}

interface HealthReport {
  generated_at: number
  category_count: number
  metric_count: number
  oldest_sample_age_seconds: number | null
  newest_sample_age_seconds: number | null
  categories: Record<string, Record<string, MetricEntry>>
}

interface HistorySample {
  timestamp: number
  category: string
  name: string
  value: number
  metadata: unknown
}

interface HistoryResponse {
  name: string
  count: number
  samples: HistorySample[]
}

type TimeRange = '1h' | '6h' | '24h' | '7d'
type Severity = 'normal' | 'warning' | 'critical' | 'unknown'

interface Threshold {
  /** Amber threshold (inclusive). */
  warn: number
  /** Red threshold (inclusive). */
  crit: number
  /** `higher-bad`: large values are bad (cpu, latency, drift). */
  dir: 'lower-bad' | 'higher-bad'
}

// ───────────────────────────────────────────────────────────────────────────
// Category metadata — colour-coding per the W8-8 spec:
//   DATA=blue · BOT=violet · EXECUTION=amber · ML=emerald · SYSTEM=gray
// ───────────────────────────────────────────────────────────────────────────

interface CategoryMeta {
  key: string
  label: string
  icon: typeof Activity
  textClass: string
  badgeClass: string
  borderClass: string
  stroke: string // hex colour fed to the SVG sparkline polyline
}

const CATEGORY_META: CategoryMeta[] = [
  {
    key: 'data_source',
    label: 'DATA',
    icon: Database,
    textClass: 'text-blue-400',
    badgeClass: 'badge-blue',
    borderClass: 'border-l-blue-500/50',
    stroke: '#60a5fa',
  },
  {
    key: 'bot',
    label: 'BOT',
    icon: Bot,
    textClass: 'text-purple-400',
    badgeClass: 'badge-purple',
    borderClass: 'border-l-purple-500/50',
    stroke: '#c084fc',
  },
  {
    key: 'execution',
    label: 'EXECUTION',
    icon: Activity,
    textClass: 'text-amber-400',
    badgeClass: 'badge-amber',
    borderClass: 'border-l-amber-500/50',
    stroke: '#fbbf24',
  },
  {
    key: 'ml',
    label: 'ML',
    icon: Cpu,
    textClass: 'text-emerald-400',
    badgeClass: 'badge-green',
    borderClass: 'border-l-emerald-500/50',
    stroke: '#4ade80',
  },
  {
    key: 'system',
    label: 'SYSTEM',
    icon: Gauge,
    textClass: 'text-gray-400',
    badgeClass: 'badge-dim',
    borderClass: 'border-l-gray-500/50',
    stroke: '#9ca3af',
  },
]

const FALLBACK_META: CategoryMeta = {
  key: 'other',
  label: 'OTHER',
  icon: Activity,
  textClass: 'text-cyan-400',
  badgeClass: 'badge-cyan',
  borderClass: 'border-l-cyan-500/50',
  stroke: '#22d3ee',
}

function getCategoryMeta(key: string): CategoryMeta {
  return CATEGORY_META.find((c) => c.key === key) ?? { ...FALLBACK_META, key }
}

// ───────────────────────────────────────────────────────────────────────────
// Per-metric units & thresholds
// ───────────────────────────────────────────────────────────────────────────

const METRIC_UNITS: Record<string, string> = {
  // data_source
  updates: 'count',
  errors: 'count',
  tracked_tokens: 'count',
  staleness: 's',
  // bot
  cycles: 'count',
  // execution
  submissions: 'count',
  fills: 'count',
  rejections: 'count',
  positions: 'count',
  paper_balance: '$',
  daily_pnl: '$',
  slippage: '$',
  // ml
  inference_latency: 'ms',
  prediction_distribution: 'score',
  drift: 'PSI',
  brier_score: 'score',
  ece: 'score',
  roc_auc: 'score',
  is_fitted: 'bool',
  n_updates: 'count',
  seconds_since_last_trained: 's',
  // system
  cpu_percent: '%',
  memory_percent: '%',
  memory_used_mb: 'MB',
}

const METRIC_THRESHOLDS: Record<string, Threshold> = {
  cpu_percent:               { warn: 70,     crit: 90,     dir: 'higher-bad' },
  memory_percent:           { warn: 70,     crit: 90,     dir: 'higher-bad' },
  staleness:                { warn: 60,     crit: 300,    dir: 'higher-bad' },
  errors:                   { warn: 5,      crit: 20,     dir: 'higher-bad' },
  drift:                    { warn: 0.10,   crit: 0.25,   dir: 'higher-bad' },
  brier_score:              { warn: 0.25,   crit: 0.33,   dir: 'higher-bad' },
  ece:                      { warn: 0.05,   crit: 0.10,   dir: 'higher-bad' },
  roc_auc:                  { warn: 0.65,   crit: 0.55,   dir: 'lower-bad'  },
  slippage:                 { warn: -0.01,  crit: -0.05,  dir: 'lower-bad'  },
  daily_pnl:                { warn: -1,     crit: -5,     dir: 'lower-bad'  },
  seconds_since_last_trained: { warn: 86400, crit: 604800, dir: 'higher-bad' },
}

const TIME_RANGE_LIMITS: Record<TimeRange, number> = {
  '1h': 120,   // 30s interval × 120 = 1h
  '6h': 720,
  '24h': 1000, // capped at backend max
  '7d': 1000,
}

const REFRESH_INTERVAL_MS = 30_000
const POLL_INTERVAL_MS = 30_000

// ───────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ───────────────────────────────────────────────────────────────────────────

function getSeverity(name: string, value: number): Severity {
  const t = METRIC_THRESHOLDS[name]
  if (!t || !Number.isFinite(value)) return 'unknown'
  if (t.dir === 'higher-bad') {
    if (value >= t.crit) return 'critical'
    if (value >= t.warn) return 'warning'
    return 'normal'
  } else {
    if (value <= t.crit) return 'critical'
    if (value <= t.warn) return 'warning'
    return 'normal'
  }
}

function severityTextClass(s: Severity): string {
  switch (s) {
    case 'normal':   return 'text-emerald-400'
    case 'warning':  return 'text-amber-400'
    case 'critical': return 'text-red-400'
    default:         return 'text-[#dde1ed]'
  }
}

function formatMetricValue(name: string, value: number): string {
  if (!Number.isFinite(value)) return '—'
  const unit = METRIC_UNITS[name]
  switch (unit) {
    case 'count':
      return Math.round(value).toLocaleString('en-US')
    case '%':
      return `${value.toFixed(1)}%`
    case '$': {
      const sign = value < 0 ? '−' : ''
      return `${sign}$${Math.abs(value).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`
    }
    case 'MB':
      return `${value.toFixed(0)} MB`
    case 's': {
      if (value >= 86400) return `${(value / 86400).toFixed(1)}d`
      if (value >= 3600)  return `${(value / 3600).toFixed(1)}h`
      if (value >= 60)    return `${Math.floor(value / 60)}m`
      return `${value.toFixed(0)}s`
    }
    case 'ms':
      return `${value.toFixed(0)}ms`
    case 'bool':
      return value >= 0.5 ? 'YES' : 'NO'
    case 'score':
    case 'PSI':
      return value.toFixed(4)
    default:
      return value.toFixed(3)
  }
}

function getUnitLabel(name: string): string {
  const u = METRIC_UNITS[name]
  if (!u) return 'value'
  if (u === 'bool') return 'flag'
  if (u === 'score' || u === 'PSI') return 'score'
  if (u === 'count') return 'count'
  return u
}

function formatAge(epochSec: number | null): string {
  if (epochSec == null) return '—'
  const diff = Date.now() / 1000 - epochSec
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

function formatClock(epochSec: number | null): string {
  if (epochSec == null) return '—'
  return new Date(epochSec * 1000).toISOString().slice(11, 19) + ' UTC'
}

function formatDuration(sec: number | null): string {
  if (sec == null || !Number.isFinite(sec)) return '—'
  if (sec < 60) return `${Math.floor(sec)}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`
  return `${Math.floor(sec / 86400)}d`
}

// ───────────────────────────────────────────────────────────────────────────
// Sparkline — thin wrapper around @/components/charts Sparkline (Recharts).
// Maintains the legacy local API (samples: HistorySample[]) so call sites in
// this file don't need to change. The Recharts Sparkline handles the actual
// rendering with the dashboard theme.
// ───────────────────────────────────────────────────────────────────────────

interface SparklineProps {
  samples: HistorySample[]
  width?: number
  height?: number
  color?: string
}

function Sparkline({
  samples,
  width = 60,
  height = 24,
  color = '#60a5fa',
}: SparklineProps) {
  // API returns newest-first; we draw oldest→newest (left→right).
  const ordered = samples ? [...samples].reverse() : []
  const values = ordered.map((s) => s.value)
  return (
    <RechartsSparkline
      data={values}
      color={color}
      width={width}
      height={height}
      strokeWidth={1.4}
      showLastDot
      className="flex-shrink-0"
    />
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Main panel
// ───────────────────────────────────────────────────────────────────────────

export default function ObservabilityPanel() {
  const [report, setReport] = useState<HealthReport | null>(null)
  const [histories, setHistories] = useState<Record<string, HistorySample[]>>({})
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [search, setSearch] = useState('')
  const [activeCats, setActiveCats] = useState<Set<string>>(
    () => new Set(CATEGORY_META.map((c) => c.key))
  )
  const [timeRange, setTimeRange] = useState<TimeRange>('1h')
  const [openSections, setOpenSections] = useState<Record<string, boolean>>(
    () => Object.fromEntries(CATEGORY_META.map((c) => [c.key, true]))
  )
  const fetchingRef = useRef(false)

  // ── Fetchers ────────────────────────────────────────────────────────────

  const fetchReport = useCallback(async () => {
    try {
      const res = await apiFetch('/api/observability')
      if (!res.ok) throw new Error(`HTTP ${res.status} ${res.statusText}`)
      const data = (await res.json()) as HealthReport
      setReport(data)
      setError(null)
      setLastUpdated(Date.now())
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    }
  }, [])

  const fetchHistories = useCallback(
    async (names: string[], range: TimeRange) => {
      if (names.length === 0) {
        setHistories({})
        return
      }
      const limit = TIME_RANGE_LIMITS[range]
      // Fire all history requests in parallel — 23 metrics is well within
      // browser concurrent-request budgets and the backend SQLite WAL
      // handles parallel reads comfortably.
      const entries = await Promise.all(
        names.map(async (n): Promise<[string, HistorySample[]]> => {
          try {
            const res = await apiFetch(
              `/api/observability/history/${encodeURIComponent(n)}?limit=${limit}`
            )
            if (!res.ok) return [n, []]
            const data = (await res.json()) as HistoryResponse
            return [n, data.samples ?? []]
          } catch {
            return [n, []]
          }
        })
      )
      setHistories(Object.fromEntries(entries))
    },
    []
  )

  const refresh = useCallback(async () => {
    if (fetchingRef.current) return
    fetchingRef.current = true
    setRefreshing(true)
    await fetchReport()
    setRefreshing(false)
    fetchingRef.current = false
  }, [fetchReport])

  // ── Polling loop (30s, paused when document hidden) ─────────────────────

  useEffect(() => {
    let cancelled = false

    const tick = async () => {
      if (typeof document !== 'undefined' && document.hidden) return
      if (fetchingRef.current) return
      fetchingRef.current = true
      setRefreshing(true)
      await fetchReport()
      if (!cancelled) {
        setRefreshing(false)
        setLoading(false)
      }
      fetchingRef.current = false
    }

    tick()
    const interval = setInterval(tick, POLL_INTERVAL_MS)

    const onVisibility = () => {
      if (!document.hidden) tick()
    }
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      cancelled = true
      clearInterval(interval)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fetchReport])

  // ── Refetch sparklines when the metric set or time range changes ────────

  useEffect(() => {
    if (!report) return
    const names = new Set<string>()
    Object.values(report.categories).forEach((bucket) => {
      Object.keys(bucket).forEach((n) => names.add(n))
    })
    fetchHistories(Array.from(names), timeRange)
  }, [report, timeRange, fetchHistories])

  // ── Derived state ──────────────────────────────────────────────────────

  /** Set of categories that actually have ≥1 metric sample in the latest report. */
  const populatedCats = useMemo(() => {
    if (!report) return new Set<string>()
    const s = new Set<string>()
    Object.entries(report.categories).forEach(([cat, bucket]) => {
      if (Object.keys(bucket).length > 0) s.add(cat)
    })
    return s
  }, [report])

  /** Categories present in the report but not in our canonical 5 (e.g. strategy/other). */
  const extraCats = useMemo(() => {
    if (!report) return [] as string[]
    const known = new Set(CATEGORY_META.map((c) => c.key))
    return Object.keys(report.categories).filter(
      (k) => !known.has(k) && Object.keys(report.categories[k]).length > 0
    )
  }, [report])

  const filteredGroups = useMemo(() => {
    if (!report) {
      return [] as { meta: CategoryMeta; metrics: { name: string; entry: MetricEntry }[] }[]
    }
    const q = search.trim().toLowerCase()
    const out: { meta: CategoryMeta; metrics: { name: string; entry: MetricEntry }[] }[] = []

    // Iterate canonical categories in display order, then append any extras.
    const orderedKeys = [
      ...CATEGORY_META.map((c) => c.key),
      ...extraCats,
    ]

    for (const key of orderedKeys) {
      if (!activeCats.has(key)) continue
      const bucket = report.categories[key] ?? {}
      const entries = Object.entries(bucket)
        .map(([name, entry]) => ({ name, entry }))
        .filter(({ name }) => !q || name.toLowerCase().includes(q))
        .sort((a, b) => a.name.localeCompare(b.name))
      if (entries.length === 0) continue
      out.push({ meta: getCategoryMeta(key), metrics: entries })
    }
    return out
  }, [report, search, activeCats, extraCats])

  // ── Handlers ───────────────────────────────────────────────────────────

  const toggleCat = (k: string) => {
    setActiveCats((prev) => {
      const next = new Set(prev)
      if (next.has(k)) next.delete(k)
      else next.add(k)
      return next
    })
  }

  const toggleSection = (k: string) => {
    setOpenSections((prev) => ({ ...prev, [k]: !(prev[k] ?? true) }))
  }

  // ── Render: loading skeleton ───────────────────────────────────────────

  if (loading && !report) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f2335]">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-400" />
            <span className="text-sm font-bold text-[#dde1ed]">System Observability</span>
          </div>
          <span className="spinner" aria-hidden="true" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton-card p-2.5">
              <div className="skeleton-line" />
              <div className="skeleton-line-lg" />
              <div className="skeleton-line" />
            </div>
          ))}
        </div>
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton-card p-2.5">
              <div className="skeleton-line-lg" />
            </div>
          ))}
        </div>
      </div>
    )
  }

  // ── Render: hard error (no data yet) ────────────────────────────────────

  if (error && !report) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-[#7e8aaa] gap-2 p-6">
        <AlertCircle className="w-5 h-5 text-red-400" />
        <div className="text-red-400 font-semibold">Observability endpoint unavailable</div>
        <div className="mono text-[10px] text-[#3e4560] max-w-md text-center break-all">
          {error}
        </div>
        <button
          onClick={() => refresh()}
          className="btn btn-ghost btn-sm mt-2 text-xs"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      </div>
    )
  }

  // ── Render: empty state (collector hasn't emitted yet) ───────────────────

  if (report && report.metric_count === 0) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f2335]">
          <div>
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-blue-400" />
              <span className="text-sm font-bold text-[#dde1ed]">System Observability</span>
              <span className="badge badge-dim text-[9.5px]">30s poll</span>
            </div>
            <p className="text-xs text-[#7e8aaa] mt-0.5">
              Auto-collected system metrics · {report.category_count} categories tracked
            </p>
          </div>
        </div>
        <div className="flex-1 flex flex-col items-center justify-center text-xs text-[#7e8aaa] gap-2 py-12">
          <Inbox className="w-8 h-8 opacity-30" />
          <div className="text-sm font-semibold text-[#dde1ed]">No metrics collected yet</div>
          <div className="text-[11px] text-center max-w-sm">
            The auto-collector emits metrics every 30 seconds after backend startup.
            If this persists, verify the backend service is running and
            <code className="mono text-[#c8cfe0] mx-1">observability-collector</code>
            is wired into the FastAPI lifespan.
          </div>
          <button
            onClick={() => refresh()}
            className="btn btn-ghost btn-sm mt-3 text-xs"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Check again
          </button>
        </div>
      </div>
    )
  }

  // ── Render: main panel ─────────────────────────────────────────────────

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="flex flex-wrap justify-between items-center gap-2 p-4 pb-2 border-b border-[#1f2335]">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-blue-400 flex-shrink-0" />
            <h2 className="text-sm font-bold text-[#dde1ed]">System Observability</h2>
            <span className="badge badge-dim text-[9.5px]">30s poll</span>
            {refreshing && (
              <span className="badge badge-blue text-[9.5px]">
                <RefreshCw className="w-2.5 h-2.5 animate-spin" />
                syncing
              </span>
            )}
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5 truncate">
            Auto-collected metrics · {report?.metric_count ?? 0} metrics ·{' '}
            {populatedCats.size} active categories
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <label className="flex items-center gap-1.5 text-[10.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
            <span>Range</span>
            <Select
              value={timeRange}
              onValueChange={(v) => setTimeRange(v as TimeRange)}
            >
              <SelectTrigger
                size="sm"
                className="h-7 text-xs bg-[#0e1015] border-[#1f2335] text-[#dde1ed] hover:border-[#2d3450] data-[size=sm]:h-7 w-[100px]"
                aria-label="Sparkline time range"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent className="bg-[#0e1015] border-[#1f2335] text-[#dde1ed]">
                <SelectItem value="1h" className="text-xs focus:bg-[#1a1f2e] focus:text-[#dde1ed]">
                  Last 1h
                </SelectItem>
                <SelectItem value="6h" className="text-xs focus:bg-[#1a1f2e] focus:text-[#dde1ed]">
                  Last 6h
                </SelectItem>
                <SelectItem value="24h" className="text-xs focus:bg-[#1a1f2e] focus:text-[#dde1ed]">
                  Last 24h
                </SelectItem>
                <SelectItem value="7d" className="text-xs focus:bg-[#1a1f2e] focus:text-[#dde1ed]">
                  Last 7d
                </SelectItem>
              </SelectContent>
            </Select>
          </label>
          <button
            onClick={() => refresh()}
            disabled={refreshing}
            className="btn btn-ghost btn-sm flex items-center gap-1.5 text-xs"
            aria-label="Refresh observability data"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>
      </header>

      {/* ── KPI strip ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5 p-4 pt-3">
        <div className="kpi-card">
          <span className="kpi-label">Total Metrics</span>
          <span className="kpi-value text-blue-400">
            {report?.metric_count ?? 0}
          </span>
          <span className="kpi-sub">{report?.category_count ?? 0} categories</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Newest Sample</span>
          <span className="kpi-value text-emerald-400">
            {formatDuration(report?.newest_sample_age_seconds ?? null)}
          </span>
          <span className="kpi-sub">since last emit</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Oldest Sample</span>
          <span className="kpi-value text-amber-400">
            {formatDuration(report?.oldest_sample_age_seconds ?? null)}
          </span>
          <span className="kpi-sub">window span</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label">Last Refresh</span>
          <span className="kpi-value text-cyan-400">
            {lastUpdated ? new Date(lastUpdated).toISOString().slice(11, 19) : '—'}
          </span>
          <span className="kpi-sub">
            {refreshing ? 'refreshing…' : formatAge((lastUpdated ?? 0) / 1000)}
          </span>
        </div>
      </div>

      {/* ── Filter bar (search + category toggles) ───────────────────── */}
      <div className="flex flex-wrap items-center gap-2 px-4 pb-2">
        <div className="relative flex-1 min-w-[180px] max-w-md">
          <Search className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-[#7e8aaa] pointer-events-none" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search metrics by name…"
            aria-label="Filter metrics by name"
            className="input input-sm pl-8 bg-[#0e1015] border-[#1f2335] text-[#dde1ed] placeholder:text-[#3e4560]"
          />
        </div>
        <div className="flex items-center gap-1 flex-wrap" role="group" aria-label="Category filters">
          {CATEGORY_META.map((c) => {
            const active = activeCats.has(c.key)
            const hasData = populatedCats.has(c.key)
            const Icon = c.icon
            return (
              <button
                key={c.key}
                onClick={() => toggleCat(c.key)}
                disabled={!hasData}
                className={`badge text-[9.5px] ${
                  active ? c.badgeClass : 'badge-dim'
                } ${!hasData ? 'opacity-30 cursor-not-allowed' : 'cursor-pointer hover:scale-[1.03]'} transition-transform`}
                title={`${c.label} category (${hasData ? 'has data' : 'no data'})`}
                aria-pressed={active}
                aria-label={`Toggle ${c.label} category`}
              >
                <Icon className="w-3 h-3" />
                {c.label}
              </button>
            )
          })}
          {extraCats.map((k) => {
            const meta = getCategoryMeta(k)
            const active = activeCats.has(k)
            const Icon = meta.icon
            return (
              <button
                key={k}
                onClick={() => toggleCat(k)}
                className={`badge text-[9.5px] ${
                  active ? meta.badgeClass : 'badge-dim'
                } cursor-pointer hover:scale-[1.03] transition-transform`}
                title={`${meta.label} category (ad-hoc)`}
                aria-pressed={active}
              >
                <Icon className="w-3 h-3" />
                {meta.label}
              </button>
            )
          })}
        </div>
      </div>

      {/* ── Soft error banner (stale data) ───────────────────────────── */}
      {error && (
        <div className="banner-warning mx-4 mb-2 text-xs">
          <AlertTriangle className="w-3.5 h-3.5 flex-shrink-0" />
          <span>
            Last refresh failed: <span className="mono">{error}</span> · showing previous data
          </span>
        </div>
      )}

      {/* ── Metric categories (collapsible sections) ──────────────────── */}
      <div className="flex-1 overflow-y-auto scrollbar-thin px-4 pb-4 space-y-2">
        {filteredGroups.length === 0 && (
          <div className="flex flex-col items-center justify-center py-12 text-xs text-[#7e8aaa] gap-2">
            <Search className="w-5 h-5 opacity-30" />
            <div>No metrics match the current filter.</div>
            {search && (
              <button
                onClick={() => setSearch('')}
                className="btn btn-ghost btn-xs text-[11px] mt-1"
              >
                Clear search
              </button>
            )}
          </div>
        )}

        {filteredGroups.map(({ meta, metrics }) => {
          const Icon = meta.icon
          const isOpen = openSections[meta.key] ?? true
          return (
            <Collapsible
              key={meta.key}
              open={isOpen}
              onOpenChange={() => toggleSection(meta.key)}
              className={`card border-l-2 ${meta.borderClass}`}
            >
              <CollapsibleTrigger className="w-full flex items-center justify-between p-2.5 cursor-pointer hover:bg-[#1a1f2e]/50 transition-colors">
                <div className="flex items-center gap-2">
                  <Icon className={`w-4 h-4 ${meta.textClass}`} />
                  <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
                    {meta.label}
                  </span>
                  <span className="badge badge-dim text-[9px]">{metrics.length}</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="text-[10px] text-[#7e8aaa] mono">
                    {metrics.length} metric{metrics.length === 1 ? '' : 's'}
                  </span>
                  <ChevronDown
                    className={`w-4 h-4 text-[#7e8aaa] transition-transform duration-200 ${
                      isOpen ? 'rotate-180' : ''
                    }`}
                  />
                </div>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="p-2.5 pt-1 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-2">
                  {metrics.map(({ name, entry }) => {
                    const sev = getSeverity(name, entry.value)
                    const hist = histories[name] ?? []
                    return (
                      <div
                        key={`${meta.key}:${name}`}
                        className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5 flex flex-col gap-1.5 hover:border-[#2d3450] transition-colors"
                      >
                        <div className="flex items-center justify-between gap-2">
                          <span
                            className="text-[10.5px] font-semibold text-[#c8cfe0] truncate mono"
                            title={name}
                          >
                            {name}
                          </span>
                          <span
                            className={`badge ${meta.badgeClass} text-[8px] px-1 py-0`}
                          >
                            {meta.label}
                          </span>
                        </div>
                        <div className="flex items-end justify-between gap-2">
                          <div className="flex flex-col min-w-0">
                            <span
                              className={`text-base font-bold mono leading-tight truncate ${severityTextClass(sev)}`}
                              title={`${entry.value}`}
                            >
                              {formatMetricValue(name, entry.value)}
                            </span>
                            <span className="text-[9.5px] text-[#3e4560] mono">
                              {getUnitLabel(name)}
                            </span>
                          </div>
                          {/* W13-9 — Recharts-backed sparkline (via the local
                              Sparkline wrapper, which now delegates to
                              @/components/charts Sparkline). */}
                          <Sparkline samples={hist} color={meta.stroke} />
                        </div>
                        <div className="flex items-center justify-between text-[9.5px] text-[#7e8aaa] mt-0.5">
                          <span className="mono" title="Sample timestamp (UTC)">
                            {formatClock(entry.timestamp)}
                          </span>
                          <span className="mono" title="Age of latest sample">
                            {formatAge(entry.timestamp)}
                          </span>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </CollapsibleContent>
            </Collapsible>
          )
        })}
      </div>
    </div>
  )
}
