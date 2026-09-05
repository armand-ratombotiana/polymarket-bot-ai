// components/EquityCurve.test.tsx — Equity Curve panel tests.
//
// W22-1 — covers the contract surfaces for the previously-untested
// EquityCurve panel:
//   • Renders the "Equity Curve" / "Portfolio Equity" title.
//   • Shows the loading state initially while the first fetch resolves.
//   • Fetches /api/history/equity on mount.
//   • Renders the chart once data arrives (≥2 points).
//   • Renders the "Accumulating paper execution points" empty-state when
//     fewer than 2 points are returned.
//   • W22-1: shows the error banner when the fetch returns HTTP 500.
//   • W22-1: shows the error banner when the fetch throws a network error.
//   • W22-1: dismisses the error banner when the Dismiss button is clicked.
//
// W22-5 — extends the suite to cover the useRealtimeData migration:
//   • Renders "Polling" badge by default (WS not yet open).
//   • Flips to "Live" badge when the WS opens.
//   • Accepts metrics-channel WS payloads shaped like { points: [] }.
//   • Drops mismatched metrics-channel payloads (BotSnapshot fallback).
//   • Ignores WS messages on channels it did not subscribe to.
//
// Strategy mirrors OrdersPanel.test.tsx + DatabaseStatusPanel.test.tsx
// (MockWebSocket stub + per-test fetch overrides + act() for
// triggerOpen / triggerMessage).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'

// Mock EquityCurveChart so we don't pull in Recharts in unit tests.
vi.mock('@/components/charts', () => ({
  EquityCurveChart: ({ data }: { data: unknown[] }) => (
    <div data-testid="equity-curve-chart-mock" data-point-count={data.length}>
      chart-mock
    </div>
  ),
}))

import EquityCurve from './EquityCurve'

// W22-5 — MockWebSocket stub. The panel now opens a real WS via
// useRealtimeData → useWebSocket; without this stub, jsdom attempts an
// actual ws://localhost:8080/ws connection that errors on every test.
class MockWebSocket {
  static instances: MockWebSocket[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  url: string
  readyState: number
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: ((event: unknown) => void) | null = null

  constructor(url: string) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    MockWebSocket.instances.push(this)
  }

  triggerOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.()
  }

  triggerMessage(data: unknown) {
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    this.onmessage?.({ data: payload })
  }

  triggerClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  send() {}
}

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleEquity = {
  points: [
    { timestamp: 1700000000000, equity: 100.0, pnl: 0.0 },
    { timestamp: 1700000010000, equity: 100.42, pnl: 0.42 },
    { timestamp: 1700000020000, equity: 101.18, pnl: 1.18 },
    { timestamp: 1700000030000, equity: 100.95, pnl: 0.95 },
  ],
}

const emptyEquity = { points: [] }
const onePointEquity = {
  points: [{ timestamp: 1700000000000, equity: 100.0, pnl: 0.0 }],
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

describe('EquityCurve', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    // W22-5 — install MockWebSocket so useRealtimeData's internal
    // useWebSocket() call doesn't attempt a real ws:// connection.
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
    vi.useRealTimers()
  })

  // ─────────────────────────────────────────────────────────────────────
  // Rendering
  // ─────────────────────────────────────────────────────────────────────

  it('renders the "Equity Curve" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<EquityCurve />)
    expect(screen.getByText(/Equity Curve/i)).toBeInTheDocument()
  })

  it('shows the loading state initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<EquityCurve />)
    expect(
      screen.getByText(/Loading equity timeline/i),
    ).toBeInTheDocument()
  })

  it('fetches /api/history/equity on mount', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(screen.getByTestId('equity-curve-chart-mock')).toBeInTheDocument()
    })
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const urls = calls.map(([url]) => url)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/history/equity')),
    ).toBe(true)
  })

  it('renders the chart once data arrives (≥2 points)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(screen.getByTestId('equity-curve-chart-mock')).toBeInTheDocument()
    })
    // Footer summary line — equity values formatted as $X.XX.
    expect(screen.getByText(/Base: \$100\.00/i)).toBeInTheDocument()
  })

  it('renders the "Accumulating paper execution points" empty-state when fewer than 2 points are returned', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(onePointEquity))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Accumulating paper execution points/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Accumulating paper execution points" empty-state when points is empty', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyEquity))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Accumulating paper execution points/i),
      ).toBeInTheDocument()
    })
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed all fetch errors via
  // `} catch { /* empty */ }`. The W22-1 fix surfaces them via an inline
  // dismissable banner so the trader knows the timeline is stale.
  //
  // W22-5 — the panel now uses useRealtimeData, which sets `error` to
  // the underlying failure string ("HTTP 500" / network message). The
  // panel wraps that with the "Failed to load equity timeline (…)"
  // prefix so the W22-1 banner text contract continues to match.

  it('W22-1: shows the error banner with HTTP status when /api/history/equity returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load equity timeline \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    // The banner has a Dismiss control.
    expect(
      screen.getByRole('button', { name: /Dismiss equity error/i }),
    ).toBeInTheDocument()
  })

  it('W22-1: shows the error banner when the fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: dismisses the error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load equity timeline/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss equity error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load equity timeline/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: re-shows the banner when a fresh error arrives after dismissal', async () => {
    // First fetch fails — banner appears, user dismisses it.
    // Second fetch (polling tick) also fails with a different message —
    // banner should re-appear with the new message.
    vi.mocked(fetch).mockImplementationOnce(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        json: async () => ({}),
      } as Response),
    )
    vi.mocked(fetch).mockImplementationOnce(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 503,
        json: async () => ({}),
      } as Response),
    )
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
    render(<EquityCurve />)
    await waitFor(() => {
      expect(
        screen.getByText(/Failed to load equity timeline \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss equity error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/Failed to load equity timeline/i),
      ).not.toBeInTheDocument()
    })
  })

  it('polls /api/history/equity on a fallback cadence (5 s) when the WS is not connected', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
    render(<EquityCurve />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByTestId('equity-curve-chart-mock')).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)
    // W22-5 — pollInterval was relaxed from 3s to 5s when migrating to
    // useRealtimeData. Advance 5 s; expect exactly one additional call.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
    const { unmount } = render(<EquityCurve />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-5 — Realtime migration tests
  // ─────────────────────────────────────────────────────────────────────

  describe('W22-5: realtime migration', () => {
    it('renders the "Polling" badge by default (WS not yet open)', async () => {
      render(<EquityCurve />)
      // Wait for the accumulating-state placeholder (initial REST fetch
      // resolved with [] — the beforeEach set up a bare vi.fn() with no
      // resolved value, so we explicitly mock the next call).
      vi.mocked(fetch).mockImplementationOnce(mockFetchOk(emptyEquity))
      await screen.findByText(/Accumulating paper execution points/)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('flips to the "Live" badge when the WS connects', async () => {
      vi.mocked(fetch).mockImplementation(mockFetchOk(emptyEquity))
      render(<EquityCurve />)
      await screen.findByText(/Accumulating paper execution points/)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      expect(await screen.findByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('accepts a metrics-channel WS payload shaped like { points: [] }', async () => {
      // The metrics channel pushes the full BotSnapshot by default — the
      // `validate` predicate drops those. When an equity-shaped payload
      // arrives (has `points` array), the hook accepts it and the panel
      // renders the new chart.
      vi.mocked(fetch).mockImplementation(mockFetchOk(emptyEquity))
      render(<EquityCurve />)
      // Initially empty (REST returned []).
      await screen.findByText(/Accumulating paper execution points/)
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'metrics',
          data: sampleEquity,
        })
      })
      // After the WS push, the chart mock should render with 4 points.
      const chart = await screen.findByTestId('equity-curve-chart-mock', {}, { timeout: 3000 })
      expect(chart).toHaveAttribute('data-point-count', '4')
    })

    it('drops a metrics-channel WS payload that does NOT look like { points: [] } (BotSnapshot fallback)', async () => {
      // The metrics channel pushes the full BotSnapshot by default. The
      // `validate` predicate must drop those so the typed state isn't
      // clobbered with mismatched fields. After dropping, the panel
      // should still render the accumulating-state placeholder.
      vi.mocked(fetch).mockImplementation(mockFetchOk(emptyEquity))
      render(<EquityCurve />)
      await screen.findByText(/Accumulating paper execution points/)
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'metrics',
          // A typical BotSnapshot payload — has no `points` field.
          data: {
            type: 'snapshot',
            timestamp: Date.now() / 1000,
            mode: 'paper',
            kill_switch: false,
            daily_pnl: 0,
            paper_balance: 100,
          },
        })
      })
      // Still the accumulating-state placeholder, not a clobbered snapshot.
      expect(screen.getByText(/Accumulating paper execution points/)).toBeInTheDocument()
    })

    it('ignores WS messages on channels it did not subscribe to', async () => {
      vi.mocked(fetch).mockImplementation(mockFetchOk(emptyEquity))
      render(<EquityCurve />)
      await screen.findByText(/Accumulating paper execution points/)
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      // Push a message on the wrong channel — data should be unchanged.
      await act(async () => {
        ws.triggerMessage({
          channel: 'orders',
          data: { orders: [{ order_id: 'X' }] },
        })
      })
      // Still the accumulating-state placeholder.
      expect(screen.getByText(/Accumulating paper execution points/)).toBeInTheDocument()
    })

    it('falls back to the REST response when the WS is not connected', async () => {
      // Override the default mock for this test — return 4 points from
      // the REST endpoint. The panel should render the chart.
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleEquity))
      render(<EquityCurve />)
      await screen.findByTestId('equity-curve-chart-mock', {}, { timeout: 3000 })
      // Still polling because the WS hasn't been triggered open.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
    })
  })
})
