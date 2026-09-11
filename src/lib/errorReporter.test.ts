// lib/errorReporter.test.ts — W14-8 unit tests for the client-side error
// reporter (Sentry-like).
//
// Strategy:
//   * `global.fetch` is mocked at the test-setup level (see `test/setup.ts`)
//     — we re-stub it per test to a fresh `vi.fn()` so call history is
//     isolated. `apiFetch` (the transport the reporter uses) calls the
//     global `fetch` symbol directly, so the stub is picked up
//     transparently without needing to mock the `@/lib/api` module.
//   * `_resetForTests()` clears the in-memory queue + cancels any pending
//     flush timer before each test, so module-level state never leaks
//     between cases.
//   * `vi.useFakeTimers()` is used for the timer-coalescing test (verify
//     that 5s after `captureError`, exactly one POST fires) — for the
//     direct-flush tests we just call `flush()` exported from the module
//     (no timer involved).
//   * `console.error` is spied so the dev-console mirror doesn't pollute
//     the test runner's output.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  captureError,
  captureMessage,
  flush,
  installErrorHandlers,
  getErrorStats,
  _resetForTests,
} from './errorReporter'

// Build a JSON Response the way `apiFetch` expects to receive it.
function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('errorReporter', () => {
  let consoleErrorSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    // Re-stub fetch on every test so call history is isolated. apiFetch
    // resolves to the global `fetch` symbol at call time, so this stub
    // is picked up transparently.
    global.fetch = vi.fn() as unknown as typeof fetch
    // Default to a happy-path 200 response; individual tests can override
    // via `vi.mocked(fetch).mockResolvedValueOnce(...)`.
    vi.mocked(fetch).mockResolvedValue(jsonResponse({ ok: true, received: 0 }))
    // Silence the dev-console mirror — `captureError` calls `console.error`
    // on every report, which would otherwise pollute test output.
    consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    // Clear the module-level queue + cancel any pending flush timer so
    // each test starts from a known-empty baseline.
    _resetForTests()
  })

  afterEach(() => {
    consoleErrorSpy.mockRestore()
    vi.useRealTimers()
  })

  // -------------------------------------------------------------------------
  // captureError — Error instance OR string → queue
  // -------------------------------------------------------------------------
  describe('captureError', () => {
    it('grows the queue by one for an Error instance', () => {
      const before = getErrorStats().queueLength
      captureError(new Error('boom'))
      expect(getErrorStats().queueLength).toBe(before + 1)
    })

    it('grows the queue by one for a string message', () => {
      const before = getErrorStats().queueLength
      captureError('something broke')
      expect(getErrorStats().queueLength).toBe(before + 1)
    })

    it('mirrors the report to console.error tagged [ErrorReporter]', () => {
      captureError(new Error('boom'), { foo: 'bar' })
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        '[ErrorReporter]',
        'boom',
        { foo: 'bar' },
      )
    })

    it('forwards the context object verbatim to the flushed payload', async () => {
      captureError(new Error('ctx-test'), { componentStack: 'in <Foo>' })
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      expect(body.errors[0].context).toEqual({ componentStack: 'in <Foo>' })
    })

    it('no-ops on the server (typeof window === undefined)', async () => {
      // jsdom always defines window, so simulate SSR by temporarily
      // deleting it. Must restore in the finally so subsequent tests in
      // the file see the real window.
      const originalWindow = globalThis.window
      try {
        // Cast to a record so the delete is type-safe on the SSR test path.
        delete (globalThis as Record<string, unknown>).window
        const before = getErrorStats().queueLength
        captureError(new Error('should be dropped'))
        expect(getErrorStats().queueLength).toBe(before)
      } finally {
        ;(globalThis as Record<string, unknown>).window = originalWindow
      }
    })
  })

  // -------------------------------------------------------------------------
  // captureMessage — info / warning / error breadcrumb
  // -------------------------------------------------------------------------
  describe('captureMessage', () => {
    it('grows the queue by one', () => {
      const before = getErrorStats().queueLength
      captureMessage('hello', 'info')
      expect(getErrorStats().queueLength).toBe(before + 1)
    })

    it('prefixes the message with [LEVEL] in the flushed payload', async () => {
      captureMessage('rate-limited', 'warning')
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      expect(body.errors[0].message).toBe('[WARNING] rate-limited')
    })

    it('defaults to info level when level is omitted', async () => {
      captureMessage('default level')
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      expect(body.errors[0].message).toBe('[INFO] default level')
    })

    it('forwards level + extra context to the context field', async () => {
      captureMessage('trace', 'error', { step: 'checkout' })
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      expect(body.errors[0].context).toEqual({ level: 'error', step: 'checkout' })
    })
  })

  // -------------------------------------------------------------------------
  // flush — batch POST to /api/client-errors
  // -------------------------------------------------------------------------
  describe('flush', () => {
    it('POSTs the queued errors to /api/client-errors as a batch', async () => {
      captureError(new Error('err-1'))
      captureError(new Error('err-2'))
      await flush()
      expect(fetch).toHaveBeenCalledTimes(1)
      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/client-errors')
      expect(init.method).toBe('POST')
      const body = JSON.parse(init.body as string)
      expect(body.errors).toHaveLength(2)
      expect(body.errors[0].message).toBe('err-1')
      expect(body.errors[1].message).toBe('err-2')
    })

    it('empties the queue after a successful flush', async () => {
      captureError(new Error('drain-me'))
      expect(getErrorStats().queueLength).toBe(1)
      await flush()
      expect(getErrorStats().queueLength).toBe(0)
    })

    it('is a no-op on an empty queue (no fetch call)', async () => {
      await flush()
      expect(fetch).not.toHaveBeenCalled()
    })

    it('silently swallows fetch rejections without throwing', async () => {
      captureError(new Error('will-fail-to-send'))
      // Simulate a network-down / backend 500 scenario.
      vi.mocked(fetch).mockRejectedValueOnce(new Error('Network down'))
      // The reporter MUST NOT propagate the failure — a down backend
      // should never become the user's problem.
      await expect(flush()).resolves.toBeUndefined()
    })

    it('silently swallows non-OK responses without throwing', async () => {
      captureError(new Error('will-404'))
      vi.mocked(fetch).mockResolvedValueOnce(
        new Response('Not Found', { status: 404 }),
      )
      await expect(flush()).resolves.toBeUndefined()
    })

    it('coalesces errors pushed within the 5s window into one POST', async () => {
      vi.useFakeTimers()
      vi.mocked(fetch).mockClear()
      // Push three errors in quick succession — none of these should
      // trigger an immediate flush; they should all batch behind the
      // single 5s timer.
      captureError(new Error('err-1'))
      captureError(new Error('err-2'))
      captureError(new Error('err-3'))
      expect(fetch).not.toHaveBeenCalled()
      // Advance the fake clock past the 5s debounce window. The timer
      // callback is async (it calls `void flush()` which awaits fetch),
      // so we use the Async variant to drain microtasks.
      await vi.advanceTimersByTimeAsync(5000)
      expect(fetch).toHaveBeenCalledTimes(1)
      const [, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(init.body as string)
      expect(body.errors).toHaveLength(3)
    })
  })

  // -------------------------------------------------------------------------
  // getErrorStats — session ID + queue introspection
  // -------------------------------------------------------------------------
  describe('getErrorStats', () => {
    it('returns a stable session ID across calls (one per page load)', () => {
      const a = getErrorStats()
      const b = getErrorStats()
      expect(a.sessionId).toBe(b.sessionId)
      // Session ID format: `${timestamp}-${random}` — sanity-check
      // the shape so a future refactor doesn't accidentally drop one half.
      expect(a.sessionId).toMatch(/^\d+-[a-z0-9]+$/)
    })

    it('reflects the current queue length', () => {
      expect(getErrorStats().queueLength).toBe(0)
      captureError(new Error('one'))
      expect(getErrorStats().queueLength).toBe(1)
      captureError(new Error('two'))
      expect(getErrorStats().queueLength).toBe(2)
    })
  })

  // -------------------------------------------------------------------------
  // installErrorHandlers — global window listeners
  // -------------------------------------------------------------------------
  describe('installErrorHandlers', () => {
    // Per-test capture of registered listeners. We stub
    // window.addEventListener to RECORD but NOT actually register, so
    // (a) listeners don't leak across tests (the listener function is a
    // fresh anonymous closure per `installErrorHandlers` call — without
    // the stub, every test would add another duplicate set of listeners
    // to the real window and they'd compound), and (b) we can invoke
    // the captured listener manually with a synthetic event to verify
    // the capture path end-to-end without depending on jsdom's event
    // bubbling (which has subtle differences from real browsers).
    let capturedListeners: Record<string, EventListener>
    let addSpy: ReturnType<typeof vi.spyOn>

    beforeEach(() => {
      capturedListeners = {}
      addSpy = vi
        .spyOn(window, 'addEventListener')
        .mockImplementation((type, fn) => {
          if (typeof fn === 'function') {
            capturedListeners[type as string] = fn as EventListener
          }
          return undefined
        })
    })

    afterEach(() => {
      addSpy.mockRestore()
    })

    it('registers `error`, `unhandledrejection`, and `beforeunload` listeners on window', () => {
      installErrorHandlers()
      const registeredTypes = Object.keys(capturedListeners)
      expect(registeredTypes).toContain('error')
      expect(registeredTypes).toContain('unhandledrejection')
      expect(registeredTypes).toContain('beforeunload')
    })

    it('the `error` listener captures an ErrorEvent into the queue', () => {
      installErrorHandlers()
      const before = getErrorStats().queueLength
      // Manually invoke the captured listener with a synthetic ErrorEvent
      // — avoids relying on jsdom event bubbling and keeps the test
      // deterministic (no chance of double-fire from accumulated
      // previous-test listeners).
      const synthetic = new ErrorEvent('error', {
        error: new Error('synthetic crash'),
        filename: 'app.js',
        lineno: 42,
        colno: 7,
      })
      capturedListeners['error'](synthetic)
      expect(getErrorStats().queueLength).toBe(before + 1)
    })

    it('the `error` listener forwards filename / lineno / colno as context', async () => {
      installErrorHandlers()
      capturedListeners['error'](
        new ErrorEvent('error', {
          error: new Error('boom'),
          filename: 'app.js',
          lineno: 42,
          colno: 7,
        }),
      )
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      expect(body.errors[0].context).toMatchObject({
        type: 'uncaught',
        filename: 'app.js',
        lineno: 42,
        colno: 7,
      })
    })

    it('the `unhandledrejection` listener captures a rejected promise reason', () => {
      installErrorHandlers()
      const before = getErrorStats().queueLength
      // Use a resolved promise in the event — the listener only reads
      // `event.reason`, so the actual promise state doesn't matter.
      // This avoids creating a real unhandled rejection that vitest's
      // global handler would flag.
      const synthetic = new PromiseRejectionEvent('unhandledrejection', {
        promise: Promise.resolve(),
        reason: new Error('rejected promise'),
      })
      capturedListeners['unhandledrejection'](synthetic)
      expect(getErrorStats().queueLength).toBe(before + 1)
    })

    it('the `unhandledrejection` listener coerces a non-Error reason to Error', async () => {
      installErrorHandlers()
      capturedListeners['unhandledrejection'](
        new PromiseRejectionEvent('unhandledrejection', {
          promise: Promise.resolve(),
          reason: 'string reason',
        }),
      )
      await flush()
      const call = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      const body = JSON.parse(call[1].body as string)
      // String reason is coerced via `new Error(String(reason))`, so the
      // message is the string itself.
      expect(body.errors[0].message).toBe('string reason')
      expect(body.errors[0].context).toEqual({ type: 'unhandled_promise_rejection' })
    })

    it('no-ops on the server (typeof window === undefined)', () => {
      const originalWindow = globalThis.window
      try {
        // Cast to a record so the delete is type-safe on the SSR test path.
        delete (globalThis as Record<string, unknown>).window
        // Must not throw — installErrorHandlers should be a silent no-op.
        expect(() => installErrorHandlers()).not.toThrow()
      } finally {
        ;(globalThis as Record<string, unknown>).window = originalWindow
      }
    })
  })

  // -------------------------------------------------------------------------
  // End-to-end capture → flush round-trip
  // -------------------------------------------------------------------------
  describe('end-to-end', () => {
    it('a captured error round-trips through the batch to the backend', async () => {
      const err = new Error('round-trip')
      err.stack = 'Error: round-trip\n    at foo (bar.js:1:1)'
      captureError(err, { componentStack: 'in <Boundary>' })

      // Stub the response so we can assert on the request body.
      vi.mocked(fetch).mockResolvedValueOnce(
        jsonResponse({ ok: true, received: 1 }),
      )

      await flush()

      const [url, init] = vi.mocked(fetch).mock.calls[0] as [string, RequestInit]
      expect(url).toContain('/api/client-errors')
      const body = JSON.parse(init.body as string)
      expect(body.errors).toHaveLength(1)
      const report = body.errors[0]
      // Required fields
      expect(report.message).toBe('round-trip')
      expect(report.stack).toContain('Error: round-trip')
      expect(report.url).toMatch(/^(https?:|about:blank)/) // jsdom uses about:blank or http://localhost
      expect(report.userAgent).toBeTypeOf('string')
      expect(report.timestamp).toBeTypeOf('number')
      expect(report.sessionId).toBeTypeOf('string')
      // Context forwarded verbatim
      expect(report.context).toEqual({ componentStack: 'in <Boundary>' })
    })
  })
})
