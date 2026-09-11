// components/MarketScreener.test.tsx — Market screener panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested MarketScreener panel:
//   1. Renders without crashing.
//   2. Renders the "Prediction Market Screener" title.
//   3. Shows loading state initially while the first fetch resolves.
//   4. Renders the markets table with rows when /api/markets returns
//      a populated list.
//   5. Renders the "No markets found" empty state when the response
//      is an empty array.
//   6. Handles fetch errors gracefully — shows the danger banner with
//      a retry affordance.
//   7. Category chips filter the rendered markets.
//   8. Search form fires a fetch with the ?search= query parameter.
//   9. Clear button resets the search filter.
//  10. Trade / Depth row button invokes onQuickTrade when supplied.
//  11. Polls /api/markets every 30 s and updates the rendered rows.
//  12. Unmounts cleanly without leaking setState.
//
// Strategy mirrors RateLimitPanel.test.tsx + DatabaseStatusPanel.test.tsx:
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • For initial-render assertions use real timers + `waitFor`.
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })`.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import MarketScreener from './MarketScreener'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleMarkets = {
  markets: [
    {
      slug: 'bitcoin-100k-by-december',
      groupItemTitle: 'Bitcoin to $100k by December',
      category: 'CRYPTO',
      volume24hr: 125000.5,
      liquidity: 28000,
      tokens: [{ token_id: 'tok_btc_100k_yes', outcome: 'YES' }],
    },
    {
      slug: 'trump-wins-2028-election',
      groupItemTitle: 'Trump wins 2028 presidential election',
      category: 'POLITICS',
      volume24hr: 88000,
      liquidity: 42000,
      tokens: [{ token_id: 'tok_trump_2028_yes', outcome: 'YES' }],
    },
    {
      slug: 'fed-rate-cut-march-meeting',
      groupItemTitle: 'Fed cuts rates at March FOMC',
      category: 'ECONOMY',
      volume24hr: 65000,
      liquidity: 18000,
      tokens: [{ token_id: 'tok_fed_cut_yes', outcome: 'YES' }],
    },
  ],
}

const emptyMarkets = { markets: [] }

// ── Fetch mock helpers ───────────────────────────────────────────────────────

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

function mockFetchNotOk(status = 500) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText: 'Internal Server Error',
    json: async () => ({}),
  } as Response)
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('MarketScreener', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<MarketScreener />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Prediction Market Screener" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<MarketScreener />)
    expect(
      screen.getByText(/Prediction Market Screener/i),
    ).toBeInTheDocument()
  })

  it('shows the loading state initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<MarketScreener />)
    expect(
      screen.getByText(/Scanning Polymarket prediction markets/i),
    ).toBeInTheDocument()
  })

  it('renders the markets table with rows once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText('Trump wins 2028 presidential election'),
    ).toBeInTheDocument()
    expect(
      screen.getByText('Fed cuts rates at March FOMC'),
    ).toBeInTheDocument()
    // Header badge "3 of 3 Markets" is rendered.
    expect(screen.getByText(/3 of 3 Markets/i)).toBeInTheDocument()
  })

  it('renders the "No markets found" empty state when the response is empty', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(screen.getByText(/No markets found/i)).toBeInTheDocument()
    })
  })

  it('handles fetch errors gracefully — shows the danger banner with a Retry button', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load markets \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('button', { name: /retry/i }),
    ).toBeInTheDocument()
  })

  it('handles network errors gracefully — shows the "Network error" banner', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('filters markets by category when a chip is selected', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    // Click the "CRYPTO" chip.
    fireEvent.click(screen.getByRole('button', { name: 'CRYPTO' }))
    // BTC market should remain visible; the other two should be filtered out.
    expect(
      screen.getByText('Bitcoin to $100k by December'),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Trump wins 2028 presidential election'),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText('Fed cuts rates at March FOMC'),
    ).not.toBeInTheDocument()
    // Header badge shows "1 of 3 Markets".
    expect(screen.getByText(/1 of 3 Markets/i)).toBeInTheDocument()
  })

  it('passes the ?search= query parameter when the search form is submitted', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    // Type a search query into the search input.
    const input = screen.getByLabelText(/Search prediction market events/i)
    fireEvent.change(input, { target: { value: 'bitcoin' } })
    // Submit the search form.
    fireEvent.click(screen.getByRole('button', { name: /Search/i }))
    // The latest fetch URL should include ?search=bitcoin.
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const searchCall = calls
      .map(([url]) => url)
      .find((url) => typeof url === 'string' && url.includes('search=bitcoin'))
    expect(searchCall).toBeTruthy()
  })

  it('shows a Clear button when search has a value, and clears the filter when clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    // Initially no Clear button.
    expect(screen.queryByRole('button', { name: /clear/i })).not.toBeInTheDocument()
    // Type a query.
    fireEvent.change(
      screen.getByLabelText(/Search prediction market events/i),
      { target: { value: 'bitcoin' } },
    )
    // Clear button now appears.
    const clearBtn = screen.getByRole('button', { name: /clear/i })
    expect(clearBtn).toBeInTheDocument()
    // Click it.
    fireEvent.click(clearBtn)
    // Search input should now be empty.
    expect(
      (screen.getByLabelText(/Search prediction market events/i) as HTMLInputElement).value,
    ).toBe('')
  })

  it('fires onQuickTrade with the token_id and slug when the Trade / Depth button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    const onQuickTrade = vi.fn()
    render(<MarketScreener onQuickTrade={onQuickTrade} />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    const tradeBtn = screen.getByRole('button', {
      name: /Open depth and trade ticket for Bitcoin to \$100k by December/i,
    })
    fireEvent.click(tradeBtn)
    expect(onQuickTrade).toHaveBeenCalledTimes(1)
    expect(onQuickTrade).toHaveBeenCalledWith(
      'tok_btc_100k_yes',
      'bitcoin-100k-by-december',
    )
  })

  it('falls back to onSelectMarket when onQuickTrade is not provided', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    const onSelectMarket = vi.fn()
    render(<MarketScreener onSelectMarket={onSelectMarket} />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    const tradeBtn = screen.getByRole('button', {
      name: /Open depth and trade ticket for Bitcoin to \$100k by December/i,
    })
    fireEvent.click(tradeBtn)
    expect(onSelectMarket).toHaveBeenCalledTimes(1)
    expect(onSelectMarket).toHaveBeenCalledWith(
      'tok_btc_100k_yes',
      'bitcoin-100k-by-december',
    )
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText('Bitcoin to $100k by December'),
      ).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls /api/markets every 30 s and updates the rendered rows', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText('Bitcoin to $100k by December'),
    ).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)

    // Swap the response to include a new market.
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({
        markets: [
          ...sampleMarkets.markets,
          {
            slug: 'eth-merge-success',
            groupItemTitle: 'Ethereum merge succeeds',
            category: 'CRYPTO',
            volume24hr: 5000,
            liquidity: 1000,
            tokens: [{ token_id: 'tok_eth_merge_yes', outcome: 'YES' }],
          },
        ],
      }),
    )
    // Advance 30 s — should fire one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
    expect(
      screen.getByText('Ethereum merge succeeds'),
    ).toBeInTheDocument()
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    const { unmount } = render(<MarketScreener />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText('Bitcoin to $100k by December'),
    ).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders all six category chips', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<MarketScreener />)
    for (const chip of ['ALL', 'CRYPTO', 'POLITICS', 'SPORTS', 'ECONOMY', 'TECH']) {
      expect(screen.getByRole('button', { name: chip })).toBeInTheDocument()
    }
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the catch block only set a static "Network error" string.
  // The W22-1 fix surfaces the underlying error message AND adds a
  // Dismiss control so the trader can clear the banner without retrying.

  it('W22-1: surfaces the underlying error message instead of a static "Network error" string', async () => {
    vi.mocked(fetch).mockRejectedValueOnce(
      new Error('Specific backend error: ECONNRESET'),
    )
    // Subsequent calls (polling) succeed so the test isn't racy.
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarkets))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText(/Specific backend error: ECONNRESET/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: shows a Dismiss button alongside the Retry button in the error banner', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load markets \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('button', { name: /retry/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /dismiss error/i }),
    ).toBeInTheDocument()
  })

  it('W22-1: dismisses the error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load markets/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /dismiss error/i }))
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load markets/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: logs the fetch error to console.error (silent swallow removed)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<MarketScreener />)
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining('[MarketScreener]'),
        expect.any(Error),
      )
    })
    consoleErrorSpy.mockRestore()
  })
})
