// components/MarketsPanel.tsx — Pro Markets & Live Order Books Panel with Hierarchical Typography & Freshness Indicators
'use client'

import { useState } from 'react'
import { OrderBook } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtPrice } from '@/lib/design-tokens'

interface Props {
  books: OrderBook[]
  onSelectMarket?: (tokenId: string, slug: string) => void
}

function ageSec(ts: number) {
  return Math.max(0, Math.floor(Date.now() / 1000 - ts))
}

function fmtAgeDisplay(s: number) {
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function ProbabilityGauge({ mid }: { mid: number | null }) {
  if (mid === null) return <span className="text-[#3e4560] mono">—</span>
  const pct = Math.round(mid * 100)
  return (
    <div className="flex items-center gap-1.5" title={`Implied probability: ${(mid * 100).toFixed(1)}%`}>
      <div className="w-14 h-1.5 bg-[#1f2335] rounded-full overflow-hidden shrink-0">
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${pct}%`,
            background: mid >= 0.6 ? '#22c55e' : mid <= 0.4 ? '#ef4444' : '#3b82f6',
          }}
        />
      </div>
      <span className="mono text-xs font-semibold text-[#dde1ed] w-9 text-right">
        {(mid * 100).toFixed(0)}%
      </span>
    </div>
  )
}

export default function MarketsPanel({ books, onSelectMarket }: Props) {
  const [search, setSearch] = useState('')
  const [sortBy, setSortBy] = useState<'mid' | 'spread' | 'age'>('mid')
  const [sortAsc, setSortAsc] = useState(false)

  const handleSort = (field: 'mid' | 'spread' | 'age') => {
    if (sortBy === field) {
      setSortAsc(!sortAsc)
    } else {
      setSortBy(field)
      setSortAsc(false)
    }
  }

  const filtered = books.filter((b) =>
    b.slug.toLowerCase().includes(search.toLowerCase()) ||
    b.token_id.toLowerCase().includes(search.toLowerCase())
  )

  const sorted = [...filtered].sort((a, b) => {
    let diff = 0
    if (sortBy === 'mid') diff = (b.mid ?? 0) - (a.mid ?? 0)
    else if (sortBy === 'spread') diff = (a.spread ?? 99) - (b.spread ?? 99)
    else if (sortBy === 'age') diff = b.updated_at - a.updated_at
    return sortAsc ? -diff : diff
  })

  return (
    <div className="card h-full flex flex-col p-3 bg-[#13161e] border border-[#1f2335]">
      {/* Header */}
      <div className="card-header pb-2 mb-1 border-b border-[#1f2335] flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Active Markets &amp; Live Books ({books.length})
          </span>
          <span className="badge badge-green text-[9.5px]">Continuous Polling</span>
        </div>

        {/* Search */}
        <div className="relative">
          <input
            type="text"
            placeholder="Search prediction markets…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input input-sm w-44 focus:w-56 transition-all text-xs"
            aria-label="Search prediction markets"
          />
        </div>
      </div>

      {/* Table */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {books.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-32 text-[#7e8aaa] text-xs">
            <span className="spinner mb-2" aria-hidden="true" />
            Synchronizing live prediction market order books…
          </div>
        ) : (
          <table className="data-table text-xs" role="table" aria-label="Polymarket active order books">
            <thead>
              <tr>
                <th scope="col" className="min-w-[220px]">Event &amp; Market Question</th>
                <th scope="col">Bid</th>
                <th scope="col">Ask</th>
                <th
                  scope="col"
                  onClick={() => handleSort('mid')}
                  className="cursor-pointer hover:text-white select-none"
                  title="Sort by implied probability (midpoint)"
                >
                  Prob {sortBy === 'mid' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('spread')}
                  className="cursor-pointer hover:text-white select-none"
                  title="Sort by bid-ask spread"
                >
                  Spread {sortBy === 'spread' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('age')}
                  className="cursor-pointer hover:text-white select-none"
                  title="Sort by data age"
                >
                  Freshness {sortBy === 'age' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((b) => {
                const info = formatHierarchicalMarket(b.slug)
                const age = ageSec(b.updated_at)
                const isStale = age > 30
                return (
                  <tr
                    key={b.token_id}
                    className={`hover:bg-blue-500/10 transition-colors group ${isStale ? 'row-stale' : ''}`}
                  >
                    <td className="py-2 max-w-[320px]">
                      <div className="flex flex-col gap-0.5">
                        {/* Event Category Tag */}
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs" aria-hidden="true">{info.category.icon}</span>
                          <span className="text-[10px] text-[#7e8aaa] uppercase font-bold tracking-wider truncate">
                            {info.eventTitle}
                          </span>
                        </div>
                        {/* Secondary Multi-Line Natural Wrapping Question */}
                        <button
                          onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                          className="text-[#dde1ed] font-medium leading-snug cursor-pointer hover:text-blue-400 text-xs block whitespace-normal text-left bg-transparent border-none p-0"
                          title={info.fullLabel}
                        >
                          {info.question}
                        </button>
                      </div>
                    </td>
                    <td className="text-green-400 mono font-semibold">
                      {fmtPrice(b.best_bid)}
                    </td>
                    <td className="text-red-400 mono font-semibold">
                      {fmtPrice(b.best_ask)}
                    </td>
                    <td>
                      <ProbabilityGauge mid={b.mid} />
                    </td>
                    <td className="text-[#7e8aaa] mono">
                      {b.spread != null ? `${(b.spread * 100).toFixed(1)}¢` : '—'}
                    </td>
                    <td>
                      <span className={`mono text-[10.5px] ${isStale ? 'text-amber-400 font-semibold' : 'text-[#7e8aaa]'}`}>
                        {fmtAgeDisplay(age)}
                        {isStale && <span className="ml-1 text-[9px] badge badge-amber">stale</span>}
                      </span>
                    </td>
                    <td className="text-right">
                      <button
                        onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                        className="btn btn-primary btn-xs"
                        aria-label={`Open trading depth for ${info.question}`}
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
