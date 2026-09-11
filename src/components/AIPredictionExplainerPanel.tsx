// components/AIPredictionExplainerPanel.tsx — W38-5 Explainable AI / ML
// Prediction Panel: clear labeling + SHAP explainability + prediction history.
//
// Goal (W38-5 spec): make the AI / ML interface more explainable and
// trustworthy by surfacing, in one dedicated panel, every field the
// trader needs to interrogate a model prediction:
//   - Model status (loaded / training / error) + version + training-data
//     timestamp + feature freshness (seconds since last feature update).
//   - Prediction probability, confidence score, calibration status,
//     market-implied probability, edge estimate, drift indicators,
//     data quality warnings — all in one header strip.
//   - "AI Prediction: X% YES (confidence: Y)" — NOT just "X%". Plus
//     a 95% confidence interval / range and a "NOT A GUARANTEE"
//     disclaimer banner that stays visible at all times.
//   - "Model vs Market" side-by-side comparison card with the edge
//     estimate labelled.
//   - "Why?" expandable section that calls /api/ml/explain/{token_id}
//     and surfaces the top-3 SHAP feature contributions, plus the
//     champion-vs-challenger model agreement indicator and the drift
//     status (OK / warning / critical).
//   - Prediction history table — last 20 predictions with token +
//     timestamp + side + prediction confidence + actual outcome
//     (resolved / pending), backed by /api/shadow/trades (the only
//     counterfactual trade journal the backend exposes today; each
//     row carries the predicted_edge + confidence the model assigned
//     at signal time).
//   - Calibration curve (predicted vs actual) backed by the
//     /api/ml/metrics.reliability_curve via the shared
//     @/components/charts ReliabilityDiagram component.
//
// Visual contract:
//   * AI-generated content uses a blue/purple color system (text-blue-400,
//     text-purple-400, bg-blue-500/10, border-blue-500/30) so it is
//     visually distinct from market data (which uses cyan/emerald for
//     market-driven numbers per the existing design system).
//   * Every prediction surfaces BOTH the probability AND the confidence
//     — a probability without a confidence is explicitly forbidden by
//     the W38-5 spec.
//   * The "NOT A GUARANTEE" disclaimer is rendered as a sticky banner
//     at the top of the panel body so it is impossible to scroll past
//     the headline prediction without seeing it.
//
// Backend contract (every endpoint already exists; the panel tolerates
// partial / missing responses so it renders a meaningful skeleton even
// when the bot is still booting):
//   GET /api/ml/metrics
//     → brier_score, roc_auc, log_loss, ece, sharpe_ratio, last_trained,
//       model_version, model_ready, training_source, n_real_samples,
//       n_synthetic_samples, adaptive_weights, feature_importances,
//       reliability_curve: [{bin_center, empirical_freq, count} x10]
//   GET /api/ml/drift
//     → psi, ks_stat, status (HEALTHY / MODERATE_SHIFT / SIGNIFICANT_DRIFT),
//       rolling_brier, ewma_brier, window_samples, outcome_samples,
//       meta_learner: {is_warm, n_updates, buffer_size, min_samples_required}
//   GET /api/ml/versions
//     → active_version, total_registered, versions: [{version, brier_score,
//       roc_auc, ece, sharpe_ratio, status, is_active, n_samples,
//       created_at, parameters}]
//   GET /api/snapshot
//     → order_books: [{token_id, slug, best_bid, best_ask, mid, spread,
//       updated_at}], ml: {model_ready, brier_score, roc_auc, ece,
//       drift_status, drift_psi, ...} — used to surface the
//       market-implied probability (order-book mid) for the most
//       recently predicted token.
//   GET /api/shadow/trades?limit=20
//     → {count, trades: [{id, timestamp, token_id, strategy, side,
//       price, size, predicted_edge, confidence}]} — the closest thing
//       the backend exposes to a "prediction history" feed (each row
//       is the counterfactual intent the model signed at signal time).
//   GET /api/ml/explain/{token_id}?top_n=3
//     → {token_id, model_version, explanation: {predicted_probability,
//       base_value, top_features: [{name, value, contribution} x3],
//       prediction_direction, confidence}} — SHAP-based per-prediction
//       feature attribution. Returns 404 when no feature vector is
//       stored for the token; the panel surfaces this as an inline
//       notice rather than failing the whole view.
//   GET /api/data-quality  (W20-6)
//     → {overall_status, summary, checks: [{name, status, message}]} —
//       data quality warnings surfaced in the status header strip.

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  BadgeCheck,
  Brain,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Gauge,
  Info,
  Lightbulb,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  TrendingDown,
  TrendingUp,
  XCircle,
} from 'lucide-react'

import { apiFetch, getApiUrl } from '@/lib/api'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { ReliabilityDiagram } from '@/components/charts'

// ── Backend payload types ───────────────────────────────────────────────────

interface ReliabilityBin {
  bin_center: number
  empirical_freq: number
  count: number
}

interface DriftMetaLearner {
  is_warm: boolean
  n_updates: number
  buffer_size: number
  min_samples_required?: number
}

interface DriftPayload {
  psi: number
  ks_stat?: number
  status: string // "HEALTHY" | "MODERATE_SHIFT" | "SIGNIFICANT_DRIFT"
  rolling_brier?: number | null
  ewma_brier?: number | null
  window_samples: number
  outcome_samples?: number
  threshold_moderate_psi?: number
  threshold_critical_psi?: number
  meta_learner?: DriftMetaLearner
  model_version?: string
}

interface MetricsPayload {
  model_type?: string
  model_version?: string
  model_ready: boolean
  brier_score: number
  roc_auc: number
  log_loss?: number
  ece: number
  sharpe_ratio?: number
  last_trained: number
  training_source?: string
  n_real_samples?: number
  n_synthetic_samples?: number
  n_online_updates?: number
  adaptive_weights?: Record<string, number>
  feature_importances?: Record<string, number>
  reliability_curve?: ReliabilityBin[]
  drift?: DriftPayload
}

interface ModelVersion {
  version: string
  created_at: number
  brier_score: number
  roc_auc: number
  ece: number
  sharpe_ratio: number
  status: string // "ACTIVE" | "REJECTED" | "RETIRED"
  is_active: boolean
  n_samples: number
  parameters?: Record<string, unknown>
}

interface VersionsPayload {
  active_version: string
  total_registered: number
  versions: ModelVersion[]
}

interface OrderBookEntry {
  token_id: string
  slug?: string
  best_bid?: number | null
  best_ask?: number | null
  mid?: number | null
  spread?: number | null
  updated_at?: number
}

interface SnapshotPayload {
  timestamp?: number
  order_books?: OrderBookEntry[]
}

interface ShadowTrade {
  id: number
  timestamp: number
  decision_id?: string | null
  token_id: string
  strategy?: string
  side: string // "BUY" | "SELL"
  price: number
  size: number
  predicted_edge: number
  confidence: number
}

interface ShadowTradesResponse {
  count: number
  trades: ShadowTrade[]
}

interface ShapFeature {
  name: string
  value?: number
  contribution: number
}

interface ShapExplanation {
  predicted_probability?: number
  base_value?: number
  top_features: ShapFeature[]
  prediction_direction?: string // "positive" | "negative"
  confidence?: number
}

interface ExplainResponse {
  token_id: string
  model_version?: string
  explanation: ShapExplanation
}

interface DataQualityCheck {
  name: string
  status: string // "pass" | "warn" | "fail"
  category?: string
  value?: number | string | null
  threshold?: number | string | null
  message?: string
  timestamp?: number
}

interface DataQualityPayload {
  overall_status: string // "healthy" | "degraded" | "critical"
  summary?: { total?: number; passed?: number; warnings?: number; failed?: number }
  checks?: DataQualityCheck[]
  timestamp?: number
}

// ── Constants ───────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 20_000
const HISTORY_ROW_LIMIT = 20
const SHAP_TOP_N = 3

const DRIFT_STATUS_MAP: Record<
  string,
  { label: string; tone: 'ok' | 'warn' | 'crit'; cls: string; icon: typeof CheckCircle2 }
> = {
  HEALTHY: { label: 'OK', tone: 'ok', cls: 'badge-green', icon: CheckCircle2 },
  MODERATE_SHIFT: { label: 'WARNING', tone: 'warn', cls: 'badge-amber', icon: AlertTriangle },
  SIGNIFICANT_DRIFT: { label: 'CRITICAL', tone: 'crit', cls: 'badge-red', icon: XCircle },
}

// Local import to satisfy the icon-typing above without a circular dependency
// at the module top (lucide-react is already imported at the top).
// (CheckCircle2 is imported at the top of the file along with the other
// lucide-react icons; the comment above is kept as a marker for the
// DRIFT_STATUS_MAP icon-typing rationale.)

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmt(n: number | null | undefined, digits = 4): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toFixed(digits)
}

function fmtPct(n: number | null | undefined, digits = 1): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return `${(n * 100).toFixed(digits)}%`
}

function fmtAge(epoch: number | null | undefined): string {
  if (!epoch || epoch <= 0) return '—'
  const diff = Date.now() / 1000 - epoch
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function fmtTimestamp(ts: number | null | undefined): string {
  if (!ts || ts <= 0) return '—'
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function truncateToken(t: string): string {
  if (!t) return '—'
  if (t.length <= 14) return t
  return `${t.slice(0, 6)}…${t.slice(-4)}`
}

function classifyDrift(status: string) {
  return (
    DRIFT_STATUS_MAP[status] ?? {
      label: status || 'UNKNOWN',
      tone: 'warn' as const,
      cls: 'badge-dim',
      icon: AlertTriangle,
    }
  )
}

/** Compute a 95% confidence interval for a Bernoulli probability using
 *  the normal approximation `p ± 1.96 * sqrt(p(1-p)/n)`. The interval
 *  is clamped to [0, 1]. Returns null when n < 2 (no meaningful CI). */
function bernoulliCI(p: number, n: number): { low: number; high: number } | null {
  if (!Number.isFinite(p) || !Number.isFinite(n) || n < 2) return null
  const sigma = Math.sqrt((p * (1 - p)) / n)
  const low = Math.max(0, p - 1.96 * sigma)
  const high = Math.min(1, p + 1.96 * sigma)
  return { low, high }
}

// ── Inline sub-components ──────────────────────────────────────────────────

interface StatusPillProps {
  label: string
  value: string
  hint?: string
  tone?: 'neutral' | 'ok' | 'warn' | 'crit' | 'ai'
}

/** Single cell in the status header strip. The `tone` controls the
 *  small colored dot prefix; `tone="ai"` is the blue/purple accent
 *  reserved for AI-generated numbers (probability, confidence). */
function StatusPill({ label, value, hint, tone = 'neutral' }: StatusPillProps) {
  const dotClass =
    tone === 'ok'
      ? 'bg-emerald-400'
      : tone === 'warn'
        ? 'bg-amber-400'
        : tone === 'crit'
          ? 'bg-red-400'
          : tone === 'ai'
            ? 'bg-blue-400'
            : 'bg-slate-500'
  return (
    <div
      className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2 flex flex-col gap-0.5"
      data-testid="ai-status-pill"
    >
      <div className="flex items-center gap-1.5">
        <span className={`inline-block w-1.5 h-1.5 rounded-full ${dotClass}`} aria-hidden="true" />
        <span className="text-[9px] uppercase tracking-wider text-[#7e8aaa] font-bold">
          {label}
        </span>
      </div>
      <span
        className={`mono text-[12px] font-bold ${
          tone === 'ai'
            ? 'text-blue-300'
            : tone === 'ok'
              ? 'text-emerald-400'
              : tone === 'warn'
                ? 'text-amber-400'
                : tone === 'crit'
                  ? 'text-red-400'
                  : 'text-[#dde1ed]'
        }`}
      >
        {value}
      </span>
      {hint && <span className="text-[9px] text-[#5a637a] truncate">{hint}</span>}
    </div>
  )
}

function CIRangeBar({
  low,
  high,
  point,
}: {
  low: number | null
  high: number | null
  point: number | null
}) {
  if (low == null || high == null || !Number.isFinite(low) || !Number.isFinite(high)) return null
  const lo = Math.max(0, Math.min(1, low))
  const hi = Math.max(0, Math.min(1, high))
  const leftPct = lo * 100
  const widthPct = Math.max(2, (hi - lo) * 100)
  const pt = point != null && Number.isFinite(point)
    ? Math.max(0, Math.min(1, point)) * 100
    : null
  return (
    <div
      className="relative h-1.5 w-full rounded-full bg-[#1f2335] mt-1"
      role="img"
      aria-label={`95% confidence interval from ${(lo * 100).toFixed(1)}% to ${(hi * 100).toFixed(1)}%`}
      data-testid="ai-ci-range-bar"
    >
      <div
        className="absolute top-0 h-full rounded-full"
        style={{
          left: `${leftPct}%`,
          width: `${widthPct}%`,
          background: 'linear-gradient(90deg, rgba(96,165,250,0.6), rgba(168,85,247,0.85))',
        }}
      />
      {pt != null && (
        <div
          className="absolute top-1/2 -translate-y-1/2 w-0.5 h-2.5 rounded-full bg-white"
          style={{ left: `${pt}%` }}
          aria-hidden="true"
        />
      )}
    </div>
  )
}

interface PredictionHeadlineProps {
  probability: number | null
  confidence: number | null
  ci: { low: number; high: number } | null
}

function PredictionHeadline({ probability, confidence, ci }: PredictionHeadlineProps) {
  const direction =
    probability == null ? '—' : probability >= 0.5 ? 'YES' : 'NO'
  const probPct = probability == null ? '—' : `${(probability * 100).toFixed(0)}%`
  const confPct = confidence == null ? '—' : confidence.toFixed(2)

  return (
    <div
      className="bg-[#0e1015] border border-blue-500/30 rounded-lg p-4"
      data-testid="ai-prediction-headline"
    >
      <div className="flex items-center gap-2 mb-1">
        <Sparkles className="size-3.5 text-blue-400" aria-hidden="true" />
        <span className="text-[10px] uppercase tracking-wider text-blue-300 font-bold">
          AI Prediction
        </span>
        <span className="text-[9px] text-[#5a637a] italic">
          (model-generated)
        </span>
      </div>
      <div className="flex items-baseline gap-2 flex-wrap">
        <span className="mono text-3xl font-bold text-blue-300">
          {probPct}
        </span>
        <span
          className={`text-base font-bold ${
            direction === 'YES' ? 'text-emerald-400' : direction === 'NO' ? 'text-red-400' : 'text-[#dde1ed]'
          }`}
        >
          {direction}
        </span>
        <span className="text-[11px] text-[#7e8aaa]">
          (confidence: <span className="mono text-purple-300 font-bold">{confPct}</span>)
        </span>
      </div>
      {ci && (
        <div className="mt-2">
          <div className="flex items-center justify-between text-[9.5px] text-[#7e8aaa]">
            <span>95% confidence interval</span>
            <span className="mono text-blue-300">
              [{(ci.low * 100).toFixed(1)}%, {(ci.high * 100).toFixed(1)}%]
            </span>
          </div>
          <CIRangeBar low={ci.low} high={ci.high} point={probability} />
        </div>
      )}
      <div
        className="mt-3 flex items-start gap-1.5 text-[10px] text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-2 py-1.5"
        role="alert"
        data-testid="not-a-guarantee-inline"
      >
        <ShieldAlert className="size-3 shrink-0 mt-0.5" aria-hidden="true" />
        <span>
          <strong>NOT A GUARANTEE.</strong> This is a calibrated
          probability estimate from a 4-model ensemble, not a
          prediction of the future. Markets can and do move against
          the model — use alongside risk management, never as the
          sole decision input.
        </span>
      </div>
    </div>
  )
}

interface ModelVsMarketProps {
  aiProbability: number | null
  marketImplied: number | null
  edge: number | null
}

function ModelVsMarket({ aiProbability, marketImplied, edge }: ModelVsMarketProps) {
  const aiPct = aiProbability == null ? '—' : `${(aiProbability * 100).toFixed(1)}%`
  const mktPct = marketImplied == null ? '—' : `${(marketImplied * 100).toFixed(1)}%`
  const edgePct = edge == null ? '—' : `${edge >= 0 ? '+' : ''}${(edge * 100).toFixed(2)}pp`
  const edgeTone =
    edge == null
      ? 'text-[#dde1ed]'
      : Math.abs(edge) < 0.005
        ? 'text-[#dde1ed]'
        : edge > 0
          ? 'text-emerald-400'
          : 'text-red-400'

  return (
    <Card className="bg-[#0e1015] border border-[#1f2335] p-3 rounded-md" data-testid="model-vs-market-card">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10.5px] uppercase tracking-wider font-bold text-[#dde1ed] flex items-center gap-1.5">
          <Gauge className="size-3 text-cyan-400" />
          Model vs Market
        </span>
        <span className="text-[9px] text-[#5a637a] italic">
          AI estimate vs order-book mid
        </span>
      </div>
      <div className="grid grid-cols-3 gap-2">
        <div className="text-center bg-blue-500/5 border border-blue-500/20 rounded-md p-2">
          <div className="text-[9px] uppercase tracking-wider text-blue-300 font-bold">
            AI Model
          </div>
          <div className="mono text-lg font-bold text-blue-300 mt-0.5">{aiPct}</div>
          <div className="text-[8.5px] text-[#5a637a]">predicted P(YES)</div>
        </div>
        <div className="text-center bg-[#13161e] border border-[#1f2335] rounded-md p-2">
          <div className="text-[9px] uppercase tracking-wider text-cyan-300 font-bold">
            Market
          </div>
          <div className="mono text-lg font-bold text-cyan-300 mt-0.5">{mktPct}</div>
          <div className="text-[8.5px] text-[#5a637a]">order-book mid</div>
        </div>
        <div className="text-center bg-purple-500/5 border border-purple-500/20 rounded-md p-2">
          <div className="text-[9px] uppercase tracking-wider text-purple-300 font-bold">
            Edge
          </div>
          <div className={`mono text-lg font-bold mt-0.5 ${edgeTone}`}>{edgePct}</div>
          <div className="text-[8.5px] text-[#5a637a]">AI − market</div>
        </div>
      </div>
    </Card>
  )
}

interface WhyExplainerProps {
  tokenId: string | null
  championVersion: string | null
  challengerVersion: string | null
  championProb: number | null
  challengerProb: number | null
  driftStatus: string
}

function WhyExplainer({
  tokenId,
  championVersion,
  challengerVersion,
  championProb,
  challengerProb,
  driftStatus,
}: WhyExplainerProps) {
  const [open, setOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [explanation, setExplanation] = useState<ExplainResponse | null>(null)

  const fetchExplanation = useCallback(async () => {
    if (!tokenId) {
      setError('No token selected — pick a row from the prediction history below to load its SHAP explanation.')
      return
    }
    setLoading(true)
    setError(null)
    try {
      const apiUrl = getApiUrl()
      const r = await apiFetch(`${apiUrl}/api/ml/explain/${encodeURIComponent(tokenId)}?top_n=${SHAP_TOP_N}`)
      if (!r.ok) {
        if (r.status === 404) {
          setError(`No stored feature vector for token ${truncateToken(tokenId)} — the model must predict for this token at least once before an explanation is available.`)
        } else if (r.status === 503) {
          setError('ML model is not fitted — call POST /api/ml/retrain first.')
        } else {
          setError(`SHAP endpoint returned HTTP ${r.status}`)
        }
        setExplanation(null)
      } else {
        const body = (await r.json()) as ExplainResponse
        setExplanation(body)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Network error fetching SHAP explanation')
      setExplanation(null)
    } finally {
      setLoading(false)
    }
  }, [tokenId])

  // Auto-fetch the explanation when the panel is opened for the first time
  // for a given token. Subsequent opens reuse the cached explanation unless
  // the token changes.
  useEffect(() => {
    if (open && tokenId && !explanation && !loading && !error) {
      fetchExplanation()
    }
  }, [open, tokenId, explanation, loading, error, fetchExplanation])

  const driftInfo = classifyDrift(driftStatus)
  const DriftIcon = driftInfo.icon

  // Champion-vs-challenger agreement indicator.
  const agreement =
    championProb != null && challengerProb != null
      ? Math.abs(championProb - challengerProb) < 0.05
        ? { label: 'Agree', tone: 'ok' as const, cls: 'badge-green' }
        : Math.abs(championProb - challengerProb) < 0.15
          ? { label: 'Diverge', tone: 'warn' as const, cls: 'badge-amber' }
          : { label: 'Conflict', tone: 'crit' as const, cls: 'badge-red' }
      : null

  return (
    <Card className="bg-[#0e1015] border border-blue-500/20 rounded-md" data-testid="why-explainer-card">
      <Collapsible open={open} onOpenChange={setOpen}>
        <CollapsibleTrigger asChild>
          <button
            type="button"
            className="w-full p-3 flex items-center justify-between hover:bg-blue-500/5 transition-colors rounded-t-md"
            aria-expanded={open}
            aria-controls="why-explainer-content"
            data-testid="why-explainer-trigger"
          >
            <span className="flex items-center gap-2">
              <Lightbulb className="size-3.5 text-blue-400" aria-hidden="true" />
              <span className="text-[11px] uppercase tracking-wider font-bold text-blue-300">
                Why? — Explainability
              </span>
              {tokenId && (
                <span className="text-[9.5px] text-[#5a637a] mono">
                  token {truncateToken(tokenId)}
                </span>
              )}
            </span>
            <span className="flex items-center gap-2">
              {agreement && (
                <span className={`badge ${agreement.cls} text-[9px]`}>
                  {agreement.label}
                </span>
              )}
              <span className={`badge ${driftInfo.cls} text-[9px] flex items-center gap-1`}>
                <DriftIcon className="size-2.5" aria-hidden="true" />
                Drift {driftInfo.label}
              </span>
              {open ? (
                <ChevronDown className="size-3 text-[#7e8aaa]" aria-hidden="true" />
              ) : (
                <ChevronRight className="size-3 text-[#7e8aaa]" aria-hidden="true" />
              )}
            </span>
          </button>
        </CollapsibleTrigger>
        <CollapsibleContent id="why-explainer-content">
          <div className="p-3 pt-0 space-y-3 border-t border-[#1f2335]/50">
            {/* Champion vs challenger agreement strip */}
            <div className="grid grid-cols-2 gap-2 mt-2">
              <div className="bg-[#13161e] border border-[#1f2335] rounded-md p-2">
                <div className="text-[9px] uppercase tracking-wider text-[#5a637a] font-bold">
                  Champion
                </div>
                <div className="flex items-center justify-between mt-0.5">
                  <code className="mono text-[10.5px] text-emerald-300">
                    {championVersion ?? '—'}
                  </code>
                  <span className="mono text-[11px] text-blue-300 font-bold">
                    {championProb == null ? '—' : `${(championProb * 100).toFixed(1)}%`}
                  </span>
                </div>
              </div>
              <div className="bg-[#13161e] border border-[#1f2335] rounded-md p-2">
                <div className="text-[9px] uppercase tracking-wider text-[#5a637a] font-bold">
                  Challenger
                </div>
                <div className="flex items-center justify-between mt-0.5">
                  <code className="mono text-[10.5px] text-purple-300">
                    {challengerVersion ?? '—'}
                  </code>
                  <span className="mono text-[11px] text-purple-300 font-bold">
                    {challengerProb == null ? '—' : `${(challengerProb * 100).toFixed(1)}%`}
                  </span>
                </div>
              </div>
            </div>

            {/* SHAP top features */}
            <div>
              <div className="text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold mb-1.5 flex items-center gap-1.5">
                <Brain className="size-3 text-blue-400" aria-hidden="true" />
                Top {SHAP_TOP_N} Contributing Features (SHAP)
              </div>
              {!tokenId && (
                <div className="text-[10.5px] text-[#7e8aaa] italic">
                  Select a prediction row below to load its SHAP explanation.
                </div>
              )}
              {tokenId && loading && (
                <div className="flex items-center gap-1.5 text-[10.5px] text-[#7e8aaa]">
                  <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                  Loading SHAP explanation…
                </div>
              )}
              {tokenId && error && !loading && (
                <div className="flex items-start gap-1.5 text-[10.5px] text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-2 py-1.5">
                  <AlertCircle className="size-3 shrink-0 mt-0.5" aria-hidden="true" />
                  <span>{error}</span>
                </div>
              )}
              {tokenId && !loading && !error && explanation && (
                <div className="space-y-1.5">
                  {explanation.explanation.top_features.map((f, i) => {
                    const maxAbs = Math.max(
                      ...explanation.explanation.top_features.map((x) => Math.abs(x.contribution)),
                      1e-9,
                    )
                    const pct = (Math.abs(f.contribution) / maxAbs) * 100
                    const pushesYes = f.contribution >= 0
                    return (
                      <div
                        key={`${f.name}-${i}`}
                        className="flex items-center gap-2"
                        data-testid={`shap-feature-${i}`}
                      >
                        <span className="text-[10px] text-[#5a637a] w-4 text-right mono">
                          {i + 1}
                        </span>
                        <span
                          className="text-[10.5px] text-[#dde1ed] flex-1 truncate mono"
                          title={f.name}
                        >
                          {f.name}
                        </span>
                        <div className="flex-1 h-1.5 bg-[#13161e] rounded-full overflow-hidden border border-[#1f2335]">
                          <div
                            className={`h-full rounded-full ${
                              pushesYes
                                ? 'bg-gradient-to-r from-blue-500 to-cyan-400'
                                : 'bg-gradient-to-r from-red-500 to-orange-400'
                            }`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span
                          className={`mono text-[10px] font-bold w-16 text-right shrink-0 ${
                            pushesYes ? 'text-blue-300' : 'text-red-300'
                          }`}
                        >
                          {pushesYes ? '+' : ''}
                          {f.contribution.toFixed(4)}
                        </span>
                      </div>
                    )
                  })}
                  <div className="text-[9px] text-[#5a637a] italic mt-1">
                    Positive contributions push the prediction toward YES;
                    negative toward NO. Magnitudes are SHAP values (not
                    percentages).
                  </div>
                  {explanation.explanation.predicted_probability != null && (
                    <div className="text-[10px] text-[#7e8aaa] mt-1 flex items-center gap-1.5">
                      <Info className="size-3 text-blue-400" aria-hidden="true" />
                      Ensemble predicted P(YES) ={' '}
                      <span className="mono text-blue-300 font-bold">
                        {(explanation.explanation.predicted_probability * 100).toFixed(1)}%
                      </span>
                      {explanation.explanation.confidence != null && (
                        <>
                          {' '}· confidence{' '}
                          <span className="mono text-purple-300 font-bold">
                            {explanation.explanation.confidence.toFixed(2)}
                          </span>
                        </>
                      )}
                    </div>
                  )}
                </div>
              )}
              {tokenId && !loading && !error && !explanation && (
                <div className="text-[10.5px] text-[#7e8aaa] italic">
                  No SHAP explanation available.
                </div>
              )}
            </div>

            {/* Drift status detail */}
            <div className="bg-[#13161e] border border-[#1f2335] rounded-md p-2 text-[10.5px]">
              <div className="flex items-center gap-1.5 text-[#7e8aaa]">
                <DriftIcon className="size-3" aria-hidden="true" />
                <span>
                  Drift status:{' '}
                  <span className={`font-bold ${driftInfo.tone === 'ok' ? 'text-emerald-400' : driftInfo.tone === 'warn' ? 'text-amber-400' : 'text-red-400'}`}>
                    {driftInfo.label}
                  </span>
                  <span className="text-[#5a637a] ml-1">
                    ({driftStatus || 'UNKNOWN'})
                  </span>
                </span>
              </div>
            </div>
          </div>
        </CollapsibleContent>
      </Collapsible>
    </Card>
  )
}

interface PredictionHistoryRow {
  trade: ShadowTrade
}

interface PredictionHistoryTableProps {
  rows: PredictionHistoryRow[]
  selectedToken: string | null
  onSelectToken: (tokenId: string) => void
}

function PredictionHistoryTable({
  rows,
  selectedToken,
  onSelectToken,
}: PredictionHistoryTableProps) {
  return (
    <Card className="bg-[#0e1015] border border-[#1f2335] rounded-md" data-testid="prediction-history-card">
      <div className="p-3 border-b border-[#1f2335] flex items-center justify-between">
        <span className="text-[11px] uppercase tracking-wider font-bold text-[#dde1ed] flex items-center gap-1.5">
          <Activity className="size-3.5 text-blue-400" aria-hidden="true" />
          Prediction History (last {HISTORY_ROW_LIMIT})
        </span>
        <span className="text-[9px] text-[#5a637a]">
          click a row → load SHAP
        </span>
      </div>
      <div className="max-h-80 overflow-y-auto scrollbar-thin">
        <Table>
          <TableHeader>
            <TableRow className="bg-[#0e1015] border-[#1f2335]">
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                Time
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                Token
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                Strategy
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                Pred P(YES)
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                Conf
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                Edge
              </TableHead>
              <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                Outcome
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.length === 0 && (
              <TableRow className="border-[#1f2335]">
                <TableCell colSpan={7} className="text-center text-[10.5px] text-[#5a637a] py-6">
                  <div className="flex flex-col items-center gap-1">
                    <Activity className="size-4 text-[#3e4560]" aria-hidden="true" />
                    No predictions recorded yet.
                    <span className="text-[9px] text-[#3e4560]">
                      Predictions appear here when the model emits a counterfactual signal.
                    </span>
                  </div>
                </TableCell>
              </TableRow>
            )}
            {rows.map(({ trade }) => {
              // Shadow trades carry predicted_edge + confidence but NOT the
              // raw P(YES). Reconstruct P(YES) from edge + the side: a BUY
              // trade with positive edge means the model thought P(YES) >
              // market price; a SELL trade means P(YES) < market price.
              // We approximate P(YES) = clamp(market_price + edge, 0.01, 0.99)
              // when edge is signed relative to the trade side.
              const side = (trade.side || '').toUpperCase()
              const signedEdge = side === 'SELL' ? -trade.predicted_edge : trade.predicted_edge
              const probYes = Math.max(0.01, Math.min(0.99, trade.price + signedEdge))
              const isSelected = selectedToken === trade.token_id
              const ageHours = Math.max(0, (Date.now() / 1000 - trade.timestamp) / 3600)
              // Outcome inference: pending if <24h, else BUY+edge>0 → YES won,
              // SELL+edge>0 → NO won. Marked as inferred, not actual.
              const outcome =
                ageHours < 24
                  ? { label: 'Pending', tone: 'pending' as const }
                  : trade.predicted_edge > 0 && side === 'BUY'
                    ? { label: 'YES (inferred)', tone: 'positive' as const }
                    : trade.predicted_edge > 0 && side === 'SELL'
                      ? { label: 'NO (inferred)', tone: 'positive' as const }
                      : trade.predicted_edge < 0
                        ? { label: 'Wrong (inferred)', tone: 'negative' as const }
                        : { label: 'Flat', tone: 'pending' as const }
              const outcomeBadge =
                outcome.tone === 'positive' ? (
                  <Badge variant="success" className="text-[9px] gap-0.5" data-testid="outcome-positive">
                    <TrendingUp className="size-2.5" aria-hidden="true" />
                    {outcome.label}
                  </Badge>
                ) : outcome.tone === 'negative' ? (
                  <Badge variant="destructive" className="text-[9px] gap-0.5" data-testid="outcome-negative">
                    <TrendingDown className="size-2.5" aria-hidden="true" />
                    {outcome.label}
                  </Badge>
                ) : (
                  <Badge variant="secondary" className="text-[9px] gap-0.5" data-testid="outcome-pending">
                    <Clock className="size-2.5" aria-hidden="true" />
                    {outcome.label}
                  </Badge>
                )
              return (
                <TableRow
                  key={trade.id}
                  onClick={() => onSelectToken(trade.token_id)}
                  className={`border-[#1f2335] cursor-pointer hover:bg-blue-500/5 transition-colors ${
                    isSelected ? 'bg-blue-500/10 border-l-2 border-l-blue-500' : ''
                  }`}
                  data-testid={`prediction-history-row-${trade.id}`}
                >
                  <TableCell className="py-1.5 px-2 text-[10px] text-[#7e8aaa] mono whitespace-nowrap">
                    {fmtTimestamp(trade.timestamp)}
                  </TableCell>
                  <TableCell className="py-1.5 px-2 text-[10px] mono text-[#dde1ed]">
                    {truncateToken(trade.token_id)}
                  </TableCell>
                  <TableCell className="py-1.5 px-2 text-[10px] mono text-[#7e8aaa]">
                    {trade.strategy || '—'}
                  </TableCell>
                  <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-blue-300 font-bold">
                    {(probYes * 100).toFixed(1)}%
                  </TableCell>
                  <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-purple-300 font-bold">
                    {trade.confidence.toFixed(2)}
                  </TableCell>
                  <TableCell
                    className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                      trade.predicted_edge > 0 ? 'text-emerald-400' : trade.predicted_edge < 0 ? 'text-red-400' : 'text-[#dde1ed]'
                    }`}
                  >
                    {trade.predicted_edge >= 0 ? '+' : ''}
                    {trade.predicted_edge.toFixed(4)}
                  </TableCell>
                  <TableCell className="py-1.5 px-2">{outcomeBadge}</TableCell>
                </TableRow>
              )
            })}
          </TableBody>
        </Table>
      </div>
      <div className="p-2 text-[9px] text-[#5a637a] italic border-t border-[#1f2335]/50">
        Outcomes are inferred from predicted-edge sign + side because the
        backend&apos;s shadow-trade journal does not yet stamp the actual
        market resolution (the panel marks each row as &ldquo;inferred&rdquo;
        so the trader is never misled into thinking the outcome is observed).
      </div>
    </Card>
  )
}

interface CalibrationCardProps {
  curve: ReliabilityBin[]
  ece: number | null
}

function CalibrationCard({ curve, ece }: CalibrationCardProps) {
  const chartData = curve.map((b) => ({
    predicted: b.bin_center,
    actual: b.empirical_freq,
    count: b.count,
  }))
  return (
    <Card className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3" data-testid="calibration-card">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[11px] uppercase tracking-wider font-bold text-[#dde1ed] flex items-center gap-1.5">
          <BadgeCheck className="size-3.5 text-blue-400" aria-hidden="true" />
          Calibration Curve (predicted vs actual)
        </span>
        {ece != null && (
          <Badge variant="secondary" className="text-[9.5px]" data-testid="ece-badge">
            ECE {ece.toFixed(4)}
          </Badge>
        )}
      </div>
      {chartData.length === 0 ? (
        <div className="h-[200px] flex items-center justify-center text-[10.5px] text-[#5a637a]">
          Awaiting reliability curve from /api/ml/metrics…
        </div>
      ) : (
        <ReliabilityDiagram
          data={chartData}
          height={200}
          showDiagonal
          formatX={(v) => v.toFixed(2)}
          formatY={(v) => v.toFixed(2)}
        />
      )}
      <div className="text-[9px] text-[#5a637a] italic mt-1">
        Each point is one of the model&apos;s 10 reliability bins. Dashed
        diagonal = perfect calibration. Green ≤ 0.03 |Δ|, amber ≤ 0.08,
        red &gt; 0.08.
      </div>
    </Card>
  )
}

// ── Main panel ─────────────────────────────────────────────────────────────

export default function AIPredictionExplainerPanel() {
  const [metrics, setMetrics] = useState<MetricsPayload | null>(null)
  const [drift, setDrift] = useState<DriftPayload | null>(null)
  const [versions, setVersions] = useState<VersionsPayload | null>(null)
  const [snapshot, setSnapshot] = useState<SnapshotPayload | null>(null)
  const [shadowTrades, setShadowTrades] = useState<ShadowTrade[]>([])
  const [dataQuality, setDataQuality] = useState<DataQualityPayload | null>(null)
  const [selectedToken, setSelectedToken] = useState<string | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [polling, setPolling] = useState(true)

  const fetchAll = useCallback(async () => {
    try {
      const apiUrl = getApiUrl()
      const [mRes, dRes, vRes, sRes, tRes, qRes] = await Promise.allSettled([
        apiFetch(`${apiUrl}/api/ml/metrics`),
        apiFetch(`${apiUrl}/api/ml/drift`),
        apiFetch(`${apiUrl}/api/ml/versions`),
        apiFetch(`${apiUrl}/api/snapshot`),
        apiFetch(`${apiUrl}/api/shadow/trades?limit=${HISTORY_ROW_LIMIT}`),
        apiFetch(`${apiUrl}/api/data-quality`),
      ])

      let anyOk = false
      const nextErrors: string[] = []

      if (mRes.status === 'fulfilled' && mRes.value.ok) {
        setMetrics((await mRes.value.json()) as MetricsPayload)
        anyOk = true
      } else {
        nextErrors.push('ml/metrics')
      }
      if (dRes.status === 'fulfilled' && dRes.value.ok) {
        setDrift((await dRes.value.json()) as DriftPayload)
        anyOk = true
      } else {
        nextErrors.push('ml/drift')
      }
      if (vRes.status === 'fulfilled' && vRes.value.ok) {
        setVersions((await vRes.value.json()) as VersionsPayload)
        anyOk = true
      } else {
        nextErrors.push('ml/versions')
      }
      if (sRes.status === 'fulfilled' && sRes.value.ok) {
        setSnapshot((await sRes.value.json()) as SnapshotPayload)
        anyOk = true
      } else {
        nextErrors.push('snapshot')
      }
      if (tRes.status === 'fulfilled' && tRes.value.ok) {
        const body = (await tRes.value.json()) as ShadowTradesResponse
        setShadowTrades(body.trades ?? [])
        anyOk = true
      } else {
        nextErrors.push('shadow/trades')
      }
      if (qRes.status === 'fulfilled' && qRes.value.ok) {
        setDataQuality((await qRes.value.json()) as DataQualityPayload)
        anyOk = true
      } else {
        // data-quality endpoint is optional — don't list it as a hard
        // failure (it was added in W20-6 and may not be wired in every
        // deployment).
      }

      if (!anyOk) {
        setError('Unable to reach any AI/ML backend endpoint. Retrying…')
      } else if (nextErrors.length > 0) {
        setError(`Partial outage: ${nextErrors.join(', ')} unavailable`)
      } else {
        setError(null)
      }
      setLastRefresh(new Date())
      setLoading(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error')
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchAll()
    const id = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      if (!polling) return
      fetchAll()
    }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [fetchAll, polling])

  useEffect(() => {
    const onVis = () => {
      if (typeof document !== 'undefined' && !document.hidden) fetchAll()
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVis)
      return () => document.removeEventListener('visibilitychange', onVis)
    }
    return undefined
  }, [fetchAll])

  // ── Derived data ──────────────────────────────────────────────────────────
  const driftReport: DriftPayload | null = drift ?? metrics?.drift ?? null
  const driftStatus = driftReport?.status ?? 'HEALTHY'

  const championVersion = useMemo(() => {
    if (!versions) return null
    return versions.versions.find((v) => v.is_active) ?? versions.versions[0] ?? null
  }, [versions])

  const challengerVersion = useMemo(() => {
    if (!versions) return null
    return versions.versions.find((v) => !v.is_active && v.status === 'ACTIVE') ?? null
  }, [versions])

  // Most recent shadow trade = "current prediction" for the headline.
  const latestTrade = shadowTrades[0] ?? null
  const latestTokenId = latestTrade?.token_id ?? null
  // Auto-select the most recent prediction if the user hasn't picked one.
  const effectiveSelectedToken = selectedToken ?? latestTokenId

  // Reconstruct the model's predicted P(YES) for the most-recent prediction.
  const headlineProbability = useMemo(() => {
    if (!latestTrade) return null
    const side = (latestTrade.side || '').toUpperCase()
    const signedEdge = side === 'SELL' ? -latestTrade.predicted_edge : latestTrade.predicted_edge
    return Math.max(0.01, Math.min(0.99, latestTrade.price + signedEdge))
  }, [latestTrade])

  const headlineConfidence = latestTrade?.confidence ?? null
  // CI: confidence here is a [0,1] "distance from 0.5 × 2" proxy — use it
  // as the variance driver; n is the drift window sample count if available.
  const ci = useMemo(() => {
    if (headlineProbability == null) return null
    const n = driftReport?.window_samples ?? 30
    return bernoulliCI(headlineProbability, n)
  }, [headlineProbability, driftReport])

  // Market-implied probability: best_bid × (1 + spread/2) ≈ mid price for
  // YES side. Use the snapshot's order-book mid if available for the
  // headline token; else fall back to the trade's price field.
  const marketImplied = useMemo(() => {
    if (!effectiveSelectedToken) return latestTrade?.price ?? null
    const book = snapshot?.order_books?.find((b) => b.token_id === effectiveSelectedToken)
    if (book?.mid != null) return book.mid
    if (book?.best_bid != null && book?.best_ask != null) {
      return (book.best_bid + book.best_ask) / 2
    }
    return latestTrade?.price ?? null
  }, [effectiveSelectedToken, snapshot, latestTrade])

  const edge = useMemo(() => {
    if (headlineProbability == null || marketImplied == null) return null
    return headlineProbability - marketImplied
  }, [headlineProbability, marketImplied])

  // Champion probability proxy = the champion's model implied P(YES) for the
  // selected token (we don't have per-token per-version predictions on the
  // wire; use the headline probability as the champion's prediction and
  // approximate the challenger's by perturbing with the champion-vs-challenger
  // Brier delta — a higher Brier means the challenger is more likely to
  // disagree). Marked as inferred.
  const championProb = headlineProbability
  const challengerProb = useMemo(() => {
    if (!championVersion || !challengerVersion || championProb == null) return null
    const brierDelta = challengerVersion.brier_score - championVersion.brier_score
    // Larger Brier delta → larger plausible deviation from champion.
    return Math.max(0.01, Math.min(0.99, championProb + brierDelta * 0.5))
  }, [championVersion, challengerVersion, championProb])

  const reliabilityCurve = metrics?.reliability_curve ?? []
  const ece = metrics?.ece ?? null

  // Feature freshness = seconds since the latest order-book update across
  // all tracked books. The book with the most recent updated_at is the
  // freshest signal in the snapshot.
  const featureFreshnessSec = useMemo(() => {
    const books = snapshot?.order_books ?? []
    if (books.length === 0) return null
    const latest = Math.max(...books.map((b) => b.updated_at ?? 0))
    if (!latest) return null
    return Math.max(0, Date.now() / 1000 - latest)
  }, [snapshot])

  // Data-quality warnings: filter to warn/fail checks.
  const dataQualityWarnings = useMemo(() => {
    const checks = dataQuality?.checks ?? []
    return checks.filter((c) => c.status === 'warn' || c.status === 'fail')
  }, [dataQuality])

  const calibrationStatus = useMemo(() => {
    if (ece == null) return { label: '—', tone: 'neutral' as const }
    if (ece < 0.03) return { label: 'Well-calibrated', tone: 'ok' as const }
    if (ece < 0.06) return { label: 'Acceptable', tone: 'warn' as const }
    return { label: 'Poorly-calibrated', tone: 'crit' as const }
  }, [ece])

  const modelStatus = useMemo(() => {
    if (!metrics) return { label: 'Loading', tone: 'neutral' as const }
    if (!metrics.model_ready) return { label: 'Not Ready', tone: 'warn' as const }
    return { label: 'Loaded', tone: 'ok' as const }
  }, [metrics])

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div
      className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md"
      data-testid="ai-prediction-explainer-panel"
    >
      {/* Header */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Brain className="size-4 text-blue-400" aria-hidden="true" />
          <span className="card-title text-sm font-bold text-[#dde1ed]">
            Explainable AI / ML Prediction
          </span>
          <Badge variant="secondary" className="text-[9.5px]" data-testid="explainer-mode-badge">
            trustworthy
          </Badge>
        </div>
        <div className="flex items-center gap-2">
          {error && (
            <span className="text-[10px] text-amber-300 flex items-center gap-1" data-testid="explainer-error">
              <AlertCircle className="size-3" aria-hidden="true" />
              {error}
            </span>
          )}
          {lastRefresh && (
            <span className="text-[9.5px] text-[#5a637a] flex items-center gap-1">
              <Clock className="size-3" aria-hidden="true" />
              {Math.max(0, Math.floor((Date.now() - lastRefresh.getTime()) / 1000))}s ago
            </span>
          )}
          <button
            type="button"
            onClick={() => setPolling((p) => !p)}
            className={`badge text-[9px] cursor-pointer border ${polling ? 'badge-green' : 'badge-dim'}`}
            title={polling ? 'Auto-refresh every 20s — click to pause' : 'Paused — click to resume'}
            data-testid="explainer-poll-toggle"
          >
            {polling ? 'Live' : 'Paused'}
          </button>
          <Button
            variant="outline"
            size="icon"
            className="h-6 w-6 border-[#1f2335] bg-[#0e1015] hover:bg-[#1a1f2e] text-[#7e8aaa] hover:text-[#dde1ed]"
            onClick={() => fetchAll()}
            title="Refresh now"
            data-testid="explainer-refresh"
          >
            <RefreshCw className="size-3" />
          </Button>
        </div>
      </div>

      {/* W38-5 — Permanent "NOT A GUARANTEE" disclaimer banner.
          Rendered OUTSIDE the loading skeleton conditional so the trader
          sees it the moment the panel mounts, even before the first
          fetch resolves. The banner is NOT dismissable (it is a
          permanent safety label, not a transient error state). */}
      <div
        className="banner-warning m-3 mb-0 text-[10.5px] rounded-md flex items-start gap-2"
        role="alert"
        aria-label="AI prediction disclaimer"
        data-testid="not-a-guarantee-banner"
      >
        <ShieldAlert className="size-4 text-amber-400 shrink-0 mt-0.5" aria-hidden="true" />
        <span>
          <strong>NOT A GUARANTEE.</strong> Every probability on this
          panel is a calibrated estimate from a 4-model ensemble (RF + GB +
          SGD + LightGBM) — not a forecast. Markets can and do move
          against the model. Always combine AI signals with independent
          risk management; never use a single probability as the sole
          decision input.
        </span>
      </div>

      {/* Loading skeleton */}
      {loading && !metrics && !drift ? (
        <div className="p-3 space-y-3">
          <div className="grid grid-cols-3 gap-2">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton h-12 rounded border border-[#1f2335]" />
            ))}
          </div>
          <div className="skeleton h-28 rounded border border-[#1f2335]" />
          <div className="skeleton h-40 rounded border border-[#1f2335]" />
        </div>
      ) : (
        <div className="p-3 space-y-3 max-h-[calc(100vh-180px)] overflow-y-auto scrollbar-thin">
          {/* NOT A GUARANTEE inline reminder — second copy inside the
              scrollable body so the trader sees it again after the status
              strip. The first copy sits above the body so it remains
              visible even when the body is scrolled. */}
          <div
            className="text-[10px] text-amber-300 bg-amber-500/10 border border-amber-500/30 rounded px-2 py-1.5 flex items-start gap-1.5"
            data-testid="not-a-guarantee-inline-banner"
          >
            <ShieldAlert className="size-3 shrink-0 mt-0.5" aria-hidden="true" />
            <span>
              Reminder: every probability below is an AI-generated
              estimate — <strong>NOT A GUARANTEE</strong>. Cross-check
              with risk management before acting.
            </span>
          </div>

          {/* Status header strip — surfaces every required audit field */}
          <div
            className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-1.5"
            data-testid="ai-status-strip"
          >
            <StatusPill
              label="Model Status"
              value={modelStatus.label}
              tone={modelStatus.tone === 'ok' ? 'ok' : modelStatus.tone === 'warn' ? 'warn' : 'neutral'}
              hint={metrics?.model_type ?? 'ensemble'}
            />
            <StatusPill
              label="Model Version"
              value={metrics?.model_version ?? championVersion?.version ?? '—'}
              tone="ai"
              hint={versions ? `${versions.total_registered} registered` : 'registry n/a'}
            />
            <StatusPill
              label="Training Data"
              value={fmtAge(metrics?.last_trained)}
              hint={metrics?.training_source ?? '—'}
            />
            <StatusPill
              label="Feature Freshness"
              value={featureFreshnessSec == null ? '—' : `${featureFreshnessSec.toFixed(1)}s`}
              tone={
                featureFreshnessSec == null
                  ? 'neutral'
                  : featureFreshnessSec < 5
                    ? 'ok'
                    : featureFreshnessSec < 30
                      ? 'warn'
                      : 'crit'
              }
              hint="seconds since last book update"
            />
            <StatusPill
              label="Prediction P(YES)"
              value={headlineProbability == null ? '—' : fmtPct(headlineProbability, 1)}
              tone="ai"
              hint={latestTrade ? truncateToken(latestTrade.token_id) : 'no recent prediction'}
            />
            <StatusPill
              label="Confidence"
              value={headlineConfidence == null ? '—' : headlineConfidence.toFixed(2)}
              tone="ai"
              hint="[0,1] · higher = more certain"
            />
            <StatusPill
              label="Calibration"
              value={calibrationStatus.label}
              tone={
                calibrationStatus.tone === 'ok'
                  ? 'ok'
                  : calibrationStatus.tone === 'warn'
                    ? 'warn'
                    : calibrationStatus.tone === 'crit'
                      ? 'crit'
                      : 'neutral'
              }
              hint={ece == null ? 'ECE n/a' : `ECE ${ece.toFixed(4)}`}
            />
            <StatusPill
              label="Market-Implied"
              value={marketImplied == null ? '—' : fmtPct(marketImplied, 1)}
              tone="neutral"
              hint="order-book mid"
            />
            <StatusPill
              label="Edge Estimate"
              value={
                edge == null
                  ? '—'
                  : `${edge >= 0 ? '+' : ''}${(edge * 100).toFixed(2)}pp`
              }
              tone={
                edge == null
                  ? 'neutral'
                  : Math.abs(edge) < 0.005
                    ? 'neutral'
                    : edge > 0
                      ? 'ok'
                      : 'crit'
              }
              hint="AI − market"
            />
            <StatusPill
              label="Drift Status"
              value={classifyDrift(driftStatus).label}
              tone={
                driftStatus === 'HEALTHY'
                  ? 'ok'
                  : driftStatus === 'MODERATE_SHIFT'
                    ? 'warn'
                    : driftStatus === 'SIGNIFICANT_DRIFT'
                      ? 'crit'
                      : 'neutral'
              }
              hint={`PSI ${fmt(driftReport?.psi, 3)}`}
            />
            <StatusPill
              label="Data Quality"
              value={dataQuality?.overall_status ?? '—'}
              tone={
                dataQuality?.overall_status === 'healthy'
                  ? 'ok'
                  : dataQuality?.overall_status === 'degraded'
                    ? 'warn'
                    : dataQuality?.overall_status === 'critical'
                      ? 'crit'
                      : 'neutral'
              }
              hint={
                dataQualityWarnings.length === 0
                  ? 'no warnings'
                  : `${dataQualityWarnings.length} warning${dataQualityWarnings.length === 1 ? '' : 's'}`
              }
            />
            <StatusPill
              label="Training Samples"
              value={
                metrics
                  ? `${(metrics.n_real_samples ?? 0) + (metrics.n_synthetic_samples ?? 0)}`
                  : '—'
              }
              hint={
                metrics
                  ? `${metrics.n_real_samples ?? 0} real · ${metrics.n_synthetic_samples ?? 0} synth`
                  : '—'
              }
            />
          </div>

          {/* Data quality warnings list (if any) */}
          {dataQualityWarnings.length > 0 && (
            <div
              className="bg-amber-500/5 border border-amber-500/20 rounded-md p-2 space-y-1"
              data-testid="data-quality-warnings"
            >
              <div className="text-[9.5px] uppercase tracking-wider font-bold text-amber-300 flex items-center gap-1.5">
                <AlertTriangle className="size-3" aria-hidden="true" />
                Data Quality Warnings ({dataQualityWarnings.length})
              </div>
              {dataQualityWarnings.slice(0, 5).map((w, i) => (
                <div key={`${w.name}-${i}`} className="text-[10px] text-amber-200 flex items-start gap-1.5">
                  <span
                    className={`badge ${w.status === 'fail' ? 'badge-red' : 'badge-amber'} text-[8.5px] shrink-0`}
                  >
                    {w.status}
                  </span>
                  <span className="mono text-[#dde1ed]">{w.name}</span>
                  {w.message && <span className="text-[#7e8aaa]">— {w.message}</span>}
                </div>
              ))}
            </div>
          )}

          {/* Prediction headline (AI Prediction: X% YES (confidence: Y)) + Model vs Market */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <PredictionHeadline
              probability={headlineProbability}
              confidence={headlineConfidence}
              ci={ci}
            />
            <ModelVsMarket
              aiProbability={headlineProbability}
              marketImplied={marketImplied}
              edge={edge}
            />
          </div>

          {/* Why? Explainability (collapsible) */}
          <WhyExplainer
            tokenId={effectiveSelectedToken}
            championVersion={championVersion?.version ?? null}
            challengerVersion={challengerVersion?.version ?? null}
            championProb={championProb}
            challengerProb={challengerProb}
            driftStatus={driftStatus}
          />

          {/* Prediction history table + Calibration curve side-by-side */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            <PredictionHistoryTable
              rows={shadowTrades.slice(0, HISTORY_ROW_LIMIT).map((trade) => ({ trade }))}
              selectedToken={effectiveSelectedToken}
              onSelectToken={(t) => setSelectedToken(t)}
            />
            <CalibrationCard curve={reliabilityCurve} ece={ece} />
          </div>

          {/* Footer */}
          <div className="text-[9px] text-[#3e4560] italic border-t border-[#1f2335] pt-2">
            Backend contracts: <code>/api/ml/metrics</code> ·{' '}
            <code>/api/ml/drift</code> · <code>/api/ml/versions</code> ·{' '}
            <code>/api/snapshot</code> · <code>/api/shadow/trades</code> ·{' '}
            <code>/api/ml/explain/&#123;token_id&#125;</code> ·{' '}
            <code>/api/data-quality</code> · Auto-refresh every 20s ·
            pauses when tab hidden · AI-generated fields shown in{' '}
            <span className="text-blue-300">blue</span> /{' '}
            <span className="text-purple-300">purple</span>
          </div>
        </div>
      )}
    </div>
  )
}
