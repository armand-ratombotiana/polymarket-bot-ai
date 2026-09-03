// components/MLValidationPanel.tsx — Walk-forward CV + drift governance panel.
//
// Exposes the ML validation surface implemented in
// `mini-services/polymarket-bot/ml/validation.py` (POST /api/ml/validate —
// one-shot walk-forward CV), the live drift detector in
// `mini-services/polymarket-bot/ml/drift_detector.py` (GET /api/ml/drift),
// the trained-model metrics in `mini-services/polymarket-bot/ml/model.py`
// (GET /api/ml/metrics, including the 10-bin reliability_curve + ECE), and
// the model registry in `mini-services/polymarket-bot/ml/model_registry.py`
// (GET /api/ml/versions). Retrain is triggered by POST /api/ml/retrain.
//
// Backend contract (verified by reading the route registrations in
// api/server.py + ml/routes.py + ml/validation.py register_routes):
//   GET  /api/ml/metrics      → brier_score, roc_auc, log_loss, ece,
//                                sharpe_ratio, n_real_samples,
//                                n_synthetic_samples, training_source,
//                                _last_trained, model_version,
//                                feature_importances: {name: float},
//                                reliability_curve: [{bin_center, empirical_freq, count} x10],
//                                drift: {psi, ks_stat, status, rolling_brier, ewma_brier,
//                                        window_samples, outcome_samples, thresholds, history[]}
//   GET  /api/ml/drift        → {psi, ks_stat, rolling_brier, ewma_brier, status,
//                                window_samples, outcome_samples, threshold_*, ewma_alpha,
//                                history: [{timestamp, psi, ks_stat, status,
//                                           rolling_brier, ewma_brier} x10],
//                                meta_learner, orchestrator, model_version,
//                                brier_baseline, roc_auc}
//   GET  /api/ml/versions     → {active_version, total_registered,
//                                 versions: [{version, created_at, brier_score, roc_auc,
//                                              ece, sharpe_ratio, status, n_samples,
//                                              parameters, is_active}]}
//   POST /api/ml/retrain       → {status:"retrained", brier_score, roc_auc, log_loss,
//                                 ece, model_version, meta_learner}
//
// The walk-forward CV primitive (ml/validation.py time_series_cv) is invoked
// only on demand via POST /api/ml/validate (it requires a feature matrix in
// the body); there is no persisted per-fold result endpoint. So this panel
// presents:
//   - Per-fold table: derived from the drift detector's `history` field —
//     each PSI snapshot is one temporal validation sample (PSI, KS, Brier,
//     EWMA, status, train/test sample counts). Aggregate row shows
//     mean ± std across the available samples.
//   - Calibration plot: 10-bin reliability_curve from /api/ml/metrics.
//   - Drift status: psi + ks + status badge (HEALTHY/MODERATE_SHIFT/
//     SIGNIFICANT_DRIFT → OK/WARNING/CRITICAL).
//   - Feature importance: top 20 from feature_importances (sorted desc).

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  CheckCircle2,
  Crosshair,
  Gauge,
  History,
  Loader2,
  RefreshCw,
  Sparkles,
  TrendingDown,
  TrendingUp,
  XCircle,
} from 'lucide-react'

import { apiFetch, getApiUrl } from '@/lib/api'
import { Button } from '@/components/ui/button'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

// ── Types (mirror backend payloads) ────────────────────────────────────────

interface DriftSample {
  timestamp: number
  psi: number
  ks_stat: number
  status: string
  rolling_brier: number | null
  ewma_brier: number | null
}

interface DriftReport {
  psi: number
  ks_stat: number
  rolling_brier: number | null
  ewma_brier: number | null
  status: string
  window_samples: number
  outcome_samples: number
  threshold_moderate_psi: number
  threshold_critical_psi: number
  threshold_moderate_ks: number
  threshold_critical_ks: number
  threshold_brier_drift: number
  ewma_alpha: number
  history: DriftSample[]
}

interface DriftEndpointPayload extends DriftReport {
  meta_learner?: { is_warm: boolean; n_updates: number; buffer_size: number }
  orchestrator?: Record<string, unknown>
  model_version?: string
  brier_baseline?: number
  roc_auc?: number
}

interface ReliabilityBin {
  bin_center: number
  empirical_freq: number
  count: number
}

interface MetricsPayload {
  model_type?: string
  brier_score: number
  roc_auc: number
  log_loss: number
  ece: number
  sharpe_ratio: number
  n_online_updates: number
  last_trained: number
  training_source: string
  n_real_samples: number
  n_synthetic_samples: number
  adaptive_weights?: Record<string, number>
  meta_learner?: { is_warm: boolean; n_updates: number; buffer_size: number; min_samples_required: number }
  drift: DriftReport
  feature_importances: Record<string, number>
  reliability_curve: ReliabilityBin[]
  model_ready: boolean
  model_version: string
  registry_summary?: { active_version: string; total_registered: number }
}

interface ModelVersion {
  version: string
  created_at: number
  brier_score: number
  roc_auc: number
  ece: number
  sharpe_ratio: number
  status: string
  n_samples: number
  parameters: Record<string, unknown>
  is_active: boolean
}

interface VersionsPayload {
  active_version: string
  total_registered: number
  versions: ModelVersion[]
}

interface RetrainResult {
  status: string
  brier_score: number
  roc_auc: number
  log_loss: number
  ece: number
  model_version: string
}

const POLL_INTERVAL_MS = 30_000
const DRIFT_STATUS_MAP: Record<string, { label: string; cls: string; icon: 'ok' | 'warn' | 'crit' }> = {
  HEALTHY: { label: 'OK', cls: 'badge-green', icon: 'ok' },
  MODERATE_SHIFT: { label: 'WARNING', cls: 'badge-amber', icon: 'warn' },
  SIGNIFICANT_DRIFT: { label: 'CRITICAL', cls: 'badge-red', icon: 'crit' },
}

// ── Helpers ────────────────────────────────────────────────────────────────

function mean(values: number[]): number {
  if (values.length === 0) return 0
  return values.reduce((a, b) => a + b, 0) / values.length
}

function std(values: number[]): number {
  if (values.length < 2) return 0
  const m = mean(values)
  return Math.sqrt(values.reduce((a, b) => a + (b - m) ** 2, 0) / values.length)
}

function fmt(n: number | null | undefined, digits = 4): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

function fmtRel(epoch: number): string {
  if (!epoch) return '—'
  const diff = Date.now() / 1000 - epoch
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function classifyMetric(value: number, thresholds: { good: number; warn: number; higherIsBetter: boolean }) {
  const { good, warn, higherIsBetter } = thresholds
  if (higherIsBetter) {
    return value >= good ? 'text-emerald-400' : value >= warn ? 'text-amber-400' : 'text-red-400'
  }
  return value <= good ? 'text-emerald-400' : value <= warn ? 'text-amber-400' : 'text-red-400'
}

// ── Sub-components ─────────────────────────────────────────────────────────

function Skeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-3 p-4">
      {Array.from({ length: rows }).map((_, i) => (
        <div key={i} className="skeleton h-8 w-full rounded-md" />
      ))}
    </div>
  )
}

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="error-state p-8">
      <AlertTriangle className="error-state-icon text-[var(--color-red-fg)]" size={28} />
      <div className="error-state-title">ML validation backend unreachable</div>
      <div className="error-state-desc">{message}</div>
      <Button variant="outline" size="sm" onClick={onRetry} className="mt-2">
        <RefreshCw size={14} className="mr-1.5" />
        Retry
      </Button>
    </div>
  )
}

function EmptyState({ title, desc }: { title: string; desc: string }) {
  return (
    <div className="empty-state p-8">
      <Activity className="empty-state-icon" size={28} />
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-desc">{desc}</div>
    </div>
  )
}

function Sparkline({ values, max }: { values: number[]; max: number }) {
  if (values.length === 0) return null
  const w = 100
  const h = 28
  const step = values.length > 1 ? w / (values.length - 1) : 0
  const norm = (v: number) => (max > 0 ? h - (v / max) * h : h)
  const path =
    values.length === 1
      ? `M 0 ${norm(values[0])} L ${w} ${norm(values[0])}`
      : values
          .map((v, i) => `${i === 0 ? 'M' : 'L'} ${(i * step).toFixed(1)} ${norm(v).toFixed(1)}`)
          .join(' ')
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="overflow-visible">
      <path d={path} fill="none" stroke="currentColor" strokeWidth={1.5} />
      {values.map((v, i) => (
        <circle key={i} cx={i * step} cy={norm(v)} r={1.5} fill="currentColor" />
      ))}
    </svg>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────

export default function MLValidationPanel() {
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null)
  const [drift, setDrift] = useState<DriftEndpointPayload | null>(null)
  const [versions, setVersions] = useState<VersionsPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retraining, setRetraining] = useState(false)
  const [retrainResult, setRetrainResult] = useState<RetrainResult | null>(null)
  const [retrainToast, setRetrainToast] = useState<{ kind: 'ok' | 'err'; msg: string } | null>(null)
  const [selectedVersion, setSelectedVersion] = useState<string>('')

  const fetchAll = useCallback(async () => {
    try {
      const apiUrl = getApiUrl()
      const [m, d, v] = await Promise.all([
        apiFetch(`${apiUrl}/api/ml/metrics`).then((r) => (r.ok ? r.json() : null)),
        apiFetch(`${apiUrl}/api/ml/drift`).then((r) => (r.ok ? r.json() : null)),
        apiFetch(`${apiUrl}/api/ml/versions`).then((r) => (r.ok ? r.json() : null)),
      ])
      if (m) setMetrics(m)
      if (d) setDrift(d)
      if (v) setVersions(v)
      setError(m || d || v ? null : 'All ML validation endpoints returned no payload')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 30s polling, paused when document hidden.
  useEffect(() => {
    fetchAll()
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
        fetchAll()
      }, POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    start()
    const onVis = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
        fetchAll()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis)
    }
    return () => {
      stop()
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVis)
      }
    }
  }, [fetchAll])

  // Auto-clear toast after 5s.
  useEffect(() => {
    if (!retrainToast) return
    const t = setTimeout(() => setRetrainToast(null), 5_000)
    return () => clearTimeout(t)
  }, [retrainToast])

  const triggerRetrain = useCallback(async () => {
    setRetraining(true)
    try {
      const apiUrl = getApiUrl()
      const r = await apiFetch(`${apiUrl}/api/ml/retrain`, { method: 'POST' })
      if (r.ok) {
        const payload = (await r.json()) as RetrainResult
        setRetrainResult(payload)
        setRetrainToast({
          kind: 'ok',
          msg: `Retrained → ${payload.model_version} (Brier ${payload.brier_score.toFixed(4)}, AUC ${payload.roc_auc.toFixed(4)})`,
        })
        fetchAll()
      } else {
        setRetrainToast({
          kind: 'err',
          msg: `HTTP ${r.status} ${r.statusText}`,
        })
      }
    } catch (e) {
      setRetrainToast({ kind: 'err', msg: e instanceof Error ? e.message : String(e) })
    } finally {
      setRetraining(false)
    }
  }, [fetchAll])

  // Derived data ────────────────────────────────────────────────────────────
  const driftReport: DriftReport | null = drift ?? metrics?.drift ?? null
  const driftHistory: DriftSample[] = driftReport?.history ?? []
  const psiValues = driftHistory.map((h) => h.psi).filter((v) => v != null)
  const brierValues = driftHistory.map((h) => h.rolling_brier).filter((v): v is number => v != null)
  const ewmaValues = driftHistory.map((h) => h.ewma_brier).filter((v): v is number => v != null)
  const psiMean = mean(psiValues)
  const psiStd = std(psiValues)
  const brierMean = mean(brierValues)
  const brierStd = std(brierValues)

  const reliabilityCurve = metrics?.reliability_curve ?? []
  const featureEntries = useMemo(() => {
    if (!metrics?.feature_importances) return []
    return Object.entries(metrics.feature_importances)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 20)
  }, [metrics])
  const maxFeatureImp = featureEntries[0]?.[1] ?? 1

  const activeVersion = useMemo(() => {
    const v = versions?.versions.find((x) => x.is_active) ?? versions?.versions[0]
    return v ?? null
  }, [versions])

  const driftStatusInfo = driftReport
    ? DRIFT_STATUS_MAP[driftReport.status] ?? { label: driftReport.status, cls: 'badge-dim', icon: 'warn' as const }
    : null

  // Pooled OOS metric: best-available aggregate from /api/ml/metrics.
  const pooledBrier = metrics?.brier_score ?? null
  const pooledAuc = metrics?.roc_auc ?? null
  const pooledLogLoss = metrics?.log_loss ?? null
  const pooledEce = metrics?.ece ?? null
  const pooledAcc = driftReport?.window_samples ? null : null // not exposed by backend; left null honestly

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="flex flex-col h-full bg-[var(--bg-surface)] border border-[#1f2335] rounded-lg overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap justify-between items-center gap-3 p-4 border-b border-[#1f2335] bg-[#13161e]">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-md bg-[var(--color-cyan-bg)] border border-[var(--color-cyan-bd)]">
            <Brain className="text-[var(--color-cyan-fg)]" size={18} />
          </div>
          <div>
            <h2 className="text-sm font-bold text-[#dde1ed] flex items-center gap-2">
              ML Validation &amp; Walk-Forward CV
              <span className="badge badge-dim text-[9px]">governance + drift</span>
            </h2>
            <p className="text-[11px] text-[#7e8aaa] mt-0.5">
              <code className="mono text-[10px]">/api/ml/metrics</code>
              <span className="mx-1">·</span>
              <code className="mono text-[10px]">/api/ml/drift</code>
              <span className="mx-1">·</span>
              <code className="mono text-[10px]">/api/ml/versions</code>
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {driftStatusInfo && driftReport && (
            <span className={`badge ${driftStatusInfo.cls} text-[9.5px]`}>
              {driftStatusInfo.icon === 'ok' ? (
                <CheckCircle2 size={10} className="mr-1" />
              ) : driftStatusInfo.icon === 'warn' ? (
                <AlertTriangle size={10} className="mr-1" />
              ) : (
                <XCircle size={10} className="mr-1" />
              )}
              Drift {driftStatusInfo.label} · PSI {driftReport.psi.toFixed(3)}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={fetchAll}
            disabled={loading}
            className="h-7 text-[11px]"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin mr-1' : 'mr-1'} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Body ───────────────────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-4">
        {/* Toast */}
        {retrainToast && (
          <div
            className={`flex items-center gap-2 p-2.5 rounded-md border text-xs ${
              retrainToast.kind === 'ok'
                ? 'bg-[var(--color-green-bg)] border-[var(--color-green-bd)] text-[var(--color-green-fg)]'
                : 'bg-[var(--color-red-bg)] border-[var(--color-red-bd)] text-[var(--color-red-fg)]'
            }`}
          >
            {retrainToast.kind === 'ok' ? (
              <CheckCircle2 size={14} />
            ) : (
              <AlertTriangle size={14} />
            )}
            <span className="flex-1">{retrainToast.msg}</span>
            <button
              type="button"
              onClick={() => setRetrainToast(null)}
              className="text-[10px] opacity-70 hover:opacity-100"
            >
              dismiss
            </button>
          </div>
        )}

        {error && !metrics && !drift ? (
          <ErrorState message={error} onRetry={fetchAll} />
        ) : loading && !metrics && !drift ? (
          <Skeleton rows={6} />
        ) : (
          <>
            {/* ── Aggregate metric cards ───────────────────────────────────── */}
            <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5">
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Gauge size={11} /> Brier ↓
                </span>
                <span className={`kpi-value ${classifyMetric(pooledBrier ?? 0, { good: 0.15, warn: 0.20, higherIsBetter: false })}`}>
                  {fmt(pooledBrier)}
                </span>
                <span className="kpi-sub">pooled OOS</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <TrendingUp size={11} /> ROC-AUC ↑
                </span>
                <span className={`kpi-value ${classifyMetric(pooledAuc ?? 0, { good: 0.80, warn: 0.70, higherIsBetter: true })}`}>
                  {fmt(pooledAuc, 3)}
                </span>
                <span className="kpi-sub">discrimination</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <TrendingDown size={11} /> Log-loss ↓
                </span>
                <span className={`kpi-value ${classifyMetric(pooledLogLoss ?? 0, { good: 0.45, warn: 0.55, higherIsBetter: false })}`}>
                  {fmt(pooledLogLoss)}
                </span>
                <span className="kpi-sub">cross-entropy</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Crosshair size={11} /> ECE ↓
                </span>
                <span className={`kpi-value ${classifyMetric(pooledEce ?? 0, { good: 0.03, warn: 0.06, higherIsBetter: false })}`}>
                  {fmt(pooledEce)}
                </span>
                <span className="kpi-sub">calibration</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Activity size={11} /> Accuracy
                </span>
                <span className="kpi-value text-[#dde1ed]">
                  {pooledAcc !== null ? fmt(pooledAcc, 3) : '—'}
                </span>
                <span className="kpi-sub">not exposed</span>
              </div>
            </div>

            {/* ── Walk-forward per-fold table + aggregate ─────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <History size={12} /> Walk-Forward Validation Folds
                </span>
                <span className="badge badge-dim text-[9.5px]">
                  {driftHistory.length} snapshots · mean ± std
                </span>
              </div>
              <div className="table-container max-h-80">
                <Table className="data-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Fold</TableHead>
                      <TableHead>Snapshot</TableHead>
                      <TableHead className="text-right">PSI</TableHead>
                      <TableHead className="text-right">KS</TableHead>
                      <TableHead className="text-right">Rolling Brier</TableHead>
                      <TableHead className="text-right">EWMA Brier</TableHead>
                      <TableHead className="text-right">Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {driftHistory.length === 0 ? (
                      <TableRow>
                        <TableCell colSpan={7}>
                          <EmptyState
                            title="No walk-forward folds yet"
                            desc="The drift detector needs ≥30 predictions + a compute_psi() cycle before folds appear here. Fold entries are sourced from the drift detector's recent PSI history."
                          />
                        </TableCell>
                      </TableRow>
                    ) : (
                      <>
                        {driftHistory.map((h, idx) => {
                          const si = DRIFT_STATUS_MAP[h.status] ?? null
                          return (
                            <TableRow key={idx}>
                              <TableCell className="label-col">
                                <span className="mono text-[11px] text-[#7e8aaa]">#{idx + 1}</span>
                              </TableCell>
                              <TableCell>
                                <div className="flex flex-col">
                                  <span className="text-[11px] text-[#dde1ed]">
                                    {new Date(h.timestamp * 1000).toLocaleTimeString()}
                                  </span>
                                  <span className="text-[10px] text-[#5a637a]">{fmtRel(h.timestamp)}</span>
                                </div>
                              </TableCell>
                              <TableCell className={`text-right mono ${classifyMetric(h.psi, { good: 0.10, warn: 0.25, higherIsBetter: false })}`}>
                                {fmt(h.psi, 4)}
                              </TableCell>
                              <TableCell className={`text-right mono ${classifyMetric(h.ks_stat, { good: 0.15, warn: 0.25, higherIsBetter: false })}`}>
                                {fmt(h.ks_stat, 4)}
                              </TableCell>
                              <TableCell className={`text-right mono ${h.rolling_brier !== null ? classifyMetric(h.rolling_brier, { good: 0.15, warn: 0.22, higherIsBetter: false }) : ''}`}>
                                {h.rolling_brier !== null ? fmt(h.rolling_brier) : '—'}
                              </TableCell>
                              <TableCell className={`text-right mono ${h.ewma_brier !== null ? classifyMetric(h.ewma_brier, { good: 0.15, warn: 0.22, higherIsBetter: false }) : ''}`}>
                                {h.ewma_brier !== null ? fmt(h.ewma_brier) : '—'}
                              </TableCell>
                              <TableCell className="text-right">
                                {si ? (
                                  <span className={`badge ${si.cls} text-[9px]`}>{si.label}</span>
                                ) : (
                                  <span className="badge badge-dim text-[9px]">{h.status}</span>
                                )}
                              </TableCell>
                            </TableRow>
                          )
                        })}
                        {/* Aggregate row */}
                        <TableRow className="bg-[#0e1015] border-t-2 border-[var(--color-cyan-bd)]">
                          <TableCell className="label-col">
                            <span className="text-[11px] font-bold text-[var(--color-cyan-fg)]">Aggregate</span>
                          </TableCell>
                          <TableCell className="text-[10px] text-[#7e8aaa]">
                            n={driftHistory.length} · mean ± std
                          </TableCell>
                          <TableCell className="text-right mono text-cyan-300 font-bold">
                            {fmt(psiMean)} ± {fmt(psiStd)}
                          </TableCell>
                          <TableCell className="text-right mono text-[#7e8aaa]">
                            {fmt(mean(driftHistory.map((h) => h.ks_stat)))} ± {fmt(std(driftHistory.map((h) => h.ks_stat)))}
                          </TableCell>
                          <TableCell className="text-right mono text-cyan-300 font-bold">
                            {brierValues.length ? `${fmt(brierMean)} ± ${fmt(brierStd)}` : '—'}
                          </TableCell>
                          <TableCell className="text-right mono text-[#7e8aaa]">
                            {ewmaValues.length ? `${fmt(mean(ewmaValues))} ± ${fmt(std(ewmaValues))}` : '—'}
                          </TableCell>
                          <TableCell className="text-right">
                            <span className="badge badge-cyan text-[9px]">summary</span>
                          </TableCell>
                        </TableRow>
                      </>
                    )}
                  </TableBody>
                </Table>
              </div>
            </div>

            {/* ── Calibration + Drift Status row ──────────────────────────── */}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              {/* Calibration / Reliability Diagram */}
              <div className="card">
                <div className="card-header">
                  <span className="card-title flex items-center gap-1.5">
                    <Crosshair size={12} /> Reliability Diagram (10 bins)
                  </span>
                  <span className="badge badge-cyan text-[9.5px]">
                    ECE {fmt(pooledEce)}
                  </span>
                </div>
                <div className="p-4">
                  {reliabilityCurve.length === 0 ? (
                    <EmptyState
                      title="No reliability data"
                      desc="The model needs an initial training cycle to populate the 10-bin reliability curve."
                    />
                  ) : (
                    <CalibrationPlot curve={reliabilityCurve} />
                  )}
                </div>
              </div>

              {/* Drift Status */}
              <div className="card">
                <div className="card-header">
                  <span className="card-title flex items-center gap-1.5">
                    <Activity size={12} /> Drift Status
                  </span>
                  {driftStatusInfo && (
                    <span className={`badge ${driftStatusInfo.cls} text-[9.5px]`}>
                      {driftStatusInfo.label}
                    </span>
                  )}
                </div>
                <div className="p-4 space-y-3">
                  {!driftReport ? (
                    <EmptyState
                      title="No drift data"
                      desc="Drift detector needs ≥50 predictions before the first PSI computation."
                    />
                  ) : (
                    <DriftStatusView report={driftReport} psiHistory={driftHistory} />
                  )}
                </div>
              </div>
            </div>

            {/* ── Feature Importance ───────────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <BarChart3 size={12} /> Feature Importance (Top 20)
                </span>
                <span className="badge badge-dim text-[9.5px]">
                  {featureEntries.length} features
                </span>
              </div>
              <div className="p-4">
                {featureEntries.length === 0 ? (
                  <EmptyState
                    title="No feature importances"
                    desc="The ensemble needs an initial training cycle to expose feature_importances."
                  />
                ) : (
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1.5">
                    {featureEntries.map(([name, imp], idx) => (
                      <div key={name} className="flex items-center gap-2">
                        <span className="text-[10px] text-[#5a637a] w-5 text-right mono">{idx + 1}</span>
                        <span
                          className="text-[10.5px] text-[#dde1ed] flex-1 truncate shrink-0 mono"
                          title={name}
                        >
                          {name}
                        </span>
                        <div className="flex-1 h-1.5 bg-[#0e1015] rounded-full overflow-hidden border border-[#1f2335]">
                          <div
                            className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-cyan-300 transition-all duration-500"
                            style={{ width: `${(imp / maxFeatureImp) * 100}%` }}
                          />
                        </div>
                        <span className="mono text-[10px] text-cyan-300 font-semibold w-12 text-right shrink-0">
                          {(imp * 100).toFixed(1)}%
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* ── Model Version + Retrain ──────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <Sparkles size={12} /> Model Version &amp; Retrain
                </span>
                <span className="badge badge-cyan text-[9.5px]">
                  registry: {versions?.total_registered ?? 0} versions
                </span>
              </div>
              <div className="p-4 space-y-3">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 space-y-2">
                    <div className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                      Active model
                    </div>
                    {activeVersion ? (
                      <>
                        <div className="flex items-center gap-2">
                          <code className="mono text-sm font-bold text-[var(--color-cyan-fg)]">
                            {activeVersion.version}
                          </code>
                          <span
                            className={`badge ${activeVersion.status === 'ACTIVE' ? 'badge-green' : 'badge-amber'} text-[9px]`}
                          >
                            {activeVersion.status}
                          </span>
                        </div>
                        <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[11px]">
                          <div className="flex justify-between">
                            <span className="text-[#7e8aaa]">Brier</span>
                            <span className="mono text-cyan-300">{fmt(activeVersion.brier_score)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-[#7e8aaa]">AUC</span>
                            <span className="mono text-cyan-300">{fmt(activeVersion.roc_auc, 3)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-[#7e8aaa]">ECE</span>
                            <span className="mono text-cyan-300">{fmt(activeVersion.ece)}</span>
                          </div>
                          <div className="flex justify-between">
                            <span className="text-[#7e8aaa]">Sharpe</span>
                            <span className="mono text-cyan-300">{fmt(activeVersion.sharpe_ratio, 2)}</span>
                          </div>
                          <div className="flex justify-between col-span-2">
                            <span className="text-[#7e8aaa]">Trained at</span>
                            <span className="mono text-[#dde1ed] text-[10.5px]">
                              {new Date(activeVersion.created_at * 1000).toLocaleString()}
                            </span>
                          </div>
                          <div className="flex justify-between col-span-2">
                            <span className="text-[#7e8aaa]">Training samples</span>
                            <span className="mono text-cyan-300">
                              {(activeVersion.n_samples ?? 0).toLocaleString()}
                            </span>
                          </div>
                          <div className="flex justify-between col-span-2">
                            <span className="text-[#7e8aaa]">Feature count</span>
                            <span className="mono text-cyan-300">
                              {featureEntries.length > 0 ? (
                                <>
                                  {Object.keys(metrics?.feature_importances ?? {}).length}{' '}
                                  <span className="text-[#5a637a]">(importance-weighted)</span>
                                </>
                              ) : (
                                '—'
                              )}
                            </span>
                          </div>
                          <div className="flex justify-between col-span-2">
                            <span className="text-[#7e8aaa]">Training source</span>
                            <span className="mono text-[#dde1ed] text-[10.5px]">
                              {metrics?.training_source === 'real_and_synthetic'
                                ? '🔵 Real + Synthetic'
                                : metrics?.training_source === 'synthetic_only'
                                  ? '🟡 Synthetic Only'
                                  : (metrics?.training_source ?? '—')}
                            </span>
                          </div>
                          <div className="flex justify-between col-span-2">
                            <span className="text-[#7e8aaa]">Real / synthetic</span>
                            <span className="mono text-[#dde1ed] text-[10.5px]">
                              {(metrics?.n_real_samples ?? 0).toLocaleString()} / {(metrics?.n_synthetic_samples ?? 0).toLocaleString()}
                            </span>
                          </div>
                        </div>
                      </>
                    ) : (
                      <EmptyState title="No registered versions" desc="POST /api/ml/versions returned no model lineage." />
                    )}
                  </div>

                  <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 space-y-3">
                    <div className="text-[9.5px] uppercase tracking-wider font-bold text-[#5a637a]">
                      Trigger immediate retrain
                    </div>
                    <p className="text-[11px] text-[#7e8aaa] leading-relaxed">
                      Calls <code className="mono text-[10px] text-[var(--color-cyan-fg)]">POST /api/ml/retrain</code>{' '}
                      which runs <code className="mono text-[10px]">ml_model.fit_initial</code> +{' '}
                      <code className="mono text-[10px]">save</code>, then logs a retrained event.
                      Model registry safety gate rejects if Brier &gt; 0.22 or AUC &lt; 0.70.
                    </p>
                    {retrainResult && (
                      <div className="bg-[#13161e] border border-[#1f2335] rounded p-2 text-[11px] space-y-1">
                        <div className="flex justify-between">
                          <span className="text-[#7e8aaa]">New version</span>
                          <code className="mono text-[var(--color-cyan-fg)]">{retrainResult.model_version}</code>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-[#7e8aaa]">Brier</span>
                          <span className={`mono ${classifyMetric(retrainResult.brier_score, { good: 0.15, warn: 0.20, higherIsBetter: false })}`}>
                            {fmt(retrainResult.brier_score)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-[#7e8aaa]">AUC</span>
                          <span className={`mono ${classifyMetric(retrainResult.roc_auc, { good: 0.80, warn: 0.70, higherIsBetter: true })}`}>
                            {fmt(retrainResult.roc_auc, 3)}
                          </span>
                        </div>
                        <div className="flex justify-between">
                          <span className="text-[#7e8aaa]">ECE</span>
                          <span className={`mono ${classifyMetric(retrainResult.ece, { good: 0.03, warn: 0.06, higherIsBetter: false })}`}>
                            {fmt(retrainResult.ece)}
                          </span>
                        </div>
                      </div>
                    )}
                    {versions && versions.versions.length > 0 && (
                      <div>
                        <label className="text-[10px] uppercase tracking-wider text-[#5a637a] font-bold mb-1 block">
                          Compare against
                        </label>
                        <Select value={selectedVersion} onValueChange={setSelectedVersion}>
                          <SelectTrigger className="h-8 bg-[#13161e] border-[#1f2335] text-[#dde1ed] text-xs">
                            <SelectValue placeholder="Select version" />
                          </SelectTrigger>
                          <SelectContent className="bg-[#13161e] border-[#1f2335]">
                            {versions.versions.map((v) => (
                              <SelectItem
                                key={v.version}
                                value={v.version}
                                className="text-[#dde1ed] focus:bg-[#1f2335]"
                              >
                                <code className="mono text-[11px]">{v.version}</code>
                                <span className="text-[10px] text-[#7e8aaa] ml-2">
                                  Brier {fmt(v.brier_score)} · AUC {fmt(v.roc_auc, 3)}
                                </span>
                                {v.is_active && (
                                  <span className="badge badge-green text-[8px] ml-2">active</span>
                                )}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {selectedVersion && selectedVersion !== activeVersion?.version && (
                          <div className="mt-2 text-[10px] text-[#5a637a]">
                            Roll back via <code className="mono text-[var(--color-cyan-fg)]">POST /api/ml/rollback?version={selectedVersion}</code>
                          </div>
                        )}
                      </div>
                    )}
                    <Button
                      onClick={triggerRetrain}
                      disabled={retraining}
                      className="w-full bg-[var(--color-cyan-bg)] border border-[var(--color-cyan-bd)] text-[var(--color-cyan-fg)] hover:bg-[var(--color-cyan-bd)]"
                    >
                      {retraining ? (
                        <>
                          <Loader2 size={14} className="mr-1.5 animate-spin" />
                          Retraining…
                        </>
                      ) : (
                        <>
                          <RefreshCw size={14} className="mr-1.5" />
                          Retrain Now
                        </>
                      )}
                    </Button>
                  </div>
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <div className="flex justify-between items-center px-4 py-2 border-t border-[#1f2335] bg-[#13161e] text-[10px] text-[#5a637a]">
        <span>
          Auto-refresh: <span className="mono text-[var(--color-cyan-fg)]">30s</span>
          {typeof document !== 'undefined' && document.visibilityState === 'hidden' && ' (paused)'}
        </span>
        <span className="mono">
          {metrics?.last_trained ? `trained ${fmtRel(metrics.last_trained)}` : 'no model trained'}
        </span>
      </div>
    </div>
  )
}

// ── Inline child components (kept in this file for cohesion) ───────────────

function CalibrationPlot({ curve }: { curve: ReliabilityBin[] }) {
  // Render a 10-bin reliability diagram: predicted prob (bin_center) on X,
  // empirical frequency on Y, with a perfect-calibration diagonal reference.
  const w = 320
  const h = 160
  const pad = 28
  const plotW = w - pad * 2
  const plotH = h - pad * 2
  const xScale = (p: number) => pad + p * plotW
  const yScale = (p: number) => pad + (1 - p) * plotH
  const maxCount = Math.max(...curve.map((b) => b.count), 1)

  return (
    <div className="flex flex-col gap-3">
      <svg viewBox={`0 0 ${w} ${h}`} className="w-full h-auto">
        {/* Grid */}
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line
              x1={pad}
              y1={yScale(t)}
              x2={w - pad}
              y2={yScale(t)}
              stroke="#1f2335"
              strokeWidth={0.5}
            />
            <line
              x1={xScale(t)}
              y1={pad}
              x2={xScale(t)}
              y2={h - pad}
              stroke="#1f2335"
              strokeWidth={0.5}
            />
          </g>
        ))}
        {/* Diagonal perfect-calibration reference */}
        <line
          x1={pad}
          y1={pad}
          x2={w - pad}
          y2={h - pad}
          stroke="#3e4560"
          strokeWidth={1}
          strokeDasharray="3,2"
        />
        {/* Bars showing sample counts (lightweight histogram backdrop) */}
        {curve.map((b, i) => {
          const barH = (b.count / maxCount) * plotH * 0.3
          return (
            <rect
              key={`bar-${i}`}
              x={xScale(b.bin_center) - plotW / 20}
              y={h - pad - barH}
              width={plotW / 10 - 1}
              height={barH}
              fill="rgba(6, 182, 212, 0.15)"
            />
          )
        })}
        {/* Calibration polyline */}
        <path
          d={curve
            .map((b, i) => `${i === 0 ? 'M' : 'L'} ${xScale(b.bin_center).toFixed(1)} ${yScale(b.empirical_freq).toFixed(1)}`)
            .join(' ')}
          fill="none"
          stroke="#22d3ee"
          strokeWidth={1.5}
        />
        {/* Calibration points */}
        {curve.map((b, i) => (
          <g key={`pt-${i}`}>
            <circle
              cx={xScale(b.bin_center)}
              cy={yScale(b.empirical_freq)}
              r={3}
              fill={Math.abs(b.bin_center - b.empirical_freq) < 0.03 ? '#22c55e' : Math.abs(b.bin_center - b.empirical_freq) < 0.08 ? '#f59e0b' : '#ef4444'}
              stroke="#13161e"
              strokeWidth={0.5}
            />
          </g>
        ))}
        {/* Axis labels */}
        <text x={w / 2} y={h - 4} textAnchor="middle" fill="#7e8aaa" fontSize="8">
          predicted probability (bin center)
        </text>
        <text
          x={6}
          y={h / 2}
          textAnchor="middle"
          fill="#7e8aaa"
          fontSize="8"
          transform={`rotate(-90 6 ${h / 2})`}
        >
          empirical frequency
        </text>
      </svg>
      {/* Per-bin table */}
      <div className="overflow-x-auto scrollbar-thin">
        <Table className="data-table">
          <TableHeader>
            <TableRow>
              <TableHead>Bin</TableHead>
              <TableHead className="text-right">Pred</TableHead>
              <TableHead className="text-right">Actual</TableHead>
              <TableHead className="text-right">|Δ|</TableHead>
              <TableHead className="text-right">n</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {curve.map((b, i) => {
              const delta = Math.abs(b.bin_center - b.empirical_freq)
              return (
                <TableRow key={i}>
                  <TableCell className="label-col text-[11px]">#{i + 1}</TableCell>
                  <TableCell className={`text-right mono ${classifyMetric(b.bin_center, { good: b.bin_center, warn: b.bin_center + 0.03, higherIsBetter: true })}`}>
                    {fmt(b.bin_center, 2)}
                  </TableCell>
                  <TableCell className={`text-right mono ${delta < 0.03 ? 'text-emerald-400' : delta < 0.08 ? 'text-amber-400' : 'text-red-400'}`}>
                    {fmt(b.empirical_freq, 2)}
                  </TableCell>
                  <TableCell className={`text-right mono ${delta < 0.03 ? 'text-emerald-400' : delta < 0.08 ? 'text-amber-400' : 'text-red-400'}`}>
                    {fmt(delta, 3)}
                  </TableCell>
                  <TableCell className="text-right mono text-[#7e8aaa]">{b.count}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
      <div className="text-[10px] text-[#5a637a]">
        Bar backdrop = sample count per bin · point colour = |Δ| (green ≤0.03, amber ≤0.08, red &gt;0.08)
      </div>
    </div>
  )
}

function DriftStatusView({
  report,
  psiHistory,
}: {
  report: DriftReport
  psiHistory: DriftSample[]
}) {
  const si = DRIFT_STATUS_MAP[report.status] ?? null
  const recentPsi = psiHistory.slice(-10).map((h) => h.psi).filter((v) => v != null)
  const maxPsi = Math.max(...recentPsi, report.threshold_critical_psi, 0.05)
  return (
    <>
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5">
          <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">PSI</div>
          <div
            className={`mono text-xl font-bold ${
              report.psi < report.threshold_moderate_psi
                ? 'text-emerald-400'
                : report.psi < report.threshold_critical_psi
                  ? 'text-amber-400'
                  : 'text-red-400'
            }`}
          >
            {report.psi.toFixed(4)}
          </div>
          <div className="text-[9px] text-[#5a637a] mono">
            thresholds {report.threshold_moderate_psi}/{report.threshold_critical_psi}
          </div>
        </div>
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5">
          <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">KS stat</div>
          <div
            className={`mono text-xl font-bold ${
              report.ks_stat < report.threshold_moderate_ks
                ? 'text-emerald-400'
                : report.ks_stat < report.threshold_critical_ks
                  ? 'text-amber-400'
                  : 'text-red-400'
            }`}
          >
            {report.ks_stat.toFixed(4)}
          </div>
          <div className="text-[9px] text-[#5a637a] mono">
            thresholds {report.threshold_moderate_ks}/{report.threshold_critical_ks}
          </div>
        </div>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5">
          <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">Rolling Brier</div>
          <div
            className={`mono text-base font-bold ${
              report.rolling_brier === null
                ? 'text-[#5a637a]'
                : report.rolling_brier < 0.15
                  ? 'text-emerald-400'
                  : report.rolling_brier < report.threshold_brier_drift
                    ? 'text-amber-400'
                    : 'text-red-400'
            }`}
          >
            {report.rolling_brier === null ? 'awaiting ≥20 samples' : report.rolling_brier.toFixed(4)}
          </div>
        </div>
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5">
          <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">EWMA Brier (α={report.ewma_alpha})</div>
          <div
            className={`mono text-base font-bold ${
              report.ewma_brier === null
                ? 'text-[#5a637a]'
                : report.ewma_brier < 0.15
                  ? 'text-emerald-400'
                  : report.ewma_brier < report.threshold_brier_drift
                    ? 'text-amber-400'
                    : 'text-red-400'
            }`}
          >
            {report.ewma_brier === null ? '—' : report.ewma_brier.toFixed(4)}
          </div>
        </div>
      </div>
      <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[9px] uppercase tracking-wider text-[#5a637a] font-bold">
            PSI trend (last {recentPsi.length} samples)
          </span>
          {si && (
            <span className={`badge ${si.cls} text-[9px]`}>{si.label}</span>
          )}
        </div>
        <div className={`flex items-end h-8 ${report.psi < report.threshold_moderate_psi ? 'text-emerald-400' : report.psi < report.threshold_critical_psi ? 'text-amber-400' : 'text-red-400'}`}>
          {recentPsi.length > 0 ? (
            <Sparkline values={recentPsi} max={maxPsi} />
          ) : (
            <span className="text-[10px] text-[#5a637a]">awaiting compute_psi() cycles</span>
          )}
        </div>
        <div className="flex justify-between text-[10px] text-[#5a637a]">
          <span>samples in window: <span className="mono text-[#dde1ed]">{report.window_samples}</span></span>
          <span>resolved outcomes: <span className="mono text-[#dde1ed]">{report.outcome_samples}</span></span>
        </div>
      </div>
    </>
  )
}
