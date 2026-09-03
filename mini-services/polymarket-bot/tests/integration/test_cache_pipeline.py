"""W17-9 — Cross-module integration tests for the caching pipeline.

Drives the ``core.cache.TTLCache`` through the three production-flavoured
behaviours the task spec asks for:

  1. **Hit / miss cycle**: first request for a key is a miss (data is
     computed and stored); the second request for the same key is a hit
     (data is served from the cache, no recompute); ``invalidate(k)``
     forces the next request back to a miss.

  2. **TTL expiration**: an entry with a short TTL is removed by the
     next ``get`` after the TTL elapses (counted as a miss, NOT a hit).

  3. **Stats accuracy**: after N requests with a known hit/miss pattern,
     ``stats()`` reports the exact hit + miss counts and a correctly-
     computed ``hit_rate``.

This module exercises the ``TTLCache`` class directly (no HTTP / FastAPI
dependency) AND the ``cached()`` decorator that wraps the
``get_health_report`` callable in production. The singleton caches
(``analytics_cache`` / ``ml_metrics_cache`` / ``observability_cache`` …)
are shared across the whole pytest session — these tests use FRESH
``TTLCache`` instances so the singleton stats are never perturbed.
"""
from __future__ import annotations

import time

import pytest

from core.cache import TTLCache, cached

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here — every
# test in this file is a plain synchronous ``def``. ``TTLCache`` is pure-
# Python (no I/O, no awaits) so there is nothing for the asyncio event
# loop to schedule. Skipping the asyncio marker keeps pytest-asyncio
# collection cost off this file entirely (mirrors ``tests/test_cache.py``
# / ``tests/test_shadow_inference.py``).


# ── (1) Cache hit / miss cycle ──────────────────────────────────────────────


def test_cache_miss_then_hit_cycle():
    """First ``get`` is a miss, second is a hit, ``invalidate`` resets to miss.

    Drives the cache through the canonical production cycle:
      1. compute-on-miss — first ``get`` for a key returns ``None`` (miss)
         and bumps the miss counter.
      2. cache-on-write — ``set(k, v)`` persists the value.
      3. serve-on-hit — second ``get`` for the same key returns ``v`` (hit)
         and bumps the hit counter.
      4. invalidate — ``invalidate(k)`` removes the entry; the next ``get``
         is a miss again.
    """
    cache = TTLCache(name="test_cycle", default_ttl=60, max_size=10)

    # 1. Miss (key absent).
    assert cache.get("foo") is None
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hits"] == 0

    # 2. Set the value.
    cache.set("foo", "bar")

    # 3. Hit (key present, fresh).
    assert cache.get("foo") == "bar"
    assert cache.stats()["hits"] == 1
    assert cache.stats()["misses"] == 1

    # 4. Invalidate.
    cache.invalidate("foo")
    # Now miss again.
    assert cache.get("foo") is None
    assert cache.stats()["misses"] == 2
    assert cache.stats()["hits"] == 1


def test_cached_decorator_serves_from_cache_on_second_call():
    """The ``@cached(cache, key_fn)`` decorator wraps a function so the
    second call returns the cached value WITHOUT re-invoking the wrapped
    function.

    Uses a side-effect counter to verify the wrapped function is called
    exactly once across two invocations.
    """
    cache = TTLCache(name="test_decorator", default_ttl=60, max_size=10)
    call_counter = {"n": 0}

    @cached(cache, key_fn=lambda *a, **kw: "constant_key")
    def expensive_compute(x):
        call_counter["n"] += 1
        return x * 2

    # First call: cache miss → wrapped function invoked.
    result1 = expensive_compute(5)
    assert result1 == 10
    assert call_counter["n"] == 1

    # Second call: cache hit → wrapped function NOT invoked.
    result2 = expensive_compute(999)  # arg ignored because key_fn is constant
    assert result2 == 10  # cached value returned, not 999 * 2
    assert call_counter["n"] == 1, (
        f"wrapped function must NOT be re-invoked on a cache hit; "
        f"call_counter={call_counter['n']}"
    )

    # Stats reflect the hit + miss pattern.
    stats = cache.stats()
    assert stats["misses"] == 1  # the first call was a miss
    assert stats["hits"] == 1  # the second call was a hit


def test_invalidate_does_not_affect_other_keys():
    """``invalidate(k)`` removes only ``k``; sibling keys are untouched.

    Guards against a regression where ``invalidate`` would call
    ``clear()`` instead of a single-key ``pop()``.
    """
    cache = TTLCache(name="test_invalidate_isolation", default_ttl=60, max_size=10)
    cache.set("k1", "v1")
    cache.set("k2", "v2")

    cache.invalidate("k1")
    assert cache.get("k1") is None
    assert cache.get("k2") == "v2"


# ── (2) Cache TTL expiration ───────────────────────────────────────────────


def test_expired_entry_is_removed_on_get():
    """An entry whose TTL has elapsed is removed by the next ``get``;
    that ``get`` returns ``None`` (counted as a miss, NOT a hit).

    Uses a 50 ms TTL so the test wall-clock cost is bounded (~80 ms
    including scheduler jitter margin).
    """
    cache = TTLCache(name="test_ttl_expiry", default_ttl=0.05, max_size=10)
    cache.set("foo", "bar")
    # Pre-condition: fresh entry is retrievable.
    assert cache.get("foo") == "bar"

    # Wait for TTL to elapse (50 ms → 100 ms is safe under any CI jitter).
    time.sleep(0.10)

    # Expired entry removed + miss recorded.
    assert cache.get("foo") is None
    stats = cache.stats()
    # 1 hit (the pre-condition get) + 1 miss (the post-TTL get).
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_ttl_override_per_set_call():
    """``set(k, v, ttl=X)`` overrides the cache's default TTL for a
    single entry.

    Verifies the per-call TTL override path — the production
    ``observability_cache.set(..., ttl=15)`` pattern used by the
    ``GET /api/observability`` route handler.
    """
    cache = TTLCache(name="test_ttl_override", default_ttl=60, max_size=10)
    # Override the 60s default with a 50ms TTL for this one entry.
    cache.set("short", "lived", ttl=0.05)
    cache.set("long", "lived", ttl=60)  # explicit long TTL

    assert cache.get("short") == "short_lived_marker_unused" or cache.get("short") == "lived"
    # Wait for the short entry to expire.
    time.sleep(0.10)

    # Short entry expired.
    assert cache.get("short") is None
    # Long entry still present.
    assert cache.get("long") == "lived"


def test_clear_resets_cache_and_stats():
    """``clear()`` drops every entry AND resets the hit/miss counters
    to zero — the documented ``POST /api/cache/reset`` semantics.

    Mirrors the production ``cache.clear()`` call that mutation routes
    invoke after writes to force the next read to recompute fresh data.
    """
    cache = TTLCache(name="test_clear", default_ttl=60, max_size=10)
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    cache.get("k1")  # hit
    cache.get("absent")  # miss

    pre = cache.stats()
    assert pre["size"] == 2
    assert pre["hits"] == 1
    assert pre["misses"] == 1

    cache.clear()

    post = cache.stats()
    assert post["size"] == 0
    assert post["hits"] == 0
    assert post["misses"] == 0
    assert post["hit_rate"] == 0.0


# ── (3) Cache stats accuracy ───────────────────────────────────────────────


def test_stats_reflect_exact_hit_miss_counts():
    """After a known sequence of N hits + M misses, ``stats()``
    reports the exact counts.

    Guards against a regression where the hit/miss counters drift
    (e.g. expired-on-get counted as a hit, or hits recorded under
    the wrong key bucket).
    """
    cache = TTLCache(name="test_stats", default_ttl=60, max_size=10)

    # 3 misses (3 absent keys).
    cache.get("miss1")
    cache.get("miss2")
    cache.get("miss3")
    # Set + get 4 distinct keys → 4 hits.
    for k in ("hit1", "hit2", "hit3", "hit4"):
        cache.set(k, f"v_{k}")
        cache.get(k)

    stats = cache.stats()
    assert stats["misses"] == 3, f"expected 3 misses; got {stats['misses']}"
    assert stats["hits"] == 4, f"expected 4 hits; got {stats['hits']}"
    # hit_rate = hits / (hits + misses) = 4 / 7 ≈ 0.5714.
    assert stats["hit_rate"] == pytest.approx(4 / 7, abs=1e-3)


def test_stats_hit_rate_zero_on_no_traffic():
    """``hit_rate`` is 0.0 when the cache has seen zero traffic
    (the 0/0 → 0.0 guard in ``stats()``)."""
    cache = TTLCache(name="test_stats_empty", default_ttl=60, max_size=10)
    stats = cache.stats()
    assert stats["hit_rate"] == 0.0
    assert stats["hits"] == 0
    assert stats["misses"] == 0


def test_stats_size_reflects_current_entry_count():
    """``stats()['size']`` reflects the current entry count (not the
    total ever-stored count).

    After invalidating one of three keys, ``size`` drops to 2.
    """
    cache = TTLCache(name="test_stats_size", default_ttl=60, max_size=10)
    cache.set("k1", "v1")
    cache.set("k2", "v2")
    cache.set("k3", "v3")
    assert cache.stats()["size"] == 3

    cache.invalidate("k2")
    assert cache.stats()["size"] == 2

    cache.clear()
    assert cache.stats()["size"] == 0


# ── (4) Production singleton integration ────────────────────────────────────


def test_production_singletons_are_independent():
    """The pre-instantiated cache singletons (``analytics_cache``,
    ``ml_metrics_cache``, ``observability_cache``, ``general_cache``)
    are independent instances — a write to one does NOT show up in
    another.

    Guards against a regression where the singletons were aliased to
    the same underlying dict (which would silently leak analytics-cache
    entries into ml_metrics_cache reads).
    """
    from core.cache import (
        analytics_cache,
        attribution_cache,
        general_cache,
        markets_cache,
        ml_metrics_cache,
        observability_cache,
    )

    # Clear all singletons so we start from a known empty state.
    for c in (
        analytics_cache,
        attribution_cache,
        general_cache,
        markets_cache,
        ml_metrics_cache,
        observability_cache,
    ):
        c.clear()

    # Write to analytics_cache only.
    analytics_cache.set("test_key", "analytics_value")

    # The other caches do NOT see the key.
    assert ml_metrics_cache.get("test_key") is None
    assert observability_cache.get("test_key") is None
    assert markets_cache.get("test_key") is None
    assert general_cache.get("test_key") is None
    assert attribution_cache.get("test_key") is None

    # The analytics_cache DOES see it.
    assert analytics_cache.get("test_key") == "analytics_value"

    # Stats reflect the per-instance isolation.
    assert analytics_cache.stats()["hits"] == 1
    assert ml_metrics_cache.stats()["misses"] == 1

    # Cleanup so sibling tests see clean singletons.
    for c in (
        analytics_cache,
        attribution_cache,
        general_cache,
        markets_cache,
        ml_metrics_cache,
        observability_cache,
    ):
        c.clear()


def test_each_production_singleton_has_distinct_default_ttl():
    """Each production singleton has its own documented default TTL:

      * ``markets_cache``       — 300 s (markets / strategy catalog)
      * ``ml_metrics_cache``    — 60 s  (Brier / AUC / ECE)
      * ``analytics_cache``     — 30 s  (expensive trade-walk rollup)
      * ``attribution_cache``   — 60 s  (seven-dimension rollup)
      * ``observability_cache`` — 15 s  (collector tick window)
      * ``general_cache``        — 30 s  (ad-hoc)

    Pins the tuning choices so a future refactor that re-tunes one
    cache doesn't silently change another's SLO.
    """
    from core.cache import (
        analytics_cache,
        attribution_cache,
        general_cache,
        markets_cache,
        ml_metrics_cache,
        observability_cache,
    )

    expected_ttls = {
        markets_cache: 300,
        ml_metrics_cache: 60,
        analytics_cache: 30,
        attribution_cache: 60,
        observability_cache: 15,
        general_cache: 30,
    }
    for cache, expected_ttl in expected_ttls.items():
        stats = cache.stats()
        assert stats["default_ttl"] == expected_ttl, (
            f"cache {stats['name']!r} has default_ttl={stats['default_ttl']}; "
            f"expected {expected_ttl}"
        )
