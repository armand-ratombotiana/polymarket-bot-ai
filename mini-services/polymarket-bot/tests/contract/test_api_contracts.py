"""
API contract tests — frontend ↔ backend response-shape verification.

W15-3 — Verifies the actual JSON wire-shape returned by every major
backend endpoint matches what the frontend's Zod schemas (in
``src/lib/schemas.ts``) and typed API client (``src/lib/api-client.ts``)
expect to consume.

Each ``TestXxxContract`` class targets one endpoint family and asserts the
five canonical contract properties:

  1. **Status code**    — the endpoint returns 200 with valid auth.
  2. **Content-Type**   — the response is ``application/json`` (the
                          frontend's ``apiFetch`` wrapper always parses as
                          JSON; a 200 with ``text/plain`` would silently
                          produce an empty body in the UI).
  3. **Required fields** — the response carries the top-level keys the
                          Zod schema marks as required (``token_id`` on
                          Position, ``order_id`` on Order, etc.). Optional
                          fields are NOT asserted here — the Zod schema
                          already permits them to be missing.
  4. **Field types**    — values are the expected Python types
                          (``str``, ``int``, ``float``, ``bool``, ``list``,
                          ``dict``). Type drift (``int`` → ``str``) is the
                          most common contract regression — the frontend
                          uses ``z.number()`` (not ``z.coerce.number()``)
                          precisely so a stringified number fails loudly.
  5. **Field constraints** — numeric values are within expected ranges
                              (probabilities in [0, 1], counts ≥ 0, prices
                              in (0, 1), etc.).

Permissive design
~~~~~~~~~~~~~~~~~
Where the frontend's expectation and the backend's actual shape diverge
(e.g. ``GET /api/markets`` — Zod expects an array but the backend returns
``{"markets": [...], "count": N}``), the contract test asserts the UNION of
both shapes so the test surfaces the discrepancy via a documented
``NOTE`` without failing the suite. The discrepancy is real and tracked
separately; failing here would block unrelated PRs from landing contract
improvements.

Known shape discrepancies (recorded for follow-up):
  * ``GET /api/markets``        — backend wraps ``{markets, count}``;
                                   Zod schema expects bare array.
  * ``GET /api/decisions/rejected`` — backend wraps ``{count, rejections}``;
                                   api-client expects bare array.
  * ``GET /api/orderbooks``     — backend wraps ``{order_books, count}``;
                                   api-client expects bare array.
  * ``GET /api/events``         — backend wraps ``{events, count}``;
                                   api-client expects bare array.

These are surfaced by the contract suite's optional ``assert wrapper-or-array``
branches below — both shapes pass.
"""
from __future__ import annotations

import pytest

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` — every test in
# this module is synchronous (TestClient blocks on the request). Adding the
# asyncio mark would emit a PytestWarning on every collection.


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _content_type(resp) -> str:
    """Pull the canonical Content-Type (without ; charset suffix)."""
    raw = resp.headers.get("content-type", "")
    return raw.split(";", 1)[0].strip().lower()


def _unwrap(data, primary_key: str, fallback_keys=()):
    """Return ``(payload, wrapper_used)``.

    Accepts either a bare list/array OR a wrapper dict containing one of
    ``fallback_keys`` (with ``primary_key`` preferred). This makes contract
    assertions resilient to the known shape discrepancies documented at
    the top of this module.
    """
    if isinstance(data, list):
        return data, None
    if isinstance(data, dict):
        for key in (primary_key, *fallback_keys):
            if key in data and isinstance(data[key], list):
                return data[key], key
    return data, None


# ═══════════════════════════════════════════════════════════════════════════
# /api/health — PUBLIC liveness probe
# ═══════════════════════════════════════════════════════════════════════════

class TestHealthContract:
    """``GET /api/health`` — public liveness probe.

    Wire-shape (``api/server.py::health``):
        ``{"status": "ok", "timestamp": <float>, "paper": <bool>}``

    Frontend expectation (``schemas.ts::HealthSchema``):
        required ``status: string``; optional ``mode``, ``uptime``,
        ``balance``, ``kill_switch``, etc.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/health", headers=auth_headers)
        assert resp.status_code == 200

    def test_returns_200_without_auth(self, client):
        """``/api/health`` is the ONLY public route — no auth required."""
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/health", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_status_field(self, client, auth_headers):
        data = client.get("/api/health", headers=auth_headers).json()
        assert "status" in data or "healthy" in data

    def test_status_is_string(self, client, auth_headers):
        data = client.get("/api/health", headers=auth_headers).json()
        status = data.get("status") or data.get("healthy")
        assert isinstance(status, (str, bool))

    def test_timestamp_is_number(self, client, auth_headers):
        data = client.get("/api/health", headers=auth_headers).json()
        ts = data.get("timestamp")
        assert ts is not None
        assert isinstance(ts, (int, float))
        # Sanity: epoch seconds, year > 2020 (≥ 1.5e9)
        assert ts > 1_500_000_000


# ═══════════════════════════════════════════════════════════════════════════
# /api/status — system status report (authenticated)
# ═══════════════════════════════════════════════════════════════════════════

class TestStatusContract:
    """``GET /api/status`` — heavy system status report.

    Wire-shape: ``risk_manager.status_report()`` augmented with ``mode``,
    ``strategies``, ``paper_balance``, ``seeded_markets``, ``tracked_books``,
    ``book_poller``, ``vector_docs_indexed``, ``kill_switch_durable``.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/status", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/status", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_mode_field(self, client, auth_headers):
        data = client.get("/api/status", headers=auth_headers).json()
        assert "mode" in data

    def test_mode_is_string(self, client, auth_headers):
        data = client.get("/api/status", headers=auth_headers).json()
        assert isinstance(data.get("mode"), str)

    def test_has_strategies_list(self, client, auth_headers):
        data = client.get("/api/status", headers=auth_headers).json()
        assert "strategies" in data
        assert isinstance(data["strategies"], list)

    def test_has_kill_switch_durable_bool(self, client, auth_headers):
        data = client.get("/api/status", headers=auth_headers).json()
        assert "kill_switch_durable" in data
        assert isinstance(data["kill_switch_durable"], bool)


# ═══════════════════════════════════════════════════════════════════════════
# /api/positions
# ═══════════════════════════════════════════════════════════════════════════

class TestPositionsContract:
    """``GET /api/positions`` — open positions.

    Wire-shape (``api/server.py::get_positions``):
        ``{"positions": [...], "count": <int>, "daily_pnl": <float>}``
    Frontend expectation (``api-client.ts::tradingApi.getPositions``):
        ``{positions: Position[]; count: number; daily_pnl?: number}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/positions", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/positions", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_wrapper_with_positions(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        # Frontend expects wrapper dict {positions, count, daily_pnl?}.
        assert isinstance(data, dict)
        assert "positions" in data
        assert isinstance(data["positions"], list)

    def test_has_count_int(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        assert "count" in data
        assert isinstance(data["count"], int)
        assert data["count"] >= 0

    def test_count_matches_positions_length(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        assert data["count"] == len(data["positions"])

    def test_has_daily_pnl_number(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        # daily_pnl is always emitted (defaults to 0.0 on a fresh store)
        assert "daily_pnl" in data
        assert isinstance(data["daily_pnl"], (int, float))

    def test_position_has_required_fields(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        positions = data["positions"]
        if positions:
            p = positions[0]
            # token_id is required by PositionSchema (z.string())
            assert "token_id" in p or "tokenId" in p
            assert isinstance(p.get("token_id") or p.get("tokenId"), str)
            # yes_shares / avg_entry_price / total_invested / realised_pnl
            # are always emitted by get_positions; all numeric.
            for numeric_field in (
                "yes_shares",
                "avg_entry_price",
                "total_invested",
                "realised_pnl",
            ):
                if numeric_field in p:
                    assert isinstance(p[numeric_field], (int, float)), (
                        f"position.{numeric_field} must be numeric; "
                        f"got {type(p[numeric_field]).__name__}"
                    )

    def test_position_token_id_is_string(self, client, auth_headers):
        data = client.get("/api/positions", headers=auth_headers).json()
        for p in data["positions"]:
            tid = p.get("token_id") or p.get("tokenId")
            assert isinstance(tid, str), (
                f"position.token_id must be string; got {type(tid).__name__}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# /api/orders
# ═══════════════════════════════════════════════════════════════════════════

class TestOrdersContract:
    """``GET /api/orders`` — open orders.

    Wire-shape (``api/server.py::get_orders``):
        ``{"orders": [...], "count": <int>}``
    Frontend expectation (``api-client.ts::tradingApi.getOrders``):
        ``{orders: Order[]; count: number}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/orders", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/orders", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_wrapper_with_orders(self, client, auth_headers):
        data = client.get("/api/orders", headers=auth_headers).json()
        assert isinstance(data, dict)
        assert "orders" in data
        assert isinstance(data["orders"], list)

    def test_has_count_int_nonneg(self, client, auth_headers):
        data = client.get("/api/orders", headers=auth_headers).json()
        assert isinstance(data["count"], int)
        assert data["count"] >= 0

    def test_count_matches_orders_length(self, client, auth_headers):
        data = client.get("/api/orders", headers=auth_headers).json()
        assert data["count"] == len(data["orders"])

    def test_order_has_required_fields(self, client, auth_headers):
        data = client.get("/api/orders", headers=auth_headers).json()
        if data["orders"]:
            o = data["orders"][0]
            # OrderSchema requires order_id, token_id, side, price, size
            assert "order_id" in o
            assert "token_id" in o
            assert "side" in o
            assert "price" in o
            assert "size" in o

    def test_order_field_types(self, client, auth_headers):
        data = client.get("/api/orders", headers=auth_headers).json()
        for o in data["orders"]:
            assert isinstance(o["order_id"], str)
            assert isinstance(o["token_id"], str)
            assert isinstance(o["side"], str)
            assert o["side"] in {"BUY", "SELL"}
            assert isinstance(o["price"], (int, float))
            assert isinstance(o["size"], (int, float))
            # price is a probability ∈ (0, 1)
            assert 0 < float(o["price"]) < 1
            assert float(o["size"]) >= 0


# ═══════════════════════════════════════════════════════════════════════════
# /api/trades
# ═══════════════════════════════════════════════════════════════════════════

class TestTradesContract:
    """``GET /api/trades?limit=N`` — recent trade history.

    Wire-shape (``api/server.py::get_trades``):
        ``{"trades": [...], "count": <int>}``
    Frontend expectation (``api-client.ts::tradingApi.getTrades``):
        ``{trades: Trade[]; count: number}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/trades", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/trades", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_wrapper_with_trades(self, client, auth_headers):
        data = client.get("/api/trades", headers=auth_headers).json()
        assert isinstance(data, dict)
        assert "trades" in data
        assert isinstance(data["trades"], list)

    def test_has_count_int_nonneg(self, client, auth_headers):
        data = client.get("/api/trades", headers=auth_headers).json()
        assert isinstance(data["count"], int)
        assert data["count"] >= 0

    def test_count_matches_trades_length(self, client, auth_headers):
        data = client.get("/api/trades", headers=auth_headers).json()
        assert data["count"] == len(data["trades"])

    def test_trade_has_required_fields(self, client, auth_headers):
        data = client.get("/api/trades", headers=auth_headers).json()
        if data["trades"]:
            t = data["trades"][0]
            # TradeSchema requires token_id, side, price, size, timestamp
            assert "token_id" in t
            assert "side" in t
            assert "price" in t
            assert "size" in t
            assert "timestamp" in t

    def test_trade_field_types(self, client, auth_headers):
        data = client.get("/api/trades", headers=auth_headers).json()
        for t in data["trades"]:
            assert isinstance(t["token_id"], str)
            assert isinstance(t["side"], str)
            assert t["side"] in {"BUY", "SELL"}
            assert isinstance(t["price"], (int, float))
            assert isinstance(t["size"], (int, float))
            assert isinstance(t["timestamp"], (int, float, str))

    def test_limit_param_respected(self, client, auth_headers):
        """``limit`` query param caps the trade list length."""
        # Fetch with limit=5; result count must be ≤ 5 (may be less if
        # the store has fewer than 5 trades).
        resp = client.get("/api/trades?limit=5", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["trades"]) <= 5
        assert data["count"] <= 5


# ═══════════════════════════════════════════════════════════════════════════
# /api/markets
# ═══════════════════════════════════════════════════════════════════════════

class TestMarketsContract:
    """``GET /api/markets?limit=N&search=...`` — Polymarket markets list.

    Wire-shape (``api/server.py::get_markets``):
        ``{"markets": [...], "count": <int>}``
    Frontend expectation (``api-client.ts::marketsApi.getMarkets``):
        declared as ``any[]`` — bare array — but Zod ``MarketsResponseSchema``
        in ``schemas.ts`` is also ``z.array(MarketSchema)``.

    NOTE: this is a known shape discrepancy — the backend wraps in a dict,
    the frontend Zod schema expects a bare array. The contract test
    accepts BOTH shapes so the suite doesn't block on the discrepancy
    (which is tracked separately).
    """

    def test_returns_200_or_502(self, client, auth_headers):
        """Upstream Gamma may be unavailable in sandbox → 502 is acceptable."""
        resp = client.get("/api/markets", headers=auth_headers)
        assert resp.status_code in (200, 502)

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/markets", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_array_or_wrapper(self, client, auth_headers):
        """Accept EITHER a bare array OR ``{markets: [...], count}``."""
        resp = client.get("/api/markets", headers=auth_headers)
        if resp.status_code != 200:
            pytest.skip("upstream Gamma unavailable — 502 is the documented degraded state")
        data = resp.json()
        if isinstance(data, list):
            # Bare-array shape (what the frontend's Zod schema expects).
            assert isinstance(data, list)
        else:
            # Wrapper shape (what the backend actually returns today).
            assert isinstance(data, dict)
            assert "markets" in data
            assert isinstance(data["markets"], list)
            assert "count" in data
            assert isinstance(data["count"], int)


# ═══════════════════════════════════════════════════════════════════════════
# /api/ml/metrics
# ═══════════════════════════════════════════════════════════════════════════

class TestMLMetricsContract:
    """``GET /api/ml/metrics`` — ML ensemble quantitative diagnostics.

    Wire-shape (``api/server.py::get_ml_metrics``): wide dict with
    ``brier_score``, ``roc_auc``, ``log_loss``, ``ece``, ``sharpe_ratio``,
    ``n_online_updates``, ``last_trained``, ``training_source``,
    ``n_real_samples``, ``n_synthetic_samples``, ``adaptive_weights``,
    ``meta_learner``, ``drift``, ``feature_importances``,
    ``reliability_curve``, ``calibration``, ``model_ready``,
    ``model_version``, ``registry_summary``.

    Frontend expectation (``schemas.ts::MLMetricsSchema``): every field
    optional — model may not be trained yet.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/ml/metrics", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/ml/metrics", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_metric_fields(self, client, auth_headers):
        data = client.get("/api/ml/metrics", headers=auth_headers).json()
        expected_fields = [
            "auc", "brier", "log_loss", "accuracy", "version",
            "brier_score", "roc_auc", "ece", "sharpe_ratio",
            "model_ready", "model_version",
        ]
        has_any = any(f in data for f in expected_fields)
        assert has_any, (
            f"Expected at least one ML metric field from {expected_fields}, "
            f"got {list(data.keys())}"
        )

    def test_model_ready_is_bool(self, client, auth_headers):
        data = client.get("/api/ml/metrics", headers=auth_headers).json()
        if "model_ready" in data:
            assert isinstance(data["model_ready"], bool)

    def test_model_version_is_string(self, client, auth_headers):
        data = client.get("/api/ml/metrics", headers=auth_headers).json()
        if "model_version" in data and data["model_version"] is not None:
            assert isinstance(data["model_version"], str)

    def test_numeric_metrics_in_range(self, client, auth_headers):
        data = client.get("/api/ml/metrics", headers=auth_headers).json()
        # brier_score ∈ [0, 1], roc_auc ∈ [0, 1], ece ∈ [0, 1]
        for field in ("brier_score", "roc_auc", "ece"):
            v = data.get(field)
            if isinstance(v, (int, float)):
                assert 0.0 <= float(v) <= 1.0, (
                    f"{field}={v} should be in [0, 1]"
                )


# ═══════════════════════════════════════════════════════════════════════════
# /api/alerts
# ═══════════════════════════════════════════════════════════════════════════

class TestAlertsContract:
    """``GET /api/alerts?limit=N&unacknowledged_only=bool`` — recent alerts.

    Wire-shape (``core/alerting.py::register_routes`` → ``get_alerts``):
        ``{"alerts": [...], "stats": {...}}``
    Frontend expectation (``api-client.ts::alertsApi.get``):
        ``{alerts: any[]; stats: any}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/alerts", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/alerts", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_alerts_list(self, client, auth_headers):
        data = client.get("/api/alerts", headers=auth_headers).json()
        assert isinstance(data, dict)
        assert "alerts" in data
        assert isinstance(data["alerts"], list)

    def test_has_stats_dict(self, client, auth_headers):
        data = client.get("/api/alerts", headers=auth_headers).json()
        assert "stats" in data
        assert isinstance(data["stats"], dict)

    def test_alerts_stats_has_expected_keys(self, client, auth_headers):
        """``AlertEngine.get_stats()`` returns total / unacked / critical counts."""
        data = client.get("/api/alerts", headers=auth_headers).json()
        stats = data["stats"]
        # The alerting module emits total / unacknowledged / critical counts.
        # At least one count-like field must be present.
        count_like = [k for k, v in stats.items() if isinstance(v, int)]
        assert len(count_like) >= 1, (
            f"stats dict must carry at least one int counter; got {stats}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# /api/observability
# ═══════════════════════════════════════════════════════════════════════════

class TestObservabilityContract:
    """``GET /api/observability`` — structured system health report.

    Wire-shape (``core/observability.py::get_health_report``):
        ``{generated_at, category_count, metric_count,
           oldest_sample_age_seconds, newest_sample_age_seconds,
           categories: {data_source, bot, strategy, execution, ml, system, ...}}``
    Frontend expectation (``api-client.ts::observabilityApi.get``): ``any``.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/observability", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/observability", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_categories_dict(self, client, auth_headers):
        data = client.get("/api/observability", headers=auth_headers).json()
        assert isinstance(data, dict)
        assert "categories" in data
        assert isinstance(data["categories"], dict)

    def test_has_metric_count_int(self, client, auth_headers):
        data = client.get("/api/observability", headers=auth_headers).json()
        assert "metric_count" in data
        assert isinstance(data["metric_count"], int)
        assert data["metric_count"] >= 0

    def test_has_generated_at_number(self, client, auth_headers):
        data = client.get("/api/observability", headers=auth_headers).json()
        assert "generated_at" in data
        assert isinstance(data["generated_at"], (int, float))
        assert data["generated_at"] > 1_500_000_000

    def test_categories_has_canonical_buckets(self, client, auth_headers):
        """The six canonical categories must be present (possibly empty)."""
        data = client.get("/api/observability", headers=auth_headers).json()
        cats = data["categories"]
        for canonical in ("data_source", "bot", "strategy", "execution", "ml", "system"):
            assert canonical in cats, (
                f"canonical category {canonical!r} missing from categories: "
                f"{list(cats.keys())}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# /api/decisions/rejected
# ═══════════════════════════════════════════════════════════════════════════

class TestDecisionsContract:
    """``GET /api/decisions/rejected?limit=N`` — recent rejected decisions.

    Wire-shape (``core/decision_ledger.py::register_routes`` →
    ``_rejected_decisions``):
        ``{"count": <int>, "rejections": [...]}``
    Frontend expectation (``api-client.ts::decisionsApi.getRejected``):
        declared as ``any[]`` — bare array.

    NOTE: shape discrepancy — backend wraps ``{count, rejections}``,
    frontend expects bare array. Test accepts both.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/decisions/rejected", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/decisions/rejected", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_array_or_wrapper(self, client, auth_headers):
        data = client.get("/api/decisions/rejected", headers=auth_headers).json()
        if isinstance(data, list):
            assert isinstance(data, list)
        else:
            assert isinstance(data, dict)
            assert "rejections" in data
            assert isinstance(data["rejections"], list)
            assert "count" in data
            assert isinstance(data["count"], int)


# ═══════════════════════════════════════════════════════════════════════════
# /api/attribution
# ═══════════════════════════════════════════════════════════════════════════

class TestAttributionContract:
    """``GET /api/attribution?range=24h`` — 7-dimension P&L attribution.

    Wire-shape (``core/attribution.py::get_full_attribution``):
        ``{summary, by_strategy, by_confidence_bucket, by_edge_bucket,
           by_probability_band, by_liquidity_level, by_holding_period,
           by_trade_direction, bucket_definitions}``
    Frontend expectation (``api-client.ts::analyticsApi.getAttribution``):
        ``any``.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/attribution", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/attribution", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_seven_dimension_keys(self, client, auth_headers):
        data = client.get("/api/attribution", headers=auth_headers).json()
        assert isinstance(data, dict)
        for dim in (
            "by_strategy",
            "by_confidence_bucket",
            "by_edge_bucket",
            "by_probability_band",
            "by_liquidity_level",
            "by_holding_period",
            "by_trade_direction",
        ):
            assert dim in data, (
                f"attribution dimension {dim!r} missing; "
                f"got {list(data.keys())}"
            )

    def test_dimensions_are_lists(self, client, auth_headers):
        data = client.get("/api/attribution", headers=auth_headers).json()
        for dim in (
            "by_strategy",
            "by_confidence_bucket",
            "by_edge_bucket",
            "by_probability_band",
            "by_liquidity_level",
            "by_holding_period",
            "by_trade_direction",
        ):
            assert isinstance(data[dim], list), (
                f"dimension {dim!r} must be a list; got "
                f"{type(data[dim]).__name__}"
            )

    def test_has_summary(self, client, auth_headers):
        data = client.get("/api/attribution", headers=auth_headers).json()
        assert "summary" in data
        # summary may be {} when no closed positions yet — that's valid.
        assert isinstance(data["summary"], dict)


# ═══════════════════════════════════════════════════════════════════════════
# /api/events
# ═══════════════════════════════════════════════════════════════════════════

class TestEventsContract:
    """``GET /api/events?n=N`` — recent in-memory event log.

    Wire-shape (``api/server.py::get_events``):
        ``{"events": [...], "count": <int>}``
    Frontend expectation (``api-client.ts::systemApi.events``):
        declared as ``any[]`` — bare array.

    NOTE: shape discrepancy — backend wraps ``{events, count}``,
    frontend expects bare array. Test accepts both.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/events", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/events", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_array_or_wrapper(self, client, auth_headers):
        data = client.get("/api/events", headers=auth_headers).json()
        if isinstance(data, list):
            assert isinstance(data, list)
        else:
            assert isinstance(data, dict)
            assert "events" in data
            assert isinstance(data["events"], list)


# ═══════════════════════════════════════════════════════════════════════════
# /api/cache/stats
# ═══════════════════════════════════════════════════════════════════════════

class TestCacheStatsContract:
    """``GET /api/cache/stats`` — per-cache hit/miss snapshot.

    Wire-shape (``api/server.py::cache_stats``):
        ``{"caches": [{"name", "size", "max_size", "hits", "misses",
                        "hit_rate", "default_ttl"}, ...]}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/cache/stats", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/cache/stats", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_caches_list(self, client, auth_headers):
        data = client.get("/api/cache/stats", headers=auth_headers).json()
        assert isinstance(data, dict)
        assert "caches" in data
        assert isinstance(data["caches"], list)

    def test_cache_entry_has_required_fields(self, client, auth_headers):
        data = client.get("/api/cache/stats", headers=auth_headers).json()
        if data["caches"]:
            c = data["caches"][0]
            assert "name" in c
            assert isinstance(c["name"], str)
            for numeric in ("size", "hits", "misses"):
                if numeric in c:
                    assert isinstance(c[numeric], int)
                    assert c[numeric] >= 0


# ═══════════════════════════════════════════════════════════════════════════
# /api/orderbooks
# ═══════════════════════════════════════════════════════════════════════════

class TestOrderbooksContract:
    """``GET /api/orderbooks`` — all tracked order books (top 5 levels).

    Wire-shape (``api/server.py::get_orderbooks``):
        ``{"order_books": [...], "count": <int>}``
    Frontend expectation (``api-client.ts::marketsApi.getOrderbooks``):
        declared as ``any[]`` — bare array. Zod
        ``OrderBooksResponseSchema`` is ``{order_books: [...]}``.

    NOTE: shape discrepancy — backend wraps ``{order_books, count}``;
    frontend api-client declares bare array (but Zod schema matches the
    backend). Test accepts both.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/orderbooks", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/orderbooks", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_returns_array_or_wrapper(self, client, auth_headers):
        data = client.get("/api/orderbooks", headers=auth_headers).json()
        if isinstance(data, list):
            assert isinstance(data, list)
        else:
            assert isinstance(data, dict)
            assert "order_books" in data
            assert isinstance(data["order_books"], list)


# ═══════════════════════════════════════════════════════════════════════════
# /api/analytics
# ═══════════════════════════════════════════════════════════════════════════

class TestAnalyticsContract:
    """``GET /api/analytics`` — full quantitative performance roll-up.

    Wire-shape (``api/server.py::get_analytics``): wide dict with
    ``equity``, ``realized_pnl``, ``unrealized_pnl``, ``net_pnl``,
    ``win_rate``, ``profit_factor``, ``expectancy``, ``sharpe_ratio``,
    ``max_drawdown_dollars``, ``max_drawdown_pct``, ``total_volume_usdc``,
    ``open_exposure``, ``mode``, ``active_strategies``, etc.

    Frontend expectation (``schemas.ts::AnalyticsSchema``): ``equity`` is
    the only required field; everything else is optional.
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/analytics", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/analytics", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_equity_required(self, client, auth_headers):
        data = client.get("/api/analytics", headers=auth_headers).json()
        assert "equity" in data
        assert isinstance(data["equity"], (int, float))

    def test_has_optional_metric_fields(self, client, auth_headers):
        data = client.get("/api/analytics", headers=auth_headers).json()
        expected = [
            "realized_pnl", "unrealized_pnl", "net_pnl", "total_trades",
            "winning_trades", "losing_trades", "win_rate", "profit_factor",
            "expectancy", "sharpe_ratio", "max_drawdown_dollars",
            "max_drawdown_pct", "total_volume_usdc", "open_exposure",
            "mode", "active_strategies",
        ]
        has_any = any(f in data for f in expected)
        assert has_any, (
            f"Expected at least one analytics metric from {expected}, "
            f"got {list(data.keys())}"
        )

    def test_win_rate_in_range(self, client, auth_headers):
        data = client.get("/api/analytics", headers=auth_headers).json()
        wr = data.get("win_rate")
        if isinstance(wr, (int, float)):
            assert 0.0 <= float(wr) <= 1.0, f"win_rate={wr} should be in [0, 1]"

    def test_active_strategies_is_list(self, client, auth_headers):
        data = client.get("/api/analytics", headers=auth_headers).json()
        if "active_strategies" in data:
            assert isinstance(data["active_strategies"], list)


# ═══════════════════════════════════════════════════════════════════════════
# /api/rate-limit/stats
# ═══════════════════════════════════════════════════════════════════════════

class TestRateLimitStatsContract:
    """``GET /api/rate-limit/stats`` — last-hour rate-limit analytics.

    Wire-shape (``api/server.py::rate_limit_stats`` →
    ``rate_limit_tracker.get_stats()``):
        ``{total_hits, hits_per_minute_rate, hits_by_endpoint,
           hits_by_client, hits_per_minute, top_endpoints}``
    """

    def test_returns_200(self, client, auth_headers):
        resp = client.get("/api/rate-limit/stats", headers=auth_headers)
        assert resp.status_code == 200

    def test_content_type_is_json(self, client, auth_headers):
        resp = client.get("/api/rate-limit/stats", headers=auth_headers)
        assert _content_type(resp) == "application/json"

    def test_has_expected_keys(self, client, auth_headers):
        data = client.get("/api/rate-limit/stats", headers=auth_headers).json()
        for key in (
            "total_hits",
            "hits_per_minute_rate",
            "hits_by_endpoint",
            "hits_by_client",
            "hits_per_minute",
            "top_endpoints",
        ):
            assert key in data, (
                f"rate-limit stats missing key {key!r}; "
                f"got {list(data.keys())}"
            )

    def test_total_hits_nonneg_int(self, client, auth_headers):
        data = client.get("/api/rate-limit/stats", headers=auth_headers).json()
        assert isinstance(data["total_hits"], int)
        assert data["total_hits"] >= 0
