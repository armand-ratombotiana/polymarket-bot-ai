// src/components/ErrorBoundary.tsx — W10-3
// Root-level React Error Boundary. Wraps the entire application (mounted in
// src/app/layout.tsx). Catches render/lifecycle errors thrown anywhere in
// the React tree below it and shows a recoverable fallback UI instead of a
// blank white screen.
//
// IMPORTANT limitations:
//  - Error boundaries only catch errors thrown during rendering, in lifecycle
//    methods, and in constructors of components below them in the tree.
//  - They do NOT catch errors in event handlers, async code (setTimeout /
//    fetch / async-await), or errors thrown in the boundary itself.
//    Event-handler / async errors must use try/catch at the call site.
//  - Requires 'use client' because class components cannot be server
//    components and React.ErrorInfo / React.Component are client-only APIs.
//
// W14-8 — `componentDidCatch` now forwards the error + React componentStack
// to the client-side error reporter (`lib/errorReporter.ts`), which batches
// and POSTs to `/api/client-errors`. Event-handler / async errors are
// captured separately by the global window listeners installed in
// `ErrorReporterInit` (also mounted in `app/layout.tsx`).

'use client'

import React from 'react'
import { captureError } from '@/lib/errorReporter'

interface Props {
  children: React.ReactNode
  /** Optional custom fallback — when provided, replaces the default UI. */
  fallback?: React.ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
  errorInfo: React.ErrorInfo | null
  /** Toggles the collapsible stack-trace <details> panel. */
  showStack: boolean
  /** Counts how many times the user has hit "Try Again" since the error.
      Used to suggest a hard reload after repeated retries. */
  retryCount: number
}

export default class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = {
      hasError: false,
      error: null,
      errorInfo: null,
      showStack: false,
      retryCount: 0,
    }
  }

  // Static — called during the "render phase" of an error. We must NOT trigger
  // side-effects here (no logging, no API calls); only return a state slice
  // that will cause `render()` to show the fallback. React calls this before
  // `componentDidCatch`.
  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error }
  }

  // Called after the error has been committed to state. Safe to side-effect
  // here (logging, telemetry). We capture the componentStack via
  // `errorInfo.componentStack` and stash it on state so the fallback UI can
  // show it in a collapsible <details>.
  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // Log to dev console for debugging (always-on local telemetry).
    console.error('[ErrorBoundary] Caught render error:', error, errorInfo)
    // W14-8 — Forward to the client-side error reporter. The reporter
    // batches reports and POSTs them to `/api/client-errors` on a 5s
    // cadence (so a render loop doesn't fire 100 POSTs). The
    // componentStack is forwarded as context so the backend log shows
    // the React component hierarchy that produced the error, not just
    // the JS stack.
    captureError(error, { componentStack: errorInfo.componentStack })
    this.setState({ errorInfo })
  }

  handleReset = () => {
    // Clears the error state so the boundary re-renders its children. If the
    // underlying cause has been fixed (e.g. transient null dereference that
    // a parent effect has since patched), the app resumes normally.
    this.setState((prev) => ({
      hasError: false,
      error: null,
      errorInfo: null,
      showStack: false,
      retryCount: prev.retryCount + 1,
    }))
  }

  handleReload = () => {
    if (typeof window !== 'undefined') {
      window.location.reload()
    }
  }

  toggleStack = () => {
    this.setState((prev) => ({ showStack: !prev.showStack }))
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children
    }

    // Allow a custom fallback to override the default UI entirely. This is
    // useful for tests or for embedding the boundary in a context where the
    // full-screen error card is the wrong shape.
    if (this.props.fallback) {
      return this.props.fallback
    }

    const { error, errorInfo, showStack, retryCount } = this.state
    const message = error?.message ?? 'Unknown error'
    const stack = error?.stack ?? ''
    const componentStack = errorInfo?.componentStack ?? ''
    const repeatedRetries = retryCount >= 2

    return (
      <div
        className="error-boundary-fallback"
        role="alertdialog"
        aria-labelledby="error-boundary-title"
        aria-describedby="error-boundary-desc"
      >
        <div className="error-boundary-icon" aria-hidden="true">⚠</div>
        <h2 id="error-boundary-title" className="error-boundary-title">
          Something went wrong
        </h2>
        <p id="error-boundary-desc" className="error-boundary-message">
          {message}
        </p>

        {repeatedRetries && (
          <p className="error-boundary-hint">
            Retrying didn&apos;t resolve this. Try reloading the page — your
            browser cache may hold a stale chunk.
          </p>
        )}

        <div className="error-boundary-actions">
          <button
            type="button"
            className="btn btn-primary"
            onClick={this.handleReset}
          >
            ↻ Try Again
          </button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={this.handleReload}
          >
            ⟳ Reload Page
          </button>
        </div>

        {(stack || componentStack) && (
          <details className="error-boundary-stack" open={showStack}>
            <summary
              onClick={(e) => {
                // Prevent default toggle so we control open state ourselves;
                // this keeps the button-style summary consistent with the rest
                // of the dashboard and lets us animate the panel.
                e.preventDefault()
                this.toggleStack()
              }}
            >
              {showStack ? '▾ Hide stack trace' : '▸ Show stack trace'}
            </summary>
            {showStack && (
              <pre className="error-boundary-stack-content scrollbar-thin">
                {stack && (
                  <>
                    <span className="error-boundary-stack-label">
                      Error stack:
                    </span>
                    {'\n'}
                    {stack}
                    {'\n\n'}
                  </>
                )}
                {componentStack && (
                  <>
                    <span className="error-boundary-stack-label">
                      Component stack:
                    </span>
                    {'\n'}
                    {componentStack}
                  </>
                )}
              </pre>
            )}
          </details>
        )}
      </div>
    )
  }
}
