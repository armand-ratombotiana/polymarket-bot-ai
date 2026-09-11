// hooks/useRealtimeData.test.ts — Tests for the hybrid REST + WS data hook.
//
// W11-4 — Verifies the four contracts of useRealtimeData:
//   1. On mount, fires a REST GET against `endpoint` and populates state.
//   2. WS messages whose `channel` matches `wsChannel` override the
//      REST-derived state.
//   3. When the WS is NOT connected, polls `endpoint` every
//      `pollInterval` ms.
//   4. Polling is suppressed while the tab is hidden.
//
// Strategy: install a MockWebSocket on `global.WebSocket` (same as
// useWebSocket.test.ts) and a `vi.fn()` fetch on `global.fetch`. Tests
// drive the WS via the mock's `triggerOpen()` / `triggerMessage()`
// helpers and the REST path via `vi.mocked(fetch).mockResolvedValue(...)`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useRealtimeData } from './useRealtimeData'

class MockWebSocket {
  static instances: MockWebSocket[] = []
  static CONNECTING = 0
  static OPEN = 1
  static CLOSING = 2
  static CLOSED = 3

  url: string
  readyState: number
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  onerror: ((event: unknown) => void) | null = null

  constructor(url: string) {
    this.url = url
    this.readyState = MockWebSocket.CONNECTING
    MockWebSocket.instances.push(this)
  }

  triggerOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.()
  }

  triggerMessage(data: unknown) {
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    this.onmessage?.({ data: payload })
  }

  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  send() {}
}

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('useRealtimeData', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
    global.fetch = vi.fn() as unknown as typeof fetch
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
    vi.clearAllTimers()
    vi.useRealTimers()
    // Restore document.hidden to its default false state.
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
      writable: true,
    })
  })

  it('sets isLoading=true on mount and false after the initial REST fetch resolves', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ positions: [] }))
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    expect(result.current.isLoading).toBe(true)
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
  })

  it('populates data with the REST response body on the initial fetch', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({ positions: [{ token_id: 'abc', size: 10 }] }),
    )
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    await waitFor(() => {
      expect(result.current.data).toEqual({
        positions: [{ token_id: 'abc', size: 10 }],
      })
    })
  })

  it('sets error when the initial REST fetch returns a non-200 response', async () => {
    vi.mocked(fetch).mockResolvedValue(
      new Response('boom', { status: 500 }),
    )
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    await waitFor(() => {
      expect(result.current.error).toContain('500')
    })
  })

  it('sets error when the initial REST fetch throws', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('network down'))
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    await waitFor(() => {
      expect(result.current.error).toContain('network down')
    })
    expect(result.current.isLoading).toBe(false)
  })

  it('overrides REST-derived state when a matching WS message arrives', async () => {
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({ positions: [{ token_id: 'abc', size: 10 }] }),
    )
    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
      }),
    )
    await waitFor(() => {
      expect(result.current.data?.positions).toHaveLength(1)
    })

    // Open the WebSocket so the hook marks itself as realtime.
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isRealtime).toBe(true)

    // Push an updated payload via the WS channel.
    act(() =>
      MockWebSocket.instances[0].triggerMessage({
        channel: 'positions',
        data: { positions: [{ token_id: 'abc', size: 20 }, { token_id: 'def', size: 5 }] },
      }),
    )

    expect(result.current.data).toEqual({
      positions: [{ token_id: 'abc', size: 20 }, { token_id: 'def', size: 5 }],
    })
  })

  it('ignores WS messages whose channel does not match wsChannel', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ positions: [] }))
    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
      }),
    )
    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([])
    })

    act(() => MockWebSocket.instances[0].triggerOpen())
    act(() =>
      MockWebSocket.instances[0].triggerMessage({
        channel: 'orders',
        data: { orders: [{ id: 'x' }] },
      }),
    )

    // Data unchanged — the orders channel is not the one we subscribed to.
    expect(result.current.data).toEqual({ positions: [] })
  })

  it('falls back to polling when the WS is not connected', async () => {
    // Use real timers + a short pollInterval so the test can `waitFor`
    // the polled state update. (Fake timers break waitFor's internal
    // setInterval, so we avoid them for hook tests that mix async fetch
    // with polling.)
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'a' }] }))
      .mockResolvedValue(
        jsonOk({ positions: [{ token_id: 'a' }, { token_id: 'b' }] }),
      )

    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
        pollInterval: 100,
      }),
    )

    // Initial REST fetch resolves.
    await waitFor(() => {
      expect(result.current.data?.positions).toHaveLength(1)
    })

    // WS is NOT open — isRealtime=false.
    expect(result.current.isRealtime).toBe(false)

    // The polling fallback fires every 100ms — wait for the second
    // payload (with two positions) to arrive.
    await waitFor(() => {
      expect(result.current.data?.positions).toHaveLength(2)
    })
  })

  it('stops polling once the WS connects', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'a' }] }))
      .mockResolvedValue(jsonOk({ positions: [{ token_id: 'polled' }] }))

    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
        pollInterval: 100,
      }),
    )

    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([{ token_id: 'a' }])
    })

    // Open the WS — polling effect should tear down its interval.
    act(() => MockWebSocket.instances[0].triggerOpen())
    await waitFor(() => {
      expect(result.current.isRealtime).toBe(true)
    })

    const fetchCountAfterOpen = vi.mocked(fetch).mock.calls.length

    // Wait well past one poll interval — no new fetch should fire.
    await new Promise((r) => setTimeout(r, 350))

    expect(vi.mocked(fetch).mock.calls.length).toBe(fetchCountAfterOpen)
  })

  it('skips poll ticks while the tab is hidden', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'a' }] }))
      .mockResolvedValue(jsonOk({ positions: [{ token_id: 'polled' }] }))

    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
        pollInterval: 100,
      }),
    )

    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([{ token_id: 'a' }])
    })

    // Hide the tab BEFORE any poll tick fires — the interval callback
    // guards on document.hidden and skips.
    Object.defineProperty(document, 'hidden', {
      value: true,
      configurable: true,
      writable: true,
    })

    const fetchCountBeforeHide = vi.mocked(fetch).mock.calls.length

    // Wait long enough for at least three poll ticks to have fired
    // (3 × 100ms = 300ms) if the interval were active and not skipping.
    await new Promise((r) => setTimeout(r, 350))

    // No new fetch should have fired while hidden.
    expect(vi.mocked(fetch).mock.calls.length).toBe(fetchCountBeforeHide)
    // And the data should still be the initial REST payload.
    expect(result.current.data?.positions).toEqual([{ token_id: 'a' }])

    // Restore visibility — polling resumes on the next tick.
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
      writable: true,
    })

    await waitFor(() => {
      expect(vi.mocked(fetch).mock.calls.length).toBeGreaterThan(
        fetchCountBeforeHide,
      )
    })
  })

  it('uses initialData as the starting state before the first fetch resolves', () => {
    vi.mocked(fetch).mockImplementation(
      () => new Promise<Response>(() => {}), // never resolves
    )
    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
        initialData: { positions: [] },
      }),
    )
    // State should be seeded with the initialData immediately.
    expect(result.current.data).toEqual({ positions: [] })
    expect(result.current.isLoading).toBe(true) // still loading until REST resolves
  })

  it('reports isRealtime=false before the WS connects and true after', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ positions: [] }))
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    await waitFor(() => {
      expect(result.current.isLoading).toBe(false)
    })
    expect(result.current.isRealtime).toBe(false)
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isRealtime).toBe(true)
  })

  // ─────────────────────────────────────────────────────────────────────
  // W41-3 — `lastUpdated` + `refetch` additions (additive — non-breaking).
  // ─────────────────────────────────────────────────────────────────────

  it('exposes lastUpdated=null before the first successful fetch resolves', () => {
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    expect(result.current.lastUpdated).toBeNull()
  })

  it('stamps lastUpdated with the current epoch ms after a successful REST fetch', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ positions: [] }))
    const before = Date.now()
    const { result } = renderHook(() =>
      useRealtimeData('/api/positions', { wsChannel: 'positions' }),
    )
    await waitFor(() => {
      expect(result.current.lastUpdated).not.toBeNull()
    })
    expect(result.current.lastUpdated!).toBeGreaterThanOrEqual(before)
  })

  it('exposes lastUpdated as a function of WS pushes (updates on each message)', async () => {
    vi.mocked(fetch).mockResolvedValue(jsonOk({ positions: [] }))
    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
      }),
    )
    await waitFor(() => {
      expect(result.current.lastUpdated).not.toBeNull()
    })
    const restStamp = result.current.lastUpdated

    act(() => MockWebSocket.instances[0].triggerOpen())
    // Yield a tick so the restStamp value is in the past relative to the
    // WS push below.
    await new Promise((r) => setTimeout(r, 5))
    act(() =>
      MockWebSocket.instances[0].triggerMessage({
        channel: 'positions',
        data: { positions: [{ token_id: 'ws' }] },
      }),
    )
    expect(result.current.lastUpdated).not.toBeNull()
    expect(result.current.lastUpdated!).toBeGreaterThan(restStamp!)
  })

  it('exposes a refetch() function that re-runs the initial REST fetch', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'first' }] }))
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'second' }] }))

    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
      }),
    )

    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([{ token_id: 'first' }])
    })

    act(() => result.current.refetch())

    // refetch flips isLoading back to true immediately.
    expect(result.current.isLoading).toBe(true)
    // Then the second mocked response resolves and replaces the data.
    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([{ token_id: 'second' }])
    })
    expect(result.current.isLoading).toBe(false)
  })

  it('refetch() clears a previously-set error so the panel can show its loading state', async () => {
    vi.mocked(fetch)
      .mockResolvedValueOnce(new Response('boom', { status: 500 }))
      .mockResolvedValueOnce(jsonOk({ positions: [{ token_id: 'ok' }] }))

    const { result } = renderHook(() =>
      useRealtimeData<{ positions: unknown[] }>('/api/positions', {
        wsChannel: 'positions',
      }),
    )

    await waitFor(() => {
      expect(result.current.error).toContain('500')
    })
    expect(result.current.error).not.toBeNull()

    act(() => result.current.refetch())

    // refetch clears the error immediately.
    expect(result.current.error).toBeNull()
    expect(result.current.isLoading).toBe(true)

    // Then the second mocked response resolves successfully.
    await waitFor(() => {
      expect(result.current.data?.positions).toEqual([{ token_id: 'ok' }])
    })
    expect(result.current.error).toBeNull()
  })
})
