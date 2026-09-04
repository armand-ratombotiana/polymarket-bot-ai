// components/AnalyticsPanel.tsx — Institutional Performance Analytics
//
// W15-5 — Migrated from a self-managed 4-second REST polling loop to
// the hybrid `useRealtimeData` hook. The panel now:
//   1. REST-prefetches /api/analytics on mount.
//   2. Subscribes to the `metrics` WS channel for live push updates.
//      Note: the `metrics` channel pushes the full BotSnapshot, whose
//      shape doesn't match the Analytics object the panel renders. To
//      avoid clobbering the typed state with mismatched data, the hook
//      is given a `validate` predicate that drops any payload missing
//      the `equity` field. When the backend eventually pushes Analytics
//      objects over the metrics channel, the validator will accept them.
//   3. Falls back to polling /api/analytics every 10s when the WS isn't
//      connected.
//   4. Renders a "● Live" / "⟳ Polling" badge so the trader can tell at
//      a glance whether the KPIs are real-time or lagged.
'use client'

import { memo, useEffect, useState } from 'react'
import { fmtUsd, fmtPnl, fmtPct } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { apiFetch } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
// W26-6 — Confidence-interval + statistical-significance widgets.
// Used by the win-rate KPI card to surface (a) the Wilson 95% CI
// visually as a range bar, and (b) the binomial-test verdict
// (significant / not-significant / insufficient-data) as a pill.
import { ConfidenceIntervalBadge } from '@/components/ui/ConfidenceIntervalBadge'
import { StatisticalSignificanceBadge } from '@/components/ui/StatisticalSignificanceBadge'

// W26-6 — Client-side binomial-test p-value approximation (null p=0.5).
// The Analytics object doesn't ship a server-computed p_value for the
// live win-rate KPI (only PaperMetrics in /api/performance/report does).
// We compute a normal-approximation p-value so the significance badge
// has a real number to display; the exact binomial-test value from the
// backend supersedes this whenever the report fetch succeeds (which it
// does for the PaperTrading card in PerformanceReportSection).
function normalCdf(x: number): number {
  // Abramowitz-Stegun 26.2.17 — good to ~7 decimal places, sufficient
  // for the dashboard's "p=0.034" 3dp display.
  const t = 1 / (1 + 0.2316419 * Math.abs(x))
  const d = 0.3989423 * Math.exp(-(x * x) / 2)
  let p =
    d *
    t *
    (0.3193815 +
      t * (-0.3565638 + t * (1.781478 + t * (-1.821256 + t * 1.330274))))
  if (x > 0) p = 1 - p
  return p
}

function binomialPValue(wins: number, n: number): number {
  if (n <= 0) return 1
  const pHat = wins / n
  const z = (pHat - 0.5) / Math.sqrt(0.25 / n)
  return 2 * (1 - normalCdf(Math.abs(z)))
}

interface Analytics {
  equity: number
  realized_pnl: number
  unrealized_pnl: number
  net_pnl: number
  total_trades: number
  winning_trades: number
  losing_trades: number
  closed_trades: number
  open_trades: number
  win_rate: number
  win_rate_ci_low: number | null
  win_rate_ci_high: number | null
  profit_factor: number | string | null
  max_drawdown_dollars: number
  max_drawdown_pct: number
  total_volume_usdc: number
  open_exposure: number
  open_position_count: number
  pending_order_capital: number
  risk_utilization: number
  mode: string
  data_freshness_seconds: number
  peak_equity: number
  active_strategies: string[]
  // S3 — extended KPI metrics (additive)
  avg_win: number | null
  avg_loss: number | null
  expectancy: number | null
  sharpe_ratio: number | null
}

const STRATEGY_LABELS: Record<string, string> = {
  mm_avellaneda_stoikov: 'Avellaneda-Stoikov MM',
  arb_binary_dutch_book: 'Dutch-Book Arb',
  ml_random_forest_quant: 'RF Quant Ensemble',
}

// W15-5 — type guard for the metrics WS channel. The channel is
// specified by the task as `metrics`, whose canonical payload is a
// BotSnapshot (mode / kill_switch / order_books / etc.) — that's NOT
// the Analytics shape this panel renders. We accept only payloads
// that look like Analytics (have the `equity` numeric field); the
// REST polling continues to drive the displayed KPIs in the meantime.
function isAnalyticsPayload(d: unknown): boolean {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return typeof obj.equity === 'number' && typeof obj.win_rate === 'number'
}

// W9-6 — wrapped in React.memo. The component takes no props, so React.memo
// with default shallow compare would never re-render. That's incorrect
// here: the panel self-polls every 4s and updates its own state. React.memo
// on a no-prop component is a no-op (only useful to skip when the parent
// re-renders). It IS valuable when this panel is rendered as a child of a
// frequently-re-rendering parent (the command-center grid re-renders on
// every snapshot tick from useBot), so we wrap it to short-circuit those
// parent-driven re-renders. Internal state updates (data/loading) still
// trigger re-renders normally.
function AnalyticsPanel() {
  // W15-5 — hybrid REST + WS subscription. Replaces the previous 4s
  // self-managed setInterval + visibilitychange listener (the
  // useRealtimeData hook handles both concerns generically).
  const { data, isLoading, isRealtime } = useRealtimeData<Analytics>(
    '/api/analytics',
    {
      wsChannel: 'metrics',
      pollInterval: 10000, // was 4s; relaxed to 10s with WS live updates
      validate: isAnalyticsPayload,
    },
  )

  if (isLoading && !data) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#7e8aaa]">
        <span className="spinner mr-2" aria-hidden="true" />
        Loading analytics metrics…
      </div>
    )
  }

  if (!data) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#7e8aaa]">
        Analytics data unavailable
      </div>
    )
  }

  const n = data.closed_trades ?? (data.winning_trades + data.losing_trades)
  // W26-6 — bumped threshold from 10 → 30 to match the
  // StatisticalSignificanceBadge's MIN_SAMPLE_SIZE. Below 30 closed
  // trades the binomial-test p-value is unreliable (a single lucky
  // streak can produce p<0.05) so we surface the "Insufficient Data"
  // verdict + the small-sample warning banner regardless of p.
  const isSmallSample = n < 30
  const winRatePct = (data.win_rate * 100).toFixed(1)
  // W28-1 — `ciLowPct` / `ciHighPct` (the percentage-formatted CI
  // bounds) were unused after the W26-6 redesign — the CI is rendered
  // directly from `data.win_rate_ci_low` / `_ci_high` (raw floats) by
  // the `ConfidenceIntervalBadge` downstream. Removed to silence
  // TS6133.

  // W26-6 — Compute the win-rate significance verdict client-side.
  // Two sources feed into it:
  //   1. The Wilson 95% CI bounds — if the CI excludes 0.5 (i.e.
  //      both bounds above OR both below 0.5), we reject the null
  //      (p=0.5) at α=0.05.
  //   2. The normal-approximation binomial-test p-value computed
  //      from wins/n — surfaced in the significance badge as
  //      "p=0.034" (3dp) so the trader can see how close to the
  //      threshold the verdict sits.
  const ciExcludes50 =
    data.win_rate_ci_low != null &&
    data.win_rate_ci_high != null &&
    ((data.win_rate_ci_low > 0.5 && data.win_rate_ci_high > 0.5) ||
      (data.win_rate_ci_low < 0.5 && data.win_rate_ci_high < 0.5))
  const winRatePValue = binomialPValue(data.winning_trades, n)
  const isWinRateSignificant = ciExcludes50 && winRatePValue < 0.05

  // Determine trend arrow from CI midpoint vs 50%
  const ciMid = data.win_rate_ci_low != null && data.win_rate_ci_high != null
    ? (data.win_rate_ci_low + data.win_rate_ci_high) / 2
    : data.win_rate
  const trendArrow = ciMid > 0.505 ? '▲' : ciMid < 0.495 ? '▼' : '▶'
  const trendColor = ciMid > 0.505 ? 'text-green-400' : ciMid < 0.495 ? 'text-red-400' : 'text-[#7e8aaa]'

  const activeStrats = data.active_strategies ?? []

  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">📊 Performance Analytics</span>
          <span className="badge badge-amber text-[9.5px]">
            {data.mode?.toUpperCase() || 'PAPER'}
          </span>
          {/* W15-5 — Live / Polling badge. Reflects the underlying
              useRealtimeData transport state. */}
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className={`mono text-xs font-bold ${trendColor}`}>
            {trendArrow}
          </span>
          <span className="mono text-xs text-green-400 font-bold">
            {winRatePct}% Win Rate
          </span>
        </div>
      </div>

      {/* Small sample warning — W26-6 raised threshold from 10 → 30
          to match StatisticalSignificanceBadge.MIN_SAMPLE_SIZE. The
          verdict text is intentionally a separate warning (the badge
          itself surfaces "Insufficient Data" inline next to the metric). */}
      {isSmallSample && (
        <div
          className="banner-warning text-[10.5px] mx-3 mt-2 py-1.5 px-2.5"
          role="alert"
          data-testid="small-sample-warning"
        >
          <span>⚠ Small sample size — results may not be reliable (n={n} &lt; 30)</span>
        </div>
      )}

      {/* W26-6 — Metrics sample-size note. Surfaced unconditionally
          (even when n is large) so the trader always knows the CI
          methodology + sample-size basis of the displayed metrics. */}
      <div
        className="text-[10px] text-[#7e8aaa] mx-3 mt-2"
        data-testid="metrics-sample-note"
      >
        Metrics based on N={n} trades. 95% confidence intervals shown.
      </div>

      {/* Active Strategies Strip */}
      {activeStrats.length > 0 && (
        <div className="px-3 pt-2.5 flex flex-wrap gap-1.5">
          <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold tracking-wider self-center">Active:</span>
          {activeStrats.map((s) => (
            <span key={s} className="badge badge-green text-[9px]">
              ● {STRATEGY_LABELS[s] ?? s.replace(/_/g, ' ')}
            </span>
          ))}
        </div>
      )}

      <div className="p-3 grid grid-cols-2 gap-2 text-[11px]">
        {/* Win Rate + Wilson CI — W26-6 rebuilt around the new
            ConfidenceIntervalBadge + StatisticalSignificanceBadge
            pair. The badge renders the point estimate (72.0%), the
            CI range "[55.0% – 84.0%]" below it, and a horizontal
            range bar visualising where the CI sits on [0, 1]. The
            significance badge sits to the right of the CI badge and
            encodes the binomial-test verdict as a colored pill. */}
        <div className="kpi-card col-span-2" data-testid="win-rate-kpi">
          <div className="flex items-center justify-between mb-1.5">
            <span className="kpi-label">Win Rate (95% CI)</span>
            <StatisticalSignificanceBadge
              pValue={winRatePValue}
              n={n}
              isSignificant={isWinRateSignificant}
            />
          </div>
          <ConfidenceIntervalBadge
            value={data.win_rate}
            ciLower={data.win_rate_ci_low ?? 0}
            ciUpper={data.win_rate_ci_high ?? 1}
            format="percentage"
            significant={isWinRateSignificant}
            pValue={winRatePValue}
            n={n}
            className="w-full"
          />
        </div>

        {/* Profit Factor */}
        <div className="kpi-card">
          <span className="kpi-label">Profit Factor</span>
          <span className="kpi-value text-[#60a5fa]">
            {typeof data.profit_factor === 'number'
              ? data.profit_factor.toFixed(2)
              : data.profit_factor === 'Infinity'
              ? '∞'
              : '—'}
          </span>
          <span className="kpi-sub">Gross wins / Gross losses</span>
        </div>

        {/* Total Trades & Volume */}
        <div className="kpi-card">
          <span className="kpi-label">Trades / Volume</span>
          <span className="kpi-value text-[#dde1ed]">{data.total_trades} trades</span>
          <span className="kpi-sub text-[#22d3ee]">{fmtUsd(data.total_volume_usdc)} vol</span>
        </div>

        {/* Max Drawdown */}
        <div className="kpi-card">
          <span className="kpi-label">Max Drawdown</span>
          <span className="kpi-value text-[#f87171]">
            {fmtUsd(data.max_drawdown_dollars)} ({fmtPct(data.max_drawdown_pct)})
          </span>
          <span className="kpi-sub">Peak: {fmtUsd(data.peak_equity)}</span>
        </div>

        {/* Realized P&L */}
        <div className="kpi-card">
          <span className="kpi-label">Realized P&amp;L</span>
          <span className={`kpi-value ${data.realized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.realized_pnl)}
          </span>
          <span className="kpi-sub">Closed positions today</span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Unrealized P&amp;L</span>
          <span className={`kpi-value ${data.unrealized_pnl >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'}`}>
            {fmtPnl(data.unrealized_pnl)}
          </span>
          <span className="kpi-sub">Mark-to-mid open book</span>
        </div>

        {/* S3 — Expectancy / Trade */}
        <div className="kpi-card">
          <span className="kpi-label">Expectancy / Trade</span>
          <span
            className={`kpi-value ${
              (data.expectancy ?? 0) >= 0 ? 'text-[#4ade80]' : 'text-[#f87171]'
            }`}
          >
            {data.expectancy != null ? fmtPnl(data.expectancy) : '—'}
          </span>
          <span className="kpi-sub">Positive = profitable system</span>
        </div>

        {/* S3 — Avg Win / Avg Loss */}
        <div className="kpi-card">
          <span className="kpi-label">Avg Win / Avg Loss</span>
          <span className="kpi-value flex items-baseline gap-1">
            <span className="text-[#4ade80]">
              {data.avg_win != null ? fmtUsd(data.avg_win) : '—'}
            </span>
            <span className="text-[#7e8aaa] text-[10px]">/</span>
            <span className="text-[#f87171]">
              {data.avg_loss != null ? fmtUsd(data.avg_loss) : '—'}
            </span>
          </span>
          <span className="kpi-sub">Asymmetry check</span>
        </div>

        {/* S3 — Sharpe Ratio */}
        <div className="kpi-card">
          <span className="kpi-label">Sharpe Ratio</span>
          <span
            className={`kpi-value ${
              data.sharpe_ratio == null
                ? 'text-[#dde1ed]'
                : data.sharpe_ratio >= 1
                ? 'text-[#4ade80]'
                : data.sharpe_ratio >= 0
                ? 'text-[#60a5fa]'
                : 'text-[#f87171]'
            }`}
          >
            {data.sharpe_ratio != null ? data.sharpe_ratio.toFixed(2) : '—'}
          </span>
          <span className="kpi-sub">Risk-adjusted return</span>
        </div>
      </div>

      {/* W26-6 — Standalone metrics disclaimer section. The
          PerformanceReportSection below ALSO renders a (shorter)
          disclaimer banner, but the task spec asks for this expanded
          5-bullet version as its own section so the trader can scan it
          without expanding the per-category report. */}
      <MetricsDisclaimerSection n={n} />

      {/* W25-6 — Honest Performance Report (per-category breakdown) +
          disclaimer banner. Fetches /api/performance/report (paper +
          walk-forward + live + disclaimer) and /api/performance/backtest
          (best experiment summary) on mount. The disclaimer banner is
          ALWAYS rendered (it's a static reminder); the per-category
          breakdown is conditionally rendered only when the report fetch
          succeeds AND the response shape matches the expected schema. */}
      <PerformanceReportSection />
    </div>
  )
}

// ── W26-6 — Metrics Disclaimer Section ───────────────────────────────────
// Five-bullet performance-metrics disclaimer. Rendered unconditionally
// (independent of the PerformanceReport fetch) so the trader is always
// warned about the backtest / paper / live distinction, the 95% CI
// convention, and the significance thresholds (α=0.05, n≥30).

function MetricsDisclaimerSection({ n }: { n: number }) {
  return (
    <div
      className="border-t border-[#1f2335] p-3 text-[10.5px] text-[#7e8aaa]"
      data-testid="metrics-disclaimer-section"
      aria-label="Performance Metrics Disclaimer"
    >
      <div className="font-semibold text-[#dde1ed] mb-1">
        ⚠ Performance Metrics Disclaimer
      </div>
      <ul className="space-y-0.5 list-disc pl-4">
        <li>
          Backtest results may be overfit — see walk-forward and paper
          metrics
        </li>
        <li>Only paper/live performance reflects actual system behavior</li>
        <li>Win rate target (95%) is aspirational, not guaranteed</li>
        <li>Metrics are reported with 95% confidence intervals</li>
        <li>Statistical significance requires p &lt; 0.05 and n ≥ 30</li>
      </ul>
      <div className="text-[9.5px] text-[#3e4560] mt-1">
        Current sample: n={n} closed trades
      </div>
    </div>
  )
}

// ── W25-6 — Performance Report Section ─────────────────────────────────────
// Honest per-category breakdown: paper / backtest / walk-forward / live,
// each reported SEPARATELY (never combined) with its own 95% confidence
// interval + binomial-test p-value vs the 50% coin-flip null. The
// disclaimer banner is rendered unconditionally — even when the backend is
// unreachable, the trader is still warned that backtest performance does
// NOT guarantee future results.

interface PaperMetrics {
  category: string
  win_rate: string
  win_rate_ci_95: string
  profit_factor: string
  expectancy: string
  max_drawdown: string
  sharpe_ratio: string
  sortino_ratio: string
  open_exposure: string
  capital_utilization: string
  avg_slippage_bps: string
  total_fees: string
  n_trades: number
  n_wins: number
  n_losses: number
  avg_win: string
  avg_loss: string
  avg_hold_time_hours: string
  p_value: string
  is_statistically_significant: boolean
  period_start: number
  period_end: number
}

interface PerformanceReport {
  paper_trading: PaperMetrics
  backtest: string
  walk_forward: string
  live: string
  disclaimer: string
}

interface BacktestSummary {
  category: 'backtest'
  n_experiments: number
  message?: string
  best_return?: number
  best_sharpe?: number
  best_strategy?: string
  disclaimer?: string
}

function isPerformanceReport(d: unknown): d is PerformanceReport {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return (
    typeof obj.disclaimer === 'string' &&
    typeof obj.backtest === 'string' &&
    typeof obj.walk_forward === 'string' &&
    typeof obj.live === 'string' &&
    typeof obj.paper_trading === 'object' &&
    obj.paper_trading !== null
  )
}

function isBacktestSummary(d: unknown): d is BacktestSummary {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return obj.category === 'backtest' && typeof obj.n_experiments === 'number'
}

function PerformanceReportSection() {
  const [report, setReport] = useState<PerformanceReport | null>(null)
  const [backtest, setBacktest] = useState<BacktestSummary | null>(null)

  useEffect(() => {
    let cancelled = false
    const fetchAll = async () => {
      try {
        const [reportRes, backtestRes] = await Promise.all([
          apiFetch('/api/performance/report'),
          apiFetch('/api/performance/backtest'),
        ])
        if (cancelled) return
        if (reportRes.ok) {
          const json = await reportRes.json()
          if (isPerformanceReport(json)) setReport(json)
        }
        if (backtestRes.ok) {
          const json = await backtestRes.json()
          if (isBacktestSummary(json)) setBacktest(json)
        }
      } catch {
        // Silent failure — the disclaimer banner still renders so the
        // trader is always warned even when the backend is unreachable.
      }
    }
    fetchAll()
    return () => {
      cancelled = true
    }
  }, [])

  const paper = report?.paper_trading
  const backtestReady =
    backtest != null &&
    backtest.n_experiments > 0 &&
    backtest.best_return != null

  return (
    <div
      className="border-t border-[#1f2335] p-3 space-y-2 text-[11px]"
      data-testid="performance-report-section"
    >
      <div className="flex items-center justify-between">
        <span className="text-xs font-bold text-[#dde1ed]">
          📈 Honest Performance Report
        </span>
        <span className="badge badge-amber text-[9px]">Per-Category</span>
      </div>

      {/* Disclaimer banner — ALWAYS rendered (even when fetch failed) */}
      <div
        className="banner-warning py-1.5 px-2.5 text-[10.5px] space-y-0.5"
        data-testid="performance-disclaimer"
        aria-label="Performance Metrics Disclaimer"
      >
        <div className="font-semibold">
          ⚠ Performance Metrics Disclaimer
        </div>
        <div>
          Backtest results may be overfit and do not guarantee future performance.
        </div>
        <div>
          Only paper-trading and live metrics reflect actual system behavior.
        </div>
        <div>Win rate target (95%) is aspirational, not guaranteed.</div>
      </div>

      {/* Per-category breakdown — conditionally rendered when the
          report fetch succeeded AND the response shape validated. */}
      {report && paper && (
        <div className="grid grid-cols-2 gap-2">
          {/* Paper Trading metrics */}
          <div className="kpi-card col-span-2">
            <div className="flex items-center justify-between mb-1">
              <span className="kpi-label">Paper Trading</span>
              <span className="text-[10px] text-[#4ade80]">
                Real-time · honest
              </span>
            </div>
            <div className="grid grid-cols-3 gap-1.5">
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Win Rate (paper)
                </span>
                <div className="text-[#4ade80] font-semibold">
                  {paper.win_rate}
                </div>
                <div className="text-[9px] text-[#7e8aaa]">
                  95% CI: {paper.win_rate_ci_95}
                </div>
              </div>
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Profit Factor (paper)
                </span>
                <div className="text-[#60a5fa] font-semibold">
                  {paper.profit_factor}
                </div>
              </div>
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Expectancy (paper)
                </span>
                <div className="text-[#dde1ed] font-semibold">
                  {paper.expectancy}
                </div>
              </div>
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Sharpe (paper)
                </span>
                <div className="text-[#dde1ed] font-semibold">
                  {paper.sharpe_ratio}
                </div>
              </div>
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Max DD (paper)
                </span>
                <div className="text-[#f87171] font-semibold">
                  {paper.max_drawdown}
                </div>
              </div>
              <div>
                <span className="text-[10px] text-[#7e8aaa] uppercase">
                  Trades (paper)
                </span>
                <div className="text-[#dde1ed] font-semibold">
                  {paper.n_trades}
                </div>
                <div className="text-[9px] text-[#7e8aaa]">
                  p={paper.p_value}
                </div>
              </div>
            </div>
          </div>

          {/* Backtest summary */}
          <div className="kpi-card">
            <div className="flex items-center justify-between mb-1">
              <span className="kpi-label">Backtest Summary</span>
              <span className="text-[10px] text-amber-400">⚠ Overfit risk</span>
            </div>
            {backtestReady && backtest ? (
              <div className="space-y-0.5 text-[10.5px]">
                <div>
                  Best Return:{' '}
                  <span className="text-[#4ade80] font-semibold">
                    {((backtest.best_return ?? 0) * 100).toFixed(2)}%
                  </span>
                </div>
                <div>
                  Best Sharpe:{' '}
                  <span className="text-[#60a5fa] font-semibold">
                    {(backtest.best_sharpe ?? 0).toFixed(2)}
                  </span>
                </div>
                <div>
                  Strategy:{' '}
                  <span className="text-[#dde1ed]">
                    {backtest.best_strategy ?? 'unknown'}
                  </span>
                </div>
                <div>
                  Experiments:{' '}
                  <span className="text-[#dde1ed]">{backtest.n_experiments}</span>
                </div>
              </div>
            ) : (
              <div className="text-[10.5px] text-[#7e8aaa]">
                No backtest experiments yet — run a backtest to populate this
                section.
              </div>
            )}
          </div>

          {/* Walk-forward summary */}
          <div className="kpi-card">
            <div className="flex items-center justify-between mb-1">
              <span className="kpi-label">Walk-Forward</span>
              <span className="text-[10px] text-[#4ade80]">Out-of-sample</span>
            </div>
            <div className="text-[10.5px] text-[#7e8aaa] leading-tight">
              {report.walk_forward}
            </div>
          </div>

          {/* Live status */}
          <div className="kpi-card col-span-2">
            <div className="flex items-center justify-between mb-1">
              <span className="kpi-label">Live Status</span>
              <span className="text-[10px] text-amber-400">Paper mode</span>
            </div>
            <div className="text-[10.5px] text-[#7e8aaa] leading-tight">
              {report.live}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// W9-6 — React.memo (no props, default shallow compare). Skips
// re-renders triggered purely by parent re-renders (e.g. useBot snapshot
// updates that don't affect this panel). Internal state updates
// (data/loading) still re-render normally because they originate inside
// the component.
export default memo(AnalyticsPanel)