// components/LiveSafetyGatePanel.test.tsx — Live Safety Gate render tests (W28-3).
//
// Strategy:
//   * `LiveSafetyGatePanel` mounts with `loading=true` and fires three
//     parallel `apiFetch` calls on mount (GET /api/live/readiness,
//     /api/status, /api/audit/logs). While loading the panel renders its
//     skeleton with the "LIVE SAFETY GATE · §82" title.
//   * On fetch failure (or null readiness) it renders the error state
//     with the "Safety-gate endpoint unavailable" copy and a Retry button.
//   * On fetch success it renders the full panel: gate banner,
//     progress bar, "Run all checks" / "Force open" / "Force close" buttons.
//   * We mock `global.fetch` per-test so cases are independent.
//
// What's covered:
//   1. Renders the panel container without crashing.
//   2. Renders the "LIVE SAFETY GATE · §82" title (loading skeleton).
//   3. Renders the loading spinner (Loader2) while waiting.
//   4. Renders without crashing when the fetch never resolves (stays loading).
//   5. Renders the error state ("Safety-gate endpoint unavailable") on failure.
//   6. Renders the Retry button inside the error state.
//   7. Renders the "Run all checks" button once readiness resolves.
//   8. Renders the "Force open" + "Force close" buttons once readiness resolves.
//   9. Renders the OPEN badge when readiness.passed = true.
//  10. Passes the Authorization header via apiFetch on the initial poll.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import LiveSafetyGatePanel from './LiveSafetyGatePanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

// W28-1 — `mockFetchReject` removed (TS6133 — declared but never
// wired into a test; the panel's error-state test uses
// `mockFetchReadiness` which returns a 500 response rather than a
// rejected promise).

function mockFetchReadiness(status = 500) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    statusText: 'Internal Server Error',
    text: async () => '',
    json: async () => ({}),
  } as Response)
}

// Minimal readiness payload — 10 checks, all passing → gate is OPEN.
const sampleReadiness = {
  passed: true,
  checks: [
    { id: 'c1', name: 'Check One', passed: true, severity: 'BLOCKING', threshold: '>=1', value: { n: 5 }, detail: 'ok' },
    { id: 'c2', name: 'Check Two', passed: true, severity: 'BLOCKING', threshold: '>=1', value: { n: 5 }, detail: 'ok' },
  ],
  passed_count: 2,
  total_count: 2,
  blocking_checks: [],
  checked_at: Math.floor(Date.now() / 1000),
}

const sampleModeStatus = {
  mode: 'paper',
  kill_switch: false,
  kill_switch_durable: false,
  live_trading_enabled: false,
  paper_trade: true,
}

const sampleAuditLogs = { logs: [] }

function mockFetchOkAll() {
  return vi.fn().mockImplementation((input: string) => {
    const url = typeof input === 'string' ? input : ''
    let payload: unknown = {}
    if (url.includes('/api/live/readiness')) payload = sampleReadiness
    else if (url.includes('/api/status')) payload = sampleModeStatus
    else if (url.includes('/api/audit/logs')) payload = sampleAuditLogs
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => payload,
      text: async () => '',
    } as Response)
  })
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('LiveSafetyGatePanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('renders the panel container without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<LiveSafetyGatePanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "LIVE SAFETY GATE · §82" title in the loading skeleton', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<LiveSafetyGatePanel />)
    expect(screen.getByText('LIVE SAFETY GATE · §82')).toBeInTheDocument()
  })

  it('renders the loading spinner (Loader2 with animate-spin) while waiting', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<LiveSafetyGatePanel />)
    // The loading skeleton header carries an `animate-spin` svg spinner.
    const spinners = document.querySelectorAll('.animate-spin')
    expect(spinners.length).toBeGreaterThanOrEqual(1)
  })

  it('renders without crashing when the fetch never resolves (stays loading)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<LiveSafetyGatePanel />)
    expect(container.firstChild).toBeTruthy()
    expect(screen.getByText('LIVE SAFETY GATE · §82')).toBeInTheDocument()
  })

  it('renders the error state ("Safety-gate endpoint unavailable") on a 500 response', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReadiness(500))
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Safety-gate endpoint unavailable/),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Unavailable" badge alongside the error state', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReadiness(500))
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(screen.getByText('Unavailable')).toBeInTheDocument()
    })
  })

  it('renders the Retry button inside the error state', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReadiness(500))
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Run all checks" button once readiness resolves', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /run all checks/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Force open" and "Force close" buttons once readiness resolves', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /force open/i }),
      ).toBeInTheDocument()
    })
    expect(
      screen.getByRole('button', { name: /force close/i }),
    ).toBeInTheDocument()
  })

  it('renders the OPEN badge when readiness.passed = true', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(screen.getByText('OPEN')).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<LiveSafetyGatePanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
