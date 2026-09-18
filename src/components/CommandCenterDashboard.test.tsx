// components/CommandCenterDashboard.test.tsx — W40-2 / W49-3 minimal render
// tests for the redesigned Command Center dashboard.
//
// The dashboard polls /api/status + /api/analytics + /api/ml/metrics +
// /api/ml/drift + /api/ingestion/health on mount and accepts three ReactNode
// panels (positions / orderBooks / recentTrades) from the parent. The
// W49-3 redesign dropped the `sidebar` prop (EquityCurve / Analytics / ML
// panels now live in their own nav sections) and replaced the prior
// "risk bar" with a System Status row (Active Strategies + AI Status |
// Data Ingestion + Alerts).
//
// Tests cover:
//   1. Renders without crashing (polls resolve to empty {} payload).
//   2. Renders the embedded CommandCenterHealthBar (data-testid).
//   3. Renders the three top-bar hero KPIs (Portfolio Value / Available
//      Balance / Open Exposure).
//   4. Renders the five P&L-row KPIs (Realized / Unrealized / Win Rate /
//      Drawdown / Sharpe).
//   5. Renders the three supplied panel children.
//   6. Renders the System Status row's four sub-cards.
//   7. Polls /api/status on mount.
//   8. Survives a non-OK /api/status response without crashing.
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

// Mock useAlertNotifications so the System Status row renders without
// opening a real WebSocket in jsdom. The hook is exercised by its own
// test file (`useAlertNotifications.test.ts`).
vi.mock('@/hooks/useAlertNotifications', () => ({
  useAlertNotifications: () => ({
    alerts: [],
    unreadCount: 0,
    enabled: true,
    isConnected: false,
    acknowledge: () => {},
    acknowledgeAll: () => {},
    toggle: () => {},
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
      />,
    )
    expect(
      screen.getByTestId('command-center-health-bar'),
    ).toBeInTheDocument()
  })

  it('renders the three hero KPIs (Portfolio Value / Available Balance / Open Exposure)', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    expect(screen.getByText('Portfolio Value')).toBeInTheDocument()
    expect(screen.getByText('Available Balance')).toBeInTheDocument()
    expect(screen.getByText('Open Exposure')).toBeInTheDocument()
  })

  it('renders the five P&L-row KPIs (Realized / Unrealized / Win Rate / Drawdown / Sharpe)', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    expect(screen.getByText('Realized P&L')).toBeInTheDocument()
    expect(screen.getByText('Unrealized P&L')).toBeInTheDocument()
    expect(screen.getByText('Win Rate')).toBeInTheDocument()
    expect(screen.getByText('Drawdown')).toBeInTheDocument()
    expect(screen.getByText('Sharpe')).toBeInTheDocument()
  })

  it('renders the three panel children supplied by the parent', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div>positions-panel</div>}
        orderBooks={<div>orderbooks-panel</div>}
        recentTrades={<div>trades-panel</div>}
      />,
    )
    expect(screen.getByText('positions-panel')).toBeInTheDocument()
    expect(screen.getByText('orderbooks-panel')).toBeInTheDocument()
    expect(screen.getByText('trades-panel')).toBeInTheDocument()
  })

  it('renders the System Status row sub-cards (Active Strategies / AI Status / Data Ingestion / Alerts)', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    expect(screen.getByText('Active Strategies')).toBeInTheDocument()
    expect(screen.getByText('AI Status')).toBeInTheDocument()
    expect(screen.getByText('Data Ingestion')).toBeInTheDocument()
    expect(screen.getByText('Alerts')).toBeInTheDocument()
  })

  it('renders the Sharpe KPI with the risk-adjusted-return sub-text', async () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    // The sub-text is hidden while analytics is loading — wait for the
    // first poll to resolve (mock resolves to an empty 200 OK) so the
    // loading flag flips to false and the sub-text renders.
    await waitFor(() => {
      expect(screen.getByText('Risk-adjusted return')).toBeInTheDocument()
    })
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
        status={"error" as ConnectionStatus}
        wsConnected={false}
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the active-strategies count when snapshot.strategies is populated', () => {
    render(
      <CommandCenterDashboard
        snapshot={makeSnapshot({
          strategies: ['mean_reversion', 'momentum', 'stat_arb'],
        })}
        status="connected"
        wsConnected
        positions={<div />}
        orderBooks={<div />}
        recentTrades={<div />}
      />,
    )
    expect(screen.getByText('mean_reversion')).toBeInTheDocument()
    expect(screen.getByText('momentum')).toBeInTheDocument()
    expect(screen.getByText('stat_arb')).toBeInTheDocument()
  })
})
