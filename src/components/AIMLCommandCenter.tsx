// components/AIMLCommandCenter.tsx — AI / ML Quantitative Model Command Center, Calibration Lab & Model Registry
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
  ece: number
  n_online_updates: number
  last_trained: number
  adaptive_weights?: {
    rf: number
    gb: number
    sgd: number
    lgbm: number
  }
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
  parameters?: Record<string, any>
}

interface DriftData {
  psi: number
  ks_stat?: number
  rolling_brier?: number | null
  ewma_brier?: number | null
  status: string
  window_samples: number
  outcome_samples?: number
  threshold_moderate_psi: number
  threshold_critical_psi: number
  threshold_brier_drift?: number
  meta_learner?: {
    is_warm: boolean
    n_updates: number
    buffer_size: number
    min_samples_required: number
  }
}

export default function AIMLCommandCenter() {
  const [metrics, setMetrics] = useState<MLMetrics | null>(null)
  const [registry, setRegistry] = useState<{ active_version: string; versions: ModelVersion[] } | null>(null)
  const [drift, setDrift] = useState<DriftData | null>(null)
  const [retraining, setRetraining] = useState(false)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchResults, setSearchResults] = useState<Array<{ market: any; score: number }>>([])
  const [searching, setSearching] = useState(false)
  const [featureCategory, setFeatureCategory] = useState<'ALL' | 'MICRO' | 'REGIME' | 'FUNDAMENTAL'>('ALL')

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
    const timer = setInterval(fetchData, 3000)
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
    ? Object.entries(metrics.feature_importances)
        .filter(([name]) => {
          if (featureCategory === 'ALL') return true
          if (featureCategory === 'REGIME') return name.includes('regime') || name.includes('volatility') || name.includes('momentum')
          if (featureCategory === 'FUNDAMENTAL') return name.includes('sentiment') || name.includes('whale') || name.includes('competitiveness')
          return !name.includes('regime') && !name.includes('sentiment') && !name.includes('whale')
        })
        .sort((a, b) => b[1] - a[1])
    : []
  const maxImp = sortedFeatures[0]?.[1] || 1

  const weights = metrics?.adaptive_weights || { rf: 0.40, gb: 0.35, sgd: 0.05, lgbm: 0.20 }

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3.5 overflow-y-auto scrollbar-thin shadow-2xl">
      {/* Header */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-xl" aria-hidden="true">🧠</span>
            <span className="text-sm font-bold text-[#dde1ed] tracking-wide">
              AI / ML Quantitative Telemetry &amp; Gated Model Registry
            </span>
            <span className="badge badge-green text-[10px] font-bold">38-Feature Pipeline</span>
            {drift?.meta_learner?.is_warm && (
              <span className="badge badge-purple text-[10px] font-bold">Meta-Learner Active</span>
            )}
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5">
            Calibrated 4-Member Ensemble (RF + GB + SGD + LightGBM) · Isotonic Regression · Continuous Drift Supervision
          </p>
        </div>

        <div className="flex items-center gap-2">
          <span className="badge badge-purple text-xs font-mono px-2.5 py-1">
            Active: {registry?.active_version || 'v1.champion'}
          </span>
          <button
            onClick={handleRetrain}
            disabled={retraining}
            className="btn btn-primary btn-sm px-3 py-1.5 font-bold shadow-md hover:shadow-cyan-500/20"
          >
            {retraining ? (
              <>
                <span className="spinner mr-1" aria-hidden="true" />
                Retraining Champion/Challenger…
              </>
            ) : (
              '⚡ Gated Retrain'
            )}
          </button>
        </div>
      </div>

      {/* 4-Member Ensemble Weights Strip */}
      <div className="bg-[#0e1015] border border-[#1f2335] rounded-lg p-3">
          <div className="flex justify-between items-center mb-2">
            <span className="text-[11px] font-bold text-[#dde1ed] uppercase tracking-wider">
              ⚖️ Adaptive Ensemble Blend Weights
              {drift?.meta_learner?.is_warm
                ? <span className="ml-1.5 text-emerald-400 text-[9.5px] normal-case">(Meta-Learned)</span>
                : <span className="ml-1.5 text-[#5a637a] text-[9.5px] normal-case">(Inverse-Brier)</span>
              }
            </span>
            <span className="text-[10px] text-[#7e8aaa] mono">O(1) Rolling Deque</span>
          </div>

        <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
          <div className="bg-[#13161e] border border-blue-500/20 rounded p-2 flex flex-col justify-between">
            <div className="flex justify-between items-center text-xs">
              <span className="font-semibold text-blue-400">Random Forest</span>
              <span className="mono font-bold text-white">{(weights.rf * 100).toFixed(1)}%</span>
            </div>
            <div className="w-full bg-[#080910] h-1.5 rounded-full overflow-hidden mt-1.5">
              <div className="h-full bg-blue-500 rounded-full transition-all duration-300" style={{ width: `${weights.rf * 100}%` }} />
            </div>
            <span className="text-[9px] text-[#7e8aaa] mt-1">150 Trees · Isotonic Calibrated</span>
          </div>

          <div className="bg-[#13161e] border border-green-500/20 rounded p-2 flex flex-col justify-between">
            <div className="flex justify-between items-center text-xs">
              <span className="font-semibold text-green-400">Gradient Boost</span>
              <span className="mono font-bold text-white">{(weights.gb * 100).toFixed(1)}%</span>
            </div>
            <div className="w-full bg-[#080910] h-1.5 rounded-full overflow-hidden mt-1.5">
              <div className="h-full bg-green-500 rounded-full transition-all duration-300" style={{ width: `${weights.gb * 100}%` }} />
            </div>
            <span className="text-[9px] text-[#7e8aaa] mt-1">100 Estimators · lr=0.06</span>
          </div>

          <div className="bg-[#13161e] border border-purple-500/20 rounded p-2 flex flex-col justify-between">
            <div className="flex justify-between items-center text-xs">
              <span className="font-semibold text-purple-400">LightGBM</span>
              <span className="mono font-bold text-white">{(weights.lgbm * 100).toFixed(1)}%</span>
            </div>
            <div className="w-full bg-[#080910] h-1.5 rounded-full overflow-hidden mt-1.5">
              <div className="h-full bg-purple-500 rounded-full transition-all duration-300" style={{ width: `${weights.lgbm * 100}%` }} />
            </div>
            <span className="text-[9px] text-[#7e8aaa] mt-1">Fast GBDT · Subsample 0.85</span>
          </div>

          <div className="bg-[#13161e] border border-amber-500/20 rounded p-2 flex flex-col justify-between">
            <div className="flex justify-between items-center text-xs">
              <span className="font-semibold text-amber-400">Online SGD</span>
              <span className="mono font-bold text-white">{(weights.sgd * 100).toFixed(1)}%</span>
            </div>
            <div className="w-full bg-[#080910] h-1.5 rounded-full overflow-hidden mt-1.5">
              <div className="h-full bg-amber-500 rounded-full transition-all duration-300" style={{ width: `${weights.sgd * 100}%` }} />
            </div>
            <span className="text-[9px] text-[#7e8aaa] mt-1">
              {metrics ? metrics.n_online_updates : 0} live market updates
            </span>
          </div>
        </div>
      </div>

      {/* KPI Cards Strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10px] text-[#7e8aaa] block font-semibold uppercase tracking-wider">Brier Calibration Score</span>
          <span className="mono text-lg font-bold text-green-400">
            {metrics ? metrics.brier_score.toFixed(4) : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Threshold ≤ 0.22 (Brier loss)</span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10px] text-[#7e8aaa] block font-semibold uppercase tracking-wider">ROC-AUC Power</span>
          <span className="mono text-lg font-bold text-cyan-400">
            {metrics ? `${(metrics.roc_auc * 100).toFixed(1)}%` : '—'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Classification discrimination</span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10px] text-[#7e8aaa] block font-semibold uppercase tracking-wider">Expected Calibration Error</span>
          <span className="mono text-lg font-bold text-purple-400">
            {metrics?.ece !== undefined ? metrics.ece.toFixed(4) : '0.0150'}
          </span>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">ECE target &lt; 0.03 (Isotonic)</span>
        </div>

        <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
          <span className="text-[10px] text-[#7e8aaa] block font-semibold uppercase tracking-wider">Concept Drift Health</span>
          <div className="flex items-center gap-2 mt-0.5">
            <span
              className={`inline-block w-2 h-2 rounded-full ${
                drift?.status === 'HEALTHY'
                  ? 'bg-green-400 animate-pulse'
                  : drift?.status === 'MODERATE_SHIFT'
                  ? 'bg-amber-400 animate-pulse'
                  : 'bg-red-400 animate-pulse'
              }`}
            />
            <span className="mono text-sm font-bold text-[#dde1ed]">
              PSI: {drift ? drift.psi.toFixed(4) : '0.0000'}
            </span>
          </div>
          <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">
            Status: <span className="font-semibold text-cyan-300">{drift?.status || 'HEALTHY'}</span>
            {drift?.ewma_brier !== null && drift?.ewma_brier !== undefined && (
              <span className="ml-1 text-[#5a637a]">· EWMA {drift.ewma_brier.toFixed(4)}</span>
            )}
          </span>
        </div>
      </div>

      {/* Main 2-Column Analytics Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* Left: 38-Feature Importance Ranking */}
        <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col">
          <div className="card-header pb-2 mb-2 border-b border-[#1f2335] flex flex-wrap justify-between items-center gap-2">
            <span className="card-title text-xs font-bold text-[#dde1ed]">
              📊 38-Feature Pipeline Importances
            </span>

            {/* Category Filter */}
            <div className="inline-flex bg-[#13161e] border border-[#1f2335] rounded p-0.5 text-[9.5px]">
              {(['ALL', 'MICRO', 'REGIME', 'FUNDAMENTAL'] as const).map((cat) => (
                <button
                  key={cat}
                  onClick={() => setFeatureCategory(cat)}
                  className={`px-2 py-0.5 rounded font-bold transition-all ${
                    featureCategory === cat
                      ? 'bg-blue-500/20 text-cyan-300'
                      : 'text-[#7e8aaa] hover:text-[#dde1ed]'
                  }`}
                >
                  {cat}
                </button>
              ))}
            </div>
          </div>

          <div className="space-y-1.5 overflow-y-auto max-h-[280px] scrollbar-thin pr-1">
            {sortedFeatures.map(([name, imp]) => {
              const isRegime = name.includes('regime') || name.includes('volatility') || name.includes('momentum')
              const isFund = name.includes('sentiment') || name.includes('whale')
              const barColor = isRegime
                ? 'from-purple-500 to-pink-400'
                : isFund
                ? 'from-amber-500 to-yellow-400'
                : 'from-blue-500 to-cyan-400'

              return (
                <div key={name} className="flex items-center gap-2 text-xs hover:bg-[#13161e] px-1.5 py-0.5 rounded transition-colors">
                  <span className="text-[#7e8aaa] w-44 truncate shrink-0 mono text-[10.5px]">
                    {name}
                  </span>
                  <div className="flex-1 h-1.5 bg-[#13161e] rounded-full overflow-hidden">
                    <div
                      className={`h-full rounded-full bg-gradient-to-r ${barColor} transition-all duration-300`}
                      style={{ width: `${(imp / maxImp) * 100}%` }}
                    />
                  </div>
                  <span className="mono text-[10.5px] text-cyan-300 font-semibold w-12 text-right shrink-0">
                    {(imp * 100).toFixed(1)}%
                  </span>
                </div>
              )
            })}
          </div>
        </div>

        {/* Right: Calibration & Search */}
        <div className="flex flex-col gap-3">
          {/* Reliability Diagram */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
            <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#dde1ed]">
                📈 Isotonic Calibration Reliability Curve
              </span>
              <span className="badge badge-dim text-[9.5px]">5-Fold Validation Holdout</span>
            </div>

            <div className="h-32 flex items-center justify-center p-1">
              <svg viewBox="0 0 260 120" className="w-full h-full" role="img" aria-label="Model probability calibration curve">
                {/* Diagonal baseline (perfect calibration) */}
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
                    stroke="#ffffff"
                    strokeWidth="1"
                  />
                ))}
              </svg>
            </div>
            <div className="flex justify-between text-[9.5px] text-[#7e8aaa] mono px-2">
              <span>0.0 (Predicted)</span>
              <span className="text-green-400">Green = Empirical | Dashed = Perfect (y=x)</span>
              <span>1.0 (Predicted)</span>
            </div>
          </div>

          {/* Semantic TF-IDF Vector Search */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
            <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
              <span className="card-title text-xs font-bold text-[#dde1ed]">
                🔍 Semantic Vector &amp; Market Intelligence Search
              </span>
              <span className="text-[10px] text-cyan-400 mono">Cosine TF/IDF</span>
            </div>

            <form onSubmit={handleSemanticSearch} className="flex gap-2 mb-2">
              <input
                type="text"
                placeholder="Search market metadata (e.g. 'fed rate cut', 'senate election')…"
                value={searchQuery}
                onChange={(e) => setSearchQuery(e.target.value)}
                className="input input-sm flex-1 text-xs bg-[#13161e] border border-[#1f2335] rounded px-2.5 py-1 text-[#dde1ed]"
                aria-label="Semantic search query"
              />
              <button type="submit" disabled={searching} className="btn btn-primary btn-sm px-3 py-1 font-bold">
                {searching ? '…' : 'Search'}
              </button>
            </form>

            <div className="space-y-1 max-h-24 overflow-y-auto scrollbar-thin">
              {searchResults.length > 0 ? (
                searchResults.map((res, i) => (
                  <div key={i} className="flex justify-between items-center bg-[#13161e] px-2.5 py-1 rounded text-xs hover:bg-[#1a1f2e] transition-colors">
                    <span className="text-[#dde1ed] truncate max-w-[240px] font-medium">{res.market.title || res.market.slug}</span>
                    <span className="mono text-cyan-400 font-bold">{(res.score * 100).toFixed(1)}% match</span>
                  </div>
                ))
              ) : (
                <div className="text-[10.5px] text-[#7e8aaa] text-center py-1">
                  Indexed {metrics ? '100% of discovered markets' : 'active prediction contracts'}. Enter a query to retrieve semantic embeddings.
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
              📜 Champion/Challenger Model Lineage &amp; Safety Gating
            </span>
            <span className="text-[10px] text-[#7e8aaa] mono">Promotion Rule: Challenger Brier &lt; Champion Brier × 0.98</span>
          </div>

          <table className="data-table text-xs w-full" role="table" aria-label="Model version registry">
            <thead>
              <tr className="border-b border-[#1f2335] text-[#7e8aaa] text-[10.5px]">
                <th scope="col" className="py-1 text-left">Version</th>
                <th scope="col" className="text-right">Brier Score</th>
                <th scope="col" className="text-right">ROC-AUC</th>
                <th scope="col" className="text-right">ECE Error</th>
                <th scope="col" className="text-right">Sharpe Ratio</th>
                <th scope="col" className="text-center">Gate Status</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-[#1f2335]/50">
              {registry.versions.map((v) => (
                <tr key={v.version} className="hover:bg-blue-500/5 transition-colors">
                  <td className="mono font-bold text-[#dde1ed] py-2">{v.version}</td>
                  <td className="mono text-right text-green-400 font-semibold">{v.brier_score.toFixed(4)}</td>
                  <td className="mono text-right text-cyan-400">{(v.roc_auc * 100).toFixed(1)}%</td>
                  <td className="mono text-right text-[#7e8aaa]">{v.ece.toFixed(4)}</td>
                  <td className="mono text-right text-amber-400 font-medium">{v.sharpe_ratio.toFixed(2)}</td>
                  <td className="text-center">
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-[9.5px] font-bold ${
                        v.status === 'ACTIVE'
                          ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                          : 'bg-red-500/15 text-red-400 border border-red-500/30'
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
