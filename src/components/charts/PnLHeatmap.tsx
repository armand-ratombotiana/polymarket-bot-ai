// components/charts/PnLHeatmap.tsx — Grid-based P&L heatmap.
//
// W16-1 — Real-time P&L heatmap visualisation. Renders one cell per open
// position; cell background colour encodes the sign + magnitude of P&L
// (green = profit, red = loss, neutral grey = break-even). The intensity
// is proportional to magnitude relative to the largest absolute P&L in
// the bucket so a single outlier doesn't swamp the rest.
//
// Construction:
//   • Pure CSS grid (no charting library needed). Each cell is a
//     <button> so keyboard focus + screen-reader semantics come for
//     free. The grid auto-flows; on narrow viewports the parent panel
//     collapses the grid into a stacked list (driven by the
//     `listLayout` prop — the PortfolioRiskPanel flips it on when the
//     viewport is <640px wide).
//   • Hover tooltip — a tiny absolutely-positioned popover anchored to
//     the cell. Uses native mouseenter/leave + focus/blur handlers so
//     keyboard users get the same tooltip on focus.
//   • Click a cell to expand a per-row detail strip below the grid
//     (token, position size, entry, current mark, P&L $, P&L %).
//     Clicking the same cell again collapses the detail strip.
//
// Data shape: each `PnLHeatmapDatum` carries enough fields to render
// without consulting the parent — the panel passes a fully-resolved
// array (the useBot snapshot's positions mapped to datums).
'use client'

import { useMemo, useState, useCallback, memo } from 'react'
import { fmtUsd, fmtPnl, fmtPct, fmtPrice } from '@/lib/design-tokens'

export interface PnLHeatmapDatum {
  /** Position token id (Polymarket CLOB token). */
  tokenId: string
  /** Human-readable market label (slug-derived question / event title). */
  label: string
  /** Side badge — "YES" / "NO" / "FLAT". */
  outcome: 'YES' | 'NO' | 'FLAT'
  /** Number of shares currently held (YES or NO). */
  shares: number
  /** Average entry price 0..1. */
  entryPrice: number
  /** Current mark price 0..1 (best mid). Null when book is missing. */
  currentPrice: number | null
  /** Position cost basis (USD). */
  positionSize: number
  /** Realised + unrealised P&L in USD (signed). */
  pnl: number
  /** P&L as a fraction of cost basis (signed). NaN when cost basis is 0. */
  pnlPct: number
}

export interface PnLHeatmapProps {
  /** Fully-resolved per-position datums. */
  data: PnLHeatmapDatum[]
  /** Override the intensity ceiling (default = max |pnl| in the bucket). */
  maxMagnitude?: number
  /** When true, render as a stacked list instead of a grid (mobile). */
  listLayout?: boolean
  /** Optional initial expanded token id (controlled-ish — defaults to none). */
  initialExpandedTokenId?: string
  /** Cell height (px). Default 64. */
  cellHeight?: number
  /** Min cell width (px) — drives the grid's auto-fill column count. */
  cellMinWidth?: number
  /** Optional className passthrough. */
  className?: string
}

// ── Colour helpers ────────────────────────────────────────────────────────
// Intensity is in [0,1]. We lerp from a faint base (alpha 0.10) to the
// saturated endpoint (alpha 0.85). The green/red endpoints match the
// dashboard's design tokens (chartTheme.colors.success / danger) so the
// heatmap matches the rest of the workstation's visual language.
const PROFIT_RGB = '34, 197, 94' // #22c55e (design-tokens.colors.green)
const LOSS_RGB = '239, 68, 68' // #ef4444 (design-tokens.colors.red)
const NEUTRAL_RGB = '126, 138, 170' // #7e8aaa (design-tokens.colors.textSecondary)

function cellBackground(pnl: number, intensity: number): string {
  if (!Number.isFinite(pnl) || pnl === 0) {
    return `rgba(${NEUTRAL_RGB}, 0.15)`
  }
  const clamped = Math.max(0, Math.min(1, intensity))
  const alpha = 0.15 + clamped * 0.70 // 0.15 → 0.85
  const rgb = pnl > 0 ? PROFIT_RGB : LOSS_RGB
  return `rgba(${rgb}, ${alpha.toFixed(3)})`
}

function cellBorder(pnl: number, intensity: number): string {
  if (!Number.isFinite(pnl) || pnl === 0) {
    return `1px solid rgba(${NEUTRAL_RGB}, 0.30)`
  }
  const clamped = Math.max(0, Math.min(1, intensity))
  const rgb = pnl > 0 ? PROFIT_RGB : LOSS_RGB
  const alpha = 0.30 + clamped * 0.55
  return `1px solid rgba(${rgb}, ${alpha.toFixed(3)})`
}

function textColor(pnl: number): string {
  if (pnl > 0) return '#4ade80' // design-tokens.colors.greenFg
  if (pnl < 0) return '#f87171' // design-tokens.colors.redFg
  return '#7e8aaa' // design-tokens.colors.textSecondary
}

interface CellProps {
  datum: PnLHeatmapDatum
  intensity: number
  expanded: boolean
  onToggle: (tokenId: string) => void
  height: number
  listLayout: boolean
}

// Cell is memoised so only the cell whose datum / intensity / expanded
// state actually changed re-renders when the parent's `data` array
// mutates (e.g. a new snapshot arrives from useBot).
const HeatmapCell = memo(function HeatmapCell({
  datum,
  intensity,
  expanded,
  onToggle,
  height,
  listLayout,
}: CellProps) {
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)

  const handleEnter = useCallback(() => setHovered(true), [])
  const handleLeave = useCallback(() => setHovered(false), [])
  const handleFocus = useCallback(() => setFocused(true), [])
  const handleBlur = useCallback(() => setFocused(false), [])
  const handleClick = useCallback(() => onToggle(datum.tokenId), [datum.tokenId, onToggle])

  const bg = cellBackground(datum.pnl, intensity)
  const border = cellBorder(datum.pnl, intensity)
  const showTooltip = hovered || focused

  return (
    <div
      data-testid="pnl-heatmap-cell-wrapper"
      className="relative"
      style={listLayout ? { width: '100%' } : undefined}
    >
      <button
        type="button"
        data-testid={`pnl-heatmap-cell-${datum.tokenId}`}
        data-token-id={datum.tokenId}
        data-pnl-sign={datum.pnl > 0 ? 'positive' : datum.pnl < 0 ? 'negative' : 'neutral'}
        onClick={handleClick}
        onMouseEnter={handleEnter}
        onMouseLeave={handleLeave}
        onFocus={handleFocus}
        onBlur={handleBlur}
        aria-label={`P&L for ${datum.label}: ${fmtPnl(datum.pnl)} (${fmtPct(datum.pnlPct)}). Press Enter to ${expanded ? 'collapse' : 'expand'} details.`}
        aria-expanded={expanded}
        className={`relative w-full text-left rounded-md transition-all focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-400 ${expanded ? 'ring-1 ring-cyan-400/60' : ''}`}
        style={{
          background: bg,
          border,
          height: listLayout ? 'auto' : `${height}px`,
          minHeight: listLayout ? '48px' : undefined,
          padding: listLayout ? '8px 10px' : '6px 8px',
        }}
      >
        <div
          className={listLayout ? 'flex items-center justify-between gap-2' : 'flex flex-col items-stretch gap-0.5 h-full'}
        >
          <span
            className="text-[10px] font-bold uppercase tracking-wide truncate"
            style={{ color: '#dde1ed' }}
            title={datum.label}
          >
            {datum.outcome} · {datum.label}
          </span>
          <span
            className="mono font-bold"
            style={{
              color: textColor(datum.pnl),
              fontSize: listLayout ? '12px' : '11px',
            }}
          >
            {fmtPnl(datum.pnl)}
          </span>
        </div>
      </button>

      {showTooltip && (
        <div
          role="tooltip"
          data-testid={`pnl-heatmap-tooltip-${datum.tokenId}`}
          className="absolute z-30 bottom-full left-1/2 -translate-x-1/2 mb-1 pointer-events-none"
          style={{
            background: '#13161e',
            border: '1px solid #1f2335',
            borderRadius: '6px',
            boxShadow: '0 4px 12px rgba(0,0,0,0.35)',
            padding: '6px 10px',
            minWidth: '180px',
            fontSize: '11px',
            color: '#dde1ed',
          }}
        >
          <TooltipRow label="Token" value={datum.tokenId} mono />
          <TooltipRow label="Position" value={fmtUsd(datum.positionSize)} mono />
          <TooltipRow label="Entry" value={fmtPrice(datum.entryPrice)} mono />
          <TooltipRow
            label="Current"
            value={datum.currentPrice == null ? '—' : fmtPrice(datum.currentPrice)}
            mono
          />
          <TooltipRow
            label="P&L $"
            value={fmtPnl(datum.pnl)}
            mono
            valueColor={textColor(datum.pnl)}
          />
          <TooltipRow
            label="P&L %"
            value={Number.isFinite(datum.pnlPct) ? fmtPct(datum.pnlPct) : '—'}
            mono
            valueColor={textColor(datum.pnl)}
          />
        </div>
      )}
    </div>
  )
})

interface TooltipRowProps {
  label: string
  value: string
  mono?: boolean
  valueColor?: string
}

function TooltipRow({ label, value, mono, valueColor }: TooltipRowProps) {
  return (
    <div className="flex items-center justify-between gap-3 py-0.5">
      <span style={{ color: '#7e8aaa' }}>{label}</span>
      <span
        className={mono ? 'mono' : ''}
        style={{ fontWeight: 600, color: valueColor ?? '#dde1ed' }}
      >
        {value}
      </span>
    </div>
  )
}

export interface ExpandedDetailProps {
  datum: PnLHeatmapDatum
}

function ExpandedDetail({ datum }: ExpandedDetailProps) {
  return (
    <div
      data-testid={`pnl-heatmap-detail-${datum.tokenId}`}
      className="mt-2 p-3 rounded-md bg-[#0e1015] border border-[#1f2335]"
    >
      <div className="grid grid-cols-2 md:grid-cols-3 gap-3 text-xs">
        <DetailField label="Token ID" value={datum.tokenId} mono />
        <DetailField label="Market" value={datum.label} />
        <DetailField label="Outcome" value={datum.outcome} />
        <DetailField label="Shares" value={datum.shares.toFixed(2)} mono />
        <DetailField
          label="Entry Price"
          value={fmtPrice(datum.entryPrice)}
          mono
        />
        <DetailField
          label="Current Price"
          value={datum.currentPrice == null ? '—' : fmtPrice(datum.currentPrice)}
          mono
        />
        <DetailField
          label="Position Size"
          value={fmtUsd(datum.positionSize)}
          mono
        />
        <DetailField
          label="P&L ($)"
          value={fmtPnl(datum.pnl)}
          mono
          valueColor={textColor(datum.pnl)}
        />
        <DetailField
          label="P&L (%)"
          value={Number.isFinite(datum.pnlPct) ? fmtPct(datum.pnlPct) : '—'}
          mono
          valueColor={textColor(datum.pnl)}
        />
      </div>
    </div>
  )
}

interface DetailFieldProps {
  label: string
  value: string
  mono?: boolean
  valueColor?: string
}

function DetailField({ label, value, mono, valueColor }: DetailFieldProps) {
  return (
    <div className="flex flex-col gap-0.5">
      <span className="text-[9.5px] uppercase font-semibold tracking-wide text-[#7e8aaa]">
        {label}
      </span>
      <span
        className={mono ? 'mono' : ''}
        style={{ color: valueColor ?? '#dde1ed', fontWeight: 600, fontSize: '11.5px' }}
      >
        {value}
      </span>
    </div>
  )
}

function PnLHeatmapImpl({
  data,
  maxMagnitude,
  listLayout = false,
  initialExpandedTokenId,
  cellHeight = 64,
  cellMinWidth = 140,
  className,
}: PnLHeatmapProps) {
  const [expandedTokenId, setExpandedTokenId] = useState<string | null>(initialExpandedTokenId ?? null)

  const effectiveMax = useMemo(() => {
    if (typeof maxMagnitude === 'number' && Number.isFinite(maxMagnitude) && maxMagnitude > 0) {
      return maxMagnitude
    }
    const m = data.reduce((acc, d) => Math.max(acc, Math.abs(d.pnl)), 0)
    return m > 0 ? m : 1
  }, [data, maxMagnitude])

  const handleToggle = useCallback((tokenId: string) => {
    setExpandedTokenId((prev) => (prev === tokenId ? null : tokenId))
  }, [])

  if (!data || data.length === 0) {
    return (
      <div
        data-testid="pnl-heatmap-empty"
        className="flex items-center justify-center text-xs text-[#7e8aaa]"
        style={{ minHeight: '120px' }}
      >
        No open positions to render.
      </div>
    )
  }

  const expandedDatum = expandedTokenId
    ? data.find((d) => d.tokenId === expandedTokenId) ?? null
    : null

  // CSS grid template — auto-fill so the grid is responsive without JS
  // measuring the container. When `listLayout` is true we drop to a
  // single-column stack.
  const gridTemplate = listLayout
    ? '1fr'
    : `repeat(auto-fill, minmax(${cellMinWidth}px, 1fr))`

  return (
    <div
      data-testid="pnl-heatmap"
      className={className}
    >
      <div
        className={listLayout ? 'flex flex-col gap-2' : 'grid gap-2'}
        style={listLayout ? undefined : { gridTemplateColumns: gridTemplate }}
      >
        {data.map((datum) => {
          const intensity = Math.abs(datum.pnl) / effectiveMax
          const expanded = expandedTokenId === datum.tokenId
          return (
            <HeatmapCell
              key={datum.tokenId}
              datum={datum}
              intensity={intensity}
              expanded={expanded}
              onToggle={handleToggle}
              height={cellHeight}
              listLayout={listLayout}
            />
          )
        })}
      </div>

      {expandedDatum && <ExpandedDetail datum={expandedDatum} />}

      {/* Legend — green → grey → red gradient strip with anchor labels. */}
      <div
        data-testid="pnl-heatmap-legend"
        className="mt-3 flex items-center gap-2 text-[9.5px] text-[#7e8aaa]"
      >
        <span>Loss</span>
        <div
          className="h-2 flex-1 rounded"
          style={{
            background:
              `linear-gradient(to right, rgba(${LOSS_RGB}, 0.85), rgba(${NEUTRAL_RGB}, 0.30) 50%, rgba(${PROFIT_RGB}, 0.85))`,
          }}
          aria-hidden="true"
        />
        <span>Profit</span>
      </div>
    </div>
  )
}

const PnLHeatmap = memo(PnLHeatmapImpl)
export default PnLHeatmap
