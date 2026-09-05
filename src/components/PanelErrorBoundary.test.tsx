// components/PanelErrorBoundary.test.tsx — W38-8 component tests.
//
// Same lifecycle-catch contract as the root ErrorBoundary but scoped to
// a single panel. We drive it by rendering a child that throws during
// render and assert on the inline fallback UI.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PanelErrorBoundary from './PanelErrorBoundary'

afterEach(() => {
  cleanup()
})

function Boom() {
  throw new Error('Boom: panel render failed')
}

describe('PanelErrorBoundary', () => {
  it('renders children when no error is thrown', () => {
    render(
      <PanelErrorBoundary label="Orders">
        <div data-testid="child">child content</div>
      </PanelErrorBoundary>,
    )
    expect(screen.getByTestId('child')).toBeInTheDocument()
  })

  it('renders without crashing even when a child throws', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary label="Orders">
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(screen.getByText(/Orders encountered an error/)).toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders the label-prefixed error title header', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary label="Positions">
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(
      screen.getByText('Positions encountered an error'),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('falls back to the default label when none is provided', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary>
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(
      screen.getByText('This panel encountered an error'),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('surfaces the error message in the fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary label="Orders">
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(screen.getByText('Boom: panel render failed')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders a custom fallback when provided', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary fallback={<div data-testid="custom">custom UI</div>}>
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(screen.getByTestId('custom')).toBeInTheDocument()
    // The default fallback title is NOT rendered.
    expect(
      screen.queryByText(/encountered an error/),
    ).not.toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders Retry + Reload buttons in the fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <PanelErrorBoundary label="Orders">
        <Boom />
      </PanelErrorBoundary>,
    )
    expect(
      screen.getByRole('button', { name: /retry/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /reload/i }),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('recovers when "Retry" is clicked', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    // Drive the boundary with a controlled child prop. Initially the
    // child throws; on retry the parent re-renders with a child that
    // does not throw, simulating a transient render error.
    function MaybeBoom({ shouldThrow }: { shouldThrow: boolean }) {
      if (shouldThrow) throw new Error('transient')
      return <div data-testid="recovered">recovered</div>
    }

    const user = userEvent.setup()
    const { rerender } = render(
      <PanelErrorBoundary label="Orders">
        <MaybeBoom shouldThrow={true} />
      </PanelErrorBoundary>,
    )
    expect(
      screen.getByText('Orders encountered an error'),
    ).toBeInTheDocument()

    // Flip the prop so the child no longer throws, then click Retry —
    // the boundary resets to hasError=false and the new child renders.
    rerender(
      <PanelErrorBoundary label="Orders">
        <MaybeBoom shouldThrow={false} />
      </PanelErrorBoundary>,
    )
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /retry/i }))
    })
    expect(screen.getByTestId('recovered')).toBeInTheDocument()
    spy.mockRestore()
  })
})
