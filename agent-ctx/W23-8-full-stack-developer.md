# W23-8 — E2E Test Suite Expansion

**Agent:** full-stack-developer
**Date:** 2026-09-04
**Scope:** Additive — 5 NEW E2E spec files + 1 config update.

## Files

- NEW `e2e/database.spec.ts`        (9 tests, 320+ lines)
- NEW `e2e/strategies.spec.ts`      (12 tests, 280+ lines)
- NEW `e2e/analytics-flows.spec.ts` (17 tests, 390+ lines)
- NEW `e2e/ml-flows.spec.ts`        (18 tests, 370+ lines)
- NEW `e2e/settings.spec.ts`        (23 tests, 470+ lines)
- EDIT `playwright.config.ts`      (testMatch, per-test/expect/action/nav timeouts, webServer 120s)

**Total new tests: 82** (was 102 in 11 files; now 184 in 16 files).

## Per-file summary

### `e2e/database.spec.ts` — Database Status flow
Covers the `system-database-status` panel (`DatabaseStatusPanel.tsx`,
W21-7). 9 tests across navigation, panel mount, backend indicator
(`[data-testid="db-backend-badge"]` showing PostgreSQL or SQLite),
PG health status display (`PostgreSQL Connection Health` card),
table list (table-or-empty-state poll), Retry PG Connection button
(`aria-label="Retry PostgreSQL connection"`), header Refresh button,
and the ErrorState card's own Retry button (conditional on backend
down — mirrors the `api-health.spec.ts` probe pattern).

### `e2e/strategies.spec.ts` — Strategy flow
Covers Strategy Registry (`StrategyMatrix.tsx`) + the analytics
Performance panel that hosts the per-strategy Leaderboard. 12 tests:
7 on the Strategy Registry (catalog header, category tabs, filter
input, card grid, Implemented Deploy/Stop controls — conditional on
`/api/strategies/catalog` reachable) + 5 on the Performance panel
(leaderboard header, ranked-rows-or-empty-state poll, AnalyticsPanel
KPI grid — conditional on `/api/analytics` reachable, cross-panel
error sweep).

### `e2e/analytics-flows.spec.ts` — Analytics flow (expanded)
Sibling file to `analytics.spec.ts` — that file covers the basic
"panel becomes active" smoke; THIS file goes deeper into each
panel's visible structure. 17 tests across 4 panel flows:
Performance (KPI tiles for P&L / Win Rate / Sharpe — conditional on
`/api/analytics`), Attribution (7-DIMENSION badge, summary KPI cards,
Dimensions/Waterfall/Strategies tabs, dimension regions), Execution
Quality (per-fill audit KPI strip, slippage histogram `role="img"`,
per-fill log table-or-empty-state), Closed Positions (ledger header,
KPI summary strip, ledger table-or-empty-state, Refresh + CSV export
controls) + cross-panel error sweep.

### `e2e/ml-flows.spec.ts` — ML flow (expanded)
Sibling file to `ml.spec.ts` — same depth-expansion pattern. 18 tests
across 3 panel flows: AI/ML Engine (telemetry header, model info
active-version badge + ensemble weights strip, 38-Feature Pipeline
badge, RF/GB/SGD/LightGBM member cards, Gated Retrain button),
ML Validation (validation header, governance+drift badge, metric
labels Brier/AUC/Log-loss/ECE/Accuracy — conditional on
`/api/ml/metrics`, Refresh control), Shadow Inference (shadow
header, Challenger Models section, champion badge — conditional on
`/api/ml/versions` reporting an active model, Register Challenger
button, comparison KPIs Total P&L/Win Rate/Sharpe) + cross-panel
error sweep.

### `e2e/settings.spec.ts` — Settings modal flow
Covers the W15-2 SettingsModal opened via the gear icon
(`aria-label="Open user preferences"`) in TopStatusBar. 23 tests
across 5 describe blocks:
- open/close (5 tests): gear trigger, Cancel button, Escape key,
  backdrop click, close button (✕).
- sections render (9 tests): all 6 sections in canonical order
  (Display, Dashboard, Trading, Notifications, Sound, Privacy) +
  per-section content assertions + Reset-to-defaults button presence
  + Save-changes-disabled-when-clean.
- theme toggle (2 tests): in-modal Theme Select exposes Dark + Light
  options; selecting opposite enables Save (dirty tracking).
- locale switcher (3 tests): in-modal Language Select options + the
  top-bar LocaleSwitcher flips the sidebar group label `Main` →
  `Principal` (and restores it afterwards) + in-modal dirty tracking.
- save/reset (2 tests): Save button enables after toggling
  Auto-refresh, then Save closes the modal (with restore-to-original
  cleanup); Reset replaces draft without closing.
- integration (2 tests): opening settings doesn't interrupt the
  active panel; no uncaught errors during open/interact/close.

### `playwright.config.ts` updates
- Added `testMatch: '**/*.spec.ts'` so only `.spec.ts` files in
  `testDir` are picked up (defensive against future scratch files).
- Added `timeout: 60_000` (per-test) — generous for lazy-loaded
  panel chunks (1–2s download each) + the `waitForTimeout(2000)`
  settles several W23-8 flow tests use.
- Added `expect: { timeout: 15_000 }` — the default 5s was too tight
  for `expect.poll` patterns (badge-visible OR error-state-visible).
- Added `actionTimeout: 30_000` + `navigationTimeout: 45_000` —
  explicit caps so a stuck action doesn't eat the whole per-test
  budget.
- Bumped `webServer.timeout` 60_000 → 120_000 — the dev server's
  first compile can take 8–12s on a cold sandbox; the previous 60s
  was tight when the host is under load from other test suites.
- Updated the file's docstring to document the new timeouts.

## Verification

### `bunx playwright test --list` ✓
184 tests in 16 files (was 102 in 11). 82 new tests across the 5
new files.

### Sample test runs (each verified to PASS individually)
- `e2e/strategies.spec.ts › can navigate to the Strategy Registry panel` ✓ (1.4m)
- `e2e/settings.spec.ts › all 6 sections render in canonical order` ✓ (50.6s)
- `e2e/database.spec.ts › can navigate to the Database status panel` ✓ (1.2m)
- `e2e/ml-flows.spec.ts › panel renders the model telemetry header` ✓ (1.3m)

(Cold-start compile takes ~44s on the sandbox; tests pass once the
dev server's first compile is done. A `timeout 90` bash command
killed the database test on its first run because the cold-compile
exceeded the command-level timeout; bumping to `timeout 150` and
re-running passed.)

### `bun run lint` ✓ (for the new files)
`bunx eslint e2e/*.spec.ts playwright.config.ts` exits 0 with no
output — all new files lint clean.

NOTE: `bun run lint` (full repo) flags 12 pre-existing
`react-hooks/static-components` errors in
`src/components/StrategyPerformancePanel.tsx` — this file was added
by a parallel W23 agent (git status shows `A` not committed) and is
NOT in this task's scope. The new E2E spec files + the playwright
config edit introduce zero new lint warnings.

## Test design conventions (matched existing E2E suite)

- Shared `beforeEach` waits for `.page-area` to be visible (45s timeout)
  — proxy for "client hydrated + at least the default panel mounted".
- Sidebar item button `aria-current="page"` is the canonical
  active-panel signal.
- PanelErrorBoundary fallback selectors (`.panel-error-boundary`,
  `.error-boundary-fallback`) — asserted to have count 0 after panel
  mount (regression guard against panel crashes).
- Backend may or may not be running — assert STRUCTURE not VALUES.
- Conditional tests probe the relevant `/api/...?XTransformPort=8080`
  endpoint first; if down, `test.skip(true, '...')` with a clear
  reason. Mirrors the `api-health.spec.ts` + `ml.spec.ts` pattern.
- Cross-panel "no uncaught page errors" sweep at the end of each
  file — captures `pageerror` events during a nav walk; failed
  fetches are NOT page errors (caught by per-panel try/catch).
- Each test file documents the component file under test + the
  relevant source line numbers as inline comments so future
  maintainers can trace selectors back to the JSX.

## No backend / DB / infra changes
Pure additive E2E test expansion. No changes to:
- Prisma schema
- API routes
- mini-services
- src/components/* (no component changes)
- Existing E2E spec files (no edits to dashboard/navigation/system/
  ml/analytics/trading/theme/error-handling/api-health/command-
  palette/responsive specs)

The only existing file touched is `playwright.config.ts` (timeout
bumps + testMatch + per-action/navigation timeouts).

## Stage summary
- 5 new E2E spec files (82 tests total)
- 1 config edit (timeouts + testMatch + docs)
- All new files lint clean
- Sample tests verified passing against the live dev server
- Test count: 102 → 184 (files: 11 → 16)
