// components/ClosedPositionsPanel.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so the panel can be driven through its
// loading → loaded → error states. The panel fetches two endpoints
// (`/api/positions/closed` + `/api/positions/closed/stats`) in
// parallel via Promise.allSettled.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import ClosedPositionsPanel from './ClosedPositionsPanel'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

const samplePositions = [
  {
    id: 1,
    timestamp: 1700000000,
    position_id: 'pos-1',
    token_id: 'tok-1',
    strategy: 'mm_avellaneda_stoikov',
    entry_price: 0.5,
    exit_price: 0.55,
    shares: 100,
    pnl: 5.0,
    holding_seconds: 3600,
    model_version: 'v1',
    decision_id: 'dec-1',
    direction: 'BUY',
    confidence: 0.7,
    predicted_edge: 0.02,
    p_yes: 0.55,
    market_mid: 0.52,
    liquidity: 1000,
    data: { slug: 'paris-rain', exit_reason: 'TP' },
  },
  {
    id: 2,
    timestamp: 1700000100,
    position_id: 'pos-2',
    token_id: 'tok-2',
    strategy: 'arb_binary_dutch_book',
    entry_price: 0.6,
    exit_price: 0.55,
    shares: 50,
    pnl: -2.5,
    holding_seconds: 1800,
    model_version: 'v1',
    decision_id: 'dec-2',
    direction: 'SELL',
    confidence: 0.6,
    predicted_edge: -0.01,
    p_yes: 0.45,
    market_mid: 0.5,
    liquidity: 500,
    data: { slug: 'tokyo-snow', exit_reason: 'SL' },
  },
]

const sampleStats = {
  count: 2,
  total_pnl: 2.5,
  avg_pnl: 1.25,
  median_pnl: 1.25,
  win_rate: 0.5,
  wins: 1,
  losses: 1,
  breakeven: 0,
  avg_holding_seconds: 2700,
  gross_profit: 5.0,
  gross_loss: 2.5,
  profit_factor: 2.0,
  best_trade: 5.0,
  worst_trade: -2.5,
  avg_entry_price: 0.55,
  avg_exit_price: 0.55,
  total_volume_shares: 150,
  strategies_count: 2,
}

function mockOk(payload: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  } as Response
}

function mockNotOk(status = 500) {
  return {
    ok: false,
    status,
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

describe('ClosedPositionsPanel', () => {
  it('renders without crashing', () => {
    apiFetchMock.mockResolvedValue(mockOk({ positions: samplePositions }))
    render(<ClosedPositionsPanel />)
    expect(
      screen.getByText('📕 Closed Positions Ledger'),
    ).toBeInTheDocument()
  })

  it('renders the "📕 Closed Positions Ledger" header once data loads', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/stats')) {
        return Promise.resolve(mockOk(sampleStats))
      }
      return Promise.resolve(mockOk({ positions: samplePositions }))
    })
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/CLOSED POSITIONS LEDGER/),
      ).toBeInTheDocument()
    })
    // Subtitle badge
    expect(screen.getByText('Realized P&L Journal')).toBeInTheDocument()
  })

  it('renders the loading skeleton before data arrives', () => {
    // Never-resolving promise → loading stays true.
    apiFetchMock.mockImplementation(() => new Promise<Response>(() => {}))
    render(<ClosedPositionsPanel />)
    expect(
      screen.getByText('📕 Closed Positions Ledger'),
    ).toBeInTheDocument()
    // The main ledger header is NOT visible yet (it only appears after data loads).
    expect(screen.queryByText(/CLOSED POSITIONS LEDGER/)).not.toBeInTheDocument()
  })

  it('shows the "Failed to load closed positions" error when the fetch returns not-ok', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Failed to load closed positions'),
      ).toBeInTheDocument()
    })
  })

  it('shows the "Failed to load closed positions" error when the fetch throws', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Failed to load closed positions'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Retry button on the error fallback', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the empty-state message when there are no closed positions', async () => {
    apiFetchMock.mockImplementation((url: string) => {
      if (url.includes('/stats')) {
        return Promise.resolve(
          mockOk({ ...sampleStats, count: 0, total_pnl: 0 }),
        )
      }
      return Promise.resolve(mockOk({ positions: [] }))
    })
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(screen.getByText('No closed positions')).toBeInTheDocument()
    })
  })

  it('fetches the /api/positions/closed endpoint on mount', async () => {
    apiFetchMock.mockResolvedValue(mockOk({ positions: samplePositions }))
    render(<ClosedPositionsPanel />)
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled()
    })
    const urls = apiFetchMock.mock.calls.map((c) => c[0] as string)
    expect(
      urls.some((u) => u.includes('/api/positions/closed?limit=500')),
    ).toBe(true)
    expect(urls.some((u) => u.includes('/api/positions/closed/stats'))).toBe(true)
  })
})
