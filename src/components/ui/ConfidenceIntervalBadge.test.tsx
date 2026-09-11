// src/components/ui/ConfidenceIntervalBadge.test.tsx — W26-6
//
// Verifies the contract documented in `ConfidenceIntervalBadge.tsx`:
//   1. Renders the point estimate + CI range label.
//   2. Renders the range bar with computed geometry.
//   3. Honors all three format options (percentage / decimal / currency).
//   4. Border colour encodes significance (green if significant, amber
//      if not).
//   5. `significant` flag overrides the p-value-derived verdict.
//   6. Tooltip surfaces full numeric details on hover.
//   7. Falls back to deriving significance from `pValue` when
//      `significant` is omitted.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  ConfidenceIntervalBadge,
  type ConfidenceIntervalBadgeProps,
} from './ConfidenceIntervalBadge'

function renderBadge(props: Partial<ConfidenceIntervalBadgeProps> = {}) {
  const defaults: ConfidenceIntervalBadgeProps = {
    value: 0.72,
    ciLower: 0.65,
    ciUpper: 0.78,
    format: 'percentage',
    significant: true,
    pValue: 0.03,
    n: 42,
  }
  return render(<ConfidenceIntervalBadge {...defaults} {...props} />)
}

describe('ConfidenceIntervalBadge', () => {
  // ── Step 1: rendering ──────────────────────────────────────────────

  it('renders the point estimate prominently', () => {
    renderBadge({ value: 0.72, format: 'percentage' })
    const pointEl = screen.getByTestId('ci-point-estimate')
    expect(pointEl).toBeInTheDocument()
    // (0.72 * 100).toFixed(1) === '72.0%'
    expect(pointEl).toHaveTextContent('72.0%')
    // Point estimate should be visually prominent (font-bold class)
    expect(pointEl.className).toContain('font-bold')
  })

  it('renders the CI range label below the point estimate', () => {
    renderBadge({ ciLower: 0.652, ciUpper: 0.781, format: 'percentage' })
    const labelEl = screen.getByTestId('ci-range-label')
    expect(labelEl).toBeInTheDocument()
    // Should render the range in the canonical "[X% – Y%]" form
    expect(labelEl).toHaveTextContent('[65.2% – 78.1%]')
  })

  it('renders the visual range bar', () => {
    renderBadge()
    const barEl = screen.getByTestId('ci-range-bar')
    expect(barEl).toBeInTheDocument()
    // The point estimate marker should also be present
    expect(screen.getByTestId('ci-point-marker')).toBeInTheDocument()
  })

  it('exposes a generated aria-label describing the estimate + CI', () => {
    renderBadge({ value: 0.72, ciLower: 0.65, ciUpper: 0.78, pValue: 0.03, n: 42 })
    const badge = screen.getByTestId('confidence-interval-badge')
    const ariaLabel = badge.getAttribute('aria-label') ?? ''
    expect(ariaLabel).toMatch(/72\.0%/)
    expect(ariaLabel).toMatch(/65\.0%/)
    expect(ariaLabel).toMatch(/78\.0%/)
    expect(ariaLabel).toMatch(/95% confidence interval/i)
    expect(ariaLabel).toMatch(/p=0\.030/)
    expect(ariaLabel).toMatch(/n=42/)
  })

  // ── Step 2: format options ─────────────────────────────────────────

  it('formats the value + CI as percentages when format="percentage"', () => {
    renderBadge({ value: 0.72, ciLower: 0.65, ciUpper: 0.78, format: 'percentage' })
    expect(screen.getByTestId('ci-point-estimate')).toHaveTextContent('72.0%')
    expect(screen.getByTestId('ci-range-label')).toHaveTextContent('[65.0% – 78.0%]')
  })

  it('formats the value + CI as a 2dp decimal when format="decimal"', () => {
    renderBadge({ value: 1.85, ciLower: 1.42, ciUpper: 2.18, format: 'decimal' })
    expect(screen.getByTestId('ci-point-estimate')).toHaveTextContent('1.85')
    expect(screen.getByTestId('ci-range-label')).toHaveTextContent('[1.42 – 2.18]')
  })

  it('formats the value + CI as USDC currency when format="currency"', () => {
    renderBadge({ value: 0.19, ciLower: 0.05, ciUpper: 0.32, format: 'currency' })
    expect(screen.getByTestId('ci-point-estimate')).toHaveTextContent('$0.19')
    expect(screen.getByTestId('ci-range-label')).toHaveTextContent('[$0.05 – $0.32]')
  })

  it('prefixes a Unicode minus sign for negative currency values', () => {
    // fmtPnl / fmtUsd convention: negative values use U+2212 (−), not
    // ASCII hyphen (-). Mirrors the design-tokens.ts helper used elsewhere.
    const MINUS = '\u2212'
    renderBadge({ value: -0.42, ciLower: -0.55, ciUpper: -0.28, format: 'currency' })
    expect(screen.getByTestId('ci-point-estimate')).toHaveTextContent(`${MINUS}$0.42`)
    expect(screen.getByTestId('ci-range-label')).toHaveTextContent(`[${MINUS}$0.55 – ${MINUS}$0.28]`)
  })

  it('defaults to "percentage" format when format is omitted', () => {
    render(<ConfidenceIntervalBadge value={0.5} ciLower={0.4} ciUpper={0.6} />)
    expect(screen.getByTestId('ci-point-estimate')).toHaveTextContent('50.0%')
  })

  // ── Step 3: significance colour ───────────────────────────────────

  it('uses a green border when significant=true', () => {
    renderBadge({ significant: true })
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-green-500/40')
    expect(badge.className).not.toContain('border-amber-500/40')
  })

  it('uses an amber border when significant=false', () => {
    renderBadge({ significant: false })
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-amber-500/40')
    expect(badge.className).not.toContain('border-green-500/40')
  })

  it('uses a green point-estimate text colour when significant', () => {
    renderBadge({ significant: true })
    const pointEl = screen.getByTestId('ci-point-estimate')
    expect(pointEl.className).toContain('text-green-400')
  })

  it('uses an amber point-estimate text colour when not significant', () => {
    renderBadge({ significant: false })
    const pointEl = screen.getByTestId('ci-point-estimate')
    expect(pointEl.className).toContain('text-amber-300')
  })

  // ── Step 4: significance fallback from pValue ──────────────────────

  it('derives significance from pValue < 0.05 when significant is omitted', () => {
    render(
      <ConfidenceIntervalBadge
        value={0.72}
        ciLower={0.65}
        ciUpper={0.78}
        pValue={0.034}
      />,
    )
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-green-500/40')
  })

  it('derives non-significance from pValue >= 0.05 when significant is omitted', () => {
    render(
      <ConfidenceIntervalBadge
        value={0.55}
        ciLower={0.42}
        ciUpper={0.66}
        pValue={0.12}
      />,
    )
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-amber-500/40')
  })

  it('treats a missing pValue as non-significant when significant is omitted', () => {
    render(
      <ConfidenceIntervalBadge
        value={0.55}
        ciLower={0.42}
        ciUpper={0.66}
      />,
    )
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-amber-500/40')
  })

  it('lets the explicit significant flag override the pValue-derived verdict', () => {
    // pValue=0.5 would derive non-significant, but explicit
    // significant=true wins.
    render(
      <ConfidenceIntervalBadge
        value={0.72}
        ciLower={0.65}
        ciUpper={0.78}
        pValue={0.5}
        significant
      />,
    )
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('border-green-500/40')
  })

  // ── Step 5: range bar geometry ────────────────────────────────────

  it('clamps the percentage range bar to the [0, 1] scale', () => {
    // CI of [0.65, 0.78] on a [0, 1] track → left=65%, width≈13%.
    renderBadge({ value: 0.72, ciLower: 0.65, ciUpper: 0.78, format: 'percentage' })
    const bar = screen.getByTestId('ci-range-bar')
    const highlight = bar.firstElementChild as HTMLElement
    expect(highlight.style.left).toBe('65%')
    // width should be at least the difference (13%) — and >= 2% floor
    const widthNum = parseFloat(highlight.style.width)
    expect(widthNum).toBeGreaterThanOrEqual(13)
    expect(widthNum).toBeLessThanOrEqual(13.5) // ~13% (no extra padding)
  })

  it('enforces a 2% minimum width for a tight CI so it stays visible', () => {
    // A 0.001-wide CI should still render a visible highlight.
    renderBadge({ value: 0.5, ciLower: 0.4995, ciUpper: 0.5005, format: 'percentage' })
    const bar = screen.getByTestId('ci-range-bar')
    const highlight = bar.firstElementChild as HTMLElement
    const widthNum = parseFloat(highlight.style.width)
    expect(widthNum).toBeGreaterThanOrEqual(2)
  })

  it('places the point-estimate marker at the value position on the track', () => {
    // value=0.72 on a [0, 1] track → marker at 72%.
    renderBadge({ value: 0.72, ciLower: 0.65, ciUpper: 0.78, format: 'percentage' })
    const marker = screen.getByTestId('ci-point-marker')
    expect(marker.style.left).toBe('72%')
  })

  it('clamps out-of-range Wilson CI bounds to the [0, 1] track', () => {
    // Wilson intervals at small n can produce negative lower bounds
    // or upper bounds > 1. The bar should clamp them so the geometry
    // doesn't break.
    renderBadge({ value: 0.5, ciLower: -0.05, ciUpper: 1.05, format: 'percentage' })
    const bar = screen.getByTestId('ci-range-bar')
    const highlight = bar.firstElementChild as HTMLElement
    // -0.05 clamped to 0 → left=0%; 1.05 clamped to 1 → width=100%.
    expect(highlight.style.left).toBe('0%')
    const widthNum = parseFloat(highlight.style.width)
    expect(widthNum).toBeCloseTo(100, 0)
  })

  // ── Step 6: tooltip ────────────────────────────────────────────────
  //
  // NOTE on duplicate tooltip content: Radix renders the tooltip
  // content TWICE in the DOM — once visibly in the portal, and once
  // as a visually-hidden `role="tooltip"` element (so screen readers
  // can announce the content without hovering). This means
  // `findByText` throws "found multiple elements" for any string
  // that appears in the tooltip. We use `findAllByText` +
  // `.length > 0` instead of `findByText` for tooltip-content
  // assertions.

  it('surfaces the full numeric detail in a tooltip on hover', async () => {
    const user = userEvent.setup()
    renderBadge({
      value: 0.72,
      ciLower: 0.65,
      ciUpper: 0.78,
      pValue: 0.034,
      n: 42,
      significant: true,
    })

    await user.hover(screen.getByTestId('confidence-interval-badge'))

    // Wait for the tooltip to mount in the portal.
    await screen.findAllByText('95% Confidence Interval')
    expect(screen.getAllByText(/Point estimate:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/CI bounds:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/p-value:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Sample size:/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Statistically significant/).length).toBeGreaterThan(0)
  })

  it('omits the p-value row from the tooltip when pValue is undefined', async () => {
    const user = userEvent.setup()
    renderBadge({
      value: 0.72,
      ciLower: 0.65,
      ciUpper: 0.78,
      significant: true,
      pValue: undefined,
      n: 42,
    })

    await user.hover(screen.getByTestId('confidence-interval-badge'))

    await screen.findAllByText('95% Confidence Interval')
    expect(screen.queryAllByText(/p-value:/)).toHaveLength(0)
  })

  it('omits the sample-size row from the tooltip when n is undefined', async () => {
    const user = userEvent.setup()
    renderBadge({
      value: 0.72,
      ciLower: 0.65,
      ciUpper: 0.78,
      significant: true,
      pValue: 0.034,
      n: undefined,
    })

    await user.hover(screen.getByTestId('confidence-interval-badge'))

    await screen.findAllByText('95% Confidence Interval')
    expect(screen.queryAllByText(/Sample size:/)).toHaveLength(0)
  })

  it('renders p-value < 0.001 as "p<0.001" in the tooltip', async () => {
    const user = userEvent.setup()
    renderBadge({
      value: 0.95,
      ciLower: 0.88,
      ciUpper: 0.99,
      pValue: 0.0001,
      significant: true,
      n: 1000,
    })

    await user.hover(screen.getByTestId('confidence-interval-badge'))
    expect((await screen.findAllByText(/p<0\.001/)).length).toBeGreaterThan(0)
  })

  it('surfaces a "Not statistically significant" tooltip when not significant', async () => {
    const user = userEvent.setup()
    renderBadge({
      value: 0.52,
      ciLower: 0.42,
      ciUpper: 0.62,
      pValue: 0.45,
      significant: false,
      n: 50,
    })

    await user.hover(screen.getByTestId('confidence-interval-badge'))
    expect((await screen.findAllByText(/Not statistically significant/)).length).toBeGreaterThan(0)
  })

  // ── Step 7: custom className passthrough ───────────────────────────

  it('merges the className prop onto the badge container', () => {
    renderBadge({ className: 'w-full col-span-2' })
    const badge = screen.getByTestId('confidence-interval-badge')
    expect(badge.className).toContain('w-full')
    expect(badge.className).toContain('col-span-2')
  })

  // ── Step 8: numerical edge cases ───────────────────────────────────

  it('handles a zero-width CI gracefully (point estimate equals both bounds)', () => {
    renderBadge({ value: 0.5, ciLower: 0.5, ciUpper: 0.5, format: 'percentage' })
    const bar = screen.getByTestId('ci-range-bar')
    const highlight = bar.firstElementChild as HTMLElement
    // Width should be at least the 2% floor
    const widthNum = parseFloat(highlight.style.width)
    expect(widthNum).toBeGreaterThanOrEqual(2)
  })

  it('handles NaN value without crashing the range bar geometry', () => {
    // A bad backend response can slip NaN through. The badge should
    // not crash — it falls back to the CI midpoint for the marker.
    renderBadge({
      value: NaN,
      ciLower: 0.4,
      ciUpper: 0.6,
      format: 'percentage',
    })
    const marker = screen.getByTestId('ci-point-marker')
    expect(marker).toBeInTheDocument()
    // Marker should be at the midpoint (0.5 → 50%) since the NaN
    // value falls back to the CI midpoint.
    expect(marker.style.left).toBe('50%')
  })
})
