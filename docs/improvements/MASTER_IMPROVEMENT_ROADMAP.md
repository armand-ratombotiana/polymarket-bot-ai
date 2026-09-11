# Master Improvement Roadmap — Polymarket Pro Trading Platform

- **Document owner:** Platform orchestrator
- **Date created:** 2026-09-03 (W17-9)
- **Source authority:** God Mode Master Prompt §62–69 (Improvement Plan +
  Implementation Plan structure)
- **Last revised:** 2026-09-03
- **Scope:** All Polymarket Pro subsystems (bot execution engine, AI/ML,
  data platform, strategy management, backtest engine, UI/UX,
  risk/portfolio, observability)
- **Related docs:**
  - Per-domain improvement plans in this directory (`*_IMPROVEMENT_PLAN.md`)
  - `docs/implementation/MASTER_IMPLEMENTATION_PLAN.md` (the actionable backlog)
  - `docs/implementation/IMPLEMENTATION_STATUS.md` (live status dashboard)
  - `docs/ARCHITECTURE.md` (current system design)
  - `docs/METRICS_SUMMARY.md` (current scale)
  - `docs/reassessment/FINAL_SYSTEM_REASSESSMENT.md` (Wave 5 baseline)

This roadmap is the single source of truth for **what has been done, what
remains, and in what order it should be done**. It is consulted by:

1. Orchestrator agents — to pick the next wave's tasks.
2. Subagents — to scope an improvement before touching code.
3. Operators — to answer "what's left before I can enable live trading?"

---

## 1. Wave History (16 waves completed)

The platform has been built through 16 progressive waves. Waves 1–7 were
batch rebuilds (15 subagents each, R/S/T/U/V/W/X prefixes). Waves 8–16
were targeted improvement waves (10 subagents each, W8-* through W16-*
task IDs) building on top of the rebuild foundation.

### Wave 1 — `GM-REBUILD` (R1–R15)
**Theme:** Execution + ML + decision-ledger foundation.
**Delivered:**
- Marketable SL/TP exits at best_bid (R1), bounded ask_size + inventory
  flush >60 s (R2), per-trade circuit breaker (R3), paper slippage
  model (R4).
- Label backfill pipeline from Gamma resolved markets (R5), drift
  detector reset (R6), time-ordered ML split (R7), NaN/Inf dropping in
  meta-learner refit (R8).
- Signal-trader confidence floor 0.45 (R9), `/api/ai/predict/{token_id}`
  + `/api/positions/{token_id}/close` (R10).
- `core/decision_ledger.py` + wired stage chain (R11/R12),
  `execution/smart_router.py` (R13), spread-derived competitiveness
  (R14), unrealized PnL + Sharpe in analytics (R15).
**Outcome:** 61 routes, balance $111.83, win rate 80 %, expectancy +$0.19.

### Wave 2 — `REBUILD-WAVE-2` (S1–S15)
**Theme:** Frontend + tests + security + observability + execution quality.
**Delivered:**
- Frontend: PositionsPanel unrealized PnL + Close button (S1),
  DepthChartModal ML Edge panel (S2), AnalyticsPanel KPI cards (S3),
  globals.css design system (S4), TopStatusBar mobile pill (S5).
- Tests (6 files, 67 tests): test_features, test_risk_manager,
  test_paper_simulator, test_decision_ledger, test_e2e_decision_chain,
  test_failure_injection (S6–S11).
- Security hardening: CORS wildcard removed, docs disabled in live
  mode, WS fail-closed, `.env` chmod 600 (S12).
- New modules: `core/observability.py`, `core/execution_quality.py`,
  `core/closed_positions.py`, `core/attribution.py` (S13–S15).
**Outcome:** 67 routes, 67 tests passing, 7-dimension attribution live.

### Wave 3 — `REBUILD-WAVE-3` (T1–T15)
**Theme:** Advanced features — shadow trading, live safety gate, ML
validation, capital allocator, data retention.
**Delivered:**
- `core/shadow_trading.py` counterfactual recorder (T1, God Mode §75).
- `core/live_safety_gate.py` 10-check staged gate (T2, God Mode §82).
  Initial readiness: 4/10.
- `ml/validation.py` walk-forward CV + OOT + leakage detection (T3).
- `backtesting/engine.py` realistic backtest (slippage, partial fills,
  execution delay, look-ahead bias) (T4).
- `core/capital_allocator.py` saturating edge curve + 5 multipliers (T5).
- `core/retention.py` 7/30/90-day pruning (T6).
- `core/observability_collector.py` 30-s background auto-collector (T7).
- `ml/model_registry.py` + `ml/routes.py` model rollback (T8).
- `ml/shadow_inference.py` challenger model (logistic_baseline) +
  `run_shadow` wired into predict() (T13).
- 36 new tests across 4 files (T9–T12). `tests/conftest.py` shared
  fixtures + autouse reset (T14, T15).
**Outcome:** 76 routes, 103 tests passing, live readiness 4/10.

### Wave 4 — `REBUILD-WAVE-4` (U1–U15)
**Theme:** Test coverage + observability instrumentation + UI polish.
**Delivered:**
- 73 new tests across 8 files: attribution, settlement, shadow_trading,
  live_safety_gate, ml_validation, **order_state_machine** (NEW module),
  backtest_engine, retention (incl. 16-case SQL injection guard) (U1–U8).
- `core/order_state_machine.py` (NEW — U6).
- Observability wired across signal_trader, market_maker, arb_scanner,
  ml/model, settlement, book_poller (U9, U10).
- Frontend: useBot price flash tracking (U11), MarketsPanel flash (U12),
  audio cues on fills + whale alerts (U13), StrategyMatrix live P&L
  per strategy (U14), LeaderboardPanel profit_factor/max_drawdown/
  net_pnl (U15).
**Outcome:** 176 tests passing, observability metrics actually
persisting, audio + price-flash UX live.

### Wave 5 — `REBUILD-WAVE-5` (V1–V15)
**Theme:** Integration fixes + capital allocator wiring + risk gates +
final reassessment.
**Delivered:**
- Async observability calls fixed (V1), capital allocator wired into
  signal_trader (V2), position manager exits through risk gate (V3),
  MTM exposure risk gate (V4), shadow trades on rejection (V5),
  closed positions on settlement YES+NO (V11), OSM CANCELLED in
  cancel_order (V13).
- 75 new tests: portfolio, gamma_client, book_poller, config, ml_model
  (V6–V10).
- `risk/routes.py` `/api/risk/strategies/paused` (V12).
- `decision_ledger.py` stamps `model_version` on PREDICTION +
  `get_prediction_history()` (V14).
- `docs/reassessment/FINAL_SYSTEM_REASSESSMENT.md` — master before/after
  comparison (V15).
**Outcome:** 218 tests passing, 77 routes, 5 ML versions, shadow
challenger live.

### Wave 6 — `REBUILD-WAVE-6` (W1–W15)
**Theme:** Fix last failing test + rotate token + 55 new tests +
observability collector wired.
**Delivered:**
- V2 liquidity type mismatch fixed (dict→float) (W1), API_TOKEN rotated
  to 64-char `secrets.token_urlsafe(48)` across 3 locations (W2).
- 55 new tests across 8 files: drift_detector, meta_learner,
  label_backfill, capital_allocator_advanced, shadow_inference,
  observability_collector, live_safety_gate_api, shadow_trading_api
  (W3–W10).
- Observability collector wired into `api/server.py` lifespan (W11).
- PositionsPanel price flash on Mark (W12), DeepAnalysisView one-click
  Trade (W13), EquityCurve drawdown overlay (W14), Git push (W15).
**Outcome:** 273 tests passing (0 failures), token rotated, observability
collector active.

### Wave 7 — `REBUILD-WAVE-7` (X1–X15)
**Theme:** Comprehensive test coverage for ALL untested modules + fix
settlement deadlock + verify routes.
**Delivered:**
- 70+ new tests across 14 modules: market_discovery, reconciliation,
  watchdog, training_orchestrator, analysis_engine, ml_copilot,
  vector_store, data_store, strategy_base, market_maker, arb_scanner,
  signal_trader, plus performance test files (X1–X14).
- Settlement deadlock FIXED — nested asyncio.Lock (store.log_event
  called inside store._lock) (X8).
- ALL 13 route modules verified wired, boot-time audit log (X9).
**Outcome:** 340 tests passing (0 failures), ALL core modules now
covered, settlement deadlock eliminated.

### Wave 8 — `W8-1..W8-10` (Wave 8 — 10 new UI panels)
**Theme:** Build every backend-facing UI panel that was missing.
**Delivered:** 10 new panels — DecisionLedgerPanel, AttributionPanel,
ExecutionQualityPanel, ClosedPositionsPanel, CapitalAllocatorPanel,
ShadowInferencePanel, LiveSafetyGatePanel, ObservabilityPanel,
RetentionPanel, MLValidationPanel. All wired into Sidebar + page.tsx
dynamic imports.
**Outcome:** 27 → 37 UI components, 12 panel endpoints verified working
with real data.

### Wave 9 — `W9-1..W9-9` (Wave 9 — accessibility + design + perf + tooling)
**Theme:** Accessibility audit + fixes, design system refinements,
performance instrumentation, operator tooling.
**Delivered:**
- Accessibility audit + 19 fixes across 7 components (W9-7),
  shortkeys modal + focus trap (W9-2), perf instrumentation
  (W9-9), WCAG 2.1 AA conformance (W9-1), theme + locale switcher
  (W9-3), notification system (W9-6), backend metrics expansion
  (W9-8), strategy matrix P&L (W9-5).
**Outcome:** 454 backend tests, 88 frontend tests, full WCAG 2.1 AA.

### Wave 10 — `W10-1..W10-9` (Wave 10 — packaging + delivery)
**Theme:** CI/CD + Docker + LICENSE + CHANGELOG + production config.
**Delivered:**
- `.github/workflows/ci.yml` (frontend lint+test, backend pytest,
  production build) (W10-1).
- `.github/workflows/release.yml` (build-frontend, build-backend wheel,
  GitHub Release with auto-changelog) (W10-1).
- `.github/dependabot.yml` weekly npm+pip+github-actions (W10-1).
- Multi-stage Dockerfile + docker-compose.yml + Caddyfile.prod (W10-2).
- LICENSE (MIT), CHANGELOG.md, `.env.example` (W10-9).
- DB migration system (W10-5), feature flags (W10-8), API versioning
  `/api/v1/` prefix (W10-3), rate limiting slowapi 6 tiers (W10-7),
  performance profiling cProfile + p50/p95/p99 (W10-6), contract tests
  for OpenAPI (W10-4).
**Outcome:** 90+ routes, 540+ backend tests, full CI/CD + container
delivery pipeline.

### Wave 11 — `W11-1..W11-8` (Wave 11 — testing infrastructure)
**Theme:** Playwright E2E + contract tests + load testing + security.
**Delivered:**
- Playwright E2E test framework + 38 tests (dashboard, navigation,
  api-health) (W11-1).
- Contract tests for OpenAPI spec (W11-8), load testing harness (W11-4),
  security penetration tests (W11-3).
**Outcome:** 38 E2E tests, contract suite pinning the API surface.

### Wave 12 — `W12-1..W12-9` (Wave 12 — performance + quality)
**Theme:** Bundle optimization, Storybook, accessibility deep dive, code
quality.
**Delivered:**
- `@next/bundle-analyzer` + webpack splitChunks + `.bundle-budget.json`
  (W12-4), Storybook stories for 6 components (W12-7), accessibility
  refinements (W12-5), error boundary (W12-1), PWA service worker
  (W12-8), i18n EN/FR (W12-9), database explorer (W12-6), offline
  indicator (W12-2), keyboard cheat sheet (W12-3).
**Outcome:** 5 chart primitives + 55+ UI components, sub-350 KB first
load budget, full i18n + PWA posture.

### Wave 13 — `W13-1..W13-9` (Wave 13 — UX completeness)
**Theme:** Dark/light theme, command palette, push notifications, charts.
**Delivered:**
- Dark/light theme switcher via next-themes (W13-4), CommandPalette
  Cmd+K with 25 nav entries + 6 page actions (W13-5), browser push
  notifications (W13-6), Recharts visualization primitives
  (EquityCurveChart, PnLBarChart, Sparkline, GaugeChart,
  ReliabilityDiagram) (W13-9), WebSocket auto-reconnect (W13-5),
  portfolio risk panel (W13-6), audit log viewer (W13-4).
**Outcome:** Full dark/light theme + Cmd+K palette + push notifications +
5 chart primitives.

### Wave 14 — `W14-1..W14-8` (Wave 14 — operations + monitoring)
**Theme:** CLI tool, rate-limit dashboard, audit log viewer, frontend
error reporting, i18n.
**Delivered:**
- `cli.py` 14-command operator tool (W14-3), rate-limit dashboard
  (W14-7), audit log viewer with severity filter + CSV/JSON export
  (W14-4), frontend error reporter (W14-8), i18n EN/FR via next-intl
  + useTranslation hook (W14-2), Prometheus `/metrics` endpoint (W14-6),
  Grafana dashboard auto-provisioned (W14-7).
**Outcome:** 17+ operational scripts, Prometheus + Grafana live, audit
log viewer with export.

### Wave 15 — `W15-1..W15-7` (Wave 15 — UI polish + docs)
**Theme:** Chart components, user preferences, documentation final review.
**Delivered:**
- MarketDepthChart + PriceHistoryChart + PriceTicker (3 new chart
  components, 64 new tests) (W15-1).
- User preferences system: `src/lib/preferences.ts` + `usePreferences`
  hook + `SettingsModal` (6 sections: Display, Dashboard, Trading,
  Notifications, Sound, Privacy) + CustomEvent broadcast (W15-2).
- Documentation final review + consistency (W15-7): `scripts/check_docs.py`
  link checker, fixed broken link in `docs/API_CLIENT.md`, README +
  METRICS_SUMMARY refreshed.
**Outcome:** 556 frontend tests, 5 chart primitives → 5 (already), full
user preferences system.

### Wave 16 — `W16-1..W16-9` (Wave 16 — async + scale)
**Theme:** Async DB pool, feature store, ML explainability, advanced
backtest report, portfolio optimizer, correlation matrix, ML copilot,
advanced smart router.
**Delivered:**
- `core/db_pool.py` `AsyncDBPool` (aiosqlite, WAL mode, per-DB pooling)
  + `core/async_repositories.py` (3 async read-side repos) +
  `/api/v2/decisions/recent` + `/api/v2/observability/latest` (W16-7).
- `ml/feature_store.py` feature versioning + W16-2 wiring.
- `ml/explainability.py` SHAP-style feature attribution (W16-3).
- `backtesting/advanced.py` walk-forward + Monte Carlo + advanced
  metrics (W16-4).
- `core/portfolio_optimizer.py` mean-variance optimizer (W16-5).
- `core/correlation.py` correlation matrix (W16-6).
- `ml/copilot.py` natural-language market analyst (W16-8).
- `execution/advanced_router.py` TWAP / VWAP / iceberg (W16-9).
- `core/alerting.py` + `core/stress_test.py` (W16-1, W16-?).

**Outcome:** Async v2 endpoints live, feature store + explainability +
advanced backtest + portfolio optimizer + correlation matrix + ML
copilot + advanced smart router all delivered.

### Cumulative result (end of Wave 16)
- **970+ backend tests**, **459+ frontend tests**, **38 E2E tests** —
  **1429+ total** (0 failures in main suites).
- **90+ API routes** across 13 route modules.
- **55+ React panels** + 5 chart primitives + dark/light + EN/FR i18n +
  PWA + Cmd+K palette.
- **17+ operational scripts** (backup, verify, rotation, integrity,
  restore round-trip, bundle analyzer, link checker, CLI 14 commands).
- **31 auto-collected system metrics** + Prometheus `/metrics` + Grafana
  dashboard auto-provisioned.
- **4 ML models** (RF, GB, SGD, LightGBM) + Level-2 meta-learner +
  walk-forward CV + drift detection + Platt/isotonic calibration.
- **10-check live safety gate** (current readiness 4/10 — paper mode
  is the default).
- Paper-trade balance: **$111.72** (started from $100), 80 % win rate,
  +$0.19 expectancy.

---

## 2. Priority Scheme (per God Mode §64)

Every improvement area in this roadmap is tagged with one of four
priorities. The priority determines whether the area is on the
critical path to live trading or a "nice to have" optimisation.

| Tag | Meaning                                              | Trigger                                            |
| --- | ---------------------------------------------------- | -------------------------------------------------- |
| **P0** | Safety / correctness / capital risk             | Blocks live trading; data loss / wrong trade risk  |
| **P1** | Core architecture / parity                       | Foundational contract; affects every subsystem     |
| **P2** | Research / analytics / model quality            | Affects strategy edge, not safety                  |
| **P3** | Optimization / polish                            | DX, performance, aesthetic                          |

**Operating principle:** A P0 task blocks every P1 task. P1 blocks P2.
P3 is scheduled opportunistically when its owning domain has no P0/P1
outstanding. An operator cannot enable live trading while **any** P0
task remains `TODO` or `BLOCKED`.

---

## 3. Improvement Areas — Status Table

The status column uses the §69 lifecycle: **DONE / IN PROGRESS / TODO /
BLOCKED**. The Phase column references §66 (Phase 0–11).

| # | Domain | Improvement area | Priority | Phase | Status | Owning plan |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Bot Execution | Order state machine enhancements | P0 | 1 | IN PROGRESS | `BOT_EXECUTION_ENGINE` |
| 2 | Bot Execution | Execution quality improvements | P1 | 2 | IN PROGRESS | `BOT_EXECUTION_ENGINE` |
| 3 | Bot Execution | Smart order routing (TWAP/VWAP/iceberg) | P1 | 3 | IN PROGRESS | `BOT_EXECUTION_ENGINE` |
| 4 | Bot Execution | Circuit breaker enhancements | P0 | 1 | IN PROGRESS | `BOT_EXECUTION_ENGINE` |
| 5 | Bot Execution | Idempotency improvements | P0 | 1 | TODO | `BOT_EXECUTION_ENGINE` |
| 6 | AI/ML | Feature store enhancements | P1 | 3 | IN PROGRESS | `AI_ML_ENGINE` |
| 7 | AI/ML | Model calibration improvements | P2 | 4 | IN PROGRESS | `AI_ML_ENGINE` |
| 8 | AI/ML | SHAP explainability integration | P2 | 4 | IN PROGRESS | `AI_ML_ENGINE` |
| 9 | AI/ML | A/B testing framework expansion | P2 | 5 | IN PROGRESS | `AI_ML_ENGINE` |
| 10 | AI/ML | Drift detection improvements | P1 | 3 | IN PROGRESS | `AI_ML_ENGINE` |
| 11 | AI/ML | Label backfill automation | P1 | 3 | IN PROGRESS | `AI_ML_ENGINE` |
| 12 | AI/ML | Shadow inference promotion gate | P0 | 2 | TODO | `AI_ML_ENGINE` |
| 13 | Data Platform | PostgreSQL/TimescaleDB migration | P1 | 6 | TODO | `DATA_PLATFORM` |
| 14 | Data Platform | Real-time pipeline enhancements | P1 | 6 | TODO | `DATA_PLATFORM` |
| 15 | Data Platform | Data quality monitoring | P0 | 5 | TODO | `DATA_PLATFORM` |
| 16 | Data Platform | Historical data retention optimization | P2 | 7 | IN PROGRESS | `DATA_PLATFORM` |
| 17 | Data Platform | Feature versioning | P1 | 6 | IN PROGRESS | `DATA_PLATFORM` |
| 18 | Strategy | Unified strategy contract (§26) | P1 | 0 | IN PROGRESS | `STRATEGY_MANAGEMENT` |
| 19 | Strategy | Strategy lifecycle management (§27) | P1 | 1 | IN PROGRESS | `STRATEGY_MANAGEMENT` |
| 20 | Strategy | Strategy attribution (§28) | P2 | 2 | IN PROGRESS | `STRATEGY_MANAGEMENT` |
| 21 | Strategy | Strategy metrics dashboard (§29) | P2 | 5 | IN PROGRESS | `STRATEGY_MANAGEMENT` |
| 22 | Backtest | Backtest realism (§31) | P1 | 3 | IN PROGRESS | `BACKTEST_ENGINE` |
| 23 | Backtest | Backtest/live parity (§32) | P1 | 4 | TODO | `BACKTEST_ENGINE` |
| 24 | Backtest | Backtest lab features (§33) | P2 | 5 | IN PROGRESS | `BACKTEST_ENGINE` |
| 25 | Backtest | Walk-forward enhancement | P2 | 5 | IN PROGRESS | `BACKTEST_ENGINE` |
| 26 | UI/UX | Command center enhancements (§35) | P2 | 8 | IN PROGRESS | `UI_UX` |
| 27 | UI/UX | Deep analysis workstation (§43) | P2 | 8 | IN PROGRESS | `UI_UX` |
| 28 | UI/UX | UI functional verification (§48) | P1 | 9 | IN PROGRESS | `UI_UX` |
| 29 | UI/UX | Design standard compliance (§49) | P2 | 9 | IN PROGRESS | `UI_UX` |
| 30 | Risk/Portfolio | Risk engine enhancements (§52) | P0 | 1 | IN PROGRESS | `RISK_PORTFOLIO` |
| 31 | Risk/Portfolio | Capital allocation improvements (§53) | P1 | 2 | IN PROGRESS | `RISK_PORTFOLIO` |
| 32 | Risk/Portfolio | Stress testing expansion | P1 | 7 | IN PROGRESS | `RISK_PORTFOLIO` |
| 33 | Risk/Portfolio | Kelly criterion optimization | P2 | 7 | TODO | `RISK_PORTFOLIO` |
| 34 | Observability | Observability expansion (§54) | P2 | 8 | IN PROGRESS | `OBSERVABILITY` |
| 35 | Observability | Auditability improvements (§55) | P1 | 9 | IN PROGRESS | `OBSERVABILITY` |
| 36 | Observability | Prometheus/Grafana enhancements | P2 | 8 | IN PROGRESS | `OBSERVABILITY` |
| 37 | Observability | Alerting system improvements | P1 | 9 | IN PROGRESS | `OBSERVABILITY` |

**Status summary (count by lifecycle state):**
- DONE: 0 (none — every area has residual work below the bar of
  "production-ready for live capital")
- IN PROGRESS: 31 (active development — partial implementation)
- TODO: 6 (not yet started)
- BLOCKED: 0 (no blocked items currently)

---

## 4. Implementation Phases (per God Mode §66)

The roadmap is sequenced through 12 phases (Phase 0 → Phase 11). Each
phase has an exit criterion — you cannot start Phase N+1 until Phase
N's exit criterion is met. A phase may span multiple waves of work.

### Phase 0 — Foundation contracts
**Goal:** Define the contracts every subsystem depends on.
- Unified strategy contract (`StrategyBase` ABC + `StrategyContext`).
- `OrderIntent` / `OrderReceipt` value types.
- `Decision` chain shape (PREDICTION → SIGNAL → RISK → ORDER → FILL).
- `FeatureVector` schema versioning.
- Status: **IN PROGRESS** — strategy contract shipped in Wave 5/Wave 16;
  remaining items are alignment work.

### Phase 1 — Safety scaffolding
**Goal:** Every capital-touching code path is fail-closed.
- Order state machine: idempotent transitions, terminal-state guards.
- Per-trade circuit breaker, daily-loss circuit breaker, kill switch.
- 10-check live safety gate (God Mode §82).
- Idempotency keys on order submission, settlement, ledger writes.
- Status: **IN PROGRESS** — gate at 4/10; idempotency keys are partial.

### Phase 2 — Execution quality
**Goal:** Realised slippage ≤ modelled slippage in 95 % of fills.
- Marketable SL/TP at best_bid (DONE Wave 1).
- Paper slippage model (crossing + size + queue) (DONE Wave 1).
- Execution quality recorder (DONE Wave 2).
- Shadow inference promotion gate (TODO — P0).
- Status: **IN PROGRESS**.

### Phase 3 — Strategy + ML pipeline hardening
**Goal:** No silent model degradation; strategies fail-closed.
- Walk-forward CV (DONE Wave 3).
- Drift detection (PSI + KS + Brier + EWMA) (DONE Wave 3, refined Wave 6).
- Label backfill automation (DONE Wave 3 — needs scheduler).
- Feature store + versioning (WAVE 16 W16-2 — needs backfill from
  historical resolved markets).
- Smart order routing (TWAP/VWAP/iceberg) (DONE Wave 16 W16-9 — needs
  live integration with order state machine).
- Status: **IN PROGRESS**.

### Phase 4 — Backtest + calibration
**Goal:** Backtest matches live within 5 % over a 30-day window.
- Realistic backtest engine (slippage, partials, delay, lookahead)
  (DONE Wave 3).
- Walk-forward + Monte Carlo advanced report (DONE Wave 16 W16-4).
- Platt + isotonic calibration (DONE Wave 6 — needs daily refresh).
- SHAP explainability (DONE Wave 16 W16-3 — needs UI integration).
- Backtest/live parity harness (TODO — P1).
- Status: **IN PROGRESS**.

### Phase 5 — Data quality + analytics
**Goal:** Zero silent data loss; every metric has an owner.
- Data quality monitoring (TODO — P0).
- Strategy attribution (DONE Wave 2 — needs 7-bucket → 12-bucket
  expansion).
- Strategy metrics dashboard (DONE Wave 8 — needs per-strategy
  Sharpe/Sortino/Calmar).
- A/B testing framework expansion (DONE Wave 16 — needs UI).
- Backtest lab features (DONE Wave 16 W16-4 — needs UI).
- Status: **IN PROGRESS**.

### Phase 6 — Data platform migration
**Goal:** PostgreSQL/TimescaleDB is the system of record.
- `core/timescale_db.py` standby adapter (DONE Wave 5).
- `core/db_pool.py` async pool (DONE Wave 16 W16-7).
- Migration scripts SQLite → PostgreSQL (TODO — P1).
- Real-time pipeline (TODO — P1).
- Feature versioning backfill (IN PROGRESS).
- Status: **TODO** (foundation laid, migration not yet started).

### Phase 7 — Risk + capital model expansion
**Goal:** Kelly-fractional sizing with correlation-aware portfolio.
- Capital allocator saturating edge curve (DONE Wave 3).
- Portfolio optimizer mean-variance (DONE Wave 16 W16-5).
- Correlation matrix (DONE Wave 16 W16-6).
- Stress testing (DONE Wave 16 — needs regime library expansion).
- Kelly criterion (TODO — P2; currently using saturating curve).
- Historical data retention optimization (IN PROGRESS).
- Status: **IN PROGRESS**.

### Phase 8 — UI/UX completeness
**Goal:** Every backend feature is observable from the UI.
- Command center enhancements (IN PROGRESS).
- Deep analysis workstation (DONE Wave 8 — needs SHAP/Calmar panels).
- Observability expansion — Prometheus + Grafana (DONE Wave 14 — needs
  SLO dashboards).
- Alerting system (DONE Wave 16 — needs on-call routing).
- Status: **IN PROGRESS**.

### Phase 9 — Verification + audit
**Goal:** Every claim is verifiable; every action is auditable.
- UI functional verification (38 E2E tests, DONE Wave 11 — needs
  per-panel coverage).
- Design standard compliance (DONE Wave 9 — needs automated linter).
- Auditability improvements — immutable audit log (DONE Wave 5 —
  needs cross-DB integrity enforcement).
- Alerting system improvements (IN PROGRESS).
- Status: **IN PROGRESS**.

### Phase 10 — Pre-live readiness
**Goal:** 10/10 on the live safety gate; operator sign-off.
- All P0 areas DONE.
- 10-check live safety gate: 10/10 passing.
- Disaster-recovery drill: completed + documented.
- Operator runbook: finalized.
- Status: **TODO**.

### Phase 11 — Live trading + ongoing optimisation
**Goal:** Real capital is at risk; ongoing P3 polish.
- Live monitoring on-call rotation.
- Post-launch incident retrospectives.
- Continuous Kelly/Sharpe tuning.
- Status: **TODO** (gated on Phase 10 exit).

---

## 5. Cross-cutting dependencies

The following dependency edges determine the critical path:

```
Phase 0 (contracts)
   └─→ Phase 1 (safety scaffolding)
        └─→ Phase 2 (execution quality)
             └─→ Phase 3 (ML pipeline)
                  └─→ Phase 4 (backtest/calibration)
                       └─→ Phase 5 (data quality)
                            └─→ Phase 6 (PG/Timescale migration)
                                 └─→ Phase 7 (risk/capital)
                                      └─→ Phase 8 (UI)
                                           └─→ Phase 9 (verification)
                                                └─→ Phase 10 (pre-live)
                                                     └─→ Phase 11 (live)
```

Hard cross-domain dependencies (cannot be parallelised):

- **Idempotency keys (Bot Execution)** → blocks **Auditability
  improvements (Observability)** — the audit log needs stable keys to
  reconstruct chains.
- **Feature versioning (Data Platform)** → blocks **Calibration
  improvements (AI/ML)** — calibration refresh must be reproducible
  against a frozen feature set.
- **Shadow inference promotion gate (AI/ML)** → blocks **Backtest/live
  parity (Backtest)** — the parity harness needs the challenger-vs-
  incumbent comparison to be statistically significant before it can
  trust backtest as a live proxy.
- **PG/Timescale migration (Data Platform)** → blocks **Real-time
  pipeline** and **Historical data retention optimization** — both
  assume Postgres-native partitioning.
- **Stress testing expansion (Risk/Portfolio)** → blocks **Kelly
  criterion optimization** — Kelly without stress-tested covariance
  is unsafe.

---

## 6. Sequencing recommendations for the next wave (W18)

Given the critical-path edges above, the next wave (W18) should
target the **P0 + critical P1** items that unblock the largest
downstream surface:

1. **Idempotency keys on order submission + ledger writes** (P0) —
   unblocks auditability.
2. **Shadow inference promotion gate** (P0) — unblocks backtest/live
   parity.
3. **Data quality monitoring** (P0) — unblocks every downstream
   analytics claim.
4. **Order state machine: terminal-state guards + idempotency** (P0) —
   completes Phase 1.
5. **Circuit breaker: cross-strategy correlation-aware tripping** (P0)
   — completes Phase 1.
6. **Unified strategy contract** — close the 47 stub-strategy gap (P1).

P2 and P3 items are deferred until the P0 surface is clear.

---

## 7. Roadmap maintenance

- **Update cadence:** every wave (after the W17-9 staging commit) the
  status table in §3 is regenerated from the implementation plan +
  implementation status files. Statuses are the single source of truth;
  the wave-by-wave narrative in this document is descriptive only.
- **Add a new improvement:** open a row in the §3 table, file a row in
  `MASTER_IMPLEMENTATION_PLAN.md`, and reference the owning domain
  plan. Never describe an improvement only in prose — every
  improvement must have an `IMPL-*` ID in the implementation plan.
- **Retire an improvement:** when a `DONE` improvement has been live
  for 30 days with no incident, move it to §8 (Completed log) below.

---

## 8. Completed log (frozen improvements)

This section is empty at W17-9 — every improvement in §3 still has
residual work below the live-trading bar. Improvements will graduate
here as Phase 10 / Phase 11 exit criteria are met.

---

## 9. References

- God Mode Master Prompt §62 (improvement plan structure)
- God Mode Master Prompt §63 (per-improvement field set)
- God Mode Master Prompt §64 (P0–P3 priority scheme)
- God Mode Master Prompt §65 (implementation backlog format)
- God Mode Master Prompt §66 (Phase 0–11 sequencing)
- God Mode Master Prompt §69 (TODO / IN_ANALYSIS / IMPLEMENTING /
  TESTING / BLOCKED / DONE statuses)
- Per-domain improvement plans in this directory
- `docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`
- `docs/implementation/IMPLEMENTATION_STATUS.md`
- `docs/ARCHITECTURE.md`
- `docs/METRICS_SUMMARY.md`
- `docs/reassessment/FINAL_SYSTEM_REASSESSMENT.md`
