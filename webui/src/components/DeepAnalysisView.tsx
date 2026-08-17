// components/DeepAnalysisView.tsx — Multi-Factor Market Intelligence & Analysis
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

export default function DeepAnalysisView() {
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
    const timer = setInterval(fetchData, 6000)
    return () => clearInterval(timer)
  }, [fetchData])

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-[#13161e] p-6 space-y-3">
        <span className="spinner mb-2" aria-hidden="true" />
        <span className="text-xs text-[#7e8aaa]">Synthesizing order books, NLP news feeds &amp; probability calibration…</span>
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
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* 1. Header */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">🔬</span>
            <h2 className="text-sm font-bold text-[#dde1ed]">
              Deep Market Intelligence &amp; Multi-Factor Forecaster
            </h2>
          </div>
          <div className="flex items-center gap-2 mt-0.5">
            <span className={`text-[9.5px] px-1.5 py-0.5 rounded border ${info.category.color} font-bold`}>
              {info.category.icon} {info.eventTitle}
            </span>
            <span className="text-xs text-[#7e8aaa] font-medium truncate max-w-xl">
              {info.question}
            </span>
          </div>
        </div>

        {/* Action Recommendation Badge */}
        {analysis?.suggested_action && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-[#7e8aaa]">Action:</span>
            <span
              className={`px-2.5 py-0.5 rounded text-xs font-black tracking-wider uppercase border ${
                analysis.suggested_action === 'TRADE_LONG_YES'
                  ? 'badge-green'
                  : analysis.suggested_action === 'TRADE_SHORT_NO'
                  ? 'badge-purple'
                  : analysis.suggested_action === 'MONITOR'
                  ? 'badge-blue'
                  : 'badge-red'
              }`}
            >
              {analysis.suggested_action.replace(/_/g, ' ')}
            </span>
          </div>
        )}
      </div>

      {/* 2. Top Opportunities Hub */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
        <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚡ Deep Scan Candidates ({data?.top_opportunities.length || 0} Ranked)
          </span>
          <span className="text-[10px] text-[#7e8aaa] mono">Click any row to load 9-factor report</span>
        </div>

        <div className="overflow-x-auto scrollbar-thin max-h-36 table-container">
          <table className="data-table text-xs" role="table" aria-label="Deep scan candidate rankings">
            <thead>
              <tr>
                <th scope="col">Contract</th>
                <th scope="col">Market Prob</th>
                <th scope="col">AI Forecast</th>
                <th scope="col">Net Alpha Edge</th>
                <th scope="col">Confidence</th>
                <th scope="col">Spread</th>
                <th scope="col">OFI Flow</th>
                <th scope="col" className="text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {data?.top_opportunities.map((opp) => {
                const rowTitle = formatMarketTitle(opp.slug)
                const isSelected = selectedToken === opp.token_id
                return (
                  <tr
                    key={opp.token_id}
                    onClick={() => fetchSingleMarket(opp.token_id)}
                    className={`cursor-pointer transition-colors ${
                      isSelected ? 'row-selected' : ''
                    }`}
                  >
                    <td className="max-w-[200px] truncate font-semibold text-[#dde1ed] text-[11px]" title={rowTitle}>
                      {rowTitle}
                    </td>
                    <td className="mono text-[#7e8aaa]">
                      {opp.market_implied_prob ? `${(opp.market_implied_prob * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono font-bold text-cyan-400">
                      {opp.ml_forecast_prob ? `${(opp.ml_forecast_prob * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono font-bold text-green-400">
                      {opp.net_edge ? `${opp.net_edge >= 0 ? '+' : ''}${(opp.net_edge * 100).toFixed(1)}%` : '—'}
                    </td>
                    <td className="mono text-amber-400 font-medium">
                      {opp.confidence_score ? `${(opp.confidence_score * 100).toFixed(0)}%` : '—'}
                    </td>
                    <td className="mono text-[#7e8aaa]">
                      {opp.spread_dollars ? `${(opp.spread_dollars * 100).toFixed(1)}¢` : '—'}
                    </td>
                    <td className={`mono font-bold ${(opp.order_flow_imbalance ?? 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                      {opp.order_flow_imbalance ? `${opp.order_flow_imbalance >= 0 ? '+' : ''}${opp.order_flow_imbalance}` : '0.00'}
                    </td>
                    <td className="text-right">
                      <span className={`text-[9px] px-1.5 py-0.5 rounded font-black ${
                        opp.suggested_action === 'TRADE_LONG_YES'
                          ? 'badge-green'
                          : opp.suggested_action === 'TRADE_SHORT_NO'
                          ? 'badge-blue'
                          : 'badge-dim'
                      }`}>
                        {opp.suggested_action?.replace('TRADE_', '') || 'MONITOR'}
                      </span>
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
          {/* Col 1: Valuation */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between">
            <div>
              <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  📊 Probabilistic Valuation
                </span>
                <span className="badge badge-dim text-[9px]">Isotonic Holdout</span>
              </div>

              <div className="space-y-2">
                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">Market-Implied Prob:</span>
                  <span className="mono text-xs font-bold text-[#dde1ed]">
                    {analysis.market_implied_prob != null ? `${(analysis.market_implied_prob * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">AI Forecast:</span>
                  <span className="mono text-xs font-bold text-cyan-400">
                    {analysis.ml_forecast_prob != null ? `${(analysis.ml_forecast_prob * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">95% Uncertainty:</span>
                  <span className="mono text-xs text-amber-400">
                    {analysis.uncertainty_interval?.[0] != null && analysis.uncertainty_interval?.[1] != null
                      ? `[${(analysis.uncertainty_interval[0] * 100).toFixed(1)}% – ${(analysis.uncertainty_interval[1] * 100).toFixed(1)}%]`
                      : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-green-500/10 p-2 rounded border border-green-500/30">
                  <span className="text-xs font-semibold text-green-300">Net Expected Alpha:</span>
                  <span className="mono text-xs font-bold text-green-400">
                    {analysis.net_edge ? `${analysis.net_edge >= 0 ? '+' : ''}${(analysis.net_edge * 100).toFixed(1)}%` : '—'}
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] text-[10px] text-[#7e8aaa] flex justify-between">
              <span>Brier: {analysis.model_metadata?.brier_score ?? '—'}</span>
              <span>Confidence: {analysis.confidence_score != null ? `${(analysis.confidence_score * 100).toFixed(0)}%` : '—'}</span>
            </div>
          </div>

          {/* Col 2: Microstructure */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between">
            <div>
              <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  ⚡ Microstructure &amp; Order Flow
                </span>
                <span className="text-[10px] text-green-400 mono">L2 Depth</span>
              </div>

              <div className="space-y-2">
                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">Top of Book Spread:</span>
                  <span className="mono text-xs font-bold text-[#dde1ed]">
                    {fmtPrice(analysis.best_bid)} / {fmtPrice(analysis.best_ask)} ({analysis.spread_dollars ? `${(analysis.spread_dollars * 100).toFixed(1)}¢` : '—'})
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">Order Flow Imbalance (OFI):</span>
                  <span className={`mono text-xs font-bold ${(analysis.order_flow_imbalance || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {analysis.order_flow_imbalance != null ? `${analysis.order_flow_imbalance >= 0 ? '+' : ''}${analysis.order_flow_imbalance}` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">Queue Depth Liquidity:</span>
                  <span className="mono text-xs font-bold text-cyan-400">
                    {analysis.total_liquidity_usdc != null ? `${fmtUsd(analysis.total_liquidity_usdc, 0)}` : '—'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-xs text-[#7e8aaa]">Est. Slippage (~$1.50 block):</span>
                  <span className="mono text-xs text-[#dde1ed]">
                    {analysis.slippage_bps ?? '—'} bps
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] text-[10px] text-[#7e8aaa] flex justify-between">
              <span>Freshness: {analysis.data_freshness_seconds != null ? `${analysis.data_freshness_seconds}s ago` : '—'}</span>
              <span>Compute: {analysis.generation_time_ms != null ? `${analysis.generation_time_ms}ms` : '—'}</span>
            </div>
          </div>

          {/* Col 3: Evidence & Rationale */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between">
            <div>
              <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  📰 Fundamental Evidence &amp; Reasons
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
                          <span className="text-blue-400 font-bold">•</span>
                          <span>{r}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-xs text-[#7e8aaa]">No risk warnings active.</span>
                  )}
                </div>

                <div className="bg-[#13161e] p-2 rounded border border-[#1f2335]">
                  <span className="text-[10px] text-[#7e8aaa] block font-semibold mb-1">Recent News Signal:</span>
                  {analysis.supporting_evidence && analysis.supporting_evidence.length > 0 ? (
                    <div className="space-y-1">
                      {analysis.supporting_evidence.map((s, i) => (
                        <div key={i} className="text-[11px] text-[#dde1ed] truncate">
                          <span className="text-green-400 font-bold">[+{s.sentiment}]</span> {s.headline}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <span className="text-xs text-[#7e8aaa]">No direct news signal matching token.</span>
                  )}
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#1f2335] flex justify-end">
              <button
                onClick={() => fetchSingleMarket(analysis.token_id)}
                disabled={analyzingSingle}
                className="btn btn-primary btn-xs"
              >
                {analyzingSingle ? 'Refreshing…' : '🔄 Refresh'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
