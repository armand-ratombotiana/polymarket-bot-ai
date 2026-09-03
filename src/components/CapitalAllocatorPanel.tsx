// components/CapitalAllocatorPanel.tsx — Saturating edge-curve capital allocator panel
//
// Exposes the capital_allocator backend (Michaelis-Menten saturating edge curve,
// multiplier-based sizing stack) on the Polymarket trading bot dashboard.
//
// Backend endpoints (verified in `register_routes` of
// `mini-services/polymarket-bot/core/capital_allocator.py` and `api/server.py`):
//
//   • GET /api/capital/allocation   — what-if sizing + component breakdown
//   • GET /api/exposure             — capital deployed, per-strategy exposure
//   • GET /api/positions/closed     — recent closed allocations (edge, conf, size)
//
// The allocator's curve constants (k = EDGE_K_M, α = EDGE_V_MAX,
// max position, max exposure) are Python module-level constants with no
// GET endpoint to read them — we display the documented values from the
// module docstring and confirm them against the breakdown response.
//
// Visual style mirrors MLPanel.tsx: dark card `bg-[#13161e]`, border
// `border-[#1f2335]`, mono numerics, badge pills, skeleton shimmer
// while loading, error banner with retry.
'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { fmtUsd, fmtPct, fmtAge } from '@/lib/design-tokens'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import {
  Activity,
  AlertTriangle,
  Banknote,
  Coins,
  Crosshair,
  Gauge as GaugeIcon,
  History,
  Layers,
  RefreshCw,
  Settings,
  TrendingUp,
  Wallet,
  Zap,
} from 'lucide-react'
import { GaugeChart } from '@/components/charts'

// ─────────────────────────────────────────────────────────────────────────────
// Types
// ─────────────────────────────────────────────────────────────────────────────

interface AllocatorComponents {
  raw_size: number
  confidence_mult: number
  calibration_mult: number
  drawdown_mult: number
  correlation_mult: number
  performance_mult: number
  liquidity_mult: number
  product_mult: number
}

interface AllocatorBreakdown {
  strategy: string
  edge: number
  confidence: number
  liquidity_usd: number
  existing_exposure_usd: number
  drawdown_usd: number
  strategy_performance: Record<string, number> | null
  brier_override: number | null
  model_brier: number | null
  size_usd: number
  cap_usd: number
  drawdown_limit_usd: number
  edge_k_m: number
  edge_v_max: number
  liquidity_k: number
  components: AllocatorComponents
}

interface ExposureReport {
  capital_invested: number
  reserved_for_pending_orders: number
  gross_market_value: number
  net_directional_exposure: number
  maximum_remaining_loss: number
  exposure_per_group: Record<string, number>
  exposure_per_strategy: Record<string, number>
  exposure_duration_hours_avg: number
  exposure_dollar_days: number
  available_cash: number
  reserved_cash: number
  open_position_count: number
}

interface ClosedPosition {
  id: number
  timestamp: number
  position_id: string
  token_id: string
  strategy: string
  entry_price: number | null
  exit_price: number | null
  shares: number | null
  pnl: number | null
  holding_seconds: number | null
  model_version: string | null
  decision_id: string | null
  direction: string | null
  confidence: number | null
  predicted_edge: number | null
  p_yes: number | null
  market_mid: number | null
  liquidity: number | null
  data: Record<string, unknown> | null
}

interface AllocatorConfig {
  k: number         // EDGE_K_M (Michaelis-Menten half-saturation edge)
  alpha: number      // EDGE_V_MAX (asymptotic max position size, USD)
  max_edge: number   // display range ceiling (decimal, e.g. 0.20 = 20%)
  max_position_size: number
  max_exposure: number
  operating_capital: number
}

// ─────────────────────────────────────────────────────────────────────────────
// Static allocator constants — mirror of `core/capital_allocator.py`
// ─────────────────────────────────────────────────────────────────────────────
// The backend surfaces `edge_k_m`, `edge_v_max`, `liquidity_k`, `cap_usd`,
// `drawdown_limit_usd` via the breakdown response, so we use those values
// when available and fall back to these documented constants.
const DEFAULT_CONFIG: AllocatorConfig = {
  k: 0.05,
  alpha: 3.00,
  max_edge: 0.20,
  max_position_size: 3.00,
  max_exposure: 5.00,
  operating_capital: 200.00, // BANKROLL_CEILING from api/server.py
}

const POLL_INTERVAL_MS = 15_000
const RECENT_ALLOCATIONS_LIMIT = 20

// ─────────────────────────────────────────────────────────────────────────────
// Saturating edge curve (Michaelis-Menten) — mirrors `saturating_edge()` in
// capital_allocator.py so the panel can render the curve locally without
// an extra round-trip per pixel.
// ─────────────────────────────────────────────────────────────────────────────
function saturatingEdge(edge: number, k: number, vMax: number): number {
  const e = Math.max(0, Number(edge) || 0)
  if (e <= 0) return 0
  return (vMax * e) / (k + e)
}

// Utilization color thresholds — <50% green, 50–80% amber, >80% red.
function utilizationStyle(pct: number): {
  badge: string
  text: string
  stroke: string
  ring: string
} {
  if (pct > 80) {
    return {
      badge: 'badge-red',
      text: 'text-red-400',
      stroke: '#ef4444',
      ring: 'rgba(239, 68, 68, 0.18)',
    }
  }
  if (pct >= 50) {
    return {
      badge: 'badge-amber',
      text: 'text-amber-400',
      stroke: '#f59e0b',
      ring: 'rgba(245, 158, 11, 0.18)',
    }
  }
  return {
    badge: 'badge-green',
    text: 'text-emerald-400',
    stroke: '#22c55e',
    ring: 'rgba(34, 197, 94, 0.18)',
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Edge → Size curve SVG
// ─────────────────────────────────────────────────────────────────────────────
function EdgeSizeCurve({
  k,
  vMax,
  maxEdge,
  operatingEdge,
  operatingSize,
}: {
  k: number
  vMax: number
  maxEdge: number
  operatingEdge: number | null
  operatingSize: number | null
}) {
  // SVG coordinate system — 0..440 wide, 0..200 tall.
  const W = 440
  const H = 200
  const PAD_L = 36
  const PAD_R = 14
  const PAD_T = 14
  const PAD_B = 28
  const plotW = W - PAD_L - PAD_R
  const plotH = H - PAD_T - PAD_B

  // Sample the curve at ~40 points across [0, maxEdge].
  const samples = 40
  const pts: Array<{ x: number; y: number; edge: number; size: number }> = []
  for (let i = 0; i <= samples; i++) {
    const edge = (i / samples) * maxEdge
    const size = saturatingEdge(edge, k, vMax)
    pts.push({
      x: PAD_L + (edge / maxEdge) * plotW,
      y: PAD_T + (1 - size / vMax) * plotH,
      edge,
      size,
    })
  }
  const pathD = pts
    .map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(2)} ${p.y.toFixed(2)}`)
    .join(' ')

  // Half-saturation reference (edge = k → size = vMax / 2).
  const halfX = PAD_L + (k / maxEdge) * plotW
  const halfY = PAD_T + (1 - 0.5) * plotH

  // Current operating point.
  const op = (() => {
    if (operatingEdge == null || operatingSize == null) return null
    const e = Math.max(0, Math.min(operatingEdge, maxEdge))
    const s = Math.max(0, Math.min(operatingSize, vMax))
    return {
      x: PAD_L + (e / maxEdge) * plotW,
      y: PAD_T + (1 - s / vMax) * plotH,
    }
  })()

  // Y-axis gridlines at 0, vMax/4, vMax/2, 3vMax/4, vMax.
  const yTicks = [0, 0.25, 0.5, 0.75, 1].map((f) => ({
    f,
    y: PAD_T + (1 - f) * plotH,
    val: f * vMax,
  }))

  // X-axis ticks at 0, 5%, 10%, 15%, 20%.
  const xTicks = [0, 0.05, 0.10, 0.15, 0.20]
    .filter((t) => t <= maxEdge)
    .map((t) => ({ t, x: PAD_L + (t / maxEdge) * plotW }))

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      className="w-full h-full"
      role="img"
      aria-label="Saturating edge to position size curve"
    >
      {/* Background plot area */}
      <rect
        x={PAD_L}
        y={PAD_T}
        width={plotW}
        height={plotH}
        fill="#0a0c12"
        stroke="#1f2335"
        strokeWidth="1"
      />

      {/* Y gridlines */}
      {yTicks.map((t) => (
        <g key={`y-${t.f}`}>
          <line
            x1={PAD_L}
            y1={t.y}
            x2={W - PAD_R}
            y2={t.y}
            stroke="#1f2335"
            strokeWidth="1"
            strokeDasharray="2 3"
          />
          <text
            x={PAD_L - 5}
            y={t.y + 3}
            fill="#5a637a"
            fontSize="9"
            textAnchor="end"
            fontFamily="JetBrains Mono, monospace"
          >
            ${t.val.toFixed(2)}
          </text>
        </g>
      ))}

      {/* X axis ticks */}
      {xTicks.map((t) => (
        <g key={`x-${t.t}`}>
          <line
            x1={t.x}
            y1={H - PAD_B}
            x2={t.x}
            y2={H - PAD_B + 3}
            stroke="#3e4560"
            strokeWidth="1"
          />
          <text
            x={t.x}
            y={H - PAD_B + 16}
            fill="#5a637a"
            fontSize="9"
            textAnchor="middle"
            fontFamily="JetBrains Mono, monospace"
          >
            {(t.t * 100).toFixed(0)}%
          </text>
        </g>
      ))}

      {/* Half-saturation reference line (edge = k → size = vMax/2) */}
      <line
        x1={halfX}
        y1={PAD_T}
        x2={halfX}
        y2={H - PAD_B}
        stroke="#3b82f6"
        strokeWidth="1"
        strokeDasharray="2 3"
        strokeOpacity="0.45"
      />
      <text
        x={halfX + 4}
        y={PAD_T + 10}
        fill="#60a5fa"
        fontSize="8.5"
        fontFamily="JetBrains Mono, monospace"
      >
        k={(k * 100).toFixed(1)}%
      </text>
      <line
        x1={PAD_L}
        y1={halfY}
        x2={halfX}
        y2={halfY}
        stroke="#3b82f6"
        strokeWidth="1"
        strokeDasharray="2 3"
        strokeOpacity="0.45"
      />

      {/* Asymptote (vMax) */}
      <line
        x1={PAD_L}
        y1={PAD_T}
        x2={W - PAD_R}
        y2={PAD_T}
        stroke="#3b82f6"
        strokeWidth="1"
        strokeDasharray="3 3"
        strokeOpacity="0.35"
      />
      <text
        x={W - PAD_R}
        y={PAD_T - 4}
        fill="#60a5fa"
        fontSize="8.5"
        textAnchor="end"
        fontFamily="JetBrains Mono, monospace"
      >
        α = ${vMax.toFixed(2)}
      </text>

      {/* Saturating curve */}
      <path
        d={pathD}
        fill="none"
        stroke="#22d3ee"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      {/* Area under curve (subtle) */}
      <path
        d={`${pathD} L ${pts[pts.length - 1].x.toFixed(2)} ${H - PAD_B} L ${pts[0].x.toFixed(2)} ${H - PAD_B} Z`}
        fill="rgba(34, 211, 238, 0.06)"
        stroke="none"
      />

      {/* Current operating point */}
      {op && (
        <g>
          <line
            x1={op.x}
            y1={op.y}
            x2={op.x}
            y2={H - PAD_B}
            stroke="#fbbf24"
            strokeWidth="1"
            strokeDasharray="2 2"
            strokeOpacity="0.6"
          />
          <line
            x1={PAD_L}
            y1={op.y}
            x2={op.x}
            y2={op.y}
            stroke="#fbbf24"
            strokeWidth="1"
            strokeDasharray="2 2"
            strokeOpacity="0.6"
          />
          <circle
            cx={op.x}
            cy={op.y}
            r="5"
            fill="#fbbf24"
            stroke="#0e1015"
            strokeWidth="2"
          />
          <circle cx={op.x} cy={op.y} r="9" fill="none" stroke="#fbbf24" strokeOpacity="0.3" strokeWidth="1" />
        </g>
      )}

      {/* Axis labels */}
      <text
        x={PAD_L + plotW / 2}
        y={H - 2}
        fill="#7e8aaa"
        fontSize="9.5"
        textAnchor="middle"
        fontWeight="600"
      >
        Predicted Edge →
      </text>
      <text
        x={10}
        y={PAD_T + plotH / 2}
        fill="#7e8aaa"
        fontSize="9.5"
        textAnchor="middle"
        fontWeight="600"
        transform={`rotate(-90 10 ${PAD_T + plotH / 2})`}
      >
        Position Size ($) →
      </text>
    </svg>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Capital utilization circular gauge
//
// W13-9 — Now backed by the shared Recharts GaugeChart component from
// @/components/charts. This wrapper preserves the panel's local API
// (pct + deployed + capital) and the "Near Cap / Moderate / Healthy"
// status badge so call sites don't need to change.
// ─────────────────────────────────────────────────────────────────────────────
function UtilizationGauge({
  pct,
  deployed,
  capital,
}: {
  pct: number
  deployed: number
  capital: number
}) {
  const clampedPct = Math.max(0, Math.min(100, pct))
  const style = utilizationStyle(clampedPct)

  return (
    <div className="flex flex-col items-center justify-center">
      <GaugeChart
        value={clampedPct}
        label="DEPLOYED"
        sublabel={`${fmtUsd(deployed)} / ${fmtUsd(capital)}`}
        color={style.stroke}
        height={180}
      />
      <span className={`badge ${style.badge} text-[9.5px] mt-1.5`}>
        {clampedPct > 80 ? 'Near Cap' : clampedPct >= 50 ? 'Moderate' : 'Healthy'}
      </span>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Skeleton loading state
// ─────────────────────────────────────────────────────────────────────────────
function SkeletonState() {
  return (
    <div className="p-3 space-y-3">
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
        <div className="skeleton-card p-3 lg:col-span-2">
          <div className="skeleton-line-lg w-1/3" />
          <div className="skeleton-line" style={{ height: '120px' }} />
        </div>
        <div className="skeleton-card p-3">
          <div className="skeleton-line-lg w-1/2" />
          <div className="skeleton-line" style={{ height: '120px' }} />
        </div>
      </div>
      <div className="skeleton-card p-3">
        <div className="skeleton-line-lg w-1/4" />
        <div className="skeleton-line" />
        <div className="skeleton-line" />
        <div className="skeleton-line" />
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────────
// Main component
// ─────────────────────────────────────────────────────────────────────────────
export default function CapitalAllocatorPanel() {
  const [breakdown, setBreakdown] = useState<AllocatorBreakdown | null>(null)
  const [exposure, setExposure] = useState<ExposureReport | null>(null)
  const [allocations, setAllocations] = useState<ClosedPosition[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [isRefreshing, setIsRefreshing] = useState(false)

  // ── Config editor state ──
  const [draftConfig, setDraftConfig] = useState<AllocatorConfig>(DEFAULT_CONFIG)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [submitMsg, setSubmitMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const configFormRef = useRef<HTMLFormElement>(null)

  // ── Live allocator config — derived from breakdown response if available,
  //    else fall back to DEFAULT_CONFIG. ──
  const liveConfig: AllocatorConfig = useMemo(() => {
    if (!breakdown) return DEFAULT_CONFIG
    return {
      k: breakdown.edge_k_m ?? DEFAULT_CONFIG.k,
      alpha: breakdown.edge_v_max ?? DEFAULT_CONFIG.alpha,
      max_edge: DEFAULT_CONFIG.max_edge,
      max_position_size: breakdown.cap_usd ?? DEFAULT_CONFIG.max_position_size,
      max_exposure: DEFAULT_CONFIG.max_exposure,
      operating_capital: DEFAULT_CONFIG.operating_capital,
    }
  }, [breakdown])

  // ── Fetch all data ──
  const fetchAll = useCallback(async (quiet = false) => {
    if (!quiet) {
      setLoading(true)
      setError(null)
    } else {
      setIsRefreshing(true)
    }
    try {
      // Parallel fetch — closed positions (for the recent allocations table
      // AND for the latest edge/confidence to drive the what-if allocation call).
      const closedRes = await apiFetch(`/api/positions/closed?limit=${RECENT_ALLOCATIONS_LIMIT}`)
      let latestEdge = 0.05
      let latestConf = 0.7
      let closedPositions: ClosedPosition[] = []
      if (closedRes.ok) {
        const j = await closedRes.json()
        closedPositions = (j?.positions ?? []) as ClosedPosition[]
        if (closedPositions.length > 0) {
          const latest = closedPositions[0]
          if (typeof latest.predicted_edge === 'number' && latest.predicted_edge > 0) {
            latestEdge = latest.predicted_edge
          }
          if (typeof latest.confidence === 'number' && latest.confidence > 0) {
            latestConf = latest.confidence
          }
        }
      }

      // What-if allocation call — uses latest signal edge/conf to plot the
      // current operating point on the curve.
      const allocUrl =
        `/api/capital/allocation` +
        `?strategy=signal_trader` +
        `&edge=${latestEdge.toFixed(4)}` +
        `&confidence=${latestConf.toFixed(4)}` +
        `&liquidity=100` +
        `&existing_exposure=0` +
        `&drawdown=0`
      const [allocRes, expoRes] = await Promise.all([
        apiFetch(allocUrl),
        apiFetch('/api/exposure'),
      ])

      if (!allocRes.ok) {
        throw new Error(`Allocator endpoint returned ${allocRes.status}`)
      }
      const allocJson = (await allocRes.json()) as AllocatorBreakdown
      setBreakdown(allocJson)

      if (expoRes.ok) {
        setExposure((await expoRes.json()) as ExposureReport)
      }

      setAllocations(closedPositions)
      setError(null)
      setLastUpdated(Date.now())
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error'
      setError(msg)
    } finally {
      setLoading(false)
      setIsRefreshing(false)
    }
  }, [])

  // ── Initial fetch + 15s polling, paused when document hidden ──
  useEffect(() => {
    fetchAll()
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      timer = setInterval(() => fetchAll(true), POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const onVis = () => {
      if (document.hidden) {
        stop()
      } else {
        // Refresh immediately on tab refocus, then resume polling.
        fetchAll(true)
        start()
      }
    }
    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVis)
    return () => {
      stop()
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [fetchAll])

  // ── Derive utilization & per-strategy split ──
  const deployed = exposure?.capital_invested ?? 0
  const operatingCapital = liveConfig.operating_capital
  const utilizationPct = operatingCapital > 0 ? (deployed / operatingCapital) * 100 : 0

  const strategySplit = useMemo(() => {
    if (!exposure?.exposure_per_strategy) return []
    const entries = Object.entries(exposure.exposure_per_strategy)
    const total = entries.reduce((a, [, v]) => a + (v || 0), 0)
    return entries
      .map(([name, value]) => ({
        name,
        value: value || 0,
        pct: total > 0 ? ((value || 0) / total) * 100 : 0,
      }))
      .sort((a, b) => b.value - a.value)
      .slice(0, 8)
  }, [exposure])

  // ── Latest signal edge (operating point on the curve) ──
  const latestSignal = allocations[0] ?? null
  const operatingEdge = latestSignal?.predicted_edge ?? null
  const operatingConf = latestSignal?.confidence ?? null
  // The actual size produced by the allocator at the operating point
  // (from the what-if breakdown call).
  const operatingSize = breakdown?.size_usd ?? null

  // ── Config editor ──
  const openEditor = () => {
    setDraftConfig(liveConfig)
    setSubmitMsg(null)
    setConfirmOpen(true)
  }

  const handleConfirmSubmit = async () => {
    setSubmitting(true)
    setSubmitMsg(null)
    try {
      // Attempt POST to the allocator update endpoint. The current backend
      // (`capital_allocator.py:register_routes`) only registers a GET route;
      // this POST is the intended future endpoint. We handle 404/405
      // gracefully with a clear "endpoint not available" message.
      const res = await apiFetch('/api/capital/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          edge_k_m: draftConfig.k,
          edge_v_max: draftConfig.alpha,
          max_edge: draftConfig.max_edge,
          max_position_size: draftConfig.max_position_size,
          max_exposure: draftConfig.max_exposure,
          operating_capital: draftConfig.operating_capital,
        }),
      })
      if (res.ok) {
        setSubmitMsg({ kind: 'ok', text: 'Allocator config updated successfully.' })
        setConfirmOpen(false)
        // Re-fetch to pick up new values.
        fetchAll(true)
      } else if (res.status === 404 || res.status === 405) {
        setSubmitMsg({
          kind: 'err',
          text:
            'Backend endpoint POST /api/capital/config is not registered. ' +
            'Allocator constants are read-only module-level values (see core/capital_allocator.py).',
        })
      } else {
        let detail = `HTTP ${res.status}`
        try {
          const j = await res.json()
          if (j?.detail) detail = typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail)
        } catch {
          /* ignore */
        }
        setSubmitMsg({ kind: 'err', text: `Update failed: ${detail}` })
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Network error'
      setSubmitMsg({ kind: 'err', text: `Update failed: ${msg}` })
    } finally {
      setSubmitting(false)
    }
  }

  const fmtPct100 = (v: number | null | undefined, digits = 1) =>
    v == null || !Number.isFinite(v) ? '—' : `${(v * 100).toFixed(digits)}%`

  // ── Render ──
  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* ── Header ── */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap justify-between items-center gap-2">
        <div className="flex items-center gap-2">
          <Coins className="w-3.5 h-3.5 text-cyan-400" aria-hidden="true" />
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            Capital Allocator
          </span>
          <span className="badge badge-cyan text-[9.5px]">Michaelis-Menten</span>
          {breakdown && (
            <span className="badge badge-dim text-[9px]">
              v_max=${breakdown.edge_v_max.toFixed(2)} · k_m={(breakdown.edge_k_m * 100).toFixed(1)}%
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {lastUpdated && (
            <span className="text-[9.5px] text-[#5a637a] mono">
              updated {fmtAge(lastUpdated / 1000)}
            </span>
          )}
          <button
            onClick={() => fetchAll(true)}
            disabled={isRefreshing || loading}
            className="btn btn-ghost btn-sm text-[10px] flex items-center gap-1 border border-[#1f2335]"
            aria-label="Refresh allocator data"
          >
            <RefreshCw className={`w-3 h-3 ${isRefreshing ? 'animate-spin' : ''}`} aria-hidden="true" />
            Refresh
          </button>
          <button
            onClick={openEditor}
            className="btn btn-ghost btn-sm text-[10px] flex items-center gap-1 border border-[#1f2335]"
            aria-label="Edit allocator config"
          >
            <Settings className="w-3 h-3" aria-hidden="true" />
            Config
          </button>
        </div>
      </div>

      {/* ── Body ── */}
      {loading ? (
        <SkeletonState />
      ) : error ? (
        <div className="p-4">
          <div className="banner-danger flex items-start gap-2 text-xs" role="alert">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" aria-hidden="true" />
            <div>
              <div className="font-semibold text-[#f87171]">Allocator API unavailable</div>
              <div className="text-[#c8cfe0] mt-1 mono text-[10.5px]">{error}</div>
              <button
                onClick={() => fetchAll()}
                className="btn btn-ghost btn-sm mt-2 text-[10px] border border-[#1f2335]"
              >
                <RefreshCw className="w-3 h-3" aria-hidden="true" /> Retry
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="p-3 space-y-3">
          {/* ── Top row: Curve + Config KPIs + Gauge ── */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {/* Edge → Size curve */}
            <div className="lg:col-span-2 bg-[#0e1015] rounded p-3 border border-[#1f2335]">
              <div className="flex justify-between items-center mb-2">
                <div className="flex items-center gap-1.5">
                  <TrendingUp className="w-3 h-3 text-cyan-400" aria-hidden="true" />
                  <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                    Edge → Size Saturating Curve
                  </span>
                </div>
                <div className="flex items-center gap-2 text-[9px] mono">
                  <span className="text-cyan-400">● curve</span>
                  {operatingEdge != null && (
                    <span className="text-amber-400">● live op-point</span>
                  )}
                </div>
              </div>
              <div className="h-44">
                <EdgeSizeCurve
                  k={liveConfig.k}
                  vMax={liveConfig.alpha}
                  maxEdge={liveConfig.max_edge}
                  operatingEdge={operatingEdge}
                  operatingSize={operatingSize}
                />
              </div>
              {operatingEdge != null && operatingSize != null && (
                <div className="mt-1.5 flex items-center justify-between text-[9.5px] mono text-[#7e8aaa]">
                  <span>
                    op-point: edge={(operatingEdge * 100).toFixed(2)}%
                    {operatingConf != null && ` · conf=${(operatingConf * 100).toFixed(0)}%`}
                  </span>
                  <span className="text-amber-400 font-semibold">
                    size=${operatingSize.toFixed(4)}
                  </span>
                </div>
              )}
            </div>

            {/* Right column: Gauge + KPIs */}
            <div className="flex flex-col gap-3">
              {/* Utilization gauge */}
              <div className="bg-[#0e1015] rounded p-3 border border-[#1f2335]">
                <div className="flex items-center gap-1.5 mb-2">
                  <GaugeIcon className="w-3 h-3 text-cyan-400" aria-hidden="true" />
                  <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                    Capital Utilization
                  </span>
                </div>
                <UtilizationGauge
                  pct={utilizationPct}
                  deployed={deployed}
                  capital={operatingCapital}
                />
              </div>
            </div>
          </div>

          {/* ── Config KPI strip ── */}
          <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-1.5">
            {[
              {
                label: 'k (half-sat)',
                value: fmtPct100(liveConfig.k, 1),
                icon: Crosshair,
                color: 'text-cyan-400',
              },
              {
                label: 'α (v_max)',
                value: fmtUsd(liveConfig.alpha, 2),
                icon: TrendingUp,
                color: 'text-cyan-400',
              },
              {
                label: 'Max Edge',
                value: fmtPct100(liveConfig.max_edge, 0),
                icon: Activity,
                color: 'text-cyan-400',
              },
              {
                label: 'Max Position',
                value: fmtUsd(liveConfig.max_position_size, 2),
                icon: Banknote,
                color: 'text-emerald-400',
              },
              {
                label: 'Max Exposure',
                value: fmtUsd(liveConfig.max_exposure, 2),
                icon: Wallet,
                color: 'text-amber-400',
              },
              {
                label: 'Operating Cap',
                value: fmtUsd(liveConfig.operating_capital, 0),
                icon: Coins,
                color: 'text-cyan-400',
              },
            ].map((kpi) => {
              const Icon = kpi.icon
              return (
                <div
                  key={kpi.label}
                  className="bg-[#0e1015] rounded p-2 border border-[#1f2335]"
                >
                  <div className="flex items-center gap-1 mb-0.5">
                    <Icon className={`w-2.5 h-2.5 ${kpi.color}`} aria-hidden="true" />
                    <span className="text-[8.5px] text-[#5a637a] uppercase tracking-wider truncate">
                      {kpi.label}
                    </span>
                  </div>
                  <div className={`mono text-[11px] font-bold ${kpi.color}`}>{kpi.value}</div>
                </div>
              )
            })}
          </div>

          {/* ── Per-strategy allocation + Breakdown multipliers ── */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            {/* Per-strategy allocation bar chart */}
            <div className="lg:col-span-2 bg-[#0e1015] rounded p-3 border border-[#1f2335]">
              <div className="flex justify-between items-center mb-2">
                <div className="flex items-center gap-1.5">
                  <Layers className="w-3 h-3 text-cyan-400" aria-hidden="true" />
                  <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                    Capital Split by Strategy
                  </span>
                </div>
                <span className="text-[9px] text-[#5a637a] mono">
                  {strategySplit.length} active
                </span>
              </div>
              {strategySplit.length === 0 ? (
                <div className="text-[10.5px] text-[#5a637a] text-center py-6 mono">
                  No open exposure — capital is idle.
                </div>
              ) : (
                <div className="space-y-1.5 max-h-44 overflow-y-auto pr-1">
                  {strategySplit.map((s) => {
                    const utilStyle = utilizationStyle(
                      (s.value / Math.max(operatingCapital, 1)) * 100,
                    )
                    return (
                      <div key={s.name} className="flex items-center gap-2">
                        <span
                          className="text-[10px] text-[#dde1ed] w-32 truncate shrink-0 mono"
                          title={s.name}
                        >
                          {s.name || '<unknown>'}
                        </span>
                        <div className="flex-1 h-3 bg-[#080910] rounded-sm overflow-hidden border border-[#181c28]">
                          <div
                            className="h-full rounded-sm transition-all duration-500"
                            style={{
                              width: `${Math.max(2, s.pct)}%`,
                              background: utilStyle.stroke,
                              boxShadow: `0 0 8px ${utilStyle.ring}`,
                            }}
                          />
                        </div>
                        <span className="mono text-[10px] text-cyan-300 font-semibold w-12 text-right shrink-0">
                          {fmtUsd(s.value, 2)}
                        </span>
                        <span className="mono text-[9px] text-[#5a637a] w-10 text-right shrink-0">
                          {s.pct.toFixed(0)}%
                        </span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            {/* Latest what-if multiplier breakdown */}
            {breakdown && (
              <div className="bg-[#0e1015] rounded p-3 border border-[#1f2335]">
                <div className="flex items-center gap-1.5 mb-2">
                  <Zap className="w-3 h-3 text-amber-400" aria-hidden="true" />
                  <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                    Latest What-If Multipliers
                  </span>
                </div>
                <div className="space-y-1">
                  {[
                    { name: 'raw_size', val: breakdown.components.raw_size, suffix: '$' },
                    { name: 'confidence', val: breakdown.components.confidence_mult, suffix: '×' },
                    { name: 'calibration', val: breakdown.components.calibration_mult, suffix: '×' },
                    { name: 'drawdown', val: breakdown.components.drawdown_mult, suffix: '×' },
                    { name: 'correlation', val: breakdown.components.correlation_mult, suffix: '×' },
                    { name: 'performance', val: breakdown.components.performance_mult, suffix: '×' },
                    { name: 'liquidity', val: breakdown.components.liquidity_mult, suffix: '×' },
                  ].map((m) => (
                    <div key={m.name} className="flex justify-between text-[10px]">
                      <span className="text-[#7e8aaa]">{m.name}</span>
                      <span
                        className={`mono font-semibold ${
                          m.val >= 0.9
                            ? 'text-emerald-400'
                            : m.val >= 0.5
                            ? 'text-amber-400'
                            : 'text-red-400'
                        }`}
                      >
                        {m.suffix === '$'
                          ? fmtUsd(m.val, 4)
                          : m.val.toFixed(3) + m.suffix}
                      </span>
                    </div>
                  ))}
                  <div className="border-t border-[#1f2335] mt-1 pt-1 flex justify-between text-[10px]">
                    <span className="text-[#7e8aaa] font-semibold">product</span>
                    <span className="mono font-bold text-cyan-300">
                      {breakdown.components.product_mult.toFixed(4)}×
                    </span>
                  </div>
                  <div className="flex justify-between text-[11px] mt-1">
                    <span className="text-[#7e8aaa] font-semibold">→ size</span>
                    <span className="mono font-bold text-amber-400">
                      {fmtUsd(breakdown.size_usd, 4)}
                    </span>
                  </div>
                  {breakdown.model_brier != null && (
                    <div className="flex justify-between text-[9.5px] text-[#5a637a] mono mt-1">
                      <span>model_brier</span>
                      <span>{breakdown.model_brier.toFixed(4)}</span>
                    </div>
                  )}
                </div>
              </div>
            )}
          </div>

          {/* ── Recent allocations table ── */}
          <div className="bg-[#0e1015] rounded border border-[#1f2335] overflow-hidden">
            <div className="card-header p-2.5 border-b border-[#1f2335] flex justify-between items-center">
              <div className="flex items-center gap-1.5">
                <History className="w-3 h-3 text-cyan-400" aria-hidden="true" />
                <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                  Recent Allocations (last {RECENT_ALLOCATIONS_LIMIT})
                </span>
              </div>
              <span className="badge badge-dim text-[9px]">
                {allocations.length} closed
              </span>
            </div>
            <div className="max-h-80 overflow-y-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Token</th>
                    <th>Strategy</th>
                    <th>Edge</th>
                    <th>Conf</th>
                    <th>Size</th>
                    <th>% Cap</th>
                    <th>P&amp;L</th>
                    <th>Time</th>
                  </tr>
                </thead>
                <tbody>
                  {allocations.length === 0 ? (
                    <tr>
                      <td
                        colSpan={8}
                        className="text-center text-[#5a637a] mono py-6"
                        style={{ fontFamily: 'Inter, sans-serif' }}
                      >
                        No closed allocations recorded yet.
                      </td>
                    </tr>
                  ) : (
                    allocations.map((p) => {
                      const slug =
                        (p.data?.slug as string | undefined) ??
                        (typeof p.data?.market_slug === 'string' ? p.data.market_slug : '') ??
                        p.token_id?.slice(0, 14) ??
                        '—'
                      const sizeUsd =
                        p.entry_price != null && p.shares != null
                          ? p.entry_price * p.shares
                          : null
                      const pctCap =
                        sizeUsd != null
                          ? (sizeUsd / liveConfig.max_position_size) * 100
                          : null
                      const edge = p.predicted_edge
                      const conf = p.confidence
                      const pnl = p.pnl
                      return (
                        <tr key={`${p.id}-${p.position_id ?? p.token_id}`}>
                          <td className="label-col" title={p.token_id}>
                            <div className="max-w-[180px] truncate" style={{ fontFamily: 'Inter, sans-serif' }}>
                              {slug || '—'}
                            </div>
                          </td>
                          <td>
                            <span className="badge badge-dim text-[9px]">
                              {(p.strategy || '—').slice(0, 16)}
                            </span>
                          </td>
                          <td>
                            {edge == null ? (
                              <span className="text-[#3e4560]">—</span>
                            ) : (
                              <span
                                className={
                                  edge > 0 ? 'text-emerald-400' : 'text-red-400'
                                }
                              >
                                {(edge * 100).toFixed(2)}%
                              </span>
                            )}
                          </td>
                          <td>
                            {conf == null ? (
                              <span className="text-[#3e4560]">—</span>
                            ) : (
                              <span
                                className={
                                  conf >= 0.7
                                    ? 'text-emerald-400'
                                    : conf >= 0.5
                                    ? 'text-amber-400'
                                    : 'text-red-400'
                                }
                              >
                                {(conf * 100).toFixed(0)}%
                              </span>
                            )}
                          </td>
                          <td className="text-cyan-300">
                            {sizeUsd == null ? '—' : fmtUsd(sizeUsd, 4)}
                          </td>
                          <td>
                            {pctCap == null ? (
                              <span className="text-[#3e4560]">—</span>
                            ) : (
                              <div className="flex items-center gap-1.5">
                                <div className="w-10 h-1 bg-[#1f2335] rounded-full overflow-hidden">
                                  <div
                                    className="h-full bg-cyan-500 rounded-full"
                                    style={{
                                      width: `${Math.min(100, pctCap)}%`,
                                    }}
                                  />
                                </div>
                                <span className="text-[9.5px] text-[#7e8aaa]">
                                  {pctCap.toFixed(0)}%
                                </span>
                              </div>
                            )}
                          </td>
                          <td>
                            {pnl == null || pnl === 0 ? (
                              <span className="text-[#3e4560]">—</span>
                            ) : (
                              <span className={pnl >= 0 ? 'pnl-positive' : 'pnl-negative'}>
                                {pnl >= 0 ? '+' : '−'}${Math.abs(pnl).toFixed(4)}
                              </span>
                            )}
                          </td>
                          <td className="text-[#7e8aaa]">
                            {fmtAge(p.timestamp)}
                          </td>
                        </tr>
                      )
                    })
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}

      {/* ── Config editor confirmation dialog (shadcn/ui AlertDialog) ── */}
      <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <AlertDialogContent className="bg-[#13161e] border border-[#2d3450] max-w-lg">
          <AlertDialogHeader>
            <AlertDialogTitle className="text-[#dde1ed] flex items-center gap-2">
              <Settings className="w-4 h-4 text-cyan-400" aria-hidden="true" />
              Allocator Configuration
            </AlertDialogTitle>
            <AlertDialogDescription className="text-[#7e8aaa] text-xs">
              Adjust the saturating edge curve parameters. Changes will be POSTed
              to <code className="mono text-cyan-400">/api/capital/config</code>.
              The current backend (<code className="mono">capital_allocator.py</code>)
              treats these as read-only module constants — the POST endpoint may
              not be registered.
            </AlertDialogDescription>
          </AlertDialogHeader>

          <form
            ref={configFormRef}
            className="grid grid-cols-2 gap-3 py-1"
            onSubmit={(e) => {
              e.preventDefault()
              handleConfirmSubmit()
            }}
          >
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-k">
                k (half-saturation edge)
              </label>
              <input
                id="cfg-k"
                type="number"
                step="0.005"
                min="0.005"
                max="0.5"
                className="input input-sm"
                value={draftConfig.k}
                onChange={(e) =>
                  setDraftConfig({ ...draftConfig, k: parseFloat(e.target.value) || 0 })
                }
              />
              <span className="form-hint">decimal (0.05 = 5%)</span>
            </div>
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-alpha">
                α (asymptotic max size, $)
              </label>
              <input
                id="cfg-alpha"
                type="number"
                step="0.25"
                min="0.5"
                max="20"
                className="input input-sm"
                value={draftConfig.alpha}
                onChange={(e) =>
                  setDraftConfig({ ...draftConfig, alpha: parseFloat(e.target.value) || 0 })
                }
              />
              <span className="form-hint">V_MAX in $</span>
            </div>
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-max-edge">
                Max edge (display range)
              </label>
              <input
                id="cfg-max-edge"
                type="number"
                step="0.01"
                min="0.05"
                max="1"
                className="input input-sm"
                value={draftConfig.max_edge}
                onChange={(e) =>
                  setDraftConfig({ ...draftConfig, max_edge: parseFloat(e.target.value) || 0 })
                }
              />
              <span className="form-hint">decimal (0.20 = 20%)</span>
            </div>
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-max-pos">
                Max position size ($)
              </label>
              <input
                id="cfg-max-pos"
                type="number"
                step="0.25"
                min="0.5"
                max="20"
                className="input input-sm"
                value={draftConfig.max_position_size}
                onChange={(e) =>
                  setDraftConfig({
                    ...draftConfig,
                    max_position_size: parseFloat(e.target.value) || 0,
                  })
                }
              />
              <span className="form-hint">per-market cap</span>
            </div>
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-max-exp">
                Max exposure ($)
              </label>
              <input
                id="cfg-max-exp"
                type="number"
                step="0.5"
                min="1"
                max="50"
                className="input input-sm"
                value={draftConfig.max_exposure}
                onChange={(e) =>
                  setDraftConfig({
                    ...draftConfig,
                    max_exposure: parseFloat(e.target.value) || 0,
                  })
                }
              />
              <span className="form-hint">per-market concentration</span>
            </div>
            <div className="form-group mb-0">
              <label className="form-label" htmlFor="cfg-cap">
                Operating capital ($)
              </label>
              <input
                id="cfg-cap"
                type="number"
                step="10"
                min="10"
                max="10000"
                className="input input-sm"
                value={draftConfig.operating_capital}
                onChange={(e) =>
                  setDraftConfig({
                    ...draftConfig,
                    operating_capital: parseFloat(e.target.value) || 0,
                  })
                }
              />
              <span className="form-hint">bankroll ceiling</span>
            </div>

            {/* Status / message */}
            {submitMsg && (
              <div
                className={`col-span-2 banner-${
                  submitMsg.kind === 'ok' ? 'info' : 'danger'
                } text-[10.5px] flex items-start gap-2`}
                role={submitMsg.kind === 'ok' ? 'status' : 'alert'}
              >
                {submitMsg.kind === 'ok' ? (
                  <Zap className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden="true" />
                ) : (
                  <AlertTriangle className="w-3.5 h-3.5 mt-0.5 shrink-0" aria-hidden="true" />
                )}
                <span className="mono">{submitMsg.text}</span>
              </div>
            )}
          </form>

          <AlertDialogFooter>
            <AlertDialogCancel className="btn btn-ghost">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                handleConfirmSubmit()
              }}
              disabled={submitting}
              className="btn btn-primary"
            >
              {submitting ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  Submitting…
                </>
              ) : (
                'Save Config'
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
