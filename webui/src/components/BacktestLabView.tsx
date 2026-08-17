// components/BacktestLabView.tsx — Quantitative Strategy Backtest & Simulation Lab
'use client'

import { useState } from 'react'
import { getApiUrl } from '@/lib/api'

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
  { id: 'mm_avellaneda_stoikov', name: 'Avellaneda-Stoikov Market Maker' },
  { id: 'arb_binary_dutch_book', name: 'Binary Dutch Book Arbitrage' },
  { id: 'ml_random_forest_quant', name: 'Random Forest Quant Classifier' },
  { id: 'mom_ema_crossover', name: 'EMA Crossover Trend Follower' },
  { id: 'stat_bollinger_reversion', name: 'Bollinger Bands Mean Reversion' },
  { id: 'event_whale_follower', name: 'Whale Block Order Follower' },
]

export default function BacktestLabView() {
  const [strategyId, setStrategyId] = useState('mm_avellaneda_stoikov')
  const [capital, setCapital] = useState(10000)
  const [days, setDays] = useState(30)
  const [slippage, setSlippage] = useState(5)
  const [running, setRunning] = useState(false)
  const [result, setResult] = useState<BacktestData | null>(null)

  const handleRun = async () => {
    setRunning(true)
    try {
      const apiUrl = getApiUrl()
      const res = await fetch(`${apiUrl}/api/backtest/run`, {
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
      }
    } catch {}
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
    <div className="flex flex-col h-full bg-[#111318] border border-[#252836] rounded-lg overflow-hidden p-4 space-y-4 overflow-y-auto scrollbar-thin">
      {/* Top Header */}
      <div className="flex justify-between items-center pb-3 border-b border-[#252836]">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-xl">🧪</span>
            <span className="text-base font-bold text-[#e8eaf0]">
              Quantitative Strategy Backtest &amp; Simulation Lab
            </span>
          </div>
          <p className="text-xs text-[#8b91a8]">
            Simulate limit orders, queue priority, slippage models, and institutional performance metrics
          </p>
        </div>
        <span className="badge badge-blue text-xs">Monte Carlo / Tick Engine</span>
      </div>

      {/* Configuration Controls Bar */}
      <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-wrap gap-4 items-end">
        <div className="flex-1 min-w-[200px]">
          <label className="text-[11px] text-[#8b91a8] block mb-1">Select Strategy</label>
          <select
            value={strategyId}
            onChange={(e) => setStrategyId(e.target.value)}
            className="w-full bg-[#111318] border border-[#252836] rounded px-3 py-1.5 text-xs text-[#e8eaf0] focus:outline-none focus:border-blue-500"
          >
            {POPULAR_STRATS.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="text-[11px] text-[#8b91a8] block mb-1">Initial Capital ($)</label>
          <input
            type="number"
            value={capital}
            onChange={(e) => setCapital(Number(e.target.value))}
            className="w-28 bg-[#111318] border border-[#252836] rounded px-3 py-1.5 text-xs mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
          />
        </div>

        <div>
          <label className="text-[11px] text-[#8b91a8] block mb-1">Horizon (Days)</label>
          <input
            type="number"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            className="w-24 bg-[#111318] border border-[#252836] rounded px-3 py-1.5 text-xs mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
          />
        </div>

        <div>
          <label className="text-[11px] text-[#8b91a8] block mb-1">Slippage (BPS)</label>
          <input
            type="number"
            value={slippage}
            onChange={(e) => setSlippage(Number(e.target.value))}
            className="w-24 bg-[#111318] border border-[#252836] rounded px-3 py-1.5 text-xs mono text-[#e8eaf0] focus:outline-none focus:border-blue-500"
          />
        </div>

        <button
          onClick={handleRun}
          disabled={running}
          className="btn btn-primary px-5 py-2 text-xs font-bold"
        >
          {running ? 'Simulating…' : '▶ Run Backtest'}
        </button>
      </div>

      {/* Results View */}
      {result && (
        <div className="space-y-4">
          {/* KPI Matrix */}
          <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-6 gap-3">
            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Total Realized P&amp;L</span>
              <span className="mono text-lg font-bold text-green-400">
                +${result.total_pnl.toFixed(2)}
              </span>
              <span className="text-[10px] text-green-400 block mt-0.5">ROI: +{result.roi_pct}%</span>
            </div>

            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Sharpe Ratio</span>
              <span className="mono text-lg font-bold text-cyan-400">
                {result.sharpe_ratio.toFixed(2)}
              </span>
              <span className="text-[10px] text-[#8b91a8] block mt-0.5">Risk-adjusted return</span>
            </div>

            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Max Drawdown (MDD)</span>
              <span className="mono text-lg font-bold text-amber-400">
                {result.max_drawdown_pct.toFixed(2)}%
              </span>
              <span className="text-[10px] text-[#8b91a8] block mt-0.5">From high-water mark</span>
            </div>

            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Profit Factor</span>
              <span className="mono text-lg font-bold text-blue-400">
                {result.profit_factor.toFixed(2)}
              </span>
              <span className="text-[10px] text-[#8b91a8] block mt-0.5">Gross Win / Loss ratio</span>
            </div>

            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Win Rate %</span>
              <span className="mono text-lg font-bold text-green-400">
                {(result.win_rate * 100).toFixed(1)}%
              </span>
              <span className="text-[10px] text-[#8b91a8] block mt-0.5">{result.winning_trades} / {result.total_trades} wins</span>
            </div>

            <div className="bg-[#161822] p-3 rounded-lg border border-[#252836]">
              <span className="text-[11px] text-[#4a5068] block">Sortino Ratio</span>
              <span className="mono text-lg font-bold text-purple-400">
                {result.sortino_ratio.toFixed(2)}
              </span>
              <span className="text-[10px] text-[#8b91a8] block mt-0.5">Downside deviation</span>
            </div>
          </div>

          {/* Equity Chart & Monthly Breakdown */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <div className="lg:col-span-2 card p-3.5 bg-[#161822] border border-[#252836]">
              <div className="card-header pb-2 mb-2 border-b border-[#252836]/60 flex justify-between items-center">
                <span className="card-title text-xs font-bold text-[#e8eaf0]">
                  📈 Simulated Portfolio Equity Curve
                </span>
                <span className="mono text-xs text-green-400 font-bold">
                  Final: ${result.final_equity.toLocaleString()}
                </span>
              </div>

              <div className="h-44 flex items-center justify-center">
                <svg viewBox="0 0 400 130" className="w-full h-full">
                  <path
                    d={getEquitySvgPath()}
                    fill="none"
                    stroke="#22c55e"
                    strokeWidth="2.5"
                    strokeLinecap="round"
                  />
                </svg>
              </div>
            </div>

            {/* Weekly Return Breakdown */}
            <div className="card p-3.5 bg-[#161822] border border-[#252836] flex flex-col justify-between">
              <div>
                <div className="card-header pb-2 mb-2 border-b border-[#252836]/60">
                  <span className="card-title text-xs font-bold text-[#e8eaf0]">
                    📅 Period Performance Breakdown
                  </span>
                </div>
                <div className="space-y-2 mt-2">
                  {Object.entries(result.monthly_returns).map(([period, ret]) => (
                    <div key={period} className="flex justify-between items-center text-xs bg-[#111318] p-2 rounded">
                      <span className="text-[#8b91a8]">{period}</span>
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
