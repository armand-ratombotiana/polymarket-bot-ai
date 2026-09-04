// components/LeaderboardPanel.test.tsx — Strategy leaderboard rendering
// and W22-5 realtime migration (useRealtimeData + Live/Polling badge).
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act, waitFor, fireEvent } from '@testing-library/react'
import LeaderboardPanel from './LeaderboardPanel'

// W22-5 — MockWebSocket stub. Same pattern as OrdersPanel.test.tsx.
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

const sampleRows = [
  {
    strategy: 'mm_avellaneda_stoikov',
    fills: 12,
    closed_trades: 5,
    net_pnl: 7.5,
    win_rate: 0.8,
    profit_factor: 2.1,
    open_exposure: 10.0,
    max_drawdown: -1.2,
    risk_adjusted_score: 1.85,
  },
  {
    strategy: 'arb_binary_dutch_book',
    fills: 8,
    closed_trades: 3,
    net_pnl: -2.0,
    win_rate: 0.33,
    profit_factor: null,
    open_exposure: 0,
    max_drawdown: -0.8,
    risk_adjusted_score: -0.45,
  },
]

function jsonOk(payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('LeaderboardPanel', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    // W22-5 — install MockWebSocket so useRealtimeData's internal
    // useWebSocket() call doesn't attempt a real ws:// connection.
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
    // Default fetch mock returns a 200 with empty ranked list so
    // useRealtimeData's initial REST fetch resolves cleanly.
    global.fetch = vi.fn().mockResolvedValue(jsonOk({ ranked: [] })) as unknown as typeof fetch
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
  })

  // ─────────────────────────────────────────────────────────────────────
  // Rendering
  // ─────────────────────────────────────────────────────────────────────

  it('renders the loading state before the initial REST fetch resolves', () => {
    // Never-resolving fetch → isLoading stays true. Override the
    // beforeEach mock with mockImplementationOnce so the first fetch
    // call (the initial REST prefetch) never resolves; subsequent
    // polling calls fall back to the default mockResolvedValue.
    vi.mocked(fetch).mockImplementationOnce(
      () => new Promise<Response>(() => {}),
    )
    render(<LeaderboardPanel />)
    expect(screen.getByText(/Loading leaderboard/)).toBeInTheDocument()
  })

  it('renders the empty-state placeholder when there are no rows', async () => {
    render(<LeaderboardPanel />)
    expect(await screen.findByText('No closed trades yet')).toBeInTheDocument()
  })

  it('renders the strategy leaderboard header', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // Wait for a row to render (indicates the REST fetch resolved and
    // the panel transitioned from loading → with-rows state).
    await screen.findByText('mm_avellaneda_stoikov', {}, { timeout: 3000 })
    // Header text is "🏆 Strategy Leaderboard" (emoji prefix). Use
    // getByText against the live DOM after the wait so the matcher
    // runs against the current DOM tree.
    expect(screen.getByText(/Strategy Leaderboard/)).toBeInTheDocument()
  })

  it('renders a row per strategy', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    expect(await screen.findByText('mm_avellaneda_stoikov')).toBeInTheDocument()
    expect(screen.getByText('arb_binary_dutch_book')).toBeInTheDocument()
  })

  it('renders the gold medal for the top-ranked strategy', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // The medal is the medal emoji (🥇) for index 0.
    expect(await screen.findByText('🥇')).toBeInTheDocument()
    expect(screen.getByText('🥈')).toBeInTheDocument()
  })

  it('renders the win rate as a percentage', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov win_rate=0.8 → "80%"
    // arb_binary_dutch_book win_rate=0.33 → "33%"
    expect(await screen.findByText('80%')).toBeInTheDocument()
    expect(screen.getByText('33%')).toBeInTheDocument()
  })

  it('renders the profit factor when not null, otherwise "PF —"', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov profit_factor=2.1 → "PF 2.10"
    expect(await screen.findByText('PF 2.10')).toBeInTheDocument()
    // arb_binary_dutch_book profit_factor=null → "PF —"
    expect(screen.getByText('PF —')).toBeInTheDocument()
  })

  it('renders the closed-trades count with a W suffix', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov closed_trades=5 → "5W"
    // arb_binary_dutch_book closed_trades=3 → "3W"
    expect(await screen.findByText('5W')).toBeInTheDocument()
    expect(screen.getByText('3W')).toBeInTheDocument()
  })

  it('renders the net P&L with sign + colour', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov net_pnl=7.5 → "+$7.50" (green)
    // arb_binary_dutch_book net_pnl=-2.0 → "-$2.00" (red)
    expect(await screen.findByText('+$7.50')).toBeInTheDocument()
    expect(screen.getByText('-$2.00')).toBeInTheDocument()
  })

  it('renders the risk-adjusted score with sign + colour', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov risk_adjusted_score=1.85 → "+1.85" (green)
    // arb_binary_dutch_book risk_adjusted_score=-0.45 → "-0.45" (red)
    expect(await screen.findByText('+1.85')).toBeInTheDocument()
    expect(screen.getByText('-0.45')).toBeInTheDocument()
  })

  it('renders the max drawdown as a dollar value', async () => {
    vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: sampleRows }))
    render(<LeaderboardPanel />)
    // mm_avellaneda_stoikov max_drawdown=-1.2 → "DD $-1.20"
    // arb_binary_dutch_book max_drawdown=-0.8 → "DD $-0.80"
    expect(await screen.findByText('DD $-1.20')).toBeInTheDocument()
    expect(screen.getByText('DD $-0.80')).toBeInTheDocument()
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-5 — Realtime migration tests
  // ─────────────────────────────────────────────────────────────────────

  describe('W22-5: realtime migration', () => {
    it('renders the "Polling" badge by default (WS not yet open)', async () => {
      render(<LeaderboardPanel />)
      // Wait for the initial REST fetch to resolve so the empty-state
      // header (with the Polling badge) is rendered.
      await screen.findByText('No closed trades yet')
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('flips to the "Live" badge when the WS connects', async () => {
      render(<LeaderboardPanel />)
      await screen.findByText('No closed trades yet')
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      expect(await screen.findByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('accepts a metrics-channel WS payload shaped like { ranked: [] }', async () => {
      // The metrics channel pushes the full BotSnapshot by default — the
      // `validate` predicate drops those. When a leaderboard-shaped
      // payload arrives (has `ranked` array), the hook accepts it and
      // the panel renders the new rows.
      render(<LeaderboardPanel />)
      // Initially empty (REST returned []).
      await screen.findByText('No closed trades yet')
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'metrics',
          data: { ranked: sampleRows },
        })
      })
      // After the WS push, the two sample rows should render.
      await screen.findByText('mm_avellaneda_stoikov', {}, { timeout: 3000 })
      expect(screen.getByText('arb_binary_dutch_book')).toBeInTheDocument()
    })

    it('drops a metrics-channel WS payload that does NOT look like { ranked: [] } (BotSnapshot fallback)', async () => {
      // The metrics channel pushes the full BotSnapshot by default. The
      // `validate` predicate must drop those so the typed state isn't
      // clobbered with mismatched fields. After dropping, the panel
      // should still render the initial REST empty-state.
      render(<LeaderboardPanel />)
      await screen.findByText('No closed trades yet')
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      await act(async () => { ws.triggerOpen() })
      await act(async () => {
        ws.triggerMessage({
          channel: 'metrics',
          // A typical BotSnapshot payload — has no `ranked` field.
          data: {
            type: 'snapshot',
            timestamp: Date.now() / 1000,
            mode: 'paper',
            kill_switch: false,
            daily_pnl: 0,
            paper_balance: 100,
          },
        })
      })
      // Still the REST-empty-state, not a clobbered snapshot.
      expect(screen.getByText('No closed trades yet')).toBeInTheDocument()
    })

    it('ignores WS messages on channels it did not subscribe to', async () => {
      render(<LeaderboardPanel />)
      await screen.findByText('No closed trades yet')
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
      // Still the REST-empty-state.
      expect(screen.getByText('No closed trades yet')).toBeInTheDocument()
    })

    it('falls back to the REST response when the WS is not connected', async () => {
      // Override the beforeEach mock for this test — return 1 row
      // from the REST endpoint. The panel should render it.
      vi.mocked(fetch).mockResolvedValueOnce(jsonOk({ ranked: [sampleRows[0]] }))
      render(<LeaderboardPanel />)
      await screen.findByText('mm_avellaneda_stoikov')
      // Still polling because the WS hasn't been triggered open.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
    })
  })

  // ─────────────────────────────────────────────────────────────────────
  // W22-1 — Error-handling tests
  // ─────────────────────────────────────────────────────────────────────
  // Previously the panel silently swallowed fetch errors via `} catch {}`.
  // The W22-1 fix surfaces them via an inline dismissable banner so the
  // trader knows the leaderboard fetch failed.

  describe('W22-1: error handling', () => {
    it('shows the leaderboard error banner when /api/leaderboard returns HTTP 500', async () => {
      vi.mocked(fetch).mockResolvedValueOnce(
        new Response('Internal Server Error', { status: 500 }),
      )
      render(<LeaderboardPanel />)
      await screen.findByText(/Leaderboard:/i)
      expect(
        screen.getByText(/HTTP 500/i),
      ).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: /Dismiss leaderboard error/i }),
      ).toBeInTheDocument()
    })

    it('shows the leaderboard error banner when the fetch throws a network error', async () => {
      vi.mocked(fetch).mockRejectedValueOnce(
        new Error('Network error: ECONNREFUSED'),
      )
      render(<LeaderboardPanel />)
      await screen.findByText(/Network error: ECONNREFUSED/i)
      expect(
        screen.getByRole('button', { name: /Dismiss leaderboard error/i }),
      ).toBeInTheDocument()
    })

    it('dismisses the error banner when the Dismiss button is clicked', async () => {
      vi.mocked(fetch).mockResolvedValueOnce(
        new Response('Internal Server Error', { status: 500 }),
      )
      render(<LeaderboardPanel />)
      await screen.findByText(/Leaderboard:/i)
      fireEvent.click(
        screen.getByRole('button', { name: /Dismiss leaderboard error/i }),
      )
      await waitFor(() => {
        expect(screen.queryByText(/Leaderboard:/i)).not.toBeInTheDocument()
      })
    })

    it('renders the error banner in the empty-state branch (no rows)', async () => {
      vi.mocked(fetch).mockResolvedValueOnce(
        new Response('Internal Server Error', { status: 500 }),
      )
      render(<LeaderboardPanel />)
      // Empty-state header should still render alongside the error banner.
      await screen.findByText(/Strategy Leaderboard/i)
      expect(
        screen.getByText(/Leaderboard:/i),
      ).toBeInTheDocument()
    })
  })
})
