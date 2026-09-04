// components/RetentionPanel.test.tsx — Data retention panel render tests (W28-3).
//
// Strategy:
//   * `RetentionPanel` mounts with `loading=true` and fires
//     `GET /api/system/health` via `apiFetch` on mount. While loading the
//     panel renders its static header (with title, refresh button, and
//     "Bounded-storage policy" badge) plus a SkeletonRows block in the body.
//   * We mock `global.fetch` (already installed as `vi.fn()` in setup.ts)
//     so the fetch never hits the network — for the "loads successfully"
//     test we return a minimal SystemHealth payload; for the others we
//     never-resolve the fetch to keep the panel in its loading state.
//
// What's covered:
//   1. Renders the panel container without crashing.
//   2. Renders the panel header title "Data Retention & Pruning".
//   3. Renders the "Bounded-storage policy" badge.
//   4. Renders the refresh button.
//   5. Renders the 60s poll interval note.
//   6. Renders the "POST /api/system/prune" endpoint note.
//   7. Renders without crashing when fetch never resolves (stays loading).
//   8. Renders the "Retention backend unreachable" error state when fetch throws.
//   9. Renders the table-prune targets after a successful fetch loads.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import RetentionPanel from './RetentionPanel'

// ── Mock payloads ──────────────────────────────────────────────────────────
// Minimal SystemHealth payload — the panel only reads `boot_time` for an
// uptime KPI; everything else is derived from local RETENTION_TARGETS.
const sampleHealth = {
  status: 'ok',
  boot_time: Math.floor(Date.now() / 1000) - 3600,
  version: '0.1.0-test',
  uptime_seconds: 3600,
}

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('RetentionPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('renders the panel container without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<RetentionPanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the panel header title "Data Retention & Pruning"', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RetentionPanel />)
    expect(
      screen.getByText(/Data Retention & Pruning/),
    ).toBeInTheDocument()
  })

  it('renders the "Bounded-storage policy" badge', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RetentionPanel />)
    expect(screen.getByText('Bounded-storage policy')).toBeInTheDocument()
  })

  it('renders the Refresh button in the header', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RetentionPanel />)
    expect(
      screen.getByRole('button', { name: /refresh/i }),
    ).toBeInTheDocument()
  })

  it('renders the "POST /api/system/prune" endpoint note', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RetentionPanel />)
    expect(screen.getByText(/POST \/api\/system\/prune/)).toBeInTheDocument()
  })

  it('renders the four-store horizons note (7d / 30d / 30d / 90d)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<RetentionPanel />)
    expect(
      screen.getByText(/7d \/ 30d \/ 30d \/ 90d horizons/),
    ).toBeInTheDocument()
  })

  it('renders the "Retention Policy by Store" table after the health fetch resolves', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<RetentionPanel />)
    await waitFor(() => {
      expect(screen.getByText('Retention Policy by Store')).toBeInTheDocument()
    })
  })

  it('renders the "Retention backend unreachable" error state when fetch throws', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<RetentionPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Retention backend unreachable'),
      ).toBeInTheDocument()
    })
  })

  it('renders without crashing when the health fetch never resolves (stays loading)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<RetentionPanel />)
    expect(container.firstChild).toBeTruthy()
    // Header is still visible during loading.
    expect(
      screen.getByText(/Data Retention & Pruning/),
    ).toBeInTheDocument()
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk(sampleHealth))
    render(<RetentionPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
