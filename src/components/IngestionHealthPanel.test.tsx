// components/IngestionHealthPanel.test.tsx — Data Ingestion Health panel tests (W31-5)
//
// Covers the seven contract surfaces required by the W31-5 spec:
//   1. Initial loading skeleton renders before the first fetch resolves.
//   2. Source health grid — three source cards (CLOB / Gamma / WebSocket),
//      each with its connection status badge + EPS / failed / error-rate.
//   3. Ingestion metrics — total events, events/min, avg latency,
//      data freshness, throughput trend (sparkline).
//   4. Data quality scores — overall score + validation pass rate +
//      duplicate rate + stale rate + invalid records.
//   5. Dead-letter queue — depth, recent failed records table, error
//      reasons breakdown bars, retry button.
//   6. Data gaps — detected gaps timeline with duration + affected markets.
//   7. Coverage — markets tracked / recent / stale / coverage %.
//   8. Auto-refresh — polls every 15 s and updates the rendered numbers
//      when a new payload arrives.
//   9. Hard-error state (no data yet) shows the retry affordance.
//
// Strategy (mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx):
//   • Stub `global.fetch` per-test via `vi.mocked(fetch).mockImplementation`.
//   • The component's fetches go through `apiFetch` (which wraps `fetch`
//     and adds an Authorization header). Mocking `global.fetch` directly
//     is sufficient because apiFetch ultimately calls it.
//   • Mock `recharts.ResponsiveContainer` (jsdom doesn't fire
//     ResizeObserver callbacks) — same pattern as RateLimitPanel.
//   • For initial-render assertions use real timers + `waitFor`.
//   • For polling assertions use `vi.useFakeTimers()` +
//     `await act(async () => { await vi.advanceTimersByTimeAsync(N) })`.
//   • Use `getAllByText` for values that appear in multiple DOM nodes.

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

import IngestionHealthPanel from './IngestionHealthPanel'

// ── Sample payloads ─────────────────────────────────────────────────────────

const baseHealthPayload = {
  sources: [
    {
      id: 'clob',
      name: 'CLOB Order Book Poller',
      status: 'connected',
      last_event_at: Math.floor(Date.now() / 1000) - 5,
      events_per_second: 12.34,
      failed_records: 2,
      error_rate: 0.005,
    },
    {
      id: 'gamma',
      name: 'Gamma Markets API',
      status: 'connected',
      last_event_at: Math.floor(Date.now() / 1000) - 30,
      events_per_second: 0,
      failed_records: 0,
      error_rate: 0,
    },
    {
      id: 'websocket',
      name: 'WebSocket Stream',
      status: 'reconnecting',
      last_event_at: Math.floor(Date.now() / 1000) - 60,
      events_per_second: 0,
      failed_records: 3,
      error_rate: 0,
    },
  ],
  metrics: {
    total_events: 84521,
    events_per_minute: 1234.5,
    avg_latency_ms: 42.3,
    data_freshness_seconds: 5.2,
    throughput_trend: [10.1, 11.4, 12.8, 12.34, 13.0, 12.34],
  },
  generated_at: Math.floor(Date.now() / 1000),
}

const baseQualityPayload = {
  overall_score: 94.5,
  validation_pass_rate: 0.96,
  duplicate_rate: 0.004,
  stale_rate: 0.02,
  invalid_records: 3,
  checks: [
    { name: 'market_data_freshness', status: 'pass', detail: '5.2s old' },
    { name: 'tracked_markets_count', status: 'pass', detail: '40 markets' },
  ],
  generated_at: Math.floor(Date.now() / 1000),
}

const baseDeadLetterPayload = {
  depth: 3,
  recent: [
    {
      id: 'market_snapshots-1730000000',
      source: 'timescale_db',
      timestamp: Math.floor(Date.now() / 1000) - 120,
      payload_summary: 'market_snapshots failed inserts',
      error: 'sqlite3.OperationalError: database is locked',
      retries: 0,
    },
    {
      id: 'orderbook_ticks-1730000050',
      source: 'timescale_db',
      timestamp: Math.floor(Date.now() / 1000) - 60,
      payload_summary: 'orderbook_ticks failed inserts',
      error: 'asyncpg.exceptions.UniqueViolationError: duplicate key',
      retries: 0,
    },
    {
      id: 'fundamental_news-1730000100',
      source: 'timescale_db',
      timestamp: Math.floor(Date.now() / 1000) - 30,
      payload_summary: 'fundamental_news failed inserts',
      error: 'sqlite3.OperationalError: database is locked',
      retries: 0,
    },
  ],
  error_breakdown: [
    { reason: 'sqlite3.OperationalError: database is locked', count: 2 },
    { reason: 'asyncpg.exceptions.UniqueViolationError: duplicate key', count: 1 },
  ],
  generated_at: Math.floor(Date.now() / 1000),
}

const baseCoveragePayload = {
  markets_tracked: 42,
  markets_recent: 38,
  markets_stale: 4,
  coverage_pct: 90.5,
  stale_markets: [
    {
      token_id: '0xabc123',
      slug: 'will-x-happen',
      last_update: Math.floor(Date.now() / 1000) - 300,
    },
    {
      token_id: '0xdef456',
      slug: 'market-y',
      last_update: Math.floor(Date.now() / 1000) - 600,
    },
  ],
  generated_at: Math.floor(Date.now() / 1000),
}

const baseGapsPayload = {
  gaps: [
    {
      id: 'clob-gap-1730000000',
      source: 'clob',
      start: Math.floor(Date.now() / 1000) - 300,
      end: Math.floor(Date.now() / 1000),
      duration_seconds: 300.0,
      affected_markets: ['0xabc123', '0xdef456'],
    },
  ],
  generated_at: Math.floor(Date.now() / 1000),
}

// Empty-state payloads — used for "no failures / no gaps / no stale" assertions.
const emptyDeadLetterPayload = {
  depth: 0,
  recent: [],
  error_breakdown: [],
  generated_at: Math.floor(Date.now() / 1000),
}

const emptyGapsPayload = {
  gaps: [],
  generated_at: Math.floor(Date.now() / 1000),
}

const emptyCoveragePayload = {
  markets_tracked: 0,
  markets_recent: 0,
  markets_stale: 0,
  coverage_pct: 0.0,
  stale_markets: [],
  generated_at: Math.floor(Date.now() / 1000),
}

const dlqRetrySuccessPayload = {
  success: true,
  retried: 3,
  message: 'cleared 3 failed-insert counter(s)',
  attempted_at: Math.floor(Date.now() / 1000),
}

// ── Fetch mock helpers ───────────────────────────────────────────────────────

// Build a fetch mock that returns the supplied payload for any of the
// five ingestion endpoints. Route by URL substring since each endpoint
// has a different shape.
function mockFetchAllIngestion(payloads: {
  health?: unknown
  quality?: unknown
  deadLetter?: unknown
  coverage?: unknown
  gaps?: unknown
}) {
  return vi.fn().mockImplementation((input: string) => {
    const url = typeof input === 'string' ? input : String(input)
    let payload: unknown = null
    if (url.includes('/api/ingestion/health')) payload = payloads.health
    else if (url.includes('/api/ingestion/quality')) payload = payloads.quality
    else if (url.includes('/api/ingestion/dead-letter/retry')) {
      // POST retry handled separately by mockFetchRouteGetPost below.
      payload = dlqRetrySuccessPayload
    } else if (url.includes('/api/ingestion/dead-letter')) payload = payloads.deadLetter
    else if (url.includes('/api/ingestion/coverage')) payload = payloads.coverage
    else if (url.includes('/api/ingestion/gaps')) payload = payloads.gaps
    return Promise.resolve({
      ok: payload !== undefined && payload !== null,
      status: payload !== undefined && payload !== null ? 200 : 404,
      json: async () => payload ?? {},
    } as Response)
  })
}

function mockFetchNotOk(status = 500, statusText = 'Internal Server Error') {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText,
    json: async () => ({}),
  } as Response)
}

// Fetch mock that returns one payload for GETs and another for POSTs (for
// the dead-letter retry button test).
function mockFetchRouteGetPost(
  getPayloads: {
    health?: unknown
    quality?: unknown
    deadLetter?: unknown
    coverage?: unknown
    gaps?: unknown
  },
  postPayload: unknown,
) {
  return vi.fn().mockImplementation((input: string, init?: RequestInit) => {
    const url = typeof input === 'string' ? input : String(input)
    const method = init?.method ?? 'GET'
    if (method === 'POST') {
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => postPayload,
      } as Response)
    }
    let payload: unknown = null
    if (url.includes('/api/ingestion/health')) payload = getPayloads.health
    else if (url.includes('/api/ingestion/quality')) payload = getPayloads.quality
    else if (url.includes('/api/ingestion/dead-letter')) payload = getPayloads.deadLetter
    else if (url.includes('/api/ingestion/coverage')) payload = getPayloads.coverage
    else if (url.includes('/api/ingestion/gaps')) payload = getPayloads.gaps
    return Promise.resolve({
      ok: payload !== undefined && payload !== null,
      status: payload !== undefined && payload !== null ? 200 : 404,
      json: async () => payload ?? {},
    } as Response)
  })
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('IngestionHealthPanel', () => {
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
    render(<IngestionHealthPanel />)
    expect(
      screen.getByText('Loading Ingestion Health…'),
    ).toBeInTheDocument()
    // The panel exposes a status role while loading.
    expect(document.querySelector('.spinner')).toBeTruthy()
  })

  // ── Source health grid ──────────────────────────────────────────────────

  it('renders three source cards (CLOB / Gamma / WebSocket) with status badges', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByText('Source Health')).toBeInTheDocument()
    })
    // All three source cards render.
    expect(screen.getByText('CLOB Order Book Poller')).toBeInTheDocument()
    expect(screen.getByText('Gamma Markets API')).toBeInTheDocument()
    expect(screen.getByText('WebSocket Stream')).toBeInTheDocument()
    // Connection status badges.
    expect(screen.getByTestId('source-status-reconnecting')).toBeInTheDocument()
    // The "connected" badge appears twice (CLOB + Gamma).
    const connectedBadges = screen.getAllByTestId('source-status-connected')
    expect(connectedBadges.length).toBe(2)
  })

  it('renders per-source EPS, failed records, and error rate values', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('source-eps-clob')).toBeInTheDocument()
    })
    // CLOB events/sec = 12.34 → "12.3" (formatRate rounds to 1dp for values ≥ 10).
    expect(screen.getByTestId('source-eps-clob').textContent).toBe('12.3')
    // CLOB failed_records = 2 → "2"
    expect(screen.getByTestId('source-failed-clob').textContent).toBe('2')
    // CLOB error_rate = 0.005 (0.5%) → "0.50%"
    expect(screen.getByTestId('source-error-rate-clob').textContent).toBe('0.50%')
    // Gamma failed_records = 0 (green).
    expect(screen.getByTestId('source-failed-gamma').textContent).toBe('0')
    // WebSocket failed_records = 3 (reconnects).
    expect(screen.getByTestId('source-failed-websocket').textContent).toBe('3')
  })

  it('renders the empty state when no sources are reported', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: { sources: [], metrics: baseHealthPayload.metrics, generated_at: Math.floor(Date.now() / 1000) },
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByText(/No ingestion sources reported/)).toBeInTheDocument()
    })
  })

  // ── Ingestion metrics KPI cards ──────────────────────────────────────────

  it('renders the four ingestion-metrics KPI cards with the supplied values', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('kpi-total-events')).toBeInTheDocument()
    })
    // total_events = 84521 → "84,521"
    expect(screen.getByTestId('kpi-total-events').textContent).toContain('84,521')
    // events_per_minute = 1234.5 → "1,235"
    expect(screen.getByTestId('kpi-events-per-minute').textContent).toContain('1,235')
    // avg_latency_ms = 42.3 → "42ms"
    expect(screen.getByTestId('kpi-avg-latency').textContent).toContain('42ms')
    // data_freshness_seconds = 5.2 → "5s"
    expect(screen.getByTestId('kpi-data-freshness').textContent).toContain('5s')
  })

  it('renders the throughput sparkline when a trend is supplied', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('throughput-card')).toBeInTheDocument()
    })
    // The Recharts mock renders an rc-responsive div.
    expect(screen.getAllByTestId('rc-responsive').length).toBeGreaterThan(0)
  })

  // ── Data quality scores ─────────────────────────────────────────────────

  it('renders the five quality-score fields with the supplied values', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('quality-overall')).toBeInTheDocument()
    })
    // overall_score = 94.5 → "94.5%"
    expect(screen.getByTestId('quality-overall').textContent).toBe('94.5%')
    // validation_pass_rate = 0.96 → "96.0%"
    expect(screen.getByTestId('quality-validation').textContent).toBe('96.0%')
    // duplicate_rate = 0.004 → "0.40%"
    expect(screen.getByTestId('quality-duplicate').textContent).toBe('0.40%')
    // stale_rate = 0.02 → "2.00%"
    expect(screen.getByTestId('quality-stale').textContent).toBe('2.00%')
    // invalid_records = 3 → "3"
    expect(screen.getByTestId('quality-invalid').textContent).toBe('3')
  })

  it('renders the quality-score badge in the card header', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('quality-score-badge')).toBeInTheDocument()
    })
    expect(screen.getByTestId('quality-score-badge').textContent).toContain('94.5%')
  })

  it('shows the quality-card "endpoint unavailable" fallback when quality payload is null', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: null,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Quality endpoint unavailable/),
      ).toBeInTheDocument()
    })
  })

  // ── Dead-letter queue ───────────────────────────────────────────────────

  it('renders the dead-letter depth and recent failed records table', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('dead-letter-card')).toBeInTheDocument()
    })
    // depth: 3 → "depth: 3"
    expect(screen.getByText(/depth:/).textContent).toContain('3')
    // Recent records table renders — the same error text appears in
    // BOTH the table row (with title attr) AND the breakdown bar, so
    // use getAllByText.
    expect(
      screen.getAllByText(/sqlite3\.OperationalError: database is locked/).length,
    ).toBeGreaterThanOrEqual(1)
    expect(
      screen.getAllByText(/asyncpg\.exceptions\.UniqueViolationError: duplicate key/).length,
    ).toBeGreaterThanOrEqual(1)
  })

  it('renders the error-reasons breakdown bars', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('dlq-breakdown')).toBeInTheDocument()
    })
    // The breakdown renders the reason text — appears in BOTH the
    // breakdown bar AND the table, so use getAllByText.
    expect(
      screen.getAllByText('sqlite3.OperationalError: database is locked').length,
    ).toBeGreaterThanOrEqual(1)
    expect(
      screen.getAllByText('asyncpg.exceptions.UniqueViolationError: duplicate key').length,
    ).toBeGreaterThanOrEqual(1)
    // The "2" count appears in the breakdown.
    expect(screen.getAllByText('2').length).toBeGreaterThanOrEqual(1)
  })

  it('renders the "No failed records" empty state when the DLQ is empty', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: emptyDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/No failed records in the dead-letter queue/),
      ).toBeInTheDocument()
    })
  })

  it('fires POST /api/ingestion/dead-letter/retry when the Retry button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(
        {
          health: baseHealthPayload,
          quality: baseQualityPayload,
          deadLetter: baseDeadLetterPayload,
          coverage: baseCoveragePayload,
          gaps: baseGapsPayload,
        },
        dlqRetrySuccessPayload,
      ),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('dead-letter-card')).toBeInTheDocument()
    })
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      screen.getByTestId('dlq-retry-button').click()
      // Flush the POST fetch microtask.
      await Promise.resolve()
      await Promise.resolve()
    })
    const callsAfter = vi.mocked(fetch).mock.calls.length
    expect(callsAfter).toBeGreaterThan(callsBefore)
    // At least one call used POST.
    const postCalls = vi.mocked(fetch).mock.calls.filter((c) => {
      const init = c[1] as RequestInit | undefined
      return init?.method === 'POST'
    })
    expect(postCalls.length).toBeGreaterThanOrEqual(1)
    const postUrl = postCalls[0][0] as string
    expect(postUrl).toContain('/api/ingestion/dead-letter/retry')
  })

  it('shows a success banner after the dead-letter retry succeeds', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteGetPost(
        {
          health: baseHealthPayload,
          quality: baseQualityPayload,
          deadLetter: baseDeadLetterPayload,
          coverage: baseCoveragePayload,
          gaps: baseGapsPayload,
        },
        dlqRetrySuccessPayload,
      ),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('dead-letter-card')).toBeInTheDocument()
    })
    await act(async () => {
      screen.getByTestId('dlq-retry-button').click()
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()
    })
    await waitFor(() => {
      expect(screen.getByTestId('dlq-retry-result')).toBeInTheDocument()
    })
    expect(
      screen.getByTestId('dlq-retry-result').textContent,
    ).toContain('cleared 3 failed-insert counter(s)')
  })

  // ── Data gaps ───────────────────────────────────────────────────────────

  it('renders the data-gap timeline when gaps are present', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('gaps-card')).toBeInTheDocument()
    })
    // The gap row renders the source badge ("clob") and the duration ("5.0m" for 300s).
    expect(screen.getByTestId('gap-row-0')).toBeInTheDocument()
    expect(screen.getByText('clob')).toBeInTheDocument()
    // 300s → "5.0m"
    expect(screen.getByText('5.0m')).toBeInTheDocument()
  })

  it('renders the "No data gaps detected" empty state when no gaps', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: emptyGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/No data gaps detected in the active window/),
      ).toBeInTheDocument()
    })
  })

  // ── Coverage ────────────────────────────────────────────────────────────

  it('renders the four coverage KPI fields with the supplied values', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('coverage-tracked')).toBeInTheDocument()
    })
    // markets_tracked = 42 → "42"
    expect(screen.getByTestId('coverage-tracked').textContent).toBe('42')
    // markets_recent = 38 → "38"
    expect(screen.getByTestId('coverage-recent').textContent).toBe('38')
    // markets_stale = 4 → "4"
    expect(screen.getByTestId('coverage-stale').textContent).toBe('4')
    // coverage_pct = 90.5 → "90.5%"
    expect(screen.getByTestId('coverage-pct').textContent).toBe('90.5%')
  })

  it('renders the stale-markets list when stale markets exist', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByText('will-x-happen')).toBeInTheDocument()
    })
    expect(screen.getByText('market-y')).toBeInTheDocument()
  })

  it('renders the coverage-card "endpoint unavailable" fallback when coverage is null', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: null,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Coverage endpoint unavailable/),
      ).toBeInTheDocument()
    })
  })

  it('renders zero-state coverage when no markets are tracked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: emptyCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('coverage-pct').textContent).toBe('0.0%')
    })
    expect(screen.getByTestId('coverage-tracked').textContent).toBe('0')
  })

  // ── Hard-error state ────────────────────────────────────────────────────

  it('renders the hard-error state with retry button when fetch returns 500', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Ingestion health endpoint unavailable'),
      ).toBeInTheDocument()
    })
    // The ErrorState's retry button uses the aria-label "Retry ingestion health fetch".
    expect(
      screen.getByRole('button', { name: /retry ingestion health fetch/i }),
    ).toBeInTheDocument()
  })

  it('renders the hard-error state when fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Ingestion health endpoint unavailable'),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Network error: ECONNREFUSED/),
    ).toBeInTheDocument()
  })

  // ── Manual refresh button ────────────────────────────────────────────────

  it('renders the manual Refresh button in the header', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /refresh ingestion health/i }),
      ).toBeInTheDocument()
    })
  })

  it('manual Refresh button triggers an additional fetch', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Source Health')).toBeInTheDocument()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      screen.getByRole('button', { name: /refresh ingestion health/i }).click()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  // ── Auto-refresh polling ────────────────────────────────────────────────

  it('polls /api/ingestion/health every 15 s and updates the rendered total-events KPI', async () => {
    vi.useFakeTimers()
    // First payload: total_events = 84521.
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    // Flush the initial fetch + setState.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByTestId('kpi-total-events').textContent).toContain('84,521')
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(5) // five endpoints polled

    // Second payload: total_events = 99999.
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: { ...baseHealthPayload, metrics: { ...baseHealthPayload.metrics, total_events: 99999 } },
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    // Advance 15 s — should fire another batch of five polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000)
    })
    expect(screen.getByTestId('kpi-total-events').textContent).toContain('99,999')
    // The 15 s poll should have fired at least five more fetches (one
    // per ingestion endpoint).
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThanOrEqual(initialCallCount + 5)
  })

  it('passes the Authorization header via apiFetch on every poll', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByText('Source Health')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('does NOT poll when the tab is hidden', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Source Health')).toBeInTheDocument()

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
    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('clears the polling interval on unmount (no leaked setState warnings)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    const { unmount } = render(<IngestionHealthPanel />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Source Health')).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders the "15s poll" badge in the header', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchAllIngestion({
        health: baseHealthPayload,
        quality: baseQualityPayload,
        deadLetter: baseDeadLetterPayload,
        coverage: baseCoveragePayload,
        gaps: baseGapsPayload,
      }),
    )
    render(<IngestionHealthPanel />)
    await waitFor(() => {
      expect(screen.getByTestId('poll-badge')).toBeInTheDocument()
    })
    expect(screen.getByTestId('poll-badge').textContent).toContain('15s poll')
  })
})
