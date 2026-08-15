// components/DeepAnalysisView.tsx — Institutional Deep Market Analysis & Multi-Factor Intelligence
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

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

  const fetchData = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/analysis/deep`)
      if (res.ok) {
        const json = await res.json()
        setData(json)
        setError(null)
        if (!selectedToken && json.top_opportunities && json.top_opportunities.length > 0) {
          setSelectedToken(json.top_opportunities[0].token_id)
          setSingleAnalysis(json.top_opportunities[0])
        }
      } else {
        setError(`Failed to fetch deep analysis: HTTP ${res.status}`)
      }
    } catch (err: any) {
      setError(err?.message || 'Network error connecting to analysis engine')
    }
    setLoading(false)
  }

  const fetchSingleMarket = async (tokenId: string) => {
    setAnalyzingSingle(true)
    setSelectedToken(tokenId)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/analysis/market/${tokenId}`)
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
  }, [])

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-[#111318] p-6 space-y-3">
        <div className="w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full animate-spin" />
        <span className="text-xs text-[#8b91a8]">Synthesizing order books, NLP news feeds &amp; probability calibration…</span>
      </div>
    )
  }

  if (error && !data) {
    return (
      <div className="flex flex-col items-center justify-center h-full bg-[#111318] p-6 space-y-3">
        <span className="text-red-400 text-sm font-bold">Analysis Engine Offline</span>
        <p className="text-xs text-[#8b91a8] max-w-md text-center">{error}</p>
        <button onClick={fetchData} className="btn btn-primary text-xs px-4 py-1.5 mt-2">
          Retry Analysis
        </button>
      </div>
    )
  }

  const analysis = singleAnalysis || data?.top_opportunities[0]
  const title = formatMarketTitle(analysis?.slug)
  const cat = getCategoryBadge('', analysis?.slug)

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin select-none">
      {/* 1. Header & Executive Summary Bar */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#252836] gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🔬</span>
            <h2 className="text-base font-bold text-[#e8eaf0]">
              Deep Market Intelligence &amp; Multi-Factor Alpha Forecaster
            </h2>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Calibrated AI Probability vs Market Implied, 5-Level Depth OFI, Fundamental NLP Evidence &amp; Execution Feasibility
          </p>
        </div>

        {/* Global Action Recommendation Badge */}
        {analysis?.suggested_action && (
          <div className="flex items-center gap-2">
            <span className="text-xs text-[#8b91a8]">Decision:</span>
            <span
              className={`px-3 py-1 rounded-md text-xs font-black tracking-wider uppercase border shadow-md ${
                analysis.suggested_action === 'TRADE_LONG_YES'
                  ? 'bg-green-500/20 text-green-400 border-green-500/40'
                  : analysis.suggested_action === 'TRADE_SHORT_NO'
                  ? 'bg-purple-500/20 text-purple-400 border-purple-500/40'
                  : analysis.suggested_action === 'MONITOR'
                  ? 'bg-blue-500/20 text-blue-400 border-blue-500/40'
                  : 'bg-red-500/20 text-red-400 border-red-500/40'
              }`}
            >
              {analysis.suggested_action.replace(/_/g, ' ')}
            </span>
          </div>
        )}
      </div>

      {/* 2. Market Contract Selector & Top Opportunities Hub */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836]">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚡ Select Active Contract for Deep Intelligence Scan ({data?.top_opportunities.length || 0} Ranked)
          </span>
          <span className="text-[10px] text-[#8b91a8] mono">Click any row to load full 9-factor report</span>
        </div>

        <div className="overflow-x-auto scrollbar-thin max-h-36">
          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Contract</th>
                <th>Market Prob</th>
                <th>AI Forecast</th>
                <th>Net Alpha Edge</th>
                <th>Confidence</th>
                <th>Spread</th>
                <th>OFI Flow</th>
                <th className="text-right">Action</th>
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
                      isSelected ? 'bg-blue-500/20 border-l-2 border-blue-500' : 'hover:bg-[#252836]/40'
                    }`}
                  >
                    <td className="max-w-[200px] truncate font-semibold text-[#e8eaf0] text-[11px]" title={rowTitle}>
                      {rowTitle}
                    </td>
                    <td className="mono text-[#8b91a8]">
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
                    <td className="mono text-[#8b91a8]">
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

      {/* 3. Detailed 9-Factor Inspection Grid for Selected Contract */}
      {analysis && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          {/* Col 1: Probability & Expected Value Matrix */}
          <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col justify-between">
            <div>
              <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#e8eaf0]">
                  📊 Probabilistic Valuation
                </span>
                <span className="text-[10px] text-cyan-400 mono">Calibrated Isotonic</span>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">Market-Implied Probability:</span>
                  <span className="mono text-sm font-bold text-[#e8eaf0]">
                    {((analysis.market_implied_prob || 0) * 100).toFixed(1)}%
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">AI/ML Forecast (v{analysis.model_metadata?.version || '1.0'}):</span>
                  <span className="mono text-sm font-bold text-cyan-400">
                    {((analysis.ml_forecast_prob || 0) * 100).toFixed(1)}%
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">95% Uncertainty Bounds:</span>
                  <span className="mono text-xs text-amber-400">
                    [{((analysis.uncertainty_interval?.[0] || 0) * 100).toFixed(1)}% — {((analysis.uncertainty_interval?.[1] || 0) * 100).toFixed(1)}%]
                  </span>
                </div>

                <div className="flex justify-between items-center bg-green-500/10 p-2.5 rounded border border-green-500/30">
                  <span className="text-xs font-semibold text-green-300">Net Expected Alpha Edge:</span>
                  <span className="mono text-sm font-black text-green-400">
                    {analysis.net_edge ? `${analysis.net_edge >= 0 ? '+' : ''}${(analysis.net_edge * 100).toFixed(1)}%` : '0.0%'}
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#252836] text-[10px] text-[#8b91a8] flex justify-between">
              <span>Model Brier Score: {analysis.model_metadata?.brier_score || 0.175}</span>
              <span>Model Confidence: {((analysis.confidence_score || 0) * 100).toFixed(0)}%</span>
            </div>
          </div>

          {/* Col 2: Microstructure, OFI & Depth */}
          <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col justify-between">
            <div>
              <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#e8eaf0]">
                  ⚡ Microstructure &amp; Order Flow
                </span>
                <span className="text-[10px] text-green-400 mono">L2 Queue Depth</span>
              </div>

              <div className="space-y-3">
                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">Top of Book Spread:</span>
                  <span className="mono text-xs font-bold text-[#e8eaf0]">
                    ${analysis.best_bid?.toFixed(3) || '—'} / ${analysis.best_ask?.toFixed(3) || '—'} ({(analysis.spread_dollars ? analysis.spread_dollars * 100 : 0).toFixed(1)}¢)
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">Order Flow Imbalance (OFI):</span>
                  <span className={`mono text-xs font-bold ${(analysis.order_flow_imbalance || 0) >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                    {analysis.order_flow_imbalance ? `${analysis.order_flow_imbalance >= 0 ? '+' : ''}${analysis.order_flow_imbalance}` : '0.00'}
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">5-Level Depth Liquidity:</span>
                  <span className="mono text-xs font-bold text-cyan-400">
                    ${analysis.total_liquidity_usdc?.toLocaleString() || '0'} USDC
                  </span>
                </div>

                <div className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836]">
                  <span className="text-xs text-[#8b91a8]">Estimated Slippage ($100 block):</span>
                  <span className="mono text-xs text-[#e8eaf0]">
                    {analysis.slippage_bps || 0} BPS
                  </span>
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#252836] text-[10px] text-[#8b91a8] flex justify-between">
              <span>Data Freshness: {analysis.data_freshness_seconds || 0}s ago</span>
              <span>Compute Time: {analysis.generation_time_ms || 1.2}ms</span>
            </div>
          </div>

          {/* Col 3: Fundamental Evidence & Action Reasons */}
          <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col justify-between">
            <div>
              <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex items-center justify-between">
                <span className="card-title text-xs font-bold text-[#e8eaf0]">
                  📰 Fundamental Evidence &amp; Reasons
                </span>
                <span className="text-[10px] text-amber-400 mono">NLP Sentiment</span>
              </div>

              <div className="space-y-2">
                <div className="bg-[#111318] p-2 rounded border border-[#252836]">
                  <span className="text-[10px] text-[#8b91a8] block font-semibold mb-1">Decision Rationale:</span>
                  {analysis.action_reasons && analysis.action_reasons.length > 0 ? (
                    <ul className="text-xs text-[#e8eaf0] space-y-1">
                      {analysis.action_reasons.map((r, i) => (
                        <li key={i} className="flex items-start gap-1.5">
                          <span className="text-blue-400 font-bold">•</span>
                          <span>{r}</span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <span className="text-xs text-[#8b91a8]">No risk warnings active.</span>
                  )}
                </div>

                <div className="bg-[#111318] p-2 rounded border border-[#252836]">
                  <span className="text-[10px] text-[#8b91a8] block font-semibold mb-1">Recent News Impact:</span>
                  {analysis.supporting_evidence && analysis.supporting_evidence.length > 0 ? (
                    <div className="space-y-1">
                      {analysis.supporting_evidence.map((s, i) => (
                        <div key={i} className="text-[11px] text-[#e8eaf0] truncate">
                          <span className="text-green-400 font-bold">[+{s.sentiment}]</span> {s.headline}
                        </div>
                      ))}
                    </div>
                  ) : (
                    <span className="text-xs text-[#4a5068]">No direct news matching token.</span>
                  )}
                </div>
              </div>
            </div>

            <div className="mt-3 pt-2 border-t border-[#252836] flex justify-end">
              <button
                onClick={() => fetchSingleMarket(analysis.token_id)}
                disabled={analyzingSingle}
                className="btn btn-primary text-xs px-3 py-1 font-bold"
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
