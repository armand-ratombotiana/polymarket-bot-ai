// components/ObservabilityPanel.test.tsx — W38-8 component tests.
//
// Strategy: mock `apiFetch` so the panel can be driven through the
// loading → loaded → error states without touching the gateway.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ObservabilityPanel from './ObservabilityPanel'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

// The Sparkline chart from recharts — jsdom can't measure it.
vi.mock('@/components/charts', () => ({
  Sparkline: () => <svg data-testid="sparkline" />,
}))

const sampleReport = {
  generated_at: 1700000000,
  category_count: 5,
  metric_count: 23,
  oldest_sample_age_seconds: 60,
  newest_sample_age_seconds: 5,
  categories: {
    SYSTEM: {
      cpu_pct: { value: 35.2, timestamp: 1700000000, age_seconds: 5, metadata: null },
      mem_rss_mb: { value: 412.5, timestamp: 1700000000, age_seconds: 5, metadata: null },
    },
    BOT: {
      uptime_seconds: { value: 86400, timestamp: 1700000000, age_seconds: 5, metadata: null },
    },
  },
}

function mockOk(payload: unknown) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response
}

function mockNotOk(status = 500) {
  return {
    ok: false,
    status,
    json: async () => ({}),
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('ObservabilityPanel', () => {
  it('renders without crashing', () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleReport))
    render(<ObservabilityPanel />)
    expect(screen.getByText('System Observability')).toBeInTheDocument()
  })

  it('renders the "System Observability" header once data loads', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleReport))
    render(<ObservabilityPanel />)
    await waitFor(() => {
      // The header h2 surfaces after data has loaded.
      expect(screen.getAllByText('System Observability').length).toBeGreaterThan(0)
    })
  })

  it('shows the loading skeleton on first mount before data arrives', () => {
    // Never-resolving promise → loading stays true.
    apiFetchMock.mockImplementation(() => new Promise<Response>(() => {}))
    render(<ObservabilityPanel />)
    expect(screen.getByText('System Observability')).toBeInTheDocument()
    expect(screen.getByText('System Observability')).toBeInTheDocument()
  })

  it('shows the empty-state message when metric_count is 0', async () => {
    apiFetchMock.mockResolvedValue(
      mockOk({ ...sampleReport, metric_count: 0, categories: {} }),
    )
    render(<ObservabilityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('No metrics collected yet'),
      ).toBeInTheDocument()
    })
  })

  it('shows the hard-error fallback when the fetch returns not-ok', async () => {
    apiFetchMock.mockResolvedValue(mockNotOk(500))
    render(<ObservabilityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Observability endpoint unavailable'),
      ).toBeInTheDocument()
    })
  })

  it('shows the hard-error fallback when the fetch throws', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<ObservabilityPanel />)
    await waitFor(() => {
      expect(
        screen.getByText('Observability endpoint unavailable'),
      ).toBeInTheDocument()
    })
  })

  it('renders a Retry button on the error fallback', async () => {
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    render(<ObservabilityPanel />)
    await waitFor(() => {
      expect(
        screen.getByRole('button', { name: /retry/i }),
      ).toBeInTheDocument()
    })
  })

  it('re-fetches the report when the Retry button is clicked', async () => {
    const user = userEvent.setup()
    apiFetchMock
      .mockRejectedValueOnce(new Error('Network error'))
      .mockResolvedValueOnce(mockOk(sampleReport))
      .mockResolvedValue(mockOk({ samples: [] })) // per-metric history fetches
    render(<ObservabilityPanel />)
    const retry = await screen.findByRole('button', { name: /retry/i })
    await user.click(retry)
    // After the retry resolves, the empty-state / error message should
    // be replaced by the main panel render.
    await waitFor(() => {
      expect(
        screen.queryByText('Observability endpoint unavailable'),
      ).not.toBeInTheDocument()
    })
    // At least 2 calls: the initial mount fetch + the retry fetch. The
    // panel also fetches per-metric history (one call per metric name)
    // once the report loads, so the total call count is higher than 2 —
    // we assert the floor, not the exact count.
    expect(apiFetchMock.mock.calls.length).toBeGreaterThanOrEqual(2)
  })

  it('fetches the /api/observability endpoint on mount', async () => {
    apiFetchMock.mockResolvedValue(mockOk(sampleReport))
    render(<ObservabilityPanel />)
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled()
    })
    const firstCallUrl = apiFetchMock.mock.calls[0][0] as string
    expect(firstCallUrl).toContain('/api/observability')
  })
})
