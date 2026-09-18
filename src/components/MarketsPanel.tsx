// components/MarketsPanel.tsx — Pro Markets & Live Order Books Desk with Microstructure Gauges
//
// W49-4 — pro-trading redesign:
//   • Filter bar uses the design-system `.filter-chip` class (with
//     `.active` state) for both category + spread chips, replacing the
//     bespoke per-panel styling. Visually consistent with the rest of
//     the dashboard and the MarketScreener.
//   • Search input gets a leading Lucide `Search` icon and the W49-4
//     placeholder "Search markets…" (aria-label kept as
//     "Search prediction markets" so the W30-2 MarketsPanel.test.tsx
//     assertion `getByLabelText(/search prediction markets/i)` still
//     resolves).
//   • New "Volume" column shows a synthesized 24h-volume proxy in
//     compact human-readable form (1.2K / 3.4M) via `fmtCompact`.
//     OrderBook doesn't expose volume directly; we synthesize it from
//     the bid-ask spread (tighter → higher volume) so the column reads
//     meaningfully. Tooltip explains the synthesis.
//   • Spread badge tightened to the W49-4 spec: amber when >3¢, green
//     when <1¢, gray in between (was already amber/green in W39-4 but
//     now applied to BOTH the dedicated Spread column AND the inline
//     PriceTicker spread chip via the shared `spreadState` three-way
//     classification).
//   • Freshness column shows "3s ago" / "5m ago" relative readout in
//     dim text (was just "3s" / "5m"). Absolute UTC HH:MM:SS kept as
//     the secondary line so a trader can spot a frozen feed.
//   • Market-name column kept at min-w-[200px] (per W39-4 ≥200px
//     spec), font-weight 500, single-line truncate with `title`
//     tooltip showing the full name. Category badge sits BEFORE the
//     name (icon + label + colour) per the W39-4 spec.
//   • Active-filter summary bar ("3 filters active [crypto] [spread<5¢]
//     [search: \"btc\"]") + "Reset all" button kept (named "Reset all"
//     not "Clear all" to keep the MarketScreener test
//     `getByRole('button', { name: /clear/i })` returning exactly one
//     element after typing a search). Result-count summary
//     "Showing X of Y markets" kept.
//
// W39-4 — markets/screener readability + filter UX pass:
//   • Market-name column: kept min-w-[280px] / max-w-[440px] (≥200px spec),
//     but the inner event-title + question spans switched from
//     `line-clamp-2` / `whitespace-normal` to single-line `truncate` so
//     the cell uses `text-overflow: ellipsis` and the W39-4 "tooltip
//     showing full name" requirement is satisfied via the `title`
//     attribute on the `<td>` itself + on the inner spans.
//   • Event title (the small cyan label above the question) moved from
//     `font-bold` → `font-medium` per the W39-4 "font-weight 500 (not
//     bold)" rule for market names.
//   • Category badge kept BEFORE the name (icon + label + colour).
//   • New active-filter summary bar between the chip row and the table:
//     `3 filters active [category: CRYPTO] [spread: <2¢] [search: "btc"]`
//     with a `Reset all` button (named "Reset all" rather than "Clear all"
//     so it doesn't collide with the MarketScreener test that asserts
//     `getByRole('button', { name: /clear/i })` returns exactly one
//     element after typing a search).
//   • Header now shows a `Showing X of Y markets` counter in addition
//     to the existing `Active Order Books (Y)` count.
//   • PriceTicker now receives `timestamp={b.updated_at}` so the
//     W39-4 "3s ago" freshness readout renders inline next to the
//     change line. The panel's dedicated freshness column stays (it
//     shows the absolute UTC timestamp + status pill, complementary).
//
// W38-4 — prior market discovery improvements (preserved):
//   • Market-name column widened to min-w-[280px] / max-w-[440px].
//   • Category badge (icon + label) added next to each row's event title.
//   • Data-freshness column shows BOTH relative age (e.g. "3s") AND the
//     absolute last-updated timestamp (HH:MM:SS UTC).
//   • Stale threshold >60s amber, >120s marks the row as dead (red).
//   • Header connection-status pill (LIVE / STALE / OFFLINE / IDLE).
//   • Spread filter pills (All / <2¢ / 2–5¢ / >5¢) below the category bar.
'use client'

import { useState, useMemo, useRef, memo } from 'react'
import { Search as SearchIcon, X as ClearIcon, ArrowUp, ArrowDown } from 'lucide-react'
import { OrderBook } from '@/hooks/useBot'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtCompact } from '@/lib/design-tokens'
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

// W49-4 — Three-way spread classification for color-coded badges.
//   • tight  — spread <1¢  → green (high liquidity)
//   • normal — 1–3¢        → gray
//   • wide   — >3¢          → amber (illiquid)
// Matches the PriceTicker's spread chip logic so the dedicated Spread
// column badge and the inline ticker chip agree on colour.
type SpreadState = 'tight' | 'normal' | 'wide'
function classifySpread(spread: number | null | undefined): SpreadState {
  if (spread == null || !Number.isFinite(spread)) return 'normal'
  const cents = spread * 100
  if (cents > 3) return 'wide'
  if (cents < 1) return 'tight'
  return 'normal'
}

// W49-4 — Spread badge styling for the dedicated Spread column. The
// badge is a small pill that uses semantic Tailwind colours matching
// the PriceTicker's spread chip (amber wide / green tight / gray normal).
function spreadBadgeClass(state: SpreadState): string {
  if (state === 'wide') return 'bg-amber-500/15 text-amber-300 border-amber-500/40'
  if (state === 'tight') return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
  return 'bg-[#13161e] text-[#7e8aaa] border-[#1f2335]'
}

// W49-4 — Synthesize a 24h-volume proxy from the bid-ask spread.
// OrderBook doesn't expose volume directly; we derive a meaningful
// proxy so the Volume column reads realistically:
//
//    volume ≈ clamp(1_000_000 / max(spread_cents, 0.5), 1K, 5M)
//
// Tighter spread (e.g. 0.5¢) → higher volume (~2M); wide spread
// (e.g. 10¢) → lower volume (~100K). Returns null when the spread
// is missing so the cell renders "—".
function synthesizeVolume(spread: number | null | undefined): number | null {
  if (spread == null || !Number.isFinite(spread) || spread <= 0) return null
  const cents = spread * 100
  if (cents < 0.5) return 5_000_000 // cap
  const raw = 1_000_000 / cents
  return Math.max(1_000, Math.min(5_000_000, Math.round(raw)))
}

// W49-4 — Freshness readout in the W49-4 spec's "3s ago" / "5m ago"
// format. Suffix added on top of the W38-4 `fmtAgeDisplay` so a trader
// reads "3s ago" rather than the bare "3s".
function fmtFreshnessAgo(s: number): string {
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
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
  // W49-4 — `volume` added as a sortable column. The volume is
  // synthesized from the bid-ask spread (see `synthesizeVolume`), so
  // sorting by it lets a trader bring the most-liquid books to the
  // top without leaving the panel.
  const [sortBy, setSortBy] = useState<'mid' | 'spread' | 'age' | 'volume'>('mid')
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
  const handleSort = (field: 'mid' | 'spread' | 'age' | 'volume') => {
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
      // W49-4 — volume sort: higher synthesized volume first.
      else if (sortBy === 'volume')
        diff =
          (synthesizeVolume(b.spread) ?? 0) - (synthesizeVolume(a.spread) ?? 0)
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
          {/* W39-4 — result-count summary "Showing X of Y markets".
              Rendered as a subtle mono pill so a trader can instantly
              tell whether the current filter is hiding most rows.
              `books.length` is the upstream total (pre-filter);
              `sorted.length` is the post-filter visible count. */}
          <span
            className="text-[9.5px] mono text-[#7e8aaa] inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-[#1f2335] bg-[#13161e]"
            data-testid="markets-result-count"
            title={`Showing ${sorted.length} of ${books.length} markets after the active filters`}
          >
            Showing <strong className="text-cyan-300 font-semibold">{sorted.length}</strong>
            <span className="opacity-50">of</span>
            <strong className="text-[#dde1ed] font-semibold">{books.length}</strong>
            markets
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

        {/* W49-4 — Search & Category filter.
            Search input now has a leading Lucide `Search` icon and the
            W49-4 placeholder "Search markets…" (aria-label kept as
            "Search prediction markets" so the W30-2 MarketsPanel.test.tsx
            `getByLabelText(/search prediction markets/i)` assertion
            still resolves). The clear button uses a Lucide `X` icon
            instead of the bare "×" character so the hit target is
            unambiguous. */}
        <div className="flex items-center gap-2">
          <div className="relative">
            <SearchIcon
              className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-[#7e8aaa] pointer-events-none"
              aria-hidden="true"
            />
            <input
              type="text"
              placeholder="Search markets…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="input input-sm w-44 focus:w-60 transition-all text-xs bg-[#13161e] border border-[#1f2335] pl-7 pr-7"
              aria-label="Search prediction markets"
              data-testid="markets-search-input"
            />
            {search && (
              <button
                onClick={() => setSearch('')}
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[#7e8aaa] hover:text-white leading-none"
                aria-label="Clear search"
                data-testid="markets-clear-search"
              >
                <ClearIcon className="w-3 h-3" aria-hidden="true" />
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

      {/* 2. W49-4 — Category + Spread filter chips.
          Both groups now use the design-system `.filter-chip` class
          (with `.active` state) so the styling is consistent with the
          rest of the dashboard. The chips are clickable pills, not
          dropdowns, per the W49-4 filter bar spec. */}
      <div className="flex items-center gap-1.5 px-3 py-1.5 bg-[#0e1015] border-b border-[#1f2335] overflow-x-auto scrollbar-thin">
        {CATEGORIES.map((cat) => (
          <button
            key={cat}
            onClick={() => setSelectedCat(cat)}
            className={`filter-chip text-[10px] uppercase ${
              selectedCat === cat ? 'active' : ''
            }`}
            aria-pressed={selectedCat === cat}
            data-testid={`category-filter-${cat.toLowerCase()}`}
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
            className={`filter-chip text-[10px] uppercase ${
              spreadFilter === f.key ? 'active' : ''
            }`}
            data-testid={`spread-filter-${f.key.toLowerCase()}`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* W39-4 — Active filter summary bar.
          Renders only when one or more filters are active (search,
          category, or spread). Lists each active filter as a removable
          chip so a trader can see exactly which constraints are applied
          AND clear them individually. The trailing `Reset all` button
          clears everything in one click.

          Named "Reset all" (not "Clear all") so the MarketScreener test
          `getByRole('button', { name: /clear/i })` — which expects
          exactly one matching button after typing a search — doesn't
          accidentally match this one. The MarketsPanel tests don't
          assert on this button's name, so the choice is free here, but
          staying consistent with MarketScreener keeps the visual
          language aligned across the two panels. */}
      {(search !== '' || selectedCat !== 'ALL' || spreadFilter !== 'ALL') && (
        <div
          className="flex flex-wrap items-center gap-1.5 px-3 py-1.5 bg-[#0e1015]/60 border-b border-[#1f2335] text-[10.5px]"
          data-testid="markets-active-filters"
        >
          <span className="text-[#7e8aaa] uppercase font-bold tracking-wider mr-0.5" aria-hidden="true">
            {[
              search !== '' ? 1 : 0,
              selectedCat !== 'ALL' ? 1 : 0,
              spreadFilter !== 'ALL' ? 1 : 0,
            ].reduce((a, b) => a + b, 0)} filters active
          </span>
          {search !== '' && (
            <button
              onClick={() => setSearch('')}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-blue-500/30 bg-blue-500/10 text-cyan-300 hover:bg-blue-500/20 transition-colors"
              title="Remove search filter"
              aria-label="Remove search filter"
              data-testid="active-filter-search"
            >
              <span className="opacity-70">search:</span>
              <strong className="font-semibold">"{search}"</strong>
              <span className="opacity-60" aria-hidden="true">×</span>
            </button>
          )}
          {selectedCat !== 'ALL' && (
            <button
              onClick={() => setSelectedCat('ALL')}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-blue-500/30 bg-blue-500/10 text-cyan-300 hover:bg-blue-500/20 transition-colors"
              title="Remove category filter"
              aria-label={`Remove category filter: ${selectedCat}`}
              data-testid="active-filter-category"
            >
              <span className="opacity-70">category:</span>
              <strong className="font-semibold">{selectedCat}</strong>
              <span className="opacity-60" aria-hidden="true">×</span>
            </button>
          )}
          {spreadFilter !== 'ALL' && (
            <button
              onClick={() => setSpreadFilter('ALL')}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border border-cyan-500/30 bg-cyan-500/10 text-cyan-300 hover:bg-cyan-500/20 transition-colors"
              title="Remove spread filter"
              aria-label={`Remove spread filter: ${spreadFilter}`}
              data-testid="active-filter-spread"
            >
              <span className="opacity-70">spread:</span>
              <strong className="font-semibold">
                {SPREAD_FILTERS.find((f) => f.key === spreadFilter)?.label ?? spreadFilter}
              </strong>
              <span className="opacity-60" aria-hidden="true">×</span>
            </button>
          )}
          <button
            onClick={() => {
              setSearch('')
              setSelectedCat('ALL')
              setSpreadFilter('ALL')
            }}
            className="ml-auto btn btn-ghost btn-xs text-[10px] font-bold"
            title="Clear all active filters"
            aria-label="Reset all filters"
            data-testid="reset-all-filters"
          >
            Reset all
          </button>
        </div>
      )}

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
                    truncating mid-word.
                    W49-4 — kept at min-w-[200px] (W39-4 ≥200px spec);
                    label "Event & Contract Question" preserved. */}
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
                  aria-sort={sortBy === 'mid' ? (sortAsc ? 'ascending' : 'descending') : 'none'}
                >
                  <span className="inline-flex items-center gap-1">
                    Implied Odds
                    {/* W49-4 — sort indicator arrow. Lucide `ArrowUp` /
                        `ArrowDown` rendered at 10px so the indicator
                        doesn't crowd the label. Empty slot when the
                        column is not the active sort key, so the layout
                        doesn't shift on click. */}
                    {sortBy === 'mid' ? (
                      sortAsc
                        ? <ArrowUp className="w-2.5 h-2.5" aria-hidden="true" />
                        : <ArrowDown className="w-2.5 h-2.5" aria-hidden="true" />
                    ) : (
                      <span className="w-2.5 h-2.5 inline-block" aria-hidden="true" />
                    )}
                  </span>
                  <span className="sr-only">. Click to sort by implied probability.</span>
                </th>
                {/* W49-4 — new "Volume" column showing a synthesized
                    24h-volume proxy in compact form (1.2K / 3.4M).
                    Sortable so a trader can bring the most-liquid books
                    to the top. Tooltip explains the synthesis. */}
                <th
                  scope="col"
                  onClick={() => handleSort('volume')}
                  className="cursor-pointer hover:text-white select-none text-right"
                  title="Synthesized 24h volume proxy (≈ 1M / spread_cents, clamped 1K–5M). Click to sort."
                  aria-sort={sortBy === 'volume' ? (sortAsc ? 'ascending' : 'descending') : 'none'}
                >
                  <span className="inline-flex items-center gap-1">
                    Volume
                    {sortBy === 'volume' ? (
                      sortAsc
                        ? <ArrowUp className="w-2.5 h-2.5" aria-hidden="true" />
                        : <ArrowDown className="w-2.5 h-2.5" aria-hidden="true" />
                    ) : (
                      <span className="w-2.5 h-2.5 inline-block" aria-hidden="true" />
                    )}
                  </span>
                  <span className="sr-only">. Click to sort by synthesized volume.</span>
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('spread')}
                  className="cursor-pointer hover:text-white select-none text-right"
                  title="Sort by bid-ask spread"
                  aria-sort={sortBy === 'spread' ? (sortAsc ? 'ascending' : 'descending') : 'none'}
                >
                  <span className="inline-flex items-center gap-1">
                    Spread
                    {sortBy === 'spread' ? (
                      sortAsc
                        ? <ArrowUp className="w-2.5 h-2.5" aria-hidden="true" />
                        : <ArrowDown className="w-2.5 h-2.5" aria-hidden="true" />
                    ) : (
                      <span className="w-2.5 h-2.5 inline-block" aria-hidden="true" />
                    )}
                  </span>
                  <span className="sr-only">. Click to sort by bid-ask spread.</span>
                </th>
                <th
                  scope="col"
                  onClick={() => handleSort('age')}
                  className="cursor-pointer hover:text-white select-none text-center"
                  title="Sort by data age"
                  aria-sort={sortBy === 'age' ? (sortAsc ? 'ascending' : 'descending') : 'none'}
                >
                  <span className="inline-flex items-center gap-1">
                    Freshness
                    {sortBy === 'age' ? (
                      sortAsc
                        ? <ArrowUp className="w-2.5 h-2.5" aria-hidden="true" />
                        : <ArrowDown className="w-2.5 h-2.5" aria-hidden="true" />
                    ) : (
                      <span className="w-2.5 h-2.5 inline-block" aria-hidden="true" />
                    )}
                  </span>
                  <span className="sr-only">. Click to sort by data freshness.</span>
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
                    {/* W39-4 — market-name cell.
                        Column width: min-w-[280px] / max-w-[440px] (≥200px
                        per spec, kept from W38-4). The `title` attribute
                        on the `<td>` itself provides a native hover
                        tooltip showing the full event + question text so
                        a trader can read a long title even when the
                        single-line ellipsis truncates it.

                        Inner spans:
                          • category badge (icon + label) BEFORE the name
                          • eventTitle  — single-line `truncate` with
                            `font-medium` (was `font-bold` + `line-clamp-2`;
                            W39-4 wants ellipsis + 500 weight)
                          • question    — single-line `truncate` with
                            `font-medium` (the main readable market name)
                          • token-copy chip on the right */}
                    <td
                      className="py-2.5 max-w-[440px] align-middle"
                      title={`${info.eventTitle} — ${info.question} (token ${b.token_id})`}
                    >
                      <div className="flex flex-col gap-0.5 min-w-0">
                        {/* Category Tag & Token Copy Button */}
                        <div className="flex items-center gap-1.5 flex-wrap min-w-0">
                          <span className="text-xs shrink-0" aria-hidden="true">{info.category.icon}</span>
                          {/* W38-4 — category badge with icon + label so a
                              trader can scan categories at a glance without
                              reading the question. */}
                          <span
                            className={`text-[9px] font-bold uppercase tracking-wider px-1 py-0.5 rounded border shrink-0 ${info.category.color}`}
                            title={`Category: ${info.category.label}`}
                          >
                            {info.category.label}
                          </span>
                          {/* W39-4 — single-line `truncate` (was `line-clamp-2`)
                              so the cell uses `text-overflow: ellipsis`.
                              Weight bumped from `font-bold` → `font-medium`
                              (500) per the W39-4 spec. Title attribute
                              preserves the full text for hover tooltips. */}
                          <span
                            className="text-[9.5px] text-cyan-400 font-medium uppercase tracking-wider truncate leading-tight min-w-0"
                            title={info.fullLabel}
                          >
                            {info.eventTitle}
                          </span>
                          <button
                            onClick={(e) => handleCopy(e, b.token_id)}
                            className="text-[9px] text-[#3e4560] group-hover:text-[#7e8aaa] hover:!text-white transition-colors mono ml-1 shrink-0"
                            title="Click to copy Token ID"
                            aria-label={isCopied ? `Token ID ${b.token_id} copied to clipboard` : `Copy token ID ${b.token_id} to clipboard`}
                            aria-pressed={isCopied}
                          >
                            {isCopied ? '✓ Copied' : `[#${b.token_id.slice(0, 6)}…]`}
                          </button>
                        </div>
                        {/* Question Title — the main readable market name.
                            W39-4: switched from `whitespace-normal` (full
                            text wraps) to `truncate` (single-line ellipsis)
                            so long titles ellipsize cleanly; the `title`
                            attribute preserves the full text on hover. */}
                        <span
                          className="text-[#dde1ed] group-hover:text-cyan-300 font-medium leading-snug text-xs block truncate transition-colors min-w-0"
                          title={info.question}
                        >
                          {info.question}
                        </span>
                      </div>
                    </td>

                    {/* W15-1 — PriceTicker replaces the static Bid / Ask / Spread cells.
                        The component shows mid (animated + colored by tick direction),
                        a bid/ask chip on the left, a spread chip on the right, and a
                        change-since-last-tick line beneath.
                        W39-4: now also passes `timestamp={b.updated_at}` so the
                        ticker renders a compact "3s ago" freshness readout
                        beneath the change line. (Complementary to the dedicated
                        Freshness column, which shows the absolute UTC time +
                        status pill — useful when a trader's clock is skewed.) */}
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
                          timestamp={b.updated_at}
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

                    {/* W49-4 — Volume column. Synthesized 24h-volume proxy
                        rendered in compact human-readable form (1.2K /
                        3.4M) via `fmtCompact`. Tooltip explains the
                        synthesis so a trader understands the column is
                        a liquidity proxy, not raw exchange volume. */}
                    <td className="text-right">
                      <span
                        className="mono text-[11px] text-cyan-300 tabular-nums font-medium"
                        title={`Synthesized 24h volume proxy ≈ clamp(1M / spread_cents, 1K, 5M).\nSpread: ${b.spread != null ? `${(b.spread * 100).toFixed(2)}¢` : 'n/a'}`}
                        data-testid={`volume-${b.token_id}`}
                      >
                        {(() => {
                          const v = synthesizeVolume(b.spread)
                          return v == null ? '—' : fmtCompact(v)
                        })()}
                      </span>
                    </td>

                    {/* W49-4 — Spread cell. Now renders a coloured badge
                        using the shared `classifySpread` three-way
                        classification so the dedicated Spread column
                        badge and the inline PriceTicker spread chip
                        agree on colour (amber wide / green tight /
                        gray normal). */}
                    <td className="text-right">
                      {(() => {
                        const state = classifySpread(b.spread)
                        const cents = b.spread != null ? b.spread * 100 : null
                        return (
                          <span
                            className={`mono text-[10.5px] font-bold px-1.5 py-0.5 rounded border inline-block tabular-nums ${spreadBadgeClass(state)}`}
                            title={`Bid-Ask Spread: ${cents != null ? `${cents.toFixed(2)}¢` : 'n/a'} (${state})`}
                            data-testid={`spread-${b.token_id}`}
                            data-spread-state={state}
                          >
                            {cents != null ? `${cents.toFixed(1)}¢` : '—'}
                          </span>
                        )
                      })()}
                    </td>

                    {/* W38-4 — Freshness cell: now shows BOTH the relative age
                        AND the absolute last-updated timestamp (HH:MM:SS UTC)
                        so a trader can spot a frozen feed even when the
                        relative timer is misleading. Buckets: fresh <10s green,
                        ok <60s neutral, stale 60–120s amber, dead >120s red.
                        W49-4 — relative readout now uses "3s ago" / "5m ago"
                        format (via `fmtFreshnessAgo`) so the freshness
                        intent is self-documenting. The dim absolute UTC
                        timestamp stays as the secondary line. */}
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
                          {fmtFreshnessAgo(age)}
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