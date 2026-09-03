// components/PriceTicker.tsx — Animated price display with directional flash.
//
// Renders a single market's current price (mid / best bid / best ask) with:
//   • Framer Motion color flash — green on tick up, red on tick down, dim
//     when unchanged. The flash fades over ~500ms so a live book feeds a
//     constant pulse of color as prices move.
//   • Subtle pulse animation (scale 1.00 → 1.04 → 1.00) on each price
//     change, so the cell visually "ticks" alongside the numeric update.
//   • Change-since-last-tick readout — absolute delta (¢) + percentage
//     move, sign-coloured.
//   • Bid/ask spread chip — small mono badge with the spread in cents,
//     coloured amber when wide (≥3¢) and dim otherwise.
//
// The component is a pure display: it accepts `price` (the current
// mid/best), `previousPrice` (the prior tick — null on first render),
// `bestBid`, `bestAsk`, and `spread`. The parent is responsible for
// tracking previous-price state (e.g. by reading useBot's `priceFlashes`
// map, or by diffing `mid` across renders). This keeps PriceTicker
// stateless and re-usable across the markets panel, depth modal, and
// future header chips.
//
// Decimal formatting:
//   • 0..0.0099   → 4dp    (e.g. 0.0042 → "0.0042")
//   • 0.01..0.099 → 3dp    (e.g. 0.042 → "0.042")
//   • 0.10..0.99  → 3dp    (e.g. 0.625 → "0.625")  ← probabilities, 0.1¢ ticks
//   • 1..9.99     → 2dp    (e.g. 4.50  → "4.50")
//   • ≥10         → 2dp    (e.g. 42.50 → "42.50")
//   null/NaN      → "—"
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

  // Spread chip color: amber when ≥3¢, muted otherwise.
  const spreadCents = spread != null ? spread * 100 : null
  const spreadColor =
    spreadCents != null && spreadCents >= 3
      ? chartTheme.colors.warning
      : chartTheme.colors.muted

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
            transition={{ duration: 0.35, ease: 'easeOut' }}
            className={`font-bold ${sizeClassMap[size]} tabular-nums`}
            style={{ fontVariantNumeric: 'tabular-nums' }}
            data-testid="price-ticker-value"
            data-direction={change.dir}
          >
            {formattedPrice}
          </motion.span>
        </AnimatePresence>

        {/* Spread cents chip (only when both sides known). */}
        {spreadCents != null && (
          <span
            className="text-[9px] px-1 py-0.5 rounded border border-[#1f2335] bg-[#0e1015]"
            style={{ color: spreadColor }}
            title={`Bid-Ask Spread: ${spreadCents.toFixed(2)}¢`}
            aria-label={`Spread ${spreadCents.toFixed(2)} cents`}
            data-testid="price-ticker-spread"
          >
            {spreadCents.toFixed(1)}¢
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
