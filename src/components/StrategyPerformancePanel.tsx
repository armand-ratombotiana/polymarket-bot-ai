// components/StrategyPerformancePanel.tsx — Comprehensive Strategy
// Performance Dashboard with per-strategy attribution (W23-5).
//
// Surfaces a per-strategy breakdown of P&L, win rate, profit factor,
// expectancy, Sharpe / Sortino / Calmar ratios, trade counts, average
// hold time, and an enabled/disabled toggle — the trader's one-glance
// view of which strategies are pulling their weight and which are
// dragging the book.
//
// Backend contract:
//
//   GET /api/strategies/performance
//     → {
//         strategies: StrategyPerformanceRow[],
//         total_pnl: number,
//         active_count: number,
//         implemented_count: number,
//         planned_count: number,
//         generated_at: number,
//       }
//
//   Each StrategyPerformanceRow mirrors what `core.portfolio.strategy_performance`
//   emits — see the W23-5 backend docstring in
//   `mini-services/polymarket-bot/core/portfolio.py`.
//
// Features:
//   1. Strategy overview cards (one per active strategy) with P&L,
//      win rate, profit factor, expectancy, Sharpe, trade count,
//      avg hold time + enabled/disabled toggle.
//   2. Attribution breakdown bar chart (PnLBarChart — green/red bars).
//   3. Performance comparison table (sortable by every numeric column).
//   4. Equity curves overlay — multi-line Recharts chart showing
//      cumulative P&L per strategy over time.
//   5. Risk-adjusted ranking (Sharpe / Sortino / Calmar sort selector).
//   6. Auto-refresh — polls every 30s, paused when document is hidden
//      (mirrors the visibility-aware pattern from DatabaseStatusPanel +
//      ObservabilityPanel).

'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  LayoutGrid,
  BarChart3,
  Table2,
  LineChart as LineChartIcon,
  Trophy,
  RefreshCw,
  AlertTriangle,
  Power,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
} from 'lucide-react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { apiFetch } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Switch } from '@/components/ui/switch'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { PnLBarChart } from '@/components/charts'
import { chartTheme, axisProps, gridProps, tooltipStyle } from '@/components/charts/theme'

// ────────────────────────────────────────────────────────────────────────────
// Types — mirror the backend `core.portfolio.strategy_performance` payload
// ────────────────────────────────────────────────────────────────────────────

export type StrategyStatus = 'IMPLEMENTED' | 'PLANNED'

export interface EquityCurvePoint {
  timestamp: number
  pnl: number
}

export interface StrategyPerformanceRow {
  strategy_id: string
  name: string
  version: string
  category: string
  description: string
  risk_level: string
  status: StrategyStatus
  is_running: boolean
  is_enabled: boolean
  // P&L
  realized_pnl: number
  unrealized_pnl: number
  net_pnl: number
  gross_pnl: number
  // Trade stats
  closed_trades: number
  open_trades: number
  fills: number
  win_rate: number
  profit_factor: number | null
  expectancy: number
  avg_win: number
  avg_loss: number
  // Risk
  sharpe_ratio: number | null
  sortino_ratio: number | null
  calmar_ratio: number | null
  max_drawdown: number
  // Trade timing
  avg_hold_hours: number
  notional_volume: number
  open_exposure: number
  // Equity curve (cumulative P&L over time)
  equity_curve: EquityCurvePoint[]
}

export interface StrategyPerformanceResponse {
  strategies: StrategyPerformanceRow[]
  total_pnl: number
  active_count: number
  implemented_count: number
  planned_count: number
  generated_at: number
}

// ────────────────────────────────────────────────────────────────────────────
// Constants
// ────────────────────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 30_000
const PERF_ENDPOINT = '/api/strategies/performance'
const TOGGLE_ENDPOINT = '/api/strategies/toggle'

// Distinct line colors per strategy — keeps the multi-line equity
// overlay readable when 5+ strategies share the same chart.
const STRATEGY_COLORS = [
  '#10b981', // emerald
  '#06b6d4', // cyan
  '#f59e0b', // amber
  '#a855f7', // purple
  '#ec4899', // pink
  '#84cc16', // lime
  '#3b82f6', // blue (only used after the first 6 are taken)
  '#f97316', // orange
]

// Risk-adjusted ranking sort options.
type RiskMetric = 'sharpe' | 'sortino' | 'calmar'
const RISK_METRICS: Array<{ id: RiskMetric; label: string; help: string }> = [
  { id: 'sharpe', label: 'Sharpe', help: 'Return / total volatility (annualised)' },
  { id: 'sortino', label: 'Sortino', help: 'Return / downside volatility (annualised)' },
  { id: 'calmar', label: 'Calmar', help: 'Annualised return / max drawdown' },
]

// Table sort columns.
type SortKey =
  | 'name'
  | 'status'
  | 'net_pnl'
  | 'win_rate'
  | 'profit_factor'
  | 'expectancy'
  | 'sharpe_ratio'
  | 'sortino_ratio'
  | 'calmar_ratio'
  | 'closed_trades'
  | 'avg_hold_hours'
  | 'max_drawdown'

type SortDir = 'asc' | 'desc'

// ────────────────────────────────────────────────────────────────────────────
// Formatting helpers
// ────────────────────────────────────────────────────────────────────────────

function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  const sign = v < 0 ? '−' : v > 0 ? '+' : ''
  return `${sign}$${Math.abs(v).toFixed(digits)}`
}

function fmtUsdPlain(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `$${v.toFixed(digits)}`
}

function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return v.toFixed(digits)
}

function fmtHours(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v) || v <= 0) return '—'
  if (v < 1) return `${(v * 60).toFixed(0)}m`
  if (v < 24) return `${v.toFixed(1)}h`
  return `${(v / 24).toFixed(1)}d`
}

function fmtTime(ts: number | undefined): string {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleTimeString()
}

// ────────────────────────────────────────────────────────────────────────────
// Sub-components
// ────────────────────────────────────────────────────────────────────────────

interface StatusBadgeProps {
  status: StrategyStatus
}

function StatusBadge({ status }: StatusBadgeProps) {
  const implemented = status === 'IMPLEMENTED'
  return (
    <Badge
      variant={implemented ? 'success' : 'secondary'}
      className="text-[9px] px-1.5 py-0.5 font-bold uppercase tracking-wider"
      data-testid="strategy-status-badge"
    >
      {status}
    </Badge>
  )
}

interface RiskLevelBadgeProps {
  level: string
}

function RiskLevelBadge({ level }: RiskLevelBadgeProps) {
  const upper = (level ?? '').toUpperCase()
  const cls =
    upper === 'LOW'
      ? 'text-green-400 bg-green-500/10 border-green-500/20'
      : upper === 'MEDIUM'
        ? 'text-amber-400 bg-amber-500/10 border-amber-500/20'
        : 'text-red-400 bg-red-500/10 border-red-500/20'
  return (
    <span
      className={`text-[9px] px-1.5 py-0.5 rounded font-bold uppercase mono border ${cls}`}
      data-testid="strategy-risk-badge"
    >
      {upper || 'UNKNOWN'}
    </span>
  )
}

interface PnlValueProps {
  value: number | null | undefined
  className?: string
  digits?: number
}

function PnlValue({ value, className = '', digits = 2 }: PnlValueProps) {
  const isPos = (value ?? 0) > 0
  const isNeg = (value ?? 0) < 0
  const cls = isPos
    ? 'text-green-400'
    : isNeg
      ? 'text-red-400'
      : 'text-[#7e8aaa]'
  return (
    <span className={`mono font-semibold ${cls} ${className}`} data-testid="pnl-value">
      {fmtUsd(value, digits)}
    </span>
  )
}

interface StatTileProps {
  label: string
  value: string
  valueClass?: string
  hint?: string
}

function StatTile({ label, value, valueClass = '', hint }: StatTileProps) {
  return (
    <div
      className="flex flex-col gap-0.5 px-2.5 py-1.5 bg-[#0e1015] rounded border border-[#1f2335]"
      data-testid="stat-tile"
    >
      <span className="text-[9px] uppercase tracking-wider text-[#5a637a] font-bold">
        {label}
      </span>
      <span className={`mono text-xs font-bold ${valueClass}`}>{value}</span>
      {hint && <span className="text-[9px] text-[#5a637a]">{hint}</span>}
    </div>
  )
}

interface StrategyCardProps {
  row: StrategyPerformanceRow
  color: string
  onToggle: (strategyId: string, next: boolean) => void
  toggling?: boolean
}

function StrategyCard({ row, color, onToggle, toggling }: StrategyCardProps) {
  const implemented = row.status === 'IMPLEMENTED'
  return (
    <Card
      className={`bg-[#141724] border rounded-lg overflow-hidden ${
        row.is_running && implemented
          ? 'border-blue-500/40 shadow-sm shadow-blue-500/10'
          : 'border-[#1f2335]'
      }`}
      data-testid="strategy-card"
      data-strategy-id={row.strategy_id}
    >
      <CardHeader className="px-3 py-2.5 border-b border-[#1f2335]">
        <div className="flex justify-between items-start gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 mb-0.5">
              <span
                className="w-2.5 h-2.5 rounded-full inline-block shrink-0"
                style={{ backgroundColor: color }}
                aria-hidden="true"
              />
              <span className="font-semibold text-xs text-[#dde1ed] truncate">
                {row.name}
              </span>
            </div>
            <span className="mono text-[9.5px] text-[#7e8aaa] block truncate">
              {row.strategy_id} · v{row.version}
            </span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <StatusBadge status={row.status} />
            {row.is_running && implemented && (
              <span
                className="w-2 h-2 rounded-full bg-green-400 animate-pulse inline-block"
                title="Running live execution loop"
                aria-label="Running"
              />
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="px-3 py-2.5 space-y-2">
        {/* Top row: P&L headline */}
        <div className="flex items-baseline justify-between gap-2">
          <span className="text-[10px] uppercase tracking-wider text-[#5a637a] font-bold">
            Net P&amp;L
          </span>
          <PnlValue value={row.net_pnl} className="text-base" />
        </div>
        <div className="grid grid-cols-2 gap-1.5">
          <StatTile
            label="Realized"
            value={fmtUsdPlain(row.realized_pnl)}
            valueClass={row.realized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
          />
          <StatTile
            label="Unrealized"
            value={fmtUsdPlain(row.unrealized_pnl)}
            valueClass={row.unrealized_pnl >= 0 ? 'text-green-400' : 'text-red-400'}
          />
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          <StatTile
            label="Win Rate"
            value={fmtPct(row.win_rate)}
            valueClass={row.win_rate >= 0.5 ? 'text-green-400' : 'text-amber-400'}
          />
          <StatTile
            label="Profit Factor"
            value={row.profit_factor === null ? '—' : row.profit_factor.toFixed(2)}
            valueClass={
              row.profit_factor === null
                ? 'text-[#7e8aaa]'
                : row.profit_factor >= 1.5
                  ? 'text-green-400'
                  : row.profit_factor >= 1
                    ? 'text-amber-400'
                    : 'text-red-400'
            }
          />
          <StatTile
            label="Expectancy"
            value={fmtNum(row.expectancy)}
            valueClass={row.expectancy >= 0 ? 'text-green-400' : 'text-red-400'}
          />
        </div>
        <div className="grid grid-cols-3 gap-1.5">
          <StatTile
            label="Sharpe"
            value={fmtNum(row.sharpe_ratio)}
            valueClass={
              row.sharpe_ratio === null
                ? 'text-[#7e8aaa]'
                : row.sharpe_ratio >= 1.5
                  ? 'text-green-400'
                  : row.sharpe_ratio >= 0.5
                    ? 'text-amber-400'
                    : 'text-red-400'
            }
            hint="Annualised"
          />
          <StatTile
            label="Trades"
            value={`${row.closed_trades}`}
            hint={`${row.fills} fills`}
          />
          <StatTile
            label="Avg Hold"
            value={fmtHours(row.avg_hold_hours)}
            hint={row.open_trades > 0 ? `${row.open_trades} open` : 'no open'}
          />
        </div>

        {/* Toggle */}
        <div className="flex justify-between items-center pt-1.5 border-t border-[#1f2335] gap-2">
          <div className="flex items-center gap-1.5 min-w-0">
            <RiskLevelBadge level={row.risk_level} />
            <span className="text-[9.5px] text-[#7e8aaa] uppercase mono truncate">
              {row.category.replace('_', ' ')}
            </span>
          </div>
          <div className="flex items-center gap-1.5 shrink-0">
            <span
              className={`text-[9.5px] mono font-semibold ${
                row.is_enabled ? 'text-green-400' : 'text-[#7e8aaa]'
              }`}
            >
              {row.is_enabled ? 'Enabled' : 'Disabled'}
            </span>
            <Switch
              checked={row.is_enabled}
              onCheckedChange={(next) => onToggle(row.strategy_id, next)}
              disabled={!implemented || toggling}
              aria-label={`Toggle ${row.name}`}
              data-testid="strategy-toggle"
            />
          </div>
        </div>
        {!implemented && (
          <div className="text-[9px] text-[#5a637a] italic">
            Research stub — toggle disabled (no execution loop)
          </div>
        )}
      </CardContent>
    </Card>
  )
}

interface AttributionChartProps {
  rows: StrategyPerformanceRow[]
}

function AttributionChart({ rows }: AttributionChartProps) {
  const data = useMemo(
    () =>
      rows
        .map((r) => ({
          name: r.strategy_id,
          value: r.net_pnl,
          sub: `${r.closed_trades} closed · ${(r.win_rate * 100).toFixed(0)}% WR`,
        }))
        .sort((a, b) => b.value - a.value),
    [rows],
  )

  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-xs text-[#7e8aaa]">
        No closed positions yet — attribution will appear here once trades are realised.
      </div>
    )
  }

  return (
    <PnLBarChart
      data={data}
      height={220}
      layout="vertical"
      formatValue={(v) => (v >= 0 ? `+${v.toFixed(2)}` : `−${Math.abs(v).toFixed(2)}`)}
      formatTooltip={(d) => (
        <div style={tooltipStyle}>
          <div style={{ fontWeight: 600, marginBottom: 2 }}>{d.name}</div>
          <div>{d.value >= 0 ? '+' : '−'}${Math.abs(d.value).toFixed(2)}</div>
          {d.sub && <div style={{ fontSize: 11, opacity: 0.7 }}>{d.sub}</div>}
        </div>
      )}
    />
  )
}

interface EquityOverlayChartProps {
  rows: StrategyPerformanceRow[]
  colors: Record<string, string>
}

function EquityOverlayChart({ rows, colors }: EquityOverlayChartProps) {
  // Merge every strategy's equity curve into a single dataset keyed by
  // timestamp. Each strategy contributes one column (its strategy_id).
  // The chart pivots: each <Line> is one strategy.
  const { data, strategies } = useMemo(() => {
    // Collect all timestamps across all strategies (sorted unique union).
    const tsSet = new Set<number>()
    for (const r of rows) {
      for (const p of r.equity_curve ?? []) {
        tsSet.add(p.timestamp)
      }
    }
    const timestamps = Array.from(tsSet).sort((a, b) => a - b)
    // For each timestamp, walk each strategy and carry forward the last
    // known cumulative pnl (so a gap in one strategy's series doesn't
    // drop the line to 0).
    const lastSeen: Record<string, number> = {}
    const data = timestamps.map((ts) => {
      const row: Record<string, number> = { timestamp: ts }
      for (const r of rows) {
        const point = r.equity_curve?.find((p) => p.timestamp === ts)
        if (point) lastSeen[r.strategy_id] = point.pnl
        if (lastSeen[r.strategy_id] !== undefined) {
          row[r.strategy_id] = lastSeen[r.strategy_id]
        }
      }
      return row
    })
    return { data, strategies: rows.map((r) => r.strategy_id) }
  }, [rows])

  if (data.length === 0 || strategies.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-xs text-[#7e8aaa]">
        No equity data yet — curves populate as strategies close positions.
      </div>
    )
  }

  return (
    <ResponsiveContainer width="100%" height={260}>
      <LineChart data={data} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
        <CartesianGrid {...gridProps} />
        <XAxis
          {...axisProps}
          dataKey="timestamp"
          type="number"
          domain={['dataMin', 'dataMax']}
          tickFormatter={(ts) => fmtTime(ts as number)}
          minTickGap={48}
        />
        <YAxis
          {...axisProps}
          tickFormatter={(v) => (v >= 0 ? `+$${v.toFixed(0)}` : `−$${Math.abs(v).toFixed(0)}`)}
          width={56}
        />
        <Tooltip
          contentStyle={tooltipStyle}
          labelFormatter={(ts) => fmtTime(Number(ts))}
          formatter={(value: number, name: string) => [
            value >= 0 ? `+$${value.toFixed(2)}` : `−$${Math.abs(value).toFixed(2)}`,
            name,
          ]}
        />
        <Legend
          wrapperStyle={{ fontSize: 10, color: chartTheme.axis }}
          iconType="circle"
          iconSize={8}
        />
        {strategies.map((sid, i) => (
          <Line
            key={sid}
            type="monotone"
            dataKey={sid}
            stroke={colors[sid] ?? STRATEGY_COLORS[i % STRATEGY_COLORS.length]}
            strokeWidth={1.6}
            dot={false}
            activeDot={{ r: 3 }}
            isAnimationActive={true}
            animationDuration={400}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  )
}

interface PerformanceTableProps {
  rows: StrategyPerformanceRow[]
  colors: Record<string, string>
}

function PerformanceTable({ rows, colors }: PerformanceTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>('net_pnl')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  const sorted = useMemo(() => {
    const arr = [...rows]
    arr.sort((a, b) => {
      let av: string | number | null
      let bv: string | number | null
      if (sortKey === 'name') {
        av = a.name.toLowerCase()
        bv = b.name.toLowerCase()
      } else if (sortKey === 'status') {
        av = a.status
        bv = b.status
      } else {
        av = (a[sortKey] as number | null) ?? null
        bv = (b[sortKey] as number | null) ?? null
      }
      // Nulls always sort last regardless of direction.
      if (av === null && bv === null) return 0
      if (av === null) return 1
      if (bv === null) return -1
      if (typeof av === 'string' && typeof bv === 'string') {
        return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
      }
      const an = Number(av)
      const bn = Number(bv)
      return sortDir === 'asc' ? an - bn : bn - an
    })
    return arr
  }, [rows, sortKey, sortDir])

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    } else {
      setSortKey(key)
      setSortDir(key === 'name' || key === 'status' ? 'asc' : 'desc')
    }
  }

  const renderSortHeader = (k: SortKey, children: React.ReactNode, align: 'left' | 'right' = 'right') => (
    <TableHead
      className="cursor-pointer select-none hover:bg-[#1a1e2c] text-[10px] uppercase tracking-wider text-[#7e8aaa] font-bold"
      onClick={() => handleSort(k)}
      style={{ textAlign: align }}
    >
      <span className="inline-flex items-center gap-1">
        {children}
        {sortKey === k ? (
          sortDir === 'asc' ? <ArrowUp size={10} /> : <ArrowDown size={10} />
        ) : (
          <ArrowUpDown size={10} className="opacity-30" />
        )}
      </span>
    </TableHead>
  )

  return (
    <div className="max-h-96 overflow-y-auto scrollbar-thin" data-testid="performance-table">
      <Table>
        <TableHeader className="sticky top-0 bg-[#0e1015] z-10">
          <TableRow className="border-[#1f2335] hover:bg-transparent">
            {renderSortHeader('name', 'Strategy', 'left')}
            {renderSortHeader('status', 'Status', 'left')}
            {renderSortHeader('net_pnl', 'Net P&L')}
            {renderSortHeader('win_rate', 'Win %')}
            {renderSortHeader('profit_factor', 'PF')}
            {renderSortHeader('expectancy', 'Exp.')}
            {renderSortHeader('sharpe_ratio', 'Sharpe')}
            {renderSortHeader('sortino_ratio', 'Sortino')}
            {renderSortHeader('calmar_ratio', 'Calmar')}
            {renderSortHeader('max_drawdown', 'MaxDD')}
            {renderSortHeader('closed_trades', 'Trades')}
            {renderSortHeader('avg_hold_hours', 'Hold')}
          </TableRow>
        </TableHeader>
        <TableBody>
          {sorted.map((r) => {
            const color = colors[r.strategy_id] ?? '#7e8aaa'
            return (
              <TableRow
                key={r.strategy_id}
                className="border-[#1f2335] hover:bg-[#141724] text-xs"
                data-testid="performance-table-row"
              >
                <TableCell className="py-1.5 px-2">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="w-2 h-2 rounded-full inline-block shrink-0"
                      style={{ backgroundColor: color }}
                      aria-hidden="true"
                    />
                    <div className="min-w-0">
                      <div className="text-[11px] text-[#dde1ed] font-semibold truncate">
                        {r.name}
                      </div>
                      <div className="mono text-[9px] text-[#7e8aaa] truncate">
                        {r.strategy_id}
                      </div>
                    </div>
                  </div>
                </TableCell>
                <TableCell className="py-1.5 px-2">
                  <StatusBadge status={r.status} />
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono">
                  <PnlValue value={r.net_pnl} />
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono text-[#dde1ed]">
                  {fmtPct(r.win_rate, 0)}
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono text-[#dde1ed]">
                  {r.profit_factor === null ? '—' : r.profit_factor.toFixed(2)}
                </TableCell>
                <TableCell className={`py-1.5 px-2 text-right mono ${r.expectancy >= 0 ? 'text-green-400' : 'text-red-400'}`}>
                  {fmtNum(r.expectancy)}
                </TableCell>
                <TableCell className={`py-1.5 px-2 text-right mono ${r.sharpe_ratio === null ? 'text-[#7e8aaa]' : r.sharpe_ratio >= 1 ? 'text-green-400' : r.sharpe_ratio >= 0 ? 'text-amber-400' : 'text-red-400'}`}>
                  {fmtNum(r.sharpe_ratio)}
                </TableCell>
                <TableCell className={`py-1.5 px-2 text-right mono ${r.sortino_ratio === null ? 'text-[#7e8aaa]' : r.sortino_ratio >= 1 ? 'text-green-400' : r.sortino_ratio >= 0 ? 'text-amber-400' : 'text-red-400'}`}>
                  {fmtNum(r.sortino_ratio)}
                </TableCell>
                <TableCell className={`py-1.5 px-2 text-right mono ${r.calmar_ratio === null ? 'text-[#7e8aaa]' : r.calmar_ratio >= 1 ? 'text-green-400' : r.calmar_ratio >= 0 ? 'text-amber-400' : 'text-red-400'}`}>
                  {fmtNum(r.calmar_ratio)}
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono text-red-400">
                  {r.max_drawdown > 0 ? `−${r.max_drawdown.toFixed(2)}` : '—'}
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono text-[#dde1ed]">
                  {r.closed_trades}
                </TableCell>
                <TableCell className="py-1.5 px-2 text-right mono text-[#7e8aaa]">
                  {fmtHours(r.avg_hold_hours)}
                </TableCell>
              </TableRow>
            )
          })}
          {sorted.length === 0 && (
            <TableRow>
              <TableCell colSpan={12} className="text-center text-[#7e8aaa] py-6 text-xs">
                No strategies to display
              </TableCell>
            </TableRow>
          )}
        </TableBody>
      </Table>
    </div>
  )
}

interface RiskRankingPanelProps {
  rows: StrategyPerformanceRow[]
}

function RiskRankingPanel({ rows }: RiskRankingPanelProps) {
  const [metric, setMetric] = useState<RiskMetric>('sharpe')

  const ranked = useMemo(() => {
    const arr = rows.filter((r) => r.status === 'IMPLEMENTED' || r.closed_trades > 0)
    arr.sort((a, b) => {
      const av = (a[metric] as number | null) ?? null
      const bv = (b[metric] as number | null) ?? null
      if (av === null && bv === null) return 0
      if (av === null) return 1
      if (bv === null) return -1
      return bv - av
    })
    return arr
  }, [rows, metric])

  const activeMetric = RISK_METRICS.find((m) => m.id === metric)!

  return (
    <div data-testid="risk-ranking-panel">
      <div className="flex items-center gap-1.5 mb-2">
        {RISK_METRICS.map((m) => (
          <button
            key={m.id}
            onClick={() => setMetric(m.id)}
            className={`px-2 py-1 text-[10px] font-semibold rounded border transition-colors ${
              metric === m.id
                ? 'bg-cyan-500/15 text-cyan-300 border-cyan-500/40'
                : 'bg-[#0e1015] text-[#7e8aaa] border-[#1f2335] hover:border-[#2d3450]'
            }`}
            aria-pressed={metric === m.id}
            title={m.help}
          >
            {m.label}
          </button>
        ))}
      </div>
      <div className="text-[9.5px] text-[#5a637a] mb-2">{activeMetric.help}</div>
      <div className="space-y-1">
        {ranked.length === 0 && (
          <div className="text-xs text-[#7e8aaa] text-center py-4">
            No risk-ranked strategies yet — closes trades to populate metrics.
          </div>
        )}
        {ranked.map((r, i) => {
          const value = (r[metric] as number | null) ?? null
          const medal = i === 0 ? '🥇' : i === 1 ? '🥈' : i === 2 ? '🥉' : `${i + 1}.`
          const valCls =
            value === null
              ? 'text-[#7e8aaa]'
              : value >= 1
                ? 'text-green-400'
                : value >= 0
                  ? 'text-amber-400'
                  : 'text-red-400'
          return (
            <div
              key={r.strategy_id}
              className="flex items-center justify-between bg-[#0e1015] px-2.5 py-1.5 rounded border border-[#1f2335] hover:border-cyan-500/30 transition-colors text-xs"
              data-testid="risk-ranking-row"
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className="w-5 text-center text-xs font-bold shrink-0">{medal}</span>
                <span className="truncate font-semibold text-[#dde1ed] text-[11px]">
                  {r.name}
                </span>
                <span className="mono text-[9px] text-[#7e8aaa] truncate hidden md:inline">
                  {r.strategy_id}
                </span>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                <span className="text-[9.5px] text-[#7e8aaa] mono">
                  {r.closed_trades}T
                </span>
                <span className={`mono font-bold text-xs ${valCls}`}>
                  {value === null ? '—' : value.toFixed(2)}
                </span>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="skeleton h-12 w-full rounded-md" />
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2.5">
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="skeleton h-20 w-full rounded-md" />
        ))}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {Array.from({ length: 6 }).map((_, i) => (
          <div key={i} className="skeleton h-56 w-full rounded-md" />
        ))}
      </div>
      <div className="skeleton h-64 w-full rounded-md" />
      <div className="skeleton h-48 w-full rounded-md" />
    </div>
  )
}

function ErrorState({ message, onRetry, retrying }: { message: string; onRetry: () => void; retrying?: boolean }) {
  return (
    <div className="error-state p-8" role="alert">
      <AlertTriangle className="error-state-icon text-[#f87171]" size={28} aria-hidden="true" />
      <div className="error-state-title">Strategy performance endpoint unavailable</div>
      <div className="error-state-desc">{message}</div>
      <Button
        variant="outline"
        size="sm"
        onClick={onRetry}
        className="mt-2"
        disabled={retrying}
        aria-label="Retry strategy performance fetch"
      >
        <RefreshCw size={14} className={retrying ? 'animate-spin' : ''} />
        {retrying ? 'Retrying…' : 'Retry'}
      </Button>
    </div>
  )
}

// ────────────────────────────────────────────────────────────────────────────
// Main panel
// ────────────────────────────────────────────────────────────────────────────

export default function StrategyPerformancePanel() {
  const [data, setData] = useState<StrategyPerformanceResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [togglingId, setTogglingId] = useState<string | null>(null)
  const [toggleError, setToggleError] = useState<string | null>(null)

  const fetchPerformance = useCallback(async () => {
    try {
      const r = await apiFetch(PERF_ENDPOINT)
      if (r.ok) {
        const json = (await r.json()) as StrategyPerformanceResponse
        setData(json)
        setError(null)
      } else {
        setError(`GET ${PERF_ENDPOINT} → ${r.status} ${r.statusText}`)
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  // Initial fetch + 30s polling, paused when document hidden.
  useEffect(() => {
    fetchPerformance()
    let timer: ReturnType<typeof setInterval> | null = null
    const startPolling = () => {
      if (timer) return
      timer = setInterval(() => {
        if (typeof document !== 'undefined' && document.hidden) return
        fetchPerformance()
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
        fetchPerformance()
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
  }, [fetchPerformance])

  const handleToggle = useCallback(
    async (strategyId: string, next: boolean) => {
      setTogglingId(strategyId)
      setToggleError(null)
      try {
        const r = await apiFetch(TOGGLE_ENDPOINT, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ strategy_name: strategyId, enabled: next }),
        })
        if (!r.ok) {
          const body = await r.json().catch(() => null)
          const msg = body?.detail || `Toggle rejected (HTTP ${r.status})`
          setToggleError(msg)
        } else {
          // Refresh to pick up the new is_running state.
          await fetchPerformance()
        }
      } catch (e) {
        setToggleError(e instanceof Error ? e.message : String(e))
      } finally {
        setTogglingId(null)
      }
    },
    [fetchPerformance],
  )

  // Assign each strategy a stable color based on its position in the
  // response (so toggling between renders doesn't shuffle colors).
  const colorMap = useMemo(() => {
    const map: Record<string, string> = {}
    const rows = data?.strategies ?? []
    for (let i = 0; i < rows.length; i++) {
      map[rows[i].strategy_id] = STRATEGY_COLORS[i % STRATEGY_COLORS.length]
    }
    return map
  }, [data?.strategies])

  const rows = data?.strategies ?? []
  const activeRows = useMemo(
    () => rows.filter((r) => r.status === 'IMPLEMENTED' || r.is_running || r.closed_trades > 0),
    [rows],
  )
  const totalPnl = data?.total_pnl ?? 0
  const activeCount = data?.active_count ?? 0
  const implementedCount = data?.implemented_count ?? 0
  const plannedCount = data?.planned_count ?? 0

  if (loading && !data) {
    return (
      <div
        className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden"
        role="status"
        aria-live="polite"
        aria-label="Loading strategy performance…"
        data-testid="strategy-performance-panel"
      >
        <div className="card-header px-3.5 py-2.5 border-b border-[#1f2335] flex items-center gap-2 bg-[#0e1015]/80">
          <span className="spinner" aria-hidden="true" />
          <span className="text-xs font-bold text-[#dde1ed] tracking-wide">
            Loading Strategy Performance…
          </span>
        </div>
        <LoadingSkeleton />
      </div>
    )
  }

  if (error && !data) {
    return (
      <div
        className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden p-4"
        data-testid="strategy-performance-panel"
      >
        <ErrorState message={error} onRetry={fetchPerformance} />
      </div>
    )
  }

  return (
    <div
      className="flex flex-col h-full bg-[#13161e] border border-[#1f2335] rounded-lg overflow-hidden overflow-y-auto scrollbar-thin p-4 space-y-4"
      data-testid="strategy-performance-panel"
    >
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div className="flex flex-wrap justify-between items-center pb-2 border-b border-[#1f2335] gap-2">
        <div>
          <div className="flex items-center gap-2">
            <LayoutGrid size={18} className="text-cyan-400" aria-hidden="true" />
            <span className="text-sm font-bold text-[#dde1ed]">
              Strategy Performance Dashboard
            </span>
          </div>
          <p className="text-xs text-[#7e8aaa]">
            Per-strategy P&amp;L · win rate · Sharpe / Sortino / Calmar · equity overlay · risk-adjusted ranking
          </p>
        </div>
        <div className="flex items-center gap-2">
          <span className="badge badge-dim text-[9.5px]">30s poll</span>
          <Badge variant="secondary" className="text-[9.5px] py-0.5">
            {activeCount} active · {implementedCount} impl · {plannedCount} planned
          </Badge>
          <PnlValue value={totalPnl} className="text-sm" />
          <Button
            variant="outline"
            size="sm"
            onClick={fetchPerformance}
            className="h-7 px-2 text-xs border-[#1f2335] text-[#7e8aaa] hover:text-white hover:border-[#2d3450]"
            aria-label="Refresh strategy performance"
            disabled={togglingId !== null}
          >
            <RefreshCw size={12} />
            Refresh
          </Button>
        </div>
      </div>

      {/* ── Toggle error banner ─────────────────────────────────────────── */}
      {toggleError && (
        <div
          className="banner-danger text-xs py-2 px-3 flex items-center justify-between"
          role="alert"
        >
          <span className="flex items-center gap-1.5">
            <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />
            <span>
              <strong>Toggle failed:</strong> {toggleError}
            </span>
          </span>
          <button
            onClick={() => setToggleError(null)}
            className="hover:underline text-xs ml-2"
            aria-label="Dismiss toggle error"
          >
            Dismiss
          </button>
        </div>
      )}

      {/* ── Strategy Overview Cards ─────────────────────────────────────── */}
      <section>
        <div className="flex items-center gap-1.5 mb-2">
          <LayoutGrid size={12} className="text-[#7e8aaa]" aria-hidden="true" />
          <h2 className="text-xs font-bold text-[#dde1ed] uppercase tracking-wider">
            Strategy Overview
          </h2>
          <span className="text-[9.5px] text-[#7e8aaa]">
            ({activeRows.length} strategies with activity)
          </span>
        </div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3" data-testid="strategy-cards-grid">
          {activeRows.length === 0 && (
            <div className="col-span-full text-center text-xs text-[#7e8aaa] py-6 border border-dashed border-[#1f2335] rounded-md">
              No active strategies. Deploy a strategy from the Strategy Registry to see live metrics here.
            </div>
          )}
          {activeRows.map((r) => (
            <StrategyCard
              key={r.strategy_id}
              row={r}
              color={colorMap[r.strategy_id] ?? '#7e8aaa'}
              onToggle={handleToggle}
              toggling={togglingId === r.strategy_id}
            />
          ))}
        </div>
      </section>

      {/* ── Attribution + Risk-Adjusted Ranking ────────────────────────── */}
      <section className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        <Card className="lg:col-span-2 bg-[#13161e] border-[#1f2335]">
          <CardHeader className="px-4 py-3 border-b border-[#1f2335]">
            <div className="flex items-center gap-2">
              <BarChart3 size={14} className="text-cyan-400" aria-hidden="true" />
              <CardTitle className="text-xs font-bold text-[#dde1ed]">
                P&amp;L Attribution by Strategy
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="px-4 py-3" data-testid="attribution-chart">
            <AttributionChart rows={rows} />
          </CardContent>
        </Card>

        <Card className="bg-[#13161e] border-[#1f2335]">
          <CardHeader className="px-4 py-3 border-b border-[#1f2335]">
            <div className="flex items-center gap-2">
              <Trophy size={14} className="text-amber-400" aria-hidden="true" />
              <CardTitle className="text-xs font-bold text-[#dde1ed]">
                Risk-Adjusted Ranking
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="px-4 py-3">
            <RiskRankingPanel rows={rows} />
          </CardContent>
        </Card>
      </section>

      {/* ── Equity Curves Overlay ──────────────────────────────────────── */}
      <section>
        <Card className="bg-[#13161e] border-[#1f2335]">
          <CardHeader className="px-4 py-3 border-b border-[#1f2335]">
            <div className="flex items-center gap-2">
              <LineChartIcon size={14} className="text-green-400" aria-hidden="true" />
              <CardTitle className="text-xs font-bold text-[#dde1ed]">
                Equity Curves Overlay (cumulative P&amp;L)
              </CardTitle>
            </div>
          </CardHeader>
          <CardContent className="px-4 py-3" data-testid="equity-overlay-chart">
            <EquityOverlayChart rows={rows} colors={colorMap} />
          </CardContent>
        </Card>
      </section>

      {/* ── Performance Comparison Table ───────────────────────────────── */}
      <section>
        <Card className="bg-[#13161e] border-[#1f2335]">
          <CardHeader className="px-4 py-3 border-b border-[#1f2335]">
            <div className="flex items-center gap-2">
              <Table2 size={14} className="text-cyan-400" aria-hidden="true" />
              <CardTitle className="text-xs font-bold text-[#dde1ed]">
                Performance Comparison
              </CardTitle>
              <span className="text-[9.5px] text-[#7e8aaa] ml-auto">
                Click any column header to sort
              </span>
            </div>
          </CardHeader>
          <CardContent className="px-2 py-2">
            <PerformanceTable rows={rows} colors={colorMap} />
          </CardContent>
        </Card>
      </section>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between text-[9.5px] text-[#5a637a] pt-1 border-t border-[#1f2335]">
        <span className="flex items-center gap-1">
          <Power size={10} aria-hidden="true" />
          Toggle POST {TOGGLE_ENDPOINT} · metrics GET {PERF_ENDPOINT}
        </span>
        {data?.generated_at && (
          <span className="mono">Generated {fmtTime(data.generated_at)}</span>
        )}
      </div>
    </div>
  )
}
