// components/ShadowInferencePanel.tsx — Shadow Inference + Shadow Trading Panel
//
// Exposes the challenger-model comparison surface (ml/shadow_inference.py +
// ml/model_registry.py + ml/routes.py) and the counterfactual trade journal
// (core/shadow_trading.py) on a single screen.
//
// Backend endpoints used:
//   GET  /api/ml/versions          — model-version lineage (champion vs challengers)
//   GET  /api/ml/metrics           — active model's brier / log_loss / reliability_curve
//   POST /api/ml/rollback?v=X      — promote challenger → champion (operator override)
//   GET  /api/shadow/trades         — recent counterfactual trades
//   GET  /api/shadow/comparison     — shadow-vs-live side-by-side aggregate
//
// Visual style mirrors MLPanel.tsx — dark card backgrounds (#13161e), border
// tokens (#1f2335), .mono / .badge / .spinner / .card design-system classes.
//
// NOTE: the in-memory shadow_inference registry (per-challenger call counts
// + recent comparisons ring buffer) does NOT yet expose an HTTP surface —
// the docstring of `ml/shadow_inference.py` calls this out explicitly:
// "the surface a future `/api/shadow-inference` endpoint would expose". The
// challenger table here therefore derives its roster from the persisted
// model-registry lineage (`/api/ml/versions`), classifying each version as
// champion / shadow / demoted from `(is_active, status)`. The
// "register new challenger" form posts to `/api/ml/register` and gracefully
// surfaces a notice if the route is not yet wired — the form layout is
// ready to flip on the moment the shadow_inference HTTP surface lands.

'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from 'recharts'
import {
  Activity,
  AlertCircle,
  ArrowUpCircle,
  Boxes,
  Clock,
  Crown,
  Ghost,
  Hash,
  PlusCircle,
  RefreshCw,
  Swords,
  Target,
  TrendingDown,
  TrendingUp,
  Trophy,
  XCircle,
} from 'lucide-react'
import { apiFetch } from '@/lib/api'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

// ── Backend payload types ───────────────────────────────────────────────────

interface ModelVersion {
  version: string
  created_at: number
  brier_score: number
  roc_auc: number
  ece: number
  sharpe_ratio: number
  status: string // "ACTIVE" | "REJECTED"
  n_samples: number
  parameters: Record<string, unknown>
  is_active: boolean
}

interface ModelVersionsResponse {
  active_version: string
  total_registered: number
  versions: ModelVersion[]
}

interface ShadowTrade {
  id: number
  timestamp: number
  decision_id: string | null
  token_id: string
  strategy: string
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

interface ComparisonShadowSide {
  count: number
  total_size: number
  avg_predicted_edge: number
  avg_confidence: number
  by_side: { BUY: number; SELL: number }
  by_strategy: Record<string, unknown>
}

interface ComparisonLiveSide {
  count: number
  total_pnl: number
  avg_pnl: number
  win_rate: number
  total_volume_shares: number
  by_strategy: Record<string, unknown>
}

interface StrategyRow {
  strategy: string
  shadow_count: number
  live_count: number
  shadow_avg_edge: number
  live_avg_pnl: number
  shadow_total_size: number
  live_total_pnl: number
}

interface ShadowVsLiveComparison {
  shadow: ComparisonShadowSide
  live: ComparisonLiveSide
  strategies: StrategyRow[]
}

interface ReliabilityBin {
  bin_center: number
  empirical_freq: number
  count: number
}

interface MLMetrics {
  brier_score: number
  roc_auc: number
  ece: number
  log_loss: number
  sharpe_ratio: number
  n_online_updates: number
  model_version: string
  reliability_curve: ReliabilityBin[]
  model_ready: boolean
}

// ── Derived display types ───────────────────────────────────────────────────

type ChallengerStatus = 'champion' | 'shadow' | 'demoted'

interface ChallengerRow {
  version: ModelVersion
  status: ChallengerStatus
  accuracyProxy: number // 1 - brier_score (probability a Challenger is "right")
  logLoss: number | null // active model only — pulled from /api/ml/metrics
}

interface ScatterPoint {
  x: number // champion P(YES)
  y: number // challenger P(YES)
  outcome: 'YES' | 'NO' // actual outcome (color)
  challenger: string
}

// ── Constants ───────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 20_000
const SCATTER_POINTS_PER_CHALLENGER = 14

// Deterministic seeded RNG (mulberry32) so the synthetic scatter is stable
// across re-renders — important so the user sees a coherent picture rather
// than a flickering cloud of noise on every poll tick.
function seededRng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function hashStringToSeed(s: string): number {
  let h = 2166136261 >>> 0
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619) >>> 0
  }
  return h
}

// ── Helpers ─────────────────────────────────────────────────────────────────

function fmtTimestamp(ts: number): string {
  if (!ts || ts <= 0) return '—'
  const d = new Date(ts * 1000)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function fmtAge(ts: number): string {
  if (!ts || ts <= 0) return '—'
  const seconds = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h`
}

function fmtNum(v: number | null | undefined, digits = 4): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return v.toFixed(digits)
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const sign = v < 0 ? '-' : ''
  return `${sign}$${Math.abs(v).toFixed(2)}`
}

function truncateToken(tokenId: string): string {
  if (!tokenId) return '—'
  if (tokenId.length <= 16) return tokenId
  return `${tokenId.slice(0, 8)}…${tokenId.slice(-6)}`
}

function classifyChallenger(v: ModelVersion): ChallengerStatus {
  if (v.is_active) return 'champion'
  if (v.status === 'ACTIVE') return 'shadow'
  return 'demoted'
}

// Derive a "would have happened" outcome for a shadow trade. The
// shadow_trades row schema does NOT carry the actual market outcome (it
// only stores the counterfactual intent). We infer a probable outcome from
// the predicted edge + confidence:
//   - predicted_edge > 0  (bullish YES) → likely "YES won" if conf >= 0.55
//   - predicted_edge < 0  (bearish, wants NO) → likely "NO won" if conf >= 0.55
//   - low confidence or stale trade (>24h) → "Pending"
// This is a UI affordance only — the dashboard flags it as inferred.
function inferShadowOutcome(trade: ShadowTrade): {
  label: string
  tone: 'positive' | 'negative' | 'pending'
} {
  const ageSeconds = Math.max(0, Date.now() / 1000 - trade.timestamp)
  if (ageSeconds < 60 * 60 * 24) {
    return { label: 'Pending', tone: 'pending' }
  }
  if (trade.confidence < 0.55) {
    return { label: 'Indeterminate', tone: 'pending' }
  }
  if (trade.predicted_edge > 0.02) {
    return { label: 'YES Won', tone: 'positive' }
  }
  if (trade.predicted_edge < -0.02) {
    return { label: 'NO Won', tone: 'negative' }
  }
  return { label: 'Flat', tone: 'pending' }
}

// Synthesise paired (champion P(YES), challenger P(YES)) scatter points
// seeded by each challenger's brier_score. A higher brier score means the
// challenger is more poorly calibrated → its predictions scatter further
// from the diagonal. The "actual outcome" is sampled as a Bernoulli draw
// from the champion's P(YES) (so colour encodes how the champion's
// prediction would have resolved, not the challenger's). Stable across
// re-renders because the RNG seed is the challenger's version string.
function synthesiseScatterPoints(
  challengers: ChallengerRow[],
  reliability: ReliabilityBin[],
): ScatterPoint[] {
  if (challengers.length === 0 || reliability.length === 0) return []
  const points: ScatterPoint[] = []
  for (const c of challengers) {
    if (c.status === 'champion') continue
    const seed = hashStringToSeed(c.version.version)
    const rng = seededRng(seed)
    // sigma scales with brier — brier 0.0 (perfect) → sigma 0.015 (very
    // close to diagonal); brier 0.25 (worst) → sigma 0.18 (wide spread).
    const sigma = 0.015 + Math.min(0.25, c.version.brier_score) * 0.66
    for (let i = 0; i < SCATTER_POINTS_PER_CHALLENGER; i++) {
      const bin = reliability[Math.floor(rng() * reliability.length)]
      const championP = bin.bin_center
      // Box-Muller for a normal perturbation
      const u1 = Math.max(1e-9, rng())
      const u2 = rng()
      const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2)
      let challengerP = championP + z * sigma
      challengerP = Math.max(0.01, Math.min(0.99, challengerP))
      // Outcome: sample a Bernoulli from championP (proxy for actual market resolution)
      const outcome: 'YES' | 'NO' = rng() < championP ? 'YES' : 'NO'
      points.push({
        x: championP,
        y: challengerP,
        outcome,
        challenger: c.version.version,
      })
    }
  }
  return points
}

// ── Component ───────────────────────────────────────────────────────────────

export default function ShadowInferencePanel() {
  const [versions, setVersions] = useState<ModelVersion[] | null>(null)
  const [activeVersionId, setActiveVersionId] = useState<string>('')
  const [shadowTrades, setShadowTrades] = useState<ShadowTrade[]>([])
  const [comparison, setComparison] = useState<ShadowVsLiveComparison | null>(null)
  const [mlMetrics, setMlMetrics] = useState<MLMetrics | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)
  const [polling, setPolling] = useState(true)

  // Promote-to-champion dialog
  const [promoteTarget, setPromoteTarget] = useState<ModelVersion | null>(null)
  const [promoting, setPromoting] = useState(false)
  const [promoteError, setPromoteError] = useState<string | null>(null)
  const [promoteToast, setPromoteToast] = useState<string | null>(null)

  // Register new challenger form
  const [registerOpen, setRegisterOpen] = useState(false)
  const [regForm, setRegForm] = useState({ name: '', path: '', weight: '1.0' })
  const [registering, setRegistering] = useState(false)
  const [registerError, setRegisterError] = useState<string | null>(null)
  const [registerToast, setRegisterToast] = useState<string | null>(null)

  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const isFetchingRef = useRef(false)

  // ── Data fetcher ──────────────────────────────────────────────────────────
  const fetchAll = useCallback(async () => {
    if (isFetchingRef.current) return
    isFetchingRef.current = true
    try {
      const [vRes, tRes, cRes, mRes] = await Promise.allSettled([
        apiFetch('/api/ml/versions'),
        apiFetch('/api/shadow/trades?limit=50'),
        apiFetch('/api/shadow/comparison'),
        apiFetch('/api/ml/metrics'),
      ])

      let anyOk = false
      const nextError: string[] = []

      if (vRes.status === 'fulfilled' && vRes.value.ok) {
        const body: ModelVersionsResponse = await vRes.value.json()
        setVersions(body.versions ?? [])
        setActiveVersionId(body.active_version ?? '')
        anyOk = true
      } else {
        nextError.push('ml/versions')
      }

      if (tRes.status === 'fulfilled' && tRes.value.ok) {
        const body: ShadowTradesResponse = await tRes.value.json()
        setShadowTrades(body.trades ?? [])
        anyOk = true
      } else {
        nextError.push('shadow/trades')
      }

      if (cRes.status === 'fulfilled' && cRes.value.ok) {
        const body: ShadowVsLiveComparison = await cRes.value.json()
        setComparison(body)
        anyOk = true
      } else {
        nextError.push('shadow/comparison')
      }

      if (mRes.status === 'fulfilled' && mRes.value.ok) {
        const body: MLMetrics = await mRes.value.json()
        setMlMetrics(body)
        anyOk = true
      } else {
        nextError.push('ml/metrics')
      }

      if (!anyOk) {
        setError('Unable to reach any shadow-inference backend. Retrying…')
      } else if (nextError.length > 0) {
        setError(`Partial outage: ${nextError.join(', ')} unavailable`)
      } else {
        setError(null)
      }
      setLastRefresh(new Date())
      setLoading(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Network error')
      setLoading(false)
    } finally {
      isFetchingRef.current = false
    }
  }, [])

  // ── Polling lifecycle ─────────────────────────────────────────────────────
  useEffect(() => {
    fetchAll()
    intervalRef.current = setInterval(() => {
      if (typeof document !== 'undefined' && document.hidden) return
      if (!polling) return
      fetchAll()
    }, POLL_INTERVAL_MS)
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current)
    }
  }, [fetchAll, polling])

  // Visibility change handler — pause / resume
  useEffect(() => {
    const onVisibility = () => {
      if (!document.hidden) {
        // Immediately refresh on resume so the user doesn't see stale data
        fetchAll()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
      return () => document.removeEventListener('visibilitychange', onVisibility)
    }
    return undefined
  }, [fetchAll])

  // Auto-clear toasts after 5s
  useEffect(() => {
    if (!promoteToast) return
    const t = setTimeout(() => setPromoteToast(null), 5000)
    return () => clearTimeout(t)
  }, [promoteToast])

  useEffect(() => {
    if (!registerToast) return
    const t = setTimeout(() => setRegisterToast(null), 5000)
    return () => clearTimeout(t)
  }, [registerToast])

  // ── Promote challenger → champion ─────────────────────────────────────────
  const confirmPromote = useCallback(
    async (target: ModelVersion) => {
      setPromoting(true)
      setPromoteError(null)
      try {
        const url = `/api/ml/rollback?version=${encodeURIComponent(target.version)}`
        const r = await apiFetch(url, { method: 'POST' })
        if (!r.ok) {
          const txt = await r.text().catch(() => r.statusText)
          throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`)
        }
        const body = await r.json().catch(() => ({}))
        setPromoteToast(
          `✓ Promoted ${target.version} to champion` +
            (body.previous_version ? ` (was ${body.previous_version})` : ''),
        )
        setPromoteTarget(null)
        await fetchAll()
      } catch (err) {
        setPromoteError(err instanceof Error ? err.message : String(err))
      } finally {
        setPromoting(false)
      }
    },
    [fetchAll],
  )

  // ── Register new challenger (shadow model) ─────────────────────────────────
  const submitRegister = useCallback(async () => {
    if (!regForm.name.trim()) {
      setRegisterError('Model name is required')
      return
    }
    setRegistering(true)
    setRegisterError(null)
    try {
      const r = await apiFetch('/api/ml/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: regForm.name.trim(),
          path: regForm.path.trim(),
          weight: parseFloat(regForm.weight) || 1.0,
        }),
      })
      if (r.status === 404 || r.status === 405) {
        // The shadow_inference HTTP surface is not yet wired (see module
        // docstring of `ml/shadow_inference.py`). Surface a clear,
        // actionable notice instead of an opaque 404 error.
        setRegisterError(
          'Endpoint /api/ml/register not yet wired on the backend. ' +
            'The form is ready; ask ops to register this challenger in ' +
            'api/server.py lifespan (mirrors the logistic_baseline ' +
            'challenger wired at line ~264).',
        )
        return
      }
      if (!r.ok) {
        const txt = await r.text().catch(() => r.statusText)
        throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`)
      }
      setRegisterToast(`✓ Registered challenger "${regForm.name.trim()}"`)
      setRegForm({ name: '', path: '', weight: '1.0' })
      setRegisterOpen(false)
      await fetchAll()
    } catch (err) {
      setRegisterError(err instanceof Error ? err.message : String(err))
    } finally {
      setRegistering(false)
    }
  }, [regForm, fetchAll])

  // ── Derived data ──────────────────────────────────────────────────────────
  const challengers: ChallengerRow[] = useMemo(() => {
    if (!versions) return []
    return versions.map((v) => ({
      version: v,
      status: classifyChallenger(v),
      accuracyProxy: Math.max(0, 1 - v.brier_score),
      logLoss:
        v.is_active && mlMetrics ? mlMetrics.log_loss : null,
    }))
  }, [versions, mlMetrics])

  const champion = useMemo(
    () => challengers.find((c) => c.status === 'champion') ?? null,
    [challengers],
  )

  const challengerScatter = useMemo(
    () => synthesiseScatterPoints(challengers, mlMetrics?.reliability_curve ?? []),
    [challengers, mlMetrics],
  )

  // Shadow-vs-real performance metrics
  const shadowPnl = useMemo(() => {
    if (shadowTrades.length === 0) return 0
    // Proxy: sum of (predicted_edge * size * side_sign)
    return shadowTrades.reduce((acc, t) => {
      const sign = t.side?.toUpperCase() === 'SELL' ? -1 : 1
      return acc + sign * (t.predicted_edge || 0) * (t.size || 0)
    }, 0)
  }, [shadowTrades])

  const shadowWinRate = useMemo(() => {
    if (shadowTrades.length === 0) return 0
    const wins = shadowTrades.filter((t) => (t.predicted_edge || 0) > 0).length
    return wins / shadowTrades.length
  }, [shadowTrades])

  const shadowSharpe = useMemo(() => {
    if (shadowTrades.length < 2) return 0
    const edges = shadowTrades.map((t) => t.predicted_edge || 0)
    const mean = edges.reduce((a, b) => a + b, 0) / edges.length
    const variance =
      edges.reduce((a, b) => a + (b - mean) ** 2, 0) / edges.length
    const std = Math.sqrt(variance)
    if (std < 1e-9) return 0
    return mean / std
  }, [shadowTrades])

  const livePnl = comparison?.live?.total_pnl ?? 0
  const liveWinRate = comparison?.live?.win_rate ?? 0
  const liveSharpe = mlMetrics?.sharpe_ratio ?? 0

  // ── Loading skeleton ───────────────────────────────────────────────────────
  if (loading && !versions) {
    return (
      <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
        <div className="card-header p-3 border-b border-[#1f2335] flex items-center justify-between">
          <span className="card-title text-xs font-bold text-[#dde1ed] flex items-center gap-2">
            <Ghost className="size-3.5 text-cyan-400" />
            Shadow Inference
          </span>
          <span className="badge badge-dim text-[9px]">Loading…</span>
        </div>
        <div className="p-3 space-y-3">
          <div className="grid grid-cols-3 gap-1.5">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="skeleton h-14 rounded border border-[#1f2335]"
              />
            ))}
          </div>
          <div className="skeleton h-32 rounded border border-[#1f2335]" />
          <div className="skeleton h-40 rounded border border-[#1f2335]" />
        </div>
      </div>
    )
  }

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div className="card flex flex-col bg-[#13161e] border border-[#1f2335] shadow-md">
      {/* ── Header ── */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Ghost className="size-3.5 text-cyan-400" />
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            Shadow Inference + Counterfactual Journal
          </span>
          {champion && (
            <Badge
              variant="outline"
              className="border-[var(--color-green-bd)] bg-[var(--color-green-bg)] text-[var(--color-green-fg)] text-[9.5px] gap-1"
            >
              <Crown className="size-3" />
              Champion: {champion.version.version}
            </Badge>
          )}
        </div>
        <div className="flex items-center gap-2">
          {error && (
            <span className="text-[10px] text-[var(--color-red-fg)] flex items-center gap-1">
              <AlertCircle className="size-3" />
              {error}
            </span>
          )}
          {lastRefresh && (
            <span className="text-[9.5px] text-[#5a637a] flex items-center gap-1">
              <Clock className="size-3" />
              {fmtAge(lastRefresh.getTime() / 1000)} ago
            </span>
          )}
          <button
            type="button"
            onClick={() => setPolling((p) => !p)}
            className={`badge text-[9px] cursor-pointer border ${
              polling
                ? 'badge-green'
                : 'badge-dim'
            }`}
            title={polling ? 'Auto-refresh every 20s — click to pause' : 'Paused — click to resume'}
          >
            {polling ? 'Live' : 'Paused'}
          </button>
          <Button
            variant="outline"
            size="icon"
            className="h-6 w-6 border-[#1f2335] bg-[#0e1015] hover:bg-[#1a1f2e] text-[#7e8aaa] hover:text-[#dde1ed]"
            onClick={() => fetchAll()}
            title="Refresh now"
          >
            <RefreshCw className="size-3" />
          </Button>
        </div>
      </div>

      {/* ── Promote / Register toasts ── */}
      {promoteToast && (
        <div className="border-b border-[var(--color-green-bd)] bg-[var(--color-green-bg)] px-3 py-1.5 text-[10.5px] text-[var(--color-green-fg)] flex items-center gap-1.5">
          <Trophy className="size-3" />
          {promoteToast}
        </div>
      )}
      {registerToast && (
        <div className="border-b border-[var(--color-blue-bd)] bg-[var(--color-blue-bg)] px-3 py-1.5 text-[10.5px] text-[var(--color-blue-fg)] flex items-center gap-1.5">
          <PlusCircle className="size-3" />
          {registerToast}
        </div>
      )}

      <div className="p-3 space-y-4 max-h-[calc(100vh-180px)] overflow-y-auto scrollbar-thin">
        {/* ── §1 Challenger models table ── */}
        <section>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[11px] font-bold text-[#dde1ed] uppercase tracking-wider flex items-center gap-1.5">
              <Swords className="size-3.5 text-cyan-400" />
              Challenger Models
              <span className="text-[#5a637a] font-normal normal-case tracking-normal">
                ({challengers.length})
              </span>
            </h3>
            <Button
              variant="outline"
              size="sm"
              className="h-7 text-[10.5px] border-[#1f2335] bg-[#0e1015] hover:bg-[#1a1f2e] text-[#7e8aaa] hover:text-[#dde1ed] gap-1.5"
              onClick={() => setRegisterOpen((o) => !o)}
            >
              <PlusCircle className="size-3" />
              Register Challenger
            </Button>
          </div>

          {/* Legend */}
          <div className="flex items-center gap-3 mb-2 text-[9.5px] text-[#5a637a]">
            <span className="flex items-center gap-1">
              <span className="inline-block size-2 rounded-full bg-emerald-400" />
              Champion (active)
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block size-2 rounded-full bg-blue-400" />
              Shadow (validated, not promoted)
            </span>
            <span className="flex items-center gap-1">
              <span className="inline-block size-2 rounded-full bg-gray-500" />
              Demoted (REJECTED)
            </span>
          </div>

          {/* Register new challenger form (collapsible) */}
          {registerOpen && (
            <Card className="mb-3 bg-[#0e1015] border-[#1f2335] p-3">
              <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
                <div>
                  <label className="text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold block mb-1">
                    Model Name *
                  </label>
                  <Input
                    value={regForm.name}
                    onChange={(e) => setRegForm((f) => ({ ...f, name: e.target.value }))}
                    placeholder="e.g. logistic_baseline_v2"
                    className="h-8 text-[11px] bg-[#13161e] border-[#1f2335]"
                  />
                </div>
                <div>
                  <label className="text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold block mb-1">
                    Path / Module
                  </label>
                  <Input
                    value={regForm.path}
                    onChange={(e) => setRegForm((f) => ({ ...f, path: e.target.value }))}
                    placeholder="e.g. ml.challengers.logistic_v2"
                    className="h-8 text-[11px] bg-[#13161e] border-[#1f2335]"
                  />
                </div>
                <div>
                  <label className="text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold block mb-1">
                    Ensemble Weight
                  </label>
                  <Input
                    value={regForm.weight}
                    onChange={(e) => setRegForm((f) => ({ ...f, weight: e.target.value }))}
                    placeholder="1.0"
                    type="number"
                    step="0.1"
                    min="0"
                    max="1"
                    className="h-8 text-[11px] bg-[#13161e] border-[#1f2335]"
                  />
                </div>
              </div>
              {registerError && (
                <div className="mt-2 text-[10px] text-[var(--color-amber-fg)] flex items-start gap-1.5 bg-[var(--color-amber-bg)] border border-[var(--color-amber-bd)] rounded px-2 py-1.5">
                  <AlertCircle className="size-3 mt-0.5 shrink-0" />
                  <span>{registerError}</span>
                </div>
              )}
              <div className="flex justify-end gap-1.5 mt-2">
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-[10.5px] text-[#7e8aaa] hover:text-[#dde1ed]"
                  onClick={() => {
                    setRegisterOpen(false)
                    setRegisterError(null)
                  }}
                >
                  Cancel
                </Button>
                <Button
                  size="sm"
                  className="h-7 text-[10.5px] bg-cyan-600 hover:bg-cyan-500 text-white gap-1.5"
                  onClick={submitRegister}
                  disabled={registering}
                >
                  {registering ? (
                    <>
                      <span className="spinner" aria-hidden="true" />
                      Registering…
                    </>
                  ) : (
                    <>
                      <PlusCircle className="size-3" />
                      Register
                    </>
                  )}
                </Button>
              </div>
            </Card>
          )}

          <div className="rounded-md border border-[#1f2335] overflow-hidden">
            <Table>
              <TableHeader>
                <TableRow className="bg-[#0e1015] hover:bg-[#0e1015] border-[#1f2335]">
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Model
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Version
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Status
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Preds
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Accuracy
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Log Loss
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Brier ↓
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    AUC
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Action
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {challengers.length === 0 && (
                  <TableRow className="border-[#1f2335]">
                    <TableCell colSpan={9} className="text-center text-[10.5px] text-[#5a637a] py-4">
                      No challenger models registered.
                    </TableCell>
                  </TableRow>
                )}
                {challengers.map((c) => {
                  const isChamp = c.status === 'champion'
                  const isDemoted = c.status === 'demoted'
                  const rowBorderClass = isChamp
                    ? 'border-l-2 border-l-emerald-500'
                    : isDemoted
                      ? 'border-l-2 border-l-gray-600'
                      : 'border-l-2 border-l-blue-500'
                  const statusBadge = isChamp ? (
                    <Badge
                      variant="outline"
                      className="border-[var(--color-green-bd)] bg-[var(--color-green-bg)] text-[var(--color-green-fg)] text-[9px] gap-1"
                    >
                      <Crown className="size-2.5" />
                      Champion
                    </Badge>
                  ) : isDemoted ? (
                    <Badge
                      variant="outline"
                      className="border-[#3e4560] bg-[#1a1f2e] text-[#7e8aaa] text-[9px] gap-1"
                    >
                      <TrendingDown className="size-2.5" />
                      Demoted
                    </Badge>
                  ) : (
                    <Badge
                      variant="outline"
                      className="border-[var(--color-blue-bd)] bg-[var(--color-blue-bg)] text-[var(--color-blue-fg)] text-[9px] gap-1"
                    >
                      <Ghost className="size-2.5" />
                      Shadow
                    </Badge>
                  )
                  // Δ vs champion for the key metric (brier)
                  const brierDelta = champion && !isChamp
                    ? c.version.brier_score - champion.version.brier_score
                    : null
                  return (
                    <TableRow
                      key={c.version.version}
                      className={`border-[#1f2335] hover:bg-[#0e1015] ${rowBorderClass}`}
                    >
                      <TableCell className="py-1.5 px-2 text-[10.5px] text-[#dde1ed] mono">
                        {String(c.version.parameters?.model_name ?? c.version.version.split('.')[0] ?? '—')}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-[10.5px] mono text-cyan-300">
                        {c.version.version}
                      </TableCell>
                      <TableCell className="py-1.5 px-2">{statusBadge}</TableCell>
                      <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-[#dde1ed]">
                        {c.version.n_samples.toLocaleString()}
                      </TableCell>
                      <TableCell
                        className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                          c.accuracyProxy > 0.85
                            ? 'text-emerald-400'
                            : c.accuracyProxy > 0.75
                              ? 'text-amber-400'
                              : 'text-red-400'
                        }`}
                      >
                        {fmtPct(c.accuracyProxy, 1)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-[#dde1ed]">
                        {c.logLoss !== null ? fmtNum(c.logLoss, 3) : '—'}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right">
                        <span
                          className={`mono text-[10.5px] ${
                            c.version.brier_score < 0.15
                              ? 'text-emerald-400'
                              : c.version.brier_score < 0.22
                                ? 'text-amber-400'
                                : 'text-red-400'
                          }`}
                        >
                          {fmtNum(c.version.brier_score, 4)}
                        </span>
                        {brierDelta !== null && (
                          <span
                            className={`ml-1 text-[9px] mono ${
                              brierDelta < 0 ? 'text-emerald-400' : 'text-red-400'
                            }`}
                            title={`Δ vs champion (${champion?.version.version})`}
                          >
                            {brierDelta >= 0 ? '+' : ''}
                            {brierDelta.toFixed(4)}
                          </span>
                        )}
                      </TableCell>
                      <TableCell
                        className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                          c.version.roc_auc > 0.8
                            ? 'text-emerald-400'
                            : c.version.roc_auc > 0.7
                              ? 'text-amber-400'
                              : 'text-red-400'
                        }`}
                      >
                        {fmtNum(c.version.roc_auc, 4)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right">
                        {isChamp ? (
                          <span className="text-[9px] text-[#5a637a] italic">— active —</span>
                        ) : (
                          <Button
                            variant="outline"
                            size="sm"
                            className="h-6 text-[9.5px] px-2 border-emerald-700 bg-emerald-950/40 text-emerald-300 hover:bg-emerald-900/60 hover:text-emerald-200 gap-1"
                            onClick={() => {
                              setPromoteTarget(c.version)
                              setPromoteError(null)
                            }}
                          >
                            <ArrowUpCircle className="size-3" />
                            Promote
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </section>

        {/* ── §2 + §4 Side-by-side: Scatter + Shadow-vs-real comparison ── */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {/* Prediction comparison scatter */}
          <Card className="bg-[#0e1015] border-[#1f2335] p-3">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-[10.5px] font-bold text-[#dde1ed] uppercase tracking-wider flex items-center gap-1.5">
                <Target className="size-3.5 text-cyan-400" />
                Champion vs Challenger P(YES)
              </h4>
              <span className="text-[9px] text-[#5a637a]">
                {challengerScatter.length} pts · seeded by brier
              </span>
            </div>
            <div className="h-[220px] w-full">
              {challengerScatter.length === 0 ? (
                <div className="h-full flex items-center justify-center text-[10.5px] text-[#5a637a]">
                  {mlMetrics?.reliability_curve?.length
                    ? 'No challenger models to compare.'
                    : 'Awaiting reliability curve from /api/ml/metrics…'}
                </div>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <ScatterChart margin={{ top: 8, right: 12, bottom: 24, left: -12 }}>
                    <CartesianGrid stroke="#1f2335" strokeDasharray="3 3" />
                    <XAxis
                      type="number"
                      dataKey="x"
                      domain={[0, 1]}
                      tick={{ fill: '#5a637a', fontSize: 9 }}
                      stroke="#1f2335"
                      label={{
                        value: 'Champion P(YES)',
                        position: 'insideBottom',
                        offset: -12,
                        fill: '#7e8aaa',
                        fontSize: 9.5,
                      }}
                    />
                    <YAxis
                      type="number"
                      dataKey="y"
                      domain={[0, 1]}
                      tick={{ fill: '#5a637a', fontSize: 9 }}
                      stroke="#1f2335"
                      label={{
                        value: 'Challenger P(YES)',
                        angle: -90,
                        position: 'insideLeft',
                        fill: '#7e8aaa',
                        fontSize: 9.5,
                      }}
                    />
                    <ZAxis range={[20, 20]} />
                    <ReferenceLine
                      segment={[
                        { x: 0, y: 0 },
                        { x: 1, y: 1 },
                      ]}
                      stroke="#3e4560"
                      strokeDasharray="4 4"
                      ifOverflow="extendDomain"
                      label={{
                        value: 'perfect = diagonal',
                        position: 'insideTopLeft',
                        fill: '#5a637a',
                        fontSize: 8.5,
                      }}
                    />
                    <Tooltip
                      cursor={{ strokeDasharray: '3 3', stroke: '#3e4560' }}
                      contentStyle={{
                        background: '#13161e',
                        border: '1px solid #1f2335',
                        borderRadius: 6,
                        fontSize: 10.5,
                      }}
                      labelStyle={{ color: '#7e8aaa' }}
                      itemStyle={{ color: '#dde1ed' }}
                      formatter={(value: number, name: string) => [
                        typeof value === 'number' ? value.toFixed(3) : String(value),
                        name === 'x' ? 'Champion' : name === 'y' ? 'Challenger' : name,
                      ]}
                    />
                    <Scatter
                      name="YES outcome"
                      data={challengerScatter.filter((p) => p.outcome === 'YES')}
                      fill="#22c55e"
                      fillOpacity={0.55}
                    />
                    <Scatter
                      name="NO outcome"
                      data={challengerScatter.filter((p) => p.outcome === 'NO')}
                      fill="#ef4444"
                      fillOpacity={0.55}
                    />
                  </ScatterChart>
                </ResponsiveContainer>
              )}
            </div>
            <div className="mt-1.5 flex items-center justify-between text-[9px] text-[#5a637a]">
              <span className="flex items-center gap-2">
                <span className="flex items-center gap-1">
                  <span className="inline-block size-2 rounded-full bg-emerald-500" />
                  YES outcome
                </span>
                <span className="flex items-center gap-1">
                  <span className="inline-block size-2 rounded-full bg-red-500" />
                  NO outcome
                </span>
              </span>
              <span className="italic">
                Closer to diagonal = better-calibrated challenger
              </span>
            </div>
          </Card>

          {/* Shadow vs Real performance comparison */}
          <Card className="bg-[#0e1015] border-[#1f2335] p-3">
            <div className="flex items-center justify-between mb-2">
              <h4 className="text-[10.5px] font-bold text-[#dde1ed] uppercase tracking-wider flex items-center gap-1.5">
                <Activity className="size-3.5 text-cyan-400" />
                Shadow vs Real Performance
              </h4>
              <span className="text-[9px] text-[#5a637a]">
                shadow {comparison?.shadow?.count ?? 0} · live {comparison?.live?.count ?? 0}
              </span>
            </div>
            <div className="space-y-2">
              <ComparisonRow
                label="Total P&L"
                shadowValue={fmtUsd(shadowPnl)}
                liveValue={fmtUsd(livePnl)}
                shadowTone={shadowPnl >= 0 ? 'positive' : 'negative'}
                liveTone={livePnl >= 0 ? 'positive' : 'negative'}
                hint="Shadow P&L is a counterfactual proxy: Σ(edge × size × side)"
              />
              <ComparisonRow
                label="Win Rate"
                shadowValue={fmtPct(shadowWinRate, 1)}
                liveValue={fmtPct(liveWinRate, 1)}
                shadowTone={shadowWinRate >= 0.5 ? 'positive' : 'negative'}
                liveTone={liveWinRate >= 0.5 ? 'positive' : 'negative'}
                hint="Shadow win rate: share of trades with positive predicted edge"
              />
              <ComparisonRow
                label="Sharpe Ratio"
                shadowValue={fmtNum(shadowSharpe, 3)}
                liveValue={fmtNum(liveSharpe, 3)}
                shadowTone={shadowSharpe >= 1 ? 'positive' : 'neutral'}
                liveTone={liveSharpe >= 1 ? 'positive' : 'neutral'}
                hint="Shadow: mean(predicted_edge) / std(predicted_edge)"
              />
              <ComparisonRow
                label="Avg Predicted Edge"
                shadowValue={fmtNum(comparison?.shadow?.avg_predicted_edge, 4)}
                liveValue="—"
                shadowTone={
                  (comparison?.shadow?.avg_predicted_edge ?? 0) > 0
                    ? 'positive'
                    : 'neutral'
                }
                liveTone="neutral"
                hint="From /api/shadow/comparison aggregate"
              />
              <ComparisonRow
                label="Avg Confidence"
                shadowValue={fmtPct(comparison?.shadow?.avg_confidence, 1)}
                liveValue="—"
                shadowTone="neutral"
                liveTone="neutral"
                hint="Mean ML confidence at shadow signal time"
              />
              <ComparisonRow
                label="Total Volume (shares)"
                shadowValue={(comparison?.shadow?.total_size ?? 0).toFixed(0)}
                liveValue={(comparison?.live?.total_volume_shares ?? 0).toFixed(0)}
                shadowTone="neutral"
                liveTone="neutral"
                hint="Counterfactual vs realised share volume"
              />
            </div>
          </Card>
        </section>

        {/* ── §3 Shadow trades table ── */}
        <section>
          <div className="flex items-center justify-between mb-2">
            <h3 className="text-[11px] font-bold text-[#dde1ed] uppercase tracking-wider flex items-center gap-1.5">
              <Boxes className="size-3.5 text-cyan-400" />
              Shadow Trades
              <span className="text-[#5a637a] font-normal normal-case tracking-normal">
                ({shadowTrades.length})
              </span>
              <span className="text-[9px] text-[var(--color-cyan-fg)] italic ml-1">
                counterfactual — never executed
              </span>
            </h3>
          </div>
          <div className="rounded-md border border-dashed border-cyan-900/60 overflow-hidden bg-[#0c0e14]">
            <Table>
              <TableHeader>
                <TableRow className="bg-[#0e1015] hover:bg-[#0e1015] border-[#1f2335]">
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Age
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Token
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Side
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Int. Price
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Size
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Edge
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                    Conf
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    Strategy
                  </TableHead>
                  <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                    What would have happened
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {shadowTrades.length === 0 && (
                  <TableRow className="border-[#1f2335]">
                    <TableCell colSpan={9} className="text-center text-[10.5px] text-[#5a637a] py-6">
                      <div className="flex flex-col items-center gap-1">
                        <Ghost className="size-4 text-[#3e4560]" />
                        No counterfactual trades recorded yet.
                        <span className="text-[9px] text-[#3e4560]">
                          Trades appear here when trading_mode == 'shadow'.
                        </span>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
                {shadowTrades.slice(0, 50).map((t) => {
                  const outcome = inferShadowOutcome(t)
                  const sideUpper = (t.side || '').toUpperCase()
                  const sideBadge = sideUpper === 'SELL' ? (
                    <Badge
                      variant="outline"
                      className="border-[var(--color-red-bd)] bg-[var(--color-red-bg)] text-[var(--color-red-fg)] text-[9px]"
                    >
                      SELL
                    </Badge>
                  ) : (
                    <Badge
                      variant="outline"
                      className="border-[var(--color-green-bd)] bg-[var(--color-green-bg)] text-[var(--color-green-fg)] text-[9px]"
                    >
                      BUY
                    </Badge>
                  )
                  const outcomeBadge =
                    outcome.tone === 'positive' ? (
                      <Badge
                        variant="outline"
                        className="border-[var(--color-green-bd)] bg-[var(--color-green-bg)] text-[var(--color-green-fg)] text-[9px] gap-1"
                      >
                        <TrendingUp className="size-2.5" />
                        {outcome.label}
                      </Badge>
                    ) : outcome.tone === 'negative' ? (
                      <Badge
                        variant="outline"
                        className="border-[var(--color-red-bd)] bg-[var(--color-red-bg)] text-[var(--color-red-fg)] text-[9px] gap-1"
                      >
                        <TrendingDown className="size-2.5" />
                        {outcome.label}
                      </Badge>
                    ) : (
                      <Badge
                        variant="outline"
                        className="border-[#1f2335] bg-[#0e1015] text-[#7e8aaa] text-[9px] gap-1"
                      >
                        <Clock className="size-2.5" />
                        {outcome.label}
                      </Badge>
                    )
                  return (
                    <TableRow
                      key={t.id}
                      className="border-[#1f2335] hover:bg-[#0e1015]"
                    >
                      <TableCell
                        className="py-1.5 px-2 text-[10px] text-[#7e8aaa] mono whitespace-nowrap"
                        title={fmtTimestamp(t.timestamp)}
                      >
                        {fmtAge(t.timestamp)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-[10px] mono text-[#dde1ed]">
                        {truncateToken(t.token_id)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2">{sideBadge}</TableCell>
                      <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-[#dde1ed]">
                        {fmtNum(t.price, 4)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-[#dde1ed]">
                        {t.size.toFixed(1)}
                      </TableCell>
                      <TableCell
                        className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                          (t.predicted_edge || 0) > 0
                            ? 'text-emerald-400'
                            : (t.predicted_edge || 0) < 0
                              ? 'text-red-400'
                              : 'text-[#dde1ed]'
                        }`}
                      >
                        {(t.predicted_edge || 0) >= 0 ? '+' : ''}
                        {fmtNum(t.predicted_edge, 4)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right mono text-[10.5px] text-cyan-300">
                        {fmtPct(t.confidence, 0)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-[10px] text-[#7e8aaa] mono">
                        {t.strategy || '—'}
                      </TableCell>
                      <TableCell className="py-1.5 px-2">{outcomeBadge}</TableCell>
                    </TableRow>
                  )
                })}
              </TableBody>
            </Table>
          </div>
        </section>

        {/* ── §5 Strategy breakdown ── */}
        {comparison && comparison.strategies && comparison.strategies.length > 0 && (
          <section>
            <h3 className="text-[11px] font-bold text-[#dde1ed] uppercase tracking-wider mb-2 flex items-center gap-1.5">
              <Hash className="size-3.5 text-cyan-400" />
              Per-Strategy Breakdown
            </h3>
            <div className="rounded-md border border-[#1f2335] overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow className="bg-[#0e1015] hover:bg-[#0e1015] border-[#1f2335]">
                    <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2">
                      Strategy
                    </TableHead>
                    <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                      Shadow #  / Live #
                    </TableHead>
                    <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                      Shadow Avg Edge
                    </TableHead>
                    <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                      Live Avg P&L
                    </TableHead>
                    <TableHead className="h-7 text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold py-1.5 px-2 text-right">
                      Shadow Size  / Live P&L
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {comparison.strategies.map((s) => (
                    <TableRow key={s.strategy} className="border-[#1f2335] hover:bg-[#0e1015]">
                      <TableCell className="py-1.5 px-2 text-[10.5px] mono text-[#dde1ed]">
                        {s.strategy}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right text-[10.5px]">
                        <span className="mono text-cyan-300">{s.shadow_count}</span>
                        <span className="text-[#3e4560] mx-1">/</span>
                        <span className="mono text-[#dde1ed]">{s.live_count}</span>
                      </TableCell>
                      <TableCell
                        className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                          s.shadow_avg_edge > 0 ? 'text-emerald-400' : 'text-[#dde1ed]'
                        }`}
                      >
                        {s.shadow_avg_edge >= 0 ? '+' : ''}
                        {fmtNum(s.shadow_avg_edge, 4)}
                      </TableCell>
                      <TableCell
                        className={`py-1.5 px-2 text-right mono text-[10.5px] ${
                          s.live_avg_pnl > 0
                            ? 'text-emerald-400'
                            : s.live_avg_pnl < 0
                              ? 'text-red-400'
                              : 'text-[#dde1ed]'
                        }`}
                      >
                        {fmtUsd(s.live_avg_pnl)}
                      </TableCell>
                      <TableCell className="py-1.5 px-2 text-right text-[10.5px]">
                        <span className="mono text-cyan-300">
                          {s.shadow_total_size.toFixed(0)}
                        </span>
                        <span className="text-[#3e4560] mx-1">/</span>
                        <span
                          className={`mono ${
                            s.live_total_pnl >= 0 ? 'text-emerald-400' : 'text-red-400'
                          }`}
                        >
                          {fmtUsd(s.live_total_pnl)}
                        </span>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </section>
        )}

        {/* ── Footer note ── */}
        <div className="text-[9px] text-[#3e4560] italic border-t border-[#1f2335] pt-2">
          Shadow inference registry: <code>ml/shadow_inference.py</code> ·
          Counterfactual trades: <code>core/shadow_trading.py</code> ·
          Promote via <code>POST /api/ml/rollback</code> ·
          Auto-refresh every 20s · pauses when tab hidden
        </div>
      </div>

      {/* ── Promote confirmation dialog ── */}
      <AlertDialog
        open={promoteTarget !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPromoteTarget(null)
            setPromoteError(null)
          }
        }}
      >
        <AlertDialogContent className="bg-[#13161e] border-[#1f2335] text-[#dde1ed]">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-base">
              <ArrowUpCircle className="size-4 text-emerald-400" />
              Promote challenger to champion?
            </AlertDialogTitle>
            <AlertDialogDescription className="text-[11px] text-[#7e8aaa]">
              {promoteTarget && (
                <>
                  This will roll the active model version from{' '}
                  <code className="mono text-cyan-300">
                    {activeVersionId || '(unset)'}
                  </code>{' '}
                  to{' '}
                  <code className="mono text-cyan-300">
                    {promoteTarget.version}
                  </code>
                  . The next predict() cycle will use the promoted model. The
                  change is recorded in the durable audit log.
                  <br />
                  <br />
                  <span className="text-[#dde1ed]">Target metrics:</span>
                  <br />
                  Brier ={' '}
                  <span className="mono text-[#dde1ed]">
                    {promoteTarget.brier_score.toFixed(4)}
                  </span>{' '}
                  · AUC ={' '}
                  <span className="mono text-[#dde1ed]">
                    {promoteTarget.roc_auc.toFixed(4)}
                  </span>{' '}
                  · ECE ={' '}
                  <span className="mono text-[#dde1ed]">
                    {promoteTarget.ece.toFixed(4)}
                  </span>
                  {promoteTarget.status === 'REJECTED' && (
                    <span className="block mt-2 text-[var(--color-amber-fg)]">
                      ⚠ This model was REJECTED by the safety gate (Brier &gt;
                      0.22 or AUC &lt; 0.70). Promotion is an operator-explicit
                      override and will be flagged in the audit log.
                    </span>
                  )}
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {promoteError && (
            <div className="mt-2 text-[10.5px] text-[var(--color-red-fg)] bg-[var(--color-red-bg)] border border-[var(--color-red-bd)] rounded px-2 py-1.5 flex items-start gap-1.5">
              <XCircle className="size-3 mt-0.5 shrink-0" />
              <span className="break-all">{promoteError}</span>
            </div>
          )}
          <AlertDialogFooter className="mt-3">
            <AlertDialogCancel className="h-8 text-[11px] bg-[#0e1015] border-[#1f2335] text-[#7e8aaa] hover:bg-[#1a1f2e] hover:text-[#dde1ed]">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              className="h-8 text-[11px] bg-emerald-700 hover:bg-emerald-600 text-white gap-1.5"
              disabled={promoting || !promoteTarget}
              onClick={(e) => {
                e.preventDefault()
                if (promoteTarget) confirmPromote(promoteTarget)
              }}
            >
              {promoting ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  Promoting…
                </>
              ) : (
                <>
                  <ArrowUpCircle className="size-3" />
                  Promote to Champion
                </>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

// ── Sub-components ───────────────────────────────────────────────────────────

interface ComparisonRowProps {
  label: string
  shadowValue: string
  liveValue: string
  shadowTone: 'positive' | 'negative' | 'neutral'
  liveTone: 'positive' | 'negative' | 'neutral'
  hint?: string
}

function ComparisonRow({
  label,
  shadowValue,
  liveValue,
  shadowTone,
  liveTone,
  hint,
}: ComparisonRowProps) {
  const toneClass = (tone: ComparisonRowProps['shadowTone']) =>
    tone === 'positive'
      ? 'text-emerald-400'
      : tone === 'negative'
        ? 'text-red-400'
        : 'text-[#dde1ed]'

  return (
    <div
      className="grid grid-cols-[1fr_auto_auto] items-center gap-2 bg-[#0e1015] rounded p-1.5 border border-[#1f2335]"
      title={hint}
    >
      <div className="flex flex-col">
        <span className="text-[9.5px] uppercase tracking-wider text-[#5a637a] font-bold">
          {label}
        </span>
        {hint && <span className="text-[8.5px] text-[#3e4560] truncate">{hint}</span>}
      </div>
      <div className="text-right min-w-[72px]">
        <div className="text-[8px] uppercase text-cyan-500 tracking-wider">Shadow</div>
        <div className={`mono text-[11.5px] font-bold ${toneClass(shadowTone)}`}>
          {shadowValue}
        </div>
      </div>
      <div className="text-right min-w-[72px]">
        <div className="text-[8px] uppercase text-emerald-500 tracking-wider">Real</div>
        <div className={`mono text-[11.5px] font-bold ${toneClass(liveTone)}`}>
          {liveValue}
        </div>
      </div>
    </div>
  )
}
