// components/CommandCenterHealthBar.test.tsx — W40-2 minimal render tests
// for the W38-3 / W39-3 compact system health bar.
//
// The bar is a presentational shell driven by `snapshot` + `status` +
// `wsConnected` props — no fetch, but it does own a 5s re-render timer
// for the "Xs ago" freshness pill. Tests cover:
//   1. Renders without crashing.
//   2. Renders the documented `data-testid` region.
//   3. Renders all six health indicators (Backend / WebSocket / Data Fresh
//      / Risk Level / Kill Switch / AI Status).
//   4. Backend indicator reflects the `status` prop (Online / Offline).
//   5. Kill switch indicator pulses when the snapshot's kill switch is on.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import CommandCenterHealthBar from './CommandCenterHealthBar'
import type { BotSnapshot, ConnectionStatus } from '@/hooks/useBot'

global.fetch = vi.fn()

function makeSnapshot(
  overrides: Partial<BotSnapshot> = {},
): BotSnapshot {
  return {
    type: 'snapshot',
    timestamp: Math.floor(Date.now() / 1000),
    mode: 'paper',
    kill_switch: false,
    kill_switch_durable: false,
    observation_only: false,
    observation_reason: '',
    daily_pnl: 0,
    paper_balance: 100,
    strategies: [],
    order_books: [],
    open_orders: [],
    positions: [],
    recent_trades: [],
    events: [],
    ...overrides,
  }
}

describe('CommandCenterHealthBar', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
      />,
    )
    expect(container.firstChild).toBeTruthy()
  })

  it('renders the documented data-testid region', () => {
    render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
      />,
    )
    expect(
      screen.getByTestId('command-center-health-bar'),
    ).toBeInTheDocument()
  })

  it('renders all six indicator labels', () => {
    render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
      />,
    )
    expect(screen.getByText('Backend')).toBeInTheDocument()
    expect(screen.getByText('WebSocket')).toBeInTheDocument()
    expect(screen.getByText('Data Fresh')).toBeInTheDocument()
    expect(screen.getByText('Risk Level')).toBeInTheDocument()
    expect(screen.getByText('Kill Switch')).toBeInTheDocument()
    expect(screen.getByText('AI Status')).toBeInTheDocument()
  })

  it('reflects the backend connection status (Online when connected)', () => {
    render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot()}
        status="connected"
        wsConnected
      />,
    )
    expect(screen.getByText('Online')).toBeInTheDocument()
  })

  it('shows Offline when backend status is disconnected', () => {
    const status: ConnectionStatus = 'disconnected'
    render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot()}
        status={status}
        wsConnected={false}
      />,
    )
    expect(screen.getByText('Offline')).toBeInTheDocument()
  })

  it('shows ON for the kill switch when snapshot.kill_switch is true', () => {
    render(
      <CommandCenterHealthBar
        snapshot={makeSnapshot({ kill_switch: true })}
        status="connected"
        wsConnected
      />,
    )
    expect(screen.getByText('ON')).toBeInTheDocument()
  })
})
