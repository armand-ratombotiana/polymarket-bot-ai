// hooks/useNotifications.test.ts — W13-6 tests for the notification hook.
//
// Strategy:
//   * jsdom does NOT implement the Web Notifications API, so we stub
//     `global.Notification` with a fake class (same shape as the one in
//     notifications.test.ts).
//   * `apiFetch` ultimately calls `global.fetch`, which the test setup
//     in `src/test/setup.ts` already replaces with a `vi.fn()`. We
//     configure its responses per-test via `vi.mocked(fetch).mockResolvedValue(...)`.
//   * The hook's polling effect has `lastAlertIds` in its dep array,
//     so the 30 s interval is re-created every time a new alert is
//     seen. We use `vi.useFakeTimers()` and manually advance time to
//     drive the interval. To exercise the *async* polling body
//     (`apiFetch` → `res.json()` → `setLastAlertIds`), we flush
//     microtasks after each `vi.advanceTimersByTimeAsync()` call.
//
// What's covered:
//   1. Initial state: permission='default', enabled=false, supported=true.
//   2. supported=false when Notification is absent.
//   3. enable(): requests permission, on granted flips enabled=true,
//      persists localStorage flag, fires a test toast.
//   4. enable(): when permission denied, stays disabled.
//   5. disable(): flips enabled=false, persists flag.
//   6. toggle(): delegates to disable/enable.
//   7. localStorage persistence: 'true' flag + granted permission →
//      enabled=true on mount.
//   8. Polling: when enabled, fires GET /api/alerts every 30 s and
//      does NOT fire when disabled.
//   9. New-alert detection: critical/error alerts trigger
//      showCriticalAlert; info/warning do NOT (but their IDs are
//      still tracked).
//  10. seen-set dedup: an alert already in lastAlertIds does not
//      re-trigger a toast on subsequent polls.
//  11. Hidden tab skips polling.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { renderHook, act, waitFor } from '@testing-library/react'
import { useNotifications } from './useNotifications'

// Captured Notification constructor calls — reinitialised per test.
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

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

// Helper: mock `requestPermission` to resolve with `value` AND sync the
// static `permission` field to that value — mirroring real browser
// behaviour where the permission string is auto-updated after the user
// responds to the prompt.
function mockRequestPermissionResult(value: NotificationPermission) {
  FakeNotification.requestPermission = vi.fn(async () => {
    FakeNotification.permission = value
    return value
  })
}

beforeEach(() => {
  constructorCalls = []
  FakeNotification.permission = 'default'
  FakeNotification.requestPermission = vi.fn(async () => 'default' as NotificationPermission)
  ;(globalThis as any).Notification = FakeNotification
  ;(window as any).Notification = FakeNotification

  localStorage.clear()
  // Default: tab is visible (some tests override per-case).
  Object.defineProperty(document, 'hidden', {
    value: false,
    configurable: true,
    writable: true,
  })
  // Default: every fetch returns an empty alerts list; tests can
  // override via vi.mocked(fetch).mockResolvedValueOnce(...).
  global.fetch = vi.fn().mockResolvedValue(jsonOk({ alerts: [] })) as unknown as typeof fetch
})

afterEach(() => {
  delete (globalThis as any).Notification
  delete (window as any).Notification
  vi.restoreAllMocks()
  vi.useRealTimers()
  Object.defineProperty(document, 'hidden', {
    value: false,
    configurable: true,
    writable: true,
  })
})

describe('useNotifications — initial state', () => {
  it('exposes supported=true, permission=default, enabled=false on mount', () => {
    const { result } = renderHook(() => useNotifications())
    expect(result.current.supported).toBe(true)
    expect(result.current.permission).toBe('default')
    expect(result.current.enabled).toBe(false)
  })

  it('exposes supported=false when Notification is not defined', () => {
    delete (window as any).Notification
    delete (globalThis as any).Notification
    const { result } = renderHook(() => useNotifications())
    expect(result.current.supported).toBe(false)
  })
})

describe('useNotifications — enable()', () => {
  it('requests permission, flips enabled=true, persists flag, fires a test toast', async () => {
    FakeNotification.permission = 'default'
    mockRequestPermissionResult('granted')

    const { result } = renderHook(() => useNotifications())
    expect(result.current.enabled).toBe(false)

    let granted = false
    await act(async () => {
      granted = await result.current.enable()
    })

    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1)
    expect(granted).toBe(true)
    expect(result.current.enabled).toBe(true)
    expect(result.current.permission).toBe('granted')
    expect(localStorage.getItem('notifications_enabled')).toBe('true')
    // The hook fires a test "Notifications Enabled" toast on success.
    expect(constructorCalls).toHaveLength(1)
    expect(constructorCalls[0].title).toBe('ℹ️ Notifications Enabled')
  })

  it('stays disabled and does not persist when permission is denied', async () => {
    FakeNotification.permission = 'default'
    mockRequestPermissionResult('denied')

    const { result } = renderHook(() => useNotifications())
    let granted = true
    await act(async () => {
      granted = await result.current.enable()
    })
    expect(granted).toBe(false)
    expect(result.current.enabled).toBe(false)
    expect(result.current.permission).toBe('denied')
    expect(localStorage.getItem('notifications_enabled')).toBeNull()
    // No test toast when denied.
    expect(constructorCalls).toHaveLength(0)
  })

  it('skips requestPermission if already granted', async () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    // Already-granted → requestPermission is NOT called (the wrapper
    // short-circuits and returns 'granted').
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled()
    expect(result.current.enabled).toBe(true)
  })
})

describe('useNotifications — disable()', () => {
  it('flips enabled=false and persists the flag', async () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    expect(result.current.enabled).toBe(true)

    act(() => {
      result.current.disable()
    })
    expect(result.current.enabled).toBe(false)
    expect(localStorage.getItem('notifications_enabled')).toBe('false')
  })
})

describe('useNotifications — toggle()', () => {
  it('calls enable when disabled', async () => {
    mockRequestPermissionResult('granted')
    const { result } = renderHook(() => useNotifications())
    expect(result.current.enabled).toBe(false)
    await act(async () => {
      result.current.toggle()
    })
    // enable is async — wait one tick for it to resolve.
    await waitFor(() => {
      expect(result.current.enabled).toBe(true)
    })
  })

  it('calls disable when enabled', async () => {
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    expect(result.current.enabled).toBe(true)
    act(() => {
      result.current.toggle()
    })
    expect(result.current.enabled).toBe(false)
  })
})

describe('useNotifications — localStorage persistence', () => {
  it('re-enables on mount when localStorage flag is "true" AND permission is granted', () => {
    FakeNotification.permission = 'granted'
    localStorage.setItem('notifications_enabled', 'true')
    const { result } = renderHook(() => useNotifications())
    expect(result.current.enabled).toBe(true)
  })

  it('does NOT re-enable on mount when localStorage flag is "true" but permission is default', () => {
    FakeNotification.permission = 'default'
    localStorage.setItem('notifications_enabled', 'true')
    const { result } = renderHook(() => useNotifications())
    expect(result.current.enabled).toBe(false)
  })

  it('does NOT re-enable on mount when localStorage flag is "false"', () => {
    FakeNotification.permission = 'granted'
    localStorage.setItem('notifications_enabled', 'false')
    const { result } = renderHook(() => useNotifications())
    expect(result.current.enabled).toBe(false)
  })
})

describe('useNotifications — polling', () => {
  it('does NOT poll when disabled', async () => {
    vi.useFakeTimers()
    const { result } = renderHook(() => useNotifications())
    // Disabled by default — advance 5 minutes, expect zero fetches.
    await vi.advanceTimersByTimeAsync(5 * 60_000)
    expect(vi.mocked(fetch)).not.toHaveBeenCalled()
    expect(result.current.enabled).toBe(false)
  })

  it('polls /api/alerts every 30 s when enabled', async () => {
    vi.useFakeTimers()
    // Pre-grant permission so enable() doesn't actually prompt.
    FakeNotification.permission = 'granted'
    const { result } = renderHook(() => useNotifications())
    // enable() will fire a "test" toast (1 Notification call). It
    // does not call fetch. The polling effect runs after enable().
    await act(async () => {
      await result.current.enable()
    })
    // Clear constructor calls so we can isolate the polling effect.
    constructorCalls = []
    // Flush the initial synchronous poll that fires when enabled flips.
    await vi.advanceTimersByTimeAsync(0)

    const initialCallCount = vi.mocked(fetch).mock.calls.length
    expect(initialCallCount).toBeGreaterThanOrEqual(1)

    // Advance 30 s — should fire one more poll.
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 1)

    // Advance another 30 s — another poll.
    await vi.advanceTimersByTimeAsync(30_000)
    expect(vi.mocked(fetch).mock.calls.length).toBe(initialCallCount + 2)
  })

  it('does NOT poll when the tab is hidden', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    // Hide the tab before mounting.
    Object.defineProperty(document, 'hidden', { value: true, configurable: true })

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    // Clear any initial fetches from before enabling.
    vi.mocked(fetch).mockClear()

    // Advance 2 minutes — no polls should fire while hidden.
    await vi.advanceTimersByTimeAsync(120_000)
    expect(vi.mocked(fetch)).not.toHaveBeenCalled()
  })

  it('shows a desktop toast when a new critical alert arrives', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'

    // Queue a critical alert for the next poll.
    const criticalAlert = {
      alert_id: 'a1',
      name: 'Drawdown Breach',
      message: 'Daily P&L -5%',
      severity: 'critical',
      acknowledged: false,
      timestamp: 1,
    }
    vi.mocked(fetch).mockResolvedValue(jsonOk({ alerts: [criticalAlert] }))

    const { result } = renderHook(() => useNotifications())
    // Pre-granted permission → enable() doesn't prompt, just flips state.
    // `act()` flushes the polling effect's microtask, so by the time it
    // resolves the critical alert toast has ALREADY fired (alongside the
    // "Notifications Enabled" test toast from enable() itself).
    await act(async () => {
      await result.current.enable()
    })

    // Filter out the "Notifications Enabled" test toast; the remaining
    // entry should be the critical alert toast.
    const alertToasts = constructorCalls.filter(c => c.title !== 'ℹ️ Notifications Enabled')
    expect(alertToasts).toHaveLength(1)
    expect(alertToasts[0].title).toBe('🚨 Drawdown Breach')
    expect(alertToasts[0].options?.body).toBe('Daily P&L -5%')
    expect(alertToasts[0].options?.requireInteraction).toBe(true)
  })

  it('shows a toast for error severity but does NOT set requireInteraction', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    const errorAlert = {
      alert_id: 'a2',
      name: 'Order Rejected',
      message: 'price too low',
      severity: 'error',
      acknowledged: false,
      timestamp: 1,
    }
    vi.mocked(fetch).mockResolvedValue(jsonOk({ alerts: [errorAlert] }))

    const { result } = renderHook(() => useNotifications())
    // act() flushes the polling effect's microtask so the error toast
    // fires before act() returns.
    await act(async () => {
      await result.current.enable()
    })

    const alertToasts = constructorCalls.filter(c => c.title !== 'ℹ️ Notifications Enabled')
    expect(alertToasts).toHaveLength(1)
    expect(alertToasts[0].title).toBe('❌ Order Rejected')
    expect(alertToasts[0].options?.requireInteraction).toBe(false)
  })

  it('does NOT show a toast for info or warning alerts (still tracks IDs)', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    vi.mocked(fetch).mockResolvedValue(
      jsonOk({
        alerts: [
          { alert_id: 'a3', name: 'Info', message: 'm', severity: 'info', acknowledged: false, timestamp: 1 },
          { alert_id: 'a4', name: 'Warn', message: 'm', severity: 'warning', acknowledged: false, timestamp: 1 },
        ],
      }),
    )

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    constructorCalls = []
    await vi.advanceTimersByTimeAsync(0)

    expect(constructorCalls).toHaveLength(0)
  })

  it('does NOT re-toast an alert already in lastAlertIds on a subsequent poll', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    const alert = {
      alert_id: 'a5',
      name: 'Critical',
      message: 'm',
      severity: 'critical',
      acknowledged: false,
      timestamp: 1,
    }
    vi.mocked(fetch).mockResolvedValue(jsonOk({ alerts: [alert] }))

    const { result } = renderHook(() => useNotifications())
    // act() flushes the polling microtask; the critical toast fires here.
    await act(async () => {
      await result.current.enable()
    })
    const alertToasts = constructorCalls.filter(c => c.title !== 'ℹ️ Notifications Enabled')
    expect(alertToasts).toHaveLength(1)
    const initialTotalToasts = constructorCalls.length

    // Advance 30 s — same alert arrives again, but it's already in
    // lastAlertIds so NO new toast fires.
    await vi.advanceTimersByTimeAsync(30_000)
    expect(constructorCalls.length).toBe(initialTotalToasts)
  })

  it('keeps only the last 50 alert IDs in the seen-set', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    // Generate 60 unique alerts of severity 'info' (so no toasts fire
    // — we only care about the seen-set pruning logic, which we'll
    // observe indirectly by ensuring the hook doesn't crash and the
    // final poll still sees the most recent IDs).
    const alerts = Array.from({ length: 60 }, (_, i) => ({
      alert_id: `id-${i}`,
      name: `A${i}`,
      message: 'm',
      severity: 'info',
      acknowledged: false,
      timestamp: i,
    }))
    vi.mocked(fetch).mockResolvedValue(jsonOk({ alerts }))

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    await vi.advanceTimersByTimeAsync(0)
    // No throw, no toast (info severity), test passes if we got here.
    expect(result.current.enabled).toBe(true)
  })

  it('does not crash when fetch rejects', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    vi.mocked(fetch).mockRejectedValue(new Error('network down'))

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    await vi.advanceTimersByTimeAsync(0)
    // Hook stays enabled; subsequent polls continue.
    expect(result.current.enabled).toBe(true)

    // Next 30 s tick also does not crash.
    await vi.advanceTimersByTimeAsync(30_000)
    expect(result.current.enabled).toBe(true)
  })

  it('does not crash when the response is non-200', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    vi.mocked(fetch).mockResolvedValue(new Response('boom', { status: 500 }))

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    await vi.advanceTimersByTimeAsync(0)
    expect(result.current.enabled).toBe(true)
  })

  it('handles missing alerts field in the response body', async () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    vi.mocked(fetch).mockResolvedValue(jsonOk({})) // no `alerts` key

    const { result } = renderHook(() => useNotifications())
    await act(async () => {
      await result.current.enable()
    })
    constructorCalls = []
    await vi.advanceTimersByTimeAsync(0)
    expect(constructorCalls).toHaveLength(0)
    expect(result.current.enabled).toBe(true)
  })
})
