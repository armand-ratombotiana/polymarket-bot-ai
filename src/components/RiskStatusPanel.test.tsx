// components/RiskStatusPanel.test.tsx — W30-2 panel tests.
//
// Strategy:
//   * RiskStatusPanel mounts and fires two `apiFetch` calls in
//     parallel (`GET /api/status` + `GET /api/risk/reconcile`) on
//     mount. The `apiFetch` wrapper hits `global.fetch` with the
//     gateway port appended — so we mock `global.fetch` directly.
//   * Loading state shows "Loading institutional risk telemetry…".
//   * Error state shows the "Risk & Exposure" header + "Unavailable"
//     badge + "Risk engine offline or starting up." copy.
//   * Success state shows the "INSTITUTIONAL RISK & RECONCILIATION"
//     header plus the mode badge, reconciled badge, and capital
//     allocation meter.
//
// What's covered:
//   1. Renders the loading banner on first paint.
//   2. Renders the "Unavailable" error banner when fetch rejects.
//   3. Renders the "INSTITUTIONAL RISK & RECONCILIATION" header once
//      data loads.
//   4. Renders the mode badge (PAPER / LIVE / SHADOW) from the status
//      payload.
//   5. Renders the reconciled badge ("✓ Reconciled" / "⚠ Discrepancy")
//      based on the recon payload.
//   6. Renders the "Capital Allocation" meter with invested dollars.
//   7. Passes the Authorization header via apiFetch.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import RiskStatusPanel from './RiskStatusPanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

const sampleRiskStatus = {
  mode: 'paper',
  observation_only: false,
  observation_reason: '',
  exposure_reconciled: true,
  bankroll_ceiling: 100,
  deployable_ceiling: 60,
  total_exposure: 12.34,
  max_total_exposure: 25,
  max_position_per_market: 3,
  dynamic_risk_multiplier: 1.0,
  effective_max_position_per_market: 3.0,
  daily_pnl: 0,
  daily_loss_limit: 2,
  max_loss_if_all_zero: 12.34,
  kill_switch: false,
  paper_balance: 100,
  open_orders: 0,
}

const sampleRecon = {
  reconciled: true,
  status: 'ok',
  findings: [],
  exposure: {
    capital_invested: 12.34,
    reserved_for_pending_orders: 0,
    net_directional_exposure: 12.34,
    maximum_remaining_loss: 12.34,
    exposure_dollar_days: 14.8,
    exposure_per_group: {},
    exposure_per_strategy: { signal_trader: 12.34 },
    available_cash: 87.66,
  },
}

function mockFetchOk(status: unknown = sampleRiskStatus, recon: unknown = sampleRecon) {
  return vi.fn().mockImplementation((input: string | URL | Request) => {
    const url = typeof input === 'string' ? input : ''
    const payload = url.includes('/api/risk/reconcile') ? recon : status
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => payload,
    } as Response)
  })
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('RiskStatusPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the loading banner on first paint', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RiskStatusPanel />)
    expect(
      screen.getByText(/Loading institutional risk telemetry/i),
    ).toBeInTheDocument()
  })

  it('renders the "Unavailable" error banner when fetch rejects', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('Unavailable')).toBeInTheDocument()
    })
    expect(
      screen.getByText(/Risk engine offline or starting up/i),
    ).toBeInTheDocument()
  })

  it('renders the "INSTITUTIONAL RISK & RECONCILIATION" header once data loads', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/INSTITUTIONAL RISK & RECONCILIATION/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the PAPER mode badge from the status payload', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText('PAPER')).toBeInTheDocument()
    })
  })

  it('renders the "✓ Reconciled" badge when recon returns reconciled=true', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText(/✓ Reconciled/)).toBeInTheDocument()
    })
  })

  it('renders the "⚠ Discrepancy" badge when recon returns reconciled=false', async () => {
    vi.mocked(global.fetch).mockImplementation(
      mockFetchOk(sampleRiskStatus, { ...sampleRecon, reconciled: false }),
    )
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText(/⚠ Discrepancy/)).toBeInTheDocument()
    })
  })

  it('renders the "Capital Allocation" meter with invested dollars', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(screen.getByText(/Capital Allocation/i)).toBeInTheDocument()
    })
    expect(screen.getByText(/\$12\.34 deployed/)).toBeInTheDocument()
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<RiskStatusPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
