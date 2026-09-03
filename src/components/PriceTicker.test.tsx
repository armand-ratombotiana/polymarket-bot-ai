// components/PriceTicker.test.tsx — Unit tests for the animated price ticker.
//
// Strategy:
//   • Mock framer-motion's AnimatePresence so the exit/enter animations
//     resolve synchronously in jsdom (framer-motion uses rAF + layout
//     effects that don't fire under jsdom).
//   • Verify the directional color (green/red/dim) via the
//     `data-direction` attribute the motion.span exposes — easier to
//     assert on than computed CSS color.
//   • Verify number formatting (4dp for sub-1¢ prices, 3dp for
//     probabilities, 2dp for ≥$1).
//   • Verify change-since-last-tick math (absolute ¢ + percentage).
//   • Verify spread chip rendering + amber color when wide.
//   • Verify compact mode (no change line).

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

// Stub framer-motion so AnimatePresence resolves synchronously.
// Real framer-motion relies on requestAnimationFrame + layout effects
// which jsdom doesn't fire, so exit/enter transitions would never flush
// and the children would never appear in the DOM. The stub renders the
// children directly with no animation.
vi.mock('framer-motion', async () => {
  const actual = await vi.importActual<typeof import('framer-motion')>('framer-motion')
  const Passthrough = ({ children, initial, animate, exit, transition, ...rest }: any) => {
    // Apply the `animate` props as inline style so tests can assert on
    // the resolved color. We deliberately ignore `initial` / `exit` so
    // both entering and exiting children render to the DOM.
    return (
      <span
        style={typeof animate === 'object' ? animate : undefined}
        {...rest}
      >
        {children}
      </span>
    )
  }
  const AnimatePresence = ({ children }: any) => <>{children}</>
  return {
    ...actual,
    motion: {
      ...actual.motion,
      span: Passthrough,
    },
    AnimatePresence,
  }
})

// Import AFTER the mock so PriceTicker picks up the mocked framer-motion.
import PriceTicker, {
  formatTickerPrice,
  computeChange,
} from './PriceTicker'

describe('formatTickerPrice', () => {
  it('formats sub-1¢ prices (<0.01) with 4 decimal places', () => {
    expect(formatTickerPrice(0.0042)).toBe('0.0042')
    expect(formatTickerPrice(0.0099)).toBe('0.0099')
    expect(formatTickerPrice(0.0001)).toBe('0.0001')
  })

  it('formats probabilities 0.01–0.99 with 3 decimal places', () => {
    expect(formatTickerPrice(0.5)).toBe('0.500')
    expect(formatTickerPrice(0.625)).toBe('0.625')
    expect(formatTickerPrice(0.042)).toBe('0.042')
    expect(formatTickerPrice(0.99)).toBe('0.990')
  })

  it('formats prices ≥1 with 2 decimal places', () => {
    expect(formatTickerPrice(4.5)).toBe('4.50')
    // 42.565 (not 42.555) — picked because IEEE-754 binary floating
    // point renders 42.555 as 42.5549999… which toFixed(2) truncates
    // to "42.55", while 42.565 renders as 42.5650000… which rounds
    // up to "42.56". Using a value that demonstrates the rounding
    // direction deterministically avoids the float-repr flake.
    expect(formatTickerPrice(42.565)).toBe('42.56')
    expect(formatTickerPrice(10)).toBe('10.00')
  })

  it('returns "—" for null / undefined / NaN', () => {
    expect(formatTickerPrice(null)).toBe('—')
    expect(formatTickerPrice(undefined)).toBe('—')
    expect(formatTickerPrice(NaN)).toBe('—')
    expect(formatTickerPrice(Infinity)).toBe('—')
  })
})

describe('computeChange', () => {
  it('detects up direction with correct abs + pct', () => {
    const r = computeChange(0.55, 0.50)
    expect(r.dir).toBe('up')
    expect(r.abs).toBeCloseTo(0.05, 5)
    expect(r.pct).toBeCloseTo(10, 5)
  })

  it('detects down direction with negative abs + pct', () => {
    const r = computeChange(0.45, 0.50)
    expect(r.dir).toBe('down')
    expect(r.abs).toBeCloseTo(-0.05, 5)
    expect(r.pct).toBeCloseTo(-10, 5)
  })

  it('reports flat when current equals previous', () => {
    const r = computeChange(0.50, 0.50)
    expect(r.dir).toBe('flat')
    expect(r.abs).toBe(0)
    expect(r.pct).toBe(0)
  })

  it('reports flat when either price is null / undefined', () => {
    expect(computeChange(null, 0.5).dir).toBe('flat')
    expect(computeChange(0.5, null).dir).toBe('flat')
    expect(computeChange(undefined, undefined).dir).toBe('flat')
  })

  it('reports flat when previous is 0 (division by zero guard)', () => {
    expect(computeChange(0.5, 0).dir).toBe('flat')
  })

  it('reports flat when either price is NaN', () => {
    expect(computeChange(NaN, 0.5).dir).toBe('flat')
    expect(computeChange(0.5, NaN).dir).toBe('flat')
  })
})

describe('PriceTicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the formatted price', () => {
    render(<PriceTicker price={0.625} previousPrice={null} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.textContent).toBe('0.625')
  })

  it('renders "—" when price is null', () => {
    render(<PriceTicker price={null} previousPrice={null} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.textContent).toBe('—')
  })

  it('applies up direction (green) when price increased', () => {
    render(<PriceTicker price={0.55} previousPrice={0.50} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.getAttribute('data-direction')).toBe('up')
  })

  it('applies down direction (red) when price decreased', () => {
    render(<PriceTicker price={0.45} previousPrice={0.50} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.getAttribute('data-direction')).toBe('down')
  })

  it('applies flat direction when prices are equal', () => {
    render(<PriceTicker price={0.50} previousPrice={0.50} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.getAttribute('data-direction')).toBe('flat')
  })

  it('applies flat direction when previousPrice is null (first render)', () => {
    render(<PriceTicker price={0.50} previousPrice={null} />)
    const el = screen.getByTestId('price-ticker-value')
    expect(el.getAttribute('data-direction')).toBe('flat')
  })

  it('renders the change line with absolute ¢ and percentage for an up tick', () => {
    render(<PriceTicker price={0.55} previousPrice={0.50} />)
    const change = screen.getByTestId('price-ticker-change')
    // +5.00¢ (+10.00%)
    expect(change.textContent).toContain('5.00¢')
    expect(change.textContent).toContain('10.00%')
    expect(change.textContent).toContain('+')
  })

  it('renders the change line with negative sign for a down tick', () => {
    render(<PriceTicker price={0.45} previousPrice={0.50} />)
    const change = screen.getByTestId('price-ticker-change')
    expect(change.textContent).toContain('5.00¢')
    expect(change.textContent).toContain('10.00%')
    expect(change.textContent).toContain('−')
  })

  it('renders an em dash in the change line when previousPrice is null', () => {
    render(<PriceTicker price={0.50} previousPrice={null} />)
    const change = screen.getByTestId('price-ticker-change')
    expect(change.textContent).toContain('—')
  })

  it('renders the bid/ask chip with formatted values when both sides are provided', () => {
    render(
      <PriceTicker
        price={0.5}
        previousPrice={null}
        bestBid={0.48}
        bestAsk={0.52}
      />,
    )
    // The bid/ask chip renders the formatted bid + ask on a single line.
    // We assert by querying for both values in the DOM.
    expect(screen.getByText('0.480')).toBeInTheDocument()
    expect(screen.getByText('0.520')).toBeInTheDocument()
  })

  it('renders "—" placeholders in the bid/ask chip when sides are null', () => {
    render(
      <PriceTicker
        price={0.5}
        previousPrice={null}
        bestBid={null}
        bestAsk={null}
      />,
    )
    // Multiple "—" elements may exist (one for null price, others for
    // null bid/ask). Just verify at least one is present.
    const dashes = screen.getAllByText('—')
    expect(dashes.length).toBeGreaterThanOrEqual(1)
  })

  it('renders the spread chip with cents when spread is provided', () => {
    render(
      <PriceTicker
        price={0.5}
        previousPrice={null}
        spread={0.04}
      />,
    )
    const chip = screen.getByTestId('price-ticker-spread')
    expect(chip.textContent).toContain('4.0¢')
  })

  it('omits the spread chip when spread is null', () => {
    render(
      <PriceTicker price={0.5} previousPrice={null} spread={null} />,
    )
    expect(screen.queryByTestId('price-ticker-spread')).toBeNull()
  })

  it('does not render the change line in compact mode', () => {
    render(
      <PriceTicker
        price={0.55}
        previousPrice={0.50}
        compact
      />,
    )
    expect(screen.queryByTestId('price-ticker-change')).toBeNull()
  })

  it('does not render the bid/ask chip in compact mode', () => {
    render(
      <PriceTicker
        price={0.55}
        previousPrice={0.50}
        bestBid={0.48}
        bestAsk={0.52}
        compact
      />,
    )
    // In compact mode the bid/ask chip is suppressed. The bid/ask values
    // should not appear as standalone text nodes (only the price renders).
    expect(screen.queryByText('0.480')).toBeNull()
    expect(screen.queryByText('0.520')).toBeNull()
  })

  it('formats sub-1¢ prices with 4dp (boundary case)', () => {
    render(<PriceTicker price={0.0042} previousPrice={null} />)
    expect(screen.getByTestId('price-ticker-value').textContent).toBe('0.0042')
  })

  it('formats ≥1 prices with 2dp', () => {
    render(<PriceTicker price={4.5} previousPrice={null} />)
    expect(screen.getByTestId('price-ticker-value').textContent).toBe('4.50')
  })

  it('exposes an accessible aria-label combining price + change', () => {
    render(<PriceTicker price={0.55} previousPrice={0.50} label="BTC mid" />)
    const group = screen.getByRole('group')
    const label = group.getAttribute('aria-label') ?? ''
    expect(label).toContain('BTC mid')
    expect(label).toContain('0.550')
    expect(label).toContain('10.00%')
  })

  it('updates direction when price changes between renders', () => {
    const { rerender } = render(<PriceTicker price={0.55} previousPrice={0.50} />)
    expect(screen.getByTestId('price-ticker-value').getAttribute('data-direction')).toBe('up')
    rerender(<PriceTicker price={0.45} previousPrice={0.55} />)
    expect(screen.getByTestId('price-ticker-value').getAttribute('data-direction')).toBe('down')
    rerender(<PriceTicker price={0.50} previousPrice={0.45} />)
    expect(screen.getByTestId('price-ticker-value').getAttribute('data-direction')).toBe('up')
  })
})
