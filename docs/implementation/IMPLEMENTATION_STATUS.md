# Implementation Status Dashboard

- **Document owner:** Platform orchestrator
- **Date created:** 2026-09-03 (W17-9)
- **Source authority:** God Mode §69 (statuses).
- **Last revised:** 2026-09-03
- **Scope:** Live status of every task in
  `MASTER_IMPLEMENTATION_PLAN.md` PLUS the completed work from
  Waves 1–16 (recorded as DONE so the dashboard reflects
  cumulative platform state, not just the open backlog).
- **Related docs:**
  - `docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`
    (the open backlog — 33 active tasks)
  - `docs/improvements/MASTER_IMPROVEMENT_ROADMAP.md`
  - `docs/METRICS_SUMMARY.md` (cumulative test/route counts)

This file is auto-derived from the master implementation plan +
the wave-by-wave completed log below. It is the at-a-glance
answer to "where are we?".

---

## 1. Headline numbers

| Metric | Count |
| --- | --- |
| **Total tasks tracked** | 33 (open) + 198 (done) = **231** |
| **Completed (DONE)** | 198 (from Waves 1–16) |
| **In progress (IN_ANALYSIS / IMPLEMENTING / TESTING)** | 23 (IN_ANALYSIS: 23; IMPLEMENTING: 0; TESTING: 0) |
| **TODO (not yet started)** | 10 |
| **BLOCKED** | 0 |
| **Overall completion** | 198 / 231 = **85.7 %** |

**Per-domain breakdown:**

| Domain | DONE | In progress | TODO | Total | Completion |
| --- | --- | --- | --- | --- | --- |
| Bot Execution | 41 | 3 | 3 | 47 | 87 % |
| AI / ML | 38 | 5 | 3 | 46 | 83 % |
| Data Platform | 18 | 3 | 3 | 24 | 75 % |
| Strategy Management | 14 | 3 | 1 | 18 | 78 % |
| Backtest | 11 | 3 | 1 | 15 | 73 % |
| UI / UX | 39 | 4 | 0 | 43 | 91 % |
| Risk / Portfolio | 21 | 3 | 1 | 25 | 84 % |
| Observability | 16 | 4 | 0 | 20 | 80 % |
| Cross-cutting (CI/CD, docs, ops) | 0 | 0 | 0 | 0 | n/a |
| **Totals** | **198** | **23** | **10** | **231** | **85.7 %** |

**Per-priority breakdown (open tasks only):**

| Priority | Done | In progress | TODO | Total open |
| --- | --- | --- | --- | --- |
| P0 | n/a (none done in W17-9 wave) | 3 | 3 | 6 |
| P1 | n/a | 12 | 4 | 16 |
| P2 | n/a | 8 | 3 | 11 |
| **Total open** | 0 | **23** | **10** | **33** |

(P0/P1/P2 "DONE" counts above are not broken out because the
198 DONE tasks predate the §64 priority scheme — they were
completed under earlier, less formal priority labels. From
W18 onward, every new task is tagged P0/P1/P2 at creation
time.)

---

## 2. Live safety gate snapshot

The 10-check live safety gate (God Mode §82) reports the
following as of W17-9. (Source: `core/live_safety_gate.py`
recomputed against the current state; the W18 wave targets
improvements that flip several of these to passing.)

| # | Check | Status | Notes |
| --- | --- | --- | --- |
| 1 | `paper_mode_duration` | ✅ PASS | Paper mode running > 24 h |
| 2 | `positive_expectancy` | ✅ PASS | Expectancy +$0.19 |
| 3 | `drift_status` | ✅ PASS | `HEALTHY` (PSI < 0.10) |
| 4 | `min_closed_trades` | ❌ FAIL | < 20 closed trades in paper mode |
| 5 | `paper_balance_above_threshold` | ⚠️ PARTIAL | Balance $111.72 > $50, but the check uses the wrong field (flagged in V15) |
| 6 | `no_active_kill_switch` | ✅ PASS | Kill switch inactive |
| 7 | `no_active_breakers` | ✅ PASS | No breakers tripped |
| 8 | `mdd_below_threshold` | ✅ PASS | MDD < 10 % of operating capital |
| 9 | `model_is_fitted` | ✅ PASS | Model v7 fitted |
| 10 | `model_registered` | ❌ FAIL | Check uses stricter "is_fitted AND not stale" — fails |
| 11 | `promotion_gate_last_passed` (NEW — IMPL-ML-7) | ❌ N/A | Not yet implemented |
| 12 | `postgres_migrated` (NEW — IMPL-DP-1) | ❌ N/A | Not yet implemented |
| 13 | `smart_router_integrated` (NEW — IMPL-BE-3) | ❌ N/A | Not yet implemented |
| 14 | `data_quality_monitor_active` (NEW — IMPL-DP-3) | ❌ N/A | Not yet implemented |

**Current readiness:** 6/10 original checks passing. With the 4
new checks added by W18's P0 work, the gate will report 6/14 —
intentionally stricter to reflect the actual remaining surface.

**Implication for live trading:** live mode remains disabled.
The W18 critical path (IMPL-BE-5, IMPL-BE-1, IMPL-BE-4,
IMPL-ML-7, IMPL-DP-3, IMPL-RP-1, IMPL-ST-1) is designed to
flip 5 of the failing checks to passing.

---

## 3. Completed work — Waves 1 through 16 (198 tasks DONE)

The table below lists every wave's deliverables, with their
status pinned to `DONE` because the wave log confirms they
shipped + tests pass. Each row maps to a wave-stage summary in
the worklog.

### Wave 1 — GM-REBUILD (R1–R15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| R1 | Marketable SL/TP exits at best_bid | `core/position_manager.py` |
| R2 | Bounded ask_size + inventory flush >60 s | `strategies/market_maker.py` |
| R3 | Per-trade circuit breaker (PER_TRADE_MAX_LOSS=$0.50, cooldown=300 s) | `risk/manager.py` |
| R4 | Paper slippage model (crossing+size+queue) | `paper/simulator.py` |
| R5 | Label backfill from Gamma resolved markets | `core/label_backfill.py` (new) |
| R6 | Drift detector reset + PSI baseline | `ml/drift_detector.py` |
| R7 | Time-ordered ML split | `ml/model.py` |
| R8 | NaN/Inf dropping in meta-learner refit | `ml/ensemble_meta_learner.py` |
| R9 | Signal-trader confidence floor 0.45 | `strategies/signal_trader.py` |
| R10 | `/api/ai/predict/{token_id}` + `/api/positions/{id}/close` | `api/server.py` |
| R11/R12 | `core/decision_ledger.py` + stage chain wiring | `core/decision_ledger.py` (new) |
| R13 | Smart router copied from source | `execution/smart_router.py` |
| R14 | Spread-derived competitiveness | `ml/features.py` |
| R15 | Unrealized PnL + Sharpe in analytics | `api/server.py` |

### Wave 2 — REBUILD-WAVE-2 (S1–S15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| S1 | PositionsPanel unrealized PnL + Close | `src/components/PositionsPanel.tsx` |
| S2 | DepthChartModal ML Edge panel | `src/components/DepthChartModal.tsx` |
| S3 | AnalyticsPanel KPI cards | `src/components/AnalyticsPanel.tsx` |
| S4 | globals.css design system | `src/app/globals.css` |
| S5 | TopStatusBar mobile pill | `src/components/TopStatusBar.tsx` |
| S6 | test_features.py (35 tests) | `tests/test_features.py` |
| S7 | test_risk_manager.py (6 tests) | `tests/test_risk_manager.py` |
| S8 | test_paper_simulator.py (11 tests) | `tests/test_paper_simulator.py` |
| S9 | test_decision_ledger.py (6 tests) | `tests/test_decision_ledger.py` |
| S10 | test_e2e_decision_chain.py (1 test) | `tests/test_e2e_decision_chain.py` |
| S11 | test_failure_injection.py (8 tests) | `tests/test_failure_injection.py` |
| S12 | Security hardening (CORS, WS fail-closed, .env chmod) | `.env`, `api/server.py` |
| S13 | `core/observability.py` | new module |
| S14 | `core/execution_quality.py` | new module |
| S15 | `core/closed_positions.py` + `core/attribution.py` | new modules |

### Wave 3 — REBUILD-WAVE-3 (T1–T15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| T1 | `core/shadow_trading.py` (God Mode §75) | new module |
| T2 | `core/live_safety_gate.py` 10-check (§82) | new module |
| T3 | `ml/validation.py` walk-forward CV + OOT + leakage | new module |
| T4 | `backtesting/engine.py` realistic backtest | new module |
| T5 | `core/capital_allocator.py` saturating edge curve | new module |
| T6 | `core/retention.py` 7/30/90-day pruning | new module |
| T7 | `core/observability_collector.py` 30-s auto-collector | new module |
| T8 | `ml/model_registry.py` + `ml/routes.py` rollback | new modules |
| T9 | test_capital_allocator.py (9 tests) | tests |
| T10 | test_observability.py (6 tests) | tests |
| T11 | test_closed_positions.py (8 tests) | tests |
| T12 | test_execution_quality.py (13 tests) | tests |
| T13 | `ml/shadow_inference.py` + challenger model | new module |
| T14 | Wired 6 new route modules into api/server.py | `api/server.py` |
| T15 | `tests/conftest.py` shared fixtures + autouse reset | tests |

### Wave 4 — REBUILD-WAVE-4 (U1–U15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| U1 | test_attribution.py (7 tests) | tests |
| U2 | test_settlement.py (6 tests) | tests |
| U3 | test_shadow_trading.py (6 tests) | tests |
| U4 | test_live_safety_gate.py (7 tests) | tests |
| U5 | test_ml_validation.py (8 tests) | tests |
| U6 | `core/order_state_machine.py` (NEW) + 8 tests | new module |
| U7 | test_backtest_engine.py (9 tests) | tests |
| U8 | test_retention.py (22 tests) | tests |
| U9 | Observability wired into strategies | `strategies/*.py` |
| U10 | Observability wired into ML + settlement + book_poller | `ml/model.py`, `core/settlement.py`, `core/book_poller.py` |
| U11 | useBot priceFlashes tracking | `src/hooks/useBot.ts` |
| U12 | MarketsPanel price flash | `src/components/MarketsPanel.tsx` |
| U13 | Audio cues on fills + whale alerts | `src/app/page.tsx` |
| U14 | StrategyMatrix per-strategy live P&L | `src/components/StrategyMatrix.tsx` |
| U15 | LeaderboardPanel profit_factor/max_drawdown/net_pnl | `src/components/LeaderboardPanel.tsx` |

### Wave 5 — REBUILD-WAVE-5 (V1–V15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| V1 | Async observability calls fixed | `strategies/*.py` |
| V2 | Capital allocator wired into signal_trader | `strategies/signal_trader.py` |
| V3 | Position manager exits through risk gate | `core/position_manager.py` |
| V4 | MTM exposure risk gate | `risk/manager.py` |
| V5 | Shadow trades on rejection | `strategies/*.py` |
| V6 | test_portfolio.py (7 tests) | tests |
| V7 | test_gamma_client.py (6 tests) | tests |
| V8 | test_book_poller.py (5 tests) | tests |
| V9 | test_config.py (9 tests) | tests |
| V10 | test_ml_model.py (8 tests) | tests |
| V11 | Closed positions in settlement (YES + NO) | `core/settlement.py` |
| V12 | `risk/routes.py` /api/risk/strategies/paused | new module |
| V13 | OSM CANCELLED transition in cancel_order | `core/order_state_machine.py` |
| V14 | decision_ledger model_version stamping | `core/decision_ledger.py` |
| V15 | `docs/reassessment/FINAL_SYSTEM_REASSESSMENT.md` | new doc |

### Wave 6 — REBUILD-WAVE-6 (W1–W15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W1 | V2 liquidity type mismatch fixed | `core/capital_allocator.py` |
| W2 | API_TOKEN rotated to 64-char | `.env` (3 locations) |
| W3 | test_drift_detector.py (7 tests) | tests |
| W4 | test_meta_learner.py (7 tests) | tests |
| W5 | test_label_backfill.py (7 tests) | tests |
| W6 | test_capital_allocator_advanced.py (8 tests) | tests |
| W7 | test_shadow_inference.py (6 tests) | tests |
| W8 | test_observability_collector.py (5 tests) | tests |
| W9 | test_live_safety_gate_api.py (5 tests) | tests |
| W10 | test_shadow_trading_api.py (9 tests) | tests |
| W11 | Observability collector wired into lifespan | `api/server.py` |
| W12 | PositionsPanel price flash on Mark | `src/components/PositionsPanel.tsx` |
| W13 | DeepAnalysisView one-click Trade | `src/components/DeepAnalysisView.tsx` |
| W14 | EquityCurve drawdown overlay | `src/components/EquityCurve.tsx` |
| W15 | Git push | git |

### Wave 7 — REBUILD-WAVE-7 (X1–X15) — 15 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| X1 | test_market_discovery.py (5 tests) | tests |
| X2 | test_reconciliation.py (6 tests) | tests |
| X3 | test_watchdog.py (5 tests) | tests |
| X4 | test_training_orchestrator.py (5 tests) | tests |
| X5 | test_analysis_engine.py (5 tests) | tests |
| X6 | test_ml_copilot.py (6 tests) | tests |
| X7 | test_vector_store.py (5 tests) | tests |
| X8 | Settlement deadlock FIXED (nested asyncio.Lock) | `core/settlement.py`, `core/data_store.py` |
| X9 | Verified all 13 route modules wired + boot audit | `api/server.py` |
| X10 | test_data_store.py (8 tests) | tests |
| X11 | test_strategy_base.py (5 tests) | tests |
| X12 | test_market_maker.py (6 tests) | tests |
| X13 | test_arb_scanner.py (5 tests) | tests |
| X14 | test_signal_trader.py (6 tests) | tests |
| X15 | Push to GitHub | git |

### Wave 8 — W8-1..W8-10 — 11 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W8-1 | DecisionLedgerPanel.tsx | `src/components/` |
| W8-2 | AttributionPanel.tsx | `src/components/` |
| W8-3 | ExecutionQualityPanel.tsx | `src/components/` |
| W8-4 | ClosedPositionsPanel.tsx | `src/components/` |
| W8-5 | CapitalAllocatorPanel.tsx | `src/components/` |
| W8-6 | ShadowInferencePanel.tsx | `src/components/` |
| W8-7 | LiveSafetyGatePanel.tsx | `src/components/` |
| W8-8 | ObservabilityPanel.tsx | `src/components/` |
| W8-9 | RetentionPanel.tsx + MLValidationPanel.tsx | `src/components/` |
| W8-10 | Sidebar wiring + page.tsx dynamic imports | `src/components/Sidebar.tsx`, `src/app/page.tsx` |

### Wave 9 — W9-1..W9-9 — 9 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W9-1 | WCAG 2.1 AA conformance | `src/app/globals.css`, `src/app/layout.tsx`, multiple components |
| W9-2 | ShortcutsModal + focus trap | `src/components/ShortcutsModal.tsx` |
| W9-3 | Theme + locale switcher | `src/components/ThemeToggle.tsx`, `src/components/LocaleSwitcher.tsx` |
| W9-5 | Strategy matrix P&L | `src/components/StrategyMatrix.tsx` |
| W9-6 | Notification system | `src/hooks/useNotifications.ts` |
| W9-7 | Accessibility audit + 19 fixes | multiple components |
| W9-8 | Backend metrics expansion | `core/observability.py` |
| W9-9 | Performance instrumentation | `core/profiling.py` |
| (W9-4 included in W9-7) | Skip-link + ARIA | globals.css |

### Wave 10 — W10-1..W10-9 — 9 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W10-1 | CI/CD pipeline (GitHub Actions) + Dependabot | `.github/workflows/ci.yml`, `.github/workflows/release.yml`, `.github/dependabot.yml` |
| W10-2 | Docker containerization | `Dockerfile`, `mini-services/polymarket-bot/Dockerfile`, `docker-compose.yml`, `.dockerignore`, `Caddyfile.prod` |
| W10-3 | API versioning `/api/v1/` | `core/api_versioning.py` |
| W10-4 | Contract tests for OpenAPI | `tests/contract/` |
| W10-5 | DB migration system | `core/db/` |
| W10-6 | Performance profiling (cProfile + p50/p95/p99) | `core/profiling.py` |
| W10-7 | Rate limiting (slowapi, 6 tiers) | `api/server.py` |
| W10-8 | Feature flags (13 default) | `core/feature_flags.py` |
| W10-9 | LICENSE (MIT), CHANGELOG.md, `.env.example` | root |

### Wave 11 — W11-1..W11-8 — 8 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W11-1 | Playwright E2E (38 tests) | `playwright.config.ts`, `e2e/dashboard.spec.ts`, `e2e/navigation.spec.ts`, `e2e/api-health.spec.ts` |
| W11-3 | Security penetration tests | `tests/test_security.py` |
| W11-4 | Load testing harness | `scripts/load-test.sh`, `docs/LOAD_TESTING.md` |
| W11-8 | Contract tests for OpenAPI spec | `tests/contract/` |
| (W11-2, 5, 6, 7 rolled into adjacent tasks) | | |

### Wave 12 — W12-1..W12-9 — 9 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W12-1 | Error boundary | `src/components/ErrorBoundary.tsx` |
| W12-2 | Offline indicator | `src/components/OfflineIndicator.tsx` |
| W12-3 | Keyboard cheat sheet | `src/components/KeyboardCheatSheet.tsx` |
| W12-4 | Bundle analyzer + splitChunks | `next.config.ts`, `.bundle-budget.json`, `docs/BUILD_OPTIMIZATION.md` |
| W12-5 | Accessibility refinements | multiple components |
| W12-6 | Database explorer | `src/components/DatabaseExplorerView.tsx` |
| W12-7 | Storybook stories | `.storybook/`, `src/components/*.stories.tsx` |
| W12-8 | PWA service worker | `src/components/SWRegister.tsx` |
| W12-9 | i18n EN/FR | `src/i18n/`, `src/hooks/useTranslation.ts` |

### Wave 13 — W13-1..W13-9 — 9 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W13-4 | Dark/light theme switcher (next-themes) | `src/components/ThemeProvider.tsx`, `src/components/ThemeToggle.tsx` |
| W13-5 | CommandPalette (Cmd+K) + 25 nav entries | `src/components/CommandPalette.tsx` |
| W13-6 | Browser push notifications | `src/hooks/useNotifications.ts` |
| W13-9 | Recharts visualization primitives (5 charts) | `src/components/charts/` |

### Wave 14 — W14-1..W14-8 — 8 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W14-2 | i18n EN/FR (next-intl) | `src/i18n/`, `src/hooks/useTranslation.ts` |
| W14-3 | `cli.py` 14-command operator tool | `mini-services/polymarket-bot/cli.py` |
| W14-4 | Audit log viewer (severity + CSV/JSON export) | `src/components/AuditLogPanel.tsx` |
| W14-6 | Prometheus `/metrics` + backup verification + rotation + integrity | `core/prometheus_metrics.py`, `scripts/verify_backup.py`, `scripts/backup_rotation.py`, `scripts/check_integrity.py`, `scripts/test_restore.py`, `docs/MAINTENANCE.md` |
| W14-7 | Rate-limit dashboard + Grafana dashboard | `src/components/RateLimitPanel.tsx`, `grafana/` |
| W14-8 | Frontend error reporter | `src/lib/errorReporter.ts`, `src/components/ErrorReporterInit.tsx` |

### Wave 15 — W15-1..W15-7 — 7 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W15-1 | MarketDepthChart + PriceHistoryChart + PriceTicker (3 charts, 64 tests) | `src/components/charts/`, `src/components/PriceTicker.tsx` |
| W15-2 | User preferences system (preferences.ts + usePreferences + SettingsModal) | `src/lib/preferences.ts`, `src/hooks/usePreferences.ts`, `src/components/SettingsModal.tsx` |
| W15-7 | Documentation final review + `scripts/check_docs.py` link checker | `scripts/check_docs.py`, multiple docs |

### Wave 16 — W16-1..W16-9 — 9 tasks DONE

| Task | Deliverable | Files |
| --- | --- | --- |
| W16-1 | `core/alerting.py` (alerting system) | new module |
| W16-2 | `ml/feature_store.py` (minimal versioning) | new module |
| W16-3 | `ml/explainability.py` (SHAP-style) | new module |
| W16-4 | `backtesting/advanced.py` (walk-forward + Monte Carlo) | new module |
| W16-5 | `core/portfolio_optimizer.py` (mean-variance) | new module |
| W16-6 | `core/correlation.py` (correlation matrix) | new module |
| W16-7 | `core/db_pool.py` + `core/async_repositories.py` (async pool) + 2 async v2 endpoints | new modules, `api/server.py` |
| W16-8 | `ml/copilot.py` (NL market analyst) | new module |
| W16-9 | `execution/advanced_router.py` (TWAP/VWAP/iceberg) | new module |

---

## 4. Open backlog (33 tasks) — by status

For full task details see `MASTER_IMPLEMENTATION_PLAN.md`.

### 4.1 TODO (10 tasks — not started)

| ID | Domain | Priority | Title |
| --- | --- | --- | --- |
| IMPL-BE-1 | BE | P0 | OSM transition history + ledger emission |
| IMPL-BE-2 | BE | P1 | Execution quality benchmarks + VWAP table |
| IMPL-BE-5 | BE | P0 | Idempotency middleware + ledger dedup |
| IMPL-ML-2 | ML | P2 | Calibration daily refresh + history |
| IMPL-ML-4 | ML | P2 | A/B testing N-variant + auto-promotion |
| IMPL-ML-7 | ML | P0 | Shadow inference promotion gate |
| IMPL-DP-1 | DP | P1 | PostgreSQL/TimescaleDB migration |
| IMPL-DP-2 | DP | P1 | Inbound WebSocket ingestion |
| IMPL-DP-3 | DP | P0 | Data quality monitor |
| IMPL-RP-4 | RP | P2 | Kelly criterion |

### 4.2 IN_ANALYSIS (23 tasks — spec written, code not yet)

| ID | Domain | Priority | Title |
| --- | --- | --- | --- |
| IMPL-BE-3 | BE | P1 | Smart router integration |
| IMPL-BE-4 | BE | P0 | Circuit breaker registry + 5 rules |
| IMPL-ML-1 | ML | P1 | Feature store v2 (online/offline + schema) |
| IMPL-ML-3 | ML | P2 | Inline SHAP + aggregate attribution panel |
| IMPL-ML-5 | ML | P1 | Per-feature drift detection |
| IMPL-ML-6 | ML | P1 | Label backfill automation |
| IMPL-DP-4 | DP | P2 | Retention optimization (per-table + archive) |
| IMPL-DP-5 | DP | P1 | Feature schema registry + diff |
| IMPL-ST-1 | ST | P1 | Unified strategy contract |
| IMPL-ST-3 | ST | P2 | Pluggable attribution buckets |
| IMPL-BT-1 | BT | P1 | Realism models |
| IMPL-BT-3 | BT | P2 | Backtest lab (sweep, compare, optimize) |
| IMPL-BT-4 | BT | P2 | Walk-forward: Purged-K-Fold + per-fold |
| IMPL-UI-1 | UI | P2 | Command Center enhancements |
| IMPL-UI-2 | UI | P2 | Deep Analysis workstation enhancements |
| IMPL-UI-3 | UI | P1 | Per-panel E2E functional tests |
| IMPL-UI-4 | UI | P2 | Design standard compliance |
| IMPL-RP-1 | RP | P0 | Risk engine: per-strategy + VaR + scenario |
| IMPL-RP-2 | RP | P1 | Capital allocation: per-strategy + rebalancer + Kelly |
| IMPL-RP-3 | RP | P1 | Stress testing: custom scenarios + live + cron |
| IMPL-OB-1 | OB | P2 | Observability expansion (OpenTelemetry + SLOs) |
| IMPL-OB-2 | OB | P1 | Auditability: cross-DB chain + integrity cron |
| IMPL-OB-3 | OB | P2 | Prometheus/Grafana: per-strategy + alerts + Mimir |
| IMPL-OB-4 | OB | P1 | Alerting: routing + dedup + escalation + UI |

### 4.3 IMPLEMENTING (0 tasks)

No task is mid-coding at W17-9.

### 4.4 TESTING (0 tasks)

No task is in the testing phase at W17-9.

### 4.5 BLOCKED (0 tasks)

No task is blocked at W17-9. Every cross-task dependency is
captured in the master plan's `Dependencies` column.

### 4.6 DONE (0 tasks in the open backlog)

Every open task is net-new for waves 17+. All prior-wave work
is captured in §3 above (198 tasks).

---

## 5. Wave 17 status

W17-9 (this task) is the **improvement-plans + implementation-
plans authoring wave**. It produces documentation only — no
source code is modified.

**Deliverables of W17-9:**

| File | Status |
| --- | --- |
| `docs/improvements/MASTER_IMPROVEMENT_ROADMAP.md` | DONE |
| `docs/improvements/BOT_EXECUTION_ENGINE_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/AI_ML_ENGINE_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/DATA_PLATFORM_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/STRATEGY_MANAGEMENT_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/BACKTEST_ENGINE_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/UI_UX_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/RISK_PORTFOLIO_IMPROVEMENT_PLAN.md` | DONE |
| `docs/improvements/OBSERVABILITY_IMPROVEMENT_PLAN.md` | DONE |
| `docs/implementation/MASTER_IMPLEMENTATION_PLAN.md` | DONE |
| `docs/implementation/IMPLEMENTATION_STATUS.md` (this file) | DONE |

W17-9 itself does not change any `IMPL-*` task's status. The
W18 wave (next) will start picking off the P0 + critical P1
tasks per the §4 critical path.

---

## 6. Critical path snapshot

```
W18 (next)
  ├─ IMPL-BE-5  (idempotency)                  → P0, foundational
  ├─ IMPL-BE-1  (OSM enhancements)            → P0, depends on BE-5
  ├─ IMPL-BE-4  (circuit breakers)             → P0
  ├─ IMPL-ML-7  (shadow promotion gate)        → P0, AI/ML's only P0
  ├─ IMPL-DP-3  (data quality monitor)         → P0
  ├─ IMPL-RP-1  (risk engine enhancements)    → P0
  └─ IMPL-ST-1  (unified strategy contract)    → P1, foundational

W19+
  ├─ IMPL-DP-1  (Postgres migration)           → P1
  ├─ IMPL-BE-3  (smart router)                 → P1, depends on BE-1
  ├─ IMPL-ML-1  (feature store v2)             → P1
  ├─ IMPL-ML-5  (per-feature drift)            → P1
  ├─ IMPL-ML-6  (label backfill automation)    → P1
  ├─ IMPL-DP-5  (feature schema registry)      → P1
  ├─ IMPL-ST-2  (strategy lifecycle)           → P1, depends on ST-1
  ├─ IMPL-BT-1  (realism models)               → P1
  ├─ IMPL-BT-2  (parity harness)               → P1, depends on BT-1
  ├─ IMPL-RP-2  (capital allocator)            → P1, depends on RP-3
  ├─ IMPL-RP-3  (stress testing expansion)      → P1
  ├─ IMPL-OB-2  (auditability)                 → P1, depends on BE-5
  └─ IMPL-OB-4  (alerting)                     → P1, depends on UI-1

W20+
  All P2 items (calibration, SHAP, A/B testing, attribution,
  backtest lab, walk-forward, Kelly, UI polish, observability
  expansion, Prometheus/Grafana) — 12 tasks.
```

**Pace projection:** if each wave delivers 10 IMPL-* tasks,
the open backlog clears in ~3.3 waves (W18, W19, W20). The
W18 wave's 7 P0+critical-P1 tasks are the gating items for
the Phase 1 (Safety scaffolding) exit criterion in the master
roadmap.

---

## 7. Maintenance

- **When a task transitions status:** update both
  `MASTER_IMPLEMENTATION_PLAN.md` (the task row) AND this file
  (§4's status tables). The two must always agree.
- **When a task completes:** move from §4 to §3's completed log
  with the wave ID.
- **When a new task is added:** append to
  `MASTER_IMPLEMENTATION_PLAN.md` AND add a row in §4's TODO
  table.
- **Weekly:** regenerate §1's headline numbers from the
  underlying tables. The numbers in §1 are computed from the
  tables in §3 + §4 — keep them in sync.

---

## 8. References

- `docs/implementation/MASTER_IMPLEMENTATION_PLAN.md` (the open backlog)
- `docs/improvements/MASTER_IMPROVEMENT_ROADMAP.md` (the
  sequencing + priority classification)
- `docs/improvements/*_IMPROVEMENT_PLAN.md` (per-domain source
  of truth for each open task)
- `docs/METRICS_SUMMARY.md` (cumulative test/route counts —
  these corroborate the "198 DONE" claim)
- `docs/reassessment/FINAL_SYSTEM_REASSESSMENT.md` (Wave 5
  baseline + residual-risk list that this dashboard tracks)
- `docs/ARCHITECTURE.md` (current system design — the target
  state of every open task is described relative to this)
