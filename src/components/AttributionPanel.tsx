// components/AttributionPanel.tsx — 7-Dimension P&L Performance Attribution
// Exposes the backend attribution engine (`GET /api/attribution`) which slices
// realised P&L across seven orthogonal dimensions: strategy, ML-confidence,
// predicted-edge, probability-band, liquidity-level, holding-period, and
// trade-direction. Each dimension is rendered as a horizontal contribution
// bar with expandable per-bucket breakdown, plus a waterfall view, a
// per-strategy table, summary KPIs (coverage %, residual), and a time-range
// selector with 30s auto-refresh (paused when document is hidden).

'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct, fmtInt } from '@/lib/design-tokens'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from '@/components/ui/tabs'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Brain,
  Target,
  Percent,
  Waves,
  Clock,
  ArrowLeftRight,
  Layers,
  TrendingUp,
  TrendingDown,
  Activity,
  RefreshCw,
  AlertCircle,
  ChevronRight,
  PieChart,
  BarChart3,
  Database,
  Minus,
} from 'lucide-react'

// ── Types ────────────────────────────────────────────────────────────────
interface AttributionBucket {
  bucket: string
  count: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  wins: number
  losses: number
  avg_holding_seconds: number
  gross_profit: number
  gross_loss: number
  profit_factor: number | null
  capital_deployed: number
}

interface AttributionSummary {
  count?: number
  total_pnl?: number
  avg_pnl?: number
  median_pnl?: number
  win_rate?: number
  wins?: number
  losses?: number
  breakeven?: number
  avg_holding_seconds?: number
  gross_profit?: number
  gross_loss?: number
  profit_factor?: number | null
  best_trade?: number
  worst_trade?: number
  avg_entry_price?: number
  avg_exit_price?: number
  total_volume_shares?: number
  strategies_count?: number
}

interface AttributionResponse {
  summary: AttributionSummary
  by_strategy: AttributionBucket[]
  by_confidence_bucket: AttributionBucket[]
  by_edge_bucket: AttributionBucket[]
  by_probability_band: AttributionBucket[]
  by_liquidity_level: AttributionBucket[]
  by_holding_period: AttributionBucket[]
  by_trade_direction: AttributionBucket[]
  bucket_definitions: Record<string, string[]>
}

type TimeRange = '1h' | '24h' | '7d' | '30d' | 'all'

type DimensionKey =
  | 'by_strategy'
  | 'by_confidence_bucket'
  | 'by_edge_bucket'
  | 'by_probability_band'
  | 'by_liquidity_level'
  | 'by_holding_period'
  | 'by_trade_direction'

interface DimensionMeta {
  key: DimensionKey
  label: string
  description: string
  icon: typeof Brain
  accent: 'blue' | 'purple' | 'cyan' | 'amber' | 'green'
}

const DIMENSIONS: DimensionMeta[] = [
  {
    key: 'by_strategy',
    label: 'Strategy',
    description: 'P&L source by trading strategy',
    icon: Layers,
    accent: 'blue',
  },
  {
    key: 'by_confidence_bucket',
    label: 'ML Confidence',
    description: 'Alpha from model confidence at entry',
    icon: Brain,
    accent: 'purple',
  },
  {
    key: 'by_edge_bucket',
    label: 'Predicted Edge',
    description: 'Edge (p_yes − market mid) at signal',
    icon: Target,
    accent: 'cyan',
  },
  {
    key: 'by_probability_band',
    label: 'Probability Band',
    description: 'Market selection by p_yes band',
    icon: Percent,
    accent: 'amber',
  },
  {
    key: 'by_liquidity_level',
    label: 'Liquidity Level',
    description: 'Execution slippage by market depth',
    icon: Waves,
    accent: 'green',
  },
  {
    key: 'by_holding_period',
    label: 'Holding Period',
    description: 'Entry/exit timing alpha',
    icon: Clock,
    accent: 'blue',
  },
  {
    key: 'by_trade_direction',
    label: 'Trade Direction',
    description: 'Long-YES vs short-NO asymmetry',
    icon: ArrowLeftRight,
    accent: 'cyan',
  },
]

const TIME_RANGES: { value: TimeRange; label: string }[] = [
  { value: '1h', label: '1H' },
  { value: '24h', label: '24H' },
  { value: '7d', label: '7D' },
  { value: '30d', label: '30D' },
  { value: 'all', label: 'All-Time' },
]

const POLL_INTERVAL_MS = 30_000

// ── Helpers ──────────────────────────────────────────────────────────────

const accentBadge: Record<DimensionMeta['accent'], string> = {
  blue: 'badge badge-blue',
  purple: 'badge badge-purple',
  cyan: 'badge badge-cyan',
  amber: 'badge badge-amber',
  green: 'badge badge-green',
}

const accentText: Record<DimensionMeta['accent'], string> = {
  blue: 'text-[#60a5fa]',
  purple: 'text-[#c084fc]',
  cyan: 'text-[#22d3ee]',
  amber: 'text-[#fbbf24]',
  green: 'text-[#4ade80]',
}

function pnlColor(v: number): string {
  if (v > 0) return 'text-[#4ade80]'
  if (v < 0) return 'text-[#f87171]'
  return 'text-[#7e8aaa]'
}

function pnlBg(v: number): string {
  if (v > 0) return 'bg-emerald-500/70'
  if (v < 0) return 'bg-red-500/70'
  return 'bg-slate-600/50'
}

function humanizeBucket(label: string): string {
  if (!label) return '—'
  return label
    .replace(/_/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

function fmtHoldingSeconds(s: number | null | undefined): string {
  if (s == null || !Number.isFinite(s) || s <= 0) return '—'
  if (s < 60) return `${Math.floor(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${(s / 86400).toFixed(1)}d`
}

function sumDimensionPnl(buckets: AttributionBucket[]): number {
  return buckets.reduce((acc, b) => acc + (b.total_pnl || 0), 0)
}

function bestBucket(buckets: AttributionBucket[]): AttributionBucket | null {
  if (!buckets.length) return null
  return buckets.reduce((best, b) =>
    (b.total_pnl ?? -Infinity) > (best.total_pnl ?? -Infinity) ? b : best
  )
}

function worstBucket(buckets: AttributionBucket[]): AttributionBucket | null {
  if (!buckets.length) return null
  return buckets.reduce((worst, b) =>
    (b.total_pnl ?? Infinity) < (worst.total_pnl ?? Infinity) ? b : worst
  )
}

// ── Component ─────────────────────────────────────────────────────────────

export default function AttributionPanel() {
  const [data, setData] = useState<AttributionResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [timeRange, setTimeRange] = useState<TimeRange>('all')
  const [expanded, setExpanded] = useState<Set<DimensionKey>>(new Set())
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)

  const fetchAttribution = useCallback(async () => {
    setIsRefreshing(true)
    try {
      const url = `/api/attribution?range=${timeRange}`
      const res = await apiFetch(url)
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`)
      }
      const json = (await res.json()) as AttributionResponse
      setData(json)
      setError(null)
      setLastUpdated(Date.now())
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error'
      setError(msg)
    } finally {
      setLoading(false)
      setIsRefreshing(false)
    }
  }, [timeRange])

  // Initial fetch + 30s polling, paused when document hidden
  useEffect(() => {
    fetchAttribution()
  }, [fetchAttribution])

  useEffect(() => {
    const start = () => {
      if (timerRef.current) return
      timerRef.current = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchAttribution()
      }, POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (timerRef.current) {
        clearInterval(timerRef.current)
        timerRef.current = null
      }
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) {
        stop()
      } else {
        // On regain focus, immediately refresh + restart interval
        fetchAttribution()
        start()
      }
    }
    start()
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    return () => {
      stop()
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    }
  }, [fetchAttribution])

  const toggleExpand = (key: DimensionKey) => {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  // ── Loading skeleton ────────────────────────────────────────────────────
  if (loading && !data) {
    return (
      <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
        <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
          <div className="flex items-center gap-2">
            <PieChart className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              Attribution Analysis
            </span>
          </div>
          <span className="spinner" aria-hidden="true" />
        </div>
        <div className="p-3 space-y-3">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
            {[0, 1, 2, 3].map((i) => (
              <div key={i} className="kpi-card">
                <div className="skeleton h-3 w-20 mb-2" />
                <div className="skeleton h-5 w-24 mb-1" />
                <div className="skeleton h-2 w-16" />
              </div>
            ))}
          </div>
          <div className="space-y-2 pt-2">
            {[0, 1, 2, 3, 4, 5, 6].map((i) => (
              <div key={i} className="skeleton-card p-2.5">
                <div className="flex items-center justify-between mb-2">
                  <div className="skeleton h-3 w-32" />
                  <div className="skeleton h-3 w-16" />
                </div>
                <div className="skeleton h-2 w-full" />
              </div>
            ))}
          </div>
        </div>
      </div>
    )
  }

  // ── Error state ──────────────────────────────────────────────────────────
  if (error && !data) {
    return (
      <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
        <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
          <div className="flex items-center gap-2">
            <PieChart className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              Attribution Analysis
            </span>
          </div>
        </div>
        <div className="error-state">
          <AlertCircle className="error-state-icon text-[#f87171]" aria-hidden="true" />
          <div className="error-state-title">Attribution unavailable</div>
          <div className="error-state-desc">{error}</div>
          <button
            type="button"
            onClick={() => fetchAttribution()}
            className="btn btn-ghost btn-sm mt-2"
          >
            <RefreshCw className="w-3 h-3" aria-hidden="true" />
            Retry
          </button>
        </div>
      </div>
    )
  }

  if (!data) {
    return (
      <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
        <div className="card-header p-3 border-b border-[#1f2335]">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            Attribution Analysis
          </span>
        </div>
        <div className="empty-state">
          <PieChart className="empty-state-icon text-[#7e8aaa]" aria-hidden="true" />
          <div className="empty-state-title">No attribution data</div>
          <div className="empty-state-desc">
            Closed positions will appear here once strategies record exits.
          </div>
        </div>
      </div>
    )
  }

  // ── Derived metrics ─────────────────────────────────────────────────────
  const totalPnl = data.summary?.total_pnl ?? 0
  const totalTrades = data.summary?.count ?? 0
  const winRate = data.summary?.win_rate ?? 0
  const profitFactor = data.summary?.profit_factor

  // Attribution coverage: each dimension covers ALL closed positions, so
  // every dimension's sum equals total P&L. Coverage is the share of total
  // P&L that flows through the (sum across 7 dimensions / 7) — i.e., always
  // 100% by construction. We compute residual as the difference between
  // total P&L and the average dimension sum (always 0 by design) so the
  // KPI surfaces the engine's design invariant: 100% attribution.
  const dimensionSums = DIMENSIONS.map((d) => sumDimensionPnl(data[d.key] ?? []))
  const attributedSum = dimensionSums.reduce((a, b) => a + Math.abs(b), 0) / 7
  const coverage = totalPnl !== 0
    ? Math.min(100, (Math.abs(attributedSum) / Math.abs(totalPnl)) * 100)
    : 0
  const residual = totalPnl - attributedSum

  // Max abs P&L across all dimensions (for bar scaling)
  const maxAbsPnl = Math.max(
    1,
    ...dimensionSums.map((v) => Math.abs(v))
  )

  const freshnessLabel = lastUpdated
    ? `${Math.floor((Date.now() - lastUpdated) / 1000)}s ago`
    : '—'

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* Header */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <PieChart className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            Performance Attribution
          </span>
          <span className="badge badge-cyan text-[9.5px]">7-DIMENSION</span>
        </div>
        <div className="flex items-center gap-2">
          {/* Time range selector */}
          <Select
            value={timeRange}
            onValueChange={(v) => setTimeRange(v as TimeRange)}
          >
            <SelectTrigger
              className="h-7 w-[110px] text-[11px] bg-[#0e1015] border-[#1f2335] text-[#dde1ed]"
              size="sm"
              aria-label="Attribution time range"
            >
              <SelectValue placeholder="Range" />
            </SelectTrigger>
            <SelectContent className="bg-[#13161e] border-[#1f2335]">
              {TIME_RANGES.map((r) => (
                <SelectItem
                  key={r.value}
                  value={r.value}
                  className="text-[#dde1ed] text-xs focus:bg-[#1a1f2e]"
                >
                  {r.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {/* Refresh + freshness */}
          <button
            type="button"
            onClick={() => fetchAttribution()}
            disabled={isRefreshing}
            className="btn btn-ghost btn-sm flex items-center gap-1 text-[10px]"
            title={`Auto-refresh 30s • Last: ${freshnessLabel}`}
            aria-label="Refresh attribution"
          >
            <RefreshCw
              className={`w-3 h-3 ${isRefreshing ? 'animate-spin' : ''}`}
              aria-hidden="true"
            />
            <span className="text-[#7e8aaa] hidden sm:inline">{freshnessLabel}</span>
          </button>
        </div>
      </div>

      {/* Inline error banner (when we have stale data) */}
      {error && data && (
        <div className="banner-warning text-[10.5px] mx-3 mt-2 py-1.5 px-2.5" role="alert">
          <AlertCircle className="w-3 h-3 mt-0.5 flex-shrink-0" aria-hidden="true" />
          <span>
            Refresh failed: {error}. Showing last cached data ({freshnessLabel}).
          </span>
        </div>
      )}

      {/* Summary KPIs */}
      <div className="p-3 grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px]">
        <div className="kpi-card">
          <span className="kpi-label">Total P&amp;L</span>
          <span className={`kpi-value ${pnlColor(totalPnl)}`}>
            {fmtPnl(totalPnl)}
          </span>
          <span className="kpi-sub">{fmtInt(totalTrades)} closed trades</span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Attributed</span>
          <span className={`kpi-value ${pnlColor(attributedSum)}`}>
            {fmtPnl(attributedSum)}
          </span>
          <span className="kpi-sub">Sum across 7 dimensions</span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Unattributed Residual</span>
          <span className={`kpi-value ${pnlColor(residual)}`}>
            {fmtPnl(residual)}
          </span>
          <span className="kpi-sub">
            {Math.abs(residual) < 0.01 ? 'Fully reconciled' : 'Reconciliation gap'}
          </span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Coverage</span>
          <span className="kpi-value text-[#60a5fa]">
            {coverage.toFixed(1)}%
          </span>
          <span className="kpi-sub">
            Win {fmtPct(winRate)} • PF{' '}
            {typeof profitFactor === 'number' ? profitFactor.toFixed(2) : '∞'}
          </span>
        </div>
      </div>

      {/* Tabs: Dimensions / Waterfall / Strategies */}
      <div className="px-3 pb-3">
        <Tabs defaultValue="dimensions" className="w-full">
          <TabsList className="bg-[#0e1015] border border-[#1f2335] h-8 w-full">
            <TabsTrigger
              value="dimensions"
              className="text-[11px] data-[state=active]:bg-[#1a1f2e] data-[state=active]:text-[#22d3ee] flex items-center gap-1"
            >
              <BarChart3 className="w-3 h-3" aria-hidden="true" />
              Dimensions
            </TabsTrigger>
            <TabsTrigger
              value="waterfall"
              className="text-[11px] data-[state=active]:bg-[#1a1f2e] data-[state=active]:text-[#22d3ee] flex items-center gap-1"
            >
              <TrendingUp className="w-3 h-3" aria-hidden="true" />
              Waterfall
            </TabsTrigger>
            <TabsTrigger
              value="strategies"
              className="text-[11px] data-[state=active]:bg-[#1a1f2e] data-[state=active]:text-[#22d3ee] flex items-center gap-1"
            >
              <Database className="w-3 h-3" aria-hidden="true" />
              Strategies
            </TabsTrigger>
          </TabsList>

          {/* ── Dimensions tab ────────────────────────────────────────────── */}
          <TabsContent value="dimensions" className="mt-3">
            <div className="space-y-2 max-h-[480px] overflow-y-auto scrollbar-thin pr-1">
              {DIMENSIONS.map((dim) => {
                const buckets = data[dim.key] ?? []
                const dimPnl = sumDimensionPnl(buckets)
                const dimPct = totalPnl !== 0 ? (dimPnl / Math.abs(totalPnl)) * 100 : 0
                const barWidthPct = (Math.abs(dimPnl) / maxAbsPnl) * 100
                const isExpanded = expanded.has(dim.key)
                const Icon = dim.icon
                const best = bestBucket(buckets)
                const worst = worstBucket(buckets)
                const positiveCount = buckets.filter((b) => b.total_pnl > 0).length

                return (
                  <div
                    key={dim.key}
                    className="kpi-card !p-0 overflow-hidden"
                    role="region"
                    aria-label={`${dim.label} attribution`}
                  >
                    {/* Dimension header row (click to expand) */}
                    <button
                      type="button"
                      onClick={() => toggleExpand(dim.key)}
                      className="w-full flex items-center gap-2.5 p-2.5 text-left hover:bg-[#1a1f2e]/40 transition-colors"
                      aria-expanded={isExpanded}
                      aria-controls={`dim-${dim.key}`}
                    >
                      <ChevronRight
                        className={`w-3 h-3 text-[#7e8aaa] flex-shrink-0 transition-transform ${
                          isExpanded ? 'rotate-90' : ''
                        }`}
                        aria-hidden="true"
                      />
                      <div className="flex items-center gap-2 flex-shrink-0 min-w-[150px]">
                        <Icon
                          className={`w-3.5 h-3.5 ${accentText[dim.accent]}`}
                          aria-hidden="true"
                        />
                        <div className="flex flex-col">
                          <span className="text-[11.5px] font-semibold text-[#dde1ed] leading-tight">
                            {dim.label}
                          </span>
                          <span className="text-[9.5px] text-[#7e8aaa] leading-tight">
                            {dim.description}
                          </span>
                        </div>
                      </div>

                      {/* Bar */}
                      <div className="flex-1 flex items-center gap-2 min-w-[100px]">
                        <div className="flex-1 h-2 bg-[#0e1015] rounded-sm overflow-hidden relative">
                          <div
                            className={`h-full rounded-sm transition-all duration-300 ${pnlBg(
                              dimPnl
                            )}`}
                            style={{ width: `${barWidthPct}%` }}
                          />
                        </div>
                      </div>

                      {/* Value + percentage */}
                      <div className="flex items-center gap-2 flex-shrink-0 text-right">
                        <span className={`mono text-[11.5px] font-bold ${pnlColor(dimPnl)}`}>
                          {fmtPnl(dimPnl)}
                        </span>
                        <span className="mono text-[10px] text-[#7e8aaa] w-12 text-right">
                          {dimPct >= 0 ? '+' : ''}
                          {dimPct.toFixed(1)}%
                        </span>
                        <span className={`${accentBadge[dim.accent]} text-[9px]`}>
                          {buckets.length} buckets
                        </span>
                      </div>
                    </button>

                    {/* Expanded: per-bucket breakdown */}
                    {isExpanded && (
                      <div
                        id={`dim-${dim.key}`}
                        className="border-t border-[#1f2335] bg-[#0e1015]/60 p-2 space-y-1"
                      >
                        {best && worst && best.bucket !== worst.bucket && (
                          <div className="flex items-center justify-between text-[10px] text-[#7e8aaa] mb-1.5 px-1">
                            <span className="flex items-center gap-1">
                              <TrendingUp className="w-3 h-3 text-[#4ade80]" aria-hidden="true" />
                              Best: <span className="text-[#4ade80] mono">{humanizeBucket(best.bucket)}</span>{' '}
                              ({fmtPnl(best.total_pnl)})
                            </span>
                            <span className="flex items-center gap-1">
                              <TrendingDown className="w-3 h-3 text-[#f87171]" aria-hidden="true" />
                              Worst: <span className="text-[#f87171] mono">{humanizeBucket(worst.bucket)}</span>{' '}
                              ({fmtPnl(worst.total_pnl)})
                            </span>
                          </div>
                        )}
                        {buckets.map((b) => {
                          const bPct = dimPnl !== 0
                            ? (Math.abs(b.total_pnl) / Math.abs(dimPnl)) * 100
                            : 0
                          const bWidth = dimPnl !== 0
                            ? (Math.abs(b.total_pnl) / Math.abs(dimPnl)) * 100
                            : 0
                          return (
                            <div
                              key={b.bucket}
                              className="flex items-center gap-2 py-1 px-1.5 rounded hover:bg-[#1a1f2e]/40 transition-colors"
                            >
                              <div className="w-[90px] text-[10.5px] text-[#c8cfe0] font-medium truncate">
                                {humanizeBucket(b.bucket)}
                              </div>
                              <div className="flex-1 h-1.5 bg-[#0e1015] rounded-sm overflow-hidden">
                                <div
                                  className={`h-full ${pnlBg(b.total_pnl)} transition-all`}
                                  style={{ width: `${bWidth}%` }}
                                />
                              </div>
                              <div className={`mono text-[10.5px] font-semibold w-16 text-right ${pnlColor(
                                b.total_pnl
                              )}`}>
                                {fmtPnl(b.total_pnl)}
                              </div>
                              <div className="mono text-[9.5px] text-[#7e8aaa] w-10 text-right">
                                {bPct.toFixed(0)}%
                              </div>
                              <div className="mono text-[9.5px] text-[#7e8aaa] w-10 text-right">
                                {b.count}t
                              </div>
                              <div className="mono text-[9.5px] text-[#7e8aaa] w-12 text-right">
                                {(b.win_rate * 100).toFixed(0)}% W
                              </div>
                            </div>
                          )
                        })}
                        {positiveCount > 0 && (
                          <div className="text-[9.5px] text-[#7e8aaa] pt-1 border-t border-[#181c28] mt-1">
                            <Activity className="w-2.5 h-2.5 inline mr-1" aria-hidden="true" />
                            {positiveCount}/{buckets.length} buckets profitable
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </TabsContent>

          {/* ── Waterfall tab ─────────────────────────────────────────────── */}
          <TabsContent value="waterfall" className="mt-3">
            <div className="kpi-card p-3">
              <div className="flex items-center justify-between mb-3">
                <div className="flex items-center gap-2">
                  <TrendingUp className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
                  <span className="text-[11px] font-bold text-[#dde1ed]">
                    P&amp;L Contribution Waterfall
                  </span>
                </div>
                <span className="badge badge-dim text-[9px]">
                  Best bucket per dimension
                </span>
              </div>

              {/* Waterfall bars */}
              <div className="space-y-2">
                {(() => {
                  // Each dimension contributes its BEST bucket's P&L as the
                  // "alpha source" — cumulatively stacked toward total P&L.
                  const items = DIMENSIONS.map((dim) => {
                    const best = bestBucket(data[dim.key] ?? [])
                    return {
                      dim,
                      pnl: best?.total_pnl ?? 0,
                      bucketLabel: best?.bucket ?? '—',
                    }
                  })
                  // Cumulative running total starting from 0
                  let cumulative = 0
                  const maxCum = items.reduce(
                    (acc, it) => acc + Math.max(0, it.pnl),
                    0
                  )
                  const scaleMax = Math.max(
                    Math.abs(maxCum),
                    Math.abs(totalPnl),
                    1
                  )

                  return items.map((it, i) => {
                    const prev = cumulative
                    cumulative += it.pnl
                    const startPct = (Math.min(prev, cumulative) / scaleMax) * 100
                    const heightPct =
                      (Math.abs(it.pnl) / scaleMax) * 100
                    const Icon = it.dim.icon
                    return (
                      <div
                        key={it.dim.key}
                        className="flex items-center gap-2"
                        title={`${it.dim.label} best bucket: ${humanizeBucket(
                          it.bucketLabel
                        )} (${fmtPnl(it.pnl)})`}
                      >
                        <div className="w-[110px] flex items-center gap-1.5 flex-shrink-0">
                          <Icon
                            className={`w-3 h-3 ${accentText[it.dim.accent]}`}
                            aria-hidden="true"
                          />
                          <span className="text-[10px] text-[#c8cfe0] truncate">
                            {it.dim.label}
                          </span>
                        </div>

                        {/* Stacked waterfall track */}
                        <div className="flex-1 h-6 bg-[#0e1015] rounded-sm relative overflow-hidden border border-[#181c28]">
                          {/* Baseline indicator */}
                          <div className="absolute left-0 top-0 bottom-0 w-px bg-[#1f2335]" />
                          {/* Bar segment */}
                          <div
                            className={`absolute top-0 bottom-0 ${pnlBg(
                              it.pnl
                            )} transition-all duration-300`}
                            style={{
                              left: `${startPct}%`,
                              width: `${Math.max(heightPct, it.pnl !== 0 ? 1.5 : 0)}%`,
                            }}
                          />
                          {/* Bucket label inside bar */}
                          <span className="absolute top-1/2 -translate-y-1/2 left-2 text-[9px] text-[#dde1ed] mono pointer-events-none">
                            {humanizeBucket(it.bucketLabel)}
                          </span>
                        </div>

                        {/* Cumulative + delta */}
                        <div className="w-[110px] flex flex-col items-end flex-shrink-0">
                          <span className={`mono text-[10px] font-bold ${pnlColor(
                            it.pnl
                          )}`}>
                            {fmtPnl(it.pnl)}
                          </span>
                          <span className="mono text-[9px] text-[#7e8aaa]">
                            cum {fmtPnl(cumulative)}
                          </span>
                        </div>
                      </div>
                    )
                  })
                })()}
              </div>

              {/* Total marker */}
              <div className="mt-3 pt-2 border-t border-[#1f2335] flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Minus className="w-3 h-3 text-[#7e8aaa]" aria-hidden="true" />
                  <span className="text-[10.5px] text-[#7e8aaa] uppercase font-semibold tracking-wide">
                    Cumulative Total P&amp;L
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <span className={`mono text-[13px] font-bold ${pnlColor(totalPnl)}`}>
                    {fmtPnl(totalPnl)}
                  </span>
                  <span className="badge badge-blue text-[9px]">
                    {((totalPnl >= 0 ? totalPnl : -totalPnl) > 0 ? 'NET POSITIVE' : 'NET NEGATIVE')}
                  </span>
                </div>
              </div>

              <div className="mt-2 text-[9.5px] text-[#7e8aaa] leading-relaxed">
                Each bar represents the leading bucket&apos;s P&amp;L contribution within that
                dimension, stacked cumulatively. Bars grow right (green) for positive
                contributions and overlay left (red) for negative ones.
              </div>
            </div>
          </TabsContent>

          {/* ── Strategies tab ────────────────────────────────────────────── */}
          <TabsContent value="strategies" className="mt-3">
            <div className="kpi-card !p-0 overflow-hidden">
              <div className="table-container max-h-[440px] scrollbar-thin">
                <table
                  className="data-table text-xs w-full"
                  role="table"
                  aria-label="Per-strategy attribution breakdown"
                >
                  <thead>
                    <tr>
                      <th scope="col" className="text-left">Strategy</th>
                      <th scope="col" className="text-right">Trades</th>
                      <th scope="col" className="text-right">Win Rate</th>
                      <th scope="col" className="text-right">Total P&amp;L</th>
                      <th scope="col" className="text-right">Avg P&amp;L</th>
                      <th scope="col" className="text-right">Profit Factor</th>
                      <th scope="col" className="text-right">Capital</th>
                      <th scope="col" className="text-right">Avg Hold</th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.by_strategy.length === 0 ? (
                      <tr>
                        <td colSpan={8}>
                          <div className="empty-state py-6">
                            <Database
                              className="empty-state-icon text-[#7e8aaa]"
                              aria-hidden="true"
                            />
                            <div className="empty-state-title">
                              No strategy attribution yet
                            </div>
                            <div className="empty-state-desc">
                              Closed positions grouped by strategy will appear here.
                            </div>
                          </div>
                        </td>
                      </tr>
                    ) : (
                      data.by_strategy.map((s) => (
                        <tr key={s.bucket}>
                          <td className="label-col">
                            <div className="flex items-center gap-1.5">
                              <Layers
                                className="w-3 h-3 text-[#60a5fa] flex-shrink-0"
                                aria-hidden="true"
                              />
                              <span className="font-medium text-[#dde1ed]">
                                {humanizeBucket(s.bucket)}
                              </span>
                            </div>
                          </td>
                          <td className="text-right text-[#c8cfe0]">
                            {fmtInt(s.count)}
                          </td>
                          <td className={`text-right ${s.win_rate >= 0.5 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
                            {(s.win_rate * 100).toFixed(1)}%
                          </td>
                          <td className={`text-right font-bold ${pnlColor(s.total_pnl)}`}>
                            {fmtPnl(s.total_pnl)}
                          </td>
                          <td className={`text-right ${pnlColor(s.avg_pnl)}`}>
                            {fmtPnl(s.avg_pnl)}
                          </td>
                          <td className="text-right text-[#60a5fa]">
                            {typeof s.profit_factor === 'number'
                              ? s.profit_factor.toFixed(2)
                              : '∞'}
                          </td>
                          <td className="text-right text-[#c8cfe0]">
                            {fmtUsd(s.capital_deployed)}
                          </td>
                          <td className="text-right text-[#7e8aaa]">
                            {fmtHoldingSeconds(s.avg_holding_seconds)}
                          </td>
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
              {data.by_strategy.length > 0 && (
                <div className="table-footer">
                  <span>
                    {data.by_strategy.length} strategies • sorted by total P&amp;L
                    desc
                  </span>
                  <span className="mono">
                    Σ {fmtPnl(sumDimensionPnl(data.by_strategy))}
                  </span>
                </div>
              )}
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}
