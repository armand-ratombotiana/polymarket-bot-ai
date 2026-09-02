// components/MarketsPanel.tsx — Pro Markets & Live Order Books Desk with Microstructure Gauges
'use client'

import { useState, useMemo } from 'react'
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
  const isHigh = mid >= 0.7
  const isLow = mid <= 0.3

  return (
    <div className="flex items-center gap-2" title={`Implied Probability: ${(mid * 100).toFixed(1)}% (Decimal: ${mid.toFixed(3)})`}>
      <div className="w-16 h-2 bg-[#0e1015] border border-[#1f2335] rounded-full overflow-hidden shrink-0 relative">
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{
            width: `${pct}%`,
            background: isHigh
              ? 'linear-gradient(90deg, #16a34a, #4ade80)'
              : isLow
              ? 'linear-gradient(90deg, #dc2626, #f87171)'
              : 'linear-gradient(90deg, #2563eb, #38bdf8)',
            boxShadow: isHigh
              ? '0 0 8px rgba(74, 222, 128, 0.4)'
              : isLow
              ? '0 0 8px rgba(248, 113, 113, 0.4)'
              : '0 0 8px rgba(56, 189, 248, 0.3)',
          }}
        />
      </div>
      <span className={`mono text-xs font-bold w-10 text-right ${isHigh ? 'text-emerald-400' : isLow ? 'text-red-400' : 'text-cyan-300'}`}>
        {(mid * 100).toFixed(0)}%
      </span>
    </div>
  )
}

const CATEGORIES = ['ALL', 'CRYPTO', 'POLITICS', 'ECONOMY', 'SPORTS', 'TECH']

export default function MarketsPanel({ books, onSelectMarket }: Props) {
  const [search, setSearch] = useState('')
  const [selectedCat, setSelectedCat] = useState('ALL')
  const [sortBy, setSortBy] = useState<'mid' | 'spread' | 'age'>('mid')
  const [sortAsc, setSortAsc] = useState(false)
  const [copiedToken, setCopiedToken] = useState<string | null>(null)

  const handleSort = (field: 'mid' | 'spread' | 'age') => {
    if (sortBy === field) {
      setSortAsc(!sortAsc)
    } else {
      setSortBy(field)
      setSortAsc(false)
    }
  }

  const handleCopy = (e: React.MouseEvent, text: string) => {
    e.stopPropagation()
    navigator.clipboard.writeText(text)
    setCopiedToken(text)
    setTimeout(() => setCopiedToken(null), 1200)
  }

  // Filter by category & search
  const filtered = useMemo(() => {
    return books.filter((b) => {
      const matchSearch =
        b.slug.toLowerCase().includes(search.toLowerCase()) ||
        b.token_id.toLowerCase().includes(search.toLowerCase())
      if (!matchSearch) return false

      if (selectedCat === 'ALL') return true
      const slugU = b.slug.toUpperCase()
      if (selectedCat === 'CRYPTO') return slugU.includes('BITCOIN') || slugU.includes('ETH') || slugU.includes('SOL') || slugU.includes('CRYPTO')
      if (selectedCat === 'POLITICS') return slugU.includes('ELECTION') || slugU.includes('PRESIDENT') || slugU.includes('TRUMP') || slugU.includes('SENATE')
      if (selectedCat === 'ECONOMY') return slugU.includes('FED') || slugU.includes('INFLATION') || slugU.includes('RATE') || slugU.includes('CPI')
      if (selectedCat === 'SPORTS') return slugU.includes('NBA') || slugU.includes('NFL') || slugU.includes('SOCCER') || slugU.includes('UFC')
      if (selectedCat === 'TECH') return slugU.includes('AI') || slugU.includes('OPENAI') || slugU.includes('GPT') || slugU.includes('TECH')
      return true
    })
  }, [books, search, selectedCat])

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      let diff = 0
      if (sortBy === 'mid') diff = (b.mid ?? 0) - (a.mid ?? 0)
      else if (sortBy === 'spread') diff = (a.spread ?? 99) - (b.spread ?? 99)
      else if (sortBy === 'age') diff = b.updated_at - a.updated_at
      return sortAsc ? -diff : diff
    })
  }, [filtered, sortBy, sortAsc])

  // Aggregate Metrics
  const avgSpreadCents = useMemo(() => {
    const valid = books.filter((b) => b.spread != null && b.spread > 0)
    if (valid.length === 0) return 0
    return (valid.reduce((acc, b) => acc + (b.spread || 0), 0) / valid.length) * 100
  }, [books])

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-xl overflow-hidden">
      {/* 1. Header & Live Metrics */}
      <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2.5 bg-[#0e1015]/80">
        <div className="flex items-center gap-2.5">
          <span className="card-title text-xs font-bold text-[#dde1ed] flex items-center gap-1.5">
            ⚡ Active Order Books ({books.length})
          </span>
          <span className="badge badge-green text-[9px] font-bold">L2 Stream</span>
          <span className="text-[10.5px] text-[#7e8aaa] mono hidden sm:inline-block">
            Avg Spread: <strong className="text-cyan-300 font-semibold">{avgSpreadCents.toFixed(1)}¢</strong>
          </span>
        </div>

        {/* Search & Category filter */}
        <div className="flex items-center gap-2">
          <div className="relative">
            <input
              type="text"
              placeholder="Search markets or token ID…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input input-sm w-44 focus:w-60 transition-all text-xs bg-[#13161e] border border-[#1f2335] pr-6"
              aria-label="Search prediction markets"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[#7e8aaa] hover:text-white text-xs leading-none"
                aria-label="Clear search"
              >
                ×
              </button>
            )}
          </div>
          {search && (
            <span className="badge badge-blue text-[9px] mono">
              {filtered.length} found
            </span>
          )}
        </div>
      </div>

      {/* 2. Category Filter Pills */}
      <div className="flex items-center gap-1 px-3 py-1.5 bg-[#0e1015] border-b border-[#1f2335] overflow-x-auto scrollbar-thin">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setSelectedCat(cat)}
            className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
              selectedCat === cat
                ? 'bg-blue-500/20 text-cyan-300 border border-blue-500/40 shadow-sm'
                : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* 3. Table */}
      <div className="overflow-auto scrollbar-thin flex-1 table-container">
        {books.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-44 text-[#7e8aaa] text-xs">
            <span className="spinner mb-2" aria-hidden="true" />
            Synchronizing live prediction market order books…
          </div>
        ) : sorted.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-44 text-[#7e8aaa] text-xs">
            No active markets matching <strong className="text-white mx-1">"{search}"</strong> in {selectedCat} category.
          </div>
        ) : (
          <table className="data-table text-xs w-full" role="table" aria-label="Polymarket active order books">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="min-w-[240px] text-left">Event &amp; Contract Question</th>
                <th scope="col" className="text-right">Bid (YES)</th>
                <th scope="col" className="text-right">Ask (YES)</th>
                <th
                  scope="col"
                  onClick={() => handleSort('mid')}
                  className="cursor-pointer hover:text-white select-none text-right"
                  title="Sort by implied probability (midpoint)"
                >
                  Implied Odds {sortBy === 'mid' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('spread')}
                  className="cursor-pointer hover:text-white select-none text-right"
                  title="Sort by bid-ask spread"
                >
                  Spread {sortBy === 'spread' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('age')}
                  className="cursor-pointer hover:text-white select-none text-center"
                  title="Sort by data age"
                >
                  Freshness {sortBy === 'age' ? (sortAsc ? '▲' : '▼') : ''}
                </th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {sorted.map((b) => {
                const info = formatHierarchicalMarket(b.slug)
                const age = ageSec(b.updated_at)
                const isStale = age > 30
                const isCopied = copiedToken === b.token_id

                return (
                  <tr
                    key={b.token_id}
                    onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                    className={`hover:bg-blue-500/10 transition-colors cursor-pointer group ${isStale ? 'row-stale' : ''}`}
                  >
                    <td className="py-2.5 max-w-[320px]">
                      <div className="flex flex-col gap-0.5">
                        {/* Category Tag & Token Copy Button */}
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs" aria-hidden="true">{info.category.icon}</span>
                          <span className="text-[9.5px] text-cyan-400 font-bold uppercase tracking-wider truncate">
                            {info.eventTitle}
                          </span>
                          <button
                            onClick={(e) => handleCopy(e, b.token_id)}
                            className="text-[9px] text-[#3e4560] group-hover:text-[#7e8aaa] hover:!text-white transition-colors mono ml-1"
                            title="Click to copy Token ID"
                          >
                            {isCopied ? '✓ Copied' : `[#${b.token_id.slice(0, 6)}…]`}
                          </button>
                        </div>
                        {/* Question Title */}
                        <span
                          className="text-[#dde1ed] group-hover:text-cyan-300 font-medium leading-snug text-xs block whitespace-normal transition-colors"
                          title={info.fullLabel}
                        >
                          {info.question}
                        </span>
                      </div>
                    </td>

                    {/* Best Bid */}
                    <td className="text-green-400 mono font-semibold text-right">
                      {fmtPrice(b.best_bid)}
                    </td>

                    {/* Best Ask */}
                    <td className="text-red-400 mono font-semibold text-right">
                      {fmtPrice(b.best_ask)}
                    </td>

                    {/* Implied Probability Gauge */}
                    <td className="text-right">
                      <ProbabilityGauge mid={b.mid} />
                    </td>

                    {/* Spread Cents */}
                    <td className="text-[#dde1ed] mono text-right font-medium">
                      {b.spread != null ? `${(b.spread * 100).toFixed(1)}¢` : '—'}
                    </td>

                    {/* Freshness Badge */}
                    <td className="text-center">
                      <span className={`mono text-[10.5px] px-1.5 py-0.5 rounded ${
                        isStale ? 'bg-amber-500/15 text-amber-400 font-bold border border-amber-500/30' : 'text-[#7e8aaa]'
                      }`}>
                        {fmtAgeDisplay(age)}
                      </span>
                    </td>

                    {/* Quick Trade Button */}
                    <td className="text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          onSelectMarket && onSelectMarket(b.token_id, b.slug)
                        }}
                        className="btn btn-primary btn-xs font-bold shadow-md hover:shadow-cyan-500/20"
                        aria-label={`Open depth and trade ticket for ${info.question}`}
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
