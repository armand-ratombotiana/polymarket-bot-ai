// components/MarketScreener.tsx — Multi-factor Prediction Market Screener
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface MarketItem {
  id?: string
  conditionId?: string
  slug: string
  groupItemTitle?: string
  category?: string
  volume24hr?: number
  liquidity?: number
  outcomePrices?: string
  tokens?: Array<{ token_id: string; outcome: string }>
}

interface Props {
  onSelectMarket?: (tokenId: string, slug: string) => void
  onQuickTrade?: (tokenId: string, slug: string) => void
}

export default function MarketScreener({ onSelectMarket, onQuickTrade }: Props) {
  const [markets, setMarkets] = useState<MarketItem[]>([])
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(false)

  const fetchMarkets = async (q: string = '') => {
    setLoading(true)
    try {
      const apiUrl = getApiUrl()
      const url = q
        ? `${apiUrl}/api/markets?search=${encodeURIComponent(q)}&limit=40`
        : `${apiUrl}/api/markets?limit=40`
      const res = await fetch(url)
      if (res.ok) {
        const data = await res.json()
        setMarkets(data.markets || [])
      }
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    fetchMarkets()
    const timer = setInterval(() => fetchMarkets(search), 30000)
    return () => clearInterval(timer)
  }, [])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    fetchMarkets(search)
  }

  return (
    <div className="card flex flex-col h-full bg-[#111318] border border-[#252836] overflow-hidden">
      {/* Header & Controls */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#252836] gap-3">
        <div className="flex items-center gap-2">
          <span className="card-title text-base font-bold text-[#e8eaf0]">
            🔍 Prediction Market Screener
          </span>
          <span className="badge badge-dim text-xs">{markets.length} Results</span>
        </div>

        <form onSubmit={handleSearch} className="flex gap-2">
          <input
            type="text"
            placeholder="Search all Polymarket events…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="bg-[#161822] border border-[#252836] rounded-md px-3 py-1 text-xs mono text-[#e8eaf0] placeholder-[#4a5068] focus:outline-none focus:border-blue-500 w-56"
          />
          <button type="submit" className="btn btn-primary px-3 py-1 text-xs font-semibold">
            Search
          </button>
        </form>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-y-auto scrollbar-thin">
        {loading ? (
          <div className="flex items-center justify-center h-48 text-[#8b91a8] text-xs">
            Scanning prediction markets…
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Market Event</th>
                <th>Category</th>
                <th>24h Volume</th>
                <th>Liquidity</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody>
              {markets.length === 0 ? (
                <tr>
                  <td colSpan={5} className="text-center py-10 text-[#4a5068] text-xs">
                    No markets found{search ? ` for "${search}"` : ''}. Try a different search or refresh.
                  </td>
                </tr>
              ) : (
                markets.map((m, i) => {
                  const title = m.groupItemTitle || m.slug
                  const vol = parseFloat(String(m.volume24hr || 0))
                  const liq = parseFloat(String(m.liquidity || 0))
                  const tokenId = m.tokens?.[0]?.token_id || m.conditionId || m.slug

                  return (
                    <tr key={i} className="hover:bg-blue-500/10 transition-colors">
                      <td className="max-w-[280px]">
                        <span className="text-[#e8eaf0] font-medium block truncate" title={title}>
                          {title}
                        </span>
                        <span className="text-[10px] text-[#4a5068] mono">{m.slug}</span>
                      </td>
                      <td>
                        <span className="badge badge-blue text-[10px] uppercase">
                          {m.category || 'general'}
                        </span>
                      </td>
                      <td className="mono text-cyan-400 font-medium">
                        ${vol.toLocaleString('en', { maximumFractionDigits: 0 })}
                      </td>
                      <td className="mono text-[#8b91a8]">
                        ${liq.toLocaleString('en', { maximumFractionDigits: 0 })}
                      </td>
<td>
                        <button
                          onClick={() => onQuickTrade ? onQuickTrade(tokenId, m.slug) : onSelectMarket && onSelectMarket(tokenId, m.slug)}
                          className="btn btn-ghost text-blue-400 hover:text-white border-blue-900/40 text-[11px] px-2.5 py-1"
                        >
                          Trade / Depth
                        </button>
                      </td>
                    </tr>
                  )
                })
              )}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
