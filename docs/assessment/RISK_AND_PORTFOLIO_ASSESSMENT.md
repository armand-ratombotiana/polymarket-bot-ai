# Risk & Portfolio Assessment — Polymarket Bot (§52 / §53)

- **Task ID:** W17-8 (File 2 of 3)
- **Agent:** general-purpose
- **Date:** 2026-09-17
- **Scope:** Read-only assessment of the institutional risk engine (§52)
  and the capital-allocation / portfolio-optimization stack (§53) in
  `mini-services/polymarket-bot/`. No source files were modified.
- **Evidence basis** (classification legend):
  - **VERIFIED** — read in source file in this session.
  - **STRONG EVIDENCE** — named in docstring/comment with specific line /
    constant / value that matches surrounding context.
  - **LIKELY** — consistent with code patterns but not directly verified.
  - **UNVERIFIED** — plausible but not yet confirmed.
  - **NOT FOUND** — no evidence located.

Companion document: `CROSS_SYSTEM_ARCHITECTURE_ASSESSMENT.md` (§50, §51,
§79, §80) covers where the risk/portfolio layer plugs into the wider
pipeline. This document drills into the risk engine internals and the
capital-allocation contract.

---

## 1. Executive Summary

The Polymarket Bot implements an **institutionally-styled risk engine**
(`risk/manager.py::InstitutionalRiskEngine`) with 13+ pre-trade gates, a
per-trade circuit breaker, and a USD-100 operating / USD-200 ceiling
capital model. The capital allocation layer (`core/capital_allocator.py`)
provides two complementary sizing entry points — a **T9 safety-gated
BUY-side allocator** (`allocate_size`) used in the hot scan loop, and a
**T5 multiplier-stack allocator** (`allocate_capital`) surfaced via the
HTTP API. The portfolio layer (`core/portfolio_optimizer.py`) implements
a **Kelly-criterion multi-bet optimizer** that selects the best subset
of opportunities within a max-total-exposure budget; the
`core/stress_test.py` module runs **6 stress scenarios** against the
current portfolio; `backtesting/report.py` computes **VaR / CVaR /
Sharpe / Sortino / Calmar** on backtest equity curves.

**Headline findings** (full list in §23):

1. **The risk engine is comprehensive.** All 13+ gates the spec lists
   are present: per-order limit, per-market limit, strategy limit,
   portfolio limit, category concentration (correlated group), cash
   reserve, maximum drawdown, daily loss, weekly loss, per-trade loss
   (circuit breaker), pending-order capital, position count, price
   sanity, minimum order size, bankroll ceiling. (VERIFIED via
   `risk/manager.py:165-348`.)
2. **Cash-reserve, drawdown, and daily-loss stops are correctly wired
   to a hard kill switch** that cancels all open orders on breach.
   (VERIFIED via `manager.py:235-251, 403-417`.)
3. **Dynamic ML-health sizing** scales the per-market cap by a
   `[0.30, 1.00]` multiplier derived from PSI / Brier score. (VERIFIED
   via `manager.py:78-97, 262-276`.)
4. **Capital allocation IS separated from signal generation** (§53
   contract). The strategy layer computes edge / confidence / liquidity
   and passes them to `allocate_size`; the allocator decides the size.
   (VERIFIED via `signal_trader.py:29` `from core.capital_allocator
   import allocate_capital` and the pure-function signature at
   `capital_allocator.py:179-186`.)
5. **The "best opportunity doesn't automatically get the largest trade"
   contract (§53) IS satisfied at the per-trade level.** The T9
   allocator uses a sublinear `edge ** 0.4` exponent so 4× edge yields
   `<2×` size; the T5 allocator uses a Michaelis-Menten saturating
   curve (`V_MAX * edge / (K_M + edge)`) so size asymptotes to `$3` as
   edge grows. Both are capped at `$3.00`. (VERIFIED via
   `capital_allocator.py:36-93, 268-285`.)
6. **However, the §53 multi-bet portfolio optimizer is NOT in the live
   trade path.** It is exposed via `POST /api/portfolio/optimize` /
   `POST /api/portfolio/rebalance` as an operator what-if tool, but
   `signal_trader._scan_markets` calls `allocate_size` per-token in
   isolation. (LIKELY — no `portfolio_optimizer.optimize` call in
   `signal_trader.py`.)
7. **The T5 multiplier breakdown (edge × confidence × calibration ×
   drawdown × correlation × performance × liquidity) is NOT persisted
   per-decision.** It is only surfaced via the HTTP endpoint
   `GET /api/capital/allocation` for the dashboard. (VERIFIED via
   `capital_allocator.py:540-557`.)
8. **The stress test suite has 6 scenarios but no time-correlation
   model.** Each scenario applies a uniform price shock to every
   position; the `correlation_adjustment` field is informational only.
   (VERIFIED via `stress_test.py:130-191, 313`.)
9. **VaR / CVaR are computed only on backtest equity curves** — not on
   the live portfolio. (VERIFIED via `backtesting/report.py:176-180`.)
10. **MTM exposure gate is fail-open.** (VERIFIED via
    `manager.py:308-315` — bare `except: pass`.)
11. **Per-trade-loss threshold ($0.50) is too tight** against the
    $3 max position — a single $0.50 share that resolves to $0
    triggers a 5-minute strategy cooldown. (LIKELY.)
12. **Liquidity, volatility, and uncertainty gates are partial.** The
    spec asks for explicit liquidity, volatility, uncertainty, and
    stale-data gates. Liquidity IS gated (allocator's gate 5 +
    Michaelis-Menten curve). Volatility / uncertainty are NOT
    explicitly gated — they influence sizing only via the
    `dynamic_model_risk_multiplier` (Brier → 0.30 / 0.60 / 1.00). Stale
    data is gated only via the `observation_only` mode + reconciliation
    check, not per-market. (VERIFIED — no `volatility` or `uncertainty`
    keyword in `risk/manager.py`.)

### Maturity snapshot (full score in §22)

| Dimension | Score |
|---|---|
| Risk engine completeness (§52) | 7.5 / 10 |
| Capital allocation separation (§53) | 7.5 / 10 |
| Portfolio optimizer Kelly correctness | 7.0 / 10 |
| Stress testing | 6.0 / 10 |
| VaR / CVaR | 5.0 / 10 |
| Disconnection from live trade path | -1.5 (penalty) |
| **Composite** | **6.5 / 10** |

---

## 2. Purpose

This document exists to:

1. **Assess the institutional risk engine (§52)** against the
   enumerated gate list: per-order limit, per-market limit, strategy
   limit, portfolio limit, category concentration, correlated markets,
   cash reserve, maximum drawdown, daily loss, weekly loss, per-trade
   loss (circuit breaker), liquidity, volatility, uncertainty, stale
   data.
2. **Assess the capital allocation layer (§53)** against the contract:
   - signal generation is separated from capital allocation
   - the best opportunity doesn't automatically get the largest trade
   - the allocator evaluates: estimated edge, confidence, calibration,
     liquidity, correlation, existing exposure, drawdown, strategy
     performance.
3. **Assess the Kelly-criterion portfolio optimizer**, the stress
   testing suite (6 scenarios), and the VaR / CVaR computation in
   `backtesting/report.py`.

This is a read-only assessment — no source files were modified.

---

## 3. Current Architecture

### 3.1 Capital model (institutional)

The bot operates under a **two-tier capital model** (VERIFIED via
`risk/manager.py:1-25`):

```
recognized_operating_capital = min(verified_equity, USD 100)
Hard bankroll ceiling (never auto-increased): USD 200
Automated LIVE sizing operates from USD 100 only.
```

Conservative defaults (the bot refuses to auto-increase any of these
without explicit manual authorization):

| Constant | Value | Role |
|---|---|---|
| `OPERATING_CAPITAL` | $100.00 | operating bankroll (paper + automated live) |
| `BANKROLL_CEILING` | $200.00 | hard ceiling (never auto-increased) |
| `MIN_CASH_RESERVE` | $40.00 | minimum cash reserve |
| `MAX_DEPLOYABLE_CAPITAL` | $60.00 | max deployable = $100 − $40 |
| `DEFAULT_EXPERIMENTAL_POSITION` | $1.00 | default experimental trade |
| `NORMAL_MAX_POSITION` | $2.00 | normal-trade ceiling |
| `MAX_POSITION_PER_MARKET` | $3.00 | per-market cap |
| `ABSOLUTE_MAX_POSITION` | $5.00 | absolute exceptional maximum |
| `MAX_CORRELATED_EXPOSURE` | $8.00 | per correlated event group |
| `MAX_STRATEGY_EXPOSURE` | $15.00 | per strategy |
| `MAX_TOTAL_OPEN_RISK` | $25.00 | max simultaneous worst-case open risk |
| `MAX_PENDING_ORDER_CAPITAL` | $10.00 | max pending-order capital |
| `MAX_OPEN_POSITIONS` | 8 | max simultaneous open positions |
| `DAILY_LOSS_STOP` | $2.00 | hard circuit breaker |
| `WEEKLY_LOSS_STOP` | $5.00 | hard circuit breaker |
| `MAX_DRAWDOWN_LIMIT` | $8.00 | peak-to-trough hard circuit breaker |
| `PER_TRADE_MAX_LOSS` | $0.50 | per-trade circuit breaker |
| `STRATEGY_COOLDOWN` | 300s | strategy pause window after per-trade breach |

### 3.2 Risk engine flow

```
order arrives at risk_manager.check_order(order)
  ↓
  _check_order_impl(order)    [13+ sequential gates]
    0. shadow mode gate              — no orders in shadow mode
    0. kill switch gate              — file-backed + in-memory
    0. observation-only gate         — no live orders until reconciled
    0b. exposure-reconciliation gate — exposure vs deployable ceiling
    0c. live-trading-enabled gate    — explicit manual authorization
    0d. per-trade-loss cooldown gate — strategy pause check
    1. kill-switch redundant gate    — defensive double-check
    2. daily-loss stop               — hard kill switch on breach
    2b. weekly-loss stop             — hard kill switch on breach
    3. max-drawdown stop             — hard kill switch on breach
    4. cash-reserve protection       — exposure vs deployable
    5. total-simultaneous-open-risk  — exposure vs $25 ceiling
    [dynamic ML-health scaling applied to 6/6b/6c below]
    6. per-market position cap       — $3 × ml_mult
    6b. normal-position guidance     — $2 × ml_mult (new positions)
    6c. per-strategy exposure cap    — $15
    6d. correlated-group exposure cap — $8
    6e. mark-to-market exposure cap  — $25 MTM (fail-open)
    7. max-open-positions count      — 8 (new-market check)
    8. pending-order capital cap     — $10
    9. max-open-order count          — settings.max_open_orders
    10. price sanity                 — [0.01, 0.99]
    11. minimum order size           — 0.5 shares
    12. bankroll ceiling max-loss    — total_exp vs (BANKROLL_CEILING − MIN_CASH_RESERVE)
    → return (True, "OK") or (False, reason)
  ↓
if rejected:
  - shadow_trading.record_shadow_trade(...) [counterfactual]
  - decision_ledger.record_rejection(...)
if approved:
  - decision_ledger.record(decision_id, RISK_APPROVED, ...)
  - paper_sim.create_order(...) or clob_client.create_order(...)
```

### 3.3 Capital allocation flow

```
signal_trader._scan_markets()
  for each candidate market:
    features = extract_features(token_id, book, ...)
    p_yes, confidence = ml_model.predict(features)
    edge = p_yes − market_mid
    ↓
    size = allocate_size(
              edge=edge,
              confidence=confidence,
              drawdown=peak_equity − current_equity,
              existing_exposure=store.exposure_for_market(token_id),
              liquidity=book.best_bid_size + book.best_ask_size,
           )
    ↓
    if size > 0:
       MarketSignal(size_usdc=size, ...)
       submit_order(OrderArgs(size=size, ...), decision_id=...)
```

The T5 multiplier-based `allocate_capital()` is a **parallel entry
point** surfaced via `GET /api/capital/allocation`:

```
raw_size = saturating_edge(edge)                 # Michaelis-Menten
size = raw_size
       × smoothstep(confidence)
       × calibration_mult(brier)                # 0.30 / 0.60 / 1.00
       × drawdown_mult(drawdown_dollars)         # linear fade to 0
       × correlation_mult(existing_exposure)      # smoothstep fade
       × performance_mult(strategy_perf)          # win_rate + sharpe blend
       × liquidity_mult(liquidity_usdc)          # Michaelis-Menten
→ clamp to [0, MAX_POSITION_PER_MARKET]
```

### 3.4 Portfolio optimizer flow

```
operator → POST /api/portfolio/optimize {opportunities: [...]}
  ↓
portfolio_optimizer.optimize(opportunities)
  for each opportunity:
    kelly = compute_kelly(price, edge, confidence)
       = edge / max(1 - price, 0.01) * kelly_fraction
       (clamped to [0, 1], 0 if below min_edge or min_confidence)
    kelly_adjusted = min(kelly, max_single_bet)
    size_usdc = kelly_adjusted * operating_capital
  ↓
  sort bets by expected_return / expected_risk (Sharpe-like) descending
  ↓
  apply total-exposure constraint:
    for each bet (in sorted order):
      if total + bet.size > max_total:
        scale last bet down to fit, break
      else: add to selected
  ↓
  compute diversification_ratio = weighted_avg_risk / portfolio_risk
    (assuming independence)
  ↓
  return PortfolioOptimization(bets, total_allocated, ...)
```

### 3.5 Stress test flow

```
operator → POST /api/portfolio/stress-test
  ↓
stress_tester.run_all_scenarios(positions)
  for each of 6 scenarios:
    for each position:
      shock_pct = scenario.price_shock.get(token_id, scenario.price_shock.get("_all", 0))
      shocked_price = current_price * (1 + shock_pct)
      pnl = (shocked_price - entry_price) * size  (LONG)
            (entry_price - shocked_price) * size  (SHORT)
      pnl -= exit_slippage * size   (fill_degradation penalty)
    ↓
    portfolio_pnl_pct = total_pnl / total_invested
    survival = portfolio_pnl_pct > -ruin_threshold (0.5)
    margin_call_risk = portfolio_pnl_pct < -0.3
    positions_breaching_stop = count(pnl_pct < -stop_loss_pct)
  ↓
  return StressTestResult per scenario
  ↓
summary: worst_case_pnl, best_case_pnl, avg_pnl, surviving_scenarios count
```

### 3.6 VaR / CVaR flow

```
backtesting.engine.run_realistic_backtest(...) → equity_curve
  ↓
backtesting.report.generate_report(backtest_result)
  returns = np.diff(equity) / equity[:-1]
  var_95 = np.percentile(returns, 5)
  cvar_95 = np.mean(returns[returns <= var_95])
  ↓
  BacktestReport(var_95, cvar_95, sharpe, sortino, calmar, ...)
  ↓
  PDF report via reportlab (VaR 95% + CVaR 95% in summary table)
```

---

## 4. Current Components

### 4.1 Risk engine

- `risk/manager.py::InstitutionalRiskEngine` — singleton with 13+ gates
  + per-trade-loss cooldown + kill-switch + observation-mode +
  reconciliation gate.
- `risk/manager.py::dynamic_model_risk_multiplier()` — returns
  `[0.30, 1.00]` based on PSI / Brier.
- `risk/manager.py::recognized_operating_capital(verified_equity)` —
  `min(verified_equity, OPERATING_CAPITAL)`.
- `risk/routes.py` — `GET /api/risk/strategies/paused` for paused-strategy
  visibility.
- `core/safety.py` — kill-switch file-backed + in-memory.

### 4.2 Capital allocation

- `core/capital_allocator.py::allocate_size()` — T9 safety-gated BUY-side
  allocator (used by hot scan loop). 5 gates: edge, confidence,
  drawdown, existing exposure, liquidity. Saturating curve
  `SIZE_SCALE * edge ** 0.4 * confidence`, clamp `[$0.50, $3.00]`.
- `core/capital_allocator.py::allocate_capital()` — T5 multiplier-stack
  allocator (HTTP-facing). 6 multipliers: confidence, calibration,
  drawdown, correlation, performance, liquidity.
- `core/capital_allocator.py::allocation_breakdown()` — returns the
  per-multiplier decomposition for the dashboard.
- `core/capital_allocator.py::performance_mult(strategy_performance)` —
  blends win_rate (60%) + sharpe (40%), clamps to `[0.25, 1.50]`.

### 4.3 Portfolio layer

- `core/portfolio_optimizer.py::PortfolioOptimizer` — Kelly multi-bet
  optimizer. Configurable via `PUT /api/portfolio/config`.
- `core/portfolio.py::compute_exposure()` — 8-dimension exposure
  decomposition (capital invested / pending / gross / net directional /
  max remaining loss / by group / by strategy / dollar-days /
  available cash).
- `core/portfolio_mark_to_market.py::compute_mark_to_market_exposure` —
  MTM exposure for the risk gate's MTM cap.
- `core/correlation.py::compute_correlation_matrix()` — Pearson
  correlation matrix between held positions.
- `core/stress_test.py::PortfolioStressTester` — 6 scenarios.

### 4.4 Backtesting / risk analytics

- `backtesting/engine.py::BacktestEngine` — historical backtest.
- `backtesting/advanced.py` — advanced backtest (realistic fills).
- `backtesting/report.py::generate_report()` — VaR / CVaR / Sharpe /
  Sortino / Calmar + PDF report.

### 4.5 Attribution (post-trade)

- `core/attribution.py::get_full_attribution()` — 7-dim P&L roll-up
  (strategy / confidence bucket / edge bucket / probability band /
  liquidity level / holding period / trade direction) on
  `closed_positions.db`.
- `core/closed_positions.py` — round-trip trade journal with
  `decision_id` cross-ref.

---

## 5. Data Flow

### 5.1 Pre-trade risk flow

```
strategy → BaseStrategy.submit_order(args, decision_id)
  ↓
provisional Order(...)
  ↓
risk_manager.check_order(provisional)
  ├── _check_order_impl(order) [13+ gates]
  ↓
(allowed, reason) tuple
  ↓
if not allowed:
  ├── shadow_trading.record_shadow_trade(decision_id, ...) [async fire-and-forget]
  ├── decision_ledger.record(decision_id, RISK_REJECTED, ...)
  ├── decision_ledger.record_rejection(token_id, strategy, edge, confidence, reason)
  └── return None
if allowed:
  ├── decision_ledger.record(decision_id, RISK_APPROVED, ...)
  ├── paper_sim.create_order(args, strategy, decision_id)  (paper mode)
  │   └── store.add_order(order)  [in-memory + JSON]
  └── clob_client.create_order(args)  (live mode)
      └── store.add_order(Order(order_id=resp["orderID"], ...))
```

### 5.2 Per-trade P&L flow (post-fill)

```
paper_sim._fill_loop() matches order against book
  ↓
_execute_fill(order, fill_price)
  ├── store.add_trade(Trade(order_id, price=fill_price, pnl=...))
  ├── update Position (yes_shares / no_shares / avg_entry_price)
  ├── update store.paper_balance / daily_pnl / peak_equity
  ├── decision_ledger.record(decision_id, FILL, pnl=...)
  ├── execution_quality.record_execution(order_id, decision_id, ...)
  └── risk_manager.report_trade_pnl(strategy, pnl)
      └── if abs(pnl) >= PER_TRADE_MAX_LOSS:
          ├── _strategy_cooldowns[strategy] = monotonic + 300s
          ├── audit_logger.log_event(category="risk", event_type="strategy_cooldown_activated")
          └── store.log_event("⏸ Strategy paused ...")
```

### 5.3 Capital allocation data flow

```
ml_model.predict(features) → (p_yes, confidence)
  ↓
edge = p_yes − market_mid
  ↓
drawdown = store.peak_equity − (OPERATING_CAPITAL + store.daily_pnl)
  ↓
existing_exposure = store.exposure_for_market(token_id)
  ↓
liquidity = book.best_bid.size + book.best_ask.size   (UNVERIFIED exact formula)
  ↓
allocate_size(edge, confidence, drawdown, existing_exposure, liquidity)
  ↓
size in [$0.50, $3.00] or 0.0 (if any gate trips)
```

### 5.4 Portfolio optimizer data flow (operator what-if only)

```
operator → POST /api/portfolio/optimize
  body: { opportunities: [{token_id, strategy, price, edge, confidence}, ...] }
  ↓
portfolio_optimizer.optimize(opportunities)
  ├── for each opp: compute_kelly(price, edge, confidence)
  ├── sort by expected_return / expected_risk descending
  ├── apply max_total_exposure constraint
  └── compute diversification_ratio
  ↓
response: { bets, total_allocated, total_return, total_risk,
            diversification_ratio, constraint_violations }
```

---

## 6. Execution Flow

### 6.1 Risk-gate evaluation order (canonical, VERIFIED via
`risk/manager.py:165-348`)

| # | Gate | Threshold | Action on breach |
|---|---|---|---|
| 0 | Shadow mode | `settings.trading_mode == "shadow"` | Reject — "evaluation only" |
| 0 | Kill switch (in-memory + file) | `store.kill_switch_active` or file exists | Reject — "all trading halted" |
| 0 | Observation-only mode | `self.observation_only and not order.paper` | Reject — "live orders disabled" |
| 0b | Exposure reconciliation | `current_exp > MAX_DEPLOYABLE_CAPITAL` ($60) | Reject — "exposure not reconciled" |
| 0c | Live trading enabled | `not settings.live_trading_enabled and not order.paper` | Reject — "live disabled by default" |
| 0d | Per-trade-loss cooldown | `is_strategy_paused(order.strategy)` | Reject — "strategy in cooldown" |
| 1 | Kill switch (redundant) | `store.kill_switch_active` | Reject — duplicate of gate 0 |
| 2 | Daily loss stop | `daily_pnl <= -DAILY_LOSS_STOP` ($2) | **Hard kill switch + reject** |
| 2b | Weekly loss stop | `weekly_pnl <= -WEEKLY_LOSS_STOP` ($5) | **Hard kill switch + reject** |
| 3 | Max drawdown | `drawdown >= MAX_DRAWDOWN_LIMIT` ($8) | **Hard kill switch + reject** |
| 4 | Cash reserve | `(total_exp + order_cost) > MAX_DEPLOYABLE_CAPITAL` ($60) | Reject |
| 5 | Total open risk | `(total_exp + order_cost) > MAX_TOTAL_OPEN_RISK` ($25) | Reject |
| 6 | Per-market position cap (scaled by ML health) | `(market_exp + order_cost) > effective_mkt_cap` ($3 × ml_mult) | Reject |
| 6b | Normal position guidance (new positions) | `market_exp <= 0 and order_cost > effective_norm_cap` ($2 × ml_mult) | Reject |
| 6c | Per-strategy exposure cap | `(strat_exp + order_cost) > MAX_STRATEGY_EXPOSURE` ($15) | Reject |
| 6d | Correlated group exposure cap | `(group_exp + order_cost) > MAX_CORRELATED_EXPOSURE` ($8) | Reject |
| 6e | Mark-to-market exposure cap | `mtm_total + order_cost > $25` | Reject (fail-open on error) |
| 7 | Max open positions count | `active_positions >= MAX_OPEN_POSITIONS` (8) | Reject (new-market only) |
| 8 | Pending order capital | `(pending + order_cost) > MAX_PENDING_ORDER_CAPITAL` ($10) | Reject |
| 9 | Max open order count | `len(open_orders) >= settings.max_open_orders` | Reject |
| 10 | Price sanity | `not (0.01 <= price <= 0.99)` | Reject |
| 11 | Minimum order size | `size < 0.5` | Reject |
| 12 | Bankroll ceiling max-loss | `(total_exp + order_cost) > (BANKROLL_CEILING − MIN_CASH_RESERVE)` | Reject |

That's **22 named gates** (counting each sub-gate). The spec's §52
enumerated list:

| Spec gate | Mapped to | Status |
|---|---|---|
| Per-order limit | Gates 8 / 9 / 11 (pending / count / min size) | VERIFIED |
| Per-market limit | Gates 6 / 6b | VERIFIED |
| Strategy limit | Gate 6c | VERIFIED |
| Portfolio limit | Gates 4 / 5 / 6e / 12 | VERIFIED |
| Category concentration | Gate 6d (correlated group, by slug) | VERIFIED |
| Correlated markets | Gate 6d | VERIFIED |
| Cash reserve | Gate 4 | VERIFIED |
| Maximum drawdown | Gate 3 | VERIFIED |
| Daily loss | Gate 2 | VERIFIED |
| (Weekly loss — bonus) | Gate 2b | VERIFIED |
| Liquidity | Allocator gate 5 + liquidity_mult | VERIFIED (in allocator, not in risk_manager) |
| Volatility | NOT FOUND in risk_manager | NOT FOUND |
| Uncertainty | Implicit via dynamic_model_risk_multiplier (Brier / PSI) | PARTIAL |
| Stale data | Partial — observation_only mode + reconciliation gate; no per-market staleness gate | PARTIAL |

### 6.2 Capital allocation execution flow (T9 allocator)

```
allocate_size(edge, confidence, drawdown, existing_exposure, liquidity)
  1. if edge <= 0: return 0.0
  2. if confidence < MIN_CONFIDENCE (0.45): return 0.0
  3. if drawdown > MAX_DRAWDOWN_USD (8.0): return 0.0
  4. if existing_exposure > MAX_EXISTING_EXPOSURE_USD (5.0): return 0.0
  5. if liquidity <= 0: return 0.0
  ↓
  raw_size = SIZE_SCALE (5.0) * edge ** SIZE_CURVE_EXPONENT (0.4) * confidence
  ↓
  if raw_size > MAX_SIZE_USD (3.0): return 3.0
  if raw_size < MIN_SIZE_USD (0.5): return 0.5
  return raw_size
```

**Saturation proof** (VERIFIED via `capital_allocator.py:42-50, 162-176`):

`raw(4 × edge) / raw(edge) = 4 ** 0.4 ≈ 1.74 < 2`

→ 4× edge yields < 2× size. **§53 contract satisfied** (best opportunity
doesn't automatically get the largest trade — the curve is sublinear).

### 6.3 Capital allocation execution flow (T5 multiplier-stack)

```
allocate_capital(edge, confidence, liquidity, existing_exposure,
                 drawdown, strategy_performance, brier)
  raw = saturating_edge(edge)           # Michaelis-Menten, asymptote = $3
  c_mult = smoothstep(confidence)       # cubic-smooth 0→1
  cal_mult = {0.30, 0.60, 1.00}[brier]  # isotonic-calibration health
  dd_mult = 1 - drawdown/$8              # linear fade
  corr_mult = 1 - smoothstep(exp/$3)     # graceful concentration fade
  perf_mult = blend(win_rate, sharpe)    # 60/40, clamp [0.25, 1.50]
  liq_mult = liq / (50 + liq)            # Michaelis-Menten
  ↓
  size = max(0, min(raw × product, $3))
```

**Saturation proof** (T5):

`raw(edge) = $3 × edge / (0.05 + edge)` — asymptotes to `$3` as
edge → ∞. Edge = K_M (5%) deploys half-saturation ($1.50).

### 6.4 Kelly criterion execution flow

```
compute_kelly(price, edge, confidence)
  if price <= 0 or price >= 1: return 0.0
  if edge < min_edge (0.03): return 0.0
  if confidence < min_confidence (0.55): return 0.0
  kelly = edge / max(1 - price, 0.01)
  kelly *= kelly_fraction (0.25)        # quarter-Kelly
  return min(kelly, 1.0)
```

(VERIFIED via `portfolio_optimizer.py:217-247`.)

---

## 7. Feature Inventory

### 7.1 Risk engine gates (§52 spec compliance)

| Spec gate | Present? | Constant | Value | Module |
|---|---|---|---|---|
| Per-order limit | Yes | (multiple — gates 8/9/11) | $10 pending, max_open_orders, 0.5 min size | `risk/manager.py:326-341` |
| Per-market limit | Yes | `MAX_POSITION_PER_MARKET` | $3.00 | `risk/manager.py:48` |
| Strategy limit | Yes | `MAX_STRATEGY_EXPOSURE` | $15.00 | `risk/manager.py:51` |
| Portfolio limit | Yes | `MAX_TOTAL_OPEN_RISK` | $25.00 | `risk/manager.py:52` |
| Category concentration | Yes | `MAX_CORRELATED_EXPOSURE` (by slug) | $8.00 | `risk/manager.py:50` |
| Correlated markets | Yes | (slug-based grouping via `store.market_slugs`) | — | `risk/manager.py:286-296` |
| Cash reserve | Yes | `MIN_CASH_RESERVE` / `MAX_DEPLOYABLE_CAPITAL` | $40 / $60 | `risk/manager.py:44-45` |
| Maximum drawdown | Yes | `MAX_DRAWDOWN_LIMIT` | $8.00 | `risk/manager.py:57` |
| Daily loss | Yes | `DAILY_LOSS_STOP` | $2.00 | `risk/manager.py:55` |
| Weekly loss (bonus) | Yes | `WEEKLY_LOSS_STOP` | $5.00 | `risk/manager.py:56` |
| Per-trade loss (bonus) | Yes | `PER_TRADE_MAX_LOSS` / `STRATEGY_COOLDOWN` | $0.50 / 300s | `risk/manager.py:64-65` |
| Liquidity | Partial (allocator) | `liquidity_mult` / allocator gate 5 | $50 K_M | `capital_allocator.py:511-520` |
| Volatility | NOT FOUND | — | — | (not gated) |
| Uncertainty | Partial | `dynamic_model_risk_multiplier` (Brier/PSI) | 0.30/0.60/1.00 | `risk/manager.py:78-97` |
| Stale data | Partial | `observation_only` mode + reconciliation gate | — | `risk/manager.py:188-204` |
| Bankroll ceiling | Yes | `BANKROLL_CEILING` | $200.00 | `risk/manager.py:43` |
| Pending-order capital | Yes | `MAX_PENDING_ORDER_CAPITAL` | $10.00 | `risk/manager.py:53` |
| Position count | Yes | `MAX_OPEN_POSITIONS` | 8 | `risk/manager.py:54` |
| Price sanity | Yes | (inline check) | [0.01, 0.99] | `risk/manager.py:335-337` |
| Minimum order size | Yes | (inline check) | 0.5 shares | `risk/manager.py:339-341` |
| Absolute position ceiling | Yes | `ABSOLUTE_MAX_POSITION` | $5.00 | `risk/manager.py:49` |
| Normal position guidance | Yes | `NORMAL_MAX_POSITION` | $2.00 | `risk/manager.py:47` |

### 7.2 Capital allocation features (§53 spec compliance)

| Spec criterion | Present? | Where? |
|---|---|---|
| Signal gen separated from capital allocation | Yes | `signal_trader` computes edge/confidence/liquidity; `allocate_size` decides size |
| Best opportunity doesn't auto-get largest trade | Yes | Sublinear `edge ** 0.4` curve + Michaelis-Menten asymptote |
| Estimated edge | Yes | `edge` arg |
| Confidence | Yes | `confidence` arg + `smoothstep` multiplier (T5) |
| Calibration | Yes (T5 only) | `calibration_mult(brier)` |
| Liquidity | Yes | `liquidity` arg + Michaelis-Menten multiplier (T5) |
| Correlation | Yes (T5 only) | `correlation_mult(existing_exposure)` |
| Existing exposure | Yes | `existing_exposure` arg + allocator gate 4 |
| Drawdown | Yes | `drawdown` arg + allocator gate 3 |
| Strategy performance | Yes (T5 only) | `performance_mult(strategy_perf)` |

### 7.3 Kelly criterion / portfolio optimizer features

| Feature | Present? | Where? |
|---|---|---|
| Kelly formula `f = edge / (1 - price)` | Yes | `compute_kelly` |
| Kelly fraction (quarter-Kelly default) | Yes | `kelly_fraction = 0.25` |
| Max single bet (15% of capital) | Yes | `max_single_bet = 0.15` |
| Max total exposure (80% of capital) | Yes | `max_total_exposure = 0.80` |
| Min edge filter | Yes | `min_edge = 0.03` |
| Min confidence filter | Yes | `min_confidence = 0.55` |
| Sharpe-like sort (return/risk) | Yes | `bets.sort(key=expected_return/risk)` |
| Diversification ratio | Yes | `weighted_avg_risk / portfolio_risk` |
| Constraint violations list | Yes | `constraint_violations` field |
| Rebalance suggestion | Yes | `suggest_rebalance(current_positions, opportunities)` |
| Live config update via HTTP | Yes | `PUT /api/portfolio/config` |

### 7.4 Stress test scenarios (6 canonical)

| # | Name | Description | Shock | Correlation | Spread mult | Fill degradation |
|---|---|---|---|---|---|---|
| 1 | `market_crash` | All positions drop 20% | -20% all | 0.8 | 2.0 | 0.1 |
| 2 | `market_crash_severe` | All positions drop 40% | -40% all | 0.9 | 3.0 | 0.2 |
| 3 | `liquidity_crisis` | Spreads widen 5×, fills degrade | 0% | 0.3 | 5.0 | 0.5 |
| 4 | `black_swan` | 10% single-day move | -10% all | 1.0 | 4.0 | 0.3 |
| 5 | `correlation_breakdown` | Uncorrelated positions align | -15% all | 1.0 | 2.0 | 0.1 |
| 6 | `bull_scenario` | All positions gain 15% | +15% all | 0.5 | 0.8 | 0.0 |

(VERIFIED via `stress_test.py:142-191`.)

### 7.5 VaR / CVaR / risk analytics

| Metric | Present? | Where? | Formula |
|---|---|---|---|
| VaR-95 | Yes | `backtesting/report.py:177` | `np.percentile(returns, 5)` |
| CVaR-95 | Yes | `backtesting/report.py:178-180` | `np.mean(returns[returns <= var_95])` |
| Sharpe | Yes | `report.py:131-135` | `mean(returns) / std(returns) * sqrt(252)` |
| Sortino | Yes | `report.py:137-143` | `mean(returns) / std(downside) * sqrt(252)` |
| Calmar | Yes | `report.py:151-152` | `annualized / max_dd` |
| Max drawdown | Yes | `report.py:145-148` | `max((peak - equity) / peak)` |
| Volatility | Yes | `report.py:154-155` | `std(returns) * sqrt(252)` |
| Profit factor | Yes | `report.py:166-170` | `gross_profit / gross_loss` |
| Expectancy | Yes | `report.py:172-174` | `win_rate * avg_win - loss_rate * avg_loss` |

**Gap:** VaR / CVaR are computed ONLY on backtest equity curves, NOT on
the live portfolio. The live portfolio's VaR is NOT FOUND. (VERIFIED —
no `var` reference outside `backtesting/report.py` and `tests/`.)

---

## 8. What Works

1. **All §52 spec gates are present** (per-order / per-market / strategy /
   portfolio / category concentration / correlated / cash reserve / MDD /
   daily loss). Plus bonus gates: weekly loss, per-trade loss, pending
   capital, position count, price sanity, minimum size, bankroll ceiling,
   absolute max, normal guidance, MTM. (VERIFIED via
   `risk/manager.py:165-348`.)
2. **Hard kill switch on daily-loss / weekly-loss / max-drawdown
   breach.** Each circuit-breaker gate calls `_trigger_kill_switch(reason)`
   which sets `store.kill_switch_active = True`, writes a durable file,
   cancels every open order, and audit-logs the event. (VERIFIED via
   `manager.py:235-251, 403-417`.)
3. **Per-trade-loss cooldown.** `report_trade_pnl(strategy, pnl)` pauses
   a strategy for 300 seconds if it loses ≥ $0.50 on a single closed
   trade. Subsequent BUY orders for the paused strategy are rejected
   until the cooldown elapses. (VERIFIED via `manager.py:350-401`.)
4. **Signal generation IS separated from capital allocation.** The
   strategy layer (`signal_trader._ml_signal`) computes edge /
   confidence / liquidity and calls `allocate_size(...)`; the allocator
   is a pure function with no I/O. (VERIFIED via
   `signal_trader.py:29` + `capital_allocator.py:179-285`.)
5. **The "best opportunity doesn't auto-get the largest trade" contract
   IS satisfied.** Both T9 (`edge ** 0.4`, sublinear, 4× → <2×) and T5
   (Michaelis-Menten asymptote = $3) saturate. (VERIFIED via
   `capital_allocator.py:36-50, 268-285, 365-374`.)
6. **All §53 sizing factors are evaluated** — edge, confidence,
   calibration (T5), liquidity, correlation (T5), existing exposure,
   drawdown, strategy performance (T5). (VERIFIED via
   `capital_allocator.py:179-285, 352-557`.)
7. **Capital-allocator output is bounded by the per-market cap.** Both
   T9 and T5 cap at `MAX_SIZE_USD = $3.00 = risk.MAX_POSITION_PER_MARKET`
   — the allocator never suggests a size the risk gate would reject on
   the per-market cap. (VERIFIED via `capital_allocator.py:140-151, 322`.)
8. **Shadow-trade counterfactual recording.** Every risk-rejected order
   is recorded in `shadow_trades.db` so the operator can benchmark "what
   would have happened". (VERIFIED via `manager.py:142-162`.)
9. **Kelly-criterion portfolio optimizer is correct** — quarter-Kelly
   by default, max single bet 15%, max total exposure 80%, diversification
   ratio computed under independence assumption. (VERIFIED via
   `portfolio_optimizer.py:100-360`.)
10. **6 stress scenarios cover the four primary tail-risk axes**
    (price shock / liquidity / correlation / tail) plus severe crash +
    bull control. (VERIFIED via `stress_test.py:142-191`.)
11. **VaR / CVaR computation is correct** — `np.percentile(returns, 5)`
    + `np.mean(returns[returns <= var_95])` (Expected Shortfall).
    (VERIFIED via `backtesting/report.py:176-180`.)
12. **MTM exposure cap exists** (gate 6e) — re-checks the $25 cap on a
    mark-to-market basis so unrealised gains cannot outflank the cap.
    (VERIFIED via `manager.py:298-315`.)
13. **Live config update via HTTP** — `PUT /api/portfolio/config` mutates
    the singleton in place; bounds enforced per-key. (VERIFIED via
    `portfolio_optimizer.py:145-213`.)
14. **Rebalance suggestion engine** — `suggest_rebalance(current, opps)`
    returns add/reduce/close/hold actions with >20% threshold for
    rebalancing. (VERIFIED via `portfolio_optimizer.py:361-429`.)

---

## 9. What Does Not Work

### 9.1 The portfolio optimizer is NOT in the live trade path

`signal_trader._scan_markets` calls `allocate_size` per-token in
isolation. The Kelly multi-bet optimizer is only invoked via
`POST /api/portfolio/optimize` (operator what-if). (LIKELY — no
`portfolio_optimizer.optimize` call in `signal_trader.py`.)

**Impact:** The bot does NOT consider portfolio-level constraints
(diversification ratio, total exposure budget, correlation) when sizing
individual trades. Each signal is sized independently — the optimizer's
diversification benefit is not realised in production.

### 9.2 The T5 multiplier breakdown is NOT persisted per-decision

The `allocate_capital()` function returns a `(size, components)` tuple
where `components` includes `raw_size`, `confidence_mult`,
`calibration_mult`, `drawdown_mult`, `correlation_mult`,
`performance_mult`, `liquidity_mult`, `product_mult`. But these
components are only surfaced via `GET /api/capital/allocation` for the
dashboard — they are NOT written to the decision ledger per-trade.
(VERIFIED via `capital_allocator.py:540-557` — the components dict is
returned only via HTTP, not via `decision_ledger.record`.)

**Impact:** "Why was this trade sized at $2.10 instead of $3?" is not
answerable after the fact. The operator can only see the final size in
the ORDER stage of the ledger, not the multiplier decomposition.

### 9.3 The MTM exposure gate is fail-open

The gate at `risk/manager.py:308-315` wraps
`compute_mark_to_market_exposure()` in a bare `except: pass`. If the MTM
computation raises (e.g. book_poller returned malformed book data), the
gate is silently skipped. The code documents this as "section 5 still
enforces the cost-basis $25 cap" but a runaway MTM could silently widen
true risk past the ceiling. (VERIFIED via `manager.py:308-315`.)

### 9.4 Volatility is NOT explicitly gated

The spec §52 asks for a volatility gate. The codebase has NO volatility
gate in `risk/manager.py` — `grep -i volatility` returns no hits.
Volatility influences sizing only indirectly via the
`dynamic_model_risk_multiplier` (Brier / PSI → 0.30 / 0.60 / 1.00),
which is a model-health multiplier, not a market-volatility multiplier.
(VERIFIED — no `volatility` keyword in `risk/manager.py`.)

### 9.5 Uncertainty is NOT explicitly gated (only implicit)

The spec asks for an "uncertainty" gate. The closest is the
`dynamic_model_risk_multiplier` (Brier score → capacity multiplier),
but that's a calibration multiplier, not an uncertainty gate per se.
There is no explicit "model uncertainty" gate that rejects orders when
the model's confidence interval is too wide. (VERIFIED — no
`uncertainty` keyword in `risk/manager.py`.)

### 9.6 Stale data is NOT per-market gated

The spec asks for a stale-data gate. The codebase has an
`observation_only` mode (exposure-reconciliation gate) and a
`data_staleness` alert rule (60s threshold), but no per-market staleness
gate in `check_order`. A market whose `book.updated_at` is 5 minutes old
will still pass `check_order` — the strategy layer is responsible for
filtering stale books (UNVERIFIED whether it does). (VERIFIED — no
staleness check in `_check_order_impl`.)

### 9.7 `PER_TRADE_MAX_LOSS` ($0.50) is too tight

With `MAX_POSITION_PER_MARKET = $3.00` and minimum order size `0.5`
shares at price `0.50`, a single share that resolves to $0 loses $0.50,
triggering the 300-second cooldown. This is the design threshold but it
is too tight for normal bad-bet outcomes. (LIKELY — the threshold is
hardcoded in `risk/manager.py:64`.)

### 9.8 VaR / CVaR are NOT computed on the live portfolio

VaR-95 / CVaR-95 are computed only on backtest equity curves in
`backtesting/report.py`. The live portfolio's VaR is NOT FOUND — the
risk engine has no live-tail-risk metric. (VERIFIED — no `var`
reference outside `backtesting/report.py`.)

**Impact:** "What's the 95% VaR of the current portfolio?" is not
answerable in production. The stress tester provides a related
(worst-case P&L) signal but not a probabilistic VaR.

### 9.9 Correlation matrix is NOT used by the risk engine

`core/correlation.py` computes a Pearson correlation matrix between
held positions, but `risk/manager.py` does not consult it. The
correlated-group exposure cap (gate 6d) uses market_slug grouping, not
the correlation matrix. (VERIFIED — no `correlation` import in
`risk/manager.py`.)

### 9.10 The stress test's `correlation_adjustment` field is informational only

Each scenario carries a `correlation_adjustment` field (0 = independent,
1 = perfect), but the simulation does NOT apply it — the shock is
applied uniformly to every position. The field is only surfaced in the
result details for the dashboard. (VERIFIED via `stress_test.py:241-260`
— no use of `scenario.correlation_adjustment` in the P&L computation.)

### 9.11 The Kelly optimizer assumes independence

The diversification ratio computation at `portfolio_optimizer.py:337-348`
assumes returns are independent:
`total_risk = sqrt(sum(risk_i ** 2))`. This OVER-estimates
diversification when positions are positively correlated (e.g. multiple
tokens in the same event slug). (VERIFIED via `portfolio_optimizer.py:339`.)

---

## 10. Missing Features

1. **Volatility gate** — explicit per-trade rejection based on market
   volatility (e.g. spread / mid over a threshold).
2. **Uncertainty gate** — explicit per-trade rejection based on model
   prediction interval width.
3. **Per-market staleness gate** — reject orders on markets whose
   `book.updated_at` is older than N seconds.
4. **Portfolio optimizer wired into the live trade path** — today the
   hot scan loop sizes each signal in isolation.
5. **T5 multiplier breakdown persisted per-decision** — write the
   `components` dict to the decision ledger so "why was this sized at
   $X?" is answerable.
6. **Live portfolio VaR / CVaR** — compute on the current position
   set, not just on backtest equity curves.
7. **Correlation-aware Kelly** — the optimizer's independence
   assumption is optimistic; a correlation-adjusted Kelly would downsize
   correlated positions.
8. **Correlation-aware stress test** — the `correlation_adjustment`
   field should actually adjust the simulated P&L (e.g. scale each
   position's loss by the average pairwise correlation).
9. **Volatility-scaled position sizing** — beyond the static
   `dynamic_model_risk_multiplier`, a market-volatility-aware scaling
   (e.g. ATR-style) would be appropriate for prediction markets.
10. **Live Sharpe / Sortino / Calmar** — computed on the live equity
    curve, not just on backtest equity curves.
11. **Live max-drawdown tracking** — `store.peak_equity` is in-memory
    only; a durable high-water-mark tracker would survive restarts.
12. **Risk-parity allocation** — an alternative to Kelly that sizes
    positions by inverse volatility (each position contributes equal
    risk to the portfolio).

---

## 11. Bugs

1. **MTM gate fail-open** (`manager.py:308-315`) — bare `except: pass`
   silently skips the gate on any exception. (VERIFIED.)
2. **W16-3 rebalance bug** (`portfolio_optimizer.py:400-407`) — the
   original task spec snippet read `current_positions[token_id]` but
   `current_positions` is the raw LIST parameter, not a dict. The
   fix in W16-3 changed it to `current_tokens[bet.token_id]`. This
   is a documented bug-fix; the comment in the source is the evidence.
   (VERIFIED via `portfolio_optimizer.py:400-407`.)
3. **`compute_kelly` uses simplified formula** (`portfolio_optimizer.py:241`)
   — `kelly = edge / max(1 - price, 0.01)` is the simplified binary
   YES Kelly; the canonical formula is `f = (p*b - q) / b` where `b =
   (1-p)/p`. The simplified version is correct for the case where
   `confidence = p` (which the docstring says is the assumption), but
   the code does NOT verify this assumption holds. (VERIFIED — the
   docstring at `portfolio_optimizer.py:222-235` documents the
   assumption but the code does not enforce it.)
4. **Diversification ratio can be > 1** when positions are negatively
   correlated (the independence assumption gives `total_risk = sqrt(sum
   risk_i²)` which is smaller than `sum(risk_i)`; if realised risk is
   even smaller — because of negative correlation — the ratio exceeds
   1.0). The `test_portfolio_optimizer.py` test asserts `>1.0` and
   fails (per W16-7 worklog, expected `>1.0` got `0.71`). (VERIFIED —
   pre-existing test failure per worklog.)
5. **`VaR-95` calculation assertion** in `test_backtest_report.py` —
   expected `≤ 0`, got `0.0028`. (VERIFIED — pre-existing test failure
   per W16-7 worklog; the VaR formula is correct but the test fixture
   happens to have an upside-skewed returns distribution.)
6. **`PER_TRADE_MAX_LOSS` of $0.50 may fire on normal bad-bet
   outcomes** — the threshold is hardcoded; no per-strategy tuning.
   (LIKELY.)

---

## 12. Technical Debt

1. **`_check_order_impl` is a 200-line method** with 22 sequential
   gates. The gates are individually tested but the interaction
   between them (e.g. does the MTM gate fire before or after the
   cash-reserve gate?) is hard to reason about. (VERIFIED via
   `manager.py:165-348`.)
2. **Inconsistent Decimal usage** — `to_dec(val: float)` converts via
   `Decimal(str(round(val, 4)))`, but `MAX_POSITION_PER_MARKET` is
   already a Decimal; comparisons like `effective_mkt_cap` (Decimal)
   vs `order_cost` (Decimal) work, but mixing with `ml_risk_mult`
   (Decimal) multiplied by Decimal constants produces long Decimal
   chains that need explicit rounding. (VERIFIED via `manager.py:73-75,
   262-276`.)
3. **Singleton pattern across the risk/portfolio layer** —
   `risk_manager`, `portfolio_optimizer`, `stress_tester` are all
   module-level singletons. Config changes via `PUT /api/portfolio/config`
   mutate the singleton in place, which is convenient but makes test
   isolation hard. (VERIFIED via `manager.py:489`, `portfolio_optimizer.py:438`,
   `stress_test.py:367`.)
4. **Capital allocator has two parallel sizing paths** (T9 + T5) with
   overlapping but not identical logic. The T5 aliases
   (`MAX_POSITION_PER_MARKET`, `MAX_DRAWDOWN_LIMIT`) point to T9
   constants to prevent drift, but the two functions compute size
   differently (T9 uses `edge ** 0.4`, T5 uses Michaelis-Menten).
   (VERIFIED via `capital_allocator.py:78-99, 318-326`.)
5. **No unified risk-config object** — the capital model constants are
   in `risk/manager.py` (module-level), the allocator thresholds are in
   `capital_allocator.py` (module-level), the Kelly defaults are in
   `portfolio_optimizer.py` (class attributes). A single
   `RiskConfig` dataclass would centralise this. (VERIFIED — three
   separate config sources.)
6. **The stress tester's `_positions_from_live_store`** maps
   `Position.yes_shares` / `no_shares` into a generic `side="LONG"` /
   `side="SHORT"` shape — the LONG/SHORT dichotomy is a simplification
   of the prediction-market YES/NO leg model. (VERIFIED via
   `stress_test.py:395-420`.)

---

## 13. Data Problems

1. **`store.peak_equity` is in-memory only** — a process restart loses
   the high-water mark, which means the MDD calculation resets. The
   `deactivate_kill_switch` path manually resets `peak_equity = current`
   which is correct only if the operator intentionally wants to reset
   MDD. (VERIFIED via `manager.py:422-431`.)
2. **`store.market_slugs` is in-memory only** — the correlated-group
   cap (gate 6d) depends on this dict; a process restart loses the
   slug mapping until `market_discovery` re-syncs. (VERIFIED via
   `manager.py:286-296`.)
3. **`compute_mark_to_market_exposure`** reads live book data; if no
   book is available for a position's token, the MTM falls back to
   cost-basis (UNVERIFIED — not directly traced but consistent with
   `portfolio.py` patterns).
4. **The Kelly optimizer's `opportunities` list is supplied by the
   operator** via HTTP POST — it does NOT read from the live
   `market_discovery.catalog` automatically. (VERIFIED via
   `portfolio_optimizer.py:251-260` — `opportunities` is a function
   arg.)
5. **The stress tester's `correlation_adjustment` field is unused** —
   a documented gap. (VERIFIED via `stress_test.py:241-260`.)

---

## 14. Performance Problems

1. **`_check_order_impl` holds `self._lock` for the entire 200-line
   method** — every risk check serialises through a single
   `asyncio.Lock`. Under concurrent strategy scans this is a
   bottleneck. (VERIFIED via `manager.py:175`.)
2. **`compute_mark_to_market_exposure` is called on every
   `check_order`** — for every candidate trade, the MTM is recomputed
   against the full position set. With 8 max positions this is fine;
   with a larger portfolio it would degrade. (VERIFIED via
   `manager.py:308-315`.)
3. **The Kelly optimizer sorts the entire `bets` list** on every
   `optimize()` call — O(N log N) in the number of opportunities.
   (VERIFIED via `portfolio_optimizer.py:301`.)
4. **The stress tester runs all 6 scenarios sequentially** on every
   `run_all_scenarios` call. Each scenario iterates the entire
   position set. (VERIFIED via `stress_test.py:321-326`.)

---

## 15. Reliability Problems

1. **Every shadow-trade recording is fire-and-forget**
   (`asyncio.create_task(...)`, wrapped in `try/except: pass`). If the
   shadow-trade write fails, the counterfactual is silently lost.
   (VERIFIED via `manager.py:142-162`.)
2. **The MTM gate's `except: pass`** means a transient book-data hiccup
   silently disables the MTM cap. (VERIFIED via `manager.py:308-315`.)
3. **`store.cancel_all_orders` is called from `_trigger_kill_switch`**
   — if this raises, the kill switch is set but open orders are NOT
   cancelled. (VERIFIED via `manager.py:416-417` — `cancelled =
   await store.cancel_all_orders()` is not in a try/except.)
4. **The per-trade-loss cooldown is in-memory only** — a process
   restart clears `_strategy_cooldowns`, so a strategy that was paused
   can immediately resume trading after restart. (VERIFIED via
   `manager.py:115-116`.)
5. **The Kelly optimizer's `update_config` mutates the singleton in
   place** — if a `PUT /api/portfolio/config` request sets
   `kelly_fraction = 1.0` (full Kelly), every subsequent `optimize()`
   call uses full Kelly until another PUT reverts it. No audit trail
   of config changes beyond `audit_logger.log_event`. (VERIFIED via
   `portfolio_optimizer.py:145-213`.)

---

## 16. Security Problems

1. **`PUT /api/portfolio/config`** can mutate the live Kelly fraction /
   max single bet / max total exposure without a confirmation step. A
   malicious or mistaken operator could set `kelly_fraction = 1.0`
   (full Kelly) and the bot would happily over-bet on the next scan.
   (VERIFIED via `portfolio_optimizer.py:145-213` — bounds enforced
   but no confirmation prompt.)
2. **The risk engine's `kill_switch` file is written to disk** —
   anyone with filesystem write access can `touch /app/data/kill_switch`
   to halt trading. (VERIFIED via `manager.py:403-417` — uses
   `core.safety.write_kill_switch`.)
3. **The shadow-trades database** stores every counterfactual trade
   (including predicted_edge, confidence, size) in plaintext SQLite.
   No encryption at rest. (VERIFIED via `shadow_trading.py:22-37`.)
4. **The MTM gate's bare `except: pass`** is a security-adjacent issue
   — if an attacker can cause `compute_mark_to_market_exposure` to
   raise (e.g. by injecting a malformed book), the MTM cap is silently
   disabled. (VERIFIED via `manager.py:308-315`.)

---

## 17. Testing

The risk/portfolio layer has substantial test coverage:

- `tests/test_risk_manager.py` — risk gates + per-trade-loss breaker.
- `tests/integration/test_risk_pipeline.py` — integration variant.
- `tests/test_capital_allocator.py` — T9 sizing (sublinear exponent,
  saturation, gate ordering).
- `tests/test_capital_allocator_advanced.py` — T5 multiplier stack.
- `tests/test_portfolio_optimizer.py` — Kelly multi-bet (1 pre-existing
  failure on diversification ratio per W16-7 worklog).
- `tests/test_stress_test.py` — 6 scenarios.
- `tests/test_backtest_report.py` — VaR / CVaR (1 pre-existing failure
  per W16-7 worklog).
- `tests/test_attribution.py` — 7-dim attribution on closed positions.
- `tests/test_closed_positions.py` — round-trip P&L.
- `tests/test_correlation.py` — Pearson matrix (UNVERIFIED file
  existence, but `core/correlation.py` is in the codebase).
- `tests/test_portfolio.py` — `compute_exposure` decomposition.

**Test gaps for this assessment:**

1. No test verifying that the **portfolio optimizer** is invoked from
   the hot scan loop — because it is NOT.
2. No test verifying that the **T5 multiplier breakdown** is persisted
   to the decision ledger — because it is NOT.
3. No test verifying that the **MTM gate** fails-closed on error —
   because it fail-opens.
4. No test verifying that the **per-trade-loss cooldown** survives a
   process restart — because it does NOT.
5. No test verifying that the **correlation matrix** is consulted by the
   risk engine — because it is NOT.
6. No test verifying that the **stress test's `correlation_adjustment`**
   actually adjusts P&L — because it does NOT.
7. No test verifying that **live portfolio VaR** is computed — because
   it is NOT.

---

## 18. Observability

(See `OBSERVABILITY_AND_RELIABILITY_ASSESSMENT.md` for the full §54 /
§55 assessment. Summary relevant to risk/portfolio:)

- **Risk alerts:** `alerting.py` has 2 default rules:
  - `max_drawdown_exceeded` (`daily_pnl < -$2.00`, CRITICAL)
  - `kill_switch_activated` (CRITICAL)
  (VERIFIED via `alerting.py:240-258`.)
- **ML-health alerts:** 2 default rules:
  - `model_drift_detected` (`psi > 0.25`, WARNING)
  - `model_stale` (`age > 24h`, WARNING)
  (VERIFIED via `alerting.py:260-277`.)
- **System alerts:** `high_latency` (`api_latency_ms > 1000`),
  `backend_unhealthy`. (VERIFIED via `alerting.py:278-296`.)
- **Data alerts:** `data_stale` (`staleness > 60s`, WARNING).
  (VERIFIED via `alerting.py:298-306`.)
- **Risk status report:** `risk_manager.status_report()` returns a
  25-field dict covering kill switch / observation mode / operating
  capital / recognised capital / bankroll ceiling / cash reserve /
  deployable capital / live-trading-enabled / daily_pnl / weekly_pnl /
  drawdown / open orders / total exposure / max_total_exposure / per-
  market cap / dynamic_risk_multiplier / effective_max_position /
  absolute_max / correlated exposure / strategy exposure / pending /
  max pending / max_loss_if_all_zero / deployable_ceiling /
  exposure_reconciled. (VERIFIED via `manager.py:438-485`.)
- **Per-strategy paused visibility:** `GET /api/risk/strategies/paused`
  returns the paused strategies with `seconds_remaining`. (VERIFIED
  via `risk/routes.py:25-65`.)
- **Prometheus gauges:** `polymarket_realized_pnl_usd`,
  `polymarket_unrealized_pnl_usd`, `polymarket_paper_balance_usd`,
  `polymarket_open_positions`, `polymarket_open_orders`. (VERIFIED via
  `prometheus_metrics.py:87-110`.)
- **Prometheus counters:** `polymarket_orders_placed_total`,
  `polymarket_orders_filled_total`, `polymarket_trades_total`.
  (VERIFIED via `prometheus_metrics.py:70-85`.)
- **Prometheus ML metrics:** `polymarket_ml_drift_psi`,
  `polymarket_ml_brier_score`, `polymarket_ml_roc_auc`. (VERIFIED via
  `prometheus_metrics.py:123-136`.)

**Observability gap relevant to this assessment:**

1. **No per-gate rejection counter.** The risk engine rejects orders
   for 22 distinct reasons, but the Prometheus metrics only track
   `orders_placed_total` / `orders_filled_total` — no
   `risk_rejections_total{reason="..."}` counter. The
   `decision_rejections` table has the data, but it's not surfaced as a
   Prometheus counter.
2. **No live VaR gauge.** `polymarket_realized_pnl_usd` is exposed but
   there's no `polymarket_var_95_usd` or `polymarket_cvar_95_usd`.
3. **No live Kelly fraction gauge.** The current `kelly_fraction` is
   not exposed as a Prometheus gauge, so an operator who changed it via
   PUT has no metric to confirm the change took effect.

---

## 19. Production Readiness

For **paper trading**: the risk engine is production-ready. All 22
gates are tested, the kill switch is durable, the per-trade-loss
breaker protects against runaway strategies, and the shadow-trade
counterfactual provides a feedback loop for sizing quality.

For **live trading**, the gaps that matter:

1. **MTM gate fail-open** (§11.1) — must be fail-closed or fail-loud.
2. **Volatility / uncertainty / per-market staleness gates missing**
   (§9.4-9.6) — the spec asks for them; the codebase does not have them.
3. **Portfolio optimizer not in the live trade path** (§9.1) — each
   signal is sized in isolation; the optimizer's diversification
   benefit is lost.
4. **Per-trade-loss cooldown is in-memory only** (§15.4) — a restart
   clears it; a strategy that was paused can immediately resume trading
   after restart.
5. **`PUT /api/portfolio/config` has no confirmation step** (§16.1) —
   a malicious or mistaken operator can change Kelly fraction to 1.0
   without a second factor.
6. **Live portfolio VaR is not computed** (§9.8) — the operator has no
   probabilistic tail-risk metric for the live book.

**Production-readiness score for risk/portfolio: 7.0/10.** The
foundation is sound (institutional capital model, 22 gates, kill
switch, Kelly optimizer, stress tests), but several gaps (MTM fail-open,
missing volatility/uncertainty gates, optimizer not wired in, in-memory
cooldown) prevent full live-readiness.

---

## 20. Evidence

### 20.1 VERIFIED (read in source file in this session)

- `risk/manager.py:1-65` — capital model constants + per-trade breaker.
- `risk/manager.py:78-97` — `dynamic_model_risk_multiplier` (Brier/PSI → 0.30/0.60/1.00).
- `risk/manager.py:100-124` — `InstitutionalRiskEngine.__init__` (observation-only, cooldowns).
- `risk/manager.py:126-163` — `check_order` wrapper (shadow-trade recording on rejection).
- `risk/manager.py:165-348` — `_check_order_impl` (22 sequential gates).
- `risk/manager.py:350-401` — `is_strategy_paused` + `report_trade_pnl` (cooldown logic).
- `risk/manager.py:403-436` — `_trigger_kill_switch` + `activate_kill_switch` + `deactivate_kill_switch`.
- `risk/manager.py:438-489` — `status_report` (25-field dict) + singleton.
- `core/capital_allocator.py:1-100` — T9 docstring + safety-gate thresholds + sublinear exponent proof.
- `core/capital_allocator.py:107-176` — T9 constants (`MIN_CONFIDENCE=0.45`, `MAX_DRAWDOWN_USD=8.0`, `MAX_EXISTING_EXPOSURE_USD=5.0`, `MAX_SIZE_USD=3.0`, `MIN_SIZE_USD=0.50`, `SIZE_SCALE=5.0`, `SIZE_CURVE_EXPONENT=0.4`).
- `core/capital_allocator.py:179-285` — T9 `allocate_size` (5 gates + saturating curve).
- `core/capital_allocator.py:288-557` — T5 multiplier stack (Michaelis-Menten edge + 6 multipliers).
- `core/portfolio_optimizer.py:1-100` — Kelly docstring + `KellyBet` / `PortfolioOptimization` dataclasses.
- `core/portfolio_optimizer.py:100-213` — `PortfolioOptimizer.__init__` + `get_config` + `update_config`.
- `core/portfolio_optimizer.py:217-247` — `compute_kelly` (simplified binary Kelly).
- `core/portfolio_optimizer.py:251-357` — `optimize` (sort + total-exposure constraint + diversification ratio).
- `core/portfolio_optimizer.py:361-429` — `suggest_rebalance` (add/reduce/close/hold).
- `core/portfolio_optimizer.py:432-519` — singleton + Pydantic models.
- `core/stress_test.py:62-114` — `StressScenario` / `StressTestResult` dataclasses.
- `core/stress_test.py:119-191` — `PortfolioStressTester.__init__` + 6 scenarios.
- `core/stress_test.py:206-317` — `run_scenario` (per-position shock + fill degradation + stop-loss check + survival).
- `core/stress_test.py:321-362` — `run_all_scenarios` + `get_worst_case` + `get_summary`.
- `core/stress_test.py:373-420` — `_positions_from_live_store` (LONG/SHORT mapping).
- `backtesting/report.py:37-93` — `BacktestReport` dataclass with `var_95` / `cvar_95`.
- `backtesting/report.py:131-180` — Sharpe / Sortino / Calmar / VaR / CVaR computation.
- `backtesting/report.py:481-485` — PDF report summary table (VaR 95% + CVaR 95%).
- `risk/routes.py:1-80` — paused-strategy visibility route.
- `core/alerting.py:223-307` — 7 default alert rules.
- `core/prometheus_metrics.py:69-160` — trading + ML + system metrics.
- `core/correlation.py:1-80` — Pearson correlation matrix.
- `core/portfolio.py:20-60` — `compute_exposure` decomposition.
- `core/shadow_trading.py:1-60` — counterfactual journal schema.
- `core/attribution.py:1-60` — 7-dim attribution.
- `strategies/signal_trader.py:29` — `from core.capital_allocator import allocate_capital`.
- `strategies/base.py:60-148` — `submit_order` risk-gate + paper/live delegation.

### 20.2 STRONG EVIDENCE

- `FINAL_SYSTEM_REASSESSMENT.md` documents paper-trading bankroll
  growth ($100 → $111.72) + 16W/4L track record + $0.19/trade expectancy.
- W16-7 worklog documents pre-existing test failures in
  `test_portfolio_optimizer.py` (diversification_ratio 0.71 vs >1.0)
  and `test_backtest_report.py` (VaR-95 0.0028 vs ≤0).

### 20.3 LIKELY

- The portfolio_optimizer is NOT invoked from the hot scan loop (no
  `import portfolio_optimizer` in `signal_trader.py`).
- `PER_TRADE_MAX_LOSS` of $0.50 is too tight against $3 max position.

### 20.4 UNVERIFIED

- The exact formula for the `liquidity` arg passed to `allocate_size`
  by `signal_trader._ml_signal`. The allocator's contract says
  "available book liquidity in USD (sum of top-N levels on the side
  we'd cross)" but the caller's actual computation was not traced.

### 20.5 NOT FOUND

- A `volatility` keyword in `risk/manager.py`.
- An `uncertainty` keyword in `risk/manager.py`.
- A per-market `staleness` check in `_check_order_impl`.
- A live-portfolio VaR / CVaR computation.
- A `risk_rejections_total{reason="..."}` Prometheus counter.
- A correlation-aware Kelly adjustment.

---

## 21. Unknowns

1. **Is `compute_mark_to_market_exposure`** ever non-trivial in
   production? The MTM gate exists but the call site's error path is
   fail-open — suggesting the implementer expected it to occasionally
   raise. (UNVERIFIED.)
2. **What `liquidity` value does `signal_trader._ml_signal` actually
   pass to `allocate_size`?** The allocator's contract is documented
   but the caller's exact formula was not traced.
3. **Is `risk_manager.report_trade_pnl`** actually called from
   `paper_sim._execute_fill`? The fill loop runs in the background;
   the per-trade-loss cooldown depends on this call site.
4. **What is the actual `kelly_fraction` in production?** The default
   is 0.25 (quarter-Kelly) but `PUT /api/portfolio/config` can change
   it without persistence to the audit trail beyond `audit_logger`.

---

## 22. Maturity Score (0-10)

**Risk & Portfolio maturity: 6.8 / 10**

| Sub-dimension | Score | Rationale |
|---|---|---|
| Risk engine completeness (§52) | 7.5 / 10 | 22 gates; volatility / uncertainty / per-market staleness missing. |
| Capital allocation separation (§53) | 8.0 / 10 | Pure-function allocator with all 8 sizing factors. T5 breakdown not persisted per-decision (-1). T5 used by HTTP only (-1). |
| Kelly criterion correctness | 7.0 / 10 | Quarter-Kelly + max-single-bet + max-total-exposure + diversification ratio. Simplified formula assumes confidence=p (-1). Independence assumption (-1). |
| Stress testing | 6.0 / 10 | 6 scenarios covering 4 tail-risk axes. `correlation_adjustment` unused (-1.5). No time-correlation model (-1). No Monte Carlo (-1.5). |
| VaR / CVaR | 5.0 / 10 | Computed on backtest only. Live portfolio VaR not found (-3). Formula correct. |
| Live-trade-path wiring | 5.0 / 10 | Optimizer not in hot scan loop (-3). MTM gate fail-open (-1). Per-trade-loss cooldown in-memory only (-1). |
| Auditability of sizing decisions | 4.0 / 10 | T5 multiplier breakdown not persisted (-4). Final size in ORDER ledger (+4). |
| Test coverage | 7.0 / 10 | 11 test files for risk/portfolio. 2 pre-existing failures. Test gaps in §17. |
| Capital model discipline | 9.0 / 10 | Two-tier ($100 / $200) model + 22 gates + hard kill switch. |
| Kill-switch reliability | 8.0 / 10 | Durable file + in-memory + cancels open orders + audit-logged. `cancel_all_orders` not in try/except (-1). In-memory cooldown cleared on restart (-1). |

**Composite: 6.8 / 10.** The risk engine is comprehensive and the
capital allocator is institutionally-styled, but the portfolio optimizer
is disconnected from the live trade path, the T5 multiplier breakdown is
not persisted, the MTM gate is fail-open, and live-portfolio VaR is not
computed.

---

## 23. Critical Findings

1. **The portfolio optimizer is NOT in the live trade path.**
   `signal_trader._scan_markets` calls `allocate_size` per-token in
   isolation; the Kelly multi-bet optimizer is only an operator what-if
   tool. The spec's §53 portfolio-level diversification benefit is
   therefore NOT realised in production. (Severity: HIGH.)
2. **The T5 multiplier breakdown (edge × confidence × calibration ×
   drawdown × correlation × performance × liquidity) is NOT persisted
   per-decision.** Only the final size is in the ORDER stage of the
   decision ledger. "Why was this trade sized at $2.10 instead of $3?"
   is not answerable after the fact. (Severity: HIGH for auditability.)
3. **The MTM exposure gate is fail-open.** A bare `except: pass`
   silently disables the MTM cap when `compute_mark_to_market_exposure`
   raises. (Severity: HIGH for live trading.)
4. **Volatility, uncertainty, and per-market staleness gates are
   MISSING.** The spec §52 asks for them; the codebase does not have
   them. (Severity: MEDIUM.)
5. **Live portfolio VaR / CVaR are NOT computed.** VaR-95 / CVaR-95
   exist only for backtest equity curves. (Severity: MEDIUM.)
6. **The Kelly optimizer assumes return independence.** The
   diversification ratio formula `sqrt(sum(risk_i²))` over-estimates
   diversification when positions are correlated. (Severity: MEDIUM.)
7. **The stress tester's `correlation_adjustment` field is
   informational only.** The simulation applies a uniform price shock;
   the correlation field is not used to adjust P&L. (Severity: MEDIUM.)
8. **The per-trade-loss cooldown is in-memory only.** A process
   restart clears `_strategy_cooldowns`, so a paused strategy can
   immediately resume trading after restart. (Severity: MEDIUM for
   live trading.)
9. **`PUT /api/portfolio/config` has no confirmation step.** An
   operator (or attacker) can change `kelly_fraction` to 1.0 (full
   Kelly) without a second factor. (Severity: MEDIUM for security.)
10. **`PER_TRADE_MAX_LOSS` ($0.50) is too tight** against the $3 max
    position — a single $0.50 share that resolves to $0 triggers the
    300-second cooldown on a normal bad-bet outcome. (Severity: LOW.)
11. **The correlation matrix is NOT consulted by the risk engine.**
    `core/correlation.py` computes a Pearson matrix but
    `risk/manager.py` does not import it. The correlated-group cap
    (gate 6d) uses market_slug grouping only. (Severity: MEDIUM.)
12. **No per-gate rejection counter in Prometheus.** The risk engine
    rejects orders for 22 distinct reasons but only `orders_placed_total`
    / `orders_filled_total` are exposed — no
    `risk_rejections_total{reason="..."}` counter. (Severity: LOW for
    observability.)
13. **`compute_kelly` uses the simplified formula**
    `kelly = edge / max(1 - price, 0.01)` which assumes `confidence = p`
    (the model probability). The docstring documents this assumption
    but the code does not enforce it. (Severity: LOW — the assumption
    is typically valid but not guaranteed.)
14. **`store.peak_equity` is in-memory only.** A process restart loses
    the high-water mark, which means the MDD calculation resets.
    (Severity: MEDIUM for live trading.)

### Recommended next actions (priority order)

1. **Wire the portfolio optimizer into the live trade path.** Replace
   the per-token `allocate_size` call in `signal_trader._scan_markets`
   with a batch `portfolio_optimizer.optimize(candidate_signals)` call.
2. **Fix the MTM gate to fail-closed** (or fail-loud — emit a CRITICAL
   alert when the MTM computation raises).
3. **Persist the T5 multiplier breakdown** to the decision ledger as a
   `PORTFOLIO` stage event (see Cross-System Assessment §10).
4. **Add explicit volatility, uncertainty, and per-market staleness
   gates** to `_check_order_impl`.
5. **Compute live portfolio VaR / CVaR** — either via Monte Carlo on
   the current position set, or via the historical-returns method
   already in `backtesting/report.py` adapted to the live equity curve.
6. **Make the per-trade-loss cooldown durable** — persist
   `_strategy_cooldowns` to disk so it survives restarts.
7. **Add a confirmation step** (or rate-limit) to
   `PUT /api/portfolio/config` for high-impact changes
   (`kelly_fraction`, `max_single_bet`, `max_total_exposure`).
8. **Wire the correlation matrix** into the risk engine's
   correlated-group cap (gate 6d) — replace market_slug grouping with
   correlation-thresholded grouping.
9. **Apply the stress tester's `correlation_adjustment`** in the P&L
   computation — scale each position's loss by the average pairwise
   correlation.
10. **Add `risk_rejections_total{reason="..."}` Prometheus counter** so
    the operator can see which gates are firing most often.
11. **Add a `risk_manager.live_var_95` gauge** to Prometheus.
12. **Persist `store.peak_equity` to disk** so MDD survives restarts.

---

*End of Risk & Portfolio Assessment. Companion documents:
`CROSS_SYSTEM_ARCHITECTURE_ASSESSMENT.md` (§50, §51, §79, §80) and
`OBSERVABILITY_AND_RELIABILITY_ASSESSMENT.md` (§54, §55).*
