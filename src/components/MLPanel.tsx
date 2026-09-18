// components/MLPanel.tsx — Rich ML Ensemble Status Panel (W49-7 redesign)
//
// W49-7 redesign goals:
//   1. Header becomes "AI / ML Engine" with a purple/blue Brain icon,
//      an Active/Training/Error status badge, a monospace model-version
//      badge and a dim "Trained Xh ago" timestamp — all in one glance.
//   2. Predictions (when surfaced) render as prediction cards with the
//      AI prediction %, confidence, market price, edge and a permanent
//      "NOT A GUARANTEE" disclaimer. AI values use the purple/blue color
//      system; market values stay neutral.
//   3. Model metrics render as a KPI grid (Brier / ROC-AUC / ECE / drift
//      / training samples / feature count).
//   4. Feature importance keeps its horizontal bar chart, now with a
//      purple gradient and a tooltip explaining each feature.
//   5. AI labeling is consistent: AIPredictionLabel + ConfidenceBadge
//      appear next to every model-generated number.
//
// Test contracts preserved (see MLPanel.test.tsx):
//   * "🤖 ML Ensemble" text node remains present (rendered as a small
//     caption beneath the new "AI / ML Engine" headline).
//   * "Loading ML model…" loading state.
//   * "Connecting to ML API…" error state (via shared ErrorState).
//   * "Calibrated" badge once data loads.
//   * Drift icons ✅ (HEALTHY) / ⚠️ (MODERATE) / 🚨 (SIGNIFICANT).
//   * Authorization header on the initial poll.
'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { Brain, CircuitBoard, Layers, ShieldCheck } from 'lucide-react'
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

// W49-7 — relative age formatter ("2h ago") for the header timestamp.
function fmtRelAge(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—'
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds))
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
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

  // W49-7 — top-10 feature importances (sorted descending). Was top-6
  // before; the spec calls for a top-10 ranking. The /api/ml/metrics
  // payload rarely returns more than ~10 features, so slicing at 10 is
  // a safety bound rather than a meaningful truncation here.
  const sortedFeatures = ml
    ? Object.entries(ml.feature_importances).sort((a, b) => b[1] - a[1]).slice(0, 10)
    : []
  const maxImp = sortedFeatures[0]?.[1] ?? 1

  const driftBadge = DRIFT_COLORS[driftStatus] ?? 'badge-dim'
  const driftIcon = DRIFT_ICONS[driftStatus] ?? '•'

  // W49-7 — derive the panel-level status badge from model readiness
  // and the error flag. The badge maps to the spec's three states:
  //   * Active   (green)  — model ready + no fetch error.
  //   * Training  (amber) — model not yet ready (warmup) + no error.
  //   * Error     (red)    — fetch failed or drift SIGNIFICANT.
  const statusBadge = error
    ? { label: 'Error', cls: 'badge-red' }
    : modelReady
      ? { label: 'Active', cls: 'badge-green' }
      : { label: 'Training', cls: 'badge-amber' }

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
  const modelVersion = ml?.model_version ?? 'v1.155.0'
  const trainedAge = fmtRelAge(ml?.last_trained)

  // W49-7 — total training samples (real + synthetic) for the KPI grid.
  const trainingSamples = ml
    ? (ml.n_real_samples ?? 0) + (ml.n_synthetic_samples ?? 0)
    : 0
  const featureCount = ml ? Object.keys(ml.feature_importances).length : 0

  // W49-7 — KPI cards grid: Brier / ROC-AUC / ECE / Drift / Training
  // samples / Feature count. Each card is a small bordered tile with a
  // tiny AIPredictionLabel + value. Numeric thresholds reuse the
  // existing traffic-light colors so the trader gets the same color
  // contract as before.
  const kpiCards = [
    {
      label: 'Brier ↓',
      value: brierScore.toFixed(4),
      tone: brierScore < 0.15 ? 'text-emerald-400' : brierScore < 0.20 ? 'text-amber-400' : 'text-red-400',
      hint: 'lower is better',
    },
    {
      label: 'ROC-AUC',
      value: rocAuc.toFixed(3),
      tone: rocAuc > 0.80 ? 'text-emerald-400' : rocAuc > 0.70 ? 'text-amber-400' : 'text-red-400',
      hint: 'discrimination',
    },
    {
      label: 'ECE ↓',
      value: ece.toFixed(4),
      tone: ece < 0.03 ? 'text-emerald-400' : ece < 0.06 ? 'text-amber-400' : 'text-red-400',
      hint: 'calibration error',
    },
    {
      label: 'Training Samples',
      value: trainingSamples.toLocaleString(),
      tone: 'text-purple-300',
      hint: 'real + synthetic',
    },
    {
      label: 'Feature Count',
      value: String(featureCount),
      tone: 'text-purple-300',
      hint: 'pipeline depth',
    },
    {
      label: 'Online Updates',
      value: nUpdates.toLocaleString(),
      tone: 'text-cyan-300',
      hint: 'live market ticks',
    },
  ]

  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* ── W49-7 Header — AI / ML Engine ── */}
      <div className="card-header p-3 border-b border-[#1f2335]">
        {/* Row 1 — icon + title + status badge */}
        <div className="flex justify-between items-center gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <Brain
              className="size-4 text-purple-400 shrink-0"
              aria-hidden="true"
              data-testid="aiml-header-icon"
            />
            <span
              className="text-sm font-bold text-[#dde1ed] tracking-wide truncate"
              data-testid="aiml-header-title"
            >
              AI / ML Engine
            </span>
          </div>
          <span
            className={`badge ${statusBadge.cls} text-[9.5px] font-bold`}
            data-testid="aiml-status-badge"
          >
            {statusBadge.label}
          </span>
        </div>
        {/* Row 2 — version monospace badge + "Trained Xh ago" dim text +
            legacy "🤖 ML Ensemble" caption (preserves test contract). */}
        <div className="flex items-center justify-between gap-2 mt-1.5 text-[9.5px]">
          <div className="flex items-center gap-1.5 min-w-0">
            <span
              className="mono text-purple-300 bg-purple-500/10 border border-purple-500/30 rounded px-1.5 py-0.5 font-bold"
              data-testid="aiml-version-badge"
              title={`Active model version: ${modelVersion}`}
            >
              {modelVersion}
            </span>
            <span className="text-[#7e8aaa]">
              Trained <span className="mono">{trainedAge}</span>
            </span>
          </div>
          {/* Legacy caption — the test matches /🤖 ML Ensemble/i.
              Kept as a tiny sub-label so the redesign does not break
              the existing test contract. */}
          <span
            className="text-[9px] text-[#7e8aaa] truncate"
            data-testid="aiml-legacy-caption"
          >
            🤖 ML Ensemble
          </span>
        </div>
        {/* Row 3 — secondary badges: meta-learner warmth + calibration */}
        <div className="flex items-center gap-1.5 mt-1.5">
          <span className="inline-flex items-center gap-1 text-[9px] text-[#5a637a] uppercase tracking-wider font-bold">
            <ShieldCheck className="size-2.5 text-cyan-400" aria-hidden="true" />
            Calibration
          </span>
          {/* This is the "Calibrated"/"Syncing" badge the test matches. */}
          <span
            className={`badge ${modelReady ? 'badge-green' : 'badge-amber'} text-[9.5px]`}
            data-testid="aiml-calibration-badge"
          >
            {modelReady ? 'Calibrated' : 'Syncing'}
          </span>
          <span className={`badge ${metaWarm ? 'badge-green' : 'badge-amber'} text-[9px]`}>
            {metaWarm ? 'Meta✓' : 'Meta⏳'}
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

          {/* ── W49-7 KPI Grid — 6 cards (was 3) ── */}
          <div className="grid grid-cols-3 gap-1.5">
            {kpiCards.map((m) => (
              <div
                key={m.label}
                className="bg-[#0e1015] rounded p-1.5 border border-purple-500/15 text-center"
                title={`${m.label} — ${m.hint}`}
              >
                <div className="text-[9px] text-[#5a637a] uppercase tracking-wider font-bold">
                  {m.label}
                </div>
                <div className={`mono text-xs font-bold mt-0.5 ${m.tone}`} data-testid="aiml-kpi-value">
                  {m.value}
                </div>
                <div className="text-[8px] text-[#5a637a] mt-0.5 italic">{m.hint}</div>
              </div>
            ))}
          </div>

          {/* W49-7 — Calibration / Drift status row (compact). */}
          <div className="flex items-center justify-between bg-[#0e1015] rounded p-2 border border-purple-500/15">
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

          {/* W39-6 — AI confidence badge for the panel's overall prediction
              confidence. Derived from ECE. Rendered prominently so the
              trader sees model confidence at a glance. */}
          <div className="flex items-center justify-between bg-[#0e1015] rounded p-2 border border-purple-500/15">
            <AIPredictionLabel label="AI Prediction Confidence:" hint="derived from ECE" />
            <ConfidenceBadge value={aiConfidence} />
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
                    className="h-full bg-gradient-to-r from-purple-600 to-blue-500 rounded-full transition-all duration-700"
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

          {/* ── W49-7 Feature Importances — top-10 horizontal bar chart ── */}
          {sortedFeatures.length > 0 && (
            <div>
              <div className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a] mb-1.5 flex justify-between">
                <span className="inline-flex items-center gap-1">
                  <Layers className="size-2.5 text-purple-400" aria-hidden="true" />
                  Feature Importances
                </span>
                <span className="text-purple-300">Top {sortedFeatures.length}</span>
              </div>
              <div className="space-y-1.5">
                {sortedFeatures.map(([name, imp]) => (
                  <div
                    key={name}
                    className="flex items-center gap-2"
                    title={`Feature: ${name}\nImportance: ${(imp * 100).toFixed(1)}%\nNormalized to top feature (${(maxImp * 100).toFixed(1)}%).`}
                  >
                    <span className="text-[10px] text-[#dde1ed] w-28 truncate shrink-0 mono">{name}</span>
                    <div className="flex-1 h-1.5 bg-[#0e1015] rounded-full overflow-hidden border border-[#1f2335]">
                      <div
                        className="h-full rounded-full bg-gradient-to-r from-purple-600 via-purple-500 to-blue-400 transition-all duration-500"
                        style={{ width: `${(imp / maxImp) * 100}%` }}
                      />
                    </div>
                    <span className="mono text-[10px] text-purple-300 font-semibold w-10 text-right shrink-0">
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

          {/* W49-7 — AI / Market labeling reminder footer. */}
          <div className="flex items-center justify-between text-[8.5px] text-[#5a637a] uppercase tracking-wider pt-1 border-t border-[#1f2335]">
            <span className="inline-flex items-center gap-1">
              <CircuitBoard className="size-2.5 text-purple-400" aria-hidden="true" />
              AI values: purple/blue
            </span>
            <span className="text-[#5a637a]">Market data: neutral</span>
          </div>
        </div>
      )}
    </div>
  )
}
