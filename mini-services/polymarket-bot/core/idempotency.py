"""
core/idempotency.py — Pre-submission idempotency / duplicate-order detection.

W24-3 — God Mode pre-submission risk gate (§pre-submission-gate).

The pre-submission risk gate (``core.pre_submission_gate.PreSubmissionGate``)
must reject a duplicate strategy decision before it reaches the exchange. A
"duplicate" is any order whose ``(strategy, token_id, side, price, size)``
5-tuple has already been seen recently — the same inputs always produce the
same deterministic key, so a strategy that fires the same signal twice in
quick succession (e.g. a re-entry after a transient paper-sim error, a
retry loop, or a bug in the strategy's dedup logic) is caught before the
second order reaches the order book.

Design
------
- ``generate_key(strategy, token_id, side, size, price)`` — deterministic
  SHA-256 over the 5-tuple. Reuses the canonical implementation from
  ``core.order_state_machine.generate_idempotency_key`` so the gate's
  notion of "duplicate" matches the OSM audit trail's notion of "duplicate".
- ``check_and_record(key, order_id, order_request)`` — atomic check-then-
  record. Returns ``(is_dup, existing_order_id)``. If the key is new, the
  (key, order_id, timestamp) triple is recorded; if the key was already
  recorded AND the recorded entry has not expired (TTL window), the call
  returns ``(True, existing_order_id)`` so the caller can reject the
  duplicate. Recording is in-memory (process-local) — a restart clears
  the cache, which is the correct behaviour for "duplicate within a session"
  (a duplicate after a multi-hour restart is not the same risk).
- ``reset()`` — clears the cache. Used by tests to isolate every test
  case from prior-test pollution.

TTL
---
The cache entries expire after ``_ttl_seconds`` (default 300s — 5 minutes).
A duplicate signal fired 6 minutes after the first is treated as a fresh
decision (the operator / strategy may legitimately re-enter after the
market moved). The TTL is conservative: long enough to catch retry
loops / paper-sim error retries (which fire within seconds), short
enough to never block a legitimate later re-entry.

Thread-safety
-------------
The cache is guarded by a ``threading.Lock`` so concurrent strategy
loops (multiple strategies running in parallel ``_run`` tasks) can call
``check_and_record`` without a race between the check and the record.
The lock is coarse-grained (single critical section) — the cache is
small (bounded by ``_max_entries`` to prevent unbounded growth) so
contention is negligible.

Wiring
------
``core.pre_submission_gate.PreSubmissionGate.check`` imports
``idempotency_manager`` from this module and calls
``check_and_record`` as check #13 of the 14-check pre-submission gate.
The gate rejects with ``rejection_category="idempotency"`` when a
duplicate is detected.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# Re-use the canonical implementation so the pre-submission gate's notion
# of "duplicate" matches the OSM audit trail's notion of "duplicate".
from core.order_state_machine import generate_idempotency_key

# Default TTL — duplicate-detection window. A signal fired more than this
# many seconds after the first is treated as a fresh decision (not a
# duplicate). 300s = 5 minutes is long enough to catch retry loops and
# short enough to never block a legitimate later re-entry.
_DEFAULT_TTL_SECONDS: float = 300.0

# Hard ceiling on cache size so a runaway strategy firing distinct signals
# at high frequency cannot grow the cache unbounded. LRU eviction: when
# the cache exceeds this size, the oldest entries are pruned first.
_DEFAULT_MAX_ENTRIES: int = 10_000


class IdempotencyManager:
    """In-memory, TTL-bounded duplicate-order cache.

    A single process-wide singleton (``idempotency_manager``) is constructed
    at module-import time so every strategy / risk-gate call site shares
    one cache. Tests reset the cache via ``reset()`` (or construct a fresh
    ``IdempotencyManager()`` for hermetic isolation).

    The cache is intentionally NOT persisted to disk — a duplicate after a
    process restart is a separate decision (the operator may have
    intentionally restarted to re-arm), and persisting would require
    schema migration / SQLite co-tenancy that the W24-3 scope doesn't
    authorise.
    """

    def __init__(
        self,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._ttl_seconds: float = float(ttl_seconds)
        self._max_entries: int = int(max_entries)
        # key -> (order_id, recorded_at_epoch_seconds)
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────

    def generate_key(
        self,
        strategy: str,
        token_id: str,
        side: str,
        size: float,
        price: float,
    ) -> str:
        """Deterministic SHA-256 over the (strategy, token_id, side, price,
        size) 5-tuple. Delegates to ``generate_idempotency_key`` so the
        gate's notion of duplicate matches the OSM audit trail's notion.
        """
        return generate_idempotency_key(strategy, token_id, side, price, size)

    def check_and_record(
        self,
        key: str,
        order_id: str,
        order_request: Optional[dict] = None,
    ) -> tuple[bool, Optional[str]]:
        """Atomic check-then-record.

        Args:
            key: the deterministic idempotency key (from ``generate_key``).
            order_id: the caller's order id (so the duplicate can be cross-
                referenced to the existing order in the audit trail).
            order_request: opaque context dict — stored alongside the cache
                entry for diagnostic logging on a duplicate hit (NOT used
                for dedup decisions; only the ``key`` matters).

        Returns:
            ``(is_dup, existing_order_id)``. ``is_dup`` is True when the
            key was already recorded AND the recorded entry is still within
            its TTL window. ``existing_order_id`` is the order_id of the
            prior (still-valid) entry — ``None`` when ``is_dup`` is False.

        Side effects:
            - When ``is_dup`` is False, records the new (key, order_id, now)
              triple in the cache (replacing any expired prior entry).
            - When ``is_dup`` is True, leaves the cache unchanged so the
              original entry's TTL window is preserved (a third duplicate
              within the same window still sees ``is_dup=True``).
            - LRU-evicts the oldest entries when the cache exceeds
              ``max_entries``.
        """
        if not key:
            # Defensive: an empty key cannot be a duplicate (every empty
            # key collides with every other empty key, which would
            # accidentally block every order). Treat empty as "no
            # dedup signal" → record nothing, return not-duplicate.
            return False, None

        now = time.time()
        with self._lock:
            existing = self._cache.get(key)
            if existing is not None:
                existing_order_id, existing_ts = existing
                age = now - existing_ts
                if age < self._ttl_seconds:
                    # Duplicate within TTL window — reject.
                    log.warning(
                        "[idempotency] duplicate order detected: key=%s "
                        "existing_order_id=%s age=%.1fs ttl=%.1fs",
                        key[:16] + "…", existing_order_id, age, self._ttl_seconds,
                    )
                    return True, existing_order_id
                # Expired — fall through to record the new entry, replacing
                # the stale one. Remove the stale entry first so the LRU
                # pruning below doesn't double-count it.
                self._cache.pop(key, None)

            # Record the new entry.
            self._cache[key] = (order_id, now)

            # LRU prune if over capacity.
            if len(self._cache) > self._max_entries:
                # Sort by recorded_at ascending; evict the oldest until
                # we're back under the cap. ``list`` materialises the
                # items so we can mutate the dict during iteration.
                sorted_items = sorted(
                    self._cache.items(), key=lambda kv: kv[1][1]
                )
                excess = len(self._cache) - self._max_entries
                for k, _v in sorted_items[:excess]:
                    self._cache.pop(k, None)

            return False, None

    def reset(self) -> None:
        """Clear the entire cache. Used by tests to isolate every test
        case from prior-test pollution."""
        with self._lock:
            self._cache.clear()

    def size(self) -> int:
        """Current cache size (for diagnostics / dashboards)."""
        with self._lock:
            return len(self._cache)

    def ttl_seconds(self) -> float:
        """Configured TTL window (for diagnostics)."""
        return self._ttl_seconds

    def get_state(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the cache state for the
        diagnostics surface. Returns counts + config — never the cache
        contents themselves (could leak order details)."""
        with self._lock:
            return {
                "size": len(self._cache),
                "max_entries": self._max_entries,
                "ttl_seconds": self._ttl_seconds,
            }


# Process-wide singleton — constructed at module-import time so every
# call site (strategies/base.submit_order via the pre-submission gate,
# the API route, tests via monkeypatch) shares one cache.
idempotency_manager = IdempotencyManager()


__all__ = [
    "IdempotencyManager",
    "idempotency_manager",
]
