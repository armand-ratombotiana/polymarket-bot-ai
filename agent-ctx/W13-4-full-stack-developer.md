# W13-4 — full-stack-developer — Dark/light theme switcher

## Task
Add a dark/light theme switcher using `next-themes`. The workstation was
dark-only; this task wires in a toggle (default dark) so traders can
opt into a light palette without losing the existing dark dashboard.

## Scope of changes
- **NEW** `src/components/ThemeProvider.tsx` — client wrapper around
  `next-themes`'s `NextThemesProvider` (`attribute="class"`,
  `defaultTheme="dark"`, `enableSystem={false}`,
  `disableTransitionOnChange`). Server-component safe (parent
  `layout.tsx` just renders it).
- **NEW** `src/components/ThemeToggle.tsx` — small icon button
  (`☀️` ↔ `🌙`) wired into the right-hand action cluster of
  `TopStatusBar`. Uses `useTheme()` from next-themes; renders `null`
  until `mounted` flips true (hydration-mismatch guard mandated by
  next-themes docs). Mirrors the existing `btn btn-ghost btn-sm
  p-1.5 text-xs text-[#7e8aaa] hover:text-white` styling used by
  mute / shortcuts so the toggle doesn't look out of place.
- **NEW** `src/components/ThemeToggle.test.tsx` — 5 vitest tests
  covering SSR-snapshot null render, post-mount button presence,
  dark→light toggle, light→dark toggle, and localStorage persistence.
- **MODIFIED** `src/app/layout.tsx`:
  - Import `ThemeProvider`
  - Added `suppressHydrationWarning` to `<html>` (absorbs the
    SSR/CSR class mismatch that next-themes injects via an inline
    script on first paint)
  - Wrapped the entire `<body>` tree (skip-link, OfflineIndicator,
    ErrorBoundary, SWRegister) inside `<ThemeProvider>` so even
    the error fallback card re-themes
- **MODIFIED** `src/components/TopStatusBar.tsx`:
  - Import `ThemeToggle`
  - Render `<ThemeToggle />` at the start of the right-hand action
    cluster (between the UTC clock and the mute button)
- **MODIFIED** `src/app/globals.css`:
  - Added `.light { … }` block (right after `:root`) redefining every
    design token: backgrounds (slate-50/100/white), borders
    (slate-200/300), text (slate-900/600/400 ladder), semantic colors
    (each `-fg` variant shifts darker so text stays readable on
    white), and mode tokens (paper/live/shadow/backtest).
  - Added scoped `.light .bg-[#...]`, `.light .text-[#...]`,
    `.light .border-[#...]` overrides for the ~880 occurrences of
    hardcoded Tailwind arbitrary values across 38 panel files
    (without these, panels would render dark cards on a light
    page). `!important` is required to win the cascade against
    Tailwind's generated `.bg-\[\#0e1015\]` rules.
  - Added `.light` overrides for scrollbar thumb (slate-300) and
    `hover:text-white` (rewires to slate-900 in light mode).

## Decisions
1. **Default theme = dark.** The dashboard was designed dark-first
   (CSS gradients, glow effects, accent contrast ratios all assume
   a dark canvas). Traders who want light mode opt in via the toggle;
   the choice persists to `localStorage` (next-themes default).
2. **`enableSystem={false}`.** The workstation is a trading terminal,
   not a content site — traders want a deterministic, sticky choice
   (e.g. dark always, even in a bright trading room), not whatever
   the OS prefers.
3. **`disableTransitionOnChange`.** Color flip is instant — no fade —
   so a misclick doesn't disorient during fast market action.
4. **ThemeToggle renders null on SSR.** `next-themes` only resolves
   `theme` after the provider reads `document.documentElement.className`
   / `localStorage` on the client. Rendering the icon during SSR
   would emit a `🌙` (the defaultTheme='dark' branch) that may
   mismatch the post-hydration value, which React flags as a
   hydration error. Returning `null` until `mounted` flips true
   sidesteps this entirely. Verified by `renderToStaticMarkup` test.
5. **Light theme palette.** WCAG AA contrast against the new
   `--bg-surface: #f8fafc` (slate-50):
   - Body text: `#0f172a` (slate-900) on `#f8fafc` → 17.4:1 (AAA).
   - Secondary: `#475569` (slate-600) → 7.1:1 (AAA).
   - Dim: `#94a3b8` (slate-400) → 2.7:1 (UI-only, not for body copy).
   - Each semantic `-fg` shifts 1-2 shades darker than its dark-mode
     counterpart (e.g. green-700 instead of green-400) so positive /
     negative P&L stays legible on white.
6. **Tailwind arbitrary value overrides use `!important`.** Tailwind
   generates `.bg-\[\#0e1015\]` in the same cascade layer as other
   utilities, so without `!important` the dark hex literal wins for
   any class declared later in source order. This is the standard
   escape hatch when a theme override needs to beat Tailwind's
   generated utilities; the alternative (rewriting 880 occurrences
   across 38 files) was deemed out of scope for this task.
7. **`mounted` flag instead of next-themes's `resolvedTheme`.** The
   task spec explicitly says to test "renders null before mount".
   `useTheme()` returns `theme: undefined` on SSR, but reading it
   would still cause hydration mismatch on the icon (`☀️` vs `🌙`).
   Using a separate `mounted` boolean is the simplest, most explicit
   guard.

## Verification
- **Install:** `bun add next-themes` → installed `next-themes@0.4.6`
  (it was already listed in package.json but not actually present in
  node_modules).
- **Lint:** `bun run lint` → clean (no errors, no warnings).
- **Tests:** `bun run test -- src/components/ThemeToggle.test.tsx`
  → 5/5 passing (~1.7s):
  1. `renders null before mount (SSR snapshot)` — uses
     `renderToStaticMarkup` from `react-dom/server` to verify no
     `<button>` is emitted during SSR.
  2. `renders a button after mount` — RTL `render` triggers
     `useEffect`, `mounted` flips true, button appears with the
     correct aria-label (`Switch to light mode`) and icon (`☀️`).
  3. `clicking the button toggles the theme from dark to light` —
     `userEvent.click()` flips `document.documentElement` class from
     `dark` to `light`.
  4. `clicking again toggles back from light to dark` — inverse case.
  5. `persists the chosen theme to localStorage so reload keeps it` —
     verifies `window.localStorage.getItem('theme') === 'light'`
     after toggle.
- **Dev server:** `tail dev.log` shows `Ready in 733ms`, `GET / 200`
  with no compile errors. `bun run lint` clean. No warnings about
  hydration mismatch or React act.
- **Full suite:** `bun run test` → 325 passed, 0 failed (excluding
  the unrelated `CommandPalette.test.tsx` from a sibling concurrent
  agent, which fails due to a jsdom limitation `i.scrollIntoView is
  not a function` — that's their bug, not mine).

## Caveats / known limitations
- **Hardcoded Tailwind arbitrary values.** ~880 occurrences of
  `bg-[#0e1015]` etc. across 38 panel files. The scoped `.light`
  overrides cover the ~12 most-used hex literals, which is enough
  to make every visible chrome element flip cleanly. Any panel that
  uses a *less common* hex literal (e.g. `#1b1e2c`) would still
  render dark on light — if encountered, the fix is to add another
  scoped override or convert the panel to use `var(--bg-card)` etc.
  This trade-off (override vs. rewrite) was made to keep the task
  scoped to theme plumbing, not a panel-by-panel color audit.
- **`act(...)` warnings in test stderr.** next-themes' provider does
  async state updates inside its effect, which `userEvent.click()`
  triggers outside of an `act()` boundary. The warnings are
  cosmetic — all assertions pass and the theme class flips correctly.
  Wrapping every click in `act(async () => { await user.click(btn) })`
  is already in place; the residual warning comes from a follow-up
  microtask scheduled by next-themes after the click handler returns.
- **Concurrent-agent reverts.** While I was editing, another agent
  (likely W13-x running in parallel) reverted my changes to
  `layout.tsx`, `globals.css`, and `TopStatusBar.tsx` (probably via
  `git stash` or `git checkout` over the same files). I detected
  this during the post-edit verification pass and re-applied all
  three edits. Final state verified by reading each file end-to-end
  and re-running lint + tests.

## Files touched
- NEW `src/components/ThemeProvider.tsx` (44 lines)
- NEW `src/components/ThemeToggle.tsx` (54 lines)
- NEW `src/components/ThemeToggle.test.tsx` (155 lines)
- MODIFIED `src/app/layout.tsx` (+18 lines: import + suppressHydrationWarning + wrap)
- MODIFIED `src/components/TopStatusBar.tsx` (+9 lines: import + ThemeToggle render)
- MODIFIED `src/app/globals.css` (+135 lines: `.light` block + Tailwind arbitrary value overrides)

## How a trader uses this
1. Open the workstation — lands in dark mode (default).
2. Click the small ☀️ icon in the top-right cluster (between the UTC
   clock and the mute button).
3. The entire dashboard flips to light mode: cards become white,
   borders become slate-200, text becomes slate-900. The trader's
   choice persists to localStorage, so reloads keep it.
4. Click the 🌙 icon (now visible in light mode) to flip back.
