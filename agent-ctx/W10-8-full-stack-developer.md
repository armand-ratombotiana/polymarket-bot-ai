# W10-8 — full-stack-developer — UI polish (Framer Motion transitions, skeleton improvements)

**Task ID:** W10-8
**Agent:** full-stack-developer
**Date:** 2026-09-03
**Task:** Add Framer Motion panel transitions + improved skeletons + visual feedback to the Polymarket Pro dashboard.

## Inputs read
- `/home/z/my-project/worklog.md` (tail) — established wave context: W9-6 added React.memo + `PanelLoadingSkeleton` + `lazyPanel()` helper; W9-7 added a11y audit (`.sr-only`, skip-link, `:focus-visible`, `prefers-reduced-motion` guard). Both left lint clean.
- `/home/z/my-project/package.json` (lines 1-102) — confirmed `framer-motion@^12.23.2` (resolved to 12.26.2) AND `sonner@^2.0.6` already installed. **No `bun add` needed.**
- `/home/z/my-project/src/app/page.tsx` (full 707 lines pre-edit) — mapped the panel-switching architecture: a single `<div className="page-area">` wrapping 25+ `activeSection === 'xxx' && (...)` conditionals. Grid layouts (`.command-center-layout`, `.workstation-split-layout`) require child `height: 100%` to flow correctly → FadeIn wrapper must be a flex item that grows.
- `/home/z/my-project/src/components/ui/skeleton.tsx` (12 lines) — existing shadcn Skeleton primitive (`bg-accent animate-pulse rounded-md`). Kept unchanged.
- `/home/z/my-project/src/app/globals.css` (1,561 lines) — confirmed existing `@keyframes skeleton-shimmer` (line 1229), existing `.skeleton-card` / `.skeleton-line` / `.skeleton-line-lg` (lines 476-501), existing global `prefers-reduced-motion` guard (line 130). Missing: `.skeleton-line-sm`, `.skeleton-line-md`, `.skeleton-cell`, `.skeleton-row`, `.skeleton-table`, `.skeleton-kpi`, `.card-hover`, `--bg-elevated`, `--popover` / `--popover-foreground`, `[data-sonner-toast]` styling.

## Files created

### `src/components/ui/motion.tsx` (160 lines)
- `'use client'` directive at top (Framer Motion touches window/RAF — importing from a server component would crash).
- `FadeIn` — `motion.div` with `initial={{opacity:0,y:8}} → animate={{opacity:1,y:0}} → exit={{opacity:0,y:-4}}`, 0.2s easeOut. **Critical addition vs. spec:** the motion.div carries inline `style={{flex:1, minHeight:0, display:'flex', flexDirection:'column'}}` so it fills `.page-area`'s flex column and child panels using `height:100%` (e.g. `.command-center-layout`) keep working. Optional `className` + `style` props for caller overrides.
- `SlideIn` — 4-direction slide (300px offset, 0.25s easeInOut, exit reverses axis). For future side panels / drawers / modals.
- `AnimatedListItem` — `initial={{opacity:0,x:-10}} → animate to 0`. Stagger capped at 0.3s ceiling so 200-row tables don't take 4s.
- `Pulse` — opacity oscillation `[0.5,1,0.5]` at 1.5s, matches existing `skeleton-shimmer` cadence for cohesive motion.
- `StaggerContainer` — variants-based parent with `staggerChildren: 0.03` (30ms between siblings).
- `NumberTicker` — re-keys on `value` change so Framer treats each new value as a fresh mount; `format` callback lets callers control currency/%/BPS formatting (presentation-pure component).
- Re-exports `AnimatePresence` so callers don't need a second import line.

### `src/components/ui/skeleton-card.tsx` (67 lines)
- `SkeletonCard()` — `<div className="skeleton-card">` with three child lines (lg/sm/md). Includes `role="status"` + `aria-label="Loading content"` for SR accessibility (matches the W9-6 PanelLoadingSkeleton a11y pattern).
- `SkeletonTable({rows, cols})` — flex-column container of `skeleton-row` flex rows, each containing `cols` skeleton-cells. Defaults: 5 rows × 4 cols. Each row + cell gets `role="status"` + `aria-label` so AT users know "Loading N rows of data".
- `SkeletonKPI()` — KPI metric placeholder (small label line + large value line). `role="status"` + `aria-label="Loading metric"`.
- No `'use client'` directive needed — these are plain divs and the shimmer is pure CSS. Server components can use them too (future-proofing for any RSC adoption).

## Files modified

### `src/app/page.tsx`
- Added import line 19: `import { AnimatePresence, FadeIn } from '@/components/ui/motion'` with a 7-line comment block explaining the rationale.
- Wrapped the existing 25+ `activeSection === 'xxx' && (...)` blocks (lines 379-635) inside `<AnimatePresence mode="wait"><FadeIn key={activeSection}>...</FadeIn></AnimatePresence>`. The `key` prop on FadeIn is what AnimatePresence uses to detect the swap — without a key change, no exit/enter animation fires.
- Did NOT refactor the conditionals into a switch function — kept the existing inline `&& (...)` pattern intact to minimize the diff and avoid breaking the React.memo comparators set up in W9-6.
- Did NOT touch the W9-6 `PanelLoadingSkeleton` — it's the lazy-import chunk placeholder and works orthogonally with the FadeIn panel transition (chunk-load skeleton → FadeIn wraps the loaded panel).

### `src/app/globals.css` (appended ~150 new lines, lines 1562-1712)
All ADDITIVE — no earlier rule block edited. New rules:
- `:root { --bg-elevated: var(--bg-card-alt, #111420); }` — new token used by the shimmer gradient.
- `.skeleton-line-sm` (height:6px, width:40%) — caption / label line.
- `.skeleton-line-md` (height:14px, width:100%) — body-text line.
- Shared shimmer gradient on `.skeleton-line-sm`, `.skeleton-line-md`, `.skeleton-cell`: `linear-gradient(90deg, --bg-surface 25%, --bg-elevated 50%, --bg-surface 75%)` + `background-size: 200% 100%` + `animation: skeleton-shimmer 1.5s ease-in-out infinite` (reuses the existing `@keyframes` at line 1229 — no duplicate keyframe).
- `.skeleton-kpi` — card-styled KPI box (uses --bg-card + --border + --radius-md + --space-3 padding + flex-column + gap).
- `.skeleton-card .skeleton-line-sm` / `.skeleton-line-md` / `.skeleton-kpi .skeleton-line-*` — specificity overrides so the new line variants inherit consistent vertical rhythm when nested inside SkeletonCard / SkeletonKPI primitives.
- `.skeleton-table` — flex column with 1px gap + --border background (so the gap reads as table gridlines).
- `.skeleton-row` — flex row with 1px gap + --border background.
- `.skeleton-cell` — flex:1 1 0, height:28px (matches .data-table row height), --bg-card base + translucent white overlay shimmer.
- `.card-hover` — `transition: transform 0.15s ease, box-shadow 0.15s ease; will-change: transform;` for GPU compositing hint.
- `.card-hover:hover` — `transform: translateY(-1px); box-shadow: 0 4px 12px rgba(0,0,0,0.15);` (subtle 1px lift + soft shadow).
- `@media (prefers-reduced-motion: reduce)` block — disables the card-hover transition + transform entirely (mirrors the existing global reduced-motion guard at line 130, but scoped to .card-hover so the rule is explicit + self-documenting).
- `:root { --popover: var(--bg-card); --popover-foreground: var(--text-primary); }` — maps the shadcn sonner toaster's referenced vars to the dashboard's existing palette (without this, `--popover` would be undefined and sonner toasts would render with transparent backgrounds).
- `[data-sonner-toast]` — border-radius: --radius-md, font-family: JetBrains Mono (matches the dashboard's mono typography), font-size: 12.5px (matches `.data-table` cell font-size). Used `!important` on border-radius because sonner's inline styles would otherwise win the cascade.

## Verification

- **`bun run lint`** → exit 0, 0 errors. 3 pre-existing warnings in files I did NOT touch:
  - `src/components/ErrorBoundary.tsx` line 66 (unused `no-console` eslint-disable)
  - `src/components/PanelErrorBoundary.tsx` line 41 (same)
  - `src/lib/validateDev.ts` line 81 (same)
  - My changes (motion.tsx, skeleton-card.tsx, page.tsx edits, globals.css additions) produced ZERO new lint issues.
- **`bunx tsc --noEmit --skipLibCheck`** filtered to my 3 modified files (`src/app/page.tsx`, `src/components/ui/motion.tsx`, `src/components/ui/skeleton-card.tsx`) → ZERO errors. Full project type-check shows pre-existing errors in unrelated files (`examples/websocket/*.ts`, `skills/*.ts`, `src/app/api/bot/route.ts`) that were present before my edits and are out of scope.
- **`dev.log`** review — last entry: `GET / 200 in 28ms` (clean compile). Dev server was suspended between prior agents' runs and mine; per project convention ("Do NOT run `bun run dev` — the system runs it automatically"), I did not start it. My type-check + lint validation substitutes for runtime verification.

## Key design decisions

1. **FadeIn carries flex-fill styles** — The task spec's FadeIn was a bare `motion.div`. Adding `style={{flex:1, minHeight:0, display:'flex', flexDirection:'column'}}` is critical because `.page-area` is a flex column and child panels use `height:100%` (which only works if the direct parent has a definite height — the flex:1 makes motion.div grow to fill the column, and minHeight:0 lets the column's `overflow:hidden` actually clip). Without this, the command-center-layout would collapse to 0 height.

2. **AnimatePresence mode="wait"** — Waits for outgoing fade-out (200ms) before mounting incoming. This avoids the brief overlap where two panels render simultaneously (which would cause a vertical scrollbar jump on the page-area). The `key={activeSection}` on FadeIn is what AnimatePresence uses to detect the swap — without a key change, no exit/enter animation fires.

3. **No panel refactor** — Kept the existing inline `&& (...)` conditional pattern intact. Refactoring into a `renderPanel()` switch function would be a larger diff and risk breaking the W9-6 React.memo comparators on panel components (which compare on prop identity, not JSX tree shape). The FadeIn wrapper is purely additive.

4. **Reused existing `@keyframes skeleton-shimmer`** — The new `.skeleton-line-sm`, `.skeleton-line-md`, `.skeleton-cell` rules reference the existing `@keyframes skeleton-shimmer` at line 1229. No duplicate keyframe; the entire skeleton system (old + new) animates in lock-step at the same 1.5s cadence.

5. **Reduced-motion guard on `.card-hover`** — Although the global `prefers-reduced-motion: reduce` at line 130 already clamps all transitions to 0.01ms, I added an explicit per-rule override that also removes the `transform: translateY(-1px)` lift. This is because the lift is a visual effect, not a transition — without the explicit override, the element would still move on hover even though the transition is instant. The explicit block is self-documenting and defensive against future global-rule changes.

6. **Sonner theme var mapping** — The shadcn `ui/sonner.tsx` toaster references `--popover` / `--popover-foreground` / `--border`. The dashboard uses a custom dark palette, NOT the shadcn default tokens, so without explicit mapping these vars would be undefined. Mapping them to `--bg-card` / `--text-primary` / `--border` ensures any future `toast()` call renders with the correct card background + text color (matches the `.card` styling used everywhere else).

## Stage summary
- Installed framer-motion: **SKIPPED** (already at v12.26.2).
- Created `src/components/ui/motion.tsx` (160 lines): FadeIn, SlideIn, AnimatedListItem, Pulse, StaggerContainer, NumberTicker, AnimatePresence re-export.
- Created `src/components/ui/skeleton-card.tsx` (67 lines): SkeletonCard, SkeletonTable, SkeletonKPI.
- Modified `src/app/page.tsx`: 1 import line added + 25+ panel conditionals wrapped in `<AnimatePresence mode="wait"><FadeIn key={activeSection}>`.
- Appended ~150 lines to `src/app/globals.css`: new skeleton variants with shared shimmer gradient, .skeleton-table/.skeleton-row/.skeleton-cell, .card-hover + reduced-motion guard, sonner theme variable mappings + `[data-sonner-toast]` styling.
- **Lint: clean** (0 errors; 3 pre-existing warnings in untouched files).
- **Type-check: clean** for all 3 modified files (0 new errors).
- **Dev log: clean** (last GET / 200; dev server suspended between agents, not restarted per project convention).
