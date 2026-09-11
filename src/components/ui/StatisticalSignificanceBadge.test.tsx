// src/components/ui/StatisticalSignificanceBadge.test.tsx — W26-6
//
// Verifies the three-state contract documented in
// `StatisticalSignificanceBadge.tsx`:
//   1. ✓ Significant     — p < 0.05 AND n ≥ 30 (green)
//   2. ⚠ Not Significant — p ≥ 0.05     AND n ≥ 30 (amber)
//   3. ⏳ Insufficient Data — n < 30 (gray, regardless of p)
//
// Plus:
//   * Tooltip content matches the active state.
//   * p-value formatting (3dp, "<0.001" for tiny p).
//   * The `isSignificant` flag wins over a contradictory `pValue`
//     (backward compat with the existing PaperMetrics contract).
//   * n value is always surfaced in the badge label.

import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  StatisticalSignificanceBadge,
  type StatisticalSignificanceBadgeProps,
} from './StatisticalSignificanceBadge'

function renderBadge(props: Partial<StatisticalSignificanceBadgeProps> = {}) {
  const defaults: StatisticalSignificanceBadgeProps = {
    pValue: 0.03,
    n: 42,
    isSignificant: true,
  }
  return render(
    <StatisticalSignificanceBadge {...defaults} {...props} />,
  )
}

describe('StatisticalSignificanceBadge', () => {
  // ── State 1: Significant ────────────────────────────────────────────

  it('renders the "Significant" label when isSignificant=true and n≥30', () => {
    renderBadge({ isSignificant: true, pValue: 0.034, n: 42 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Significant (p=0.034, n=42)')
    // Icon "✓" should be present
    expect(label.parentElement?.textContent ?? '').toContain('✓')
  })

  it('uses a green colour scheme when significant', () => {
    renderBadge({ isSignificant: true, pValue: 0.034, n: 42 })
    const badge = screen.getByTestId('statistical-significance-badge')
    expect(badge.className).toContain('border-green-500/40')
    expect(badge.className).toContain('bg-green-500/10')
    expect(badge.className).toContain('text-green-400')
  })

  it('renders p<0.001 for tiny p-values', () => {
    renderBadge({ isSignificant: true, pValue: 0.0001, n: 1000 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Significant (p<0.001, n=1000)')
  })

  // ── State 2: Not Significant ────────────────────────────────────────

  it('renders the "Not Significant" label when isSignificant=false and n≥30', () => {
    renderBadge({ isSignificant: false, pValue: 0.12, n: 50 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Not Significant (p=0.120, n=50)')
    expect(label.parentElement?.textContent ?? '').toContain('⚠')
  })

  it('uses an amber colour scheme when not significant', () => {
    renderBadge({ isSignificant: false, pValue: 0.12, n: 50 })
    const badge = screen.getByTestId('statistical-significance-badge')
    expect(badge.className).toContain('border-amber-500/40')
    expect(badge.className).toContain('bg-amber-500/10')
    expect(badge.className).toContain('text-amber-300')
  })

  it('still renders "Not Significant" even when p < 0.05 if isSignificant=false', () => {
    // Backward-compat: the explicit `isSignificant` flag from the
    // backend wins over the client-derived verdict from pValue.
    renderBadge({ isSignificant: false, pValue: 0.02, n: 50 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Not Significant')
  })

  // ── State 3: Insufficient Data ──────────────────────────────────────

  it('renders the "Insufficient Data" label when n < 30 (regardless of p)', () => {
    // Even with a "significant" p-value, n < 30 should override.
    renderBadge({ isSignificant: true, pValue: 0.001, n: 15 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Insufficient Data (n=15<30)')
    expect(label.parentElement?.textContent ?? '').toContain('⏳')
  })

  it('uses a gray colour scheme when insufficient data', () => {
    renderBadge({ isSignificant: true, pValue: 0.001, n: 15 })
    const badge = screen.getByTestId('statistical-significance-badge')
    expect(badge.className).toContain('border-gray-500/40')
    expect(badge.className).toContain('bg-gray-500/10')
    expect(badge.className).toContain('text-gray-400')
  })

  it('renders "Insufficient Data" when n = 29 (boundary)', () => {
    // n = 29 < 30 → insufficient.
    renderBadge({ isSignificant: true, pValue: 0.001, n: 29 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Insufficient Data')
  })

  it('does NOT render "Insufficient Data" when n = 30 (boundary)', () => {
    // n = 30 ≥ 30 → falls through to the significant/not-significant
    // branch based on isSignificant.
    renderBadge({ isSignificant: true, pValue: 0.034, n: 30 })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Significant')
    expect(label).not.toHaveTextContent('Insufficient Data')
  })

  // ── p-value formatting edge cases ───────────────────────────────────

  it('renders "p=n/a" for p-value when pValue is undefined', () => {
    renderBadge({ isSignificant: true, n: 50, pValue: undefined })
    const label = screen.getByTestId('sig-badge-label')
    expect(label).toHaveTextContent('Significant (p=n/a, n=50)')
  })

  it('renders p<0.001 when pValue < 0.001', () => {
    renderBadge({ isSignificant: true, pValue: 0.0005, n: 100 })
    expect(screen.getByTestId('sig-badge-label')).toHaveTextContent('p<0.001')
  })

  it('formats p-value with 3 decimal places', () => {
    renderBadge({ isSignificant: true, pValue: 0.03456, n: 100 })
    expect(screen.getByTestId('sig-badge-label')).toHaveTextContent('p=0.035')
  })

  // ── Tooltip ────────────────────────────────────────────────────────
  //
  // NOTE on duplicate tooltip content: Radix renders the tooltip
  // content TWICE in the DOM — once visibly in the portal, and once
  // as a visually-hidden `role="tooltip"` element (so screen readers
  // can announce the content without hovering). We use
  // `findAllByText` + `.length > 0` instead of `findByText` for
  // tooltip-content assertions.

  it('surfaces the "significant" explanation in the tooltip', async () => {
    const user = userEvent.setup()
    renderBadge({ isSignificant: true, pValue: 0.034, n: 42 })

    await user.hover(screen.getByTestId('statistical-significance-badge'))

    await screen.findAllByText('Statistical significance')
    expect(screen.getAllByText(/unlikely to have arisen by chance/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Threshold:/).length).toBeGreaterThan(0)
    // Threshold summary mentions both α and n — check the body text
    // (the threshold line) contains both numbers.
    const thresholdLine = screen.getAllByText(/Threshold:/)[0]
    const thresholdText = thresholdLine.parentElement?.textContent ?? ''
    expect(thresholdText).toMatch(/0\.05/)
    expect(thresholdText).toMatch(/30/)
  })

  it('surfaces the "not significant" explanation in the tooltip', async () => {
    const user = userEvent.setup()
    renderBadge({ isSignificant: false, pValue: 0.12, n: 50 })

    await user.hover(screen.getByTestId('statistical-significance-badge'))

    await screen.findAllByText('Statistical significance')
    expect(screen.getAllByText(/not distinguishable from a 50%/).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/coin-flip null/).length).toBeGreaterThan(0)
  })

  it('surfaces the "insufficient data" explanation in the tooltip', async () => {
    const user = userEvent.setup()
    renderBadge({ isSignificant: true, pValue: 0.001, n: 15 })

    await user.hover(screen.getByTestId('statistical-significance-badge'))

    await screen.findAllByText('Statistical significance')
    expect(screen.getAllByText(/below the 30-trade minimum/).length).toBeGreaterThan(0)
  })

  it('exposes an aria-label describing the state + p-value + n', () => {
    renderBadge({ isSignificant: true, pValue: 0.034, n: 42 })
    const badge = screen.getByTestId('statistical-significance-badge')
    const ariaLabel = badge.getAttribute('aria-label') ?? ''
    expect(ariaLabel).toMatch(/Statistical significance/i)
    expect(ariaLabel).toMatch(/Significant/)
    expect(ariaLabel).toMatch(/p=0\.034/)
    expect(ariaLabel).toMatch(/n=42/)
  })

  // ── Custom className passthrough ────────────────────────────────────

  it('merges the className prop onto the badge container', () => {
    renderBadge({ className: 'mt-2 self-start' })
    const badge = screen.getByTestId('statistical-significance-badge')
    expect(badge.className).toContain('mt-2')
    expect(badge.className).toContain('self-start')
  })
})
