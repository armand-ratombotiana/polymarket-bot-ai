"""
tests/test_cache.py — W11-2 backend caching layer unit tests.

Covers the eight behavioural guarantees the W11-2 task spec asks for:

  1. **Basic get / set round-trip** — ``set(k, v)`` followed by
     ``get(k)`` returns ``v`` (and records a hit).
  2. **Get on miss returns None** — ``get('absent')`` returns ``None``
     (and records a miss).
  3. **TTL expiration** — an entry whose TTL has elapsed is removed on
     the next ``get`` and that ``get`` returns ``None`` (treated as a
     miss, NOT a hit).
  4. **Invalidate removes a single key** — ``invalidate(k)`` removes
     ``k`` from the cache without touching other keys; subsequent
     ``get(k)`` returns ``None``.
  5. **Clear resets the cache AND the counters** — after ``clear()``,
     ``stats()`` reports ``size=0``, ``hits=0``, ``misses=0``,
     ``hit_rate=0.0``.
  6. **Stats reporting** — ``stats()`` returns the documented dict
     shape (``name``, ``size``, ``max_size``, ``hits``, ``misses``,
     ``hit_rate``, ``default_ttl``); ``hit_rate`` is
     ``hits / (hits + misses)`` with the 0/0 → 0.0 guard.
  7. **LRU eviction at max_size** — once the cache is at capacity,
     inserting a new key removes the OLDEST entry (by ``created_at``)
     rather than raising / silently dropping the new insert.
  8. **Thread safety (concurrent get/set)** — N threads each performing
     M ``set`` ops with distinct keys do not corrupt the cache's
     internal dict (no ``RuntimeError: dictionary changed size during
     iteration`` during the lazy-expiry sweep in ``_evict_expired``).
  9. **The ``cached()`` decorator** — wrapping a function with
     ``@cached(cache, key_fn=...)`` returns the cached value on the
     second call without re-invoking the wrapped function (verified by
     a side-effect counter); the wrapper preserves ``__name__`` /
     ``__wrapped__`` for introspection.

The tests do NOT spin up the FastAPI app or hit any HTTP route — they
exercise the ``TTLCache`` class directly so the contract under test is
isolated from the rest of the pipeline. The ``TTLCache`` is a pure-Python
in-memory data structure (no I/O, no asyncio) so every test is a sync
``def`` — no ``pytest.mark.asyncio`` required.
"""
from __future__ import annotations

import threading
import time

import pytest

from core.cache import TTLCache, cached


# ── 1. Basic get / set round-trip ────────────────────────────────────────────


def test_set_then_get_returns_value():
    """``cache.set('k', v)`` followed by ``cache.get('k')`` returns ``v``."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.set("foo", "bar")
    assert cache.get("foo") == "bar"


def test_set_records_a_hit_on_subsequent_get():
    """A successful ``get`` increments the cache's hit counter."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.set("foo", "bar")
    cache.get("foo")
    assert cache.stats()["hits"] == 1


# ── 2. Get on miss ───────────────────────────────────────────────────────────


def test_get_on_absent_key_returns_none():
    """``get('absent')`` returns ``None`` (no KeyError, no default)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    assert cache.get("nope") is None


def test_get_on_absent_key_records_a_miss():
    """A miss increments the cache's miss counter (and NOT the hit counter)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.get("nope")
    stats = cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0


# ── 3. TTL expiration ────────────────────────────────────────────────────────


def test_expired_entry_returns_none_on_get():
    """An entry whose TTL has elapsed is removed on the next ``get`` and
    that ``get`` returns ``None`` (counted as a miss, NOT a hit)."""
    cache = TTLCache(name="t", default_ttl=0.05, max_size=10)
    cache.set("foo", "bar")
    # Sleep long enough for the entry to expire (TTL=0.05s → 60ms is safe
    # under any CI scheduler jitter).
    time.sleep(0.10)
    assert cache.get("foo") is None
    stats = cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 0
    # The expired entry was removed by the get path.
    assert stats["size"] == 0


def test_set_with_explicit_ttl_overrides_default():
    """The ``ttl`` arg to ``set`` overrides the cache's ``default_ttl``."""
    cache = TTLCache(name="t", default_ttl=999, max_size=10)
    cache.set("foo", "bar", ttl=0.05)
    time.sleep(0.10)
    assert cache.get("foo") is None


def test_non_expired_entry_is_returned():
    """A fresh entry whose TTL has NOT elapsed is returned verbatim
    (guards against a regression where ``time.time() > entry.expires_at``
    is inverted)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.set("foo", {"a": 1, "b": [2, 3]})
    out = cache.get("foo")
    assert out == {"a": 1, "b": [2, 3]}


# ── 4. Invalidate ────────────────────────────────────────────────────────────


def test_invalidate_removes_single_key():
    """``invalidate(k)`` removes ``k`` only; other keys are untouched."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_invalidate_absent_key_is_a_noop():
    """``invalidate`` on a missing key does not raise."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    # Should not raise.
    cache.invalidate("absent")
    assert cache.stats()["size"] == 0


# ── 5. Clear ─────────────────────────────────────────────────────────────────


def test_clear_resets_size_and_counters():
    """After ``clear()``, ``stats()`` reports ``size=0``, ``hits=0``,
    ``misses=0``, ``hit_rate=0.0``."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.get("a")  # hit
    cache.get("missing")  # miss
    assert cache.stats()["size"] == 2
    cache.clear()
    stats = cache.stats()
    assert stats["size"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert stats["hit_rate"] == 0.0


# ── 6. Stats reporting ───────────────────────────────────────────────────────


def test_stats_returns_documented_shape():
    """``stats()`` returns the documented dict shape with the documented
    keys (``name``, ``size``, ``max_size``, ``hits``, ``misses``,
    ``hit_rate``, ``default_ttl``)."""
    cache = TTLCache(name="my_cache", default_ttl=42.0, max_size=7)
    stats = cache.stats()
    expected_keys = {"name", "size", "max_size", "hits", "misses", "hit_rate", "default_ttl"}
    assert set(stats.keys()) == expected_keys
    assert stats["name"] == "my_cache"
    assert stats["max_size"] == 7
    assert stats["default_ttl"] == 42.0
    assert stats["size"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 0
    assert stats["hit_rate"] == 0.0


def test_hit_rate_is_hits_over_total():
    """``hit_rate`` is ``hits / (hits + misses)`` with the 0/0 → 0.0 guard."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    # 3 hits + 1 miss → hit_rate = 0.75.
    cache.set("a", 1)
    cache.get("a")  # hit
    cache.get("a")  # hit
    cache.get("a")  # hit
    cache.get("nope")  # miss
    stats = cache.stats()
    assert stats["hits"] == 3
    assert stats["misses"] == 1
    assert stats["hit_rate"] == 0.75


def test_hit_rate_zero_when_no_traffic():
    """``hit_rate`` is 0.0 (not NaN, not a ZeroDivisionError) when the
    cache has had zero traffic (the 0/0 guard)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    assert cache.stats()["hit_rate"] == 0.0


# ── 7. LRU eviction at max_size ───────────────────────────────────────────────


def test_lru_eviction_removes_oldest_at_capacity():
    """When the cache is at ``max_size``, the next ``set`` removes the
    OLDEST entry (by ``created_at``) so the new entry fits."""
    cache = TTLCache(name="t", default_ttl=30, max_size=3)
    cache.set("a", 1)
    time.sleep(0.005)  # ensure distinct created_at timestamps
    cache.set("b", 2)
    time.sleep(0.005)
    cache.set("c", 3)
    assert cache.stats()["size"] == 3
    # Cache is now at capacity. Inserting "d" should evict "a" (oldest).
    time.sleep(0.005)
    cache.set("d", 4)
    assert cache.stats()["size"] == 3  # still at capacity, not 4
    assert cache.get("a") is None  # evicted
    assert cache.get("b") == 2  # retained
    assert cache.get("c") == 3  # retained
    assert cache.get("d") == 4  # the new entry


def test_set_overwrite_does_not_grow_size():
    """Re-setting an EXISTING key updates the value without exceeding
    ``max_size`` (the new entry replaces the old rather than creating a
    second slot)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=2)
    cache.set("a", 1)
    cache.set("a", 2)  # overwrite
    assert cache.stats()["size"] == 1
    assert cache.get("a") == 2


def test_eviction_first_drops_expired_entries():
    """At capacity, ``set`` first evicts expired entries before falling
    back to LRU. With one expired entry + capacity reached, the new
    insert should NOT evict a live entry."""
    cache = TTLCache(name="t", default_ttl=999, max_size=2)
    cache.set("a", 1, ttl=0.05)  # will expire (short TTL)
    cache.set("b", 2)  # fresh (default_ttl=999s)
    time.sleep(0.10)  # let "a" expire, "b" is still fresh
    # Cache is at capacity (size=2), but "a" is expired. Inserting "c"
    # should evict the expired "a" (free) rather than the live "b".
    cache.set("c", 3)
    assert cache.stats()["size"] == 2
    assert cache.get("a") is None  # was expired, then evicted
    assert cache.get("b") == 2  # survived (was live, not LRU-evicted)
    assert cache.get("c") == 3


# ── 8. Thread safety (concurrent get/set) ────────────────────────────────────


def test_concurrent_set_with_distinct_keys_does_not_lose_entries():
    """N threads each performing M ``set`` ops with distinct keys do not
    corrupt the cache's internal dict — the final ``size`` equals N*M
    (bounded by ``max_size``) and every key is retrievable.

    The failure mode this guards against: ``_evict_expired`` (invoked
    from ``set`` under the lock) iterates ``self._cache.items()`` while
    another thread holds the GIL but has not yet entered the lock.
    Without the lock, the iteration would raise ``RuntimeError:
    dictionary changed size during iteration`` on a CPython <3.12 build.
    """
    n_threads = 8
    ops_per_thread = 50
    cache = TTLCache(
        name="t",
        default_ttl=30,
        # Set max_size high enough that no eviction kicks in (so we can
        # assert every key made it in).
        max_size=n_threads * ops_per_thread + 100,
    )

    def worker(thread_id: int) -> None:
        for i in range(ops_per_thread):
            key = f"t{thread_id}-k{i}"
            cache.set(key, thread_id * 1000 + i)

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every (thread, op) pair produced a distinct key, and none were
    # evicted (max_size was generous), so size == n_threads * ops_per_thread.
    assert cache.stats()["size"] == n_threads * ops_per_thread
    # Spot-check: the last key each thread wrote is retrievable.
    for t in range(n_threads):
        last_key = f"t{t}-k{ops_per_thread - 1}"
        assert cache.get(last_key) == t * 1000 + (ops_per_thread - 1)


def test_concurrent_get_and_set_does_not_raise():
    """One thread ``set``ting while another ``get``s does not raise
    (the lock guards both paths)."""
    cache = TTLCache(name="t", default_ttl=0.10, max_size=100)
    cache.set("seed", 1)
    errors: list[Exception] = []

    def setter() -> None:
        try:
            for i in range(100):
                cache.set(f"k{i}", i, ttl=0.05)  # short TTL → expiry races
        except Exception as e:  # noqa: BLE001 — capture any error from the worker
            errors.append(e)

    def getter() -> None:
        try:
            for i in range(100):
                cache.get(f"k{i}")  # may hit or miss, must not raise
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    t1 = threading.Thread(target=setter)
    t2 = threading.Thread(target=getter)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == [], f"concurrent get/set raised: {errors!r}"


# ── 9. The cached() decorator ─────────────────────────────────────────────────


def test_cached_decorator_returns_cached_value_without_reinvoking():
    """The second call to a ``@cached``-wrapped function returns the
    cached value WITHOUT re-invoking the wrapped function (verified by
    a side-effect counter)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    call_count = {"n": 0}

    @cached(cache, key_fn=lambda *a, **kw: "const")
    def expensive(x):
        call_count["n"] += 1
        return x * 2

    assert expensive(5) == 10
    assert expensive(5) == 10  # second call should hit the cache
    assert expensive(5) == 10  # third call too
    # The wrapped function should have been invoked exactly once.
    assert call_count["n"] == 1


def test_cached_decorator_distinguishes_keys():
    """Different ``key_fn`` outputs route to different cache slots —
    calling ``expensive(5)`` and ``expensive(7)`` invokes the function
    twice (once per distinct key)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)
    call_count = {"n": 0}

    @cached(cache, key_fn=lambda x, *a, **kw: f"x={x}")
    def expensive(x):
        call_count["n"] += 1
        return x * 2

    assert expensive(5) == 10
    assert expensive(7) == 14  # different key → cache miss → re-invoke
    assert expensive(5) == 10  # back to the first key → cache hit
    assert expensive(7) == 14  # second key → cache hit
    assert call_count["n"] == 2  # one invocation per distinct key


def test_cached_decorator_preserves_name_and_wrapped():
    """The wrapper preserves ``__name__`` / ``__wrapped__`` for
    introspection (FastAPI uses ``__name__`` for its route registration
    and ``inspect.signature`` follows ``__wrapped__``)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)

    @cached(cache, key_fn=lambda *a, **kw: "k")
    def my_func(x):
        """My docstring."""
        return x

    assert my_func.__name__ == "my_func"
    assert my_func.__wrapped__ is not None
    # The decorator should preserve the docstring for documentation
    # tools (FastAPI renders route docstrings into the OpenAPI schema).
    assert my_func.__doc__ == "My docstring."


def test_cached_decorator_respects_ttl_override():
    """The ``ttl`` arg to ``cached()`` overrides the cache's
    ``default_ttl`` for that function only."""
    cache = TTLCache(name="t", default_ttl=999, max_size=10)

    @cached(cache, key_fn=lambda *a, **kw: "k", ttl=0.05)
    def fast_expiring(x):
        return x

    fast_expiring(1)
    time.sleep(0.10)
    # The cached entry should have expired.
    assert cache.get("k") is None


def test_cached_decorator_increments_hit_and_miss_counters():
    """The decorator uses the cache's get/set, so each wrapped call
    increments the cache's hit/miss counters (verifying the cache is
    actually consulted, not bypassed)."""
    cache = TTLCache(name="t", default_ttl=30, max_size=10)

    @cached(cache, key_fn=lambda *a, **kw: "k")
    def f(x):
        return x

    f(1)  # miss → set
    f(1)  # hit
    f(1)  # hit
    stats = cache.stats()
    assert stats["misses"] == 1
    assert stats["hits"] == 2
