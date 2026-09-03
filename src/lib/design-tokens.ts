// lib/design-tokens.ts — TypeScript mirror of CSS design tokens.
// Use these constants in dynamic styles (style={{}} props) that cannot
// use CSS custom properties directly.

export const colors = {
  bgBase:    '#080910',
  bgSurface: '#0e1015',
  bgCard:    '#13161e',
  bgHover:   '#1a1f2e',

  border:    '#1f2335',
  borderFocus: '#3b82f6',

  textPrimary:   '#dde1ed',
  textSecondary: '#7e8aaa',
  textDim:       '#3e4560',
  textMono:      '#c8cfe0',

  green:    '#22c55e',
  greenFg:  '#4ade80',
  red:      '#ef4444',
  redFg:    '#f87171',
  amber:    '#f59e0b',
  amberFg:  '#fbbf24',
  blue:     '#3b82f6',
  blueFg:   '#60a5fa',
  cyan:     '#06b6d4',
  cyanFg:   '#22d3ee',
  purple:   '#a855f7',
  purpleFg: '#c084fc',
} as const

export const mode = {
  paper:    { label: 'PAPER',    color: '#f59e0b', bg: 'rgba(245,158,11,0.10)',  border: 'rgba(245,158,11,0.30)' },
  live:     { label: 'LIVE',     color: '#ef4444', bg: 'rgba(239,68,68,0.10)',   border: 'rgba(239,68,68,0.35)' },
  shadow:   { label: 'SHADOW',   color: '#06b6d4', bg: 'rgba(6,182,212,0.10)',   border: 'rgba(6,182,212,0.25)' },
  backtest: { label: 'BACKTEST', color: '#a855f7', bg: 'rgba(168,85,247,0.10)',  border: 'rgba(168,85,247,0.25)' },
} as const

export type TradingMode = keyof typeof mode

export const status = {
  healthy:     { label: 'Healthy',     color: '#22c55e', dotClass: 'healthy' },
  degraded:    { label: 'Degraded',    color: '#f59e0b', dotClass: 'degraded' },
  unavailable: { label: 'Unavailable', color: '#ef4444', dotClass: 'unavailable' },
  stale:       { label: 'Stale',       color: '#f59e0b', dotClass: 'stale' },
  unknown:     { label: 'Unknown',     color: '#3e4560', dotClass: 'unknown' },
  disabled:    { label: 'Disabled',    color: '#3e4560', dotClass: 'unknown' },
  experimental:{ label: 'Experimental',color: '#a855f7', dotClass: 'unknown' },
} as const

export type StatusKey = keyof typeof status

/** Format a USDC value with the given precision. Returns '—' for null/undefined. */
export function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const sign = v < 0 ? '−' : ''
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

/** Format a P&L value with leading +/− sign. Returns '—' for null/undefined. */
export function fmtPnl(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const sign = v >= 0 ? '+' : '−'
  return `${sign}$${Math.abs(v).toLocaleString('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`
}

/** Format a price (probability) 0–1 as a 3dp decimal. Returns '—' for null/undefined. */
export function fmtPrice(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

/** Format a number as percentage. Returns '—' for null/undefined. */
export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return `${(v * 100).toFixed(digits)}%`
}

/** Format an integer count. Returns '—' for null/undefined. */
export function fmtInt(v: number | null | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  return Math.round(v).toLocaleString('en-US')
}

/** Seconds since epoch → "Xs ago" / "Xm ago" freshness string. */
export function fmtAge(epochSec: number | null | undefined): string {
  if (epochSec == null) return '—'
  const diff = Date.now() / 1000 - epochSec
  if (diff < 0) return 'just now'
  if (diff < 60) return `${Math.floor(diff)}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  return `${Math.floor(diff / 3600)}h ago`
}

/** Return CSS freshness class based on age in seconds. */
export function freshnessClass(epochSec: number | null | undefined, staleThreshSec = 30, deadThreshSec = 120): string {
  if (epochSec == null) return 'freshness-dead'
  const age = Date.now() / 1000 - epochSec
  if (age < 10) return 'freshness-fresh'
  if (age < staleThreshSec) return 'freshness-ok'
  if (age < deadThreshSec) return 'freshness-stale'
  return 'freshness-dead'
}

/** Format epoch seconds as HH:MM:SS UTC. */
export function fmtTime(epochSec: number | null | undefined): string {
  if (epochSec == null) return '—'
  return new Date(epochSec * 1000).toISOString().slice(11, 19) + ' UTC'
}

/** Format uptime seconds as HH:MM:SS. */
export function fmtUptime(s: number): string {
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(sec).padStart(2, '0')}`
}
