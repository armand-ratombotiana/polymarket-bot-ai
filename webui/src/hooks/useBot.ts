// hooks/useBot.ts
// Central hook — connects to the bot API WebSocket with automatic REST fallback.
'use client'

import { useEffect, useRef, useState, useCallback } from 'react'
import { getApiUrl, getWsUrl } from '@/lib/api'

export interface OrderBook {
  token_id: string
  slug: string
  best_bid: number | null
  best_ask: number | null
  mid: number | null
  spread: number | null
  updated_at: number
}

export interface Order {
  order_id: string
  token_id: string
  slug: string
  side: 'BUY' | 'SELL'
  price: number
  size: number
  size_matched: number
  strategy: string
  paper: boolean
  created_at: number
}

export interface Position {
  token_id: string
  slug: string
  yes_shares: number
  avg_entry_price: number
  total_invested: number
  realised_pnl: number
}

export interface Trade {
  trade_id: string
  slug: string
  side: 'BUY' | 'SELL'
  price: number
  size: number
  pnl: number
  strategy: string
  paper: boolean
  timestamp: number
}

export interface BotSnapshot {
  type: string
  timestamp: number
  mode: 'paper' | 'live'
  kill_switch: boolean
  daily_pnl: number
  paper_balance: number | null
  strategies: string[]
  order_books: OrderBook[]
  open_orders: Order[]
  positions: Position[]
  recent_trades: Trade[]
  events: string[]
}

const DEFAULT_SNAPSHOT: BotSnapshot = {
  type: 'snapshot',
  timestamp: 0,
  mode: 'paper',
  kill_switch: false,
  daily_pnl: 0,
  paper_balance: null,
  strategies: [],
  order_books: [],
  open_orders: [],
  positions: [],
  recent_trades: [],
  events: [],
}

export type ConnectionStatus = 'connecting' | 'connected' | 'disconnected' | 'error'

export function useBot() {
  const [snapshot, setSnapshot] = useState<BotSnapshot>(DEFAULT_SNAPSHOT)
  const [status, setStatus] = useState<ConnectionStatus>('connecting')
  const wsRef = useRef<WebSocket | null>(null)
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const restPollRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const isWsConnectedRef = useRef(false)

  // Direct REST fetch to populate data immediately or on fallback
  const fetchRestSnapshot = useCallback(async () => {
    const apiUrl = getApiUrl()
    try {
      const res = await fetch(`${apiUrl}/api/snapshot`)
      if (res.ok) {
        const data = await res.json()
        setSnapshot(data)
        if (!isWsConnectedRef.current) {
          setStatus('connected')
        }
        return true
      }
    } catch {
      // If /api/snapshot is not yet available, fallback to composite fetch
      try {
        const [booksRes, statusRes, eventsRes, ordersRes, posRes, tradesRes] = await Promise.all([
          fetch(`${apiUrl}/api/orderbooks`).catch(() => null),
          fetch(`${apiUrl}/api/status`).catch(() => null),
          fetch(`${apiUrl}/api/events`).catch(() => null),
          fetch(`${apiUrl}/api/orders`).catch(() => null),
          fetch(`${apiUrl}/api/positions`).catch(() => null),
          fetch(`${apiUrl}/api/trades`).catch(() => null),
        ])

        const booksData = booksRes?.ok ? await booksRes.json() : { order_books: [] }
        const statusData = statusRes?.ok ? await statusRes.json() : {}
        const eventsData = eventsRes?.ok ? await eventsRes.json() : { events: [] }
        const ordersData = ordersRes?.ok ? await ordersRes.json() : { orders: [] }
        const posData = posRes?.ok ? await posRes.json() : { positions: [] }
        const tradesData = tradesRes?.ok ? await tradesRes.json() : { trades: [] }

        setSnapshot({
          type: 'snapshot',
          timestamp: Date.now() / 1000,
          mode: statusData.mode || 'paper',
          kill_switch: Boolean(statusData.kill_switch),
          daily_pnl: statusData.daily_pnl || 0,
          paper_balance: statusData.paper_balance ?? 10000,
          strategies: statusData.strategies || [],
          order_books: booksData.order_books || [],
          open_orders: ordersData.orders || [],
          positions: posData.positions || [],
          recent_trades: tradesData.trades || [],
          events: eventsData.events || [],
        })
        if (!isWsConnectedRef.current) {
          setStatus('connected')
        }
        return true
      } catch (err) {
        if (!isWsConnectedRef.current) {
          setStatus('disconnected')
        }
      }
    }
    return false
  }, [])

  const connect = useCallback(() => {
    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return
    }

    const wsUrl = getWsUrl()
    try {
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        isWsConnectedRef.current = true
        setStatus('connected')
      }

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data) as BotSnapshot
          if (data && data.order_books) {
            setSnapshot(data)
          }
        } catch {}
      }

      ws.onerror = () => {
        isWsConnectedRef.current = false
        // Let fallback polling handle data
      }

      ws.onclose = () => {
        isWsConnectedRef.current = false
        if (retryRef.current) clearTimeout(retryRef.current)
        retryRef.current = setTimeout(() => {
          connect()
        }, 3000)
      }
    } catch {
      isWsConnectedRef.current = false
    }
  }, [])

  useEffect(() => {
    // 1. Fetch data immediately on mount via REST
    fetchRestSnapshot()

    // 2. Start WebSocket connection
    connect()

    // 3. Keep REST polling every 2s as a reliable fallback/refresh
    restPollRef.current = setInterval(() => {
      if (!isWsConnectedRef.current) {
        fetchRestSnapshot()
      }
    }, 2000)

    return () => {
      if (wsRef.current) {
        wsRef.current.close()
      }
      if (retryRef.current) clearTimeout(retryRef.current)
      if (restPollRef.current) clearInterval(restPollRef.current)
    }
  }, [connect, fetchRestSnapshot])

  // Actions
  const activateKillSwitch = async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/kill-switch/activate`, { method: 'POST' }).catch(() => {})
    fetchRestSnapshot()
  }

  const deactivateKillSwitch = async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/kill-switch/deactivate`, { method: 'POST' }).catch(() => {})
    fetchRestSnapshot()
  }

  const cancelAllOrders = async () => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/orders`, { method: 'DELETE' }).catch(() => {})
    fetchRestSnapshot()
  }

  const cancelOrder = async (orderId: string) => {
    const apiUrl = getApiUrl()
    await fetch(`${apiUrl}/api/orders/${orderId}`, { method: 'DELETE' }).catch(() => {})
    fetchRestSnapshot()
  }

  return {
    snapshot,
    status,
    activateKillSwitch,
    deactivateKillSwitch,
    cancelAllOrders,
    cancelOrder,
  }
}
