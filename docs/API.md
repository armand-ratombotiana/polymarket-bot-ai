# API Reference — 77 Routes

This is the complete reference for the Polymarket Pro Bot backend. Every HTTP
route registered on the FastAPI `app` (in `mini-services/polymarket-bot/api/server.py`
plus the 12 `register_routes(app)` module hooks) is documented here, grouped
by tag, alphabetised within each group. Four "framework" routes auto-generated
by FastAPI / Starlette (`/docs`, `/redoc`, `/openapi.json`) plus the WebSocket
`/ws` endpoint are documented in the final section.

## Base URL & Gateway

| Surface        | URL                                  |
| -------------- | ------------------------------------ |
| Caddy gateway  | `http://<host>:81/...`                |
| Backend direct | `http://<host>:8080/...`             |

The Caddy gateway auto-routes to the backend by injecting the
`?XTransformPort=8080` query parameter on every proxied request. The Next.js
frontend calls every API via `apiFetch()`, which transparently rewrites the
URL to go through port 81 — callers therefore never have to think about
`XTransformPort`.

```
frontend (Next.js, apiFetch) ──▶  Caddy :81  ──▶  FastAPI :8080
                                              (?XTransformPort=8080 injected by Caddy)
```

## Authentication

Every route is fail-closed bearer-token authenticated, **except**:

- `GET /api/health` — the only unauthenticated liveness probe.
- `OPTIONS *` — CORS preflight is always allowed.
- In paper-trade / non-live mode only: `/docs`, `/redoc`, `/openapi.json` are
  also reachable without a token (FastAPI's auto-generated documentation
  surface). In `TRADING_MODE=live` these three are dropped from the public
  set so the OpenAPI schema is not exfiltrated from a live deployment.

All other routes require:

```
Authorization: Bearer <API_TOKEN>
```

where `<API_TOKEN>` is the value of `API_TOKEN` in `.env`. If `API_TOKEN` is
not configured, the middleware returns `503 {"code":"AUTH_NOT_CONFIGURED"}`.
If the header is missing or wrong, it returns `401 {"detail":"Unauthorized — missing or invalid API token"}`.

The WebSocket `/ws` endpoint authenticates via the `?token=<API_TOKEN>` query
parameter (compared with `hmac.compare_digest` to avoid timing attacks); it
closes with code `4401 Unauthorized` if absent or wrong.

## Table of Contents

- [1. AI](#1-ai)
- [2. Analysis](#2-analysis)
- [3. Arbitrage](#3-arbitrage)
- [4. Audit](#4-audit)
- [5. Backtesting](#5-backtesting)
- [6. Capital](#6-capital)
- [7. Config](#7-config)
- [8. Database](#8-database)
- [9. Decisions](#9-decisions)
- [10. Execution Quality](#10-execution-quality)
- [11. Markets](#11-markets)
- [12. ML](#12-ml)
- [13. Observability](#13-observability)
- [14. Orders](#14-orders)
- [15. Positions](#15-positions)
- [16. Risk](#16-risk)
- [17. Shadow](#17-shadow)
- [18. Strategies](#18-strategies)
- [19. System](#19-system)
- [20. Trading](#20-trading)
- [21. Live Safety](#21-live-safety)
- [22. Framework Routes](#22-framework-routes)

## Summary Table

| #  | Method   | Path                                          | Tag               |
| -- | -------- | --------------------------------------------- | ----------------- |
| 1  | POST     | `/api/ai/analyze-market`                      | AI                |
| 2  | POST     | `/api/ai/copilot`                             | AI                |
| 3  | GET      | `/api/ai/predict/{token_id}`                  | AI                |
| 4  | GET      | `/api/ai/search`                              | AI                |
| 5  | GET      | `/api/analysis/deep`                          | Analysis          |
| 6  | GET      | `/api/analysis/market/{token_id}`             | Analysis          |
| 7  | GET      | `/api/analysis/news`                          | Analysis          |
| 8  | GET      | `/api/analysis/news/sources`                  | Analysis          |
| 9  | GET      | `/api/analysis/news/stats`                    | Analysis          |
| 10 | GET      | `/api/attribution`                            | Analysis          |
| 11 | GET      | `/api/arbitrage/opportunities`                | Arbitrage         |
| 12 | POST     | `/api/arbitrage/execute`                      | Arbitrage         |
| 13 | GET      | `/api/audit/logs`                             | Audit             |
| 14 | POST     | `/api/backtest/run`                           | Backtesting       |
| 15 | GET      | `/api/capital/allocation`                     | Capital           |
| 16 | GET      | `/api/config`                                 | Config            |
| 17 | PUT      | `/api/config`                                 | Config            |
| 18 | GET      | `/api/database/records`                       | Database          |
| 19 | GET      | `/api/database/reconciliation`                | Database          |
| 20 | GET      | `/api/decision/{token_id}`                    | Decisions         |
| 21 | GET      | `/api/decisions/rejected`                     | Decisions         |
| 22 | GET      | `/api/depth/{token_id}`                       | Markets           |
| 23 | GET      | `/api/events`                                 | System            |
| 24 | GET      | `/api/exposure`                               | Risk              |
| 25 | GET      | `/api/health`                                 | System            |
| 26 | GET      | `/api/history/equity`                         | System            |
| 27 | GET      | `/api/history/ohlcv/{token_id}`               | Markets           |
| 28 | POST     | `/api/kill-switch/activate`                   | Risk              |
| 29 | POST     | `/api/kill-switch/deactivate`                 | Risk              |
| 30 | GET      | `/api/leaderboard`                            | Risk              |
| 31 | GET      | `/api/live/readiness`                         | Live Safety       |
| 32 | POST     | `/api/live/enable`                            | Live Safety       |
| 33 | GET      | `/api/markets`                                | Markets           |
| 34 | GET      | `/api/markets/catalog`                        | Markets           |
| 35 | GET      | `/api/markets/coverage`                       | Markets           |
| 36 | GET      | `/api/ml`                                     | ML                |
| 37 | GET      | `/api/ml/drift`                               | ML (line 1631)    |
| 38 | GET      | `/api/ml/drift`                               | ML (line 1766)*   |
| 39 | GET      | `/api/ml/metrics`                             | ML                |
| 40 | POST     | `/api/ml/learn`                               | ML                |
| 41 | GET      | `/api/ml/registry`                             | ML                |
| 42 | POST     | `/api/ml/retrain`                             | ML                |
| 43 | POST     | `/api/ml/rollback`                            | ML                |
| 44 | GET      | `/api/ml/training-orchestrator`               | ML                |
| 45 | POST     | `/api/ml/validate`                            | ML Validation     |
| 46 | GET      | `/api/ml/versions`                            | ML                |
| 47 | GET      | `/api/observability`                          | Observability     |
| 48 | GET      | `/api/observability/history/{name}`           | Observability     |
| 49 | GET      | `/api/orderbooks`                             | Markets           |
| 50 | DELETE   | `/api/orders`                                 | Orders            |
| 51 | DELETE   | `/api/orders/{order_id}`                      | Orders            |
| 52 | GET      | `/api/orders`                                 | Orders            |
| 53 | GET      | `/api/positions`                               | Positions         |
| 54 | GET      | `/api/positions/closed`                       | Positions         |
| 55 | GET      | `/api/positions/closed/stats`                 | Positions         |
| 56 | POST     | `/api/positions/{token_id}/close`             | Positions         |
| 57 | GET      | `/api/risk/reconcile`                         | Risk              |
| 58 | GET      | `/api/risk/strategies/paused`                 | Risk              |
| 59 | POST     | `/api/risk/observation-mode`                  | Risk              |
| 60 | GET      | `/api/shadow/comparison`                      | Shadow            |
| 61 | GET      | `/api/shadow/trades`                          | Shadow            |
| 62 | GET      | `/api/snapshot`                               | System            |
| 63 | GET      | `/api/status`                                 | System            |
| 64 | GET      | `/api/strategies/catalog`                     | Strategies        |
| 65 | POST     | `/api/strategies/toggle`                      | Strategies        |
| 66 | POST     | `/api/system/prune`                          | System            |
| 67 | GET      | `/api/system/health`                          | System            |
| 68 | GET      | `/api/system/mode`                            | System            |
| 69 | POST     | `/api/trade`                                  | Trading           |
| 70 | GET      | `/api/trades`                                 | Trading           |
| 71 | GET      | `/api/analytics`                              | System            |
| 72 | GET      | `/api/execution-quality`                      | Execution Quality |
| 73 | GET      | `/docs`                                       | Framework         |
| 74 | GET      | `/openapi.json`                               | Framework         |
| 75 | GET      | `/redoc`                                      | Framework         |
| 76 | WS       | `/ws`                                         | Framework         |

\* Both `/api/ml/drift` registrations appear in `app.routes`; the second
handler (line 1766) shadows the first at request-dispatch time. Both are
listed for completeness; the running server serves only the second.

> Total: **76 HTTP routes + 1 WebSocket = 77 routes.**

---

## 1. AI

### POST /api/ai/analyze-market

**Tag**: AI · **Auth**: Bearer required

Generate a quant + fundamental briefing for a specific prediction market.

**Request Body** (`MarketAnalyzeRequest`):

```json
{ "token_id": "0xabc123..." }
```

| Field      | Type   | Required | Notes                       |
| ---------- | ------ | -------- | --------------------------- |
| `token_id` | string | yes      | Polymarket CLOB token id.   |

**Response** (200): whatever `copilot_engine.analyze_market(token_id)` returns — a structured quant + fundamental briefing dict.

**Errors**:
- 401: Unauthorized
- 503: `AUTH_NOT_CONFIGURED` (no `API_TOKEN` env var)

---

### POST /api/ai/copilot

**Tag**: AI · **Auth**: Bearer required

Ask the GenAI Copilot for market analysis, trade ideas, or risk insights.

**Request Body** (`CopilotQueryRequest`):

```json
{ "query": "What's the edge on the next Fed-rate decision market?" }
```

| Field   | Type   | Required |
| ------- | ------ | -------- |
| `query` | string | yes      |

**Response** (200): the natural-language answer dict from `copilot_engine.answer_query(query)`.

**Errors**: 401 / 503 (auth).

---

### GET /api/ai/predict/{token_id}

**Tag**: AI · **Auth**: Bearer required

Return the ML ensemble's directional view for a single YES token.

**Path Parameters**:

| Name        | Type   | Required |
| ----------- | ------ | -------- |
| `token_id`  | string | yes      |

**Response** (200):

```json
{
  "token_id": "0xabc...",
  "p_yes": 0.6231,
  "confidence": 0.2462,
  "market_mid": 0.5800,
  "best_bid": 0.5750,
  "best_ask": 0.5850,
  "spread": 0.0100,
  "edge": 0.0431,
  "edge_bps": 431.0,
  "recommended_action": "BUY",
  "action_reason": "edge=+4.31ct ≥ +2ct AND confidence=0.246 ≥ 0.10",
  "thresholds": { "min_edge_cents": 2.0, "min_confidence": 0.10 },
  "model_status": {
    "model_ready": true,
    "model_version": "v1.155.0",
    "brier_score": 0.1013,
    "roc_auc": 0.9451,
    "ece": 0.0836,
    "n_online_updates": 12
  },
  "market": { /* compact catalog record */ },
  "book_updated_at": 1788409517.69,
  "timestamp": 1788409517.69
}
```

**Errors**:
- 401 / 503 (auth)
- 404: token not present in `market_discovery.catalog` — wait for the next sync or call `/api/markets/coverage`
- 422: feature extraction returned None — book mid missing or outside `(0.001, 0.999)` band
- 502: no live order book for this token; the poller is re-prioritized, retry shortly

---

### GET /api/ai/search

**Tag**: AI · **Auth**: Bearer required

Semantic vector similarity search across all prediction markets.

**Query Parameters**:

| Name    | Type | Required | Default | Notes                         |
| ------- | ---- | -------- | ------- | ----------------------------- |
| `query` | str  | yes      | —       | `min_length=1`                |
| `top_k` | int  | no       | 8       | Max number of results to return |

**Response** (200):

```json
{
  "query": "us election",
  "results": [
    { "market": { /* catalog metadata */ }, "score": 0.873 }
  ]
}
```

**Errors**: 401 / 503 / 422 (missing/empty `query`).

---

## 2. Analysis

### GET /api/analysis/deep

**Tag**: Analysis · **Auth**: Bearer required

Return top multi-factor opportunity rankings and fundamental sentiment.

**Query Parameters**: none.

**Response** (200):

```json
{
  "top_opportunities": [ /* deep_analysis_engine.get_top_ranked_opportunities(15) */ ],
  "recent_news":       [ /* fundamental_engine.news_feed[:15] */ ],
  "timestamp": 1788409517.69
}
```

**Errors**: 401 / 503.

---

### GET /api/analysis/market/{token_id}

**Tag**: Analysis · **Auth**: Bearer required

Return complete 9-factor probabilistic, microstructure, and recommendation analysis for a single contract.

**Path Parameters**:

| Name       | Type   | Required |
| ---------- | ------ | -------- |
| `token_id` | string | yes      |

**Response** (200): the dict returned by `deep_analysis_engine.analyze_market(token_id)` — a 9-factor probabilistic + microstructure + recommendation analysis.

**Errors**: 401 / 503 / 404 (unknown token handled by `analyze_market`).

---

### GET /api/analysis/news

**Tag**: Analysis · **Auth**: Bearer required

Return news headlines with sentiment scores. Items carry `is_seed` provenance.

**Query Parameters**:

| Name       | Type   | Required | Default | Notes                              |
| ---------- | ------ | -------- | ------- | ---------------------------------- |
| `limit`    | int    | no       | 50      | Max items                          |
| `category` | string | no       | —       | Filter by category (`all` = no op) |

**Response** (200):

```json
{
  "news":  [ /* fundamental_engine.news_feed truncated/filtered */ ],
  "count": 42
}
```

**Errors**: 401 / 503.

---

### GET /api/analysis/news/sources

**Tag**: Analysis · **Auth**: Bearer required

Return catalog of configured news sources. GDELT is config-only (not connected).

**Query Parameters**: none.

**Response** (200): `fundamental_engine.get_source_catalog()` payload.

**Errors**: 401 / 503.

---

### GET /api/analysis/news/stats

**Tag**: Analysis · **Auth**: Bearer required

Return live NLP sentiment breakdown and global ingestion rate telemetry.

**Query Parameters**: none.

**Response** (200): `fundamental_engine.get_news_stats()` payload.

**Errors**: 401 / 503.

---

### GET /api/attribution

**Tag**: analytics (registered dynamically by `core.attribution.register_routes`) · **Auth**: Bearer required

Full seven-dimension P&L attribution roll-up across all closed positions.

**Query Parameters**: none.

**Response** (200): the dict returned by `core.attribution.get_full_attribution()` — P&L attribution across strategy / confidence / edge / probability / liquidity / holding-period / direction dimensions.

**Errors**: 401 / 503.

> **Registered dynamically**: `core/attribution.py::register_routes(app)` is invoked from `api/server.py` line ~2143. No routes are added at import time on `core.attribution` itself; the endpoint appears on `app.routes` only after server boot.

---

## 3. Arbitrage

### GET /api/arbitrage/opportunities

**Tag**: Arbitrage · **Auth**: Bearer required

Return real-time dual-outcome and multi-pool arbitrage opportunities.

**Query Parameters**: none.

**Response** (200):

```json
{
  "opportunities": [ /* arbitrage_scanner.scan_opportunities() */ ],
  "count": 3
}
```

**Errors**: 401 / 503.

---

### POST /api/arbitrage/execute

**Tag**: Arbitrage · **Auth**: Bearer required

Execute a dual-leg Dutch-book arbitrage. Both legs pass the same risk gate and are hard-capped by the per-market ceiling. Live execution is only possible for real token ids; synthetic complementary legs are reported but not transmitted to the exchange.

**Request Body** (`ArbitrageExecuteRequest`):

```json
{
  "token_id_yes": "0xabc...",
  "token_id_no":  "0xdef...",
  "size_usdc":    2.00
}
```

| Field          | Type   | Required | Notes                                          |
| -------------- | ------ | -------- | ---------------------------------------------- |
| `token_id_yes` | string | yes      | YES-leg token id                               |
| `token_id_no`  | string | yes      | NO-leg token id (suffix `_no` ⇒ synthetic)     |
| `size_usdc`    | float  | yes      | USD per leg; capped at `MAX_POSITION_PER_MARKET`|

**Response** (200):

```json
{
  "status":    "processed",
  "size_usdc": 2.0,
  "slug":      "will-x-happen",
  "legs": [
    { "leg": "yes", "token_id": "0xabc...", "status": "PLACED_PAPER", "order_id": "..." },
    { "leg": "no",  "token_id": "0xdef...", "status": "SKIPPED",      "reason": "synthetic complementary token — not transmissible" }
  ]
}
```

**Errors**:
- 401 / 503 (auth)
- 400: `size_usdc` resolves to ≤ 0 OR a leg was rejected by `risk_manager.check_order`

---

## 4. Audit

### GET /api/audit/logs

**Tag**: Audit · **Auth**: Bearer required

Query immutable SQLite audit trail logs.

**Query Parameters**:

| Name       | Type   | Required | Default | Notes                            |
| ---------- | ------ | -------- | ------- | -------------------------------- |
| `limit`    | int    | no       | 100     | Max rows                         |
| `category` | string | no       | —       | Filter by audit-log `category`   |

**Response** (200):

```json
{ "logs": [ /* audit_logger.get_recent_events(...) */ ], "count": 100 }
```

**Errors**: 401 / 503.

---

## 5. Backtesting

### POST /api/backtest/run

**Tag**: Backtesting · **Auth**: Bearer required

Run quantitative simulation across historical ticks for any registered strategy.

**Request Body** (`BacktestRequest`):

```json
{
  "strategy_id":     "ml_random_forest_quant",
  "initial_capital": 10000.0,
  "days":            30,
  "fee_bps":         0.0,
  "slippage_bps":    5.0
}
```

| Field             | Type   | Required | Default | Constraints         |
| ----------------- | ------ | -------- | ------- | ------------------- |
| `strategy_id`     | string | yes      | —       | must be in catalog  |
| `initial_capital` | float  | no       | 10000.0 | `ge=100, le=1000000`|
| `days`            | int    | no       | 30      | `ge=1, le=365`      |
| `fee_bps`         | float  | no       | 0.0     | `ge=0, le=100`      |
| `slippage_bps`    | float  | no       | 5.0     | `ge=0, le=50`       |

**Response** (200):

```json
{
  "status":         "completed",
  "synthetic":       true,
  "synthetic_kind":  "monte_carlo_archetype",
  "disclaimer":      "Synthetic archetype simulation — not recorded market history (M8 pending)",
  "result":          { /* backtest_engine.run_backtest(...).to_dict() */ }
}
```

**Errors**: 401 / 503 / 422 (Pydantic validation) / 400 (unknown `strategy_id`).

---

## 6. Capital

### GET /api/capital/allocation

**Tag**: Capital (registered dynamically by `core.capital_allocator.register_routes`) · **Auth**: Bearer required

Return the USD allocation size + full component breakdown for a signal — the
capital allocator maps (edge, confidence, liquidity, exposure, drawdown) to a
USD position size via a saturating Michaelis–Menten edge curve, smoothstep
confidence gate, and Brier / drawdown / correlation / performance / liquidity
multipliers.

**Query Parameters**:

| Name                 | Type   | Required | Default | Constraints     | Notes                                                          |
| -------------------- | ------ | -------- | ------- | --------------- | ------------------------------------------------------------- |
| `strategy`           | string | yes      | —       | non-empty       | Strategy name (audit / attribution only — does not affect sizing)|
| `edge`               | float  | yes      | —       | `ge=-1.0, le=1.0`| Signed alpha edge (decimal, `0.05` = +5%)                     |
| `confidence`         | float  | no       | 0.5     | `ge=0.0, le=1.0` | Model confidence `\|P(YES)-0.5\|*2`                            |
| `liquidity`          | float  | no       | 0.0     | `ge=0.0`        | USD depth on the side being taken                             |
| `existing_exposure`  | float  | no       | 0.0     | `ge=0.0`        | USD already deployed in market / correlated group             |
| `drawdown`           | float  | no       | 0.0     | `ge=0.0`        | USD drawdown from the high-water mark                          |
| `win_rate`           | float? | no       | —       | `ge=0.0, le=1.0`| Strategy realised win-rate (feeds `performance_mult`)         |
| `sharpe`             | float? | no       | —       | —               | Strategy realised Sharpe ratio                                |
| `brier`              | float? | no       | —       | `ge=0.0, le=1.0`| Override ML Brier score for what-if                           |

**Response** (200): the full `allocation_breakdown(...)` dict — USD size + per-component transparency (`edge_curve`, `confidence_gate`, `brier_mult`, `drawdown_mult`, `correlation_mult`, `performance_mult`, `liquidity_mult`, `components`).

**Errors**:
- 401 / 503
- 422: missing required `strategy` or `edge`

> **Registered dynamically**: `core/capital_allocator.py::register_routes(app)` is invoked from `api/server.py` line ~2167.

---

## 7. Config

### GET /api/config

**Tag**: Config · **Auth**: Bearer required

Return the live strategy configuration.

**Query Parameters**: none.

**Response** (200):

```json
{
  "mm_spread_bps":           200,
  "mm_quote_size_usdc":      1.0,
  "mm_max_inventory_usdc":   5.0,
  "arb_min_profit_bps":     50,
  "arb_order_size_usdc":    2.0,
  "signal_min_confidence":  0.65,
  "daily_loss_limit_usdc":  0.50,
  "max_total_exposure_usdc":10.0,
  "max_open_orders":        20
}
```

**Errors**: 401 / 503.

---

### PUT /api/config

**Tag**: Config · **Auth**: Bearer required

Update the live strategy configuration in place. Only supplied fields are
mutated; omitted fields retain their current value.

**Request Body** (`StrategyConfigUpdate`):

```json
{ "signal_min_confidence": 0.70, "daily_loss_limit_usdc": 0.30 }
```

| Field                     | Type    | Required | Constraints                              |
| ------------------------- | ------- | -------- | ---------------------------------------- |
| `mm_spread_bps`           | int?    | no       | `ge=10, le=2000`                         |
| `mm_quote_size_usdc`      | float?  | no       | `ge=0.5, le=5.0`                         |
| `mm_max_inventory_usdc`   | float?  | no       | `ge=1.0, le=15.0`                        |
| `arb_min_profit_bps`      | int?    | no       | `ge=5, le=1000`                          |
| `arb_order_size_usdc`     | float?  | no       | `ge=0.5, le=5.0`                         |
| `signal_min_confidence`   | float?  | no       | `ge=0.5, le=0.99`                        |
| `daily_loss_limit_usdc`   | float?  | no       | `ge=0.25, le=2.0`                        |

**Response** (200):

```json
{ "status": "updated", "config": { /* full config dict, same as GET /api/config */ } }
```

**Errors**: 401 / 503 / 422 (validation).

---

## 8. Database

### GET /api/database/records

**Tag**: Database · **Auth**: Bearer required

Query latest time-series records from the ACTIVE backend (KD-29). Reads through the engine so results always match the backend that is actually accepting writes; errors are surfaced, never swallowed.

**Query Parameters**:

| Name    | Type   | Required | Default            | Notes                                            |
| ------- | ------ | -------- | ------------------ | ------------------------------------------------ |
| `table` | string | no       | `market_snapshots` | Must be in `core.timescale_db._TABLES` whitelist |
| `limit` | int    | no       | 25                 | Clamped to `[1, 500]`                            |

**Response** (200): whatever `timescale_db.fetch_records(table=..., limit=...)` returns — list of rows from the active backend (Postgres continuous aggregates when wired, otherwise the SQLite WAL).

**Errors**:
- 401 / 503
- 400: `Invalid table <table>` — table not in whitelist

---

### GET /api/database/reconciliation

**Tag**: Database · **Auth**: Bearer required

Most recent storage-vs-engine reconciliation artifact (P0-DAT-03). If no
artifact is on disk yet, runs `run_reconciliation()` synchronously and
returns the freshly-computed report.

**Query Parameters**: none.

**Response** (200): `core.reconciliation.last_reconciliation()` (or freshly computed) — a structured dict describing per-table row-count parity between in-memory state and the persisted backend.

**Errors**: 401 / 503.

---

## 9. Decisions

### GET /api/decision/{token_id}

**Tag**: Decisions (registered dynamically by `core.decision_ledger.register_routes`) · **Auth**: Bearer required

Return the recent decision-event chain for `token_id` — the
PREDICTION → SIGNAL → RISK_APPROVED/REJECTED → ORDER → FILL lifecycle.

**Path Parameters**:

| Name       | Type   | Required |
| ---------- | ------ | -------- |
| `token_id` | string | yes      |

**Query Parameters**:

| Name    | Type | Required | Default | Constraints     |
| ------- | ---- | -------- | ------- | --------------- |
| `limit` | int  | no       | 50      | `ge=1, le=500`  |

**Response** (200):

```json
{
  "token_id": "0xabc...",
  "count":    5,
  "events":   [
    { "decision_id": "dec-...", "stage": "PREDICTION", "data_json": "...", "data": { /* decoded */ }, ... },
    ...
  ]
}
```

Each event row carries the raw `decision_events` columns plus a decoded `data`
key.

**Errors**:
- 401 / 503
- 404: `no decision events recorded for token <token_id>`

---

### GET /api/decisions/rejected

**Tag**: Decisions (registered dynamically by `core.decision_ledger.register_routes`) · **Auth**: Bearer required

Return recent rejected decisions (most recent first).

**Query Parameters**:

| Name    | Type | Required | Default | Constraints     |
| ------- | ---- | -------- | ------- | --------------- |
| `limit` | int  | no       | 50      | `ge=1, le=500`  |

**Response** (200):

```json
{
  "count":       12,
  "rejections":  [ /* rows from decision_rejections table */ ]
}
```

**Errors**: 401 / 503.

---

## 10. Execution Quality

### GET /api/execution-quality

**Tag**: Execution Quality (registered dynamically by `core.execution_quality.register_routes`) · **Auth**: Bearer required

Return aggregate execution-quality stats + the most recent N fills.

**Query Parameters**:

| Name                  | Type    | Required | Default | Constraints | Notes                                                                 |
| --------------------- | ------- | -------- | ------- | ----------- | --------------------------------------------------------------------- |
| `time_window_seconds` | float?  | no       | —       | `ge=0`      | Rolling-window filter (only fills from the last N seconds)           |
| `strategy`            | string? | no       | —       | —           | Restrict to a single strategy name                                    |
| `limit`               | int     | no       | 50      | `ge=1, le=500` | Max recent fills returned alongside the aggregate stats             |

**Response** (200):

```json
{
  "stats": {
    "time_window_seconds": null,
    "avg_slippage_bps":    3.2,
    "median_slippage_bps": 2.1,
    "p95_slippage_bps":    8.4,
    "worst_slippage_bps":  12.0,
    "avg_latency_ms":      142.3,
    "avg_realized_edge":   0.0012,
    "total_realized_edge": 0.0421,
    "by_side":             { "BUY": {...}, "SELL": {...} }
  },
  "recent_fills": [
    {
      "timestamp":         1788409517.69,
      "decision_id":       "dec-...",
      "token_id":         "0xabc...",
      "strategy":         "ml_random_forest_quant",
      "side":             "BUY",
      "signal_price":     0.5800,
      "decision_price":   0.5810,
      "submitted_price":  0.5815,
      "best_bid":         0.5750,
      "best_ask":         0.5850,
      "expected_fill":    0.5850,
      "actual_fill":      0.5861,
      "spread":           0.0100,
      "slippage":         0.0011,
      "slippage_bps":     11.0,
      "latency_ms":       142.3,
      "realized_edge":    0.0009
    }
  ]
}
```

**Errors**: 401 / 503.

> **Registered dynamically**: `core/execution_quality.py::register_routes(app)` is invoked from `api/server.py` line ~2119.

---

## 11. Markets

### GET /api/depth/{token_id}

**Tag**: Markets · **Auth**: Bearer required

Return cumulative bid/ask depth (top 10 levels each) for a token. If the
book is missing, the poller is prioritized for this token and an empty
structure is returned (no error).

**Path Parameters**:

| Name       | Type   | Required |
| ---------- | ------ | -------- |
| `token_id` | string | yes      |

**Response** (200):

```json
{
  "token_id": "0xabc...",
  "slug":     "will-x-happen",
  "bids":     [ { "price": 0.58, "size": 100.0, "total": 100.0 }, ... ],
  "asks":     [ { "price": 0.59, "size": 50.0,  "total": 50.0  }, ... ],
  "mid":      0.585,
  "spread":   0.01,
  "best_bid": 0.58,
  "best_ask": 0.59
}
```

If no book is tracked:

```json
{ "token_id": "0xabc...", "bids": [], "asks": [], "mid": null, "spread": null }
```

**Errors**: 401 / 503.

---

### GET /api/history/ohlcv/{token_id}

**Tag**: Markets · **Auth**: Bearer required

Return OHLCV candlestick bars for visual charting.

Priority:
1. Real candles from TimescaleDB continuous aggregates (`market.price_candle_*`) when TimescaleDB is connected and rows exist for this token — labeled `synthetic=False`.
2. Seeded random-walk anchored to live mid when no stored candles exist — explicitly labeled `synthetic=True` so callers always know the data source.

**Path Parameters**:

| Name       | Type   | Required |
| ---------- | ------ | -------- |
| `token_id` | string | yes      |

**Query Parameters**:

| Name         | Type   | Required | Default | Allowed values         |
| ------------ | ------ | -------- | ------- | ---------------------- |
| `resolution` | string | no       | `"5m"`  | `"1m"`, `"5m"`, `"1h"` |
| `count`      | int    | no       | 40      | number of bars        |

**Response** (200): list of candle bars `{ bucket, open, high, low, close, vwap, tick_count, synthetic }`.

**Errors**: 401 / 503.

---

### GET /api/markets

**Tag**: Markets · **Auth**: Bearer required

List Polymarket markets via the Gamma API.

**Query Parameters**:

| Name     | Type   | Required | Default | Notes                            |
| -------- | ------ | -------- | ------- | -------------------------------- |
| `limit`  | int    | no       | 50      | Max markets                      |
| `search` | string | no       | —       | Free-text search (slug / question)|

**Response** (200):

```json
{ "markets": [ /* gamma_client.search_markets OR get_markets(active=True) */ ], "count": 50 }
```

**Errors**:
- 401 / 503
- 502: upstream Gamma error — `str(e)` in detail

---

### GET /api/markets/catalog

**Tag**: Markets · **Auth**: Bearer required

Return indexed market catalog with full hierarchy metadata.

**Query Parameters**:

| Name       | Type   | Required | Default | Notes                          |
| ---------- | ------ | -------- | ------- | ------------------------------ |
| `limit`    | int    | no       | 100     | Max items                      |
| `category` | string | no       | —       | Filter by catalog category     |

**Response** (200):

```json
{ "catalog": [ /* market_discovery.get_full_catalog(...) */ ], "count": 100 }
```

**Errors**: 401 / 503.

---

### GET /api/markets/coverage

**Tag**: Markets · **Auth**: Bearer required

Return authoritative Polymarket catalog coverage metrics and exclusion audit log.

**Query Parameters**: none.

**Response** (200): `market_discovery.get_coverage_report()` payload — total market count, indexed count, exclusion reasons, last-sync timestamp.

**Errors**: 401 / 503.

---

### GET /api/orderbooks

**Tag**: Markets · **Auth**: Bearer required

Return the top 5 bid/ask levels for every tracked token book.

**Query Parameters**: none.

**Response** (200):

```json
{
  "order_books": [
    {
      "token_id":   "0xabc...",
      "slug":       "will-x-happen",
      "bids":       [ { "price": 0.58, "size": 100.0 }, ... ],
      "asks":       [ { "price": 0.59, "size": 50.0  }, ... ],
      "best_bid":   0.58,
      "best_ask":   0.59,
      "mid":        0.585,
      "spread":     0.01,
      "updated_at": 1788409517.69
    }
  ],
  "count": 60
}
```

**Errors**: 401 / 503.

---

## 12. ML

> **Duplicate-path note**: `GET /api/ml/drift` is registered **twice** in
> `api/server.py` — at line 1631 (handler `get_drift_report`) and again at
> line 1766 (handler `get_model_drift`). Both registrations appear in
> `app.routes`; at request-dispatch time the second one wins. Both are
> documented below for completeness; the running server serves only the
> second (`get_model_drift`).

### GET /api/ml

**Tag**: ML · **Auth**: Bearer required

Rich ML status: ensemble health, stacking meta-learner, and drift signals.

**Response** (200):

```json
{
  "model_type":              "4-Member Calibrated Ensemble + Level-2 Stacking Meta-Learner",
  "members":                 { "rf": "RandomForestClassifier (isotonic-calibrated)", "gb": "...", "sgd": "...", "lgbm": "..." },
  "model_ready":             true,
  "model_version":           "v1.155.0",
  "n_online_updates":        12,
  "last_trained":            1788409517.69,
  "training_source":         "synthetic_seed",
  "n_real_samples":          42,
  "n_synthetic_samples":     3000,
  "adaptive_weights":        { "rf": 0.4, "gb": 0.3, "sgd": 0.2, "lgbm": 0.1 },
  "meta_learner":            { /* ensemble_meta_learner.get_summary() */ },
  "drift":                   { /* drift_detector.get_status_report() */ },
  "training_orchestrator":   { /* training_orchestrator.stats */ },
  "label_backfill":          { /* label_backfill_engine.stats */ },
  "brier_score":             0.1013,
  "roc_auc":                 0.9451,
  "ece":                     0.0836,
  "feature_importances":     { /* dict */ }
}
```

**Errors**: 401 / 503.

---

### GET /api/ml/drift  (handler #1, line 1631)

**Tag**: ML · **Auth**: Bearer required

Full drift-monitoring dashboard: PSI, KS statistic, rolling Brier, EWMA
Brier early-warning, drift status, and PSI history.

> ⚠️ This registration is **shadowed** at runtime by the second
> `/api/ml/drift` registration (line 1766). It is listed here only because
> both Route objects appear in `app.routes` and contribute to the X9
> route-count audit. Requests to `GET /api/ml/drift` will be served by the
> second handler (`get_model_drift`).

**Query Parameters**: none.

**Response** (would return, if reachable): `{ **drift_detector.get_status_report(), "meta_learner": {...}, "orchestrator": {...}, "model_version": "...", "brier_baseline": ..., "roc_auc": ... }`.

**Errors**: 401 / 503.

---

### GET /api/ml/drift  (handler #2, line 1766 — currently served)

**Tag**: ML · **Auth**: Bearer required

Return real-time Population Stability Index (PSI) and concept shift metrics.

**Query Parameters**: none.

**Response** (200): `drift_detector.get_status_report()` payload — `psi`, `ks_stat`, `rolling_brier`, `ewma_brier`, `drift_status`, `psi_history`.

**Errors**: 401 / 503.

---

### GET /api/ml/metrics

**Tag**: ML · **Auth**: Bearer required

Full quantitative diagnostics: Brier, EWMA Brier, ROC-AUC, ECE, drift, meta-learner, reliability curve.

**Response** (200):

```json
{
  "model_type":            "4-Member Calibrated Ensemble + Level-2 Stacking Meta-Learner",
  "brier_score":           0.1013,
  "roc_auc":               0.9451,
  "log_loss":              0.4321,
  "ece":                   0.0836,
  "sharpe_ratio":          1.42,
  "n_online_updates":      12,
  "last_trained":          1788409517.69,
  "training_source":       "synthetic_seed",
  "n_real_samples":        42,
  "n_synthetic_samples":   3000,
  "adaptive_weights":      { "rf": 0.4, ... },
  "meta_learner":          { /* ensemble_meta_learner.get_summary() */ },
  "drift":                 { /* drift_detector.get_status_report() */ },
  "feature_importances":   { /* dict */ },
  "reliability_curve":     [ /* (bin_low, bin_high, n, mean_pred, mean_actual) tuples */ ],
  "model_ready":           true,
  "model_version":         "v1.155.0",
  "registry_summary":      { /* model_registry.get_summary() */ }
}
```

**Errors**: 401 / 503.

---

### POST /api/ml/learn

**Tag**: ML · **Auth**: Bearer required

Feed a resolved ground-truth outcome into the online SGD learner.

1. Backfills outcome labels in both DB backends (TimescaleDB + SQLite).
2. Fetches the most recent stored feature vector for this token.
3. Calls `ml_model.update()` to incrementally train the SGD online learner.

**Query Parameters**:

| Name           | Type   | Required | Notes                          |
| -------------- | ------ | -------- | ------------------------------ |
| `token_id`     | string | yes      | Resolved market's token id     |
| `resolved_yes` | bool   | yes      | `true` if YES side won         |

**Response** (200):

```json
{
  "status":                  "updated",
  "token_id":                "0xabc...",
  "resolved_yes":            true,
  "feature_rows_labelled":  3,
  "online_update_applied":  true,
  "n_updates":               13
}
```

**Errors**: 401 / 503.

---

### GET /api/ml/registry

**Tag**: ML · **Auth**: Bearer required

Return model version lineage, benchmarks, ECE, and validation status.

**Response** (200): `model_registry.get_summary()` payload — `{ "active_version": "v1.155.0", "versions": [ ... ], "n_registered": N, ... }`.

**Errors**: 401 / 503.

---

### POST /api/ml/retrain

**Tag**: ML · **Auth**: Bearer required

Trigger manual re-training and re-calibration of the ML ensemble. Runs
`ml_model.fit_initial()` and `ml_model.save()` in a background thread,
then logs a `🧠 ML model retrained` event with the new metrics.

**Request Body**: none.

**Response** (200):

```json
{
  "status":         "retrained",
  "brier_score":    0.0998,
  "roc_auc":        0.9512,
  "log_loss":       0.4201,
  "ece":            0.0791,
  "model_version":  "v1.156.0",
  "meta_learner":   { /* ensemble_meta_learner.get_summary() */ }
}
```

**Errors**: 401 / 503.

---

### POST /api/ml/rollback

**Tag**: ML (registered dynamically by `ml.routes.register_routes`) · **Auth**: Bearer required

Roll the active model version back to a previously registered version.

**Query Parameters**:

| Name      | Type   | Required | Notes                                                          |
| --------- | ------ | -------- | -------------------------------------------------------------- |
| `version` | string | yes      | Target version (e.g. `v1.155.0`). Must exist in registry lineage. |

**Response** (200):

```json
{
  "rolled_back":        true,
  "previous_version":  "v1.156.0",
  "active_version":     "v1.155.0",
  "target_metrics":     { /* ModelVersionRecord.to_dict() of the rolled-back version */ }
}
```

**Errors**:
- 401 / 503
- 404: `Version 'v1.x' not found in model registry lineage; rollback refused.`

A best-effort durable audit row is written via `audit_logger.log_event(category="ml", event_type="model_rollback", details="active_version rolled back X -> Y")`; a transient audit-DB error never fails the rollback.

---

### GET /api/ml/training-orchestrator

**Tag**: ML · **Auth**: Bearer required

Return training orchestrator status: retrain count, last champion Brier, drift thresholds.

**Response** (200):

```json
{
  /* training_orchestrator.stats */
  "model_version":  "v1.155.0",
  "model_ready":    true,
  "drift_status":   "CLEAR"
}
```

**Errors**: 401 / 503.

---

### POST /api/ml/validate

**Tag**: ML Validation (registered dynamically by `ml.validation.register_routes`) · **Auth**: Bearer required

Run walk-forward CV and/or out-of-time validation on the posted data. Also
runs a leakage audit (near-duplicate-row conflicts, label-flip detection)
unless explicitly disabled.

**Request Body** (`ValidationRequest`):

```json
{
  "X":                 [[0.1, 0.2, ...], ...],
  "y":                 [0, 1, 0, 1, ...],
  "X_test":            [[0.1, 0.2, ...], ...],
  "y_test":            [0, 1, ...],
  "validation_type":   "both",
  "n_splits":          5,
  "min_train_size":    200,
  "model_class":       "RandomForestClassifier",
  "model_params":      { "n_estimators": 100 },
  "run_leakage_check": true
}
```

| Field               | Type                  | Required | Default   | Constraints                              |
| ------------------- | --------------------- | -------- | --------- | ---------------------------------------- |
| `X`                 | `list[list[float]]`   | yes      | —         | 2-D feature matrix                       |
| `y`                 | `list[int]`           | yes      | —         | Binary labels `{0,1}`                    |
| `X_test`             | `list[list[float]]?`  | no       | —         | Required when `validation_type` ∈ {`oot`, `both`} |
| `y_test`             | `list[int]?`          | no       | —         | Required with `X_test`                   |
| `validation_type`    | string                | no       | `"cv"`    | One of `cv` / `oot` / `both`             |
| `n_splits`          | int                   | no       | 5         | `ge=1, le=50`                            |
| `min_train_size`    | int                   | no       | 200       | `ge=10`                                  |
| `model_class`        | string?               | no       | `RandomForestClassifier` | Must be in `_MODEL_WHITELIST`            |
| `model_params`       | `dict[str, Any]?`     | no       | —         | kwargs for the estimator constructor     |
| `run_leakage_check`  | bool                  | no       | `true`    | Run `validate_no_leakage` and append it  |

**Response** (200):

```json
{
  "model_class":       "RandomForestClassifier",
  "model_params":      { "n_estimators": 100 },
  "n_samples":         1000,
  "n_features":        38,
  "validation_type":   "both",
  "generated_at":      1788409517.69,
  "leakage_check":    { /* validate_no_leakage result */ },
  "cv":               { /* time_series_cv result with per_fold + aggregate */ },
  "oot":              { /* out_of_time_test result */ }
}
```

**Errors**:
- 401 / 503
- 400: `model_class` not in whitelist OR `model_params` rejected OR `validation_type` invalid OR `X` not 2-D OR `X`/`y` length mismatch OR `validation_type='oot'/'both'` requires `X_test` and `y_test`
- 413: `payload too large: <N> rows > <MAX_PAYLOAD_ROWS> max`
- 500: validation raised unexpectedly

---

### GET /api/ml/versions

**Tag**: ML (registered dynamically by `ml.routes.register_routes`) · **Auth**: Bearer required

Return the full registered model-version lineage with metrics.

**Response** (200):

```json
{
  "active_version":    "v1.155.0",
  "total_registered":  5,
  "versions":          [
    {
      "version":         "v1.155.0",
      "created_at":      1788409517.69,
      "brier_score":      0.1013,
      "roc_auc":          0.9451,
      "ece":              0.0836,
      "sharpe_ratio":     0.0,
      "status":           "ACTIVE",
      "n_samples":        3000,
      "parameters":      { /* dict */ },
      "is_active":        true
    }
  ]
}
```

**Errors**: 401 / 503.

---

## 13. Observability

### GET /api/observability

**Tag**: Observability (registered dynamically by `core.observability.register_routes`) · **Auth**: Bearer required

Structured system health report — latest value per (category, name), bucketed
under the six canonical categories (`data_source` / `bot` / `strategy` /
`execution` / `ml` / `system`) plus an `other` bucket for ad-hoc metrics.
Includes overall metric count and oldest/newest sample ages.

**Response** (200): `observability.get_health_report()` payload.

**Errors**: 401 / 503.

---

### GET /api/observability/history/{name}

**Tag**: Observability (registered dynamically by `core.observability.register_routes`) · **Auth**: Bearer required

Return the most recent N samples for metric `name` (newest first).

**Path Parameters**:

| Name   | Type   | Required |
| ------ | ------ | -------- |
| `name` | string | yes      |

**Query Parameters**:

| Name    | Type | Required | Default | Constraints       |
| ------- | ---- | -------- | ------- | ----------------- |
| `limit` | int  | no       | 100     | `ge=1, le=1000`   |

**Response** (200):

```json
{
  "name":    "bot.cycle.duration_ms",
  "count":   100,
  "samples": [ /* most-recent-N rows from the observability table */ ]
}
```

**Errors**: 401 / 503.

---

## 14. Orders

### DELETE /api/orders

**Tag**: Trading (server.py) — grouped here by path prefix `/api/orders*` · **Auth**: Bearer required

Cancel every open order. Routes through `paper_sim.cancel_all()` in paper
mode, or `clob_client.cancel_all_orders()` + `store.cancel_all_orders()` in
live mode.

**Response** (200):

```json
{ "cancelled": 3 }
```

**Errors**: 401 / 503.

---

### DELETE /api/orders/{order_id}

**Tag**: Trading — grouped here by path prefix `/api/orders*` · **Auth**: Bearer required

Cancel a single order by id.

**Path Parameters**:

| Name        | Type   | Required |
| ----------- | ------ | -------- |
| `order_id`  | string | yes      |

**Response** (200):

```json
{ "cancelled": "<order_id>" }
```

**Errors**:
- 401 / 503
- 404: `Order <order_id> not found`

---

### GET /api/orders

**Tag**: Trading — grouped here by path prefix `/api/orders*` · **Auth**: Bearer required

List all currently-open orders.

**Response** (200):

```json
{
  "orders": [
    {
      "order_id":      "abc123",
      "token_id":      "0xabc...",
      "slug":          "will-x-happen",
      "side":          "BUY",
      "price":         0.58,
      "size":          17.24,
      "size_matched":  0.0,
      "strategy":      "manual",
      "paper":         true,
      "created_at":     1788409517.69
    }
  ],
  "count": 1
}
```

**Errors**: 401 / 503.

---

## 15. Positions

### GET /api/positions

**Tag**: Trading — grouped here by path prefix `/api/positions*` · **Auth**: Bearer required

List all currently-open positions.

**Response** (200):

```json
{
  "positions": [
    {
      "token_id":         "0xabc...",
      "slug":             "will-x-happen",
      "yes_shares":       17.24,
      "avg_entry_price":  0.5800,
      "total_invested":   10.00,
      "realised_pnl":     0.0
    }
  ],
  "count":     1,
  "daily_pnl": 0.12
}
```

**Errors**: 401 / 503.

---

### GET /api/positions/closed

**Tag**: Positions (registered dynamically by `core.closed_positions.register_routes`) · **Auth**: Bearer required

Return recent closed positions (most recent first).

**Query Parameters**:

| Name       | Type    | Required | Default | Constraints     | Notes                              |
| ---------- | ------- | -------- | ------- | --------------- | ---------------------------------- |
| `limit`    | int     | no       | 50      | `ge=1, le=500`  | Max rows to return                 |
| `strategy` | string? | no       | —       | —               | Filter to a single strategy name   |

**Response** (200):

```json
{
  "count":     12,
  "positions": [
    {
      "timestamp":         1788409517.69,
      "decision_id":       "dec-...",
      "token_id":          "0xabc...",
      "strategy":          "ml_random_forest_quant",
      "entry_price":       0.5800,
      "exit_price":        0.6200,
      "shares":            17.24,
      "pnl":               0.69,
      "holding_seconds":   186400.0,
      "model_version":     "v1.155.0",
      "direction":         "long_yes",
      "confidence":        0.2462,
      "predicted_edge":    0.0431,
      "p_yes":             0.6231,
      "market_mid":        0.5800,
      "liquidity":         5000.0,
      "data":              { /* decoded metadata_json extras */ }
    }
  ]
}
```

**Errors**: 401 / 503.

---

### GET /api/positions/closed/stats

**Tag**: Positions (registered dynamically by `core.closed_positions.register_routes`) · **Auth**: Bearer required

Aggregate P&L / win-rate / profit-factor roll-up across all recorded closed positions.

**Query Parameters**: none.

**Response** (200): `closed_positions.get_closed_stats()` payload — `total_pnl`, `n_positions`, `win_rate`, `profit_factor`, `avg_holding_seconds`, etc.

**Errors**: 401 / 503.

---

### POST /api/positions/{token_id}/close

**Tag**: Trading — grouped here by path prefix `/api/positions*` · **Auth**: Bearer required

One-click marketable close of an open position.

Long YES positions are closed by submitting a SELL order at the current
`best_bid` (a marketable limit — any resting bid ≥ best_bid is matched
immediately). Long NO positions are closed by submitting a BUY order at
the current `best_ask` (symmetric — covers the synthetic short).

Risk checks (`risk_manager.check_order`) are applied exactly as for a
manual `/api/trade`, and the order flows through `paper_sim` when
`settings.paper_trade` is true, or `clob_client` otherwise.

Use `dry_run=true` to preview the fill price, share count, and estimated realised P&L without submitting an order.

**Path Parameters**:

| Name       | Type   | Required |
| ---------- | ------ | -------- |
| `token_id` | string | yes      |

**Request Body** (`PositionCloseRequest`, all fields optional):

```json
{ "max_size_shares": 5.0, "dry_run": true }
```

| Field             | Type    | Required | Default | Constraints | Notes                                             |
| ----------------- | ------- | -------- | ------- | ----------- | ------------------------------------------------- |
| `max_size_shares` | float?  | no       | —       | `ge=0.0`    | Cap the close size for partial scale-outs         |
| `dry_run`         | bool    | no       | `false` | —           | If true, preview only — no order is submitted     |

**Response** (200, when `dry_run=false`):

```json
{
  "status":            "submitted",
  "token_id":          "0xabc...",
  "slug":              "will-x-happen",
  "order_id":          "close-0abc",
  "side":              "SELL",
  "price":             0.5800,
  "size_shares":       17.2400,
  "notional_usdc":     10.00,
  "estimated_pnl":     0.00,
  "best_bid":          0.5800,
  "best_ask":          0.5850,
  "book_updated_at":   1788409517.69,
  "paper_trade":       true,
  "remaining_position": {
    "yes_shares":              0.0,
    "no_shares":               0.0,
    "avg_entry_price":         0.5800,
    "total_invested_before":   10.00,
    "realised_pnl_before":     0.00
  },
  "note": "FOK marketable close submitted — paper_sim fill-loop will settle within ~1s in paper mode; live mode awaits exchange ack."
}
```

**Response** (200, when `dry_run=true`): same shape but `"status": "dry_run"` and `"note": "dry_run=true — no order submitted"`.

**Errors**:
- 401 / 503
- 404: `no open position for token '<token_id>' — nothing to close`
- 400: `requested close size resolves to 0 shares — nothing to close` OR `Risk rejection: <reason>`
- 502: no live order book OR best_bid/best_ask empty (poller re-prioritized)

---

## 16. Risk

### GET /api/exposure

**Tag**: Risk · **Auth**: Bearer required

Full exposure decomposition (mandate section 2).

**Response** (200): `compute_exposure()` payload — `maximum_remaining_loss`, `reserved_for_pending_orders`, `open_position_count`, per-token breakdown.

**Errors**: 401 / 503.

---

### GET /api/leaderboard

**Tag**: Risk · **Auth**: Bearer required

Strategy leaderboard ranked by reproducible risk-adjusted net performance.

**Response** (200): `leaderboard()` payload — per-strategy P&L, win-rate, Sharpe, sort order.

**Errors**: 401 / 503.

---

### POST /api/kill-switch/activate

**Tag**: Risk · **Auth**: Bearer required

Activate the kill switch (halt all trading).

**Request Body**: none.

**Response** (200):

```json
{ "status": "activated", "kill_switch": true }
```

Also writes a `🛑 KILL SWITCH activated — all trading halted` event to the
recent-events log. The durable kill-switch flag (`/app/data/kill_switch.flag`)
is written so the state survives restarts.

**Errors**: 401 / 503.

---

### POST /api/kill-switch/deactivate

**Tag**: Risk · **Auth**: Bearer required

Deactivate the kill switch (resume trading, subject to other gates).

**Request Body**: none.

**Response** (200):

```json
{ "status": "deactivated", "kill_switch": false }
```

Also writes a `▶ Kill switch deactivated — trading resumed` event.

**Errors**: 401 / 503.

---

### POST /api/risk/observation-mode

**Tag**: Risk · **Auth**: Bearer required

Toggle observation-only mode. When active, new live orders are blocked
(before they hit `clob_client`) — the bot still scans markets and updates
its model, but never places a live order.

**Request Body** (`ObservationModeRequest`):

```json
{ "active": true, "reason": "manual investigation — suspicious fill" }
```

| Field    | Type   | Required | Default | Notes                              |
| -------- | ------ | -------- | ------- | ---------------------------------- |
| `active` | bool   | yes      | —       | `true` = enable observation mode   |
| `reason` | string | no       | `""`    | Operator justification (logged)    |

**Response** (200): the dict returned by `risk_manager.set_observation_mode(active, reason)` plus a `status` key.

**Errors**: 401 / 503.

---

### GET /api/risk/reconcile

**Tag**: Risk · **Auth**: Bearer required

Reconciliation investigation for the current open exposure.

**Response** (200): `compute_reconciliation(bankroll_ceiling=float(BANKROLL_CEILING))` payload.

**Errors**: 401 / 503.

---

### GET /api/risk/strategies/paused

**Tag**: Risk (registered dynamically by `risk.routes.register_routes`) · **Auth**: Bearer required

Return currently paused (cooldown) strategies + active strategies.

**Response** (200):

```json
{
  "paused": [
    { "strategy": "signal_trader", "seconds_remaining": 287.4 }
  ],
  "active": [
    { "strategy": "mm_avellaneda_stoikov" },
    { "strategy": "ml_random_forest_quant" }
  ],
  "cooldown_seconds": 300.0,
  "threshold_usd":    0.50
}
```

- `paused` is sorted by `seconds_remaining` descending (most recently paused first); expired entries are filtered out.
- `active` is the set of strategies currently running per `strategy_registry.get_active_instances()` that are NOT in the paused set. Sorted by name for deterministic output.
- `cooldown_seconds` and `threshold_usd` are the configured `STRATEGY_COOLDOWN` and `PER_TRADE_MAX_LOSS` constants from `risk/manager.py` — lets operators compute "fraction of cooldown elapsed" without a second round-trip.

**Errors**: 401 / 503.

> **Read-only / non-mutating**: this snapshot reads
> `risk_manager._strategy_cooldowns.items()` directly without calling
> `is_strategy_paused` (whose lazy-clear contract would pop expired entries
> under read). Mutation is left to the next `check_order` call.

---

## 17. Shadow

### GET /api/shadow/comparison

**Tag**: Shadow (registered dynamically by `core.shadow_trading.register_routes`) · **Auth**: Bearer required

Shadow-vs-live side-by-side comparison — what the production strategy
*would have* traded vs what it *actually* traded.

**Query Parameters**: none.

**Response** (200): `get_shadow_vs_live_comparison()` payload.

**Errors**: 401 / 503.

---

### GET /api/shadow/trades

**Tag**: Shadow (registered dynamically by `core.shadow_trading.register_routes`) · **Auth**: Bearer required

Return recent counterfactual trades (most recent first).

**Query Parameters**:

| Name       | Type    | Required | Default | Constraints     | Notes                              |
| ---------- | ------- | -------- | ------- | --------------- | ---------------------------------- |
| `limit`    | int     | no       | 50      | `ge=1, le=500`  | Max shadow trades to return        |
| `strategy` | string? | no       | —       | —               | Filter by strategy name            |

**Response** (200):

```json
{
  "count":  8,
  "trades": [
    {
      "timestamp":       1788409517.69,
      "decision_id":    "dec-...",
      "token_id":       "0xabc...",
      "strategy":        "ml_random_forest_quant",
      "side":            "BUY",
      "price":           0.5800,
      "size":            17.24,
      "predicted_edge":  0.0431,
      "confidence":      0.2462
    }
  ]
}
```

**Errors**: 401 / 503.

---

## 18. Strategies

### GET /api/strategies/catalog

**Tag**: Strategies · **Auth**: Bearer required

Return all 50 strategies with metadata, category, and running state.

**Response** (200):

```json
{
  "catalog": [ /* strategy_registry.get_catalog() */ ],
  "total":   50
}
```

**Errors**: 401 / 503.

---

### POST /api/strategies/toggle

**Tag**: Strategies · **Auth**: Bearer required

Dynamically start or stop any of the 50 strategies at runtime.

**Request Body** (`StrategyToggleRequest`):

```json
{ "strategy_name": "ml_random_forest_quant", "enabled": true }
```

| Field            | Type   | Required | Notes                              |
| ---------------- | ------ | -------- | ---------------------------------- |
| `strategy_name`  | string | yes      | Case-insensitive; lower-cased     |
| `enabled`        | bool   | yes      | `true` to start, `false` to stop   |

**Response** (200):

```json
{ "status": "started", "strategy": "ml_random_forest_quant" }
```

or, when stopping:

```json
{ "status": "stopped", "strategy": "ml_random_forest_quant" }
```

(or `"status": "not_running"` if a stop was requested for an inactive strategy).

**Errors**:
- 401 / 503
- 400: `Strategy <strat_id> not found in catalog`

---

## 19. System

### GET /api/analytics

**Tag**: System · **Auth**: Bearer required

Aggregate trading analytics with Wilson 95% confidence intervals, profit
factor, expectancy, unrealized P&L (mark-to-live-mid), equity, max
drawdown, exposure decomposition, and data freshness.

**Response** (200):

```json
{
  "equity":                    111.72,
  "realized_pnl":              11.72,
  "unrealized_pnl":            0.42,
  "net_pnl":                   12.14,
  "total_trades":              42,
  "winning_trades":            34,
  "losing_trades":             8,
  "closed_trades":             42,
  "open_trades":               0,
  "win_rate":                  0.8095,
  "win_rate_ci_low":           0.6513,
  "win_rate_ci_high":          0.9047,
  "profit_factor":             4.21,
  "avg_win":                   0.41,
  "avg_loss":                  -0.13,
  "expectancy":                0.30,
  "sharpe_ratio":              1.42,
  "max_drawdown_dollars":      2.30,
  "max_drawdown_pct":          0.0230,
  "total_volume_usdc":         420.00,
  "open_exposure":             0.00,
  "open_position_count":       0,
  "pending_order_capital":     0.00,
  "risk_utilization":          0.0000,
  "mode":                      "paper",
  "data_freshness_seconds":    1.2,
  "peak_equity":               114.02,
  "active_strategies":         ["mm_avelaneda_stoikov", "ml_random_forest_quant", ...]
}
```

**Errors**: 401 / 503.

---

### GET /api/events

**Tag**: System · **Auth**: Bearer required

Return the most recent N human-readable events from the in-memory event log.

**Query Parameters**:

| Name | Type | Required | Default | Notes                |
| ---- | ---- | -------- | ------- | -------------------- |
| `n`  | int  | no       | 50      | Number of events     |

**Response** (200):

```json
{
  "events": [ /* newest-last; reversed for UI display */ ],
  "count":  50
}
```

**Errors**: 401 / 503.

---

### GET /api/health

**Tag**: System · **Auth**: **public** (the only unauthenticated route)

Liveness probe.

**Response** (200):

```json
{ "status": "ok", "timestamp": 1788409517.69, "paper": true }
```

**Errors**: none (intentionally public; never 401 / 503).

---

### GET /api/history/equity

**Tag**: System · **Auth**: Bearer required

Return the in-memory equity curve (paper-balance samples written by the
paper simulator's fill loop).

**Response** (200):

```json
{ "points": [ /* list of {t, equity} tuples */ ], "count": 42 }
```

**Errors**: 401 / 503.

---

### GET /api/snapshot

**Tag**: System · **Auth**: Bearer required

Return the full snapshot payload — what the WebSocket broadcasts on every
1-second tick.

**Response** (200):

```json
{
  "type":                   "snapshot",
  "timestamp":              1788409517.69,
  "mode":                   "paper",
  "kill_switch":            false,
  "kill_switch_durable":    false,
  "observation_only":       false,
  "observation_reason":     "",
  "daily_pnl":              0.12,
  "paper_balance":          111.72,
  "strategies":             ["mm_avelaneda_stoikov", ...],
  "order_books":            [ /* compact book summaries */ ],
  "open_orders":            [ /* compact order summaries */ ],
  "positions":              [ /* positions with current_price + unrealized_pnl */ ],
  "recent_trades":          [ /* last 50 trades */ ],
  "events":                 [ /* last 50 events */ ],
  "ml":                     {
    "model_ready":        true,
    "brier_score":        0.1013,
    "roc_auc":            0.9451,
    "ece":                0.0836,
    "n_updates":          12,
    "drift_status":       "CLEAR",
    "drift_psi":          0.03,
    "drift_brier":        null,
    "drift_ewma_brier":   null,
    "adaptive_weights":   { "rf": 0.4, ... },
    "meta_learner_warm":  true,
    "training_source":    "synthetic_seed"
  }
}
```

**Errors**: 401 / 503.

---

### GET /api/status

**Tag**: System · **Auth**: Bearer required

Risk-manager status report augmented with mode, strategies, paper balance,
seeded-market count, tracked-book count, book-poller stats, vector-doc
count, and the durable kill-switch flag.

**Response** (200):

```json
{
  /* risk_manager.status_report() */
  "mode":                  "paper",
  "strategies":            ["mm_avelaneda_stoikov", ...],
  "paper_balance":         111.72,
  "seeded_markets":        60,
  "tracked_books":         60,
  "book_poller":           { /* book_poller.stats */ },
  "vector_docs_indexed":   500,
  "kill_switch_durable":   false
}
```

**Errors**: 401 / 503.

---

### POST /api/system/prune

**Tag**: System (registered dynamically by `core.retention.register_routes`) · **Auth**: Bearer required

Trigger a data-retention prune across one or all stores. Each store has its
own retention horizon (env-var-driven). The endpoint never 500s on a DB
error — underlying prune functions swallow + log persistence errors and
report `pruned=0` (or `error` set in the all-target summary).

**Request Body** (`PruneRequest`, optional — `POST` with no body defaults to `{"target":"all"}`):

```json
{ "target": "all" }
```

| Field    | Type   | Required | Default | Allowed values                                                       |
| ------- | ------ | -------- | ------- | --------------------------------------------------------------------- |
| `target`| string | no       | `"all"` | `all` / `observability` / `decision_ledger` / `execution_quality` / `audit_events` |

**Response** (200, when `target="all"`):

```json
{
  "observability":        { "pruned": 12, "db": "/app/data/observability.db", "error": null },
  "decision_ledger":      { "pruned": 3,  "db": "/app/data/decision_ledger.db", "error": null },
  "execution_quality":    { "pruned": 1,  "db": "/app/data/execution_quality.db", "error": null },
  "audit_events":         { "pruned": 45, "db": "/app/data/audit_events.db", "error": null },
  "total_pruned":         61
}
```

**Response** (200, when `target=<single>`):

```json
{ "target": "observability", "pruned": 12 }
```

**Errors**:
- 401 / 503
- 400: `unknown prune target: '<x>'. Valid targets: all, observability, decision_ledger, execution_quality, audit_events.`

---

### GET /api/system/health

**Tag**: System · **Auth**: Bearer required

Honest pipeline health: real component checks only — no hardcoded values.

Status derivation:
- **UNHEALTHY** if any CRITICAL finding (kill switch, circuit breakers) OR the database is unreachable.
- **DEGRADED** on WARNING findings (stale heartbeats, feed stall).
- **HEALTHY** otherwise.

**Response** (200):

```json
{
  "status":              "HEALTHY",
  "status_derivation":   "computed from live component checks (no hardcoded values)",
  "timestamp":            1788409517.69,
  "checks": {
    "kill_switch":     { "status": "CLEAR", "detail": "no kill switch active" },
    "timescale_db":    { "status": "UP", "detail": "postgres — 1234 snaps / 5678 ticks / 0 failed writes" },
    "reconciliation":  { "status": "UP", "detail": "clean at 1788409517.69" },
    "book_poller":     { "status": "UP", "detail": "100 success / 2 errors — 60 tracked books" },
    "ml_engine":       { "status": "UP", "detail": "active model v1.155.0, online updates 12, training data: synthetic" },
    "watchdog":        { "status": "UP", "detail": "9 subsystems registered, 0 stale" }
  },
  "poller":             { /* tier1_tokens, tier2_tokens, total_tracked, success_rate, error_count, latency_ms, oldest_book_age_seconds, ws_client_started */ },
  "ml_engine":          { /* active_version, brier_score, psi_drift, drift_status, training_data_kind */ },
  "timescale_db":       { /* db_stats */ },
  "storage":            { /* database_engine, vector_index_size, audit_trail_backend, market_intelligence_db, state_persistence */ },
  "services":           [ { "name": "FastAPI Server", "status": "UP", "port": 8080 }, ... ],
  "tripwires":          { "critical": [], "warnings": [] },
  "mode":               "paper"
}
```

**Errors**: 401 / 503 (auth).

---

### GET /api/system/mode

**Tag**: System · **Auth**: Bearer required

Canonical, network-visible trading mode and safety posture (P0-GOV-01).

**Response** (200):

```json
{
  "mode":                  "paper",
  "paper_trade":           true,
  "live_trading_enabled":  false,
  "auth_enforced":         true,
  "kill_switch":           false,
  "kill_switch_durable":   false,
  "weekly":                { /* store.weekly_pnl_snapshot() */ },
  "mode_derivation":       "TRADING_MODE/PAPER_TRADE env — single source of truth"
}
```

**Errors**: 401 / 503.

---

## 20. Trading

### POST /api/trade

**Tag**: Trading · **Auth**: Bearer required

Place a new manual trade order.

**Request Body** (`ManualTradeRequest`):

```json
{
  "token_id":  "0xabc...",
  "price":     0.58,
  "side":      "BUY",
  "size_usdc": 10.00
}
```

| Field       | Type   | Required | Constraints          | Notes                                |
| ----------- | ------ | -------- | -------------------- | ------------------------------------ |
| `token_id`  | string | yes      | —                    | Polymarket CLOB token id             |
| `price`     | float  | yes      | `gt=0, lt=1`         | Limit price (0..1 exclusive)         |
| `side`      | string | yes      | regex `^(BUY\|SELL\|buy\|sell)$` | Direction                         |
| `size_usdc` | float  | no       | `gt=0`, default 10.0 | USD notional — converted to shares via `size_usdc / price` |

**Response** (200):

```json
{
  "status": "placed",
  "order":  { /* Order dataclass.to_dict() — order_id, token_id, side, price, size, strategy, paper, created_at */ }
}
```

Pre-trade risk validation runs through `risk_manager.check_order()` for both
paper and live orders. If rejected, a `⚠ Risk block [manual]: <reason>`
event is logged and the request fails with 400.

**Errors**:
- 401 / 503
- 400: `Risk rejection: <reason>` OR `Failed to place order`
- 422: Pydantic validation (price out of band, missing required field, side not matching regex)

---

### GET /api/trades

**Tag**: Trading · **Auth**: Bearer required

Return the most recent N trades (most recent first).

**Query Parameters**:

| Name    | Type | Required | Default | Notes                |
| ------- | ---- | -------- | ------- | -------------------- |
| `limit` | int  | no       | 50      | Max trades to return |

**Response** (200):

```json
{
  "trades": [
    {
      "trade_id":   "trd-...",
      "slug":       "will-x-happen",
      "side":        "BUY",
      "price":       0.58,
      "size":        17.24,
      "pnl":         0.69,
      "strategy":    "ml_random_forest_quant",
      "paper":       true,
      "timestamp":   1788409517.69
    }
  ],
  "count": 50
}
```

**Errors**: 401 / 503.

---

## 21. Live Safety

### GET /api/live/readiness

**Tag**: Live (registered dynamically by `core.live_safety_gate.register_routes`) · **Auth**: Bearer required

Run all 10 God Mode §82 staged checks and return the verdict dict. Never
500s — a check that throws records itself as failed.

**Response** (200):

```json
{
  "passed":           false,
  "passed_count":      8,
  "total_count":       10,
  "blocking_checks":  ["clob_credentials_present", "live_trading_enabled_env"],
  "checks": [
    { "name": "kill_switch_clear",          "passed": true,  "detail": "no kill switch active" },
    { "name": "clob_credentials_present",    "passed": false, "detail": "POLY_PRIVATE_KEY not set" },
    /* ... 8 more ... */
  ],
  "checked_at": 1788409517.69
}
```

**Errors**: 401 / 503 (auth — endpoint itself never 500s on a check failure).

---

### POST /api/live/enable

**Tag**: Live (registered dynamically by `core.live_safety_gate.register_routes`) · **Auth**: Bearer required

Attempt to flip the bot into live trading mode. Requires `confirm=true`
(defence against accidental double-click). Runs `check_live_readiness()`
first; if any check fails, returns HTTP 409 with the blocking-check list
and the full readiness payload. On success, flips the in-memory mode flags
(`live_trading_enabled=True`, `trading_mode="live"`, `paper_trade=False`)
and logs an audit event.

> ⚠️ **In-memory flip only**: the change does NOT persist across process
> restarts. For durable activation, set `TRADING_MODE=live` and
> `LIVE_TRADING_ENABLED=true` in `.env` and restart — the response payload
> carries this guidance.

**Request Body** (`EnableLiveRequest`):

```json
{ "confirm": true, "reason": "operator request — pre-production smoke test" }
```

| Field     | Type   | Required | Default | Notes                                                          |
| --------- | ------ | -------- | ------- | -------------------------------------------------------------- |
| `confirm` | bool   | no       | `false` | Must be `true` to authorise activation                          |
| `reason`  | string | no       | `""`    | Operator justification (recorded in the audit trail)           |

**Response** (200, when all §82 checks pass):

```json
{
  "status":              "live_trading_enabled",
  "mode":                "live",
  "paper_trade":         false,
  "live_trading_enabled": true,
  "guidance":            "In-memory flags flipped — set TRADING_MODE=live + LIVE_TRADING_ENABLED=true in .env and restart for durable activation."
}
```

**Errors**:
- 401 / 503 (auth)
- 400: `confirm=true is required to enable live trading (defence against accidental activation).`
- 409: `Live trading NOT enabled — God Mode §82 safety gate failed.` — body carries `{ message, passed_count, total_count, blocking_checks, checks, guidance }`

---

## 22. Framework Routes

These four routes are auto-generated by FastAPI / Starlette or implemented as
a WebSocket. They are not backend domain endpoints, but they are part of the
77-route surface counted by the X9 audit (`app.routes` enumeration).

### GET /docs

**Tag**: Framework · **Auth**: public in paper mode; bearer-protected in live mode

Swagger UI auto-generated from the OpenAPI schema. Disabled in
`TRADING_MODE=live` (dropped from `PUBLIC_PATHS` at module load).

**Response** (200): HTML page.

---

### GET /redoc

**Tag**: Framework · **Auth**: public in paper mode; bearer-protected in live mode

ReDoc UI auto-generated from the OpenAPI schema. Disabled in
`TRADING_MODE=live`.

**Response** (200): HTML page.

---

### GET /openapi.json

**Tag**: Framework · **Auth**: public in paper mode; bearer-protected in live mode

OpenAPI 3.x JSON schema auto-generated by FastAPI. Disabled in
`TRADING_MODE=live`.

**Response** (200): OpenAPI JSON document.

---

### WS /ws

**Tag**: Framework · **Auth**: `?token=<API_TOKEN>` query param required

WebSocket endpoint. On connect, the server immediately pushes a full
`_build_snapshot()` payload; thereafter it broadcasts a fresh snapshot
every 1 second (driven by `_broadcast_loop`). The server also reads
incoming text frames with a 30-second timeout (used as a keep-alive —
client pings are tolerated but ignored).

**Connection URL**:

```
ws://<host>:8080/ws?token=<API_TOKEN>
```

(Through the Caddy gateway: `ws://<host>:81/ws?token=<API_TOKEN>`)

**Auth**: `?token=<API_TOKEN>` query param, compared with
`hmac.compare_digest` against `settings.api_token`. If absent or wrong,
the server closes the connection with code `4401 Unauthorized`. If
`API_TOKEN` is not configured at all, the server also closes 4401
(fail-closed).

**First message (server → client, on connect)**: a JSON-serialised snapshot
dict (same shape as `GET /api/snapshot`).

**Subsequent messages (server → client, every 1s)**: same snapshot shape.

**Errors**:
- 4401: WebSocket close (no token / wrong token / token unconfigured)

---

## Appendix — Route Registration Map

The X9 audit (`api/server.py` lines ~2310-2413) verifies all 13
`register_routes` modules are wired. The full inventory:

| Module                              | Where wired in `server.py` | Routes added                                                                          |
| ----------------------------------- | --------------------------- | ------------------------------------------------------------------------------------- |
| `core.decision_ledger`              | line ~2105 (R11 block)      | `GET /api/decision/{token_id}`, `GET /api/decisions/rejected`                         |
| `core.execution_quality`            | line ~2117 (S14 block)      | `GET /api/execution-quality`                                                          |
| `core.observability`                | line ~2127 (S13 block)      | `GET /api/observability`, `GET /api/observability/history/{name}`                     |
| `core.closed_positions`             | line ~2139 (S15 block)      | `GET /api/positions/closed`, `GET /api/positions/closed/stats`                        |
| `core.attribution`                  | line ~2140 (S15 block)      | `GET /api/attribution`                                                                |
| `core.capital_allocator`            | line ~2165 (T5 block)       | `GET /api/capital/allocation`                                                         |
| `core.shadow_trading`               | line ~2197 (T1 block)       | `GET /api/shadow/trades`, `GET /api/shadow/comparison`                                |
| `core.live_safety_gate`             | line ~2207 (T2 block)       | `GET /api/live/readiness`, `POST /api/live/enable`                                    |
| `ml.validation`                     | line ~2219 (T3 block, try/except `ImportError`) | `POST /api/ml/validate`                                    |
| `core.retention`                    | line ~2241 (T6 block)       | `POST /api/system/prune`                                                              |
| `ml.routes`                         | line ~2252 (T8 block)       | `GET /api/ml/versions`, `POST /api/ml/rollback`                                        |
| `risk.routes`                       | line ~2265 (V12 block)      | `GET /api/risk/strategies/paused`                                                     |
| `core.observability_collector`      | line ~2287 (W11 block)      | **NO HTTP ROUTES ADDED** — wraps `app.router.lifespan_context` to start a 30s background collector |

**Total dynamic REST routes added**: 18
**Total server.py `@app` HTTP routes**: 55 (54 unique paths + 1 duplicate `/api/ml/drift`)
**Total server.py WebSocket routes**: 1 (`/ws`)
**Total FastAPI auto-generated framework routes**: 3 (`/docs`, `/redoc`, `/openapi.json`)

**Grand total: 77 routes** (76 HTTP + 1 WebSocket).

This count matches the X9 audit log line:
```
[X9 route audit] OK=13 modules (...); missing=0 (<none>); HTTP routes on app=76
```
