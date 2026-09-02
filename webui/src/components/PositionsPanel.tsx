// components/PositionsPanel.tsx — Active Portfolio Positions, Real-Time P&L & Exposure Governance
'use client'

import { useState, useMemo } from 'react'
import { Position } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtPnl, fmtUsd } from '@/lib/design-tokens'

interface Props {
  positions: Position[]
  dailyPnl: number
  onSelectMarket?: (market: { tokenId: string; slug: string }) => void
}

export default function PositionsPanel({ positions, dailyPnl, onSelectMarket }: Props) {
  const [filterQuery, setFilterQuery] = useState('')
  const [outcomeFilter, setOutcomeFilter] = useState<'ALL' | 'YES' | 'NO'>('ALL')
  const [sortBy, setSortBy] = useState<'size' | 'pnl' | 'market'>('size')

  const totalInvested = positions.reduce((acc, p) => acc + p.total_invested, 0)
  const totalRealized = positions.reduce((acc, p) => acc + p.realised_pnl, 0)
  const MAX_PER_MARKET = 3.0 // USD 3.00 institutional limit
  const MAX_TOTAL_PORTFOLIO = 25.0 // USD 25.00 total exposure cap

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

  const handleExportCsv = () => {
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
  }

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

      {/* Filter & Search Bar */}
      <div className="flex items-center justify-between gap-2 mb-2">
        <div className="relative flex-1 max-w-xs">
          <input
            type="text"
            placeholder="Search position by market / contract..."
            value={filterQuery}
            onChange={(e) => setFilterQuery(e.target.value)}
            className="w-full bg-[#0e1015] border border-[#1f2335] focus:border-cyan-500/50 rounded text-xs px-2.5 py-1.5 text-[#dde1ed] placeholder-[#3e4560] outline-none transition-all"
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

        <div className="flex items-center gap-1.5">
          {/* Outcome Filter */}
          <div className="inline-flex bg-[#0e1015] border border-[#1f2335] rounded p-0.5 text-[10px]">
            {(['ALL', 'YES', 'NO'] as const).map((side) => (
              <button
                key={side}
                onClick={() => setOutcomeFilter(side)}
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
                <th scope="col" className="text-right">Cost Basis</th>
                <th scope="col" className="text-center min-w-[110px]">Cap Limit ($3 Max)</th>
                <th scope="col" className="text-right">Realized P&amp;L</th>
                <th scope="col" className="text-center">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {filteredPositions.map((p) => {
                const info = formatHierarchicalMarket(p.slug)
                const utilizationPct = Math.min((p.total_invested / MAX_PER_MARKET) * 100, 100)
                const isYes = p.yes_shares > 0
                const isNearCap = utilizationPct > 80

                return (
                  <tr
                    key={p.token_id}
                    className="hover:bg-blue-500/10 transition-colors group"
                  >
                    {/* Market Title */}
                    <td
                      className="py-2.5 max-w-[240px] cursor-pointer"
                      onClick={() => onSelectMarket?.({ tokenId: p.token_id, slug: p.slug })}
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

                    {/* Action Button */}
                    <td className="text-center">
                      <button
                        onClick={() => onSelectMarket?.({ tokenId: p.token_id, slug: p.slug })}
                        className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-cyan-400 hover:text-white hover:border-cyan-500/50"
                        title="Open Depth & Trade Modal"
                      >
                        Trade
                      </button>
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

