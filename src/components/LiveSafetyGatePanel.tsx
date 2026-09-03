// components/LiveSafetyGatePanel.tsx — God Mode §82 Live Trading Safety Gate
// 10-check staged validation panel exposing the live_safety_gate backend.
'use client'

import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Shield,
  ShieldCheck,
  ShieldAlert,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  Lock,
  Unlock,
  PlayCircle,
  Power,
  ChevronDown,
  ChevronRight,
  RefreshCw,
  Clock,
  History,
  Activity,
  Loader2,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { Input } from '@/components/ui/input'
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
import { apiFetch } from '@/lib/api'
import { fmtAge, fmtTime } from '@/lib/design-tokens'

// ── Backend types ──────────────────────────────────────────────────────────
// Mirrors `core/live_safety_gate.py` `check_live_readiness()` return shape.
interface SafetyCheck {
  id: string
  name: string
  passed: boolean
  severity: string // "BLOCKING"
  threshold: string
  value: Record<string, unknown> | null
  detail: string
}

interface ReadinessVerdict {
  passed: boolean
  checks: SafetyCheck[]
  passed_count: number
  total_count: number
  blocking_checks: string[]
  checked_at: number // epoch seconds
}

// Light-weight subset of /api/status payload — only what the banner needs.
interface ModeStatus {
  mode?: string
  kill_switch?: boolean
  kill_switch_durable?: boolean
  live_trading_enabled?: boolean
  paper_trade?: boolean
}

interface AuditEvent {
  timestamp: number
  category: string
  event_type: string
  details: string
  token_id?: string | null
  slug?: string | null
}

// ── Check-status classification ────────────────────────────────────────────
// Backend only emits pass/fail, but it records exception-raised checks as
// failed with a "check raised:" prefix in `detail`. We surface those as
// WARNING (amber) to distinguish a broken dependency from a genuine
// threshold miss.
type CheckStatus = 'PASS' | 'FAIL' | 'WARNING' | 'PENDING'

function classifyCheck(c: SafetyCheck | undefined): CheckStatus {
  if (!c) return 'PENDING'
  if (c.passed) return 'PASS'
  if (typeof c.detail === 'string' && c.detail.startsWith('check raised:')) return 'WARNING'
  return 'FAIL'
}

const STATUS_STYLES: Record<
  CheckStatus,
  { badge: string; icon: typeof CheckCircle2; ring: string; glow: string; label: string }
> = {
  PASS: {
    badge: 'badge-green',
    icon: CheckCircle2,
    ring: 'border-green-500/40',
    glow: 'shadow-[0_0_0_1px_rgba(34,197,94,0.15)_inset]',
    label: 'PASS',
  },
  FAIL: {
    badge: 'badge-red',
    icon: XCircle,
    ring: 'border-red-500/45',
    glow: 'shadow-[0_0_0_1px_rgba(239,68,68,0.15)_inset]',
    label: 'FAIL',
  },
  WARNING: {
    badge: 'badge-amber',
    icon: AlertTriangle,
    ring: 'border-amber-500/45',
    glow: 'shadow-[0_0_0_1px_rgba(245,158,11,0.15)_inset]',
    label: 'WARN',
  },
  PENDING: {
    badge: 'badge-dim',
    icon: Clock,
    ring: 'border-[#1f2335]',
    glow: '',
    label: 'PEND',
  },
}

const POLL_INTERVAL_MS = 10_000
const HISTORY_LIMIT = 12
const FORCE_OPEN_CONFIRMATION_PHRASE = 'I UNDERSTAND THE RISKS'

// Filter audit events to gate-relevant transitions for the timeline.
const GATE_EVENT_TYPES = new Set([
  'live_trading_enabled',
  'kill_switch_activated',
  'kill_switch_deactivated',
  'observation_mode_enabled',
  'observation_mode_disabled',
])

// ── Sub-components ─────────────────────────────────────────────────────────

function GateBanner({
  open,
  killSwitch,
  mode,
  checkedAt,
}: {
  open: boolean
  killSwitch: boolean
  mode?: string
  checkedAt: number | null
}) {
  // Gate is OPEN only when all 10 checks pass AND no kill switch is active.
  const effectiveOpen = open && !killSwitch
  const Icon = effectiveOpen ? Unlock : Lock
  const headline = effectiveOpen ? 'GATE OPEN' : 'GATE CLOSED'
  const subline = effectiveOpen
    ? 'Live trading authorised — all 10 §82 checks passed.'
    : killSwitch
    ? 'Kill switch active — gate sealed. All trading halted.'
    : 'Live trading blocked — one or more §82 checks failed.'
  const bannerClass = effectiveOpen
    ? 'bg-emerald-500/10 border-emerald-500/40 text-emerald-200'
    : 'bg-red-500/10 border-red-500/45 text-red-200'

  return (
    <div
      className={`relative overflow-hidden rounded-lg border px-4 py-3 sm:px-5 sm:py-4 ${bannerClass}`}
      role="status"
      aria-live="polite"
    >
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3 min-w-0">
          <span
            className={`flex h-9 w-9 sm:h-10 sm:w-10 items-center justify-center rounded-full ${
              effectiveOpen
                ? 'bg-emerald-500/15 border border-emerald-500/40'
                : 'bg-red-500/15 border border-red-500/45'
            }`}
          >
            <Icon className="h-5 w-5 sm:h-6 sm:w-6" aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <div className="flex items-baseline gap-2 flex-wrap">
              <span className="text-base sm:text-lg font-bold tracking-wide">
                {headline}
              </span>
              {mode && (
                <span className="text-[10px] uppercase tracking-wider opacity-70">
                  · mode={mode}
                </span>
              )}
              {killSwitch && (
                <span className="text-[10px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-red-500/30 border border-red-500/40">
                  KILL SWITCH
                </span>
              )}
            </div>
            <p className="text-[11px] sm:text-xs opacity-85 mt-0.5 truncate">
              {subline}
            </p>
          </div>
        </div>
        <div className="text-right flex-shrink-0">
          <div className="text-[9.5px] uppercase tracking-wider opacity-70">
            Last Evaluation
          </div>
          <div className="mono text-[11px] font-semibold opacity-95">
            {checkedAt != null ? fmtTime(checkedAt) : '—'}
          </div>
          <div className="text-[9.5px] opacity-70">
            {checkedAt != null ? fmtAge(checkedAt) : 'never'}
          </div>
        </div>
      </div>
    </div>
  )
}

function CheckCard({
  check,
  index,
  expanded,
  onToggle,
}: {
  check: SafetyCheck
  index: number
  expanded: boolean
  onToggle: () => void
}) {
  const status = classifyCheck(check)
  const st = STATUS_STYLES[status]
  const StatusIcon = st.icon

  return (
    <div
      className={`bg-[#0e1015] border ${st.ring} ${st.glow} rounded-lg overflow-hidden transition-colors`}
    >
      <button
        type="button"
        onClick={onToggle}
        className="w-full text-left p-3 flex items-start gap-2.5 hover:bg-[#13161e] transition-colors focus:outline-none focus-visible:ring-1 focus-visible:ring-[#3b82f6]/40"
        aria-expanded={expanded}
        aria-controls={`check-detail-${check.id}`}
      >
        <span
          className={`mono text-[10px] font-bold w-5 h-5 flex items-center justify-center rounded ${
            status === 'PASS'
              ? 'bg-green-500/15 text-green-400'
              : status === 'FAIL'
              ? 'bg-red-500/15 text-red-400'
              : status === 'WARNING'
              ? 'bg-amber-500/15 text-amber-400'
              : 'bg-[#1f2335] text-[#7e8aaa]'
          }`}
        >
          {index + 1}
        </span>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <StatusIcon
              className={`h-3.5 w-3.5 flex-shrink-0 ${
                status === 'PASS'
                  ? 'text-green-400'
                  : status === 'FAIL'
                  ? 'text-red-400'
                  : status === 'WARNING'
                  ? 'text-amber-400'
                  : 'text-[#7e8aaa]'
              }`}
              aria-hidden="true"
            />
            <span className="text-[12px] font-semibold text-[#dde1ed] truncate">
              {check.name}
            </span>
            <span className={`badge ${st.badge} text-[9px] px-1.5 py-0`}>
              {st.label}
            </span>
          </div>
          <div className="mono text-[9.5px] text-[#7e8aaa] mt-0.5 truncate">
            {check.id}
          </div>
        </div>
        <span className="text-[#7e8aaa] flex-shrink-0 mt-0.5">
          {expanded ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </span>
      </button>

      {/* Always-visible detail line */}
      <div className="px-3 pb-2 -mt-1">
        <p
          className={`text-[11px] leading-snug ${
            status === 'PASS'
              ? 'text-[#9aa3bc]'
              : status === 'FAIL'
              ? 'text-red-300/85'
              : status === 'WARNING'
              ? 'text-amber-300/85'
              : 'text-[#7e8aaa]'
          } line-clamp-2`}
          title={check.detail}
        >
          {check.detail || 'No detail returned by check.'}
        </p>
      </div>

      {expanded && (
        <div
          id={`check-detail-${check.id}`}
          className="border-t border-[#1f2335] bg-[#08090f]/60 px-3 py-2.5 space-y-2"
        >
          <div>
            <div className="text-[9.5px] uppercase tracking-wider text-[#7e8aaa] font-semibold">
              Threshold
            </div>
            <code className="mono text-[10.5px] text-cyan-300/95 break-all">
              {check.threshold || '—'}
            </code>
          </div>
          <div>
            <div className="text-[9.5px] uppercase tracking-wider text-[#7e8aaa] font-semibold">
              Detail
            </div>
            <p className="text-[10.5px] text-[#c8cfe0] leading-relaxed break-words">
              {check.detail || '—'}
            </p>
          </div>
          {check.value != null && (
            <div>
              <div className="text-[9.5px] uppercase tracking-wider text-[#7e8aaa] font-semibold">
                Measured value
              </div>
              <pre className="mono text-[10px] text-[#9aa3bc] bg-[#080910] border border-[#1f2335] rounded p-2 overflow-x-auto max-h-32">
                {JSON.stringify(check.value, null, 2)}
              </pre>
            </div>
          )}
          <div className="flex items-center justify-between text-[9.5px] text-[#7e8aaa] pt-0.5">
            <span>
              Severity:{' '}
              <span className="mono text-[#c8cfe0]">{check.severity}</span>
            </span>
            <span className="mono">#{index + 1} in staged order</span>
          </div>
        </div>
      )}
    </div>
  )
}

function CheckSkeleton() {
  return (
    <div className="bg-[#0e1015] border border-[#1f2335] rounded-lg p-3 animate-pulse">
      <div className="flex items-center gap-2.5">
        <div className="w-5 h-5 rounded bg-[#1f2335]" />
        <div className="flex-1 space-y-1.5">
          <div className="h-2.5 w-2/3 rounded bg-[#1f2335]" />
          <div className="h-2 w-1/3 rounded bg-[#181c28]" />
        </div>
        <div className="h-4 w-10 rounded bg-[#1f2335]" />
      </div>
      <div className="h-2 w-full rounded bg-[#181c28] mt-2.5" />
    </div>
  )
}

function HistoryTimeline({ events }: { events: AuditEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="text-[11px] text-[#7e8aaa] text-center py-4">
        No gate transitions recorded yet.
      </div>
    )
  }

  return (
    <ol className="space-y-1.5 max-h-72 overflow-y-auto scrollbar-thin pr-1">
      {events.map((e, i) => {
        const isOpen =
          e.event_type === 'live_trading_enabled' ||
          e.event_type === 'kill_switch_deactivated' ||
          e.event_type === 'observation_mode_disabled'
        const Icon = isOpen ? Unlock : Lock
        return (
          <li
            key={`${e.timestamp}-${i}`}
            className="flex items-start gap-2 text-[11px] bg-[#0e1015] border border-[#1f2335] rounded px-2.5 py-1.5"
          >
            <span
              className={`mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded ${
                isOpen
                  ? 'bg-emerald-500/15 text-emerald-400'
                  : 'bg-red-500/15 text-red-400'
              }`}
            >
              <Icon className="h-3 w-3" aria-hidden="true" />
            </span>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-1.5 flex-wrap">
                <span className="mono text-[10px] font-semibold text-[#dde1ed]">
                  {e.event_type}
                </span>
                <span className="text-[9px] text-[#7e8aaa]">
                  {fmtAge(e.timestamp)}
                </span>
              </div>
              <p className="text-[10px] text-[#9aa3bc] mt-0.5 line-clamp-2 break-words">
                {e.details}
              </p>
            </div>
            <span className="mono text-[9px] text-[#7e8aaa] flex-shrink-0 mt-0.5">
              {fmtTime(e.timestamp)}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

// ── Main panel ──────────────────────────────────────────────────────────────

export default function LiveSafetyGatePanel() {
  const [readiness, setReadiness] = useState<ReadinessVerdict | null>(null)
  const [modeStatus, setModeStatus] = useState<ModeStatus | null>(null)
  const [history, setHistory] = useState<AuditEvent[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [running, setRunning] = useState(false)
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  // Force-open dialog state
  const [openDialogOpen, setOpenDialogOpen] = useState(false)
  const [openConfirmText, setOpenConfirmText] = useState('')
  const [openDialogStep, setOpenDialogStep] = useState<1 | 2>(1)
  const [openBusy, setOpenBusy] = useState(false)
  const [openError, setOpenError] = useState<string | null>(null)

  // Force-close dialog state
  const [closeDialogOpen, setCloseDialogOpen] = useState(false)
  const [closeBusy, setCloseBusy] = useState(false)
  const [closeError, setCloseError] = useState<string | null>(null)

  // Operation feedback toast
  const [toast, setToast] = useState<{
    kind: 'success' | 'error' | 'info'
    msg: string
  } | null>(null)

  const inFlightRef = useRef(false)

  const showToast = useCallback(
    (kind: 'success' | 'error' | 'info', msg: string) => {
      setToast({ kind, msg })
      window.setTimeout(() => setToast(null), 4500)
    },
    [],
  )

  const fetchAll = useCallback(async () => {
    if (inFlightRef.current) return
    inFlightRef.current = true
    try {
      const [readinessRes, statusRes, auditRes] = await Promise.all([
        apiFetch('/api/live/readiness'),
        apiFetch('/api/status').catch(() => null),
        apiFetch(`/api/audit/logs?limit=40&category=system`).catch(() => null),
      ])

      if (!readinessRes.ok) {
        const txt = await readinessRes.text().catch(() => '')
        throw new Error(
          `readiness endpoint returned ${readinessRes.status}${txt ? `: ${txt.slice(0, 200)}` : ''}`,
        )
      }
      const data = (await readinessRes.json()) as ReadinessVerdict
      setReadiness(data)

      if (statusRes && statusRes.ok) {
        const s = await statusRes.json().catch(() => ({}))
        setModeStatus(s as ModeStatus)
      }

      if (auditRes && auditRes.ok) {
        const a = (await auditRes.json().catch(() => ({ logs: [] }))) as {
          logs?: AuditEvent[]
        }
        const filtered = (a.logs ?? [])
          .filter((e) => GATE_EVENT_TYPES.has(e.event_type))
          .slice(0, HISTORY_LIMIT)
        setHistory(filtered)
      }

      setError(null)
      setLastUpdated(Date.now())
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setError(msg)
    } finally {
      setLoading(false)
      inFlightRef.current = false
    }
  }, [])

  // Initial load + 10s polling, paused when document hidden.
  useEffect(() => {
    fetchAll()
    let interval: ReturnType<typeof setInterval> | null = null

    const start = () => {
      if (interval) return
      interval = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchAll()
      }, POLL_INTERVAL_MS)
    }
    const stop = () => {
      if (interval) {
        clearInterval(interval)
        interval = null
      }
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) {
        stop()
      } else {
        // Refresh immediately on tab re-focus, then resume polling.
        fetchAll()
        start()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    start()
    return () => {
      stop()
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
    }
  }, [fetchAll])

  const runAllChecks = useCallback(async () => {
    setRunning(true)
    try {
      // GET /api/live/readiness re-evaluates all 10 checks on the server.
      const res = await apiFetch('/api/live/readiness')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const data = (await res.json()) as ReadinessVerdict
      setReadiness(data)
      setLastUpdated(Date.now())
      showToast(
        data.passed ? 'success' : 'info',
        `Re-evaluation complete — ${data.passed_count}/${data.total_count} checks passing.`,
      )
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      showToast('error', `Failed to run checks: ${msg}`)
    } finally {
      setRunning(false)
    }
  }, [showToast])

  const forceOpenGate = useCallback(async () => {
    setOpenBusy(true)
    setOpenError(null)
    try {
      const res = await apiFetch('/api/live/enable', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          confirm: true,
          reason: 'manual operator override via LiveSafetyGatePanel',
        }),
      })
      if (res.status === 409) {
        const body = (await res.json().catch(() => ({}))) as {
          detail?: { message?: string; blocking_checks?: string[] }
        }
        const blocking = body.detail?.blocking_checks ?? []
        setOpenError(
          `${body.detail?.message ?? 'Gate refused to open.'} Blocking: ${blocking.join(', ') || 'none listed'}`,
        )
        showToast('error', 'Gate refused — blocking checks remain.')
        return
      }
      if (!res.ok) {
        const txt = await res.text().catch(() => '')
        throw new Error(`HTTP ${res.status}${txt ? `: ${txt.slice(0, 200)}` : ''}`)
      }
      const body = (await res.json().catch(() => ({}))) as {
        enabled?: boolean
        mode?: string
      }
      showToast(
        'success',
        `Live trading ENABLED in-memory (mode=${body.mode ?? 'live'}). Restart required for durable activation.`,
      )
      setOpenDialogOpen(false)
      setOpenConfirmText('')
      setOpenDialogStep(1)
      // Re-poll immediately so the banner reflects the new state.
      fetchAll()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setOpenError(msg)
      showToast('error', `Force-open failed: ${msg}`)
    } finally {
      setOpenBusy(false)
    }
  }, [fetchAll, showToast])

  const forceCloseGate = useCallback(async () => {
    setCloseBusy(true)
    setCloseError(null)
    try {
      // Activating the kill switch is the de-facto "force close gate"
      // mechanism — it halts all trading immediately and is logged as
      // kill_switch_activated in the audit trail. There is no
      // /api/live/disable endpoint; the kill switch is the contract.
      const res = await apiFetch('/api/kill-switch/activate', {
        method: 'POST',
      })
      if (!res.ok) {
        const txt = await res.text().catch(() => '')
        throw new Error(`HTTP ${res.status}${txt ? `: ${txt.slice(0, 200)}` : ''}`)
      }
      showToast(
        'success',
        'Kill switch activated — gate sealed. All trading halted.',
      )
      setCloseDialogOpen(false)
      fetchAll()
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setCloseError(msg)
      showToast('error', `Force-close failed: ${msg}`)
    } finally {
      setCloseBusy(false)
    }
  }, [fetchAll, showToast])

  const toggleExpand = useCallback((id: string) => {
    setExpanded((prev) => ({ ...prev, [id]: !prev[id] }))
  }, [])

  const expandAll = useCallback(() => {
    if (!readiness) return
    const all: Record<string, boolean> = {}
    readiness.checks.forEach((c) => (all[c.id] = true))
    setExpanded(all)
  }, [readiness])

  const collapseAll = useCallback(() => setExpanded({}), [])

  // ── Render: loading skeleton ────────────────────────────────────────────
  if (loading && !readiness) {
    return (
      <div className="card bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden shadow-xl">
        <div className="card-header p-3 border-b border-[#1f2335] flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Shield className="h-4 w-4 text-cyan-400" aria-hidden="true" />
            <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
              LIVE SAFETY GATE · §82
            </span>
          </div>
          <Loader2 className="h-3.5 w-3.5 animate-spin text-[#7e8aaa]" />
        </div>
        <div className="p-3 space-y-3">
          <div className="h-20 rounded-lg bg-[#0e1015] border border-[#1f2335] animate-pulse" />
          <div className="h-2 rounded bg-[#1f2335] animate-pulse" />
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
            {Array.from({ length: 10 }).map((_, i) => (
              <CheckSkeleton key={i} />
            ))}
          </div>
        </div>
      </div>
    )
  }

  // ── Render: error state ──────────────────────────────────────────────────
  if ((error || !readiness) && !loading) {
    return (
      <div className="card bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden shadow-xl">
        <div className="card-header p-3 border-b border-[#1f2335] flex items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <ShieldAlert className="h-4 w-4 text-red-400" aria-hidden="true" />
            <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
              LIVE SAFETY GATE · §82
            </span>
            <span className="badge badge-red text-[9.5px]">Unavailable</span>
          </div>
        </div>
        <div className="p-4 text-center space-y-3">
          <p className="text-xs text-[#9aa3bc]">
            Safety-gate endpoint unavailable. The bot may be starting up, or the
            <code className="mono mx-1 text-cyan-300">/api/live/readiness</code>
            route is not responding.
          </p>
          {error && (
            <pre className="mono text-[10px] text-red-300/85 bg-[#080910] border border-[#1f2335] rounded p-2 max-h-32 overflow-auto text-left">
              {error}
            </pre>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setLoading(true)
              setError(null)
              fetchAll()
            }}
            className="btn btn-ghost btn-sm"
          >
            <RefreshCw className="h-3 w-3" />
            Retry
          </Button>
        </div>
      </div>
    )
  }

  // ── Render: ready ────────────────────────────────────────────────────────
  const verdict = readiness as ReadinessVerdict
  const passedCount = verdict.passed_count
  const totalCount = verdict.total_count || 10
  const progressPct = Math.round((passedCount / totalCount) * 100)
  const killSwitch = Boolean(modeStatus?.kill_switch)
  const gateOpen = verdict.passed && !killSwitch

  // Status counts for the legend.
  const counts = verdict.checks.reduce(
    (acc, c) => {
      const s = classifyCheck(c)
      acc[s] = (acc[s] ?? 0) + 1
      return acc
    },
    {} as Record<CheckStatus, number>,
  )

  return (
    <div className="card bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden shadow-xl flex flex-col">
      {/* Header */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap justify-between items-center gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed] tracking-wide flex items-center gap-1.5">
            <Shield className="h-4 w-4 text-cyan-400" aria-hidden="true" />
            LIVE SAFETY GATE · §82
          </span>
          <span
            className={`badge ${gateOpen ? 'badge-green' : 'badge-red'} text-[9.5px]`}
          >
            {gateOpen ? (
              <ShieldCheck className="h-3 w-3" />
            ) : (
              <ShieldAlert className="h-3 w-3" />
            )}
            {gateOpen ? 'OPEN' : 'CLOSED'}
          </span>
          {lastUpdated && (
            <span className="text-[9.5px] text-[#7e8aaa] mono">
              · updated {fmtAge(lastUpdated / 1000)}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1.5">
          <Button
            variant="outline"
            size="sm"
            onClick={runAllChecks}
            disabled={running}
            className="btn btn-ghost btn-sm"
            title="Re-run all 10 staged checks"
          >
            {running ? (
              <Loader2 className="h-3 w-3 animate-spin" />
            ) : (
              <PlayCircle className="h-3 w-3" />
            )}
            Run all checks
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setOpenDialogOpen(true)}
            className="btn btn-amber btn-sm"
            title="Force-open the gate — bypasses fail-closed contract"
          >
            <Unlock className="h-3 w-3" />
            Force open
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setCloseDialogOpen(true)}
            className="btn btn-danger btn-sm"
            title="Force-close the gate — activates the kill switch"
          >
            <Lock className="h-3 w-3" />
            Force close
          </Button>
        </div>
      </div>

      {/* Banner + progress */}
      <div className="p-3 space-y-3">
        <GateBanner
          open={verdict.passed}
          killSwitch={killSwitch}
          mode={modeStatus?.mode}
          checkedAt={verdict.checked_at}
        />

        {/* Staged validation progress */}
        <div className="bg-[#0e1015] border border-[#1f2335] rounded-lg px-3 py-2.5">
          <div className="flex justify-between items-center text-[10.5px] text-[#7e8aaa] mb-1.5">
            <span className="font-semibold text-[#dde1ed] flex items-center gap-1.5">
              <Activity className="h-3 w-3" aria-hidden="true" />
              Staged Validation Progress
            </span>
            <span
              className={`mono font-bold ${
                progressPct === 100
                  ? 'text-green-400'
                  : progressPct >= 70
                  ? 'text-amber-400'
                  : 'text-red-400'
              }`}
            >
              {passedCount}/{totalCount} checks passed · {progressPct}%
            </span>
          </div>
          <Progress
            value={progressPct}
            className="h-2 bg-[#13161e] border border-[#1f2335] [&>[data-slot=progress-indicator]]:bg-gradient-to-r [&>[data-slot=progress-indicator]]:from-cyan-500 [&>[data-slot=progress-indicator]]:to-emerald-500"
          />
          <div className="flex flex-wrap gap-2 mt-2 text-[9.5px]">
            <span className="flex items-center gap-1 text-green-400">
              <span className="w-1.5 h-1.5 rounded-full bg-green-400 inline-block" />
              PASS {counts.PASS ?? 0}
            </span>
            <span className="flex items-center gap-1 text-red-400">
              <span className="w-1.5 h-1.5 rounded-full bg-red-400 inline-block" />
              FAIL {counts.FAIL ?? 0}
            </span>
            <span className="flex items-center gap-1 text-amber-400">
              <span className="w-1.5 h-1.5 rounded-full bg-amber-400 inline-block" />
              WARN {counts.WARNING ?? 0}
            </span>
            <span className="flex items-center gap-1 text-[#7e8aaa]">
              <span className="w-1.5 h-1.5 rounded-full bg-[#3e4560] inline-block" />
              PEND {counts.PENDING ?? 0}
            </span>
            {verdict.blocking_checks.length > 0 && (
              <span className="ml-auto text-[9px] text-red-300/85">
                Blocking: {verdict.blocking_checks.join(', ')}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* 10-check grid */}
      <div className="px-3 pb-2">
        <div className="flex justify-between items-center mb-2">
          <span className="text-[10px] uppercase tracking-wider text-[#7e8aaa] font-semibold">
            10 Staged Checks
          </span>
          <div className="flex gap-2 text-[9.5px]">
            <button
              type="button"
              onClick={expandAll}
              className="text-cyan-400 hover:text-cyan-300 transition-colors"
            >
              Expand all
            </button>
            <span className="text-[#3e4560]">·</span>
            <button
              type="button"
              onClick={collapseAll}
              className="text-[#7e8aaa] hover:text-[#dde1ed] transition-colors"
            >
              Collapse all
            </button>
          </div>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
          {verdict.checks.map((c, idx) => (
            <CheckCard
              key={c.id}
              check={c}
              index={idx}
              expanded={Boolean(expanded[c.id])}
              onToggle={() => toggleExpand(c.id)}
            />
          ))}
        </div>
      </div>

      {/* History timeline */}
      <div className="px-3 pb-3 pt-2 border-t border-[#1f2335] mt-auto">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-[10px] uppercase tracking-wider text-[#7e8aaa] font-semibold flex items-center gap-1.5">
            <History className="h-3 w-3" aria-hidden="true" />
            Gate Transition History
          </span>
          <span className="text-[9px] text-[#7e8aaa]">
            last {HISTORY_LIMIT} events · system audit trail
          </span>
        </div>
        <HistoryTimeline events={history} />
      </div>

      {/* ── Force-open dialog: double confirmation with typed phrase ── */}
      <AlertDialog
        open={openDialogOpen}
        onOpenChange={(o) => {
          setOpenDialogOpen(o)
          if (!o) {
            setOpenConfirmText('')
            setOpenDialogStep(1)
            setOpenError(null)
          }
        }}
      >
        <AlertDialogContent className="bg-[#13161e] border border-red-500/40 text-[#dde1ed] max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-red-300">
              <AlertTriangle className="h-4 w-4" />
              Force-open live trading gate
            </AlertDialogTitle>
            <AlertDialogDescription className="text-[#9aa3bc] text-xs leading-relaxed">
              {openDialogStep === 1 ? (
                <>
                  This is a <strong className="text-red-300">dangerous</strong>{' '}
                  operation. It bypasses the fail-closed §82 contract and
                  attempts to flip the bot into live trading mode via{' '}
                  <code className="mono text-cyan-300">POST /api/live/enable</code>
                  . The backend will still refuse (HTTP 409) if any of the 10
                  staged checks are failing — this action only succeeds when the
                  gate is currently passing.
                  <br />
                  <br />
                  <strong className="text-amber-300">Side-effects:</strong> flips
                  in-memory mode flags (<code className="mono text-cyan-300">live_trading_enabled=true</code>,
                  <code className="mono text-cyan-300">trading_mode=live</code>,
                  <code className="mono text-cyan-300">paper_trade=false</code>),
                  logs an audit event, and starts admitting real orders
                  immediately. Restart the process for durable activation.
                </>
              ) : (
                <>
                  To confirm, type{' '}
                  <code className="mono text-amber-300 bg-[#080910] px-1.5 py-0.5 rounded border border-[#1f2335]">
                    {FORCE_OPEN_CONFIRMATION_PHRASE}
                  </code>{' '}
                  exactly as shown. This action is logged to the immutable audit
                  trail under your operator token.
                </>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>

          {openDialogStep === 2 && (
            <Input
              type="text"
              value={openConfirmText}
              autoFocus
              autoComplete="off"
              spellCheck={false}
              onChange={(e) => setOpenConfirmText(e.target.value)}
              className="bg-[#080910] border border-[#1f2335] text-[#dde1ed] mono text-sm"
              placeholder="Type the confirmation phrase…"
              aria-label="Confirmation phrase"
            />
          )}

          {openError && (
            <pre className="mono text-[10px] text-red-300 bg-[#080910] border border-red-500/30 rounded p-2 max-h-32 overflow-auto">
              {openError}
            </pre>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel
              disabled={openBusy}
              className="btn btn-ghost btn-sm"
            >
              Cancel
            </AlertDialogCancel>
            {openDialogStep === 1 ? (
              <AlertDialogAction
                onClick={(e) => {
                  e.preventDefault()
                  setOpenDialogStep(2)
                }}
                className="btn btn-amber btn-sm"
              >
                <Power className="h-3 w-3" />
                I understand — continue
              </AlertDialogAction>
            ) : (
              <AlertDialogAction
                onClick={(e) => {
                  e.preventDefault()
                  if (openConfirmText !== FORCE_OPEN_CONFIRMATION_PHRASE) {
                    setOpenError(
                      `Confirmation phrase does not match. Expected exactly: "${FORCE_OPEN_CONFIRMATION_PHRASE}".`,
                    )
                    return
                  }
                  forceOpenGate()
                }}
                disabled={
                  openBusy || openConfirmText !== FORCE_OPEN_CONFIRMATION_PHRASE
                }
                className="btn btn-danger btn-sm"
              >
                {openBusy ? (
                  <Loader2 className="h-3 w-3 animate-spin" />
                ) : (
                  <Unlock className="h-3 w-3" />
                )}
                Force open gate
              </AlertDialogAction>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ── Force-close dialog: single confirmation ── */}
      <AlertDialog
        open={closeDialogOpen}
        onOpenChange={(o) => {
          setCloseDialogOpen(o)
          if (!o) setCloseError(null)
        }}
      >
        <AlertDialogContent className="bg-[#13161e] border border-red-500/40 text-[#dde1ed] max-w-md">
          <AlertDialogHeader>
            <AlertDialogTitle className="flex items-center gap-2 text-red-300">
              <Lock className="h-4 w-4" />
              Force-close live trading gate
            </AlertDialogTitle>
            <AlertDialogDescription className="text-[#9aa3bc] text-xs leading-relaxed">
              Activates the kill-switch circuit breaker via{' '}
              <code className="mono text-cyan-300">
                POST /api/kill-switch/activate
              </code>
              . This is the de-facto &quot;close the gate&quot; action — all
              trading halts immediately, open orders are cancelled, and an
              audit event is logged. The gate will remain sealed until an
              operator deactivates the kill switch and the 10 staged checks
              re-pass.
            </AlertDialogDescription>
          </AlertDialogHeader>

          {closeError && (
            <pre className="mono text-[10px] text-red-300 bg-[#080910] border border-red-500/30 rounded p-2 max-h-32 overflow-auto">
              {closeError}
            </pre>
          )}

          <AlertDialogFooter>
            <AlertDialogCancel
              disabled={closeBusy}
              className="btn btn-ghost btn-sm"
            >
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={(e) => {
                e.preventDefault()
                forceCloseGate()
              }}
              disabled={closeBusy}
              className="btn btn-danger btn-sm"
            >
              {closeBusy ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Lock className="h-3 w-3" />
              )}
              Activate kill switch
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* ── Operation feedback toast ── */}
      {toast && (
        <div
          role="alert"
          aria-live="assertive"
          className={`fixed bottom-4 right-4 z-50 max-w-sm rounded-lg border px-3 py-2 shadow-xl text-xs ${
            toast.kind === 'success'
              ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-200'
              : toast.kind === 'error'
              ? 'bg-red-500/15 border-red-500/40 text-red-200'
              : 'bg-cyan-500/15 border-cyan-500/40 text-cyan-200'
          }`}
        >
          <div className="flex items-start gap-2">
            <span className="mt-0.5">
              {toast.kind === 'success' ? (
                <CheckCircle2 className="h-3.5 w-3.5" />
              ) : toast.kind === 'error' ? (
                <XCircle className="h-3.5 w-3.5" />
              ) : (
                <AlertTriangle className="h-3.5 w-3.5" />
              )}
            </span>
            <span className="flex-1">{toast.msg}</span>
            <button
              type="button"
              onClick={() => setToast(null)}
              className="text-[10px] opacity-60 hover:opacity-100"
              aria-label="Dismiss"
            >
              ✕
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
