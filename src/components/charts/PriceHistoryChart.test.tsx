// components/charts/PriceHistoryChart.test.tsx — Unit tests for the price history chart.
//
// Strategy (mirrors Charts.test.tsx):
//   • Mock `recharts.ResponsiveContainer` so children render directly.
//   • Mock the @/lib/api apiFetch helper so self-fetch mode resolves
//     with mock OHLCV bars without a real network round-trip.
//   • Verify the chart renders without crashing with mock price history.
//   • Verify the empty-state, loading, and error states each render
//     correctly.
//   • Verify the time-range selector buttons are present + clickable.
//   • Verify custom color overrides + showMarkers / showVolume toggles.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

// Stub ResponsiveContainer so children render directly with the height prop.
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  const Passthrough = ({ children, height, width }: any) => (
    <div
      data-testid="rc-responsive"
      style={{
        width: typeof width === 'number' ? `${width}px` : (width ?? '100%'),
        height: typeof height === 'number' ? `${height}px` : (height ?? '100%'),
      }}
    >
      {children}
    </div>
  )
  return {
    ...actual,
    ResponsiveContainer: Passthrough,
  }
})

// Mock the apiFetch helper so self-fetch mode resolves synchronously
// with our mock bars. We return a fake Response-like object that
// exposes .ok and .json().
const mockBars = [
  { timestamp: 1700000000, open: 0.50, high: 0.52, low: 0.49, close: 0.51, volume: 1000 },
  { timestamp: 1700000060, open: 0.51, high: 0.53, low: 0.50, close: 0.52, volume: 1200 },
  { timestamp: 1700000120, open: 0.52, high: 0.54, low: 0.51, close: 0.53, volume: 800 },
  { timestamp: 1700000180, open: 0.53, high: 0.55, low: 0.52, close: 0.54, volume: 1500 },
  { timestamp: 1700000240, open: 0.54, high: 0.56, low: 0.53, close: 0.55, volume: 1100 },
]

function makeOkResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => body,
  } as Response
}

vi.mock('@/lib/api', () => ({
  getApiUrl: () => 'http://test.local',
  apiFetch: vi.fn(async () => makeOkResponse({ bars: mockBars, synthetic: true })),
}))

// Import AFTER the mocks so PriceHistoryChart picks up the mocked
// ResponsiveContainer and apiFetch.
import PriceHistoryChart, {
  type PriceHistoryBar,
} from './PriceHistoryChart'

describe('PriceHistoryChart', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('renders the time-range selector with all 6 ranges', () => {
    render(<PriceHistoryChart bars={mockBars} />)
    expect(screen.getByTestId('range-btn-1m')).toBeInTheDocument()
    expect(screen.getByTestId('range-btn-5m')).toBeInTheDocument()
    expect(screen.getByTestId('range-btn-15m')).toBeInTheDocument()
    expect(screen.getByTestId('range-btn-1h')).toBeInTheDocument()
    expect(screen.getByTestId('range-btn-4h')).toBeInTheDocument()
    expect(screen.getByTestId('range-btn-1d')).toBeInTheDocument()
  })

  it('renders the chart with pre-fetched bars', () => {
    render(<PriceHistoryChart bars={mockBars} height={240} />)
    expect(screen.getByTestId('price-history-chart')).toBeInTheDocument()
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('renders an empty-state message when bars is empty and no tokenId', () => {
    render(<PriceHistoryChart bars={[]} />)
    expect(screen.getByTestId('price-history-empty')).toBeInTheDocument()
    expect(screen.getByText('No price history available')).toBeInTheDocument()
  })

  it('renders a loading state when tokenId is provided and bars are empty', async () => {
    render(<PriceHistoryChart tokenId="tok-1" />)
    // Initially the loading state renders, then apiFetch resolves and
    // the chart replaces it.
    expect(screen.getByTestId('price-history-loading')).toBeInTheDocument()
    expect(screen.getByText('Loading price history…')).toBeInTheDocument()
  })

  it('renders the chart after self-fetch resolves', async () => {
    render(<PriceHistoryChart tokenId="tok-1" />)
    // Wait for the apiFetch mock to resolve and the chart to render.
    await waitFor(() => {
      expect(screen.getByTestId('price-history-chart')).toBeInTheDocument()
    })
  })

  it('renders an error state when apiFetch rejects', async () => {
    const { apiFetch } = await import('@/lib/api')
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error('network'))
    render(<PriceHistoryChart tokenId="tok-1" />)
    await waitFor(() => {
      expect(screen.getByTestId('price-history-error')).toBeInTheDocument()
    })
    expect(screen.getByText(/Network error/)).toBeInTheDocument()
  })

  it('renders an error state when apiFetch returns HTTP 500', async () => {
    const { apiFetch } = await import('@/lib/api')
    vi.mocked(apiFetch).mockResolvedValueOnce({
      ok: false,
      status: 500,
      json: async () => ({}),
    } as Response)
    render(<PriceHistoryChart tokenId="tok-1" />)
    await waitFor(() => {
      expect(screen.getByTestId('price-history-error')).toBeInTheDocument()
    })
    expect(screen.getByText(/HTTP 500/)).toBeInTheDocument()
  })

  it('hides the time-range selector when showRangeSelector is false', () => {
    render(<PriceHistoryChart bars={mockBars} showRangeSelector={false} />)
    expect(screen.queryByTestId('range-btn-5m')).toBeNull()
  })

  it('renders without volume bars when showVolume is false', () => {
    render(<PriceHistoryChart bars={mockBars} showVolume={false} />)
    // Should still render the chart; we just verify the rc wrapper is
    // present and there's no crash.
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('renders without high/low markers when showMarkers is false', () => {
    render(<PriceHistoryChart bars={mockBars} showMarkers={false} />)
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('accepts a custom lineColor', () => {
    render(<PriceHistoryChart bars={mockBars} lineColor="#ff00ff" />)
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('accepts custom formatX / formatY functions', () => {
    render(
      <PriceHistoryChart
        bars={mockBars}
        formatX={(ts) => `x=${ts}`}
        formatY={(v) => `$${v.toFixed(2)}`}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('updates the active range button when clicked', () => {
    render(<PriceHistoryChart bars={mockBars} />)
    const btn15m = screen.getByTestId('range-btn-15m')
    // Initially 5m is active (default resolution).
    expect(screen.getByTestId('range-btn-5m').getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(btn15m)
    expect(btn15m.getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('range-btn-5m').getAttribute('aria-pressed')).toBe('false')
  })

  it('fires onResolutionChange callback when a range button is clicked', () => {
    const onRes = vi.fn()
    render(<PriceHistoryChart bars={mockBars} onResolutionChange={onRes} />)
    fireEvent.click(screen.getByTestId('range-btn-1h'))
    expect(onRes).toHaveBeenCalledWith('1h')
  })

  it('normalizes ms timestamps to seconds', () => {
    // Pass timestamps in ms (>1e12); the chart should normalize to seconds
    // internally. We just verify no crash and chart renders.
    const msBars: PriceHistoryBar[] = mockBars.map((b) => ({
      ...b,
      timestamp: b.timestamp * 1000,
    }))
    render(<PriceHistoryChart bars={msBars} />)
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('handles a single-bar dataset', () => {
    render(
      <PriceHistoryChart
        bars={[{ timestamp: 1700000000, open: 0.5, high: 0.5, low: 0.5, close: 0.5, volume: 100 }]}
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('handles bars with missing volume', () => {
    render(
      <PriceHistoryChart
        bars={[
          { timestamp: 1700000000, open: 0.5, high: 0.5, low: 0.5, close: 0.5 },
          { timestamp: 1700000060, open: 0.5, high: 0.51, low: 0.49, close: 0.51 },
        ]}
        showVolume
      />,
    )
    expect(screen.getByTestId('rc-responsive')).toBeInTheDocument()
  })

  it('is responsive (ResponsiveContainer with width="100%")', () => {
    render(<PriceHistoryChart bars={mockBars} />)
    const wrapper = screen.getByTestId('rc-responsive')
    expect((wrapper as HTMLElement).style.width).toBe('100%')
  })

  it('respects the height prop', () => {
    render(<PriceHistoryChart bars={mockBars} height={200} />)
    const wrapper = screen.getByTestId('rc-responsive')
    expect((wrapper as HTMLElement).style.height).toBe('200px')
  })
})
