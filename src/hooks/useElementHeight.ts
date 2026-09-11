// hooks/useElementHeight.ts — Measure a container's pixel height with
// ResizeObserver so virtualized tables can size their viewport to fill
// the available space (no fixed `height` prop hardcoding, no scroll-
// inside-scroll / dead-space artefacts).
//
// W16-6 — added to support VirtualTable in panels with variable-height
// parents (TradesPanel, ClosedPositionsPanel, AuditLogPanel). All three
// use `h-full flex flex-col` parents, so the table area's height depends
// on the surrounding card header / KPI strip / filter bar heights —
// known only at runtime.
//
// Returns `[setRef, height]`:
//   • `setRef` — attach to the container whose height you want to track.
//   • `height` — current pixel height (0 until the first measurement
//     lands, which the consumer should treat as "render VirtualTable
//     with a sensible fallback until measured").
'use client'

import { useCallback, useEffect, useRef, useState } from 'react'

export function useElementHeight<T extends HTMLElement>() {
  const [height, setHeight] = useState(0)
  const observerRef = useRef<ResizeObserver | null>(null)

  const setRef = useCallback((node: T | null) => {
    // Tear down the previous observer if we're switching nodes.
    if (observerRef.current) {
      observerRef.current.disconnect()
      observerRef.current = null
    }
    if (!node) return
    // Initial measurement — also covers the case where ResizeObserver
    // fires late on first mount.
    setHeight(node.getBoundingClientRect().height)
    if (typeof ResizeObserver === 'undefined') return
    const obs = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const h = entry.contentRect.height
        setHeight((prev) => (Math.abs(prev - h) > 0.5 ? h : prev))
      }
    })
    obs.observe(node)
    observerRef.current = obs
  }, [])

  useEffect(() => {
    return () => {
      observerRef.current?.disconnect()
      observerRef.current = null
    }
  }, [])

  return [setRef, height] as const
}
