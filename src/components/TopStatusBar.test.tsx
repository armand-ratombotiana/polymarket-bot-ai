// components/TopStatusBar.test.tsx — W38-8 component tests.
//
// Strategy: the status bar is a heavy composite — it composes
// ThemeToggle, LocaleSwitcher, ConnectionStatusPill, AlertNotificationsPanel,
// and SettingsModal. Each of those has its own test file. Here we mock
// the children to keep the TopStatusBar tests focused on its own
// rendering contract + action callbacks.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, cleanup, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import TopStatusBar from './TopStatusBar'
import type { BotSnapshot, ConnectionStatus } from '@/hooks/useBot'

// ── Mocks ─────────────────────────────────────────────────────────────────
const apiFetchMock = vi.fn()
vi.mock('@/lib/api', () => ({
  getApiUrl: () => '',
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}))

// Children — stubbed so they render predictable content + don't trigger
// their own fetches / WebSockets. Each is given an aria-label so the
// tests can find them.
vi.mock('./ThemeToggle', () => ({
  __esModule: true,
  default: () => (
    <button aria-label="Toggle theme" type="button">
      Theme
    </button>
  ),
}))
vi.mock('./LocaleSwitcher', () => ({
  __esModule: true,
  default: () => (
    <button aria-label="Select language" type="button">
      EN
    </button>
  ),
}))
vi.mock('./ConnectionStatus', () => ({
  __esModule: true,
  default: () => <div data-testid="connection-status-pill">WS Live</div>,
}))
vi.mock('./AlertNotificationsPanel', () => ({
  __esModule: true,
  AlertNotificationsPanel: () => (
    <button aria-label="Alerts" type="button">
      🔔
    </button>
  ),
}))
vi.mock('./SettingsModal', () => ({
  __esModule: true,
  default: ({ isOpen }: { isOpen: boolean }) =>
    isOpen ? <div data-testid="settings-modal">settings</div> : null,
}))

const baseSnapshot: BotSnapshot = {
  type: 'snapshot',
  timestamp: 1700000000,
  mode: 'paper',
  kill_switch: false,
  kill_switch_durable: false,
  observation_only: false,
  observation_reason: '',
  daily_pnl: 5.5,
  paper_balance: 105.25,
  strategies: ['mm_avellaneda_stoikov'],
  order_books: [],
  open_orders: [],
  positions: [],
  recent_trades: [],
  events: [],
}

const baseProps = {
  snapshot: baseSnapshot,
  status: 'connected' as ConnectionStatus,
  uptime: 3600,
  onKillSwitch: vi.fn(),
  onResumeSwitch: vi.fn(),
  onCancelAll: vi.fn(),
  onOpenShortcuts: vi.fn(),
  onToggleMute: vi.fn(),
  muted: false,
  onOpenConfig: vi.fn(),
  onMobileNav: vi.fn(),
}

beforeEach(() => {
  apiFetchMock.mockReset()
})

afterEach(() => {
  cleanup()
})

describe('TopStatusBar', () => {
  it('renders without crashing', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} />)
    // The header is a <header role="banner">.
    expect(screen.getByRole('banner')).toBeInTheDocument()
  })

  it('renders the PAPER TRADING mode badge when mode=paper', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} />)
    expect(screen.getByText('PAPER TRADING')).toBeInTheDocument()
  })

  it('renders the LIVE TRADING mode badge when mode=live', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, mode: 'live' }}
      />,
    )
    expect(screen.getByText('LIVE TRADING')).toBeInTheDocument()
  })

  it('renders the SHADOW MODE badge when mode=shadow', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, mode: 'shadow' }}
      />,
    )
    expect(screen.getByText('SHADOW MODE')).toBeInTheDocument()
  })

  it('renders the 🛑 HALTED badge when kill_switch=true', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, kill_switch: true }}
      />,
    )
    expect(screen.getByText('🛑 HALTED')).toBeInTheDocument()
  })

  it('renders the 👁 OBS ONLY badge when observation_only=true and kill_switch=false', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, observation_only: true }}
      />,
    )
    expect(screen.getByText('👁 OBS ONLY')).toBeInTheDocument()
  })

  it('renders the KILL SWITCH button when kill_switch=false', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} />)
    expect(
      screen.getByRole('button', { name: /kill switch/i }),
    ).toBeInTheDocument()
  })

  it('renders the ▶ RESUME button when kill_switch=true', () => {
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, kill_switch: true }}
      />,
    )
    expect(screen.getByRole('button', { name: /resume/i })).toBeInTheDocument()
  })

  it('calls onKillSwitch when the KILL SWITCH button is clicked', async () => {
    const onKillSwitch = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} onKillSwitch={onKillSwitch} />)
    await user.click(screen.getByRole('button', { name: /kill switch/i }))
    expect(onKillSwitch).toHaveBeenCalledTimes(1)
  })

  it('calls onResumeSwitch when the RESUME button is clicked', async () => {
    const onResumeSwitch = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        snapshot={{ ...baseSnapshot, kill_switch: true }}
        onResumeSwitch={onResumeSwitch}
      />,
    )
    await user.click(screen.getByRole('button', { name: /resume/i }))
    expect(onResumeSwitch).toHaveBeenCalledTimes(1)
  })

  it('calls onCancelAll when the Cancel All button is clicked', async () => {
    const onCancelAll = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} onCancelAll={onCancelAll} />)
    await user.click(screen.getByRole('button', { name: /cancel all/i }))
    expect(onCancelAll).toHaveBeenCalledTimes(1)
  })

  it('calls onOpenShortcuts when the shortcuts (⌨️) button is clicked', async () => {
    const onOpenShortcuts = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        onOpenShortcuts={onOpenShortcuts}
      />,
    )
    await user.click(
      screen.getByRole('button', { name: /open keyboard shortcuts/i }),
    )
    expect(onOpenShortcuts).toHaveBeenCalledTimes(1)
  })

  it('calls onOpenConfig when the Config button is clicked', async () => {
    const onOpenConfig = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} onOpenConfig={onOpenConfig} />)
    await user.click(
      screen.getByRole('button', { name: /open strategy and risk configuration/i }),
    )
    expect(onOpenConfig).toHaveBeenCalledTimes(1)
  })

  it('calls onToggleMute when the mute toggle is clicked', async () => {
    const onToggleMute = vi.fn()
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(
      <TopStatusBar
        {...baseProps}
        onToggleMute={onToggleMute}
        muted={false}
      />,
    )
    await user.click(screen.getByRole('button', { name: /mute audio alerts/i }))
    expect(onToggleMute).toHaveBeenCalledTimes(1)
  })

  it('opens the Settings modal when the gear (🛠) button is clicked', async () => {
    const user = userEvent.setup()
    apiFetchMock.mockResolvedValue({ ok: false, json: async () => ({}) } as Response)
    render(<TopStatusBar {...baseProps} />)
    expect(screen.queryByTestId('settings-modal')).not.toBeInTheDocument()
    await user.click(
      screen.getByRole('button', { name: /open user preferences/i }),
    )
    expect(screen.getByTestId('settings-modal')).toBeInTheDocument()
  })

  it('fetches the /api/ml/metrics + /api/ml/drift endpoints on mount', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ brier_score: 0.1, roc_auc: 0.9 }),
    } as Response)
    render(<TopStatusBar {...baseProps} />)
    await waitFor(() => {
      expect(apiFetchMock).toHaveBeenCalled()
    })
    const urls = apiFetchMock.mock.calls.map((c) => c[0] as string)
    expect(urls.some((u) => u.includes('/api/ml/metrics'))).toBe(true)
    expect(urls.some((u) => u.includes('/api/ml/drift'))).toBe(true)
  })

  it('handles fetch errors gracefully when the ML endpoints fail', async () => {
    // Should not throw — the error is caught internally + logged.
    apiFetchMock.mockRejectedValue(new Error('Network error'))
    expect(() => render(<TopStatusBar {...baseProps} />)).not.toThrow()
  })
})
