// components/charts/CorrelationMatrix.test.tsx — Unit tests for the correlation matrix.
//
// Strategy:
//   • The component accepts an optional `matrix` prop that bypasses
//     the fetch loop. Every test passes a deterministic payload via
//     this prop so we don't have to mock the fetch + auto-refresh
//     loop. (The fetch loop is exercised by the panel-level test.)
//   • Each cell is a <button> with `data-coefficient`,
//     `data-row`, `data-col`, `data-sign` attributes — stable hooks
//     for asserting colour mapping + tooltip wiring.
//   • The diagonal cells (i === j) are always +1.00 (self-correlation);
//     the test verifies that the data-coefficient matches.
//   • The hover tooltip shows the precise coefficient + the row/col
//     labels — asserted by querying the tooltip role.

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CorrelationMatrix, {
  type CorrelationMatrixPayload,
} from './CorrelationMatrix'

const samplePayload: CorrelationMatrixPayload = {
  tokens: ['tok-a', 'tok-b', 'tok-c'],
  labels: ['Token A', 'Token B', 'Token C'],
  matrix: [
    [1.0, 0.85, -0.42],
    [0.85, 1.0, 0.0],
    [-0.42, 0.0, 1.0],
  ],
  method: 'pearson',
  sampleSize: 50,
  updatedAt: 1700000000000,
}

describe('CorrelationMatrix', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders the matrix when a payload is provided via the prop', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    expect(screen.getByTestId('corr-matrix')).toBeInTheDocument()
  })

  it('renders N×N cells (including the diagonal)', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    // 3 tokens → 9 cells (3 row labels × 3 col labels).
    // Each cell's data-testid follows the pattern
    // `corr-cell-{rowLabel}-{colLabel}`.
    for (const row of ['Token A', 'Token B', 'Token C']) {
      for (const col of ['Token A', 'Token B', 'Token C']) {
        expect(screen.getByTestId(`corr-cell-${row}-${col}`)).toBeInTheDocument()
      }
    }
  })

  it('renders the diagonal cells with coefficient = +1.00', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const diagA = screen.getByTestId('corr-cell-Token A-Token A')
    expect(diagA.getAttribute('data-coefficient')).toBe('1.000')
    const diagB = screen.getByTestId('corr-cell-Token B-Token B')
    expect(diagB.getAttribute('data-coefficient')).toBe('1.000')
    const diagC = screen.getByTestId('corr-cell-Token C-Token C')
    expect(diagC.getAttribute('data-coefficient')).toBe('1.000')
  })

  it('marks positive-correlation cells with data-sign=positive', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token B')
    expect(cell.getAttribute('data-coefficient')).toBe('0.850')
    expect(cell.getAttribute('data-sign')).toBe('positive')
  })

  it('marks negative-correlation cells with data-sign=negative', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token C')
    expect(cell.getAttribute('data-coefficient')).toBe('-0.420')
    expect(cell.getAttribute('data-sign')).toBe('negative')
  })

  it('marks zero-correlation cells with data-sign=neutral', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token B-Token C')
    expect(cell.getAttribute('data-coefficient')).toBe('0.000')
    expect(cell.getAttribute('data-sign')).toBe('neutral')
  })

  it('renders the formatted coefficient inside each cell', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    // 0.85 → "0.85" (the unicode minus is only for negative values)
    const cell = screen.getByTestId('corr-cell-Token A-Token B')
    expect(cell.textContent).toContain('0.85')
    // -0.42 → "−0.42" (unicode minus U+2212)
    const negCell = screen.getByTestId('corr-cell-Token A-Token C')
    expect(negCell.textContent).toContain('\u22120.42')
  })

  it('renders the colour legend (−1 → +1 gradient)', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const legend = screen.getByTestId('corr-matrix-legend')
    expect(legend).toBeInTheDocument()
    expect(legend.textContent).toMatch(/−1.0/i)
    expect(legend.textContent).toMatch(/\+1.0/i)
  })

  it('renders the method label + sample size badge', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    expect(screen.getAllByText(/pearson/i).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/50/).length).toBeGreaterThan(0)
  })

  it('shows a hover tooltip with the coefficient + the row/col pair', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token B')
    fireEvent.mouseEnter(cell)
    const tooltip = screen.getByTestId('corr-tooltip-Token A-Token B')
    expect(tooltip).toBeInTheDocument()
    expect(tooltip.textContent).toContain('Token A')
    expect(tooltip.textContent).toContain('Token B')
    expect(tooltip.textContent).toContain('0.85')
  })

  it('hides the tooltip on mouse leave', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token B')
    fireEvent.mouseEnter(cell)
    expect(screen.getByTestId('corr-tooltip-Token A-Token B')).toBeInTheDocument()
    fireEvent.mouseLeave(cell)
    expect(screen.queryByTestId('corr-tooltip-Token A-Token B')).not.toBeInTheDocument()
  })

  it('shows the tooltip on keyboard focus', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token B-Token C')
    fireEvent.focus(cell)
    expect(screen.getByTestId('corr-tooltip-Token B-Token C')).toBeInTheDocument()
  })

  it('renders an empty-state placeholder when the payload has 0 tokens', () => {
    const emptyPayload: CorrelationMatrixPayload = {
      tokens: [],
      labels: [],
      matrix: [],
      method: 'pearson',
      sampleSize: 0,
    }
    render(<CorrelationMatrix matrix={emptyPayload} />)
    expect(screen.getByTestId('corr-matrix-empty')).toBeInTheDocument()
    expect(screen.getByText(/No positions to correlate/i)).toBeInTheDocument()
  })

  it('renders "—" for cells with NaN / null coefficients', () => {
    const nanPayload: CorrelationMatrixPayload = {
      tokens: ['tok-x', 'tok-y'],
      labels: ['X', 'Y'],
      matrix: [
        [1.0, Number.NaN],
        [Number.NaN, 1.0],
      ],
      method: 'pearson',
    }
    render(<CorrelationMatrix matrix={nanPayload} />)
    const cell = screen.getByTestId('corr-cell-X-Y')
    // formatCorr(NaN) → "—"
    expect(cell.textContent).toContain('—')
  })

  it('falls back to truncated token ids when labels are missing', () => {
    const noLabelsPayload: CorrelationMatrixPayload = {
      tokens: ['verylongtokenid-1234567890-abcdef', 'anothertoken-0987654321'],
      matrix: [
        [1.0, 0.5],
        [0.5, 1.0],
      ],
      method: 'pearson',
    }
    render(<CorrelationMatrix matrix={noLabelsPayload} />)
    // The matrix renders 4 cells (2x2). Asserting that the truncated
    // labels (max 12 chars) appear somewhere in the document.
    expect(screen.getByTestId('corr-matrix')).toBeInTheDocument()
    // Find the row/column header for the first token — its truncated
    // label is "verylongtok…" (12 chars + ellipsis = 13 visible).
    // The truncated text should appear inside the row / column header.
    const truncated = noLabelsPayload.tokens[0].slice(0, 8) + '…'
    // The label could appear once for the column header and once for
    // the row header — assert at least one occurrence.
    const allCells = screen.getAllByText(truncated)
    expect(allCells.length).toBeGreaterThan(0)
  })

  it('renders a 1×1 matrix (single position) without crashing', () => {
    const single: CorrelationMatrixPayload = {
      tokens: ['tok-solo'],
      labels: ['Solo'],
      matrix: [[1.0]],
      method: 'pearson',
    }
    render(<CorrelationMatrix matrix={single} />)
    expect(screen.getByTestId('corr-cell-Solo-Solo')).toBeInTheDocument()
    expect(screen.getByTestId('corr-cell-Solo-Solo').getAttribute('data-coefficient')).toBe('1.000')
  })

  it('honours the cellSize prop', () => {
    render(<CorrelationMatrix matrix={samplePayload} cellSize={72} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token B') as HTMLElement
    expect(cell.style.width).toBe('72px')
    expect(cell.style.height).toBe('72px')
  })

  it('exposes an aria-label with the pair + coefficient for each cell', () => {
    render(<CorrelationMatrix matrix={samplePayload} />)
    const cell = screen.getByTestId('corr-cell-Token A-Token C')
    const label = cell.getAttribute('aria-label') ?? ''
    expect(label).toContain('Token A')
    expect(label).toContain('Token C')
    expect(label).toContain('−0.42') // unicode minus
  })
})
