"""Pydantic response models for OpenAPI documentation.

These models describe the *actual* shapes returned by the most-used backend
routes so that the auto-generated ``/openapi.json`` schema (consumed by
Swagger UI at ``/docs`` and ReDoc at ``/redoc``) surfaces meaningful response
schemas instead of the generic ``{}`` placeholder FastAPI falls back to when
no ``response_model`` is declared on the route.

Design notes
------------
* The models match the EXACT return shapes of their corresponding routes
  (verified against ``api/server.py`` line-by-line) so attaching
  ``response_model=<ModelName>`` to a route never filters out a field the
  route already returns. FastAPI validates the response against the model
  on every request — a mismatch would 500 the route.
* Spec-intended future fields (e.g. ``mode``/``uptime``/``balance`` on
  :class:`HealthResponse`) are declared ``Optional`` with ``default=None``
  and surfaced in the docs as "future / not yet populated" so callers can
  code against the documented future shape without the route silently
  dropping them today. Routes that attach these models pass
  ``response_model_exclude_unset=True`` so the unset (default) fields do
  NOT appear in the wire response — keeping the actual payload small and
  the contract honest.
* List-returning routes (``/api/positions``, ``/api/orders``,
  ``/api/trades``) actually return wrapper objects of the shape
  ``{<key>: [...], count: N, ...}`` — NOT bare JSON arrays. The wrapper
  models (:class:`PositionsResponse`, :class:`OrdersResponse`,
  :class:`TradesResponse`) capture that shape so the docs show the full
  envelope (which the dashboard relies on for the ``count`` field).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ── Generic error envelope ──────────────────────────────────────────────────


class ErrorResponse(BaseModel):
    """Standard error envelope returned by all 4xx / 5xx responses."""

    detail: str = Field(..., description="Human-readable error message")
    path: Optional[str] = Field(
        None,
        description="Request path that triggered the error (set by the global exception handler)",
    )


# ── Health / system ─────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Liveness probe response (``GET /api/health``).

    The route returns ``status``, ``timestamp``, and ``paper`` today. The
    remaining fields (``mode``, ``uptime``, ``balance``) are surfaced in
    the OpenAPI schema as the intended future shape — they are
    ``Optional`` and ``None`` by default, and the route passes
    ``response_model_exclude_unset=True`` so absent fields are dropped
    from the wire payload (no spurious ``"mode": null`` keys).
    """

    status: str = Field(..., description="Liveness status", examples=["ok"])
    timestamp: float = Field(
        ..., description="Server time as Unix epoch seconds"
    )
    paper: bool = Field(
        ..., description="True when paper trading mode is active"
    )
    mode: Optional[str] = Field(
        None, description="Canonical trading mode (paper | live)"
    )
    uptime: Optional[float] = Field(
        None, description="Server uptime in seconds (future field)"
    )
    balance: Optional[float] = Field(
        None, description="Current account balance in USD (future field)"
    )


# ── Trading ─────────────────────────────────────────────────────────────────


class PositionItem(BaseModel):
    """A single open position row inside :class:`PositionsResponse`."""

    token_id: str = Field(..., description="Polymarket condition token id")
    slug: str = Field("", description="Market slug (human-readable market id)")
    yes_shares: float = Field(..., description="Number of YES shares held")
    avg_entry_price: float = Field(
        ..., description="Volume-weighted average entry price"
    )
    total_invested: float = Field(..., description="USD invested at entry")
    realised_pnl: float = Field(
        ..., description="Cumulative realised P&L on this token"
    )


class PositionsResponse(BaseModel):
    """Wrapper for ``GET /api/positions``.

    The route returns a dict (not a bare list) so callers can read the
    ``count`` and ``daily_pnl`` fields without an extra round-trip.
    """

    positions: list[PositionItem] = Field(
        default_factory=list, description="Open positions (may be empty)"
    )
    count: int = Field(..., description="Number of positions returned")
    daily_pnl: float = Field(..., description="Realised P&L for the trading day")


class OrderItem(BaseModel):
    """A single open order row inside :class:`OrdersResponse`."""

    order_id: str = Field(..., description="Order id (exchange-issued or paper)")
    token_id: str = Field(..., description="Polymarket condition token id")
    slug: str = Field("", description="Market slug (human-readable market id)")
    side: str = Field(..., description="Order side (BUY | SELL)")
    price: float = Field(..., description="Limit price in USD (0, 1)")
    size: float = Field(..., description="Order size in shares")
    size_matched: float = Field(
        ..., description="Cumulative filled size in shares"
    )
    strategy: str = Field(..., description="Strategy that placed the order")
    paper: bool = Field(..., description="True when this is a paper-trade order")
    created_at: float = Field(..., description="Order creation time (epoch s)")


class OrdersResponse(BaseModel):
    """Wrapper for ``GET /api/orders``."""

    orders: list[OrderItem] = Field(
        default_factory=list, description="Open orders (may be empty)"
    )
    count: int = Field(..., description="Number of orders returned")


class TradeItem(BaseModel):
    """A single trade row inside :class:`TradesResponse`."""

    trade_id: Optional[str] = Field(
        None, description="Trade id (may be None for legacy rows)"
    )
    slug: str = Field("", description="Market slug (human-readable market id)")
    side: str = Field(..., description="Trade side (BUY | SELL)")
    price: float = Field(..., description="Fill price in USD (0, 1)")
    size: float = Field(..., description="Fill size in shares")
    pnl: float = Field(..., description="Realised P&L on this trade (0 if open)")
    strategy: str = Field(..., description="Strategy that produced the trade")
    paper: bool = Field(..., description="True when this is a paper-trade fill")
    timestamp: float = Field(..., description="Fill time (epoch s)")


class TradesResponse(BaseModel):
    """Wrapper for ``GET /api/trades``.

    The route returns a dict (not a bare list) so callers can read the
    ``count`` field without an extra round-trip. W16-5 — the route also
    returns cursor-pagination fields (``next_cursor`` / ``has_more``)
    so a dashboard can page through the trade history without
    re-fetching the entire list on every poll. Both fields are
    Optional / defaulted so the OpenAPI schema documents them but the
    route still validates even if a future caller forgets to populate
    them.
    """

    trades: list[TradeItem] = Field(
        default_factory=list, description="Recent trades (newest first)"
    )
    count: int = Field(..., description="Number of trades returned")
    next_cursor: Optional[str] = Field(
        None,
        description=(
            "Opaque base64 cursor to pass as ?cursor=... on the next "
            "request to fetch the following page. ``None`` on the last "
            "page (when ``has_more`` is False). W16-5."
        ),
    )
    has_more: bool = Field(
        False,
        description="True when at least one more trade exists beyond this page.",
    )


# ── ML diagnostics ─────────────────────────────────────────────────────────


class MLMetricsResponse(BaseModel):
    """Full quantitative diagnostics for the ML ensemble (``GET /api/ml/metrics``).

    Captures the Brier score, ROC AUC, log loss, ECE, Sharpe ratio, online
    update count, model version, and the structured ``meta_learner`` /
    ``drift`` sub-objects returned by the route.
    """

    model_type: str = Field(
        ...,
        description="Model family description",
        examples=["4-Member Calibrated Ensemble + Level-2 Stacking Meta-Learner"],
    )
    brier_score: Optional[float] = Field(
        None, description="Brier score (lower is better)"
    )
    roc_auc: Optional[float] = Field(
        None, description="ROC AUC score (higher is better)"
    )
    log_loss: Optional[float] = Field(None, description="Log loss")
    ece: Optional[float] = Field(
        None, description="Expected Calibration Error"
    )
    sharpe_ratio: Optional[float] = Field(
        None, description="Per-trade Sharpe ratio"
    )
    n_online_updates: int = Field(
        ..., description="Cumulative online SGD updates applied"
    )
    last_trained: Optional[float] = Field(
        None, description="Last full retrain timestamp (epoch s)"
    )
    training_source: str = Field(
        ..., description="Where the training data came from"
    )
    n_real_samples: int = Field(..., description="Real labeled samples seen")
    n_synthetic_samples: int = Field(
        ..., description="Synthetic labeled samples seen"
    )
    adaptive_weights: Optional[dict[str, Any]] = Field(
        None, description="Per-member adaptive ensemble weights"
    )
    meta_learner: Optional[dict[str, Any]] = Field(
        None, description="Stacking meta-learner summary"
    )
    drift: Optional[dict[str, Any]] = Field(
        None, description="Drift detector status report"
    )
    feature_importances: Optional[dict[str, Any]] = Field(
        None, description="Per-feature importance scores"
    )
    reliability_curve: Optional[list[Any]] = Field(
        None, description="Calibration reliability curve points"
    )
    model_ready: bool = Field(..., description="True when the ensemble is trained")
    model_version: Optional[str] = Field(
        None, description="Active model version string"
    )
    registry_summary: Optional[dict[str, Any]] = Field(
        None, description="Model registry summary"
    )
    # W11-5 — post-hoc probability calibration (Platt / isotonic) metrics.
    # Optional because older snapshots cached before the calibrator landed
    # may not include the key; ``response_model_exclude_unset=True`` drops
    # it from the wire payload when absent.
    calibration: Optional[dict[str, Any]] = Field(
        None,
        description="Post-hoc probability calibration (Platt / isotonic) metrics",
    )


# ── Alerting ────────────────────────────────────────────────────────────────


class AlertResponse(BaseModel):
    """A single alert row (W10-7 alerting system).

    Matches the ``Alert`` dataclass in ``core/alerting.py`` (``asdict()``-serialized
    by the alerting routes under ``/api/alerts/*``).
    """

    alert_id: str = Field(..., description="Unique alert id")
    timestamp: float = Field(..., description="Alert fire time (epoch s)")
    category: str = Field(
        ..., description="Alert category (risk | ml | system | data | execution)"
    )
    name: str = Field(..., description="Rule name that fired the alert")
    severity: str = Field(
        ..., description="Severity (info | warning | critical | error)"
    )
    message: str = Field(..., description="Human-readable alert message")
    value: Optional[float] = Field(
        None, description="Observed metric value that crossed the threshold"
    )
    threshold: Optional[float] = Field(
        None, description="Threshold the value crossed"
    )
    metadata: Optional[dict[str, Any]] = Field(
        default_factory=dict, description="Arbitrary key-value metadata"
    )
    acknowledged: bool = Field(False, description="True when acknowledged")


# ── Cache stats (informational — surfaced by /api/system/health and others) ──


class CacheStatsResponse(BaseModel):
    """In-memory cache statistics for an LRU/TTL cache."""

    name: str = Field(..., description="Cache name")
    size: int = Field(..., description="Current entry count")
    max_size: int = Field(..., description="Maximum entry count")
    hits: int = Field(..., description="Cache hit count")
    misses: int = Field(..., description="Cache miss count")
    hit_rate: float = Field(..., description="Hit rate (0.0–1.0)")
    default_ttl: float = Field(..., description="Default TTL in seconds")


__all__ = [
    "AlertResponse",
    "CacheStatsResponse",
    "ErrorResponse",
    "HealthResponse",
    "MLMetricsResponse",
    "OrderItem",
    "OrdersResponse",
    "PositionItem",
    "PositionsResponse",
    "TradeItem",
    "TradesResponse",
]
