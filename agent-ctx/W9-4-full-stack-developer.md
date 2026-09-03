# W9-4 — full-stack-developer

## Task
Set up vitest + @testing-library/react + jsdom and write tests for 4 key components: `api.ts`, `Sidebar.tsx`, `PositionsPanel.tsx`, `AnalyticsPanel.tsx`.

## Outcome
- 88 tests passing across 4 test files
- Lint clean
- No component source files modified (pure additive test infrastructure)

## Files created
- `vitest.config.ts` — jsdom env, globals=true, `@`→`./src` alias
- `src/test/setup.ts` — jest-dom, `fetch = vi.fn()`, `matchMedia` mock, `MockResizeObserver`, act-warning suppression, `afterEach(vi.restoreAllMocks)`
- `src/lib/api.test.ts` — 21 tests (gateway port logic, auth header injection, ws URL helpers)
- `src/components/Sidebar.test.tsx` — 18 tests (group rendering, button roles, active state, mobile backdrop, onMobileClose behaviour)
- `src/components/PositionsPanel.test.tsx` — 27 tests (positions render, YES/NO badges, color-coded P&L, Trade/Close buttons, empty state, filtering, CSV export)
- `src/components/AnalyticsPanel.test.tsx` — 22 tests (loading state, KPI labels, formatting, small-sample warning, error state, auth header)

## Files modified
- `package.json` — added `test` and `test:watch` scripts

## Notes for downstream agents
- The global `fetch = vi.fn()` is reset between tests via `afterEach(vi.restoreAllMocks)`; tests that need to assert on fetch call args should set `global.fetch = vi.fn()` in their own `beforeEach` (api.test.ts and AnalyticsPanel.test.tsx already do this).
- jsdom lacks `matchMedia` and `ResizeObserver` — both are mocked in setup.ts.
- Sidebar's nav buttons no longer have `role="menuitem"` (the file was modified by another Wave-9 agent during this task). Tests use `container.querySelectorAll('button.sidebar-item')` instead of `getAllByRole('menuitem')`.
- PositionsPanel is now wrapped in `React.memo` with a custom comparator. The Close button is **always** rendered (no longer conditional on `onClosePosition` being provided). The market-contract cell is now a `<button>` wrapper.
- AnalyticsPanel.tsx now uses `useCallback` for the fetcher and pauses polling when `document.hidden`. The default RTL environment is "visible", so polling starts normally in tests.
- `fmtPnl` uses Unicode U+2212 (MINUS SIGN) for negative values, not ASCII hyphen. Test fixtures use `\u2212` to match.
EOF
