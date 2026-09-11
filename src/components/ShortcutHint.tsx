// components/ShortcutHint.tsx — W17-6 Floating "?" hint button.
//
// Renders a small circular button pinned to the bottom-right corner
// of the viewport that opens the KeyboardCheatSheet. A tooltip on
// hover announces "Press ? for keyboard shortcuts" so the trader
// learns the `?` shortcut by interacting with the affordance.
//
// Why a separate component (and not just a button in TopStatusBar):
//   * The TopStatusBar is already dense (mode pill, kill switch,
//     latency, freshness, ML, balance, P&L, theme, locale, mute,
//     shortcuts, config, cancel-all, kill/resume). Adding one more
//     icon dilutes the action cluster.
//   * The bottom-right corner is the universal "help / FAB" slot —
//     the trader's eye lands there when they're looking for help
//     the same way they look for the kill switch in the top-right
//     corner.
//   * Floating the button over the panel area means it stays visible
//     regardless of which nav section is active (the TopStatusBar is
//     sticky but the page-area scrolls under it).
//
// Accessibility:
//   * `aria-label="Open keyboard cheat sheet"` so screen-reader
//     users hear the action's purpose, not just "question mark".
//   * `title` provides a hover tooltip for sighted mouse users.
//   * The Radix `Tooltip` adds a richer hint on hover (also announced
//     to screen readers via aria-describedby).
//   * 44×44 px touch target (per WCAG 2.5.5) — the visual 32×32
//     circle is wrapped in a 44×44 hit area.
//
// Hydration: rendered `null` until `mounted === true` so the SSR
// payload doesn't include a `fixed`-positioned button that might
// overlap with the loading-state splash. Mirrors the ThemeToggle
// pattern.

'use client'

import { useEffect, useState } from 'react'
import { Tooltip, TooltipTrigger, TooltipContent } from '@/components/ui/tooltip'

export interface ShortcutHintProps {
  /** Invoked when the button is clicked. The parent opens the
   *  KeyboardCheatSheet via this callback. */
  onOpen: () => void
  /** Optional className override — used by tests to assert the
   *  button's visibility / position without relying on the default
   *  Tailwind classes. */
  className?: string
}

export default function ShortcutHint({ onOpen, className }: ShortcutHintProps) {
  const [mounted, setMounted] = useState(false)
  useEffect(() => setMounted(true), [])
  if (!mounted) return null

  return (
    <div
      className="fixed bottom-4 right-4 z-30"
      style={{ paddingBottom: 'env(safe-area-inset-bottom)' }}
      aria-live="polite"
    >
      <Tooltip>
        <TooltipTrigger asChild>
          <button
            type="button"
            onClick={onOpen}
            // 44x44 hit area per WCAG 2.5.5 — the visual 32x32 circle
            // is centered within the larger touch target.
            className={
              'w-11 h-11 rounded-full bg-[#13161e] border border-[#2d3450] hover:border-cyan-500/60 hover:bg-[#1a1f2e] text-cyan-400 hover:text-cyan-300 shadow-lg flex items-center justify-center transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0b0e14] ' +
              (className ?? '')
            }
            aria-label="Open keyboard cheat sheet"
            title="Press ? for keyboard shortcuts"
            data-testid="shortcut-hint-button"
          >
            <span
              aria-hidden="true"
              className="text-lg font-bold leading-none"
              style={{ fontFamily: 'JetBrains Mono, ui-monospace, monospace' }}
            >
              ?
            </span>
          </button>
        </TooltipTrigger>
        <TooltipContent side="left" sideOffset={6}>
          <span>
            Press <kbd className="mono font-bold">?</kbd> for keyboard shortcuts
          </span>
        </TooltipContent>
      </Tooltip>
    </div>
  )
}
