// components/ThemeProvider.tsx — W13-4 Dark/light theme switcher
//
// Wraps `next-themes`'s `NextThemesProvider` so the entire workstation
// (rendered inside `<body>` of `src/app/layout.tsx`) becomes theme-aware.
//
// Configuration choices:
//   - `attribute="class"`  — toggles a `dark` / `light` class on `<html>`.
//                            `globals.css` exposes both `.dark` (default
//                            `:root` values) and `.light` overrides, so
//                            switching the class flips every CSS variable
//                            that the workstation relies on.
//   - `defaultTheme="dark"` — the dashboard was designed dark-first; the
//                            existing palette, contrast ratios, and glow
//                            effects all assume a dark canvas. Defaulting
//                            to dark preserves the existing look until
//                            the trader opts in to light mode.
//   - `enableSystem={false}` — we don't follow `prefers-color-scheme`
//                            because the workstation is a trading
//                            terminal, not a content site. Traders want
//                            a deterministic, sticky choice (e.g. dark
//                            always — even in a bright trading room).
//   - `disableTransitionOnChange` — color flip is instant, no fade,
//                            so a misclick doesn't disorient during
//                            fast market action.
//
// This component is a client component because `next-themes` reads
// `document.cookie` / `localStorage` on mount; the parent `layout.tsx`
// (a server component) just renders this without touching window APIs.

'use client'

import { ThemeProvider as NextThemesProvider } from 'next-themes'
import type { ThemeProviderProps } from 'next-themes'

export default function ThemeProvider({ children }: ThemeProviderProps) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      disableTransitionOnChange
    >
      {children}
    </NextThemesProvider>
  )
}
