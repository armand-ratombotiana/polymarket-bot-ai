// components/DepthChartModal.tsx — Interactive Order Book Depth & Quick Trade Modal
'use client'

import { useEffect, useState, useRef } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtPrice } from '@/lib/design-tokens'

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
  const [feedback, setFeedback] = useState<{ ok: boolean; text: string } | null>(null)
  const [_lastFetched, setLastFetched] = useState<number | null>(null)
  
  const modalRef = useRef<HTMLDivElement>(null)

  // Escape key handler
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  const priceRef = useRef(price)
  priceRef.current = price

  useEffect(() => {
    if (!tokenId) return
    const fetchDepth = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/depth/${tokenId}`)
        if (res.ok) {
          const json = await res.json()
          setData(json)
          setLastFetched(Date.now())
          if (json.mid && priceRef.current === '0.50') {
            setPrice(json.mid.toFixed(3))
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
      const res = await apiFetch(`${apiUrl}/api/trade`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id: tokenId,
          price: parseFloat(price),
          side,
          size_usdc: parseFloat(sizeUsdc),
        }),
      })
      const body = await res.json().catch(() => null)
      if (res.ok) {
        setFeedback({ ok: true, text: body?.detail || `Order filled: ${side} $${sizeUsdc} @ ${price}` })
        if (onOrderPlaced) onOrderPlaced()
      } else {
        setFeedback({ ok: false, text: body?.detail || `Risk gate rejected: HTTP ${res.status}` })
      }
    } catch {
      setFeedback({ ok: false, text: 'Network error submitting trade' })
    }
    setLoading(false)
  }

  const maxBidTotal = data?.bids[data.bids.length - 1]?.total || 1
  const maxAskTotal = data?.asks[data.asks.length - 1]?.total || 1
  const maxTotal = Math.max(maxBidTotal, maxAskTotal, 1)

  return (
    <div
      className="modal-backdrop"
      onClick={(e) => { if (e.target === e.currentTarget) onClose() }}
      role="presentation"
    >
      <div
        ref={modalRef}
        className="modal modal-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="depth-modal-title"
      >
        {/* Header */}
        <div className="modal-header">
          <div>
            <div className="flex items-center gap-2">
              <span id="depth-modal-title" className="text-sm font-bold text-[#dde1ed]">
                📊 Order Book Depth: <span className="text-blue-400">{slug || tokenId.slice(0, 16)}</span>
              </span>
              <span className="badge badge-amber text-[9.5px]">Paper</span>
            </div>
            <span className="text-[11px] text-[#7e8aaa] mono mt-0.5 block">
              Mid: {data?.mid ? `${(data.mid * 100).toFixed(1)}¢` : '—'} | Spread: {data?.spread ? `${(data.spread * 100).toFixed(1)}¢` : '—'}
            </span>
          </div>
          <button
            onClick={onClose}
            className="modal-close"
            aria-label="Close market depth modal"
          >
            ✕
          </button>
        </div>

        {/* Content */}
        <div className="modal-body space-y-4">
          {/* Depth Chart Columns */}
          <div className="grid grid-cols-2 gap-3 text-[11px]">
            {/* Bids */}
            <div className="bg-[#0e1015] p-2.5 rounded border border-[#1f2335]">
              <div className="text-[10px] font-bold uppercase text-green-400 mb-2 flex justify-between">
                <span>Bids (Buy Orders)</span>
                <span>Cumulative</span>
              </div>
              <div className="space-y-1">
                {data?.bids && data.bids.length > 0 ? (
                  data.bids.map((b, i) => (
                    <div
                      key={i}
                      onClick={() => setPrice(b.price.toFixed(3))}
                      className="flex justify-between items-center relative py-0.5 px-1 rounded cursor-pointer hover:bg-green-500/10"
                      title={`Click to set limit price to ${fmtPrice(b.price)}`}
                    >
                      <div
                        className="absolute left-0 top-0 bottom-0 bg-green-500/20 rounded-l transition-all duration-200"
                        style={{ width: `${(b.total / maxTotal) * 100}%` }}
                      />
                      <span className="mono text-green-400 z-10 font-semibold">{fmtPrice(b.price)}</span>
                      <span className="mono text-[#7e8aaa] z-10">{b.size.toFixed(1)} ({b.total.toFixed(0)})</span>
                    </div>
                  ))
                ) : (
                  <div className="text-[#3e4560] text-center py-4">No active bids</div>
                )}
              </div>
            </div>

            {/* Asks */}
            <div className="bg-[#0e1015] p-2.5 rounded border border-[#1f2335]">
              <div className="text-[10px] font-bold uppercase text-red-400 mb-2 flex justify-between">
                <span>Cumulative</span>
                <span>Asks (Sell Orders)</span>
              </div>
              <div className="space-y-1">
                {data?.asks && data.asks.length > 0 ? (
                  data.asks.map((a, i) => (
                    <div
                      key={i}
                      onClick={() => setPrice(a.price.toFixed(3))}
                      className="flex justify-between items-center relative py-0.5 px-1 rounded cursor-pointer hover:bg-red-500/10"
                      title={`Click to set limit price to ${fmtPrice(a.price)}`}
                    >
                      <div
                        className="absolute right-0 top-0 bottom-0 bg-red-500/20 rounded-r transition-all duration-200"
                        style={{ width: `${(a.total / maxTotal) * 100}%` }}
                      />
                      <span className="mono text-[#7e8aaa] z-10">{a.size.toFixed(1)} ({a.total.toFixed(0)})</span>
                      <span className="mono text-red-400 z-10 font-semibold">{fmtPrice(a.price)}</span>
                    </div>
                  ))
                ) : (
                  <div className="text-[#3e4560] text-center py-4">No active asks</div>
                )}
              </div>
            </div>
          </div>

          {/* Quick Trade Form */}
          <div className="bg-[#0e1015] p-3 rounded border border-[#1f2335] space-y-3">
            <div className="text-[11px] font-semibold uppercase text-[#7e8aaa] flex justify-between items-center">
              <span>Manual Paper Trade Execution</span>
              <div className="flex gap-1" role="group" aria-label="Trade direction">
                <button
                  type="button"
                  onClick={() => setSide('BUY')}
                  className={`btn btn-xs ${side === 'BUY' ? 'btn-success' : 'btn-ghost'}`}
                  aria-pressed={side === 'BUY'}
                >
                  Buy YES
                </button>
                <button
                  type="button"
                  onClick={() => setSide('SELL')}
                  className={`btn btn-xs ${side === 'SELL' ? 'btn-danger' : 'btn-ghost'}`}
                  aria-pressed={side === 'SELL'}
                >
                  Sell YES
                </button>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3 text-xs">
              <div>
                <label className="form-label text-[10px]">Limit Price ($0.01 – $0.99)</label>
                <input
                  type="number"
                  step="0.005"
                  min="0.01"
                  max="0.99"
                  value={price}
                  onChange={(e) => setPrice(e.target.value)}
                  className="input input-sm mono"
                  aria-label="Limit price"
                />
              </div>
              <div>
                <label className="form-label text-[10px]">Order Size (USDC) — Max $3/market</label>
                <input
                  type="number"
                  step="0.5"
                  min="0.5"
                  max="3"
                  value={sizeUsdc}
                  onChange={(e) => setSizeUsdc(e.target.value)}
                  className="input input-sm mono"
                  aria-label="Order size in USDC"
                />
              </div>
            </div>

            <div className="flex justify-between items-center pt-1">
              <span className="text-[11px] text-[#7e8aaa] mono">
                Est. Shares: {price && sizeUsdc ? (parseFloat(sizeUsdc) / Math.max(parseFloat(price), 0.01)).toFixed(1) : '—'}
              </span>
              <button
                onClick={handleTrade}
                disabled={loading}
                className={`btn btn-sm ${side === 'BUY' ? 'btn-success' : 'btn-danger'}`}
              >
                {loading ? 'Submitting…' : `Place ${side} Order`}
              </button>
            </div>

            {feedback && (
              <div className={`text-[11px] p-2 rounded text-center ${
                feedback.ok ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
              }`} role="status">
                {feedback.ok ? '✅ ' : '⚠️ '}{feedback.text}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
