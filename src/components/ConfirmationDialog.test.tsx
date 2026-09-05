// components/ConfirmationDialog.test.tsx — W38-8 component tests.
//
// The dialog is a controlled presentational component (no fetch). Tests
// focus on its rendering contract + interaction behaviour: open/close,
// severity variants, Escape/backdrop cancel, confirm + loading state.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ConfirmationDialog from './ConfirmationDialog'

afterEach(() => {
  cleanup()
})

describe('ConfirmationDialog', () => {
  const baseProps = {
    open: true,
    title: 'Cancel all open orders?',
    description: 'This will cancel every open order across all strategies.',
    onConfirm: vi.fn(),
    onCancel: vi.fn(),
  }

  it('renders nothing when open=false', () => {
    render(<ConfirmationDialog {...baseProps} open={false} />)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('renders without crashing when open', () => {
    render(<ConfirmationDialog {...baseProps} />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('renders the title header', () => {
    render(<ConfirmationDialog {...baseProps} />)
    expect(
      screen.getByText('Cancel all open orders?'),
    ).toBeInTheDocument()
  })

  it('renders the description text', () => {
    render(<ConfirmationDialog {...baseProps} />)
    expect(
      screen.getByText(/This will cancel every open order/),
    ).toBeInTheDocument()
  })

  it('renders the default confirm + cancel labels', () => {
    render(<ConfirmationDialog {...baseProps} />)
    expect(
      screen.getByRole('button', { name: 'Confirm' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'Cancel' }),
    ).toBeInTheDocument()
  })

  it('uses custom confirm + cancel labels when provided', () => {
    render(
      <ConfirmationDialog
        {...baseProps}
        confirmLabel="Yes, cancel"
        cancelLabel="No, keep them"
      />,
    )
    expect(
      screen.getByRole('button', { name: 'Yes, cancel' }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: 'No, keep them' }),
    ).toBeInTheDocument()
  })

  it('renders the impact summary banner when impact is provided', () => {
    render(
      <ConfirmationDialog
        {...baseProps}
        impact="This will cancel 5 open orders"
      />,
    )
    expect(screen.getByText('This will cancel 5 open orders')).toBeInTheDocument()
  })

  it('does NOT render an impact banner when impact is omitted', () => {
    render(<ConfirmationDialog {...baseProps} />)
    expect(
      screen.queryByText(/This will cancel \d+ open orders/),
    ).not.toBeInTheDocument()
  })

  it('shows the danger icon for severity=danger', () => {
    render(<ConfirmationDialog {...baseProps} severity="danger" />)
    expect(screen.getByText('🛑')).toBeInTheDocument()
  })

  it('shows the warning icon for severity=warning', () => {
    render(<ConfirmationDialog {...baseProps} severity="warning" />)
    expect(screen.getByText('⚠️')).toBeInTheDocument()
  })

  it('shows the info icon for severity=info', () => {
    render(<ConfirmationDialog {...baseProps} severity="info" />)
    expect(screen.getByText('ℹ️')).toBeInTheDocument()
  })

  it('calls onCancel when the Cancel button is clicked', async () => {
    const onCancel = vi.fn()
    const user = userEvent.setup()
    render(<ConfirmationDialog {...baseProps} onCancel={onCancel} />)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledTimes(1)
  })

  it('calls onConfirm when the Confirm button is clicked', async () => {
    const onConfirm = vi.fn()
    const user = userEvent.setup()
    render(<ConfirmationDialog {...baseProps} onConfirm={onConfirm} />)
    await user.click(screen.getByRole('button', { name: 'Confirm' }))
    expect(onConfirm).toHaveBeenCalledTimes(1)
  })

  it('calls onCancel when Escape is pressed', async () => {
    const onCancel = vi.fn()
    const user = userEvent.setup()
    render(<ConfirmationDialog {...baseProps} onCancel={onCancel} />)
    await user.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalled()
  })

  it('disables both buttons while loading=true', () => {
    render(
      <ConfirmationDialog
        {...baseProps}
        loading={true}
        confirmLabel="Confirm"
      />,
    )
    // Confirm button is disabled while processing.
    const confirmBtn = screen.getByRole('button', { name: 'Confirm' })
    expect(confirmBtn).toBeDisabled()
    const cancelBtn = screen.getByRole('button', { name: 'Cancel' })
    expect(cancelBtn).toBeDisabled()
    // The confirm button shows the "Processing…" label while loading.
    expect(screen.getByText('Processing…')).toBeInTheDocument()
  })
})
