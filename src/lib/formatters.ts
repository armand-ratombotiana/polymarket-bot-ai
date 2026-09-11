// lib/formatters.ts — Market slug → human-readable title, category badge,
// and hierarchical (event / question) display helpers.

export interface MarketCategoryInfo {
  icon: string
  label: string
  color: string
}

export interface HierarchicalMarketInfo {
  category: MarketCategoryInfo
  eventTitle: string
  question: string
  fullLabel: string
}

interface CategoryRule {
  keywords: string[]
  info: MarketCategoryInfo
}

const CATEGORY_RULES: CategoryRule[] = [
  {
    keywords: ['bitcoin', 'crypto', 'ethereum', 'eth', 'solana', 'bnb', 'doge', 'xrp', 'cardano', 'litecoin', 'token', 'coin', 'halving', 'memecoin'],
    info: { icon: '₿', label: 'CRYPTO', color: 'border-amber-400/40 text-amber-300' },
  },
  {
    keywords: ['election', 'president', 'trump', 'biden', 'harris', 'congress', 'senate', 'governor', 'mayor', 'primaries', 'campaign', 'vote', 'poll', 'inauguration', 'ballot'],
    info: { icon: '🏛', label: 'POLITICS', color: 'border-blue-400/40 text-blue-300' },
  },
  {
    keywords: ['nba', 'nfl', 'mlb', 'nhl', 'super-bowl', 'world-cup', 'fifa', 'tennis', 'ncaa', 'championship', 'quarterback', 'playoff', 'final-four', 'grand-slam', 'olympics'],
    info: { icon: '🏀', label: 'SPORTS', color: 'border-emerald-400/40 text-emerald-300' },
  },
  {
    keywords: ['fed', 'rate-cut', 'inflation', 'gdp', 'recession', 'unemployment', 'tariff', 'interest-rate', 'stock-market', 's&p', 'nasdaq', 'treasury', 'oil-price', 'dow', 'fomc'],
    info: { icon: '📉', label: 'ECONOMY', color: 'border-rose-400/40 text-rose-300' },
  },
  {
    keywords: ['weather', 'snow', 'temperature', 'hurricane', 'storm', 'rain', 'heat-wave', 'tornado', 'record-high', 'record-low', 'rainfall'],
    info: { icon: '⛈', label: 'WEATHER', color: 'border-cyan-400/40 text-cyan-300' },
  },
  {
    keywords: ['spacex', 'starship', 'nasa', 'rocket', 'launch', 'mars', 'orbit', 'iss', 'artemis', 'space'],
    info: { icon: '🚀', label: 'SPACE', color: 'border-violet-400/40 text-violet-300' },
  },
  {
    keywords: ['ai', 'artificial-intelligence', 'chatgpt', 'openai', 'llm', 'robot', 'agi', 'deepmind', 'agent', 'model-', 'gpt'],
    info: { icon: '🤖', label: 'AI & TECH', color: 'border-fuchsia-400/40 text-fuchsia-300' },
  },
  {
    keywords: ['movie', 'oscar', 'grammy', 'celebrity', 'album', 'tv', 'show', 'netflix', 'taylor-swift', 'beyonce', 'box-office', 'marvel', 'disney', 'youtube', 'tiktok'],
    info: { icon: '🎬', label: 'ENTERTAINMENT', color: 'border-pink-400/40 text-pink-300' },
  },
]

const DEFAULT_CATEGORY: MarketCategoryInfo = {
  icon: '📊',
  label: 'MARKETS',
  color: 'border-slate-400/40 text-slate-300',
}

function slugify(value: string): string {
  return String(value || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
}

function humanize(word: string): string {
  return word.replace(/-/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function getCategoryBadge(category: string, slug?: string): MarketCategoryInfo {
  const raw = `${category || ''} ${slug || ''}`
  const haystack = slugify(raw)
  if (!haystack) return DEFAULT_CATEGORY
  const rule = CATEGORY_RULES.find((r) =>
    r.keywords.some((k) => haystack.includes(slugify(k)))
  )
  return rule ? rule.info : DEFAULT_CATEGORY
}

export function formatMarketTitle(slug: string): string {
  if (!slug) return 'Unknown Market'
  const cleaned = slugify(slug)
  const words = cleaned.split('-').filter(Boolean)
  if (words.length === 0) return humanize(slug)
  if (words.length <= 3) return humanize(words.join(' '))
  // "will-x-happen" style slugs: keep the leading verb + core subject,
  // drop trailing prepositions/articles for a compact title.
  const stop = new Set(['the', 'a', 'an', 'of', 'in', 'on', 'by', 'at', 'to', 'for', 'and', 'or', 'this', 'next'])
  const head = words.filter((w, i) => i === 0 || i === 1 || !stop.has(w))
  return humanize(head.join(' '))
}

export function formatHierarchicalMarket(slug?: string | null): HierarchicalMarketInfo {
  if (!slug) {
    return {
      category: { ...DEFAULT_CATEGORY },
      eventTitle: 'UNKNOWN',
      question: 'Unknown market',
      fullLabel: 'Unknown market',
    }
  }

  const cleaned = slugify(slug)
  const words = cleaned.split('-').filter(Boolean)
  const category = getCategoryBadge('', cleaned)

  // Event = first word(s) after a leading "will" if present, else first word.
  let eventWords: string[] = []
  let questionWords: string[] = []
  if (words.length <= 2) {
    eventWords = words
  } else if (words[0] === 'will' && words.length >= 4) {
    eventWords = words.slice(0, 2)
    questionWords = words.slice(2)
  } else {
    eventWords = words.slice(0, 1)
    questionWords = words.slice(1)
  }

  const eventTitle = humanize(eventWords.join(' ')).toUpperCase()
  const question = humanize(questionWords.join(' ')) || humanize(cleaned)

  return {
    category,
    eventTitle,
    question,
    fullLabel: humanize(cleaned),
  }
}
