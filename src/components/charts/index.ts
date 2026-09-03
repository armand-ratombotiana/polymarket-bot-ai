// components/charts/index.ts — Barrel export for chart components + theme.
//
// Importers should prefer the named imports from this barrel for tree-shaking
// and discoverability:
//
//   import { EquityCurveChart, PnLBarChart, Sparkline, GaugeChart, ReliabilityDiagram, chartTheme } from '@/components/charts'

export { default as EquityCurveChart } from './EquityCurveChart'
export type { EquityCurveChartProps, EquityCurvePoint } from './EquityCurveChart'

export { default as PnLBarChart } from './PnLBarChart'
export type { PnLBarChartProps, PnLBarDatum } from './PnLBarChart'

export { default as Sparkline } from './Sparkline'
export type { SparklineProps } from './Sparkline'

export { default as GaugeChart } from './GaugeChart'
export type { GaugeChartProps } from './GaugeChart'

export { default as ReliabilityDiagram } from './ReliabilityDiagram'
export type { ReliabilityDiagramProps, ReliabilityPoint } from './ReliabilityDiagram'

export {
  chartTheme,
  tooltipStyle,
  tooltipStyleLight,
  axisProps,
  gridProps,
  tooltipCursor,
  pnlColor,
  utilizationColor,
} from './theme'
export type { ChartTheme } from './theme'
