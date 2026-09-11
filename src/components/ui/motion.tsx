// src/components/ui/motion.tsx — Reusable Framer Motion wrappers.
// W10-8 — UI polish layer. All components are client-side only
// (Framer Motion touches `window`/`requestAnimationFrame` on mount, so
// importing these from a server component would crash the build).
//
// Design rules followed (per W10-8 spec):
//  - Subtle durations only: 0.15–0.25s. Anything longer feels sluggish on a
//    real-time trading dashboard where the user switches panels frequently.
//  - Animate transform + opacity only — never width/height/top/left, which
//    trigger layout reflow and jank on rapid snapshot updates.
//  - Every motion.* component carries `'use client'` via this file's
//    directive at the top. Consumers don't need to repeat the directive.
'use client'

import { motion, AnimatePresence } from 'framer-motion'
import type { CSSProperties, ReactNode } from 'react'

// ───────────────────────────────────────────────────────────────────────────
// FadeIn — subtle fade + 8px rise. The default panel-transition wrapper.
// `key` should be set by the caller to the active panel id so AnimatePresence
// can detect the swap and animate out → in.
//
// The motion.div fills its flex parent (the `.page-area` column) via
// `flex:1; min-height:0; display:flex; flex-direction:column` so child
// panels that use `height:100%` (e.g. `.command-center-layout`,
// `<div style={{height:'100%'}}>` wrappers) keep working unchanged.
// `min-height:0` is the standard fix for "flex child with overflow: hidden
// inside a flex column" — without it the child's intrinsic min-content
// height would prevent it from shrinking and `overflow:hidden` would clip
// incorrectly.
// ───────────────────────────────────────────────────────────────────────────
export function FadeIn({
  children,
  delay = 0,
  className,
  style,
}: {
  children: ReactNode
  delay?: number
  className?: string
  style?: CSSProperties
}) {
  return (
    <motion.div
      className={className}
      style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column', ...style }}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: -4 }}
      transition={{ duration: 0.2, delay, ease: 'easeOut' }}
    >
      {children}
    </motion.div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// SlideIn — for side panels / modals / drawers that enter from a screen
// edge. The `direction` controls the off-screen start position; the exit
// reverses the same axis so the panel "leaves the way it came in".
// Default distance (300px) matches the typical sidebar/drawer width.
// ───────────────────────────────────────────────────────────────────────────
export function SlideIn({
  children,
  direction = 'right',
  className,
}: {
  children: ReactNode
  direction?: 'left' | 'right' | 'up' | 'down'
  className?: string
}) {
  const x = direction === 'left' ? -300 : direction === 'right' ? 300 : 0
  const y = direction === 'up' ? -300 : direction === 'down' ? 300 : 0
  return (
    <motion.div
      className={className}
      initial={{ opacity: 0, x, y }}
      animate={{ opacity: 1, x: 0, y: 0 }}
      exit={{ opacity: 0, x, y }}
      transition={{ duration: 0.25, ease: 'easeInOut' }}
    >
      {children}
    </motion.div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// AnimatedListItem — for table rows / list items. Staggers each item by
// `index * 20ms` up to a 0.3s ceiling so a 200-row table doesn't take 4
// seconds to animate in. Animates x instead of y so it reads as "items
// slotting into place" rather than "items dropping from above".
// ───────────────────────────────────────────────────────────────────────────
export function AnimatedListItem({
  children,
  index,
}: {
  children: ReactNode
  index: number
}) {
  return (
    <motion.div
      initial={{ opacity: 0, x: -10 }}
      animate={{ opacity: 1, x: 0 }}
      transition={{ duration: 0.15, delay: Math.min(index * 0.02, 0.3) }}
    >
      {children}
    </motion.div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// Pulse — gentle opacity oscillation for "live"/"connecting"/"loading"
// states. 1.5s cycle mirrors the existing `.skeleton-shimmer` cadence so
// motion feels cohesive.
// ───────────────────────────────────────────────────────────────────────────
export function Pulse({ children }: { children: ReactNode }) {
  return (
    <motion.div
      animate={{ opacity: [0.5, 1, 0.5] }}
      transition={{ duration: 1.5, repeat: Infinity, ease: 'easeInOut' }}
    >
      {children}
    </motion.div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// StaggerContainer — wraps a list of AnimatedListItem siblings so they
// reveal sequentially. Children should use the `variants` prop OR be
// AnimatedListItem instances; this container just sets the orchestration.
// 30ms stagger is short enough that 20 items finish in ~0.6s.
// ───────────────────────────────────────────────────────────────────────────
export function StaggerContainer({ children }: { children: ReactNode }) {
  return (
    <motion.div
      initial="hidden"
      animate="visible"
      variants={{
        hidden: { opacity: 0 },
        visible: { opacity: 1, transition: { staggerChildren: 0.03 } },
      }}
    >
      {children}
    </motion.div>
  )
}

// ───────────────────────────────────────────────────────────────────────────
// NumberTicker — for KPI / stat values that change in place. Re-keys on
// `value` change so Framer treats each new value as a fresh mount → the
// `initial` fade-up runs again, drawing the eye to the updated figure
// without a heavy counting animation.
// `format` is called with the raw number so callers control formatting
// (currency, %, BPS, etc.) — keeps this component presentation-pure.
// ───────────────────────────────────────────────────────────────────────────
export function NumberTicker({
  value,
  format,
}: {
  value: number
  format?: (n: number) => string
}) {
  return (
    <motion.span
      key={value}
      initial={{ opacity: 0.5, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.2 }}
    >
      {format ? format(value) : value}
    </motion.span>
  )
}

// Re-export AnimatePresence so callers don't need a second import line.
// `mode="wait"` is the canonical pattern for panel swaps — old content
// fully exits before new content begins entering, avoiding overlap.
export { AnimatePresence }
