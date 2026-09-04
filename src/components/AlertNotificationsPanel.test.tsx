// components/AlertNotificationsPanel.test.tsx — W23-4 component tests.
//
// Strategy:
//   * The hook (`useAlertNotifications`) has its own comprehensive
//     test file (`useAlertNotifications.test.ts`) — here we mock the
//     hook itself with `vi.mock` so the component tests stay focused
//     on the panel's rendering contract (badge, popover open/close,
//     per-row acknowledge, Acknowledge All, Live indicator).
//   * Each test rebuilds the mock factory with the state it needs via
//     `mockReturnValue`. This keeps each test self-contained — no
//     shared mutable state across cases.
//   * Radix Popover requires a real pointer-based interaction to open.
//     We use `@testing-library/user-event` (already a project dep)
//     to click the bell trigger, then assert on the popover content
//     which Radix portals into `document.body`.
//
// What's covered:
//   1. Renders the bell trigger button without crashing.
//   2. Does NOT render the unread badge when unreadCount is 0.
//   3. Renders the unread badge with the correct numeric count.
//   4. Clamps the badge at "99+" when unreadCount > 99.
//   5. Opening the popover shows the empty state when no alerts.
//   6. Opening the popover shows the Live indicator when isConnected.
//   7. Opening the popover shows the Polling indicator when not connected.
//   8. Renders each alert row with name + message + severity.
//   9. Colour-codes each severity (critical/error/warning/info dot classes).
//  10. Clicking a row calls acknowledge(id).
//  11. Clicking "Acknowledge All" calls acknowledgeAll().
//  12. The mute toggle calls toggle().
//  13. The Acknowledge All button is hidden when there are no alerts.
//  14. The bell trigger's aria-label includes the unread count.
//  15. Closing the popover (ESC) hides the panel content.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AlertNotificationsPanel } from './AlertNotificationsPanel'
import type { Alert } from '@/hooks/useAlertNotifications'

// --- Mock the hook so tests stay focused on the panel rendering ----------
// `vi.mock` is hoisted to the top of the file by Vitest — the factory
// runs BEFORE any test imports the module. We use a mutable mock
// implementation that each test overrides via `mockReturnValue`.
const mockImpl = vi.fn()
vi.mock('@/hooks/useAlertNotifications', () => ({
  useAlertNotifications: () => mockImpl(),
}))

// --- Helpers --------------------------------------------------------------
function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    alert_id: 'a-' + Math.random().toString(36).slice(2, 9),
    name: 'Test Alert',
    message: 'test message body',
    severity: 'info',
    timestamp: Date.now(),
    ...overrides,
  }
}

interface HookState {
  alerts: Alert[]
  unreadCount: number
  enabled: boolean
  isConnected: boolean
  acknowledge: ReturnType<typeof vi.fn>
  acknowledgeAll: ReturnType<typeof vi.fn>
  toggle: ReturnType<typeof vi.fn>
}

function setHookState(overrides: Partial<HookState> = {}) {
  const defaults: HookState = {
    alerts: [],
    unreadCount: 0,
    enabled: true,
    isConnected: false,
    acknowledge: vi.fn(),
    acknowledgeAll: vi.fn(),
    toggle: vi.fn(),
  }
  const merged = { ...defaults, ...overrides }
  mockImpl.mockReturnValue(merged)
  return merged
}

beforeEach(() => {
  mockImpl.mockReset()
})

afterEach(() => {
  cleanup()
})

// --- Tests ----------------------------------------------------------------

describe('AlertNotificationsPanel — trigger button', () => {
  it('renders the bell trigger button without crashing', () => {
    setHookState()
    render(<AlertNotificationsPanel />)
    expect(
      screen.getByRole('button', { name: /alerts/i }),
    ).toBeInTheDocument()
  })

  it('does NOT render the unread badge when unreadCount is 0', () => {
    setHookState({ unreadCount: 0 })
    render(<AlertNotificationsPanel />)
    expect(screen.queryByTestId('unread-badge')).not.toBeInTheDocument()
  })

  it('renders the unread badge with the correct numeric count', () => {
    setHookState({ unreadCount: 3 })
    render(<AlertNotificationsPanel />)
    const badge = screen.getByTestId('unread-badge')
    expect(badge).toHaveTextContent('3')
  })

  it('clamps the badge at "99+" when unreadCount exceeds 99', () => {
    setHookState({ unreadCount: 150 })
    render(<AlertNotificationsPanel />)
    const badge = screen.getByTestId('unread-badge')
    expect(badge).toHaveTextContent('99+')
  })

  it('includes the unread count in the trigger aria-label', () => {
    setHookState({ unreadCount: 5 })
    render(<AlertNotificationsPanel />)
    expect(
      screen.getByRole('button', { name: /alerts, 5 unread/i }),
    ).toBeInTheDocument()
  })

  it('does not include "unread" in the aria-label when there are zero', () => {
    setHookState({ unreadCount: 0 })
    render(<AlertNotificationsPanel />)
    // The label should be just "Alerts" — no ", N unread" suffix.
    const trigger = screen.getByRole('button', { name: /^Alerts$/i })
    expect(trigger).toBeInTheDocument()
  })
})

describe('AlertNotificationsPanel — empty state', () => {
  it('shows the empty state when there are no alerts', async () => {
    setHookState({ alerts: [], unreadCount: 0 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /^Alerts$/i }))
    expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    expect(
      screen.getByText(/no active alerts/i),
    ).toBeInTheDocument()
  })

  it('does NOT render the Acknowledge All button when there are no alerts', async () => {
    setHookState({ alerts: [], unreadCount: 0 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /^Alerts$/i }))
    expect(
      screen.queryByRole('button', { name: /acknowledge all/i }),
    ).not.toBeInTheDocument()
  })
})

describe('AlertNotificationsPanel — Live indicator', () => {
  it('shows the Live indicator (green dot) when isConnected=true', async () => {
    setHookState({ isConnected: true })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts/i }))
    const indicator = screen.getByTestId('live-indicator')
    expect(indicator).toHaveTextContent('Live')
    // The dot should be green — assert via innerHTML because the dot
    // span carries multiple Tailwind classes whose `.` characters
    // collide with CSS selector escaping.
    expect(indicator.innerHTML).toContain('bg-green-400')
  })

  it('shows the Polling indicator (amber dot) when isConnected=false', async () => {
    setHookState({ isConnected: false })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts/i }))
    const indicator = screen.getByTestId('live-indicator')
    expect(indicator).toHaveTextContent('Polling')
    expect(indicator.innerHTML).toContain('bg-amber-400')
  })
})

describe('AlertNotificationsPanel — alerts list rendering', () => {
  it('renders each alert row with name + message + severity label', async () => {
    const alerts: Alert[] = [
      makeAlert({ alert_id: 'a1', name: 'Drawdown Breach', message: 'Daily P&L -5%', severity: 'critical' }),
      makeAlert({ alert_id: 'a2', name: 'Order Rejected', message: 'price too low', severity: 'error' }),
    ]
    setHookState({ alerts, unreadCount: 2 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 2 unread/i }))
    expect(screen.getByText('Drawdown Breach')).toBeInTheDocument()
    expect(screen.getByText('Daily P&L -5%')).toBeInTheDocument()
    expect(screen.getByText('Order Rejected')).toBeInTheDocument()
    expect(screen.getByText('price too low')).toBeInTheDocument()
    // Each row shows the severity label in lowercase.
    expect(screen.getByText('critical')).toBeInTheDocument()
    expect(screen.getByText('error')).toBeInTheDocument()
  })

  it('colour-codes the severity dot for each severity level', async () => {
    const alerts: Alert[] = [
      makeAlert({ alert_id: 'a1', name: 'C', severity: 'critical', timestamp: 1 }),
      makeAlert({ alert_id: 'a2', name: 'E', severity: 'error', timestamp: 2 }),
      makeAlert({ alert_id: 'a3', name: 'W', severity: 'warning', timestamp: 3 }),
      makeAlert({ alert_id: 'a4', name: 'I', severity: 'info', timestamp: 4 }),
    ]
    setHookState({ alerts, unreadCount: 4 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 4 unread/i }))

    // Each row is rendered as a button whose aria-label includes the
    // alert name. We assert the dot colour by querying for the dot
    // span inside each row.
    const row1 = screen.getByRole('button', { name: /acknowledge alert: c/i })
    const row2 = screen.getByRole('button', { name: /acknowledge alert: e/i })
    const row3 = screen.getByRole('button', { name: /acknowledge alert: w/i })
    const row4 = screen.getByRole('button', { name: /acknowledge alert: i/i })

    // Assert via innerHTML because the dot span carries multiple
    // Tailwind classes whose `.` characters collide with CSS selector
    // escaping (querySelector('.w-2.h-2.rounded-full') would match
    // any element with the four classes `w-2`, `h-2`, `rounded`, and
    // `full` — none of which exist as standalone classes).
    expect(row1.innerHTML).toContain('bg-red-400')
    expect(row2.innerHTML).toContain('bg-orange-400')
    expect(row3.innerHTML).toContain('bg-amber-400')
    expect(row4.innerHTML).toContain('bg-blue-400')
  })

  it('renders the alert count in the footer', async () => {
    const alerts: Alert[] = [
      makeAlert({ alert_id: 'a1', name: 'A', severity: 'info' }),
      makeAlert({ alert_id: 'a2', name: 'B', severity: 'info' }),
    ]
    setHookState({ alerts, unreadCount: 2 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 2 unread/i }))
    expect(screen.getByText(/2 active alerts/i)).toBeInTheDocument()
    expect(screen.getByText(/2 unread/i)).toBeInTheDocument()
  })

  it('uses singular "alert" when there is exactly one', async () => {
    const alerts: Alert[] = [makeAlert({ alert_id: 'a1', name: 'Only', severity: 'info' })]
    setHookState({ alerts, unreadCount: 1 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 1 unread/i }))
    expect(screen.getByText(/1 active alert/i)).toBeInTheDocument()
  })
})

describe('AlertNotificationsPanel — interactions', () => {
  it('calls acknowledge(id) when an alert row is clicked', async () => {
    const acknowledge = vi.fn()
    const alerts: Alert[] = [
      makeAlert({ alert_id: 'a1', name: 'Drawdown', severity: 'critical' }),
    ]
    setHookState({ alerts, unreadCount: 1, acknowledge })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 1 unread/i }))
    await user.click(screen.getByRole('button', { name: /acknowledge alert: drawdown/i }))
    expect(acknowledge).toHaveBeenCalledWith('a1')
    expect(acknowledge).toHaveBeenCalledTimes(1)
  })

  it('calls acknowledgeAll() when the "Acknowledge All" button is clicked', async () => {
    const acknowledgeAll = vi.fn()
    const alerts: Alert[] = [
      makeAlert({ alert_id: 'a1', name: 'A', severity: 'info' }),
      makeAlert({ alert_id: 'a2', name: 'B', severity: 'info' }),
    ]
    setHookState({ alerts, unreadCount: 2, acknowledgeAll })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts, 2 unread/i }))
    await user.click(screen.getByRole('button', { name: /acknowledge all alerts/i }))
    expect(acknowledgeAll).toHaveBeenCalledTimes(1)
  })

  it('calls toggle() when the mute toggle is clicked', async () => {
    const toggle = vi.fn()
    setHookState({ enabled: true, toggle })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts/i }))
    await user.click(screen.getByRole('button', { name: /mute desktop alert notifications/i }))
    expect(toggle).toHaveBeenCalledTimes(1)
  })

  it('renders the muted (🔕) icon when enabled=false', async () => {
    setHookState({ enabled: false })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts/i }))
    expect(
      screen.getByRole('button', { name: /enable desktop alert notifications/i }),
    ).toHaveTextContent('🔕')
  })

  it('renders the enabled (🔔) icon when enabled=true', async () => {
    setHookState({ enabled: true })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /alerts/i }))
    expect(
      screen.getByRole('button', { name: /mute desktop alert notifications/i }),
    ).toHaveTextContent('🔔')
  })

  it('hides the panel content when the popover is dismissed with ESC', async () => {
    setHookState({ alerts: [], unreadCount: 0 })
    const user = userEvent.setup()
    render(<AlertNotificationsPanel />)
    await user.click(screen.getByRole('button', { name: /^Alerts$/i }))
    expect(screen.getByTestId('empty-state')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(screen.queryByTestId('empty-state')).not.toBeInTheDocument()
  })
})
