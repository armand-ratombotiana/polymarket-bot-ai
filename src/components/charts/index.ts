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

// W15-1 — Market depth + price history charts.
export { default as MarketDepthChart } from './MarketDepthChart'
export type { MarketDepthChartProps, DepthLevel } from './MarketDepthChart'

export { default as PriceHistoryChart } from './PriceHistoryChart'
export type {
  PriceHistoryChartProps,
  PriceHistoryBar,
  HistoryResolution,
} from './PriceHistoryChart'

// W16-1 — Risk matrix charts.
export { default as PnLHeatmap } from './PnLHeatmap'
export type { PnLHeatmapProps, PnLHeatmapDatum } from './PnLHeatmap'

export { default as CorrelationMatrix } from './CorrelationMatrix'
export type {
  CorrelationMatrixProps,
  CorrelationMatrixPayload,
} from './CorrelationMatrix'

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
