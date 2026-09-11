-- ============================================================================
-- Migration 001: Initial SQLite Schema
-- ============================================================================
-- Captures every table + index that the per-module ``_init_db()`` methods
-- create on first boot, so a fresh data directory reaches a known-good
-- schema state through the migration system alone (W13-7).
--
-- All statements use ``IF NOT EXISTS`` so this migration is fully
-- idempotent — running it against a database whose schema was already
-- bootstrapped by the existing ``_init_db()`` calls is a no-op (the
-- migration manager and the legacy ``_init_db`` paths coexist).
--
-- Tables are grouped by owning module so the file mirrors the source
-- layout. Migration manager loads this file through ``sqlite3``;
-- PostgreSQL / TimescaleDB-specific migrations (the
-- ``001_initial_enterprise_schemas.sql`` sibling managed by
-- ``core/db/migration_runner.py``) are filtered out by
-- ``_is_sqlite_compatible()`` and never reach this engine.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- decision_ledger.db  (core/decision_ledger.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    decision_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    token_id TEXT,
    strategy TEXT,
    pnl REAL DEFAULT 0.0,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_dec_id       ON decision_events(decision_id, timestamp ASC);
CREATE INDEX IF NOT EXISTS idx_dec_token    ON decision_events(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_dec_stage    ON decision_events(stage);
CREATE INDEX IF NOT EXISTS idx_dec_stage_ts ON decision_events(stage, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_dec_ts       ON decision_events(timestamp DESC);

CREATE TABLE IF NOT EXISTS decision_rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    decision_id TEXT,
    token_id TEXT,
    strategy TEXT,
    predicted_edge REAL,
    confidence REAL,
    reason TEXT,
    market_mid REAL
);
CREATE INDEX IF NOT EXISTS idx_rej_token       ON decision_rejections(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_rej_decision    ON decision_rejections(decision_id);
CREATE INDEX IF NOT EXISTS idx_rej_ts         ON decision_rejections(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_rej_reason_ts   ON decision_rejections(reason, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_rej_strategy_ts ON decision_rejections(strategy, timestamp DESC);


-- ----------------------------------------------------------------------------
-- execution_quality.db  (core/execution_quality.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_quality (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    order_id TEXT NOT NULL,
    decision_id TEXT,
    token_id TEXT,
    strategy TEXT,
    side TEXT,
    signal_price REAL,
    decision_price REAL,
    submitted_price REAL,
    best_bid REAL,
    best_ask REAL,
    expected_fill REAL,
    actual_fill REAL,
    spread REAL,
    slippage REAL,
    slippage_bps REAL,
    latency_ms REAL,
    realized_edge REAL,
    paper INTEGER DEFAULT 0,
    data_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_ts        ON execution_quality(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eq_strategy  ON execution_quality(strategy, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eq_token     ON execution_quality(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eq_decision  ON execution_quality(decision_id);
CREATE INDEX IF NOT EXISTS idx_eq_slippage  ON execution_quality(slippage_bps DESC);
CREATE INDEX IF NOT EXISTS idx_eq_side_ts  ON execution_quality(side, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eq_paper_ts  ON execution_quality(paper, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_eq_order     ON execution_quality(order_id);


-- ----------------------------------------------------------------------------
-- observability.db  (core/observability.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL NOT NULL,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_metrics_cat_name_time ON metrics(category, name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_name_time    ON metrics(name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_cat          ON metrics(category);
CREATE INDEX IF NOT EXISTS idx_metrics_ts           ON metrics(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_cat_ts       ON metrics(category, timestamp DESC);


-- ----------------------------------------------------------------------------
-- closed_positions.db  (core/closed_positions.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS closed_positions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    position_id     TEXT    NOT NULL UNIQUE,
    token_id        TEXT    NOT NULL,
    strategy         TEXT,
    entry_price     REAL,
    exit_price      REAL,
    shares          REAL,
    pnl             REAL    DEFAULT 0.0,
    holding_seconds REAL    DEFAULT 0.0,
    model_version   TEXT,
    decision_id     TEXT,
    direction       TEXT,
    confidence      REAL,
    predicted_edge  REAL,
    p_yes           REAL,
    market_mid      REAL,
    liquidity       REAL,
    metadata_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_token     ON closed_positions(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cp_strategy  ON closed_positions(strategy, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cp_time      ON closed_positions(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cp_decision  ON closed_positions(decision_id);
CREATE INDEX IF NOT EXISTS idx_cp_direction ON closed_positions(direction);
CREATE INDEX IF NOT EXISTS idx_cp_pnl       ON closed_positions(pnl);
CREATE INDEX IF NOT EXISTS idx_cp_model_ts ON closed_positions(model_version, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_cp_exit_price ON closed_positions(exit_price);


-- ----------------------------------------------------------------------------
-- alerts.db  (core/alerting.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    value REAL,
    threshold REAL,
    metadata TEXT,
    acknowledged INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_sev_ack_ts ON alerts(severity, acknowledged, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_cat_ts     ON alerts(category, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_ack_ts     ON alerts(acknowledged, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_name       ON alerts(name);


-- ----------------------------------------------------------------------------
-- feature_flags.db  (core/feature_flags.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_flags (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    description TEXT,
    config TEXT,
    updated_at REAL
);


-- ----------------------------------------------------------------------------
-- audit_trail.db  (core/audit_logger.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    category TEXT NOT NULL,
    event_type TEXT NOT NULL,
    token_id TEXT,
    slug TEXT,
    details TEXT NOT NULL,
    pnl REAL DEFAULT 0.0,
    strategy TEXT,
    idempotency_key TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_audit_time ON audit_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_cat  ON audit_events(category);


-- ----------------------------------------------------------------------------
-- order_state_machine.db  (core/order_state_machine.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS order_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    order_id TEXT NOT NULL,
    state TEXT NOT NULL,
    strategy TEXT,
    token_id TEXT,
    side TEXT,
    price REAL,
    size REAL,
    filled_size REAL,
    idempotency_key TEXT,
    decision_id TEXT,
    metadata_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_ord_id          ON order_transitions(order_id, timestamp ASC);
CREATE INDEX IF NOT EXISTS idx_ord_idempotency ON order_transitions(idempotency_key, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ord_token      ON order_transitions(token_id, timestamp DESC);


-- ----------------------------------------------------------------------------
-- shadow_trades.db  (core/shadow_trading.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    decision_id     TEXT,
    token_id        TEXT,
    strategy        TEXT,
    side            TEXT,
    price           REAL,
    size            REAL,
    predicted_edge  REAL,
    confidence      REAL
);
CREATE INDEX IF NOT EXISTS idx_st_time      ON shadow_trades(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_st_strategy ON shadow_trades(strategy, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_st_token     ON shadow_trades(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_st_decision  ON shadow_trades(decision_id);


-- ----------------------------------------------------------------------------
-- market_intelligence.db  (core/market_db.py)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_id TEXT NOT NULL,
    slug TEXT,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread REAL,
    volume_24h REAL,
    liquidity REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_token ON market_snapshots(token_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS orderbook_ticks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_id TEXT NOT NULL,
    best_bid_size REAL,
    best_ask_size REAL,
    ofi REAL,
    micro_price REAL
);
CREATE INDEX IF NOT EXISTS idx_ticks_token ON orderbook_ticks(token_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS fundamental_news (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    headline TEXT NOT NULL,
    source TEXT,
    category TEXT,
    sentiment REAL,
    matched_tokens TEXT
);
CREATE INDEX IF NOT EXISTS idx_news_time ON fundamental_news(timestamp DESC);

CREATE TABLE IF NOT EXISTS ml_feature_store (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    token_id TEXT NOT NULL,
    features_json TEXT NOT NULL,
    p_pred REAL,
    confidence REAL,
    outcome_resolved INTEGER DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_feat_token ON ml_feature_store(token_id, timestamp DESC);
