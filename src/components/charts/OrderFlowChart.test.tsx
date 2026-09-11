// components/charts/OrderFlowChart.test.tsx — Unit tests for the order-flow chart.
//
// Strategy (mirrors MarketDepthChart.test.tsx):
//   • Mock `recharts.ResponsiveContainer` so children render directly
//     (jsdom doesn't fire ResizeObserver callbacks, so the real
//     ResponsiveContainer would never measure its parent → children
//     never render).
//   • Verify the chart renders without crashing with a mixed buy/sell
//     trade stream.
//   • Verify the empty-state message renders when no trades are in
//     the time window.
//   • Verify trades outside the window are filtered out (5m window
//     keeps a 3-minute-old trade; 30s window drops it).
//   • Verify the cumulative delta accumulates correctly across the
//     window (the `buildChartData` helper is the source of truth).
//   • Verify buyVol / sellVol diverge (buys positive, sells negative).
//   • Verify maxBars caps the rendered rows.
//   • Verify the custom color overrides don't crash.

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

// Import AFTER the mock so OrderFlowChart picks up the mocked
// ResponsiveContainer.
import OrderFlowChart, {
  type FlowTrade,
  buildChartData,
} from './OrderFlowChart'

// Fixed `now` so the time-window assertions are deterministic. The test
// suite is run in jsdom where Date.now() returns real wall-clock ms —
// fixing `now` means a 3-minute-old trade is ALWAYS 180s old, regardless
// of when CI runs the suite.
const NOW = 1_700_000_000_000 // 2023-11-14T22:53:20Z

// Three buys + two sells spanning 4 minutes. Used by multiple tests.
const mixedTrades: FlowTrade[] = [
  { timestamp: NOW - 240_000, side: 'BUY', size: 10, price: 0.50 }, // 4m ago
  { timestamp: NOW - 180_000, side: 'BUY', size: 20, price: 0.51 }, // 3m ago
  { timestamp: NOW - 120_000, side: 'SELL', size: 15, price: 0.49 }, // 2m ago
  { timestamp: NOW - 60_000, side: 'BUY', size: 30, price: 0.52 }, // 1m ago
  { timestamp: NOW - 15_000, side: 'SELL', size: 25, price: 0.48 }, // 15s ago
]

describe('buildChartData', () => {
  it('filters trades outside the time window', () => {
    // 5-minute window — all 5 trades are within 4 minutes, so all pass.
    const rows5m = buildChartData(mixedTrades, 5 * 60_000, NOW, 100)
    expect(rows5m.length).toBe(5)

    // 1-minute window — the boundary trade (exactly 60s old, inclusive)
    // + the 15s-old trade both survive. The cutoff `now - windowMs`
    // is INCLUSIVE so the most-recent 60s of activity stays visible.
    const rows1m = buildChartData(mixedTrades, 60_000, NOW, 100)
    expect(rows1m.length).toBe(2)
    // Newest trade at the bottom (sorted oldest-first).
    expect(rows1m[rows1m.length - 1].side).toBe('SELL')
  })

  it('sorts trades oldest-first on the X axis', () => {
    const rows = buildChartData(mixedTrades, 5 * 60_000, NOW, 100)
    expect(rows[0].ts).toBe(NOW - 240_000)
    expect(rows[rows.length - 1].ts).toBe(NOW - 15_000)
  })

  it('splits buy and sell volume into separate fields with sells negative', () => {
    const rows = buildChartData(mixedTrades, 5 * 60_000, NOW, 100)
    // Trade 0 (BUY 10) → buyVol=10, sellVol=0
    expect(rows[0].buyVol).toBe(10)
    expect(rows[0].sellVol).toBe(0)
    // Trade 2 (SELL 15) → buyVol=0, sellVol=-15 (negative for divergent bar)
    expect(rows[2].buyVol).toBe(0)
    expect(rows[2].sellVol).toBe(-15)
  })

  it('accumulates the cumulative delta correctly across the window', () => {
    const rows = buildChartData(mixedTrades, 5 * 60_000, NOW, 100)
    // Expected delta after each trade (BUY +, SELL -):
    //   +10, +30, +15, +45, +20
    expect(rows[0].delta).toBe(10)
    expect(rows[1].delta).toBe(30)
    expect(rows[2].delta).toBe(15)
    expect(rows[3].delta).toBe(45)
    expect(rows[4].delta).toBe(20)
  })

  it('caps the rendered rows at maxBars (drops oldest beyond the cap)', () => {
    const rows = buildChartData(mixedTrades, 5 * 60_000, NOW, 3)
    // Only the 3 most-recent trades should remain.
    expect(rows.length).toBe(3)
    expect(rows[0].ts).toBe(NOW - 120_000)
    expect(rows[2].ts).toBe(NOW - 15_000)
  })

  it('returns an empty array when no trades are in window', () => {
    const rows = buildChartData([], 60_000, NOW, 100)
    expect(rows).toEqual([])
  })

  it('skips trades with NaN or non-finite timestamps', () => {
    const bad: FlowTrade[] = [
      { timestamp: NaN, side: 'BUY', size: 5, price: 0.5 },
      { timestamp: NOW - 10_000, side: 'BUY', size: 5, price: 0.5 },
    ]
    const rows = buildChartData(bad, 60_000, NOW, 100)
    expect(rows.length).toBe(1)
  })
})

describe('OrderFlowChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing with valid trades', () => {
    render(<OrderFlowChart trades={mixedTrades} now={NOW} window="5m" />)
    expect(screen.getByTestId('order-flow-chart')).toBeInTheDocument()
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('renders the empty-state message when no trades are provided', () => {
    render(<OrderFlowChart trades={[]} now={NOW} window="1m" />)
    expect(screen.getByTestId('order-flow-chart-empty')).toBeInTheDocument()
    expect(screen.getByText(/No order flow in the last 1m/)).toBeInTheDocument()
  })

  it('renders the empty-state message when all trades fall outside the window', () => {
    // 30s window but the newest trade is 15s old — should still render.
    const { rerender } = render(
      <OrderFlowChart trades={[mixedTrades[4]]} now={NOW} window="30s" />,
    )
    expect(screen.getByTestId('order-flow-chart')).toBeInTheDocument()

    // Now make the only trade fall outside the window — 1m window but the
    // trade is 2m old.
    rerender(<OrderFlowChart trades={[mixedTrades[2]]} now={NOW} window="1m" />)
    expect(screen.getByTestId('order-flow-chart-empty')).toBeInTheDocument()
  })

  it('respects the height prop (via outer wrapper)', () => {
    render(
      <OrderFlowChart trades={mixedTrades} now={NOW} window="5m" height={180} />,
    )
    const wrapper = screen.getByTestId('order-flow-chart') as HTMLElement
    expect(wrapper.style.height).toBe('180px')
  })

  it('renders the time-window label in the empty-state message', () => {
    render(<OrderFlowChart trades={[]} now={NOW} window="30s" />)
    expect(screen.getByText(/No order flow in the last 30s/)).toBeInTheDocument()
  })

  it('accepts custom buy/sell/delta color overrides without crashing', () => {
    render(
      <OrderFlowChart
        trades={mixedTrades}
        now={NOW}
        window="5m"
        buyColor="#00ff00"
        sellColor="#ff0000"
        deltaColor="#ffff00"
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('does not crash when showDeltaLine is false (right Y-axis omitted)', () => {
    render(
      <OrderFlowChart
        trades={mixedTrades}
        now={NOW}
        window="5m"
        showDeltaLine={false}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('does not crash when showZeroLine is false', () => {
    render(
      <OrderFlowChart
        trades={mixedTrades}
        now={NOW}
        window="5m"
        showZeroLine={false}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('handles a single trade without crashing', () => {
    render(
      <OrderFlowChart
        trades={[{ timestamp: NOW - 5_000, side: 'BUY', size: 12, price: 0.5 }]}
        now={NOW}
        window="30s"
      />,
    )
    expect(screen.getByTestId('order-flow-chart')).toBeInTheDocument()
  })

  it('is responsive (ResponsiveContainer with width="100%")', () => {
    render(<OrderFlowChart trades={mixedTrades} now={NOW} window="5m" />)
    const wrapper = screen.getByTestId('rc-responsive')
    expect((wrapper as HTMLElement).style.width).toBe('100%')
  })

  it('caps the rendered rows at maxBars', () => {
    // 5 trades in window; maxBars=2 → only the 2 most-recent render.
    // The chart still renders (no empty state).
    render(
      <OrderFlowChart
        trades={mixedTrades}
        now={NOW}
        window="5m"
        maxBars={2}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })
})
