# Strategy + Backtest Implementation Roadmap

**Document owner:** general-purpose agent (W37-1)
**Date:** 2026-09-08
**Scope:** consolidates the strategy management and backtest engine
work across Waves 1–36 into a single wave-by-wave roadmap, with
per-wave status markers (`DONE` / `PARTIALLY DONE` / `TODO`) and
pointers to the source files that close each gap. Companion
documents:
- `docs/assessment/STRATEGY_MANAGEMENT_ASSESSMENT.md` — full
  §25–29 assessment (W17-5 baseline + W37-1 update).
- `docs/assessment/BACKTEST_ENGINE_ASSESSMENT.md` — full §30–33
  assessment (W17-6 baseline + W37-1 update).
- `docs/improvements/STRATEGY_MANAGEMENT_IMPROVEMENT_PLAN.md` —
  per-improvement plan (ST-1..ST-N).
- `docs/improvements/BACKTEST_ENGINE_IMPROVEMENT_PLAN.md` —
  per-improvement plan (BT-1..BT-N).

This roadmap is the single source of truth for "what wave closed
which gap". Each wave entry lists:
1. **Status** — `DONE` / `PARTIALLY DONE` / `TODO`.
2. **Goal** — what the wave set out to deliver.
3. **Delivered** — concrete file:line evidence the wave landed.
4. **Residual** — what the wave did NOT close (cross-references
   back to the assessment CF numbers).

---

## Wave 1 — Discovery, inventory, assessments — **DONE**

**Goal:** Discover the strategy and backtest code surface; produce
honest assessments of where the platform stands against God Mode
§25–33.

**Delivered:**
- `docs/assessment/STRATEGY_MANAGEMENT_ASSESSMENT.md` (W17-5
  baseline, 1,526 LOC of 23-section evidence). Score: **4.5 / 10**.
- `docs/assessment/BACKTEST_ENGINE_ASSESSMENT.md` (W17-6 baseline,
  1,095 LOC). Score: **3.5 / 10**.
- `docs/improvements/STRATEGY_MANAGEMENT_IMPROVEMENT_PLAN.md`
  (per-improvement plan ST-1..ST-N, 453 LOC).
- `docs/improvements/BACKTEST_ENGINE_IMPROVEMENT_PLAN.md`
  (per-improvement plan BT-1..BT-N, 452 LOC).

**Residual:** the W17-5 / W17-6 assessments remained the canonical
references for ~18 waves until the W37-1 update refreshed them.

---

## Wave 2 — Historical data quality — **DONE**

**Goal:** Ensure `market_snapshots` + `orderbook_ticks` tables exist,
are populated by the live data pipeline, and are queryable for
backtest replay.

**Delivered:**
- `core/market_db.py` — `record_snapshot()` writes per-token
  top-of-book snapshots (best_bid / best_ask / mid / spread /
  volume_24h / liquidity) into `market_snapshots`.
- `core/timescale_db.py::_init_sqlite_fallback` — schema definition
  for `market_snapshots` (token_id, timestamp, best_bid, best_ask,
  mid, spread, volume_24h, liquidity) + `orderbook_ticks`
  (best_bid_size, best_ask_size).
- `core/db/migrations/001_initial_schema.sql:247` —
  `market_snapshots` DDL.
- `core/db/migrations/001_initial_enterprise_schemas.sql:256` —
  `feature_snapshot` DDL (orphaned — see Wave 4 residual).
- `core/book_poller.py` — background poller that subscribes to
  tokens and refreshes `store.order_books` continuously; snapshots
  are persisted to `market_snapshots` on each poll cycle.

**Residual:** the `feature_snapshot` and `market_snapshots` tables
exist but are not linked back to trades (no `feature_snapshot_id` /
`market_snapshot_id` FK on `closed_positions`) — see Wave 4
residual.

---

## Wave 3 — Strategy interface, registry, lifecycle — **PARTIALLY DONE**

**Goal:** Land the §26 9-method `StrategyContract` ABC; refactor
the registry to honestly tag IMPLEMENTED vs PLANNED; introduce a
strategy lifecycle state machine (§27).

**Delivered:**
- `strategies/base.py:89-153` — `StrategyContract` ABC with
  `@abstractmethod` declarations for the 9 §26 methods
  (`metadata`, `configure`, `validate`, `generate_signal`,
  `estimate_edge`, `size_position`, `entry_logic`, `exit_logic`,
  `diagnostics`). `BaseStrategy` inherits and provides default
  implementations (`base.py:220-333`).
- `strategies/base.py:60-86` — `Signal` dataclass value type.
- `strategies/registry.py:30-32` — `STATUS_IMPLEMENTED` /
  `STATUS_PLANNED` / `STATUS_EXPERIMENTAL` lifecycle status
  constants.
- `strategies/registry.py:47` — `status` field on `StrategyMeta`
  dataclass (W19-6 honest status reporting).
- `strategies/registry.py:222-254` —
  `StrategyRegistry.get_catalog(implemented_only=False)` honors
  the filter and surfaces both `status` and derived `implemented`
  boolean + `is_disabled` flag (W24-8 auto-disable integration).
- `strategies/registry.py:129-144` —
  `_IMPLEMENTED_STRATEGY_CLASSES` dict mapping 11 catalog ids to
  concrete `BaseStrategy` subclasses.
- `strategies/registry.py:316-414` — `disable()` / `enable()` /
  `is_disabled()` SUSPENDED-equivalent operator/monitor path
  (W24-8).
- `strategies/registry.py:424-507` — `pause_for_market()` /
  `close_positions_for_market()` / `reset_market_state()` /
  `is_market_paused()` / `is_market_closed()` per-market pause /
  close state (W34-3) — called by `MarketEventIngester` on
  MARKET_SUSPENDED / MARKET_RESOLVED events.

**Residual:**
- 8 of 11 concrete strategies still rely on the default
  `BaseStrategy` implementations of the contract methods — they
  override `_run` + strategy-specific `evaluate` / `_scan_markets`
  / `_act_on_signal` instead. The contract is introspectable but
  not load-bearing. (Strategy assessment CF11.)
- §27's 9-state lifecycle machine (RESEARCH → EXPERIMENTAL →
  BACKTESTED → VALIDATED → PAPER → LIVE_CANDIDATE → LIVE →
  SUSPENDED → RETIRED) is not implemented. What exists is the
  coarse `STATUS_*` flag + the W24-8 `StrategyHealthStatus`
  four-state runtime health enum + the PAPER → LIVE gate in
  `core/live_safety_gate.py`. (Strategy assessment CF12.)

---

## Wave 4 — Attribution, reconciliation, metrics — **DONE** (metrics) / **PARTIALLY DONE** (attribution)

**Goal:** Close the §28 attribution chain (Trade → Strategy →
Strategy Version → Signal → Prediction → Model Version → Feature
Snapshot → Market Snapshot) and the §29 metrics surface (trades,
wins, losses, win rate, realized P&L, ROI, expectancy, profit
factor, Sharpe, Sortino, drawdown, turnover, exposure, capital
efficiency, slippage, average holding time).

**Delivered (metrics, §29):**
- `core/portfolio.py:281-324` — `_sharpe_ratio` /
  `_sortino_ratio` / `_calmar_ratio` helpers (W23-5).
- `core/portfolio.py:327-…` — `strategy_performance()`
  per-strategy dashboard with `expectancy`, `sharpe_ratio`,
  `sortino_ratio`, `calmar_ratio`, `max_drawdown`, `win_rate`,
  `profit_factor`, `avg_win`, `avg_loss`, `notional_volume`,
  `open_exposure`, `avg_hold_hours`, equity curve.
- `core/portfolio.py::strategy_stats` — live per-strategy roll-up
  (unchanged since W17-5: fills, gross/net P&L, capital_exposed,
  profit_per_dollar_exposed, profit_per_exposure_day,
  exposure_dollar_days, avg_holding_duration_hours, win_rate,
  profit_factor, avg_win, avg_loss, max_drawdown, notional_volume).
- `core/attribution.py:398-…` — `get_full_attribution()` 7-dimension
  roll-up (`by_strategy`, `by_confidence_bucket`, `by_edge_bucket`,
  `by_probability_band`, `by_liquidity_level`, `by_holding_period`,
  `by_trade_direction`). W11-9 single-SELECT optimization.
- `GET /api/leaderboard` (30s cache) + `GET /api/attribution`
  (60s cache) — both retained.

**Delivered (decision-ledger chain, partial §28):**
- `core/decision_ledger.py:111-116` — 6 canonical stages:
  `PREDICTION`, `SIGNAL`, `RISK_APPROVED`, `RISK_REJECTED`,
  `ORDER`, `FILL`. `GATE_REJECTED` added W24-3.
- `core/decision_ledger.py:272-273` — auto-stamp `model_version`
  on every `PREDICTION` stage event via `_resolve_active_model_version()`.
- `core/closed_positions.py:23-37` — schema with `strategy`,
  `decision_id`, `model_version`, `confidence`, `predicted_edge`,
  `p_yes`, `market_mid`, `liquidity`, `holding_seconds`,
  `direction`. Indexed on `(token_id, timestamp DESC)`,
  `(strategy, timestamp DESC)`, `(timestamp DESC)`.
- `strategies/base.py:622-711` — `BaseStrategy.submit_order`
  records `GATE_REJECTED` (W24-3) + `RISK_APPROVED` /
  `RISK_REJECTED` for every strategy (not just signal_trader).

**Residual (§28 chain still broken at 3 links):**
- `strategy_version` is referenced in
  `backtesting/experiment_store.py` (backtest experiment version)
  and the `002_unified_schema.sql` migration (column on
  `experiments`), but no production writer populates
  `strategy_version` on `closed_positions` or `decision_events`.
  (Strategy assessment CF2.)
- `feature_snapshot_id` is referenced in `core/timescale_db.py`
  and the `001_initial_enterprise_schemas.sql` migration but no
  production code writes the column on `closed_positions` rows.
  (Strategy assessment CF4.)
- `market_snapshot_id` likewise. (Strategy assessment CF4.)
- Only `signal_trader.py` emits `STAGE_PREDICTION` /
  `STAGE_SIGNAL`. The 10 other IMPLEMENTED strategies skip these
  stages. (Strategy assessment CF3.)

**Residual (§29 metrics):**
- Per-strategy turnover is not a computed field on
  `strategy_stats` (the only turnover-related code is a
  data-quality heuristic at `portfolio.py:131`).
- Slippage is tracked in `core/execution_quality.py` but not
  joined into `strategy_stats`. (Strategy assessment CF6
  residual.)

---

## Wave 5 — Deterministic replay, broker abstraction, persistence — **DONE**

**Goal:** Build a deterministic historical-replay backtest engine
that consumes `market_snapshots`; introduce a unified `Broker` ABC
that gives backtest/paper/live parity; persist every backtest run
to a queryable SQLite store.

**Delivered (deterministic replay, §30):**
- `backtesting/historical_replay.py::HistoricalReplayEngine` —
  the new §30 replay engine.
  - `load_snapshots(token_id, start_time, end_time)` —
    `SELECT` against `market_snapshots` with a LEFT JOIN on
    `orderbook_ticks` for `bid_size` / `ask_size`; graceful
    fallback to `market_snapshots`-only.
  - `replay(token_id, strategy, start_time, end_time,
    initial_capital)` — single-pass replay loop. Per snapshot:
    build `context` dict, call `strategy.generate_signal(context)`,
    act on returned signal (BUY at `best_ask`, SELL at `best_bid`),
    mark-to-market at `mid`.
  - Force-close any open position at the last snapshot so
    `total_return` reflects realised P&L.
  - `_compute_metrics` — total_return / Sharpe / max_drawdown /
    win_rate / profit_factor (annualised by `sqrt(252)`, matching
    the walk-forward convention).
- `backtesting/historical_replay.py::SimpleStrategy` — default
  mean-reversion strategy that ships with the module; pluggable
  via `strategy=` kwarg on `replay()`.
- `POST /api/backtest/historical-replay` (`api/server.py:4482`) —
  API route wrapping `HistoricalReplayEngine.replay()` in
  `asyncio.to_thread` + W20-3 experiment persistence.

**Delivered (broker abstraction, §32):**
- `core/broker.py::Broker` ABC (`broker.py:140-228`) with 6
  abstractmethods: `submit_order`, `cancel_order`,
  `get_order_status`, `get_positions`, `get_balance`,
  `apply_slippage`. Plus shared `_canonical_slippage` static
  helper.
- `core/broker.py::PaperBroker` (`broker.py:326-478`) —
  delegates to `paper.simulator.paper_sim`; `apply_slippage`
  delegates to `PaperSimulator._apply_slippage`.
- `core/broker.py::LiveBroker` (`broker.py:484-669`) — delegates
  to `core.clob_client.clob_client`; same canonical slippage
  estimator.
- `core/broker.py::BacktestBroker` (`broker.py:675-832`) —
  hermetic local ledger (own `_capital` + `_positions` dict);
  fills are immediate; `apply_slippage` uses the same canonical
  model.
- `core/broker.py::get_broker(mode)` factory (`broker.py:838-873`)
  — `"paper"` / `"live"` / `"backtest"`.
- `core/execution_interface.py` (W18-5) — `submit_exit_order` /
  `cancel_exit_order` paper/live branching helper for TP/SL
  exits.

**Delivered (experiment persistence, §33):**
- `backtesting/experiment_store.py::ExperimentStore` — SQLite
  persistence with `experiments` table (19 columns + 3 indexes).
  JSON blobs for `config` / `equity_curve` / `trades` capped at
  10 KB each.
- `backtesting/experiment_store.py::BacktestExperiment`
  dataclass — engine-agnostic row shape.
- `backtesting/experiment_store.py::experiment_store` — module-
  level singleton; fault-tolerant init.
- `GET /api/backtest/experiments` (`api/server.py:4639`) — list
  newest-first, optional strategy filter, limit clamped to
  [1, 1000].
- `GET /api/backtest/experiments/{experiment_id}`
  (`api/server.py:4685`) — fetch one experiment (decodes JSON
  blobs).
- `POST /api/backtest/compare` (`api/server.py:4715`) — compare
  multiple experiments by headline risk metrics.

**Residual:**
- The `Broker` ABC is present but `BaseStrategy.submit_order`
  still branches on `settings.paper_trade` directly. The Broker
  is available for new strategies but not load-bearing for the
  existing 11. (Backtest assessment CF10.)
- No parameter-sweep batch runner. (Backtest assessment CF4
  residual.)

---

## Wave 6 — Realistic execution, bias/leakage detection — **PARTIALLY DONE**

**Goal:** Realistic microstructure model (spread + depth + partial
fills + exec delay + sqrt market-impact); 6-rule look-ahead
detector.

**Delivered (realistic execution, §31):**
- `backtesting/engine.py::_SyntheticOrderBook` (`engine.py:347-401`)
  — 5-level synthetic book with exponential depth decay;
  `consume(side, shares)` walks levels and returns
  `(filled_shares, avg_fill_price)`.
- `backtesting/engine.py::_simulate_realistic_trade`
  (`engine.py:505-634`) — per-trade simulation with: spread +
  depth + walk-the-book partial fills + 1-3s exec delay +
  adverse mid drift + sqrt market-impact slippage + binary
  resolution.
- `backtesting/engine.py::run_realistic_backtest`
  (`engine.py:637-821`) — hourly cadence loop; returns
  `{trades, equity_curve, metrics, look_ahead_bias}`.

**Delivered (look-ahead detector, partial):**
- `backtesting/engine.py::_LookAheadDetector` (`engine.py:403-502`)
  — 6 rule classes:
  - LE_01 FUTURE_OUTCOME_LEAK — `p_model` saturates at the
    outcome extremum.
  - LE_02 ENTRY_PRICE_EXTREMUM — fill price equals period
    low/high within 1 bp.
  - LE_03 UNREALISTIC_WIN_RATE — backtest win-rate > 0.95
    over > 30 trades.
  - LE_04 FUTURE_TIMESTAMP_ACCESS — `data_ts > decision_ts`.
  - LE_05 STRATEGY_ATTRIBUTE_LEAK — strategy exposes a
    `future_*` or `*_leak` attribute.
  - LE_06 PERFECT_CALIBRATION — `corr(p_model, outcome) > 0.95`
    over > 30 trades.

**Residual:**
- LE_04 is dead code (`_simulate_realistic_trade` has no
  `data_ts` parameter; the rule is never invoked). (Backtest
  assessment B6.)
- LE_07 UNREALISTIC_SHARPE is NOT implemented — Sharpe ratios
  of 20-41 still slip through unflagged. (Backtest assessment
  CF5.)
- The new `historical_replay.py` engine has NO `_LookAheadDetector`
  instance. The §30 replay path is *less* guarded than the
  legacy §31 realistic-MC path. (Backtest assessment CF9.)
- Bugs B1-B8 from the W17-6 assessment are all still present
  (equity floor, annualisation mismatch, fabricated
  `monthly_returns`, walk-forward $1-bet simplification,
  unseeded MC). (Backtest assessment CF11.)

---

## Wave 7 — Backtest/live parity and paper validation — **PARTIALLY DONE**

**Goal:** Close §32 (Backtest/Live execution-interface parity);
validate by running the same strategy through both engines and
asserting PnL is within a tolerance band.

**Delivered:**
- `core/broker.py` (W19-7) — the `Broker` ABC + 3 concrete
  implementations + `get_broker(mode)` factory + shared
  `_canonical_slippage` helper. The single canonical slippage
  model lives on `PaperSimulator._apply_slippage`; every
  `Broker` subclass delegates via the shared helper.
- `core/execution_interface.py` (W18-5) — paper/live branching
  helper for TP/SL exit orders.
- `backtesting/historical_replay.py` (W20-3) — the new §30
  replay engine accepts a duck-typed strategy object exposing
  `generate_signal(context) -> dict | None`; the contract method
  exists on `BaseStrategy`, so any concrete subclass can be
  passed via the `strategy=` kwarg.

**Residual:**
- `BaseStrategy.submit_order` still branches on
  `settings.paper_trade` directly (does not consume the `Broker`
  ABC). The 11 IMPLEMENTED strategies don't get parity for free.
  (Backtest assessment CF10.)
- No integration test that runs the same `MarketMakerStrategy`
  against both engines and asserts PnL difference is within a
  tolerance band. (Backtest assessment "Recommended Next Actions"
  item 9.)
- The historical-replay engine bypasses the risk engine
  (`risk_manager.check_order`) and the decision ledger
  (`PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL` chain).
  (Backtest assessment §5 "Risk" stage marked BROKEN.)

---

## Wave 8 — Hardening, performance, regression testing — **IN PROGRESS**

**Goal:** Production hardening (pre-submission risk gate, order
state machine, dedup registry, latency tracker, strategy health
monitor, per-market pause/close); regression test coverage;
performance optimizations; observability.

**Delivered (hardening):**
- `core/pre_submission_gate.py` (W24-3) — 14-check pre-submission
  gate (kill switch / balance / exposure / single-position /
  open-orders / daily-loss / drawdown / data freshness / spread /
  liquidity / edge / confidence / idempotency / circuit breaker)
  runs BEFORE `risk_manager.check_order`.
- `core/order_state_machine.py` (W18-1) — SQLite-backed OSM with
  `CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN →
  FILLED / CANCELLED / REJECTED` transitions.
  `BaseStrategy.submit_order` pre-mints the OSM id and stamps
  every transition with `metadata.exchange_order_id` for live
  orders.
- `core/dedup.py` (W24-6) — duplicate-order prevention within a
  60s TTL by `token_id:side:size:price`.
- `core/latency_tracker.py` (W23-2) — signal→order→fill pipeline
  latency tracker per `decision_id`.
- `core/strategy_health.py` (W24-8) — `StrategyHealthMonitor`
  with four-state per-strategy lifecycle (`HEALTHY / DEGRADED /
  DISABLED / INACTIVE`) + five thresholds (min_win_rate=30%,
  min_expectancy=-$0.05, max_drawdown=15%,
  min_trades_for_eval=10, max_errors_per_hour=10,
  stale_strategy_hours=24) + sync `disable()` / `enable()` /
  `is_disabled()` on `StrategyRegistry`. `GET /api/strategies/health`
  + `GET /api/strategies/health/summary` exposed.
- `core/rejected_opportunities.py` (W24-3) — durable store for
  pre-submission-gate rejections + risk-engine rejections.
- `strategies/registry.py:424-507` (W34-3) — per-market pause /
  close state (called by `MarketEventIngester` on
  MARKET_SUSPENDED / MARKET_RESOLVED).
- `core/live_safety_gate.py` — PAPER → LIVE gate (24h paper +
  positive expectancy + MDD < $2). Unchanged since W17-5.
- `core/circuit_breaker.py` — runtime circuit-breaker for the
  CLOB client.

**Delivered (honest performance reporting):**
- `GET /api/performance-report/category/{category}` (W24-2 /
  W26-2) — separates backtest / walk-forward / paper / live
  metrics into per-category panels with a disclaimer.
- `core/performance_reporter.py` — generates the per-category
  report.

**Delivered (testing):**
- `tests/test_experiment_store.py` — `ExperimentStore` save /
  get / list / compare contract tests.
- `tests/test_backtest_engine.py` — 9 tests covering
  `run_realistic_backtest` shape, metrics, equity, look-ahead,
  slippage monotonicity (unchanged from W17-6).
- `tests/test_advanced_backtest.py` — 12 tests covering
  `walk_forward_analysis` + `monte_carlo_simulation`
  (unchanged from W17-6).
- `tests/test_backtest_report.py` — 20 tests covering
  `generate_report` + edge cases + serialisation + PDF + API
  routes + metric computations (unchanged from W17-6).
- `tests/test_paper_simulator.py` — 11 tests covering
  `paper_sim` fill logic + 3-component slippage model
  (unchanged from W17-6).
- Strategy tests for the 3 original strategies
  (`test_signal_trader.py`, `test_market_maker.py`,
  `test_arb_scanner.py`, `test_strategy_base.py`) — unchanged
  from W17-5.

**Residual:**
- No integration test of `MarketMakerStrategy` against both
  `run_realistic_backtest` and `HistoricalReplayEngine.replay()`
  over the same period. (Backtest assessment "Recommended Next
  Actions" item 9.)
- No property-based test for the look-ahead detector (a
  hypothesis-style test that injects a deliberately leaky
  strategy and asserts LE_01 fires). (Backtest assessment §17.4.)
- No load test of the PDF route. (Backtest assessment §17.4.)
- Per-strategy Sharpe / Sortino / Calmar / expectancy tests for
  the 8 newer strategies (`mean_reversion`, `momentum`, `value`,
  `stat_arb`, `event_driven`, `convergence`, `spread_capture`,
  `liquidity`) — coverage is lighter than for the 3 originals.
- No backtest observability: `engine.py` still emits zero log
  lines; no `record_metric("backtest", ...)` calls; no
  `polymarket_backtest_runs_total` Prometheus counter; no
  decision-ledger integration for backtest runs. (Backtest
  assessment §18.)
- Wave 8 is **IN PROGRESS** — the W24-2/W24-3/W24-6/W24-8
  hardening batch landed but the W37-1 follow-ups (LE_07,
  LookAheadDetector port to historical_replay, Broker ABC
  migration, B1-B8 bug fixes) are still TODO.

---

## Cross-wave summary table

| Wave | Status | Maturity delta |
|---|---|---|
| 1 — Discovery, inventory, assessments | **DONE** | baseline scores: Strategy 4.5 / 10, Backtest 3.5 / 10 |
| 2 — Historical data quality | **DONE** | `market_snapshots` schema + `book_poller` |
| 3 — Strategy interface, registry, lifecycle | **PARTIALLY DONE** | Strategy 4.5 → 6.0 (contract ABC + 11 IMPLEMENTED + status field; lifecycle machine still missing) |
| 4 — Attribution, reconciliation, metrics | **DONE** (metrics) / **PARTIALLY DONE** (attribution) | Strategy 6.0 → 6.5 (live Sharpe/Sortino/Calmar/expectancy; §28 chain still broken at 3 links) |
| 5 — Deterministic replay, broker abstraction, persistence | **DONE** | Backtest 3.5 → 6.0 (historical_replay + Broker ABC + ExperimentStore + 6 new API routes) |
| 6 — Realistic execution, bias/leakage detection | **PARTIALLY DONE** | Backtest 6.0 → 6.5 (realistic MC sim + 6 LE rules; LE_04 dead, LE_07 missing, historical_replay unguarded) |
| 7 — Backtest/live parity and paper validation | **PARTIALLY DONE** | Backtest 6.5 → 6.7 (Broker ABC present but not load-bearing) |
| 8 — Hardening, performance, regression testing | **IN PROGRESS** | Strategy 6.5 → 6.8 (pre-submission gate + OSM + dedup + latency tracker + health monitor + per-market pause); Backtest 6.5 → 6.7 (honest performance reporting + experiment store tests) |

**Updated maturity scores (W37-1):**
- Strategy management: **6.8 / 10** (was 4.5 / 10 at W17-5).
- Backtest engine: **6.7 / 10** (was 3.5 / 10 at W17-6).

---

## Highest-leverage remaining work (priority order)

1. **Port `_LookAheadDetector` into `historical_replay.py`** +
   add `LE_07 UNREALISTIC_SHARPE`. Closes Backtest CF5 + CF9.
2. **Wire `strategy_version` + `feature_snapshot_id` +
   `market_snapshot_id` into `closed_positions.record`** so the
   §28 attribution chain is reconstructable end-to-end. Closes
   Strategy CF2 + CF4.
3. **Migrate `BaseStrategy.submit_order` to consume a `Broker`
   instance** so §32 parity is load-bearing for the 11 existing
   strategies. Closes Backtest CF10 + Strategy CF7 residual.
4. **Emit `STAGE_SIGNAL` (with `confidence` + `predicted_edge`)
   from the 10 non-signal_trader strategies** so the §28 chain
   `Signal → Prediction` link is filled. Closes Strategy CF3 +
   CF10.
5. **Refactor the 8 newer concrete strategies to override the
   contract methods** (`metadata`, `generate_signal`,
   `estimate_edge`, `size_position`, `entry_logic`,
   `exit_logic`, `diagnostics`) so the §26 contract is
   load-bearing. Closes Strategy CF5 residual + CF11.
6. **Fix B1 (equity floor) + B3 (fabricated monthly_returns) +
   B8 (unseeded MC)** in the legacy `engine.py`. Closes Backtest
   CF11.
7. **Replace the legacy `/api/backtest/run` substring dispatch**
   with a `_IMPLEMENTED_STRATEGY_CLASSES` lookup; refuse to
   backtest PLANNED ids with a 400. Closes Strategy CF7 residual.
8. **Implement the §27 9-state lifecycle machine** with durable
   state transitions on disk; gate `start_strategy` on the
   current state. Closes Strategy CF12.
9. **Add a parameter-sweep batch runner** that sweeps
   `slippage_bps` and persists each run as an experiment.
   Closes Backtest CF4 residual.
10. **Add backtest observability** (record_metric + Prometheus
    counter + decision-ledger integration). Closes Backtest §18
    gap.

---

*End of Strategy + Backtest Implementation Roadmap. Generated by
W37-1 general-purpose agent.*
