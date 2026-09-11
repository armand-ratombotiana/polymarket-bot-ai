# Master Implementation Plan — Backlog

- **Document owner:** Platform orchestrator
- **Date created:** 2026-09-03 (W17-9)
- **Source authority:** God Mode §65 (implementation backlog
  format), §69 (statuses).
- **Last revised:** 2026-09-03
- **Scope:** Every actionable task derived from the per-domain
  improvement plans in `docs/improvements/`.
- **Related docs:**
  - `docs/improvements/MASTER_IMPROVEMENT_ROADMAP.md`
  - `docs/improvements/*_IMPROVEMENT_PLAN.md` (per-domain
    source-of-truth)
  - `docs/implementation/IMPLEMENTATION_STATUS.md` (live status
    dashboard — counts derived from this file)

This file is the canonical backlog. Every task has an `IMPL-*` ID
and the per-task field set required by God Mode §65:
`ID, Domain, Priority, Problem, Evidence, Implementation, Files,
Dependencies, Tests, Acceptance Criteria, Status`.

**Status lifecycle (per §69):** `TODO` → `IN_ANALYSIS` →
`IMPLEMENTING` → `TESTING` → `DONE`. Blocked tasks can be in any
state with `BLOCKED:` prefix. `DONE` tasks graduate to
`IMPLEMENTATION_STATUS.md`'s completed log.

**Task ID scheme:** `IMPL-<DOMAIN_PREFIX>-<NUMBER>` where the
domain prefix is 2-3 chars (BE = Bot Execution, ML = AI/ML, DP =
Data Platform, ST = Strategy Management, BT = Backtest, UI =
UI/UX, RP = Risk/Portfolio, OB = Observability).

---

## A. Bot Execution (BE)

### IMPL-BE-1 — OSM transition history + ledger emission

- **Domain:** Bot Execution
- **Priority:** P0
- **Problem:** OSM transitions are not recorded in the decision
  ledger; the `PARTIAL → CANCELLED → late-fill` race is possible.
- **Evidence:** `tests/test_order_state_machine.py` (8 tests,
  U6 Wave 4) — happy path + InvalidTransition only.
  `FINAL_SYSTEM_REASSESSMENT.md` §4.
- **Implementation:** See improvement plan BE-1 (Bot Execution).
- **Files:** `core/order_state_machine.py`, `core/decision_ledger.py`,
  `tests/test_order_state_machine.py`, `tests/test_decision_ledger.py`.
- **Dependencies:** IMPL-BE-5 (idempotency key primitive).
- **Tests:** 8 existing + 12 new = 20.
- **Acceptance Criteria:** All 20 OSM tests pass; `osm_invalid_transitions_total == 0` over 24 h paper session.
- **Status:** TODO

### IMPL-BE-2 — Execution quality benchmarks + VWAP table

- **Domain:** Bot Execution
- **Priority:** P1
- **Problem:** No post-fill benchmarks; no maker/taker split; no
  VWAP comparison.
- **Evidence:** `tests/test_execution_quality.py` (13 tests, T12
  Wave 3). `src/components/ExecutionQualityPanel.tsx` (W8-3).
- **Implementation:** See improvement plan BE-2.
- **Files:** `core/execution_quality.py`, `core/observability_collector.py`, `migrations/0XX_execution_quality_benchmarks.sql`, `tests/test_execution_quality.py`, `src/components/ExecutionQualityPanel.tsx`.
- **Dependencies:** IMPL-DP-3 (data quality monitor for orderbook_ticks).
- **Tests:** 13 existing + 9 new = 22.
- **Acceptance Criteria:** All 22 tests pass; >= 95 % of fills have non-NULL `benchmark_price_1m` after 24 h.
- **Status:** TODO

### IMPL-BE-3 — Smart router integration (TWAP/VWAP/iceberg)

- **Domain:** Bot Execution
- **Priority:** P1
- **Problem:** `execution/advanced_router.py` exists but is not
  wired into the live order path.
- **Evidence:** `tests/test_advanced_router.py` (12 tests, W16-9);
  no caller in `strategies/*.py` (verified via Grep).
- **Implementation:** See improvement plan BE-3.
- **Files:** `core/order_state_machine.py`, `execution/advanced_router.py`, `execution/smart_router.py`, `strategies/base.py`, `strategies/signal_trader.py`, `api/server.py`, `tests/test_advanced_router.py`, `tests/test_signal_trader.py`.
- **Dependencies:** IMPL-BE-1 (OSM parent/child), IMPL-BE-2
  (execution_quality routing_policy column).
- **Tests:** 12 existing + 13 new = 25.
- **Acceptance Criteria:** All 25 tests pass; live safety gate `smart_router_integrated` check passes.
- **Status:** IN_ANALYSIS

### IMPL-BE-4 — Circuit breaker registry + 5 rules

- **Domain:** Bot Execution
- **Priority:** P0
- **Problem:** Only 2 breakers (per-trade, external-API); no
  cross-strategy correlation, latency, or reconnect-storm breaker.
- **Evidence:** `tests/test_circuit_breaker.py` (9 tests).
- **Implementation:** See improvement plan BE-4.
- **Files:** `core/circuit_breaker.py`, `core/observability_collector.py`, `strategies/base.py`, `risk/routes.py`, `tests/test_circuit_breaker.py`, `src/components/BreakersPanel.tsx` (new).
- **Dependencies:** None.
- **Tests:** 9 existing + 19 new = 28.
- **Acceptance Criteria:** All 28 breaker tests pass; pause-all fires within 15 s when 3 strategies trip.
- **Status:** IN_ANALYSIS

### IMPL-BE-5 — Idempotency middleware + ledger dedup

- **Domain:** Bot Execution
- **Priority:** P0
- **Problem:** No idempotency keys on mutating endpoints; duplicate
  submissions create duplicate orders.
- **Evidence:** `FINAL_SYSTEM_REASSESSMENT.md` §4.
- **Implementation:** See improvement plan BE-5.
- **Files:** `core/idempotency.py` (new), `core/decision_ledger.py`, `core/settlement.py`, `api/server.py`, `migrations/0XX_idempotency.sql` (new), `tests/test_idempotency.py` (new), `tests/test_decision_ledger.py`, `tests/test_settlement.py`.
- **Dependencies:** None (foundational).
- **Tests:** +20 new.
- **Acceptance Criteria:** All idempotency tests pass; same Idempotency-Key returns same response within 5 ms.
- **Status:** TODO

---

## B. AI / ML (ML)

### IMPL-ML-1 — Feature store v2 (online/offline + schema registry)

- **Domain:** AI/ML
- **Priority:** P1
- **Problem:** Single feature_vectors table; no online/offline split; implicit schema.
- **Evidence:** `tests/test_feature_store.py` (13 tests, W16-2).
- **Implementation:** See improvement plan ML-1.
- **Files:** `ml/feature_store.py`, `ml/features.py`, `ml/model.py`, `migrations/0XX_feature_store_v2.sql`, `tests/test_feature_store.py`.
- **Dependencies:** IMPL-DP-5 (feature versioning — overlaps; ML-1 supersedes).
- **Tests:** 13 existing + 15 new = 28.
- **Acceptance Criteria:** Online lookup p95 < 100 ms; schema drift triggers WARNING within 1 s.
- **Status:** IN_ANALYSIS

### IMPL-ML-2 — Calibration daily refresh + history

- **Domain:** AI/ML
- **Priority:** P2
- **Problem:** Calibration fit once at training time; never refreshed.
- **Evidence:** `tests/test_calibration.py` (8 tests, Wave 6); `src/components/charts/ReliabilityDiagram.tsx`.
- **Implementation:** See improvement plan ML-2.
- **Files:** `ml/calibration.py`, `ml/training_orchestrator.py`, `migrations/0XX_calibration_history.sql`, `ml/routes.py`, `src/components/charts/ReliabilityDiagram.tsx`, `src/components/MLPanel.tsx`, `tests/test_calibration.py`.
- **Dependencies:** IMPL-ML-1 (feature store), IMPL-ML-3 (SHAP).
- **Tests:** 8 existing + 10 new = 18.
- **Acceptance Criteria:** Daily refresh runs at 04:00 UTC; ReliabilityDiagram renders both curves.
- **Status:** TODO

### IMPL-ML-3 — Inline SHAP + aggregate attribution panel

- **Domain:** AI/ML
- **Priority:** P2
- **Problem:** SHAP computed on demand only; not stored; not in decision ledger UI.
- **Evidence:** `tests/test_explainability.py` (9 tests, W16-3).
- **Implementation:** See improvement plan ML-3.
- **Files:** `ml/explainability.py`, `ml/model.py`, `core/decision_ledger.py`, `migrations/0XX_decision_shap.sql`, `ml/routes.py`, `src/components/DecisionLedgerPanel.tsx`, `src/components/FeatureAttributionPanel.tsx` (new), `tests/test_explainability.py`.
- **Dependencies:** IMPL-ML-1 (feature store schema).
- **Tests:** 9 existing + 9 new = 18.
- **Acceptance Criteria:** Every PREDICTION ledger row has a decision_shap row (after flag flip).
- **Status:** IN_ANALYSIS

### IMPL-ML-4 — A/B testing N-variant + auto-promotion

- **Domain:** AI/ML
- **Priority:** P2
- **Problem:** 2-variant only; manual flip; no stopping rule; no UI.
- **Evidence:** `tests/test_ab_testing.py` (10 tests, W14-5).
- **Implementation:** See improvement plan ML-4.
- **Files:** `ml/ab_testing.py`, `ml/model.py`, `api/server.py`, `src/components/ABTestingPanel.tsx` (new), `src/components/Sidebar.tsx`, `tests/test_ab_testing.py`.
- **Dependencies:** IMPL-ML-7 (shadow inference promotion gate).
- **Tests:** 10 existing + 12 new = 22.
- **Acceptance Criteria:** Experiment with clear winner auto-stops within 1 h.
- **Status:** TODO

### IMPL-ML-5 — Per-feature drift detection

- **Domain:** AI/ML
- **Priority:** P1
- **Problem:** Model-level drift only; no per-feature drift; baseline is warmup not training-set.
- **Evidence:** `tests/test_drift_detector.py` (7 tests, W6).
- **Implementation:** See improvement plan ML-5.
- **Files:** `ml/drift_detector.py`, `ml/model_registry.py`, `ml/routes.py`, `src/components/MLPanel.tsx`, `tests/test_drift_detector.py`.
- **Dependencies:** IMPL-ML-1 (feature store schema).
- **Tests:** 7 existing + 11 new = 18.
- **Acceptance Criteria:** Per-feature drift table renders; > 50 % features MODERATE → escalate within 5 min.
- **Status:** IN_ANALYSIS

### IMPL-ML-6 — Label backfill automation

- **Domain:** AI/ML
- **Priority:** P1
- **Problem:** Manual trigger; no retry; no dedup; no auto-retrain.
- **Evidence:** `tests/test_label_backfill.py` (7 tests, W5).
- **Implementation:** See improvement plan ML-6.
- **Files:** `core/label_backfill.py`, `ml/training_orchestrator.py`, `migrations/0XX_label_backfill_failures.sql`, `api/server.py`, `tests/test_label_backfill.py`.
- **Dependencies:** IMPL-DP-3 (data quality — Gamma API monitor).
- **Tests:** 7 existing + 9 new = 16.
- **Acceptance Criteria:** Daily backfill runs at 03:00 UTC; auto-retrain triggers when n_new > 100.
- **Status:** IN_ANALYSIS

### IMPL-ML-7 — Shadow inference promotion gate

- **Domain:** AI/ML
- **Priority:** P0
- **Problem:** No automated promotion gate; manual promotion can promote a worse model.
- **Evidence:** `tests/test_shadow_inference.py` (6 tests, W7); live safety gate has no promotion-gate check.
- **Implementation:** See improvement plan ML-7.
- **Files:** `ml/shadow_inference.py`, `core/live_safety_gate.py`, `api/server.py`, `tests/test_shadow_inference.py`, `tests/test_live_safety_gate.py`.
- **Dependencies:** IMPL-ML-4 (A/B auto-promotion hook).
- **Tests:** 6 existing + 12 new = 18.
- **Acceptance Criteria:** Live safety gate reports 6/10 (was 4/10); no model can be promoted without gate's blessing.
- **Status:** TODO

---

## C. Data Platform (DP)

### IMPL-DP-1 — PostgreSQL/TimescaleDB migration

- **Domain:** Data Platform
- **Priority:** P1
- **Problem:** SQLite cannot support multi-process, fast time-series, continuous aggregates.
- **Evidence:** `core/timescale_db.py` standby adapter (V-wave 5); `core/db_pool.py` (W16-7).
- **Implementation:** See improvement plan DP-1.
- **Files:** `core/db_pool.py`, `core/timescale_db.py`, `core/data_store.py`, `core/decision_ledger.py`, `core/observability.py`, `core/execution_quality.py`, `core/closed_positions.py`, `core/shadow_trading.py`, `core/market_db.py`, `core/audit_logger.py`, `core/async_repositories.py`, `alembic.ini`, `migrations/versions/*.py`, `scripts/migrate_sqlite_to_postgres/*.py`, `docker-compose.yml`, `requirements.txt`, `tests/conftest.py`, `tests/test_async_db.py`.
- **Dependencies:** None (foundational).
- **Tests:** All existing tests pass against Postgres + 20 new migration tests.
- **Acceptance Criteria:** 7-day dual-write with 0 drift; `orderbook_ticks` 30-day VWAP < 100 ms p95.
- **Status:** TODO

### IMPL-DP-2 — Inbound WebSocket ingestion

- **Domain:** Data Platform
- **Priority:** P1
- **Problem:** 2-s poll-based ingestion; tick-to-trade latency bounded by poll.
- **Evidence:** `core/book_poller.py` (Wave 7, 2-s loop); `core/ws_client.py` (Wave 8, outbound only).
- **Implementation:** See improvement plan DP-2.
- **Files:** `core/ws_client.py`, `core/book_poller.py`, `core/circuit_breaker.py`, `tests/test_ws_client.py`, `tests/test_book_poller.py`.
- **Dependencies:** IMPL-DP-1 (Postgres — 10x more rows).
- **Tests:** +12 new.
- **Acceptance Criteria:** Tick-to-trade p95 < 200 ms; book_poller fallback activates within 60 s of WS failure.
- **Status:** TODO

### IMPL-DP-3 — Data quality monitor

- **Domain:** Data Platform
- **Priority:** P0
- **Problem:** No data-freshness / anomaly / reconciliation monitoring.
- **Evidence:** `FINAL_SYSTEM_REASSESSMENT.md` §3.5.
- **Implementation:** See improvement plan DP-3.
- **Files:** `core/data_quality.py` (new), `core/reconciliation.py`, `core/config.py`, `core/alerting.py`, `ml/training_orchestrator.py`, `api/server.py`, `migrations/0XX_data_quality_results.sql` (new), `src/components/DataQualityPanel.tsx` (new), `src/components/Sidebar.tsx`, `tests/test_data_quality.py` (new).
- **Dependencies:** IMPL-DP-1 (Postgres — faster freshness queries), IMPL-ML-6 (label backfill retry).
- **Tests:** +20 new.
- **Acceptance Criteria:** Simulated stale-table scenario emits alert within 5 min.
- **Status:** TODO

### IMPL-DP-4 — Retention optimization (per-table + archive + aggregate)

- **Domain:** Data Platform
- **Priority:** P2
- **Problem:** Global 7/30/90-day pruning; no archive; no aggregation.
- **Evidence:** `tests/test_retention.py` (22 tests, U8 Wave 4).
- **Implementation:** See improvement plan DP-4.
- **Files:** `core/retention.py`, `core/config.py`, `migrations/0XX_orderbook_ticks_1m.sql` (new), `tests/test_retention.py`, `scripts/db-maintenance.sh`.
- **Dependencies:** IMPL-DP-1 (Postgres — TimescaleDB continuous aggregates).
- **Tests:** 22 existing + 10 new = 32.
- **Acceptance Criteria:** After 1 week, `data/archive/` has Parquet files; `orderbook_ticks_1m` populated within 1 h of cron.
- **Status:** IN_ANALYSIS

### IMPL-DP-5 — Feature schema registry + diff

- **Domain:** Data Platform
- **Priority:** P1
- **Problem:** Implicit schema; adding a feature silently breaks older models.
- **Evidence:** `tests/test_feature_store.py` (13 tests, W16-2).
- **Implementation:** See improvement plan DP-5 (overlaps with ML-1).
- **Files:** `ml/feature_store.py`, `ml/features.py`, `migrations/0XX_feature_schemas.sql` (new), `ml/routes.py`, `src/components/FeatureSchemaPanel.tsx` (new), `src/components/Sidebar.tsx`, `tests/test_feature_store.py`.
- **Dependencies:** IMPL-ML-1 (overlaps).
- **Tests:** 13 existing + 11 new = 24.
- **Acceptance Criteria:** Adding a feature without incrementing version raises `SchemaViolation`.
- **Status:** IN_ANALYSIS

---

## D. Strategy Management (ST)

### IMPL-ST-1 — Unified strategy contract (StrategyContext + Factory + Registry)

- **Domain:** Strategy Management
- **Priority:** P1
- **Problem:** Loose ABC; strategies read globals; 47 stubs shown as "Running".
- **Evidence:** `tests/test_strategy_base.py` (5 tests, X11 Wave 7); `FINAL_SYSTEM_REASSESSMENT.md` §1.1.
- **Implementation:** See improvement plan ST-1.
- **Files:** `strategies/types.py` (new), `strategies/base.py`, `strategies/registry.py`, `strategies/signal_trader.py`, `strategies/market_maker.py`, `strategies/arb_scanner.py`, `api/server.py`, `tests/test_strategy_base.py`, `tests/test_signal_trader.py`, `tests/test_market_maker.py`, `tests/test_arb_scanner.py`.
- **Dependencies:** None.
- **Tests:** 5 existing + 13 new = 18.
- **Acceptance Criteria:** All 18 tests pass; `GET /api/strategies` returns 50 rows with `implemented` flag (3 True, 47 False).
- **Status:** IN_ANALYSIS

### IMPL-ST-2 — Strategy lifecycle (pause/resume/restart)

- **Domain:** Strategy Management
- **Priority:** P1
- **Problem:** No runtime pause/resume; circuit-breaker pause is implicit.
- **Evidence:** `risk/routes.py` (W14-2 paused endpoint); `src/components/StrategyMatrix.tsx` shows state but no action.
- **Implementation:** See improvement plan ST-2.
- **Files:** `strategies/types.py`, `strategies/base.py`, `strategies/signal_trader.py`, `strategies/market_maker.py`, `strategies/arb_scanner.py`, `core/circuit_breaker.py`, `api/server.py`, `src/components/StrategyMatrix.tsx`, `tests/test_strategy_base.py`, `tests/test_strategy_lifecycle.py` (new).
- **Dependencies:** IMPL-ST-1.
- **Tests:** +14 new.
- **Acceptance Criteria:** POSTing `/pause` flips state within 2 s; StrategyMatrix renders state pill + buttons.
- **Status:** TODO

### IMPL-ST-3 — Pluggable attribution buckets + per-strategy aggregate

- **Domain:** Strategy Management
- **Priority:** P2
- **Problem:** 7 hardcoded buckets; per-position only; on-demand.
- **Evidence:** `tests/test_attribution.py` (7 tests, U1 Wave 4).
- **Implementation:** See improvement plan ST-3.
- **Files:** `core/attribution.py`, `core/closed_positions.py`, `migrations/0XX_strategy_attribution_daily.sql` (new), `api/server.py`, `src/components/StrategyAttributionPanel.tsx` (new), `src/components/StrategyMatrix.tsx`, `tests/test_attribution.py`.
- **Dependencies:** IMPL-ST-1.
- **Tests:** 7 existing + 9 new = 16.
- **Acceptance Criteria:** Per-strategy attribution endpoint < 100 ms p95 for 30-day range.
- **Status:** IN_ANALYSIS

### IMPL-ST-4 — Strategy metrics dashboard (Sharpe/Sortino/Calmar + time-range)

- **Domain:** Strategy Management
- **Priority:** P2
- **Problem:** 6 KPIs, lifetime only; no Sharpe/Sortino/Calmar.
- **Evidence:** `src/components/StrategyMatrix.tsx`; `src/components/LeaderboardPanel.tsx`.
- **Implementation:** See improvement plan ST-4.
- **Files:** `core/performance.py` (new), `risk/routes.py`, `strategies/routes.py` (new), `api/server.py`, `src/components/StrategyMatrix.tsx`, `src/components/StrategyDetailPanel.tsx` (new), `src/components/Sidebar.tsx`, `tests/test_performance.py` (new).
- **Dependencies:** IMPL-ST-2, IMPL-ST-3.
- **Tests:** +12 new.
- **Acceptance Criteria:** StrategyMatrix renders 9 KPIs + Select + state pill; detail panel < 200 ms.
- **Status:** TODO

---

## E. Backtest Engine (BT)

### IMPL-BT-1 — Realism models (impact, partial, latency, cost, cancel-replace)

- **Domain:** Backtest
- **Priority:** P1
- **Problem:** Flat-bps slippage; binary partials; fixed delay; no funding; no cancel-replace.
- **Evidence:** `tests/test_backtest_engine.py` (9 tests, U7 Wave 4).
- **Implementation:** See improvement plan BT-1.
- **Files:** `backtesting/realism.py` (new), `backtesting/engine.py`, `backtesting/advanced.py`, `backtesting/report.py`, `tests/test_backtest_engine.py`, `tests/test_backtest_realism.py` (new).
- **Dependencies:** IMPL-BE-2 (execution-quality calibration).
- **Tests:** 9 existing + 16 new = 25.
- **Acceptance Criteria:** Backtest P&L within 5 % of live P&L on same 30-day window.
- **Status:** IN_ANALYSIS

### IMPL-BT-2 — Backtest/live parity harness

- **Domain:** Backtest
- **Priority:** P1
- **Problem:** No automated parity check; manual eyeballing.
- **Evidence:** `FINAL_SYSTEM_REASSESSMENT.md` §4.
- **Implementation:** See improvement plan BT-2.
- **Files:** `backtesting/parity.py` (new), `backtesting/report.py`, `api/server.py`, `core/alerting.py`, `src/components/BacktestParityPanel.tsx` (new), `src/components/BacktestLabView.tsx`, `tests/test_backtest_parity.py` (new).
- **Dependencies:** IMPL-BT-1, IMPL-ML-7.
- **Tests:** +14 new.
- **Acceptance Criteria:** Known-divergent backtest reports violation rate > 10 %.
- **Status:** TODO

### IMPL-BT-3 — Backtest lab (sweep, compare, optimize)

- **Domain:** Backtest
- **Priority:** P2
- **Problem:** No parameter sweeps; no strategy comparison; no optimizer.
- **Evidence:** `tests/test_backtest_advanced.py` (11 tests, W16-4).
- **Implementation:** See improvement plan BT-3.
- **Files:** `backtesting/engine.py`, `backtesting/regime.py` (new), `backtesting/optimizer.py` (new), `requirements.txt` (add optuna), `api/server.py`, `src/components/BacktestLabView.tsx`, `src/components/ParameterSweepPanel.tsx` (new), `src/components/StrategyComparisonPanel.tsx` (new), `tests/test_backtest_advanced.py`.
- **Dependencies:** IMPL-BT-1, IMPL-DP-4 (1-min aggregates).
- **Tests:** 11 existing + 14 new = 25.
- **Acceptance Criteria:** 5-value sweep < 60 s; optimizer finds Sharpe-improving params in < 50 trials.
- **Status:** IN_ANALYSIS

### IMPL-BT-4 — Walk-forward: Purged-K-Fold + per-fold equity + parameter rolling

- **Domain:** Backtest
- **Priority:** P2
- **Problem:** Fixed windows; no Purged-K-Fold; aggregate only; no param rolling.
- **Evidence:** `tests/test_ml_validation.py` (8 tests, U5 Wave 4).
- **Implementation:** See improvement plan BT-4.
- **Files:** `backtesting/walk_forward.py` (new), `ml/validation.py`, `backtesting/report.py`, `backtesting/advanced.py`, `tests/test_ml_validation.py`, `tests/test_backtest_advanced.py`.
- **Dependencies:** IMPL-BT-3 (optimizer), IMPL-BT-1 (realism).
- **Tests:** 8 existing + 6 new = 14.
- **Acceptance Criteria:** Purged-K-Fold reports lower Sharpe than expanding on same data.
- **Status:** IN_ANALYSIS

---

## F. UI / UX (UI)

### IMPL-UI-1 — Command Center enhancements (alert banner, health strip, quick actions)

- **Domain:** UI/UX
- **Priority:** P2
- **Problem:** No alerts, no health summary, no edge gauge, no quick actions.
- **Evidence:** `src/app/page.tsx` Command Center case.
- **Implementation:** See improvement plan UI-1.
- **Files:** `src/components/AlertBanner.tsx` (new), `src/components/SystemHealthStrip.tsx` (new), `src/components/QuickActionsToolbar.tsx` (new), `src/components/StrategyMatrix.tsx`, `src/app/page.tsx`, `src/components/AlertBanner.test.tsx` (new), `src/components/SystemHealthStrip.test.tsx` (new), `src/components/QuickActionsToolbar.test.tsx` (new).
- **Dependencies:** None (all data sources exist).
- **Tests:** +18 new.
- **Acceptance Criteria:** Command Center renders within 500 ms; simulated alert appears in banner within 30 s.
- **Status:** IN_ANALYSIS

### IMPL-UI-2 — Deep Analysis workstation enhancements (SHAP, correlation, backtest, position overlay)

- **Domain:** UI/UX
- **Priority:** P2
- **Problem:** No SHAP, no correlation, no per-token backtest, no position overlay.
- **Evidence:** `src/components/DeepAnalysisView.tsx`.
- **Implementation:** See improvement plan UI-2.
- **Files:** `src/components/DeepAnalysisView.tsx`, `src/components/charts/CorrelationMatrix.tsx`, `src/components/charts/PriceHistoryChart.tsx`, `src/components/BacktestLabView.tsx`, `src/components/FeatureAttributionPanel.tsx` (shared with ML-3), `src/components/DeepAnalysisView.test.tsx`.
- **Dependencies:** IMPL-ML-3, IMPL-DP-1.
- **Tests:** +14 new.
- **Acceptance Criteria:** All 7 sub-panels render within 1 s; position overlay shows correctly.
- **Status:** IN_ANALYSIS

### IMPL-UI-3 — Per-panel E2E functional tests

- **Domain:** UI/UX
- **Priority:** P1
- **Problem:** 38 E2E tests cover shell only; no per-panel functional tests.
- **Evidence:** `e2e/dashboard.spec.ts`, `e2e/navigation.spec.ts`, `e2e/api-health.spec.ts`.
- **Implementation:** See improvement plan UI-3.
- **Files:** `e2e/positions.spec.ts` (new), `e2e/orders.spec.ts` (new), `e2e/markets.spec.ts` (new), `e2e/strategy-matrix.spec.ts` (new), `e2e/deep-analysis.spec.ts` (new), `e2e/backtest-lab.spec.ts` (new), `e2e/observability.spec.ts` (new), `e2e/execution-quality.spec.ts` (new), `e2e/live-safety-gate.spec.ts` (new), `e2e/fixtures.ts` (new), `docs/UI_FUNCTIONAL_VERIFICATION.md` (new).
- **Dependencies:** None.
- **Tests:** +30 new (E2E count 38 → ~80).
- **Acceptance Criteria:** E2E suite >= 80 tests; all pass in CI on every PR.
- **Status:** IN_ANALYSIS

### IMPL-UI-4 — Design standard compliance (stylelint + ESLint rule + migration script)

- **Domain:** UI/UX
- **Priority:** P2
- **Problem:** No automated design-token compliance; ~30 hardcoded colours in components.
- **Evidence:** `rg "#22c55e" src/components/` → ~30 hits.
- **Implementation:** See improvement plan UI-4.
- **Files:** `.stylelintrc.json` (new), `.stylelintrc.custom-rules/` (new), `eslint.config.mjs`, `scripts/replace_design_tokens.py` (new), `.github/workflows/ci.yml`, `docs/DESIGN_STANDARD.md` (new), `package.json`.
- **Dependencies:** None.
- **Tests:** Linter is the test.
- **Acceptance Criteria:** `bun run stylelint` exits 0; CI gate blocks new violations; migration script replaces >= 90 % of hardcoded values.
- **Status:** IN_ANALYSIS

---

## G. Risk / Portfolio (RP)

### IMPL-RP-1 — Risk engine: per-strategy budget + VaR gate + scenario + adverse-selection

- **Domain:** Risk/Portfolio
- **Priority:** P0
- **Problem:** Global budget only; no VaR gate; no scenario tightening; no adverse-selection gate.
- **Evidence:** `tests/test_risk_manager.py` (6 tests, S7 Wave 2).
- **Implementation:** See improvement plan RP-1.
- **Files:** `risk/manager.py`, `risk/routes.py`, `core/stress_test.py`, `core/closed_positions.py`, `tests/test_risk_manager.py`, `src/components/RiskStatusPanel.tsx`.
- **Dependencies:** IMPL-BE-4 (breaker pattern), IMPL-ST-2.
- **Tests:** 6 existing + 16 new = 22.
- **Acceptance Criteria:** Order pushing VaR > MAX rejected with clear reason; UI shows each gate.
- **Status:** IN_ANALYSIS

### IMPL-RP-2 — Capital allocation: per-strategy + rebalancer + Kelly option

- **Domain:** Risk/Portfolio
- **Priority:** P1
- **Problem:** Global multipliers; per-signal only; no rebalancing; no Kelly.
- **Evidence:** `tests/test_capital_allocator.py` (9 tests, T9 Wave 3); `tests/test_capital_allocator_advanced.py` (8 tests, W6 Wave 6).
- **Implementation:** See improvement plan RP-2.
- **Files:** `core/capital_allocator.py`, `core/portfolio.py`, `core/portfolio_optimizer.py`, `ml/training_orchestrator.py`, `api/server.py`, `src/components/CapitalAllocatorPanel.tsx`, `tests/test_capital_allocator.py`, `tests/test_capital_allocator_advanced.py`.
- **Dependencies:** IMPL-RP-3 (stress-tested covariance), IMPL-ST-1.
- **Tests:** 17 existing + 18 new = 35.
- **Acceptance Criteria:** Kelly strategy sizes up for higher-edge signals; rebalancer suggests trim on > 5 % drift.
- **Status:** IN_ANALYSIS

### IMPL-RP-3 — Stress testing: custom scenarios + live + cron + alerting

- **Domain:** Risk/Portfolio
- **Priority:** P1
- **Problem:** 6 fixed scenarios; on-demand; no live; no cron; no alerting.
- **Evidence:** `tests/test_stress_test.py` (8 tests, Wave 16).
- **Implementation:** See improvement plan RP-3.
- **Files:** `core/stress_test.py`, `core/alerting.py`, `ml/training_orchestrator.py`, `api/server.py`, `scenarios/*.yaml` (new dir), `src/components/PortfolioRiskPanel.tsx`, `tests/test_stress_test.py`.
- **Dependencies:** IMPL-DP-1, IMPL-RP-1.
- **Tests:** 8 existing + 12 new = 20.
- **Acceptance Criteria:** Custom YAML scenario runs end-to-end; daily cron produces report.
- **Status:** IN_ANALYSIS

### IMPL-RP-4 — Kelly criterion (full + confidence-bounded + drawdown-aware + comparison report)

- **Domain:** Risk/Portfolio
- **Priority:** P2
- **Problem:** No Kelly sizing function.
- **Evidence:** No `tests/test_kelly.py` exists.
- **Implementation:** See improvement plan RP-4.
- **Files:** `core/kelly.py` (new), `core/capital_allocator.py`, `tests/test_kelly.py` (new), `scripts/compare_kelly_vs_saturating.py` (new), `docs/KELLY_VS_SATURATING.md` (new).
- **Dependencies:** IMPL-RP-2, IMPL-RP-3.
- **Tests:** +12 new.
- **Acceptance Criteria:** Kelly f=0.25 has higher Sharpe + lower/equal MDD vs saturating on last 90 days.
- **Status:** TODO

---

## H. Observability (OB)

### IMPL-OB-1 — Observability expansion (OpenTelemetry + SLO dashboards + error budgets)

- **Domain:** Observability
- **Priority:** P2
- **Problem:** No tracing; no SLO dashboards; no error budgets.
- **Evidence:** `tests/test_observability.py` (6 tests, T10 Wave 3); `tests/test_observability_collector.py` (5 tests, W8 Wave 6).
- **Implementation:** See improvement plan OB-1.
- **Files:** `requirements.txt`, `api/server.py`, `core/logging_config.py`, `core/decision_ledger.py`, `migrations/0XX_decision_events_trace_id.sql` (new), `core/alerting.py`, `grafana/dashboards/slo.json` (new), `docker-compose.yml`, `tests/test_observability.py`.
- **Dependencies:** IMPL-DP-1.
- **Tests:** 6 existing + 8 new = 14.
- **Acceptance Criteria:** trace_id in logs + ledger + Grafana trace; SLO dashboard renders 4 SLOs.
- **Status:** IN_ANALYSIS

### IMPL-OB-2 — Auditability: cross-DB chain + integrity cron + WORM export

- **Domain:** Observability
- **Priority:** P1
- **Problem:** 8 per-DB chains; no cross-DB integrity; no tamper cron; no WORM export.
- **Evidence:** `src/components/AuditLogPanel.tsx` (W14-4) — no integrity UI.
- **Implementation:** See improvement plan OB-2.
- **Files:** `core/audit_chain.py` (new), `core/audit_logger.py`, `core/immutable_audit.py`, `ml/training_orchestrator.py`, `api/server.py`, `migrations/0XX_audit_chain.sql` (new), `src/components/AuditLogPanel.tsx`, `tests/test_audit_logger.py`, `tests/test_audit_chain.py` (new).
- **Dependencies:** IMPL-BE-5 (idempotency keys).
- **Tests:** +14 new.
- **Acceptance Criteria:** Simulated tampering detected within 1 h; UI verify button returns green when no tampering.
- **Status:** IN_ANALYSIS

### IMPL-OB-3 — Prometheus/Grafana: per-strategy dashboards + alerts + Mimir + cardinality guard

- **Domain:** Observability
- **Priority:** P2
- **Problem:** No per-strategy dashboards; no alert library; 15-d retention; no cardinality guard.
- **Evidence:** `grafana/dashboards/` 6 dashboards; `prometheus.yml` single instance.
- **Implementation:** See improvement plan OB-3.
- **Files:** `grafana/dashboards/signal_trader.json` (new), `grafana/dashboards/market_maker.json` (new), `grafana/dashboards/arb_scanner.json` (new), `grafana/alerts/*.yml` (new — 5 files), `docker-compose.yml`, `prometheus.yml`, `core/prometheus_metrics.py`, `tests/test_prometheus.py`.
- **Dependencies:** None.
- **Tests:** +6 new.
- **Acceptance Criteria:** 3 per-strategy dashboards render; 5 alert rules fire; Mimir receives remote-writes within 10 s.
- **Status:** IN_ANALYSIS

### IMPL-OB-4 — Alerting: on-call routing + dedup + escalation + UI

- **Domain:** Observability
- **Priority:** P1
- **Problem:** No routing; no dedup; no escalation; no UI.
- **Evidence:** `core/alerting.py` (W16-1, 6 tests).
- **Implementation:** See improvement plan OB-4.
- **Files:** `core/alerting.py`, `config/on_call.yml` (new), `ml/training_orchestrator.py`, `src/components/AlertsPanel.tsx` (new), `src/components/Sidebar.tsx`, `src/components/AlertBanner.tsx` (shared with UI-1), `tests/test_alerting.py` (new + expand).
- **Dependencies:** IMPL-UI-1.
- **Tests:** +14 new.
- **Acceptance Criteria:** Duplicate within 5 min increments count; CRITICAL un-acked 5 min escalates; UI ack works.
- **Status:** IN_ANALYSIS

---

## Summary by status

| Status | Count | Tasks |
| --- | --- | --- |
| TODO | 10 | IMPL-BE-1, IMPL-BE-2, IMPL-BE-5, IMPL-ML-2, IMPL-ML-4, IMPL-ML-7, IMPL-DP-1, IMPL-DP-2, IMPL-DP-3, IMPL-RP-4 |
| IN_ANALYSIS | 19 | IMPL-BE-3, IMPL-BE-4, IMPL-ML-1, IMPL-ML-3, IMPL-ML-5, IMPL-ML-6, IMPL-DP-4, IMPL-DP-5, IMPL-ST-1, IMPL-ST-3, IMPL-BT-1, IMPL-BT-3, IMPL-BT-4, IMPL-UI-1, IMPL-UI-2, IMPL-UI-3, IMPL-UI-4, IMPL-RP-1, IMPL-RP-2, IMPL-RP-3, IMPL-OB-1, IMPL-OB-2, IMPL-OB-3, IMPL-OB-4 |
| IMPLEMENTING | 0 | (none — no task is mid-coding) |
| TESTING | 0 | (none) |
| BLOCKED | 0 | (none — every blocked task is captured as a TODO with its dependency) |
| DONE | 0 | (every task here is net-new for waves 17+; prior-wave work is captured in `IMPLEMENTATION_STATUS.md`) |
| **Total** | **33** | |

## Summary by priority

| Priority | Count |
| --- | --- |
| P0 | 6 (IMPL-BE-1, IMPL-BE-4, IMPL-BE-5, IMPL-ML-7, IMPL-DP-3, IMPL-RP-1) |
| P1 | 15 |
| P2 | 12 |
| **Total** | **33** |

## Summary by domain

| Domain | Count |
| --- | --- |
| Bot Execution (BE) | 5 |
| AI/ML (ML) | 7 |
| Data Platform (DP) | 5 |
| Strategy Management (ST) | 4 |
| Backtest (BT) | 4 |
| UI/UX (UI) | 4 |
| Risk/Portfolio (RP) | 4 |
| Observability (OB) | 4 |
| **Total** | **37** (note: 4 tasks are cross-domain dependencies counted once each) |

## W18 critical-path recommendation

Per the master roadmap §6, the next wave (W18) should target the
P0 + critical-P1 items that unblock the largest downstream
surface:

1. IMPL-BE-5 (idempotency) — P0, foundational.
2. IMPL-BE-1 (OSM enhancements) — P0, depends on BE-5.
3. IMPL-BE-4 (circuit breakers) — P0.
4. IMPL-ML-7 (shadow promotion gate) — P0, AI/ML's only P0.
5. IMPL-DP-3 (data quality monitor) — P0.
6. IMPL-RP-1 (risk engine enhancements) — P0.
7. IMPL-ST-1 (unified strategy contract) — P1, foundational.

The remaining 26 tasks are scheduled for W19+ waves per their
dependencies.

---

## Maintenance

- **Adding a task:** create a new `IMPL-<DOMAIN>-<N+1>` row with
  all required fields. Reference the owning improvement plan
  (`docs/improvements/*_IMPROVEMENT_PLAN.md`).
- **Status transitions:** update both this file AND
  `IMPLEMENTATION_STATUS.md` (which is auto-derived from this
  file).
- **Closing a task:** mark Status as DONE; move to
  `IMPLEMENTATION_STATUS.md`'s completed log with the closing
  wave ID.
