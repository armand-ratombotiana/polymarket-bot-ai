// components/ConnectionStatus.tsx — Compact WebSocket / polling health pill.
//
// W15-5 — Real-time panels (PositionsPanel / OrdersPanel / AnalyticsPanel)
// were migrated to `useRealtimeData`, which transparently falls back to
// REST polling when the WebSocket is down. This component surfaces the
// underlying transport state in the TopStatusBar so the trader can see at
// a glance whether live pushes are flowing (green dot) or whether the UI
// is relying on the polling fallback (amber dot).
//
// Design contract:
//   - Green dot + "WS Live" label when the WebSocket is open.
//   - Amber dot + "Polling" label when the WS is not connected (still
//     handshaking, mid-reconnect, or permanently failed).
//   - Red dot + "Error" label when the WS reported an `onerror` event.
//     We don't tear down the socket on `onerror` — the browser fires
//     `onclose` shortly after, which useWebSocket already routes into
//     the amber polling state via its reconnect logic. The red state
//     therefore surfaces only briefly before reconnect kicks in.
//
// The dot is wrapped in a Radix Tooltip so a hover/focus reveals the
// full state detail without consuming header real estate.
'use client'

import { useState, useCallback } from 'react'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'
import { useWebSocket } from '@/hooks/useWebSocket'

export type TransportState = 'live' | 'polling' | 'error'

export interface ConnectionStatusProps {
  /** Optional className override for the pill wrapper. */
  className?: string
  /** Optional compact mode — hides the label, shows only the dot. */
  compact?: boolean
}

export function ConnectionStatus({ className, compact = false }: ConnectionStatusProps) {
  // `hasErrored` flips true when `onerror` fires. It's reset back to false
  // on the next `onConnect` (the WS successfully re-established). We use a
  // local state instead of deriving from `isConnected` because the
  // browser's WS lifecycle emits `onerror` → `onclose` → (reconnect) →
  // `onopen`, and we want to surface the error mid-cycle rather than mask
  // it as "polling".
  const [hasErrored, setHasErrored] = useState(false)

  const { isConnected } = useWebSocket({
    onConnect: useCallback(() => setHasErrored(false), []),
    onError: useCallback(() => setHasErrored(true), []),
  })

  const state: TransportState = hasErrored ? 'error' : isConnected ? 'live' : 'polling'

  const dotClass =
    state === 'live'
      ? 'bg-green-400 shadow-sm shadow-green-500/50 animate-pulse'
      : state === 'error'
      ? 'bg-red-400 shadow-sm shadow-red-500/50'
      : 'bg-amber-400 shadow-sm shadow-amber-500/50 animate-pulse'

  const label = state === 'live' ? 'WS Live' : state === 'error' ? 'WS Error' : 'Polling'
  const tip =
    state === 'live'
      ? 'WebSocket connected — real-time pushes are active.'
      : state === 'error'
      ? 'WebSocket reported an error. Falling back to REST polling; reconnect in progress.'
      : 'WebSocket not connected. Live data is being refreshed via REST polling.'

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`Connection status: ${label}`}
          className={
            'flex items-center gap-1.5 bg-[#0e1015] border border-[#1f2335] px-2 py-1 rounded-md text-[11px] whitespace-nowrap transition-colors hover:border-[#2d3450] ' +
            (className ?? '')
          }
        >
          <span
            className={`w-2 h-2 rounded-full ${dotClass}`}
            aria-hidden="true"
          />
          {!compact && (
            <span
              className={`mono font-semibold ${
                state === 'live' ? 'text-green-400' : state === 'error' ? 'text-red-400' : 'text-amber-300'
              }`}
            >
              {label}
            </span>
          )}
        </button>
      </TooltipTrigger>
      <TooltipContent side="bottom" className="max-w-xs">
        <p className="text-xs leading-relaxed">{tip}</p>
      </TooltipContent>
    </Tooltip>
  )
}

export default ConnectionStatus
