// components/TradesPanel.tsx — Recent Trade Executions Feed
'use client'

import { useState, useMemo } from 'react'
import { Trade } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtAge, fmtPrice, fmtPnl, fmtUsd } from '@/lib/design-tokens'

interface Props {
  trades: Trade[]
}

export default function TradesPanel({ trades }: Props) {
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

  const displayedTrades = filteredTrades.slice(0, 100)

  // Execution Summary Metrics
  const stats = useMemo(() => {
    const totalVol = trades.reduce((acc, t) => acc + (t.size * t.price), 0)
    const netPnl = trades.reduce((acc, t) => acc + (t.pnl || 0), 0)
    const wins = trades.filter((t) => (t.pnl || 0) > 0).length
    const closed = trades.filter((t) => (t.pnl || 0) !== 0).length
    const winRate = closed > 0 ? (wins / closed) * 100 : 0
    return { totalVol, netPnl, winRate, totalCount: trades.length }
  }, [trades])

  const handleExportCsv = () => {
    if (trades.length === 0) return
    const headers = ['Trade ID', 'Timestamp', 'Market Slug', 'Side', 'Price', 'Shares', 'P&L', 'Strategy']
    const rows = trades.map((t) => [
      t.trade_id,
      new Date(t.timestamp).toISOString(),
      `"${t.slug.replace(/"/g, '""')}"`,
      t.side,
      t.price.toFixed(4),
      t.size.toFixed(2),
      t.pnl.toFixed(4),
      t.strategy || 'manual',
    ])
    const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
    const encodedUri = encodeURI(csvContent)
    const link = document.createElement('a')
    link.setAttribute('href', encodedUri)
    link.setAttribute('download', `polymarket_executions_${Date.now()}.csv`)
    document.body.appendChild(link)
    link.click()
    document.body.removeChild(link)
  }

  const copyToClipboard = (text: string, id: string) => {
    navigator.clipboard.writeText(text)
    setCopiedId(id)
    setTimeout(() => setCopiedId(null), 1500)
  }

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335] shadow-xl">
      {/* Header with Stats */}
      <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Recent Executions ({filteredTrades.length})
          </span>
          <span className="badge badge-green text-[9.5px]">Audit Stream</span>
        </div>

        {/* Aggregate KPI Badges */}
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

      {/* Filter & Search Bar */}
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

      {/* Trades List */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {filteredTrades.length === 0 ? (
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
                <th scope="col" className="text-right">P&amp;L</th>
                <th scope="col" className="text-right">Strategy</th>
                <th scope="col" className="text-right">Time</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/40">
              {displayedTrades.map((t) => {
                const info = formatHierarchicalMarket(t.slug)
                const tradeVal = t.size * t.price
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
                    <td className="text-center">
                      <span
                        className={`inline-block px-2 py-0.5 rounded text-[9px] font-bold ${
                          t.side === 'BUY'
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : 'bg-red-500/15 text-red-400 border border-red-500/30'
                        }`}
                      >
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
                    <td
                      className={`mono text-right font-bold ${
                        t.pnl > 0 ? 'text-green-400' : t.pnl < 0 ? 'text-red-400' : 'text-[#7e8aaa]'
                      }`}
                    >
                      {t.pnl !== 0 ? fmtPnl(t.pnl) : '—'}
                    </td>
                    <td className="mono text-right text-[10px] text-[#7e8aaa]">
                      <span className="px-1.5 py-0.5 rounded bg-[#0e1015] border border-[#1f2335]">
                        {t.strategy || 'manual'}
                      </span>
                    </td>
                    <td className="mono text-right text-[#7e8aaa] text-[10.5px]">{fmtAge(t.timestamp)}</td>
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

