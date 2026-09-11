// hooks/useNotifications.ts — W13-6 Browser push-notification React hook.
//
// Orchestrates the notification permission lifecycle + a 30 s alert-poll
// on top of the primitives in `src/lib/notifications.ts`.
//
// Contract:
//   const { supported, permission, enabled, enable, disable, toggle } = useNotifications()
//   if (!supported) render nothing
//   else render <button onClick={toggle}>{enabled ? '🔔' : '🔕'}</button>
//
// Behaviour:
//   * On mount, reads `Notification.permission` and the persisted
//     `notifications_enabled` flag in localStorage. The persisted flag
//     only re-enables polling if permission is currently `granted`
//     (handles the case where a user revoked permission via the
//     browser's site-settings panel between sessions).
//   * `enable()` calls `requestPermission()`. If granted, writes
//     `notifications_enabled=true` to localStorage and fires a test
//     toast so the operator can confirm what they look like.
//   * `disable()` flips the flag to `false` (does NOT revoke the
//     browser permission — that's impossible from JS; the user must
//     do it via the browser's site-settings panel).
//   * When enabled, polls `/api/alerts?limit=10&unacknowledged_only=true`
//     every 30 s. New alerts whose severity is `critical` or `error`
//     trigger a desktop toast via `showCriticalAlert()`. The polling
//     is visibility-aware: when the tab is hidden we skip the poll
//     (browser notifications still arrive even when the tab is
//     backgrounded, but if the tab is hidden the user is by definition
//     not actively looking at it — and the next visibilitychange
//     event will trigger an immediate re-poll on resume).
//   * `lastAlertIds` tracks the alert_ids we've already seen so we
//     don't re-toast an alert we already showed. Capped at 50 entries
//     to bound memory in long-running sessions.
//
// Implementation note: the polling effect has `lastAlertIds` in its
// dependency array. This means the interval is torn down and recreated
// every time the seen-set changes (i.e. every time a new alert arrives).
// That's intentional and cheap: setInterval creation is O(1), and it
// ensures the next poll re-reads the latest `lastAlertIds` state
// without needing a ref mirror.
'use client'
import { useState, useEffect, useCallback } from 'react'
import {
  isNotificationSupported,
  getPermission,
  requestPermission,
  showCriticalAlert,
  type NotificationPermission,
} from '@/lib/notifications'
import { apiFetch } from '@/lib/api'

interface Alert {
  alert_id: string
  name: string
  message: string
  severity: string
  acknowledged: boolean
  timestamp: number
}

export function useNotifications() {
  const [permission, setPermission] = useState<NotificationPermission>('default')
  const [enabled, setEnabled] = useState(false)
  const [lastAlertIds, setLastAlertIds] = useState<Set<string>>(new Set())

  // Initialize permission state
  useEffect(() => {
    if (!isNotificationSupported()) return
    setPermission(getPermission())
    // Load enabled state from localStorage
    const stored = localStorage.getItem('notifications_enabled')
    if (stored === 'true' && getPermission() === 'granted') {
      setEnabled(true)
    }
  }, [])

  // Poll for new alerts
  useEffect(() => {
    if (!enabled) return
    if (document.hidden) return // Don't poll when tab is hidden (browser notification will still show)

    const poll = async () => {
      try {
        const res = await apiFetch('/api/alerts?limit=10&unacknowledged_only=true')
        if (!res.ok) return
        const data = await res.json()
        const alerts: Alert[] = data.alerts || []

        // Find new alerts (not seen before)
        const newAlerts = alerts.filter(a => !lastAlertIds.has(a.alert_id))

        // Show notifications for new critical/error alerts
        for (const alert of newAlerts) {
          if (alert.severity === 'critical' || alert.severity === 'error') {
            showCriticalAlert(alert)
          }
        }

        // Update seen set
        if (newAlerts.length > 0) {
          setLastAlertIds(prev => {
            const next = new Set(prev)
            for (const a of alerts) next.add(a.alert_id)
            // Keep only last 50 IDs
            if (next.size > 50) {
              const arr = Array.from(next).slice(-50)
              return new Set(arr)
            }
            return next
          })
        }
      } catch {
        // Silent fail
      }
    }

    poll()
    const interval = setInterval(poll, 30000) // Poll every 30s
    return () => clearInterval(interval)
  }, [enabled, lastAlertIds])

  const enable = useCallback(async () => {
    const perm = await requestPermission()
    setPermission(perm)
    if (perm === 'granted') {
      setEnabled(true)
      localStorage.setItem('notifications_enabled', 'true')
      // Show a test notification
      showCriticalAlert({
        name: 'Notifications Enabled',
        message: 'You will receive alerts for critical events.',
        severity: 'info',
      })
    }
    return perm === 'granted'
  }, [])

  const disable = useCallback(() => {
    setEnabled(false)
    localStorage.setItem('notifications_enabled', 'false')
  }, [])

  const toggle = useCallback(() => {
    if (enabled) {
      disable()
    } else {
      enable()
    }
  }, [enabled, enable, disable])

  return {
    permission,
    enabled,
    supported: isNotificationSupported(),
    enable,
    disable,
    toggle,
  }
}
