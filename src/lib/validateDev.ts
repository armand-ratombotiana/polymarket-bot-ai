// lib/validateDev.ts — Dev-mode schema validator helper.
//
// W10-5 — Runtime type safety net for the frontend.
//
// Why this file exists:
//   When a backend response fails Zod validation, the production-safe
//   behaviour is to silently fall back to raw data (so the UI keeps
//   working). But in development, silent fallbacks hide API drift —
//   a renamed field looks like a frontend bug until someone notices the
//   console has no errors.
//
//   This module is the single point where schema validation errors are
//   surfaced. In `process.env.NODE_ENV === 'development'`, errors are
//   logged to `console.error` with a structured, readable issue tree
//   (URL, raw payload, and the Zod issue path/message). In production,
//   every function is a no-op — the call sites stay in the bundle but
//   cost nothing at runtime (a single env-check branch).
//
// Usage:
//   `safeFetch` and `safeParse` (in `lib/safeFetch.ts`) call
//   `logSchemaError` automatically. Consumers don't need to call this
//   directly — but they can, e.g. for ad-hoc validation outside the
//   fetch wrapper.
//
// Design choices:
//   * Uses `process.env.NODE_ENV` rather than a `__DEV__` global because
//     Next.js 16 + bundlers tree-shake `if (process.env.NODE_ENV !==
//     'production')` branches in production builds (dead-code elimination).
//     This makes the dev-only console.error calls free in prod bundles.
//   * Each Zod issue is formatted with its JSON path (e.g.
//     `positions[0].avg_price`) so the developer can find the broken
//     field at a glance.
//   * The raw payload is included as the second console.error argument
//     (collapsible in browser devtools) rather than JSON-stringified,
//     so developers can inspect it interactively.

import type { ZodError, ZodType } from 'zod'

const isDev =
  typeof process !== 'undefined' &&
  process.env &&
  process.env.NODE_ENV !== 'production'

/**
 * Format a Zod issue path array as a dotted path string. Array indices
 * are rendered with `[i]` to mirror JS syntax (e.g.
 * `positions[0].avg_price`).
 */
function formatPath(path: (string | number)[]): string {
  if (path.length === 0) return '<root>'
  return path
    .map((p, i) => {
      if (typeof p === 'number') return `[${p}]`
      return i === 0 ? String(p) : `.${p}`
    })
    .join('')
}

/**
 * Log a schema validation error in development. No-op in production.
 *
 * @param url       The URL or label identifying the source of the payload
 *                  (e.g. `/api/positions` or `<ws-message>`).
 * @param raw       The raw JSON payload that failed validation.
 * @param zodError  The ZodError returned by `schema.safeParse(...)`.
 */
export function logSchemaError(
  url: string,
  raw: unknown,
  zodError: ZodError,
): void {
  if (!isDev) return

  // Zod v4 issues are under `.issues` (also available as `.errors` alias).
  const issues = (zodError as unknown as { issues?: ZodIssue[]; errors?: ZodIssue[] }).issues ?? []
  const issueLines = issues.map(
    (iss: ZodIssue) =>
      `  • ${formatPath(iss.path)} — ${iss.code}${iss.message ? `: ${iss.message}` : ''}`,
  )

  console.error(
    `[schema] ${url} — ${issues.length} validation issue(s):`,
    '\n' + issueLines.join('\n'),
    '\nraw payload:',
    raw,
  )
}

/**
 * Validate a value against a Zod schema in dev only. In production,
 * returns `{ success: true, data: value as T }` without actually
 * running the schema (skips the validation cost).
 *
 * Useful for hot paths (e.g. WebSocket message handlers) where the
 * validation is desirable for debugging but shouldn't impact
 * production latency.
 *
 * @example
 * ```ts
 * const r = validateDev(parsed, SnapshotSchema)
 * if (!r.success) console.warn('bad snapshot', r.error)
 * ```
 */
export function validateDev<T>(
  value: unknown,
  schema: ZodType<T>,
): { success: true; data: T } | { success: false; error: string } {
  if (!isDev) {
    // Production fast-path: skip validation entirely. Caller still has
    // to branch on `.success`, but the schema never runs.
    return { success: true, data: value as T }
  }
  const result = schema.safeParse(value)
  if (result.success) return { success: true, data: result.data }
  logSchemaError('<validateDev>', value, result.error)
  return { success: false, error: result.error.message }
}

// Minimal local ZodIssue shape to avoid importing the full ZodError type
// (which differs slightly between Zod v3 and v4). We only read `.path`
// and `.code` and `.message`.
interface ZodIssue {
  path: (string | number)[]
  code: string
  message?: string
}
