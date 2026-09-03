// hooks/useFeatureFlags.test.ts — Tests for the W12-1 feature-flag hook.
//
// Verifies the four contracts of useFeatureFlags:
//   1. On mount, fires a REST GET against `/api/flags` and populates state.
//   2. `isEnabled(key)` returns true/false based on the cached flags.
//   3. Polls `/api/flags` every `pollIntervalMs` ms (default 60 s).
//   4. Visibility-aware: pauses polling when the tab is hidden, resumes
//      (with an immediate re-fetch) when the tab becomes visible.
//
// Strategy: mock `global.fetch` via `vi.fn()` and use
// `@testing-library/react`'s `renderHook` + `waitFor` / `act` to drive
// the hook through its lifecycle. Real timers (NOT fake) — `waitFor`
// uses `setInterval` internally which fake timers pause, causing every
// polling-aware test to hang (see useRealtimeData.test.ts for the
// same caveat). Polling tests pass `pollIntervalMs: 100` and await a
// real `setTimeout` so ≥3 ticks fire without burning a real 60 s.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useFeatureFlags } from './useFeatureFlags'

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useFeatureFlags', () => {
  beforeEach(() => {
    global.fetch = vi.fn() as unknown as typeof fetch
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
      writable: true,
    })
  })

  afterEach(() => {
    vi.restoreAllMocks()
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
      writable: true,
    })
  })

  it('starts in loading state and clears it after the initial fetch resolves', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ flags: [], count: 0 }))
    const { result } = renderHook(() => useFeatureFlags())
    expect(result.current.isLoading).toBe(true)
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
  })

  it('populates flags from the REST response body', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({
        flags: [
          {
            key: 'live_trading',
            enabled: true,
            description: 'live',
            config: {},
            updated_at: 0,
          },
          {
            key: 'shadow_trading',
            enabled: false,
            description: 'shadow',
            config: {},
            updated_at: 0,
          },
        ],
        count: 2,
      }),
    )
    const { result } = renderHook(() => useFeatureFlags())
    await waitFor(() => {
      expect(result.current.flags).toHaveLength(2)
    })
    expect(result.current.flags[0].key).toBe('live_trading')
    expect(result.current.flags[1].key).toBe('shadow_trading')
  })

  it('isEnabled returns false while loading and reflects cached state after fetch', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({
        flags: [
          {
            key: 'live_trading',
            enabled: true,
            description: 'live',
            config: {},
            updated_at: 0,
          },
          {
            key: 'shadow_trading',
            enabled: false,
            description: 'shadow',
            config: {},
            updated_at: 0,
          },
        ],
        count: 2,
      }),
    )
    const { result } = renderHook(() => useFeatureFlags())
    // While loading: every key is false (fail-safe).
    expect(result.current.isEnabled('live_trading')).toBe(false)
    expect(result.current.isEnabled('shadow_trading')).toBe(false)
    expect(result.current.isEnabled('unknown_key')).toBe(false)

    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })

    // After fetch: reflects cached values.
    expect(result.current.isEnabled('live_trading')).toBe(true)
    expect(result.current.isEnabled('shadow_trading')).toBe(false)
    // Unknown key still false.
    expect(result.current.isEnabled('unknown_key')).toBe(false)
  })

  it('polls every pollIntervalMs', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ flags: [], count: 0 }))
    renderHook(() => useFeatureFlags({ pollIntervalMs: 100 }))

    // Wait for the initial fetch to complete.
    await waitFor(() => {
      expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
    })

    // Wait long enough for at least 3 polling ticks (initial + 3 polls = 4).
    await new Promise((r) => setTimeout(r, 350))
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThanOrEqual(4)
  })

  it('pauses polling when the tab is hidden and resumes on visible', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ flags: [], count: 0 }))
    renderHook(() => useFeatureFlags({ pollIntervalMs: 100 }))
    await waitFor(() => {
      expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
    })

    // Hide the tab.
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })

    // Wait 400 ms while hidden — no additional polls should fire.
    await new Promise((r) => setTimeout(r, 400))
    const hiddenCount = vi.mocked(fetch).mock.calls.length
    // Only the initial fetch; no polling while hidden.
    expect(hiddenCount).toBe(1)

    // Make the tab visible again — immediate re-fetch + resumed poll.
    Object.defineProperty(document, 'hidden', { value: false, configurable: true })
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'))
    })
    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThanOrEqual(2)
    })

    // Wait a bit more — polling continues.
    await new Promise((r) => setTimeout(r, 250))
    expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThanOrEqual(3)
  })

  it('refresh() triggers an immediate re-fetch', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ flags: [], count: 0 }))
    const { result } = renderHook(() => useFeatureFlags({ pollIntervalMs: 10_000 }))
    await waitFor(() => {
      expect(vi.mocked(fetch)).toHaveBeenCalledTimes(1)
    })

    await act(async () => {
      await result.current.refresh()
    })
    expect(vi.mocked(fetch)).toHaveBeenCalledTimes(2)
  })

  it('does not crash when the fetch throws (keeps stale cache, fails-closed)', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(
      jsonOk({
        flags: [
          {
            key: 'live_trading',
            enabled: true,
            description: 'live',
            config: {},
            updated_at: 0,
          },
        ],
        count: 1,
      }),
    )
    vi.mocked(fetch).mockRejectedValueOnce(new Error('network down'))

    const { result } = renderHook(() => useFeatureFlags({ pollIntervalMs: 10_000 }))
    await waitFor(() => {
      expect(result.current.flags).toHaveLength(1)
    })
    // Initially fetched: live_trading=true.
    expect(result.current.isEnabled('live_trading')).toBe(true)

    // Trigger a refresh that throws — stale cache is preserved.
    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.isEnabled('live_trading')).toBe(true)
  })

  it('does not crash when the response is non-200', async () => {
    vi.mocked(fetch).mockResolvedValue(new Response('boom', { status: 500 }))
    const { result } = renderHook(() => useFeatureFlags({ pollIntervalMs: 10_000 }))
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
    // Non-200 keeps the empty initial state — every key fails-closed.
    expect(result.current.flags).toHaveLength(0)
    expect(result.current.isEnabled('live_trading')).toBe(false)
  })
})
