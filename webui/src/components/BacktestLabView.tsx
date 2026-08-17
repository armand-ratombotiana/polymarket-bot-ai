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
  sharpe_ratio: number
  sortino_ratio: number
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
  { id: 'ml_random_forest_quant', name: 'Random Forest Quant Classifier (Active)' },
  { id: 'mom_ema_crossover', name: 'EMA Crossover Trend Follower (Research)' },
  { id: 'stat_bollinger_reversion', name: 'Bollinger Bands Mean Reversion (Research)' },
  { id: 'event_whale_follower', name: 'Whale Block Order Follower (Research)' },
]

export default function BacktestLabView() {
  const [strategyId, setStrategyId] = useState('mm_avellaneda_stoikov')
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
    <div className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4 space-y-3 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex justify-between items-center pb-2 border-b border-[#1f2335]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-lg" aria-hidden="true">🧪</span>
            <span className="text-sm font-bold text-[#dde1ed]">
              Quantitative Strategy Simulation Lab
            </span>
            <span className="mode-badge mode-badge-backtest">BACKTEST</span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            Monte Carlo parameter sweeps, queue priority models, and simulated slippage
          </p>
        </div>
      </div>

      {/* Backtest Notice Watermark */}
      <div className="banner-experimental text-xs py-2 px-3" role="note">
        <span aria-hidden="true">⚠️</span>
        <span>
          <strong>SIMULATION NOTICE:</strong> BACKTEST — HISTORICAL SIMULATION USING MONTE CARLO ARCHETYPES. This simulates strategy performance against statistical distributions, not historical live tick replays.
        </span>
      </div>

      {/* Configuration Controls Bar */}
      <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-wrap gap-3 items-end">
        <div className="flex-1 min-w-[200px]">
          <label className="form-label text-[10px]">Strategy Archetype</label>
          <select
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            className="select w-full text-xs"
          >
            {POPULAR_STRATS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="form-label text-[10px]">Simulated Capital ($)</label>
          <input
            type="number"
            value={capital}
            onChange={(e) => setCapital(Number(e.target.value))}
            className="input input-sm w-28 mono"
          />
        </div>

        <div>
          <label className="form-label text-[10px]">Horizon (Days)</label>
          <input
            type="number"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="input input-sm w-24 mono"
          />
        </div>

        <div>
          <label className="form-label text-[10px]">Slippage (BPS)</label>
          <input
            type="number"
            value={slippage}
            onChange={(e) => setSlippage(Number(e.target.value))}
            className="input input-sm w-24 mono"
          />
        </div>

        <button
          onClick={handleRun}
          disabled={running}
          className="btn btn-primary btn-sm"
        >
          {running ? (
            <>
              <span className="spinner mr-1" aria-hidden="true" />
              Simulating…
            </>
          ) : (
            '▶ Run Simulation'
          )}
        </button>
      </div>

      {error && (
        <div className="banner-danger text-xs py-2 px-3">
          ⚠️ {error}
        </div>
      )}

      {/* Results View */}
      {result && (
        <div className="space-y-3">
          {/* KPI Matrix */}
          <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-2.5">
            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Simulated P&amp;L</span>
              <span className="mono text-base font-bold text-green-400">
                +{fmtUsd(result.total_pnl)}
              </span>
              <span className="text-[9.5px] text-green-400 block mt-0.5">ROI: +{result.roi_pct}%</span>
            </div>

            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Sharpe Ratio</span>
              <span className="mono text-base font-bold text-cyan-400">
                {result.sharpe_ratio.toFixed(2)}
              </span>
              <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Risk-adjusted</span>
            </div>

            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Max Drawdown</span>
              <span className="mono text-base font-bold text-amber-400">
                {fmtPct(result.max_drawdown_pct / 100)}
              </span>
              <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Peak-to-trough</span>
            </div>

            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Profit Factor</span>
              <span className="mono text-base font-bold text-blue-400">
                {result.profit_factor.toFixed(2)}
              </span>
              <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Wins / Losses</span>
            </div>

            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Win Rate %</span>
              <span className="mono text-base font-bold text-green-400">
                {fmtPct(result.win_rate)}
              </span>
              <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">{result.winning_trades} / {result.total_trades} wins</span>
            </div>

            <div className="bg-[#0e1015] p-3 rounded-lg border border-[#1f2335]">
              <span className="text-[10px] text-[#7e8aaa] uppercase font-medium block">Sortino Ratio</span>
              <span className="mono text-base font-bold text-purple-400">
                {result.sortino_ratio.toFixed(2)}
              </span>
              <span className="text-[9.5px] text-[#7e8aaa] block mt-0.5">Downside volatility</span>
            </div>
          </div>

          {/* Equity Chart & Period Breakdown */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
            <div className="lg:col-span-2 card p-3 bg-[#0e1015] border border-[#1f2335]">
              <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335] flex justify-between items-center">
                <span className="card-title text-xs font-bold text-[#dde1ed]">
                  📈 Simulated Equity Timeline
                </span>
                <span className="mono text-xs text-green-400 font-bold">
                  Final: {fmtUsd(result.final_equity)}
                </span>
              </div>

              <div className="h-40 flex items-center justify-center">
                <svg viewBox="0 0 400 130" className="w-full h-full" role="img" aria-label="Simulated equity curve">
                  <path
                    d={getEquitySvgPath()}
                    fill="none"
                    stroke="#22c55e"
                    strokeWidth="2"
                    strokeLinecap="round"
                  />
                </svg>
              </div>
            </div>

            {/* Weekly Return Breakdown */}
            <div className="card p-3 bg-[#0e1015] border border-[#1f2335] flex flex-col justify-between">
              <div>
                <div className="card-header pb-1.5 mb-1.5 border-b border-[#1f2335]">
                  <span className="card-title text-xs font-bold text-[#dde1ed]">
                    📅 Period Breakdown
                  </span>
                </div>
                <div className="space-y-1.5 mt-2">
                  {Object.entries(result.monthly_returns).map(([period, ret]) => (
                    <div key={period} className="flex justify-between items-center text-xs bg-[#13161e] p-2 rounded">
                      <span className="text-[#7e8aaa]">{period}</span>
                      <span className="mono font-bold text-green-400">+{ret}%</span>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
