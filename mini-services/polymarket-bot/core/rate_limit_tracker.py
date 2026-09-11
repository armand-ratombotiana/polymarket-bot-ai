"""core/rate_limit_tracker.py — Tracks rate limit hits for the analytics dashboard.

W14-7 — In-memory rate limit hit tracker (not persisted — for dashboard only).

Why a dedicated module?
~~~~~~~~~~~~~~~~~~~~~~~

The existing ``core.prometheus_metrics`` module already exposes a
``rate_limit_hits_total`` counter that scrapes into Grafana via the
``/metrics`` endpoint — but that surface is keyed on Prometheus's label
model (``endpoint``) and is intentionally minimal (one monotonic counter).
The W14-7 dashboard panel needs richer information:

  * which *client IPs* are hitting the limit (so an operator can spot a
    single noisy caller);
  * a *per-minute* time-series of hits over the last hour (so the
    dashboard can show a "rate of rate-limiting" line chart, not just a
    cumulative count);
  * the *limit string* the caller violated (e.g. ``5/minute``) so the
    panel can group hits by policy tier;
  * a *sample* of recent hits (capped) so the dashboard can show a
    live event stream if we extend it later.

Adding these to Prometheus would require new labels (high-cardinality
risk for ``client_ip``) and a separate histogram for the per-minute
series — both add complexity to the scraping pipeline. A small
in-memory tracker with a deque + a couple of default-dicts is the
right tool for the dashboard's "last hour, top N" view, where the
data is ephemeral and a process restart is an acceptable freshness
boundary.

Concurrency
------------
``RateLimitTracker`` is called from:

  * the ``rate_limit_handler`` exception handler in ``api/server.py``
    (which runs on the FastAPI worker thread);
  * the ``request_logging_middleware`` for every request (also a
    worker-thread path).

Both paths are async but the tracker's ``with self._lock`` blocks are
short (deque append + dict increment) — the GIL serialises the actual
mutations, and ``threading.Lock`` defends against the
``deque.append``/``dict.__setitem__`` interleaving that could otherwise
drop a count if a context switch landed between the append and the
``self._request_counts[endpoint] += 1`` line. The lock is NOT held
across ``get_stats``'s full pass (only to snapshot the lists) — but
since the lists are rebuilt each call from a fresh ``list(...)``
materialisation, an appends-during-iteration race would at worst
under-count by one sample for the in-flight request, never crash.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, Dict


@dataclass(frozen=True)
class RateLimitHit:
    """Single rate-limit-hit event.

    Stored in a ``deque(maxlen=...)`` so the tracker bounds its memory
    footprint regardless of how many hits arrive.
    """

    timestamp: float
    endpoint: str
    method: str
    client_ip: str
    limit: str  # e.g. "120/minute" — the policy the caller exceeded


class RateLimitTracker:
    """In-memory rate limit hit tracker (not persisted — for dashboard only).

    State
    -----
    ``_hits``           : ``deque(maxlen=max_records)`` of every rate limit
                          hit the server has emitted, oldest evicted
                          automatically.
    ``_request_counts`` : ``endpoint -> total_requests`` counter for the
                          "most-requested endpoints" view (NOT only the
                          rate-limited ones — covers the dashboard's
                          "Top endpoints" table). The key is the raw
                          endpoint path; record_request keys it as
                          ``"endpoint:status"`` for finer-grained panels
                          that may want to surface 4xx vs 2xx separately.
    ``_lock``           : coarse mutex around the two mutators.

    All state is process-local. A restart zeroes everything. There is
    intentionally no disk persistence — the dashboard exists to surface
    *recent* patterns, not as an audit trail (audit lives in
    ``core/audit_logger.py``).
    """

    def __init__(self, max_records: int = 1000):
        self._hits: Deque[RateLimitHit] = deque(maxlen=max_records)
        # Mixed-purpose: record_hit increments ``endpoint``, record_request
        # increments ``"endpoint:status"``. A single dict keeps the
        # memory footprint small and lets the panel iterate once over
        # both surfaces.
        self._request_counts: Dict[str, int] = defaultdict(int)
        self._lock = threading.Lock()

    # ── Mutators ────────────────────────────────────────────────────────

    def record_hit(
        self,
        endpoint: str,
        method: str,
        client_ip: str,
        limit: str,
    ) -> None:
        """Record a single rate-limit hit (a 429 was returned).

        Called from the ``rate_limit_handler`` exception handler in
        ``api/server.py`` — every ``RateLimitExceeded`` exception is
        appended to the in-memory tracker before the 429 response is
        returned. Best-effort: a tracker exception must NEVER change the
        429 response shape (the rate-limit decision has already been made;
        the tracker is purely observability).
        """
        with self._lock:
            self._hits.append(
                RateLimitHit(
                    timestamp=time.time(),
                    endpoint=endpoint,
                    method=method,
                    client_ip=client_ip,
                    limit=limit,
                )
            )
            # Bump the per-endpoint rate-limit-hit counter so the "top
            # endpoints" table reflects the rate-limit view too (it's
            # incremented separately from record_request so the two
            # surfaces can diverge if needed).
            self._request_counts[endpoint] += 1

    def record_request(self, endpoint: str, status: int) -> None:
        """Record ALL requests (not just rate-limited ones).

        Called from ``request_logging_middleware`` in ``api/server.py``
        for every HTTP response (2xx / 4xx / 5xx alike). The key is
        ``f"{endpoint}:{status}"`` so the dashboard can group by status
        code (e.g. a sudden spike in ``GET /api/orders:500`` is a
        different signal than ``GET /api/orders:200``).
        """
        with self._lock:
            key = f"{endpoint}:{status}"
            self._request_counts[key] += 1

    # ── Read API ────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Return a snapshot of the last hour's rate-limit activity.

        The returned shape is consumed directly by the dashboard's
        ``GET /api/rate-limit/stats`` route (registered in
        ``api/server.py``) and rendered by the ``RateLimitPanel`` React
        component. All "by X" maps are pre-sorted descending by count and
        capped (top-N) so the JSON payload is bounded regardless of how
        many distinct endpoints / clients have been seen.
        """
        # Snapshot the deque under the lock so the iteration below can't
        # be raced by an appender. The list copy is O(N) but N is
        # bounded by max_records (default 1000), so this is cheap.
        with self._lock:
            hits_snapshot = list(self._hits)
            counts_snapshot = dict(self._request_counts)

        now = time.time()
        # Recent hits (last hour). The dashboard's "Total hits (last
        # hour)" KPI is the length of this filtered list.
        recent_hits = [h for h in hits_snapshot if now - h.timestamp < 3600]

        # ── Hits by endpoint ───────────────────────────────────────────
        by_endpoint: Dict[str, int] = defaultdict(int)
        for h in recent_hits:
            by_endpoint[h.endpoint] += 1

        # ── Hits by client IP ──────────────────────────────────────────
        by_client: Dict[str, int] = defaultdict(int)
        for h in recent_hits:
            by_client[h.client_ip] += 1

        # ── Hits per minute (last 60 minutes) ──────────────────────────
        # Bucket each hit into its minute (epoch // 60) and present the
        # series as ``{minutes_ago: count}`` where ``minutes_ago`` is
        # ``60 - (now_minute - hit_minute)`` so the dashboard can label
        # the x-axis as "60m ago ... 1m ago ... now" without doing the
        # epoch math itself.
        now_minute = int(now // 60)
        hits_per_minute: Dict[int, int] = defaultdict(int)
        for h in recent_hits:
            minute = int(h.timestamp // 60)
            hits_per_minute[60 - (now_minute - minute)] += 1

        # ── Top requested endpoints (all-requests view, not just hits) ─
        # Filter the request_counts snapshot down to keys WITHOUT a ":"
        # (i.e. the raw-endpoint counters incremented by record_hit, not
        # the "endpoint:status" pairs incremented by record_request) so
        # the "Top endpoints" table reflects rate-limit-hit volume
        # specifically — that's the metric the operator cares about when
        # investigating "which routes are getting throttled the most".
        hit_endpoints = {
            k: v
            for k, v in counts_snapshot.items()
            if ":" not in k
        }

        # Hit rate (hits/min over the last hour). If we have hits, divide
        # by the elapsed minutes between the oldest hit and now (clamped
        # to [1, 60]) so a single hit at minute 0 doesn't report as
        # "1 hit / 60 min = 0.017/min" — instead, if all hits arrived in
        # the last 5 minutes, the rate reflects that 5-minute window.
        if recent_hits:
            oldest_ts = min(h.timestamp for h in recent_hits)
            elapsed_min = max(1.0, min(60.0, (now - oldest_ts) / 60.0))
            hits_per_min = round(len(recent_hits) / elapsed_min, 2)
        else:
            hits_per_min = 0.0

        return {
            "total_hits": len(recent_hits),
            "hits_per_minute_rate": hits_per_min,
            "hits_by_endpoint": dict(
                sorted(by_endpoint.items(), key=lambda x: -x[1])[:20]
            ),
            "hits_by_client": dict(
                sorted(by_client.items(), key=lambda x: -x[1])[:10]
            ),
            "hits_per_minute": dict(sorted(hits_per_minute.items())),
            "top_endpoints": dict(
                sorted(hit_endpoints.items(), key=lambda x: -x[1])[:20]
            ),
        }

    def reset(self) -> None:
        """Clear all tracked state. Used by tests for hermetic isolation."""
        with self._lock:
            self._hits.clear()
            self._request_counts.clear()


# ── Module-level singleton ──────────────────────────────────────────────────
# Mirrors the pattern used by ``core.cache`` / ``core.prometheus_metrics``:
# the singleton is constructed at module-import time so every import site
# (``api/server.py``, ``core/live_safety_gate.py``, tests…) shares the same
# in-memory tracker. A process restart zeroes the state, which is fine
# for a dashboard whose window is "last hour".
rate_limit_tracker = RateLimitTracker()


__all__ = ["RateLimitHit", "RateLimitTracker", "rate_limit_tracker"]
