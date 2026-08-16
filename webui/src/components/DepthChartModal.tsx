// components/DepthChartModal.tsx — Interactive Order Book Depth & Quick Trade Modal
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface DepthLevel {
  price: number
  size: number
  total: number
}

interface DepthData {
  token_id: string
  slug: string
  bids: DepthLevel[]
  asks: DepthLevel[]
  mid: number | null
  spread: number | null
  best_bid: number | null
  best_ask: number | null
}

interface Props {
  tokenId: string | null
  slug: string | null
  onClose: () => void
  onOrderPlaced?: () => void
}

export default function DepthChartModal({ tokenId, slug, onClose, onOrderPlaced }: Props) {
  const [data, setData] = useState<DepthData | null>(null)
  const [side, setSide] = useState<'BUY' | 'SELL'>('BUY')
  const [price, setPrice] = useState<string>('0.50')
  const [sizeUsdc, setSizeUsdc] = useState<string>('1.5')
  const [loading, setLoading] = useState(false)
  const [feedback, setFeedback] = useState<string | null>(null)

  useEffect(() => {
    if (!tokenId) return
    const fetchDepth = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await fetch(`${apiUrl}/api/depth/${tokenId}`)
        if (res.ok) {
          const json = await res.json()
          setData(json)
          if (json.mid && price === '0.50') {
            setPrice(json.mid.toFixed(2))
          }
        }
      } catch {}
    }
    fetchDepth()
    const timer = setInterval(fetchDepth, 2000)
    return () => clearInterval(timer)
  }, [tokenId])

  if (!tokenId) return null

  const handleTrade = async () => {
    setLoading(true)
    setFeedback(null)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/trade`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id: tokenId,
          price: parseFloat(price),
          side,
          size_usdc: parseFloat(sizeUsdc),
        }),
      })
      if (res.ok) {
        setFeedback('✅ Order submitted successfully')
        if (onOrderPlaced) onOrderPlaced()
      } else {
        const err = await res.json()
        setFeedback(`❌ Failed: ${err.detail || 'Error'}`)
      }
    } catch {
      setFeedback('❌ Error reaching API server')
    }
    setLoading(false)
  }

  const maxBidTotal = data?.bids[data.bids.length - 1]?.total || 1
  const maxAskTotal = data?.asks[data.asks.length - 1]?.total || 1
  const maxTotal = Math.max(maxBidTotal, maxAskTotal, 1)

  return (
    <div className="fixed inset-0 bg-black/80 flex items-center justify-center z-50 backdrop-blur-sm p-4">
      <div className="card w-full max-w-2xl bg-[#111318] border border-[#252836] shadow-2xl rounded-lg overflow-hidden flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="card-header flex justify-between items-center px-4 py-3 border-b border-[#252836]">
          <div>
            <span className="card-title text-sm font-semibold text-[#e8eaf0] block">
              📊 Market Depth: <span className="text-blue-400">{slug || tokenId.slice(0, 16)}</span>
            </span>
            <span className="text-[11px] text-[#8b91a8] mono">
              Mid: {data?.mid ? `${(data.mid * 100).toFixed(1)}¢` : '—'} | Spread: {data?.spread ? `${(data.spread * 100).toFixed(1)}¢` : '—'}
            </span>
          </div>
          <button onClick={onClose} className="text-[#8b91a8] hover:text-white text-lg px-2">
            ✕
          </button>
        </div>

        {/* Content */}
        <div className="p-4 overflow-y-auto space-y-4 flex-1">
          {/* Depth Chart Columns */}
          <div className="grid grid-cols-2 gap-3 text-[11px]">
            {/* Bids */}
            <div className="bg-[#161822] p-2.5 rounded border border-[#252836]">
              <div className="text-[10px] font-semibold uppercase text-green-400 mb-2">Bids (Buy Orders)</div>
              <div className="space-y-1">
                {data?.bids && data.bids.length > 0 ? (
                  data.bids.map((b, i) => (
                    <div
                      key={i}
                      onClick={() => setPrice(b.price.toFixed(4))}
                      className="flex justify-between items-center relative py-0.5 px-1 rounded cursor-pointer hover:bg-green-500/10"
                    >
                      <div
                        className="absolute left-0 top-0 bottom-0 bg-green-500/20 rounded-l transition-all duration-300"
                        style={{ width: `${(b.total / maxTotal) * 100}%` }}
                      />
                      <span className="mono text-green-400 z-10 font-medium">{b.price.toFixed(4)}</span>
                      <span className="mono text-[#8b91a8] z-10">{b.size.toFixed(1)} ({b.total.toFixed(0)})</span>
                    </div>
                  ))
                ) : (
                  <div className="text-[#4a5068] text-center py-4">No active bids</div>
                )}
              </div>
            </div>

            {/* Asks */}
            <div className="bg-[#161822] p-2.5 rounded border border-[#252836]">
              <div className="text-[10px] font-semibold uppercase text-red-400 mb-2">Asks (Sell Orders)</div>
              <div className="space-y-1">
                {data?.asks && data.asks.length > 0 ? (
                  data.asks.map((a, i) => (
                    <div
                      key={i}
                      onClick={() => setPrice(a.price.toFixed(4))}
                      className="flex justify-between items-center relative py-0.5 px-1 rounded cursor-pointer hover:bg-red-500/10"
                    >
                      <div
                        className="absolute right-0 top-0 bottom-0 bg-red-500/20 rounded-r transition-all duration-300"
                        style={{ width: `${(a.total / maxTotal) * 100}%` }}
                      />
                      <span className="mono text-[#8b91a8] z-10">{a.size.toFixed(1)} ({a.total.toFixed(0)})</span>
                      <span className="mono text-red-400 z-10 font-medium">{a.price.toFixed(4)}</span>
                    </div>
                  ))
                ) : (
                  <div className="text-[#4a5068] text-center py-4">No active asks</div>
                )}
              </div>
            </div>
          </div>

          {/* Quick Trade Form */}
          <div className="bg-[#161822] p-3 rounded border border-[#252836] space-y-3">
            <div className="text-[11px] font-semibold uppercase text-[#8b91a8] flex justify-between items-center">
              <span>Quick Trade Execution</span>
              <div className="flex gap-1">
                <button
                  type="button"
                  onClick={() => setSide('BUY')}
                  className={`px-3 py-1 rounded text-[11px] font-semibold transition-all ${
                    side === 'BUY' ? 'bg-green-500 text-black' : 'bg-[#252836] text-[#8b91a8]'
                  }`}
                >
                  Buy YES
                </button>
                <button
                  type="button"
                  onClick={() => setSide('SELL')}
                  className={`px-3 py-1 rounded text-[11px] font-semibold transition-all ${
                    side === 'SELL' ? 'bg-red-500 text-white' : 'bg-[#252836] text-[#8b91a8]'
                  }`}
                >
                  Sell YES
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Limit Price ($0.01 - $0.99)</label>
                <input
                  type="number"
                  step="0.005"
                  min="0.01"
                  max="0.99"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  className="w-full bg-[#111318] border border-[#252836] rounded px-2.5 py-1.5 mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
                />
              </div>
              <div>
                <label className="text-[10px] text-[#4a5068] block mb-1">Order Size (USDC) — Max $3/market</label>
                <input
                  type="number"
                  step="0.5"
                  min="0.5"
                  max="3"
                  value={sizeUsdc}
                  onChange={(e) => setSizeUsdc(e.target.value)}
                  className="w-full bg-[#111318] border border-[#252836] rounded px-2.5 py-1.5 mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
                />
              </div>
            </div>

            <div className="flex justify-between items-center pt-1">
              <span className="text-[11px] text-[#8b91a8] mono">
                Est. Shares: {price && sizeUsdc ? (parseFloat(sizeUsdc) / Math.max(parseFloat(price), 0.01)).toFixed(1) : '—'}
              </span>
              <button
                onClick={handleTrade}
                disabled={loading}
                className={`btn font-semibold px-4 py-1.5 text-xs ${
                  side === 'BUY' ? 'btn-success' : 'btn-danger'
                }`}
              >
                {loading ? 'Submitting…' : `Place ${side} Order`}
              </button>
            </div>

            {feedback && (
              <div className="text-[11px] font-medium pt-1 text-center">{feedback}</div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
