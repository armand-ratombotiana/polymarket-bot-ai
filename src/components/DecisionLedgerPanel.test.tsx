// components/DecisionLedgerPanel.test.tsx — W30-2 panel tests.
//
// Strategy:
//   * DecisionLedgerPanel mounts and fires `apiFetch('/api/decisions/rejected?limit=50')`
//     on mount. Polling is every 10s (paused when document is hidden).
//   * Loading state shows the "🧠 DECISION LEDGER" header + "Loading…" badge.
//   * Error state shows "Decision ledger unavailable" + Retry button.
//   * Success state shows the header + "Correlation Audit" badge + KPI
//     strip (Decisions / Avg Edge / Avg Conf / Top Reason).
//
// What's covered:
//   1. Renders without crashing.
//   2. Renders the "🧠 DECISION LEDGER" header.
//   3. Renders the "Loading…" badge on first paint.
//   4. Renders the "Decision ledger unavailable" error banner when fetch
//      rejects.
//   5. Renders the Retry button inside the error banner.
//   6. Renders the "Correlation Audit" badge once data loads.
//   7. Renders the "Decisions" KPI chip with the rejection count.
//   8. Passes the Authorization header via apiFetch.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import DecisionLedgerPanel from './DecisionLedgerPanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

const sampleDecisions = {
  count: 2,
  rejections: [
    {
      timestamp: Math.floor(Date.now() / 1000) - 60,
      decision_id: 'dec_001',
      token_id: 'tok_btc_100k_yes',
      strategy: 'signal_trader',
      predicted_edge: 0.042,
      confidence: 0.62,
      reason: 'low_confidence',
      market_mid: 0.42,
    },
    {
      timestamp: Math.floor(Date.now() / 1000) - 120,
      decision_id: 'dec_002',
      token_id: 'tok_eth_flip',
      strategy: 'signal_trader',
      predicted_edge: 0.018,
      confidence: 0.51,
      reason: 'wide_spread',
      market_mid: 0.55,
    },
  ],
}

function mockFetchOk(payload: unknown = sampleDecisions) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('DecisionLedgerPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<DecisionLedgerPanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "🧠 DECISION LEDGER" header', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<DecisionLedgerPanel />)
    expect(screen.getByText(/🧠 DECISION LEDGER/i)).toBeInTheDocument()
  })

  it('renders the "Loading…" badge on first paint', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<DecisionLedgerPanel />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders the "Decision ledger unavailable" error banner when fetch rejects', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<DecisionLedgerPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Decision ledger unavailable/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the Retry button inside the error banner', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<DecisionLedgerPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Correlation Audit" badge once data loads', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<DecisionLedgerPanel />)
    await waitFor(() => {
      expect(screen.getByText(/Correlation Audit/i)).toBeInTheDocument()
    })
  })

  it('renders the "Decisions" KPI chip with the rejection count', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<DecisionLedgerPanel />)
    await waitFor(() => {
      // The Decisions chip shows count "2".
      expect(screen.getByText('2')).toBeInTheDocument()
    })
    // The chip's label is "Decisions" (rendered as "Decisions:" inside
    // the StatChip — the trailing colon is appended by the StatChip
    // component's ``{label}:`` template). Match with a regex so the
    // assertion survives either rendering shape.
    expect(screen.getByText(/^Decisions:?$/)).toBeInTheDocument()
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<DecisionLedgerPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
