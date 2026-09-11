// components/ui/VirtualTable.test.tsx — Virtual scrolling table coverage.
//
// Verifies the contract documented in `VirtualTable.tsx`:
//   1. Renders the header labels + every visible row's cell content.
//   2. Renders the empty state (default + custom) when `data` is [].
//   3. Renders the loading state when `loading` is true.
//   4. Invokes `onRowClick` with the row payload when a row is clicked.
//   5. Honors per-column alignment (`left` / `right` / `center`).
//
// We use react-window's FixedSizeList under the hood, which only mounts
// the visible window of rows. To make the tests deterministic regardless
// of viewport height, we pass `height` large enough (default 400) to fit
// the small sample data sets, so every row is rendered.
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import VirtualTable, { Column } from './VirtualTable'

interface Row {
  id: number
  name: string
  price: number
}

const sampleData: Row[] = [
  { id: 1, name: 'BTC/USD', price: 42000.5 },
  { id: 2, name: 'ETH/USD', price: 2500.25 },
  { id: 3, name: 'SOL/USD', price: 105.75 },
]

const columns: Column[] = [
  { key: 'id', label: 'ID', width: 60, align: 'left' },
  { key: 'name', label: 'Market', width: 180, align: 'left' },
  { key: 'price', label: 'Price', width: 120, align: 'right' },
]

describe('VirtualTable', () => {
  // ── Step 6 spec: rendering with data ────────────────────────────────
  it('renders the column headers + visible row contents from data', () => {
    render(<VirtualTable columns={columns} data={sampleData} />)

    // All three column headers should render in the sticky header row.
    expect(screen.getByText('ID')).toBeInTheDocument()
    expect(screen.getByText('Market')).toBeInTheDocument()
    expect(screen.getByText('Price')).toBeInTheDocument()

    // Row cell contents (1, 2, 3 — the id column). String coercion of
    // the price column also surfaces in the DOM.
    expect(screen.getByText('BTC/USD')).toBeInTheDocument()
    expect(screen.getByText('ETH/USD')).toBeInTheDocument()
    expect(screen.getByText('SOL/USD')).toBeInTheDocument()

    // Numbers are stringified via `String(row[col.key])`.
    expect(screen.getByText('42000.5')).toBeInTheDocument()
    expect(screen.getByText('2500.25')).toBeInTheDocument()
    expect(screen.getByText('105.75')).toBeInTheDocument()
  })

  it('uses a custom render function when one is provided on a column', () => {
    const cols: Column[] = [
      { key: 'name', label: 'Market', width: 180 },
      {
        key: 'price',
        label: 'Price',
        width: 120,
        align: 'right',
        render: (row) => <span data-testid={`price-${row.id}`}>${row.price.toFixed(2)}</span>,
      },
    ]
    render(<VirtualTable columns={cols} data={sampleData} />)

    expect(screen.getByTestId('price-1')).toHaveTextContent('$42000.50')
    expect(screen.getByTestId('price-2')).toHaveTextContent('$2500.25')
    expect(screen.getByTestId('price-3')).toHaveTextContent('$105.75')
  })

  // ── Step 6 spec: empty state ────────────────────────────────────────
  it('renders the default "No data" empty state when data is []', () => {
    render(<VirtualTable columns={columns} data={[]} />)
    expect(screen.getByText('No data')).toBeInTheDocument()
    // Header shouldn't render when there's no data — the empty state
    // replaces the whole table.
    expect(screen.queryByText('Market')).not.toBeInTheDocument()
  })

  it('renders a custom emptyState node when provided', () => {
    render(
      <VirtualTable
        columns={columns}
        data={[]}
        emptyState={
          <div data-testid="custom-empty">
            📭 No trades yet — your bot is warming up.
          </div>
        }
      />,
    )
    expect(screen.getByTestId('custom-empty')).toBeInTheDocument()
    expect(screen.queryByText('No data')).not.toBeInTheDocument()
  })

  // ── Step 6 spec: loading state ──────────────────────────────────────
  it('renders the loading state when loading is true (regardless of data)', () => {
    const { rerender } = render(
      <VirtualTable columns={columns} data={sampleData} loading />,
    )
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.getByRole('status')).toBeInTheDocument()
    // Even though data was passed, the loading branch wins.
    expect(screen.queryByText('BTC/USD')).not.toBeInTheDocument()

    // Loading wins over empty state too.
    rerender(<VirtualTable columns={columns} data={[]} loading />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText('No data')).not.toBeInTheDocument()
  })

  // ── Step 6 spec: row click ──────────────────────────────────────────
  it('invokes onRowClick with the row payload when a row is clicked', async () => {
    const user = userEvent.setup()
    const onRowClick = vi.fn()
    render(
      <VirtualTable
        columns={columns}
        data={sampleData}
        onRowClick={onRowClick}
      />,
    )

    // Click on the row containing 'ETH/USD'.
    const row = screen.getByText('ETH/USD').closest('[role="row"]')
    expect(row).not.toBeNull()
    await user.click(row as HTMLElement)

    expect(onRowClick).toHaveBeenCalledTimes(1)
    expect(onRowClick).toHaveBeenCalledWith(
      expect.objectContaining({ id: 2, name: 'ETH/USD', price: 2500.25 }),
    )
  })

  it('does NOT attach a click cursor when onRowClick is omitted', () => {
    render(<VirtualTable columns={columns} data={sampleData} />)
    const row = screen.getByText('BTC/USD').closest('[role="row"]') as HTMLElement
    expect(row.style.cursor).toBe('default')
  })

  // ── Step 6 spec: column alignment ──────────────────────────────────
  it('honors per-column alignment (left / right / center) on header + cells', () => {
    const cols: Column[] = [
      { key: 'name', label: 'Market', width: 180, align: 'left' },
      { key: 'price', label: 'Price', width: 120, align: 'right' },
      { key: 'id', label: 'ID', width: 60, align: 'center' },
    ]
    render(<VirtualTable columns={cols} data={sampleData} />)

    // Header cells.
    const headerCells = screen
      .getAllByRole('columnheader')
      .map((el) => (el as HTMLElement).style.textAlign)
    expect(headerCells).toEqual(['left', 'right', 'center'])

    // Body cells — the first row's three cells.
    const firstRow = screen.getByText('BTC/USD').closest('[role="row"]')
    expect(firstRow).not.toBeNull()
    const bodyCells = Array.from(
      (firstRow as HTMLElement).querySelectorAll('[role="cell"]'),
    ).map((el) => (el as HTMLElement).style.textAlign)
    expect(bodyCells).toEqual(['left', 'right', 'center'])
  })

  it('defaults to left alignment when align is omitted on a column', () => {
    const cols: Column[] = [
      { key: 'name', label: 'Market', width: 180 },
    ]
    render(<VirtualTable columns={cols} data={sampleData} />)
    const header = screen.getByRole('columnheader') as HTMLElement
    expect(header.style.textAlign).toBe('left')
  })

  // ── Extra: respects custom rowHeight + height ───────────────────────
  it('passes height + rowHeight through to FixedSizeList without throwing', () => {
    // Renders with a custom geometry. We don't assert pixel math (jsdom
    // doesn't lay out), but verifying it renders without throwing
    // confirms the prop wiring.
    expect(() =>
      render(
        <VirtualTable
          columns={columns}
          data={sampleData}
          rowHeight={48}
          height={240}
        />,
      ),
    ).not.toThrow()
    expect(screen.getByText('BTC/USD')).toBeInTheDocument()
  })
})
