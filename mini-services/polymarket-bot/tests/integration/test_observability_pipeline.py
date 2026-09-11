"""W17-9 — Cross-module integration tests for the observability pipeline.

Drives the three pillars of the observability stack end-to-end:

  1. **Metrics → storage → retrieval**: record a metric sample via
     ``observability.record_metric(category, name, value, **metadata)``,
     verify it lands in the SQLite store, and is returned by both
     ``get_metric_history(name, limit=N)`` (newest-first time series)
     and ``get_health_report()`` (latest-value-per-(category, name)
     structured report).

  2. **Alert → notification pipeline**: feed a metrics dict (e.g.
     ``{api_latency_ms: 2000}``) to ``AlertEngine.evaluate`` — verify
     the alert is persisted to SQLite, surfaced by ``get_recent``, and
     its ``acknowledged`` flag flips on ``acknowledge(alert_id)`` /
     ``acknowledge_all()``.

  3. **Profiling → performance report**: drive the ``Profiler`` singleton
     with a few sample requests, verify ``get_stats`` returns per-
     endpoint p50/p95/p99 + error counts, AND verify the
     ``GET /api/profiling/stats`` HTTP endpoint surfaces the same data
     through the production FastAPI middleware chain.

Hermeticity
-----------
``conftest.py`` redirects ``OBSERVABILITY_DB_PATH`` + ``ALERT_DB_PATH``
to a writable ``/tmp/pmbot_conftest_isolation/`` sandbox BEFORE any
project module is imported. Each test that exercises a singleton
constructs a fresh instance against a ``tmp_path``-scoped SQLite file
(mirrors ``tests/test_observability.py`` / ``tests/test_alerting.py``)
so the module-level singletons (``observability`` / ``alert_engine``)
are never perturbed by these tests.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.alerting import (
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    AlertEngine,
    register_routes as register_alert_routes,
)
from core.observability import (
    CAT_BOT,
    CAT_ML,
    CAT_SYSTEM,
    Observability,
)
from core.profiling import Profiler, profiler

# pytest-asyncio strict mode — explicit module-level mark for async tests.
pytestmark = pytest.mark.asyncio


# ── (1) Metrics → storage → retrieval ───────────────────────────────────────


async def test_metric_recorded_then_retrieved_via_history():
    """``record_metric`` persists a sample; ``get_metric_history(name)``
    returns it newest-first.

    Verifies the round-trip: write → SQLite → read.
    """
    obs = Observability()  # uses conftest-redirected DB_PATH
    metric_name = "w17_9_test_record_then_history"

    await obs.record_metric(
        CAT_SYSTEM, metric_name, 42.5, source="integration_test"
    )

    history = await obs.get_metric_history(metric_name, limit=10)
    assert len(history) >= 1
    latest = history[0]
    assert latest["name"] == metric_name
    assert latest["category"] == CAT_SYSTEM
    assert latest["value"] == pytest.approx(42.5, abs=1e-3)
    # Metadata is JSON-decoded back into a dict.
    assert latest["metadata"] is not None
    assert latest["metadata"].get("source") == "integration_test"


async def test_metric_history_returns_newest_first():
    """``get_metric_history`` returns samples newest-first.

    Records three samples with strictly-increasing timestamps; the
    returned list must be in reverse-chronological order.
    """
    obs = Observability()
    metric_name = "w17_9_test_history_order"

    await obs.record_metric(CAT_BOT, metric_name, 1.0)
    await obs.record_metric(CAT_BOT, metric_name, 2.0)
    await obs.record_metric(CAT_BOT, metric_name, 3.0)

    history = await obs.get_metric_history(metric_name, limit=10)
    # The three samples we just wrote are at the top (newest-first).
    assert len(history) >= 3
    values = [h["value"] for h in history[:3]]
    assert values == [3.0, 2.0, 1.0], (
        f"history should be newest-first (3.0, 2.0, 1.0); got {values}"
    )
    # Timestamps are descending.
    timestamps = [h["timestamp"] for h in history[:3]]
    assert timestamps == sorted(timestamps, reverse=True)


async def test_health_report_returns_latest_value_per_metric():
    """``get_health_report`` returns the latest value per (category, name)
    pair, bucketed under the six canonical categories.

    After recording two samples for the same metric, the report should
    surface only the most-recent value (not both).
    """
    obs = Observability()
    metric_name = "w17_9_test_health_report"

    await obs.record_metric(CAT_SYSTEM, metric_name, 10.0)
    await obs.record_metric(CAT_SYSTEM, metric_name, 20.0)

    report = await obs.get_health_report()
    # Report shape.
    assert "categories" in report
    assert report["category_count"] >= 6  # canonical categories
    assert "system" in report["categories"]
    system_metrics = report["categories"]["system"]
    assert metric_name in system_metrics
    # Latest value surfaced (20.0, not 10.0).
    entry = system_metrics[metric_name]
    assert entry["value"] == pytest.approx(20.0, abs=1e-3)
    # age_seconds is non-negative (sample is from the past).
    assert entry["age_seconds"] >= 0


# ── (2) Alert → notification pipeline ──────────────────────────────────────


async def test_alert_evaluation_persists_alert(tmp_path):
    """Feeding a metrics dict that crosses a threshold fires an alert
    AND persists it to SQLite (``get_recent`` returns it).

    Uses a fresh ``AlertEngine`` against ``tmp_path`` so the
    module-level singleton is untouched.
    """
    engine = AlertEngine(db_path=tmp_path / "alerts.db")

    # High-latency alert (default rule: api_latency_ms > 1000).
    fired = engine.evaluate({"api_latency_ms": 2000})
    assert len(fired) >= 1
    alert = next(a for a in fired if a.name == "high_latency")
    assert alert.severity == SEVERITY_WARNING
    assert alert.category == "system"
    assert alert.value == 2000.0
    assert alert.threshold == 1000

    # Alert persisted to SQLite — get_recent surfaces it.
    rows = engine.get_recent(limit=10)
    assert len(rows) >= 1
    assert any(r["name"] == "high_latency" for r in rows)
    assert all(r["acknowledged"] == 0 for r in rows)


async def test_alert_acknowledge_flips_flag(tmp_path):
    """``acknowledge(alert_id)`` flips the acknowledged flag on one row.

    Mirrors the production flow: a dashboard operator clicks "ack" on
    a single alert; the row's flag flips and ``get_stats`` reflects the
    decreased unacked count.
    """
    engine = AlertEngine(db_path=tmp_path / "alerts.db")
    fired = engine.evaluate({"psi": 0.5})  # model_drift_detected
    alert_id = fired[0].alert_id

    # Pre: unacked.
    rows = engine.get_recent()
    assert rows[0]["acknowledged"] == 0
    assert engine.get_stats()["unacknowledged"] == 1

    # Acknowledge.
    assert engine.acknowledge(alert_id) is True

    # Post: acknowledged + unacked drops to 0.
    rows = engine.get_recent()
    assert rows[0]["acknowledged"] == 1
    stats = engine.get_stats()
    assert stats["unacknowledged"] == 0
    assert stats["total_alerts"] == 1


async def test_alert_acknowledge_all_clears_unacked(tmp_path):
    """``acknowledge_all()`` flips every unacked alert and returns the
    count.

    Fires 3 alerts (2 warning + 1 critical), then acknowledges all →
    ``unacknowledged`` drops to 0 and ``critical_unacknowledged`` also
    drops to 0.
    """
    engine = AlertEngine(db_path=tmp_path / "alerts.db")
    engine.evaluate({"psi": 0.5})  # warning (model_drift_detected)
    engine.evaluate({"api_latency_ms": 2000})  # warning (high_latency)
    engine.evaluate({"daily_pnl": -5.0})  # critical (max_drawdown_exceeded)

    pre = engine.get_stats()
    assert pre["total_alerts"] == 3
    assert pre["unacknowledged"] == 3
    assert pre["critical_unacknowledged"] == 1

    count = engine.acknowledge_all()
    assert count == 3

    post = engine.get_stats()
    assert post["unacknowledged"] == 0
    assert post["critical_unacknowledged"] == 0
    assert post["total_alerts"] == 3  # total unchanged


async def test_alert_acknowledge_unknown_id_returns_false(tmp_path):
    """``acknowledge(unknown_id)`` returns False without raising."""
    engine = AlertEngine(db_path=tmp_path / "alerts.db")
    assert engine.acknowledge("nonexistent_alert_id_xyz") is False


async def test_alert_pipeline_via_http_api(tmp_path, monkeypatch):
    """The HTTP surface mirrors the engine: an alert fired via the
    engine is visible via ``GET /api/alerts`` and ack-able via
    ``POST /api/alerts/{id}/acknowledge``.

    Mirrors the pattern in ``tests/test_alerting.py``: monkeypatch the
    module-level ``alert_engine`` singleton to a fresh ``tmp_path``-scoped
    instance so the route handlers hit the isolated DB.
    """
    fresh = AlertEngine(db_path=tmp_path / "api_alerts.db")
    import core.alerting as _alerting_mod

    monkeypatch.setattr(_alerting_mod, "alert_engine", fresh)

    app = FastAPI()
    register_alert_routes(app)
    client = TestClient(app)

    # Initially empty.
    response = client.get("/api/alerts")
    assert response.status_code == 200
    assert response.json()["alerts"] == []
    assert response.json()["stats"]["total_alerts"] == 0

    # Fire an alert directly via the engine (the production
    # ``POST /api/alerts/evaluate`` endpoint pulls metrics from
    # ``core.observability`` rather than accepting a JSON body, so the
    # cleanest path is to fire the alert via the engine + verify the
    # API surface reads it back).
    fired = fresh.evaluate({"api_latency_ms": 2000})  # high_latency
    assert len(fired) >= 1
    alert_id = fired[0].alert_id

    # GET /api/alerts now returns the fired alert.
    response = client.get("/api/alerts")
    assert response.status_code == 200
    alerts = response.json()["alerts"]
    assert len(alerts) >= 1
    surf_alert = next(a for a in alerts if a["alert_id"] == alert_id)
    assert surf_alert["acknowledged"] == 0

    # Acknowledge via the API.
    response = client.post(f"/api/alerts/{alert_id}/acknowledge")
    assert response.status_code == 200
    assert response.json() == {"ok": True}

    # GET confirms acknowledged=1.
    response = client.get("/api/alerts")
    alerts = response.json()["alerts"]
    acked = next(a for a in alerts if a["alert_id"] == alert_id)
    assert acked["acknowledged"] == 1

    # Acknowledge-all clears the remaining unacked alerts (fire one more
    # first, then ack-all).
    fired2 = fresh.evaluate({"psi": 0.5})  # model_drift_detected
    assert len(fired2) >= 1
    response = client.post("/api/alerts/acknowledge-all")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["acknowledged"] >= 1


# ── (3) Profiling → performance report ─────────────────────────────────────


def test_profiler_records_per_endpoint_latencies():
    """``Profiler.record(method, endpoint, duration, status)`` persists
    per-endpoint latency samples.

    After recording N requests, ``get_stats`` returns per-endpoint
    request_count / avg_latency / p50 / p95 / p99 / error_count.
    """
    pf = Profiler()
    pf.record("GET", "/api/test1", 0.010, 200)
    pf.record("GET", "/api/test1", 0.020, 200)
    pf.record("GET", "/api/test1", 0.030, 200)
    pf.record("GET", "/api/test1", 0.050, 500)  # error
    pf.record("GET", "/api/test2", 0.100, 200)

    stats = pf.get_stats(sort_by="count")
    # Two distinct endpoints recorded.
    assert len(stats) == 2
    # /api/test1 has 4 requests, 1 error.
    test1 = next(s for s in stats if s["endpoint"] == "/api/test1")
    assert test1["request_count"] == 4
    assert test1["error_count"] == 1
    assert test1["avg_latency_ms"] == pytest.approx(
        ((0.010 + 0.020 + 0.030 + 0.050) / 4) * 1000, rel=1e-2
    )
    # p50 = median of [0.010, 0.020, 0.030, 0.050] = (0.020 + 0.030) / 2 = 0.025
    assert test1["p50_ms"] == pytest.approx(25.0, abs=1.0)
    # p95 = sorted index 3 (0.050)
    assert test1["p95_ms"] == pytest.approx(50.0, abs=1.0)
    # /api/test2 has 1 request, 0 errors.
    test2 = next(s for s in stats if s["endpoint"] == "/api/test2")
    assert test2["request_count"] == 1
    assert test2["error_count"] == 0
    assert test2["avg_latency_ms"] == pytest.approx(100.0, rel=1e-2)


def test_profiler_get_slowest_returns_top_n_by_p95():
    """``get_slowest(N)`` returns the top-N endpoints ranked by p95
    latency descending.
    """
    pf = Profiler()
    pf.record("GET", "/api/fast", 0.005, 200)
    pf.record("GET", "/api/medium", 0.050, 200)
    pf.record("GET", "/api/slow", 0.500, 200)

    slowest = pf.get_slowest(limit=2)
    assert len(slowest) == 2
    # Slowest first.
    assert slowest[0]["endpoint"] == "/api/slow"
    assert slowest[1]["endpoint"] == "/api/medium"
    # p95 values descending.
    assert slowest[0]["p95_ms"] >= slowest[1]["p95_ms"]


def test_profiler_get_summary_returns_totals():
    """``get_summary`` returns total_endpoints / total_requests /
    total_errors / overall_error_rate across every recorded endpoint."""
    pf = Profiler()
    pf.record("GET", "/api/test1", 0.010, 200)
    pf.record("GET", "/api/test1", 0.020, 500)  # error
    pf.record("GET", "/api/test2", 0.030, 200)

    summary = pf.get_summary()
    assert summary["total_endpoints"] == 2
    assert summary["total_requests"] == 3
    assert summary["total_errors"] == 1
    # overall_error_rate = 1/3 * 100 ≈ 33.33%
    assert summary["overall_error_rate"] == pytest.approx(33.33, abs=0.1)


def test_profiling_stats_endpoint_via_production_app():
    """The production ``api.server.app`` exposes ``GET /api/profiling/stats``
    backed by the module-level ``profiler`` singleton.

    Reset the profiler to a clean baseline, make several authenticated
    requests to a known route (e.g. ``GET /api/system/health``), then
    query the stats endpoint and verify the route shows up with the
    expected request count.
    """
    # Reset the singleton before the test so we start from a clean baseline.
    profiler.reset()

    # Import the production app + disable rate limiter (mirrors the
    # test_integration.py pattern).
    from api.server import app

    try:
        from api.server import limiter  # type: ignore[attr-defined]

        limiter.enabled = False  # type: ignore[attr-defined]
    except ImportError:
        pass

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-token-conftest"}

    # Make N requests to a stable GET endpoint so the profiler accumulates
    # samples for that route.
    N = 5
    for _ in range(N):
        resp = client.get("/api/system/health", headers=headers)
        # Don't fail the test on a non-200 from the health route — the
        # profiling middleware records the latency regardless of status
        # code (verified above in the unit test). We just need the
        # middleware to fire.
        assert resp.status_code < 500, (
            f"health endpoint returned {resp.status_code}; "
            f"body={resp.text[:200]}"
        )

    # Query the stats endpoint.
    resp = client.get("/api/profiling/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Summary + per-endpoint list present.
    assert "summary" in body
    assert "endpoints" in body
    endpoints = body["endpoints"]
    # At least one of the recorded endpoints is the health route.
    # (The middleware records every authenticated route, so other
    # requests — including this very /api/profiling/stats request — may
    # also show up.)
    health_endpoints = [
        e for e in endpoints
        if "/api/system/health" in e.get("endpoint", "")
    ]
    assert len(health_endpoints) >= 1, (
        f"expected /api/system/health in profiling stats; got endpoints="
        f"{[e.get('endpoint') for e in endpoints]}"
    )
    health = health_endpoints[0]
    assert health["request_count"] >= N
    # avg_latency_ms is positive (every request took some non-zero time).
    assert health["avg_latency_ms"] >= 0


def test_profiling_reset_endpoint_clears_stats():
    """``POST /api/profiling/reset`` clears the in-memory profiler state.

    After reset, ``GET /api/profiling/stats`` returns an empty endpoints
    list and zeroed totals.
    """
    from api.server import app

    try:
        from api.server import limiter  # type: ignore[attr-defined]

        limiter.enabled = False  # type: ignore[attr-defined]
    except ImportError:
        pass

    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": "Bearer test-token-conftest"}

    # Make a request so the profiler has at least one entry.
    client.get("/api/system/health", headers=headers)
    pre_reset = client.get("/api/profiling/stats", headers=headers).json()
    assert pre_reset["summary"]["total_requests"] >= 1

    # Reset.
    resp = client.post("/api/profiling/reset", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    # Stats now empty.
    post_reset = client.get("/api/profiling/stats", headers=headers).json()
    # The reset wipes every endpoint's stats, but the subsequent
    # /api/profiling/stats call itself is recorded AFTER the reset (so
    # total_requests is at least 1 again — but the totals only include
    # the post-reset traffic).
    assert post_reset["summary"]["total_endpoints"] <= 1
