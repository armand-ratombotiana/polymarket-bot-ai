-- ============================================================================
-- Migration 001: Initial Enterprise Schemas & Canonical Data Platform
-- Polymarket Data Intelligence, Event Knowledge, AI/ML, Risk & Execution
-- ============================================================================

-- 1. Enable TimescaleDB and pgvector extensions
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 2. Create 15 Logical Schemas
CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS reference;
CREATE SCHEMA IF NOT EXISTS market;
CREATE SCHEMA IF NOT EXISTS news;
CREATE SCHEMA IF NOT EXISTS intelligence;
CREATE SCHEMA IF NOT EXISTS feature;
CREATE SCHEMA IF NOT EXISTS ml;
CREATE SCHEMA IF NOT EXISTS strategy;
CREATE SCHEMA IF NOT EXISTS trading;
CREATE SCHEMA IF NOT EXISTS risk;
CREATE SCHEMA IF NOT EXISTS accounting;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS operations;
CREATE SCHEMA IF NOT EXISTS simulation;

-- ============================================================================
-- OPERATIONS & SCHEMA MIGRATION TRACKING
-- ============================================================================
CREATE TABLE IF NOT EXISTS operations.schema_migration (
    version VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    checksum VARCHAR(64) NOT NULL,
    execution_time_ms DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS operations.structured_log (
    time TIMESTAMPTZ NOT NULL,
    level VARCHAR(16) NOT NULL,
    service VARCHAR(64) NOT NULL,
    module VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    message TEXT NOT NULL,
    correlation_id VARCHAR(64),
    context JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
SELECT create_hypertable('operations.structured_log', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_log_corr ON operations.structured_log (correlation_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_log_level ON operations.structured_log (level, time DESC);

CREATE TABLE IF NOT EXISTS operations.system_metric (
    time TIMESTAMPTZ NOT NULL,
    service VARCHAR(64) NOT NULL,
    metric_name VARCHAR(64) NOT NULL,
    metric_value DOUBLE PRECISION NOT NULL,
    tags JSONB
);
SELECT create_hypertable('operations.system_metric', 'time', if_not_exists => TRUE);

-- ============================================================================
-- RAW INGESTION VAULT & SOURCE REGISTRY
-- ============================================================================
CREATE TABLE IF NOT EXISTS raw.source_registry (
    source_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    domain VARCHAR(128) NOT NULL,
    source_type VARCHAR(32) NOT NULL, -- clob_rest, clob_ws, gamma_api, rss, gdelt, cryptopanic
    endpoint_url TEXT NOT NULL,
    rate_limit_rps DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    credibility_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    records_observed BIGINT NOT NULL DEFAULT 0,
    records_accepted BIGINT NOT NULL DEFAULT 0,
    records_errored BIGINT NOT NULL DEFAULT 0,
    last_success_at TIMESTAMPTZ,
    last_error_at TIMESTAMPTZ,
    last_error_msg TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS raw.raw_observation (
    observation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id VARCHAR(64) NOT NULL REFERENCES raw.source_registry(source_id),
    payload_checksum VARCHAR(64) NOT NULL,
    content_type VARCHAR(32) NOT NULL DEFAULT 'application/json',
    raw_payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    parse_status VARCHAR(32) NOT NULL DEFAULT 'PENDING', -- PENDING, PARSED, CORRUPTED, QUARANTINED
    error_details TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_source_time ON raw.raw_observation (source_id, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_checksum ON raw.raw_observation (payload_checksum);

CREATE TABLE IF NOT EXISTS raw.dead_letter_record (
    dead_letter_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id VARCHAR(64) NOT NULL,
    raw_payload TEXT NOT NULL,
    error_class VARCHAR(128) NOT NULL,
    error_message TEXT NOT NULL,
    stack_trace TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- REFERENCE DATA (Events, Markets, Outcomes)
-- ============================================================================
CREATE TABLE IF NOT EXISTS reference.event (
    event_id VARCHAR(64) PRIMARY KEY,
    ticker VARCHAR(64),
    title TEXT NOT NULL,
    description TEXT,
    category VARCHAR(64),
    tags TEXT[],
    start_date TIMESTAMPTZ,
    end_date TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS reference.market (
    condition_id VARCHAR(128) PRIMARY KEY,
    event_id VARCHAR(64) REFERENCES reference.event(event_id),
    slug VARCHAR(255) NOT NULL,
    question TEXT NOT NULL,
    description TEXT,
    market_type VARCHAR(32) NOT NULL DEFAULT 'binary',
    yes_token_id VARCHAR(128) NOT NULL,
    no_token_id VARCHAR(128),
    min_tick_size DOUBLE PRECISION NOT NULL DEFAULT 0.001,
    min_order_size DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    volume_24h DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    liquidity DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_resolved BOOLEAN NOT NULL DEFAULT FALSE,
    winning_outcome VARCHAR(16), -- YES, NO, or custom
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_market_slug ON reference.market (slug);
CREATE INDEX IF NOT EXISTS idx_market_tokens ON reference.market (yes_token_id, no_token_id);

-- ============================================================================
-- MARKET MICROSTRUCTURE (Hypertables & Real-Time Books)
-- ============================================================================
CREATE TABLE IF NOT EXISTS market.orderbook_snapshot (
    time TIMESTAMPTZ NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    slug VARCHAR(255),
    best_bid DOUBLE PRECISION,
    best_ask DOUBLE PRECISION,
    mid DOUBLE PRECISION,
    spread DOUBLE PRECISION,
    bid_depth_10 DOUBLE PRECISION DEFAULT 0.0,
    ask_depth_10 DOUBLE PRECISION DEFAULT 0.0,
    volume_24h DOUBLE PRECISION DEFAULT 0.0,
    liquidity DOUBLE PRECISION DEFAULT 0.0,
    bids_json JSONB,
    asks_json JSONB
);
SELECT create_hypertable('market.orderbook_snapshot', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_book_token_time ON market.orderbook_snapshot (token_id, time DESC);

CREATE TABLE IF NOT EXISTS market.orderbook_tick (
    time TIMESTAMPTZ NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    best_bid_size DOUBLE PRECISION NOT NULL,
    best_ask_size DOUBLE PRECISION NOT NULL,
    ofi DOUBLE PRECISION NOT NULL,
    micro_price DOUBLE PRECISION NOT NULL
);
SELECT create_hypertable('market.orderbook_tick', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_tick_token_time ON market.orderbook_tick (token_id, time DESC);

CREATE TABLE IF NOT EXISTS market.market_trade (
    time TIMESTAMPTZ NOT NULL,
    trade_id VARCHAR(128) NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    side VARCHAR(8) NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    maker_address VARCHAR(128),
    taker_address VARCHAR(128)
);
SELECT create_hypertable('market.market_trade', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_trade_token_time ON market.market_trade (token_id, time DESC);

-- ============================================================================
-- NEWS & EVENT INTELLIGENCE
-- ============================================================================
CREATE TABLE IF NOT EXISTS news.news_document (
    document_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_id VARCHAR(64) NOT NULL REFERENCES raw.source_registry(source_id),
    external_id VARCHAR(255),
    headline TEXT NOT NULL,
    body TEXT,
    url TEXT,
    author VARCHAR(128),
    publisher VARCHAR(128),
    category VARCHAR(64),
    sentiment_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    credibility_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    novelty_score DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    matched_token_ids TEXT[],
    published_at TIMESTAMPTZ NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news.news_document (published_at DESC);

CREATE TABLE IF NOT EXISTS intelligence.entity (
    entity_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    entity_type VARCHAR(32) NOT NULL, -- PERSON, ORG, GPE, ASSET, PROTOCOL
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS intelligence.event_cluster (
    cluster_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    summary TEXT,
    confidence DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    first_seen_at TIMESTAMPTZ NOT NULL,
    last_updated_at TIMESTAMPTZ NOT NULL,
    affected_tokens TEXT[],
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- FEATURE STORE (Point-In-Time Features)
-- ============================================================================
CREATE TABLE IF NOT EXISTS feature.feature_definition (
    feature_id VARCHAR(64) PRIMARY KEY,
    feature_name VARCHAR(128) NOT NULL,
    feature_family VARCHAR(32) NOT NULL, -- microstructure, cross_market, sentiment, execution
    data_type VARCHAR(16) NOT NULL DEFAULT 'float',
    window_seconds INTEGER NOT NULL DEFAULT 0,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS feature.feature_value (
    time TIMESTAMPTZ NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    feature_id VARCHAR(64) NOT NULL REFERENCES feature.feature_definition(feature_id),
    val DOUBLE PRECISION NOT NULL,
    available_at TIMESTAMPTZ NOT NULL
);
SELECT create_hypertable('feature.feature_value', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_feat_token_time ON feature.feature_value (token_id, feature_id, time DESC);

CREATE TABLE IF NOT EXISTS feature.feature_snapshot (
    snapshot_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_id VARCHAR(128) NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    features_array DOUBLE PRECISION[] NOT NULL,
    feature_names TEXT[] NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_featsnap_token_time ON feature.feature_snapshot (token_id, time DESC);

-- ============================================================================
-- MACHINE LEARNING (Labels, Registry, Predictions, Drift)
-- ============================================================================
CREATE TABLE IF NOT EXISTS ml.model_registry (
    model_id VARCHAR(64) PRIMARY KEY,
    version VARCHAR(32) NOT NULL,
    algorithm VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'CANDIDATE', -- RESEARCH, CANDIDATE, VALIDATED, CHAMPION, RETIRED
    hyperparameters JSONB NOT NULL,
    metrics JSONB NOT NULL,
    brier_score DOUBLE PRECISION,
    log_loss DOUBLE PRECISION,
    roc_auc DOUBLE PRECISION,
    ece DOUBLE PRECISION,
    n_training_samples INTEGER NOT NULL,
    trained_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    promoted_at TIMESTAMPTZ,
    artifact_path TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ml.prediction (
    prediction_id UUID DEFAULT uuid_generate_v4(),
    model_id VARCHAR(64) NOT NULL REFERENCES ml.model_registry(model_id),
    token_id VARCHAR(128) NOT NULL,
    time TIMESTAMPTZ NOT NULL,
    feature_snapshot_id UUID REFERENCES feature.feature_snapshot(snapshot_id),
    raw_probability DOUBLE PRECISION NOT NULL,
    calibrated_probability DOUBLE PRECISION NOT NULL,
    confidence DOUBLE PRECISION NOT NULL,
    trading_permission BOOLEAN NOT NULL DEFAULT FALSE,
    actual_outcome INTEGER, -- 1=YES, 0=NO, populated upon settlement
    outcome_resolved_at TIMESTAMPTZ,
    PRIMARY KEY (prediction_id, time)
);
SELECT create_hypertable('ml.prediction', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_pred_token_time ON ml.prediction (token_id, time DESC);

CREATE TABLE IF NOT EXISTS ml.drift_observation (
    id SERIAL PRIMARY KEY,
    model_id VARCHAR(64) NOT NULL REFERENCES ml.model_registry(model_id),
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    psi_score DOUBLE PRECISION NOT NULL,
    drift_status VARCHAR(32) NOT NULL, -- STABLE, MODERATE_DRIFT, SIGNIFICANT_DRIFT
    feature_drift JSONB
);

-- ============================================================================
-- STRATEGY & DECISION ENGINE
-- ============================================================================
CREATE TABLE IF NOT EXISTS strategy.strategy_registry (
    strategy_id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(128) NOT NULL,
    family VARCHAR(32) NOT NULL,
    implementation_status VARCHAR(32) NOT NULL DEFAULT 'NOT_IMPLEMENTED', -- IMPLEMENTED, RESEARCH_STUB, RETIRED
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    max_capital_allocation DOUBLE PRECISION NOT NULL DEFAULT 15.0,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS strategy.strategy_decision (
    decision_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    strategy_id VARCHAR(64) NOT NULL REFERENCES strategy.strategy_registry(strategy_id),
    token_id VARCHAR(128) NOT NULL,
    action VARCHAR(16) NOT NULL, -- BUY, SELL, HOLD, CANCEL
    side VARCHAR(8),
    target_price DOUBLE PRECISION,
    target_size DOUBLE PRECISION,
    gross_edge DOUBLE PRECISION,
    est_slippage DOUBLE PRECISION,
    est_fees DOUBLE PRECISION,
    net_edge DOUBLE PRECISION,
    confidence DOUBLE PRECISION,
    decision_reason TEXT,
    decision_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prediction_id UUID
);
CREATE INDEX IF NOT EXISTS idx_strat_dec_time ON strategy.strategy_decision (strategy_id, decision_at DESC);

-- ============================================================================
-- FORMAL RISK SAFETY KERNEL
-- ============================================================================
CREATE TABLE IF NOT EXISTS risk.risk_configuration (
    config_id SERIAL PRIMARY KEY,
    operating_capital_usd DOUBLE PRECISION NOT NULL DEFAULT 100.0,
    absolute_bankroll_ceiling_usd DOUBLE PRECISION NOT NULL DEFAULT 200.0,
    max_order_size_usd DOUBLE PRECISION NOT NULL DEFAULT 3.0,
    max_market_exposure_usd DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    max_total_exposure_usd DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    max_open_orders INTEGER NOT NULL DEFAULT 8,
    daily_loss_stop_usd DOUBLE PRECISION NOT NULL DEFAULT 2.0,
    weekly_loss_stop_usd DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    max_drawdown_pct DOUBLE PRECISION NOT NULL DEFAULT 0.15,
    min_liquidity_usd DOUBLE PRECISION NOT NULL DEFAULT 50.0,
    max_spread DOUBLE PRECISION NOT NULL DEFAULT 0.10,
    data_freshness_sec DOUBLE PRECISION NOT NULL DEFAULT 10.0,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk.risk_decision (
    risk_decision_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    decision_id UUID REFERENCES strategy.strategy_decision(decision_id),
    token_id VARCHAR(128) NOT NULL,
    order_size_usd DOUBLE PRECISION NOT NULL,
    is_allowed BOOLEAN NOT NULL,
    checks_evaluated JSONB NOT NULL,
    failed_check VARCHAR(64),
    rejection_reason TEXT,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS risk.kill_switch_event (
    event_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    action VARCHAR(16) NOT NULL, -- ACTIVATED, DEACTIVATED
    actor VARCHAR(64) NOT NULL,
    reason TEXT NOT NULL,
    orders_cancelled_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- TRADING, ORDERS & EXECUTION
-- ============================================================================
CREATE TABLE IF NOT EXISTS trading.order_intent (
    intent_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    decision_id UUID REFERENCES strategy.strategy_decision(decision_id),
    risk_decision_id UUID REFERENCES risk.risk_decision(risk_decision_id),
    strategy_id VARCHAR(64) NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    side VARCHAR(8) NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    order_type VARCHAR(16) NOT NULL DEFAULT 'GTC',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trading.order (
    internal_order_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    intent_id UUID REFERENCES trading.order_intent(intent_id),
    client_order_id VARCHAR(64) UNIQUE NOT NULL,
    exchange_order_id VARCHAR(128),
    strategy_id VARCHAR(64) NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    side VARCHAR(8) NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    original_size DOUBLE PRECISION NOT NULL,
    remaining_size DOUBLE PRECISION NOT NULL,
    filled_size DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    status VARCHAR(32) NOT NULL DEFAULT 'SUBMITTED', -- SUBMITTED, OPEN, PARTIALLY_FILLED, FILLED, CANCELLED, REJECTED
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_order_status ON trading.order (status, token_id);

CREATE TABLE IF NOT EXISTS trading.order_transition (
    transition_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    internal_order_id UUID NOT NULL REFERENCES trading.order(internal_order_id),
    from_status VARCHAR(32) NOT NULL,
    to_status VARCHAR(32) NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trading.fill (
    fill_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    internal_order_id UUID NOT NULL REFERENCES trading.order(internal_order_id),
    exchange_fill_id VARCHAR(128) UNIQUE NOT NULL,
    token_id VARCHAR(128) NOT NULL,
    side VARCHAR(8) NOT NULL,
    price DOUBLE PRECISION NOT NULL,
    size DOUBLE PRECISION NOT NULL,
    fee_usd DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_maker BOOLEAN NOT NULL DEFAULT TRUE,
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
    filled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_fill_token_time ON trading.fill (token_id, filled_at DESC);

-- ============================================================================
-- ACCOUNTING, SETTLEMENT & LEDGER
-- ============================================================================
CREATE TABLE IF NOT EXISTS accounting.cash_ledger (
    entry_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    entry_type VARCHAR(32) NOT NULL, -- DEPOSIT, TRADE_BUY, TRADE_SELL, FEE, SETTLEMENT_PAYOUT
    amount_usd DOUBLE PRECISION NOT NULL,
    balance_after_usd DOUBLE PRECISION NOT NULL,
    reference_id UUID,
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS accounting.position_lot (
    lot_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    token_id VARCHAR(128) NOT NULL,
    outcome VARCHAR(16) NOT NULL DEFAULT 'YES',
    shares DOUBLE PRECISION NOT NULL,
    avg_entry_price DOUBLE PRECISION NOT NULL,
    total_invested DOUBLE PRECISION NOT NULL,
    realized_pnl DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    is_closed BOOLEAN NOT NULL DEFAULT FALSE,
    mode VARCHAR(16) NOT NULL DEFAULT 'paper',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pos_lot_token ON accounting.position_lot (token_id, outcome, is_closed);

CREATE TABLE IF NOT EXISTS accounting.settlement (
    settlement_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    condition_id VARCHAR(128) NOT NULL,
    winning_token_id VARCHAR(128),
    winning_outcome VARCHAR(16) NOT NULL,
    payout_per_share DOUBLE PRECISION NOT NULL,
    total_payout_usd DOUBLE PRECISION NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS accounting.reconciliation_run (
    reconciliation_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    status VARCHAR(32) NOT NULL, -- RECONCILED, DISCREPANCY_DETECTED, RESOLVED
    internal_balance_usd DOUBLE PRECISION NOT NULL,
    exchange_balance_usd DOUBLE PRECISION NOT NULL,
    internal_open_orders INTEGER NOT NULL,
    exchange_open_orders INTEGER NOT NULL,
    discrepancy_details JSONB,
    performed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- CONTINUOUS AGGREGATES FOR REAL OHLCV (Replacing synthetic candles)
-- ============================================================================
CREATE MATERIALIZED VIEW IF NOT EXISTS market.price_candle_1m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 minute', time) AS bucket,
    token_id,
    FIRST(mid, time) AS open,
    MAX(mid) AS high,
    MIN(mid) AS low,
    LAST(mid, time) AS close,
    AVG(mid) AS vwap,
    COUNT(*) AS tick_count
FROM market.orderbook_snapshot
WHERE mid IS NOT NULL
GROUP BY bucket, token_id
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market.price_candle_5m
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', time) AS bucket,
    token_id,
    FIRST(mid, time) AS open,
    MAX(mid) AS high,
    MIN(mid) AS low,
    LAST(mid, time) AS close,
    AVG(mid) AS vwap,
    COUNT(*) AS tick_count
FROM market.orderbook_snapshot
WHERE mid IS NOT NULL
GROUP BY bucket, token_id
WITH NO DATA;

CREATE MATERIALIZED VIEW IF NOT EXISTS market.price_candle_1h
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    token_id,
    FIRST(mid, time) AS open,
    MAX(mid) AS high,
    MIN(mid) AS low,
    LAST(mid, time) AS close,
    AVG(mid) AS vwap,
    COUNT(*) AS tick_count
FROM market.orderbook_snapshot
WHERE mid IS NOT NULL
GROUP BY bucket, token_id
WITH NO DATA;

-- ============================================================================
-- INITIAL SEED RECORDS (ON CONFLICT DO NOTHING)
-- ============================================================================
INSERT INTO raw.source_registry (source_id, name, domain, source_type, endpoint_url, is_active)
VALUES 
    ('clob_rest', 'Polymarket CLOB REST API', 'clob.polymarket.com', 'clob_rest', 'https://clob.polymarket.com', TRUE),
    ('clob_ws', 'Polymarket CLOB WebSocket', 'ws-subscriptions-clob.polymarket.com', 'clob_ws', 'wss://ws-subscriptions-clob.polymarket.com/ws/market', TRUE),
    ('gamma_api', 'Polymarket Gamma Discovery API', 'gamma-api.polymarket.com', 'gamma_api', 'https://gamma-api.polymarket.com', TRUE)
ON CONFLICT (source_id) DO NOTHING;

INSERT INTO ml.model_registry (
    model_id, version, algorithm, status, hyperparameters, metrics,
    n_training_samples, artifact_path
)
VALUES (
    'champion_ensemble_v1', '1.0.0', 'Ensemble(RF+GB+SGD)', 'CHAMPION',
    '{"n_estimators": 50, "loss": "log_loss"}'::jsonb,
    '{"brier_score": 0.0645, "accuracy": 0.94}'::jsonb,
    1000, '/app/data/model.pkl'
)
ON CONFLICT (model_id) DO NOTHING;

INSERT INTO strategy.strategy_registry (
    strategy_id, name, family, implementation_status, is_active, max_capital_allocation
)
VALUES 
    ('market_maker', 'Microstructure Market Maker', 'MARKET_MAKING', 'IMPLEMENTED', TRUE, 15.0),
    ('arb_scanner', 'Cross-Outcome Arbitrage Scanner', 'ARBITRAGE', 'IMPLEMENTED', TRUE, 15.0),
    ('signal_trader', 'AI Directional Signal Trader', 'DIRECTIONAL', 'IMPLEMENTED', TRUE, 15.0)
ON CONFLICT (strategy_id) DO NOTHING;

