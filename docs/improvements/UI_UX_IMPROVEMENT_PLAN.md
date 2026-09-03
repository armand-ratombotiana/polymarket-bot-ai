# UI / UX — Improvement Plan

- **Domain:** Frontend UI/UX (command center, deep analysis
  workstation, functional verification, design standard
  compliance)
- **Owning modules:** `src/components/*`, `src/app/page.tsx`,
  `src/app/globals.css`, `src/lib/*`, `src/hooks/*`,
  `src/components/charts/*`, `src/components/ui/*`,
  `e2e/*.spec.ts`, `playwright.config.ts`
- **Source authority:** God Mode §35 (command center), §43 (deep
  analysis workstation), §48 (UI functional verification), §49
  (design standard compliance).
- **Priority classification (per God Mode §64):**
  - P1 — UI functional verification (regression-prone surface).
  - P2 — command center, deep analysis workstation, design
    standard compliance.
- **Status as of W17-9:** IN PROGRESS — see per-improvement
  status below.

This plan defines every improvement in the UI/UX domain using the
per-improvement field set required by God Mode §63. Each
improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement UI-1 — Command Center Enhancements (§35)

- **Problem:** `src/app/page.tsx` ships a Command Center (the
  default `activeSection='command'` panel) with KPI cards
  (balance, P&L, win rate, expectancy), a recent-fills feed,
  and a strategy-status matrix. However, several operator-grade
  features are missing: (a) no "active alerts" surface (the
  `core/alerting.py` alerts live on the backend but the command
  center doesn't surface them); (b) no system-health summary
  (the W9-7 health metrics exist but are buried in the
  ObservabilityPanel); (c) no per-strategy live edge gauge (the
  strategy matrix shows P&L but not "is this strategy currently
  profitable per trade?"); (d) no quick-action toolbar (the
  operator must navigate to a sub-panel to kill-switch, pause a
  strategy, or refresh data).
- **Evidence:**
  - `src/app/page.tsx` — Command Center renders 4 KPI cards +
    a recent-fills table + the strategy matrix.
  - `src/components/CommandPalette.tsx` (W13-5) — 25 nav entries
    + 6 page actions; no quick-action buttons on the Command
    Center itself.
  - `src/components/SystemHealthView.tsx` (W8-10) — buried in
    System group; not surfaced on the Command Center.
- **Current State:** 4 KPI cards + recent fills + strategy
  matrix. No alerts, no health summary, no edge gauge, no
  quick-action toolbar.
- **Desired State:**
  1. **Alert banner** at the top of the Command Center — shows
     the latest un-acked alert (red/yellow/green dot + message +
     ack button).
  2. **System-health mini-strip** — 5 mini-gauges (CPU, memory,
    DB query latency, WS connections, gateway latency) inline
    under the KPI cards.
  3. **Per-strategy live edge gauge** — each strategy in the
     matrix shows a green/red dot indicating "last trade was
     profitable" + a small sparkline of the last 20 trades' edge.
  4. **Quick-action toolbar** — 4 buttons inline: Refresh All,
    Pause All Strategies, Activate Kill Switch, Open Command
    Palette (Cmd+K).
- **Proposed Solution:**
  1. New `AlertBanner.tsx` component — fetches
     `/api/alerts?status=active&limit=1` every 30 s.
  2. New `SystemHealthStrip.tsx` — fetches `/api/observability/
     latest?category=system` every 30 s.
  3. Extend `StrategyMatrix.tsx` — per-row edge gauge + sparkline.
  4. New `QuickActionsToolbar.tsx` — 4 buttons with confirm
     modals for the destructive ones.
  5. Wire all 4 into `page.tsx`'s Command Center case.
- **Architecture:**
  ```
  page.tsx (activeSection='command')
    ├─→ <AlertBanner />           ← /api/alerts?status=active&limit=1
    ├─→ <KpiCards />              ← (existing)
    ├─→ <SystemHealthStrip />      ← /api/observability/latest
    ├─→ <QuickActionsToolbar />    ← pause-all / kill-switch / refresh / palette
    ├─→ <StrategyMatrix />         ← (existing + edge gauge + sparkline)
    └─→ <RecentFills />            ← (existing)
  ```
- **Implementation:**
  1. `AlertBanner.tsx` — uses `useNotifications` hook + 30-s
     poll.
  2. `SystemHealthStrip.tsx` — 5 `GaugeChart` primitives.
  3. Extend `StrategyMatrix.tsx` — per-row `Sparkline` + edge dot.
  4. `QuickActionsToolbar.tsx` — 4 buttons with
     `ConfirmationDialog` wrappers for destructive actions.
  5. Wire into `page.tsx`.
- **Files Affected:**
  - `src/components/AlertBanner.tsx` (new)
  - `src/components/SystemHealthStrip.tsx` (new)
  - `src/components/QuickActionsToolbar.tsx` (new)
  - `src/components/StrategyMatrix.tsx` (extend)
  - `src/app/page.tsx` (extend Command Center case)
  - `src/components/AlertBanner.test.tsx` (new)
  - `src/components/SystemHealthStrip.test.tsx` (new)
  - `src/components/QuickActionsToolbar.test.tsx` (new)
- **Dependencies:** None (all data sources exist).
- **Risk:** LOW — additive; no destructive changes.
- **Priority:** P2 (polish).
- **Expected Benefit:**
  - Operators see alerts without navigating to the Observability
    panel.
  - System health is at-a-glance.
  - Per-strategy live edge gauges surface decay before it shows
    up in P&L.
  - Quick actions reduce mean-time-to-respond.
- **Tests:** +18 tests covering each new component, the polling,
  the destructive-action confirm flow, the empty-state path.
- **Metrics:**
  - `command_center_render_ms` histogram.
  - `command_center_quick_action_total{action}` counter.
  - `command_center_alert_ack_lag_seconds` histogram.
- **Acceptance Criteria:**
  - All 18 new tests pass.
  - The Command Center renders within 500 ms of opening.
  - A simulated alert appears in the banner within 30 s.
- **Status:** IN PROGRESS.

---

## Improvement UI-2 — Deep Analysis Workstation (§43)

- **Problem:** `src/components/DeepAnalysisView.tsx` (W8-1, W13-9
  enhanced) ships the per-token deep-analysis view: order book
  ladder, market depth chart, price history chart, ML edge
  panel, one-click Trade button. However, several deep-analysis
  features are missing: (a) no SHAP feature-attribution panel
  (ML-3 will land it on the backend; the panel doesn't exist); (b)
  no correlation matrix for the token vs other tracked tokens
  (the W16-6 correlation matrix exists on the backend but no
  UI); (c) no backtest-on-this-token button (the BacktestLab
  exists but is not parameterised per token); (d) no
  position-context overlay (if the bot has a position on this
  token, the entry price + unrealized P&L should overlay on
  the price history chart).
- **Evidence:**
  - `src/components/DeepAnalysisView.tsx` — renders 4 panels
    (book, depth, history, ML edge).
  - `src/components/charts/CorrelationMatrix.tsx` (W16-6) —
    exists but used only in `PortfolioRiskPanel.tsx`.
  - `src/components/BacktestLabView.tsx` (W8-10) — separate
    panel; not parameterised per token.
- **Current State:** 4 panels per token. No SHAP, no
  correlation, no per-token backtest, no position overlay.
- **Desired State:**
  1. **SHAP panel** — `FeatureAttributionPanel.tsx` (from ML-3)
    embedded in the deep-analysis view.
  2. **Correlation matrix** — `CorrelationMatrix.tsx` embedded,
    showing the current token vs the top 10 most-correlated
    tracked tokens.
  3. **Per-token backtest button** — opens the Backtest Lab
    pre-parameterised to the current token.
  4. **Position overlay** — if the bot has a position on this
    token, draw the entry-price line + unrealized-P&L band on
    the price history chart.
- **Proposed Solution:**
  1. Embed `FeatureAttributionPanel.tsx` in `DeepAnalysisView`.
  2. Embed `CorrelationMatrix.tsx` (refactor to accept a
    `focus_token` parameter).
  3. Extend `BacktestLabView` to accept a `token_id` query
    parameter.
  4. Extend `PriceHistoryChart.tsx` to render the position
    overlay.
- **Architecture:**
  ```
  DeepAnalysisView (per token)
    ├─→ <OrderBookLadder />             ← (existing)
    ├─→ <MarketDepthChart />            ← (existing)
    ├─→ <PriceHistoryChart>             ← (existing + position overlay)
    │     └─→ if position exists:
    │          entry_price horizontal line + unrealized P&L band
    ├─→ <MLEdgePanel />                 ← (existing)
    ├─→ <FeatureAttributionPanel />     ← new (token-scoped)
    ├─→ <CorrelationMatrix focusToken={token_id} /> ← new
    └─→ <BacktestThisTokenButton />     ← new
         └─→ onClick: navigate to /backtest-lab?token_id=...
  ```
- **Implementation:**
  1. Extend `DeepAnalysisView.tsx` with the 4 new sub-panels.
  2. Refactor `CorrelationMatrix.tsx` to accept `focusToken`.
  3. Extend `PriceHistoryChart.tsx` with the position overlay.
  4. Add the backtest-on-this-token button.
- **Files Affected:**
  - `src/components/DeepAnalysisView.tsx` (extend)
  - `src/components/charts/CorrelationMatrix.tsx` (extend)
  - `src/components/charts/PriceHistoryChart.tsx` (extend)
  - `src/components/BacktestLabView.tsx` (accept token_id param)
  - `src/components/FeatureAttributionPanel.tsx` (new — shared
    with ML-3)
  - `src/components/DeepAnalysisView.test.tsx` (extend)
- **Dependencies:** ML-3 (SHAP backend), DP-1 (correlation
  matrix on the backend — the W16-6 module is implemented).
- **Risk:** LOW — additive.
- **Priority:** P2 (analytics UX).
- **Expected Benefit:**
  - Operators get the full per-token picture in one view.
  - SHAP panel answers "why is the model bullish on this token?"
  - Correlation matrix surfaces diversification opportunities /
    concentration risks.
  - Position overlay replaces mental arithmetic.
- **Tests:** +14 tests covering each new sub-panel, the position
  overlay, the backtest-button navigation.
- **Metrics:**
  - `deep_analysis_render_ms` histogram.
  - `deep_analysis_backtest_button_total` counter.
  - `deep_analysis_position_overlay_active` gauge.
- **Acceptance Criteria:**
  - All 14 new tests pass.
  - The Deep Analysis view renders all 7 sub-panels within 1 s.
  - The position overlay shows correctly when the bot has a
    position.
- **Status:** IN PROGRESS.

---

## Improvement UI-3 — UI Functional Verification (§48)

- **Problem:** `e2e/dashboard.spec.ts`, `e2e/navigation.spec.ts`,
  `e2e/api-health.spec.ts` (W11-1) ship 38 E2E tests covering
  the app shell + navigation + API health. However, there is no
  per-panel functional verification — the tests don't open the
  PositionsPanel, click Close, and assert the order is
  submitted. They don't open the StrategyConfigModal, change a
  config value, and assert it persists. They don't open the
  LiveSafetyGatePanel, attempt to enable live trading, and
  assert the 409 response.
- **Evidence:**
  - `e2e/*.spec.ts` — 38 tests, all in 3 files. Coverage:
    app shell, navigation, API health.
  - `src/components/*.test.tsx` — 459+ unit tests at the
    component level (vitest + RTL). But unit tests don't catch
    integration regressions (e.g. a panel renders but the
    action button doesn't fire the right API call).
  - `FINAL_SYSTEM_REASSESSMENT.md` §4 lists "per-panel E2E
    coverage" as a residual risk.
- **Current State:** 38 E2E tests covering the shell; 0 per-panel
  functional tests.
- **Desired State:**
  1. Per-panel E2E test files: `e2e/positions.spec.ts`,
     `e2e/orders.spec.ts`, `e2e/markets.spec.ts`,
     `e2e/strategy-matrix.spec.ts`, `e2e/deep-analysis.spec.ts`,
     `e2e/backtest-lab.spec.ts`, `e2e/observability.spec.ts`,
     `e2e/execution-quality.spec.ts`, `e2e/live-safety-gate.spec.ts`.
  2. Each panel has at least 3 functional tests: (a) panel
     renders, (b) primary action fires the expected API call,
     (c) error state is handled.
  3. E2E suite grows from 38 → ~80 tests.
  4. CI runs E2E on every PR (already wired in
     `.github/workflows/ci.yml`).
- **Proposed Solution:**
  1. New test files under `e2e/`.
  2. Shared fixtures: a `mockBackend` Playwright fixture that
     intercepts `/api/*` and returns canned responses (so the
     tests don't depend on a running backend).
  3. Per-panel test authoring guide in `docs/UI_FUNCTIONAL_VERIFICATION.md`
     (new).
- **Architecture:**
  ```
  e2e/positions.spec.ts
    ├─→ test: renders the panel
    ├─→ test: Close button fires POST /api/positions/{id}/close
    ├─→ test: filter dropdown filters the rows
    └─→ test: empty-state renders when no positions
  e2e/live-safety-gate.spec.ts
    ├─→ test: renders the 10-check grid
    ├─→ test: POST /api/live/enable returns 409 with the blocking list
    └─→ test: ack button fires POST /api/live/ack
  ```
- **Implementation:**
  1. New test files (9 panels * 3-4 tests each = ~30 tests).
  2. Shared `mockBackend` fixture in `e2e/fixtures.ts`.
  3. Authoring guide.
- **Files Affected:**
  - `e2e/positions.spec.ts` (new)
  - `e2e/orders.spec.ts` (new)
  - `e2e/markets.spec.ts` (new)
  - `e2e/strategy-matrix.spec.ts` (new)
  - `e2e/deep-analysis.spec.ts` (new)
  - `e2e/backtest-lab.spec.ts` (new)
  - `e2e/observability.spec.ts` (new)
  - `e2e/execution-quality.spec.ts` (new)
  - `e2e/live-safety-gate.spec.ts` (new)
  - `e2e/fixtures.ts` (new — mockBackend fixture)
  - `docs/UI_FUNCTIONAL_VERIFICATION.md` (new — authoring guide)
- **Dependencies:** None — purely additive to the test suite.
- **Risk:** LOW — additive; existing 38 tests preserved.
- **Priority:** P1 (regression prevention).
- **Expected Benefit:**
  - Per-panel regressions caught in CI before they reach the
    operator.
  - Documented contract per panel (the authoring guide).
  - Foundation for visual regression testing (next wave).
- **Tests:** the 30+ new tests ARE the deliverable.
- **Metrics:**
  - `e2e_tests_total` gauge.
  - `e2e_tests_passing_pct` gauge.
  - `e2e_suite_duration_seconds` histogram.
- **Acceptance Criteria:**
  - E2E suite grows to >= 80 tests.
  - All tests pass in CI on every PR.
  - The authoring guide is referenced by at least 1 PR that
    adds a new panel.
- **Status:** IN PROGRESS.

---

## Improvement UI-4 — Design Standard Compliance (§49)

- **Problem:** `src/app/globals.css` (S4, Wave 2; refined in
  W9-7, W13-4, W15-2) defines the design system: colour
  palette, type scale, elevation system, button-hover lift,
  themed scrollbar, focus-visible outlines, etc. However,
  compliance is not enforced — a contributor can add a new
  panel with hardcoded `#22c55e` instead of `var(--success)`,
  or a `padding: 12px` instead of `var(--space-3)`. Over time
  the design system erodes.
- **Evidence:**
  - `src/app/globals.css` — 600+ lines defining the system.
  - `docs/ACCESSIBILITY.md` (W9-7) — documents the a11y
    standards but is human-enforced.
  - No automated linter for design-token compliance.
  - Spot-check: `rg "#22c55e" src/components/` returns ~30 hits
    (hardcoded green instead of `var(--success)`).
- **Current State:** Design system defined; compliance manual.
- **Desired State:**
  1. **Stylelint** config with custom rules: no hardcoded
     colours (must use CSS variables); no hardcoded spacing
     (must use `var(--space-*)`); no `z-index` literals (must
     use `var(--z-*)`).
  2. **ESLint rule** (custom or via `eslint-plugin-css-modules`)
     that flags `<div style={{ color: '#22c55e' }}>`.
  3. **CI gate** — the linter runs on every PR; violations fail
     the build.
  4. **Migration script** — `scripts/replace_design_tokens.py`
     that automatically replaces hardcoded values with the
     equivalent design tokens (operator-reviewed diff).
  5. **Documentation** — `docs/DESIGN_STANDARD.md` (new) listing
     every token + when to use which variant.
- **Proposed Solution:**
  1. `.stylelintrc.json` + custom rule plugin.
  2. Custom ESLint rule (`no-inline-color`).
  3. CI step in `.github/workflows/ci.yml`.
  4. Migration script.
  5. Authoring guide.
- **Architecture:**
  ```
  .stylelintrc.json
    └─→ rules:
         no-hardcoded-colors: true
         no-hardcoded-spacing: true
         no-z-index-literals: true
         declaration-property-value-allowed-list: { color: [/var\(--.+\)/] }
  ESLint
    └─→ no-inline-color: flags style={{ color: '#22c55e' }}
  CI
    └─→ frontend job runs stylelint + eslint; both must pass
  scripts/replace_design_tokens.py
    └─→ for each .tsx/.css file:
         find hardcoded values → replace with var(--token)
         output a diff for operator review
  ```
- **Implementation:**
  1. `.stylelintrc.json` + `stylelint-config` extension.
  2. Custom ESLint rule.
  3. Migration script.
  4. CI integration.
  5. Authoring guide.
- **Files Affected:**
  - `.stylelintrc.json` (new)
  - `.stylelintrc.custom-rules/` (new — custom rule plugin)
  - `eslint.config.mjs` (extend with custom rule)
  - `scripts/replace_design_tokens.py` (new)
  - `.github/workflows/ci.yml` (extend frontend job)
  - `docs/DESIGN_STANDARD.md` (new)
  - `package.json` (add stylelint devDep)
  - Every `.tsx`/`.css` file with hardcoded values (migration
    script replaces them).
- **Dependencies:** None.
- **Risk:** MEDIUM — the migration script touches many files.
  Mitigation: operator-reviewed diff; the linter is warnings-
  only for 1 wave, then errors.
- **Priority:** P2 (polish).
- **Expected Benefit:**
  - Design system compliance enforced automatically.
  - Theme changes (e.g. dark→light) apply globally without
    hardcoded-colour patches.
  - New contributors don't need to memorise the design system
    to add a compliant panel.
- **Tests:** the linter IS the test. CI gate ensures 0
  violations.
- **Metrics:**
  - `design_token_violations_total` counter.
  - `design_token_migration_total` counter.
- **Acceptance Criteria:**
  - `bun run stylelint` exits 0.
  - `bun run lint` exits 0 (no inline-colour violations).
  - CI gate blocks any PR with new violations.
  - The migration script replaces >= 90 % of hardcoded values
    (the rest are operator-reviewed).
- **Status:** IN PROGRESS.
