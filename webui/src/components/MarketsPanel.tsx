// components/MarketsPanel.tsx — Pro Markets & Live Order Books Panel
'use client'

import { useState } from 'react'
import { OrderBook } from '@/hooks/useBot'

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
    if (sortBy === 'mid') {
      diff = (a.mid || 0) - (b.mid || 0)
    } else if (sortBy === 'spread') {
      diff = (a.spread || 0) - (b.spread || 0)
    } else if (sortBy === 'age') {
      diff = a.updated_at - b.updated_at
    }
    return sortAsc ? diff : -diff
  })

  return (
    <div className="card flex flex-col h-full min-h-0 bg-[#111318] border border-[#252836]">
      {/* Panel Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-3 py-2 border-b border-[#252836] gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            📈 Markets &amp; Order Books
          </span>
          <span className="text-[10px] text-[#8b91a8] mono">({sorted.length} active)</span>
        </div>

        <div className="flex items-center gap-1.5">
          <input
            type="text"
            placeholder="Search markets…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-[#161822] border border-[#252836] rounded px-2 py-0.5 text-[11px] mono text-[#e8eaf0] placeholder-[#4a5068] focus:outline-none focus:border-blue-500 w-32"
          />
        </div>
      </div>

      {/* Table */}
      <div className="overflow-auto scrollbar-thin flex-1">
        {books.length === 0 ? (
          <div className="flex items-center justify-center h-28 text-[#4a5068] text-xs">
            Loading real-time order books…
          </div>
        ) : (
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Market</th>
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
              {sorted.map((b) => (
                <tr
                  key={b.token_id}
                  className="hover:bg-blue-500/10 transition-colors group"
                >
                  <td className="max-w-[150px]">
                    <span
                      onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                      className="text-[#e8eaf0] font-medium block truncate cursor-pointer hover:text-blue-400"
                      title={b.slug}
                    >
                      {b.slug}
                    </span>
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
                      className="text-[10px] uppercase font-semibold text-blue-400 hover:text-white bg-blue-500/10 hover:bg-blue-500/30 px-2 py-0.5 rounded border border-blue-500/20"
                    >
                      Trade
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
