// components/StrategyMatrix.tsx — Quantitative Strategy Registry
'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, X } from 'lucide-react'
import { getApiUrl, apiFetch } from '@/lib/api'

interface StrategyMeta {
  strategy_id: string
  name: string
  category: string
  description: string
  risk_level: string
  is_running: boolean
}

// U14: Per-strategy live P&L row from GET /api/leaderboard (subset of fields)
interface StrategyPerf {
  strategy: string
  net_pnl: number
  win_rate: number
  closed_trades: number
}

// Canonical implemented strategies supported by the execution bot
const IMPLEMENTED_STRATEGIES = new Set([
  'mm_avellaneda_stoikov',
  'arb_binary_dutch_book',
  'ml_random_forest_quant',
])

const CATEGORIES = [
  { id: 'all', label: 'All Catalog' },
  { id: 'implemented', label: 'Implemented (3)' },
  { id: 'market_making', label: 'Market Making' },
  { id: 'arbitrage', label: 'Arbitrage' },
  { id: 'statistical', label: 'Stat Arb' },
  { id: 'momentum', label: 'Momentum' },
  { id: 'event_driven', label: 'Event Driven' },
  { id: 'machine_learning', label: 'AI / ML' },
]

export default function StrategyMatrix() {
  const [catalog, setCatalog] = useState<StrategyMeta[]>([])
  // U14: per-strategy performance map keyed by strategy_id
  const [perf, setPerf] = useState<Record<string, StrategyPerf>>({})
  const [activeTab, setActiveTab] = useState('all')
  const [search, setSearch] = useState('')
  const [toggling, setToggling] = useState<string | null>(null)
  const [stubNotice, setStubNotice] = useState<string | null>(null)
  // W22-1 — surface fetch / toggle failures instead of silently swallowing.
  // Each error string is keyed by the operation that produced it so the
  // banner can show a useful "which call failed" prefix.
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [perfError, setPerfError] = useState<string | null>(null)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const fetchCatalog = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/strategies/catalog`)
      if (res.ok) {
        const json = await res.json()
        setCatalog(json.catalog || [])
        setCatalogError(null)
      } else {
        setCatalogError(`Failed to load strategy catalog (HTTP ${res.status})`)
      }
    } catch (e) {
      console.error('[StrategyMatrix] Failed to fetch strategy catalog:', e)
      setCatalogError(e instanceof Error ? e.message : 'Network error loading strategy catalog')
    }
  }

  // U14: live per-strategy P&L / win-rate / trade count from the leaderboard.
  // Fetched in parallel with fetchCatalog — never blocks catalog rendering.
  const fetchPerf = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/leaderboard`)
      if (res.ok) {
        const json = await res.json()
        const rows: StrategyPerf[] = json.ranked ?? []
        const map: Record<string, StrategyPerf> = {}
        for (const r of rows) map[r.strategy] = r
        setPerf(map)
        setPerfError(null)
      } else {
        setPerfError(`Failed to load per-strategy performance (HTTP ${res.status})`)
      }
    } catch (e) {
      console.error('[StrategyMatrix] Failed to fetch per-strategy performance:', e)
      setPerfError(e instanceof Error ? e.message : 'Network error loading per-strategy performance')
    }
  }

  useEffect(() => {
    // U14: catalog + leaderboard fetched in parallel (no await between them)
    fetchCatalog()
    fetchPerf()
    const timer = setInterval(() => {
      fetchCatalog()
      fetchPerf()
    }, 4000)
    return () => clearInterval(timer)
  }, [])

  const handleToggle = async (strategyId: string, currentStatus: boolean) => {
    if (!IMPLEMENTED_STRATEGIES.has(strategyId)) {
      setStubNotice(`"${strategyId}" is a metadata-only research stub (_execute_cycle = pass). It does not execute live trades.`)
      setTimeout(() => setStubNotice(null), 5000)
      return
    }

    setToggling(strategyId)
    setToggleError(null)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/strategies/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_name: strategyId, enabled: !currentStatus }),
      })
      if (!res.ok) {
        const body = await res.json().catch(() => null)
        const msg = body?.detail || `Risk gate rejected toggle (HTTP ${res.status})`
        console.error('[StrategyMatrix] Strategy toggle rejected:', msg)
        setToggleError(msg)
      } else {
        await fetchCatalog()
      }
    } catch (e) {
      console.error('[StrategyMatrix] Failed to toggle strategy:', e)
      setToggleError(e instanceof Error ? e.message : 'Network error toggling strategy')
    }
    setToggling(null)
  }

  const filtered = catalog.filter((s) => {
    const isImp = IMPLEMENTED_STRATEGIES.has(s.strategy_id)
    if (activeTab === 'implemented') return isImp
    const matchCat = activeTab === 'all' || s.category === activeTab
    const matchSearch =
      s.name.toLowerCase().includes(search.toLowerCase()) ||
      s.description.toLowerCase().includes(search.toLowerCase()) ||
      s.strategy_id.toLowerCase().includes(search.toLowerCase())
    return matchCat && matchSearch
  })

  const runningImplemented = catalog.filter((s) => s.is_running && IMPLEMENTED_STRATEGIES.has(s.strategy_id)).length

  return (
    <div className="card flex flex-col h-full overflow-hidden bg-[#13161e] border border-[#1f2335]">
      {/* Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#1f2335] gap-3">
        <div className="flex items-center gap-3">
          <span className="card-title text-sm font-bold text-[#dde1ed]">
            ⚡ Quantitative Strategy Matrix
          </span>
          <span className="badge badge-green text-xs font-semibold">
            {runningImplemented} of 3 Implemented Active
          </span>
          <span className="badge badge-dim text-[10px]">
            47 Stubs / Research
          </span>
        </div>

        {/* Search */}
        <input
          type="text"
          placeholder="Filter strategies…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="input input-sm w-48 text-xs"
          aria-label="Filter strategies"
        />
      </div>

      {/* Category Tabs */}
      <div className="flex items-center gap-1.5 px-3 py-2 bg-[#0e1015] border-b border-[#1f2335] overflow-x-auto scrollbar-thin shrink-0">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            onClick={() => setActiveTab(c.id)}
            className={`tab-item text-xs py-1 px-2.5 ${activeTab === c.id ? 'active' : ''}`}
          >
            {c.label}
          </button>
        ))}
      </div>

      {/* Notice Banner */}
      {stubNotice && (
        <div className="banner-warning mx-4 mt-2 text-xs py-2 px-3 flex items-center justify-between">
          <span>⚠️ {stubNotice}</span>
          <button onClick={() => setStubNotice(null)} className="text-white hover:underline text-xs ml-2">
            Dismiss
          </button>
        </div>
      )}

      {/* W22-1 — Error banners for fetch / toggle failures. Previously
          these errors were silently swallowed by `} catch {}`; now they
          surface inline with a dismiss control. */}
      {catalogError && (
        <div className="banner-danger mx-4 mt-2 text-xs py-2 px-3 flex items-center justify-between" role="alert">
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span><strong>Catalog:</strong> {catalogError}</span>
          </span>
          <button onClick={() => setCatalogError(null)} className="hover:underline text-xs ml-2 flex items-center gap-0.5" aria-label="Dismiss catalog error">
            <X className="w-3 h-3" aria-hidden="true" /> Dismiss
          </button>
        </div>
      )}
      {perfError && (
        <div className="banner-warning mx-4 mt-2 text-xs py-2 px-3 flex items-center justify-between" role="alert">
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span><strong>Performance:</strong> {perfError}</span>
          </span>
          <button onClick={() => setPerfError(null)} className="hover:underline text-xs ml-2 flex items-center gap-0.5" aria-label="Dismiss performance error">
            <X className="w-3 h-3" aria-hidden="true" /> Dismiss
          </button>
        </div>
      )}
      {toggleError && (
        <div className="banner-danger mx-4 mt-2 text-xs py-2 px-3 flex items-center justify-between" role="alert">
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span><strong>Toggle failed:</strong> {toggleError}</span>
          </span>
          <button onClick={() => setToggleError(null)} className="hover:underline text-xs ml-2 flex items-center gap-0.5" aria-label="Dismiss toggle error">
            <X className="w-3 h-3" aria-hidden="true" /> Dismiss
          </button>
        </div>
      )}

      {/* Grid of Strategies */}
      <div className="flex-1 overflow-y-auto p-3 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 scrollbar-thin">
        {filtered.map((s) => {
          const isImplemented = IMPLEMENTED_STRATEGIES.has(s.strategy_id)
          // U14: per-strategy perf row (may be undefined if no closed trades yet)
          const p = perf[s.strategy_id]
          return (
            <div
              key={s.strategy_id}
              className={`p-3 rounded-lg border transition-all flex flex-col justify-between ${
                s.is_running && isImplemented
                  ? 'bg-[#141724] border-blue-500/40 shadow-sm shadow-blue-500/10'
                  : isImplemented
                  ? 'bg-[#0e1015] border-[#1f2335] hover:border-[#3b82f6]/40'
                  : 'bg-[#0e1015]/60 border-[#1f2335]/60 opacity-65'
              }`}
            >
              <div>
                <div className="flex justify-between items-start mb-1.5 gap-1">
                  <div>
                    <span className="font-semibold text-xs text-[#dde1ed] block">{s.name}</span>
                    <span className="mono text-[9.5px] text-[#7e8aaa]">{s.strategy_id}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    {isImplemented ? (
                      <span className="badge badge-green text-[9px]">Implemented</span>
                    ) : (
                      <span className="badge badge-dim text-[9px]">Stub</span>
                    )}
                  </div>
                </div>
                <p className="text-[11px] text-[#7e8aaa] leading-relaxed mb-3">{s.description}</p>
                {/* U14: live P&L strip — green/red net_pnl, win-rate %, closed-trade count */}
                {p && (
                  <div
                    className={`mono text-[10px] font-semibold mb-2 ${
                      p.net_pnl >= 0 ? 'text-green-400' : 'text-red-400'
                    }`}
                    title={`net_pnl ${p.net_pnl.toFixed(2)} · win_rate ${(p.win_rate * 100).toFixed(1)}% · ${p.closed_trades} closed trades`}
                  >
                    {p.net_pnl >= 0 ? '+' : ''}{p.net_pnl.toFixed(2)} · {p.win_rate * 100}% WR · {p.closed_trades} trades
                  </div>
                )}
              </div>

              <div className="flex justify-between items-center pt-2 border-t border-[#1f2335]">
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-[#7e8aaa] uppercase mono">
                    {s.category.replace('_', ' ')}
                  </span>
                  <span className="text-[#3e4560]">·</span>
                  <span
                    className={`text-[9.5px] px-1.5 py-0.2 rounded font-bold uppercase mono ${
                      s.risk_level === 'LOW'
                        ? 'text-green-400 bg-green-500/10 border border-green-500/20'
                        : s.risk_level === 'MEDIUM'
                        ? 'text-amber-400 bg-amber-500/10 border border-amber-500/20'
                        : 'text-red-400 bg-red-500/10 border border-red-500/20'
                    }`}
                  >
                    {s.risk_level}
                  </span>
                </div>
                
                {isImplemented ? (
                  <div className="flex items-center gap-1.5">
                    {s.is_running && (
                      <span className="w-2 h-2 rounded-full bg-green-400 animate-pulse inline-block" title="Running live execution loop" />
                    )}
                    <button
                      onClick={() => handleToggle(s.strategy_id, s.is_running)}
                      disabled={toggling === s.strategy_id}
                      className={`btn btn-xs font-bold ${
                        s.is_running
                          ? 'btn-danger'
                          : 'btn-primary'
                      }`}
                    >
                      {toggling === s.strategy_id ? '…' : s.is_running ? 'Stop' : 'Deploy'}
                    </button>
                  </div>
                ) : (
                  <button
                    onClick={() => handleToggle(s.strategy_id, false)}
                    className="btn btn-ghost btn-xs text-[#7e8aaa] cursor-not-allowed opacity-60"
                    title="This strategy is a research stub with no execution loop"
                  >
                    Stub Only
                  </button>
                )}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}
