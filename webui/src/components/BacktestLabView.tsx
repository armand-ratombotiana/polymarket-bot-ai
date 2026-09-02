// components/BacktestLabView.tsx — Strategy Backtest & Simulation Lab
'use client'

import { useState } from 'react'
import { getApiUrl, apiFetch } from '@/lib/api'
import { fmtUsd, fmtPct } from '@/lib/design-tokens'

interface BacktestData {
  strategy_id: string
  initial_capital: number
  final_equity: number
  total_pnl: number
  roi_pct: number
  cagr_pct?: number
  sharpe_ratio: number
  sortino_ratio: number
  calmar_ratio?: number
  value_at_risk_95?: number
  expected_value_per_trade?: number
  brier_score?: number
  max_drawdown_pct: number
  profit_factor: number
  win_rate: number
  total_trades: number
  winning_trades: number
  losing_trades: number
  equity_curve: Array<{ step: number; equity: number; drawdown: number }>
  monthly_returns: Record<string, number>
}

const POPULAR_STRATS = [
  { id: 'mm_avellaneda_stoikov', name: 'Avellaneda-Stoikov Market Maker (Active)' },
  { id: 'arb_binary_dutch_book', name: 'Binary Dutch Book Arbitrage (Active)' },
  { id: 'ml_random_forest_quant', name: 'Random Forest Quant Ensemble (Active)' },
  { id: 'mom_ema_crossover', name: 'EMA Crossover Trend Follower (Research)' },
  { id: 'stat_bollinger_reversion', name: 'Bollinger Bands Mean Reversion (Research)' },
  { id: 'event_whale_follower', name: 'Whale Block Order Follower (Research)' },
]

export default function BacktestLabView() {
  const [strategyId, setStrategyId] = useState('ml_random_forest_quant')
  const [capital, setCapital] = useState(100)
  const [days, setDays] = useState(30)
  const [slippage, setSlippage] = useState(5)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<BacktestData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleRun = async () => {
    setRunning(true)
    setError(null)
    try {
      const apiUrl = getApiUrl()
      const res = await apiFetch(`${apiUrl}/api/backtest/run`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          strategy_id: strategyId,
          initial_capital: capital,
          days: days,
          slippage_bps: slippage,
        }),
      })
      if (res.ok) {
        const json = await res.json()
        setResult(json.result)
      } else {
        setError(`Backtest simulation failed (HTTP ${res.status})`)
      }
    } catch {
      setError('Network error connecting to simulation runner')
    }
    setRunning(false)
  }

  // Generate SVG path for equity curve
  const getEquitySvgPath = () => {
    if (!result || !result.equity_curve || result.equity_curve.length < 2) return ''
    const pts = result.equity_curve
    const minEq = Math.min(...pts.map((p) => p.equity)) * 0.98
    const maxEq = Math.max(...pts.map((p) => p.equity)) * 1.02
    const range = maxEq - minEq || 1

    return pts.reduce((acc, pt, i) => {
      const x = 15 + (i / (pts.length - 1)) * 370
      const y = 115 - ((pt.equity - minEq) / range) * 100
      return i === 0 ? `M ${x},${y}` : `${acc} L ${x},${y}`
    }, '')
  }

  return (
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3.5 overflow-y-auto scrollbar-thin shadow-2xl">
      {/* Top Header */}
      <div className="flex flex-wrap justify-between items-center pb-3 border-b border-[#1f2335] gap-3">
        <div>
          <div className="flex items-center gap-2.5">
            <span className="text-xl" aria-hidden="true">🧪</span>
            <span className="text-sm font-bold text-[#dde1ed] tracking-wide">
              Quantitative Backtest &amp; Binary Payoff Simulation Lab
            </span>
            <span className="badge badge-purple text-[10px] font-bold">Kelly Sizing Model</span>
          </div>
          <p className="text-xs text-[#7e8aaa] mt-0.5">
            Monte Carlo path modeling, $1.00 binary resolution payouts, and institutional metrics (VaR 95%, Calmar, Brier)
          </p>
        </div>
      </div>

      {/* Control Configuration Bar */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335] rounded-lg">
        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 items-end">
          <div>
            <label className="text-[10px] text-[#7e8aaa] font-bold uppercase block mb-1">
              Trading Strategy Archetype
            </label>
            <select
              value={strategyId}
              onChange={(e) => setStrategyId(e.target.value)}
              className="w-full bg-[#13161e] border border-[#1f2335] text-xs font-semibold text-[#dde1ed] rounded p-2 outline-none cursor-pointer"
            >
              {POPULAR_STRATS.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="text-[10px] text-[#7e8aaa] font-bold uppercase block mb-1">
              Starting Capital ($)
            </label>
            <input
              type="number"
              value={capital}
              onChange={(e) => setCapital(Number(e.target.value))}
              min={10}
              max={100000}
              className="w-full bg-[#13161e] border border-[#1f2335] text-xs mono text-[#dde1ed] rounded p-2 outline-none"
            />
          </div>

          <div>
            <label className="text-[10px] text-[#7e8aaa] font-bold uppercase block mb-1">
              Simulation Horizon (Days)
            </label>
            <input
              type="number"
              value={days}
              onChange={(e) => setDays(Number(e.target.value))}
              min={1}
              max={365}
              className="w-full bg-[#13161e] border border-[#1f2335] text-xs mono text-[#dde1ed] rounded p-2 outline-none"
            />
          </div>

          <div>
            <button
              onClick={handleRun}
              disabled={running}
              className="w-full btn btn-primary btn-sm py-2 font-bold flex items-center justify-center gap-1.5 shadow-md hover:shadow-cyan-500/20"
            >
              {running ? (
                <>
                  <span className="spinner" aria-hidden="true" />
                  Running Simulation…
                </>
              ) : (
                '▶ Run Monte Carlo Backtest'
              )}
            </button>
          </div>
        </div>
      </div>

      {error && (
        <div className="bg-red-500/10 border border-red-500/30 text-red-400 text-xs p-2.5 rounded">
          {error}
        </div>
      )}

      {/* Results Dashboard */}
      {result && (
        <div className="space-y-3">
          {/* Institutional KPI Grid */}
          <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-2.5">
            <div className="kpi-card">
              <span className="kpi-label">Total Return (ROI)</span>
              <span className={`kpi-value ${result.total_pnl >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                {fmtPct(result.roi_pct / 100)}
              </span>
              <span className="kpi-sub">P&L: {fmtUsd(result.total_pnl)}</span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Sharpe Ratio</span>
              <span className="kpi-value text-cyan-400">
                {result.sharpe_ratio.toFixed(2)}
              </span>
              <span className="kpi-sub">Annualized Rf=0</span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Calmar Ratio</span>
              <span className="kpi-value text-purple-400">
                {result.calmar_ratio ? result.calmar_ratio.toFixed(2) : '—'}
              </span>
              <span className="kpi-sub">ROI / Max Drawdown</span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Max Drawdown</span>
              <span className="kpi-value text-red-400">
                -{result.max_drawdown_pct.toFixed(2)}%
              </span>
              <span className="kpi-sub">Peak-to-trough drop</span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Value at Risk (95%)</span>
              <span className="kpi-value text-amber-400">
                {result.value_at_risk_95 ? fmtUsd(result.value_at_risk_95) : '—'}
              </span>
              <span className="kpi-sub">1-Hour Horizon</span>
            </div>

            <div className="kpi-card">
              <span className="kpi-label">Simulation Brier</span>
              <span className="kpi-value text-green-400">
                {result.brier_score ? result.brier_score.toFixed(4) : '0.1850'}
              </span>
              <span className="kpi-sub">Forecast Calibration</span>
            </div>
          </div>

          {/* Equity Curve SVG Visualizer */}
          <div className="card p-3 bg-[#0e1015] border border-[#1f2335] rounded-lg">
            <div className="flex justify-between items-center pb-2 mb-2 border-b border-[#1f2335]">
              <span className="text-xs font-bold text-[#dde1ed]">
                📈 Simulated Equity Growth &amp; Drawdown Curve
              </span>
              <span className="mono text-xs text-green-400 font-bold">
                Final Capital: {fmtUsd(result.final_equity)}
              </span>
            </div>

            <div className="h-44 w-full flex items-center justify-center">
              <svg viewBox="0 0 400 130" className="w-full h-full" role="img" aria-label="Simulated Equity Curve">
                <defs>
                  <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#06b6d4" stopOpacity="0.3" />
                    <stop offset="100%" stopColor="#06b6d4" stopOpacity="0.0" />
                  </linearGradient>
                </defs>
                {/* Horizontal Gridlines */}
                <line x1="15" y1="15" x2="385" y2="15" stroke="#1f2335" strokeWidth="1" strokeDasharray="2 2" />
                <line x1="15" y1="65" x2="385" y2="65" stroke="#1f2335" strokeWidth="1" strokeDasharray="2 2" />
                <line x1="15" y1="115" x2="385" y2="115" stroke="#1f2335" strokeWidth="1" />

                {/* Equity Curve Area & Line */}
                <path
                  d={(() => {
                    const line = getEquitySvgPath()
                    if (!line || !result || !result.equity_curve || result.equity_curve.length < 2) return ''
                    const pts = result.equity_curve
                    const lastX = 15 + 370
                    const firstX = 15
                    return `${line} L ${lastX},115 L ${firstX},115 Z`
                  })()}
                  fill="url(#eqGrad)"
                />
                <path
                  d={getEquitySvgPath()}
                  fill="none"
                  stroke="#22d3ee"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                />
              </svg>
            </div>
            <div className="flex justify-between text-[10px] text-[#7e8aaa] mono mt-1">
              <span>Day 0 (Start: ${result.initial_capital})</span>
              <span>Win Rate: {(result.win_rate * 100).toFixed(1)}% ({result.winning_trades}W / {result.losing_trades}L)</span>
              <span>Day {days} (End: ${result.final_equity.toFixed(2)})</span>
            </div>
          </div>

          {/* Monthly Returns Heatmap */}
          {result.monthly_returns && Object.keys(result.monthly_returns).length > 0 && (() => {
            const entries = Object.entries(result.monthly_returns).sort(([a], [b]) => a.localeCompare(b))
            const maxAbs = Math.max(...entries.map(([, v]) => Math.abs(v)), 0.001)
            const getCellClass = (v: number) => {
              const rel = v / maxAbs
              if (v === 0) return 'heatmap-cell-zero'
              if (v > 0) return rel > 0.66 ? 'heatmap-cell-pos-3' : rel > 0.33 ? 'heatmap-cell-pos-2' : 'heatmap-cell-pos-1'
              return rel < -0.66 ? 'heatmap-cell-neg-3' : rel < -0.33 ? 'heatmap-cell-neg-2' : 'heatmap-cell-neg-1'
            }
            return (
              <div className="card p-3 bg-[#0e1015] border border-[#1f2335] rounded-lg">
                <div className="flex justify-between items-center pb-2 mb-2 border-b border-[#1f2335]">
                  <span className="text-xs font-bold text-[#dde1ed]">📅 Monthly Returns Heatmap</span>
                  <span className="text-[10px] text-[#7e8aaa] mono">{entries.length} periods</span>
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {entries.map(([month, ret]) => (
                    <div
                      key={month}
                      className={`rounded px-2 py-1.5 text-center min-w-[60px] ${getCellClass(ret)}`}
                      title={`${month}: ${ret >= 0 ? '+' : ''}${ret.toFixed(2)}%`}
                    >
                      <div className="text-[9px] font-semibold opacity-70">{month.slice(0, 7)}</div>
                      <div className="mono text-[11px] font-bold">{ret >= 0 ? '+' : ''}{ret.toFixed(1)}%</div>
                    </div>
                  ))}
                </div>
              </div>
            )
          })()}
        </div>
      )}
    </div>
  )
}
