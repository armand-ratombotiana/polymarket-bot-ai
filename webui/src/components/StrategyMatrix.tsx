// components/StrategyMatrix.tsx — 50+ Quantitative Strategy Hub
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface StrategyMeta {
  strategy_id: string
  name: string
  category: string
  description: string
  risk_level: string
  is_running: boolean
}

const CATEGORIES = [
  { id: 'all', label: 'All (50)' },
  { id: 'market_making', label: 'Market Making (8)' },
  { id: 'arbitrage', label: 'Arbitrage (8)' },
  { id: 'statistical', label: 'Stat Arb (8)' },
  { id: 'momentum', label: 'Momentum & Trend (8)' },
  { id: 'event_driven', label: 'Event & Sentiment (8)' },
  { id: 'machine_learning', label: 'AI / ML / RL (10)' },
]

export default function StrategyMatrix() {
  const [catalog, setCatalog] = useState<StrategyMeta[]>([])
  const [activeTab, setActiveTab] = useState('all')
  const [search, setSearch] = useState('')
  const [toggling, setToggling] = useState<string | null>(null)

  const fetchCatalog = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/strategies/catalog`)
      if (res.ok) {
        const json = await res.json()
        setCatalog(json.catalog || [])
      }
    } catch {}
  }

  useEffect(() => {
    fetchCatalog()
    const timer = setInterval(fetchCatalog, 4000)
    return () => clearInterval(timer)
  }, [])

  const handleToggle = async (strategyId: string, currentStatus: boolean) => {
    setToggling(strategyId)
    try {
      const apiUrl = getApiUrl()
      await fetch(`${apiUrl}/api/strategies/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ strategy_name: strategyId, enabled: !currentStatus }),
      })
      await fetchCatalog()
    } catch {}
    setToggling(null)
  }

  const filtered = catalog.filter((s) => {
    const matchCat = activeTab === 'all' || s.category === activeTab
    const matchSearch =
      s.name.toLowerCase().includes(search.toLowerCase()) ||
      s.description.toLowerCase().includes(search.toLowerCase())
    return matchCat && matchSearch
  })

  const runningCount = catalog.filter((s) => s.is_running).length

  return (
    <div className="card flex flex-col h-full overflow-hidden bg-[#111318] border border-[#252836]">
      {/* Header */}
      <div className="card-header flex flex-wrap justify-between items-center px-4 py-3 border-b border-[#252836] gap-3">
        <div className="flex items-center gap-3">
          <span className="card-title text-base font-bold text-[#e8eaf0]">
            ⚡ 50+ Quantitative Strategy Matrix
          </span>
          <span className="badge badge-green text-xs font-semibold">
            {runningCount} Active
          </span>
        </div>

        {/* Search */}
        <input
          type="text"
          placeholder="Filter 50 strategies…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="bg-[#161822] border border-[#252836] rounded-md px-3 py-1 text-xs mono text-[#e8eaf0] placeholder-[#4a5068] focus:outline-none focus:border-blue-500 w-56"
        />
      </div>

      {/* Category Tabs */}
      <div className="flex items-center gap-1.5 px-4 py-2 bg-[#0e1015] border-b border-[#252836] overflow-x-auto scrollbar-thin shrink-0">
        {CATEGORIES.map((c) => (
          <button
            key={c.id}
            onClick={() => setActiveTab(c.id)}
            className={`px-3 py-1 rounded text-xs font-medium whitespace-nowrap transition-all ${
              activeTab === c.id
                ? 'bg-blue-500 text-black font-semibold shadow-sm'
                : 'text-[#8b91a8] hover:text-[#e8eaf0] hover:bg-[#161822]'
            }`}
          >
            {c.label}
          </button>
        ))}
      </div>

      {/* Grid of Strategies */}
      <div className="flex-1 overflow-y-auto p-4 grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 scrollbar-thin">
        {filtered.map((s) => (
          <div
            key={s.strategy_id}
            className={`p-3 rounded-lg border transition-all flex flex-col justify-between ${
              s.is_running
                ? 'bg-[#141724] border-blue-500/40 shadow-sm shadow-blue-500/10'
                : 'bg-[#161822] border-[#252836] opacity-80 hover:opacity-100'
            }`}
          >
            <div>
              <div className="flex justify-between items-start mb-1.5">
                <span className="font-semibold text-xs text-[#e8eaf0]">{s.name}</span>
                <span
                  className={`text-[10px] uppercase font-bold px-1.5 py-0.5 rounded ${
                    s.risk_level === 'Low'
                      ? 'text-green-400 bg-green-500/10'
                      : s.risk_level === 'High'
                      ? 'text-red-400 bg-red-500/10'
                      : 'text-amber-400 bg-amber-500/10'
                  }`}
                >
                  {s.risk_level} Risk
                </span>
              </div>
              <p className="text-[11px] text-[#8b91a8] leading-relaxed mb-3">{s.description}</p>
            </div>

            <div className="flex justify-between items-center pt-2 border-t border-[#252836]/60">
              <span className="text-[10px] text-[#4a5068] uppercase mono">
                Category: {s.category.replace('_', ' ')}
              </span>
              <button
                onClick={() => handleToggle(s.strategy_id, s.is_running)}
                disabled={toggling === s.strategy_id}
                className={`px-3 py-1 rounded text-xs font-semibold transition-all ${
                  s.is_running
                    ? 'bg-blue-500/20 text-blue-400 border border-blue-500/50 hover:bg-red-500/20 hover:text-red-400 hover:border-red-500/50'
                    : 'bg-[#252836] text-[#8b91a8] hover:bg-blue-500 hover:text-black'
                }`}
              >
                {toggling === s.strategy_id ? '…' : s.is_running ? 'Running' : 'Deploy'}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
