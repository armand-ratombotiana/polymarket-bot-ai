// components/CommandCenterMetricsStrip.test.tsx — W40-2 minimal render
// tests for the W38-3 aggregated metrics strip.
//
// The strip polls /api/status, /api/analytics, /api/ml/metrics,
// /api/ml/drift, /api/ingestion/health in parallel on mount and also
// consumes `useAlertNotifications` (which opens its own WebSocket).
// Tests cover:
//   1. Renders without crashing (polls resolve to empty {} payload).
//   2. Renders the documented `data-testid` region.
//   3. Renders the five cluster labels (Portfolio / Trading / Risk / AI / System).
//   4. Polls /api/status on mount.
//
// Mock strategy: `apiFetch` resolves every URL to an empty 200 OK so
// none of the five polled hooks hang the test. `useAlertNotifications`
// is mocked to return an empty alerts list so no WebSocket gets opened.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import CommandCenterMetricsStrip from './CommandCenterMetricsStrip'
import type { BotSnapshot } from '@/hooks/useBot'

const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

// Stub the alerts hook so the strip doesn't try to open a real WebSocket.
vi.mock('@/hooks/useAlertNotifications', () => ({
  useAlertNotifications: () => ({
    alerts: [],
    unreadCount: 0,
    enabled: true,
    isConnected: false,
    acknowledge: vi.fn(),
    acknowledgeAll: vi.fn(),
    toggle: vi.fn(),
  }),
}))

function makeSnapshot(
  overrides: Partial<BotSnapshot> = {},
): BotSnapshot {
  return {
    type: 'snapshot',
    timestamp: Math.floor(Date.now() / 1000),
    mode: 'paper',
    kill_switch: false,
    kill_switch_durable: false,
    observation_only: false,
    observation_reason: '',
    daily_pnl: 0,
    paper_balance: 100,
    strategies: [],
    order_books: [],
    open_orders: [],
    positions: [],
    recent_trades: [],
    events: [],
    ...overrides,
  }
}

function mockOk(payload: unknown = {}) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response
}

beforeEach(() => {
  apiFetchMock.mockReset()
  apiFetchMock.mockResolvedValue(mockOk({}))
})

afterEach(() => {
  cleanup()
})

describe('CommandCenterMetricsStrip', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <CommandCenterMetricsStrip snapshot={makeSnapshot()} />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the documented data-testid region', () => {
    render(<CommandCenterMetricsStrip snapshot={makeSnapshot()} />)
    expect(
      screen.getByTestId('command-center-metrics-strip'),
    ).toBeInTheDocument()
  })

  it('renders the five cluster blocks', () => {
    render(<CommandCenterMetricsStrip snapshot={makeSnapshot()} />)
    expect(screen.getByTestId('cluster-portfolio')).toBeInTheDocument()
    expect(screen.getByTestId('cluster-trading')).toBeInTheDocument()
    expect(screen.getByTestId('cluster-risk')).toBeInTheDocument()
    expect(screen.getByTestId('cluster-ai')).toBeInTheDocument()
    expect(screen.getByTestId('cluster-system')).toBeInTheDocument()
  })

  it('polls /api/status on mount', async () => {
    render(<CommandCenterMetricsStrip snapshot={makeSnapshot()} />)
    await waitFor(() => {
      expect(
        apiFetchMock.mock.calls.some(
          (c) => typeof c[0] === 'string' && c[0].includes('/api/status'),
        ),
      ).toBe(true)
    })
  })

  it('renders the snapshot-derived portfolio KPIs (Total Value, Available Balance, Open Exposure)', () => {
    render(
      <CommandCenterMetricsStrip
        snapshot={makeSnapshot({ paper_balance: 1234.5 })}
      />,
    )
    expect(screen.getByText('Total Value')).toBeInTheDocument()
    expect(screen.getByText('Available Balance')).toBeInTheDocument()
    expect(screen.getByText('Open Exposure')).toBeInTheDocument()
  })
})
