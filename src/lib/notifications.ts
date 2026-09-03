// lib/notifications.ts — W13-6 Browser push-notification utilities.
//
// Thin, framework-agnostic wrapper around the Web Notifications API
// (https://developer.mozilla.org/en-US/docs/Web/API/Notifications_API)
// used by the dashboard to surface critical trading alerts even when
// the operator has the tab in the background.
//
// Design notes:
//   * Every function is `typeof window === 'undefined'`-guarded so it
//     is safe to import from a server component / module-scope code —
//     the call simply no-ops in non-browser environments.
//   * `showNotification` auto-closes the toast after 10 seconds (the
//     OS-level default in Chromium is 20 s, which is too long for a
//     trading dashboard where stale alerts are noise).
//   * `requireInteraction: true` is only set for `severity: 'critical'`
//     alerts so the toast persists until dismissed — every other
//     severity auto-closes.
//   * `onclick` focuses the originating window so the operator can
//     jump straight back to the dashboard.
//
// The hook in `src/hooks/useNotifications.ts` orchestrates permission
// lifecycle + polling; this module is just the primitives.
'use client'

export type NotificationPermission = 'default' | 'granted' | 'denied'

export function isNotificationSupported(): boolean {
  return typeof window !== 'undefined' && 'Notification' in window
}

export function getPermission(): NotificationPermission {
  if (!isNotificationSupported()) return 'denied'
  return Notification.permission as NotificationPermission
}

export async function requestPermission(): Promise<NotificationPermission> {
  if (!isNotificationSupported()) return 'denied'
  if (Notification.permission === 'granted') return 'granted'
  const result = await Notification.requestPermission()
  return result as NotificationPermission
}

export function showNotification(title: string, options?: NotificationOptions) {
  if (!isNotificationSupported() || Notification.permission !== 'granted') return
  try {
    const notif = new Notification(title, {
      icon: '/icon.svg',
      badge: '/icon.svg',
      ...options,
    })
    // Auto-close after 10 seconds (unless requireInteraction was set).
    setTimeout(() => notif.close(), 10000)
    // Focus window on click
    notif.onclick = () => {
      window.focus()
      notif.close()
    }
    return notif
  } catch (e) {
    console.error('Notification error:', e)
  }
}

export function showCriticalAlert(alert: { name: string; message: string; severity: string }) {
  const icons: Record<string, string> = {
    critical: '🚨',
    error: '❌',
    warning: '⚠️',
    info: 'ℹ️',
  }
  const icon = icons[alert.severity] || '🔔'
  showNotification(`${icon} ${alert.name}`, {
    body: alert.message,
    tag: `alert-${alert.name}`,
    requireInteraction: alert.severity === 'critical',
  })
}

export function showTradeNotification(trade: { side: string; token_id: string; price: number; size: number }) {
  const icon = trade.side === 'BUY' ? '📈' : '📉'
  showNotification(`${icon} Trade Filled`, {
    body: `${trade.side} ${trade.size} @ ${trade.price.toFixed(4)} — ${trade.token_id.slice(0, 12)}...`,
    tag: `trade-${trade.token_id}`,
  })
}
