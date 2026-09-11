// components/AlertNotificationsPanel.tsx — W23-4 Real-time alert bell.
//
// Composes the `useAlertNotifications` hook with a Radix Popover to
// surface the live alert feed in the TopStatusBar. The trigger is a
// bell icon button with a numeric unread-count badge; the dropdown
// panel lists recent alerts (most-recent first), colour-coded by
// severity, with per-item acknowledge + a bulk "Acknowledge All"
// action. A "Live" pill in the panel header reflects the WebSocket
// transport state so the trader knows whether the feed is actively
// pushing (green dot) or whether the hook's last open is stale
// (amber dot).
//
// Visual language mirrors the rest of the workstation's dark theme
// (#0e1015 panel surface, #1f2335 borders, #dde1ed primary text,
// #7e8aaa secondary text) so the panel slots in next to the existing
// status-bar pills without visual clash. Severity colours follow the
// W13-6 conventions already used by `showCriticalAlert`:
//   critical = red, error = orange, warning = amber, info = blue.
//
// Accessibility:
//   * The bell trigger has an `aria-label` that includes the unread
//     count so screen readers announce "Alerts, 3 unread".
//   * The dropdown is a Radix Popover (`role="dialog"`) so ESC +
//     outside-click dismiss it by default.
//   * Each alert row is a `<button>` (not a `<div>`) so it's
//     keyboard-focusable; clicking or pressing Enter acknowledges.
//   * The "Acknowledge All" button is disabled when the list is
//     empty so the user can't trigger a no-op.
'use client'

import { useState } from 'react'
import { Popover, PopoverTrigger, PopoverContent } from '@/components/ui/popover'
import { Button } from '@/components/ui/button'
import { useAlertNotifications, type Alert } from '@/hooks/useAlertNotifications'

// Severity → (icon, label colour, dot colour) map. The icon is
// rendered inline with the alert name; the dot sits at the left
// edge of each row so the trader can scan severity at a glance.
const SEVERITY_META: Record<
  Alert['severity'],
  { icon: string; text: string; dot: string; ring: string }
> = {
  critical: {
    icon: '🚨',
    text: 'text-red-400',
    dot: 'bg-red-400',
    ring: 'border-l-red-500',
  },
  error: {
    icon: '❌',
    text: 'text-orange-400',
    dot: 'bg-orange-400',
    ring: 'border-l-orange-500',
  },
  warning: {
    icon: '⚠️',
    text: 'text-amber-300',
    dot: 'bg-amber-400',
    ring: 'border-l-amber-500',
  },
  info: {
    icon: 'ℹ️',
    text: 'text-blue-400',
    dot: 'bg-blue-400',
    ring: 'border-l-blue-500',
  },
}

function fmtRelative(ts: number): string {
  if (!ts || typeof ts !== 'number') return ''
  const now = Date.now()
  const diff = Math.max(0, now - ts)
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`
  return `${Math.floor(diff / 86_400_000)}d ago`
}

export interface AlertNotificationsPanelProps {
  /** Optional className override for the trigger button wrapper. */
  className?: string
}

export function AlertNotificationsPanel({ className }: AlertNotificationsPanelProps) {
  const { alerts, unreadCount, enabled, isConnected, acknowledge, acknowledgeAll, toggle } =
    useAlertNotifications()
  const [open, setOpen] = useState(false)

  const triggerLabel = `Alerts${unreadCount > 0 ? `, ${unreadCount} unread` : ''}`

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={triggerLabel}
          title="Real-time alert feed"
          className={
            'relative btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white ' +
            (className ?? '')
          }
          aria-haspopup="dialog"
          aria-expanded={open}
        >
          {/* Bell icon — inline SVG so it inherits the parent's currentColor
              and stays crisp at 16px without an extra icon font dependency. */}
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="M8 1.5a.75.75 0 0 1 .75.75v.55a4.5 4.5 0 0 1 3.75 4.43V8.5l1.2 2.1a.6.6 0 0 1-.52.9H2.82a.6.6 0 0 1-.52-.9L3.5 8.5V7.23A4.5 4.5 0 0 1 7.25 2.8v-.55A.75.75 0 0 1 8 1.5Z"
              fill="currentColor"
              opacity="0.9"
            />
            <path
              d="M6.5 12.5a1.5 1.5 0 0 0 3 0h-3Z"
              fill="currentColor"
            />
          </svg>
          {/* Unread count badge — only rendered when there's at least one
              unread alert. Sits at the top-right of the bell, slightly
              outside the button bounds so it doesn't get clipped by
              overflow rules. */}
          {unreadCount > 0 && (
            <span
              className="absolute -top-0.5 -right-0.5 min-w-[15px] h-[15px] px-1 flex items-center justify-center text-[9px] font-bold rounded-full bg-red-500 text-white shadow-sm shadow-red-500/40"
              aria-hidden="true"
              data-testid="unread-badge"
            >
              {unreadCount > 99 ? '99+' : unreadCount}
            </span>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        sideOffset={6}
        className="w-[360px] max-w-[calc(100vw-1.5rem)] p-0 bg-[#13161e] border border-[#1f2335] text-[#dde1ed] shadow-xl"
      >
        {/* Header — title + Live indicator + mute toggle */}
        <div className="flex items-center justify-between px-3 py-2 border-b border-[#1f2335]">
          <div className="flex items-center gap-2">
            <span className="text-xs font-bold uppercase tracking-wider text-[#dde1ed]">
              Alerts
            </span>
            {/* Live indicator — reflects the underlying WebSocket transport.
                Green dot + "Live" when connected; amber dot + "Polling"
                when the WS is mid-handshake or down. Matches the visual
                language of ConnectionStatusPill. */}
            <span
              className="flex items-center gap-1 text-[10px] font-semibold px-1.5 py-0.5 rounded bg-[#0e1015] border border-[#1f2335]"
              title={
                isConnected
                  ? 'WebSocket connected — real-time pushes are active.'
                  : 'WebSocket not connected — feed will catch up on reconnect.'
              }
              data-testid="live-indicator"
            >
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  isConnected
                    ? 'bg-green-400 animate-pulse shadow-sm shadow-green-500/50'
                    : 'bg-amber-400'
                }`}
                aria-hidden="true"
              />
              <span
                className={isConnected ? 'text-green-400' : 'text-amber-300'}
              >
                {isConnected ? 'Live' : 'Polling'}
              </span>
            </span>
          </div>
          {/* Mute toggle — flips the local `enabled` flag in the hook.
              When off, the hook stops firing desktop toasts but the
              in-app feed keeps recording alerts so the trader can
              still see them in the panel. */}
          <button
            type="button"
            onClick={toggle}
            className="text-[10px] font-semibold text-[#7e8aaa] hover:text-[#dde1ed] px-1.5 py-0.5 rounded hover:bg-[#1f2335]"
            title={
              enabled
                ? 'Desktop notifications enabled — click to mute'
                : 'Desktop notifications muted — click to enable'
            }
            aria-pressed={enabled}
            aria-label={
              enabled ? 'Mute desktop alert notifications' : 'Enable desktop alert notifications'
            }
          >
            {enabled ? '🔔' : '🔕'}
          </button>
        </div>

        {/* Body — alerts list OR empty state. Capped at 360px height with
            overflow scroll so the panel doesn't grow taller than the
            viewport on long feeds. Custom scrollbar styling matches the
            dark theme. */}
        <div
          className="max-h-[360px] overflow-y-auto"
          style={{ scrollbarWidth: 'thin' }}
        >
          {alerts.length === 0 ? (
            <div
              className="flex flex-col items-center justify-center py-8 px-4 text-center"
              data-testid="empty-state"
            >
              <span className="text-2xl mb-2" aria-hidden="true">
                🔔
              </span>
              <p className="text-xs text-[#7e8aaa]">
                No active alerts. New alerts will appear here in real time.
              </p>
            </div>
          ) : (
            <ul role="list" className="divide-y divide-[#1f2335]">
              {alerts.map((alert) => {
                const meta = SEVERITY_META[alert.severity] ?? SEVERITY_META.info
                return (
                  <li
                    key={alert.alert_id}
                    className={`border-l-2 ${meta.ring}`}
                  >
                    <button
                      type="button"
                      onClick={() => acknowledge(alert.alert_id)}
                      className="w-full text-left px-3 py-2 hover:bg-[#1a1e2c] focus:bg-[#1a1e2c] focus:outline-none transition-colors"
                      aria-label={`Acknowledge alert: ${alert.name}`}
                      title="Click to acknowledge"
                    >
                      <div className="flex items-start gap-2">
                        <span
                          className={`mt-1 w-2 h-2 rounded-full shrink-0 ${meta.dot}`}
                          aria-hidden="true"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-baseline justify-between gap-2">
                            <span
                              className={`text-xs font-semibold truncate ${meta.text}`}
                            >
                              <span aria-hidden="true" className="mr-1">
                                {meta.icon}
                              </span>
                              {alert.name}
                            </span>
                            <span className="text-[10px] text-[#7e8aaa] shrink-0 mono">
                              {fmtRelative(alert.timestamp)}
                            </span>
                          </div>
                          <p className="text-[11px] text-[#a8adc2] mt-0.5 line-clamp-2 break-words">
                            {alert.message}
                          </p>
                          <div className="flex items-center justify-between mt-1">
                            <span
                              className={`text-[9px] font-bold uppercase tracking-wider ${meta.text}`}
                            >
                              {alert.severity}
                            </span>
                            <span className="text-[9px] text-[#5a627a] hover:text-[#dde1ed]">
                              click to ack →
                            </span>
                          </div>
                        </div>
                      </div>
                    </button>
                  </li>
                )
              })}
            </ul>
          )}
        </div>

        {/* Footer — alert count + Acknowledge All. The bulk action is
            disabled when the list is empty so the user can't trigger
            a no-op. */}
        {alerts.length > 0 && (
          <div className="flex items-center justify-between px-3 py-2 border-t border-[#1f2335] bg-[#0e1015]">
            <span className="text-[10px] text-[#7e8aaa]">
              {alerts.length} active {alerts.length === 1 ? 'alert' : 'alerts'}
              {unreadCount > 0 && (
                <span className="text-red-400 font-semibold"> · {unreadCount} unread</span>
              )}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={acknowledgeAll}
              className="h-7 px-2 text-[10px] font-semibold text-[#dde1ed] hover:bg-[#1f2335]"
              aria-label="Acknowledge all alerts"
            >
              ✓ Acknowledge All
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

export default AlertNotificationsPanel
