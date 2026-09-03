// lib/safeFetch.ts — A fetch wrapper that validates the JSON response
// against a Zod schema at runtime.
//
// W10-5 — Runtime type safety net for the frontend.
//
// Why this file exists:
//   `apiFetch` (in `lib/api.ts`) injects auth headers + the gateway port
//   but returns a raw `Response` whose `.json()` payload is `Promise<any>`.
//   That `any` flows straight into React state — a backend that drops a
//   field or changes a type produces `undefined`/`NaN` in the UI with no
//   visible error in the console.
//
//   `safeFetch` wraps `apiFetch` and runs the parsed JSON through a Zod
//   schema, returning a discriminated union:
//
//     { success: true,  data: T }              // schema matched
//     { success: false, error: string, raw: unknown } // HTTP error OR schema mismatch
//
//   Callers can:
//     1. Branch on `.success` and surface a degraded state on failure.
//     2. Forward `raw` to `validateDev.logSchemaError` in dev to get a
//        console-visible issue tree (see `lib/validateDev.ts`).
//
// Design choices:
//   * Reuses `apiFetch` (not the global `fetch`) so the auth + gateway
//     port injection stays consistent. This means callers using
//     `safeFetch` automatically get the `XTransformPort=8080` query param
//     appended for `/api/*` URLs.
//   * The schema param is `ZodType<T>` (the runtime class), not the
//     pure type `ZodSchema<T>`. Zod v4 renamed `ZodSchema` to `ZodType`;
//     `ZodSchema` is no longer exported as a value (only as a deprecated
//     type alias in some builds). Using `ZodType` is forward-compatible.
//   * `raw` is captured even on parse failure so dev tooling can show
//     the original JSON alongside the validation error.
//   * Network errors (fetch throws) are caught and returned as
//     `{ success: false }` rather than re-thrown — callers don't have to
//     wrap every call site in try/catch.

import type { ZodType } from 'zod'
import { apiFetch } from '@/lib/api'
import { logSchemaError } from '@/lib/validateDev'

export type SafeFetchSuccess<T> = { success: true; data: T }
export type SafeFetchFailure = { success: false; error: string; raw: unknown }
export type SafeFetchResult<T> = SafeFetchSuccess<T> | SafeFetchFailure

/**
 * Fetch a URL, validate the JSON response against a Zod schema, and return
 * a discriminated union of either `{ success: true, data }` or
 * `{ success: false, error, raw }`.
 *
 * In development, parse failures are also logged via `logSchemaError` so
 * API drift surfaces loudly in the browser console.
 *
 * @example
 * ```ts
 * const r = await safeFetch('/api/positions', PositionsResponseSchema)
 * if (r.success) {
 *   setPositions(r.data)            // typed as Position[]
 * } else {
 *   console.warn('positions fetch failed:', r.error)
 *   setPositions([])                // safe fallback
 * }
 * ```
 */
export async function safeFetch<T>(
  url: string,
  schema: ZodType<T>,
  init?: RequestInit,
): Promise<SafeFetchResult<T>> {
  try {
    const res = await apiFetch(url, init)
    if (!res.ok) {
      return {
        success: false,
        error: `HTTP ${res.status} ${res.statusText || ''}`.trim(),
        raw: null,
      }
    }

    let json: unknown
    try {
      json = await res.json()
    } catch (e) {
      return {
        success: false,
        error: `Response was not valid JSON: ${String(e)}`,
        raw: null,
      }
    }

    const result = schema.safeParse(json)
    if (result.success) {
      return { success: true, data: result.data }
    }

    // Schema mismatch — surface to dev console (no-op in production).
    logSchemaError(url, json, result.error)

    return {
      success: false,
      error: result.error.message,
      raw: json,
    }
  } catch (e) {
    return {
      success: false,
      error: String(e),
      raw: null,
    }
  }
}

/**
 * Synchronous variant — validate an already-parsed JSON value against a
 * Zod schema. Useful for WebSocket message payloads (where the JSON is
 * already parsed before reaching the validator).
 *
 * Returns the same discriminated union shape as `safeFetch` so callers
 * can use the same branching pattern for both REST and WS paths.
 *
 * @example
 * ```ts
 * ws.onmessage = (e) => {
 *   const data = JSON.parse(e.data)
 *   const r = safeParse(data, SnapshotSchema)
 *   if (r.success) setSnapshot(r.data)
 * }
 * ```
 */
export function safeParse<T>(value: unknown, schema: ZodType<T>): SafeFetchResult<T> {
  const result = schema.safeParse(value)
  if (result.success) {
    return { success: true, data: result.data }
  }
  logSchemaError('<ws-message>', value, result.error)
  return { success: false, error: result.error.message, raw: value }
}
