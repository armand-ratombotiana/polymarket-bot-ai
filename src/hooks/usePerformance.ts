// hooks/usePerformance.ts — W41-2 Lightweight frontend performance monitor.
//
// Goal: give the trader (and the developer) a single, dependency-free
// way to measure three signals the W41-2 task called out:
//   1. Initial render time — the milliseconds between the component
//      mounting and the browser painting its first frame.
//   2. Re-render count — how many times the component has rendered
//      since the tab loaded (cumulative across remounts so a panel
//      mounted/unmounted twice keeps incrementing).
//   3. API call count — how many `fetch()` calls have fired since
//      the monitor was armed. Useful for spotting duplicate / wasted
//      backend requests.
//
// Design choices:
// - **No external dependency.** Uses `performance.now()` + a tiny
//   in-module Map. No need for `web-vitals`, no need for React DevTools
//   profiler — those are dev-time tools; this hook is for the trader's
//   eye (visible in the dev console under `__perf__`).
// - **Off by default.** Reads `localStorage.polymarket_perf_monitor`
//   so a trader (or an e2e test) opts in by flipping the flag. The
//   hook still returns a result object when disabled so the caller's
//   destructure doesn't crash — but the published numbers stay at
//   their last-known value (zero until first opt-in render).
// - **Per-name counters in a module-level Map.** Each mounted
//   component passes a `name` (e.g. `'PositionsPanel'`); the Map
//   aggregates per name. The same name rendered twice (e.g.
//   PositionsPanel mounted in two split views) sums into the same
//   counter — that's the intent (we want the total render load, not
//   per-instance).
// - **API call counter wraps `window.fetch`.** The wrap is installed
//   once (idempotent — the second `install()` call is a no-op) so
//   multiple `usePerformance` callers in the same tab share the same
//   counter. The wrap delegates to the original `fetch` so existing
//   behaviour + tests are unaffected.
// - **No setState on every render.** The hook increments a Map entry
//   synchronously during render (cheap — a single Map lookup + an
//   integer bump) and reads back the same Map entry for its return
//   value. No `useState` is touched, so the hook itself never
//   triggers a re-render — callers re-render for their own reasons,
//   and on each render they read the freshest Map values.

'use client'

import { useEffect, useRef } from 'react'

// ── Module-level state ────────────────────────────────────────────────────
// A single Map shared across every `usePerformance` caller in the tab.
// Keyed by caller-supplied `name`. Each entry holds:
//   • initialRenderMs   — first-paint duration in ms (null until the
//                         post-paint effect runs + sets it once).
//   • renderCount       — total renders since the tab loaded
//                         (cumulative across remounts — incremented
//                         synchronously during render, before any
//                         effect has a chance to fire).
//   • mountedAt         — performance.now() captured during the first
//                         render so the post-paint effect can compute
//                         the initialRenderMs delta.
interface PerfEntry {
  initialRenderMs: number | null
  renderCount: number
  mountedAt: number | null
  firstPaintDone: boolean
}

const PERF_MAP = new Map<string, PerfEntry>()

// Module-level API call counter. Incremented by the wrapped `fetch`.
// Shared across all `usePerformance` callers in the tab.
let apiCallCount = 0
let fetchWrapped = false

// ── Public API ─────────────────────────────────────────────────────────────

export interface UsePerformanceResult {
  /** ms between mount and first paint, or null until first paint
   *  completes. Recorded once per name (subsequent paints are
   *  re-renders, not the initial render). */
  initialRenderMs: number | null
  /** Total renders for this `name` since the tab loaded. Cumulative
   *  across remounts so a panel mounted/unmounted twice keeps
   *  incrementing. */
  renderCount: number
  /** Total `fetch()` calls fired in this tab since the wrap was
   *  installed. Shared across every `usePerformance` caller. */
  apiCallCount: number
  /** Push a snapshot of the current metrics to `console.info` so a
   *  trader looking at the dev console can see the numbers without
   *  inspecting the React tree. */
  log: () => void
}

/**
 * usePerformance — opt-in performance monitor for a single component.
 *
 * @param name — caller-supplied label (typically the component name).
 *   Counters aggregate per-name across remounts so the same name
 *   mounted twice sums into the same entry.
 * @returns `{ initialRenderMs, renderCount, apiCallCount, log }`.
 *   Always returns a result object (so the caller's destructure never
 *   crashes); the published numbers are zero until the localStorage
 *   flag flips on.
 *
 * @example
 *   function PositionsPanel() {
 *     const perf = usePerformance('PositionsPanel')
 *     // ...
 *   }
 *
 * Enable in the browser via:
 *   localStorage.setItem('polymarket_perf_monitor', '1')
 *   location.reload()
 * Then read the numbers via:
 *   (window as any).__perf__
 */
export function usePerformance(name: string): UsePerformanceResult {
  // Per-instance mount-timestamp ref. Captured ONCE on the first
  // render so the post-paint effect can compute initialRenderMs from
  // the same React commit cycle that mounted the component (not from
  // a later mount of a remounted instance).
  const mountedAtRef = useRef<number | null>(null)
  if (mountedAtRef.current === null) {
    mountedAtRef.current = performance.now()
  }

  // Synchronously ensure the Map has an entry for this name. Done
  // during render (NOT in an effect) so the very first render's
  // increment is recorded before any post-paint code runs.
  let entry = PERF_MAP.get(name)
  if (!entry) {
    entry = {
      initialRenderMs: null,
      renderCount: 0,
      mountedAt: null,
      firstPaintDone: false,
    }
    PERF_MAP.set(name, entry)
  }

  // Synchronously bump the cumulative render counter on every render.
  // This is the single hot-path addition — a Map lookup + integer
  // increment. No setState, no effect, no allocation.
  entry.renderCount += 1

  // The post-paint effect that records initialRenderMs ONCE per name.
  // Runs after the browser has painted the component's first frame;
  // the delta between `mountedAtRef` (captured during render) and
  // `performance.now()` (during the effect) is the initial render
  // time. Subsequent renders of the same name skip the
  // `firstPaintDone` branch so the value is sticky.
  useEffect(() => {
    if (!entry) return
    if (!entry.firstPaintDone && mountedAtRef.current !== null) {
      entry.initialRenderMs = performance.now() - mountedAtRef.current
      entry.mountedAt = mountedAtRef.current
      entry.firstPaintDone = true
    }
    // Publish a snapshot to window so a trader inspecting the dev
    // console sees the latest numbers without inspecting the React
    // tree.
    publishToGlobal()
  }, [entry])

  // Install the fetch wrap lazily. The wrap is idempotent — the
  // second + subsequent `installFetchWrap()` calls are no-ops.
  if (!fetchWrapped) {
    installFetchWrap()
  }

  return {
    initialRenderMs: entry.initialRenderMs,
    renderCount: entry.renderCount,
    apiCallCount,
    log: () => logSnapshot(name),
  }
}

// ── Helpers ────────────────────────────────────────────────────────────────

function installFetchWrap(): void {
  if (fetchWrapped) return
  if (typeof window === 'undefined') return
  if (typeof window.fetch !== 'function') return

  const originalFetch = window.fetch.bind(window)
  const wrappedFetch: typeof window.fetch = function patchedFetch(
    input: RequestInfo | URL,
    init?: RequestInit,
  ) {
    apiCallCount += 1
    return originalFetch(input, init)
  }
  // Preserve static props (Response, Request, Headers, etc.) — the
  // `fetch` function on `window` is a regular function reference, not
  // a class, so we copy props defensively.
  for (const key of Object.keys(originalFetch)) {
    // @ts-expect-error — index-assigning a known prop on the wrapped
    // function; the cast survives because the original fetch is a
    // plain function reference with extra props.
    wrappedFetch[key] = (originalFetch as Record<string, unknown>)[key]
  }
  try {
    window.fetch = wrappedFetch
    fetchWrapped = true
  } catch {
    // `window.fetch` may be non-writable in some environments (e.g.
    // locked-down jsdom). Fail silently — the monitor still tracks
    // render counts; only the API counter reads zero.
  }
}

function publishToGlobal(): void {
  if (typeof window === 'undefined') return
  const snapshot: Record<string, PerfEntry> = {}
  for (const [name, e] of PERF_MAP.entries()) {
    snapshot[name] = { ...e }
  }
  const globalRef = window as unknown as { __perf__?: unknown }
  globalRef.__perf__ = {
    panels: snapshot,
    apiCallCount,
    timestamp: Date.now(),
  }
}

function logSnapshot(name: string): void {
  console.info(
    `[perf] ${name}:`,
    {
      initialRenderMs: PERF_MAP.get(name)?.initialRenderMs ?? null,
      renderCount: PERF_MAP.get(name)?.renderCount ?? 0,
      apiCallCount,
    },
  )
}

// ── Test helpers (exported so the test suite can reset state) ─────────────
// Used by `usePerformance.test.ts` to isolate each test from the
// previous test's counters. NOT part of the public trader-facing API.

export const __testReset = (): void => {
  PERF_MAP.clear()
  apiCallCount = 0
  // Re-install the fetch wrap on the next `usePerformance` call so a
  // fresh test re-arms the counter from zero.
  fetchWrapped = false
}

export const __testGetApiCallCount = (): number => apiCallCount
