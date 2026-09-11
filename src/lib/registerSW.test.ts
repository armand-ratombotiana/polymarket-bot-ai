// src/lib/registerSW.test.ts — W11-8 unit tests for the SW registration helper.
//
// The helper is intentionally defensive: it must be safe to call from any
// context (SSR, sandboxed iframe without SW API, fully capable browser)
// and never throw. These tests pin that contract.
//
// We don't assert that `navigator.serviceWorker.register` was actually
// called with `/sw.js` — that requires a real SW environment and a `load`
// event fire, both of which are flaky under jsdom. Instead we assert the
// observable invariants: no throw, SSR no-op, missing-API no-op.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { registerServiceWorker } from './registerSW'

describe('registerServiceWorker', () => {
  // jsdom doesn't ship a ServiceWorkerContainer — we mock the slice of the
  // navigator API the helper touches on each test.
  const originalNavigator = (globalThis as { navigator?: Navigator }).navigator
  const originalWindow = globalThis.window
  const originalDocument = globalThis.document

  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    // Restore the global shape so subsequent test files see the real jsdom.
    if (originalNavigator !== undefined) {
      Object.defineProperty(globalThis, 'navigator', {
        value: originalNavigator,
        configurable: true,
        writable: true,
      })
    }
    if (originalWindow !== undefined) {
      ;(globalThis as { window?: Window }).window = originalWindow
    }
    if (originalDocument !== undefined) {
      ;(globalThis as { document?: Document }).document = originalDocument
    }
    vi.restoreAllMocks()
  })

  it('does not throw when called in a browser that has SW support', () => {
    const register = vi.fn().mockReturnValue(
      Promise.resolve({ scope: '/' }),
    )
    Object.defineProperty(globalThis, 'navigator', {
      value: { serviceWorker: { register } },
      configurable: true,
      writable: true,
    })
    Object.defineProperty(globalThis, 'document', {
      value: { readyState: 'complete' },
      configurable: true,
      writable: true,
    })
    expect(() => registerServiceWorker()).not.toThrow()
  })

  it('returns early (no throw) when window is undefined (SSR)', () => {
    // Simulate SSR: window is unset. The helper must short-circuit on the
    // `typeof window === 'undefined'` guard and never touch navigator.
    const savedWindow = (globalThis as { window?: Window }).window
    delete (globalThis as { window?: Window }).window
    try {
      expect(() => registerServiceWorker()).not.toThrow()
    } finally {
      ;(globalThis as { window?: Window }).window = savedWindow
    }
  })

  it('returns early (no throw) when navigator.serviceWorker is missing', () => {
    // Older browsers / sandboxed iframes: no SW API at all.
    Object.defineProperty(globalThis, 'navigator', {
      value: {},
      configurable: true,
      writable: true,
    })
    expect(() => registerServiceWorker()).not.toThrow()
  })

  it('checks for serviceWorker support before calling register', () => {
    // Replace navigator with a Proxy that records every property accessed via
    // BOTH the `in` operator (has trap) and dot access (get trap). The helper
    // guards with `'serviceWorker' in navigator`, which triggers the `has`
    // trap, not the `get` trap — so we must instrument both.
    const accessedKeys: string[] = []
    const proxiedNavigator = new Proxy(
      {},
      {
        get(_t, prop) {
          accessedKeys.push(`get:${String(prop)}`)
          return undefined
        },
        has(_t, prop) {
          accessedKeys.push(`has:${String(prop)}`)
          // Report `serviceWorker` as absent so the helper short-circuits
          // before trying to call `.register()`.
          return false
        },
      },
    )
    Object.defineProperty(globalThis, 'navigator', {
      value: proxiedNavigator,
      configurable: true,
      writable: true,
    })
    expect(() => registerServiceWorker()).not.toThrow()
    // Confirm the helper actually performed the presence check.
    expect(accessedKeys).toContain('has:serviceWorker')
  })

  it('swallows registration errors instead of throwing', async () => {
    // Some sandboxes (like the sandbox preview) have `'serviceWorker' in
    // navigator` truthy but `.register()` throws a SecurityError. The
    // helper must catch it and log, not crash the React mount.
    const consoleError = vi
      .spyOn(console, 'error')
      .mockImplementation(() => {})
    const register = vi.fn().mockImplementation(() => {
      // Mimic a synchronous throw — Promise.reject would also be caught,
      // but the helper's `.catch` chain handles both paths.
      throw new DOMException('not allowed', 'SecurityError')
    })
    Object.defineProperty(globalThis, 'navigator', {
      value: { serviceWorker: { register } },
      configurable: true,
      writable: true,
    })
    Object.defineProperty(globalThis, 'document', {
      value: { readyState: 'complete' },
      configurable: true,
      writable: true,
    })
    expect(() => registerServiceWorker()).not.toThrow()
    // register was reached (proves the SW-support branch fired).
    expect(register).toHaveBeenCalled()
    // The error was logged, not silently swallowed.
    expect(consoleError).toHaveBeenCalled()
  })

  it('registers on the window "load" event when document is not yet complete', () => {
    const register = vi.fn().mockReturnValue(Promise.resolve({ scope: '/' }))
    Object.defineProperty(globalThis, 'navigator', {
      value: { serviceWorker: { register } },
      configurable: true,
      writable: true,
    })
    Object.defineProperty(globalThis, 'document', {
      value: { readyState: 'loading' },
      configurable: true,
      writable: true,
    })
    const addEventListener = vi.spyOn(globalThis.window, 'addEventListener')
    registerServiceWorker()
    // The helper deferred registration to the load event rather than calling
    // register() immediately.
    expect(register).not.toHaveBeenCalled()
    expect(addEventListener).toHaveBeenCalledWith('load', expect.any(Function))
  })
})
