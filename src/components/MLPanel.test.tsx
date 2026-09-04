// components/MLPanel.test.tsx — W30-2 panel tests.
//
// Strategy:
//   * MLPanel mounts and fires `apiFetch('/api/ml/metrics')` on mount.
//   * Loading state shows "Loading ML model…".
//   * Error state shows "Connecting to ML API…".
//   * Success state renders the "🤖 ML Ensemble" header plus the
//     drift-status badge, calibration badge, and Brier/AUC cards.
//
// What's covered:
//   1. Renders without crashing.
//   2. Renders the "🤖 ML Ensemble" header.
//   3. Renders the "Loading ML model…" loading state when fetch
//      never resolves.
//   4. Renders the "Connecting to ML API…" error state when fetch
//      rejects.
//   5. Renders the "Calibrated" badge once data loads successfully.
//   6. Renders the drift status icon (✅ / ⚠️ / 🚨).
//   7. Passes the Authorization header via apiFetch.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, cleanup } from '@testing-library/react'
import MLPanel from './MLPanel'

// ── Mock helpers ────────────────────────────────────────────────────────────
function mockFetchNever() {
  return vi.fn().mockImplementation(() => new Promise<Response>(() => {}))
}

function mockFetchReject(msg = 'Network error: ECONNREFUSED') {
  return vi.fn().mockRejectedValue(new Error(msg))
}

const sampleMLStatus = {
  model_type: 'CalibratedEnsemble',
  model_ready: true,
  model_version: 'v1.4.2',
  n_online_updates: 1234,
  last_trained: Math.floor(Date.now() / 1000),
  training_source: 'hybrid',
  n_real_samples: 800,
  n_synthetic_samples: 4200,
  brier_score: 0.1823,
  roc_auc: 0.781,
  ece: 0.021,
  feature_importances: {
    edge: 0.32,
    liquidity_usd: 0.21,
    spread: 0.14,
    confidence: 0.11,
    drift_psi: 0.08,
    age_sec: 0.07,
  },
  adaptive_weights: { rf: 0.3, gb: 0.3, sgd: 0.2, lgbm: 0.2 },
  meta_learner: {
    is_warm: true,
    n_updates: 142,
    buffer_size: 200,
    min_samples_required: 50,
  },
  drift: {
    psi: 0.08,
    ks_stat: 0.06,
    rolling_brier: 0.19,
    ewma_brier: 0.185,
    status: 'HEALTHY',
    window_samples: 200,
    outcome_samples: 142,
  },
}

function mockFetchOk(payload: unknown = sampleMLStatus) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

// ── Tests ───────────────────────────────────────────────────────────────────
describe('MLPanel', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders without crashing', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    const { container } = render(<MLPanel />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "🤖 ML Ensemble" header', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLPanel />)
    expect(screen.getByText(/🤖 ML Ensemble/i)).toBeInTheDocument()
  })

  it('renders the "Loading ML model…" state when fetch never resolves', () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchNever())
    render(<MLPanel />)
    expect(screen.getByText(/Loading ML model/i)).toBeInTheDocument()
  })

  it('renders the "Connecting to ML API…" state when fetch rejects', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchReject())
    render(<MLPanel />)
    await waitFor(() => {
      expect(screen.getByText(/Connecting to ML API/i)).toBeInTheDocument()
    })
  })

  it('renders the "Calibrated" badge once data loads successfully', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<MLPanel />)
    await waitFor(() => {
      expect(screen.getByText('Calibrated')).toBeInTheDocument()
    })
  })

  it('renders the ✅ drift-status icon when drift status is HEALTHY', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<MLPanel />)
    await waitFor(() => {
      expect(screen.getByText(/✅/)).toBeInTheDocument()
    })
  })

  it('renders the 🚨 drift-status icon when drift status is SIGNIFICANT_DRIFT', async () => {
    const drifted = {
      ...sampleMLStatus,
      drift: { ...sampleMLStatus.drift, status: 'SIGNIFICANT_DRIFT' },
    }
    vi.mocked(global.fetch).mockImplementation(mockFetchOk(drifted))
    render(<MLPanel />)
    await waitFor(() => {
      expect(screen.getByText(/🚨/)).toBeInTheDocument()
    })
  })

  it('passes the Authorization header via apiFetch on the initial poll', async () => {
    vi.mocked(global.fetch).mockImplementation(mockFetchOk())
    render(<MLPanel />)
    await waitFor(() => {
      expect(vi.mocked(global.fetch).mock.calls.length).toBeGreaterThanOrEqual(1)
    })
    const init = (vi.mocked(global.fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })
})
