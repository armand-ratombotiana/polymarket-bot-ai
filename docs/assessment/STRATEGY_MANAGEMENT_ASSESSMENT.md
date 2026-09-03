# Strategy Management Assessment — W17-5

**Assessment date:** 2026-09-04
**Scope:** §25–29 of the God Mode Master Prompt — strategy inventory,
strategy contract, registry lifecycle, attribution chain, strategy metrics.
**Code base:** `mini-services/polymarket-bot/strategies/` (4 files, 1,339 LOC)
plus the strategy-facing surfaces in `core/attribution.py`,
`core/decision_ledger.py`, `core/closed_positions.py`, `core/portfolio.py`,
`backtesting/engine.py`, and `api/server.py`.

---

## 1. Executive Summary

The platform advertises a "50-strategy quantitative trading framework" in
`strategies/registry.py` (§3 below), but only **3 of those 50 entries are
real strategies with executable trading loops**. The other **47 are
metadata-only catalog stubs** whose `_execute_cycle()` is literally
`pass` (verified — `QuantStrategyInstance._execute_cycle` at
`registry.py:117`). Calling `POST /api/strategy/toggle` on any of the
47 stub strategies succeeds (the registry instantiates a
`QuantStrategyInstance`, calls `start()`, and the strategy "appears
running" in `GET /api/strategy/catalog`) but never produces a single
trade, signal, or P&L contribution. This is the dominant finding.

The three real strategies — `signal_trader` (ML-driven directional,
Kelly sizing), `market_maker` (Avellaneda–Stoikov with inventory
flush), and `arb_scanner` (binary Dutch-book arbitrage) — are
**individually well-engineered and tested** (combined test footprint:
2,067 LOC across `test_signal_trader.py`, `test_market_maker.py`,
`test_arb_scanner.py`, `test_strategy_base.py`; all 35 strategy tests
pass). However, they share **no unified strategy contract**: the §26
interface (`metadata()`, `configure()`, `validate()`,
`generate_signal()`, `estimate_edge()`, `size_position()`,
`entry_logic()`, `exit_logic()`, `diagnostics()`) is entirely absent
— `BaseStrategy` exposes only `start()`, `stop()`, `_run()` (abstract),
`submit_order()`, and `cancel_order()`. Each concrete strategy
inlines its own signal-generation, sizing, and entry/exit logic with
no shared signature.

The **strategy-attribution chain** (§28) is *partially* implemented:
the `decision_id` UUID links `PREDICTION → SIGNAL → RISK_APPROVED →
RISK_REJECTED → ORDER → FILL` (verified in `core/decision_ledger.py`,
auto-stamped `model_version` on PREDICTION stages), and
`closed_positions` records `strategy`, `decision_id`, `model_version`,
`confidence`, `predicted_edge`, `p_yes`, `market_mid`, `liquidity`,
`direction`. **But there is no `strategy_version` field anywhere in
the codebase** (`rg "strategy_version|strat_version"` returns no
matches in `*.py`), and the **`feature_snapshot` and `market_snapshots`
tables exist in migrations but are not linked back to trades or
decisions** — closed positions carry no `feature_snapshot_id` /
`market_snapshot_id` foreign keys. The full §28 chain (`Trade →
Strategy → Strategy Version → Signal → Prediction → Model Version →
Feature Snapshot → Market Snapshot`) therefore **cannot be reconstructed
end-to-end**.

The **strategy metrics** surface (§29) is the strongest area: live
per-strategy roll-ups (`core/portfolio.py::strategy_stats` — fills,
win_rate, profit_factor, max_drawdown, capital_exposed,
exposure_dollar_days, avg_holding_duration_hours, profit_per_dollar,
profit_per_exposure_day, notional_volume) plus the
`/api/leaderboard` endpoint are present and tested. Backtest metrics
(`backtesting/engine.py::BacktestResult`) cover Sharpe, Sortino,
Calmar, VaR-95, Brier score, MDD, profit_factor, win_rate, ROI, CAGR.
**Gaps: no live Sharpe / Sortino / expectancy / turnover / capital
efficiency computation on real-time data** — those metrics are only
computed inside the backtester's synthetic equity curve.

**Strategy lifecycle states** (§27 — RESEARCH → EXPERIMENTAL →
BACKTESTED → VALIDATED → PAPER → LIVE_CANDIDATE → LIVE → SUSPENDED →
RETIRED) are **NOT FOUND**. `StrategyMeta` has only a
`default_enabled: bool` flag and the registry has no state-machine
field. There is no "SUSPENDED" concept anywhere except the generic
risk-manager `_strategy_cooldowns` dict (which is a runtime risk
circuit-breaker, not a lifecycle state). The `live_safety_gate.py`
"PAPER_MODE_24H" check (24h continuous paper session required before
LIVE) is the only thing approximating a PAPER → LIVE transition gate.

**Maturity score: 4.5 / 10** (§22). Three production-grade strategies
in a working harness, solid attribution plumbing for what exists, but
a strategy registry that materially misrepresents what's running, no
unified strategy contract, no lifecycle state machine, and a
half-finished attribution chain. The gap between the marketing copy
("50-strategy quantitative framework") and the on-disk reality
("3 strategies + 47 no-op stubs") is the single largest credibility
risk in the codebase.

---

## 2. Purpose

This document assesses the platform's strategy-management capability
against the requirements in §25–29 of the God Mode Master Prompt:

- **§25** — Find all strategies in the repository and document each
  strategy's behaviour, data consumed, and signals produced.
- **§26** — Verify whether strategies follow a unified
  `metadata/configure/validate/generate_signal/estimate_edge/
  size_position/entry_logic/exit_logic/diagnostics` contract.
- **§27** — Verify the registry exposes the full §27 metadata field
  set (ID, Name, Version, Description, Status, Market Types,
  Configuration, Risk Profile, Capital Allocation, Model Dependencies,
  Backtest/Paper/Live Results, Created At, Updated At) and a 9-state
  lifecycle.
- **§28** — Verify every trade is attributable across the full
  Trade → Strategy → Strategy Version → Signal → Prediction →
  Model Version → Feature Snapshot → Market Snapshot chain.
- **§29** — Verify the platform tracks the full §29 metrics surface
  (trades, wins, losses, win rate, realized P&L, ROI, expectancy,
  profit factor, Sharpe, Sortino, drawdown, turnover, exposure,
  capital efficiency, slippage, average holding time).

The intended audience is the platform owner deciding whether to
invest in (a) collapsing the 47 stub strategies down to their 3
working siblings, (b) formalizing a unified strategy contract, and
(c) closing the attribution/metrics gaps identified below.

---

## 3. Current Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ api/server.py (FastAPI lifespan)                                  │
│   on_startup:  strategy_registry.start_strategy(<id>)             │
│                ├── "mm_avellaneda_stoikov"  → MarketMakerStrategy │
│                ├── "arb_binary_dutch_book" → ArbScannerStrategy  │
│                └── "ml_random_forest_quant" → SignalTraderStrategy│
│   on_shutdown: strategy_registry.stop_strategy(<id>)              │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│ strategies/registry.py                                           │
│   STRATEGY_CATALOG : list[StrategyMeta]  (50 entries, 6 groups) │
│   StrategyRegistry                                                │
│     ├── get_catalog()           → list[dict] for /api/strategy   │
│     ├── start_strategy(id)      → dispatches to a concrete class  │
│     │   • 3 ids → real strategy class                             │
│     │   • 47 ids → QuantStrategyInstance (no-op _execute_cycle)   │
│     ├── stop_strategy(id)                                          │
│     └── get_active_instances()                                    │
└────────────────────────────┬─────────────────────────────────────┘
                             │
        ┌────────────────────┼─────────────────────┐
        ▼                    ▼                     ▼
┌─────────────────┐ ┌──────────────────┐ ┌───────────────────────┐
│ signal_trader.py│ │ market_maker.py  │ │ arb_scanner.py        │
│   (483 LOC)     │ │   (408 LOC)      │ │   (291 LOC)           │
│                 │ │                  │ │                       │
│ _scan_markets  │ │ _review_quotes   │ │ _scan_for_arb         │
│ _evaluate_market│ │ _place_skewed_   │ │ _check_long_dutch_   │
│ _ml_signal     │ │   quotes         │ │   book                │
│ _act_on_signal │ │ _flush_stale_    │ │ _check_short_         │
│ record_outcome │ │   inventory      │ │   overpriced          │
│                 │ │ _ml_spread_adj   │ │ _execute_arb          │
│ ─────────────  │ │ ──────────────  │ │ ───────────────────  │
│ ALL inherit BaseStrategy: start/stop/_run/submit_order/        │
│ cancel_order — no shared signal/edge/sizing contract           │
└─────────────────┴──────────────────┴───────────────────────┘
        │                    │                     │
        └────────┬───────────┴──────────┬──────────┘
                 ▼                       ▼
┌──────────────────────────┐  ┌──────────────────────────────────┐
│ core/decision_ledger.py  │  │ core/closed_positions.py         │
│  decision_events         │  │  closed_positions                │
│  (decision_id, stage,    │  │  (decision_id, strategy,         │
│   strategy, model_ver,   │  │   model_version, confidence,     │
│   p_yes, edge, mid, …)   │  │   predicted_edge, p_yes,         │
│                          │  │   liquidity, holding_seconds, …) │
└──────────────────────────┘  └──────────────┬───────────────────┘
                                              │
                                              ▼
                       ┌──────────────────────────────────────┐
                       │ core/attribution.py                  │
                       │  by_strategy, by_confidence_bucket,  │
                       │  by_edge_bucket, by_probability_band,│
                       │  by_liquidity_level,                 │
                       │  by_holding_period, by_trade_dir     │
                       └──────────────────────────────────────┘
```

**Architecture verdict:** Layering is sound — strategies inherit a
common `BaseStrategy`, route through a shared `submit_order` →
`risk_manager` → `paper_sim`/`clob_client` path, write a unified
`decision_ledger`, and roll up through `closed_positions` →
`attribution`. The architectural debt is *inside* the strategies
(each inlines its own logic with no contract) and at the registry
level (47 fake stubs).

---

## 4. Current Components

### 4.1 `strategies/base.py` — `BaseStrategy` (148 LOC)
- Abstract base class for every strategy.
- Public surface: `name`, `start()`, `stop()`, `_run()` (abstract),
  `submit_order(args, decision_id="")`, `cancel_order(order_id)`.
- `submit_order` is the canonical integration point: it builds a
  provisional `Order`, calls `risk_manager.check_order`, records
  `RISK_APPROVED` / `RISK_REJECTED` stages into `decision_ledger`
  (lazy-imported), then dispatches to either `paper_sim.create_order`
  (paper mode) or `clob_client.create_order` (live mode). The
  `decision_id` flows through every downstream stage. **VERIFIED**.
- The `decision_id` default of `""` is a back-compat shim: legacy
  callers (`/api/trade` manual-trade endpoint, `/api/position/close`)
  submit orders with empty `decision_id` and the ledger writes a
  `RISK_APPROVED` row whose `decision_id=""` — these rows are
  technically orphan chains (no upstream `PREDICTION`/`SIGNAL` stage)
  but the schema permits it. **VERIFIED**.

### 4.2 `strategies/signal_trader.py` — `SignalTraderStrategy` (483 LOC)
- Catalog id: `ml_random_forest_quant`. Catalog name: *"Random Forest
  Quant Model"*.
- Architecture: 15 s scan loop → `_scan_markets()` iterates
  `market_discovery.catalog` (800+ markets) → for each token:
  `extract_features(mkt, book)` → `ml_model.predict(features, token_id)`
  → directional gate (`p_yes ≥ 0.55` → BUY, `≤ 0.45` → SELL) →
  fractional-Kelly sizing via `core.capital_allocator.allocate_capital`
  (Michaelis–Menten saturating edge curve + smoothstep multipliers
  for confidence/calibration/drawdown/exposure/performance/liquidity).
- Decision-ledger stages emitted: `PREDICTION` (always, for every
  evaluated market), `SIGNAL` (only when all gates pass), plus four
  `record_rejection()` paths: `low_confidence`, `wide_spread`,
  `neutral_zone`, `insufficient_kelly_edge`,
  `capital_allocator_zero`. **VERIFIED**.
- Online learning: `record_outcome(token_id, resolved_yes)` updates
  the ML model from resolved markets (called by `paper/simulator.py`
  on settlement).
- Cache: `OrderedDict` feature/market caches bounded at 500 entries
  each (`FEATURE_CACHE_MAX = 500`). **VERIFIED**.
- Top-3 signal cap per scan cycle: `signals[:3]` in `_scan_markets`.

### 4.3 `strategies/market_maker.py` — `MarketMakerStrategy` (408 LOC)
- Catalog id: `mm_avellaneda_stoikov`. Catalog name: *"Avellaneda-
  Stoikov MM"*.
- Architecture: 4 s quote-review loop on up to 8 markets (auto-selected
  by 24 h volume from Gamma, or `MM_TOKEN_IDS` env override) → for
  each token: `_review_quotes()` checks if mid moved >0.4 % or
  resting quotes were filled → if so `_place_skewed_quotes()` cancels
  old quotes, computes reservation price
  `r = mid − q·γ·σ² + ml_skew`, places bid/ask at `r ± half_spread`.
- Avellaneda–Stoikov parameters: `γ=0.08` (risk aversion),
  `σ²` = max(spread²/2, rolling_vol_feature²) with feature index 36
  fallback, base spread = `MM_SPREAD_BPS/10000` (min 1 %),
  `MAX_MARKETS_TO_QUOTE = 8`, `MID_TOLERANCE = 0.004`.
- ML coupling: `_ml_spread_adjustment()` tightens spread 15 % when
  ML confidence > 0.7, widens 25 % when < 0.3, and skews reservation
  price by up to ±2 % toward ML fair value. **VERIFIED**.
- Inventory flush: any non-zero YES inventory held > 60 s is dumped
  via marketable SELL at `best_bid` (`_flush_stale_inventory`).
- Bug fix documented in-source: previous formula multiplied `q` by
  an extra `0.01`, making the inventory skew negligible (0.0008)
  — removed so the A-S skew term actually moves price.
- One-sided quoting: a market with mid near 0.99 still receives a
  bid-only quote (not both sides). **VERIFIED** in
  `_place_skewed_quotes`.

### 4.4 `strategies/arb_scanner.py` — `ArbScannerStrategy` (291 LOC)
- Catalog id: `arb_binary_dutch_book`. Catalog name: *"Binary Dutch
  Book"*.
- Architecture: `_scan_interval` (default ~10 s) loop → for each
  YES/NO token pair discovered from Gamma → `_check_long_dutch_book()`
  (long-side: `Ask(YES) + Ask(NO) < 1 − min_profit_frac`) and
  `_check_short_overpriced()` (short-side: `Bid(YES) + Bid(NO) > 1 +
  min_profit_frac`) → top-3 opportunities per cycle →
  `_execute_arb()` submits two FOK orders simultaneously via
  `asyncio.gather`.
- Staleness guard: skips books whose `updated_at` is > 30 s old
  (prevents trading on stale quotes). **VERIFIED** at
  `arb_scanner.py:148-151` and `:197-200`.
- Depth guard: requires `asks[0].size`/`bids[0].size` ≥
  `min_required_shares` so the order doesn't move the market against
  itself. **VERIFIED** at `arb_scanner.py:154-158`.
- ML quality filter: `_ml_arb_suspicion()` flags when ML
  `abs(p_yes − book_price) > 0.20` at confidence > 0.7 — skips the
  arb rather than trading on a stale or erroneous book. **VERIFIED**.
- Pair-refresh loop: every 600 s, re-fetches markets from Gamma so
  new binary markets are picked up automatically.

### 4.5 `strategies/registry.py` — `StrategyRegistry` + `QuantStrategyInstance` (184 LOC)
- `STRATEGY_CATALOG`: 50 `StrategyMeta` entries across 6 groups
  (8 market-making + 8 arbitrage + 8 statistical + 8 momentum +
  8 event-driven + 10 ML/RL). Each entry: `strategy_id`, `name`,
  `category`, `description`, `risk_level`, `default_enabled`.
- `get_catalog()` returns 50 dicts with `implemented` boolean — only
  `mm_avellaneda_stoikov`, `arb_binary_dutch_book`,
  `ml_random_forest_quant` have `implemented=True` (the other 47 are
  metadata-only stubs whose `_execute_cycle()` body is `pass`).
  **VERIFIED** at `registry.py:131-146`.
- `start_strategy(id)` dispatches: 3 known ids → concrete class,
  everything else → `QuantStrategyInstance(meta)` which runs an
  `asyncio.sleep(5.0)` loop calling `_execute_cycle()` (a no-op
  `pass`). Starting any of the 47 stub strategies therefore appears
  to succeed (returns `True`, shows up in `get_active_instances()`)
  but produces no signals, no orders, no P&L. **VERIFIED**.

### 4.6 `core/decision_ledger.py` (decision chain)
- Schema: `decision_events (id, timestamp, decision_id, stage,
  token_id, strategy, pnl, data_json)` + `decision_rejections`.
- Six canonical stages: `PREDICTION`, `SIGNAL`, `RISK_APPROVED`,
  `RISK_REJECTED`, `ORDER`, `FILL` (constants at
  `decision_ledger.py:111-116`).
- Auto-stamps `model_version` on every `PREDICTION` stage event
  (`decision_ledger.py:272-273`) — resolves the active model version
  via `_resolve_active_model_version()`.
- Rejection rows: separate `decision_rejections` table for fast
  filtered listing (`GET /api/decisions/rejected`).

### 4.7 `core/closed_positions.py` (trade journal)
- Schema (verified at `closed_positions.py:23-37`):
  `id, timestamp, position_id, token_id, strategy, entry_price,
  exit_price, shares, pnl, holding_seconds, model_version,
  decision_id, direction, confidence, predicted_edge, p_yes,
  market_mid, liquidity, metadata_json`.
- Indexed on `(token_id, timestamp DESC)`, `(strategy, timestamp
  DESC)`, `(timestamp DESC)`.
- This is the canonical source for `core/attribution.py`.

### 4.8 `core/attribution.py` (7-dimension P&L roll-up, 522 LOC)
- Buckets closed positions across 7 orthogonal dimensions:
  `by_strategy`, `by_confidence_bucket` (low/medium/high/very_high),
  `by_edge_bucket` (negative/small/medium/large/very_large),
  `by_probability_band` (deep_no/no/neutral/yes/strong_yes),
  `by_liquidity_level` (thin/low/medium/high/very_high),
  `by_holding_period` (intraday/short/medium/long),
  `by_trade_direction` (BUY/SELL).
- Per-bucket payload: `count, total_pnl, avg_pnl, win_rate, wins,
  losses, avg_holding_seconds, gross_profit, gross_loss,
  profit_factor (None when no losses), capital_deployed`.
- `get_full_attribution()` does a single SELECT (W11-9 optimization)
  instead of 7 parallel SELECTs, then reuses the in-memory list across
  all 7 `_attribute_*_from_rows` synchronised aggregators.
- TTL-cached 60 s via `attribution_cache`; `POST /api/trade` and
  `POST /api/position/close` invalidate.
- Endpoint: `GET /api/attribution`.

### 4.9 `core/portfolio.py::strategy_stats` + `leaderboard()`
- Per-strategy live roll-up: `fills, closed_trades, gross_pnl,
  net_pnl, capital_exposed, open_exposure,
  profit_per_dollar_exposed, profit_per_exposure_day,
  exposure_dollar_days, avg_holding_duration_hours, win_rate,
  profit_factor, avg_win, avg_loss, max_drawdown, notional_volume`.
- `risk_adjusted_score(stats) = net_pnl − exposure_penalty
  − drawdown_penalty − capital_time_penalty − uncertainty_penalty`
  (uncertainty_penalty shrinks as `closed_trades` grows past 20).
- Endpoint: `GET /api/leaderboard` (30 s cache).

### 4.10 `backtesting/engine.py` — `BacktestEngine` + `BacktestResult`
- Synthetic simulation with binary-market payoffs, slippage, fees,
  queue priority.
- `BacktestResult` fields: `strategy_id, initial_capital,
  final_equity, total_pnl, roi_pct, cagr_pct, sharpe_ratio,
  sortino_ratio, calmar_ratio, value_at_risk_95,
  expected_value_per_trade, brier_score, max_drawdown_pct,
  profit_factor, win_rate, total_trades, winning_trades,
  losing_trades, equity_curve, monthly_returns`.
- Endpoints: `POST /api/backtest/run`, `POST /api/backtest/report`,
  `POST /api/backtest/report/pdf`.
- **Caveat:** backtest dispatch is by `strategy_id` substring
  (`if "mm" in strategy_id`, `elif "arb" in strategy_id`,
  `elif "mom" in strategy_id`, `elif "ml" in strategy_id`) — it does
  NOT call the live strategy classes' `_run()` loop. The backtest is a
  separate synthetic profile, not a replay of the live code path.

### 4.11 `core/live_safety_gate.py` (PAPER → LIVE gate)
- The only thing approximating a §27 lifecycle state machine:
  `CHECK_PAPER_MODE` requires trading mode == paper AND session age
  ≥ 24 h before allowing live-trading enable.
- Plus `CHECK_POSITIVE_EXPECTANCY` (avg PnL > 0 across closed trades)
  and `CHECK_MAX_DRAWDOWN` (drawdown < $2.00).
- `POST /api/safety/live/enable` is the explicit PAPER → LIVE gate.

---

## 5. Data Flow

### 5.1 Signal → Trade → Attribution (signal_trader path)

```
market_discovery.catalog (800+ markets)
   │
   ▼
book_poller (background) ──► store.order_books[token_id] = OrderBook
   │
   ▼
signal_trader._scan_markets()  (15 s loop)
   │
   ▼ for each (token_id, mkt) in catalog_items:
signal_trader._evaluate_market(mkt, token_id)
   │
   ├─► extract_features(mkt, book) → np.ndarray[38]   ──► ml/features.py
   │                                                    cached in
   │                                                    _feature_cache[yes_token]
   │
   ▼
ml_model.predict(features, token_id) → (p_yes, confidence)
   │
   ▼
_ml_signal(...) → MarketSignal | None
   │   ├── ledger.record(PREDICTION, p_yes, confidence, mid, edge)
   │   ├── gates:
   │   │     • confidence < 0.45   → record_rejection(low_confidence)
   │   │     • spread ≥ 0.04       → record_rejection(wide_spread)
   │   │     • 0.45 < p_yes < 0.55 → record_rejection(neutral_zone)
   │   │     • kelly ≤ 0.02        → record_rejection(insufficient_kelly_edge)
   │   │     • allocate_capital() → 0 → record_rejection(capital_allocator_zero)
   │   └── ledger.record(SIGNAL, direction, target_price, size_usdc, kelly_f, ...)
   │
   ▼ MarketSignal
_act_on_signal(sig) → submit_order(args, decision_id=sig.decision_id)
   │
   ▼
BaseStrategy.submit_order:
   │   ├── risk_manager.check_order(provisional)
   │   ├── ledger.record(RISK_APPROVED or RISK_REJECTED, reason=…)
   │   └── paper_sim.create_order(args, decision_id=…) OR clob_client.create_order(args)
   │
   ▼
paper/simulator._execute_fill()
   │   ├── ledger.record(FILL, pnl=realised_pnl)
   │   ├── closed_positions.record(token_id, strategy, decision_id, model_version,
   │   │                        confidence, predicted_edge, p_yes, market_mid,
   │   │                        liquidity, holding_seconds, pnl, direction)
   │   └── attribution_cache.clear()  (invalidate)
   │
   ▼
core/attribution.get_full_attribution()
   │   └── 7-dimension roll-up across all closed positions
   │
   ▼
GET /api/attribution (60 s TTL cache)
GET /api/leaderboard (30 s TTL cache, per-strategy strategy_stats)
```

### 5.2 Data flows for market_maker and arb_scanner
- Both go through the same `submit_order → decision_ledger →
  closed_positions → attribution` pipeline, so the data-flow diagram
  above is identical except for the strategy-internal signal
  generation (`_review_quotes` vs `_scan_for_arb`).
- **Notable gap:** `market_maker` and `arb_scanner` do NOT emit
  `PREDICTION` / `SIGNAL` stages to the decision ledger — only
  `signal_trader` does. So a market-maker quote that fills has a
  decision chain that starts at `RISK_APPROVED`, not at
  `PREDICTION`. The §28 attribution chain (`Signal → Prediction`)
  therefore has a hole for 2 of the 3 strategies. **VERIFIED** — only
  `signal_trader.py` imports `decision_ledger.record(...,
  stage="PREDICTION")`.

---

## 6. Execution Flow

### 6.1 Server startup (`api/server.py` lifespan)
```
on_startup:
  1. await strategy_registry.start_strategy("mm_avellaneda_stoikov")
  2. await strategy_registry.start_strategy("arb_binary_dutch_book")
  3. await strategy_registry.start_strategy("ml_random_forest_quant")
  4. watchdog.beat("strategy_registry")
  → 3 asyncio tasks created (one per strategy)
  → each task runs strategy._run() loop
```

### 6.2 Per-strategy loop cadence
| Strategy | Loop interval | Concurrency pattern |
|---|---|---|
| `signal_trader` | 15 s | single scan loop, top-3 signals/cycle |
| `market_maker` | 4 s | per-token `_review_quotes`, refresh markets every 10 cycles (~40 s) |
| `arb_scanner` | ~10 s (configurable) | scan all pairs, top-3 opps/cycle, refresh pairs every 600 s |

### 6.3 Order lifecycle
```
OrderArgs → BaseStrategy.submit_order
  → risk_manager.check_order (position limits, per-strategy cooldown,
                              strategy-level circuit breaker)
  → decision_ledger.record(RISK_APPROVED / RISK_REJECTED)
  → [paper] paper_sim.create_order    OR   [live] clob_client.create_order
  → store.add_order(order)
  → [paper] paper_sim._execute_fill loop:
      → store.fill_order
      → closed_positions.record (if round-trip complete)
      → decision_ledger.record(FILL, pnl=…)
      → attribution_cache.clear
```

### 6.4 Server shutdown
```
on_shutdown:
  for strat_id in strategy_registry.get_active_instances():
      await strategy_registry.stop_strategy(strat_id)
  → each task.cancel(); await task (swallows CancelledError)
```

---

## 7. Feature Inventory

### 7.1 What the system claims (catalog — 50 entries, 6 groups)
| Group | Count | Real implementations |
|---|---:|---:|
| Market Making & Liquidity Provision (Group A) | 8 | **1** (`mm_avellaneda_stoikov`) |
| Arbitrage & Relative Value (Group B) | 8 | **1** (`arb_binary_dutch_book`) |
| Statistical Arbitrage & Mean Reversion (Group C) | 8 | 0 |
| Momentum, Breakout & Trend Following (Group D) | 8 | 0 |
| Event-Driven, Sentiment & Intelligence (Group E) | 8 | 0 |
| Machine Learning & Reinforcement Learning (Group F) | 10 | **1** (`ml_random_forest_quant`) |
| **Total** | **50** | **3** |

### 7.2 What the system actually does

| Capability | Present? | Where |
|---|---|---|
| ML directional trader with online learning | ✅ | `signal_trader.py` + `ml/model.py` |
| Fractional Kelly sizing with safety gates | ✅ | `signal_trader.py` + `core/capital_allocator.py` |
| Avellaneda–Stoikov market maker with inventory skew | ✅ | `market_maker.py` |
| Stale inventory flush (> 60 s) | ✅ | `market_maker._flush_stale_inventory` |
| ML-coupled spread tightening/widening | ✅ | `market_maker._ml_spread_adjustment` |
| Binary Dutch-book arbitrage (long & short side) | ✅ | `arb_scanner.py` |
| Cross-market / multi-outcome arbitrage | ❌ | not implemented (catalog stubs only) |
| Statistical mean-reversion (Bollinger, OU, RSI, Kalman) | ❌ | not implemented (catalog stubs only) |
| Momentum / breakout (EMA, MACD, Donchian, ADX) | ❌ | not implemented (catalog stubs only) |
| Event-driven (news sentiment, polls, whale follower) | ❌ | not implemented (catalog stubs only) |
| ML/RL (XGBoost, LightGBM, SVM, Bayesian, Q-learning) | ❌ | not implemented (catalog stubs only) |
| Strategy registry with start/stop API | ✅ | `registry.py` + `/api/strategy/toggle` |
| Unified decision ledger (PREDICTION→FILL chain) | ✅ (partial) | `core/decision_ledger.py` — only signal_trader emits PREDICTION/SIGNAL |
| Closed positions journal with attribution columns | ✅ | `core/closed_positions.py` |
| 7-dimension P&L attribution roll-up | ✅ | `core/attribution.py` + `GET /api/attribution` |
| Per-strategy live metrics (`strategy_stats`) | ✅ | `core/portfolio.py` + `GET /api/leaderboard` |
| Backtest engine with Sharpe/Sortino/Calmar/VaR/Brier | ✅ | `backtesting/engine.py` |
| Live safety gate (24 h paper + positive expectancy + MDD < $2) | ✅ | `core/live_safety_gate.py` |
| Strategy lifecycle state machine (RESEARCH→RETIRED) | ❌ | NOT FOUND |
| Unified strategy contract (`metadata/configure/validate/...`) | ❌ | NOT FOUND |
| `strategy_version` field for attribution | ❌ | NOT FOUND |

---

## 8. What Works

1. **The 3 real strategies are production-quality and well-tested.**
   Combined 2,067 LOC of unit tests; all 35 strategy tests pass
   (`pytest tests/test_strategy_base.py tests/test_signal_trader.py
   tests/test_market_maker.py tests/test_arb_scanner.py -q` → 35
   passed). **VERIFIED.**

2. **The decision-ledger chain is sound for `signal_trader`.**
   PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL is fully
   linked by `decision_id`, and `model_version` is auto-stamped on
   every PREDICTION stage (`decision_ledger.py:272-273`). The
   `decision_id` UUID is propagated from `_ml_signal` through
   `submit_order` to `paper_sim._execute_fill`, so any closed
   position can be traced back to the originating prediction.
   **VERIFIED.**

3. **The closed-positions schema captures the §28 attribution columns
   that exist.** `strategy`, `decision_id`, `model_version`,
   `confidence`, `predicted_edge`, `p_yes`, `market_mid`,
   `liquidity`, `holding_seconds`, `direction` are all recorded
   on every closed trade. **VERIFIED** at `closed_positions.py:23-37`.

4. **The 7-dimension attribution engine is properly engineered.**
   The W11-9 optimization (single `SELECT *` instead of 7 parallel
   SELECTs) is in place. Bucket classifiers are pure functions
   (`classify_confidence`, `classify_edge`, `classify_probability`,
   `classify_liquidity`, `classify_holding_period`,
   `classify_trade_direction`) with documented boundaries. Empty
   buckets still appear in output for stable dashboard schema.
   `profit_factor` correctly returns `None` when `gross_loss == 0`.
   **VERIFIED.**

5. **The live per-strategy roll-up is comprehensive for what it
   covers.** `strategy_stats` returns fills, closed_trades,
   gross/net PnL, capital exposed, open exposure, profit per dollar
   exposed, profit per exposure-day, exposure-dollar-days, avg holding
   duration, win_rate, profit_factor, avg_win/avg_loss, max_drawdown,
   notional volume. `risk_adjusted_score` combines them into a single
   ranking metric. **VERIFIED.**

6. **The backtest engine produces institutional-grade metrics.**
   Sharpe, Sortino, Calmar, VaR-95, Brier score, MDD, profit factor,
   win rate, ROI, CAGR, expected value per trade — all in
   `BacktestResult`. **VERIFIED.**

7. **The live-safety gate is a real PAPER → LIVE gate.** 24 h
   continuous paper session + positive expectancy + MDD < $2 are
   enforced before `POST /api/safety/live/enable` flips trading mode.
   **VERIFIED** in `core/live_safety_gate.py`.

8. **The arb scanner has good guards against trading on bad data.**
   Staleness guard (> 30 s old book → skip), depth guard (insufficient
   size at ask/bid → skip), ML suspicion filter (model disagrees with
   book by > 20 ¢ at high confidence → skip). **VERIFIED.**

9. **The market maker correctly bounds ask size by actual inventory**
   so it never lists a SELL for shares it doesn't hold
   (`market_maker.py:278`). The A-S inventory skew formula has been
   fixed (the previous extra-`0.01` bug documented in-source).
   **VERIFIED.**

10. **`GET /api/strategy/catalog` honestly reports which strategies
    are implemented.** The `implemented` boolean is `True` for only
    the 3 real strategies, `False` for the 47 stubs. The problem is
    that the API does not prevent you from starting a stub strategy
    via `/api/strategy/toggle` — but the catalog itself is truthful.
    **VERIFIED.**

---

## 9. What Does Not Work

1. **47 of the 50 catalog entries are non-functional stubs.**
   `QuantStrategyInstance._execute_cycle()` is literally `pass`
   (`registry.py:117-119`). Starting any of these strategies
   succeeds (returns `True`, adds to `_instances`) but produces no
   signals, no orders, no P&L. The catalog advertises strategies
   like "GLFT Optimal Quoter", "Pair Cointegration Trader",
   "Donchian Channel Breakout", "News Sentiment Breakout",
   "XGBoost Directional", "Q-Learning Execution Agent" — none of
   these exist as executable code. **VERIFIED.**

2. **No unified strategy contract.** The §26 contract
   (`metadata/configure/validate/generate_signal/estimate_edge/
   size_position/entry_logic/exit_logic/diagnostics`) is entirely
   absent. Each of the 3 real strategies inlines its own:
   - signal generation: `signal_trader._ml_signal`,
     `market_maker._place_skewed_quotes`, `arb_scanner._scan_for_arb`
   - sizing: `signal_trader` calls `allocate_capital`;
     `market_maker` uses `self._quote_size / bid_price`;
     `arb_scanner` uses `self._order_size / yes_price`
   - entry logic: each strategy hand-rolls its own `OrderArgs`
     construction inside its loop body
   - exit logic: `signal_trader` recycles stale orders after 180 s;
     `market_maker` flushes inventory after 60 s; `arb_scanner` uses
     FOK orders that self-settle
   - diagnostics: none of the strategies implement a `diagnostics()`
     method; per-strategy introspection is via `get_catalog()` (a
     metadata read, not a runtime diagnostic)
   **VERIFIED** — no method matching the §26 contract names appears
   anywhere in `strategies/*.py`.

3. **`market_maker` and `arb_scanner` do NOT emit `PREDICTION` /
   `SIGNAL` stages to the decision ledger.** Only `signal_trader`
   calls `decision_ledger.record(stage="PREDICTION", ...)` /
   `stage="SIGNAL"`. A market-maker fill therefore has a decision
   chain that starts at `RISK_APPROVED` (skipping the upstream
   PREDICTION/SIGNAL stages), so the §28 chain
   (Signal → Prediction → …) is broken for 2 of the 3 strategies.
   **VERIFIED** via grep for `STAGE_PREDICTION` / `STAGE_SIGNAL`
   callers — only `signal_trader.py` matches.

4. **No `strategy_version` field exists anywhere in the codebase.**
   `rg "strategy_version|strat_version"` returns no matches in
   `*.py`. The §28 attribution chain calls for `Strategy → Strategy
   Version` linkage; only `model_version` is captured (which is
   a different thing — it tracks the ML model version, not the
   strategy code version). **VERIFIED.**

5. **`feature_snapshot` and `market_snapshots` tables exist but are
   not linked to trades or decisions.** Both tables are defined in
   migrations (`core/db/migrations/001_initial_enterprise_schemas.sql:256`
   for `feature.feature_snapshot`, `001_initial_schema.sql:247` for
   `market_snapshots`), but `closed_positions` has no
   `feature_snapshot_id` / `market_snapshot_id` foreign keys, and
   `decision_events.data_json` carries no snapshot references. The
   §28 chain's last two links (`Feature Snapshot`, `Market
   Snapshot`) therefore cannot be reconstructed for any trade.
   **VERIFIED.**

6. **No strategy lifecycle state machine.** §27 specifies 9 states
   (RESEARCH → EXPERIMENTAL → BACKTESTED → VALIDATED → PAPER →
   LIVE_CANDIDATE → LIVE → SUSPENDED → RETIRED). `StrategyMeta` has
   only `default_enabled: bool`. The only lifecycle-like state in
   the codebase is `risk_manager._strategy_cooldowns` (a runtime
   circuit-breaker) and `live_safety_gate`'s PAPER → LIVE gate.
   There is no RESEARCH / EXPERIMENTAL / RETIRED concept.
   **VERIFIED** via `rg "RESEARCH|EXPERIMENTAL|BACKTESTED|
   VALIDATED|LIVE_CANDIDATE|SUSPENDED|RETIRED"` against `*.py` —
   no matches in `strategies/`, `core/`, `risk/`, or `api/`.

7. **`StrategyMeta` is missing most of the §27 fields.** The §27
   spec calls for: ID, Name, Version, Description, Status, Market
   Types, Configuration, Risk Profile, Capital Allocation, Model
   Dependencies, Backtest Results, Paper Results, Live Results,
   Created At, Updated At. `StrategyMeta` carries only: `strategy_id`
   (ID), `name` (Name), `category`, `description` (Description),
   `risk_level` (a coarse "Low/Medium/High" — a partial Risk
   Profile), `default_enabled` (a coarse Status). **Missing:**
   Version, Market Types, Configuration, Capital Allocation, Model
   Dependencies, Backtest Results, Paper Results, Live Results,
   Created At, Updated At. **VERIFIED** at `registry.py:18-25`.

8. **Live Sharpe / Sortino / expectancy / turnover / capital
   efficiency are not computed.** These appear only inside the
   backtester (`backtesting/engine.py::BacktestResult`). Live
   `strategy_stats` returns win_rate, profit_factor, max_drawdown,
   profit_per_dollar_exposed (a partial capital efficiency), and
   avg_holding_duration_hours, but **no Sharpe, no Sortino, no
   expectancy (avg win × win_rate + avg loss × loss_rate), no
   turnover (notional / capital), no slippage stats.** Slippage is
   tracked separately in `core/execution_quality.py` but not joined
   into the per-strategy leaderboard. **VERIFIED** at
   `core/portfolio.py:159-212`.

9. **The backtester does not replay the live strategy code.** The
   `if "mm" in strategy_id elif "arb" in strategy_id elif "mom" in
   strategy_id elif "ml" in strategy_id` dispatch
   (`backtesting/engine.py:131-149`) selects a synthetic profile,
   NOT the live `MarketMakerStrategy` / `ArbScannerStrategy` /
   `SignalTraderStrategy` classes. A backtest result therefore
   reflects the backtester's internal model of a "mm-style"
   strategy, not the actual production strategy. Backtest results
   in the §27 sense (a field on `StrategyMeta`) would not be
   comparable to live results. **VERIFIED.**

10. **Strategy catalog `default_enabled` is decorative.** Three
    catalog entries have `default_enabled=True`
    (`mm_avellaneda_stoikov`, `arb_binary_dutch_book`,
    `ml_random_forest_quant`), and the api/server startup hardcodes
    the same three ids. But `default_enabled` is never read by
    `start_strategy()` or the startup code — the startup list is
    literal. So if someone adds a 4th real strategy and sets
    `default_enabled=True` on it, it still won't start automatically
    unless someone also edits `api/server.py:420-422`.
    **VERIFIED** at `api/server.py:420-422` and `registry.py:148-170`.

11. **The top-3 signals/cycle cap on `signal_trader` is hardcoded.**
    `signals[:3]` at `signal_trader.py:163`. If 10 high-conviction
    signals arrive in the same 15 s scan, 7 are dropped silently
    (no rejection ledger entry, no observability metric for the
    drop). Same for `arb_scanner`'s `opportunities[:3]`. **VERIFIED.**

12. **`signal_trader.record_outcome` only updates the ML model — it
    does not write to `closed_positions`.** The closed-position
    recording happens in `paper/simulator.py::_execute_fill` (when
    the round-trip completes), not on resolution. This means a
    position that's still open at market resolution may not get its
    `record_outcome` call, and the model is updated only when
    `paper/simulator` calls it. **VERIFIED** — the only caller of
    `signal_trader.record_outcome` is in `paper/simulator.py`.

---

## 10. Missing Features

### §26 — Strategy Contract (all 9 methods)
- `metadata()` — partial (only `name`, `strategy_id` from class attr
  + `StrategyMeta` dataclass, no method)
- `configure()` — NOT FOUND
- `validate()` — NOT FOUND (validation is implicit in
  `risk_manager.check_order`, not a strategy method)
- `generate_signal()` — NOT FOUND as a shared method; per-strategy:
  `signal_trader._ml_signal`, `market_maker._place_skewed_quotes`,
  `arb_scanner._check_long_dutch_book` / `_check_short_overpriced`
- `estimate_edge()` — NOT FOUND; edge is computed inline
  (`predicted_edge = p_yes - mid` in signal_trader; arb profit in
  arb_scanner; implicit in market_maker's reservation-price skew)
- `size_position()` — NOT FOUND; sizing is per-strategy
  (`allocate_capital` for signal_trader, `quote_size/price` for
  market_maker/arb_scanner)
- `entry_logic()` — NOT FOUND; entry is inline in each strategy's
  loop body
- `exit_logic()` — NOT FOUND; exit is inline (`_flush_stale_inventory`
  for market_maker, `_recycle_stale_orders` for signal_trader, FOK
  self-settle for arb_scanner)
- `diagnostics()` — NOT FOUND

### §27 — Registry Metadata (15 fields, 9 lifecycle states)
Missing fields: **Version, Market Types, Configuration, Capital
Allocation, Model Dependencies, Backtest Results, Paper Results, Live
Results, Created At, Updated At.**

Missing lifecycle states: **RESEARCH, EXPERIMENTAL, BACKTESTED,
VALIDATED, LIVE_CANDIDATE, SUSPENDED, RETIRED.** (PAPER and LIVE are
partially approximated by `paper_trade` env flag + live_safety_gate.)

### §28 — Attribution Chain Gaps
- `Strategy → Strategy Version`: **MISSING** (no `strategy_version`
  field)
- `Trade → Feature Snapshot`: **MISSING** (no
  `feature_snapshot_id` FK on `closed_positions`)
- `Trade → Market Snapshot`: **MISSING** (no `market_snapshot_id`
  FK on `closed_positions`)
- `Prediction → Signal` for market_maker and arb_scanner: **MISSING**
  (no PREDICTION/SIGNAL stages recorded)

### §29 — Strategy Metrics Gaps (live, not backtest)
- **Sharpe ratio** — MISSING (backtest only)
- **Sortino ratio** — MISSING (backtest only)
- **Expectancy** (avg_win × win_rate + avg_loss × loss_rate) —
  MISSING as a computed field (raw inputs are present, the
  aggregation is not)
- **Turnover** (notional / capital) — MISSING
- **Capital efficiency** — partial (`profit_per_dollar_exposed` is a
  crude proxy; no risk-adjusted capital efficiency like Calmar or
  MAR)
- **Slippage** — tracked in `core/execution_quality.py` but NOT
  joined into per-strategy leaderboard

### Cross-cutting gaps
- No strategy hot-reload (changing a strategy's parameters requires
  `stop_strategy` + `start_strategy` — see
  `/api/strategy/config/live` GET endpoint at `api/server.py:2054`
  which returns the config but the POST at `:2082` atomically updates
  settings without restarting the strategy task).
- No strategy-level kill switch (risk_manager has a per-strategy
  `_strategy_cooldowns` circuit breaker, but no manual operator
  "pause strategy X" button distinct from `stop_strategy`).
- No A/B strategy comparison at the strategy level (there is
  `ml/ab_testing.py` for ML models, not for strategies).
- No strategy-level alerting (alerts exist for system health, not
  for "strategy X is underperforming").

---

## 11. Bugs

### B1 — A-S inventory skew formula previously multiplied `q` by 0.01
**Severity:** Medium (now fixed; documented in-source at
`market_maker.py:234-237`).
**Status:** VERIFIED fixed. The previous formula
`r = mid - q * 0.01 * γ * σ²` made the inventory skew negligible
(0.01 × 0.08 = 0.0008). The fix removed the extra `0.01` factor.

### B2 — `market_maker` could previously list SELL orders for shares not held
**Severity:** High (now fixed; documented at `market_maker.py:275-278`).
**Status:** VERIFIED fixed. The previous `max(1.0, self._quote_size /
ask_price)` floor could over-size the ask. The fix bounds
`ask_size = min(max(1.0, self._quote_size / ask_price), q)`.

### B3 — Top-3 signals/cycle cap silently drops signals
**Severity:** Low-Medium. `signal_trader._scan_markets` keeps only
`signals[:3]` per 15 s scan; the dropped signals are never recorded
in the decision ledger (no `record_rejection` for the cap). This
understates `signal_trader.evaluations` vs `signal_trader.signals`
observability metrics and means high-conviction signals can be
silently lost if 4+ arrive in the same scan window. **VERIFIED** at
`signal_trader.py:163`. Not yet fixed.

### B4 — `_emit_ledger` returns silently if no running loop
**Severity:** Low. `signal_trader._emit_ledger` swallows the case
where `asyncio.get_event_loop()` returns a stopped loop or raises
`RuntimeError`. This means decision-ledger writes are silently
dropped if the strategy is called outside an asyncio context (e.g.,
in tests calling `_ml_signal` directly without an event loop).
**VERIFIED** at `signal_trader.py:215-225`. Acceptable for production
(loop is always running) but is a foot-gun for testing.

### B5 — `existing_exposure` argument to `allocate_capital` is a hack
**Severity:** Medium. `signal_trader.py:368-373` constructs a
synthetic empty position object via
`type(store.positions.get(token_id, None)).__new__(...)` when a
position exists, but actually returns `0.0` for that case because the
inline `if token_id in store.positions else 0.0` short-circuits the
synthetic object. The synthetic-object construction is dead code —
the `if` always evaluates the `else` branch when there's no position,
and the `if` branch calls `.total_invested` on the real position.
**VERIFIED** at `signal_trader.py:367-373`. Confusing but not
incorrect; the synthetic-object expression is unreachable.

### B6 — `default_enabled` field is never read
**Severity:** Low. Catalog `default_enabled=True` on 3 entries is
decorative — `api/server.py:420-422` hardcodes the same 3 ids
literally. Adding a 4th real strategy with `default_enabled=True`
will NOT auto-start it. **VERIFIED.**

### B7 — Backtest dispatch by substring, not by strategy class
**Severity:** Medium. `backtesting/engine.py:131-149` dispatches on
`if "mm" in strategy_id elif "arb" in strategy_id elif "mom" in
strategy_id elif "ml" in strategy_id`. A catalog id like
`mm_glft_optimal` (Group A, second entry) would dispatch to the
market-maker backtest profile even though `mm_glft_optimal` is not
implemented. A catalog id like `ml_qlearning_execution` would dispatch
to the ML profile. This means backtests for the 47 stub strategies
return plausible-looking results that don't reflect any real
implementation. **VERIFIED.**

---

## 12. Technical Debt

1. **The 47-stub strategy catalog is the largest single source of
   technical debt.** It exists because someone wrote the catalog
   first (50 entries across 6 groups, all the fancy names), then
   implemented only 3. Either (a) delete the 47 stubs and shrink the
   catalog to the 3 real strategies, or (b) implement the 47 stubs.
   The current state — 47 fake entries that look real in the API —
   is the worst of both worlds.

2. **No unified strategy contract.** Every new strategy reimplements
   sizing, entry, exit, diagnostics from scratch with no shared
   signature. This makes it impossible to write generic
   strategy-level tooling (a generic strategy visualizer, a generic
   strategy A/B comparator, a generic strategy backtest runner that
   replays the live code path).

3. **The `_emit_ledger` fire-and-forget pattern swallows errors.**
   `signal_trader._emit_ledger` uses `asyncio.ensure_future(coro)`
   and catches all exceptions at scheduling time. If the coro itself
   raises after scheduling, the exception is logged at error level by
   the ledger's own try/except, but never surfaces to the strategy.
   This is intentional (the strategy's scan cadence must never block
   on SQLite I/O) but means ledger writes can silently fail.

4. **`feature_snapshot` and `market_snapshots` tables exist but are
   orphaned.** Two migrations define these tables, but no production
   code writes to them in a way that links back to trades. The
   schema is there; the wiring is not.

5. **`signal_trader._market_cache` is bounded but `_feature_cache`
   eviction policy is FIFO, not LRU.** `OrderedDict.popitem(last=False)`
   evicts the oldest-inserted entry, not the least-recently-used.
   For a hot market (frequently updated), this means the cached
   features for the most-active market can be evicted in favor of a
   stale one. **VERIFIED** at `signal_trader.py:192-197`. Low impact
   (feature extraction is cheap) but suboptimal.

6. **`market_maker` and `arb_scanner` have no `_emit_rejection`
   equivalent.** `signal_trader` records every rejection (low
   confidence, wide spread, neutral zone, insufficient Kelly,
   allocator-zero) to the decision ledger; the other two strategies
   do not. This means a market_maker that decides not to quote
   (e.g., because the book is one-sided or mid is extreme) leaves no
   audit trail. **VERIFIED.**

7. **The `QuantStrategyInstance` class is dead-but-not-deleted code.**
   It exists only to provide a `_run()` loop for the 47 stub
   strategies. If the stubs are removed (recommended), the entire
   class can be deleted, simplifying the registry.

---

## 13. Data Problems

1. **No `strategy_version` is ever recorded.** Every closed position
   has `strategy` (a string like `"signal_trader"` or
   `"market_maker"` or `"manual"`) and `model_version` (the ML
   model's version string), but no `strategy_version`. If the
   `signal_trader` code is modified (e.g., the Kelly fraction changes
   from 0.25 to 0.20), there is no way to distinguish pre-change
   trades from post-change trades in the journal. **VERIFIED.**

2. **`closed_positions.decision_id` is sometimes empty.** Manual
   trades from `/api/trade` and `/api/position/close` create
   closed-position rows with `strategy="manual"` /
   `strategy="manual_close"` and `decision_id=""` (the default).
   These rows are technically orphan chains — they have no
   PREDICTION/SIGNAL/RISK_APPROVED/ORDER/FILL ancestry. They show up
   in `by_strategy` attribution as `"manual"` buckets, which is
   correct, but they pollute the "no upstream chain" diagnostic
   surface. **VERIFIED** at `api/server.py:2267, 2510, 2612, 2632`.

3. **`feature_snapshot` and `market_snapshots` tables have no FK to
   trades.** Per §28, a trade should link to the feature snapshot
   used at signal time and the market snapshot at execution time.
   Neither FK exists. **VERIFIED.**

4. **No retention policy on `decision_events`.** The table grows
   unboundedly (every PREDICTION for every scanned market is
   recorded). At ~800 markets × 4 scans/min × 1440 min/day = ~4.6 M
   rows/day. The `core/retention.py` module exists but its scope
   (audit_trail, closed_positions, decision_events, etc.) needs
   verification per-table. **LIKELY** — retention module exists, but
   per-table retention enforcement not verified in this assessment.

5. **The arb scanner's `_ml_arb_suspicion` filter uses a different
   feature-extraction call than `signal_trader`.** Both call
   `extract_features(mkt_data, book)`, but `arb_scanner` looks up
   `mkt_data = market_discovery.catalog.get(token_id)` while
   `signal_trader` uses the catalog iterator's `mkt` directly. If
   `market_discovery.catalog` is updated between the two calls, they
   see different market data. Low-likelihood race but possible.
   **LIKELY.**

6. **`closed_positions` records `confidence` and `predicted_edge`
   only for `signal_trader` trades.** `market_maker` and `arb_scanner`
   go through `BaseStrategy.submit_order`, which doesn't pass
   `confidence` or `predicted_edge` to the closed-positions recorder
   (only `decision_id`). The `by_confidence_bucket` and
   `by_edge_bucket` attribution dimensions therefore have a
   `NULL`/`unknown` bucket inflated by MM and arb trades.
   **VERIFIED** — `submit_order` does not accept `confidence` /
   `predicted_edge` kwargs.

---

## 14. Performance Problems

1. **`closed_positions._all_rows()` is capped at 10,000 rows.**
   `core/attribution.py:265-270` fetches up to 10,000 closed
   positions per `get_full_attribution()` call. For a mature
   deployment with > 10,000 closed trades, the attribution roll-up
   silently truncates the oldest trades. W11-9 collapsed 7 SELECTs
   into 1, but the row cap is still a correctness cliff. **VERIFIED.**

2. **`signal_trader` evaluates every market in `market_discovery.catalog`
   on every 15 s scan.** With 800+ markets, that's ~50
   `extract_features + ml_model.predict` calls per second. The ML
   predict is the hot path; whether it's vectorized depends on
   `ml/model.py`. **LIKELY** — `extract_features` is documented as
   pure-Python in `ml/features.py`; the predict path's throughput
   was not benchmarked in this assessment.

3. **`market_maker` re-quotes on every 4 s cycle when mid moves
   > 0.4 %.** In high-volatility markets, this can trigger
   cancel-and-replace on every cycle, generating API rate-limit
   pressure on Polymarket's CLOB. The `circuit_breaker` in
   `risk/manager.py` mitigates this, but the MM strategy itself has
   no rate-limit-aware backoff. **LIKELY.**

4. **The 7-dimension attribution cache TTL is 60 s.** A new closed
   position invalidates the cache, so under active trading the cache
   hit rate may be near zero. **VERIFIED** at `attribution.py:485-496`.

5. **`leaderboard()` walks `store.trades` in Python** (not SQL).
   `strategy_stats(strategy)` filters `store.trades` by strategy,
   then computes win_rate / profit_factor / max_drawdown in a Python
   loop. For 10,000+ trades this is O(N) per strategy per call.
   **VERIFIED** at `core/portfolio.py:159-212`.

---

## 15. Reliability Problems

1. **`signal_trader._emit_ledger` swallows all errors silently.** If
   the decision ledger's SQLite DB is unwritable (e.g., disk full),
   every PREDICTION/SIGNAL/record_rejection call fails, but the
   strategy keeps running. The errors are logged at debug level (not
   warning/error), so they may go unnoticed. **VERIFIED** at
   `signal_trader.py:215-225`.

2. **`market_maker._discover_markets` falls back to sleeping 5 s and
   retrying once.** If the Gamma API is down at startup, the MM
   strategy silently no-ops after one retry. There's no exponential
   backoff, no operator alert. **VERIFIED** at `market_maker.py:65-73`.

3. **`arb_scanner._build_market_pairs` has the same one-retry pattern.**
   If pair discovery fails twice, the strategy logs and continues
   with whatever pairs it had (possibly zero). **VERIFIED** at
   `arb_scanner.py:47-58`.

4. **Strategy tasks are `asyncio.create_task`-created with no
   supervision.** If a strategy's `_run()` raises an unhandled
   exception, the task dies silently — only an error log line is
   emitted. There's no watchdog that restarts a crashed strategy.
   The `watchdog.beat("strategy_registry")` call at startup only
   confirms the registry initialized; it does not monitor per-strategy
   task liveness. **VERIFIED** at `base.py:36-39` and
   `api/server.py:423`.

5. **`signal_trader._ml_signal` does not handle `ml_model.predict`
   raising.** If `predict` raises (e.g., feature shape mismatch,
   model not fitted), the exception propagates up to `_scan_markets`,
   which catches at the per-market level (`signal_trader.py:147`).
   So a single broken market breaks only that iteration, not the
   scan. **VERIFIED** — acceptable, but the broken market is
   silently skipped with no ledger entry.

6. **`paper/simulator._execute_fill` is the single point that records
   `FILL` stage + `closed_positions`.** If `paper_sim` crashes or
   its DB is unwritable, the ORDER stage exists but no FILL stage
   and no closed-position row appear. The decision chain breaks at
   ORDER → FILL. **VERIFIED** structurally; production reliability
   depends on `paper_sim`'s error handling, which is out of scope
   here.

---

## 16. Security Problems

1. **No per-strategy authz.** Any authenticated API caller can
   start/stop any strategy via `POST /api/strategy/toggle`. There's
   no "operator can stop arb_scanner but not signal_trader"
   role-based control. **VERIFIED** — `toggle_strategy` at
   `api/server.py:1719` only checks `enforce_api_auth`.

2. **No confirmation prompt for stopping a strategy with open
   orders.** `stop_strategy` cancels the task but does not
   necessarily cancel open orders (the strategy's `stop()` is
   responsible for cleanup, but `BaseStrategy.stop()` only cancels
   the asyncio task, not the open orders). A stopped MM strategy
   could leave resting quotes on the book. **VERIFIED** at
   `base.py:42-51` — `stop()` does not call `cancel_order` for any
   open orders.

3. **The `mm_token_ids_list` env var is a manual override** that
   bypasses the auto-discovery path. If a malicious operator sets
   `MM_TOKEN_IDS` to a token they control, the MM strategy will
   quote that token indefinitely (providing liquidity the operator
   can adversarially fill against). **VERIFIED** at
   `market_maker.py:130-131`.

4. **Strategy code is imported at runtime via lazy imports** (e.g.,
   `from strategies.market_maker import MarketMakerStrategy` inside
   `start_strategy`). If an attacker could write to the
   `strategies/` directory (e.g., via a path-traversal in another
   subsystem), they could inject a malicious strategy class.
   **LIKELY** — runtime import is a common Python pattern; not a
   vulnerability per se but increases blast radius of any
   file-write vulnerability elsewhere.

---

## 17. Testing

### 17.1 Strategy test coverage (VERIFIED)
| Test file | LOC | Tests | Status |
|---|---:|---:|---|
| `tests/test_strategy_base.py` | 449 | 6 | ✅ pass |
| `tests/test_signal_trader.py` | 490 | 8 | ✅ pass |
| `tests/test_market_maker.py` | 591 | 12 | ✅ pass |
| `tests/test_arb_scanner.py` | 537 | 9 | ✅ pass |
| **Total** | **2,067** | **35** | **35/35 pass** |

Command: `python -m pytest tests/test_strategy_base.py
tests/test_signal_trader.py tests/test_market_maker.py
tests/test_arb_scanner.py -q --no-header -p no:warnings` → 35 passed.

### 17.2 Attribution / decision-ledger / closed-positions tests
- `tests/test_attribution.py` — 729 LOC, covers the 7 attribution
  dimensions + `get_full_attribution` payload shape + `profit_factor`
  divide-by-zero guard + empty-trades contract.
- `tests/test_decision_ledger.py` — covers all 6 public methods
  (`new_decision_id`, `record`, `get_chain`, `get_chain_by_token`,
  `record_rejection`, `get_rejections`).
- `tests/test_closed_positions.py` — covers closed-position
  recording.
- `tests/test_e2e_decision_chain.py` — integration test for the full
  PREDICTION → FILL chain. **VERIFIED** file exists.

### 17.3 Backtest tests
- `tests/test_backtest_engine.py` — 6 contract requirements +
  2 bonus tests for the realistic-backtest surface.
- `tests/test_backtest_report.py` — pre-existing failure noted in
  W16-7 worklog (VaR-95 calculation assertion). NOT introduced by
  this assessment.

### 17.4 Test gaps
1. **No test for the 47 stub strategies.** `QuantStrategyInstance`
   has no tests. Starting a stub strategy is not asserted to be a
   no-op (or to be rejected, if that's the desired behavior).
2. **No test for the `market_maker` and `arb_scanner` decision-ledger
   gap.** There's no test asserting that MM and arb fills have a
   chain starting at `RISK_APPROVED` (the missing PREDICTION/SIGNAL
   stages).
3. **No test for `strategy_version` (because the field doesn't
   exist).**
4. **No test for the 10,000-row attribution cap.**
5. **No test for the top-3 signals/cycle cap** — that the 4th
   high-conviction signal is silently dropped.
6. **No integration test that the live-safety gate blocks LIVE
   enable before 24 h of paper trading** (the test exists in
   `tests/test_live_safety_gate.py` but its coverage of the
   PAPER → LIVE transition specifically is partial).

---

## 18. Observability

### 18.1 Per-strategy observability metrics (VERIFIED)
Emitted via `core.observability.record_metric(category, name, value)`:

| Strategy | Metric | Source |
|---|---|---|
| `signal_trader` | `strategy.signal_trader.evaluations` | `signal_trader.py:153` |
| `signal_trader` | `strategy.signal_trader.signals` | `signal_trader.py:154` |
| `signal_trader` | `strategy.signal_trader.rejected` | `signal_trader.py:155` |
| `market_maker` | `strategy.market_maker.quotes_active` | `market_maker.py:93` |
| `arb_scanner` | `strategy.arb_scanner.pairs_scanned` | `arb_scanner.py:123` |
| `arb_scanner` | `strategy.arb_scanner.opportunities` | `arb_scanner.py:124` |
| `arb_scanner` | `strategy.arb_scanner.rejected` | `arb_scanner.py:125` |

### 18.2 Per-strategy API endpoints (VERIFIED)
- `GET /api/strategy/catalog` — 50 entries, `implemented` boolean
  distinguishes real from stub.
- `POST /api/strategy/toggle` — start/stop a strategy.
- `GET /api/leaderboard` — per-strategy `strategy_stats` (30 s cache).
- `GET /api/attribution` — 7-dimension roll-up (60 s cache).
- `GET /api/positions/closed` — closed positions feed (filterable
  by strategy).
- `GET /api/positions/closed/stats` — aggregate P&L / win-rate /
  profit-factor.
- `GET /api/decision/{token_id}` — recent decision events for a
  token.
- `GET /api/decisions/rejected` — recent rejected decisions.
- `GET /api/v2/decisions/recent` — async DB-pool version (W16-7).
- `GET /api/v2/observability/latest` — async DB-pool version (W16-7).

### 18.3 Observability gaps
1. **No per-strategy Prometheus metric for P&L.** `prometheus_metrics.py`
   exists but its strategy-scoped surface was not verified to include
   per-strategy realized P&L gauge. **UNVERIFIED** — module exists,
   contents not inspected in this assessment.
2. **No structured strategy log format.** Strategies log via
   `log = logging.getLogger(__name__)` with free-form messages like
   `"📊 Market Maker active — quoting N liquid market(s)"`. There's
   no JSON-structured strategy event format for downstream log
   aggregation. **VERIFIED** — log calls are human-readable strings.
3. **No strategy-level alerting.** `core/alerting.py` exists but
   alert rules are system-level (circuit breaker trips, drawdown
   breaches), not strategy-level ("signal_trader win rate < 40 % over
   last 100 trades"). **LIKELY.**

---

## 19. Production Readiness

### 19.1 Ready for production
- **`signal_trader`** — production-quality: bounded caches, decision-
  ledger integration on every gate, capital allocator with safety
  gates, online learning loop. The 15 s cadence is appropriate for
  Polymarket binary markets.
- **`market_maker`** — production-quality with caveats: A-S formula
  is correct after the bug fix, inventory flush prevents adverse
  selection, ML-coupled spread adjustment is sound. Caveat: no
  rate-limit-aware backoff (see §14.3).
- **`arb_scanner`** — production-quality with staleness + depth +
  ML-suspicion guards. FOK orders self-settle.
- **Decision ledger + closed positions + attribution** — solid,
  tested, single-SELECT optimized.
- **Live safety gate** — real PAPER → LIVE gate with 24 h / positive
  expectancy / MDD < $2 checks.

### 19.2 Not ready for production
- **47 catalog stub strategies** — should NOT be exposed via
  `/api/strategy/catalog` in a production deployment. Operators
  seeing "Q-Learning Execution Agent" in the catalog may try to start
  it, see `is_running: true` in the response, and assume it's
  trading. **Recommendation:** either delete the stubs or filter them
  out of `get_catalog()` until implemented.
- **Backtest dispatch by substring** — backtests for the 47 stubs
  return plausible-looking results that don't reflect any real
  implementation. Misleading for any operator running a backtest
  on `ml_qlearning_execution` and trusting the Sharpe number.
- **No `strategy_version`** — without it, post-hoc analysis of
  "trades taken before vs after the Kelly fraction change" is
  impossible.

### 19.3 Production deployment checklist (recommendations)
1. Delete the 47 stub entries from `STRATEGY_CATALOG` (or hide them
   behind a `--include-stubs` flag).
2. Add `strategy_version` field to `closed_positions` and
   `decision_events`, populated from `BaseStrategy.VERSION` class
   attribute.
3. Add `feature_snapshot_id` and `market_snapshot_id` FKs to
   `closed_positions`.
4. Emit `PREDICTION` / `SIGNAL` stages from `market_maker` and
   `arb_scanner` (or document that those strategies skip these
   stages by design).
5. Implement the §26 unified strategy contract (at minimum:
   `metadata()`, `generate_signal()`, `size_position()`,
   `diagnostics()`).
6. Add per-strategy Sharpe / Sortino / expectancy to
   `strategy_stats` (reusing the `backtesting/engine.py` math).
7. Add a strategy-liveness watchdog that restarts crashed strategy
   tasks.
8. Add per-strategy alerting ("strategy X has 0 fills in last 1 h",
   "strategy X win rate < 30 % over last 50 trades").

---

## 20. Evidence

| Claim | Classification | Source |
|---|---|---|
| `STRATEGY_CATALOG` has 50 entries across 6 groups | **VERIFIED** | `strategies/registry.py:30-92` (counted 8+8+8+8+8+10=50 entries) |
| Only 3 strategies are `implemented=True` | **VERIFIED** | `strategies/registry.py:134` (`implemented = {"mm_avellaneda_stoikov", "arb_binary_dutch_book", "ml_random_forest_quant"}`) |
| 47 catalog entries are stubs | **VERIFIED** | `strategies/registry.py:117-119` (`QuantStrategyInstance._execute_cycle` is `pass`) |
| `BaseStrategy` lacks §26 contract methods | **VERIFIED** | `strategies/base.py:19-148` — only `start/stop/_run/submit_order/cancel_order` |
| No `strategy_version` field anywhere in `*.py` | **VERIFIED** | `rg "strategy_version\|strat_version"` against `mini-services/polymarket-bot/` returned no `.py` matches |
| No §27 lifecycle state names (RESEARCH/EXPERIMENTAL/...) | **VERIFIED** | `rg "RESEARCH\|EXPERIMENTAL\|BACKTESTED\|VALIDATED\|LIVE_CANDIDATE\|SUSPENDED\|RETIRED"` against `strategies/, core/, risk/, api/` returned no matches |
| Only `signal_trader` emits `PREDICTION` / `SIGNAL` ledger stages | **VERIFIED** | `rg "STAGE_PREDICTION\|STAGE_SIGNAL"` against `strategies/` returned matches only in `signal_trader.py` |
| `closed_positions` schema has 18 columns including `model_version`, `decision_id`, `confidence`, `predicted_edge`, `p_yes`, `market_mid`, `liquidity` | **VERIFIED** | `core/closed_positions.py:23-37` |
| `feature_snapshot` and `market_snapshots` tables exist in migrations but are not linked to trades | **VERIFIED** | `core/db/migrations/001_initial_enterprise_schemas.sql:256`, `001_initial_schema.sql:247`; no FK from `closed_positions` |
| Backtest dispatch is by substring, not by strategy class | **VERIFIED** | `backtesting/engine.py:131-149` (`if "mm" in strategy_id elif "arb" in strategy_id ...`) |
| 35 strategy tests pass | **VERIFIED** | `pytest tests/test_strategy_base.py tests/test_signal_trader.py tests/test_market_maker.py tests/test_arb_scanner.py -q` → 35 passed |
| `strategy_stats` returns 14 fields including `win_rate`, `profit_factor`, `max_drawdown`, `avg_holding_duration_hours` | **VERIFIED** | `core/portfolio.py:194-212` |
| Live `strategy_stats` does NOT compute Sharpe / Sortino / expectancy / turnover | **VERIFIED** | `core/portfolio.py:159-212` — no Sharpe/Sortino/expectancy/turnover in return dict |
| `BacktestResult` computes Sharpe, Sortino, Calmar, VaR-95, Brier, MDD, profit_factor, win_rate | **VERIFIED** | `backtesting/engine.py:11-30` (constructor signature) |
| `signal_trader` records 4 rejection paths to decision_ledger | **VERIFIED** | `strategies/signal_trader.py:292-392` (`low_confidence`, `wide_spread`, `neutral_zone`, `insufficient_kelly_edge`, `capital_allocator_zero`) |
| `live_safety_gate` enforces 24 h paper mode + positive expectancy + MDD < $2 | **VERIFIED** | `core/live_safety_gate.py:77-79` (`CHECK_PAPER_MODE`, `CHECK_POSITIVE_EXPECTANCY`, `CHECK_MAX_DRAWDOWN`); `:203` (`PAPER_MODE_MIN_SECONDS = 24*3600`) |
| `signal_trader` top-3 cap silently drops signals | **VERIFIED** | `strategies/signal_trader.py:163` (`for sig in signals[:3]`) |
| `signal_trader._emit_ledger` swallows errors silently | **VERIFIED** | `strategies/signal_trader.py:215-225` |
| Attribution cache TTL is 60 s, invalidated on `POST /api/trade` | **VERIFIED** | `core/attribution.py:485-496`; `api/server.py:2295-2303` |
| `closed_positions._all_rows()` capped at 10,000 rows | **VERIFIED** | `core/attribution.py:265-270` |
| `_ml_signal` is sync and uses `asyncio.ensure_future` for ledger writes | **VERIFIED** | `strategies/signal_trader.py:204-225, 253` |
| `signal_trader.record_outcome` is called from `paper/simulator.py` on settlement | **VERIFIED** | `strategies/signal_trader.py:476-483`; cross-ref caller via grep |

---

## 21. Unknowns

1. **What does `core/prometheus_metrics.py` actually expose at the
   per-strategy granularity?** Module exists, contents not inspected
   in this assessment. If it already exposes `signal_trader_pnl_total`
   etc., the §29 Sharpe/Sortino gap may be smaller than claimed.
   **UNVERIFIED.**

2. **What's the actual production retention policy on
   `decision_events`?** `core/retention.py` exists but its per-table
   enforcement was not verified. **UNVERIFIED.**

3. **Does the `ml/feature_store.py` (W16-2 task, mentioned in W16-7
   worklog as concurrent subagent work) close the §28
   Feature-Snapshot gap?** The W16-2 task may have added the
   `feature_snapshot_id` FK that this assessment flags as missing.
   **UNVERIFIED** — the W16-7 worklog mentions W16-2 was concurrent,
   but the merged state was not inspected.

4. **Does `risk_manager._strategy_cooldowns` count as a §27
   SUSPENDED state?** It's a runtime circuit-breaker (per-strategy
   cooldown after consecutive losses), not a lifecycle state. If
   the platform owner considers it sufficient as a "SUSPENDED"
   proxy, the §27 gap is smaller than flagged. **LIKELY** —
   semantically distinct but operationally similar.

5. **Does the `shadow_trading.py` subsystem implement a §27
   PAPER-vs-LIVE comparison at the strategy level?** Module exists,
   not inspected. **UNVERIFIED.**

6. **What's the `mm_glft_optimal` strategy's intended behavior?**
   The catalog describes "GLFT Optimal Quoter" (Gueant-Tapia-Manziadi
   intensity-based optimal quote spread) but the implementation is
   the no-op stub. Is there a design doc or a partial implementation
   somewhere not in the strategies/ directory? **UNVERIFIED.**

---

## 22. Maturity Score

**Score: 4.5 / 10**

### Scoring breakdown
| Dimension | Score | Rationale |
|---|---:|---|
| Strategy inventory completeness (§25) | 3 / 10 | 3 of 50 advertised strategies implemented; catalog materially misrepresents capability |
| Strategy contract uniformity (§26) | 1 / 10 | §26 contract entirely absent; each strategy inlines its own logic |
| Registry metadata + lifecycle (§27) | 2 / 10 | 5 of 15 §27 fields present; 0 of 9 lifecycle states implemented |
| Attribution chain completeness (§28) | 5 / 10 | decision_id → model_version → closed_positions chain works for signal_trader; missing strategy_version, feature_snapshot_id, market_snapshot_id; broken for market_maker/arb_scanner |
| Strategy metrics coverage (§29) | 6 / 10 | Live: win_rate, profit_factor, max_drawdown, capital efficiency (partial), avg holding, slippage (separate). Missing: live Sharpe, Sortino, expectancy, turnover. Backtest: full Sharpe/Sortino/Calmar/VaR/Brier |
| Strategy code quality (3 real strategies) | 8 / 10 | Well-engineered, tested, in-source documentation of past bugs and fixes |
| Decision-ledger + attribution plumbing | 8 / 10 | Sound architecture, W11-9 optimization, tested |
| Live safety / production hardening | 7 / 10 | Real PAPER → LIVE gate, positive expectancy check, MDD check |
| Test coverage | 7 / 10 | 2,067 LOC of strategy tests + 729 LOC attribution tests; gaps for stubs, lifecycle, version |
| Observability | 6 / 10 | Per-strategy metrics emitted, 7-dim attribution endpoint, no per-strategy Prometheus P&L gauge (unverified) |

**Weighted average ≈ 4.5 / 10.**

The platform is **production-usable for the 3 real strategies** but
**materially misrepresents its capability** through the 47-stub
catalog. The §26–28 gaps (no contract, no version, no snapshot FKs)
are the highest-leverage improvements.

---

## 23. Critical Findings

### CF1 — 47 of 50 advertised strategies are non-functional stubs
**Severity:** Critical (credibility / operational risk).
**Evidence:** `strategies/registry.py:117-119` —
`QuantStrategyInstance._execute_cycle()` is `pass`. `get_catalog()`
returns `implemented=True` for only 3 ids (`registry.py:134`).
**Impact:** An operator calling `POST /api/strategy/toggle` with
`"ml_qlearning_execution"` will receive `{"status": "started",
"strategy": "ml_qlearning_execution"}` and the strategy will appear
in `GET /api/strategy/catalog` with `is_running: true`. The
strategy will never produce a trade. If the operator doesn't notice
the missing `implemented: false` flag, they may believe they have a
Q-learning execution agent running.
**Recommendation:** Delete the 47 stub entries from
`STRATEGY_CATALOG`, OR filter `get_catalog()` to only return
`implemented=True` entries, OR raise a 400 error from
`toggle_strategy` when the requested id is not in the
`implemented` set.

### CF2 — No `strategy_version` field; the §28 attribution chain is broken at "Strategy → Strategy Version"
**Severity:** High (post-hoc analysis impossible).
**Evidence:** `rg "strategy_version|strat_version"` against all
`*.py` files in `mini-services/polymarket-bot/` returns no matches.
`closed_positions.py:23-37` schema has `model_version` (the ML model
version) but no `strategy_version` (the strategy code version).
**Impact:** If `signal_trader`'s Kelly fraction is changed from 0.25
to 0.20, every closed trade before and after the change is tagged
with `strategy="signal_trader"` and the same `model_version`. There
is no way to distinguish pre-change trades from post-change trades,
so A/B analysis of the parameter change is impossible.
**Recommendation:** Add a `VERSION` class attribute to
`BaseStrategy` (e.g., `VERSION = "1.0.0"`), override in each
subclass, and persist it on every `closed_positions` row and every
`decision_events` row at PREDICTION/SIGNAL time.

### CF3 — `market_maker` and `arb_scanner` do not emit PREDICTION / SIGNAL stages
**Severity:** Medium-High (§28 chain broken for 2 of 3 strategies).
**Evidence:** `rg "STAGE_PREDICTION|STAGE_SIGNAL"` against
`strategies/` returned matches only in `signal_trader.py`.
`market_maker.submit_order` and `arb_scanner.submit_order` (both
inherited from `BaseStrategy`) record `RISK_APPROVED` / `FILL` but
no upstream `PREDICTION` / `SIGNAL`.
**Impact:** A market-maker fill's decision chain starts at
`RISK_APPROVED`, not at `PREDICTION`. The §28 chain
(`Signal → Prediction`) has a hole for 2 of 3 strategies.
**Recommendation:** Either (a) emit `PREDICTION` / `SIGNAL` stages
from MM and arb (with appropriate edge/confidence semantics — for
MM, the reservation-price skew could be the "signal"; for arb, the
profit-per-share is the "edge"), or (b) document that those
strategies intentionally skip these stages and adjust the §28
contract accordingly.

### CF4 — `feature_snapshot` and `market_snapshots` tables exist but are orphaned
**Severity:** Medium (the schema is ready, the wiring is missing).
**Evidence:** Both tables are defined in migrations
(`001_initial_enterprise_schemas.sql:256`, `001_initial_schema.sql:247`)
and `core/market_db.py` writes to `market_snapshots`. But
`closed_positions` has no `feature_snapshot_id` /
`market_snapshot_id` FK, and `decision_events.data_json` does not
reference snapshot ids.
**Impact:** The §28 chain's last two links (`Feature Snapshot`,
`Market Snapshot`) cannot be reconstructed for any trade. Even
though the snapshot data may exist on disk, there's no foreign-key
path from a trade back to its snapshot.
**Recommendation:** Add `feature_snapshot_id` and
`market_snapshot_id` columns to `closed_positions`. Have
`signal_trader._ml_signal` capture the snapshot ids at signal time
and propagate them through `submit_order` → `paper_sim` →
`closed_positions.record`.

### CF5 — No unified strategy contract (§26)
**Severity:** Medium (architectural debt; future strategies will
reimplement the same logic with no shared signature).
**Evidence:** `strategies/base.py:19-148` exposes only `start/stop/
_run/submit_order/cancel_order`. None of the §26 methods
(`metadata/configure/validate/generate_signal/estimate_edge/
size_position/entry_logic/exit_logic/diagnostics`) exist.
**Impact:** Every new strategy reimplements sizing, entry, exit,
diagnostics from scratch. There's no generic strategy visualizer,
no generic A/B comparator, no generic backtest runner that replays
the live code path.
**Recommendation:** Define an abstract `StrategyContract` ABC with
the 9 §26 methods. Refactor the 3 real strategies to implement it.
Add a test that asserts every concrete strategy class implements
all 9 methods.

### CF6 — Live Sharpe / Sortino / expectancy / turnover not computed
**Severity:** Medium (operators lack standard risk-adjusted
performance metrics for live trading).
**Evidence:** `core/portfolio.py:159-212` returns 14 fields, none
of which are Sharpe / Sortino / expectancy / turnover. The math
for all four exists in `backtesting/engine.py` but is not reused
in live `strategy_stats`.
**Impact:** The `/api/leaderboard` endpoint ranks strategies by a
custom `risk_adjusted_score` (net P&L − penalties) that is not
comparable to industry-standard Sharpe / Sortino. An operator
familiar with Sharpe-based ranking cannot use the leaderboard
directly.
**Recommendation:** Extract the Sharpe / Sortino / expectancy /
turnover math from `backtesting/engine.py` into a shared
`core/performance_metrics.py` module, and call it from both
`strategy_stats` (live) and `BacktestResult` (backtest).

### CF7 — Backtest dispatch by substring, not by strategy class
**Severity:** Medium (backtest results for the 47 stub strategies
are misleading).
**Evidence:** `backtesting/engine.py:131-149` —
`if "mm" in strategy_id: ... elif "arb" in strategy_id: ...
elif "mom" in strategy_id: ... elif "ml" in strategy_id: ...`.
**Impact:** A backtest of `ml_qlearning_execution` returns a
plausible-looking Sharpe / Sortino / MDD that has no relationship
to any real implementation.
**Recommendation:** Replace the substring dispatch with a
`strategy_registry.get_strategy_class(strategy_id)` lookup that
returns `None` for stubs. Refuse to backtest unimplemented
strategies with a 400 error.

### CF8 — No strategy-liveness watchdog
**Severity:** Medium (a crashed strategy is not restarted).
**Evidence:** `strategies/base.py:36-39` — `start()` creates an
`asyncio.create_task` with no supervision. The `watchdog.beat(
"strategy_registry")` at `api/server.py:423` only confirms
registry initialization, not per-strategy task liveness.
**Impact:** If `signal_trader._run()` raises an unhandled
exception (e.g., `ml_model.predict` raises a TypeError on a
malformed feature vector), the task dies silently. The strategy
appears "running" in `get_active_instances()` (because the
`_instances` dict still has the entry) but is not actually
executing its loop.
**Recommendation:** Add a strategy-liveness watchdog that checks
each strategy task's `done()` state every N seconds and either
restarts the strategy or alerts the operator.

### CF9 — Top-3 signals/cycle cap silently drops signals
**Severity:** Low-Medium (high-conviction signals can be lost).
**Evidence:** `signal_trader.py:163` (`for sig in signals[:3]`);
`arb_scanner.py:131` (`for opp in opportunities[:3]`).
**Impact:** If 10 high-conviction signals arrive in the same 15 s
scan, 7 are dropped with no `record_rejection` and no observability
metric. Under sustained high-signal regimes, the strategy
systematically under-trades.
**Recommendation:** Either (a) raise the cap to a configurable
`MAX_SIGNALS_PER_CYCLE` setting, or (b) record a
`signal_trader.dropped` metric and a `record_rejection(...,
reason="cycle_cap")` for the dropped signals so the drop is
auditable.

### CF10 — `closed_positions` attribution columns populated only for `signal_trader` trades
**Severity:** Medium (attribution dimensions are biased).
**Evidence:** `BaseStrategy.submit_order` does not accept
`confidence` / `predicted_edge` / `p_yes` / `market_mid` /
`liquidity` kwargs. Only `signal_trader._ml_signal` →
`MarketSignal.decision_id` propagates these through to the
`SIGNAL` ledger stage, and `paper/simulator._execute_fill` is the
single point that writes to `closed_positions` (it would need to
join back to the SIGNAL stage's `data_json` to recover these
fields for MM/arb trades).
**Impact:** The `by_confidence_bucket`, `by_edge_bucket`,
`by_probability_band`, `by_liquidity_level` attribution dimensions
have inflated `unknown` buckets because MM and arb trades have
NULL `confidence` / `predicted_edge` / `p_yes` / `liquidity` in
`closed_positions`.
**Recommendation:** Either (a) extend `submit_order` to accept
these kwargs and persist them, or (b) have
`paper/simulator._execute_fill` join the SIGNAL stage's
`data_json` to recover them at fill time.

---

**End of Strategy Management Assessment — W17-5.**
