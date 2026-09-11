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
// W39-5 — Redesigned for trading-operations clarity:
//   • Right-aligned numeric columns (Shares, Avg Entry, Mark, Cost Basis,
//     Realized P&L, Unrealized).
//   • Color-coded P&L (green positive, red negative) — already present,
//     re-affirmed.
//   • Strategy badge in the Market Contract cell (when `position.strategy`
//     is provided by the snapshot).
//   • Risk status indicator dot (green/amber/red) next to the outcome
//     badge. When `position.risk_status` is provided it wins; otherwise
//     the panel derives a status from the unrealized P&L magnitude:
//     green when positive or zero, amber when the loss is < 10% of cost
//     basis, red when ≥ 10%.
//   • "Time held" column with human-readable "3h 24m" formatting, derived
//     from `position.opened_at` (when provided). Hidden when no position
//     in the visible set exposes an `opened_at` timestamp.
//   • Close Position button restyled as an explicitly destructive action —
//     filled red background, "✕ Close" copy, and (when the new
//     `requireConfirmation` prop is true) opens an inline ConfirmationDialog
//     before invoking `onClosePosition`, with a position-specific impact
//     summary ("Size: 10 shares, Mark: $0.55, Est. proceeds: $5.50") +
//     risk warning.
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
import { fmtPnl, fmtUsd, fmtDurationHm, fmtTimeAbs, fmtPrice } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { useStaleAge } from '@/hooks/useStaleAge'
import { Badge } from '@/components/ui/badge'
import { ErrorState, StaleIndicator } from '@/components/ui/states'
import ConfirmationDialog from './ConfirmationDialog'

interface PositionsApiResponse {
  positions: Position[]
}

type RiskStatus = 'healthy' | 'warning' | 'danger'

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
   * flag.
   */
  isRealtime?: boolean
  showUnrealizedPnl?: boolean
  showPriceFlashes?: boolean
  /**
   * W39-5 — when true, clicking the Close Position button opens an inline
   * ConfirmationDialog before invoking `onClosePosition`. Defaults to
   * `false` so existing tests (which pass onClosePosition and click
   * Close directly) keep their direct-call behaviour; page.tsx opts in
   * to confirmation for production safety.
   */
  requireConfirmation?: boolean
}

// W39-5 — derive a risk-status dot color from unrealized P&L + cost basis.
// `risk_status` (when provided by the backend risk engine) wins; otherwise
// we apply a conservative heuristic:
//   • green  — unrealized_pnl ≥ 0 (in profit, or breakeven)
//   • amber  — loss < 10% of total_invested (small drawdown, within risk tolerance)
//   • red    — loss ≥ 10% of total_invested (material drawdown, attention needed)
// When unrealized_pnl isn't published, we degrade gracefully to amber
// (signalling "unmeasured" rather than falsely green).
function deriveRiskStatus(p: Position): RiskStatus {
  if (p.risk_status) return p.risk_status
  if (typeof p.unrealized_pnl !== 'number') return 'warning'
  if (p.unrealized_pnl >= 0) return 'healthy'
  const lossPct = p.total_invested > 0 ? Math.abs(p.unrealized_pnl) / p.total_invested : 0
  return lossPct >= 0.1 ? 'danger' : 'warning'
}

const RISK_DOT_CLASS: Record<RiskStatus, string> = {
  healthy: 'bg-green-400',
  warning: 'bg-amber-400',
  danger:  'bg-red-400',
}

const RISK_DOT_TITLE: Record<RiskStatus, string> = {
  healthy: 'Risk: Healthy (in profit or breakeven)',
  warning: 'Risk: Watch (small drawdown or unmeasured)',
  danger:  'Risk: Material drawdown — review exposure',
}

// W39-5 — Strategy badge class. The strategy string is rendered verbatim
// (e.g. "mm_avellaneda_stoikov") inside a neutral chip. Trailing "manual"
// (case-insensitive) is highlighted as a human override.
function StrategyBadge({ strategy }: { strategy: string }) {
  const isManual = /manual/i.test(strategy)
  return (
    <span
      className={`inline-flex items-center px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wide border ${
        isManual
          ? 'bg-purple-500/15 text-purple-300 border-purple-500/30'
          : 'bg-cyan-500/10 text-cyan-300 border-cyan-500/30'
      }`}
      title={`Strategy: ${strategy}`}
    >
      {strategy}
    </span>
  )
}

// W9-6 — wrapped in React.memo with a custom comparator. See the comment
// at the bottom of the file for the full reasoning.
function PositionsPanel({
  positions: positionsOverride,
  dailyPnl,
  onSelectMarket,
  onClosePosition,
  priceFlashes,
  isRealtime: isRealtimeOverride,
  showUnrealizedPnl = true,
  showPriceFlashes = true,
  requireConfirmation = false,
}: Props) {
  const [filterQuery, setFilterQuery] = useState('')
  const [outcomeFilter, setOutcomeFilter] = useState<'ALL' | 'YES' | 'NO'>('ALL')
  const [sortBy, setSortBy] = useState<'size' | 'pnl' | 'market'>('size')
  // W39-5 — token id of the position the trader is currently confirming
  // a Close on. When non-null, the inline ConfirmationDialog is rendered.
  const [confirmCloseTokenId, setConfirmCloseTokenId] = useState<string | null>(null)

  const {
    data: fetched,
    isLoading,
    isRealtime: wsIsRealtime,
    error,
    lastUpdated,
    refetch,
  } = useRealtimeData<PositionsApiResponse>('/api/positions', {
    wsChannel: 'positions',
    pollInterval: 5000,
  })

  const positions = positionsOverride ?? fetched?.positions ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  // W41-3 — compute the data's age so we can surface a StaleIndicator
  // in the header when the snapshot is older than 30s (stale) or 120s
  // (dead). Skipped when the caller provides an override (the override
  // doesn't expose a timestamp; the parent's snapshot freshness is its
  // own concern).
  const age = useStaleAge(positionsOverride == null ? lastUpdated : null)

  const MAX_PER_MARKET = 3.0 // USD 3.00 institutional limit
  const MAX_TOTAL_PORTFOLIO = 25.0 // USD 25.00 total exposure cap

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

  // W39-5 — only render the "Time Held" column when at least one visible
  // position exposes an `opened_at` timestamp. Hiding the column entirely
  // when no row has data avoids an empty header in the paper-trading
  // snapshot (which doesn't currently publish `opened_at`).
  const showTimeHeldColumn = useMemo(
    () => filteredPositions.some((p) => typeof p.opened_at === 'number'),
    [filteredPositions],
  )

  // W39-5 — the position currently pending Close confirmation (when
  // `requireConfirmation` is true). Looked up by token_id so the dialog
  // can render a position-specific impact summary.
  const confirmingPosition = useMemo(
    () => (confirmCloseTokenId ? positions.find((p) => p.token_id === confirmCloseTokenId) ?? null : null),
    [confirmCloseTokenId, positions],
  )

  const handleExportCsv = useCallback(() => {
    if (positions.length === 0) return
    const headers = ['Token ID', 'Market Slug', 'Outcome', 'Shares', 'Avg Entry Price', 'Total Cost USD', 'Realized PnL', 'Strategy', 'Opened At']
    const rows = positions.map((p) => [
      p.token_id,
      `"${p.slug.replace(/"/g, '""')}"`,
      p.yes_shares > 0 ? 'YES' : 'NO',
      p.yes_shares > 0 ? p.yes_shares.toFixed(2) : (p.no_shares ?? 0).toFixed(2),
      p.avg_entry_price.toFixed(4),
      p.total_invested.toFixed(4),
      p.realised_pnl.toFixed(4),
      p.strategy ?? 'manual',
      p.opened_at ? new Date(p.opened_at * 1000).toISOString() : '',
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

  // W39-5 — Close handler. When `requireConfirmation` is true, the click
  // opens the inline ConfirmationDialog (which then calls onClosePosition
  // on confirm). When false, the click calls onClosePosition directly —
  // preserves the legacy direct-call behaviour that the existing tests
  // assert against.
  const handleCloseClick = useCallback(
    (tokenId: string) => {
      if (requireConfirmation) {
        setConfirmCloseTokenId(tokenId)
      } else {
        onClosePosition?.(tokenId)
      }
    },
    [requireConfirmation, onClosePosition],
  )

  const handleConfirmClose = useCallback(() => {
    if (confirmCloseTokenId) {
      onClosePosition?.(confirmCloseTokenId)
    }
    setConfirmCloseTokenId(null)
  }, [confirmCloseTokenId, onClosePosition])

  const handleCancelClose = useCallback(() => {
    setConfirmCloseTokenId(null)
  }, [])

  // W39-5 — pre-compute the impact summary string for the dialog so the
  // trader sees exactly what closing will do before confirming. Falls back
  // gracefully when mark price / shares aren't available.
  const confirmImpact = useMemo(() => {
    if (!confirmingPosition) return ''
    const shares = confirmingPosition.yes_shares > 0
      ? confirmingPosition.yes_shares
      : (confirmingPosition.no_shares ?? 0)
    const mark = typeof confirmingPosition.current_price === 'number'
      ? confirmingPosition.current_price
      : null
    const proceeds = mark !== null ? shares * mark : null
    const parts: string[] = [`Size: ${shares.toFixed(1)} shares`]
    if (mark !== null) {
      parts.push(`Mark: ${fmtPrice(mark)}`)
      if (proceeds !== null) parts.push(`Est. proceeds: ${fmtUsd(proceeds)}`)
    } else {
      parts.push(`Cost basis: ${fmtUsd(confirmingPosition.total_invested)}`)
    }
    return parts.join(' · ')
  }, [confirmingPosition])

  const confirmDescription = useMemo(() => {
    if (!confirmingPosition) return ''
    const info = formatHierarchicalMarket(confirmingPosition.slug)
    const isYes = confirmingPosition.yes_shares > 0
    return `Close position for ${info.fullLabel} (${isYes ? 'YES' : 'NO'})? This submits a marketable opposing order to flatten your exposure immediately.`
  }, [confirmingPosition])

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
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
          {/* W41-3 — StaleIndicator renders as an inline amber/red pill
              when the fetched snapshot is older than 30s. Hidden while
              fresh (<30s) so the header doesn't accumulate noise. Skipped
              when the caller provides a positions override (no timestamp
              surfaced from the override). */}
          {age !== null && <StaleIndicator age={age} />}
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

      {isLoading && positions.length === 0 && (
        <div className="flex items-center justify-center py-8 text-xs text-[#7e8aaa]">
          <span className="spinner mr-2" aria-hidden="true" />
          Loading positions…
        </div>
      )}

      {/* W41-3 — Error state. Rendered only when the initial REST fetch
          failed AND no override was supplied (the override short-circuits
          the loading gate; an error from the underlying hook is irrelevant
          in that case). Includes a Retry button that calls the hook's
          refetch(), which re-runs the initial fetch + clears the error. */}
      {!isLoading && error && positionsOverride == null && positions.length === 0 && (
        <ErrorState
          message="Positions unavailable"
          detail={error}
          onRetry={refetch}
          retryLabel="Retry"
        />
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
                <th scope="col" className="text-center">Risk</th>
                <th scope="col" className="text-center">Outcome</th>
                <th scope="col" className="text-right">Shares</th>
                <th scope="col" className="text-right">Avg Entry</th>
                <th scope="col" className="text-right">Mark</th>
                <th scope="col" className="text-right">Cost Basis</th>
                <th scope="col" className="text-center min-w-[110px]">Cap Limit ($3 Max)</th>
                <th scope="col" className="text-right">Realized P&amp;L</th>
                {showUnrealizedPnl && (
                  <th scope="col" className="text-right">Unrealized</th>
                )}
                {showTimeHeldColumn && (
                  <th scope="col" className="text-right">Time Held</th>
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
                const flashDir = priceFlashes?.[p.token_id]
                const flashClass =
                  showPriceFlashes && flashDir === 'up'
                    ? ' price-up'
                    : showPriceFlashes && flashDir === 'down'
                      ? ' price-down'
                      : ''
                // W39-5 — risk status dot derivation per row.
                const riskStatus = deriveRiskStatus(p)

                return (
                  <tr
                    key={p.token_id}
                    className="hover:bg-blue-500/10 transition-colors group"
                  >
                    {/* Market Title — includes the strategy badge when
                        the snapshot provides one (W39-5). */}
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
                          <div className="flex items-center gap-1.5 flex-wrap">
                            <span className="text-[9px] text-cyan-400 font-bold uppercase tracking-wider truncate">
                              {info.category.icon} {info.eventTitle}
                            </span>
                            {p.strategy && <StrategyBadge strategy={p.strategy} />}
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

                    {/* W39-5 — Risk status indicator dot. Title attribute
                        exposes the human-readable risk classification to
                        screen readers + hover tooltips. */}
                    <td className="text-center">
                      <span
                        className={`inline-block w-2.5 h-2.5 rounded-full ${RISK_DOT_CLASS[riskStatus]}`}
                        role="img"
                        aria-label={RISK_DOT_TITLE[riskStatus]}
                        title={RISK_DOT_TITLE[riskStatus]}
                      />
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

                    {/* Mark */}
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
                        `showUnrealizedPnl` preference is false. */}
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

                    {/* W39-5 — Time Held column. Hidden entirely when no
                        visible position exposes `opened_at`. The title
                        attribute carries the absolute timestamp for hover
                        tooltips + screen-reader context. */}
                    {showTimeHeldColumn && (
                      <td
                        className="mono text-right text-[#7e8aaa] text-[10.5px]"
                        title={typeof p.opened_at === 'number' ? fmtTimeAbs(p.opened_at) : undefined}
                      >
                        {typeof p.opened_at === 'number' ? fmtDurationHm(p.opened_at) : '—'}
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
                        {/* W39-5 — Close Position button restyled as an
                            explicitly destructive action. Filled red
                            background (not just red text), explicit
                            "✕" icon, and the destructive-action
                            aria-label. When `requireConfirmation` is
                            true, the click opens the ConfirmationDialog
                            instead of calling onClosePosition directly. */}
                        <button
                          onClick={() => handleCloseClick(p.token_id)}
                          className="btn btn-sm text-[10px] px-2 py-0.5 border border-red-500/40 bg-red-500/10 text-red-400 hover:text-white hover:bg-red-500/30 hover:border-red-500/60 font-bold flex items-center gap-1"
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

      {/* W39-5 — Close Position confirmation dialog. Rendered inline so
          the panel can drive its own impact summary from the live
          position snapshot (size + mark + estimated proceeds) without
          threading every position through the parent. */}
      <ConfirmationDialog
        open={confirmCloseTokenId !== null && confirmingPosition !== null}
        severity="danger"
        title="Close Position?"
        description={confirmDescription}
        impact={confirmImpact}
        riskWarning="Market close executes immediately at the best available price. On thin books this may slip materially below the displayed mark — review the order book depth before confirming."
        confirmLabel="✕ Close Position"
        cancelLabel="Keep Position"
        onConfirm={handleConfirmClose}
        onCancel={handleCancelClose}
      />
    </div>
  )
}

// W9-6 — React.memo with a custom comparator. See the original (pre-W39-5)
// header comment for the full reasoning. The W39-5 additions are:
//   • `requireConfirmation` is a primitive boolean, diffed inline so a
//     parent flipping the confirmation preference re-renders the panel.
export default memo(PositionsPanel, (prev, next) => {
  if (prev.positions !== next.positions) return false
  if (prev.dailyPnl !== next.dailyPnl) return false
  if (prev.onSelectMarket !== next.onSelectMarket) return false
  if (prev.onClosePosition !== next.onClosePosition) return false
  if (prev.isRealtime !== next.isRealtime) return false
  if (prev.showUnrealizedPnl !== next.showUnrealizedPnl) return false
  if (prev.showPriceFlashes !== next.showPriceFlashes) return false
  if (prev.requireConfirmation !== next.requireConfirmation) return false
  if (JSON.stringify(prev.priceFlashes) !== JSON.stringify(next.priceFlashes)) return false
  return true
})
