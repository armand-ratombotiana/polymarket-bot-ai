// components/AIMLCommandCenter.tsx — Dedicated AI / ML Command Center & Calibration Lab
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface ReliabilityBin {
  bin_center: number
  empirical_freq: number
  count: number
}

interface MLMetrics {
  model_type: string
  brier_score: number
  roc_auc: number
  log_loss: number
  n_online_updates: number
  last_trained: number
  feature_importances: Record<string, number>
  reliability_curve: ReliabilityBin[]
  model_ready: boolean
}

export default function AIMLCommandCenter() {
  const [metrics, setMetrics] = useState<MLMetrics | null>(null)
  const [retraining, setRetraining] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Array<{ market: any; score: number }>>([])
  const [searching, setSearching] = useState(false)

  const fetchMetrics = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/ml/metrics`)
      if (res.ok) {
        setMetrics(await res.json())
      }
    } catch {}
  }

  useEffect(() => {
    fetchMetrics()
    const timer = setInterval(fetchMetrics, 5000)
    return () => clearInterval(timer)
  }, [])

  const handleRetrain = async () => {
    setRetraining(true)
    try {
      const apiUrl = getApiUrl()
      await fetch(`${apiUrl}/api/ml/retrain`, { method: 'POST' })
      await fetchMetrics()
    } catch {}
    setRetraining(false)
  }

  const handleSemanticSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/ai/search?query=${encodeURIComponent(searchQuery)}&top_k=6`)
      if (res.ok) {
        const json = await res.json()
        setSearchResults(json.results || [])
      }
    } catch {}
    setSearching(false)
  }

  const sortedFeatures = metrics
    ? Object.entries(metrics.feature_importances).sort((a, b) => b[1] - a[1])
    : []
  const maxImp = sortedFeatures[0]?.[1] || 1

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Top Header & Benchmarks */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#252836] gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🧠</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              AI / ML Quantitative Command Center &amp; Calibration Lab
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Calibrated Gradient Boosting + Random Forest + Online SGD Ensemble Engine
          </p>
        </div>

        <div className="flex items-center gap-2">
          <button
            onClick={handleRetrain}
            disabled={retraining}
            className="btn btn-primary text-xs font-semibold px-4 py-1.5"
          >
            {retraining ? 'Re-training Ensemble…' : '⚡ Re-train & Calibrate Model'}
          </button>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Brier Score Loss (Lower is Better)</span>
          <span className="mono text-lg font-bold text-green-400">
            {metrics ? metrics.brier_score.toFixed(4) : '—'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Optimal probability calibration</span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">ROC-AUC Discriminative Power</span>
          <span className="mono text-lg font-bold text-cyan-400">
            {metrics ? `${(metrics.roc_auc * 100).toFixed(1)}%` : '—'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Directional classification accuracy</span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Online Ground-Truth Updates</span>
          <span className="mono text-lg font-bold text-blue-400">
            {metrics ? metrics.n_online_updates : 0}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Passive-Aggressive SGD steps</span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Log-Loss Cross Entropy</span>
          <span className="mono text-lg font-bold text-amber-400">
            {metrics ? metrics.log_loss.toFixed(4) : '—'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Information divergence score</span>
        </div>
      </div>

      {/* Main 2-Column Analytics Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left: 24-Feature Store & Importances */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              📊 24-Feature Microstructure Importances
            </span>
            <span className="text-[10px] text-[#8b91a8] mono">Sorted by Gini split gain</span>
          </div>

          <div className="space-y-1.5 overflow-y-auto max-h-[320px] scrollbar-thin pr-1">
            {sortedFeatures.map(([name, imp]) => (
              <div key={name} className="flex items-center gap-2 text-xs">
                <span className="text-[#8b91a8] w-36 truncate shrink-0 mono text-[11px]">
                  {name}
                </span>
                <div className="flex-1 h-2 bg-[#111318] rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-blue-500 to-cyan-400 transition-all duration-500"
                    style={{ width: `${(imp / maxImp) * 100}%` }}
                  />
                </div>
                <span className="mono text-[11px] text-cyan-400 font-semibold w-12 text-right shrink-0">
                  {(imp * 100).toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Right: Isotonic Reliability Curve & Semantic Embeddings Explorer */}
        <div className="flex flex-col gap-4">
          {/* Reliability Diagram */}
          <div className="card p-3.5 bg-[#161822] border border-[#252836]">
            <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#e8eaf0]">
                📈 Probability Calibration Reliability Diagram
              </span>
              <span className="badge badge-green text-[10px]">Isotonic Calibrated</span>
            </div>

            <div className="h-36 flex items-center justify-center p-1">
              <svg viewBox="0 0 260 120" className="w-full h-full">
                {/* Diagonal Reference Line (Perfect Calibration) */}
                <line x1="15" y1="105" x2="245" y2="15" stroke="#3b4054" strokeWidth="1" strokeDasharray="3 3" />
                {/* Empirical Curve */}
                {metrics?.reliability_curve && metrics.reliability_curve.length > 1 && (
                  <path
                    d={metrics.reliability_curve.reduce(
                      (acc, pt, i) =>
                        i === 0
                          ? `M ${15 + pt.bin_center * 230},${105 - pt.empirical_freq * 90}`
                          : `${acc} L ${15 + pt.bin_center * 230},${105 - pt.empirical_freq * 90}`,
                      ''
                    )}
                    fill="none"
                    stroke="#22c55e"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                  />
                )}
                {/* Dots */}
                {metrics?.reliability_curve?.map((pt, i) => (
                  <circle
                    key={i}
                    cx={15 + pt.bin_center * 230}
                    cy={105 - pt.empirical_freq * 90}
                    r="3"
                    fill="#3b82f6"
                  />
                ))}
              </svg>
            </div>
            <div className="flex justify-between text-[10px] text-[#8b91a8] mono px-2">
              <span>0% (Predicted)</span>
              <span>Theoretical: Diagonal line (y=x)</span>
              <span>100% (Predicted)</span>
            </div>
          </div>

          {/* Semantic Vector Search Explorer */}
          <div className="card p-3.5 bg-[#161822] border border-[#252836]">
            <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#e8eaf0]">
                🔍 Semantic Vector Embeddings Explorer
              </span>
              <span className="text-[10px] text-cyan-400 mono">Cosine Similarity Index</span>
            </div>

            <form onSubmit={handleSemanticSearch} className="flex gap-2 mb-2">
              <input
                type="text"
                placeholder="Search markets by semantic theme (e.g. 'crypto rate cut')…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="flex-1 bg-[#111318] border border-[#252836] rounded px-3 py-1 text-xs text-[#e8eaf0] placeholder-[#4a5068] focus:outline-none focus:border-blue-500"
              />
              <button type="submit" disabled={searching} className="btn btn-primary px-3 py-1 text-xs">
                {searching ? '…' : 'Query'}
              </button>
            </form>

            <div className="space-y-1.5 max-h-36 overflow-y-auto scrollbar-thin">
              {searchResults.length > 0 ? (
                searchResults.map((res, i) => (
                  <div key={i} className="flex justify-between items-center bg-[#111318] px-2.5 py-1 rounded text-xs">
                    <span className="text-[#e8eaf0] truncate max-w-[240px]">{res.market.title || res.market.slug}</span>
                    <span className="mono text-cyan-400 font-semibold">{(res.score * 100).toFixed(1)}% match</span>
                  </div>
                ))
              ) : (
                <div className="text-[11px] text-[#4a5068] text-center py-2">
                  Enter a semantic query to inspect vector distances across all prediction markets.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
