// components/MarketsPanel.tsx — Pro Markets & Live Order Books Desk with Microstructure Gauges
//
// W38-4 — market discovery improvements:
//   • Market-name column widened to min-w-[280px] / max-w-[440px] with
//     line-clamp-2 so long event titles wrap to two readable lines
//     instead of destructively truncating mid-word.
//   • Category badge (icon + label) added next to each row's event title
//     so a trader can scan categories at a glance without reading the
//     question.
//   • Data-freshness column now shows BOTH the relative age (e.g. "3s")
//     AND the absolute last-updated timestamp (HH:MM:SS UTC) so a
//     trader can spot a frozen feed even when the relative timer is
//     misleading (e.g., after a clock skew).
//   • Stale threshold bumped to >60s amber (was >30s) per W38-4 spec;
//     >120s marks the row as dead (red).
//   • Header now shows a connection-status pill (LIVE / STALE / OFFLINE)
//     derived from the freshest book — so a trader instantly knows
//     whether the order-book stream is keeping up.
//   • Spread filter pills (All / <2¢ / 2–5¢ / >5¢) added below the
//     category bar so traders can isolate tradable markets by spread.
'use client'

import { useState, useMemo, useRef, memo } from 'react'
import { OrderBook } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import PriceTicker from './PriceTicker'
import PriceHistoryChart from './charts/PriceHistoryChart'

interface Props {
  books: OrderBook[]
  onSelectMarket?: (tokenId: string, slug: string) => void
  // U12 — Per-token price-flash direction map (token_id → 'up' | 'down').
  // When present, the mid-price cell applies the `.price-up` / `.price-down`
  // CSS class so a CSS keyframe can briefly tint the cell green/red on tick.
  priceFlashes?: Record<string, 'up' | 'down'>
  // W15-2 — preference flag. When false, the `.price-up` / `.price-down`
  // CSS class is suppressed on the implied-probability cell (traders
  // who find the flashing distracting). Defaults to `true` so every
  // existing call site + existing test keeps the prior behaviour.
  showPriceFlashes?: boolean
}

function ageSec(ts: number) {
  return Math.max(0, Math.floor(Date.now() / 1000 - ts))
}

function fmtAgeDisplay(s: number) {
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  return `${Math.floor(s / 3600)}h`
}

// W38-4 — Format epoch seconds as HH:MM:SS UTC for the "last updated"
// column. Lets a trader compare the panel's clock to the upstream
// feed's clock and spot frozen feeds even when relative age is fuzzy.
function fmtLastUpdatedUTC(ts: number): string {
  if (!Number.isFinite(ts) || ts <= 0) return '—'
  return new Date(ts * 1000).toISOString().slice(11, 19)
}

// W38-4 — Connection-status buckets derived from the freshest book's age.
//   LIVE   — at least one book updated in the last 60s
//   STALE  — freshest book is 60–120s old (amber)
//   OFFLINE — freshest book >120s old OR no books at all (red)
//   IDLE   — no books yet (panel waiting on first snapshot)
type ConnStatus = 'LIVE' | 'STALE' | 'OFFLINE' | 'IDLE'
function deriveConnStatus(books: OrderBook[]): ConnStatus {
  if (!books || books.length === 0) return 'IDLE'
  const newest = books.reduce((acc, b) => (b.updated_at > acc ? b.updated_at : acc), 0)
  const age = ageSec(newest)
  if (age <= 60) return 'LIVE'
  if (age <= 120) return 'STALE'
  return 'OFFLINE'
}

// W38-4 — Spread bucket thresholds (in cents, 0..1 probability * 100).
//   TIGHT   < 2¢ — high liquidity, tradable
//   NORMAL  2–5¢ — typical
//   WIDE    > 5¢ — illiquid / avoid for size
type SpreadFilter = 'ALL' | 'TIGHT' | 'NORMAL' | 'WIDE'
const SPREAD_FILTERS: { key: SpreadFilter; label: string; title: string }[] = [
  { key: 'ALL', label: 'All', title: 'Show all spreads' },
  { key: 'TIGHT', label: '<2¢', title: 'Tight spreads (<2¢) — high liquidity' },
  { key: 'NORMAL', label: '2–5¢', title: 'Normal spreads (2–5¢)' },
  { key: 'WIDE', label: '>5¢', title: 'Wide spreads (>5¢) — illiquid, avoid for size' },
]
function spreadBucket(spread: number | null | undefined): SpreadFilter {
  if (spread == null || !Number.isFinite(spread)) return 'WIDE'
  const cents = spread * 100
  if (cents < 2) return 'TIGHT'
  if (cents <= 5) return 'NORMAL'
  return 'WIDE'
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

// W9-6 — wrapped in React.memo. The component receives `books` (a new
// array reference on every snapshot from useBot — every poll/WebSocket
// message — so memo won't skip many renders by itself), `onSelectMarket`
// (stable when parent wraps it in useCallback), and `priceFlashes`
// (mutates ~500ms after each tick as flashes clear). For memo to be
// effective, the parent (page.tsx) MUST pass stable callback identities
// via useCallback — otherwise the memo is bypassed on every parent render.
// We still wrap with React.memo (default shallow compare) so that any
// future parent-side memoization of `books` (e.g. via a selector hook)
// would automatically skip this panel's re-render.
function MarketsPanel({ books, onSelectMarket, priceFlashes, showPriceFlashes = true }: Props) {
  const [search, setSearch] = useState('')
  const [selectedCat, setSelectedCat] = useState('ALL')
  const [sortBy, setSortBy] = useState<'mid' | 'spread' | 'age'>('mid')
  const [sortAsc, setSortAsc] = useState(false)
  const [copiedToken, setCopiedToken] = useState<string | null>(null)
  // W38-4 — spread-bucket filter (ALL / TIGHT / NORMAL / WIDE).
  const [spreadFilter, setSpreadFilter] = useState<SpreadFilter>('ALL')
  // W15-1 — internal modal state for the PriceHistoryChart viewer.
  // The "View History" button per row sets this; the modal renders
  // PriceHistoryChart with the row's tokenId. Closed on backdrop click
  // or Escape (handled by the modal itself).
  const [historyMarket, setHistoryMarket] = useState<{ tokenId: string; slug: string } | null>(null)

  // W15-1 — Track previous mid price per token so PriceTicker can
  // compute change-since-last-tick. The ref is mutated on every render
  // (NOT in an effect — otherwise the first render after a tick would
  // see the new price as both `current` and `previous`, hiding the flash).
  // The lookup is keyed by token_id; we lazily populate it on each
  // render so a brand-new market starts with no previous (no flash).
  const prevMidsRef = useRef<Record<string, number>>({})

  // W9-6 — handlers kept as inline functions rather than useCallback:
  // they are wrapped in per-row arrow lambdas inside the JSX
  // (onClick={() => handleSort('mid')}), so making the outer function stable
  // has no memoization benefit. The expensive work (filter + sort) is
  // already memoized via the useMemo blocks below.
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

  // Filter by category, search, and spread bucket.
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
    }).filter((b) => {
      if (spreadFilter === 'ALL') return true
      return spreadBucket(b.spread) === spreadFilter
    })
  }, [books, search, selectedCat, spreadFilter])

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

  // W38-4 — connection status derived from the freshest book. Rendered
  // as a pill in the header so a trader instantly knows whether the
  // order-book stream is keeping up. LIVE / STALE / OFFLINE / IDLE.
  const connStatus = useMemo(() => deriveConnStatus(books), [books])

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] shadow-xl overflow-hidden">
      {/* 1. Header & Live Metrics */}
      <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2.5 bg-[#0e1015]/80">
        <div className="flex items-center gap-2.5">
          <span className="card-title text-xs font-bold text-[#dde1ed] flex items-center gap-1.5">
            ⚡ Active Order Books ({books.length})
          </span>
          {/* W38-4 — connection-status pill derived from the freshest book.
              LIVE / STALE / OFFLINE / IDLE — see deriveConnStatus(). */}
          <span
            className={`badge text-[9px] font-bold inline-flex items-center gap-1 border ${
              connStatus === 'LIVE'
                ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
                : connStatus === 'STALE'
                ? 'bg-amber-500/15 text-amber-300 border-amber-500/40'
                : connStatus === 'OFFLINE'
                ? 'bg-red-500/15 text-red-300 border-red-500/40'
                : 'bg-[#13161e] text-[#7e8aaa] border-[#1f2335]'
            }`}
            title={`Feed status: ${connStatus} — derived from the freshest book's age (≤60s LIVE, 60–120s STALE, >120s OFFLINE)`}
            data-testid="markets-conn-status"
            data-status={connStatus}
          >
            <span
              className={`inline-block w-1.5 h-1.5 rounded-full ${
                connStatus === 'LIVE'
                  ? 'bg-emerald-400 animate-pulse'
                  : connStatus === 'STALE'
                  ? 'bg-amber-400'
                  : connStatus === 'OFFLINE'
                  ? 'bg-red-400'
                  : 'bg-[#3e4560]'
              }`}
              aria-hidden="true"
            />
            {connStatus}
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

      {/* 2. Category Filter Pills + W38-4 Spread Filter Pills */}
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
        {/* W38-4 — visual divider between category + spread filter groups. */}
        <span className="w-px h-4 bg-[#1f2335] mx-1" aria-hidden="true" />
        <span className="text-[9.5px] text-[#7e8aaa] uppercase font-bold tracking-wider mr-1" aria-hidden="true">Spread</span>
        {SPREAD_FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setSpreadFilter(f.key)}
            title={f.title}
            aria-pressed={spreadFilter === f.key}
            className={`px-2 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
              spreadFilter === f.key
                ? 'bg-cyan-500/20 text-cyan-300 border border-cyan-500/40 shadow-sm'
                : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
            }`}
            data-testid={`spread-filter-${f.key.toLowerCase()}`}
          >
            {f.label}
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
          // W38-4 — richer empty state: show which filters are active so
          // the trader can tell whether they over-constrained the view.
          <div className="flex flex-col items-center justify-center h-44 text-[#7e8aaa] text-xs gap-1.5 px-6 text-center">
            <span className="text-2xl mb-1" aria-hidden="true">🔍</span>
            <div className="text-[#dde1ed] font-semibold">No markets match the current filters</div>
            <div className="text-[10.5px] mono">
              {search ? <>search: <strong className="text-white">"{search}"</strong> · </> : null}
              category: <strong className="text-white">{selectedCat}</strong> · spread: <strong className="text-white">{spreadFilter}</strong>
            </div>
            <button
              onClick={() => { setSearch(''); setSelectedCat('ALL'); setSpreadFilter('ALL') }}
              className="mt-2 btn btn-ghost btn-xs text-[10px]"
            >
              Reset all filters
            </button>
          </div>
        ) : (
          <table className="data-table text-xs w-full" role="table" aria-label="Polymarket active order books">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                {/* W38-4 — widened from min-w-[240px] → min-w-[280px] and
                    added max-w-[440px] so long event titles wrap to two
                    lines (line-clamp-2) instead of destructively
                    truncating mid-word. */}
                <th scope="col" className="min-w-[280px] max-w-[440px] text-left">Event &amp; Contract Question</th>
                {/* W15-1 — PriceTicker replaces the static Bid / Ask / Spread
                    columns with a single animated price cell that shows
                    mid + bid/ask chip + spread chip + change-since-last-tick. */}
                <th scope="col" className="text-right">Live Price (Bid / Ask / Δ)</th>
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
                <th scope="col" className="text-right">Actions</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {sorted.map((b) => {
                const info = formatHierarchicalMarket(b.slug)
                const age = ageSec(b.updated_at)
                // W38-4 — stale thresholds updated: amber at >60s, dead at >120s.
                // (was >30s amber only — bumped per the W38-4 freshness spec.)
                const isStale = age > 60
                const isDead = age > 120
                const isCopied = copiedToken === b.token_id
                // U12 — Resolve this row's price-flash direction once per render.
                // Undefined (no flash active) yields no extra class on the cell.
                const flashDir = priceFlashes?.[b.token_id]
                // W15-2 — suppress the flash class entirely when the
                // `showPriceFlashes` preference is off; the cell renders
                // without the green/red tint so a trader who finds the
                // flashing distracting gets a calmer view.
                const flashClass =
                  showPriceFlashes && flashDir === 'up'
                    ? ' price-up'
                    : showPriceFlashes && flashDir === 'down'
                    ? ' price-down'
                    : ''
                // W15-1 — Look up the previous mid for this token to feed
                // PriceTicker's change-since-last-tick computation. We
                // snapshot the previous value BEFORE updating the ref below,
                // so the row renders with the correct delta.
                const previousMid = b.mid != null ? prevMidsRef.current[b.token_id] ?? null : null
                // Update the ref with the current mid for the next render.
                // This must happen during render (not in an effect) so the
                // very next render of the same book gets this as previous.
                if (b.mid != null && Number.isFinite(b.mid)) {
                  prevMidsRef.current[b.token_id] = b.mid
                }

                return (
                  <tr
                    key={b.token_id}
                    onClick={() => onSelectMarket && onSelectMarket(b.token_id, b.slug)}
                    className={`hover:bg-blue-500/10 transition-colors cursor-pointer group ${
                      isDead ? 'row-stale opacity-60' : isStale ? 'row-stale' : ''
                    }`}
                  >
                    {/* W38-4 — widened max-w from 320px → 440px to match
                        the header column and allow long event titles
                        to wrap to two readable lines (line-clamp-2). */}
                    <td className="py-2.5 max-w-[440px]">
                      <div className="flex flex-col gap-0.5">
                        {/* Category Tag & Token Copy Button */}
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="text-xs" aria-hidden="true">{info.category.icon}</span>
                          {/* W38-4 — category badge with icon + label so a
                              trader can scan categories at a glance without
                              reading the question. */}
                          <span
                            className={`text-[9px] font-bold uppercase tracking-wider px-1 py-0.5 rounded border ${info.category.color}`}
                            title={`Category: ${info.category.label}`}
                          >
                            {info.category.label}
                          </span>
                          {/* W38-4 — line-clamp-2 (was truncate) so long
                              event titles wrap to two readable lines
                              instead of destructively truncating mid-word. */}
                          <span
                            className="text-[9.5px] text-cyan-400 font-bold uppercase tracking-wider line-clamp-2 leading-tight"
                            title={info.fullLabel}
                          >
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

                    {/* W15-1 — PriceTicker replaces the static Bid / Ask / Spread cells.
                        The component shows mid (animated + colored by tick direction),
                        a bid/ask chip on the left, a spread chip on the right, and a
                        change-since-last-tick line beneath. */}
                    <td className="text-right py-2.5 pr-3">
                      <div className="flex justify-end relative">
                        <PriceTicker
                          price={b.mid}
                          previousPrice={previousMid}
                          bestBid={b.best_bid}
                          bestAsk={b.best_ask}
                          spread={b.spread}
                          size="sm"
                          label={`${info.eventTitle} ${info.question} mid price`}
                        />
                      </div>
                    </td>

                    {/* Implied Probability Gauge — mid-price cell.
                        U12: apply .price-up / .price-down when a flash is active
                        for this token so CSS can animate the cell background.
                        W15-2: flash class is empty when `showPriceFlashes`
                        preference is off (computed in `flashClass` above). */}
                    <td className={`text-right${flashClass}`}>
                      <ProbabilityGauge mid={b.mid} />
                    </td>

                    {/* Spread Cents */}
                    <td className="text-[#dde1ed] mono text-right font-medium">
                      {b.spread != null ? `${(b.spread * 100).toFixed(1)}¢` : '—'}
                    </td>

                    {/* W38-4 — Freshness cell: now shows BOTH the relative age
                        AND the absolute last-updated timestamp (HH:MM:SS UTC)
                        so a trader can spot a frozen feed even when the
                        relative timer is misleading. Buckets: fresh <10s green,
                        ok <60s neutral, stale 60–120s amber, dead >120s red. */}
                    <td className="text-center">
                      <div
                        className="flex flex-col items-center gap-0.5"
                        title={`Last updated: ${fmtLastUpdatedUTC(b.updated_at)} UTC (age ${fmtAgeDisplay(age)})`}
                        data-testid={`freshness-${b.token_id}`}
                      >
                        <span
                          className={`mono text-[10.5px] px-1.5 py-0.5 rounded inline-flex items-center gap-1 ${
                            isDead
                              ? 'bg-red-500/15 text-red-400 font-bold border border-red-500/30'
                              : isStale
                              ? 'bg-amber-500/15 text-amber-400 font-bold border border-amber-500/30'
                              : age < 10
                              ? 'bg-emerald-500/10 text-emerald-300 font-semibold border border-emerald-500/20'
                              : 'text-[#7e8aaa]'
                          }`}
                        >
                          <span
                            className={`inline-block w-1 h-1 rounded-full ${
                              isDead
                                ? 'bg-red-400'
                                : isStale
                                ? 'bg-amber-400'
                                : age < 10
                                ? 'bg-emerald-400 animate-pulse'
                                : 'bg-[#3e4560]'
                            }`}
                            aria-hidden="true"
                          />
                          {fmtAgeDisplay(age)}
                        </span>
                        <span className="mono text-[9px] text-[#3e4560] tabular-nums">
                          {fmtLastUpdatedUTC(b.updated_at)}
                        </span>
                      </div>
                    </td>

                    {/* W15-1 — Action buttons: Depth (opens DepthChartModal which
                        now contains MarketDepthChart) + History (opens the
                        PriceHistoryChart modal). */}
                    <td className="text-right">
                      <div className="flex items-center justify-end gap-1.5">
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            onSelectMarket && onSelectMarket(b.token_id, b.slug)
                          }}
                          className="btn btn-primary btn-xs font-bold shadow-md hover:shadow-cyan-500/20"
                          aria-label={`View order book depth and trade ticket for ${info.question}`}
                        >
                          Depth
                        </button>
                        <button
                          onClick={(e) => {
                            e.stopPropagation()
                            setHistoryMarket({ tokenId: b.token_id, slug: b.slug })
                          }}
                          className="btn btn-ghost btn-xs font-bold"
                          aria-label={`View price history chart for ${info.question}`}
                        >
                          History
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

      {/* W15-1 — PriceHistoryChart modal. Rendered when the user clicks
          the "History" button on any row. Self-fetches OHLCV bars from
          /api/history/ohlcv and manages its own 5s polling. Escape key
          and backdrop click both close the modal. */}
      {historyMarket && (
        <div
          className="modal-backdrop"
          onClick={(e) => { if (e.target === e.currentTarget) setHistoryMarket(null) }}
          role="presentation"
        >
          <div
            className="modal modal-wide"
            role="dialog"
            aria-modal="true"
            aria-labelledby="history-modal-title"
          >
            <div className="modal-header">
              <div>
                <div className="flex items-center gap-2">
                  <span id="history-modal-title" className="text-sm font-bold text-[#dde1ed]">
                    📈 Price History: <span className="text-cyan-300">{historyMarket.slug || historyMarket.tokenId.slice(0, 16)}</span>
                  </span>
                </div>
                <span className="text-[11px] text-[#7e8aaa] mono mt-0.5 block">
                  token: {historyMarket.tokenId.slice(0, 18)}…
                </span>
              </div>
              <button
                onClick={() => setHistoryMarket(null)}
                className="modal-close"
                aria-label="Close price history modal"
              >
                ✕
              </button>
            </div>
            <div className="modal-body space-y-3">
              <PriceHistoryChart
                tokenId={historyMarket.tokenId}
                resolution="5m"
                count={60}
                height={320}
              />
              <div className="text-[10px] text-[#7e8aaa] mono border-t border-[#1f2335] pt-2">
                <span aria-hidden="true">ℹ️</span> Bars are synthetic when no TimescaleDB
                candles are persisted. Chart auto-refreshes every 5s.
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// W9-6 — React.memo with a custom comparator. `books` always changes
// reference on each snapshot (so memo rarely skips on its own), but the
// comparator also collapses identical priceFlashes maps so two snapshots
// with the same mid prices back-to-back won't re-render this 200+ cell
// table. `onSelectMarket` must be stable in the parent (useCallback) —
// otherwise the comparator returns false on every parent render.
export default memo(MarketsPanel, (prev, next) => {
  if (prev.books !== next.books) return false
  if (prev.onSelectMarket !== next.onSelectMarket) return false
  if (JSON.stringify(prev.priceFlashes) !== JSON.stringify(next.priceFlashes)) return false
  // W15-2 — preferences-driven display flag; flipped in the SettingsModal.
  if (prev.showPriceFlashes !== next.showPriceFlashes) return false
  return true
})