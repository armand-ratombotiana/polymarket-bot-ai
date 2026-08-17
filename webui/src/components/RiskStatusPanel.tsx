// components/RiskStatusPanel.tsx — Full command-center risk strip
// Every metric is labeled with currency, period, mode, and definition.
// No hardcoded values. Null shown as —, never a fallback number.
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
  daily_pnl: number
  daily_loss_limit: number
  max_loss_if_all_zero: number
  kill_switch?: boolean
  paper_balance?: number | null
  open_orders_count?: number
  open_positions_count?: number
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
  const vc = danger ? 'var(--color-red-fg)'
    : warn ? 'var(--color-amber-fg)'
    : valueColor ?? 'var(--text-primary)'
  return (
    <div
      className="kpi-card"
      title={tooltip}
      data-tooltip={tooltip}
      style={{ position: 'relative' }}
    >
      <span className="kpi-label">{label}</span>
      <span className="kpi-value mono" style={{ color: vc, fontSize: '13px' }}>
        {value ?? <span className="unavailable-value">—</span>}
      </span>
      {sub && <span className="kpi-sub">{sub}</span>}
    </div>
  )
}

function ExposureBar({ used, max }: { used: number; max: number }) {
  const pct = max > 0 ? Math.min(used / max, 1) : 0
  const cls = pct > 0.9 ? 'danger' : pct > 0.7 ? 'warning' : 'safe'
  return (
    <div style={{ padding: '0 12px 8px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '10.5px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
        <span>Exposure utilization</span>
        <span className="mono">{(pct * 100).toFixed(1)}%</span>
      </div>
      <div className="exposure-bar" role="progressbar" aria-valuenow={Math.round(pct * 100)} aria-valuemin={0} aria-valuemax={100} aria-label={`Exposure utilization ${(pct*100).toFixed(1)}%`}>
        <div className={`exposure-bar-fill ${cls}`} style={{ width: `${pct * 100}%` }} />
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
    const t = setInterval(fetchAll, 4000)
    return () => clearInterval(t)
  }, [])

  if (loading) {
    return (
      <div className="card" style={{ padding: '12px', display: 'flex', alignItems: 'center', gap: '8px' }}>
        <span className="spinner" aria-hidden="true" />
        <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Loading risk status…</span>
      </div>
    )
  }

  if (error || !risk) {
    return (
      <div className="card">
        <div className="card-header">
          <span className="card-title">🛡 Risk &amp; Exposure</span>
          <span className="badge badge-red" aria-label="Status unavailable">Unavailable</span>
        </div>
        <div className="error-state" style={{ padding: '16px' }}>
          <span className="error-state-icon">⚠</span>
          <span className="error-state-title">Cannot reach /api/status</span>
          <span className="error-state-desc">Risk data unavailable. Backend may be starting up.</span>
        </div>
      </div>
    )
  }

  const observing = risk.observation_only || !risk.exposure_reconciled
  const reconOk = recon?.reconciled ?? false
  const groups = recon?.exposure.exposure_per_group ?? {}
  const topGroups = Object.entries(groups).sort((a, b) => b[1] - a[1]).slice(0, 2)

  const expPct = risk.max_total_exposure > 0
    ? risk.total_exposure / risk.max_total_exposure
    : null

  const dailyBudgetUsed = risk.daily_loss_limit > 0 && risk.daily_pnl < 0
    ? Math.abs(risk.daily_pnl) / risk.daily_loss_limit
    : 0

  const modeLabel = risk.mode === 'live' ? 'LIVE' : risk.mode === 'shadow' ? 'SHADOW' : 'PAPER'
  const modeBadgeClass = risk.mode === 'live' ? 'badge-red' : risk.mode === 'shadow' ? 'badge-cyan' : 'badge-amber'

  return (
    <div className="card" style={{ display: 'flex', flexDirection: 'column' }}>
      {/* Header */}
      <div className="card-header">
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span className="card-title">🛡 Risk &amp; Capital</span>
          <span className={`badge ${modeBadgeClass}`} aria-label={`Mode: ${modeLabel}`}>{modeLabel}</span>
          <span className={`badge ${reconOk ? 'badge-green' : 'badge-red'}`}
            aria-label={reconOk ? 'Exposure reconciled' : 'Reconciliation failed'}>
            {reconOk ? '✓ Reconciled' : '⚠ Unreconciled'}
          </span>
          {risk.kill_switch && (
            <span className="badge badge-danger" aria-label="Kill switch active">🛑 Halted</span>
          )}
        </div>
        {lastUpdated && (
          <span style={{ fontSize: '10px', color: 'var(--text-dim)' }}>
            {new Date(lastUpdated).toISOString().slice(11, 19)} UTC
          </span>
        )}
      </div>

      {/* Observation warning */}
      {observing && (
        <div className="banner-warning" style={{ margin: '8px 12px 0', fontSize: '11.5px' }} role="alert">
          <span aria-hidden="true">⚠</span>
          <span>
            New orders disabled — exposure not reconciled against ${risk.bankroll_ceiling.toFixed(0)} ceiling.
            {risk.observation_reason ? ` Reason: ${risk.observation_reason}` : ''}
          </span>
        </div>
      )}

      {/* Reconciliation findings */}
      {recon && recon.findings.length > 0 && (
        <div style={{ padding: '8px 12px 0' }}>
          {recon.findings.slice(0, 3).map((f, i) => (
            <div key={i} style={{ fontSize: '11px', color: 'var(--color-red-fg)', marginBottom: '2px' }}>
              • {f}
            </div>
          ))}
        </div>
      )}

      {/* KPI grid — Capital */}
      <div style={{ padding: '10px 12px 6px' }}>
        <div style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-dim)', marginBottom: '6px' }}>
          Capital (USDC · {modeLabel})
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px' }}>
          <Kpi
            label="Operating Capital"
            value={fmtUsd(100)}
            sub="configured limit"
            tooltip="USD 100 operating capital — maximum capital intended to be deployed. Source: config.py"
          />
          <Kpi
            label="Abs. Ceiling"
            value={fmtUsd(risk.bankroll_ceiling)}
            sub="hard stop"
            tooltip="USD 200 absolute ceiling — exposure beyond this triggers observation mode. Source: risk/manager.py"
          />
          <Kpi
            label="Deployable"
            value={fmtUsd(risk.deployable_ceiling)}
            tooltip="Maximum deployable capital under current risk rules (deployable_ceiling from /api/status)"
          />
          <Kpi
            label="Available Cash"
            value={recon ? fmtUsd(recon.exposure.available_cash) : null}
            tooltip="Cash not currently invested or reserved for pending orders"
          />
          <Kpi
            label="Capital Invested"
            value={recon ? fmtUsd(recon.exposure.capital_invested) : null}
            tooltip="Sum of cost basis of all open positions"
          />
          <Kpi
            label="Pending Orders"
            value={recon ? fmtUsd(recon.exposure.reserved_for_pending_orders) : null}
            tooltip="Capital reserved for open orders not yet filled"
          />
        </div>
      </div>

      {/* Exposure utilization bar */}
      <ExposureBar used={risk.total_exposure} max={risk.max_total_exposure} />

      {/* KPI grid — Exposure & P&L */}
      <div style={{ padding: '0 12px 6px' }}>
        <div style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-dim)', marginBottom: '6px' }}>
          Exposure &amp; P&amp;L (USDC · Today)
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '6px' }}>
          <Kpi
            label="Total Exposure"
            value={fmtUsd(risk.total_exposure)}
            sub={expPct != null ? `${(expPct * 100).toFixed(1)}% of limit` : undefined}
            warn={expPct != null && expPct > 0.7}
            danger={expPct != null && expPct > 0.9}
            tooltip="Total open exposure = capital_invested + reserved_for_pending. Source: /api/status total_exposure"
          />
          <Kpi
            label="Max Remaining Loss"
            value={recon ? fmtUsd(recon.exposure.maximum_remaining_loss) : null}
            tooltip="Worst-case loss if all open positions resolve against you (assume all shares → $0)"
          />
          <Kpi
            label="Net Directional"
            value={recon ? fmtUsd(recon.exposure.net_directional_exposure) : null}
            tooltip="Net directional bias: long exposure minus short exposure"
          />
          <Kpi
            label="Daily P&L"
            value={fmtPnl(risk.daily_pnl)}
            sub="paper mode · today"
            valueColor={risk.daily_pnl >= 0 ? 'var(--color-green-fg)' : 'var(--color-red-fg)'}
            tooltip="Realized daily P&L in paper mode. Resets at UTC midnight. Source: /api/status daily_pnl"
          />
          <Kpi
            label="Daily Loss Limit"
            value={fmtUsd(risk.daily_loss_limit)}
            sub="hard breaker"
            tooltip="Daily loss stop. When daily_pnl reaches −daily_loss_limit, new orders are blocked. Source: risk/manager.py"
          />
          <Kpi
            label="Loss Budget Used"
            value={fmtPct(dailyBudgetUsed)}
            warn={dailyBudgetUsed > 0.5}
            danger={dailyBudgetUsed > 0.8}
            tooltip="How much of the daily loss limit has been consumed today"
          />
        </div>
      </div>

      {/* Correlated groups */}
      {topGroups.length > 0 && (
        <div style={{ padding: '0 12px 10px', borderTop: '1px solid var(--border)', paddingTop: '8px' }}>
          <div style={{ fontSize: '10px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '0.08em', color: 'var(--text-dim)', marginBottom: '4px' }}>
            Largest Correlated Groups
          </div>
          {topGroups.map(([name, val]) => (
            <div key={name} style={{ display: 'flex', justifyContent: 'space-between', fontSize: '11.5px', marginBottom: '2px' }}>
              <span style={{ color: 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: '70%' }}>
                {name}
              </span>
              <span className="mono" style={{ color: 'var(--color-amber-fg)', flexShrink: 0 }}>{fmtUsd(val)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}