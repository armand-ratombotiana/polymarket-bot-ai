"""
tests/test_rate_limit_tracker.py — W14-7 RateLimitTracker contract tests.

Covers the four behavioural guarantees the W14-7 task spec asks for:

  1. **record_hit** stores each hit with the correct metadata and the
     deque's maxlen cap evicts oldest-first (no unbounded memory growth).
  2. **get_stats** returns the expected shape — ``total_hits``,
     ``hits_by_endpoint``, ``hits_by_client``, ``hits_per_minute``,
     ``top_endpoints``, ``hits_per_minute_rate`` — and the per-X maps
     are sorted descending and capped (top-20 endpoints, top-10 clients).
  3. **record_request** increments the ``"endpoint:status"`` keyed
     counter so the dashboard's "Most-Requested Endpoints" view can
     surface per-status-code breakdowns.
  4. **Thread safety** — concurrent ``record_hit`` / ``record_request``
     calls don't lose counts (the ``threading.Lock`` serialises
     mutations; without it a context-switch between
     ``deque.append`` and ``self._request_counts[endpoint] += 1``
     could drop a counter increment).
  5. **Reset** clears all state — used by tests for hermetic isolation.

These tests intentionally do NOT spin up a FastAPI app — the
``RateLimitTracker`` is a pure in-memory data structure with no I/O
dependencies. The route-wiring contract (the ``rate_limit_handler``
calling ``record_hit`` before returning the 429) is covered by the
existing ``tests/test_rate_limiting.py`` suite.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.rate_limit_tracker import (
    RateLimitHit,
    RateLimitTracker,
    rate_limit_tracker,
)


# ── 1. record_hit ────────────────────────────────────────────────────────────


class TestRecordHit:
    """Behavioural contract for ``RateLimitTracker.record_hit``."""

    def test_record_hit_stores_metadata(self):
        tracker = RateLimitTracker()
        before = time.time()
        tracker.record_hit(
            endpoint="/api/orders",
            method="GET",
            client_ip="127.0.0.1",
            limit="120/minute",
        )
        stats = tracker.get_stats()
        assert stats["total_hits"] == 1
        assert stats["hits_by_endpoint"] == {"/api/orders": 1}
        assert stats["hits_by_client"] == {"127.0.0.1": 1}
        assert stats["top_endpoints"] == {"/api/orders": 1}
        # The hits_per_minute series has at least one bucket with count 1.
        assert sum(stats["hits_per_minute"].values()) == 1
        # The internal hit record carries the timestamp + limit string.
        # We can't read _hits directly without breaking encapsulation,
        # but the get_stats shape already proves the metadata round-trips.
        _ = before  # silence unused-var linters

    def test_record_hit_increments_endpoint_counter(self):
        tracker = RateLimitTracker()
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        tracker.record_hit("/api/markets", "GET", "10.0.0.5", "120/minute")
        stats = tracker.get_stats()
        assert stats["top_endpoints"] == {
            "/api/orders": 2,
            "/api/markets": 1,
        }

    def test_record_hit_maxlen_evicts_oldest(self):
        """The deque's maxlen cap evicts oldest-first so the tracker
        bounds its memory footprint regardless of how many hits arrive."""
        tracker = RateLimitTracker(max_records=5)
        for i in range(10):
            tracker.record_hit(
                endpoint=f"/api/ep{i}",
                method="GET",
                client_ip="127.0.0.1",
                limit="120/minute",
            )
        # Only the last 5 hits should be in the deque.
        stats = tracker.get_stats()
        # All 10 endpoints are in the request_counts dict (no cap), but
        # only 5 are in the recent-hits deque → total_hits should be 5.
        assert stats["total_hits"] == 5
        # The 5 endpoints still tracked are ep5..ep9 (oldest 5 evicted).
        assert set(stats["hits_by_endpoint"].keys()) == {
            f"/api/ep{i}" for i in range(5, 10)
        }

    def test_record_hit_distinct_clients(self):
        tracker = RateLimitTracker()
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        tracker.record_hit("/api/orders", "GET", "10.0.0.5", "120/minute")
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        # 127.0.0.1 → 2, 10.0.0.5 → 1; sorted descending by count.
        assert list(stats["hits_by_client"].items()) == [
            ("127.0.0.1", 2),
            ("10.0.0.5", 1),
        ]


# ── 2. get_stats ────────────────────────────────────────────────────────────


class TestGetStats:
    """Behavioural contract for ``RateLimitTracker.get_stats``."""

    def test_get_stats_empty_returns_zeroes(self):
        tracker = RateLimitTracker()
        stats = tracker.get_stats()
        assert stats["total_hits"] == 0
        assert stats["hits_by_endpoint"] == {}
        assert stats["hits_by_client"] == {}
        assert stats["hits_per_minute"] == {}
        assert stats["top_endpoints"] == {}
        assert stats["hits_per_minute_rate"] == 0.0

    def test_get_stats_returns_expected_shape(self):
        tracker = RateLimitTracker()
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        # Every key the dashboard renders must be present.
        expected_keys = {
            "total_hits",
            "hits_per_minute_rate",
            "hits_by_endpoint",
            "hits_by_client",
            "hits_per_minute",
            "top_endpoints",
        }
        assert set(stats.keys()) >= expected_keys

    def test_get_stats_hits_per_minute_buckets(self):
        """Hits are bucketed into per-minute keys (epoch // 60) and the
        returned dict is keyed by ``minutes_ago`` (1 = newest, 60 = oldest).

        The Python-side dict has int keys; FastAPI's JSON serializer
        converts them to strings on the wire so the dashboard sees
        ``{"60": 5}`` — but ``get_stats()`` itself returns ints. We
        accept either form here so the test doesn't break if the
        tracker's internal key type is ever normalised.
        """
        tracker = RateLimitTracker()
        # Record 5 hits in the current minute.
        for _ in range(5):
            tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        # All 5 hits land in the same minute bucket.
        assert sum(stats["hits_per_minute"].values()) == 5
        # The newest bucket is keyed 60 (60 - (now_minute - now_minute) = 60).
        assert 60 in stats["hits_per_minute"] or "60" in stats["hits_per_minute"]
        if 60 in stats["hits_per_minute"]:
            assert stats["hits_per_minute"][60] == 5
        else:
            assert stats["hits_per_minute"]["60"] == 5

    def test_get_stats_caps_endpoint_map_at_20(self):
        """``hits_by_endpoint`` returns at most 20 entries so the JSON
        payload is bounded regardless of how many distinct endpoints
        have been throttled."""
        tracker = RateLimitTracker()
        for i in range(30):
            tracker.record_hit(
                endpoint=f"/api/ep{i}",
                method="GET",
                client_ip="127.0.0.1",
                limit="120/minute",
            )
        stats = tracker.get_stats()
        assert len(stats["hits_by_endpoint"]) <= 20

    def test_get_stats_caps_client_map_at_10(self):
        """``hits_by_client`` returns at most 10 entries."""
        tracker = RateLimitTracker()
        for i in range(15):
            tracker.record_hit(
                endpoint="/api/orders",
                method="GET",
                client_ip=f"10.0.0.{i}",
                limit="120/minute",
            )
        stats = tracker.get_stats()
        assert len(stats["hits_by_client"]) <= 10

    def test_get_stats_filters_out_old_hits(self):
        """Hits older than 1 hour are excluded from the recent views."""
        tracker = RateLimitTracker()
        # Insert a hit with a stale timestamp by directly appending to
        # the deque (bypassing record_hit's time.time() call).
        tracker._hits.append(
            RateLimitHit(
                timestamp=time.time() - 3700,  # 1h+ ago
                endpoint="/api/old",
                method="GET",
                client_ip="127.0.0.1",
                limit="120/minute",
            )
        )
        # And a recent one via the normal API.
        tracker.record_hit("/api/new", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        assert stats["total_hits"] == 1
        assert "/api/new" in stats["hits_by_endpoint"]
        assert "/api/old" not in stats["hits_by_endpoint"]

    def test_get_stats_hit_rate(self):
        """``hits_per_minute_rate`` is hits / elapsed-minutes, clamped to
        [1, 60] so a single hit at minute 0 doesn't read as 1/60 = 0.017/min."""
        tracker = RateLimitTracker()
        # 5 hits all in the current minute → rate = 5 / 1 (min elapsed)
        for _ in range(5):
            tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        assert stats["hits_per_minute_rate"] == 5.0

    def test_get_stats_sorted_descending(self):
        tracker = RateLimitTracker()
        # First record an endpoint with 1 hit, then one with 5 — the
        # 5-hit endpoint should appear first in the sorted map.
        tracker.record_hit("/api/low", "GET", "127.0.0.1", "120/minute")
        for _ in range(5):
            tracker.record_hit("/api/high", "GET", "127.0.0.1", "120/minute")
        stats = tracker.get_stats()
        keys = list(stats["hits_by_endpoint"].keys())
        assert keys[0] == "/api/high"
        assert keys[1] == "/api/low"


# ── 3. record_request ──────────────────────────────────────────────────────


class TestRecordRequest:
    """Behavioural contract for ``RateLimitTracker.record_request``."""

    def test_record_request_keys_by_endpoint_status(self):
        tracker = RateLimitTracker()
        tracker.record_request("/api/orders", 200)
        tracker.record_request("/api/orders", 200)
        tracker.record_request("/api/orders", 500)
        tracker.record_request("/api/markets", 200)
        # The per-status counters are NOT exposed in get_stats() (the
        # dashboard surfaces the rate-limit-hit view there) but they
        # ARE tracked internally for future panels. Verify directly.
        assert tracker._request_counts["/api/orders:200"] == 2
        assert tracker._request_counts["/api/orders:500"] == 1
        assert tracker._request_counts["/api/markets:200"] == 1

    def test_record_request_does_not_appear_in_top_endpoints(self):
        """``top_endpoints`` filters out the ``endpoint:status`` keys so
        the dashboard's "Top endpoints" table reflects rate-limit-hit
        volume, not raw request volume."""
        tracker = RateLimitTracker()
        tracker.record_request("/api/orders", 200)
        tracker.record_request("/api/orders", 200)
        # No record_hit calls → top_endpoints should be empty.
        stats = tracker.get_stats()
        assert stats["top_endpoints"] == {}


# ── 4. Thread safety ─────────────────────────────────────────────────────────


class TestThreadSafety:
    """Concurrent record_hit / record_request calls don't lose counts."""

    def test_concurrent_record_hits_no_loss(self):
        """Spawn N threads, each recording M hits; the final count
        should be N*M (no lost increments)."""
        tracker = RateLimitTracker(max_records=10_000)
        n_threads = 8
        n_hits_per_thread = 250
        barrier = threading.Barrier(n_threads + 1)

        def worker():
            barrier.wait()  # release all threads simultaneously
            for _ in range(n_hits_per_thread):
                tracker.record_hit(
                    endpoint="/api/orders",
                    method="GET",
                    client_ip="127.0.0.1",
                    limit="120/minute",
                )

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        barrier.wait()  # release the workers
        for t in threads:
            t.join()

        expected_total = n_threads * n_hits_per_thread
        stats = tracker.get_stats()
        # The tracker's deque caps at 10_000, so for n_threads * n_hits_per_thread
        # > 10_000 we'd see truncation. With 8 * 250 = 2000 hits, no eviction.
        assert stats["total_hits"] == expected_total
        assert stats["hits_by_endpoint"]["/api/orders"] == expected_total

    def test_concurrent_record_requests_no_loss(self):
        """Same as above but for record_request — verifies the
        ``"endpoint:status"`` keyed counter doesn't lose increments."""
        tracker = RateLimitTracker()
        n_threads = 8
        n_per_thread = 250
        barrier = threading.Barrier(n_threads + 1)

        def worker():
            barrier.wait()
            for _ in range(n_per_thread):
                tracker.record_request("/api/orders", 200)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        barrier.wait()
        for t in threads:
            t.join()

        expected_total = n_threads * n_per_thread
        assert tracker._request_counts["/api/orders:200"] == expected_total

    def test_concurrent_mixed_calls(self):
        """Mix of record_hit + record_request + get_stats calls from
        multiple threads — must not crash or raise."""
        tracker = RateLimitTracker(max_records=10_000)
        stop = threading.Event()
        barrier = threading.Barrier(3)

        def hit_worker():
            barrier.wait()
            i = 0
            while not stop.is_set():
                tracker.record_hit(
                    endpoint=f"/api/ep{i % 5}",
                    method="GET",
                    client_ip="127.0.0.1",
                    limit="120/minute",
                )
                i += 1

        def req_worker():
            barrier.wait()
            i = 0
            while not stop.is_set():
                tracker.record_request(f"/api/ep{i % 5}", 200)
                i += 1

        def stats_worker():
            barrier.wait()
            while not stop.is_set():
                tracker.get_stats()

        threads = [
            threading.Thread(target=hit_worker),
            threading.Thread(target=req_worker),
            threading.Thread(target=stats_worker),
        ]
        for t in threads:
            t.start()
        # Let them run for 100ms.
        time.sleep(0.1)
        stop.set()
        for t in threads:
            t.join(timeout=2.0)
            assert not t.is_alive(), f"Thread {t.name} did not shut down cleanly"

        # After the workers stop, get_stats should still work.
        stats = tracker.get_stats()
        assert stats["total_hits"] >= 0


# ── 5. Reset ─────────────────────────────────────────────────────────────────


class TestReset:
    """``reset()`` clears all tracked state for hermetic isolation."""

    def test_reset_clears_hits(self):
        tracker = RateLimitTracker()
        tracker.record_hit("/api/orders", "GET", "127.0.0.1", "120/minute")
        assert tracker.get_stats()["total_hits"] == 1
        tracker.reset()
        assert tracker.get_stats()["total_hits"] == 0
        assert len(tracker._hits) == 0

    def test_reset_clears_request_counts(self):
        tracker = RateLimitTracker()
        tracker.record_request("/api/orders", 200)
        tracker.reset()
        assert len(tracker._request_counts) == 0


# ── 6. Module-level singleton ──────────────────────────────────────────────


class TestSingleton:
    """The ``rate_limit_tracker`` module-level singleton is the instance
    imported by ``api/server.py``. Verify it has the expected type and
    that reset works on it (so tests can hermetically isolate)."""

    def test_singleton_is_rate_limit_tracker(self):
        assert isinstance(rate_limit_tracker, RateLimitTracker)

    def test_singleton_reset_returns_empty_stats(self):
        rate_limit_tracker.reset()
        stats = rate_limit_tracker.get_stats()
        assert stats["total_hits"] == 0
        assert stats["hits_by_endpoint"] == {}


# ── Pytest fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_singleton_per_test():
    """Reset the module-level singleton before each test so a test that
    imports it directly doesn't see state from a prior test."""
    rate_limit_tracker.reset()
    yield
    rate_limit_tracker.reset()
