// components/SystemHealthView.tsx — Pipeline Health & Subsystem Telemetry
'use client'

import { useEffect, useState } from 'react'
import { AlertTriangle, RefreshCw, X } from 'lucide-react'
import { getApiUrl, apiFetch } from '@/lib/api'

interface HealthData {
  status: string
  timestamp: number
  poller: {
    tier1_tokens: number
    tier2_tokens: number
    total_tracked: number
    success_rate: number
    latency_ms: number
  }
  ml_engine: {
    active_version: string
    brier_score: number
    psi_drift: number
    drift_status: string
  }
  market_db?: {
    db_backend: string
    db_path: string
    size_mb: number
    snapshots_recorded: number
    ticks_recorded: number
    news_items_recorded: number
    ml_feature_vectors: number
  }
  storage: {
    vector_index_size: number
    audit_trail_backend: string
    market_intelligence_db?: string
    state_persistence: string
  }
  services: Array<{ name: string; status: string; port?: number; frequency?: string }>
}

export default function SystemHealthView() {
  const [health, setHealth] = useState<HealthData | null>(null)
  const [loading, setLoading] = useState(true)
  // W22-1 — surface fetch failures instead of silently swallowing.
  const [error, setError] = useState<string | null>(null)

  const fetchHealth = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/system/health`)
      if (res.ok) {
        setHealth(await res.json())
        setError(null)
      } else {
        setError(`System health endpoint unavailable (HTTP ${res.status})`)
      }
    } catch (e) {
      console.error('[SystemHealthView] Failed to fetch system health:', e)
      setError(e instanceof Error ? e.message : 'Network error loading system health')
    }
    setLoading(false)
  }

  useEffect(() => {
    fetchHealth()
    const timer = setInterval(fetchHealth, 3000)
    return () => clearInterval(timer)
  }, [])

  if (loading && !health) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-xs text-[#7e8aaa]">
        <span className="spinner mb-2" aria-hidden="true" />
        Gathering pipeline health &amp; supervisor telemetry…
      </div>
    )
  }

  if (!health) {
    return (
      <div
        className="flex flex-col items-center justify-center h-full text-xs text-[#7e8aaa] gap-3 px-4"
        role="alert"
      >
        {/* W22-1 — previously this state silently swallowed the underlying
            error. Now we surface it inline with a retry button so the
            trader can see *why* health telemetry is missing. */}
        <div className="flex items-center gap-2 text-amber-400">
          <AlertTriangle className="w-5 h-5" aria-hidden="true" />
          <span className="font-semibold">System health telemetry endpoint unavailable.</span>
        </div>
        {error && (
          <div className="banner-danger text-xs px-3 py-2 rounded max-w-md flex items-center justify-between gap-3" role="alert">
            <span className="flex items-center gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
              <span>{error}</span>
            </span>
            <div className="flex items-center gap-2 shrink-0">
              <button
                onClick={() => { setError(null); setLoading(true); fetchHealth() }}
                className="hover:underline text-xs flex items-center gap-1 shrink-0"
                aria-label="Retry health fetch"
              >
                <RefreshCw className="w-3 h-3" aria-hidden="true" /> Retry
              </button>
              <button
                onClick={() => setError(null)}
                className="hover:underline text-xs flex items-center gap-0.5 shrink-0"
                aria-label="Dismiss error"
              >
                <X className="w-3 h-3" aria-hidden="true" /> Dismiss
              </button>
            </div>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">🩺</span>
            <span className="text-sm font-bold text-[#dde1ed]">
              Platform Subsystem Health &amp; Process Telemetry
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            Order Book Poller, Supervisor Watchdog, TimescaleDB Storage &amp; Risk Sizing ($100 Operating / $200 Ceiling)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-amber text-[9.5px]">$100 Operating Capital</span>
          <span className="badge badge-green text-[9.5px]">Process Supervisor Active</span>
        </div>
      </div>

      {/* W22-1 — transient fetch errors during polling. Shown inline so
          the trader knows the latest poll failed even though stale data
          is still rendered. Dismissable. */}
      {error && (
        <div className="banner-danger text-xs py-2 px-3 flex items-center justify-between" role="alert">
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>{error}</span>
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => { setError(null); fetchHealth() }}
              className="hover:underline text-xs flex items-center gap-1"
              aria-label="Retry health fetch"
            >
              <RefreshCw className="w-3 h-3" aria-hidden="true" /> Retry
            </button>
            <button
              onClick={() => setError(null)}
              className="hover:underline text-xs flex items-center gap-0.5"
              aria-label="Dismiss error"
            >
              <X className="w-3 h-3" aria-hidden="true" /> Dismiss
            </button>
          </div>
        </div>
      )}

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        <div className="kpi-card">
          <span className="kpi-label">Poller Success Rate</span>
          <span className="kpi-value text-green-400">
            {health.poller.success_rate}%
          </span>
          <span className="kpi-sub">
            {health.poller.total_tracked} books · {health.poller.latency_ms}ms avg
          </span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Market DB Size</span>
          <span className="kpi-value text-cyan-400">
            {health.market_db ? `${health.market_db.size_mb} MB` : 'Buffered'}
          </span>
          <span className="kpi-sub">
            {health.market_db
              ? `${health.market_db.snapshots_recorded.toLocaleString()} snaps · ${health.market_db.ticks_recorded.toLocaleString()} ticks`
              : 'In-memory state'}
          </span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Model Drift PSI</span>
          <span className="kpi-value text-blue-400">
            {health.ml_engine.psi_drift.toFixed(4)}
          </span>
          <span className="kpi-sub text-green-400">
            {health.ml_engine.drift_status}
          </span>
        </div>

        <div className="kpi-card">
          <span className="kpi-label">Feature Store Vectors</span>
          <span className="kpi-value text-amber-400">
            {health.market_db ? health.market_db.ml_feature_vectors.toLocaleString() : 0}
          </span>
          <span className="kpi-sub">38-dimensional shape</span>
        </div>
      </div>

      {/* Supervised Microservices Grid */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335]">
        <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#dde1ed]">
            ⚙️ Supervised Processes &amp; Loops
          </span>
          <span className="badge badge-dim text-[9.5px]">FastAPI Async Tasks</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-2.5">
          {health.services.map((s, i) => (
            <div key={i} className="flex justify-between items-center bg-[#13161e] p-2.5 rounded border border-[#1f2335] text-xs">
              <div>
                <span className="font-semibold text-[#dde1ed] block">{s.name}</span>
                {s.frequency && (
                  <span className="text-[10px] text-[#7e8aaa] mono">{s.frequency}</span>
                )}
                {s.port && (
                  <span className="text-[10px] text-cyan-400 mono">Port: {s.port}</span>
                )}
              </div>
              <span className={`badge text-[9.5px] font-bold ${
                s.status === 'HEALTHY' || s.status === 'UP' || s.status === 'RUNNING'
                  ? 'badge-green'
                  : 'badge-amber'
              }`}>
                ● {s.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
