// src/components/PanelErrorBoundary.tsx — W10-3
// Panel-scoped Error Boundary. A lighter-weight companion to the root
// ErrorBoundary: wraps an individual panel (one of the switch-cases in
// src/app/page.tsx) so that a crash in ONE panel (e.g. malformed API
// payload causing a TypeError during render) does NOT take down the entire
// dashboard. The other sidebar sections keep working; only the crashed
// panel shows a recoverable inline fallback.
//
// Same lifecycle-catch semantics as ErrorBoundary (render-phase errors only).
// Use try/catch in async panel fetch hooks for non-render errors.

'use client'

import React from 'react'

interface Props {
  children: React.ReactNode
  /** Optional human label for the panel — shown in the fallback so the user
      knows which panel crashed. Defaults to "This panel". */
  label?: string
  /** Optional custom fallback — overrides the default inline UI. */
  fallback?: React.ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

export default class PanelErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo) {
    // Log to dev console. Includes the panel label so multi-panel crashes
    // are easy to attribute in the dev console without expanding the trace.
    console.error(
      `[PanelErrorBoundary${this.props.label ? `: ${this.props.label}` : ''}]`,
      'caught render error:',
      error,
      errorInfo,
    )
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null })
  }

  handleReload = () => {
    if (typeof window !== 'undefined') {
      window.location.reload()
    }
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children
    }

    if (this.props.fallback) {
      return this.props.fallback
    }

    const label = this.props.label ?? 'This panel'
    const message = this.state.error?.message ?? 'Unknown render error'

    return (
      <div
        className="panel-error-boundary"
        role="alert"
        aria-live="assertive"
      >
        <div className="panel-error-boundary-body">
          <span className="panel-error-boundary-icon" aria-hidden="true">⚠</span>
          <div className="panel-error-boundary-text">
            <div className="panel-error-boundary-title">
              {label} encountered an error
            </div>
            <div className="panel-error-boundary-message mono" title={message}>
              {message}
            </div>
          </div>
        </div>
        <div className="panel-error-boundary-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={this.handleReset}
          >
            ↻ Retry
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={this.handleReload}
          >
            ⟳ Reload
          </button>
        </div>
      </div>
    )
  }
}
