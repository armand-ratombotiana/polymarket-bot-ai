// components/RiskStatusPanel.tsx
// USD 200 hard-bankroll risk exposure monitor: observation-mode gate,
// exposure decomposition, and reconciliation verdict.
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'

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
}

export default function RiskStatusPanel() {
  const [risk, setRisk] = useState<RiskStatus | null>(null)
  const [recon, setRecon] = useState<Reconciliation | null>(null)

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
      } catch {}
    }

    fetchAll()
    const timer = setInterval(fetchAll, 4000)
    return () => clearInterval(timer)
  }, [])

  if (!risk) {
    return (
      <div className="card p-3 flex items-center justify-center text-xs text-[#4a5068]">
        Loading risk status…
      </div>
    )
  }

  const observing = risk.observation_only || !risk.exposure_reconciled
  const groups = recon?.exposure.exposure_per_group ?? {}
  const topGroup = Object.entries(groups).sort((a, b) => b[1] - a[1])[0]

  return (
    <div className="card flex flex-col">
      <div className="card-header">
        <span className="card-title">🛡️ Risk &amp; Exposure</span>
        <span className={`text-[11px] font-semibold mono ${observing ? 'text-amber-400' : 'text-green-400'}`}>
          {observing ? 'OBSERVATION ONLY' : 'RECONCILED'}
        </span>
      </div>

      {observing && (
        <div className="mx-3 mt-2 px-3 py-2 rounded bg-amber-500/10 border border-amber-500/30 text-[11px] text-amber-300 leading-relaxed">
          ⚠ New live orders disabled — exposure not reconciled against the{' '}
          {risk.bankroll_ceiling.toFixed(0)} bankroll ceiling. Reduce open exposure
          before resuming live trading.
        </div>
      )}

      <div className="p-3 grid grid-cols-2 gap-2 text-[11px]">
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Deployable Ceiling</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${risk.deployable_ceiling.toFixed(0)} / ${risk.bankroll_ceiling.toFixed(0)}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Open Exposure</span>
          <span className={`mono font-semibold text-[13px] ${risk.total_exposure > risk.deployable_ceiling ? 'text-red-400' : 'text-amber-400'}`}>
            ${risk.total_exposure.toFixed(2)}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Max Remaining Loss</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${recon?.exposure.maximum_remaining_loss.toFixed(2) ?? '—'}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Capital Invested</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${recon?.exposure.capital_invested.toFixed(2) ?? '—'}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Pending Orders</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${recon?.exposure.reserved_for_pending_orders.toFixed(2) ?? '—'}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Available Cash</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${recon?.exposure.available_cash.toFixed(2) ?? '—'}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Exposure / Day</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            {recon?.exposure.exposure_dollar_days.toFixed(1) ?? '—'}
          </span>
        </div>
        <div className="bg-[#111318] p-2 rounded border border-[#252836]">
          <span className="text-[#4a5068] block">Net Directional</span>
          <span className="mono font-semibold text-[#e8eaf0] text-[13px]">
            ${recon?.exposure.net_directional_exposure.toFixed(2) ?? '—'}
          </span>
        </div>
      </div>

      {topGroup && (
        <div className="px-3 pb-2 text-[11px]">
          <span className="text-[#4a5068]">Largest correlated group: </span>
          <span className="mono text-amber-400">
            {topGroup[0].slice(0, 24)} ${topGroup[1].toFixed(2)}
          </span>
        </div>
      )}

      {recon && recon.findings.length > 0 && (
        <div className="px-3 pb-3 space-y-1">
          {recon.findings.slice(0, 3).map((f, i) => (
            <div key={i} className="text-[10px] text-red-400/80 leading-snug">
              • {f}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}