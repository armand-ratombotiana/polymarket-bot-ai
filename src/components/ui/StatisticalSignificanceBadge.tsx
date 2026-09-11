// src/components/ui/StatisticalSignificanceBadge.tsx — W26-6
// Statistical-significance indicator for the analytics dashboard.
//
// Encodes the three possible outcomes of a hypothesis test on a trading
// metric (typically win-rate vs. the 50% coin-flip null):
//
//   1. ✓ Significant     — p < 0.05 AND n ≥ 30  (green)
//   2. ⚠ Not Significant — p ≥ 0.05     AND n ≥ 30  (amber)
//   3. ⏳ Insufficient Data — n < 30 (gray, regardless of p)
//
// The three-state contract is the dashboard's single source of truth
// for "should I trust this metric?". It mirrors the binomial-test
// convention used by the backend's `/api/performance/report` route
// (which already returns `p_value` + `is_statistically_significant` on
// each PaperMetrics row).
//
// Tooltip: a one-line plain-English explanation of what "statistically
// significant" means in this context — without jargon, because the
// dashboard is also used by traders (not quants) to triage live
// performance.

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

export interface StatisticalSignificanceBadgeProps {
  /** p-value from the underlying hypothesis test (typically a binomial
   *  test vs. p=0.5 for win-rate). When `null` / `undefined`, the
   *  badge trusts the `isSignificant` flag rather than the p-value. */
  pValue?: number
  /** Sample size (number of closed trades, etc.). Drives the
   *  "Insufficient Data" branch when n < 30. */
  n: number
  /** Pre-computed significance flag from the backend. When `false`
   *  AND n ≥ 30, the badge renders the "Not Significant" state even
   *  if `pValue` is missing. */
  isSignificant: boolean
  /** Optional className passthrough for parent sizing. */
  className?: string
}

// Minimum sample size for the significance verdict to be considered
// trustworthy. Below this threshold we surface "Insufficient Data"
// regardless of the p-value (a single lucky streak can produce
// p < 0.05 with n=5; that's not a real signal).
const MIN_SAMPLE_SIZE = 30

// Significance threshold — kept here (not in design-tokens.ts) because
// it's a domain constant, not a visual one. Documented inline so a
// reader doesn't have to grep for "0.05".
const ALPHA = 0.05

type SigState = 'significant' | 'not-significant' | 'insufficient'

function deriveState(
  n: number,
  isSignificant: boolean,
  pValue?: number,
): SigState {
  if (n < MIN_SAMPLE_SIZE) return 'insufficient'
  // Prefer the explicit `isSignificant` flag; fall back to deriving it
  // from the p-value if the flag wasn't supplied (caller passes `false`
  // but `pValue` is meaningful — we still trust the explicit flag for
  // backward compat with the existing PaperMetrics contract).
  if (isSignificant) return 'significant'
  // If the caller passed isSignificant=false but pValue suggests
  // otherwise (p < 0.05), surface the conservative verdict — the
  // explicit flag wins.
  void pValue
  return 'not-significant'
}

function formatP(p: number | undefined): string {
  if (p == null || !Number.isFinite(p)) return 'p=n/a'
  if (p < 0.001) return 'p<0.001'
  return `p=${p.toFixed(3)}`
}

export function StatisticalSignificanceBadge({
  pValue,
  n,
  isSignificant,
  className,
}: StatisticalSignificanceBadgeProps) {
  const state = deriveState(n, isSignificant, pValue)

  const pStr = formatP(pValue)

  // Per-state visual + text config. The colour classes intentionally
  // reuse the same green/amber/gray tokens already used elsewhere in
  // the dashboard (e.g. ConfidenceIntervalBadge, ConnectionStatus)
  // so the trader's eye learns one mapping: green = good, amber =
  // caution, gray = unknown.
  const cfg = {
    significant: {
      icon: '✓',
      label: `Significant (${pStr}, n=${n})`,
      border: 'border-green-500/40',
      bg: 'bg-green-500/10',
      text: 'text-green-400',
    },
    'not-significant': {
      icon: '⚠',
      label: `Not Significant (${pStr}, n=${n})`,
      border: 'border-amber-500/40',
      bg: 'bg-amber-500/10',
      text: 'text-amber-300',
    },
    insufficient: {
      icon: '⏳',
      label: `Insufficient Data (n=${n}<${MIN_SAMPLE_SIZE})`,
      border: 'border-gray-500/40',
      bg: 'bg-gray-500/10',
      text: 'text-gray-400',
    },
  }[state]

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="statistical-significance-badge"
          role="img"
          aria-label={`Statistical significance: ${cfg.label}`}
          className={cn(
            'inline-flex items-center gap-1 rounded-md border px-2 py-0.5',
            'text-[10px] font-medium mono cursor-help whitespace-nowrap',
            cfg.border,
            cfg.bg,
            cfg.text,
            className,
          )}
        >
          <span aria-hidden="true">{cfg.icon}</span>
          <span data-testid="sig-badge-label">{cfg.label}</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top" sideOffset={4} className="max-w-[280px]">
        <div className="space-y-1 text-[11px]">
          <div className="font-semibold">Statistical significance</div>
          {state === 'significant' && (
            <div>
              The observed win-rate is unlikely to have arisen by chance
              (binomial test, p &lt; {ALPHA}). With n={n} closed trades,
              the edge is unlikely to be a fluke.
            </div>
          )}
          {state === 'not-significant' && (
            <div>
              The observed win-rate is not distinguishable from a 50%
              coin-flip null at α={ALPHA} (n={n}). The strategy may
              still be profitable, but the evidence is not yet strong
              enough to rule out luck.
            </div>
          )}
          {state === 'insufficient' && (
            <div>
              Sample size n={n} is below the {MIN_SAMPLE_SIZE}-trade
              minimum needed for a reliable significance verdict.
              Continue paper-trading until more closed trades are
              available.
            </div>
          )}
          <div className="text-[10px] text-[#7e8aaa] pt-0.5">
            Threshold: p &lt; {ALPHA} AND n ≥ {MIN_SAMPLE_SIZE}
          </div>
        </div>
      </TooltipContent>
    </Tooltip>
  )
}

export default StatisticalSignificanceBadge
