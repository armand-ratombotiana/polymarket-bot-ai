// components/KpiCard.tsx — W39-3 Reusable KPI card primitive
//
// Powers the redesigned Command Center's three-tier KPI hierarchy:
//
//   ┌───────────────────────────────────────────────────────┐
//   │  LABEL (uppercase, small, dimmed)        [stale][err]  │
//   │  $1,234.56   ← large bold tabular-nums value           │
//   │  sub-text (trend %, timestamp, or context)             │
//   └───────────────────────────────────────────────────────┘
//
// All visual styling lives in CSS utility classes (.kpi-card, .kpi-label,
// .kpi-value, .kpi-sub, .kpi-tone-*, .kpi-stale-pill, .kpi-skeleton)
// declared in src/app/globals.css. This component is the typed shell that
// consumes those tokens so call-sites stay declarative.
//
// Variants:
//   * size="lg" — used by the Command Center top bar (3 hero KPIs).
//                 Larger padding + var(--text-2xl) value.
//   * size="md" — used by the P&L row (5 medium KPIs).
//                 Default padding + var(--text-lg) value.
//   * size="sm" — used by the risk bar (3 compact KPIs).
//                 Default padding + var(--text-md) value.
//
// State machine (mutually exclusive — checked in this order):
//   1. loading  → render skeleton block in place of the value.
//   2. error    → render red "—" with an "err" pill next to the label.
//   3. stale    → render the value normally, but show an amber "stale"
//                 pill next to the label so the trader knows the
//                 number is older than the freshness threshold.
//   4. value    → render the formatted string with the chosen tone.
//
// Color tones are applied to the value text ONLY — labels and sub-text
// stay neutral so the headline number carries the semantic weight.
'use client'

import { memo, type ReactNode } from 'react'

export type KpiTone = 'neutral' | 'positive' | 'negative' | 'warning'
export type KpiSize = 'lg' | 'md' | 'sm'

export interface KpiCardProps {
  /** Stable identifier — surfaced as `data-testid="kpi-{id}"` for tests
   *  and as `data-kpi-id` for screen-reader / debugging. */
  id: string
  /** Uppercase label rendered above the value. */
  label: string
  /** Pre-formatted value string. When `loading` is true this is ignored. */
  value: string | null
  /** Optional small grey sub-text below the value (trend %, timestamp). */
  sub?: ReactNode
  /** Color tone applied to the value text. */
  tone?: KpiTone
  /** Size variant — controls padding + value font-size. Defaults to 'md'. */
  size?: KpiSize
  /** When true, replaces the value with a shimmering skeleton block. */
  loading?: boolean
  /** When non-null, renders a red "err" pill and replaces the value with "—". */
  error?: string | null
  /** When true, renders an amber "stale" pill next to the label. */
  stale?: boolean
  /** When true, the card lifts on hover (interactive affordance). */
  interactive?: boolean
  /** Tooltip text rendered as the card's `title` attribute. */
  title?: string
  /** Optional trailing element rendered after the value (e.g. an icon). */
  trailing?: ReactNode
}

const TONE_CLASS: Record<KpiTone, string> = {
  neutral: 'kpi-tone-neutral',
  positive: 'kpi-tone-positive',
  negative: 'kpi-tone-negative',
  warning: 'kpi-tone-warning',
}

const VALUE_SIZE_CLASS: Record<KpiSize, string> = {
  lg: 'kpi-value-lg',
  md: 'kpi-value-md',
  sm: 'kpi-value-sm',
}

const SKELETON_CLASS: Record<KpiSize, string> = {
  lg: 'kpi-skeleton kpi-skeleton-lg',
  md: 'kpi-skeleton kpi-skeleton-md',
  sm: 'kpi-skeleton kpi-skeleton-md',
}

function KpiCardImpl({
  id,
  label,
  value,
  sub,
  tone = 'neutral',
  size = 'md',
  loading = false,
  error = null,
  stale = false,
  interactive = false,
  title,
  trailing,
}: KpiCardProps) {
  const cardClasses = [
    'kpi-card',
    size === 'lg' ? 'kpi-card-lg' : '',
    interactive ? 'is-interactive' : '',
  ]
    .filter(Boolean)
    .join(' ')

  const showStale = stale && !loading && !error
  const showError = !!error && !loading

  return (
    <div
      className={cardClasses}
      title={title}
      data-testid={`kpi-${id}`}
      data-kpi-id={id}
      role="group"
      aria-label={`${label}: ${
        loading ? 'loading' : error ? 'error' : value ?? '—'
      }`}
    >
      <div className="kpi-label">
        <span className="truncate">{label}</span>
        {showStale && (
          <span
            className="kpi-stale-pill"
            title="Stale — last refresh was more than 30s ago"
            aria-label="stale"
          >
            stale
          </span>
        )}
        {showError && (
          <span
            className="kpi-error-pill"
            title={error ?? undefined}
            aria-label="error"
          >
            err
          </span>
        )}
      </div>

      {loading ? (
        <span
          className={SKELETON_CLASS[size]}
          role="status"
          aria-live="polite"
          aria-label="loading"
        />
      ) : showError ? (
        <span
          className={`kpi-value ${VALUE_SIZE_CLASS[size]} kpi-tone-negative`}
        >
          —
        </span>
      ) : (
        <span className={`kpi-value ${VALUE_SIZE_CLASS[size]} ${TONE_CLASS[tone]}`}>
          {value ?? '—'}
          {trailing}
        </span>
      )}

      {sub && !loading && !showError && <div className="kpi-sub">{sub}</div>}
    </div>
  )
}

// Wrap in `memo` so the parent's 2s snapshot re-renders don't cascade into
// KPI card re-renders when the displayed value hasn't actually changed.
// Shallow-compares the primitive props; ReactNode props (sub, trailing)
// are compared by reference — callers should keep them stable.
export const KpiCard = memo(KpiCardImpl)

export default KpiCard
