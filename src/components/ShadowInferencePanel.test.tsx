// components/ShadowInferencePanel.test.tsx — Shadow inference render tests (W28-3).
//
// Strategy:
//   * `ShadowInferencePanel` mounts with `loading=true` and fires four
//     parallel `apiFetch` calls on mount (GET /api/ml/versions,
//     /api/shadow/trades, /api/shadow/comparison, /api/ml/metrics).
//     While loading the panel renders a skeleton with the "Shadow Inference"
//     title and a "Loading…" badge; once data arrives, the header switches
//     to "Shadow Inference + Counterfactual Journal".
//   * We mock `global.fetch` per-test so cases are independent.
//
// What's covered:
//   1. Renders the panel container without crashing.
//   2. Renders the loading-state header title "Shadow Inference".
//   3. Renders the "Loading…" badge while waiting for the first fetch.
//   4. Renders without crashing when the fetch never resolves (stays loading).
//   5. Renders the "Shadow Inference + Counterfactual Journal" header after data loads.
//   6. Renders the Refresh-now button (aria-label / title="Refresh now").
//   7. Renders the Live/Paused polling toggle button.
//   8. Renders the "Champion" badge once a champion model version is resolved.
//   9. Renders the "Unable to reach any shadow-inference backend" error message.
//  10. Passes the Authorization header via apiFetch on the initial poll.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import ShadowInferencePanel from './ShadowInferencePanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

// Minimal payloads — only what the panel needs to flip out of its loading
// skeleton. The versions response contains exactly one ACTIVE champion.
const sampleVersions = {
  active_version: '0xabc123',
  total_registered: 1,
  versions: [
    {
      version: '0xabc123',
      created_at: Math.floor(Date.now() / 1000) - 3600,
      brier_score: 0.182,
      roc_auc: 0.78,
      ece: 0.024,
      sharpe_ratio: 1.6,
      status: 'ACTIVE',
      n_samples: 5000,
      parameters: {},
      is_active: true,
    },
  ],
}

const sampleShadowTrades = { count: 0, trades: [] }
const sampleComparison = {
  shadow: { total_pnl: 0, win_rate: 0, n_trades: 0 },
  live: { total_pnl: 0, win_rate: 0, n_trades: 0 },
}
const sampleMlMetrics = {
  brier_score: 0.18,
  roc_auc: 0.78,
  log_loss: 0.51,
  ece: 0.024,
  sharpe_ratio: 1.6,
  reliability_curve: [],
}

function mockFetchOkAll() {
  return vi.fn().mockImplementation((input: string) => {
    const url = typeof input === 'string' ? input : ''
    let payload: unknown = {}
    if (url.includes('/api/ml/versions')) payload = sampleVersions
    else if (url.includes('/api/shadow/trades')) payload = sampleShadowTrades
    else if (url.includes('/api/shadow/comparison')) payload = sampleComparison
    else if (url.includes('/api/ml/metrics')) payload = sampleMlMetrics
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => payload,
    } as Response)
  })
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('ShadowInferencePanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('renders the panel container without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<ShadowInferencePanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the loading-state header title "Shadow Inference"', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<ShadowInferencePanel />)
    // The skeleton header shows "Shadow Inference" (without the
    // " + Counterfactual Journal" suffix that the loaded header carries).
    expect(screen.getByText('Shadow Inference')).toBeInTheDocument()
  })

  it('renders the "Loading…" badge while waiting for the first fetch', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<ShadowInferencePanel />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('renders without crashing when the fetch never resolves (stays loading)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<ShadowInferencePanel />)
    expect(container.firstChild).toBeTruthy()
    expect(screen.getByText('Shadow Inference')).toBeInTheDocument()
  })

  it('renders the "Shadow Inference + Counterfactual Journal" header after data loads', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Shadow Inference + Counterfactual Journal'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Refresh-now button (title="Refresh now") after data loads', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /refresh now/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the Live/Paused polling toggle button after data loads', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      // The toggle shows "Live" by default (polling=true) — the title
      // attribute carries the explanatory hint.
      const toggle = screen.getByTitle(/auto-refresh every 20s/i)
      expect(toggle).toBeInTheDocument()
      expect(toggle).toHaveTextContent('Live')
    })
  })

  it('renders the Champion badge once a champion model version is resolved', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      // The header badge shows "Champion: 0xabc123".
      expect(
        screen.getByText(/Champion: 0xabc123/),
      ).toBeInTheDocument()
    })
  })

  it('renders the "Unable to reach any shadow-inference backend" error message when all fetches throw', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Unable to reach any shadow-inference backend/),
      ).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOkAll())
    render(<ShadowInferencePanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
