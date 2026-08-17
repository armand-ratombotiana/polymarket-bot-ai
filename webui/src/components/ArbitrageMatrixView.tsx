// components/ArbitrageMatrixView.tsx — Binary Dutch-Book Arbitrage Matrix
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { formatHierarchicalMarket } from '@/lib/formatters'
import { fmtPrice, fmtUsd } from '@/lib/design-tokens'

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
  const [lastExecuted, setLastExecuted] = useState<{ ok: boolean; message: string } | null>(null)

  const fetchOpportunities = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/arbitrage/opportunities`)
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
      const res = await apiFetch(`${apiUrl}/api/arbitrage/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          token_id_yes: opp.token_id_yes,
          token_id_no: opp.token_id_no,
          size_usdc: Math.min(opp.max_executable_size_usdc, 3.0), // $3 per-market cap
        }),
      })
      if (res.ok) {
        const data = await res.json()
        const statuses = (data.legs || []).map((l: { leg: string; status: string }) => `${l.leg}: ${l.status}`).join(' · ')
        setLastExecuted({ ok: true, message: `Arb routed (${statuses})` })
        fetchOpportunities()
      } else {
        const body = await res.json().catch(() => null)
        setLastExecuted({ ok: false, message: body?.detail || `Execution rejected by risk engine (HTTP ${res.status})` })
      }
    } catch {
      setLastExecuted({ ok: false, message: 'Execution request failed' })
    }
    setExecuting(null)
  }

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* Header */}
      <div className="flex justify-between items-start pb-2 border-b border-[#1f2335]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">⚡</span>
            <span className="text-sm font-bold text-[#dde1ed]">
              Binary Dutch-Book Arbitrage Scanner
            </span>
            <span className="badge badge-amber text-[9.5px]">Paper Mode · $3 Cap</span>
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5">
            Detects binary market implied spread discounts: <code>Ask(YES) + Ask(NO) &lt; $1.00</code>
          </p>
        </div>
      </div>

      {/* Synthetic Pricing Disclosure */}
      <div className="banner-warning text-xs py-2 px-3" role="note">
        <span aria-hidden="true">ℹ️</span>
        <span>
          <strong>ESTIMATED PRICING NOTICE:</strong> NO-side token price is calculated as <code>(1.0 − YES_bid − 0.005)</code> when independent NO-token books are unpolled. Real dual-leg execution validates live liquidity on both sides prior to filling.
        </span>
      </div>

      {lastExecuted && (
        <div className={`text-xs px-3 py-2 rounded flex justify-between items-center ${
          lastExecuted.ok ? 'bg-green-500/10 border border-green-500/30 text-green-400' : 'bg-red-500/10 border border-red-500/30 text-red-400'
        }`} role="status">
          <span>{lastExecuted.ok ? '✅ ' : '⚠️ '}{lastExecuted.message}</span>
          <button onClick={() => setLastExecuted(null)} className="hover:underline font-bold ml-2">✕</button>
        </div>
      )}

      {/* Opportunities List */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex-1">
        <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            🎯 Detected Discrepancies ({opportunities.length})
          </span>
          <span className="text-[10px] text-[#7e8aaa] mono">Sorted by gross edge (bps)</span>
        </div>

        {loading ? (
          <div className="flex flex-col items-center justify-center h-40 text-xs text-[#7e8aaa]">
            <span className="spinner mb-2" aria-hidden="true" />
            Scanning paired order books for Dutch-book inefficiencies…
          </div>
        ) : opportunities.length === 0 ? (
          <div className="empty-state py-8">
            <span className="empty-state-icon" aria-hidden="true">🎯</span>
            <span className="empty-state-title">No Dutch-book opportunities detected</span>
            <span className="empty-state-desc">
              When market sum of YES ask and synthetic NO ask dips below $0.995, opportunities will appear here.
            </span>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin table-container">
            <table className="data-table text-xs" role="table" aria-label="Arbitrage opportunities table">
              <thead>
                <tr>
                  <th scope="col" className="min-w-[200px]">Market Contract</th>
                  <th scope="col" className="text-right">YES Ask</th>
                  <th scope="col" className="text-right">Est. NO Ask</th>
                  <th scope="col" className="text-right">Combined Cost</th>
                  <th scope="col" className="text-right">Gross Edge</th>
                  <th scope="col" className="text-right">Est. ROI</th>
                  <th scope="col" className="text-right">Max Size</th>
                  <th scope="col" className="text-right">Action</th>
                </tr>
              </thead>
              <tbody>
                {opportunities.map((opp) => {
                  const info = formatHierarchicalMarket(opp.slug)
                  return (
                    <tr key={opp.token_id_yes} className="hover:bg-blue-500/10 transition-colors">
                      <td className="py-2.5 max-w-[240px]">
                        <div className="flex flex-col gap-0.5">
                          <span className="text-[9px] text-[#7e8aaa] uppercase font-bold tracking-wider truncate">
                            {info.category.icon} {info.eventTitle}
                          </span>
                          <span className="text-[#dde1ed] font-medium leading-tight text-xs block whitespace-normal" title={info.fullLabel}>
                            {info.question}
                          </span>
                        </div>
                      </td>
                      <td className="mono text-right text-green-400 font-bold">{fmtPrice(opp.yes_ask)}</td>
                      <td className="mono text-right text-cyan-400 font-bold">{fmtPrice(opp.no_ask)}</td>
                      <td className="mono text-right text-amber-400 font-bold">{fmtPrice(opp.total_cost)}</td>
                      <td className="mono text-right font-bold text-green-400">+{opp.gross_profit_bps.toFixed(0)} bps</td>
                      <td className="mono text-right font-bold text-emerald-400">+{opp.net_roi_pct.toFixed(2)}%</td>
                      <td className="mono text-right text-[#dde1ed]">{fmtUsd(opp.max_executable_size_usdc)}</td>
                      <td className="text-right">
                        <button
                          onClick={() => handleExecute(opp)}
                          disabled={executing === opp.token_id_yes}
                          className="btn btn-success btn-xs"
                          aria-label={`Execute paper arbitrage on ${info.question}`}
                        >
                          {executing === opp.token_id_yes ? 'Routing…' : '⚡ Execute'}
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
