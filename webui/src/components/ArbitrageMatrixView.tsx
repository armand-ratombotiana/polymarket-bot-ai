// components/ArbitrageMatrixView.tsx — Cross-Market & Dutch-Book Arbitrage Matrix
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'
import { formatMarketTitle, getCategoryBadge } from '@/lib/formatters'

interface ArbitrageOpportunity {
  token_id_yes: string
  token_id_no: string
  slug: string
  category: string
  yes_ask: number
  no_ask: number
  total_cost: number
  gross_profit_bps: number
  net_roi_pct: number
  max_executable_size_usdc: number
  timestamp: number
}

export default function ArbitrageMatrixView() {
  const [opportunities, setOpportunities] = useState<ArbitrageOpportunity[]>([])
  const [loading, setLoading] = useState(true)
  const [executing, setExecuting] = useState<string | null>(null)

  const fetchOpps = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/arbitrage/opportunities`)
      if (res.ok) {
        const json = await res.json()
        setOpportunities(json.opportunities || [])
      }
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    fetchOpps()
    const timer = setInterval(fetchOpps, 3000)
    return () => clearInterval(timer)
  }, [])

  const handleExecute = async (opp: ArbitrageOpportunity) => {
    setExecuting(opp.token_id_yes)
    try {
      const apiUrl = getApiUrl()
      await fetch(`${apiUrl}/api/trade`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id: opp.token_id_yes,
          price: opp.yes_ask,
          side: 'BUY',
          size_usdc: Math.min(opp.max_executable_size_usdc, 5.0),
        }),
      })
      await fetchOpps()
    } catch {}
    setExecuting(null)
  }

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Header */}
      <div className="flex justify-between items-center pb-3 border-b border-[#252836]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">⚡</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              Binary Dutch-Book &amp; Multi-Pool Arbitrage Matrix
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Instantaneous negative-risk spreads (Ask(YES) + Ask(NO) &lt; $1.00) with atomic dual-leg routing
          </p>
        </div>
        <span className="badge badge-green text-xs font-semibold">1-Click Atomic Routing Active</span>
      </div>

      {/* Opportunities List */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836] flex-1">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            🎯 Active Arbitrage Spreads ({opportunities.length})
          </span>
          <span className="text-[10px] text-[#8b91a8] mono">Sorted by Profit BPS</span>
        </div>

        {loading ? (
          <div className="flex items-center justify-center h-40 text-xs text-[#8b91a8]">
            Scanning order book pairs for dual-leg inefficiencies…
          </div>
        ) : opportunities.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-40 text-[#4a5068] text-xs">
            <span>No negative-risk arbitrage opportunities detected right now</span>
            <span className="text-[10px] text-[#252836] mt-1">Autonomous scanner runs continuously every 3 seconds</span>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin">
            <table className="data-table text-xs">
              <thead>
                <tr>
                  <th>Prediction Market Contract</th>
                  <th>YES Ask</th>
                  <th>NO Ask</th>
                  <th>Combined Cost</th>
                  <th>Gross Margin (BPS)</th>
                  <th>Risk-Free ROI</th>
                  <th>Max Size</th>
                  <th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opp) => {
                  const title = formatMarketTitle(opp.slug)
                  const cat = getCategoryBadge(opp.category, opp.slug)
                  return (
                    <tr key={opp.token_id_yes} className="hover:bg-blue-500/10 transition-colors">
                      <td className="max-w-[220px]">
                        <div className="flex items-center gap-1.5">
                          <span className="text-xs shrink-0">{cat.icon}</span>
                          <span className="text-[#e8eaf0] font-semibold block truncate text-[11px]" title={title}>
                            {title}
                          </span>
                        </div>
                      </td>
                      <td className="mono text-green-400 font-semibold">${opp.yes_ask.toFixed(3)}</td>
                      <td className="mono text-cyan-400 font-semibold">${opp.no_ask.toFixed(3)}</td>
                      <td className="mono text-amber-400 font-bold">${opp.total_cost.toFixed(3)}</td>
                      <td className="mono font-bold text-green-400">+{opp.gross_profit_bps.toFixed(0)} BPS</td>
                      <td className="mono font-bold text-emerald-400">+{opp.net_roi_pct.toFixed(2)}%</td>
                      <td className="mono text-[#8b91a8]">${opp.max_executable_size_usdc.toFixed(2)}</td>
                      <td className="text-right">
                        <button
                          onClick={() => handleExecute(opp)}
                          disabled={executing === opp.token_id_yes}
                          className="btn btn-success text-[10px] font-bold px-3 py-1"
                        >
                          {executing === opp.token_id_yes ? 'Executing…' : '⚡ Arb 1-Click'}
                        </button>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
