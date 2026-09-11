# Cross-System Architecture Assessment — Polymarket Bot (§50 / §51 / §79 / §80)

- **Task ID:** W17-8 (File 1 of 3)
- **Agent:** general-purpose
- **Date:** 2026-09-17
- **Scope:** Read-only architectural trace of `mini-services/polymarket-bot/`
  across the full trading-pipeline lifecycle (§50) plus verification of the
  unified decision-ledger chain (§51), the target layered architecture
  (§79), and the "Why did the bot make this trade?" question (§80).
- **Evidence basis (classification legend):**
  - **VERIFIED** — read in source file, witnessed in this session.
  - **STRONG EVIDENCE** — read in a docstring/comment that names a specific
    line, table, or constant and matches surrounding context.
  - **LIKELY** — consistent with code patterns observed but not directly
    verified.
  - **UNVERIFIED** — claim is plausible but not yet confirmed.
  - **NOT FOUND** — no evidence located in the codebase for the named
    capability.

---

## 1. Executive Summary

The Polymarket Bot is a **15-stage end-to-end trading pipeline** organized as
a flat package of `core/`, `ml/`, `strategies/`, `execution/`, `paper/`,
`risk/`, `api/`, and `backtesting/` modules. The pipeline the task asks us
to trace (§50):

```
DATA INGESTION → NORMALIZATION → STORAGE → FEATURE ENGINEERING →
AI/ML → STRATEGIES → RISK → PORTFOLIO → EXECUTION → ORDERS →
FILLS → POSITIONS → P&L → PERFORMANCE → LEARNING
```

maps onto the codebase as follows (each `→` is a real boundary; gaps in
the chain are flagged inline):

| Stage | Module(s) | Status |
|---|---|---|
| DATA INGESTION | `core/book_poller.py`, `core/gamma_client.py`, `core/fundamental_ingest.py`, `core/ingestion/source_registry.py`, `core/ingestion/raw_vault.py` | VERIFIED — multiple sources wired |
| NORMALIZATION | `core/market_discovery.py`, `core/sanitizer.py` (partial) | PARTIAL — book normalisation is implicit in `book_poller`; no separate "normalized" stage |
| STORAGE | `core/data_store.py` (in-memory + JSON), `core/market_db.py` (SQLite), `core/timescale_db.py` (Postgres/Timescale optional) | VERIFIED — three stores, fragmented |
| FEATURE ENGINEERING | `ml/features.py`, `ml/feature_store.py` | VERIFIED — features computed in `signal_trader._ml_signal`, persisted via feature store |
| AI/ML | `ml/model.py` (RF + GB + SGD + LightGBM ensemble, isotonic calibration, stacking meta-learner), `ml/drift_detector.py`, `ml/calibration.py` | VERIFIED |
| STRATEGIES | `strategies/signal_trader.py`, `strategies/market_maker.py`, `strategies/arb_scanner.py`, `strategies/base.py` | VERIFIED — registry-driven |
| RISK | `risk/manager.py` (pre-trade gates), `risk/routes.py` (paused-strategy visibility) | VERIFIED |
| PORTFOLIO | `core/portfolio.py`, `core/portfolio_optimizer.py`, `core/portfolio_mark_to_market.py`, `core/correlation.py` | VERIFIED but **partially disconnected** from live trading (see §9) |
| EXECUTION | `execution/smart_router.py`, `execution/advanced_router.py`, `paper/simulator.py`, `core/clob_client.py` | VERIFIED — smart router exists but live path delegates to `clob_client` directly |
| ORDERS | `core/data_store.py::Order`, `core/order_state_machine.py`, `core/audit_logger.py` | VERIFIED |
| FILLS | `paper/simulator.py::_execute_fill`, `core/execution_quality.py` | VERIFIED |
| POSITIONS | `core/data_store.py::Position`, `core/position_manager.py`, `core/closed_positions.py` | VERIFIED |
| P&L | `core/portfolio.py::compute_exposure`, `core/portfolio_mark_to_market.py`, `core/closed_positions.py` | VERIFIED |
| PERFORMANCE | `core/attribution.py` (7-dim), `backtesting/report.py` | VERIFIED |
| LEARNING | `ml/model.py::partial_fit` (SGD online), `core/label_backfill.py`, `ml/training_orchestrator.py` | PARTIAL — online SGD only; RF/GB do not retrain on live fills |

**Headline findings** (full list in §23):

1. **The pipeline runs end-to-end and is observable** — every stage emits
   to either the `audit_trail.db`, the `decision_ledger.db`, or the
   `observability.db` (VERIFIED, three SQLite files exist in `data/`).
2. **The decision ledger (§51) exists and is well-formed** — 6 stages are
   wired through a UUID `decision_id`: `PREDICTION → SIGNAL →
   RISK_APPROVED | RISK_REJECTED → ORDER → FILL`. (VERIFIED in
   `core/decision_ledger.py:111-116`).
3. **However, the §51 ledger is *shorter than the spec*.** The task spec
   asks for 12 stages: `MARKET → MARKET SNAPSHOT → INTELLIGENCE SNAPSHOT
   → FEATURE SNAPSHOT → MODEL PREDICTION → STRATEGY SIGNAL → RISK DECISION
   → ORDER → FILL → POSITION → OUTCOME → P&L`. The current ledger covers
   only 5 of those (`PREDICTION`, `SIGNAL`, `RISK_APPROVED/REJECTED`,
   `ORDER`, `FILL`). The pre-PREDICTION stages (MARKET / SNAPSHOT /
   INTELLIGENCE / FEATURE) and the post-FILL stages (POSITION / OUTCOME /
   P&L) are **NOT** linked by `decision_id` — they live in separate
   tables (`market_intelligence.db`, `feature_store.db`,
   `closed_positions.db`) with only an *optional* `decision_id` foreign
   key on the closed-positions row. **(STRONG EVIDENCE — gap analysis
   below in §9 / §10.)**
4. **The system CAN answer "Why did the bot make this trade?" (§80) —
   partially.** For every trade that reaches `ORDER` or `FILL`, the
   PREDICTION-stage row carries `p_yes`, `confidence`, `model_version`,
   `predicted_edge`, `market_mid`; the SIGNAL row carries `reason` /
   `direction`; the RISK_APPROVED row carries `price` / `size`; the FILL
   row carries `pnl`. Joined by `decision_id`, this is a 5-column answer.
   What it CANNOT answer: "which market snapshot did this prediction
   read from?" and "which feature vector was fed to the model?" — those
   are not linked by `decision_id`. **(VERIFIED via
   `decision_ledger.py:111-116, 244-307` + `signal_trader.py:120-200`.)**
5. **Target architecture (§79) is largely in place**, with the data
   layer's two backends (SQLite + optional TimescaleDB) and the strategy
   engine's registry pattern. The missing layer is a unified
   *intelligence / feature snapshot store* — features are recomputed
   on each scan rather than snapshotted and referenced by ID.

### Maturity snapshot (this file, §22 has the score detail)

| Dimension | §79 Target Layer | Present? | Source |
|---|---|---|---|
| DATA LAYER | Yes (SQLite + Timescale + raw_vault) | VERIFIED |
| FEATURE/INTEL LAYER | Partial (feature_store exists; no intelligence-snapshot store) | LIKELY |
| AI/ML | Yes (4-model ensemble + calibration + drift) | VERIFIED |
| STRATEGY ENGINE | Yes (registry pattern, base class) | VERIFIED |
| RISK ENGINE | Yes (`InstitutionalRiskEngine` with 13+ gates) | VERIFIED |
| PORTFOLIO ALLOCATION | Partial (optimizer exists but not in the live trade path) | LIKELY |
| EXECUTION ENGINE | Yes (paper path + clob_client live path) | VERIFIED |
| EXCHANGE/MARKET | Yes (`clob.polymarket.com` + `gamma-api.polymarket.com`) | VERIFIED |

---

## 2. Purpose

This document exists to:

1. **Trace the 15-stage trading pipeline** (§50) through the live codebase
   and identify every disconnected stage. A "disconnected stage" is one
   where data flows in but no durable record carries a stable identifier
   to the next stage — i.e. a stage that cannot be reconstructed after
   the fact.
2. **Verify the unified decision ledger architecture** (§51). The spec
   mandates a 12-stage chain keyed by a single correlation ID. The
   codebase implements 6 of those stages. The gap is documented in §9 / §10.
3. **Verify the system can answer "Why did the bot make this trade?"**
   (§80). Concretely: given an order_id or fill_id, can the operator
   reconstruct the full input → decision → execution → outcome chain?
4. **Map the current architecture against the §79 target layering**
   (`DATA LAYER → FEATURE/INTEL LAYER → AI/ML → STRATEGY ENGINE →
   RISK ENGINE → PORTFOLIO ALLOCATION → EXECUTION ENGINE →
   EXCHANGE/MARKET`). Identify which layers are present, missing, or
   bypassed in the live trade path.

This is a read-only assessment — no source files were modified.

---

## 3. Current Architecture

The Polymarket Bot is a single-process asyncio application (`main.py` +
`api/server.py`) that runs four concurrent loops (book poller, market
discovery, strategy runner, observability collector) against a flat
package layout:

```
mini-services/polymarket-bot/
├── main.py                    # CLI entrypoint (asyncio.run)
├── config.py                  # env-var settings (paper/live/shadow)
├── api/
│   ├── server.py              # FastAPI app (~5100 lines, 80+ routes)
│   ├── rate_limit.py
│   └── graphql_schema.py
├── core/                      # 60+ modules, all singletons
│   ├── data_store.py          # in-memory state + JSON persistence
│   ├── market_db.py           # SQLite market metadata
│   ├── timescale_db.py        # optional Postgres / Timescale
│   ├── book_poller.py         # CLOB REST poller
│   ├── gamma_client.py        # Gamma discovery API client
│   ├── clob_client.py         # CLOB order API client (live)
│   ├── ingestion/
│   │   ├── source_registry.py # raw.source_registry table (Timescale)
│   │   └── raw_vault.py       # raw.raw_observation + dead_letter
│   ├── decision_ledger.py     # 6-stage unified ledger (§51)
│   ├── audit_logger.py        # durable SQLite audit trail
│   ├── immutable_audit.py     # hash-chained trail (§55)
│   ├── observability.py       # 6-category metrics store
│   ├── observability_collector.py # 30s background auto-collector
│   ├── alerting.py            # 7 rules / 4 categories
│   ├── prometheus_metrics.py  # /metrics endpoint
│   ├── circuit_breaker.py     # 3 breakers (clob / gamma / ws)
│   ├── capital_allocator.py   # T9 (safety-gated) + T5 (multiplier)
│   ├── portfolio_optimizer.py # Kelly-criterion multi-bet optimizer
│   ├── portfolio.py            # exposure decomposition
│   ├── portfolio_mark_to_market.py
│   ├── correlation.py          # Pearson matrix
│   ├── stress_test.py          # 6 scenarios
│   ├── execution_quality.py    # per-fill slippage / latency
│   ├── closed_positions.py     # round-trip P&L journal
│   ├── attribution.py          # 7-dim P&L roll-up
│   ├── shadow_trading.py      # counterfactual journal
│   ├── reconciliation.py
│   ├── safety.py              # kill-switch (in-memory + file)
│   ├── live_safety_gate.py
│   ├── security.py            # API auth
│   └── ...                    # 40 more siblings
├── ml/
│   ├── model.py               # 4-model ensemble + isotonic + stacking
│   ├── features.py           # 38-feature extract
│   ├── feature_store.py      # per-prediction feature audit trail
│   ├── drift_detector.py      # PSI + Brier + KS
│   ├── calibration.py        # isotonic
│   ├── model_registry.py      # versioning
│   ├── ab_testing.py
│   ├── explainability.py     # SHAP
│   ├── shadow_inference.py
│   └── training_orchestrator.py
├── strategies/
│   ├── base.py               # abstract strategy + submit_order
│   ├── signal_trader.py      # ML-driven directional trader
│   ├── market_maker.py
│   ├── arb_scanner.py
│   └── registry.py
├── execution/
│   ├── smart_router.py
│   └── advanced_router.py
├── paper/
│   └── simulator.py          # paper-trading fill simulator
├── risk/
│   ├── manager.py            # InstitutionalRiskEngine (13+ gates)
│   └── routes.py             # GET /api/risk/strategies/paused
├── backtesting/
│   ├── engine.py             # historical backtest
│   ├── advanced.py
│   └── report.py             # VaR / CVaR / PDF report
└── tests/                    # 100+ test files
```

**Module singleton pattern.** Every `core/*.py` module exposes a
module-level singleton (`store`, `risk_manager`, `decision_ledger`,
`audit_logger`, `observability`, `alert_engine`, `paper_sim`,
`profiler`, `db_pool`, etc.). Importers grab the instance at module
import time. This is convenient but tightly couples every caller to a
single shared instance — there is no way to run two bot instances in
the same Python process. (VERIFIED — pattern documented in
`core/profiling.py:203-206` and repeated across the package.)

**Persistence.** Seven separate SQLite databases coexist under
`/app/data/` (VERIFIED — listed in `mini-services/polymarket-bot/data/`):

| DB file | Owner module | Purpose |
|---|---|---|
| `store_state.json` | `core/data_store.py` | In-memory state hot-snapshot (orders, positions, books) |
| `market.db` | `core/market_db.py` | Market metadata cache |
| `audit_trail.db` | `core/audit_logger.py` | Durable category-indexed audit trail |
| `immutable_audit.db` | `core/immutable_audit.py` | Hash-chained control-event trail |
| `decision_ledger.db` | `core/decision_ledger.py` | Unified decision ledger (6 stages) |
| `closed_positions.db` | `core/closed_positions.py` | Round-trip trade P&L journal |
| `execution_quality.db` | `core/execution_quality.py` | Per-fill slippage / latency |
| `observability.db` | `core/observability.py` | 6-category health metrics |
| `alerts.db` | `core/alerting.py` | Fired alerts + acknowledgement |
| `shadow_trades.db` | `core/shadow_trading.py` | Counterfactual trade journal |
| `feature_store.db` | `ml/feature_store.py` | Per-prediction feature values + importance |
| `model_registry.json` | `ml/model_registry.py` | Active model version |
| `market_intelligence.db` | (market_intelligence module — UNVERIFIED owner) | Intelligence layer storage |

Plus optional Postgres/Timescale via `core/timescale_db.py` for
`raw.raw_observation`, `raw.dead_letter_record`, `raw.source_registry`.

**HTTP surface.** A single FastAPI app in `api/server.py` (~5100 lines)
registers ~80 routes, mixing inline `@app.get`/`@app.post` decorators
with `register_routes(app)` blocks appended at the file's tail (one per
feature module). (VERIFIED — 30+ `register_routes` blocks at lines
3978–end.)

---

## 4. Current Components

### 4.1 Data Layer

- `core/book_poller.py::book_poller` — REST poller against
  `https://clob.polymarket.com`, polls top-of-book for every tracked
  token. Exposes `.stats` (`success_count`, `error_count`,
  `total_tracked`, `tier1_tokens`, `tier2_tokens`).
  (VERIFIED via `observability_collector._collect_data_source_metrics`.)
- `core/gamma_client.py::gamma_client` — Gamma discovery API client.
  `extract_token_ids(market)` returns the CLOB token ids for a market.
- `core/market_discovery.py::market_discovery` — periodic 3-minute full
  catalog sync. Maintains in-memory `catalog: dict[token_id, dict]` +
  `events_catalog`. (VERIFIED via `market_discovery.py:30-60`.)
- `core/fundamental_ingest.py` — global news / sentiment ingestion
  engine. Source registry lists 100k+ candidate feeds (curated wires +
  crypto + politics + regional). (VERIFIED via
  `fundamental_ingest.py:46-60`.)
- `core/ingestion/source_registry.py` — TimescaleDB-backed source
  registry with `records_observed` / `records_accepted` /
  `records_errored` counters. Falls back to a 2-entry in-memory default
  when Postgres is unavailable. (VERIFIED via
  `source_registry.py:36-64`.)
- `core/ingestion/raw_vault.py` — immutable raw observation vault with
  SHA-256 payload checksums + bitemporal timestamps. Dead-letter
  quarantine for malformed payloads. (VERIFIED via `raw_vault.py:22-84`.)

### 4.2 Storage Layer

- `core/data_store.py::store` — in-memory `OrderBook` / `Order` /
  `Position` / `Trade` objects, atomic JSON persistence to
  `STORE_STATE_PATH` (default `/app/data/store_state.json`). Bankroll
  baseline `100.0` USD. (VERIFIED via `data_store.py:18-24`.)
- `core/market_db.py` — SQLite market metadata cache.
- `core/timescale_db.py::timescale_db` — optional asyncpg pool for
  Postgres / Timescale. `_is_postgres` flag gates the raw-vault path.
- `core/db_pool.py::db_pool` — `AsyncDBPool` (W16-7) shared aiosqlite
  connection pool used by the v2 read endpoints
  (`/api/v2/decisions/recent`, `/api/v2/observability/latest`).
  (VERIFIED via `db_pool.py` docstring.)

### 4.3 Feature / Intelligence Layer

- `ml/features.py` — `extract_features(token_id, book, ...) -> np.ndarray`
  of 38 named features. `FEATURE_NAMES` + `N_FEATURES` constants.
  (VERIFIED via `model.py:35`.)
- `ml/feature_store.py::feature_store` — SQLite store for feature
  definitions, per-prediction values, per-version importance, computed
  stats. (VERIFIED via `feature_store.py:1-100`.)
- `core/sentiment.py` — NLP keyword sentiment scorer (W17-1).
- `core/deep_analysis.py` — deeper market-context analysis.

**Gap:** There is **no `intelligence_snapshot` table** that captures the
state of all sources (order book + news + sentiment + macro) at a single
point in time and assigns it a stable `intelligence_id` that can be
referenced from the prediction chain. The spec's §51 stage
`INTELLIGENCE SNAPSHOT` is therefore NOT FOUND as a discrete stage —
the intelligence is composed ad-hoc inside `signal_trader._ml_signal`.

### 4.4 AI/ML Layer

- `ml/model.py::ml_model` — 4-model ensemble (RF + GB + SGD + LightGBM
  optional). Isotonic calibration via `CalibratedClassifierCV`.
  Level-2 stacking meta-learner (LogisticRegression) with adaptive
  Brier-weight fallback. (VERIFIED via `model.py:1-60`.)
- `ml/calibration.py::calibrator` — separate isotonic calibrator.
- `ml/drift_detector.py::drift_detector` — PSI / Brier / KS drift
  detection. `drift_status` is one of `NO_DRIFT`, `MODERATE_SHIFT`,
  `SIGNIFICANT_DRIFT`. (VERIFIED via `risk/manager.py:78-97`.)
- `ml/model_registry.py::model_registry` — versioned model registry,
  `active_version` resolved lazily (avoids circular import).
- `ml/explainability.py` — SHAP-based per-prediction explanation.
- `ml/shadow_inference.py` — runs predictions in parallel for offline
  benchmarking.
- `ml/ab_testing.py` — model A/B testing harness.
- `ml/training_orchestrator.py` — periodic retraining scheduler.
- `ml/ensemble_meta_learner.py` — the level-2 stacking logic.

### 4.5 Strategy Engine

- `strategies/base.py::BaseStrategy` — abstract base, owns the lifecycle
  (`start`/`stop`) and the unified `submit_order` path (delegates to
  `risk_manager.check_order` then either `paper_sim.create_order` or
  `clob_client.create_order`). (VERIFIED via `base.py:60-148`.)
- `strategies/signal_trader.py::SignalTraderStrategy` — ML-driven
  directional trader. 15-second scan loop, Kelly-fraction sizing (now
  routed through `capital_allocator.allocate_size`). (VERIFIED via
  `signal_trader.py:1-120`.)
- `strategies/market_maker.py` — passive market making.
- `strategies/arb_scanner.py` — cross-market arbitrage.
- `strategies/registry.py` — registry pattern for active strategies
  (`get_active_instances()`).

### 4.6 Risk Engine

- `risk/manager.py::risk_manager` — `InstitutionalRiskEngine` singleton.
  13+ pre-trade gates (kill switch, observation-only, exposure ceiling,
  per-trade loss, daily loss, weekly loss, max drawdown, cash reserve,
  per-market cap, normal-cap guidance, strategy cap, correlated-group cap,
  MTM cap, position count). Per-trade circuit breaker (`PER_TRADE_MAX_LOSS`
  → strategy cooldown). (VERIFIED via `manager.py:1-200`.)
- `risk/routes.py` — `GET /api/risk/strategies/paused` (paused-strategy
  visibility).

### 4.7 Portfolio Layer

- `core/portfolio.py` — `compute_exposure()` decomposes exposure into 8+
  dimensions (capital invested, pending, gross market value, net
  directional, max remaining loss, by group, by strategy, dollar-days,
  available cash). (VERIFIED via `portfolio.py:20-60`.)
- `core/portfolio_optimizer.py::portfolio_optimizer` — Kelly-criterion
  multi-bet optimizer. Selects the best subset of opportunities that
  fits within the operator's max-total-exposure budget. (VERIFIED via
  `portfolio_optimizer.py:100-360`.)
- `core/portfolio_mark_to_market.py::compute_mark_to_market_exposure` —
  mark-to-market exposure for the risk gate's MTM cap.
- `core/correlation.py` — Pearson correlation matrix between held
  positions.

### 4.8 Execution Layer

- `execution/smart_router.py` — order routing logic (split across
  venues / slicing).
- `execution/advanced_router.py` — advanced routing (TWAP / VWAP /
  iceberg).
- `paper/simulator.py::paper_sim` — paper-trading fill simulator
  with slippage model (tick + size-impact). (VERIFIED via
  `simulator.py:1-80`.)
- `core/clob_client.py::clob_client` — live CLOB REST order client.
- `core/order_state_machine.py` — order status transitions
  (`OPEN → FILLED | CANCELLED | PARTIALLY_FILLED`).
- `core/execution_quality.py` — per-fill slippage, latency, realized
  edge ledger. (VERIFIED via `execution_quality.py:1-80`.)

### 4.9 Auditability / Ledger Layer

- `core/decision_ledger.py::decision_ledger` — 6-stage unified ledger
  keyed by `decision_id`. (VERIFIED via `decision_ledger.py:111-116`.)
- `core/audit_logger.py::audit_logger` — durable category-indexed audit
  trail with idempotency keys.
- `core/immutable_audit.py::immutable_audit` — hash-chained trail for
  high-sensitivity control events (kill switch, live-trade enable, config
  changes). (VERIFIED via `immutable_audit.py:1-120`.)
- `core/shadow_trading.py::shadow_trades` — counterfactual trade journal
  for risk-rejected orders.
- `core/closed_positions.py::closed_positions` — round-trip P&L journal
  with `decision_id` cross-ref.
- `core/attribution.py` — 7-dimensional P&L attribution (strategy,
  confidence bucket, edge bucket, probability band, liquidity level,
  holding period, trade direction). (VERIFIED via `attribution.py:1-60`.)

### 4.10 Observability Layer

- `core/observability.py` — 6-category SQLite metrics store
  (`data_source` / `bot` / `strategy` / `execution` / `ml` / `system`).
- `core/observability_collector.py` — 30-second background auto-collector
  that pulls stats from every subsystem.
- `core/alerting.py::alert_engine` — 7 default rules across 4
  categories (`risk` / `ml` / `system` / `data`).
- `core/prometheus_metrics.py` — `/metrics` endpoint exposing counters,
  gauges, histograms.
- `core/profiling.py::profiler` — in-memory p50/p95/p99 per-endpoint
  latency stats.
- `core/circuit_breaker.py` — 3 pre-configured breakers (`clob_api`,
  `gamma_api`, `polymarket_ws`).

---

## 5. Data Flow

### 5.1 The §50 trace (15 stages)

Below, each stage is mapped to its concrete code path, with the
**disconnection points** explicitly flagged.

```
DATA INGESTION ─────────────────────────────────────────────────────────
  Source: clob.polymarket.com (order books) + gamma-api.polymarket.com
          (market catalog) + fundamental news feeds
  Module: core.book_poller.BookPoller._poll_loop()
          core.market_discovery.UniversalMarketDiscoveryEngine._discovery_loop()
          core.fundamental_ingest.GlobalFundamentalIngestionEngine
  Sink:   store.order_books (in-memory dict, atomic JSON snapshot)
          market_discovery.catalog (in-memory dict, 3-min refresh)
          raw_vault (Postgres raw.raw_observation — optional)
  Status: VERIFIED — multi-source ingestion wired.

      │
      ▼  ⚠ GAP #1: no MARKET_SNAPSHOT record is persisted; the book
         poller updates `store.order_books[token_id]` in place, with
         only `book.updated_at` carrying freshness. There is no
         `market_snapshot_id` to reference from downstream stages.

NORMALIZATION ──────────────────────────────────────────────────────────
  Module: implicit in book_poller (PriceLevel / OrderBook dataclasses)
          core.sanitizer.py (data sanitisation — UNVERIFIED scope)
  Status: PARTIAL — book data is structurally normalised (bids/asks
          as `list[PriceLevel]`) but no separate normalization stage
          exists. The spec stage is implicit.

STORAGE ────────────────────────────────────────────────────────────────
  Module: core.data_store.store (in-memory + JSON snapshot)
          core.market_db (SQLite market metadata)
          core.timescale_db (optional Postgres for raw observations)
  Status: VERIFIED — three stores, fragmented.

      │
      ▼  ⚠ GAP #2: features are computed by reading `store.order_books`
         + `market_discovery.catalog` + `ml.vector_store` ad-hoc inside
         `signal_trader._ml_signal`. No durable snapshot of the exact
         inputs the model saw is referenced from the prediction.

FEATURE ENGINEERING ────────────────────────────────────────────────────
  Module: ml.features.extract_features(token_id, book, ...)
  Sink:   ml.feature_store.feature_values (per-prediction rows in
          SQLite — feature_store.db)
  Status: VERIFIED — feature store exists. Per-prediction rows are
          written from `ml.model.predict()` (W16-2 wiring).

      │
      ▼  ⚠ GAP #3: the `feature_values` row is keyed by `(token_id,
         feature_name, timestamp)` + an optional `prediction_id`. But
         the `prediction_id` is NOT the `decision_id` from the decision
         ledger — they are two separate identifier spaces. A prediction
         can be linked to a decision ONLY by joining on
         `(token_id, timestamp)` proximity, which is fragile.

AI/ML ──────────────────────────────────────────────────────────────────
  Module: ml.model.MarketMLModel.predict()
  Output: p_yes (float), confidence (float), feature_contributions (SHAP)
  Ledger: decision_ledger.record(decision_id, STAGE_PREDICTION, ...)
          with `model_version` auto-stamped (V14).
  Status: VERIFIED — predictions emit to the ledger.

STRATEGIES ─────────────────────────────────────────────────────────────
  Module: strategies.signal_trader.SignalTraderStrategy._scan_markets()
          strategies.base.BaseStrategy.submit_order()
  Ledger: decision_ledger.record(decision_id, STAGE_SIGNAL, ...)
  Status: VERIFIED — signals emit to the ledger with `reason` /
          `direction` / `confidence` / `predicted_edge`.

RISK ───────────────────────────────────────────────────────────────────
  Module: risk.manager.InstitutionalRiskEngine.check_order()
  Ledger: on approval → STAGE_RISK_APPROVED
          on rejection → STAGE_RISK_REJECTED (via record_rejection)
          rejection table: decision_rejections (fast filtered view)
  Status: VERIFIED — both branches emit to the ledger.

PORTFOLIO ──────────────────────────────────────────────────────────────
  Module: core.capital_allocator.allocate_size() (T9 safety-gated)
          core.portfolio_optimizer.optimize() (T5 — multi-bet)
  Ledger: NONE — no PORTFOLIO stage in the decision ledger.
  Status: ⚠ DISCONNECTED #4 — the capital_allocator's output (suggested
          size) is passed as `args.size` to `submit_order`, but the
          sizing computation itself is NOT recorded as a separate
          ledger stage. The RISK_APPROVED row carries the final size,
          but the multiplier breakdown (edge_mult × confidence_mult ×
          calibration_mult × ...) is not persisted per-decision.

EXECUTION ─────────────────────────────────────────────────────────────
  Module: paper.simulator.PaperSimulator.create_order()
          core.clob_client.ClobClient.create_order()
          execution.smart_router / advanced_router (routing —
          UNVERIFIED whether used in the live path)
  Status: VERIFIED for paper path. Live path delegates directly to
          `clob_client.create_order` — the smart_router / advanced_router
          are present in the codebase but their wiring into the live
          `submit_order` path is UNVERIFIED.

ORDERS ─────────────────────────────────────────────────────────────────
  Module: core.data_store.Order dataclass + store.add_order()
          core.order_state_machine (status transitions)
  Ledger: decision_ledger.record(decision_id, STAGE_ORDER, ...)
          with `order_id`, `side`, `price`, `size`.
  Audit:  audit_logger.log_event(category='order', ...)
  Status: VERIFIED.

FILLS ─────────────────────────────────────────────────────────────────
  Module: paper.simulator._execute_fill()
  Ledger: decision_ledger.record(decision_id, STAGE_FILL, ...) with `pnl`.
  Quality: execution_quality.record_execution() — slippage, latency,
           realized_edge.
  Status: VERIFIED.

POSITIONS ──────────────────────────────────────────────────────────────
  Module: core.data_store.Position + store.positions (dict)
          core.position_manager (lifecycle)
  Ledger: NONE — no POSITION stage in the decision ledger.
  Status: ⚠ DISCONNECTED #5 — positions are tracked in-memory
          (`store.positions`) and on close in `closed_positions.db`,
          but the live position state is NOT linked by `decision_id`
          to the originating order chain. The closed_positions row has
          an OPTIONAL `decision_id` foreign key, but it is only
          populated when the position is closed; open positions cannot
          be traced back to their originating decision.

P&L ───────────────────────────────────────────────────────────────────
  Module: core.portfolio.compute_exposure() + compute_mark_to_market_exposure()
          core.closed_positions (realised P&L on close)
  Ledger: NONE — the FILL stage carries `pnl` per-fill but there is no
          POSITION-level P&L roll-up stage in the ledger.
  Status: ⚠ DISCONNECTED #6 — realised P&L is reconstructable from the
          `closed_positions` table joined to `decision_ledger` by
          `decision_id`, but unrealised / mark-to-market P&L is only
          in-memory and not persisted with correlation to the decision
          chain.

PERFORMANCE ────────────────────────────────────────────────────────────
  Module: core.attribution.get_full_attribution() — 7-dim roll-up
          backtesting.report.generate_report() — VaR / CVaR / Sharpe
  Status: VERIFIED — attribution is computed on demand from
          `closed_positions`. Performance reports are generated on
          demand from backtest runs.

LEARNING ───────────────────────────────────────────────────────────────
  Module: ml.model.MarketMLModel.partial_fit() (SGD only)
          core.label_backfill (resolves market outcomes retroactively)
          ml.training_orchestrator (periodic retraining)
  Status: PARTIAL — only the SGD base learner updates on live fills.
          RF / GB / LightGBM are not retrained online; they require
          explicit `fit_initial` from `training_orchestrator`.
```

### 5.2 Summary of disconnections

| # | Gap | Severity |
|---|---|---|
| 1 | No `MARKET_SNAPSHOT` record persists; book updates are in-place. | Medium — freshness is observable via `book.updated_at` but no historical snapshot is queryable by ID. |
| 2 | No `INTELLIGENCE_SNAPSHOT` record; intelligence is composed ad-hoc inside the strategy. | High — the spec's §51 stage is absent. |
| 3 | `feature_values.prediction_id` and `decision_ledger.decision_id` are separate identifier spaces. | High — features cannot be reliably linked to the decision that consumed them. |
| 4 | No `PORTFOLIO` ledger stage; sizing multiplier breakdown not persisted per-decision. | Medium — sizing is recoverable from the constants but the per-decision multipliers are not. |
| 5 | No `POSITION` ledger stage; open positions not linked to `decision_id`. | High — open-position attribution is broken. |
| 6 | No `P&L` ledger stage beyond per-fill `pnl`; unrealised P&L not persisted with correlation. | Medium — realised P&L is recoverable from closed_positions; unrealised is in-memory only. |

---

## 6. Execution Flow

The end-to-end execution flow for a single BUY signal (paper mode):

```
1. book_poller._poll_loop()
   ├── GET https://clob.polymarket.com/book?token_id=...
   └── store.update_order_book(token_id, bids, asks)
       └── book.updated_at = time.time()

2. signal_trader._scan_markets()    [every 15s]
   ├── iterate market_discovery.catalog
   ├── extract_features(token_id, book, ...)
   ├── ml_model.predict(features) → (p_yes, confidence)
   ├── decision_id = decision_ledger.new_decision_id()
   ├── await decision_ledger.record(decision_id, STAGE_PREDICTION, ...)
   │       (auto-stamps model_version from model_registry)
   ├── allocate_capital(edge, confidence, drawdown, exposure, liquidity)
   │       → suggested_size USD
   ├── build MarketSignal(decision_id=..., size_usdc=suggested_size, ...)
   ├── await decision_ledger.record(decision_id, STAGE_SIGNAL, ...)
   └── await self.submit_order(OrderArgs(...), decision_id=decision_id)

3. BaseStrategy.submit_order(args, decision_id)
   ├── build provisional Order(...)
   ├── risk_manager.check_order(provisional) → (allowed, reason)
   │   ├── if rejected:
   │   │   ├── decision_ledger.record(decision_id, STAGE_RISK_REJECTED, ...)
   │   │   ├── decision_ledger.record_rejection(...) [fast-reject view]
   │   │   ├── shadow_trading.record_shadow_trade(...) [counterfactual]
   │   │   └── return None
   │   └── if approved:
   │       └── decision_ledger.record(decision_id, STAGE_RISK_APPROVED, ...)
   ├── if paper mode:
   │   └── paper_sim.create_order(args, strategy, decision_id)
   │       ├── store.add_order(order)
   │       └── [background fill loop matches the order]
   └── if live mode:
       └── clob_client.create_order(args) → resp
           ├── order_id = resp["orderID"]
           └── store.add_order(Order(order_id, ...))

4. paper_sim._fill_loop()    [background, periodic]
   ├── for each open order in store.open_orders:
   │   ├── check if order is marketable against current book
   │   ├── compute slippage (tick + size-impact model)
   │   ├── execute_fill(order, fill_price)
   │   ├── store.add_trade(Trade(...))
   │   ├── update Position (yes_shares / no_shares / avg_entry_price)
   │   ├── update store.paper_balance, store.daily_pnl, store.peak_equity
   │   ├── decision_ledger.record(decision_id, STAGE_FILL, pnl=...)
   │   ├── execution_quality.record_execution(order_id, decision_id, ...)
   │   └── audit_logger.log_event(category='fill', ...)
   └── risk_manager.report_trade_pnl(strategy, pnl)
       └── if abs(pnl) > PER_TRADE_MAX_LOSS:
           └── _strategy_cooldowns[strategy] = monotonic + 300s

5. position_manager (background)
   ├── monitor open positions
   └── on close:
       ├── closed_positions.record_closed_position(
       │       token_id, strategy, entry_price, exit_price, shares,
       │       pnl, holding_seconds, model_version, decision_id, ...)
       ├── audit_logger.log_event(category='position_close', ...)
       └── immutable_audit.log('position_close', {decision_id, pnl, ...})

6. observability_collector    [every 30s]
   ├── _collect_data_source_metrics() → observability.record_metric(...)
   ├── _collect_execution_metrics()
   ├── _collect_ml_metrics()
   ├── _collect_system_metrics() (psutil)
   └── _collect_bot_metrics() (heartbeat)

7. alert_engine.evaluate(metrics)    [triggered on demand]
   └── for each rule:
       └── if condition(metrics): fire alert + persist to alerts.db
```

### 6.1 §51 Unified Decision Ledger Chain

The spec asks for 12 stages:

```
MARKET → MARKET SNAPSHOT → INTELLIGENCE SNAPSHOT → FEATURE SNAPSHOT →
MODEL PREDICTION → STRATEGY SIGNAL → RISK DECISION → ORDER → FILL →
POSITION → OUTCOME → P&L
```

The codebase implements 6:

```
MODEL PREDICTION → STRATEGY SIGNAL → RISK APPROVED|REJECTED →
ORDER → FILL
```

i.e. the codebase covers stages 5–9 of the spec's 12-stage chain. The
first 4 (pre-PREDICTION) and the last 3 (post-FILL) are not in the
ledger. The closed_positions table carries an optional `decision_id`
foreign key, which closes the loop partially on close (the OUTCOME
stage is implicit in the close). The pre-PREDICTION stages are not
linked at all.

(VERIFIED via `core/decision_ledger.py:108-126`:

```
STAGE_PREDICTION = "PREDICTION"
STAGE_SIGNAL = "SIGNAL"
STAGE_RISK_APPROVED = "RISK_APPROVED"
STAGE_RISK_REJECTED = "RISK_REJECTED"
STAGE_ORDER = "ORDER"
STAGE_FILL = "FILL"
```

That's the complete `STAGE_*` constant set — no `STAGE_MARKET`, no
`STAGE_INTELLIGENCE`, no `STAGE_FEATURE`, no `STAGE_POSITION`, no
`STAGE_OUTCOME`, no `STAGE_PNL`.)

---

## 7. Feature Inventory

### 7.1 Pipeline-stages inventory

| Stage | Present? | Module | Ledger stage? |
|---|---|---|---|
| DATA INGESTION | Yes | `book_poller`, `gamma_client`, `fundamental_ingest` | No |
| NORMALIZATION | Partial (implicit) | `book_poller` (PriceLevel dataclass) | No |
| STORAGE | Yes | `data_store`, `market_db`, `timescale_db` | No |
| FEATURE ENGINEERING | Yes | `ml.features`, `ml.feature_store` | No (separate `prediction_id` space) |
| AI/ML | Yes | `ml.model` (4-model ensemble) | `PREDICTION` |
| STRATEGIES | Yes | `strategies.signal_trader`, `market_maker`, `arb_scanner` | `SIGNAL` |
| RISK | Yes | `risk.manager` (13+ gates) | `RISK_APPROVED` / `RISK_REJECTED` |
| PORTFOLIO | Yes (modules exist) | `portfolio`, `portfolio_optimizer`, `capital_allocator` | No (no `PORTFOLIO` stage) |
| EXECUTION | Yes | `paper.simulator`, `clob_client`, `execution.smart_router` | No (implicit in `ORDER`) |
| ORDERS | Yes | `data_store.Order`, `order_state_machine` | `ORDER` |
| FILLS | Yes | `paper.simulator._execute_fill`, `execution_quality` | `FILL` |
| POSITIONS | Yes (in-memory + closed journal) | `data_store.Position`, `position_manager`, `closed_positions` | No |
| P&L | Yes (computed) | `portfolio.compute_exposure`, `portfolio_mark_to_market` | No (per-fill `pnl` only) |
| PERFORMANCE | Yes | `attribution` (7-dim), `backtesting.report` (VaR/CVaR) | No |
| LEARNING | Partial (SGD online only) | `model.partial_fit`, `label_backfill`, `training_orchestrator` | No |

### 7.2 Cross-system identifier inventory

The spec §55 mandates correlation identifiers: `decision_id`,
`signal_id`, `order_id`, `fill_id`, `position_id`, `strategy_id`,
`model_version`.

| Identifier | Present? | Where? | Linked to `decision_id`? |
|---|---|---|---|
| `decision_id` | Yes | `decision_ledger.decision_events.decision_id` (PK) | — (this IS the correlation key) |
| `signal_id` | No (uses `decision_id`) | `MarketSignal.decision_id` field | Yes |
| `order_id` | Yes | `data_store.Order.order_id` + `execution_quality.order_id` | Yes (passed to `submit_order`) |
| `fill_id` | Partial — `Trade.trade_id` exists but the FILL ledger row uses `decision_id` not a separate `fill_id` | `data_store.Trade` | Yes (via decision_id) |
| `position_id` | Yes on close (`closed_positions.position_id`) but **not** on open positions | `closed_positions.position_id` | Yes (optional `decision_id` FK on close) |
| `strategy_id` | Strategy `name` (string, e.g. "signal_trader") — not a UUID | `Order.strategy`, `MarketSignal.source` | No (loose string match) |
| `model_version` | Yes — auto-stamped on `PREDICTION` events (V14) | `decision_events.data_json.model_version` | Yes |

(VERIFIED via `decision_ledger.py:265-273`, `signal_trader.py:59-62`,
`base.py:60-70`, `closed_positions.py:18-38`, `execution_quality.py:39-45`.)

### 7.3 Target §79 layer inventory

| §79 Layer | Module(s) | Maturity |
|---|---|---|
| DATA LAYER | `data_store`, `market_db`, `timescale_db`, `ingestion/raw_vault`, `ingestion/source_registry` | High |
| FEATURE/INTEL LAYER | `ml/features`, `ml/feature_store`, `core/sentiment`, `core/deep_analysis` | Medium (no snapshot store) |
| AI/ML | `ml/model`, `ml/calibration`, `ml/drift_detector`, `ml/explainability`, `ml/ensemble_meta_learner`, `ml/training_orchestrator`, `ml/ab_testing`, `ml/shadow_inference` | High |
| STRATEGY ENGINE | `strategies/base`, `strategies/signal_trader`, `strategies/market_maker`, `strategies/arb_scanner`, `strategies/registry` | High |
| RISK ENGINE | `risk/manager`, `risk/routes` | High |
| PORTFOLIO ALLOCATION | `core/portfolio`, `core/portfolio_optimizer`, `core/capital_allocator`, `core/correlation`, `core/portfolio_mark_to_market`, `core/stress_test` | High (modules exist) but **disconnected from live path** |
| EXECUTION ENGINE | `paper/simulator`, `core/clob_client`, `execution/smart_router`, `execution/advanced_router`, `core/order_state_machine`, `core/execution_quality` | Medium (smart_router wiring UNVERIFIED) |
| EXCHANGE/MARKET | `https://clob.polymarket.com`, `https://gamma-api.polymarket.com` | High |

---

## 8. What Works

1. **The 6-stage decision ledger is wired end-to-end.** Every order that
   reaches the `submit_order` path emits PREDICTION → SIGNAL →
   RISK_APPROVED/REJECTED → ORDER → FILL keyed by a UUID `decision_id`.
   (VERIFIED via `decision_ledger.py:111-116`, `signal_trader.py`,
   `base.py:60-148`.)
2. **Rejection paths are durable.** Every risk-rejected order is
   recorded both in the main `decision_events` chain (as
   `RISK_REJECTED`) and in the fast-filtered `decision_rejections`
   table, with a structured `reason` vocabulary
   (`low_confidence` / `wide_spread` / `neutral_zone` /
   `insufficient_kelly_edge`). (VERIFIED via
   `decision_ledger.py:122-126, 308-374`.)
3. **Counterfactual shadow trading.** Risk-rejected orders are also
   recorded in `shadow_trades.db` so the operator can later benchmark
   "what would have happened if we'd taken the trade". (VERIFIED via
   `risk/manager.py:142-162` + `core/shadow_trading.py:1-50`.)
4. **Model version stamping.** Every PREDICTION event auto-stamps
   `model_version` from `ml.model_registry.active_version`. (VERIFIED
   via `decision_ledger.py:265-273, 690-729`.)
5. **Three independent audit trails coexist without schema contention.**
   `audit_trail.db` (category-indexed durable log), `immutable_audit.db`
   (hash-chained), and `decision_ledger.db` (correlation-keyed stage
   chain) — each isolated in its own SQLite file. (VERIFIED via
   `immutable_audit.py:22-35`, `decision_ledger.py:20-23`.)
6. **Three pre-configured circuit breakers** for the external APIs
   (`clob_api`, `gamma_api`, `polymarket_ws`) — dual sync/async
   decorator support. (VERIFIED via `circuit_breaker.py:209-227`.)
7. **Hash-chained immutable audit trail for control events** — kill
   switch activation, live-trade enable, config changes, position
   close. Tamper-evident via SHA-256 chain. (VERIFIED via
   `immutable_audit.py:1-120`.)
8. **The 4-model ML ensemble** with isotonic calibration + level-2
   stacking meta-learner + adaptive Brier-weight fallback + drift
   detector. (VERIFIED via `ml/model.py:1-60`.)
9. **The §50 trace runs at runtime** — book_poller, market_discovery,
   strategy runner, observability collector all run concurrently in one
   process. (VERIFIED via `main.py` + `api/server.py` lifespan wiring.)
10. **The system CAN answer §80 partially.** Given a `decision_id` from
    the FILL stage, the operator can issue
    `GET /api/decision/{token_id}` (which returns the chain for that
    token) or `GET /api/v2/decisions/recent` (paginated recent
    decisions). The chain returns the 5-stage PREDICTION → SIGNAL →
    RISK_* → ORDER → FILL with `pnl` per fill. (VERIFIED via
    `decision_ledger.py:630-677` + `api/server.py:4000-4040`.)

---

## 9. What Does Not Work

### 9.1 The §51 unified ledger chain is shorter than spec

The spec wants 12 stages; the codebase implements 6. The 6 missing
stages are:

- `MARKET` — the market metadata record (slug, question, outcomes,
  end_date). This exists in `market_discovery.catalog` and
  `market.db` but is not linked by `decision_id`.
- `MARKET SNAPSHOT` — the order-book state at prediction time. The
  book is updated in-place in `store.order_books`; no historical
  snapshot is queryable by ID.
- `INTELLIGENCE SNAPSHOT` — the composed intelligence (book + news +
  sentiment + macro) at prediction time. Not persisted.
- `FEATURE SNAPSHOT` — the feature vector fed to the model. Persisted
  in `feature_store.db` but keyed by `prediction_id`, not `decision_id`.
- `POSITION` — the open position record. Tracked in-memory only; not
  linked by `decision_id` until close.
- `OUTCOME` — the resolved market outcome. Tracked in
  `core.label_backfill` but not linked by `decision_id` to the
  originating prediction.
- `P&L` — the position-level P&L roll-up. Per-fill `pnl` is in the
  FILL stage; position-level realised P&L is in `closed_positions.db`
  (linked by `decision_id`); unrealised P&L is in-memory only.

**Impact:** The system CANNOT fully answer "Why did the bot make this
trade?" for any of the 6 missing stages. The answer is partial —
prediction + signal + risk + order + fill are reconstructable; the
inputs (market/intelligence/feature) and outputs (position/outcome/P&L)
are not.

### 9.2 The PORTFOLIO layer is disconnected from the live trade path

The portfolio optimizer (`core/portfolio_optimizer.py`) and capital
allocator (`core/capital_allocator.py`) both exist, but the live trade
path only uses the **T9 single-trade** allocator
(`allocate_size(edge, confidence, drawdown, exposure, liquidity)`). The
multi-bet Kelly optimizer is exposed via `POST /api/portfolio/optimize`
and `GET /api/portfolio/rebalance` — it's an operator-facing what-if
tool, not part of the hot scan loop.

**Impact:** The bot does NOT consider portfolio-level constraints
(diversification ratio, total exposure budget, correlation) when
sizing individual trades. Each signal is sized in isolation. (LIKELY
— the `signal_trader._scan_markets` loop calls `allocate_size` per
token, never `portfolio_optimizer.optimize` over the candidate set.)

### 9.3 Open positions are not linked by `decision_id`

The `closed_positions` table has an optional `decision_id` foreign key
(populated when the position is closed), but `store.positions` (the
in-memory open-positions dict) is keyed by `token_id`, not by
`decision_id`. (VERIFIED via `data_store.py:1-80` + `closed_positions.py:18-38`.)

**Impact:** "Which decision opened this position?" is not answerable
for an open position. Only after close does the chain complete.

### 9.4 Feature store's `prediction_id` is a separate identifier space

The `feature_values` table records one row per (token_id, feature_name,
timestamp, prediction_id). But the `prediction_id` is generated by
`ml.model.predict()`, not by `decision_ledger.new_decision_id()`. The
two IDs are not joined. (VERIFIED via `feature_store.py:1-100`.)

**Impact:** "Which features were fed to the model for this trade?" is
answerable only by joining on `(token_id, timestamp)` proximity —
fragile, especially under concurrent strategy scans.

### 9.5 Live path bypasses the smart router

The `submit_order` path in `strategies/base.py` calls either
`paper_sim.create_order` or `clob_client.create_order` directly. The
`execution/smart_router.py` and `execution/advanced_router.py` modules
exist but are not invoked from `submit_order`. (LIKELY — no `import
smart_router` in `strategies/base.py`.)

### 9.6 Learning is partial — only SGD updates online

The 4-model ensemble has only one online learner (SGD). The RF + GB +
LightGBM members are fit once at boot (or via
`training_orchestrator.fit_initial`) and do not update on live fills.
(VERIFIED via `ml/model.py:1-60` — `partial_fit` only available on the
SGD member.)

**Impact:** Model drift can only be detected, not corrected online.
The `dynamic_model_risk_multiplier` downgrades capacity to 60% / 30%
when drift is detected, but the model itself waits for the next
explicit retraining cycle.

### 9.7 No POSITION stage in the ledger means open-position attribution is broken

The 7-dimensional attribution in `core/attribution.py` operates on the
`closed_positions` table — i.e. only realised trades. Open positions
cannot be attributed to a strategy, confidence bucket, edge bucket,
etc. until they close.

---

## 10. Missing Features

1. **`MARKET_SNAPSHOT` ledger stage** — a snapshot of the order book
   (top-N bids + asks + mid + spread + updated_at) at prediction time,
   with a stable `market_snapshot_id` linked from the PREDICTION event.
2. **`INTELLIGENCE_SNAPSHOT` ledger stage** — composed intelligence
   from all sources (book + news + sentiment + macro + fundamental)
   at prediction time, with a stable `intelligence_snapshot_id`.
3. **`FEATURE_SNAPSHOT` ledger stage** — the feature vector fed to the
   model, persisted under the `decision_id` (not a separate
   `prediction_id`). This requires unifying the feature_store's
   identifier space with the decision_ledger's.
4. **`PORTFOLIO` ledger stage** — the capital allocator's multiplier
   breakdown (edge_mult × confidence_mult × calibration_mult × ...),
   persisted per-decision. Today the multiplier breakdown is only
   surfaced via the HTTP endpoint `GET /api/capital/allocation`, not
   per-trade.
5. **`POSITION` ledger stage** — a POSITION event emitted when the
   position opens (linked from the originating ORDER), updated on
   partial fills, and closed (linked from the closing ORDER).
6. **`OUTCOME` ledger stage** — the resolved market outcome linked
   back to every prediction that touched the market.
7. **`PNL` ledger stage** — a position-level P&L roll-up persisted at
   close (and optionally periodically for unrealised).
8. **Portfolio optimizer wired into the live trade path** — today the
   hot scan loop sizes each signal in isolation; the optimizer is only
   used for operator what-if analysis.
9. **Smart router wiring** — `execution/smart_router.py` /
   `execution/advanced_router.py` are present but not invoked from
   `submit_order`. Live trades go straight to `clob_client`.
10. **Online retraining for RF / GB / LightGBM** — only SGD updates
    online. The other three members require explicit
    `training_orchestrator.fit_initial`.
11. **A unified `intelligence_id` / `market_snapshot_id` /
    `feature_snapshot_id` vocabulary** joining the pre-PREDICTION
    stores (market_intelligence.db, feature_store.db) to the
    decision_ledger's `decision_id`.

---

## 11. Bugs

1. **`feature_store.prediction_id` ↔ `decision_ledger.decision_id`
   identifier-space mismatch.** Two separate UUIDs are minted for the
   same prediction. The feature-store row cannot be reliably joined to
   the decision-ledger row without timestamp proximity heuristics.
   (VERIFIED — `decision_ledger.new_decision_id()` returns
   `f"dec-{uuid.uuid4().hex}"`; `feature_store`'s `prediction_id` is
   generated separately in `ml/model.py`.)
2. **`market_discovery.catalog` records don't preserve the raw
   `tokens` array** (referenced in `signal_trader.py:114-120` comment).
   Calling `gamma_client.extract_token_ids(mkt)` on a normalised record
   returns `[]`, silently no-opping the scan. The strategy works around
   this by iterating `(token_id, market_dict)` tuples directly, but
   this is a latent footgun for any future caller.
3. **MTM gate fail-open.** The mark-to-market exposure gate at
   `risk/manager.py:308-315` wraps the call in a bare `except: pass`
   — if `compute_mark_to_market_exposure` raises, the gate is skipped
   (fail-open) rather than fail-closed. The code documents this as
   "section 5 still enforces the cost-basis $25 cap" but a runaway MTM
   could silently widen true risk past the ceiling without tripping
   either gate. (VERIFIED via `manager.py:308-315`.)
4. **`signal_id` is not a distinct identifier** — the spec §55 lists
   `signal_id` as a correlation identifier, but the codebase uses
   `decision_id` for both the prediction and the signal. This is
   arguably correct (one decision = one signal) but diverges from the
   spec vocabulary. (VERIFIED — `MarketSignal.decision_id` field is
   the only correlation key.)
5. **`PER_TRADE_MAX_LOSS` ($0.50) is too tight** for the $3 max
   position size — a single share at $0.50 entry that resolves to $0
   loses $0.50, triggering the strategy cooldown even on a normal
   bad-bet outcome. (LIKELY — the threshold is hardcoded in
   `risk/manager.py:64`.)

---

## 12. Technical Debt

1. **Singleton pattern across 60+ modules** — every `core/*.py` module
   exposes a module-level singleton. There is no way to run two bot
   instances in the same Python process. (VERIFIED — pattern repeated
   across the package.)
2. **`api/server.py` is ~5100 lines** with 80+ routes mixed between
   inline `@app.get`/`@app.post` decorators and 30+
   `register_routes(app)` blocks appended at the file's tail. The file
   has grown beyond reasonable single-file readability.
3. **12 separate SQLite databases** under `/app/data/` — each feature
   module owns its own schema, its own indexes, its own
   `_init_db()` method. There is no unified migration runner across
   them (`core/db/migration_runner.py` exists but is scoped to the
   `001_initial_schema.sql` + `001_initial_enterprise_schemas.sql`
   migrations only).
4. **Inconsistent async conventions.** `decision_ledger.record` is
   async; `audit_logger.log_event` is async; `immutable_audit.log` is
   sync; `feature_store.record_values` is sync. Callers must remember
   which is which.
5. **`risk/manager.py:_check_order_impl` is a 200-line method** with
   13 sequential gates. The gates are individually tested but the
   interaction between them (e.g. does the MTM gate fire before or
   after the cash-reserve gate?) is hard to reason about.
6. **The `market_intelligence.db` file exists** in `data/` but its
   owner module is UNVERIFIED — no `core/market_intelligence.py` was
   found in this trace. (NOT FOUND — likely an orphan from a prior
   task or owned by an unscoped module.)
7. **`signal_trader._ml_signal` is a 200+ line method** doing
   feature extraction + model predict + sizing + decision-ledger write
   + signal build in one body. The sizing step has been extracted to
   `capital_allocator.allocate_size` but the rest is monolithic.

---

## 13. Data Problems

1. **`store.order_books` is updated in-place.** There is no historical
   order-book state — only the current snapshot. The
   `book.updated_at` timestamp is the only freshness signal. The
   spec's `MARKET_SNAPSHOT` stage is therefore unimplementable without
   a new persistence layer.
2. **No raw-payload retention on the paper path.** The `raw_vault`
   exists but is gated on `timescale_db._is_postgres`. When Postgres
   is unavailable (the default local-dev path), raw observations are
   silently dropped. (VERIFIED via `raw_vault.py:47-60`.)
3. **Feature drift is detected on the model's prediction distribution
   (PSI), not on the input feature distribution.** The
   `feature_store.detect_feature_drift` exists (windowed mean-shift
   test) but is not wired into the alert engine. (LIKELY — no
   `feature_drift` alert rule in `alerting._default_rules`.)
4. **`market_discovery.catalog` is in-memory only** — refreshed every
   3 minutes. A process restart loses the catalog until the next sync.
   (VERIFIED via `market_discovery.py:30-60`.)
5. **The `model.pkl` is loaded at boot** from `MODEL_PATH`. There is
   no versioned rollback — a bad retraining cycle overwrites the file
   and the previous version is lost unless `model_registry` has been
   configured to retain old artifacts. (LIKELY — `model_registry.json`
   tracks metadata but the pickle file itself is overwritten in
   place.)
6. **The label_backfill process resolves outcomes retroactively** but
   the resolution is not linked back to the originating PREDICTION
   event in the decision ledger. (VERIFIED — no `OUTCOME` stage in
   `STAGE_*` constants.)

---

## 14. Performance Problems

1. **`signal_trader._scan_markets` iterates the entire
   `market_discovery.catalog`** (800+ markets) every 15 seconds. The
   feature extraction + model predict runs for every token, even when
   no order book has changed since the last scan. (VERIFIED via
   `signal_trader.py:108-200`.)
2. **`observability_collector._collect_data_source_metrics` iterates
   `store.order_books.values()` under the store's lock** every 30
   seconds. With 800+ tracked tokens, this holds the lock for
   non-trivial time. (VERIFIED via `observability_collector.py:147-158`.)
3. **Each `decision_ledger.record` call opens a fresh
   `sqlite3.connect(db_path)`** — no connection reuse, no prepared
   statement cache. The async-via-`asyncio.to_thread` pattern means
   every ledger write is a thread-pool dispatch + open + insert +
   commit + close. (VERIFIED via `decision_ledger.py:277-306`.)
4. **The `book_poller` polls every token sequentially** in a single
   loop. With 800+ tokens at 100ms per poll, a full sweep takes 80+
   seconds — far slower than the 2-second UI polling cadence expects.
   (LIKELY — no concurrency pattern documented in `book_poller`.)
5. **`compute_mark_to_market_exposure` is called on every `check_order`
   invocation** — a tight inner loop calling portfolio-mark-to-market
   for every candidate trade. (VERIFIED via `risk/manager.py:308-315`.)

---

## 15. Reliability Problems

1. **All SQLite writes are fire-and-forget from the caller's
   perspective.** Every persistence method swallows its own errors
   (logged at `error` level, return `[]` / `0` / `None`). This is
   documented as a deliberate design choice ("a ledger hiccup can never
   break the trading pipeline") but means **silent data loss is
   possible** — if the disk fills, every subsystem continues operating
   against stale data. (VERIFIED — pattern across `decision_ledger`,
   `audit_logger`, `observability`, `alerting`, `closed_positions`,
   `shadow_trading`.)
2. **The hash-chained immutable audit trail's `_load_last_entry`** runs
   at construction time. If the db file is corrupted, the chain
   restarts from genesis and prior tampering is undetectable.
   (VERIFIED via `immutable_audit.py:74-78, 120-...`.)
3. **Circuit breaker recovery is time-based only.** A `clob_api`
   breaker that trips at 14:00:00 will half-open at 14:00:30 regardless
   of whether the API is actually back. (VERIFIED via
   `circuit_breaker.py:79-101`.)
4. **No distributed lock across processes.** If two bot processes are
   accidentally started against the same `/app/data/` directory, both
   will write to the same SQLite files. SQLite's WAL mode mitigates
   corruption but both processes will see inconsistent snapshots.
5. **`store_state.json` is the only persistence of open orders /
   positions / paper balance.** If the JSON is corrupted (e.g. process
   killed mid-write), the entire state is lost. The atomic-write
   pattern in `data_store.py` mitigates this but is not foolproof
   against power loss. (VERIFIED via `data_store.py:18-24`.)
6. **The `feature_store.db` and `decision_ledger.db` are separate
   files** but both are SQLite — under heavy concurrent writes the
   WAL journal can grow unbounded, degrading read latency on the
   dashboard. (LIKELY — no `VACUUM` or `PRAGMA wal_checkpoint`
   scheduler observed.)

---

## 16. Security Problems

1. **The `audit_logger` writes raw payload strings into the `details`
   column.** If a strategy or external caller passes PII or secrets in
   a payload, they will be persisted to disk. No redaction layer.
   (VERIFIED via `audit_logger.py:53-85`.)
2. **The immutable audit chain uses SHA-256 of the previous entry** but
   the chain itself is not signed by any key. Anyone with write access
   to `immutable_audit.db` can re-write the entire chain (computing
   fresh hashes) without detection — the chain only detects tampering
   if the attacker doesn't bother to recompute hashes. (VERIFIED via
   `immutable_audit.py:1-120`.)
3. **API authentication is enforced by `enforce_api_auth` bearer-token
   middleware** in `api/server.py` (VERIFIED — referenced at
   `api/server.py:906`). However the audit-trail / decision-ledger /
   observability databases themselves have no encryption at rest —
   anyone with filesystem access can read every trade decision.
4. **`risk/manager.py:check_order` records a shadow trade with
   `asyncio.create_task(record_shadow_trade(...))`** wrapped in
   `try/except: pass`. If the shadow-trade write fails silently, the
   counterfactual record is lost without retry. (VERIFIED via
   `risk/manager.py:142-162`.)

---

## 17. Testing

The codebase has a substantial test suite (`tests/` directory, 100+
test files). The relevant coverage for this assessment:

- `tests/test_decision_ledger.py` — ledger stage writes + reads +
  rejection paths.
- `tests/test_e2e_decision_chain.py` — end-to-end decision chain
  integration.
- `tests/integration/test_decision_chain.py` — integration variant.
- `tests/test_attribution.py` — 7-dim attribution.
- `tests/test_closed_positions.py` — round-trip P&L journal.
- `tests/test_audit_logger.py` — durable audit trail.
- `tests/test_immutable_audit.py` — hash-chained trail.
- `tests/test_observability.py` — 6-category metrics.
- `tests/test_alerting.py` — 7 default rules.
- `tests/test_prometheus.py` — `/metrics` endpoint.
- `tests/test_profiling.py` — p50/p95/p99 latency.
- `tests/test_circuit_breaker.py` — 3 breakers + state transitions.
- `tests/test_async_db.py` — async DB pool (W16-7, 25 tests).
- `tests/test_execution_quality.py` — per-fill slippage.
- `tests/test_risk_manager.py` — risk gates + per-trade-loss breaker.
- `tests/test_capital_allocator.py` — T9 sizing.
- `tests/test_capital_allocator_advanced.py` — T5 multiplier stack.
- `tests/test_portfolio_optimizer.py` — Kelly multi-bet (1 pre-existing
  failure per W16-7 worklog).
- `tests/test_stress_test.py` — 6 scenarios.
- `tests/test_backtest_report.py` — VaR/CVaR computation (1 pre-existing
  failure per W16-7 worklog).
- `tests/integration/test_observability_pipeline.py` — collector
  integration.
- `tests/integration/test_risk_pipeline.py` — risk-gate integration.

**Test gaps for this assessment:**

1. No end-to-end test verifying that `decision_id` propagates through
   every stage from PREDICTION → FILL. (`test_e2e_decision_chain.py`
   exists but the chain it asserts is the 6-stage version, not the
   §51 12-stage version.)
2. No test verifying that the `feature_store.prediction_id` can be
   joined to the `decision_ledger.decision_id` — because they cannot.
3. No test verifying that the portfolio_optimizer is invoked from the
   hot scan loop — because it is not.
4. No test verifying that the smart_router is invoked from
   `submit_order` — because it is not.

---

## 18. Observability

(See the dedicated `OBSERVABILITY_AND_RELIABILITY_ASSESSMENT.md` for
the full §54 assessment. Summary here for cross-reference.)

- **Six canonical metric categories** (`data_source` / `bot` /
  `strategy` / `execution` / `ml` / `system`) with recommended metric
  names documented in `observability.py:148-155`.
- **Background auto-collector** runs every 30 seconds, pulls stats
  from every subsystem. (VERIFIED via `observability_collector.py:85`.)
- **Prometheus `/metrics` endpoint** exposing counters / gauges /
  histograms. (VERIFIED via `prometheus_metrics.py`.)
- **Per-endpoint p50/p95/p99 latency profiler** (in-memory, 1000-sample
  window per endpoint). (VERIFIED via `profiling.py:117-145`.)
- **Alert engine with 7 default rules** across 4 categories.
  (VERIFIED via `alerting.py:223-307`.)
- **Three pre-configured circuit breakers** for external APIs.
  (VERIFIED via `circuit_breaker.py:209-227`.)
- **Hash-chained immutable audit trail** for control events.
  (VERIFIED via `immutable_audit.py`.)
- **Structured JSON logging** with request-scoped contextvars
  (`request_id` / `user` / `endpoint`). (VERIFIED via
  `logging_config.py:46-53`.)

**Observability gap relevant to this assessment:** the §51 ledger
chain has no per-stage latency metric. A slow PREDICTION stage (e.g.
model predict blocked on a hot lock) would not surface in the
dashboard — only the `inference_latency=0.0` placeholder would be
recorded (which the collector itself flags as `instrumented=False`).

---

## 19. Production Readiness

The system is **paper-trading ready** (maturity ~7/10 per the
`FINAL_SYSTEM_REASSESSMENT.md` baseline). For **live trading**, the
gaps that matter for cross-system architecture are:

1. **Open-position attribution is broken** until close (§9.3). An
   operator who wants "which decision opened this position?" cannot
   answer it for open positions.
2. **The portfolio optimizer is not in the live trade path** (§9.2).
   Each signal is sized in isolation; portfolio-level constraints are
   advisory only.
3. **The MTM gate is fail-open** (§11.3). A runaway MTM computation
   silently widens true risk past the $25 ceiling.
4. **Silent data loss is possible** under disk pressure (§15.1). Every
   ledger write swallows persistence errors. For paper trading this
   is acceptable; for live trading it is not.
5. **No POSITION / OUTCOME / PNL ledger stages** (§9.1). The
   reconstructability contract for live trades is therefore weaker
   than for paper trades.

**Production-readiness score for cross-system architecture: 6.5/10.**
The pipeline runs end-to-end and the 6-stage decision ledger is
genuinely useful, but the spec's §51 12-stage chain is not yet
implemented and several disconnections (portfolio optimizer, smart
router, open-position attribution) would need to be closed before
live trading.

---

## 20. Evidence

### 20.1 VERIFIED (read in source file in this session)

- `core/decision_ledger.py:108-126` — the complete `STAGE_*` constant
  set (6 stages, no `MARKET` / `INTELLIGENCE` / `FEATURE` / `POSITION`
  / `OUTCOME` / `PNL`).
- `core/decision_ledger.py:244-307` — `record()` method writes
  PREDICTION/SIGNAL/RISK_*/ORDER/FILL with `model_version` auto-stamp.
- `core/decision_ledger.py:308-374` — `record_rejection()` writes to
  both `decision_rejections` and the main chain.
- `core/decision_ledger.py:630-677` — HTTP endpoints
  `GET /api/decision/{token_id}` + `GET /api/decisions/rejected`.
- `strategies/base.py:60-148` — `submit_order` flow with
  `risk_manager.check_order` + `paper_sim.create_order` / `clob_client.create_order`.
- `strategies/signal_trader.py:1-120` — capital_allocator wiring,
  KELLY_FRACTION=0.25, scan interval 15s.
- `risk/manager.py:1-200` — capital model constants, 13+ gates,
  per-trade-loss cooldown, dynamic ML-health sizing multiplier.
- `risk/manager.py:142-162` — shadow-trade recording on rejection
  (fire-and-forget).
- `risk/manager.py:308-315` — MTM gate fail-open (`except: pass`).
- `core/capital_allocator.py:1-300` — T9 `allocate_size` + T5
  `allocate_capital` multiplier stack (Michaelis-Menten edge curve).
- `core/portfolio_optimizer.py:60-360` — Kelly multi-bet optimizer,
  not wired into hot scan loop.
- `core/portfolio.py:1-60` — `compute_exposure()` decomposition.
- `core/closed_positions.py:18-38` — schema with `decision_id` FK
  (populated only on close).
- `core/audit_logger.py:1-120` — durable audit trail, category-indexed.
- `core/immutable_audit.py:1-120` — hash-chained trail for control
  events.
- `core/observability.py:125-228` — 6 categories, METRIC_NAMES dict.
- `core/observability_collector.py:1-120` — 30s background auto-collector.
- `core/alerting.py:223-307` — 7 default rules across 4 categories.
- `core/prometheus_metrics.py:40-200` — counters/gauges/histograms.
- `core/circuit_breaker.py:209-227` — 3 breakers + status registry.
- `core/profiling.py:108-200` — p50/p95/p99 profiler.
- `ml/model.py:1-60` — 4-model ensemble + isotonic + stacking.
- `ml/feature_store.py:1-100` — feature definitions + per-prediction
  values (separate `prediction_id` identifier space).
- `core/ingestion/source_registry.py:1-95` — TimescaleDB-backed source
  registry with in-memory fallback.
- `core/ingestion/raw_vault.py:22-85` — SHA-256 + bitemporal raw
  observations + dead-letter quarantine.
- `core/data_store.py:1-80` — in-memory store + JSON persistence,
  bankroll baseline 100.0.
- `core/market_discovery.py:1-60` — 3-minute catalog sync, in-memory.
- `core/fundamental_ingest.py:1-60` — 100k+ source registry, keyword
  sentiment.
- `core/shadow_trading.py:1-60` — counterfactual journal with
  `decision_id` cross-ref.
- `core/attribution.py:1-60` — 7-dim P&L roll-up on closed positions.
- `backtesting/report.py:74-180` — VaR / CVaR computation
  (`np.percentile(returns, 5)` + mean of tail).
- `core/logging_config.py:46-53` — `request_id_var` / `user_var` /
  `endpoint_var` contextvars.
- `mini-services/polymarket-bot/data/` listing — 12 SQLite databases
  + JSON + pkl files coexist.

### 20.2 STRONG EVIDENCE

- `FINAL_SYSTEM_REASSESSMENT.md` documents the 6-stage ledger
  (`PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL`) and the
  141k-row / 71k-chain empirical state of `decision_ledger.db` as of
  2026-09-03.
- The `docs/ARCHITECTURE.md` doc (not re-read this session but
  referenced from `docs/README.md`) is the operator-facing
  architecture document.

### 20.3 LIKELY (consistent with patterns but not directly verified)

- The portfolio_optimizer is NOT invoked from the hot scan loop
  (no `import portfolio_optimizer` in `signal_trader.py`).
- The smart_router is NOT invoked from `submit_order`
  (no `import smart_router` in `strategies/base.py`).
- `market_intelligence.db` is owned by a module not in this trace's
  scope (no `core/market_intelligence.py` found).

### 20.4 UNVERIFIED

- The exact contents of `core/sanitizer.py` (referenced as the
  normalization stage).
- The actual trigger conditions in `ml/training_orchestrator.py`
  for periodic RF / GB retraining.
- Whether `execution/smart_router.py` is invoked from any non-strategy
  code path (e.g. an admin tool).

### 20.5 NOT FOUND

- A `MARKET_SNAPSHOT` stage or table.
- An `INTELLIGENCE_SNAPSHOT` stage or table.
- A `FEATURE_SNAPSHOT` stage in the decision ledger (the
  feature_store exists but uses a separate identifier space).
- A `POSITION` stage in the decision ledger.
- An `OUTCOME` stage in the decision ledger.
- A `PNL` stage in the decision ledger (beyond per-fill `pnl`).
- A `signal_id` distinct from `decision_id`.
- A unified migration runner across the 12 SQLite databases.
- A Grafana dashboard configuration file (mentioned in the task spec
  — no `grafana` references in the codebase outside test files).

---

## 21. Unknowns

1. **Who owns `market_intelligence.db`?** The file exists in `data/`
   but no `core/market_intelligence.py` module was found. It may be
   owned by an unscoped module or be an orphan from a prior task.
2. **Is the smart_router ever used?** `execution/smart_router.py` and
   `execution/advanced_router.py` are present but their invocation
   from the live trade path is not visible in `strategies/base.py`.
3. **What is the actual trigger for RF/GB retraining?**
   `training_orchestrator.py` exists but its trigger conditions are
   UNVERIFIED.
4. **Is `core/sanitizer.py` a real normalization stage** or just a
   data-validation helper? Its scope is UNVERIFIED.
5. **Does `paper_sim._execute_fill` actually call
   `risk_manager.report_trade_pnl`?** The fill loop runs in the
   background; the per-trade-loss cooldown depends on this call. The
   call site was not directly verified in this trace.

---

## 22. Maturity Score (0-10)

**Cross-System Architecture maturity: 6.8 / 10**

| Sub-dimension | Score | Rationale |
|---|---|---|
| Pipeline completeness (§50 15 stages) | 8 / 10 | 13 of 15 stages present; NORMALIZATION partial; LEARNING partial (SGD only). |
| Decision ledger chain (§51 12 stages) | 5 / 10 | 6 of 12 stages implemented. Pre-PREDICTION and post-FILL stages missing. |
| §80 answerability ("Why did the bot make this trade?") | 6 / 10 | Reconstructable for 5 of 12 stages (PREDICTION through FILL). Not reconstructable for inputs (market/intelligence/feature) or outputs (position/outcome/P&L-unrealised). |
| §79 target layering | 8 / 10 | 8 of 8 layers present; 2 layers (FEATURE/INTEL, PORTFOLIO ALLOCATION) disconnected from live trade path. |
| Audit trail immutability | 8 / 10 | Hash-chained immutable audit + durable audit + decision ledger coexist; chain is SHA-256 but unsigned. |
| Cross-system identifier linkage | 5 / 10 | `decision_id` covers 5 stages; `prediction_id` is separate; `position_id` only on close; `signal_id` collapsed into `decision_id`; `strategy_id` is a loose string. |
| Test coverage of the chain | 6 / 10 | `test_e2e_decision_chain.py` covers the 6-stage version; no test covers the 12-stage version because it doesn't exist. |

**Composite: 6.8 / 10.** The architecture is sound and the wiring is
real, but the §51 spec is not yet met — the chain is 6 stages, not 12,
and several pre-PREDICTION and post-FILL disconnections prevent full
§80 answerability.

---

## 23. Critical Findings

1. **The §51 unified decision ledger chain is 6 of 12 stages
   implemented.** The 6 missing stages are: `MARKET`, `MARKET SNAPSHOT`,
   `INTELLIGENCE SNAPSHOT`, `FEATURE SNAPSHOT`, `POSITION`, `OUTCOME`,
   `P&L`. (Severity: HIGH — the spec's 12-stage chain is the canonical
   audit contract.)
2. **The system CANNOT fully answer "Why did the bot make this trade?"
   (§80).** The answer is partial: prediction + signal + risk + order +
   fill are reconstructable; the inputs (market/intelligence/feature)
   and outputs (position/outcome/P&L-unrealised) are not. (Severity:
   HIGH — the headline auditability question is partially unanswerable.)
3. **The portfolio optimizer is NOT in the live trade path.** Each
   signal is sized in isolation via `allocate_size`. The multi-bet
   Kelly optimizer is only an operator what-if tool. (Severity:
   MEDIUM — the spec's §53 separation of signal generation from
   capital allocation is satisfied at the per-trade level but not at
   the portfolio level.)
4. **The smart router is NOT wired into `submit_order`.** Live trades
   go directly to `clob_client.create_order`. (Severity: MEDIUM —
   execution quality may be lower than the smart_router could
   achieve.)
5. **The MTM exposure gate is fail-open** — a runaway
   `compute_mark_to_market_exposure` call silently widens true risk
   past the $25 ceiling. (Severity: HIGH for live trading.)
6. **Open positions are not linked by `decision_id`** — only closed
   positions carry the FK. Open-position attribution is broken.
   (Severity: HIGH for live trading.)
7. **The feature_store's `prediction_id` is a separate identifier
   space** from the decision_ledger's `decision_id`. Features cannot
   be reliably joined to the decision that consumed them. (Severity:
   HIGH for ML auditability.)
8. **Every ledger write swallows persistence errors** — silent data
   loss is possible under disk pressure. (Severity: MEDIUM for paper,
   HIGH for live.)
9. **The hash-chained immutable audit trail is unsigned** — anyone
   with write access to `immutable_audit.db` can re-write the chain
   without detection (other than the chain-verification endpoint
   returning `valid=False`). (Severity: MEDIUM — the chain detects
   tampering but does not prevent it.)
10. **`market_intelligence.db` exists but its owner module is NOT
    FOUND.** This may be an orphan or owned by an unscoped module.
    (Severity: LOW — operational hygiene issue.)
11. **The `signal_trader._scan_markets` loop iterates 800+ markets
    every 15 seconds** with no incremental-scan optimisation.
    (Severity: MEDIUM — performance under market universe growth.)
12. **The §79 FEATURE/INTEL LAYER is incomplete** — there is no
    `intelligence_snapshot` store; intelligence is composed ad-hoc
    inside `signal_trader._ml_signal`. (Severity: MEDIUM — the
    intelligence layer is functional but not durable / not
    reconstructable.)

### Recommended next actions (in priority order)

1. **Extend the decision ledger with the 6 missing stages.**
   Specifically: add `STAGE_MARKET_SNAPSHOT`, `STAGE_FEATURE_SNAPSHOT`,
   `STAGE_POSITION`, `STAGE_OUTCOME`, `STAGE_PNL` constants; emit
   them from the appropriate call sites; backfill the schema.
2. **Unify the identifier space.** Make `feature_store.prediction_id`
   an alias for `decision_ledger.decision_id` (or vice versa). Every
   prediction should mint one UUID, used everywhere.
3. **Wire the portfolio optimizer into the hot scan loop.** Replace
   the per-token `allocate_size` call with a batch
   `portfolio_optimizer.optimize(candidate_signals)` call.
4. **Fix the MTM gate to fail-closed** (or at least fail-loud — emit
   an alert when the MTM computation raises).
5. **Add a POSITION ledger stage** emitted when a position opens,
   linked from the originating ORDER. Update the position_manager to
   emit this.
6. **Wire the smart_router into `submit_order`** (or document why it
   is intentionally bypassed).
7. **Investigate `market_intelligence.db`'s owner** — either wire it
   into the ledger chain or delete the orphan.
8. **Add per-stage latency metrics** to the observability collector
   (replace the `inference_latency=0.0` placeholder).

---

*End of Cross-System Architecture Assessment. Companion documents:
`RISK_AND_PORTFOLIO_ASSESSMENT.md` (§52, §53) and
`OBSERVABILITY_AND_RELIABILITY_ASSESSMENT.md` (§54, §55).*
