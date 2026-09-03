// components/charts/TradeTape.test.tsx — Unit tests for the trade tape.
//
// Strategy:
//   • Verify the tape renders the most-recent trades at the top.
//   • Verify the per-minute rate stat (trades/min) counts trades within
//     the 60s window.
//   • Verify the pause-on-hover behaviour: when the pointer enters the
//     body region, the "PAUSED" badge appears; new trades arriving
//     while paused are NOT rendered until the pointer leaves.
//   • Verify the empty-state message renders when no trades are
//     provided.
//   • Verify the maxRows cap (extra trades drop off the bottom).
//   • Verify BUY/SELL rows are colour-coded via the `data-side` attr.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import TradeTape from './TradeTape'
import type { FlowTrade } from './OrderFlowChart'

const NOW = 1_700_000_000_000

// Helper — build N synthetic trades spaced 1s apart, ending at NOW.
function buildTrades(n: number, startSide: 'BUY' | 'SELL' = 'BUY'): FlowTrade[] {
  const out: FlowTrade[] = []
  for (let i = 0; i < n; i++) {
    out.push({
      timestamp: NOW - (n - 1 - i) * 1000,
      side: i % 2 === 0 ? startSide : startSide === 'BUY' ? 'SELL' : 'BUY',
      size: 10 + i,
      price: 0.5 + (i % 5) * 0.01,
    })
  }
  return out
}

describe('TradeTape', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders without crashing with a valid trade list', () => {
    render(<TradeTape trades={buildTrades(5)} now={NOW} />)
    expect(screen.getByTestId('trade-tape')).toBeInTheDocument()
  })

  it('renders the empty-state message when no trades are provided', () => {
    render(<TradeTape trades={[]} now={NOW} />)
    expect(screen.getByTestId('trade-tape-empty')).toBeInTheDocument()
    expect(screen.getByText('No trades yet')).toBeInTheDocument()
  })

  it('renders one row per trade up to maxRows', () => {
    const trades = buildTrades(5)
    render(<TradeTape trades={trades} now={NOW} maxRows={5} />)
    const rows = screen.getAllByTestId('trade-tape-row')
    expect(rows.length).toBe(5)
  })

  it('caps the rendered rows at maxRows (drops oldest beyond the cap)', () => {
    const trades = buildTrades(10)
    render(<TradeTape trades={trades} now={NOW} maxRows={3} />)
    const rows = screen.getAllByTestId('trade-tape-row')
    expect(rows.length).toBe(3)
  })

  it('renders trades newest-first (top of the list)', () => {
    // Build 3 trades with distinct sizes so we can identify them by size.
    const trades: FlowTrade[] = [
      { timestamp: NOW - 30_000, side: 'BUY', size: 100, price: 0.50 }, // oldest
      { timestamp: NOW - 20_000, side: 'BUY', size: 200, price: 0.51 },
      { timestamp: NOW - 10_000, side: 'SELL', size: 300, price: 0.52 }, // newest
    ]
    render(<TradeTape trades={trades} now={NOW} />)
    const rows = screen.getAllByTestId('trade-tape-row')
    // The first row should be the newest trade (size 300).
    expect(rows[0].textContent).toContain('300.00')
    // The last row should be the oldest trade (size 100).
    expect(rows[rows.length - 1].textContent).toContain('100.00')
  })

  it('colour-codes rows by side via the data-side attribute', () => {
    const trades: FlowTrade[] = [
      { timestamp: NOW - 10_000, side: 'BUY', size: 10, price: 0.5 },
      { timestamp: NOW - 5_000, side: 'SELL', size: 5, price: 0.49 },
    ]
    render(<TradeTape trades={trades} now={NOW} />)
    const rows = screen.getAllByTestId('trade-tape-row')
    expect(rows[0].getAttribute('data-side')).toBe('SELL')
    expect(rows[1].getAttribute('data-side')).toBe('BUY')
  })

  it('renders BUY and SELL labels in the rows', () => {
    const trades: FlowTrade[] = [
      { timestamp: NOW - 10_000, side: 'BUY', size: 10, price: 0.5 },
      { timestamp: NOW - 5_000, side: 'SELL', size: 5, price: 0.49 },
    ]
    render(<TradeTape trades={trades} now={NOW} />)
    expect(screen.getByText('BUY')).toBeInTheDocument()
    expect(screen.getByText('SELL')).toBeInTheDocument()
  })

  it('shows the per-minute rate (trades/min) in the header', () => {
    // 5 trades, all within 60s of NOW.
    const trades = buildTrades(5)
    render(<TradeTape trades={trades} now={NOW} />)
    const header = screen.getByTestId('trade-tape-header')
    expect(header.textContent).toContain('5/min')
  })

  it('excludes trades older than the 60s rate window from the per-minute count', () => {
    const trades: FlowTrade[] = [
      { timestamp: NOW - 120_000, side: 'BUY', size: 10, price: 0.5 }, // 2m ago
      { timestamp: NOW - 30_000, side: 'BUY', size: 5, price: 0.5 }, // 30s ago
      { timestamp: NOW - 5_000, side: 'SELL', size: 3, price: 0.5 }, // 5s ago
    ]
    render(<TradeTape trades={trades} now={NOW} />)
    const header = screen.getByTestId('trade-tape-header')
    // 2 trades within 60s (NOT the 2m-ago one).
    expect(header.textContent).toContain('2/min')
  })

  it('pauses on hover (renders PAUSED badge)', () => {
    render(<TradeTape trades={buildTrades(3)} now={NOW} />)
    // Initially not paused.
    expect(screen.queryByTestId('trade-tape-paused')).toBeNull()

    // Hover over the body.
    fireEvent.mouseEnter(screen.getByTestId('trade-tape-body'))
    expect(screen.getByTestId('trade-tape-paused')).toBeInTheDocument()

    // Move out — paused badge disappears.
    fireEvent.mouseLeave(screen.getByTestId('trade-tape-body'))
    expect(screen.queryByTestId('trade-tape-paused')).toBeNull()
  })

  it('does NOT update the rendered rows while paused', () => {
    // Start with 3 trades.
    const { rerender } = render(<TradeTape trades={buildTrades(3)} now={NOW} />)
    expect(screen.getAllByTestId('trade-tape-row').length).toBe(3)

    // Pause.
    fireEvent.mouseEnter(screen.getByTestId('trade-tape-body'))
    expect(screen.getByTestId('trade-tape-paused')).toBeInTheDocument()

    // Add 3 more trades. While paused, the visible list should NOT grow.
    rerender(<TradeTape trades={buildTrades(6)} now={NOW} />)
    expect(screen.getAllByTestId('trade-tape-row').length).toBe(3)

    // Unpause — the latest snapshot (6 trades) renders.
    fireEvent.mouseLeave(screen.getByTestId('trade-tape-body'))
    // The render happens via the effect on the next tick; force a
    // re-render via the same prop to flush the state.
    rerender(<TradeTape trades={buildTrades(6)} now={NOW} />)
    // After unpause + re-render, the tape should show up to maxRows (40)
    // of the 6 trades — i.e. 6 rows.
    expect(screen.getAllByTestId('trade-tape-row').length).toBe(6)
  })

  it('dedupes identical trade snapshots (no re-render flicker)', () => {
    const trades = buildTrades(3)
    const { rerender } = render(<TradeTape trades={trades} now={NOW} />)
    expect(screen.getAllByTestId('trade-tape-row').length).toBe(3)

    // Re-render with the SAME trades array — the row count shouldn't change.
    // (The dedupe guards against the polling fetch returning an unchanged
    // snapshot every 2s and re-triggering the framer-motion enter animation.)
    rerender(<TradeTape trades={trades} now={NOW} />)
    expect(screen.getAllByTestId('trade-tape-row').length).toBe(3)
  })

  it('skips trades with NaN timestamps', () => {
    const trades: FlowTrade[] = [
      { timestamp: NaN, side: 'BUY', size: 10, price: 0.5 },
      { timestamp: NOW - 5_000, side: 'BUY', size: 5, price: 0.5 },
    ]
    render(<TradeTape trades={trades} now={NOW} />)
    const rows = screen.getAllByTestId('trade-tape-row')
    expect(rows.length).toBe(1)
  })

  it('exposes an aria-label summarising the tape state', () => {
    render(<TradeTape trades={buildTrades(3)} now={NOW} />)
    const root = screen.getByTestId('trade-tape')
    const label = root.getAttribute('aria-label') ?? ''
    expect(label).toContain('Trade tape')
    expect(label).toContain('3 prints')
  })

  it('includes "paused" in the aria-label when paused', () => {
    render(<TradeTape trades={buildTrades(2)} now={NOW} />)
    fireEvent.mouseEnter(screen.getByTestId('trade-tape-body'))
    const root = screen.getByTestId('trade-tape')
    const label = root.getAttribute('aria-label') ?? ''
    expect(label).toContain('paused')
  })

  it('renders the column headers (Time / Side / Price / Size)', () => {
    render(<TradeTape trades={buildTrades(2)} now={NOW} />)
    // The column headers are rendered as small uppercase labels.
    const tape = screen.getByTestId('trade-tape')
    expect(tape.textContent).toContain('Time')
    expect(tape.textContent).toContain('Side')
    expect(tape.textContent).toContain('Price')
    expect(tape.textContent).toContain('Size')
  })

  it('respects the height prop (body maxHeight = height − 70)', () => {
    render(<TradeTape trades={buildTrades(2)} now={NOW} height={200} />)
    const body = screen.getByTestId('trade-tape-body')
    expect((body as HTMLElement).style.maxHeight).toBe('130px')
  })
})
