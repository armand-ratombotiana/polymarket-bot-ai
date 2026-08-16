// components/ArbitrageMatrixView.tsx — Binary Dutch-Book Arbitrage Matrix with Redesigned Institutional Table UX
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'
import { formatHierarchicalMarket } from '@/lib/formatters'

interface ArbOpportunity {
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
  status: string
}

export default function ArbitrageMatrixView() {
  const [opportunities, setOpportunities] = useState<ArbOpportunity[]>([])
  const [loading, setLoading] = useState(true)
  const [executing, setExecuting] = useState<string | null>(null)
  const [lastExecuted, setLastExecuted] = useState<string | null>(null)

  const fetchOpportunities = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/arbitrage/opportunities`)
      if (res.ok) {
        const data = await res.json()
        setOpportunities(data.opportunities || [])
      }
    } catch {}
    setLoading(false)
  }

  useEffect(() => {
    fetchOpportunities()
    const timer = setInterval(fetchOpportunities, 3000)
    return () => clearInterval(timer)
  }, [])

  const handleExecute = async (opp: ArbOpportunity) => {
    setExecuting(opp.token_id_yes)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/arbitrage/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id_yes: opp.token_id_yes,
          token_id_no: opp.token_id_no,
          size_usdc: Math.min(opp.max_executable_size_usdc, 50.0),
        }),
      })
      if (res.ok) {
        setLastExecuted(`Successfully routed dual-leg Dutch book on ${opp.slug}`)
        fetchOpportunities()
      }
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
        <span className="badge badge-green text-xs font-bold">1-Click Atomic Routing Active</span>
      </div>

      {lastExecuted && (
        <div className="bg-green-500/10 border border-green-500/30 text-green-400 text-xs px-3 py-2 rounded flex justify-between items-center">
          <span>✅ {lastExecuted}</span>
          <button onClick={() => setLastExecuted(null)} className="text-green-500 hover:text-green-300 font-bold">✕</button>
        </div>
      )}

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
            <span className="font-semibold">No negative-risk arbitrage opportunities detected right now</span>
            <span className="text-[10px] text-[#4a5068] mt-1">Autonomous scanner runs continuously every 3 seconds</span>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin">
            <table className="data-table text-xs">
              <thead>
                <tr>
                  <th className="min-w-[220px]">Prediction Market Contract</th>
                  <th className="text-right">YES Ask</th>
                  <th className="text-right">NO Ask</th>
                  <th className="text-right">Combined Cost</th>
                  <th className="text-right">Gross Margin</th>
                  <th className="text-right">Risk-Free ROI</th>
                  <th className="text-right">Max Size</th>
                  <th className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opp) => {
                  const info = formatHierarchicalMarket(opp.slug)
                  return (
                    <tr key={opp.token_id_yes} className="hover:bg-blue-500/10 transition-colors">
                      <td className="py-2.5 max-w-[240px]">
                        <div className="flex flex-col gap-0.5">
                          <span className="text-[9px] text-[#8b91a8] uppercase font-bold tracking-wider truncate">
                            {info.category.icon} {info.eventTitle}
                          </span>
                          <span className="text-[#e8eaf0] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                            {info.question}
                          </span>
                        </div>
                      </td>
                      <td className="mono text-right text-green-400 font-bold">${opp.yes_ask.toFixed(3)}</td>
                      <td className="mono text-right text-cyan-400 font-bold">${opp.no_ask.toFixed(3)}</td>
                      <td className="mono text-right text-amber-400 font-bold">${opp.total_cost.toFixed(3)}</td>
                      <td className="mono text-right font-bold text-green-400">+{opp.gross_profit_bps.toFixed(0)} BPS</td>
                      <td className="mono text-right font-bold text-emerald-400">+{opp.net_roi_pct.toFixed(2)}%</td>
                      <td className="mono text-right text-[#e8eaf0]">${opp.max_executable_size_usdc.toFixed(2)}</td>
                      <td className="text-right">
                        <button
                          onClick={() => handleExecute(opp)}
                          disabled={executing === opp.token_id_yes}
                          className="btn btn-success text-[10px] font-bold px-3 py-1"
                        >
                          {executing === opp.token_id_yes ? 'Routing…' : '⚡ Execute Arb'}
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
