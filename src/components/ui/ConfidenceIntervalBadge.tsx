// src/components/ui/ConfidenceIntervalBadge.tsx — W26-6
// Confidence-interval display widget for the analytics dashboard.
//
// Renders a point estimate with its 95% CI bounds below it, plus a
// horizontal range bar visualising where the CI sits on a [0,1] scale
// (for `percentage` format) or a local scale (for `decimal` / `currency`).
// The border colour encodes statistical significance:
//   * green   — `significant` is true (typically p < 0.05)
//   * amber   — `significant` is false (or omitted and p ≥ 0.05)
//
// A Radix tooltip surfaces the full numeric detail (point estimate, CI
// bounds, p-value if provided, sample size hint if provided) on hover.
// The tooltip is wrapped in a `<TooltipProvider>` so the component is
// self-contained — no ancestor provider required.
//
// Why a separate component (and not inline JSX inside AnalyticsPanel):
//   * The same CI visualisation is reused across multiple KPI cards
//     (win-rate, expectancy, Sharpe). Duplicating the markup × 3 would
//     drift out of sync the moment one site changed its formatting.
//   * The significance colour + tooltip text are coupled to the data
//     shape; centralising them makes the contract auditable.
//   * Tests can assert the CI label format / significance class without
//     spinning up the full AnalyticsPanel + WS + REST plumbing.

'use client'

// W28-1 — `import * as React from 'react'` removed (TS6133 — no
// `React.X` references; with `jsx: 'react-jsx'` no explicit React
// import is needed and the file uses no hooks).
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
} from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

export type CIFormat = 'percentage' | 'decimal' | 'currency'

export interface ConfidenceIntervalBadgeProps {
  /** Point estimate (e.g. 0.72 for 72% win-rate, 1.85 for Sharpe,
   *  0.19 for expectancy in USD). */
  value: number
  /** Lower bound of the 95% confidence interval, same units as `value`. */
  ciLower: number
  /** Upper bound of the 95% confidence interval, same units as `value`. */
  ciUpper: number
  /** Display format for the value + CI bounds. Defaults to `'percentage'`.
   *  - `percentage`  → 0.72 rendered as "72.0%"
   *  - `decimal`     → 1.85 rendered as "1.85"
   *  - `currency`    → 0.19 rendered as "$0.19" (USDC-style, 2dp). */
  format?: CIFormat
  /** Whether the underlying hypothesis test rejected the null
   *  (typically p < 0.05). Drives the border colour: green if true,
   *  amber if false. If omitted, the badge falls back to `pValue`
   *  when provided (p < 0.05 → significant), else defaults to
   *  non-significant (amber). */
  significant?: boolean
  /** Optional p-value from the underlying hypothesis test. Surfaced in
   *  the tooltip and, when `significant` is omitted, used to derive the
   *  significance flag (`p < 0.05`). */
  pValue?: number
  /** Optional sample size — surfaced in the tooltip ("n=42"). */
  n?: number
  /** Optional aria-label override. Defaults to a generated description
   *  like "Point estimate 72.0%, 95% CI [65.2%, 78.1%]". */
  ariaLabel?: string
  /** Optional className passthrough so the parent can size / position
   *  the badge inside a KPI grid cell. */
  className?: string
}

// ── Formatting helpers ────────────────────────────────────────────────────

function formatValue(v: number, format: CIFormat): string {
  switch (format) {
    case 'percentage':
      return `${(v * 100).toFixed(1)}%`
    case 'currency': {
      const sign = v < 0 ? '−' : ''
      return `${sign}$${Math.abs(v).toLocaleString('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      })}`
    }
    case 'decimal':
    default:
      return v.toFixed(2)
  }
}

function formatPValue(p: number | undefined): string | null {
  if (p == null || !Number.isFinite(p)) return null
  // Standard convention: render as "p=0.034" with 3dp so the
  // threshold comparison (0.05) is visually obvious. Values < 0.001
  // are rendered as "p<0.001" to avoid misleading precision.
  if (p < 0.001) return 'p<0.001'
  return `p=${p.toFixed(3)}`
}

// ── Range bar geometry ──────────────────────────────────────────────────
//
// The bar is rendered as a horizontal track of width 100% with the CI
// range highlighted. For `percentage`, the track covers the full [0,1]
// scale (so the bar visually communicates "how much of the plausible
// range does this CI occupy"). For `decimal` / `currency`, we use a
// local scale centred on the CI midpoint with a ±50% padding so the
// highlighted range is always visible (and the point estimate sits
// inside it).

function computeBarGeometry(
  value: number,
  ciLower: number,
  ciUpper: number,
  format: CIFormat,
): { leftPct: number; widthPct: number; pointPct: number } {
  // Clamp to safe numbers — if the caller passes NaN / Infinity the
  // bar would still render but with nonsensical offsets.
  const lo = Number.isFinite(ciLower) ? ciLower : 0
  const hi = Number.isFinite(ciUpper) ? ciUpper : 1
  const val = Number.isFinite(value) ? value : (lo + hi) / 2

  if (format === 'percentage') {
    // Track = [0, 1]. Clamp everything to [0, 1] so out-of-range CIs
    // (which can happen with Wilson intervals at small n) don't break
    // the bar geometry.
    const cl = Math.max(0, Math.min(1, lo))
    const ch = Math.max(0, Math.min(1, hi))
    const cv = Math.max(0, Math.min(1, val))
    return {
      leftPct: cl * 100,
      widthPct: Math.max(2, (ch - cl) * 100), // min 2% so a tight CI is still visible
      pointPct: cv * 100,
    }
  }

  // Local scale: midpoint ± 50% of the CI width.
  const mid = (lo + hi) / 2
  const half = Math.max((hi - lo) / 2, Math.abs(val - mid), 1e-9)
  const scaleMin = mid - half * 1.5
  const scaleMax = mid + half * 1.5
  const span = scaleMax - scaleMin || 1
  const cl = Math.max(scaleMin, Math.min(scaleMax, lo))
  const ch = Math.max(scaleMin, Math.min(scaleMax, hi))
  const cv = Math.max(scaleMin, Math.min(scaleMax, val))
  return {
    leftPct: ((cl - scaleMin) / span) * 100,
    widthPct: Math.max(4, ((ch - cl) / span) * 100),
    pointPct: ((cv - scaleMin) / span) * 100,
  }
}

// ── Component ──────────────────────────────────────────────────────────

export function ConfidenceIntervalBadge({
  value,
  ciLower,
  ciUpper,
  format = 'percentage',
  significant,
  pValue,
  n,
  ariaLabel,
  className,
}: ConfidenceIntervalBadgeProps) {
  // Derive significance if not explicitly provided.
  const isSignificant =
    significant ?? (pValue != null && Number.isFinite(pValue) && pValue < 0.05)

  const fmtVal = formatValue(value, format)
  const fmtLo = formatValue(ciLower, format)
  const fmtHi = formatValue(ciUpper, format)
  const ciLabel = `[${fmtLo} – ${fmtHi}]`

  const geom = computeBarGeometry(value, ciLower, ciUpper, format)

  const generatedAria =
    ariaLabel ??
    `Point estimate ${fmtVal}, 95% confidence interval ${ciLabel}` +
      (pValue != null ? `, ${formatPValue(pValue)}` : '') +
      (n != null ? `, n=${n}` : '')

  // Border colour encodes significance: green when significant,
  // amber when not. The text colour matches for visual consistency.
  const borderClass = isSignificant
    ? 'border-green-500/40'
    : 'border-amber-500/40'
  const accentClass = isSignificant ? 'bg-green-500/70' : 'bg-amber-500/70'
  const valueTextClass = isSignificant ? 'text-green-400' : 'text-amber-300'

  // Tooltip content — full numeric detail. Rendered in a small grid
  // so the trader can scan the numbers at a glance.
  const pStr = formatPValue(pValue)

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          data-testid="confidence-interval-badge"
          role="img"
          aria-label={generatedAria}
          className={cn(
            'inline-flex flex-col gap-1 rounded-md border px-2.5 py-1.5',
            'bg-[#13161e]/60 cursor-help min-w-[120px]',
            borderClass,
            className,
          )}
        >
          {/* Point estimate — prominent */}
          <span
            data-testid="ci-point-estimate"
            className={cn('text-sm font-bold mono', valueTextClass)}
          >
            {fmtVal}
          </span>

          {/* CI range — small, below */}
          <span
            data-testid="ci-range-label"
            className="text-[10px] text-[#7e8aaa] mono"
          >
            {ciLabel}
          </span>

          {/* Visual range bar */}
          <div
            data-testid="ci-range-bar"
            className="relative h-1.5 w-full rounded-full bg-[#1f2335] overflow-hidden mt-0.5"
            aria-hidden="true"
          >
            {/* CI highlighted range */}
            <div
              className={cn('absolute inset-y-0 rounded-full', accentClass)}
              style={{
                left: `${geom.leftPct}%`,
                width: `${geom.widthPct}%`,
              }}
            />
            {/* Point estimate marker */}
            <div
              data-testid="ci-point-marker"
              className="absolute inset-y-0 w-[2px] bg-white/80"
              style={{ left: `${geom.pointPct}%` }}
            />
          </div>
        </div>
      </TooltipTrigger>
      <TooltipContent side="top" sideOffset={4} className="max-w-[260px]">
        <div className="space-y-0.5 text-[11px]">
          <div className="font-semibold">95% Confidence Interval</div>
          <div>
            Point estimate:{' '}
            <span className="mono font-semibold">{fmtVal}</span>
          </div>
          <div>
            CI bounds:{' '}
            <span className="mono">
              {fmtLo} – {fmtHi}
            </span>
          </div>
          {pStr != null && (
            <div>
              p-value: <span className="mono">{pStr}</span>
            </div>
          )}
          {n != null && (
            <div>
              Sample size: <span className="mono">n={n}</span>
            </div>
          )}
          <div className="text-[10px] text-[#7e8aaa] pt-0.5">
            {isSignificant
              ? '✓ Statistically significant (p &lt; 0.05)'
              : '⚠ Not statistically significant (p ≥ 0.05)'}
          </div>
        </div>
      </TooltipContent>
    </Tooltip>
  )
}

export default ConfidenceIntervalBadge
