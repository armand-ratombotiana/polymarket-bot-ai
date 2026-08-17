// components/MarketChartModal.tsx — Interactive Candlestick & Historical Price Modal
'use client'

import { useEffect, useState } from 'react'
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

  const title = formatMarketTitle(slug)
  const cat = getCategoryBadge('', slug)

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

  useEffect(() => {
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
        setTradeMsg({ ok: true, text: body?.detail || 'Order placed' })
        if (onOrderPlaced) onOrderPlaced()
      } else {
        setTradeMsg({ ok: false, text: body?.detail || `Order failed (HTTP ${res.status})` })
      }
    } catch {
      setTradeMsg({ ok: false, text: 'Order request failed — check connection' })
    }
    setPlacing(false)
  }

  const minP = bars.length > 0 ? Math.min(...bars.map((b) => b.low)) * 0.96 : 0.01
  const maxP = bars.length > 0 ? Math.max(...bars.map((b) => b.high)) * 1.04 : 0.99
  const rangeP = maxP - minP || 0.01

  return (
    <div className="fixed inset-0 bg-black/75 flex items-center justify-center z-50 p-4 backdrop-blur-sm select-none">
      <div className="bg-[#111318] border border-[#252836] rounded-xl w-full max-w-3xl overflow-hidden shadow-2xl flex flex-col max-h-[90vh]">
        {/* Header */}
        <div className="px-5 py-3 border-b border-[#252836] flex justify-between items-center bg-[#161822]">
          <div className="flex items-center gap-3">
            <span className="text-xl">{cat.icon}</span>
            <div>
              <h3 className="text-sm font-bold text-[#e8eaf0] truncate max-w-md">{title}</h3>
              <div className="flex items-center gap-2 mt-0.5">
                <span className={`text-[9px] px-1.5 py-0.2 rounded border ${cat.color}`}>{cat.label}</span>
                <span className="text-[10px] text-[#4a5068] mono">{tokenId.slice(0, 18)}…</span>
              </div>
            </div>
          </div>

          <div className="flex items-center gap-2">
            {/* Timeframe selector */}
            <div className="flex bg-[#111318] p-0.5 rounded border border-[#252836]">
              {(['1m', '5m', '1h'] as Array<'1m' | '5m' | '1h'>).map((r) => (
                <button
                  key={r}
                  onClick={() => setResolution(r)}
                  className={`px-2.5 py-0.5 rounded text-[11px] font-bold uppercase transition-all ${
                    resolution === r ? 'bg-blue-500 text-black' : 'text-[#8b91a8] hover:text-white'
                  }`}
                >
                  {r}
                </button>
              ))}
            </div>

            <button
              onClick={() => setShowEma(!showEma)}
              className={`px-2 py-0.5 rounded text-[10px] mono border transition-all ${
                showEma ? 'bg-cyan-500/20 text-cyan-400 border-cyan-500/40' : 'bg-[#111318] text-[#8b91a8] border-[#252836]'
              }`}
            >
              EMA(8/21)
            </button>

            <button
              onClick={onClose}
              className="text-[#8b91a8] hover:text-white text-base px-2 py-0.5 rounded hover:bg-[#252836]"
            >
              ✕
            </button>
          </div>
        </div>

        {/* Chart Canvas */}
        <div className="p-5 flex-1 min-h-[260px] flex flex-col justify-center">
          {loading || bars.length === 0 ? (
            <div className="flex items-center justify-center h-48 text-xs text-[#8b91a8]">
              Rendering historical price timeline…
            </div>
          ) : (
            <div className="w-full h-56 relative bg-[#0e1015] p-3 rounded-lg border border-[#252836]">
              <svg viewBox="0 0 440 180" className="w-full h-full">
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
        <div className="p-4 bg-[#161822] border-t border-[#252836] flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div>
              <label className="text-[10px] text-[#8b91a8] block mb-0.5">Price ($)</label>
              <input
                type="number"
                step="0.001"
                min="0.01"
                max="0.99"
                value={price}
                onChange={(e) => setPrice(e.target.value)}
                className="w-24 bg-[#111318] border border-[#252836] rounded px-2.5 py-1 text-xs mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
              />
            </div>
            <div>
              <label className="text-[10px] text-[#8b91a8] block mb-0.5">Amount ($ USDC - Max $3/market)</label>
              <input
                type="number"
                step="0.5"
                min="0.5"
                max="3"
                value={sizeUsdc}
                onChange={(e) => setSizeUsdc(e.target.value)}
                className="w-24 bg-[#111318] border border-[#252836] rounded px-2.5 py-1 text-xs mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
              />
            </div>
          </div>

          {tradeMsg && (
            <div className={`text-[11px] px-3 py-1.5 rounded w-full sm:w-auto ${
              tradeMsg.ok ? 'bg-green-500/10 text-green-400' : 'bg-red-500/10 text-red-400'
            }`}>
              {tradeMsg.ok ? '✅ ' : '⚠ '}{tradeMsg.text}
            </div>
          )}

          <div className="flex items-center gap-2">
            <button
              onClick={() => handlePlaceOrder('BUY')}
              disabled={placing}
              className="btn btn-success px-5 py-2 text-xs font-bold"
            >
              {placing ? 'Placing…' : 'Buy YES'}
            </button>
            <button
              onClick={() => handlePlaceOrder('SELL')}
              disabled={placing}
              className="btn btn-danger px-5 py-2 text-xs font-bold"
            >
              {placing ? 'Placing…' : 'Sell YES / Buy NO'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
