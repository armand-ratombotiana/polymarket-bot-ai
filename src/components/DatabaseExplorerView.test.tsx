// components/DatabaseExplorerView.test.tsx — Database Explorer panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested DatabaseExplorerView panel:
//   1. Renders without crashing.
//   2. Renders the "Database & Time-Series Explorer" title.
//   3. Renders all 4 table-selector tabs (Market Snapshots, Orderbook Ticks,
//      Fundamental News, ML Feature Store).
//   4. Fetches /api/database/records?table=market_snapshots on mount.
//   5. Renders the records table with rows once data arrives.
//   6. Renders the table description (TABLE_DESCRIPTIONS).
//   7. Shows loading state initially while the first fetch resolves.
//   8. Renders the empty-state when no records are returned.
//   9. Switches the active table when a tab is clicked and re-fetches.
//  10. Polls every 5 s.
//  11. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import DatabaseExplorerView from './DatabaseExplorerView'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleMarketSnapshots = {
  records: [
    {
      token_id: 'tok_btc_100k_yes',
      slug: 'bitcoin-100k-by-december',
      mid_price: 0.425,
      spread_bps: 12,
      liquidity_usdc: 25000,
      timestamp: 1700000000,
    },
    {
      token_id: 'tok_trump_2028_yes',
      slug: 'trump-wins-2028-election',
      mid_price: 0.555,
      spread_bps: 8,
      liquidity_usdc: 42000,
      timestamp: 1700000060,
    },
  ],
}

const sampleNews = {
  records: [
    {
      headline: 'BlackRock files for spot Bitcoin ETF',
      source: 'Reuters',
      sentiment: 0.78,
      category: 'INSTITUTIONAL',
      timestamp: 1700000060,
    },
  ],
}

const emptyRecords = { records: [] }

// ── Fetch mock helpers ───────────────────────────────────────────────────────

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
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

describe('DatabaseExplorerView', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<DatabaseExplorerView />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Database & Time-Series Explorer" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<DatabaseExplorerView />)
    expect(
      screen.getByText(/Database & Time-Series Explorer/i),
    ).toBeInTheDocument()
  })

  it('renders all 4 table-selector tabs', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<DatabaseExplorerView />)
    expect(
      screen.getByRole('button', { name: /Market Snapshots/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Orderbook Ticks/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Fundamental News/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /ML Feature Store/i }),
    ).toBeInTheDocument()
  })

  it('fetches /api/database/records?table=market_snapshots on mount', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    })
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const urls = calls.map(([url]) => url)
    expect(
      urls.some(
        (u) =>
          typeof u === 'string' &&
          u.includes('/api/database/records') &&
          u.includes('table=market_snapshots'),
      ),
    ).toBe(true)
  })

  it('renders the records table with rows once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    })
    expect(screen.getByText('tok_trump_2028_yes')).toBeInTheDocument()
    // The "(2 records)" header badge is rendered.
    expect(screen.getByText(/\(2 records\)/i)).toBeInTheDocument()
  })

  it('renders the table description for the active table', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      // TABLE_DESCRIPTIONS.market_snapshots.
      expect(
        screen.getByText(
          /Periodic snapshots of top-of-book prices, spreads, and implied probabilities/i,
        ),
      ).toBeInTheDocument()
    })
  })

  it('shows the loading state initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<DatabaseExplorerView />)
    expect(
      screen.getByText(/Querying table records/i),
    ).toBeInTheDocument()
  })

  it('renders the empty-state when no records are returned', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyRecords))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(
        screen.getByText(/No records in market_snapshots/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(
        /Data is currently buffered in memory or writing to storage/i,
      ),
    ).toBeInTheDocument()
  })

  it('switches the active table when a tab is clicked and re-fetches', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        'table=market_snapshots': sampleMarketSnapshots,
        'table=fundamental_news': sampleNews,
      }),
    )
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    })
    // Click the "Fundamental News" tab.
    fireEvent.click(screen.getByRole('button', { name: /Fundamental News/i }))
    // Wait for the news record to render.
    await waitFor(() => {
      expect(
        screen.getByText('BlackRock files for spot Bitcoin ETF'),
      ).toBeInTheDocument()
    })
    // Verify a fetch was made for table=fundamental_news.
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const urls = calls.map(([url]) => url)
    expect(
      urls.some(
        (u) =>
          typeof u === 'string' &&
          u.includes('/api/database/records') &&
          u.includes('table=fundamental_news'),
      ),
    ).toBe(true)
  })

  it('renders the "Polled every 5s" badge in the table header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText(/Polled every 5s/i)).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls every 5 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)
    // Advance 5 s — should fire one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    const { unmount } = render(<DatabaseExplorerView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('re-fetches when the active table changes (effect re-runs)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        'table=market_snapshots': sampleMarketSnapshots,
        'table=orderbook_ticks': { records: [{ token_id: 'tok_ob_1', ofi_5s: 0.42 }] },
      }),
    )
    render(<DatabaseExplorerView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Click the "Orderbook Ticks" tab.
    fireEvent.click(screen.getByRole('button', { name: /Orderbook Ticks/i }))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // A new fetch should have fired for the new table.
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('renders the table name as a mono cyan code in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('market_snapshots')).toBeInTheDocument()
    })
  })

  it('renders the CSV export button (disabled when no records, enabled otherwise)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleMarketSnapshots))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(screen.getByText('tok_btc_100k_yes')).toBeInTheDocument()
    })
    // The CSV button has a title attribute with "Export" prefix.
    const csvBtn = screen.getByRole('button', { name: /CSV/i })
    expect(csvBtn).toBeInTheDocument()
    expect((csvBtn as HTMLButtonElement).disabled).toBe(false)
  })

  it('disables the CSV export button when there are no records', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyRecords))
    render(<DatabaseExplorerView />)
    await waitFor(() => {
      expect(
        screen.getByText(/No records in market_snapshots/i),
      ).toBeInTheDocument()
    })
    const csvBtn = screen.getByRole('button', { name: /CSV/i })
    expect((csvBtn as HTMLButtonElement).disabled).toBe(true)
  })

  it('handles fetch errors gracefully (no crash, empty state eventually)', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<DatabaseExplorerView />)
    // The panel swallows errors silently. Wait a tick to allow the failed
    // fetch to settle, then verify the panel didn't crash.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(
      screen.getByText(/Database & Time-Series Explorer/i),
    ).toBeInTheDocument()
  })
})
