// components/OrderFlowPanel.test.tsx — Order-flow workstation render tests (W28-3).
//
// Strategy:
//   * `OrderFlowPanel` is a pure-presentational component that receives its
//     `trades`, `orderBooks`, and `isRealtime` props from the parent's
//     useBot hook. It does NOT open its own WebSocket — but it DOES poll
//     `/api/depth/{token_id}` every 2s for the selected market's depth
//     ladder. We mock `global.fetch` (already installed as `vi.fn()` in
//     `src/test/setup.ts`) so the polling effect's initial fetch resolves
//     with a minimal depth payload without leaking a real network call.
//   * Each test rebuilds the fetch mock so cases are independent.
//
// What's covered:
//   1. Renders the panel container + region role.
//   2. Renders the panel header (token selector + window buttons + stats).
//   3. Renders the order-flow chart card heading.
//   4. Renders the imbalance + tape card headings.
//   5. Renders the LIVE badge when isRealtime is true.
//   6. Renders the POLL badge when isRealtime is false.
//   7. Renders the "No markets available" placeholder when orderBooks is empty.
//   8. Renders without crashing with empty trades + empty books.
//   9. Surfaces the cumulative Δ stat (the "+X.X" badge).
//  10. Surfaces the Tape-speed stat (the "X/min" badge).
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import OrderFlowPanel from './OrderFlowPanel'
import type { Trade, OrderBook } from '@/hooks/useBot'

// ── Fetch mock ──────────────────────────────────────────────────────────────
// The depth fetch returns a minimal valid DepthData payload — the
// imbalance meter only needs bidVolume/askVolume aggregates to render.
function mockDepthOk() {
  return vi.fn().mockImplementation((input: string) => {
    if (typeof input === 'string' && input.includes('/api/depth/')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          token_id: 'tok-a',
          bids: [{ price: 0.48, size: 100, total: 100 }],
          asks: [{ price: 0.52, size: 80, total: 80 }],
          mid: 0.5,
          spread: 0.04,
          best_bid: 0.48,
          best_ask: 0.52,
        }),
      } as Response)
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response)
  })
}

// ── Fixtures ────────────────────────────────────────────────────────────────
// W28-1 — `OrderBook` interface (src/hooks/useBot.ts) carries:
//   { token_id, slug, best_bid, best_ask, mid, spread, updated_at }
// The previous fixture was casting an object that had `bids`/`asks`
// (depth-ladder fields — those are fetched separately via
// /api/depth/{token_id}) AND was missing `updated_at`, so the
// `as OrderBook` cast tripped TS2352 ("Conversion may be a mistake").
// Fixed by matching the OrderBook shape exactly; `bids`/`asks` are
// supplied by the `mockDepthOk` fetch mock above.
const sampleOrderBooks: OrderBook[] = [
  {
    token_id: 'tok-a',
    slug: 'sample-market-a',
    best_bid: 0.48,
    best_ask: 0.52,
    mid: 0.5,
    spread: 0.04,
    updated_at: Math.floor(Date.now() / 1000),
  },
]

const sampleTrades: Trade[] = [
  {
    token_id: 'tok-a',
    side: 'BUY',
    size: 12.5,
    price: 0.5,
    timestamp: Math.floor(Date.now() / 1000) - 5,
  } as Trade,
  {
    token_id: 'tok-a',
    side: 'SELL',
    size: 4.2,
    price: 0.49,
    timestamp: Math.floor(Date.now() / 1000) - 3,
  } as Trade,
]

// ── Tests ───────────────────────────────────────────────────────────────────
describe('OrderFlowPanel', () => {
  beforeEach(() => {
    global.fetch = mockDepthOk()
  })

  it('renders the panel container without crashing', async () => {
    const { container } = render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    expect(container.firstChild).toBeTruthy()
    // The panel exposes a region role + data-testid for screen readers.
    expect(screen.getByTestId('order-flow-panel')).toBeTruthy()
    expect(screen.getByRole('region', { name: /order flow panel/i })).toBeTruthy()
  })

  it('renders the panel header (token selector + window buttons + stats)', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    expect(screen.getByTestId('order-flow-panel-header')).toBeTruthy()
    // Token label.
    expect(screen.getByText('Token')).toBeInTheDocument()
    // Token selector option.
    expect(screen.getByText('sample-market-a')).toBeInTheDocument()
    // All three window buttons render.
    expect(screen.getByTestId('order-flow-window-30s')).toBeInTheDocument()
    expect(screen.getByTestId('order-flow-window-1m')).toBeInTheDocument()
    expect(screen.getByTestId('order-flow-window-5m')).toBeInTheDocument()
  })

  it('renders the order-flow chart card heading', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    expect(
      screen.getByText(/Order Flow — buys vs sells/i),
    ).toBeInTheDocument()
  })

  it('renders the imbalance + tape card headings', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    expect(screen.getByText('Bid / Ask Imbalance')).toBeInTheDocument()
    expect(screen.getByText('Time & Sales')).toBeInTheDocument()
  })

  it('renders the LIVE badge when isRealtime is true', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    const badge = screen.getByTestId('order-flow-realtime-badge')
    expect(badge).toHaveTextContent('LIVE')
  })

  it('renders the POLL badge when isRealtime is false', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={false}
      />,
    )
    const badge = screen.getByTestId('order-flow-realtime-badge')
    expect(badge).toHaveTextContent('POLL')
  })

  it('renders the "No markets available" placeholder when orderBooks is empty', async () => {
    render(
      <OrderFlowPanel trades={[]} orderBooks={[]} isRealtime={true} />,
    )
    expect(screen.getByText('No markets available')).toBeInTheDocument()
  })

  it('renders without crashing with empty trades + empty books', async () => {
    const { container } = render(
      <OrderFlowPanel trades={[]} orderBooks={[]} isRealtime={false} />,
    )
    expect(container.firstChild).toBeTruthy()
    // Tape-speed stat renders with a "0/min" value (zero trades → 0/min).
    // The empty-state tape card also renders a "0/min" elsewhere, so use
    // getAllByText and assert at least one match.
    const tapeMatches = screen.getAllByText('0/min')
    expect(tapeMatches.length).toBeGreaterThanOrEqual(1)
  })

  it('surfaces the cumulative Δ stat (signed, one decimal) for BUY-heavy flow', async () => {
    // BUY size 12.5 − SELL size 4.2 = +8.3 (one decimal place).
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    // The Δ badge shows "+8.3".
    expect(screen.getByText('+8.3')).toBeInTheDocument()
  })

  it('fires a depth fetch for the selected token on mount', async () => {
    render(
      <OrderFlowPanel
        trades={sampleTrades}
        orderBooks={sampleOrderBooks}
        isRealtime={true}
      />,
    )
    await waitFor(() => {
      const depthCalls = vi
        .mocked(global.fetch)
        .mock.calls.filter(([url]) => typeof url === 'string' && url.includes('/api/depth/tok-a'))
      expect(depthCalls.length).toBeGreaterThanOrEqual(1)
    })
  })
})
