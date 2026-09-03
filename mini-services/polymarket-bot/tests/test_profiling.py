"""tests/test_profiling.py — W15-4 Profiler contract tests.

Covers the four behavioural guarantees the W15-4 task spec asks for:

  1. **EndpointStats** computation — ``p50`` / ``p95`` / ``p99`` /
     ``error_rate`` / ``avg_latency`` / ``to_dict`` shape.
  2. **Profiler.record** stores each request with the correct metadata
     and the bounded-latencies-list cap evicts oldest-first (no
     unbounded memory growth).
  3. **Profiler.get_stats / get_slowest / get_summary** return the
     expected shape and respect the ``sort_by`` argument.
  4. **Thread safety** — concurrent ``record`` calls don't lose counts
     (the ``threading.Lock`` serialises mutations).
  5. **Reset** clears all state — used by ``POST /api/profiling/reset``
     for hermetic isolation.
  6. **API routes** — ``GET /api/profiling/stats`` /
     ``GET /api/profiling/slowest`` / ``POST /api/profiling/reset``
     wired into ``api.server.app``, auth-protected, return the expected
     JSON shape.
"""
from __future__ import annotations

import threading
import time

import pytest
from fastapi.testclient import TestClient

from core.profiling import EndpointStats, Profiler, profiler

# ── Defensive: disable the rate-limit middleware so a fast test sequence ──
# against a per-minute-limited route doesn't 429 mid-suite (mirrors the
# pattern in tests/test_openapi.py).
try:  # pragma: no cover — toggle path only fires when slowapi is present
    from api.server import limiter  # type: ignore[attr-defined]

    limiter.enabled = False  # type: ignore[attr-defined]
except ImportError:
    pass

# ``conftest.py`` sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so the
# bearer token below matches what the ``enforce_api_auth`` middleware
# accepts.
VALID_TOKEN = "test-token-conftest"


# ═══════════════════════════════════════════════════════════════════════════
# 1. EndpointStats — percentile / rate computation
# ═══════════════════════════════════════════════════════════════════════════


class TestEndpointStats:
    """Behavioural contract for the ``EndpointStats`` dataclass."""

    def test_empty_stats_zero_everything(self):
        """A freshly-constructed ``EndpointStats`` must report 0 for every
        percentile and for the error rate (no division-by-zero)."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        assert s.avg_latency == 0.0
        assert s.p50 == 0.0
        assert s.p95 == 0.0
        assert s.p99 == 0.0
        assert s.error_rate == 0.0
        d = s.to_dict()
        assert d["endpoint"] == "/api/x"
        assert d["method"] == "GET"
        assert d["request_count"] == 0
        assert d["avg_latency_ms"] == 0
        assert d["p50_ms"] == 0
        assert d["p95_ms"] == 0
        assert d["p99_ms"] == 0
        assert d["error_count"] == 0
        assert d["error_rate"] == 0
        assert d["last_called"] == 0.0

    def test_avg_latency(self):
        """``avg_latency`` is the arithmetic mean of all recorded durations."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        s.request_count = 4
        s.total_time = 0.1 + 0.2 + 0.3 + 0.4
        assert s.avg_latency == pytest.approx(0.25)

    def test_p50_is_median(self):
        """``p50`` is the median of the ``latencies`` list."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        s.latencies = [0.1, 0.2, 0.3, 0.4, 0.5]
        assert s.p50 == pytest.approx(0.3)
        # Even count → median is the mean of the two middle values.
        s.latencies = [0.1, 0.2, 0.3, 0.4]
        assert s.p50 == pytest.approx(0.25)

    def test_p95_index_clamped(self):
        """``p95`` returns the 95th-percentile value, clamped to the last
        index (so a list shorter than 20 elements doesn't index past the
        end)."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        # 100 samples uniform on [0.01, 1.00] — the 95th percentile
        # rounds to ~0.95.
        s.latencies = [i / 100.0 for i in range(1, 101)]
        assert s.p95 == pytest.approx(0.95, abs=0.05)
        # Small list: idx = int(5 * 0.95) = 4, clamped to len-1 = 4.
        s.latencies = [0.1, 0.2, 0.3, 0.4, 0.5]
        assert s.p95 == pytest.approx(0.5)

    def test_p99_index_clamped(self):
        """``p99`` returns the 99th-percentile value, clamped to the last
        index."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        s.latencies = [i / 100.0 for i in range(1, 101)]
        assert s.p99 == pytest.approx(0.99, abs=0.05)
        # Small list: idx = int(5 * 0.99) = 4, clamped to len-1 = 4.
        s.latencies = [0.1, 0.2, 0.3, 0.4, 0.5]
        assert s.p99 == pytest.approx(0.5)

    def test_error_rate_computation(self):
        """``error_rate`` is ``error_count / request_count``."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        s.request_count = 100
        s.error_count = 5
        assert s.error_rate == pytest.approx(0.05)
        # Zero requests → zero error rate (no division-by-zero).
        s.request_count = 0
        assert s.error_rate == 0.0

    def test_to_dict_converts_to_ms_and_pct(self):
        """``to_dict`` converts seconds → ms and ratio → percent."""
        s = EndpointStats(endpoint="/api/x", method="GET")
        s.request_count = 10
        s.total_time = 1.0  # avg = 0.1 s = 100 ms
        s.latencies = [0.05, 0.10, 0.15, 0.20, 0.25]
        s.error_count = 2
        d = s.to_dict()
        assert d["avg_latency_ms"] == pytest.approx(100.0)
        # p50 of [0.05, 0.10, 0.15, 0.20, 0.25] = 0.15 → 150 ms
        assert d["p50_ms"] == pytest.approx(150.0)
        # error_rate 2/10 = 0.20 → 20 %
        assert d["error_rate"] == 20.0

    def test_to_dict_has_required_keys(self):
        """``to_dict`` shape is the contract the dashboard / perf_report
        depends on — every key below MUST be present."""
        d = EndpointStats(endpoint="/api/x", method="GET").to_dict()
        for key in (
            "endpoint",
            "method",
            "request_count",
            "avg_latency_ms",
            "p50_ms",
            "p95_ms",
            "p99_ms",
            "error_count",
            "error_rate",
            "last_called",
        ):
            assert key in d, f"to_dict missing required key {key!r}"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Profiler.record — bounded latencies list + counting
# ═══════════════════════════════════════════════════════════════════════════


class TestProfilerRecord:
    """Behavioural contract for ``Profiler.record``."""

    def test_record_creates_new_endpoint_entry(self):
        """A first-time ``(method, endpoint)`` pair creates a new
        ``EndpointStats`` entry under the ``f"{method} {endpoint}"`` key."""
        p = Profiler()
        p.record("GET", "/api/orders", 0.05, 200)
        stats = p.get_stats()
        assert len(stats) == 1
        assert stats[0]["endpoint"] == "/api/orders"
        assert stats[0]["method"] == "GET"
        assert stats[0]["request_count"] == 1
        assert stats[0]["p95_ms"] == pytest.approx(50.0)

    def test_record_accumulates_counts_and_latencies(self):
        """Subsequent ``record`` calls for the same key increment the
        count and append to the latencies list."""
        p = Profiler()
        for _ in range(5):
            p.record("GET", "/api/orders", 0.1, 200)
        stats = p.get_stats()
        assert stats[0]["request_count"] == 5
        assert stats[0]["avg_latency_ms"] == pytest.approx(100.0)

    def test_record_keys_by_method_plus_endpoint(self):
        """``GET /api/orders`` and ``DELETE /api/orders`` are tracked as
        separate endpoints (the key is ``f"{method} {path}"``)."""
        p = Profiler()
        p.record("GET", "/api/orders", 0.05, 200)
        p.record("DELETE", "/api/orders", 0.10, 200)
        summary = p.get_summary()
        assert summary["total_endpoints"] == 2

    def test_record_counts_errors_above_400(self):
        """``status >= 400`` increments the error counter."""
        p = Profiler()
        p.record("GET", "/api/orders", 0.05, 200)
        p.record("GET", "/api/orders", 0.05, 404)
        p.record("GET", "/api/orders", 0.05, 500)
        stats = p.get_stats()
        assert stats[0]["error_count"] == 2
        assert stats[0]["error_rate"] == pytest.approx(66.67, abs=0.1)

    def test_record_4xx_counts_as_error(self):
        """``status == 400`` is an error (≥ 400, not just > 500)."""
        p = Profiler()
        p.record("POST", "/api/trade", 0.05, 400)
        stats = p.get_stats()
        assert stats[0]["error_count"] == 1
        assert stats[0]["error_rate"] == 100.0

    def test_record_3xx_not_an_error(self):
        """``status == 302`` is NOT an error (< 400)."""
        p = Profiler()
        p.record("GET", "/api/orders", 0.05, 302)
        stats = p.get_stats()
        assert stats[0]["error_count"] == 0
        assert stats[0]["error_rate"] == 0.0

    def test_record_latencies_list_bounded(self):
        """After ``MAX_LATENCIES + 1`` records, the latencies list is
        trimmed to the last ``MAX_LATENCIES`` entries — no unbounded
        memory growth."""
        p = Profiler()
        # Sanity: MAX_LATENCIES is the documented cap.
        assert Profiler.MAX_LATENCIES == 1000
        for i in range(Profiler.MAX_LATENCIES + 50):
            p.record("GET", "/api/orders", float(i) / 1000.0, 200)
        # Internal: latencies list is capped.
        with p._lock:
            stat = p._stats["GET /api/orders"]
            assert len(stat.latencies) == Profiler.MAX_LATENCIES
            # Oldest 50 evicted; the list now starts at index 50.
            assert stat.latencies[0] == pytest.approx(50 / 1000.0)
            assert stat.latencies[-1] == pytest.approx((Profiler.MAX_LATENCIES + 49) / 1000.0)
        # Public: request_count is NOT capped (the cap is on the
        # latencies list only, so summary stats reflect lifetime traffic).
        summary = p.get_summary()
        assert summary["total_requests"] == Profiler.MAX_LATENCIES + 50

    def test_record_updates_last_called(self):
        """``last_called`` is updated on every record call to the
        current ``time.time()``."""
        p = Profiler()
        before = time.time()
        p.record("GET", "/api/orders", 0.05, 200)
        after = time.time()
        with p._lock:
            stat = p._stats["GET /api/orders"]
            assert before <= stat.last_called <= after


# ═══════════════════════════════════════════════════════════════════════════
# 3. Profiler.get_stats / get_slowest / get_summary / reset
# ═══════════════════════════════════════════════════════════════════════════


class TestProfilerQueries:
    """Behavioural contract for the query / reset methods."""

    def test_get_stats_default_sort_by_p95(self):
        """``get_stats()`` defaults to sorting by p95 descending."""
        p = Profiler()
        # /slow has higher p95 (0.20s) than /fast (0.05s).
        for _ in range(20):
            p.record("GET", "/api/fast", 0.05, 200)
        for _ in range(20):
            p.record("GET", "/api/slow", 0.20, 200)
        stats = p.get_stats()
        assert stats[0]["endpoint"] == "/api/slow"
        assert stats[1]["endpoint"] == "/api/fast"

    def test_get_stats_sort_by_count(self):
        """``sort_by="count"`` orders by request_count descending."""
        p = Profiler()
        for _ in range(3):
            p.record("GET", "/api/three", 0.05, 200)
        for _ in range(10):
            p.record("GET", "/api/ten", 0.05, 200)
        stats = p.get_stats(sort_by="count")
        assert stats[0]["endpoint"] == "/api/ten"
        assert stats[0]["request_count"] == 10
        assert stats[1]["endpoint"] == "/api/three"

    def test_get_stats_sort_by_errors(self):
        """``sort_by="errors"`` orders by error_count descending."""
        p = Profiler()
        p.record("GET", "/api/clean", 0.05, 200)
        for _ in range(5):
            p.record("GET", "/api/broken", 0.05, 500)
        stats = p.get_stats(sort_by="errors")
        assert stats[0]["endpoint"] == "/api/broken"
        assert stats[0]["error_count"] == 5

    def test_get_stats_unknown_sort_falls_back_to_p95(self):
        """An unknown ``sort_by`` value falls back to ``p95`` (does not
        raise ``KeyError``)."""
        p = Profiler()
        for _ in range(5):
            p.record("GET", "/api/x", 0.05, 200)
        # Just verify no exception; the result is sorted by p95.
        stats = p.get_stats(sort_by="unknown_metric")
        assert len(stats) == 1

    def test_get_stats_returns_list_of_dicts_with_to_dict_shape(self):
        """Every entry returned by ``get_stats`` carries the full
        ``EndpointStats.to_dict()`` shape (no internal fields leak)."""
        p = Profiler()
        p.record("GET", "/api/x", 0.05, 200)
        stats = p.get_stats()
        assert isinstance(stats, list)
        assert all(isinstance(s, dict) for s in stats)
        for s in stats:
            for key in (
                "endpoint", "method", "request_count",
                "avg_latency_ms", "p50_ms", "p95_ms", "p99_ms",
                "error_count", "error_rate", "last_called",
            ):
                assert key in s

    def test_get_slowest_respects_limit(self):
        """``get_slowest(limit=N)`` returns at most N entries."""
        p = Profiler()
        for i in range(10):
            p.record("GET", f"/api/ep{i}", float(i) / 100.0, 200)
        slowest = p.get_slowest(limit=3)
        assert len(slowest) == 3
        # Sorted by p95 descending → the slowest (ep9 with 0.09s) is first.
        assert slowest[0]["endpoint"] == "/api/ep9"

    def test_get_slowest_zero_limit_returns_empty(self):
        """``get_slowest(limit=0)`` returns an empty list (not an error)."""
        p = Profiler()
        p.record("GET", "/api/x", 0.05, 200)
        slowest = p.get_slowest(limit=0)
        assert slowest == []

    def test_get_summary_empty_state(self):
        """``get_summary`` on a fresh Profiler reports all zeros."""
        p = Profiler()
        summary = p.get_summary()
        assert summary["total_endpoints"] == 0
        assert summary["total_requests"] == 0
        assert summary["total_errors"] == 0
        assert summary["overall_error_rate"] == 0

    def test_get_summary_aggregates_across_endpoints(self):
        """``get_summary`` sums ``request_count`` / ``error_count`` across
        every endpoint, and ``overall_error_rate`` is the global ratio."""
        p = Profiler()
        # 100 requests across two endpoints, 5 errors total.
        for _ in range(50):
            p.record("GET", "/api/a", 0.05, 200)
        for _ in range(45):
            p.record("GET", "/api/b", 0.05, 200)
        for _ in range(5):
            p.record("GET", "/api/b", 0.05, 500)
        summary = p.get_summary()
        assert summary["total_endpoints"] == 2
        assert summary["total_requests"] == 100
        assert summary["total_errors"] == 5
        assert summary["overall_error_rate"] == 5.0

    def test_reset_clears_all_state(self):
        """``reset`` drops every endpoint's stats so the next
        ``get_stats`` returns an empty list."""
        p = Profiler()
        for _ in range(10):
            p.record("GET", "/api/x", 0.05, 200)
        assert p.get_summary()["total_requests"] == 10
        p.reset()
        assert p.get_summary()["total_endpoints"] == 0
        assert p.get_stats() == []


# ═══════════════════════════════════════════════════════════════════════════
# 4. Thread safety
# ═══════════════════════════════════════════════════════════════════════════


class TestThreadSafety:
    """The ``threading.Lock`` serialises mutations so concurrent record
    calls don't lose counts."""

    def test_concurrent_record_no_lost_updates(self):
        """8 threads × 250 record calls each → exactly 2000 requests
        recorded (no lost increments under the lock)."""
        p = Profiler()
        n_threads = 8
        n_per_thread = 250
        expected_total = n_threads * n_per_thread

        def _worker() -> None:
            for _ in range(n_per_thread):
                p.record("GET", "/api/x", 0.001, 200)

        threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = p.get_summary()
        assert summary["total_requests"] == expected_total

    def test_concurrent_record_and_get_stats_no_crash(self):
        """Concurrent record + get_stats + get_summary from multiple
        threads runs for 200ms without crashing (lock prevents
        ``RuntimeError: dictionary changed size during iteration``)."""
        p = Profiler()
        stop = threading.Event()

        def _recorder() -> None:
            i = 0
            while not stop.is_set():
                p.record("GET", f"/api/ep{i % 5}", 0.0001, 200 if i % 10 else 500)
                i += 1

        def _reader() -> None:
            while not stop.is_set():
                p.get_stats()
                p.get_summary()

        threads = [
            threading.Thread(target=_recorder),
            threading.Thread(target=_recorder),
            threading.Thread(target=_reader),
        ]
        for t in threads:
            t.start()
        time.sleep(0.2)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
        # No assertion needed — the test passes if no exception propagated
        # out of any thread (any uncaught exception would surface as a
        # ``threading.Thread.join`` traceback in pytest output).
        assert p.get_summary()["total_requests"] > 0


# ═══════════════════════════════════════════════════════════════════════════
# 5. Singleton
# ═══════════════════════════════════════════════════════════════════════════


class TestSingleton:
    """The module-level ``profiler`` instance is a ``Profiler``."""

    def test_singleton_is_profiler_instance(self):
        assert isinstance(profiler, Profiler)

    def test_singleton_reset_returns_empty_state(self):
        """``profiler.reset()`` is the operational escape hatch — verify
        it leaves the singleton in a clean state."""
        profiler.reset()
        summary = profiler.get_summary()
        assert summary["total_endpoints"] == 0
        assert summary["total_requests"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. API routes (TestClient against api.server.app)
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def client() -> TestClient:
    """TestClient bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` lets the global exception handler
    return sanitized 500s on the error-path tests. Mirrors the pattern
    in ``tests/test_openapi.py``.
    """
    return TestClient(_app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """Bearer token header every authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


@pytest.fixture(autouse=True)
def _reset_profiler_singleton():
    """Reset the singleton profiler before AND after every API test so a
    prior test's recorded latencies don't leak into the next test's
    assertions. Mirrors the autouse-reset convention in
    ``tests/test_rate_limit_tracker.py``."""
    profiler.reset()
    yield
    profiler.reset()


# Import once at module load so the ``client`` fixture can use it
# without re-importing per-test (the import is heavy — full lifespan
# of the polymarket-bot app).
try:
    from api.server import app as _app
except ImportError:  # pragma: no cover — defensive: api.server may be heavy
    _app = None  # type: ignore[assignment]


# Skip the entire API-route test class if api.server couldn't be imported
# (e.g. running the suite against a stripped-down install where FastAPI
# isn't present). The unit tests above cover the Profiler contract
# regardless.
@pytest.mark.skipif(_app is None, reason="api.server.app not importable")
class TestAPIRoutes:
    """The three profiling routes wired into ``api.server.app``."""

    def test_stats_returns_200_with_summary_and_endpoints(self, client, auth_headers):
        """``GET /api/profiling/stats`` returns 200 with the
        ``summary`` + ``endpoints`` shape."""
        # Pre-populate the singleton so the response has at least one row.
        profiler.record("GET", "/api/test_route", 0.05, 200)
        response = client.get("/api/profiling/stats", headers=auth_headers)
        assert response.status_code == 200, (
            f"GET /api/profiling/stats must return 200; got "
            f"{response.status_code}. Body: {response.text[:300]!r}"
        )
        data = response.json()
        assert "summary" in data
        assert "endpoints" in data
        assert isinstance(data["endpoints"], list)
        assert data["summary"]["total_endpoints"] >= 1
        # The pre-populated row should appear.
        endpoints = data["endpoints"]
        paths = {ep["endpoint"] for ep in endpoints}
        assert "/api/test_route" in paths

    def test_stats_supports_sort_by_query_param(self, client, auth_headers):
        """``?sort_by=count`` is honoured (no 422 / 500)."""
        for _ in range(5):
            profiler.record("GET", "/api/frequent", 0.01, 200)
        profiler.record("GET", "/api/rare", 0.5, 200)
        response = client.get(
            "/api/profiling/stats?sort_by=count", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        # Sorted by count desc → /api/frequent (5) before /api/rare (1).
        assert data["endpoints"][0]["endpoint"] == "/api/frequent"
        assert data["endpoints"][0]["request_count"] == 5

    def test_stats_requires_auth(self, client):
        """``GET /api/profiling/stats`` without a bearer token returns
        401 (``enforce_api_auth`` middleware rejects)."""
        response = client.get("/api/profiling/stats")
        assert response.status_code == 401

    def test_slowest_returns_200_with_slowest_list(self, client, auth_headers):
        """``GET /api/profiling/slowest?limit=N`` returns 200 with a
        ``slowest`` list of length ≤ N."""
        for i in range(5):
            profiler.record("GET", f"/api/ep{i}", float(i) / 100.0, 200)
        response = client.get(
            "/api/profiling/slowest?limit=3", headers=auth_headers
        )
        assert response.status_code == 200
        data = response.json()
        assert "slowest" in data
        assert isinstance(data["slowest"], list)
        assert len(data["slowest"]) <= 3
        # Sorted by p95 descending → /api/ep4 (0.04s) is first.
        assert data["slowest"][0]["endpoint"] == "/api/ep4"

    def test_slowest_limit_validation(self, client, auth_headers):
        """``limit`` is clamped to ``[1, 100]`` by FastAPI's ``Query(ge=1,
        le=100)`` — values outside that range return 422."""
        response = client.get(
            "/api/profiling/slowest?limit=0", headers=auth_headers
        )
        assert response.status_code == 422
        response = client.get(
            "/api/profiling/slowest?limit=200", headers=auth_headers
        )
        assert response.status_code == 422

    def test_reset_returns_ok(self, client, auth_headers):
        """``POST /api/profiling/reset`` returns 200 with ``{"ok": true}``
        and zeroes the singleton's pre-reset state.

        NOTE: the ``POST /api/profiling/reset`` request ITSELF is recorded
        by the request_logging_middleware AFTER the handler returns (so
        the response already reflects the reset). That means the
        profiler will hold a single ``POST /api/profiling/reset`` entry
        immediately after this call — that's the correct contract, not
        a leak. We assert that every PRE-RESET entry is gone."""
        profiler.record("GET", "/api/pre_reset_endpoint", 0.05, 200)
        assert profiler.get_summary()["total_requests"] == 1
        response = client.post("/api/profiling/reset", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"ok": True}
        # The pre-reset endpoint must be gone — only the reset call
        # itself (recorded by the middleware after the handler) may
        # remain in the stats.
        stats = profiler.get_stats()
        endpoints = {(s["method"], s["endpoint"]) for s in stats}
        assert ("GET", "/api/pre_reset_endpoint") not in endpoints
        # The reset route's own recording IS expected to land — the
        # middleware records after the handler returns, so by the time
        # we read the stats here, the POST /api/profiling/reset call
        # is already in the dict. This is the intended contract.
        assert ("POST", "/api/profiling/reset") in endpoints

    def test_middleware_records_every_request(self, client, auth_headers):
        """The request_logging_middleware feeds every request into
        ``profiler.record`` — a single ``GET /api/health`` call should
        show up in the profiler's stats under the ``GET /api/health``
        key."""
        # Reset to a clean baseline so we don't see routes hit by prior
        # tests in this same TestClient session.
        profiler.reset()
        # Hit a PUBLIC_PATH route (no auth needed, but pass it for symmetry).
        client.get("/api/health", headers=auth_headers)
        stats = profiler.get_stats()
        health_eps = [s for s in stats if s["endpoint"] == "/api/health" and s["method"] == "GET"]
        assert len(health_eps) == 1, (
            f"profiler must record GET /api/health; got endpoints "
            f"{[s['method'] + ' ' + s['endpoint'] for s in stats]}"
        )
        assert health_eps[0]["request_count"] >= 1

    def test_middleware_records_4xx_as_error(self, client, auth_headers):
        """A 404 response (e.g. ``GET /api/no-such-route``) increments
        the error counter so the ``error_rate`` field reflects the
        failure ratio."""
        profiler.reset()
        client.get("/api/no-such-route", headers=auth_headers)
        stats = profiler.get_stats()
        miss_eps = [
            s for s in stats if s["endpoint"] == "/api/no-such-route"
        ]
        assert len(miss_eps) == 1
        assert miss_eps[0]["error_count"] == 1
        assert miss_eps[0]["error_rate"] == 100.0
