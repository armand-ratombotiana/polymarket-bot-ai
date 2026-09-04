// components/DatabaseStatusPanel.test.tsx — Database Status panel tests (W21-7)
//
// Covers the four contract surfaces required by the W21-7 spec:
//   1. Initial loading skeleton renders before the first fetch resolves.
//   2. SQLite backend rendering (fallback state — no pg_health block,
//      amber BackendBadge, "SQLite Fallbacks" KPI = 0).
//   3. PostgreSQL backend rendering (healthy state — green BackendBadge,
//      full PG health grid with uptime %, latency, pool size, etc.).
//   4. Manual retry button — clicking fires POST /api/system/db-retry
//      and surfaces the result banner.
//   5. Auto-refresh — polls /api/system/db-status every 15 s and
//      updates the rendered numbers when a new payload arrives.
//   6. Hard-error state (no data yet) shows the retry affordance.
//   7. Recent errors list — last 5 errors render in the error card.
//   8. Database tables grid — empty state + populated table render.
//
// Strategy (mirrors RateLimitPanel.test.tsx + AnalyticsPanel.test.tsx):
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • The component's fetches go through `apiFetch` (which wraps `fetch`
//     and adds an Authorization header). Mocking `global.fetch` directly
//     is sufficient because apiFetch ultimately calls it.
//   • For initial-render assertions use real timers + `waitFor` (the
//     default 1s polling tick that drives `waitFor` flushes the fetch
//     microtask + setState).
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })`.
//   • Use `getAllByText` for values that appear in multiple DOM nodes.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import DatabaseStatusPanel from './DatabaseStatusPanel'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sqlitePayload = {
  backend: 'sqlite',
  pg_health: null,
  fallback_counter: 0,
  tables: [
    {
      name: 'market_snapshots',
      row_count: 1245,
      size_mb: 2.3,
      database: 'sqlite',
      last_modified: Math.floor(Date.now() / 1000) - 60,
    },
    {
      name: 'orderbook_ticks',
      row_count: 8421,
      size_mb: 5.7,
      database: 'sqlite',
      last_modified: Math.floor(Date.now() / 1000) - 30,
    },
  ],
  recent_errors: [],
  generated_at: Math.floor(Date.now() / 1000),
}

const postgresPayload = {
  backend: 'postgresql',
  pg_health: {
    status: 'healthy',
    uptime_pct: 99.85,
    avg_latency_ms: 4.2,
    last_check_epoch: Math.floor(Date.now() / 1000) - 5,
    consecutive_failures: 0,
    pool_size: 10,
    pool_in_use: 3,
  },
  fallback_counter: 2,
  tables: [
    {
      name: 'market_snapshots',
      row_count: 98765,
      size_mb: 18.4,
      database: 'pg',
      last_modified: Math.floor(Date.now() / 1000) - 15,
    },
    {
      name: 'orderbook_ticks',
      row_count: 543210,
      size_mb: 47.2,
      database: 'pg',
      last_modified: Math.floor(Date.now() / 1000) - 5,
    },
    {
      name: 'fundamental_news',
      row_count: 432,
      size_mb: 0.8,
      database: 'pg',
      last_modified: Math.floor(Date.now() / 1000) - 120,
    },
  ],
  recent_errors: [
    {
      timestamp: Math.floor(Date.now() / 1000) - 300,
      error: 'asyncpg.exceptions.PostgresConnectionError: connection refused',
      retry_attempt: 1,
      backend: 'postgresql',
    },
    {
      timestamp: Math.floor(Date.now() / 1000) - 180,
      error: 'Connection pool exhausted — fallback to SQLite',
      retry_attempt: 2,
      backend: 'postgresql',
    },
  ],
  generated_at: Math.floor(Date.now() / 1000),
}

const degradedPayload = {
  ...postgresPayload,
  pg_health: {
    ...postgresPayload.pg_health,
    status: 'degraded',
    uptime_pct: 92.3,
    avg_latency_ms: 38.7,
    consecutive_failures: 2,
    pool_in_use: 9,
  },
  fallback_counter: 7,
}

const emptyPayload = {
  backend: 'sqlite',
  pg_health: null,
  fallback_counter: 0,
  tables: [],
  recent_errors: [],
  generated_at: Math.floor(Date.now() / 1000),
}

const retrySuccessPayload = {
  success: true,
  backend: 'postgresql',
  message: 'PG pool re-armed (10 connections healthy)',
  attempted_at: Math.floor(Date.now() / 1000),
}

const retryFailPayload = {
  success: false,
  backend: 'postgresql',
  message: 'PG still unreachable: connection refused',
  attempted_at: Math.floor(Date.now() / 1000),
}

// ── Fetch mock helpers ───────────────────────────────────────────────────────

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

function mockFetchNotOk(status = 500, statusText = 'Internal Server Error') {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText,
    json: async () => ({}),
  } as Response)
}

// Distinguish GET vs POST calls so the retry button test can return a
// different payload for POST /api/system/db-retry.
function mockFetchRouteGetPost(getPayload: unknown, postPayload: unknown) {
  return vi.fn().mockImplementation((_input: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET'
    if (method === 'POST') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => postPayload,
      } as Response)
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => getPayload,
    } as Response)
  })
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('DatabaseStatusPanel', () => {
  beforeEach(() => {
    // Re-install a fresh fetch mock before each test.
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // ── Initial loading state ─────────────────────────────────────────────

  it('renders the loading skeleton on first mount before data arrives', () => {
    // Never-resolving promise → loading stays true indefinitely.
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<DatabaseStatusPanel />)
    expect(
      screen.getByText('Loading Database Status…'),
    ).toBeInTheDocument()
    // The panel exposes a status role while loading.
    expect(document.querySelector('.spinner')).toBeTruthy()
  })

  // ── SQLite backend rendering ──────────────────────────────────────────

  it('renders the SQLite backend badge (amber) and "No fallbacks recorded" hint', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    // Header BackendBadge — SQLite amber.
    const badge = screen.getByTestId('db-backend-badge')
    expect(badge).toHaveTextContent('SQLite')
    expect(badge.className).toContain('bg-amber-500')
    // "SQLite" appears in the header badge + the Active Backend KPI +
    // once per SQLite table row — assert at least one render.
    const sqliteMatches = screen.getAllByText('SQLite')
    expect(sqliteMatches.length).toBeGreaterThanOrEqual(2)
    expect(
      screen.getByText('No fallbacks recorded'),
    ).toBeInTheDocument()
    // PG Connection Health card — when pg_health is null, the panel
    // shows the "PostgreSQL pool is not configured" note.
    expect(
      screen.getByText(/PostgreSQL pool is not configured/),
    ).toBeInTheDocument()
    // Retry PG Connection button is still rendered even on SQLite mode.
    expect(
      screen.getByRole('button', { name: /retry postgresql connection/i }),
    ).toBeInTheDocument()
  })

  it('renders the database tables table with row counts for SQLite tables', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('market_snapshots')).toBeInTheDocument()
    })
    expect(screen.getByText('orderbook_ticks')).toBeInTheDocument()
    // row_count 1245 → toLocaleString produces "1,245"
    expect(screen.getByText('1,245')).toBeInTheDocument()
    expect(screen.getByText('8,421')).toBeInTheDocument()
    // The "SQLite" per-table badge appears 2 times (one per table).
    const sqliteTableBadges = screen.getAllByText('SQLite')
    // 1 in the header + 2 in the table = at least 3.
    expect(sqliteTableBadges.length).toBeGreaterThanOrEqual(3)
  })

  it('shows the "No connection errors recorded" empty state when recent_errors is empty', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/No connection errors recorded/),
      ).toBeInTheDocument()
    })
  })

  // ── PostgreSQL backend rendering ──────────────────────────────────────

  it('renders the PostgreSQL backend badge (green) and full PG health grid', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(postgresPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    // Header BackendBadge — PostgreSQL green.
    const badge = screen.getByTestId('db-backend-badge')
    expect(badge).toHaveTextContent('PostgreSQL')
    expect(badge.className).toContain('bg-green-500')
    // PG Connection Health grid renders all five columns.
    expect(screen.getByText('Uptime')).toBeInTheDocument()
    expect(screen.getByText('Avg Latency')).toBeInTheDocument()
    expect(screen.getByText('Pool In-Use')).toBeInTheDocument()
    expect(screen.getByText('Consecutive Failures')).toBeInTheDocument()
    // uptime_pct 99.85 → "99.85%" (appears in BOTH the PG Uptime KPI
    // card AND the PG Connection Health grid).
    const uptimeMatches = screen.getAllByText('99.85%')
    expect(uptimeMatches.length).toBeGreaterThanOrEqual(1)
    // avg_latency_ms 4.2 → "4.2ms" (only in the grid).
    expect(screen.getByText('4.2ms')).toBeInTheDocument()
    // pool_in_use 3 / pool_size 10 → "3/10" (only in the grid).
    expect(screen.getByText('3/10')).toBeInTheDocument()
    // consecutive_failures 0 → "0" (only in the grid).
    expect(screen.getByText('0')).toBeInTheDocument()
    // HealthBadge "Healthy" text — appears in BOTH the HealthBadge
    // next to the card title AND the Status column in the grid.
    const healthyMatches = screen.getAllByText('Healthy')
    expect(healthyMatches.length).toBeGreaterThanOrEqual(1)
  })

  it('renders the SQLite Fallbacks KPI = 2 (amber because < 5)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(postgresPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      // fallback_counter = 2 → "2"
      expect(screen.getByText('Fallbacks to SQLite')).toBeInTheDocument()
    })
    expect(screen.getByText('2')).toBeInTheDocument()
  })

  it('renders the recent errors list with the last 5 errors', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(postgresPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(
          /asyncpg.exceptions.PostgresConnectionError: connection refused/,
        ),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Connection pool exhausted — fallback to SQLite/),
    ).toBeInTheDocument()
  })

  it('renders PG table rows with the "PG" per-table badge', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(postgresPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('fundamental_news')).toBeInTheDocument()
    })
    // Per-table PG badge should be present (3 tables × 1 = 3 occurrences).
    const pgBadges = screen.getAllByText('PG')
    expect(pgBadges.length).toBeGreaterThanOrEqual(3)
  })

  // ── Degraded PG state ─────────────────────────────────────────────────

  it('renders the "Degraded" health badge when pg_health.status is degraded', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(degradedPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      // "Degraded" appears in BOTH the HealthBadge next to the card
      // title AND the Status column in the PG health grid.
      const degradedMatches = screen.getAllByText('Degraded')
      expect(degradedMatches.length).toBeGreaterThanOrEqual(1)
    })
    // uptime 92.3 → "92.30%" appears in BOTH the PG Uptime KPI card AND
    // the PG Connection Health grid.
    const uptimeMatches = screen.getAllByText('92.30%')
    expect(uptimeMatches.length).toBeGreaterThanOrEqual(1)
    // fallback_counter 7 → red (≥ 5) — only in the KPI card.
    expect(screen.getByText('7')).toBeInTheDocument()
    // The first Degraded match (the HealthBadge) should carry the amber
    // variant class on its closest badge slot.
    const degradedBadge = screen.getAllByText('Degraded')[0].closest('[data-slot="badge"]')
    expect(degradedBadge).toBeTruthy()
    expect(degradedBadge?.className).toContain('bg-amber-500')
  })

  // ── Empty state ───────────────────────────────────────────────────────

  it('renders the empty state for tables when no table stats are reported', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(emptyPayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('No table statistics available'),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(/backend has not reported table-level row counts/),
    ).toBeInTheDocument()
  })

  // ── Hard-error state ──────────────────────────────────────────────────

  it('renders the hard-error state with retry button when fetch returns 500', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Database status endpoint unavailable'),
      ).toBeInTheDocument()
    })
    // The ErrorState's retry button uses the aria-label "Retry database status fetch".
    expect(
      screen.getByRole('button', { name: /retry database status fetch/i }),
    ).toBeInTheDocument()
  })

  it('renders the hard-error state when fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Database status endpoint unavailable'),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Network error: ECONNREFUSED/),
    ).toBeInTheDocument()
  })

  // ── Manual retry button ───────────────────────────────────────────────

  it('fires POST /api/system/db-retry when the Retry PG button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(sqlitePayload, retrySuccessPayload),
    )
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Click the Retry PG button.
    await act(async () => {
      screen
        .getByRole('button', { name: /retry postgresql connection/i })
        .click()
      // Flush the POST fetch microtask.
      await Promise.resolve()
      await Promise.resolve()
    })
    // The manual retry should fire at least one POST.
    const callsAfter = vi.mocked(fetch).mock.calls.length
    expect(callsAfter).toBeGreaterThan(callsBefore)
    // At least one call used method POST.
    const postCalls = vi.mocked(fetch).mock.calls.filter((c) => {
      const init = c[1] as RequestInit | undefined
      return init?.method === 'POST'
    })
    expect(postCalls.length).toBeGreaterThanOrEqual(1)
    // The POST URL should be /api/system/db-retry (with XTransformPort appended).
    const postUrl = postCalls[0][0] as string
    expect(postUrl).toContain('/api/system/db-retry')
  })

  it('shows a success banner after the retry succeeds', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(sqlitePayload, retrySuccessPayload),
    )
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    await act(async () => {
      screen
        .getByRole('button', { name: /retry postgresql connection/i })
        .click()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(
        screen.getByText(/PG pool re-armed/),
      ).toBeInTheDocument()
    })
  })

  it('shows a failure banner after the retry fails', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(sqlitePayload, retryFailPayload),
    )
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    await act(async () => {
      screen
        .getByRole('button', { name: /retry postgresql connection/i })
        .click()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(
        screen.getByText(/PG still unreachable: connection refused/),
      ).toBeInTheDocument()
    })
  })

  it('renders the manual Refresh button in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /refresh database status/i }),
      ).toBeInTheDocument()
    })
  })

  it('manual Refresh button triggers a fetch', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Click the header Refresh button (aria-label).
    await act(async () => {
      screen.getByRole('button', { name: /refresh database status/i }).click()
      await vi.advanceTimersByTimeAsync(0)
    })
    // The manual refresh should fire an additional fetch.
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  // ── Auto-refresh polling ──────────────────────────────────────────────
  //
  // These tests use `vi.useFakeTimers()` + `act(async () => await
  // vi.advanceTimersByTimeAsync(N))` because `waitFor` itself uses
  // setTimeout internally (which is faked).

  it('polls /api/system/db-status every 15 s and updates the fallback counter KPI', async () => {
    vi.useFakeTimers()
    // First payload: fallback_counter = 0.
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('No fallbacks recorded')).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)

    // Second payload: fallback_counter = 3.
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sqlitePayload, fallback_counter: 3 }),
    )
    // Advance 15 s — should fire one more poll.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })
    // KPI should now reflect "Fallbacks to SQLite" with value 3.
    expect(screen.getByText('Fallbacks to SQLite')).toBeInTheDocument()
    expect(screen.getByText('3')).toBeInTheDocument()
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)
  })

  it('passes the Authorization header via apiFetch on every poll', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('does NOT poll when the tab is hidden', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('No fallbacks recorded')).toBeInTheDocument()

    // Hide the tab before the next poll fires.
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    const callsBefore = vi.mocked(fetch).mock.calls.length
    // Advance 60 s — no polls should fire while hidden.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)

    // Restore tab visibility and verify polling resumes.
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
    })
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
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    const { unmount } = render(<DatabaseStatusPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Database Backend Status')).toBeInTheDocument()
    // Unmount should run the effect cleanup → clearInterval.
    expect(() => act(() => unmount())).not.toThrow()
    // Advance time after unmount — should not trigger any fetches.
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders the "15s poll" badge in the header', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sqlitePayload))
    render(<DatabaseStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('15s poll')).toBeInTheDocument()
    })
  })
})
