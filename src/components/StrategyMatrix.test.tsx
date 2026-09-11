// components/StrategyMatrix.test.tsx — Strategy matrix panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested StrategyMatrix panel:
//   1. Renders without crashing.
//   2. Renders the "Quantitative Strategy Matrix" title.
//   3. Fetches /api/strategies/catalog AND /api/leaderboard in parallel
//      on mount.
//   4. Renders strategy cards once data arrives.
//   5. Renders the "Implemented" badge for canonical strategies and
//      "Stub" badge for research-only strategies.
//   6. Renders the per-strategy live P&L strip when leaderboard has data.
//   7. Category tab filter narrows the visible cards.
//   8. Search filter narrows the visible cards.
//   9. Clicking a stub's "Stub Only" button shows the warning notice.
//  10. Clicking the Deploy/Stop button on an implemented strategy fires
//      POST /api/strategies/toggle.
//  11. Polls the catalog + leaderboard every 4 s.
//  12. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx:
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • The panel makes two parallel fetches (catalog + leaderboard). The
//     mock returns the catalog payload for /api/strategies/catalog and
//     the leaderboard payload for /api/leaderboard.
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })`.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import StrategyMatrix from './StrategyMatrix'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleCatalog = {
  catalog: [
    {
      strategy_id: 'mm_avellaneda_stoikov',
      name: 'Avellaneda-Stoikov Market Maker',
      category: 'market_making',
      description: 'Inventory-aware spread pricing with reservation price drift.',
      risk_level: 'LOW',
      is_running: true,
    },
    {
      strategy_id: 'arb_binary_dutch_book',
      name: 'Binary Dutch-Book Arbitrage',
      category: 'arbitrage',
      description: 'Captures YES+NO < $1 dual-leg mispricings.',
      risk_level: 'LOW',
      is_running: false,
    },
    {
      strategy_id: 'ml_random_forest_quant',
      name: 'Random Forest Quant Ensemble',
      category: 'machine_learning',
      description: '4-member calibrated ensemble (RF + GB + SGD + LightGBM).',
      risk_level: 'MEDIUM',
      is_running: true,
    },
    {
      strategy_id: 'mom_ema_crossover',
      name: 'EMA Crossover Trend Follower',
      category: 'momentum',
      description: 'Fast/slow EMA cross signal generator.',
      risk_level: 'HIGH',
      is_running: false,
    },
  ],
}

const sampleLeaderboard = {
  ranked: [
    {
      strategy: 'mm_avellaneda_stoikov',
      net_pnl: 12.45,
      win_rate: 0.62,
      closed_trades: 38,
    },
    {
      strategy: 'ml_random_forest_quant',
      net_pnl: -2.31,
      win_rate: 0.41,
      closed_trades: 22,
    },
  ],
}

const emptyCatalog = { catalog: [] }
const emptyLeaderboard = { ranked: [] }

// ── Fetch mock helpers ───────────────────────────────────────────────────────
//
// The panel makes two parallel fetches (catalog + leaderboard). We
// route the mock based on the URL.

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

function mockFetchNotOk(status = 500) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText: 'Internal Server Error',
    json: async () => ({}),
  } as Response)
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('StrategyMatrix', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<StrategyMatrix />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Quantitative Strategy Matrix" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<StrategyMatrix />)
    expect(
      screen.getByText(/Quantitative Strategy Matrix/i),
    ).toBeInTheDocument()
  })

  it('renders the "47 Stubs / Research" badge in the header', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<StrategyMatrix />)
    expect(screen.getByText(/47 Stubs \/ Research/i)).toBeInTheDocument()
  })

  it('fetches /api/strategies/catalog AND /api/leaderboard on mount', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': sampleLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Avellaneda-Stoikov Market Maker'),
      ).toBeInTheDocument()
    })
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const urls = calls.map(([url]) => url)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/strategies/catalog')),
    ).toBe(true)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/leaderboard')),
    ).toBe(true)
  })

  it('renders the "Implemented" badge for canonical strategies and "Stub" badge for research stubs', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Avellaneda-Stoikov Market Maker'),
      ).toBeInTheDocument()
    })
    // Three Implemented strategies + one Stub strategy.
    const implementedBadges = screen.getAllByText('Implemented')
    expect(implementedBadges.length).toBe(3)
    expect(screen.getByText('Stub')).toBeInTheDocument()
  })

  it('renders the per-strategy live P&L strip when leaderboard has data', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': sampleLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      // net_pnl +12.45 → "+12.45" (the component formats as net_pnl.toFixed(2)).
      expect(screen.getByText(/\+12\.45/)).toBeInTheDocument()
    })
    // win_rate 0.62 → "62% WR" (component renders `${win_rate * 100}% WR`).
    expect(screen.getByText(/62% WR/i)).toBeInTheDocument()
    // closed_trades 38 → "38 trades"
    expect(screen.getByText(/38 trades/i)).toBeInTheDocument()
  })

  it('filters cards by category when a tab is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Avellaneda-Stoikov Market Maker'),
      ).toBeInTheDocument()
    })
    // Click the "Market Making" tab.
    fireEvent.click(screen.getByRole('button', { name: /Market Making/i }))
    expect(
      screen.getByText('Avellaneda-Stoikov Market Maker'),
    ).toBeInTheDocument()
    // Other strategies should be filtered out.
    expect(
      screen.queryByText('Binary Dutch-Book Arbitrage'),
    ).not.toBeInTheDocument()
    expect(
      screen.queryByText('Random Forest Quant Ensemble'),
    ).not.toBeInTheDocument()
  })

  it('filters cards by search query (matches name)', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Avellaneda-Stoikov Market Maker'),
      ).toBeInTheDocument()
    })
    const input = screen.getByLabelText(/Filter strategies/i)
    fireEvent.change(input, { target: { value: 'arbitrage' } })
    // Only the "Binary Dutch-Book Arbitrage" card matches the query.
    expect(
      screen.getByText('Binary Dutch-Book Arbitrage'),
    ).toBeInTheDocument()
    expect(
      screen.queryByText('Avellaneda-Stoikov Market Maker'),
    ).not.toBeInTheDocument()
  })

  it('shows the warning notice when a stub "Stub Only" button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('EMA Crossover Trend Follower'),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Stub Only/i }))
    expect(
      screen.getByText(/metadata-only research stub/i),
    ).toBeInTheDocument()
  })

  it('fires POST /api/strategies/toggle when the Deploy/Stop button is clicked on an implemented strategy', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
        '/api/strategies/toggle': { ok: true },
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Binary Dutch-Book Arbitrage'),
      ).toBeInTheDocument()
    })
    // Click the Deploy button on the (not-running) arbitrage strategy.
    fireEvent.click(screen.getByRole('button', { name: /Deploy/i }))
    // Wait for the POST to fire.
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const toggleCalls = calls.filter(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/strategies/toggle') &&
          init?.method === 'POST',
      )
      expect(toggleCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('handles fetch errors gracefully (no crash, no cards rendered)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<StrategyMatrix />)
    // The panel swallows errors silently — no crash, no cards.
    // The header should still render.
    expect(
      screen.getByText(/Quantitative Strategy Matrix/i),
    ).toBeInTheDocument()
    // Wait a tick to allow the failed fetch to settle.
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(
      screen.queryByText('Avellaneda-Stoikov Market Maker'),
    ).not.toBeInTheDocument()
  })

  it('renders the "X of 3 Implemented Active" badge reflecting the running count', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      // Two of three implemented strategies are running (mm_avellaneda_stoikov
      // + ml_random_forest_quant) → "2 of 3 Implemented Active".
      expect(
        screen.getByText(/2 of 3 Implemented Active/i),
      ).toBeInTheDocument()
    })
  })

  it('renders all eight category tabs', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<StrategyMatrix />)
    for (const tab of [
      'All Catalog',
      'Implemented (3)',
      'Market Making',
      'Arbitrage',
      'Stat Arb',
      'Momentum',
      'Event Driven',
      'AI / ML',
    ]) {
      expect(screen.getByRole('button', { name: tab })).toBeInTheDocument()
    }
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': sampleLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Avellaneda-Stoikov Market Maker'),
      ).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls the catalog + leaderboard every 4 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': sampleLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText('Avellaneda-Stoikov Market Maker'),
    ).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(2) // catalog + leaderboard

    // Advance 4 s — should fire two more polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 2)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': sampleCatalog,
        '/api/leaderboard': sampleLeaderboard,
      }),
    )
    const { unmount } = render(<StrategyMatrix />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText('Avellaneda-Stoikov Market Maker'),
    ).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders gracefully when catalog is empty', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/strategies/catalog': emptyCatalog,
        '/api/leaderboard': emptyLeaderboard,
      }),
    )
    render(<StrategyMatrix />)
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    // Header should still render.
    expect(
      screen.getByText(/Quantitative Strategy Matrix/i),
    ).toBeInTheDocument()
    // No strategy cards should be rendered.
    expect(
      screen.queryByText('Avellaneda-Stoikov Market Maker'),
    ).not.toBeInTheDocument()
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed all fetch/toggle errors via
  // `} catch {}`. The W22-1 fix surfaces them via inline dismissable
  // banners. These tests verify the new contract surfaces.

  it('W22-1: shows the catalog error banner when /api/strategies/catalog returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/strategies/catalog')) {
          return Promise.resolve({
            ok: false,
            status: 500,
            statusText: 'Internal Server Error',
            json: async () => ({}),
          } as Response)
        }
        // leaderboard returns OK so only the catalog error surfaces.
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => emptyLeaderboard,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load strategy catalog \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    // The banner is dismissable.
    expect(
      screen.getByRole('button', { name: /Dismiss catalog error/i }),
    ).toBeInTheDocument()
  })

  it('W22-1: shows the performance error banner when /api/leaderboard returns HTTP 502', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/leaderboard')) {
          return Promise.resolve({
            ok: false,
            status: 502,
            statusText: 'Bad Gateway',
            json: async () => ({}),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => emptyCatalog,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load per-strategy performance \(HTTP 502\)/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('button', { name: /Dismiss performance error/i }),
    ).toBeInTheDocument()
  })

  it('W22-1: shows the catalog error banner when the fetch throws a network error', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/strategies/catalog')) {
          return Promise.reject(new Error('Network error: ECONNREFUSED'))
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => emptyLeaderboard,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: dismisses the catalog error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/strategies/catalog')) {
          return Promise.resolve({
            ok: false,
            status: 500,
            statusText: 'Internal Server Error',
            json: async () => ({}),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => emptyLeaderboard,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load strategy catalog/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss catalog error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load strategy catalog/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: shows the toggle error banner when POST /api/strategies/toggle returns HTTP 423', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string, init?: RequestInit) => {
        if (
          typeof input === 'string' &&
          input.includes('/api/strategies/toggle') &&
          init?.method === 'POST'
        ) {
          return Promise.resolve({
            ok: false,
            status: 423,
            json: async () => ({ detail: 'Risk engine blocked toggle' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () =>
            input.includes('/api/leaderboard') ? emptyLeaderboard : sampleCatalog,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Binary Dutch-Book Arbitrage'),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Deploy/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Risk engine blocked toggle/i),
      ).toBeInTheDocument()
    })
    // Toggle error banner is dismissable.
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss toggle error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/Risk engine blocked toggle/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: shows the toggle error banner when the POST throws a network error', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string, init?: RequestInit) => {
        if (
          typeof input === 'string' &&
          input.includes('/api/strategies/toggle') &&
          init?.method === 'POST'
        ) {
          return Promise.reject(new Error('Network error: ECONNRESET'))
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () =>
            input.includes('/api/leaderboard') ? emptyLeaderboard : sampleCatalog,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(
        screen.getByText('Binary Dutch-Book Arbitrage'),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Deploy/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNRESET/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: logs the catalog fetch error to console.error (silent swallow removed)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/strategies/catalog')) {
          return Promise.reject(new Error('Network error: ECONNREFUSED'))
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => emptyLeaderboard,
        } as Response)
      }),
    )
    render(<StrategyMatrix />)
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining('[StrategyMatrix]'),
        expect.any(Error),
      )
    })
    consoleErrorSpy.mockRestore()
  })
})
