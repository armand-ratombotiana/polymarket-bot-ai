// components/CommandCenterDashboard.test.tsx — W40-2 minimal render tests
// for the W39-3 redesigned Command Center dashboard.
//
// The dashboard polls /api/status + /api/analytics on mount and accepts
// four ReactNode panels from the parent. Tests cover:
//   1. Renders without crashing (polls resolve to empty {} payload).
//   2. Renders the embedded CommandCenterHealthBar (data-testid).
//   3. Renders the three top-bar hero KPIs (Balance / Available / Exposure).
//   4. Renders the four supplied panel children.
//   5. Polls /api/status on mount.
//
// Mock strategy: `apiFetch` resolves every URL to an empty 200 OK so the
// loading skeletons never hang the test. Pattern mirrors
// AIMLCommandCenter.test.tsx → "renders without crashing" baseline.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import CommandCenterDashboard from './CommandCenterDashboard'
import type { BotSnapshot, ConnectionStatus } from '@/hooks/useBot'

const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
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
  // Default: every fetch resolves to an empty 200 OK so the dashboard's
  // usePolled hook flips out of its loading state immediately.
  apiFetchMock.mockResolvedValue(mockOk({}))
})

afterEach(() => {
  cleanup()
})

describe('CommandCenterDashboard', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div>positions-panel</div>}
        orderBooks={<div>orderbooks-panel</div>}
        recentTrades={<div>trades-panel</div>}
        sidebar={<div>sidebar-panel</div>}
      />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the embedded CommandCenterHealthBar', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
        sidebar={<div />}
      />,
    )
    expect(
      screen.getByTestId('command-center-health-bar'),
    ).toBeInTheDocument()
  })

  it('renders the three hero KPIs (Balance / Available / Exposure)', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
        sidebar={<div />}
      />,
    )
    expect(screen.getByText('Balance')).toBeInTheDocument()
    expect(screen.getByText('Available')).toBeInTheDocument()
    expect(screen.getByText('Exposure')).toBeInTheDocument()
  })

  it('renders the four panel children supplied by the parent', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div>positions-panel</div>}
        orderBooks={<div>orderbooks-panel</div>}
        recentTrades={<div>trades-panel</div>}
        sidebar={<div>sidebar-panel</div>}
      />,
    )
    expect(screen.getByText('positions-panel')).toBeInTheDocument()
    expect(screen.getByText('orderbooks-panel')).toBeInTheDocument()
    expect(screen.getByText('trades-panel')).toBeInTheDocument()
    expect(screen.getByText('sidebar-panel')).toBeInTheDocument()
  })

  it('polls /api/status on mount', async () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
        sidebar={<div />}
      />,
    )
    await waitFor(() => {
      expect(
        apiFetchMock.mock.calls.some(
          (c) => typeof c[0] === 'string' && c[0].includes('/api/status'),
        ),
      ).toBe(true)
    })
  })

  it('survives a non-OK /api/status response without crashing', () => {
    apiFetchMock.mockImplementation((input: string) =>
      Promise.resolve(
        typeof input === 'string' && input.includes('/api/status')
          ? ({ ok: false, status: 500, json: async () => ({}) } as Response)
          : mockOk({}),
      ),
    )
    const { container } = render(
      <CommandCenterDashboard
        snapshot={makeSnapshot({ kill_switch: true })}
        status="error" as ConnectionStatus
        wsConnected={false}
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
        sidebar={<div />}
      />,
    )
    expect(container.firstChild).toBeTruthy()
  })
})
