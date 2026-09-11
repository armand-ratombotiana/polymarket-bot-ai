// components/ui/VirtualTable.tsx — High-performance virtualized table
//
// W16-6 — Virtual scrolling for large lists (100+ rows). Built on top of
// `react-window`'s `List` (v2 API) so only the visible window of rows is
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
//
// react-window v2 notes:
//   • v2 replaced `FixedSizeList` with `List` and changed the props:
//     `itemCount` → `rowCount`, `itemSize` → `rowHeight`,
//     `children` (render-prop) → `rowComponent` + `rowProps`,
//     `ref` → `listRef`, `width`/`height` → `style` (or `defaultHeight`).
//   • `RowComponent` receives `{ index, style, ariaAttributes }` from
//     the List, plus whatever's in `rowProps` (here: `columns`, `data`,
//     `onRowClick`). We use `rowProps` so the row component can be
//     memoized externally and react-window can skip re-rendering rows
//     whose `rowProps` reference is stable.
'use client'

import { List, useListRef } from 'react-window'
// W28-1 — `useCallback` removed from the import list (TS6133 —
// RowComponent is a stable module-scope function, so no
// useCallback wrapper is needed). `type CSSProperties` added so we
// can annotate RowComponent's `style` prop without relying on the
// global React namespace.
import { type CSSProperties, ReactNode, useMemo } from 'react'

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

// Row component is defined at module scope (outside VirtualTable) so it
// has a stable reference across renders. react-window v2 re-renders rows
// when the `rowComponent` reference changes; a module-scope component
// prevents needless row re-mounts.
interface RowComponentProps {
  columns: Column[]
  data: any[]
  onRowClick?: (row: any) => void
}

// W28-1 — react-window v2's `List` expects `rowComponent` to accept
// `{ ariaAttributes, index, style } & RowProps`. Declaring all three
// reserved props here lets TS infer `RowProps = RowComponentProps`
// (the props we forward via `rowProps`) without `index` / `style`
// leaking in and tripping `ExcludeForbiddenKeys_2<RowProps>` on the
// `rowProps` prop. We don't destructure `ariaAttributes` (we don't
// render it — VirtualTable supplies its own ARIA via the `role`
// attributes on the row/cell divs), and TS doesn't flag unread
// properties in a destructuring pattern (only unread locals).
function RowComponent({
  index,
  style,
  columns,
  data,
  onRowClick,
}: RowComponentProps & {
  ariaAttributes: {
    'aria-posinset': number
    'aria-setsize': number
    role: 'listitem'
  }
  index: number
  style: CSSProperties
}) {
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
  // `scrollToRow` programmatically — e.g. to surface a freshly filled
  // trade at the top of the TradesPanel.
  const listRef = useListRef(null)

  const totalWidth = useMemo(
    () => columns.reduce((sum, c) => sum + c.width, 0),
    [columns],
  )

  // Memoize rowProps so the object reference is stable across renders
  // (when columns/data/onRowClick don't change). react-window v2 uses
  // this to skip row re-renders — a new object every render would
  // force every visible row to re-render on every parent update.
  const rowProps = useMemo<RowComponentProps>(
    () => ({ columns, data, onRowClick }),
    [columns, data, onRowClick],
  )

  // The row component is stable (module-scope). No useCallback needed
  // since RowComponent is already a stable reference.
  const rowComponent = RowComponent

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
        listRef={listRef}
        defaultHeight={height}
        rowCount={data.length}
        rowHeight={rowHeight}
        rowComponent={rowComponent}
        rowProps={rowProps}
        style={{ width: totalWidth, height }}
      />
    </div>
  )
}
