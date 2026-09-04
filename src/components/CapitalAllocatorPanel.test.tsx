// components/CapitalAllocatorPanel.test.tsx — Capital allocator render tests (W28-3).
//
// Strategy:
//   * `CapitalAllocatorPanel` mounts with `loading=true` and fires three
//     parallel `apiFetch` calls (GET /api/positions/closed, then
//     /api/capital/allocation + /api/exposure) on mount. While loading the
//     panel renders its static header (with title "Capital Allocator" and
//     the "Michaelis-Menten" badge) plus a SkeletonState block in the body.
//   * We mock `global.fetch` per-test so cases are independent.
//
// What's covered:
//   1. Renders the panel container without crashing.
//   2. Renders the panel header title "Capital Allocator".
//   3. Renders the "Michaelis-Menten" badge.
//   4. Renders the Refresh button.
//   5. Renders the Config (edit allocator config) button.
//   6. Renders without crashing when fetch never resolves (stays loading).
//   7. Renders the "Allocator API unavailable" error banner when fetch throws.
//   8. Renders the Retry button inside the error banner.
//   9. Renders the "Edge → Size Saturating Curve" heading once data loads.
//  10. Passes the Authorization header via apiFetch on the initial poll.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import CapitalAllocatorPanel from './CapitalAllocatorPanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

// ── Sample payloads ────────────────────────────────────────────────────────
// Minimal positions list — empty array means the panel falls back to its
// default signal edge/confidence to drive the what-if allocation call.
const sampleClosedPositions = { positions: [] }

const sampleBreakdown = {
  strategy: 'signal_trader',
  edge: 0.05,
  confidence: 0.7,
  liquidity_usd: 100,
  existing_exposure_usd: 0,
  drawdown_usd: 0,
  strategy_performance: null,
  brier_override: null,
  model_brier: 0.18,
  size_usd: 1.5,
  cap_usd: 3.0,
  drawdown_limit_usd: 5.0,
  edge_k_m: 0.05,
  edge_v_max: 3.0,
  liquidity_k: 50,
  components: {
    raw_size: 3.0,
    confidence_mult: 0.7,
    calibration_mult: 1.0,
    drawdown_mult: 1.0,
    correlation_mult: 1.0,
    performance_mult: 1.0,
    liquidity_mult: 0.5,
    product_mult: 1.0,
  },
}

const sampleExposure = {
  capital_invested: 12.34,
  reserved_for_pending_orders: 0,
  gross_market_value: 12.34,
  net_directional_exposure: 12.34,
  maximum_remaining_loss: 12.34,
  exposure_per_group: {},
  exposure_per_strategy: { signal_trader: 12.34 },
  exposure_duration_hours_avg: 1.2,
  exposure_dollar_days: 14.8,
  available_cash: 187.66,
  reserved_cash: 0,
  open_position_count: 1,
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('CapitalAllocatorPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('renders the panel container without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<CapitalAllocatorPanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the panel header title "Capital Allocator"', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<CapitalAllocatorPanel />)
    expect(screen.getByText('Capital Allocator')).toBeInTheDocument()
  })

  it('renders the "Michaelis-Menten" badge', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<CapitalAllocatorPanel />)
    expect(screen.getByText('Michaelis-Menten')).toBeInTheDocument()
  })

  it('renders the Refresh button (aria-label "Refresh allocator data")', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<CapitalAllocatorPanel />)
    expect(
      screen.getByRole('button', { name: /refresh allocator data/i }),
    ).toBeInTheDocument()
  })

  it('renders the Config button (aria-label "Edit allocator config")', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<CapitalAllocatorPanel />)
    expect(
      screen.getByRole('button', { name: /edit allocator config/i }),
    ).toBeInTheDocument()
  })

  it('renders without crashing when the fetch never resolves (stays loading)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<CapitalAllocatorPanel />)
    expect(container.firstChild).toBeTruthy()
    // Header still renders during loading.
    expect(screen.getByText('Capital Allocator')).toBeInTheDocument()
  })

  it('renders the "Allocator API unavailable" error banner when fetch throws', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<CapitalAllocatorPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Allocator API unavailable'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Retry button inside the error banner', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<CapitalAllocatorPanel />)
    await waitFor(() => {
      expect(screen.getByText('Allocator API unavailable')).toBeInTheDocument()
    })
    // The error-state Retry button — label "Retry".
    expect(
      screen.getByRole('button', { name: /retry/i }),
    ).toBeInTheDocument()
  })

  it('renders the "Edge → Size Saturating Curve" heading once data loads', async () => {
    vi.mocked(global.fetch).mockImplementation((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : ''
      let payload: unknown = {}
      if (url.includes('/api/positions/closed')) payload = sampleClosedPositions
      else if (url.includes('/api/capital/allocation')) payload = sampleBreakdown
      else if (url.includes('/api/exposure')) payload = sampleExposure
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<CapitalAllocatorPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Edge → Size Saturating Curve/i),
      ).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : ''
      let payload: unknown = {}
      if (url.includes('/api/positions/closed')) payload = sampleClosedPositions
      else if (url.includes('/api/capital/allocation')) payload = sampleBreakdown
      else if (url.includes('/api/exposure')) payload = sampleExposure
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<CapitalAllocatorPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
