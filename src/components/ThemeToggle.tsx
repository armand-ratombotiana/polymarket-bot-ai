// components/ThemeToggle.tsx — W13-4 Dark/light theme switcher button
//
// Renders the small ☀️ / 🌙 icon button that lives in the TopStatusBar
// right-hand cluster (alongside mute, shortcuts, config). Clicking flips
// the active theme between `dark` (default) and `light`.
//
// Why a separate component:
//   - `next-themes`'s `useTheme()` only knows the active theme *after*
//     mount (it reads `document.documentElement.className` or
//     `localStorage` on the client). Rendering the icon during SSR would
//     emit a `🌙` (the defaultTheme='dark' branch) that may mismatch the
//     post-hydration value, which React flags as a hydration error and
//     causes a full client re-render. We avoid that by rendering `null`
//     until `mounted === true`.
//
// Why a small icon button (not a fancy dropdown / segmented control):
//   - The TopStatusBar is already dense (mode pill, kill switch, P&L,
//     ML health, latency, freshness, clock, mute, shortcuts, config,
//     cancel-all, kill/resume). One more tiny icon doesn't blow the
//     layout. Matches the existing `btn btn-ghost btn-sm` visual
//     language used by mute / shortcuts so the toggle doesn't look
//     like a different control category.
//
// Accessibility:
//   - `aria-label` announces the *target* state ("Switch to light mode"
//     when currently dark) so a screen reader tells the trader what
//     the click will do, not what the icon shows.
//   - `title` provides a hover tooltip for sighted mouse users.
//   - `aria-pressed` reflects whether dark is currently active, since
//     "dark mode on" is a meaningful toggled state.

'use client'

import { useTheme } from 'next-themes'
import { useEffect, useState } from 'react'

export default function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const [mounted, setMounted] = useState(false)

  // Avoid hydration mismatch: next-themes only resolves `theme` after
  // the provider reads the DOM/localStorage on mount, so on the very
  // first server-render `theme` is undefined. Returning null here
  // means the server-rendered HTML has no button at all, and React
  // hydrates a stable tree once `mounted` flips to true on the client.
  useEffect(() => setMounted(true), [])
  if (!mounted) return null

  const isDark = theme === 'dark'

  return (
    <button
      onClick={() => setTheme(isDark ? 'light' : 'dark')}
      className="btn btn-ghost btn-sm p-1.5 text-xs text-[#7e8aaa] hover:text-white"
      aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
      title={isDark ? 'Light mode' : 'Dark mode'}
      aria-pressed={isDark}
    >
      <span aria-hidden="true">{isDark ? '☀️' : '🌙'}</span>
    </button>
  )
}
