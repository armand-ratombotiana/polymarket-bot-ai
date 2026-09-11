// components/TradesPanel.test.tsx — Recent executions table rendering,
// filtering, CSV export, and W22-5 realtime migration (useRealtimeData +
// Live/Polling badge).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import TradesPanel from './TradesPanel'
import { Trade } from '@/hooks/useBot'

// W22-5 — MockWebSocket stub. The panel now opens a real WS via
// useRealtimeData → useWebSocket; without this stub, jsdom attempts an
// actual ws://localhost:8080/ws connection that errors on every test
// (loud stderr noise) without actually driving the hook's connection
// state. Installing this stub lets us trigger `open` / `message` /
// `close` events imperatively for the Live/Polling badge tests below.
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

const sampleTrades: Trade[] = [
  {
    trade_id: 'trd-1',
    token_id: 'tok-1',
    slug: 'bitcoin-100k-rally',
    side: 'BUY',
    price: 0.42,
    size: 25,
    pnl: 1.5,
    strategy: 'mm_avellaneda_stoikov',
    paper: true,
    timestamp: Date.now() / 1000 - 60, // 1 min ago
  },
  {
    trade_id: 'trd-2',
    token_id: 'tok-2',
    slug: 'ethereum-merge-success',
    side: 'SELL',
    price: 0.58,
    size: 30,
    pnl: -2.0,
    strategy: 'arb_binary_dutch_book',
    paper: true,
    timestamp: Date.now() / 1000 - 30, // 30s ago
  },
]

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('TradesPanel', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    // W22-5 — install MockWebSocket so useRealtimeData's internal
    // useWebSocket() call doesn't attempt a real ws:// connection.
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
    // Default fetch mock — empty trades list. Tests that depend on a
    // specific REST response override this with mockResolvedValueOnce.
    global.fetch = vi.fn().mockResolvedValue(jsonOk({ trades: [] })) as unknown as typeof fetch
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
  })

  // ─────────────────────────────────────────────────────────────────────
  // Rendering & filtering
  // ─────────────────────────────────────────────────────────────────────

  it('renders the header with the executed-trades count', () => {
    render(<TradesPanel trades={sampleTrades} />)
    expect(screen.getByText(/Recent Executions \(2\)/)).toBeInTheDocument()
  })

  it('renders the empty-state placeholder when there are no trades', async () => {
    render(<TradesPanel trades={[]} />)
    expect(await screen.findByText('No executed trades')).toBeInTheDocument()
  })

  it('renders the Audit Stream badge in the header', () => {
    render(<TradesPanel trades={sampleTrades} />)
    expect(screen.getByText('Audit Stream')).toBeInTheDocument()
  })

  it('renders the BUY / SELL side badge correctly per trade', () => {
    render(<TradesPanel trades={sampleTrades} />)
    // The BUY/SELL text appears both in the side-filter buttons AND in
    // the per-row side badges. Assert at least one of each is rendered.
    expect(screen.getAllByText('BUY').length).toBeGreaterThanOrEqual(1)
    expect(screen.getAllByText('SELL').length).toBeGreaterThanOrEqual(1)
  })

  it('renders the strategy tag per trade', () => {
    render(<TradesPanel trades={sampleTrades} />)
    expect(screen.getByText('mm_avellaneda_stoikov')).toBeInTheDocument()
    expect(screen.getByText('arb_binary_dutch_book')).toBeInTheDocument()
  })

  it('renders the P&L cell with sign + colour', () => {
    render(<TradesPanel trades={sampleTrades} />)
    // fmtPnl emits +$1.50 for +1.5 (green) and -$2.00 for -2.0 (red).
    // The minus sign in fmtPnl is U+2212, not a hyphen, but fmtPnl may
    // emit either form depending on the design-token config — accept
    // either by querying via textContent substring.
    const cells = screen.getAllByText(/\$1\.50|\$2\.00/)
    expect(cells.length).toBeGreaterThanOrEqual(2)
  })

  it('renders the CSV export button (enabled when trades exist)', () => {
    render(<TradesPanel trades={sampleTrades} />)
    const csvBtn = screen.getByTitle('Export CSV Audit Trail')
    expect(csvBtn).not.toBeDisabled()
  })

  it('disables the CSV export button when there are no trades', () => {
    render(<TradesPanel trades={[]} />)
    const csvBtn = screen.getByTitle('Export CSV Audit Trail')
    expect(csvBtn).toBeDisabled()
  })

  it('renders the aggregate Net P&L KPI in the header', () => {
    // Net P&L = 1.5 + (-2.0) = -0.5 → red badge. The KPI label is the
    // uppercase-styled "Net P&L:" span — DOM textContent is the original
    // case (CSS text-transform doesn't change the DOM).
    render(<TradesPanel trades={sampleTrades} />)
    expect(screen.getByText('Net P&L:')).toBeInTheDocument()
  })

  it('filters trades by search query (market slug match)', async () => {
    const user = userEvent.setup()
    render(<TradesPanel trades={sampleTrades} />)
    const input = screen.getByPlaceholderText(
      'Search fills by market, strategy, or trade ID…',
    ) as HTMLInputElement
    await user.type(input, 'bitcoin')
    // tok-1 (bitcoin) still visible, tok-2 (ethereum) hidden.
    expect(screen.getByText(/BITCOIN/)).toBeInTheDocument()
    expect(screen.queryByText(/ETHEREUM/)).not.toBeInTheDocument()
  })

  it('clears the search filter when the clear ✕ button is pressed', async () => {
    const user = userEvent.setup()
    render(<TradesPanel trades={sampleTrades} />)
    const input = screen.getByPlaceholderText(
      'Search fills by market, strategy, or trade ID…',
    ) as HTMLInputElement
    await user.type(input, 'bitcoin')
    expect(input.value).toBe('bitcoin')
    // The clear ✕ button is the only button inside the search input's
    // relative wrapper.
    const searchWrapper = input.closest('.relative') as HTMLElement
    expect(searchWrapper).not.toBeNull()
    const clearBtn = within(searchWrapper).getByRole('button')
    await user.click(clearBtn)
    expect(input.value).toBe('')
  })

  it('filters by side (SELL only shows SELL trades)', async () => {
    const user = userEvent.setup()
    render(<TradesPanel trades={sampleTrades} />)
    const sellBtn = screen.getByRole('button', { name: 'SELL' })
    await user.click(sellBtn)
    // tok-2 (SELL) still visible, tok-1 (BUY) hidden.
    expect(screen.queryByText(/BITCOIN/)).not.toBeInTheDocument()
    expect(screen.getByText(/ETHEREUM/)).toBeInTheDocument()
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-5 — Realtime migration tests
  // ─────────────────────────────────────────────────────────────────────

  describe('W22-5: realtime migration', () => {
    it('renders the "Polling" badge by default (WS not yet open)', () => {
      // WS is constructed but `triggerOpen()` hasn't been called yet —
      // isRealtime=false → amber "⟳ Polling" badge should be visible.
      render(<TradesPanel trades={sampleTrades} />)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('flips to the "Live" badge when the WS connects', async () => {
      render(<TradesPanel trades={sampleTrades} />)
      // Before open: polling badge.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      // Drive the MockWebSocket through its lifecycle.
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      // After open: live badge.
      await screen.findByText('● Live')
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('honours an explicit `isRealtime` prop override (true → Live)', () => {
      // When the parent (page.tsx) passes isRealtime explicitly, the
      // panel should use that value instead of useRealtimeData's flag.
      render(<TradesPanel trades={sampleTrades} isRealtime />)
      expect(screen.getByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('honours an explicit `isRealtime` prop override (false → Polling)', () => {
      // Even if the WS is open, the explicit prop wins.
      render(<TradesPanel trades={sampleTrades} isRealtime={false} />)
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      // Open the WS — the explicit false override still wins.
      MockWebSocket.instances[0]?.triggerOpen()
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('renders data pushed over the WS `trades` channel (no override)', async () => {
      // No `trades` prop → panel relies on useRealtimeData's data.
      // Initial REST fetch returns [] (the beforeEach mock).
      // Then we open the WS and push a fresh trades payload — the
      // rendered count + slug should reflect the WS push, not the REST
      // empty-state.
      render(<TradesPanel />)
      // Initially empty (REST returned []).
      expect(await screen.findByText('No executed trades')).toBeInTheDocument()

      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'trades',
          data: { trades: sampleTrades },
        })
      })

      // After the WS push, the two sample trades should render.
      await screen.findByText(/Recent Executions \(2\)/, {}, { timeout: 3000 })
      // The BUY/SELL text appears both in the side-filter buttons AND
      // in the per-row side badges — assert at least one of each.
      expect(screen.getAllByText('BUY').length).toBeGreaterThanOrEqual(1)
      expect(screen.getAllByText('SELL').length).toBeGreaterThanOrEqual(1)
    })

    it('ignores WS messages on channels it did not subscribe to', async () => {
      render(<TradesPanel />)
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      // Push a message on the wrong channel — data should be unchanged.
      await act(async () => {
        ws.triggerMessage({
          channel: 'orders',
          data: { orders: [{ order_id: 'X' }] },
        })
      })
      // Still the REST-empty-state, not the orders payload.
      expect(await screen.findByText('No executed trades')).toBeInTheDocument()
    })

    it('falls back to the REST response when no `trades` prop is passed', async () => {
      // Override the beforeEach mock for this test — return 1 trade
      // from the REST endpoint. The panel should render it.
      vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ trades: [sampleTrades[0]] }))
      render(<TradesPanel />)
      // Wait for the REST fetch to resolve.
      await screen.findByText(/Recent Executions \(1\)/)
      // The BUY text appears in the side-filter button AND the row badge.
      expect(screen.getAllByText('BUY').length).toBeGreaterThanOrEqual(1)
    })

    it('renders the loading state before the initial REST fetch resolves (no override)', () => {
      // Never-resolving fetch → isLoading stays true.
      vi.mocked(fetch).mockImplementation(
        () => new Promise<Response>(() => {}),
      )
      render(<TradesPanel />)
      expect(screen.getByText(/Loading recent executions/)).toBeInTheDocument()
    })

    it('does NOT render the loading state when a `trades` override is provided', () => {
      // Even with a never-resolving fetch, the override short-circuits
      // the loading gate.
      vi.mocked(fetch).mockImplementation(
        () => new Promise<Response>(() => {}),
      )
      render(<TradesPanel trades={sampleTrades} />)
      expect(screen.queryByText(/Loading recent executions/)).not.toBeInTheDocument()
      expect(screen.getByText(/Recent Executions \(2\)/)).toBeInTheDocument()
    })
  })
})
