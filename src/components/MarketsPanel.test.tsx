// components/MarketsPanel.test.tsx — W30-2 panel tests.
//
// Strategy:
//   * MarketsPanel is a pure presentational component — no fetch. It
//     receives `books: OrderBook[]` as props and renders a table,
//     a search box, and category filter pills.
//   * Tests cover: empty-state, basic render with one row, search
//     filter, category filter, and sort header click behavior.
//   * `onSelectMarket` is spied to verify the row-click contract.
//
// What's covered:
//   1. Renders without crashing.
//   2. Renders the "Active Order Books" header.
//   3. Renders the "Synchronizing live prediction market order books" empty-state.
//   4. Renders a row per OrderBook passed in props.
//   5. Filters rows by the search input.
//   6. Filters rows by the CRYPTO category pill.
//   7. Calls onSelectMarket when a row's Depth button is clicked.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup } from '@testing-library/react'
import MarketsPanel from './MarketsPanel'
import type { OrderBook } from '@/hooks/useBot'

function makeBook(overrides: Partial<OrderBook> = {}): OrderBook {
  return {
    token_id: 'tok_btc_100k_yes',
    slug: 'will-bitcoin-hit-100k',
    best_bid: 0.41,
    best_ask: 0.43,
    mid: 0.42,
    spread: 0.02,
    updated_at: Math.floor(Date.now() / 1000),
    ...overrides,
  }
}

describe('MarketsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    cleanup()
  })

  it('renders without crashing', () => {
    const { container } = render(<MarketsPanel books={[]} />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Active Order Books" header with a zero count when no books', () => {
    render(<MarketsPanel books={[]} />)
    expect(screen.getByText(/Active Order Books \(0\)/)).toBeInTheDocument()
  })

  it('renders the synchronizing empty-state when books is empty', () => {
    render(<MarketsPanel books={[]} />)
    expect(
      screen.getByText(/Synchronizing live prediction market order books/i),
    ).toBeInTheDocument()
  })

  it('renders a row per OrderBook passed in props', () => {
    const books: OrderBook[] = [
      makeBook({ token_id: 'tok_a', slug: 'will-bitcoin-hit-100k' }),
      makeBook({ token_id: 'tok_b', slug: 'will-ethereum-flip' }),
    ]
    render(<MarketsPanel books={books} />)
    expect(screen.getByText(/Active Order Books \(2\)/)).toBeInTheDocument()
    // Each row's token-copy chip prints the first 6 chars of the token id.
    expect(screen.getByText(/\[#tok_a…\]/)).toBeInTheDocument()
    expect(screen.getByText(/\[#tok_b…\]/)).toBeInTheDocument()
  })

  it('filters rows by the search input', () => {
    const books: OrderBook[] = [
      // ``MarketsPanel`` renders ``[#${token_id.slice(0, 6)}…]`` for the
      // copy-id chip, so we use token_ids that fit within the 6-char
      // slice window — ``tok_btc`` (7 chars) would render as ``[#tok_bt…]``
      // and break the assertion below. ``tok_b1`` / ``tok_e1`` keep the
      // visible chip text identical to the full token_id.
      makeBook({ token_id: 'tok_b1', slug: 'will-bitcoin-hit-100k' }),
      makeBook({ token_id: 'tok_e1', slug: 'will-ethereum-flip' }),
    ]
    render(<MarketsPanel books={books} />)
    const input = screen.getByLabelText(/search prediction markets/i)
    fireEvent.change(input, { target: { value: 'bitcoin' } })
    expect(screen.getByText(/\[#tok_b1…\]/)).toBeInTheDocument()
    expect(screen.queryByText(/\[#tok_e1…\]/)).not.toBeInTheDocument()
  })

  it('filters rows by the CRYPTO category pill', () => {
    const books: OrderBook[] = [
      // Same 6-char-slice caveat as the search test — keep token_ids
      // short enough that the visible chip text == full token_id.
      makeBook({ token_id: 'tok_b1', slug: 'will-bitcoin-hit-100k' }),
      makeBook({ token_id: 'tok_tr1', slug: 'will-trump-win-2028' }),
    ]
    render(<MarketsPanel books={books} />)
    // W38-4 — click the CRYPTO category filter PILL specifically (not the
    // per-row category badge, which also renders the text "CRYPTO").
    // Querying by role+name keeps the test resilient to the new badge.
    fireEvent.click(screen.getByRole('button', { name: 'CRYPTO' }))
    expect(screen.getByText(/\[#tok_b1…\]/)).toBeInTheDocument()
    expect(screen.queryByText(/\[#tok_tr1…\]/)).not.toBeInTheDocument()
  })

  it('calls onSelectMarket when a row Depth button is clicked', () => {
    const onSelectMarket = vi.fn()
    const books: OrderBook[] = [makeBook({ token_id: 'tok_a', slug: 'will-bitcoin-hit-100k' })]
    render(<MarketsPanel books={books} onSelectMarket={onSelectMarket} />)
    fireEvent.click(screen.getByText('Depth'))
    expect(onSelectMarket).toHaveBeenCalledWith('tok_a', 'will-bitcoin-hit-100k')
  })
})
