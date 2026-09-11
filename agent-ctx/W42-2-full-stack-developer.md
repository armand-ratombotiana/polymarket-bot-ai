# W42-2 — full-stack-developer

**Task:** Add minimal "renders without crashing" tests for the 3
remaining untested frontend components:
`ErrorReporterInit`, `SWRegister`, `ThemeProvider`.

## Why these three were the last holdouts

All three are tiny client-only side-effect wrappers:

- `ErrorReporterInit.tsx` — returns `null`, calls
  `installErrorHandlers()` once on mount.
- `SWRegister.tsx` — returns `null`, calls
  `registerServiceWorker()` once on mount.
- `ThemeProvider.tsx` — wraps `next-themes`'s `NextThemesProvider`
  (class attribute, dark default, no system, instant transitions) and
  forwards `children`.

None has any visual output to assert against (the first two render
`null`; the third renders only what its children render), so the
minimal-but-meaningful contract is "renders without crashing in
jsdom" — i.e. the `useEffect` / provider wiring does not throw during
mount/unmount. Deeper behaviour assertions already live in the lib
test files (`lib/errorReporter.test.ts`, `lib/registerSW.test.ts`)
and the sibling `ThemeToggle.test.tsx` (which exercises the actual
theme-toggling path through the provider).

## Files added (3 new test files, additive only)

### `src/components/ErrorReporterInit.test.tsx` (1 test)
- Renders `<ErrorReporterInit />` in jsdom and asserts
  `container.firstChild === null` (the component renders `null`).
- Implicitly exercises the mount → `useEffect` →
  `installErrorHandlers()` → unmount path; if the side-effect wiring
  threw, the test would fail at `render()` rather than at the
  assertion.
- Does NOT mock `@/lib/errorReporter` — the real
  `installErrorHandlers` is import-safe in jsdom (it only registers
  window listeners, no network calls).

### `src/components/SWRegister.test.tsx` (1 test)
- Renders `<SWRegister />` in jsdom and asserts
  `container.firstChild === null`.
- Does NOT mock `@/lib/registerSW` — the real `registerServiceWorker`
  is import-safe in jsdom (it bails early when
  `'serviceWorker' in navigator` is false, which is the case in
  jsdom).

### `src/components/ThemeProvider.test.tsx` (1 test)
- Renders `<ThemeProvider><div>Test Child</div></ThemeProvider>` and
  asserts the child text appears via `screen.getByText`.
- Verifies the wrapper does not swallow children and that the
  `NextThemesProvider` mounts cleanly under jsdom (which requires
  the `matchMedia` polyfill already installed in `src/test/setup.ts`).
- Does NOT assert on the resolved `dark` class on `<html>` — that
  is the provider's contract with next-themes, not the wrapper's,
  and is already covered indirectly by `ThemeToggle.test.tsx`.

## Approach

1. Read all three source files first to confirm the contract under
   test:
   - `ErrorReporterInit` returns `null` + 1 useEffect.
   - `SWRegister` returns `null` + 1 useEffect.
   - `ThemeProvider` returns `<NextThemesProvider>…children…</NextThemesProvider>`.
2. Read `ThemeToggle.test.tsx` to mirror the provider-configuration
   shape (so the new ThemeProvider test doesn't accidentally diverge
   from the existing next-themes usage pattern).
3. Read `ErrorBoundary.test.tsx` for the `cleanup()` / no-mock
   pattern when testing side-effect-only components.
4. Wrote each test file using the exact code from the task spec,
   adding only a header comment explaining WHY the test is minimal
   and WHERE the deeper behaviour coverage already lives (so a
   future reader doesn't file a "this should test the SW
   registration" ticket).
5. Verified the real `installErrorHandlers` / `registerServiceWorker`
   imports are jsdom-safe (no `window.fetch` calls at import time,
   no serviceWorker API required) so the tests don't need module
   mocks that would themselves become a maintenance burden.

## Verification

- **Targeted run** —
  `TMPDIR=/dev/shm/vitest-tmp bun run test -- --run src/components/ErrorReporterInit.test.tsx src/components/SWRegister.test.tsx src/components/ThemeProvider.test.tsx`
  → 3 files / 3 tests pass (duration 2.87s).
- **Full suite** —
  `TMPDIR=/dev/shm/vitest-tmp bun run test` → **93 files / 1518 tests
  pass**. One uncaught `TypeError` is reported from
  `AIPredictionExplainerPanel.test.tsx` teardown
  (`explanation.explanation.top_features` undefined). This is
  **PRE-EXISTING** — called out by the W41-2 and W41-3 worklogs as
  out of scope and unrelated to this task. It does not cause any
  test to fail; vitest surfaces it as `Errors 1` after the suite
  finishes. The `bun run test` script exits with code 1 because of
  this single uncaught error, which matches the baseline behaviour
  before this task.
- **Lint** — `bun run lint` → EXIT 0, clean.
- **Untested-components count** —
  `comm -23 <(ls src/components/*.tsx | grep -v ".test." | grep -v ".skip" | grep -v ".stories" | sed 's|.*/||;s|\.tsx||' | sort) <(ls src/components/*.test.tsx 2>/dev/null | sed 's|.*/||;s|\.test\.tsx||' | sort) | wc -l`
  → **0**. Every top-level `.tsx` component in `src/components/` now
  has a sibling `.test.tsx`.

## Test-count delta

- Before: 90 files / 1515 tests (per W41-2 worklog baseline).
- After: 93 files / 1518 tests (+3 files / +3 tests), exactly matching
  the 3 new files added by this task.

## Caveats

- **The 3 new tests are deliberately "renders without crashing"-only.**
  A more ambitious test would assert that
  `installErrorHandlers` / `registerServiceWorker` were actually
  invoked (via `vi.spyOn`), but those calls are already covered by
  the lib unit tests, and asserting call-site invocation would
  couple the component test to the lib's internal call shape (a
  maintenance liability with no additional defect-detection power).
  The chosen contract ("mount does not throw, render output is
  `null`/children") is the minimum that catches the only realistic
  regression in these wrappers: a typo in the import path or a
  broken `useEffect` dependency array.

- **`AIPredictionExplainerPanel` uncaught error is pre-existing.**
  Not in scope for this task; W41-2 explicitly noted it as
  pre-existing. The full vitest suite reports
  `Test Files 93 passed (93) / Tests 1518 passed (1518) / Errors 1`,
  identical to the W41-3 baseline modulo the +3 new tests.

## Files touched

- `src/components/ErrorReporterInit.test.tsx` (NEW — 1 test)
- `src/components/SWRegister.test.tsx` (NEW — 1 test)
- `src/components/ThemeProvider.test.tsx` (NEW — 1 test)
- `agent-ctx/W42-2-full-stack-developer.md` (this work record)
- `worklog.md` (appended entry)
