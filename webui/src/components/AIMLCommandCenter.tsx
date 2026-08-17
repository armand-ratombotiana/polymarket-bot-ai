// components/AIMLCommandCenter.tsx — AI / ML Command Center, Calibration Lab & Model Registry
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
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">🧠</span>
            <span className="text-sm font-bold text-[#dde1ed]">
              AI / ML Quantitative Model Telemetry &amp; Registry
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            32-Feature Extraction Pipeline · RF+GB+SGD Ensemble · Calibration &amp; Drift Diagnostics
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="badge badge-purple text-xs font-mono">
            Active: {registry?.active_version || '—'}
          </span>
          <button
            onClick={handleRetrain}
            disabled={retraining}
            className="btn btn-primary btn-sm"
          >
            {retraining ? (
              <>
                <span className="spinner mr-1" aria-hidden="true" />
                Retraining Ensemble…
              </>
            ) : (
              '⚡ Retrain Model'
            )}
          </button>
        </div>
      </div>

      {/* Experimental Synthetic Notice */}
      <div className="banner-experimental text-xs py-2 px-3" role="note">
        <span aria-hidden="true">🧪</span>
        <span>
          <strong>EXPERIMENTAL — SYNTHETIC TRAINING DATA:</strong> The current ensemble model is trained on 3,000 synthetic generator samples. Evaluation metrics (AUC, Brier score) reflect synthetic holdouts and are not validated for real-capital trading.
        </span>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10.5px] text-[#7e8aaa] block font-medium uppercase">Brier Score (Synthetic)</span>
          <span className="mono text-base font-bold text-green-400">
            {metrics ? metrics.brier_score.toFixed(4) : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Threshold: ≤ 0.22</span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10.5px] text-[#7e8aaa] block font-medium uppercase">ROC-AUC Power</span>
          <span className="mono text-base font-bold text-cyan-400">
            {metrics ? `${(metrics.roc_auc * 100).toFixed(1)}%` : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Discriminative capability</span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10.5px] text-[#7e8aaa] block font-medium uppercase">Drift PSI Index</span>
          <span className="mono text-base font-bold text-amber-400">
            {drift ? drift.psi.toFixed(4) : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">
            vs uniform baseline ({drift?.status || '—'})
          </span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10.5px] text-[#7e8aaa] block font-medium uppercase">Online SGD Updates</span>
          <span className="mono text-base font-bold text-[#dde1ed]">
            {metrics ? metrics.n_online_updates : 0}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Incremental outcome learning</span>
        </div>
      </div>

      {/* Main 2-Column Analytics Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Left: 32-Feature Store & Importances */}
        <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              📊 32-Feature Microstructure Importances
            </span>
            <span className="text-[10px] text-[#7e8aaa] mono">Gini split gain</span>
          </div>

          <div className="space-y-1.5 overflow-y-auto max-h-[260px] scrollbar-thin pr-1">
            {sortedFeatures.map(([name, imp]) => (
              <div key={name} className="flex items-center gap-2 text-xs">
                <span className="text-[#7e8aaa] w-40 truncate shrink-0 mono text-[10.5px]">
                  {name}
                </span>
                <div className="flex-1 h-1.5 bg-[#13161e] rounded-full overflow-hidden">
                  <div
                    className="h-full rounded-full bg-gradient-to-r from-blue-500 to-cyan-400 transition-all duration-300"
                    style={{ width: `${(imp / maxImp) * 100}%` }}
                  />
                </div>
                <span className="mono text-[10.5px] text-cyan-400 font-semibold w-12 text-right shrink-0">
                  {(imp * 100).toFixed(1)}%
                </span>
              </div>
            ))}
          </div>
        </div>

        {/* Right: Calibration & Search */}
        <div className="flex flex-col gap-3">
          {/* Reliability Diagram */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
            <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#dde1ed]">
                📈 Probability Calibration Diagram
              </span>
              <span className="badge badge-dim text-[9.5px]">Holdout Set</span>
            </div>

            <div className="h-28 flex items-center justify-center p-1">
              <svg viewBox="0 0 260 120" className="w-full h-full" role="img" aria-label="Model probability calibration curve">
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
                    strokeWidth="2"
                    strokeLinecap="round"
                  />
                )}
                {metrics?.reliability_curve?.map((pt, i) => (
                  <circle
                    key={i}
                    cx={15 + pt.bin_center * 230}
                    cy={105 - pt.empirical_freq * 90}
                    r="2.5"
                    fill="#3b82f6"
                  />
                ))}
              </svg>
            </div>
            <div className="flex justify-between text-[9.5px] text-[#7e8aaa] mono px-2">
              <span>0.0 (Predicted)</span>
              <span>Theoretical: Diagonal (y=x)</span>
              <span>1.0 (Predicted)</span>
            </div>
          </div>

          {/* Lexical Search */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
            <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#dde1ed]">
                🔍 Lexical / TF-IDF Similarity Search
              </span>
              <span className="text-[10px] text-cyan-400 mono">Cosine Score</span>
            </div>

            <form onSubmit={handleSemanticSearch} className="flex gap-2 mb-2">
              <input
                type="text"
                placeholder="Search market metadata (e.g. 'fed rate cut')…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="input input-sm flex-1 text-xs"
                aria-label="Semantic search query"
              />
              <button type="submit" disabled={searching} className="btn btn-primary btn-sm">
                {searching ? '…' : 'Query'}
              </button>
            </form>

            <div className="space-y-1 max-h-24 overflow-y-auto scrollbar-thin">
              {searchResults.length > 0 ? (
                searchResults.map((res, i) => (
                  <div key={i} className="flex justify-between items-center bg-[#13161e] px-2.5 py-1 rounded text-xs">
                    <span className="text-[#dde1ed] truncate max-w-[240px]">{res.market.title || res.market.slug}</span>
                    <span className="mono text-cyan-400 font-semibold">{(res.score * 100).toFixed(1)}% match</span>
                  </div>
                ))
              ) : (
                <div className="text-[10.5px] text-[#7e8aaa] text-center py-1">
                  TF-IDF word/bigram search across tracked prediction market metadata.
                </div>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Model Registry Version Lineage */}
      {registry && registry.versions.length > 0 && (
        <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
          <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              📜 Model Version Lineage &amp; Gatekeeping
            </span>
            <span className="text-[10px] text-[#7e8aaa] mono">Brier ≤ 0.22 / AUC ≥ 70%</span>
          </div>

          <table className="data-table text-xs" role="table" aria-label="Model version registry">
            <thead>
              <tr>
                <th scope="col">Version</th>
                <th scope="col">Brier Score</th>
                <th scope="col">ROC-AUC</th>
                <th scope="col">ECE Error</th>
                <th scope="col">Sharpe Ratio</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {registry.versions.map((v) => (
                <tr key={v.version} className="hover:bg-blue-500/5 transition-colors">
                  <td className="mono font-bold text-[#dde1ed]">{v.version}</td>
                  <td className="mono text-green-400 font-semibold">{v.brier_score.toFixed(4)}</td>
                  <td className="mono text-cyan-400">{(v.roc_auc * 100).toFixed(1)}%</td>
                  <td className="mono text-[#7e8aaa]">{v.ece.toFixed(4)}</td>
                  <td className="mono text-amber-400 font-medium">{v.sharpe_ratio.toFixed(2)}</td>
                  <td>
                    <span
                      className={`badge text-[9.5px] ${
                        v.status === 'ACTIVE'
                          ? 'badge-green'
                          : 'badge-red'
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
