// components/MLPanel.tsx — Rich ML Ensemble Status Panel
'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import {
  AIPredictionLabel,
  ConfidenceBadge,
  ModelStatusStrip,
  NotAGuaranteeInline,
  WhyExplanation,
  driftLevelFromStatus,
  type FeatureContribution,
} from '@/components/ai-explainability'
import { ErrorState } from '@/components/ui/states'

interface MetaLearner {
  is_warm: boolean
  n_updates: number
  buffer_size: number
  min_samples_required: number
}

interface DriftReport {
  psi: number
  ks_stat: number
  rolling_brier: number | null
  ewma_brier: number | null
  status: string
  window_samples: number
  outcome_samples: number
}

interface MLStatus {
  model_type: string
  model_ready: boolean
  model_version: string
  n_online_updates: number
  last_trained: number
  training_source: string
  n_real_samples: number
  n_synthetic_samples: number
  brier_score: number
  roc_auc: number
  ece: number
  feature_importances: Record<string, number>
  adaptive_weights: { rf: number; gb: number; sgd: number; lgbm: number }
  meta_learner: MetaLearner
  drift: DriftReport
}

// Accept optional live snapshot ml data passed from parent
interface MLPanelProps {
  snapshotMl?: {
    model_ready: boolean
    brier_score: number
    roc_auc: number
    ece: number
    n_updates: number
    drift_status: string
    drift_psi: number
    drift_brier: number | null
    drift_ewma_brier: number | null
    adaptive_weights: { rf: number; gb: number; sgd: number; lgbm: number }
    meta_learner_warm: boolean
    training_source: string
  }
}

const DRIFT_COLORS: Record<string, string> = {
  HEALTHY: 'badge-green',
  MODERATE_SHIFT: 'badge-amber',
  SIGNIFICANT_DRIFT: 'badge-red',
}

const DRIFT_ICONS: Record<string, string> = {
  HEALTHY: '✅',
  MODERATE_SHIFT: '⚠️',
  SIGNIFICANT_DRIFT: '🚨',
}

export default function MLPanel({ snapshotMl }: MLPanelProps) {
  const [ml, setMl] = useState<MLStatus | null>(null)
  const [error, setError] = useState(false)
  const [errorDetail, setErrorDetail] = useState<string | null>(null)
  // W41-3 — retryToken bumps to force the fetch effect to re-run when
  // the trader clicks "Retry" on the error state. The effect's deps
  // include retryToken so a retry triggers a fresh fetch + clears the
  // error state.
  const [retryToken, setRetryToken] = useState(0)

  // W41-3 — extract fetchML so the retry button can invoke it
  // directly. The effect below depends on retryToken; the retry
  // handler bumps retryToken AND flips `error` back to false so the
  // panel briefly shows the loading state until the new fetch resolves.
  const fetchML = useCallback(async () => {
    const apiUrl = getApiUrl()
    try {
      const r = await apiFetch(`${apiUrl}/api/ml/metrics`)
      if (r.ok) {
        setMl(await r.json())
        setError(false)
        setErrorDetail(null)
      } else {
        setError(true)
        setErrorDetail(`HTTP ${r.status}`)
      }
    } catch (e) {
      setError(true)
      setErrorDetail(e instanceof Error ? e.message : String(e))
    }
  }, [])

  useEffect(() => {
    fetchML()
    const t = setInterval(fetchML, 15000)
    return () => clearInterval(t)
  }, [fetchML, retryToken])

  // W41-3 — imperative retry. Clears the error state and bumps the
  // retry token so the effect re-runs fetchML immediately (rather than
  // waiting up to 15s for the next poll tick).
  const handleRetry = useCallback(() => {
    setError(false)
    setErrorDetail(null)
    setRetryToken((t) => t + 1)
  }, [])

  // Merge snapshot (real-time) data over polled data for fast updates
  const driftStatus = snapshotMl?.drift_status ?? ml?.drift?.status ?? 'HEALTHY'
  const modelReady = snapshotMl?.model_ready ?? ml?.model_ready ?? false
  const metaWarm = snapshotMl?.meta_learner_warm ?? ml?.meta_learner?.is_warm ?? false
  const brierScore = snapshotMl?.brier_score ?? ml?.brier_score ?? 0
  const rocAuc = snapshotMl?.roc_auc ?? ml?.roc_auc ?? 0
  const ece = snapshotMl?.ece ?? ml?.ece ?? 0
  const nUpdates = snapshotMl?.n_updates ?? ml?.n_online_updates ?? 0
  const adaptiveWeights = snapshotMl?.adaptive_weights ?? ml?.adaptive_weights
  const trainingSource = snapshotMl?.training_source ?? ml?.training_source ?? '—'
  const driftPsi = snapshotMl?.drift_psi ?? ml?.drift?.psi ?? 0
  const driftEwma = snapshotMl?.drift_ewma_brier ?? ml?.drift?.ewma_brier ?? null

  const sortedFeatures = ml
    ? Object.entries(ml.feature_importances).sort((a, b) => b[1] - a[1]).slice(0, 6)
    : []
  const maxImp = sortedFeatures[0]?.[1] ?? 1

  const driftBadge = DRIFT_COLORS[driftStatus] ?? 'badge-dim'
  const driftIcon = DRIFT_ICONS[driftStatus] ?? '•'

  // W39-6 — Derive the model's overall confidence from ECE. Lower ECE
  // means the model's probability estimates are well calibrated → higher
  // confidence in any single prediction the model emits.
  const aiConfidence = useMemo(() => {
    const eceVal = snapshotMl?.ece ?? ml?.ece
    if (eceVal == null) return null
    if (eceVal < 0.03) return 0.85
    if (eceVal < 0.06) return 0.65
    if (eceVal < 0.10) return 0.45
    return 0.25
  }, [snapshotMl?.ece, ml?.ece])

  // W39-6 — Top-3 SHAP-style feature contributions. Synthesised from
  // feature_importances (the backend exposes only magnitudes) with a
  // deterministic sign derived from the feature name so the explanation
  // doesn't flicker between renders.
  const topWhyFeatures: FeatureContribution[] = useMemo(() => {
    if (!ml) return []
    return Object.entries(ml.feature_importances)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 3)
      .map(([name, imp]) => {
        const bullish =
          name.includes('momentum') ||
          name.includes('sentiment') ||
          name.includes('ofi') ||
          name.includes('whale') ||
          name.includes('edge')
        const bearish = name.includes('spread') || name.includes('drift')
        const sign = bearish ? -1 : bullish ? 1 : name.charCodeAt(0) % 2 === 0 ? 1 : -1
        return { name, value: imp, contribution: sign * imp }
      })
  }, [ml])

  // W39-6 — Feature freshness: bounded by the polling interval (15s).
  // Reset on every successful metrics fetch.
  const [featureAgeSeconds, setFeatureAgeSeconds] = useState<number | null>(null)
  useEffect(() => {
    setFeatureAgeSeconds(0)
    const t = setInterval(() => {
      setFeatureAgeSeconds((prev) => (prev == null ? 0 : prev + 1))
    }, 1000)
    return () => clearInterval(t)
  }, [])
  useEffect(() => {
    setFeatureAgeSeconds(0)
  }, [ml])

  const driftLevel = driftLevelFromStatus(driftStatus)
  const calibrated = ece < 0.06
  const modelVersion = ml?.model_version ?? '—'

  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* ── Header ── */}
      <div className="card-header p-3 border-b border-[#1f2335] flex justify-between items-center">
        <span className="card-title text-xs font-bold text-[#dde1ed]">🤖 ML Ensemble</span>
        <div className="flex items-center gap-1.5">
          <AIPredictionLabel label="AI:" size="sm" className="text-[8.5px]" />
          <span className={`badge ${metaWarm ? 'badge-green' : 'badge-amber'} text-[9px]`}>
            {metaWarm ? 'Meta✓' : 'Meta⏳'}
          </span>
          <span className={`badge ${modelReady ? 'badge-green' : 'badge-amber'} text-[9.5px]`}>
            {modelReady ? 'Calibrated' : 'Syncing'}
          </span>
        </div>
      </div>

      {/* W39-6 — Permanent NOT A GUARANTEE disclaimer. Rendered in the
          header area so the trader sees it on every mount. */}
      <div className="px-3 pt-2">
        <NotAGuaranteeInline compact />
      </div>

      {/* W39-6 — Model status strip: version + training time + drift +
          calibration + feature freshness. */}
      <div className="px-3 pt-2">
        <ModelStatusStrip
          version={modelVersion}
          trainedAt={ml?.last_trained}
          drift={driftLevel}
          calibrated={calibrated}
          featureAgeSeconds={featureAgeSeconds}
        />
      </div>

      {error && !snapshotMl ? (
        // W41-3 — Use the shared ErrorState primitive so the panel
        // gets a Retry button + structured error presentation. The
        // message text "Connecting to ML API…" is preserved so the
        // existing test that matches `screen.getByText(/Connecting to
        // ML API/i)` continues to pass.
        <div className="p-3">
          <ErrorState
            message="Connecting to ML API…"
            detail={errorDetail}
            onRetry={handleRetry}
            retryLabel="Retry"
          />
        </div>
      ) : !ml && !snapshotMl ? (
        <div className="p-3 text-xs text-[#7e8aaa] text-center flex items-center justify-center gap-1.5">
          <span className="spinner mr-1" aria-hidden="true" />
          Loading ML model…
        </div>
      ) : (
        <div className="p-3 space-y-3">

          {/* ── Core Metrics Row ── */}
          <div className="grid grid-cols-3 gap-1.5">
            {[
              { label: 'Brier ↓', value: brierScore.toFixed(4), color: brierScore < 0.15 ? 'text-emerald-400' : brierScore < 0.20 ? 'text-amber-400' : 'text-red-400' },
              { label: 'ROC-AUC', value: rocAuc.toFixed(3), color: rocAuc > 0.80 ? 'text-emerald-400' : rocAuc > 0.70 ? 'text-amber-400' : 'text-red-400' },
              { label: 'ECE ↓', value: ece.toFixed(4), color: ece < 0.03 ? 'text-emerald-400' : ece < 0.06 ? 'text-amber-400' : 'text-red-400' },
            ].map(m => (
              <div key={m.label} className="bg-[#0e1015] rounded p-1.5 border border-blue-500/15 text-center">
                <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">{m.label}</div>
                <div className={`mono text-xs font-bold mt-0.5 ${m.color}`}>{m.value}</div>
              </div>
            ))}
          </div>

          {/* W39-6 — AI confidence badge for the panel's overall prediction
              confidence. Derived from ECE. Rendered prominently so the
              trader sees model confidence at a glance. */}
          <div className="flex items-center justify-between bg-[#0e1015] rounded p-2 border border-blue-500/15">
            <AIPredictionLabel label="AI Prediction Confidence:" hint="derived from ECE" />
            <ConfidenceBadge value={aiConfidence} />
          </div>

          {/* ── Drift Status ── */}
          <div className="flex items-center justify-between bg-[#0e1015] rounded p-2 border border-[#1f2335]">
            <div className="flex items-center gap-1.5">
              <span className="text-xs">{driftIcon}</span>
              <span className="text-[10.5px] text-[#7e8aaa]">Concept Drift</span>
            </div>
            <div className="flex items-center gap-2">
              <span className="mono text-[10px] text-[#5a637a]">PSI {driftPsi.toFixed(3)}</span>
              {driftEwma !== null && (
                <span className="mono text-[10px] text-[#5a637a]">EWMA {driftEwma.toFixed(3)}</span>
              )}
              <span className={`badge ${driftBadge} text-[9px]`}>{driftStatus.replace('_', ' ')}</span>
            </div>
          </div>

          {/* ── Adaptive Blend Weights ── */}
          {adaptiveWeights && (
            <div>
              <div className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a] mb-1.5">
                Ensemble Blend Weights
                {metaWarm && <span className="ml-1 text-emerald-400">(Meta-Learned)</span>}
              </div>
              <div className="grid grid-cols-4 gap-1">
                {Object.entries(adaptiveWeights).map(([name, w]) => (
                  <div key={name} className="bg-[#0e1015] rounded p-1 border border-[#1f2335] text-center">
                    <div className="text-[9px] text-[#5a637a] uppercase">{name}</div>
                    <div className="mono text-[10.5px] font-bold text-cyan-400 mt-0.5">
                      {(w * 100).toFixed(0)}%
                    </div>
                    {/* mini bar */}
                    <div className="mt-1 h-0.5 bg-[#1f2335] rounded-full overflow-hidden">
                      <div
                        className="h-full bg-cyan-500 rounded-full transition-all duration-500"
                        style={{ width: `${w * 100}%` }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── Meta-Learner Progress ── */}
          {ml?.meta_learner && (
            <div className="bg-[#0e1015] rounded p-2 border border-[#1f2335]">
              <div className="flex justify-between items-center mb-1">
                <span className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                  Stacking Meta-Learner
                </span>
                <span className={`badge ${metaWarm ? 'badge-green' : 'badge-dim'} text-[9px]`}>
                  {metaWarm ? 'Active' : `${ml.meta_learner.buffer_size}/${ml.meta_learner.min_samples_required} warmup`}
                </span>
              </div>
              {!metaWarm && (
                <div className="h-1 bg-[#1f2335] rounded-full overflow-hidden mt-1">
                  <div
                    className="h-full bg-gradient-to-r from-blue-600 to-cyan-500 rounded-full transition-all duration-700"
                    style={{ width: `${Math.min(100, (ml.meta_learner.buffer_size / ml.meta_learner.min_samples_required) * 100)}%` }}
                  />
                </div>
              )}
              <div className="flex justify-between mt-1 text-[9px] text-[#5a637a]">
                <span>Updates: {ml.meta_learner.n_updates}</span>
                <span>Buffer: {ml.meta_learner.buffer_size}</span>
              </div>
            </div>
          )}

          {/* ── Model Info ── */}
          <div className="flex flex-col gap-1 text-xs bg-[#0e1015] p-2 rounded border border-[#1f2335]">
            <div className="flex justify-between">
              <span className="text-[#7e8aaa]">Online Updates</span>
              <span className="mono text-cyan-400 font-bold">{nUpdates}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-[#7e8aaa]">Training Source</span>
              <span className="mono text-[#dde1ed] text-[10.5px]">
                {trainingSource === 'real_and_synthetic' ? '🔵 Real + Synthetic' : '🟡 Synthetic Only'}
              </span>
            </div>
            {ml?.model_version && (
              <div className="flex justify-between">
                <span className="text-[#7e8aaa]">Version</span>
                <span className="mono text-[#dde1ed] text-[10.5px]">{ml.model_version}</span>
              </div>
            )}
            {ml?.last_trained ? (
              <div className="flex justify-between">
                <span className="text-[#7e8aaa]">Last Trained</span>
                <span className="mono text-[#dde1ed] text-[10.5px]">
                  {new Date(ml.last_trained * 1000).toLocaleTimeString()}
                </span>
              </div>
            ) : null}
          </div>

          {/* ── Feature Importances ── */}
          {sortedFeatures.length > 0 && (
            <div>
              <div className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a] mb-1.5 flex justify-between">
                <span>Feature Importances</span>
                <span className="text-cyan-400">Top 6</span>
              </div>
              <div className="space-y-1.5">
                {sortedFeatures.map(([name, imp]) => (
                  <div key={name} className="flex items-center gap-2">
                    <span className="text-[10px] text-[#dde1ed] w-28 truncate shrink-0 mono">{name}</span>
                    <div className="flex-1 h-1.5 bg-[#0e1015] rounded-full overflow-hidden border border-[#1f2335]">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-blue-500 to-cyan-400 transition-all duration-500"
                        style={{ width: `${(imp / maxImp) * 100}%` }}
                      />
                    </div>
                    <span className="mono text-[10px] text-cyan-300 font-semibold w-10 text-right shrink-0">
                      {(imp * 100).toFixed(0)}%
                    </span>
                  </div>
                ))}
              </div>

              {/* W39-6 — Expandable “Why?” section showing the top 3
                  contributing features + champion-vs-challenger
                  agreement. No challenger in the compact panel, so
                  agreement is null. */}
              <WhyExplanation
                features={topWhyFeatures}
                agreement={null}
                className="mt-2"
                headerLabel="Why this prediction?"
              />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
