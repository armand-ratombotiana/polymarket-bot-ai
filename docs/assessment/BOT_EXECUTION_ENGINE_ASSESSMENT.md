# Bot & Execution Engine Assessment — W17-2

**Service:** `mini-services/polymarket-bot`
**Scope per God Mode Master Prompt:** §7 (Bot Lifecycle), §8 (Order Management System), §9 (Execution Quality), §10 (Execution Safety).
**Author:** general-purpose subagent.
**Date:** 2026-09-03.
**Evidence classification used throughout:** `VERIFIED` (file:line cited & read in this session), `STRONG EVIDENCE` (multiple converging citations), `LIKELY` (single citation plus consistent context), `UNVERIFIED` (plausible but not traced in this session), `NOT FOUND` (searched & absent).

---

## 1. Executive Summary

The Polymarket bot is a **paper-mode production-grade system** with an unusually rigorous pre-trade risk gate, a durable kill switch, and a fully traced decision ledger (`PREDICTION → SIGNAL → RISK_APPROVED/REJECTED → ORDER → FILL`). The execution-quality schema is correct in shape and the order state machine is correctly specified in `core/order_state_machine.py`.

However, **the live trading path is not yet safe for real funds** for four structural reasons discovered during this assessment:

1. **The order state machine (`core/order_state_machine.py`) is NOT wired into the production trade path.** It is invoked exactly once — in `paper/simulator.py:139`, on a `CANCELLED` transition, wrapped in `try/except: pass`. The `Order` dataclass in `order_state_machine.py` is a different class than `core.data_store.Order` used by `strategies/base.py`, `paper/simulator.py`, and `risk/manager.py`. The live order lifecycle has no enforced state transitions, no `CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN → FILLED` audit trail.
2. **There is no live fill acknowledgement loop.** Paper fills are detected by a 1-second polling loop (`paper/simulator.py::_fill_loop`) that compares open paper orders against the live order book. For live orders, no equivalent loop exists; `clob_client.get_trades()` exists but is never called from the trade path. There is no CLOB reconciliation: the local `store.positions` and `store.open_orders` dicts can drift indefinitely from exchange truth.
3. **`generate_idempotency_key()` exists but is never consulted on submission.** `core/clob_client.py::create_order` mints a fresh `uuid.uuid4()` order_id and a random 16-byte EIP-712 nonce on every call. A duplicate strategy decision would simply produce a second live order. Duplicate-fill detection is limited to a passive counter in `core/portfolio.py:99` (`duplicate_fill_anomaly = len(trades) - len({t.trade_id for t in trades})`).
4. **The three-tier price waterfall (Theoretical → Executable → Realized Edge) is structurally collapsed.** `record_execution()` is only called from `paper/simulator.py:307`, always with `signal_price=order.price`. The schema column `submitted_price` is hard-coded to `order.price` (`core/execution_quality.py:278`). Therefore `signal_price == decision_price == submitted_price` for every recorded fill — `realized_edge` measures "limit-vs-fill" (i.e. crossing cost / slippage) rather than "signal-vs-fill" (i.e. model edge retention).

Additionally, **the Smart Order Router (`execution/smart_router.py`) with TWAP / VWAP / Iceberg slicing is not wired into the order submission path**. It is invoked only by `core/analysis_engine.py` (for slippage estimation in metrics) and by `POST /api/execution/plan` which returns a plan but does not execute it. The slippage-tolerance gate (`SLIPPAGE_TOLERANCE_HEALTHY_BPS = 15`) is enforced inside `plan_execution`, but since `plan_execution` is never called from `submit_order`, **no live order is ever rejected for slippage**.

Risk controls (§10) are comprehensive on the pre-trade side: 22 institutional gates are enforced by `risk_manager.check_order` on every submission path (strategies/base.py, position_manager TP/SL exits, manual /api/trade, /api/positions/{id}/close, /api/arbitrage/execute). The `core/live_safety_gate.py` 10-check staged gate is correct and well-tested. The live path's deficit is **post-trade** (no reconciliation, no fill ack, no state-machine enforcement) — not pre-trade.

**Maturity score: 6 / 10** (see §22).

---

## 2. Purpose

The bot & execution engine is responsible for the full lifecycle of every prediction-market trade:

1. Discovering markets and ingesting order books.
2. Generating ML signals and selecting strategies.
3. Sizing positions and routing orders (paper or live).
4. Submitting orders to the Polymarket CLOB REST API (or simulating fills against the live book in paper mode).
5. Detecting fills and recording P&L, execution quality, and attribution.
6. Managing exits (TP/SL, manual close, settlement).
7. Enforcing institutional risk gates on every submission path.

This assessment audits the lifecycle (§7), the order management system (§8), the execution quality ledger (§9), and the execution safety gates (§10), per the God Mode Master Prompt.

---

## 3. Current Architecture

### High-level layering (`VERIFIED` from `mini-services/polymarket-bot/` directory listing + `api/server.py:1-130`)

```
┌─ API / WebSocket layer (api/server.py — FastAPI, 5101 lines, 80+ routes)
│   ├─ Manual trade: POST /api/trade
│   ├─ Position close: POST /api/positions/{id}/close
│   ├─ Order management: GET/DELETE /api/orders[/{id}]
│   ├─ Risk controls: POST /api/kill-switch/{activate,deactivate}, /api/risk/observation-mode
│   ├─ Execution planning: POST /api/execution/plan (plan-only; no execution)
│   ├─ Live readiness: GET /api/live/readiness, POST /api/live/enable
│   ├─ Decision ledger: GET /api/decision/{token_id}, /api/decisions/rejected, /api/v2/decisions/recent
│   ├─ Execution quality: GET /api/execution-quality
│   ├─ Attribution: GET /api/attribution
│   └─ WebSocket: /ws (broadcast on trades / orders / positions / alerts / system channels)
│
├─ Strategy layer (strategies/)
│   ├─ base.py::BaseStrategy — submit_order() risk gate + paper/live routing
│   ├─ signal_trader.py::SignalTraderStrategy — ML Random Forest + SGD online ensemble, 15s scan
│   ├─ market_maker.py::MarketMakerStrategy — Avellaneda-Stoikov reservation price, 4s loop
│   └─ arb_scanner.py::ArbScannerStrategy — binary Dutch-Book arbitrage, configurable scan interval
│
├─ Execution layer
│   ├─ paper/simulator.py::PaperSimulator — 1s fill loop, slippage model (1 tick crossing + 0.5 tick / 50-share depth + 0/1 tick queue)
│   ├─ execution/smart_router.py::SmartOrderRouter — TWAP/VWAP/Iceberg slicer (NOT wired to submit_order)
│   ├─ execution/advanced_router.py::AdvancedOrderRouter — strategy recommendation (exposed only via /api/execution/plan)
│   └─ core/clob_client.py::ClobClient — EIP-712 signed POST /order to Polymarket CLOB REST API
│
├─ Risk layer
│   ├─ risk/manager.py::InstitutionalRiskEngine — 22-gate pre-trade check_order()
│   ├─ core/safety.py — durable file-backed kill switch
│   ├─ core/circuit_breaker.py — generic API-call circuit breaker
│   ├─ core/live_safety_gate.py — 10-check staged live-readiness gate
│   └─ core/watchdog.py — heartbeat + tripwire monitor (daily/weekly loss, drawdown, book stall)
│
├─ State layer
│   ├─ core/data_store.py::DataStore — in-memory books/orders/positions/trades + atomic JSON disk persistence
│   ├─ core/order_state_machine.py::OrderStateMachine — SQLite append-only state history (NOT wired to production path)
│   ├─ core/decision_ledger.py::DecisionLedger — SQLite PREDICTION→SIGNAL→RISK_APPROVED→ORDER→FILL stage chain
│   ├─ core/execution_quality.py — SQLite per-fill execution quality ledger
│   ├─ core/closed_positions.py — SQLite closed-position journal
│   └─ core/attribution.py — 7-dimension P&L roll-up
│
├─ Data ingestion layer
│   ├─ core/gamma_client.py — Polymarket Gamma REST client (markets catalog)
│   ├─ core/market_discovery.py::UniversalMarketDiscoveryEngine — 180s full-catalog sync, 2000-market safety ceiling
│   ├─ core/book_poller.py — tiered REST polling (Tier 1: 2s × 50 tokens; Tier 2: 6s × remainder)
│   ├─ core/fundamental_ingest.py — news/sentiment feed
│   └─ core/ws_client.py — WebSocket client (retired per KD-08/KD-24; `subscribe()` had zero callers — D5 decision: tiered REST polling only)
│
└─ ML layer (ml/)
    ├─ model.py — Random Forest + SGD online classifier
    ├─ drift_detector.py — PSI + Brier drift status (HEALTHY / MODERATE_SHIFT / SIGNIFICANT_DRIFT)
    ├─ training_orchestrator.py — drift-triggered + 6h scheduled retraining
    └─ vector_store.py — semantic market search
```

### Concurrency model (`VERIFIED` from `main.py`, `api/server.py:279-540`, `paper/simulator.py:60-75`)

- `asyncio` single event loop per process. All strategy loops, paper sim fill loop, position-manager loop, market-discovery loop, book poller tiers, broadcast loop, status broadcast, market reseed, token sync, state persistence, reconciliation loop, and training orchestrator are `asyncio.create_task` with explicit `name=` for `watchdog` liveness checks.
- A `threading.Lock` (not `asyncio.Lock`) backs the `CircuitBreaker` in `core/circuit_breaker.py:77`. The book poller uses `asyncio.Semaphore(MAX_CONCURRENT=12)` for REST concurrency.
- The `DataStore` uses `asyncio.Lock` for mutation serialization; reads via `store.order_books.get(...)` (plain dict access) deliberately bypass the lock (documented in `core/execution_quality.py:253-257`).
- The lifespan function (`api/server.py:279-540`) is the canonical startup/shutdown sequence — migrations → token-strength check → live-mode guards → timescale pool init → watchdog start → paper sim → market seeding → market discovery → book poller → settlement/fundamental/position-manager → strategy registry (3 base strategies) → training orchestrator → label backfill → background tasks (broadcast, status, reseed, token sync, persist, recon). Shutdown reverses the order with explicit cancellation of each `asyncio.Task`.

### Deployment model (`VERIFIED` from `Dockerfile`, `supervisord.conf`, `main.py:283-311`)

- `main.py serve` → `uvicorn.run("api.server:app", ...)`. Docker image runs `supervisord` per `supervisord.conf`.
- CLI commands: `serve` (FastAPI), `run` (Rich dashboard), `paper` (force paper mode), `markets` (catalog list), `cancel-all` (emergency), `status` (risk report).

---

## 4. Current Components

### §7 — Bot Lifecycle components (`VERIFIED`)

| Stage | Component | File:line | Evidence |
|---|---|---|---|
| BOT START | `_startup()` / `lifespan()` | `main.py:56-105`, `api/server.py:279-540` | VERIFIED — 9 subsystems registered with watchdog; explicit startup ordering. |
| MARKET DISCOVERY | `UniversalMarketDiscoveryEngine` | `core/market_discovery.py:30-202` | VERIFIED — paginates Gamma `/markets?limit=100&offset=...` every 180s; safety ceiling `max_offset=2000` (20×100 markets). |
| MARKET FILTERING | `_evaluate_market`, `_refresh_markets` | `strategies/signal_trader.py:108-150`, `strategies/market_maker.py:64-100` | VERIFIED — signal_trader filters by `_min_confidence` (default 0.65); market_maker by `MAX_MARKETS_TO_QUOTE=8`. |
| DATA COLLECTION | `BookPoller` | `core/book_poller.py:1-80` | VERIFIED — Tier1: 2s × 50 tokens, Tier2: 6s; Semaphore=12; TIMEOUT=6s; rolling 30-result window circuit breaker. WS retired per KD-08/KD-24. |
| SIGNAL GENERATION | `_scan_markets`, `ml_model.predict` | `strategies/signal_trader.py:108-150` | VERIFIED — 15s scan; uses pre-polled `store.order_books`; bounded `OrderedDict` feature cache (500 entries). |
| STRATEGY SELECTION | `strategy_registry.start_strategy` | `api/server.py:419-423` | VERIFIED — three base strategies started in lifespan: `mm_avellaneda_stoikov`, `arb_binary_dutch_book`, `ml_random_forest_quant`. |
| RISK CHECK | `risk_manager.check_order` | `risk/manager.py:126-348` | VERIFIED — invoked from `strategies/base.py:83`, `core/position_manager.py:114,188`, `api/server.py:2270,2615,3653`. 22 gates enumerated. |
| POSITION SIZING | `allocate_capital` | `core/capital_allocator.py` (cited from `strategies/signal_trader.py:29`) | STRONG EVIDENCE — Kelly fraction 0.25; size capped by `MAX_POSITION_PER_MARKET * dynamic_model_risk_multiplier`. |
| ORDER CREATION | `BaseStrategy.submit_order` | `strategies/base.py:60-148` | VERIFIED — provisional Order → `risk_manager.check_order` → `decision_ledger.record(RISK_APPROVED)` → `paper_sim.create_order` or `clob_client.create_order`. |
| ORDER SUBMISSION (paper) | `paper_sim.create_order` | `paper/simulator.py:75-124` | VERIFIED — mints `paper-{uuid.uuid4().hex[:12]}` order_id; appends to store; records ORDER stage in decision ledger. |
| ORDER SUBMISSION (live) | `clob_client.create_order` | `core/clob_client.py:256-347` | VERIFIED — EIP-712 typed-data signing via `Account.sign_typed_data`; POST `/order`; mints fresh `uuid.uuid4()` order_id and 16-byte `nonce` per call. |
| ACKNOWLEDGEMENT (paper) | `_fill_loop` / `_try_fill_orders` | `paper/simulator.py:152-175` | VERIFIED — 1s polling; checks `book.best_ask <= order.price` (BUY) / `book.best_bid >= order.price` (SELL). |
| ACKNOWLEDGEMENT (live) | (none) | `core/clob_client.py:365-370` | NOT FOUND — `get_trades()` exists but is never called from the trade path. No WS fill subscription. |
| FILL / PARTIAL FILL (paper) | `_execute_fill` | `paper/simulator.py:241-309` | VERIFIED — `fill_size = order.size_remaining` (single-shot; no partial fills modeled); applies 3-component slippage; records FILL stage + execution_quality. |
| FILL / PARTIAL FILL (live) | (none) | — | NOT FOUND — no live fill detection. |
| POSITION MANAGEMENT | `PositionManager` | `core/position_manager.py:35-240` | VERIFIED — 5s loop; registers TP/SL on every position; cancels prior exit order before re-submitting; SELL exit at `best_bid` (marketable). TP/SL exits re-clear the same risk gate as entries (V3 fix). |
| EXIT DECISION | `evaluate_positions` | `core/position_manager.py:51-219` | VERIFIED — TP trigger: `mid >= take_profit_price`; SL trigger: `mid <= stop_loss_price`. High-water-mark trailing. |
| CLOSE / SETTLEMENT | `settlement_engine`, `paper_sim._execute_fill` | `core/settlement.py`, `paper/simulator.py:241-309` | STRONG EVIDENCE — paper fills → `store.record_fill(trade)` → `daily_pnl`, `paper_balance`, `peak_equity`, `equity_history` updated; P&L computed inline as `(fill_price - avg_entry_price) * fill_size` (SELL only). |
| P&L | `store.record_fill` | `core/data_store.py` (called from `paper/simulator.py:270`) | VERIFIED — updates daily_pnl, paper_balance, peak_equity; updates Position.realised_pnl and Position.avg_entry_price. |
| ATTRIBUTION | `core.attribution` | `core/attribution.py:1-80` | VERIFIED — slices closed_positions across 7 dimensions: strategy, confidence_bucket, edge_bucket, probability_band, liquidity_level, holding_period, trade_direction. |

### §8 — Order Management System components (`VERIFIED`)

| Component | File:line | Evidence |
|---|---|---|
| `OrderState` enum (10 states) | `core/order_state_machine.py:85-103` | VERIFIED — CREATED, VALIDATED, SUBMITTED, ACKNOWLEDGED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED, EXPIRED. |
| `ALLOWED_TRANSITIONS` (22 edges) | `core/order_state_machine.py:125-162` | VERIFIED — fail-closed: terminal states map to empty frozenset; illegal transitions raise `InvalidTransition`. |
| `Order` (frozen dataclass) | `core/order_state_machine.py:188-216` | VERIFIED — order_id, state, strategy, token_id, side, price, size, idempotency_key, decision_id, created_at, updated_at, filled_size, metadata. |
| `generate_idempotency_key` | `core/order_state_machine.py:220-248` | VERIFIED — SHA-256 over `\|`-joined canonical string of (strategy, token_id, side.upper, price:8dp, size:8dp). Deterministic. |
| `transition(order, new_state)` | `core/order_state_machine.py:308-337` | VERIFIED — pure; uses `dataclasses.replace`; raises `InvalidTransition` on illegal moves. |
| `OrderStateMachine` (SQLite) | `core/order_state_machine.py:341-573` | VERIFIED — `order_transitions` table (append-only), indexes on (order_id, ts ASC), (idempotency_key, ts DESC), (token_id, ts DESC). `save`/`load`/`get_history`. |
| **Production integration** | `paper/simulator.py:139` | VERIFIED — **only** call site is `transition(order_id, OrderState.CANCELLED, reason="manual cancel")` wrapped in `try/except: pass`. The state machine is NOT exercised on the order creation / fill / rejection paths. |

### §9 — Execution Quality components (`VERIFIED`)

| Component | File:line | Evidence |
|---|---|---|
| `execution_quality` SQLite table | `core/execution_quality.py:138-217` | VERIFIED — 21 columns (timestamp, order_id, decision_id, token_id, strategy, side, signal_price, decision_price, submitted_price, best_bid, best_ask, expected_fill, actual_fill, spread, slippage, slippage_bps, latency_ms, realized_edge, paper, data_json). 7 indexes (ts, strategy+ts, token+ts, decision, slippage DESC, side+ts, paper+ts, order_id). |
| `record_execution(order, fill_price, signal_price=None)` | `core/execution_quality.py:230-373` | VERIFIED — best-effort (never raises); resolves book snapshot from `store.order_books`; computes expected_fill (BUY→best_ask, SELL→best_bid); slippage = actual − expected; bps = slippage/abs(expected) × 10_000; realized_edge = (sig_px − actual) for BUY, (actual − sig_px) for SELL; latency_ms = (now − order.created_at) × 1000. |
| Production caller | `paper/simulator.py:305-308` | VERIFIED — `record_execution(order, fill_price, signal_price=order.price)` — **always passes `signal_price = order.price`** (the limit price). |
| `submitted_price` derivation | `core/execution_quality.py:278` | VERIFIED — hard-coded `submitted_px = float(getattr(order, "price", 0.0))`. There is no code path where `submitted_price != decision_price`. |
| `get_execution_stats(time_window, strategy)` | `core/execution_quality.py:378-...` | VERIFIED (via decorator `timed_query` at line 378); aggregates count, avg/median/p95/p99 slippage_bps, realized_edge; by_side breakdown. |
| HTTP exposure | `api/server.py:3984+` | VERIFIED — `GET /api/execution-quality` (per module docstring at `core/execution_quality.py:47-53`). |

### §10 — Execution Safety components (`VERIFIED`)

| Risk control | File:line | Enforced on submission path? |
|---|---|---|
| Shadow mode gate | `risk/manager.py:177-181` | VERIFIED — `if settings.trading_mode == "shadow": return False, ...` |
| Durable + in-memory kill switch | `risk/manager.py:184-186`, `core/safety.py:18-49` | VERIFIED — file-backed marker at `/app/data/kill_switch`; survives restart; checked on every `check_order`. |
| Observation-only mode | `risk/manager.py:189-193` | VERIFIED — `if self.observation_only and not order.paper: return False, ...` |
| Exposure reconciliation gate | `risk/manager.py:197-204` | VERIFIED — live orders blocked while `total_exposure > MAX_DEPLOYABLE_CAPITAL ($60)`. |
| Live trading disabled by default | `risk/manager.py:207-208`, `main.py:65-71`, `api/server.py:354-363` | VERIFIED — requires `LIVE_TRADING_ENABLED=true` AND wallet credentials. Lifespan raises `RuntimeError` on misconfiguration. |
| Per-trade strategy cooldown | `risk/manager.py:213-219, 350-401` | VERIFIED — `PER_TRADE_MAX_LOSS=$0.50`, `STRATEGY_COOLDOWN=300s`; `report_trade_pnl()` sets cooldown; `is_strategy_paused()` consulted by `check_order`. |
| Daily loss stop ($2) | `risk/manager.py:234-237` | VERIFIED — triggers `_trigger_kill_switch` (cancels all open orders + writes durable marker). |
| Weekly loss stop ($5) | `risk/manager.py:240-244` | VERIFIED — `store.roll_weekly_window()` then check. |
| Max drawdown ($8) | `risk/manager.py:247-251` | VERIFIED — measured against `OPERATING_CAPITAL ($100)` not `BANKROLL_CEILING ($200)` (V-fix documented in code comment). |
| Cash reserve protection ($60 deployable) | `risk/manager.py:254-256` | VERIFIED — `total_exp + order_cost > MAX_DEPLOYABLE_CAPITAL` rejects. |
| Total simultaneous open risk ($25) | `risk/manager.py:259-260` | VERIFIED. |
| Max position per market ($3, dynamic) | `risk/manager.py:268-270` | VERIFIED — `MAX_POSITION_PER_MARKET * dynamic_model_risk_multiplier()`. |
| Absolute max position ($5) | `risk/manager.py:271-272` | VERIFIED. |
| Normal position size guidance ($2, dynamic) | `risk/manager.py:275-276` | VERIFIED — applied to NEW positions only. |
| Per-strategy exposure cap ($15) | `risk/manager.py:279-284` | VERIFIED. |
| Correlated event-group cap ($8) | `risk/manager.py:288-296` | VERIFIED — grouped by `store.market_slugs[token_id]`. |
| Mark-to-market exposure cap ($25) | `risk/manager.py:298-315` | VERIFIED — **fail-open**: `try/except: pass` on `compute_mark_to_market_exposure` failure. |
| Max open positions (8) | `risk/manager.py:318-324` | VERIFIED — only counts active positions (`yes_shares > 0.001 OR total_invested > 0.01`). |
| Pending order capital cap ($10) | `risk/manager.py:327-329` | VERIFIED. |
| Max open order count | `risk/manager.py:332-333` | VERIFIED — `settings.max_open_orders`. |
| Price sanity bounds [0.01, 0.99] | `risk/manager.py:336-337` | VERIFIED. |
| Minimum order size ($0.50) | `risk/manager.py:340-341` | VERIFIED — `order.size < 0.5` rejected. |
| Bankroll ceiling protection | `risk/manager.py:344-346` | VERIFIED. |
| Live safety gate (10 checks) | `core/live_safety_gate.py:9-39` | VERIFIED — paper_mode_24h, positive_expectancy, max_drawdown_under_2usd, win_rate_over_50pct, min_20_closed_trades, ml_trained_on_real_data, drift_healthy, kill_switch_tested, risk_limits_verified, api_credentials_configured. |

---

## 5. Data Flow

### Paper-trade data flow (`VERIFIED`)

```
Gamma REST ──► gamma_client ──► market_discovery.catalog (dict: token_id → market_dict)
                                          │
                                          ▼
                                    book_poller (REST tiered: 2s/6s)
                                          │
                                          ▼
                                store.order_books (dict: token_id → OrderBook)
                                          │
                  ┌───────────────────────┼───────────────────────┐
                  ▼                       ▼                       ▼
        signal_trader._scan_markets   market_maker._review_quotes  arb_scanner._scan_for_arb
                  │                       │                       │
                  ▼                       ▼                       ▼
              ml_model.predict ──► MarketSignal ──► BaseStrategy.submit_order(args, decision_id)
                                          │
                                          ▼
                              risk_manager.check_order(provisional)  ◄── 22 institutional gates
                                          │
                              ┌───────────┴────────────┐
                              │ (allowed)              │ (rejected)
                              ▼                        ▼
                  decision_ledger.record(RISK_APPROVED)  decision_ledger.record(RISK_REJECTED)
                              │                        + shadow_trading.record_shadow_trade()
                              ▼
                  paper_sim.create_order(args, strategy, decision_id)
                              │
                              ▼
                  store.add_order(Order)  ──►  store.open_orders (dict)
                  decision_ledger.record(ORDER)
                              │
                              ▼
                  [paper_sim 1s fill loop]
                  _try_fill_orders → _can_fill(order, book) → _apply_slippage(order, raw_price, book)
                              │
                              ▼
                  _execute_fill(order, fill_price):
                    • pnl = (fill_price - avg_entry_price) * fill_size   (SELL only)
                    • risk_manager.report_trade_pnl(strategy, pnl) ──► per-trade cooldown
                    • store.record_fill(Trade) ──► daily_pnl, paper_balance, peak_equity, equity_history
                    • store.update_order(FILLED, size_matched=order.size)
                    • decision_ledger.record(FILL, pnl=...)
                    • execution_quality.record_execution(order, fill_price, signal_price=order.price)
                    • closed_positions.record_closed_position(...)  (via SELL exit)
```

### Live-trade data flow (`VERIFIED` with gaps marked)

```
[same upstream: Gamma → market_discovery → book_poller → store.order_books]
                                          │
                                          ▼
                              strategies/base.py::submit_order
                                          │
                                          ▼
                              risk_manager.check_order(provisional)   ◄── same 22 gates (paper=False)
                                          │
                              ┌───────────┴────────────┐
                              │ (allowed)              │ (rejected)
                              ▼                        ▼
                  decision_ledger.record(RISK_APPROVED)  decision_ledger.record(RISK_REJECTED)
                              │
                              ▼
                  clob_client.create_order(args):
                    • Account.sign_typed_data(EIP-712, Order{maker, signer, taker, tokenId, price, size, side, nonce, feeRateBps, signatureType})
                    • POST /order  ──► Polymarket CLOB REST API
                    • returns response dict or None on error
                              │
                              ▼
                  Order(order_id=resp.orderID, paper=False, decision_id=...) ──► store.add_order
                              │
                              ▼
                  [GAP] no state-machine transition CREATED → SUBMITTED → ACKNOWLEDGED
                  [GAP] no fill acknowledgement loop (no get_trades() polling; no WS subscription)
                  [GAP] no reconciliation of store.open_orders / store.positions against CLOB truth
                  [GAP] no idempotency check (fresh uuid + nonce per call → duplicates pass)
                  [GAP] no execution_quality.record_execution call (only paper_sim calls it)
```

### Risk-gate data flow (`VERIFIED`)

```
BaseStrategy.submit_order / position_manager TP-SL / api/server /api/trade, /api/positions/.../close, /api/arbitrage/execute
        │
        ▼
risk_manager.check_order(provisional: Order) ──► _check_order_impl(order)
        │
        ▼
async with self._lock:
    0. shadow mode → reject
    0. kill_switch_active OR kill_switch_file_exists() → reject
    0. observation_only AND not paper → reject
    0b. exposure > MAX_DEPLOYABLE_CAPITAL → reject
    0c. not paper AND not live_trading_enabled → reject
    0d. strategy paused (cooldown) → reject
    1. kill_switch_active → reject (redundant with 0.)
    2. daily_pnl <= -DAILY_LOSS_STOP → trigger kill + reject
    2b. weekly_pnl <= -WEEKLY_LOSS_STOP → trigger kill + reject
    3. drawdown >= MAX_DRAWDOWN_LIMIT → trigger kill + reject
    4. total_exp + order_cost > MAX_DEPLOYABLE_CAPITAL → reject (BUY only)
    5. total_exp + order_cost > MAX_TOTAL_OPEN_RISK → reject (BUY only)
    6. market_exp + order_cost > effective_mkt_cap → reject (BUY only)
    6. market_exp + order_cost > ABSOLUTE_MAX_POSITION → reject (BUY only)
    6b. new position AND order_cost > effective_norm_cap → reject (BUY only)
    6c. strategy_exp + order_cost > MAX_STRATEGY_EXPOSURE → reject (BUY only)
    6d. correlated group_exp + order_cost > MAX_CORRELATED_EXPOSURE → reject (BUY only)
    6e. mtm_exposure + order_cost > $25 → reject (BUY only; FAIL-OPEN on exception)
    7. active_positions >= MAX_OPEN_POSITIONS → reject (new market only)
    8. pending_capital + order_cost > MAX_PENDING_ORDER_CAPITAL → reject
    9. open_orders count >= max_open_orders → reject
    10. price not in [0.01, 0.99] → reject
    11. size < 0.5 → reject
    12. total_exp + order_cost > BANKROLL_CEILING - MIN_CASH_RESERVE → reject
        │
        ▼
    return (True, "OK") OR (False, reason)
        │
        ▼ (on rejection)
shadow_trading.record_shadow_trade(decision_id, token_id, strategy, side, price, size, predicted_edge=0, confidence=0)
        │   (fire-and-forget; best-effort)
        ▼
decision_ledger.record(RISK_REJECTED, reason=reason)
```

---

## 6. Execution Flow (full §7 lifecycle trace)

This traces the canonical lifecycle per §7 of the God Mode Master Prompt, stage by stage, with evidence file:line.

### Stage 1 — BOT START (`VERIFIED`)

- `main.py::_startup()` (CLI `run` command) OR `api/server.py::lifespan()` (CLI `serve` command) — both initialise the bot.
- Live-mode guard (`main.py:64-71`, `api/server.py:353-363`): live requires `LIVE_TRADING_ENABLED=true` AND `POLY_PRIVATE_KEY` configured; both raise `typer.Exit(1)` / `RuntimeError` if missing.
- `clob_client.derive_api_key()` is called even in paper mode (line 91-93) so data-access paths work.
- Paper simulator started conditionally (`main.py:96-97`, `api/server.py:390-393`).
- WS client NOT started (KD-08/KD-24 decision: tiered REST polling only — `main.py:99-100`, `api/server.py:407-408`).
- Strategy registry starts 3 base strategies (`api/server.py:419-423`).
- Migration runner executes additive `CREATE TABLE IF NOT EXISTS` against 10 SQLite DBs (`api/server.py:292-326`).
- **Async execution / crash recovery**: each long-running component is an `asyncio.create_task` with a `name=`; the lifespan shutdown handler cancels each task and `await`s it with `CancelledError` swallow. `core/watchdog.py` registers 9 subsystems and runs heartbeat checks at `settings.watchdog_check_interval` — stale heartbeats emit WARNING findings; critical tripwires (daily loss, weekly loss, drawdown, book stall) auto-activate the durable kill switch when `settings.tripwire_auto_kill` is true (`core/watchdog.py:42-100`).

### Stage 2 — MARKET DISCOVERY (`VERIFIED`)

- `core/market_discovery.py::UniversalMarketDiscoveryEngine::_discovery_loop` runs every 180s after a 2s startup grace.
- `sync_full_catalog()` paginates Gamma `/markets?limit=100&offset=...&active=true&closed=false` with a `max_offset=2000` safety ceiling (20 pages × 100 markets).
- Builds two catalogs: `catalog` (token_id → market metadata) and `events_catalog` (event_id → event metadata).
- Maintains `excluded_markets` audit log of skipped/invalid items.
- `httpx.AsyncClient(timeout=20.0, follow_redirects=True)`.

### Stage 3 — MARKET FILTERING (`VERIFIED`)

- `signal_trader` iterates `market_discovery.catalog.items()` and calls `_evaluate_market(mkt, token_id=tid)` per token; signals with `confidence < self._min_confidence` are filtered out (`strategies/signal_trader.py:142-148`).
- `market_maker` discovers markets via `_discover_markets()` and picks `MAX_MARKETS_TO_QUOTE=8` liquid markets; periodically calls `_refresh_markets()` every 10 loop iterations to swap in markets whose books are actually quotable (`strategies/market_maker.py:64-100`).
- `arb_scanner` builds YES/NO binary pairs via `gamma_client.extract_binary_pair(mkt)`; refreshes pairs every 600s in a background task (`strategies/arb_scanner.py:70-84`).

### Stage 4 — DATA COLLECTION (`VERIFIED`)

- `core/book_poller.py::BookPoller` — tiered REST polling.
- Tier 1: 50 tokens polled every `TIER1_INTERVAL=2.0s` (high-priority / actively quoted).
- Tier 2: remainder polled every `TIER2_INTERVAL=6.0s` (background).
- `asyncio.Semaphore(MAX_CONCURRENT=12)` for in-flight request limiting.
- `httpx.AsyncClient` with `timeout=TIMEOUT=6.0`.
- Rolling 30-result window circuit breaker: opens on >50% failure rate, stays open for `recovery_timeout`.
- Per-token promotion via `prioritize_tokens([token_id])` — used by `POST /api/positions/{id}/close` when a book is empty (`api/server.py:2530`).
- **Retries / backoff**: per-request retry is NOT implemented at the book_poller level — a failed request increments `_error_count` and moves on. The circuit breaker provides coarse-grained failure isolation but does not retry individual requests. UNVERIFIED whether `httpx.AsyncClient` does internal retries (default: no retries).
- **WS retired**: `core/ws_client.py` exists but is not started (KD-08/KD-24: `subscribe()` had zero callers; D5 decision: REST polling only).

### Stage 5 — SIGNAL GENERATION (`VERIFIED`)

- `strategies/signal_trader.py::_scan_markets` runs every `SCAN_INTERVAL=15s`.
- For each token, calls `_evaluate_market(mkt, token_id)` which:
  - Fetches `store.get_order_book(token_id)` (cached, pre-polled by `book_poller`).
  - Calls `ml_model.predict(features)` where features come from `ml.features.extract_features`.
  - Computes predicted probability `p_yes`, market mid, and edge (`p_yes − market_mid`).
  - Mints a `MarketSignal` (with `decision_id = uuid.uuid4().hex`) for BUY (p_yes ≥ 0.55) or SELL (p_yes ≤ 0.45) directions.
  - Filters out signals where `confidence < self._min_confidence` (default 0.65).
  - Calls `decision_ledger.record(decision_id, stage="PREDICTION", ...)` and `stage="SIGNAL", ...` (per module docstring `core/decision_ledger.py:11-12`).
- `_recycle_stale_orders()` cancels orders older than `STALE_ORDER_SECONDS=180` before scanning.
- Model saved every `MODEL_SAVE_INTERVAL=300s` via `asyncio.to_thread(ml_model.save)`.

### Stage 6 — STRATEGY SELECTION (`VERIFIED`)

- `strategy_registry` is configured in lifespan with three strategies: `mm_avellaneda_stoikov`, `arb_binary_dutch_book`, `ml_random_forest_quant` (`api/server.py:419-423`).
- Each strategy has its own `_run()` loop; `BaseStrategy.start()` creates an `asyncio.create_task(name=f"strategy-{self.name}")`.
- Strategy enabling is config-driven: `settings.mm_enabled`, `settings.arb_enabled`, `settings.signal_enabled` (`main.py:143-148`).
- Selection is hard-coded — there is no dynamic strategy-selection layer that picks among strategies per market condition.

### Stage 7 — RISK CHECK (`VERIFIED` — see §10 / §5 for details)

- `BaseStrategy.submit_order` builds a provisional `Order(order_id="pre-check", ...)` and calls `risk_manager.check_order(provisional)` (`strategies/base.py:72-83`).
- Returns `(False, reason)` for any of the 22 gates; on rejection, calls `decision_ledger.record(decision_id, stage="RISK_REJECTED", reason=reason)` and `shadow_trading.record_shadow_trade(...)` (fire-and-forget).
- On approval, calls `decision_ledger.record(decision_id, stage="RISK_APPROVED", ...)` before the order leaves the strategy layer (`strategies/base.py:106-122`).

### Stage 8 — POSITION SIZING (`STRONG EVIDENCE`)

- `core/capital_allocator.py::allocate_capital` is imported at the top of `strategies/signal_trader.py:29` and used in `_ml_signal` (per the comment at line 22-28 — "the allocator is now the single source of truth for position size").
- Kelly fraction: `KELLY_FRACTION = 0.25` (quarter-Kelly).
- `MIN_KELLY_NUMERATOR = 0.02` — minimum raw Kelly f* numerator: `(p*b - (1-p)) > 2%`.
- Size is capped by `MAX_POSITION_PER_MARKET * dynamic_model_risk_multiplier()` (`risk/manager.py:264-270`).
- `dynamic_model_risk_multiplier()` returns 1.00 / 0.60 / 0.30 based on `drift_detector.drift_status` (PSI) and `drift_detector.rolling_brier` (`risk/manager.py:78-97`).

### Stage 9 — ORDER CREATION (`VERIFIED`)

- `BaseStrategy.submit_order` constructs `OrderArgs(token_id, price, side, size)` and either calls `paper_sim.create_order(args, strategy=self.name, decision_id=decision_id)` or `clob_client.create_order(args)`.
- Paper: mints `Order(order_id=f"paper-{uuid.uuid4().hex[:12]}", paper=True, decision_id=...)` and calls `store.add_order(order)` + `decision_ledger.record(stage="ORDER", ...)`.
- Live: `clob_client.create_order` returns the CLOB response dict; `BaseStrategy` constructs `Order(order_id=resp.get("orderID") or resp.get("order_id", "unknown"), paper=False, decision_id=...)` and calls `store.add_order(order)`.
- **GAP (CRITICAL)**: Neither paper nor live path calls `order_state_machine.create_order(...)` or `order_state_machine.transition(...)`. The `decision_id` is propagated but the state machine is bypassed entirely.

### Stage 10 — ORDER SUBMISSION (`VERIFIED`)

- Paper: `paper_sim.create_order` adds the order to `store.open_orders` (in-memory dict).
- Live: `clob_client.create_order` (`core/clob_client.py:256-347`) signs an EIP-712 typed-data payload with the wallet private key, then POSTs to `/order` on the Polymarket CLOB REST API.
- The CLOB `Order` payload includes `maker`, `signer`, `taker`, `tokenId`, `price`, `size`, `side`, `nonce`, `feeRateBps=0`, `signatureType=0`, `signature`.
- `nonce = int.from_bytes(secrets.token_bytes(16), "big")` — fresh random per call (no idempotency).
- `order_id = str(uuid.uuid4())` — fresh per call (no client-side idempotency key).
- Returns `None` on `httpx.HTTPStatusError` or any other exception — logged but no retry.
- **GAP**: No retry, no backoff, no idempotency key sent to the exchange. A network timeout after the exchange has accepted the order but before the client received the response would produce a duplicate order on retry — and there is no retry, so the order is simply lost from the local store's perspective.

### Stage 11 — ACKNOWLEDGEMENT (`VERIFIED paper`, `NOT FOUND live`)

- Paper: `paper_sim._fill_loop` (1s cadence) polls `store.get_open_orders()`, fetches `store.get_order_book(token_id)` per open paper order, and calls `_can_fill(order, book)`. Fill condition: `book.best_ask <= order.price` (BUY) or `book.best_bid >= order.price` (SELL).
- Live: `clob_client.create_order` returns the CLOB response (which may include an `orderID`), but there is NO subsequent polling of the CLOB `/data/trades` endpoint to detect fills. `clob_client.get_trades()` (`core/clob_client.py:365-370`) exists but is not invoked from any strategy, paper_sim, or position_manager path.
- WS fill stream: `core/ws_client.py` is retained but not started — `subscribe()` had zero callers (KD-08/KD-24). Even if it were started, it does not appear to subscribe to user-trade fill channels.
- **GAP (CRITICAL)**: For live orders, the local `store.open_orders` will retain the order as `OPEN` indefinitely; `store.positions` will never reflect the filled entry; `store.daily_pnl` will never credit/debit the realised P&L. The bot becomes blind to its own live positions after the first fill.

### Stage 12 — FILL / PARTIAL FILL (`VERIFIED paper single-shot; NOT FOUND partial; NOT FOUND live`)

- Paper: `paper_sim._execute_fill(order, fill_price)` (`paper/simulator.py:241-309`):
  - `fill_size = order.size_remaining` — **single-shot only**. Partial fills are not modeled. The simulator either fills the entire order or leaves it open.
  - P&L computed for SELL only: `pnl = (fill_price - pos.avg_entry_price) * fill_size`.
  - Calls `risk_manager.report_trade_pnl(strategy, pnl)` — feeds the per-trade-loss circuit breaker.
  - Constructs `Trade(...)` and calls `store.record_fill(trade)` — updates `daily_pnl`, `paper_balance`, `peak_equity`, `equity_history`, `Position.realised_pnl`, `Position.avg_entry_price`.
  - `store.update_order(order_id, status=FILLED, size_matched=order.size)` — full size matched.
  - `decision_ledger.record(decision_id, stage="FILL", pnl=pnl, fill_price=..., fill_size=..., side=..., order_id=..., trade_id=...)`.
  - `execution_quality.record_execution(order, fill_price, signal_price=order.price)`.
- Slippage model `_apply_slippage(order, raw_price, book)` (`paper/simulator.py:177-225`):
  - Crossing penalty: flat 1 tick (0.01).
  - Size impact: 0.5 tick per `SLIPPAGE_DEPTH_BUCKET=50.0` shares in excess of top-of-book depth.
  - Queue position: deterministic 0 or 1 tick from `SHA-256(order.order_id)[0] & 0x01`.
  - Total slippage ticks = `1 + (overflow / 50) * 0.5 + (hash & 0x01)`.
  - Slipped price clamped to `[0.01, 0.99]`.
- Live: NO fill detection.

### Stage 13 — POSITION MANAGEMENT (`VERIFIED`)

- `core/position_manager.py::PositionManager::_loop` runs every 5s.
- For each position with `yes_shares > 0`:
  - Fetches `store.get_order_book(token_id)`. Skips if book or `book.mid` is None.
  - Auto-registers a `ManagedPosition` if not already tracked.
  - Updates `high_water_mark = max(high_water_mark, mid)`.
  - TP trigger: `mid >= take_profit_price` (default `min(entry * 1.25, 0.99)`).
  - SL trigger: `mid <= stop_loss_price` (default `max(entry * 0.95, 0.01)`).
  - On trigger: cancels prior exit order (`managed.active_exit_order_id`), builds `Order(side=SELL, price=book.best_bid, size=pos.yes_shares, strategy="position_manager_tp" or "_sl")`, re-clears `risk_manager.check_order(exit_order)` (V3 fix), then calls `paper_sim.create_order(exit_order, strategy=strat, decision_id=...)`.
  - **GAP**: Position manager only calls `paper_sim.create_order` — it does NOT route through `BaseStrategy.submit_order` and does NOT have a live-order path. Live TP/SL exits would not fire.

### Stage 14 — EXIT DECISION (`VERIFIED`)

- TP/SL: as above (position_manager).
- Manual close: `POST /api/positions/{token_id}/close` (`api/server.py:2482-2601`) — supports `dry_run=true` and `max_size_shares` for partial scale-out.
- Long YES → SELL at `best_bid`; long NO → BUY at `best_ask` (synthetic short coverage).
- Stale-order recycle: `signal_trader._recycle_stale_orders` cancels orders older than 180s before each scan.
- Inventory flush (market_maker): documented `_inventory_since` dict for dumping stale inventory after 60s.

### Stage 15 — CLOSE / SETTLEMENT (`STRONG EVIDENCE`)

- `core/settlement.py::settlement_engine` is started in lifespan (`api/server.py:412`) — handles market resolution (didn't deep-read).
- Paper close: `paper_sim._execute_fill` (for SELL exits) → `store.record_fill` → `closed_positions.record_closed_position` (via attribution integration in `_execute_fill` or via the position_manager audit log path).
- `closed_positions` schema (`core/closed_positions.py:18-43`): `position_id` (UNIQUE idempotency key), entry/exit prices, shares, pnl, holding_seconds, model_version, decision_id, direction, confidence, predicted_edge, p_yes, market_mid, liquidity, metadata_json.

### Stage 16 — P&L (`VERIFIED`)

- Per-fill P&L: `pnl = (fill_price - pos.avg_entry_price) * fill_size` (SELL; `paper/simulator.py:247-248`). For BUY entries, no P&L is realised at entry.
- Realised P&L rolled up in `store.daily_pnl`, `store.paper_balance` (the source of truth for paper equity), `store.peak_equity` (for drawdown), `store.equity_history`.
- Persisted to disk via `store.save_to_disk()` (`core/data_store.py:310-355`) — atomic write via `tmp_file.replace(STATE_FILE)`. Loaded on boot via `store.load_from_disk()` (`core/data_store.py:357-...`, called at module import at line 442).
- **GAP**: state persistence is JSON-only — `daily_pnl`, `paper_balance`, `peak_equity`, `positions`, `trades` survive a restart, but `open_orders` does NOT appear in the persistence dict. A restart loses open-order state.

### Stage 17 — ATTRIBUTION (`VERIFIED`)

- `core/attribution.py::get_full_attribution()` slices `closed_positions` across 7 dimensions:
  - `by_strategy` — which strategy makes the money.
  - `by_confidence_bucket` — low / medium / high / very_high / unknown.
  - `by_edge_bucket` — negative / small / medium / large / very_large / unknown.
  - `by_probability_band` — deep_no / no / neutral / yes / strong_yes / unknown.
  - `by_liquidity_level` — thin / low / medium / high / very_high / unknown.
  - `by_holding_period` — intraday / short / medium / long.
  - `by_trade_direction` — BUY / SELL / unknown.
- Each bucket row carries `count, total_pnl, avg_pnl, win_rate, wins, losses, avg_holding_seconds, gross_profit, gross_loss, profit_factor, capital_deployed`.
- TTL-cached via `core.cache.attribution_cache` (W11-2).
- HTTP exposure: `GET /api/attribution` (`api/server.py` per `core/attribution.py:21`).

### Cross-cutting §7 concerns

- **Async execution**: single asyncio event loop per process; all subsystems are `asyncio.create_task` with explicit `name=` for watchdog liveness.
- **Polling**: book_poller (2s/6s tiered), paper_sim (1s), position_manager (5s), signal_trader (15s), market_maker (4s), market_discovery (180s), arb_scanner (`settings.arb_scan_interval_seconds`), reconciliation (daily), state persistence (60s `_state_persistence_loop`).
- **WebSockets**: outbound only (`core/ws_broadcast.py`); inbound (CLOB fill stream) is retired per KD-08/KD-24.
- **Retries / backoff**: NOT implemented at the order-submission layer. `clob_client.create_order` does a single POST; on failure it logs and returns None. `book_poller` does not retry individual failed requests (relies on the next poll cycle). The generic `core/circuit_breaker.py` is available but UNVERIFIED whether it is actually applied to `clob_client` calls — the decorator usage pattern is documented but the call sites were not enumerated in this session.
- **Timeouts**: `book_poller` 6s, `market_discovery` 20s, `httpx.AsyncClient` defaults elsewhere.
- **Reconnect**: N/A — REST polling, no persistent connections to maintain.
- **Crash recovery**: `store.load_from_disk()` restores positions, trades, daily_pnl, paper_balance, peak_equity. Durable kill-switch marker survives restart. SQLite ledgers (decision, execution_quality, closed_positions, order_state_machine, audit_trail, alerts, feature_flags, shadow_trades, market_intelligence, observability) are append-only and survive restart.
- **Persistent state**: see above — open_orders are NOT persisted.
- **Idempotency**: `generate_idempotency_key()` exists but is NOT consulted on submission. `closed_positions.position_id` is UNIQUE in SQLite (idempotency on close records). `audit_logger.idempotency_key` is UNIQUE (but auto-generated per event — not used for dedup across retries).
- **Duplicate prevention**: `closed_positions.position_id UNIQUE` constraint; `core/portfolio.py:99` `duplicate_fill_anomaly` passive counter. No active dedup on order submission.
- **Race conditions**: `risk_manager._lock` (asyncio.Lock) serialises the entire `check_order` impl, so concurrent strategy loops cannot interleave risk checks. `store._lock` (asyncio.Lock) guards `add_order`, `record_fill`, `cancel_all_orders`. `paper_sim._execute_fill` reads `store.positions.get(token_id)` synchronously without the lock (documented at `core/execution_quality.py:253-257` as the established pattern).

---

## 7. Feature Inventory

### Implemented features (`VERIFIED`)

| Feature | Component | Notes |
|---|---|---|
| Paper-trade simulation | `paper/simulator.py` | 1s fill loop; 3-component slippage model; virtual balance |
| Live-trade submission | `core/clob_client.py` | EIP-712 signed POST /order; NOT fill-acked |
| Tiered book polling | `core/book_poller.py` | 2s/6s tiers; Semaphore=12; circuit breaker |
| Universal market discovery | `core/market_discovery.py` | 180s catalog sync; 2000-market ceiling |
| ML signal generation | `ml/model.py`, `strategies/signal_trader.py` | RF + SGD online ensemble; 15s scan |
| Avellaneda-Stoikov market making | `strategies/market_maker.py` | Reservation price; inventory skew; 4s loop |
| Binary arbitrage scanner | `strategies/arb_scanner.py` | Long-side Dutch Book + short-side overpriced |
| 22-gate institutional risk engine | `risk/manager.py` | See §10 |
| Durable kill switch | `core/safety.py` | File-backed; survives restart |
| Per-trade-loss circuit breaker | `risk/manager.py:59-65, 350-401` | $0.50 loss → 300s strategy cooldown |
| Watchdog tripwires | `core/watchdog.py` | Heartbeat + daily/weekly loss + drawdown + book stall |
| 10-check live readiness gate | `core/live_safety_gate.py` | Staged validation before live trading enabled |
| Order state machine | `core/order_state_machine.py` | 10 states, 22 transitions, SQLite append-only — but NOT wired to production path |
| Execution-quality ledger | `core/execution_quality.py` | 21-column schema; 7 indexes; signal/decision/submitted/best_bid/best_ask/expected/actual/spread/slippage/bps/latency/realized_edge |
| Decision ledger | `core/decision_ledger.py` | PREDICTION→SIGNAL→RISK_APPROVED/REJECTED→ORDER→FILL chain |
| Closed-position journal | `core/closed_positions.py` | Round-trip P&L + 7-dim attribution context |
| 7-dimension attribution engine | `core/attribution.py` | strategy / confidence / edge / probability / liquidity / holding / direction |
| Smart Order Router (TWAP/VWAP/Iceberg) | `execution/smart_router.py` | Slippage gate; drift-adaptive tolerance — NOT wired to submit_order |
| Advanced Order Router | `execution/advanced_router.py` | Exposed only via `POST /api/execution/plan` (plan-only) |
| Shadow trading | `core/shadow_trading.py` | Counterfactual journal on risk-rejected orders (§75) |
| Immutable hash-chained audit | `core/immutable_audit.py` | W17-5 |
| WebSocket broadcast | `core/ws_broadcast.py` | trades/orders/positions/alerts/system channels |
| Prometheus metrics | `core/prometheus_metrics.py` | HTTP / trading / ML / system surfaces |
| Async SQLite read pool | `core/db_pool.py`, `core/async_repositories.py` | W16-7 — v2 endpoints |
| Paper-sim slippage model | `paper/simulator.py:177-225` | 1 tick crossing + 0.5 tick / 50-share depth + 0/1 tick queue |
| ML drift detector | `ml/drift_detector.py` | PSI + Brier; HEALTHY / MODERATE_SHIFT / SIGNIFICANT_DRIFT |
| Continuous training orchestrator | `ml/training_orchestrator.py` | Drift-triggered + 6h schedule |
| Label backfill service | `core/label_backfill.py` | Resolved-market synthetic book → 38-dim features → labeled rows |
| Capital allocator (Kelly) | `core/capital_allocator.py` | Quarter-Kelly with MIN_KELLY_NUMERATOR=0.02 gate |
| Position manager (TP/SL) | `core/position_manager.py` | 5s loop; high-water-mark trailing; re-clears risk gate on exits |

### Documented but NOT implemented (`NOT FOUND`)

| Feature | Expected | Status |
|---|---|---|
| Live fill acknowledgement | Polling `/data/trades` or WS subscription | NOT FOUND — `clob_client.get_trades()` exists but never called |
| Live order state transitions | CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN → FILLED | NOT FOUND — state machine exists but is bypassed on the live path |
| Order idempotency on submission | `idempotency_key` sent to CLOB, dedup before exchange | NOT FOUND — fresh `uuid` + `nonce` per call |
| Live reconciliation | `store.open_orders` vs CLOB open orders; `store.positions` vs CLOB positions | NOT FOUND — `core/reconciliation.py` reconciles timescale_db tables only |
| Partial fills | `size_matched < size` updates; subsequent fill on remainder | NOT FOUND — paper_sim is single-shot (`fill_size = order.size_remaining`) |
| Live TP/SL exits | Position_manager routing through `clob_client` on live mode | NOT FOUND — position_manager calls `paper_sim.create_order` unconditionally |

---

## 8. What Works

(`VERIFIED` for each unless noted.)

1. **Paper-trade pipeline is end-to-end functional**: PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL → P&L → ATTRIBUTION. Tested in `tests/test_e2e_decision_chain.py` and `tests/integration/test_decision_chain.py`.

2. **Risk gate is comprehensive and consistently invoked**. Every order submission path (`strategies/base.py:83`, `core/position_manager.py:114,188`, `api/server.py:2270,2615,3653`) calls `risk_manager.check_order` with a provisional `Order` object — including TP/SL exits (V3 fix) and manual trades. The 22 gates cover shadow mode, kill switch (durable + in-memory), observation-only, exposure reconciliation, live-mode authorisation, per-trade cooldown, daily/weekly loss stops, max drawdown, cash reserve, total open risk, per-market / absolute / normal / per-strategy / correlated-group / mark-to-market exposure caps, max open positions count, pending order capital, max open order count, price bounds, minimum size, bankroll ceiling.

3. **Durable kill switch works correctly**. File-backed marker at `/app/data/kill_switch`; activation writes the marker + reason file; deactivation removes both. Survives process restart. Auto-triggers on daily loss / weekly loss / max drawdown breach (`risk/manager.py:234-251`).

4. **Decision ledger is correctly structured**. SQLite append-only `decision_events` table with `(decision_id, stage, token_id, strategy, pnl, data_json)`; the full lifecycle chain is reconstructable per token_id or decision_id. W11-9 query-timing decorator surfaces slow queries.

5. **Execution-quality schema is correct in shape**. All §9-required fields are present: `signal_price, decision_price, submitted_price, best_bid, best_ask, expected_fill, actual_fill, spread, slippage, slippage_bps, latency_ms, realized_edge`. 7 indexes cover the common query patterns.

6. **Live safety gate is institutional-grade**. The 10-check staged gate (`core/live_safety_gate.py`) requires 24h paper session, positive expectancy, drawdown < $2, win rate > 50%, ≥20 closed trades, ML trained on real data, drift healthy, kill switch tested, risk limits verified, API credentials configured. Fail-closed on any exception inside a check.

7. **Watchdog + tripwires**. Heartbeat monitoring for 9 subsystems; auto-kill on critical tripwires when `settings.tripwire_auto_kill` is enabled.

8. **State persistence for paper mode**. `store.save_to_disk()` atomic-writes `daily_pnl, paper_balance, peak_equity, equity_history, positions, trades` to `/app/data/store_state.json`; `load_from_disk()` restores on boot. Bankroll-baseline drift is detected and the high-water mark is re-based to prevent fabricated drawdowns (`core/data_store.py:371-377`).

9. **Slippage model is realistic and deterministic**. The 3-component model (crossing + size impact + queue position) is tuned to Polymarket's 1¢ tick; the queue position is SHA-256-derived from `order_id` so a given order always sees the same penalty across runs (reproducible P&L).

10. **Position manager V3 fix**. TP/SL exit orders now clear the same risk gate as entries — previously exits could bypass circuit breakers. The fix is documented in-code (`core/position_manager.py:102-110, 176-184`).

11. **Shadow trading journal**. Every risk-rejected order records a counterfactual shadow trade (`risk/manager.py:142-163`), enabling post-hoc analysis of "what would have been traded".

12. **Comprehensive test suite**. 80+ test files in `tests/`, including unit tests for every core module and integration tests for the decision chain, risk pipeline, observability pipeline, ML pipeline, and cache pipeline. Contract tests under `tests/contract/`. Load tests under `tests/load/`. Penetration tests in `tests/test_penetration.py`.

---

## 9. What Does Not Work

(`VERIFIED` for each.)

1. **Order state machine is bypassed on the production path.** `core/order_state_machine.py::transition()` is invoked exactly once in production code — `paper/simulator.py:139` on `CANCELLED`, wrapped in `try/except: pass`. The `Order` dataclass in `order_state_machine.py` (frozen, 13 fields) is a different class than `core.data_store.Order` (mutable, different fields). The live and paper order creation paths construct `core.data_store.Order` and never touch `order_state_machine.Order`. There is no `CREATED → VALIDATED → SUBMITTED → ACKNOWLEDGED → OPEN → FILLED` audit trail in `order_state_machine.db` for production orders — only the test suite populates it.

2. **`generate_idempotency_key()` is never consulted on submission.** `core/clob_client.py::create_order` mints a fresh `uuid.uuid4()` for `order_id` and a random 16-byte `nonce` for the EIP-712 payload on every call. The idempotency-key helper exists in `order_state_machine.py:220-248` and the SQLite index `idx_ord_idempotency` exists, but no code path queries `SELECT ... WHERE idempotency_key = ?` before submitting.

3. **No live fill acknowledgement.** `clob_client.get_trades(maker_address, limit)` exists at `core/clob_client.py:365-370` but is never called. `paper_sim._fill_loop` (1s) only handles `order.paper == True` orders (`paper/simulator.py:164-165`). For live orders, `store.open_orders` retains the order as `OPEN` indefinitely, `store.positions` never reflects the filled entry, `store.daily_pnl` never credits/debits the realised P&L, and `decision_ledger.record(stage="FILL", ...)` is never invoked.

4. **No live reconciliation.** `core/reconciliation.py::run_reconciliation` reconciles timescale_db tables (`market_snapshots, orderbook_ticks, fundamental_news, ml_feature_store`) against engine-insert counters. It does NOT reconcile `store.open_orders` or `store.positions` against the CLOB exchange. Drift between local state and exchange truth is undetectable by the current reconciliation job.

5. **No partial fills modeled.** `paper_sim._execute_fill` uses `fill_size = order.size_remaining` — single-shot. A 100-share order that crosses a 30-share top of book fills 100 shares at the slipped price, not 30 at top-of-book + 70 at the next level. `OrderStatus.PARTIALLY_FILLED` exists in `core/data_store.py:36` but is never set in production code.

6. **Three-tier price waterfall is structurally collapsed.** `record_execution(order, fill_price, signal_price=order.price)` is the only caller (`paper/simulator.py:307`). Inside `record_execution`, `submitted_px = float(getattr(order, "price", 0.0))` is hard-coded (`core/execution_quality.py:278`). Therefore for every recorded fill: `signal_price == decision_price == submitted_price == order.price`. `realized_edge` measures limit-vs-fill (i.e. crossing cost), not signal-vs-fill (i.e. model edge retention). The §9 "Theoretical Edge → Executable Edge → Realized Edge" framework is not measurable with the current data.

7. **Smart Order Router is not wired to submission.** `execution/smart_router.py::plan_execution` is invoked only by `core/analysis_engine.py:78` (for slippage estimation in metrics) and by `POST /api/execution/plan` (which returns a plan but does not execute the slices). `strategies/base.py::submit_order` does not call `smart_router.plan_execution` or check `SLIPPAGE_TOLERANCE_HEALTHY_BPS = 15`. No live or paper order is ever rejected for slippage tolerance.

8. **`min_edge` and `min_liquidity` are not enforced on the order path.** `core/portfolio_optimizer.py` defines `DEFAULT_MIN_EDGE = 0.03` and `DEFAULT_MIN_CONFIDENCE = 0.55`, but the optimizer is not consulted by `risk_manager.check_order` or by `signal_trader`. The schema field `min_liquidity_usd DOUBLE PRECISION NOT NULL DEFAULT 50.0` exists in `core/db/migrations/001_initial_enterprise_schemas.sql:360` but no production code reads or enforces it.

9. **`min_confidence` is a strategy gate, not a risk gate.** `signal_trader._min_confidence = max(0.45, settings.signal_min_confidence)` (default 0.65) is enforced in `_scan_markets` (`strategies/signal_trader.py:145`) but NOT in `risk_manager.check_order`. A manual `POST /api/trade` could submit with no confidence floor.

10. **Live TP/SL exits don't fire.** `core/position_manager.py:135, 209` unconditionally calls `paper_sim.create_order(...)` even when `settings.paper_trade == False`. Live positions have no automated exit management.

11. **`open_orders` are not persisted.** `store.save_to_disk()` writes `positions, trades, daily_pnl, paper_balance, peak_equity, equity_history` but NOT `open_orders`. A restart loses all open-order state — paper orders become orphaned in memory and never re-hydrated. Live orders are similarly lost from local state (though they persist on the exchange).

12. **Live order errors are swallowed without retry.** `clob_client.create_order` catches `httpx.HTTPStatusError` and generic `Exception`, logs, and returns `None`. `BaseStrategy.submit_order` then returns `None` (`strategies/base.py:128-129`). The order is silently dropped — no retry, no backoff, no dead-letter queue, no alerting beyond the log line.

---

## 10. Missing Features

(`VERIFIED` absent unless noted.)

### §7 — Bot Lifecycle

- **Live fill acknowledgement loop.** Required: periodic polling of `clob_client.get_trades(maker_address)` (e.g. 2s cadence) OR a WebSocket user-channel subscription. The CLOB WS client `core/ws_client.py` exists but is retired.
- **Live order state transitions.** Required: `BaseStrategy.submit_order` should call `order_state_machine.create_order(...)` → `transition(VALIDATED)` → `transition(SUBMITTED)` → `transition(ACKNOWLEDGED)` (on CLOB response) → `transition(OPEN)` → `transition(FILLED)` (on fill ack).
- **Live reconciliation job.** Required: periodic diff of `store.open_orders` vs `clob_client.get_open_orders()` and `store.positions` vs `clob_client.get_positions()`. The existing `core/reconciliation.py` framework can be extended.
- **Partial-fill handling.** Required: `paper_sim._execute_fill` should compute `fill_size = min(order.size_remaining, top_of_book_depth)` and transition to `PARTIALLY_FILLED` when `fill_size < size_remaining`.
- **Order retry with idempotency.** Required: `clob_client.create_order` should accept an `idempotency_key` argument and retry with the same key on transient failure.
- **Live TP/SL routing.** Required: `position_manager` should branch on `settings.paper_trade` and call `clob_client.create_order` for live exits.

### §8 — Order Management System

- **Idempotency key enforcement on submission.** `generate_idempotency_key(strategy, token_id, side, price, size)` exists but is never sent to the exchange or checked against the local `order_transitions` table.
- **Duplicate fill detection.** Required: `INSERT OR IGNORE` on `(order_id, fill_id)` or a UNIQUE constraint on the trade_id column in the execution_quality / closed_positions tables.
- **Stale order handling for live orders.** `signal_trader._recycle_stale_orders` only operates on paper orders via `paper_sim.cancel_order`. Live orders need `clob_client.cancel_order`.
- **Order amendment.** Not implemented — cancelling and re-submitting is the only option.
- **TIF (Time-In-Force) enforcement.** `OrderState.EXPIRED` is defined in the state machine but no code path transitions to it. The arb_scanner's `_recycle_stale_orders` uses a 180s soft timeout via cancellation, not expiry.

### §9 — Execution Quality

- **Signal-time mid-price capture.** Required: `signal_trader._evaluate_market` should record the market mid at signal time and propagate it through `decision_id` → `paper_sim.create_order` → `record_execution(signal_price=signal_time_mid)`. Currently `signal_price` defaults to `order.price` (the limit).
- **Submitted-price divergence capture.** Required: a code path where the broker re-prices or the strategy amends the limit before submission, recording `submitted_price != decision_price`.
- **Live execution-quality recording.** Required: `clob_client.create_order` ack path should call `execution_quality.record_execution` with the CLOB fill price.
- **Fee tracking.** Schema has no `fees` column. Polymarket's `feeRateBps=0` is hard-coded in the CLOB payload — but if fees were ever non-zero, the `realized_edge` computation would be wrong.

### §10 — Execution Safety

- **Slippage tolerance enforcement on the live path.** `SLIPPAGE_TOLERANCE_HEALTHY_BPS = 15` is defined but only checked inside `smart_router.plan_execution`, which is never called from `submit_order`.
- **`min_liquidity` enforcement.** Schema field exists; no enforcement.
- **`min_edge` enforcement on the risk path.** `portfolio_optimizer.DEFAULT_MIN_EDGE = 0.03` exists; not consulted by `risk_manager.check_order`.
- **`min_confidence` enforcement on the risk path.** Strategy gate only.
- **Per-order max size cap.** `max_order_size_usd` schema field exists (`enterprise_schemas.sql:353`); not enforced. The risk gate enforces `MAX_POSITION_PER_MARKET` (per-market cap) but not a per-order cap.

---

## 11. Bugs

(`VERIFIED` unless noted. Severity: P0 = production blocker, P1 = correctness bug, P2 = cosmetic / robustness.)

| ID | Severity | Description | File:line | Evidence |
|---|---|---|---|---|
| B-01 | P0 | Order state machine is bypassed on the production trade path. `transition()` invoked only in `paper_sim.cancel_order` (wrapped in `try/except: pass`). The `Order` dataclass in `order_state_machine.py` is a different class than `core.data_store.Order`. | `paper/simulator.py:139`, `strategies/base.py:60-148` | VERIFIED |
| B-02 | P0 | No live fill acknowledgement. `clob_client.get_trades()` never called; no WS fill subscription; `store.open_orders` retains live orders as OPEN indefinitely. | `core/clob_client.py:365-370` | VERIFIED |
| B-03 | P0 | No idempotency on live order submission. Fresh `uuid.uuid4()` order_id + random 16-byte nonce per call. Duplicate strategy decisions produce duplicate orders. | `core/clob_client.py:270, 318` | VERIFIED |
| B-04 | P0 | No live reconciliation of open orders / positions against CLOB truth. | `core/reconciliation.py:48-...` | VERIFIED |
| B-05 | P0 | Live TP/SL exits don't fire — `position_manager` calls `paper_sim.create_order` unconditionally. | `core/position_manager.py:135, 209` | VERIFIED |
| B-06 | P1 | Three-tier price waterfall is collapsed — `signal_price == decision_price == submitted_price == order.price` for every recorded fill. `realized_edge` measures crossing cost, not model edge retention. | `paper/simulator.py:307`, `core/execution_quality.py:278` | VERIFIED |
| B-07 | P1 | `open_orders` not persisted to disk. A restart loses all open-order state. | `core/data_store.py:310-355` | VERIFIED |
| B-08 | P1 | No partial fills modeled — `fill_size = order.size_remaining` is single-shot. | `paper/simulator.py:242` | VERIFIED |
| B-09 | P1 | Live order errors silently swallowed — `clob_client.create_order` returns `None` on exception; `BaseStrategy.submit_order` returns `None`; no retry, no alert. | `core/clob_client.py:342-347`, `strategies/base.py:128-129` | VERIFIED |
| B-10 | P1 | Smart Order Router slippage tolerance (`15 BPS` healthy / `8 BPS` drift) never enforced on submission — `plan_execution` not called from `submit_order`. | `execution/smart_router.py:136-149`, `strategies/base.py:60-148` | VERIFIED |
| B-11 | P1 | `min_liquidity` schema field exists but is never enforced. | `core/db/migrations/001_initial_enterprise_schemas.sql:360` | VERIFIED |
| B-12 | P1 | `min_edge` from `portfolio_optimizer` not consulted by `risk_manager.check_order` or `signal_trader`. | `core/portfolio_optimizer.py:109`, `risk/manager.py:165-348` | VERIFIED |
| B-13 | P1 | Mark-to-market exposure gate is fail-open — `try/except: pass` swallows exceptions from `compute_mark_to_market_exposure`. | `risk/manager.py:308-315` | VERIFIED |
| B-14 | P2 | `_recycle_stale_orders` only operates on paper orders via `paper_sim.cancel_order` — live stale orders are not recycled. | `strategies/signal_trader.py:109` (per `STALE_ORDER_SECONDS=180` comment at line 43) | LIKELY (call site not deep-read in this session) |
| B-15 | P2 | `duplicate_fill_anomaly` in `core/portfolio.py:99` is a passive counter — it surfaces duplicate trade_ids in the report but does not actively reject duplicates at fill time. | `core/portfolio.py:99` | VERIFIED |
| B-16 | P2 | `core/data_store.py::OrderStatus` enum lacks `REJECTED` and `EXPIRED` (only `OPEN, FILLED, CANCELLED, PARTIALLY_FILLED`), so the production store cannot represent those states even if the state machine could. | `core/data_store.py:32-36` | VERIFIED |

---

## 12. Technical Debt

(`VERIFIED`.)

1. **Two parallel Order representations.** `core.data_store.Order` (mutable dataclass, used everywhere) vs `core/order_state_machine.Order` (frozen dataclass, used only in tests). They should be unified — likely by having `data_store.Order` carry an `order_state_machine.Order` instance or by migrating all consumers to the frozen dataclass.

2. **Bare `except: pass` in critical paths.** `paper/simulator.py:140` swallows state-machine transition failures on cancel. `risk/manager.py:314` swallows MTM computation failures. `paper/simulator.py:308` swallows execution-quality recording failures. Each is documented as best-effort, but the cumulative effect is silent failure accumulation in the audit trail.

3. **Local imports inside hot paths.** `paper/simulator.py` does `from core.decision_ledger import decision_ledger` inside `_execute_fill` (line 279), `from core.execution_quality import record_execution` inside `_execute_fill` (line 306), `from risk.manager import risk_manager` inside `_execute_fill` (line 254). Same pattern in `position_manager.py` (lines 86, 113, 159, 187). These were added to break circular imports, but they add ~microsecond overhead per fill and obscure the dependency graph.

4. **5101-line `api/server.py`.** The file holds 80+ routes, the lifespan function, the auth policy, the WS connection manager, the market-seeding helper, and several background-task loops. It should be split into `api/routes/trading.py`, `api/routes/risk.py`, `api/routes/execution.py`, `api/lifespan.py`, etc.

5. **Inconsistent persistence models.** `core/data_store` uses JSON-on-disk; `decision_ledger`, `execution_quality`, `closed_positions`, `order_state_machine`, `audit_trail` use SQLite; `core/timescale_db.py` targets TimescaleDB / PostgreSQL. The async pool (`core/db_pool.py`, `core/async_repositories.py`) only reads from SQLite. There is no unified ORM or migration story across the SQLite databases — each module owns its own schema and `_init_db()` call.

6. **`max_offset=2000` market-discovery safety ceiling.** `core/market_discovery.py:73` caps catalog sync at 2000 markets. As Polymarket grows, this will silently truncate the universe. Should be configurable or removed.

7. **WS client retained but unused.** `core/ws_client.py` is imported, instantiated, but never started. `main.py:99-100` and `api/server.py:407-408` document the KD-08/KD-24 retirement. The code should either be deleted or re-enabled with a clear plan.

8. **`OrderStatus` enum in `data_store.py` is incomplete.** Missing `REJECTED` and `EXPIRED` — see B-16.

9. **Magic constants throughout.** `STALE_ORDER_SECONDS=180`, `MODEL_SAVE_INTERVAL=300`, `MAX_MARKETS_TO_QUOTE=8`, `SLIPPAGE_DEPTH_BUCKET=50.0`, `MIN_KELLY_NUMERATOR=0.02`, `PER_TRADE_MAX_LOSS=0.50`, `STRATEGY_COOLDOWN=300.0` — most are not exposed via `config.py` and not tunable at runtime.

10. **`tests/test_paper_simulator.py` imports `fresh_store` and tests against a singleton.** The conftest at `tests/conftest.py:368` notes the singleton calls `load_from_disk()` at import time, requiring test isolation tricks.

---

## 13. Data Problems

(`VERIFIED`.)

1. **`signal_price` always equals `order.price`** (B-06). The execution-quality ledger cannot answer "did we capture the model's theoretical edge?" — only "did the fill beat the limit?".

2. **`store.open_orders` is in-memory only and not persisted.** Restart loses open-order state (B-07). Cross-restart order continuity is impossible without re-fetching from the CLOB.

3. **No reconciliation of `store.positions` vs CLOB positions.** Live fills update `store.positions` only if `paper_sim._execute_fill` runs (which it doesn't for live orders). The local position state is therefore always stale in live mode after the first fill.

4. **`equity_history` is a list, unbounded.** `core/data_store.py` appends to `self.equity_history` on every fill; persistence writes the whole list to JSON. Long-running bots will accumulate unbounded history and slow down `save_to_disk`.

5. **`peak_equity` rebasing logic** (`core/data_store.py:371-377`) is fragile. If the bankroll baseline ever changes (e.g. operating capital re-approval), the peak is re-based — but if it changes mid-session without a restart, the in-memory peak_equity is stale.

6. **TimescaleDB / PostgreSQL pool initialised even in paper mode.** `api/server.py:377-378` unconditionally calls `timescale_db.init_postgres_pool()`. If PostgreSQL is unavailable, this logs an error but continues — paper-mode bots shouldn't depend on a Timescale backend.

7. **`market_discovery.catalog` normalisation drops the raw `tokens` array.** `strategies/signal_trader.py:117-120` documents that calling `gamma_client.extract_token_ids(mkt)` on normalized catalog records returns `[]` because the `tokens` field was stripped. The workaround uses the catalog key directly, but the normalisation is a silent data-loss footgun.

---

## 14. Performance Problems

(`VERIFIED` unless noted.)

1. **`risk_manager._check_order_impl` holds `self._lock` for the entire duration.** Every `check_order` call serialises through `async with self._lock` — including the `compute_mark_to_market_exposure()` await (which itself may acquire other locks) and the multiple `store.total_exposure()` / `store.exposure_for_market()` awaits. Under concurrent strategy loops (signal_trader 15s, market_maker 4s, arb_scanner 5s+), this can become a bottleneck.

2. **`compute_mark_to_market_exposure` is called on every order.** `risk/manager.py:308-315`. UNVERIFIED what the implementation does, but if it iterates `store.positions` and fetches a fresh book for each, it could be O(n_positions × n_tokens) per order.

3. **`book_poller` polls 50 Tier-1 tokens every 2s.** That's 25 req/s sustained, which is well within Polymarket's rate limits but produces significant asyncio task overhead.

4. **`store.order_books` is a plain dict.** Reads inside `paper_sim._try_fill_orders` iterate all open paper orders per 1s tick and call `store.get_order_book(token_id)` per order. If open orders scale to 100+, this is 100+ dict lookups per second.

5. **`market_discovery.sync_full_catalog` is synchronous from the event loop's perspective.** It uses `httpx.AsyncClient` so it doesn't block the loop, but a 20-page pagination with 20s timeouts per page could take up to 400s in the worst case — overlapping with the 180s sleep, the loop could pile up.

6. **No query-result caching on the v2 async read endpoints.** `GET /api/v2/decisions/recent` hits SQLite on every call. The W11-2 TTL cache is wired for the analytics/attribution endpoints but not for the v2 async decision / observability reads.

7. **`execution_quality.record_execution` opens a fresh `sqlite3.connect(DB_PATH)` per fill.** Paper fills are ~1/second; SQLite handles this fine, but a hot live-trading session (10+ fills/second) would benefit from a connection pool.

---

## 15. Reliability Problems

(`VERIFIED`.)

1. **No retry on `clob_client.create_order`.** A transient HTTP 5xx or network blip silently drops the order. Combined with B-03 (no idempotency), a retry would risk duplicates — so the absence of retry is also the absence of a duplicate-order risk.

2. **No circuit breaker on `clob_client` calls.** `core/circuit_breaker.py` exists with documented decorator usage, but UNVERIFIED whether it's applied to `clob_client.create_order`. The book_poller has its own internal 30-result rolling window breaker; `gamma_client` and `clob_client` do not appear to have one.

3. **`paper_sim._fill_loop` is a single `asyncio.Task`.** If it crashes (e.g. `store.get_open_orders()` raises), the `except Exception: log.debug` swallows the error and the loop continues — but if the task itself is cancelled (e.g. `paper_sim.stop()` during shutdown), there's no auto-restart. `watchdog.beat("paper_sim")` is called only at startup, not on each fill cycle.

4. **`store.save_to_disk` is called from `_state_persistence_loop` (60s cadence).** A crash between persistence cycles loses up to 60s of state. The atomic `tmp_file.replace(STATE_FILE)` is good, but the cadence is too coarse for a trading system.

5. **Migration runner runs at every startup.** `api/server.py:292-326` calls `run_migrations` against 10 SQLite DBs. The migrations are idempotent (`CREATE TABLE IF NOT EXISTS`), but a future non-additive migration could lock the DB at startup.

6. **`clob_client.derive_api_key()` failure in live mode raises `typer.Exit(1)` from `main.py:85-87` but in `serve` mode (`main.py:91-93`) the failure is silently swallowed (`except Exception: pass`).** The server starts but live trading would fail on the first order with `RuntimeError("Not authenticated")` (`core/clob_client.py:261-262`).

7. **Watchdog auto-kill is opt-in via `settings.tripwire_auto_kill`.** If an operator forgets to enable it, critical tripwires (daily loss, weekly loss, drawdown) emit findings but don't activate the kill switch — the risk_manager's own gates would still catch them at the next `check_order`, but a runaway strategy could submit a flurry of orders between tripwire firing and the next check.

---

## 16. Security Problems

(`VERIFIED` unless noted. Mostly out-of-scope for §7-10 but noted for completeness.)

1. **`POLY_PRIVATE_KEY` is loaded via `settings` and held in memory by `clob_client._key`.** UNVERIFIED whether `settings` reads from env var or .env file — but the key is plaintext in memory for the process lifetime.

2. **API token strength check is fail-LOUD-on-weak but does NOT crash the server.** `api/server.py:328-351` — a weak token (`API_TOKEN=test`) logs a WARNING and writes an audit event but the server still starts. The auth middleware fails-closed (503) only on empty tokens.

3. **`POST /api/client-errors` and `/graphql` are public (no auth).** Documented at `api/server.py:151-167`. The client-errors endpoint only accepts opaque JSON (no PII / order body), so unauthenticated exposure is safe. The GraphQL schema is Query-only (no Mutation), so unauthenticated exposure can't trigger a trade.

4. **`/metrics` is public.** Prometheus scrapers use their own auth at the ingress layer (`api/server.py:145-148`). The endpoint emits only metric values, no PII / order body / secrets.

5. **EIP-712 signature uses `signatureType=0` (EIP-712) and `feeRateBps=0`.** UNVERIFIED whether the CLOB enforces `feeRateBps=0` server-side or whether a misconfigured client could under- or over-pay fees.

6. **`taker = "0x0000000000000000000000000000000000000000"`** (`core/clob_client.py:269`). The zero address is the canonical "any taker" marker for Polymarket limit orders, but UNVERIFIED whether the CLOB accepts this universally.

7. **Penetration tests exist** (`tests/test_penetration.py`) — UNVERIFIED content in this session, but the existence is positive evidence of security diligence.

---

## 17. Testing

(`VERIFIED` from `tests/` directory listing + test-file inspection.)

### Coverage

- **Unit tests**: 80+ test files covering every core module. Notable:
  - `tests/test_order_state_machine.py` — 6+ tests for transitions, idempotency, SQLite persistence.
  - `tests/test_execution_quality.py` — execution-quality ledger tests.
  - `tests/test_paper_simulator.py` — paper simulator slippage / fill tests.
  - `tests/test_smart_router.py` — 12 tests for SmartOrderRouter (slippage, TWAP/VWAP/Iceberg selection, drift-adaptive tolerance).
  - `tests/test_risk_manager.py` — risk gate tests.
  - `tests/test_signal_trader.py` — signal generation tests.
  - `tests/test_position_manager.py` — TP/SL exit tests.
  - `tests/test_decision_ledger.py` — decision ledger tests.
  - `tests/test_e2e_decision_chain.py` — E2E test for PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL chain.
- **Integration tests** (`tests/integration/`): `test_decision_chain.py`, `test_risk_pipeline.py`, `test_observability_pipeline.py`, `test_ml_pipeline.py`, `test_cache_pipeline.py`.
- **Contract tests** (`tests/contract/`): `test_api_contracts.py`, `test_consistency.py`, `test_error_contracts.py`.
- **Load tests** (`tests/load/`): `test_benchmarks.py`, `locustfile.py`.
- **Penetration tests**: `tests/test_penetration.py`.

### Gaps

1. **No test exercises the live CLOB submission path.** `clob_client.create_order` is not tested against a real or mock CLOB. `tests/test_clob_client.py` exists — UNVERIFIED content, but the live EIP-712 signing + POST is not exercised end-to-end.
2. **No test verifies that `order_state_machine.transition()` is invoked on the production order path.** The state-machine tests (`tests/test_order_state_machine.py`) test the API in isolation; no integration test asserts that `submit_order` writes to `order_state_machine.db`.
3. **No test for live fill acknowledgement.** `clob_client.get_trades()` is not tested in a fill-ack loop.
4. **No test for live reconciliation.** `core/reconciliation.py::run_reconciliation` is tested for timescale_db tables, not for orders/positions vs CLOB.
5. **No test for partial fills.** The paper simulator's single-shot fill model is not tested against a multi-level book walk.
6. **No test that `signal_price != order.price` in execution_quality.** The collapse of the three-tier waterfall (B-06) is not caught by any test because no test passes a `signal_price` other than `order.price`.
7. **Pre-existing failures** (per W16-7 worklog entry): `tests/test_backtest_report.py` (VaR-95 calculation), `tests/test_portfolio_optimizer.py` (diversification_ratio), `tests/test_feature_store.py` (PermissionError), `tests/test_db_indexes.py` (flaky timing). These are unrelated to the bot & execution engine but indicate test-suite rot.

---

## 18. Observability

(`VERIFIED`.)

### Channels

- **Decision ledger** (`core/decision_ledger.py`): SQLite append-only `decision_events` table; `decision_id`-keyed stage chain (PREDICTION / SIGNAL / RISK_APPROVED / RISK_REJECTED / ORDER / FILL). HTTP: `GET /api/decision/{token_id}`, `GET /api/decisions/rejected`, `GET /api/v2/decisions/recent`.
- **Execution quality** (`core/execution_quality.py`): SQLite `execution_quality` table with 21 columns + 7 indexes. HTTP: `GET /api/execution-quality`.
- **Observability metrics** (`core/observability.py`): SQLite `metrics` table. HTTP: `GET /api/observability`, `GET /api/observability/history/{name}`, `GET /api/v2/observability/latest`.
- **Closed positions** (`core/closed_positions.py`): SQLite `closed_positions` table. HTTP: `GET /api/positions/closed`, `GET /api/positions/closed/stats`.
- **Attribution** (`core/attribution.py`): 7-dimension roll-up. HTTP: `GET /api/attribution`.
- **Audit logger** (`core/audit_logger.py`): SQLite `audit_trail` table with `idempotency_key UNIQUE` constraint.
- **Immutable hash-chained audit** (`core/immutable_audit.py`, W17-5): hash-chained append-only log; entries reference the previous hash.
- **Prometheus** (`core/prometheus_metrics.py`): HTTP / trading / ML / system counters + histograms; exposed at `/metrics`.
- **WebSocket broadcast** (`core/ws_broadcast.py`): channels `trades`, `orders`, `positions`, `alerts`, `system`.
- **Watchdog** (`core/watchdog.py`): heartbeat monitoring + tripwire findings; HTTP: `GET /api/system/health`.
- **Profiling** (`core/profiling.py`, W15-4): per-endpoint p50/p95/p99 latency; HTTP: `GET /api/profiling/stats`, `/slowest`, `POST /api/profiling/reset`.
- **Rate-limit tracker** (`core/rate_limit_tracker.py`): per-endpoint / per-IP / per-minute hit series for the dashboard.

### Gaps

1. **No execution-quality recording for live orders** (B-06, B-09) — the ledger only captures paper fills.
2. **No live fill acknowledgement metric** — there's no counter for "live fills detected" vs "live fills missed".
3. **`store.open_orders` count is observable but `store.positions` drift vs CLOB is not** — no metric for `local_position_count - clob_position_count`.

---

## 19. Production Readiness

### Paper mode: READY (`VERIFIED`)

- End-to-end pipeline functional.
- Risk gate comprehensive (22 gates).
- Decision ledger traces the full lifecycle.
- Execution-quality ledger captures per-fill metrics.
- State persistence survives restarts (with the `open_orders` caveat, B-07).
- Durable kill switch + watchdog tripwires.
- Comprehensive test suite (80+ test files).

### Live mode: NOT READY (`VERIFIED`)

| Blocker | Severity | Section |
|---|---|---|
| No live fill acknowledgement | P0 | §11 B-02 |
| No live order state transitions | P0 | §11 B-01 |
| No idempotency on live submission | P0 | §11 B-03 |
| No live reconciliation | P0 | §11 B-04 |
| Live TP/SL exits don't fire | P0 | §11 B-05 |
| No partial fills modeled | P1 | §11 B-08 |
| Three-tier price waterfall collapsed | P1 | §11 B-06 |
| Live order errors silently swallowed | P1 | §11 B-09 |
| Slippage tolerance not enforced | P1 | §11 B-10 |
| `min_liquidity` / `min_edge` not enforced | P1 | §11 B-11, B-12 |

### Mitigations already in place

- `core/live_safety_gate.py` provides a 10-check staged gate that MUST pass before live trading is enabled. As long as the gate's checks (paper_mode_24h, positive_expectancy, etc.) are honoured, live trading cannot be accidentally enabled before the gaps above are closed. However, the gate does NOT verify fill acknowledgement, state-machine integration, or reconciliation — those would need to be added as additional checks (#11, #12, #13) before live mode is genuinely safe.

---

## 20. Evidence

(Citations are `file:line` — all read in this session.)

### §7 — Bot Lifecycle evidence

- `main.py:54-105` — `_startup()` initialises paper_sim, clob_client, credentials.
- `main.py:99-100` — WS client explicitly NOT started (KD-08/KD-24 D5 decision: REST polling).
- `api/server.py:279-540` — `lifespan()` async context manager: migrations → token-strength check → live-mode guards → timescale pool → watchdog → paper_sim → market_seeding → market_discovery → book_poller → settlement/fundamental/position_manager → strategy_registry → training_orchestrator → label_backfill → background tasks.
- `api/server.py:380-423` — 9 watchdog-registered subsystems; 3 base strategies started.
- `api/server.py:490-531` — shutdown sequence cancels every background task, calls `stop()` on each subsystem, closes DB pool.
- `core/market_discovery.py:54-80` — `_discovery_loop` every 180s; `sync_full_catalog` paginates Gamma with `max_offset=2000` ceiling.
- `core/book_poller.py:22-80` — tiered polling (Tier 1: 2s × 50 tokens; Tier 2: 6s); Semaphore=12; 6s timeout; 30-result rolling window circuit breaker.
- `strategies/signal_trader.py:40-150` — `SCAN_INTERVAL=15s`, `MODEL_SAVE_INTERVAL=300s`, `STALE_ORDER_SECONDS=180`, `KELLY_FRACTION=0.25`, `MIN_KELLY_NUMERATOR=0.02`; `_min_confidence = max(0.45, settings.signal_min_confidence)`.
- `strategies/base.py:60-148` — `submit_order`: provisional Order → `risk_manager.check_order` → `decision_ledger.record(RISK_APPROVED/REJECTED)` → paper/live branch.
- `paper/simulator.py:60-159` — `start()` creates `_fill_loop` task (1s cadence); `_try_fill_orders` iterates open paper orders.
- `paper/simulator.py:177-225` — `_apply_slippage`: 1 tick crossing + 0.5 tick / 50-share depth + 0/1 tick queue (deterministic SHA-256 of order_id).
- `paper/simulator.py:241-309` — `_execute_fill`: `fill_size = order.size_remaining` (single-shot); P&L `(fill_price - avg_entry) * fill_size` (SELL only); `risk_manager.report_trade_pnl`; `store.record_fill`; `store.update_order(FILLED)`; `decision_ledger.record(FILL)`; `execution_quality.record_execution(signal_price=order.price)`.
- `core/position_manager.py:35-240` — 5s loop; TP/SL trigger; cancels prior exit; re-clears risk gate (V3 fix); unconditionally calls `paper_sim.create_order` for exits.
- `core/clob_client.py:256-347` — `create_order`: EIP-712 signing, fresh `uuid.uuid4()` order_id + random 16-byte nonce, POST /order, returns None on error.
- `core/clob_client.py:365-370` — `get_trades(maker_address, limit)`: defined but never called from production code.

### §8 — Order Management System evidence

- `core/order_state_machine.py:85-103` — 10-state `OrderState` enum.
- `core/order_state_machine.py:125-162` — `ALLOWED_TRANSITIONS` table (22 edges); terminal states map to empty frozenset.
- `core/order_state_machine.py:188-216` — frozen `Order` dataclass (13 fields).
- `core/order_state_machine.py:220-248` — `generate_idempotency_key(strategy, token_id, side, price, size)` — SHA-256 over canonical pipe-delimited string.
- `core/order_state_machine.py:308-337` — `transition(order, new_state)` — pure; uses `dataclasses.replace`; raises `InvalidTransition` on illegal moves.
- `core/order_state_machine.py:341-573` — `OrderStateMachine` SQLite persistence: `order_transitions` table (append-only); indexes on `(order_id, ts ASC)`, `(idempotency_key, ts DESC)`, `(token_id, ts DESC)`.
- `paper/simulator.py:139` — **ONLY production call site** of `transition()` (CANCELLED, wrapped in `try/except: pass`).
- `core/data_store.py:32-36` — `OrderStatus` enum has only `OPEN, FILLED, CANCELLED, PARTIALLY_FILLED` — missing `REJECTED, EXPIRED`.
- `core/portfolio.py:99` — `duplicate_fill_anomaly = len(trades) - len({t.trade_id for t in trades})` — passive counter.
- `core/reconciliation.py:48-...` — reconciles timescale_db tables only; no orders/positions vs CLOB reconciliation.

### §9 — Execution Quality evidence

- `core/execution_quality.py:39-45` — schema columns: `signal_price, decision_price, submitted_price, best_bid, best_ask, expected_fill, actual_fill, spread, slippage, slippage_bps, latency_ms, realized_edge, paper, data_json`.
- `core/execution_quality.py:138-217` — 7 indexes: `(timestamp DESC)`, `(strategy, ts DESC)`, `(token_id, ts DESC)`, `(decision_id)`, `(slippage_bps DESC)`, `(side, ts DESC)`, `(paper, ts DESC)`, `(order_id)`.
- `core/execution_quality.py:230-373` — `record_execution(order, fill_price, signal_price=None)`: resolves book from `store.order_books`; computes `expected_fill` (BUY→best_ask, SELL→best_bid); `slippage = actual - expected`; `slippage_bps = slippage / abs(expected) * 10_000`; `realized_edge = (sig_px - actual)` for BUY, `(actual - sig_px)` for SELL.
- `core/execution_quality.py:276-278` — `sig_px = signal_price or order.price`; `decision_px = order.price`; `submitted_px = order.price` — **all three collapse to `order.price` when caller passes `signal_price=order.price`**.
- `paper/simulator.py:307` — production caller passes `signal_price=order.price`.
- `paper/simulator.py:305-308` — wrapped in `try/except: pass`.

### §10 — Execution Safety evidence

- `risk/manager.py:42-65` — module-level constants: `OPERATING_CAPITAL=$100`, `BANKROLL_CEILING=$200`, `MIN_CASH_RESERVE=$40`, `MAX_DEPLOYABLE_CAPITAL=$60`, `DEFAULT_EXPERIMENTAL_POSITION=$1`, `NORMAL_MAX_POSITION=$2`, `MAX_POSITION_PER_MARKET=$3`, `ABSOLUTE_MAX_POSITION=$5`, `MAX_CORRELATED_EXPOSURE=$8`, `MAX_STRATEGY_EXPOSURE=$15`, `MAX_TOTAL_OPEN_RISK=$25`, `MAX_PENDING_ORDER_CAPITAL=$10`, `MAX_OPEN_POSITIONS=8`, `DAILY_LOSS_STOP=$2`, `WEEKLY_LOSS_STOP=$5`, `MAX_DRAWDOWN_LIMIT=$8`, `PER_TRADE_MAX_LOSS=$0.50`, `STRATEGY_COOLDOWN=300s`.
- `risk/manager.py:126-348` — `_check_order_impl`: 22 gates in order; see §5 trace.
- `risk/manager.py:142-163` — on rejection, schedules `shadow_trading.record_shadow_trade` via `asyncio.create_task` (fire-and-forget).
- `risk/manager.py:308-315` — MTM exposure gate is `try/except: pass` (fail-open).
- `core/safety.py:18-49` — `KILL_SWITCH_PATH = /app/data/kill_switch`; `kill_switch_file_exists()`, `write_kill_switch(reason)`, `clear_kill_switch()`, `read_kill_switch_reason()`.
- `core/watchdog.py:36-100` — `Watchdog` class; heartbeat registration; `run_checks` evaluates staleness + daily/weekly loss + drawdown + book stall.
- `core/live_safety_gate.py:9-39` — 10-check staged gate; `KILL_SWITCH_TESTED_PATH = /app/data/live_safety_kill_switch_tested`; `PAPER_MODE_MIN_SECONDS = 24h`, `MIN_CLOSED_TRADES = 20`, `MIN_WIN_RATE = 0.50`, `MAX_LIVE_DRAWDOWN_USD = $2.00`, `DRIFT_HEALTHY_STATUS = "HEALTHY"`.
- `execution/smart_router.py:23-24` — `SLIPPAGE_TOLERANCE_HEALTHY_BPS = 15.0`, `SLIPPAGE_TOLERANCE_DRIFT_BPS = 8.0`.
- `execution/smart_router.py:114-179` — `plan_execution`: slippage gate at lines 136-149 (rejects plan if `slippage_bps > tolerance`); selects direct/TWAP/VWAP/iceberg by size.
- `execution/smart_router.py:285-304` — module-level singleton `smart_router = SmartOrderRouter()`.
- **NOT FOUND**: any call site of `smart_router.plan_execution` or `smart_router._twap_slices` / `_vwap_slices` / `_iceberg_slices` from `strategies/base.py`, `paper/simulator.py`, `core/clob_client.py`, `core/position_manager.py`, or any other production module. The only callers are `core/analysis_engine.py:78` (`smart_router.calculate_slippage` — for metrics only) and `api/server.py:4547` (`router.plan` via `AdvancedOrderRouter` — plan-only endpoint).

---

## 21. Unknowns

(`UNVERIFIED` — not traced in this session.)

1. **`core/settlement.py` internals.** Started in lifespan but not deep-read. Unknown whether settlement interacts with live CLOB positions or only paper.
2. **`core/circuit_breaker.py` call sites.** The decorator is documented but UNVERIFIED whether it's applied to `clob_client.create_order` / `gamma_client.get_markets` / `book_poller._fetch_book`. The book_poller has its own internal circuit breaker (separate implementation).
3. **`httpx.AsyncClient` retry configuration.** Default `httpx` does not retry; UNVERIFIED whether `clob_client` or `book_poller` configure retries via a transport adapter.
4. **`tests/test_clob_client.py` content.** File exists but not read in this session — unknown whether it tests live EIP-712 signing against a mock CLOB.
5. **`core/job_queue.py` content.** File exists but not read — unknown whether it provides retry / backoff for trade submissions.
6. **`config.py` full surface.** Only spot-checked `signal_min_confidence: float = 0.65` (line 79) and `mm_*` / `arb_*` / `signal_*` settings referenced in strategies. Unknown whether `max_open_orders`, `tripwire_auto_kill`, `watchdog_heartbeat_timeout`, etc. have sensible defaults.
7. **`ml/model.py` predict path.** UNVERIFIED whether `ml_model.predict` is synchronous or async, and whether it can block the event loop.
8. **`core/audit_logger.py` idempotency.** The schema has `idempotency_key TEXT UNIQUE` (line 46), but UNVERIFIED whether `log_event` retries on UNIQUE violation or generates a fresh key on conflict (line 67 suggests fresh key fallback).
9. **`core/timescale_db.py` TimescaleDB / PostgreSQL pool behaviour** in paper mode. `api/server.py:377-378` unconditionally inits the pool — UNVERIFIED what happens on connection failure.
10. **Polymarket CLOB API idempotency contract.** UNVERIFIED whether the CLOB itself deduplicates orders by `(maker, nonce)` or by a client-supplied idempotency key. The bot does not send a client-supplied idempotency key.

---

## 22. Maturity Score

Per §61 criteria (0-10 scale, 10 = institutional-grade production-ready).

| Dimension | Score | Rationale |
|---|---|---|
| Lifecycle completeness (§7) | 6 / 10 | Paper path complete and tested. Live path missing fill acknowledgement (B-02), state-machine integration (B-01), reconciliation (B-04), TP/SL exits (B-05). |
| Order management (§8) | 4 / 10 | State machine correctly specified but NOT wired to production (B-01). Idempotency helper exists but not consulted (B-03). Duplicate-fill detection is passive (B-15). `OrderStatus` enum incomplete (B-16). |
| Execution quality (§9) | 6 / 10 | Schema correct in shape (21 columns, 7 indexes). Recording works for paper. Three-tier waterfall collapsed (B-06) — `signal_price == decision_price == submitted_price`. Live fills not recorded (B-02). |
| Execution safety (§10) | 8 / 10 | 22 institutional gates enforced on every submission path. Durable kill switch. 10-check live readiness gate. Per-trade-loss circuit breaker. MTM gate is fail-open (B-13). `min_liquidity` / `min_edge` / slippage tolerance not enforced on submission (B-10, B-11, B-12). |
| Observability | 8 / 10 | Decision ledger, execution-quality, audit trail, immutable hash-chained audit, Prometheus, WebSocket broadcast, watchdog, profiling. Gap: no live-fill-ack metric, no local-vs-CLOB drift metric. |
| Testing | 7 / 10 | 80+ test files. E2E decision-chain test. Integration tests. Contract tests. Penetration tests. Gaps: no live CLOB E2E, no state-machine integration test, no partial-fill test, no three-tier-waterfall divergence test. |
| Production readiness (paper) | 8 / 10 | Ready, with the `open_orders` not-persisted caveat (B-07). |
| Production readiness (live) | 3 / 10 | 5 P0 blockers (B-01 through B-05) prevent safe live trading. |

**Overall: 6 / 10** — paper-mode production-grade; live-mode has critical gaps that must be closed before any real-funds deployment. The `core/live_safety_gate.py` 10-check gate provides a backstop, but it does not currently verify fill acknowledgement, state-machine integration, or reconciliation — those should be added as checks #11, #12, #13 before the gate's "passed" verdict is treated as sufficient for live trading.

---

## 23. Critical Findings

Ranked by severity. Each finding cites evidence from §20.

### C-01 — Order state machine is NOT wired into the production trade path (P0)

**Evidence (VERIFIED):** `core/order_state_machine.py` defines a correct 10-state, 22-transition state machine with a frozen `Order` dataclass, deterministic `idempotency_key`, and SQLite append-only history. The ONLY production call site is `paper/simulator.py:139`, which invokes `transition(order_id, OrderState.CANCELLED, reason="manual cancel")` wrapped in `try/except: pass`. The state machine is never invoked on `CREATED`, `VALIDATED`, `SUBMITTED`, `ACKNOWLEDGED`, `OPEN`, `FILLED`, `REJECTED`, or `EXPIRED` transitions in production code. The `Order` dataclass in `order_state_machine.py` is a different class than `core.data_store.Order` used by `strategies/base.py`, `paper/simulator.py`, and `risk/manager.py` — they are not unified.

**Impact:** There is no enforced order-lifecycle audit trail. A crash between `clob_client.create_order` (POST /order) and `store.add_order` (local dict update) leaves the local state out of sync with the exchange with no recovery mechanism. The `order_state_machine.db` SQLite file is empty for production orders — only the test suite populates it. Duplicate orders, lost orders, and zombie orders (locally OPEN but exchange-FILLED) are undetectable.

**Remediation:**
1. Unify the `Order` dataclass — either migrate `core.data_store.Order` to import from `core.order_state_machine.Order` or vice versa.
2. In `strategies/base.py::submit_order`, call `order_state_machine.create_order(...)` after the risk gate passes; transition `CREATED → VALIDATED` before paper/live routing.
3. In `paper/simulator.py::create_order`, transition `VALIDATED → SUBMITTED` before `store.add_order`; transition `SUBMITTED → ACKNOWLEDGED` after `store.add_order` returns.
4. In `paper/simulator.py::_execute_fill`, transition `ACKNOWLEDGED → OPEN` (on first poll where the order is still open) and `OPEN → FILLED` (or `PARTIALLY_FILLED` for partial fills).
5. In `core/clob_client.py::create_order`, transition `SUBMITTED → ACKNOWLEDGED` on receipt of the CLOB response; transition `SUBMITTED → REJECTED` on `httpx.HTTPStatusError`.
6. Add an integration test that asserts `order_state_machine.get_history(order_id)` returns the full transition chain after a paper fill.

### C-02 — No live fill acknowledgement (P0)

**Evidence (VERIFIED):** `core/clob_client.py:365-370` defines `get_trades(maker_address, limit)` but it is never called from any production module. `paper/simulator.py:152-175::_fill_loop` (1s cadence) explicitly skips non-paper orders (`if not order.paper: continue` at line 164). `core/ws_client.py` is retained but explicitly NOT started (`main.py:99-100`, `api/server.py:407-408`).

**Impact:** For live orders, `store.open_orders` retains the order as `OPEN` indefinitely; `store.positions` never reflects the filled entry; `store.daily_pnl` never credits/debits the realised P&L; `decision_ledger.record(stage="FILL", ...)` is never invoked; `execution_quality.record_execution` is never invoked. The bot becomes blind to its own live positions after the first fill. Risk gates that depend on `store.total_exposure()` and `store.positions` (sections 4-7 of `_check_order_impl`) will under-count exposure — a live-filled position does not consume the per-market / per-strategy / correlated-group / total-open-risk caps, so subsequent orders can pile on risk past the institutional limits.

**Remediation:**
1. Add a `_live_fill_loop` (2s cadence) that calls `clob_client.get_trades(maker_address=self.address, limit=50)`, diffs against `store.trades` by `trade_id`, and for each new trade calls `paper_sim._execute_fill`-equivalent logic (P&L computation, `store.record_fill`, `store.update_order(FILLED)`, `decision_ledger.record(FILL)`, `execution_quality.record_execution`).
2. Alternatively, re-enable `core/ws_client.py` with a user-channel WS subscription to fill events (Polymarket CLOB supports WS user channels).
3. Add a Prometheus counter `live_fills_detected_total` and a gauge `local_vs_clob_position_drift` so drift is observable.

### C-03 — No idempotency on live order submission (P0)

**Evidence (VERIFIED):** `core/clob_client.py:270` mints `nonce = int.from_bytes(secrets.token_bytes(16), "big")` per call. `core/clob_client.py:318` mints `order_id = str(uuid.uuid4())` per call. `core/order_state_machine.py:220-248` defines `generate_idempotency_key(strategy, token_id, side, price, size)` but no production code calls it on the submission path. The SQLite index `idx_ord_idempotency ON order_transitions(idempotency_key, timestamp DESC)` exists but is never queried.

**Impact:** A duplicate strategy decision (e.g. a signal that fires twice within a scan interval, or a strategy that's started twice due to a config reload) produces two distinct live orders at the exchange. There is no client-side dedup. Polymarket's CLOB may or may not deduplicate by `(maker, nonce)` — UNVERIFIED — but the bot does not rely on it.

**Remediation:**
1. In `strategies/base.py::submit_order`, compute `idempotency_key = generate_idempotency_key(self.name, args.token_id, args.side, args.price, args.size)`.
2. Query `order_state_machine` for an existing order with that `idempotency_key` in a non-terminal state; if found, return the existing order instead of submitting a new one.
3. Pass the `idempotency_key` to `clob_client.create_order` as a client-supplied reference (if the CLOB API supports it) or at minimum log it alongside the CLOB `order_id` for post-hoc dedup analysis.

### C-04 — No live reconciliation of orders / positions vs CLOB truth (P0)

**Evidence (VERIFIED):** `core/reconciliation.py:48-...` reconciles timescale_db tables (`market_snapshots, orderbook_ticks, fundamental_news, ml_feature_store`) against engine-insert counters. It does NOT reconcile `store.open_orders` vs `clob_client.get_open_orders()` or `store.positions` vs CLOB positions. There is no `clob_client.get_open_orders()` method — UNVERIFIED whether the CLOB exposes one.

**Impact:** Even with C-02 fixed, drift can accumulate from missed fill acks, late cancel confirmations, exchange-side order expiries, or position resolutions. The local state becomes a fiction; risk gates enforce against a stale view; the dashboard displays wrong numbers.

**Remediation:**
1. Add `clob_client.get_open_orders()` and `clob_client.get_positions()` (if the CLOB supports them).
2. Add a periodic (60s) reconciliation job that diffs local vs CLOB and either auto-corrects local state or activates observation-only mode (`risk_manager.set_observation_mode(True, reason="reconciliation drift")`).
3. Extend `core/reconciliation.py::run_reconciliation` to include orders/positions drift in its report.

### C-05 — Live TP/SL exits don't fire (P0)

**Evidence (VERIFIED):** `core/position_manager.py:135` and `:209` unconditionally call `paper_sim.create_order(exit_order, strategy=strat, decision_id=...)` regardless of `settings.paper_trade`. There is no `if settings.paper_trade: ... else: clob_client.create_order(...)` branch in `PositionManager`.

**Impact:** A live position that hits its TP or SL trigger submits a paper exit order — which fills against the live book in the simulator's in-memory state but never reaches the exchange. The actual live position remains open and continues to accrue adverse P&L.

**Remediation:**
1. Branch `position_manager` exit submission on `settings.paper_trade`.
2. For live exits, call `clob_client.create_order(OrderArgs(token_id, price=book.best_bid, side=Side.SELL, size=pos.yes_shares))` and rely on C-02's fill-ack loop to detect the exit fill.

### C-06 — Three-tier execution-quality waterfall is structurally collapsed (P1)

**Evidence (VERIFIED):** `paper/simulator.py:307` is the only production caller of `execution_quality.record_execution`, and it passes `signal_price=order.price`. Inside `record_execution`, `core/execution_quality.py:276-278` sets `sig_px = signal_price or order.price`, `decision_px = order.price`, `submitted_px = order.price`. Therefore `signal_price == decision_price == submitted_price == order.price` for every recorded fill.

**Impact:** `realized_edge` measures `limit-vs-fill` (i.e. crossing cost / slippage), not `signal-vs-fill` (i.e. model edge retention). The §9 framework "Theoretical Edge → Executable Edge → Realized Edge" is not measurable with the current data. Operators cannot answer "are we capturing the model's theoretical edge after slippage and fees?".

**Remediation:**
1. In `strategies/signal_trader.py::_evaluate_market`, capture `signal_time_mid = book.mid` and propagate it through the `MarketSignal` → `submit_order` → `paper_sim.create_order` → `_execute_fill` → `record_execution` chain.
2. Change `paper/simulator.py:307` to pass `signal_price=order.signal_price` (a new `Order` field) instead of `signal_price=order.price`.
3. Add a test that asserts `signal_price != decision_price` in execution_quality when the signal-time mid differs from the limit.

### C-07 — Smart Order Router slippage tolerance is NOT enforced on submission (P1)

**Evidence (VERIFIED):** `execution/smart_router.py:136-149` rejects an `ExecutionPlan` if `slippage_bps > tolerance` (15 BPS healthy / 8 BPS drift). However, `strategies/base.py::submit_order` does NOT call `smart_router.plan_execution` or check the tolerance. The router is invoked only by `core/analysis_engine.py:78` (for slippage estimation in metrics) and by `POST /api/execution/plan` (which returns a plan but does not execute the slices). There is no call site of `smart_router.plan_execution` from any production submission path.

**Impact:** No live or paper order is ever rejected for excessive slippage. A signal that fires on a thin book can submit a marketable limit that crosses a 50-BPS spread, paying 50 BPS of slippage for an edge that may be only 20 BPS — a net -30 BPS trade that the smart_router would have rejected.

**Remediation:**
1. In `strategies/base.py::submit_order`, before calling `paper_sim.create_order` / `clob_client.create_order`, call `smart_router.plan_execution(book, side=args.side, total_size_usdc=args.price * args.size)`. If `plan.approved is False`, return `None` and record a `RISK_REJECTED` decision-ledger entry with the slippage-rejection reason.
2. Optionally route large orders through the router's TWAP/VWAP slicer (currently `plan_execution` only returns a plan — there is no executor).

### C-08 — `min_liquidity`, `min_edge`, and per-order max size are NOT enforced (P1)

**Evidence (VERIFIED):**
- `core/db/migrations/001_initial_enterprise_schemas.sql:360` — `min_liquidity_usd DOUBLE PRECISION NOT NULL DEFAULT 50.0` (schema field, never read).
- `core/portfolio_optimizer.py:109` — `DEFAULT_MIN_EDGE = 0.03` (not consulted by `risk_manager.check_order` or `signal_trader`).
- `core/db/migrations/001_initial_enterprise_schemas.sql:353` — `max_order_size_usd DOUBLE PRECISION NOT NULL DEFAULT 3.0` (schema field, never read).
- `risk/manager.py:165-348` — `_check_order_impl` enforces `MAX_POSITION_PER_MARKET` (per-market cap) and `ABSOLUTE_MAX_POSITION` (per-position cap), but NOT a per-order cap, NOT a `min_liquidity` floor, NOT a `min_edge` floor.

**Impact:** A manual `POST /api/trade` can submit an order on a market with $0 liquidity (empty book) — the order rests until cancelled or expired, consuming pending-order capital. A signal can fire on a 0.5-cent edge (well below the 3-cent `DEFAULT_MIN_EDGE`) and the risk gate does not reject it. A single order can be up to `MAX_POSITION_PER_MARKET = $3` (dynamic-scaled), which is also the per-position cap — meaning a single order can max out a market in one shot.

**Remediation:**
1. Add `min_liquidity` enforcement in `risk_manager._check_order_impl`: fetch `store.get_order_book(token_id)`, compute `book.bids[0].size * book.bids[0].price + book.asks[0].size * book.asks[0].price` (top-of-book liquidity), reject if below threshold.
2. Add `min_edge` enforcement: require a `predicted_edge` field on the `Order` dataclass; reject orders where `predicted_edge < min_edge` (configurable).
3. Add `max_order_size_usd` enforcement: reject orders where `order.price * order.size > max_order_size_usd`.

### C-09 — Live order errors are silently swallowed (P1)

**Evidence (VERIFIED):** `core/clob_client.py:338-347` — `create_order` catches `httpx.HTTPStatusError` (logs `Order rejected [%s]: %s`), catches generic `Exception` (logs `Order error: %s`), and returns `None`. `strategies/base.py:128-129` — on `None` return, the strategy returns `None` from `submit_order`. No retry, no backoff, no dead-letter queue, no alerting beyond the log line. No `decision_ledger.record(stage="ORDER_REJECTED" or "ORDER_FAILED", ...)` is recorded.

**Impact:** A transient HTTP 5xx, a network blip, or a CLOB rate-limit (HTTP 429) silently drops the order. The decision ledger shows `RISK_APPROVED` followed by no `ORDER` stage — the gap is visible only to an operator manually diffing the ledger.

**Remediation:**
1. Add a retry with exponential backoff (e.g. 3 retries: 1s, 2s, 4s) for transient failures (HTTP 5xx, network errors).
2. On permanent failure (HTTP 4xx other than 429), record `decision_ledger.record(decision_id, stage="ORDER_REJECTED", reason=...)`.
3. On exhaustion of retries, record `decision_ledger.record(decision_id, stage="ORDER_FAILED", reason=...)` and emit an alert via `core/alerting.py`.

### C-10 — `open_orders` not persisted to disk (P1)

**Evidence (VERIFIED):** `core/data_store.py:310-355` — `save_to_disk()` persists `daily_pnl, paper_balance, peak_equity, equity_history, positions, trades`. The dict `store.open_orders` is NOT in the persistence payload. `load_from_disk()` (`core/data_store.py:357-...`) restores only the persisted fields.

**Impact:** A restart loses all open-order state. Paper orders become orphaned in memory (the simulator's `_fill_loop` will never see them again because they're not in `store.open_orders` after restart). For live orders, the local store loses its record of what was submitted — combined with C-04 (no reconciliation), the bot cannot know whether a live order is still open, has filled, or was cancelled.

**Remediation:**
1. Add `open_orders` to the `save_to_disk()` payload.
2. Add a `load_from_disk()` restoration path for `open_orders`.
3. Alternatively, persist `open_orders` to a SQLite table (more robust than JSON for concurrent access).

---

*End of assessment.*
