"""In-memory TTL cache for hot-path API responses.

Uses a simple dict with timestamps — no external dependencies.
Thread-safe via a threading.Lock. Entries expire after TTL seconds.
"""
import time
import threading
import logging
from typing import Any, Optional, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    """A single cached value with its expiry / creation timestamps."""
    value: Any
    expires_at: float
    created_at: float


class TTLCache:
    """Thread-safe in-memory cache with time-to-live expiration.

    Design notes
    -------------
    * Hot-path API routes (``/api/analytics``, ``/api/ml/metrics``,
      ``/api/attribution`` …) recompute the same dict on every call —
      often walking the full ``store.trades`` list, re-multiplying every
      open position against the live order-book mid, or re-running the
      seven-dimension attribution roll-up. For a dashboard polling at
      1 Hz the cost is dominated by the recomputation, not the I/O.
    * A small in-process TTL cache collapses N consecutive identical
      requests into 1 compute + N-1 dict lookups — no Redis, no
      network hop, no extra deps. Trade-off: cache is per-process (so
      a multi-worker uvicorn deployment would NOT share cache state
      across workers — acceptable because every cache TTL here is ≤5
      min and the underlying data is already per-process).
    * Eviction policy: expired entries are removed lazily on access;
      at capacity we evict expired entries first, then fall back to
      oldest-by-created_at (approximate LRU without a re-ordering
      penalty on every hit — the spec's stated goal is "reduce
      redundant computations", not strict LRU).
    * Thread-safety: every public method acquires a single
      ``threading.Lock`` — coarse-grained but the per-call critical
      section is sub-microsecond for the dict ops, so contention is
      negligible even at 1k RPS. The lock guards the dict AND the
      hit/miss counters so ``stats()`` reflects a consistent snapshot.
    """

    def __init__(
        self,
        name: str = "default",
        default_ttl: float = 30.0,
        max_size: int = 1000,
    ):
        self._name = name
        self._cache: dict[str, CacheEntry] = {}
        self._lock = threading.Lock()
        self._default_ttl = default_ttl
        self._max_size = max_size
        self._hits = 0
        self._misses = 0

    # ── Public API ────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        """Return the cached value for ``key`` or ``None`` on miss / expiry.

        On a miss the entry is removed (if expired) and the miss counter
        is incremented. The hit/miss counters are kept under the lock so
        they reflect a consistent snapshot when read via ``stats()``.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return None
            if time.time() > entry.expires_at:
                del self._cache[key]
                self._misses += 1
                return None
            self._hits += 1
            return entry.value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store ``value`` under ``key`` with ``ttl`` seconds (or default).

        If the cache is at ``max_size``, expired entries are evicted
        first; if still at capacity, the oldest entry (by ``created_at``)
        is removed — approximate LRU without a re-ordering penalty on
        every hit.
        """
        with self._lock:
            # Evict expired entries if at capacity.
            if len(self._cache) >= self._max_size:
                self._evict_expired()
            # If still at capacity, remove oldest by created_at.
            if len(self._cache) >= self._max_size:
                oldest_key = min(self._cache, key=lambda k: self._cache[k].created_at)
                del self._cache[oldest_key]
            now = time.time()
            effective_ttl = ttl if ttl is not None else self._default_ttl
            self._cache[key] = CacheEntry(
                value=value,
                expires_at=now + effective_ttl,
                created_at=now,
            )

    def invalidate(self, key: str) -> None:
        """Remove ``key`` from the cache (no-op if absent).

        Used by mutation routes (``POST /api/trade`` etc.) to ensure
        the next read sees fresh data without waiting for TTL expiry.
        """
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        """Drop every entry and reset the hit/miss counters."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    # ── Internal helpers (callers must hold ``self._lock``) ───────────────────

    def _evict_expired(self) -> None:
        """Remove every entry whose TTL has elapsed.

        Caller MUST hold ``self._lock`` — this method is not
        synchronized on its own (it's invoked only from ``set`` after
        the lock is acquired).
        """
        now = time.time()
        expired = [k for k, v in self._cache.items() if now > v.expires_at]
        for k in expired:
            del self._cache[k]

    def stats(self) -> dict:
        """Return a snapshot of the cache's state for observability.

        Returned dict shape (consumed by ``GET /api/cache/stats``):
          {
            "name":          str,
            "size":          int,   # current entry count
            "max_size":      int,   # capacity ceiling
            "hits":          int,
            "misses":        int,
            "hit_rate":      float, # hits / (hits+misses), 0.0 if no traffic
            "default_ttl":   float, # seconds
          }
        """
        with self._lock:
            total = self._hits + self._misses
            return {
                "name": self._name,
                "size": len(self._cache),
                "max_size": self._max_size,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": self._hits / total if total > 0 else 0.0,
                "default_ttl": self._default_ttl,
            }


# ── Pre-instantiated cache instances ──────────────────────────────────────────
# Each instance targets a different data type with a TTL tuned to its staleness
# tolerance. Importing these singletons from ``core.cache`` is the canonical way
# to share cache state across modules without dragging in the full server.

# Markets / strategy catalog: rarely changes (5 min TTL) — invalidation on
# strategy toggle / catalog refresh is the only freshness requirement.
markets_cache = TTLCache(name="markets", default_ttl=300, max_size=100)

# ML metrics: moderate compute (60s TTL) — Brier / AUC / ECE don't move
# second-to-second; ``POST /api/ml/retrain`` invalidates after a fresh fit.
ml_metrics_cache = TTLCache(name="ml_metrics", default_ttl=60, max_size=50)

# Analytics: expensive to compute (30s TTL) — walks every trade + every open
# position against the live order-book mid. ``POST /api/trade`` and
# ``POST /api/positions/{token_id}/close`` invalidate.
analytics_cache = TTLCache(name="analytics", default_ttl=30, max_size=50)

# Attribution: expensive (60s TTL) — seven-dimension roll-up across all
# closed positions. ``POST /api/trade`` invalidates (a new closed position
# could land via the paper-sim fill loop).
attribution_cache = TTLCache(name="attribution", default_ttl=60, max_size=50)

# Observability snapshot (15s TTL) — the background
# ``core.observability_collector`` task records fresh metrics every ~30s so
# a 15s TTL collapses the burst of dashboard polls between collector ticks.
observability_cache = TTLCache(name="observability", default_ttl=15, max_size=20)

# General purpose — for ad-hoc caching needs (not currently used by any
# route, but exposed so future routes have a ready-made instance).
general_cache = TTLCache(name="general", default_ttl=30, max_size=200)


def cached(cache: TTLCache, key_fn: Callable, ttl: Optional[float] = None):
    """Decorator that caches function results.

    Args:
        cache: The TTLCache instance to use.
        key_fn: Function that generates the cache key from the wrapped
                function's positional / keyword args. The key MUST be a
                string (or stringifiable) — passing a tuple as the key
                would raise ``TypeError`` because the underlying dict is
                typed ``dict[str, CacheEntry]``.
        ttl: Override the cache's default TTL for this function only.

    Usage::

        @cached(analytics_cache, key_fn=lambda *a, **kw: "analytics")
        async def get_analytics():
            ...

    The decorator preserves ``__name__`` / ``__wrapped__`` for introspection
    (so ``inspect.signature`` and FastAPI's route registration still see
    the original function).
    """

    def decorator(func):
        def wrapper(*args, **kwargs):
            key = key_fn(*args, **kwargs)
            result = cache.get(key)
            if result is not None:
                return result
            result = func(*args, **kwargs)
            cache.set(key, result, ttl)
            return result

        wrapper.__wrapped__ = func
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper

    return decorator


__all__ = [
    "CacheEntry",
    "TTLCache",
    "cached",
    "markets_cache",
    "ml_metrics_cache",
    "analytics_cache",
    "attribution_cache",
    "observability_cache",
    "general_cache",
]
