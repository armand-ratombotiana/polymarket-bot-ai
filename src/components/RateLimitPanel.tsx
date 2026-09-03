// components/RateLimitPanel.tsx — Rate Limit Analytics Dashboard (W14-7)
//
// Surfaces the in-memory ``RateLimitTracker`` snapshot emitted by
// ``GET /api/rate-limit/stats`` (registered in ``api/server.py``). The
// panel surfaces five views of the same underlying data:
//
//   1. KPI cards     — total hits (last hour), top endpoint, top client,
//                       hit rate (hits/min).
//   2. Endpoint bar  — `PnLBarChart` of hits-by-endpoint (top 20).
//   3. Per-min line  — `Sparkline` of hits-per-minute over the last hour.
//   4. Top endpoints — table of most-rate-limited routes with counts.
//   5. Top clients   — table of client IPs with the most hits.
//
// Visual language mirrors ObservabilityPanel.tsx (dark `#13161e` card
// surface, `#1f2335` borders, `#dde1ed` primary text). Polls every
// 30s and pauses when the document is hidden (same visibility-aware
// pattern used across the W8–W13 panels).
'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { apiFetch } from '@/lib/api'
import {
  Activity,
  RefreshCw,
  AlertCircle,
  Inbox,
  TrendingUp,
  Globe,
  Server,
  Zap,
} from 'lucide-react'
import { PnLBarChart, Sparkline, chartTheme } from '@/components/charts'

// ───────────────────────────────────────────────────────────────────────────
// Types — mirror the JSON shape returned by api/server.py::rate_limit_stats
// ───────────────────────────────────────────────────────────────────────────

interface RateLimitStats {
  total_hits: number
  hits_per_minute_rate: number
  hits_by_endpoint: Record<string, number>
  hits_by_client: Record<string, number>
  hits_per_minute: Record<string, number>
  top_endpoints: Record<string, number>
}

// ───────────────────────────────────────────────────────────────────────────
// Constants
// ───────────────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 30_000

// ───────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ───────────────────────────────────────────────────────────────────────────

function formatNumber(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString('en-US')
}

function formatRate(n: number): string {
  if (!Number.isFinite(n)) return '—'
  return `${n.toFixed(2)} / min`
}

function formatRelativeTime(epochMs: number | null): string {
  if (epochMs == null) return '—'
  const diff = Date.now() - epochMs
  if (diff < 0) return 'just now'
  if (diff < 5_000) return 'just now'
  if (diff < 60_000) return `${Math.floor(diff / 1000)}s ago`
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`
  return `${Math.floor(diff / 3_600_000)}h ago`
}

function shortEndpoint(ep: string, maxLen = 36): string {
  if (!ep) return '—'
  if (ep.length <= maxLen) return ep
  // Keep the leading /api/<resource> and trail with ellipsis.
  return `${ep.slice(0, maxLen - 1)}…`
}

function shortIp(ip: string): string {
  if (!ip) return '—'
  return ip
}

// ───────────────────────────────────────────────────────────────────────────
// Subcomponents
// ───────────────────────────────────────────────────────────────────────────

interface KpiCardProps {
  label: string
  value: string
  hint?: string
  icon: typeof Activity
  accentClass?: string
}

function KpiCard({ label, value, hint, icon: Icon, accentClass = 'text-[#dde1ed]' }: KpiCardProps) {
  return (
    <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-2.5 flex flex-col gap-1 hover:border-[#2d3450] transition-colors">
      <div className="flex items-center gap-1.5 text-[10px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
        <Icon className="w-3 h-3 flex-shrink-0" />
        <span className="truncate">{label}</span>
      </div>
      <div className={`text-base font-bold ${accentClass} mono truncate`}>{value}</div>
      {hint && <div className="text-[9.5px] text-[#7e8aaa] truncate">{hint}</div>}
    </div>
  )
}

interface EndpointRowProps {
  endpoint: string
  count: number
  max: number
}

function EndpointRow({ endpoint, count, max }: EndpointRowProps) {
  const pct = max > 0 ? Math.min(100, (count / max) * 100) : 0
  return (
    <div className="flex items-center gap-2 py-1 border-b border-[#1f2335]/50 last:border-b-0">
      <div
        className="flex-1 min-w-0 text-[11px] mono text-[#dde1ed] truncate"
        title={endpoint}
      >
        {shortEndpoint(endpoint, 48)}
      </div>
      <div className="w-24 h-1.5 bg-[#1f2335] rounded-sm overflow-hidden flex-shrink-0">
        <div
          className="h-full bg-[#f59e0b] transition-all duration-300"
          style={{ width: `${pct}%` }}
          aria-hidden="true"
        />
      </div>
      <div className="w-12 text-right text-[11px] mono text-[#dde1ed] font-semibold">
        {formatNumber(count)}
      </div>
    </div>
  )
}

interface ClientRowProps {
  ip: string
  count: number
  max: number
}

function ClientRow({ ip, count, max }: ClientRowProps) {
  const pct = max > 0 ? Math.min(100, (count / max) * 100) : 0
  return (
    <div className="flex items-center gap-2 py-1 border-b border-[#1f2335]/50 last:border-b-0">
      <div
        className="flex-1 min-w-0 text-[11px] mono text-[#dde1ed] truncate"
        title={ip}
      >
        {shortIp(ip)}
      </div>
      <div className="w-24 h-1.5 bg-[#1f2335] rounded-sm overflow-hidden flex-shrink-0">
        <div
          className="h-full bg-[#10b981] transition-all duration-300"
          style={{ width: `${pct}%` }}
          aria-hidden="true"
        />
      </div>
      <div className="w-12 text-right text-[11px] mono text-[#dde1ed] font-semibold">
        {formatNumber(count)}
      </div>
    </div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Main panel
// ───────────────────────────────────────────────────────────────────────────

export default function RateLimitPanel() {
  const [stats, setStats] = useState<RateLimitStats | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const fetchingRef = useRef(false)

  // ── Fetcher ──────────────────────────────────────────────────────────
  const fetchStats = useCallback(async (silent = false) => {
    // Guard against overlapping fetches when the 30s interval fires
    // while a manual Refresh is still in flight.
    if (fetchingRef.current) return
    fetchingRef.current = true
    if (!silent) setRefreshing(true)
    try {
      const res = await apiFetch('/api/rate-limit/stats')
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`)
      }
      const data = (await res.json()) as RateLimitStats
      setStats(data)
      setError(null)
      setLastUpdated(Date.now())
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : String(e)
      setError(msg)
    } finally {
      fetchingRef.current = false
      if (!silent) setRefreshing(false)
      setLoading(false)
    }
  }, [])

  // ── Initial fetch + visibility-aware polling ─────────────────────────
  useEffect(() => {
    fetchStats()
    let timer: ReturnType<typeof setInterval> | null = null
    const startPolling = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchStats(true)
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
        // Refresh immediately on tab regain so the user doesn't see a
        // stale snapshot for up to 30s after switching back.
        fetchStats(true)
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
  }, [fetchStats])

  // ── Derived data for charts ──────────────────────────────────────────
  const endpointBarData = useMemo(() => {
    if (!stats?.hits_by_endpoint) return []
    return Object.entries(stats.hits_by_endpoint).map(([name, value]) => ({
      name: shortEndpoint(name, 24),
      value,
      sub: `${value} hits`,
    }))
  }, [stats])

  const perMinuteSeries = useMemo(() => {
    if (!stats?.hits_per_minute) return []
    // hits_per_minute is keyed "1".."60" where 60 = oldest, 1 = newest.
    // Sort ascending and emit values oldest→newest (left→right) so the
    // Sparkline's last-dot indicator marks the current minute.
    return Object.entries(stats.hits_per_minute)
      .map(([k, v]) => ({ minute: parseInt(k, 10) || 0, hits: v }))
      .sort((a, b) => a.minute - b.minute)
      .map((d) => d.hits)
  }, [stats])

  const topEndpointList = useMemo(() => {
    if (!stats?.hits_by_endpoint) return []
    return Object.entries(stats.hits_by_endpoint)
      .map(([endpoint, count]) => ({ endpoint, count }))
      .sort((a, b) => b.count - a.count)
  }, [stats])

  const topClientList = useMemo(() => {
    if (!stats?.hits_by_client) return []
    return Object.entries(stats.hits_by_client)
      .map(([ip, count]) => ({ ip, count }))
      .sort((a, b) => b.count - a.count)
  }, [stats])

  const maxEndpointCount = topEndpointList[0]?.count ?? 0
  const maxClientCount = topClientList[0]?.count ?? 0

  const topEndpointEntry = topEndpointList[0]
  const topClientEntry = topClientList[0]

  // ── Render: loading skeleton ─────────────────────────────────────────
  if (loading && !stats) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f2335]">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-amber-400" />
            <span className="text-sm font-bold text-[#dde1ed]">Rate Limits</span>
          </div>
          <span className="spinner" aria-hidden="true" />
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="skeleton-card p-2.5">
              <div className="skeleton-line" />
              <div className="skeleton-line-lg" />
              <div className="skeleton-line" />
            </div>
          ))}
        </div>
        <div className="space-y-2">
          {[0, 1, 2].map((i) => (
            <div key={i} className="skeleton-card p-2.5">
              <div className="skeleton-line-lg" />
            </div>
          ))}
        </div>
      </div>
    )
  }

  // ── Render: hard error (no data yet) ─────────────────────────────────
  if (error && !stats) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-[#7e8aaa] gap-2 p-6">
        <AlertCircle className="w-5 h-5 text-red-400" />
        <div className="text-red-400 font-semibold">Rate-limit stats endpoint unavailable</div>
        <div className="mono text-[10px] text-[#3e4560] max-w-md text-center break-all">
          {error}
        </div>
        <button
          onClick={() => fetchStats()}
          className="btn btn-ghost btn-sm mt-2 text-xs"
        >
          <RefreshCw className="w-3.5 h-3.5" />
          Retry
        </button>
      </div>
    )
  }

  // ── Render: empty state (no hits yet) ────────────────────────────────
  const isEmpty =
    stats != null &&
    stats.total_hits === 0 &&
    Object.keys(stats.hits_by_endpoint ?? {}).length === 0

  if (isEmpty) {
    return (
      <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4">
        <div className="flex items-center justify-between pb-2 border-b border-[#1f2335]">
          <div>
            <div className="flex items-center gap-2">
              <Activity className="w-4 h-4 text-amber-400" />
              <span className="text-sm font-bold text-[#dde1ed]">Rate Limits</span>
              <span className="badge badge-dim text-[9.5px]">30s poll</span>
            </div>
            <p className="text-xs text-[#7e8aaa] mt-0.5">
              Per-route throttle analytics · last 1h window
            </p>
          </div>
        </div>
        <div className="flex-1 flex flex-col items-center justify-center text-xs text-[#7e8aaa] gap-2 py-12">
          <Inbox className="w-8 h-8 opacity-30" />
          <div className="text-sm font-semibold text-[#dde1ed]">No rate-limit hits in the last hour</div>
          <div className="text-[11px] text-center max-w-sm">
            The dashboard will surface 429 hits here as soon as a client
            exceeds a route's per-minute allowance. Routes are limited
            by the policy shown below.
          </div>
          <div className="mt-3 flex flex-wrap gap-2 justify-center max-w-md">
            {[
              { k: 'Read', v: '120/min' },
              { k: 'Write', v: '30/min' },
              { k: 'Heavy', v: '5/min' },
              { k: 'Trade', v: '20/min' },
              { k: 'Arb', v: '10/min' },
              { k: 'Live', v: '3/min' },
            ].map((p) => (
              <span
                key={p.k}
                className="badge badge-dim mono text-[10px]"
                title={`${p.k} limit: ${p.v}`}
              >
                {p.k}: {p.v}
              </span>
            ))}
          </div>
          <button
            onClick={() => fetchStats()}
            className="btn btn-ghost btn-sm mt-3 text-xs"
          >
            <RefreshCw className="w-3.5 h-3.5" />
            Check again
          </button>
        </div>
      </div>
    )
  }

  // ── Render: main panel ───────────────────────────────────────────────
  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden">
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="flex flex-wrap justify-between items-center gap-2 p-4 pb-2 border-b border-[#1f2335]">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Activity className="w-4 h-4 text-amber-400 flex-shrink-0" />
            <h2 className="text-sm font-bold text-[#dde1ed]">Rate Limits</h2>
            <span className="badge badge-dim text-[9.5px]">30s poll</span>
            {refreshing && (
              <span className="badge badge-amber text-[9.5px]">
                <RefreshCw className="w-2.5 h-2.5 animate-spin" />
                syncing
              </span>
            )}
            {error && stats && (
              <span
                className="badge badge-red text-[9.5px]"
                title={error}
              >
                stale
              </span>
            )}
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5 truncate">
            Per-route throttle analytics · last 1h window ·{' '}
            updated {formatRelativeTime(lastUpdated)}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <button
            onClick={() => fetchStats()}
            disabled={refreshing}
            className="btn btn-ghost btn-sm flex items-center gap-1.5 text-xs"
            aria-label="Refresh rate-limit stats"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${refreshing ? 'animate-spin' : ''}`} />
            <span>Refresh</span>
          </button>
        </div>
      </header>

      {/* ── Body (scrollable) ─────────────────────────────────────────── */}
      <div
        className="flex-1 overflow-y-auto p-4 space-y-3 scrollbar-thin"
        style={{ maxHeight: '100%' }}
      >
        {/* ── KPI cards ──────────────────────────────────────────────── */}
        <section
          aria-label="Rate limit summary KPIs"
          className="grid grid-cols-2 md:grid-cols-4 gap-2.5"
        >
          <KpiCard
            label="Total Hits (1h)"
            value={formatNumber(stats?.total_hits ?? 0)}
            hint="rate-limited requests"
            icon={Zap}
            accentClass={
              (stats?.total_hits ?? 0) > 0
                ? 'text-amber-400'
                : 'text-[#dde1ed]'
            }
          />
          <KpiCard
            label="Hit Rate"
            value={formatRate(stats?.hits_per_minute_rate ?? 0)}
            hint="hits / minute"
            icon={TrendingUp}
            accentClass={
              (stats?.hits_per_minute_rate ?? 0) > 1
                ? 'text-red-400'
                : 'text-[#dde1ed]'
            }
          />
          <KpiCard
            label="Top Endpoint"
            value={topEndpointEntry ? shortEndpoint(topEndpointEntry.endpoint, 18) : '—'}
            hint={topEndpointEntry ? `${formatNumber(topEndpointEntry.count)} hits` : undefined}
            icon={Server}
            accentClass="text-blue-400"
          />
          <KpiCard
            label="Top Client"
            value={topClientEntry ? shortIp(topClientEntry.ip) : '—'}
            hint={topClientEntry ? `${formatNumber(topClientEntry.count)} hits` : undefined}
            icon={Globe}
            accentClass="text-emerald-400"
          />
        </section>

        {/* ── Charts row ─────────────────────────────────────────────── */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {/* Hits by endpoint — PnLBarChart */}
          <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
                Hits by Endpoint
              </h3>
              <span className="text-[9.5px] text-[#7e8aaa] mono">
                {endpointBarData.length} endpoints
              </span>
            </div>
            <div className="h-[220px]">
              {endpointBarData.length === 0 ? (
                <div className="h-full flex items-center justify-center text-[11px] text-[#7e8aaa]">
                  No hits recorded
                </div>
              ) : (
                <PnLBarChart
                  data={endpointBarData}
                  height={220}
                  layout="vertical"
                  showZeroLine={false}
                  successColor={chartTheme.colors.warning}
                  dangerColor={chartTheme.colors.danger}
                  formatValue={(v) => formatNumber(v)}
                  formatTooltip={(d) => (
                    <div style={{ fontSize: 11 }}>
                      <div style={{ fontWeight: 600, marginBottom: 2 }}>
                        {d.name}
                      </div>
                      <div>{formatNumber(d.value)} hits</div>
                    </div>
                  )}
                />
              )}
            </div>
          </div>

          {/* Hits per minute — Sparkline */}
          <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
                Hits per Minute (60m)
              </h3>
              <span className="text-[9.5px] text-[#7e8aaa] mono">
                rate: {formatRate(stats?.hits_per_minute_rate ?? 0)}
              </span>
            </div>
            <div className="h-[220px] flex items-center justify-center">
              {perMinuteSeries.length < 2 ? (
                <div className="text-[11px] text-[#7e8aaa]">
                  Not enough samples for a trend yet
                </div>
              ) : (
                <Sparkline
                  data={perMinuteSeries}
                  color={chartTheme.colors.warning}
                  width="100%"
                  height={220}
                  strokeWidth={1.4}
                  showLastDot
                />
              )}
            </div>
          </div>
        </section>

        {/* ── Tables row ─────────────────────────────────────────────── */}
        <section className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {/* Top endpoints table */}
          <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
                Top Rate-Limited Endpoints
              </h3>
              <span className="text-[9.5px] text-[#7e8aaa] mono">
                {topEndpointList.length} shown
              </span>
            </div>
            <div className="flex items-center gap-2 pb-1 border-b border-[#1f2335]">
              <div className="flex-1 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                Endpoint
              </div>
              <div className="w-24 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider text-center">
                Share
              </div>
              <div className="w-12 text-right text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                Hits
              </div>
            </div>
            <div className="max-h-72 overflow-y-auto scrollbar-thin">
              {topEndpointList.length === 0 ? (
                <div className="py-6 text-center text-[11px] text-[#7e8aaa]">
                  No endpoints throttled yet
                </div>
              ) : (
                topEndpointList.map(({ endpoint, count }) => (
                  <EndpointRow
                    key={`${endpoint}-${count}`}
                    endpoint={endpoint}
                    count={count}
                    max={maxEndpointCount}
                  />
                ))
              )}
            </div>
          </div>

          {/* Top clients table */}
          <div className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
                Top Rate-Limited Clients
              </h3>
              <span className="text-[9.5px] text-[#7e8aaa] mono">
                {topClientList.length} shown
              </span>
            </div>
            <div className="flex items-center gap-2 pb-1 border-b border-[#1f2335]">
              <div className="flex-1 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                Client IP
              </div>
              <div className="w-24 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider text-center">
                Share
              </div>
              <div className="w-12 text-right text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                Hits
              </div>
            </div>
            <div className="max-h-72 overflow-y-auto scrollbar-thin">
              {topClientList.length === 0 ? (
                <div className="py-6 text-center text-[11px] text-[#7e8aaa]">
                  No clients throttled yet
                </div>
              ) : (
                topClientList.map(({ ip, count }) => (
                  <ClientRow
                    key={`${ip}-${count}`}
                    ip={ip}
                    count={count}
                    max={maxClientCount}
                  />
                ))
              )}
            </div>
          </div>
        </section>

        {/* ── Top requested endpoints (all-requests view) ───────────── */}
        <section className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
              Most-Requested Endpoints
            </h3>
            <span className="text-[9.5px] text-[#7e8aaa] mono">
              all-requests view
            </span>
          </div>
          <div className="flex items-center gap-2 pb-1 border-b border-[#1f2335]">
            <div className="flex-1 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
              Endpoint
            </div>
            <div className="w-24 text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider text-center">
              Share
            </div>
            <div className="w-12 text-right text-[9.5px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
              Hits
            </div>
          </div>
          <div className="max-h-60 overflow-y-auto scrollbar-thin">
            {(() => {
              const topReqList = stats?.top_endpoints
                ? Object.entries(stats.top_endpoints)
                    .map(([endpoint, count]) => ({ endpoint, count }))
                    .sort((a, b) => b.count - a.count)
                : []
              if (topReqList.length === 0) {
                return (
                  <div className="py-6 text-center text-[11px] text-[#7e8aaa]">
                    No requests recorded yet
                  </div>
                )
              }
              const maxReqCount = topReqList[0]?.count ?? 0
              return topReqList.map(({ endpoint, count }) => (
                <EndpointRow
                  key={`req-${endpoint}-${count}`}
                  endpoint={endpoint}
                  count={count}
                  max={maxReqCount}
                />
              ))
            })()}
          </div>
        </section>

        {/* ── Policy reference ───────────────────────────────────────── */}
        <section className="bg-[#0e1015] border border-[#1f2335] rounded-md p-3 flex flex-col gap-2">
          <h3 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
            Rate-Limit Policy
          </h3>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {[
              { k: 'Read routes', v: '120/min', desc: 'Generous — allows polling' },
              { k: 'Write routes', v: '30/min', desc: 'POST/PUT/DELETE' },
              { k: 'Heavy routes', v: '5/min', desc: 'ML retrain, backtest' },
              { k: 'Trade routes', v: '20/min', desc: 'Orders, position-close' },
              { k: 'Arbitrage', v: '10/min', desc: 'Auth + heavy' },
              { k: 'Live enable', v: '3/min', desc: 'One-shot escalation' },
            ].map((p) => (
              <div
                key={p.k}
                className="bg-[#13161e] border border-[#1f2335] rounded-md p-2 flex flex-col gap-0.5"
              >
                <div className="text-[10px] text-[#7e8aaa] font-semibold uppercase tracking-wider">
                  {p.k}
                </div>
                <div className="text-sm font-bold text-amber-400 mono">{p.v}</div>
                <div className="text-[9.5px] text-[#7e8aaa]">{p.desc}</div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  )
}
