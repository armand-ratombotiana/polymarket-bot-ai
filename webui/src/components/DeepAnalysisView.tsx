// components/DeepAnalysisView.tsx — Deep Market Intelligence, Alpha Opportunities & Whale Activity Tracker
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

interface AlphaOpportunity {
  token_id: string
  slug: string
  alpha_score: number
  regime: string
  implied_prob: number
  ml_prob: number
  spread_pct: number
  ofi: number
  sentiment: number
  category: string
}

interface WhaleAlert {
  token_id: string
  slug: string
  side: string
  size_usdc: number
  price: number
  timestamp: number
  impact_score: number
}

interface NewsItem {
  headline: string
  source: string
  category: string
  timestamp: number
  sentiment: number
  related_tokens: string[]
}

interface DeepAnalysisData {
  top_opportunities: AlphaOpportunity[]
  whale_activity: WhaleAlert[]
  regimes: Record<string, string>
  correlation_matrix: {
    categories: string[]
    matrix: number[][]
  }
  recent_news: NewsItem[]
  timestamp: number
}

export default function DeepAnalysisView() {
  const [data, setData] = useState<DeepAnalysisData | null>(null)
  const [loading, setLoading] = useState(true)

  const fetchData = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/analysis/deep`)
      if (res.ok) {
        setData(await res.json())
      }
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    fetchData()
    const timer = setInterval(fetchData, 4000)
    return () => clearInterval(timer)
  }, [])

  if (loading || !data) {
    return (
      <div className="flex items-center justify-center h-full text-xs text-[#8b91a8]">
        Analyzing order books, whale block flow, and cross-category correlations…
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex justify-between items-center pb-3 border-b border-[#252836]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🔬</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              Deep Market Intelligence, Alpha Opportunities &amp; Whale Flow
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Multi-Factor Alpha Scoring, Large Order Flow Imbalance, NLP News Sentiment &amp; Cross-Category Correlations
          </p>
        </div>
        <span className="badge badge-green text-xs font-semibold">Live Model Pipeline Active</span>
      </div>

      {/* Top Opportunities Table */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836]">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚡ Top 10 Ranked Alpha Opportunities (High Expected Value)
          </span>
          <span className="text-[10px] text-[#8b91a8] mono">Sorted by Alpha Composite</span>
        </div>

        <div className="overflow-x-auto scrollbar-thin">
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Market Contract</th>
                <th>Category</th>
                <th>Regime</th>
                <th>Implied Prob</th>
                <th>ML Forecast</th>
                <th>Spread</th>
                <th>OFI Flow</th>
                <th>Sentiment</th>
                <th className="text-right">Alpha Score</th>
              </tr>
            </thead>
            <tbody>
              {data.top_opportunities.map((opp) => {
                const title = formatMarketTitle(opp.slug)
                const cat = getCategoryBadge(opp.category, opp.slug)
                return (
                  <tr key={opp.token_id} className="hover:bg-blue-500/10 transition-colors">
                    <td className="max-w-[200px]">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs shrink-0">{cat.icon}</span>
                        <span className="text-[#e8eaf0] font-semibold block truncate text-[11px]" title={title}>
                          {title}
                        </span>
                      </div>
                    </td>
                    <td>
                      <span className={`text-[10px] px-1.5 py-0.5 rounded border ${cat.color}`}>
                        {cat.label}
                      </span>
                    </td>
                    <td>
                      <span className="badge badge-dim text-[10px] mono">{opp.regime}</span>
                    </td>
                    <td className="mono text-[#8b91a8]">{(opp.implied_prob * 100).toFixed(1)}%</td>
                    <td className="mono font-semibold text-cyan-400">{(opp.ml_prob * 100).toFixed(1)}%</td>
                    <td className="mono text-[#8b91a8]">{(opp.spread_pct * 100).toFixed(1)}%</td>
                    <td className={`mono font-bold ${opp.ofi >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {opp.ofi >= 0 ? '+' : ''}{opp.ofi.toFixed(2)}
                    </td>
                    <td className="mono text-amber-400 font-medium">
                      {opp.sentiment !== 0 ? `${opp.sentiment > 0 ? '+' : ''}${opp.sentiment.toFixed(2)}` : '0.00'}
                    </td>
                    <td className="text-right">
                      <span className="mono font-bold text-green-400 bg-green-500/10 px-2 py-0.5 rounded border border-green-500/20">
                        +{opp.alpha_score.toFixed(3)}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* Whale Alerts & Fundamental News 2-Column Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Whale Activity Feed */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              🐋 Whale Block Flow Tracker (&gt; $5,000 USDC)
            </span>
            <span className="badge badge-blue text-[10px]">Real-Time Smart Money</span>
          </div>

          <div className="space-y-2 overflow-y-auto max-h-56 scrollbar-thin">
            {data.whale_activity.length === 0 ? (
              <div className="text-center py-6 text-[#4a5068] text-xs">
                Scanning order books for block size trades &gt; $5,000…
              </div>
            ) : (
              data.whale_activity.map((w, i) => {
                const title = formatMarketTitle(w.slug)
                return (
                  <div key={i} className="flex justify-between items-center bg-[#111318] p-2 rounded border border-[#252836] text-xs">
                    <div className="truncate max-w-[200px]">
                      <span className="text-[#e8eaf0] font-semibold block truncate text-[11px]" title={title}>{title}</span>
                      <span className="text-[10px] text-[#8b91a8] mono">Price: ${w.price.toFixed(3)}</span>
                    </div>
                    <div className="text-right">
                      <span className="mono font-bold text-green-400 block">${w.size_usdc.toLocaleString()}</span>
                      <span className={`text-[9px] px-1.5 py-0.2 rounded font-bold ${w.side === 'BUY' ? 'badge-green' : 'badge-red'}`}>
                        {w.side} YES
                      </span>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </div>

        {/* Fundamental News & Sentiment */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              📰 Macro &amp; Fundamental Sentiment Stream
            </span>
            <span className="text-[10px] text-cyan-400 mono">NLP Entity Scored</span>
          </div>

          <div className="space-y-2 overflow-y-auto max-h-56 scrollbar-thin">
            {data.recent_news.map((n, i) => (
              <div key={i} className="bg-[#111318] p-2 rounded border border-[#252836] text-xs space-y-1">
                <div className="flex justify-between items-center">
                  <span className="text-[10px] text-[#4a5068] mono font-semibold">{n.source} • {n.category}</span>
                  <span
                    className={`text-[10px] font-bold px-1.5 py-0.2 rounded ${
                      n.sentiment > 0
                        ? 'bg-green-500/20 text-green-400'
                        : n.sentiment < 0
                        ? 'bg-red-500/20 text-red-400'
                        : 'bg-slate-500/20 text-slate-300'
                    }`}
                  >
                    Sentiment: {n.sentiment > 0 ? `+${n.sentiment}` : n.sentiment}
                  </span>
                </div>
                <p className="text-[#e8eaf0] font-medium leading-snug text-[11px]">{n.headline}</p>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
