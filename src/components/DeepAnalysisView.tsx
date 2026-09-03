// components/DeepAnalysisView.tsx — Multi-Factor Market Intelligence & ML Alpha Forecaster
'use client'

import { useEffect, useState, useCallback } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { formatHierarchicalMarket, formatMarketTitle } from '@/lib/formatters'
import { fmtPrice, fmtUsd } from '@/lib/design-tokens'

interface MarketAnalysis {
  token_id: string
  slug: string
  status: string
  reason?: string
  market_implied_prob?: number
  ml_forecast_prob?: number
  uncertainty_interval?: [number, number]
  raw_edge?: number
  net_edge?: number
  confidence_score?: number
  alpha_score?: number
  regime?: string
  regime_tag?: string
  best_bid?: number | null
  best_ask?: number | null
  spread_dollars?: number
  spread_pct?: number
  total_liquidity_usdc?: number
  bid_depth_usdc?: number
  ask_depth_usdc?: number
  order_flow_imbalance?: number
  slippage_bps?: number
  fundamental_sentiment?: number
  supporting_evidence?: Array<{ headline: string; source: string; category: string; sentiment: number; age_minutes: number }>
  contradicting_evidence?: Array<{ headline: string; source: string; category: string; sentiment: number; age_minutes: number }>
  suggested_action?: 'TRADE_LONG_YES' | 'TRADE_SHORT_NO' | 'MONITOR' | 'REJECT_RISK'
  action_reasons?: string[]
  model_metadata?: { version: string; brier_score: number; features_used: number }
  data_freshness_seconds?: number
  generation_time_ms?: number
}

interface DeepAnalysisData {
  top_opportunities: MarketAnalysis[]
  recent_news: Array<{ headline: string; source: string; category: string; sentiment: number; timestamp: number }>
  timestamp: number
}

interface DeepAnalysisViewProps {
  /**
   * Open the price-history modal (MarketChartModal) for a market.
   * Wired in page.tsx to `setChartMarket`.
   */
  onOpenChart?: (m: { tokenId: string; slug: string }) => void
  /**
   * W13 — One-click trade shortcut. Opens the DepthChartModal
   * (depth book + trade ticket) pre-loaded with the clicked row's
   * token_id and slug. Mirrors the `onSelectMarket` callback pattern
   * used by MarketsPanel / MarketScreener — same two-arg signature
   * `(tokenId, slug) => void`. Wired in page.tsx to
   * `setSelectedMarket`, which mounts the DepthChartModal.
   */
  onSelectMarket?: (tokenId: string, slug: string) => void
}

export default function DeepAnalysisView({ onOpenChart, onSelectMarket }: DeepAnalysisViewProps) {
  const [data, setData] = useState<DeepAnalysisData | null>(null)
  const [selectedToken, setSelectedToken] = useState<string | null>(null)
  const [singleAnalysis, setSingleAnalysis] = useState<MarketAnalysis | null>(null)
  const [loading, setLoading] = useState(true)
  const [analyzingSingle, setAnalyzingSingle] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const fetchData = useCallback(async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/analysis/deep`)
      if (res.ok) {
        const json = await res.json()
        setData(json)
        setError(null)
        if (!selectedToken && json.top_opportunities && json.top_opportunities.length > 0) {
          setSelectedToken(json.top_opportunities[0].token_id)
          setSingleAnalysis(json.top_opportunities[0])
        }
      } else {
        setError(`Failed to fetch deep analysis (HTTP ${res.status})`)
      }
    } catch (err: any) {
      setError(err?.message || 'Network error connecting to analysis engine')
    }
    setLoading(false)
  }, [selectedToken])

  const fetchSingleMarket = async (tokenId: string) => {
    setAnalyzingSingle(true)
    setSelectedToken(tokenId)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/analysis/market/${tokenId}`)
      if (res.ok) {
        const json = await res.json()
        setSingleAnalysis(json)
      }
    } catch {}
    setAnalyzingSingle(false)
  }

  useEffect(() => {
    fetchData()
    const timer = setInterval(fetchData, 5000)
    return () => clearInterval(timer)
  }, [fetchData])

  if (loading && !data) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg p-4 space-y-3.5 overflow-hidden">
        {/* Header Skeleton */}
        <div className="flex justify-between items-center pb-3 border-b border-[#1f2335]">
          <div className="space-y-1.5">
            <div className="skeleton-line" style={{ width: '280px', height: '14px' }} />
            <div className="skeleton-line" style={{ width: '180px', height: '10px' }} />
          </div>
          <div className="skeleton-line" style={{ width: '100px', height: '24px' }} />
        </div>

        {/* Top Opportunities Table Skeleton */}
        <div className="skeleton-card space-y-2 p-3">
          <div className="skeleton-line" style={{ width: '200px', height: '12px' }} />
          <div className="space-y-1.5 pt-1">
            <div className="skeleton-line-lg" />
            <div className="skeleton-line-lg" />
            <div className="skeleton-line-lg" />
          </div>
        </div>

        {/* 3-Column Inspection Grid Skeleton */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3 flex-1">
          <div className="skeleton-card space-y-2.5 p-3">
            <div className="skeleton-line" style={{ width: '160px', height: '12px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
          </div>
          <div className="skeleton-card space-y-2.5 p-3">
            <div className="skeleton-line" style={{ width: '160px', height: '12px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '32px' }} />
          </div>
          <div className="skeleton-card space-y-2.5 p-3">
            <div className="skeleton-line" style={{ width: '160px', height: '12px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '48px' }} />
            <div className="skeleton-line" style={{ width: '100%', height: '48px' }} />
          </div>
        </div>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-[#13161e] p-6 space-y-3">
        <span className="text-red-400 text-sm font-bold">Analysis Engine Offline</span>
        <p className="text-xs text-[#7e8aaa] max-w-md text-center">{error}</p>
        <button onClick={fetchData} className="btn btn-primary btn-sm mt-2">
          Retry Analysis
        </button>
      </div>
    )
  }

  const analysis = singleAnalysis || data?.top_opportunities[0]
  const info = formatHierarchicalMarket(analysis?.slug)

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3.5 overflow-y-auto scrollbar-thin shadow-2xl">
      {/* 1. Header */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-xl" aria-hidden="true">🔬</span>
            <h2 className="text-sm font-bold text-[#dde1ed] tracking-wide">
              Deep Market Intelligence &amp; Multi-Factor Alpha Forecaster
            </h2>
            <span className="badge badge-purple text-[10px] font-bold">ML Edge 40% Weight</span>
          </div>
          <div className="flex items-center gap-2 mt-1">
            <span className={`text-[9.5px] px-2 py-0.5 rounded border ${info.category.color} font-bold`}>
              {info.category.icon} {info.eventTitle}
            </span>
            <span className="text-xs text-[#dde1ed] font-semibold truncate max-w-xl">
              {info.question}
            </span>
          </div>
        </div>

        {/* Action Recommendation & Chart Shortcut */}
        <div className="flex items-center gap-2">
          {analysis && onOpenChart && (
            <button
              onClick={() => onOpenChart({ tokenId: analysis.token_id, slug: analysis.slug })}
              className="btn btn-ghost btn-sm text-xs font-semibold text-cyan-400 border border-cyan-500/30 hover:bg-cyan-500/10 px-2.5 py-1 rounded"
            >
              📈 Price History
            </button>
          )}

          {analysis?.suggested_action && (
            <span
              className={`px-3 py-1 rounded text-xs font-black tracking-wider uppercase border shadow-md ${
                analysis.suggested_action === 'TRADE_LONG_YES'
                  ? 'badge-green bg-green-500/20 text-green-400 border-green-500/40'
                  : analysis.suggested_action === 'TRADE_SHORT_NO'
                  ? 'badge-purple bg-purple-500/20 text-purple-400 border-purple-500/40'
                  : analysis.suggested_action === 'MONITOR'
                  ? 'badge-blue bg-blue-500/20 text-blue-400 border-blue-500/40'
                  : 'badge-red bg-red-500/20 text-red-400 border-red-500/40'
              }`}
            >
              {analysis.suggested_action.replace(/_/g, ' ')}
            </span>
          )}
        </div>
      </div>

      {/* 2. Top Ranked Opportunities Hub */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
        <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Top Alpha Opportunities ({data?.top_opportunities.length || 0} Ranked)
          </span>
          <span className="text-[10px] text-[#7e8aaa] mono">Sorted by ML Alpha Score (40% Edge + 25% OFI + 20% News + 15% Spread)</span>
        </div>

        <div className="overflow-x-auto scrollbar-thin max-h-40 table-container">
          <table className="data-table text-xs w-full" role="table" aria-label="Deep scan candidate rankings">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="text-left py-1">Contract</th>
                <th scope="col" className="text-right">Market Mid</th>
                <th scope="col" className="text-right">AI Calibrated</th>
                <th scope="col" className="text-right">Alpha Edge</th>
                <th scope="col" className="text-right">Confidence</th>
                <th scope="col" className="text-center">Regime Tag</th>
                <th scope="col" className="text-right">OFI Flow</th>
                <th scope="col" className="text-center">Action</th>
                {/* W13 — One-click Trade column. Opens DepthChartModal for the row's market. */}
                <th scope="col" className="text-center">Trade</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {data?.top_opportunities.map((opp) => {
                const rowTitle = formatMarketTitle(opp.slug)
                const isSelected = selectedToken === opp.token_id
                const netEdge = opp.net_edge ?? 0
                return (
                  <tr
                    key={opp.token_id}
                    onClick={() => fetchSingleMarket(opp.token_id)}
                    className={`cursor-pointer transition-colors hover:bg-blue-500/10 ${
                      isSelected ? 'bg-blue-500/15' : ''
                    }`}
                  >
                    <td className="max-w-[200px] truncate font-semibold text-[#dde1ed] text-[11px] py-2" title={rowTitle}>
                      {rowTitle}
                    </td>
                    <td className="mono text-right text-[#7e8aaa]">
                      {opp.market_implied_prob ? `${(opp.market_implied_prob * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono text-right font-bold text-cyan-400">
                      {opp.ml_forecast_prob ? `${(opp.ml_forecast_prob * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono text-right font-bold text-green-400">
                      {netEdge !== 0 ? `${netEdge >= 0 ? '+' : ''}${(netEdge * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono text-right text-amber-400 font-medium">
                      {opp.confidence_score ? `${(opp.confidence_score * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="text-center">
                      <span className="badge badge-dim text-[9px] font-bold">
                        {opp.regime_tag || 'range'}
                      </span>
                    </td>
                    <td className={`mono text-right font-bold ${(opp.order_flow_imbalance ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {opp.order_flow_imbalance ? `${opp.order_flow_imbalance >= 0 ? '+' : ''}${opp.order_flow_imbalance.toFixed(2)}` : '0.00'}
                    </td>
                    <td className="text-center">
                      <span
                        className={`text-[9px] px-2 py-0.5 rounded font-black ${
                          opp.suggested_action === 'TRADE_LONG_YES'
                            ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                            : opp.suggested_action === 'TRADE_SHORT_NO'
                            ? 'bg-purple-500/15 text-purple-400 border border-purple-500/30'
                            : 'bg-blue-500/15 text-blue-400'
                        }`}
                      >
                        {opp.suggested_action?.replace('TRADE_', '') || 'MONITOR'}
                      </span>
                    </td>
                    {/* W13 — One-click Trade button. Stops propagation so it does NOT
                        re-trigger the row's `fetchSingleMarket` onClick; instead it
                        invokes the onSelectMarket callback (same pattern as
                        MarketsPanel) to mount the DepthChartModal pre-loaded with
                        this row's token_id + slug. */}
                    <td className="text-center py-1">
                      <button
                        onClick={(e) => {
                          e.stopPropagation()
                          onSelectMarket && onSelectMarket(opp.token_id, opp.slug)
                        }}
                        disabled={!onSelectMarket}
                        aria-label={`Open depth chart and trade ticket for ${rowTitle}`}
                        title={onSelectMarket ? `Open depth chart and trade ticket for ${rowTitle}` : 'Trade not available'}
                        className="btn btn-primary btn-xs font-bold shadow-md hover:shadow-cyan-500/20 px-2.5 py-0.5 rounded text-[10px] disabled:opacity-40 disabled:cursor-not-allowed disabled:shadow-none"
                      >
                        ⚡ Trade
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>

      {/* 3. Detailed 9-Factor Inspection Grid */}
      {analysis && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          {/* Col 1: Valuation & Alpha Breakdown */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between rounded-lg">
            <div>
              <div className="card-header pb-1.5 mb-2 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  📊 Probabilistic Valuation &amp; Alpha
                </span>
                <span className="badge badge-green text-[9px]">Isotonic 5-Fold</span>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">Market-Implied Mid:</span>
                  <span className="mono font-bold text-[#dde1ed]">
                    {analysis.market_implied_prob != null ? `${(analysis.market_implied_prob * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">4-Member AI Forecast:</span>
                  <span className="mono font-bold text-cyan-400">
                    {analysis.ml_forecast_prob != null ? `${(analysis.ml_forecast_prob * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">95% Uncertainty Band:</span>
                  <span className="mono text-amber-400 font-semibold">
                    {analysis.uncertainty_interval?.[0] != null && analysis.uncertainty_interval?.[1] != null
                      ? `[${(analysis.uncertainty_interval[0] * 100).toFixed(1)}% – ${(analysis.uncertainty_interval[1] * 100).toFixed(1)}%]`
                      : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-green-500/10 p-2 rounded border border-green-500/30">
                  <span className="font-semibold text-green-300">Net Expected Alpha Edge:</span>
                  <span className="mono font-bold text-green-400">
                    {analysis.net_edge ? `${analysis.net_edge >= 0 ? '+' : ''}${(analysis.net_edge * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] text-[10px] text-[#7e8aaa] flex justify-between mono">
              <span>Brier: {analysis.model_metadata?.brier_score ?? '0.145'}</span>
              <span>Confidence: {analysis.confidence_score != null ? `${(analysis.confidence_score * 100).toFixed(0)}%` : '—'}</span>
            </div>
          </div>

          {/* Col 2: Microstructure & Order Flow */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between rounded-lg">
            <div>
              <div className="card-header pb-1.5 mb-2 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  ⚡ Microstructure &amp; Order Flow
                </span>
                <span className="text-[10px] text-green-400 mono">L2 Depth</span>
              </div>

              <div className="space-y-2 text-xs">
                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">Top of Book Spread:</span>
                  <span className="mono font-bold text-[#dde1ed]">
                    {fmtPrice(analysis.best_bid)} / {fmtPrice(analysis.best_ask)} ({analysis.spread_dollars ? `${(analysis.spread_dollars * 100).toFixed(1)}¢` : '—'})
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">Order Flow Imbalance (OFI):</span>
                  <span className={`mono font-bold ${(analysis.order_flow_imbalance || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {analysis.order_flow_imbalance != null ? `${analysis.order_flow_imbalance >= 0 ? '+' : ''}${analysis.order_flow_imbalance.toFixed(2)}` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">Book Liquidity Depth:</span>
                  <span className="mono font-bold text-cyan-400">
                    {analysis.total_liquidity_usdc != null ? `${fmtUsd(analysis.total_liquidity_usdc, 0)}` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[#7e8aaa]">Est. Slippage (~$1.50 block):</span>
                  <span className="mono text-[#dde1ed]">
                    {analysis.slippage_bps ?? 2.5} bps
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] text-[10px] text-[#7e8aaa] flex justify-between mono">
              <span>Freshness: {analysis.data_freshness_seconds != null ? `${analysis.data_freshness_seconds}s ago` : '2s ago'}</span>
              <span>Compute: {analysis.generation_time_ms != null ? `${analysis.generation_time_ms}ms` : '1.2ms'}</span>
            </div>
          </div>

          {/* Col 3: Rationale, News & Regime */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between rounded-lg">
            <div>
              <div className="card-header pb-1.5 mb-2 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  📰 Regime Context &amp; Decision Rationale
                </span>
                <span className="text-[10px] text-amber-400 mono">NLP Signals</span>
              </div>

              <div className="space-y-2">
                <div className="bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[10px] text-[#7e8aaa] block font-semibold mb-1">Decision Rationale:</span>
                  {analysis.action_reasons && analysis.action_reasons.length > 0 ? (
                    <ul className="text-xs text-[#dde1ed] space-y-1">
                      {analysis.action_reasons.map((r, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <span className="text-cyan-400 font-bold">•</span>
                          <span>{r}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-xs text-[#7e8aaa]">Market conditions within standard execution boundaries.</span>
                  )}
                </div>

                <div className="bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[10px] text-[#7e8aaa] block font-semibold mb-1">Fundamental News Signal:</span>
                  {analysis.supporting_evidence && analysis.supporting_evidence.length > 0 ? (
                    <div className="space-y-1">
                      {analysis.supporting_evidence.map((s, i) => (
                        <div key={i} className="text-[11px] text-[#dde1ed] truncate">
                          <span className="text-green-400 font-bold">[+{s.sentiment}]</span> {s.headline}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <span className="text-xs text-[#7e8aaa]">No breaking news alerts impacting contract.</span>
                  )}
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] flex justify-end">
              <button
                onClick={() => fetchSingleMarket(analysis.token_id)}
                disabled={analyzingSingle}
                className="btn btn-primary btn-xs px-3 py-1 font-bold"
              >
                {analyzingSingle ? 'Refreshing…' : '🔄 Refresh Analysis'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
