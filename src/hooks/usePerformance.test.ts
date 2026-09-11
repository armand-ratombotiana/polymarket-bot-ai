// hooks/usePerformance.test.ts — W41-2 frontend performance monitor.
//
// Verifies the three contract guarantees:
//   1. `initialRenderMs` is recorded once (first paint only).
//   2. `renderCount` increments on every render AND persists across
//      unmount/remount cycles (cumulative, not per-instance).
//   3. `apiCallCount` increments on every `fetch()` call.
//
// Plus the disable-by-default behaviour: when the localStorage flag
// is absent, the hook still returns a result object (so the caller's
// destructure doesn't crash) but the published numbers stay at their
// last-known value (zero until first opt-in render).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { usePerformance, __testReset, __testGetApiCallCount } from './usePerformance'

describe('usePerformance', () => {
  beforeEach(() => {
    // Each test starts from a clean Map + counter.
    __testReset()
    // Restore the real fetch so the wrap install path runs cleanly.
    if (typeof global.fetch !== 'function') {
      global.fetch = vi.fn().mockResolvedValue(new Response('{}'))
    }
    localStorage.clear()
  })

  afterEach(() => {
    __testReset()
    vi.restoreAllMocks()
  })

  it('returns a result object even when the monitor is disabled', () => {
    // No localStorage flag set — monitor disabled.
    const { result } = renderHook(() => usePerformance('TestPanel'))
    expect(result.current).toEqual({
      initialRenderMs: expect.any(Object), // null before paint measurement
      renderCount: expect.any(Number),
      apiCallCount: expect.any(Number),
      log: expect.any(Function),
    })
    // renderCount starts at 1 (the first render).
    expect(result.current.renderCount).toBe(1)
    // log is callable even when disabled.
    expect(() => result.current.log()).not.toThrow()
  })

  it('increments renderCount on every render', () => {
    const { result, rerender } = renderHook(() => usePerformance('Counter'))
    expect(result.current.renderCount).toBe(1)
    rerender()
    expect(result.current.renderCount).toBe(2)
    rerender()
    rerender()
    expect(result.current.renderCount).toBe(4)
  })

  it('publishes the initialRenderMs only once (first paint)', async () => {
    localStorage.setItem('polymarket_perf_monitor', '1')
    const { result, rerender } = renderHook(() => usePerformance('InitialRender'))

    // After the first effect runs (post-paint), the entry should be
    // populated. We use `act` + a microtask flush so the effect runs.
    await act(async () => {
      await Promise.resolve()
    })

    // Trigger a re-render so the latest Map values propagate to the
    // returned object (the hook reads the Map synchronously on every
    // render, but the previous render's return value was captured
    // before the effect set initialRenderMs).
    rerender()

    // The first-paint measurement should be a non-negative number.
    // (We don't assert a specific bound — CI runners are flaky for
    // sub-ms timing.)
    expect(result.current.initialRenderMs).not.toBeNull()
    expect(result.current.initialRenderMs!).toBeGreaterThanOrEqual(0)

    const firstPaintValue = result.current.initialRenderMs

    // Re-render — the firstPaintDone flag is now true, so the value
    // should NOT change.
    rerender()
    await act(async () => {
      await Promise.resolve()
    })
    expect(result.current.initialRenderMs).toBe(firstPaintValue)
  })

  it('counts fetch() calls when the wrap is installed', async () => {
    localStorage.setItem('polymarket_perf_monitor', '1')
    // Spy on fetch — installs the wrap on first usePerformance call.
    const fetchSpy = vi.fn().mockResolvedValue(new Response('{"ok":1}'))
    global.fetch = fetchSpy as unknown as typeof global.fetch

    const { result, rerender } = renderHook(() => usePerformance('ApiPanel'))
    const before = __testGetApiCallCount()

    // Fire two fetches.
    await act(async () => {
      await fetch('/api/test1')
      await fetch('/api/test2')
    })

    const after = __testGetApiCallCount()
    expect(after - before).toBe(2)

    // Re-render so the hook's returned apiCallCount reflects the
    // latest module-level counter (the previous render's return
    // value was captured before the fetches fired).
    rerender()
    expect(result.current.apiCallCount).toBeGreaterThanOrEqual(2)
  })

  it('aggregates counters per-name across remounts', () => {
    localStorage.setItem('polymarket_perf_monitor', '1')
    const { unmount, rerender } = renderHook(() => usePerformance('StickyPanel'))
    // Mount → render 1
    expect(unmount).toBeTypeOf('function')
    // Re-render → render 2 + 3
    rerender()
    rerender()
    // Now unmount + remount — renderCount should keep climbing
    // (cumulative, not reset). The Map entry persists across the
    // remount because the entry is keyed by name, not by React
    // instance.
    unmount()
    const { result } = renderHook(() => usePerformance('StickyPanel'))
    expect(result.current.renderCount).toBeGreaterThan(2)
  })

  it('log() does not throw when no entry exists for the name', () => {
    const { result } = renderHook(() => usePerformance('NoEntryYet'))
    // The internal Map may not have an entry (monitor disabled);
    // log() should degrade gracefully.
    expect(() => result.current.log()).not.toThrow()
  })

  it('publishes a __perf__ global on the window when enabled', async () => {
    localStorage.setItem('polymarket_perf_monitor', '1')
    renderHook(() => usePerformance('GlobalPublish'))
    await act(async () => {
      await Promise.resolve()
    })
    const w = window as unknown as { __perf__?: { panels: Record<string, unknown>; apiCallCount: number } }
    expect(w.__perf__).toBeDefined()
    expect(w.__perf__!.panels).toHaveProperty('GlobalPublish')
    expect(typeof w.__perf__!.apiCallCount).toBe('number')
  })
})
