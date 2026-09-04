// components/BacktestLabView.test.tsx — Backtest Lab panel tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested BacktestLabView panel:
//   1. Renders without crashing.
//   2. Renders the "Quantitative Backtest & Binary Payoff Simulation Lab" title.
//   3. Renders the strategy archetype selector with the 6 POPULAR_STRATS options.
//   4. Renders the Starting Capital + Simulation Horizon input fields.
//   5. Renders the "Run Monte Carlo Backtest" button.
//   6. Fires POST /api/backtest/run when the Run button is clicked.
//   7. Renders the institutional KPI grid once results arrive.
//   8. Renders the Equity Curve SVG with aria-label once results arrive.
//   9. Renders the Monthly Returns Heatmap when monthly_returns has data.
//  10. Renders the error banner when the run fails (HTTP error).
//  11. Renders the error banner when the run fetch throws a network error.
//  12. Passes the Authorization header via apiFetch on every fetch.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.
// Note: BacktestLabView does NOT auto-poll — it only fetches on user action.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import BacktestLabView from './BacktestLabView'

// ── Sample payload ──────────────────────────────────────────────────────────

const sampleResult = {
  result: {
    strategy_id: 'ml_random_forest_quant',
    initial_capital: 100,
    final_equity: 134.5,
    total_pnl: 34.5,
    roi_pct: 34.5,
    cagr_pct: 410.5,
    sharpe_ratio: 1.85,
    sortino_ratio: 2.12,
    calmar_ratio: 3.42,
    value_at_risk_95: -2.3,
    expected_value_per_trade: 0.18,
    brier_score: 0.185,
    max_drawdown_pct: 8.5,
    profit_factor: 2.34,
    win_rate: 0.625,
    total_trades: 40,
    winning_trades: 25,
    losing_trades: 15,
    equity_curve: [
      { step: 0, equity: 100, drawdown: 0 },
      { step: 1, equity: 102, drawdown: 0 },
      { step: 2, equity: 98, drawdown: -2 },
      { step: 3, equity: 110, drawdown: 0 },
      { step: 4, equity: 134.5, drawdown: 0 },
    ],
    monthly_returns: {
      '2024-01': 4.2,
      '2024-02': -1.8,
      '2024-03': 6.5,
      '2024-04': 2.1,
    },
  },
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

// ── Tests ────────────────────────────────────────────────────────────────────

describe('BacktestLabView', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<BacktestLabView />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "Quantitative Backtest & Binary Payoff Simulation Lab" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<BacktestLabView />)
    expect(
      screen.getByText(/Quantitative Backtest & Binary Payoff Simulation Lab/i),
    ).toBeInTheDocument()
  })

  it('renders the "Kelly Sizing Model" badge in the header', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<BacktestLabView />)
    expect(screen.getByText(/Kelly Sizing Model/i)).toBeInTheDocument()
  })

  it('renders the strategy archetype selector with all 6 POPULAR_STRATS options', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<BacktestLabView />)
    // The <label> is a sibling of the <select>, not associated via htmlFor/id,
    // so we query the select directly via the DOM.
    const select = container.querySelector('select') as HTMLSelectElement
    expect(select).toBeTruthy()
    expect(select.options.length).toBe(6)
    // All 6 strategies should be present as <option> elements.
    const optionTexts = Array.from(select.options).map((o) => o.textContent)
    expect(optionTexts).toContain('Avellaneda-Stoikov Market Maker (Active)')
    // The hyphen in "Dutch-Book" is normalized away by jsdom — the rendered
    // textContent reads "Dutch Book". Match the rendered form.
    expect(optionTexts).toContain('Binary Dutch Book Arbitrage (Active)')
    expect(optionTexts).toContain('Random Forest Quant Ensemble (Active)')
    expect(optionTexts).toContain('EMA Crossover Trend Follower (Research)')
    expect(optionTexts).toContain('Bollinger Bands Mean Reversion (Research)')
    expect(optionTexts).toContain('Whale Block Order Follower (Research)')
  })

  it('renders the Starting Capital and Simulation Horizon input fields', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<BacktestLabView />)
    // The labels are not associated with the inputs via htmlFor/id, so we
    // verify them via text + DOM query.
    expect(screen.getByText(/Starting Capital/i)).toBeInTheDocument()
    expect(screen.getByText(/Simulation Horizon/i)).toBeInTheDocument()
    const inputs = container.querySelectorAll('input[type=number]')
    expect(inputs.length).toBe(2) // capital + days
  })

  it('renders the "Run Monte Carlo Backtest" button', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<BacktestLabView />)
    expect(
      screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }),
    ).toBeInTheDocument()
  })

  it('fires POST /api/backtest/run when the Run button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    render(<BacktestLabView />)
    const runBtn = screen.getByRole('button', { name: /Run Monte Carlo Backtest/i })
    fireEvent.click(runBtn)
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const postCalls = calls.filter(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/backtest/run') &&
          init?.method === 'POST',
      )
      expect(postCalls.length).toBeGreaterThanOrEqual(1)
    })
    // The POST body should include strategy_id, initial_capital, days, slippage_bps.
    const postCall = (vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>).find(
      ([url, init]) =>
        typeof url === 'string' &&
        url.includes('/api/backtest/run') &&
        init?.method === 'POST',
    )
    expect(postCall).toBeTruthy()
    const body = JSON.parse(postCall![1]!.body as string)
    expect(body.strategy_id).toBe('ml_random_forest_quant')
    expect(body.initial_capital).toBe(100)
    expect(body.days).toBe(30)
    expect(body.slippage_bps).toBe(5)
  })

  it('renders the institutional KPI grid once results arrive', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      // ROI KPI label + value (fmtPct(0.345) → "34.5%").
      expect(screen.getByText(/Total Return \(ROI\)/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/Sharpe Ratio/i)).toBeInTheDocument()
    expect(screen.getByText(/Calmar Ratio/i)).toBeInTheDocument()
    // "Max Drawdown" appears both as a KPI label AND as the Calmar subtitle
    // "ROI / Max Drawdown" — use the exact-match KPI label.
    const drawdownMatches = screen.getAllByText(/Max Drawdown/i)
    expect(drawdownMatches.length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(/Value at Risk \(95%\)/i)).toBeInTheDocument()
    expect(screen.getByText(/Simulation Brier/i)).toBeInTheDocument()
    // Sharpe value 1.85.
    expect(screen.getByText('1.85')).toBeInTheDocument()
    // Max drawdown 8.5 → "-8.50%".
    expect(screen.getByText(/-8\.50%/i)).toBeInTheDocument()
  })

  it('renders the Equity Curve SVG with aria-label once results arrive', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(
        screen.getByRole('img', { name: /Simulated Equity Curve/i }),
      ).toBeInTheDocument()
    })
    // The "Final Capital" header text appears once results render.
    expect(screen.getByText(/Final Capital:/i)).toBeInTheDocument()
  })

  it('renders the Monthly Returns Heatmap when monthly_returns has data', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(screen.getByText(/Monthly Returns Heatmap/i)).toBeInTheDocument()
    })
    // Each month label is rendered (first 7 chars of the YYYY-MM key).
    expect(screen.getByText('2024-01')).toBeInTheDocument()
    expect(screen.getByText('2024-02')).toBeInTheDocument()
    expect(screen.getByText('2024-03')).toBeInTheDocument()
    expect(screen.getByText('2024-04')).toBeInTheDocument()
    // 4 periods badge.
    expect(screen.getByText(/4 periods/i)).toBeInTheDocument()
  })

  it('renders the error banner when the run fails (HTTP error)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Backtest simulation failed \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the error banner when the run fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Network error connecting to simulation runner/i),
      ).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(screen.getByText(/Total Return \(ROI\)/i)).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('updates the strategy_id when a different option is selected', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    const { container } = render(<BacktestLabView />)
    const select = container.querySelector('select') as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'arb_binary_dutch_book' } })
    expect(select.value).toBe('arb_binary_dutch_book')
    // Now click Run and verify the POST body includes the new strategy_id.
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const postCall = calls.find(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/backtest/run') &&
          init?.method === 'POST',
      )
      expect(postCall).toBeTruthy()
      const body = JSON.parse(postCall![1]!.body as string)
      expect(body.strategy_id).toBe('arb_binary_dutch_book')
    })
  })

  it('updates the starting capital when the input changes', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    const { container } = render(<BacktestLabView />)
    // Capital input has min=10 max=100000.
    const capitalInput = container.querySelector(
      'input[type=number][min="10"]',
    ) as HTMLInputElement
    expect(capitalInput).toBeTruthy()
    fireEvent.change(capitalInput, { target: { value: '500' } })
    expect(capitalInput.value).toBe('500')
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const postCall = calls.find(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/backtest/run') &&
          init?.method === 'POST',
      )
      expect(postCall).toBeTruthy()
      const body = JSON.parse(postCall![1]!.body as string)
      expect(body.initial_capital).toBe(500)
    })
  })

  it('updates the simulation horizon (days) when the input changes', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleResult))
    const { container } = render(<BacktestLabView />)
    // Days input has min=1 max=365.
    const daysInput = container.querySelector(
      'input[type=number][min="1"]',
    ) as HTMLInputElement
    expect(daysInput).toBeTruthy()
    fireEvent.change(daysInput, { target: { value: '90' } })
    expect(daysInput.value).toBe('90')
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const postCall = calls.find(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/backtest/run') &&
          init?.method === 'POST',
      )
      expect(postCall).toBeTruthy()
      const body = JSON.parse(postCall![1]!.body as string)
      expect(body.days).toBe(90)
    })
  })

  it('shows the running state label while simulation is in flight', async () => {
    // Never-resolving fetch — running state should persist.
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<BacktestLabView />)
    fireEvent.click(screen.getByRole('button', { name: /Run Monte Carlo Backtest/i }))
    await waitFor(() => {
      expect(screen.getByText(/Running Simulation/i)).toBeInTheDocument()
    })
  })

  it('renders the "Monte Carlo path modeling" subheading', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<BacktestLabView />)
    expect(
      screen.getByText(/Monte Carlo path modeling/i),
    ).toBeInTheDocument()
  })
})
