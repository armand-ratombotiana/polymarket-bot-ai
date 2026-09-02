// components/MarketChartModal.tsx — Interactive Candlestick & Historical Price Modal
'use client'

import { useEffect, useState, useRef } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

interface Bar {
  timestamp: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

interface Props {
  tokenId: string
  slug: string
  onClose: () => void
  onOrderPlaced?: () => void
}

export default function MarketChartModal({ tokenId, slug, onClose, onOrderPlaced }: Props) {
  const [resolution, setResolution] = useState<'1m' | '5m' | '1h'>('5m')
  const [bars, setBars] = useState<Bar[]>([])
  const [loading, setLoading] = useState(true)
  const [showEma, setShowEma] = useState(true)

  // Fast Trade state ($100 operating capital — $3 per-market cap)
  const [price, setPrice] = useState('0.50')
  const [sizeUsdc, setSizeUsdc] = useState('1.5')
  const [placing, setPlacing] = useState(false)
  const [tradeMsg, setTradeMsg] = useState<{ ok: boolean; text: string } | null>(null)
  
  const modalRef = useRef<HTMLDivElement>(null)

  const title = formatMarketTitle(slug)
  const cat = getCategoryBadge('', slug)

  // Escape key handler
  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [onClose])

  useEffect(() => {
    const fetchOhlcv = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/history/ohlcv/${tokenId}?resolution=${resolution}&count=40`)
        if (res.ok) {
          const json = await res.json()
          setBars(json.bars || [])
          if (json.bars && json.bars.length > 0) {
            setPrice(String(json.bars[json.bars.length - 1].close.toFixed(3)))
          }
        }
      } catch {}
      setLoading(false)
    }
    fetchOhlcv()
  }, [tokenId, resolution])

  const handlePlaceOrder = async (side: 'BUY' | 'SELL') => {
    setPlacing(true)
    setTradeMsg(null)
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
        setTradeMsg({ ok: true, text: body?.detail || `Order placed: ${side} $${sizeUsdc} @ ${price}` })
        if (onOrderPlaced) onOrderPlaced()
      } else {
        setTradeMsg({ ok: false, text: body?.detail || `Risk gate rejected: HTTP ${res.status}` })
      }
    } catch {
      setTradeMsg({ ok: false, text: 'Order submission failed — check backend connectivity' })
    }
    setPlacing(false)
  }

  const minP = bars.length > 0 ? Math.min(...bars.map((b) => b.low)) * 0.96 : 0.01
  const maxP = bars.length > 0 ? Math.max(...bars.map((b) => b.high)) * 1.04 : 0.99
  const rangeP = maxP - minP || 0.01

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
        aria-labelledby="chart-modal-title"
      >
        {/* Header */}
        <div className="modal-header">
          <div className="flex items-center gap-3">
            <span className="text-xl" aria-hidden="true">{cat.icon}</span>
            <div>
              <h3 id="chart-modal-title" className="text-sm font-bold text-[#dde1ed] truncate max-w-md">{title}</h3>
              <div className="flex items-center gap-2 mt-0.5">
                <span className={`text-[9px] px-1.5 py-0.2 rounded border ${cat.color}`}>{cat.label}</span>
                <span className="text-[10px] text-[#7e8aaa] mono">{tokenId.slice(0, 18)}…</span>
                <span className="badge badge-amber text-[9px]">Paper Mode</span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Timeframe selector */}
            <div className="flex bg-[#0e1015] p-0.5 rounded border border-[#1f2335]">
              {(['1m', '5m', '1h'] as Array<'1m' | '5m' | '1h'>).map((r) => (
                <button
                  key={r}
                  onClick={() => setResolution(r)}
                  className={`px-2.5 py-0.5 rounded text-[11px] font-bold uppercase transition-all ${
                    resolution === r ? 'bg-blue-500 text-black' : 'text-[#7e8aaa] hover:text-white'
                  }`}
                  aria-label={`Timeframe ${r}`}
                >
                  {r}
                </button>
              ))}
            </div>

            <button
              onClick={() => setShowEma(!showEma)}
              className={`px-2 py-0.5 rounded text-[10px] mono border transition-all ${
                showEma ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/40' : 'bg-[#0e1015] text-[#7e8aaa] border-[#1f2335]'
              }`}
              aria-label="Toggle EMA 21 indicator"
            >
              EMA(21)
            </button>

            <button
              onClick={onClose}
              className="modal-close"
              aria-label="Close market chart modal"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Synthetic Notice Banner */}
        <div className="banner-experimental text-[11px] mx-4 mt-3 py-1.5 px-3" role="note">
          <span aria-hidden="true">ℹ️</span>
          <span>
            <strong>SYNTHETIC DATA:</strong> Historical candlestick series is simulated by <code>/api/history/ohlcv</code>. Real Polymarket tick history is not yet persisted.
          </span>
        </div>

        {/* Chart Canvas */}
        <div className="p-4 flex-1 min-h-[240px] flex flex-col justify-center">
          {loading || bars.length === 0 ? (
            <div className="flex flex-col items-center justify-center h-48 text-xs text-[#7e8aaa]">
              <span className="spinner mb-2" aria-hidden="true" />
              Rendering price timeline…
            </div>
          ) : (
            <div className="w-full h-52 relative bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <svg viewBox="0 0 440 180" className="w-full h-full" role="img" aria-label={`Price candlestick chart for ${title}`}>
                {/* Horizontal grid lines */}
                {[0.25, 0.5, 0.75].map((pct, i) => (
                  <line
                    key={i}
                    x1="10"
                    y1={10 + pct * 160}
                    x2="430"
                    y2={10 + pct * 160}
                    stroke="#1c1f2e"
                    strokeDasharray="2 2"
                  />
                ))}

                {/* Candlestick Bars */}
                {bars.map((b, i) => {
                  const x = 15 + i * 10.2
                  const yOpen = 170 - ((b.open - minP) / rangeP) * 160
                  const yClose = 170 - ((b.close - minP) / rangeP) * 160
                  const yHigh = 170 - ((b.high - minP) / rangeP) * 160
                  const yLow = 170 - ((b.low - minP) / rangeP) * 160
                  const isGreen = b.close >= b.open
                  const color = isGreen ? '#22c55e' : '#ef4444'

                  return (
                    <g key={i}>
                      {/* High-Low Wick */}
                      <line x1={x} y1={yHigh} x2={x} y2={yLow} stroke={color} strokeWidth="1.2" />
                      {/* Open-Close Body */}
                      <rect
                        x={x - 3.2}
                        y={Math.min(yOpen, yClose)}
                        width="6.4"
                        height={Math.max(Math.abs(yClose - yOpen), 2)}
                        fill={color}
                        rx="1"
                      />
                    </g>
                  )
                })}

                {/* Real EMA(21) overlay */}
                {showEma && bars.length > 21 && (
                  <path
                    d={(() => {
                      const k = 2 / (21 + 1)
                      let ema = bars[0].close
                      const pts: Array<{ x: number; y: number }> = []
                      bars.forEach((b, i) => {
                        ema = b.close * k + ema * (1 - k)
                        if (i >= 20) {
                          const x = 15 + i * 10.2
                          const y = 170 - ((ema - minP) / rangeP) * 160
                          pts.push({ x, y })
                        }
                      })
                      return pts.reduce(
                        (acc, pt, i) => (i === 0 ? `M ${pt.x},${pt.y}` : `${acc} L ${pt.x},${pt.y}`),
                        ''
                      )
                    })()}
                    fill="none"
                    stroke="#38bdf8"
                    strokeWidth="1.8"
                  />
                )}
              </svg>
            </div>
          )}
        </div>

        {/* Quick Trade Footer Pad */}
        <div className="modal-footer bg-[#111420] flex-wrap justify-between gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <div>
              <label className="form-label mb-0.5 text-[10px]">Price ($)</label>
              <input
                type="number"
                step="0.001"
                min="0.01"
                max="0.99"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                className="input input-sm w-24 mono"
                aria-label="Order limit price"
              />
            </div>
            <div>
              <label className="form-label mb-0.5 text-[10px]">Size ($ USDC · Max $3)</label>
              <div className="flex items-center gap-1.5">
                <input
                  type="number"
                  step="0.5"
                  min="0.5"
                  max="3"
                  value={sizeUsdc}
                  onChange={(e) => setSizeUsdc(e.target.value)}
                  className="input input-sm w-20 mono"
                  aria-label="Order size in USDC"
                />
                <div className="flex items-center gap-1">
                  {['0.5', '1.0', '1.5', '3.0'].map((preset) => (
                    <button
                      key={preset}
                      type="button"
                      onClick={() => setSizeUsdc(preset)}
                      className={`px-1.5 py-0.5 rounded text-[10px] mono font-bold border transition-all ${
                        sizeUsdc === preset
                          ? 'bg-blue-500/20 text-cyan-300 border-blue-500/50'
                          : 'bg-[#0e1015] text-[#7e8aaa] border-[#1f2335] hover:text-white'
                      }`}
                    >
                      ${preset}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            {/* Payoff Calculation */}
            {(() => {
              const p = parseFloat(price) || 0.5
              const s = parseFloat(sizeUsdc) || 1.5
              const estShares = p > 0 ? s / p : 0
              const estPayout = estShares * 1.0
              const estProfit = estPayout - s
              const returnPct = s > 0 ? (estProfit / s) * 100 : 0

              return (
                <div className="flex items-center gap-2 bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded text-[10.5px]">
                  <span className="text-[#7e8aaa]">Est. Shares: <strong className="text-[#dde1ed] mono">{estShares.toFixed(1)}</strong></span>
                  <span className="text-[#3e4560]">|</span>
                  <span className="text-[#7e8aaa]">Payout: <strong className="text-green-400 mono">${estPayout.toFixed(2)}</strong> ({returnPct >= 0 ? `+${returnPct.toFixed(0)}%` : `${returnPct.toFixed(0)}%`})</span>
                </div>
              )
            })()}
          </div>

          {tradeMsg && (
            <div className={`text-[11px] px-3 py-1 rounded w-full sm:w-auto ${
              tradeMsg.ok ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
            }`} role="status">
              {tradeMsg.ok ? '✅ ' : '⚠️ '}{tradeMsg.text}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={() => handlePlaceOrder('BUY')}
              disabled={placing}
              className="btn btn-success btn-sm"
              aria-label={`Buy YES outcome for ${sizeUsdc} USDC`}
            >
              {placing ? 'Placing…' : 'Buy YES'}
            </button>
            <button
              onClick={() => handlePlaceOrder('SELL')}
              disabled={placing}
              className="btn btn-danger btn-sm"
              aria-label={`Sell YES outcome for ${sizeUsdc} USDC`}
            >
              {placing ? 'Placing…' : 'Sell YES'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
