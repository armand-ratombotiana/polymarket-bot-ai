# W28-3 — full-stack-developer — Tests for untested Wave 21-26 UI panels

## Task summary
Add minimal vitest + React Testing Library coverage for the six previously-
untested Wave 21-26 dashboard panels to push component-test coverage
beyond 50%.

## Context reviewed before starting
- Listed all `src/components/*.tsx` (95 components).
- Listed all `src/components/*.test.tsx` (28 test files at start).
- Cross-referenced against the spec's "high-priority" panel list:
  - **DatabaseStatusPanel** — already has tests (W21-7 work).
  - **PerformanceReportPanel** — already has tests (W26-2 work).
  - **AlertNotificationsPanel** — already has tests (W23-4 work).
  - **PortfolioRiskPanel** — already has tests.
  - **OrderFlowPanel** — NO test → created.
  - **RetentionPanel** — NO test → created.
  - **MLValidationPanel** — NO test → created.
  - **CapitalAllocatorPanel** — NO test → created.
  - **ShadowInferencePanel** — NO test → created.
  - **LiveSafetyGatePanel** — NO test → created.

## Files created (6 test files, 61 new tests total)
1. `src/components/OrderFlowPanel.test.tsx` — 10 tests
   - Prop-based panel (trades + orderBooks + isRealtime).
   - Mocks the per-token `/api/depth/{token_id}` poll on mount.
   - Covers: container render, header, chart card heading, imbalance +
     tape card headings, LIVE/POLL badge, empty-books placeholder,
     empty-trades-and-books smoke, signed Δ stat, depth fetch.
2. `src/components/RetentionPanel.test.tsx` — 10 tests
   - Mocks `GET /api/system/health` (60s poll).
   - Covers: container, header title, "Bounded-storage policy" badge,
     Refresh button, endpoint note, horizon note, loaded-state
     "Retention Policy by Store" table, "Retention backend unreachable"
     error state, never-resolve smoke, Authorization header.
3. `src/components/MLValidationPanel.test.tsx` — 10 tests
   - Mocks three parallel calls: `/api/ml/metrics`, `/api/ml/drift`,
     `/api/ml/versions`.
   - Covers: container, header title, "governance + drift" badge, three
     endpoint notes, never-resolve smoke, "ML validation backend
     unreachable" error, Refresh button, "Retrain Now" button (loaded
     state), "Drift OK" badge (loaded state), Authorization header.
4. `src/components/CapitalAllocatorPanel.test.tsx` — 10 tests
   - Mocks three calls: `/api/positions/closed`, `/api/capital/allocation`,
     `/api/exposure`.
   - Covers: container, header title, "Michaelis-Menten" badge,
     Refresh-button aria-label, Config-button aria-label, never-resolve
     smoke, "Allocator API unavailable" error, Retry button, "Edge → Size
     Saturating Curve" heading (loaded), Authorization header.
5. `src/components/ShadowInferencePanel.test.tsx` — 10 tests
   - Mocks four calls: `/api/ml/versions`, `/api/shadow/trades`,
     `/api/shadow/comparison`, `/api/ml/metrics`.
   - Covers: container, loading-state "Shadow Inference" title, "Loading…"
     badge, never-resolve smoke, loaded-state "Shadow Inference +
     Counterfactual Journal" header, Refresh-now button, Live/Paused
     polling toggle (title attribute), "Champion: 0xabc123" badge,
     "Unable to reach any shadow-inference backend" error, Authorization
     header.
6. `src/components/LiveSafetyGatePanel.test.tsx` — 11 tests
   - Mocks three calls: `/api/live/readiness`, `/api/status`,
     `/api/audit/logs`.
   - Covers: container, "LIVE SAFETY GATE · §82" title (loading skeleton),
     `animate-spin` spinner present, never-resolve smoke, "Safety-gate
     endpoint unavailable" error on HTTP 500, "Unavailable" badge, Retry
     button, "Run all checks" button (loaded), "Force open" + "Force
     close" buttons (loaded), OPEN gate badge, Authorization header.

## Strategy notes (mirrors existing patterns in the repo)
- `global.fetch` is mocked per-test via `vi.mocked(fetch).mockImplementation`
  — the project's `src/test/setup.ts` already installs
  `global.fetch = vi.fn()` globally, so individual tests just override the
  implementation per test via `beforeEach`.
- `apiFetch` (from `src/lib/api.ts`) wraps `fetch` and adds the
  `Authorization: Bearer ...` header — so mocking `global.fetch` directly
  is sufficient to cover both URL routing and the auth-header assertion.
- For panels that fire multiple parallel fetches, the fetch mock switches
  its returned payload based on the URL substring, so all parallel
  promises resolve with the right shape and the loaded state can be
  asserted via `waitFor`.

## Gotchas worth recording for future panel-test authors
1. **Multiple identical text matches.** `OrderFlowPanel` renders the
   "0/min" tape-speed value in BOTH the top stats bar AND inside the
   `OrderBookImbalance` meter when no depth is loaded. Use
   `getAllByText('0/min')` and `expect(...).toBeGreaterThanOrEqual(1)`
   rather than `getByText`, which throws on duplicate matches.
2. **Retrain button only renders after data loads.** `MLValidationPanel`'s
   "Retrain Now" button lives in the loaded-state body (not the header).
   Tests asserting its presence must wait for the metrics fetch to
   resolve first — wrap `getByRole('button', { name: /retrain now/i })`
   in `waitFor`.

## Verification results
- `bun run lint` — clean (0 errors, 0 warnings).
- `bun run test` — all **1176 tests** pass across **59 test files**.
- Test-file count grew from 28 → 34 component test files
  (6 new test files added).

## Worklog entry
Appended to `/home/z/my-project/worklog.md` (separator `---` + standard
format).
