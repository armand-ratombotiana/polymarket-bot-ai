// components/DeepAnalysisView.test.tsx — Deep Analysis panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested DeepAnalysisView panel:
//   1. Renders without crashing.
//   2. Renders the "Deep Market Intelligence & Multi-Factor Alpha Forecaster" title.
//   3. Shows the loading skeleton initially while the first fetch resolves.
//   4. Shows the hard-error state with Retry button when fetch returns 500.
//   5. Shows the hard-error state when fetch throws a network error.
//   6. Renders the "Top Alpha Opportunities" table with rows once data arrives.
//   7. Fires GET /api/analysis/market/{tokenId} when a row is clicked.
//   8. Fires onSelectMarket callback when the row's Trade button is clicked.
//   9. Fires onOpenChart callback when the "Price History" button is clicked.
//  10. Renders the "Probabilistic Valuation & Alpha" inspection card.
//  11. Renders the "Microstructure & Order Flow" inspection card.
//  12. Renders the "Regime Context & Decision Rationale" inspection card.
//  13. Polls /api/analysis/deep every 5 s.
//  14. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import DeepAnalysisView from './DeepAnalysisView'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleAnalysis = {
  top_opportunities: [
    {
      token_id: 'tok_btc_100k_yes',
      slug: 'bitcoin-100k-by-december',
      status: 'OK',
      market_implied_prob: 0.42,
      ml_forecast_prob: 0.58,
      uncertainty_interval: [0.51, 0.65],
      raw_edge: 0.16,
      net_edge: 0.135,
      confidence_score: 0.78,
      alpha_score: 0.62,
      regime: 'trending',
      regime_tag: 'trending',
      best_bid: 0.41,
      best_ask: 0.43,
      spread_dollars: 0.02,
      spread_pct: 4.6,
      total_liquidity_usdc: 25000,
      bid_depth_usdc: 12000,
      ask_depth_usdc: 13000,
      order_flow_imbalance: 0.42,
      slippage_bps: 2.4,
      fundamental_sentiment: 0.32,
      supporting_evidence: [
        {
          headline: 'BlackRock files for spot Bitcoin ETF',
          source: 'Reuters',
          category: 'INSTITUTIONAL',
          sentiment: 0.78,
          age_minutes: 22,
        },
      ],
      contradicting_evidence: [],
      suggested_action: 'TRADE_LONG_YES',
      action_reasons: ['ML forecast 16% above market mid', 'Positive OFI 0.42', 'Supporting news sentiment 0.78'],
      model_metadata: { version: 'v1.4.champion', brier_score: 0.1842, features_used: 38 },
      data_freshness_seconds: 2,
      generation_time_ms: 1.2,
    },
    {
      token_id: 'tok_fed_cut_yes',
      slug: 'fed-rate-cut-march-meeting',
      status: 'OK',
      market_implied_prob: 0.55,
      ml_forecast_prob: 0.48,
      uncertainty_interval: [0.42, 0.55],
      raw_edge: -0.07,
      net_edge: -0.085,
      confidence_score: 0.62,
      alpha_score: 0.31,
      regime: 'range',
      regime_tag: 'range',
      best_bid: 0.54,
      best_ask: 0.56,
      spread_dollars: 0.02,
      spread_pct: 3.6,
      total_liquidity_usdc: 18000,
      bid_depth_usdc: 9000,
      ask_depth_usdc: 9000,
      order_flow_imbalance: -0.18,
      slippage_bps: 3.1,
      fundamental_sentiment: 0.05,
      supporting_evidence: [],
      contradicting_evidence: [],
      suggested_action: 'MONITOR',
      action_reasons: [],
      model_metadata: { version: 'v1.4.champion', brier_score: 0.1842, features_used: 38 },
      data_freshness_seconds: 4,
      generation_time_ms: 1.5,
    },
  ],
  recent_news: [],
  timestamp: Math.floor(Date.now() / 1000),
}

const emptyAnalysis = {
  top_opportunities: [],
  recent_news: [],
  timestamp: Math.floor(Date.now() / 1000),
}

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

describe('DeepAnalysisView', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<DeepAnalysisView />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the loading skeleton initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<DeepAnalysisView />)
    // The skeleton uses `skeleton-line` / `skeleton-card` class names.
    expect(document.querySelector('.skeleton-line')).toBeTruthy()
  })

  it('renders the "Deep Market Intelligence & Multi-Factor Alpha Forecaster" title once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Deep Market Intelligence & Multi-Factor Alpha Forecaster/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the hard-error state with a Retry button when fetch returns 500', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(screen.getByText(/Analysis Engine Offline/i)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Failed to fetch deep analysis \(HTTP 500\)/i),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Retry Analysis/i }),
    ).toBeInTheDocument()
  })

  it('renders the hard-error state when fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(screen.getByText(/Analysis Engine Offline/i)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Network error: ECONNREFUSED/i),
    ).toBeInTheDocument()
  })

  it('renders the "Top Alpha Opportunities" table with rows once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      // Top opportunities count = 2.
      expect(screen.getByText(/Top Alpha Opportunities \(2 Ranked\)/i)).toBeInTheDocument()
    })
  })

  it('renders the "ML Edge 40% Weight" badge in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(screen.getByText(/ML Edge 40% Weight/i)).toBeInTheDocument()
    })
  })

  it('renders the Probabilistic Valuation & Alpha inspection card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Probabilistic Valuation & Alpha/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the Microstructure & Order Flow inspection card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Microstructure & Order Flow/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the Regime Context & Decision Rationale inspection card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Regime Context & Decision Rationale/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the suggested_action badge when analysis has suggested_action', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      // suggested_action TRADE_LONG_YES → badge text "TRADE LONG YES"
      expect(screen.getByText(/TRADE LONG YES/i)).toBeInTheDocument()
    })
  })

  it('renders the action reasons list when supporting evidence is present', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/ML forecast 16% above market mid/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Positive OFI 0\.42/i),
    ).toBeInTheDocument()
  })

  it('renders the supporting evidence headline in the Fundamental News Signal card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/BlackRock files for spot Bitcoin ETF/i),
      ).toBeInTheDocument()
    })
  })

  it('fires GET /api/analysis/market/{tokenId} when a row is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/analysis/deep': sampleAnalysis,
        '/api/analysis/market/': sampleAnalysis.top_opportunities[1],
      }),
    )
    render(<DeepAnalysisView />)
    await waitFor(() => {
      // Top opportunities count = 2 — wait for table render.
      expect(screen.getByText(/Top Alpha Opportunities \(2 Ranked\)/i)).toBeInTheDocument()
    })
    // Click the second row (which has slug 'fed-rate-cut-march-meeting').
    // The row's clickable title cell shows formatMarketTitle(slug) →
    // 'Fed Rate Cut March Meeting'.
    fireEvent.click(screen.getByText(/Fed Rate Cut March Meeting/i))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const singleCall = calls
        .map(([url]) => url)
        .find((u) => typeof u === 'string' && u.includes('/api/analysis/market/'))
      expect(singleCall).toBeTruthy()
    })
  })

  it('fires onSelectMarket callback when the row\'s Trade button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    const onSelectMarket = vi.fn()
    render(<DeepAnalysisView onSelectMarket={onSelectMarket} />)
    await waitFor(() => {
      expect(screen.getByText(/Top Alpha Opportunities \(2 Ranked\)/i)).toBeInTheDocument()
    })
    // Click the first row's Trade button (aria-label includes the row title).
    const tradeBtns = screen.getAllByRole('button', {
      name: /Open depth chart and trade ticket for/i,
    })
    fireEvent.click(tradeBtns[0])
    expect(onSelectMarket).toHaveBeenCalledTimes(1)
    expect(onSelectMarket).toHaveBeenCalledWith(
      'tok_btc_100k_yes',
      'bitcoin-100k-by-december',
    )
  })

  it('fires onOpenChart callback when the "Price History" button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    const onOpenChart = vi.fn()
    render(<DeepAnalysisView onOpenChart={onOpenChart} />)
    await waitFor(() => {
      expect(screen.getByText(/Price History/i)).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Price History/i }))
    expect(onOpenChart).toHaveBeenCalledTimes(1)
    expect(onOpenChart).toHaveBeenCalledWith({
      tokenId: 'tok_btc_100k_yes',
      slug: 'bitcoin-100k-by-december',
    })
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Deep Market Intelligence/i),
      ).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls /api/analysis/deep every 5 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText(/Deep Market Intelligence/i),
    ).toBeInTheDocument()
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
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    const { unmount } = render(<DeepAnalysisView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText(/Deep Market Intelligence/i),
    ).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders gracefully when top_opportunities is empty', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Deep Market Intelligence/i),
      ).toBeInTheDocument()
    })
    // Header badge "0 Ranked" when there are no opportunities.
    expect(screen.getByText(/Top Alpha Opportunities \(0 Ranked\)/i)).toBeInTheDocument()
    // No Trade buttons rendered.
    expect(
      screen.queryAllByRole('button', {
        name: /Open depth chart and trade ticket for/i,
      }).length,
    ).toBe(0)
  })

  it('does not render the "Price History" button when onOpenChart is not provided', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Deep Market Intelligence/i),
      ).toBeInTheDocument()
    })
    expect(
      screen.queryByRole('button', { name: /Price History/i }),
    ).not.toBeInTheDocument()
  })

  it('renders the row Trade button as disabled when onSelectMarket is not provided', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalysis))
    render(<DeepAnalysisView />)
    await waitFor(() => {
      expect(screen.getByText(/Top Alpha Opportunities \(2 Ranked\)/i)).toBeInTheDocument()
    })
    // The Trade button is always rendered (even without onSelectMarket),
    // but it is disabled — aria-label includes the row title.
    const tradeBtns = screen.getAllByRole('button', {
      name: /Open depth chart and trade ticket for/i,
    })
    expect(tradeBtns.length).toBe(2)
    // Both should be disabled when onSelectMarket is not provided.
    for (const btn of tradeBtns) {
      expect((btn as HTMLButtonElement).disabled).toBe(true)
    }
  })
})
