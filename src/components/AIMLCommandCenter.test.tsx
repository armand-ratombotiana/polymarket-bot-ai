// components/AIMLCommandCenter.test.tsx — AI / ML Command Center tests (W22-2)
//
// Covers the contract surfaces required by the W22-2 spec for the
// previously-untested AIMLCommandCenter panel:
//   1. Renders without crashing.
//   2. Renders the "AI / ML Quantitative Telemetry & Gated Model Registry" title.
//   3. Fetches /api/ml/metrics, /api/ml/registry, /api/ml/drift in parallel on mount.
//   4. Renders the 4-member ensemble weights strip (RF + GB + LightGBM + SGD).
//   5. Renders the four KPI cards (Brier, ROC-AUC, ECE, Drift) once data arrives.
//   6. Renders the 38-feature importance ranking once metrics arrive.
//   7. Renders the model registry lineage table when registry.versions > 0.
//   8. Hides the model registry when registry.versions is empty.
//   9. Fires POST /api/ml/retrain when the "Gated Retrain" button is clicked.
//  10. Fires GET /api/ai/search when the semantic search form is submitted.
//  11. Renders the "Meta-Learner Active" badge when drift.meta_learner.is_warm.
//  12. Polls every 3 s.
//  13. Unmounts cleanly without leaking setState.
//
// Strategy mirrors DatabaseStatusPanel.test.tsx + RateLimitPanel.test.tsx.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act, fireEvent } from '@testing-library/react'
import AIMLCommandCenter from './AIMLCommandCenter'

// ── Sample payloads ─────────────────────────────────────────────────────────

const sampleMetrics = {
  model_type: 'RandomForestQuantEnsemble',
  brier_score: 0.1842,
  roc_auc: 0.812,
  log_loss: 0.5123,
  ece: 0.0231,
  n_online_updates: 1247,
  last_trained: Math.floor(Date.now() / 1000) - 600,
  adaptive_weights: { rf: 0.42, gb: 0.31, sgd: 0.07, lgbm: 0.2 },
  feature_importances: {
    'microstructure.spread_pct': 0.184,
    'regime.volatility_30s': 0.121,
    'sentiment.score_60s': 0.095,
    'microstructure.ofi_5s': 0.087,
  },
  reliability_curve: [
    { bin_center: 0.1, empirical_freq: 0.12, count: 38 },
    { bin_center: 0.3, empirical_freq: 0.28, count: 65 },
    { bin_center: 0.5, empirical_freq: 0.52, count: 90 },
    { bin_center: 0.7, empirical_freq: 0.71, count: 80 },
    { bin_center: 0.9, empirical_freq: 0.88, count: 50 },
  ],
  model_ready: true,
}

const sampleRegistry = {
  active_version: 'v1.4.champion',
  versions: [
    {
      version: 'v1.4.champion',
      created_at: Math.floor(Date.now() / 1000) - 86400,
      brier_score: 0.1842,
      roc_auc: 0.812,
      ece: 0.0231,
      sharpe_ratio: 1.85,
      status: 'ACTIVE',
    },
    {
      version: 'v1.3.challenger',
      created_at: Math.floor(Date.now() / 1000) - 172800,
      brier_score: 0.2105,
      roc_auc: 0.785,
      ece: 0.0288,
      sharpe_ratio: 1.55,
      status: 'RETIRED',
    },
  ],
}

const sampleDrift = {
  psi: 0.0823,
  status: 'HEALTHY',
  window_samples: 1500,
  outcome_samples: 1450,
  threshold_moderate_psi: 0.15,
  threshold_critical_psi: 0.30,
  meta_learner: {
    is_warm: true,
    n_updates: 247,
    buffer_size: 1500,
    min_samples_required: 100,
  },
}

const sampleDriftWarm = {
  ...sampleDrift,
  meta_learner: { ...sampleDrift.meta_learner, is_warm: false },
}

const sampleSearchResults = {
  results: [
    { market: { title: 'Fed rate cut at March FOMC', slug: 'fed-rate-cut-march' }, score: 0.92 },
    { market: { title: 'Bitcoin above $100k by December', slug: 'bitcoin-100k-december' }, score: 0.78 },
  ],
}

// ── Fetch mock helpers ───────────────────────────────────────────────────────
//
// The panel makes three parallel fetches (metrics + registry + drift) and
// also a separate fetch for the semantic search. We route by URL fragment.

function mockFetchRouteByUrl(routes: Record<string, unknown>) {
  return vi.fn().mockImplementation((input: string) => {
    for (const [fragment, payload] of Object.entries(routes)) {
      if (typeof input === 'string' && input.includes(fragment)) {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => payload,
        } as Response)
      }
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      json: async () => ({}),
    } as Response)
  })
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('AIMLCommandCenter', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders without crashing', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { container } = render(<AIMLCommandCenter />)
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the "AI / ML Quantitative Telemetry & Gated Model Registry" title', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<AIMLCommandCenter />)
    expect(
      screen.getByText(/AI \/ ML Quantitative Telemetry & Gated Model Registry/i),
    ).toBeInTheDocument()
  })

  it('renders the "38-Feature Pipeline" badge in the header', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<AIMLCommandCenter />)
    // The text "38-Feature Pipeline" appears in BOTH the header badge AND
    // the card-title "📊 38-Feature Pipeline Importances" — use getAllByText.
    const matches = screen.getAllByText(/38-Feature Pipeline/i)
    expect(matches.length).toBeGreaterThanOrEqual(1)
  })

  it('fetches /api/ml/metrics, /api/ml/registry, AND /api/ml/drift on mount', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
    const urls = calls.map(([url]) => url)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/ml/metrics')),
    ).toBe(true)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/ml/registry')),
    ).toBe(true)
    expect(
      urls.some((u) => typeof u === 'string' && u.includes('/api/ml/drift')),
    ).toBe(true)
  })

  it('renders the 4-member ensemble weights strip (RF + GB + LightGBM + SGD)', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    expect(screen.getByText('Gradient Boost')).toBeInTheDocument()
    expect(screen.getByText('LightGBM')).toBeInTheDocument()
    expect(screen.getByText('Online SGD')).toBeInTheDocument()
    // weights.rf 0.42 → "42.0%"
    expect(screen.getByText('42.0%')).toBeInTheDocument()
    // weights.lgbm 0.2 → "20.0%"
    expect(screen.getByText('20.0%')).toBeInTheDocument()
  })

  it('renders the four KPI cards once data arrives (Brier, ROC-AUC, ECE, Drift)', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      // Brier Calibration Score KPI label.
      expect(screen.getByText(/Brier Calibration Score/i)).toBeInTheDocument()
    })
    // Brier score 0.1842 appears in BOTH the KPI strip AND the model registry
    // table (champion's brier_score is also 0.1842) — use getAllByText.
    const brierMatches = screen.getAllByText('0.1842')
    expect(brierMatches.length).toBeGreaterThanOrEqual(1)
    // ROC-AUC Power KPI — roc_auc 0.812 → "81.2%"
    expect(screen.getByText(/ROC-AUC Power/i)).toBeInTheDocument()
    // roc_auc value 0.812 appears in KPI ("81.2%") AND model registry ("81.2%")
    const rocMatches = screen.getAllByText('81.2%')
    expect(rocMatches.length).toBeGreaterThanOrEqual(1)
    // ECE KPI — ece 0.0231 → "0.0231"
    expect(screen.getByText(/Expected Calibration Error/i)).toBeInTheDocument()
    // ECE value 0.0231 appears in BOTH the KPI strip AND the registry table
    // (champion's ece is also 0.0231).
    const eceMatches = screen.getAllByText('0.0231')
    expect(eceMatches.length).toBeGreaterThanOrEqual(1)
    // Drift KPI — psi 0.0823 → "PSI: 0.0823"
    expect(screen.getByText(/Concept Drift Health/i)).toBeInTheDocument()
    expect(screen.getByText(/PSI: 0.0823/i)).toBeInTheDocument()
  })

  it('renders the 38-feature importance ranking once metrics arrive', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/38-Feature Pipeline Importances/i),
      ).toBeInTheDocument()
    })
    // Feature names are rendered as text nodes.
    expect(screen.getByText('microstructure.spread_pct')).toBeInTheDocument()
    expect(screen.getByText('regime.volatility_30s')).toBeInTheDocument()
    expect(screen.getByText('sentiment.score_60s')).toBeInTheDocument()
    // Feature importance values — imp 0.184 → "18.4%"
    expect(screen.getByText('18.4%')).toBeInTheDocument()
    expect(screen.getByText('12.1%')).toBeInTheDocument()
  })

  it('renders the model registry lineage table when versions are present', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/Champion\/Challenger Model Lineage/i),
      ).toBeInTheDocument()
    })
    expect(screen.getByText('v1.4.champion')).toBeInTheDocument()
    expect(screen.getByText('v1.3.challenger')).toBeInTheDocument()
    // Active version header — "Active: v1.4.champion"
    expect(screen.getByText(/Active: v1\.4\.champion/i)).toBeInTheDocument()
    // Version status badges.
    const activeBadges = screen.getAllByText('ACTIVE')
    expect(activeBadges.length).toBeGreaterThanOrEqual(1)
    const retiredBadges = screen.getAllByText('RETIRED')
    expect(retiredBadges.length).toBeGreaterThanOrEqual(1)
  })

  it('hides the model registry table when registry.versions is empty', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': { active_version: 'v1.0', versions: [] },
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    expect(
      screen.queryByText(/Champion\/Challenger Model Lineage/i),
    ).not.toBeInTheDocument()
  })

  it('fires POST /api/ml/retrain when the "Gated Retrain" button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
        '/api/ml/retrain': { ok: true, message: 'retrain queued' },
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /Gated Retrain|Retraining Champion\/Challenger/i }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const retrainCalls = calls.filter(
        ([url, init]) =>
          typeof url === 'string' &&
          url.includes('/api/ml/retrain') &&
          init?.method === 'POST',
      )
      expect(retrainCalls.length).toBeGreaterThanOrEqual(1)
    })
  })

  it('fires GET /api/ai/search when the semantic search form is submitted', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
        '/api/ai/search': sampleSearchResults,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    const input = screen.getByLabelText(/Semantic search query/i)
    fireEvent.change(input, { target: { value: 'fed rate cut' } })
    fireEvent.click(screen.getByRole('button', { name: /Search/i }))
    await waitFor(() => {
      const calls = vi.mocked(fetch).mock.calls as Array<[string, RequestInit?]>
      const searchCall = calls
        .map(([url]) => url)
        .find((u) => typeof u === 'string' && u.includes('/api/ai/search'))
      expect(searchCall).toBeTruthy()
    })
    // The search results should render once data arrives.
    await waitFor(() => {
      expect(screen.getByText('Fed rate cut at March FOMC')).toBeInTheDocument()
    })
    expect(
      screen.getByText('Bitcoin above $100k by December'),
    ).toBeInTheDocument()
  })

  it('renders the "Meta-Learner Active" badge when drift.meta_learner.is_warm is true', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText(/Meta-Learner Active/i)).toBeInTheDocument()
    })
  })

  it('does NOT render the "Meta-Learner Active" badge when drift.meta_learner.is_warm is false', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDriftWarm,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    expect(
      screen.queryByText(/Meta-Learner Active/i),
    ).not.toBeInTheDocument()
  })

  it('passes the Authorization header via apiFetch on every fetch', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit?])[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('polls every 3 s', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Random Forest')).toBeInTheDocument()
    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(3)
    // Advance 3 s — should fire three more polls.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 3)
  })

  it('clears the polling interval on unmount (no leaked setState)', async () => {
    vi.useFakeTimers()
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    const { unmount } = render(<AIMLCommandCenter />)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(screen.getByText('Random Forest')).toBeInTheDocument()
    expect(() => act(() => unmount())).not.toThrow()
    const callsBefore = vi.mocked(fetch).mock.calls.length
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(vi.mocked(fetch).mock.calls.length).toBe(callsBefore)
  })

  it('renders the "Adaptive Ensemble Blend Weights" card header', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/Adaptive Ensemble Blend Weights/i),
      ).toBeInTheDocument()
    })
  })

  it('renders the Reliability Curve SVG with aria-label', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    expect(
      screen.getByRole('img', {
        name: /Model probability calibration curve/i,
      }),
    ).toBeInTheDocument()
  })

  it('renders the feature-importance category filter buttons', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    for (const cat of ['ALL', 'MICRO', 'REGIME', 'FUNDAMENTAL']) {
      expect(screen.getByRole('button', { name: cat })).toBeInTheDocument()
    }
  })

  it('filters features when the REGIME category button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('microstructure.spread_pct')).toBeInTheDocument()
    })
    // Click REGIME filter.
    fireEvent.click(screen.getByRole('button', { name: 'REGIME' }))
    // regime.volatility_30s should remain visible (it matches the REGIME filter).
    expect(screen.getByText('regime.volatility_30s')).toBeInTheDocument()
    // microstructure.spread_pct should be filtered out.
    expect(
      screen.queryByText('microstructure.spread_pct'),
    ).not.toBeInTheDocument()
  })

  it('renders the online SGD live update count from metrics', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchRouteByUrl({
        '/api/ml/metrics': sampleMetrics,
        '/api/ml/registry': sampleRegistry,
        '/api/ml/drift': sampleDrift,
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      // n_online_updates 1247 → "1247 live market updates"
      expect(screen.getByText(/1247 live market updates/i)).toBeInTheDocument()
    })
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed all fetch / retrain / search
  // errors via `} catch {}`. The W22-1 fix surfaces them via inline
  // dismissable banners.

  it('W22-1: shows the telemetry error banner when all three ML endpoints return HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: async () => ({}),
      } as Response),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/AI\/ML telemetry endpoints unavailable/i),
      ).toBeInTheDocument()
    })
    // Telemetry error banner has Retry + Dismiss controls.
    expect(screen.getByRole('button', { name: /retry/i })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /Dismiss telemetry error/i }),
    ).toBeInTheDocument()
  })

  it('W22-1: shows the telemetry error banner when the fetch throws a network error', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNREFUSED'))
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/Network error: ECONNREFUSED/i),
      ).toBeInTheDocument()
    })
  })

  it('W22-1: dismisses the telemetry error banner when the Dismiss button is clicked', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockResolvedValue({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        json: async () => ({}),
      } as Response),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(
        screen.getByText(/AI\/ML telemetry endpoints unavailable/i),
      ).toBeInTheDocument()
    })
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss telemetry error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/AI\/ML telemetry endpoints unavailable/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: shows the retrain error banner when POST /api/ml/retrain returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string, init?: RequestInit) => {
        if (
          typeof input === 'string' &&
          input.includes('/api/ml/retrain') &&
          init?.method === 'POST'
        ) {
          return Promise.resolve({
            ok: false,
            status: 500,
            json: async () => ({ detail: 'GPU queue saturated' }),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => {
            if (typeof input === 'string' && input.includes('/api/ml/metrics')) return sampleMetrics
            if (typeof input === 'string' && input.includes('/api/ml/registry')) return sampleRegistry
            if (typeof input === 'string' && input.includes('/api/ml/drift')) return sampleDrift
            return {}
          },
        } as Response)
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    fireEvent.click(
      screen.getByRole('button', { name: /Gated Retrain|Retraining Champion\/Challenger/i }),
    )
    await waitFor(() => {
      expect(screen.getByText(/GPU queue saturated/i)).toBeInTheDocument()
    })
    // Retrain error banner is dismissable.
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss retrain error/i }),
    )
    await waitFor(() => {
      expect(screen.queryByText(/GPU queue saturated/i)).not.toBeInTheDocument()
    })
  })

  it('W22-1: shows the search error banner when GET /api/ai/search returns HTTP 500', async () => {
    vi.mocked(fetch).mockImplementation(
      vi.fn().mockImplementation((input: string) => {
        if (typeof input === 'string' && input.includes('/api/ai/search')) {
          return Promise.resolve({
            ok: false,
            status: 500,
            statusText: 'Internal Server Error',
            json: async () => ({}),
          } as Response)
        }
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => {
            if (typeof input === 'string' && input.includes('/api/ml/metrics')) return sampleMetrics
            if (typeof input === 'string' && input.includes('/api/ml/registry')) return sampleRegistry
            if (typeof input === 'string' && input.includes('/api/ml/drift')) return sampleDrift
            return {}
          },
        } as Response)
      }),
    )
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(screen.getByText('Random Forest')).toBeInTheDocument()
    })
    const input = screen.getByLabelText(/Semantic search query/i)
    fireEvent.change(input, { target: { value: 'fed rate cut' } })
    fireEvent.click(screen.getByRole('button', { name: /Search/i }))
    await waitFor(() => {
      expect(
        screen.getByText(/Semantic search failed \(HTTP 500\)/i),
      ).toBeInTheDocument()
    })
    // Search error banner is dismissable.
    fireEvent.click(
      screen.getByRole('button', { name: /Dismiss search error/i }),
    )
    await waitFor(() => {
      expect(
        screen.queryByText(/Semantic search failed/i),
      ).not.toBeInTheDocument()
    })
  })

  it('W22-1: logs the telemetry fetch error to console.error (silent swallow removed)', async () => {
    const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    vi.mocked(fetch).mockRejectedValue(new Error('Network error: ECONNRESET'))
    render(<AIMLCommandCenter />)
    await waitFor(() => {
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        expect.stringContaining('[AIMLCommandCenter]'),
        expect.any(Error),
      )
    })
    consoleErrorSpy.mockRestore()
  })
})
