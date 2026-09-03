// components/RiskStatusPanel.tsx — Institutional Risk & Capital Governance Strip
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtPnl, fmtPct } from '@/lib/design-tokens'

interface Reconciliation {
  reconciled: boolean
  status: string
  findings: string[]
  exposure: {
    capital_invested: number
    reserved_for_pending_orders: number
    net_directional_exposure: number
    maximum_remaining_loss: number
    exposure_dollar_days: number
    exposure_per_group: Record<string, number>
    exposure_per_strategy: Record<string, number>
    available_cash: number
  }
}

interface RiskStatus {
  mode: string
  observation_only: boolean
  observation_reason: string
  exposure_reconciled: boolean
  bankroll_ceiling: number
  deployable_ceiling: number
  total_exposure: number
  max_total_exposure: number
  max_position_per_market: number
  dynamic_risk_multiplier?: number
  effective_max_position_per_market?: number
  daily_pnl: number
  daily_loss_limit: number
  weekly_pnl?: number
  weekly_loss_limit?: number
  drawdown_dollars?: number
  max_drawdown_limit?: number
  max_loss_if_all_zero: number
  kill_switch?: boolean
  paper_balance?: number | null
  open_orders?: number
}

interface KpiProps {
  label: string
  value: string | null
  sub?: string
  valueColor?: string
  tooltip?: string
  warn?: boolean
  danger?: boolean
}

function Kpi({ label, value, sub, valueColor, tooltip, warn, danger }: KpiProps) {
  const vc = danger
    ? 'var(--color-red-fg)'
    : warn
    ? 'var(--color-amber-fg)'
    : valueColor ?? 'var(--text-primary)'
  return (
    <div
      className="kpi-card bg-[#0e1015] border border-[#1f2335] p-2.5 rounded-lg flex flex-col justify-between"
      title={tooltip}
      data-tooltip={tooltip}
    >
      <span className="text-[10px] text-[#7e8aaa] font-semibold uppercase tracking-wider block mb-1">
        {label}
      </span>
      <span className="mono font-bold text-sm" style={{ color: vc }}>
        {value ?? <span className="text-[#3e4560]">—</span>}
      </span>
      {sub && <span className="text-[9.5px] text-[#7e8aaa] mt-0.5 block">{sub}</span>}
    </div>
  )
}

function CapitalAllocationBar({ invested, reserved, maxDeployable }: { invested: number; reserved: number; maxDeployable: number }) {
  const investedPct = maxDeployable > 0 ? (invested / maxDeployable) * 100 : 0
  const reservedPct = maxDeployable > 0 ? (reserved / maxDeployable) * 100 : 0
  const availablePct = Math.max(0, 100 - investedPct - reservedPct)

  return (
    <div className="px-3 py-2 bg-[#0e1015] border border-[#1f2335] rounded-lg mx-3 mb-2">
      <div className="flex justify-between items-center text-[10.5px] text-[#7e8aaa] mb-1.5">
        <span className="font-semibold text-[#dde1ed] flex items-center gap-1.5">
          <span>📊 Capital Allocation</span>
          <span className="text-[9.5px] font-normal text-[#7e8aaa]">(${invested.toFixed(2)} deployed / $60 ceiling)</span>
        </span>
        <span className="mono text-cyan-300 font-bold">{investedPct.toFixed(1)}% Deployed</span>
      </div>

      <div className="w-full bg-[#13161e] border border-[#1f2335] h-2 rounded-full overflow-hidden flex">
        <div
          className="h-full bg-cyan-400 transition-all duration-300"
          style={{ width: `${Math.min(investedPct, 100)}%` }}
          title={`Invested: $${invested.toFixed(2)}`}
        />
        <div
          className="h-full bg-amber-400 transition-all duration-300"
          style={{ width: `${Math.min(reservedPct, 100)}%` }}
          title={`Reserved: $${reserved.toFixed(2)}`}
        />
      </div>

      <div className="flex justify-between text-[9px] text-[#7e8aaa] mt-1 mono">
        <span className="flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-cyan-400 inline-block" /> Active: ${invested.toFixed(2)}
        </span>
        <span className="flex items-center gap-1">
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 inline-block" /> Pending: ${reserved.toFixed(2)}
        </span>
        <span className="flex items-center gap-1 text-green-400">
          <span className="w-1.5 h-1.5 rounded-full bg-green-400 inline-block" /> Avail: ${(maxDeployable - invested - reserved).toFixed(2)}
        </span>
      </div>
    </div>
  )
}

export default function RiskStatusPanel() {
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [recon, setRecon] = useState<Reconciliation | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState(false)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)

  useEffect(() => {
    const fetchAll = async () => {
      try {
        const apiUrl = getApiUrl()
        const [statusRes, reconRes] = await Promise.all([
          apiFetch(`${apiUrl}/api/status`),
          apiFetch(`${apiUrl}/api/risk/reconcile`),
        ])
        if (statusRes.ok) setRisk(await statusRes.json())
        if (reconRes.ok) setRecon(await reconRes.json())
        setError(false)
        setLastUpdated(Date.now())
      } catch {
        setError(true)
      } finally {
        setLoading(false)
      }
    }
    fetchAll()
    const t = setInterval(fetchAll, 3000)
    return () => clearInterval(t)
  }, [])

  if (loading) {
    return (
      <div className="card p-3 flex items-center gap-2 bg-[#13161e] border border-[#1f2335]">
        <span className="spinner" aria-hidden="true" />
        <span className="text-xs text-[#7e8aaa]">Loading institutional risk telemetry…</span>
      </div>
    )
  }

  if (error || !risk) {
    return (
      <div className="card bg-[#13161e] border border-[#1f2335]">
        <div className="card-header p-3 border-b border-[#1f2335] flex justify-between">
          <span className="card-title text-xs font-bold text-[#dde1ed]">🛡 Risk &amp; Exposure</span>
          <span className="badge badge-red">Unavailable</span>
        </div>
        <div className="p-4 text-center text-xs text-[#7e8aaa]">
          Risk engine offline or starting up.
        </div>
      </div>
    )
  }

  const observing = risk.observation_only || !risk.exposure_reconciled
  const reconOk = recon?.reconciled ?? false
  const groups = recon?.exposure.exposure_per_group ?? {}
  const topGroups = Object.entries(groups).sort((a, b) => b[1] - a[1]).slice(0, 2)

  const expPct = risk.max_total_exposure > 0 ? risk.total_exposure / risk.max_total_exposure : null
  const modeLabel = risk.mode === 'live' ? 'LIVE' : risk.mode === 'shadow' ? 'SHADOW' : 'PAPER'
  const modeBadgeClass = risk.mode === 'live' ? 'badge-red' : risk.mode === 'shadow' ? 'badge-cyan' : 'badge-amber'

  const dynamicMult = risk.dynamic_risk_multiplier ?? 1.0
  const effectiveCap = risk.effective_max_position_per_market ?? 3.0

  return (
    <div className="card h-full flex flex-col bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden shadow-xl">
      {/* Header */}
      <div className="card-header p-3 border-b border-[#1f2335] flex flex-wrap justify-between items-center gap-2">
        <div className="flex items-center gap-2">
          <span className="card-title text-xs font-bold text-[#dde1ed] tracking-wide">
            🛡 INSTITUTIONAL RISK &amp; RECONCILIATION
          </span>
          <span className={`badge ${modeBadgeClass} text-[9.5px]`}>{modeLabel}</span>
          <span
            className={`badge ${reconOk ? 'badge-green' : 'badge-red'} text-[9.5px]`}
            title={reconOk ? 'Exposure reconciled with ledger' : 'Ledger reconciliation discrepancy detected'}
          >
            {reconOk ? '✓ Reconciled' : '⚠ Discrepancy'}
          </span>
          {risk.kill_switch && <span className="badge badge-red animate-pulse text-[9.5px]">🛑 Circuit Breaker Active</span>}
        </div>

        {/* Dynamic Model Health Multiplier Badge */}
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-[#7e8aaa]">ML Risk Scale:</span>
          <span
            className={`px-2 py-0.5 rounded text-[10px] font-bold font-mono ${
              dynamicMult >= 1.0
                ? 'bg-green-500/15 text-green-400 border border-green-500/30'
                : dynamicMult >= 0.6
                ? 'bg-amber-500/15 text-amber-400 border border-amber-500/30'
                : 'bg-red-500/15 text-red-400 border border-red-500/30'
            }`}
          >
            {(dynamicMult * 100).toFixed(0)}% (${effectiveCap.toFixed(2)} Cap)
          </span>
        </div>
      </div>

      {/* Observation Warning */}
      {observing && (
        <div className="bg-amber-500/10 border-l-4 border-amber-500 text-amber-300 text-xs px-3 py-2 mx-3 mt-2 rounded">
          <strong>Observation Mode Active:</strong> New live orders disabled until portfolio exposure is reconciled.
          {risk.observation_reason ? ` (${risk.observation_reason})` : ''}
        </div>
      )}

      {/* Capital Allocation Visual Meter */}
      <div className="mt-2.5">
        <CapitalAllocationBar
          invested={recon?.exposure.capital_invested || 0}
          reserved={recon?.exposure.reserved_for_pending_orders || 0}
          maxDeployable={60.0}
        />
      </div>

      {/* Primary KPI Grid — Capital & Position Limits */}
      <div className="px-3 pb-2">
        <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
          <Kpi
            label="Operating Bankroll"
            value={fmtUsd(100)}
            sub="Active Sizing Base"
            tooltip="USD 100.00 operating capital — automated sizing baseline"
          />
          <Kpi
            label="Deployable Ceiling"
            value={fmtUsd(risk.deployable_ceiling || 60)}
            sub="$40 Cash Reserve"
            tooltip="Maximum capital deployable without breaching the $40 minimum cash reserve"
          />
          <Kpi
            label="Max Per Market"
            value={`$${effectiveCap.toFixed(2)}`}
            sub={`Scaled: ${(dynamicMult * 100).toFixed(0)}%`}
            valueColor="var(--color-cyan-fg)"
            tooltip="Dynamically scaled maximum dollar commitment per individual market based on ML calibration health"
          />
          <Kpi
            label="Total Exposure"
            value={fmtUsd(risk.total_exposure)}
            sub={expPct != null ? `${(expPct * 100).toFixed(1)}% of $25 limit` : undefined}
            warn={expPct != null && expPct > 0.7}
            danger={expPct != null && expPct > 0.9}
            tooltip="Total open risk across all positions + active open orders ($25.00 hard cap)"
          />
          <Kpi
            label="Daily Loss Stop"
            value={`-$2.00`}
            sub={`PnL: ${fmtPnl(risk.daily_pnl)}`}
            valueColor={risk.daily_pnl >= 0 ? 'var(--color-green-fg)' : 'var(--color-red-fg)'}
            tooltip="Circuit breaker activates and cancels all open orders if daily realized losses reach -$2.00"
          />
          <Kpi
            label="Max Drawdown Stop"
            value={`-$8.00`}
            sub="High-Water Mark"
            tooltip="Hard stop: halts all execution if portfolio draws down $8.00 from peak equity"
          />
        </div>
      </div>

      {/* Correlated Groups Strip */}
      {topGroups.length > 0 && (
        <div className="px-3 pb-3 pt-2 border-t border-[#1f2335] mt-auto">
          <div className="text-[10px] font-bold uppercase tracking-wider text-[#7e8aaa] mb-1.5 flex justify-between">
            <span>Largest Correlated Market Exposure</span>
            <span>Limit: $8.00 Max</span>
          </div>
          <div className="space-y-1">
            {topGroups.map(([name, val]) => (
              <div key={name} className="flex justify-between items-center text-xs bg-[#0e1015] px-2.5 py-1 rounded border border-[#1f2335]">
                <span className="text-[#dde1ed] truncate max-w-[200px] text-[11px]">{name}</span>
                <span className="mono text-amber-400 font-bold">{fmtUsd(val)}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}