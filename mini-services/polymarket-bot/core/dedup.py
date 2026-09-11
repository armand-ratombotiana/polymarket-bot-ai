"""Unified deduplication registry — prevents duplicate events across the system.

W24-6 — comprehensive duplicate event prevention. This module provides a
single in-process registry that every duplicate-sensitive call site can
consult before recording / firing / persisting an event:

1. Order duplicates    (via idempotency keys — ``token_id:side:size:price``)
2. Trade/fill duplicates (via ``trade_id`` or ``order_id`` for paper fills)
3. Market data duplicates (via snapshot hash — wired by data_validator W24-4)
4. Decision ledger duplicates (via ``correlation_id:stage``)
5. Alert duplicates   (via ``alert_id``)
6. Audit event duplicates (via ``event_id:timestamp``)

Each deduplication layer has its own TTL (time-to-live) window. The TTL is
implemented as a *time bucket* — two calls within the same TTL window for
the same key return False (duplicate); two calls in different windows
return True (unique). This avoids the need for an expiry sweeper thread
(the bucket changes deterministically as wall-clock advances, so stale
keys naturally fall out of the active bucket).

Memory bound: each entity_type registry is a ``deque(maxlen=10000)`` so
the registry is O(1) memory per type even under sustained load (a
runaway caller that hammers ``check_and_add`` with novel keys evicts the
oldest entries instead of growing unbounded).

Thread-safety: every public method acquires ``self._lock`` so the
registry is safe to call from sync and async code paths alike (the
underlying ``deque`` and ``dict`` are not thread-safe by themselves).

Contract
--------
``check_and_add(entity_type, key, ttl_seconds=300) -> bool``:
    Returns ``True`` if this is a NEW (non-duplicate) entity. The key is
    recorded so a subsequent call with the same key + within the same
    TTL window returns ``False``. Always best-effort — wrap the call
    site in ``try/except`` if the caller's hot path must NEVER raise
    (every wired call site in the bot already does this).

``get_stats(entity_type=None) -> dict``:
    Returns per-entity-type counters (total_seen / duplicates_blocked /
    unique_passed / duplicate_rate). With no arg, returns a dict keyed
    by entity_type.

``clear(entity_type=None) -> None``:
    Drops one entity_type's registry (or every registry when called
    with no arg). Used by tests for isolation and by the
    ``POST /api/dedup/clear`` admin endpoint.

This module is import-safe — no I/O, no global state outside the
singleton. The singleton is constructed at import time so a
``from core.dedup import dedup_registry`` is the canonical access
pattern (mirrors ``core.cache`` / ``core.rate_limit_tracker`` / etc.).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

logger = logging.getLogger(__name__)


@dataclass
class DedupStats:
    """Per-entity-type dedup counters (returned by ``get_stats``)."""

    entity_type: str
    total_seen: int
    duplicates_blocked: int
    unique_passed: int
    duplicate_rate: float


class DedupRegistry:
    """Centralized deduplication registry.

    A single in-process registry keyed by ``entity_type`` (e.g. ``order``,
    ``fill``, ``decision``, ``alert``). Each entity_type has its own
    bounded ``deque`` of recently-seen composite keys (``key:bucket``)
    so a repeat call within the same TTL window returns ``False``
    (duplicate) without growing the registry unbounded.

    The TTL is implemented as a *time bucket*: ``int(time.time() /
    ttl_seconds)``. Two calls within the same TTL window for the same
    key share a bucket → second call is a duplicate. Two calls in
    adjacent windows have different buckets → second call is unique.
    This avoids needing an expiry sweeper thread.

    Memory bound: each ``deque(maxlen=10000)`` evicts the oldest entry
    once full, so a runaway caller hammering novel keys cannot OOM the
    process — the registry is O(1) memory per entity_type.
    """

    def __init__(self) -> None:
        self._registries: dict[str, deque] = {}
        self._stats: dict[str, DedupStats] = {}
        self._lock = threading.Lock()

    def check_and_add(
        self,
        entity_type: str,
        key: str,
        ttl_seconds: float = 300,
    ) -> bool:
        """Check if a key is a duplicate. If not, add it.

        Args:
            entity_type: ``"order"``, ``"fill"``, ``"snapshot"``,
                ``"decision"``, ``"alert"``, ``"audit"`` (any string;
                unknown types are auto-created on first sight).
            key: Unique identifier for the entity (caller-supplied).
            ttl_seconds: How long to remember the key. Implemented as
                a time bucket — two calls within the same TTL window
                share a bucket and are dedup'd; two calls in adjacent
                windows have different buckets and pass. Default 300s.

        Returns:
            ``True`` if this is a NEW (non-duplicate) entity — the key
            has been recorded. ``False`` if this is a DUPLICATE — the
            key was already seen in the current TTL window.
        """
        with self._lock:
            if entity_type not in self._registries:
                self._registries[entity_type] = deque(maxlen=10000)
                self._stats[entity_type] = DedupStats(
                    entity_type=entity_type,
                    total_seen=0,
                    duplicates_blocked=0,
                    unique_passed=0,
                    duplicate_rate=0.0,
                )

            registry = self._registries[entity_type]
            stats = self._stats[entity_type]
            stats.total_seen += 1

            # Composite key with TTL bucket — same key in the same
            # bucket = duplicate; same key in a different bucket =
            # unique (the prior window has expired). Guard against
            # ttl_seconds <= 0 (caller mistake) so we never divide by
            # zero; fall back to a 1s bucket in that degenerate case.
            ttl = ttl_seconds if ttl_seconds and ttl_seconds > 0 else 1.0
            bucket = int(time.time() / ttl)
            composite = f"{key}:{bucket}"

            if composite in registry:
                stats.duplicates_blocked += 1
                stats.duplicate_rate = (
                    stats.duplicates_blocked / stats.total_seen
                )
                logger.debug(
                    "Dedup blocked: %s/%s",
                    entity_type,
                    key[:32],
                )
                return False

            registry.append(composite)
            stats.unique_passed += 1
            stats.duplicate_rate = (
                stats.duplicates_blocked / stats.total_seen
            )
            return True

    def get_stats(self, entity_type: str | None = None) -> dict:
        """Get deduplication statistics.

        Args:
            entity_type: When supplied, returns the stats dict for ONE
                entity_type (or a zeroed ``DedupStats`` shape if the
                type has never been seen). When ``None``, returns a
                dict keyed by entity_type.

        Returns:
            dict (or dict-of-dicts). Always JSON-serializable —
            ``DedupStats.__dict__`` is plain ``{str: int|float}``.
        """
        with self._lock:
            if entity_type is not None:
                if entity_type in self._stats:
                    return asdict(self._stats[entity_type])
                # Unknown type — return a zeroed stub so the API shape
                # is stable for callers that pre-list the entity types
                # they care about (mirrors the ``DedupStats`` default
                # constructor used in ``__init__`` for a fresh type).
                return asdict(
                    DedupStats(
                        entity_type=entity_type,
                        total_seen=0,
                        duplicates_blocked=0,
                        unique_passed=0,
                        duplicate_rate=0.0,
                    )
                )
            return {k: asdict(v) for k, v in self._stats.items()}

    def clear(self, entity_type: str | None = None) -> None:
        """Clear the dedup registry.

        Args:
            entity_type: When supplied, clears ONLY that type's
                registry + stats. When ``None``, clears every type.
        """
        with self._lock:
            if entity_type is not None:
                self._registries.pop(entity_type, None)
                self._stats.pop(entity_type, None)
            else:
                self._registries.clear()
                self._stats.clear()


# Singleton — mirrors the pattern in ``core.cache`` /
# ``core.rate_limit_tracker`` / ``core.latency_tracker`` so the
# canonical access pattern is ``from core.dedup import dedup_registry``.
dedup_registry = DedupRegistry()
