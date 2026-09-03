// hooks/useWebSocket.test.ts — Unit tests for the generic WebSocket hook.
//
// W11-4 — Verifies the four core contracts of useWebSocket:
//   1. Connects on mount.
//   2. Parses incoming JSON and dispatches to onMessage.
//   3. Reconnects with capped exponential backoff (well, fixed delay).
//   4. Cleans up on unmount / explicit disconnect.
//   5. Pauses on tab-hidden, resumes on tab-visible.
//
// Strategy: install a MockWebSocket class on `global.WebSocket` so the
// hook's `new WebSocket(getAuthedWsUrl())` constructs a mock we control.
// Tests then call `.triggerOpen()` / `.triggerMessage()` /
// `.triggerClose()` on the latest mock instance to drive the hook's
// state transitions.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useWebSocket } from './useWebSocket'

// Minimal MockWebSocket — implements just enough of the WebSocket
// surface (constructor + the four event-handler slots + close + send)
// for the hook to believe it's talking to a real socket. Each instance
// is pushed onto `MockWebSocket.instances` so tests can assert against
// the call count (e.g. "reconnect created a 2nd instance").
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

  // Test-only helpers — the production WebSocket emits these via the
  // browser; we trigger them imperatively from the test thread.
  triggerOpen() {
    this.readyState = MockWebSocket.OPEN
    this.onopen?.()
  }

  triggerMessage(data: unknown) {
    const payload = typeof data === 'string' ? data : JSON.stringify(data)
    this.onmessage?.({ data: payload })
  }

  triggerClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  triggerError(err?: unknown) {
    this.onerror?.(err)
  }

  // Production surface — close() is called by the hook's cleanup path.
  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  send() {
    // No-op for tests that don't assert on send payload.
  }
}

describe('useWebSocket', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
    vi.clearAllTimers()
    vi.useRealTimers()
  })

  it('attempts to connect on mount', () => {
    renderHook(() => useWebSocket())
    expect(MockWebSocket.instances).toHaveLength(1)
    expect(MockWebSocket.instances[0].url).toContain('/ws')
    expect(MockWebSocket.instances[0].url).toContain('XTransformPort')
  })

  it('reports isConnected=false initially and flips to true on open', () => {
    const { result } = renderHook(() => useWebSocket())
    expect(result.current.isConnected).toBe(false)
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isConnected).toBe(true)
  })

  it('invokes onConnect exactly once when the socket opens', () => {
    const onConnect = vi.fn()
    renderHook(() => useWebSocket({ onConnect }))
    expect(onConnect).not.toHaveBeenCalled()
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(onConnect).toHaveBeenCalledTimes(1)
  })

  it('parses JSON messages and dispatches the parsed payload to onMessage', () => {
    const onMessage = vi.fn()
    const { result } = renderHook(() => useWebSocket({ onMessage }))
    act(() => MockWebSocket.instances[0].triggerOpen())
    act(() =>
      MockWebSocket.instances[0].triggerMessage({
        channel: 'positions',
        data: [{ token_id: 'abc', size: 10 }],
      }),
    )
    expect(onMessage).toHaveBeenCalledWith({
      channel: 'positions',
      data: [{ token_id: 'abc', size: 10 }],
    })
    expect(result.current.lastMessage).toEqual({
      channel: 'positions',
      data: [{ token_id: 'abc', size: 10 }],
    })
  })

  it('accepts pre-stringified JSON payloads without double-wrapping', () => {
    const onMessage = vi.fn()
    renderHook(() => useWebSocket({ onMessage }))
    act(() => MockWebSocket.instances[0].triggerMessage('{"channel":"orders"}'))
    expect(onMessage).toHaveBeenCalledWith({ channel: 'orders' })
  })

  it('does NOT invoke onMessage when the payload is not valid JSON', () => {
    const onMessage = vi.fn()
    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    renderHook(() => useWebSocket({ onMessage }))
    act(() => MockWebSocket.instances[0].triggerMessage('not-json'))
    expect(onMessage).not.toHaveBeenCalled()
    expect(errorSpy).toHaveBeenCalled()
    errorSpy.mockRestore()
  })

  it('sets isConnected=false and invokes onDisconnect when the socket closes', () => {
    const onDisconnect = vi.fn()
    const { result } = renderHook(() => useWebSocket({ onDisconnect }))
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isConnected).toBe(true)
    act(() => MockWebSocket.instances[0].triggerClose())
    expect(result.current.isConnected).toBe(false)
    expect(onDisconnect).toHaveBeenCalledTimes(1)
  })

  it('reconnects after the configured reconnectInterval when the socket closes', () => {
    vi.useFakeTimers()
    renderHook(() =>
      useWebSocket({ reconnectInterval: 1000, maxReconnectAttempts: 5 }),
    )
    expect(MockWebSocket.instances).toHaveLength(1)
    act(() => MockWebSocket.instances[0].triggerClose())
    // No reconnect yet — interval hasn't elapsed.
    expect(MockWebSocket.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(999))
    expect(MockWebSocket.instances).toHaveLength(1)
    act(() => vi.advanceTimersByTime(1))
    expect(MockWebSocket.instances).toHaveLength(2)
  })

  it('stops reconnecting after maxReconnectAttempts is reached', () => {
    vi.useFakeTimers()
    renderHook(() =>
      useWebSocket({ reconnectInterval: 100, maxReconnectAttempts: 3 }),
    )
    // Each close + timer advance = one reconnect attempt.
    act(() => MockWebSocket.instances[0].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(2)

    act(() => MockWebSocket.instances[1].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(3)

    act(() => MockWebSocket.instances[2].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(4)

    // 4th close — counter is at 3 (== max), no more reconnects.
    act(() => MockWebSocket.instances[3].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(4)
  })

  it('resets the reconnect attempt counter after a successful open', () => {
    vi.useFakeTimers()
    renderHook(() =>
      useWebSocket({ reconnectInterval: 100, maxReconnectAttempts: 2 }),
    )
    // Burn through one reconnect.
    act(() => MockWebSocket.instances[0].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(2)
    // Successful open resets the counter to 0.
    act(() => MockWebSocket.instances[1].triggerOpen())
    // We should now be able to reconnect 2 more times before giving up.
    act(() => MockWebSocket.instances[1].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(3)
    act(() => MockWebSocket.instances[2].triggerClose())
    act(() => vi.advanceTimersByTime(100))
    expect(MockWebSocket.instances).toHaveLength(4)
  })

  it('does NOT reconnect after explicit disconnect()', () => {
    vi.useFakeTimers()
    const { result } = renderHook(() =>
      useWebSocket({ reconnectInterval: 100, maxReconnectAttempts: 5 }),
    )
    act(() => MockWebSocket.instances[0].triggerOpen())
    act(() => result.current.disconnect())
    act(() => vi.advanceTimersByTime(10_000))
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it('does NOT reconnect after unmount', () => {
    vi.useFakeTimers()
    const { unmount } = renderHook(() =>
      useWebSocket({ reconnectInterval: 100, maxReconnectAttempts: 5 }),
    )
    act(() => MockWebSocket.instances[0].triggerOpen())
    act(() => unmount())
    act(() => vi.advanceTimersByTime(10_000))
    expect(MockWebSocket.instances).toHaveLength(1)
  })

  it('send() is a no-op when the socket is not OPEN', () => {
    const { result } = renderHook(() => useWebSocket())
    const sendSpy = vi.spyOn(MockWebSocket.instances[0], 'send')
    // Socket is still in CONNECTING state.
    act(() => result.current.send({ foo: 'bar' }))
    expect(sendSpy).not.toHaveBeenCalled()
  })

  it('send() forwards a JSON-serialised payload when the socket is OPEN', () => {
    const { result } = renderHook(() => useWebSocket())
    const sendSpy = vi.spyOn(MockWebSocket.instances[0], 'send')
    act(() => MockWebSocket.instances[0].triggerOpen())
    act(() => result.current.send({ foo: 'bar' }))
    expect(sendSpy).toHaveBeenCalledTimes(1)
    expect(sendSpy).toHaveBeenCalledWith(JSON.stringify({ foo: 'bar' }))
  })

  it('closes the socket and fires onDisconnect when the tab becomes hidden', () => {
    vi.useFakeTimers()
    const onDisconnect = vi.fn()
    renderHook(() =>
      useWebSocket({ onDisconnect, reconnectInterval: 100_000 }),
    )
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(onDisconnect).not.toHaveBeenCalled()

    Object.defineProperty(document, 'hidden', {
      value: true,
      configurable: true,
      writable: true,
    })
    act(() => document.dispatchEvent(new Event('visibilitychange')))

    expect(onDisconnect).toHaveBeenCalled()
  })

  it('creates a new WebSocket instance when the tab becomes visible again', () => {
    vi.useFakeTimers()
    renderHook(() => useWebSocket({ reconnectInterval: 100_000 }))
    act(() => MockWebSocket.instances[0].triggerOpen())
    const initialCount = MockWebSocket.instances.length

    // Hide → show cycle.
    Object.defineProperty(document, 'hidden', {
      value: true,
      configurable: true,
      writable: true,
    })
    act(() => document.dispatchEvent(new Event('visibilitychange')))
    Object.defineProperty(document, 'hidden', {
      value: false,
      configurable: true,
      writable: true,
    })
    act(() => document.dispatchEvent(new Event('visibilitychange')))

    expect(MockWebSocket.instances.length).toBeGreaterThan(initialCount)
  })
})
