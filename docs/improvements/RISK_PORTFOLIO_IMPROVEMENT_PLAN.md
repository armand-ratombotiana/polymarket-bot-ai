# Risk & Portfolio — Improvement Plan

- **Domain:** Risk engine, capital allocation, stress testing,
  Kelly criterion
- **Owning modules:** `risk/manager.py`, `risk/routes.py`,
  `core/capital_allocator.py`, `core/portfolio.py`,
  `core/portfolio_optimizer.py`, `core/portfolio_mark_to_market.py`,
  `core/correlation.py`, `core/stress_test.py`, `core/safety.py`,
  `core/live_safety_gate.py`
- **Source authority:** God Mode §52 (risk engine), §53 (capital
  allocation).
- **Priority classification (per God Mode §64):**
  - P0 — risk engine enhancements (capital protection).
  - P1 — capital allocation, stress testing.
  - P2 — Kelly criterion (currently using saturating curve).
- **Status as of W17-9:** IN PROGRESS — see per-improvement status
  below.

This plan defines every improvement in the risk/portfolio domain
using the per-improvement field set required by God Mode §63.
Each improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement RP-1 — Risk Engine Enhancements (§52)

- **Problem:** `risk/manager.py` (R3, Wave 1; refined in V3, V4,
  Wave 5) implements: kill switch, max drawdown circuit breaker,
  per-trade circuit breaker, MTM risk gate, 10-check live safety
  gate. However, several risk surfaces are missing: (a) no
  per-strategy risk budget (every strategy shares the global
  `MAX_TOTAL_EXPOSURE_USDC`); (b) no portfolio-VaR computation
  (the `core/stress_test.py` ships VaR but only on demand, not
  as a continuous gate); (c) no scenario-aware risk gate (the
  gate is static; it doesn't tighten during high-volatility
  regimes); (d) no adverse-selection risk gate (after 3
  consecutive losing trades on the same strategy, halve the
  position size).
- **Evidence:**
  - `tests/test_risk_manager.py` (S7, Wave 2) — 6 tests covering
    kill switch, daily loss, circuit breaker, MDD baseline.
  - `tests/test_live_safety_gate.py` (U4, Wave 4) — 7 tests
    covering the 10 checks.
  - `FINAL_SYSTEM_REASESSMENT.md` §6 lists "per-strategy risk
    budget" as a residual risk.
  - `core/stress_test.py` (Wave 16) — computes VaR but is not
    wired into the gate stack.
- **Current State:** Global risk budget; static gate; no VaR
  gate; no scenario awareness; no adverse-selection gate.
- **Desired State:**
  1. **Per-strategy risk budget**: each strategy gets a
     configurable fraction of `MAX_TOTAL_EXPOSURE_USDC`
     (e.g. signal_trader 60 %, market_maker 30 %, arb_scanner
     10 %).
  2. **Portfolio-VaR gate**: at every `check_order`, compute
     the 95 % VaR of the resulting portfolio; reject if VaR
     would exceed `MAX_PORTFOLIO_VAR_USDC`.
  3. **Scenario-aware tightening**: when the rolling 1-h
     volatility exceeds 2x its 7-day average, the per-strategy
     budget is halved.
  4. **Adverse-selection gate**: after 3 consecutive losing
     trades on a strategy, the strategy's position size is
     halved for 30 min.
  5. New endpoints exposing each gate's state.
- **Proposed Solution:**
  1. `StrategyRiskBudget` dataclass in `risk/manager.py`.
  2. `PortfolioVaRCalculator` (already in `core/stress_test.py`;
     wire into `check_order`).
  3. `ScenarioAwareTightener` class — subscribes to the
     observability collector's `rolling_volatility` metric.
  4. `AdverseSelectionGate` class — reads recent closed positions
     from `core/closed_positions.py`.
  5. New endpoints.
- **Architecture:**
  ```
  check_order(order)
    └─→ existing gates (kill switch, MDD, per-trade breaker, MTM)
    └─→ StrategyRiskBudget.check(order) → reject if strategy over budget
    └─→ PortfolioVaRCalculator.check(order) → reject if VaR > MAX_PORTFOLIO_VAR
    └─→ ScenarioAwareTightener.check(order) → tighten size if vol regime high
    └─→ AdverseSelectionGate.check(order) → halve size if 3 consecutive losses
  ```
- **Implementation:**
  1. Extend `risk/manager.py`.
  2. Wire `core/stress_test.py::PortfolioVaRCalculator` into the
     gate.
  3. New `ScenarioAwareTightener` class.
  4. New `AdverseSelectionGate` class.
  5. New endpoints + UI updates.
- **Files Affected:**
  - `mini-services/polymarket-bot/risk/manager.py` (extend)
  - `mini-services/polymarket-bot/risk/routes.py` (extend)
  - `mini-services/polymarket-bot/core/stress_test.py` (wire)
  - `mini-services/polymarket-bot/core/closed_positions.py`
    (extend — adverse selection read)
  - `mini-services/polymarket-bot/tests/test_risk_manager.py`
    (expand from 6 → ~22 tests)
  - `src/components/RiskStatusPanel.tsx` (extend — render new gates)
- **Dependencies:** BE-4 (circuit breakers — the adverse-
  selection gate's 3-loss counter is similar to the per-trade
  breaker); ST-2 (strategy lifecycle — the per-strategy budget
  needs the lifecycle state).
- **Risk:** HIGH — touches the central risk gate. Mitigation:
  feature-flagged; existing gates preserved as the always-on
  baseline; new gates additive.
- **Priority:** P0 (capital protection).
- **Expected Benefit:**
  - Per-strategy budget prevents one strategy from eating all
    the capital.
  - VaR gate prevents a single order from blowing up the
    portfolio risk.
  - Scenario-aware tightening reduces risk during vol spikes.
  - Adverse-selection gate reduces risk during strategy decay.
- **Tests:** +16 tests covering each new gate, the scenario-
  aware tightening, the adverse-selection path, endpoint schema,
  UI rendering.
- **Metrics:**
  - `risk_gate_check_total{gate, result}` counter.
  - `risk_gate_var_value_usd` gauge.
  - `risk_gate_tightening_active` gauge.
  - `risk_gate_adverse_selection_active{strategy}` gauge.
- **Acceptance Criteria:**
  - All 22 risk-manager tests pass.
  - An order that would push the portfolio VaR > MAX is
    rejected with a clear reason.
  - The UI shows each gate's state in `RiskStatusPanel`.
- **Status:** IN PROGRESS.

---

## Improvement RP-2 — Capital Allocation Improvements (§53)

- **Problem:** `core/capital_allocator.py` (T5, Wave 3) implements
  the saturating-edge curve with 5 multipliers (edge, win-rate,
  drawdown, correlation, liquidity). It's wired into
  `signal_trader.py` (V2, Wave 5). However, (a) the multipliers
  are global — no per-strategy tuning; (b) the allocator is
  invoked only at signal time — no rebalancing (a position that
  grew to be 50 % of the portfolio is never trimmed); (c) no
  Kelly-fractional option (the saturating curve is conservative;
  operators may want Kelly-based sizing for higher-edge
  strategies).
- **Evidence:**
  - `tests/test_capital_allocator.py` (T9, Wave 3) — 9 tests.
  - `tests/test_capital_allocator_advanced.py` (W6, Wave 6) — 8
    tests covering the 5 multipliers.
  - `core/portfolio_optimizer.py` (W16-5) — ships mean-variance
    optimizer but not wired into the allocator.
- **Current State:** Saturating curve + 5 multipliers; per-signal
  invocation; no rebalancing; no Kelly.
- **Desired State:**
  1. **Per-strategy multiplier config** — each strategy can
     override the global multipliers (e.g. market_maker uses
     higher edge multiplier, signal_trader uses higher
     drawdown multiplier).
  2. **Rebalancing**: a daily cron runs the portfolio optimizer
     (W16-5); if any position's weight differs from the
     optimizer's target by > 5 %, a trim/extend order is
     suggested (operator-confirmed).
  3. **Kelly-fractional sizing**: the allocator can be
     configured per-strategy to use Kelly (with a fractional
     f=0.25 to be safe) instead of the saturating curve.
  4. New endpoint `GET /api/capital/allocation/strategy/{id}`
     returning the per-strategy config + current sizing.
- **Proposed Solution:**
  1. Extend `core/capital_allocator.py::CapitalAllocator` with
     per-strategy config.
  2. New `Rebalancer` class in `core/portfolio.py` — uses the
     optimizer.
  3. Kelly sizing mode in the allocator.
  4. New endpoint + UI.
- **Architecture:**
  ```
  CapitalAllocator
    └─→ strategy_config: dict[strategy, StrategyConfig]
         └─→ StrategyConfig(multipliers, sizing_mode="saturating"|"kelly", kelly_f=0.25)
    └─→ allocate(strategy, edge, ...):
         if strategy_config.sizing_mode == "kelly":
            size = kelly_size(edge, win_rate, kelly_f)
         else:
            size = saturating_curve(edge, multipliers)
  Rebalancer.run(portfolio, target_weights)
    └─→ for each position:
         actual_weight = position.value / portfolio.value
         target_weight = target_weights[position.token_id]
         if abs(actual_weight - target_weight) > 0.05:
            suggest_trim_or_extend(position, target_weight)
  ```
- **Implementation:**
  1. Extend `core/capital_allocator.py`.
  2. New `Rebalancer` class in `core/portfolio.py`.
  3. Kelly sizing helper.
  4. New endpoint.
  5. UI: per-strategy config in `CapitalAllocatorPanel.tsx`.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/capital_allocator.py`
    (extend)
  - `mini-services/polymarket-bot/core/portfolio.py` (extend)
  - `mini-services/polymarket-bot/core/portfolio_optimizer.py`
    (wire)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (cron for rebalancer)
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `src/components/CapitalAllocatorPanel.tsx` (extend)
  - `mini-services/polymarket-bot/tests/test_capital_allocator.py`
    (expand from 9 → ~20 tests)
  - `mini-services/polymarket-bot/tests/test_capital_allocator_advanced.py`
    (expand from 8 → ~15 tests)
- **Dependencies:** RP-3 (stress testing — Kelly needs
  stress-tested covariance); ST-1 (unified contract — per-
  strategy config comes from `StrategySpec`).
- **Risk:** MEDIUM — Kelly sizing can be aggressive.
  Mitigation: Kelly fractional default f=0.25; saturating
  curve remains the default for new strategies.
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Per-strategy tuning surfaces strategy-specific risk
    appetites.
  - Rebalancing prevents concentration drift.
  - Kelly sizing unlocks higher-edge strategies.
- **Tests:** +18 tests covering per-strategy config,
  rebalancing, Kelly mode, endpoint schema, UI rendering.
- **Metrics:**
  - `capital_allocator_size_total{strategy, mode}` counter.
  - `capital_allocator_rebalance_suggestions_total` counter.
  - `capital_allocator_kelly_f{strategy}` gauge.
- **Acceptance Criteria:**
  - All 35 capital-allocator tests pass (20 + 15).
  - A strategy with Kelly sizing produces larger sizes for
    higher-edge signals (verified by test).
  - The rebalancer suggests at least 1 trim when a position
    exceeds 5 % weight drift.
- **Status:** IN PROGRESS.

---

## Improvement RP-3 — Stress Testing Expansion

- **Problem:** `core/stress_test.py` (Wave 16) ships 6 stress
  scenarios (2008-style crash, 2020 COVID crash, flash crash,
  rate-hike shock, liquidity crisis, regime change) + computes
  portfolio VaR/CVaR/expected-shortfall per scenario. However,
  (a) the scenario library is fixed — operators cannot add
  custom scenarios; (b) no live stress test (the scenarios run
  on historical data only, not on the current portfolio); (c)
  no stress-test cron (operators must invoke manually); (d) no
  alerting integration (a failed stress test should alert).
- **Evidence:**
  - `tests/test_stress_test.py` (Wave 16) — 8 tests covering
    the 6 scenarios + VaR/CVaR computation.
  - `src/components/PortfolioRiskPanel.tsx` (W13-6) — renders
    the stress-test results but only on demand.
  - `core/alerting.py` (W16-1) — has no `stress_test_failure`
    alert type.
- **Current State:** 6 fixed scenarios; on-demand invocation;
  no live portfolio stress; no cron; no alerting.
- **Desired State:**
  1. **Custom scenario API**: operators can define a scenario
     (market shock % per token, duration, recovery pattern)
     and run it.
  2. **Live portfolio stress**: the stress test runs on the
     current portfolio (not historical data).
  3. **Daily cron**: stress test runs every day at 06:00 UTC.
  4. **Alerting integration**: if any scenario produces a
     portfolio loss > 20 %, alert.
  5. New endpoint `GET /api/stress-test/scenarios` + `POST
     /api/stress-test/run` + `POST /api/stress-test/scenarios`
     (custom).
- **Proposed Solution:**
  1. `ScenarioLibrary` class — manages built-in + custom
     scenarios.
  2. `LiveStressTest` class — applies scenario shocks to the
     current portfolio.
  3. Cron wiring in `training_orchestrator`.
  4. Alert integration.
  5. Endpoints + UI panel.
- **Architecture:**
  ```
  ScenarioLibrary
    └─→ built_in: [GFC_2008, COVID_2020, FLASH_CRASH, ...]
    └─→ custom: loaded from scenarios/ directory or DB
  LiveStressTest.run(scenario, portfolio)
    └─→ for each position in portfolio:
         shocked_price = position.price * scenario.shock(position)
         pnl_impact = (shocked_price - position.entry) * position.size
    └─→ aggregate: portfolio_pnl_impact, var, cvar
    └─→ if abs(portfolio_pnl_impact) > 0.20 * portfolio.value:
         alerting.alert("stress_test_failure", severity="WARN")
  cron 06:00 UTC
    └─→ for each scenario in library:
         LiveStressTest.run(scenario, current_portfolio)
  ```
- **Implementation:**
  1. Extend `core/stress_test.py`.
  2. New `ScenarioLibrary` class.
  3. New `LiveStressTest` class.
  4. Cron wiring.
  5. Alert integration.
  6. Endpoints + UI panel.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/stress_test.py` (extend)
  - `mini-services/polymarket-bot/core/alerting.py` (extend)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (cron)
  - `mini-services/polymarket-bot/api/server.py` (new endpoints)
  - `mini-services/polymarket-bot/scenarios/*.yaml` (new dir
    for custom scenarios)
  - `src/components/PortfolioRiskPanel.tsx` (extend — render
    live + custom scenarios)
  - `mini-services/polymarket-bot/tests/test_stress_test.py`
    (expand from 8 → ~20 tests)
- **Dependencies:** DP-1 (Postgres — VaR queries faster); RP-1
  (risk engine — VaR gate uses the stress-test calculator).
- **Risk:** LOW — additive; existing 6 scenarios preserved.
- **Priority:** P1 (capital protection — stress test catches
  tail risks before they happen).
- **Expected Benefit:**
  - Operators can simulate "what happens to my portfolio if
    token X drops 30 %?" in one click.
  - Daily cron catches new tail risks automatically.
  - Custom scenarios support operator-specific concerns (e.g.
    political-event shock).
- **Tests:** +12 tests covering custom scenarios, live stress
  test, cron, alerting, endpoint schema, UI rendering.
- **Metrics:**
  - `stress_test_runs_total{scenario, result}` counter.
  - `stress_test_portfolio_loss_pct{scenario}` gauge.
  - `stress_test_alerts_total` counter.
- **Acceptance Criteria:**
  - All 20 stress-test tests pass.
  - A custom scenario defined in YAML runs end-to-end.
  - The daily cron produces a report in
    `data/reports/stress_test_<date>.json`.
- **Status:** IN PROGRESS.

---

## Improvement RP-4 — Kelly Criterion Optimization

- **Problem:** `core/capital_allocator.py` uses a saturating edge
  curve (`size = base * (1 - exp(-k * edge))`) instead of Kelly
  sizing. The saturating curve is conservative (never allocates
  more than `MAX_POSITION_PER_MARKET_USDC` regardless of edge).
  Kelly sizing (`size = (p * b - (1-p)) / b` where b is the
  odds) can allocate more aggressively when the edge is high.
  However, full Kelly is dangerous (small estimation errors
  blow up the bankroll); fractional Kelly (f=0.25 or f=0.5)
  is the safe alternative.
- **Evidence:**
  - `core/capital_allocator.py` — `saturating_curve(edge, k=...)`
    is the only sizing function.
  - RP-2 (capital allocation improvements) introduces Kelly as
    a per-strategy option, but the underlying Kelly calculation
    does not exist.
  - No `tests/test_kelly.py` exists.
- **Current State:** Saturating curve only; no Kelly option.
- **Desired State:**
  1. **Kelly sizing function**: `kelly_size(p_win, b, f=0.25)`
     returns the fractional-Kelly position size.
  2. **Confidence-bounded Kelly**: uses the model's confidence
     (not just p_win) to shrink the Kelly estimate when the
     model is uncertain.
  3. **Drawdown-aware Kelly**: reduces f during drawdowns (e.g.
     f = 0.25 * (1 - drawdown / MAX_DRAWDOWN)).
  4. **Comparison report**: a backtest comparing saturating
     vs Kelly sizing over the last 90 days, showing P&L,
     Sharpe, max drawdown for each.
- **Proposed Solution:**
  1. `kelly_size(p_win, b, f)` function in a new
     `core/kelly.py`.
  2. `ConfidenceBoundedKelly` class.
  3. `DrawdownAwareKelly` class.
  4. Backtest report comparing the two sizing functions.
- **Architecture:**
  ```
  kelly_size(p_win=0.55, b=1.0, f=0.25)
    └─→ full_kelly = (p_win * b - (1 - p_win)) / b
    └─→ return f * full_kelly * bankroll
  ConfidenceBoundedKelly.size(p_win, confidence, b, f)
    └─→ shrunk_p_win = p_win * confidence + 0.5 * (1 - confidence)
    └─→ kelly_size(shrunk_p_win, b, f)
  DrawdownAwareKelly.size(p_win, b, f, drawdown, max_dd)
    └─→ effective_f = f * (1 - drawdown / max_dd)
    └─→ kelly_size(p_win, b, effective_f)
  Backtest comparison (90-day window)
    └─→ run with saturating: report P&L, Sharpe, MDD
    └─→ run with Kelly: report P&L, Sharpe, MDD
    └─→ comparison_report
  ```
- **Implementation:**
  1. New module `core/kelly.py`.
  2. Wire into `core/capital_allocator.py` (the per-strategy
     Kelly mode added by RP-2).
  3. Backtest comparison script.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/kelly.py` (new)
  - `mini-services/polymarket-bot/core/capital_allocator.py`
    (extend — Kelly mode uses `kelly.size()`)
  - `mini-services/polymarket-bot/tests/test_kelly.py` (new)
  - `mini-services/polymarket-bot/scripts/compare_kelly_vs_saturating.py`
    (new — backtest comparison)
  - `docs/KELLY_VS_SATURATING.md` (new — comparison report
    documentation)
- **Dependencies:** RP-2 (capital allocation — Kelly mode is
  one of the per-strategy sizing options); RP-3 (stress testing
  — Kelly sizing needs stress-tested covariance to be safe).
- **Risk:** HIGH — Kelly sizing can blow up the bankroll on
  estimation errors. Mitigation: fractional f=0.25 default;
  confidence-bounded variant; drawdown-aware variant.
- **Priority:** P2 (optimization).
- **Expected Benefit:**
  - Higher-edge strategies can size up safely (fractional +
    drawdown-aware).
  - Comparison report gives operators evidence to choose
    between the two sizing functions.
- **Tests:** +12 tests covering Kelly formula, confidence
  bounding, drawdown awareness, comparison report.
- **Metrics:**
  - `kelly_sizing_total{variant}` counter.
  - `kelly_effective_f` gauge.
- **Acceptance Criteria:**
  - All 12 Kelly tests pass.
  - The comparison report shows Kelly with f=0.25 has a higher
    Sharpe than saturating on a high-edge strategy (verified by
    backtest on the last 90 days).
  - Kelly with f=0.25 has a max drawdown <= saturating's max
    drawdown on the same window.
- **Status:** TODO (not started — gated on RP-2 + RP-3).
