// components/ConnectionStatus.test.tsx — W15-5 transport-state pill.
//
// The component renders a small dot + label that reflects the WebSocket
// transport state. It is mounted inside TopStatusBar so the trader
// can tell at a glance whether live pushes are flowing (green / "WS Live")
// or whether the UI is relying on REST polling (amber / "Polling").
// A transient `onerror` event surfaces red ("WS Error") before the
// reconnect logic flips back to amber.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, act } from '@testing-library/react'
import ConnectionStatus from './ConnectionStatus'

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

  triggerError(err?: unknown) {
    this.onerror?.(err)
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

describe('ConnectionStatus', () => {
  let originalWebSocket: typeof WebSocket

  beforeEach(() => {
    originalWebSocket = global.WebSocket
    MockWebSocket.instances = []
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      MockWebSocket as unknown as typeof WebSocket
  })

  afterEach(() => {
    ;(global as unknown as { WebSocket: typeof WebSocket }).WebSocket =
      originalWebSocket
  })

  it('renders the "Polling" state before the WS connects', () => {
    render(<ConnectionStatus />)
    // The amber "Polling" pill is the default state — the WS is
    // constructed but triggerOpen() hasn't fired yet.
    expect(screen.getByText('Polling')).toBeInTheDocument()
    expect(screen.queryByText('WS Live')).not.toBeInTheDocument()
    expect(screen.queryByText('WS Error')).not.toBeInTheDocument()
  })

  it('flips to the "WS Live" state when the WS opens', async () => {
    render(<ConnectionStatus />)
    expect(screen.getByText('Polling')).toBeInTheDocument()
    const ws = MockWebSocket.instances[0]
    expect(ws).toBeTruthy()
    await act(async () => { ws.triggerOpen() })
    expect(screen.getByText('WS Live')).toBeInTheDocument()
    expect(screen.queryByText('Polling')).not.toBeInTheDocument()
  })

  it('flips to the "WS Error" state when onerror fires', async () => {
    render(<ConnectionStatus />)
    const ws = MockWebSocket.instances[0]
    expect(ws).toBeTruthy()
    await act(async () => { ws.triggerError(new Event('error')) })
    expect(screen.getByText('WS Error')).toBeInTheDocument()
    expect(screen.queryByText('Polling')).not.toBeInTheDocument()
    expect(screen.queryByText('WS Live')).not.toBeInTheDocument()
  })

  it('recovers from error to live on the next open', async () => {
    render(<ConnectionStatus />)
    const ws = MockWebSocket.instances[0]
    expect(ws).toBeTruthy()
    // Error fires first.
    await act(async () => { ws.triggerError(new Event('error')) })
    expect(screen.getByText('WS Error')).toBeInTheDocument()
    // Then a successful reconnect.
    await act(async () => { ws.triggerOpen() })
    expect(screen.getByText('WS Live')).toBeInTheDocument()
    expect(screen.queryByText('WS Error')).not.toBeInTheDocument()
  })

  it('exposes the connection state via an aria-label on the pill button', async () => {
    render(<ConnectionStatus />)
    // Initial state — polling.
    expect(
      screen.getByRole('button', { name: /Connection status: Polling/i }),
    ).toBeInTheDocument()
    const ws = MockWebSocket.instances[0]
    expect(ws).toBeTruthy()
    await act(async () => { ws.triggerOpen() })
    expect(
      screen.getByRole('button', { name: /Connection status: WS Live/i }),
    ).toBeInTheDocument()
  })

  it('hides the text label in compact mode but keeps the dot', () => {
    const { container } = render(<ConnectionStatus compact />)
    // No "Polling" / "WS Live" / "WS Error" text rendered.
    expect(screen.queryByText('Polling')).not.toBeInTheDocument()
    expect(screen.queryByText('WS Live')).not.toBeInTheDocument()
    expect(screen.queryByText('WS Error')).not.toBeInTheDocument()
    // The dot span is still present.
    const dot = container.querySelector('.w-2.h-2.rounded-full')
    expect(dot).not.toBeNull()
    // The accessible label still describes the state.
    expect(
      screen.getByRole('button', { name: /Connection status: Polling/i }),
    ).toBeInTheDocument()
  })

  it('renders a green dot when WS is live, amber when polling', async () => {
    const { container } = render(<ConnectionStatus />)
    // Default state — amber dot.
    let dot = container.querySelector('.w-2.h-2.rounded-full')
    expect(dot?.className).toContain('bg-amber-400')
    // After open — green dot.
    const ws = MockWebSocket.instances[0]
    expect(ws).toBeTruthy()
    await act(async () => { ws.triggerOpen() })
    dot = container.querySelector('.w-2.h-2.rounded-full')
    expect(dot?.className).toContain('bg-green-400')
    expect(dot?.className).not.toContain('bg-amber-400')
  })
})
