// components/charts/CorrelationMatrix.tsx — N×N price-correlation matrix.
//
// W16-1 — Visualises the Pearson correlation between every pair of
// held positions. Diagonal cells are always +1.00 (self-correlation).
// Off-diagonal cells are coloured by sign + magnitude:
//
//   • +1.00 → deep green (assets move in lock-step)
//   •  0.00 → neutral grey (uncorrelated)
//   • −1.00 → deep red (inverse exposure — natural hedge)
//
// The matrix is fetched from `/api/analytics/correlation` (the FastAPI
// handler in `mini-services/polymarket-bot/api/server.py` computes the
// Pearson coefficient between per-token close-price series). The
// caller MAY pass a `matrix` prop directly to bypass the fetch (used
// by the test suite).
//
// Hover over any cell reveals a tooltip with the precise coefficient
// + the token pair. The matrix is responsive: on narrow viewports the
// token labels are truncated and the cell font shrinks. The matrix
// renders as a CSS grid (`gridTemplateColumns: auto repeat(N, 1fr)`)
// so the row header column is fixed-width and the N×N body fills the
// remaining space evenly.
'use client'

import { useEffect, useMemo, useState, useCallback, memo } from 'react'
import { apiFetch } from '@/lib/api'

export interface CorrelationMatrixPayload {
  /** Token ids in row/column order. */
  tokens: string[]
  /** Human-readable labels aligned with `tokens`. */
  labels?: string[]
  /** N×N matrix of correlation coefficients in [-1, +1]. */
  matrix: number[][]
  /** Method label (always "pearson" today; surfaced for forward-compat). */
  method: 'pearson' | string
  /** Optional sample size per pair (returned when the backend can compute it). */
  sampleSize?: number
  /** Optional last-updated epoch seconds. */
  updatedAt?: number
}

export interface CorrelationMatrixProps {
  /** Optional override — bypasses the fetch when present. */
  matrix?: CorrelationMatrixPayload
  /** Override the fetch endpoint (testing / staging). */
  endpoint?: string
  /** Auto-refresh interval in ms. Default 30_000. */
  refreshIntervalMs?: number
  /** Pause auto-refresh when the document is hidden. Default true. */
  pauseWhenHidden?: boolean
  /** Cell size (px). Default 56. */
  cellSize?: number
  /** Optional className passthrough. */
  className?: string
}

// ── Colour mapping ────────────────────────────────────────────────────────
// Maps a correlation coefficient r ∈ [-1, +1] to an rgba background.
// Magnitude drives the alpha (0 → 0.10, ±1 → 0.85); sign picks the
// colour channel (red for negative, green for positive).
const POS_RGB = '74, 222, 128' // greenFg
const NEG_RGB = '248, 113, 113' // redFg
const NEUTRAL_RGB = '126, 138, 170' // textSecondary

function corrBackground(r: number): string {
  if (!Number.isFinite(r)) {
    return `rgba(${NEUTRAL_RGB}, 0.10)`
  }
  const clamped = Math.max(-1, Math.min(1, r))
  const alpha = 0.10 + Math.abs(clamped) * 0.75
  const rgb = clamped >= 0 ? POS_RGB : NEG_RGB
  return `rgba(${rgb}, ${alpha.toFixed(3)})`
}

function corrText(r: number): string {
  if (!Number.isFinite(r)) return '#7e8aaa'
  return Math.abs(r) > 0.55 ? '#0e1015' : '#dde1ed'
}

function corrBorder(r: number, isDiagonal: boolean): string {
  if (isDiagonal) return '1px solid rgba(34, 211, 238, 0.45)' // cyan diagonal accent
  if (!Number.isFinite(r)) return `1px solid rgba(${NEUTRAL_RGB}, 0.20)`
  const alpha = 0.20 + Math.abs(r) * 0.50
  const rgb = r >= 0 ? POS_RGB : NEG_RGB
  return `1px solid rgba(${rgb}, ${alpha.toFixed(3)})`
}

function formatCorr(r: number): string {
  if (!Number.isFinite(r)) return '—'
  const sign = r < 0 ? '−' : ''
  return `${sign}${Math.abs(r).toFixed(2)}`
}

interface CellProps {
  rowLabel: string
  colLabel: string
  coefficient: number
  isDiagonal: boolean
  size: number
}

const MatrixCell = memo(function MatrixCell({
  rowLabel,
  colLabel,
  coefficient,
  isDiagonal,
  size,
}: CellProps) {
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const showTooltip = hovered || focused

  return (
    <div
      data-testid="corr-cell-wrapper"
      className="relative"
    >
      <button
        type="button"
        data-testid={`corr-cell-${rowLabel}-${colLabel}`}
        data-coefficient={Number.isFinite(coefficient) ? coefficient.toFixed(3) : 'nan'}
        data-row={rowLabel}
        data-col={colLabel}
        data-sign={coefficient > 0 ? 'positive' : coefficient < 0 ? 'negative' : 'neutral'}
        onMouseEnter={() => setHovered(true)}
        onMouseLeave={() => setHovered(false)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        aria-label={`Correlation ${rowLabel} ↔ ${colLabel}: ${formatCorr(coefficient)}`}
        className="flex items-center justify-center text-[10px] mono font-semibold rounded-sm transition-transform hover:scale-105 focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400"
        style={{
          background: corrBackground(coefficient),
          border: corrBorder(coefficient, isDiagonal),
          color: corrText(coefficient),
          width: `${size}px`,
          height: `${size}px`,
        }}
      >
        {formatCorr(coefficient)}
      </button>

      {showTooltip && (
        <div
          role="tooltip"
          data-testid={`corr-tooltip-${rowLabel}-${colLabel}`}
          className="absolute z-30 bottom-full left-1/2 -translate-x-1/2 mb-1 pointer-events-none whitespace-nowrap"
          style={{
            background: '#13161e',
            border: '1px solid #1f2335',
            borderRadius: '6px',
            boxShadow: '0 4px 12px rgba(0,0,0,0.35)',
            padding: '4px 8px',
            fontSize: '10.5px',
            color: '#dde1ed',
          }}
        >
          <span className="font-semibold">{rowLabel}</span>
          <span style={{ color: '#7e8aaa' }}> ↔ </span>
          <span className="font-semibold">{colLabel}</span>
          <span style={{ color: '#7e8aaa' }}> · ρ = </span>
          <span style={{ color: corrText(coefficient), fontWeight: 700 }}>
            {formatCorr(coefficient)}
          </span>
        </div>
      )}
    </div>
  )
})

function CorrelationMatrixImpl({
  matrix: matrixProp,
  endpoint = '/api/analytics/correlation',
  refreshIntervalMs = 30_000,
  pauseWhenHidden = true,
  cellSize = 56,
  className,
}: CorrelationMatrixProps) {
  const [fetched, setFetched] = useState<CorrelationMatrixPayload | null>(null)
  const [isLoading, setIsLoading] = useState(matrixProp == null)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  const payload = matrixProp ?? fetched

  const doFetch = useCallback(async () => {
    setIsLoading(true)
    setError(null)
    try {
      const res = await apiFetch(endpoint)
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`)
      }
      const json = (await res.json()) as CorrelationMatrixPayload
      setFetched(json)
      setLastUpdated(Date.now())
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Unknown error'
      setError(msg)
    } finally {
      setIsLoading(false)
    }
  }, [endpoint])

  // Initial fetch + auto-refresh — paused when the document is hidden.
  useEffect(() => {
    if (matrixProp != null) {
      // Caller is providing the payload — skip the fetch loop entirely.
      setIsLoading(false)
      return
    }
    doFetch()
    if (!refreshIntervalMs || refreshIntervalMs <= 0) return
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      timer = setInterval(() => {
        if (pauseWhenHidden && typeof document !== 'undefined' && document.hidden) return
        doFetch()
      }, refreshIntervalMs)
    }
    const stop = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const onVisibility = () => {
      if (pauseWhenHidden && typeof document !== 'undefined' && document.hidden) {
        stop()
      } else {
        doFetch()
        start()
      }
    }
    start()
    if (pauseWhenHidden && typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    return () => {
      stop()
      if (pauseWhenHidden && typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    }
  }, [doFetch, refreshIntervalMs, pauseWhenHidden, matrixProp])

  // Memoise the labels + diagonal so we don't re-derive on every render.
  const { labels, matrix, dim } = useMemo(() => {
    if (!payload || !payload.tokens || payload.tokens.length === 0) {
      return { labels: [] as string[], matrix: [] as number[][], dim: 0 }
    }
    const n = payload.tokens.length
    const lbl = (payload.labels && payload.labels.length === n)
      ? payload.labels
      : payload.tokens.map((t) => (t.length > 10 ? `${t.slice(0, 8)}…` : t))
    return {
      labels: lbl,
      matrix: payload.matrix,
      dim: n,
    }
  }, [payload])

  if (isLoading && !payload) {
    return (
      <div
        data-testid="corr-matrix-loading"
        className="flex items-center justify-center text-xs text-[#7e8aaa]"
        style={{ minHeight: '120px' }}
      >
        <span className="spinner mr-2" aria-hidden="true" />
        Loading correlation matrix…
      </div>
    )
  }

  if (error && !payload) {
    return (
      <div
        data-testid="corr-matrix-error"
        className="flex items-center justify-center text-xs text-[#f87171]"
        style={{ minHeight: '120px' }}
        role="alert"
      >
        <span className="mr-2">⚠</span>
        Failed to load correlation: {error}
      </div>
    )
  }

  if (!payload || dim === 0) {
    return (
      <div
        data-testid="corr-matrix-empty"
        className="flex items-center justify-center text-xs text-[#7e8aaa]"
        style={{ minHeight: '120px' }}
      >
        No positions to correlate. Open at least two positions to populate the matrix.
      </div>
    )
  }

  return (
    <div
      data-testid="corr-matrix"
      className={className}
    >
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between text-[10px] text-[#7e8aaa]">
          <span>
            Pearson correlation · method = <span className="mono">{payload.method}</span>
            {typeof payload.sampleSize === 'number' && (
              <> · n = <span className="mono">{payload.sampleSize}</span></>
            )}
          </span>
          {lastUpdated && (
            <span>
              updated {new Date(lastUpdated).toLocaleTimeString()}
            </span>
          )}
        </div>

        {/* Matrix — uses CSS grid with `auto` for the row-header column. */}
        <div
          className="overflow-auto scrollbar-thin"
          style={{ maxWidth: '100%' }}
        >
          <div
            role="table"
            aria-label="Position correlation matrix"
            className="inline-grid gap-0"
            style={{
              gridTemplateColumns: `auto repeat(${dim}, ${cellSize}px)`,
            }}
          >
            {/* Top-left corner cell. */}
            <div
              role="rowheader"
              className="flex items-center justify-center text-[9px] text-[#7e8aaa]"
              style={{ width: '88px', height: `${cellSize}px` }}
            >
              <span className="rotate-0">tokens</span>
            </div>

            {/* Column headers. */}
            {labels.map((label, j) => (
              <div
                key={`col-${j}-${label}`}
                role="columnheader"
                className="flex items-end justify-center pb-1 text-[9px] text-[#7e8aaa] mono"
                style={{ height: `${cellSize}px`, writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
                title={payload.tokens[j]}
              >
                <span className="truncate" style={{ maxWidth: `${cellSize * 1.5}px` }}>{label}</span>
              </div>
            ))}

            {/* Rows. */}
            {labels.map((rowLabel, i) => (
              <RowFragment
                key={`row-${i}-${rowLabel}`}
                rowLabel={rowLabel}
                rowTokenId={payload.tokens[i]}
                colLabels={labels}
                colTokenIds={payload.tokens}
                row={matrix[i] ?? []}
                cellSize={cellSize}
              />
            ))}
          </div>
        </div>

        {/* Legend — colour bar from −1 to +1. */}
        <div
          data-testid="corr-matrix-legend"
          className="flex items-center gap-2 text-[9.5px] text-[#7e8aaa]"
        >
          <span>−1.0 (inverse)</span>
          <div
            className="h-2 flex-1 rounded"
            style={{
              background:
                `linear-gradient(to right, rgba(${NEG_RGB}, 0.85), rgba(${NEUTRAL_RGB}, 0.30) 50%, rgba(${POS_RGB}, 0.85))`,
            }}
            aria-hidden="true"
          />
          <span>+1.0 (lock-step)</span>
        </div>

        {error && (
          <div
            className="text-[10px] text-amber-400"
            role="alert"
            data-testid="corr-matrix-stale"
          >
            ⚠ Stale data — last refresh failed: {error}
          </div>
        )}
      </div>
    </div>
  )
}

interface RowFragmentProps {
  rowLabel: string
  rowTokenId: string
  colLabels: string[]
  colTokenIds: string[]
  row: number[]
  cellSize: number
}

const RowFragment = memo(function RowFragment({
  rowLabel,
  rowTokenId,
  colLabels,
  colTokenIds,
  row,
  cellSize,
}: RowFragmentProps) {
  return (
    <>
      <div
        role="rowheader"
        className="flex items-center justify-end pr-2 text-[9px] text-[#7e8aaa] mono truncate"
        style={{ width: '88px', height: `${cellSize}px` }}
        title={rowTokenId}
      >
        <span className="truncate" style={{ maxWidth: '84px' }}>{rowLabel}</span>
      </div>
      {colLabels.map((colLabel, j) => {
        const coeff = Number.isFinite(row[j]) ? row[j] : Number.NaN
        const isDiagonal = rowTokenId === colTokenIds[j]
        return (
          <MatrixCell
            key={`cell-${rowTokenId}-${colTokenIds[j]}`}
            rowLabel={rowLabel}
            colLabel={colLabel}
            coefficient={coeff}
            isDiagonal={isDiagonal}
            size={cellSize}
          />
        )
      })}
    </>
  )
})

const CorrelationMatrix = memo(CorrelationMatrixImpl)
export default CorrelationMatrix
