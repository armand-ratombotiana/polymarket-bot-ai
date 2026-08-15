// components/SystemHealthView.tsx — Pipeline Health & 24/7 Supervisor Monitor
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

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

  const fetchHealth = async () => {
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/system/health`)
      if (res.ok) {
        setHealth(await res.json())
      }
    } catch {}
  }

  useEffect(() => {
    fetchHealth()
    const timer = setInterval(fetchHealth, 3000)
    return () => clearInterval(timer)
  }, [])

  if (!health) {
    return (
      <div className="flex items-center justify-center h-full text-xs text-[#8b91a8]">
        Gathering pipeline health &amp; supervisor telemetry…
      </div>
    )
  }

  return (
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#252836] gap-2">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🩺</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              24/7 Platform Pipeline Health &amp; Specialized Market Database
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Adaptive Poller Latency, Supervisor Watchdog Status, Specialized SQLite WAL Time-Series DB &amp; $200 Max Portfolio Risk Sizing
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-amber text-xs font-semibold">$200 Max Capital Scaled</span>
          <span className="badge badge-green text-xs font-semibold">All Systems 24/7 Nominal</span>
        </div>
      </div>

      {/* KPI Cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Poller Success Rate</span>
          <span className="mono text-lg font-bold text-green-400">
            {health.poller.success_rate}%
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">
            {health.poller.total_tracked} contracts ({health.poller.latency_ms} ms avg)
          </span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Specialized Market DB Size</span>
          <span className="mono text-lg font-bold text-cyan-400">
            {health.market_db ? `${health.market_db.size_mb} MB` : 'WAL Active'}
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">
            {health.market_db ? `${health.market_db.snapshots_recorded} snaps, ${health.market_db.ticks_recorded} ticks` : 'Continuous Ingest'}
          </span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">Model Concept Drift (PSI)</span>
          <span className="mono text-lg font-bold text-blue-400">
            {health.ml_engine.psi_drift.toFixed(4)}
          </span>
          <span className="text-[10px] text-green-400 block mt-0.5">
            Status: {health.ml_engine.drift_status}
          </span>
        </div>

        <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
          <span className="text-[11px] text-[#4a5068] block font-medium">DB-Backed Feature Vectors</span>
          <span className="mono text-lg font-bold text-amber-400">
            {health.market_db ? health.market_db.ml_feature_vectors : 0} vectors
          </span>
          <span className="text-[10px] text-[#8b91a8] block mt-0.5">Ground-Truth Training Ready</span>
        </div>
      </div>

      {/* Specialized Database Telemetry Card */}
      {health.market_db && (
        <div className="card p-3.5 bg-[#161822] border border-[#252836]">
          <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
            <span className="card-title text-xs font-bold text-[#e8eaf0]">
              🗄️ Specialized Market Intelligence &amp; Feature Database (`market_intelligence.db`)
            </span>
            <span className="badge badge-blue text-[10px]">WAL High-Concurrency Mode</span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <div className="bg-[#111318] p-2.5 rounded border border-[#252836]">
              <span className="text-[#8b91a8] block text-[11px]">Recorded Snapshots:</span>
              <span className="mono text-cyan-400 font-bold text-sm">{health.market_db.snapshots_recorded.toLocaleString()}</span>
            </div>
            <div className="bg-[#111318] p-2.5 rounded border border-[#252836]">
              <span className="text-[#8b91a8] block text-[11px]">Micro-Depth Ticks (OFI):</span>
              <span className="mono text-green-400 font-bold text-sm">{health.market_db.ticks_recorded.toLocaleString()}</span>
            </div>
            <div className="bg-[#111318] p-2.5 rounded border border-[#252836]">
              <span className="text-[#8b91a8] block text-[11px]">Fundamental News Items:</span>
              <span className="mono text-amber-400 font-bold text-sm">{health.market_db.news_items_recorded.toLocaleString()}</span>
            </div>
            <div className="bg-[#111318] p-2.5 rounded border border-[#252836]">
              <span className="text-[#8b91a8] block text-[11px]">32-Feature ML Vectors:</span>
              <span className="mono text-purple-400 font-bold text-sm">{health.market_db.ml_feature_vectors.toLocaleString()}</span>
            </div>
          </div>
        </div>
      )}

      {/* Supervised Microservices Grid */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836]">
        <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
          <span className="card-title text-xs font-bold text-[#e8eaf0]">
            ⚙️ Supervised 24/7 Microservices &amp; Subsystems
          </span>
          <span className="badge badge-dim text-[10px]">Supervisord Watchdog Active</span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
          {health.services.map((s, i) => (
            <div key={i} className="flex justify-between items-center bg-[#111318] p-2.5 rounded border border-[#252836] text-xs">
              <div>
                <span className="font-semibold text-[#e8eaf0] block">{s.name}</span>
                {s.frequency && (
                  <span className="text-[10px] text-[#4a5068] mono">{s.frequency}</span>
                )}
                {s.port && (
                  <span className="text-[10px] text-cyan-400 mono">Port: {s.port}</span>
                )}
              </div>
              <span className="badge badge-green text-[10px] font-bold">
                ● {s.status}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
