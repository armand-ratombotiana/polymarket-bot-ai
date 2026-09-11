"""GraphQL schema for the Polymarket bot API.

Provides a flexible query interface that lets clients request exactly
the fields they need, avoiding over-fetching and under-fetching.

Strawberry's default ``auto_camel_case=True`` config is in effect: the
Python snake_case field / argument names are surfaced as camelCase in
the GraphQL schema (e.g. ``token_id`` → ``tokenId``,
``unrealized_pnl`` → ``unrealizedPnl``,
``unacknowledged_only`` → ``unacknowledgedOnly``,
``ml_metrics`` → ``mlMetrics``, ``cache_stats`` → ``cacheStats``). This
matches the GraphQL community convention (every major client library
defaults to camelCase) and lets GraphiQL / Apollo Sandbox / Postman
auto-complete without surprises.

Example queries (POST ``/graphql``, body ``{"query": "..."}``):
    query { positions { tokenId side size unrealizedPnl } }
    query { mlMetrics { auc brier } }
    query { alerts(unacknowledgedOnly: true) { name severity message } }
    query { cacheStats { name size hits misses hitRate } }
"""
import logging
import time
from typing import Optional

import strawberry

logger = logging.getLogger(__name__)


@strawberry.type
class Position:
    token_id: str
    side: str
    size: float
    avg_price: float
    current_price: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    realized_pnl: Optional[float] = None
    opened_at: Optional[str] = None
    strategy: Optional[str] = None


@strawberry.type
class Order:
    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    status: str
    created_at: Optional[str] = None


@strawberry.type
class Trade:
    trade_id: Optional[str]
    token_id: str
    side: str
    price: float
    size: float
    timestamp: str
    strategy: Optional[str] = None


@strawberry.type
class MLMetrics:
    auc: Optional[float] = None
    brier: Optional[float] = None
    log_loss: Optional[float] = None
    accuracy: Optional[float] = None
    version: Optional[str] = None


@strawberry.type
class Alert:
    alert_id: str
    timestamp: float
    category: str
    name: str
    severity: str
    message: str
    acknowledged: bool


@strawberry.type
class Health:
    status: str
    mode: Optional[str] = None
    balance: Optional[float] = None
    uptime: Optional[float] = None


@strawberry.type
class MarketSummary:
    token_id: str
    question: Optional[str] = None
    yes_price: Optional[float] = None
    no_price: Optional[float] = None
    spread: Optional[float] = None
    volume: Optional[float] = None


@strawberry.type
class CacheStats:
    name: str
    size: int
    hits: int
    misses: int
    hit_rate: float


@strawberry.type
class Query:
    @strawberry.field
    def health(self) -> Health:
        """Liveness probe mirroring the REST ``GET /api/health`` payload.

        Returns ``status="error"`` on any backend lookup failure so a
        monitoring client can surface the failure as a degraded GraphQL
        response instead of a 5xx (a missing submodule shouldn't take
        down the entire GraphQL surface).
        """
        # Call the existing health check
        try:
            from api.server import store, settings

            return Health(
                status="healthy",
                mode=getattr(settings, "trading_mode", "paper"),
                balance=getattr(store, "paper_balance", None),
                uptime=max(0.0, time.time() - getattr(store, "session_start", time.time())),
            )
        except Exception as e:  # noqa: BLE001 — defensive: schema must not raise
            logger.error("GraphQL health error: %s", e)
            return Health(status="error")

    @strawberry.field
    def positions(self) -> list[Position]:
        """Open positions from the global ``DataStore`` singleton.

        Translates the ``store.positions`` dict (keyed by ``token_id``,
        values are ``core.data_store.Position`` dataclass instances) into
        the GraphQL ``Position`` shape. Returns an empty list on any
        backend error so a partial failure (e.g. the ``store`` singleton
        not being importable in a stripped-down test environment) yields
        an empty array rather than a 5xx GraphQL error.
        """
        try:
            from api.server import store

            positions_data = store.get_positions() if hasattr(store, "get_positions") else []
            return [
                Position(
                    token_id=p.get("token_id", ""),
                    side=p.get("side", ""),
                    size=p.get("size", 0),
                    avg_price=p.get("avg_price", 0),
                    current_price=p.get("current_price"),
                    unrealized_pnl=p.get("unrealized_pnl"),
                    realized_pnl=p.get("realized_pnl"),
                    opened_at=p.get("opened_at"),
                    strategy=p.get("strategy"),
                )
                for p in positions_data
            ]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL positions error: %s", e)
            return []

    @strawberry.field
    def orders(self) -> list[Order]:
        """Open orders from the global ``DataStore`` singleton."""
        try:
            from api.server import store

            orders_data = store.get_orders() if hasattr(store, "get_orders") else []
            return [
                Order(
                    order_id=o.get("order_id", ""),
                    token_id=o.get("token_id", ""),
                    side=o.get("side", ""),
                    price=o.get("price", 0),
                    size=o.get("size", 0),
                    status=o.get("status", ""),
                    created_at=o.get("created_at"),
                )
                for o in orders_data
            ]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL orders error: %s", e)
            return []

    @strawberry.field
    def trades(self, limit: int = 50) -> list[Trade]:
        """Recent fills from the global ``DataStore`` singleton.

        ``limit`` caps the returned trade count (default 50, mirroring
        the REST ``GET /api/trades`` default). The resolver passes the
        raw integer through to ``store.get_trades`` so the data layer
        owns the clamping / slicing policy — the GraphQL layer never
        re-implements trade-list pagination in two places.
        """
        try:
            from api.server import store

            trades_data = store.get_trades(limit) if hasattr(store, "get_trades") else []
            return [
                Trade(
                    trade_id=t.get("trade_id"),
                    token_id=t.get("token_id", ""),
                    side=t.get("side", ""),
                    price=t.get("price", 0),
                    size=t.get("size", 0),
                    timestamp=str(t.get("timestamp", "")),
                    strategy=t.get("strategy"),
                )
                for t in trades_data
            ]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL trades error: %s", e)
            return []

    @strawberry.field
    def ml_metrics(self) -> MLMetrics:
        """Current ML ensemble benchmark metrics.

        Reads from the ``ml_model`` singleton. Returns an empty
        ``MLMetrics`` (all fields ``None``) on any backend failure so a
        cold-start environment without a trained model still resolves
        the query — the client sees ``null`` values rather than a 5xx.
        """
        try:
            from ml.model import ml_model

            if hasattr(ml_model, "get_metrics"):
                metrics = ml_model.get_metrics()
            else:
                # Fallback: assemble the metrics dict from the
                # ``MarketMLModel`` public attributes (populated by
                # ``fit_initial`` and the rolling-update path).
                metrics = {
                    "auc": getattr(ml_model, "roc_auc", None),
                    "brier": getattr(ml_model, "brier_score", None),
                    "log_loss": getattr(ml_model, "log_loss_score", None),
                    "accuracy": None,
                    "version": getattr(ml_model, "training_source", None),
                }
            return MLMetrics(
                auc=metrics.get("auc"),
                brier=metrics.get("brier"),
                log_loss=metrics.get("log_loss"),
                accuracy=metrics.get("accuracy"),
                version=metrics.get("version"),
            )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL ml_metrics error: %s", e)
            return MLMetrics()

    @strawberry.field
    def alerts(
        self, unacknowledged_only: bool = False, limit: int = 50
    ) -> list[Alert]:
        """Recent alerts from the ``alert_engine`` singleton.

        ``unacknowledged_only=True`` filters to ``acknowledged = 0``
        rows (mirrors the REST ``GET /api/alerts?unacknowledged_only=true``
        query parameter).
        """
        try:
            from core.alerting import alert_engine

            alerts_data = alert_engine.get_recent(limit, unacknowledged_only)
            return [
                Alert(
                    alert_id=a.get("alert_id", ""),
                    timestamp=a.get("timestamp", 0),
                    category=a.get("category", ""),
                    name=a.get("name", ""),
                    severity=a.get("severity", "info"),
                    message=a.get("message", ""),
                    acknowledged=bool(a.get("acknowledged", False)),
                )
                for a in alerts_data
            ]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL alerts error: %s", e)
            return []

    @strawberry.field
    def cache_stats(self) -> list[CacheStats]:
        """Per-cache hit/miss snapshot from the ``core.cache`` singletons.

        Mirrors the REST ``GET /api/cache/stats`` payload — one entry
        per TTLCache singleton (markets / ml_metrics / analytics).
        """
        try:
            from core.cache import (
                analytics_cache,
                markets_cache,
                ml_metrics_cache,
            )

            caches = [markets_cache, ml_metrics_cache, analytics_cache]
            return [
                CacheStats(
                    name=c.stats()["name"],
                    size=c.stats()["size"],
                    hits=c.stats()["hits"],
                    misses=c.stats()["misses"],
                    hit_rate=c.stats()["hit_rate"],
                )
                for c in caches
            ]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("GraphQL cache_stats error: %s", e)
            return []


schema = strawberry.Schema(query=Query)
