// components/EquityCurve.tsx — Real-Time Equity Curve Chart
'use client'

import { useEffect, useState } from 'react'
import { getApiUrl } from '@/lib/api'

interface EquityPoint {
  timestamp: number
  equity: number
  pnl: number
}

export default function EquityCurve() {
  const [points, setPoints] = useState<EquityPoint[]>([])

  useEffect(() => {
    const fetchEquity = async () => {
      try {
        const apiUrl = getApiUrl()
        const res = await fetch(`${apiUrl}/api/history/equity`)
        if (res.ok) {
          const json = await res.json()
          if (json.points && json.points.length > 0) {
            setPoints(json.points)
          }
        }
      } catch {}
    }
    fetchEquity()
    const timer = setInterval(fetchEquity, 3000)
    return () => clearInterval(timer)
  }, [])

  const currentEquity = points.length > 0 ? points[points.length - 1].equity : null
  const currentPnl = points.length > 0 ? points[points.length - 1].pnl : 0.0

  if (points.length < 2) {
    return (
      <div className="card p-3 flex flex-col justify-between h-full min-h-[140px]">
        <div className="card-header pb-1">
          <span className="card-title">📈 Equity Curve</span>
          <span className="mono text-xs text-green-400 font-semibold">
            {currentEquity !== null ? `$${currentEquity.toFixed(2)}` : '—'}
          </span>
        </div>
        <div className="flex-1 flex items-center justify-center text-xs text-[#4a5068]">
          Accumulating equity points…
        </div>
      </div>
    )
  }

  // Calculate SVG path
  const minEq = Math.min(...points.map((p) => p.equity)) * 0.999
  const maxEq = Math.max(...points.map((p) => p.equity)) * 1.001
  const range = maxEq - minEq || 1

  const width = 280
  const height = 75
  const padding = 5

  const coords = points.map((p, i) => {
    const x = padding + (i / (points.length - 1)) * (width - 2 * padding)
    const y = height - padding - ((p.equity - minEq) / range) * (height - 2 * padding)
    return { x, y }
  })

  const pathD = coords.reduce((acc, pt, i) => (i === 0 ? `M ${pt.x},${pt.y}` : `${acc} L ${pt.x},${pt.y}`), '')
  const areaD = `${pathD} L ${coords[coords.length - 1].x},${height} L ${coords[0].x},${height} Z`

  const isProfit = currentPnl >= 0
  const strokeColor = isProfit ? '#22c55e' : '#ef4444'
  const fillColor = isProfit ? 'rgba(34, 197, 94, 0.15)' : 'rgba(239, 68, 68, 0.15)'

  return (
    <div className="card p-3 flex flex-col justify-between h-full min-h-[140px]">
      <div className="card-header pb-1 flex justify-between items-center">
        <span className="card-title">📈 Portfolio Equity</span>
        <div className="flex items-center gap-2">
          <span className="mono text-xs font-semibold text-[#e8eaf0]">
            {currentEquity !== null ? `$${currentEquity.toFixed(2)}` : '—'}
          </span>
          <span className={`badge ${isProfit ? 'badge-green' : 'badge-red'} text-[10px]`}>
            {isProfit ? '+' : ''}${currentPnl.toFixed(2)}
          </span>
        </div>
      </div>

      {/* SVG Chart */}
      <div className="flex-1 flex items-center justify-center py-1">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-full overflow-visible">
          <defs>
            <linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={strokeColor} stopOpacity="0.3" />
              <stop offset="100%" stopColor={strokeColor} stopOpacity="0.0" />
            </linearGradient>
          </defs>
          <path d={areaD} fill="url(#eqGrad)" />
          <path d={pathD} fill="none" stroke={strokeColor} strokeWidth="2" strokeLinecap="round" />
          {coords.length > 0 && (
            <circle
              cx={coords[coords.length - 1].x}
              cy={coords[coords.length - 1].y}
              r="3.5"
              fill={strokeColor}
            />
          )}
        </svg>
      </div>

      <div className="flex justify-between text-[10px] text-[#4a5068] pt-1 mono">
        <span>Min: ${minEq.toFixed(1)}</span>
        <span>Peak: ${maxEq.toFixed(1)}</span>
      </div>
    </div>
  )
}
