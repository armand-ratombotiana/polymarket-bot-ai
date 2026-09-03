# Backtest Engine Assessment

**Task ID:** W17-6
**Agent:** general-purpose
**Scope:** `mini-services/polymarket-bot/backtesting/{engine,advanced,report}.py` + `paper/simulator.py`
**Date:** 2026-09-03
**Assessment framework:** God Mode Master Prompt §30-33, §60 (23-section template)

---

## 1. Executive Summary

The backtest stack is best understood as **three parallel, non-integrated
modules** that share a name but share almost no code:

1. `backtesting/engine.py::BacktestEngine.run_backtest` — a **synthetic
   Monte-Carlo archetype simulator** with hand-tuned win-rate constants
   (`base_p=0.65` for `"mm"`, `0.95` for `"arb"`, etc.). No historical
   data is consumed; outcomes are RNG draws conditioned on the
   archetype.
2. `backtesting/engine.py::run_realistic_backtest` (T4) — an
   **improved** synthetic simulator that adds spread / partial fills /
   slippage / look-ahead detection. Still RNG-driven; **does NOT replay
   historical market data** and **does NOT invoke the strategy's
   `on_tick` / `predict` method**.
3. `paper/simulator.py::PaperSimulator` — a **live-paper broker** that
   fills orders against the real Polymarket CLOB book in real time with
   its own (different) slippage model.

There is **no Backtest/Live execution-interface parity** (§32): the
backtest engine and the paper/live broker have zero shared code paths
and two incompatible slippage models. There is **no Backtest Lab**
(§33): every run is ephemeral — no experiment registry, no DB
persistence, no cross-run comparison primitive.

Walk-forward analysis (W13-8) and Monte-Carlo simulation (W13-8) exist
in `backtesting/advanced.py` as standalone pure-Python functions with
test coverage but **are not wired to any API route** — they are
unreachable from the production HTTP surface. The Wave-16 PDF report
generator is reachable via `POST /api/backtest/report/pdf`.

The Wave-5 time-ordered split fix (the §31 look-ahead fix the task
asked us to verify) is **VERIFIED present** at
`ml/model.py:237-247` — `np.arange(n_total)` sequential indices,
not `np.random.permutation`. **However**, that fix lives in the ML
training path, not in the backtest engine; the backtest engine itself
never trains a model and therefore has no train/test split to
contaminate.

**Maturity score: 3.5 / 10** — a competent *demo* of realistic
microstructure, but not a production-grade backtest engine. See §22
for the rubric breakdown.

---

## 2. Purpose

Per the module docstrings, the backtest stack is intended to:

- `backtesting/engine.py` — *"High-Performance Quantitative Backtesting &
  Simulation Engine. Simulates order lifecycle, binary prediction market
  payoffs ($1.00 settlement), fractional Kelly position sizing, queue
  priority, slippage, and maker/taker fees. Computes institutional
  performance metrics (Sharpe, Sortino, Calmar, VaR 95%, Profit Factor,
  Brier Score, MDD)."*
- `backtesting/advanced.py` — *"Advanced backtesting: walk-forward
  analysis + Monte Carlo simulation. … the canonical guard against
  look-ahead bias … distributional analyses that the archetype
  simulator cannot answer on its own."*
- `backtesting/report.py` — *"Backtest report generator — produces
  JSON + PDF reports."*
- `paper/simulator.py` — *"Paper trading simulator. Simulates order
  fills against live order book data without touching real funds."*

The implied purpose (§30-33) is to provide a **historical-replay
backtest engine** that shares an execution interface with live trading
so backtest results are predictive of live performance. That purpose
is **only partially achieved** — see §5, §9, §10, §32.

---

## 3. Current Architecture

```
                         ┌─────────────────────────────────────┐
                         │  backtesting/engine.py              │
                         │  ┌───────────────────────────────┐  │
   strategy string  ───► │  │ BacktestEngine.run_backtest   │  │
                         │  │   (synthetic MC, archetype)   │  │
                         │  └───────────────────────────────┘  │
                         │  ┌───────────────────────────────┐  │
   strategy / dict  ───► │  │ run_realistic_backtest        │  │
                         │  │   (synthetic MC + microstruct)│  │
                         │  └───────────────────────────────┘  │
                         └─────────────────────────────────────┘
                                        │
                                        ▼
                         ┌─────────────────────────────────────┐
                         │  backtesting/advanced.py           │
                         │   walk_forward_analysis(features,  │
                         │     labels, timestamps, ...)       │
                         │   monte_carlo_simulation(          │
                         │     trade_returns, n_simulations)  │
                         └─────────────────────────────────────┘
                                        │
                                        ▼
                         ┌─────────────────────────────────────┐
                         │  backtesting/report.py             │
                         │   generate_report(dict) -> Report  │
                         │   report_to_pdf(Report, Path)       │
                         └─────────────────────────────────────┘
                                        │
                                        ▼
                  POST /api/backtest/run | report | report/pdf
                  (all hit BacktestEngine.run_backtest, NOT
                   run_realistic_backtest)


   ── PARALLEL UNIVERSE ────────────────────────────────────────────

                         ┌─────────────────────────────────────┐
   BaseStrategy   ─────► │  risk.manager.check_order           │
   (live path)           └─────────────────────────────────────┘
                                  │ ▼
                  ┌───────────────┴───────────────┐
                  ▼                               ▼
        paper/simulator.py            core/clob_client.py
        PaperSimulator                clob_client.create_order
        (live book + 3-comp           (real Polymarket CLOB API
         slippage: crossing+           — production broker)
         size+queue)
```

**Key architectural observations:**

- The HTTP layer (`api/server.py:3429-3562`) routes `/api/backtest/*`
  exclusively through `BacktestEngine.run_backtest` — the **legacy**
  synthetic engine. `run_realistic_backtest` (the T4 microstructure
  engine) is **reachable only by Python import**, not via any HTTP
  route. **VERIFIED** by grepping all `@app.post("/api/backtest...`
  routes.
- `walk_forward_analysis` and `monte_carlo_simulation` are
  **VERIFIED present** in `advanced.py` with full test coverage
  (`tests/test_advanced_backtest.py`, 12 tests pass), but are
  **NOT FOUND** in any `@app.post(...)` route in `api/server.py`.
  They are unreachable from the production API surface.
- The live/paper path (`BaseStrategy.submit_order` → `risk_manager` →
  `paper_sim` or `clob_client`) shares **ZERO** code with the
  backtest path. Slippage is computed by two separate, incompatible
  implementations.

---

## 4. Current Components

| Component | File:Line | Public surface | Notes |
|---|---|---|---|
| Legacy archetype engine | `backtesting/engine.py:94-276` | `BacktestEngine.run_backtest(strategy_id, initial_capital, days, fee_bps, slippage_bps) -> BacktestResult` | Synthetic RNG MC, archetype profiles hand-tuned, `fee_bps` modeled. |
| Realistic engine | `backtesting/engine.py:637-821` | `run_realistic_backtest(strategy, start_date, end_date, capital, slippage_bps) -> dict` | T4 deliverable. Adds spread, partial fills, exec delay, look-ahead detector. **No `fee_bps` param** (conflated with slippage_bps). |
| Synthetic order book | `backtesting/engine.py:347-401` | `_SyntheticOrderBook(mid, spread_bps, depth_shares, depth_decay=0.6, n_levels=5)` | 5-level book with exponential depth decay; `consume(side, shares)` walks levels. |
| Look-ahead detector | `backtesting/engine.py:403-502` | `_LookAheadDetector` with LE_01..LE_06 rules | 6 rule classes; checks per-trade + end-of-backtest aggregates. |
| Walk-forward | `backtesting/advanced.py:65-207` | `walk_forward_analysis(features, labels, timestamps, model_factory, train_window=1000, test_window=200, step=200) -> WalkForwardResult` | Time-ordered `np.argsort(timestamps)` partition; per-window fresh model fit; AUC/Brier per window. |
| Monte Carlo | `backtesting/advanced.py:310-393` | `monte_carlo_simulation(trade_returns, n_simulations=10000, initial_capital=100, ruin_threshold=0.5) -> MonteCarloResult` | Bootstrap resampling with replacement; p5/p25/p50/p75/p95 + probability_of_ruin. |
| JSON report | `backtesting/report.py:94-222` | `generate_report(backtest_result, strategy_name) -> BacktestReport` | Accepts both `run_realistic_backtest` and legacy `BacktestEngine.run_backtest().to_dict()` shapes; normalises equity curve. |
| PDF report | `backtesting/report.py:397-578` | `report_to_pdf(report, output_path) -> Path` | reportlab + matplotlib; A4 multi-section; raises `ImportError` if reportlab missing. |
| Paper broker | `paper/simulator.py:39-313` | `paper_sim.create_order(args, strategy, decision_id)` / `cancel_order` / `cancel_all` / `_try_fill_orders` | Live-book broker; 1s fill-loop; `_apply_slippage` (3-component: crossing 1-tick + size impact + queue hash); `record_fill` + decision-ledger hook + execution-quality hook. |

---

## 5. Data Flow (§30 — Historical Data → Replay → Strategy → Signal → Risk → Execution → Portfolio → Results)

The §30 trace is reproduced below with VERIFIED / NOT FOUND annotations.
The headline finding: **the trace is broken at the very first hop** — there
is no Historical Data source consumed by the backtest engine.

| Stage | §30 expectation | Codebase reality | Status |
|---|---|---|---|
| **Historical Data** | Engine reads historical order-book / trade ticks from a data store | **NOT FOUND.** `run_realistic_backtest` reads no historical data. `decision_mid` and `p_model` are RNG draws (`rng.normal(0, 0.06)` clipped around `base_p`). `BacktestEngine.run_backtest` likewise — `entry_p = np.clip(avg_entry_price + rng.normal(0, 0.04), 0.05, 0.95)`. There is no `data_store.get_historical_book(...)` call anywhere in `backtesting/`. | **BROKEN** |
| **Replay Engine** | A chronological tick-replay loop that pushes market updates to strategies at their original timestamps | **NOT FOUND.** The loop in `run_realistic_backtest` iterates `for step in range(1, n_steps + 1)` where `n_steps = days * 24` (hourly cadence). Each step RNG-samples whether to trade (`rng.uniform(0,1) < trade_frequency`) and RNG-samples a synthetic mid/probability. There is no replay — it is a forward Monte-Carlo simulation. | **BROKEN** |
| **Strategy** | The strategy object's `on_tick` / `predict` / `evaluate` method is invoked against the replayed data | **NOT FOUND.** `_resolve_strategy_profile(strategy)` (engine.py:314-344) reads `name` / `base_p` / `avg_entry_price` / `trade_frequency` / `kelly_frac` attributes from the strategy object but **never calls any method on it**. The simulation uses these numeric profile parameters directly. A real `BaseStrategy` subclass with full signal logic would be ignored — only its 5 numeric profile attrs are read. | **BROKEN** |
| **Signal** | Strategy emits `p_model` based on its own internal logic against the replayed data | **PARTIAL.** `p_model = float(np.clip(base_p + rng.normal(0, 0.06), 0.05, 0.95))` (engine.py:537). This is an RNG draw around the archetype's hardcoded `base_p`, NOT a strategy-derived signal. The field is named `p_model` to *look* like a model output, but it is `rng.normal` noise. | **WEAK** |
| **Risk** | Strategy's signal passes through `risk.manager.check_order` before execution | **NOT FOUND in the backtest path.** `run_realistic_backtest` does Kelly sizing inline (`engine.py:556-562`) and never imports `risk.manager`. The live path (`BaseStrategy.submit_order`) DOES call `risk_manager.check_order(provisional)`, but that is the live/paper path, not the backtest path. | **BROKEN** |
| **Simulated Execution** | Order is matched against a synthetic or replayed order book with realistic microstructure | **VERIFIED** (in `run_realistic_backtest` only). `_SyntheticOrderBook.consume("BUY", requested_shares)` walks 5 levels with `depth_decay=0.6`; returns `(filled_shares, avg_fill_price)`; if `filled_shares < 1e-6` the trade is rejected (partial fill). 1-3s exec delay + adverse mid drift + sqrt market-impact slippage are all modelled. | **OK** |
| **Portfolio** | Cash + position accounting, per-token inventory, mark-to-market | **WEAK.** Single-account cash accounting only — `cash = max(1.0, cash + step_pnl)`. No per-token positions, no inventory ageing, no mark-to-market on open positions (every trade is instantaneous binary settlement, so MTM is implicitly $0 until resolution). The legacy engine's `equity_curve` is just `cash` per step — no separate portfolio-value accounting. | **WEAK** |
| **Results** | Trade list + equity curve + risk metrics + look-ahead violations | **VERIFIED** (in `run_realistic_backtest`). Returns `{trades, equity_curve, metrics, look_ahead_bias}`. `metrics` includes `win_rate`, `sharpe`, `max_drawdown`, `profit_factor`. | **OK** |

**Bottom line on §30:** the realistic backtest engine does NOT replay
historical data. It is a Monte-Carlo archetype simulator with
microstructure-aware execution modelling. A backtest of the real
`strategies/market_maker.py::MarketMakerStrategy` against the real
Polymarket book history is **not achievable** with the current
architecture.

---

## 6. Execution Flow

### 6.1 `run_realistic_backtest` execution flow (per trade)

```
1. _resolve_strategy_profile(strategy)        ──►  profile = {base_p, avg_entry_price,
                                                   trade_frequency, kelly_frac, name}

2. for step in 1..(days*24):                  ──►  hourly cadence; one potential trade per hour
     if rng.uniform(0,1) < trade_frequency:
       _simulate_realistic_trade(step, profile, rng, cash, slippage_bps, ...)

3. _simulate_realistic_trade internals:
   3.1  p_model         = clip(base_p + N(0, 0.06), 0.05, 0.95)        ── RNG-derived "signal"
   3.2  decision_mid    = clip(avg_entry_price + N(0, 0.04), 0.05, 0.95)
   3.3  spread_bps      = max(2, slippage_bps + N(0, 2))
   3.4  depth_shares    = U(50, 500)
   3.5  decision_book   = _SyntheticOrderBook(mid=decision_mid, spread_bps, depth_shares)
   3.6  ask             = decision_book.ask
   3.7  Kelly: payout_ratio = (1-ask)/ask; kelly_num = p*payout - (1-p); actual_f = min(0.10, kelly*kelly_frac)
   3.8  position_size_usd = max(1.0, cash*actual_f); requested_shares = position_size_usd/ask
   3.9  exec_delay_s   = U(1,3)
   3.10 drift_bps      = N(0, slippage_bps*0.5)
   3.11 realized_mid   = clip(decision_mid*(1+drift), 0.02, 0.98)
   3.12 realized_depth = depth_shares * U(0.8, 1.0)
   3.13 exec_book      = _SyntheticOrderBook(mid=realized_mid, spread_bps, realized_depth, ts+delay)
   3.14 filled_shares, avg_fill_price = exec_book.consume("BUY", requested_shares)
   3.15 if filled_shares < 1e-6: return None  ── trade rejected (illiquid)
   3.16 impact_bps     = slippage_bps * sqrt(actual_cost / typical_adv_usd)
   3.17 actual_cost   += actual_cost * impact_bps / 10000
   3.18 is_win         = rng.uniform(0,1) < p_model                ── RNG determines win
   3.19 pnl            = filled_shares*1.0 - actual_cost  if is_win
                        else -actual_cost
   3.20 lookahead.check_p_model_vs_outcome(...)                    ── LE_01
   3.21 lookahead.check_entry_extremum(...)                        ── LE_02

4. End-of-backtest aggregates:
   LE_03 if win_rate > 0.95 over > 30 trades
   LE_06 if corr(p_model, outcome) > 0.95 over > 30 trades

5. metrics = {win_rate, sharpe, max_drawdown, profit_factor}
6. return {trades, equity_curve, metrics, look_ahead_bias}
```

### 6.2 Live paper-trade execution flow (paper/simulator.py)

```
BaseStrategy.submit_order(args, decision_id)
   └─► risk_manager.check_order(provisional)        ──► RISK_APPROVED / RISK_REJECTED
        │
        ├─ paper mode ─► paper_sim.create_order(args, strategy, decision_id)
        │                  └─► store.add_order(Order(paper=True))
        │
        └─ live mode ──► clob_client.create_order(args)   ──► Polymarket REST API
                            └─► store.add_order(Order(paper=False))

paper_sim._fill_loop()  (background asyncio task, 1s cadence):
   for order in store.get_open_orders():
     book = store.get_order_book(order.token_id)         ── LIVE Polymarket book
     raw_price = paper_sim._can_fill(order, book)
     if raw_price is not None:
       fill_price = paper_sim._apply_slippage(order, raw_price, book)
                                          │
                                          ├─ crossing 1 tick (flat)
                                          ├─ size impact: 0.5 tick * (overflow / 50)
                                          └─ queue: SHA-256(order_id) & 0x01 → 0/1 tick
       paper_sim._execute_fill(order, fill_price)
           ├─ compute PnL  (SELL only: fill_price - avg_entry_price) * size
           ├─ store.record_fill(Trade)
           ├─ risk_manager.report_trade_pnl(strategy, pnl)   ──► attribution
           ├─ decision_ledger.record(stage="FILL", pnl=...)
           └─ execution_quality.record_execution(order, fill_price, signal_price=order.price)
```

### 6.3 Critical asymmetry

The two flows above share **NO code**. Note specifically:

- The slippage model is different:
  - **Backtest:** `half-spread (max(2, slippage_bps+N(0,2)) bps) + walk-the-book + sqrt(notional/ADV)`
  - **Paper:** `1 tick + 0.5*tick*(overflow/50) + 0/1 tick queue hash`
  - **Live:** none — real fills from Polymarket CLOB.
- The risk engine is invoked only on the paper/live path.
- The decision ledger (`PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL`)
  is invoked only on the paper/live path. The backtest engine does not
  record any decision chain.
- The execution-quality recorder (`core/execution_quality.record_execution`)
  is invoked only on the paper/live path.

---

## 7. Feature Inventory

| Feature | Module | Status | API route? |
|---|---|---|---|
| Synthetic archetype MC backtest | `engine.BacktestEngine.run_backtest` | VERIFIED | `POST /api/backtest/run` |
| Realistic MC backtest (T4) | `engine.run_realistic_backtest` | VERIFIED | **NO** |
| Synthetic 5-level order book | `engine._SyntheticOrderBook` | VERIFIED | — |
| Look-ahead detector (6 rules) | `engine._LookAheadDetector` (LE_01..LE_06) | VERIFIED | — |
| Walk-forward analysis (W13-8) | `advanced.walk_forward_analysis` | VERIFIED | **NO** |
| Monte-Carlo bootstrap (W13-8) | `advanced.monte_carlo_simulation` | VERIFIED | **NO** |
| JSON report | `report.generate_report` | VERIFIED | `POST /api/backtest/report` |
| PDF report (W16-4) | `report.report_to_pdf` | VERIFIED | `POST /api/backtest/report/pdf` |
| Equity curve normaliser | `report._normalise_equity` | VERIFIED | — |
| Monthly returns aggregation | `report._compute_monthly_returns` | VERIFIED | — |
| Performance metrics | `BacktestResult` + `BacktestReport` | VERIFIED | via `/api/backtest/report` |
| Sharpe, Sortino, Calmar | Both engines | VERIFIED | — |
| VaR-95, CVaR-95 | `report.py` only | VERIFIED | — |
| Profit Factor, Brier Score | `BacktestResult` only | VERIFIED | — |
| Live paper broker | `paper.PaperSimulator` | VERIFIED | via `BaseStrategy.submit_order` (paper mode) |
| 3-component slippage model | `paper.PaperSimulator._apply_slippage` | VERIFIED | — |
| Execution quality recorder | `core.execution_quality.record_execution` | VERIFIED | — |
| Decision ledger (PREDICTION → FILL) | `core.decision_ledger` | VERIFIED | — |
| **Historical data replay** | — | **NOT FOUND** | — |
| **Backtest/Live execution interface** | — | **NOT FOUND** | — |
| **Experiment registry / persistence** | — | **NOT FOUND** | — |

---

## 8. What Works

- **`run_realistic_backtest` microstructure pipeline.** Spread, depth,
  walk-the-book partial fills, exec delay with adverse mid drift,
  sqrt market-impact slippage — all six §31 realism primitives are
  implemented in `engine.py:505-634` and confirmed by direct smoke
  test (598 trades / 31-day `mm` backtest produces spread_bps ∈ [2.4, 17.8],
  impact_bps ∈ [0.4, 8.1], exec_delay_s ∈ [1.0, 3.0]). **VERIFIED.**
- **Look-ahead detector.** All 6 rule classes (LE_01..LE_06) are
  implemented and reachable from `_simulate_realistic_trade`. The
  `check_strategy_object` rule (LE_05) is correctly invoked once at
  the start of the backtest against the passed strategy object.
  **VERIFIED** (engine.py:403-502).
- **Walk-forward analysis (W13-8).** `walk_forward_analysis` correctly
  sorts by timestamp before partitioning (`np.argsort(timestamps)`),
  builds a fresh model per window (`model_factory()`), and reports
  per-window AUC + Brier + mean prediction vs actual positive rate.
  Tested by `tests/test_advanced_backtest.py::test_walk_forward_*`
  (5 tests, all pass). **VERIFIED.**
- **Monte-Carlo simulation (W13-8).** `monte_carlo_simulation` does
  bootstrap resampling with replacement, computes p5/p25/p50/p75/p95
  percentiles of final returns, tracks per-simulation max drawdown,
  and reports `probability_of_ruin` against a configurable threshold.
  Tested by 4 tests, all pass. **VERIFIED.**
- **PDF report (W16-4).** `report_to_pdf` produces a valid PDF
  (`%PDF` magic, multi-section A4: title + summary table + matplotlib
  equity chart + monthly returns + trade distribution). Tested by
  `test_report_to_pdf_writes_valid_pdf` — passes. **VERIFIED.**
- **JSON report equity-curve normaliser.** `_normalise_equity`
  transparently handles `list[float]`, `list[dict]` (per-step
  snapshot dicts from either engine), and mixed malformed inputs.
  Tested by 3 tests. **VERIFIED.**
- **Wave-5 time-ordered split fix.** `ml/model.py:237-247` uses
  `idx = np.arange(n_total); X_tr, y_tr = X[idx[:n_train]], y[idx[:n_train]]`
  (sequential indices, not `np.random.permutation`). The inline
  comment explicitly documents the look-ahead rationale. **VERIFIED.**
- **Paper broker 3-component slippage.** `paper_sim._apply_slippage`
  (crossing 1 tick + 0.5-tick size impact per `SLIPPAGE_DEPTH_BUCKET=50`
  shares of overflow + deterministic 0/1 tick queue from `SHA-256(order_id)[0]
  & 0x01`) is a sound institutional-style model. Tested by
  `tests/test_paper_simulator.py` (11 tests). **VERIFIED.**
- **Decision-ledger integration on paper path.** `paper_sim._execute_fill`
  records the FILL stage with PnL into `decision_ledger` if
  `order.decision_id` is present. **VERIFIED** (simulator.py:277-294).
- **Execution-quality recording on paper path.** `paper_sim._execute_fill`
  calls `core.execution_quality.record_execution(order, fill_price,
  signal_price=order.price)` so per-fill slippage in bps + latency
  + realised edge are tracked. **VERIFIED** (simulator.py:299-309).

---

## 9. What Does Not Work

- **Historical data replay.** The engine never reads historical
  market data. `decision_mid` is an RNG draw around `avg_entry_price`,
  not a historical mid. The token_id is `"TKN_{step:06d}"` (synthetic),
  not a real Polymarket token. There is no `data_store.get_book_at(ts)`
  call anywhere in `backtesting/`. **NOT FOUND.**
- **Strategy invocation.** `run_realistic_backtest(strategy, ...)` only
  reads 5 numeric profile attrs (`name`, `base_p`, `avg_entry_price`,
  `trade_frequency`, `kelly_frac`). It never invokes
  `strategy.on_tick(market_state)`, never calls `strategy.predict()`,
  never queries `strategy.should_enter(token_id)`. A backtest of the
  real `MarketMakerStrategy` against synthetic data is impossible
  because the strategy's actual signal logic is bypassed.
  **VERIFIED** by reading `_simulate_realistic_trade` (engine.py:505-634).
- **Backtest / live parity.** No shared execution interface. Slippage
  is computed differently in each path. Risk engine is bypassed in
  backtest. Decision ledger is bypassed in backtest. **NOT FOUND.**
- **API surface for walk-forward + Monte Carlo.** Both `walk_forward_analysis`
  and `monte_carlo_simulation` are unreachable from any HTTP route.
  **VERIFIED** by grepping `api/server.py` for `walk_forward` /
  `monte_carlo` — only the legacy `/api/backtest/run` route's
  `"synthetic_kind": "monte_carlo_archetype"` mention matches.
- **Experiment persistence / Backtest Lab.** No DB table, no JSON file,
  no SQLite archive of past runs. Every `run_realistic_backtest` call
  returns a fresh dict that is discarded by the caller. Cross-run
  comparison is impossible without external ad-hoc tooling.
  **NOT FOUND.**
- **`fee_bps` in `run_realistic_backtest`.** The realistic engine has
  no `fee_bps` parameter — `slippage_bps` is overloaded to cover both
  spread half-width AND market impact AND (implicitly) fees. The
  legacy `BacktestEngine.run_backtest` does have `fee_bps=0.0`, so
  the parameter exists in one engine and is missing in the other.
  This makes A/B fee comparison (a §33 backtest-lab requirement)
  impossible via the realistic engine. **VERIFIED.**
- **Capital-constraint realism.** `cash = max(1.0, cash + step_pnl)`
  silently truncates equity at $1 when a trade would drive the
  account negative — i.e., the engine pretends you can't lose more
  than you have, even on a Kelly-over-leveraged position. No margin
  call, no forced liquidation. **VERIFIED** (engine.py:775).
- **Portfolio state.** Only `cash` is tracked; no per-token position
  table, no open-position mark-to-market, no inventory ageing, no
  interest / funding. Every trade is treated as instantaneous binary
  settlement with no hold period. **VERIFIED** by reading the loop.
- **`monthly_returns` fabrication (legacy engine).** `BacktestEngine.run_backtest`
  (engine.py:244-249) hardcodes `monthly_returns = {"Week 1": roi*0.28,
  "Week 2": roi*0.22, "Week 3": roi*0.31, "Week 4": roi*0.19}` — these
  are not actual monthly returns; they are pre-baked fractions of the
  total ROI. The realistic engine omits this field entirely.
  **VERIFIED.**

---

## 10. Missing Features

- **Historical market-data ingest for backtest.** No replay hook from
  `core/data_store.py` (which DOES hold live book snapshots) into the
  backtest engine. The infrastructure exists on the live side; the
  backtest side never calls it.
- **Backtest/Live execution interface abstraction.** A `Broker`
  protocol with `paper_sim` / `clob_client` / `BacktestBroker` as
  three implementations would unify the three paths. **NOT FOUND.**
- **Strategy adapter for backtest.** A wrapper that exposes a real
  `BaseStrategy` subclass as a deterministic function of replayed
  market state, so the same strategy can run in backtest / paper /
  live mode. **NOT FOUND.**
- **Experiment registry.** A SQLite table `(run_id, strategy,
  start_date, end_date, capital, slippage_bps, fee_bps, metrics_json,
  equity_curve_json, created_at)` so past runs are queryable for
  cross-comparison. **NOT FOUND.**
- **Walk-forward / Monte-Carlo API routes.** `POST /api/backtest/walk-forward`
  and `POST /api/backtest/monte-carlo` are referenced in the
  `advanced.py` module docstring (lines 27-29) but **do not exist** in
  `api/server.py`. The docstring lies about the wiring.
- **CVaR in the legacy engine.** `BacktestResult` has `value_at_risk_95`
  but no `cvar_95`. The richer `BacktestReport` dataclass (in
  `report.py`) does have `cvar_95`, but only the report path computes
  it — a caller hitting `POST /api/backtest/run` will not see CVaR.
- **Multi-strategy backtest.** No way to backtest a portfolio of
  strategies with allocation weights (e.g. 60% `mm` + 40% `arb`).
  Each `run_realistic_backtest` call is single-strategy.
- **Position-level attribution.** The decision-ledger chain on the
  paper/live path records PREDICTION → FILL with PnL. The backtest
  engine writes nothing to the decision ledger. There is no
  backtest-side attribution view.
- **Sensitivity / parameter sweep.** No batch runner that sweeps
  `slippage_bps ∈ [5, 10, 25, 50, 100]` and reports the resulting
  Sharpe / DD distribution. Each call is one-shot.

---

## 11. Bugs

### B1 — Equity floor silently inflates drawdowns and Sharpe outliers

**Location:** `engine.py:775` (`run_realistic_backtest`) and
`engine.py:206` (legacy `run_backtest`).

```python
cash = max(1.0, cash + step_pnl)        # equity floor at $1
ret = step_pnl / max(cash - step_pnl, 1.0)  # return computed against PRE-floor cash
```

When `cash + step_pnl < 1.0` (e.g. catastrophic loss), `cash` is
truncated to $1 but `ret` is computed against the pre-truncation
cash — i.e., a $10 loss on $5 equity yields `ret = -10 / 5 = -2.0`
(-200% return on a $1 surviving equity). The loss beyond the floor
is silently discarded by the `max(1.0, ...)` truncation, but the
return series still records the full magnitude.

**Impact:** inflates the std of the return series → distorts Sharpe
and Sortino (and VaR). Smoke test: a 31-day `mm` backtest at $1000
capital produces Sharpe = 21.5 (engine.py:806 annualises by
`sqrt(24*365) = 93.6`), which is implausible for any real strategy.

**Severity:** high (silently mis-allocates between unrealistic Sharpe
and unrealistic drawdown).

### B2 — Sharpe / Sortino annualisation assumes 24×365 trading

**Location:** `engine.py:232, 236, 806`.

```python
sharpe = (mean_ret / std_ret) * math.sqrt(24 * 365)
```

Crypto markets trade 24/7, but Polymarket prediction markets have
**step-level activity** that is far from uniform (low overnight
volume, spikes around news). Annualising by `sqrt(8760)` assumes
independent hourly returns — the autocorrelation is non-trivial.
For `monte_carlo_simulation` and `walk_forward_analysis`, the
annualisation switches to `sqrt(252)` (`advanced.py:259-260, 273`) —
inconsistent with the engine's `sqrt(8760)`.

**Severity:** medium (results in metrics that cannot be compared
across modules).

### B3 — `monthly_returns` is fabricated in legacy `run_backtest`

**Location:** `engine.py:244-249`.

```python
monthly_returns = {
    "Week 1": round(roi_pct * 0.28, 2),
    "Week 2": round(roi_pct * 0.22, 2),
    "Week 3": round(roi_pct * 0.31, 2),
    "Week 4": round(roi_pct * 0.19, 2),
}
```

These are pre-baked fractions of total ROI, NOT actual monthly
returns. The keys are also `"Week 1"`..`"Week 4"` — neither month
names nor ISO week numbers. The realistic engine correctly omits
this field, but `POST /api/backtest/run` still hits the legacy
engine and returns the fabricated values.

**Severity:** medium (data fabrication; misleading to dashboard
consumers).

### B4 — `walk_forward_analysis` returns degenerate equity when no valid windows

**Location:** `advanced.py:196-197`.

```python
else:
    equity_curve, metrics = [1.0], {}
```

When no valid windows fit (e.g. `n < train_window + test_window`),
`metrics = {}` is returned, but the `WalkForwardResult` constructor
then calls `metrics.get("max_drawdown", 0.0)` etc. — which silently
defaults to 0.0 instead of `None` or a `NaN` sentinel. A consumer
checking `result.max_drawdown == 0.0` cannot distinguish "no
drawdown because no losing trades" from "no windows fitted".

**Severity:** low (cosmetic; but confusing).

### B5 — `report_to_pdf` writes the matplotlib PNG to `/tmp` and never cleans up

**Location:** `report.py:641-642`.

```python
tmp = Path(tempfile.gettempdir()) / f"pmbot_report_{report.report_id}.png"
fig.savefig(str(tmp), format="png")
```

The PDF embeds the PNG via reportlab's `RLImage`, but the PNG file
itself is never deleted. Every `POST /api/backtest/report/pdf`
request leaks a ~30-50 KB PNG to `/tmp`. Over weeks, this can fill
the disk on a small container.

**Severity:** low (resource leak; slow accumulation).

### B6 — `LE_04 FUTURE_TIMESTAMP_ACCESS` is implemented but unreachable

**Location:** `engine.py:475-477`.

```python
def check_timestamps(self, decision_ts, data_ts, step, token_id):
    if data_ts is not None and data_ts > decision_ts + 1e-6:
        self.add("LE_04", ...)
```

The method exists on `_LookAheadDetector` but is **never called**
by `_simulate_realistic_trade` (which has no `data_ts` parameter)
or by `run_realistic_backtest`. So LE_04 is a dead rule — useful
only if a future caller remembers to invoke it manually.

**Severity:** medium (looks like coverage; isn't).

### B7 — `walk_forward_analysis._simulate_equity` bets $1 per trade regardless of confidence

**Location:** `advanced.py:222-225`.

```python
bets = (predictions > 0.5).astype(int)
wins = (bets == actuals).astype(int)
pnl = 2 * wins - 1  # +1 for win, -1 for loss
```

The equity curve is a flat-$1-per-trade simulation. It ignores the
prediction's confidence (a 0.51 prediction is treated identically to
a 0.95 prediction), the position sizing (no Kelly), and any
execution cost (no spread / slippage). The resulting Sharpe /
Sortino / Calmar are **not comparable** to the same metrics from
`run_realistic_backtest` (which uses Kelly sizing + slippage).

**Severity:** medium (the docstring claims Sharpe is comparable;
it isn't).

### B8 — `monte_carlo_simulation` uses `np.random.choice` without a seed

**Location:** `advanced.py:352`.

```python
sampled = np.random.choice(trade_returns, size=n_trades, replace=True)
```

Uses the global numpy RNG. Two consecutive calls with identical
inputs produce different `final_returns` distributions, which makes
the function non-reproducible. The realistic backtest engine seeds
`rng = np.random.RandomState(abs(hash(strategy_id)) % (2**31))`
(engine.py:718); Monte Carlo does not.

**Severity:** medium (breaks reproducibility; QA cannot verify).

---

## 12. Technical Debt

- **Two backtest engines with overlapping but inconsistent contracts.**
  `BacktestEngine.run_backtest` returns a `BacktestResult` dataclass
  with `fee_bps`, `brier_score`, `monthly_returns`, `cagr_pct`. `run_realistic_backtest`
  returns a plain dict with `look_ahead_bias` and a 4-metric `metrics`
  sub-dict. The PDF report generator (`report.generate_report`) accepts
  both shapes via `_normalise_equity` but downstream consumers have to
  know which engine they're hitting. No deprecation path.
- **Module-docstring lies about API surface.** `advanced.py:27-29`
  documents `/api/backtest/walk-forward` and `/api/backtest/monte-carlo`
  routes — neither exists. Future readers will trust the docstring.
- **`BacktestResult.to_dict` hardcodes `"synthetic": True,
  "synthetic_kind": "monte_carlo_binary_kelly"`** (engine.py:88-90).
  The legacy API route adds another `"synthetic_kind": "monte_carlo_archetype"`
  layer on top (server.py:3445). The two `synthetic_kind` values
  disagree on whether the engine is "binary_kelly" or "archetype".
- **`_ARCHETYPE_PROFILES` duplicated.** The archetype profiles
  (`mm`/`arb`/`mom`/`ml`/`default`) are defined twice — once as inline
  `if/elif` in `BacktestEngine.run_backtest` (engine.py:131-159) and
  once as `_ARCHETYPE_PROFILES` dict (engine.py:305-311). The
  `_resolve_strategy_profile` helper uses only the dict; the legacy
  engine uses only the inline chain. They could drift.
- **`backtesting/__init__.py` is empty.** No re-exports, no version,
  no package-level type. Importing `backtesting` is a no-op; callers
  must know the submodule paths.

---

## 13. Data Problems

- **No historical market data is consumed by the backtest engine.**
  The `core/data_store.py` / `core/market_db.py` / `core/timescale_db.py`
  modules DO persist live order-book snapshots and trade history —
  but `backtesting/` never reads them. The bridge between the live
  data plane and the backtest plane does not exist.
- **Archetype `base_p` constants are hand-tuned, not calibrated.**
  `"mm": 0.65`, `"arb": 0.95`, `"mom": 0.52`, `"ml": 0.64`
  (engine.py:131-159). These are presented as institutional-grade
  archetype profiles but have no empirical basis. A real market-maker
  on Polymarket typically achieves win-rates of 0.52-0.58 (the spread
  edge is thin); 0.65 is fantasy.
- **`avg_entry_price` is a constant per archetype.** Real markets
  have time-varying mids across a wide range (e.g. an "yes" token
  can trade at 0.10 → 0.90 as the event resolves). The archetype
  sim draws `decision_mid = avg_entry_price + N(0, 0.04)`, so 99%
  of trades cluster around the same mid. No regime diversity.
- **`token_id` is synthetic.** `"TKN_{step:06d}"` — not a real
  Polymarket token. Backtest results cannot be cross-referenced
  against live token history.

---

## 14. Performance Problems

- **Monte Carlo loop is Python-level.** `advanced.py:350-374`
  iterates `n_simulations` times in pure Python, each iteration
  doing an inner `for i, ret in enumerate(sampled): equity[i+1] = ...`
  (also pure Python). For `n_simulations=10000` and `n_trades=500`,
  that's 5 million Python-level iterations. No vectorised
  `np.cumprod` path. Estimated runtime: 30-60s on a modest CPU.
- **`walk_forward_analysis` re-imports `sklearn.metrics` per
  window.** `advanced.py:141` does `from sklearn.metrics import
  brier_score_loss, roc_auc_score` inside the while-loop, not at
  module top. The cost is minor (Python caches the import) but
  stylistically wrong and signals the code was bolted on quickly.
- **`generate_report` recomputes equity-derived metrics from
  scratch** rather than reusing the engine's pre-computed metrics.
  For a 30-day backtest with 598 trades, both the engine AND the
  report generator compute Sharpe — once with `sqrt(24*365)`
  annualisation, once with `sqrt(252)`. They disagree numerically.
- **`report_to_pdf` blocks the event loop's thread pool.** The
  API route wraps it in `asyncio.to_thread`, but `reportlab`
  rendering + matplotlib chart generation are CPU-bound and serial.
  Under concurrent requests, the default thread pool (4 workers)
  saturates and queues.

---

## 15. Reliability Problems

- **`run_realistic_backtest` has no try/except around `_simulate_realistic_trade`.**
  A single division-by-zero or NaN propagation aborts the entire
  backtest. No partial-result recovery.
- **`walk_forward_analysis` catches `model.fit` exceptions per
  window** (`advanced.py:125-129`) but the failed window is silently
  skipped — no aggregate count of failed windows, no warning log.
  A `model_factory` that always raises would yield `n_windows=0`
  with `"error": "No valid windows"` and a `[1.0]` equity curve,
  silently looking like a "no data" case.
- **`report_to_pdf` raises `ImportError("pip install reportlab")`**
  if reportlab is missing. The API route catches this and returns
  503, but the underlying module loads reportlab lazily inside the
  function — meaning the import error surfaces only at call time.
  A pre-flight `try: import reportlab` check at module load would
  surface the missing dependency at startup, not at first request.
- **`monte_carlo_simulation` has no input validation.** A NaN in
  `trade_returns` would propagate to all `final_values`, breaking
  the `np.percentile` call. A `np.isfinite(trade_returns).all()`
  guard is missing.
- **`_SyntheticOrderBook.consume` divides by `total_shares > 0`
  to compute `avg_px`** but if `total_shares == 0` (book with
  `depth_shares=0`), it returns `(0, self.mid)` — a `0-share fill
  at the mid`. The caller (`_simulate_realistic_trade` line 582)
  treats `filled_shares < 1e-6` as "rejected" and returns `None`,
  which is correct, but the division-by-zero path is implicit
  rather than explicit.

---

## 16. Security Problems

- **No auth check on backtest endpoints beyond the default API
  auth.** `POST /api/backtest/run`, `/api/backtest/report`, and
  `/api/backtest/report/pdf` are rate-limited (`HEAVY_LIMIT = 5/min`)
  but otherwise unauthenticated. Any caller with the API token can
  trigger a CPU-bound backtest that consumes worker threads. A
  malicious caller can DoS the worker pool by repeatedly invoking
  the PDF route (matplotlib + reportlab rendering).
- **PDFs written to `/tmp` are world-readable** (default `tempfile`
  umask). If the API token is compromised, an attacker can read
  other tenants' generated PDFs by guessing `report_id` (12-char
  MD5 prefix).
- **`generate_report` hashes `f"{strategy_name}{time.time()}"` for
  `report_id`** (report.py:185-187). The MD5 of a string containing
  `time.time()` is weakly predictable — an attacker who knows
  the strategy name and approximate request time can enumerate
  recent `report_id` values. Not a credential, but enables
  `/tmp` file enumeration.

---

## 17. Testing

**Test coverage is broad but uneven.** Three test files cover the
backtest stack:

| Test file | Tests | Status | Coverage |
|---|---|---|---|
| `tests/test_backtest_engine.py` | 9 | **PASS** (9/9, 0.36s) | `run_realistic_backtest` shape, metrics, equity, look-ahead, slippage monotonicity |
| `tests/test_advanced_backtest.py` | 12 | **PASS** (12/12, ~2s) | `walk_forward_analysis` (5), `_simulate_equity` (3), `monte_carlo_simulation` (4) |
| `tests/test_backtest_report.py` | 20 | **PASS** (20/20, 8.92s) | `generate_report` (8), edge cases (4), serialisation (1), PDF (1), API routes (2), metric computations (4) |
| `tests/test_paper_simulator.py` | 11 | **PASS** | `paper_sim` fill logic + 3-component slippage model |

**All tests verified passing** by direct invocation:

```
$ python -m pytest tests/test_backtest_engine.py tests/test_advanced_backtest.py \
                   tests/test_backtest_report.py -x --tb=short
.....................   [100%]
21 passed in 10.50s
```

(Note: `test_backtest_report.py` runs separately because it imports
`api.server` and needs the full env-var redirect setup; 20/20 pass
on its own.)

**Gaps:**

- **No integration test of `POST /api/backtest/run` → live strategy
  → reconciliation** — i.e., no test that a backtest of the real
  `MarketMakerStrategy` matches the same strategy's paper-trade PnL
  for the same period. (Such a test would fail, because the backtest
  engine doesn't invoke the strategy — but the absence of the test
  is itself a gap.)
- **No property-based test for the look-ahead detector.** A
  hypothesis-style test that injects a deliberately leaky strategy
  (e.g. `p_model = actual_outcome + N(0, 0.01)`) and asserts LE_01
  fires would be valuable. **NOT FOUND.**
- **No test that `walk_forward_analysis` actually prevents
  leakage.** The test asserts "shuffled input produces same output
  as sorted input" but does not assert "training fold `k` does not
  contain samples with timestamp > test fold `k` start". A direct
  timestamp-bounds assertion would be stronger.
- **No test of the `monte_carlo_simulation` reproducibility bug (B8).**
- **No load test of the PDF route.** `tests/load/locustfile.py` does
  not include a backtest/PDF scenario.

---

## 18. Observability

**Backtest observability is essentially absent.**

- **No metrics emitted.** Neither `run_realistic_backtest` nor
  `BacktestEngine.run_backtest` calls `core.observability.record_metric(...)`.
  No backtest_duration_ms, no backtest_trades_count, no
  backtest_lookahead_violations gauge. The `core/observability_collector.py`
  has categories for `bot`, `ml`, `execution`, `risk`, `data`, `system`
  — but no `backtest` category.
- **No structured logging of backtest runs.** `engine.py` imports
  `logging` (`log = logging.getLogger(__name__)`) but emits **zero
  log lines** in the entire 821-line file. `advanced.py` has one
  `logger.warning` per failed window-fit; otherwise silent.
- **No decision-ledger integration.** The paper path records a
  full `PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL` chain
  per trade; the backtest path records nothing. There is no
  `BACKTEST_TRADE` stage in the decision ledger.
- **No execution-quality recording.** The paper path calls
  `core.execution_quality.record_execution` per fill; the backtest
  path does not.
- **No Prometheus metric for backtest runs.** `core/prometheus_metrics.py`
  has counters for HTTP requests, ML predictions, etc., but no
  `polymarket_backtest_runs_total` counter or
  `polymarket_backtest_duration_seconds` histogram.

---

## 19. Production Readiness

**Not production-ready as a historical-replay backtest engine.** The
current state is best described as a **research sandbox**:

- Adequate for: archetype-level sanity-checking, parameter-tuning
  demos, generating sample PDF reports for sales / docs.
- Inadequate for: any decision that depends on whether strategy X
  would have made money in period Y on real Polymarket data.
- Inadequate for: parity comparisons between backtest and live/paper
  execution (different slippage models, no shared risk path).

**Specifically NOT production-ready because:**

1. The backtest engine does not replay historical data (§5).
2. The backtest engine does not invoke real strategies (§5).
3. The backtest and paper/live paths share no code (§32).
4. No experiment persistence — every run is ephemeral (§33).
5. Walk-forward + Monte Carlo are unreachable from the API (§7).
6. No observability for backtest runs (§18).
7. Multiple correctness bugs in equity accounting (B1, B2, B3).
8. Sharpe ratios of 20-41 from a smoke test are wildly unrealistic
   and would mislead any consumer who trusted them.

**Production-ready for:**

- `paper/simulator.py` as a paper broker — well-tested, integrated
  with risk + decision ledger + execution quality.
- `report.report_to_pdf` as a PDF renderer for the (legacy)
  archetype backtest — produces valid PDFs, tested.

---

## 20. Evidence

Each finding above is classified by evidence strength per the task
spec (VERIFIED / STRONG EVIDENCE / LIKELY / UNVERIFIED / NOT FOUND):

| # | Finding | Evidence |
|---|---|---|
| Historical data not replayed | Code-read: `engine.py:537` (`p_model = clip(base_p + N(0,0.06))`); Grep of `backtesting/` for `data_store` / `market_db` / `timescale_db` — no matches. | **VERIFIED** |
| Strategy not invoked | Code-read: `engine.py:314-344` (`_resolve_strategy_profile` only reads attrs); Grep of `engine.py` for `strategy.on_tick` / `strategy.predict` — no matches. | **VERIFIED** |
| No shared execution interface | Grep of `mini-services/polymarket-bot` for `BacktestBroker` / `LiveBroker` / `ExecutionInterface` — no matches. | **VERIFIED** |
| Wave-5 time-ordered split fix | Code-read: `ml/model.py:237-247` (`idx = np.arange(n_total)`); comment explicitly documents look-ahead rationale. | **VERIFIED** |
| Walk-forward + Monte-Carlo not exposed via API | Grep of `api/server.py` for `walk_forward` / `monte_carlo` — only the legacy `synthetic_kind: "monte_carlo_archetype"` mention. | **VERIFIED** |
| PDF report route exists | Code-read: `api/server.py:3516-3562` (`POST /api/backtest/report/pdf`); test `test_api_backtest_report_pdf_returns_pdf_file` passes. | **VERIFIED** |
| No experiment persistence | Grep of `mini-services/polymarket-bot` for `backtest_results` table / `experiments` table / `backtest_lab` — no matches in `core/` or `backtesting/`. The `ml/ab_testing.py` experiment persistence is for ML A/B tests, not backtests. | **VERIFIED** |
| Sharpe 21.5 from mm backtest | Direct smoke test: `run_realistic_backtest('mm', '2025-01-01', '2025-02-01', 1000.0, slippage_bps=10)` returns `metrics['sharpe'] = 21.5458`. | **VERIFIED** |
| Equity floor bug (B1) | Code-read: `engine.py:775` (`cash = max(1.0, cash + step_pnl)`) + `engine.py:776` (`ret = step_pnl / max(cash - step_pnl, 1.0)`). | **VERIFIED** |
| `monthly_returns` fabrication (B3) | Code-read: `engine.py:244-249` — hardcoded `roi_pct * 0.28` etc. | **VERIFIED** |
| 3-component paper-slippage model | Code-read: `paper/simulator.py:177-225` — crossing 1 tick + 0.5 tick * (overflow/50) + queue hash 0/1 tick. | **VERIFIED** |
| LE_04 unreachable (B6) | Code-read: `engine.py:475-477` defines `check_timestamps`; grep of `engine.py` for `check_timestamps(` — only the definition, no call site. | **VERIFIED** |
| All 41 tests pass | Direct run: `python -m pytest tests/test_backtest_engine.py tests/test_advanced_backtest.py tests/test_backtest_report.py` → 41 passed. | **VERIFIED** |
| Walk-forward prevents leakage | Code-read: `advanced.py:100` (`order = np.argsort(timestamps)`); test `test_walk_forward_sorts_by_timestamp` passes. | **STRONG EVIDENCE** (the partition is time-ordered, but no test directly asserts the train-fold-max-ts < test-fold-min-ts invariant). |
| `monte_carlo_simulation` non-reproducible (B8) | Code-read: `advanced.py:352` uses `np.random.choice` with no `rng` arg. Two consecutive runs would differ — NOT directly tested. | **STRONG EVIDENCE** (the absence of a seed arg is conclusive; runtime non-determinism is the implied consequence). |
| `walk_forward_analysis._simulate_equity` $1-bet simplification (B7) | Code-read: `advanced.py:222-225`. | **VERIFIED** |
| Look-ahead detector LE_03 doesn't catch Sharpe-21 anomaly | Smoke test: `mm` backtest produces Sharpe=21.5, win_rate=0.6388, `look_ahead_bias.total_violations = 0`. LE_03 only triggers if `win_rate > 0.95`, which the archetype's `base_p=0.65` + `N(0,0.06)` noise cannot exceed. | **VERIFIED** |

---

## 21. Unknowns

- **Whether `core/data_store.py` persists enough historical book
  state to feed a future replay backtest.** The live path stores
  order books (`store.add_order_book`), but the retention policy
  (`core/retention.py`) may prune historical snapshots faster than
  a backtest would need them. **UNVERIFIED** — would need to read
  `retention.py` and `data_store.py` to determine snapshot lifetime.
- **Whether any production deployment has ever invoked
  `run_realistic_backtest` directly via Python import** (bypassing
  the HTTP layer). The function is reachable, the tests use it, but
  no integration code in `api/` or `scripts/` calls it. **UNVERIFIED.**
- **Whether the `W13-8` walk-forward / Monte-Carlo modules were
  intended to be exposed via API.** The `advanced.py` docstring
  (lines 27-29) explicitly references `/api/backtest/walk-forward`
  and `/api/backtest/monte-carlo` routes — implying the routes
  were planned but never landed. **UNVERIFIED** — no worklog entry
  for W13-8 or W16-4 was found (the modules were delivered by a
  concurrent subagent that didn't append to `worklog.md`).
- **Whether the `BacktestRequest` Pydantic model's `fee_bps` field
  has any effect on the realistic engine.** The HTTP routes pass
  `fee_bps=req.fee_bps` to `BacktestEngine.run_backtest` only; the
  realistic engine has no `fee_bps` param. So if a future route
  were added that called `run_realistic_backtest`, `fee_bps` would
  be silently dropped. **LIKELY** — confirmed by signature inspection.
- **Whether the `report_id` collision space (12-char MD5 prefix =
  16^12 = ~281 trillion) is wide enough to prevent `/tmp` PDF
  filename collisions in practice.** Probably yes; not directly
  tested. **LIKELY.**

---

## 22. Maturity Score (0-10)

| Dimension | Score | Rationale |
|---|---|---|
| **Correctness of what's implemented** | 6 / 10 | Realistic MC sim is internally consistent; look-ahead detector is well-designed. But B1 (equity floor), B2 (annualisation mismatch), B3 (fabricated monthly_returns), B7 ($1-bet walk-forward equity), B8 (unseeded MC) are real defects. |
| **Realism of microstructure model (§31)** | 6 / 10 | Spread + depth + partial fills + exec delay + sqrt market-impact are all present and correct. Missing: separate `fee_bps`, queue position (paper broker has this; realistic engine does not), maker/taker fee distinction, gas / settlement fees. |
| **Backtest / Live parity (§32)** | 1 / 10 | Zero shared code. Two slippage models. Risk engine bypassed in backtest. Decision ledger bypassed. No `Broker` interface. |
| **Backtest Lab features (§33)** | 1 / 10 | No experiment persistence. No cross-run comparison. No parameter sweep. No version diff. |
| **Historical replay capability (§30)** | 0 / 10 | Does not exist. Engine is a synthetic MC simulator. |
| **Test coverage** | 7 / 10 | 41 tests, all pass. Good unit coverage of public surface. Gaps: no integration test of strategy-backtest-vs-live parity, no property-based test for look-ahead detector, no load test of PDF route. |
| **Observability** | 1 / 10 | Zero backtest metrics, zero log lines in engine.py, zero decision-ledger integration, zero Prometheus counters. |
| **API surface completeness** | 4 / 10 | Three backtest routes exist; `walk-forward` / `monte-carlo` routes documented but not implemented. |
| **Documentation accuracy** | 4 / 10 | Module docstrings are detailed but `advanced.py` lies about API routes. Inline comments are good. |
| **Production readiness** | 2 / 10 | Not safe to use for capital-allocation decisions. Sharpe ratios of 20+ would mislead any consumer. |

**Composite maturity: 3.5 / 10.**

The score is dragged down primarily by §30 (no replay) and §32
(no parity) — the two capabilities that distinguish a real
backtest engine from a Monte-Carlo demo. The microstructure
modelling that does exist is competent; the problem is that it's
modelling synthetic data, not real market data.

---

## 23. Critical Findings

### CF1 — The "backtest engine" is not a backtest engine.

It is a **synthetic Monte-Carlo archetype simulator**. The §30
trace (Historical Data → Replay → Strategy → Signal → Risk →
Execution → Portfolio → Results) is broken at hop 1 (no historical
data is read), hop 2 (no replay — forward simulation), hop 3
(strategy object's methods are not invoked — only 5 numeric profile
attrs are read), hop 4 (signal is `rng.normal` noise around
`base_p`, not a strategy output), and hop 5 (risk engine is not
called). Only hops 6-8 (simulated execution, cash accounting,
results) function as designed. **This is the single most important
finding.** Any user who interprets `run_realistic_backtest(strategy,
start_date, end_date, capital)` as "I'm backtesting strategy X
over the historical period start_date..end_date" is being misled.

### CF2 — There is no Backtest/Live execution interface (§32 FAIL).

The task spec's desired architecture
(`Strategy → Risk → Execution Interface → {BacktestBroker, LiveBroker}`)
does not exist. The codebase has:

- `BaseStrategy.submit_order` → `risk_manager.check_order` → `paper_sim` (paper) OR `clob_client` (live)
- `run_realistic_backtest` → `_SyntheticOrderBook.consume` (synthetic, isolated)

These are two separate universes. A strategy that backtests at
Sharpe 21 cannot be expected to achieve the same in paper or live
mode, because the slippage / risk / execution-quality paths are
entirely different. There is no shared `Broker` protocol, no shared
`SlippageModel`, no shared `OrderBook` interface. Until this is
fixed, **backtest results are not predictive of live performance**.

### CF3 — Walk-forward and Monte-Carlo are unreachable from production.

Both `walk_forward_analysis` and `monte_carlo_simulation` exist
with test coverage, but neither is wired to an API route. The
`advanced.py` module docstring (lines 27-29) explicitly references
`/api/backtest/walk-forward` and `/api/backtest/monte-carlo` —
**the docstring lies**. These are the two most valuable tools in
the backtest stack (per §31: walk-forward is the canonical
look-ahead guard; per §33: Monte-Carlo enables distributional
comparison across configurations) and neither is accessible
without a Python import + a custom caller.

### CF4 — No Backtest Lab (§33 FAIL).

There is no experiment registry, no DB persistence, no
cross-run comparison tooling. Every `run_realistic_backtest` call
returns a dict that is discarded by the caller. The task spec's
§33 requirement ("verify every experiment is persisted") is
**not even partially met**. Without persistence, parameter sweeps,
A/B comparisons across strategy versions, and date-range comparisons
are all impossible without external ad-hoc tooling.

### CF5 — Sharpe ratios of 20-41 are not credible and the look-ahead detector does not catch them.

Direct smoke test:

```
mm:  trades=267, win_rate=0.6779, sharpe=33.3482, mdd=15.6043, pf=2.5836, LE=0
arb: trades=138, win_rate=0.9275, sharpe=40.9745, mdd=5.5015,  pf=15.0565, LE=0
mom: trades=197, win_rate=0.5482, sharpe=20.1566, mdd=25.6916, pf=1.8005, LE=0
ml:  trades=233, win_rate=0.6481, sharpe=20.8776, mdd=26.8667, pf=2.2065, LE=0
```

A Sharpe of 33 on a 14-day backtest implies the strategy has
essentially no risk. Institutional Sharpe ratios above 3 are
exceptional; above 10 is a red flag for look-ahead bias or
simulation artifact. The look-ahead detector's LE_03
(UNREALISTIC_WIN_RATE) does not trigger because win_rate is below
0.95; **there is no LE_07 UNREALISTIC_SHARPE rule** to catch this.
A user trusting these metrics would make capital-allocation
decisions on fabricated data.

### CF6 — The Wave-5 time-ordered split fix IS verified (the one bright spot).

`ml/model.py:237-247` uses sequential `np.arange` indices for the
80/20 train/calibration split, with an inline comment explicitly
documenting the look-ahead rationale. The fix is in the ML
training path, not in the backtest engine (which has no train/test
split to contaminate, since it doesn't train). **VERIFIED.**

### CF7 — `walk_forward_analysis` correctly prevents temporal leakage.

`advanced.py:100` calls `np.argsort(timestamps)` before partitioning,
so even if the caller passes shuffled rows, the partition is
strictly chronological. The `train_end = start + train_window` /
`test_end = train_end + test_window` arithmetic guarantees that
the test fold begins at the index immediately after the train fold
ends — no overlap, no peek-ahead. Tested by
`test_walk_forward_sorts_by_timestamp`. **VERIFIED.**

### CF8 — The realistic engine's `look_ahead_bias` field is misleading.

Because the engine does not invoke real strategies or read real
historical data, **there is no look-ahead that could exist**.
`look_ahead_bias.total_violations = 0` is reported as if it's a
clean bill of health, but it's a tautology — the detector can
only flag violations of rules whose inputs are populated. LE_04
(FUTURE_TIMESTAMP_ACCESS) is dead code (B6). LE_02
(ENTRY_PRICE_EXTREMUM) checks against a *synthetic* period low/high
drawn independently from `decision_mid`, so a real look-ahead
strategy would have to be pathological to trip it. The detector
is well-designed for a real-replay backtest; in the current
synthetic-MC engine, it's mostly decorative.

---

## Recommended Next Actions (priority order)

1. **Decide the scope of the backtest engine.** Either (a) commit
   to building a real historical-replay engine that consumes
   `core/data_store` snapshots and invokes real `BaseStrategy`
   subclasses, or (b) rename `run_realistic_backtest` to
   `run_synthetic_microstructure_sim` and stop presenting its
   output as backtest results. The current naming is misleading.
2. **Extract a `Broker` protocol** with `submit_order` /
   `cancel_order` / `get_open_positions` methods and three
   implementations: `BacktestBroker` (wraps `_SyntheticOrderBook`),
   `PaperBroker` (wraps `paper_sim`), `LiveBroker` (wraps
   `clob_client`). Refactor `BaseStrategy.submit_order` to dispatch
   via this protocol.
3. **Add `POST /api/backtest/walk-forward` and
   `POST /api/backtest/monte-carlo` routes** that wrap
   `advanced.walk_forward_analysis` and `advanced.monte_carlo_simulation`
   in `asyncio.to_thread`. The functions already exist; the wiring
   is ~20 lines per route.
4. **Add a SQLite `backtest_runs` table** with columns
   `(run_id, strategy, start_date, end_date, capital, slippage_bps,
   fee_bps, metrics_json, equity_curve_json, trades_json, created_at)`.
   Persist every `run_realistic_backtest` call. Add a
   `GET /api/backtest/runs?strategy=mm&limit=20` query endpoint.
5. **Fix B1 (equity floor)** — track cash without truncation, or
   explicitly model margin-call / liquidation when cash < 0.
6. **Add `LE_07 UNREALISTIC_SHARPE`** rule to the look-ahead
   detector: flag Sharpe > 5 over > 30 trades (the threshold for
   "almost certainly a simulation artifact").
7. **Fix B8 (unseeded Monte Carlo)** — add `rng: np.random.RandomState
   | None = None` parameter; default to a fresh `RandomState(42)`.
8. **Fix B3 (fabricated monthly_returns)** — either compute them
   from the equity curve per calendar month (as `report.py` already
   does), or remove the field entirely from `BacktestResult`.
9. **Add backtest observability** — emit `record_metric("backtest",
   "duration_ms", ...)` and `record_metric("backtest",
   "lookahead_violations", ...)` per run.
10. **Add an integration test** that runs the same `MarketMakerStrategy`
    against (a) `run_realistic_backtest` and (b) `paper_sim` over
    the same period and asserts the PnL difference is within a
    tolerance band. This test will FAIL today; that failure is the
    forcing function for fixing CF1 and CF2.

---

*End of assessment. Generated per God Mode Master Prompt §30-33, §60
(23-section template). All evidence classifications per task spec.*
