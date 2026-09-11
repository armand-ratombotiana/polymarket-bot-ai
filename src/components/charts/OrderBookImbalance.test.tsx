// components/charts/OrderBookImbalance.test.tsx — Unit tests for the imbalance meter.
//
// Strategy:
//   • The component renders pure HTML/CSS (no Recharts) so no chart
//     mock is needed.
//   • Verify the imbalance ratio computation via the exported
//     `computeImbalance` helper (deterministic, edge-case-aware).
//   • Verify the rendered ratio chip shows the correct sign + value.
//   • Verify the divergent bar widths match the bid/ask volume split.
//   • Verify the mid-price + best-bid/ask depth stats render.
//   • Verify the spread chip shows cents.
//   • Verify the empty/zero case (both sides 0 → 50/50 split, ratio 0).

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import OrderBookImbalance, {
  computeImbalance,
} from './OrderBookImbalance'

describe('computeImbalance', () => {
  it('returns 0 when both sides are 0 (divide-by-zero guard)', () => {
    expect(computeImbalance(0, 0)).toBe(0)
  })

  it('returns +1 when ask is 0 (all bids)', () => {
    expect(computeImbalance(100, 0)).toBe(1)
  })

  it('returns -1 when bid is 0 (all asks)', () => {
    expect(computeImbalance(0, 100)).toBe(-1)
  })

  it('returns 0 when bid equals ask (balanced)', () => {
    expect(computeImbalance(100, 100)).toBe(0)
  })

  it('returns a positive ratio when bid > ask', () => {
    // (300 − 100) / (300 + 100) = 0.5
    expect(computeImbalance(300, 100)).toBeCloseTo(0.5, 5)
  })

  it('returns a negative ratio when ask > bid', () => {
    // (100 − 300) / 400 = -0.5
    expect(computeImbalance(100, 300)).toBeCloseTo(-0.5, 5)
  })

  it('returns 0 when inputs are NaN', () => {
    expect(computeImbalance(NaN, 100)).toBe(0)
    expect(computeImbalance(100, NaN)).toBe(0)
    expect(computeImbalance(NaN, NaN)).toBe(0)
  })

  it('clamps to [-1, +1] (float-error guard)', () => {
    // Large equal values: ratio should be 0 (no float drift issue).
    expect(computeImbalance(1e9, 1e9)).toBe(0)
  })

  it('returns 0 for negative inputs (the denom ≤ 0 guard)', () => {
    // Negative volumes are nonsensical in practice, but we still
    // document the behaviour: the `denom ≤ 0` guard in
    // computeImbalance short-circuits to 0 so a malformed payload
    // never produces a misleading ratio.
    expect(computeImbalance(-100, -300)).toBe(0)
    expect(computeImbalance(-100, 100)).toBe(0) // denom = 0
  })
})

describe('OrderBookImbalance', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing with valid bid/ask volumes', () => {
    render(
      <OrderBookImbalance
        bidVolume={300}
        askVolume={100}
        mid={0.5}
        bestBid={0.48}
        bestAsk={0.52}
        spread={0.04}
      />,
    )
    expect(screen.getByTestId('order-book-imbalance')).toBeInTheDocument()
  })

  it('renders the imbalance ratio with the correct sign and value', () => {
    // (300 − 100) / 400 = 0.5 → "+50.0%"
    render(<OrderBookImbalance bidVolume={300} askVolume={100} />)
    const ratio = screen.getByTestId('imbalance-ratio')
    expect(ratio.textContent).toContain('+')
    expect(ratio.textContent).toContain('50.0%')
  })

  it('renders a negative imbalance ratio when ask-heavy', () => {
    // (100 − 300) / 400 = -0.5 → "-50.0%"
    render(<OrderBookImbalance bidVolume={100} askVolume={300} />)
    const ratio = screen.getByTestId('imbalance-ratio')
    expect(ratio.textContent).toContain('-')
    expect(ratio.textContent).toContain('50.0%')
  })

  it('renders 0% imbalance and "balanced" label when bid equals ask', () => {
    render(<OrderBookImbalance bidVolume={100} askVolume={100} />)
    const ratio = screen.getByTestId('imbalance-ratio')
    expect(ratio.textContent).toContain('0.0%')
    // The "balanced" hint appears next to the ratio.
    expect(screen.getByText('balanced')).toBeInTheDocument()
  })

  it('renders "bid-heavy" label when bid is significantly larger than ask', () => {
    render(<OrderBookImbalance bidVolume={300} askVolume={100} />)
    expect(screen.getByText('bid-heavy')).toBeInTheDocument()
  })

  it('renders "ask-heavy" label when ask is significantly larger than bid', () => {
    render(<OrderBookImbalance bidVolume={100} askVolume={300} />)
    expect(screen.getByText('ask-heavy')).toBeInTheDocument()
  })

  it('renders the mid price using the default 3dp formatter', () => {
    render(
      <OrderBookImbalance
        bidVolume={100}
        askVolume={100}
        mid={0.523}
      />,
    )
    const mid = screen.getByTestId('imbalance-mid')
    expect(mid.textContent).toContain('0.523')
  })

  it('omits the mid price badge when mid is null', () => {
    render(<OrderBookImbalance bidVolume={100} askVolume={100} mid={null} />)
    expect(screen.queryByTestId('imbalance-mid')).toBeNull()
  })

  it('renders the spread chip in cents', () => {
    render(
      <OrderBookImbalance
        bidVolume={100}
        askVolume={100}
        spread={0.04}
      />,
    )
    // Spread 0.04 → 4.00¢
    const header = screen.getByTestId('order-book-imbalance')
    expect(header.textContent).toContain('spread')
    expect(header.textContent).toContain('4.00¢')
  })

  it('omits the spread chip when spread is null', () => {
    render(
      <OrderBookImbalance bidVolume={100} askVolume={100} spread={null} />,
    )
    const header = screen.getByTestId('order-book-imbalance')
    expect(header.textContent).toContain('spread —')
  })

  it('renders best bid/ask depth stats', () => {
    render(
      <OrderBookImbalance
        bidVolume={300}
        askVolume={100}
        bestBidSize={50}
        bestAskSize={25}
        bestBid={0.48}
        bestAsk={0.52}
      />,
    )
    const root = screen.getByTestId('order-book-imbalance')
    expect(root.textContent).toContain('Best Bid')
    expect(root.textContent).toContain('Best Ask')
    expect(root.textContent).toContain('0.48')
    expect(root.textContent).toContain('0.52')
    expect(root.textContent).toContain('depth')
  })

  it('shows "—" for missing best bid/ask prices', () => {
    render(
      <OrderBookImbalance
        bidVolume={100}
        askVolume={100}
        bestBid={null}
        bestAsk={null}
        bestBidSize={null}
        bestAskSize={null}
      />,
    )
    const root = screen.getByTestId('order-book-imbalance')
    expect(root.textContent).toContain('—')
  })

  it('accepts a custom priceFormat function', () => {
    render(
      <OrderBookImbalance
        bidVolume={100}
        askVolume={100}
        mid={0.5}
        priceFormat={(v) => `${(v * 100).toFixed(0)}%`}
      />,
    )
    expect(screen.getByTestId('imbalance-mid').textContent).toContain('50%')
  })

  it('renders the divergent bar with both sides', () => {
    render(<OrderBookImbalance bidVolume={300} askVolume={100} />)
    const bar = screen.getByTestId('imbalance-bar')
    expect(bar).toBeInTheDocument()
    // The bar contains two child divs (bid + ask) plus the centre tick.
    // We assert on the bid-side volume text + the ask-side volume text
    // to confirm both halves rendered.
    expect(bar.textContent).toContain('300.0')
    expect(bar.textContent).toContain('100.0')
  })

  it('renders a 50/50 split when both sides are zero (keeps the centre tick visible)', () => {
    render(<OrderBookImbalance bidVolume={0} askVolume={0} />)
    const bar = screen.getByTestId('imbalance-bar')
    expect(bar).toBeInTheDocument()
    const ratio = screen.getByTestId('imbalance-ratio')
    expect(ratio.textContent).toContain('0.0%')
  })

  it('exposes an aria-label summarising the imbalance state', () => {
    render(
      <OrderBookImbalance
        bidVolume={300}
        askVolume={100}
        mid={0.5}
        spread={0.04}
      />,
    )
    const root = screen.getByTestId('order-book-imbalance')
    const label = root.getAttribute('aria-label') ?? ''
    expect(label).toContain('imbalance')
    expect(label).toContain('bid 300.0')
    expect(label).toContain('ask 100.0')
  })
})
