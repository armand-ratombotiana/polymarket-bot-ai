// components/TradesPanel.tsx — Recent Trade Executions Feed
//
// W22-5 — Migrated from the implicit "parent-passes-snapshot.recent_trades"
// pattern (the parent's useBot poll drives updates) to the hybrid
// `useRealtimeData` hook. The panel now:
//   1. REST-prefetches /api/trades?limit=100 on mount.
//   2. Subscribes to the `trades` WS channel for live push updates.
//   3. Falls back to polling /api/trades?limit=100 every 10s when the
//      WS isn't connected.
//   4. Renders a "● Live" / "⟳ Polling" badge so the trader can tell
//      at a glance whether the executions list is real-time or lagged.
//
// W39-5 — Redesigned for clearer execution audit clarity:
//   • Trade direction indicator ↑ (BUY, green) / ↓ (SELL, red) prepended
//     to the side badge so the direction is scannable at a glance even
//     without reading the BUY/SELL text.
//   • Fees + Slippage columns. Both fall back to "—" when the snapshot
//     doesn't expose them (the Trade interface marks both optional).
//   • Audit trail link icon (📋) next to the strategy tag. When the
//     `onViewAuditTrail` callback is provided (page.tsx wires it to
//     switch the active sidebar section to the Decision Ledger), the
//     icon is a clickable button that surfaces the trade's
//     `decision_id` (or falls back to `trade_id`) so the trader can
//     jump to the decision ledger for full PREDICTION → SIGNAL → RISK
//     → ORDER → FILL audit chain. Hidden when no callback is provided
//     so existing tests (which don't pass onViewAuditTrail) still pass.
//   • Timestamp rendered as relative ("3m ago") with the absolute ISO
//     timestamp surfaced via the title attribute for hover + screen
//     reader context.
//
// Backwards-compat: callers MAY still pass `trades` as a prop.
'use client'

import { useState, useMemo, useCallback, memo } from 'react'
import { Trade } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtPnl, fmtUsd, fmtTimeAbs } from '@/lib/design-tokens'
import { useRealtimeData } from '@/hooks/useRealtimeData'
import { Badge } from '@/components/ui/badge'

interface TradesApiResponse {
  trades?: Trade[]
}

interface Props {
  trades?: Trade[]
  isRealtime?: boolean
  /**
   * W39-5 — optional audit-trail callback. When provided, the panel
   * renders a 📋 link icon next to each trade's strategy tag; clicking
   * invokes this callback with the trade's `decision_id` (or
   * `trade_id` fallback) so the parent (page.tsx) can switch to the
   * Decision Ledger panel filtered to that decision. When omitted,
   * the audit icon is hidden — preserves the existing test contract.
   */
  onViewAuditTrail?: (decisionId: string) => void
}

function TradesPanel({ trades: tradesOverride, isRealtime: isRealtimeOverride, onViewAuditTrail }: Props) {
  const {
    data: fetched,
    isLoading,
    isRealtime: wsIsRealtime,
  } = useRealtimeData<TradesApiResponse>('/api/trades?limit=100', {
    wsChannel: 'trades',
    pollInterval: 10000,
  })

  const trades = tradesOverride ?? fetched?.trades ?? []
  const isRealtime = isRealtimeOverride ?? wsIsRealtime

  const [filterQuery, setFilterQuery] = useState('')
  const [sideFilter, setSideFilter] = useState<'ALL' | 'BUY' | 'SELL'>('ALL')
  const [copiedId, setCopiedId] = useState<string | null>(null)

  const filteredTrades = useMemo(() => {
    return trades.filter((t) => {
      const matchesSearch =
        t.slug.toLowerCase().includes(filterQuery.toLowerCase()) ||
        (t.trade_id && t.trade_id.includes(filterQuery)) ||
        (t.strategy && t.strategy.toLowerCase().includes(filterQuery.toLowerCase()))
      const matchesSide = sideFilter === 'ALL' || t.side.toUpperCase() === sideFilter
      return matchesSearch && matchesSide
    })
  }, [trades, filterQuery, sideFilter])

  const displayedTrades = useMemo(() => filteredTrades.slice(0, 100), [filteredTrades])

  const stats = useMemo(() => {
    const totalVol = trades.reduce((acc, t) => acc + (t.size * t.price), 0)
    const netPnl = trades.reduce((acc, t) => acc + (t.pnl || 0), 0)
    const wins = trades.filter((t) => (t.pnl || 0) > 0).length
    const closed = trades.filter((t) => (t.pnl || 0) !== 0).length
    const winRate = closed > 0 ? (wins / closed) * 100 : 0
    return { totalVol, netPnl, winRate, totalCount: trades.length }
  }, [trades])

  // W39-5 — aggregate fees + average slippage for the header KPI strip.
  // Both degrade to "—" when no trade in the visible set exposes the
  // optional fee/slippage_bps fields.
  const totalFees = useMemo(
    () => trades.reduce((acc, t) => acc + (typeof t.fee === 'number' ? t.fee : 0), 0),
    [trades],
  )
  const hasFees = useMemo(() => trades.some((t) => typeof t.fee === 'number'), [trades])
  const avgSlippageBps = useMemo(() => {
    const withSlip = trades.filter((t) => typeof t.slippage_bps === 'number')
    if (withSlip.length === 0) return null
    return withSlip.reduce((acc, t) => acc + (t.slippage_bps as number), 0) / withSlip.length
  }, [trades])

  const handleExportCsv = useCallback(() => {
    if (trades.length === 0) return
    const headers = ['Trade ID', 'Timestamp', 'Market Slug', 'Side', 'Price', 'Shares', 'P&L', 'Fee', 'Slippage (bps)', 'Strategy', 'Decision ID']
    const rows = trades.map((t) => [
      t.trade_id,
      new Date(t.timestamp).toISOString(),
      `"${t.slug.replace(/"/g, '""')}"`,
      t.side,
      t.price.toFixed(4),
      t.size.toFixed(2),
      t.pnl.toFixed(4),
      typeof t.fee === 'number' ? t.fee.toFixed(4) : '',
      typeof t.slippage_bps === 'number' ? t.slippage_bps.toFixed(2) : '',
      t.strategy || 'manual',
      t.decision_id ?? '',
    ])
    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
    const encodedUri = encodeURI(csvContent)
    const link = document.createElement('a')
    link.setAttribute('href', encodedUri)
    link.setAttribute('download', `polymarket_executions_${Date.now()}.csv`)
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }, [trades])

  const copyToClipboard = useCallback((text: string, id: string) => {
    navigator.clipboard.writeText(text)
    setCopiedId(id)
    setTimeout(() => setCopiedId(null), 1500)
  }, [])

  // W39-5 — stable callback for the audit-trail link icon. Falls back to
  // trade_id when decision_id isn't published by the backend (preserves
  // a usable audit jump target in either case).
  const handleViewAudit = useCallback(
    (trade: Trade) => {
      const target = trade.decision_id ?? trade.trade_id
      onViewAuditTrail?.(target)
    },
    [onViewAuditTrail],
  )

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
      <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Recent Executions ({filteredTrades.length})
          </span>
          <span className="badge badge-green text-[9.5px]">Audit Stream</span>
          {isRealtime ? (
            <Badge variant="success" className="text-[9.5px] py-0.5">● Live</Badge>
          ) : (
            <Badge variant="warning" className="text-[9.5px] py-0.5">⟳ Polling</Badge>
          )}
        </div>

        <div className="flex items-center gap-2 text-xs">
          <div className="bg-[#0e1015] border border-[#1f2335] px-2 py-0.5 rounded flex items-center gap-1">
            <span className="text-[9.5px] text-[#7e8aaa] uppercase font-semibold">Vol:</span>
            <span className="mono font-bold text-cyan-400 text-xs">{fmtUsd(stats.totalVol)}</span>
          </div>
          <div className="bg-[#0e1015] border border-[#1f2335] px-2 py-0.5 rounded flex items-center gap-1">
            <span className="text-[9.5px] text-[#7e8aaa] uppercase font-semibold">Net P&amp;L:</span>
            <span className={`mono font-bold text-xs ${stats.netPnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
              {fmtPnl(stats.netPnl)}
            </span>
          </div>
          {/* W39-5 — Fees KPI. Hidden when no trade in the visible set
              exposes the optional `fee` field, so the header doesn't
              show a misleading "$0.00" for paper-trading snapshots. */}
          {hasFees && (
            <div className="bg-[#0e1015] border border-[#1f2335] px-2 py-0.5 rounded flex items-center gap-1" title="Total fees paid on the visible trade set">
              <span className="text-[9.5px] text-[#7e8aaa] uppercase font-semibold">Fees:</span>
              <span className="mono font-bold text-amber-300 text-xs">{fmtUsd(totalFees)}</span>
            </div>
          )}
          {/* W39-5 — Average slippage KPI. Hidden when no trade exposes
              `slippage_bps` (paper-trading snapshots don't currently
              measure slippage vs. the quoted mid). */}
          {avgSlippageBps !== null && (
            <div className="bg-[#0e1015] border border-[#1f2335] px-2 py-0.5 rounded flex items-center gap-1" title="Average slippage vs. quoted mid (basis points)">
              <span className="text-[9.5px] text-[#7e8aaa] uppercase font-semibold">Avg Slip:</span>
              <span className={`mono font-bold text-xs ${avgSlippageBps >= 0 ? 'text-amber-300' : 'text-green-400'}`}>
                {avgSlippageBps >= 0 ? '+' : '−'}{Math.abs(avgSlippageBps).toFixed(1)} bps
              </span>
            </div>
          )}
          <button
            onClick={handleExportCsv}
            disabled={trades.length === 0}
            className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
            title="Export CSV Audit Trail"
          >
            📥 CSV
          </button>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="inline-flex bg-[#0e1015] border border-[#1f2335] rounded p-0.5 text-[10px]">
          {(['ALL', 'BUY', 'SELL'] as const).map((s) => (
            <button
              key={s}
              onClick={() => setSideFilter(s)}
              className={`px-2 py-0.5 rounded font-bold transition-all ${
                sideFilter === s
                  ? 'bg-blue-500/20 text-cyan-300 shadow-sm'
                  : 'text-[#7e8aaa] hover:text-[#dde1ed]'
              }`}
            >
              {s}
            </button>
          ))}
        </div>

        <div className="relative flex-1 max-w-xs">
          <input
            type="text"
            placeholder="Search fills by market, strategy, or trade ID…"
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            className="w-full text-xs bg-[#0e1015] border border-[#1f2335] focus:border-cyan-500/50 rounded px-2.5 py-1 text-[#dde1ed] placeholder-[#3e4560] outline-none"
            aria-label="Search trade fills"
          />
          {filterQuery && (
            <button
              onClick={() => setFilterQuery('')}
              className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-[#7e8aaa] hover:text-white"
            >
              ✕
            </button>
          )}
        </div>
      </div>

      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {isLoading && trades.length === 0 ? (
          <div className="flex items-center justify-center py-12 text-xs text-[#7e8aaa]">
            <span className="spinner mr-2" aria-hidden="true" />
            Loading recent executions…
          </div>
        ) : filteredTrades.length === 0 ? (
          <div className="empty-state py-6">
            <span className="empty-state-icon text-2xl" aria-hidden="true">⚡</span>
            <span className="empty-state-title text-sm font-semibold">No executed trades</span>
            <span className="empty-state-desc text-xs text-center max-w-xs">
              {filterQuery || sideFilter !== 'ALL'
                ? 'No executions match your active filter.'
                : 'Fills will appear here as orders match against live Polymarket books.'}
            </span>
          </div>
        ) : (
          <table className="data-table text-xs" role="table" aria-label="Recent trade execution log">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="min-w-[180px] text-left">Market Contract</th>
                <th scope="col" className="text-center">Side</th>
                <th scope="col" className="text-right">Price</th>
                <th scope="col" className="text-right">Shares</th>
                <th scope="col" className="text-right">Value</th>
                {/* W39-5 — Fees + Slippage columns. Always rendered (so
                    the header row stays consistent) — individual cells
                    fall back to "—" when the snapshot doesn't expose
                    the optional field. */}
                <th scope="col" className="text-right">Fee</th>
                <th scope="col" className="text-right">Slippage</th>
                <th scope="col" className="text-right">P&amp;L</th>
                <th scope="col" className="text-right">Strategy</th>
                <th scope="col" className="text-right">Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/40">
              {displayedTrades.map((t) => {
                const info = formatHierarchicalMarket(t.slug)
                const tradeVal = t.size * t.price
                const isBuy = t.side.toUpperCase() === 'BUY'
                // W39-5 — direction indicator glyph prepended to the
                // side badge so BUY/SELL is scannable by shape alone.
                const dirGlyph = isBuy ? '↑' : '↓'
                return (
                  <tr key={t.trade_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="py-2 max-w-[200px]">
                      <div className="flex flex-col gap-0.5">
                        <span className="text-[9px] text-cyan-400 uppercase font-bold tracking-wider truncate">
                          {info.category.icon} {info.eventTitle}
                        </span>
                        <span className="text-[#dde1ed] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                          {info.question}
                        </span>
                        <span
                          onClick={() => copyToClipboard(t.trade_id, t.trade_id)}
                          className="text-[9px] text-[#5a637a] hover:text-cyan-300 mono cursor-pointer w-fit"
                          title="Click to copy Trade ID"
                        >
                          {copiedId === t.trade_id ? '✓ Copied ID' : `ID: ${t.trade_id.slice(0, 10)}…`}
                        </span>
                      </div>
                    </td>
                    {/* W39-5 — Side badge with direction glyph. ↑ BUY is
                        tinted green; ↓ SELL is tinted red. The glyph
                        sits to the LEFT of the BUY/SELL text so the
                        direction is the first thing the eye catches. */}
                    <td className="text-center">
                      <span
                        className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-[9px] font-bold ${
                          isBuy
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : 'bg-red-500/15 text-red-400 border border-red-500/30'
                        }`}
                        title={isBuy ? 'Buy (long open / short close)' : 'Sell (long close / short open)'}
                      >
                        <span aria-hidden="true" className="text-[11px] leading-none">{dirGlyph}</span>
                        {t.side}
                      </span>
                    </td>
                    <td className="mono text-right text-cyan-400 font-bold">
                      {fmtPrice(t.price)}
                    </td>
                    <td className="mono text-right font-medium text-[#dde1ed]">
                      {t.size.toFixed(1)}
                    </td>
                    <td className="mono text-right text-[#7e8aaa] text-xs">
                      {fmtUsd(tradeVal)}
                    </td>
                    {/* W39-5 — Fee cell. Falls back to "—" when the
                        snapshot doesn't publish `t.fee` (paper-trading
                        mode today). */}
                    <td className="mono text-right text-[10.5px] text-amber-300">
                      {typeof t.fee === 'number' ? fmtUsd(t.fee) : <span className="text-[#3e4560]">—</span>}
                    </td>
                    {/* W39-5 — Slippage cell. Falls back to "—" when the
                        snapshot doesn't publish `t.slippage_bps`.
                        Negative slippage (price improvement) tints
                        green; positive (adverse) tints amber. */}
                    <td className={`mono text-right text-[10.5px] ${
                      typeof t.slippage_bps === 'number'
                        ? t.slippage_bps >= 0
                          ? 'text-amber-300'
                          : 'text-green-400'
                        : 'text-[#3e4560]'
                    }`}>
                      {typeof t.slippage_bps === 'number'
                        ? `${t.slippage_bps >= 0 ? '+' : '−'}${Math.abs(t.slippage_bps).toFixed(1)} bps`
                        : '—'}
                    </td>
                    <td
                      className={`mono text-right font-bold ${
                        t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#7e8aaa]'
                      }`}
                    >
                      {t.pnl !== 0 ? fmtPnl(t.pnl) : '—'}
                    </td>
                    {/* W39-5 — Strategy tag + audit-trail link icon. The
                        📋 icon is only rendered when `onViewAuditTrail`
                        is provided (page.tsx opts in for production).
                        Clicking it invokes the callback with the
                        trade's decision_id (or trade_id fallback) so
                        the parent can switch to the Decision Ledger. */}
                    <td className="mono text-right text-[10px] text-[#7e8aaa]">
                      <span className="inline-flex items-center gap-1">
                        <span className="px-1.5 py-0.5 rounded bg-[#0e1015] border border-[#1f2335]">
                          {t.strategy || 'manual'}
                        </span>
                        {onViewAuditTrail && (
                          <button
                            type="button"
                            onClick={() => handleViewAudit(t)}
                            className="inline-flex items-center justify-center w-5 h-5 rounded border border-[#1f2335] bg-[#0e1015] text-[#7e8aaa] hover:text-cyan-300 hover:border-cyan-500/50 transition-colors"
                            aria-label={`Open decision ledger audit trail for trade ${t.trade_id}`}
                            title={`Audit trail · Decision ID: ${t.decision_id ?? t.trade_id}`}
                          >
                            <span aria-hidden="true" className="text-[10px]">📋</span>
                          </button>
                        )}
                      </span>
                    </td>
                    {/* W39-5 — Time rendered in relative format ("3m
                        ago") with the absolute ISO timestamp surfaced
                        via the title attribute for hover + screen
                        reader context. */}
                    <td className="mono text-right text-[#7e8aaa] text-[10.5px]" title={`Executed: ${fmtTimeAbs(t.timestamp)}`}>
                      {fmtAge(t.timestamp)}
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

export default memo(TradesPanel)
