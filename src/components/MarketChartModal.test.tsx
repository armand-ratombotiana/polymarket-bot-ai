// components/MarketChartModal.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so we can drive the modal through its
// loading → loaded → error states. The modal is uncontrolled — every
// test renders it with the same `tokenId`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import MarketChartModal from './MarketChartModal'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

const sampleBars = [
  { timestamp: 1, open: 0.5, high: 0.55, low: 0.45, close: 0.52, volume: 100 },
  { timestamp: 2, open: 0.52, high: 0.58, low: 0.5, close: 0.55, volume: 120 },
  { timestamp: 3, open: 0.55, high: 0.6, low: 0.54, close: 0.59, volume: 90 },
]

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
    json: async () => ({}),
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('MarketChartModal', () => {
  it('renders without crashing', () => {
    apiFetchMock.mockResolvedValue(mockOk({ bars: [] }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders the "SYNTHETIC DATA" notice banner', () => {
    apiFetchMock.mockResolvedValue(mockOk({ bars: [] }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    expect(screen.getByText(/SYNTHETIC DATA/)).toBeInTheDocument()
  })

  it('renders the "Rendering price timeline…" placeholder while loading', () => {
    // Never-resolving promise → loading stays true.
    apiFetchMock.mockImplementation(() => new Promise<Response>(() => {}))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    expect(screen.getByText('Rendering price timeline…')).toBeInTheDocument()
  })

  it('renders the SVG candlestick chart after bars load', async () => {
    apiFetchMock.mockResolvedValue(mockOk({ bars: sampleBars }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    await waitFor(() => {
      expect(
        screen.getByRole('img', {
          name: /Price candlestick chart for/i,
        }),
      ).toBeInTheDocument()
    })
  })

  it('renders the 1m / 5m / 1h timeframe selector', () => {
    apiFetchMock.mockResolvedValue(mockOk({ bars: [] }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    expect(screen.getByRole('button', { name: /timeframe 1m/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /timeframe 5m/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /timeframe 1h/i })).toBeInTheDocument()
  })

  it('shows a stale-chart banner when the OHLCV fetch throws', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    await waitFor(() => {
      // The banner is rendered with a leading ⚠️ emoji, so use a regex
      // substring match instead of an exact-text match.
      expect(
        screen.getByText(/Network error loading price history/),
      ).toBeInTheDocument()
    })
  })

  it('calls onClose when the close (✕) button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue(mockOk({ bars: [] }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={onClose} />)
    await user.click(
      screen.getByRole('button', { name: /close market chart modal/i }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue(mockOk({ bars: [] }))
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={onClose} />)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the success banner when a BUY order POST succeeds', async () => {
    const user = userEvent.setup()
    apiFetchMock
      .mockResolvedValueOnce(mockOk({ bars: [] })) // initial OHLCV
      .mockResolvedValueOnce(
        mockOk({ detail: 'Order placed: BUY $1.5 @ 0.5' }), // trade POST
      )
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /Buy YES outcome/i }))
    await waitFor(() => {
      expect(screen.getByText(/Order placed: BUY \$1.5 @ 0.5/)).toBeInTheDocument()
    })
  })

  it('shows the error banner when a SELL order POST is rejected', async () => {
    const user = userEvent.setup()
    apiFetchMock
      .mockResolvedValueOnce(mockOk({ bars: [] })) // initial OHLCV
      .mockResolvedValueOnce(mockNotOk(400)) // trade POST rejected
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /Sell YES outcome/i }))
    await waitFor(() => {
      expect(screen.getByText(/Risk gate rejected: HTTP 400/)).toBeInTheDocument()
    })
  })

  it('shows a network error banner when the order POST throws', async () => {
    const user = userEvent.setup()
    apiFetchMock
      .mockResolvedValueOnce(mockOk({ bars: [] })) // initial OHLCV
      .mockRejectedValueOnce(new Error('Network error')) // trade POST
    render(<MarketChartModal tokenId="tok-1" slug="paris-rain" onClose={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: /Buy YES outcome/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Order submission failed/),
      ).toBeInTheDocument()
    })
  })
})
