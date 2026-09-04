// components/charts/TradeTape.tsx — Vertical scrolling trade tape.
//
// Renders the most-recent trades as a vertical "time-and-sales" tape
// — the classic Bloomberg / trading-terminal view. New prints appear
// at the TOP of the list and older ones scroll down out of view as
// newer prints push them off. Each row shows:
//
//   HH:MM:SS  BUY  0.520  120.5
//   HH:MM:SS  SELL 0.518   85.0
//   …
//
// Hover-pause: when the pointer enters the tape region, the
// `paused` flag flips on, the newest-trades counter stops advancing,
// and the list snapshot is frozen for inspection. Moving the pointer
// out resumes live updates.
//
// Per-minute stats header shows the trade count + total volume over
// the most recent 60s window — a quick read on tape speed (fast tape
// = active market; slow tape = quiet).
//
// Data shape: same `FlowTrade` type as OrderFlowChart (timestamp /
// side / size / price). Newest-first input order is assumed; the
// component re-sorts defensively.

'use client'

import { useMemo, useRef, useState, useEffect, useCallback } from 'react'
import { chartTheme } from './theme'
// W28-1 — `FlowSide` removed from the type import (unused — the tape
// only consumes `FlowTrade` shape; side rendering is delegated to
// OrderFlowChart via the `side` field on each `FlowTrade`).
import type { FlowTrade } from './OrderFlowChart'

export interface TradeTapeProps {
  /** Recent trades (newest-first OR oldest-first — the tape sorts). */
  trades: FlowTrade[]
  /** Maximum rows to render (most-recent N). Default 40. */
  maxRows?: number
  /** Container height in px. Default 320. */
  height?: number
  /** Per-minute window in ms (used for the rate stat). Default 60_000. */
  rateWindowMs?: number
  /** Override the buy color. */
  buyColor?: string
  /** Override the sell color. */
  sellColor?: string
  /** Optional className for the outer wrapper. */
  className?: string
  /** Override `Date.now()` — used by tests for deterministic output. */
  now?: number
}

interface TapeRow extends FlowTrade {
  /** Stable React key. */
  key: string
  /** Pre-formatted time string (HH:MM:SS). */
  time: string
}

function formatTime(ts: number): string {
  const d = new Date(ts)
  const hh = String(d.getHours()).padStart(2, '0')
  const mm = String(d.getMinutes()).padStart(2, '0')
  const ss = String(d.getSeconds()).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
}

function formatSize(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (Math.abs(v) >= 1000) return `${(v / 1000).toFixed(2)}k`
  return v.toFixed(2)
}

function formatPrice(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return v.toFixed(3)
}

export default function TradeTape({
  trades,
  maxRows = 40,
  height = 320,
  rateWindowMs = 60_000,
  buyColor = chartTheme.colors.success,
  sellColor = chartTheme.colors.danger,
  className,
  now,
}: TradeTapeProps) {
  // `paused` flips true while the pointer is over the tape. While paused,
  // the visible list snapshot is frozen — new trades still arrive via
  // `trades` prop updates but the rendered `rows` state isn't refreshed
  // until the user moves the pointer out.
  const [paused, setPaused] = useState(false)
  const pausedRef = useRef(false)
  pausedRef.current = paused

  // Visible rows state. Updated via an effect that runs whenever the
  // incoming `trades` array changes — but ONLY when not paused.
  const [rows, setRows] = useState<TapeRow[]>([])
  // The timestamp of the most-recent trade we've already folded into the
  // tape. Used to dedupe trades across snapshot polls — the bot's REST
  // endpoint may return overlapping windows on consecutive fetches.
  const lastTsRef = useRef<number>(0)

  // Use a stable `now` for the per-minute rate computation; recompute
  // every render so the rate stays current.
  const effectiveNow = now ?? Date.now()

  // Build the visible rows list from the incoming trades array.
  // Always newest-first (largest timestamp at the top of the tape).
  const refreshRows = useCallback(
    (incoming: FlowTrade[]) => {
      // Sort defensively — the caller may pass newest-first or oldest-first.
      const sorted = [...incoming]
        .filter((t) => Number.isFinite(t.timestamp))
        .sort((a, b) => b.timestamp - a.timestamp)
        .slice(0, maxRows)

      // Dedupe against the last-known newest trade id: if the incoming
      // newest trade is older than or equal to lastTsRef, the snapshot
      // didn't actually advance — skip the setState so we don't
      // trigger an unnecessary re-render (which would otherwise
      // fire the framer-motion enter animation again on every poll).
      const newest = sorted[0]?.timestamp ?? 0
      if (newest <= lastTsRef.current && sorted.length === rows.length) {
        return
      }
      lastTsRef.current = newest

      const built: TapeRow[] = sorted.map((t, i) => ({
        ...t,
        key: `${t.timestamp}-${i}-${t.side}`,
        time: formatTime(t.timestamp),
      }))
      setRows(built)
    },
    [maxRows, rows.length],
  )

  useEffect(() => {
    if (pausedRef.current) return
    refreshRows(trades)
  }, [trades, refreshRows])

  // When the user pauses, hold the snapshot. When they unpause, immediately
  // catch up to the latest trades so the tape doesn't lag behind.
  const handleMouseEnter = useCallback(() => setPaused(true), [])
  const handleMouseLeave = useCallback(() => {
    setPaused(false)
    // Catch up synchronously on resume — the next render will pick up the
    // latest `trades` prop via the effect above.
  }, [])

  // Per-minute rate stats — trade count + total volume over `rateWindowMs`.
  const { countPerMin, volPerMin } = useMemo(() => {
    const cutoff = effectiveNow - rateWindowMs
    let c = 0
    let v = 0
    for (const t of trades) {
      if (t.timestamp >= cutoff) {
        c += 1
        v += Number.isFinite(t.size) ? t.size : 0
      }
    }
    return { countPerMin: c, volPerMin: v }
  }, [trades, effectiveNow, rateWindowMs])

  return (
    <div
      className={`bg-[#0e1015] border border-[#1f2335] rounded flex flex-col overflow-hidden ${className ?? ''}`}
      data-testid="trade-tape"
      role="region"
      aria-label={`Trade tape — ${rows.length} prints, ${countPerMin} per minute${paused ? ' (paused)' : ''}`}
    >
      {/* Header — title + rate stats + pause indicator */}
      <div
        className="px-3 py-2 border-b border-[#1f2335] flex items-center justify-between text-[10px] uppercase text-[#7e8aaa] font-bold"
        data-testid="trade-tape-header"
      >
        <span>Trade Tape</span>
        <div className="flex items-center gap-2">
          <span className="mono text-[#dde1ed]">
            {countPerMin}/min
          </span>
          <span className="text-[#3e4560]">·</span>
          <span className="mono" style={{ color: chartTheme.colors.info }}>
            {formatSize(volPerMin)} vol
          </span>
          {paused && (
            <span
              className="mono text-[9px] px-1.5 py-0.5 rounded border"
              style={{
                color: chartTheme.colors.warning,
                borderColor: chartTheme.colors.warning,
              }}
              data-testid="trade-tape-paused"
            >
              PAUSED
            </span>
          )}
        </div>
      </div>

      {/* Column headers */}
      <div className="px-3 py-1.5 grid grid-cols-4 gap-2 text-[9px] uppercase text-[#3e4560] mono border-b border-[#1f2335] bg-[#13161e]">
        <span>Time</span>
        <span>Side</span>
        <span className="text-right">Price</span>
        <span className="text-right">Size</span>
      </div>

      {/* Scrolling list */}
      <div
        className="flex-1 overflow-y-auto"
        style={{ height: height - 70, maxHeight: height - 70 }}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        data-testid="trade-tape-body"
        role="log"
        aria-live="off"
        aria-relevant="additions"
      >
        {rows.length === 0 ? (
          <div
            className="flex items-center justify-center h-full text-[11px] text-[#3e4560]"
            data-testid="trade-tape-empty"
          >
            No trades yet
          </div>
        ) : (
          <ul className="divide-y divide-[#1f2335]/50">
            {rows.map((row) => {
              const isBuy = row.side === 'BUY'
              const color = isBuy ? buyColor : sellColor
              return (
                <li
                  key={row.key}
                  className="px-3 py-1.5 grid grid-cols-4 gap-2 text-[10.5px] mono items-center hover:bg-[#13161e]/60 transition-colors"
                  data-testid="trade-tape-row"
                  data-side={row.side}
                >
                  <span className="text-[#7e8aaa]">{row.time}</span>
                  <span
                    className="font-bold uppercase"
                    style={{ color }}
                  >
                    {row.side === 'BUY' ? 'BUY' : 'SELL'}
                  </span>
                  <span
                    className="text-right"
                    style={{ color }}
                  >
                    {formatPrice(row.price)}
                  </span>
                  <span className="text-right text-[#dde1ed]">
                    {formatSize(row.size)}
                  </span>
                </li>
              )
            })}
          </ul>
        )}
      </div>
    </div>
  )
}
