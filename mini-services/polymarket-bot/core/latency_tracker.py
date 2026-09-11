"""core/latency_tracker.py — Signal→Order→Fill pipeline latency tracker.

W23-2 — In-memory tracker for the three-stage pipeline latency.

Why a dedicated module?
~~~~~~~~~~~~~~~~~~~~~~~

The unified decision ledger (``core/decision_ledger.py``) records every
stage of the trading pipeline as an audit row, but it is optimised for
*forensic* reconstruction (``GET /api/decision/{token_id}/full-chain``)
rather than for *operational* latency SLO monitoring. Surfacing
"what is the p95 signal→order latency over the last 24h?" against the
ledger would require a per-decision_id JOIN across the
``decision_events`` table filtered by ``stage IN ('SIGNAL','ORDER',
'FILL')`` and then a windowed aggregation — every dashboard poll. That
is too heavy for a polling panel that wants to render in <50 ms.

This module mirrors the proven pattern of ``core/rate_limit_tracker.py``
(W14-7) and ``core/profiling.py`` (W15-4): a small in-memory data
structure (deque + dict) updated by fire-and-forget ``record_*`` calls
from the live trade path, snapshot via ``get_stats()`` / ``get_recent()``
for the dashboard, bounded memory footprint, process-local state, a
process restart zeroes the data (acceptable for a "last N hours"
operational view).

Pipeline stages
---------------

The tracker keys every record on the ``correlation_id`` (alias for the
decision_ledger's ``decision_id`` — the same identifier that already
threads MARKET_SNAPSHOT → … → FILL). Each record accumulates up to three
timestamps as the corresponding ``record_*`` call fires:

  1. ``record_signal(correlation_id, token_id, strategy)`` — set
     ``signal_time`` (and ``token_id`` / ``strategy`` for the
     by-strategy breakdown).
  2. ``record_order(correlation_id)`` — set ``order_time``; computes
     ``signal_to_order_ms``.
  3. ``record_fill(correlation_id)`` — set ``fill_time``; computes
     ``order_to_fill_ms`` and ``signal_to_fill_ms``.

A record is *complete* when ``fill_time`` is non-None (every stage has
fired). The stats endpoint reports p50 / p95 / p99 latencies for each
of the three segments, plus a per-strategy breakdown, a count of
in-flight records (signal recorded, fill not yet), and a count of
records that fired signal+order but never filled (the "orphaned order"
signal an operator investigating stale-order cancels cares about).

Concurrency
-----------
The tracker is called from:

  * ``strategies/signal_trader.py::_ml_signal`` (async strategy task);
  * ``strategies/base.py::submit_order`` (async strategy task);
  * ``paper/simulator.py::_execute_fill`` (async paper fill loop) and
    ``core/live_fill_monitor.py::_process_trade`` (async live fill
    poller).

All callers are async but the mutations under ``self._lock`` are short
(dict / deque updates). ``threading.Lock`` defends against the
``dict.__setitem__`` / ``deque.append`` interleaving that could otherwise
drop a latency sample if a context switch landed between the timestamp
write and the latency computation. ``get_stats`` snapshots the lists
under the lock and then does the percentile math outside the lock so a
slow ``get_stats`` call can't starve the writers.
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional


# ── Defaults ─────────────────────────────────────────────────────────────────
# The tracker retains the last ``_MAX_RECORDS`` decisions in memory (bounded
# deque, oldest evicted) and ``get_stats(hours=...)`` filters further to the
# trailing window. 5000 is enough to cover a busy 24h window for a single
# strategy process; if a higher rate ever needs supporting, raise the cap
# (memory cost: ~200 bytes per record → 1 MB at 5000).
_MAX_RECORDS = 5000


@dataclass
class LatencyRecord:
    """Accumulates the three pipeline timestamps for one ``correlation_id``.

    Stored in a ``deque(maxlen=_MAX_RECORDS)`` so the tracker bounds its
    memory footprint regardless of how many decisions arrive. A record
    is created lazily by ``record_signal`` and mutated in place by
    ``record_order`` / ``record_fill``.
    """

    correlation_id: str
    token_id: str = ""
    strategy: str = ""
    signal_time: Optional[float] = None
    order_time: Optional[float] = None
    fill_time: Optional[float] = None
    # Pre-computed latencies (ms). Populated as each downstream stage
    # fires so ``get_stats`` can iterate the deque once and bucket by
    # completion state without re-doing the arithmetic.
    signal_to_order_ms: Optional[float] = None
    order_to_fill_ms: Optional[float] = None
    signal_to_fill_ms: Optional[float] = None

    def to_dict(self) -> dict:
        """Serialise to a JSON-friendly dict for the ``/recent`` endpoint."""
        return {
            "correlation_id": self.correlation_id,
            "token_id": self.token_id,
            "strategy": self.strategy,
            "signal_time": self.signal_time,
            "order_time": self.order_time,
            "fill_time": self.fill_time,
            "signal_to_order_ms": self.signal_to_order_ms,
            "order_to_fill_ms": self.order_to_fill_ms,
            "signal_to_fill_ms": self.signal_to_fill_ms,
            "complete": self.fill_time is not None,
        }


class LatencyTracker:
    """In-memory signal→order→fill latency tracker.

    State
    -----
    ``_records`` : ``deque(maxlen=_MAX_RECORDS)`` of every decision the
                    tracker has seen, oldest evicted automatically.
    ``_index``   : ``correlation_id -> LatencyRecord`` so ``record_order``
                    / ``record_fill`` can find the existing record in
                    O(1) without scanning the deque. The index is NOT
                    bounded — it is pruned in lockstep with deque
                    eviction (the ``_prune_index`` helper drops entries
                    no longer in the deque).
    ``_lock``    : coarse mutex around every mutator + the snapshot in
                    ``get_stats`` / ``get_recent``.

    All state is process-local. A restart zeroes everything. There is
    intentionally no disk persistence — the dashboard exists to surface
    *recent* patterns, not as an audit trail (audit lives in
    ``core/decision_ledger.py`` / ``core/immutable_audit.py``).
    """

    def __init__(self, max_records: int = _MAX_RECORDS) -> None:
        self._records: Deque[LatencyRecord] = deque(maxlen=max_records)
        self._max_records = max_records
        self._index: Dict[str, LatencyRecord] = {}
        self._lock = threading.Lock()

    # ── Mutators ────────────────────────────────────────────────────────

    def record_signal(
        self,
        correlation_id: str,
        token_id: str = "",
        strategy: str = "",
    ) -> None:
        """Record the SIGNAL stage timestamp for ``correlation_id``.

        Idempotent: if the correlation_id already has a record (e.g. a
        retried signal), the existing record is preserved and only the
        missing fields are populated — ``signal_time`` is NOT overwritten
        so the first-signal timestamp remains the canonical anchor for
        downstream latency math.

        Called from ``strategies/signal_trader.py::_ml_signal`` after the
        decision-ledger SIGNAL stage is recorded. Best-effort: a tracker
        exception must NEVER break the strategy scan (the signal has
        already been generated; the tracker is purely observability).
        """
        if not correlation_id:
            return
        with self._lock:
            rec = self._index.get(correlation_id)
            if rec is None:
                rec = LatencyRecord(
                    correlation_id=correlation_id,
                    token_id=token_id,
                    strategy=strategy,
                    signal_time=time.time(),
                )
                self._append_record(rec)
            else:
                # Existing record — populate the missing fields without
                # overwriting signal_time (preserve the first-signal
                # anchor for retried signals).
                if rec.signal_time is None:
                    rec.signal_time = time.time()
                if not rec.token_id and token_id:
                    rec.token_id = token_id
                if not rec.strategy and strategy:
                    rec.strategy = strategy

    def record_order(self, correlation_id: str) -> None:
        """Record the ORDER stage timestamp for ``correlation_id``.

        Computes ``signal_to_order_ms`` if ``signal_time`` is already
        set; otherwise leaves it ``None`` (the order was submitted
        without a recorded signal — e.g. a manual order — and the
        signal→order segment is simply not available for that record).

        Called from ``strategies/base.py::submit_order`` after the
        RISK_APPROVED decision-ledger stage is recorded and before the
        paper / live submit path actually places the order. Best-effort:
        wrapped in try/except at the call site so a tracker exception
        never blocks order submission.
        """
        if not correlation_id:
            return
        now = time.time()
        with self._lock:
            rec = self._index.get(correlation_id)
            if rec is None:
                # Order submitted without a prior signal record (manual
                # order, or signal recording failed). Create a stub so
                # the FILL stage can still anchor to ``order_time`` for
                # the order→fill segment.
                rec = LatencyRecord(
                    correlation_id=correlation_id,
                    order_time=now,
                )
                self._append_record(rec)
            else:
                if rec.order_time is None:
                    rec.order_time = now
                    if rec.signal_time is not None:
                        rec.signal_to_order_ms = max(
                            0.0, (now - rec.signal_time) * 1000.0
                        )

    def record_fill(self, correlation_id: str) -> None:
        """Record the FILL stage timestamp for ``correlation_id``.

        Computes ``order_to_fill_ms`` and ``signal_to_fill_ms`` if the
        upstream timestamps are present. A record is *complete* once
        ``fill_time`` is set.

        Called from ``paper/simulator.py::_execute_fill`` (paper mode)
        and ``core/live_fill_monitor.py::_process_trade`` (live mode)
        after the decision-ledger FILL stage is recorded. Best-effort:
        wrapped in try/except at the call site so a tracker exception
        never blocks a fill.
        """
        if not correlation_id:
            return
        now = time.time()
        with self._lock:
            rec = self._index.get(correlation_id)
            if rec is None:
                # Fill arrived for a decision we never tracked (signal +
                # order recording both failed, or the decision_id is from
                # a pre-W23-2 session). Create a stub so the dashboard
                # at least sees the fill event.
                rec = LatencyRecord(
                    correlation_id=correlation_id,
                    fill_time=now,
                )
                self._append_record(rec)
            else:
                if rec.fill_time is None:
                    rec.fill_time = now
                    if rec.order_time is not None:
                        rec.order_to_fill_ms = max(
                            0.0, (now - rec.order_time) * 1000.0
                        )
                    if rec.signal_time is not None:
                        rec.signal_to_fill_ms = max(
                            0.0, (now - rec.signal_time) * 1000.0
                        )

    # ── Read API ────────────────────────────────────────────────────────

    def get_stats(self, hours: float = 24.0) -> dict:
        """Return a snapshot of the trailing ``hours`` window's latencies.

        The returned shape is consumed directly by the dashboard's
        ``GET /api/latency/stats`` route (registered in ``api/server.py``)
        and is intended for rendering by a future ``LatencyPanel`` React
        component. All percentile maps are pre-computed so the JSON
        payload is bounded regardless of how many records are in the
        deque.

        Shape::

            {
              "window_hours": 24.0,
              "total_records": 142,
              "complete_records": 87,
              "in_flight_records": 12,   # signal recorded, fill pending
              "orphaned_records": 43,   # signal+order recorded, no fill
              "signal_only_records": 0, # signal recorded, no order/fill
              "latencies_ms": {
                "signal_to_order": {"count": 95, "avg": 12.3, "p50": 8.1,
                                    "p95": 45.6, "p99": 89.2, "max": 120.5},
                "order_to_fill":   {...},
                "signal_to_fill":   {...}
              },
              "by_strategy": {
                "signal_trader": {
                  "count": 87,
                  "signal_to_order_p95_ms": 45.6,
                  "order_to_fill_p95_ms": 234.5,
                  "signal_to_fill_p95_ms": 280.1
                },
                ...
              }
            }
        """
        cutoff = time.time() - hours * 3600.0
        with self._lock:
            snapshot = [r for r in self._records if r.signal_time is not None and r.signal_time >= cutoff]

        # ── Counts ───────────────────────────────────────────────────────
        complete = sum(1 for r in snapshot if r.fill_time is not None)
        in_flight = sum(
            1 for r in snapshot
            if r.signal_time is not None and r.fill_time is None and r.order_time is not None
        )
        orphaned = in_flight  # alias: signal+order, no fill
        signal_only = sum(
            1 for r in snapshot
            if r.signal_time is not None and r.order_time is None
        )

        # ── Per-segment latencies (ms) ───────────────────────────────────
        s2o = [r.signal_to_order_ms for r in snapshot if r.signal_to_order_ms is not None]
        o2f = [r.order_to_fill_ms for r in snapshot if r.order_to_fill_ms is not None]
        s2f = [r.signal_to_fill_ms for r in snapshot if r.signal_to_fill_ms is not None]

        # ── Per-strategy breakdown ───────────────────────────────────────
        # Bucket the records by strategy, then compute p95 for each segment.
        # Strategies with <2 records get a single-row entry without
        # percentiles (p95 of one sample is the sample itself; we still
        # surface it so the panel can render the row).
        by_strategy: Dict[str, dict] = {}
        strat_buckets: Dict[str, list] = {}
        for r in snapshot:
            if not r.strategy:
                continue
            strat_buckets.setdefault(r.strategy, []).append(r)
        for strat, recs in strat_buckets.items():
            s2o_strat = [x.signal_to_order_ms for x in recs if x.signal_to_order_ms is not None]
            o2f_strat = [x.order_to_fill_ms for x in recs if x.order_to_fill_ms is not None]
            s2f_strat = [x.signal_to_fill_ms for x in recs if x.signal_to_fill_ms is not None]
            by_strategy[strat] = {
                "count": len(recs),
                "signal_to_order_p95_ms": _percentile(s2o_strat, 0.95),
                "order_to_fill_p95_ms": _percentile(o2f_strat, 0.95),
                "signal_to_fill_p95_ms": _percentile(s2f_strat, 0.95),
            }

        return {
            "window_hours": float(hours),
            "total_records": len(snapshot),
            "complete_records": complete,
            "in_flight_records": in_flight,
            "orphaned_records": orphaned,
            "signal_only_records": signal_only,
            "latencies_ms": {
                "signal_to_order": _segment_stats(s2o),
                "order_to_fill": _segment_stats(o2f),
                "signal_to_fill": _segment_stats(s2f),
            },
            "by_strategy": by_strategy,
        }

    def get_recent(self, limit: int = 50) -> list[dict]:
        """Return the ``limit`` most-recent records as JSON-friendly dicts.

        Sorted by ``signal_time`` descending (newest first) so a
        dashboard "live event stream" panel can render the most recent
        decisions at the top. ``limit`` is clamped to ``[1, 500]`` to
        bound the payload size — the dashboard can paginate if it ever
        needs more.
        """
        limit = max(1, min(500, int(limit)))
        with self._lock:
            snapshot = list(self._records)
        # Newest first. Records with no signal_time (stub created by
        # record_order or record_fill for an un-tracked correlation_id)
        # sort by their first-set timestamp so they appear at the top of
        # the recent view too.
        def _sort_key(r: LatencyRecord) -> float:
            return r.fill_time or r.order_time or r.signal_time or 0.0
        snapshot.sort(key=_sort_key, reverse=True)
        return [r.to_dict() for r in snapshot[:limit]]

    # ── Test / operational helpers ─────────────────────────────────────

    def reset(self) -> None:
        """Clear all tracked state. Used by tests for hermetic isolation."""
        with self._lock:
            self._records.clear()
            self._index.clear()

    # ── Internal ───────────────────────────────────────────────────────

    def _append_record(self, rec: LatencyRecord) -> None:
        """Append ``rec`` to the deque and the index, pruning the index
        when the deque's maxlen cap evicts the oldest entry.

        MUST be called under ``self._lock``.
        """
        # If the deque is about to evict the oldest record, drop its
        # index entry too so the index doesn't grow without bound. The
        # ``deque`` doesn't expose a "would-evict" hook, so we check
        # ``len == maxlen`` before appending — if the deque is full, the
        # next append WILL evict ``self._records[0]``.
        if len(self._records) == self._max_records:
            evicted = self._records[0]
            # Drop the evicted record's index entry — but ONLY if the
            # index still points at it (a retried signal may have
            # already replaced the index entry with a newer record for
            # the same correlation_id; in that case leave the newer
            # entry alone).
            if self._index.get(evicted.correlation_id) is evicted:
                self._index.pop(evicted.correlation_id, None)
        self._records.append(rec)
        self._index[rec.correlation_id] = rec


# ── Helpers ─────────────────────────────────────────────────────────────────


def _percentile(values: list[float], pct: float) -> float:
    """Return the ``pct``-th percentile of ``values`` (0.0..1.0).

    Uses the "nearest rank" method (sorted, index = ceil(pct * n) - 1)
    so the result is always a real sample (no interpolation). Returns
    0.0 for an empty list (the dashboard renders 0 as "no data").
    """
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n == 1:
        return float(s[0])
    # Nearest-rank: index = ceil(pct * n) - 1, clamped to [0, n-1].
    idx = max(0, min(n - 1, math.ceil(pct * n) - 1))
    return float(s[idx])


def _segment_stats(values: list[float]) -> dict:
    """Compute count / avg / p50 / p95 / p99 / max for a segment.

    Returns zeroes for every field when ``values`` is empty so the
    dashboard can render an empty-state row without special-casing.
    """
    if not values:
        return {
            "count": 0,
            "avg": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
        }
    return {
        "count": len(values),
        "avg": round(sum(values) / len(values), 3),
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "p99": round(_percentile(values, 0.99), 3),
        "max": round(max(values), 3),
    }


# ── Module-level singleton ──────────────────────────────────────────────────
# Mirrors the pattern used by ``core.rate_limit_tracker`` /
# ``core.profiling``: the singleton is constructed at module-import time
# so every import site (``strategies/signal_trader.py``,
# ``strategies/base.py``, ``paper/simulator.py``,
# ``core/live_fill_monitor.py``, ``api/server.py``, tests…) shares the
# same in-memory tracker. A process restart zeroes the state, which is
# fine for a dashboard whose window is "last N hours".
latency_tracker = LatencyTracker()


__all__ = ["LatencyRecord", "LatencyTracker", "latency_tracker"]
