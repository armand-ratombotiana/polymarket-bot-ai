// components/ArbitrageMatrixView.test.tsx — Arbitrage matrix panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested ArbitrageMatrixView panel:
//   1. Renders without crashing.
//   2. Renders the "High-Frequency Binary Dutch-Book Arbitrage Scanner" title.
//   3. Shows the loading state initially while the first fetch resolves.
//   4. Renders the opportunities table with rows when data arrives.
//   5. Renders the aggregate KPI strip (Active Arbs, Max Edge, Avg Net ROI).
//   6. Filters opportunities by search query.
//   7. Filters opportunities by min-BPS slider.
//   8. Renders the empty-state when no opportunities are returned.
//   9. Fires POST /api/arbitrage/execute when the Execute Arb button is clicked.
//  10. Surfaces a success banner after a successful execution.
//  11. Surfaces a failure banner after a failed execution.
//  12. Polls /api/arbitrage/opportunities every 2.5 s.
//  13. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import ArbitrageMatrixView from './ArbitrageMatrixView'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleOpps = {
  opportunities: [
    {
      token_id_yes: 'tok_yes_btc_100k',
      token_id_no: 'tok_no_btc_100k',
      slug: 'bitcoin-100k-by-december',
      category: 'CRYPTO',
      yes_ask: 0.4825,
      no_ask: 0.4850,
      total_cost: 0.9675,
      gross_profit_bps: 32,
      net_roi_pct: 2.45,
      max_executable_size_usdc: 5.0,
      status: 'OPEN',
    },
    {
      token_id_yes: 'tok_yes_trump_2028',
      token_id_no: 'tok_no_trump_2028',
      slug: 'trump-wins-2028-election',
      category: 'POLITICS',
      yes_ask: 0.4200,
      no_ask: 0.5550,
      total_cost: 0.9750,
      gross_profit_bps: 20,
      net_roi_pct: 1.85,
      max_executable_size_usdc: 4.5,
      status: 'OPEN',
    },
  ],
}

const emptyOpps = { opportunities: [] }

// ── Fetch mock helpers ───────────────────────────────────────────────────────

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

function mockFetchRouteGetPost(getPayload: unknown, postPayload: unknown) {
  return vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => postPayload,
      } as Response)
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => getPayload,
    } as Response)
  })
}

function mockFetchRouteByUrl(routes: Record<string, unknown>) {
  return vi.fn().mockImplementation((input: string) => {
    for (const [fragment, payload] of Object.entries(routes)) {
      if (typeof input === 'string' && input.includes(fragment)) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => payload,
        } as Response)
      }
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response)
  })
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('ArbitrageMatrixView', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<ArbitrageMatrixView />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "High-Frequency Binary Dutch-Book Arbitrage Scanner" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<ArbitrageMatrixView />)
    expect(
      screen.getByText(/High-Frequency Binary Dutch-Book Arbitrage Scanner/i),
    ).toBeInTheDocument()
  })

  it('renders the "Paper Mode · $3 Cap" badge', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<ArbitrageMatrixView />)
    expect(screen.getByText(/Paper Mode/i)).toBeInTheDocument()
  })

  it('shows the loading state initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<ArbitrageMatrixView />)
    expect(
      screen.getByText(/Scanning synchronized binary order books/i),
    ).toBeInTheDocument()
  })

  it('renders the opportunities table with rows once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      // The slug is parsed by formatHierarchicalMarket into:
      //   eventTitle: "BITCOIN"  question: "100k By December"
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/Wins 2028 Election/i)).toBeInTheDocument()
  })

  it('renders the aggregate KPI strip (Active Arbs, Max Edge, Avg Net ROI)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      // Active Arbs KPI label.
      expect(screen.getByText(/Active Arbs:/i)).toBeInTheDocument()
    })
    // Max Edge KPI — 32 bps (gross_profit_bps of the higher opportunity).
    expect(screen.getByText(/Max Edge:/i)).toBeInTheDocument()
    // Avg Net ROI KPI — (2.45 + 1.85) / 2 = 2.15%.
    expect(screen.getByText(/Avg Net ROI:/i)).toBeInTheDocument()
    // The "+32 bps" value appears in BOTH the KPI strip AND the first
    // row's Gross Edge column — use getAllByText.
    const edgeMatches = screen.getAllByText(/\+32 bps/i)
    expect(edgeMatches.length).toBeGreaterThanOrEqual(1)
    const roiMatches = screen.getAllByText(/\+2\.15%/i)
    expect(roiMatches.length).toBeGreaterThanOrEqual(1)
  })

  it('renders the empty-state when no opportunities are returned', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(
        screen.getByText(/No arbitrage discrepancies found/i),
      ).toBeInTheDocument()
    })
  })

  it('filters opportunities by search query', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const input = screen.getByPlaceholderText(/Filter arbitrage by market name/i)
    fireEvent.change(input, { target: { value: 'trump' } })
    expect(screen.getByText(/Wins 2028 Election/i)).toBeInTheDocument()
    expect(screen.queryByText(/100k By December/i)).not.toBeInTheDocument()
  })

  it('filters opportunities by min-BPS slider', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    // Initial minBps = 10, both opps (32 bps and 20 bps) should be visible.
    expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    expect(screen.getByText(/Wins 2028 Election/i)).toBeInTheDocument()
    // Raise minBps to 25 — only the 32-bps opp should remain.
    const slider = screen.getByRole('slider')
    fireEvent.change(slider, { target: { value: '25' } })
    expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    expect(
      screen.queryByText(/Wins 2028 Election/i),
    ).not.toBeInTheDocument()
  })

  it('fires POST /api/arbitrage/execute when the Execute Arb button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(sampleOpps, {
        legs: [
          { leg: 'YES', status: 'FILLED' },
          { leg: 'NO', status: 'FILLED' },
        ],
      }),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    // Click the first Execute Arb button (matched by aria-label prefix).
    const buttons = screen.getAllByRole('button', {
      name: /Execute paper arbitrage on/i,
    })
    fireEvent.click(buttons[0])
    // Wait for the POST to fire.
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const postCalls = calls.filter(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/arbitrage/execute') &&
          init?.method === 'POST',
      )
      expect(postCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('shows a success banner after a successful arb execution', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(sampleOpps, {
        legs: [
          { leg: 'YES', status: 'FILLED' },
          { leg: 'NO', status: 'FILLED' },
        ],
      }),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const buttons = screen.getAllByRole('button', {
      name: /Execute paper arbitrage on/i,
    })
    fireEvent.click(buttons[0])
    await waitFor(() => {
      expect(
        screen.getByText(/Arbitrage legs successfully executed/i),
      ).toBeInTheDocument()
    })
  })

  it('shows a failure banner when the execute endpoint returns an error status', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          return Promise.resolve({
            ok: false,
            status: 423,
            json: async () => ({ detail: 'Risk engine blocked execution' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => sampleOpps,
        } as Response)
      }),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const buttons = screen.getAllByRole('button', {
      name: /Execute paper arbitrage on/i,
    })
    fireEvent.click(buttons[0])
    await waitFor(() => {
      expect(
        screen.getByText(/Risk engine blocked execution/i),
      ).toBeInTheDocument()
    })
  })

  it('shows a failure banner when the execute fetch throws a network error', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          return Promise.reject(new Error('Network error: ECONNRESET'))
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => sampleOpps,
        } as Response)
      }),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const buttons = screen.getAllByRole('button', {
      name: /Execute paper arbitrage on/i,
    })
    fireEvent.click(buttons[0])
    await waitFor(() => {
      expect(
        screen.getByText(/Execution network request failed/i),
      ).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls /api/arbitrage/opportunities every 2.5 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)
    // Advance 2.5 s — should fire one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2_500)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    const { unmount } = render(<ArbitrageMatrixView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('fires Scan Now button click — triggers a manual fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    const callsBefore = vi.mocked(fetch).mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: /Scan Now/i }))
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
    })
  })

  it('renders the Verified Dutch-Book Pairs card header with the filtered count', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      // Card header shows "Verified Dutch-Book Pairs (2)" — filteredOpps.length.
      expect(
        screen.getByText(/Verified Dutch-Book Pairs \(2\)/i),
      ).toBeInTheDocument()
    })
  })

  it('fires onSelectMarket callback when a row market-cell is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    const onSelectMarket = vi.fn()
    render(<ArbitrageMatrixView onSelectMarket={onSelectMarket} />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    // Click the market question cell (the first cell in the row).
    fireEvent.click(screen.getByText(/100k By December/i))
    expect(onSelectMarket).toHaveBeenCalledTimes(1)
    expect(onSelectMarket).toHaveBeenCalledWith({
      tokenId: 'tok_yes_btc_100k',
      slug: 'bitcoin-100k-by-december',
    })
  })

  it('renders without an onSelectMarket prop without crashing', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
    // Clicking the market question cell should NOT crash.
    fireEvent.click(screen.getByText(/100k By December/i))
  })

  it('renders arbitrage opportunities with the gross edge and net ROI values', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      // gross_profit_bps 32 → "+32 bps" (appears in KPI strip AND row).
      const edge32 = screen.getAllByText(/\+32 bps/i)
      expect(edge32.length).toBeGreaterThanOrEqual(1)
    })
    // net_roi_pct 2.45 → "+2.45%"
    const roi245 = screen.getAllByText(/\+2\.45%/i)
    expect(roi245.length).toBeGreaterThanOrEqual(1)
    // Second opp: 20 bps / 1.85%
    const edge20 = screen.getAllByText(/\+20 bps/i)
    expect(edge20.length).toBeGreaterThanOrEqual(1)
    const roi185 = screen.getAllByText(/\+1\.85%/i)
    expect(roi185.length).toBeGreaterThanOrEqual(1)
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed all fetch errors via
  // `} catch {}`. The W22-1 fix surfaces them via an inline dismissable
  // banner with a Retry button.

  it('W22-1: shows the fetch-error banner when /api/arbitrage/opportunities returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: async () => ({}),
      } as Response),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load arbitrage opportunities \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    // The banner has both Retry and Dismiss controls.
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /dismiss error/i })).toBeInTheDocument()
  })

  it('W22-1: shows the fetch-error banner when the fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: dismisses the fetch-error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: async () => ({}),
      } as Response),
    )
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load arbitrage opportunities/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /dismiss error/i }))
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load arbitrage opportunities/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: refetches when the Retry button in the fetch-error banner is clicked', async () => {
    // First fetch fails, then subsequent fetches (after Retry click) succeed.
    vi.mocked(fetch).mockImplementationOnce(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: async () => ({}),
      } as Response),
    )
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleOpps))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load arbitrage opportunities/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /retry/i }))
    await waitFor(() => {
      expect(screen.getByText(/100k By December/i)).toBeInTheDocument()
    })
  })

  it('W22-1: logs the fetch error to console.error (silent swallow removed)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNRESET'))
    render(<ArbitrageMatrixView />)
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining('[ArbitrageMatrixView]'),
        expect.any(Error),
      )
    })
    consoleErrorSpy.mockRestore()
  })
})
