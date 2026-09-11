// components/DepthChartModal.tsx — Interactive Order Book Depth & Quick Trade Modal
'use client'

import { useEffect, useState, useRef } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtPrice } from '@/lib/design-tokens'
import MarketDepthChart from './charts/MarketDepthChart'

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

// S2: payload shape returned by GET /api/ai/predict/{token_id} (see api/server.py)
interface MlPred {
  p_yes: number
  confidence: number
  market_mid: number | null
  best_bid?: number | null
  best_ask?: number | null
  spread?: number | null
  edge: number | null
  edge_bps: number | null
  recommended_action: 'BUY' | 'SELL' | 'HOLD'
  action_reason?: string
  thresholds?: {
    min_edge_cents: number
    min_confidence: number
  }
  model_status?: {
    model_ready?: boolean
    model_version?: string | number | null
    brier_score?: number | null
    roc_auc?: number | null
    ece?: number | null
    n_online_updates?: number
  }
  timestamp?: number
}

interface Props {
  tokenId: string | null
  slug: string | null
  onClose: () => void
  onOrderPlaced?: () => void
}

export default function DepthChartModal({ tokenId, slug, onClose, onOrderPlaced }: Props) {
  const [data, setData] = useState<DepthData | null>(null)
  // S2: ML ensemble directional view (polled every 5s from /api/ai/predict/{token_id})
  const [mlPred, setMlPred] = useState<MlPred | null>(null)
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
      } catch (e) {
        // W22-1 — log the failure so it shows up in the dev console. The
        // panel keeps the last known depth data (or shows placeholders
        // until the next 2s poll) — same UX as before, but now we
        // surface the underlying error for debugging.
        console.error('[DepthChartModal] Failed to fetch order book depth:', e)
      }
    }
    fetchDepth()
    const timer = setInterval(fetchDepth, 2000)
    return () => clearInterval(timer)
  }, [tokenId])

  // S2: poll the ML ensemble prediction endpoint every 5s. `apiFetch`
  // auto-injects the XTransformPort gateway header (see @/lib/api), so we
  // can pass a clean `/api/...` path. We clear stale predictions on token
  // switch so a new modal never shows the previous token's edge.
  useEffect(() => {
    if (!tokenId) return
    setMlPred(null)
    const fetchMlPred = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await apiFetch(`${apiUrl}/api/ai/predict/${tokenId}`)
        if (res.ok) {
          const json: MlPred = await res.json()
          setMlPred(json)
        }
      } catch (e) {
        // network/HTTP errors are logged for debugging — the panel will keep
        // the last known value or show placeholders until the next poll.
        // W22-1 — previously silent; now logged via console.error.
        console.error('[DepthChartModal] Failed to fetch ML prediction:', e)
      }
    }
    fetchMlPred()
    const mlTimer = setInterval(fetchMlPred, 5000)
    return () => clearInterval(mlTimer)
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
    } catch (e) {
      console.error('[DepthChartModal] Trade submission network error:', e)
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
          {/* W15-1 — MarketDepthChart: a Recharts-powered cumulative-depth
              visualization that complements the textual bid/ask ladder
              below. The chart shows the stepped areas for bids (green,
              left of mid) and asks (red, right of mid), with a dashed
              mid-price reference line and a top-right spread chip.
              Falls back gracefully to an empty-state message when no
              depth data has loaded yet (first 2s fetch window). */}
          <div className="bg-[#0e1015] p-2.5 rounded border border-[#1f2335]">
            <div className="text-[10px] font-bold uppercase text-[#7e8aaa] mb-1.5 flex items-center justify-between">
              <span>📊 Cumulative Market Depth</span>
              <span className="text-[9px] mono text-[#3e4560]">
                bids {data?.bids?.length ?? 0} · asks {data?.asks?.length ?? 0}
              </span>
            </div>
            <MarketDepthChart
              bids={data?.bids ?? []}
              asks={data?.asks ?? []}
              mid={data?.mid ?? null}
              bestBid={data?.best_bid ?? null}
              bestAsk={data?.best_ask ?? null}
              spread={data?.spread ?? null}
              height={220}
            />
          </div>

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

          {/* S2: ML Edge Panel — model P(YES) vs market mid, polled @5s */}
          <div className="bg-[#0e1015] p-3 rounded border border-[#1f2335] space-y-2.5">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-[11px] font-bold uppercase text-[#7e8aaa]">
                  🧠 ML Edge
                </span>
                <span
                  className={`badge text-[9px] ${
                    mlPred?.model_status?.model_ready ? 'badge-green' : 'badge-amber'
                  }`}
                  title={
                    mlPred?.model_status?.model_ready
                      ? `model v${mlPred.model_status.model_version ?? '?'} · brier ${mlPred.model_status.brier_score ?? '—'} · AUC ${mlPred.model_status.roc_auc ?? '—'}`
                      : 'ensemble not yet trained — predictions are uncalibrated'
                  }
                >
                  {mlPred?.model_status?.model_ready ? 'Model Ready' : 'Booting'}
                </span>
              </div>
              <span className="text-[9.5px] text-[#7e8aaa] mono">
                {mlPred
                  ? `updated ${new Date((mlPred.timestamp ?? 0) * 1000).toLocaleTimeString()}`
                  : 'polling @5s'}
              </span>
            </div>

            <div className="grid grid-cols-4 gap-2 text-[11px]">
              {/* Model P(YES) */}
              <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1.5">
                <div className="text-[9px] uppercase text-[#7e8aaa]">Model P(YES)</div>
                <div className="mono font-semibold text-[#dde1ed] leading-tight">
                  {mlPred ? `${(mlPred.p_yes * 100).toFixed(1)}%` : '—'}
                </div>
                <div className="text-[9px] text-[#7e8aaa] mono">
                  conf {mlPred ? `${(mlPred.confidence * 100).toFixed(0)}%` : '—'}
                </div>
              </div>

              {/* Market Mid */}
              <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1.5">
                <div className="text-[9px] uppercase text-[#7e8aaa]">Market Mid</div>
                <div className="mono font-semibold text-[#dde1ed] leading-tight">
                  {mlPred?.market_mid != null
                    ? `${(mlPred.market_mid * 100).toFixed(1)}¢`
                    : '—'}
                </div>
                <div className="text-[9px] text-[#7e8aaa] mono">
                  {mlPred?.market_mid != null
                    ? `$${mlPred.market_mid.toFixed(3)}`
                    : 'no book'}
                </div>
              </div>

              {/* Edge (model P(YES) − market mid) */}
              <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1.5">
                <div className="text-[9px] uppercase text-[#7e8aaa]">Edge</div>
                <div
                  className={`mono font-semibold leading-tight ${
                    mlPred?.edge == null
                      ? 'text-[#3e4560]'
                      : mlPred.edge > 0
                        ? 'text-green-400'
                        : mlPred.edge < 0
                          ? 'text-red-400'
                          : 'text-[#dde1ed]'
                  }`}
                >
                  {mlPred?.edge == null
                    ? '—'
                    : `${mlPred.edge >= 0 ? '+' : ''}${(mlPred.edge * 100).toFixed(2)}%`}
                </div>
                <div className="text-[9px] text-[#7e8aaa] mono">
                  {mlPred?.edge_bps != null
                    ? `${mlPred.edge_bps >= 0 ? '+' : ''}${mlPred.edge_bps.toFixed(0)} bps`
                    : '— bps'}
                </div>
              </div>

              {/* Recommended Action badge */}
              <div className="bg-[#13161e] border border-[#1f2335] rounded px-2 py-1.5">
                <div className="text-[9px] uppercase text-[#7e8aaa]">Action</div>
                {(() => {
                  const action = mlPred?.recommended_action
                  const badgeCls =
                    action === 'BUY'
                      ? 'badge-green'
                      : action === 'SELL'
                        ? 'badge-red'
                        : action === 'HOLD'
                          ? 'badge-amber'
                          : 'badge-dim'
                  return (
                    <span
                      className={`badge ${badgeCls} text-[10px] font-bold mt-0.5 inline-block`}
                    >
                      {action ?? '—'}
                    </span>
                  )
                })()}
                <div className="text-[9px] text-[#7e8aaa] mono mt-0.5">±2ct gate</div>
              </div>
            </div>

            {mlPred?.action_reason && (
              <div className="text-[10px] text-[#7e8aaa] mono leading-snug border-t border-[#1f2335] pt-1.5">
                <span className="text-[#3e4560]">reason:</span> {mlPred.action_reason}
              </div>
            )}
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
                <label className="form-label text-[10px]">Order Size ($ USDC · Max $3)</label>
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
            </div>

            <div className="flex flex-wrap justify-between items-center pt-1 gap-2">
              {(() => {
                const p = parseFloat(price) || 0.5
                const s = parseFloat(sizeUsdc) || 1.5
                const estShares = p > 0 ? s / p : 0
                const estPayout = estShares * 1.0
                const estProfit = estPayout - s
                const returnPct = s > 0 ? (estProfit / s) * 100 : 0

                return (
                  <div className="flex items-center gap-2 bg-[#13161e] border border-[#1f2335] px-2 py-0.5 rounded text-[10.5px]">
                    <span className="text-[#7e8aaa]">Est. Shares: <strong className="text-[#dde1ed] mono">{estShares.toFixed(1)}</strong></span>
                    <span className="text-[#3e4560]">|</span>
                    <span className="text-[#7e8aaa]">Payout: <strong className="text-green-400 mono">${estPayout.toFixed(2)}</strong> ({returnPct >= 0 ? `+${returnPct.toFixed(0)}%` : `${returnPct.toFixed(0)}%`})</span>
                  </div>
                )
              })()}
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
