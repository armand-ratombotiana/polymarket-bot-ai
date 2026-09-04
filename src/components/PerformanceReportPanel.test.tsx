// components/PerformanceReportPanel.test.tsx — W26-2 component tests.
//
// Strategy:
//   * Mock `global.fetch` (already mocked to vi.fn() in `test/setup.ts`)
//     with a fully-typed `PerformanceReport` payload so the panel's
//     initial REST fetch resolves with valid data.
//   * Each test rebuilds the mock via `mockFetchOk(payload)` so the
//     cases are independent (no shared mutable state).
//   * For the auto-refresh test, use `vi.useFakeTimers()` + advance
//     by `refreshIntervalMs` and assert that fetch was called again.
//
// What's covered:
//   1.  Renders the panel header + disclaimer banner even while loading.
//   2.  Renders the four category tabs (Backtest / Walk-Forward / Paper / Live).
//   3.  After data loads, renders the active category's 12 metric cards.
//   4.  Win-rate metric card displays the 95% CI text "[X%, Y%]".
//   5.  Win-rate card renders a CI range bar element.
//   6.  Switching tabs (click) updates the displayed metric grid.
//   7.  Unavailable category (live) shows the unavailable message.
//   8.  Disclaimer banner contains the required disclosure text.
//   9.  Auto-refresh fires fetch on the configured interval.
//   10. Pauses auto-refresh when the document is hidden.
//   11. Renders an equity-curve chart container when the category supplies one.
//   12. Renders the error badge when the fetch fails.
//   13. Falls back to a "fully unavailable" report when the response shape is
//       unrecognised — the disclaimer still renders.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  PerformanceReportPanel,
  type PerformanceReport,
  type CategoryMetrics,
} from './PerformanceReportPanel'

// --- Mock data ---------------------------------------------------------------

function makeCategory(
  category: CategoryMetrics['category'],
  overrides: Partial<CategoryMetrics> = {},
): CategoryMetrics {
  const base: CategoryMetrics = {
    category,
    available: true,
    win_rate: 0.72,
    win_rate_ci_low: 0.652,
    win_rate_ci_high: 0.781,
    profit_factor: 2.4,
    expectancy: 0.18,
    max_drawdown_pct: 0.08,
    sharpe_ratio: 1.6,
    sortino_ratio: 2.1,
    open_exposure: 12.34,
    capital_utilization: 0.45,
    avg_slippage_bps: 3.2,
    total_fees: 12.4,
    n_trades: 240,
    p_value: 0.001,
    is_statistically_significant: true,
    ...overrides,
  }
  return base
}

const mockReport: PerformanceReport = {
  backtest: makeCategory('backtest', {
    equity_curve: [
      { timestamp: 1700000000000, equity: 100 },
      { timestamp: 1700000100000, equity: 102 },
      { timestamp: 1700000200000, equity: 105 },
      { timestamp: 1700000300000, equity: 108 },
    ],
  }),
  walk_forward: makeCategory('walk_forward', {
    win_rate: 0.61,
    win_rate_ci_low: 0.54,
    win_rate_ci_high: 0.68,
    is_statistically_significant: false,
    p_value: 0.08,
    n_trades: 80,
  }),
  paper_trading: makeCategory('paper_trading', {
    win_rate: 0.85,
    win_rate_ci_low: 0.79,
    win_rate_ci_high: 0.9,
    expectancy: 0.42,
    n_trades: 150,
    equity_curve: [
      { timestamp: 1700000000000, equity: 100 },
      { timestamp: 1700000100000, equity: 105 },
      { timestamp: 1700000200000, equity: 110 },
    ],
  }),
  live: {
    category: 'live',
    available: false,
    unavailable_reason: 'Live trading not enabled',
    win_rate: null,
    win_rate_ci_low: null,
    win_rate_ci_high: null,
    profit_factor: null,
    expectancy: null,
    max_drawdown_pct: null,
    sharpe_ratio: null,
    sortino_ratio: null,
    open_exposure: null,
    capital_utilization: null,
    avg_slippage_bps: null,
    total_fees: null,
    n_trades: 0,
    p_value: null,
    is_statistically_significant: false,
  },
  disclaimer:
    '⚠ Backtest performance does NOT guarantee future results. Only paper/live metrics reflect actual system behavior. Win rate target (95%) is aspirational.',
}

// --- Mock helpers ------------------------------------------------------------

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
    json: async () => ({}),
  } as Response)
}

// --- Test suite --------------------------------------------------------------

describe('PerformanceReportPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.useRealTimers()
  })

  // ── Loading + disclaimer ────────────────────────────────────────────────

  it('renders the panel header + disclaimer banner even while loading', () => {
    // Never-resolving fetch → loading stays true.
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<PerformanceReportPanel />)
    expect(
      screen.getByText('📈 Honest Performance Report'),
    ).toBeInTheDocument()
    // Disclaimer is ALWAYS rendered — even before the fetch resolves.
    expect(screen.getByTestId('performance-disclaimer')).toBeInTheDocument()
  })

  it('renders the four category tabs', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<PerformanceReportPanel />)
    expect(screen.getByTestId('tab-backtest')).toHaveTextContent('Backtest')
    expect(screen.getByTestId('tab-walk-forward')).toHaveTextContent('Walk-Forward')
    expect(screen.getByTestId('tab-paper')).toHaveTextContent('Paper Trading')
    expect(screen.getByTestId('tab-live')).toHaveTextContent('Live')
  })

  // ── Render with mock data ───────────────────────────────────────────────

  it('renders the active category metric grid after data loads', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    // Default active category is paper_trading.
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    // 12 metric cards (one per metric). Each card carries a stable
    // `data-card-type="metric"` attribute (in addition to its unique
    // `data-testid`), so we can count them without enumerating IDs.
    const cards = document.querySelectorAll('[data-card-type="metric"]')
    expect(cards.length).toBe(12)
  })

  it('renders the auto-refresh badge', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('auto-refresh-badge')).toHaveTextContent('⟳ 30s')
    })
  })

  // ── Confidence interval ─────────────────────────────────────────────────

  it('displays the win-rate 95% CI as "[low%, high%]" text', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      // paper_trading win_rate=0.85, ci_low=0.79, ci_high=0.9
      // Expected text: "85.0% [79.0%, 90.0%]"
      expect(screen.getByTestId('category-paper_trading-winrate')).toHaveTextContent(
        /85\.0%\s*\[79\.0%,\s*90\.0%\]/,
      )
    })
  })

  it('renders a CI range bar element under the win-rate card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('ci-range-bar')).toBeInTheDocument()
    })
  })

  it('does NOT render the CI range bar when CI bounds are null', async () => {
    // walk_forward is the second tab; live is unavailable. We have to
    // switch to a category whose CI bounds are null to assert absence.
    // Make paper_trading have null CI bounds.
    const noCiReport: PerformanceReport = {
      ...mockReport,
      paper_trading: { ...mockReport.paper_trading, win_rate_ci_low: null, win_rate_ci_high: null },
    }
    vi.mocked(fetch).mockImplementation(mockFetchOk(noCiReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-winrate')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('ci-range-bar')).not.toBeInTheDocument()
  })

  // ── Tab switching ────────────────────────────────────────────────────────

  it('switches the displayed metric grid when a different tab is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    const user = userEvent.setup()
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    // Switch to Backtest.
    await user.click(screen.getByTestId('tab-backtest'))
    await waitFor(() => {
      expect(screen.getByTestId('category-backtest-grid')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('category-paper_trading-grid')).not.toBeInTheDocument()
    // Backtest has win_rate 0.72 → "72.0% [65.2%, 78.1%]".
    expect(screen.getByTestId('category-backtest-winrate')).toHaveTextContent(
      /72\.0%\s*\[65\.2%,\s*78\.1%\]/,
    )
  })

  it('shows the unavailable card when the selected category is unavailable', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    const user = userEvent.setup()
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    // Switch to Live — unavailable.
    await user.click(screen.getByTestId('tab-live'))
    await waitFor(() => {
      expect(screen.getByTestId('category-live-unavailable')).toBeInTheDocument()
    })
    // The unavailable reason is shown.
    expect(screen.getByText(/Live trading not enabled/)).toBeInTheDocument()
  })

  // ── Equity curve ─────────────────────────────────────────────────────────

  it('renders an equity-curve chart container when the category supplies equity_curve data', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    const user = userEvent.setup()
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    // paper_trading has equity_curve with 3 points — chart should render.
    expect(screen.getByTestId('category-paper_trading-equity')).toBeInTheDocument()
    // Switch to walk_forward — equity_curve is undefined — chart should NOT render.
    await user.click(screen.getByTestId('tab-walk-forward'))
    await waitFor(() => {
      expect(screen.getByTestId('category-walk_forward-grid')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('category-walk_forward-equity')).not.toBeInTheDocument()
  })

  // ── Disclaimer text ──────────────────────────────────────────────────────

  it('displays the disclaimer with the required disclosure text', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    const disclaimer = await screen.findByTestId('performance-disclaimer')
    expect(disclaimer).toHaveTextContent(/does NOT guarantee future results/i)
    expect(disclaimer).toHaveTextContent(/Only paper\/live metrics reflect actual system behavior/i)
    expect(disclaimer).toHaveTextContent(/Win rate target \(95%\) is aspirational/i)
  })

  it('renders the fallback disclaimer when the response is malformed', async () => {
    // No `disclaimer` field — coerceLegacyShape returns null — panel falls
    // back to the static FALLBACK_DISCLAIMER constant.
    vi.mocked(fetch).mockImplementation(mockFetchOk({ some_unrelated: 'payload' }))
    render(<PerformanceReportPanel />)
    const disclaimer = await screen.findByTestId('performance-disclaimer')
    expect(disclaimer).toHaveTextContent(/does NOT guarantee future results/i)
  })

  // ── Error handling ───────────────────────────────────────────────────────

  it('renders the error badge when the fetch fails with a non-OK status', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('report-error')).toHaveTextContent(/HTTP 500/)
    })
    // Disclaimer is STILL rendered.
    expect(screen.getByTestId('performance-disclaimer')).toBeInTheDocument()
  })

  it('renders the error badge when the fetch throws', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error'))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('report-error')).toHaveTextContent(/Network error/)
    })
  })

  // ── Auto-refresh ─────────────────────────────────────────────────────────
  //
  // These tests use `vi.useFakeTimers()` + `act(async () => await
  // vi.advanceTimersByTimeAsync(N))` because `waitFor` itself uses
  // setTimeout internally (which is faked). The async variant both
  // advances the fake clock AND flushes pending microtasks (including
  // the fetch Promise.resolve chain that the panel's setInterval
  // callback kicks off).

  it('auto-refreshes on the configured interval', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel refreshIntervalMs={100} />)

    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)

    // Advance fake timers by 100ms → one auto-refresh tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)

    // Advance by another 100ms → second auto-refresh tick.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(3)
  })

  it('pauses auto-refresh when the document is hidden, resumes on visibilitychange', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel refreshIntervalMs={100} />)

    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)

    // Hide the document.
    Object.defineProperty(document, 'hidden', {
      configurable: true,
      value: true,
    })
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    // Advance by 500ms — should NOT trigger an auto-refresh (paused).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)

    // Show the document again.
    Object.defineProperty(document, 'hidden', {
      configurable: true,
      value: false,
    })
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    // Now advancing the timer SHOULD trigger a refresh.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(100)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })

  it('cleans up the auto-refresh interval on unmount', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    const { unmount } = render(<PerformanceReportPanel refreshIntervalMs={100} />)
    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
    unmount()
    // After unmount, advancing the timer should NOT trigger any new fetch.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
  })

  // ── Endpoint URL + headers ──────────────────────────────────────────────

  it('fetches /api/performance/report on mount via the gateway', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    expect(vi.mocked(fetch)).toHaveBeenCalled()
    const firstCallUrl = (vi.mocked(fetch).mock.calls[0] as [string, unknown])[0] as string
    expect(firstCallUrl).toContain('/api/performance/report')
    expect(firstCallUrl).toContain('XTransformPort=8080')
  })

  it('injects an Authorization header into the fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(mockReport))
    render(<PerformanceReportPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('category-paper_trading-grid')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
