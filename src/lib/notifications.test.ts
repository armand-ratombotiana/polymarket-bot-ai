// lib/notifications.test.ts — W13-6 unit tests for the notification
// primitives.
//
// Strategy: jsdom does NOT implement the Web Notifications API, so
// every test stubs `global.Notification` with a fake constructor that
// records its constructor arguments + exposes a static `permission`
// and async `requestPermission()`. We then call the real exports
// from `notifications.ts` and assert on the captured state.
//
// What's covered:
//   1. isNotificationSupported: true when `Notification` is on window,
//      false when removed.
//   2. getPermission: returns 'denied' when not supported, otherwise
//      returns `Notification.permission`.
//   3. requestPermission: returns 'denied' when not supported, returns
//      'granted' immediately when already granted, otherwise awaits
//      `Notification.requestPermission()` and forwards the result.
//   4. showNotification: no-ops when not supported or permission not
//      granted; otherwise constructs a Notification with icon/badge
//      defaults, schedules a 10s auto-close, and wires an onclick
//      handler that focuses the window and closes the toast.
//   5. showCriticalAlert: prefixes the title with the per-severity
//      emoji, sets `tag: alert-${name}`, and only sets
//      `requireInteraction: true` when severity === 'critical'.
//   6. showTradeNotification: uses 📈/📉 based on side, formats the
//      body with `side size @ price — truncated_token_id...`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  isNotificationSupported,
  getPermission,
  requestPermission,
  showNotification,
  showCriticalAlert,
  showTradeNotification,
} from './notifications'

// Captured constructor calls — reinitialised per-test in `beforeEach`.
let constructorCalls: Array<{ title: string; options: NotificationOptions | undefined }>

// The fake Notification class. Must be a class (the source does
// `new Notification(title, options)`) so we use a class with a real
// constructor; the static members mirror the Web Notifications API.
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

// Install / restore the stub around each test so call history and
// static permission state never leak across cases.
beforeEach(() => {
  constructorCalls = []
  FakeNotification.permission = 'default'
  FakeNotification.requestPermission = vi.fn(async () => 'default' as NotificationPermission)
  // Install on both `global` (used by the source via `Notification`)
  // AND `window` (so `'Notification' in window` returns true).
  ;(globalThis as any).Notification = FakeNotification
  ;(window as any).Notification = FakeNotification
})

afterEach(() => {
  // Remove the stub so the next test starts from a clean slate.
  delete (globalThis as any).Notification
  delete (window as any).Notification
  vi.restoreAllMocks()
  vi.useRealTimers()
})

describe('isNotificationSupported', () => {
  it('returns true when Notification is defined on window', () => {
    expect(isNotificationSupported()).toBe(true)
  })

  it('returns false when Notification is not on window', () => {
    delete (window as any).Notification
    delete (globalThis as any).Notification
    expect(isNotificationSupported()).toBe(false)
  })
})

describe('getPermission', () => {
  it('returns denied when notifications are not supported', () => {
    delete (window as any).Notification
    delete (globalThis as any).Notification
    expect(getPermission()).toBe('denied')
  })

  it('returns the current Notification.permission when supported', () => {
    FakeNotification.permission = 'granted'
    expect(getPermission()).toBe('granted')
    FakeNotification.permission = 'denied'
    expect(getPermission()).toBe('denied')
    FakeNotification.permission = 'default'
    expect(getPermission()).toBe('default')
  })
})

describe('requestPermission', () => {
  it('returns denied when notifications are not supported', async () => {
    delete (window as any).Notification
    delete (globalThis as any).Notification
    const result = await requestPermission()
    expect(result).toBe('denied')
  })

  it('returns granted immediately when permission is already granted', async () => {
    FakeNotification.permission = 'granted'
    const result = await requestPermission()
    expect(result).toBe('granted')
    expect(FakeNotification.requestPermission).not.toHaveBeenCalled()
  })

  it('calls Notification.requestPermission when status is default', async () => {
    FakeNotification.permission = 'default'
    FakeNotification.requestPermission.mockResolvedValue('granted' as NotificationPermission)
    const result = await requestPermission()
    expect(FakeNotification.requestPermission).toHaveBeenCalledTimes(1)
    expect(result).toBe('granted')
  })

  it('forwards a denied result', async () => {
    FakeNotification.permission = 'default'
    FakeNotification.requestPermission.mockResolvedValue('denied' as NotificationPermission)
    const result = await requestPermission()
    expect(result).toBe('denied')
  })
})

describe('showNotification', () => {
  it('returns undefined when notifications are not supported', () => {
    delete (window as any).Notification
    delete (globalThis as any).Notification
    const result = showNotification('hi')
    expect(result).toBeUndefined()
    expect(constructorCalls).toHaveLength(0)
  })

  it('returns undefined when permission is not granted', () => {
    FakeNotification.permission = 'default'
    const result = showNotification('hi')
    expect(result).toBeUndefined()
    expect(constructorCalls).toHaveLength(0)
  })

  it('constructs a Notification with default icon/badge and merges options', () => {
    FakeNotification.permission = 'granted'
    showNotification('Title', { body: 'hello' })
    expect(constructorCalls).toHaveLength(1)
    expect(constructorCalls[0].title).toBe('Title')
    expect(constructorCalls[0].options).toMatchObject({
      icon: '/icon.svg',
      badge: '/icon.svg',
      body: 'hello',
    })
  })

  it('auto-closes the notification after 10 seconds', () => {
    vi.useFakeTimers()
    FakeNotification.permission = 'granted'
    const notif = showNotification('Title') as unknown as FakeNotification
    expect(notif).toBeInstanceOf(FakeNotification)
    // Not closed yet immediately after construction.
    expect(notif.close).not.toHaveBeenCalled()
    // Advance 10s — close should fire.
    vi.advanceTimersByTime(10_000)
    expect(notif.close).toHaveBeenCalledTimes(1)
  })

  it('focuses the window and closes on click', () => {
    FakeNotification.permission = 'granted'
    const focusSpy = vi.spyOn(window, 'focus').mockImplementation(() => {})
    const notif = showNotification('Title') as unknown as FakeNotification
    expect(notif.onclick).toBeInstanceOf(Function)
    notif.onclick!(new Event('click'))
    expect(focusSpy).toHaveBeenCalledTimes(1)
    expect(notif.close).toHaveBeenCalledTimes(1)
    focusSpy.mockRestore()
  })

  it('returns the Notification instance', () => {
    FakeNotification.permission = 'granted'
    const notif = showNotification('Title')
    expect(notif).toBeInstanceOf(FakeNotification)
  })

  it('swallows constructor errors and logs them', () => {
    FakeNotification.permission = 'granted'
    // Force the constructor to throw.
    ;(globalThis as any).Notification = class {
      constructor() {
        throw new Error('boom')
      }
      static permission = 'granted'
      static requestPermission = vi.fn()
    }
    ;(window as any).Notification = (globalThis as any).Notification
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const result = showNotification('Title')
    expect(result).toBeUndefined()
    expect(errSpy).toHaveBeenCalledWith('Notification error:', expect.any(Error))
    errSpy.mockRestore()
  })
})

describe('showCriticalAlert', () => {
  beforeEach(() => {
    FakeNotification.permission = 'granted'
  })

  it('uses 🚨 for critical severity and sets requireInteraction', () => {
    showCriticalAlert({ name: 'Drawdown Breach', message: '-5%', severity: 'critical' })
    expect(constructorCalls).toHaveLength(1)
    expect(constructorCalls[0].title).toBe('🚨 Drawdown Breach')
    expect(constructorCalls[0].options).toMatchObject({
      body: '-5%',
      tag: 'alert-Drawdown Breach',
      requireInteraction: true,
    })
  })

  it('uses ❌ for error severity and sets requireInteraction=false', () => {
    showCriticalAlert({ name: 'Order Rejected', message: 'price too low', severity: 'error' })
    expect(constructorCalls[0].title).toBe('❌ Order Rejected')
    // The implementation always sets `requireInteraction` to the boolean
    // result of `severity === 'critical'` — so for non-critical severities
    // the field is present and explicitly `false` (not omitted).
    expect(constructorCalls[0].options?.requireInteraction).toBe(false)
  })

  it('uses ⚠️ for warning severity', () => {
    showCriticalAlert({ name: 'Latency Spike', message: '250ms', severity: 'warning' })
    expect(constructorCalls[0].title).toBe('⚠️ Latency Spike')
  })

  it('uses ℹ️ for info severity', () => {
    showCriticalAlert({ name: 'Info', message: 'msg', severity: 'info' })
    expect(constructorCalls[0].title).toBe('ℹ️ Info')
  })

  it('uses 🔔 fallback for unknown severity', () => {
    showCriticalAlert({ name: 'X', message: 'm', severity: 'totally-unknown' })
    expect(constructorCalls[0].title).toBe('🔔 X')
  })

  it('no-ops when permission is not granted', () => {
    FakeNotification.permission = 'denied'
    showCriticalAlert({ name: 'X', message: 'm', severity: 'critical' })
    expect(constructorCalls).toHaveLength(0)
  })
})

describe('showTradeNotification', () => {
  beforeEach(() => {
    FakeNotification.permission = 'granted'
  })

  it('uses 📈 and formats body for a BUY trade', () => {
    showTradeNotification({
      side: 'BUY',
      token_id: 'abcdefghijklmnop',
      price: 0.123456,
      size: 100,
    })
    expect(constructorCalls).toHaveLength(1)
    expect(constructorCalls[0].title).toBe('📈 Trade Filled')
    expect(constructorCalls[0].options?.body).toBe('BUY 100 @ 0.1235 — abcdefghijkl...')
    expect(constructorCalls[0].options?.tag).toBe('trade-abcdefghijklmnop')
  })

  it('uses 📉 for a SELL trade', () => {
    showTradeNotification({ side: 'SELL', token_id: 'tok', price: 0.5, size: 7 })
    expect(constructorCalls[0].title).toBe('📉 Trade Filled')
    expect(constructorCalls[0].options?.body).toContain('SELL 7 @ 0.5000')
  })

  it('no-ops when permission is not granted', () => {
    FakeNotification.permission = 'denied'
    showTradeNotification({ side: 'BUY', token_id: 'tok', price: 1, size: 1 })
    expect(constructorCalls).toHaveLength(0)
  })
})
