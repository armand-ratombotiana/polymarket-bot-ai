# Worklog

## R11+R12 — Unified Decision Ledger (SQLite) + pipeline wiring
- **Date:** 2026-09-03
- **Scope:** NEW `core/decision_ledger.py` + additive `decision_id` field on
  `core/data_store.Order` + additive wiring into `strategies/signal_trader.py`,
  `strategies/base.py`, `paper/simulator.py`, and `api/server.py` (no existing
  code removed; all changes are append-only or new helper methods / kwargs).

### Background / investigation
- The trading pipeline already existed as discrete stages — ML predict →
  signal generation → risk gate → order submission → fill — but each stage
  logged to a different store (`audit_trail.db`, `store_state.json`,
  in-memory `event_log`). Tracing the lifecycle of a single decision
  required correlating timestamps across three data sources, with no
  canonical id linking them.
- `core/audit_logger.py` already establishes the SQLite + `asyncio.to_thread`
  pattern used here; the new ledger mirrors that convention so the two
  databases coexist without schema contention.
- `strategies/signal_trader._ml_signal` is **synchronous** (returns
  `MarketSignal | None` directly). The decision-ledger writes are async, so
  the strategy uses a fire-and-forget pattern (`asyncio.ensure_future` on the
  running loop) — this is critical because awaiting each ledger write inline
  would block the 15 s scan cadence on SQLite I/O.
- The four rejection paths in `_ml_signal` are:
  1. `confidence < self._min_confidence` → `low_confidence`
  2. `spread >= 0.04` → `wide_spread`
  3. `0.45 < p_yes < 0.55` → `neutral_zone`
  4. `kelly_numerator <= MIN_KELLY_NUMERATOR` → `insufficient_kelly_edge`
  Each path now calls `record_rejection()` with `decision_id`, `predicted_edge`,
  `confidence`, `reason`, and `market_mid` before returning `None`.
- `OrderArgs` (in `core/clob_client.py`) is the canonical order-shape passed
  to `paper_sim.create_order` and `clob_client.create_order`. The
  `decision_id` is passed as a separate kwarg through `submit_order` →
  `create_order` (NOT added to `OrderArgs` itself, keeping that dataclass
  untouched per "additive only — do NOT remove existing code").

### R11 — New `core/decision_ledger.py`

#### Schema (SQLite, separate db at `DECISION_LEDGER_DB_PATH`
defaulting to `/app/data/decision_ledger.db`)
- **`decision_events`** — `(id, timestamp, decision_id, stage, token_id,
  strategy, pnl, data_json)` — the main chronological chain.
  Indexes: `(decision_id, timestamp ASC)` for chain reconstruction,
  `(token_id, timestamp DESC)` for the token-level feed, `(stage)` for
  per-stage aggregate queries.
- **`decision_rejections`** — `(id, timestamp, decision_id, token_id,
  strategy, predicted_edge, confidence, reason, market_mid)` — a fast
  filtered rejection view. Each rejection is also written to
  `decision_events` with `stage='RISK_REJECTED'` so the originating
  `decision_id`'s chain has a complete end-to-end trail.
- WAL-friendly `CREATE TABLE IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`
  idempotent init — safe to call on every boot.

#### Public API
- `decision_ledger.new_decision_id() -> str` — returns a fresh
  `dec-{uuid4.hex}` (32 hex chars after prefix; globally unique, sortable
  prefix for log grepping).
- `decision_ledger.record(decision_id, stage, token_id=None,
  strategy=None, pnl=0.0, **data)` — async write. `**data` is JSON-serialised
  (with `default=str` for Decimals / enums / dataclasses) and stored in
  `data_json`. Empty `decision_id` is skipped silently (legacy / manual
  orders don't break the ledger).
- `decision_ledger.record_rejection(token_id, strategy, predicted_edge,
  confidence, reason, market_mid=None, decision_id="")` — async write.
  Records BOTH a `RISK_REJECTED` chain event (if `decision_id` provided)
  AND a row in the `decision_rejections` table.
- `decision_ledger.get_chain(decision_id) -> list[dict]` — chronological
  (oldest-first) stage chain. Each row includes both raw `data_json` and a
  decoded `data` key for caller convenience.
- `decision_ledger.get_chain_by_token(token_id, limit=50) -> list[dict]` —
  most-recent-first stage events for a token.
- `decision_ledger.get_rejections(limit=50) -> list[dict]` — most-recent-
  first rejection feed.
- `register_routes(app)` — appends two FastAPI routes:
  - `GET /api/decision/{token_id}?limit=N` — recent decision events for a
    token (404 if none recorded). Returns `{token_id, count, events[]}`.
  - `GET /api/decisions/rejected?limit=N` — recent rejections. Returns
    `{count, rejections[]}`.

#### Module-level constants
Stage vocabulary (single source of truth across the pipeline):
- `STAGE_PREDICTION = "PREDICTION"`
- `STAGE_SIGNAL = "SIGNAL"`
- `STAGE_RISK_APPROVED = "RISK_APPROVED"`
- `STAGE_RISK_REJECTED = "RISK_REJECTED"`
- `STAGE_ORDER = "ORDER"`
- `STAGE_FILL = "FILL"`

Rejection reason vocabulary:
- `REASON_LOW_CONFIDENCE = "low_confidence"`
- `REASON_WIDE_SPREAD = "wide_spread"`
- `REASON_NEUTRAL_ZONE = "neutral_zone"`
- `REASON_INSUFFICIENT_KELLY_EDGE = "insufficient_kelly_edge"`

### R11 — `core/data_store.py` (additive)
- Appended `decision_id: str = ""` to the `Order` dataclass after the existing
  `paper: bool = False` field. Default empty string means all existing
  `Order(...)` constructors (10 sites across `position_manager.py`,
  `api/server.py`, `strategies/base.py`, `paper/simulator.py`) continue to
  work unchanged. The field is populated only by the
  signal-trader → submit-order → create-order chain.

### R12 — Pipeline wiring (additive only)

#### (1) `strategies/signal_trader.py`
- Added `decision_id: str = ""` to the `MarketSignal` dataclass (after
  `source: str`).
- Added two helper methods:
  - `_emit_ledger(coro)` (static) — schedules an async ledger write on the
    running loop via `asyncio.ensure_future`. Swallows errors and
    no-op's if the loop isn't running (e.g. unit-test context).
  - `_emit_rejection(token_id, decision_id, predicted_edge, confidence,
    reason, market_mid)` — wraps `decision_ledger.record_rejection()` and
    schedules it via `_emit_ledger`.
- `_ml_signal()` is now structured as:
  1. Generate `dec_id = decision_ledger.new_decision_id()` (with try/except
     fallback to `""` if the ledger module fails to import — strategy never
     crashes on ledger plumbing).
  2. Compute `p_yes`, `confidence`, `mid`, `spread`, `predicted_edge`.
  3. Emit **PREDICTION** stage (always — even rejected predictions leave a
     traceable chain).
  4. Each of the four rejection paths calls `_emit_rejection(...)` BEFORE
     returning `None`. The existing `return None` statements are preserved.
  5. After all gates pass, emit **SIGNAL** stage with full kelly context
     (`direction`, `target_price`, `size_usdc`, `kelly_f`, `kelly_numerator`,
     `win_prob`, `payout_ratio`, `p_yes`, `confidence`, `market_mid`, `reason`).
  6. Return `MarketSignal(..., decision_id=dec_id)`.
- `_act_on_signal()` now passes `decision_id=sig.decision_id` to
  `self.submit_order(args, decision_id=...)`.

#### (2) `strategies/base.py`
- `submit_order(self, args, decision_id: str = "") -> Order | None` — new
  keyword arg (default empty for legacy callers).
- `provisional` Order now includes `decision_id=decision_id`.
- On risk rejection: emits **RISK_REJECTED** stage with `reason`,
  `side`, `price`, `size`. (The existing `await store.log_event(...)` is
  preserved.)
- On risk approval: emits **RISK_APPROVED** stage before paper/live dispatch.
- Paper path: `paper_sim.create_order(args, strategy=self.name,
  decision_id=decision_id)`.
- Live path: the resulting `Order(...)` is constructed with
  `decision_id=decision_id` so it propagates through to `store.add_order`.
- All ledger writes are wrapped in `try/except` and logged at DEBUG on
  failure — the order path never blocks on ledger I/O.

#### (3) `paper/simulator.py`
- `create_order(self, args, strategy="", decision_id="")` — new keyword arg.
- The resulting `Order(...)` carries `decision_id=decision_id`.
- After `store.add_order(order)`, emits **ORDER** stage with `order_id`,
  `side`, `price`, `size`, `paper=True`. (Existing `store.log_event` and
  `log.info` calls are preserved.)
- `_execute_fill(order, fill_price)` — after `store.update_order(...)` (and
  before the existing `store.log_event(...)` for the fill), emits **FILL**
  stage with `pnl`, `fill_price`, `fill_size`, `side`, `order_id`,
  `trade_id`, `paper=True`. A missing `decision_id` (legacy / manual
  order) is skipped silently.

#### (4) `api/server.py`
- Appended at end of file (after the WebSocket `/ws` route):
  ```python
  from core.decision_ledger import register_routes as _register_decision_routes
  _register_decision_routes(app)
  ```
- This registers the two new endpoints:
  - `GET /api/decision/{token_id}`
  - `GET /api/decisions/rejected`
- Both inherit the existing fail-closed bearer-token auth middleware (no
  new public paths added).

### Verification

#### py_compile (clean)
- `core/decision_ledger.py` ✓
- `core/data_store.py` ✓
- `strategies/signal_trader.py` ✓
- `strategies/base.py` ✓
- `paper/simulator.py` ✓
- `api/server.py` ✓

#### Decision-ledger unit smoke test (PASS)
- `new_decision_id()` returns `dec-{32-hex}` (globally unique).
- `record()` writes 5 stages (PREDICTION → SIGNAL → RISK_APPROVED →
  ORDER → FILL) for a single `decision_id`.
- `get_chain(decision_id)` returns them in chronological order.
- FILL stage's `pnl` column carries the realised P&L.
- `get_chain_by_token(token_id)` returns events newest-first.
- `record_rejection()` writes BOTH a `RISK_REJECTED` chain event AND a
  `decision_rejections` row.
- `get_rejections()` returns newest-first.
- Empty `decision_id` is a no-op for `record()` and returns `[]` for
  `get_chain()` / `get_chain_by_token()`.

#### FastAPI route registration smoke test (PASS)
- Both routes registered on `app`:
  - `GET /api/decision/{token_id}`
  - `GET /api/decisions/rejected`
- `GET /api/decision/TOK_DEMO` → 200, 5 stages, `data_json` decoded into
  `data` key, `pnl` column populated on FILL.
- `GET /api/decision/NOPE` → 404.
- `GET /api/decisions/rejected` → 200, 1 rejection with `predicted_edge`,
  `confidence`, `reason`, `market_mid` columns.
- `GET /api/decisions/rejected` (no auth header) → 401 (fail-closed
  middleware preserved).
- `?limit=N` query param respected.

#### End-to-end integration test (PASS)
- Patched `ml_model.predict` and `extract_features` for deterministic
  outcomes; bypassed `risk_manager.check_order` (test scope = ledger
  plumbing, not the risk engine itself).
- **Happy path**: `_ml_signal(...)` returns a `MarketSignal` with
  `decision_id` populated. Chain contains `[PREDICTION, SIGNAL]` after
  the call. P&L on FILL = 0.0 for an opening BUY (correct — P&L is
  computed only for closing SELLs).
- **All 4 rejection paths** verified — each emits a `record_rejection()`
  with the correct `reason` code (`low_confidence`, `wide_spread`,
  `neutral_zone`, `insufficient_kelly_edge`). All 4 appear in
  `get_rejections()`.
- **End-to-end paper trade**: `_act_on_signal` → `submit_order` →
  `paper_sim.create_order` → `_execute_fill`. The complete chain for the
  originating `decision_id` is
  `[PREDICTION, SIGNAL, RISK_APPROVED, ORDER, FILL]` (chronological).
- The `Order` returned from `paper_sim.create_order` carries
  `decision_id=did2` (verified via `store.open_orders`).
- `get_chain_by_token(token_id)` returns ≥5 rows for the token.

### Notes / known behaviour
- The `_emit_ledger` fire-and-forget pattern means ledger writes happen
  *after* `_ml_signal` returns. Callers that need to verify the chain
  synchronously must `await asyncio.sleep(...)` (typically 100-150 ms is
  enough for SQLite WAL writes to flush on a non-loaded system). Production
  callers (the strategy scan loop) don't need to wait — the writes
  eventually land.
- The `record()` and `record_rejection()` methods both use
  `asyncio.to_thread(_insert)` for SQLite I/O, so they don't block the
  event loop. Combined with `_emit_ledger`'s `ensure_future`, the strategy
  scan cadence is unaffected.
- A missing / empty `decision_id` is silently skipped on writes (and
  returns `[]` on `get_chain`). This preserves backward compatibility for
  manual / legacy order paths (`/api/trade`, `position_manager.exit_order`,
  etc.) that don't yet participate in the unified ledger.
- The `decision_rejections` table duplicates the rejection info that's
  also in `decision_events` (as a `RISK_REJECTED` stage row). This is
  intentional — the rejections table is indexed for fast filtered listing
  on the dashboard, while the chain row keeps the originating
  `decision_id`'s audit trail complete.
- The FILL stage records `pnl=0.0` for opening BUY orders (no P&L
  realised until the position is closed). This mirrors the existing
  `_execute_fill` P&L logic in `paper/simulator.py` (P&L is only computed
  for SELL orders closing a long position).
- Live-mode orders (`paper=False`) currently flow through `clob_client`
  without a ledger ORDER stage record (only PREDICTION → SIGNAL →
  RISK_APPROVED is recorded for live orders). Adding an ORDER stage for
  live orders would require either extending `clob_client.create_order`'s
  return shape or wrapping it in `strategies/base.py`; out of scope for
  R11+R12 (the task spec only mentions paper `create_order`).

### Open items / follow-ups
- (Optional) Add a `GET /api/decision/{token_id}/chain/{decision_id}`
  endpoint that calls `get_chain(decision_id)` directly for tracing a
  single decision chain end-to-end (vs. the current token-level feed).
- (Optional) Surface decision-ledger stats (count of rejections per
  reason, top rejected tokens) on the dashboard.
- (Optional) Extend `position_manager.py` exit orders to populate
  `decision_id` so close-trade P&L is attributable to the originating
  signal.
- (Optional) Wire a `STAGE_CANCEL` event when an open paper order is
  cancelled (via `paper_sim.cancel_order`) so cancelled chains don't
  appear as "missing FILL" silently.

---

## R10 — Fix ML predict endpoint + add one-click position close
- **Date:** 2025-09-03
- **Scope:** `mini-services/polymarket-bot/api/server.py` (additive only; no existing code removed)

### Background / investigation
- `core/market_discovery.py` exposes a singleton `market_discovery` whose
  `catalog: dict[str, dict]` (token_id → market metadata) is the canonical
  in-memory market index. The task explicitly requires reading market
  metadata from `market_discovery.catalog` — the `store.market_info`
  attribute referenced in earlier draft specs **does not exist** on
  `core/data_store.DataStore` (verified by grep + runtime import).
- `ml/model.py::MarketMLModel.predict(features, token_id)` returns
  `(p_yes, confidence)`; `confidence = |p_yes − 0.5| × 2`.
- `ml/features.py::extract_features(market, book)` expects a Gamma-style
  market dict (`volume24hr`, `volume`, `liquidity|liquidityNum`,
  `endDate|end_date_iso|endDateIso`). The catalog record uses normalized
  snake_case keys (`volume_24h`, `total_volume`, `liquidity`, `end_date`),
  so the predict endpoint bridges both shapes before feature extraction.
- `core/data_store.Position` carries `yes_shares` (long YES) and
  `no_shares` (long NO). Closing a long YES → SELL at best_bid; closing a
  long NO → BUY at best_ask (symmetric synthetic-short cover).
- Risk gate (`risk.manager.RiskManager.check_order`) is applied on every
  order — close orders are not exempt (consistent with `/api/trade`).
- `paper/simulator.PaperSimulator._can_fill` fills a SELL at price=best_bid
  whenever `best_bid ≥ price` (true by construction), so a marketable
  close order settles in the ~1s paper fill-loop.

### Changes (all additive — no existing endpoints modified)

#### 1. `GET /api/ai/predict/{token_id}` (tag: `ai`)
- Reads metadata from `market_discovery.catalog[token_id]`; 404 if absent
  (hint: retry after next catalog sync, or check `/api/markets/coverage`).
- Fetches live `OrderBook` via `store.get_order_book`; 502 + poller hint
  if absent.
- Builds a feature-compatible market dict (snake_case → Gamma-style aliases)
  and calls `ml.features.extract_features` + `ml_model.predict`.
- Returns:
  - `p_yes`, `confidence`, `market_mid`, `best_bid`, `best_ask`, `spread`
  - `edge = p_yes − market_mid` and `edge_bps` (basis points)
  - `recommended_action`: `BUY` / `SELL` / `HOLD`
    - **BUY**: `edge ≥ +2 ct (200 bps)` AND `confidence ≥ 0.10`
    - **SELL**: `edge ≤ −2 ct (−200 bps)` AND `confidence ≥ 0.10`
    - **HOLD**: otherwise (insufficient edge OR insufficient model conviction)
    - Edge-based (not absolute p_yes) so a model that sees a 50/50 coin
      priced at 0.20 surfaces BUY rather than HOLD merely because
      `|p_yes − 0.5|` is small.
  - `action_reason`: human-readable string explaining the decision
  - `thresholds`: the gates used (auditable)
  - `model_status`: model_ready, version, brier, roc_auc, ece, n_updates
  - `market`: compact catalog payload (token_id, event_id, question,
    slug, outcome, category, end_date, status, volume_24h, total_volume,
    liquidity, last_synced)
  - `book_updated_at`, `timestamp`

#### 2. `POST /api/positions/{token_id}/close` (tag: `trading`)
- Optional JSON body (`PositionCloseRequest`):
  - `max_size_shares: float | None` — cap for partial scale-outs
  - `dry_run: bool` — preview without submitting
- Looks up position under `store._lock` (snapshots values before release).
- 404 if no open position (`yes_shares ≤ 0 AND no_shares ≤ 0`).
- Determines side from position direction:
  - Long YES → `SELL` at `best_bid` (marketable limit; any bid ≥ best_bid
    matches immediately)
  - Long NO → `BUY` at `best_ask` (symmetric synthetic-short cover)
- 502 + poller hint if book missing or both sides empty; specific 502 if
  the relevant side (bid for YES close, ask for NO close) is empty.
- Computes `estimated_pnl` for YES-close using cost basis
  (`(close_price − avg_entry_price) × size_shares`).
- `dry_run=true` returns the preview without submitting (includes
  `remaining_position` projection).
- Applies `risk_manager.check_order` (same gate as `/api/trade`);
  400 with reason on rejection.
- Submits `OrderArgs(order_type="FOK")` through `paper_sim` (paper mode)
  or `clob_client` (live mode); for live mode constructs and stores the
  resulting `Order`.
- Writes an immutable `audit_logger.log_event` row
  (`category=trading`, `event_type=position_close`).
- Emits a `store.log_event` user-visible event line.
- Returns: `status`, `order_id`, `side`, `price`, `size_shares`,
  `notional_usdc`, `estimated_pnl`, `best_bid`, `best_ask`,
  `book_updated_at`, `paper_trade`, `remaining_position` (post-close
  projection), `note`.

### Verification (in-process TestClient smoke tests)
- `python3 -c "import ast; ast.parse(open('api/server.py').read())"` → clean.
- Full module import succeeds (after redirecting data paths to /tmp for
  the sandbox); both new routes registered:
  - `GET  /api/ai/predict/{token_id}`
  - `POST /api/positions/{token_id}/close`
- End-to-end TestClient run with a seeded catalog record + fake order book:
  - `GET /api/ai/predict/TEST_TOKEN_YES` → 200, `p_yes=0.932`,
    `market_mid=0.81`, `edge=+0.122`, `recommended_action=BUY`,
    `action_reason="edge=+12.20ct ≥ +2ct AND confidence=0.864 ≥ 0.10"`.
  - `POST /api/positions/.../close` (`dry_run=true`) → 200, `side=SELL`,
    `price=0.80` (best_bid), `size_shares=50.0`, `estimated_pnl=15.0`,
    `remaining_position.yes_shares=0.0`.
  - Partial close (`max_size_shares=10, dry_run=true`) → 200, sized to 10.
  - Live submit (paper mode, small position under the $3/mkt cap):
    200 with `order_id=paper-…`, fill-loop settled in ~1s,
    position reduced to 0, `realised_pnl=0.56`, trade recorded.
  - Large live submit (>$3 cap): 400 risk-rejection (expected — same
    gate as `/api/trade`).
  - 404 paths: `predict` on unknown token (catalog miss); `close` on
    token without a position.
  - Auth: missing `Authorization` header → 401 (fail-closed).

### Open items / follow-ups
- `clob_client.create_order` currently ignores `OrderArgs.order_type`
  (signs a vanilla limit order); the `FOK` flag is metadata only. If the
  exchange is expected to honor fill-or-kill semantics, `clob_client`
  must be extended separately (out of scope for R10).
- The recommend-action thresholds (`MIN_EDGE_CT=0.02`, `MIN_CONFIDENCE=0.10`)
  are hard-coded constants mirroring the conviction gates in
  `strategies/signal_trader.py`; a future task could expose them via
  `/api/config` for live tuning.

---

## R5 — ML label backfill service
- **Date:** 2026-09-03
- **Scope:** New `core/label_backfill.py` service + additive `core/timescale_db.py`
  methods + minimal `api/server.py` lifespan wiring (no existing endpoints modified).

### Summary
Added a resolved-market label backfill service that pages through Gamma API
resolved markets, builds 38-dim feature vectors from market metadata + a
synthetic order book, persists `(features, resolved_label)` rows into the
SQLite `ml_feature_store`, and triggers a model retrain once ≥50 real labels
have accumulated. Runs on a 45 s startup grace, then on a 24 h daily cycle.

### Files
- **NEW** `mini-services/polymarket-bot/core/label_backfill.py`
  - `LabelBackfillEngine` singleton (`label_backfill_engine`) with
    `start()` / `stop()` / `run_backfill_once()` / `stats`.
  - 45 s startup grace → first backfill pass → daily 86 400 s loop.
  - Pages Gamma `get_markets(active=False, closed=True, order="updatedAt")`
    up to `MAX_PAGES=25` × `PAGE_SIZE=100` (≤ 2 500 markets/cycle).
  - For each market: parses `outcomePrices` for `resolved_yes` (mirrors
    `core/settlement.py`'s `p0 >= 0.9` convention), extracts YES/NO token
    IDs via `gamma_client.extract_token_ids`, builds a 5-level synthetic
    order book from `outcomePrices` + `volume24hr` + `liquidity` (mid
    clipped into `[0.02, 0.98]` so `extract_features()` doesn't reject the
    sample), and calls `ml.features.extract_features()` to get the 38-dim
    vector.
  - Persists via `timescale_db.record_feature_vector(..., outcome_resolved=…)`
    which writes to SQLite `ml_feature_store` (and PG when pool is up).
  - Idempotent per token: `timescale_db.has_labeled_sample(token_id)` gates
    every write so a token is never re-labeled across cycles.
  - Retrain trigger: after each cycle, if
    `fetch_labeled_feature_vectors(limit=10_000)` returns ≥
    `MIN_LABELS_FOR_RETRAIN=50` samples, calls `ml_model.fit_initial()`
    (off the event loop via `asyncio.to_thread`) then `ml_model.save()`.
    Logging includes resulting `brier_score`/`roc_auc`/`ece`/`training_source`.

- **MODIFIED** `mini-services/polymarket-bot/core/timescale_db.py`
  (additive only — no existing methods touched, no duplicates introduced)
  - **NEW** `has_labeled_sample(token_id) -> bool`: returns True iff any
    row in `ml_feature_store` for `token_id` has `outcome_resolved IS NOT
    NULL`. Used by the backfill service for idempotent dedup.
  - **REUSED (pre-existing)** `fetch_labeled_feature_vectors(limit=200)
    -> list[tuple[np.ndarray, int]]`: returns up to `limit` labeled
    `(features, label)` tuples from `ml_feature_store` (most-recent
    first), padded/trimmed to `N_FEATURES`. This method was already in
    the file (consumed by `EnsembleMetaLearner.warm_from_labeled_samples()`);
    R5 simply re-uses it as the count + sample source for the ≥50-label
    retrain threshold check. No duplicate definition was added — the
    meta-learner contract is preserved verbatim (verified by AST scan:
    exactly one `fetch_labeled_feature_vectors` and one
    `has_labeled_sample` method on `TimescaleDBEngine`).
  - Both methods follow the existing sync + sqlite3-only convention used
    by `fetch_recent_feature_vector` / `fetch_training_samples`.

- **MODIFIED** `mini-services/polymarket-bot/api/server.py` (additive)
  - Registered `"label_backfill"` with the watchdog subsystem list.
  - Lifespan startup: after `training_orchestrator.start()`, calls
    `await label_backfill_engine.start()` + `watchdog.beat("label_backfill")`
    + a `store.log_event` for UI visibility.
  - Lifespan shutdown: `await label_backfill_engine.stop()` before
    `settlement_engine.stop()`.
  - `/api/ml` endpoint now exposes `label_backfill: stats` alongside
    `training_orchestrator` stats for observability.

### Verification
- `python -m py_compile` clean on `core/label_backfill.py`,
  `core/timescale_db.py`, `api/server.py`.
- AST scan confirms `TimescaleDBEngine` has exactly 1
  `fetch_labeled_feature_vectors` (pre-existing) and 1
  `has_labeled_sample` (new R5) — no duplicate definitions, no existing
  methods removed/renamed (additive-only constraint honored).
- Targeted functional test passes:
  - Engine constructs in non-running state; stats snapshot well-formed.
  - `_resolve_outcome` parses YES / NO / unresolvable markets correctly.
  - `_build_synthetic_book` produces a 5-level book with clipped mid in
    `[0.02, 0.98]`, sized from `volume24hr`.
  - `ml.features.extract_features(synthetic_book)` returns a valid
    `(38,)` float32 vector (no None).
  - End-to-end: `record_feature_vector(outcome_resolved=1)` →
    `has_labeled_sample(token_id)=True` (and False for other tokens) →
    `fetch_labeled_feature_vectors` returns the same row as a
    `list[tuple[np.ndarray, int]]` (1 sample, label=1).
  - Retrain-deferred: with <50 labeled samples, `_maybe_trigger_retrain`
    correctly returns False (does NOT call `fit_initial`).
  - Idempotency: second `_persist_token_label` call for the same token
    returns `(added=0, skipped=1)` — no duplicate writes.
  - Meta-learner contract preserved: simulated
    `for features, label in fetch_labeled_feature_vectors(...)` iteration
    (exactly how `EnsembleMetaLearner.warm_from_labeled_samples` consumes
    the result) works correctly.

### Notes / known behaviour
- The model's `fit_initial()` blends real+synthetic training data only when
  `timescale_db.fetch_training_samples(min_samples=200)` returns ≥200
  labeled rows (existing system threshold, untouched per "additive only"
  constraint). At 50–199 labeled rows the retrain fires but falls back
  to synth-only training — this is pre-existing `fit_initial()` semantics,
  not a bug introduced by R5. Once settlement-engine live labels +
  backfill labels together cross 200, real data is blended in automatically.
- Pagination caps at 25 pages × 100 markets = 2 500 markets/cycle to bound
  worst-case Gamma API load. Tunable via `MAX_PAGES` / `PAGE_SIZE`.
- The synthetic order book is a best-effort reconstruction from Gamma
  metadata (resolved markets no longer have a live CLOB book). Cluster
  correlation defaults to 0.5 and fundamental sentiment defaults to 0.0
  for backfilled samples — both are graceful fallbacks already in
  `ml.features.extract_features`.

### Next actions
- (Optional) Lower `fetch_training_samples`'s `min_samples=200` to align
  with the backfill's 50-label threshold if we want the retrain at 50
  labels to actually blend real data — would be a separate, non-additive
  change to `ml/model.py`.
- (Optional) Add a `/api/ml/label-backfill/run` POST endpoint that calls
  `await label_backfill_engine.run_backfill_once()` for on-demand cycles
  outside the daily schedule.
- (Optional) Surface `label_backfill` stats on the dashboard.

---
Task ID: GM-REBUILD (Wave 1 — 15 subagents R1-R15: execution fixes, ML fixes, decision ledger, analytics, smart router)
Agent: orchestrator + 15 subagents
Task: Rebuild all critical improvements from scratch after sandbox reset (4 prior waves lost).

Work Log:
- R1: position_manager.py — marketable SL/TP exits at best_bid (was mid), SL tightened 15%→5%, cancel stale exits
- R2: market_maker.py — dropped 0.01 A-S damping, bounded ask_size to inventory, inventory flush >60s
- R3: risk/manager.py — MDD baseline OPERATING_CAPITAL fix, per-trade circuit breaker (PER_TRADE_MAX_LOSS=$0.50, cooldown=300s)
- R4: paper/simulator.py — slippage model (crossing+size+queue), wired report_trade_pnl into fills
- R5: core/label_backfill.py (NEW) — pages resolved markets from Gamma, writes labeled features, triggers retrain
- R6: ml/drift_detector.py — reset() clears all state, PSI uses model's own distribution, threshold 0.25
- R7: ml/model.py — time-ordered split (arange vs random permutation), Sharpe from real equity history
- R8: ml/ensemble_meta_learner.py — warm_from_labeled_samples(), NaN/Inf dropping in refit, WARNING-level logging
- R9: strategies/signal_trader.py — catalog.items() instead of values(), confidence floor 0.55→0.45
- R10: api/server.py — GET /api/ai/predict/{token_id}, POST /api/positions/{token_id}/close
- R11+R12: core/decision_ledger.py (NEW) + wired through signal_trader→base→paper_sim→server
- R13: execution/smart_router.py copied from source
- R14: ml/features.py — competitiveness derived from spread (was constant 0.9)
- R15: api/server.py — unrealized_pnl+current_price in snapshot, expectancy+avg_win+avg_loss+sharpe in analytics

Stage Summary:
- Backend healthy, 61 API routes, lint clean, zero overflow
- Win rate 80%, expectancy +$0.19, avg_win $0.25, avg_loss -$0.03
- Balance $111.83 (up from $100 — profitable!)
- Decision ledger operational (PREDICTION→SIGNAL→RISK→ORDER→FILL chain)
- ML predictions returning real values, drift HEALTHY
- All 15 execution+ML+decision fixes rebuilt from scratch
