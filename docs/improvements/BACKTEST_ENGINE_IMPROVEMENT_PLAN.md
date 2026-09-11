# Backtest Engine — Improvement Plan

- **Domain:** Backtest engine (realism, parity, lab features,
  walk-forward enhancement)
- **Owning modules:** `backtesting/engine.py`,
  `backtesting/advanced.py`, `backtesting/report.py`,
  `core/label_backfill.py`, `ml/validation.py`
- **Source authority:** God Mode §31 (backtest realism), §32
  (backtest/live parity), §33 (backtest lab features).
- **Priority classification (per God Mode §64):**
  - P1 — backtest realism, backtest/live parity.
  - P2 — backtest lab features, walk-forward enhancement.
- **Status as of W17-9:** IN PROGRESS — see per-improvement status
  below.

This plan defines every improvement in the backtest engine using
the per-improvement field set required by God Mode §63. Each
improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement BT-1 — Backtest Realism (§31)

- **Problem:** `backtesting/engine.py` (T4, Wave 3) implements
  slippage, partial fills, execution delay, and look-ahead
  detection. However, several realism gaps remain: (a) the
  slippage model is a flat bps-per-side — no market-impact
  component (large orders should slip more than small ones); (b)
  the partial-fill model is binary (50 % / 100 %) — no
  size-vs-book-depth-aware partial percentage; (c) the execution
  delay is a fixed 1 s — no latency distribution; (d) no
  funding/financing costs (Polymarket doesn't charge funding but
  the opportunity cost of capital is real); (e) no
  cancel-replace simulation (a strategy that cancels + replaces
  an order eats queue position).
- **Evidence:**
  - `tests/test_backtest_engine.py` (U7, Wave 4) — 9 tests
    covering slippage, partials, lookahead, equity curve.
  - `backtesting/advanced.py` (W16-4) — adds walk-forward + Monte
    Carlo but reuses the same slippage model.
  - `docs/ARCHITECTURE.md` §6 documents the paper slippage model
    (crossing + size + queue) but the BACKTEST engine uses a
    simpler flat-bps model.
- **Current State:** Flat-bps slippage; binary partials; fixed
  1-s delay; no funding; no cancel-replace simulation.
- **Desired State:**
  1. **Square-root market impact model** — slippage = base_bps +
     k * sqrt(order_size / book_depth_at_top). The `k` constant
     is calibrated from live execution-quality data (BE-2).
  2. **Continuous partial fills** — the fill percentage is
     `min(1.0, book_depth_at_top / order_size)`, sampled from a
     uniform distribution in `[0.5, 1.0]` when book_depth is
     insufficient.
  3. **Latency distribution** — execution delay is sampled from a
     lognormal distribution with mean=1 s, sigma=0.3 (calibrated
     from live latency data).
  4. **Opportunity cost of capital** — daily cost of `position_size
     * risk_free_rate / 365`.
  5. **Cancel-replace penalty** — a strategy that cancels + replaces
     an order within 10 s eats a 10-bps queue-position penalty on
     the next fill.
  6. The realism model is **configurable** via a `BacktestRealismConfig`
     dataclass — operators can disable specific components for
     sensitivity analysis.
- **Proposed Solution:**
  1. `MarketImpactModel` class (square-root).
  2. `PartialFillModel` class (continuous).
  3. `LatencyDistribution` class (lognormal sampler).
  4. `OpportunityCostModel` class.
  5. `CancelReplacePenalty` class.
  6. `BacktestRealismConfig` dataclass.
  7. Refactor `backtesting/engine.py::BacktestEngine` to use these
     models.
  8. The realism level is recorded in the backtest report
     (per-run audit trail).
- **Architecture:**
  ```
  BacktestRealismConfig(
    impact_model="sqrt",         # or "flat"
    partial_fill="continuous",  # or "binary"
    latency="lognormal",        # or "fixed"
    opportunity_cost=True,
    cancel_replace_penalty=True,
  )
  BacktestEngine(realism=config)
    └─→ for each signal:
         ├─→ impact = MarketImpactModel.compute(signal.size, book_depth)
         ├─→ partial_pct = PartialFillModel.sample(signal.size, book_depth)
         ├─→ delay = LatencyDistribution.sample()
         ├─→ cost = OpportunityCostModel.compute(position, days_held)
         └─→ if signal.is_cancel_replace:
              penalty = CancelReplacePenalty.compute()
         └─→ fill = Fill(price=signal.price + impact, qty=signal.size*partial_pct, ...)
  ```
- **Implementation:**
  1. New module `backtesting/realism.py` with the 5 model classes
     + `BacktestRealismConfig`.
  2. Refactor `backtesting/engine.py` to accept the config.
  3. Calibrate `k` from live execution-quality data (BE-2's
     `execution_quality` table).
  4. Tests for each model + the configurable realism level.
- **Files Affected:**
  - `mini-services/polymarket-bot/backtesting/realism.py` (new)
  - `mini-services/polymarket-bot/backtesting/engine.py` (refactor)
  - `mini-services/polymarket-bot/backtesting/advanced.py`
    (extend to accept the realism config)
  - `mini-services/polymarket-bot/backtesting/report.py`
    (record realism level in the report)
  - `mini-services/polymarket-bot/tests/test_backtest_engine.py`
    (expand from 9 → ~25 tests)
  - `mini-services/polymarket-bot/tests/test_backtest_realism.py`
    (new)
- **Dependencies:** BE-2 (execution-quality data feeds the
  calibration); DP-1 (Postgres migration — calibration queries
  benefit from the speedup).
- **Risk:** MEDIUM — the realism model directly affects the
  backtest's predicted P&L. Mitigation: A/B the old flat-bps
  model against the new model in a known window; the difference
  should be small (the new model should be more conservative).
- **Priority:** P1 (model quality — backtest decisions are only
  as good as the realism).
- **Expected Benefit:**
  - Backtest P&L matches live P&L within 5 % (the parity target
    of BT-2).
  - Market-impact model surfaces strategy capacity limits (a
    strategy that looks good at $1k/trade but slips 50 bps at
    $10k/trade is capacity-constrained).
  - Operators can disable specific realism components for
    sensitivity analysis ("how much P&L is funding cost?").
- **Tests:** +16 tests covering each model, the configurable
  realism level, the calibration lookup, the report-level audit
  trail.
- **Metrics:**
  - `backtest_realism_impact_bps` histogram.
  - `backtest_realism_partial_pct` histogram.
  - `backtest_realism_latency_ms` histogram.
  - `backtest_realism_cost_total` gauge.
- **Acceptance Criteria:**
  - All 25 backtest-engine tests pass.
  - A backtest with the new realism model on a known 30-day
    window reports P&L within 5 % of the live P&L for the same
    window.
  - The report records every realism component's contribution.
- **Status:** IN PROGRESS.

---

## Improvement BT-2 — Backtest/Live Parity (§32)

- **Problem:** There is no automated parity check between backtest
  and live. An operator can run a backtest for the same window
  the live system traded, but there is no harness that (a)
  ingests both backtest + live results, (b) aligns them by
  timestamp + token, (c) computes the P&L delta per trade, (d)
  surfaces trades with delta > 5 % as "parity violations", (e)
  alerts if the violation rate exceeds 10 %.
- **Evidence:**
  - `backtesting/engine.py::BacktestEngine.run()` returns a
    `BacktestReport` with `trades`, `equity_curve`, `metrics`.
  - `core/closed_positions.py::get_closed()` returns live
    closed positions.
  - No harness joins them.
  - `FINAL_SYSTEM_REASSESSMENT.md` §4 lists "backtest/live
    parity harness" as a residual risk.
- **Current State:** Manual parity analysis (operator eyeballs
  two reports).
- **Desired State:**
  1. `BacktestLiveParityHarness` class — ingests
     `BacktestReport` + `closed_positions` for the same window.
  2. Per-trade alignment: matches backtest trades to live trades
     by `(token_id, entry_time ± 60 s)`.
  3. Parity metrics: per-trade P&L delta, fill-price delta,
     slippage delta, entry-time delta.
  4. Violation detection: any trade with P&L delta > 5 % is
     flagged.
  5. New endpoint `POST /api/backtest/parity` — accepts a
     backtest report + a date range, returns the parity analysis.
  6. UI: `BacktestParityPanel.tsx` — renders the per-trade delta
     scatter plot + the violation list.
- **Proposed Solution:**
  1. `BacktestLiveParityHarness` class in
     `backtesting/parity.py` (new).
  2. Per-trade alignment algorithm (greedy nearest-timestamp
     match).
  3. Parity metrics computation.
  4. Endpoint + UI panel.
  5. Alert integration: if violation rate > 10 %, alert via
     `core/alerting.py`.
- **Architecture:**
  ```
  BacktestLiveParityHarness.analyze(backtest_report, live_window)
    └─→ for each backtest_trade:
         find live_trade with same token_id + entry_time within ±60s
         if found:
           pnl_delta = backtest_trade.pnl - live_trade.pnl
           fill_price_delta = backtest_trade.entry - live_trade.entry
           if abs(pnl_delta / live_trade.pnl) > 0.05:
             violations.append({trade, delta, ...})
    └─→ summary:
         matched_trades, unmatched_backtest, unmatched_live,
         violation_count, violation_rate
    └─→ if violation_rate > 0.10:
         alerting.alert("parity_violation", severity="WARN")
  POST /api/backtest/parity
    └─→ body: { backtest_report: ..., live_window: [start, end] }
    └─→ response: { summary, per_trade: [...], violations: [...] }
  BacktestParityPanel.tsx
    └─→ scatter plot (backtest P&L vs live P&L) + violation table
  ```
- **Implementation:**
  1. New module `backtesting/parity.py`.
  2. Alignment algorithm.
  3. Endpoint + UI panel.
  4. Alert integration.
- **Files Affected:**
  - `mini-services/polymarket-bot/backtesting/parity.py` (new)
  - `mini-services/polymarket-bot/backtesting/report.py`
    (extend to include realism level)
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `mini-services/polymarket-bot/core/alerting.py` (extend)
  - `src/components/BacktestParityPanel.tsx` (new)
  - `src/components/BacktestLabView.tsx` (link to parity panel)
  - `mini-services/polymarket-bot/tests/test_backtest_parity.py`
    (new)
- **Dependencies:** BT-1 (realism — the parity harness is only
  meaningful if the backtest is realistic); ML-7 (shadow
  inference promotion gate — the parity harness is a precondition
  for the gate to consider a model "backtest-valid").
- **Risk:** LOW — additive; the harness reads existing data.
- **Priority:** P1 (core architecture — parity is a precondition
  for trusting any backtest result).
- **Expected Benefit:**
  - Operators can answer "does my backtest match my live?" in
    one click.
  - Parity violations surface realism gaps (BT-1) immediately.
  - The shadow-inference promotion gate (ML-7) uses parity as
    one of its criteria.
- **Tests:** +14 tests covering alignment, parity metrics,
  violation detection, endpoint schema, UI rendering, alert
  integration.
- **Metrics:**
  - `backtest_parity_violation_rate` gauge.
  - `backtest_parity_matched_trades` gauge.
  - `backtest_parity_unmatched_total{side}` counter.
- **Acceptance Criteria:**
  - All 14 parity tests pass.
  - A backtest with known divergence (e.g. flat-bps slippage vs
    live with market impact) reports a violation rate > 10 %.
  - The parity panel renders within 1 s of submitting the
    backtest report.
- **Status:** TODO.

---

## Improvement BT-3 — Backtest Lab Features (§33)

- **Problem:** `backtesting/advanced.py` (W16-4) ships
  walk-forward + Monte Carlo, but the lab lacks several
  features operators expect: (a) parameter sweeps (run the same
  strategy with `min_confidence` from 0.4 to 0.7 in 0.05 steps);
  (b) strategy comparison (run 2 strategies on the same window
    + compare); (c) regime filtering (run only on bull/bear/sideways
    sub-windows); (d) parameter optimization (grid search + Bayesian
    optimization).
- **Evidence:**
  - `tests/test_backtest_advanced.py` (W16-4) — 11 tests covering
    walk-forward + Monte Carlo.
  - `src/components/BacktestLabView.tsx` (W8-10) — renders the
    walk-forward + Monte Carlo results but has no parameter-sweep
    or strategy-comparison UI.
- **Current State:** Walk-forward + Monte Carlo only.
- **Desired State:**
  1. **Parameter sweep**: `BacktestEngine.sweep(param_name,
     param_values)` returns a list of reports.
  2. **Strategy comparison**: `BacktestEngine.compare(strategies,
     window)` returns a comparison report.
  3. **Regime filtering**: `RegimeFilter` class labels each
     sub-window as bull/bear/sideways; the engine can run only
     on a chosen regime.
  4. **Grid search**: `BacktestOptimizer.grid_search(params,
     metric)` returns the best parameter combination.
  5. **Bayesian optimization** (optional): `BacktestOptimizer.
     bayesian_optimize(params, metric, n_trials=50)`.
  6. UI: `BacktestLabView.tsx` gains a "Sweep" tab + a "Compare"
     tab + an "Optimize" tab.
- **Proposed Solution:**
  1. Extend `BacktestEngine` with `sweep`, `compare`.
  2. New `RegimeFilter` class (uses moving-average crossover to
     label regimes).
  3. New `BacktestOptimizer` class (grid search + Optuna-based
     Bayesian optimization — Optuna is already a popular Python
     library).
  4. UI tabs in `BacktestLabView.tsx`.
- **Architecture:**
  ```
  BacktestEngine.sweep("min_confidence", [0.40, 0.45, 0.50, 0.55, 0.60])
    └─→ for each value:
         run(strategy with config.min_confidence=value)
         collect report
    └─→ return [report1, report2, ...] + summary (best by metric)
  BacktestEngine.compare([strategy_a, strategy_b], window)
    └─→ for each strategy: run(window) → report
    └─→ return comparison_report
  RegimeFilter.label(window)
    └─→ for each sub-window: classify as BULL / BEAR / SIDEWAYS
  BacktestOptimizer.grid_search(
    {"min_confidence": [0.4..0.7], "max_position": [10, 25, 50]},
    metric="sharpe"
  )
    └─→ for each combination: run → collect metric
    └─→ return best
  ```
- **Implementation:**
  1. Extend `backtesting/engine.py`.
  2. New `backtesting/regime.py`.
  3. New `backtesting/optimizer.py`.
  4. UI tabs + forms.
- **Files Affected:**
  - `mini-services/polymarket-bot/backtesting/engine.py` (extend)
  - `mini-services/polymarket-bot/backtesting/regime.py` (new)
  - `mini-services/polymarket-bot/backtesting/optimizer.py` (new)
  - `mini-services/polymarket-bot/requirements.txt` (add optuna)
  - `mini-services/polymarket-bot/api/server.py` (new endpoints)
  - `src/components/BacktestLabView.tsx` (extend)
  - `src/components/ParameterSweepPanel.tsx` (new)
  - `src/components/StrategyComparisonPanel.tsx` (new)
  - `mini-services/polymarket-bot/tests/test_backtest_advanced.py`
    (expand from 11 → ~25 tests)
- **Dependencies:** BT-1 (realism — sweeps + comparisons must use
  the realistic engine); DP-4 (retention — the sweep needs 1-min
  aggregates for long windows).
- **Risk:** MEDIUM — Optuna adds a heavy dependency. Mitigation:
  feature-flagged `BACKTEST_OPTIMIZER_ENABLED`; the grid search
  alone is sufficient for most operators.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Operators can tune strategy parameters without manual
    one-at-a-time runs.
  - Strategy comparison surfaces which strategy is best for
    which regime.
  - Bayesian optimization finds non-obvious parameter
    combinations.
- **Tests:** +14 tests covering sweep, compare, regime filter,
  grid search, optimizer integration, UI rendering.
- **Metrics:**
  - `backtest_sweep_runs_total` counter.
  - `backtest_compare_runs_total` counter.
  - `backtest_optimizer_trials_total{method}` counter.
- **Acceptance Criteria:**
  - All 25 backtest-advanced tests pass.
  - A 5-value sweep completes in < 60 s.
  - The optimizer finds a Sharpe-improving parameter combination
    in < 50 trials on a test problem.
- **Status:** IN PROGRESS.

---

## Improvement BT-4 — Walk-Forward Enhancement

- **Problem:** `ml/validation.py` (T3, Wave 3) implements
  walk-forward cross-validation (expanding-window + rolling-
  window). `backtesting/advanced.py` (W16-4) adds walk-forward
  backtest reporting. However, (a) the windows are fixed (no
  anchored vs rolling choice); (b) no Purged-K-Fold (de Lozano
  2018) — overlapping labels leak across train/test boundaries;
  (c) no walk-forward equity curve per fold (only the aggregate
  equity curve is reported); (d) no parameter-rolling (the
  strategy parameters are fixed across folds; in real life,
  operators re-tune per fold).
- **Evidence:**
  - `tests/test_ml_validation.py` (U5, Wave 4) — 8 tests cover
    walk-forward + OOT + leakage detection.
  - `tests/test_backtest_advanced.py` (W16-4) — 11 tests cover
    walk-forward backtest.
  - No Purged-K-Fold implementation.
- **Current State:** Expanding + rolling window; no Purged-K-Fold;
  aggregate equity curve only; fixed parameters across folds.
- **Desired State:**
  1. **Purged-K-Fold**: each test fold is purged of labels whose
     holding period overlaps with the train fold (prevents
     leakage from positions that span the boundary).
  2. **Anchored vs rolling choice**: operator can pick.
  3. **Per-fold equity curve**: each fold's equity curve is
     reported separately + the aggregate.
  4. **Parameter rolling**: each fold can re-tune the strategy
     parameters (using BT-3's grid search).
- **Proposed Solution:**
  1. `PurgedKFold` class in `backtesting/walk_forward.py` (new).
  2. Extend `ml/validation.py::walk_forward_cv` with the
     `method="purged"|"expanding"|"rolling"` parameter.
  3. Per-fold equity curve in `BacktestReport`.
  4. `ParameterRoller` class — runs the grid search per fold.
- **Architecture:**
  ```
  WalkForwardCV(method="purged", n_folds=5, purge_window=300s)
    └─→ for each fold i:
         train = labels[0..i] minus labels within purge_window of test_start
         test = labels[i..i+1] minus labels within purge_window of train_end
         if method == "expanding": train = labels[0..i]
         if method == "rolling": train = labels[i-window..i]
         run(model, train, test) → fold_report
    └─→ aggregate: aggregate_equity_curve + per_fold_equity_curves
  ParameterRoller.roll(strategy, folds, param_grid, metric="sharpe")
    └─→ for each fold:
         best_params = BacktestOptimizer.grid_search(param_grid, metric, fold_data)
         run(strategy with best_params) → fold_report
  ```
- **Implementation:**
  1. New module `backtesting/walk_forward.py`.
  2. Extend `ml/validation.py`.
  3. Extend `BacktestReport` to carry per-fold equity curves.
  4. `ParameterRoller` class.
- **Files Affected:**
  - `mini-services/polymarket-bot/backtesting/walk_forward.py`
    (new)
  - `mini-services/polymarket-bot/ml/validation.py` (extend)
  - `mini-services/polymarket-bot/backtesting/report.py` (extend)
  - `mini-services/polymarket-bot/backtesting/advanced.py`
    (extend)
  - `mini-services/polymarket-bot/tests/test_ml_validation.py`
    (expand from 8 → ~14 tests)
  - `mini-services/polymarket-bot/tests/test_backtest_advanced.py`
    (expand for per-fold)
- **Dependencies:** BT-3 (parameter rolling uses the optimizer);
  BT-1 (realism).
- **Risk:** MEDIUM — Purged-K-Fold is a behavioural change (the
  validation metrics will be lower than the current leaky
  metrics). Mitigation: feature flag `PURGED_KFOLD_ENABLED`;
  operators can A/B the two metrics.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Leakage-free validation metrics (the current metrics are
    optimistically biased).
  - Per-fold equity curves surface fold-level stability.
  - Parameter rolling surfaces parameter-stability across
    regimes.
- **Tests:** +6 tests covering Purged-K-Fold, anchored vs rolling,
  per-fold equity curve, parameter rolling.
- **Metrics:**
  - `walk_forward_method_runs_total{method}` counter.
  - `walk_forward_purge_count` gauge.
  - `walk_forward_fold_sharpe_stdev` gauge (cross-fold
    stability).
- **Acceptance Criteria:**
  - All 14 ml-validation tests pass.
  - A walk-forward with `method="purged"` reports a Sharpe
    lower than `method="expanding"` on the same data (the
    leak-free number is lower).
  - Per-fold equity curves render in the report.
- **Status:** IN PROGRESS.
