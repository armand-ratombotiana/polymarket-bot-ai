// components/MarketScreener.tsx — Multi-factor Prediction Market Screener
//
// W38-4 — market discovery improvements:
//   • Opportunity score (0–100) computed via a transparent weighted
//     formula that combines liquidity, 24h volume, spread tightness,
//     AI confidence, and time-to-resolution. Each factor is normalized
//     0..1 against the current page of results so the relative ranking
//     stays meaningful even when the absolute numbers are tiny.
//   • Score badge tooltip shows the full breakdown (per-factor points +
//     weights) so a trader can see exactly WHY a market ranks where it
//     does — no opaque "AI score" black box.
//   • New filter chips: AI confidence (≥50% / ≥70%), edge (≥2¢ / ≥5¢),
//     time-to-resolution (Any / <1d / <7d / <30d). The chips compose
//     with the existing category + search filters.
//   • "Export CSV" button in the header — downloads the currently
//     filtered result set as a CSV file (slug, title, category, vol,
//     liquidity, opportunity score, AI confidence, edge, resolution).
//   • Improved empty/no-results states — empty-result rows now show
//     the active filter context + a reset button so the trader can
//     tell whether they over-constrained the view (vs. the upstream
//     actually being empty).

'use client'

import { useEffect, useState, useRef, useCallback, useMemo } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtAge } from '@/lib/design-tokens'

interface MarketItem {
  id?: string
  conditionId?: string
  slug: string
  groupItemTitle?: string
  category?: string
  volume24hr?: number
  liquidity?: number
  outcomePrices?: string
  // W38-4 — optional ISO date the market resolves. Used to compute
  // "time to resolution" (days remaining). May be absent on legacy
  // payloads; we fall back to a null bucket.
  endDate?: string
  // W38-4 — optional upstream-provided AI confidence 0..1. When absent,
  // the screener synthesizes a confidence from volume + liquidity
  // (high volume + liquidity → high confidence in price discovery).
  aiConfidence?: number
  tokens?: Array<{ token_id: string; outcome: string }>
}

interface Props {
  onSelectMarket?: (tokenId: string, slug: string) => void
  onQuickTrade?: (tokenId: string, slug: string) => void
}

// W38-4 — opportunity score weights (documented + shown in UI tooltip).
//   • Liquidity  35% — deeper books → tighter spreads → safer to size
//   • Volume     30% — confirms genuine trader interest (vs. dead book)
//   • Spread     15% — tighter spread = lower cost to enter/exit
//   • AI conf    10% — model's directional conviction (when available)
//   • Resolution 10% — closer resolution = less time-value decay risk
//
// Weights sum to 1.0. Each factor is min-max normalized against the
// current page of results so the relative ranking stays meaningful.
const SCORE_WEIGHTS = {
  liquidity: 0.35,
  volume: 0.30,
  spread: 0.15,
  aiConfidence: 0.10,
  resolution: 0.10,
} as const

// W38-4 — derived row computed from the raw MarketItem + the page's
// normalization stats. The score is rounded to an integer 0..100 so
// the badge + CSV stay readable.
interface ScoredMarket {
  market: MarketItem
  tokenId: string
  title: string
  category: string
  volume: number
  liquidity: number
  spreadCents: number | null
  aiConfidence: number
  edgeCents: number
  daysToResolution: number | null
  // Per-factor contribution to the final score (0..100, after weighting).
  // Surfaced in the badge tooltip so a trader can see WHY a market ranks
  // where it does.
  scoreBreakdown: {
    liquidity: number
    volume: number
    spread: number
    aiConfidence: number
    resolution: number
  }
  score: number
}

// W38-4 — derive the per-market edge (in cents) from liquidity + volume.
//
// "Edge" here is a heuristic proxy for the theoretical edge a market
// maker can capture on this market: deeper liquidity relative to 24h
// volume means the book is over-capitalized (small edge per trade), while
// thin liquidity on a high-volume market means each trade moves price
// (large potential edge but high risk). We model it as:
//
//    edge_cents = clamp(5 * (volume / max(liquidity, 1)), 0, 10)
//
// So when liquidity == volume, edge = 5¢; high-volume + low-liquidity
// pushes toward 10¢; low-volume + high-liquidity pushes toward 0¢.
function deriveEdgeCents(volume: number, liquidity: number): number {
  if (!Number.isFinite(volume) || !Number.isFinite(liquidity) || liquidity <= 0) {
    return 0
  }
  const raw = 5 * (volume / liquidity)
  return Math.max(0, Math.min(10, raw))
}

// W38-4 — derive a synthetic AI confidence (0..1) when the upstream
// payload doesn't include one. Combines volume + liquidity into a
// [0, 1] signal: higher volume and deeper liquidity → higher
// confidence in the current price discovery.
function deriveAiConfidence(m: MarketItem, volume: number, liquidity: number): number {
  if (typeof m.aiConfidence === 'number' && Number.isFinite(m.aiConfidence)) {
    return Math.max(0, Math.min(1, m.aiConfidence))
  }
  // Synthetic: cap each input at a meaningful ceiling so a single
  // mega-market doesn't saturate the score.
  const volSignal = Math.min(1, Math.log10(Math.max(1, volume)) / 6) // 1M → 1.0
  const liqSignal = Math.min(1, Math.log10(Math.max(1, liquidity)) / 5) // 100k → 1.0
  return Math.max(0, Math.min(1, 0.5 * volSignal + 0.5 * liqSignal))
}

// W38-4 — parse the market's endDate into "days to resolution" from now.
// Returns null when the date is missing, malformed, or in the past.
function deriveDaysToResolution(m: MarketItem): number | null {
  if (!m.endDate || typeof m.endDate !== 'string') return null
  const t = Date.parse(m.endDate)
  if (!Number.isFinite(t)) return null
  const days = (t - Date.now()) / 86_400_000
  return days > 0 ? Math.round(days) : null
}

// W38-4 — derive the synthetic bid-ask spread (in cents) from
// liquidity + volume. Polymarket's Gamma API doesn't always include
// a real spread for inactive books; we synthesize one so the
// opportunity score has something to chew on:
//
//    spread_cents ≈ clamp(20 / sqrt(liquidity), 0.5, 20)
//
// Deep liquidity → tight spread; thin liquidity → wide spread.
function deriveSpreadCents(liquidity: number, volume: number): number | null {
  if (!Number.isFinite(liquidity) || liquidity <= 0) return null
  const liqComponent = 20 / Math.sqrt(liquidity)
  // Volume dampens spread slightly (active markets tighten).
  const volComponent = Number.isFinite(volume) && volume > 0
    ? Math.min(2, Math.log10(volume) / 3)
    : 0
  return Math.max(0.5, Math.min(20, liqComponent - volComponent))
}

// W38-4 — min-max normalize a numeric value against the page's
// [min, max] range. Returns 0..1 (0 when min == max). Negative
// inputs clamp to 0.
function minMax(v: number, min: number, max: number): number {
  if (!Number.isFinite(v)) return 0
  if (max <= min) return 0
  const n = (v - min) / (max - min)
  return Math.max(0, Math.min(1, n))
}

// W38-4 — compute the scored-market rows from the raw markets list.
//
// The function runs three passes:
//   1. Per-row derived metrics (edge, AI confidence, spread, days-to-
//      resolution) — these don't depend on the rest of the page.
//   2. Page-wide min/max for each numeric factor so we can normalize.
//   3. Final weighted score + per-factor breakdown (post-weight, in
//      points out of 100) for each row.
//
// Returns the scored rows (NOT sorted — sorting happens in a
// separate memo so the user's column sort takes precedence).
function computeScoredMarkets(markets: MarketItem[]): ScoredMarket[] {
  if (!markets.length) return []

  // Pass 1: derive per-row metrics.
  const rows = markets.map((m) => {
    const title = m.groupItemTitle || m.slug
    const volume = parseFloat(String(m.volume24hr || 0)) || 0
    const liquidity = parseFloat(String(m.liquidity || 0)) || 0
    const tokenId = m.tokens?.[0]?.token_id || m.conditionId || m.slug
    const category = (m.category || 'general').toUpperCase()
    const spreadCents = deriveSpreadCents(liquidity, volume)
    const aiConfidence = deriveAiConfidence(m, volume, liquidity)
    const edgeCents = deriveEdgeCents(volume, liquidity)
    const daysToResolution = deriveDaysToResolution(m)
    return {
      market: m,
      tokenId,
      title,
      category,
      volume,
      liquidity,
      spreadCents,
      aiConfidence,
      edgeCents,
      daysToResolution,
    }
  })

  // Pass 2: compute page-wide min/max for each numeric factor.
  const volMax = Math.max(1, ...rows.map((r) => r.volume))
  const liqMax = Math.max(1, ...rows.map((r) => r.liquidity))
  const spreadMin = rows.reduce(
    (acc, r) => (r.spreadCents != null ? Math.min(acc, r.spreadCents) : acc),
    Infinity,
  )
  const spreadMax = rows.reduce(
    (acc, r) => (r.spreadCents != null ? Math.max(acc, r.spreadCents) : acc),
    -Infinity,
  )
  const edgeMax = Math.max(1, ...rows.map((r) => r.edgeCents))
  // For resolution: closer = better, but a missing date shouldn't
  // dominate. Treat null as "neutral" (0.5) so it neither helps nor
  // hurts the score.
  const daysMax = Math.max(1, ...rows.map((r) => r.daysToResolution ?? 1))

  // Pass 3: weighted score + breakdown (each factor contributes
  // weight * normalized * 100 points).
  return rows.map((r) => {
    const volScore = minMax(r.volume, 0, volMax)
    const liqScore = minMax(r.liquidity, 0, liqMax)
    // Spread is inverted: tighter (lower) = higher score.
    const spreadScore =
      r.spreadCents == null || !Number.isFinite(spreadMin) || !Number.isFinite(spreadMax) || spreadMax <= spreadMin
        ? 0.5
        : 1 - minMax(r.spreadCents, spreadMin, spreadMax)
    const confScore = r.aiConfidence // already 0..1
    const edgeScore = minMax(r.edgeCents, 0, edgeMax)
    const resScore = r.daysToResolution == null ? 0.5 : 1 - minMax(r.daysToResolution, 0, daysMax)

    const breakdown = {
      liquidity: SCORE_WEIGHTS.liquidity * liqScore * 100,
      volume: SCORE_WEIGHTS.volume * volScore * 100,
      spread: SCORE_WEIGHTS.spread * spreadScore * 100,
      aiConfidence: SCORE_WEIGHTS.aiConfidence * confScore * 100,
      resolution: SCORE_WEIGHTS.resolution * resScore * 100,
    }
    const score = Math.round(
      breakdown.liquidity +
        breakdown.volume +
        breakdown.spread +
        breakdown.aiConfidence +
        breakdown.resolution,
    )
    return { ...r, scoreBreakdown: breakdown, score }
  })
}

// W38-4 — CSV export of the currently filtered (post-filter) rows.
// Triggers a client-side download via a Blob URL; no API round-trip.
function exportRowsToCSV(rows: ScoredMarket[]): void {
  if (!rows.length) return
  const header = [
    'slug',
    'title',
    'category',
    'volume_24h_usd',
    'liquidity_usd',
    'spread_cents',
    'ai_confidence_pct',
    'edge_cents',
    'days_to_resolution',
    'opportunity_score',
  ]
  const escape = (v: string | number | null | undefined): string => {
    if (v == null) return ''
    const s = String(v)
    // Quote any field that contains commas, quotes, or newlines.
    if (/[",\n]/.test(s)) return `"${s.replace(/"/g, '""')}"`
    return s
  }
  const body = rows.map((r) =>
    [
      r.market.slug,
      r.title,
      r.category,
      r.volume.toFixed(2),
      r.liquidity.toFixed(2),
      r.spreadCents != null ? r.spreadCents.toFixed(2) : '',
      (r.aiConfidence * 100).toFixed(1),
      r.edgeCents.toFixed(2),
      r.daysToResolution != null ? String(r.daysToResolution) : '',
      String(r.score),
    ]
      .map(escape)
      .join(','),
  )
  const csv = [header.join(','), ...body].join('\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `polymarket-screener-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '')}.csv`
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  // Revoke on next tick so the download has time to start.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

// W38-4 — AI confidence filter chips.
type AiConfidenceFilter = 'ALL' | 'GTE50' | 'GTE70'
const AI_CONFIDENCE_FILTERS: { key: AiConfidenceFilter; label: string; min: number; title: string }[] = [
  { key: 'ALL', label: 'All', min: 0, title: 'Show all confidence levels' },
  { key: 'GTE50', label: '≥50%', min: 0.5, title: 'AI confidence ≥ 50%' },
  { key: 'GTE70', label: '≥70%', min: 0.7, title: 'AI confidence ≥ 70% (high conviction)' },
]

// W38-4 — Edge filter chips (in cents).
type EdgeFilter = 'ALL' | 'GTE2' | 'GTE5'
const EDGE_FILTERS: { key: EdgeFilter; label: string; minCents: number; title: string }[] = [
  { key: 'ALL', label: 'All', minCents: 0, title: 'Show all edge levels' },
  { key: 'GTE2', label: '≥2¢', minCents: 2, title: 'Edge ≥ 2¢ (minimum tradable)' },
  { key: 'GTE5', label: '≥5¢', minCents: 5, title: 'Edge ≥ 5¢ (high-edge)' },
]

// W38-4 — Time-to-resolution filter chips (in days).
type ResolutionFilter = 'ALL' | 'LT1D' | 'LT7D' | 'LT30D'
const RESOLUTION_FILTERS: { key: ResolutionFilter; label: string; maxDays: number | null; title: string }[] = [
  { key: 'ALL', label: 'Any', maxDays: null, title: 'Show all resolution windows' },
  { key: 'LT1D', label: '<1d', maxDays: 1, title: 'Resolves within 1 day' },
  { key: 'LT7D', label: '<7d', maxDays: 7, title: 'Resolves within 7 days' },
  { key: 'LT30D', label: '<30d', maxDays: 30, title: 'Resolves within 30 days' },
]

// W38-4 — score badge colour buckets. Score 0..100 maps to:
//   ≥75 → emerald (top opportunities)
//   50–74 → cyan (solid)
//   25–49 → amber (marginal)
//   <25 → slate (skip)
function scoreBadgeClass(score: number): string {
  if (score >= 75) return 'bg-emerald-500/15 text-emerald-300 border-emerald-500/40'
  if (score >= 50) return 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40'
  if (score >= 25) return 'bg-amber-500/15 text-amber-300 border-amber-500/40'
  return 'bg-slate-500/15 text-slate-300 border-slate-500/40'
}

export default function MarketScreener({ onSelectMarket, onQuickTrade }: Props) {
  const [markets, setMarkets] = useState<MarketItem[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null)

  // W38-4 — additional filter state for AI confidence, edge, and
  // time-to-resolution chips. These compose with the existing category
  // + search filters.
  const [selectedCategory, setSelectedCategory] = useState<string>('ALL')
  const [aiConfidenceFilter, setAiConfidenceFilter] = useState<AiConfidenceFilter>('ALL')
  const [edgeFilter, setEdgeFilter] = useState<EdgeFilter>('ALL')
  const [resolutionFilter, setResolutionFilter] = useState<ResolutionFilter>('ALL')

  const searchRef = useRef(search)
  searchRef.current = search

  const fetchMarkets = useCallback(async (q?: string) => {
    const query = q !== undefined ? q : searchRef.current
    setLoading(true)
    setError(null)
    try {
      const apiUrl = getApiUrl()
      const url = query
        ? `${apiUrl}/api/markets?search=${encodeURIComponent(query)}&limit=50`
        : `${apiUrl}/api/markets?limit=50`
      const res = await apiFetch(url)
      if (res.ok) {
        const data = await res.json()
        setMarkets(data.markets || [])
        setLastRefreshed(Date.now() / 1000)
      } else {
        setError(`Failed to load markets (HTTP ${res.status})`)
      }
    } catch (e) {
      console.error('[MarketScreener] Failed to fetch markets:', e)
      setError(e instanceof Error ? e.message : 'Network error while querying Gamma markets')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchMarkets('')
    const timer = setInterval(() => {
      fetchMarkets(searchRef.current)
    }, 30000)
    return () => clearInterval(timer)
  }, [fetchMarkets])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    fetchMarkets(search)
  }

  const CATEGORY_CHIPS = ['ALL', 'CRYPTO', 'POLITICS', 'SPORTS', 'ECONOMY', 'TECH']

  // W38-4 — pre-compute scored rows once per markets list (so the
  // tooltip / CSV / filter logic all read from the same source).
  const scoredAll = useMemo(() => computeScoredMarkets(markets), [markets])

  // Apply category + AI confidence + edge + resolution filters.
  const filteredScored = useMemo(() => {
    const aiMin = AI_CONFIDENCE_FILTERS.find((f) => f.key === aiConfidenceFilter)?.min ?? 0
    const edgeMin = EDGE_FILTERS.find((f) => f.key === edgeFilter)?.minCents ?? 0
    const resMax = RESOLUTION_FILTERS.find((f) => f.key === resolutionFilter)?.maxDays ?? null
    return scoredAll.filter((s) => {
      if (selectedCategory !== 'ALL') {
        const cat = s.category
        const slug = s.market.slug.toUpperCase()
        const matchCat =
          (selectedCategory === 'CRYPTO' && (cat.includes('CRYPTO') || slug.includes('BITCOIN') || slug.includes('ETH') || slug.includes('SOL'))) ||
          (selectedCategory === 'POLITICS' && (cat.includes('POLITICS') || slug.includes('ELECTION') || slug.includes('PRESIDENT') || slug.includes('TRUMP'))) ||
          (selectedCategory === 'SPORTS' && (cat.includes('SPORTS') || slug.includes('NBA') || slug.includes('NFL') || slug.includes('SOCCER'))) ||
          (selectedCategory === 'ECONOMY' && (cat.includes('ECONOMY') || slug.includes('FED') || slug.includes('INFLATION') || slug.includes('RATE'))) ||
          (selectedCategory === 'TECH' && (cat.includes('TECH') || slug.includes('AI') || slug.includes('OPENAI') || slug.includes('GPT')))
        if (!matchCat) return false
      }
      if (s.aiConfidence < aiMin) return false
      if (s.edgeCents < edgeMin) return false
      if (resMax != null && (s.daysToResolution == null || s.daysToResolution > resMax)) return false
      return true
    })
  }, [scoredAll, selectedCategory, aiConfidenceFilter, edgeFilter, resolutionFilter])

  // Backwards-compat: filteredMarkets is the same as filteredScored but
  // exposes the raw MarketItem shape for the test "3 of 3 Markets" badge.
  // The header badge counts the post-filter set.
  const filteredMarkets = useMemo(() => filteredScored.map((s) => s.market), [filteredScored])

  // W38-4 — opportunity score tooltip: full breakdown per factor.
  // Rendered as the title attribute on the score badge so hover reveals
  // exactly how many points each factor contributed (transparent formula).
  function scoreTooltip(s: ScoredMarket): string {
    const lines = [
      `Opportunity Score: ${s.score}/100`,
      `  Liquidity  ×${SCORE_WEIGHTS.liquidity.toFixed(2)}  → ${s.scoreBreakdown.liquidity.toFixed(1)} pts`,
      `  Volume     ×${SCORE_WEIGHTS.volume.toFixed(2)}  → ${s.scoreBreakdown.volume.toFixed(1)} pts`,
      `  Spread     ×${SCORE_WEIGHTS.spread.toFixed(2)}  → ${s.scoreBreakdown.spread.toFixed(1)} pts`,
      `  AI conf    ×${SCORE_WEIGHTS.aiConfidence.toFixed(2)}  → ${s.scoreBreakdown.aiConfidence.toFixed(1)} pts`,
      `  Resolution ×${SCORE_WEIGHTS.resolution.toFixed(2)}  → ${s.scoreBreakdown.resolution.toFixed(1)} pts`,
      ``,
      `Liquidity: ${fmtUsd(s.liquidity, 0)}`,
      `Volume 24h: ${fmtUsd(s.volume, 0)}`,
      s.spreadCents != null ? `Spread: ${s.spreadCents.toFixed(1)}¢` : `Spread: n/a`,
      `AI confidence: ${(s.aiConfidence * 100).toFixed(0)}%`,
      `Edge: ${s.edgeCents.toFixed(1)}¢`,
      s.daysToResolution != null ? `Days to resolution: ${s.daysToResolution}` : `Days to resolution: n/a`,
    ]
    return lines.join('\n')
  }

  const hasActiveFilters =
    selectedCategory !== 'ALL' ||
    aiConfidenceFilter !== 'ALL' ||
    edgeFilter !== 'ALL' ||
    resolutionFilter !== 'ALL' ||
    Boolean(search)

  const resetAllFilters = () => {
    setSelectedCategory('ALL')
    setAiConfidenceFilter('ALL')
    setEdgeFilter('ALL')
    setResolutionFilter('ALL')
    setSearch('')
    fetchMarkets('')
  }

  return (
    <div className="card flex flex-col h-full bg-[#13161e] border border-[#1f2335] overflow-hidden shadow-xl">
      {/* Header & Controls */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#1f2335] gap-3">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="card-title text-sm font-bold text-[#dde1ed]">
            🔍 Prediction Market Screener
          </span>
          <span className="badge badge-cyan text-xs font-semibold">
            {filteredMarkets.length} of {markets.length} Markets
          </span>
          {lastRefreshed && (
            <span className="text-[10.5px] text-[#7e8aaa] mono">
              Refreshed {fmtAge(lastRefreshed)}
            </span>
          )}
          {/* W38-4 — Export CSV button. Renders the currently filtered
              result set as a CSV download. Disabled when no rows. */}
          <button
            type="button"
            onClick={() => exportRowsToCSV(filteredScored)}
            disabled={filteredScored.length === 0}
            className="btn btn-ghost btn-xs text-[10.5px] font-bold disabled:opacity-40 disabled:cursor-not-allowed"
            aria-label="Export filtered markets to CSV"
            title={`Export ${filteredScored.length} filtered market${filteredScored.length === 1 ? '' : 's'} to CSV`}
            data-testid="export-csv-btn"
          >
            ⤓ Export CSV
          </button>
        </div>

        <form onSubmit={handleSearch} className="flex items-center gap-2">
          <input
            type="text"
            placeholder="Search Polymarket events…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input input-sm w-56 text-xs bg-[#0e1015] border border-[#1f2335]"
            aria-label="Search prediction market events"
          />
          <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>
            {loading ? <span className="spinner" aria-hidden="true" /> : 'Search'}
          </button>
          {search && (
            <button
              type="button"
              onClick={() => { setSearch(''); fetchMarkets(''); }}
              className="btn btn-ghost btn-sm text-xs"
              title="Clear search filter"
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {/* Category Chips Filter Bar */}
      <div className="flex items-center gap-1.5 px-4 py-2 bg-[#0e1015] border-b border-[#1f2335] overflow-x-auto scrollbar-thin">
        {CATEGORY_CHIPS.map((cat) => (
          <button
            key={cat}
            onClick={() => setSelectedCategory(cat)}
            className={`px-2.5 py-1 rounded text-[10.5px] font-bold uppercase transition-all ${
              selectedCategory === cat
                ? 'bg-blue-500/20 text-cyan-300 border border-blue-500/40'
                : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
            }`}
          >
            {cat}
          </button>
        ))}
      </div>

      {/* W38-4 — Additional factor filter chips: AI confidence, edge, time-to-resolution */}
      <div className="flex items-center gap-3 px-4 py-2 bg-[#0e1015]/60 border-b border-[#1f2335] overflow-x-auto scrollbar-thin text-[10px]">
        <div className="flex items-center gap-1.5">
          <span className="text-[#7e8aaa] uppercase font-bold tracking-wider mr-0.5" aria-hidden="true">AI Conf</span>
          {AI_CONFIDENCE_FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setAiConfidenceFilter(f.key)}
              title={f.title}
              aria-pressed={aiConfidenceFilter === f.key}
              className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
                aiConfidenceFilter === f.key
                  ? 'bg-fuchsia-500/20 text-fuchsia-300 border border-fuchsia-500/40'
                  : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
              }`}
              data-testid={`ai-conf-filter-${f.key.toLowerCase()}`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="w-px h-4 bg-[#1f2335]" aria-hidden="true" />
        <div className="flex items-center gap-1.5">
          <span className="text-[#7e8aaa] uppercase font-bold tracking-wider mr-0.5" aria-hidden="true">Edge</span>
          {EDGE_FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setEdgeFilter(f.key)}
              title={f.title}
              aria-pressed={edgeFilter === f.key}
              className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
                edgeFilter === f.key
                  ? 'bg-amber-500/20 text-amber-300 border border-amber-500/40'
                  : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
              }`}
              data-testid={`edge-filter-${f.key.toLowerCase()}`}
            >
              {f.label}
            </button>
          ))}
        </div>
        <span className="w-px h-4 bg-[#1f2335]" aria-hidden="true" />
        <div className="flex items-center gap-1.5">
          <span className="text-[#7e8aaa] uppercase font-bold tracking-wider mr-0.5" aria-hidden="true">Resolution</span>
          {RESOLUTION_FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setResolutionFilter(f.key)}
              title={f.title}
              aria-pressed={resolutionFilter === f.key}
              className={`px-1.5 py-0.5 rounded text-[10px] font-bold uppercase transition-all ${
                resolutionFilter === f.key
                  ? 'bg-emerald-500/20 text-emerald-300 border border-emerald-500/40'
                  : 'text-[#7e8aaa] hover:text-[#dde1ed] bg-[#13161e] border border-[#1f2335]'
              }`}
              data-testid={`resolution-filter-${f.key.toLowerCase()}`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>

      {/* Error state */}
      {error && (
        <div className="banner-danger mx-3 mt-2 text-xs py-1.5 px-3 flex items-center gap-2" role="alert">
          <span aria-hidden="true">⚠️</span>
          <span className="flex-1 truncate">{error}</span>
          <button onClick={() => fetchMarkets()} className="underline cursor-pointer shrink-0">
            Retry
          </button>
          <button
            onClick={() => setError(null)}
            className="underline cursor-pointer shrink-0"
            aria-label="Dismiss error"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* Table */}
      <div className="flex-1 overflow-y-auto scrollbar-thin table-container">
        {loading && markets.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 text-[#7e8aaa] text-xs">
            <span className="spinner mb-2" aria-hidden="true" />
            Scanning Polymarket prediction markets…
          </div>
        ) : (
          <table className="data-table" role="table" aria-label="Prediction market screener results">
            <thead>
              <tr>
                <th scope="col" className="min-w-[260px]">Market Event</th>
                <th scope="col">Category</th>
                <th scope="col">24h Volume</th>
                <th scope="col">Liquidity</th>
                {/* W38-4 — Opportunity Score column. Tooltip on the
                    header explains the formula; tooltip on each badge
                    shows the per-factor breakdown. */}
                <th scope="col" className="text-right" title="Opportunity Score = 0.35·liquidity + 0.30·volume + 0.15·spread + 0.10·AI_conf + 0.10·resolution (each factor min-max normalized 0..1, then weighted, scaled to 100). Hover any badge for the breakdown.">
                  Score
                </th>
                {/* W38-4 — Edge column shows derived theoretical edge in cents. */}
                <th scope="col" className="text-right" title="Theoretical edge in cents (heuristic: 5 × volume / liquidity, clamped to 0–10¢)">
                  Edge
                </th>
                {/* W38-4 — Time to resolution column. */}
                <th scope="col" className="text-right" title="Days until market resolution (from endDate if present)">
                  Resolution
                </th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {filteredScored.length === 0 ? (
                // W38-4 — improved empty state. Shows the active filter
                // context + a reset button so the trader can tell whether
                // they over-constrained the view vs. the upstream
                // actually being empty.
                <tr>
                  <td colSpan={8} className="text-center py-10 text-[#7e8aaa] text-xs">
                    <div className="flex flex-col items-center gap-2">
                      <span className="text-2xl" aria-hidden="true">🔍</span>
                      <div className="text-[#dde1ed] font-semibold">
                        No markets found{search ? ` for "${search}"` : ''}
                      </div>
                      {hasActiveFilters ? (
                        <>
                          <div className="text-[10.5px] mono">
                            active filters: {[
                              selectedCategory !== 'ALL' && `cat=${selectedCategory}`,
                              aiConfidenceFilter !== 'ALL' && `ai_conf=${aiConfidenceFilter}`,
                              edgeFilter !== 'ALL' && `edge=${edgeFilter}`,
                              resolutionFilter !== 'ALL' && `res=${resolutionFilter}`,
                              search && `search="${search}"`,
                            ].filter(Boolean).join(' · ') || 'none'}
                          </div>
                          <button
                            type="button"
                            onClick={resetAllFilters}
                            className="btn btn-ghost btn-xs text-[10px] mt-1"
                          >
                            Reset all filters
                          </button>
                        </>
                      ) : (
                        <div className="text-[10.5px]">
                          Try adjusting your search query or category filter.
                        </div>
                      )}
                    </div>
                  </td>
                </tr>
              ) : (
                filteredScored.map((s, i) => (
                  <tr
                    key={i}
                    onClick={() => onSelectMarket && onSelectMarket(s.tokenId, s.market.slug)}
                    className="hover:bg-blue-500/10 transition-colors cursor-pointer group"
                  >
                    <td className="max-w-[340px]">
                      <span className="text-[#dde1ed] group-hover:text-cyan-300 font-medium block truncate transition-colors" title={s.title}>
                        {s.title}
                      </span>
                      <span className="text-[10px] text-[#7e8aaa] mono block truncate">{s.market.slug}</span>
                    </td>
                    <td>
                      <span className="badge badge-blue text-[9.5px] uppercase">
                        {s.market.category || 'general'}
                      </span>
                    </td>
                    <td className="mono text-cyan-400 font-medium">
                      {fmtUsd(s.volume, 0)}
                    </td>
                    <td className="mono text-[#7e8aaa]">
                      {fmtUsd(s.liquidity, 0)}
                    </td>
                    {/* W38-4 — Opportunity Score badge with full breakdown
                        in the tooltip (transparent formula). */}
                    <td className="text-right">
                      <span
                        className={`mono text-[10.5px] font-bold px-1.5 py-0.5 rounded border ${scoreBadgeClass(s.score)}`}
                        title={scoreTooltip(s)}
                        data-testid={`opportunity-score-${i}`}
                        data-score={s.score}
                      >
                        {s.score}
                      </span>
                    </td>
                    {/* W38-4 — Edge (cents) */}
                    <td className="mono text-right text-amber-300 text-[11px] font-medium">
                      {s.edgeCents.toFixed(1)}¢
                    </td>
                    {/* W38-4 — Time to resolution */}
                    <td className="mono text-right text-[#7e8aaa] text-[11px]">
                      {s.daysToResolution != null ? `${s.daysToResolution}d` : '—'}
                    </td>
                    <td className="text-right">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          if (onQuickTrade) onQuickTrade(s.tokenId, s.market.slug)
                          else if (onSelectMarket) onSelectMarket(s.tokenId, s.market.slug)
                        }}
                        className="btn btn-primary btn-xs font-semibold"
                        aria-label={`Open depth and trade ticket for ${s.title}`}
                      >
                        Trade / Depth
                      </button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
