// components/OrdersPanel.test.tsx — Working-orders table rendering, actions,
// and W15-5 realtime migration (useRealtimeData + Live/Polling badge).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import OrdersPanel from './OrdersPanel'
import { Order } from '@/hooks/useBot'

// W15-5 — MockWebSocket stub. Same pattern as PositionsPanel.test.tsx.
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

  close() {
    this.readyState = MockWebSocket.CLOSED
    this.onclose?.()
  }

  send() {}
}

const sampleOrders: Order[] = [
  {
    order_id: 'ord-1',
    token_id: 'tok-1',
    slug: 'bitcoin-100k-rally',
    side: 'BUY',
    price: 0.42,
    size: 25,
    size_matched: 5,
    strategy: 'mm_avellaneda_stoikov',
    paper: true,
    created_at: Date.now() / 1000 - 60, // 1 min ago
  },
  {
    order_id: 'ord-2',
    token_id: 'tok-2',
    slug: 'ethereum-merge-success',
    side: 'SELL',
    price: 0.58,
    size: 30,
    size_matched: 0,
    strategy: 'arb_binary_dutch_book',
    paper: true,
    created_at: Date.now() / 1000 - 30, // 30s ago
  },
]

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('OrdersPanel', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
    // Default fetch mock — empty orders list. Tests that depend on a
    // specific REST response override this with mockResolvedValueOnce.
    global.fetch = vi.fn().mockResolvedValue(jsonOk({ orders: [] })) as unknown as typeof fetch
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
  })

  // ─────────────────────────────────────────────────────────────────────
  // Rendering & actions
  // ─────────────────────────────────────────────────────────────────────

  it('renders the header with the working-orders count', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    expect(screen.getByText(/Working Orders \(2\)/)).toBeInTheDocument()
  })

  it('renders the empty-state placeholder when there are no orders', async () => {
    render(<OrdersPanel orders={[]} onCancel={vi.fn()} />)
    expect(await screen.findByText('No working limit orders')).toBeInTheDocument()
  })

  it('renders the Cancel All button when orders exist + onCancelAll provided', () => {
    const onCancelAll = vi.fn()
    render(
      <OrdersPanel orders={sampleOrders} onCancel={vi.fn()} onCancelAll={onCancelAll} />,
    )
    expect(screen.getByRole('button', { name: /Cancel all working orders/i })).toBeInTheDocument()
  })

  it('does NOT render the Cancel All button when no onCancelAll handler is provided', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    expect(screen.queryByRole('button', { name: /Cancel all working orders/i })).not.toBeInTheDocument()
  })

  it('renders a Cancel button per order', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    const cancelBtns = screen.getAllByRole('button', { name: /Cancel order ord-/i })
    expect(cancelBtns).toHaveLength(2)
  })

  it('calls onCancel with the correct order id when Cancel is clicked', async () => {
    const user = userEvent.setup()
    const onCancel = vi.fn()
    render(<OrdersPanel orders={sampleOrders} onCancel={onCancel} />)
    const cancelBtns = screen.getAllByRole('button', { name: /Cancel order ord-/i })
    await user.click(cancelBtns[0]) // ord-1
    expect(onCancel).toHaveBeenCalledWith('ord-1')
    await user.click(cancelBtns[1]) // ord-2
    expect(onCancel).toHaveBeenCalledWith('ord-2')
    expect(onCancel).toHaveBeenCalledTimes(2)
  })

  it('calls onCancelAll when the Cancel All button is clicked', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn()
    render(
      <OrdersPanel orders={sampleOrders} onCancel={vi.fn()} onCancelAll={onCancelAll} />,
    )
    const cancelAllBtn = screen.getByRole('button', { name: /Cancel all working orders/i })
    await user.click(cancelAllBtn)
    expect(onCancelAll).toHaveBeenCalledTimes(1)
  })

  it('renders the BUY / SELL side badge correctly per order', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    // Each row renders a side badge; both BUY and SELL should appear.
    expect(screen.getByText('BUY')).toBeInTheDocument()
    expect(screen.getByText('SELL')).toBeInTheDocument()
  })

  it('renders the strategy tag per order', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    expect(screen.getByText('mm_avellaneda_stoikov')).toBeInTheDocument()
    expect(screen.getByText('arb_binary_dutch_book')).toBeInTheDocument()
  })

  it('renders the fill progress bar only when matched > 0', () => {
    const { container } = render(
      <OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />,
    )
    // ord-1 has size_matched=5 → progress bar.
    // ord-2 has size_matched=0 → no progress bar.
    const progressBars = container.querySelectorAll('.bg-green-400.h-full.rounded-full')
    expect(progressBars).toHaveLength(1)
  })

  it('renders the open capital exposure KPI when orders exist', () => {
    render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
    // Open Capital = sum(price * (size - matched)).
    // ord-1: 0.42 * 20 = 8.40
    // ord-2: 0.58 * 30 = 17.40
    // Total: 25.80
    expect(screen.getByText(/\$25\.80/)).toBeInTheDocument()
  })

  // ─────────────────────────────────────────────────────────────────────
  // W15-5 — Realtime migration tests
  // ─────────────────────────────────────────────────────────────────────

  describe('W15-5: realtime migration', () => {
    it('renders the "Polling" badge by default (WS not yet open)', () => {
      render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('flips to the "Live" badge when the WS connects', async () => {
      render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      ws.triggerOpen()
      expect(await screen.findByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('honours an explicit `isRealtime` prop override (true → Live)', () => {
      render(
        <OrdersPanel orders={sampleOrders} onCancel={vi.fn()} isRealtime />,
      )
      expect(screen.getByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('honours an explicit `isRealtime` prop override (false → Polling)', () => {
      render(
        <OrdersPanel
          orders={sampleOrders}
          onCancel={vi.fn()}
          isRealtime={false}
        />,
      )
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      MockWebSocket.instances[0]?.triggerOpen()
      // Explicit false wins over WS-open.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('renders data pushed over the WS `orders` channel (no override)', async () => {
      render(<OrdersPanel onCancel={vi.fn()} />)
      // Initially empty (REST returned []).
      expect(await screen.findByText('No working limit orders')).toBeInTheDocument()
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'orders',
          data: { orders: sampleOrders },
        })
      })
      // After the WS push, the two sample orders should render.
      await screen.findByText(/Working Orders \(2\)/, {}, { timeout: 3000 })
      expect(screen.getByText('BUY')).toBeInTheDocument()
      expect(screen.getByText('SELL')).toBeInTheDocument()
    })

    it('ignores WS messages on channels it did not subscribe to', async () => {
      render(<OrdersPanel onCancel={vi.fn()} />)
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      // Push on the wrong channel — data should be unchanged.
      await act(async () => {
        ws.triggerMessage({
          channel: 'positions',
          data: { positions: [] },
        })
      })
      expect(await screen.findByText('No working limit orders')).toBeInTheDocument()
    })

    it('falls back to the REST response when no `orders` override is passed', async () => {
      vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ orders: [sampleOrders[0]] }))
      render(<OrdersPanel onCancel={vi.fn()} />)
      await screen.findByText(/Working Orders \(1\)/)
      expect(screen.getByText('BUY')).toBeInTheDocument()
    })

    it('renders the loading state before the initial REST fetch resolves (no override)', () => {
      vi.mocked(fetch).mockImplementation(
        () => new Promise<Response>(() => {}),
      )
      render(<OrdersPanel onCancel={vi.fn()} />)
      expect(screen.getByText(/Loading working orders/)).toBeInTheDocument()
    })

    it('does NOT render the loading state when an `orders` override is provided', () => {
      vi.mocked(fetch).mockImplementation(
        () => new Promise<Response>(() => {}),
      )
      render(<OrdersPanel orders={sampleOrders} onCancel={vi.fn()} />)
      expect(screen.queryByText(/Loading working orders/)).not.toBeInTheDocument()
      expect(screen.getByText(/Working Orders \(2\)/)).toBeInTheDocument()
    })
  })
})
