# Bot Execution Engine — Improvement Plan

- **Domain:** Bot execution engine (order lifecycle, smart routing,
  circuit breakers, idempotency)
- **Owning modules:** `core/order_state_machine.py`,
  `core/execution_quality.py`, `core/position_manager.py`,
  `core/circuit_breaker.py`, `execution/smart_router.py`,
  `execution/advanced_router.py`, `paper/simulator.py`,
  `core/decision_ledger.py`, `core/data_store.py`,
  `core/settlement.py`
- **Priority classification (per God Mode §64):** mostly P0 (capital
  risk) and P1 (core architecture). Smart order routing and execution
  quality are P1.
- **Status as of W17-9:** IN PROGRESS — see per-improvement status
  below.

This plan defines every improvement in the bot execution engine using
the per-improvement field set required by God Mode §63. Each
improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md` (column: `IMPL-*`).

---

## Improvement BE-1 — Order State Machine Enhancements

- **Problem:** The current Order State Machine (OSM) in
  `core/order_state_machine.py` ships the canonical states
  (CREATED → SUBMITTED → PARTIAL → FILLED / CANCELLED / REJECTED /
  EXPIRED) with `InvalidTransition` enforcement, but several
  production-grade behaviours are missing: (a) no terminal-state
  guard for the partial-fill → CANCELLED path that has been observed
  to allow a race with a late fill, (b) no per-transition reason
  capture, (c) no idempotency-key enforcement at the state-machine
  layer, (d) no event log emission on transition.
- **Evidence:**
  - `tests/test_order_state_machine.py` (U6, Wave 4) — 8 tests pass,
    but cover happy path + `InvalidTransition` only.
  - `FINAL_SYSTEM_REASSESSMENT.md` §4 (V15) notes: "OSM is
    correct but lacks observability hooks; transitions are not
    recorded in the decision ledger."
  - Production incident log: 1 stale `PARTIAL` order in
    `data_store.db` from Wave 5 that took a late fill after the
    `CANCELLED` transition had already fired.
- **Current State:** OSM has 7 states, ~12 transitions, 8 tests.
  No transition-reason capture. No event-log emission. No idempotency
  key enforcement. Terminal-state guard is present for FILLED but not
  PARTIAL.
- **Desired State:** Every transition captures a reason string; every
  transition emits a structured event to the decision ledger
  (`stage=ORDER_TRANSITION`); terminal states are guarded with a
  "double-transition raises InvalidTransition" assertion; an
  idempotency key (`str`) is required at construction time; the
  state machine refuses to re-emit a transition that has the same
  idempotency key + target state.
- **Proposed Solution:**
  1. Add `OrderTransition` dataclass: `from_state`, `to_state`,
     `reason: str`, `idempotency_key: str`, `timestamp: float`,
     `metadata: dict`.
  2. Add `Order.transition_history: list[OrderTransition]` field.
  3. Enforce `idempotency_key` in `Order.__init__` (required).
  4. Add `_record_transition()` helper that appends to
     `transition_history` and (if `ledger` is wired) calls
     `DecisionLedger.record_order_transition(...)`.
  5. Extend `InvalidTransition` to carry `current_state`,
     `attempted_state`, `reason` so the error message is actionable.
  6. Add terminal-state guard: any transition FROM a terminal state
     raises `InvalidTransition("terminal_state_violation")`.
- **Architecture:**
  ```
  Order.__init__(id, idempotency_key, ...)
    └─→ state = CREATED
         └─→ .submit()  → state = SUBMITTED
              └─→ .fill(qty, partial=True)  → state = PARTIAL
                   └─→ .cancel(reason="user_request")
                        └─→ InvalidTransition if state in TERMINAL
              └─→ .fill(qty, partial=False) → state = FILLED
  Every transition calls _record_transition() → ledger + transition_history.
  ```
- **Implementation:**
  1. Extend `OrderTransition` and `Order` dataclasses in
     `core/order_state_machine.py`.
  2. Refactor every state-mutating method (`submit`, `fill`,
     `cancel`, `reject`, `expire`) to call `_record_transition()`.
  3. Add `InvalidTransition.__init__(current, attempted, reason)`.
  4. Add `Order.transition_history` property (immutable view).
  5. Wire `DecisionLedger.record_order_transition` — new stage
     `ORDER_TRANSITION` in `core/decision_ledger.py`.
  6. Update `tests/test_order_state_machine.py` to cover transitions
     + terminal guard + idempotency + ledger emission.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/order_state_machine.py`
    (extend + refactor)
  - `mini-services/polymarket-bot/core/decision_ledger.py`
    (new stage)
  - `mini-services/polymarket-bot/tests/test_order_state_machine.py`
    (expand from 8 → ~25 tests)
  - `mini-services/polymarket-bot/tests/test_decision_ledger.py`
    (new transition-stage tests)
- **Dependencies:** BE-5 (Idempotency) — the OSM uses the same
  idempotency-key primitive.
- **Risk:** HIGH — touches the central state machine. Mitigation: keep
  `Order` backwards-compatible (idempotency_key defaults to
  `uuid4().hex` if not supplied, so existing call sites in
  `signal_trader.py` / `market_maker.py` / `position_manager.py`
  keep working during the migration).
- **Priority:** P0 (capital risk — late fills after CANCELLED = real
  loss in live mode).
- **Expected Benefit:**
  - Eliminate the PARTIAL→CANCELLED→late-fill race.
  - Every order transition queryable in the decision ledger.
  - Foundation for shadow-vs-live order reconciliation.
- **Tests:**
  - 8 existing tests preserved.
  - +12 new tests covering transition history, terminal guard,
    idempotency, ledger emission, InvalidTransition payload.
- **Metrics:**
  - `osm_invalid_transitions_total` counter (Prometheus).
  - `osm_terminal_guard_violations_total` counter.
  - `osm_idempotency_replays_total` counter.
- **Acceptance Criteria:**
  - All 20+ OSM tests pass.
  - `osm_invalid_transitions_total == 0` over a 24-h paper session.
  - Every order in `data_store.db` has a non-empty
    `idempotency_key` column.
  - Every `ORDER_TRANSITION` ledger entry carries `reason` +
    `idempotency_key` + `from_state` + `to_state`.
- **Status:** IN PROGRESS.

---

## Improvement BE-2 — Execution Quality Improvements

- **Problem:** `core/execution_quality.py` records slippage, latency,
  and realized edge per fill, but (a) slippage is recorded only at the
  moment of fill — no tick-by-tick re-benchmark 1 min / 5 min / 15 min
  after the fill to catch adverse selection; (b) the realised-edge
  calculation does not separate maker vs taker fills; (c) no
  slippage-vs-order-size regression (a key signal for the smart
  router's TWAP / VWAP sizing decisions).
- **Evidence:**
  - `tests/test_execution_quality.py` (T12, Wave 3) — 13 tests pass.
  - `src/components/ExecutionQualityPanel.tsx` (W8-3) — renders
    avg/median/p95/worst slippage and a 5-bucket histogram, but the
    panel exposes no "1-minute-after" benchmark column.
  - Wave 16 introduced `execution/advanced_router.py` (TWAP/VWAP/
    iceberg) — without maker-vs-taker separation, the router cannot
    be tuned against the data it generates.
- **Current State:** Single-row-per-fill `execution_quality` table.
  Schema: `(id, timestamp, order_id, decision_id, token_id, strategy,
  side, signal_price, decision_price, submitted_price, best_bid,
  best_ask, expected_fill, actual_fill, spread, slippage,
  slippage_bps, latency_ms, realized_edge, paper, data_json)`. No
  benchmark-at-N-minutes columns. No maker/taker flag.
- **Desired State:** Per-fill row gains `liquidity_type` enum
  (`MAKER|TAKER`), `benchmark_price_1m`, `benchmark_price_5m`,
  `benchmark_price_15m` (NULL until the tick arrives; populated by a
  background benchmark task). Add a `execution_quality_vwaps` table
  capturing the VWAP of the order's lifetime + the strategy's VWAP
  for the day, so realized-vs-VWAP can be computed.
- **Proposed Solution:**
  1. Add `liquidity_type` column (default `TAKER`).
  2. Add `benchmark_price_1m` / `benchmark_price_5m` /
     `benchmark_price_15m` columns (NULL on insert).
  3. Add `execution_quality_vwaps` table: `(fill_id, order_vwap,
     strategy_vwap, token_vwap)`.
  4. Background benchmark task in `core/observability_collector.py`
    (every 30 s) — finds fills whose benchmark columns are NULL and
    whose age >= the benchmark horizon; looks up the historical
    mid from `orderbook_ticks` and writes the benchmark.
  5. New endpoint `GET /api/execution-quality/benchmarks` returning
    per-fill benchmark data + VWAP comparison.
  6. Update `ExecutionQualityPanel.tsx` (W8-3) with a "1m / 5m / 15m
    after" column group.
- **Architecture:**
  ```
  fill recorded
    └─→ execution_quality row (NULL benchmarks)
         └─→ observability_collector 30s tick
              └─→ if benchmark_age >= horizon and benchmark NULL:
                   look up orderbook_ticks mid at fill_time + horizon
                   write benchmark_price_<horizon>
  ExecutionQualityPanel.tsx polls /api/execution-quality?include_benchmarks=true
  ```
- **Implementation:**
  1. Migration `migrations/0XX_execution_quality_benchmarks.sql`
     (idempotent `ALTER TABLE ... ADD COLUMN`).
  2. Extend `record_execution()` to accept `liquidity_type`.
  3. Add `BenchmarkCollector` in `core/observability_collector.py`.
  4. Add `/api/execution-quality/benchmarks` route in
     `core/execution_quality.py::register_routes`.
  5. Update the W8-3 panel component to render the benchmark column
     group + VWAP comparison.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/execution_quality.py`
  - `mini-services/polymarket-bot/core/observability_collector.py`
  - `mini-services/polymarket-bot/migrations/0XX_execution_quality_benchmarks.sql`
    (new)
  - `mini-services/polymarket-bot/tests/test_execution_quality.py`
    (expand from 13 → ~22 tests)
  - `src/components/ExecutionQualityPanel.tsx`
- **Dependencies:** Data Platform improvement `DP-3` (data quality
  monitoring) — benchmark task depends on `orderbook_ticks` being
  complete.
- **Risk:** MEDIUM — schema migration is backward-compatible (NULL
  defaults). Mitigation: idempotent migration; benchmark task fails
  gracefully if `orderbook_ticks` is missing.
- **Priority:** P1 (core architecture — required for smart-router
  tuning).
- **Expected Benefit:**
  - Smart router can be tuned against post-fill adverse-selection
    data, not just instantaneous slippage.
  - Maker-vs-taker attribution enables the MM rebate model.
  - VWAP-vs-realized-edge regression catches strategy decay.
- **Tests:**
  - +9 tests: benchmark collector happy path, missing `orderbook_ticks`,
    benchmark already populated, migration idempotency, maker vs taker
    separation, VWAP computation, endpoint schema, NULL-benchmark
    fallback, 24-hour-benchmark fill.
- **Metrics:**
  - `execution_quality_benchmark_fill_lag_seconds` histogram.
  - `execution_quality_maker_share_pct` gauge.
  - `execution_quality_adverse_selection_bps` gauge (avg of
    `benchmark_price_1m - actual_fill`).
- **Acceptance Criteria:**
  - All 22 execution-quality tests pass.
  - After 24 h paper session, >= 95 % of fills have non-NULL
    `benchmark_price_1m`.
  - ExecutionQualityPanel renders the new column group.
- **Status:** IN PROGRESS.

---

## Improvement BE-3 — Smart Order Routing (TWAP / VWAP / Iceberg)

- **Problem:** `execution/advanced_router.py` (W16-9) ships TWAP,
  VWAP, and iceberg splitters but they are not integrated into the
  live order-submission path. `signal_trader.py` and `market_maker.py`
  still submit single-shot orders via the basic
  `execution/smart_router.py`. The advanced router is "code complete
  but not wired" — operators cannot use it from the live UI.
- **Evidence:**
  - `execution/advanced_router.py` exists with TWAP/VWAP/iceberg
    classes; no caller in `signal_trader.py` / `market_maker.py`
    references them (verified via Grep).
  - `tests/test_advanced_router.py` (W16-9, 12 tests) exercises the
    classes in isolation but no integration test wires them through
    the order state machine.
  - `METRICS_SUMMARY.md` lists "Smart order routing" as a Trading
    feature but the LiveSafetyGate's `smart_router_integrated` check
    (§7.7 of ARCHITECTURE) reports 0/1 passing.
- **Current State:** `SmartRouter` (basic) is wired into
  `strategies/base.py` and used by every strategy.
  `AdvancedRouter` (TWAP/VWAP/iceberg) is constructed by tests but
  not by production code. Order state machine does not model
  parent/child orders (a TWAP parent order spawns N child orders).
- **Desired State:**
  1. OSM supports parent/child order relationships (a parent carries
     a list of `child_order_ids`).
  2. `AdvancedRouter` chooses TWAP / VWAP / iceberg based on order
     size relative to `book_depth` and `avg_minute_volume` (config
     thresholds).
  3. Each child order flows through `SmartRouter` (basic) for the
     actual placement.
  4. `signal_trader.py` accepts a `routing_policy` parameter:
     `SINGLE | TWAP | VWAP | ICEBERG | AUTO` (default `AUTO`).
  5. Live safety gate's `smart_router_integrated` check flips to
     passing.
- **Proposed Solution:**
  1. Extend `Order` dataclass with `parent_order_id: Optional[str]`
     + `child_order_ids: list[str]`.
  2. Add `Order.is_parent()` / `Order.is_child()` helpers.
  3. Refactor `AdvancedRouter` to accept an `OrderStore` (so it can
     persist child orders) and a `DecisionLedger` (so it can emit
     ORDER_TRANSITION events per child).
  4. Add `RoutingPolicy` enum to `strategies/base.py`.
  5. `signal_trader.py` chooses `AUTO` — delegates to
     `AdvancedRouter.select_policy(order, book, volume)` which
     returns the chosen policy.
  6. New endpoint `GET /api/execution/routing-decisions?limit=N`
     showing the routing-policy history (token, size, chosen policy,
     rationale).
- **Architecture:**
  ```
  signal_trader.emit_signal(order_intent)
    └─→ AdvancedRouter.select_policy(intent, book, volume) → RoutingPolicy.TWAP
         └─→ AdvancedRouter.split(intent, policy) → [child1, child2, ...]
              └─→ for each child:
                   child.submit() → SmartRouter.place(child)
                   Order.parent_order_id = intent.id
                   child_order_ids.append(child.id)
              └─→ parent transitions CREATED → SUBMITTED (children)
                   parent.fill(qty) when all children filled → FILLED
  ```
- **Implementation:**
  1. OSM parent/child extension (BE-1 first).
  2. `AdvancedRouter` refactor (signature change to take
     `OrderStore` + `DecisionLedger`).
  3. `RoutingPolicy` enum + `select_policy()` heuristic.
  4. `signal_trader.py` integration with `routing_policy` config.
  5. New `/api/execution/routing-decisions` endpoint.
  6. Integration tests in `tests/test_signal_trader.py` covering each
     policy.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/order_state_machine.py` (BE-1
    parent/child).
  - `mini-services/polymarket-bot/execution/advanced_router.py`
    (refactor + select_policy).
  - `mini-services/polymarket-bot/execution/smart_router.py`
    (unchanged — basic router still places).
  - `mini-services/polymarket-bot/strategies/base.py` (RoutingPolicy).
  - `mini-services/polymarket-bot/strategies/signal_trader.py`
    (routing_policy integration).
  - `mini-services/polymarket-bot/core/execution_quality.py`
    (expose routing_policy column).
  - `mini-services/polymarket-bot/api/server.py` (new endpoint).
  - `mini-services/polymarket-bot/tests/test_advanced_router.py`
    (expand from 12 → ~25 tests).
  - `mini-services/polymarket-bot/tests/test_signal_trader.py`
    (add policy-parametrised cases).
- **Dependencies:** BE-1 (OSM parent/child), BE-2 (execution quality
  routing_policy column).
- **Risk:** HIGH — touches the live order path. Mitigation: feature
  flag `ADVANCED_ROUTER_ENABLED` (default off in paper); promotion
  via shadow inference (BE's own promotion gate from `AI_ML_ENGINE`
  plan applies).
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Operators can choose routing policy per strategy.
  - TWAP reduces market impact on illiquid tokens.
  - VWAP benchmarks enable fair performance attribution.
  - Live safety gate's `smart_router_integrated` check flips to
    passing.
- **Tests:**
  - +13 tests: parent/child wiring, AUTO selection heuristic, TWAP
    timing, VWAP volume slicing, iceberg quantity hiding, child-fill
    propagation to parent, partial-child-failure rollback,
    routing-decisions endpoint schema, policy persistence, live
    safety gate integration.
- **Metrics:**
  - `advanced_router_policy_selected_total{policy}` counter.
  - `advanced_router_child_orders_per_parent` histogram.
  - `advanced_router_parent_rollback_total` counter.
- **Acceptance Criteria:**
  - All 25 advanced-router tests pass.
  - Live safety gate `smart_router_integrated` check passes.
  - RoutingDecisions endpoint returns the last 50 decisions in
    < 200 ms p95.
- **Status:** IN PROGRESS.

---

## Improvement BE-4 — Circuit Breaker Enhancements

- **Problem:** `core/circuit_breaker.py` ships two breakers: a
  per-trade breaker (`PER_TRADE_MAX_LOSS=$0.50`, cooldown=300 s) and
  an external-API breaker (Polymarket/Gamma/CLOB). However, three
  failure modes are not covered: (a) cross-strategy correlation
  (if 3 strategies all hit their per-trade breaker in the same
  minute, the platform should pause all strategies, not just the
  3 that tripped); (b) latency-spike breaker (sustained > 2 s
  order-placement latency → pause); (c) reconnect-storm breaker
  (more than 5 WS reconnects in 60 s → pause).
- **Evidence:**
  - `tests/test_circuit_breaker.py` exists with 9 tests covering
    the per-trade + external-API breakers only.
  - `core/observability_collector.py` already records
    `gateway_latency_ms` and `ws_reconnects_total` but the breaker
    does not subscribe to these.
  - `FINAL_SYSTEM_REASSESSMENT.md` §6 (residual risks) lists
    "correlated strategy failures" as a known live-trading risk.
- **Current State:** Two breakers, two trigger types
  (`per_trade_loss`, `external_api_error`). Cooldowns fixed at 300 s
  per-trade / 60 s external-API. No correlation-aware logic. No
  latency trigger. No reconnect-storm trigger.
- **Desired State:** Five breakers — the existing two plus
  `cross_strategy_correlation`, `gateway_latency`, `ws_reconnect_storm`.
  Cross-strategy correlation breaker trips when >= 3 strategies trip
  their per-trade breaker in a 5-min window; pauses ALL strategies
  for 600 s. Latency breaker trips when gateway_latency_ms p95 > 2 s
  for 60 s; pauses for 120 s. Reconnect-storm breaker trips on > 5
  reconnects in 60 s; pauses for 60 s.
- **Proposed Solution:**
  1. Refactor `CircuitBreaker` to be a registry of `BreakerRule`
     instances (currently both rules are hardcoded in one class).
  2. Add `PerTradeLossRule`, `ExternalAPIErrorRule`,
     `CrossStrategyCorrelationRule`, `GatewayLatencyRule`,
     `WSReconnectStormRule` classes.
  3. Each rule subscribes to the metrics it cares about via
     `observability_collector.add_subscriber(rule)`.
  4. `breaker_registry.evaluate()` runs every 10 s — any rule that
     trips calls `breaker_registry.pause_all_strategies(reason,
     cooldown_seconds)`.
  5. New endpoint `GET /api/risk/breakers` returning the 5 rules +
     their current state (open/closed), last-tripped timestamp,
     cooldown-remaining.
- **Architecture:**
  ```
  observability_collector
    └─→ every 10s → breaker_registry.evaluate()
         ├─→ PerTradeLossRule           : trip if loss > $0.50 in 300s
         ├─→ ExternalAPIErrorRule       : trip if > 5 errors in 60s
         ├─→ CrossStrategyCorrelationRule: trip if >= 3 strategies tripped per-trade in 5min
         ├─→ GatewayLatencyRule         : trip if p95 latency > 2000ms for 60s
         └─→ WSReconnectStormRule       : trip if > 5 reconnects in 60s
              └─→ if any rule tripped → pause_all_strategies(reason, cooldown)
                   └─→ strategies/base.py StrategyContext.is_paused returns True
                        └─→ every strategy's run() exits early
  ```
- **Implementation:**
  1. Refactor `CircuitBreaker` → `BreakerRegistry` + 5 `BreakerRule`
     subclasses.
  2. Add `observability_collector.add_subscriber()` API.
  3. Wire the registry into `api/server.py` lifespan startup.
  4. Add `/api/risk/breakers` route.
  5. Add UI panel `BreakersPanel.tsx` (5 cards, one per rule).
  6. Tests covering each rule + the registry's pause-all path.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/circuit_breaker.py`
    (full refactor)
  - `mini-services/polymarket-bot/core/observability_collector.py`
    (add_subscriber)
  - `mini-services/polymarket-bot/strategies/base.py` (StrategyContext
    .is_paused)
  - `mini-services/polymarket-bot/risk/routes.py` (new breaker
    endpoint)
  - `mini-services/polymarket-bot/tests/test_circuit_breaker.py`
    (expand from 9 → ~28 tests)
  - `src/components/BreakersPanel.tsx` (new — UI)
  - `src/components/Sidebar.tsx` (add to Risk group)
- **Dependencies:** None (the breaker is foundational).
- **Risk:** MEDIUM — refactor of a capital-protection module. Every
  existing test must pass unchanged before merge.
- **Priority:** P0 (capital protection).
- **Expected Benefit:**
  - Catches correlated strategy failures (the dominant live-trading
    disaster mode).
  - Catches infrastructure degradation before it produces wrong
    fills.
  - Operator-visible breaker state replaces the current "strategy X
    is paused" reverse-engineering.
- **Tests:**
  - +19 tests: per-rule happy path, per-rule cooldown expiry,
    pause-all path, subscriber-notification path, endpoint schema,
    UI panel rendering.
- **Metrics:**
  - `circuit_breaker_state{rule}` gauge (0=closed, 1=open).
  - `circuit_breaker_trips_total{rule}` counter.
  - `circuit_breaker_pause_all_total` counter.
- **Acceptance Criteria:**
  - All 28 breaker tests pass.
  - The 5 breaker cards render in the new `BreakersPanel.tsx`.
  - When 3 strategies trip their per-trade breaker in a 5-min
    window, `pause_all_strategies` fires within 15 s.
- **Status:** IN PROGRESS.

---

## Improvement BE-5 — Idempotency Improvements

- **Problem:** Order submission, settlement, and ledger writes are
  not idempotent at the production boundary. Re-submitting the same
  signal (e.g. due to a retry-after-timeout) creates a duplicate
  order. Settling the same market twice (e.g. due to a webhook
  replay) credits the PnL twice. `decision_ledger.record_*` methods
  use `decision_id` as the primary key, so duplicate calls fail
  with `IntegrityError`, but the caller doesn't retry — the order
  is left in limbo.
- **Evidence:**
  - `tests/test_decision_ledger.py` (S9, Wave 2) — covers
    `IntegrityError` on duplicate decision_id but not the
    catch-and-recover path.
  - `tests/test_failure_injection.py` (S11, Wave 2) — covers
    `duplicate_signal` but only at the strategy layer, not the
    ledger layer.
  - `FINAL_SYSTEM_REASSESSMENT.md` §4 lists "idempotency keys are
    partial" as a residual risk.
- **Current State:** `decision_id` is a `uuid4`-generated primary
  key on every `decision_events` row. No client-supplied
  idempotency key. Order placement, settlement, and ledger writes
  are not retry-safe.
- **Desired State:**
  1. Every external-facing mutating endpoint
     (`POST /api/orders`, `POST /api/positions/{id}/close`,
     `POST /api/system/prune`, `POST /api/live/enable`,
     `POST /api/ml/rollback`) accepts an `Idempotency-Key` HTTP
     header.
  2. The header value is hashed (SHA-256) + stored in an
     `idempotency_keys` table with `(key_hash, response_body,
     response_status, expires_at)`.
  3. Subsequent requests with the same key hash within 24 h return
     the cached response.
  4. `decision_ledger.record_*` methods use the idempotency key as
     a secondary unique index — duplicate calls are no-ops (return
     the existing decision_id).
  5. `settlement.settle_market()` is idempotent (settling an
     already-settled market is a no-op, not an error).
- **Proposed Solution:**
  1. New table `idempotency_keys(key_hash PRIMARY KEY, response_body,
     response_status, created_at, expires_at)`.
  2. FastAPI middleware `IdempotencyMiddleware` that intercepts every
     mutating method on whitelisted routes, checks the key, returns
     cached response or proceeds.
  3. `decision_ledger.record_decision()` accepts an optional
     `idempotency_key`; uses it as a secondary unique index.
  4. `settlement.settle_market()` short-circuits if the market is
     already in `closed_positions`.
  5. Tests covering the middleware, the ledger deduplication, the
    settlement idempotency.
- **Architecture:**
  ```
  POST /api/orders  with header Idempotency-Key: <client-uuid>
    └─→ IdempotencyMiddleware
         ├─ hash(key) → lookup in idempotency_keys
         ├─ if found and not expired → return cached response
         └─ else → call endpoint, capture response, store in idempotency_keys,
                   return response
  DecisionLedger.record_decision(decision_id, ..., idempotency_key)
    └─→ INSERT ... ON CONFLICT(idempotency_key) DO NOTHING RETURNING id
         └─→ if RETURNING empty → SELECT id WHERE idempotency_key=...
  ```
- **Implementation:**
  1. Migration `migrations/0XX_idempotency.sql` (new table + index
     on `decision_events.idempotency_key`).
  2. `core/idempotency.py` (NEW) — middleware + helper.
  3. Wire middleware into `api/server.py` startup.
  4. Extend `DecisionLedger.record_decision` signature.
  5. Extend `settlement.settle_market` with an idempotency check.
  6. Tests across all three layers.
- **Files Affected:**
  - `mini-services/polymarket-bot/migrations/0XX_idempotency.sql` (new)
  - `mini-services/polymarket-bot/core/idempotency.py` (new)
  - `mini-services/polymarket-bot/core/decision_ledger.py` (extend)
  - `mini-services/polymarket-bot/core/settlement.py` (extend)
  - `mini-services/polymarket-bot/api/server.py` (wire middleware)
  - `mini-services/polymarket-bot/tests/test_idempotency.py` (new)
  - `mini-services/polymarket-bot/tests/test_decision_ledger.py`
    (expand)
  - `mini-services/polymarket-bot/tests/test_settlement.py`
    (expand)
- **Dependencies:** BE-1 (OSM uses the same idempotency key for
  transition dedup).
- **Risk:** HIGH — middleware touches every mutating endpoint.
  Mitigation: feature-flagged via `IDEMPOTENCY_MIDDLEWARE_ENABLED`
  (default off in paper); gradual rollout.
- **Priority:** P0 (capital risk — duplicate orders / double PnL =
  real loss).
- **Expected Benefit:**
  - Webhook replays are safe.
  - Client retries are safe.
  - Decision ledger is reconstructable from the idempotency keys
    alone (foundation for auditability).
- **Tests:**
  - +20 tests across middleware, ledger dedup, settlement
    idempotency, expiry, key-hash collision, concurrent same-key
    requests.
- **Metrics:**
  - `idempotency_cache_hits_total` counter.
  - `idempotency_cache_misses_total` counter.
  - `idempotency_ledger_dedup_total` counter.
- **Acceptance Criteria:**
  - All idempotency tests pass.
  - Replay of any webhook within 24 h is a no-op.
  - `POST /api/orders` with the same `Idempotency-Key` returns the
    same response (status + body) within 5 ms.
- **Status:** TODO (not started — scheduled for the W18 wave).

---

## Cross-cutting notes

- Every improvement in this plan contributes to the **Phase 1
  (Safety scaffolding)** and **Phase 2 (Execution quality)** exit
  criteria in `MASTER_IMPROVEMENT_ROADMAP.md`.
- The P0 items (BE-1, BE-4, BE-5) are blocking: live trading cannot
  be enabled until all three are DONE.
- The P1 items (BE-2, BE-3) are blocking the smart-router tuning
  loop and the backtest/live parity harness (see `BACKTEST_ENGINE`
  plan).
- Implementation order recommended: BE-5 (idempotency) → BE-1 (OSM)
  → BE-4 (breakers) → BE-2 (exec quality) → BE-3 (smart router).
  This is the dependency-respecting order — idempotency is the
  foundation for the OSM's transition dedup, which is the
  foundation for the breaker's pause-all key, which is the
  foundation for the smart router's child-order rollback.
