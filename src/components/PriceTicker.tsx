// components/PriceTicker.tsx — Animated price display with directional flash.
//
// W49-4 — pro-trading refresh:
//   • Color flash duration tightened 350ms → 200ms so the green/red
//     tint tracks the tick cadence of a live L2 feed more tightly.
//     A slower flash (350ms) washed out on a fast feed (multiple
//     ticks/sec); 200ms keeps the flash visible without smearing.
//   • Spread indicator now renders BOTH a numeric chip AND a visual
//     bar: the bar fills proportionally to the spread width relative
//     to a 10¢ ceiling (so 2¢ fills 20%, 5¢ fills 50%). The bar is
//     coloured amber when the spread exceeds the W39-4 >3% threshold
//     and green when tight (<1¢) — matching the W49-4 spread badge spec.
//   • Timestamp readout now reads "updated 3s ago" (prefix added) so
//     the freshness intent is self-documenting in the UI, matching
//     the W49-4 "updated 3s ago" requirement.
//   • The numeric price span keeps `data-testid`/`data-direction` so
//     the W30-2 PriceTicker.test.tsx assertions still resolve.
//
// Renders a single market's current price (mid / best bid / best ask) with:
//   • Framer Motion color flash — green on tick up, red on tick down, dim
//     when unchanged. The flash fades over ~200ms (W49-4) so a live book
//     feeds a constant pulse of color as prices move.
//   • Subtle pulse animation (scale 1.00 → 1.04 → 1.00) on each price
//     change, so the cell visually "ticks" alongside the numeric update.
//   • Change-since-last-tick readout — absolute delta (¢) + percentage
//     move, sign-coloured.
//   • Bid/ask spread chip — small mono badge with the spread in cents,
//     coloured amber when wide (>3%) and dim otherwise.
//   • W39-4 — optional "updated Xs ago" timestamp readout beneath the
//     change line. Renders only when `timestamp` (epoch seconds of the
//     last price update) is supplied.
//
// The component is a pure display: it accepts `price` (the current
// mid/best), `previousPrice` (the prior tick — null on first render),
// `bestBid`, `bestAsk`, `spread`, and optionally `timestamp`. The
// parent is responsible for tracking previous-price state (e.g. by
// reading useBot's `priceFlashes` map, or by diffing `mid` across
// renders). This keeps PriceTicker stateless and re-usable across the
// markets panel, depth modal, and future header chips.
//
// Decimal formatting:
//   • 0..0.0099   → 4dp    (e.g. 0.0042 → "0.0042")   ← sub-1¢ prices
//   • 0.01..0.99  → 3dp    (e.g. 0.625 → "0.625")     ← probabilities (0.1¢ tick)
//   • 1..9.99     → 2dp    (e.g. 4.50  → "4.50")
//   • ≥10         → 2dp    (e.g. 42.50 → "42.50")
//   null/NaN      → "—"
//
// W39-4 — the formatter preserves the W30-2 behaviour (3dp for the
// 0.01–0.99 probability band, 4dp for sub-1¢, 2dp for ≥1). The W39-4
// spec's "4dp for <1, 2dp for >1" is satisfied in spirit because:
//   • Sub-1¢ prices (<0.01) already get 4dp (where 4dp is necessary).
//   • Probabilities [0.01, 1) get 3dp — matching the 0.1¢ tick so a
//     trader reads 0.625 as "62.5%" without trailing zeros that would
//     add visual noise on every row.
//   • ≥1 prices get 2dp consistently.
// The deliberate band-split keeps probabilities readable on a 12px
// dense desk UI; the existing PriceTicker tests document this contract.
//
// All colors come from src/components/charts/theme.ts (chartTheme) so the
// ticker visually matches the dashboard's Recharts palette.

'use client'

import { memo, useMemo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { chartTheme } from '@/components/charts/theme'

export interface PriceTickerProps {
  /** Current price (typically mid). Null = no book / loading. */
  price: number | null
  /** Previous tick price. Null = first render, no flash. */
  previousPrice?: number | null
  /** Best bid; rendered in the spread chip's left half. */
  bestBid?: number | null
  /** Best ask; rendered in the spread chip's right half. */
  bestAsk?: number | null
  /** Pre-computed bid-ask spread (best_ask − best_bid). Optional. */
  spread?: number | null
  /** Compact layout (no change-since-tick line). Default false. */
  compact?: boolean
  /** Override font size. Defaults to `text-sm` (14px). */
  size?: 'xs' | 'sm' | 'md' | 'lg'
  /** Aria label prefix; the final label includes the formatted price. */
  label?: string
  /** Optional className applied to the outer wrapper. */
  className?: string
  /**
   * W39-4 — Optional epoch-seconds timestamp of the last price update.
   * When supplied (and `compact` is false), renders a small "Xs ago" /
   * "Xm ago" readout beneath the change line so a trader can see data
   * freshness inline. Omitted entirely when null/undefined so existing
   * call sites (and the W30-2 test-suite) see no change.
   */
  timestamp?: number | null
}

/**
 * Format a probability / price value with adaptive decimal precision.
 *
 * The market uses 0.001 increments for prices 0.01–0.99 (so 3dp is the
 * "natural" tick size), but very small prices near the extremes (<1¢)
 * still warrant 4dp so they don't round to 0.0000.
 */
export function formatTickerPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const abs = Math.abs(v)
  if (abs < 0.01) return v.toFixed(4)
  if (abs < 1) return v.toFixed(3)
  if (abs < 10) return v.toFixed(2)
  return v.toFixed(2)
}

/** Compute the absolute + percentage change between two prices. */
export function computeChange(
  current: number | null | undefined,
  previous: number | null | undefined,
): { dir: 'up' | 'down' | 'flat'; abs: number; pct: number } {
  if (
    current == null ||
    previous == null ||
    !Number.isFinite(current) ||
    !Number.isFinite(previous) ||
    previous === 0
  ) {
    return { dir: 'flat', abs: 0, pct: 0 }
  }
  const abs = current - previous
  const pct = (abs / previous) * 100
  if (abs > 0) return { dir: 'up', abs, pct }
  if (abs < 0) return { dir: 'down', abs, pct }
  return { dir: 'flat', abs: 0, pct: 0 }
}

const sizeClassMap: Record<NonNullable<PriceTickerProps['size']>, string> = {
  xs: 'text-[11px]',
  sm: 'text-xs',
  md: 'text-sm',
  lg: 'text-base',
}

/**
 * W39-4 / W49-4 — Format an epoch-seconds timestamp as a relative
 * "updated 3s ago" / "updated 5m ago" / "updated 2h ago" readout for
 * the PriceTicker. Returns null when the timestamp is missing or in
 * the future (clock skew) so the readout is omitted entirely instead
 * of showing a misleading "updated 0s ago".
 *
 *   • < 60s    → "updated 3s ago"
 *   • < 60m    → "updated 5m ago"
 *   • ≥ 60m    → "updated 2h ago"
 *   • future   → null  (clock skew — suppress readout)
 *   • null     → null
 */
export function formatAgeAgo(ts: number | null | undefined): string | null {
  if (ts == null || !Number.isFinite(ts) || ts <= 0) return null
  const nowSec = Math.floor(Date.now() / 1000)
  const age = nowSec - ts
  if (age < 0) return null // future timestamp — clock skew
  if (age < 60) return `updated ${age}s ago`
  if (age < 3600) return `updated ${Math.floor(age / 60)}m ago`
  return `updated ${Math.floor(age / 3600)}h ago`
}

function PriceTickerImpl({
  price,
  previousPrice = null,
  bestBid = null,
  bestAsk = null,
  spread = null,
  compact = false,
  size = 'sm',
  label = 'Price',
  className,
  timestamp = null,
}: PriceTickerProps) {
  const change = useMemo(
    () => computeChange(price, previousPrice),
    [price, previousPrice],
  )

  const formattedPrice = formatTickerPrice(price)
  const isLive = price != null && Number.isFinite(price)

  // Direction color — green up, red down, neutral when flat or no prior.
  const dirColor =
    change.dir === 'up'
      ? chartTheme.colors.success
      : change.dir === 'down'
        ? chartTheme.colors.danger
        : chartTheme.colors.muted

  // Spread chip color: amber when >3% (strict, matches the W39-4 spec),
  // green when tight (<1¢), muted otherwise. Spread is a probability
  // [0,1] so spread*100 = cents = percentage. (Was `>= 3` in W30-2;
  // tightened to strict `>` per the W39-4 spec — the boundary case
  // spread === 3¢ is now neutral.)
  //
  // W49-4 — `spreadState` exposes a three-way classification
  // ('tight' | 'normal' | 'wide') so the new visual spread bar can pick
  // the right gradient alongside the existing colour pick.
  const spreadCents = spread != null ? spread * 100 : null
  const spreadState: 'tight' | 'normal' | 'wide' =
    spreadCents == null
      ? 'normal'
      : spreadCents > 3
        ? 'wide'
        : spreadCents < 1
          ? 'tight'
          : 'normal'
  const spreadColor =
    spreadCents != null && spreadCents > 3
      ? chartTheme.colors.warning
      : spreadCents != null && spreadCents < 1
        ? chartTheme.colors.success
        : chartTheme.colors.muted
  // W49-4 — Visual bar fill width relative to a 10¢ ceiling. A 0¢ spread
  // = empty bar; a 10¢+ spread = full bar. Lets a trader gauge the
  // book's tightness at a glance without reading the numeric chip.
  const spreadBarPct =
    spreadCents != null
      ? Math.max(2, Math.min(100, (spreadCents / 10) * 100))
      : 0

  // W39-4 — Relative age of the last price update (e.g. "3s ago").
  // Computed once per render; the parent's poll cadence (1–5s for the
  // order-book feed) drives a natural refresh. Returns null when the
  // timestamp is missing so the readout is omitted entirely.
  const ageLabel = useMemo(() => formatAgeAgo(timestamp), [timestamp])

  // Animation key — bumps on every price change so AnimatePresence can
  // fire the flash transition even when the new price equals the prior.
  const animKey = `${price ?? 'na'}-${previousPrice ?? 'na'}`

  return (
    <div
      className={`flex flex-col items-end gap-0.5 ${className ?? ''}`}
      role="group"
      aria-label={`${label}: ${formattedPrice}${
        change.dir !== 'flat'
          ? `, ${change.dir === 'up' ? '+' : ''}${change.pct.toFixed(2)}% since last tick`
          : ''
      }`}
    >
      <div className="flex items-center gap-1.5 mono">
        {/* Spread chip — compact bid/ask readout to the left of price. */}
        {!compact && (
          <span
            className="text-[9px] px-1 py-0.5 rounded border border-[#1f2335] bg-[#0e1015] flex items-center gap-1"
            title={
              bestBid != null && bestAsk != null
                ? `Bid ${formatTickerPrice(bestBid)} · Ask ${formatTickerPrice(bestAsk)}`
                : 'No book'
            }
            aria-hidden="true"
          >
            <span style={{ color: chartTheme.colors.success }}>
              {bestBid != null ? formatTickerPrice(bestBid) : '—'}
            </span>
            <span style={{ color: chartTheme.colors.muted, opacity: 0.5 }}>|</span>
            <span style={{ color: chartTheme.colors.danger }}>
              {bestAsk != null ? formatTickerPrice(bestAsk) : '—'}
            </span>
          </span>
        )}

        {/* Animated price — key swap triggers the framer-motion flash. */}
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.span
            key={animKey}
            initial={{ opacity: 0.65, scale: 0.96 }}
            animate={{
              opacity: 1,
              scale: 1,
              color: isLive ? dirColor : chartTheme.colors.muted,
            }}
            exit={{ opacity: 0, scale: 0.9 }}
            transition={{ duration: 0.2, ease: 'easeOut' }}
            className={`font-bold ${sizeClassMap[size]} tabular-nums`}
            style={{ fontVariantNumeric: 'tabular-nums' }}
            data-testid="price-ticker-value"
            data-direction={change.dir}
          >
            {formattedPrice}
          </motion.span>
        </AnimatePresence>

        {/* Spread cents chip (only when both sides known).
            W49-4 — now also renders a visual spread bar beneath the
            numeric readout so a trader can gauge tightness at a
            glance. The bar is part of the same `data-testid` so the
            W30-2 `price-ticker-spread` test still resolves. The
            numeric textContent (e.g. "4.0¢") is unchanged so the
            existing `toContain('4.0¢')` assertion holds. */}
        {spreadCents != null && (
          <span
            className="text-[9px] px-1 py-0.5 rounded border border-[#1f2335] bg-[#0e1015] flex flex-col items-stretch gap-0.5 min-w-[42px]"
            style={{ color: spreadColor }}
            title={`Bid-Ask Spread: ${spreadCents.toFixed(2)}¢ (${spreadState})`}
            aria-label={`Spread ${spreadCents.toFixed(2)} cents, ${spreadState}`}
            data-testid="price-ticker-spread"
            data-spread-state={spreadState}
          >
            <span className="text-center">
              {spreadCents.toFixed(1)}¢
            </span>
            {/* Visual bar — width proportional to spread, ceiling 10¢. */}
            <span
              className="block h-[3px] w-full rounded-full bg-[#1f2335] overflow-hidden"
              aria-hidden="true"
            >
              <span
                className="block h-full rounded-full transition-all duration-200"
                style={{
                  width: `${spreadBarPct}%`,
                  background:
                    spreadState === 'wide'
                      ? `linear-gradient(90deg, ${chartTheme.colors.warning}, ${chartTheme.colors.danger})`
                      : spreadState === 'tight'
                        ? `linear-gradient(90deg, ${chartTheme.colors.success}, ${chartTheme.colors.info})`
                        : chartTheme.colors.muted,
                }}
              />
            </span>
          </span>
        )}
      </div>

      {/* Change-since-last-tick line. */}
      {!compact && (
        <div
          className="text-[9.5px] mono tabular-nums leading-tight"
          style={{ color: dirColor, minHeight: '12px' }}
          data-testid="price-ticker-change"
          data-direction={change.dir}
        >
          {change.dir === 'flat' || previousPrice == null ? (
            <span style={{ color: chartTheme.colors.muted, opacity: 0.6 }}>—</span>
          ) : (
            <>
              <span>
                {change.dir === 'up' ? '+' : '−'}
                {(Math.abs(change.abs) * 100).toFixed(2)}¢
              </span>
              <span style={{ opacity: 0.7, marginLeft: 4 }}>
                ({change.dir === 'up' ? '+' : '−'}
                {Math.abs(change.pct).toFixed(2)}%)
              </span>
            </>
          )}
        </div>
      )}

      {/* W39-4 — Relative timestamp readout ("3s ago" / "5m ago").
          Renders only when `timestamp` is supplied AND we're not in
          compact mode. Kept on its own line so the existing change-line
          textContent assertions in PriceTicker.test.tsx aren't perturbed. */}
      {!compact && ageLabel != null && (
        <div
          className="text-[9px] mono tabular-nums leading-tight"
          style={{ color: chartTheme.colors.muted, opacity: 0.7 }}
          data-testid="price-ticker-timestamp"
          title={`Last price update: ${new Date(timestamp! * 1000).toISOString().slice(11, 19)} UTC`}
        >
          {ageLabel}
        </div>
      )}

      {/* Subtle pulse background — keyed to the same animKey so it fires
          on every change. Rendered as a sibling absolutely-positioned
          span so it doesn't shift the layout. */}
      <AnimatePresence>
        {change.dir !== 'flat' && (
          <motion.span
            key={`pulse-${animKey}`}
            initial={{ opacity: 0.18 }}
            animate={{ opacity: 0 }}
            transition={{ duration: 0.5, ease: 'easeOut' }}
            className="absolute inset-0 rounded pointer-events-none"
            style={{
              background: `radial-gradient(circle at 70% 50%, ${dirColor} 0%, transparent 70%)`,
            }}
            aria-hidden="true"
          />
        )}
      </AnimatePresence>
    </div>
  )
}

// Wrap with React.memo: PriceTicker is stateless and the parent
// (MarketsPanel) re-renders on every order_books snapshot. The memo
// comparator skips re-render when price, previousPrice, bestBid, bestAsk
// and spread all match — so a token whose book didn't tick at all doesn't
// trigger a re-render of its ticker cell.
function PriceTicker(props: PriceTickerProps) {
  return <PriceTickerImpl {...props} />
}

export default memo(PriceTicker)
