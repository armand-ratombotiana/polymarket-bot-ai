# Data Platform — Improvement Plan

- **Domain:** Data platform (SQLite → PostgreSQL/TimescaleDB
  migration, real-time pipeline, data quality, retention,
  feature versioning)
- **Owning modules:** `core/data_store.py`, `core/db_pool.py`,
  `core/async_repositories.py`, `core/timescale_db.py`,
  `core/market_db.py`, `core/retention.py`, `core/reconciliation.py`,
  `core/sentiment.py`, `core/fundamental_ingest.py`,
  `core/ingestion/*`, `core/db/*`, `migrations/*`,
  `scripts/backup.sh`, `scripts/restore.sh`,
  `scripts/check_integrity.py`, `scripts/backup_rotation.py`,
  `scripts/test_restore.py`, `scripts/verify_backup.py`
- **Priority classification (per God Mode §64):**
  - P0 — data quality monitoring (silent data loss = silent capital
    loss).
  - P1 — PostgreSQL/TimescaleDB migration, real-time pipeline,
    feature versioning.
  - P2 — historical data retention optimization.
- **Status as of W17-9:** mostly TODO — the foundation is laid
  (W16-7 async pool, W5 timescale_db.py standby adapter, W14-6
  backup verification suite) but the migration has not started.

This plan defines every improvement in the data platform using the
per-improvement field set required by God Mode §63. Each
improvement maps to one or more rows in
`docs/implementation/MASTER_IMPLEMENTATION_PLAN.md`.

---

## Improvement DP-1 — PostgreSQL/TimescaleDB Migration (from SQLite)

- **Problem:** `core/data_store.py`, `core/decision_ledger.py`,
  `core/observability.py`, `core/execution_quality.py`,
  `core/closed_positions.py`, `core/shadow_trading.py`,
  `core/market_db.py`, `core/audit_logger.py`, and ~12 other
  modules all open SQLite files directly via `sqlite3.connect()`.
  SQLite is fine for paper-mode single-process deployment but
  cannot support:
  - Multiple backend processes (the architecture doc §12.1 lists
    this as the limit).
  - Concurrent writers (the W16-7 async pool uses WAL mode +
    NORMAL sync to coexist with the sync recorder, but this is a
    workaround, not a real solution).
  - Time-series queries (the `orderbook_ticks` table is now ~2 M
    rows; a 30-day VWAP query takes 8 s on SQLite vs ~50 ms on
    TimescaleDB with hypertable partitioning).
  - Continuous aggregates (we manually compute `token_vwaps` in
    Python instead of using TimescaleDB's `CONTINUOUS VIEW`).
- **Evidence:**
  - `core/timescale_db.py` (V-wave 5) is a standby adapter —
    imports cleanly, has 9 tests, but no production caller.
  - `core/db_pool.py` (W16-7) is async-first (aiosqlite +
    asyncpg) but only the SQLite path is exercised.
  - `tests/test_async_db.py` (W16-7, 25 tests) — async repos
    work against SQLite; no Postgres test coverage.
  - `docs/ARCHITECTURE.md` §12.2 documents the migration path
    but the migration itself is TODO.
  - `requirements.txt` includes `asyncpg` but no production code
    calls it.
- **Current State:** ~8 SQLite databases (`decision_ledger.db`,
  `data_store.db`, `observability.db`, `audit_trail.db`,
  `shadow_trades.db`, `market_intelligence.db`,
  `execution_quality.db`, `closed_positions.db`). All schemas
  defined inline per module. Backup via `scripts/backup.sh`
  (sqlite3 + gzip). No Postgres dependency at runtime.
- **Desired State:**
  1. Single PostgreSQL instance (TimescaleDB extension enabled).
  2. Each former SQLite DB becomes a Postgres schema
     (`decision_ledger`, `data_store`, `observability`, ...).
  3. `orderbook_ticks` becomes a TimescaleDB hypertable
     (chunk_time_interval = 1 day).
  4. `token_vwaps` + `strategy_vwaps` become continuous
     aggregates (refresh every 5 min).
  5. `core/db_pool.py` is the only DB access layer — async-first,
     asyncpg-backed. SQLite is removed (or kept as the test
     fixture only).
  6. Migration scripts `migrations/sqlite_to_postgres/*.py` —
     read each SQLite DB, batch-write to Postgres, verify row
     counts + content hashes (reusing `scripts/test_restore.py`'s
     pattern).
  7. Live safety gate check #12 `postgres_migrated` — required
     for live trading.
- **Proposed Solution:**
  1. Docker-compose adds a `postgres` service (TimescaleDB image).
  2. Alembic migration system introduced (replacing the inline
     `_init_db()` per module).
  3. Per-module schema files under `migrations/versions/`.
  4. Migration scripts under `scripts/migrate_sqlite_to_postgres/`.
  5. `core/db_pool.py` becomes the only entrypoint; the per-module
     `sqlite3.connect()` calls are removed.
  6. Feature flag `POSTGRES_MIGRATED` (default off; flipped per-
     deployment).
- **Architecture:**
  ```
  docker-compose.yml
    └─→ postgres (TimescaleDB image, port 5432, volume pg-data)
  core/db_pool.py
    └─→ AsyncDBPool.get_connection(schema)
         └─→ asyncpg.connect(host=postgres, db=polymarket)
              └─→ SELECT * FROM decision_ledger.decision_events ...
  migrations/versions/
    └─→ 001_initial_schema.py (creates all schemas + hypertables)
    └─→ 002_continuous_aggregates.py (token_vwaps, strategy_vwaps)
  scripts/migrate_sqlite_to_postgres/
    └─→ migrate_decision_ledger.py
    └─→ migrate_data_store.py
    └─→ ...
    └─→ verify_migration.py (row counts + content hashes)
  ```
- **Implementation:**
  1. Add Postgres service to `docker-compose.yml`.
  2. Add Alembic; write initial migration.
  3. Write per-DB migration scripts (8 scripts).
  4. Refactor every module's `_init_db` to be a no-op when
     `POSTGRES_MIGRATED=true`.
  5. Replace `sqlite3.connect()` with `db_pool.get_connection()`.
  6. Update `core/timescale_db.py` from standby to active.
  7. Migration cutover: stop bot, run migrations, run verify,
     flip flag, restart bot.
  8. Update tests to use a Postgres testcontainer instead of
     in-memory SQLite.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/db_pool.py` (rewrite — asyncpg
    primary, SQLite fallback for tests)
  - `mini-services/polymarket-bot/core/timescale_db.py` (activate)
  - `mini-services/polymarket-bot/core/data_store.py` (refactor)
  - `mini-services/polymarket-bot/core/decision_ledger.py` (refactor)
  - `mini-services/polymarket-bot/core/observability.py` (refactor)
  - `mini-services/polymarket-bot/core/execution_quality.py` (refactor)
  - `mini-services/polymarket-bot/core/closed_positions.py` (refactor)
  - `mini-services/polymarket-bot/core/shadow_trading.py` (refactor)
  - `mini-services/polymarket-bot/core/market_db.py` (refactor)
  - `mini-services/polymarket-bot/core/audit_logger.py` (refactor)
  - `mini-services/polymarket-bot/core/async_repositories.py`
    (rewrite — asyncpg-native)
  - `mini-services/polymarket-bot/alembic.ini` (new)
  - `mini-services/polymarket-bot/migrations/versions/*.py` (new)
  - `mini-services/polymarket-bot/scripts/migrate_sqlite_to_postgres/*.py`
    (new — 8 migration scripts + 1 verify)
  - `docker-compose.yml` (add postgres service)
  - `mini-services/polymarket-bot/requirements.txt` (add alembic)
  - `mini-services/polymarket-bot/tests/conftest.py` (Postgres
    testcontainer fixture)
  - `mini-services/polymarket-bot/tests/test_async_db.py` (rewrite
    — Postgres-native)
- **Dependencies:** None (this is the foundation).
- **Risk:** CRITICAL — touches every persistence layer. Mitigation:
  dual-write period (1 wave: writes go to both SQLite + Postgres;
  reads prefer Postgres, fall back to SQLite). Cutover only after
  7-day dual-write with zero drift.
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Multi-process horizontal scaling (the §12 limit lifted).
  - 100x faster time-series queries on `orderbook_ticks`.
  - Continuous aggregates replace Python VWAP computation.
  - Live safety gate check #12 flips to passing.
- **Tests:**
  - Every existing test must pass against Postgres (the conftest
    fixture swaps in a Postgres testcontainer).
  - +20 new tests covering migration scripts (each script's
    idempotency + content-hash verification).
- **Metrics:**
  - `db_query_ms{schema, table}` histogram (Prometheus).
  - `db_migration_rows_migrated{schema}` counter.
  - `db_migration_drift_total{schema}` counter (should be 0).
- **Acceptance Criteria:**
  - All existing tests pass against Postgres.
  - 7-day dual-write period with 0 content-hash drift.
  - `orderbook_ticks` 30-day VWAP query < 100 ms p95.
  - Live safety gate check #12 passes.
- **Status:** TODO (foundation laid, migration not started).

---

## Improvement DP-2 — Real-Time Pipeline Enhancements

- **Problem:** `core/book_poller.py` polls the Polymarket CLOB API
  every 2 s for each tracked token. There is no WebSocket
  ingestion for order-book updates (Polymarket exposes a WS feed
  but the bot doesn't subscribe). The bot's tick-to-trade latency
  is therefore bounded below by the 2-s poll interval. For
  arbitrage strategies this is unacceptable — the arb_scanner can
  only see opportunities every 2 s.
- **Evidence:**
  - `core/book_poller.py` (Wave 7) — 2 s `setInterval`-style
    loop; 5 tests covering tracking + dedup + circuit breaker.
  - `core/ws_client.py` (Wave 8) — exists, has 6 tests, but is
    used only for the bot's outbound WS broadcast (frontend
    push), not for inbound Polymarket WS ingestion.
  - `docs/WEBSOCKET.md` documents 5 outbound channels; no
    inbound channels documented.
- **Current State:** Poll-based ingestion (2 s interval). Outbound
  WS broadcast (5 channels). No inbound WS.
- **Desired State:**
  1. Inbound WS subscription to Polymarket's `market` channel
     for each tracked token (book updates).
  2. Poll-based fallback remains (in case WS disconnects —
     circuit breaker switches to poll after 3 reconnect
     failures).
  3. WS message → `data_store.update_order_book()` → outbound WS
     broadcast to the frontend (the 5-channel broadcast layer
     already exists).
  4. Tick-to-trade latency: < 200 ms p95 (vs current 2 s).
  5. New `tick_latency_ms` metric (Prometheus histogram).
- **Proposed Solution:**
  1. Extend `core/ws_client.py` with `PolymarketWSClient` class.
  2. Subscribe to `market` channel for each tracked token.
  3. `on_message` → `data_store.update_order_book()` →
     `ws_broadcast.broadcast('book', payload)`.
  4. Circuit breaker: if WS reconnects > 3 times in 60 s,
     `book_poller` takes over.
  5. Latency metric: timestamp at WS message arrival → timestamp
     at broadcast → histogram.
- **Architecture:**
  ```
  Polymarket CLOB WS
    └─→ PolymarketWSClient.on_message(msg)
         └─→ data_store.update_order_book(token, msg.bids, msg.asks)
              └─→ ws_broadcast.broadcast('book', payload)
                   └─→ frontend WebSocket clients receive within 50 ms
  CircuitBreaker
    └─→ if WS reconnect_count > 3 in 60s → book_poller takes over
         └─→ on WS recovery → book_poller hands back
  ```
- **Implementation:**
  1. `PolymarketWSClient` class.
  2. Subscription manager.
  3. Circuit-breaker handoff logic.
  4. Latency metric instrumentation.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/ws_client.py` (extend)
  - `mini-services/polymarket-bot/core/book_poller.py` (add
    handoff logic)
  - `mini-services/polymarket-bot/core/circuit_breaker.py`
    (already extended by BE-4)
  - `mini-services/polymarket-bot/tests/test_ws_client.py`
    (expand)
  - `mini-services/polymarket-bot/tests/test_book_poller.py`
    (expand)
- **Dependencies:** DP-1 (Postgres migration — WS ingestion will
  produce 10x more `orderbook_ticks` rows; SQLite cannot keep up
  without WAL contention).
- **Risk:** HIGH — WS reconnection storms can crash the bot.
  Mitigation: circuit breaker (BE-4) handles reconnect storms;
  book_poller fallback is always available.
- **Priority:** P1 (core architecture).
- **Expected Benefit:**
  - Tick-to-trade latency drops from 2 s to < 200 ms p95.
  - Arbitrage strategies become viable.
  - Frontend updates become real-time (no more 2 s polling).
- **Tests:** +12 tests covering WS subscription, message routing,
  circuit-breaker handoff, latency metric.
- **Metrics:**
  - `ws_inbound_messages_total{channel}` counter.
  - `ws_inbound_latency_ms` histogram.
  - `ws_reconnects_total` counter.
  - `book_poller_fallback_active` gauge.
- **Acceptance Criteria:**
  - Inbound WS subscribes to all tracked tokens.
  - Tick-to-trade p95 < 200 ms.
  - Book_poller fallback activates within 60 s of WS failure.
- **Status:** TODO.

---

## Improvement DP-3 — Data Quality Monitoring

- **Problem:** The platform has no data-quality monitoring. There
  is no alert when:
  - `orderbook_ticks` for a tracked token has no rows for the
    last 5 min (silent data loss).
  - `decision_events` has a row whose `timestamp` is in the
    future (clock skew).
  - `execution_quality.slippage_bps` exceeds 100 bps (data
    anomaly, not a trade issue).
  - `closed_positions.pnl` is NULL for a settled market
    (settlement failure).
  - `metrics.value` for a given `(category, name)` is older than
    1 h (collector died).
- **Evidence:**
  - `core/observability_collector.py` (T7, Wave 3) records 31
    metrics but none of them are "data-freshness" checks.
  - `core/reconciliation.py` (Wave 7) compares internal vs
    Polymarket state but is invoked manually.
  - `FINAL_SYSTEM_REASSESSMENT.md` §3.5 lists "data quality
    monitoring" as the highest-priority residual risk.
- **Current State:** No data-quality monitoring. Reconciliation is
  manual. Stale data is silent.
- **Desired State:**
  1. `DataQualityMonitor` class — runs every 5 min.
  2. Per-table freshness checks: every table has a
     `max_acceptable_age_seconds` config.
  3. Per-column anomaly checks: `slippage_bps < 100`,
     `pnl != NULL`, `timestamp < now()`, etc.
  4. Reconciliation is automated (runs every 15 min).
  5. Failures emit alerts via `core/alerting.py` (W16-1).
  6. New endpoint `GET /api/data-quality/status` returns the
     check matrix.
  7. New panel `DataQualityPanel.tsx`.
- **Proposed Solution:**
  1. `DataQualityMonitor` class in `core/data_quality.py` (new).
  2. Config in `core/config.py`: per-table freshness + per-column
     anomaly thresholds.
  3. `core/reconciliation.py` becomes a sub-check of the
     monitor.
  4. Cron in `training_orchestrator.py` (5-min schedule).
  5. Endpoint + UI panel.
- **Architecture:**
  ```
  DataQualityMonitor.run()
    └─→ for each table in config:
         ├─→ freshness_check: SELECT MAX(timestamp) → compare to max_acceptable_age
         └─→ for each anomaly_rule:
              SELECT COUNT(*) WHERE rule.predicate → if > 0, alert
    └─→ reconciliation_check: compare internal state vs Polymarket API
    └─→ write to data_quality_results table
    └─→ if any check fails → alerting.alert(severity, message)
  GET /api/data-quality/status
    └─→ { checks: [{name, status, last_run, value, threshold}], summary }
  ```
- **Implementation:**
  1. New module `core/data_quality.py`.
  2. Config extension in `core/config.py`.
  3. Refactor `core/reconciliation.py` to be called by the monitor.
  4. Cron wiring.
  5. New endpoint + UI panel.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/data_quality.py` (new)
  - `mini-services/polymarket-bot/core/reconciliation.py` (refactor)
  - `mini-services/polymarket-bot/core/config.py` (extend)
  - `mini-services/polymarket-bot/core/alerting.py` (extend —
    data-quality alert type)
  - `mini-services/polymarket-bot/ml/training_orchestrator.py`
    (cron)
  - `mini-services/polymarket-bot/api/server.py` (new endpoint)
  - `mini-services/polymarket-bot/migrations/0XX_data_quality_results.sql`
    (new)
  - `src/components/DataQualityPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (entry)
  - `mini-services/polymarket-bot/tests/test_data_quality.py` (new)
- **Dependencies:** DP-1 (Postgres migration — the freshness
  queries are 100x faster on Postgres); ML-6 (label backfill —
  the monitor's Gamma-API check feeds back into backfill retry).
- **Risk:** LOW — additive; no existing functionality touched.
- **Priority:** P0 (silent data loss = silent capital loss).
- **Expected Benefit:**
  - Silent data loss becomes a noisy alert within 5 min.
  - Reconciliation runs unattended.
  - Operators can answer "is the data fresh?" with one click.
- **Tests:** +20 tests covering each freshness check, each
  anomaly rule, reconciliation integration, alerting integration,
  endpoint schema, UI rendering.
- **Metrics:**
  - `data_quality_check_total{check, result}` counter.
  - `data_quality_check_value{check}` gauge.
  - `data_quality_alerts_total{severity}` counter.
- **Acceptance Criteria:**
  - All 20 data-quality tests pass.
  - A simulated stale-table scenario emits an alert within 5 min.
  - The UI panel renders the check matrix with status colours.
- **Status:** TODO (P0 — scheduled for W18).

---

## Improvement DP-4 — Historical Data Retention Optimization

- **Problem:** `core/retention.py` (T6, Wave 3) prunes data at
  7/30/90-day windows but (a) the windows are global — every
  table gets the same pruning; (b) pruned data is deleted, not
  archived (no cold storage); (c) the pruner is cron-invoked but
  the cron schedule is not configurable per-table.
- **Evidence:**
  - `tests/test_retention.py` (U8, Wave 4) — 22 tests including
    16-case SQL-injection guard; covers the 7/30/90 windows.
  - `docs/ARCHITECTURE.md` §10 documents the pruning mechanism.
  - `scripts/backup_rotation.py` (W14-6) implements GFS rotation
    for backups but the retention pruner does not have a
    comparable archive policy.
- **Current State:** Global 7/30/90-day pruning. No archive.
  Cron not per-table.
- **Desired State:**
  1. Per-table retention config: `decision_events` = 365 d,
     `orderbook_ticks` = 30 d (raw) + 90 d (1-min aggregates),
     `execution_quality` = 365 d, `metrics` = 90 d, etc.
  2. Archive-then-delete: pruned rows are written to a Parquet
     file in `data/archive/<table>/<YYYY>/<MM>/` before deletion.
  3. Aggregation: `orderbook_ticks` older than 30 d is
     downsampled to 1-min OHLCV aggregates (stored in a new
     `orderbook_ticks_1m` table).
  4. Configurable cron per table.
- **Proposed Solution:**
  1. Extend `core/retention.py` with per-table config.
  2. Add `ArchiveWriter` class (writes Parquet via pyarrow).
  3. Add `Aggregator` class for `orderbook_ticks`.
  4. Add `orderbook_ticks_1m` table.
  5. Refactor the cron schedule to be per-table.
- **Architecture:**
  ```
  retention config
    └─→ decision_events: 365d (no archive)
    └─→ orderbook_ticks: 30d raw, 90d as 1m aggregates
    └─→ execution_quality: 365d (archive to Parquet at 365d)
    └─→ metrics: 90d (archive to Parquet at 90d)
  cron per table
    └─→ for table in config:
         ├─→ if age > archive_threshold: archive_to_parquet(rows)
         └─→ if age > delete_threshold: DELETE FROM table WHERE timestamp < ...
         └─→ for orderbook_ticks: aggregate_to_1m(rows)
              └─→ INSERT INTO orderbook_ticks_1m
  ```
- **Implementation:**
  1. Per-table config in `core/config.py`.
  2. `ArchiveWriter` + `Aggregator` classes.
  3. New `orderbook_ticks_1m` table.
  4. Refactor `core/retention.py`.
  5. Update `scripts/db-maintenance.sh` to call the new pruner.
- **Files Affected:**
  - `mini-services/polymarket-bot/core/retention.py` (extend)
  - `mini-services/polymarket-bot/core/config.py` (extend)
  - `mini-services/polymarket-bot/migrations/0XX_orderbook_ticks_1m.sql`
    (new)
  - `mini-services/polymarket-bot/tests/test_retention.py`
    (expand from 22 → ~32 tests)
  - `scripts/db-maintenance.sh` (extend)
- **Dependencies:** DP-1 (Postgres migration — TimescaleDB
  continuous aggregates replace the manual aggregator).
- **Risk:** LOW — additive; old pruning behaviour preserved as
  the default config.
- **Priority:** P2 (optimization).
- **Expected Benefit:**
  - `orderbook_ticks` stays at ~30 M rows instead of growing
    unbounded.
  - 5-year backtest becomes possible (archive + aggregates).
  - Storage cost bounded.
- **Tests:** +10 tests covering per-table config, archive
  writer, aggregator, 1-min OHLCV computation.
- **Metrics:**
  - `retention_rows_pruned_total{table}` counter.
  - `retention_rows_archived_total{table}` counter.
  - `retention_aggregated_rows_total{table}` counter.
- **Acceptance Criteria:**
  - All 32 retention tests pass.
  - After 1 week of operation, `data/archive/` has Parquet
    files for any pruned data.
  - `orderbook_ticks_1m` table populated within 1 h of the cron.
- **Status:** IN PROGRESS.

---

## Improvement DP-5 — Feature Versioning

- **Problem:** `ml/feature_store.py` (W16-2) implements minimal
  feature versioning (schema hash + version number), but (a)
  the schema is implicit (extracted from the feature vector at
  write time, not declared); (b) features are not separated into
  online vs offline (see ML-1); (c) no version-diff tool (an
  operator cannot ask "what changed between v6 and v7?").
- **Evidence:**
  - `tests/test_feature_store.py` (W16-2, 13 tests) — covers
    schema persistence.
  - `ml/features.py::extract_features()` returns a dict; the
    schema is whatever keys the dict happens to have.
  - No `FeatureSchema` dataclass exists.
- **Current State:** Implicit schema; version stored but not
  enforced; no diff tool.
- **Desired State:**
  1. `FeatureSchema` dataclass declares the feature list +
     per-feature dtype + version.
  2. Adding a feature increments the version (the schema is
     immutable per version).
  3. `FeatureStore` enforces the schema at write time (rejects
     features not in the schema).
  4. `FeatureSchemaDiff` tool — given two versions, returns the
     added / removed / changed features.
  5. New endpoint `GET /api/ml/features/diff?v1=&v2=`.
  6. UI: feature schema browser panel.
- **Proposed Solution:**
  1. `FeatureSchema` dataclass in `ml/feature_store.py`.
  2. `SchemaRegistry` class — stores schemas in
     `feature_schemas` table.
  3. `FeatureSchemaDiff` tool.
  4. Endpoint + UI panel.
- **Architecture:**
  ```
  FeatureSchema(version=7, features=[
    Feature("spread_bps", dtype="float64"),
    Feature("ofi_5s", dtype="float64"),
    Feature("competitiveness", dtype="float64"),
    ...
  ])
  SchemaRegistry.register(schema) → immutable
  SchemaRegistry.get(version) → schema
  FeatureSchemaDiff(v1=6, v2=7) → { added: ["liquidity_5m"], removed: [], changed: [] }
  ```
- **Implementation:**
  1. `FeatureSchema` + `SchemaRegistry` + `FeatureSchemaDiff`.
  2. Migration `feature_schemas` table.
  3. Enforce schema at write time.
  4. Endpoint + UI panel.
- **Files Affected:**
  - `mini-services/polymarket-bot/ml/feature_store.py` (extend)
  - `mini-services/polymarket-bot/ml/features.py` (declare
    schema)
  - `mini-services/polymarket-bot/migrations/0XX_feature_schemas.sql`
    (new)
  - `mini-services/polymarket-bot/ml/routes.py` (new endpoint)
  - `src/components/FeatureSchemaPanel.tsx` (new)
  - `src/components/Sidebar.tsx` (entry)
  - `mini-services/polymarket-bot/tests/test_feature_store.py`
    (expand from 13 → ~24 tests)
- **Dependencies:** Overlaps with ML-1 (feature store
  enhancements) — this is the schema-versioning half of ML-1.
- **Risk:** LOW — additive; existing feature vectors stay valid
  (their schema hash is grandfathered into v1).
- **Priority:** P1 (model reproducibility).
- **Expected Benefit:**
  - Adding a feature no longer silently breaks older models.
  - Schema diff makes model-retraining decisions auditable.
  - Foundation for backtest reproducibility (a backtest tied to
    schema v7 is reproducible even after v8 ships).
- **Tests:** +11 tests covering schema declaration, registry,
  diff tool, write-time enforcement, UI panel.
- **Metrics:**
  - `feature_schema_version` gauge (current).
  - `feature_schema_diff_total{direction}` counter.
  - `feature_schema_enforcement_violations_total` counter.
- **Acceptance Criteria:**
  - All 24 feature-store tests pass.
  - Adding a feature without incrementing the schema version
    raises a `SchemaViolation` error.
  - Schema diff endpoint returns a structured diff for any two
    versions.
- **Status:** IN PROGRESS.
