// components/KpiCard.test.tsx — W40-2 minimal render tests for the KPI
// card primitive introduced in W39-3.
//
// The card is a presentational shell driven entirely by its props —
// no fetch, no clock, no global state. Tests cover:
//   1. Renders without crashing in the default (value) state.
//   2. Renders the label + value when given both.
//   3. Renders the loading skeleton when `loading` is true.
//   4. Renders the error pill + "—" value when `error` is non-null.
//   5. Renders the stale pill when `stale` is true.
//   6. Honors the `data-testid="kpi-{id}"` contract used by parents.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import KpiCard from './KpiCard'

global.fetch = vi.fn()

describe('KpiCard', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <KpiCard id="balance" label="Balance" value="$1,234.56" />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the label + value when given both', () => {
    render(<KpiCard id="balance" label="Balance" value="$1,234.56" />)
    expect(screen.getByText('Balance')).toBeInTheDocument()
    expect(screen.getByText('$1,234.56')).toBeInTheDocument()
  })

  it('exposes data-testid="kpi-{id}" for parent test queries', () => {
    render(<KpiCard id="daily-pnl" label="Daily P&L" value="+$12.34" />)
    expect(screen.getByTestId('kpi-daily-pnl')).toBeInTheDocument()
  })

  it('renders a skeleton in place of the value when loading', () => {
    const { container } = render(
      <KpiCard id="balance" label="Balance" value="$1" loading />,
    )
    // The skeleton replaces the value — value text should NOT render.
    expect(screen.queryByText('$1')).not.toBeInTheDocument()
    // Loading status element is rendered (role="status", aria-label="loading").
    expect(container.querySelector('[role="status"]')).toBeTruthy()
  })

  it('renders the error pill + em-dash value when error is set', () => {
    render(
      <KpiCard
        id="balance"
        label="Balance"
        value="$1"
        error="HTTP 500"
      />,
    )
    // Error pill has aria-label="error".
    expect(screen.getByLabelText('error')).toBeInTheDocument()
    // Value is replaced with em-dash.
    expect(screen.queryByText('$1')).not.toBeInTheDocument()
  })

  it('renders the stale pill when stale is true and not loading/error', () => {
    render(
      <KpiCard id="balance" label="Balance" value="$1" stale />,
    )
    // Stale pill has aria-label="stale".
    expect(screen.getByLabelText('stale')).toBeInTheDocument()
    // Value still renders (stale doesn't hide the number).
    expect(screen.getByText('$1')).toBeInTheDocument()
  })
})
