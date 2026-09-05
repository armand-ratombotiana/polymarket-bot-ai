// components/ErrorBoundary.test.tsx — W38-8 component tests.
//
// The boundary is a React class component — we drive it through its
// lifecycle by rendering a child component that throws during render.
// We stub the error reporter (`captureError`) so the test does not
// enqueue a real POST to /api/client-errors.
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ErrorBoundary from './ErrorBoundary'

// Stub the error reporter so componentDidCatch doesn't fire a fetch.
vi.mock('@/lib/errorReporter', () => ({
  captureError: vi.fn(),
}))

afterEach(() => {
  cleanup()
})

function Boom({ shouldThrow = true }: { shouldThrow?: boolean }) {
  if (shouldThrow) {
    throw new Error('Boom: simulated render error')
  }
  return <div data-testid="boom-ok">All good</div>
}

describe('ErrorBoundary', () => {
  it('renders children when no error is thrown', () => {
    render(
      <ErrorBoundary>
        <div data-testid="child">child content</div>
      </ErrorBoundary>,
    )
    expect(screen.getByTestId('child')).toBeInTheDocument()
  })

  it('renders without crashing even when a child throws', () => {
    // React logs the caught error to console.error — silence it so the
    // test output stays clean.
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Something went wrong')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders the title header "Something went wrong" when a child throws', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(
      screen.getByRole('heading', { name: /something went wrong/i }),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('surfaces the error message in the fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(
      screen.getByText('Boom: simulated render error'),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders a custom fallback when provided', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary fallback={<div data-testid="custom-fallback">custom UI</div>}>
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByTestId('custom-fallback')).toBeInTheDocument()
    // The default fallback title is NOT rendered.
    expect(screen.queryByText('Something went wrong')).not.toBeInTheDocument()
    spy.mockRestore()
  })

  it('renders the "Try Again" and "Reload Page" buttons in the fallback', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )
    expect(
      screen.getByRole('button', { name: /try again/i }),
    ).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /reload page/i }),
    ).toBeInTheDocument()
    spy.mockRestore()
  })

  it('recovers (re-renders children) when "Try Again" is clicked', async () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})

    // Drive the boundary with a controlled child prop. Initially the
    // child throws; on recovery the parent re-renders with a child that
    // does not throw, simulating a transient render error that has
    // since been patched upstream.
    function MaybeBoom({ shouldThrow }: { shouldThrow: boolean }) {
      if (shouldThrow) throw new Error('transient')
      return <div data-testid="recovered">recovered</div>
    }

    const user = userEvent.setup()
    const { rerender } = render(
      <ErrorBoundary>
        <MaybeBoom shouldThrow={true} />
      </ErrorBoundary>,
    )
    expect(screen.getByText('Something went wrong')).toBeInTheDocument()

    // Flip the prop first so the child no longer throws, then click
    // "Try Again" — the boundary resets to hasError=false and the
    // new (non-throwing) child renders.
    rerender(
      <ErrorBoundary>
        <MaybeBoom shouldThrow={false} />
      </ErrorBoundary>,
    )
    await act(async () => {
      await user.click(screen.getByRole('button', { name: /try again/i }))
    })
    expect(screen.getByTestId('recovered')).toBeInTheDocument()
    spy.mockRestore()
  })
})
