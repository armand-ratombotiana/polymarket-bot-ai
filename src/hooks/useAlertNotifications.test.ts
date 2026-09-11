// hooks/useAlertNotifications.test.ts — W23-4 tests for the real-time
// alert-notifications hook.
//
// Strategy:
//   * The hook composes `useWebSocket`, so we install the same
//     `MockWebSocket` shim used by `useWebSocket.test.ts` /
//     `ConnectionStatus.test.tsx`. Tests then drive the hook by calling
//     `MockWebSocket.instances[0].triggerMessage(payload)` to simulate
//     the server pushing a frame.
//   * The hook calls `showCriticalAlert` from `@/lib/notifications`,
//     which ultimately calls `new Notification(...)`. jsdom does NOT
//     implement the Notifications API, so we stub `global.Notification`
//     with a fake class that records constructor calls — the same
//     pattern as `useNotifications.test.ts`.
//   * `act()` is required around every `triggerMessage` / state mutation
//     because each one drives a React state update inside the hook.
//
// What's covered:
//   1. Initial state (empty alerts, zero unread, enabled=true, not connected).
//   2. WS messages on the `alerts` channel with `type: 'alert'` are
//      captured and added to the alerts list (most-recent first).
//   3. Unread count increments per alert.
//   4. Messages on other channels are ignored.
//   5. Messages on the `alerts` channel with a non-`alert` type are
//      ignored (e.g. `type: 'snapshot'`).
//   6. The alerts list is capped at 50 entries (FIFO eviction).
//   7. `acknowledge(id)` removes a single alert and decrements unread.
//   8. `acknowledge(id)` for an unknown id is a no-op.
//   9. `acknowledgeAll()` clears both lists and unread count.
//  10. `toggle()` flips `enabled` between true/false.
//  11. `isConnected` reflects the WS open/close state.
//  12. Browser notification fires when enabled AND permission is granted.
//  13. Browser notification does NOT fire when enabled=false.
//  14. Browser notification does NOT fire when permission is default.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { useAlertNotifications, type Alert } from './useAlertNotifications'

// --- MockWebSocket shim ----------------------------------------------------
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

  triggerClose() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  triggerError(err?: unknown) {
    this.onerror?.(err)
  }

  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  send() {}
}

// --- Fake Notification (jsdom doesn't implement the Notifications API) -----
let constructorCalls: Array<{ title: string; options: NotificationOptions | undefined }>

class FakeNotification {
  title: string
  options: NotificationOptions | undefined
  onclick: ((ev: Event) => void) | null = null
  close = vi.fn()
  static permission: NotificationPermission = 'default'
  static requestPermission = vi.fn(async () => 'default' as NotificationPermission)

  constructor(title: string, options?: NotificationOptions) {
    this.title = title
    this.options = options
    constructorCalls.push({ title, options })
  }
}

beforeEach(() => {
  constructorCalls = []
  FakeNotification.permission = 'default'
  FakeNotification.requestPermission = vi.fn(async () => 'default' as NotificationPermission)
  ;(globalThis as any).Notification = FakeNotification
  ;(window as any).Notification = FakeNotification

  MockWebSocket.instances = []
  ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
    MockWebSocket as unknown as typeof WebSocket
})

afterEach(() => {
  delete (globalThis as any).Notification
  delete (window as any).Notification
  vi.restoreAllMocks()
  vi.useRealTimers()
})

// --- Helpers ---------------------------------------------------------------
function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    alert_id: 'a-' + Math.random().toString(36).slice(2, 9),
    name: 'Test Alert',
    message: 'test message body',
    severity: 'info',
    timestamp: Date.now(),
    ...overrides,
  }
}

// Push a message through the WS as the server would. Wrapped in `act`
// because each call drives a React state update inside the hook.
function pushAlert(alert: Alert) {
  act(() => {
    MockWebSocket.instances[0].triggerMessage({
      channel: 'alerts',
      data: { type: 'alert', alert },
    })
  })
}

function pushNonAlertFrame() {
  act(() => {
    MockWebSocket.instances[0].triggerMessage({
      channel: 'positions',
      data: [{ token_id: 'abc', size: 10 }],
    })
  })
}

// --- Tests -----------------------------------------------------------------

describe('useAlertNotifications — initial state', () => {
  it('exposes empty alerts, zero unread, enabled=true, not connected on mount', () => {
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
    expect(result.current.enabled).toBe(true)
    // The MockWebSocket is constructed but `triggerOpen` hasn't fired,
    // so isConnected should be false.
    expect(result.current.isConnected).toBe(false)
  })
})

describe('useAlertNotifications — WebSocket message capture', () => {
  it('captures alerts channel messages with type=alert and adds them to the alerts list', () => {
    const { result } = renderHook(() => useAlertNotifications())
    const a1 = makeAlert({ alert_id: 'a1', name: 'Drawdown', severity: 'critical' })
    pushAlert(a1)
    expect(result.current.alerts).toHaveLength(1)
    expect(result.current.alerts[0]).toEqual(a1)
    expect(result.current.unreadCount).toBe(1)
  })

  it('prepends new alerts so the list stays most-recent-first', () => {
    const { result } = renderHook(() => useAlertNotifications())
    const a1 = makeAlert({ alert_id: 'a1', name: 'First', timestamp: 1000 })
    const a2 = makeAlert({ alert_id: 'a2', name: 'Second', timestamp: 2000 })
    pushAlert(a1)
    pushAlert(a2)
    expect(result.current.alerts).toHaveLength(2)
    // a2 was pushed last → it should be at the head.
    expect(result.current.alerts[0].alert_id).toBe('a2')
    expect(result.current.alerts[1].alert_id).toBe('a1')
    expect(result.current.unreadCount).toBe(2)
  })

  it('ignores messages on other channels (positions, orders, snapshot)', () => {
    const { result } = renderHook(() => useAlertNotifications())
    pushNonAlertFrame()
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
  })

  it('ignores alerts-channel messages whose data.type is NOT "alert"', () => {
    const { result } = renderHook(() => useAlertNotifications())
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        channel: 'alerts',
        data: { type: 'snapshot', alerts: [] },
      })
    })
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
  })

  it('ignores payloads missing the alert_id field', () => {
    const { result } = renderHook(() => useAlertNotifications())
    act(() => {
      MockWebSocket.instances[0].triggerMessage({
        channel: 'alerts',
        data: { type: 'alert', alert: { name: 'no id', message: 'm', severity: 'info', timestamp: 1 } },
      })
    })
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
  })

  it('caps the alerts list at 50 entries (FIFO eviction)', () => {
    const { result } = renderHook(() => useAlertNotifications())
    // Push 60 alerts — only the most-recent 50 should be retained.
    for (let i = 0; i < 60; i++) {
      pushAlert(makeAlert({ alert_id: `a-${i}`, name: `Alert ${i}`, timestamp: i }))
    }
    expect(result.current.alerts).toHaveLength(50)
    // Most-recent alert should be at the head (a-59, the last pushed).
    expect(result.current.alerts[0].alert_id).toBe('a-59')
    // The oldest retained alert should be a-10 (a-0..a-9 evicted).
    expect(result.current.alerts[49].alert_id).toBe('a-10')
    // Unread count continues to grow beyond 50 — only the LIST is
    // capped, not the counter (the trader should still see "you
    // missed 60 alerts" if they walked away).
    expect(result.current.unreadCount).toBe(60)
  })
})

describe('useAlertNotifications — acknowledge()', () => {
  it('removes a single alert and decrements unreadCount', () => {
    const { result } = renderHook(() => useAlertNotifications())
    const a1 = makeAlert({ alert_id: 'a1' })
    const a2 = makeAlert({ alert_id: 'a2' })
    pushAlert(a1)
    pushAlert(a2)
    expect(result.current.unreadCount).toBe(2)
    act(() => result.current.acknowledge('a1'))
    expect(result.current.alerts).toHaveLength(1)
    expect(result.current.alerts[0].alert_id).toBe('a2')
    expect(result.current.unreadCount).toBe(1)
  })

  it('does NOT underflow unreadCount below zero', () => {
    const { result } = renderHook(() => useAlertNotifications())
    pushAlert(makeAlert({ alert_id: 'a1' }))
    expect(result.current.unreadCount).toBe(1)
    act(() => result.current.acknowledge('a1'))
    expect(result.current.unreadCount).toBe(0)
    // Ack again — should clamp at 0.
    act(() => result.current.acknowledge('non-existent'))
    expect(result.current.unreadCount).toBe(0)
  })

  it('is a no-op for an unknown alert_id', () => {
    const { result } = renderHook(() => useAlertNotifications())
    pushAlert(makeAlert({ alert_id: 'a1' }))
    expect(result.current.alerts).toHaveLength(1)
    expect(result.current.unreadCount).toBe(1)
    act(() => result.current.acknowledge('does-not-exist'))
    expect(result.current.alerts).toHaveLength(1)
    expect(result.current.unreadCount).toBe(1)
  })
})

describe('useAlertNotifications — acknowledgeAll()', () => {
  it('clears the alerts list and resets unreadCount to zero', () => {
    const { result } = renderHook(() => useAlertNotifications())
    pushAlert(makeAlert({ alert_id: 'a1' }))
    pushAlert(makeAlert({ alert_id: 'a2' }))
    pushAlert(makeAlert({ alert_id: 'a3' }))
    expect(result.current.alerts).toHaveLength(3)
    expect(result.current.unreadCount).toBe(3)
    act(() => result.current.acknowledgeAll())
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
  })

  it('is a no-op when the list is already empty', () => {
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
    act(() => result.current.acknowledgeAll())
    expect(result.current.alerts).toEqual([])
    expect(result.current.unreadCount).toBe(0)
  })
})

describe('useAlertNotifications — toggle()', () => {
  it('flips enabled from true → false on first call', () => {
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.enabled).toBe(true)
    act(() => result.current.toggle())
    expect(result.current.enabled).toBe(false)
  })

  it('flips enabled false → true on the next call', () => {
    const { result } = renderHook(() => useAlertNotifications())
    act(() => result.current.toggle())
    expect(result.current.enabled).toBe(false)
    act(() => result.current.toggle())
    expect(result.current.enabled).toBe(true)
  })

  it('does NOT revoke the browser notification permission (only the local flag)', () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useAlertNotifications())
    act(() => result.current.toggle())
    expect(result.current.enabled).toBe(false)
    // Permission is unchanged — only the local flag flipped.
    expect(FakeNotification.permission).toBe('granted')
  })
})

describe('useAlertNotifications — isConnected', () => {
  it('reflects true when the underlying WS opens', () => {
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.isConnected).toBe(false)
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isConnected).toBe(true)
  })

  it('reflects false when the WS closes', () => {
    const { result } = renderHook(() => useAlertNotifications())
    act(() => MockWebSocket.instances[0].triggerOpen())
    expect(result.current.isConnected).toBe(true)
    act(() => MockWebSocket.instances[0].triggerClose())
    expect(result.current.isConnected).toBe(false)
  })
})

describe('useAlertNotifications — desktop toast side-effects', () => {
  it('fires a desktop toast when enabled=true AND permission=granted', () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.enabled).toBe(true)
    pushAlert(makeAlert({ alert_id: 'a1', name: 'Drawdown', message: '-5%', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(1)
    // showCriticalAlert prepends the severity icon — assert on the body
    // to avoid coupling to the icon selection.
    expect(constructorCalls[0].options?.body).toBe('-5%')
    expect(constructorCalls[0].title).toContain('Drawdown')
  })

  it('does NOT fire a desktop toast when enabled=false', () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useAlertNotifications())
    act(() => result.current.toggle())
    expect(result.current.enabled).toBe(false)
    pushAlert(makeAlert({ alert_id: 'a1', name: 'X', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(0)
    // The alert is still recorded in the list — only the toast is muted.
    expect(result.current.alerts).toHaveLength(1)
    expect(result.current.unreadCount).toBe(1)
  })

  it('does NOT fire a desktop toast when permission is default', () => {
    FakeNotification.permission = 'default'
    const { result } = renderHook(() => useAlertNotifications())
    expect(result.current.enabled).toBe(true)
    pushAlert(makeAlert({ alert_id: 'a1', name: 'X', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(0)
    // The alert is still recorded.
    expect(result.current.alerts).toHaveLength(1)
  })

  it('fires a toast per alert when multiple alerts arrive in sequence', () => {
    FakeNotification.permission = 'granted'
    renderHook(() => useAlertNotifications())
    pushAlert(makeAlert({ alert_id: 'a1', severity: 'critical' }))
    pushAlert(makeAlert({ alert_id: 'a2', severity: 'error' }))
    pushAlert(makeAlert({ alert_id: 'a3', severity: 'warning' }))
    pushAlert(makeAlert({ alert_id: 'a4', severity: 'info' }))
    expect(constructorCalls).toHaveLength(4)
  })

  it('does NOT crash when the Notifications API is unavailable', () => {
    delete (globalThis as any).Notification
    delete (window as any).Notification
    const { result } = renderHook(() => useAlertNotifications())
    // Hook should not throw — the panel is the source of truth.
    expect(() => pushAlert(makeAlert({ alert_id: 'a1' }))).not.toThrow()
    expect(result.current.alerts).toHaveLength(1)
  })
})

describe('useAlertNotifications — closure stability across re-renders', () => {
  it('keeps receiving alerts after the enabled flag flips (toast gates only)', () => {
    FakeNotification.permission = 'granted'
    const { result, rerender } = renderHook(() => useAlertNotifications())
    pushAlert(makeAlert({ alert_id: 'a1', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(1)
    // Flip enabled off — subsequent pushes should NOT fire a toast
    // but SHOULD still record the alert.
    act(() => result.current.toggle())
    rerender()
    pushAlert(makeAlert({ alert_id: 'a2', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(1)
    expect(result.current.alerts).toHaveLength(2)
    // Flip enabled back on — toasts resume.
    act(() => result.current.toggle())
    rerender()
    pushAlert(makeAlert({ alert_id: 'a3', severity: 'critical' }))
    expect(constructorCalls).toHaveLength(2)
  })
})
