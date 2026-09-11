// components/MLValidationPanel.test.tsx — ML validation render tests (W28-3).
//
// Strategy:
//   * `MLValidationPanel` mounts with `loading=true` and fires three
//     parallel `apiFetch` calls (GET /api/ml/metrics, /api/ml/drift,
//     /api/ml/versions) on mount. While loading the panel renders its
//     static header (with title "ML Validation & Walk-Forward CV" and
//     the "/api/ml/metrics · /api/ml/drift · /api/ml/versions" footnote)
//     plus a Skeleton block.
//   * We mock `global.fetch` (already installed as `vi.fn()` in setup.ts)
//     per-test so cases are independent.
//
// What's covered:
//   1. Renders the panel container without crashing.
//   2. Renders the panel header title "ML Validation & Walk-Forward CV".
//   3. Renders the "governance + drift" badge.
//   4. Renders the three endpoint notes (metrics / drift / versions).
//   5. Renders without crashing when fetch never resolves (stays loading).
//   6. Renders the "ML validation backend unreachable" error state.
//   7. Renders the Refresh button.
//   8. Renders the Retrain button (operator action).
//   9. Passes the Authorization header via apiFetch on the initial poll.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import MLValidationPanel from './MLValidationPanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
// W28-1 — `mockFetchOk` removed (TS6133 — declared but never wired
// into a test; the panel's three endpoint tests below all use the
// URL-routing inline mockImplementation instead).
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

// Minimal payload that satisfies the panel's payload-shape guards so the
// loaded-state path renders without throwing.
const sampleMetrics = {
  brier_score: 0.182,
  roc_auc: 0.78,
  log_loss: 0.51,
  ece: 0.024,
  sharpe_ratio: 1.6,
  n_real_samples: 240,
  n_synthetic_samples: 5000,
  training_source: 'synthetic_v3',
  _last_trained: Math.floor(Date.now() / 1000) - 3600,
  model_version: '0xabc123',
  feature_importances: { edge: 0.45, conf: 0.3, liquidity: 0.25 },
  reliability_curve: [
    { bin_center: 0.1, empirical_freq: 0.12, count: 24 },
    { bin_center: 0.5, empirical_freq: 0.48, count: 50 },
    { bin_center: 0.9, empirical_freq: 0.91, count: 30 },
  ],
  drift: {
    psi: 0.08,
    ks_stat: 0.12,
    status: 'ok',
    rolling_brier: 0.18,
    ewma_brier: 0.19,
    window_samples: 200,
    outcome_samples: 80,
    history: [],
  },
}

const sampleDrift = {
  psi: 0.08,
  ks_stat: 0.12,
  rolling_brier: 0.18,
  ewma_brier: 0.19,
  status: 'ok',
  window_samples: 200,
  outcome_samples: 80,
  history: [],
}

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

// ── Tests ───────────────────────────────────────────────────────────────────
describe('MLValidationPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  it('renders the panel container without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<MLValidationPanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the panel header title "ML Validation & Walk-Forward CV"', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLValidationPanel />)
    expect(
      screen.getByText(/ML Validation & Walk-Forward CV/),
    ).toBeInTheDocument()
  })

  it('renders the "governance + drift" badge', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLValidationPanel />)
    expect(screen.getByText('governance + drift')).toBeInTheDocument()
  })

  it('renders the three endpoint notes (metrics / drift / versions)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLValidationPanel />)
    expect(screen.getByText('/api/ml/metrics')).toBeInTheDocument()
    expect(screen.getByText('/api/ml/drift')).toBeInTheDocument()
    expect(screen.getByText('/api/ml/versions')).toBeInTheDocument()
  })

  it('renders without crashing when the fetch never resolves (stays loading)', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<MLValidationPanel />)
    expect(container.firstChild).toBeTruthy()
    // Header is still visible during loading.
    expect(
      screen.getByText(/ML Validation & Walk-Forward CV/),
    ).toBeInTheDocument()
  })

  it('renders the "ML validation backend unreachable" error state when fetch throws', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<MLValidationPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('ML validation backend unreachable'),
      ).toBeInTheDocument()
    })
  })

  it('renders the Refresh button in the header', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLValidationPanel />)
    expect(
      screen.getByRole('button', { name: /refresh/i }),
    ).toBeInTheDocument()
  })

  it('renders the Retrain button (operator action) once metrics resolve', async () => {
    vi.mocked(global.fetch).mockImplementation((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : ''
      let payload: unknown = {}
      if (url.includes('/api/ml/metrics')) payload = sampleMetrics
      else if (url.includes('/api/ml/drift')) payload = sampleDrift
      else if (url.includes('/api/ml/versions')) payload = sampleVersions
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<MLValidationPanel />)
    // The "Retrain Now" button lives inside the loaded-state body, so we
    // wait for it to mount after the three parallel fetches resolve.
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retrain now/i }),
      ).toBeInTheDocument()
    })
  })

  it('renders the drift status badge ("Drift OK") once metrics resolve', async () => {
    vi.mocked(global.fetch).mockImplementation((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : ''
      let payload: unknown = {}
      if (url.includes('/api/ml/metrics')) payload = sampleMetrics
      else if (url.includes('/api/ml/drift')) payload = sampleDrift
      else if (url.includes('/api/ml/versions')) payload = sampleVersions
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<MLValidationPanel />)
    await waitFor(() => {
      // The drift-status badge carries the status label derived from the
      // drift payload's `status` field — "ok" → "OK" → "Drift OK · PSI …".
      expect(screen.getByText(/Drift OK/i)).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation((input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : ''
      let payload: unknown = {}
      if (url.includes('/api/ml/metrics')) payload = sampleMetrics
      else if (url.includes('/api/ml/drift')) payload = sampleDrift
      else if (url.includes('/api/ml/versions')) payload = sampleVersions
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => payload,
      } as Response)
    })
    render(<MLValidationPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
