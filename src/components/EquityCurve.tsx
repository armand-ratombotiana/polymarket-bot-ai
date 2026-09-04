// components/EquityCurve.tsx — Real-Time Equity Curve Chart
//
// W22-5 — Migrated from the self-managed 3-second REST polling loop (with
// W22-1's inline error banner) to the hybrid `useRealtimeData` hook. The
// panel now:
//   1. REST-prefetches /api/history/equity on mount.
//   2. Subscribes to the `metrics` WS channel for live push updates.
//      The `metrics` channel pushes the full BotSnapshot, whose shape
//      doesn't match the EquityResponse `{ points: EquityPoint[] }`
//      the panel renders. To avoid clobbering the typed state with
//      mismatched data, the hook is given a `validate` predicate that
//      drops any payload missing the `points` array. When the backend
//      eventually pushes equity-shaped objects over the metrics
//      channel, the validator will accept them.
//   3. Falls back to polling /api/history/equity every 5s when the WS
//      isn't connected.
//   4. Renders a "● Live" / "⟳ Polling" badge so the trader can tell at
//      a glance whether the equity timeline is real-time or lagged.
//
// W22-1 backwards-compat: the previous inline error banner (with HTTP
// status + Dismiss button) is preserved via a small inline state that
// mirrors the useRealtimeData `error` field. The banner surfaces the
// last fetch failure and can be dismissed; on a fresh error, the
// banner re-appears.
'use client'

import { useState, useEffect } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { fmtUsd, fmtPnl, fmtPct, colors } from '@/lib/design-tokens'
import { EquityCurveChart } from '@/components/charts'
import { Badge } from '@/components/ui/badge'

interface EquityPoint {
  timestamp: number
  equity: number
  pnl: number
}

interface EquityResponse {
  points?: EquityPoint[]
}

// W22-5 — type guard for the metrics WS channel. The channel pushes
// the full BotSnapshot by default; only payloads that look like an
// EquityResponse (have the `points` array) are accepted. When the
// payload doesn't match, the data state is left untouched and the REST
// polling continues to drive the displayed equity timeline.
function isEquityPayload(d: unknown): boolean {
  if (!d || typeof d !== 'object') return false
  const obj = d as Record<string, unknown>
  return Array.isArray(obj.points)
}

export default function EquityCurve() {
  // W22-5 — hybrid REST + WS subscription. Replaces the previous 3s
  // self-managed setInterval (the useRealtimeData hook handles polling,
  // visibility-aware pause, and WS fallback generically).
  const { data, isLoading, isRealtime, error } = useRealtimeData<EquityResponse>(
    '/api/history/equity',
    {
      wsChannel: 'metrics',
      pollInterval: 5000, // was 3s; relaxed to 5s with WS live updates
      validate: isEquityPayload,
    },
  )

  const points: EquityPoint[] = data?.points ?? []
  const lastUpdated = points.length > 0 ? points[points.length - 1].timestamp : null

  // W22-1 backwards-compat — surface fetch failures via a dismissable
  // inline banner. useRealtimeData exposes the latest error string
  // (or null when the last fetch succeeded); we mirror it into local
  // state so we can track dismissal independently. The banner
  // re-appears whenever a fresh error arrives.
  //
  // The error string is wrapped with the W22-1 "Failed to load equity
  // timeline" prefix so the W22-1 tests' assertion on
  // `/Failed to load equity timeline \(HTTP 500\)/` continues to match.
  const wrappedError = error ? `Failed to load equity timeline (${error})` : null
  const [dismissedError, setDismissedError] = useState<string | null>(null)
  useEffect(() => {
    if (wrappedError && wrappedError !== dismissedError) {
      setDismissedError(null)
    }
  }, [wrappedError, dismissedError])
  const showError = wrappedError && wrappedError !== dismissedError
  const dismissError = () => setDismissedError(wrappedError)
  const errorBanner = showError && (
    <div
      className="banner-danger text-[10.5px] mx-3 mt-2 py-1.5 px-2.5 flex items-center justify-between"
      role="alert"
    >
      <span className="flex items-center gap-1.5">
        <AlertTriangle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
        <span><strong>Equity:</strong> {wrappedError}</span>
      </span>
      <button
        onClick={dismissError}
        className="hover:underline text-xs flex items-center gap-0.5 shrink-0"
        aria-label="Dismiss equity error"
      >
        <X className="w-3 h-3" aria-hidden="true" /> Dismiss
      </button>
    </div>
  )

  const currentEquity = points.length > 0 ? points[points.length - 1].equity : null
  const currentPnl = points.length > 0 ? points[points.length - 1].pnl : 0.0

  if (isLoading && points.length === 0) {
    return (
      <div className="card p-3 flex flex-col justify-between min-h-[160px]">
        <div className="card-header pb-1 flex justify-between items-center">
          <div className="flex items-center gap-1.5">
            <span className="card-title">📈 Equity Curve</span>
            {/* W22-5 — Live / Polling badge. */}
            {isRealtime ? (
              <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
            ) : (
              <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
            )}
          </div>
          <span className="badge badge-dim text-[10px]">USDC · Paper</span>
        </div>
        {errorBanner}
        <div className="flex-1 flex items-center justify-center text-xs text-[#4a5068]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading equity timeline…
        </div>
      </div>
    )
  }

  if (points.length < 2) {
    return (
      <div className="card p-3 flex flex-col justify-between min-h-[160px]">
        <div className="card-header pb-1 flex justify-between items-center">
          <div className="flex items-center gap-1.5">
            <span className="card-title">📈 Equity Curve</span>
            {/* W22-5 — Live / Polling badge. */}
            {isRealtime ? (
              <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
            ) : (
              <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
            )}
          </div>
          <div className="flex items-center gap-1.5">
            <span className="badge badge-amber text-[10px]">Paper</span>
            <span className="mono text-xs text-green-400 font-semibold">
              {currentEquity !== null ? fmtUsd(currentEquity) : '—'}
            </span>
          </div>
        </div>
        {errorBanner}
        <div className="flex-1 flex flex-col items-center justify-center text-xs text-[#7e8aaa] text-center p-3">
          <span className="text-base mb-1" aria-hidden="true">⏱️</span>
          <span>Accumulating paper execution points…</span>
          <span className="text-[10px] text-[#4a5068] mt-1">Baseline: $100.00 Operating Capital</span>
        </div>
      </div>
    )
  }

  // Calculate min/max for footer display (kept for the summary line below
  // the chart; the chart itself computes its own Y-domain via Recharts).
  const baseline = 100.0
  const allValues = [...points.map((p) => p.equity), baseline]
  const minEq = Math.min(...allValues)
  const maxEq = Math.max(...allValues)

  // W14 — drawdown from peak (running peak-to-trough excursion).
  const drawdowns: number[] = []
  let peak = -Infinity
  for (const p of points) {
    peak = Math.max(peak, p.equity)
    drawdowns.push(peak > 0 ? (p.equity - peak) / peak : 0)
  }
  const maxDrawdown = drawdowns.reduce((m, d) => Math.min(m, d), 0)
  const maxDrawdownPct = Math.abs(maxDrawdown) // 0..1 magnitude for display

  const isProfit = currentPnl >= 0

  // Map to EquityCurveChart input shape — includes the precomputed drawdown
  // per timestamp so the chart's red overlay matches W14's contract.
  const chartData = points.map((p, i) => ({
    timestamp: p.timestamp,
    equity: p.equity,
    drawdown: drawdowns[i],
  }))

  return (
    <div className="card p-3 flex flex-col justify-between min-h-[160px] bg-[#13161e] border border-[#1f2335] shadow-md">
      <div className="card-header pb-1 flex justify-between items-center">
        <div className="flex items-center gap-1.5">
          <span className="card-title text-xs font-bold text-[#dde1ed]">📈 Portfolio Equity</span>
          <span className="badge badge-amber text-[9.5px]">Paper</span>
          {/* W22-5 — Live / Polling badge. */}
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span className="mono text-xs font-semibold text-[#dde1ed]">
            {currentEquity !== null ? fmtUsd(currentEquity) : '—'}
          </span>
          <span className={`badge ${isProfit ? 'badge-green' : 'badge-red'} text-[10px]`}>
            {fmtPnl(currentPnl)}
          </span>
          {/* W14 — Current max drawdown label (red tokens). */}
          <span
            className={`badge ${maxDrawdownPct > 0 ? 'badge-red' : 'badge-dim'} text-[10px]`}
            title={`Max drawdown from peak (running). Worst peak-to-trough excursion so far.`}
            style={maxDrawdownPct > 0 ? { color: colors.redFg } : undefined}
          >
            ↓DD {fmtPct(maxDrawdownPct)}
          </span>
        </div>
      </div>

      {errorBanner}

      {/* W13-9 — Recharts AreaChart via the shared EquityCurveChart.
          Replaces the hand-rolled SVG. Keeps the gradient fill, drawdown
          overlay band, baseline reference line, and hover tooltip. */}
      <div className="flex-1 flex items-center justify-center py-1 relative">
        <EquityCurveChart
          data={chartData}
          height={85}
          baseline={baseline}
          showDrawdown
          formatX={(ts) => new Date(ts).toISOString().slice(14, 19)}
          formatY={(eq) => `$${eq.toFixed(2)}`}
        />
      </div>

      <div className="flex justify-between items-center text-[10px] text-[#7e8aaa] pt-1 mono border-t border-[#1f2335]">
        <span>Base: $100.00</span>
        <span>Min: {fmtUsd(minEq)}</span>
        <span>Peak: {fmtUsd(maxEq)}</span>
        {lastUpdated && <span>{new Date(lastUpdated).toISOString().slice(14, 19)}</span>}
      </div>
    </div>
  )
}
