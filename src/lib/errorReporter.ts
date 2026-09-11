// lib/errorReporter.ts — W14-8 Client-side error reporter (Sentry-like).
//
// Captures three classes of frontend failure:
//   1. Uncaught runtime errors (via the global `error` window event).
//   2. Unhandled promise rejections (via the `unhandledrejection` event).
//   3. React render/lifecycle errors (via ErrorBoundary.componentDidCatch).
//
// Plus a manual API for opportunistic breadcrumbs:
//   - captureError(errOrString, context?)    — log an error
//   - captureMessage(msg, level?, context?)  — log a non-error message
//
// Reports are batched in an in-memory queue and flushed to the backend
// `/api/client-errors` endpoint every 5 seconds (so a page with many
// small errors doesn't fire 50 separate POSTs). The endpoint is public
// (no auth required) so error reporting still works when the trader's
// API token is misconfigured or expired — operators would rather see
// the auth failure in the error log than lose the telemetry that
// would have surfaced it.
//
// Failure-mode contract: the reporter must NEVER throw, and NEVER cause
// an infinite error loop. Every fetch is wrapped in try/catch +
// `.catch(() => {})`. If the backend is down or the URL 404s, the
// reporter silently drops the batch — degrading gracefully is more
// important than re-trying (which could amplify load during an outage).
//
// SSR safety: every entry point is `typeof window === 'undefined'`-guarded
// so importing this module from a server component is a no-op. The
// `installErrorHandlers()` function is a no-op on the server.
'use client'

import { apiFetch } from '@/lib/api'

export interface ErrorReport {
  message: string
  stack?: string
  filename?: string
  lineno?: number
  colno?: number
  url: string
  userAgent: string
  timestamp: number
  userId?: string
  sessionId: string
  release?: string
  context?: Record<string, unknown>
}

// Module-level state — one session per page load. The session ID lets the
// backend correlate a chain of errors (e.g. an initial render crash
// followed by a retry crash) to a single user visit. Regenerated on full
// page reload (intentional — a refresh is a new "session" from the user's
// perspective).
const SESSION_ID: string = generateSessionId()

// Batching queue — drained by `flush()` every 5s (debounced) or on
// `beforeunload`. We keep this mutable + module-level rather than per-
// instance because the reporter is a process-wide singleton.
const ERROR_QUEUE: ErrorReport[] = []

// Active debounce timer for the 5s flush. `null` when no flush is pending.
let flushTimer: ReturnType<typeof setTimeout> | null = null

function generateSessionId(): string {
  // `${timestamp}-${random}` is unique enough for client-side correlation;
  // the backend dedupes by (sessionId, timestamp, message) tuple when
  // aggregating, so collisions would still be benign.
  return `${Date.now()}-${Math.random().toString(36).slice(2, 11)}`
}

function getUrl(): string {
  if (typeof window === 'undefined') return ''
  return window.location.href
}

function getUserAgent(): string {
  if (typeof window === 'undefined') return 'SSR'
  return navigator.userAgent
}

/**
 * Capture an error (Error instance OR string message) and queue it for
 * delivery to the backend. Safe to call from anywhere — event handlers,
 * async catch blocks, ErrorBoundary.componentDidCatch.
 *
 * The optional `context` object is forwarded verbatim to the backend
 * (e.g. `{ componentStack }` from React.ErrorInfo, or `{ type: 'uncaught' }`
 * from the global error handler). Avoid putting non-serializable values
 * (DOM nodes, class instances) in here — they'll be JSON.stringify'd.
 */
export function captureError(
  error: Error | string,
  context?: Record<string, unknown>,
): void {
  if (typeof window === 'undefined') return

  const report: ErrorReport = {
    message: typeof error === 'string' ? error : error.message,
    stack: typeof error === 'string' ? undefined : error.stack,
    url: getUrl(),
    userAgent: getUserAgent(),
    timestamp: Date.now(),
    sessionId: SESSION_ID,
    context,
  }

  ERROR_QUEUE.push(report)

  // Mirror to dev console so a developer with devtools open sees the
  // error immediately even before the 5s batch flushes. Tagged with
  // `[ErrorReporter]` so it's filterable from regular console noise.
  console.error('[ErrorReporter]', report.message, context ?? '')

  scheduleFlush()
}

/**
 * Capture a non-error message (info / warning / breadcrumb). Useful for
 * tracing user flows leading up to a crash — e.g. "user opened trade modal",
 * "user clicked Submit", "API returned 503".
 *
 * `level` defaults to `'info'` and is prefixed into the message string so
 * the backend's plain-text logger (which only has `message`) can still
 * filter by severity.
 */
export function captureMessage(
  message: string,
  level: 'info' | 'warning' | 'error' = 'info',
  context?: Record<string, unknown>,
): void {
  if (typeof window === 'undefined') return

  const report: ErrorReport = {
    message: `[${level.toUpperCase()}] ${message}`,
    url: getUrl(),
    userAgent: getUserAgent(),
    timestamp: Date.now(),
    sessionId: SESSION_ID,
    context: { level, ...context },
  }

  ERROR_QUEUE.push(report)
  scheduleFlush()
}

/**
 * Schedule a deferred flush (5s). Subsequent calls within the 5s window
 * are coalesced — only the first call arms the timer, so 100 errors in
 * 5s produce exactly ONE POST, not 100.
 */
function scheduleFlush() {
  if (flushTimer) return
  flushTimer = setTimeout(() => {
    void flush()
  }, 5000)
}

/**
 * Drain the queue and POST the batch to `/api/client-errors`.
 *
 * Uses `apiFetch` (the codebase's authed + gateway-port-aware transport)
 * so the URL gets `?XTransformPort=8080` appended automatically — without
 * it the request would land on Next.js port 3000 and 404 silently.
 *
 * Failures are silently swallowed: a down backend must NOT propagate to
 * the trader's UI, and must NOT trigger an infinite "error → report →
 * fetch fails → throws → report → ..." loop. The `.catch(() => {})`
 * on the fetch + the outer try/catch both defend against this.
 */
export async function flush(): Promise<void> {
  // Clear the pending-timer flag immediately so a new error arriving
  // during the (async) fetch window re-arms a fresh 5s timer rather
  // than being lost when the splice() below empties the queue.
  flushTimer = null
  if (ERROR_QUEUE.length === 0) return

  // Splice (not pop) so concurrent callers see an empty queue and bail.
  // The spliced batch is what we POST — anything pushed after this
  // point goes into the next 5s window.
  const reports = ERROR_QUEUE.splice(0, ERROR_QUEUE.length)

  try {
    await apiFetch('/api/client-errors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ errors: reports }),
    }).catch(() => {
      // Silent fail — don't cause an infinite error loop. The reports
      // are already spliced out of the queue, so we've lost them; that
      // is the intended degradation (a down backend shouldn't OOM the
      // client by accumulating an unbounded queue).
    })
  } catch {
    // Double defence — even if the `.catch` above is somehow bypassed
    // (it shouldn't be), the outer try/catch ensures we never throw.
  }
}

/**
 * Install global window listeners for uncaught errors and unhandled
 * promise rejections. Idempotent — calling it twice registers duplicate
 * listeners (which would double-report), so the caller (ErrorReporterInit)
 * should ensure it's only called once per page load.
 *
 * Also wires `beforeunload` to drain the queue synchronously-ish (the
 * fetch is fire-and-forget; `navigator.sendBeacon` would be more
 * reliable but our endpoint isn't Beacon-shaped, so we accept that some
 * errors may be lost on tab-close).
 */
export function installErrorHandlers(): void {
  if (typeof window === 'undefined') return

  // Uncaught runtime errors — script parse errors, undefined-is-not-a-
  // function, etc. `event.error` is the Error instance when available;
  // `event.message` is the fallback (string) when the browser redacts
  // the stack for cross-origin scripts (no `crossorigin` attribute).
  window.addEventListener('error', (event) => {
    captureError(event.error || event.message, {
      type: 'uncaught',
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    })
  })

  // Unhandled promise rejections — `await fetch()` that rejects without
  // a try/catch, or a `.then()` chain with no `.catch()`. `event.reason`
  // is typically an Error but can be anything (a string, an object, etc.)
  // — we coerce to Error so the reporter's `stack` field is populated
  // when possible.
  window.addEventListener('unhandledrejection', (event) => {
    const reason = event.reason
    captureError(
      reason instanceof Error ? reason : new Error(String(reason)),
      { type: 'unhandled_promise_rejection' },
    )
  })

  // Drain on page unload — fire the fetch without awaiting. Note: this
  // is best-effort; the browser may tear down the page before the
  // request completes. For high-stakes reliability, switch to
  // `navigator.sendBeacon` (would require a separate beacon-shaped
  // endpoint that accepts the same JSON body as a plain POST).
  window.addEventListener('beforeunload', () => {
    void flush()
  })
}

/**
 * Read-only stats for debugging / tests. Exposes the active session ID
 * (so a test can assert it stays stable across calls) and the current
 * queue length (so a test can assert that captureError grew the queue
 * and flush emptied it).
 */
export function getErrorStats(): {
  sessionId: string
  queueLength: number
} {
  return {
    sessionId: SESSION_ID,
    queueLength: ERROR_QUEUE.length,
  }
}

/**
 * Test-only helper — clears the queue + cancels any pending flush timer
 * so each test starts from a known-empty baseline. NOT exported from
 * the index barrel; intended for unit tests only.
 *
 * The leading underscore is the conventional "this is private — don't
 * call from app code" signal; the function is still shipped to clients
 * (no tree-shaking away of unused exports) but the cost is negligible.
 */
export function _resetForTests(): void {
  ERROR_QUEUE.length = 0
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
}
