// components/MarketsPanel.tsx — Pro Markets & Live Order Books Panel with Hierarchical Multi-Line Typography
'use client'

import { useState } from 'react'
import { OrderBook } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'

interface Props {
  books: OrderBook[]
  onSelectMarket?: (tokenId: string, slug: string) => void
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

function ProbabilityGauge({ mid }: { mid: number | null }) {
  if (mid === null) return <span className="text-[#4a5068] mono">—</span>
  const pct = Math.round(mid * 100)
  return (
    <div className="flex items-center gap-1.5">
      <div className="w-14 h-1.5 bg-[#252836] rounded-full overflow-hidden shrink-0">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{
            width: `${pct}%`,
            background: mid >= 0.6 ? '#22c55e' : mid <= 0.4 ? '#ef4444' : '#3b82f6',
          }}
        />
      </div>
      <span className="mono text-xs font-semibold text-[#e8eaf0] w-9 text-right">
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
    <div className="card h-full flex flex-col p-3.5 bg-[#161822] border border-[#252836]">
      {/* Header */}
      <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚡ Active Markets &amp; Live Order Books ({books.length})
          </span>
          <span className="badge badge-green text-[10px]">Continuous Stream</span>
        </div>

        {/* Search */}
        <div className="relative">
          <input
            type="text"
            placeholder="Search prediction markets…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-[#111318] border border-[#252836] rounded px-2.5 py-0.5 text-xs text-[#e8eaf0] placeholder-[#4a5068] w-40 focus:outline-none focus:border-blue-500 focus:w-56 transition-all"
          />
        </div>
      </div>

      {/* Table */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {books.length === 0 ? (
          <div className="flex items-center justify-center h-28 text-[#4a5068] text-xs">
            Synchronizing live prediction market order books…
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th className="min-w-[240px]">Event &amp; Market Question</th>
                <th>Bid</th>
                <th>Ask</th>
                <th
                  onClick={() => handleSort('mid')}
                  className="cursor-pointer hover:text-white select-none"
                >
                  Prob {sortBy === 'mid' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  onClick={() => handleSort('spread')}
                  className="cursor-pointer hover:text-white select-none"
                >
                  Spread {sortBy === 'spread' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  onClick={() => handleSort('age')}
                  className="cursor-pointer hover:text-white select-none"
                >
                  Age {sortBy === 'age' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {sorted.map((b) => {
                const info = formatHierarchicalMarket(b.slug)
                return (
                  <tr
                    key={b.token_id}
                    className="hover:bg-blue-500/10 transition-colors group"
                  >
                    <td className="py-2.5 max-w-[320px]">
                      <div className="flex flex-col gap-0.5">
                        {/* Event Category Tag */}
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs">{info.category.icon}</span>
                          <span className="text-[10px] text-[#8b91a8] uppercase font-bold tracking-wider truncate">
                            {info.eventTitle}
                          </span>
                        </div>
                        {/* Secondary Multi-Line Natural Wrapping Question */}
                        <span
                          onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                          className="text-[#e8eaf0] font-medium leading-snug cursor-pointer hover:text-blue-400 text-xs block whitespace-normal"
                          title={info.fullLabel}
                        >
                          {info.question}
                        </span>
                      </div>
                    </td>
                    <td className="text-green-400 mono font-semibold">
                      {b.best_bid != null ? b.best_bid.toFixed(3) : '—'}
                    </td>
                    <td className="text-red-400 mono font-semibold">
                      {b.best_ask != null ? b.best_ask.toFixed(3) : '—'}
                    </td>
                    <td>
                      <ProbabilityGauge mid={b.mid} />
                    </td>
                    <td className="text-[#8b91a8] mono">
                      {b.spread != null ? `${(b.spread * 100).toFixed(1)}¢` : '—'}
                    </td>
                    <td className="text-[#4a5068] mono text-[10px]">{age(b.updated_at)}</td>
                    <td className="text-right">
                      <button
                        onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                        className="text-[10px] uppercase font-bold text-blue-400 hover:text-white bg-blue-500/10 hover:bg-blue-500/30 px-2.5 py-1 rounded border border-blue-500/20"
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
