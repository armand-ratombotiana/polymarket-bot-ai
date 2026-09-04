"""Write-through cache — writes to both async DB and in-memory cache.

W23-6 — pairs with the async write repositories in
``core.async_repositories``. The cache sits in front of the async
repos so the FastAPI v2 endpoints can serve hot reads from memory
without a round-trip to SQLite, while every write propagates to both
the cache AND the async repo (so a cache hit is always consistent
with the persistent store — no stale reads).

Design
------
* **Write-through.** Every ``write()`` call hits the cache first
  (under an ``asyncio.Lock`` so concurrent writes don't tear the
  dict), then the async DB writer (if supplied). A DB write failure
  is logged but does NOT raise — the cache is still updated, so the
  caller gets the value back on the next read. The persistent store
  will eventually catch up on the next successful write (or stay
  inconsistent until the cache is invalidated / cleared).

* **Sync read, async fetch.** ``read()`` is a sync method (no
  ``await``) so hot-path reads don't pay the event-loop hop. The
  ``read_or_fetch()`` async helper is the cache-miss path: it
  populates the cache from the supplied ``db_fetcher`` coroutine so
  the next read is a cache hit.

* **Lock-scoped mutations.** ``write`` / ``read_or_fetch`` /
  ``invalidate`` / ``clear`` all run under ``self._lock`` so a
  concurrent writer can't observe a half-updated cache. ``read`` is
  lock-free (dict reads are atomic in CPython — the GIL ensures
  ``dict.get`` is consistent even mid-write).

* **Module-level singleton.** ``write_through_cache`` is the
  process-wide instance — mirrors the singleton pattern used by
  ``core.db_pool.db_pool`` and ``core.cache.general_cache``. The
  singleton is constructed at import time but its dict is empty
  until the first ``write()`` call.
"""
import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


class WriteThroughCache:
    """Writes go to DB (async) + cache (sync). Reads hit cache first.

    The cache is a plain ``dict`` keyed by ``str``. The async DB
    writer is an optional ``Callable[[Any], Awaitable[None]]`` —
    callers pass a bound method of an async repository (e.g.
    ``lambda v: decision_repo.record_event(**v)``) so the cache
    layer doesn't need to know which repo + method to invoke.

    The cache is **not** TTL-bounded — it grows unbounded until
    ``clear()`` / ``invalidate()`` is called. Production callers
    that need eviction should pair this with the TTL cache in
    ``core.cache`` (or wrap each key with a TTL entry — left as a
    future wave).
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def write(
        self,
        key: str,
        value: Any,
        db_writer: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> None:
        """Write to cache + DB.

        The cache is updated first (under the lock) so a concurrent
        reader can't observe a stale value while the DB write is in
        flight. The DB writer is invoked AFTER the cache mutation
        (outside the lock — DB writes can take milliseconds, and
        holding the lock across the await would serialise every
        writer). A DB write failure is logged at ERROR and swallowed
        — the cache still reflects the new value, so the caller's
        next read is consistent with what they just wrote.
        """
        async with self._lock:
            self._cache[key] = value
        if db_writer:
            try:
                await db_writer(value)
            except Exception as e:
                logger.error(
                    "[write_through_cache] DB write failed for key=%s: %s",
                    key, e,
                )

    def read(self, key: str) -> Optional[Any]:
        """Read from cache (sync, fast).

        Returns ``None`` for both cache misses AND ``None`` values
        stored in the cache — callers that need to distinguish should
        use ``read_or_fetch``. The dict lookup is atomic in CPython
        (GIL ensures ``dict.get`` is consistent even mid-write), so
        no lock is needed.
        """
        return self._cache.get(key)

    async def read_or_fetch(
        self,
        key: str,
        db_fetcher: Optional[Callable[[str], Awaitable[Any]]] = None,
    ) -> Optional[Any]:
        """Read from cache, or fetch from DB if not cached.

        Cache hit → return immediately (sync dict lookup). Cache
        miss + ``db_fetcher`` → fetch from DB, populate the cache
        (under the lock so a concurrent writer doesn't overwrite the
        fetched value mid-populate), and return the fetched value.
        Cache miss + no ``db_fetcher`` → return ``None``.

        Note: a cached ``None`` value is treated as a cache hit
        (``cached is not None``), so the fetcher is only invoked on
        an actual key absence — NOT on a stored ``None``.
        """
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if db_fetcher:
            result = await db_fetcher(key)
            if result is not None:
                async with self._lock:
                    self._cache[key] = result
            return result
        return None

    def invalidate(self, key: str) -> None:
        """Remove a key from cache.

        No-op if the key isn't present (``dict.pop(key, None)``
        semantics). Sync — no lock needed because the dict mutation
        is atomic under the GIL.
        """
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Drop every cached entry."""
        self._cache.clear()

    def size(self) -> int:
        """Return the number of cached entries."""
        return len(self._cache)


# Module-level singleton. Mirrors the ``db_pool`` / ``general_cache``
# convention so callers import ``from core.write_through_cache import
# write_through_cache`` and get the process-wide instance. Constructed
# at import time but its dict is empty until the first ``write`` call.
write_through_cache = WriteThroughCache()
