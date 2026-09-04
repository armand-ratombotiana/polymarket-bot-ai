// components/DatabaseStatusPanel.tsx — Database Status Panel (W21-7)
//
// Exposes the live database backend (PostgreSQL vs SQLite) and PG-pool
// health stats so the trader can see at-a-glance whether the system is
// running on the primary PG store or has fallen back to the SQLite
// standby, plus how many times the fallback has fired, the row count /
// on-disk size of each persisted table, and the last 5 connection
// errors. A manual "Retry PG Connection" button lets the operator
// re-arm the PG pool without restarting the bot.
//
// Backend contract (mirrors the AsyncDBPool standby surface in
// `mini-services/polymarket-bot/core/db_pool.py` + `core/async_repositories.py`):
//
//   GET /api/system/db-status
//     → {
//         backend: 'postgresql' | 'sqlite',
//         pg_health: {
//           status: 'healthy' | 'degraded' | 'unhealthy' | 'unknown',
//           uptime_pct: number,
//           avg_latency_ms: number,
//           last_check_epoch: number,
//           consecutive_failures: number,
//           pool_size: number,
//           pool_in_use: number,
//         } | null,
//         fallback_counter: number,
//         tables: Array<{ name: string; row_count: number; size_mb: number;
//                         database: 'pg' | 'sqlite'; last_modified: number }>,
//         recent_errors: Array<{ timestamp: number; error: string;
//                                retry_attempt: number; backend: string }>,
//         generated_at: number,
//       }
//
//   POST /api/system/db-retry
//     → { success: boolean; backend: string; message: string; attempted_at: number }
//
// Visual language mirrors SystemHealthView.tsx + ObservabilityPanel.tsx
// (dark `#13161e` card surface, `#1f2335` borders, `#dde1ed` primary
// text) but uses shadcn/ui primitives (Card, Badge, Table, Button) per
// the W21-7 spec. Polls every 15s and pauses when the document is
// hidden (matches the visibility-aware polling pattern established
// by ObservabilityPanel.tsx).

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { apiFetch } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import {
  Database,
  Server,
  Activity,
  AlertTriangle,
  RefreshCw,
  CheckCircle2,
  XCircle,
  Clock,
  Layers,
  Zap,
} from 'lucide-react'

// ────────────────────────────────────────────────────────────────────────────
// Types — mirror the JSON shape documented above
// ────────────────────────────────────────────────────────────────────────────

export type DbBackend = 'postgresql' | 'sqlite'
export type PgHealthStatus = 'healthy' | 'degraded' | 'unhealthy' | 'unknown'

export interface PgHealthReport {
  status: PgHealthStatus
  uptime_pct: number
  avg_latency_ms: number
  last_check_epoch: number
  consecutive_failures: number
  pool_size: number
  pool_in_use: number
}

export interface DbTableStat {
  name: string
  row_count: number
  size_mb: number
  database: 'pg' | 'sqlite'
  last_modified: number
}

export interface DbErrorEntry {
  timestamp: number
  error: string
  retry_attempt: number
  backend: string
}

export interface DatabaseStatusPayload {
  backend: DbBackend
  pg_health: PgHealthReport | null
  fallback_counter: number
  tables: DbTableStat[]
  recent_errors: DbErrorEntry[]
  generated_at: number
}

export interface DatabaseRetryResult {
  success: boolean
  backend: string
  message: string
  attempted_at: number
}

// ────────────────────────────────────────────────────────────────────────────
// Constants
// ────────────────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 15_000
const STATUS_ENDPOINT = '/api/system/db-status'
const RETRY_ENDPOINT = '/api/system/db-retry'

// ────────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ────────────────────────────────────────────────────────────────────────────

function formatBytes(mb: number | undefined | null): string {
  if (mb === undefined || mb === null || Number.isNaN(mb)) return '—'
  if (mb < 1 / 1024) return `${(mb * 1024 * 1024).toFixed(0)} B`
  if (mb < 1) return `${(mb * 1024).toFixed(1)} KB`
  if (mb < 1024) return `${mb.toFixed(2)} MB`
  return `${(mb / 1024).toFixed(2)} GB`
}

function formatRowCount(n: number | undefined | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—'
  return n.toLocaleString()
}

function formatRelativeTime(epoch: number | undefined | null): string {
  if (!epoch) return '—'
  const diff = Date.now() / 1000 - epoch
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.round(diff)}s ago`
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`
  return `${Math.round(diff / 86400)}d ago`
}

function formatUptimePct(pct: number | undefined | null): string {
  if (pct === undefined || pct === null || Number.isNaN(pct)) return '—'
  return `${pct.toFixed(2)}%`
}

function formatLatency(ms: number | undefined | null): string {
  if (ms === undefined || ms === null || Number.isNaN(ms)) return '—'
  return `${ms.toFixed(1)}ms`
}

// ────────────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────────────

interface BackendBadgeProps {
  backend: DbBackend
}

function BackendBadge({ backend }: BackendBadgeProps) {
  const isPg = backend === 'postgresql'
  return (
    <Badge
      variant={isPg ? 'success' : 'warning'}
      className="px-3 py-1.5 text-sm font-bold gap-2"
      data-testid="db-backend-badge"
    >
      <span
        className={`inline-block h-2 w-2 rounded-full ${
          isPg ? 'bg-green-400' : 'bg-amber-400'
        }`}
        aria-hidden="true"
      />
      {isPg ? 'PostgreSQL' : 'SQLite'}
    </Badge>
  )
}

interface HealthBadgeProps {
  status: PgHealthStatus
}

function HealthBadge({ status }: HealthBadgeProps) {
  // healthy → green, degraded → amber, unhealthy → red, unknown → dim
  const variant: 'success' | 'warning' | 'destructive' | 'secondary' =
    status === 'healthy'
      ? 'success'
      : status === 'degraded'
        ? 'warning'
        : status === 'unhealthy'
          ? 'destructive'
          : 'secondary'
  const label =
    status === 'healthy'
      ? 'Healthy'
      : status === 'degraded'
        ? 'Degraded'
        : status === 'unhealthy'
          ? 'Unhealthy'
          : 'Unknown'
  return (
    <Badge variant={variant} className="px-2 py-1 text-xs gap-1.5">
      {status === 'healthy' ? (
        <CheckCircle2 size={12} aria-hidden="true" />
      ) : status === 'unhealthy' ? (
        <XCircle size={12} aria-hidden="true" />
      ) : (
        <AlertTriangle size={12} aria-hidden="true" />
      )}
      {label}
    </Badge>
  )
}

interface KpiCardProps {
  label: string
  value: string
  sub?: string
  valueClass?: string
  icon?: typeof Database
}

function KpiCard({ label, value, sub, valueClass, icon: Icon }: KpiCardProps) {
  return (
    <div className="kpi-card" data-testid="db-kpi-card">
      <span className="kpi-label flex items-center gap-1.5">
        {Icon && <Icon size={11} aria-hidden="true" />}
        {label}
      </span>
      <span className={`kpi-value ${valueClass ?? ''}`}>{value}</span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  )
}

interface ErrorStateProps {
  message: string
  onRetry: () => void
  retrying?: boolean
}

function ErrorState({ message, onRetry, retrying }: ErrorStateProps) {
  return (
    <div className="error-state p-8" role="alert">
      <AlertTriangle
        className="error-state-icon text-[#f87171]"
        size={28}
        aria-hidden="true"
      />
      <div className="error-state-title">Database status endpoint unavailable</div>
      <div className="error-state-desc">{message}</div>
      <Button
        variant="outline"
        size="sm"
        onClick={onRetry}
        className="mt-2"
        disabled={retrying}
        aria-label="Retry database status fetch"
      >
        <RefreshCw size={14} className={retrying ? 'animate-spin' : ''} />
        {retrying ? 'Retrying…' : 'Retry'}
      </Button>
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="skeleton h-12 w-full rounded-md" />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="skeleton h-20 w-full rounded-md" />
        ))}
      </div>
      <div className="skeleton h-48 w-full rounded-md" />
      <div className="skeleton h-32 w-full rounded-md" />
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Main panel
// ────────────────────────────────────────────────────────────────────────────

export default function DatabaseStatusPanel() {
  const [status, setStatus] = useState<DatabaseStatusPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [retrying, setRetrying] = useState(false)
  const [retryResult, setRetryResult] = useState<DatabaseRetryResult | null>(null)

  const fetchStatus = useCallback(async () => {
    try {
      const r = await apiFetch(STATUS_ENDPOINT)
      if (r.ok) {
        const json = (await r.json()) as DatabaseStatusPayload
        setStatus(json)
        setError(null)
      } else {
        setError(`GET ${STATUS_ENDPOINT} → ${r.status} ${r.statusText}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 15s polling, paused when document hidden.
  // The tick itself re-checks `document.hidden` so a visibility flip
  // that happens between the visibilitychange event and the next tick
  // still short-circuits (mirrors RateLimitPanel.tsx + ObservabilityPanel).
  useEffect(() => {
    fetchStatus()
    let timer: ReturnType<typeof setInterval> | null = null
    const startPolling = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchStatus()
      }, POLL_INTERVAL_MS)
    }
    const stopPolling = () => {
      if (timer) {
        clearInterval(timer)
        timer = null
      }
    }
    const onVisibility = () => {
      if (typeof document !== 'undefined' && document.hidden) {
        stopPolling()
      } else {
        // Refresh immediately on tab regain so the operator doesn't
        // see a stale snapshot for up to 15s after switching back.
        fetchStatus()
        startPolling()
      }
    }
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility)
    }
    startPolling()
    return () => {
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', onVisibility)
      }
      stopPolling()
    }
  }, [fetchStatus])

  const handleRetryPg = useCallback(async () => {
    setRetrying(true)
    setRetryResult(null)
    try {
      const r = await apiFetch(RETRY_ENDPOINT, { method: 'POST' })
      if (r.ok) {
        const json = (await r.json()) as DatabaseRetryResult
        setRetryResult(json)
        // Re-fetch the status immediately so the operator sees the
        // post-retry state without waiting for the next poll tick.
        await fetchStatus()
      } else {
        setRetryResult({
          success: false,
          backend: 'unknown',
          message: `POST ${RETRY_ENDPOINT} → ${r.status} ${r.statusText}`,
          attempted_at: Date.now() / 1000,
        })
      }
    } catch (e) {
      setRetryResult({
        success: false,
        backend: 'unknown',
        message: e instanceof Error ? e.message : String(e),
        attempted_at: Date.now() / 1000,
      })
    } finally {
      setRetrying(false)
    }
  }, [fetchStatus])

  const handleManualRefresh = useCallback(() => {
    fetchStatus()
  }, [fetchStatus])

  // ── Derived display values ──────────────────────────────────────────────

  const backend = status?.backend ?? 'sqlite'
  const pgHealth = status?.pg_health ?? null
  const healthStatus: PgHealthStatus = pgHealth?.status ?? 'unknown'
  const fallbackCounter = status?.fallback_counter ?? 0
  const tables = status?.tables ?? []
  const recentErrors = status?.recent_errors ?? []

  const totalRows = useMemo(
    () => tables.reduce((sum, t) => sum + (t.row_count ?? 0), 0),
    [tables],
  )
  const totalSizeMb = useMemo(
    () => tables.reduce((sum, t) => sum + (t.size_mb ?? 0), 0),
    [tables],
  )

  // ── Render ──────────────────────────────────────────────────────────────

  if (loading && !status) {
    return (
      <div
        className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden"
        role="status"
        aria-live="polite"
        aria-label="Loading database status…"
      >
        <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center gap-2 bg-[#0e1015]/80">
          <span className="spinner" aria-hidden="true" />
          <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
            Loading Database Status…
          </span>
        </div>
        <LoadingSkeleton />
      </div>
    )
  }

  if (error && !status) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4">
        <ErrorState message={error} onRetry={handleManualRefresh} />
      </div>
    )
  }

  return (
    <div
      className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin"
      data-testid="database-status-panel"
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-2">
        <div>
          <div className="flex items-center gap-2">
            <Database size={18} className="text-cyan-400" aria-hidden="true" />
            <span className="text-sm font-bold text-[#dde1ed]">
              Database Backend Status
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            PostgreSQL primary · SQLite fallback · pool health, table stats &amp; recent errors
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-dim text-[9.5px]">15s poll</span>
          <BackendBadge backend={backend} />
          <Button
            variant="outline"
            size="sm"
            onClick={handleManualRefresh}
            className="h-7 px-2 text-xs border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450]"
            aria-label="Refresh database status"
            disabled={retrying}
          >
            <RefreshCw size={12} className={retrying ? 'animate-spin' : ''} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── KPI Cards ─────────────────────────────────────────────────── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <KpiCard
          label="Active Backend"
          value={backend === 'postgresql' ? 'PostgreSQL' : 'SQLite'}
          sub={
            backend === 'postgresql'
              ? 'Primary PG pool active'
              : 'Fallback SQLite active'
          }
          valueClass={
            backend === 'postgresql'
              ? 'text-green-400'
              : 'text-amber-400'
          }
          icon={Server}
        />
        <KpiCard
          label="PG Uptime"
          value={formatUptimePct(pgHealth?.uptime_pct)}
          sub={
            pgHealth
              ? `Avg ${formatLatency(pgHealth.avg_latency_ms)}`
              : 'PG pool not configured'
          }
          valueClass={
            !pgHealth
              ? 'text-[#7e8aaa]'
              : pgHealth.uptime_pct >= 99
                ? 'text-green-400'
                : pgHealth.uptime_pct >= 90
                  ? 'text-amber-400'
                  : 'text-red-400'
          }
          icon={Activity}
        />
        <KpiCard
          label="SQLite Fallbacks"
          value={formatRowCount(fallbackCounter)}
          sub={
            fallbackCounter === 0
              ? 'No fallbacks recorded'
              : 'Fallbacks to SQLite'
          }
          valueClass={
            fallbackCounter === 0
              ? 'text-green-400'
              : fallbackCounter < 5
                ? 'text-amber-400'
                : 'text-red-400'
          }
          icon={AlertTriangle}
        />
        <KpiCard
          label="Total Rows"
          value={formatRowCount(totalRows)}
          sub={`${formatBytes(totalSizeMb)} across ${tables.length} tables`}
          valueClass="text-cyan-400"
          icon={Layers}
        />
      </div>

      {/* ── PG Connection Health ──────────────────────────────────────── */}
      <Card className="bg-[#0e1015] border-[#1f2335] py-0 gap-0">
        <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
          <CardTitle className="text-xs font-bold text-[#dde1ed] flex items-center gap-2">
            <Server size={12} className="text-green-400" aria-hidden="true" />
            PostgreSQL Connection Health
            {pgHealth && <HealthBadge status={healthStatus} />}
          </CardTitle>
        </CardHeader>
        <CardContent className="px-3 py-3">
          {pgHealth ? (
            <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5 text-xs">
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Status
                </div>
                <div
                  className={`font-bold ${
                    healthStatus === 'healthy'
                      ? 'text-green-400'
                      : healthStatus === 'degraded'
                        ? 'text-amber-400'
                        : healthStatus === 'unhealthy'
                          ? 'text-red-400'
                          : 'text-[#7e8aaa]'
                  }`}
                >
                  {healthStatus.charAt(0).toUpperCase() + healthStatus.slice(1)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Uptime
                </div>
                <div className="font-bold mono text-[#dde1ed]">
                  {formatUptimePct(pgHealth.uptime_pct)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Avg Latency
                </div>
                <div className="font-bold mono text-cyan-400">
                  {formatLatency(pgHealth.avg_latency_ms)}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Pool In-Use
                </div>
                <div className="font-bold mono text-[#dde1ed]">
                  {pgHealth.pool_in_use}/{pgHealth.pool_size}
                </div>
              </div>
              <div>
                <div className="text-[10px] uppercase tracking-wider text-[#7e8aaa] mb-0.5">
                  Consecutive Failures
                </div>
                <div
                  className={`font-bold mono ${
                    pgHealth.consecutive_failures === 0
                      ? 'text-green-400'
                      : pgHealth.consecutive_failures < 3
                        ? 'text-amber-400'
                        : 'text-red-400'
                  }`}
                >
                  {pgHealth.consecutive_failures}
                </div>
              </div>
            </div>
          ) : (
            <div className="text-xs text-[#7e8aaa] py-2">
              <Clock size={14} className="inline mr-1.5 -mt-0.5" aria-hidden="true" />
              PostgreSQL pool is not configured — operating on the SQLite
              standby backend. Last status check:{' '}
              <span className="mono">
                {formatRelativeTime(status?.generated_at ?? 0)}
              </span>
              .
            </div>
          )}

          {/* Manual retry button + result banner */}
          <div className="mt-3 flex flex-wrap items-center gap-2 pt-3 border-t border-[#1f2335]">
            <Button
              variant="outline"
              size="sm"
              onClick={handleRetryPg}
              disabled={retrying}
              className="h-7 px-3 text-xs border-[#1f2335] text-[#dde1ed] hover:bg-[#1f2335] hover:text-white"
              aria-label="Retry PostgreSQL connection"
            >
              {retrying ? (
                <RefreshCw size={12} className="animate-spin" />
              ) : (
                <Zap size={12} className="text-amber-400" />
              )}
              {retrying ? 'Retrying…' : 'Retry PG Connection'}
            </Button>
            {retryResult && (
              <span
                role="status"
                aria-live="polite"
                className={`text-[11px] mono ${
                  retryResult.success
                    ? 'text-green-400'
                    : 'text-red-400'
                }`}
              >
                {retryResult.success ? '✓' : '✗'}{' '}
                {retryResult.message} · {formatRelativeTime(retryResult.attempted_at)}
              </span>
            )}
          </div>
        </CardContent>
      </Card>

      {/* ── Database Tables ───────────────────────────────────────────── */}
      <Card className="bg-[#0e1015] border-[#1f2335] py-0 gap-0">
        <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
          <CardTitle className="text-xs font-bold text-[#dde1ed] flex items-center gap-2">
            <Layers size={12} className="text-cyan-400" aria-hidden="true" />
            Database Tables
            <span className="text-[10px] text-[#7e8aaa] font-normal mono">
              ({tables.length} tables · {formatRowCount(totalRows)} rows ·{' '}
              {formatBytes(totalSizeMb)})
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="px-3 py-2">
          {tables.length === 0 ? (
            <div className="empty-state py-8">
              <Database
                className="empty-state-icon"
                size={24}
                aria-hidden="true"
              />
              <div className="empty-state-title">No table statistics available</div>
              <div className="empty-state-desc">
                The backend has not reported table-level row counts or sizes.
                This is normal when the SQLite standby is empty or when the PG
                pool has not yet mirrored schema to its read replica.
              </div>
            </div>
          ) : (
            <div className="max-h-72 overflow-y-auto scrollbar-thin">
              <Table>
                <TableHeader>
                  <TableRow className="border-[#1f2335] hover:bg-transparent">
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Table
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Database
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2 text-right">
                      Rows
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2 text-right">
                      Size
                    </TableHead>
                    <TableHead className="text-[10px] uppercase tracking-wider text-[#7e8aaa] h-8 px-2">
                      Last Modified
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tables.map((t, i) => (
                    <TableRow
                      key={`${t.name}-${i}`}
                      className="border-[#1f2335] hover:bg-[#13161e]/60"
                    >
                      <TableCell className="mono text-xs text-[#dde1ed] px-2 py-1.5">
                        {t.name}
                      </TableCell>
                      <TableCell className="px-2 py-1.5">
                        <Badge
                          variant={t.database === 'pg' ? 'success' : 'warning'}
                          className="text-[9.5px] px-1.5 py-0"
                        >
                          {t.database === 'pg' ? 'PG' : 'SQLite'}
                        </Badge>
                      </TableCell>
                      <TableCell className="mono text-xs text-cyan-400 px-2 py-1.5 text-right">
                        {formatRowCount(t.row_count)}
                      </TableCell>
                      <TableCell className="mono text-xs text-[#7e8aaa] px-2 py-1.5 text-right">
                        {formatBytes(t.size_mb)}
                      </TableCell>
                      <TableCell className="mono text-[10px] text-[#7e8aaa] px-2 py-1.5">
                        {formatRelativeTime(t.last_modified)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Recent Errors ────────────────────────────────────────────── */}
      <Card className="bg-[#0e1015] border-[#1f2335] py-0 gap-0">
        <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
          <CardTitle className="text-xs font-bold text-[#dde1ed] flex items-center gap-2">
            <AlertTriangle
              size={12}
              className="text-red-400"
              aria-hidden="true"
            />
            Recent Connection Errors
            <span className="text-[10px] text-[#7e8aaa] font-normal mono">
              (last {Math.min(recentErrors.length, 5)})
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="px-3 py-2">
          {recentErrors.length === 0 ? (
            <div className="text-xs text-green-400 py-3 flex items-center gap-2">
              <CheckCircle2 size={14} aria-hidden="true" />
              No connection errors recorded in the active window.
            </div>
          ) : (
            <div className="max-h-60 overflow-y-auto scrollbar-thin space-y-1.5">
              {recentErrors.slice(0, 5).map((e, i) => (
                <div
                  key={`${e.timestamp}-${i}`}
                  className="flex items-start gap-2 bg-[#13161e] p-2 rounded border border-[#1f2335] text-xs"
                >
                  <XCircle
                    size={12}
                    className="text-red-400 flex-shrink-0 mt-0.5"
                    aria-hidden="true"
                  />
                  <div className="flex-1 min-w-0">
                    <div className="text-[#dde1ed] break-words">{e.error}</div>
                    <div className="text-[10px] text-[#7e8aaa] mono mt-0.5">
                      <span>{formatRelativeTime(e.timestamp)}</span>
                      {e.backend && <span> · backend: {e.backend}</span>}
                      {e.retry_attempt > 0 && (
                        <span> · retry #{e.retry_attempt}</span>
                      )}
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* ── Footer ───────────────────────────────────────────────────── */}
      <div className="text-[10px] text-[#7e8aaa] mono text-center pt-1">
        Generated at {formatRelativeTime(status?.generated_at)} · endpoint:{' '}
        <span className="text-cyan-400">{STATUS_ENDPOINT}</span>
      </div>
    </div>
  )
}
