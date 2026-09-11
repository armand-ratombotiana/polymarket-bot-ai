"""tests/test_prometheus.py — W13-1 Prometheus metrics endpoint contract tests.

Covers the four behavioural guarantees the W13-1 task spec asks for:

  1. **``/metrics`` returns 200 without an Authorization header** — the
     endpoint is intentionally unauthenticated so Prometheus scrapers
     (which don't carry the application API token) can pull metrics
     without a 401 / 503 cascade.
  2. **The metrics payload contains the expected metric names** — at a
     minimum the W13-1 contract surfaces:
        * ``polymarket_http_requests_total``       (Counter)
        * ``polymarket_http_request_duration_seconds`` (Histogram)
        * ``polymarket_http_requests_in_progress`` (Gauge)
        * ``polymarket_paper_balance_usd``         (Gauge)
        * ``polymarket_realized_pnl_usd``          (Gauge)
        * ``polymarket_unrealized_pnl_usd``        (Gauge)
        * ``polymarket_open_positions``            (Gauge)
        * ``polymarket_ml_drift_psi``              (Gauge)
        * ``polymarket_ml_brier_score``            (Gauge)
        * ``polymarket_cache_hits_total``          (Counter)
        * ``polymarket_auth_failures_total``       (Counter)
        * ``polymarket_rate_limit_hits_total``     (Counter)
  3. **``record_request()`` increments the counter** — calling the
     helper with a (method, endpoint, status, duration) tuple produces
     a metric line whose value goes from 0 → 1.
  4. **``update_balance()`` sets the gauge** — calling the helper with
     ``(balance=1000.0, realized=50.0, unrealized=25.0)`` produces
     gauge lines whose values match the args.

Hermeticity
~~~~~~~~~~~
Imports the production ``api.server.app`` so the route + middleware
under test is the REAL one (mirrors the ``tests/test_openapi.py``
contract). The autouse ``_reset_store_factory_defaults`` conftest
fixture wipes store singletons before every test; rate limiting is
disabled in ``conftest.py`` so per-route slowapi limits don't 429
mid-suite.

All tests are SYNC ``def test_...`` — ``TestClient`` bridges each
request through its own anyio portal (mirrors ``test_openapi.py`` /
``test_integration.py``).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from api.server import app
from core.prometheus_metrics import (
    get_metrics,
    record_request,
    update_balance,
)

# Defensive: disable the rate-limit middleware so a fast test sequence
# against a per-minute-limited route doesn't 429 mid-suite.
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# the bearer token below matches what the ``enforce_api_auth`` middleware
# accepts.
VALID_TOKEN = "test-token-conftest"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests; without it Starlette
    re-raises in the test process. Mirrors the pattern in
    ``tests/test_openapi.py``.

    The production ``app`` carries a ``lifespan`` that initializes
    TimescaleDB / paper_sim / market seeding — ``TestClient(app)`` (NOT
    ``with TestClient(app)``) skips the lifespan so each test stays fast.
    """
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ── Test 1: /metrics returns 200 without auth ──────────────────────────────


def test_metrics_endpoint_returns_200_without_auth(client: TestClient):
    """W13-1 contract: ``GET /metrics`` is unauthenticated.

    Prometheus scrapers don't carry the application API token — they
    rely on network-level isolation + optional ingress auth (mTLS /
    OAuth2 proxy) at the deployment boundary. If the endpoint required
    the bearer token, every scrape would 401 and Grafana panels would
    go dark the moment the API token was rotated.

    Verifies:
      * status code is 200 (NOT 401 / 503).
      * the response ``Content-Type`` is the canonical Prometheus
        text exposition format (``text/plain; version=0.0.4;
        charset=utf-8`` — exposed as ``CONTENT_TYPE_LATEST``).
      * the response body is non-empty and decodes as UTF-8.
    """
    response = client.get("/metrics")
    assert response.status_code == 200, (
        f"/metrics returned {response.status_code} — expected 200 (the endpoint "
        f"is intentionally unauthenticated so Prometheus scrapers can pull "
        f"metrics without the application API token). Body: {response.text[:200]}"
    )
    # The ``Content-Type`` header is set by the route handler via
    # ``Response(content=..., media_type=CONTENT_TYPE_LATEST)``.
    content_type = response.headers.get("content-type", "")
    assert "text/plain" in content_type, (
        f"/metrics Content-Type must be text/plain (Prometheus exposition format) — "
        f"got {content_type!r}"
    )
    # The ``version=`` parameter identifies the Prometheus exposition
    # version (e.g. ``version=0.0.4`` on prometheus_client 0.20.x or
    # ``version=1.0.0`` on prometheus_client 0.24.x). Either is valid —
    # what matters is that the parameter is present (so a Prometheus
    # scraper can select the correct parser).
    assert "version=" in content_type, (
        f"/metrics Content-Type must include a 'version=' parameter (Prometheus "
        f"exposition version) — got {content_type!r}"
    )
    assert "charset=utf-8" in content_type, (
        f"/metrics Content-Type must include 'charset=utf-8' so a Prometheus "
        f"scraper decodes non-ASCII metric labels correctly — got {content_type!r}"
    )
    # Body must be non-empty + decode cleanly as UTF-8 (TestClient
    # already decodes for us when we access ``.text``).
    assert len(response.text) > 0, (
        "/metrics returned an empty body — the prometheus_client registry "
        "should always emit at least the python_gc_* default metrics."
    )


# ── Test 2: /metrics payload contains expected metric names ──────────────────


def test_metrics_payload_contains_expected_metric_names(client: TestClient):
    """W13-1 contract: the metrics payload surfaces every canonical metric.

    The Grafana dashboard in ``grafana/dashboard.json`` queries these
    metric names via PromQL — if any are renamed or removed, the
    corresponding panels will go to "no data" silently. This test
    pins the metric names so a refactor that renames (say)
    ``polymarket_paper_balance_usd`` to ``polymarket_balance_usd``
    fails loudly here rather than silently breaking the dashboard.
    """
    response = client.get("/metrics")
    assert response.status_code == 200
    payload = response.text

    expected_metrics = [
        # HTTP.
        "polymarket_http_requests_total",
        "polymarket_http_request_duration_seconds",
        "polymarket_http_requests_in_progress",
        # Trading.
        "polymarket_orders_placed_total",
        "polymarket_orders_filled_total",
        "polymarket_trades_total",
        "polymarket_realized_pnl_usd",
        "polymarket_unrealized_pnl_usd",
        "polymarket_paper_balance_usd",
        "polymarket_open_positions",
        "polymarket_open_orders",
        # ML.
        "polymarket_ml_predictions_total",
        "polymarket_ml_drift_psi",
        "polymarket_ml_brier_score",
        "polymarket_ml_roc_auc",
        # System.
        "polymarket_cache_hits_total",
        "polymarket_cache_misses_total",
        "polymarket_db_size_bytes",
        "polymarket_alerts_active",
        # Auth / rate-limit.
        "polymarket_auth_failures_total",
        "polymarket_rate_limit_hits_total",
    ]

    missing = [m for m in expected_metrics if m not in payload]
    assert not missing, (
        f"/metrics payload is missing expected metric names: {missing}. "
        f"The Grafana dashboard in grafana/dashboard.json queries these "
        f"names via PromQL — a rename or removal will silently break "
        f"the corresponding panels."
    )


# ── Test 3: record_request() increments the counter ─────────────────────────


def test_record_request_increments_counter():
    """W13-1 contract: ``record_request()`` increments
    ``polymarket_http_requests_total`` by exactly 1 per call.

    The middleware calls this helper on every response (200, 4xx, 5xx);
    a Grafana panel computing ``rate(polymarket_http_requests_total[1m])``
    needs the counter to increment by exactly 1 per request, not 0
    (silent scrape hole) or >1 (double-counting across middleware
    layers).

    Snapshots the counter before / after a single ``record_request()``
    call and asserts the delta is exactly 1.
    """
    # Snapshot the counter BEFORE — sample the registry output and
    # parse the metric line for our (method, endpoint, status) tuple.
    method = "GET"
    endpoint = "/api/health"
    status = 200
    duration = 0.0123

    # Capture the BEFORE value by parsing the metrics output.
    before_payload = get_metrics().decode("utf-8")
    before_value = _parse_counter_value(
        before_payload,
        "polymarket_http_requests_total",
        {"method": method, "endpoint": endpoint, "status": str(status)},
    )

    # Record a single request.
    record_request(
        method=method,
        endpoint=endpoint,
        status=status,
        duration=duration,
    )

    # Capture the AFTER value.
    after_payload = get_metrics().decode("utf-8")
    after_value = _parse_counter_value(
        after_payload,
        "polymarket_http_requests_total",
        {"method": method, "endpoint": endpoint, "status": str(status)},
    )

    assert after_value is not None, (
        f"polymarket_http_requests_total{{method={method},endpoint={endpoint},"
        f"status={status}}} not found in metrics payload after record_request() — "
        f"the counter wasn't incremented with the expected label set."
    )
    expected_delta = 1
    actual_delta = after_value - (before_value or 0.0)
    assert actual_delta == expected_delta, (
        f"record_request() should increment polymarket_http_requests_total by "
        f"exactly {expected_delta}, but the delta was {actual_delta} "
        f"(before={before_value}, after={after_value}). Double-counting would "
        f"inflate dashboard rates; a delta of 0 would silently break the "
        f"req/s panel."
    )


# ── Test 4: update_balance() sets the gauge ──────────────────────────────────


def test_update_balance_sets_gauge():
    """W13-1 contract: ``update_balance()`` sets the paper-balance /
    realized-PnL / unrealized-PnL gauges to the supplied values.

    The Grafana "Paper Balance ($)" and "Realized + Unrealized P&L ($)"
    panels query these gauges directly — a regression that left them
    at 0 (or at a stale last-set value) would make the panels show
    misleading numbers without any error indication.

    Sets ``(balance=1234.56, realized=78.90, unrealized=12.34)`` and
    asserts each gauge line in the metrics output matches.
    """
    balance = 1234.56
    realized = 78.90
    unrealized = 12.34

    update_balance(
        balance=balance,
        realized=realized,
        unrealized=unrealized,
    )

    payload = get_metrics().decode("utf-8")

    # All three gauges are unlabelled — the metric line shape is just
    # ``<name> <value>`` (no ``{...}`` label block).
    paper_balance_value = _parse_unlabelled_gauge_value(
        payload, "polymarket_paper_balance_usd"
    )
    realized_pnl_value = _parse_unlabelled_gauge_value(
        payload, "polymarket_realized_pnl_usd"
    )
    unrealized_pnl_value = _parse_unlabelled_gauge_value(
        payload, "polymarket_unrealized_pnl_usd"
    )

    assert paper_balance_value is not None, (
        "polymarket_paper_balance_usd gauge not found in metrics payload after "
        "update_balance() — the gauge wasn't set."
    )
    assert _approx_eq(paper_balance_value, balance), (
        f"polymarket_paper_balance_usd = {paper_balance_value}, expected {balance} "
        f"(delta should be 0; prometheus_client stores floats as-is, so any drift "
        f"indicates a unit conversion bug)."
    )
    assert realized_pnl_value is not None, (
        "polymarket_realized_pnl_usd gauge not found in metrics payload after "
        "update_balance(realized=...) — the gauge wasn't set."
    )
    assert _approx_eq(realized_pnl_value, realized), (
        f"polymarket_realized_pnl_usd = {realized_pnl_value}, expected {realized}."
    )
    assert unrealized_pnl_value is not None, (
        "polymarket_unrealized_pnl_usd gauge not found in metrics payload after "
        "update_balance(unrealized=...) — the gauge wasn't set."
    )
    assert _approx_eq(unrealized_pnl_value, unrealized), (
        f"polymarket_unrealized_pnl_usd = {unrealized_pnl_value}, expected {unrealized}."
    )


# ── Test 5: /metrics is in PUBLIC_PATHS (no auth required) ────────────────────


def test_metrics_is_in_public_paths():
    """W13-1 contract: ``/metrics`` is registered in the server's
    ``PUBLIC_PATHS`` set so the ``enforce_api_auth`` middleware
    short-circuits without consulting ``settings.api_token``.

    Without this, a misconfigured (or rotated) API token would 503 the
    Prometheus scraper and Grafana panels would silently go dark —
    the dashboard would still render but every panel would show "no
    data" with no indication of WHY.

    Verifies the path is in ``PUBLIC_PATHS`` (a direct set-membership
    check) — this is the load-bearing assertion; the HTTP-level test
    above is the contract test (proves the end-to-end behaviour).
    """
    from api.server import PUBLIC_PATHS

    assert "/metrics" in PUBLIC_PATHS, (
        f"/metrics not in PUBLIC_PATHS ({PUBLIC_PATHS}) — the enforce_api_auth "
        f"middleware would short-circuit on a missing/invalid API token and "
        f"return 401 / 503 to the Prometheus scraper."
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_counter_value(payload: str, metric_name: str, labels: dict[str, str]) -> float | None:
    """Parse a labelled counter value from a Prometheus exposition payload.

    Looks for a line of the form::

        <metric_name>{<label>="<value>", ...} <counter_value>

    Returns ``None`` if no matching line is found.
    """
    for line in payload.splitlines():
        if not line.startswith(metric_name):
            continue
        if "{" not in line or "}" not in line:
            continue
        # Skip HELP / TYPE header lines.
        if line.startswith("#"):
            continue
        # Parse the label block.
        label_block = line[line.index("{") + 1: line.rindex("}")]
        # Parse ``key="value", ...`` pairs.
        parsed_labels: dict[str, str] = {}
        for pair in label_block.split(","):
            if "=" not in pair:
                continue
            k, _, v = pair.partition("=")
            v = v.strip().strip('"')
            parsed_labels[k.strip()] = v
        # Check if all required labels match.
        if all(parsed_labels.get(k) == v for k, v in labels.items()):
            # The value is the trailing whitespace-separated token.
            value_str = line[line.rindex("}") + 1:].strip()
            if not value_str:
                continue
            try:
                return float(value_str)
            except ValueError:
                continue
    return None


def _parse_unlabelled_gauge_value(payload: str, metric_name: str) -> float | None:
    """Parse an unlabelled gauge value (``<name> <value>``) from a
    Prometheus exposition payload.

    Returns ``None`` if no matching line is found.
    """
    for line in payload.splitlines():
        if line.startswith("#"):
            continue
        # Match ``<metric_name> <value>`` exactly (no ``{``).
        if line.startswith(metric_name + " "):
            value_str = line[len(metric_name) + 1:].strip()
            try:
                return float(value_str)
            except ValueError:
                continue
        # Also match ``<metric_name>`` at end-of-line (value=0 implicit?
        # prometheus_client always emits the explicit value, so this is
        # defensive only).
        if line == metric_name:
            return 0.0
    return None


def _approx_eq(a: float, b: float, *, rel_tol: float = 1e-6, abs_tol: float = 1e-6) -> bool:
    """Tolerant float equality (mirrors ``math.isclose`` defaults)."""
    return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)
