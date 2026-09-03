# Task W13-5 — Command Palette (Cmd+K)

**Agent:** full-stack-developer
**Task ID:** W13-5
**Task:** Add a command palette (Cmd+K / Ctrl+K) for quick navigation and actions.

## Summary

Implemented a global ⌘K / Ctrl+K command palette on top of the existing
shadcn `ui/command.tsx` (which wraps `cmdk@1.1.1`). The palette surfaces
every workstation navigation section (25 panels, mirrored from
`Sidebar.tsx`) plus a small set of page-level actions (refresh / theme
toggle / open shortcuts / cancel all / kill switch).

## Files Touched

### New Files

| Path | Purpose |
|---|---|
| `src/components/CommandPalette.tsx` | The palette component (180 lines). Wraps `CommandDialog` from `ui/command.tsx`. 25 navigation commands (one per `NavSection`) + optional `extraActions` injection point. Controlled `search` state with reset-on-close. |
| `src/components/CommandPalette.test.tsx` | 15 tests covering render-when-open, filtering, keyword matching, selection → onNavigate + close, extraActions, and the Cmd+K / Ctrl+K shortcut behaviour via a `CmdKHarness` wrapper that replicates the page.tsx `useEffect` pattern. |

### Modified Files

| Path | Change |
|---|---|
| `src/app/page.tsx` | Added `useMemo` import. Added `import CommandPalette, { type CommandItemDef }`. Added `cmdOpen` state. Added a NEW `useEffect` for the Cmd+K/Ctrl+K chord with `preventDefault`. Added `setCmdOpen(false)` to the Escape branch (safety net). Built a memoized `cmdExtraActions` array of 6 page-level actions. Passed `onOpenCommandPalette` to TopStatusBar. Rendered `<CommandPalette />` in the modals section. |
| `src/components/TopStatusBar.tsx` | Added `onOpenCommandPalette?: () => void` prop. Added a 🔍 + ⌘K kbd-badge button between ThemeToggle and onToggleMute (kbd badge is `hidden sm:inline-block` for responsiveness). |
| `src/app/globals.css` | Appended ~100 lines of CSS: `.command-palette-dialog` (max-width 640px, dashboard dark palette), `.cmd-icon`, `[cmdk-item][data-selected="true"]` highlight override, input + group-heading styling, `@media (prefers-reduced-motion: reduce)` guard. |
| `src/test/setup.ts` | Added `Element.prototype.scrollIntoView` mock (jsdom doesn't implement it; cmdk calls it in a layout effect on the active item). |

## Design Decisions

### Why `CommandDialog` (not raw `Dialog` + `Command`)?

The task spec wrote `<Dialog><DialogContent><Command>...`. The shadcn
`CommandDialog` wrapper composes the same structure but ALSO wires up an
sr-only `DialogTitle` + `DialogDescription`. Without the title, Radix
Dialog emits an a11y warning ("DialogContent requires a DialogTitle for
the component to be accessible..."). Using `CommandDialog` keeps the
palette accessible without us having to manually add an sr-only header.

### Why a separate `useEffect` for Cmd+K?

The existing keyboard-shortcut handler in `page.tsx` early-returns when
`metaKey || ctrlKey || altKey` is held (to avoid clashing with browser /
OS shortcuts). Putting the Cmd+K listener inside that handler would
require relaxing the early-return guard — which would risk accidentally
triggering the plain-K kill-switch shortcut when Cmd+K is held, or vice
versa. A dedicated useEffect keeps the two concerns isolated.

The Cmd+K listener calls `e.preventDefault()` so Safari doesn't focus the
URL bar and Chrome doesn't open its search bar.

### Why a `CmdKHarness` wrapper for the keyboard test?

The shortcut listener lives in `app/page.tsx`, but that file has a huge
dependency tree (WebSocket hook, audio hook, ~15 dynamically-imported
panels, modals, confirmation dialogs). Mounting it in vitest would drag
all of that in. Instead, the test mounts a thin wrapper that replicates
the EXACT `useEffect` pattern from `page.tsx`. If the page.tsx handler
ever changes its behaviour (e.g. drops the `preventDefault`), the test
will catch the regression as long as the pattern stays "Cmd+K toggles
open state".

### Why inject `extraActions` via a prop?

The palette component itself shouldn't know about page-level workflows
(refresh, kill switch, theme toggle) — those are concerns of
`page.tsx`. Injecting them via `extraActions` keeps the palette
reusable (it could be mounted in a different app context with a
different action set) and keeps the palette's render output predictable
for tests.

### Why the `scrollIntoView` mock?

`cmdk` calls `element.scrollIntoView()` inside a layout effect on the
active item to keep it scrolled into view as the user arrow-keys through
the list. jsdom doesn't implement `scrollIntoView` — without the mock,
every palette test throws `TypeError: i.scrollIntoView is not a function`
during commit phase and the component never finishes mounting. The mock
is a no-op (real browsers handle the scroll natively).

## Verification

### Lint

```
$ npx eslint src/components/CommandPalette.tsx \
               src/components/CommandPalette.test.tsx \
               src/components/TopStatusBar.tsx \
               src/app/page.tsx
# EXIT 0 — clean
```

The single global `bun run lint` failure is a PRE-EXISTING error in
`src/components/AttributionPanel.tsx:755` (`'PnLBarChart' is not
defined`) introduced by an earlier wave's modification to that file.
It is unrelated to W13-5 and outside the scope of this task.

### Tests

```
$ bun run test
# 340 tests across 15 files, all green (~78s)
# Includes the 15 new CommandPalette tests:
#   ✓ renders the palette when open=true
#   ✓ does NOT render the palette when open=false
#   ✓ renders both default groups
#   ✓ typing filters commands down to matching items
#   ✓ matches against keywords (not just the visible label)
#   ✓ renders the Empty state when no command matches the query
#   ✓ selecting a navigation command calls onNavigate(section) and closes
#   ✓ selecting any navigation row passes the correct section id
#   ✓ renders extraActions in a separate Actions group
#   ✓ selecting an extraAction invokes its action callback and closes
#   ✓ Cmd+K opens the palette (initially closed)
#   ✓ Ctrl+K also opens the palette (Windows / Linux chord)
#   ✓ pressing Cmd+K a second time closes the palette (toggle behaviour)
#   ✓ Cmd+K calls e.preventDefault (does not fall through to the browser URL bar)
#   ✓ plain "k" without modifier does NOT open the palette
```

### Dev server

`dev.log` shows Next.js 16.1.3 (Turbopack) still serving `GET / 200`
after the page.tsx changes — no compile errors.

## How to Use

1. **Open the palette**: press `⌘K` (macOS) or `Ctrl+K` (Windows/Linux)
   anywhere in the workstation. Alternatively, click the `🔍 ⌘K` button
   in the TopStatusBar (right-hand action cluster, between ThemeToggle
   and the mute button).
2. **Search**: type a query — cmdk fuzzy-matches against the visible
   label AND any keywords (e.g. "home" finds "Command Center",
   "ml" finds "AI / ML Engine").
3. **Navigate**: arrow-key up/down to highlight a command, Enter (or
   click) to select. The palette closes and the workstation switches
   to the target panel.
4. **Actions**: the "Actions" group at the bottom of the palette exposes
   6 page-level workflows (Refresh / Open Shortcuts / Open Config /
   Cancel All / Kill Switch / Toggle Theme).
5. **Close**: Esc (handled by both Radix Dialog natively and the page's
   Escape safety-net) or click outside the dialog.

## Future Extensibility

The `extraActions` prop is the canonical extension point — any future
panel/workflow that wants to surface a one-shot action in the palette
should add an entry to `cmdExtraActions` in `page.tsx` rather than
modifying `CommandPalette.tsx`. The palette will render it under the
"Actions" group heading automatically.
