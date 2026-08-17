// components/MarketScreener.tsx — Multi-factor Prediction Market Screener
'use client'

import { useEffect, useState, useRef, useCallback } from 'react'
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
  const [error, setError] = useState<string | null>(null)
  const [lastRefreshed, setLastRefreshed] = useState<number | null>(null)
  
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
    } catch {
      setError('Network error while querying Gamma markets')
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

  return (
    <div className="card flex flex-col h-full bg-[#13161e] border border-[#1f2335] overflow-hidden">
      {/* Header & Controls */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#1f2335] gap-3">
        <div className="flex items-center gap-2">
          <span className="card-title text-sm font-bold text-[#dde1ed]">
            🔍 Prediction Market Screener
          </span>
          <span className="badge badge-dim text-xs">
            {markets.length} Markets
          </span>
          {lastRefreshed && (
            <span className="text-[10.5px] text-[#7e8aaa] mono">
              Refreshed {fmtAge(lastRefreshed)}
            </span>
          )}
        </div>

        <form onSubmit={handleSearch} className="flex items-center gap-2">
          <input
            type="text"
            placeholder="Search Polymarket events…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="input input-sm w-56 text-xs"
            aria-label="Search prediction market events"
          />
          <button type="submit" className="btn btn-primary btn-sm" disabled={loading}>
            {loading ? <span className="spinner" aria-hidden="true" /> : 'Search'}
          </button>
          {search && (
            <button
              type="button"
              onClick={() => { setSearch(''); fetchMarkets(''); }}
              className="btn btn-ghost btn-sm"
              title="Clear search filter"
            >
              Clear
            </button>
          )}
        </form>
      </div>

      {/* Error state */}
      {error && (
        <div className="banner-danger mx-3 mt-2 text-xs py-1.5 px-3">
          <span aria-hidden="true">⚠️</span>
          <span>{error}</span>
          <button onClick={() => fetchMarkets()} className="ml-auto underline cursor-pointer">
            Retry
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
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {markets.length === 0 ? (
                <tr>
                  <td colSpan={5} className="text-center py-10 text-[#7e8aaa] text-xs">
                    No markets found{search ? ` for "${search}"` : ''}. Try adjusting your search query.
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
                      <td className="max-w-[340px]">
                        <span className="text-[#dde1ed] font-medium block truncate" title={title}>
                          {title}
                        </span>
                        <span className="text-[10px] text-[#7e8aaa] mono block truncate">{m.slug}</span>
                      </td>
                      <td>
                        <span className="badge badge-blue text-[9.5px] uppercase">
                          {m.category || 'general'}
                        </span>
                      </td>
                      <td className="mono text-cyan-400 font-medium">
                        {fmtUsd(vol, 0)}
                      </td>
                      <td className="mono text-[#7e8aaa]">
                        {fmtUsd(liq, 0)}
                      </td>
                      <td className="text-right">
                        <button
                          onClick={() => onQuickTrade ? onQuickTrade(tokenId, m.slug) : onSelectMarket && onSelectMarket(tokenId, m.slug)}
                          className="btn btn-primary btn-xs"
                          aria-label={`Open depth and trade ticket for ${title}`}
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
