// components/DeepAnalysisView.tsx — Deep Market Analysis, Whale Tracker & Fundamental Intelligence
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface Opportunity {
  token_id: string
  slug: string
  mid_price: number
  spread: number
  alpha_score: number
  direction: string
  regime: string
  sentiment: string
  ofi: number
}

interface WhaleActivity {
  token_id: string
  slug: string
  side: string
  price: number
  size_usdc: number
  timestamp: number
}

interface NewsItem {
  headline: string
  source: string
  category: string
  timestamp: number
  sentiment: number
}

interface DeepAnalysisData {
  opportunities: Opportunity[]
  whale_alerts: WhaleActivity[]
  correlations: { categories: string[]; matrix: number[][] }
  recent_news: NewsItem[]
}

function age(ts: number) {
  const s = Math.floor(Date.now() / 1000 - ts)
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
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
        Analyzing order books, whale flows &amp; fundamental sentiment…
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
              Deep Market Intelligence &amp; Whale Flow Analysis
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Real-time Opportunity Ranking, Institutional Block Flows, Market Regimes &amp; Fundamental News
          </p>
        </div>
        <span className="badge badge-green text-xs font-semibold">Live Scanner Active</span>
      </div>

      {/* Top Opportunities Matrix */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836]">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚡ Top 10 Quantitative Opportunity Rankings (Alpha Score)
          </span>
          <span className="text-[10px] text-cyan-400 mono">OFI + Spread + ML + Sentiment</span>
        </div>

        <div className="overflow-x-auto scrollbar-thin">
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Market Event</th>
                <th>Mid Price</th>
                <th>Spread</th>
                <th>Alpha Score</th>
                <th>Action Signal</th>
                <th>Market Regime</th>
                <th>Sentiment</th>
              </tr>
            </thead>
            <tbody>
              {data.opportunities.map((opp, i) => (
                <tr key={i} className="hover:bg-blue-500/5 transition-colors">
                  <td className="max-w-[200px]">
                    <span className="text-[#e8eaf0] font-medium block truncate" title={opp.slug}>
                      {opp.slug}
                    </span>
                  </td>
                  <td className="mono text-cyan-400 font-semibold">${opp.mid_price.toFixed(3)}</td>
                  <td className="mono text-[#8b91a8]">{(opp.spread * 100).toFixed(1)}¢</td>
                  <td>
                    <div className="flex items-center gap-1.5">
                      <div className="w-16 h-1.5 bg-[#111318] rounded-full overflow-hidden">
                        <div
                          className="h-full bg-gradient-to-r from-blue-500 to-green-400 rounded-full"
                          style={{ width: `${Math.min(opp.alpha_score, 100)}%` }}
                        />
                      </div>
                      <span className="mono font-bold text-green-400">{opp.alpha_score}</span>
                    </div>
                  </td>
                  <td>
                    <span
                      className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                        opp.direction.includes('BUY YES')
                          ? 'bg-green-500/20 text-green-400 border border-green-500/40'
                          : 'bg-red-500/20 text-red-400 border border-red-500/40'
                      }`}
                    >
                      {opp.direction}
                    </span>
                  </td>
                  <td>
                    <span className="text-[10px] text-[#8b91a8] bg-[#111318] px-2 py-0.5 rounded border border-[#252836] mono">
                      {opp.regime}
                    </span>
                  </td>
                  <td>
                    <span
                      className={`text-[10px] font-semibold ${
                        opp.sentiment === 'Bullish' ? 'text-green-400' : opp.sentiment === 'Bearish' ? 'text-red-400' : 'text-[#8b91a8]'
                      }`}
                    >
                      {opp.sentiment}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* 2-Column Grid: Whale Block Flows & Fundamental News */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left: Whale Tracker */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <div className="flex items-center gap-2">
              <span>🐋</span>
              <span className="card-title text-xs font-bold text-[#e8eaf0]">
                Whale Block Activity Tracker (&gt; $5,000 USDC)
              </span>
            </div>
            <span className="badge badge-amber text-[10px]">Smart-Money Flow</span>
          </div>

          <div className="space-y-2 overflow-y-auto max-h-64 scrollbar-thin">
            {data.whale_alerts.map((w, i) => (
              <div key={i} className="flex justify-between items-center bg-[#111318] p-2 rounded border border-[#252836] text-xs">
                <div>
                  <div className="flex items-center gap-2">
                    <span
                      className={`text-[10px] font-bold px-1.5 py-0.2 rounded ${
                        w.side === 'BUY' ? 'bg-green-500/20 text-green-400' : 'bg-red-500/20 text-red-400'
                      }`}
                    >
                      {w.side}
                    </span>
                    <span className="text-[#e8eaf0] font-medium truncate max-w-[180px]">{w.slug}</span>
                  </div>
                  <span className="text-[10px] text-[#4a5068] mono mt-0.5 block">{age(w.timestamp)}</span>
                </div>
                <div className="text-right">
                  <span className="mono font-bold text-amber-400 block">${w.size_usdc.toLocaleString()}</span>
                  <span className="mono text-[10px] text-[#8b91a8]">@ {w.price.toFixed(4)}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Right: Fundamental News & Sentiment */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <div className="flex items-center gap-2">
              <span>📰</span>
              <span className="card-title text-xs font-bold text-[#e8eaf0]">
                Breaking News &amp; Fundamental Sentiment Feed
              </span>
            </div>
            <span className="badge badge-blue text-[10px]">NLP Scored</span>
          </div>

          <div className="space-y-2 overflow-y-auto max-h-64 scrollbar-thin">
            {data.recent_news.map((n, i) => (
              <div key={i} className="bg-[#111318] p-2 rounded border border-[#252836] text-xs space-y-1">
                <p className="text-[#e8eaf0] leading-snug">{n.headline}</p>
                <div className="flex justify-between items-center text-[10px] text-[#4a5068]">
                  <span className="mono">{n.source} • {n.category}</span>
                  <span
                    className={`font-semibold ${
                      n.sentiment > 0.1 ? 'text-green-400' : n.sentiment < -0.1 ? 'text-red-400' : 'text-[#8b91a8]'
                    }`}
                  >
                    Sentiment: {n.sentiment > 0 ? '+' : ''}{(n.sentiment * 100).toFixed(0)}%
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
