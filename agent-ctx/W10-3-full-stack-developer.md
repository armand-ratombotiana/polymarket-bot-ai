# Task W10-3 — React Error Boundary + global error handling

**Agent:** full-stack-developer
**Date:** 2026-09-03
**Scope:** NEW `src/components/ErrorBoundary.tsx`, NEW `src/components/PanelErrorBoundary.tsx`,
NEW `src/app/error.tsx`, NEW `src/app/not-found.tsx`; EDIT `src/app/layout.tsx`,
`src/app/page.tsx`, `src/app/globals.css`. Additive — no existing logic modified.

---

## Background / investigation

- `src/app/layout.tsx` is a Server Component (no `'use client'`) that renders
  `<html><body>{children}</body></html>` with a skip-link before `{children}`.
  The single child today is `src/app/page.tsx` (the workstation dashboard,
  a Client Component). There was no error boundary above the page, so any
  uncaught render error in a panel would propagate to the Next.js App
  Router's default error UI (white screen + stack dump in dev).
- `src/app/page.tsx` (783 lines after edits) renders one of ~25
  `activeSection === '<section>'` cases inside `<AnimatePresence mode="wait">`
  + `<FadeIn key={activeSection}>` (a W10-8 panel-transition wrapper). Each
  case mounts a different panel component (MarketsPanel, PositionsPanel,
  DecisionLedgerPanel, …). Most panels are loaded via `next/dynamic` with
  `ssr: false` and a `PanelLoadingSkeleton` fallback (W9-6).
- The design system in `src/app/globals.css` defines dark-theme tokens
  (`--bg-card: #13161e`, `--text-primary: #dde1ed`, `--color-red-fg: #f87171`,
  `--color-amber-fg: #fbbf24`, `--z-critical: 60`, etc.). The dashboard
  surfaces use a `.card` pattern (`border-radius: 8px; border: 1px solid
  var(--border); background: var(--bg-card)`) and a `.btn` pattern with
  variants (`.btn-primary`, `.btn-ghost`, `.btn-sm`). Both are reused for
  the error fallback UIs so the boundary cards visually match the
  workstation.
- Existing class names: `error-boundary-fallback`, `error-page`,
  `not-found-page` did NOT exist in globals.css (verified via grep — 0
  matches before this task). No CSS conflicts.
- ESLint config (`eslint.config.mjs`) disables `no-console`,
  `@typescript-eslint/no-unused-disable-directive`, etc. — so `console.error`
  is fine, but `// eslint-disable-next-line no-console` directives are
  reported as unused (cleaned up after first lint pass).
- React error boundaries are class-only (the `static getDerivedStateFromError`
  + `componentDidCatch` lifecycle doesn't exist for function components),
  so the directive `'use client'` is REQUIRED on `ErrorBoundary.tsx` and
  `PanelErrorBoundary.tsx` — class components cannot be Server Components,
  and `React.ErrorInfo` references client-only types. The Next.js
  `app/error.tsx` is also required to be a Client Component per App Router
  spec; `not-found.tsx` can stay a Server Component (no hooks / handlers).
- Limitations of error boundaries (documented in component headers):
  - Catch: render phase, lifecycle methods, constructor errors below them.
  - Do NOT catch: event-handler errors, async errors (setTimeout/fetch),
    errors thrown in the boundary itself. Those need try/catch at call site.

## Files added / edited

### NEW `src/components/ErrorBoundary.tsx` — root-level boundary
- Class component extending `React.Component<Props, State>`.
- State: `{ hasError, error, errorInfo, showStack, retryCount }`.
- `static getDerivedStateFromError(error)` — render-phase hook, returns
  `{ hasError: true, error }`. NO side effects (no logging here — React
  calls this during the render phase).
- `componentDidCatch(error, errorInfo)` — commit-phase hook, logs to
  `console.error` (with `[ErrorBoundary]` prefix for dev-console
  filtering), stashes `errorInfo` on state for the fallback UI. Commented
  hook point for future telemetry POST to `/api/errors`.
- `handleReset()` — clears state, increments `retryCount`. After 2
  retries (`repeatedRetries` flag) the fallback shows an amber hint
  suggesting a hard reload (browser cache may hold a stale chunk).
- `handleReload()` — `window.location.reload()` guarded by
  `typeof window !== 'undefined'`.
- `toggleStack()` — toggles the collapsible `<details>` panel. The summary
  has `onClick={e => { e.preventDefault(); this.toggleStack() }}` so we
  control the open state ourselves (lets the ▸/▾ chevron stay in sync with
  `state.showStack` instead of relying on the native disclosure triangle).
- Fallback UI: full-viewport (`position: fixed; inset: 0`) overlay with
  `backdrop-filter: blur(6px)` over `--bg-overlay`, an animated pulsing
  ⚠ icon (`@keyframes error-boundary-pulse` 2.4s, with `prefers-reduced-
  motion: reduce` opt-out), a `role="alertdialog"` with `aria-labelledby`
  + `aria-describedby`, the error message in a mono-font red chip, a
  `Try Again` + `Reload Page` button row (`.btn-primary` + `.btn-ghost`),
  and a collapsible stack-trace `<details>` showing both `error.stack`
  and `errorInfo.componentStack` in a scrollable `<pre>` (max-height
  320px). Pass-through: when `hasError === false` returns
  `this.props.children` directly (no wrapper div — preserves layout).
- Optional `fallback?: React.ReactNode` prop overrides the default UI.

### NEW `src/components/PanelErrorBoundary.tsx` — panel-level boundary
- Lighter-weight companion to `ErrorBoundary` for wrapping individual
  panels in `page.tsx` so a single panel crash doesn't take down the
  whole dashboard.
- State: `{ hasError, error }` (no stack UI — keeps the inline fallback
  compact).
- Same lifecycle pattern: `getDerivedStateFromError` +
  `componentDidCatch`. Logs include the optional `label` prop
  (`[PanelErrorBoundary: Live Order Books]`) so multi-panel crashes are
  attributable in the dev console.
- Fallback UI: a card-style div (`.panel-error-boundary`) that fills its
  parent (`height: 100%`) with a red-tinted background, an icon + title
  row (`{label} encountered an error`), a truncated mono message
  (max-height 6em with overflow auto), and a `Retry` + `Reload` button
  row aligned right (`.btn-primary .btn-sm` + `.btn-ghost .btn-sm`).
- Optional `label?: string` (shown in fallback; defaults to "This panel")
  and `fallback?: React.ReactNode` (overrides default UI).

### NEW `src/app/error.tsx` — Next.js App Router error page
- Client Component (`'use client'` — required by App Router spec).
- Signature: `Error({ error, reset }: { error: Error & { digest?: string };
  reset: () => void })`.
- `useEffect` logs `[app/error.tsx] Route-segment error caught:` to dev
  console (mirrors the in-tree boundary's logging so the source is
  always traceable regardless of which boundary caught the error).
- Renders `.error-page` (full-viewport centered card) with pulsing ⚠,
  error message in a red mono chip, optional `error.digest` (stable hash
  Next.js attaches to server-side errors — useful for log correlation),
  and a `Try again` (calls `reset()`) + `Reload page` button row.
- Coexists with the in-tree `<ErrorBoundary>` in `layout.tsx`: that one
  catches client render-tree errors; this one catches App-Router-level
  route-segment errors that escape the tree boundary.

### NEW `src/app/not-found.tsx` — Next.js 404 page
- Server Component (no `'use client'` — no hooks/handlers needed).
- Renders `.not-found-page` (full-viewport centered card) with a large
  amber `404` in JetBrains Mono (72px, letter-spacing -0.04em, drop
  shadow), a `Page not found` title, a description line, and a
  `← Back to Workstation` link (via `next/link`) pointing at `/`.

### EDIT `src/app/layout.tsx`
- Added `import ErrorBoundary from '@/components/ErrorBoundary'`.
- Wrapped `{children}` with `<ErrorBoundary>{children}</ErrorBoundary>`
  inside `<body>`, after the existing skip-link. The skip-link stays
  outside the boundary so it remains accessible even if the page below
  crashes (keyboard users can still navigate away).

### EDIT `src/app/page.tsx`
- Added `import PanelErrorBoundary from '@/components/PanelErrorBoundary'`
  after the `ConfirmationDialog` import.
- Wrapped each of the 25 `activeSection === '...'` render cases with
  `<PanelErrorBoundary label="...">...</PanelErrorBoundary>`. Labels
  match the sidebar section names: "Command Center", "Live Order Books",
  "Market Screener", "Positions", "Open Orders", "Recent Trades",
  "Strategy Registry", "Arbitrage Matrix", "Deep Analysis",
  "AI / ML Engine", "AI Copilot", "Shadow Inference", "ML Validation",
  "Performance Analytics", "Backtest Lab", "Attribution",
  "Execution Quality", "Closed Positions", "Capital Allocator",
  "System Health", "Database Explorer", "Observability", "Retention",
  "Decision Ledger", "Live Safety Gate".
- The wrapper sits INSIDE the existing `<div style={{ height: '100%',
  overflow: ... }}>` for each case (so the parent's height/overflow
  styling is preserved). On the happy path `PanelErrorBoundary` returns
  `this.props.children` directly (no wrapper div) — zero layout change.
  On error, its `.panel-error-boundary` fallback uses `height: 100%`
  to fill the parent cell.
- The command center case wraps the entire `<div className="command-
  center-layout">` grid in ONE PanelErrorBoundary (label="Command
  Center") — a single boundary for the whole grid, consistent with the
  per-section granularity of the other cases.
- Existing W10-8 `<AnimatePresence mode="wait"><FadeIn key=
  {activeSection}>...</FadeIn></AnimatePresence>` wrapper is untouched;
  PanelErrorBoundary sits inside FadeIn (so a panel crash during the
  fade-in still triggers the fallback without breaking the transition).

### EDIT `src/app/globals.css`
- Appended a new `W10-3 — ERROR BOUNDARY STYLES` section after the
  existing scrollbar-corner rule (line 1560) and before the W10-8 polish
  layer. Three style groups:
  1. `.error-boundary-fallback` + children — root-level fallback
     (full-viewport overlay, blur backdrop, pulsing red ⚠, mono error
     chip, collapsible stack trace in a scrollable `<pre>`).
  2. `.panel-error-boundary` + children — panel-level fallback
     (fills parent cell, red-tinted background, icon+title row, mono
     message, right-aligned Retry/Reload buttons).
  3. `.error-page` + `.not-found-page` + children — App Router route
     fallbacks (full-viewport centered, amber 404 in mono, red error
     chip, digest line, button rows).
- All colors / spacing / radii / z-index pulled from existing CSS custom
  properties (`--bg-overlay`, `--color-red-bg/bd/fg`, `--color-amber-fg`,
  `--text-primary`, `--border`, `--z-critical`, `--radius-*`, `--space-*`,
  `--easing-std`) — no new design tokens introduced, no hardcoded hex
  outside of fallback values inside `var(..., <fallback>)`.
- Shared `@keyframes error-boundary-pulse` (2.4s) + a
  `prefers-reduced-motion: reduce` opt-out so the pulsing icon is static
  for users who request reduced motion.

## Verification

- `bun run lint` — first pass produced 4 warnings (all "Unused eslint-
  disable directive" — project's `no-console` rule is OFF, so the
  `// eslint-disable-next-line no-console` directives I'd added defensively
  were no-ops). Removed all three from my new files. Re-run: clean, no
  warnings or errors.
- `dev.log` — `▲ Next.js 16.1.3 (Turbopack)`, `✓ Ready in 733ms`,
  `GET / 200 in 8.3s (compile: 8.2s, render: 131ms)`. No compile errors.
- Manual sanity: the per-section `<PanelErrorBoundary>` wrappers add zero
  DOM on the happy path (render returns `this.props.children` directly),
  so the 25 existing panels keep their exact layout, height, and
  overflow behavior. Only when an error is caught does the fallback
  `.panel-error-boundary` div mount — and it uses `height: 100%` so it
  fills the parent grid cell without collapsing the dashboard.

## Bug-watch

- No genuine bugs found. One subtle gotcha worth noting for future
  readers: error boundaries catch errors thrown DURING RENDER, in
  lifecycle methods, and in constructors of components below them — but
  NOT in event handlers, async code (setTimeout / fetch / async-await),
  or errors thrown in the boundary itself. The components/hooks in this
  codebase that fetch data (e.g. `useBot`, the various panel `useEffect`
  hooks) must continue to use try/catch inside their async paths;
  `<PanelErrorBoundary>` only protects against render-phase crashes
  (e.g. a `null.someProp` dereference during a `.map` over a malformed
  API response). This is documented in the component header comments.
- The collapsible `<details>` in `ErrorBoundary`'s fallback uses
  `summary onClick={e => { e.preventDefault(); this.toggleStack() }}`
  rather than relying on the native disclosure triangle — this keeps
  the ▸/▾ chevron in `state.showStack` sync. The native triangle is
  hidden via `list-style: none` + `::-webkit-details-marker { display:
  none }`.

## Stage Summary

- Created `ErrorBoundary.tsx` (root-level, full-viewport fallback with
  pulsing ⚠, collapsible stack trace, Try Again + Reload buttons).
- Created `PanelErrorBoundary.tsx` (panel-level, inline red-tinted card
  with label + Retry + Reload buttons — designed to fill the parent grid
  cell).
- Created `app/error.tsx` (Next.js App Router error page, mirrors the
  in-tree fallback visual language, surfaces `error.digest` for log
  correlation).
- Created `app/not-found.tsx` (404 page with large mono `404` and a
  `← Back to Workstation` link).
- Wrapped all 25 `activeSection === '...'` render cases in `page.tsx`
  with `<PanelErrorBoundary label="...">`. Happy path: zero extra DOM.
- Wrapped `{children}` in `layout.tsx` with `<ErrorBoundary>` — outermost
  safety net above the page.
- Added ~270 lines of dark-theme CSS to `globals.css` for the three
  error surfaces, all using existing design tokens (no new tokens).
- `bun run lint`: clean (0 errors, 0 warnings after eslint-disable
  cleanup).
- `dev.log`: clean compile, `GET / 200`.
- Existing functionality preserved: skip-link, AnimatePresence/FadeIn
  W10-8 panel transitions, all 25 panel render paths, modal wiring,
  kill-switch / observation banners, keyboard-shortcut nav.
