// components/PositionsPanel.test.tsx — Active positions table rendering & actions.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import PositionsPanel from './PositionsPanel'
import { Position } from '@/hooks/useBot'

// fmtPnl emits a Unicode "−" (U+2212) for negative values, not a hyphen.
const MINUS = '\u2212'

const samplePositions: Position[] = [
  {
    token_id: 'tok-1',
    slug: 'bitcoin-100k-rally',
    yes_shares: 50,
    no_shares: 0,
    avg_entry_price: 0.45,
    total_invested: 22.5,
    realised_pnl: 1.5,
    current_price: 0.55,
    unrealized_pnl: 5.0,
  },
  {
    token_id: 'tok-2',
    slug: 'ethereum-merge-success',
    yes_shares: 0,
    no_shares: 30,
    avg_entry_price: 0.6,
    total_invested: 18.0,
    realised_pnl: -2.0,
    current_price: 0.5,
    unrealized_pnl: -3.0,
  },
]

describe('PositionsPanel', () => {
  it('renders the header with the active position count', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText(/ACTIVE POSITIONS/)).toBeInTheDocument()
    expect(screen.getByText(/ACTIVE POSITIONS \(2\)/)).toBeInTheDocument()
  })

  it('renders all positions with derived market info', () => {
    // The category icon is rendered alongside the event title in the same
    // <span>, so we use substring matching to find "BITCOIN" / "ETHEREUM".
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText(/BITCOIN/)).toBeInTheDocument()
    expect(screen.getByText(/ETHEREUM/)).toBeInTheDocument()
    // Question text (second span) is rendered on its own.
    expect(screen.getByText('100k Rally')).toBeInTheDocument()
    expect(screen.getByText('Merge Success')).toBeInTheDocument()
  })

  it('renders YES / NO outcome badges correctly', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const yesBadges = screen.getAllByText('YES')
    const noBadges = screen.getAllByText('NO')
    expect(yesBadges.length).toBeGreaterThanOrEqual(1)
    expect(noBadges.length).toBeGreaterThanOrEqual(1)
  })

  it('renders shares with one decimal precision', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText('50.0')).toBeInTheDocument() // yes_shares=50 → "50.0"
    expect(screen.getByText('30.0')).toBeInTheDocument() // no_shares=30  → "30.0"
  })

  it('renders avg entry price with 3-decimal precision', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText('$0.450')).toBeInTheDocument()
    expect(screen.getByText('$0.600')).toBeInTheDocument()
  })

  it('renders the live mark price when current_price is set', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText('$0.550')).toBeInTheDocument() // tok-1 mark
    expect(screen.getByText('$0.500')).toBeInTheDocument() // tok-2 mark
  })

  it('renders the cost basis (total_invested) as USD', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(screen.getByText('$22.50')).toBeInTheDocument()
    expect(screen.getByText('$18.00')).toBeInTheDocument()
  })

  it('color-codes unrealized PnL: green for positive, red for negative', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const posPnl = screen.getByText('+$5.00')
    const negPnl = screen.getByText(`${MINUS}$3.00`)
    const posTd = posPnl.closest('td')
    const negTd = negPnl.closest('td')
    expect(posTd).not.toBeNull()
    expect(negTd).not.toBeNull()
    expect(posTd?.className).toContain('text-green-400')
    expect(posTd?.className).not.toContain('text-red-400')
    expect(negTd?.className).toContain('text-red-400')
    expect(negTd?.className).not.toContain('text-green-400')
  })

  it('color-codes realized PnL too (positive green, negative red)', () => {
    // Use a non-zero dailyPnl so the daily-PnL badge doesn't collide with
    // the realized-PnL cells. tok-1 realised=+1.5 → "+$1.50" (green),
    // tok-2 realised=-2.0 → "−$2.00" (red). Total realized = -0.5 →
    // header badge "−$0.50" (different text from tok-2's "−$2.00").
    render(<PositionsPanel positions={samplePositions} dailyPnl={7.77} />)
    const posPnl = screen.getByText('+$1.50')
    const negPnl = screen.getByText(`${MINUS}$2.00`)
    expect(posPnl.closest('td')?.className).toContain('text-green-400')
    expect(negPnl.closest('td')?.className).toContain('text-red-400')
  })

  it('renders a Trade button for each position', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const tradeButtons = screen.getAllByTitle('Open Depth & Trade Modal')
    expect(tradeButtons).toHaveLength(2)
    tradeButtons.forEach((btn) => {
      expect(btn.textContent).toContain('Trade')
    })
  })

  it('renders a Close button for each position (always present)', () => {
    // The Close button is rendered unconditionally; the onClosePosition
    // handler is simply a no-op when not provided.
    render(
      <PositionsPanel
        positions={samplePositions}
        dailyPnl={0}
        onClosePosition={() => {}}
      />,
    )
    const closeButtons = screen.getAllByTitle('Close position at market')
    expect(closeButtons).toHaveLength(2)
    closeButtons.forEach((btn) => {
      expect(btn.textContent).toContain('Close')
    })
  })

  it('still renders Close buttons when onClosePosition is not provided', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const closeButtons = screen.getAllByTitle('Close position at market')
    expect(closeButtons).toHaveLength(2)
  })

  it('calls onClosePosition with the correct token id when Close is clicked', async () => {
    const user = userEvent.setup()
    const onClosePosition = vi.fn()
    render(
      <PositionsPanel
        positions={samplePositions}
        dailyPnl={0}
        onClosePosition={onClosePosition}
      />,
    )
    const closeButtons = screen.getAllByTitle('Close position at market')
    await user.click(closeButtons[0]) // tok-1
    expect(onClosePosition).toHaveBeenCalledWith('tok-1')
    await user.click(closeButtons[1]) // tok-2
    expect(onClosePosition).toHaveBeenCalledWith('tok-2')
    expect(onClosePosition).toHaveBeenCalledTimes(2)
  })

  it('Close click is a no-op when onClosePosition is not provided (no throw)', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const closeButtons = screen.getAllByTitle('Close position at market')
    // Should not throw — `onClosePosition?.()` short-circuits on undefined.
    await expect(user.click(closeButtons[0])).resolves.toBeUndefined()
  })

  it('calls onSelectMarket with token + slug when the Trade button is clicked', async () => {
    const user = userEvent.setup()
    const onSelectMarket = vi.fn()
    render(
      <PositionsPanel
        positions={samplePositions}
        dailyPnl={0}
        onSelectMarket={onSelectMarket}
      />,
    )
    const tradeButtons = screen.getAllByTitle('Open Depth & Trade Modal')
    await user.click(tradeButtons[0])
    expect(onSelectMarket).toHaveBeenCalledWith({
      tokenId: 'tok-1',
      slug: 'bitcoin-100k-rally',
    })
  })

  it('renders the exposure utilisation gauge (cap limit column)', () => {
    const { container } = render(
      <PositionsPanel positions={samplePositions} dailyPnl={0} />,
    )
    // The "$X/$3" suffix appears in the cap-limit column for each row.
    expect(screen.getByText(/\$22\.50\/\$3/)).toBeInTheDocument()
    expect(screen.getByText(/\$18\.00\/\$3/)).toBeInTheDocument()
    // Gauge fill divs exist
    const fills = container.querySelectorAll('.h-full.rounded-full')
    expect(fills.length).toBeGreaterThanOrEqual(2)
  })

  it('shows the empty-state placeholder when there are no positions', () => {
    render(<PositionsPanel positions={[]} dailyPnl={0} />)
    expect(screen.getByText('No positions found')).toBeInTheDocument()
    // Empty-state descriptive copy (no filters applied)
    expect(
      screen.getByText(/Automated strategies.*will populate live positions here/),
    ).toBeInTheDocument()
  })

  it('shows the filter-mismatch empty-state copy when a filter is active', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const input = screen.getByPlaceholderText(
      'Search position by market / contract...',
    ) as HTMLInputElement
    await user.type(input, 'nonexistentmarket')
    expect(screen.getByText('No positions found')).toBeInTheDocument()
    expect(
      screen.getByText('No open positions match your active filters.'),
    ).toBeInTheDocument()
  })

  it('renders the CSV export button (enabled when positions exist)', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const csvBtn = screen.getByTitle('Export Positions CSV')
    expect(csvBtn).not.toBeDisabled()
  })

  it('disables the CSV export button when there are no positions', () => {
    render(<PositionsPanel positions={[]} dailyPnl={0} />)
    const csvBtn = screen.getByTitle('Export Positions CSV')
    expect(csvBtn).toBeDisabled()
  })

  it('renders the daily PnL KPI badge with sign + colour', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={12.34} />)
    const dailyPnl = screen.getByText('+$12.34')
    expect(dailyPnl).toBeInTheDocument()
    // Positive → green class
    expect(dailyPnl.className).toContain('text-green-400')
  })

  it('renders the daily PnL KPI red when negative', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={-5.0} />)
    const dailyPnl = screen.getByText(`${MINUS}$5.00`)
    expect(dailyPnl).toBeInTheDocument()
    expect(dailyPnl.className).toContain('text-red-400')
  })

  it('renders the search input + outcome filter buttons + sort select', () => {
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    expect(
      screen.getByPlaceholderText('Search position by market / contract...'),
    ).toBeInTheDocument()
    const sortSelect = screen.getByRole('combobox') as HTMLSelectElement
    expect(sortSelect).toBeInTheDocument()
    expect(sortSelect.options.length).toBe(3)
  })

  it('filters positions by query (market slug match)', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const input = screen.getByPlaceholderText(
      'Search position by market / contract...',
    ) as HTMLInputElement
    await user.type(input, 'bitcoin')
    // tok-1 still visible, tok-2 hidden
    expect(screen.getByText(/BITCOIN/)).toBeInTheDocument()
    expect(screen.queryByText(/ETHEREUM/)).not.toBeInTheDocument()
  })

  it('clears the filter when the clear ✕ button is pressed', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    const input = screen.getByPlaceholderText(
      'Search position by market / contract...',
    ) as HTMLInputElement
    await user.type(input, 'bitcoin')
    expect(input.value).toBe('bitcoin')
    // The clear ✕ button is the only button inside the search input's
    // relative wrapper. Find it via the input's container.
    const searchWrapper = input.closest('.relative') as HTMLElement
    expect(searchWrapper).not.toBeNull()
    const clearBtn = within(searchWrapper).getByRole('button')
    await user.click(clearBtn)
    expect(input.value).toBe('')
  })

  it('filters by outcome (YES only shows YES positions)', async () => {
    const user = userEvent.setup()
    render(<PositionsPanel positions={samplePositions} dailyPnl={0} />)
    // Click the YES outcome filter button (there are multiple "YES" texts —
    // the badge in the row + the filter button. Scope to the filter bar by
    // clicking the one inside the inline-flex container).
    const yesFilterBtn = screen.getByRole('button', { name: 'YES' })
    await user.click(yesFilterBtn)
    // tok-1 has YES shares → still visible. tok-2 has NO shares → hidden.
    expect(screen.getByText(/BITCOIN/)).toBeInTheDocument()
    expect(screen.queryByText(/ETHEREUM/)).not.toBeInTheDocument()
  })

  it('sorts by size descending (default sort) — largest invested first', () => {
    const { container } = render(
      <PositionsPanel positions={samplePositions} dailyPnl={0} />,
    )
    // The default sort is 'size' → b.total_invested - a.total_invested
    // tok-1 ($22.50) → first row, tok-2 ($18.00) → second row.
    const rows = container.querySelectorAll('tbody tr')
    expect(rows.length).toBe(2)
    const firstRowText = rows[0].textContent ?? ''
    expect(firstRowText).toContain('BITCOIN')
  })
})
