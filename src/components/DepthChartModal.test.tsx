// components/DepthChartModal.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` + `MarketDepthChart` so the modal renders
// without needing a real backend or a real Recharts canvas. The modal
// is a controlled component — `tokenId={null}` means "closed".
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import DepthChartModal from './DepthChartModal'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

// Stub the depth chart — jsdom can't measure a Recharts ResponsiveContainer.
vi.mock('./charts/MarketDepthChart', () => ({
  __esModule: true,
  default: (props: Record<string, unknown>) => (
    <div data-testid="depth-chart" data-mid={String(props.mid)}>
      depth chart placeholder
    </div>
  ),
}))

const sampleDepth = {
  token_id: 'tok-abc',
  slug: 'paris-rain',
  bids: [
    { price: 0.55, size: 100, total: 100 },
    { price: 0.54, size: 200, total: 300 },
  ],
  asks: [
    { price: 0.56, size: 80, total: 80 },
    { price: 0.57, size: 120, total: 200 },
  ],
  mid: 0.555,
  spread: 0.01,
  best_bid: 0.55,
  best_ask: 0.56,
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
    json: async () => ({}),
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('DepthChartModal', () => {
  it('renders nothing when tokenId is null', () => {
    render(
      <DepthChartModal tokenId={null} slug={null} onClose={vi.fn()} />,
    )
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders without crashing when a tokenId is provided', async () => {
    // First fetch (depth) succeeds; second fetch (ML pred) returns not-ok.
    apiFetchMock
      .mockResolvedValueOnce(mockOk(sampleDepth))
      .mockResolvedValue(mockNotOk(500))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
  })

  it('renders the "Order Book Depth" header with the slug', async () => {
    apiFetchMock
      .mockResolvedValueOnce(mockOk(sampleDepth))
      .mockResolvedValue(mockNotOk(500))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    // The header text is split across a parent span ("Order Book Depth: ")
    // and a child span ("paris-rain"); use a function matcher so the
    // regex matches the combined textContent of the title element.
    expect(
      await screen.findByText((_content, element) => {
        return (
          element?.getAttribute('id') === 'depth-modal-title' &&
          /Order Book Depth:.*paris-rain/.test(element.textContent || '')
        )
      }),
    ).toBeInTheDocument()
  })

  it('renders the manual paper trade form labels', async () => {
    apiFetchMock
      .mockResolvedValueOnce(mockOk(sampleDepth))
      .mockResolvedValue(mockNotOk(500))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    expect(
      await screen.findByText(/Manual Paper Trade Execution/),
    ).toBeInTheDocument()
    // Limit price + order size labels.
    expect(screen.getByText('Limit Price ($0.01 – $0.99)')).toBeInTheDocument()
    expect(screen.getByText('Order Size ($ USDC · Max $3)')).toBeInTheDocument()
  })

  it('renders "No active bids" / "No active asks" when the depth fetch fails', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    await waitFor(() => {
      expect(screen.getByText('No active bids')).toBeInTheDocument()
      expect(screen.getByText('No active asks')).toBeInTheDocument()
    })
  })

  it('calls onClose when the close (✕) button is clicked', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    apiFetchMock
      .mockResolvedValueOnce(mockOk(sampleDepth))
      .mockResolvedValue(mockNotOk(500))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={onClose} />,
    )
    await screen.findByRole('dialog')
    await user.click(
      screen.getByRole('button', { name: /close market depth modal/i }),
    )
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when Escape is pressed', async () => {
    const onClose = vi.fn()
    const user = userEvent.setup()
    apiFetchMock
      .mockResolvedValueOnce(mockOk(sampleDepth))
      .mockResolvedValue(mockNotOk(500))
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={onClose} />,
    )
    await screen.findByRole('dialog')
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the success feedback banner when a trade POST succeeds', async () => {
    const user = userEvent.setup()
    // URL-aware mock implementation keeps the test deterministic even when
    // depth @ 2s and ML pred @ 5s polling timers fire between mount and
    // the simulated click — a chained ``mockResolvedValueOnce`` queue
    // would otherwise be consumed by the polls.
    apiFetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url)
      if (u.includes('/api/depth/')) return Promise.resolve(mockOk(sampleDepth))
      if (u.includes('/api/ai/predict/')) return Promise.resolve(mockNotOk(500))
      if (u.includes('/api/trade') && init?.method === 'POST') {
        return Promise.resolve(
          mockOk({ detail: 'Order filled: BUY $1.5 @ 0.555' }),
        )
      }
      return Promise.resolve(mockNotOk(500))
    })
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: /Place BUY Order/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Order filled: BUY \$1.5 @ 0.555/),
      ).toBeInTheDocument()
    })
  })

  it('shows the error feedback banner when a trade POST fails', async () => {
    const user = userEvent.setup()
    // Use URL-aware mock implementation so polling timers (depth @ 2s, ML
    // pred @ 5s) don't consume the one-shot trade-POST rejection. The
    // chained ``mockResolvedValueOnce`` / ``mockRejectedValueOnce`` queue
    // is fragile under polling — a `mockImplementation` keyed on URL +
    // method gives the test deterministic control of every call.
    apiFetchMock.mockImplementation((url: string, init?: RequestInit) => {
      const u = String(url)
      if (u.includes('/api/depth/')) return Promise.resolve(mockOk(sampleDepth))
      if (u.includes('/api/ai/predict/')) return Promise.resolve(mockNotOk(500))
      if (u.includes('/api/trade') && init?.method === 'POST') {
        return Promise.reject(new Error('Network error'))
      }
      return Promise.resolve(mockNotOk(500))
    })
    render(
      <DepthChartModal tokenId="tok-abc" slug="paris-rain" onClose={vi.fn()} />,
    )
    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: /Place BUY Order/i }))
    await waitFor(() => {
      // The feedback banner renders with a leading ⚠️ emoji, so use a
      // regex substring match instead of an exact-text match.
      expect(
        screen.getByText(/Network error submitting trade/),
      ).toBeInTheDocument()
    })
  })
})
