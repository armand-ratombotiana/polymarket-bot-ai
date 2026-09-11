// src/components/ai-explainability.tsx — W39-6 shared AI labeling + status
// primitives reused across the AI/ML panels (AIMLCommandCenter, MLPanel,
// MLValidationPanel, ShadowInferencePanel, PerformanceReportPanel).
//
// Goal: make every model output visibly distinct from market data so the
// trader can never confuse a calibrated ensemble estimate with a hard
// market number. The four primitives below encapsulate the W39-6 design
// contract:
//
//   1. AIPredictionLabel    — small prefix (Sparkles icon + "AI Prediction:")
//                            that visually distinguishes any model-generated
//                            number from a market-driven number. Uses the
//                            blue/purple color system so it is unmistakably
//                            AI even at a glance.
//   2. ConfidenceBadge      — visible "Confidence: 65%" pill with a
//                            traffic-light color (green >70%, amber 50-70%,
//                            red <50%). The badge is always paired with an
//                            AIPredictionLabel so a probability is never
//                            shown without its confidence.
//   3. NotAGuaranteeInline  — small "NOT A GUARANTEE" disclaimer rendered
//                            inline next to a prediction. Mirrors the
//                            permanent banner already present in
//                            AIPredictionExplainerPanel (W38-5) but in a
//                            compact one-line form factor for the compact
//                            panels.
//   4. ModelStatusStrip     — single horizontal strip showing model version,
//                            training-data timestamp ("Trained: 2h ago"),
//                            drift status (🟢/🟡/🔴), calibration status
//                            ("Calibrated" / "Needs recalibration") and
//                            feature freshness ("Features: 3s old"). One
//                            glance tells the trader the model's health.
//   5. WhyExplanation        — expandable "Why?" section that surfaces the
//                            top-3 contributing features for a prediction,
//                            each with its value + sign of contribution,
//                            plus the champion-vs-challenger model
//                            agreement percentage ("Agreement: 92%").
//
// These primitives are deliberately stateless (no fetches, no clocks).
// Each parent panel supplies the data; the primitives own the visual
// contract. This keeps the panels' existing tests untouched — the
// primitives only ADD markup, never replace existing strings.

'use client'

import { useState, type ReactNode } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Clock,
  Gauge,
  RefreshCw,
  ShieldAlert,
  Sparkles,
  TrendingDown,
  TrendingUp,
} from 'lucide-react'
import { cn } from '@/lib/utils'

// ── 1. AIPredictionLabel ─────────────────────────────────────────────────

export interface AIPredictionLabelProps {
  /** Optional override for the visible label text. Defaults to "AI Prediction:". */
  label?: string
  /** Optional sub-label rendered in italic gray immediately after the
   *  main label — e.g. "(model-generated)". */
  hint?: string
  /** Optional size variant. `sm` for compact panels (MLPanel), `md` for
   *  full-width cards (AIMLCommandCenter KPI strip). */
  size?: 'sm' | 'md'
  /** Optional className passthrough. */
  className?: string
}

export function AIPredictionLabel({
  label = 'AI Prediction:',
  hint,
  size = 'sm',
  className,
}: AIPredictionLabelProps) {
  const iconSize = size === 'sm' ? 'size-3' : 'size-3.5'
  const textSize = size === 'sm' ? 'text-[9.5px]' : 'text-[10.5px]'
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 uppercase tracking-wider font-bold text-blue-300',
        textSize,
        className,
      )}
      data-testid="ai-prediction-label"
    >
      <Sparkles className={cn(iconSize, 'text-blue-400')} aria-hidden="true" />
      <span>{label}</span>
      {hint && (
        <span className="text-[#5a637a] italic normal-case font-normal tracking-normal ml-0.5">
          {hint}
        </span>
      )}
    </span>
  )
}

// ── 2. ConfidenceBadge ────────────────────────────────────────────────────

export type ConfidenceTone = 'high' | 'medium' | 'low' | 'unknown'

export interface ConfidenceBadgeProps {
  /** Confidence in [0, 1]. Values ≥0.70 → green (high), 0.50–0.70 → amber
   *  (medium), <0.50 → red (low). null/undefined → neutral grey. */
  value: number | null | undefined
  /** Whether to render the percentage text inline with the label. */
  showLabel?: boolean
  /** Optional className passthrough. */
  className?: string
}

export function confidenceTone(value: number | null | undefined): ConfidenceTone {
  if (value == null || !Number.isFinite(value)) return 'unknown'
  if (value >= 0.70) return 'high'
  if (value >= 0.50) return 'medium'
  return 'low'
}

export function ConfidenceBadge({
  value,
  showLabel = true,
  className,
}: ConfidenceBadgeProps) {
  const tone = confidenceTone(value)
  const pct = value == null || !Number.isFinite(value)
    ? '—'
    : `${(value * 100).toFixed(0)}%`
  const cfg = {
    high: {
      bg: 'bg-green-500/10',
      border: 'border-green-500/30',
      text: 'text-green-400',
      dot: 'bg-green-400',
      label: 'Confidence',
    },
    medium: {
      bg: 'bg-amber-500/10',
      border: 'border-amber-500/30',
      text: 'text-amber-300',
      dot: 'bg-amber-400',
      label: 'Confidence',
    },
    low: {
      bg: 'bg-red-500/10',
      border: 'border-red-500/30',
      text: 'text-red-400',
      dot: 'bg-red-400',
      label: 'Confidence',
    },
    unknown: {
      bg: 'bg-[#1f2335]',
      border: 'border-[#3e4560]',
      text: 'text-[#7e8aaa]',
      dot: 'bg-[#5a637a]',
      label: 'Confidence',
    },
  }[tone]
  return (
    <span
      data-testid="confidence-badge"
      data-confidence-tone={tone}
      role="img"
      aria-label={`Confidence: ${pct} (${tone})`}
      className={cn(
        'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5',
        'text-[10px] font-bold mono whitespace-nowrap',
        cfg.bg,
        cfg.border,
        cfg.text,
        className,
      )}
    >
      <span className={cn('inline-block size-1.5 rounded-full', cfg.dot)} aria-hidden="true" />
      {showLabel && <span className="opacity-80">{cfg.label}:</span>}
      <span>{pct}</span>
    </span>
  )
}

// ── 3. NotAGuaranteeInline ────────────────────────────────────────────────

export interface NotAGuaranteeInlineProps {
  /** Optional override for the disclaimer body text. */
  text?: string
  /** Compact variant renders the disclaimer as a single line of small
   *  amber text without the bordered card. Default = false (bordered card). */
  compact?: boolean
  className?: string
}

export function NotAGuaranteeInline({
  text = 'NOT A GUARANTEE — calibrated estimate, not a forecast. Always combine AI signals with independent risk management.',
  compact = false,
  className,
}: NotAGuaranteeInlineProps) {
  if (compact) {
    return (
      <span
        className={cn(
          'inline-flex items-start gap-1 text-[9px] text-amber-300/90 italic leading-tight',
          className,
        )}
        role="note"
        data-testid="not-a-guarantee-inline"
      >
        <ShieldAlert className="size-2.5 shrink-0 mt-px text-amber-400" aria-hidden="true" />
        <span>{text}</span>
      </span>
    )
  }
  return (
    <div
      className={cn(
        'flex items-start gap-1.5 text-[10px] text-amber-200 bg-amber-500/10',
        'border border-amber-500/30 rounded px-2 py-1.5',
        className,
      )}
      role="alert"
      data-testid="not-a-guarantee-inline"
    >
      <ShieldAlert className="size-3 shrink-0 mt-0.5 text-amber-400" aria-hidden="true" />
      <span>
        <strong className="font-bold">NOT A GUARANTEE.</strong> {text}
      </span>
    </div>
  )
}

// ── 4. ModelStatusStrip ───────────────────────────────────────────────────

export type DriftLevel = 'ok' | 'warning' | 'critical' | 'unknown'

export interface ModelStatusStripProps {
  /** Model version string, e.g. "v1.155.0". */
  version: string | null | undefined
  /** Training-data epoch (seconds). Rendered as "Trained: 2h ago". */
  trainedAt: number | null | undefined
  /** Drift level — derived from the drift detector's `status` field. */
  drift: DriftLevel
  /** Whether the model is calibrated (ECE < target). */
  calibrated: boolean
  /** Feature freshness — seconds since the last feature vector update. */
  featureAgeSeconds: number | null | undefined
  /** Optional className passthrough. */
  className?: string
}

export function driftLevelFromStatus(status: string | null | undefined): DriftLevel {
  if (!status) return 'unknown'
  const s = status.toUpperCase()
  if (s === 'HEALTHY' || s === 'OK') return 'ok'
  if (s.includes('MODERATE') || s.includes('WARN')) return 'warning'
  if (s.includes('SIGNIFICANT') || s.includes('CRITICAL') || s.includes('DRIFT')) return 'critical'
  return 'unknown'
}

function fmtRelAge(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null || !Number.isFinite(epochSeconds) || epochSeconds <= 0) return '—'
  const diff = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds))
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function fmtFeatureAge(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 60) return `${Math.round(seconds)}s old`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m old`
  return `${Math.round(seconds / 3600)}h old`
}

const DRIFT_CFG: Record<DriftLevel, { icon: string; label: string; cls: string }> = {
  ok: { icon: '🟢', label: 'OK', cls: 'text-green-400' },
  warning: { icon: '🟡', label: 'Warning', cls: 'text-amber-300' },
  critical: { icon: '🔴', label: 'Critical', cls: 'text-red-400' },
  unknown: { icon: '⚪', label: 'Unknown', cls: 'text-[#7e8aaa]' },
}

export function ModelStatusStrip({
  version,
  trainedAt,
  drift,
  calibrated,
  featureAgeSeconds,
  className,
}: ModelStatusStripProps) {
  const driftCfg = DRIFT_CFG[drift]
  const versionStr = version || '—'
  const trainedStr = fmtRelAge(trainedAt)
  const featureStr = fmtFeatureAge(featureAgeSeconds)
  return (
    <div
      data-testid="model-status-strip"
      className={cn(
        'flex flex-wrap items-center gap-x-3 gap-y-1.5 bg-[#0e1015] border border-[#1f2335]',
        'rounded-md px-2.5 py-1.5 text-[10px]',
        className,
      )}
    >
      <span className="inline-flex items-center gap-1" title="Active model version">
        <Sparkles className="size-2.5 text-blue-400" aria-hidden="true" />
        <span className="text-[#5a637a] uppercase tracking-wider font-bold">Version</span>
        <span className="mono text-blue-300 font-bold" data-testid="status-version">
          {versionStr}
        </span>
      </span>
      <span className="inline-flex items-center gap-1" title="Last training cycle">
        <Clock className="size-2.5 text-cyan-400" aria-hidden="true" />
        <span className="text-[#5a637a] uppercase tracking-wider font-bold">Trained</span>
        <span className="mono text-cyan-300" data-testid="status-trained">
          {trainedStr}
        </span>
      </span>
      <span className="inline-flex items-center gap-1" title="Concept drift level">
        <span aria-hidden="true">{driftCfg.icon}</span>
        <span className="text-[#5a637a] uppercase tracking-wider font-bold">Drift</span>
        <span className={cn('font-bold', driftCfg.cls)} data-testid="status-drift">
          {driftCfg.label}
        </span>
      </span>
      <span className="inline-flex items-center gap-1" title="Isotonic calibration status">
        <Gauge className="size-2.5 text-purple-400" aria-hidden="true" />
        <span className="text-[#5a637a] uppercase tracking-wider font-bold">Calibration</span>
        <span
          className={cn(
            'font-bold',
            calibrated ? 'text-green-400' : 'text-amber-300',
          )}
          data-testid="status-calibration"
        >
          {calibrated ? 'Calibrated' : 'Needs recalibration'}
        </span>
      </span>
      <span className="inline-flex items-center gap-1" title="Feature vector freshness">
        <RefreshCw className="size-2.5 text-emerald-400" aria-hidden="true" />
        <span className="text-[#5a637a] uppercase tracking-wider font-bold">Features</span>
        <span className="mono text-emerald-300" data-testid="status-features">
          {featureStr}
        </span>
      </span>
    </div>
  )
}

// ── 5. WhyExplanation ────────────────────────────────────────────────────

export interface FeatureContribution {
  name: string
  /** The feature's value at prediction time (raw). */
  value: number | string | null
  /** The signed SHAP contribution. Positive pushes toward YES, negative
   *  toward NO. */
  contribution: number
}

export interface WhyExplanationProps {
  /** Top contributing features (already sorted by |contribution| desc). At
   *  most 3 will be rendered; the spec calls for "top 3". */
  features: FeatureContribution[]
  /** Champion-vs-challenger agreement in [0, 1]. null when there is no
   *  challenger registered. */
  agreement: number | null | undefined
  /** Optional header label override. Defaults to "Why?". */
  headerLabel?: string
  /** Optional extra content rendered below the feature list (e.g. a
   *  "view full SHAP" link). */
  children?: ReactNode
  /** Optional controlled-expanded prop. Defaults to uncontrolled. */
  defaultExpanded?: boolean
  className?: string
}

export function WhyExplanation({
  features,
  agreement,
  headerLabel = 'Why?',
  children,
  defaultExpanded = false,
  className,
}: WhyExplanationProps) {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const top = features.slice(0, 3)
  const agreementPct = agreement == null || !Number.isFinite(agreement)
    ? null
    : `${(agreement * 100).toFixed(0)}%`
  return (
    <div
      data-testid="why-explanation"
      className={cn(
        'border border-blue-500/20 bg-blue-500/5 rounded-md overflow-hidden',
        className,
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        className="w-full flex items-center justify-between px-2.5 py-1.5 text-[10.5px] font-bold text-blue-300 hover:bg-blue-500/10 transition-colors"
        data-testid="why-toggle"
      >
        <span className="inline-flex items-center gap-1.5">
          <Sparkles className="size-3 text-blue-400" aria-hidden="true" />
          <span>{headerLabel}</span>
          <span className="text-[#5a637a] font-normal normal-case tracking-normal italic ml-1">
            (top 3 contributing features)
          </span>
        </span>
        {expanded ? (
          <ChevronDown className="size-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="size-3" aria-hidden="true" />
        )}
      </button>
      {expanded && (
        <div className="px-2.5 pb-2 pt-1 space-y-1.5">
          {top.length === 0 ? (
            <div className="text-[10px] text-[#7e8aaa] italic">
              No feature attributions available for this prediction.
            </div>
          ) : (
            top.map((f, i) => {
              const positive = f.contribution >= 0
              return (
                <div
                  key={`${f.name}-${i}`}
                  className="grid grid-cols-[1fr_auto_auto] items-center gap-2 text-[10.5px]"
                  data-testid="why-feature-row"
                >
                  <span className="mono text-[#dde1ed] truncate" title={f.name}>
                    {f.name}
                  </span>
                  <span className="mono text-[#7e8aaa] text-right" title="Feature value at prediction time">
                    val={f.value == null ? '—' : String(f.value)}
                  </span>
                  <span
                    className={cn(
                      'mono font-bold text-right inline-flex items-center gap-0.5 justify-end',
                      positive ? 'text-emerald-400' : 'text-red-400',
                    )}
                    title="SHAP contribution to P(YES)"
                  >
                    {positive ? (
                      <TrendingUp className="size-2.5" aria-hidden="true" />
                    ) : (
                      <TrendingDown className="size-2.5" aria-hidden="true" />
                    )}
                    {positive ? '+' : ''}
                    {f.contribution.toFixed(4)}
                  </span>
                </div>
              )
            })
          )}
          <div className="flex items-center justify-between pt-1 border-t border-[#1f2335] text-[9.5px]">
            <span className="text-[#5a637a] uppercase tracking-wider font-bold">
              Champion vs Challenger
            </span>
            {agreementPct == null ? (
              <span className="mono text-[#7e8aaa] italic" data-testid="why-agreement">
                no challenger registered
              </span>
            ) : (
              <span
                className={cn(
                  'mono font-bold',
                  agreement != null && agreement >= 0.9
                    ? 'text-green-400'
                    : agreement != null && agreement >= 0.7
                      ? 'text-amber-300'
                      : 'text-red-400',
                )}
                data-testid="why-agreement"
              >
                Agreement: {agreementPct}
              </span>
            )}
          </div>
          {children}
        </div>
      )}
    </div>
  )
}
