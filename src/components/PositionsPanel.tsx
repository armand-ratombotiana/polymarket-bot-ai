// components/PositionsPanel.tsx — Active Portfolio Positions, Real-Time P&L & Exposure Governance
//
// W15-5 — Migrated from `useBot`'s 2-second REST polling to the hybrid
// `useRealtimeData` hook. The panel now:
//   1. On mount, REST-prefetches /api/positions to populate state without
//      a flash of empty content.
//   2. In parallel, opens a WebSocket and subscribes to the `positions`
//      channel — when a message arrives with `msg.channel === 'positions'`,
//      the data state is swapped in atomically (sub-millisecond vs. the
//      previous 2s poll lag).
//   3. If the WS is not connected (handshaking / mid-reconnect / permanently
//      failed), falls back to polling /api/positions every 5s.
//   4. The header renders a "● Live" badge while the WS is connected, and
//      a "⟳ Polling" badge while it isn't — so the trader can tell at a
//      glance whether the displayed positions are real-time or lagged.
//
// Backwards-compat: callers MAY still pass `positions` as a prop (the
// existing tests do this, and page.tsx still threads the prop through).
// When provided, the prop overrides the fetched data — the WS subscription
// still runs (so `isRealtime` stays accurate), but the rendered rows come
// from the override. When omitted, the panel self-fetches via
// useRealtimeData.
'use client'

import { useState, useMemo, useCallback, memo } from 'react'
import { Position } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtPnl, fmtUsd } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'

interface PositionsApiResponse {
  positions: Position[]
}

interface Props {
  /**
   * Optional override for the positions list. When provided, the panel
   * uses this directly and skips rendering the useRealtimeData result
   * (the WS subscription still runs so the Live/Polling badge reflects
   * the actual transport state). When omitted, the panel self-fetches
   * via useRealtimeData('/api/positions', { wsChannel: 'positions' }).
   */
  positions?: Position[]
  dailyPnl: number
  onSelectMarket?: (market: { tokenId: string; slug: string }) => void
  onClosePosition?: (tokenId: string) => void
  priceFlashes?: Record<string, 'up' | 'down'>
  /**
   * Optional override for the realtime indicator. When omitted, the
   * panel derives the badge state from useRealtimeData's `isRealtime`
   * flag. Useful when the parent (e.g. page.tsx) is already tracking the
   * WS connection state via useBot and wants to drive the badge
   * consistently across sibling panels.
   */
  isRealtime?: boolean
  /**
   * W15-2 — preference flag. When false, the entire "Unrealized"
   * column (header `<th>` + every row's `<td>`) is hidden. Traders
   * who haven't reconciled exposure may want to hide this until
   * the backend reliably publishes `current_price`. Defaults to `true`
   * so every existing call site + existing test keeps the prior
   * behaviour.
   */
  showUnrealizedPnl?: boolean
  /**
   * W15-2 — preference flag. When false, the `.price-up` /
   * `.price-down` CSS class is suppressed on the Mark cell (traders
   * who find the flashing distracting). Defaults to `true` so every
   * existing call site + existing test keeps the prior behaviour.
   */
  showPriceFlashes?: boolean
}

// W9-6 — wrapped in React.memo with a custom comparator. The component
// receives `priceFlashes` (changes every ~500ms as flashes clear), so a
// shallow compare would cause many missed memo hits. The comparator below
// skips the priceFlashes object identity by diffing only the inputs that
// drive the rendered output: positions array reference, dailyPnl number,
// and the callback identities. priceFlashes is intentionally compared by
// JSON-stringified snapshot so two flashes maps with identical contents
// don't trigger a re-render (rare but possible).
function PositionsPanel({
  positions: positionsOverride,
  dailyPnl,
  onSelectMarket,
  onClosePosition,
  priceFlashes,
  isRealtime: isRealtimeOverride,
  showUnrealizedPnl = true,
  showPriceFlashes = true,
}: Props) {
  const [filterQuery, setFilterQuery] = useState('')
  const [outcomeFilter, setOutcomeFilter] = useState<'ALL' | 'YES' | 'NO'>('ALL')
  const [sortBy, setSortBy] = useState<'size' | 'pnl' | 'market'>('size')

  // W15-5 — hybrid REST + WS subscription. Always invoked (Rules of Hooks
  // forbid conditional calls), even when the caller passes a `positions`
  // override — the WS subscription still drives `isRealtime` so the
  // Live/Polling badge accurately reflects the transport state.
  const {
    data: fetched,
    isLoading,
    isRealtime: wsIsRealtime,
  } = useRealtimeData<PositionsApiResponse>('/api/positions', {
    wsChannel: 'positions',
    pollInterval: 5000, // was 2s under useBot's REST poll; relaxed to 5s
  })

  // Resolve the effective positions array + realtime flag. The override
  // takes precedence when provided (backwards-compat with tests + the
  // page.tsx wiring that still threads useBot's snapshot through).
  const positions = positionsOverride ?? fetched?.positions ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  const MAX_PER_MARKET = 3.0 // USD 3.00 institutional limit
  const MAX_TOTAL_PORTFOLIO = 25.0 // USD 25.00 total exposure cap

  // W9-6 — memoize aggregate reductions so they only recompute when the
  // positions array identity changes (not on every input/filter change).
  const totalInvested = useMemo(() => positions.reduce((acc, p) => acc + p.total_invested, 0), [positions])
  const totalRealized = useMemo(() => positions.reduce((acc, p) => acc + p.realised_pnl, 0), [positions])

  const filteredPositions = useMemo(() => {
    return positions
      .filter((p) => {
        const matchesQuery = p.slug.toLowerCase().includes(filterQuery.toLowerCase()) || p.token_id.includes(filterQuery)
        const isYes = p.yes_shares > 0
        const isNo = (p.no_shares ?? 0) > 0 || (!isYes && p.total_invested > 0)
        const matchesOutcome =
          outcomeFilter === 'ALL'
            ? true
            : outcomeFilter === 'YES'
            ? isYes
            : isNo
        return matchesQuery && matchesOutcome
      })
      .sort((a, b) => {
        if (sortBy === 'size') return b.total_invested - a.total_invested
        if (sortBy === 'pnl') return b.realised_pnl - a.realised_pnl
        return a.slug.localeCompare(b.slug)
      })
  }, [positions, filterQuery, outcomeFilter, sortBy])

  // W9-6 — wrap CSV export in useCallback so it isn't recreated on every
  // render (only depends on `positions`).
  const handleExportCsv = useCallback(() => {
    if (positions.length === 0) return
    const headers = ['Token ID', 'Market Slug', 'Outcome', 'Shares', 'Avg Entry Price', 'Total Cost USD', 'Realized PnL']
    const rows = positions.map((p) => [
      p.token_id,
      `"${p.slug.replace(/"/g, '""')}"`,
      p.yes_shares > 0 ? 'YES' : 'NO',
      p.yes_shares > 0 ? p.yes_shares.toFixed(2) : (p.no_shares ?? 0).toFixed(2),
      p.avg_entry_price.toFixed(4),
      p.total_invested.toFixed(4),
      p.realised_pnl.toFixed(4),
    ])
    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
    const encodedUri = encodeURI(csvContent)
    const link = document.createElement('a')
    link.setAttribute('href', encodedUri)
    link.setAttribute('download', `polymarket_positions_${Date.now()}.csv`)
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }, [positions])

  const portfolioExposurePct = Math.min((totalInvested / MAX_TOTAL_PORTFOLIO) * 100, 100)

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
      {/* Header with Stats Strip */}
      <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse" />
          <span className="card-title text-xs font-bold text-[#dde1ed] tracking-wide">
            💼 ACTIVE POSITIONS ({positions.length})
          </span>
          <span className="badge badge-amber text-[9.5px]">USD 25 Exposure Cap</span>
          {/* W15-5 — Live / Polling badge. Reflects the actual transport
              state of the underlying useRealtimeData subscription. The
              dot color + label give the trader an at-a-glance signal of
              whether the displayed rows are real-time (WS push) or
              lagged (5s poll fallback). */}
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>

        {/* Aggregate KPI Badges */}
        <div className="flex items-center gap-2 text-xs">
          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5" title="Total Invested / $25 Exposure Cap">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Exposure:</span>
            <span className="mono font-bold text-cyan-400 text-xs">{fmtUsd(totalInvested)}</span>
            <span className="text-[9.5px] text-[#5a637a]">({portfolioExposurePct.toFixed(0)}%)</span>
          </div>

          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Realized:</span>
            <span className={`mono font-bold text-xs ${totalRealized >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {fmtPnl(totalRealized)}
            </span>
          </div>

          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Daily PnL:</span>
            <span className={`mono font-bold text-xs ${dailyPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {fmtPnl(dailyPnl)}
            </span>
          </div>

          <button
            onClick={handleExportCsv}
            disabled={positions.length === 0}
            className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
            title="Export Positions CSV"
          >
            📥 CSV
          </button>
        </div>
      </div>

      {/* W15-5 — loading state. Only surfaces on the FIRST fetch (before
          useRealtimeData has resolved any data) AND when no `positions`
          override was passed. Once the initial REST fetch returns, the
          panel renders the table even when the WS is still handshaking —
          the Live/Polling badge in the header conveys the transport lag
          instead of blanking the panel. */}
      {isLoading && positions.length === 0 && (
        <div className="flex items-center justify-center py-8 text-xs text-[#7e8aaa]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading positions…
        </div>
      )}

      {/* Filter & Search Bar */}
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="relative flex-1 max-w-xs">
          <input
            type="text"
            placeholder="Search position by market / contract..."
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            aria-label="Search positions by market name or contract token ID"
            className="w-full bg-[#0e1015] border border-[#1f2335] focus:border-cyan-500/50 rounded text-xs px-2.5 py-1.5 text-[#dde1ed] placeholder-[#3e4560] outline-none transition-all"
          />
          {filterQuery && (
            <button
              onClick={() => setFilterQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[#7e8aaa] hover:text-white"
              aria-label="Clear search filter"
            >
              ✕
            </button>
          )}
        </div>

        <div className="flex items-center gap-1.5">
          {/* Outcome Filter */}
          <div className="inline-flex bg-[#0e1015] border border-[#1f2335] rounded p-0.5 text-[10px]" role="group" aria-label="Filter positions by outcome">
            {(['ALL', 'YES', 'NO'] as const).map((side) => (
              <button
                key={side}
                onClick={() => setOutcomeFilter(side)}
                aria-pressed={outcomeFilter === side}
                className={`px-2 py-0.5 rounded font-bold transition-all ${
                  outcomeFilter === side
                    ? 'bg-blue-500/20 text-cyan-300 shadow-sm'
                    : 'text-[#7e8aaa] hover:text-[#dde1ed]'
                }`}
              >
                {side}
              </button>
            ))}
          </div>

          {/* Sort Selector */}
          <select
            value={sortBy}
            onChange={(e) => setSortBy(e.target.value as any)}
            aria-label="Sort positions by"
            className="bg-[#0e1015] border border-[#1f2335] text-[#7e8aaa] rounded text-[10px] font-semibold px-2 py-1 outline-none cursor-pointer"
          >
            <option value="size">Sort: Size ($)</option>
            <option value="pnl">Sort: Realized P&L</option>
            <option value="market">Sort: Market Name</option>
          </select>
        </div>
      </div>

      {/* Positions Table */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {filteredPositions.length === 0 ? (
          <div className="empty-state">
            <span className="empty-state-icon text-2xl" aria-hidden="true">💼</span>
            <span className="empty-state-title text-sm font-semibold">No positions found</span>
            <span className="empty-state-desc text-xs max-w-sm text-center">
              {filterQuery || outcomeFilter !== 'ALL'
                ? 'No open positions match your active filters.'
                : 'Automated strategies (Market Maker, Arbitrage, Signal Trader) will populate live positions here.'}
            </span>
          </div>
        ) : (
          <table className="data-table text-xs w-full" role="table" aria-label="Portfolio open positions">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="min-w-[190px] py-1.5 text-left">Market Contract</th>
                <th scope="col" className="text-center">Outcome</th>
                <th scope="col" className="text-right">Shares</th>
                <th scope="col" className="text-right">Avg Entry</th>
                {/* S1 — live mark price column */}
                <th scope="col" className="text-right">Mark</th>
                <th scope="col" className="text-right">Cost Basis</th>
                <th scope="col" className="text-center min-w-[110px]">Cap Limit ($3 Max)</th>
                <th scope="col" className="text-right">Realized P&amp;L</th>
                {/* S1 — unrealized mark-to-market P&amp;L column.
                    W15-2: the entire column is hidden when the
                    `showUnrealizedPnl` preference is false. */}
                {showUnrealizedPnl && (
                  <th scope="col" className="text-right">Unrealized</th>
                )}
                <th scope="col" className="text-center">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {filteredPositions.map((p) => {
                const info = formatHierarchicalMarket(p.slug)
                const utilizationPct = Math.min((p.total_invested / MAX_PER_MARKET) * 100, 100)
                const isYes = p.yes_shares > 0
                const isNearCap = utilizationPct > 80
                // W12 — Resolve this row's price-flash direction once per render.
                // Undefined (no flash active) yields no extra class on the Mark cell.
                // W15-2 — when the `showPriceFlashes` preference is false the
                // CSS class is suppressed (the flashDir lookup still runs so the
                // memo comparator + downstream logic stay simple).
                const flashDir = priceFlashes?.[p.token_id]
                const flashClass =
                  showPriceFlashes && flashDir === 'up'
                    ? ' price-up'
                    : showPriceFlashes && flashDir === 'down'
                      ? ' price-down'
                      : ''

                return (
                  <tr
                    key={p.token_id}
                    className="hover:bg-blue-500/10 transition-colors group"
                  >
                    {/* Market Title */}
                    <td
                      className="py-2.5 max-w-[240px]"
                    >
                      <button
                        type="button"
                        onClick={() => onSelectMarket?.({ tokenId: p.token_id, slug: p.slug })}
                        className="w-full text-left bg-transparent border-0 p-0 cursor-pointer"
                        aria-label={`Open depth chart and trade modal for ${info.fullLabel}`}
                      >
                        <div className="flex flex-col gap-0.5">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[9px] text-cyan-400 font-bold uppercase tracking-wider truncate">
                              {info.category.icon} {info.eventTitle}
                            </span>
                          </div>
                          <span
                            className="text-[#dde1ed] group-hover:text-cyan-300 font-medium leading-snug text-xs block whitespace-normal transition-colors"
                            title={info.fullLabel}
                          >
                            {info.question}
                          </span>
                        </div>
                      </button>
                    </td>

                    {/* Outcome Badge */}
                    <td className="text-center">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[9.5px] font-bold uppercase tracking-wide ${
                          isYes
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : 'bg-red-500/15 text-red-400 border border-red-500/30'
                        }`}
                      >
                        {isYes ? 'YES' : 'NO'}
                      </span>
                    </td>

                    {/* Shares */}
                    <td className="mono text-right font-semibold text-[#dde1ed]">
                      {p.yes_shares > 0 ? p.yes_shares.toFixed(1) : (p.no_shares ?? 0).toFixed(1)}
                    </td>

                    {/* Avg Entry */}
                    <td className="mono text-right text-[#7e8aaa] text-xs">
                      ${p.avg_entry_price.toFixed(3)}
                    </td>

                    {/* S1 — Mark (current price). Falls back to "—" when the
                        backend hasn't populated current_price yet.
                        W12: apply .price-up / .price-down when a flash is
                        active for this row's token_id.
                        W15-2: flashClass is empty when the preference is
                        off so the cell renders plain. */}
                    <td className={`mono text-right text-[#dde1ed] text-xs${flashClass}`}>
                      {typeof p.current_price === 'number'
                        ? `$${p.current_price.toFixed(3)}`
                        : <span className="text-[#3e4560]">—</span>}
                    </td>

                    {/* Cost Basis */}
                    <td className="mono text-right font-semibold text-cyan-300">
                      {fmtUsd(p.total_invested)}
                    </td>

                    {/* Exposure Utilization Gauge */}
                    <td className="text-center px-2">
                      <div className="flex flex-col gap-1 items-center">
                        <div className="w-full bg-[#0e1015] border border-[#1f2335] h-1.5 rounded-full overflow-hidden">
                          <div
                            className={`h-full rounded-full transition-all ${
                              isNearCap ? 'bg-amber-400' : 'bg-cyan-400'
                            }`}
                            style={{ width: `${utilizationPct}%` }}
                          />
                        </div>
                        <span className="text-[9px] mono text-[#7e8aaa]">
                          {utilizationPct.toFixed(0)}% (${p.total_invested.toFixed(2)}/$3)
                        </span>
                      </div>
                    </td>

                    {/* Realized PnL */}
                    <td
                      className={`mono text-right font-bold text-xs ${
                        p.realised_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                      }`}
                    >
                      {fmtPnl(p.realised_pnl)}
                    </td>

                    {/* S1 — Unrealized P&amp;L (mark-to-market). Color-coded
                        green/red. Falls back to "—" when unrealized_pnl is
                        not provided by the backend.
                        W15-2 — the entire cell is hidden when the
                        `showUnrealizedPnl` preference is false (matches the
                        conditional `<th>` header). */}
                    {showUnrealizedPnl && (
                      <td
                        className={`mono text-right font-bold text-xs ${
                          typeof p.unrealized_pnl === 'number'
                            ? p.unrealized_pnl >= 0
                              ? 'text-green-400'
                              : 'text-red-400'
                            : 'text-[#3e4560]'
                        }`
                      }
                      >
                        {typeof p.unrealized_pnl === 'number'
                          ? fmtPnl(p.unrealized_pnl)
                          : '—'}
                      </td>
                    )}

                    {/* Action Button */}
                    <td className="text-center">
                      <div className="flex items-center justify-center gap-1">
                        <button
                          onClick={() => onSelectMarket?.({ tokenId: p.token_id, slug: p.slug })}
                          className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-cyan-400 hover:text-white hover:border-cyan-500/50"
                          title="Open Depth & Trade Modal"
                        >
                          Trade
                        </button>
                        {/* S1 — Close position button. Only invokes the handler
                            when onClosePosition is provided (additive prop). */}
                        <button
                          onClick={() => onClosePosition?.(p.token_id)}
                          className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-red-400 hover:text-white hover:border-red-500/50"
                          aria-label={`Close position for ${info.fullLabel}`}
                          title="Close position at market"
                        >
                          <span aria-hidden="true">✕</span> Close
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

// W9-6 — React.memo with a custom comparator. `positions` is the only
// prop whose identity changes frequently (every snapshot). `dailyPnl` is a
// primitive. `onSelectMarket` / `onClosePosition` are stable (useCallback
// in useBot + page.tsx). `priceFlashes` mutates on every tick, so we diff
// its keys/values with a JSON string rather than identity. When all four
// compare equal, the component skips re-rendering entirely — a meaningful
// win since this panel renders ~50 positions × ~9 columns on the command
// center grid plus its own dedicated tab.
//
// W15-5 — the `isRealtime` override is a primitive boolean; it's diffed
// inline alongside `dailyPnl`. The useRealtimeData hook lives inside the
// component and re-runs on every render — but its internal state updates
// (data / isLoading / isRealtime) trigger React's normal re-render path,
// so memo on the prop surface doesn't interfere with WS-driven re-renders.
export default memo(PositionsPanel, (prev, next) => {
  if (prev.positions !== next.positions) return false
  if (prev.dailyPnl !== next.dailyPnl) return false
  if (prev.onSelectMarket !== next.onSelectMarket) return false
  if (prev.onClosePosition !== next.onClosePosition) return false
  if (prev.isRealtime !== next.isRealtime) return false
  // W15-2 — preference flags are primitive booleans; diff inline so a
  // preference flip re-renders the table (column show/hide + flash class).
  if (prev.showUnrealizedPnl !== next.showUnrealizedPnl) return false
  if (prev.showPriceFlashes !== next.showPriceFlashes) return false
  // priceFlashes is intentionally compared by serialized contents.
  if (JSON.stringify(prev.priceFlashes) !== JSON.stringify(next.priceFlashes)) return false
  return true
})
