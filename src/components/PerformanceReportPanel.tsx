// components/PerformanceReportPanel.tsx — Honest Performance Report
//
// W26-2 — Standalone panel that surfaces the bot's honest performance
// metrics SEPARATED BY CATEGORY (backtest / walk-forward / paper / live).
// This is distinct from `AnalyticsPanel`'s embedded `PerformanceReportSection`
// (which shows only the paper + best-backtest rows as a compact strip inside
// the command-center grid). This panel is the dedicated trader-facing view
// that lets the trader click through each category and audit every metric
// the system reports for that slice — with confidence intervals, p-values,
// slippage/fees, and an explicit disclaimer that backtest performance does
// NOT guarantee future results.
//
// Design contract:
//   * 4 category tabs — Backtest | Walk-Forward | Paper Trading | Live.
//     Switching tabs re-renders the metric-card grid + equity curve for
//     the selected category without re-fetching (the whole report is one
//     API response). When a category is "not available" (e.g. live when
//     the bot is still in paper mode), every metric card renders N/A and
//     a small inline pill explains why.
//   * 12 metric cards per category — Win Rate (with 95% Wilson CI +
//     significance p-value), Profit Factor, Expectancy ($/trade), Max
//     Drawdown (%), Sharpe, Sortino, Open Exposure ($), Capital
//     Utilization (%), Avg Slippage (bps), Total Fees ($), # Trades,
//     Statistical Significance. Green for positive, red for negative,
//     neutral grey when not applicable.
//   * Disclaimer banner — ALWAYS rendered (even when the fetch fails or
//     the response shape doesn't validate). The honest-disclosure text
//     is the panel's most important single artefact; the trader must see
//     it whenever the panel mounts.
//   * Equity curve — Recharts `EquityCurveChart` (the existing dark-themed
//     area chart already used by `EquityCurve` / `EquityCurveChart`).
//     Rendered only when the selected category supplies an `equity_curve`
//     array of length ≥ 2.
//   * Auto-refresh — `setInterval` every 30s while the document is visible.
//     Pauses on `visibilitychange` to hidden (matches the workstation's
//     existing polling panels: `useRealtimeData`, `useBot`, etc.).
//
// Backend contract (the panel tolerates a partial / missing response):
//   GET /api/performance/report?XTransformPort=8080
//   → 200 { backtest, walk_forward, paper_trading, live, disclaimer }
//   where each category is a `CategoryMetrics` object. If the backend
//   instead returns the legacy shape (paper_trading as object, others as
//   strings — see `AnalyticsPanel.PerformanceReportSection`), the panel
//   renders the paper_trading object (if present) and shows the string
//   fields under a "raw status" sub-card, with every metric card N/A.

'use client'

import { useCallback, useEffect, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Card } from '@/components/ui/card'
import {
  Tabs,
  TabsList,
  TabsTrigger,
  TabsContent,
} from '@/components/ui/tabs'
import { EquityCurveChart } from '@/components/charts'

// ── Types ─────────────────────────────────────────────────────────────────

export type CategoryId = 'backtest' | 'walk_forward' | 'paper_trading' | 'live'

export interface CategoryMetrics {
  category: CategoryId
  /** Whether this category has data yet (e.g. live=false until enabled). */
  available: boolean
  /** Human-readable reason when `available=false` (e.g. "Live trading not enabled"). */
  unavailable_reason?: string
  win_rate: number | null // 0..1
  /** Wilson 95% CI bounds in 0..1. null when not computable (n<2). */
  win_rate_ci_low: number | null
  win_rate_ci_high: number | null
  profit_factor: number | null
  expectancy: number | null // $/trade
  max_drawdown_pct: number | null // 0..1
  sharpe_ratio: number | null
  sortino_ratio: number | null
  open_exposure: number | null // $ (USDC)
  capital_utilization: number | null // 0..1
  avg_slippage_bps: number | null
  total_fees: number | null // $
  n_trades: number
  /** Binomial-test p-value vs the 50% coin-flip null. null when not computable. */
  p_value: number | null
  is_statistically_significant: boolean
  /** Optional equity-curve series for the chart. */
  equity_curve?: Array<{ timestamp: number; equity: number }>
  /** Optional raw status string (used by the legacy backend shape). */
  raw?: string
}

export interface PerformanceReport {
  backtest: CategoryMetrics
  walk_forward: CategoryMetrics
  paper_trading: CategoryMetrics
  live: CategoryMetrics
  disclaimer: string
}

interface PerformanceReportPanelProps {
  /** Override the refresh interval (ms). Defaults to 30_000. Tests pass 100. */
  refreshIntervalMs?: number
}

// ── Validation ────────────────────────────────────────────────────────────

const CATEGORY_IDS: CategoryId[] = ['backtest', 'walk_forward', 'paper_trading', 'live']

function isCategoryMetrics(d: unknown): d is CategoryMetrics {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return (
    typeof obj.category === 'string' &&
    CATEGORY_IDS.includes(obj.category as CategoryId) &&
    typeof obj.available === 'boolean'
  )
}

function isPerformanceReport(d: unknown): d is PerformanceReport {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return (
    typeof obj.disclaimer === 'string' &&
    isCategoryMetrics(obj.backtest) &&
    isCategoryMetrics(obj.walk_forward) &&
    isCategoryMetrics(obj.paper_trading) &&
    isCategoryMetrics(obj.live)
  )
}

/** Coerce the legacy backend shape (paper_trading as object, others as
 *  strings) into the panel's full `PerformanceReport` contract so the
 *  panel renders something useful even before the backend is upgraded. */
function coerceLegacyShape(d: unknown): PerformanceReport | null {
  if (!d || typeof d !== 'object') return null
  const obj = d as Record<string, unknown>
  if (typeof obj.disclaimer !== 'string') return null
  const paperRaw = obj.paper_trading
  const paper = isCategoryMetrics(paperRaw)
    ? paperRaw
    : makeUnavailable('paper_trading', 'Paper-trading metrics unavailable')
  const stringToCat = (id: CategoryId, val: unknown): CategoryMetrics => {
    if (isCategoryMetrics(val)) return val
    if (typeof val === 'string') {
      return makeUnavailable(id, val)
    }
    return makeUnavailable(id, 'No data')
  }
  return {
    backtest: stringToCat('backtest', obj.backtest),
    walk_forward: stringToCat('walk_forward', obj.walk_forward),
    paper_trading: paper,
    live: stringToCat('live', obj.live),
    disclaimer: obj.disclaimer,
  }
}

function makeUnavailable(category: CategoryId, reason: string): CategoryMetrics {
  return {
    category,
    available: false,
    unavailable_reason: reason,
    win_rate: null,
    win_rate_ci_low: null,
    win_rate_ci_high: null,
    profit_factor: null,
    expectancy: null,
    max_drawdown_pct: null,
    sharpe_ratio: null,
    sortino_ratio: null,
    open_exposure: null,
    capital_utilization: null,
    avg_slippage_bps: null,
    total_fees: null,
    n_trades: 0,
    p_value: null,
    is_statistically_significant: false,
    raw: reason,
  }
}

const FALLBACK_DISCLAIMER =
  '⚠ Backtest performance does NOT guarantee future results. Only paper/live metrics reflect actual system behavior. Win rate target (95%) is aspirational.'

// ── Formatting helpers ────────────────────────────────────────────────────

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return 'N/A'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return 'N/A'
  const sign = v < 0 ? '−' : ''
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

function fmtPnl(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return 'N/A'
  const sign = v >= 0 ? '+' : '−'
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return 'N/A'
  return v.toFixed(digits)
}

function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return 'N/A'
  return Math.round(v).toLocaleString('en-US')
}

function fmtPValue(p: number | null | undefined): string {
  if (p == null || !Number.isFinite(p)) return 'N/A'
  if (p < 0.001) return 'p<0.001'
  return `p=${p.toFixed(3)}`
}

// ── Confidence interval range bar ─────────────────────────────────────────
// A tiny horizontal bar showing the 95% CI relative to the full [0,1]
// range. Used as a visual companion to the textual CI display so the
// trader can glance at how tight / loose the interval is.

function CIRangeBar({
  low,
  high,
  point,
}: {
  low: number | null
  high: number | null
  point: number | null
}) {
  // Cannot render a meaningful bar without both bounds.
  if (low == null || high == null || !Number.isFinite(low) || !Number.isFinite(high)) {
    return null
  }
  const lo = Math.max(0, Math.min(1, low))
  const hi = Math.max(0, Math.min(1, high))
  const leftPct = lo * 100
  const widthPct = Math.max(2, (hi - lo) * 100) // min 2% so it's visible
  const pt = point != null && Number.isFinite(point)
    ? Math.max(0, Math.min(1, point)) * 100
    : null
  return (
    <div
      className="relative h-1.5 w-full rounded-full bg-[#1f2335] mt-1"
      role="img"
      aria-label={`95% confidence interval from ${(lo * 100).toFixed(1)}% to ${(hi * 100).toFixed(1)}%`}
      data-testid="ci-range-bar"
    >
      <div
        className="absolute top-0 h-full rounded-full"
        style={{
          left: `${leftPct}%`,
          width: `${widthPct}%`,
          background: 'linear-gradient(90deg, rgba(74,222,128,0.6), rgba(74,222,128,0.85))',
        }}
      />
      {pt != null && (
        <div
          className="absolute top-1/2 -translate-y-1/2 w-0.5 h-2.5 rounded-full bg-[#dde1ed]"
          style={{ left: `${pt}%` }}
          aria-hidden="true"
        />
      )}
    </div>
  )
}

// ── Metric card ───────────────────────────────────────────────────────────

interface MetricCardProps {
  label: string
  value: string
  sub?: string
  /** 'positive' | 'negative' | 'neutral' — controls the value colour. */
  tone?: 'positive' | 'negative' | 'neutral' | 'info'
  /** Optional CI range bar (win-rate card only). */
  ciBar?: React.ReactNode
  /** Optional badge (e.g. "Significant" / "Not significant"). */
  badge?: React.ReactNode
  testId?: string
}

function MetricCard({
  label,
  value,
  sub,
  tone = 'neutral',
  ciBar,
  badge,
  testId,
}: MetricCardProps) {
  const toneColor =
    tone === 'positive'
      ? 'text-[#4ade80]'
      : tone === 'negative'
        ? 'text-[#f87171]'
        : tone === 'info'
          ? 'text-[#60a5fa]'
          : 'text-[#dde1ed]'
  return (
    <Card
      className="bg-[#13161e] border border-[#1f2335] shadow-sm p-3 gap-2 rounded-md"
      data-testid={testId ?? 'metric-card'}
      data-card-type="metric"
    >
      <div className="flex items-center justify-between">
        <span className="text-[10px] font-semibold uppercase tracking-wider text-[#7e8aaa]">
          {label}
        </span>
        {badge}
      </div>
      <div className={`mono text-base font-bold ${toneColor}`} data-testid="metric-value">
        {value}
      </div>
      {sub && <div className="text-[10px] text-[#7e8aaa] leading-tight">{sub}</div>}
      {ciBar}
    </Card>
  )
}

// ── Category metrics grid ─────────────────────────────────────────────────

function CategoryMetricsGrid({
  metrics,
  testIdPrefix,
}: {
  metrics: CategoryMetrics
  testIdPrefix: string
}) {
  // When the category is unavailable, render a single full-width
  // explanation card instead of the 12-metric grid. The disclaimer
  // banner above still reminds the trader that paper/live are the only
  // categories that reflect actual system behavior.
  if (!metrics.available) {
    return (
      <Card
        className="bg-[#13161e] border border-[#1f2335] p-4 rounded-md"
        data-testid={`${testIdPrefix}-unavailable`}
      >
        <div className="flex items-center gap-2 text-[#7e8aaa] text-xs">
          <span aria-hidden="true">⏸️</span>
          <span>
            {metrics.unavailable_reason ?? 'No data available for this category yet.'}
          </span>
        </div>
      </Card>
    )
  }

  const winRate = metrics.win_rate
  const ciLow = metrics.win_rate_ci_low
  const ciHigh = metrics.win_rate_ci_high
  const winRateDisplay =
    winRate != null
      ? `${fmtPct(winRate)} [${fmtPct(ciLow, 1)}, ${fmtPct(ciHigh, 1)}]`
      : 'N/A'
  const isSignificant = metrics.is_statistically_significant
  const pValueStr = fmtPValue(metrics.p_value)

  // Colour-code expectancy / profit factor / sharpe by sign.
  const expectancyTone =
    metrics.expectancy == null
      ? 'neutral'
      : metrics.expectancy >= 0
        ? 'positive'
        : 'negative'
  const profitFactorTone =
    metrics.profit_factor == null
      ? 'neutral'
      : metrics.profit_factor >= 1
        ? 'positive'
        : 'negative'
  const sharpeTone =
    metrics.sharpe_ratio == null
      ? 'neutral'
      : metrics.sharpe_ratio >= 1
        ? 'positive'
        : metrics.sharpe_ratio >= 0
          ? 'info'
          : 'negative'
  const sortinoTone =
    metrics.sortino_ratio == null
      ? 'neutral'
      : metrics.sortino_ratio >= 1
        ? 'positive'
        : metrics.sortino_ratio >= 0
          ? 'info'
          : 'negative'

  return (
    <div
      className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3"
      data-testid={`${testIdPrefix}-grid`}
    >
      <MetricCard
        label="Win Rate (95% CI)"
        value={winRateDisplay}
        sub={`n=${metrics.n_trades} · ${pValueStr}`}
        tone={winRate != null && winRate >= 0.5 ? 'positive' : 'negative'}
        ciBar={<CIRangeBar low={ciLow} high={ciHigh} point={winRate} />}
        testId={`${testIdPrefix}-winrate`}
      />
      <MetricCard
        label="Profit Factor"
        value={fmtNum(metrics.profit_factor)}
        sub="Gross wins / Gross losses"
        tone={profitFactorTone}
        testId={`${testIdPrefix}-profit-factor`}
      />
      <MetricCard
        label="Expectancy"
        value={fmtPnl(metrics.expectancy)}
        sub="Per-trade expectancy"
        tone={expectancyTone}
        testId={`${testIdPrefix}-expectancy`}
      />
      <MetricCard
        label="Max Drawdown"
        value={fmtPct(metrics.max_drawdown_pct)}
        sub="Peak-to-trough excursion"
        tone="negative"
        testId={`${testIdPrefix}-max-dd`}
      />
      <MetricCard
        label="Sharpe Ratio"
        value={fmtNum(metrics.sharpe_ratio)}
        sub="Risk-adjusted return"
        tone={sharpeTone}
        testId={`${testIdPrefix}-sharpe`}
      />
      <MetricCard
        label="Sortino Ratio"
        value={fmtNum(metrics.sortino_ratio)}
        sub="Downside-adjusted return"
        tone={sortinoTone}
        testId={`${testIdPrefix}-sortino`}
      />
      <MetricCard
        label="Open Exposure"
        value={fmtUsd(metrics.open_exposure)}
        sub="Capital in open positions"
        tone="info"
        testId={`${testIdPrefix}-exposure`}
      />
      <MetricCard
        label="Capital Utilization"
        value={fmtPct(metrics.capital_utilization)}
        sub="Risk budget consumed"
        tone={
          metrics.capital_utilization == null
            ? 'neutral'
            : metrics.capital_utilization > 0.9
              ? 'negative'
              : 'info'
        }
        testId={`${testIdPrefix}-cap-util`}
      />
      <MetricCard
        label="Avg Slippage"
        value={metrics.avg_slippage_bps == null ? 'N/A' : `${metrics.avg_slippage_bps.toFixed(1)} bps`}
        sub="Realized vs quoted mid"
        tone={
          metrics.avg_slippage_bps == null
            ? 'neutral'
            : metrics.avg_slippage_bps > 5
              ? 'negative'
              : 'info'
        }
        testId={`${testIdPrefix}-slippage`}
      />
      <MetricCard
        label="Total Fees"
        value={fmtUsd(metrics.total_fees)}
        sub="Cumulative paid"
        tone="neutral"
        testId={`${testIdPrefix}-fees`}
      />
      <MetricCard
        label="Number of Trades"
        value={fmtInt(metrics.n_trades)}
        sub="Closed positions counted"
        tone="neutral"
        testId={`${testIdPrefix}-n-trades`}
      />
      <MetricCard
        label="Statistical Significance"
        value={isSignificant ? 'Significant' : 'Not significant'}
        sub={pValueStr}
        tone={isSignificant ? 'positive' : 'negative'}
        badge={
          <Badge
            variant={isSignificant ? 'success' : 'warning'}
            className="text-[9px] py-0.5"
            data-testid={`${testIdPrefix}-significance-badge`}
          >
            {isSignificant ? '✓ sig' : '✗ ns'}
          </Badge>
        }
        testId={`${testIdPrefix}-significance`}
      />
    </div>
  )
}

// ── Main panel ────────────────────────────────────────────────────────────

export function PerformanceReportPanel({
  refreshIntervalMs = 30_000,
}: PerformanceReportPanelProps) {
  const [report, setReport] = useState<PerformanceReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [activeCategory, setActiveCategory] = useState<CategoryId>('paper_trading')
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  const fetchReport = useCallback(async () => {
    try {
      const res = await apiFetch('/api/performance/report')
      if (!res.ok) {
        setError(`HTTP ${res.status}`)
        setLoading(false)
        return
      }
      const json: unknown = await res.json()
      // Accept either the new typed shape or the legacy shape (paper object +
      // others as strings). `coerceLegacyShape` returns null only when the
      // response doesn't even have a `disclaimer` string — in which case we
      // fall back to a fully-unavailable report so the panel still renders
      // the metric grid skeleton with N/A values + the always-on disclaimer.
      let next: PerformanceReport | null = null
      if (isPerformanceReport(json)) {
        next = json
      } else {
        next = coerceLegacyShape(json)
      }
      if (!next) {
        next = {
          backtest: makeUnavailable('backtest', 'Backtest experiments not yet run'),
          walk_forward: makeUnavailable('walk_forward', 'Walk-forward analysis pending'),
          paper_trading: makeUnavailable('paper_trading', 'Paper-trading metrics unavailable'),
          live: makeUnavailable('live', 'Live trading not enabled'),
          disclaimer: FALLBACK_DISCLAIMER,
        }
      }
      setReport(next)
      setError(null)
      setLastUpdated(Date.now())
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error')
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch.
  useEffect(() => {
    fetchReport()
  }, [fetchReport])

  // Auto-refresh every `refreshIntervalMs` ms while the document is visible.
  // Pauses when the tab is hidden (matches `useRealtimeData` / `useBot`
  // visibility-aware polling conventions).
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (id != null) return
      id = setInterval(fetchReport, refreshIntervalMs)
    }
    const stop = () => {
      if (id != null) {
        clearInterval(id)
        id = null
      }
    }
    const onVisibility = () => {
      if (document.hidden) stop()
      else start()
    }
    if (typeof document !== 'undefined' && !document.hidden) start()
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    return () => {
      stop()
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    }
  }, [fetchReport, refreshIntervalMs])

  const disclaimer = report?.disclaimer ?? FALLBACK_DISCLAIMER
  const activeMetrics =
    report?.[activeCategory] ?? makeUnavailable(activeCategory, 'No data')

  // Equity curve data — adapt to the EquityCurveChart input shape.
  const equityCurve = activeMetrics.equity_curve ?? []
  const hasEquity = equityCurve.length >= 2

  return (
    <div
      className="flex flex-col gap-3 h-full"
      data-testid="performance-report-panel"
    >
      {/* Header */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-bold text-[#dde1ed]">
            📈 Honest Performance Report
          </span>
          <Badge variant="secondary" className="text-[10px] py-0.5">
            Per-Category
          </Badge>
        </div>
        <div className="flex items-center gap-2 text-[10.5px] text-[#7e8aaa]">
          {loading && (
            <span className="flex items-center gap-1" data-testid="report-loading">
              <span className="spinner" aria-hidden="true" /> Loading…
            </span>
          )}
          {error && (
            <Badge variant="warning" className="text-[9.5px] py-0.5" data-testid="report-error">
              ⚠ {error}
            </Badge>
          )}
          {!loading && !error && lastUpdated && (
            <span data-testid="report-last-updated">
              Updated {new Date(lastUpdated).toLocaleTimeString()}
            </span>
          )}
          <Badge variant="success" className="text-[9.5px] py-0.5" data-testid="auto-refresh-badge">
            ⟳ 30s
          </Badge>
        </div>
      </div>

      {/* Disclaimer banner — ALWAYS rendered, even when fetch fails. */}
      <div
        className="banner-warning p-3 text-[11px] rounded-md"
        role="alert"
        aria-label="Performance Metrics Disclaimer"
        data-testid="performance-disclaimer"
      >
        <div className="font-semibold mb-0.5">⚠ Performance Metrics Disclaimer</div>
        <div>{disclaimer}</div>
      </div>

      {/* Category tabs */}
      <Tabs
        value={activeCategory}
        onValueChange={(v) => setActiveCategory(v as CategoryId)}
        className="w-full"
        data-testid="performance-report-tabs"
      >
        <TabsList className="bg-[#0e1015] border border-[#1f2335]">
          <TabsTrigger value="backtest" data-testid="tab-backtest">
            Backtest
          </TabsTrigger>
          <TabsTrigger value="walk_forward" data-testid="tab-walk-forward">
            Walk-Forward
          </TabsTrigger>
          <TabsTrigger value="paper_trading" data-testid="tab-paper">
            Paper Trading
          </TabsTrigger>
          <TabsTrigger value="live" data-testid="tab-live">
            Live
          </TabsTrigger>
        </TabsList>

        {/* Render content for each category so the trader can switch
            without the layout flashing — the active one is shown, the
            others are radix-hidden but kept mounted (NOT a real
            performance concern; the data is already in memory). */}
        <TabsContent value={activeCategory} forceMount>
          <div className="mt-3 flex flex-col gap-3">
            <CategoryMetricsGrid
              metrics={activeMetrics}
              testIdPrefix={`category-${activeCategory}`}
            />

            {/* Equity curve — only when the category supplies one. */}
            {hasEquity && (
              <Card
                className="bg-[#13161e] border border-[#1f2335] shadow-sm p-3 rounded-md"
                data-testid={`category-${activeCategory}-equity`}
              >
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-bold text-[#dde1ed]">
                    Equity Curve — {activeCategory.replace('_', ' ')}
                  </span>
                  <span className="badge badge-amber text-[9.5px]">
                    {activeMetrics.n_trades} trades
                  </span>
                </div>
                <EquityCurveChart
                  data={equityCurve}
                  height={240}
                  baseline={equityCurve[0]?.equity ?? 100}
                />
              </Card>
            )}

            {/* Raw status string fallback (legacy backend shape). */}
            {activeMetrics.raw && !activeMetrics.available && (
              <Card
                className="bg-[#13161e] border border-[#1f2335] p-3 rounded-md text-[11px] text-[#7e8aaa] leading-relaxed"
                data-testid={`category-${activeCategory}-raw`}
              >
                {activeMetrics.raw}
              </Card>
            )}
          </div>
        </TabsContent>
      </Tabs>
    </div>
  )
}

export default PerformanceReportPanel
