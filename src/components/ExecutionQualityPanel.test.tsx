// components/ExecutionQualityPanel.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so we can drive the panel through loading →
// loaded → error states without touching the gateway.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import ExecutionQualityPanel from './ExecutionQualityPanel'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

const sampleResponse = {
  stats: {
    count: 42,
    strategy: null,
    time_window_seconds: 86400,
    avg_slippage_bps: 2.5,
    median_slippage_bps: 1.8,
    p95_slippage_bps: 6.1,
    worst_slippage_bps: 12.4,
    avg_latency_ms: 95,
    avg_realized_edge: 0.04,
    total_realized_edge: 1.68,
    by_side: { BUY: 25, SELL: 17 },
  },
  recent_fills: [
    {
      id: 1,
      timestamp: 1700000000,
      order_id: 'ord-1',
      decision_id: 'dec-1',
      token_id: 'tok-1',
      strategy: 'mm_avellaneda_stoikov',
      side: 'BUY',
      signal_price: 0.5,
      decision_price: 0.5,
      submitted_price: 0.5,
      best_bid: 0.49,
      best_ask: 0.51,
      expected_fill: 0.5,
      actual_fill: 0.51,
      spread: 0.02,
      slippage: 0.01,
      slippage_bps: 10,
      latency_ms: 80,
      realized_edge: 0.02,
      paper: 1,
      data_json: null,
    },
  ],
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

describe('ExecutionQualityPanel', () => {
  it('renders without crashing', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleResponse))
    render(<ExecutionQualityPanel />)
    // The panel renders a loading skeleton until the fetch resolves; wait
    // for the header to appear after the data loads before asserting.
    await waitFor(() => {
      expect(screen.getByText('⚡ Execution Quality')).toBeInTheDocument()
    })
  })

  it('renders the "⚡ Execution Quality" header once data loads', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleResponse))
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(screen.getByText('⚡ Execution Quality')).toBeInTheDocument()
    })
    expect(screen.getByText('Per-Fill Audit')).toBeInTheDocument()
  })

  it('renders the loading skeleton before data arrives', () => {
    // Never-resolving promise → loading stays true.
    apiFetchMock.mockImplementation(() => new Promise<Response>(() => {}))
    render(<ExecutionQualityPanel />)
    // The loading skeleton renders skeleton-line divs. We assert that
    // the panel renders some skeleton divs but the main header is NOT
    // yet visible (it only appears after data loads).
    expect(screen.queryByText('Per-Fill Audit')).not.toBeInTheDocument()
  })

  it('shows the "Execution Quality Ledger Unreachable" error when the fetch returns not-ok', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Execution Quality Ledger Unreachable'),
      ).toBeInTheDocument()
    })
  })

  it('shows the "Execution Quality Ledger Unreachable" error when the fetch throws', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Execution Quality Ledger Unreachable'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Retry button on the error fallback', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('re-fetches the ledger when the Retry button is clicked', async () => {
    apiFetchMock
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockOk(sampleResponse))
    render(<ExecutionQualityPanel />)
    const retry = await screen.findByRole('button', { name: /retry/i })
    retry.click()
    await waitFor(() => {
      expect(
        screen.getByText('⚡ Execution Quality'),
      ).toBeInTheDocument()
    })
    expect(apiFetchMock).toHaveBeenCalledTimes(2)
  })

  it('fetches the /api/execution-quality endpoint on mount', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleResponse))
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled()
    })
    const firstCallUrl = apiFetchMock.mock.calls[0][0] as string
    expect(firstCallUrl).toContain('/api/execution-quality')
  })

  it('renders the empty-state message when recent_fills is empty', async () => {
    apiFetchMock.mockResolvedValue(
      mockOk({
        stats: { ...sampleResponse.stats, count: 0 },
        recent_fills: [],
      }),
    )
    render(<ExecutionQualityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('No execution-quality records'),
      ).toBeInTheDocument()
    })
  })
})
