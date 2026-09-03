// components/AnalyticsPanel.test.tsx — KPI rendering, formatting & error state.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import AnalyticsPanel from './AnalyticsPanel'

// W15-5 — MockWebSocket stub. Same pattern as useWebSocket.test.ts /
// useRealtimeData.test.ts. The panel now opens a real WS via
// useRealtimeData → useWebSocket; without this stub, jsdom attempts an
// actual ws://localhost:8080/ws connection that errors on every test.
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

// fmtPnl emits a Unicode "−" (U+2212) for negative values.
const MINUS = '\u2212'

const sampleAnalytics = {
  equity: 110.5,
  realized_pnl: 11.5,
  unrealized_pnl: 1.22,
  net_pnl: 12.72,
  total_trades: 42,
  winning_trades: 30,
  losing_trades: 12,
  closed_trades: 42,
  open_trades: 3,
  win_rate: 0.714,
  win_rate_ci_low: 0.55,
  win_rate_ci_high: 0.84,
  profit_factor: 2.5,
  max_drawdown_dollars: 4.2,
  max_drawdown_pct: 0.04,
  total_volume_usdc: 523.4,
  open_exposure: 12.5,
  open_position_count: 3,
  pending_order_capital: 0,
  risk_utilization: 0.5,
  mode: 'paper',
  data_freshness_seconds: 2,
  peak_equity: 112.0,
  active_strategies: ['mm_avellaneda_stoikov', 'arb_binary_dutch_book'],
  avg_win: 1.2,
  avg_loss: -0.45,
  expectancy: 0.19,
  sharpe_ratio: 1.85,
}

function mockFetchOk(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: async () => payload,
  } as Response)
}

function mockFetchNotOk(status = 500) {
  return vi.fn().mockResolvedValue({
    ok: false,
    status,
    json: async () => ({}),
  } as Response)
}

describe('AnalyticsPanel', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    // Re-install a fresh fetch mock before each test.
    global.fetch = vi.fn() as unknown as typeof fetch
    // W15-5 — install MockWebSocket so useRealtimeData's internal
    // useWebSocket() call doesn't attempt a real ws:// connection.
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
  })

  it('renders the loading state on first mount before data arrives', () => {
    // Never-resolving promise → loading stays true indefinitely.
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => {}))
    render(<AnalyticsPanel />)
    expect(screen.getByText(/Loading analytics/i)).toBeInTheDocument()
  })

  it('renders the "Performance Analytics" header + mode badge after data loads', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
    })
    // mode = "paper" → uppercased badge
    expect(screen.getByText('PAPER')).toBeInTheDocument()
  })

  it('renders all KPI card labels (expectancy, avg win/loss, sharpe, etc.)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('Expectancy / Trade')).toBeInTheDocument()
    })
    expect(screen.getByText('Avg Win / Avg Loss')).toBeInTheDocument()
    expect(screen.getByText('Sharpe Ratio')).toBeInTheDocument()
    expect(screen.getByText('Profit Factor')).toBeInTheDocument()
    expect(screen.getByText('Max Drawdown')).toBeInTheDocument()
    expect(screen.getByText('Trades / Volume')).toBeInTheDocument()
  })

  it('formats the win rate as a percentage with one decimal', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      // (0.714 * 100).toFixed(1) === '71.4'
      expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
    })
    // KPI card also shows the win rate value with the same formatting
    expect(screen.getByText('71.4%')).toBeInTheDocument()
  })

  it('formats expectancy with leading + sign and USD currency', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('+$0.19')).toBeInTheDocument()
    })
  })

  it('formats expectancy red when negative', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sampleAnalytics, expectancy: -0.42 }),
    )
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText(`${MINUS}$0.42`)).toBeInTheDocument()
    })
    // The expectancy span uses text-[#f87171] when (expectancy ?? 0) < 0
    const expectancySpan = screen.getByText(`${MINUS}$0.42`)
    expect(expectancySpan.className).toContain('text-[#f87171]')
  })

  it('formats Sharpe ratio with two decimals', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('1.85')).toBeInTheDocument()
    })
  })

  it('formats avg win / avg loss as separate USD values', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('$1.20')).toBeInTheDocument()
      expect(screen.getByText(`${MINUS}$0.45`)).toBeInTheDocument()
    })
  })

  it('formats profit factor with two decimals', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      // profit_factor = 2.5 → toFixed(2) = "2.50"
      expect(screen.getByText('2.50')).toBeInTheDocument()
    })
  })

  it('formats total trades and volume', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('42 trades')).toBeInTheDocument()
    })
    expect(screen.getByText('$523.40 vol')).toBeInTheDocument()
  })

  it('formats max drawdown with USD + percentage', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      // fmtUsd(4.2) = "$4.20", fmtPct(0.04) = "4.0%"
      expect(screen.getByText(/\$4\.20 \(4\.0%\)/)).toBeInTheDocument()
    })
  })

  it('renders the small-sample warning when closed_trades < 10', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sampleAnalytics, closed_trades: 5 }),
    )
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeInTheDocument()
    })
    expect(screen.getByText(/Small sample/)).toBeInTheDocument()
  })

  it('does NOT render the small-sample warning when closed_trades >= 10', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
    })
    expect(screen.queryByText(/Small sample/)).not.toBeInTheDocument()
  })

  it('renders active strategies as labelled badges', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(
        screen.getByText(/Avellaneda-Stoikov MM/),
      ).toBeInTheDocument()
      expect(screen.getByText(/Dutch-Book Arb/)).toBeInTheDocument()
    })
  })

  it('renders the win-rate trend arrow + colour based on CI midpoint', async () => {
    // CI midpoint = (0.55 + 0.84) / 2 = 0.695 → > 0.505 → "▲" green
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('▲')).toBeInTheDocument()
    })
    // The trend span uses text-green-400 when ciMid > 0.505
    const trendSpan = screen.getByText('▲')
    expect(trendSpan.className).toContain('text-green-400')
  })

  it('shows "Analytics data unavailable" when fetch returns not-ok', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchNotOk(500))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('Analytics data unavailable')).toBeInTheDocument()
    })
  })

  it('shows "Analytics data unavailable" when fetch throws', async () => {
    vi.mocked(fetch).mockRejectedValue(new Error('Network error'))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('Analytics data unavailable')).toBeInTheDocument()
    })
  })

  it('fetches analytics on mount and calls the /api/analytics endpoint', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
    })
    expect(vi.mocked(fetch)).toHaveBeenCalled()
    const firstCallUrl = (vi.mocked(fetch).mock.calls[0] as [string, unknown])[0] as string
    expect(firstCallUrl).toContain('/api/analytics')
    expect(firstCallUrl).toContain('XTransformPort=8080')
  })

  it('injects an Authorization header into the analytics fetch', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
    })
    const init = (vi.mocked(fetch).mock.calls[0] as [string, RequestInit])[1]
    const headers = new Headers(init.headers)
    expect(headers.get('Authorization')).toMatch(/^Bearer\s+\S+$/)
  })

  it('stops the polling interval on unmount (no leaked setState warnings)', async () => {
    vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
    const { unmount } = render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
    })
    // Unmount should run the effect cleanup → clearInterval.
    expect(() => act(() => unmount())).not.toThrow()
  })

  it('renders Infinity (∞) when profit_factor === "Infinity"', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sampleAnalytics, profit_factor: 'Infinity' }),
    )
    render(<AnalyticsPanel />)
    await waitFor(() => {
      expect(screen.getByText('∞')).toBeInTheDocument()
    })
  })

  it('renders "—" for expectancy when the field is null', async () => {
    vi.mocked(fetch).mockImplementation(
      mockFetchOk({ ...sampleAnalytics, expectancy: null }),
    )
    render(<AnalyticsPanel />)
    await waitFor(() => {
      // The expectancy KPI card renders a single "—" inside its value span.
      const expectancyLabel = screen.getByText('Expectancy / Trade')
      const card = expectancyLabel.closest('.kpi-card')
      const valueSpan = card?.querySelector('.kpi-value')
      expect(valueSpan?.textContent).toBe('—')
    })
  })

  // ─────────────────────────────────────────────────────────────────────
  // W15-5 — Realtime migration tests
  // ─────────────────────────────────────────────────────────────────────

  describe('W15-5: realtime migration', () => {
    it('renders the "Polling" badge before the WS connects', async () => {
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
      render(<AnalyticsPanel />)
      await waitFor(() => {
        expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
      })
      // WS is constructed but `triggerOpen()` hasn't been called yet —
      // isRealtime=false → amber "⟳ Polling" badge should be visible.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      expect(screen.queryByText('● Live')).not.toBeInTheDocument()
    })

    it('flips to the "Live" badge when the WS connects', async () => {
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
      render(<AnalyticsPanel />)
      await waitFor(() => {
        expect(screen.getByText('📊 Performance Analytics')).toBeInTheDocument()
      })
      // Before open: polling badge.
      expect(screen.getByText('⟳ Polling')).toBeInTheDocument()
      // Drive the MockWebSocket through its lifecycle.
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      ws.triggerOpen()
      // After open: live badge.
      expect(await screen.findByText('● Live')).toBeInTheDocument()
      expect(screen.queryByText('⟳ Polling')).not.toBeInTheDocument()
    })

    it('drops metrics-channel WS payloads that do not look like Analytics', async () => {
      // The `metrics` channel canonically pushes BotSnapshot — not the
      // Analytics shape. The panel's `validate` predicate should drop
      // such payloads so the displayed KPIs are not clobbered with
      // mismatched fields.
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
      render(<AnalyticsPanel />)
      await waitFor(() => {
        expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
      })
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      ws.triggerOpen()
      // Push a BotSnapshot-shaped payload — no `equity` numeric field.
      ws.triggerMessage({
        channel: 'metrics',
        data: { mode: 'paper', kill_switch: false, order_books: [] },
      })
      // The win rate label is still the REST-derived 71.4% (not clobbered).
      expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
    })

    it('accepts metrics-channel WS payloads that match the Analytics shape', async () => {
      // Initial REST response with win_rate 71.4%.
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
      render(<AnalyticsPanel />)
      await waitFor(() => {
        expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
      })
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      ws.triggerOpen()
      // Push an Analytics-shaped payload via the metrics channel —
      // win_rate moves from 0.714 to 0.85.
      ws.triggerMessage({
        channel: 'metrics',
        data: { ...sampleAnalytics, win_rate: 0.85 },
      })
      // The new win rate (85.0%) should be reflected in BOTH the header
      // pill and the KPI card.
      await waitFor(() => {
        expect(screen.getByText('85.0% Win Rate')).toBeInTheDocument()
      })
      expect(screen.getByText('85.0%')).toBeInTheDocument()
    })

    it('ignores WS messages on channels it did not subscribe to', async () => {
      vi.mocked(fetch).mockImplementation(mockFetchOk(sampleAnalytics))
      render(<AnalyticsPanel />)
      await waitFor(() => {
        expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
      })
      const ws = MockWebSocket.instances[0]
      expect(ws).toBeTruthy()
      ws.triggerOpen()
      // Push on the wrong channel — data should be unchanged.
      ws.triggerMessage({
        channel: 'positions',
        data: { positions: [] },
      })
      expect(screen.getByText('71.4% Win Rate')).toBeInTheDocument()
    })
  })
})
