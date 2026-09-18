# W50-2c — full-stack-developer — Redesign TopStatusBar.tsx for compact, information-dense, modern header

**Task ID:** W50-2c
**Agent:** full-stack-developer
**Scope:** EDIT (additive + visual redesign) of
`src/components/TopStatusBar.tsx` (668 → 784 lines, +116).

No new files, no test changes, no API changes. Existing 17-test suite in
`src/components/TopStatusBar.test.tsx` continues to pass verbatim — every
aria-label, mode-badge literal, halt/obs badge text, and the
`/api/ml/metrics` + `/api/ml/drift` mount-time fetch contract preserved.

## Goal

Bring the trading workstation header up to a professional trading-terminal
bar: compact single row, clear LEFT/CENTER/RIGHT clusters separated by
subtle 1px `var(--border-dim)` vertical dividers, 32px-tall stat chips
with `tabular-nums` (digits don't shimmy), a pulse-ring connection dot
labelled Live/Degraded/Offline, an "Updated Xs ago" freshness chip that
turns amber at 10s and red at 30s, a new compact System Health pill in
the right cluster (alongside the existing 2px bottom mini-bar), and a
1px top-border highlight + `shadow-lg` for a floating effect.

## Constraints honoured

- **All existing props preserved** (snapshot, status, uptime,
  onKillSwitch, onResumeSwitch, onCancelAll, onOpenShortcuts,
  onToggleMute, muted, onOpenConfig, onMobileNav, panelName, panelGroup,
  onOpenSystemHealth).
- **All existing class names preserved** — `topbar`, `btn` / `btn-ghost`
  / `btn-amber` / `btn-kill` / `btn-resume`, `mono`, `sr-live`,
  `freshness-fresh` / `freshness-ok` / `freshness-stale` /
  `freshness-dead`, `topbar-health-shimmer` keyframe, etc. The CSS agent
  (W50 sibling task) can layer its colour palette on top without my
  changes interfering.
- **Client component**: kept (`'use client'` directive unchanged).
- **No functionality broken**: every button still calls its handler;
  the existing 17-test vitest suite passes 17/17.
- **Accessibility preserved + enhanced**: every interactive element
  retains its aria-label, the new System Health pill is a real `<button>`
  when onOpenSystemHealth is provided (keyboard-focusable, ring on
  focus), every action button gained `focus-visible:ring-2` styling,
  the SR live region still announces "Today P&L X. Paper balance Y.
  Connection Z. System health W." on every snapshot tick.

## Files touched

- `src/components/TopStatusBar.tsx` (668 → 784 lines; additive redesign)
- `worklog.md` (appended W50-2c entry per task instructions)

## Verification

| Check | Command | Result |
|---|---|---|
| ESLint | `bun run lint` | **clean** (no warnings/errors) |
| TypeScript | `bunx tsc --noEmit --skipLibCheck` | **0 errors in TopStatusBar.tsx** |
| Vitest | `bunx vitest run src/components/TopStatusBar.test.tsx` | **17 / 17 passed** (577ms) |

The 4 remaining tsc errors (`src/app/page.tsx`, `src/components/CommandCenterDashboard.tsx`,
`vitest.singlefork.config.ts`) are pre-existing — confirmed via `git
stash` + tsc on the clean tree before my edits — and outside this task's
file scope (other W50 sibling agents are actively editing those files).

## Test contracts preserved

Per the existing `TopStatusBar.test.tsx` (17 tests):

- `getByRole('banner')` — header element (✓ `topbar` class + role="banner").
- `getByText('PAPER TRADING')` / `'LIVE TRADING'` / `'SHADOW MODE'` —
  mode badge literals unchanged.
- `getByText('🛑 HALTED')` — halt badge when `kill_switch=true`.
- `getByText('👁 OBS ONLY')` — obs badge when `observation_only=true &&
  !kill_switch`.
- `getByRole('button', { name: /kill switch/i })` — Kill Switch button.
- `getByRole('button', { name: /resume/i })` — Resume button when
  `kill_switch=true`.
- `getByRole('button', { name: /cancel all/i })` — Cancel All button.
- `getByRole('button', { name: /open keyboard shortcuts/i })` —
  Shortcuts button aria-label.
- `getByRole('button', { name: /open strategy and risk configuration/i })` —
  Config button aria-label.
- `getByRole('button', { name: /mute audio alerts/i })` — Mute button
  aria-label (when `muted=false`).
- `getByRole('button', { name: /open user preferences/i })` — Settings
  (🛠) button aria-label.
- `/api/ml/metrics` + `/api/ml/drift` fetches on mount — preserved.
- Doesn't throw on fetch error — preserved.

## Design summary (what changed visually)

### Layout & dividers
- Two new `<span aria-hidden="true" className="hidden md:block self-center h-6 w-px shrink-0" style={{ background: 'var(--border-dim)' }} />` dividers inserted between LEFT↔CENTER and CENTER↔RIGHT clusters.
- Header root gained `shadow-lg shadow-black/30` + a 1px top highlight via `style={{ borderTop: '1px solid rgba(255,255,255,0.04)' }}` for the floating effect.

### LEFT cluster
- Mobile hamburger is now a 32px (h-8 w-8) square button.
- Breadcrumb separators replaced bare '/' glyphs with small 8×10 SVG chevrons (currentColor stroke, 1.25 width) for a modern look.

### CENTER KPI cluster
- Every mono value span gained `tabular-nums` (Balance, P&L, Exposure, Brier, AUC, Uptime) so digits don't shimmy tick-to-tick.
- KPI labels gained `tracking-wider` for a refined uppercase look.

### RIGHT cluster — status pills
- **UTC clock (xl+)**: switched to `inline-flex`, 32px height, tabular-nums.
- **REST connection pill (sm+)**: rewrote the dot as a layered pulse-ring — an absolutely-positioned `animate-ping` overlay sits behind a crisp 8×8 dot. Visible label switched from "Connected/Connecting…/Disconnected" to "Live/Degraded/Offline"; full connLabel preserved in title + SR live region.
- **Freshness chip (sm+)**: added "Updated " prefix; changed `freshnessClass(dataAge, 15, 60)` → `freshnessClass(dataAge, 10, 30)` so spec thresholds hold: <10s green, 10–30s amber, ≥30s red.
- **Latency chip (lg+)**: 32px height + tabular-nums.
- **NEW System Health pill (sm+)**: compact dot + tier-label pill that calls onOpenSystemHealth when provided (degrades to non-interactive role="status" when not). Coexists with the existing 2px bottom mini-bar.

### RIGHT cluster — action buttons
Every button standardised to 32px height (h-8) with consistent focus-visible:ring styling:
- Settings (🛠): h-8 w-8 p-0 square.
- Mute (🔊/🔇): h-8 w-8 p-0 square (sm+).
- Shortcuts (⌨️): h-8 w-8 p-0 square (sm+).
- Config (⚙️): h-8 with "Config" text label hidden below lg.
- Cancel All: h-8 with amber focus-visible ring.
- Kill Switch / Resume: h-8 with red/green focus-visible rings; existing bg-red-600 / bg-green-600 fills + animate-pulse on RESUME preserved verbatim.

### Cleanup
- Removed pre-existing TS6133 unused-variable `connDotClass` (was declared but never read in the JSX; verified via grep before deletion). No behaviour change.

## Prior work consulted
- W49-6 (original compact single-line layout — base for W50-2c).
- W38-8 (original component tests — every assertion preserved).
- W39-8 (SR live region for P&L — preserved verbatim).
- W15-2 (SettingsModal — preserved verbatim).
- W22-1 (ML telemetry fetch — preserved verbatim).

## Approach
- Read full TopStatusBar.tsx + TopStatusBar.test.tsx to map every test contract that must survive the redesign.
- Used MultiEdit for surgical, atomic edits to the JSX (header root, mobile nav, breadcrumb, KPI chips, status pills, action buttons) — each edit's `old_str` was matched verbatim from the file so nothing was inadvertently clobbered.
- Ran the existing 17-test vitest suite after each batch of edits to catch regressions early.
- Verified tsc + lint are clean for TopStatusBar.tsx specifically (the 4 pre-existing tsc errors in other files were confirmed via git stash, not introduced by this task).
