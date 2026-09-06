// @ts-nocheck
// src/components/ui/states.test.tsx — W41-3 reusable state primitives.
//
// Verifies the contract documented in `states.tsx`:
//   1. PanelSkeleton  — renders N shimmering lines + role="status".
//   2. EmptyState      — renders icon + title + message + optional action.
//   3. ErrorState      — renders message + optional detail + optional Retry.
//   4. StaleIndicator  — hidden when age < 30s; amber 30–120s; red >120s.
//   5. DisconnectedState — renders message + hint + optional Retry.
//
// Strategy: every component renders a `data-testid` so the tests target
// state without relying on text matching (which is fragile across i18n +
// copy edits). All tests are synchronous — the components are stateless
// presentational primitives.

import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import {
  PanelSkeleton,
  EmptyState,
  ErrorState,
  StaleIndicator,
  DisconnectedState,
} from './states'

describe('PanelSkeleton', () => {
  it('renders the panel-skeleton testid + role=status', () => {
    const { container } = render(<PanelSkeleton />)
    expect(screen.getByTestId('panel-skeleton')).toBeTruthy()
    expect(screen.getByRole('status')).toBeTruthy()
    expect(screen.getByLabelText('Loading content')).toBeTruthy()
  })

  it('renders the requested number of skeleton lines (default 3)', () => {
    const { container } = render(<PanelSkeleton />)
    const lines = container.querySelectorAll('.animate-pulse')
    expect(lines.length).toBe(3)
  })

  it('honours the `lines` prop (clamped to 1..12)', () => {
    const { container } = render(<PanelSkeleton lines={5} />)
    const lines = container.querySelectorAll('.animate-pulse')
    expect(lines.length).toBe(5)
  })

  it('clamps `lines` below 1 up to 1', () => {
    const { container } = render(<PanelSkeleton lines={0} />)
    const lines = container.querySelectorAll('.animate-pulse')
    expect(lines.length).toBe(1)
  })

  it('clamps `lines` above 12 down to 12', () => {
    const { container } = render(<PanelSkeleton lines={99} />)
    const lines = container.querySelectorAll('.animate-pulse')
    expect(lines.length).toBe(12)
  })

  it('renders the last line at half width (visual variance)', () => {
    const { container } = render(<PanelSkeleton lines={3} />)
    const lines = container.querySelectorAll('.animate-pulse')
    // The last line has the `w-1/2` class; the others have `w-full`.
    expect(lines[0].className).toContain('w-full')
    expect(lines[2].className).toContain('w-1/2')
  })
})

describe('EmptyState', () => {
  it('renders the panel-empty-state testid + role=status', () => {
    render(<EmptyState title="No data" />)
    expect(screen.getByTestId('panel-empty-state')).toBeTruthy()
    expect(screen.getByRole('status')).toBeTruthy()
  })

  it('renders the icon, title, and message', () => {
    render(<EmptyState icon="📭" title="No positions" message="Open one to see it here." />)
    expect(screen.getByText('📭')).toBeTruthy()
    expect(screen.getByText('No positions')).toBeTruthy()
    expect(screen.getByText('Open one to see it here.')).toBeTruthy()
  })

  it('hides the icon when icon=" "', () => {
    render(<EmptyState icon="" title="No data" />)
    expect(screen.queryByText('📭')).toBeNull()
  })

  it('uses a default icon when none is provided', () => {
    render(<EmptyState title="No data" />)
    expect(screen.getByText('📭')).toBeTruthy()
  })

  it('renders the optional action node when provided', () => {
    render(
      <EmptyState
        title="No data"
        action={<button data-testid="reset-action">Reset filters</button>}
      />,
    )
    expect(screen.getByTestId('reset-action')).toBeTruthy()
    expect(screen.getByText('Reset filters')).toBeTruthy()
  })

  it('does not render the message slot when message is undefined', () => {
    render(<EmptyState title="No data" />)
    expect(screen.queryByText('Open one to see it here.')).toBeNull()
  })
})

describe('ErrorState', () => {
  it('renders the panel-error-state testid + role=alert', () => {
    render(<ErrorState />)
    expect(screen.getByTestId('panel-error-state')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
  })

  it('uses the default message when none is provided', () => {
    render(<ErrorState />)
    expect(screen.getByText('Unable to load data')).toBeTruthy()
  })

  it('renders the custom message when provided', () => {
    render(<ErrorState message="Analytics data unavailable" />)
    expect(screen.getByText('Analytics data unavailable')).toBeTruthy()
  })

  it('renders the optional detail line when provided', () => {
    render(<ErrorState message="Failed" detail="HTTP 500: Internal Server Error" />)
    expect(screen.getByText('HTTP 500: Internal Server Error')).toBeTruthy()
  })

  it('omits the Retry button when onRetry is not provided', () => {
    render(<ErrorState message="Failed" />)
    expect(screen.queryByTestId('panel-error-retry')).toBeNull()
  })

  it('renders the Retry button when onRetry is provided', () => {
    render(<ErrorState message="Failed" onRetry={() => {}} />)
    expect(screen.getByTestId('panel-error-retry')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })

  it('calls onRetry when the Retry button is clicked', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(<ErrorState message="Failed" onRetry={onRetry} />)
    await user.click(screen.getByTestId('panel-error-retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('honours the retryLabel override', () => {
    render(<ErrorState message="Failed" onRetry={() => {}} retryLabel="Try again" />)
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
  })
})

describe('StaleIndicator', () => {
  it('returns null when age < 30s (data is fresh)', () => {
    const { container } = render(<StaleIndicator age={10} />)
    expect(container.firstChild).toBeNull()
  })

  it('returns null when age is exactly 30s (boundary - <30 hidden)', () => {
    const { container } = render(<StaleIndicator age={29} />)
    expect(container.firstChild).toBeNull()
  })

  it('renders the amber Stale pill when 30 <= age < 120', () => {
    render(<StaleIndicator age={45} />)
    const pill = screen.getByTestId('panel-stale-indicator')
    expect(pill).toBeTruthy()
    expect(pill.getAttribute('data-stale-level')).toBe('stale')
    expect(pill.textContent).toContain('Stale')
    expect(pill.textContent).toContain('45s')
  })

  it('renders the red Dead pill when age >= 120', () => {
    render(<StaleIndicator age={180} />)
    const pill = screen.getByTestId('panel-stale-indicator')
    expect(pill).toBeTruthy()
    expect(pill.getAttribute('data-stale-level')).toBe('dead')
    expect(pill.textContent).toContain('Dead')
    expect(pill.textContent).toContain('3m')
  })

  it('switches the unit from seconds to minutes at 60s', () => {
    render(<StaleIndicator age={60} />)
    expect(screen.getByTestId('panel-stale-indicator').textContent).toContain('1m')
  })

  it('returns null when age is not a finite number', () => {
    const { container } = render(<StaleIndicator age={Number.NaN} />)
    expect(container.firstChild).toBeNull()
  })

  it('exposes a human-readable aria-label', () => {
    render(<StaleIndicator age={45} />)
    expect(screen.getByTestId('panel-stale-indicator').getAttribute('aria-label'))
      .toMatch(/45 seconds old.*stale/i)
  })
})

describe('DisconnectedState', () => {
  it('renders the panel-disconnected-state testid + role=alert', () => {
    render(<DisconnectedState />)
    expect(screen.getByTestId('panel-disconnected-state')).toBeTruthy()
    expect(screen.getByRole('alert')).toBeTruthy()
  })

  it('renders the default message + hint', () => {
    render(<DisconnectedState />)
    expect(screen.getByText('Backend unavailable')).toBeTruthy()
    expect(screen.getByText(/backend service appears to be unreachable/i)).toBeTruthy()
  })

  it('honours custom message + hint', () => {
    render(
      <DisconnectedState
        message="Bot offline"
        hint="The Polymarket bot service is not responding."
      />,
    )
    expect(screen.getByText('Bot offline')).toBeTruthy()
    expect(screen.getByText('The Polymarket bot service is not responding.')).toBeTruthy()
  })

  it('omits the Retry button when onRetry is not provided', () => {
    render(<DisconnectedState />)
    expect(screen.queryByTestId('panel-disconnected-retry')).toBeNull()
  })

  it('renders the Retry button when onRetry is provided', () => {
    render(<DisconnectedState onRetry={() => {}} />)
    expect(screen.getByTestId('panel-disconnected-retry')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Retry Connection' })).toBeTruthy()
  })

  it('calls onRetry when the Retry button is clicked', async () => {
    const user = userEvent.setup()
    const onRetry = vi.fn()
    render(<DisconnectedState onRetry={onRetry} />)
    await user.click(screen.getByTestId('panel-disconnected-retry'))
    expect(onRetry).toHaveBeenCalledTimes(1)
  })

  it('honours the retryLabel override', () => {
    render(
      <DisconnectedState
        onRetry={() => {}}
        retryLabel="Reconnect now"
      />,
    )
    expect(screen.getByRole('button', { name: 'Reconnect now' })).toBeTruthy()
  })
})
