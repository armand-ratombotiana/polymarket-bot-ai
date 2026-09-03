// components/RetentionPanel.tsx — Data retention / pruning control panel.
//
// Exposes the bounded-storage retention policy implemented in
// `mini-services/polymarket-bot/core/retention.py` over the HTTP surface
// `POST /api/system/prune` (registered by `register_routes(app)`). Mirrors
// the visual style established by `MLPanel.tsx` and `SystemHealthView.tsx`
// (dark `#13161e` cards, `#1f2335` borders, `.kpi-card` / `.badge-*` /
// `.data-table` design-system classes from `globals.css`).
//
// Backend contract (verified by reading core/retention.py register_routes):
//   POST /api/system/prune        body {target: "all" | "observability" |
//                                  "decision_ledger" | "execution_quality" |
//                                  "audit_events"} (default "all")
//                                  → {timestamp, results: {target:
//                                  {pruned, max_age_hours, db_path, error}},
//                                     total_pruned, success}
//                                     OR  {target, pruned} for a single target
//
// The four retention horizons below mirror the module constants
// OBSERVABILITY_RETENTION_HOURS=168 (7d), DECISION_LEDGER_RETENTION_HOURS=720
// (30d), EXECUTION_QUALITY_RETENTION_HOURS=720 (30d),
// AUDIT_EVENTS_RETENTION_HOURS=2160 (90d). There is no live GET endpoint that
// re-exposes the policy at runtime, so the canonical values are embedded here
// as the source-of-truth display (the env-var-driven defaults in retention.py
// are the only override path; the inline config editor surfaces this honestly
// — local edits are staged for display, persistence requires an env-var
// override at boot).

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Database,
  Trash2,
  History,
  ShieldCheck,
  AlertTriangle,
  RefreshCw,
  Loader2,
  CheckCircle2,
  XCircle,
  Clock,
  HardDrive,
  Server,
} from 'lucide-react'

import { apiFetch, getApiUrl } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'

// ── Static policy source-of-truth (mirrors core/retention.py constants) ────

interface RetentionTarget {
  /** URL target string passed to POST /api/system/prune */
  target: string
  /** Human-readable store label */
  label: string
  /** SQLite table(s) pruned by this target */
  tables: string[]
  /** Default retention horizon in days (env-var override at boot) */
  horizonDays: number
  /** env var name controlling the DB path */
  envVar: string
  /** Default DB file path (when env var unset) */
  defaultDbPath: string
  /** Short rationale for the chosen horizon */
  rationale: string
}

const RETENTION_TARGETS: RetentionTarget[] = [
  {
    target: 'observability',
    label: 'Observability (metrics)',
    tables: ['metrics'],
    horizonDays: 7,
    envVar: 'OBSERVABILITY_DB_PATH',
    defaultDbPath: '/app/data/observability.db',
    rationale: 'High-frequency system snapshots (CPU/mem every ~10s) — fastest-growing store.',
  },
  {
    target: 'decision_ledger',
    label: 'Decision Ledger',
    tables: ['decision_events', 'decision_rejections'],
    horizonDays: 30,
    envVar: 'DECISION_LEDGER_DB_PATH',
    defaultDbPath: '/app/data/decision_ledger.db',
    rationale: 'Full PREDICTION → SIGNAL → RISK_* → ORDER → FILL chain kept for one trade lifecycle.',
  },
  {
    target: 'execution_quality',
    label: 'Execution Quality',
    tables: ['execution_quality'],
    horizonDays: 30,
    envVar: 'EXECUTION_QUALITY_DB_PATH',
    defaultDbPath: '/app/data/execution_quality.db',
    rationale: 'Per-fill slippage / latency / realized-edge rows — drives the 30-day rolling exec view.',
  },
  {
    target: 'audit_events',
    label: 'Audit Events',
    tables: ['audit_events'],
    horizonDays: 90,
    envVar: 'AUDIT_DB_PATH',
    defaultDbPath: '/app/data/audit_trail.db',
    rationale: 'Forensic / compliance window — three months is the typical reconstruction horizon.',
  },
]

// ── Runtime types ───────────────────────────────────────────────────────────

interface PruneAllResult {
  timestamp: number
  results: Record<
    string,
    { pruned: number; max_age_hours: number; db_path: string; error: string | null }
  >
  total_pruned: number
  success: boolean
}

interface PruneSingleResult {
  target: string
  pruned: number
}

interface MarketDbStats {
  db_backend?: string
  size_mb?: number
  snapshots_recorded?: number
  ticks_recorded?: number
  news_items_recorded?: number
  ml_feature_vectors?: number
}

interface SystemHealth {
  status?: string
  checks?: Record<string, { status: string; detail: string }>
  market_db?: MarketDbStats
}

/** One row of client-side prune history (kept in localStorage). */
interface PruneHistoryEntry {
  id: string
  timestamp: number
  target: string
  triggered_by: 'manual' | 'auto-refresh-attempt'
  total_pruned: number
  success: boolean
  per_store?: Record<string, { pruned: number; error: string | null }>
  error?: string
}

const HISTORY_KEY = 'polymarket:retention:prune_history'
const HISTORY_MAX = 25
const POLL_INTERVAL_MS = 60_000

// ── Helpers ────────────────────────────────────────────────────────────────

function formatBytes(mb: number | undefined | null): string {
  if (mb === undefined || mb === null || Number.isNaN(mb)) return '—'
  if (mb < 1 / 1024) return `${(mb * 1024 * 1024).toFixed(0)} B`
  if (mb < 1) return `${(mb * 1024).toFixed(1)} KB`
  if (mb < 1024) return `${mb.toFixed(2)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

function formatRelativeTime(epoch: number): string {
  if (!epoch) return '—'
  const diff = Date.now() / 1000 - epoch
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function loadHistory(): PruneHistoryEntry[] {
  if (typeof window === 'undefined') return []
  try {
    const raw = window.localStorage.getItem(HISTORY_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as PruneHistoryEntry[]) : []
  } catch {
    return []
  }
}

function saveHistory(entries: PruneHistoryEntry[]): void {
  if (typeof window === 'undefined') return
  try {
    const trimmed = entries.slice(0, HISTORY_MAX)
    window.localStorage.setItem(HISTORY_KEY, JSON.stringify(trimmed))
  } catch {
    /* localStorage quota or serialization issue — best-effort, never fatal */
  }
}

// ── Sub-components ─────────────────────────────────────────────────────────

function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-4">
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
      <div className="error-state-title">Retention backend unreachable</div>
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
      <Database className="empty-state-icon" size={28} />
      <div className="empty-state-title">{title}</div>
      <div className="empty-state-desc">{desc}</div>
    </div>
  )
}

function HorizonBadge({ days }: { days: number }) {
  const cls = days <= 7 ? 'badge-amber' : days <= 30 ? 'badge-cyan' : 'badge-green'
  return <span className={`badge ${cls} text-[10px]`}>{days}d</span>
}

// ── Main panel ─────────────────────────────────────────────────────────────

export default function RetentionPanel() {
  const [health, setHealth] = useState<SystemHealth | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pruning, setPruning] = useState(false)
  const [pruneTarget, setPruneTarget] = useState<string>('all')
  const [lastResult, setLastResult] = useState<PruneAllResult | PruneSingleResult | null>(null)
  const [history, setHistory] = useState<PruneHistoryEntry[]>([])
  // Inline config editor — local-only state (backend has no PUT endpoint).
  const [editedHorizons, setEditedHorizons] = useState<Record<string, number>>(() =>
    Object.fromEntries(RETENTION_TARGETS.map((t) => [t.target, t.horizonDays])),
  )

  const fetchHealth = useCallback(async () => {
    try {
      const apiUrl = getApiUrl()
      const r = await apiFetch(`${apiUrl}/api/system/health`)
      if (r.ok) {
        setHealth(await r.json())
        setError(null)
      } else {
        setError(`GET /api/system/health → ${r.status} ${r.statusText}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 60s polling, paused when document hidden.
  useEffect(() => {
    fetchHealth()
    let timer: ReturnType<typeof setInterval> | null = null
    const start = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
        fetchHealth()
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
        fetchHealth()
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
  }, [fetchHealth])

  // Load client-side prune history on mount.
  useEffect(() => {
    setHistory(loadHistory())
  }, [])

  const triggerPrune = useCallback(
    async (target: string) => {
      setPruning(true)
      const startedAt = Date.now() / 1000
      let entry: PruneHistoryEntry = {
        id: `${startedAt}-${target}-${Math.random().toString(36).slice(2, 8)}`,
        timestamp: startedAt,
        target,
        triggered_by: 'manual',
        total_pruned: 0,
        success: false,
      }
      try {
        const apiUrl = getApiUrl()
        const r = await apiFetch(`${apiUrl}/api/system/prune`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ target }),
        })
        const payload = r.ok ? await r.json() : null
        if (r.ok) {
          if (target === 'all') {
            const all = payload as PruneAllResult
            entry = {
              ...entry,
              success: !!all?.success,
              total_pruned: all?.total_pruned ?? 0,
              per_store: Object.fromEntries(
                Object.entries(all?.results ?? {}).map(([k, v]) => [
                  k,
                  { pruned: v.pruned, error: v.error },
                ]),
              ),
            }
          } else {
            const single = payload as PruneSingleResult
            entry = {
              ...entry,
              success: true,
              total_pruned: single?.pruned ?? 0,
              per_store: { [target]: { pruned: single?.pruned ?? 0, error: null } },
            }
          }
          setLastResult(payload)
        } else {
          entry = { ...entry, success: false, error: `HTTP ${r.status} ${r.statusText}` }
        }
      } catch (e) {
        entry = {
          ...entry,
          success: false,
          error: e instanceof Error ? e.message : String(e),
        }
      } finally {
        setPruning(false)
        const next = [entry, ...loadHistory()].slice(0, HISTORY_MAX)
        setHistory(next)
        saveHistory(next)
        // Refresh table sizes after prune completes.
        fetchHealth()
      }
    },
    [fetchHealth],
  )

  const marketDb = health?.market_db
  const totalHistoryPruned = useMemo(
    () => history.reduce((acc, h) => acc + (h.total_pruned || 0), 0),
    [history],
  )
  const lastSuccessfulPrune = useMemo(
    () => history.find((h) => h.success),
    [history],
  )

  return (
    <div className="flex flex-col h-full bg-[var(--bg-surface)] border border-[#1f2335] rounded-lg overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap justify-between items-center gap-3 p-4 border-b border-[#1f2335] bg-[#13161e]">
        <div className="flex items-center gap-2.5">
          <div className="p-2 rounded-md bg-[var(--color-amber-bg)] border border-[var(--color-amber-bd)]">
            <Database className="text-[var(--color-amber-fg)]" size={18} />
          </div>
          <div>
            <h2 className="text-sm font-bold text-[#dde1ed] flex items-center gap-2">
              Data Retention &amp; Pruning
              <span className="badge badge-dim text-[9px]">Bounded-storage policy</span>
            </h2>
            <p className="text-[11px] text-[#7e8aaa] mt-0.5">
              Four SQLite stores · 7d / 30d / 30d / 90d horizons · <code className="mono text-[10px]">POST /api/system/prune</code>
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-cyan text-[9.5px]">
            <Server size={10} className="mr-1" />
            {history.length} ops logged
          </span>
          {lastSuccessfulPrune && (
            <span className="badge badge-green text-[9.5px]">
              <CheckCircle2 size={10} className="mr-1" />
              Last prune {formatRelativeTime(lastSuccessfulPrune.timestamp)}
            </span>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={fetchHealth}
            disabled={loading}
            className="h-7 text-[11px]"
          >
            <RefreshCw size={12} className={loading ? 'animate-spin mr-1' : 'mr-1'} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Body (scrollable) ─────────────────────────────────────────────── */}
      <div className="flex-1 overflow-y-auto scrollbar-thin p-4 space-y-4">
        {error && !health ? (
          <ErrorState message={error} onRetry={fetchHealth} />
        ) : loading && !health ? (
          <SkeletonRows rows={5} />
        ) : (
          <>
            {/* ── KPI Row ───────────────────────────────────────────────────── */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <HardDrive size={11} /> Market DB Size
                </span>
                <span className="kpi-value text-cyan-400">
                  {formatBytes(marketDb?.size_mb ?? 0)}
                </span>
                <span className="kpi-sub">
                  {marketDb?.db_backend ?? '—'}
                </span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Database size={11} /> Snapshots
                </span>
                <span className="kpi-value text-emerald-400">
                  {(marketDb?.snapshots_recorded ?? 0).toLocaleString()}
                </span>
                <span className="kpi-sub">market_snapshots table</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Clock size={11} /> Ticks
                </span>
                <span className="kpi-value text-amber-400">
                  {(marketDb?.ticks_recorded ?? 0).toLocaleString()}
                </span>
                <span className="kpi-sub">orderbook_ticks table</span>
              </div>
              <div className="kpi-card">
                <span className="kpi-label flex items-center gap-1">
                  <Trash2 size={11} /> Total Pruned
                </span>
                <span className="kpi-value text-[var(--color-blue-fg)]">
                  {totalHistoryPruned.toLocaleString()}
                </span>
                <span className="kpi-sub">rows (this browser session)</span>
              </div>
            </div>

            {/* ── Retention Policy Table ───────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title">Retention Policy by Store</span>
                <span className="badge badge-dim text-[9.5px]">
                  <ShieldCheck size={10} className="mr-1" />
                  Env-var overrides at boot
                </span>
              </div>
              <div className="table-container">
                <Table className="data-table">
                  <TableHeader>
                    <TableRow>
                      <TableHead>Store</TableHead>
                      <TableHead>Tables</TableHead>
                      <TableHead>Horizon</TableHead>
                      <TableHead>DB Path</TableHead>
                      <TableHead className="text-right">Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {RETENTION_TARGETS.map((t) => {
                      const checkKey =
                        t.target === 'observability'
                          ? 'observability'
                          : t.target === 'audit_events'
                            ? 'audit'
                            : t.target === 'decision_ledger'
                              ? 'decision_ledger'
                              : 'execution_quality'
                      const check = health?.checks?.[checkKey]
                      const up = !!check && ['UP', 'HEALTHY', 'OK'].includes(check.status)
                      return (
                        <TableRow key={t.target}>
                          <TableCell className="label-col">
                            <div className="flex flex-col">
                              <span className="font-semibold text-[#dde1ed]">{t.label}</span>
                              <span className="text-[10px] text-[#5a637a]">{t.rationale}</span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-col gap-0.5">
                              {t.tables.map((tbl) => (
                                <code
                                  key={tbl}
                                  className="mono text-[10px] text-[var(--color-cyan-fg)]"
                                >
                                  {tbl}
                                </code>
                              ))}
                            </div>
                          </TableCell>
                          <TableCell>
                            <HorizonBadge days={t.horizonDays} />
                          </TableCell>
                          <TableCell>
                            <div className="flex flex-col">
                              <code className="mono text-[10px] text-[#dde1ed]">
                                {t.defaultDbPath}
                              </code>
                              <code className="mono text-[9px] text-[#5a637a]">{t.envVar}</code>
                            </div>
                          </TableCell>
                          <TableCell className="text-right">
                            {check ? (
                              <span
                                className={`badge ${up ? 'badge-green' : 'badge-amber'} text-[9.5px]`}
                                title={check.detail}
                              >
                                {check.status}
                              </span>
                            ) : (
                              <span className="badge badge-dim text-[9.5px]">no probe</span>
                            )}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            </div>

            {/* ── Manual Prune ─────────────────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <Trash2 size={12} /> Manual Prune
                </span>
                {lastResult && (
                  <span className="badge badge-cyan text-[9.5px]">
                    Last: {(lastResult as PruneAllResult).total_pruned ??
                      (lastResult as PruneSingleResult).pruned ?? 0}{' '}
                    rows deleted
                  </span>
                )}
              </div>
              <div className="p-4 space-y-3">
                <div className="flex flex-wrap items-end gap-3">
                  <div className="flex-1 min-w-[200px]">
                    <label className="text-[10px] uppercase tracking-wider text-[#5a637a] font-bold mb-1 block">
                      Target store
                    </label>
                    <Select value={pruneTarget} onValueChange={setPruneTarget}>
                      <SelectTrigger className="h-9 bg-[#0e1015] border-[#1f2335] text-[#dde1ed] text-xs">
                        <SelectValue placeholder="Select target" />
                      </SelectTrigger>
                      <SelectContent className="bg-[#13161e] border-[#1f2335]">
                        <SelectItem value="all" className="text-[#dde1ed] focus:bg-[#1f2335]">
                          <span className="font-semibold">all stores</span>
                          <span className="text-[10px] text-[#7e8aaa] ml-2">(run_all_pruning)</span>
                        </SelectItem>
                        {RETENTION_TARGETS.map((t) => (
                          <SelectItem
                            key={t.target}
                            value={t.target}
                            className="text-[#dde1ed] focus:bg-[#1f2335]"
                          >
                            {t.label}
                            <span className="text-[10px] text-[#7e8aaa] ml-2">
                              ({t.horizonDays}d)
                            </span>
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <AlertDialog>
                    <AlertDialogTrigger asChild>
                      <Button
                        variant="destructive"
                        size="sm"
                        disabled={pruning}
                        className="bg-[var(--color-red-bg)] border border-[var(--color-red-bd)] text-[var(--color-red-fg)] hover:bg-[var(--color-red-bd)]"
                      >
                        {pruning ? (
                          <>
                            <Loader2 size={14} className="mr-1.5 animate-spin" />
                            Pruning…
                          </>
                        ) : (
                          <>
                            <Trash2 size={14} className="mr-1.5" />
                            Prune Now
                          </>
                        )}
                      </Button>
                    </AlertDialogTrigger>
                    <AlertDialogContent className="bg-[#13161e] border-[#1f2335] text-[#dde1ed]">
                      <AlertDialogHeader>
                        <AlertDialogTitle className="text-[#dde1ed] flex items-center gap-2">
                          <AlertTriangle size={16} className="text-[var(--color-amber-fg)]" />
                          Confirm immediate prune
                        </AlertDialogTitle>
                        <AlertDialogDescription className="text-[#7e8aaa] text-xs">
                          This will permanently delete rows older than the configured horizon from{' '}
                          <span className="font-semibold text-[var(--color-amber-fg)]">
                            {pruneTarget === 'all' ? 'all four stores' : RETENTION_TARGETS.find((t) => t.target === pruneTarget)?.label}
                          </span>
                          . The operation is irreversible (no soft-delete).
                          {pruneTarget === 'all' ? (
                            <ul className="mt-2 space-y-1 list-disc list-inside">
                              {RETENTION_TARGETS.map((t) => (
                                <li key={t.target} className="text-[11px]">
                                  <code className="mono text-[var(--color-cyan-fg)]">{t.target}</code>{' '}
                                  — rows older than <span className="font-semibold">{t.horizonDays}d</span> ({t.tables.join(', ')})
                                </li>
                              ))}
                            </ul>
                          ) : (
                            <div className="mt-2 text-[11px]">
                              Target tables:{' '}
                              <code className="mono text-[var(--color-cyan-fg)]">
                                {RETENTION_TARGETS.find((t) => t.target === pruneTarget)?.tables.join(', ')}
                              </code>
                            </div>
                          )}
                        </AlertDialogDescription>
                      </AlertDialogHeader>
                      <AlertDialogFooter>
                        <AlertDialogCancel className="bg-[#0e1015] border-[#1f2335] text-[#dde1ed] hover:bg-[#1f2335]">
                          Cancel
                        </AlertDialogCancel>
                        <AlertDialogAction
                          onClick={() => triggerPrune(pruneTarget)}
                          className="bg-[var(--color-red-bg)] border border-[var(--color-red-bd)] text-[var(--color-red-fg)] hover:bg-[var(--color-red-bd)]"
                        >
                          <Trash2 size={14} className="mr-1.5" />
                          Delete rows
                        </AlertDialogAction>
                      </AlertDialogFooter>
                    </AlertDialogContent>
                  </AlertDialog>
                </div>
                {lastResult && (
                  <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 text-xs">
                    <div className="flex items-center gap-2 mb-2">
                      {(lastResult as PruneAllResult).success === true ||
                      (lastResult as PruneAllResult).success === false ? (
                        <CheckCircle2 size={12} className="text-emerald-400" />
                      ) : (
                        <CheckCircle2 size={12} className="text-cyan-400" />
                      )}
                      <span className="font-semibold text-[#dde1ed]">
                        Prune result —{' '}
                        {new Date(
                          ((lastResult as PruneAllResult).timestamp ?? Date.now() / 1000) * 1000,
                        ).toLocaleTimeString()}
                      </span>
                    </div>
                    {(lastResult as PruneAllResult).results ? (
                      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                        {Object.entries((lastResult as PruneAllResult).results).map(([k, v]) => (
                          <div
                            key={k}
                            className="bg-[#13161e] border border-[#1f2335] rounded p-2 text-center"
                          >
                            <div className="text-[9px] text-[#5a637a] uppercase tracking-wider">
                              {k}
                            </div>
                            <div className="mono text-sm font-bold text-cyan-400 mt-0.5">
                              {v.pruned.toLocaleString()}
                            </div>
                            {v.error && (
                              <div className="text-[9px] text-red-400 mt-0.5 truncate" title={v.error}>
                                {v.error}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    ) : (
                      <div className="text-cyan-400 mono">
                        Deleted {(lastResult as PruneSingleResult).pruned.toLocaleString()} row(s) from{' '}
                        <code>{(lastResult as PruneSingleResult).target}</code>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {/* ── Prune History ────────────────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <History size={12} /> Prune History
                </span>
                <span className="badge badge-dim text-[9.5px]">
                  client-side · localStorage
                </span>
              </div>
              {history.length === 0 ? (
                <EmptyState
                  title="No prune operations logged yet"
                  desc="Manual and auto-triggered prunes will appear here. History is kept locally per browser."
                />
              ) : (
                <div className="table-container max-h-72">
                  <Table className="data-table">
                    <TableHeader>
                      <TableRow>
                        <TableHead>When</TableHead>
                        <TableHead>Target</TableHead>
                        <TableHead className="text-right">Rows Deleted</TableHead>
                        <TableHead>Per-store detail</TableHead>
                        <TableHead className="text-right">Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {history.map((h) => (
                        <TableRow key={h.id}>
                          <TableCell className="label-col">
                            <div className="flex flex-col">
                              <span>{new Date(h.timestamp * 1000).toLocaleTimeString()}</span>
                              <span className="text-[10px] text-[#5a637a]">
                                {formatRelativeTime(h.timestamp)}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell>
                            <code className="mono text-[11px] text-[var(--color-cyan-fg)]">
                              {h.target}
                            </code>
                          </TableCell>
                          <TableCell className="text-right mono text-cyan-300 font-bold">
                            {h.total_pruned.toLocaleString()}
                          </TableCell>
                          <TableCell>
                            {h.per_store ? (
                              <div className="flex flex-wrap gap-1">
                                {Object.entries(h.per_store).map(([k, v]) => (
                                  <span
                                    key={k}
                                    className={`badge ${v.error ? 'badge-red' : 'badge-dim'} text-[9px]`}
                                    title={v.error ?? ''}
                                  >
                                    {k}: {v.pruned}
                                  </span>
                                ))}
                              </div>
                            ) : (
                              <span className="text-[10px] text-[#5a637a]">—</span>
                            )}
                          </TableCell>
                          <TableCell className="text-right">
                            {h.success ? (
                              <span className="badge badge-green text-[9.5px]">
                                <CheckCircle2 size={10} className="mr-1" /> OK
                              </span>
                            ) : (
                              <span
                                className="badge badge-red text-[9.5px]"
                                title={h.error ?? 'failed'}
                              >
                                <XCircle size={10} className="mr-1" /> FAIL
                              </span>
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </div>

            {/* ── Inline Config Editor ─────────────────────────────────────── */}
            <div className="card">
              <div className="card-header">
                <span className="card-title flex items-center gap-1.5">
                  <ShieldCheck size={12} /> Horizon Configuration
                </span>
                <span className="badge badge-amber text-[9.5px]">
                  <AlertTriangle size={10} className="mr-1" />
                  read-only — env-var override required
                </span>
              </div>
              <div className="p-4 space-y-3">
                <p className="text-[11px] text-[#7e8aaa] leading-relaxed">
                  Retention horizons are loaded from{' '}
                  <code className="mono text-[10px] text-[var(--color-cyan-fg)]">core/retention.py</code>{' '}
                  module constants at boot. Runtime updates require an env-var
                  override + service restart — there is no live PUT endpoint yet.
                  The form below stages local-only edits for review (no backend
                  write).
                </p>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2.5">
                  {RETENTION_TARGETS.map((t) => {
                    const edited = editedHorizons[t.target] ?? t.horizonDays
                    const dirty = edited !== t.horizonDays
                    return (
                      <div
                        key={t.target}
                        className={`bg-[#0e1015] border rounded-md p-2.5 ${
                          dirty ? 'border-[var(--color-amber-bd)]' : 'border-[#1f2335]'
                        }`}
                      >
                        <div className="flex items-center justify-between mb-1.5">
                          <span className="text-[11px] font-semibold text-[#dde1ed]">
                            {t.label}
                          </span>
                          {dirty && (
                            <span className="badge badge-amber text-[9px]">unsaved</span>
                          )}
                        </div>
                        <div className="flex items-center gap-2">
                          <Input
                            type="number"
                            min={1}
                            max={3650}
                            value={edited}
                            onChange={(e) => {
                              const v = parseInt(e.target.value, 10)
                              setEditedHorizons((prev) => ({
                                ...prev,
                                [t.target]: Number.isFinite(v) ? Math.max(1, v) : t.horizonDays,
                              }))
                            }}
                            className="h-8 bg-[#13161e] border-[#1f2335] text-[#dde1ed] mono text-xs"
                          />
                          <span className="text-[11px] text-[#7e8aaa]">days</span>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="h-8 px-2 text-[10px] text-[#7e8aaa] hover:text-[#dde1ed]"
                            onClick={() =>
                              setEditedHorizons((prev) => ({ ...prev, [t.target]: t.horizonDays }))
                            }
                            disabled={!dirty}
                          >
                            Reset
                          </Button>
                        </div>
                        <div className="text-[9.5px] text-[#5a637a] mt-1.5">
                          env: <code className="mono">{t.envVar}</code>
                        </div>
                      </div>
                    )
                  })}
                </div>
                <div className="flex justify-end gap-2 pt-1">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setEditedHorizons(
                        Object.fromEntries(RETENTION_TARGETS.map((t) => [t.target, t.horizonDays])),
                      )
                    }
                    className="h-8 text-[11px] bg-[#0e1015] border-[#1f2335] text-[#dde1ed] hover:bg-[#1f2335]"
                  >
                    Reset all
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled
                    className="h-8 text-[11px] bg-[var(--color-amber-bg)] border-[var(--color-amber-bd)] text-[var(--color-amber-fg)] opacity-70 cursor-not-allowed"
                    title="Backend has no PUT endpoint — env-var override required"
                  >
                    <AlertTriangle size={12} className="mr-1.5" />
                    Apply (no backend endpoint)
                  </Button>
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ── Footer ─────────────────────────────────────────────────────────── */}
      <div className="flex justify-between items-center px-4 py-2 border-t border-[#1f2335] bg-[#13161e] text-[10px] text-[#5a637a]">
        <span>
          Auto-refresh: <span className="mono text-[var(--color-blue-fg)]">60s</span>
          {typeof document !== 'undefined' && document.visibilityState === 'hidden' && ' (paused)'}
        </span>
        <span className="mono">
          {health ? `last sync ${formatRelativeTime(Math.floor(Date.now() / 1000))}` : 'no sync'}
        </span>
      </div>
    </div>
  )
}
