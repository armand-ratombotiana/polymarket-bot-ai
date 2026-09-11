// src/components/ui/states.tsx — W41-3 Reusable panel-state primitives.
//
// The dashboard has ~30 panels that all need the same five lifecycle
// states (loading / empty / error / stale / disconnected). Before W41-3
// each panel re-implemented its own ad-hoc version of these states,
// which led to drift: some panels showed a spinner with no message,
// some showed "no data" without an icon, some had no error retry at
// all. This module is the single source of truth for panel states so
// every panel renders the same vocabulary.
//
// Visual language:
//   * PanelSkeleton   — shimmering skeleton lines (uses shadcn's Skeleton).
//   * EmptyState      — friendly "no data" message with an icon.
//   * ErrorState      — red-tinted error message with an optional Retry
//                       button. Uses the project's `.error-state` CSS
//                       classes for consistency with the existing
//                       empty-state pattern.
//   * StaleIndicator  — inline amber pill that surfaces "data is Xs old"
//                       in the panel header. Hidden when data is fresh
//                       (<30s); amber when 30–120s; red when >120s.
//   * DisconnectedState — banner shown when the panel's backend is
//                       unreachable. Distinct from ErrorState: this is
//                       for connection failures (network down, backend
//                       down), whereas ErrorState is for HTTP errors or
//                       data-shape issues.
//
// All components are framework-agnostic (plain divs, no 'use client' needed
// — except for ErrorState / DisconnectedState's retry button, which uses
// the shadcn Button. The Button itself is a server-compatible component,
// so these remain server-renderable).
//
// Every exported component renders a `data-testid` so tests can target
// the state without relying on text matching (which is fragile across
// i18n + copy edits).

import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'

// ─────────────────────────────────────────────────────────────────────────
// PanelSkeleton — shimmering loading placeholder.
// ─────────────────────────────────────────────────────────────────────────

/**
 * PanelSkeleton — Reusable loading skeleton shown while a panel fetches
 * its initial data. Renders N shimmering lines using the project's
 * existing Skeleton primitive. The last line is half-width so the block
 * reads as "content" rather than a uniform grid.
 *
 * Designed to slot into a panel's body where a table or grid would
 * normally render. Uses `role="status"` + `aria-live="polite"` so
 * screen readers announce "loading" without flickering the visual.
 */
export function PanelSkeleton({ lines = 3 }: { lines?: number }) {
  const safeLines = Math.max(1, Math.min(lines, 12))
  return (
    <div
      className="flex flex-col gap-2 py-6 px-3"
      role="status"
      aria-live="polite"
      aria-label="Loading content"
      data-testid="panel-skeleton"
    >
      {Array.from({ length: safeLines }).map((_, i) => (
        <Skeleton
          key={i}
          className={`h-4 ${i === safeLines - 1 ? 'w-1/2' : 'w-full'}`}
        />
      ))}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// EmptyState — friendly "no data" placeholder.
// ─────────────────────────────────────────────────────────────────────────

/**
 * EmptyState — Reusable friendly empty-state placeholder shown when a
 * panel has no data to display. Uses the existing `.empty-state` CSS
 * classes (declared in src/app/globals.css) so the visual language
 * stays consistent with the rest of the dashboard.
 */
export function EmptyState({
  icon = '📭',
  title,
  message,
  action,
}: {
  /** Emoji or short string rendered as the prominent icon. Empty string hides. */
  icon?: string
  /** Bold title — required so screen readers always have a label. */
  title: string
  /** Optional one-line description rendered below the title. */
  message?: string
  /** Optional trailing element (e.g. a "Reset filters" button). */
  action?: React.ReactNode
}) {
  return (
    <div
      className="empty-state"
      role="status"
      data-testid="panel-empty-state"
    >
      {icon && (
        <span className="empty-state-icon" aria-hidden="true">
          {icon}
        </span>
      )}
      <span className="empty-state-title">{title}</span>
      {message && <span className="empty-state-desc">{message}</span>}
      {action}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// ErrorState — red-tinted error message with optional Retry button.
// ─────────────────────────────────────────────────────────────────────────

/**
 * ErrorState — Reusable error-state placeholder shown when a panel's
 * data fetch fails. Includes a Retry button when `onRetry` is provided.
 * Uses the existing `.error-state` CSS classes for visual consistency
 * with the rest of the dashboard's empty/error pattern.
 *
 * `message` is the user-facing copy ("Analytics data unavailable",
 * "Connecting to ML API…", etc.). When the caller wants to also expose
 * the underlying error string from the data hook (e.g. "HTTP 500"),
 * pass it as `detail` — rendered as a smaller line below the message.
 */
export function ErrorState({
  message = 'Unable to load data',
  detail,
  onRetry,
  retryLabel = 'Retry',
}: {
  message?: string
  /** Optional smaller second line — typically the raw error string. */
  detail?: string | null
  /** When provided, renders a Retry button that calls this callback. */
  onRetry?: () => void
  /** Overrides the default "Retry" button label. */
  retryLabel?: string
}) {
  return (
    <div
      className="error-state"
      role="alert"
      data-testid="panel-error-state"
    >
      <span className="error-state-icon" aria-hidden="true">
        ⚠️
      </span>
      <span className="error-state-title">{message}</span>
      {detail && (
        <span className="error-state-desc" style={{ fontFamily: 'var(--font-mono, monospace)' }}>
          {detail}
        </span>
      )}
      {onRetry && (
        <Button
          size="sm"
          variant="outline"
          onClick={onRetry}
          className="mt-1"
          data-testid="panel-error-retry"
          aria-label={retryLabel}
        >
          ⟳ {retryLabel}
        </Button>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// StaleIndicator — inline "data is Xs old" pill for the panel header.
// ─────────────────────────────────────────────────────────────────────────

/**
 * StaleIndicator — Inline amber pill rendered when data is older than
 * the freshness threshold. The `age` is the data's age in seconds.
 *
 * Visual:
 *   * <30s   — not rendered (data is fresh; the pill would be noise).
 *   * 30–120s — amber "Stale · {Ns}" pill.
 *   * >120s  — red "Dead · {Nm}" pill.
 *
 * Render this in a panel's header next to the Live/Polling badge so
 * a trader can tell at a glance whether the displayed numbers are
 * current. The pill's `title` attribute carries the full age + a
 * human-readable verdict for hover + screen-reader context.
 */
export function StaleIndicator({ age }: { age: number }) {
  if (!Number.isFinite(age) || age < 30) return null
  const label = age < 60 ? `${Math.round(age)}s` : `${Math.floor(age / 60)}m`
  const isDead = age >= 120
  return (
    <span
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[9.5px] font-bold uppercase tracking-wide border ${
        isDead
          ? 'bg-red-500/15 text-red-300 border-red-500/40'
          : 'bg-amber-500/15 text-amber-300 border-amber-500/40'
      }`}
      title={`Data is ${Math.round(age)}s old — ${
        isDead ? 'considered dead (>120s)' : 'stale (>30s)'
      }`}
      data-testid="panel-stale-indicator"
      data-stale-level={isDead ? 'dead' : 'stale'}
      aria-label={`Data is ${Math.round(age)} seconds old, ${
        isDead ? 'dead' : 'stale'
      }`}
    >
      <span
        className={`inline-block w-1.5 h-1.5 rounded-full ${
          isDead ? 'bg-red-400' : 'bg-amber-400'
        }`}
        aria-hidden="true"
      />
      {isDead ? 'Dead' : 'Stale'} · {label}
    </span>
  )
}

// ─────────────────────────────────────────────────────────────────────────
// DisconnectedState — banner for "backend unreachable".
// ─────────────────────────────────────────────────────────────────────────

/**
 * DisconnectedState — Reusable banner shown when the panel's backend
 * connection is unavailable (WS down + REST polling failing). Includes
 * a Retry button when `onRetry` is provided.
 *
 * Distinct from ErrorState: this is for connection failures (network
 * down, backend down, ECONNREFUSED), whereas ErrorState is for HTTP
 * errors (4xx/5xx) or data-shape issues. The visual language is
 * intentionally different so a trader can tell at a glance whether
 * the problem is "the data is wrong" (ErrorState) or "the wire is
 * dead" (DisconnectedState).
 */
export function DisconnectedState({
  onRetry,
  message = 'Backend unavailable',
  hint = 'The backend service appears to be unreachable. Retrying will attempt to reconnect.',
  retryLabel = 'Retry Connection',
}: {
  onRetry?: () => void
  message?: string
  hint?: string
  retryLabel?: string
}) {
  return (
    <div
      className="flex flex-col items-center justify-center py-8 px-4 text-center gap-2"
      role="alert"
      data-testid="panel-disconnected-state"
    >
      <span className="text-2xl" aria-hidden="true">
        🔌
      </span>
      <span className="text-sm font-semibold text-red-300">{message}</span>
      <span className="text-xs text-[#7e8aaa] max-w-xs">{hint}</span>
      {onRetry && (
        <Button
          size="sm"
          variant="outline"
          onClick={onRetry}
          className="mt-2"
          data-testid="panel-disconnected-retry"
          aria-label={retryLabel}
        >
          ⟳ {retryLabel}
        </Button>
      )}
    </div>
  )
}
