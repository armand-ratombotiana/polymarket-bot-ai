// components/ui/VirtualTable.tsx — High-performance virtualized table
//
// W16-6 — Virtual scrolling for large lists (100+ rows). Built on top of
// `react-window`'s `FixedSizeList` so only the visible window of rows is
// mounted in the DOM at any time. This keeps the TradesPanel (up to 100
// fills), ClosedPositionsPanel (up to 500 closed positions) and
// AuditLogPanel (up to 100 audit events) responsive even under load.
//
// Design notes:
//   • Header is rendered as a sticky flex row ABOVE the virtual list —
//     not inside it — so it never scrolls out of view.
//   • Each row is a flex row of fixed-width cells. The widths are
//     declared per-column (Column.width) so the header and the body
//     rows share the same column geometry — no layout drift.
//   • Row clicks are forwarded to `onRowClick(row)` when provided. The
//     row gets `cursor: pointer` only when a handler is supplied.
//   • Empty + loading states are rendered in-place so the surrounding
//     card doesn't collapse when there's nothing to show.
//   • Rows + cells carry `role="row"` / `role="cell"` (and the header
//     carries `role="columnheader"`) so screen readers + the testing
//     suite can navigate the table semantically without `<table>`.
'use client'

// react-window v2 exports `List` (v1 used `FixedSizeList`).
import { List } from 'react-window'
import { ReactNode, useRef, useCallback, useMemo } from 'react'

export interface Column {
  key: string
  label: string
  width: number
  render?: (row: any) => ReactNode
  align?: 'left' | 'right' | 'center'
}

interface VirtualTableProps {
  columns: Column[]
  data: any[]
  rowHeight?: number
  height?: number
  onRowClick?: (row: any) => void
  emptyState?: ReactNode
  loading?: boolean
}

export default function VirtualTable({
  columns,
  data,
  rowHeight = 40,
  height = 400,
  onRowClick,
  emptyState,
  loading,
}: VirtualTableProps) {
  // The list ref is hoisted so callers (or future extensions) can drive
  // `scrollToItem` programmatically — e.g. to surface a freshly filled
  // trade at the top of the TradesPanel.
  const listRef = useRef<List>(null)

  const totalWidth = useMemo(
    () => columns.reduce((sum, c) => sum + c.width, 0),
    [columns],
  )

  // Render a single row. `react-window` calls this with the absolute
  // `index` into `data` + a `style` object that positions the row inside
  // the scrollable viewport. We MUST spread `style` onto the root row
  // element — otherwise react-window can't position the row correctly
  // and the list will appear empty.
  //
  // The callback is memoized on [columns, data, onRowClick] so react-window
  // doesn't tear down + re-mount the row component on every parent render
  // (a known react-window perf pitfall when the child render prop is
  // re-created each render).
  const Row = useCallback(
    ({ index, style }: { index: number; style: React.CSSProperties }) => {
      const row = data[index]
      return (
        <div
          role="row"
          style={{
            ...style,
            display: 'flex',
            alignItems: 'center',
            borderBottom: '1px solid var(--border, #1f2335)',
            cursor: onRowClick ? 'pointer' : 'default',
          }}
          onClick={() => onRowClick?.(row)}
          data-row-index={index}
        >
          {columns.map((col) => (
            <div
              role="cell"
              key={col.key}
              style={{
                width: col.width,
                minWidth: col.width,
                padding: '0 8px',
                textAlign: col.align || 'left',
                fontSize: '12px',
                fontFamily: 'JetBrains Mono, monospace',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {col.render ? col.render(row) : String(row[col.key] ?? '')}
            </div>
          ))}
        </div>
      )
    },
    [columns, data, onRowClick],
  )

  // Loading state — surface BEFORE the empty check so a freshly-mounted
  // panel that's still fetching doesn't briefly flash "No data".
  if (loading) {
    return (
      <div
        role="status"
        style={{
          width: '100%',
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: 'var(--text-dim, #8b949e)',
          fontSize: '12px',
        }}
      >
        Loading…
      </div>
    )
  }

  if (!data.length) {
    return (
      <div
        style={{
          width: '100%',
          height,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
        }}
      >
        {emptyState || 'No data'}
      </div>
    )
  }

  return (
    <div
      role="table"
      aria-rowcount={data.length + 1}
      style={{ width: '100%', overflow: 'auto' }}
    >
      {/* Header — sticky above the virtualized body. The header widths
          mirror the column.width declarations so cells stay aligned. */}
      <div
        role="row"
        style={{
          display: 'flex',
          borderBottom: '2px solid var(--border, #1f2335)',
          background: 'var(--bg-surface, #13161e)',
        }}
      >
        {columns.map((col) => (
          <div
            role="columnheader"
            key={col.key}
            style={{
              width: col.width,
              minWidth: col.width,
              padding: '8px',
              fontSize: '11px',
              fontWeight: 600,
              textTransform: 'uppercase',
              letterSpacing: '0.05em',
              color: 'var(--text-dim, #8b949e)',
              textAlign: col.align || 'left',
            }}
          >
            {col.label}
          </div>
        ))}
      </div>

      {/* Virtual list — only the visible window of rows is mounted.
          `width` is the sum of all column widths so the body aligns
          with the header exactly. When the parent container is wider
          than `totalWidth`, the leftover space stays empty (consistent
          with the existing data-table styling); when narrower, the
          outer `overflow: auto` wrapper lets the trader scroll
          horizontally. */}
      <List
        ref={listRef}
        height={height}
        itemCount={data.length}
        itemSize={rowHeight}
        width={totalWidth}
      >
        {Row}
      </List>
    </div>
  )
}
