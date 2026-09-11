// components/charts/theme.ts — Recharts theme matching the dashboard dark mode.
//
// Centralises all chart colors so every chart in the dashboard reads from a
// single source of truth. The dashboard surface uses dark `#13161e` cards on
// `#080910` background, so axis/grid/tooltip defaults are tuned for that
// aesthetic. Charts can opt into light mode by overriding the relevant
// `--chart-*` CSS variables at a parent, or by passing their own `colors`.
//
// Usage:
//   import { chartTheme, tooltipStyle, axisProps, gridProps } from './theme'
//   <AreaChart data={data}>
//     <CartesianGrid {...gridProps} />
//     <XAxis {...axisProps} dataKey="x" />
//     <YAxis {...axisProps} />
//     <Tooltip contentStyle={tooltipStyle} />
//     <Area stroke={chartTheme.colors.primary} />
//   </AreaChart>

export interface ChartTheme {
  colors: {
    primary: string
    success: string
    danger: string
    warning: string
    info: string
    muted: string
    // Light-theme mirrors — used when the chart container is inside a
    // `.light` ancestor or when the caller explicitly opts into light mode.
    primaryLight: string
    successLight: string
    dangerLight: string
    warningLight: string
    infoLight: string
    mutedLight: string
  }
  grid: string
  gridLight: string
  axis: string
  axisLight: string
  tooltip: {
    background: string
    border: string
    text: string
    backgroundLight: string
    borderLight: string
    textLight: string
  }
}

export const chartTheme: ChartTheme = {
  colors: {
    primary: '#3b82f6',
    success: '#10b981',
    danger: '#ef4444',
    warning: '#f59e0b',
    info: '#06b6d4',
    muted: '#6b7280',
    primaryLight: '#2563eb',
    successLight: '#059669',
    dangerLight: '#dc2626',
    warningLight: '#d97706',
    infoLight: '#0891b2',
    mutedLight: '#4b5563',
  },
  grid: 'rgba(255,255,255,0.05)',
  gridLight: 'rgba(15,23,42,0.08)',
  axis: '#8b949e',
  axisLight: '#475569',
  tooltip: {
    background: '#13161e',
    border: '#1f2335',
    text: '#e6edf3',
    backgroundLight: '#ffffff',
    borderLight: '#e2e8f0',
    textLight: '#0f172a',
  },
}

// Convenience style object for <Tooltip contentStyle={...} />
export const tooltipStyle = {
  backgroundColor: chartTheme.tooltip.background,
  border: `1px solid ${chartTheme.tooltip.border}`,
  borderRadius: '6px',
  color: chartTheme.tooltip.text,
  fontSize: '12px',
  boxShadow: '0 4px 12px rgba(0,0,0,0.35)',
  padding: '6px 10px',
} as const

// Light-mode variant — used when the host panel knows it's rendering on a
// white background (e.g. an exported report).
export const tooltipStyleLight = {
  backgroundColor: chartTheme.tooltip.backgroundLight,
  border: `1px solid ${chartTheme.tooltip.borderLight}`,
  borderRadius: '6px',
  color: chartTheme.tooltip.textLight,
  fontSize: '12px',
  padding: '6px 10px',
} as const

// Spread these props onto Recharts axis components for consistent styling.
export const axisProps = {
  tick: { fill: chartTheme.axis, fontSize: 10 },
  tickLine: { stroke: chartTheme.axis, strokeWidth: 0.5 } as const,
  axisLine: { stroke: chartTheme.axis, strokeWidth: 0.5 } as const,
} as const

// Spread onto <CartesianGrid strokeDasharray="3 3" {...gridProps} />
export const gridProps = {
  stroke: chartTheme.grid,
  strokeDasharray: '3 3',
} as const

// Cursor for Tooltip — a subtle dashed line on hover.
export const tooltipCursor = {
  stroke: chartTheme.colors.info,
  strokeWidth: 1,
  strokeDasharray: '3 3',
  strokeOpacity: 0.45,
} as const

// Helper: pick a color by P&L sign — used by PnLBarChart and others.
export function pnlColor(value: number): string {
  if (value > 0) return chartTheme.colors.success
  if (value < 0) return chartTheme.colors.danger
  return chartTheme.colors.muted
}

// Helper: pick a status color for utilization thresholds (0–100).
export function utilizationColor(pct: number): string {
  if (pct > 80) return chartTheme.colors.danger
  if (pct >= 50) return chartTheme.colors.warning
  return chartTheme.colors.success
}
