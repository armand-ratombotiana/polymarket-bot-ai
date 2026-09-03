# Strategy Management — Improvement Plan

- **Domain:** Strategy management (unified contract, lifecycle,
  attribution, metrics dashboard)
- **Owning modules:** `strategies/base.py`, `strategies/registry.py`,
  `strategies/signal_trader.py`, `strategies/market_maker.py`,
  `strategies/arb_scanner.py`, `core/attribution.py`,
  `core/closed_positions.py`, `core/capital_allocator.py`,
  `risk/manager.py`, `risk/routes.py`
- **Source authority:** God Mode §26 (unified strategy contract),
  §27 (lifecycle), §28 (attribution), §29 (metrics dashboard).
- **Priority classification (per God Mode §64):**
  - P1 — unified strategy contract (foundation); lifecycle
    management.
  - P2 — attribution; metrics dashboard.
- **Status as of W17-9:** IN PROGRESS — see per-improvement status
  below.

This plan defines every improvement in the strategy management
domain using the per-improvement field set required by God Mode §63.
Each improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement ST-1 — Unified Strategy Contract (§26)

- **Problem:** `strategies/base.py::StrategyBase` defines an ABC
  with `evaluate()` and `run()` methods, but the contract is loose:
  (a) there is no `StrategyContext` value type — every strategy
  reads globals (`config`, `data_store`, `risk_manager`) directly;
  (b) the return type of `evaluate()` is `Optional[Signal]` where
  `Signal` is a partial dict — no declared fields for `intent`,
  `size`, `routing_policy`, `time_in_force`; (c) strategies are
  instantiated in `api/server.py` startup with ad-hoc arguments;
  there is no `StrategyFactory` or `StrategySpec`.
- **Evidence:**
  - `strategies/signal_trader.py`, `strategies/market_maker.py`,
    `strategies/arb_scanner.py` — each reads `config.X_ENABLED`,
    `config.X_MIN_CONFIDENCE`, etc. directly.
  - `strategies/registry.py` lists 50 strategies but only 3 are
    implemented (`market_maker`, `arb_scanner`,
    `signal_trader`); the 47 stubs render as "Running" in the UI
    (per `FINAL_SYSTEM_REASSESSMENT.md` §1.1).
  - `tests/test_strategy_base.py` (X11, Wave 7) — 5 tests cover
    risk gate + lifecycle but not the contract surface.
- **Current State:** Loose ABC; strategies read globals; 47 stubs
  shown as "Running" with no `implemented` flag distinction.
- **Desired State:**
  1. `StrategyContext` dataclass: holds `config`, `data_store`,
     `risk_manager`, `capital_allocator`, `decision_ledger`,
     `execution_quality`, `ws_broadcast`, `ml_model` — injected
     once at construction.
  2. `StrategySpec` dataclass: declares `name`, `version`,
     `enabled`, `params` (dict), `routing_policy` (default
     `SINGLE`), `risk_profile` (max_position, max_loss,
     cooldown_seconds).
  3. `OrderIntent` value type returned by `evaluate()` — replaces
     the partial `Signal` dict.
  4. `StrategyFactory.create(spec, context)` — instantiates the
     right strategy class from the spec.
  5. `StrategyRegistry` (replaces `strategies/registry.py`) —
     every registered strategy has an `implemented: bool` flag
     (TRUE only for the 3 implemented ones); stubs are visibly
     marked as "Not implemented".
  6. The `GET /api/strategies` endpoint returns `implemented`
     per row.
- **Proposed Solution:**
  1. New `StrategyContext`, `StrategySpec`, `OrderIntent` types
     in `strategies/types.py` (new file).
  2. Refactor `StrategyBase` to take `(spec, context)` in
     `__init__`.
  3. `evaluate()` returns `Optional[OrderIntent]`.
  4. `StrategyFactory` + `StrategyRegistry`.
  5. Refactor the 3 implemented strategies to use the new
     contract.
  6. The 47 stubs are registered with `implemented=False` and
     return `None` from `evaluate()`.
- **Architecture:**
  ```
  StrategyContext(spec) ←── injected ──── StrategyFactory.create(spec, ctx)
   ├─ config
   ├─ data_store                    ┌── StrategyBase (ABC)
   ├─ risk_manager                   │    __init__(spec, ctx)
   ├─ capital_allocator              │    evaluate(ctx) → OrderIntent | None
   ├─ decision_ledger                │    on_fill(fill)
   ├─ execution_quality              │    on_reject(reason)
   ├─ ws_broadcast                   │    lifecycle: start() / stop() / pause()
   ├─ ml_model                       │
   └─ alerting                       └── SignalTrader / MarketMaker / ArbScanner
                                            (extend StrategyBase)
  StrategyRegistry
    └─→ register(StrategySpec(implemented=True, ...))
    └─→ list_all() → [StrategySpec]
  ```
- **Implementation:**
  1. New file `strategies/types.py` with the 4 dataclasses.
  2. Refactor `strategies/base.py`.
  3. `StrategyFactory` + `StrategyRegistry` in
     `strategies/registry.py`.
  4. Refactor the 3 implemented strategies.
  5. Update `api/server.py` startup to use the factory.
  6. Update `GET /api/strategies` to return `implemented`.
- **Files Affected:**
  - `mini-services/polymarket-bot/strategies/types.py` (new)
  - `mini-services/polymarket-bot/strategies/base.py` (refactor)
  - `mini-services/polymarket-bot/strategies/registry.py` (rewrite)
  - `mini-services/polymarket-bot/strategies/signal_trader.py`
    (refactor)
  - `mini-services/polymarket-bot/strategies/market_maker.py`
    (refactor)
  - `mini-services/polymarket-bot/strategies/arb_scanner.py`
    (refactor)
  - `mini-services/polymarket-bot/api/server.py` (startup)
  - `mini-services/polymarket-bot/tests/test_strategy_base.py`
    (expand from 5 → ~18 tests)
  - `mini-services/polymarket-bot/tests/test_signal_trader.py`
  - `mini-services/polymarket-bot/tests/test_market_maker.py`
  - `mini-services/polymarket-bot/tests/test_arb_scanner.py`
- **Dependencies:** None — this is the foundation.
- **Risk:** HIGH — every strategy must be refactored. Mitigation:
  backward-compatible `StrategyBase.__init__(spec=None,
  context=None)` so the old call sites keep working during the
  migration.
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Every strategy reads its config through the same `StrategyContext`
    — no more global-state leaks.
  - The 47 stubs are visibly "Not implemented" — no more
    "Running" lies.
  - Foundation for strategy lifecycle management (ST-2).
- **Tests:** +13 tests covering context injection, factory
  dispatch, registry listing, stub-vs-implemented distinction,
  OrderIntent validation.
- **Metrics:**
  - `strategies_active_total` gauge.
  - `strategies_implemented_total` gauge.
  - `strategies_evaluate_ms{strategy}` histogram.
- **Acceptance Criteria:**
  - All 18 strategy-base tests pass.
  - `GET /api/strategies` returns 50 rows with `implemented=True`
    for 3 and `implemented=False` for 47.
  - No strategy reads a global directly (Grep assertion in tests).
- **Status:** IN PROGRESS.

---

## Improvement ST-2 — Strategy Lifecycle Management (§27)

- **Problem:** Strategies are started at backend startup and run
  forever. There is no `pause()`, `resume()`, `restart()` API.
  The operator cannot disable a strategy at runtime without
  restarting the backend. The `core/circuit_breaker.py` per-trade
  breaker pauses a strategy for 300 s, but the pause is implicit
  — the operator cannot see "this strategy is paused for 4 more
  minutes" in the UI.
- **Evidence:**
  - `strategies/base.py` — no `pause`/`resume` methods.
  - `risk/routes.py` exposes `GET /api/risk/strategies/paused`
    (W14-2) but it reads from the circuit breaker's internal
    state, not a declared lifecycle state.
  - `src/components/StrategyMatrix.tsx` (W14-2) shows "Running"
    or "Paused" but the state is not actionable (no resume
    button).
- **Current State:** Strategies start at startup; circuit breaker
  can pause them implicitly for 300 s; no explicit lifecycle API.
- **Desired State:**
  1. `StrategyLifecycleState` enum: `STARTING | RUNNING | PAUSED
     | STOPPING | STOPPED | ERROR`.
  2. `StrategyBase.start()`, `pause(reason)`, `resume()`,
     `stop()`, `restart()` methods.
  3. `pause(reason)` records the reason + expected-resume time;
     the strategy's `run()` exits early while paused.
  4. New endpoints: `POST /api/strategies/{id}/pause`,
     `POST /api/strategies/{id}/resume`,
     `POST /api/strategies/{id}/restart`,
     `GET /api/strategies/{id}/state`.
  5. `StrategyMatrix.tsx` shows the lifecycle state + a
    pause/resume/restart button per strategy.
- **Proposed Solution:**
  1. `StrategyLifecycleState` enum in `strategies/types.py`.
  2. `StrategyBase` gains a `state` field + the 4 methods.
  3. `StrategyContext` gains `is_paused()` for use inside
    `evaluate()`.
  4. The per-trade circuit breaker's `pause()` calls
     `strategy.pause(reason="per_trade_loss", duration=300)`.
  5. New endpoints + UI buttons.
- **Architecture:**
  ```
  StrategyBase
    └─→ state: StrategyLifecycleState = STOPPED
    └─→ start() → state=STARTING → ... → state=RUNNING
    └─→ pause(reason, duration=300) → state=PAUSED
         └─→ resume_at = now + duration
         └─→ run() exits early until resume_at
    └─→ resume() → state=RUNNING
    └─→ stop() → state=STOPPING → ... → state=STOPPED
    └─→ restart() → stop() + start()

  POST /api/strategies/signal_trader/pause
    └─→ strategy.pause("operator_request", 600)
         └─→ 200 OK + state=PAUSED + resume_at
  StrategyMatrix.tsx
    └─→ per-strategy row: state pill + Pause/Resume/Restart buttons
  ```
- **Implementation:**
  1. Extend `strategies/types.py` + `strategies/base.py`.
  2. Refactor `run()` loops in the 3 implemented strategies to
     check `state`.
  3. Wire the circuit breaker's pause into the new API.
  4. New endpoints in `risk/routes.py` (or a new
     `strategies/routes.py`).
  5. UI buttons in `StrategyMatrix.tsx`.
- **Files Affected:**
  - `mini-services/polymarket-bot/strategies/types.py` (extend)
  - `mini-services/polymarket-bot/strategies/base.py` (extend)
  - `mini-services/polymarket-bot/strategies/signal_trader.py`
  - `mini-services/polymarket-bot/strategies/market_maker.py`
  - `mini-services/polymarket-bot/strategies/arb_scanner.py`
  - `mini-services/polymarket-bot/core/circuit_breaker.py` (wire
    into the new pause API)
  - `mini-services/polymarket-bot/api/server.py` (new endpoints)
  - `src/components/StrategyMatrix.tsx` (state pill + buttons)
  - `mini-services/polymarket-bot/tests/test_strategy_base.py`
    (expand for lifecycle)
  - `mini-services/polymarket-bot/tests/test_strategy_lifecycle.py`
    (new)
- **Dependencies:** ST-1 (unified contract — the lifecycle is
  defined on `StrategyBase`).
- **Risk:** MEDIUM — the run-loop refactor must not break the
  paper-trading session. Mitigation: feature flag
  `STRATEGY_LIFECYCLE_API_ENABLED`; the implicit circuit-breaker
  pause keeps working regardless.
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Operators can disable a misbehaving strategy without
    restarting the backend.
  - The circuit breaker's pause is now visible + actionable.
  - The `StrategyMatrix` becomes a real strategy-management
    surface, not just a list.
- **Tests:** +14 tests covering each state transition, the
  pause-with-duration path, the run-loop early-exit, endpoint
  schema, UI rendering.
- **Metrics:**
  - `strategy_state{strategy}` gauge.
  - `strategy_pause_total{reason}` counter.
  - `strategy_resume_total` counter.
  - `strategy_restart_total` counter.
- **Acceptance Criteria:**
  - All 14 lifecycle tests pass.
  - POSTing `/pause` to a running strategy flips its state to
    PAUSED within 2 s.
  - The strategy matrix renders the state pill + buttons.
- **Status:** IN PROGRESS.

---

## Improvement ST-3 — Strategy Attribution (§28)

- **Problem:** `core/attribution.py` (S15, Wave 2) computes 7-bucket
  P&L attribution (strategy, direction, confidence_bucket,
  time_of_day, holding_period, market_category, model_version)
  per closed position. However, (a) the buckets are hardcoded —
  no way to add a new dimension without code change; (b) no
  per-strategy contribution-to-total-P&L view (the attribution
  is per-position, not aggregated per strategy); (c) the
  attribution is computed on demand, not on every close —
  operators querying `/api/attribution?range=24h` re-compute
  every time.
- **Evidence:**
  - `tests/test_attribution.py` (U1, Wave 4) — 7 tests covering
    the 7 buckets.
  - `src/components/AttributionPanel.tsx` (W8-10) — renders the
    per-position attribution; no per-strategy view.
  - `docs/ARCHITECTURE.md` §9 documents the 7 buckets.
- **Current State:** 7 hardcoded buckets; on-demand computation;
  per-position only.
- **Desired State:**
  1. Pluggable buckets: an `AttributionBucket` ABC; the 7 existing
     ones are subclasses; new buckets (e.g. `routing_policy`,
     `liquidity_type`, `slippage_bucket`) register at startup.
  2. Per-strategy aggregate attribution: a new table
     `strategy_attribution_daily` keyed by `(date, strategy,
     bucket_name, bucket_value)` with `pnl`, `trade_count`,
     `win_rate`, `expectancy` columns. Computed at close time +
     on a daily cron.
  3. New endpoint `GET /api/strategies/{id}/attribution?range=`
     returning the per-strategy aggregate.
  4. UI: a `StrategyAttributionPanel.tsx` per strategy showing
     the bucket breakdown.
- **Proposed Solution:**
  1. `AttributionBucket` ABC in `core/attribution.py`.
  2. Refactor the 7 existing buckets to subclasses.
  3. New `strategy_attribution_daily` table.
  4. `AttributionAggregator` class — runs on every close +
    daily cron.
  5. Endpoint + UI panel.
- **Architecture:**
  ```
  AttributionBucket (ABC)
    └─→ key: str
    └─→ extract(position) → str (bucket value)
  ├── StrategyBucket
  ├── DirectionBucket
  ├── ConfidenceBucket
  ├── TimeOfDayBucket
  ├── HoldingPeriodBucket
  ├── MarketCategoryBucket
  ├── ModelVersionBucket
  ├── RoutingPolicyBucket (new)
  ├── LiquidityTypeBucket (new)
  └── SlippageBucket (new)
  AttributionAggregator.aggregate(closed_position)
    └─→ for each registered bucket:
         key = bucket.key()
         value = bucket.extract(position)
         upsert strategy_attribution_daily (date, strategy, key, value, pnl, ...)
  GET /api/strategies/signal_trader/attribution?range=30d
    └─→ { buckets: [{name, values: [{value, pnl, trades, win_rate}]}] }
  ```
- **Implementation:**
  1. `AttributionBucket` ABC + refactor.
  2. New table + `AttributionAggregator` class.
  3. Wire aggregator into `closed_positions.record_close()`.
  4. Endpoint + UI panel.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/attribution.py` (rewrite)
  - `mini-services/polymarket-bot/core/closed_positions.py`
    (extend)
  - `mini-services/polymarket-bot/migrations/0XX_strategy_attribution_daily.sql`
    (new)
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `src/components/StrategyAttributionPanel.tsx` (new)
  - `src/components/StrategyMatrix.tsx` (link to per-strategy
    attribution)
  - `mini-services/polymarket-bot/tests/test_attribution.py`
    (expand from 7 → ~16 tests)
- **Dependencies:** ST-1 (unified contract — the strategy name
  comes from `StrategySpec`).
- **Risk:** LOW — additive; existing 7-bucket behaviour preserved.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Pluggable buckets — operators can add new dimensions without
    code changes.
  - Per-strategy view surfaces "which strategy is making money
    where".
  - Pre-computed aggregate makes the UI sub-100 ms instead of
    re-computing on every query.
- **Tests:** +9 tests covering pluggable buckets, aggregator,
  per-strategy endpoint, UI panel.
- **Metrics:**
  - `attribution_buckets_registered` gauge.
  - `attribution_aggregations_total` counter.
  - `attribution_aggregation_ms` histogram.
- **Acceptance Criteria:**
  - All 16 attribution tests pass.
  - The per-strategy attribution endpoint returns in < 100 ms p95
    for a 30-day range.
  - The new `StrategyAttributionPanel` renders the bucket
    breakdown.
- **Status:** IN PROGRESS.

---

## Improvement ST-4 — Strategy Metrics Dashboard (§29)

- **Problem:** `src/components/StrategyMatrix.tsx` (W14-2) shows
  per-strategy P&L + win rate + trade count + profit_factor +
  max_drawdown + net_pnl. But (a) it's missing Sharpe, Sortino,
  Calmar; (b) it's not per-time-range (always lifetime); (c) it
  doesn't surface the strategy's lifecycle state (ST-2) or
  attribution breakdown (ST-3).
- **Evidence:**
  - `src/components/StrategyMatrix.tsx` — renders 6 KPIs per
    strategy.
  - `src/components/LeaderboardPanel.tsx` — renders the same 6
    KPIs in a leaderboard layout.
  - No per-time-range filter in either panel.
- **Current State:** 6 KPIs, lifetime only.
- **Desired State:**
  1. Add Sharpe, Sortino, Calmar to the KPI set (9 total).
  2. Time-range filter: 1 d / 7 d / 30 d / 90 d / lifetime.
  3. Lifecycle state pill (from ST-2).
  4. Attribution breakdown expandable section (from ST-3).
  5. Per-strategy detail view: click a strategy row → opens a
     detail panel with the equity curve + the 9 KPIs + the
     attribution breakdown.
- **Proposed Solution:**
  1. Backend: extend `/api/leaderboard` to accept a `range`
     parameter; compute Sharpe/Sortino/Calmar server-side.
  2. Backend: new endpoint
     `/api/strategies/{id}/detail?range=` returning equity curve
     + KPIs + attribution.
  3. Frontend: time-range Select in `StrategyMatrix.tsx`.
  4. Frontend: expandable detail row.
  5. Frontend: new `StrategyDetailPanel.tsx` for the dedicated
     detail view.
- **Architecture:**
  ```
  GET /api/leaderboard?range=7d
    └─→ for each strategy:
         pnl, win_rate, trade_count, profit_factor, max_drawdown,
         net_pnl, sharpe, sortino, calmar
  GET /api/strategies/signal_trader/detail?range=30d
    └─→ {
         kpis: { ...9 metrics },
         equity_curve: [...],
         attribution: { buckets: [...] },
         lifecycle: "RUNNING",
       }
  StrategyMatrix.tsx
    └─→ time-range Select + per-strategy row (9 KPIs + state pill)
         └─→ click → opens StrategyDetailPanel
  ```
- **Implementation:**
  1. Backend: extend `risk/routes.py` + new
     `strategies/routes.py`.
  2. Backend: Sharpe/Sortino/Calmar computation helper (new
     `core/performance.py`).
  3. Frontend: time-range Select.
  4. Frontend: `StrategyDetailPanel.tsx`.
  5. Wire into `StrategyMatrix.tsx`.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/performance.py` (new)
  - `mini-services/polymarket-bot/risk/routes.py` (extend)
  - `mini-services/polymarket-bot/strategies/routes.py` (new)
  - `mini-services/polymarket-bot/api/server.py` (register)
  - `src/components/StrategyMatrix.tsx` (extend)
  - `src/components/StrategyDetailPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (entry for detail panel)
  - `mini-services/polymarket-bot/tests/test_performance.py` (new)
- **Dependencies:** ST-2 (lifecycle state), ST-3 (attribution
  breakdown).
- **Risk:** LOW — additive; existing 6 KPIs preserved.
- **Priority:** P2 (analytics).
- **Expected Benefit:**
  - Operators see risk-adjusted returns (Sharpe/Sortino/Calmar)
    alongside raw P&L.
  - Time-range filter surfaces recent vs lifetime performance.
  - Per-strategy detail view becomes the operator's "strategy
    health" page.
- **Tests:** +12 tests covering Sharpe/Sortino/Calmar computation,
  time-range filter, detail endpoint schema, UI rendering.
- **Metrics:**
  - `strategy_metrics_compute_ms{metric}` histogram.
  - `strategy_dashboard_render_ms` histogram.
- **Acceptance Criteria:**
  - All 12 performance tests pass.
  - `StrategyMatrix` renders 9 KPIs per strategy + the time-range
    Select + the lifecycle state pill.
  - The detail panel renders within 200 ms of opening.
- **Status:** IN PROGRESS.
