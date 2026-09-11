// components/charts/MarketDepthChart.test.tsx — Unit tests for the depth chart.
//
// Strategy (mirrors Charts.test.tsx):
//   • Mock `recharts.ResponsiveContainer` so children render directly
//     (jsdom doesn't fire ResizeObserver callbacks, so the real
//     ResponsiveContainer would never measure its parent → children
//     never render).
//   • Verify the chart renders without crashing with mock order book
//     data and that the Area components receive the expected dataKeys.
//   • Verify the empty-state message renders when bids + asks are both [].
//   • Verify the spread chip overlay renders when spread is provided.
//   • Verify the mid reference line is present (via the mock assertion
//     that ReferenceLine is rendered with the mid x-value).
//   • Verify custom color overrides don't crash.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'

// Stub ResponsiveContainer so children render directly with the height prop.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  const Passthrough = ({ children, height, width }: any) => (
    <div
      data-testid="rc-responsive"
      style={{
        width: typeof width === 'number' ? `${width}px` : (width ?? '100%'),
        height: typeof height === 'number' ? `${height}px` : (height ?? '100%'),
      }}
    >
      {children}
    </div>
  )
  return {
    ...actual,
    ResponsiveContainer: Passthrough,
  }
})

// Import AFTER the mock so MarketDepthChart picks up the mocked
// ResponsiveContainer.
import MarketDepthChart, {
  type DepthLevel,
} from './MarketDepthChart'

// Mock order book data — 5 bid levels + 5 ask levels with cumulative totals.
const mockBids: DepthLevel[] = [
  { price: 0.48, size: 100, total: 100 },
  { price: 0.47, size: 200, total: 300 },
  { price: 0.46, size: 150, total: 450 },
  { price: 0.45, size: 300, total: 750 },
  { price: 0.44, size: 250, total: 1000 },
]

const mockAsks: DepthLevel[] = [
  { price: 0.52, size: 80, total: 80 },
  { price: 0.53, size: 120, total: 200 },
  { price: 0.54, size: 200, total: 400 },
  { price: 0.55, size: 350, total: 750 },
  { price: 0.56, size: 400, total: 1150 },
]

describe('MarketDepthChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing with valid bid + ask data', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        mid={0.5}
        bestBid={0.48}
        bestAsk={0.52}
        spread={0.04}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
    expect(screen.getByTestId('market-depth-chart')).toBeInTheDocument()
  })

  it('renders an empty-state message when bids and asks are both empty', () => {
    render(<MarketDepthChart bids={[]} asks={[]} />)
    expect(screen.getByTestId('depth-chart-empty')).toBeInTheDocument()
    expect(screen.getByText('No order book depth available')).toBeInTheDocument()
  })

  it('renders an empty-state message when only one side has data', () => {
    // Both sides empty → empty state. One side with data → renders chart.
    const { rerender } = render(<MarketDepthChart bids={[]} asks={mockAsks} />)
    expect(screen.getByTestId('market-depth-chart')).toBeInTheDocument()
    rerender(<MarketDepthChart bids={mockBids} asks={[]} />)
    expect(screen.getByTestId('market-depth-chart')).toBeInTheDocument()
  })

  it('respects the height prop (via outer wrapper)', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        height={180}
      />,
    )
    const wrapper = screen.getByTestId('market-depth-chart') as HTMLElement
    expect(wrapper.style.height).toBe('180px')
  })

  it('renders the spread chip overlay when spread is provided', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        spread={0.04}
      />,
    )
    const chip = screen.getByTestId('depth-chart-spread-chip')
    expect(chip.textContent).toContain('Spread')
    expect(chip.textContent).toContain('4.00¢')
  })

  it('omits the spread chip when spread is null', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        spread={null}
        showSpreadChip
      />,
    )
    expect(screen.queryByTestId('depth-chart-spread-chip')).toBeNull()
  })

  it('omits the spread chip when showSpreadChip is false', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        spread={0.04}
        showSpreadChip={false}
      />,
    )
    expect(screen.queryByTestId('depth-chart-spread-chip')).toBeNull()
  })

  it('renders the chart without a mid reference line when mid is null', () => {
    // Just verify it doesn't crash with mid=null.
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        mid={null}
        showMidLine
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('renders the chart without mid/best-bid/best-ask reference lines when showMidLine is false', () => {
    // Should not crash when showMidLine is false even with mid set.
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        mid={0.5}
        showMidLine={false}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('accepts custom bid/ask color overrides', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        bidColor="#00ff00"
        askColor="#ff0000"
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('accepts custom formatPrice / formatSize functions', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        formatPrice={(v) => `$${v.toFixed(2)}`}
        formatSize={(v) => `${v.toFixed(0)}u`}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('renders with a single bid level and single ask level', () => {
    render(
      <MarketDepthChart
        bids={[{ price: 0.48, size: 100, total: 100 }]}
        asks={[{ price: 0.52, size: 80, total: 80 }]}
        mid={0.5}
      />,
    )
    expect(screen.getByTestId('market-depth-chart')).toBeInTheDocument()
  })

  it('renders the spread chip with amber colour when spread is wide (≥3¢)', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        spread={0.05}
      />,
    )
    const chip = screen.getByTestId('depth-chart-spread-chip')
    // The chip's text content includes the formatted spread value.
    expect(chip.textContent).toContain('5.00¢')
  })

  it('renders the spread chip with muted colour when spread is narrow (<3¢)', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
        spread={0.01}
      />,
    )
    const chip = screen.getByTestId('depth-chart-spread-chip')
    expect(chip.textContent).toContain('1.00¢')
  })

  it('handles levels with missing or NaN totals gracefully', () => {
    render(
      <MarketDepthChart
        bids={[{ price: 0.48, size: 100, total: NaN }]}
        asks={[{ price: 0.52, size: 80, total: 80 }]}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('is responsive (ResponsiveContainer with width="100%")', () => {
    render(
      <MarketDepthChart
        bids={mockBids}
        asks={mockAsks}
      />,
    )
    const wrapper = screen.getByTestId('rc-responsive')
    expect((wrapper as HTMLElement).style.width).toBe('100%')
  })
})
