// components/PortfolioRiskPanel.tsx — Real-time P&L heatmap + correlation matrix.
//
// W16-1 — Combines the new PnLHeatmap + CorrelationMatrix + an exposure /
// VaR / diversification KPI strip into a single self-contained panel.
// Mounted under the Sidebar's new "Risk Matrix" nav item.
//
// Data flow:
//   1. The panel self-fetches /api/analytics/risk-summary on mount. That
//      endpoint (added in api/server.py) bundles:
//        • total_exposure, max_single_position_exposure, open_position_count
//        • diversification_score (1 - mean |r| across the upper triangle)
//        • value_at_risk_95 (historical VaR from the equity-curve deltas)
//        • expected_shortfall_95 (CVaR — average of the worst 5% tail)
//        • correlation_matrix (the N×N Pearson matrix payload)
//   2. The panel separately reads `positions` (passed as a prop by
//      page.tsx via the useBot snapshot) so the heatmap can map the
//      position array to PnLHeatmapDatum instances without an extra
//      round-trip. When the prop is omitted, the panel self-fetches
//      /api/positions via useRealtimeData (mirrors PositionsPanel's
//      pattern).
//   3. Auto-refresh every 30s — paused when the document is hidden so
//      a background tab doesn't burn backend quota.
//
// Layout:
//   • Top KPI strip — 5 KPI cards (exposure / max single / diversification /
//     VaR-95 / ES-95) + a "refresh in Xs" countdown chip.
//   • Two-column grid (lg+) — heatmap on the left, correlation matrix on
//     the right. Stacks vertically below `md` width.
//   • Exposure breakdown — list of per-position exposure with the
//     max-position badge highlighted.
'use client'

import { useCallback, useEffect, useMemo, useRef, useState, memo } from 'react'
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import {
  Activity,
  RefreshCw,
  AlertTriangle,
  Layers,
  TrendingDown,
  Gauge,
  Shield,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import { fmtUsd, fmtPct } from '@/lib/design-tokens'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import {
  PnLHeatmap,
  type PnLHeatmapDatum,
  CorrelationMatrix,
  type CorrelationMatrixPayload,
} from '@/components/charts'
import type { Position } from '@/hooks/useBot'

// ── Backend payload shape ────────────────────────────────────────────────
// Mirrors `core/correlation.py::compute_risk_summary()` exactly. Optional
// fields are nullable so the panel renders a "—" placeholder when the
// backend can't compute them (e.g. VaR needs ≥2 equity-curve points;
// the very first dashboard load before any fills have landed).
interface RiskSummaryPayload {
  total_exposure: number
  max_single_position_exposure: number
  open_position_count: number
  diversification_score: number
  value_at_risk_95: number | null
  expected_shortfall_95: number | null
  correlation_matrix: CorrelationMatrixPayload
  computed_at: number
}

interface PositionsApiResponse {
  positions: Position[]
}

export interface PortfolioRiskPanelProps {
  /** Optional override — page.tsx threads the useBot snapshot through. */
  positions?: Position[]
  /** Optional override — true when the WS connection is live. */
  isRealtime?: boolean
  /** Refresh interval (ms). Default 30_000. */
  refreshIntervalMs?: number
  /** Optional className passthrough. */
  className?: string
}

const POLL_INTERVAL_MS = 30_000

// ── Helpers ──────────────────────────────────────────────────────────────

/**
 * Map a useBot Position into a PnLHeatmapDatum. The heatmap renders the
 * (realised + unrealised) P&L per cell; when the backend hasn't
 * populated `unrealized_pnl`, we fall back to the realised figure so
 * the cell still shows a meaningful magnitude.
 */
function positionToDatum(p: Position): PnLHeatmapDatum {
  const info = formatHierarchicalMarket(p.slug)
  const yes = p.yes_shares
  const no = p.no_shares ?? 0
  const outcome: PnLHeatmapDatum['outcome'] = yes > 0 ? 'YES' : no > 0 ? 'NO' : 'FLAT'
  const shares = yes > 0 ? yes : no
  const realised = p.realised_pnl ?? 0
  const unrealised = typeof p.unrealized_pnl === 'number' ? p.unrealized_pnl : 0
  const pnl = realised + unrealised
  const cost = p.total_invested ?? 0
  const pnlPct = cost > 0 ? pnl / cost : Number.NaN
  return {
    tokenId: p.token_id,
    label: info.question || info.fullLabel || p.slug || p.token_id,
    outcome,
    shares,
    entryPrice: p.avg_entry_price,
    currentPrice: typeof p.current_price === 'number' ? p.current_price : null,
    positionSize: cost,
    pnl,
    pnlPct,
  }
}

function pnlTextColor(v: number): string {
  if (v > 0) return '#4ade80'
  if (v < 0) return '#f87171'
  return '#7e8aaa'
}

// ── KPI strip ────────────────────────────────────────────────────────────
interface KpiCardProps {
  label: string
  value: string
  sub?: string
  icon: typeof Activity
  valueColor?: string
}

function KpiCard({ label, value, sub, icon: Icon, valueColor }: KpiCardProps) {
  return (
    <div className="kpi-card" data-testid={`risk-kpi-${label.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`}>
      <span className="kpi-label flex items-center gap-1">
        <Icon className="w-3 h-3" aria-hidden="true" />
        {label}
      </span>
      <span
        className="kpi-value mono"
        style={{ color: valueColor ?? '#dde1ed' }}
      >
        {value}
      </span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  )
}

// ── Main panel ───────────────────────────────────────────────────────────
function PortfolioRiskPanelImpl({
  positions: positionsOverride,
  isRealtime: isRealtimeOverride,
  refreshIntervalMs = POLL_INTERVAL_MS,
  className,
}: PortfolioRiskPanelProps) {
  const [summary, setSummary] = useState<RiskSummaryPayload | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [secondsToRefresh, setSecondsToRefresh] = useState<number>(Math.floor(refreshIntervalMs / 1000))
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const countdownRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Fetch positions via useRealtimeData so the panel can render the
  // heatmap even when no `positions` prop is supplied (mirrors the
  // PositionsPanel pattern). The override takes precedence when present.
  const {
    data: fetchedPositions,
    isLoading: positionsLoading,
    isRealtime: wsIsRealtime,
  } = useRealtimeData<PositionsApiResponse>('/api/positions', {
    wsChannel: 'positions',
    pollInterval: 5000,
  })

  const positions = positionsOverride ?? fetchedPositions?.positions ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  const doFetch = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await apiFetch('/api/analytics/risk-summary')
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`)
      }
      const json = (await res.json()) as RiskSummaryPayload
      setSummary(json)
      setLastUpdated(Date.now())
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error'
      setError(msg)
    } finally {
      setIsLoading(false)
    }
  }, [])

  // Initial fetch + 30s auto-refresh (paused when document hidden).
  useEffect(() => {
    doFetch()
  }, [doFetch])

  useEffect(() => {
    const start = () => {
      if (timerRef.current) return
      timerRef.current = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        doFetch()
      }, refreshIntervalMs)
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
        doFetch()
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
  }, [doFetch, refreshIntervalMs])

  // 1s countdown to "next refresh in Xs" — purely cosmetic.
  useEffect(() => {
    setSecondsToRefresh(Math.floor(refreshIntervalMs / 1000))
    countdownRef.current = setInterval(() => {
      setSecondsToRefresh((prev) => {
        if (prev <= 1) {
          return Math.floor(refreshIntervalMs / 1000)
        }
        return prev - 1
      })
    }, 1000)
    return () => {
      if (countdownRef.current) {
        clearInterval(countdownRef.current)
        countdownRef.current = null
      }
    }
  }, [refreshIntervalMs])

  // Map positions → heatmap datums. Memoised so we don't recompute on
  // every parent re-render (only when the positions array identity
  // actually changes).
  const heatData = useMemo(() => positions.map(positionToDatum), [positions])

  const corrPayload = summary?.correlation_matrix ?? null

  // ── Loading state ────────────────────────────────────────────────────────
  if (isLoading && !summary) {
    return (
      <div
        data-testid="portfolio-risk-loading"
        className={`card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md ${className ?? ''}`}
      >
        <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
          <div className="flex items-center gap-2">
            <Layers className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              Portfolio Risk Matrix
            </span>
          </div>
          <span className="spinner" aria-hidden="true" />
        </div>
        <div className="p-3 space-y-3">
          <div className="grid-kpi">
            {[0, 1, 2, 3, 4].map((i) => (
              <div key={i} className="kpi-card">
                <div className="skeleton h-3 w-20 mb-2" />
                <div className="skeleton h-5 w-24 mb-1" />
                <div className="skeleton h-2 w-16" />
              </div>
            ))}
          </div>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <div className="skeleton-card p-2.5 h-64" />
            <div className="skeleton-card p-2.5 h-64" />
          </div>
        </div>
      </div>
    )
  }

  // ── Error state ──────────────────────────────────────────────────────────
  if (error && !summary) {
    return (
      <div
        data-testid="portfolio-risk-error"
        className={`card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md ${className ?? ''}`}
      >
        <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
          <div className="flex items-center gap-2">
            <Layers className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              Portfolio Risk Matrix
            </span>
          </div>
        </div>
        <div className="error-state">
          <AlertTriangle className="error-state-icon text-[#f87171]" aria-hidden="true" />
          <div className="error-state-title">Risk matrix unavailable</div>
          <div className="error-state-desc">{error}</div>
          <button
            type="button"
            onClick={() => doFetch()}
            className="btn btn-ghost btn-sm mt-2"
          >
            <RefreshCw className="w-3 h-3" aria-hidden="true" />
            Retry
          </button>
        </div>
      </div>
    )
  }

  const totalExposure = summary?.total_exposure ?? 0
  const maxSingle = summary?.max_single_position_exposure ?? 0
  const diversification = summary?.diversification_score ?? 1
  const var95 = summary?.value_at_risk_95 ?? null
  const es95 = summary?.expected_shortfall_95 ?? null
  const openCount = summary?.open_position_count ?? positions.length

  const diversificationColor =
    diversification >= 0.7 ? '#4ade80' : diversification >= 0.4 ? '#fbbf24' : '#f87171'

  return (
    <div
      data-testid="portfolio-risk-panel"
      className={`card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md ${className ?? ''}`}
    >
      {/* ── Header ────────────────────────────────────────────────────────── */}
      <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <Layers className="w-3.5 h-3.5 text-[#22d3ee]" aria-hidden="true" />
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            Portfolio Risk Matrix
          </span>
          <span className="badge badge-amber text-[9.5px]">
            {openCount} {openCount === 1 ? 'position' : 'positions'}
          </span>
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>
        <div className="flex items-center gap-2 text-[10px] text-[#7e8aaa]">
          {lastUpdated && (
            <span title={`Last updated: ${new Date(lastUpdated).toLocaleString()}`}>
              updated {new Date(lastUpdated).toLocaleTimeString()}
            </span>
          )}
          <span className="mono" title={`Auto-refresh in ${secondsToRefresh}s`}>
            ⟳ {secondsToRefresh}s
          </span>
          <button
            type="button"
            onClick={() => doFetch()}
            className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
            title="Refresh now"
            aria-label="Refresh risk matrix now"
          >
            <RefreshCw className="w-3 h-3" aria-hidden="true" />
            Refresh
          </button>
        </div>
      </div>

      <div className="p-3 flex flex-col gap-3">
        {/* ── KPI strip ────────────────────────────────────────────────────── */}
        <div className="grid-kpi text-[11px]">
          <KpiCard
            label="Total Exposure"
            value={fmtUsd(totalExposure)}
            sub="Sum of cost basis"
            icon={Activity}
          />
          <KpiCard
            label="Max Single"
            value={fmtUsd(maxSingle)}
            sub="Largest position"
            icon={TrendingDown}
            valueColor={maxSingle > 0.6 * totalExposure && totalExposure > 0 ? '#fbbf24' : '#dde1ed'}
          />
          <KpiCard
            label="Diversification"
            value={fmtPct(diversification, 0)}
            sub="1 − mean|ρ|"
            icon={Shield}
            valueColor={diversificationColor}
          />
          <KpiCard
            label="VaR 95%"
            value={var95 == null ? '—' : fmtUsd(var95)}
            sub="1-period historical"
            icon={Gauge}
            valueColor={var95 == null ? '#7e8aaa' : '#fbbf24'}
          />
          <KpiCard
            label="Expected Shortfall 95%"
            value={es95 == null ? '—' : fmtUsd(es95)}
            sub="Avg worst-5% tail"
            icon={TrendingDown}
            valueColor={es95 == null ? '#7e8aaa' : '#f87171'}
          />
        </div>

        {/* ── Heatmap + Correlation matrix (2-col on lg+) ───────────────── */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          <Card className="bg-[#0e1015] border-[#1f2335]">
            <CardHeader className="p-3 pb-2">
              <CardTitle className="text-xs flex items-center gap-2 text-[#dde1ed]">
                <span className="text-[#22d3ee]">🔥</span>
                P&amp;L Heatmap
                <span className="text-[9.5px] text-[#7e8aaa] font-normal">
                  per-position · green = profit · red = loss
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-1">
              {heatData.length === 0 ? (
                <div
                  data-testid="portfolio-risk-heatmap-empty"
                  className="flex items-center justify-center text-xs text-[#7e8aaa] py-8"
                >
                  No open positions to render.
                </div>
              ) : positionsLoading && heatData.length === 0 ? (
                <div className="flex items-center justify-center text-xs text-[#7e8aaa] py-8">
                  <span className="spinner mr-2" aria-hidden="true" />
                  Loading positions…
                </div>
              ) : (
                <PnLHeatmap data={heatData} cellHeight={64} cellMinWidth={150} />
              )}
            </CardContent>
          </Card>

          <Card className="bg-[#0e1015] border-[#1f2335]">
            <CardHeader className="p-3 pb-2">
              <CardTitle className="text-xs flex items-center gap-2 text-[#dde1ed]">
                <span className="text-[#22d3ee]">⊞</span>
                Correlation Matrix
                <span className="text-[9.5px] text-[#7e8aaa] font-normal">
                  Pearson · ρ ∈ [−1, +1]
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="p-3 pt-1">
              {corrPayload ? (
                <CorrelationMatrix matrix={corrPayload} cellSize={48} />
              ) : (
                <div
                  data-testid="portfolio-risk-matrix-empty"
                  className="flex items-center justify-center text-xs text-[#7e8aaa] py-8"
                >
                  Correlation matrix unavailable.
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* ── Exposure breakdown ─────────────────────────────────────────── */}
        <Card className="bg-[#0e1015] border-[#1f2335]">
          <CardHeader className="p-3 pb-2">
            <CardTitle className="text-xs flex items-center gap-2 text-[#dde1ed]">
              <span className="text-[#22d3ee]">📊</span>
              Exposure Breakdown
              <span className="text-[9.5px] text-[#7e8aaa] font-normal">
                per-position · largest highlighted
              </span>
            </CardTitle>
          </CardHeader>
          <CardContent className="p-3 pt-1">
            {heatData.length === 0 ? (
              <div className="flex items-center justify-center text-xs text-[#7e8aaa] py-4">
                No open positions.
              </div>
            ) : (
              <ExposureBreakdown
                data={heatData}
                maxMagnitude={heatData.reduce((acc, d) => Math.max(acc, d.positionSize), 0)}
              />
            )}
          </CardContent>
        </Card>

        {error && (
          <div
            className="banner-warning text-[10.5px] py-1.5 px-2.5"
            role="alert"
            data-testid="portfolio-risk-stale"
          >
            <span aria-hidden="true">⚠️</span>
            <span>Stale data — last refresh failed: {error}</span>
          </div>
        )}
      </div>
    </div>
  )
}

interface ExposureBreakdownProps {
  data: PnLHeatmapDatum[]
  maxMagnitude: number
}

function ExposureBreakdownImpl({ data, maxMagnitude }: ExposureBreakdownProps) {
  const sorted = useMemo(
    () => [...data].sort((a, b) => b.positionSize - a.positionSize),
    [data],
  )
  if (maxMagnitude <= 0) {
    return (
      <div className="text-xs text-[#7e8aaa] py-2">No exposure to break down.</div>
    )
  }
  return (
    <div
      data-testid="portfolio-risk-exposure-breakdown"
      className="flex flex-col gap-1.5 max-h-72 overflow-y-auto scrollbar-thin"
    >
      {sorted.map((d, i) => {
        const pct = (d.positionSize / maxMagnitude) * 100
        const isMax = i === 0
        return (
          <div
            key={d.tokenId}
            className="flex items-center gap-2 text-xs"
            data-testid={`exposure-row-${d.tokenId}`}
          >
            <span
              className="mono text-[#7e8aaa] text-[10px] w-6 text-right"
              aria-hidden="true"
            >
              {i + 1}.
            </span>
            <span
              className="flex-1 truncate text-[#dde1ed]"
              title={d.label}
            >
              {isMax && (
                <span className="badge badge-amber text-[9px] mr-1.5 py-0">MAX</span>
              )}
              <span className="text-[9px] uppercase font-bold tracking-wide text-cyan-400 mr-1.5">
                {d.outcome}
              </span>
              {d.label}
            </span>
            <div className="w-32 h-1.5 bg-[#1f2335] rounded-full overflow-hidden">
              <div
                className="h-full rounded-full transition-all"
                style={{
                  width: `${pct.toFixed(1)}%`,
                  background: isMax ? '#fbbf24' : '#22d3ee',
                }}
              />
            </div>
            <span className="mono font-bold text-cyan-300 w-20 text-right">
              {fmtUsd(d.positionSize)}
            </span>
            <span
              className="mono w-16 text-right font-bold"
              style={{ color: pnlTextColor(d.pnl) }}
            >
              {d.pnl >= 0 ? '+' : '−'}${Math.abs(d.pnl).toFixed(2)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

const ExposureBreakdown = memo(ExposureBreakdownImpl)
const PortfolioRiskPanel = memo(PortfolioRiskPanelImpl)
export default PortfolioRiskPanel
