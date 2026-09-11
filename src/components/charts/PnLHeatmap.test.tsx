// components/charts/PnLHeatmap.test.tsx — Unit tests for the P&L heatmap.
//
// Strategy:
//   • The heatmap is a pure CSS-grid component (no Recharts), so we
//     don't mock any chart library. We render the component directly
//     with deterministic data and assert on the rendered cells.
//   • Cell colour is asserted via the `data-pnl-sign` attribute (a
//     stable, test-friendly hook the component exposes).
//   • Hover tooltip is asserted by simulating `mouseEnter` /
//     `focus` on a cell and querying for the tooltip role.
//   • Click-to-expand is asserted by clicking a cell and querying
//     for the expanded detail strip.
//   • Mobile list-layout branch is asserted by toggling the
//     `listLayout` prop and verifying the grid switches to a stack.
//   • Empty-state branch is asserted by passing `data={[]}` and
//     querying for the empty placeholder.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import PnLHeatmap, { type PnLHeatmapDatum } from './PnLHeatmap'

const sampleData: PnLHeatmapDatum[] = [
  {
    tokenId: 'tok-a',
    label: 'Bitcoin Rally',
    outcome: 'YES',
    shares: 50,
    entryPrice: 0.45,
    currentPrice: 0.55,
    positionSize: 22.5,
    pnl: 5.0,
    pnlPct: 0.2222,
  },
  {
    tokenId: 'tok-b',
    label: 'Ethereum Merge',
    outcome: 'NO',
    shares: 30,
    entryPrice: 0.60,
    currentPrice: 0.50,
    positionSize: 18.0,
    pnl: -3.0,
    pnlPct: -0.1667,
  },
  {
    tokenId: 'tok-c',
    label: 'Fed Rate Cut',
    outcome: 'YES',
    shares: 20,
    entryPrice: 0.50,
    currentPrice: 0.50,
    positionSize: 10.0,
    pnl: 0.0,
    pnlPct: 0.0,
  },
]

describe('PnLHeatmap', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders one cell per datum', () => {
    render(<PnLHeatmap data={sampleData} />)
    expect(screen.getByTestId('pnl-heatmap')).toBeInTheDocument()
    // Three cells (one per datum) — the wrapper has the data-testid
    // `pnl-heatmap-cell-{tokenId}`.
    expect(screen.getByTestId('pnl-heatmap-cell-tok-a')).toBeInTheDocument()
    expect(screen.getByTestId('pnl-heatmap-cell-tok-b')).toBeInTheDocument()
    expect(screen.getByTestId('pnl-heatmap-cell-tok-c')).toBeInTheDocument()
  })

  it('renders the empty-state placeholder when data is []', () => {
    render(<PnLHeatmap data={[]} />)
    expect(screen.getByTestId('pnl-heatmap-empty')).toBeInTheDocument()
    expect(screen.getByText(/No open positions to render/i)).toBeInTheDocument()
  })

  it('marks profit cells with data-pnl-sign=positive', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    expect(cell.getAttribute('data-pnl-sign')).toBe('positive')
  })

  it('marks loss cells with data-pnl-sign=negative', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-b')
    expect(cell.getAttribute('data-pnl-sign')).toBe('negative')
  })

  it('marks break-even cells with data-pnl-sign=neutral', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-c')
    expect(cell.getAttribute('data-pnl-sign')).toBe('neutral')
  })

  it('renders the colour legend', () => {
    render(<PnLHeatmap data={sampleData} />)
    const legend = screen.getByTestId('pnl-heatmap-legend')
    expect(legend).toBeInTheDocument()
    expect(legend.textContent).toMatch(/Loss/i)
    expect(legend.textContent).toMatch(/Profit/i)
  })

  it('renders the formatted P&L value inside each cell', () => {
    // fmtPnl uses a Unicode MINUS (U+2212) for negative values.
    const MINUS = '\u2212'
    render(<PnLHeatmap data={sampleData} />)
    // tok-a: pnl=5.0 → "+$5.00"
    expect(screen.getByTestId('pnl-heatmap-cell-tok-a').textContent).toContain('+$5.00')
    // tok-b: pnl=-3.0 → "−$3.00" (Unicode minus)
    expect(screen.getByTestId('pnl-heatmap-cell-tok-b').textContent).toContain(`${MINUS}$3.00`)
  })

  it('shows a hover tooltip with the per-position detail fields', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    // Tooltip is rendered lazily (only on hover/focus), so it should
    // NOT be in the document before interaction.
    expect(screen.queryByTestId('pnl-heatmap-tooltip-tok-a')).not.toBeInTheDocument()
    fireEvent.mouseEnter(cell)
    const tooltip = screen.getByTestId('pnl-heatmap-tooltip-tok-a')
    expect(tooltip).toBeInTheDocument()
    // Tooltip body contains the per-position labels.
    expect(tooltip.textContent).toContain('Token')
    expect(tooltip.textContent).toContain('Position')
    expect(tooltip.textContent).toContain('Entry')
    expect(tooltip.textContent).toContain('Current')
    expect(tooltip.textContent).toContain('P&L $')
    expect(tooltip.textContent).toContain('P&L %')
  })

  it('hides the tooltip on mouse leave', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    fireEvent.mouseEnter(cell)
    expect(screen.getByTestId('pnl-heatmap-tooltip-tok-a')).toBeInTheDocument()
    fireEvent.mouseLeave(cell)
    expect(screen.queryByTestId('pnl-heatmap-tooltip-tok-a')).not.toBeInTheDocument()
  })

  it('shows the tooltip on keyboard focus (accessibility)', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-b')
    fireEvent.focus(cell)
    expect(screen.getByTestId('pnl-heatmap-tooltip-tok-b')).toBeInTheDocument()
    fireEvent.blur(cell)
    expect(screen.queryByTestId('pnl-heatmap-tooltip-tok-b')).not.toBeInTheDocument()
  })

  it('expands the detail strip on cell click', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    expect(screen.queryByTestId('pnl-heatmap-detail-tok-a')).not.toBeInTheDocument()
    fireEvent.click(cell)
    expect(screen.getByTestId('pnl-heatmap-detail-tok-a')).toBeInTheDocument()
  })

  it('collapses the detail strip when the same cell is clicked again', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    fireEvent.click(cell)
    expect(screen.getByTestId('pnl-heatmap-detail-tok-a')).toBeInTheDocument()
    fireEvent.click(cell)
    expect(screen.queryByTestId('pnl-heatmap-detail-tok-a')).not.toBeInTheDocument()
  })

  it('switches the active expansion when a different cell is clicked', () => {
    render(<PnLHeatmap data={sampleData} />)
    fireEvent.click(screen.getByTestId('pnl-heatmap-cell-tok-a'))
    expect(screen.getByTestId('pnl-heatmap-detail-tok-a')).toBeInTheDocument()
    expect(screen.queryByTestId('pnl-heatmap-detail-tok-b')).not.toBeInTheDocument()
    fireEvent.click(screen.getByTestId('pnl-heatmap-cell-tok-b'))
    expect(screen.queryByTestId('pnl-heatmap-detail-tok-a')).not.toBeInTheDocument()
    expect(screen.getByTestId('pnl-heatmap-detail-tok-b')).toBeInTheDocument()
  })

  it('exposes aria-label with the position label + P&L + press-Enter hint', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    const label = cell.getAttribute('aria-label') ?? ''
    expect(label).toContain('Bitcoin Rally')
    expect(label).toContain('expand')
  })

  it('sets aria-expanded=true on the expanded cell', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    expect(cell.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(cell)
    expect(cell.getAttribute('aria-expanded')).toBe('true')
  })

  it('honours the cellHeight prop', () => {
    render(<PnLHeatmap data={sampleData} cellHeight={80} />)
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a') as HTMLElement
    // The cell is a <button> with an inline height style.
    expect(cell.style.height).toBe('80px')
  })

  it('renders in list-layout mode (mobile collapse)', () => {
    render(<PnLHeatmap data={sampleData} listLayout />)
    // In list-layout mode, the cell height is `auto` (driven by content)
    // rather than the fixed cellHeight.
    const cell = screen.getByTestId('pnl-heatmap-cell-tok-a') as HTMLElement
    expect(cell.style.height).toBe('auto')
    expect(cell.style.minHeight).toBe('48px')
  })

  it('uses the explicit maxMagnitude override when provided', () => {
    // Pass a tiny maxMagnitude so the largest cell saturates intensity.
    render(<PnLHeatmap data={sampleData} maxMagnitude={5.0} cellHeight={50} />)
    const profitCell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    // The cell's background colour should be a saturated green.
    const bg = profitCell.style.background
    expect(bg).toMatch(/rgba\(34, 197, 94,/) // PROFIT_RGB
  })

  it('derives the maxMagnitude from the data when not overridden', () => {
    render(<PnLHeatmap data={sampleData} cellHeight={50} />)
    // The largest |pnl| in the sample is 5.0 (tok-a). The cell should
    // saturate at the same intensity as the explicit-override case.
    const profitCell = screen.getByTestId('pnl-heatmap-cell-tok-a')
    const bg = profitCell.style.background
    expect(bg).toMatch(/rgba\(34, 197, 94,/)
  })

  it('renders the detail strip with the full set of fields when expanded', () => {
    render(<PnLHeatmap data={sampleData} />)
    fireEvent.click(screen.getByTestId('pnl-heatmap-cell-tok-c'))
    const detail = screen.getByTestId('pnl-heatmap-detail-tok-c')
    expect(detail.textContent).toContain('Token ID')
    expect(detail.textContent).toContain('Market')
    expect(detail.textContent).toContain('Outcome')
    expect(detail.textContent).toContain('Shares')
    expect(detail.textContent).toContain('Entry Price')
    expect(detail.textContent).toContain('Current Price')
    expect(detail.textContent).toContain('Position Size')
    expect(detail.textContent).toContain('P&L ($)')
    expect(detail.textContent).toContain('P&L (%)')
  })

  it('renders the outcome badge inside each cell label', () => {
    render(<PnLHeatmap data={sampleData} />)
    const cellA = screen.getByTestId('pnl-heatmap-cell-tok-a')
    expect(cellA.textContent).toMatch(/YES/)
    const cellB = screen.getByTestId('pnl-heatmap-cell-tok-b')
    expect(cellB.textContent).toMatch(/NO/)
  })
})
