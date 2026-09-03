// src/components/OfflineIndicator.test.tsx — W11-8 PWA offline banner tests.
//
// The component must:
//   - render nothing when online (the happy path; never visually intrudes
//     on the dashboard)
//   - render the banner when the browser fires the `offline` event
//   - hide the banner when the browser fires the `online` event
//   - honour `navigator.onLine` as the initial value on mount
//
// We control `navigator.onLine` via a property setter on the jsdom navigator
// (jsdom leaves it hardcoded to `true` by default). The `online` / `offline`
// window events are dispatched with `new Event('online' | 'offline')`.

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import OfflineIndicator from './OfflineIndicator'

// jsdom hardcodes navigator.onLine to `true`. We swap in a setter so each
// test can stage the online/offline state it needs.
function setNavigatorOnLine(value: boolean) {
  Object.defineProperty(globalThis.navigator, 'onLine', {
    configurable: true,
    get: () => value,
  })
}

describe('OfflineIndicator', () => {
  afterEach(() => {
    // Restore the default jsdom value so other test files aren't surprised.
    Object.defineProperty(globalThis.navigator, 'onLine', {
      configurable: true,
      get: () => true,
    })
    vi.restoreAllMocks()
  })

  it('renders nothing when online (default jsdom state)', () => {
    setNavigatorOnLine(true)
    const { container } = render(<OfflineIndicator />)
    expect(container.firstChild).toBeNull()
    expect(screen.queryByTestId('offline-indicator')).not.toBeInTheDocument()
  })

  it('renders the banner when navigator.onLine is false on mount', () => {
    setNavigatorOnLine(false)
    render(<OfflineIndicator />)
    const banner = screen.getByTestId('offline-indicator')
    expect(banner).toBeInTheDocument()
    // Banner should be polite-live so screen readers announce the state
    // change without stealing focus.
    expect(banner.getAttribute('role')).toBe('status')
    expect(banner.getAttribute('aria-live')).toBe('polite')
  })

  it('shows the banner when the browser fires the offline event', () => {
    setNavigatorOnLine(true)
    render(<OfflineIndicator />)
    // Initially online → no banner.
    expect(screen.queryByTestId('offline-indicator')).not.toBeInTheDocument()

    // Flip navigator.onLine to false (the offline event listener typically
    // fires alongside an OS-level network drop) and dispatch the event.
    setNavigatorOnLine(false)
    act(() => {
      globalThis.window.dispatchEvent(new Event('offline'))
    })

    expect(screen.getByTestId('offline-indicator')).toBeInTheDocument()
  })

  it('hides the banner when the browser fires the online event', () => {
    setNavigatorOnLine(false)
    render(<OfflineIndicator />)
    // Started offline → banner visible.
    expect(screen.getByTestId('offline-indicator')).toBeInTheDocument()

    setNavigatorOnLine(true)
    act(() => {
      globalThis.window.dispatchEvent(new Event('online'))
    })

    expect(
      screen.queryByTestId('offline-indicator'),
    ).not.toBeInTheDocument()
  })

  it('includes a user-visible message explaining what happened', () => {
    setNavigatorOnLine(false)
    render(<OfflineIndicator />)
    expect(
      screen.getByText(/you are offline/i, { exact: false }),
    ).toBeInTheDocument()
  })

  it('removes its event listeners on unmount (no leaked setState warnings)', () => {
    setNavigatorOnLine(true)
    const { unmount } = render(<OfflineIndicator />)
    expect(() =>
      act(() => {
        unmount()
        // Dispatching after unmount would warn if listeners weren't cleaned.
        globalThis.window.dispatchEvent(new Event('offline'))
      }),
    ).not.toThrow()
  })
})
