"""Prometheus metrics for the Polymarket bot.

Exposes counters, gauges, and histograms for:
- HTTP request count, latency, status codes
- Trading: orders placed, fills, P&L
- ML: predictions made, drift PSI, model version
- System: memory, DB sizes, cache hit rates

W13-1 — Prometheus-compatible ``/metrics`` endpoint.

Design notes
------------
* All metric instances are module-level singletons constructed at first
  import (the ``prometheus_client`` library handles thread-safety
  internally via a ``threading.Lock`` per-collector). Subsequent
  imports of this module (test reload, dev-server hot-reload) return
  the SAME singletons — the registry de-duplicates on metric name +
  label-set.
* The metric namespace prefix ``polymarket_`` prevents collisions with
  any other Python process sharing the same Prometheus instance (e.g.
  a Celery worker scraping the same registry).
* Label cardinality is intentionally low: HTTP metrics are labelled
  by ``method`` (≤9 values), ``endpoint`` (paths from the OpenAPI
  schema, <80 values), and ``status`` (HTTP status code, ~60 values).
  Trading / ML metrics are labelled by ``side`` (2 values) /
  ``strategy`` (~10 values) / ``cache_name`` (6 values) /
  ``db_name`` (~10 values) / ``severity`` (3 values). Total series
  count is bounded under ~2k — well within Prometheus's default
  2-million-active-series budget per server.
* ``CONTENT_TYPE_LATEST`` is re-exported here so the FastAPI route
  handler in ``api/server.py`` can ``from core.prometheus_metrics import
  CONTENT_TYPE_LATEST`` without an extra import line.
"""
from __future__ import annotations

import logging
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Info,
    generate_latest,
)

logger = logging.getLogger(__name__)

# === HTTP metrics ===
http_requests_total = Counter(
    'polymarket_http_requests_total',
    'Total HTTP requests',
    ['method', 'endpoint', 'status']
)

http_request_duration_seconds = Histogram(
    'polymarket_http_request_duration_seconds',
    'HTTP request duration in seconds',
    ['method', 'endpoint'],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0]
)

http_requests_in_progress = Gauge(
    'polymarket_http_requests_in_progress',
    'Number of HTTP requests currently in progress'
)

# === Trading metrics ===
orders_placed_total = Counter(
    'polymarket_orders_placed_total',
    'Total orders placed',
    ['side', 'strategy']
)

orders_filled_total = Counter(
    'polymarket_orders_filled_total',
    'Total orders filled',
    ['side', 'strategy']
)

trades_total = Counter(
    'polymarket_trades_total',
    'Total trades executed'
)

realized_pnl = Gauge(
    'polymarket_realized_pnl_usd',
    'Realized P&L in USD'
)

unrealized_pnl = Gauge(
    'polymarket_unrealized_pnl_usd',
    'Unrealized P&L in USD'
)

paper_balance = Gauge(
    'polymarket_paper_balance_usd',
    'Paper trading account balance in USD'
)

open_positions = Gauge(
    'polymarket_open_positions',
    'Number of open positions'
)

open_orders = Gauge(
    'polymarket_open_orders',
    'Number of open orders'
)

# === ML metrics ===
ml_predictions_total = Counter(
    'polymarket_ml_predictions_total',
    'Total ML predictions made'
)

ml_model_version = Info(
    'polymarket_ml_model',
    'ML model information'
)

ml_drift_psi = Gauge(
    'polymarket_ml_drift_psi',
    'ML model drift (Population Stability Index)'
)

ml_brier_score = Gauge(
    'polymarket_ml_brier_score',
    'ML model Brier score'
)

ml_roc_auc = Gauge(
    'polymarket_ml_roc_auc',
    'ML model ROC AUC'
)

# === System metrics ===
cache_hits_total = Counter(
    'polymarket_cache_hits_total',
    'Cache hits',
    ['cache_name']
)

cache_misses_total = Counter(
    'polymarket_cache_misses_total',
    'Cache misses',
    ['cache_name']
)

db_size_bytes = Gauge(
    'polymarket_db_size_bytes',
    'SQLite database file size in bytes',
    ['db_name']
)

alerts_active = Gauge(
    'polymarket_alerts_active',
    'Number of unacknowledged alerts',
    ['severity']
)

# === API token auth ===
auth_failures_total = Counter(
    'polymarket_auth_failures_total',
    'Total authentication failures'
)

rate_limit_hits_total = Counter(
    'polymarket_rate_limit_hits_total',
    'Total rate limit hits (429 responses)',
    ['endpoint']
)

# === Risk gate failures (W18-6 — P0-C06 MTM fail-closed) ===
# Incremented every time the mark-to-market risk gate in
# ``risk/manager.py::_check_order_impl`` (section 6e) cannot compute the
# portfolio's marked exposure and therefore FAILS CLOSED — blocking
# every subsequent order until the price feed / MTM module is repaired.
# A non-zero rate on this counter is a P0 trading halt: operators must
# investigate ``store.order_books`` (missing mid quotes), the MTM module
# (``core.portfolio_mark_to_market``), or position integrity before
# resuming trading. Mirrors the contract of ``auth_failures_total``
# (best-effort increment — failures inside the metrics pipeline itself
# are swallowed at the call site so a metrics hiccup can never break a
# risk-gate decision).
mtm_gate_failures_total = Counter(
    'polymarket_mtm_gate_failures_total',
    'Total MTM risk-gate fail-closed events (every order blocked until '
    'price feed / MTM module is repaired — investigate immediately)',
)


# === Functions ===
def record_request(method: str, endpoint: str, status: int, duration: float) -> None:
    """Record an HTTP request.

    Called from the FastAPI request-logging middleware on every response
    (200, 4xx, 5xx, OPTIONS preflight — all of it). The ``status`` arg
    is the final HTTP status code; ``duration`` is wall-clock seconds
    elapsed between ``request_started`` and ``response_sent``.

    Best-effort: any error in the prometheus call path is swallowed so
    a metrics pipeline hiccup can never break a request — mirrors the
    contract of every other observability helper in this codebase
    (``core.observability`` / ``core.audit_logger``).
    """
    try:
        http_requests_total.labels(
            method=method,
            endpoint=endpoint,
            status=str(status),
        ).inc()
        http_request_duration_seconds.labels(
            method=method,
            endpoint=endpoint,
        ).observe(duration)
    except Exception:  # pragma: no cover — defensive: metrics must never break a request
        logger.debug("[prometheus] record_request failed", exc_info=True)


def record_trade(side: str, strategy: str, filled: bool = False) -> None:
    """Record a trade / order placement.

    Increments ``trades_total`` and ``orders_placed_total`` on every
    call; additionally increments ``orders_filled_total`` when
    ``filled=True`` (the order actually executed against the book
    rather than just resting open).
    """
    try:
        trades_total.inc()
        orders_placed_total.labels(side=side, strategy=strategy).inc()
        if filled:
            orders_filled_total.labels(side=side, strategy=strategy).inc()
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] record_trade failed", exc_info=True)


def update_balance(balance: float, realized: float = 0, unrealized: float = 0) -> None:
    """Update balance gauges.

    ``balance`` is the current paper-trading virtual balance; ``realized``
    and ``unrealized`` are optional P&L rollups (the gauges default to
    their last-set value when an arg is zero — callers that only track
    the headline balance can leave them at 0).
    """
    try:
        paper_balance.set(balance)
        if realized:
            realized_pnl.set(realized)
        if unrealized:
            unrealized_pnl.set(unrealized)
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] update_balance failed", exc_info=True)


def update_ml_metrics(
    version: str,
    psi: float = 0,
    brier: float = 0,
    auc: float = 0,
) -> None:
    """Update ML metrics.

    Called periodically by the training orchestrator / drift detector
    so a Grafana panel can correlate a PSI spike with a Brier / ROC AUC
    regression and trigger an alert before the model degrades past the
    kill threshold (PSI=0.25 is the standard drift-action boundary).
    """
    try:
        ml_model_version.info({'version': version})
        ml_drift_psi.set(psi)
        ml_brier_score.set(brier)
        ml_roc_auc.set(auc)
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] update_ml_metrics failed", exc_info=True)


def record_cache_hit(cache_name: str) -> None:
    """Increment the cache-hit counter for the given cache."""
    try:
        cache_hits_total.labels(cache_name=cache_name).inc()
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] record_cache_hit failed", exc_info=True)


def record_cache_miss(cache_name: str) -> None:
    """Increment the cache-miss counter for the given cache."""
    try:
        cache_misses_total.labels(cache_name=cache_name).inc()
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] record_cache_miss failed", exc_info=True)


def record_auth_failure() -> None:
    """Increment the auth-failure counter (called from ``enforce_api_auth``)."""
    try:
        auth_failures_total.inc()
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] record_auth_failure failed", exc_info=True)


def record_rate_limit_hit(endpoint: str) -> None:
    """Increment the rate-limit-hit counter (called from ``rate_limit_handler``)."""
    try:
        rate_limit_hits_total.labels(endpoint=endpoint).inc()
    except Exception:  # pragma: no cover
        logger.debug("[prometheus] record_rate_limit_hit failed", exc_info=True)


def get_metrics() -> bytes:
    """Return Prometheus-format metrics payload (bytes).

    The returned ``bytes`` payload is the canonical Prometheus text
    exposition format — the FastAPI route wraps it in a ``Response``
    with ``media_type=CONTENT_TYPE_LATEST`` so a scraper like
    ``prom/prometheus`` correctly parses the histogram summaries and
    counter increments.
    """
    return generate_latest()


__all__ = [
    # Re-export the constant so the route handler doesn't need to
    # import directly from ``prometheus_client`` (single source of truth).
    "CONTENT_TYPE_LATEST",
    # Metric singletons (advanced callers can import + observe directly).
    "http_requests_total",
    "http_request_duration_seconds",
    "http_requests_in_progress",
    "orders_placed_total",
    "orders_filled_total",
    "trades_total",
    "realized_pnl",
    "unrealized_pnl",
    "paper_balance",
    "open_positions",
    "open_orders",
    "ml_predictions_total",
    "ml_model_version",
    "ml_drift_psi",
    "ml_brier_score",
    "ml_roc_auc",
    "cache_hits_total",
    "cache_misses_total",
    "db_size_bytes",
    "alerts_active",
    "auth_failures_total",
    "rate_limit_hits_total",
    "mtm_gate_failures_total",
    # Helper functions.
    "record_request",
    "record_trade",
    "update_balance",
    "update_ml_metrics",
    "record_cache_hit",
    "record_cache_miss",
    "record_auth_failure",
    "record_rate_limit_hit",
    "get_metrics",
    # Re-export ``time`` so the middleware's existing ``import time``
    # doesn't need to add a second line for the ``time.time()`` call.
    "time",
]
