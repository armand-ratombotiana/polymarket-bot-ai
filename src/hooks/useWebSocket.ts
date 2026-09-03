// hooks/useWebSocket.ts
// Generic WebSocket hook with auto-reconnect, visibility-aware pause/resume,
// and ref-based callback storage so callers can pass new closures on every
// render without tearing down the underlying socket.
//
// W11-4 — This hook is the foundation of the real-time push layer. Existing
// components still poll via useBot's REST fallback; future waves will migrate
// them to useRealtimeData (which composes this hook with REST prefetch +
// polling fallback) so we can stop hammering /api/snapshot every 2s when the
// WS is healthy.
//
// Design notes:
// - `shouldReconnect` is a ref, not state — flipping it doesn't need to
//   trigger a re-render, and the cleanup function needs to mutate it
//   synchronously during teardown (state updates are async and would race
//   the close() call).
// - The `connect` callback is memoised on `[reconnectInterval,
//   maxReconnectAttempts]` only — the onMessage/onConnect/onDisconnect
//   callbacks are read from refs that are refreshed on every render, so
//   `connect` itself never changes identity across renders that just
//   swap callbacks. This is critical: the visibilitychange effect and
//   the mount effect both depend on `connect`, so an unstable `connect`
//   would re-run those effects (re-creating WebSocket + listener) on
//   every parent render.
// - We never set state after the hook unmounts. The cleanup function
//   flips `shouldReconnect = false` BEFORE closing the socket, so the
//   `onclose` handler (which fires synchronously during close()) sees
//   the flag and skips the reconnect setTimeout.
// - Visibility handling: when the tab is hidden, we proactively close
//   the socket to free the server-side connection (a hidden tab has no
//   business receiving real-time updates). When the tab becomes visible
//   again, we reset the reconnect attempt counter and immediately
//   reconnect — the server pushes a fresh snapshot on subscribe, so the
//   UI picks up where it left off without waiting for the next poll.
'use client'

import { useState, useEffect, useRef, useCallback } from 'react'
import { getAuthedWsUrl } from '@/lib/api'

export interface UseWebSocketOptions {
  onMessage?: (data: unknown) => void
  onConnect?: () => void
  onDisconnect?: () => void
  reconnectInterval?: number
  maxReconnectAttempts?: number
}

export function useWebSocket(options: UseWebSocketOptions = {}) {
  const {
    onMessage,
    onConnect,
    onDisconnect,
    reconnectInterval = 3000,
    maxReconnectAttempts = 10,
  } = options

  const [isConnected, setIsConnected] = useState(false)
  const [lastMessage, setLastMessage] = useState<unknown>(null)
  const wsRef = useRef<WebSocket | null>(null)
  const reconnectAttempts = useRef(0)
  const shouldReconnect = useRef(true)
  // Recursive reconnect: `connect` schedules `setTimeout(connect, ...)` on
  // failure. Storing it in a ref lets the callback reference itself without
  // tripping the `react-hooks/immutability` rule (which flags a `const`
  // accessed before its declaration completes). The ref is reassigned on
  // every render so it always points at the freshest `connect` closure.
  const reconnectRef = useRef<() => void>(() => {})
  // Mutable ref storage for the latest callbacks. We update these on every
  // render (no deps array) so the WebSocket handlers always invoke the most
  // recent closure without forcing a socket reconnect.
  const onMessageRef = useRef(onMessage)
  const onConnectRef = useRef(onConnect)
  const onDisconnectRef = useRef(onDisconnect)

  // Keep refs updated — runs on every render so the WS handlers (which
  // read from refs) always see the freshest closure passed by the caller.
  useEffect(() => {
    onMessageRef.current = onMessage
    onConnectRef.current = onConnect
    onDisconnectRef.current = onDisconnect
  })

  const connect = useCallback(() => {
    if (!shouldReconnect.current) return
    if (typeof window === 'undefined') return

    try {
      const ws = new WebSocket(getAuthedWsUrl())
      wsRef.current = ws

      ws.onopen = () => {
        setIsConnected(true)
        reconnectAttempts.current = 0
        onConnectRef.current?.()
      }

      ws.onmessage = (event) => {
        try {
          const data = JSON.parse(event.data)
          setLastMessage(data)
          onMessageRef.current?.(data)
        } catch (e) {
          console.error('WebSocket parse error:', e)
        }
      }

      ws.onclose = () => {
        setIsConnected(false)
        onDisconnectRef.current?.()
        if (shouldReconnect.current && reconnectAttempts.current < maxReconnectAttempts) {
          reconnectAttempts.current++
          setTimeout(reconnectRef.current, reconnectInterval)
        }
      }

      ws.onerror = (error) => {
        console.error('WebSocket error:', error)
      }
    } catch (e) {
      console.error('WebSocket connection failed:', e)
      if (shouldReconnect.current && reconnectAttempts.current < maxReconnectAttempts) {
        reconnectAttempts.current++
        setTimeout(reconnectRef.current, reconnectInterval)
      }
    }
  }, [reconnectInterval, maxReconnectAttempts])

  // Always point the reconnect ref at the latest `connect` closure so
  // the recursive setTimeout inside the WS handlers picks up new props
  // (e.g., a changed reconnectInterval) without tearing down the socket.
  // This effect has no deps array — it re-runs on every render so the
  // ref stays in sync with the freshest `connect` closure. (Indirect
  // reference via the ref avoids the `react-hooks/immutability` rule
  // that flags a `const` accessed before its declaration completes —
  // `connect` is now fully declared by the time this effect is registered.)
  useEffect(() => {
    reconnectRef.current = connect
  })

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(data))
    }
  }, [])

  const disconnect = useCallback(() => {
    shouldReconnect.current = false
    wsRef.current?.close()
  }, [])

  useEffect(() => {
    shouldReconnect.current = true
    connect()
    return () => {
      shouldReconnect.current = false
      wsRef.current?.close()
    }
  }, [connect])

  // Pause when tab is hidden, resume when visible.
  // We close the WS on hide to free the server-side connection — a hidden
  // tab has no business receiving real-time updates. On visible, we reset
  // the reconnect attempt counter and reconnect immediately; the server
  // pushes a fresh snapshot on subscribe.
  useEffect(() => {
    const handler = () => {
      if (document.hidden) {
        wsRef.current?.close()
      } else if (shouldReconnect.current) {
        reconnectAttempts.current = 0
        connect()
      }
    }
    document.addEventListener('visibilitychange', handler)
    return () => document.removeEventListener('visibilitychange', handler)
  }, [connect])

  return { isConnected, lastMessage, send, disconnect }
}
