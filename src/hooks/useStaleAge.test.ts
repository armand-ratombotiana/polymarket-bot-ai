// hooks/useStaleAge.test.ts — W41-3 staleness tracker.
//
// Verifies:
//   1. Returns null when lastUpdated is null (no data yet).
//   2. Returns 0 (or near-zero) immediately after a successful fetch.
//   3. Returns an increasing age as time passes (uses fake timers).
//   4. Clears its interval on unmount (no leaked setState warnings).

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useStaleAge } from './useStaleAge'

describe('useStaleAge', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('returns null when lastUpdated is null', () => {
    const { result } = renderHook(() => useStaleAge(null))
    expect(result.current).toBeNull()
  })

  it('returns 0 immediately when lastUpdated is the current time', () => {
    const now = Date.now()
    const { result } = renderHook(() => useStaleAge(now))
    expect(result.current).not.toBeNull()
    expect(result.current!).toBeLessThan(1)
  })

  it('returns the correct age after the tick interval elapses', () => {
    const start = Date.now()
    const { result } = renderHook(() => useStaleAge(start))
    expect(result.current!).toBeLessThan(1)

    // Advance fake timers past one tick (5s).
    act(() => {
      vi.advanceTimersByTime(5000)
    })
    expect(result.current!).toBeGreaterThanOrEqual(5)
    expect(result.current!).toBeLessThan(6)

    // Advance another 30s — age should grow.
    act(() => {
      vi.advanceTimersByTime(30000)
    })
    expect(result.current!).toBeGreaterThanOrEqual(35)
  })

  it('does not leak a setInterval after unmount', () => {
    const start = Date.now()
    const { unmount } = renderHook(() => useStaleAge(start))
    // Spy on clearInterval to ensure it's called.
    const clearSpy = vi.spyOn(global, 'clearInterval')
    unmount()
    expect(clearSpy).toHaveBeenCalled()
    clearSpy.mockRestore()
  })

  it('re-syncs `now` when lastUpdated changes (e.g. after a refetch)', () => {
    const initial = Date.now()
    const { result, rerender } = renderHook(
      ({ lu }) => useStaleAge(lu),
      { initialProps: { lu: initial } },
    )
    // Wait one tick.
    act(() => {
      vi.advanceTimersByTime(10000)
    })
    expect(result.current!).toBeGreaterThanOrEqual(10)

    // Simulate a fresh fetch — lastUpdated jumps forward 1s.
    const fresh = Date.now() + 1000
    rerender({ lu: fresh })
    expect(result.current!).toBeLessThan(1)
  })
})
