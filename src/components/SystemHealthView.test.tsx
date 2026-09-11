// components/SystemHealthView.test.tsx — System Health panel tests (W22-1)
//
// W22-1 — covers the contract surfaces required by the W22-1 spec for the
// previously-untested SystemHealthView panel:
//   1. Renders the loading state initially while the first fetch resolves.
//   2. Renders the "Platform Subsystem Health & Process Telemetry" title
//      once data arrives.
//   3. Renders the four KPI cards (Poller Success Rate, Market DB Size,
//      Model Drift PSI, Feature Store Vectors) when data arrives.
//   4. Renders the Supervised Processes grid.
//   5. W22-1: shows the fetch-error banner with Retry/Dismiss controls when
//      the health endpoint returns HTTP 500.
//   6. W22-1: shows the fetch-error banner when the fetch throws a network
//      error.
//   7. W22-1: dismisses the error banner when the Dismiss button is clicked.
//   8. W22-1: refetches when the Retry button is clicked.
//   9. W22-1: logs the error to console.error (silent swallow removed).
//  10. Polls /api/system/health every 3 s.
//  11. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import SystemHealthView from './SystemHealthView'

// ── Sample payload ──────────────────────────────────────────────────────────

const sampleHealth = {
  status: 'HEALTHY',
  timestamp: Math.floor(Date.now() / 1000),
  poller: {
    tier1_tokens: 8,
    tier2_tokens: 12,
    total_tracked: 20,
    success_rate: 99.2,
    latency_ms: 42,
  },
  ml_engine: {
    active_version: 'v1.4.champion',
    brier_score: 0.1842,
    psi_drift: 0.0823,
    drift_status: 'HEALTHY',
  },
  market_db: {
    db_backend: 'SQLite',
    db_path: '/data/markets.db',
    size_mb: 12.5,
    snapshots_recorded: 1500,
    ticks_recorded: 12000,
    news_items_recorded: 240,
    ml_feature_vectors: 9800,
  },
  storage: {
    vector_index_size: 1000,
    audit_trail_backend: 'SQLite',
    market_intelligence_db: 'SQLite',
    state_persistence: 'SQLite',
  },
  services: [
    { name: 'Order Book Poller', status: 'RUNNING', frequency: '2s', port: 8000 },
    { name: 'Supervisor Watchdog', status: 'HEALTHY', frequency: '1s' },
  ],
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

describe('SystemHealthView', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders the loading state initially while the first fetch is in flight', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<SystemHealthView />)
    expect(
      screen.getByText(/Gathering pipeline health/i),
    ).toBeInTheDocument()
  })

  it('renders the "Platform Subsystem Health & Process Telemetry" title once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Platform Subsystem Health & Process Telemetry/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the four KPI cards once data arrives', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<SystemHealthView />)
    await waitFor(() => {
      // Poller Success Rate KPI — success_rate 99.2 → "99.2%"
      expect(screen.getByText(/Poller Success Rate/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/Market DB Size/i)).toBeInTheDocument()
    expect(screen.getByText(/Model Drift PSI/i)).toBeInTheDocument()
    expect(screen.getByText(/Feature Store Vectors/i)).toBeInTheDocument()
    expect(screen.getByText(/99\.2%/i)).toBeInTheDocument()
    expect(screen.getByText(/12\.5 MB/i)).toBeInTheDocument()
  })

  it('renders the Supervised Processes grid with each service name', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(screen.getByText('Order Book Poller')).toBeInTheDocument()
    })
    expect(screen.getByText('Supervisor Watchdog')).toBeInTheDocument()
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed all fetch errors via
  // `} catch {}` and showed only "System health telemetry endpoint
  // unavailable." with no underlying error message. The W22-1 fix
  // surfaces the underlying error inline with Retry/Dismiss controls.

  it('W22-1: shows the error banner with the underlying HTTP status when /api/system/health returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(
        screen.getByText(/System health endpoint unavailable \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    // The banner has both Retry and Dismiss controls.
    expect(screen.getByRole('button', { name: /retry health fetch/i })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /dismiss error/i })).toBeInTheDocument()
  })

  it('W22-1: shows the error banner when the fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: dismisses the error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(
        screen.getByText(/System health endpoint unavailable/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /dismiss error/i }))
    await waitFor(() => {
      expect(
        screen.queryByText(/System health endpoint unavailable/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: refetches when the Retry button is clicked', async () => {
    // First fetch fails, then subsequent fetches succeed.
    vi.mocked(fetch).mockImplementationOnce(mockFetchNotOk(500))
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(
        screen.getByText(/System health endpoint unavailable/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /retry health fetch/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Platform Subsystem Health & Process Telemetry/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: logs the fetch error to console.error (silent swallow removed)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNRESET'))
    render(<SystemHealthView />)
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining('[SystemHealthView]'),
        expect.any(Error),
      )
    })
    consoleErrorSpy.mockRestore()
  })

  it('polls /api/system/health every 3 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<SystemHealthView />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(
      screen.getByText(/Platform Subsystem Health & Process Telemetry/i),
    ).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleHealth))
    const { unmount } = render(<SystemHealthView />)
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
})
