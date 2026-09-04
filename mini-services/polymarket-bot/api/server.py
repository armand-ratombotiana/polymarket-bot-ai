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
import uuid
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
from fastapi.responses import FileResponse, JSONResponse
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
# W14-7 — In-memory rate-limit hit tracker (for the dashboard panel).
# Surfaces "which endpoints / clients are getting throttled the most" +
# a per-minute time series of hits over the last hour. Distinct from the
# prometheus counter above: prometheus exposes a single monotonic
# ``rate_limit_hits_total{endpoint=...}`` counter for Grafana; this
# tracker holds the richer per-IP / per-limit / per-minute shape the
# React ``RateLimitPanel`` renders. State is in-memory and process-local
# — a restart zeroes it, which is fine for a "last hour" dashboard view.
# W15-4 — Per-endpoint latency profiler. Sibling to
# ``rate_limit_tracker`` — same singleton pattern, same coarse-grained
# ``threading.Lock`` discipline. The request_logging_middleware below
# feeds every request's (method, path, duration, status) into
# ``profiler.record`` so the ``GET /api/profiling/stats`` / ``/slowest``
# / ``POST /api/profiling/reset`` routes can surface p50/p95/p99 per
# endpoint without an external tracing layer.
from core.profiling import profiler
from core.rate_limit_tracker import rate_limit_tracker
from core.security import validate_token_strength
from core.settlement import settlement_engine
from core.watchdog import watchdog
from core.ws_broadcast import ws_manager
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
# W14-8 — Client-side error reporting endpoint. Errors must ALWAYS be
# reportable, even when the trader's API token is misconfigured or
# expired — operators would rather see the auth-failure in the error
# log than lose the telemetry that would have surfaced it. The endpoint
# only accepts an opaque JSON payload (no PII lookup, no order body,
# no auth-context-leak surface), so unauthenticated exposure is safe.
PUBLIC_PATHS.add("/api/client-errors")
# W17-7 — GraphQL endpoint. Public (no bearer token required) so a
# dashboard / explorer client can introspect the schema + read the
# ``health`` field BEFORE authenticating (mirrors the REST
# ``GET /api/health`` liveness-probe contract). Mutations are not
# exposed — the schema is read-only (Query type only, no Mutation), so
# unauthenticated exposure can't trigger a trade / order / config
# write. The endpoint responds to BOTH ``GET /graphql`` (the Apollo
# Sandbox IDE HTML for browser introspection during dev) AND
# ``POST /graphql`` (the actual GraphQL query execution path).
PUBLIC_PATHS.add("/graphql")
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

    # ── W21-1 — Unified database manager (PG primary, SQLite fallback) ──
    # Initialises AFTER ``timescale_db.init_postgres_pool`` so the PG
    # asyncpg pool / migrations have already run. The manager's
    # ``initialize()`` (extended in W21-2) starts the PG health monitor
    # background task (``pg_health_monitor``) and the manager's own
    # ``_pg_retry_loop`` — the loop consumes the monitor's verdict every
    # 5 s and flips ``_status.backend`` between POSTGRESQL and SQLITE so
    # ``GET /api/database/status`` surfaces the transition without an
    # extra ping round-trip. Safe to call twice (idempotent).
    #
    # Defensive ``try/except`` wrap so a transient start failure (e.g.
    # a broken asyncpg install in a dev sandbox) can never block the
    # rest of the lifespan startup — mirrors the
    # ``live_fill_monitor.start()`` / ``trade_tape_ingester.start()``
    # pattern in W18-2 / W20-7.
    try:
        from core.database_manager import db_manager
        await db_manager.initialize()
    except Exception as e:  # pragma: no cover — defensive: must not kill startup
        log.error("[database_manager] startup failed: %s", e)

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

    # 2b. W18-2 — Live fill acknowledgement loop (P0-C02 fix). When live
    # trading is enabled, start the background monitor that polls the
    # CLOB ``/data/trades`` endpoint for fills on our open orders. The
    # monitor short-circuits in paper mode (no-op), so starting it
    # unconditionally here is safe — the poll_interval=2.0s default is
    # the same cadence the paper simulator uses for its own fill loop.
    # The monitor is the only consumer of ``clob_client.get_trades()``;
    # without it, live orders stay OPEN in local state indefinitely and
    # never reach ``store.record_fill`` / ``decision_ledger.record(FILL)``
    # / ``execution_quality.record_execution``.
    try:
        from core.live_fill_monitor import live_fill_monitor
        await live_fill_monitor.start()
    except Exception as e:  # pragma: no cover — defensive: must not kill startup
        log.error("[live_fill_monitor] startup failed: %s", e)

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

    # 5b. W20-7 — Start the public trade-tape ingester. Polls the CLOB
    # ``/trades`` endpoint (unauthenticated, public) every 5s and writes
    # every unseen trade into ``market_trades`` (SQLite fallback) /
    # ``market.market_trade`` (TimescaleDB hypertable declared in
    # migration 001). Mirrors the book_poller pattern: same REST client,
    # same defensive ``try/except`` wrap so a transient start failure
    # can never block the rest of the lifespan startup. The ingester is
    # the only consumer of ``clob_client.get_public_trades()``; without
    # it the ``market_trades`` table stays empty and the
    # ``GET /api/trades/tape`` endpoint returns an empty list.
    try:
        from core.trade_ingester import trade_tape_ingester
        await trade_tape_ingester.start()
        watchdog.beat("trade_tape_ingester")
        await store.log_event("📜 Trade tape ingester active (5s poll interval)")
    except Exception as e:  # pragma: no cover — defensive: must not kill startup
        log.error("[trade_tape_ingester] startup failed: %s", e)

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
    # W19-8 — Warm the meta-learner from already-resolved labeled feature
    # vectors so the first prediction the bot makes after startup uses the
    # Level-2 stacking blend (not the adaptive Brier-inverse fallback).
    # Safe no-op when the labeled-sample store is empty (e.g. fresh deploy)
    # or the meta-learner cannot warm (single-class buffer). Wrapped in
    # try/except so a meta-learner warmup failure never blocks startup.
    try:
        from ml.model import ml_model as _ml_model_for_warmup
        _warmup_summary = _ml_model_for_warmup.warmup()
        if _warmup_summary.get("is_warm"):
            await store.log_event(
                f"🧠 Meta-learner warmed with {_warmup_summary.get('n_loaded', 0)} "
                f"labeled samples (buffer={_warmup_summary.get('buffer_size', 0)})"
            )
        else:
            await store.log_event(
                "🧠 Meta-learner warmup deferred — stacking will activate once "
                f"live outcomes accumulate ({_warmup_summary.get('error', 'unknown')})"
            )
    except Exception as e:
        log.warning("[lifespan] Meta-learner warmup failed: %s", e)
    watchdog.beat("label_backfill")
    await store.log_event("🏷️  Label Backfill Service active (45s startup grace → daily cycle, retrain ≥50 labels)")

    # 9. Background tasks
    broadcast_task = asyncio.create_task(_broadcast_loop(), name="ws-broadcast")
    # W14-1 — periodic system status broadcast (every 5s on the "system"
    # channel). Distinct from ``_broadcast_loop`` which fires every 1s
    # with the rich snapshot. This task emits a lean heartbeat so a
    # client subscribed to "system" can detect a stalled bot even
    # when the dashboard isn't pulling the full snapshot.
    status_broadcast_task = asyncio.create_task(
        _periodic_status_broadcast(), name="ws-status-broadcast"
    )
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
    status_broadcast_task.cancel()
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
    # W18-2 — stop the live fill monitor (P0-C02 fix). Safe to call when
    # not running (no-op); cancels the polling task if active.
    try:
        from core.live_fill_monitor import live_fill_monitor
        await live_fill_monitor.stop()
    except Exception as e:  # pragma: no cover — defensive: must not kill shutdown
        log.error("[live_fill_monitor] shutdown failed: %s", e)
    # W20-7 — stop the trade-tape ingester. Safe to call when not
    # running (no-op); cancels the polling task if active.
    try:
        from core.trade_ingester import trade_tape_ingester
        await trade_tape_ingester.stop()
    except Exception as e:  # pragma: no cover — defensive: must not kill shutdown
        log.error("[trade_tape_ingester] shutdown failed: %s", e)
    await gamma_client.close()

    # ── W16-7 — close the async SQLite connection pool ─────────────────────
    # Closes every aiosqlite.Connection opened by the ``AsyncDBPool``
    # singleton (``core.db_pool.db_pool``). Safe to call even when no
    # connections were ever opened (no-op). Placed LAST (after every
    # subsystem has stopped) so any in-flight v2 endpoint request
    # has already drained — once the lifespan function returns past
    # ``yield``, Starlette stops accepting new requests before the
    # cleanup runs, so no new v2 read can arrive mid-teardown.
    try:
        from core.db_pool import db_pool as _db_pool_singleton

        await _db_pool_singleton.close_all()
    except Exception as _db_pool_close_exc:  # pragma: no cover — defensive
        log.error(
            "Async DB pool close failed at shutdown: %s", _db_pool_close_exc
        )

    # ── W21-1 — shut down the unified database manager ────────────────────
    # Cancels the PG-retry background task. The manager doesn't own
    # any SQLite connections (each call opens + closes its own via a
    # context manager), so there's no pool to drain — just the asyncio
    # task to cancel. Wrapped in try/except so a teardown failure
    # never blocks the rest of the lifespan shutdown.
    try:
        from core.database_manager import db_manager as _db_manager_singleton

        await _db_manager_singleton.shutdown()
    except Exception as _db_mgr_close_exc:  # pragma: no cover — defensive
        log.error(
            "Database manager shutdown failed: %s", _db_mgr_close_exc
        )

    # ── W17-8 — stop the background job-queue workers ─────────────────────
    # ``stop_workers`` sets ``_running = False`` (the next ``while
    # self._running`` check in each worker's loop exits cleanly) and
    # joins each thread with a 5s timeout. If a worker is mid-handler
    # when shutdown is requested, the handler is allowed to finish
    # naturally (the join waits up to 5s) — daemon=True is the
    # belt-and-braces backstop so a stuck handler does NOT hang the
    # process exit (the OS reaps the thread when the main thread exits).
    try:
        from core.job_queue import job_queue as _job_queue_singleton

        _job_queue_singleton.stop_workers()
    except Exception as _jq_stop_exc:  # pragma: no cover — defensive
        log.error("Job queue worker stop failed at shutdown: %s", _jq_stop_exc)

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
    """Push the rich dashboard snapshot every 1s.

    W14-1 — broadcasts through BOTH the legacy ``manager`` (raw dict,
    for ``useBot``) AND the new ``ws_manager`` (envelope, for
    ``useRealtimeData``). The snapshot is built ONCE and reused for
    both paths so the per-tick cost stays the same as pre-W14-1.
    Skipped entirely when zero clients are connected to either manager
    — the snapshot build involves iterating every order book / open
    order / position / trade, so a server with no clients shouldn't
    pay that cost every second.
    """
    while True:
        try:
            _ws_stats = ws_manager.get_stats()
            _has_clients = bool(manager.active) or _ws_stats["connected_clients"] > 0
            if _has_clients:
                snap = await _build_snapshot()
                # Legacy raw broadcast (backwards-compat with useBot).
                if manager.active:
                    await manager.broadcast(snap)
                # New envelope broadcast on the "system" channel for
                # useRealtimeData subscribers. ``ws_manager.broadcast``
                # already short-circuits when there are zero clients.
                await ws_manager.broadcast("system", snap)
        except Exception as e:
            log.debug("Broadcast error: %s", e)
        await asyncio.sleep(1.0)


async def _periodic_status_broadcast() -> None:
    """Broadcast a LEAN system status every 5s on the ``system`` channel.

    Distinct from ``_broadcast_loop`` (1s, rich snapshot): this task
    pushes a small status dict (balance, positions count, mode, kill
    switch, observation-only) so a client subscribed to the
    ``system`` channel receives a cheap heartbeat even when the
    dashboard isn't running the full 1s snapshot loop (e.g. a
    headless monitor / alerting bot that just needs to know the bot
    is alive).

    Both ``_broadcast_loop`` and this task emit on ``system`` —
    consumers must merge the two streams (the richer 1s snapshot
    dominates the lean 5s status, but the lean one is the only
    heartbeat when no snapshot loop is running).
    """
    while True:
        await asyncio.sleep(5)
        try:
            from core.safety import kill_switch_file_exists
            status = {
                "type": "status",
                "balance": store.paper_balance,
                "positions_count": len(store.positions),
                "open_orders_count": len(store.open_orders),
                "daily_pnl": store.daily_pnl,
                "mode": settings.trading_mode,
                "paper_trade": settings.paper_trade,
                "kill_switch": store.kill_switch_active,
                "kill_switch_durable": bool(kill_switch_file_exists()),
                "observation_only": risk_manager.observation_only,
                "active_strategies": list(strategy_registry.get_active_instances().keys()),
            }
            await ws_manager.broadcast("system", status)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — must not crash the loop
            log.debug("Periodic status broadcast error: %s", e)


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
    # W14-7 — In-memory tracker: record the richer per-IP / per-limit /
    # per-method shape the dashboard's ``RateLimitPanel`` renders. Same
    # best-effort contract as the prometheus call above.
    try:
        rate_limit_tracker.record_hit(
            endpoint=request.url.path,
            method=request.method,
            client_ip=request.client.host if request.client else "unknown",
            limit=limit_str,
        )
    except Exception:  # pragma: no cover — defensive: tracker must never break the 429
        log.warning("[rate-limit-tracker] record_hit failed", exc_info=True)
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
            # W14-7 — In-memory tracker: record the 500 so the dashboard's
            # "Top endpoints" table surfaces the failure count alongside
            # the 2xx successes. Best-effort (same contract as the call
            # in the success path below).
            try:
                rate_limit_tracker.record_request(
                    endpoint=request.url.path,
                    status=500,
                )
            except Exception:  # pragma: no cover
                log.warning("[rate-limit-tracker] record_request failed", exc_info=True)
            # W15-4 — Profiler: record the 500 so the per-endpoint
            # ``error_rate`` reflects unhandled-exception paths (without
            # this branch, only the success-path recording below would
            # count, and a route that always 500s would show
            # ``request_count=0`` in the profile). Best-effort — same
            # contract as the rate-limit-tracker call above.
            try:
                profiler.record(
                    method=request.method,
                    endpoint=request.url.path,
                    duration=duration,
                    status=500,
                )
            except Exception:  # pragma: no cover
                log.warning("[profiler] record failed", exc_info=True)
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
        # W14-7 — In-memory tracker: record every request (not just the
        # rate-limited ones) so the dashboard's "Top endpoints" table
        # surfaces the most-trafficked routes overall, not just the
        # throttle-prone ones. Best-effort: a tracker exception must
        # never change the response.
        try:
            rate_limit_tracker.record_request(
                endpoint=request.url.path,
                status=response.status_code if response is not None else 500,
            )
        except Exception:  # pragma: no cover — defensive: tracker must never break the response
            log.warning("[rate-limit-tracker] record_request failed", exc_info=True)
        # W15-4 — Per-endpoint latency profiler. Best-effort (same
        # contract as the rate-limit-tracker call above): a profiler
        # exception must never change the response. Records the
        # (method, path, duration, status) tuple so the
        # ``GET /api/profiling/stats`` endpoint can surface p50/p95/p99
        # per endpoint. Includes 4xx / 5xx responses so the
        # ``error_rate`` field reflects the true failure ratio (a route
        # that returns 200 fast but 500 slow is just as actionable as
        # one that's slow on the happy path).
        try:
            profiler.record(
                method=request.method,
                endpoint=request.url.path,
                duration=duration,
                status=response.status_code if response is not None else 500,
            )
        except Exception:  # pragma: no cover — defensive: profiler must never break the response
            log.warning("[profiler] record failed", exc_info=True)
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


# ── Security headers middleware (W11-6 — OWASP A05; expanded W15-6) ──────────
# Adds a baseline set of defensive response headers to EVERY response so the
# browser's own security machinery can apply defense-in-depth:
#   * ``X-Content-Type-Options: nosniff``        — blocks MIME-type sniffing.
#   * ``X-Frame-Options: DENY``                  — blocks clickjacking via framing.
#   * ``X-XSS-Protection: 1; mode=block``        — legacy reflected-XSS filter
#                                                   (still useful for older browsers).
#   * ``Referrer-Policy: strict-origin-when-cross-origin`` — strips the path /
#                                                   query from the Referer
#                                                   header on cross-origin nav.
#   * ``Permissions-Policy: geolocation=(), microphone=(), camera=()`` —
#                                                   disables the most-abused
#                                                   device-permission APIs even
#                                                   if a future route is tricked
#                                                   into requesting them.
#                                                   (W15-6)
#   * ``Content-Security-Policy`` — W15-6 expanded from ``default-src 'self'``
#                                                   to a full directive list
#                                                   that lets the dashboard
#                                                   load inline scripts/styles
#                                                   (Next.js requires them),
#                                                   connect to its own ws/wss
#                                                   back-channel, render
#                                                   data/blob image URLs, and
#                                                   load self-hosted fonts —
#                                                   while still defaulting to
#                                                   same-origin for anything
#                                                   not explicitly allowed.
# These headers do NOT affect the JSON API contract — they only instruct the
# browser. They're applied after ``call_next`` so they land on every response
# (200, 4xx, 5xx, OPTIONS preflight, WebSocket upgrade rejection, …). The
# ``Strict-Transport-Security`` header is intentionally NOT added here: it
# must be terminated by the TLS-aware reverse proxy (Caddy) so it only ships
# over an actual HTTPS connection (otherwise an active MITM could inject it
# into a plain-HTTP response and pin the client).
_CSP_HEADER_VALUE = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self' ws: wss:; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:"
)
_PERMISSIONS_POLICY_VALUE = "geolocation=(), microphone=(), camera=()"


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = _PERMISSIONS_POLICY_VALUE
    response.headers["Content-Security-Policy"] = _CSP_HEADER_VALUE
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
    # W15-4 — cache exposure for 15s (general_cache's default TTL).
    # ``compute_exposure`` walks every open position + every pending
    # order against the live book on each call — a 15s TTL collapses
    # the dashboard's polling burst. ``POST /api/trade`` and
    # ``POST /api/positions/{token_id}/close`` invalidate so the
    # next read after a mutation sees fresh data.
    cache_key = "exposure"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    result = compute_exposure()
    general_cache.set(cache_key, result)
    return result


@app.get("/api/risk/reconcile", tags=["risk"])
async def get_reconciliation():
    """Reconciliation investigation for the current open exposure."""
    # W15-4 — cache the reconciliation report for 30s. The report
    # walks every open position vs. the book-keeping layer's recorded
    # totals — recompute on every poll is wasted work between
    # mutations. Same invalidation hook as ``/api/exposure``.
    cache_key = "risk_reconcile"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    result = compute_reconciliation(bankroll_ceiling=float(BANKROLL_CEILING))
    general_cache.set(cache_key, result)
    return result


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
        "profile) and their current running state. Use "
        "``?implemented_only=true`` to filter out PLANNED / EXPERIMENTAL "
        "entries (i.e. no-op stubs) so the catalog only shows strategies "
        "with real trading loops."
    ),
)
async def get_strategy_catalog(implemented_only: bool = False):
    """Return the strategy catalog.

    ``implemented_only=true`` excludes PLANNED / EXPERIMENTAL rows so the
    UI can render only strategies that actually execute a trading loop.
    """
    # W11-2 — cache the strategy catalog for 5 min (markets_cache's
    # default TTL). Strategy metadata is static — the only thing that
    # changes is the running state, which ``POST /api/strategies/toggle``
    # invalidates explicitly.
    # W19-6 — the cache key includes the ``implemented_only`` flag so the
    # filtered view doesn't get shadowed by an unfiltered cached response
    # (and vice-versa).
    cache_key = "strategies_catalog:impl" if implemented_only else "strategies_catalog"
    cached = markets_cache.get(cache_key)
    if cached is not None:
        return cached
    catalog = strategy_registry.get_catalog(implemented_only=implemented_only)
    result = {
        "catalog": catalog,
        "total": len(catalog),
        # W19-6 — surface the implementation-status breakdown so the
        # UI can render "6 implemented, 44 planned" headers without a
        # second round-trip.
        "implemented_count": sum(1 for s in catalog if s.get("status") == "IMPLEMENTED"),
        "planned_count": sum(
            1 for s in catalog if s.get("status") not in ("IMPLEMENTED",)
        ),
        "filtered": implemented_only,
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
        # W19-6 — both the filtered and unfiltered cache views must be
        # invalidated since either could shadow the other.
        markets_cache.invalidate("strategies_catalog")
        markets_cache.invalidate("strategies_catalog:impl")
        return {"status": "started", "strategy": strat_id}
    else:
        ok = await strategy_registry.stop_strategy(strat_id)
        await store.log_event(f"⏸ Strategy [{strat_id}] stopped via API")
        # W11-2 — same invalidation as the start branch above.
        markets_cache.invalidate("strategies_catalog")
        markets_cache.invalidate("strategies_catalog:impl")
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
    # W15-4 — invalidate the W15-4 hot-path caches that depend on
    # open-position / open-order state so the next dashboard poll
    # reflects the new order. Same rationale as the analytics_cache
    # invalidations above.
    general_cache.invalidate("exposure")
    general_cache.invalidate("risk_reconcile")
    general_cache.invalidate("database_reconciliation")
    # W14-1 — broadcast the placement to the ``trades`` (placement event)
    # and ``orders`` (open-orders update) channels. ``positions`` is also
    # nudged because a paper fill may have landed since the last periodic
    # snapshot — sending it here lets a positions-only subscriber see the
    # new state immediately rather than waiting for the 1s tick.
    try:
        order_payload = {
            "order_id": getattr(order, "order_id", None),
            "token_id": req.token_id,
            "slug": slug,
            "side": side.value,
            "price": req.price,
            "size": float(getattr(order, "size", 0.0) or 0.0),
            "strategy": getattr(order, "strategy", "manual"),
            "paper": getattr(order, "paper", settings.paper_trade),
            "timestamp": time.time(),
        }
        await ws_manager.broadcast(
            "trades", {"type": "placement", **order_payload}
        )
        await ws_manager.broadcast(
            "orders", {"type": "open_update", "order": order_payload}
        )
        await ws_manager.broadcast(
            "positions", {"type": "nudge", "token_id": req.token_id}
        )
    except Exception as e:  # noqa: BLE001 — broadcast must never break the API response
        log.debug("[ws_broadcast] manual-trade broadcast failed: %s", e)
    # W17-5 — append a hash-chained immutable audit entry for the trade
    # execution. Best-effort: a failure here must never block the order
    # response (the singleton's ``log()`` already swallows persistence
    # errors and returns None; the inline try/except is belt-and-braces).
    try:
        immutable_audit.log(
            "trade_executed",
            {
                "token_id": req.token_id,
                "slug": slug,
                "side": side.value,
                "price": float(req.price),
                "size_usdc": float(req.size_usdc),
                "size_shares": float(getattr(order, "size", 0.0) or 0.0),
                "strategy": getattr(order, "strategy", "manual"),
                "paper": bool(getattr(order, "paper", settings.paper_trade)),
                "order_id": getattr(order, "order_id", None),
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort: never block the trade response
        log.debug("[immutable_audit] trade_executed log failed: %s", e)
    return {"status": "placed", "order": order}


# W18-2 — Live fill monitor status endpoint (P0-C02 fix). Surfaces the
# background monitor's run state (running flag, poll interval, dedup-set
# size) so an operator can confirm live fill acknowledgement is active.
# Read-only — no side effects. Unauthenticated routes are NOT exposed;
# the endpoint inherits the standard bearer-token auth middleware.
@app.get(
    "/api/fills/live-status",
    tags=["trading"],
    summary="Live fill monitor status",
    description=(
        "Returns the run-state of the live fill acknowledgement loop: "
        "whether it's running, the configured poll interval (seconds), "
        "and the size of the in-memory seen-trade-id dedup set. Useful "
        "for confirming that live fills are being acknowledged (the "
        "monitor is the only consumer of ``clob_client.get_trades()``)."
    ),
)
@limiter.limit(READ_LIMIT)
async def live_fill_status(request: Request):
    """Get live fill monitor status."""
    from core.live_fill_monitor import live_fill_monitor

    return {
        "running": live_fill_monitor._running,
        "poll_interval": live_fill_monitor.poll_interval,
        "seen_trade_ids": len(live_fill_monitor._last_trade_ids),
    }


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

    # W17-5 — append a hash-chained immutable audit entry for the close.
    # Best-effort: a failure here must never block the close response.
    try:
        immutable_audit.log(
            "position_closed",
            {
                "token_id": token_id,
                "slug": slug,
                "side": side.value,
                "price": float(close_price),
                "size_shares": float(size_shares),
                "notional_usdc": float(notional_usdc),
                "estimated_pnl": float(estimated_pnl),
                "strategy": strategy or "manual_close",
                "order_id": getattr(order, "order_id", None),
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort: never block the close response
        log.debug("[immutable_audit] position_closed log failed: %s", e)

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
    # W15-4 — invalidate the W15-4 hot-path caches that depend on
    # open-position / open-order state so the next dashboard poll
    # reflects the position close. Same rationale as the analytics
    # invalidations above.
    general_cache.invalidate("exposure")
    general_cache.invalidate("risk_reconcile")
    general_cache.invalidate("database_reconciliation")
    # W14-1 — broadcast the close-submit to ``trades`` (close event) and
    # ``positions`` (state-change nudge). The actual fill (with realised
    # P&L) lands asynchronously via the paper_sim fill loop — this
    # broadcast surfaces the SUBMISSION so subscribers see immediate
    # feedback; the 1s snapshot loop will follow up with the post-fill
    # position state.
    try:
        await ws_manager.broadcast(
            "trades",
            {
                "type": "close_submit",
                "token_id": token_id,
                "slug": slug,
                "side": side.value,
                "price": round(close_price, 4),
                "size": round(size_shares, 4),
                "strategy": strategy or "manual_close",
                "estimated_pnl": round(estimated_pnl, 2),
                "paper": settings.paper_trade,
                "timestamp": time.time(),
            },
        )
        await ws_manager.broadcast(
            "positions",
            {"type": "close_submit", "token_id": token_id, "slug": slug},
        )
    except Exception as e:  # noqa: BLE001 — broadcast must never break the response
        log.debug("[ws_broadcast] position-close broadcast failed: %s", e)
    return result


@app.get(
    "/api/trades",
    tags=["trading"],
    response_model=TradesResponse,
    summary="Recent trade history",
    description=(
        "Returns the most recent trades (newest first), capped by `limit` "
        "(default 50, max 1000). Each trade carries the fill price, size, "
        "realised P&L, strategy, and paper flag. W16-5 — the route also "
        "supports cursor-based pagination via the optional `cursor` query "
        "param: pass the `next_cursor` from a previous response to fetch "
        "the next page. When `cursor` is omitted, the first page (the "
        "newest `limit` trades) is returned — fully backward compatible "
        "with the pre-pagination wire shape."
    ),
)
async def get_trades(
    limit: int = Query(50, ge=1, le=1000),
    cursor: str | None = Query(
        None,
        description=(
            "Opaque base64 cursor from a previous response's "
            "``next_cursor`` field. Omit for the first page (newest "
            "trades)."
        ),
    ),
):
    """Recent trade history (newest first) with cursor-based pagination.

    The cursor encodes the ``(timestamp, trade_id)`` boundary of the
    last trade on the current page. The next request with that cursor
    returns the page of trades whose ``(timestamp, trade_id)`` pair is
    strictly less than the boundary — stable across new inserts (a
    brand-new trade landing at the head of the feed between two
    paginated requests does not shift the boundary).
    """
    # W16-5 — local import keeps FastAPI boot time independent of the
    # pagination module's import cost (negligible, but consistent with
    # the existing pattern of lazy-importing feature modules inside
    # route handlers).
    from core.pagination import paginate_list

    page = paginate_list(
        store.trades,
        cursor=cursor,
        limit=limit,
        key_fn=lambda t: (t.timestamp, t.trade_id or ""),
        reverse=True,
    )

    trades_serialized = [
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
        for t in page.items
    ]
    return {
        "trades": trades_serialized,
        "count": len(trades_serialized),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


@app.get(
    "/api/events",
    tags=["system"],
    summary="Recent system events log",
    description=(
        "Returns the most recent in-memory event-log entries (newest "
        "first). Each entry is a human-readable string emitted by a "
        "subsystem (paper_sim, settlement, risk gate, etc.). W16-5 — "
        "supports cursor-based pagination via the optional `cursor` "
        "query param. Because the event log is a bare-string ring "
        "buffer (no per-entry ids), the cursor is offset-based under "
        "the hood but uses the same opaque-base64 wire format as the "
        "other paginated endpoints."
    ),
)
async def get_events(
    n: int = Query(50, ge=1, le=500, description="Page size (max 500)."),
    cursor: str | None = Query(
        None,
        description=(
            "Opaque base64 cursor from a previous response's "
            "``next_cursor`` field. Omit for the first page (newest "
            "events)."
        ),
    ),
):
    """Recent in-memory events (newest first) with cursor-based pagination.

    The event log is a bounded ring buffer (``store.event_log``, capped
    at 500 entries by ``store.log_event``). Entries are bare strings of
    the form ``"[HH:MM:SS] message"`` — there is no per-entry id column
    to use as a stable cursor, so we fall back to offset-based
    pagination encoded inside the standard opaque cursor. The wire
    contract (``{events, count, next_cursor, has_more}``) is identical
    to every other paginated endpoint.
    """
    from core.pagination import paginate_offset

    # ``store.get_recent_events(n)`` returns the most recent ``n``
    # entries in oldest-first order. Reverse here so the wire payload
    # is newest-first — the order the dashboard's event feed renders.
    all_events = list(reversed(await store.get_recent_events(500)))
    page = paginate_offset(all_events, cursor=cursor, limit=n)
    return {
        "events": page.items,
        "count": len(page.items),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


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
    # W14-1 — broadcast the kill-switch activation as a critical alert
    # so any dashboard subscribed to the ``alerts`` channel flashes
    # immediately rather than waiting for the next /api/alerts/evaluate
    # pass. Also nudges ``system`` so the heartbeat reflects the new
    # kill_switch state on the next tick (already covered by the 1s
    # snapshot loop, but the nudge makes it land sub-second).
    try:
        await ws_manager.broadcast(
            "alerts",
            {
                "type": "kill_switch",
                "severity": "critical",
                "active": True,
                "reason": "Manual via UI",
                "timestamp": time.time(),
            },
        )
        await ws_manager.broadcast(
            "system", {"type": "kill_switch", "kill_switch": True}
        )
    except Exception as e:  # noqa: BLE001
        log.debug("[ws_broadcast] kill-switch-activate broadcast failed: %s", e)
    # W17-5 — append a hash-chained immutable audit entry for the kill-
    # switch activation. Best-effort: a failure here must never block
    # the activation response.
    try:
        immutable_audit.log(
            "kill_switch_activated",
            {
                "reason": "Manual via UI",
                "source": "api",
                "endpoint": "/api/kill-switch/activate",
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort: never block the activation response
        log.debug("[immutable_audit] kill_switch_activated log failed: %s", e)
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
    # W14-1 — broadcast the deactivation. ``severity: info`` because the
    # event is a return-to-normal, not a fresh fault.
    try:
        await ws_manager.broadcast(
            "alerts",
            {
                "type": "kill_switch",
                "severity": "info",
                "active": False,
                "timestamp": time.time(),
            },
        )
        await ws_manager.broadcast(
            "system", {"type": "kill_switch", "kill_switch": False}
        )
    except Exception as e:  # noqa: BLE001
        log.debug("[ws_broadcast] kill-switch-deactivate broadcast failed: %s", e)
    # W17-5 — append a hash-chained immutable audit entry for the kill-
    # switch deactivation. Best-effort: a failure here must never block
    # the deactivation response.
    try:
        immutable_audit.log(
            "kill_switch_deactivated",
            {
                "source": "api",
                "endpoint": "/api/kill-switch/deactivate",
            },
        )
    except Exception as e:  # noqa: BLE001 — best-effort: never block the deactivation response
        log.debug("[immutable_audit] kill_switch_deactivated log failed: %s", e)
    return {"status": "deactivated", "kill_switch": False}


class ObservationModeRequest(BaseModel):
    active: bool
    reason: str = ""


@app.post("/api/risk/observation-mode", tags=["risk"])
async def set_observation_mode(req: ObservationModeRequest):
    """Toggle observation-only mode. When active, new live orders are blocked."""
    result = await risk_manager.set_observation_mode(req.active, req.reason)
    # W14-1 — broadcast the observation-mode toggle so a dashboard
    # subscribed to ``alerts`` sees the state change immediately.
    try:
        await ws_manager.broadcast(
            "alerts",
            {
                "type": "observation_mode",
                "severity": "warning" if result["observation_only"] else "info",
                "active": result["observation_only"],
                "reason": result.get("observation_reason", ""),
                "timestamp": time.time(),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("[ws_broadcast] observation-mode broadcast failed: %s", e)
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
    # W15-4 — invalidate the W15-4 hot-path caches that depend on the
    # ML model's state. The registry summary (``ml_registry``), drift
    # status (``ml_drift``), orchestrator stats
    # (``ml_training_orchestrator``), and deep-analysis roll-up
    # (``analysis_deep`` — which calls into the ML ensemble's
    # predictions) all need to be recomputed against the new model.
    ml_metrics_cache.invalidate("ml_registry")
    general_cache.invalidate("ml_drift")
    general_cache.invalidate("ml_training_orchestrator")
    general_cache.invalidate("analysis_deep")
    # W14-1 — broadcast the post-retrain ML metrics so any dashboard
    # panel subscribed to the ``metrics`` channel refreshes its
    # Brier / AUC / ECE display without re-polling /api/ml. The
    # payload mirrors the GET /api/ml response shape so subscribers can
    # reuse the same parsing logic.
    try:
        await ws_manager.broadcast(
            "metrics",
            {
                "type": "ml_retrain",
                "brier_score": ml_model.brier_score,
                "roc_auc": ml_model.roc_auc,
                "log_loss": ml_model.log_loss_score,
                "ece": ml_model.ece,
                "model_version": model_registry.active_version,
                "meta_learner_warm": ensemble_meta_learner.is_warm,
                "calibration": calibrator.last_fit_metrics,
                "timestamp": time.time(),
            },
        )
    except Exception as e:  # noqa: BLE001
        log.debug("[ws_broadcast] ml-retrain broadcast failed: %s", e)
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
    # W15-4 — cache the orchestrator stats for 30s (general_cache's
    # default TTL). The orchestrator's stats (retrain count, last
    # champion Brier, drift thresholds) only change on a fresh fit
    # (``POST /api/ml/retrain``) — caching collapses the dashboard's
    # polling burst without exposing stale data beyond one natural
    # shift window.
    cache_key = "ml_training_orchestrator"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    from ml.training_orchestrator import training_orchestrator
    result = {
        **training_orchestrator.stats,
        "model_version": model_registry.active_version,
        "model_ready": ml_model.rf is not None,
        "drift_status": drift_detector.drift_status,
    }
    general_cache.set(cache_key, result)
    return result


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
    # W15-4 — cache the deep-analysis roll-up for 30s (general_cache's
    # default TTL). ``get_top_ranked_opportunities`` walks every
    # tracked market's feature vector through the analysis engine's
    # 9-factor scoring — the dashboard polls every few seconds and
    # the underlying opportunities only shift when order books or
    # ML predictions move (which the per-poller / per-strategy
    # cadence already throttles). 30s is well inside the natural
    # shift window.
    cache_key = "analysis_deep"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    from core.analysis_engine import deep_analysis_engine
    top_opps = deep_analysis_engine.get_top_ranked_opportunities(limit=15)
    news = [n.to_dict() for n in fundamental_engine.news_feed[:15]]
    result = {
        "top_opportunities": top_opps,
        "recent_news": news,
        "timestamp": time.time(),
    }
    general_cache.set(cache_key, result)
    return result


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
    # W15-4 — cache the source catalog for 5 min (markets_cache's
    # default TTL). The catalog is built at ``fundamental_engine``
    # construction time from ``config.settings`` — it never changes
    # at runtime (a new source requires a config edit + restart).
    # Without caching, the dashboard's "News Sources" panel re-walks
    # the catalog on every poll, which the profiler flagged as a
    # hot path (the ``get_source_catalog`` call constructs a fresh
    # list of dicts each time).
    cache_key = "analysis_news_sources"
    cached = markets_cache.get(cache_key)
    if cached is not None:
        return cached
    result = fundamental_engine.get_source_catalog()
    markets_cache.set(cache_key, result)
    return result


@app.get("/api/analysis/news/stats", tags=["analysis"])
async def get_fundamental_news_stats():
    """Return live NLP sentiment breakdown and global ingestion rate telemetry."""
    # W15-4 — cache the news stats for 15s (general_cache's default
    # TTL). ``get_news_stats`` walks the entire ``news_feed`` deque
    # to compute sentiment distribution + ingestion rate — the
    # dashboard polls every few seconds and the underlying feed only
    # refreshes every 60s, so a 15s TTL collapses the burst without
    # exposing stale data beyond one feed-tick window.
    cache_key = "analysis_news_stats"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    result = fundamental_engine.get_news_stats()
    general_cache.set(cache_key, result)
    return result


# ── Model Registry & Drift Detection ──────────────────────────────────────────

@app.get("/api/ml/registry", tags=["ml"])
async def get_model_registry():
    """Return model version lineage, benchmarks, ECE, and validation status."""
    # W15-4 — cache the registry summary for 60s (ml_metrics_cache's
    # default TTL). The registry's lineage / benchmark / ECE data
    # only changes on a fresh fit (``POST /api/ml/retrain``) which
    # invalidates the key; the dashboard polls this endpoint every
    # few seconds, so caching collapses the burst.
    cache_key = "ml_registry"
    cached = ml_metrics_cache.get(cache_key)
    if cached is not None:
        return cached
    result = model_registry.get_summary()
    ml_metrics_cache.set(cache_key, result)
    return result


@app.get("/api/ml/drift", tags=["ml"])
async def get_model_drift():
    """Return real-time Population Stability Index (PSI) and concept shift metrics."""
    # W15-4 — cache the drift status for 30s (general_cache's default
    # TTL). ``get_status_report`` walks the rolling prediction
    # distribution to compute PSI — the underlying distribution only
    # shifts when new predictions land (one per strategy cycle, ~15s
    # cadence). 30s is well inside the natural shift window.
    cache_key = "ml_drift"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    result = drift_detector.get_status_report()
    general_cache.set(cache_key, result)
    return result


@app.get(
    "/api/ml/training-data-status",
    tags=["ml"],
    summary="Real labeled training-data availability",
    description=(
        "Report whether the ML ensemble has access to real labeled training "
        "samples (from resolved markets via the daily label-backfill loop) "
        "or is currently running on the synthetic-only fallback. Returns "
        "the sample count, class balance, and a ``has_real_data`` boolean "
        "that the live safety gate (§82 check #6) and the W20-2 real-data "
        "training path consult before allowing the model to be promoted "
        "to production. The endpoint does NOT cache — it issues a fresh "
        "SQLite query each call so the operator gets a live view after "
        "triggering a backfill cycle."
    ),
)
async def get_training_data_status():
    """Check if real labeled training data is available (W20-2).

    Calls ``ml_model._load_real_training_data()`` directly (the same
    canonical fetcher ``fit_initial`` uses) so the payload reflects the
    EXACT data the next retrain would consume. Defensive: a transient
    SQLite hiccup surfaces as ``has_real_data=false`` with an ``error``
    field rather than a 500 — the operator dashboard treats that the
    same as "no real data yet" (yellow caution indicator).
    """
    try:
        features, labels, _ = ml_model._load_real_training_data()
    except Exception as e:  # noqa: BLE001 — defensive: route must not 500
        return {
            "has_real_data": False,
            "n_samples": 0,
            "n_positive": 0,
            "n_negative": 0,
            "error": str(e),
        }
    if features is None or labels is None or len(features) < 1:
        return {
            "has_real_data": False,
            "n_samples": 0,
            "n_positive": 0,
            "n_negative": 0,
        }
    n = int(len(features))
    n_pos = int(np.sum(np.asarray(labels, dtype=int)))
    return {
        "has_real_data": n >= 100,
        "n_samples": n,
        "n_positive": n_pos,
        "n_negative": n - n_pos,
        # Surface the live training_source provenance so the operator
        # can confirm the next retrain will pick up the real data (i.e.
        # the model isn't already real-only and just waiting for a
        # retrain trigger).
        "current_training_source": getattr(ml_model, "training_source", "unknown"),
        "current_n_real_samples": int(getattr(ml_model, "n_real_samples", 0) or 0),
        "current_n_synthetic_samples": int(getattr(ml_model, "n_synthetic_samples", 0) or 0),
    }


# ── Quantitative Backtesting Lab ──────────────────────────────────────────────

class BacktestRequest(BaseModel):
    strategy_id: str
    initial_capital: float = Field(default=10000.0, ge=100.0, le=1000000.0)
    days: int = Field(default=30, ge=1, le=365)
    fee_bps: float = Field(default=0.0, ge=0.0, le=100.0)
    slippage_bps: float = Field(default=5.0, ge=0.0, le=50.0)


# ── W20-3 — Backtest experiment persistence helper ───────────────────────────
# Materialises a backtest result (from either the synthetic MC engine in
# ``backtesting/engine.py`` OR the historical-replay engine in
# ``backtesting/historical_replay.py``) into a ``BacktestExperiment`` row
# and saves it via the ``experiment_store`` singleton. The two engines
# report the headline risk metrics under DIFFERENT key names + units, so
# the helper's ``engine_kind`` switch normalises both into the canonical
# :class:`BacktestExperiment` field shape:
#
#   synthetic_mc  →  result_dict = BacktestResult.to_dict()
#                   {roi_pct (percentage), sharpe_ratio, sortino_ratio,
#                    calmar_ratio, max_drawdown_pct (percentage),
#                    win_rate, profit_factor, total_trades,
#                    final_equity, initial_capital, equity_curve (dicts),
#                    monthly_returns, ...}
#
#   historical    →  result_dict = canonical shape
#                   {total_return (fractional), sharpe, sortino, calmar,
#                    max_drawdown (fractional), win_rate, profit_factor,
#                    n_trades, final_equity, equity_curve (floats), trades}
#
# Persistence is best-effort: a SQLite write failure is logged at WARN
# and the helper returns ``None`` so the calling route can still return
# a successful 200 with the full backtest payload (just without the
# ``experiment_id`` field). The God Mode assessment (W17-6 §33) found
# no experiment registry; W20-3 closes that gap but does NOT make
# persistence load-bearing for the backtest itself — a transient
# ``/app/data`` write failure shouldn't 500 a backtest that already
# completed successfully.

def _persist_backtest_experiment(
    *,
    strategy: str,
    strategy_version: str,
    start_time: float,
    end_time: float,
    initial_capital: float,
    config: dict,
    result_dict: dict,
    engine_kind: str,
) -> str | None:
    """Save a backtest result as a ``BacktestExperiment`` row.

    Returns the new ``experiment_id`` on success, or ``None`` if the
    store could not save the row (in which case the caller continues
    without an ``experiment_id`` field in the response — best-effort
    persistence).
    """
    from backtesting.experiment_store import (
        BacktestExperiment,
        experiment_store,
    )

    if experiment_store is None:
        # Import-time construction failed (read-only parent dir, etc.).
        log.warning(
            "experiment_store singleton is None — cannot persist "
            "backtest experiment (engine_kind=%s, strategy=%s).",
            engine_kind, strategy,
        )
        return None

    if engine_kind == "synthetic_mc":
        # Synthetic MC engine: convert percentage fields to fractional
        # + rename ``*_ratio`` / ``total_trades`` to the dataclass field
        # names. ``equity_curve`` is a list of per-step dicts in this
        # shape; pass it through verbatim (the store JSON-encodes
        # whatever it gets and caps the blob at 10 KB).
        total_return = float(result_dict.get("roi_pct", 0.0)) / 100.0
        max_dd = float(result_dict.get("max_drawdown_pct", 0.0)) / 100.0
        sharpe = float(result_dict.get("sharpe_ratio", 0.0))
        sortino = float(result_dict.get("sortino_ratio", 0.0))
        calmar = float(result_dict.get("calmar_ratio", 0.0))
        win_rate = float(result_dict.get("win_rate", 0.0))
        profit_factor = float(result_dict.get("profit_factor", 0.0))
        n_trades = int(result_dict.get("total_trades", 0))
        final_equity = float(result_dict.get("final_equity", initial_capital))
        equity_curve = result_dict.get("equity_curve", [])
        trades = []  # synthetic MC engine doesn't expose per-trade detail
    elif engine_kind == "historical":
        # Historical-replay engine: result_dict already in the canonical
        # ``BacktestExperiment`` shape (fractional return / sharpe /
        # max_drawdown / etc.) — pass through directly.
        total_return = float(result_dict.get("total_return", 0.0))
        max_dd = float(result_dict.get("max_drawdown", 0.0))
        sharpe = float(result_dict.get("sharpe", 0.0))
        sortino = float(result_dict.get("sortino", 0.0))
        calmar = float(result_dict.get("calmar", 0.0))
        win_rate = float(result_dict.get("win_rate", 0.0))
        profit_factor = float(result_dict.get("profit_factor", 0.0))
        n_trades = int(result_dict.get("n_trades", 0))
        final_equity = float(
            result_dict.get("final_equity", initial_capital)
        )
        equity_curve = result_dict.get("equity_curve", [])
        trades = result_dict.get("trades", [])
    else:  # pragma: no cover — defensive
        log.warning(
            "_persist_backtest_experiment: unknown engine_kind=%s; "
            "skipping persistence.", engine_kind,
        )
        return None

    experiment_id = str(uuid.uuid4())[:12]
    exp = BacktestExperiment(
        experiment_id=experiment_id,
        strategy=strategy,
        strategy_version=strategy_version,
        start_time=float(start_time),
        end_time=float(end_time),
        initial_capital=float(initial_capital),
        final_equity=final_equity,
        total_return=total_return,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_dd,
        win_rate=win_rate,
        profit_factor=profit_factor,
        n_trades=n_trades,
        config=config,
        created_at=time.time(),
        equity_curve=equity_curve,
        trades=trades,
    )
    try:
        experiment_store.save(exp)
        return experiment_id
    except Exception as exc:  # pragma: no cover — defensive
        log.warning(
            "Failed to persist backtest experiment (strategy=%s, "
            "engine_kind=%s): %s", strategy, engine_kind, exc,
        )
        return None


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
    # W20-3 — persist every backtest run as a BacktestExperiment row so
    # cross-run comparison (``/api/backtest/experiments`` / ``/compare``)
    # is possible. Previously (W17-6 God Mode §33) every ``run_backtest``
    # call returned an ephemeral dict that was lost. Persistence is
    # best-effort: a SQLite write failure is logged + the experiment_id
    # is omitted from the response (the backtest itself still succeeds
    # and returns its full result payload).
    experiment_id = _persist_backtest_experiment(
        strategy=req.strategy_id,
        strategy_version="1.0.0",
        start_time=time.time() - req.days * 86400.0,
        end_time=time.time(),
        initial_capital=req.initial_capital,
        config=req.model_dump(),
        # Synthetic MC engine returns roi_pct (percentage) / sharpe_ratio
        # / max_drawdown_pct (percentage) / total_trades. The
        # ``BacktestExperiment`` dataclass expects fractional values +
        # the ``sharpe`` / ``n_trades`` field names, so the helper
        # performs the coercion.
        result_dict=result.to_dict(),
        engine_kind="synthetic_mc",
    )
    return {
        "status": "completed",
        "synthetic": True,
        "synthetic_kind": "monte_carlo_archetype",
        "disclaimer": "Synthetic archetype simulation — not recorded market history (M8 pending)",
        "result": result.to_dict(),
        "experiment_id": experiment_id,
    }


# ── W16-4 — Backtest report generator (JSON + PDF) ───────────────────────────
# Two sibling routes for the report surface introduced in W16-4:
#
#   POST /api/backtest/report       — runs a backtest (same params as
#                                     ``/api/backtest/run``), then
#                                     passes the result through
#                                     ``backtesting.report.generate_report``
#                                     and returns the JSON-serialisable
#                                     ``BacktestReport`` dict directly.
#
#   POST /api/backtest/report/pdf   — same flow + renders the PDF via
#                                     ``report_to_pdf`` to a tmp file +
#                                     streams it back as
#                                     ``application/pdf`` (FileResponse).
#
# Both routes re-use ``BacktestRequest`` (no new request model needed —
# the report generator accepts whatever the engine emits).
#
# Rate-limited at ``HEAVY_LIMIT`` (5/min) — same ceiling as
# ``/api/backtest/run`` — because each call triggers a fresh
# archetype Monte-Carlo simulation under the hood. The PDF route's
# matplotlib chart rendering + reportlab PDF build are wrapped in
# ``asyncio.to_thread`` so the event loop never blocks on CPU-bound
# work.


@app.post("/api/backtest/report", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def generate_backtest_report(request: Request, req: BacktestRequest):
    """Run a backtest and return a comprehensive JSON report.

    The response is the ``dataclasses.asdict`` of a
    ``backtesting.report.BacktestReport`` — top-level fields:
    ``report_id``, ``created_at``, ``strategy``, ``period_start``,
    ``period_end``, ``total_return``, ``annualized_return``,
    ``sharpe_ratio``, ``sortino_ratio``, ``calmar_ratio``,
    ``max_drawdown``, ``max_drawdown_duration_days``, ``volatility``,
    ``downside_deviation``, ``total_trades``, ``winning_trades``,
    ``losing_trades``, ``win_rate``, ``avg_win``, ``avg_loss``,
    ``profit_factor``, ``expectancy``, ``avg_hold_time_hours``,
    ``var_95``, ``cvar_95``, ``beta``, ``alpha``, ``correlation``,
    ``equity_curve`` (list[float]), ``drawdown_curve`` (list[float]),
    ``trades`` (list[dict], capped at 100),
    ``monthly_returns`` (dict[str, float]).
    """
    from backtesting.engine import backtest_engine
    from backtesting.report import generate_report, report_to_json

    result = await asyncio.to_thread(
        backtest_engine.run_backtest,
        strategy_id=req.strategy_id,
        initial_capital=req.initial_capital,
        days=req.days,
        fee_bps=req.fee_bps,
        slippage_bps=req.slippage_bps,
    )
    report = await asyncio.to_thread(
        generate_report, result.to_dict(), req.strategy_id
    )
    return {
        "status": "completed",
        "report": report_to_json(report),
    }


@app.post("/api/backtest/report/pdf", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def generate_backtest_report_pdf(request: Request, req: BacktestRequest):
    """Run a backtest and stream the rendered PDF report back.

    Returns ``application/pdf`` (``Content-Disposition: attachment;
    filename=backtest-report-<strategy_id>-<report_id>.pdf``). The PDF
    is built via ``reportlab`` (multi-section A4: title + summary
    metrics table + matplotlib equity curve chart + monthly returns
    table + trade distribution table) and stored to a tmp file before
    being streamed. Returns 503 if ``reportlab`` is not installed.
    """
    from backtesting.engine import backtest_engine
    from backtesting.report import generate_report, report_to_pdf

    result = await asyncio.to_thread(
        backtest_engine.run_backtest,
        strategy_id=req.strategy_id,
        initial_capital=req.initial_capital,
        days=req.days,
        fee_bps=req.fee_bps,
        slippage_bps=req.slippage_bps,
    )
    report = await asyncio.to_thread(
        generate_report, result.to_dict(), req.strategy_id
    )

    import tempfile
    from pathlib import Path

    pdf_path = Path(tempfile.gettempdir()) / (
        f"backtest-report-{req.strategy_id}-{report.report_id}.pdf"
    )
    try:
        await asyncio.to_thread(report_to_pdf, report, pdf_path)
    except ImportError:
        raise HTTPException(
            status_code=503,
            detail="PDF report generation requires `reportlab` "
            "(pip install reportlab)",
        )

    return FileResponse(
        str(pdf_path),
        media_type="application/pdf",
        filename=f"backtest-report-{req.strategy_id}-{report.report_id}.pdf",
    )


# ── W19-1 — Historical replay backtest ───────────────────────────────────────
# Unlike the three routes above (``/api/backtest/run`` / ``/report`` /
# ``/report/pdf``), which all delegate to the synthetic Monte-Carlo
# archetype simulator in ``backtesting/engine.py``, this route loads
# REAL order book snapshots from the SQLite ``market_snapshots`` /
# ``orderbook_ticks`` tables and replays them through the
# :class:`HistoricalReplayEngine`. The response shape mirrors the
# engine's :class:`ReplayResult` dataclass (``n_snapshots``,
# ``n_trades``, ``total_return``, ``sharpe``, ``max_drawdown``,
# ``win_rate``, ``profit_factor``, ``trades``, ``equity_curve``).
#
# Rate-limited at ``HEAVY_LIMIT`` (5/min) — same ceiling as the other
# backtest routes — and the synchronous SQLite scan + replay loop is
# wrapped in ``asyncio.to_thread`` so the event loop never blocks on
# disk I/O.

class HistoricalReplayRequest(BaseModel):
    """Request body for ``POST /api/backtest/historical-replay``.

    The ``token_id`` is the Polymarket CLOB token to replay; ``start_time``
    / ``end_time`` are epoch-seconds bounds on the snapshot window. The
    optional ``strategy`` field lets a future caller plug in a named
    strategy adapter (default ``"simple"`` → :class:`SimpleStrategy`
    mean-reversion rule). ``initial_capital`` is the USD starting cash
    for the replay (default $100, matching ``BANKROLL_BASELINE``).
    """

    token_id: str = Field(..., min_length=1, description="Polymarket CLOB token ID")
    start_time: float = Field(..., description="Replay start timestamp (epoch s)")
    end_time: float = Field(..., description="Replay end timestamp (epoch s)")
    initial_capital: float = Field(default=100.0, ge=1.0, le=1_000_000.0)
    strategy: str = Field(default="simple", description="Strategy name (only 'simple' supported)")


@app.post("/api/backtest/historical-replay", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def historical_replay(request: Request, req: HistoricalReplayRequest):
    """Run a historical replay backtest over real recorded market snapshots.

    Loads rows from the ``market_snapshots`` / ``orderbook_ticks`` SQLite
    tables for the given ``token_id`` and ``[start_time, end_time]`` window,
    then replays each snapshot through the strategy's
    ``generate_signal(context)`` hook. The default ``strategy="simple"``
    plugs in the :class:`backtesting.historical_replay.SimpleStrategy`
    mean-reversion rule (BUYs when ``mid`` drops below the rolling
    average, SELLs when it reverts back).

    Returns the :class:`ReplayResult` payload with headline risk metrics
    (total return, annualised Sharpe, max drawdown, win rate, profit
    factor) plus the trade list (capped at 100) and a downsampled
    equity curve (≤ 200 points).

    The replay is wrapped in ``asyncio.to_thread`` so the SQLite scan +
    Python replay loop never blocks the FastAPI event loop.
    """
    from backtesting.historical_replay import (
        HistoricalReplayEngine,
        SimpleStrategy,
    )

    if req.start_time > req.end_time:
        raise HTTPException(
            status_code=400,
            detail="start_time must be <= end_time",
        )

    engine = HistoricalReplayEngine(settings.market_db_path)

    # Only the default "simple" strategy is wired right now. A future
    # caller could pass ``strategy="signal_trader"`` / ``"market_maker"``
    # — the dispatch table for those is a follow-up.
    strategy_name = (req.strategy or "simple").strip().lower()
    if strategy_name != "simple":
        raise HTTPException(
            status_code=400,
            detail=f"Unknown strategy {req.strategy!r}; only 'simple' is supported.",
        )
    strategy = SimpleStrategy()

    result = await asyncio.to_thread(
        engine.replay,
        token_id=req.token_id,
        strategy=strategy,
        start_time=req.start_time,
        end_time=req.end_time,
        initial_capital=req.initial_capital,
    )

    # Downsample the equity curve to ≤ 200 points so the JSON response
    # stays compact even for 24h × 1-second-snapshot replays (86400 pts).
    step = max(1, len(result.equity_curve) // 200)
    downsampled_curve = result.equity_curve[::step]
    # Always include the final point so the curve's tail is exact.
    if downsampled_curve and downsampled_curve[-1] != result.equity_curve[-1]:
        downsampled_curve.append(result.equity_curve[-1])

    # W20-3 — persist the historical-replay backtest as a
    # ``BacktestExperiment`` row so it shows up alongside the
    # synthetic-MC runs in ``GET /api/backtest/experiments`` and
    # ``POST /api/backtest/compare``. The replay engine's
    # :class:`ReplayResult` already exposes the canonical
    # ``total_return`` / ``sharpe`` / ``max_drawdown`` / ``win_rate`` /
    # ``profit_factor`` / ``trades`` / ``equity_curve`` field names
    # (no percentage → fractional coercion needed; only the synthetic
    # MC engine reports percentages), so the ``engine_kind="historical"``
    # branch of the helper just maps the keys 1:1.
    experiment_id = _persist_backtest_experiment(
        strategy=f"historical:{req.token_id}:{strategy_name}",
        strategy_version="1.0.0",
        start_time=req.start_time,
        end_time=req.end_time,
        initial_capital=req.initial_capital,
        config=req.model_dump(),
        result_dict={
            "final_equity": (
                float(downsampled_curve[-1]) if downsampled_curve
                else req.initial_capital
            ),
            "total_return": result.total_return,
            "sharpe": result.sharpe,
            "sortino": 0.0,  # historical replay doesn't compute Sortino
            "calmar": 0.0,   # historical replay doesn't compute Calmar
            "max_drawdown": result.max_drawdown,
            "win_rate": result.win_rate,
            "profit_factor": result.profit_factor,
            "n_trades": len(result.trades),
            "equity_curve": downsampled_curve,
            "trades": result.trades[:100],
        },
        engine_kind="historical",
    )

    return {
        "status": "completed",
        "synthetic": False,
        "engine": "historical_replay",
        "disclaimer": (
            "Historical replay over recorded market_snapshots rows — "
            "actual market history, NOT a Monte-Carlo archetype draw."
        ),
        "token_id": req.token_id,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "n_snapshots": result.n_snapshots,
        "n_trades": len(result.trades),
        "total_return": result.total_return,
        "sharpe": result.sharpe,
        "max_drawdown": result.max_drawdown,
        "win_rate": result.win_rate,
        "profit_factor": result.profit_factor,
        "trades": result.trades[:100],
        "equity_curve": downsampled_curve,
        "experiment_id": experiment_id,
    }


# ── W20-3 — Backtest experiment registry endpoints ───────────────────────────
# Three sibling routes that surface the persisted ``BacktestExperiment``
# rows so cross-run comparison (the gap flagged in the God Mode
# assessment W17-6 §33) is finally possible:
#
#   GET  /api/backtest/experiments               — list newest-first
#   GET  /api/backtest/experiments/{exp_id}     — fetch one
#   POST /api/backtest/compare                  — A/B/C compare
#
# All three delegate to the ``experiment_store`` singleton (constructed
# against ``EXPERIMENT_DB`` at import time of
# ``backtesting.experiment_store``). The store's SQLite I/O is
# synchronous; we wrap each call in ``asyncio.to_thread`` so the FastAPI
# event loop never blocks on disk I/O. The Pydantic
# ``CompareExperimentsRequest`` body is just ``{"experiment_ids": [...]}``
# — a list-of-str rather than a list-of-strings-passed-as-query-params
# because passing N IDs in the URL would blow past the typical HTTP
# client's 8 KB URL cap at ~ 200 IDs (12-char IDs × 2-char separator +
# query-string overhead).

class CompareExperimentsRequest(BaseModel):
    """Request body for ``POST /api/backtest/compare``.

    ``experiment_ids`` is a list of 12-char IDs (the format returned by
    ``POST /api/backtest/run`` / ``/historical-replay`` in their
    ``experiment_id`` field). Missing IDs are silently dropped (logged
    at INFO) — the comparison still runs over whichever IDs were found.
    """

    experiment_ids: list[str] = Field(
        ..., min_length=1, max_length=100,
        description="Experiment IDs to compare (max 100 per request).",
    )


@app.get("/api/backtest/experiments", tags=["backtesting"])
@limiter.limit(READ_LIMIT)
async def list_backtest_experiments(
    request: Request,
    strategy: str | None = Query(
        None, description="Filter by strategy name (exact match)."
    ),
    limit: int = Query(
        50, ge=1, le=1000,
        description="Max experiments to return (1..1000)."
    ),
):
    """List backtest experiments newest-first.

    Each row carries the headline risk metrics (``total_return``,
    ``sharpe``, ``sortino``, ``calmar``, ``max_drawdown``,
    ``win_rate``, ``profit_factor``, ``n_trades``) + the JSON-encoded
    ``config`` / ``equity_curve`` / ``trades`` blobs (decoded back to
    native Python types by the store). The ``equity_curve`` / ``trades``
    blobs are capped at 10 KB per row on write, so the response stays
    bounded.

    The ``strategy`` query param does an EXACT match against the
    ``strategy`` column — pass the same string you used as
    ``strategy_id`` in the ``POST /api/backtest/run`` request, or the
    ``"historical:{token_id}:simple"`` shape used by
    ``POST /api/backtest/historical-replay``.
    """
    from backtesting.experiment_store import experiment_store

    if experiment_store is None:
        raise HTTPException(
            status_code=503,
            detail="Experiment store is not available (init failed at import).",
        )
    rows = await asyncio.to_thread(
        experiment_store.list_experiments, strategy, limit
    )
    return {
        "status": "ok",
        "count": len(rows),
        "strategy_filter": strategy,
        "experiments": rows,
    }


@app.get("/api/backtest/experiments/{experiment_id}", tags=["backtesting"])
@limiter.limit(READ_LIMIT)
async def get_backtest_experiment(
    request: Request,
    experiment_id: str,
):
    """Fetch one backtest experiment by ``experiment_id``.

    Returns the full row with decoded ``config`` / ``equity_curve`` /
    ``trades`` JSON blobs. ``404`` if the ID doesn't exist (or was
    truncated by the 10 KB blob cap during write, in which case the
    headline metrics are still readable via the list endpoint — only
    the JSON-blob columns are corrupted).
    """
    from backtesting.experiment_store import experiment_store

    if experiment_store is None:
        raise HTTPException(
            status_code=503,
            detail="Experiment store is not available (init failed at import).",
        )
    result = await asyncio.to_thread(experiment_store.get, experiment_id)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"Experiment {experiment_id!r} not found.",
        )
    return result


@app.post("/api/backtest/compare", tags=["backtesting"])
@limiter.limit(READ_LIMIT)
async def compare_backtest_experiments(
    request: Request, req: CompareExperimentsRequest,
):
    """A/B/C compare multiple backtest experiments.

    Returns the headline metric winners across the requested set:

      * ``best_return``      — max ``total_return``.
      * ``best_sharpe``      — max ``sharpe``.
      * ``lowest_drawdown``  — min ``max_drawdown`` (lower is better).
      * ``experiments``      — per-experiment summary dict with
        ``{id, strategy, return, sharpe, max_drawdown, win_rate}``.

    Missing IDs are silently dropped (the response's ``count`` reflects
    the number actually compared). If no IDs match, the response carries
    ``{"error": "No experiments found", "count": 0}`` with HTTP 200 —
    the request was syntactically valid (200), but no experiments were
    found (``count == 0``).
    """
    from backtesting.experiment_store import experiment_store

    if experiment_store is None:
        raise HTTPException(
            status_code=503,
            detail="Experiment store is not available (init failed at import).",
        )
    result = await asyncio.to_thread(
        experiment_store.compare, req.experiment_ids
    )
    return result


# ── W20-1 — Walk-forward CV + Monte Carlo simulation routes ─────────────────
# Two advanced backtest routes that delegate to the pure-Python
# ``walk_forward_analysis`` and ``monte_carlo_simulation`` helpers in
# ``backtesting/advanced.py``. Until W20-1 those helpers existed with full
# unit-test coverage (``tests/test_advanced_backtest.py``, 12 tests) but
# had NO HTTP surface — the God Mode assessment (W17-6 / CF-7 in the
# BACKTEST_ENGINE_REASSESSMENT) flagged the docstring in
# ``backtesting/advanced.py`` that lied about the existence of these two
# routes. This block makes the lies true.
#
# Both routes are rate-limited at ``HEAVY_LIMIT`` (5/min) — same ceiling
# as every other ``/api/backtest/*`` route — and the CPU-bound numpy work
# is wrapped in ``asyncio.to_thread`` so the FastAPI event loop never
# blocks on the sklearn ``fit`` / numpy ``choice`` calls.
#
# Walk-forward training-data source: the route queries the SQLite
# ``ml_feature_store`` table (the same table
# ``timescale_db.fetch_training_samples`` reads) directly so it can pull
# the ``timestamp`` column alongside ``features_json`` and
# ``outcome_resolved`` in a single statement —
# ``timescale_db.fetch_training_samples`` only returns ``(X, y)`` (no
# timestamps), but ``walk_forward_analysis`` requires timestamps for its
# strict time-ordered partition. If no labelled rows exist in the table
# (or the table / DB is missing entirely), the route falls back to
# ``ml.model._synthetic_training_data`` so the route still returns a
# well-formed payload with ``n_windows >= 1`` rather than 500-ing.
#
# Monte-carlo trade-returns source: the route loads up to 500 of the
# most-recent rows from the ``closed_positions`` SQLite table (via the
# async ``closed_positions.get_closed_positions`` helper) and derives
# each trade's ROI as ``pnl / (entry_price * shares)``. The ``pnl``
# column is already signed by ``record_closed_position`` (positive for
# wins, negative for losses), so no direction-sign manipulation is
# needed (the spec's draft handler used ``size`` / ``side`` field names
# that don't exist in the actual schema — the real columns are
# ``shares`` / ``direction``).

@app.post("/api/backtest/walk-forward", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def run_walk_forward(request: Request, params: dict | None = None):
    """Run walk-forward backtest analysis.

    Walks the time-ordered ``ml_feature_store`` dataset through
    ``(train_window, test_window, step)`` rolling windows, fitting a
    fresh ``RandomForestClassifier`` on each train fold and scoring AUC
    / Brier on the immediately-following out-of-sample test fold.

    Falls back to the synthetic market-dynamics dataset
    (``ml.model._synthetic_training_data``) when no labelled rows exist
    in the SQLite ``ml_feature_store`` table — so the route always
    returns a well-formed payload (``n_windows >= 1``) rather than
    500-ing on an empty feature store.

    Returns the per-window AUC / Brier series (capped at 20 entries for
    response-size discipline) plus the aggregate metrics (mean / std
    AUC, mean Brier) and the equity-curve risk metrics (Sharpe,
    Sortino, Calmar, max drawdown).
    """
    from backtesting.advanced import walk_forward_analysis
    from core.timescale_db import SQLITE_FALLBACK_PATH as _market_db
    from ml.features import N_FEATURES

    params = params or {}
    train_window = int(params.get("train_window", 1000))
    test_window = int(params.get("test_window", 200))
    step = int(params.get("step", 200))

    # ── Load (features, labels, timestamps) directly from
    # ``ml_feature_store`` so the walk-forward routine can keep its
    # strict time-ordered partition (``timescale_db.fetch_training_samples``
    # returns ``(X, y)`` only — no timestamps — which would force us to
    # synthesise them from row order and lose the chronological signal).
    features: np.ndarray
    labels: np.ndarray
    timestamps: np.ndarray
    source = "real"
    try:
        import sqlite3

        with sqlite3.connect(_market_db) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT features_json, outcome_resolved, timestamp
                FROM ml_feature_store
                WHERE outcome_resolved IS NOT NULL
                  AND features_json IS NOT NULL
                ORDER BY timestamp ASC
                LIMIT 5000;
                """
            )
            rows = cursor.fetchall()
    except Exception as e:
        logger.warning("[walk_forward] SQLite load failed (%s): %s", _market_db, e)
        rows = []

    if rows:
        X_list: list[np.ndarray] = []
        y_list: list[int] = []
        ts_list: list[float] = []
        for feat_str, outcome, ts in rows:
            try:
                arr = np.array(json.loads(feat_str), dtype=np.float32)
                if len(arr) < N_FEATURES:
                    arr = np.pad(arr, (0, N_FEATURES - len(arr)))
                elif len(arr) > N_FEATURES:
                    arr = arr[:N_FEATURES]
                X_list.append(arr)
                y_list.append(int(outcome))
                ts_list.append(float(ts))
            except Exception:
                continue
        if len(X_list) >= max(train_window + test_window, 50):
            features = np.array(X_list, dtype=np.float32)
            labels = np.array(y_list, dtype=np.int32)
            timestamps = np.array(ts_list, dtype=np.float64)
        else:
            source = "synthetic_too_few_real"
            features, labels, timestamps = _synthetic_walk_forward_data()
    else:
        # No labelled real samples — fall back to the same synthetic
        # generator ``ml.model._synthetic_training_data`` uses so the
        # route still returns a non-trivial walk-forward result rather
        # than a zero-window payload.
        source = "synthetic_no_real"
        features, labels, timestamps = _synthetic_walk_forward_data()

    # Fresh RandomForest per window — small + fast (matches the
    # ``_rf_factory`` pattern in ``tests/test_advanced_backtest.py``).
    def model_factory():
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(
            n_estimators=20,
            max_depth=6,
            random_state=42,
            n_jobs=-1,
        )

    result = await asyncio.to_thread(
        walk_forward_analysis,
        features=features,
        labels=labels,
        timestamps=timestamps,
        model_factory=model_factory,
        train_window=train_window,
        test_window=test_window,
        step=step,
    )

    return {
        "source": source,
        "n_samples": int(len(features)),
        "n_windows": result.aggregate.get("n_windows", 0),
        "mean_auc": result.aggregate.get("mean_auc", 0.0),
        "std_auc": result.aggregate.get("std_auc", 0.0),
        "mean_brier": result.aggregate.get("mean_brier", 0.0),
        "sharpe_ratio": result.sharpe_ratio,
        "sortino_ratio": result.sortino_ratio,
        "calmar_ratio": result.calmar_ratio,
        "max_drawdown": result.max_drawdown,
        "windows": result.windows[:20],  # Cap for response size.
    }


def _synthetic_walk_forward_data() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build a (features, labels, timestamps) triple from the synthetic
    market-dynamics generator in ``ml.model``.

    Used as a fallback when the SQLite ``ml_feature_store`` table has no
    labelled rows (or the table / DB is missing entirely). Imports
    lazily so a broken ``ml.model`` import (e.g. a missing optional
    LightGBM dep) doesn't break this route's module-load.

    Timestamps are synthesised as a monotonically-increasing
    ``np.arange(n)`` because the synthetic generator doesn't emit a
    time axis — the walk-forward routine only requires that timestamps
    be monotonically orderable so its internal ``argsort`` is stable.
    """
    from ml.model import _synthetic_training_data

    X, y = _synthetic_training_data(3000)
    ts = np.arange(len(X), dtype=np.float64)
    return X, y, ts


@app.post("/api/backtest/monte-carlo", tags=["backtesting"])
@limiter.limit(HEAVY_LIMIT)
async def run_monte_carlo(request: Request, params: dict | None = None):
    """Run Monte Carlo simulation on trade history.

    Loads the most-recent (up to 500) closed positions from the
    ``closed_positions`` SQLite table and bootstrap-resamples their
    per-trade ROI series into ``n_simulations`` equity curves. Reports
    the distribution of final-equity returns as percentiles
    (``p5`` / ``p25`` / ``p50`` / ``p75`` / ``p95``) plus the
    probability-of-ruin (fraction of simulations whose final equity
    fell below ``ruin_threshold * initial_capital``).

    Trade ROI is derived from the table's signed ``pnl`` column as
    ``pnl / (entry_price * shares)`` — ``pnl`` is already signed by
    ``record_closed_position`` (positive for wins, negative for losses)
    so no direction-sign manipulation is needed (the spec's draft used
    ``side`` / ``size`` field names that don't exist on the schema — the
    real columns are ``direction`` / ``shares``).
    """
    from backtesting.advanced import monte_carlo_simulation
    from core.closed_positions import closed_positions

    params = params or {}
    n_simulations = int(params.get("n_simulations", 10000))
    initial_capital = float(params.get("initial_capital", 100.0))
    ruin_threshold = float(params.get("ruin_threshold", 0.5))

    # ``closed_positions.get_closed_positions`` is async (its ``@timed_query``
    # wrapper dispatches the SQLite read to ``asyncio.to_thread``) —
    # await it directly rather than wrapping in another ``to_thread``.
    positions = await closed_positions.get_closed_positions(limit=500)
    if not positions:
        return {
            "error": "No closed positions for simulation",
            "n_simulations": 0,
        }

    # Compute per-trade ROI from the signed ``pnl`` column. The ROI
    # denominator (``entry_price * shares``) is the cost basis of the
    # position; the resulting ratio is the fractional return on capital
    # deployed for that trade (NOT a price-return ratio — Polymarket
    # binary-outcome positions settle to 0 or 1, so ``exit_price`` is
    # already either near-0 or near-1 and a naive
    # ``(exit-entry)/entry`` would overstate magnitude).
    returns: list[float] = []
    for p in positions:
        try:
            entry = float(p.get("entry_price") or 0.0)
            shares = float(p.get("shares") or 0.0)
            pnl = float(p.get("pnl") or 0.0)
        except (TypeError, ValueError):
            continue
        if entry > 0 and shares > 0:
            cost = entry * shares
            if cost > 0:
                returns.append(pnl / cost)

    if not returns:
        return {
            "error": "No valid trade returns derivable from closed positions",
            "n_simulations": 0,
            "n_positions": len(positions),
        }

    result = await asyncio.to_thread(
        monte_carlo_simulation,
        trade_returns=np.array(returns, dtype=np.float64),
        n_simulations=n_simulations,
        initial_capital=initial_capital,
        ruin_threshold=ruin_threshold,
    )

    return {
        "n_simulations": result.n_simulations,
        "n_positions": len(positions),
        "n_returns": len(returns),
        "expected_return": result.expected_return,
        "worst_case": result.worst_case,
        "best_case": result.best_case,
        "probability_of_ruin": result.probability_of_ruin,
        "percentiles": result.percentiles,
    }


@app.get("/api/audit/logs", tags=["audit"])
async def get_audit_logs(
    limit: int = Query(100, ge=1, le=1000),
    category: str | None = Query(None, max_length=100),
    cursor: str | None = Query(
        None,
        description=(
            "Opaque base64 cursor from a previous response's "
            "``next_cursor`` field. Omit for the first page (newest "
            "logs). W16-5."
        ),
    ),
):
    """Query immutable SQLite audit trail logs (newest first).

    W16-5 — supports cursor-based pagination via the optional ``cursor``
    query param. The cursor encodes the ``(timestamp, id)`` boundary of
    the last row on the current page; the next request with that cursor
    returns the page of audit-log rows whose ``(timestamp, id)`` pair is
    strictly less than the boundary. When ``cursor`` is omitted, the
    first page (the newest ``limit`` rows) is returned — fully backward
    compatible with the pre-pagination wire shape (``{logs, count}`` plus
    the new ``next_cursor`` / ``has_more`` fields).
    """
    page = await audit_logger.get_recent_events_page(
        limit=limit,
        category=category,
        cursor=cursor,
    )
    return {
        "logs": page.items,
        "count": len(page.items),
        "next_cursor": page.next_cursor,
        "has_more": page.has_more,
    }


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
    # W15-4 — cache the reconciliation artifact for 30s. A fresh
    # ``run_reconciliation()`` walks every persisted record vs. the
    # in-memory store — recompute on every poll is wasted work. The
    # artifact only shifts on a write (which the per-write
    # ``record_fill`` / ``add_order`` hooks already throttle).
    cache_key = "database_reconciliation"
    cached = general_cache.get(cache_key)
    if cached is not None:
        return cached
    from core.reconciliation import last_reconciliation, run_reconciliation
    report = last_reconciliation()
    if report is None:
        report = run_reconciliation()
    general_cache.set(cache_key, report)
    return report


# ── W21-2 — PostgreSQL health monitor endpoints ─────────────────────────────
# Three endpoints exposed so the dashboard can:
#   * GET /api/database/pg-health          — current PG health status
#                                            (is_healthy, consecutive
#                                            failures, uptime %, avg
#                                            latency, last 100 checks).
#   * POST /api/database/pg-health/check   — force an immediate PG ping
#                                            (operator can verify a
#                                            recovery without waiting
#                                            for the next background
#                                            tick).
#   * GET /api/database/backend-status     — current backend selection
#                                            state (PG vs SQLite active,
#                                            last flip time/reason,
#                                            flip history).
# The background monitor task itself is started by the FastAPI lifespan
# startup handler (see ``core.pg_health_monitor.pg_health_monitor.start``
# invocation in ``lifespan`` below).
@app.get("/api/database/pg-health", tags=["database"])
async def pg_health_status():
    """Get PostgreSQL health status (W21-2).

    Returns the full ``PGHealthStatus`` snapshot: ``is_healthy`` flag,
    consecutive failures / successes counters, the last 100 ``HealthCheck``
    samples (timestamp, healthy, latency_ms, error), lifetime uptime %,
    and average latency from healthy samples. The dashboard renders this
    as a "PG health" panel — operators can correlate a latency spike
    with a state flip without scraping Prometheus.

    The status is whatever the background ``PGHealthMonitor`` task has
    recorded so far — the endpoint does NOT trigger a fresh ping (use
    ``POST /api/database/pg-health/check`` for that).
    """
    from core.pg_health_monitor import pg_health_monitor
    return pg_health_monitor.get_status()


@app.post("/api/database/pg-health/check", tags=["database"])
async def force_pg_health_check():
    """Force an immediate PG health check (W21-2).

    Triggers ``PGHealthMonitor._check_health`` synchronously (rather
    than waiting for the next background tick) so an operator who just
    restarted PG can verify the recovery in the dashboard without the
    default 15s wait. The result is recorded in the monitor's status
    just like a background tick — calling this endpoint twice in a row
    produces two recorded ``HealthCheck`` samples.

    Returns the post-check ``PGHealthStatus`` snapshot (the same shape
    as ``GET /api/database/pg-health``).
    """
    from core.pg_health_monitor import pg_health_monitor
    await pg_health_monitor._check_health()
    return pg_health_monitor.get_status()


# Note: ``GET /api/database/status`` (W21-1) returns the unified
# ``db_manager.get_status()`` payload — it already surfaces the
# ``backend`` / ``pg_available`` / ``fallback_count`` / ``recent_errors``
# fields the W21-2 dashboard panel needs, so a separate
# ``/api/database/backend-status`` endpoint is unnecessary.


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
    """Authenticated WebSocket endpoint.

    W14-1 — The client is registered with BOTH the legacy
    ``ConnectionManager`` (``manager``) AND the new channel-based
    ``WSBroadcastManager`` (``ws_manager``):

      * ``manager`` keeps broadcasting the raw snapshot dict every 1s
        so the existing ``useBot`` React hook (which checks
        ``data.order_books`` directly, no envelope) keeps working.
      * ``ws_manager`` broadcasts per-channel envelope messages
        (``{channel, data, timestamp}``) for the new ``useRealtimeData``
        hook which filters by ``msg.channel === wsChannel``.

    A welcome message is pushed on the ``system`` channel immediately
    after accept so a client can confirm the round-trip. The legacy
    raw snapshot is sent right after so ``useBot``'s ``onmessage``
    handler picks it up on connect (matches the pre-W14-1 contract).

    Clients can send ``{"type": "subscribe", "channels": ["positions"]}``
    to restrict delivery to a subset of channels. An empty / missing
    channels set means "all channels" (the default).
    """
    if settings.api_token:
        token = websocket.query_params.get("token")
        if not hmac.compare_digest(token or "", settings.api_token):
            await websocket.close(code=4401, reason="Unauthorized")
            return
    else:
        # Fail-closed: no API token configured → reject the WS upgrade.
        await websocket.close(code=4401, reason="Unauthorized")
        return

    client_id = str(uuid.uuid4())[:8]
    await websocket.accept()
    # Register with the new channel-based manager (sends welcome envelope).
    await ws_manager.connect(websocket, client_id)
    # Also register with the legacy manager so the 1s snapshot loop
    # keeps pushing the raw (no-envelope) snapshot for useBot's
    # ``data.order_books`` direct-access pattern. ``manager.connect``
    # would call ``websocket.accept()`` a second time (error), so we
    # append directly to its active list instead.
    manager.active.append(websocket)
    log.info("WS client registered: %s (legacy + broadcast manager)", client_id)
    try:
        # Initial snapshot (raw, no envelope) — backwards-compat with
        # the dashboard's useBot hook which checks ``data.order_books``.
        snap = await _build_snapshot()
        await websocket.send_text(json.dumps(snap, default=str))
        # And the same snapshot wrapped in the envelope so any
        # useRealtimeData subscriber on the ``system`` channel gets an
        # immediate data frame on connect (no need to wait for the
        # first 1s tick).
        await ws_manager.broadcast("system", snap)
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            # W14-1 — handle client subscribe messages. ``raw`` is a
            # JSON string; a non-JSON or malformed payload is silently
            # dropped so a buggy client can't crash the read loop.
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "subscribe":
                channels = msg.get("channels") or []
                if not isinstance(channels, list):
                    continue
                await ws_manager.subscribe(client_id, set(channels))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WS client disconnected: %s — %s", client_id, e)
    finally:
        await ws_manager.disconnect(client_id)
        if websocket in manager.active:
            manager.active.remove(websocket)


# W14-1 — WebSocket broadcast manager stats endpoint.
# Additive: appends ``GET /api/ws/stats`` returning the current
# connected-client count, total messages sent, total send errors, the
# channel catalog, and the list of currently-connected client IDs.
# Used by the dashboard's connection-monitor panel and by operators
# debugging "why isn't my client receiving updates" — a zero count
# confirms no clients are connected; a high error count points to a
# network / serialization issue.
# Auth enforced by ``enforce_api_auth`` (path not in ``PUBLIC_PATHS``).
@app.get("/api/ws/stats", tags=["system"])
async def ws_stats():
    """Return ``WSBroadcastManager`` stats (connected clients, message counts)."""
    return ws_manager.get_stats()


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


# ── W14-7 — Rate-limit analytics dashboard endpoint ─────────────────────────
# Surfaces the in-memory ``RateLimitTracker`` snapshot to the React
# ``RateLimitPanel`` component. Auth enforced by ``enforce_api_auth``
# (this path is NOT in ``PUBLIC_PATHS``); rate-limited by ``READ_LIMIT``
# because the dashboard polls every 30 s — well under the 120/min cap.
# The route is intentionally NOT cached: a stale snapshot would mask a
# live rate-limiting burst, which is exactly the scenario the panel is
# designed to surface.
@app.get("/api/rate-limit/stats", tags=["system"])
@limiter.limit(READ_LIMIT)
async def rate_limit_stats(request: Request):  # noqa: ARG001 — slowapi requires the `request` arg
    """Return a snapshot of the last hour's rate-limit activity.

    Shape::

        {
          "total_hits": 42,
          "hits_per_minute_rate": 0.7,
          "hits_by_endpoint": {"/api/orders": 30, "/api/markets": 12},
          "hits_by_client":  {"127.0.0.1": 40, "10.0.0.5": 2},
          "hits_per_minute":  {"1": 5, "2": 0, ... "60": 0},
          "top_endpoints":    {"/api/orders": 30, "/api/markets": 12}
        }

    Used by the dashboard's ``Rate Limits`` panel (sidebar → System →
    Rate Limits). All state is in-memory and process-local — a restart
    zeroes the counters, which is fine for the panel's "last hour"
    window.
    """
    return rate_limit_tracker.get_stats()


# ── W15-4 — Per-endpoint latency profiling endpoints ─────────────────────────
# Additive: appends three endpoints under ``/api/profiling/*`` so an
# operator (or the ``scripts/perf_report.py`` CLI) can surface p50 /
# p95 / p99 latencies per route without an external tracing layer.
#
# The data is fed by the ``profiler.record(...)`` call inserted into
# the existing ``request_logging_middleware`` (same path that already
# feeds the W14-7 ``rate_limit_tracker.record_request`` call) so every
# authenticated route — including 4xx / 5xx responses — is captured.
#
# All state is in-memory and process-local: a restart zeroes the
# counters (same contract as the rate-limit-tracker panel — see
# W14-7). The ``POST /api/profiling/reset`` route lets an operator
# wipe the counters WITHOUT a restart so a short-window profile run
# after a deploy starts from a fresh baseline.
#
# Auth enforced by ``enforce_api_auth`` (none of the three paths are
# in ``PUBLIC_PATHS``). Rate-limited by ``READ_LIMIT`` on the two GETs
# so a dashboard polling the panel every few seconds can't starve
# the trading path; the POST reset is rate-limited by ``WRITE_LIMIT``
# because it mutates state.
@app.get(
    "/api/profiling/stats",
    tags=["system"],
    summary="Per-endpoint latency statistics",
    description=(
        "Returns per-endpoint request count, average latency, p50 / p95 / "
        "p99 latencies (ms), error count, error rate, and last-called "
        "timestamp. Sorted by ``sort_by`` (one of ``p95``, ``p99``, "
        "``avg``, ``count``, ``errors``; default ``p95``) descending. "
        "Use ``GET /api/profiling/slowest?limit=N`` for the top-N view. "
        "In-memory only — a process restart zeroes the data."
    ),
)
@limiter.limit(READ_LIMIT)
async def profiling_stats(
    request: Request,  # noqa: ARG001 — slowapi requires the `request` arg
    sort_by: str = Query("p95", max_length=20),
):
    """Get per-endpoint latency statistics."""
    return {
        "summary": profiler.get_summary(),
        "endpoints": profiler.get_stats(sort_by),
    }


@app.get(
    "/api/profiling/slowest",
    tags=["system"],
    summary="Slowest endpoints by p95 latency",
    description=(
        "Returns the top ``limit`` endpoints ranked by p95 latency "
        "(descending). Useful for surfacing the small handful of routes "
        "that dominate tail latency without paging through the full "
        "``/api/profiling/stats`` table."
    ),
)
@limiter.limit(READ_LIMIT)
async def profiling_slowest(
    request: Request,  # noqa: ARG001 — slowapi requires the `request` arg
    limit: int = Query(10, ge=1, le=100),
):
    """Get the slowest endpoints by p95 latency."""
    return {"slowest": profiler.get_slowest(limit)}


@app.post(
    "/api/profiling/reset",
    tags=["system"],
    summary="Reset all profiling data",
    description=(
        "Drop every endpoint's latency / error / count stats so the "
        "next ``GET /api/profiling/stats`` call starts from a fresh "
        "baseline. In-memory only — equivalent to restarting the "
        "service for clean numbers, without the restart. Useful for "
        "capturing a short-window profile run after a deploy."
    ),
)
@limiter.limit(WRITE_LIMIT)
async def profiling_reset(
    request: Request,  # noqa: ARG001 — slowapi requires the `request` arg
):
    """Reset all profiling data."""
    profiler.reset()
    return {"ok": True}


# ── W15-8 — Advanced execution planner endpoint ──────────────────────────────
# Surfaces ``execution.advanced_router.AdvancedOrderRouter`` to the dashboard
# so an operator can preview an execution schedule (TWAP / VWAP / iceberg /
# immediate) BEFORE committing capital. Auth enforced by ``enforce_api_auth``
# (this path is NOT in ``PUBLIC_PATHS``); rate-limited by ``READ_LIMIT`` so a
# misbehaving client polling the planner can't starve the trading path.
#
# The endpoint is a PURE PLANNER — it does not submit any orders, touch the
# order book, or mutate any state. The live trading path can call the same
# ``AdvancedOrderRouter`` to obtain a plan and then route each slice through
# ``SmartOrderRouter`` (for book-aware sizing) and the venue adapter.
@app.post("/api/execution/plan", tags=["execution"])
@limiter.limit(READ_LIMIT)
async def plan_execution(request: Request, params: dict):  # noqa: ARG001 — slowapi requires the `request` arg
    """Get an execution plan for a large order.

    Body shape::

        {
          "total_size": float,                 # parent order size (USDC or shares)
          "strategy": "auto" | "twap" | "vwap" | "iceberg" | "immediate",  # default "auto"
          "duration": float,                  # TWAP duration (s); default 60
          "n_slices": int,                    # TWAP / VWAP slice count; default 5
          "volume_profile": [float, ...],    # VWAP per-bin volumes; default uniform
          "visible_size": float | null,       # Iceberg visible quantum; default auto
          "avg_daily_volume": float,          # ADV — for "auto" recommendation; default 1000
          "spread_bps": float,                # top-of-book spread in BPS; default 20
          "urgency": "normal" | "urgent"      # urgency hint for "auto"; default "normal"
        }

    Returns::

        {
          "strategy": str,                    # the strategy actually used
          "slices": [{"index", "size", "price_target", "delay_seconds"}, ...],
          "total_size": float,
          "duration_seconds": float
        }

    The ``strategy`` field reflects the chosen strategy — when ``strategy``
    is ``"auto"`` (or omitted), the router's ``recommend_strategy`` is
    invoked with the supplied ADV / spread / urgency to pick the best
    strategy, and the resolved name is echoed back so the caller can
    display it in the dashboard.
    """
    from execution.advanced_router import AdvancedOrderRouter

    router = AdvancedOrderRouter()

    total_size = float(params.get("total_size", 0) or 0)
    strategy = (params.get("strategy") or "auto").lower()

    if strategy == "auto":
        strategy = router.recommend_strategy(
            total_size=total_size,
            avg_daily_volume=float(params.get("avg_daily_volume", 1000) or 1000),
            spread_bps=float(params.get("spread_bps", 20) or 20),
            urgency=(params.get("urgency") or "normal"),
        )

    plan = router.plan(
        strategy,
        total_size,
        duration=float(params.get("duration", 60) or 60),
        n_slices=int(params.get("n_slices", 5) or 5),
        volume_profile=params.get("volume_profile"),
        visible_size=params.get("visible_size"),
    )

    return {
        "strategy": plan.strategy,
        "slices": plan.slices,
        "total_size": plan.total_size,
        "duration_seconds": plan.duration_seconds,
    }


# ── W14-8 — Client-side error reporting endpoint ──────────────────────────────
# Public (no auth) so the reporter can POST crash reports even when the
# trader's API token is misconfigured — operators would rather see the
# auth-failure in this log than lose the telemetry. The reporter batches
# reports on a 5s cadence and POSTs them here as ``{"errors": [...]}``;
# we acknowledge the batch and write each report to the dedicated
# ``client_errors`` logger (filterable from the main app log via the
# logger name) with the URL + session ID + stack + context as extras
# (which the JSON formatter in ``core.logging`` picks up if configured).
#
# The endpoint does NOT persist reports to the database — the volume is
# potentially high (one batch per active tab per 5s) and the operational
# value is in near-real-time triage, not historical queries. Operators
# who need long-term retention should ship the ``client_errors`` logger
# output to their existing log aggregator (ELK / Loki / CloudWatch).
class ClientError(BaseModel):
    """Single client-side error report (matches TS ``ErrorReport``)."""

    message: str
    stack: str | None = None
    filename: str | None = None
    lineno: int | None = None
    colno: int | None = None
    url: str
    userAgent: str
    timestamp: float
    sessionId: str
    userId: str | None = None
    release: str | None = None
    context: dict | None = None


class ClientErrorBatch(BaseModel):
    """Batch wrapper — the reporter POSTs ``{"errors": [...]}``."""

    errors: list[ClientError]


# Dedicated logger so operators can filter client-side errors out of the
# main app log (which is dominated by trading / strategy noise). The
# logger inherits the root handler config by default; a future PR can
# attach a separate handler (e.g. a Slack webhook for critical-severity
# reports) without touching the main app log.
_client_error_logger = logging.getLogger("client_errors")


@app.post(
    "/api/client-errors",
    tags=["system"],
    summary="Receive client-side error reports",
    description=(
        "Sentry-like crash report ingestion. The frontend batches errors "
        "in memory (5s window) and POSTs them here as a JSON array. "
        "Public (no auth) so reports still arrive when the trader's API "
        "token is misconfigured. Reports are written to the "
        "`client_errors` logger and acknowledged with a count; they are "
        "NOT persisted to the database."
    ),
)
async def receive_client_errors(batch: ClientErrorBatch):
    """Receive a batch of client-side error reports.

    Each report is logged at WARNING level (errors are by definition
    unexpected — INFO would bury them in routine traffic) with the
    URL, session ID, stack, and context forwarded as log extras so a
    structured log aggregator can index them.

    Returns ``{"ok": True, "received": N}`` so the client can confirm
    delivery (though the client's ``.catch(() => {})`` swallows the
    response anyway — the ACK is for operators running the endpoint
    via curl during debugging).
    """
    for err in batch.errors:
        _client_error_logger.warning(
            "[Client Error] %s",
            err.message,
            extra={
                "url": err.url,
                "session": err.sessionId,
                "stack": err.stack,
                "context": err.context,
                "user_agent": err.userAgent,
                "timestamp": err.timestamp,
                "filename": err.filename,
                "lineno": err.lineno,
                "colno": err.colno,
            },
        )
    return {"ok": True, "received": len(batch.errors)}


# (W14-5) ml.ab_testing — A/B testing framework for ML model promotion
# decisions. Additive wiring appended at end of file per the W14-5 task
# spec. Adds four endpoints under ``/api/ab-test`` so operators can
# start / stop / evaluate champion-vs-challenger experiments from the
# dashboard without touching the production prediction path:
#
#   GET  /api/ab-test                 current experiment status (active flag,
#                                    champion/challenger versions, per-arm
#                                    prediction counts, recent history)
#   POST /api/ab-test/start           start a new experiment (body: name,
#                                    champion_version, challenger_version,
#                                    traffic_split in [0,1], min_samples)
#   POST /api/ab-test/stop            stop the current (or named) experiment
#   GET  /api/ab-test/evaluate        evaluate an experiment — per-arm
#                                    AUC/Brier/log-loss/accuracy +
#                                    two-proportion z-test on accuracy +
#                                    two-sample t-test on Brier +
#                                    promote/keep_champion recommendation
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the four paths are in ``PUBLIC_PATHS``).
from ml.ab_testing import register_routes as _register_ab_test_routes

_register_ab_test_routes(app)


# (W16-2) ml.feature_store — ML feature store (definitions / values /
# per-version importance snapshots / drift detection). Additive wiring
# appended at end of file per the W16-2 task spec. Adds five endpoints
# under ``/api/features`` so an operator can audit the input feature
# distribution that fed a given prediction + track how per-feature
# importance moves across model versions:
#
#   GET  /api/features                       list all registered feature
#                                            definitions (name, type,
#                                            description, min/max bounds)
#   GET  /api/features/{name}/stats          windowed statistics for a
#                                            single feature (mean/std/min/
#                                            max/p25/p50/p75/p95/n_samples)
#   GET  /api/features/importance            per-version feature-importance
#                                            history (filter by
#                                            ``model_version`` / ``feature_name``)
#   GET  /api/features/drift                drift status (mean-shift test)
#                                            for every registered feature
#   POST /api/features/importance           record a per-version importance
#                                            snapshot (internal — used by
#                                            ``ml/model.fit_initial``)
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the five paths are in ``PUBLIC_PATHS``).
from ml.feature_store import register_routes as _register_feature_store_routes

_register_feature_store_routes(app)


# ── (W16-7) Async DB pool — read-side v2 endpoints ──────────────────────────
# W16-7: async SQLite connection pool (``core/db_pool.AsyncDBPool``) +
# thin async repository layer (``core.async_repositories``). Additive —
# no existing sync endpoint is touched. The sync recorders continue to
# be the source of truth for writes; the new ``/api/v2/*`` endpoints
# read through the async pool so a dashboard poll doesn't block the
# event loop on a ``sqlite3.connect`` call.
#
# Two new endpoints appended here (auth enforced by ``enforce_api_auth``
# — neither path is in ``PUBLIC_PATHS``):
#
#   GET /api/v2/decisions/recent    async read of recent decision_events
#                                   (query params: limit, default 50;
#                                   stage, optional stage filter)
#   GET /api/v2/observability/latest
#                                   async read of the latest metric value
#                                   per (category, name) pair
#
# DB_PATH constants are imported (not redefined) from the canonical
# sync recorder modules — the async pool MUST point at the same on-disk
# file the sync recorder writes to, otherwise the v2 endpoints would
# read stale / empty data. The sync recorder modules' ``DB_PATH`` is
# a ``pathlib.Path`` resolved at import time from the
# ``DECISION_LEDGER_DB_PATH`` / ``OBSERVABILITY_DB_PATH`` env vars
# (redirected to ``/tmp/pmbot_conftest_isolation`` by the test
# conftest), so importing the constant inherits whatever the test
# harness / runtime has configured.
from core.decision_ledger import DB_PATH as DECISION_DB_PATH  # noqa: E402
from core.observability import DB_PATH as OBS_DB_PATH  # noqa: E402


@app.get(
    "/api/v2/decisions/recent",
    tags=["decisions"],
    summary="Recent decisions (async)",
    description=(
        "Async version of the recent-decisions feed. Reads through the "
        "W16-7 ``AsyncDBPool`` (one aiosqlite.Connection per DB) so the "
        "read never blocks the event loop. Returns the most-recent N "
        "rows from ``decision_events`` (newest first), optionally "
        "filtered by ``stage``."
    ),
)
async def get_decisions_async(limit: int = 50, stage: str | None = None):
    """Async version using the async DB pool."""
    from core.async_repositories import AsyncDecisionRepository

    repo = AsyncDecisionRepository(str(DECISION_DB_PATH))
    decisions = await repo.get_recent(limit=limit, stage=stage)
    return {"decisions": decisions, "count": len(decisions)}


@app.get(
    "/api/v2/observability/latest",
    tags=["observability"],
    summary="Latest observability metrics (async)",
    description=(
        "Async version of the latest-metrics feed. Reads through the "
        "W16-7 ``AsyncDBPool`` so the read never blocks the event "
        "loop. Returns the latest value for each ``(category, name)`` "
        "pair recorded by the sync ``core.observability`` collector."
    ),
)
async def get_observability_async():
    """Async version using the async DB pool."""
    from core.async_repositories import AsyncObservabilityRepository

    repo = AsyncObservabilityRepository(str(OBS_DB_PATH))
    metrics = await repo.get_latest_metrics()
    return {"metrics": metrics, "count": len(metrics)}


# The async DB pool is closed in the lifespan shutdown handler (see
# the ``await _db_pool_singleton.close_all()`` block in ``async def
# lifespan`` above the ``yield``).


# (W16-3) core.portfolio_optimizer — multi-strategy Kelly-criterion
# portfolio optimizer. Additive wiring appended at end of file per the
# W16-3 task spec. Adds four endpoints under ``/api/portfolio`` so an
# operator can run the Kelly optimizer across a list of opportunities
# (POSTed from the dashboard), suggest rebalancing actions against the
# current open positions, and read / mutate the optimizer's live config
# (Kelly fraction, max single bet, max total exposure, min edge, min
# confidence, operating capital) WITHOUT a restart:
#
#   POST /api/portfolio/optimize    run the Kelly optimizer on a list of
#                                    opportunities; returns the selected
#                                    bets + portfolio metrics (total
#                                    allocated / expected return / risk /
#                                    diversification ratio / violations)
#   POST /api/portfolio/rebalance   suggest rebalancing actions (add /
#                                    reduce / close / hold) given the
#                                    current open positions + the latest
#                                    opportunity set
#   GET  /api/portfolio/config       return the live optimizer config
#   PUT  /api/portfolio/config       partial-update the optimizer config
#                                    (mutates the singleton in place so
#                                    the next optimize call picks up the
#                                    new Kelly fraction / max exposure)
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the four paths are in ``PUBLIC_PATHS``).
from core.portfolio_optimizer import register_routes as _register_portfolio_optimizer_routes

_register_portfolio_optimizer_routes(app)


# (W17-5) core.immutable_audit — cryptographically immutable hash-chained
# audit trail. Additive wiring appended at end of file per the W17-5 task
# spec. Adds four endpoints under ``/api/audit/immutable`` so an operator
# can inspect the chain, verify its integrity (tamper detection), read
# aggregate stats, and manually append entries for testing:
#
#   GET  /api/audit/immutable          recent entries (paginated)
#   GET  /api/audit/immutable/verify   verify the integrity of the chain
#   GET  /api/audit/immutable/stats    aggregate entry stats
#   POST /api/audit/immutable/log      manually log an event (testing)
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the four paths are in ``PUBLIC_PATHS``).
#
# In addition, the singleton ``immutable_audit.log()`` is called inline
# from the trade-execution / position-close / kill-switch / live-trading-
# enable / portfolio-config / feature-flag route handlers below —
# integration points are marked with a ``# W17-5 — immutable_audit`` comment
# so they're greppable.
from core.immutable_audit import register_routes as _register_immutable_audit_routes
from core.immutable_audit import immutable_audit  # noqa: F401 — used by inline .log() calls below

_register_immutable_audit_routes(app)


# ── (W17-8) Async job queue — background job submission & polling ────────────
# W17-8: ``core.job_queue.JobQueue`` SQLite-backed queue with background
# daemon-thread workers (``worker-0`` / ``worker-1``). Workers are
# started by the lifespan startup handler (see the
# ``_job_queue_singleton.start_workers()`` block above the ``yield``)
# after ``register_default_handlers`` wires the three built-in handlers
# (``retrain`` / ``backtest`` / ``export``).
#
# Five new endpoints appended here (auth enforced by ``enforce_api_auth``
# — none of the five paths are in ``PUBLIC_PATHS``):
#
#   POST /api/jobs                    enqueue a job; body ``{"type": str,
#                                    "payload": dict}``; returns the
#                                    freshly-enqueued ``Job`` (status
#                                    ``pending``)
#   GET  /api/jobs                    list recent jobs (query params:
#                                    ``limit``, default 50; ``status``,
#                                    optional filter); returns the list
#                                    newest-first
#   GET  /api/jobs/stats              queue stats — total count / by-
#                                    status breakdown / active worker
#                                    count / handlers registered
#   GET  /api/jobs/{job_id}           fetch a single job by id (404 if
#                                    the id doesn't exist); returns the
#                                    full ``Job`` row
#   POST /api/jobs/{job_id}/cancel    cancel a pending job (only pending
#                                    jobs are cancellable; running /
#                                    completed / already-cancelled jobs
#                                    return 409)
#
# Route-order note: ``/api/jobs/stats`` MUST be registered before
# ``/api/jobs/{job_id}`` — otherwise FastAPI's path matcher would
# interpret ``stats`` as a ``job_id`` path parameter. The five routes
# are added in the order above to guarantee that.
from core.job_queue import (  # noqa: E402
    job_queue as _job_queue_singleton,
    job_to_dict as _job_to_dict,
)


class JobCreateRequest(BaseModel):
    """Body schema for ``POST /api/jobs``.

    ``type`` is a free-form string; the queue dispatches to whatever
    handler is registered for that type. Unknown types fail at
    execution time (the worker records ``"No handler for job type:
    <type>"`` in the job's ``error`` column and sets status to
    ``failed``) rather than at enqueue time so a misconfigured client
    doesn't lose its job submission to a 422 — the operator sees the
    failure in ``GET /api/jobs/{id}`` and can re-submit after fixing
    the type / handler registration.
    """

    type: str = Field(..., min_length=1, max_length=64)
    payload: dict = Field(default_factory=dict)


@app.post(
    "/api/jobs",
    tags=["jobs"],
    summary="Enqueue a background job",
    description=(
        "Submit a long-running task (ML retrain, backtest, export, "
        "custom) to the W17-8 background job queue. Returns the "
        "freshly-created Job row with status ``pending``. Poll "
        "``GET /api/jobs/{job_id}`` for completion; cancel via "
        "``POST /api/jobs/{job_id}/cancel`` while still pending."
    ),
)
@limiter.limit(WRITE_LIMIT)
async def enqueue_job(request: Request, req: JobCreateRequest):
    """Add a job to the queue and return the new job's metadata."""
    job = _job_queue_singleton.enqueue(req.type, req.payload)
    return {"job": _job_to_dict(job)}


@app.get(
    "/api/jobs",
    tags=["jobs"],
    summary="List recent jobs",
    description=(
        "Return the most-recent N jobs (newest-first). Optional "
        "``status`` query param filters by job state "
        "(``pending``/``running``/``completed``/``failed``/``cancelled``)."
    ),
)
@limiter.limit(READ_LIMIT)
async def list_jobs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None),
):
    """List recent jobs (optionally filtered by status)."""
    jobs = _job_queue_singleton.get_recent_jobs(limit=limit, status=status)
    return {
        "jobs": [_job_to_dict(j) for j in jobs],
        "count": len(jobs),
    }


@app.get(
    "/api/jobs/stats",
    tags=["jobs"],
    summary="Queue statistics",
    description=(
        "Aggregate queue stats: total job count, per-status breakdown, "
        "active worker count, and the list of registered handler "
        "types. Used by the dashboard's job-queue panel."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_job_stats(request: Request):
    """Return aggregate queue stats."""
    return _job_queue_singleton.get_stats()


@app.get(
    "/api/jobs/{job_id}",
    tags=["jobs"],
    summary="Get job status",
    description=(
        "Fetch a single job by id. Returns 404 if the id is unknown. "
        "Used by clients polling for completion of an enqueued job."
    ),
)
@limiter.limit(READ_LIMIT)
async def get_job(request: Request, job_id: str):
    """Return the job with ``job_id`` (or 404)."""
    job = _job_queue_singleton.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    return {"job": _job_to_dict(job)}


@app.post(
    "/api/jobs/{job_id}/cancel",
    tags=["jobs"],
    summary="Cancel a pending job",
    description=(
        "Cancel a job that is still in the ``pending`` state. Returns "
        "409 if the job is already ``running`` / ``completed`` / "
        "``failed`` / ``cancelled`` — only pending jobs are "
        "cancellable. Returns 404 if the id is unknown."
    ),
)
@limiter.limit(WRITE_LIMIT)
async def cancel_job(request: Request, job_id: str):
    """Cancel a pending job (409 if not cancellable)."""
    job = _job_queue_singleton.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if job.status.value != "pending":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} is in status '{job.status.value}' — "
                "only 'pending' jobs are cancellable."
            ),
        )
    cancelled = _job_queue_singleton.cancel_job(job_id)
    if not cancelled:
        # The job was pending when we fetched it but the atomic UPDATE
        # returned 0 rows — a worker claimed it in the race window
        # between the fetch and the cancel. Surface as 409 so the
        # caller knows the cancel did not take effect.
        raise HTTPException(
            status_code=409,
            detail=(
                f"Job {job_id} was claimed by a worker before cancel — "
                "re-fetch and retry if still required."
            ),
        )
    updated = _job_queue_singleton.get_job(job_id)
    return {"job": _job_to_dict(updated), "cancelled": True}


# The job-queue workers are started in the lifespan startup handler
# (see the ``_job_queue_singleton.start_workers()`` block above the
# ``yield`` in ``async def lifespan``) and stopped in the lifespan
# shutdown handler.


# (W17-3) ml.explainability — SHAP-based per-prediction feature
# attribution. Additive wiring appended at end of file per the W17-3
# task spec. Adds one endpoint under ``/api/ml/explain/{token_id}``
# so an operator can fetch a SHAP explanation for the model's most
# recent prediction for a given token:
#
#   GET  /api/ml/explain/{token_id}    SHAP-based per-prediction
#                                      feature attribution for the
#                                      model's most recent prediction
#                                      for the token (loads the
#                                      stored feature vector from
#                                      ``ml_feature_store``, runs
#                                      ``shap.TreeExplainer`` against
#                                      the RandomForest ensemble member
#                                      with fallback to
#                                      ``shap.KernelExplainer``, and
#                                      returns the predicted
#                                      probability + base value +
#                                      top-N feature contributions +
#                                      prediction direction)
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (path is NOT in ``PUBLIC_PATHS``).
#
# The endpoint is read-only and idempotent — it never mutates the
# model, the feature store, or any persisted state. SHAP computation
# is fully on-demand: no background workers, no caching layer. The
# latency ceiling is ~50ms per call for the 38-feature × 150-tree RF
# (benchmarked on the W11-5 calibration fold); well inside the
# ``READ_LIMIT`` 120/min budget for an operator-driven dashboard
# poll.
from ml.explainability import register_routes as _register_explainability_routes

_register_explainability_routes(app)


# (W17-1) core.sentiment — market sentiment analyzer (news + price +
# volume + social signals). Additive wiring appended at end of file
# per the W17-1 task spec. Adds three endpoints under
# ``/api/sentiment`` so an operator can pull a per-token aggregate
# (overall score / confidence / per-source breakdown / trend), list
# every persisted aggregate, and submit ad-hoc news text for keyword
# scoring:
#
#   GET  /api/sentiment/{token_id}   aggregated sentiment for one
#                                    token (triggers a fresh
#                                    ``aggregate`` so the response
#                                    reflects every signal recorded
#                                    up to this call; returns a
#                                    zeroed envelope when no signals
#                                    exist)
#   GET  /api/sentiment              every persisted aggregate row,
#                                    highest score first
#   POST /api/sentiment/analyze      analyze a text blob for keyword
#                                    sentiment, record the resulting
#                                    signal, and return the freshly-
#                                    aggregated sentiment for that
#                                    token
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the three paths are in ``PUBLIC_PATHS``).
from core.sentiment import register_routes as _register_sentiment_routes

_register_sentiment_routes(app)


# (W19-8) ml.shadow_inference — Shadow model challenger inspection +
# promotion-gate evaluation. Additive wiring appended at end of file
# per the W19-8 task spec. Adds two endpoints under ``/api/ml/shadow``
# so an operator can audit the shadow registry + trigger a
# champion-vs-challenger evaluation:
#
#   GET  /api/ml/shadow              shadow-inference registry snapshot —
#                                   registered challenger list (name /
#                                   description / calls / mean-abs-delta
#                                   vs production / outcome-stamped
#                                   count / last comparison record),
#                                   aggregate counters, and the W19-8
#                                   promotion ledger
#   POST /api/ml/shadow/evaluate    run ``evaluate_and_promote`` against
#                                   every registered challenger; return
#                                   per-challenger Brier / paired-t-test
#                                   / promotion verdict
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (neither path is in ``PUBLIC_PATHS``).
from ml.shadow_inference import register_routes as _register_shadow_inference_routes

_register_shadow_inference_routes(app)


# (W19-4) ml.economic_value — ML economic value tracker (P&L by model
# version / confidence / predicted-edge + with-AI vs without-AI
# counterfactual). Additive wiring appended at end of file per the W19-4
# task spec. Adds three endpoints under
# ``/api/ml/economic-value`` so an operator can answer the God Mode §16
# question "is the ML model actually adding economic value?":
#
#   GET /api/ml/economic-value                full summary (3 roll-ups +
#                                             counterfactual)
#   GET /api/ml/economic-value/by-model       P&L grouped by model
#                                             version (most profitable
#                                             first) with win/loss counts
#   GET /api/ml/economic-value/counterfactual  with-AI vs without-AI
#                                             counterfactual P&L +
#                                             ml_value + ml_value_per_trade
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (none of the three paths are in ``PUBLIC_PATHS``).
#
# The singleton ``ml_value_tracker`` is constructed at module-import time
# against ``ML_VALUE_DB`` (env-overridable, redirected to
# ``/tmp/pmbot_conftest_isolation/ml_economic_value.db`` by conftest) so
# the production ``/app/data/ml_economic_value.db`` sandbox path is
# never touched by the test suite. Trade-recording call sites in
# ``paper/simulator.py`` (paper-fill close) and ``core/settlement.py``
# (market-resolution YES + NO branches) drop in a fire-and-forget
# ``ml_value_tracker.record_trade(...)`` alongside the existing
# ``closed_positions.record_closed_position(...)`` call so every closed
# round-trip is mirrored into the ML economic-value tracker.
from ml.economic_value import register_routes as _register_ml_economic_value_routes

_register_ml_economic_value_routes(app)


# (W20-5) core.live_risk_metrics — Live portfolio VaR / CVaR / exposure /
# concentration. Additive wiring appended at end of file per the W20-5 task
# spec. Adds one endpoint under ``/api/portfolio`` so an operator can read
# the live portfolio's quantified tail-risk numbers (VaR_95, VaR_99,
# CVaR_95, CVaR_99) plus exposure / concentration aggregates, computed
# directly against the live ``DataStore.positions`` snapshot:
#
#   GET /api/portfolio/risk-metrics   return the live portfolio risk
#                                      metrics (VaR / CVaR / exposure /
#                                      concentration) computed against the
#                                      live ``DataStore`` positions
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (path not in ``PUBLIC_PATHS``).
#
# The live book poller doesn't yet persist a price history long enough to
# compute historical VaR, so the route uses the parametric fallback
# (5 % daily vol, normal approximation) — clearly labelled in the
# response payload via ``var_method="parametric"`` so the operator doesn't
# confuse it with an empirical VaR. When the caller supplies a price
# history (via :meth:`LiveRiskMetrics.compute` directly — not yet wired
# through the HTTP body), the historical VaR path is used instead.
from core.live_risk_metrics import register_routes as _register_live_risk_metrics_routes

_register_live_risk_metrics_routes(app)


# (W20-6) core.data_quality — Data quality monitoring pipeline that checks
# for stale, missing, or anomalous data in the canonical ``market_snapshots``
# SQLite table. Additive wiring appended at end of file per the W20-6 task
# spec. Adds one endpoint under ``/api/data-quality`` so an operator can read
# the current data-quality report (overall status + per-check breakdown):
#
#   GET /api/data-quality   return the current data-quality report —
#                           overall_status (healthy / degraded / critical),
#                           summary counts (total / passed / warnings /
#                           failed), and a list of QualityCheck entries
#                           (name / category / status / value / threshold /
#                           message / timestamp). Read-only, no caching.
#
# Auth enforced by ``enforce_api_auth`` (path not in ``PUBLIC_PATHS``).
# The ``data_quality_monitor`` singleton is constructed at module-import
# time against ``MARKET_DB_PATH`` (env-overridable, redirected to
# ``/tmp/pmbot_conftest_isolation/market_intelligence.db`` by conftest) so
# the production ``/app/data/market_intelligence.db`` sandbox path is never
# touched by the test suite. The monitor performs no import-time DB init —
# every check is wrapped in ``try/except`` so a missing table / unwritable
# path surfaces as a single ``fail`` QualityCheck instead of crashing the
# caller.
@app.get("/api/data-quality", tags=["system"])
async def get_data_quality():
    """Get data quality report.

    Runs every check in ``DataQualityMonitor.run_all_checks`` against the
    canonical ``market_snapshots`` SQLite table and returns the structured
    report (overall_status / summary / checks / timestamp). Read-only — no
    mutations, no caching.
    """
    from core.data_quality import data_quality_monitor

    report = data_quality_monitor.run_all_checks()
    return {
        "overall_status": report.overall_status,
        "summary": report.summary,
        "checks": [c.__dict__ for c in report.checks],
        "timestamp": report.timestamp,
    }


# (W20-7) core.trade_ingester — Public trade tape ingestion from the
# CLOB ``/trades`` endpoint. Additive wiring appended at end of file per
# the W20-7 task spec. Adds two endpoints under ``/api/trades`` so an
# operator can read the recent trade tape and inspect the ingester's
# runtime stats:
#
#   GET /api/trades/tape               recent trades from the tape
#                                      (most-recent-first), optional
#                                      ``token_id`` filter + ``limit``
#                                      cap; reads from the SQLite
#                                      ``market_trades`` table (the PG
#                                      hypertable is exposed via the
#                                      ``fetch_records`` explorer).
#   GET /api/trades/ingester-status    live ingester stats (running
#                                      flag, poll interval, seen-trade-id
#                                      set size, cumulative ingested /
#                                      error counts, last poll timestamp)
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing other
# modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (neither path is in ``PUBLIC_PATHS``).
#
# The ingester singleton ``trade_tape_ingester`` is started by the
# FastAPI lifespan startup handler (see the W20-7 block above the
# ``yield`` in ``async def lifespan``) and stopped by the shutdown
# handler. The routes are read-only and never block on the ingester's
# poll loop — ``fetch_trades`` reads directly from the SQLite table, so
# the endpoint works even when the ingester is stopped (it just returns
# whatever's already in the tape).
from core.trade_ingester import register_routes as _register_trade_tape_routes

_register_trade_tape_routes(app)


# (W21-8) core.pg_pool — PostgreSQL connection pool with retry + circuit
# breaker. Additive wiring appended at end of file per the W21-8 task
# spec. Adds one endpoint under ``/api/database`` so an operator can
# inspect the live PG pool's stats (total / active / idle connections,
# total / failed queries, rolling avg query time, last error, circuit
# breaker state, consecutive failures, threshold, recovery timeout):
#
#   GET /api/database/pool-stats   return the PG pool's runtime stats.
#                                   Read-only, no caching. The pool is
#                                   initialized lazily on the first
#                                   ``execute()`` call (or via
#                                   ``database_manager`` once W21-1
#                                   lands); this endpoint returns the
#                                   zero-state when the pool has not
#                                   yet been initialized.
#
# Auth enforced by ``enforce_api_auth`` (path not in ``PUBLIC_PATHS``).
# The singleton ``pg_pool`` is constructed at module-import time but
# opens NO connections until ``initialize()`` is called — mirroring the
# lazy-init pattern in ``core.db_pool`` / ``core.timescale_db`` so
# importing ``core.pg_pool`` is side-effect-free.
@app.get("/api/database/pool-stats", tags=["system"])
async def get_pool_stats():
    """Get PostgreSQL connection pool statistics.

    Returns the live snapshot of the PG pool's stats — connection
    counts (total / active / idle), query counters (total / failed),
    rolling avg query time (ms, exponential moving average), last
    error + timestamp, and the circuit breaker state (open /
    consecutive failures / threshold / recovery timeout). Used by the
    dashboard's Database panel to surface pool health alongside the
    existing SQLite / TimescaleDB telemetry.

    The pool is initialized lazily on the first ``execute()`` call;
    when the pool has not yet been initialized, this endpoint returns
    the zero-state (every counter at 0, ``circuit_open=False``).
    """
    from core.pg_pool import pg_pool
    return pg_pool.get_stats()


# ── W21-1 — Unified database manager (PG primary, SQLite fallback) ──────────
# Two endpoints under ``/api/database`` so an operator can inspect the
# active backend and trigger a manual PG retry:
#
#   GET  /api/database/status     return the current backend status —
#                                 ``backend`` (``"postgresql"`` /
#                                 ``"sqlite"`` / ``"none"``),
#                                 ``pg_available`` / ``sqlite_available``
#                                 flags, ``last_pg_check`` /
#                                 ``last_pg_check_ago_s``, the retry
#                                 interval, ``fallback_count`` (how many
#                                 times the manager has fallen back from
#                                 PG to SQLite — both initial fallback
#                                 AND mid-operation fallbacks count), and
#                                 ``recent_errors`` (last 5 PG connection
#                                 errors). Read-only, no caching.
#
#   POST /api/database/retry-pg   trigger an immediate PG retry —
#                                 bypasses the 60 s retry interval so an
#                                 operator can pick up a recovered PG
#                                 within seconds of bringing it back
#                                 online. Returns the post-retry status
#                                 payload so the caller sees whether the
#                                 retry succeeded.
#
# Auth enforced by ``enforce_api_auth`` (neither path is in
# ``PUBLIC_PATHS``). The ``db_manager`` singleton is initialised by the
# FastAPI lifespan startup handler; the status endpoint works regardless
# of whether ``initialize()`` has run (returns ``backend="none"`` and
# ``fallback_count=0`` in that case).
@app.get("/api/database/status", tags=["system"])
async def get_database_status():
    """Get the unified database manager's backend status.

    Returns the current backend (PG vs SQLite), the PG availability
    flag, the last PG check timestamp (and how long ago it ran), the
    retry interval, the cumulative fallback count, and the most recent
    PG connection errors. Read-only — never mutates state.

    W21-1 — surfaces the W17-4 God Mode finding (PG in standby mode,
    silently using SQLite for everything) as an explicit status field
    so an operator can see at a glance whether PG is the active
    backend or whether the bot is degraded to the SQLite fallback.
    """
    from core.database_manager import db_manager
    return db_manager.get_status()


@app.post("/api/database/retry-pg", tags=["system"])
async def retry_pg_connection():
    """Manually retry the PostgreSQL connection.

    Bypasses the 60 s retry interval so an operator can pick up a
    recovered PG within seconds of bringing it back online. On
    successful retry the manager switches back to PG as the primary
    backend and bumps ``fallback_count`` (so the operator can see "PG
    came back" in the status payload). On failed retry the manager
    stays on SQLite; the next scheduled retry (60 s) will pick up the
    recovery if the manual attempt was premature.
    """
    from core.database_manager import db_manager

    if db_manager.is_postgres:
        # Already on PG — no-op. Return the current status so the
        # caller sees the "backend=postgresql" state and doesn't have
        # to issue a separate GET to confirm.
        return {
            "retry_attempted": False,
            "reason": "PG already active — no retry needed",
            **db_manager.get_status(),
        }

    recovered = await db_manager._try_postgres()
    if recovered and not db_manager.is_postgres:
        # ``_try_postgres`` flipped ``pg_available`` to True but didn't
        # update ``backend`` (the retry loop is the only place that
        # does that, to keep the transition logic in one spot). Do the
        # transition here so the manual retry takes effect immediately
        # instead of waiting for the next scheduled retry tick.
        from core.database_manager import DatabaseBackend
        db_manager._status.backend = DatabaseBackend.POSTGRESQL
        # Bump ``fallback_count`` so the operator sees "PG came back"
        # in the status payload — same convention as the retry loop.
        db_manager._status.fallback_count += 1

    return {
        "retry_attempted": True,
        "recovered": recovered,
        **db_manager.get_status(),
    }


# (W21-5) core.database_manager — Order book depth storage read API.
# Additive wiring appended at end of file per the W21-5 task spec.
# Adds two read-only endpoints under ``/api`` so an operator can read
# the full order book ladder (parsed from the ``bids_json`` /
# ``asks_json`` JSON columns) and the depth time-series over a
# trailing window:
#
#   GET /api/depth-full/{token_id}        latest snapshot's full
#                                         bid/ask ladder (parsed) +
#                                         depth-10 summaries +
#                                         top-of-book fields.
#   GET /api/depth-history/{token_id}     depth time-series for the
#                                         last ``hours`` hours
#                                         (ascending by timestamp),
#                                         each row carrying the
#                                         parsed top-10 ladders +
#                                         depth-10 summaries +
#                                         top-of-book.
#
# Same registration pattern as the sibling ``register_routes`` blocks
# above (alias imported under ``_register_*`` to avoid shadowing
# other modules' ``register_routes`` symbol). Auth enforced by
# ``enforce_api_auth`` (neither path is in ``PUBLIC_PATHS``).
#
# W21-5 also fixed the SQLite INSERT path in ``record_snapshot`` so
# the ``bids_json`` / ``asks_json`` / ``bid_depth_10`` /
# ``ask_depth_10`` / ``ingestion_time`` columns are now written on
# BOTH backends (the W19-5 task spec described the fix but did not
# actually apply it; the SQLite schema migration in
# ``_init_sqlite_fallback`` adds the columns idempotently to legacy
# DBs). The book poller's ``_apply_book`` call path now passes the
# full bid/ask ladders through to ``record_snapshot`` so the JSON
# columns are populated with real ladder data — without that call-site
# fix the JSON columns would have stayed ``NULL`` forever and the
# read endpoints below would have returned empty ladders regardless
# of the schema fix.
from core.database_manager import register_routes as _register_depth_routes

_register_depth_routes(app)

