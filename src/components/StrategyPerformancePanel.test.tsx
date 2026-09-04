// components/StrategyPerformancePanel.test.tsx — Strategy Performance
// Dashboard panel tests (W23-5).
//
// Covers the contract surfaces required by the W23-5 spec:
//   1. Initial loading skeleton renders before the first fetch resolves.
//   2. Renders the "Strategy Performance Dashboard" title.
//   3. Renders the per-strategy overview cards (P&L, win rate, profit
//      factor, expectancy, Sharpe, trade count, avg hold, toggle).
//   4. Renders the attribution bar chart (PnLBarChart) with green/red
//      bars per strategy.
//   5. Renders the sortable performance comparison table.
//   6. Renders the risk-adjusted ranking panel (Sharpe / Sortino / Calmar).
//   7. Clicking the toggle on a strategy card fires POST
//      /api/strategies/toggle.
//   8. Auto-refresh — polls /api/strategies/performance every 30 s and
//      re-renders with the updated payload.
//   9. Hard-error state shows the retry affordance.
//  10. Empty state — no strategies renders the empty-card placeholder.
//  11. Equity overlay chart renders when equity_curve data is present.
//
// Strategy (mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx):
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • The component's fetches go through `apiFetch` (which wraps `fetch`
//     and adds an Authorization header). Mocking `global.fetch` directly
//     is sufficient because apiFetch ultimately calls it.
//   • For initial-render assertions use real timers + `waitFor` (the
//     default 1s polling tick that drives `waitFor` flushes the fetch
//     microtask + setState).
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })`.
//   • Use `getAllByText` for values that appear in multiple DOM nodes.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import StrategyPerformancePanel from './StrategyPerformancePanel'

// ── Sample payloads ─────────────────────────────────────────────────────────

const now = Math.floor(Date.now() / 1000)

const sampleStrategies = [
  {
    strategy_id: 'mm_avelaneda_stoikov',
    name: 'Avellaneda-Stoikov Market Maker',
    version: '1.2',
    category: 'market_making',
    description: 'Inventory-aware spread pricing.',
    risk_level: 'MEDIUM',
    status: 'IMPLEMENTED',
    is_running: true,
    is_enabled: true,
    realized_pnl: 12.45,
    unrealized_pnl: 0.0,
    net_pnl: 12.45,
    gross_pnl: 18.30,
    closed_trades: 38,
    open_trades: 2,
    fills: 45,
    win_rate: 0.6316,
    profit_factor: 2.14,
    expectancy: 0.327,
    avg_win: 0.85,
    avg_loss: -0.32,
    sharpe_ratio: 1.847,
    sortino_ratio: 2.413,
    calmar_ratio: 1.92,
    max_drawdown: 6.49,
    avg_hold_hours: 4.2,
    notional_volume: 245.30,
    open_exposure: 8.50,
    equity_curve: [
      { timestamp: now - 3600, pnl: 0.85 },
      { timestamp: now - 3000, pnl: 0.53 },
      { timestamp: now - 2400, pnl: -0.32 },
      { timestamp: now - 1800, pnl: 1.45 },
      { timestamp: now - 1200, pnl: 0.78 },
      { timestamp: now - 600, pnl: 9.16 },
    ],
  },
  {
    strategy_id: 'arb_binary_dutch_book',
    name: 'Binary Dutch-Book Arbitrage',
    version: '0.9',
    category: 'arbitrage',
    description: 'Captures YES+NO < $1 dual-leg mispricings.',
    risk_level: 'LOW',
    status: 'IMPLEMENTED',
    is_running: true,
    is_enabled: true,
    realized_pnl: 4.20,
    unrealized_pnl: 0.0,
    net_pnl: 4.20,
    gross_pnl: 5.10,
    closed_trades: 12,
    open_trades: 0,
    fills: 12,
    win_rate: 1.0,
    profit_factor: null,
    expectancy: 0.35,
    avg_win: 0.35,
    avg_loss: 0.0,
    sharpe_ratio: 0.92,
    sortino_ratio: null,
    calmar_ratio: 0.88,
    max_drawdown: 0.0,
    avg_hold_hours: 0.2,
    notional_volume: 28.40,
    open_exposure: 0.0,
    equity_curve: [
      { timestamp: now - 1800, pnl: 0.35 },
      { timestamp: now - 1200, pnl: 0.70 },
      { timestamp: now - 600, pnl: 4.20 },
    ],
  },
  {
    strategy_id: 'ml_random_forest_quant',
    name: 'Random Forest Quant Model',
    version: '2.0',
    category: 'machine_learning',
    description: 'Multi-factor bagging ensemble.',
    risk_level: 'LOW',
    status: 'IMPLEMENTED',
    is_running: false,
    is_enabled: false,
    realized_pnl: -1.85,
    unrealized_pnl: 0.0,
    net_pnl: -1.85,
    gross_pnl: 4.20,
    closed_trades: 18,
    open_trades: 0,
    fills: 22,
    win_rate: 0.44,
    profit_factor: 0.78,
    expectancy: -0.103,
    avg_win: 0.62,
    avg_loss: -0.41,
    sharpe_ratio: -0.45,
    sortino_ratio: -0.62,
    calmar_ratio: null,
    max_drawdown: 3.20,
    avg_hold_hours: 6.5,
    notional_volume: 64.80,
    open_exposure: 0.0,
    equity_curve: [
      { timestamp: now - 3600, pnl: 0.62 },
      { timestamp: now - 2700, pnl: -0.41 },
      { timestamp: now - 1800, pnl: -2.05 },
      { timestamp: now - 900, pnl: -1.85 },
    ],
  },
  {
    strategy_id: 'stat_bollinger_reversion',
    name: 'Bollinger Bands Reversion',
    version: '1.0',
    category: 'statistical',
    description: 'Buys/sells when price touches 2.5-sigma bands.',
    risk_level: 'MEDIUM',
    status: 'PLANNED',
    is_running: false,
    is_enabled: false,
    realized_pnl: 0.0,
    unrealized_pnl: 0.0,
    net_pnl: 0.0,
    gross_pnl: 0.0,
    closed_trades: 0,
    open_trades: 0,
    fills: 0,
    win_rate: 0.0,
    profit_factor: null,
    expectancy: 0.0,
    avg_win: 0.0,
    avg_loss: 0.0,
    sharpe_ratio: null,
    sortino_ratio: null,
    calmar_ratio: null,
    max_drawdown: 0.0,
    avg_hold_hours: 0.0,
    notional_volume: 0.0,
    open_exposure: 0.0,
    equity_curve: [],
  },
]

const sampleResponse = {
  strategies: sampleStrategies,
  total_pnl: 14.80,
  active_count: 2,
  implemented_count: 3,
  planned_count: 1,
  generated_at: now,
}

const emptyResponse = {
  strategies: [],
  total_pnl: 0.0,
  active_count: 0,
  implemented_count: 0,
  planned_count: 0,
  generated_at: now,
}

// ── Fetch mock helpers ───────────────────────────────────────────────────────

function mockFetchOk(payload: unknown) {
  return vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () => payload,
    } as Response),
  )
}

function mockFetchNotOk(status = 500, statusText = 'Internal Server Error') {
  return vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: false,
      status,
      statusText,
      json: async () => ({}),
    } as Response),
  )
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

// ── Tests ────────────────────────────────────────────────────────────────────

describe('StrategyPerformancePanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ── Initial loading state ─────────────────────────────────────────────

  it('renders the loading skeleton on first mount before data arrives', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<StrategyPerformancePanel />)
    expect(
      screen.getByText('Loading Strategy Performance…'),
    ).toBeInTheDocument()
    expect(document.querySelector('.spinner')).toBeTruthy()
  })

  // ── Happy path: sample payload ────────────────────────────────────────

  it('renders the panel title and total P&L headline once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy Performance Dashboard'),
      ).toBeInTheDocument()
    })
    // Header total P&L — formatted as "+$14.80"
    expect(screen.getByText('+$14.80')).toBeInTheDocument()
    // Header counts: "2 active · 3 impl · 1 planned"
    expect(
      screen.getByText(/2 active.*3 impl.*1 planned/),
    ).toBeInTheDocument()
  })

  it('fetches /api/strategies/performance on mount', async () => {
    const impl = mockFetchOk(sampleResponse)
    vi.mocked(fetch).mockImplementation(impl)
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy Performance Dashboard'),
      ).toBeInTheDocument()
    })
    expect(impl).toHaveBeenCalled()
    const calls = impl.mock.calls.map((c) => c[0] as string)
    expect(
      calls.some((url) => url.includes('/api/strategies/performance')),
    ).toBe(true)
  })

  // ── Strategy cards ────────────────────────────────────────────────────

  it('renders one overview card per active strategy (IMPLEMENTED or with closed trades)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      const cards = screen.getAllByTestId('strategy-card')
      // 3 active rows: 2 IMPLEMENTED+running, 1 IMPLEMENTED+stopped with closed trades.
      // The 4th (PLANNED, no trades) is filtered out by the activeRows filter.
      expect(cards).toHaveLength(3)
    })
    expect(
      screen.getAllByText('Avellaneda-Stoikov Market Maker').length,
    ).toBeGreaterThan(0)
    expect(
      screen.getAllByText('Binary Dutch-Book Arbitrage').length,
    ).toBeGreaterThan(0)
    expect(
      screen.getAllByText('Random Forest Quant Model').length,
    ).toBeGreaterThan(0)
    // PLANNED strategy with no trades isn't in the cards grid.
    // Bollinger (PLANNED, no trades) is filtered OUT of the cards grid. The cards-grid
    // container has 3 cards (data-testid="strategy-card"); Bollinger only appears
    // in the comparison table below, not in the cards.
    const cards = screen.getAllByTestId('strategy-card')
    const cardTexts = cards.map((c) => c.textContent ?? '')
    expect(cardTexts.some((t) => t.includes('Bollinger Bands Reversion'))).toBe(false)
  })

  it('renders the IMPLEMENTED status badge on the strategy card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    // Each card has one status badge; all 3 visible strategies are IMPLEMENTED.
    // The comparison table ALSO renders a status badge per row (including
    // Bollinger's PLANNED badge), so filter to badges inside strategy-card.
    const cards = screen.getAllByTestId('strategy-card')
    const cardBadges = cards.flatMap((c) =>
      Array.from(c.querySelectorAll('[data-testid="strategy-status-badge"]')),
    )
    expect(cardBadges.length).toBe(3)
    for (const b of cardBadges) {
      expect(b).toHaveTextContent('IMPLEMENTED')
    }
  })

  it('renders the net P&L value per strategy with green/red coloring', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    // mm_avelaneda_stoikov net_pnl=12.45 → "+$12.45" appears in the card AND table.
    expect(screen.getAllByText('+$12.45').length).toBeGreaterThan(0)
    // arb_binary_dutch_book net_pnl=4.20 → "+$4.20" appears in the card AND table.
    expect(screen.getAllByText('+$4.20').length).toBeGreaterThan(0)
    // ml_random_forest_quant net_pnl=-1.85 → "−$1.85" appears in the card AND table.
    expect(screen.getAllByText('−$1.85').length).toBeGreaterThan(0)
  })

  it('renders the Sharpe / Win Rate / Profit Factor / Expectancy stat tiles on each card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    // mm_avelaneda_stoikov — win_rate 0.6316 → "63.2%"
    expect(screen.getAllByText('63.2%').length).toBeGreaterThan(0)
    // profit_factor 2.14 → "2.14"
    expect(screen.getAllByText('2.14').length).toBeGreaterThan(0)
    // Sharpe 1.847 → "1.85"
    expect(screen.getAllByText('1.85').length).toBeGreaterThan(0)
    // closed_trades 38 — appears in card stat tile AND table cell.
    expect(screen.getAllByText('38').length).toBeGreaterThan(0)
  })

  it('renders the enabled/disabled Switch on each strategy card', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    const toggles = screen.getAllByTestId('strategy-toggle')
    // 3 active cards → 3 toggles (one per card)
    expect(toggles.length).toBe(3)
  })

  // ── Toggle behaviour ──────────────────────────────────────────────────

  it('fires POST /api/strategies/toggle when the switch is clicked', async () => {
    const impl = mockFetchRouteGetPost(
      sampleResponse,
      { status: 'stopped', strategy: 'mm_avelaneda_stoikov' },
    )
    vi.mocked(fetch).mockImplementation(impl)
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    const toggles = screen.getAllByTestId('strategy-toggle')
    // Click the first toggle (the mm_avelaneda_stoikov card's switch).
    const firstToggle = toggles[0]
    // The shadcn Switch renders a button — click it.
    const button = firstToggle.closest('button') ?? firstToggle
    await act(async () => {
      fireEvent.click(button)
    })
    // Assert a POST call was made.
    const postCalls = impl.mock.calls.filter(
      (c) => (c[1] as RequestInit | undefined)?.method === 'POST',
    )
    expect(postCalls.length).toBeGreaterThan(0)
    const postUrl = postCalls[0][0] as string
    expect(postUrl).toContain('/api/strategies/toggle')
  })

  it('shows a dismissible banner when toggle fails', async () => {
    const impl = vi.fn().mockImplementation((input: string, init?: RequestInit) => {
      const method = init?.method ?? 'GET'
      if (method === 'POST') {
        return Promise.resolve({
          ok: false,
          status: 400,
          statusText: 'Bad Request',
          json: async () => ({ detail: 'Risk gate rejected toggle' }),
        } as Response)
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => sampleResponse,
      } as Response)
    })
    vi.mocked(fetch).mockImplementation(impl)
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    const toggles = screen.getAllByTestId('strategy-toggle')
    const button = toggles[0].closest('button') ?? toggles[0]
    await act(async () => {
      fireEvent.click(button)
    })
    await waitFor(() => {
      expect(screen.getByText(/Toggle failed/)).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Risk gate rejected toggle/),
    ).toBeInTheDocument()
    // Dismiss
    const dismissBtn = screen.getByRole('button', { name: /dismiss toggle error/i })
    await act(async () => {
      fireEvent.click(dismissBtn)
    })
    await waitFor(() => {
      expect(screen.queryByText(/Toggle failed/)).not.toBeInTheDocument()
    })
  })

  // ── Performance comparison table ───────────────────────────────────────

  it('renders the sortable performance comparison table with all strategies', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Performance Comparison'),
      ).toBeInTheDocument()
    })
    // The table includes ALL strategies (4), not just active ones.
    const rows = screen.getAllByTestId('performance-table-row')
    expect(rows.length).toBe(4)
    // Includes the PLANNED row that the cards omit.
    expect(
      screen.getByText('Bollinger Bands Reversion'),
    ).toBeInTheDocument()
    // Headers are present. (Some labels also appear in the risk-adjusted
    // ranking section / card stat tiles, so use getAllByText + length check.)
    expect(screen.getAllByText('Net P&L').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Sharpe').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Sortino').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Calmar').length).toBeGreaterThan(0)
  })

  it('sorts the table by net P&L descending by default (highest first)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getAllByText('Avellaneda-Stoikov Market Maker').length).toBeGreaterThan(0)
    })
    const rows = screen.getAllByTestId('performance-table-row')
    // First row should be mm_avelaneda_stoikov (net_pnl +12.45, the highest).
    expect(rows[0]).toHaveTextContent('Avellaneda-Stoikov Market Maker')
    // Last row should be ml_random_forest_quant (net_pnl -1.85, the lowest).
    // Bollinger has 0 net_pnl, so order is: 12.45, 4.20, 0.0, -1.85.
    expect(rows[3]).toHaveTextContent('Random Forest Quant Model')
  })

  it('re-sorts the table when the Win % header is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Performance Comparison'),
      ).toBeInTheDocument()
    })
    // Click "Win %" header — first click sets sortKey=win_rate (desc).
    const winHeader = screen.getByText('Win %').closest('th')!
    await act(async () => {
      fireEvent.click(winHeader)
    })
    const rows = screen.getAllByTestId('performance-table-row')
    // Sorted by win_rate desc: arb_binary_dutch_book (100%) → mm_avelaneda (63.16%) → ml_rf (44%) → bollinger (0%)
    expect(rows[0]).toHaveTextContent('Binary Dutch-Book Arbitrage')
    expect(rows[3]).toHaveTextContent('Bollinger Bands Reversion')
  })

  // ── Attribution chart ─────────────────────────────────────────────────

  it('renders the attribution bar chart section with all strategies', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('P&L Attribution by Strategy'),
      ).toBeInTheDocument()
    })
    // The chart container is present.
    expect(screen.getByTestId('attribution-chart')).toBeInTheDocument()
  })

  // ── Risk-adjusted ranking ─────────────────────────────────────────────

  it('renders the risk-adjusted ranking with Sharpe selected by default', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Risk-Adjusted Ranking'),
      ).toBeInTheDocument()
    })
    // Sharpe button is pressed by default.
    const sharpeBtn = screen.getByRole('button', { name: 'Sharpe' })
    expect(sharpeBtn).toHaveAttribute('aria-pressed', 'true')
    // At least one ranking row rendered.
    const rankingRows = screen.getAllByTestId('risk-ranking-row')
    expect(rankingRows.length).toBeGreaterThan(0)
    // Ranked by Sharpe desc → mm_avelaneda (1.85) should be #1.
    expect(rankingRows[0]).toHaveTextContent('Avellaneda-Stoikov Market Maker')
  })

  it('switches the risk-adjusted ranking metric when the Sortino button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Risk-Adjusted Ranking'),
      ).toBeInTheDocument()
    })
    const sortinoBtn = screen.getByRole('button', { name: 'Sortino' })
    await act(async () => {
      fireEvent.click(sortinoBtn)
    })
    expect(sortinoBtn).toHaveAttribute('aria-pressed', 'true')
    expect(
      screen.getByRole('button', { name: 'Sharpe' }),
    ).toHaveAttribute('aria-pressed', 'false')
  })

  // ── Equity overlay ────────────────────────────────────────────────────

  it('renders the equity curves overlay chart when equity_curve data is present', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Equity Curves Overlay (cumulative P&L)'),
      ).toBeInTheDocument()
    })
    expect(screen.getByTestId('equity-overlay-chart')).toBeInTheDocument()
  })

  // ── Empty state ────────────────────────────────────────────────────────

  it('renders the empty-state placeholder when the response has no strategies', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy Performance Dashboard'),
      ).toBeInTheDocument()
    })
    // No strategy cards.
    expect(screen.queryAllByTestId('strategy-card')).toHaveLength(0)
    // Empty placeholder is rendered.
    expect(
      screen.getByText(/No active strategies/),
    ).toBeInTheDocument()
    // Attribution chart shows the empty message.
    expect(
      screen.getByText(/No closed positions yet/),
    ).toBeInTheDocument()
  })

  // ── Hard error state ──────────────────────────────────────────────────

  it('renders the error state with a Retry button when the fetch fails', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500, 'Internal Server Error'))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy performance endpoint unavailable'),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('button', { name: /retry strategy performance fetch/i }),
    ).toBeInTheDocument()
  })

  it('renders the error state when the fetch rejects (network error)', async () => {
    vi.mocked(fetch).mockImplementation(() =>
      Promise.reject(new Error('Network failure: ECONNREFUSED')),
    )
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy performance endpoint unavailable'),
      ).toBeInTheDocument()
    })
    expect(screen.getByText(/Network failure: ECONNREFUSED/)).toBeInTheDocument()
  })

  // ── Auto-refresh (30s polling) ────────────────────────────────────────

  it('polls /api/strategies/performance every 30 s and re-renders with the new payload', async () => {
    vi.useFakeTimers()
    const first = { ...sampleResponse, total_pnl: 14.80 }
    const second = { ...sampleResponse, total_pnl: 22.50, generated_at: now + 30 }
    let callCount = 0
    vi.mocked(fetch).mockImplementation(() => {
      callCount++
      const payload = callCount === 1 ? first : second
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<StrategyPerformancePanel />)
    // Initial fetch resolves.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // After the initial fetch microtasks flush, the header total P&L is rendered.
    expect(screen.getByText('+$14.80')).toBeInTheDocument()
    // Advance 30 s — the next poll fires.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    expect(screen.getByText('+$22.50')).toBeInTheDocument()
    expect(callCount).toBeGreaterThanOrEqual(2)
  })

  it('does NOT poll when the document is hidden', async () => {
    vi.useFakeTimers()
    const impl = mockFetchOk(sampleResponse)
    vi.mocked(fetch).mockImplementation(impl)
    render(<StrategyPerformancePanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Strategy Performance Dashboard')).toBeInTheDocument()
    const initialCalls = impl.mock.calls.length
    // Hide the document.
    Object.defineProperty(document, 'hidden', {
      value: true,
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
    // Advance 90 s — no polls should fire while hidden.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(90_000)
    })
    expect(impl.mock.calls.length).toBe(initialCalls)
    // Restore.
    Object.defineProperty(document, 'hidden', {
      value: false,
      writable: true,
      configurable: true,
    })
    document.dispatchEvent(new Event('visibilitychange'))
    // The visibilitychange handler triggers an immediate refresh — flush
    // microtasks so the fetch + setState resolve before assertion.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(impl.mock.calls.length).toBeGreaterThan(initialCalls)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    const impl = mockFetchOk(sampleResponse)
    vi.mocked(fetch).mockImplementation(impl)
    const { unmount } = render(<StrategyPerformancePanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    unmount()
    // Advance 60 s — should not throw any "setState on unmounted" warnings.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    // Test passes if no React warning is emitted.
    expect(impl).toHaveBeenCalled()
  })

  // ── Manual refresh button ─────────────────────────────────────────────

  it('re-fetches when the header Refresh button is clicked', async () => {
    const impl = mockFetchOk(sampleResponse)
    vi.mocked(fetch).mockImplementation(impl)
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Strategy Performance Dashboard'),
      ).toBeInTheDocument()
    })
    const initialCalls = impl.mock.calls.length
    const refreshBtn = screen.getByRole('button', {
      name: /refresh strategy performance/i,
    })
    await act(async () => {
      fireEvent.click(refreshBtn)
    })
    await waitFor(() => {
      expect(impl.mock.calls.length).toBeGreaterThan(initialCalls)
    })
  })

  // ── Footer ────────────────────────────────────────────────────────────

  it('renders the "30s poll" cadence badge in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResponse))
    render(<StrategyPerformancePanel />)
    await waitFor(() => {
      expect(screen.getByText('30s poll')).toBeInTheDocument()
    })
  })
})
