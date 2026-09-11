# W14-2 — i18n system (full-stack-developer)

## Scope
Add internationalization (i18n) to the workstation frontend so the
trader can flip the UI between English (default) and French. Touches
the Sidebar nav labels + footer status + a new LocaleSwitcher in
TopStatusBar. Does NOT touch any backend code.

## Files created
- `src/messages/en.json` — 108 lines, 6 namespaces (nav, groups,
  common, status, positions, analytics).
- `src/messages/fr.json` — 108 lines, identical key set to en.json.
- `src/i18n/config.ts` — locale catalog + `getLocale`/`setLocale`
  primitives. SSR-safe (returns `defaultLocale` when `window` is
  undefined). try/catch wraps `localStorage` access for privacy mode.
- `src/i18n/request.ts` — `next-intl/server` request config. Pins
  server locale to `defaultLocale` so SSR payload matches first
  client render.
- `src/hooks/useTranslation.ts` — 76 lines. `useState('en')` initial
  → `useEffect` reconciles to persisted locale on mount →
  `useCallback`-memoised `t(key)` walks the messages object with
  key-fallback for missing/non-string leaves. `changeLocale` persists
  + flips in-memory state in one call.
- `src/components/LocaleSwitcher.tsx` — 44 lines. Compact 2-option
  `<select>` (EN / FR). Uses CSS variables (`--border`,
  `--text-secondary`) so it inherits the active theme. Deliberately
  not using shadcn Select (~12KB Radix portal) for a 2-option control.
- `src/hooks/useTranslation.test.ts` — 290 lines, 21 tests across 3
  describe blocks (config primitives, catalog parity, hook behavior).

## Files modified
- `src/components/Sidebar.tsx` — added `labelKey: string` field to
  NavItem + NavGroup interfaces, populated labelKey for all 25 nav
  items + 8 groups (capital group uses `groups.capital_group` since
  `nav.capital` is reserved for the nav-item label). Rendered
  `t(group.labelKey)` / `t(item.labelKey)` / `t('status.bot_active')`
  in the visible label, the collapsed-mode tooltip, and the footer.
  Did NOT touch: kbd shortcuts, icons, collapse toggle, sr-only
  hints, mobile drawer, aria-current logic.
- `src/components/TopStatusBar.tsx` — added `import LocaleSwitcher`
  + rendered `<LocaleSwitcher />` immediately after `<ThemeToggle />`
  in the right-side action cluster (appearance + language grouped).

## Verification
- `bun run lint` — clean (eslint . exits 0, zero warnings).
- `bun run test` for my changes + adjacent files — 144/144 pass
  across 8 test files (useTranslation 21/21, Sidebar 18/18, etc.).
- 2 pre-existing flaky test files (`errorReporter.test.ts`,
  `RateLimitPanel.test.tsx`) fail on `main` as well — verified via
  `git stash`. NOT caused by W14-2.

## Decisions / deviations from task spec
1. **fr.json extension**: task spec's fr.json was a partial translation
   missing several keys (`nav.decisions`, `nav.attribution`,
   `nav.execution`, `nav.closed`, `nav.capital`, `nav.shadow`,
   `nav.validation`, `nav.observability`, `nav.retention`, `nav.safety`,
   `groups.capital_group`, plus partial `common`/`positions`/`analytics`
   namespaces). Extended to a complete translation so:
   - the locale-parity test passes,
   - the trader never sees a raw key string in French UI.
2. **useTranslation useEffect deps**: used `[locale]` instead of `[]`
   to satisfy `react-hooks/exhaustive-deps` without an eslint-disable.
   Behavior is unchanged — the effect is a no-op when persisted value
   already matches in-memory state.
3. **Sidebar keeps `label` field**: kept the English `label` field as a
   fallback alongside the new `labelKey` for grep-ability + back-compat
   with any consumer reading `item.label` directly.

## Cross-task dependencies
- Reads from `/agent-ctx/W13-4-full-stack-developer.md` (theme system)
  for the TopStatusBar action-cluster layout pattern.
- No new tasks blocked by W14-2.
