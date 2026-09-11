// components/AttributionPanel.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so the panel can be driven through its
// loading → loaded → error states without touching the gateway.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import AttributionPanel from './AttributionPanel'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

// PnLBarChart uses Recharts — jsdom can't measure it. Stub it.
vi.mock('@/components/charts', () => ({
  PnLBarChart: () => <div data-testid="pnl-bar-chart">bar chart placeholder</div>,
}))

const sampleAttribution = {
  summary: {
    count: 42,
    total_pnl: 12.5,
    avg_pnl: 0.3,
    median_pnl: 0.2,
    win_rate: 0.65,
    wins: 27,
    losses: 15,
    breakeven: 0,
    avg_holding_seconds: 1800,
    gross_profit: 25.0,
    gross_loss: 12.5,
    profit_factor: 2.0,
    best_trade: 5.0,
    worst_trade: -2.5,
    avg_entry_price: 0.5,
    avg_exit_price: 0.52,
    total_volume_shares: 5000,
    strategies_count: 2,
  },
  by_strategy: [
    {
      bucket: 'mm_avellaneda_stoikov',
      count: 20,
      total_pnl: 8.0,
      avg_pnl: 0.4,
      win_rate: 0.7,
      wins: 14,
      losses: 6,
      avg_holding_seconds: 1800,
      gross_profit: 16.0,
      gross_loss: 8.0,
      profit_factor: 2.0,
      capital_deployed: 50,
    },
    {
      bucket: 'arb_binary_dutch_book',
      count: 22,
      total_pnl: 4.5,
      avg_pnl: 0.2,
      win_rate: 0.6,
      wins: 13,
      losses: 9,
      avg_holding_seconds: 900,
      gross_profit: 9.0,
      gross_loss: 4.5,
      profit_factor: 2.0,
      capital_deployed: 40,
    },
  ],
  by_confidence_bucket: [
    {
      bucket: 'low',
      count: 10,
      total_pnl: 1.0,
      avg_pnl: 0.1,
      win_rate: 0.5,
      wins: 5,
      losses: 5,
      avg_holding_seconds: 1200,
      gross_profit: 2.0,
      gross_loss: 1.0,
      profit_factor: 2.0,
      capital_deployed: 20,
    },
  ],
  by_edge_bucket: [],
  by_probability_band: [],
  by_liquidity_level: [],
  by_holding_period: [],
  by_trade_direction: [],
  bucket_definitions: {},
}

function mockOk(payload: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response
}

function mockNotOk(status = 500) {
  return {
    ok: false,
    status,
    statusText: 'Internal Server Error',
    json: async () => ({}),
    text: async () => '',
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('AttributionPanel', () => {
  it('renders without crashing', () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleAttribution))
    render(<AttributionPanel />)
    expect(screen.getByText('Attribution Analysis')).toBeInTheDocument()
  })

  it('renders the "Performance Attribution" header once data loads', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleAttribution))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Performance Attribution'),
      ).toBeInTheDocument()
    })
    // 7-DIMENSION badge in the header.
    expect(screen.getByText('7-DIMENSION')).toBeInTheDocument()
  })

  it('renders the loading skeleton on first mount before data arrives', () => {
    // Never-resolving promise → loading stays true.
    apiFetchMock.mockImplementation(() => new Promise<Response>(() => {}))
    render(<AttributionPanel />)
    expect(screen.getByText('Attribution Analysis')).toBeInTheDocument()
    // The main "Performance Attribution" header is NOT visible yet.
    expect(
      screen.queryByText('Performance Attribution'),
    ).not.toBeInTheDocument()
  })

  it('shows the "Attribution unavailable" error when the fetch returns not-ok', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Attribution unavailable'),
      ).toBeInTheDocument()
    })
  })

  it('shows the "Attribution unavailable" error when the fetch throws', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Attribution unavailable'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Retry button on the error fallback', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the empty-state message when the response has no summary', async () => {
    apiFetchMock.mockResolvedValue(
      mockOk({ ...sampleAttribution, summary: {} }),
    )
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(screen.getByText('No attribution data')).toBeInTheDocument()
    })
  })

  it('fetches the /api/attribution endpoint on mount', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleAttribution))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled()
    })
    const firstCallUrl = apiFetchMock.mock.calls[0][0] as string
    expect(firstCallUrl).toContain('/api/attribution')
    // Default time range is "all".
    expect(firstCallUrl).toContain('range=all')
  })

  it('renders the Tabs for the three view modes (Dimensions / Waterfall / Strategies)', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleAttribution))
    render(<AttributionPanel />)
    await waitFor(() => {
      expect(screen.getByText('Dimensions')).toBeInTheDocument()
      expect(screen.getByText('Waterfall')).toBeInTheDocument()
      expect(screen.getByText('Strategies')).toBeInTheDocument()
    })
  })
})
