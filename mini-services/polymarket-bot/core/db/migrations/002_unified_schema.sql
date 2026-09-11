-- ============================================================================
-- Migration 002: Unified Schema (PostgreSQL + SQLite compatible)
-- ============================================================================
-- W21-3 — unified schema migration that works on BOTH PostgreSQL and SQLite.
--
-- Canonical auto-increment syntax: ``SERIAL PRIMARY KEY`` (PostgreSQL-native).
-- The migration manager translates this to ``INTEGER PRIMARY KEY AUTOINCREMENT``
-- when running on SQLite (see ``core.db.migration_manager._translate_for_sqlite``).
--
-- All statements use ``IF NOT EXISTS`` so the migration is fully idempotent:
--   * On a fresh database, every table + index is created.
--   * On a database that already ran migration 001 (SQLite-only, with
--     ``AUTOINCREMENT`` and slightly different column sets), the
--     ``CREATE TABLE IF NOT EXISTS`` is a no-op for shared table names
--     (the existing schema is preserved). ``CREATE INDEX`` statements
--     that reference columns missing from the 001 schema are skipped
--     with a warning by the migration manager — they do NOT abort the
--     migration. This allows 002 to run cleanly after 001 on the same
--     database without manual intervention.
--
-- The migration declares the canonical unified schema for the bot's
-- operational data — market data, decisions, execution quality, P&L,
-- observability, alerts, audit, features, jobs, immutable audit chain,
-- ML economic value, and backtest experiments.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- Market data tables
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_snapshots (
    id SERIAL PRIMARY KEY,  -- SQLite: INTEGER PRIMARY KEY AUTOINCREMENT
    timestamp REAL NOT NULL,
    token_id TEXT NOT NULL,
    best_bid REAL,
    best_ask REAL,
    mid REAL,
    spread REAL,
    bid_size REAL DEFAULT 0,
    ask_size REAL DEFAULT 0,
    volume REAL DEFAULT 0,
    bids_json TEXT,  -- Full order book bids (JSON array)
    asks_json TEXT,  -- Full order book asks (JSON array)
    bid_depth_10 REAL DEFAULT 0,  -- Sum of top 10 bid levels
    ask_depth_10 REAL DEFAULT 0,  -- Sum of top 10 ask levels
    ingestion_time REAL
);
CREATE INDEX IF NOT EXISTS idx_ms_token_ts ON market_snapshots(token_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS market_trades (
    id SERIAL PRIMARY KEY,
    trade_id TEXT UNIQUE,
    token_id TEXT NOT NULL,
    price REAL NOT NULL,
    size REAL NOT NULL,
    side TEXT NOT NULL,
    timestamp REAL NOT NULL,
    ingestion_time REAL,
    maker_address TEXT,
    taker_order_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_mt_token_ts ON market_trades(token_id, timestamp DESC);


-- ----------------------------------------------------------------------------
-- Decision ledger (unified)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_events (
    id SERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    timestamp REAL NOT NULL,
    data_json TEXT,
    model_version TEXT
);
CREATE INDEX IF NOT EXISTS idx_de_corr ON decision_events(correlation_id);
CREATE INDEX IF NOT EXISTS idx_de_token_ts ON decision_events(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_de_stage ON decision_events(stage);

CREATE TABLE IF NOT EXISTS decision_rejections (
    id SERIAL PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    token_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    reason TEXT,
    risk_data TEXT,
    timestamp REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dr_corr ON decision_rejections(correlation_id);


-- ----------------------------------------------------------------------------
-- Execution quality
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS execution_quality (
    id SERIAL PRIMARY KEY,
    timestamp REAL NOT NULL,
    token_id TEXT,
    side TEXT,
    intended_price REAL,
    fill_price REAL,
    slippage_bps REAL,
    latency_ms REAL,
    realized_edge REAL,
    order_id TEXT,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_eq_token_ts ON execution_quality(token_id, timestamp DESC);


-- ----------------------------------------------------------------------------
-- Closed positions
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS closed_positions (
    id SERIAL PRIMARY KEY,
    token_id TEXT NOT NULL,
    position_id TEXT,
    side TEXT,
    entry_price REAL,
    exit_price REAL,
    size REAL,
    realized_pnl REAL,
    exit_reason TEXT,
    opened_at REAL,
    closed_at REAL NOT NULL,
    strategy TEXT,
    model_version TEXT,
    confidence REAL,
    predicted_edge REAL,
    p_yes REAL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_cp_closed_at ON closed_positions(closed_at DESC);
CREATE INDEX IF NOT EXISTS idx_cp_token ON closed_positions(token_id);


-- ----------------------------------------------------------------------------
-- Observability metrics
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS observability_metrics (
    id SERIAL PRIMARY KEY,
    category TEXT NOT NULL,
    name TEXT NOT NULL,
    value REAL,
    timestamp REAL NOT NULL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_cat_name_ts ON observability_metrics(category, name, timestamp DESC);


-- ----------------------------------------------------------------------------
-- Alerts
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
CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_sev_ack ON alerts(severity, acknowledged, timestamp DESC);


-- ----------------------------------------------------------------------------
-- Audit trail
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_events (
    id SERIAL PRIMARY KEY,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    slug TEXT,
    token_id TEXT,
    strategy TEXT,
    category TEXT,
    details TEXT,
    ingestion_time REAL
);
CREATE INDEX IF NOT EXISTS idx_ae_ts ON audit_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ae_type ON audit_events(event_type);


-- ----------------------------------------------------------------------------
-- Feature flags
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_flags (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL,
    description TEXT,
    config TEXT,
    updated_at REAL
);


-- ----------------------------------------------------------------------------
-- Feature store
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS feature_definitions (
    name TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    description TEXT,
    min_value REAL,
    max_value REAL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_values (
    id SERIAL PRIMARY KEY,
    token_id TEXT,
    feature_name TEXT NOT NULL,
    value REAL,
    timestamp REAL NOT NULL,
    prediction_id TEXT,
    FOREIGN KEY (feature_name) REFERENCES feature_definitions(name)
);
CREATE INDEX IF NOT EXISTS idx_fv_token_ts ON feature_values(token_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_fv_feature ON feature_values(feature_name);

CREATE TABLE IF NOT EXISTS feature_importance (
    id SERIAL PRIMARY KEY,
    feature_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    importance REAL NOT NULL,
    rank INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    UNIQUE(feature_name, model_version, timestamp)
);
CREATE INDEX IF NOT EXISTS idx_fi_version ON feature_importance(model_version);


-- ----------------------------------------------------------------------------
-- Job queue
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL,
    payload TEXT NOT NULL,
    result TEXT,
    error TEXT,
    progress REAL DEFAULT 0.0,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    worker_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_type ON jobs(job_type);


-- ----------------------------------------------------------------------------
-- Immutable audit chain
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_chain (
    entry_id SERIAL PRIMARY KEY,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    entry_hash TEXT NOT NULL,
    sequence INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ac_ts ON audit_chain(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ac_hash ON audit_chain(entry_hash);


-- ----------------------------------------------------------------------------
-- ML economic value
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ml_trade_attribution (
    id SERIAL PRIMARY KEY,
    trade_id TEXT,
    token_id TEXT,
    model_version TEXT,
    prediction REAL,
    confidence REAL,
    predicted_edge REAL,
    actual_pnl REAL,
    timestamp REAL,
    metadata TEXT
);
CREATE INDEX IF NOT EXISTS idx_mla_version ON ml_trade_attribution(model_version);
CREATE INDEX IF NOT EXISTS idx_mla_conf ON ml_trade_attribution(confidence);


-- ----------------------------------------------------------------------------
-- Backtest experiments
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    strategy_version TEXT,
    start_time REAL,
    end_time REAL,
    initial_capital REAL,
    final_equity REAL,
    total_return REAL,
    sharpe REAL,
    sortino REAL,
    calmar REAL,
    max_drawdown REAL,
    win_rate REAL,
    profit_factor REAL,
    n_trades INTEGER,
    config TEXT,
    created_at REAL,
    equity_curve TEXT,
    trades TEXT
);
CREATE INDEX IF NOT EXISTS idx_exp_strategy ON experiments(strategy);
CREATE INDEX IF NOT EXISTS idx_exp_created ON experiments(created_at DESC);
