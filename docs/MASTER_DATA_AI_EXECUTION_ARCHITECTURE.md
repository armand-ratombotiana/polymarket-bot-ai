# Master Data, AI/ML, Risk & Execution Architecture

## 1. Executive Summary

The Polymarket Algorithmic Trading Workstation operates as an institutional-grade, point-in-time-correct, event-driven trading intelligence and execution operating system anchored in **PostgreSQL / TimescaleDB**.

Every observation, news event, feature vector, model prediction, strategy decision, risk check, order state transition, fill, position lot, settlement, and financial ledger transaction is durably persisted and mathematically auditable.

```mermaid
flowchart TD
    subgraph Sources [1. External Ingestion & Provenance]
        CLOB_REST[Polymarket CLOB REST]
        CLOB_WS[Polymarket CLOB WebSocket]
        GAMMA[Gamma Discovery API]
        NEWS[Verified RSS / News Feeds]
    end

    subgraph RawVault [2. Raw Data Vault]
        RAW_OBS[raw.raw_observation <br/> SHA-256 Hash + Payload]
        DEAD_LETTER[raw.dead_letter_record]
        SRC_REG[raw.source_registry]
    end

    subgraph CanonicalDB [3. Canonical TimescaleDB Platform]
        MKT_SNAP[market.orderbook_snapshot]
        MKT_TICK[market.orderbook_tick]
        NEWS_DOC[news.news_document]
        FEAT_SNAP[feature.feature_snapshot]
        ML_PRED[ml.prediction]
    end

    subgraph DecisionRisk [4. Strategy & Formal Risk Kernel]
        STRAT[strategy.strategy_decision]
        RISK_KERNEL[risk.risk_decision <br/> $100 Capital / $200 Ceiling]
        KILL_SWITCH[risk.kill_switch_event]
    end

    subgraph ExecutionLedger [5. Execution & Double-Entry Accounting]
        ORDER_SM[trading.order & order_transition]
        FILLS[trading.fill]
        LEDGER[accounting.cash_ledger & position_lot]
        SETTLE[accounting.settlement]
    end

    Sources --> RawVault
    RawVault --> CanonicalDB
    CanonicalDB --> DecisionRisk
    DecisionRisk --> ExecutionLedger
```

---

## 2. Core Architecture Tenets

1. **PostgreSQL / TimescaleDB as the System of Record**:
   - 15 logical schemas isolate data boundaries.
   - Hypertables partition high-velocity time series with zero insert degradation.
2. **Point-in-Time Truth & Anti-Lookahead Isolation**:
   - Features, predictions, and datasets strictly enforce `available_at <= decision_at`.
3. **Formal Risk Safety Kernel**:
   - Hardcoded capital limits: **$100 Operating Capital, $200 Absolute Ceiling, ~$3.00 Max Order Size, $2.00 Daily Loss Stop, $10.00 Weekly Loss Stop**.
   - Fail-closed invariant enforcement.
4. **Event-Sourced Order Execution & Double-Entry Accounting**:
   - Fully auditable state machine with idempotent fills and exact decimal balance conservation.
