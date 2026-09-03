// components/ExecutionQualityPanel.tsx — Per-fill Execution Quality Telemetry
//   Exposes the backend execution_quality ledger (slippage / latency /
//   realized-edge) as an institutional-grade execution analytics panel.
//   Backed by GET /api/execution-quality (see core/execution_quality.py).
'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  Clock,
  Gauge,
  RefreshCw,
  Target,
  TrendingDown,
  TrendingUp,
  Zap,
} from 'lucide-react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtAge, fmtPnl, fmtPrice, fmtUsd } from '@/lib/design-tokens'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'

// ── Types ────────────────────────────────────────────────────────────────
/** One row of `execution_quality` table — mirrors the SQLite schema in
 * `core/execution_quality.py::register_routes` recent-fills slice. */
export interface ExecutionQualityFill {
  id: number
  timestamp: number // epoch seconds
  order_id: string
  decision_id: string | null
  token_id: string | null
  strategy: string | null
  side: string | null // 'BUY' | 'SELL' | ''
  signal_price: number
  decision_price: number
  submitted_price: number
  best_bid: number | null
  best_ask: number | null
  expected_fill: number
  actual_fill: number
  spread: number | null
  slippage: number // signed: positive = adverse
  slippage_bps: number // signed bps
  latency_ms: number
  realized_edge: number // signed $ per fill
  paper: number // 0/1
  data_json: string | null
}

/** Aggregate stats returned by `get_execution_stats()` under `stats`. */
export interface ExecutionQualityStats {
  count: number
  strategy: string | null
  time_window_seconds: number | null
  avg_slippage_bps: number
  median_slippage_bps: number
  p95_slippage_bps: number
  worst_slippage_bps: number
  avg_latency_ms: number
  avg_realized_edge: number
  total_realized_edge: number
  by_side: { BUY: number; SELL: number }
}

/** Full API response envelope. */
interface ExecutionQualityResponse {
  stats: ExecutionQualityStats
  recent_fills: ExecutionQualityFill[]
}

type TimeRange = '1h' | '24h' | '7d'

const TIME_RANGES: { value: TimeRange; label: string; seconds: number }[] = [
  { value: '1h', label: '1 Hour', seconds: 3600 },
  { value: '24h', label: '24 Hours', seconds: 86400 },
  { value: '7d', label: '7 Days', seconds: 604800 },
]

const POLL_INTERVAL_MS = 15_000
const MAX_FILLS = 200

// ── Helpers ───────────────────────────────────────────────────────────────
function fmtBps(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const sign = v > 0 ? '+' : v < 0 ? '−' : ''
  return `${sign}${Math.abs(v).toFixed(digits)} bps`
}

function fmtMs(v: number | null | undefined, digits = 0): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${v.toFixed(digits)} ms`
}

/** Slippage colour: <5 bps green, 5–20 bps amber, >20 bps red. */
function slippageColorClass(bps: number): string {
  const abs = Math.abs(bps)
  if (abs < 5) return 'text-green-400'
  if (abs < 20) return 'text-amber-400'
  return 'text-red-400'
}

function slippageBadgeClass(bps: number): string {
  const abs = Math.abs(bps)
  if (abs < 5) return 'badge badge-green'
  if (abs < 20) return 'badge badge-amber'
  return 'badge badge-red'
}

function realizedEdgeClass(v: number): string {
  return v > 0 ? 'text-green-400' : v < 0 ? 'text-red-400' : 'text-[#7e8aaa]'
}

function median(values: number[]): number {
  if (!values.length) return 0
  const s = [...values].sort((a, b) => a - b)
  const mid = Math.floor(s.length / 2)
  return s.length % 2 === 0 ? (s[mid - 1] + s[mid]) / 2 : s[mid]
}

/** Bucket slippage (absolute bps) into the 5-bucket histogram. */
interface SlippageBucket {
  label: string
  range: string
  count: number
  color: string
  barClass: string
}

function computeHistogram(fills: ExecutionQualityFill[]): SlippageBucket[] {
  const buckets: SlippageBucket[] = [
    { label: '0–5', range: 'Excellent', count: 0, color: 'text-green-400', barClass: 'bg-green-500/60' },
    { label: '5–10', range: 'Good', count: 0, color: 'text-green-400', barClass: 'bg-green-500/40' },
    { label: '10–20', range: 'Acceptable', count: 0, color: 'text-amber-400', barClass: 'bg-amber-500/60' },
    { label: '20–50', range: 'Poor', count: 0, color: 'text-red-400', barClass: 'bg-red-500/60' },
    { label: '50+', range: 'Severe', count: 0, color: 'text-red-400', barClass: 'bg-red-500/80' },
  ]
  for (const f of fills) {
    const a = Math.abs(f.slippage_bps ?? 0)
    if (a < 5) buckets[0].count++
    else if (a < 10) buckets[1].count++
    else if (a < 20) buckets[2].count++
    else if (a < 50) buckets[3].count++
    else buckets[4].count++
  }
  return buckets
}

/** Build SVG sparkline path for latency over recent fills (oldest → newest). */
function sparklinePath(values: number[], w: number, h: number, pad = 2): string {
  if (values.length < 2) return ''
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const stepX = (w - pad * 2) / (values.length - 1)
  return values
    .map((v, i) => {
      const x = pad + i * stepX
      const y = pad + (1 - (v - min) / range) * (h - pad * 2)
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
}

// ── Component ─────────────────────────────────────────────────────────────
export default function ExecutionQualityPanel() {
  const [data, setData] = useState<ExecutionQualityResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchData = useCallback(async () => {
    const range = TIME_RANGES.find((r) => r.value === timeRange) ?? TIME_RANGES[1]
    const url = `${getApiUrl()}/api/execution-quality?time_window_seconds=${range.seconds}&limit=${MAX_FILLS}`
    try {
      setError(null)
      const res = await apiFetch(url)
      if (!res.ok) {
        const txt = await res.text().catch(() => '')
        throw new Error(`HTTP ${res.status}${txt ? ` — ${txt.slice(0, 200)}` : ''}`)
      }
      const json = (await res.json()) as ExecutionQualityResponse
      setData(json)
      setLastUpdated(Date.now())
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    } finally {
      setLoading(false)
      setIsRefreshing(false)
    }
  }, [timeRange])

  // Initial + range-change fetch
  useEffect(() => {
    setLoading(true)
    fetchData()
  }, [fetchData])

  // Polling with document-hidden pause
  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current)
    timerRef.current = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      setIsRefreshing(true)
      fetchData()
    }, POLL_INTERVAL_MS)
    const onVisibility = () => {
      if (typeof document !== 'undefined' && !document.hidden) {
        setIsRefreshing(true)
        fetchData()
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      if (timerRef.current) clearInterval(timerRef.current)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [fetchData])

  // ── Derived metrics ──────────────────────────────────────────────────────
  const fills = data?.recent_fills ?? []
  const stats = data?.stats

  const derived = useMemo(() => {
    const latencies = fills.map((f) => f.latency_ms ?? 0).filter((v) => Number.isFinite(v))
    const medianLat = median(latencies)
    const totalRealized = stats?.total_realized_edge ?? 0
    const totalFills = stats?.count ?? fills.length
    const buyCount = stats?.by_side?.BUY ?? 0
    const sellCount = stats?.by_side?.SELL ?? 0
    // Fill rate: rows with a valid BUY/SELL side and non-zero actual_fill price.
    const validFills = fills.filter(
      (f) => (f.side === 'BUY' || f.side === 'SELL') && f.actual_fill && f.actual_fill > 0,
    ).length
    const fillRate = fills.length > 0 ? (validFills / fills.length) * 100 : 0
    const histogram = computeHistogram(fills)
    // Worst executions: top 5 by adverse slippage (signed bps desc → most adverse first).
    const worst = [...fills].sort((a, b) => (b.slippage_bps ?? 0) - (a.slippage_bps ?? 0)).slice(0, 5)
    // Latency timeline: most recent 40 fills, oldest → newest for the sparkline.
    const latencyTimeline = [...fills]
      .sort((a, b) => a.timestamp - b.timestamp)
      .slice(-40)
      .map((f) => f.latency_ms ?? 0)
    return {
      medianLat,
      totalRealized,
      totalFills,
      buyCount,
      sellCount,
      fillRate,
      histogram,
      worst,
      latencyTimeline,
    }
  }, [fills, stats])

  // ── Loading skeleton ─────────────────────────────────────────────────────
  if (loading && !data) {
    return (
      <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl space-y-3">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f2335]">
          <div className="skeleton-line" style={{ width: '220px', height: '14px' }} />
          <div className="skeleton-line" style={{ width: '90px', height: '24px' }} />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="skeleton-card p-2 space-y-1.5">
              <div className="skeleton-line" style={{ width: '70%', height: '9px' }} />
              <div className="skeleton-line" style={{ width: '90%', height: '16px' }} />
              <div className="skeleton-line" style={{ width: '60%', height: '9px' }} />
            </div>
          ))}
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <div className="skeleton-card p-3 space-y-2">
            <div className="skeleton-line" style={{ width: '50%', height: '12px' }} />
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="skeleton-line-lg" />
            ))}
          </div>
          <div className="skeleton-card p-3 space-y-2">
            <div className="skeleton-line" style={{ width: '50%', height: '12px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '80px' }} />
          </div>
        </div>
        <div className="skeleton-card p-3 flex-1 space-y-2">
          <div className="skeleton-line" style={{ width: '40%', height: '12px' }} />
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="skeleton-line-lg" />
          ))}
        </div>
      </div>
    )
  }

  // ── Error state ──────────────────────────────────────────────────────────
  if (error && !data) {
    return (
      <div className="card h-full flex flex-col items-center justify-center p-6 bg-[#13161e] border border-[#1f2335] shadow-xl space-y-3">
        <AlertTriangle className="size-8 text-red-400" aria-hidden="true" />
        <span className="text-sm font-bold text-red-400">Execution Quality Ledger Unreachable</span>
        <p className="text-xs text-[#7e8aaa] max-w-md text-center">
          Could not load per-fill execution quality metrics from{' '}
          <code className="text-[#c8cfe0]">/api/execution-quality</code>.
        </p>
        <p className="text-[10px] text-[#5a637a] mono max-w-md text-center break-all">{error}</p>
        <button onClick={() => fetchData()} className="btn btn-primary btn-sm mt-2 flex items-center gap-1">
          <RefreshCw className="size-3" aria-hidden="true" /> Retry
        </button>
      </div>
    )
  }

  const avgSlippage = stats?.avg_slippage_bps ?? 0
  const avgSlippageColor = slippageColorClass(avgSlippage)
  const hasFills = fills.length > 0

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
      {/* ── Header ──────────────────────────────────────────────────────────── */}
      <div className="card-header pb-2 mb-3 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Gauge className="size-3.5 text-cyan-400" aria-hidden="true" />
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Execution Quality
          </span>
          <span className="badge badge-cyan text-[9.5px]">Per-Fill Audit</span>
          {stats?.count != null && (
            <span className="badge badge-dim text-[9.5px]">
              {derived.totalFills} fills · {derived.buyCount}B / {derived.sellCount}S
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {/* Auto-refresh indicator */}
          <span
            className={`flex items-center gap-1 text-[9.5px] mono ${
              isRefreshing ? 'text-cyan-400' : 'text-[#5a637a]'
            }`}
            title={`Auto-refresh every ${POLL_INTERVAL_MS / 1000}s${typeof document !== 'undefined' && document.hidden ? ' — paused (tab hidden)' : ''}`}
            aria-label="Auto-refresh status"
          >
            <RefreshCw
              className={`size-3 ${isRefreshing ? 'animate-spin' : ''}`}
              aria-hidden="true"
            />
            {typeof document !== 'undefined' && document.hidden ? 'Paused' : lastUpdated ? fmtAge(lastUpdated / 1000) : '—'}
          </span>

          {/* Manual refresh */}
          <button
            onClick={() => {
              setIsRefreshing(true)
              fetchData()
            }}
            className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
            title="Refresh now"
            aria-label="Refresh execution quality data"
          >
            <RefreshCw className="size-3" aria-hidden="true" /> Refresh
          </button>

          {/* Time-range select */}
          <Select
            value={timeRange}
            onValueChange={(v) => setTimeRange(v as TimeRange)}
          >
            <SelectTrigger
              size="sm"
              className="h-7 w-[110px] text-[10.5px] bg-[#0e1015] border-[#1f2335] text-[#dde1ed] hover:border-[#2d3450]"
              aria-label="Time range filter"
            >
              <SelectValue placeholder="Range" />
            </SelectTrigger>
            <SelectContent className="bg-[#0e1015] border-[#1f2335] text-[#dde1ed]">
              {TIME_RANGES.map((r) => (
                <SelectItem
                  key={r.value}
                  value={r.value}
                  className="text-[10.5px] focus:bg-[#1a1f2e] focus:text-cyan-300"
                >
                  {r.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>

      {/* Inline error banner (data present but refresh failed) */}
      {error && data && (
        <div className="banner-warning text-[10.5px] mb-2 py-1.5 px-2.5" role="alert">
          <span aria-hidden="true">⚠️</span>
          <span>Refresh failed: {error.slice(0, 160)} — showing last cached data.</span>
        </div>
      )}

      {/* ── KPI strip ───────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mb-3">
        <div className="kpi-card">
          <span className="kpi-label flex items-center gap-1">
            <TrendingDown className="size-2.5" aria-hidden="true" /> Avg Slippage
          </span>
          <span className={`kpi-value ${avgSlippageColor}`}>{fmtBps(avgSlippage)}</span>
          <span className="kpi-sub">
            med {fmtBps(stats?.median_slippage_bps)} · p95 {fmtBps(stats?.p95_slippage_bps)}
          </span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label flex items-center gap-1">
            <Clock className="size-2.5" aria-hidden="true" /> Median Latency
          </span>
          <span className="kpi-value text-[#60a5fa]">{fmtMs(derived.medianLat)}</span>
          <span className="kpi-sub">avg {fmtMs(stats?.avg_latency_ms)} · signal→fill</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label flex items-center gap-1">
            <Target className="size-2.5" aria-hidden="true" /> Realized Edge
          </span>
          <span className={`kpi-value ${realizedEdgeClass(derived.totalRealized)}`}>
            {fmtPnl(derived.totalRealized, 4)}
          </span>
          <span className="kpi-sub">
            avg {fmtUsd(stats?.avg_realized_edge, 4)} / fill
          </span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label flex items-center gap-1">
            <Activity className="size-2.5" aria-hidden="true" /> Fill Rate
          </span>
          <span className="kpi-value text-[#22d3ee]">{derived.fillRate.toFixed(1)}%</span>
          <span className="kpi-sub">{fills.length} sampled fills</span>
        </div>
        <div className="kpi-card">
          <span className="kpi-label flex items-center gap-1">
            <Zap className="size-2.5" aria-hidden="true" /> Total Fills
          </span>
          <span className="kpi-value text-[#dde1ed]">{derived.totalFills.toLocaleString('en-US')}</span>
          <span className="kpi-sub">
            worst {fmtBps(stats?.worst_slippage_bps)}
          </span>
        </div>
      </div>

      {/* ── Charts row: histogram + latency timeline ────────────────────────── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mb-3">
        {/* Slippage distribution histogram */}
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10.5px] font-bold text-[#dde1ed] uppercase tracking-wider">
              📊 Slippage Distribution
            </span>
            <span className="text-[9px] text-[#5a637a] mono">{fills.length} fills</span>
          </div>
          {hasFills ? (
            <div className="space-y-1.5" role="img" aria-label="Slippage distribution by bucket">
              {derived.histogram.map((b) => {
                const maxCount = Math.max(...derived.histogram.map((x) => x.count), 1)
                const pct = (b.count / maxCount) * 100
                const sharePct = fills.length > 0 ? (b.count / fills.length) * 100 : 0
                return (
                  <div key={b.label} className="flex items-center gap-2 text-[10.5px]">
                    <span className="mono w-12 text-[#7e8aaa] font-bold">{b.label}</span>
                    <div className="flex-1 h-4 bg-[#13161e] rounded-sm overflow-hidden border border-[#1f2335]/60">
                      <div
                        className={`h-full ${b.barClass} transition-all duration-300`}
                        style={{ width: `${Math.max(pct, b.count > 0 ? 4 : 0)}%` }}
                      />
                    </div>
                    <span className={`mono w-10 text-right font-bold ${b.color}`}>{b.count}</span>
                    <span className="mono w-12 text-right text-[9px] text-[#5a637a]">{sharePct.toFixed(0)}%</span>
                    <span className="hidden sm:inline text-[9px] text-[#5a637a] w-16">{b.range}</span>
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="py-6 text-center text-[10px] text-[#5a637a]">No fills in window</div>
          )}
        </div>

        {/* Latency sparkline timeline */}
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-[10.5px] font-bold text-[#dde1ed] uppercase tracking-wider">
              ⏱ Latency Timeline
            </span>
            <span className="text-[9px] text-[#5a637a] mono">
              last {derived.latencyTimeline.length} fills
            </span>
          </div>
          {derived.latencyTimeline.length >= 2 ? (
            <div className="relative">
              <svg
                viewBox="0 0 300 70"
                className="w-full h-[70px]"
                preserveAspectRatio="none"
                role="img"
                aria-label={`Latency over the last ${derived.latencyTimeline.length} fills`}
              >
                <defs>
                  <linearGradient id="latGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#06b6d4" stopOpacity="0.35" />
                    <stop offset="100%" stopColor="#06b6d4" stopOpacity="0.02" />
                  </linearGradient>
                </defs>
                {/* Grid lines */}
                {[14, 35, 56].map((y) => (
                  <line
                    key={y}
                    x1="0"
                    y1={y}
                    x2="300"
                    y2={y}
                    stroke="#1f2335"
                    strokeWidth="0.5"
                    strokeDasharray="2 3"
                  />
                ))}
                {(() => {
                  const path = sparklinePath(derived.latencyTimeline, 300, 70, 3)
                  const last = derived.latencyTimeline[derived.latencyTimeline.length - 1]
                  const min = Math.min(...derived.latencyTimeline)
                  const max = Math.max(...derived.latencyTimeline)
                  const range = max - min || 1
                  const lastY = 3 + (1 - (last - min) / range) * (70 - 6)
                  const areaPath = `${path} L297,${(70 - 3).toFixed(1)} L3,${(70 - 3).toFixed(1)} Z`
                  return (
                    <>
                      <path d={areaPath} fill="url(#latGrad)" />
                      <path
                        d={path}
                        fill="none"
                        stroke="#22d3ee"
                        strokeWidth="1.5"
                        strokeLinejoin="round"
                        strokeLinecap="round"
                      />
                      <circle
                        cx="297"
                        cy={lastY.toFixed(1)}
                        r="2.5"
                        fill="#22d3ee"
                        stroke="#0e1015"
                        strokeWidth="1"
                      />
                    </>
                  )
                })()}
              </svg>
              <div className="flex items-center justify-between text-[9px] text-[#5a637a] mono mt-1">
                <span>
                  min {Math.min(...derived.latencyTimeline).toFixed(0)}ms
                </span>
                <span className="text-cyan-400">
                  now {derived.latencyTimeline[derived.latencyTimeline.length - 1].toFixed(0)}ms
                </span>
                <span>
                  max {Math.max(...derived.latencyTimeline).toFixed(0)}ms
                </span>
              </div>
            </div>
          ) : (
            <div className="py-6 text-center text-[10px] text-[#5a637a]">Not enough fills for timeline</div>
          )}
        </div>
      </div>

      {/* ── Worst executions ────────────────────────────────────────────────── */}
      <div className="bg-[#0e1015] border border-red-500/25 rounded-md p-3 mb-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10.5px] font-bold text-red-400 uppercase tracking-wider flex items-center gap-1">
            <AlertTriangle className="size-2.5" aria-hidden="true" /> Worst Executions (Top 5)
          </span>
          <span className="text-[9px] text-[#5a637a] mono">by adverse slippage</span>
        </div>
        {derived.worst.length > 0 ? (
          <div className="overflow-x-auto scrollbar-thin">
            <table className="data-table text-xs w-full" role="table" aria-label="Top 5 worst slippage fills">
              <thead>
                <tr className="text-[#7e8aaa] text-[10px]">
                  <th scope="col" className="text-left min-w-[140px]">Token</th>
                  <th scope="col" className="text-center">Side</th>
                  <th scope="col" className="text-right">Intended</th>
                  <th scope="col" className="text-right">Fill</th>
                  <th scope="col" className="text-right">Slippage</th>
                  <th scope="col" className="text-right">Latency</th>
                  <th scope="col" className="text-right">Edge</th>
                  <th scope="col" className="text-right">Age</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1f2335]/40">
                {derived.worst.map((f) => (
                  <tr key={`worst-${f.id}`} className="bg-red-500/5 hover:bg-red-500/10 transition-colors">
                    <td className="py-1.5 max-w-[200px]">
                      <div className="flex flex-col">
                        <span className="text-[#c8cfe0] font-medium text-[10.5px] truncate" title={f.token_id ?? ''}>
                          {(f.token_id || '—').slice(0, 18)}…
                        </span>
                        <span className="text-[9px] text-[#5a637a] mono">
                          {f.strategy || 'manual'}
                        </span>
                      </div>
                    </td>
                    <td className="text-center">
                      <span
                        className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ${
                          f.side === 'BUY'
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : 'bg-red-500/15 text-red-400 border border-red-500/30'
                        }`}
                      >
                        {f.side || '—'}
                      </span>
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">{fmtPrice(f.expected_fill)}</td>
                    <td className="mono text-right text-[#c8cfe0] font-bold">{fmtPrice(f.actual_fill)}</td>
                    <td className={`mono text-right font-bold ${slippageColorClass(f.slippage_bps)}`}>
                      {fmtBps(f.slippage_bps)}
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">{fmtMs(f.latency_ms)}</td>
                    <td className={`mono text-right font-bold ${realizedEdgeClass(f.realized_edge)}`}>
                      {fmtPnl(f.realized_edge, 4)}
                    </td>
                    <td className="mono text-right text-[#5a637a] text-[10px]">{fmtAge(f.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="py-4 text-center text-[10px] text-[#5a637a]">No fills recorded in this window</div>
        )}
      </div>

      {/* ── Full execution-quality table ────────────────────────────────────── */}
      <div className="flex-1 min-h-0 flex flex-col">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10.5px] font-bold text-[#dde1ed] uppercase tracking-wider flex items-center gap-1">
            📋 Per-Fill Quality Audit
          </span>
          <span className="text-[9px] text-[#5a637a] mono">
            {fills.length} of {derived.totalFills} fills shown
          </span>
        </div>
        <div className="overflow-auto scrollbar-thin flex-1 table-container border border-[#1f2335] rounded-md">
          {hasFills ? (
            <table className="data-table text-xs" role="table" aria-label="Per-fill execution quality log">
              <thead>
                <tr className="text-[#7e8aaa] text-[10px]">
                  <th scope="col" className="text-left min-w-[160px]">Token / Strategy</th>
                  <th scope="col" className="text-center">Side</th>
                  <th scope="col" className="text-right">Intended</th>
                  <th scope="col" className="text-right">Fill</th>
                  <th scope="col" className="text-right">Slippage (bps)</th>
                  <th scope="col" className="text-right">Latency (ms)</th>
                  <th scope="col" className="text-right">Realized Edge</th>
                  <th scope="col" className="text-center">Mode</th>
                  <th scope="col" className="text-right">Age</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1f2335]/40">
                {fills.map((f) => (
                  <tr key={f.id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-1.5 max-w-[220px]">
                      <div className="flex flex-col gap-0.5">
                        <span
                          className="text-[10px] text-cyan-400 font-bold truncate"
                          title={f.token_id ?? ''}
                        >
                          {(f.token_id || '—').slice(0, 22)}
                        </span>
                        <span className="text-[9px] text-[#5a637a] mono">
                          {f.strategy || 'manual'}
                        </span>
                      </div>
                    </td>
                    <td className="text-center">
                      <span
                        className={`inline-block px-1.5 py-0.5 rounded text-[9px] font-bold ${
                          f.side === 'BUY'
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : f.side === 'SELL'
                            ? 'bg-red-500/15 text-red-400 border border-red-500/30'
                            : 'bg-[#1f2335] text-[#7e8aaa] border border-[#1f2335]'
                        }`}
                      >
                        {f.side || '—'}
                      </span>
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">{fmtPrice(f.expected_fill)}</td>
                    <td className="mono text-right text-[#c8cfe0] font-bold">{fmtPrice(f.actual_fill)}</td>
                    <td className="text-right">
                      <span className={slippageBadgeClass(f.slippage_bps)}>
                        {fmtBps(f.slippage_bps)}
                      </span>
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">{fmtMs(f.latency_ms)}</td>
                    <td
                      className={`mono text-right font-bold flex items-center justify-end gap-0.5 ${
                        realizedEdgeClass(f.realized_edge)
                      }`}
                    >
                      {f.realized_edge > 0 ? (
                        <TrendingUp className="size-2.5" aria-hidden="true" />
                      ) : f.realized_edge < 0 ? (
                        <TrendingDown className="size-2.5" aria-hidden="true" />
                      ) : null}
                      {fmtPnl(f.realized_edge, 4)}
                    </td>
                    <td className="text-center">
                      <span
                        className={`text-[9px] font-bold px-1.5 py-0.5 rounded ${
                          f.paper
                            ? 'bg-amber-500/10 text-amber-400 border border-amber-500/25'
                            : 'bg-red-500/10 text-red-400 border border-red-500/25'
                        }`}
                      >
                        {f.paper ? 'PAPER' : 'LIVE'}
                      </span>
                    </td>
                    <td className="mono text-right text-[#5a637a] text-[10px]">{fmtAge(f.timestamp)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="empty-state py-8">
              <span className="empty-state-icon text-2xl" aria-hidden="true">⚡</span>
              <span className="empty-state-title text-sm font-semibold">No execution-quality records</span>
              <span className="empty-state-desc text-xs text-center max-w-xs">
                Slippage, latency, and realized-edge metrics will appear here as orders fill against the paper exchange.
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
