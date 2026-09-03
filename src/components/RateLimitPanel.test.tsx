// components/RateLimitPanel.test.tsx — Rate-limit analytics dashboard tests (W14-7)
//
// Covers the four user-facing contract surfaces:
//   1. Initial loading skeleton renders before the first fetch resolves.
//   2. KPI cards display the four headline values (total hits, hit rate,
//      top endpoint, top client) once data arrives.
//   3. Auto-refresh polls /api/rate-limit/stats every 30 s and updates
//      the rendered numbers when a new payload arrives.
//   4. Empty state (no hits in the last hour) shows the policy
//      reference badges instead of empty charts.
//   5. Hard error (no data yet) shows the retry affordance.
//
// Strategy:
//   • Mock `recharts.ResponsiveContainer` (jsdom doesn't fire
//     ResizeObserver callbacks, so the real container would never
//     measure its parent → children never render). Same pattern as
//     `charts/Charts.test.tsx`.
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • For initial-render assertions use real timers + `waitFor` (the
//     default 1 s polling-tick that drives `waitFor` is enough to flush
//     the fetch microtask + setState).
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })` to
//     drive the 30 s interval deterministically (waitFor itself relies
//     on setTimeout, which is faked — so it can't be mixed with fake
//     timers).
//   • Use `getAllByText` for values that appear in multiple DOM nodes
//     (the top endpoint is rendered both as a KPI value AND a table row).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'

// ── Recharts mock — must come BEFORE the component import ─────────────────────
vi.mock('recharts', async () => {
  const actual = await vi.importActual<typeof import('recharts')>('recharts')
  const Passthrough = ({ children, height, width }: any) => (
    <div
      data-testid="rc-responsive"
      style={{
        width: typeof width === 'number' ? `${width}px` : width ?? '100%',
        height: typeof height === 'number' ? `${height}px` : height ?? '100%',
      }}
    >
      {children}
    </div>
  )
  return { ...actual, ResponsiveContainer: Passthrough }
})

import RateLimitPanel from './RateLimitPanel'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleStats = {
  total_hits: 42,
  hits_per_minute_rate: 0.7,
  hits_by_endpoint: {
    '/api/orders': 20,
    '/api/markets': 12,
    '/api/live/enable': 6,
    '/api/trade': 4,
  },
  hits_by_client: {
    '127.0.0.1': 30,
    '10.0.0.5': 8,
    '203.0.113.9': 4,
  },
  hits_per_minute: {
    '1': 5, '5': 3, '10': 2, '20': 4, '30': 6, '45': 8, '60': 14,
  } as Record<string, number>,
  top_endpoints: {
    '/api/orders': 20,
    '/api/markets': 12,
    '/api/live/enable': 6,
    '/api/trade': 4,
  },
}

const emptyStats = {
  total_hits: 0,
  hits_per_minute_rate: 0,
  hits_by_endpoint: {},
  hits_by_client: {},
  hits_per_minute: {},
  top_endpoints: {},
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

describe('RateLimitPanel', () => {
  beforeEach(() => {
    // Re-install a fresh fetch mock before each test.
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders the loading skeleton on first mount before data arrives', () => {
    // Never-resolving promise → loading stays true indefinitely.
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<RateLimitPanel />)
    // The loading skeleton renders the panel header + spinner, no KPI labels.
    expect(screen.getByText('Rate Limits')).toBeInTheDocument()
    // Spinner element exists in loading state.
    expect(document.querySelector('.spinner')).toBeTruthy()
  })

  it('renders the four KPI cards after data loads', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('Total Hits (1h)')).toBeInTheDocument()
    })
    expect(screen.getByText('Hit Rate')).toBeInTheDocument()
    expect(screen.getByText('Top Endpoint')).toBeInTheDocument()
    expect(screen.getByText('Top Client')).toBeInTheDocument()
  })

  it('formats the total-hits KPI value as a localized integer', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      // total_hits = 42 → "42"
      expect(screen.getByText('42')).toBeInTheDocument()
    })
  })

  it('formats the hit-rate KPI as "X.XX / min"', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('0.70 / min')).toBeInTheDocument()
    })
  })

  it('renders the top endpoint name (appears in KPI + table — use getAllByText)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      // "/api/orders" appears in BOTH the Top Endpoint KPI card value
      // AND in the Top Rate-Limited Endpoints table row. The KPI
      // truncates to ≤ 18 chars but "/api/orders" is only 11 chars so
      // it shows in full. Use getAllByText to assert at least 1 render.
      const matches = screen.getAllByText('/api/orders')
      expect(matches.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('renders the top client IP (appears in KPI + table — use getAllByText)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      // "127.0.0.1" appears in BOTH the Top Client KPI card value AND
      // the Top Rate-Limited Clients table row.
      const matches = screen.getAllByText('127.0.0.1')
      expect(matches.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('renders the section headers for charts and tables', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('Hits by Endpoint')).toBeInTheDocument()
    })
    expect(screen.getByText('Hits per Minute (60m)')).toBeInTheDocument()
    expect(screen.getByText('Top Rate-Limited Endpoints')).toBeInTheDocument()
    expect(screen.getByText('Top Rate-Limited Clients')).toBeInTheDocument()
    expect(screen.getByText('Most-Requested Endpoints')).toBeInTheDocument()
    expect(screen.getByText('Rate-Limit Policy')).toBeInTheDocument()
  })

  it('renders the "30s poll" badge in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('30s poll')).toBeInTheDocument()
    })
  })

  it('renders the empty state when total_hits is 0 and hits_by_endpoint is empty', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('No rate-limit hits in the last hour'),
      ).toBeInTheDocument()
    })
    // Policy reference badges should be visible.
    expect(screen.getByText('Read: 120/min')).toBeInTheDocument()
    expect(screen.getByText('Write: 30/min')).toBeInTheDocument()
    expect(screen.getByText('Heavy: 5/min')).toBeInTheDocument()
  })

  it('renders the hard-error state with retry button when fetch fails', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Rate-limit stats endpoint unavailable'),
      ).toBeInTheDocument()
    })
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('renders the policy reference cards in the main panel', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('Read routes')).toBeInTheDocument()
    })
    expect(screen.getByText('Write routes')).toBeInTheDocument()
    expect(screen.getByText('Heavy routes')).toBeInTheDocument()
    expect(screen.getByText('Trade routes')).toBeInTheDocument()
    expect(screen.getByText('Arbitrage')).toBeInTheDocument()
    expect(screen.getByText('Live enable')).toBeInTheDocument()
  })

  it('shows endpoint counts in the Top Rate-Limited Endpoints table', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      // /api/orders → 20 hits — appears in the table.
      // Multiple elements may contain "20" (KPI hint, table row, bar),
      // so we assert at least one exists.
      const twenties = screen.getAllByText('20')
      expect(twenties.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('shows client IPs in the Top Rate-Limited Clients table', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      // 10.0.0.5 only appears in the table (not in KPI card), so it's unique.
      expect(screen.getByText('10.0.0.5')).toBeInTheDocument()
    })
  })

  // ── Auto-refresh polling ───────────────────────────────────────────────
  //
  // These tests use `vi.useFakeTimers()` + `act(async () => await
  // vi.advanceTimersByTimeAsync(N))` because:
  //   • `waitFor` itself uses setTimeout internally — when timers are
  //     faked, `waitFor` never gets a chance to re-check.
  //   • The async `apiFetch` Promise resolves on the microtask queue,
  //     so we need `advanceTimersByTimeAsync` (which awaits microtasks)
  //     instead of the sync `advanceTimersByTime`.

  it('polls /api/rate-limit/stats every 30 s and updates KPIs', async () => {
    vi.useFakeTimers()
    // First payload: total_hits = 42.
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    // KPI should now reflect the initial payload.
    expect(screen.getByText('42')).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)

    // Second payload: total_hits = 99.
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sampleStats, total_hits: 99, hits_per_minute_rate: 1.65 }),
    )

    // Advance 30 s — should fire one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000)
    })
    // KPI should now reflect the updated payload.
    expect(screen.getByText('99')).toBeInTheDocument()
    expect(screen.getByText('1.65 / min')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('does NOT poll when the tab is hidden', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('42')).toBeInTheDocument()

    // Hide the tab before the next poll fires.
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })

    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Advance 2 minutes — no polls should fire while hidden.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(120_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)

    // Restore tab visibility and verify polling resumes.
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    // Trigger the visibilitychange event so the effect handler runs.
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(0)
    })
    // The immediate refresh-on-regain should fire at least one fetch.
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('clears the polling interval on unmount (no leaked setState warnings)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    const { unmount } = render(<RateLimitPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('42')).toBeInTheDocument()
    // Unmount should run the effect cleanup → clearInterval.
    expect(() => act(() => unmount())).not.toThrow()
    // Advance time after unmount — should not throw "setState on
    // unmounted component" warnings or trigger any fetches.
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('manual Refresh button triggers a fetch', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('42')).toBeInTheDocument()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Click the Refresh button (aria-label, not visible label).
    await act(async () => {
      screen.getByRole('button', { name: /refresh rate-limit stats/i }).click()
      await vi.advanceTimersByTimeAsync(0)
    })
    // The manual refresh should fire an additional fetch.
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('passes the Authorization header via apiFetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleStats))
    render(<RateLimitPanel />)
    await waitFor(() => {
      expect(screen.getByText('42')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
