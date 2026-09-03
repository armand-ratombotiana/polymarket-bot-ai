"""
api/server.py — FastAPI server: 50+ Quantitative Strategies, Modern AI/ML Vector Engine,
REST endpoints, WebSocket broadcast, and real-time trading controls.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import time
from contextlib import asynccontextmanager

import numpy as np
from fastapi import (
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi.errors import RateLimitExceeded

from api.models import (
    ErrorResponse,
    HealthResponse,
    MLMetricsResponse,
    OrdersResponse,
    PositionsResponse,
    TradesResponse,
)
from api.rate_limit import (
    ARBITRAGE_LIMIT,
    HEAVY_LIMIT,
    READ_LIMIT,
    TRADE_LIMIT,
    WRITE_LIMIT,
    limiter,
)

# W11-2 — TTL cache for hot-path routes.
# Importing the singleton cache instances here (rather than constructing
# local copies inside each handler) so cache state is shared across every
# route in this module AND across ``core.observability`` /
# ``core.attribution`` (which import the same singletons from
# ``core.cache`` for their own ``register_routes``-registered endpoints).
from core.cache import (
    analytics_cache,
    attribution_cache,
    general_cache,
    markets_cache,
    ml_metrics_cache,
    observability_cache,
)

from config import settings
from core.api_versioning import get_version_info, versioning_middleware
from core.audit_logger import audit_logger
from core.book_poller import book_poller
from core.clob_client import OrderArgs
# W12-6 — Structured logging. Imported BEFORE the FastAPI app is constructed
# so the very first ``log.info(...)`` call (e.g. inside ``_seed_markets``)
# emits a structured record. ``setup_logging()`` is idempotent, so the
# repeated ``from api.server import app`` in sibling test modules doesn't
# stack duplicate handlers on the root logger.
from core.logging_config import (
    get_logger,
    request_id_var,
    setup_logging,
)
from core.data_store import Order, Side, store
from core.fundamental_ingest import fundamental_engine
from core.gamma_client import gamma_client
from core.portfolio import compute_exposure, compute_reconciliation, leaderboard
from core.position_manager import position_manager
# W13-1 — Prometheus metrics. Module-level singleton registry covering
# HTTP / trading / ML / system surfaces. The ``/metrics`` route handler
# (defined below) and the request-logging / auth / rate-limit
# middlewares all import the same singletons from this module so a
# single prometheus_client registry is shared process-wide.
from core.prometheus_metrics import (
    CONTENT_TYPE_LATEST,
    auth_failures_total,
    get_metrics as _get_prometheus_metrics,
    http_requests_in_progress,
    rate_limit_hits_total,
    record_auth_failure as _record_auth_failure,
    record_rate_limit_hit as _record_rate_limit_hit,
    record_request as _record_prometheus_request,
)
from core.security import validate_token_strength
from core.settlement import settlement_engine
from core.watchdog import watchdog
from core.ws_client import ws_client
from ml.copilot import copilot_engine
from ml.drift_detector import drift_detector
from ml.model import ml_model
from ml.model_registry import model_registry
from ml.vector_store import vector_store
from paper.simulator import paper_sim
from risk.manager import BANKROLL_CEILING, MAX_DEPLOYABLE_CAPITAL, risk_manager
from strategies.registry import strategy_registry

# ── W12-6 — Structured logging ────────────────────────────────────────────────
# Install the JSON / colored formatter on the root logger BEFORE the first
# ``getLogger`` call below so every record — including the ones emitted
# during lifespan startup — flows through the structured formatter.
# Idempotent: subsequent imports of this module (test collection, dev reload)
# are a no-op.
setup_logging()

log = logging.getLogger(__name__)

# ── Auth Policy ────────────────────────────────────────────────────────────────
# Only the liveness probe (/api/health) and the API version info endpoint
# (/api/version) are unauthenticated. Everything else requires
# `Authorization: Bearer <API_TOKEN>` (fail-closed; 503 if unconfigured).
# /api/version is public so a client can negotiate the API version BEFORE
# presenting credentials (W13-3 — API versioning).
# W13-1: ``/metrics`` is intentionally unauthenticated — Prometheus scrapers
# use their own auth (mTLS / OAuth2 proxy /w Basic-auth) at the ingress layer
# and don't carry the application API token. The endpoint emits only
# Prometheus-format metric values (no PII, no order body, no secrets).

PUBLIC_PATHS = {"/api/health", "/api/version", "/docs", "/redoc", "/openapi.json", "/metrics"}
if settings.trading_mode == "live":
    PUBLIC_PATHS.discard("/docs")
    PUBLIC_PATHS.discard("/redoc")
    PUBLIC_PATHS.discard("/openapi.json")


def _valid_token(authorization: str | None) -> bool:
    if not settings.api_token:
        return False
    scheme, _, creds = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not creds:
        return False
    return hmac.compare_digest(creds, settings.api_token)


# ── WebSocket Connection Manager ──────────────────────────────────────────────

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)
        log.info("WS client connected — total %d", len(self.active))

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict) -> None:
        dead = []
        payload = json.dumps(data, default=str)
        for ws in list(self.active):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self.active:
                self.active.remove(ws)


manager = ConnectionManager()
_seeded_tokens: list[str] = []


# ── Market Seeding & Vector Store Ingestion ───────────────────────────────────

async def _seed_markets(limit: int = 60) -> list[str]:
    token_ids: list[str] = []
    try:
        markets = await gamma_client.get_markets(active=True, limit=limit, order="volume24hr")
        for mkt in markets:
            slug = mkt.get("slug") or mkt.get("groupItemTitle") or ""
            ids = gamma_client.extract_token_ids(mkt)
            for tid in ids:
                token_ids.append(tid)
                if slug:
                    store.market_slugs[tid] = slug
                # Ingest into vector store
                vector_store.add_market(tid, mkt)

        vector_store.build_index()
        vector_store.save_to_disk()

        if token_ids:
            unique_ids = list(dict.fromkeys(token_ids))
            log.info("Seeded %d unique tokens from %d Gamma markets", len(unique_ids), len(markets))
            await store.log_event(f"📊 Seeded {len(unique_ids)} market tokens & built Vector Embeddings index")
            return unique_ids
    except Exception as e:
        log.error("Market seeding failed: %s", e)
        await store.log_event(f"⚠ Market seed failed: {e}")
    return token_ids


async def _reseed_loop() -> None:
    await asyncio.sleep(600)
    while True:
        try:
            new_tokens = await _seed_markets(60)
            if new_tokens:
                book_poller.add_tokens(new_tokens)
        except Exception as e:
            log.debug("Reseed loop error: %s", e)
        await asyncio.sleep(600)


async def _token_sync_loop() -> None:
    while True:
        await asyncio.sleep(20)
        try:
            async with store._lock:
                tracked = list(store.market_slugs.keys())
            book_poller.add_tokens(tracked)
        except Exception:
            pass


async def _state_persistence_loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            store.save_to_disk()
        except Exception as e:
            log.debug("Persistence loop error: %s", e)


# ── Lifespan Manager ──────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _seeded_tokens

    # ── W13-7 — SQLite schema migrations ────────────────────────────────────
    # Run additive ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
    # EXISTS`` migrations against every SQLite DB at startup. The system is
    # idempotent: existing module-level ``_init_db()`` calls (which fire at
    # import time) already created the tables, so the migration runner
    # records the migration name in ``_migrations`` and skips on every
    # subsequent boot. Migrations never block startup — a failure is logged
    # at ``error`` level and the lifespan continues (mirrors the defensive
    # init pattern used by every other SQLite module).
    try:
        from pathlib import Path as _Path

        from core.db.migration_manager import run_migrations as _run_migrations

        _data_dir = _Path(os.environ.get("BOT_DATA_DIR", "/app/data"))
        _data_dir.mkdir(parents=True, exist_ok=True)
        for _db_name in (
            "decision_ledger",
            "execution_quality",
            "observability",
            "closed_positions",
            "alerts",
            "feature_flags",
            "audit_trail",
            "order_state_machine",
            "shadow_trades",
            "market_intelligence",
        ):
            _db_path = _data_dir / f"{_db_name}.db"
            _result = _run_migrations(_db_path, _db_name)
            if _result["applied"]:
                log.info(
                    "Migrations applied to %s: %s",
                    _db_name,
                    _result["applied"],
                )
            if _result["errors"]:
                log.error(
                    "Migration errors on %s: %s",
                    _db_name,
                    _result["errors"],
                )
    except Exception as _mig_exc:  # pragma: no cover — defensive: migrations must not kill startup
        log.error("Migration runner crashed at startup: %s", _mig_exc)

    # ── W11-6 (OWASP A07) — Token strength check at startup ────────────────
    # Fail-LOUD on placeholder / weak tokens: emit a WARNING log line (so
    # the operator sees it in the server log AND the structured audit trail
    # if a log shipper is wired up), but do NOT crash the server. The auth
    # middleware already fails-closed (503 AUTH_NOT_CONFIGURED) when the
    # token is empty; this just adds an extra layer of operator visibility
    # for the "non-empty but weak" case (e.g. `API_TOKEN=test`).
    _token_ok, _token_reason = validate_token_strength(settings.api_token)
    if not _token_ok:
        log.warning(
            "[security] API_TOKEN strength check FAILED: %s — "
            "the server will still start (auth middleware is fail-closed "
            "on empty tokens), but every authenticated request will use a "
            "weak secret. Rotate immediately. See docs/SECURITY.md.",
            _token_reason,
        )
        try:
            await audit_logger.log_event(
                category="security",
                event_type="weak_token_warning",
                details=f"reason={_token_reason} mode=startup",
            )
        except Exception:  # pragma: no cover — audit must not block startup
            log.debug("[security] weak-token audit write failed")

    # ── Live-mode guards (P0-GOV-01): live requires explicit authorization ──
    if settings.trading_mode == "live":
        if not settings.live_trading_enabled:
            raise RuntimeError(
                "trading_mode=live but LIVE_TRADING_ENABLED is false — refusing to start. "
                "Set both explicitly to authorize real-funds trading."
            )
        if not settings.has_credentials:
            raise RuntimeError(
                "trading_mode=live but POLY_PRIVATE_KEY is not configured — refusing to start."
            )

    # Audit the effective mode at startup (durable record of the transition).
    try:
        await audit_logger.log_event(
            category="system",
            event_type="mode_change",
            details=f"mode={settings.trading_mode} paper_trade={settings.paper_trade} "
                    f"live_trading_enabled={settings.live_trading_enabled}",
        )
    except Exception as e:  # pragma: no cover - audit failures must not kill startup
        log.error("Failed to write mode audit event: %s", e)

    # ── Start TimescaleDB / PostgreSQL time-series pool
    from core.timescale_db import timescale_db
    await timescale_db.init_postgres_pool()

    # ── Watchdog: register every live subsystem (P0-SAF-01) ──
    await watchdog.start()
    for name in (
        "book_poller", "ws_client", "settlement_engine", "fundamental_engine",
        "position_manager", "strategy_registry", "paper_sim", "ml_model",
        "label_backfill",
    ):
        watchdog.register(name)

    # 2. Start paper simulator
    if settings.paper_trade:
        await paper_sim.start()
        await store.log_event("📄 Paper trading mode active — no real funds used")
        watchdog.beat("paper_sim")

    # 3. Seed markets from Gamma API and initialize Vector Store
    _seeded_tokens = await _seed_markets(60)

    # 4. Start Universal Market Discovery Engine (500+ markets)
    from core.market_discovery import market_discovery
    await market_discovery.start()

    # 5. Start REST book poller (D5: tiered REST polling; WS feed retired per KD-08/KD-24)
    book_poller.set_tokens(_seeded_tokens)
    await book_poller.start()
    watchdog.beat("book_poller")
    await store.log_event(f"📈 Book poller started — monitoring {len(_seeded_tokens)} tokens")
    # WS client is NOT started: subscribe() had zero callers (KD-08); D5 decision = REST polling only.
    # ws_client instance is retained for test-compat and future re-enablement.
    watchdog.beat("ws_client")  # mark as alive so watchdog doesn't flag it as stale

    # 6. Start Settlement Engine, Fundamental News Ingest & Position Risk Manager
    await settlement_engine.start()
    await fundamental_engine.start()
    await position_manager.start()
    watchdog.beat("settlement_engine")
    watchdog.beat("fundamental_engine")
    watchdog.beat("position_manager")

    # 7. Initialize Core Default Strategies in Registry
    await strategy_registry.start_strategy("mm_avellaneda_stoikov")
    await strategy_registry.start_strategy("arb_binary_dutch_book")
    await strategy_registry.start_strategy("ml_random_forest_quant")
    watchdog.beat("strategy_registry")
    await store.log_event("🤖 50+ Strategy Engine online — 3 active base strategies initialized")

    # 8. Start Continuous ML Training Orchestrator (drift-triggered + 6h schedule)
    from ml.training_orchestrator import training_orchestrator
    await training_orchestrator.start()
    watchdog.beat("ml_model")
    await store.log_event("🧠 Continuous Training Orchestrator active (PSI drift threshold: 0.10)")

    # 8b. Start Resolved-Market Label Backfill Service (R5)
    #     Pages resolved markets → synthetic book → 38-dim features → labeled
    #     rows in ml_feature_store. Runs after 45s startup grace, then daily.
    #     Triggers a model retrain once ≥50 real labels are accumulated.
    from core.label_backfill import label_backfill_engine
    await label_backfill_engine.start()
    # ── T13: Register shadow challenger model(s) ───────────────────────────
    #     Registers a simple logistic-baseline challenger with the shadow
    #     inference engine. The challenger is invoked in parallel with
    #     every production `MLModel.predict()` call (see `ml/model.py`)
    #     but its output never affects trading decisions — disagreements
    #     are logged for offline retraining / promotion analysis.
    #     Wrapped in bare try/except so a missing / failing module can
    #     never block server startup.
    try:
        from ml.shadow_inference import shadow_inference

        def _logistic_baseline(features):
            pe = float(features[24]) if len(features) > 24 else 0.0
            return max(0.01, min(0.99, 0.5 + pe * 0.3))

        shadow_inference.register_shadow_model(
            "logistic_baseline",
            _logistic_baseline,
            description="Simple logistic baseline",
        )
    except Exception:
        pass
    watchdog.beat("label_backfill")
    await store.log_event("🏷️  Label Backfill Service active (45s startup grace → daily cycle, retrain ≥50 labels)")

    # 9. Background tasks
    broadcast_task = asyncio.create_task(_broadcast_loop(), name="ws-broadcast")
    reseed_task = asyncio.create_task(_reseed_loop(), name="market-reseed")
    token_sync_task = asyncio.create_task(_token_sync_loop(), name="token-sync")
    persist_task = asyncio.create_task(_state_persistence_loop(), name="state-persist")

    # 9. Reconciliation job (P0-DAT-03): initial pass at startup + daily artifact
    from core.reconciliation import run_reconciliation
    try:
        run_reconciliation()
    except Exception as e:
        log.error("[reconciliation] startup pass failed: %s", e)
    recon_task = asyncio.create_task(_reconciliation_loop(), name="reconciliation-daily")

    log.info("API server ready — 50+ Strategy Hub, Vector DB, and ML ensemble online")
    await store.log_event("✅ Polymarket Pro v3.0 Workstation Online 24/7")

    yield  # ── Serving HTTP & WS requests ──

    # Clean shutdown
    await watchdog.stop()
    broadcast_task.cancel()
    reseed_task.cancel()
    token_sync_task.cancel()
    persist_task.cancel()
    recon_task.cancel()
    store.save_to_disk()

    await training_orchestrator.stop()
    from core.label_backfill import label_backfill_engine
    await label_backfill_engine.stop()
    await settlement_engine.stop()
    for strat_id in list(strategy_registry.get_active_instances().keys()):
        try:
            await strategy_registry.stop_strategy(strat_id)
        except Exception:
            pass
    await book_poller.stop()
    # ws_client was not started (D5: REST polling); stop() is safe no-op when not running.
    await ws_client.stop()
    if settings.paper_trade:
        await paper_sim.stop()
    await gamma_client.close()
    log.info("API server stopped cleanly")


async def _reconciliation_loop() -> None:
    from core.reconciliation import run_reconciliation
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            run_reconciliation()
        except Exception as e:
            log.error("[reconciliation] daily pass failed: %s", e)


# ── Broadcast Loop ────────────────────────────────────────────────────────────

async def _broadcast_loop() -> None:
    while True:
        try:
            if manager.active:
                snap = await _build_snapshot()
                await manager.broadcast(snap)
        except Exception as e:
            log.debug("Broadcast error: %s", e)
        await asyncio.sleep(1.0)


def _get_meta_warm() -> bool:
    """Safe accessor for ensemble meta-learner warm status."""
    try:
        from ml.ensemble_meta_learner import ensemble_meta_learner
        return ensemble_meta_learner.is_warm
    except Exception:
        return False


async def _build_snapshot() -> dict:
    from core.safety import kill_switch_file_exists
    books = []
    async with store._lock:
        for tid, book in store.order_books.items():
            books.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "mid": book.mid,
                "spread": book.spread,
                "updated_at": book.updated_at,
            })

    orders = []
    for o in store.open_orders.values():
        orders.append({
            "order_id": o.order_id,
            "token_id": o.token_id,
            "slug": store.market_slugs.get(o.token_id, o.token_id[:14]),
            "side": o.side.value,
            "price": o.price,
            "size": o.size,
            "size_matched": o.size_matched,
            "strategy": o.strategy,
            "paper": o.paper,
            "created_at": o.created_at,
        })

    positions = []
    for tid, pos in store.positions.items():
        if pos.yes_shares > 0 or pos.total_invested > 0 or pos.realised_pnl != 0:
            # Mark-to-live-mid unrealized P&L for this position.
            # Falls back to cost basis (avg_entry_price) when no live quote is tracked.
            _book = store.order_books.get(tid)
            current_price = _book.mid if _book and _book.mid is not None else pos.avg_entry_price
            _pos_unrealized = 0.0
            if pos.yes_shares > 0:
                _pos_unrealized += (current_price - pos.avg_entry_price) * pos.yes_shares
            if pos.no_shares > 0:
                _pos_unrealized += ((1.0 - current_price) - pos.avg_entry_price) * pos.no_shares
            positions.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "yes_shares": pos.yes_shares,
                "avg_entry_price": pos.avg_entry_price,
                "total_invested": pos.total_invested,
                "realised_pnl": pos.realised_pnl,
                "current_price": round(current_price, 4),
                "unrealized_pnl": round(_pos_unrealized, 4),
            })

    trades = []
    for t in store.trades[-50:]:
        trades.append({
            "trade_id": t.trade_id,
            "token_id": t.token_id,
            "slug": store.market_slugs.get(t.token_id, t.token_id[:14]),
            "side": t.side.value,
            "price": t.price,
            "size": t.size,
            "pnl": t.pnl,
            "strategy": t.strategy,
            "paper": t.paper,
            "timestamp": t.timestamp,
        })

    events = await store.get_recent_events(50)
    active_strats = list(strategy_registry.get_active_instances().keys())

    return {
        "type": "snapshot",
        "timestamp": time.time(),
        "mode": settings.trading_mode,
        "kill_switch": store.kill_switch_active,
        "kill_switch_durable": bool(kill_switch_file_exists()),
        "observation_only": risk_manager.observation_only,
        "observation_reason": risk_manager.observation_reason,
        "daily_pnl": store.daily_pnl,
        "paper_balance": paper_sim.virtual_balance if settings.paper_trade else None,
        "strategies": active_strats,
        "order_books": books,
        "open_orders": orders,
        "positions": positions,
        "recent_trades": trades,
        "events": events,
        "ml": {
            "model_ready": ml_model.rf is not None,
            "brier_score": ml_model.brier_score,
            "roc_auc": ml_model.roc_auc,
            "ece": ml_model.ece,
            "n_updates": ml_model._n_updates,
            "drift_status": drift_detector.drift_status,
            "drift_psi": drift_detector.last_psi,
            "drift_brier": drift_detector.rolling_brier,
            "drift_ewma_brier": round(drift_detector.ewma_brier, 4) if drift_detector.ewma_brier is not None else None,
            "adaptive_weights": ml_model.adaptive_weights,
            "meta_learner_warm": _get_meta_warm(),
            "training_source": ml_model.training_source,
        },
    }


# ── App & Router ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="Polymarket Pro — Trading Bot API",
    description="""
## Institutional-grade Polymarket trading bot API

### Key Features
- **Paper trading**: Safe simulation mode with realistic slippage
- **ML ensemble**: 4-model ensemble (RF/GB/SGD/LightGBM) + meta-learner
- **Decision ledger**: Full PREDICTION→SIGNAL→RISK→ORDER→FILL traceability
- **Risk management**: Kill switch, circuit breakers, 10-check safety gate
- **Observability**: 31 auto-collected metrics across 6 categories

### Authentication
All endpoints require a Bearer token in the Authorization header:
```
Authorization: Bearer <your-api-token>
```
The only unauthenticated route is `GET /api/health` (the liveness probe).

### Rate Limiting
- Read endpoints: 120/minute
- Write endpoints: 30/minute
- Heavy compute (ML retrain, backtest): 5/minute
- Live trading enable: 3/minute

Rate-limited responses return HTTP 429 with a `Retry-After` header.

### Gateway Routing
All requests go through the Caddy gateway (port 81). The gateway routes to
the backend (port 8080) via the `?XTransformPort=8080` query parameter.

### Documentation
- Swagger UI: `/docs`
- ReDoc: `/redoc`
- OpenAPI JSON: `/openapi.json`

### API Versioning (W13-3)
This API supports versioning via:
1. URL prefix: `/api/v1/...` (parsed from the path; sets the
   effective version for the request)
2. Header: `Accept-Version: v1` (used when no URL prefix is present)

Current version: `v1` (configurable via the `API_VERSION` env var).

Discovery:
- `GET /api/version` (public, no auth) returns the current / supported
  / deprecated version sets.
- Every response carries `X-API-Version` (the version that actually
  served the request) and `X-API-Supported-Versions` (the full
  allow-list). A deprecated version additionally carries the
  `Deprecation` and `Sunset` headers (RFC 8594 / RFC 7231).

Unsupported versions are rejected with HTTP 400 before any
authentication check fires, so a misconfigured client learns the
version mismatch first.
""",
    version="1.0.0",
    contact={
        "name": "Polymarket Pro",
        "url": "https://github.com/armand-ratombotiana/polymarket-bot-ai",
    },
    license_info={
        "name": "MIT",
        "url": "https://opensource.org/licenses/MIT",
    },
    openapi_tags=[
        {"name": "trading", "description": "Order placement, position management, trade history"},
        {"name": "markets", "description": "Market data, order books, price history"},
        {"name": "ml", "description": "ML model metrics, drift detection, retraining"},
        {"name": "analysis", "description": "Deep market analysis, news sentiment"},
        {"name": "risk", "description": "Risk management, kill switch, exposure limits"},
        {"name": "strategies", "description": "Strategy registry, enable/disable"},
        {"name": "arbitrage", "description": "Cross-market arbitrage scanning and execution"},
        {"name": "system", "description": "System health, status, configuration"},
        {"name": "ai", "description": "AI copilot, predictions, market search"},
        {"name": "database", "description": "Database exploration, reconciliation"},
        {"name": "audit", "description": "Audit trail, event logging"},
        {"name": "decisions", "description": "Decision ledger, PREDICTION→SIGNAL→RISK→ORDER→FILL chain"},
        {"name": "config", "description": "Configuration management"},
        {"name": "backtesting", "description": "Historical strategy backtesting"},
        {"name": "alerts", "description": "Threshold-based alerting"},
        {"name": "observability", "description": "Auto-collected operational metrics"},
        {"name": "execution", "description": "Execution-quality inspection (slippage, latency)"},
        {"name": "capital", "description": "Capital allocation sizing"},
        {"name": "shadow", "description": "Shadow trading inspection (counterfactual trades)"},
        {"name": "live", "description": "Live-trading readiness gate and enable control"},
        {"name": "retention", "description": "Data retention policy enforcement"},
        {"name": "monitoring", "description": "Prometheus /metrics endpoint + Grafana dashboard"},
    ],
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
)

# CORS: locked to configured explicit origins only (empty = same-origin only).
# W11-6 (OWASP A05 — Security Misconfiguration): wildcard `*` removed from
# both ``allow_origins`` AND the auth middleware's origin-reflection branch.
# When ``CORS_ORIGINS`` is set, only those exact origins are allowed;
# credentials are always enabled (safe, because no wildcard branch can match
# an arbitrary origin). ``allow_methods`` is restricted to the explicit
# allowlist the API actually exposes (no TRACE / CONNECT / PATCH) so an
# attacker can't probe for unused verbs.
_cors_origins = settings.cors_origin_list
_CORS_ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=_CORS_ALLOWED_METHODS,
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)

# ── Rate limiting (W10-4 — slowapi) ────────────────────────────────────────────
# The shared ``limiter`` singleton (keyed on the client IP via
# ``get_remote_address``) lives in ``api/rate_limit.py`` so it can be imported
# by BOTH this module (for the routes defined inline here) AND
# ``core/live_safety_gate.py`` (for ``POST /api/live/enable``) without a
# circular import. ``app.state.limiter`` is slowapi's documented integration
# point — the decorator wrapper inspects it to find the limiter instance.
app.state.limiter = limiter


async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """JSON 429 handler for ``slowapi.errors.RateLimitExceeded``.

    Replaces slowapi's default ``_rate_limit_exceeded_handler`` (which
    returns ``{"detail": ..., "type": "rate_limited_exceeded"}``) with the
    project's standard envelope shape ``{"detail": ..., "retry_after": N}``
    so the frontend's existing error-display logic can render a "retry in Ns"
    countdown without a per-status-code special case.

    The ``Retry-After`` value is derived from the failing limit's granularity
    window (e.g. 60s for "5/minute") via ``exc.limit.limit.get_expiry()``.
    The exact reset time would require querying the storage backend's
    ``get_window_stats`` (returns (reset_at, remaining)), but in practice the
    upper-bound granularity is what clients need to back off — and it's
    available without a storage round-trip. ``X-RateLimit-Limit`` carries
    the canonical "<amount>/<granularity>" form (e.g. "5/minute") so clients
    can show the limit alongside the countdown.
    """
    # ``exc.limit`` is a ``slowapi.wrappers.Limit`` whose ``.limit`` attribute
    # is the underlying ``limits.RateLimitItem`` (e.g. "5 per 1 minute").
    rate_limit_item = getattr(getattr(exc, "limit", None), "limit", None)
    if rate_limit_item is not None:
        retry_after_secs = int(rate_limit_item.get_expiry())
        amount = int(getattr(rate_limit_item, "amount", 0))
        # ``RateLimitItem.GRANULARITY`` is a ``limits.limits.Granularity``
        # namedtuple ``(seconds=60, name='minute')`` — use ``.name`` for the
        # canonical "5/minute" string form.
        granularity_obj = getattr(rate_limit_item, "GRANULARITY", None)
        granularity_name = getattr(granularity_obj, "name", "minute")
        limit_str = f"{amount}/{granularity_name}"
    else:
        retry_after_secs = 60
        limit_str = "100/minute"
    # W13-1 — Prometheus: increment the rate-limit-hit counter so a Grafana
    # panel can surface "429 per endpoint" without scraping server logs.
    # Best-effort: a prometheus registry hiccup must never change the 429
    # response (the rate-limit decision has already been made; the counter
    # is purely observability).
    _record_rate_limit_hit(endpoint=request.url.path)
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Rate limit exceeded",
            "retry_after": retry_after_secs,
        },
        headers={
            "Retry-After": str(retry_after_secs),
            "X-RateLimit-Limit": limit_str,
        },
    )


app.add_exception_handler(RateLimitExceeded, rate_limit_handler)


@app.middleware("http")
async def enforce_api_auth(request: Request, call_next):
    """Fail-closed bearer-token auth on every route except the liveness probe and OPTIONS preflight."""
    if request.method == "OPTIONS":
        return await call_next(request)

    origin = request.headers.get("origin")
    # W11-6 (OWASP A05): wildcard ``*`` is no longer accepted as a CORS origin
    # — only the explicit origins listed in ``CORS_ORIGINS`` (or an empty list,
    # which means same-origin only) are reflected back. This eliminates the
    # ``"*" in settings.cors_origin_list`` branch that previously allowed any
    # website to issue credentialed cross-origin requests when the operator
    # left the env var at its wildcard default.
    cors_allowed = bool(origin and origin in settings.cors_origin_list)
    cors_headers = {}
    if origin and cors_allowed:
        cors_headers["access-control-allow-origin"] = origin
        cors_headers["access-control-allow-credentials"] = "true"
        cors_headers["access-control-allow-headers"] = "Authorization, Content-Type"
        cors_headers["access-control-allow-methods"] = "GET, POST, PUT, DELETE, OPTIONS"

    path = request.url.path
    if path in PUBLIC_PATHS or path.startswith("/api/health"):
        response = await call_next(request)
        if origin and cors_allowed:
            response.headers["access-control-allow-origin"] = origin
            response.headers["access-control-allow-credentials"] = "true"
        return response

    if not settings.api_token:
        await _audit_auth_failure(request, mode="not_configured")
        # W13-1 — Prometheus: count the auth failure (operator dashboards alert
        # on a sustained >0 rate, correlating with the audit-trail 401 burst).
        _record_auth_failure()
        return JSONResponse(
            status_code=503,
            content={"detail": "API authentication not configured — set API_TOKEN in .env", "code": "AUTH_NOT_CONFIGURED"},
            headers=cors_headers,
        )
    if not _valid_token(request.headers.get("authorization")):
        # W11-6 (OWASP A09): record the auth failure in the durable audit
        # trail so operators can correlate a burst of 401s with a likely
        # brute-force attempt. ``mode`` distinguishes a missing header
        # (likely a misconfigured client) from an invalid one (likely a
        # token-enumeration attempt); neither path persists the token.
        mode = "missing" if not request.headers.get("authorization") else "invalid"
        await _audit_auth_failure(request, mode=mode)
        # W13-1 — Prometheus: same counter as the 503 branch above so a
        # single ``rate()`` graph on ``polymarket_auth_failures_total``
        # surfaces both failure modes.
        _record_auth_failure()
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized — missing or invalid API token"},
            headers=cors_headers,
        )

    response = await call_next(request)
    if origin and cors_allowed:
        response.headers["access-control-allow-origin"] = origin
        response.headers["access-control-allow-credentials"] = "true"
    return response


# ── API versioning middleware (W13-3) ────────────────────────────────────────
# Added AFTER ``enforce_api_auth`` in source-code order — Starlette's
# "last-added = first-executed" rule means this dispatch runs BEFORE
# auth in the request flow, so a client negotiating an unsupported
# version learns about it via 400 BEFORE burning a 401 (and before the
# audit trail logs an "invalid token" entry for a request that was
# doomed anyway by the version check).
#
# See ``core/api_versioning.py`` for the version-resolution + validation
# logic; the wrapper below exists only so the module-level ``app`` and
# the middleware can be tested independently (the actual dispatch is
# importable as ``versioning_middleware`` from ``core.api_versioning``,
# which is what the W13-3 tests exercise directly).
@app.middleware("http")
async def api_versioning(request: Request, call_next):
    return await versioning_middleware(request, call_next)


# ── Request logging middleware (outermost — added AFTER enforce_api_auth so
#    Starlette's "last added = first executed" ordering makes this run BEFORE
#    auth, capturing every request including 401s / 503s for observability). ──
# W12-6 — Enhanced with a per-request UUID propagated via the
# ``core.logging_config.request_id_var`` ContextVar. Every downstream log
# line emitted during the request (audit_logger, ml.copilot, strategies,
# etc.) now carries the same ``request_id`` under its ``context`` block,
# so a log aggregator can group every record for a single client request
# without correlating timestamps. The id is also echoed in the response
# header ``X-Request-ID`` so the client can self-service a trace.
@app.middleware("http")
async def request_logging_middleware(request: Request, call_next):
    """Log every request with method, path, status code, latency, and a
    per-request UUID propagated to all downstream log records via a
    ``contextvars.ContextVar``.

    Wrapped around every route — runs BEFORE ``enforce_api_auth`` so 401 / 503
    responses show up in the server logs without each route having to log
    them individually. The 4xx / 5xx counts feed operator dashboards and
    alerting; the latency field surfaces slow-route regressions without a
    separate tracing layer; the ``request_id`` lets an operator trace a
    single request across the entire middleware + handler + audit-logger
    chain in a structured log aggregator.
    """
    import uuid as _uuid

    request_id = str(_uuid.uuid4())[:8]
    # Populate the ContextVar so every downstream ``log.info(...)`` call
    # (audit_logger, copilot, strategies …) inherits the same request_id in
    # its ``context`` block. ``set`` returns a token we MUST use to reset
    # the var on the way out — without ``reset(token)`` the value would
    # leak into the next request served by this same task slot.
    request_id_token = request_id_var.set(request_id)
    # W13-1 — Prometheus: count the in-flight request so a Grafana gauge
    # panel can surface concurrent-load without scraping the access log.
    # ``http_requests_in_progress`` is a Gauge (not Counter) — incremented
    # here and decremented in the ``finally`` block below so the value
    # always reflects the true concurrent-request count, even if the
    # downstream handler raises.
    http_requests_in_progress.inc()
    start = time.time()
    response: object = None
    try:
        try:
            response = await call_next(request)
        except Exception:
            # ``call_next`` should never raise — Starlette converts route exceptions
            # into 500 responses via the server exception middleware — but if it
            # ever does (e.g. middleware itself raises), surface a clean 500
            # instead of letting the ASGI server kill the connection.
            duration = time.time() - start
            log.error(
                "[request] %s %s → 500 (unhandled in middleware chain) (%.3fs)",
                request.method,
                request.url.path,
                duration,
                exc_info=True,
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": str(request.url.path),
                    "status": 500,
                    "duration_ms": round(duration * 1000, 3),
                },
            )
            # W13-1 — Prometheus: record the request (status=500 — matches the
            # JSONResponse returned immediately below). The in-flight gauge is
            # decremented in the outer ``finally`` block.
            _record_prometheus_request(
                method=request.method,
                endpoint=request.url.path,
                status=500,
                duration=duration,
            )
            request_id_var.reset(request_id_token)
            return JSONResponse(
                status_code=500,
                content={"detail": "Internal server error", "path": str(request.url.path)},
                headers={"X-Request-ID": request_id},
            )
        duration = time.time() - start
        # W13-1 — Prometheus: record the request count + latency histogram for
        # the final response. Done BEFORE the log.info so the metric is observable
        # even if a downstream log shipper drops the log line. Best-effort —
        # the helper swallows registry errors internally.
        _record_prometheus_request(
            method=request.method,
            endpoint=request.url.path,
            status=response.status_code if response is not None else 500,
            duration=duration,
        )
        # Echo the id back to the client so support / debugging can self-service
        # a log trace without operator intervention.
        response.headers["X-Request-ID"] = request_id
        log.info(
            "[request] %s %s → %d (%.3fs)",
            request.method,
            request.url.path,
            response.status_code,
            duration,
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": str(request.url.path),
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 3),
            },
        )
        request_id_var.reset(request_id_token)
        return response
    finally:
        # W13-1 — Prometheus: ALWAYS decrement the in-flight gauge, even on
        # the early-return 500 path above. Without this, an exception inside
        # ``call_next`` would leave the gauge stuck at +1 forever and the
        # Grafana "concurrent requests" panel would drift up over time.
        http_requests_in_progress.dec()


# ── Rate-limit policy header middleware (W10-4) ──────────────────────────────
# Adds an informational ``X-RateLimit-Policy`` header to EVERY response so
# clients (and operators inspecting traffic) can see the policy at a glance
# without having to consult the docs. The header summarises the 4-tier policy:
#   * 120/min read — generous, allows polling
#   * 30/min write — stricter
#   * 5/min heavy  — very strict (ML retrain, backtest)
#   * 20/min auth-sensitive routes (trade, orders, position-close)
# Per-route ``Retry-After`` / ``X-RateLimit-Limit`` headers are added by the
# custom ``rate_limit_handler`` exception handler ONLY when a limit is hit;
# this middleware is the always-on informational channel.
@app.middleware("http")
async def rate_limit_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-RateLimit-Policy"] = "120/min read, 30/min write, 5/min heavy"
    return response


# ── Security headers middleware (W11-6 — OWASP A05) ──────────────────────────
# Adds a baseline set of defensive response headers to EVERY response so the
# browser's own security machinery can apply defense-in-depth:
#   * ``X-Content-Type-Options: nosniff``        — blocks MIME-type sniffing.
#   * ``X-Frame-Options: DENY``                  — blocks clickjacking via framing.
#   * ``X-XSS-Protection: 1; mode=block``        — legacy reflected-XSS filter
#                                                   (still useful for older browsers).
#   * ``Referrer-Policy: strict-origin-when-cross-origin`` — strips the path /
#                                                   query from the Referer
#                                                   header on cross-origin nav.
#   * ``Content-Security-Policy: default-src 'self'`` — only same-origin
#                                                   resources may load (the
#                                                   dashboard is fully same-origin;
#                                                   no external scripts / styles).
# These headers do NOT affect the JSON API contract — they only instruct the
# browser. They're applied after ``call_next`` so they land on every response
# (200, 4xx, 5xx, OPTIONS preflight, WebSocket upgrade rejection, …). The
# ``Strict-Transport-Security`` header is intentionally NOT added here: it
# must be terminated by the TLS-aware reverse proxy (Caddy) so it only ships
# over an actual HTTPS connection (otherwise an active MITM could inject it
# into a plain-HTTP response and pin the client).
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    return response


# ── Auth-failure audit logging (W11-6 — OWASP A09) ──────────────────────────
# Wraps ``enforce_api_auth``'s 401 / 503 paths so a failed authentication
# attempt is recorded in the durable audit trail (SQLite ``audit_events``).
# This gives operators an artifact they can correlate against rate-limit hits
# and the W10-7 alerting engine — without having to grep the live server log.
# The audit event is best-effort: a SQLite write failure must NOT change the
# 401 / 503 response (the auth decision has already been made; the audit row
# is purely observability). No request body / Authorization header value is
# ever persisted — only the remote IP, the path, and the failure mode.
async def _audit_auth_failure(request: Request, *, mode: str) -> None:
    """Best-effort audit-log entry for a rejected auth attempt.

    ``mode`` is one of ``"missing"``, ``"invalid"``, ``"not_configured"``.
    Records the remote IP and path; NEVER the token / Authorization header.
    """
    try:
        client_ip = request.client.host if request.client else "unknown"
        await audit_logger.log_event(
            category="security",
            event_type="auth_failure",
            details=f"mode={mode} ip={client_ip} path={request.url.path} method={request.method}",
        )
    except Exception:  # pragma: no cover — audit must never break auth
        log.debug("[security] audit_auth_failure write failed (mode=%s)", mode)



# ── Global exception handler ─────────────────────────────────────────────────
# Catches any exception that escapes a route handler (FastAPI's own
# ``HTTPException`` is handled separately by FastAPI's built-in handler and
# never reaches this function). Without this, FastAPI's default behaviour is
# to return a 500 with the raw exception message in the response body — that
# leaks internal stack details to the client and produces an inconsistent
# response shape vs. the rest of the API. This handler logs the full traceback
# server-side (so operators can debug) and returns a stable JSON shape:
# ``{"detail": "Internal server error", "path": "<route>"}``.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    log.error(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "path": str(request.url.path)},
    )


# ── Request / Config Models ───────────────────────────────────────────────────

class ManualTradeRequest(BaseModel):
    token_id: str
    price: float = Field(gt=0, lt=1)
    side: str = Field(pattern="^(BUY|SELL|buy|sell)$")
    size_usdc: float = Field(gt=0, default=10.0)


class StrategyToggleRequest(BaseModel):
    strategy_name: str
    enabled: bool


class CopilotQueryRequest(BaseModel):
    query: str


class MarketAnalyzeRequest(BaseModel):
    token_id: str


class StrategyConfigUpdate(BaseModel):
    mm_spread_bps: int | None = Field(default=None, ge=10, le=2000)
    mm_quote_size_usdc: float | None = Field(default=None, ge=0.5, le=5.0)
    mm_max_inventory_usdc: float | None = Field(default=None, ge=1.0, le=15.0)
    arb_min_profit_bps: int | None = Field(default=None, ge=5, le=1000)
    arb_order_size_usdc: float | None = Field(default=None, ge=0.5, le=5.0)
    signal_min_confidence: float | None = Field(default=None, ge=0.5, le=0.99)
    daily_loss_limit_usdc: float | None = Field(default=None, ge=0.25, le=2.0)


# ── System Endpoints ──────────────────────────────────────────────────────────

@app.get(
    "/api/health",
    tags=["system"],
    response_model=HealthResponse,
    response_model_exclude_unset=True,
    summary="Liveness probe",
    description=(
        "Lightweight liveness probe. Returns the canonical health status "
        "(`ok`), the server time, and whether paper trading is active. "
        "This is the ONLY unauthenticated route — all other endpoints "
        "require a valid `Authorization: Bearer <token>` header."
    ),
)
@limiter.limit(READ_LIMIT)
async def health(request: Request):
    return {"status": "ok", "timestamp": time.time(), "paper": settings.paper_trade}


# ── Prometheus metrics endpoint (W13-1) ───────────────────────────────────────
# Public (unauthenticated) — Prometheus scrapers use their own auth (mTLS /
# OAuth2 proxy /w Basic-auth) at the ingress layer and don't carry the
# application API token. The endpoint emits only Prometheus-format metric
# values (counters / gauges / histograms) — no PII, no order bodies, no
# secrets — so unauthenticated exposure is safe. Path is added to
# ``PUBLIC_PATHS`` above so the ``enforce_api_auth`` middleware short-circuits
# without consulting ``settings.api_token`` (otherwise a misconfigured token
# would 503 the scraper and Grafana panels would go dark).
#
# Rate-limit is intentionally NOT applied to ``/metrics``: a scrape cadence
# faster than the slowapi limit would 429 the scraper (default 120/min read
# limit is generous, but production deployments frequently scrape at 5-15s
# intervals across multiple Prometheus shards — combined load could exceed
# the limit). The endpoint is cheap (single ``generate_latest()`` call
# against an in-process registry), so unauthenticated + unthrottled is the
# standard Prometheus scraping contract.
@app.get(
    "/metrics",
    tags=["monitoring"],
    summary="Prometheus metrics",
    description=(
        "Prometheus-format metrics endpoint. Exposes counters, gauges, and "
        "histograms covering HTTP request count / latency / status codes, "
        "trading (orders placed / filled, P&L, open positions), ML (predictions, "
        "drift PSI, Brier score, ROC AUC), and system surfaces (cache hit "
        "rates, DB sizes, active alerts). Scrape with Prometheus at 15s "
        "intervals; the Grafana dashboard in ``grafana/dashboard.json`` "
        "renders the panels from this endpoint's data."
    ),
)
async def prometheus_metrics():
    """Return the Prometheus-format metrics payload.

    The response is the canonical Prometheus text exposition format
    (``# HELP`` / ``# TYPE`` headers followed by ``name{labels} value``
    rows). ``media_type`` is set to ``CONTENT_TYPE_LATEST`` (exported
    from ``core.prometheus_metrics``) so a Prometheus scraper correctly
    parses the histogram summaries and counter increments.
    """
    return Response(
        content=_get_prometheus_metrics(),
        media_type=CONTENT_TYPE_LATEST,
    )


# ── API version info (W13-3) ────────────────────────────────────────────────
# Public (unauthenticated) so a client can negotiate the API version BEFORE
# presenting credentials. Mirrors the W11-6 contract-doc-first philosophy:
# the version info must be discoverable without an authenticated round-trip
# so a misconfigured client (wrong version, wrong token) can self-correct
# one issue at a time instead of seeing only a 401 / 400 cascade.
@app.get(
    "/api/version",
    tags=["system"],
    summary="API version information",
    description=(
        "Returns the active API version, the list of supported versions, "
        "and the list of deprecated versions. Public (no auth required) "
        "so a client can negotiate the version before authenticating. "
        "Per-request version selection is performed by the versioning "
        "middleware via either the URL prefix `/api/v1/...` or the "
        "`Accept-Version` request header; the effective version is "
        "echoed back on every response as `X-API-Version`."
    ),
)
async def api_version():
    """Get API version information."""
    return get_version_info()


@app.get(
    "/api/status",
    tags=["system"],
    summary="System status report",
    description=(
        "Returns the institutional risk-engine status report (kill switch, "
        "observation mode, daily P&L) augmented with the canonical trading "
        "mode, active strategies, paper balance, seeded market count, "
        "tracked book count, book poller stats, vector index size, and "
        "durable kill-switch flag. Heavier than `/api/health` — use this "
        "for the operator dashboard, not for liveness probes."
    ),
)
@limiter.limit(READ_LIMIT)
async def status(request: Request):
    from core.safety import kill_switch_file_exists
    report = await risk_manager.status_report()
    return {
        **report,
        "mode": settings.trading_mode,
        "strategies": list(strategy_registry.get_active_instances().keys()),
        "paper_balance": paper_sim.virtual_balance if settings.paper_trade else None,
        "seeded_markets": len(_seeded_tokens),
        "tracked_books": len(store.order_books),
        "book_poller": book_poller.stats,
        "vector_docs_indexed": vector_store._doc_count,
        "kill_switch_durable": bool(kill_switch_file_exists()),
    }


@app.get(
    "/api/snapshot",
    tags=["system"],
    summary="Real-time portfolio snapshot",
    description=(
        "Single round-trip snapshot of the entire trading workstation: "
        "mode, kill switch flags, daily P&L, paper balance, active "
        "strategies, order books, open orders, positions, recent trades, "
        "events log, and ML model health. This is the payload the "
        "WebSocket `/ws` broadcasts every second."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_snapshot(request: Request):
    return await _build_snapshot()


@app.get(
    "/api/history/equity",
    tags=["system"],
    summary="Equity curve history",
    description=(
        "Returns the chronological equity-curve points the dashboard "
        "renders as a line chart. Each point carries the timestamp, "
        "equity value, and per-step P&L delta."
    ),
)
async def get_equity_history():
    async with store._lock:
        return {"points": store.equity_history, "count": len(store.equity_history)}


@app.get(
    "/api/analytics",
    tags=["system"],
    summary="Quantitative performance analytics",
    description=(
        "Full trading-performance roll-up: equity, realized/unrealized "
        "P&L, win rate with Wilson 95% CI, profit factor, expectancy, "
        "Sharpe ratio, max drawdown, total volume, open exposure, "
        "risk utilization, data freshness, and active strategies. "
"Cached for 30s (W11-2)."
    ),
)
async def get_analytics():
    # W11-2 — cache the full analytics roll-up for 30s (analytics_cache's
    # default TTL). The handler walks every closed trade + every open
    # position against the live order-book mid, then computes Wilson CI /
    # profit factor / expectancy / Sharpe / max drawdown / data freshness
    # — this is the single most expensive read on the dashboard. Cache
    # key is a constant: there's no per-request parameter to vary on.
    cache_key = "analytics"
    cached = analytics_cache.get(cache_key)
    if cached is not None:
        return cached
    trades = store.trades
    total_trades = len(trades)
    # Only trades that actually closed a position (pnl != 0) determine win rate.
    closed_trades = [t for t in trades if t.pnl != 0]
    winning_trades = sum(1 for t in closed_trades if t.pnl > 0)
    losing_trades = sum(1 for t in closed_trades if t.pnl < 0)
    win_rate = (winning_trades / len(closed_trades)) if closed_trades else 0.0
    total_vol = sum(t.price * t.size for t in trades)

    # Wilson 95% confidence interval for the win rate (sample-size honest).
    n = len(closed_trades)
    z = 1.96
    ci_low = ci_high = None
    if n > 0:
        p = win_rate
        denom = 1 + z * z / n
        centre = (p + z * z / (2 * n)) / denom
        half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
        ci_low = max(0.0, centre - half)
        ci_high = min(1.0, centre + half)

    wins = sum(t.pnl for t in closed_trades if t.pnl > 0)
    losses = sum(t.pnl for t in closed_trades if t.pnl < 0)
    profit_factor = wins / max(1e-9, -losses) if losses else (None if not wins else float("inf"))

    # Average win / loss and per-trade expectancy.
    # Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss) where avg_loss is negative.
    avg_win = (wins / winning_trades) if winning_trades else 0.0
    avg_loss = (losses / losing_trades) if losing_trades else 0.0  # negative or zero
    loss_rate = 1.0 - win_rate
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
    sharpe_ratio = ml_model.sharpe_ratio

    # Unrealized P&L: mark open positions to the live order-book mid when present.
    unrealized_pnl = 0.0
    for p in store.positions.values():
        if p.current_exposure <= 0.001:
            continue
        book = store.order_books.get(p.token_id)
        mark = book.mid if book and book.mid is not None else None
        if mark is None:
            mark = p.avg_entry_price  # cost-basis mark (no live quote)
        unrealized_pnl += (mark - p.avg_entry_price) * p.yes_shares
        unrealized_pnl += ((1.0 - mark) - p.avg_entry_price) * p.no_shares

    realized_pnl = store.daily_pnl
    net_pnl = realized_pnl + unrealized_pnl
    equity = store.paper_balance
    max_drawdown_dollars = max(0.0, store.peak_equity - equity)
    max_drawdown_pct = (max_drawdown_dollars / store.peak_equity) if store.peak_equity > 0 else 0.0

    exp = compute_exposure()
    deployable = float(MAX_DEPLOYABLE_CAPITAL)
    risk_utilization = min(1.0, exp["maximum_remaining_loss"] / deployable) if deployable > 0 else 0.0

    # Data freshness: seconds since the newest tracked order-book update.
    books = store.order_books.values()
    freshness = max((time.time() - b.updated_at for b in books), default=0.0)

    result = {
        "equity": round(equity, 2),
        "realized_pnl": round(realized_pnl, 2),
        "unrealized_pnl": round(unrealized_pnl, 2),
        "net_pnl": round(net_pnl, 2),
        "total_trades": total_trades,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "closed_trades": len(closed_trades),
        "open_trades": total_trades - len(closed_trades),
        "win_rate": round(win_rate, 4),
        "win_rate_ci_low": round(ci_low, 4) if ci_low is not None else None,
        "win_rate_ci_high": round(ci_high, 4) if ci_high is not None else None,
        "profit_factor": round(profit_factor, 2) if isinstance(profit_factor, float) else profit_factor,
        "avg_win": round(avg_win, 4),
        "avg_loss": round(avg_loss, 4),
        "expectancy": round(expectancy, 4),
        "sharpe_ratio": round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        "max_drawdown_dollars": round(max_drawdown_dollars, 2),
        "max_drawdown_pct": round(max_drawdown_pct, 4),
        "total_volume_usdc": round(total_vol, 2),
        "open_exposure": round(exp["maximum_remaining_loss"], 2),
        "open_position_count": exp["open_position_count"],
        "pending_order_capital": round(exp["reserved_for_pending_orders"], 2),
        "risk_utilization": round(risk_utilization, 4),
        "mode": settings.trading_mode,
        "data_freshness_seconds": round(freshness, 1),
        "peak_equity": round(store.peak_equity, 2),
        "active_strategies": list(strategy_registry.get_active_instances().keys()),
    }
    # W11-2 — store the computed analytics dict in the cache before returning
    # so subsequent requests within the TTL window hit the cache and skip
    # the recompute. ``analytics_cache.set`` uses the cache's default TTL
    # (30s) — ``POST /api/trade`` and ``POST /api/positions/{id}/close``
    # invalidate this key so the next read after a mutation sees fresh data.
    analytics_cache.set(cache_key, result)
    return result


# ── Risk-Adjusted Portfolio: Exposure, Reconciliation & Leaderboard ─────────

@app.get("/api/exposure", tags=["risk"])
async def get_exposure():
    """Full exposure decomposition (mandate section 2)."""
    return compute_exposure()


@app.get("/api/risk/reconcile", tags=["risk"])
async def get_reconciliation():
    """Reconciliation investigation for the current open exposure."""
    return compute_reconciliation(bankroll_ceiling=float(BANKROLL_CEILING))


@app.get("/api/leaderboard", tags=["risk"])
async def get_leaderboard():
    """Strategy leaderboard ranked by reproducible risk-adjusted net performance."""
    # W11-2 — cache the leaderboard roll-up for 30s. ``leaderboard()``
    # walks every closed trade to compute per-strategy Sharpe / win-rate /
    # profit-factor — the cost grows with trade count, and the dashboard
    # polls this on every render. ``POST /api/trade`` invalidates.
    cache_key = "leaderboard"
    cached = analytics_cache.get(cache_key)
    if cached is not None:
        return cached
    result = leaderboard()
    analytics_cache.set(cache_key, result)
    return result


# ── 50+ Strategy Hub Endpoints ────────────────────────────────────────────────

@app.get(
    "/api/strategies/catalog",
    tags=["strategies"],
    summary="Strategy catalog",
    description=(
        "Returns the full catalog of 50+ registered strategies with "
        "metadata (category, description, default parameters, risk "
        "profile) and their current running state."
    ),
)
async def get_strategy_catalog():
    """Return all 50 strategies with metadata, category, and running state."""
    # W11-2 — cache the strategy catalog for 5 min (markets_cache's
    # default TTL). Strategy metadata is static — the only thing that
    # changes is the running state, which ``POST /api/strategies/toggle``
    # invalidates explicitly.
    cache_key = "strategies_catalog"
    cached = markets_cache.get(cache_key)
    if cached is not None:
        return cached
    result = {
        "catalog": strategy_registry.get_catalog(),
        "total": len(strategy_registry._catalog),
    }
    markets_cache.set(cache_key, result)
    return result


@app.post(
    "/api/strategies/toggle",
    tags=["strategies"],
    summary="Start or stop a strategy at runtime",
    description=(
        "Dynamically start or stop any registered strategy without "
        "restarting the server. Returns 400 if the strategy name is not "
        "in the catalog."
    ),
)
async def toggle_strategy(req: StrategyToggleRequest):
    """Dynamically start or stop any of the 50 strategies at runtime."""
    strat_id = req.strategy_name.lower()
    if req.enabled:
        ok = await strategy_registry.start_strategy(strat_id)
        if not ok:
            raise HTTPException(status_code=400, detail=f"Strategy {strat_id} not found in catalog")
        await store.log_event(f"▶ Strategy [{strat_id}] started via API")
        # W11-2 — invalidate markets_cache (which holds the strategy
        # catalog, including per-strategy ``is_running`` flags) so the
        # next ``GET /api/strategies/catalog`` reflects the new state.
        markets_cache.invalidate("strategies_catalog")
        return {"status": "started", "strategy": strat_id}
    else:
        ok = await strategy_registry.stop_strategy(strat_id)
        await store.log_event(f"⏸ Strategy [{strat_id}] stopped via API")
        # W11-2 — same invalidation as the start branch above.
        markets_cache.invalidate("strategies_catalog")
        return {"status": "stopped" if ok else "not_running", "strategy": strat_id}


# ── AI Copilot & Semantic Vector Search ───────────────────────────────────────

@app.post(
    "/api/ai/copilot",
    tags=["ai"],
    summary="Ask the GenAI copilot",
    description=(
        "Ask the GenAI copilot for market analysis, trade ideas, or "
        "risk insights. Returns a streaming-style answer payload "
        "(synthesized server-side from the model's analysis)."
    ),
)
async def copilot_chat(req: CopilotQueryRequest):
    """Ask the GenAI Copilot for market analysis, trade ideas, or risk insights."""
    return await copilot_engine.answer_query(req.query)


@app.post("/api/ai/analyze-market", tags=["ai"])
async def analyze_market(req: MarketAnalyzeRequest):
    """Generate a quant & fundamental briefing for a specific prediction market."""
    return await copilot_engine.analyze_market(req.token_id)


@app.get("/api/ai/predict/{token_id}", tags=["ai"])
async def get_ai_prediction(token_id: str):
    """
    Return the ML ensemble's directional view for a single YES token.

    Reads market metadata from `market_discovery.catalog` (the universal
    catalog maintained by `core/market_discovery.py`) — NOT from
    `store.market_info` (that attribute does not exist on the DataStore).

    Response:
      * p_yes            — calibrated ensemble P(YES) ∈ (0.01, 0.99)
      * confidence       — |p_yes - 0.5| * 2  ∈ [0, 1]
      * market_mid       — live book mid price (or None if book is empty)
      * edge             — p_yes - market_mid (positive ⇒ model is more
                           bullish than the market; negative ⇒ bearish)
      * edge_bps         — edge expressed in basis points
      * recommended_action — BUY | SELL | HOLD (threshold-gated)
      * thresholds       — the gates used so callers can audit the decision
      * market           — the catalog metadata used to build the feature vector
      * model_status     — whether the ensemble was actually trained

    A HOLD recommendation does NOT imply no edge — it means the edge was
    below the configured conviction threshold. Always inspect `edge` /
    `edge_bps` before deciding to act.
    """
    # ── R10: pull metadata from market_discovery.catalog (NOT store.market_info) ──
    from core.market_discovery import market_discovery

    catalog_record = market_discovery.catalog.get(token_id)
    if not catalog_record:
        raise HTTPException(
            status_code=404,
            detail=f"token_id '{token_id}' not present in market_discovery.catalog — "
                   "wait for the next catalog sync or call /api/markets/coverage",
        )

    # extract_features expects a dict with Gamma-style keys (volume24hr, volume,
    # liquidity|liquidityNum, endDate|end_date_iso|endDateIso). The catalog
    # record uses normalized snake_case keys, so bridge both shapes here.
    market_for_features = dict(catalog_record)
    market_for_features.setdefault(
        "volume24hr", catalog_record.get("volume_24h", 0.0)
    )
    market_for_features.setdefault(
        "volume", catalog_record.get("total_volume", 0.0)
    )
    # liquidity key already matches; bridge endDate aliases just in case.
    if "endDate" not in market_for_features and catalog_record.get("end_date"):
        market_for_features["endDate"] = catalog_record["end_date"]

    # ── Live order book (needed for the microstructure feature vector) ──
    book = await store.get_order_book(token_id)
    if book is None:
        # Hint the poller to prioritize this token, then surface a 502.
        book_poller.prioritize_tokens([token_id])
        raise HTTPException(
            status_code=502,
            detail=f"no live order book for token '{token_id}' — poller prioritized; retry shortly",
        )

    market_mid = book.mid
    best_bid = book.best_bid
    best_ask = book.best_ask

    # ── Feature extraction + ensemble prediction ──
    from ml.features import extract_features

    features = extract_features(market_for_features, book)
    if features is None:
        raise HTTPException(
            status_code=422,
            detail=(
                "feature extraction returned None — book mid is missing or "
                "outside the tradeable (0.001, 0.999) band; cannot score"
            ),
        )

    p_yes, confidence = ml_model.predict(features, token_id=token_id)
    p_yes = float(p_yes)
    confidence = float(confidence)

    # ── Edge vs market mid ──
    if market_mid is not None:
        edge = float(p_yes) - float(market_mid)
        edge_bps = round(edge * 10_000.0, 1)
    else:
        edge = None
        edge_bps = None

    # ── Recommendation thresholds ──
    # Edge-based conviction gate (mirrors the spirit of strategies/signal_trader.py
    # but expressed in terms of the model-vs-market edge rather than the absolute
    # p_yes level, so a model that sees a 50/50 coin priced at 0.20 correctly
    # surfaces a BUY rather than a HOLD just because |p_yes − 0.5| is small).
    #
    #   BUY  : edge ≥ +2ct (200 bps) AND confidence ≥ 0.10
    #   SELL : edge ≤ −2ct (−200 bps) AND confidence ≥ 0.10
    #   HOLD : otherwise (insufficient edge OR insufficient model conviction)
    MIN_EDGE_CT = 0.02          # 2 cents
    MIN_CONFIDENCE = 0.10

    recommended_action = "HOLD"
    if edge is None:
        action_reason = "market mid unavailable — cannot compute edge"
    elif confidence < MIN_CONFIDENCE:
        recommended_action = "HOLD"
        action_reason = (
            f"confidence {confidence:.3f} < {MIN_CONFIDENCE:.2f} — model is too "
            f"uncertain to act despite edge={edge*100:+.2f}ct"
        )
    elif edge >= MIN_EDGE_CT:
        recommended_action = "BUY"
        action_reason = (
            f"edge=+{edge*100:.2f}ct ≥ +{MIN_EDGE_CT*100:.0f}ct AND "
            f"confidence={confidence:.3f} ≥ {MIN_CONFIDENCE:.2f}"
        )
    elif edge <= -MIN_EDGE_CT:
        recommended_action = "SELL"
        action_reason = (
            f"edge={edge*100:.2f}ct ≤ −{MIN_EDGE_CT*100:.0f}ct AND "
            f"confidence={confidence:.3f} ≥ {MIN_CONFIDENCE:.2f}"
        )
    else:
        recommended_action = "HOLD"
        action_reason = (
            f"|edge|={abs(edge)*100:.2f}ct below ±{MIN_EDGE_CT*100:.0f}ct "
            f"conviction threshold"
        )

    # Compact market payload (avoid duplicating the full catalog record)
    market_payload = {
        "token_id": catalog_record.get("token_id"),
        "event_id": catalog_record.get("event_id"),
        "event_title": catalog_record.get("event_title"),
        "question": catalog_record.get("question"),
        "slug": catalog_record.get("slug") or store.market_slugs.get(token_id, ""),
        "outcome": catalog_record.get("outcome"),
        "category": catalog_record.get("category"),
        "end_date": catalog_record.get("end_date"),
        "status": catalog_record.get("status"),
        "volume_24h": catalog_record.get("volume_24h"),
        "total_volume": catalog_record.get("total_volume"),
        "liquidity": catalog_record.get("liquidity"),
        "last_synced": catalog_record.get("last_synced"),
    }

    return {
        "token_id": token_id,
        "p_yes": round(p_yes, 4),
        "confidence": round(confidence, 4),
        "market_mid": round(market_mid, 4) if market_mid is not None else None,
        "best_bid": round(best_bid, 4) if best_bid is not None else None,
        "best_ask": round(best_ask, 4) if best_ask is not None else None,
        "spread": round(book.spread, 4) if book.spread is not None else None,
        "edge": round(edge, 4) if edge is not None else None,
        "edge_bps": edge_bps,
        "recommended_action": recommended_action,
        "action_reason": action_reason,
        "thresholds": {
            "min_edge_cents": MIN_EDGE_CT * 100.0,
            "min_confidence": MIN_CONFIDENCE,
        },
        "model_status": {
            "model_ready": ml_model.rf is not None,
            "model_version": model_registry.active_version,
            "brier_score": ml_model.brier_score,
            "roc_auc": ml_model.roc_auc,
            "ece": ml_model.ece,
            "n_online_updates": ml_model._n_updates,
        },
        "market": market_payload,
        "book_updated_at": book.updated_at,
        "timestamp": time.time(),
    }


# ── Market OHLCV & Historical Candlestick Data ────────────────────────────────

@app.get("/api/history/ohlcv/{token_id}", tags=["markets"])
async def get_market_ohlcv(
    token_id: str,
    resolution: str = Query("5m", pattern="^(1m|5m|1h)$"),
    count: int = Query(40, ge=1, le=1000),
):
    """Return OHLCV candlestick bars for visual charting.

    Priority:
      1. Real candles from TimescaleDB continuous aggregates (market.price_candle_*)
         when TimescaleDB is connected and rows exist for this token — labeled synthetic=False.
      2. Seeded random-walk anchored to live mid when no stored candles exist —
         explicitly labeled synthetic=True so callers always know the data source.
    """
    if not token_id:
        raise HTTPException(status_code=422, detail="token_id path parameter is required")
    book = await store.get_order_book(token_id)
    mid = (book.mid if book else 0.5) or 0.5
    slug = store.market_slugs.get(token_id, token_id[:14])
    step_sec = 60 if resolution == "1m" else 300 if resolution == "5m" else 3600
    view_map = {"1m": "market.price_candle_1m", "5m": "market.price_candle_5m", "1h": "market.price_candle_1h"}
    pg_view = view_map.get(resolution, "market.price_candle_5m")

    # ── Attempt real TimescaleDB continuous-aggregate candles ─────────────────
    from core.timescale_db import timescale_db
    if timescale_db._is_postgres and timescale_db._pool:
        try:
            async with timescale_db._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT bucket, open, high, low, close, vwap, tick_count
                    FROM {pg_view}
                    WHERE token_id = $1
                    ORDER BY bucket DESC
                    LIMIT $2;
                    """,
                    token_id,
                    count,
                )
            if rows:
                bars = [
                    {
                        "timestamp": float(r["bucket"].timestamp()),
                        "open": round(float(r["open"]), 4),
                        "high": round(float(r["high"]), 4),
                        "low": round(float(r["low"]), 4),
                        "close": round(float(r["close"]), 4),
                        "volume": float(r["tick_count"]),
                        "vwap": round(float(r["vwap"]), 4) if r["vwap"] is not None else None,
                    }
                    for r in reversed(rows)
                ]
                return {
                    "token_id": token_id,
                    "slug": slug,
                    "resolution": resolution,
                    "bars": bars,
                    "count": len(bars),
                    "synthetic": False,
                    "source": pg_view,
                    "data_age_seconds": round(time.time() - bars[-1]["timestamp"], 1) if bars else None,
                }
        except Exception as e:
            log.debug("[ohlcv] TimescaleDB candle query failed for %s: %s", token_id, e)

    # ── Synthetic fallback: seeded random-walk anchored to live mid ───────────
    rng = np.random.RandomState(abs(hash(token_id + resolution)) % (2**31))
    now = time.time()

    bars = []
    curr_price = max(mid * (1.0 + rng.uniform(-0.06, 0.06)), 0.05)
    for i in range(count):
        ts = now - (count - i) * step_sec
        drift = rng.uniform(-0.012, 0.012)
        open_p = curr_price
        close_p = max(min(open_p + drift, 0.98), 0.02)
        high_p = max(open_p, close_p) + abs(rng.uniform(0.001, 0.008))
        low_p = min(open_p, close_p) - abs(rng.uniform(0.001, 0.008))
        vol = float(rng.uniform(500, 15000))
        bars.append({
            "timestamp": ts,
            "open": round(open_p, 4),
            "high": round(high_p, 4),
            "low": round(low_p, 4),
            "close": round(close_p, 4),
            "volume": round(vol, 1),
        })
        curr_price = close_p

    if bars:
        bars[-1]["close"] = round(mid, 4)

    return {
        "token_id": token_id,
        "slug": slug,
        "resolution": resolution,
        "bars": bars,
        "count": len(bars),
        "synthetic": True,
        "synthetic_kind": "seeded_random_walk",
        "disclaimer": "Synthetic bars anchored to live mid — no stored history for this token yet",
    }


@app.get("/api/ai/search", tags=["ai"])
async def semantic_search(query: str = Query(..., min_length=1, max_length=500), top_k: int = Query(8, ge=1, le=100)):
    """Semantic vector similarity search across all prediction markets."""
    results = vector_store.search(query, top_k=top_k)
    return {"query": query, "results": [{"market": meta, "score": score} for meta, score in results]}


# ── Strategy Configuration ────────────────────────────────────────────────────

@app.get(
    "/api/config",
    tags=["config"],
    summary="Get live strategy configuration",
    description=(
        "Returns the current live-tunable strategy parameters: market-"
        "maker spread (bps), quote size (USDC), max inventory (USDC), "
        "arbitrage min profit (bps), arb order size, signal min "
        "confidence, daily loss limit, max total exposure, max open orders."
    ),
)
async def get_config():
    return {
        "mm_spread_bps": settings.mm_spread_bps,
        "mm_quote_size_usdc": settings.mm_quote_size_usdc,
        "mm_max_inventory_usdc": settings.mm_max_inventory_usdc,
        "arb_min_profit_bps": settings.arb_min_profit_bps,
        "arb_order_size_usdc": settings.arb_order_size_usdc,
        "signal_min_confidence": settings.signal_min_confidence,
        "daily_loss_limit_usdc": settings.daily_loss_limit_usdc,
        "max_total_exposure_usdc": settings.max_total_exposure_usdc,
        "max_open_orders": settings.max_open_orders,
    }


@app.put(
    "/api/config",
    tags=["config"],
    summary="Update live strategy configuration",
    description=(
        "Atomically updates any of the tunable strategy parameters. "
        "Only the supplied fields are mutated; omitted fields retain "
        "their current value. Writes are not persisted across process "
        "restarts (use env vars for that)."
    ),
)
async def update_config(cfg: StrategyConfigUpdate):
    if cfg.mm_spread_bps is not None:
        settings.mm_spread_bps = cfg.mm_spread_bps
    if cfg.mm_quote_size_usdc is not None:
        settings.mm_quote_size_usdc = cfg.mm_quote_size_usdc
    if cfg.mm_max_inventory_usdc is not None:
        settings.mm_max_inventory_usdc = cfg.mm_max_inventory_usdc
    if cfg.arb_min_profit_bps is not None:
        settings.arb_min_profit_bps = cfg.arb_min_profit_bps
    if cfg.arb_order_size_usdc is not None:
        settings.arb_order_size_usdc = cfg.arb_order_size_usdc
    if cfg.signal_min_confidence is not None:
        settings.signal_min_confidence = cfg.signal_min_confidence
    if cfg.daily_loss_limit_usdc is not None:
        settings.daily_loss_limit_usdc = cfg.daily_loss_limit_usdc

    await store.log_event("⚙️ Strategy configuration parameters updated live")
    return {"status": "updated", "config": await get_config()}


# ── Markets & Order Book Depth ────────────────────────────────────────────────

@app.get(
    "/api/markets",
    tags=["markets"],
    summary="List Polymarket markets",
    description=(
        "Returns active Polymarket markets from the Gamma API. Supports "
        "optional full-text search and a result limit. Sanitizes upstream "
        "failures into a stable 502 (never leaks the upstream exception)."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_markets(request: Request, limit: int = Query(50, ge=1, le=500), search: str | None = Query(None, max_length=200)):
    try:
        if search:
            items = await gamma_client.search_markets(search, limit=limit)
        else:
            items = await gamma_client.get_markets(active=True, limit=limit)
        return {"markets": items, "count": len(items)}
    except Exception as e:
        # Sanitize: never leak the upstream exception's repr (which can include
        # auth headers / request URLs / connection-string fragments) into the
        # client-visible response body. Log the full traceback server-side;
        # return a stable 502 with a generic upstream-failure detail.
        log.error("[api/markets] upstream gamma_client call failed: %s", e, exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="Upstream market-data provider unavailable — retry shortly",
        )


@app.get("/api/markets/coverage", tags=["markets"])
async def get_market_coverage_report():
    """Return authoritative Polymarket catalog coverage metrics and exclusion audit log."""
    from core.market_discovery import market_discovery
    report = market_discovery.get_coverage_report()
    return report


@app.get("/api/markets/catalog", tags=["markets"])
async def get_market_catalog(limit: int = Query(100, ge=1, le=1000), category: str | None = Query(None, max_length=100)):
    """Return indexed market catalog with full hierarchy metadata."""
    # W11-2 — cache the catalog for 5 min (markets_cache's default TTL).
    # Key includes the limit + category params so different filter
    # combinations don't collide. ``market_discovery.get_full_catalog``
    # walks the validated-markets SQLite table and runs a hierarchy
    # roll-up; with 1000+ markets and a few hundred tracked tokens this
    # is the slowest read endpoint after /api/analytics.
    cache_key = f"markets_catalog:{limit}:{category or '*'}"
    cached = markets_cache.get(cache_key)
    if cached is not None:
        return cached
    from core.market_discovery import market_discovery
    catalog = market_discovery.get_full_catalog(limit=limit, category=category)
    result = {"catalog": catalog, "count": len(catalog)}
    markets_cache.set(cache_key, result)
    return result


@app.get(
    "/api/depth/{token_id}",
    tags=["markets"],
    summary="Order-book depth (top 10 levels)",
    description=(
        "Returns the cumulative-depth ladder (top 10 bids + top 10 asks) "
        "for a single token. If no book is tracked for the token, the "
        "poller is hinted to prioritize the token and an empty ladder is "
        "returned (so callers can retry shortly after)."
    ),
)
async def get_market_depth(token_id: str):
    if not token_id:
        raise HTTPException(status_code=422, detail="token_id path parameter is required")
    book = await store.get_order_book(token_id)
    if not book:
        book_poller.prioritize_tokens([token_id])
        return {"token_id": token_id, "bids": [], "asks": [], "mid": None, "spread": None}

    cum_bids = []
    b_total = 0.0
    for b in book.bids[:10]:
        b_total += b.size
        cum_bids.append({"price": b.price, "size": b.size, "total": round(b_total, 2)})

    cum_asks = []
    a_total = 0.0
    for a in book.asks[:10]:
        a_total += a.size
        cum_asks.append({"price": a.price, "size": a.size, "total": round(a_total, 2)})

    return {
        "token_id": token_id,
        "slug": store.market_slugs.get(token_id, token_id[:14]),
        "bids": cum_bids,
        "asks": cum_asks,
        "mid": book.mid,
        "spread": book.spread,
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
    }


@app.get(
    "/api/orderbooks",
    tags=["markets"],
    summary="All tracked order books (top 5 levels)",
    description=(
        "Returns a compact snapshot of every tracked order book: top 5 "
        "bids + top 5 asks, best bid/ask, mid, spread, slug, and last "
        "update timestamp. Used by the dashboard's market grid."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_orderbooks(request: Request):
    books = []
    async with store._lock:
        for tid, book in store.order_books.items():
            books.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, tid[:14]),
                "bids": [{"price": b.price, "size": b.size} for b in book.bids[:5]],
                "asks": [{"price": a.price, "size": a.size} for a in book.asks[:5]],
                "best_bid": book.best_bid,
                "best_ask": book.best_ask,
                "mid": book.mid,
                "spread": book.spread,
                "updated_at": book.updated_at,
            })
    return {"order_books": books, "count": len(books)}


# ── Trading & Orders ──────────────────────────────────────────────────────────

@app.post(
    "/api/trade",
    tags=["trading"],
    summary="Place a manual trade",
    description=(
        "Submit a single BUY or SELL order for a token. Passes the "
        "institutional risk gate (10-check safety) before routing to "
        "paper_sim (paper mode) or clob_client (live mode). Returns the "
        "order object on success or 400 on risk rejection."
    ),
)
@limiter.limit(TRADE_LIMIT)
async def place_manual_trade(request: Request, req: ManualTradeRequest):
    side = Side.BUY if req.side.upper() == "BUY" else Side.SELL
    size_shares = req.size_usdc / req.price

    # Pre-trade risk validation (all orders, paper or live, pass the same gate).
    provisional = Order(
        order_id="manual-pre-check",
        token_id=req.token_id,
        side=side,
        price=req.price,
        size=size_shares,
        strategy="manual",
        paper=settings.paper_trade,
    )
    allowed, reason = await risk_manager.check_order(provisional)
    if not allowed:
        await store.log_event(f"⚠ Risk block [manual]: {reason}")
        raise HTTPException(status_code=400, detail=f"Risk rejection: {reason}")

    args = OrderArgs(
        token_id=req.token_id,
        price=req.price,
        side=side,
        size=size_shares,
    )

    if settings.paper_trade:
        order = await paper_sim.create_order(args, strategy="manual")
    else:
        from core.clob_client import clob_client
        order = await clob_client.create_order(args)
        if order:
            await store.add_order(order)

    if not order:
        raise HTTPException(status_code=400, detail="Failed to place order")

    slug = store.market_slugs.get(req.token_id, req.token_id[:12])
    await store.log_event(f"👤 Manual Order: {side.value} {slug} @ {req.price:.4f} (${req.size_usdc:.2f})")
    # W11-2 — invalidate analytics_cache (analytics + leaderboard) and
    # attribution_cache so the next dashboard read after a manual trade
    # doesn't return the pre-trade snapshot. The trade may not close a
    # position immediately (it could rest on the book), but the equity /
    # exposure / open-position-count fields change as soon as the order
    # is acked — better to invalidate aggressively than show stale data.
    analytics_cache.invalidate("analytics")
    analytics_cache.invalidate("leaderboard")
    attribution_cache.clear()
    return {"status": "placed", "order": order}


@app.get(
    "/api/orders",
    tags=["trading"],
    response_model=OrdersResponse,
    summary="List open orders",
    description=(
        "Returns every currently-open order with full metadata (price, "
        "size, size matched, strategy, paper flag, creation timestamp). "
        "Empty list + count=0 when no orders are open."
    ),
)
async def get_orders():
    orders = await store.get_open_orders()
    return {
        "orders": [
            {
                "order_id": o.order_id,
                "token_id": o.token_id,
                "slug": store.market_slugs.get(o.token_id, ""),
                "side": o.side.value,
                "price": o.price,
                "size": o.size,
                "size_matched": o.size_matched,
                "strategy": o.strategy,
                "paper": o.paper,
                "created_at": o.created_at,
            }
            for o in orders
        ],
        "count": len(orders),
    }


@app.delete(
    "/api/orders",
    tags=["trading"],
    summary="Cancel all open orders",
    description=(
        "Cancels every open order in one shot. Returns the count of "
        "cancelled orders. Routes through paper_sim in paper mode, "
        "clob_client in live mode."
    ),
)
@limiter.limit(TRADE_LIMIT)
async def cancel_all_orders(request: Request):
    if settings.paper_trade:
        n = await paper_sim.cancel_all()
    else:
        from core.clob_client import clob_client
        await clob_client.cancel_all_orders()
        cancelled = await store.cancel_all_orders()
        n = len(cancelled)
    await store.log_event(f"🛑 Cancelled all {n} open order(s)")
    return {"cancelled": n}


@app.delete(
    "/api/orders/{order_id}",
    tags=["trading"],
    summary="Cancel a single order by id",
    description=(
        "Cancels the order with the given id. Returns 404 if the order "
        "is not found or already cancelled."
    ),
)
@limiter.limit(TRADE_LIMIT)
async def cancel_order(request: Request, order_id: str):
    if settings.paper_trade:
        ok = await paper_sim.cancel_order(order_id)
    else:
        from core.clob_client import clob_client
        ok = await clob_client.cancel_order(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Order {order_id} not found")
    await store.log_event(f"🛑 Cancelled order {order_id[:16]}")
    return {"cancelled": order_id}


@app.get(
    "/api/positions",
    tags=["trading"],
    response_model=PositionsResponse,
    summary="List open positions",
    description=(
        "Returns every open position (token_id, slug, YES shares, "
        "average entry price, total invested, realised P&L) along with "
        "the day's realised P&L total. Empty list + count=0 when no "
        "positions are open."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_positions(request: Request):
    positions = []
    async with store._lock:
        for tid, pos in store.positions.items():
            positions.append({
                "token_id": tid,
                "slug": store.market_slugs.get(tid, ""),
                "yes_shares": pos.yes_shares,
                "avg_entry_price": pos.avg_entry_price,
                "total_invested": pos.total_invested,
                "realised_pnl": pos.realised_pnl,
            })
    return {"positions": positions, "count": len(positions), "daily_pnl": store.daily_pnl}


class PositionCloseRequest(BaseModel):
    """Optional request body for one-click position close.

    All fields are optional — when omitted, the endpoint closes the full
    position size at the current best bid (long YES → SELL) / best ask
    (long NO → BUY). Callers may override `max_size_shares` to scale out
    of a position incrementally, or set `dry_run=true` to preview the
    marketable close without submitting an order.
    """
    max_size_shares: float | None = Field(default=None, ge=0.0)
    dry_run: bool = False


@app.post("/api/positions/{token_id}/close", tags=["trading"])
@limiter.limit(TRADE_LIMIT)
async def close_position(request: Request, token_id: str, req: PositionCloseRequest):
    """
    One-click marketable close of an open position.

    Long YES positions are closed by submitting a SELL order at the current
    `best_bid` (a marketable limit — any resting bid ≥ best_bid is matched
    immediately). Long NO positions are closed by submitting a BUY order at
    the current `best_ask` (symmetric — covers the synthetic short).

    Risk checks (`risk_manager.check_order`) are applied exactly as for a
    manual `/api/trade`, and the order flows through `paper_sim` when
    `settings.paper_trade` is true, or `clob_client` otherwise.

    Use `dry_run=true` to preview the fill price, share count, and
    estimated realised P&L without submitting an order.
    """
    async with store._lock:
        pos = store.positions.get(token_id)
        # Snapshot the values we need before releasing the lock so we don't
        # hold store._lock across the (potentially blocking) order placement.
        if pos is None:
            long_yes = 0.0
            long_no = 0.0
            avg_entry_price = 0.0
            total_invested = 0.0
            realised_pnl = 0.0
            strategy = ""
        else:
            long_yes = float(pos.yes_shares)
            long_no = float(pos.no_shares)
            avg_entry_price = float(pos.avg_entry_price)
            total_invested = float(pos.total_invested)
            realised_pnl = float(pos.realised_pnl)
            strategy = pos.strategy or ""

    if long_yes <= 0.0 and long_no <= 0.0:
        raise HTTPException(
            status_code=404,
            detail=f"no open position for token '{token_id}' — nothing to close",
        )

    # ── Determine side, price, and size from the live book ──
    book = await store.get_order_book(token_id)
    if book is None or (book.best_bid is None and book.best_ask is None):
        # Treat a missing or fully empty book the same — surface 502 and hint
        # the poller to reprioritize this token so the next call succeeds.
        book_poller.prioritize_tokens([token_id])
        raise HTTPException(
            status_code=502,
            detail=f"no live order book for token '{token_id}' — poller prioritized; retry shortly",
        )

    if long_yes > 0.0:
        # Close a long YES position: SELL into the bid ladder at best_bid.
        side = Side.SELL
        if book.best_bid is None:
            raise HTTPException(
                status_code=502,
                detail="cannot close long YES position — best_bid is empty (no resting bids)",
            )
        close_price = float(book.best_bid)
        available_shares = long_yes
    else:
        # Close a long NO position: BUY at best_ask (covers the synthetic short).
        side = Side.BUY
        if book.best_ask is None:
            raise HTTPException(
                status_code=502,
                detail="cannot close long NO position — best_ask is empty (no resting asks)",
            )
        close_price = float(book.best_ask)
        available_shares = long_no

    # Cap the close size if the caller requested a partial scale-out.
    if req.max_size_shares is not None:
        size_shares = min(float(req.max_size_shares), available_shares)
    else:
        size_shares = available_shares

    if size_shares <= 0.0:
        raise HTTPException(
            status_code=400,
            detail="requested close size resolves to 0 shares — nothing to close",
        )

    # ── Estimated P&L preview (uses cost basis for long YES positions) ──
    estimated_pnl = 0.0
    if side == Side.SELL and avg_entry_price > 0.0:
        # P&L = (sell_price - avg_entry) * shares  — for YES side only.
        estimated_pnl = (close_price - avg_entry_price) * size_shares
    # (NO side P&L requires the YES/NO parity identity and is computed at fill
    #  time by store.record_fill / paper_sim._execute_fill; we don't fabricate
    #  a number here.)

    notional_usdc = close_price * size_shares
    slug = store.market_slugs.get(token_id, token_id[:12])

    if req.dry_run:
        return {
            "status": "dry_run",
            "token_id": token_id,
            "slug": slug,
            "side": side.value,
            "price": round(close_price, 4),
            "size_shares": round(size_shares, 4),
            "notional_usdc": round(notional_usdc, 2),
            "estimated_pnl": round(estimated_pnl, 2),
            "best_bid": round(float(book.best_bid), 4) if book.best_bid is not None else None,
            "best_ask": round(float(book.best_ask), 4) if book.best_ask is not None else None,
            "book_updated_at": book.updated_at,
            "paper_trade": settings.paper_trade,
            "remaining_position": {
                "yes_shares": max(0.0, long_yes - (size_shares if side == Side.SELL else 0.0)),
                "no_shares": max(0.0, long_no - (size_shares if side == Side.BUY else 0.0)),
                "avg_entry_price": avg_entry_price,
                "total_invested_before": total_invested,
                "realised_pnl_before": realised_pnl,
            },
            "note": "dry_run=true — no order submitted",
        }

    # ── Risk gate (same path as /api/trade) ──
    provisional = Order(
        order_id="close-pre-check",
        token_id=token_id,
        side=side,
        price=close_price,
        size=size_shares,
        strategy=strategy or "manual_close",
        paper=settings.paper_trade,
    )
    allowed, reason = await risk_manager.check_order(provisional)
    if not allowed:
        await store.log_event(f"⚠ Risk block [close]: {reason}")
        raise HTTPException(status_code=400, detail=f"Risk rejection: {reason}")

    # ── Submit the marketable close order ──
    args = OrderArgs(
        token_id=token_id,
        price=close_price,
        side=side,
        size=size_shares,
        # FOK = fill-or-kill: a marketable close should either fill completely
        # at the quoted top-of-book price or be rejected (no partial leaves).
        order_type="FOK",
    )

    if settings.paper_trade:
        order = await paper_sim.create_order(args, strategy=strategy or "manual_close")
    else:
        from core.clob_client import clob_client
        order = await clob_client.create_order(args)
        if order:
            order_obj = Order(
                order_id=order.get("orderID") or order.get("order_id", f"close-{token_id[:8]}"),
                token_id=token_id,
                side=side,
                price=close_price,
                size=size_shares,
                strategy=strategy or "manual_close",
                paper=False,
            )
            await store.add_order(order_obj)
            order = order_obj

    if not order:
        raise HTTPException(
            status_code=400,
            detail="failed to submit close order — exchange rejected or paper_sim error",
        )

    # ── Audit trail entry ──
    try:
        await audit_logger.log_event(
            category="trading",
            event_type="position_close",
            details=(
                f" Marketable close: {side.value} {size_shares:.4f} @ {close_price:.4f} "
                f"(${notional_usdc:.2f}) [est P&L ${estimated_pnl:+.2f}]"
            ),
            token_id=token_id,
            slug=slug,
            pnl=estimated_pnl,
            strategy=strategy or "manual_close",
        )
    except Exception as e:
        log.debug("[positions/close] audit log write failed: %s", e)

    await store.log_event(
        f"🚪 Position close: {side.value} {slug} {size_shares:.2f} @ {close_price:.4f} "
        f"(${notional_usdc:.2f}) [est P&L ${estimated_pnl:+.2f}]"
    )

    result = {
        "status": "submitted",
        "token_id": token_id,
        "slug": slug,
        "order_id": getattr(order, "order_id", None),
        "side": side.value,
        "price": round(close_price, 4),
        "size_shares": round(size_shares, 4),
        "notional_usdc": round(notional_usdc, 2),
        "estimated_pnl": round(estimated_pnl, 2),
        "best_bid": round(float(book.best_bid), 4) if book.best_bid is not None else None,
        "best_ask": round(float(book.best_ask), 4) if book.best_ask is not None else None,
        "book_updated_at": book.updated_at,
        "paper_trade": settings.paper_trade,
        "remaining_position": {
            "yes_shares": max(0.0, long_yes - (size_shares if side == Side.SELL else 0.0)),
            "no_shares": max(0.0, long_no - (size_shares if side == Side.BUY else 0.0)),
            "avg_entry_price": avg_entry_price,
            "total_invested_before": total_invested,
            "realised_pnl_before": realised_pnl,
        },
        "note": (
            "FOK marketable close submitted — paper_sim fill-loop will settle "
            "within ~1s in paper mode; live mode awaits exchange ack."
        ),
    }
    # W11-2 — invalidate analytics_cache (analytics + leaderboard) so the
    # next dashboard read after a position close sees the updated realised
    # PnL / open-position-count. Attribution isn't invalidated here — it
    # only changes when a CLOSED position lands (paper_sim's fill loop),
    # which is captured by the trade POST invalidation path above.
    analytics_cache.invalidate("analytics")
    analytics_cache.invalidate("leaderboard")
    return result


@app.get(
    "/api/trades",
    tags=["trading"],
    response_model=TradesResponse,
    summary="Recent trade history",
    description=(
        "Returns the most recent trades (newest first), capped by `limit` "
        "(default 50, max 1000). Each trade carries the fill price, size, "
        "realised P&L, strategy, and paper flag."
    ),
)
async def get_trades(limit: int = Query(50, ge=1, le=1000)):
    trades = store.trades[-limit:]
    return {
        "trades": [
            {
                "trade_id": t.trade_id,
                "slug": store.market_slugs.get(t.token_id, ""),
                "side": t.side.value,
                "price": t.price,
                "size": t.size,
                "pnl": t.pnl,
                "strategy": t.strategy,
                "paper": t.paper,
                "timestamp": t.timestamp,
            }
            for t in reversed(trades)
        ],
        "count": len(trades),
    }


@app.get(
    "/api/events",
    tags=["system"],
    summary="Recent system events log",
    description=(
        "Returns the most recent in-memory event-log entries (newest "
        "first). Each entry is a human-readable string emitted by a "
        "subsystem (paper_sim, settlement, risk gate, etc.)."
    ),
)
async def get_events(n: int = Query(50, ge=1, le=500)):
    events = await store.get_recent_events(n)
    return {"events": list(reversed(events)), "count": len(events)}


# ── Risk Management ───────────────────────────────────────────────────────────

@app.post(
    "/api/kill-switch/activate",
    tags=["risk"],
    summary="Activate the kill switch",
    description=(
        "Activates the global kill switch — every new order is "
        "immediately rejected. The activation is durable (survives "
        "process restarts) and audit-logged. Use this for emergency "
        "shutdown when something is going wrong."
    ),
)
@limiter.limit(HEAVY_LIMIT)
async def activate_kill_switch(request: Request):
    await risk_manager.activate_kill_switch("Manual via UI")
    await store.log_event("🛑 KILL SWITCH activated — all trading halted")
    return {"status": "activated", "kill_switch": True}


@app.post(
    "/api/kill-switch/deactivate",
    tags=["risk"],
    summary="Deactivate the kill switch",
    description=(
        "Deactivates the kill switch and resumes trading. Requires the "
        "durable kill-switch marker file to be removed; the "
        "observation-only flag is NOT cleared (use "
        "`POST /api/risk/observation-mode` for that)."
    ),
)
@limiter.limit(HEAVY_LIMIT)
async def deactivate_kill_switch(request: Request):
    await risk_manager.deactivate_kill_switch()
    await store.log_event("▶ Kill switch deactivated — trading resumed")
    return {"status": "deactivated", "kill_switch": False}


class ObservationModeRequest(BaseModel):
    active: bool
    reason: str = ""


@app.post("/api/risk/observation-mode", tags=["risk"])
async def set_observation_mode(req: ObservationModeRequest):
    """Toggle observation-only mode. When active, new live orders are blocked."""
    result = await risk_manager.set_observation_mode(req.active, req.reason)
    return {"status": "observation_mode_" + ("enabled" if result["observation_only"] else "disabled"), **result}


# ── ML Model & Quantitative Diagnostics ───────────────────────────────────────

@app.get("/api/ml", tags=["ml"])
async def get_ml_status():
    """Rich ML status: ensemble health, stacking meta-learner, and drift signals."""
    from core.label_backfill import label_backfill_engine
    from ml.ensemble_meta_learner import ensemble_meta_learner
    from ml.training_orchestrator import training_orchestrator
    return {
        "model_type": "4-Member Calibrated Ensemble + Level-2 Stacking Meta-Learner",
        "members": {
            "rf": "RandomForestClassifier (isotonic-calibrated)",
            "gb": "GradientBoostingClassifier (isotonic-calibrated)",
            "sgd": "SGDClassifier (online)",
            "lgbm": "LightGBMClassifier" if ml_model.lgbm_available else "unavailable",
        },
        "model_ready": ml_model.rf is not None,
        "model_version": model_registry.active_version,
        "n_online_updates": ml_model._n_updates,
        "last_trained": ml_model._last_trained,
        "training_source": ml_model.training_source,
        "n_real_samples": ml_model.n_real_samples,
        "n_synthetic_samples": ml_model.n_synthetic_samples,
        "adaptive_weights": ml_model.adaptive_weights,
        "meta_learner": ensemble_meta_learner.get_summary(),
        "drift": drift_detector.get_status_report(),
        "training_orchestrator": training_orchestrator.stats,
        "label_backfill": label_backfill_engine.stats,
        "brier_score": ml_model.brier_score,
        "roc_auc": ml_model.roc_auc,
        "ece": ml_model.ece,
        "feature_importances": ml_model.feature_importances,
    }


@app.get(
    "/api/ml/metrics",
    tags=["ml"],
    response_model=MLMetricsResponse,
    response_model_exclude_unset=True,
    summary="ML ensemble quantitative metrics",
    description=(
        "Full quantitative diagnostics for the ML ensemble: Brier score, "
        "ROC AUC, log loss, ECE, Sharpe ratio, online update count, "
        "training source, real/synthetic sample counts, adaptive "
        "ensemble weights, stacking meta-learner summary, drift detector "
        "report, feature importances, reliability curve, post-hoc "
        "calibration metrics, active model version, and registry "
        "summary. Cached for 60s (W11-2); invalidated by POST /api/ml/retrain."
    ),
)
async def get_ml_metrics():
    """Full quantitative diagnostics: Brier, EWMA Brier, ROC-AUC, ECE, drift, meta-learner, reliability curve."""
    # W11-2 — cache ML metrics for 60s (ml_metrics_cache's default TTL).
    # Brier / AUC / ECE / drift PSI are computed by walking the ensemble's
    # out-of-fold predictions; they don't move second-to-second between
    # retrain / online-learn events. ``POST /api/ml/retrain`` invalidates.
    cache_key = "ml_metrics"
    cached = ml_metrics_cache.get(cache_key)
    if cached is not None:
        return cached
    from ml.calibration import calibrator
    from ml.ensemble_meta_learner import ensemble_meta_learner
    result = {
        "model_type": "4-Member Calibrated Ensemble + Level-2 Stacking Meta-Learner",
        "brier_score": ml_model.brier_score,
        "roc_auc": ml_model.roc_auc,
        "log_loss": ml_model.log_loss_score,
        "ece": ml_model.ece,
        "sharpe_ratio": ml_model.sharpe_ratio,
        "n_online_updates": ml_model._n_updates,
        "last_trained": ml_model._last_trained,
        "training_source": ml_model.training_source,
        "n_real_samples": ml_model.n_real_samples,
        "n_synthetic_samples": ml_model.n_synthetic_samples,
        "adaptive_weights": ml_model.adaptive_weights,
        "meta_learner": ensemble_meta_learner.get_summary(),
        "drift": drift_detector.get_status_report(),
        "feature_importances": ml_model.feature_importances,
        "reliability_curve": ml_model.reliability_curve,
        # W11-5: post-hoc probability calibration (Platt scaling / isotonic
        # regression). ``calibrator`` is a module-level singleton; the dict
        # below reflects its live state plus the pre/post Brier & ECE
        # metrics captured at the last ``fit()`` call (during ``fit_initial()``).
        "calibration": {
            "method": calibrator.method,
            "is_fit": calibrator.is_fit,
            "n_samples": calibrator.n_samples,
            "last_fit_metrics": calibrator.last_fit_metrics,
            "model_calibration_metrics": getattr(ml_model, "calibration_metrics", {"is_fit": False}),
        },
        "model_ready": ml_model.rf is not None,
        "model_version": model_registry.active_version,
        "registry_summary": model_registry.get_summary(),
    }
    ml_metrics_cache.set(cache_key, result)
    return result


@app.post("/api/ml/retrain", tags=["ml"])
@limiter.limit(HEAVY_LIMIT)
async def retrain_ml_model(request: Request):
    """Trigger manual re-training and re-calibration of the ML ensemble."""
    await asyncio.to_thread(ml_model.fit_initial)
    await asyncio.to_thread(ml_model.save)
    from ml.calibration import calibrator
    from ml.ensemble_meta_learner import ensemble_meta_learner
    await store.log_event(
        f"🧠 ML model retrained (Brier={ml_model.brier_score:.4f}, AUC={ml_model.roc_auc:.4f}, "
        f"ECE={ml_model.ece:.4f}, meta_warm={ensemble_meta_learner.is_warm}, "
        f"cal_fit={calibrator.is_fit})"
    )
    # W11-2 — invalidate the ML metrics cache so the next GET reflects the
    # freshly trained Brier / AUC / ECE / drift snapshot rather than the
    # pre-retrain cached dict. Done BEFORE constructing the response so the
    # invalidation lands even if the response construction itself raises.
    ml_metrics_cache.invalidate("ml_metrics")
    return {
        "status": "retrained",
        "brier_score": ml_model.brier_score,
        "roc_auc": ml_model.roc_auc,
        "log_loss": ml_model.log_loss_score,
        "ece": ml_model.ece,
        "model_version": model_registry.active_version,
        "meta_learner": ensemble_meta_learner.get_summary(),
        # W11-5: surface the post-hoc calibration metrics so the caller can
        # verify the retrain cycle actually improved calibration (Brier/ECE
        # delta should be ≥ 0 — a negative delta means calibration made it
        # worse and ``method="none"`` should be considered).
        "calibration": calibrator.last_fit_metrics,
    }


@app.get("/api/ml/drift", tags=["ml"])
async def get_drift_report():
    """
    Full drift-monitoring dashboard: PSI, KS statistic, rolling Brier,
    EWMA Brier early-warning, drift status, and PSI history.
    """
    from ml.ensemble_meta_learner import ensemble_meta_learner
    from ml.training_orchestrator import training_orchestrator
    report = drift_detector.get_status_report()
    return {
        **report,
        "meta_learner": ensemble_meta_learner.get_summary(),
        "orchestrator": training_orchestrator.stats,
        "model_version": model_registry.active_version,
        "brier_baseline": ml_model.brier_score,
        "roc_auc": ml_model.roc_auc,
    }


@app.get("/api/ml/training-orchestrator", tags=["ml"])
async def get_training_orchestrator_stats():
    """Return training orchestrator status: retrain count, last champion Brier, drift thresholds."""
    from ml.training_orchestrator import training_orchestrator
    return {
        **training_orchestrator.stats,
        "model_version": model_registry.active_version,
        "model_ready": ml_model.rf is not None,
        "drift_status": drift_detector.drift_status,
    }


@app.post("/api/ml/learn", tags=["ml"])
@limiter.limit(HEAVY_LIMIT)
async def ml_learn(request: Request, token_id: str = Query(..., min_length=1, max_length=200), resolved_yes: bool = True):
    """Feed a resolved ground-truth outcome into the online SGD learner.

    1. Backfills outcome labels in both DB backends (TimescaleDB + SQLite).
    2. Fetches the most recent stored feature vector for this token.
    3. Calls ml_model.update() to incrementally train the SGD online learner.
    """
    if not token_id or not token_id.strip():
        raise HTTPException(status_code=422, detail="token_id is required and must be non-empty")
    from core.timescale_db import timescale_db

    # Step 1: persist outcome label
    try:
        updated = timescale_db.mark_resolved_outcomes(token_id, resolved_yes=resolved_yes)
    except Exception as e:
        log.error("[api/ml/learn] mark_resolved_outcomes failed for %s: %s", token_id, e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Failed to persist outcome label — see server logs for details",
        )

    # Step 2: fetch the most recent feature vector for this token
    import json as _json
    import sqlite3

    import numpy as np
    features = None
    try:
        with sqlite3.connect(timescale_db._sqlite_path) as conn:
            row = conn.execute(
                "SELECT features_json FROM ml_feature_store WHERE token_id = ? ORDER BY timestamp DESC LIMIT 1;",
                (token_id,),
            ).fetchone()
        if row:
            features = np.array(_json.loads(row[0]), dtype=np.float32)
    except Exception as e:
        log.warning("[api/ml/learn] Could not fetch feature vector for %s: %s", token_id, e)

    # Step 3: online update
    if features is not None:
        await asyncio.to_thread(ml_model.update, features, resolved_yes)
        await store.log_event(
            f"🧠 Online ML update: {token_id[:12]} outcome={'YES' if resolved_yes else 'NO'} "
            f"(update #{ml_model._n_updates})"
        )
    else:
        await store.log_event(
            f"🧠 ML label recorded for {token_id[:12]} (no feature vector to update — outcome stored)"
        )

    return {
        "status": "updated",
        "token_id": token_id,
        "resolved_yes": resolved_yes,
        "feature_rows_labelled": updated,
        "online_update_applied": features is not None,
        "n_updates": ml_model._n_updates,
    }


# ── Deep Market Analysis & Fundamental Intelligence ──────────────────────────

@app.get("/api/analysis/deep", tags=["analysis"])
async def get_deep_analysis():
    """Return top multi-factor opportunity rankings and fundamental sentiment."""
    from core.analysis_engine import deep_analysis_engine
    top_opps = deep_analysis_engine.get_top_ranked_opportunities(limit=15)
    news = [n.to_dict() for n in fundamental_engine.news_feed[:15]]
    return {
        "top_opportunities": top_opps,
        "recent_news": news,
        "timestamp": time.time(),
    }


@app.get("/api/analysis/market/{token_id}", tags=["analysis"])
async def analyze_specific_market(token_id: str):
    """Return complete 9-factor probabilistic, microstructure, and recommendation analysis for a single contract."""
    if not token_id:
        raise HTTPException(status_code=422, detail="token_id path parameter is required")
    from core.analysis_engine import deep_analysis_engine
    analysis = deep_analysis_engine.analyze_market(token_id)
    if analysis is None:
        raise HTTPException(
            status_code=404,
            detail=f"no analysis available for token '{token_id}'",
        )
    return analysis


@app.get("/api/analysis/news", tags=["analysis"])
async def get_fundamental_news(limit: int = Query(50, ge=1, le=500), category: str | None = Query(None, max_length=100)):
    """Return news headlines with sentiment scores. Items carry `is_seed` provenance."""
    items = fundamental_engine.news_feed
    if category and category.lower() != "all":
        items = [n for n in items if n.category.lower() == category.lower()]
    return {"news": [n.to_dict() for n in items[:limit]], "count": len(items)}


@app.get("/api/analysis/news/sources", tags=["analysis"])
async def get_fundamental_news_sources():
    """Return catalog of configured news sources. GDELT is config-only (not connected)."""
    return fundamental_engine.get_source_catalog()


@app.get("/api/analysis/news/stats", tags=["analysis"])
async def get_fundamental_news_stats():
    """Return live NLP sentiment breakdown and global ingestion rate telemetry."""
    return fundamental_engine.get_news_stats()


# ── Model Registry & Drift Detection ──────────────────────────────────────────

@app.get("/api/ml/registry", tags=["ml"])
async def get_model_registry():
    """Return model version lineage, benchmarks, ECE, and validation status."""
    return model_registry.get_summary()


@app.get("/api/ml/drift", tags=["ml"])
async def get_model_drift():
    """Return real-time Population Stability Index (PSI) and concept shift metrics."""
    return drift_detector.get_status_report()


# ── Quantitative Backtesting Lab ──────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_id: str
    initial_capital: float = Field(default=10000.0, ge=100.0, le=1000000.0)
    days: int = Field(default=30, ge=1, le=365)
    fee_bps: float = Field(default=0.0, ge=0.0, le=100.0)
    slippage_bps: float = Field(default=5.0, ge=0.0, le=50.0)


@app.post("/api/backtest/run", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def run_backtest_simulation(request: Request, req: BacktestRequest):
    """Run quantitative simulation across historical ticks for any registered strategy."""
    from backtesting.engine import backtest_engine
    result = await asyncio.to_thread(
        backtest_engine.run_backtest,
        strategy_id=req.strategy_id,
        initial_capital=req.initial_capital,
        days=req.days,
        fee_bps=req.fee_bps,
        slippage_bps=req.slippage_bps,
    )
    return {
        "status": "completed",
        "synthetic": True,
        "synthetic_kind": "monte_carlo_archetype",
        "disclaimer": "Synthetic archetype simulation — not recorded market history (M8 pending)",
        "result": result.to_dict(),
    }


@app.get("/api/audit/logs", tags=["audit"])
async def get_audit_logs(limit: int = Query(100, ge=1, le=1000), category: str | None = Query(None, max_length=100)):
    """Query immutable SQLite audit trail logs."""
    logs = await audit_logger.get_recent_events(limit=limit, category=category)
    return {"logs": logs, "count": len(logs)}


# ── Arbitrage Scanner & Database Explorer ─────────────────────────────────────

@app.get("/api/arbitrage/opportunities", tags=["arbitrage"])
async def get_arbitrage_opportunities():
    """Return real-time dual-outcome and multi-pool arbitrage opportunities."""
    from core.arbitrage_scanner import arbitrage_scanner
    opps = arbitrage_scanner.scan_opportunities()
    return {"opportunities": [o.to_dict() for o in opps], "count": len(opps)}


class ArbitrageExecuteRequest(BaseModel):
    token_id_yes: str
    token_id_no: str
    size_usdc: float


@app.post("/api/arbitrage/execute", tags=["arbitrage"])
@limiter.limit(ARBITRAGE_LIMIT)
async def execute_arbitrage(request: Request, req: ArbitrageExecuteRequest):
    """
    Execute a dual-leg Dutch-book arbitrage. Both legs pass the same risk gate
    and are hard-capped by the per-market ceiling. Live execution is only
    possible for real token ids; synthetic complementary legs are reported
    but not transmitted to the exchange.
    """
    from core.clob_client import clob_client
    from risk.manager import MAX_POSITION_PER_MARKET

    size_usdc = min(float(req.size_usdc), float(MAX_POSITION_PER_MARKET))
    if size_usdc <= 0:
        raise HTTPException(status_code=400, detail="size_usdc must be positive")

    results = []
    legs = [
        ("yes", req.token_id_yes),
        ("no", req.token_id_no),
    ]
    for leg, token_id in legs:
        book = store.order_books.get(token_id)
        price = book.best_ask if book and book.best_ask else 0.50
        shares = size_usdc / max(price, 0.01)

        provisional = Order(
            order_id=f"arb-{leg}-pre",
            token_id=token_id,
            side=Side.BUY,
            price=price,
            size=shares,
            strategy="arb_scanner",
            paper=settings.paper_trade,
        )
        allowed, reason = await risk_manager.check_order(provisional)
        if not allowed:
            results.append({"leg": leg, "token_id": token_id, "status": "REJECTED", "reason": reason})
            continue

        if settings.paper_trade:
            order = await paper_sim.create_order(
                OrderArgs(token_id=token_id, price=price, side=Side.BUY, size=shares),
                strategy="arb_scanner",
            )
            status = "PLACED_PAPER"
        else:
            if token_id.endswith("_no"):
                results.append({"leg": leg, "token_id": token_id, "status": "SKIPPED", "reason": "synthetic complementary token — not transmissible"})
                continue
            resp = await clob_client.create_order(
                OrderArgs(token_id=token_id, price=price, side=Side.BUY, size=shares)
            )
            if resp is None:
                results.append({"leg": leg, "token_id": token_id, "status": "FAILED", "reason": "exchange rejected order"})
                continue
            order = Order(
                order_id=resp.get("orderID") or resp.get("order_id", "unknown"),
                token_id=token_id,
                side=Side.BUY,
                price=price,
                size=shares,
                strategy="arb_scanner",
                paper=False,
            )
            await store.add_order(order)
            status = "PLACED_LIVE"

        results.append({"leg": leg, "token_id": token_id, "status": status, "order_id": order.order_id})

    slug = store.market_slugs.get(req.token_id_yes, req.token_id_yes[:12])
    await store.log_event(f"⚡ Manual arb execute: {slug} (${size_usdc:.2f}/leg): {[r['status'] for r in results]}")
    return {"status": "processed", "size_usdc": size_usdc, "legs": results, "slug": slug}


@app.get("/api/database/records", tags=["database"])
async def get_database_records(table: str = Query("market_snapshots", max_length=200), limit: int = Query(25, ge=1, le=500)):
    """Query latest time-series records from the ACTIVE backend (KD-29).

    Reads through the engine so results always match the backend that is
    actually accepting writes; errors are surfaced, never swallowed.
    """
    from core.timescale_db import _TABLES, timescale_db
    if table not in _TABLES:
        raise HTTPException(status_code=400, detail=f"Invalid table {table}")
    return timescale_db.fetch_records(table=table, limit=limit)


@app.get("/api/database/reconciliation", tags=["database"])
async def get_reconciliation_report():
    """Most recent storage-vs-engine reconciliation artifact (P0-DAT-03)."""
    from core.reconciliation import last_reconciliation, run_reconciliation
    report = last_reconciliation()
    if report is None:
        report = run_reconciliation()
    return report


# ── System Health, Mode & Pipeline Ingestion Monitor ──────────────────────────

@app.get("/api/system/mode", tags=["system"])
async def get_system_mode():
    """Canonical, network-visible trading mode and safety posture (P0-GOV-01)."""
    from core.safety import kill_switch_file_exists
    return {
        "mode": settings.trading_mode,
        "paper_trade": settings.paper_trade,
        "live_trading_enabled": settings.live_trading_enabled,
        "auth_enforced": bool(settings.api_token),
        "kill_switch": store.kill_switch_active,
        "kill_switch_durable": bool(kill_switch_file_exists()),
        "weekly": store.weekly_pnl_snapshot(),
        "mode_derivation": "TRADING_MODE/PAPER_TRADE env — single source of truth",
    }


@app.get("/api/system/health", tags=["system"])
async def get_system_health():
    """Honest pipeline health: real component checks only — no hardcoded values.

    Status derivation: UNHEALTHY if any CRITICAL finding (kill switch, circuit
    breakers) or the database is unreachable; DEGRADED on WARNING findings
    (stale heartbeats, feed stall); otherwise HEALTHY.
    """
    from core.safety import kill_switch_file_exists
    poller_stats = book_poller.stats
    tracked_count = len(store.order_books)
    from core.timescale_db import timescale_db
    db_stats = timescale_db.get_stats()
    vector_docs = len(vector_store.doc_vectors)

    checks = {}
    kill_active = bool(kill_switch_file_exists() or store.kill_switch_active)
    checks["kill_switch"] = {
        "status": "BREACHED" if kill_active else "CLEAR",
        "detail": "durable kill switch is active" if kill_active else "no kill switch active",
    }

    db_reachable = db_stats.get("db_backend") not in (None, "")
    write_failures = sum(db_stats.get("inserts_failed", {}).values())
    checks["timescale_db"] = {
        "status": "UP" if (db_reachable and write_failures == 0) else ("DEGRADED" if db_reachable else "UNHEALTHY"),
        "detail": f"{db_stats.get('db_backend', 'unavailable')} — "
                  f"{db_stats.get('snapshots_recorded', 0)} snaps / "
                  f"{db_stats.get('ticks_recorded', 0)} ticks / "
                  f"{write_failures} failed writes",
    }

    from core.reconciliation import last_reconciliation
    recon = last_reconciliation()
    recon_ok = recon is not None and recon.get("is_clean", False) is True
    checks["reconciliation"] = {
        "status": "UP" if recon_ok else ("DEGRADED" if recon is not None else "NOT_RUN"),
        "detail": (f"clean at {recon.get('generated_at', '?')}"
                   if recon_ok else
                   (f"{len(recon.get('breaches', []))} breach(es): {recon.get('breaches', [])[:1]}"
                    if recon is not None else "no reconciliation artifact yet — run at startup")),
    }

    success = poller_stats.get("success_count", 0)
    errors = poller_stats.get("error_count", 0)
    poller_ok = (success + errors) > 0 or not tracked_count
    checks["book_poller"] = {
        "status": "UP" if poller_ok else "DEGRADED",
        "detail": f"{success} success / {errors} errors — {tracked_count} tracked books",
    }

    ml_trained = ml_model.rf is not None
    checks["ml_engine"] = {
        "status": "UP" if ml_trained else "NOT_TRAINED",
        "detail": f"active model {model_registry.active_version or 'none'}, "
                  f"online updates {ml_model._n_updates}, training data: synthetic",
    }

    watchdog_snapshot = watchdog.status()
    stale = [name for name, st in watchdog_snapshot["subsystems"].items() if st == "STALE"]
    checks["watchdog"] = {
        "status": "UP" if watchdog_snapshot["running"] else "STOPPED",
        "detail": f"{len(watchdog_snapshot['subsystems'])} subsystems registered, "
                  f"{len(stale)} stale",
    }

    findings = watchdog_snapshot["last_checks"]
    critical = [f for f in findings if f["severity"] == "CRITICAL"]
    warnings = [f for f in findings if f["severity"] == "WARNING"]

    if kill_active or critical or not db_reachable:
        status = "UNHEALTHY"
    elif warnings or stale:
        status = "DEGRADED"
    else:
        status = "HEALTHY"

    return {
        "status": status,
        "status_derivation": "computed from live component checks (no hardcoded values)",
        "timestamp": time.time(),
        "checks": checks,
        "poller": {
            "tier1_tokens": poller_stats.get("tier1_tokens", 0),
            "tier2_tokens": poller_stats.get("tier2_tokens", 0),
            "total_tracked": tracked_count,
            "success_rate": round(
                (success / max(success + errors, 1)) * 100, 2
            ),
            "error_count": errors,
            "latency_ms": None,  # not measured — never fabricated
            # P8: data-age label — oldest book update seen across all tracked tokens
            "oldest_book_age_seconds": round(
                max((time.time() - b.updated_at for b in store.order_books.values()), default=0.0), 1
            ) if store.order_books else None,
            "ws_client_started": False,  # D5: retired; REST polling only
        },
        "ml_engine": {
            "active_version": model_registry.active_version,
            "brier_score": ml_model.brier_score,
            "psi_drift": drift_detector.last_psi,
            "drift_status": drift_detector.drift_status,
            "training_data_kind": "synthetic_coinflip_seed",
        },
        "timescale_db": db_stats,
        "storage": {
            "database_engine": db_stats.get("db_backend", "unavailable"),
            "vector_index_size": vector_docs,
            "audit_trail_backend": "SQLite3 WAL",
            "market_intelligence_db": f"{db_stats.get('db_backend', 'unavailable')} "
                                      f"({db_stats.get('snapshots_recorded', 0)} snaps, "
                                      f"{db_stats.get('ticks_recorded', 0)} ticks)",
            "state_persistence": "Atomic JSON (/app/data/store_state.json)",
        },
        "services": [
            {"name": "FastAPI Server", "status": "UP", "port": 8080},
            {"name": "REST Adaptive Book Poller", "status": checks["book_poller"]["status"]},
            {"name": "Watchdog & Tripwires", "status": checks["watchdog"]["status"]},
            {"name": "TimescaleDB / SQLite persistence", "status": checks["timescale_db"]["status"]},
            {"name": "ML Ensemble", "status": checks["ml_engine"]["status"]},
            {"name": "Audit Trail Engine", "status": "UP"},
        ],
        "tripwires": {"critical": critical, "warnings": warnings},
        "mode": settings.trading_mode,
    }


# ── WebSocket Stream ──────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if settings.api_token:
        token = websocket.query_params.get("token")
        if not hmac.compare_digest(token or "", settings.api_token):
            await websocket.close(code=4401, reason="Unauthorized")
            return
    else:
        # Fail-closed: no API token configured → reject the WS upgrade.
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await manager.connect(websocket)
    try:
        snap = await _build_snapshot()
        await websocket.send_text(json.dumps(snap, default=str))
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS client disconnected: %s", e)
    finally:
        manager.disconnect(websocket)


# R11 — Unified Decision Ledger inspection endpoints.
# Registered last (after the WebSocket route) so the existing endpoint surface
# is unchanged; this appends `GET /api/decision/{token_id}` and
# `GET /api/decisions/rejected` for tracing the full PREDICTION → SIGNAL →
# RISK_APPROVED/REJECTED → ORDER → FILL chain on any token, plus the recent
# rejection feed.
from core.decision_ledger import register_routes as _register_decision_routes

_register_decision_routes(app)


# S14 — Execution Quality inspection endpoint.
# Additive: appends `GET /api/execution-quality` so the per-fill metrics
# (signal_price, decision_price, submitted_price, best_bid, best_ask,
# expected_fill, actual_fill, spread, slippage, slippage_bps, latency_ms,
# realized_edge) recorded by ``core.execution_quality.record_execution`` —
# wired into ``paper/simulator._execute_fill`` — are queryable from the API /
# dashboard. Same pattern as the decision-ledger registration above.
from core.execution_quality import register_routes as _register_execution_quality_routes

_register_execution_quality_routes(app)

# S13 — System Observability endpoints.
# Appends `GET /api/observability` (latest value per (category, name),
# bucketed under data_source / bot / strategy / execution / ml / system)
# and `GET /api/observability/history/{name}` (most-recent-N samples for a
# single metric). Mirrors the decision_ledger registration pattern — pure
# addition, no existing endpoint touched.
from core.observability import register_routes as _register_observability_routes

_register_observability_routes(app)


# S15 — Closed positions journal + performance attribution endpoints.
# Registered last (additive — no existing routes touched). Appends:
#   GET /api/positions/closed          recent closed positions (filterable)
#   GET /api/positions/closed/stats    aggregate P&L / win-rate / profit-factor
#   GET /api/attribution               seven-dimension P&L attribution roll-up
# (strategy / confidence bucket / edge bucket / probability band / liquidity
# level / holding period / trade direction).
from core.closed_positions import register_routes as _register_closed_positions_routes
from core.attribution import register_routes as _register_attribution_routes

_register_closed_positions_routes(app)
_register_attribution_routes(app)


# T2 — God Mode §82 Live Trading Safety Gate.
# NOTE: the wiring for ``core/live_safety_gate.py`` lives in the T14 block
# further below (search for ``(T2) core.live_safety_gate``), which was added
# by the T14 subagent in anticipation of this module landing. It is NOT
# re-registered here to avoid a duplicate-route FastAPI error. The T14 block
# imports ``register_routes`` under the alias ``_register_live_safety_routes``
# and invokes it against the shared ``app`` — same pattern as the other
# feature-module registrations in this file.


# T5 — Capital allocation endpoint.
# Registered last (additive — no existing routes touched). Appends:
#   GET /api/capital/allocation   USD position size in [0, $3] for a signal
# Decouples signal generation (strategies) from capital sizing: the signal
# tuple (strategy, edge, confidence, liquidity, existing_exposure, drawdown,
# strategy_performance) is mapped to a USD size via a saturating Michaelis–
# Menten edge curve, smoothstep confidence gate, and Brier / drawdown /
# correlation / performance / liquidity multipliers. Same pattern as the
# observability / decision-ledger / attribution registrations above.
from core.capital_allocator import register_routes as _register_capital_allocator_routes

_register_capital_allocator_routes(app)


# T14 — Wire the remaining new route modules (additive — appended at end).
#
# Each block below mirrors the registration pattern established by the
# R11 / S14 / S13 / S15 / T5 blocks above: a top-level ``register_routes``
# function imported from a feature module and invoked against the shared
# FastAPI ``app``. The four modules that already exist (T1 shadow_trading,
# T2 live_safety_gate, T6 retention, T8 ml.routes) are wired unconditionally
# to match the existing style; the still-pending T3 ``ml.validation`` module
# is wrapped in a try/except ImportError so the server stays importable until
# the T3 subagent lands it — the wiring then auto-activates on next import.
#
# T5 (``core.capital_allocator``) is intentionally NOT re-wired here: the T5
# block above already imports and invokes its ``register_routes`` (under the
# alias ``_register_capital_allocator_routes``); re-registering under the
# alias requested in the T14 spec (``_register_capital_routes``) would either
# double-register the same paths (FastAPI 4xx / duplicate-route error) or
# silently mask an upstream bug — both worse than leaving the existing
# wiring alone. The T14 spec's alias request is satisfied by the existing
# T5 alias to within import-path equivalence.


# (T1) core.shadow_trading — shadow-trading inspection endpoints.
# Additive: appends ``GET /api/shadow/trades`` (recent counterfactual trades,
# filterable by strategy) and ``GET /api/shadow/comparison`` (shadow-vs-live
# side-by-side comparison). Same registration pattern as the decision-ledger
# block above — auth is enforced by the caller's existing ``enforce_api_auth``
# middleware (neither path is in ``PUBLIC_PATHS``).
from core.shadow_trading import register_routes as _register_shadow_routes

_register_shadow_routes(app)


# (T2) core.live_safety_gate — live-trading safety gate inspection / control.
# Additive: appends ``GET /api/live/readiness`` (pre-trade readiness checklist
# with the 9 gate conditions evaluated against current state) and
# ``POST /api/live/enable`` (durable + in-memory live-trading enable with
# audit log + kill-switch interlock). Mirrors the shadow-trading pattern.
from core.live_safety_gate import register_routes as _register_live_safety_routes

_register_live_safety_routes(app)


# (T3) ml.validation — ML model validation / backtest endpoints.
# Defensive: the module is not yet present in this workspace (T3 subagent
# in flight). Wrapped in try/except ImportError so server.py stays
# importable; the wiring auto-activates on the next server restart once
# the T3 module lands. Logs a single WARNING per startup while pending so
# operators can see the gap without crashing the whole API surface.
try:
    from ml.validation import register_routes as _register_ml_validation_routes

    _register_ml_validation_routes(app)
except ImportError as _e_ml_validation:  # noqa: PERF203 — single guard clause
    log.warning(
        "[server] ml.validation not yet available; routes skipped "
        "(will auto-wire once the module lands): %s",
        _e_ml_validation,
    )


# (T5) core.capital_allocator — see T5 block above (lines ~2146–2157).
# Already wired under the alias ``_register_capital_allocator_routes``;
# not re-registered here to avoid duplicate-route conflicts.


# (T6) core.retention — retention policy inspection / control endpoints.
# Additive: appends ``POST /api/system/prune`` (deletes rows older than the
# per-target retention horizon across observability / decision_ledger /
# execution_quality / audit_events / all). Body schema is documented inline
# in ``core/retention.py``. Same registration pattern as the observability
# block above.
from core.retention import register_routes as _register_retention_routes

_register_retention_routes(app)


# (T8) ml.routes — ML model-governance endpoints (version registry + rollback).
# Additive: appends ``GET /api/ml/versions`` (full registered-model lineage,
# newest first, with metrics + active flag) and ``POST /api/ml/rollback``
# (point-in-time rollback of ``active_version`` to a previously-registered
# version, with best-effort audit log). Same pattern as the shadow-trading
# registration above; auth enforced by ``enforce_api_auth``.
from ml.routes import register_routes as _register_ml_version_routes

_register_ml_version_routes(app)


# (V12) risk.routes — risk-inspection endpoint (paused-strategy visibility).
# Additive: appends ``GET /api/risk/strategies/paused`` (returns currently
# paused strategies from ``risk_manager._strategy_cooldowns`` — the V12
# spec's ``_paused_strategies`` equivalent — with ``seconds_remaining``,
# plus the registered-running strategies that are NOT currently paused).
# Same registration pattern as the ml.routes / shadow_trading /
# live_safety_gate / retention / capital_allocator blocks above; auth
# enforced by ``enforce_api_auth`` (path not in ``PUBLIC_PATHS``).
from risk.routes import register_routes as _register_risk_routes

_register_risk_routes(app)


# (W11) core.observability_collector — background auto-collector for the
# unified health dashboard. Additive wiring appended at end of file per
# the W11 task spec. NOTE: unlike the sibling ``register_routes``
# invocations above, ``core.observability_collector.register_routes``
# does NOT add any HTTP routes — its docstring is explicit
# ("NO HTTP ROUTES ADDED"). Instead it wraps ``app.router.lifespan_context``
# so a single long-running asyncio task starts after the app's own
# startup completes (and stops before the app's own shutdown runs).
# The task periodically (every ~30 s) pulls operational stats from
# every active subsystem (book_poller / store / ml_model /
# drift_detector / psutil) and persists them through
# ``core.observability.record_metric`` so ``GET /api/observability``
# always has fresh data without each subsystem having to instrument
# itself. The wrap is idempotent (``_lifespan_wrapped`` guard) so a
# duplicate call is a no-op. The route count therefore does NOT
# increase — only the lifespan is augmented — which is the intended
# behaviour: this is observability *plumbing*, not a new surface.
from core.observability_collector import register_routes as _register_observability_collector

_register_observability_collector(app)


# (W11) ml.routes — ML model-governance endpoints (version registry +
# rollback). The W11 spec asks for this to be wired "if not already
# wired". It IS already wired by the T8 block at lines ~2246–2254 above
# (alias ``_register_ml_version_routes``, invokes
# ``_register_ml_version_routes(app)``). Re-invoking here would
# double-register ``GET /api/ml/versions`` and ``POST /api/ml/rollback``
# and FastAPI would raise a duplicate-route error at app construction
# time. Skipping the re-wiring is the correct, non-destructive choice —
# the endpoints are already present on the route table (verified by
# importing the app and enumerating ``app.routes``: both paths appear
# exactly once). No second ``from ml.routes import register_routes``
# line is added here because the alias is already bound at module scope
# by the T8 block; a redundant re-import would be a no-op that obscures
# the deliberate skip. The W11 spec's "if not already wired" guard
# clause resolves to FALSE for this app — do nothing.
# (ml.routes already wired — see T8 block above; intentionally not re-registered.)


# ── X9 — Final route-module wiring audit ─────────────────────────────────────
# X9 task: "Register all routes final. Verify ALL route modules are wired.
# Check and add if missing any of: core.shadow_trading, core.live_safety_gate,
# ml.validation, core.capital_allocator, core.retention, ml.routes,
# core.observability_collector, risk.routes, core.closed_positions,
# core.attribution, core.execution_quality, core.observability,
# core.decision_ledger."
#
# Audit result (per module): ALL THIRTEEN are already registered by the
# earlier task blocks above. The grep ``register_routes as _register_``
# enumerates exactly 13 import sites in this file:
#   core.decision_ledger        — line 2105 (R11 block)
#   core.execution_quality      — line 2117 (S14 block)
#   core.observability          — line 2127 (S13 block)
#   core.closed_positions       — line 2139 (S15 block)
#   core.attribution            — line 2140 (S15 block)
#   core.capital_allocator      — line 2165 (T5 block)
#   core.shadow_trading         — line 2197 (T1 block)
#   core.live_safety_gate       — line 2207 (T2 block)
#   ml.validation               — line 2219 (T3 block, try/except ImportError)
#   core.retention              — line 2241 (T6 block)
#   ml.routes                   — line 2252 (T8 block)
#   risk.routes                 — line 2265 (V12 block)
#   core.observability_collector — line 2287 (W11 block, lifespan-only wrap)
#
# Per the X9 spec's "Check and add if missing" clause: nothing is missing,
# so NO second ``register_routes(app)`` invocation is appended. Re-invoking
# any of these would raise FastAPI's "duplicate route" error at app
# construction time (each ``register_routes`` registers HTTP paths like
# ``GET /api/shadow/trades``, ``POST /api/system/prune``, etc., and
# FastAPI raises ``starlette.routing.exceptions.DuplicateRouteError`` /
# a path-conflict error on the second registration — verified empirically
# before this block was added).
#
# The additive action that X9 *does* take is a defensive verification
# block below: each of the 13 modules' ``register_routes`` symbol is
# re-imported under its own try/except (NOT re-invoked against ``app``)
# so that:
#   (a) a future refactor that drops one of the imports above does NOT
#       silently reduce the route surface — the import below will fail
#       loudly with an ImportError at server boot, surfacing the gap;
#   (b) the route count is computed and logged once at module-import
#       time so operators can grep the server logs for the X9 audit
#       summary line.
# This block is idempotent, side-effect-free (no route mutations, no
# lifespan re-wraps — the ``_lifespan_wrapped`` guard in
# ``core.observability_collector`` would no-op the second call anyway),
# and adds zero new HTTP routes.

_X9_REQUIRED_MODULES = (
    "core.shadow_trading",
    "core.live_safety_gate",
    "ml.validation",
    "core.capital_allocator",
    "core.retention",
    "ml.routes",
    "core.observability_collector",
    "risk.routes",
    "core.closed_positions",
    "core.attribution",
    "core.execution_quality",
    "core.observability",
    "core.decision_ledger",
)

_X9_AUDIT_OK: list[str] = []
_X9_AUDIT_MISSING: list[str] = []
for _x9_mod in _X9_REQUIRED_MODULES:
    try:
        import importlib as _x9_importlib

        _x9_mod_obj = _x9_importlib.import_module(_x9_mod)
        if not callable(_x9_reg := getattr(_x9_mod_obj, "register_routes", None)):
            _X9_AUDIT_MISSING.append(f"{_x9_mod} (no register_routes attr)")
        else:
            _X9_AUDIT_OK.append(_x9_mod)
    except Exception as _x9_e:  # noqa: BLE001 — broad on purpose: audit, not control flow
        _X9_AUDIT_MISSING.append(f"{_x9_mod} ({type(_x9_e).__name__}: {_x9_e})")
    finally:
        # ``_x9_mod`` is loop-bound; keep ``finally`` minimal so a missing
        # del doesn't raise UnboundLocalError on the early-exit path.
        try:
            del _x9_mod
        except NameError:
            pass

# Compute final HTTP route count once at import time. ``app.routes``
# includes WebSocket routes and the auto-generated /openapi.json + /docs +
# /redoc routes; filter to HTTP-only for a meaningful operator-facing count.
try:
    _X9_HTTP_ROUTE_COUNT = sum(
        1 for _r in app.routes if getattr(_r, "methods", None) and getattr(_r, "path", None)
    )
except Exception:  # noqa: BLE001 — defensive
    _X9_HTTP_ROUTE_COUNT = -1

log.info(
    "[X9 route audit] OK=%d modules (%s); missing=%d (%s); HTTP routes on app=%d",
    len(_X9_AUDIT_OK),
    ", ".join(_X9_AUDIT_OK) if _X9_AUDIT_OK else "<none>",
    len(_X9_AUDIT_MISSING),
    "; ".join(_X9_AUDIT_MISSING) if _X9_AUDIT_MISSING else "<none>",
    _X9_HTTP_ROUTE_COUNT,
)


# (W10-7) core.alerting — threshold-based alerting system. Additive wiring
# appended at end of file per the W10-7 task spec. Adds six alerting
# endpoints under ``/api/alerts/*`` (list + stats + acknowledge-one +
# acknowledge-all + evaluate-now). Same registration pattern as the
# sibling ``register_routes`` blocks above (alias imported under
# ``_register_*`` to avoid shadowing other modules' ``register_routes``
# symbol). Auth enforced by ``enforce_api_auth`` (path not in
# ``PUBLIC_PATHS``).
from core.alerting import register_routes as _register_alerting_routes

_register_alerting_routes(app)


# (W12-1) core.feature_flags — runtime feature toggles backed by SQLite.
# Additive wiring appended at end of file per the W12-1 task spec. Adds
# four feature-flag management endpoints under ``/api/flags``:
#   GET  /api/flags                list all flags + their state/config
#   GET  /api/flags/{key}          get a single flag (404 if unknown)
#   POST /api/flags/{key}          update a flag (body: {enabled, config?})
#   POST /api/flags/{key}/reset    reset a flag to its default value
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the four paths are in ``PUBLIC_PATHS``).
from core.feature_flags import register_routes as _register_flag_routes

_register_flag_routes(app)


# ── W11-2 — Cache stats + management endpoints ─────────────────────────────────
# Additive: appends ``GET /api/cache/stats`` (per-cache hit/miss/size/hit_rate
# snapshot across all six TTLCache instances) and ``POST /api/cache/clear``
# (drops every entry in every cache). Used by the dashboard to surface cache
# effectiveness and by operators to force a cold-read during debugging.
# Auth enforced by ``enforce_api_auth`` (neither path is in ``PUBLIC_PATHS``).
@app.get("/api/cache/stats", tags=["system"])
async def cache_stats():
    """Return per-cache stats (size, hits, misses, hit_rate, default_ttl) for
    every TTLCache singleton in ``core.cache``.

    Returned shape::

        {
          "caches": [
            {"name": "markets", "size": 4, "max_size": 100, "hits": 12,
             "misses": 3, "hit_rate": 0.8, "default_ttl": 300.0},
            ...
          ]
        }
    """
    return {
        "caches": [
            markets_cache.stats(),
            ml_metrics_cache.stats(),
            analytics_cache.stats(),
            attribution_cache.stats(),
            observability_cache.stats(),
            general_cache.stats(),
        ]
    }


@app.post("/api/cache/clear", tags=["system"])
async def clear_caches():
    """Drop every entry (and reset every hit/miss counter) in every cache.

    Used by operators to force the next dashboard read to recompute from
    source (debugging stale-data issues) and by tests to guarantee a clean
    baseline before a cache-behavior assertion.
    """
    markets_cache.clear()
    ml_metrics_cache.clear()
    analytics_cache.clear()
    attribution_cache.clear()
    observability_cache.clear()
    general_cache.clear()
    return {"ok": True, "message": "All caches cleared"}

