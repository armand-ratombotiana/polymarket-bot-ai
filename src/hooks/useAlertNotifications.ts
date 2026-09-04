// hooks/useAlertNotifications.ts — W23-4 Real-time alert notifications via WS.
//
// Composes `useWebSocket` to receive push messages on the `alerts`
// channel (sent by the server whenever an alert fires — drawdown
// breach, order rejection, model drift, etc.). The hook keeps a
// rolling 50-entry list of recent alerts + an unread counter and
// fires a desktop toast via the W13-6 `showCriticalAlert` helper
// whenever a new alert arrives (subject to the user's `enabled`
// flag and the browser's notification permission state).
//
// Design notes:
//   * `useWebSocket` stores `onMessage` in a ref that is refreshed
//     on every render (no deps array on the ref-sync effect), so
//     passing a fresh closure on each render is fine — it does
//     NOT tear down the underlying socket. We wrap the callback
//     in `useCallback` purely so its identity is stable across
//     renders that don't actually change `enabled`; this keeps
//     React's reconciliation cheap.
//   * `enabled` is a local-state toggle — distinct from the
//     browser's notification permission. When `enabled=false`,
//     the hook still records alerts (so the trader can see them
//     in the panel) but does NOT fire desktop toasts. This lets
//     the trader mute the OS-level interruption without losing
//     the in-app alert feed.
//   * The hook is intentionally agnostic about the alert shape
//     fields beyond the four documented below — extra fields
//     (e.g. `strategy_id`, `token_id`) are preserved in the
//     state list so the panel can render them if present.
//   * `acknowledge(alertId)` removes a single alert and
//     decrements the unread counter (clamped at 0).
//     `acknowledgeAll()` clears both. There is no server-side
//     acknowledge call yet — this hook tracks "acknowledged" as
//     a purely client-side notion (the alert remains in the
//     backend's active list until it ages out / is resolved
//     server-side). W23-5 will wire `POST /api/alerts/:id/ack`
//     when that endpoint lands.
'use client'

import { useState, useCallback } from 'react'
import { useWebSocket } from './useWebSocket'
import { showCriticalAlert, isNotificationSupported } from '@/lib/notifications'

export interface Alert {
  alert_id: string
  name: string
  message: string
  severity: 'critical' | 'error' | 'warning' | 'info'
  timestamp: number
}

export interface UseAlertNotificationsResult {
  /** Most-recent-first list of alerts (capped at 50). */
  alerts: Alert[]
  /** Number of alerts the user has not yet acknowledged. */
  unreadCount: number
  /** Whether the user has enabled OS-level toast notifications. */
  enabled: boolean
  /** Whether the underlying WebSocket is currently open. */
  isConnected: boolean
  /** Remove a single alert from the list + decrement unread. */
  acknowledge: (alertId: string) => void
  /** Clear the entire alert list + reset unread to 0. */
  acknowledgeAll: () => void
  /** Flip the local `enabled` flag (does NOT revoke browser permission). */
  toggle: () => void
}

export function useAlertNotifications(): UseAlertNotificationsResult {
  const [alerts, setAlerts] = useState<Alert[]>([])
  const [unreadCount, setUnreadCount] = useState(0)
  const [enabled, setEnabled] = useState(true)

  const { isConnected } = useWebSocket({
    onMessage: useCallback(
      (msg: unknown) => {
        // The WS server wraps each push as `{ channel, data }`. We
        // only act on the `alerts` channel with `type: 'alert'`
        // payloads — other channels (positions, orders, snapshot)
        // are handled by their respective hooks.
        const m = msg as { channel?: string; data?: { type?: string; alert?: Alert } }
        if (!m || m.channel !== 'alerts') return
        if (!m.data || m.data.type !== 'alert') return
        const alert = m.data.alert
        if (!alert || typeof alert.alert_id !== 'string') return

        setAlerts((prev) => [alert, ...prev].slice(0, 50)) // Keep last 50
        setUnreadCount((prev) => prev + 1)

        // Fire a desktop toast — subject to both the local `enabled`
        // flag AND the browser's permission grant. `showCriticalAlert`
        // no-ops cleanly if permission is not granted or the
        // Notifications API is unavailable.
        if (enabled && isNotificationSupported()) {
          try {
            showCriticalAlert({
              name: alert.name,
              message: alert.message,
              severity: alert.severity,
            })
          } catch {
            // Silent fail — the in-app panel is the source of truth.
          }
        }
      },
      [enabled],
    ),
  })

  const acknowledge = useCallback((alertId: string) => {
    setAlerts((prev) => {
      const found = prev.some((a) => a.alert_id === alertId)
      if (!found) return prev
      if (found) setUnreadCount((p) => Math.max(0, p - 1))
      return prev.filter((a) => a.alert_id !== alertId)
    })
  }, [])

  const acknowledgeAll = useCallback(() => {
    setAlerts([])
    setUnreadCount(0)
  }, [])

  const toggle = useCallback(() => setEnabled((e) => !e), [])

  return {
    alerts,
    unreadCount,
    enabled,
    isConnected,
    acknowledge,
    acknowledgeAll,
    toggle,
  }
}

export default useAlertNotifications
