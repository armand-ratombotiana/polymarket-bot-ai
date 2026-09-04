// components/ArbitrageMatrixView.tsx — High-Frequency Binary Dutch-Book Arbitrage Scanner
'use client'

import { useEffect, useState, useMemo } from 'react'
import { AlertTriangle, X } from 'lucide-react'
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

interface Props {
  onSelectMarket?: (m: { tokenId: string; slug: string }) => void
}

export default function ArbitrageMatrixView({ onSelectMarket }: Props = {}) {
  const [opportunities, setOpportunities] = useState<ArbOpportunity[]>([])
  const [loading, setLoading] = useState(true)
  const [executing, setExecuting] = useState<string | null>(null)
  const [lastExecuted, setLastExecuted] = useState<{ ok: boolean; message: string } | null>(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [minBps, setMinBps] = useState(10)
  // W22-1 — surface fetch failures instead of silent swallowing.
  const [fetchError, setFetchError] = useState<string | null>(null)

  const fetchOpportunities = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/arbitrage/opportunities`)
      if (res.ok) {
        const data = await res.json()
        setOpportunities(data.opportunities || [])
        setFetchError(null)
      } else {
        setFetchError(`Failed to load arbitrage opportunities (HTTP ${res.status})`)
      }
    } catch (e) {
      console.error('[ArbitrageMatrixView] Failed to fetch arbitrage opportunities:', e)
      setFetchError(e instanceof Error ? e.message : 'Network error loading arbitrage opportunities')
    }
    setLoading(false)
  }

  useEffect(() => {
    fetchOpportunities()
    const timer = setInterval(fetchOpportunities, 2500)
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
          size_usdc: Math.min(opp.max_executable_size_usdc, 3.0), // $3 per-market risk ceiling
        }),
      })
      if (res.ok) {
        const data = await res.json()
        const statuses = (data.legs || []).map((l: { leg: string; status: string }) => `${l.leg}: ${l.status}`).join(' · ')
        setLastExecuted({ ok: true, message: `Arbitrage legs successfully executed (${statuses})` })
        fetchOpportunities()
      } else {
        const body = await res.json().catch(() => null)
        setLastExecuted({ ok: false, message: body?.detail || `Execution rejected by risk engine (HTTP ${res.status})` })
      }
    } catch (e) {
      console.error('[ArbitrageMatrixView] Failed to execute arbitrage:', e)
      setLastExecuted({ ok: false, message: 'Execution network request failed' })
    }
    setExecuting(null)
  }

  const filteredOpps = useMemo(() => {
    return opportunities.filter((opp) => {
      const matchesSearch = opp.slug.toLowerCase().includes(searchQuery.toLowerCase())
      const matchesBps = opp.gross_profit_bps >= minBps
      return matchesSearch && matchesBps
    })
  }, [opportunities, searchQuery, minBps])

  const maxEdge = opportunities.reduce((max, o) => Math.max(max, o.gross_profit_bps), 0)
  const avgRoi = opportunities.length > 0
    ? opportunities.reduce((sum, o) => sum + o.net_roi_pct, 0) / opportunities.length
    : 0

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3.5 overflow-y-auto scrollbar-thin shadow-2xl">
      {/* Header */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-xl" aria-hidden="true">⚡</span>
            <span className="text-sm font-bold text-[#dde1ed] tracking-wide">
              High-Frequency Binary Dutch-Book Arbitrage Scanner
            </span>
            <span className="badge badge-amber text-[9.5px]">Paper Mode · $3 Cap</span>
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5">
            Real-time mispricing detector: <code>Ask(YES) + Ask(NO) &lt; $1.00 - fees</code> (Guaranteed synthetic delta-neutral profit)
          </p>
        </div>

        {/* Aggregate KPI Strip */}
        <div className="flex items-center gap-2 text-xs">
          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Active Arbs:</span>
            <span className="mono font-bold text-cyan-400 text-xs">{opportunities.length}</span>
          </div>
          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Max Edge:</span>
            <span className="mono font-bold text-green-400 text-xs">+{maxEdge.toFixed(0)} bps</span>
          </div>
          <div className="bg-[#0e1015] border border-[#1f2335] px-2.5 py-1 rounded-md flex items-center gap-1.5">
            <span className="text-[10px] text-[#7e8aaa] uppercase font-semibold">Avg Net ROI:</span>
            <span className="mono font-bold text-emerald-400 text-xs">+{avgRoi.toFixed(2)}%</span>
          </div>
          <button
            onClick={() => {
              if (opportunities.length === 0) return
              const headers = ['Market Slug', 'YES Ask', 'NO Ask', 'Combined Cost', 'Gross Edge (bps)', 'Net ROI (%)', 'Max Executable USD']
              const rows = opportunities.map((o) => [
                `"${o.slug.replace(/"/g, '""')}"`,
                o.yes_ask.toFixed(4),
                o.no_ask.toFixed(4),
                o.total_cost.toFixed(4),
                o.gross_profit_bps.toFixed(0),
                o.net_roi_pct.toFixed(2),
                o.max_executable_size_usdc.toFixed(2),
              ])
              const csvContent = 'data:text/csv;charset=utf-8,' + [headers.join(','), ...rows.map((r) => r.join(','))].join('\n')
              const encodedUri = encodeURI(csvContent)
              const link = document.createElement('a')
              link.setAttribute('href', encodedUri)
              link.setAttribute('download', `polymarket_arbitrage_${Date.now()}.csv`)
              document.body.appendChild(link)
              link.click()
              document.body.removeChild(link)
            }}
            disabled={opportunities.length === 0}
            className="btn btn-ghost btn-sm text-[10px] px-2 py-0.5 border border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450] flex items-center gap-1"
            title="Export Arbitrage Matrix CSV"
          >
            📥 CSV
          </button>
        </div>
      </div>

      {/* Filter & Execution Controls */}
      <div className="flex flex-wrap items-center justify-between gap-3 bg-[#0e1015] p-2.5 rounded-lg border border-[#1f2335]">
        <div className="relative flex-1 max-w-sm">
          <input
            type="text"
            placeholder="Filter arbitrage by market name..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="w-full bg-[#13161e] border border-[#1f2335] rounded text-xs px-2.5 py-1.5 text-[#dde1ed] placeholder-[#3e4560] outline-none"
          />
        </div>

        <div className="flex items-center gap-3 text-xs">
          <div className="flex items-center gap-2">
            <span className="text-[#7e8aaa] text-[11px] font-semibold">Min Profit:</span>
            <input
              type="range"
              min={0}
              max={150}
              step={5}
              value={minBps}
              onChange={(e) => setMinBps(Number(e.target.value))}
              className="w-24 accent-cyan-400 cursor-pointer"
            />
            <span className="mono text-cyan-400 font-bold w-12">{minBps} bps</span>
          </div>

          <button
            onClick={fetchOpportunities}
            className="btn btn-ghost btn-xs text-[#7e8aaa] hover:text-white border border-[#1f2335] px-2.5 py-1"
          >
            🔄 Scan Now
          </button>
        </div>
      </div>

      {lastExecuted && (
        <div
          className={`text-xs px-3 py-2 rounded flex justify-between items-center ${
            lastExecuted.ok
              ? 'bg-green-500/10 border border-green-500/30 text-green-400'
              : 'bg-red-500/10 border border-red-500/30 text-red-400'
          }`}
          role="status"
        >
          <span>{lastExecuted.ok ? '✅ ' : '⚠️ '}{lastExecuted.message}</span>
          <button onClick={() => setLastExecuted(null)} className="hover:underline font-bold ml-2">✕</button>
        </div>
      )}

      {/* W22-1 — fetch-error banner (previously silently swallowed). */}
      {fetchError && (
        <div
          className="banner-danger text-xs px-3 py-2 rounded flex justify-between items-center"
          role="alert"
        >
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>{fetchError}</span>
          </span>
          <div className="flex items-center gap-2">
            <button onClick={() => fetchOpportunities()} className="hover:underline text-xs">Retry</button>
            <button
              onClick={() => setFetchError(null)}
              className="hover:underline text-xs flex items-center gap-0.5"
              aria-label="Dismiss error"
            >
              <X className="w-3 h-3" aria-hidden="true" /> Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Opportunities List */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex-1">
        <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            🎯 Verified Dutch-Book Pairs ({filteredOpps.length})
          </span>
          <span className="text-[10px] text-[#7e8aaa] mono">Automatic Dual-Leg Order Placement</span>
        </div>

        {loading ? (
          <div className="flex flex-col items-center justify-center h-40 text-xs text-[#7e8aaa]">
            <span className="spinner mb-2" aria-hidden="true" />
            Scanning synchronized binary order books for Dutch-book inefficiencies…
          </div>
        ) : filteredOpps.length === 0 ? (
          <div className="empty-state py-8">
            <span className="empty-state-icon text-2xl" aria-hidden="true">🎯</span>
            <span className="empty-state-title text-sm font-semibold">No arbitrage discrepancies found</span>
            <span className="empty-state-desc text-xs max-w-md text-center text-[#7e8aaa]">
              When the combined ask cost of YES and synthetic NO drops below $0.995 (exceeding {minBps} bps edge), opportunities will appear here.
            </span>
          </div>
        ) : (
          <div className="overflow-x-auto scrollbar-thin table-container">
            <table className="data-table text-xs w-full" role="table" aria-label="Arbitrage opportunities table">
              <thead>
                <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                  <th scope="col" className="min-w-[200px] text-left py-1">Market Contract</th>
                  <th scope="col" className="text-right">YES Ask</th>
                  <th scope="col" className="text-right">NO Ask</th>
                  <th scope="col" className="text-right">Combined Cost</th>
                  <th scope="col" className="text-right">Gross Edge</th>
                  <th scope="col" className="text-right">Net ROI</th>
                  <th scope="col" className="text-right">Max Cap ($3)</th>
                  <th scope="col" className="text-center">Action</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[#1f2335]/50">
                {filteredOpps.map((opp) => {
                  const info = formatHierarchicalMarket(opp.slug)
                  return (
                    <tr key={opp.token_id_yes} className="hover:bg-blue-500/10 transition-colors group">
                      <td
                        className="py-2.5 max-w-[240px] cursor-pointer"
                        onClick={() => onSelectMarket?.({ tokenId: opp.token_id_yes, slug: opp.slug })}
                      >
                        <div className="flex flex-col gap-0.5">
                          <span className="text-[9px] text-cyan-400 font-bold uppercase tracking-wider truncate">
                            {info.category.icon} {info.eventTitle}
                          </span>
                          <span className="text-[#dde1ed] group-hover:text-cyan-300 font-medium leading-snug text-xs block whitespace-normal transition-colors" title={info.fullLabel}>
                            {info.question}
                          </span>
                        </div>
                      </td>
                      <td className="mono text-right text-green-400 font-semibold">{fmtPrice(opp.yes_ask)}</td>
                      <td className="mono text-right text-cyan-400 font-semibold">{fmtPrice(opp.no_ask)}</td>
                      <td className="mono text-right text-amber-400 font-bold">{fmtPrice(opp.total_cost)}</td>
                      <td className="mono text-right font-bold text-green-400">+{opp.gross_profit_bps.toFixed(0)} bps</td>
                      <td className="mono text-right font-bold text-emerald-400">+{opp.net_roi_pct.toFixed(2)}%</td>
                      <td className="mono text-right text-[#dde1ed]">{fmtUsd(Math.min(opp.max_executable_size_usdc, 3.0))}</td>
                      <td className="text-center">
                        <button
                          onClick={() => handleExecute(opp)}
                          disabled={executing === opp.token_id_yes}
                          className="btn btn-primary btn-xs px-3 py-1 font-bold shadow-md hover:shadow-cyan-500/20"
                          aria-label={`Execute paper arbitrage on ${info.question}`}
                        >
                          {executing === opp.token_id_yes ? (
                            <>
                              <span className="spinner mr-1" aria-hidden="true" />
                              Routing…
                            </>
                          ) : (
                            '⚡ Execute Arb'
                          )}
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
