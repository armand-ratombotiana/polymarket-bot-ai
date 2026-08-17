// components/AIMLCommandCenter.tsx — Dedicated AI / ML Command Center, Calibration Lab & Model Registry
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

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

interface ModelVersion {
  version: string
  created_at: number
  brier_score: number
  roc_auc: number
  ece: number
  sharpe_ratio: number
  status: string
}

interface DriftData {
  psi: number
  status: string
  window_samples: number
  threshold_moderate: number
  threshold_critical: number
}

export default function AIMLCommandCenter() {
  const [metrics, setMetrics] = useState<MLMetrics | null>(null)
  const [registry, setRegistry] = useState<{ active_version: string; versions: ModelVersion[] } | null>(null)
  const [drift, setDrift] = useState<DriftData | null>(null)
  const [retraining, setRetraining] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Array<{ market: any; score: number }>>([])
  const [searching, setSearching] = useState(false)

  const fetchData = async () => {
    try {
      const apiUrl = getApiUrl()
      const [resM, resR, resD] = await Promise.all([
        apiFetch(`${apiUrl}/api/ml/metrics`),
        apiFetch(`${apiUrl}/api/ml/registry`),
        apiFetch(`${apiUrl}/api/ml/drift`),
      ])
      if (resM.ok) setMetrics(await resM.json())
      if (resR.ok) setRegistry(await resR.json())
      if (resD.ok) setDrift(await resD.json())
    } catch {}
  }

  useEffect(() => {
    fetchData()
    const timer = setInterval(fetchData, 4000)
    return () => clearInterval(timer)
  }, [])

  const handleRetrain = async () => {
    setRetraining(true)
    try {
      const apiUrl = getApiUrl()
      await apiFetch(`${apiUrl}/api/ml/retrain`, { method: 'POST' })
      await fetchData()
    } catch {}
    setRetraining(false)
  }

  const handleSemanticSearch = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!searchQuery.trim()) return
    setSearching(true)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/ai/search?query=${encodeURIComponent(searchQuery)}&top_k=6`)
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
              AI / ML Quantitative Command Center &amp; Model Registry
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            32-Feature Microstructure Pipeline, Isotonic Calibration, Drift Detection &amp; Model Governance
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="badge badge-blue text-xs font-mono">
            Active: {registry?.active_version || '—'}
          </span>
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
          <span className="text-[11px] text-[#4a5068] block font-medium">Brier Score (Calibration Loss)</span>
          <span className="mono text-lg font-bold text-green-400">
            {metrics ? metrics.brier_score.toFixed(4) : '—'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Optimal Brier threshold &lt; 0.22</span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">ROC-AUC Discriminative Power</span>
          <span className="mono text-lg font-bold text-cyan-400">
            {metrics ? `${(metrics.roc_auc * 100).toFixed(1)}%` : '—'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Directional classification accuracy</span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Concept Drift PSI Index</span>
          <span className="mono text-lg font-bold text-blue-400">
            {drift ? drift.psi.toFixed(4) : '—'}
          </span>
          <span className="text-[10px] text-green-400 block mt-0.5">
            Status: {drift?.status || '—'}
          </span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Online Updates / Model Health</span>
          <span className="mono text-lg font-bold text-amber-400">
            {metrics ? metrics.n_online_updates : 0}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Passive-Aggressive SGD updates</span>
        </div>
      </div>

      {/* Main 2-Column Analytics Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Left: 32-Feature Store & Importances */}
        <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              📊 32-Feature Microstructure &amp; Fundamental Importances
            </span>
            <span className="text-[10px] text-[#8b91a8] mono">Sorted by Gini split gain</span>
          </div>

          <div className="space-y-1.5 overflow-y-auto max-h-[300px] scrollbar-thin pr-1">
            {sortedFeatures.map(([name, imp]) => (
              <div key={name} className="flex items-center gap-2 text-xs">
                <span className="text-[#8b91a8] w-40 truncate shrink-0 mono text-[11px]">
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

            <div className="h-32 flex items-center justify-center p-1">
              <svg viewBox="0 0 260 120" className="w-full h-full">
                <line x1="15" y1="105" x2="245" y2="15" stroke="#3b4054" strokeWidth="1" strokeDasharray="3 3" />
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

            <div className="space-y-1 max-h-24 overflow-y-auto scrollbar-thin">
              {searchResults.length > 0 ? (
                searchResults.map((res, i) => (
                  <div key={i} className="flex justify-between items-center bg-[#111318] px-2.5 py-1 rounded text-xs">
                    <span className="text-[#e8eaf0] truncate max-w-[240px]">{res.market.title || res.market.slug}</span>
                    <span className="mono text-cyan-400 font-semibold">{(res.score * 100).toFixed(1)}% match</span>
                  </div>
                ))
              ) : (
                <div className="text-[11px] text-[#4a5068] text-center py-1">
                  Enter a semantic query to inspect vector distances across all prediction markets.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Model Registry Version Lineage */}
      {registry && registry.versions.length > 0 && (
        <div className="card p-3.5 bg-[#161822] border border-[#252836]">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              📜 Model Version Lineage &amp; Validation Gatekeeping
            </span>
            <span className="text-[10px] text-[#8b91a8] mono">Automated Risk Gates</span>
          </div>

          <table className="data-table text-xs">
            <thead>
              <tr>
                <th>Model Version</th>
                <th>Brier Score</th>
                <th>ROC-AUC</th>
                <th>ECE Error</th>
                <th>Sharpe Ratio</th>
                <th>Gate Status</th>
              </tr>
            </thead>
            <tbody>
              {registry.versions.map((v) => (
                <tr key={v.version} className="hover:bg-blue-500/5 transition-colors">
                  <td className="mono font-bold text-[#e8eaf0]">{v.version}</td>
                  <td className="mono text-green-400 font-semibold">{v.brier_score.toFixed(4)}</td>
                  <td className="mono text-cyan-400">{(v.roc_auc * 100).toFixed(1)}%</td>
                  <td className="mono text-[#8b91a8]">{v.ece.toFixed(4)}</td>
                  <td className="mono text-amber-400 font-medium">{v.sharpe_ratio.toFixed(2)}</td>
                  <td>
                    <span
                      className={`text-[10px] font-bold px-2 py-0.5 rounded ${
                        v.status === 'ACTIVE'
                          ? 'bg-green-500/20 text-green-400 border border-green-500/40'
                          : 'bg-red-500/20 text-red-400 border border-red-500/40'
                      }`}
                    >
                      {v.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
