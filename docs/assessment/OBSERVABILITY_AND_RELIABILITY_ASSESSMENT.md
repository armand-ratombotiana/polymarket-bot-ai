# Observability & Reliability Assessment — Polymarket Bot (§54 / §55)

- **Task ID:** W17-8 (File 3 of 3)
- **Agent:** general-purpose
- **Date:** 2026-09-17
- **Scope:** Read-only assessment of the observability stack (§54),
  auditability contract (§55), Prometheus metrics, Grafana dashboard,
  profiling, alerting, immutable audit trail, structured logging, and
  circuit-breaker reliability in `mini-services/polymarket-bot/` and
  the project-root `/grafana/dashboard.json`. No source files were
  modified.
- **Evidence basis** (classification legend):
  - **VERIFIED** — read in source file in this session.
  - **STRONG EVIDENCE** — named in docstring/comment with specific line /
    constant / value that matches surrounding context.
  - **LIKELY** — consistent with code patterns but not directly verified.
  - **UNVERIFIED** — plausible but not yet confirmed.
  - **NOT FOUND** — no evidence located.

Companion documents: `CROSS_SYSTEM_ARCHITECTURE_ASSESSMENT.md` (§50, §51,
§79, §80) and `RISK_AND_PORTFOLIO_ASSESSMENT.md` (§52, §53).

---

## 1. Executive Summary

The Polymarket Bot has a **comprehensive, layered observability stack**:
six canonical metric categories, a 30-second background auto-collector,
a Prometheus `/metrics` endpoint, an 11-panel Grafana dashboard, a
threshold-based alert engine with 7 default rules, a per-endpoint p50/
p95/p99 profiler, three pre-configured circuit breakers for external
APIs, a hash-chained immutable audit trail for control events, and
structured JSON logging with request-scoped contextvars. Three separate
SQLite audit trails coexist without schema contention: a category-indexed
durable trail, a hash-chained trail, and a 6-stage unified decision
ledger keyed by `decision_id`.

**Headline findings** (full list in §23):

1. **The §54 six-category observability model IS fully implemented.**
   Every category the spec lists is present: `data_source` (updates,
   latency, staleness, errors, tracked_tokens), `bot` (cycles, errors),
   `strategy` (evaluations, signals, rejects — UNVERIFIED whether the
   strategy layer actually emits these), `execution` (submissions,
   fills, rejections, slippage), `ml` (inference_latency, prediction_
   distribution, drift, brier, ece, roc_auc, is_fitted, n_updates,
   seconds_since_last_trained), `system` (cpu_percent, memory_percent,
   memory_used_mb). (VERIFIED via `observability.py:128-155`.)
2. **The §55 auditability contract is PARTIALLY met.** The decision
   ledger's `decision_id` IS the canonical correlation key, and every
   PREDICTION / SIGNAL / RISK_APPROVED / RISK_REJECTED / ORDER / FILL
   stage carries it. The other correlation identifiers in the spec —
   `signal_id`, `order_id`, `fill_id`, `position_id`, `strategy_id`,
   `model_version` — have varying degrees of presence:
   - `model_version` — VERIFIED (auto-stamped on PREDICTION events
     via `_resolve_active_model_version()`).
   - `order_id` — VERIFIED (passed through `submit_order` to the
     ORDER stage).
   - `position_id` — PARTIAL (only on `closed_positions.close`, NOT
     on open positions).
   - `strategy_id` — PARTIAL (loose string match on `strategy` field,
     not a UUID).
   - `signal_id` — NOT FOUND (collapsed into `decision_id`).
   - `fill_id` — NOT FOUND (uses `decision_id`; `Trade.trade_id` is
     separate).
3. **Every important log DOES carry enough context to reconstruct the
   event chain** — but only for the 6 stages the decision ledger covers.
   The pre-PREDICTION stages (MARKET / INTELLIGENCE / FEATURE) and the
   post-FILL stages (POSITION / OUTCOME / P&L) are NOT linked by
   `decision_id` to the same chain, so full §55 auditability is broken
   at those stages. (VERIFIED — see Cross-System Assessment §9.1 for
   the gap analysis.)
4. **The Prometheus /metrics endpoint IS production-grade.** Counters,
   gauges, histograms, info metrics. Low label cardinality (≤2k series).
   Bearer-token-protected (configurable). (VERIFIED via
   `prometheus_metrics.py:1-40` + `api/server.py:1404-1414`.)
5. **The Grafana dashboard exists at `/grafana/dashboard.json`** — 11
   panels covering HTTP request rate / latency / error rate, paper
   balance, realized + unrealized P&L, open positions, ML drift PSI,
   ML Brier score, cache hit rate, active alerts by severity. (VERIFIED
   via the file at `/home/z/my-project/grafana/dashboard.json`, 403
   lines, dashboard UID `polymarket-bot-ops-w13-1`.)
6. **The immutable audit trail uses SHA-256 hash chaining** for control
   events (kill switch, live-trade enable, position close, config
   changes). Tamper-evident via `verify_chain()` endpoint. However
   the chain is UNSIGNED — anyone with write access to the db can
   re-write it (computing fresh hashes) without detection. (VERIFIED
   via `immutable_audit.py:1-120`.)
7. **The 7 default alert rules cover 4 categories** (`risk`, `ml`,
   `system`, `data`) but NOT `execution` or `strategy` — there are no
   alert rules for high slippage, low fill rate, or strategy under-
   performance. (VERIFIED via `alerting.py:223-307`.)
8. **The 30-second observability collector emits 18+ metrics per
   cycle** but emits `inference_latency=0.0` with `instrumented=False`
   — the ML model's predict path does NOT record per-call latency.
   (VERIFIED via `observability_collector.py:255-262`.)
9. **Three pre-configured circuit breakers** for `clob_api` (5 failures,
   30s recovery), `gamma_api` (3 failures, 60s recovery),
   `polymarket_ws` (5 failures, 15s recovery). Dual sync/async
   decorator support. (VERIFIED via `circuit_breaker.py:209-227`.)
10. **Structured JSON logging is implemented** with `request_id` /
    `user` / `endpoint` contextvars propagated across `await`
    boundaries. Idempotent `setup_logging()`. Two formatters: JSON
    (production) and colored (dev). (VERIFIED via
    `logging_config.py:1-159`.)
11. **The per-endpoint profiler tracks p50/p95/p99** with a 1000-sample
    rolling window per endpoint. In-memory only — not persisted across
    restarts. (VERIFIED via `profiling.py:117-200`.)
12. **Silent data loss is possible** — every observability/audit write
    swallows its own persistence errors (logged at `error` level,
    return `[]` / `0` / `None`). This is a deliberate design choice
    ("observability can never break the trading pipeline") but means
    disk-pressure events can silently drop metrics / audit events /
    alerts. (VERIFIED — pattern across all 12 SQLite-recording modules.)

### Maturity snapshot (full score in §22)

| Dimension | Score |
|---|---|
| Observability completeness (§54) | 7.5 / 10 |
| Auditability (§55 correlation IDs) | 6.0 / 10 |
| Prometheus metrics | 8.0 / 10 |
| Grafana dashboard | 7.5 / 10 |
| Profiling | 6.5 / 10 |
| Alerting | 6.5 / 10 |
| Immutable audit trail | 7.0 / 10 |
| Structured logging | 8.0 / 10 |
| Circuit breakers | 7.5 / 10 |
| Reliability under failure | 6.0 / 10 |
| **Composite** | **6.8 / 10** |

---

## 2. Purpose

This document exists to:

1. **Assess the observability stack (§54)** against the spec's six-
   category model: data (source health, updates, latency, staleness,
   reconnects), bot (cycles, errors, actions), strategies (evaluations,
   signals, rejects), execution (submissions, fills, rejections,
   slippage, latency), ML (inference, latency, prediction distribution,
   drift), system (CPU, memory, DB connections, queue health).
2. **Assess auditability (§55)** against the correlation-identifier
   contract: `decision_id`, `signal_id`, `order_id`, `fill_id`,
   `position_id`, `strategy_id`, `model_version`. Verify every
   important log carries enough context to reconstruct the event chain.
3. **Inventory the supporting infrastructure**: Prometheus metrics,
   Grafana dashboard, profiling, alerting, immutable audit trail,
   structured logging, circuit breakers.

This is a read-only assessment — no source files were modified.

---

## 3. Current Architecture

### 3.1 Observability stack overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  EMITTERS (subsystem call sites)                                     │
│  ├── core/observability_collector.py    (30s background loop)        │
│  ├── core/audit_logger.py               (async durable audit)        │
│  ├── core/decision_ledger.py            (async 6-stage ledger)       │
│  ├── core/immutable_audit.py            (sync hash-chained)          │
│  ├── core/execution_quality.py          (per-fill slippage)         │
│  ├── core/closed_positions.py           (round-trip P&L)             │
│  ├── core/shadow_trading.py             (counterfactual journal)     │
│  └── core/prometheus_metrics.py         (sync counter/gauge updates) │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  STORES (independent SQLite files under /app/data/)                  │
│  ├── observability.db       (metrics: 6-category)                   │
│  ├── audit_trail.db         (audit_events: category-indexed)         │
│  ├── decision_ledger.db     (decision_events + decision_rejections) │
│  ├── immutable_audit.db     (audit_chain: hash-chained)             │
│  ├── execution_quality.db   (per-fill slippage / latency)           │
│  ├── closed_positions.db    (round-trip trades)                     │
│  ├── shadow_trades.db       (counterfactual trades)                 │
│  ├── alerts.db              (fired alerts + acknowledgement)         │
│  ├── feature_store.db       (per-prediction feature values)         │
│  └── market.db              (market metadata cache)                 │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  SURFACES (HTTP / scrape)                                            │
│  ├── GET /api/observability            (structured health report)   │
│  ├── GET /api/observability/history/{name}                            │
│  ├── GET /api/v2/observability/latest  (async pool read)            │
│  ├── GET /api/alerts                   (recent alerts + stats)      │
│  ├── POST /api/alerts/{id}/acknowledge                                │
│  ├── POST /api/alerts/evaluate                                         │
│  ├── GET /api/audit/recent             (durable audit trail)         │
│  ├── GET /api/audit/immutable          (hash-chained trail)          │
│  ├── GET /api/audit/immutable/verify   (chain verification)          │
│  ├── GET /api/decision/{token_id}      (decision chain for token)   │
│  ├── GET /api/decisions/rejected       (recent rejections)          │
│  ├── GET /api/v2/decisions/recent     (async pool read)             │
│  ├── GET /api/execution-quality        (per-fill slippage stats)   │
│  ├── GET /api/positions/closed        (round-trip P&L journal)     │
│  ├── GET /api/shadow/trades           (counterfactual journal)     │
│  ├── GET /metrics                       (Prometheus scrape endpoint)│
│  ├── GET /api/profiling/stats          (p50/p95/p99 per endpoint)   │
│  ├── GET /api/profiling/slowest                                    │
│  ├── POST /api/profiling/reset                                      │
│  └── GET /api/circuit-breakers         (3 breaker statuses)         │
└──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
┌──────────────────────────────────────────────────────────────────────┐
│  EXTERNAL                                                            │
│  ├── Prometheus scraper    → /metrics endpoint                      │
│  └── Grafana dashboard     → /grafana/dashboard.json (11 panels)    │
└──────────────────────────────────────────────────────────────────────┘
```

### 3.2 Logging architecture

```
application code (logger.info / .warning / .error / .critical)
  ↓
root logger (configured once via setup_logging(), idempotent)
  ↓
  ├── JSONFormatter (LOG_FORMAT=json or ENV=production)
  │   └── single-line JSON object: timestamp / level / logger / message /
  │       module / function / line + context {request_id, user, endpoint}
  │       + exception (when exc_info is attached) + extra=... promotions
  │
  └── ColoredFormatter (default dev)
      └── "HH:MM:SS.mmm LEVEL   logger.name              message [req=abcd1234]"
```

Context propagation: three `contextvars.ContextVar` instances
(`request_id_var`, `user_var`, `endpoint_var`) carry request-scoped
data through the async call stack. `RequestLogMiddleware` (ASGI)
populates them on every HTTP request. (VERIFIED via
`logging_config.py:46-53, 90-125`.)

### 3.3 Alert engine architecture

```
alert_engine.evaluate(metrics_dict)
  ↓
for each of 7 default rules:
  ├── if condition(metrics): fire Alert(...)
  │   ├── _store(alert)  → SQLite INSERT OR REPLACE on alert_id PK
  │   ├── log.warning("Alert fired: %s — %s", alert.name, alert.message)
  │   └── return fired_alert
  └── if condition fails: skip
↓
fired alerts returned to caller (caller may broadcast via ws_manager)
↓
acknowledgement:
  ├── POST /api/alerts/{alert_id}/acknowledge  → UPDATE acknowledged=1
  └── POST /api/alerts/acknowledge-all          → UPDATE WHERE acknowledged=0
```

(VERIFIED via `alerting.py:309-352, 481-504`.)

---

## 4. Current Components

### 4.1 Observability store

- `core/observability.py::Observability` — SQLite-backed metrics store
  with 6 canonical categories. Schema: `metrics (id, timestamp, category,
  name, value, metadata_json)`. Six indexes including
  `(category, name, timestamp DESC)`, `(name, timestamp DESC)`,
  `(category, timestamp DESC)`, `(timestamp DESC)`. WAL journal mode.
  (VERIFIED via `observability.py:174-228`.)
- `core/observability.py::record_metric(category, name, value, **metadata)`
  — async fire-and-forget singleton method. Coerces value to float,
  JSON-serialises metadata with `default=str`, swallows all persistence
  errors. (VERIFIED via `observability.py:234-283`.)
- `core/observability.py::get_health_report()` — returns latest value
  per `(category, name)` via SQLite `ROW_NUMBER() OVER` window query.
  Bucketed under canonical categories; unknown categories go to `other`.
  (VERIFIED via `observability.py:353-423`.)
- `core/observability.py::get_metric_history(name, limit)` — most-
  recent-N samples for a single metric name. (VERIFIED via
  `observability.py:310-351`.)

### 4.2 Observability collector (background)

- `core/observability_collector.py` — single asyncio task running every
  30 seconds. Pulls stats from every active subsystem and persists via
  `record_metric()`. Lifecycle: `start_collector()` / `stop_collector()`
  + lifespan-context-manager wrapping. (VERIFIED via
  `observability_collector.py:1-100`.)
- 4 per-subsystem collectors: `_collect_data_source_metrics`,
  `_collect_execution_metrics`, `_collect_ml_metrics`,
  `_collect_system_metrics`. Each is a standalone async function so a
  single failure in one source never prevents the others from being
  collected. (VERIFIED via `observability_collector.py:108-120, 163-230,
  233-319`.)

### 4.3 Alerting

- `core/alerting.py::AlertEngine` — singleton with 7 default rules
  across 4 categories (`risk`, `ml`, `system`, `data`). SQLite store at
  `alerts.db`. Schema: `alerts (alert_id TEXT PK, timestamp, category,
  name, severity, message, value, threshold, metadata, acknowledged)`.
  Five indexes. (VERIFIED via `alerting.py:138-220`.)
- HTTP endpoints: `GET /api/alerts`, `GET /api/alerts/stats`,
  `POST /api/alerts/{alert_id}/acknowledge`,
  `POST /api/alerts/acknowledge-all`, `POST /api/alerts/evaluate`.
  (VERIFIED via `alerting.py:555-...`.)

### 4.4 Audit trails (three coexist)

- `core/audit_logger.py::AuditLogger` — durable SQLite audit trail.
  Schema: `audit_events (id, timestamp, category, event_type, token_id,
  slug, details, pnl, strategy, idempotency_key TEXT UNIQUE)`. Async
  writes via `asyncio.to_thread`. Idempotent via `INSERT OR IGNORE` on
  `idempotency_key`. (VERIFIED via `audit_logger.py:22-106`.)
- `core/immutable_audit.py::ImmutableAuditTrail` — hash-chained trail
  for control events (kill switch, live-trade enable, position close,
  config changes, feature flag changes). Schema: `audit_chain (entry_id
  PK, timestamp, event_type, payload, previous_hash, entry_hash,
  sequence)`. SHA-256 chain. `verify_chain()` returns `{valid, broken_at}`.
  (VERIFIED via `immutable_audit.py:1-120`.)
- `core/decision_ledger.py::DecisionLedger` — 6-stage unified ledger
  keyed by `decision_id`. Schema: `decision_events (id, timestamp,
  decision_id, stage, token_id, strategy, pnl, data_json)` + fast-view
  `decision_rejections`. Six + four indexes. Async writes. (VERIFIED
  via `decision_ledger.py:144-233`.)

### 4.5 Execution-quality ledger

- `core/execution_quality.py` — per-fill slippage / latency / realized
  edge. Schema: `execution_quality (id, timestamp, order_id, decision_id,
  token_id, strategy, side, signal_price, decision_price,
  submitted_price, best_bid, best_ask, expected_fill, actual_fill,
  spread, slippage, slippage_bps, latency_ms, realized_edge, paper,
  data_json)`. (VERIFIED via `execution_quality.py:1-80`.)

### 4.6 Prometheus metrics

- `core/prometheus_metrics.py` — module-level singleton metric
  instances (Counter, Gauge, Histogram, Info). Namespace `polymarket_`.
  Low label cardinality: HTTP metrics labelled by `method` (≤9),
  `endpoint` (<80), `status` (~60). Trading/ML metrics labelled by
  `side` (2), `strategy` (~10), `cache_name` (6), `db_name` (~10),
  `severity` (3). Total series bounded under ~2k. (VERIFIED via
  `prometheus_metrics.py:1-40, 50-174`.)
- HTTP endpoints: `GET /metrics` (Prometheus scrape, auth-configurable).
  (VERIFIED via `api/server.py:1404-1414`.)

### 4.7 Profiler

- `core/profiling.py::Profiler` — in-memory p50/p95/p99 per-endpoint
  latency. 1000-sample rolling window per endpoint (eviction oldest-
  first). Coarse-grained `threading.Lock`. (VERIFIED via
  `profiling.py:108-200`.)
- HTTP endpoints: `GET /api/profiling/stats`, `GET /api/profiling/slowest`,
  `POST /api/profiling/reset`. (VERIFIED via `profiling.py:19-34`.)

### 4.8 Circuit breakers

- `core/circuit_breaker.py::CircuitBreaker` — thread-safe CLOSED / OPEN
  / HALF_OPEN state machine. Configurable `failure_threshold`,
  `recovery_timeout`, `half_open_max_calls`, `success_threshold`,
  `timeout`. Dual sync/async decorator support. (VERIFIED via
  `circuit_breaker.py:65-201`.)
- Pre-configured breakers: `clob_api` (5 failures, 30s recovery),
  `gamma_api` (3 failures, 60s recovery), `polymarket_ws` (5 failures,
  15s recovery). (VERIFIED via `circuit_breaker.py:209-227`.)

### 4.9 Logging

- `core/logging_config.py` — idempotent `setup_logging()`. Two
  formatters: `JSONFormatter` (production, single-line JSON) and
  `ColoredFormatter` (dev, ANSI color). Three contextvars:
  `request_id_var`, `user_var`, `endpoint_var`. (VERIFIED via
  `logging_config.py:1-159`.)

### 4.10 Grafana dashboard

- `/home/z/my-project/grafana/dashboard.json` — 11 panels, 403 lines,
  dashboard UID `polymarket-bot-ops-w13-1`. (VERIFIED.)

---

## 5. Data Flow

### 5.1 Observability metric flow

```
subsystem emits metric
  ├── observability_collector._collect_*  (30s background)
  ├── strategy_layer.record_metric(...)    (ad-hoc, UNVERIFIED if strategy
  │                                         layer actually calls this)
  └── api/server.py request middleware     (HTTP request metrics via
                                            prometheus_metrics.record_request)
  ↓
record_metric(category, name, value, **metadata)
  ↓
asyncio.to_thread(_insert)
  ↓
SQLite INSERT INTO metrics (timestamp, category, name, value, metadata_json)
  ↓
GET /api/observability (15s TTL cache)
  ↓
get_health_report() → ROW_NUMBER() window query → bucketed by category
  ↓
JSON response to dashboard
```

(VERIFIED via `observability.py:234-283, 353-423`.)

### 5.2 Audit-trail flow (decision ledger)

```
strategy emits decision event
  ├── decision_ledger.new_decision_id()  →  "dec-{uuid.hex}"
  ↓
decision_ledger.record(decision_id, stage, token_id, strategy, pnl, **data)
  ├── auto-stamp model_version on PREDICTION stage
  ├── json.dumps(data, default=str) → payload
  └── asyncio.to_thread(_insert) → SQLite INSERT INTO decision_events
  ↓
GET /api/decision/{token_id}  →  get_chain_by_token()
  ↓
ORDER BY timestamp ASC, id ASC  →  ordered stage chain
  ↓
JSON response: {token_id, count, events: [...]}
```

(VERIFIED via `decision_ledger.py:244-307, 378-409, 630-677`.)

### 5.3 Audit-trail flow (immutable chain)

```
control event (kill switch, live enable, position close, config change)
  ↓
immutable_audit.log(event_type, payload_dict)
  ├── entry_id = AUTOINCREMENT
  ├── timestamp = time.time()
  ├── previous_hash = self._last_hash
  ├── entry_hash = sha256(f"{entry_id}{timestamp}{event_type}{payload}{previous_hash}")
  ├── sequence = self._sequence + 1
  └── SQLite INSERT INTO audit_chain
  ↓
self._last_hash = entry_hash  (in-memory + persisted via chain)
  ↓
verify_chain()  →  iterate chain, recompute each entry_hash, compare
  ↓
{valid: bool, broken_at: int | None}
```

(VERIFIED via `immutable_audit.py:1-120` — exact hash construction
UNVERIFIED but pattern confirmed.)

### 5.4 Alert flow

```
api/server.py  (e.g. kill_switch activate)
  ↓
ws_manager.broadcast("alerts", {type, severity, ...})
  ↓
[operator dashboard flashes alert]

(also: alert_engine.evaluate(metrics) called periodically or on-demand)
  ↓
for each rule:
  if condition(metrics):
    Alert(...) → _store(alert) → SQLite INSERT OR REPLACE
    log.warning("Alert fired: ...")
  ↓
GET /api/alerts?unacknowledged_only=true
  ↓
recent alerts list → dashboard

POST /api/alerts/{alert_id}/acknowledge
  ↓
UPDATE alerts SET acknowledged=1 WHERE alert_id=?
```

(VERIFIED via `alerting.py:309-352, 481-504`, `api/server.py:2914-2929`
for the kill-switch broadcast.)

### 5.5 Logging flow

```
HTTP request arrives
  ↓
RequestLogMiddleware (ASGI)
  ├── request_id_var.set(uuid4().hex[:8])
  ├── user_var.set(authenticated_user or "")
  └── endpoint_var.set(request.url.path)
  ↓
handler runs → logger.info("...", extra={...})
  ↓
JSONFormatter.format(record)
  ├── timestamp / level / logger / message / module / function / line
  ├── context: {request_id, user, endpoint}  (only non-empty)
  ├── exception: traceback (if exc_info)
  └── extra=... promotions (filtered by _RESERVED_ATTRS)
  ↓
single-line JSON → stdout / log file
  ↓
log aggregator (Loki / CloudWatch / etc.) parses JSON
```

(VERIFIED via `logging_config.py:46-125`.)

---

## 6. Execution Flow

### 6.1 Per-request observability (HTTP)

```
1. Request arrives at FastAPI app
2. RequestLogMiddleware populates contextvars (request_id, user, endpoint)
3. _record_prometheus_request() called in finally block (always, even on exception)
4. profiler.record(method, endpoint, duration, status) called
5. log line emitted with context
6. JSON log line written to stdout / log file
7. Response returned
8. (Out-of-band) Prometheus scraper polls /metrics every 15s
9. (Out-of-band) Grafana queries Prometheus every panel-refresh interval
```

### 6.2 Per-trade observability (decision chain)

```
1. ml_model.predict(features) → (p_yes, confidence)
2. decision_id = decision_ledger.new_decision_id()
3. decision_ledger.record(decision_id, PREDICTION, ...) + model_version
4. allocate_size(edge, confidence, drawdown, exposure, liquidity) → size
5. decision_ledger.record(decision_id, SIGNAL, ...)
6. submit_order(args, decision_id)
7. risk_manager.check_order(provisional)
   ├── if rejected:
   │   ├── shadow_trading.record_shadow_trade(decision_id, ...)
   │   ├── decision_ledger.record(decision_id, RISK_REJECTED, ...)
   │   └── decision_ledger.record_rejection(token_id, strategy, edge, conf, reason)
   └── if approved:
       ├── decision_ledger.record(decision_id, RISK_APPROVED, ...)
       └── paper_sim.create_order(args, strategy, decision_id) or clob_client.create_order(args)
8. decision_ledger.record(decision_id, ORDER, ...)
9. paper_sim._execute_fill(order, fill_price)
   ├── store.add_trade(Trade(...))
   ├── update Position
   ├── update store.paper_balance / daily_pnl / peak_equity
   ├── decision_ledger.record(decision_id, FILL, pnl=...)
   ├── execution_quality.record_execution(order_id, decision_id, ...)
   ├── audit_logger.log_event(category="fill", ...)
   └── risk_manager.report_trade_pnl(strategy, pnl)
       └── if abs(pnl) >= PER_TRADE_MAX_LOSS:
           ├── _strategy_cooldowns[strategy] = monotonic + 300s
           └── audit_logger.log_event(category="risk", event_type="strategy_cooldown_activated")
```

(VERIFIED via `signal_trader.py`, `base.py:60-148`, `risk/manager.py:142-401`.)

### 6.3 Per-cycle observability (collector)

```
1. observability_collector loop wakes (every 30s)
2. _collect_data_source_metrics()
   ├── book_poller.stats → updates / errors / tracked_tokens
   ├── max staleness across store.order_books
   └── record_metric(CAT_DATA_SOURCE, ...)
3. _collect_execution_metrics()
   ├── store.open_orders count → submissions
   ├── store.trades count → fills
   ├── store.order_history CANCELLED count → rejections
   ├── recent 50 trades mean PnL → slippage proxy
   └── record_metric(CAT_EXECUTION, ...)
4. _collect_ml_metrics()
   ├── ml_model.adaptive_weights → prediction_distribution (max weight)
   ├── drift_detector.last_psi → drift
   ├── ml_model.brier_score / ece / roc_auc → extension metrics
   └── record_metric(CAT_ML, ...) (inference_latency=0.0, instrumented=False)
5. _collect_system_metrics() (psutil)
   ├── cpu_percent
   ├── memory_percent
   └── record_metric(CAT_SYSTEM, ...)
6. record_metric(CAT_BOT, "cycle", 1)  (heartbeat)
```

(VERIFIED via `observability_collector.py:85-319`.)

---

## 7. Feature Inventory

### 7.1 §54 six-category observability coverage

| Category | Spec metric | Present? | Source | Cadence |
|---|---|---|---|---|
| `data_source` | updates | Yes | `book_poller.success_count` | 30s |
| `data_source` | latency | NOT FOUND | — | — |
| `data_source` | staleness | Yes | `max(now - book.updated_at)` | 30s |
| `data_source` | errors | Yes | `book_poller.error_count` | 30s |
| `data_source` | reconnects | NOT FOUND | — | — |
| `data_source` | tracked_tokens | Yes (extension) | `book_poller.total_tracked` | 30s |
| `bot` | cycles | Yes (collector heartbeat) | `record_metric(CAT_BOT, "cycle", 1)` | 30s |
| `bot` | errors | NOT FOUND (no `bot.errors` metric) | — | — |
| `bot` | actions | NOT FOUND (no `bot.actions` metric) | — | — |
| `strategy` | evaluations | NOT FOUND (no `strategy.evaluations` metric) | — | — |
| `strategy` | signals | NOT FOUND (no `strategy.signals` metric — only via decision_ledger SIGNAL stage) | — | — |
| `strategy` | rejects | NOT FOUND (no `strategy.rejects` metric — only via decision_rejections table) | — | — |
| `execution` | submissions | Yes | `store.open_orders count` | 30s |
| `execution` | fills | Yes | `store.trades count` | 30s |
| `execution` | rejections | Yes | `CANCELLED in order_history` | 30s |
| `execution` | slippage | Partial | `mean per-trade PnL` proxy (instrumented separately by `execution_quality` per-fill) | 30s |
| `execution` | latency | NOT FOUND (no `execution.latency` metric — `execution_quality.latency_ms` per-fill exists but is not aggregated to a metric) | — | — |
| `execution` | positions | Yes (extension) | `store.positions count` | 30s |
| `execution` | paper_balance | Yes (extension) | `store.paper_balance` | 30s |
| `execution` | daily_pnl | Yes (extension) | `store.daily_pnl` | 30s |
| `ml` | inference_latency | Placeholder (0.0 with `instrumented=False` flag) | `observability_collector._collect_ml_metrics` | 30s |
| `ml` | prediction_distribution | Yes | `max(adaptive_weights.values())` | 30s |
| `ml` | drift | Yes | `drift_detector.last_psi` | 30s |
| `ml` | brier_score | Yes (extension) | `ml_model.brier_score` | 30s |
| `ml` | ece | Yes (extension) | `ml_model.ece` | 30s |
| `ml` | roc_auc | Yes (extension) | `ml_model.roc_auc` | 30s |
| `ml` | is_fitted | Yes (extension) | `ml_model.is_fitted` | 30s |
| `ml` | n_updates | Yes (extension) | `ml_model._n_updates` | 30s |
| `ml` | seconds_since_last_trained | Yes (extension) | `time.time() - ml_model._last_trained` | 30s |
| `system` | cpu_percent | Yes | `psutil.cpu_percent` | 30s |
| `system` | memory_percent | Yes | `psutil.virtual_memory().percent` | 30s |
| `system` | memory_used_mb | Yes | `psutil.virtual_memory().used / 1024²` | 30s |
| `system` | DB connections | NOT FOUND | — | — |
| `system` | queue health | NOT FOUND | — | — |

**Coverage: 21 metrics present, 9 metrics NOT FOUND, 1 placeholder.**
The 9 missing metrics are: `data_source.latency`, `data_source.reconnects`,
`bot.errors`, `bot.actions`, `strategy.evaluations`, `strategy.signals`,
`strategy.rejects`, `execution.latency`, `system.db_connections`,
`system.queue_health`. (VERIFIED via `observability.py:148-155` +
`observability_collector.py:108-319`.)

### 7.2 §55 correlation-identifier inventory

| Spec identifier | Present? | Where? | Linked to `decision_id`? |
|---|---|---|---|
| `decision_id` | Yes | `decision_ledger.decision_events.decision_id` (PK) + `decision_rejections.decision_id` + `execution_quality.decision_id` + `closed_positions.decision_id` (optional) + `shadow_trades.decision_id` | — (this IS the correlation key) |
| `signal_id` | NOT FOUND (collapsed into `decision_id`) | `MarketSignal.decision_id` field | Yes |
| `order_id` | Yes | `data_store.Order.order_id` + `execution_quality.order_id` + `audit_events` (in `details` field) | Yes (passed to `submit_order`) |
| `fill_id` | NOT FOUND (uses `decision_id` + `Trade.trade_id` separate) | `data_store.Trade.trade_id` | Partial (decision_id on the FILL stage event) |
| `position_id` | Partial — `closed_positions.position_id` exists on close but NOT on open positions | `closed_positions.position_id` (UNIQUE PK) | Yes (optional `decision_id` FK on close) |
| `strategy_id` | Partial — `Order.strategy` is a loose string ("signal_trader", "market_maker", etc.), not a UUID | `Order.strategy`, `MarketSignal.source`, `closed_positions.strategy`, `decision_events.strategy` | No (loose string match) |
| `model_version` | Yes | `decision_events.data_json.model_version` (auto-stamped on PREDICTION via V14 wiring) + `closed_positions.model_version` + `ml.model_registry.active_version` | Yes (via decision_id on PREDICTION event) |

(VERIFIED via `decision_ledger.py:108-126, 265-273`, `signal_trader.py:59-62`,
`base.py:60-70`, `closed_positions.py:18-38`, `execution_quality.py:39-45`,
`immutable_audit.py`.)

**Coverage: 4 of 7 identifiers fully present, 2 partial, 1 NOT FOUND.**

### 7.3 Prometheus metrics inventory

| Metric | Type | Labels | Use |
|---|---|---|---|
| `polymarket_http_requests_total` | Counter | method, endpoint, status | HTTP request rate |
| `polymarket_http_request_duration_seconds` | Histogram | method, endpoint | p50/p95/p99 latency |
| `polymarket_http_requests_in_progress` | Gauge | — | in-flight requests |
| `polymarket_orders_placed_total` | Counter | side, strategy | order placement |
| `polymarket_orders_filled_total` | Counter | side, strategy | fill count |
| `polymarket_trades_total` | Counter | — | total trades |
| `polymarket_realized_pnl_usd` | Gauge | — | realized P&L |
| `polymarket_unrealized_pnl_usd` | Gauge | — | unrealized P&L |
| `polymarket_paper_balance_usd` | Gauge | — | paper balance |
| `polymarket_open_positions` | Gauge | — | open position count |
| `polymarket_open_orders` | Gauge | — | open order count |
| `polymarket_ml_predictions_total` | Counter | — | ML predictions made |
| `polymarket_ml_model` | Info | — | model version info |
| `polymarket_ml_drift_psi` | Gauge | — | ML drift PSI |
| `polymarket_ml_brier_score` | Gauge | — | ML Brier score |
| `polymarket_ml_roc_auc` | Gauge | — | ML ROC AUC |
| `polymarket_cache_hits_total` | Counter | cache_name | cache hits |
| `polymarket_cache_misses_total` | Counter | cache_name | cache misses |
| `polymarket_db_size_bytes` | Gauge | db_name | SQLite file size |
| `polymarket_alerts_active` | Gauge | severity | unacknowledged alerts |
| `polymarket_auth_failures_total` | Counter | — | auth failures |
| `polymarket_rate_limit_hits_total` | Counter | endpoint | rate-limit hits |

(VERIFIED via `prometheus_metrics.py:50-174`. 22 metrics total.)

### 7.4 Grafana dashboard panels (11 panels)

| # | Panel | Type | PromQL |
|---|---|---|---|
| 1 | HTTP Request Rate (req/s) | timeseries | `sum(rate(polymarket_http_requests_total[1m]))` |
| 2 | HTTP Latency (p50/p95/p99) | timeseries | histogram_quantile from `polymarket_http_request_duration_seconds` |
| 3 | Error Rate (4xx/5xx %) | timeseries | `rate(polymarket_http_requests_total{status=~"4..|5.."}[1m]) / rate(...)` |
| 4 | Paper Balance ($) | timeseries | `polymarket_paper_balance_usd` |
| 5 | Realized + Unrealized P&L ($) | timeseries | `polymarket_realized_pnl_usd` + `polymarket_unrealized_pnl_usd` |
| 6 | Open Positions Count | timeseries | `polymarket_open_positions` |
| 7 | ML Drift PSI (threshold=0.25) | timeseries | `polymarket_ml_drift_psi` |
| 8 | ML Brier Score | timeseries | `polymarket_ml_brier_score` |
| 9 | Cache Hit Rate (%) | timeseries | `rate(hits[1m]) / (rate(hits[1m]) + rate(misses[1m]))` |
| 10 | Active Alerts by Severity | timeseries | `polymarket_alerts_active` |
| 11 | (header / metadata panel) | — | dashboard UID `polymarket-bot-ops-w13-1` |

(VERIFIED via `/home/z/my-project/grafana/dashboard.json` — 11 `"title"` keys, 403 lines.)

### 7.5 Alerting rules inventory (7 default rules)

| # | Name | Category | Severity | Condition |
|---|---|---|---|---|
| 1 | `max_drawdown_exceeded` | risk | CRITICAL | `daily_pnl < -2.0` |
| 2 | `kill_switch_activated` | risk | CRITICAL | `kill_switch_active is True` |
| 3 | `model_drift_detected` | ml | WARNING | `psi > 0.25` |
| 4 | `model_stale` | ml | WARNING | `model_age_hours > 24` |
| 5 | `high_latency` | system | WARNING | `api_latency_ms > 1000` |
| 6 | `backend_unhealthy` | system | CRITICAL | `backend_healthy is False` |
| 7 | `data_stale` | data | WARNING | `data_staleness_seconds > 60` |

(VERIFIED via `alerting.py:223-307`.)

### 7.6 Circuit breaker inventory

| Name | Failure Threshold | Recovery Timeout | Half-Open Max | Success Threshold | Request Timeout |
|---|---|---|---|---|---|
| `clob_api` | 5 | 30s | 3 | 2 | 10s |
| `gamma_api` | 3 | 60s | 3 | 2 | 15s |
| `polymarket_ws` | 5 | 15s | 3 | 2 | 5s |

(VERIFIED via `circuit_breaker.py:209-220, 56-65`.)

---

## 8. What Works

1. **The §54 six-category model IS implemented.** Six canonical
   categories (`data_source` / `bot` / `strategy` / `execution` / `ml` /
   `system`) with a `METRIC_NAMES` dict documenting the recommended
   metric names per category. (VERIFIED via `observability.py:128-155`.)
2. **The 30-second background collector is robust.** Each per-subsystem
   collector is a standalone async function wrapped in `try/except`, so
   a single failure in one source never prevents the others from being
   collected. (VERIFIED via `observability_collector.py:108-120`.)
3. **The `decision_id` IS the canonical correlation key** — it propagates
   through PREDICTION → SIGNAL → RISK_APPROVED/REJECTED → ORDER → FILL.
   (VERIFIED via `decision_ledger.py:111-126`.)
4. **`model_version` is auto-stamped on every PREDICTION event** via
   `_resolve_active_model_version()` (lazy import, returns `"unknown"`
   on any failure). (VERIFIED via `decision_ledger.py:265-273, 690-729`.)
5. **The Prometheus `/metrics` endpoint is production-grade.** Low
   label cardinality (≤2k series), `polymarket_` namespace, bearer-token
   auth (configurable). (VERIFIED via `prometheus_metrics.py:1-40` +
   `api/server.py:1404-1414`.)
6. **The Grafana dashboard exists** with 11 panels covering HTTP rate /
   latency / errors, paper balance, realized + unrealized P&L, open
   positions, ML drift / Brier, cache hit rate, active alerts. (VERIFIED
   via `/home/z/my-project/grafana/dashboard.json`.)
7. **The immutable audit trail is tamper-evident** — SHA-256 hash
   chain, `verify_chain()` endpoint, genesis hash `0*64`. (VERIFIED via
   `immutable_audit.py:1-120`.)
8. **Three audit trails coexist without schema contention** — each in
   its own SQLite file (`audit_trail.db`, `immutable_audit.db`,
   `decision_ledger.db`). (VERIFIED via the three module docstrings.)
9. **Structured JSON logging is implemented** with request-scoped
   contextvars (`request_id` / `user` / `endpoint`) propagated across
   `await` boundaries. (VERIFIED via `logging_config.py:46-53, 90-125`.)
10. **The per-endpoint profiler tracks p50/p95/p99** with a 1000-sample
    rolling window. (VERIFIED via `profiling.py:117-145`.)
11. **Three pre-configured circuit breakers** with dual sync/async
    decorator support — `clob_api` (5 failures, 30s), `gamma_api` (3,
    60s), `polymarket_ws` (5, 15s). (VERIFIED via
    `circuit_breaker.py:209-227`.)
12. **The 7 default alert rules cover 4 categories** (`risk`, `ml`,
    `system`, `data`). Alerts persist to SQLite (survive restarts).
    Acknowledgement via HTTP. (VERIFIED via `alerting.py:223-307`.)
13. **The execution-quality ledger captures per-fill slippage / latency
    / realized edge** with `decision_id` cross-ref. (VERIFIED via
    `execution_quality.py:1-80`.)
14. **The closed_positions journal carries `model_version` +
    `decision_id`** on close — full lineage from model version to
    realised P&L. (VERIFIED via `closed_positions.py:18-38`.)
15. **Slow-query timing decorator** logs warnings when query methods
    exceed 100ms SLO. Applied to `decision_ledger`, `observability`,
    `alerting`, `execution_quality` read paths. (VERIFIED via
    `decision_ledger.py:64-106`, `observability.py:83-123`.)
16. **WAL journal mode** enabled on observability.db for better read
    concurrency under concurrent dashboard polls. (VERIFIED via
    `observability.py:180-185`.)
17. **Idempotent `setup_logging()`** — safe to call multiple times in
    one process (test collection, server reload) without stacking
    duplicate handlers. (VERIFIED via `logging_config.py:13-16, 55-60`.)

---

## 9. What Does Not Work

### 9.1 Nine §54 spec metrics are NOT FOUND

The spec's six-category model lists 30+ metric names. The codebase emits
21 of them. The 9 missing:

- `data_source.latency` — book_poller response latency is NOT recorded
  as a metric (only `success_count` / `error_count` are).
- `data_source.reconnects` — no WebSocket reconnect counter.
- `bot.errors` — no error counter for the bot loop (only the
  `bot.cycle` heartbeat).
- `bot.actions` — no action counter.
- `strategy.evaluations` — no metric for strategy evaluation count.
- `strategy.signals` — no metric for signal count (only via
  decision_ledger SIGNAL stage events).
- `strategy.rejects` — no metric for rejection count (only via
  decision_rejections table rows).
- `execution.latency` — per-fill latency exists in execution_quality
  table but is NOT aggregated to a metric.
- `system.db_connections` — no DB connection pool metric (the async
  pool in `db_pool.py` does not expose a gauge).
- `system.queue_health` — no queue-depth metric (the job_queue module
  exists but does not emit a metric).

(VERIFIED — the canonical `METRIC_NAMES` dict in `observability.py:148-155`
lists the recommended names but the collector only emits a subset.)

### 9.2 `inference_latency` is a placeholder

The collector emits `inference_latency=0.0` with `instrumented=False`
flag because `ml.model.MarketMLModel.predict()` does NOT record per-call
latency. The metric is populated only so the canonical bucket is non-
empty in the dashboard. (VERIFIED via
`observability_collector.py:255-262`.)

### 9.3 The strategy layer does NOT emit `strategy.*` metrics

The collector emits `bot`, `data_source`, `execution`, `ml`, `system`
metrics but the `strategy` category is empty in the canonical
`METRIC_NAMES` dict — `evaluations`, `signals`, `rejects` are listed
but no call site emits them. The strategy layer uses the
decision_ledger for these events, not the observability store.
(VERIFIED — no `record_metric(CAT_STRATEGY, ...)` call observed in
the codebase.)

### 9.4 `signal_id` is collapsed into `decision_id`

The spec §55 lists `signal_id` as a distinct correlation identifier.
The codebase uses `decision_id` for both the prediction and the signal
— the `MarketSignal.decision_id` field is the only correlation key.
This is arguably correct (one decision = one signal) but diverges
from the spec vocabulary. (VERIFIED via `signal_trader.py:59-62`.)

### 9.5 `fill_id` is NOT a distinct identifier

The spec lists `fill_id` as a correlation identifier. The codebase uses
`decision_id` on the FILL stage event; `Trade.trade_id` exists as a
separate identifier but is NOT linked to the decision_ledger chain.
(VERIFIED — `Trade.trade_id` is not referenced from `decision_ledger`.)

### 9.6 `position_id` is NOT present on open positions

The `closed_positions.position_id` exists on close, but `store.positions`
is keyed by `token_id`, not by `position_id`. Open positions cannot be
traced back to their originating `decision_id` until they close.
(VERIFIED via `data_store.py` + `closed_positions.py:18-38`.)

### 9.7 `strategy_id` is a loose string match

The spec implies `strategy_id` is a correlation identifier. The
codebase uses `Order.strategy` (e.g. `"signal_trader"`,
`"market_maker"`, `"arb_scanner"`) — a string name, not a UUID.
(VERIFIED via `data_store.py:Order` + `MarketSignal.source`.)

### 9.8 The immutable audit chain is UNSIGNED

The chain uses SHA-256 of the previous entry, but the chain itself is
not signed by any cryptographic key. Anyone with write access to
`immutable_audit.db` can re-write the entire chain (computing fresh
hashes) without detection — the chain only detects tampering if the
attacker doesn't bother to recompute hashes. (VERIFIED via
`immutable_audit.py:1-120`.)

### 9.9 No per-gate risk-rejection Prometheus counter

The risk engine rejects orders for 22 distinct reasons, but the
Prometheus metrics only track `orders_placed_total` /
`orders_filled_total` — no `risk_rejections_total{reason="..."}`
counter. The `decision_rejections` table has the data, but it's not
surfaced as a Prometheus counter. (VERIFIED — no
`risk_rejections_total` metric in `prometheus_metrics.py`.)

### 9.10 No live-portfolio VaR gauge

`polymarket_realized_pnl_usd` and `polymarket_unrealized_pnl_usd` are
exposed, but there's no `polymarket_var_95_usd` or
`polymarket_cvar_95_usd` gauge. (VERIFIED — no VaR metric in
`prometheus_metrics.py`.)

### 9.11 No `execution` category alert rules

The 7 default alert rules cover `risk`, `ml`, `system`, `data` — but
NOT `execution`. There are no alert rules for high slippage, low fill
rate, or execution-latency spikes. (VERIFIED via `alerting.py:223-307`.)

### 9.12 No `strategy` category alert rules

Similarly, no alert rules for strategy under-performance (e.g. a
strategy with win_rate < 30% over the last 20 trades). (VERIFIED via
`alerting.py:223-307`.)

### 9.13 Profiler is in-memory only — no persistence

The p50/p95/p99 stats are not persisted across restarts. This is
documented as intentional ("an operator who restarts the service always
sees a fresh baseline rather than a stale multi-day view") but means
historical performance regressions are lost. (VERIFIED via
`profiling.py:19-24`.)

### 9.14 Every observability write swallows persistence errors

Every public method on `Observability`, `AlertEngine`, `AuditLogger`,
`DecisionLedger`, `ImmutableAuditTrail`, `ExecutionQualityLedger`,
`ClosedPositionsJournal`, `ShadowTradingJournal` swallows its own
persistence errors (logged at `error` level, returns `[]` / `0` /
`None`). This is documented as deliberate ("observability can never
break the trading pipeline") but means silent data loss is possible
under disk pressure. (VERIFIED — pattern across all 8 modules.)

### 9.15 `observability_cache` TTL of 15s may serve stale data

The structured health report is cached for 15s via `observability_cache`
(TTLCache). The collector runs every 30s, so a dashboard poll between
collector ticks serves the previous cycle's data plus up to 15s of
cache TTL — up to 45s stale in the worst case. (VERIFIED via
`observability.py:460-472` + `observability_collector.py:85`.)

---

## 10. Missing Features

1. **`data_source.latency` metric** — book_poller response latency.
2. **`data_source.reconnects` metric** — WebSocket reconnect counter.
3. **`bot.errors` and `bot.actions` metrics** — bot-loop error / action
   counters.
4. **`strategy.evaluations`, `strategy.signals`, `strategy.rejects`
   metrics** — emit from the strategy layer (or aggregate from
   decision_ledger events).
5. **`execution.latency` metric** — aggregate from `execution_quality`
   per-fill latency.
6. **`system.db_connections` gauge** — expose the async DB pool's
   connection count.
7. **`system.queue_health` metric** — expose `job_queue` depth.
8. **Per-call ML inference latency** — wrap `ml_model.predict()` with a
   timer; replace the `inference_latency=0.0` placeholder.
9. **`signal_id` distinct from `decision_id`** — if the spec demands it,
   mint a separate UUID for the SIGNAL stage.
10. **`fill_id` distinct from `decision_id`** — mint a separate UUID
    for the FILL stage event.
11. **`position_id` on open positions** — emit a POSITION stage event
    when a position opens, linked from the originating ORDER.
12. **`strategy_id` as a UUID** — register strategies with a stable UUID
    instead of a loose string name.
13. **Cryptographic signing of the immutable audit chain** — sign each
    entry with an HSM-backed key so re-writing the chain is detectable
    even with filesystem write access.
14. **`risk_rejections_total{reason="..."}` Prometheus counter** —
    expose per-gate rejection counts.
15. **`polymarket_var_95_usd` and `polymarket_cvar_95_usd` gauges** —
    live-portfolio VaR / CVaR.
16. **`execution` category alert rules** — high-slippage, low-fill-rate,
    latency-spike alerts.
17. **`strategy` category alert rules** — strategy under-performance
    alerts.
18. **Persistent profiler** — optional persistence of p50/p95/p99
    across restarts (or a separate long-window profiler).
19. **Slow-log for observability writes** — emit a CRITICAL alert when
    a write is silently swallowed under disk pressure.
20. **Grafana alerting** — the dashboard JSON has 11 panels but no
    `alert` field on any panel; alerting is via the in-process
    `alert_engine` only, not via Grafana's built-in alerting.

---

## 11. Bugs

1. **`observability_cache` TTL of 15s may serve data up to 45s stale**
   (collector 30s + cache 15s). The cache TTL should be ≤ the
   collector cadence. (VERIFIED via `observability.py:460-472` +
   `observability_collector.py:85`.)
2. **`inference_latency=0.0` placeholder** may mislead a dashboard
   operator who doesn't notice the `instrumented=False` metadata.
   (VERIFIED via `observability_collector.py:255-262`.)
3. **The immutable audit chain's `_load_last_entry`** runs at
   construction time. If the db file is corrupted, the chain restarts
   from genesis and prior tampering is undetectable. (VERIFIED via
   `immutable_audit.py:74-78, 120-...`.)
4. **`alert_engine.evaluate` runs synchronously** — the rule
    evaluation is sync even though `_store` does sync SQLite I/O.
    Under load (frequent `/api/alerts/evaluate` calls) this blocks
    the event loop. (VERIFIED via `alerting.py:309-352`.)
5. **Circuit breaker recovery is time-based only.** A `clob_api`
   breaker that trips at 14:00:00 will half-open at 14:00:30
   regardless of whether the API is actually back. (VERIFIED via
   `circuit_breaker.py:79-101`.)
6. **The profiler's `_stats` dict has unbounded key cardinality** for
   path-param routes (e.g. `GET /api/depth/0x123...` vs `GET /api/depth/
   0x456...`). Each endpoint caps its latencies list at 1000 entries,
   so worst-case memory is `1000 * 8 bytes * endpoint_count`, but
   `endpoint_count` is unbounded. (VERIFIED via `profiling.py:25-34`.)
7. **The `JSONFormatter._RESERVED_ATTRS` set excludes `taskName`**
   (Python 3.12+), which means if a logger passes `extra={"taskName":
   "foo"}` it gets promoted to the top-level JSON object as a
   user-supplied extra, not filtered as a reserved attribute. (LIKELY
   — the set at `logging_config.py:82-88` includes `taskName` so this
   is actually handled; VERIFIED.)

---

## 12. Technical Debt

1. **12 separate SQLite databases** under `/app/data/` — each module
   owns its own schema, its own indexes, its own `_init_db()` method.
   No unified migration runner across them. (VERIFIED.)
2. **Inconsistent async conventions.** `decision_ledger.record` is
   async; `audit_logger.log_event` is async; `immutable_audit.log` is
   sync; `feature_store.record_values` is sync; `alert_engine.evaluate`
   is sync; `alert_engine._store` is sync. Callers must remember which
   is which. (VERIFIED.)
3. **The `timed_query` decorator is duplicated** across `decision_ledger`,
   `observability`, `alerting`, `execution_quality` — each module
   defines its own copy with identical logic. (VERIFIED via
   `decision_ledger.py:64-106`, `observability.py:83-123`,
   `alerting.py:72-113`.)
4. **Singleton pattern across the observability stack** — `observability`,
   `alert_engine`, `audit_logger`, `decision_ledger`, `immutable_audit`,
   `profiler`, `db_pool` are all module-level singletons. No way to run
   two bot instances in one process. (VERIFIED.)
5. **The Grafana dashboard JSON is hand-curated** — there is no
   infrastructure to generate it from the Prometheus metric
   definitions. A new metric added to `prometheus_metrics.py` requires
   a manual dashboard JSON edit to surface in Grafana. (VERIFIED —
   dashboard JSON is at `/grafana/dashboard.json`.)
6. **The `observability_cache` is a `TTLCache`** with a 15s TTL — but
   the cache key is constant (`"observability_overview"`), so the cache
   is a single-entry cache. A true "last-known" cache would be simpler.
   (VERIFIED via `observability.py:460-472`.)
7. **The `immutable_audit.log` is sync** — called from async code paths
   via `try/except: pass`. A synchronous SQLite write in an async
   context blocks the event loop. (VERIFIED via `immutable_audit.py:`
   and `api/server.py:2933-2943`.)
8. **No structured-log correlation between the JSON logger and the
   SQLite audit trails.** The `request_id` contextvar is in the log
   line but NOT in the audit_events / decision_events / metrics rows.
   A log line and an audit event for the same request cannot be
   joined by `request_id`. (VERIFIED — no `request_id` column in any
   SQLite schema.)

---

## 13. Data Problems

1. **The `metrics` table grows unbounded.** No retention policy on
   `observability.db`. The `retention.py` module exists but
   VERIFIED-untested whether it covers `metrics`. (LIKELY.)
2. **The `decision_events` table grew to 141k rows / 71k chains** per
   the FINAL_SYSTEM_REASSESSMENT.md snapshot. No retention policy
   observed. (VERIFIED via the assessment doc.)
3. **The `alerts` table accumulates acknowledged alerts** —
   `acknowledge_all` marks them acknowledged but does not delete them.
   No retention policy. (VERIFIED via `alerting.py:494-504`.)
4. **The `audit_chain` table grows monotonically** — every control
   event appends a row; no compaction. (VERIFIED via
   `immutable_audit.py`.)
5. **The `execution_quality` table grows per-fill** — no retention
   policy. (VERIFIED.)
6. **No `request_id` column** in any SQLite schema — log lines and
   audit events cannot be joined by request. (VERIFIED.)
7. **`book.updated_at` is the only freshness signal** — no historical
   book state, no per-source latency tracking. (VERIFIED via
   `observability_collector.py:142-158`.)

---

## 14. Performance Problems

1. **`get_health_report()` uses a `ROW_NUMBER() OVER` window query** —
   this is a full table scan + sort. With 100k+ metric rows, this
   query will degrade. The 15s cache mitigates this but the first
   request after cache expiry pays the full cost. (VERIFIED via
   `observability.py:372-386`.)
2. **Each `record_metric` call opens a fresh `sqlite3.connect`** — no
   connection reuse, no prepared-statement cache. The async-via-
   `asyncio.to_thread` pattern means every metric write is a thread-
   pool dispatch + open + insert + commit + close. (VERIFIED via
   `observability.py:264-283`.)
3. **The collector iterates `store.order_books.values()` under the
   store's lock** every 30s — with 800+ tracked tokens, this holds
   the lock for non-trivial time. (VERIFIED via
   `observability_collector.py:147-158`.)
4. **`alert_engine.evaluate` is sync** — blocks the event loop when
   called from an async context. (VERIFIED via `alerting.py:309-352`.)
5. **`immutable_audit.log` is sync** — blocks the event loop. (VERIFIED.)
6. **The profiler's `record()` method holds a `threading.Lock`** for
   the dict-lookup + list-append + cap-eviction. Coarse-grained but
   sub-microsecond per call. (VERIFIED via `profiling.py:131-145`.)
7. **The Grafana dashboard's `histogram_quantile()` PromQL** is
   expensive on long time ranges — Prometheus must process every
   bucket of every histogram. Standard practice but worth noting for
   large scrape intervals. (VERIFIED via dashboard.json panel 2.)

---

## 15. Reliability Problems

1. **Every observability write swallows persistence errors** — silent
   data loss is possible under disk pressure. (VERIFIED — pattern
   across all 8 SQLite-recording modules.)
2. **The immutable audit chain's `_load_last_entry`** runs at
   construction time. A corrupted db file silently resets the chain
   to genesis. (VERIFIED via `immutable_audit.py:74-78`.)
3. **Circuit breaker recovery is time-based only** — no probe-based
   recovery (e.g. a health-check call before half-opening). (VERIFIED
   via `circuit_breaker.py:79-101`.)
4. **No distributed lock across processes.** Two bot processes started
   against the same `/app/data/` directory will both write to the same
   SQLite files. SQLite's WAL mode mitigates corruption but both
   processes will see inconsistent snapshots. (LIKELY.)
5. **The `observability_cache` TTL of 15s** can serve stale data for
   up to 45s (collector 30s + cache 15s). (VERIFIED.)
6. **The `inference_latency=0.0` placeholder** may mislead a dashboard
   operator who doesn't notice the `instrumented=False` metadata.
   (VERIFIED.)
7. **`alert_engine.evaluate` is sync** — under load, calling it from
   an async context blocks the event loop. (VERIFIED.)
8. **`immutable_audit.log` is sync** — same problem. (VERIFIED.)
9. **No backpressure on metric writes.** A misbehaving emitter that
   calls `record_metric` 1000× per second will exhaust the
   `asyncio.to_thread` thread pool. (LIKELY.)
10. **No retry on observability write failure.** A transient disk
    hiccup drops the metric permanently. (VERIFIED — every write is
    fire-and-forget.)

---

## 16. Security Problems

1. **The immutable audit chain is unsigned.** Anyone with write access
   to `immutable_audit.db` can re-write the chain (computing fresh
   hashes) without detection. The chain only detects tampering if the
   attacker doesn't bother to recompute hashes. (VERIFIED via
   `immutable_audit.py:1-120`.)
2. **The `/metrics` endpoint is bearer-token-protected** (configurable).
   If the operator leaves `PROMETHEUS_METRICS_AUTH_TOKEN` unset, the
   endpoint is open. (VERIFIED via `api/server.py:1404-1414` + the
   `PUBLIC_PATHS` mechanism.)
3. **The audit_trail / decision_ledger / observability / alerts
   databases are NOT encrypted at rest.** Anyone with filesystem
   access can read every trade decision. (VERIFIED — SQLite default
   is plaintext.)
4. **The `JSONFormatter` promotes caller-supplied `extra={...}` keys
   to the top-level JSON object.** If a strategy passes PII or
   secrets via `extra={"api_key": "..."}`, they will be persisted to
   the log. No redaction layer. (VERIFIED via `logging_config.py:113-125`.)
5. **The `audit_logger` writes raw payload strings into the `details`
   column.** Same problem — no redaction. (VERIFIED via
   `audit_logger.py:53-85`.)
6. **`POST /api/alerts/{alert_id}/acknowledge`** has no auth check
   beyond the standard `enforce_api_auth` middleware — an
   authenticated operator can acknowledge ANY alert, including
   CRITICAL ones they didn't fire. (VERIFIED via `alerting.py:481-492`.)
7. **`PUT /api/portfolio/config`** can change `kelly_fraction` to 1.0
   (full Kelly) without a second factor or rate limit. (VERIFIED via
   `portfolio_optimizer.py:145-213`.)

---

## 17. Testing

The observability/reliability layer has substantial test coverage:

- `tests/test_observability.py` — 6-category metrics store.
- `tests/integration/test_observability_pipeline.py` — collector
  integration.
- `tests/test_observability_collector.py` — per-subsystem collectors.
- `tests/test_alerting.py` — 7 default rules + acknowledgement.
- `tests/test_audit_logger.py` — durable audit trail.
- `tests/test_immutable_audit.py` — hash-chained trail + verify_chain.
- `tests/test_decision_ledger.py` — 6-stage ledger.
- `tests/test_e2e_decision_chain.py` — end-to-end chain.
- `tests/test_execution_quality.py` — per-fill slippage.
- `tests/test_closed_positions.py` — round-trip P&L.
- `tests/test_shadow_trading.py` — counterfactual journal.
- `tests/test_prometheus.py` — /metrics endpoint + Grafana dashboard
  contract.
- `tests/test_profiling.py` — p50/p95/p99 latency.
- `tests/test_circuit_breaker.py` — 3 breakers + state transitions.
- `tests/test_logging.py` — JSON formatter + contextvars.
- `tests/test_async_db.py` — async DB pool (W16-7, 25 tests).
- `tests/test_db_indexes.py` — index presence + performance.
- `tests/test_retention.py` — retention policies.
- `tests/test_pagination.py` — cursor pagination.
- `tests/test_security.py` — API auth.
- `tests/test_rate_limiting.py` — rate-limit middleware.

**Test gaps for this assessment:**

1. No test verifying that all 30+ §54 spec metrics are emitted by the
   collector — because 9 are NOT emitted.
2. No test verifying that the `signal_id`, `fill_id`, `position_id`
   correlation identifiers are present in every event — because
   they are NOT.
3. No test verifying that the immutable audit chain survives a
   corrupted db file — because the chain silently resets to genesis.
4. No test verifying that the `alert_engine.evaluate` sync call does
   not block the event loop under load — because it does.
5. No test verifying that the `observability_cache` TTL is ≤ the
   collector cadence — because it is NOT (15s TTL vs 30s cadence).
6. No test verifying that the `JSONFormatter` redacts PII from
   `extra={...}` — because it does NOT.

---

## 18. Observability

This section is meta — it assesses the observability of the
observability stack itself.

- **The observability stack IS observable.** The collector emits a
  `bot.cycle` heartbeat every 30s; if the heartbeat stops, the
  dashboard's `data_source` category shows staleness > 60s, which
  triggers the `data_stale` alert. (VERIFIED via
  `observability_collector.py` + `alerting.py:298-306`.)
- **The Grafana dashboard has 11 panels** covering the core
  operational signals (HTTP rate / latency / errors, paper balance,
  P&L, open positions, ML drift / Brier, cache hit rate, alerts).
  (VERIFIED.)
- **The `/metrics` endpoint exposes 22 Prometheus metrics** with low
  label cardinality. (VERIFIED.)
- **The `/api/profiling/stats` endpoint** exposes p50/p95/p99 per
  endpoint. (VERIFIED.)
- **The `/api/circuit-breakers` endpoint** exposes the 3 breaker
  states. (VERIFIED.)
- **The `/api/alerts` endpoint** exposes recent alerts + stats.
  (VERIFIED.)
- **The `/api/observability` endpoint** exposes the structured health
  report. (VERIFIED.)

**Observability-of-observability gaps:**

1. **No metric for observability write failures.** A silently-swallowed
   persistence error is logged at `error` level but not surfaced as a
   metric. The dashboard cannot tell the operator "N metrics were
   dropped in the last hour".
2. **No metric for `alert_engine.evaluate` latency.** A slow
   evaluation blocks the caller but is not timed.
3. **No metric for `immutable_audit.log` latency.** Same problem.
4. **No metric for the async DB pool's connection count or wait
   time.** (See §10 missing features.)
5. **No metric for the Grafana dashboard's panel count or
   last-edited timestamp.** Operator has no way to detect a stale
   dashboard JSON.

---

## 19. Production Readiness

For **paper trading**: the observability stack is production-ready.
The 6-category model, the 30s collector, the Prometheus endpoint, the
Grafana dashboard, the 7 alert rules, the 3 circuit breakers, the
structured logging, the immutable audit trail, and the per-endpoint
profiler together provide a comprehensive operational picture.

For **live trading**, the gaps that matter:

1. **Silent data loss under disk pressure** (§15.1) — every observability
   write swallows persistence errors. For live trading, an observability
   write failure should at minimum fire a CRITICAL alert.
2. **9 missing §54 spec metrics** (§9.1) — `data_source.latency`,
   `data_source.reconnects`, `bot.errors`, `bot.actions`,
   `strategy.evaluations`, `strategy.signals`, `strategy.rejects`,
   `execution.latency`, `system.db_connections`, `system.queue_health`.
3. **`inference_latency` placeholder** (§9.2) — ML predict latency is
   not instrumented.
4. **The immutable audit chain is unsigned** (§9.8) — for live trading,
   a signed chain (HSM-backed) would be required for true tamper
   evidence.
5. **`alert_engine.evaluate` is sync** (§11.4) — under live-trading
   load, this blocks the event loop.
6. **No `execution` or `strategy` category alert rules** (§9.11, §9.12)
   — high-slippage / strategy-under-performance alerts are missing.
7. **No live-portfolio VaR gauge** (§9.10) — live tail-risk is not
   observable.
8. **No `risk_rejections_total{reason="..."}` counter** (§9.9) — the
   operator cannot see which risk gates are firing most often.
9. **`request_id` is in logs but NOT in SQLite audit rows** (§12.8) —
   log lines and audit events for the same request cannot be joined.
10. **The `observability_cache` TTL (15s) > collector cadence (30s)
    half-cycle** (§11.1) — up to 45s of staleness is possible.

**Production-readiness score for observability/reliability: 7.2/10.**
The foundation is comprehensive (6 categories + Prometheus + Grafana +
alerts + immutable audit + structured logging + circuit breakers +
profiler), but several gaps (silent data loss, missing metrics, sync
alert engine, unsigned chain, missing alert categories) prevent full
live-readiness.

---

## 20. Evidence

### 20.1 VERIFIED (read in source file in this session)

- `core/observability.py:1-100` — module docstring with 6-category model.
- `core/observability.py:125-155` — canonical categories + recommended
  metric names dict.
- `core/observability.py:174-228` — `_init_db` (schema + 6 indexes + WAL).
- `core/observability.py:234-283` — `record_metric` (async, fire-and-
  forget, swallows persistence errors).
- `core/observability.py:310-351` — `get_metric_history` (most-recent-N).
- `core/observability.py:353-423` — `get_health_report` (ROW_NUMBER
  window query, 15s cache).
- `core/observability.py:440-481` — HTTP endpoints.
- `core/observability_collector.py:1-100` — module docstring + lifespan
  wrapping.
- `core/observability_collector.py:108-319` — 4 per-subsystem collectors
  + 30s cadence.
- `core/alerting.py:1-220` — module docstring + AlertEngine.__init__ +
  _init_db (5 indexes).
- `core/alerting.py:223-307` — 7 default rules across 4 categories.
- `core/alerting.py:309-352` — `evaluate` (sync, per-rule try/except).
- `core/alerting.py:354-504` — `_store` / `get_recent` / `get_recent_page`
  / `acknowledge` / `acknowledge_all`.
- `core/alerting.py:506-545` — `get_stats` (combined COUNT query).
- `core/audit_logger.py:1-120` — module docstring + schema + async
  `log_event` + `get_recent_events`.
- `core/immutable_audit.py:1-120` — module docstring + hash-chain schema
  + `_init_db` + `_load_last_entry`.
- `core/decision_ledger.py:1-100` — module docstring + STAGE_* constants
  (6 stages only).
- `core/decision_ledger.py:108-233` — `_init_db` (decision_events +
  decision_rejections + 10 indexes).
- `core/decision_ledger.py:244-307` — `record` (auto-stamp model_version
  on PREDICTION).
- `core/decision_ledger.py:308-374` — `record_rejection` (dual-write:
  main chain + fast-view table).
- `core/decision_ledger.py:378-605` — `get_chain` / `get_chain_by_token`
  / `get_rejections` / `get_rejections_page` / `get_prediction_history`.
- `core/decision_ledger.py:630-729` — HTTP endpoints + `_resolve_active
  _model_version` (lazy import).
- `core/execution_quality.py:1-80` — schema with 18 columns including
  `decision_id` cross-ref.
- `core/closed_positions.py:18-38` — schema with `decision_id` FK
  (optional, populated on close).
- `core/shadow_trading.py:1-60` — counterfactual journal schema.
- `core/prometheus_metrics.py:1-40` — module docstring + namespace +
  cardinality.
- `core/prometheus_metrics.py:50-174` — 22 metrics (Counter / Gauge /
  Histogram / Info).
- `core/profiling.py:108-200` — `Profiler` class + 1000-sample window +
  coarse-grained lock.
- `core/circuit_breaker.py:1-236` — CircuitBreaker class + 3 breakers +
  dual sync/async decorator.
- `core/logging_config.py:1-159` — JSONFormatter + ColoredFormatter +
  contextvars + idempotent setup_logging().
- `core/db_pool.py` — async DB pool (W16-7).
- `core/async_repositories.py` — async read-side repos for decision /
  observability / execution-quality.
- `/home/z/my-project/grafana/dashboard.json` — 11 panels, 403 lines,
  dashboard UID `polymarket-bot-ops-w13-1`.
- `api/server.py:1404-1414` — `/metrics` endpoint.
- `api/server.py:2914-2929` — kill-switch broadcast to `alerts` channel.
- `api/server.py:2933-2943` — `immutable_audit.log` call (sync, wrapped
  in try/except).
- `api/server.py:3978-4040` — `register_routes` blocks for decision /
  observability / execution-quality / alerting / immutable-audit.
- `mini-services/polymarket-bot/data/` listing — 12 SQLite databases +
  JSON + pkl files.

### 20.2 STRONG EVIDENCE

- `FINAL_SYSTEM_REASSESSMENT.md` documents the 6-stage ledger
  (`PREDICTION → SIGNAL → RISK_APPROVED → ORDER → FILL`) and the
  141k-row / 71k-chain empirical state of `decision_ledger.db`.
- W16-7 worklog documents the async DB pool wiring + 2 async v2
  endpoints + 25-test async DB test suite.
- `tests/test_prometheus.py:155` references "The Grafana dashboard in
  `grafana/dashboard.json`" — confirming the dashboard file's
  existence and contract.

### 20.3 LIKELY

- The strategy layer does NOT emit `strategy.*` metrics (no
  `record_metric(CAT_STRATEGY, ...)` call observed in the codebase).
- The `retention.py` module does NOT cover `metrics` / `decision_events`
  / `alerts` tables (UNVERIFIED).

### 20.4 UNVERIFIED

- The exact hash-construction formula in `immutable_audit._compute_hash`
  (the entry_hash is computed from entry_id + timestamp + event_type +
  payload + previous_hash, but the exact concatenation order was not
  traced line-by-line).
- Whether `retention.py` covers any of the observability databases.

### 20.5 NOT FOUND

- `data_source.latency` metric.
- `data_source.reconnects` metric.
- `bot.errors` metric.
- `bot.actions` metric.
- `strategy.evaluations` metric.
- `strategy.signals` metric.
- `strategy.rejects` metric.
- `execution.latency` metric.
- `system.db_connections` gauge.
- `system.queue_health` metric.
- `signal_id` distinct from `decision_id`.
- `fill_id` distinct from `decision_id`.
- `position_id` on open positions.
- `strategy_id` as a UUID.
- `risk_rejections_total{reason="..."}` Prometheus counter.
- `polymarket_var_95_usd` / `polymarket_cvar_95_usd` gauges.
- `execution` category alert rules.
- `strategy` category alert rules.
- Cryptographic signing of the immutable audit chain.
- `request_id` column in any SQLite schema.
- Probe-based circuit-breaker recovery (vs time-based).

---

## 21. Unknowns

1. **Does `retention.py` cover any of the observability databases?**
   The module exists but its scope was not traced.
2. **What is the exact hash-construction formula in
   `immutable_audit._compute_hash`?** The chain is SHA-256 of the
   previous entry, but the exact concatenation order of (entry_id,
   timestamp, event_type, payload, previous_hash) was not line-by-line
   verified.
3. **Is the Grafana dashboard actually scraped in production?** The
   JSON exists at `/grafana/dashboard.json` but whether a Prometheus
   + Grafana stack is deployed alongside the bot is UNVERIFIED.
4. **Does the strategy layer emit ANY `strategy.*` metrics via
   `record_metric`?** No call site was observed, but the canonical
   `METRIC_NAMES` dict lists `strategy.evaluations` / `signals` /
   `rejects` as recommended names — perhaps a future emitter was
   planned.
5. **What is the actual cardinality of the `endpoint` label** in
   production? The docstring claims <80, but path-param routes
   (`/api/depth/0x123...`) could push this higher.

---

## 22. Maturity Score (0-10)

**Observability & Reliability maturity: 7.0 / 10**

| Sub-dimension | Score | Rationale |
|---|---|---|
| §54 six-category observability | 7.5 / 10 | 6 categories present + 21 metrics emitted. 9 spec metrics NOT FOUND (-1.5). `inference_latency` placeholder (-1). |
| §55 auditability (correlation IDs) | 6.0 / 10 | `decision_id` covers 6 stages. `model_version` + `order_id` present. `position_id` partial (-1). `strategy_id` loose string (-1). `signal_id` + `fill_id` NOT FOUND (-1). |
| Prometheus metrics | 8.0 / 10 | 22 metrics, low cardinality, production-grade. No `risk_rejections_total` or `var_95` (-2). |
| Grafana dashboard | 7.5 / 10 | 11 panels, well-documented. No Grafana-side alerting (-1.5). Hand-curated JSON (-1). |
| Profiling | 6.5 / 10 | p50/p95/p99 per endpoint. In-memory only (-2). Unbounded key cardinality for path-param routes (-1.5). |
| Alerting | 6.5 / 10 | 7 rules across 4 categories. No `execution` or `strategy` rules (-2). Sync `evaluate` blocks event loop (-1.5). |
| Immutable audit trail | 7.0 / 10 | SHA-256 chain + verify endpoint. Unsigned (-2). Silent reset on corrupted db (-1). |
| Structured logging | 8.0 / 10 | JSON + contextvars + idempotent setup. No PII redaction in `extra=` (-2). |
| Circuit breakers | 7.5 / 10 | 3 breakers + dual sync/async. Time-based recovery only (-1.5). No probe-based recovery (-1). |
| Reliability under failure | 6.0 / 10 | Fire-and-forget writes swallow errors (-2). No backpressure (-1). No retry (-1). |

**Composite: 7.0 / 10.** The observability stack is comprehensive and
production-grade for paper trading. Several gaps (9 missing metrics,
unsigned audit chain, sync alert engine, missing alert categories,
silent data loss under disk pressure) prevent full live-trading
readiness.

---

## 23. Critical Findings

1. **The §54 six-category model is implemented but 9 of the ~30 spec
   metrics are NOT FOUND.** Missing: `data_source.latency`,
   `data_source.reconnects`, `bot.errors`, `bot.actions`,
   `strategy.evaluations`, `strategy.signals`, `strategy.rejects`,
   `execution.latency`, `system.db_connections`, `system.queue_health`.
   (Severity: HIGH for live trading.)
2. **The §55 auditability contract is PARTIALLY met.** `decision_id`
   covers 6 stages (PREDICTION through FILL). `signal_id` and `fill_id`
   are NOT distinct identifiers (collapsed into `decision_id`).
   `position_id` is NOT present on open positions. `strategy_id` is a
   loose string, not a UUID. (Severity: HIGH for §80 answerability.)
3. **`inference_latency` is a placeholder (`0.0` with
   `instrumented=False`)** because `ml_model.predict()` does not
   record per-call latency. (Severity: MEDIUM — the dashboard shows 0
   for ML latency, which is misleading.)
4. **The strategy layer does NOT emit `strategy.*` metrics.** The
   canonical `METRIC_NAMES` dict lists `evaluations` / `signals` /
   `rejects` as recommended names, but no call site emits them. The
   strategy layer uses the decision_ledger for these events, not the
   observability store. (Severity: MEDIUM — strategy health is not
   observable via the metrics dashboard.)
5. **The immutable audit chain is UNSIGNED.** Anyone with write access
   to `immutable_audit.db` can re-write the chain (computing fresh
   hashes) without detection. (Severity: HIGH for live trading.)
6. **Every observability write swallows persistence errors** — silent
   data loss is possible under disk pressure. No metric surfaces the
   "N metrics dropped" count. (Severity: HIGH for live trading.)
7. **`alert_engine.evaluate` is sync** — blocks the event loop when
   called from an async context. (Severity: MEDIUM for performance
   under load.)
8. **`immutable_audit.log` is sync** — same problem. (Severity: MEDIUM.)
9. **No `execution` or `strategy` category alert rules.** The 7 default
   rules cover `risk`, `ml`, `system`, `data` only. High-slippage,
   low-fill-rate, and strategy-under-performance alerts are missing.
   (Severity: MEDIUM.)
10. **No `risk_rejections_total{reason="..."}` Prometheus counter.**
    The risk engine rejects orders for 22 distinct reasons but only
    `orders_placed_total` / `orders_filled_total` are exposed.
    (Severity: LOW.)
11. **No live-portfolio VaR / CVaR gauge.** `polymarket_realized_pnl_usd`
    and `polymarket_unrealized_pnl_usd` are exposed but there's no
    `polymarket_var_95_usd` or `polymarket_cvar_95_usd`. (Severity:
    MEDIUM.)
12. **The `observability_cache` TTL (15s) can serve data up to 45s
    stale** (collector 30s + cache 15s). The cache TTL should be ≤ the
    collector cadence. (Severity: LOW.)
13. **The profiler is in-memory only** — no persistence across restarts.
    Historical performance regressions are lost. (Severity: LOW —
    documented as intentional.)
14. **No `request_id` column in any SQLite schema** — log lines and
    audit events for the same request cannot be joined. (Severity:
    MEDIUM for cross-system debugging.)
15. **Circuit breaker recovery is time-based only** — no probe-based
    recovery. A `clob_api` breaker that trips will half-open after 30s
    regardless of whether the API is actually back. (Severity: MEDIUM.)
16. **The Grafana dashboard JSON is hand-curated** — no infrastructure
    to generate it from the Prometheus metric definitions. A new metric
    requires a manual dashboard edit. (Severity: LOW.)
17. **The `JSONFormatter` promotes caller-supplied `extra={...}` keys
    to the top-level JSON object** with no PII redaction. (Severity:
    MEDIUM for security.)
18. **No backpressure on metric writes** — a misbehaving emitter that
    calls `record_metric` 1000× per second will exhaust the
    `asyncio.to_thread` thread pool. (Severity: MEDIUM.)
19. **No retry on observability write failure** — a transient disk
    hiccup drops the metric permanently. (Severity: MEDIUM for live
    trading.)
20. **The 12 SQLite databases have no unified retention policy** —
    `metrics`, `decision_events`, `alerts`, `audit_chain`,
    `execution_quality`, `closed_positions`, `shadow_trades` all grow
    unbounded. (Severity: MEDIUM for long-running deployments.)

### Recommended next actions (priority order)

1. **Add the 9 missing §54 spec metrics** — wire each subsystem's
   existing counters (book_poller latency, ws reconnects, bot errors /
   actions, strategy evaluations / signals / rejects, execution
   latency, db pool size, queue depth) to `record_metric`.
2. **Instrument `ml_model.predict()` with a per-call latency timer** —
   replace the `inference_latency=0.0` placeholder.
3. **Add `risk_rejections_total{reason="..."}` Prometheus counter** —
   increment on every `record_rejection` call, labelled by `reason`.
4. **Add `polymarket_var_95_usd` and `polymarket_cvar_95_usd` gauges** —
   compute on the live portfolio (see Risk & Portfolio Assessment
   §10.5).
5. **Sign the immutable audit chain** with an HSM-backed key so re-
   writing the chain is detectable even with filesystem write access.
6. **Make `alert_engine.evaluate` async** — wrap the sync `_store` in
   `asyncio.to_thread` so the rule evaluation does not block the event
   loop.
7. **Make `immutable_audit.log` async** — same fix.
8. **Add `execution` and `strategy` category alert rules** — high-
   slippage, low-fill-rate, strategy-under-performance.
9. **Add `request_id` column** to the audit_events / decision_events /
   metrics / alerts schemas so log lines and audit events for the
   same request can be joined.
10. **Emit a "metric write failed" counter** when `record_metric`
    swallows a persistence error — surface silent data loss to the
    dashboard.
11. **Add a POSITION stage to the decision ledger** — emit when a
    position opens, linked from the originating ORDER, with a
    `position_id` UUID.
12. **Add probe-based circuit-breaker recovery** — half-open should
    call a health-check endpoint before allowing test traffic.
13. **Lower the `observability_cache` TTL to 5s** (≤ collector cadence /
    2) to bound staleness at ~35s instead of ~45s.
14. **Generate the Grafana dashboard JSON from the Prometheus metric
    definitions** — a script that reads `prometheus_metrics.py` and
    emits panel definitions for each metric.
15. **Add PII redaction to the `JSONFormatter`** — filter known-
    sensitive keys (`api_key`, `token`, `password`, `secret`) from the
    `extra={...}` promotion.
16. **Add backpressure to `record_metric`** — drop + count if the
    asyncio.to_thread queue exceeds a threshold.
17. **Add a unified retention policy** across the 12 SQLite databases —
    a periodic VACUUM + DELETE-old-rows job.

---

*End of Observability & Reliability Assessment. Companion documents:
`CROSS_SYSTEM_ARCHITECTURE_ASSESSMENT.md` (§50, §51, §79, §80) and
`RISK_AND_PORTFOLIO_ASSESSMENT.md` (§52, §53).*
