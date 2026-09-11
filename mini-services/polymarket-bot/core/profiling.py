"""Request profiling — tracks per-endpoint latency statistics.

Records p50, p95, p99 latencies per endpoint. Stores in memory (not
persisted — for live monitoring).

Design notes
------------
* Sibling to ``core/rate_limit_tracker.py`` and ``core/cache.py`` — same
  singleton pattern, same coarse-grained ``threading.Lock`` discipline.
  The per-call critical section is sub-microsecond for the dict ops +
  bounded-latencies-list append, so contention is negligible even at
  ~1k RPS.
* Records the last ``MAX_LATENCIES`` (1000) samples per endpoint so the
  p95/p99 percentiles reflect a recent window rather than the full
  process lifetime — a slow endpoint that was fixed an hour ago must
  not be pinned red forever. Eviction is oldest-first via list slice
  (``O(N)`` but runs only every 1000 calls per endpoint so amortised
  ``O(1)`` per call).
* Stats endpoints (``GET /api/profiling/stats``, ``/slowest``,
  ``POST /api/profiling/reset``) are registered into the FastAPI app by
  ``api/server.py``. Stats are not persisted across restarts —
  intentionally, so an operator who restarts the service always sees a
  fresh baseline rather than a stale multi-day view that masks recent
  regressions.
* Endpoint identity: ``f"{method} {path}"`` so the same path under
  different verbs (e.g. ``GET /api/orders`` vs ``DELETE /api/orders``)
  are tracked separately. The raw ``request.url.path`` is used verbatim
  (no normalisation) so path params show up as distinct keys (e.g.
  ``GET /api/depth/0x123...`` vs ``GET /api/depth/0x456...``). This is
  a trade-off: richer per-resource stats at the cost of unbounded key
  cardinality for path-param routes — acceptable because each endpoint
  caps its latencies list at 1000 entries, so the worst-case memory is
  ``1000 * 8 bytes * endpoint_count``, and the dashboard only ever
  renders the top-N slowest.
"""
from __future__ import annotations

import logging
import statistics
import threading
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class EndpointStats:
    """Per-endpoint latency accumulator.

    ``latencies`` is the last ``Profiler.MAX_LATENCIES`` samples (most
    recent last). Older samples are dropped once the cap is reached so
    percentile estimates reflect recent traffic rather than the full
    process lifetime — see the design notes in the module docstring.
    """

    endpoint: str
    method: str
    request_count: int = 0
    total_time: float = 0.0
    latencies: list = field(default_factory=list)  # Last 1000 latencies
    error_count: int = 0
    last_called: float = 0.0

    @property
    def avg_latency(self) -> float:
        return self.total_time / self.request_count if self.request_count > 0 else 0

    @property
    def p50(self) -> float:
        return statistics.median(self.latencies) if self.latencies else 0

    @property
    def p95(self) -> float:
        if not self.latencies:
            return 0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.95)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def p99(self) -> float:
        if not self.latencies:
            return 0
        sorted_lat = sorted(self.latencies)
        idx = int(len(sorted_lat) * 0.99)
        return sorted_lat[min(idx, len(sorted_lat) - 1)]

    @property
    def error_rate(self) -> float:
        return self.error_count / self.request_count if self.request_count > 0 else 0

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "method": self.method,
            "request_count": self.request_count,
            "avg_latency_ms": round(self.avg_latency * 1000, 2),
            "p50_ms": round(self.p50 * 1000, 2),
            "p95_ms": round(self.p95 * 1000, 2),
            "p99_ms": round(self.p99 * 1000, 2),
            "error_count": self.error_count,
            "error_rate": round(self.error_rate * 100, 2),
            "last_called": self.last_called,
        }


class Profiler:
    """In-memory request profiler.

    Thread-safe via a single ``threading.Lock`` — coarse-grained but the
    per-call critical section is sub-microsecond for the dict ops +
    bounded-latencies-list append, so contention is negligible even at
    ~1k RPS (same rationale as ``core/cache.py::TTLCache``).
    """

    MAX_LATENCIES = 1000  # Keep last 1000 per endpoint

    def __init__(self) -> None:
        self._stats: dict[str, EndpointStats] = {}
        self._lock = threading.Lock()

    def record(self, method: str, endpoint: str, duration: float, status: int) -> None:
        """Record one request's outcome.

        ``status >= 400`` counts as an error for ``error_rate``. The
        duration is stored in seconds (matches ``time.perf_counter`` /
        ``time.time()`` deltas); ``to_dict`` converts to milliseconds
        for human readability.
        """
        key = f"{method} {endpoint}"
        with self._lock:
            if key not in self._stats:
                self._stats[key] = EndpointStats(endpoint=endpoint, method=method)

            stat = self._stats[key]
            stat.request_count += 1
            stat.total_time += duration
            stat.last_called = time.time()

            # Keep bounded latencies list
            stat.latencies.append(duration)
            if len(stat.latencies) > self.MAX_LATENCIES:
                stat.latencies = stat.latencies[-self.MAX_LATENCIES:]

            if status >= 400:
                stat.error_count += 1

    def get_stats(self, sort_by: str = "p95") -> list[dict]:
        """Return all endpoint stats, sorted descending by ``sort_by``.

        ``sort_by`` is one of ``p95``, ``p99``, ``avg``, ``count``,
        ``errors``; unknown values fall back to ``p95``.
        """
        with self._lock:
            stats = list(self._stats.values())

        # Sort
        sort_key = {
            "p95": lambda s: s.p95,
            "p99": lambda s: s.p99,
            "avg": lambda s: s.avg_latency,
            "count": lambda s: s.request_count,
            "errors": lambda s: s.error_count,
        }.get(sort_by, lambda s: s.p95)

        stats.sort(key=sort_key, reverse=True)
        return [s.to_dict() for s in stats]

    def get_slowest(self, limit: int = 10) -> list[dict]:
        """Return the ``limit`` slowest endpoints by p95 latency."""
        return self.get_stats(sort_by="p95")[:limit]

    def get_summary(self) -> dict:
        """Return overall totals across every recorded endpoint."""
        with self._lock:
            total_requests = sum(s.request_count for s in self._stats.values())
            total_errors = sum(s.error_count for s in self._stats.values())
            endpoint_count = len(self._stats)

        return {
            "total_endpoints": endpoint_count,
            "total_requests": total_requests,
            "total_errors": total_errors,
            "overall_error_rate": round(
                total_errors / total_requests * 100, 2
            ) if total_requests > 0 else 0,
        }

    def reset(self) -> None:
        """Drop every endpoint's stats. Used by ``POST /api/profiling/reset``.

        Wipes the in-memory ``_stats`` dict so the next ``GET
        /api/profiling/stats`` call starts from a fresh baseline. This is
        the operational equivalent of "restart the service for clean
        numbers" without the restart — useful for capturing a
        short-window profile run after a deploy.
        """
        with self._lock:
            self._stats.clear()


# Singleton — mirrors the ``core.rate_limit_tracker.rate_limit_tracker``
# / ``core.cache.*_cache`` convention so importers can grab the instance
# at module import time without dragging in a class.
profiler = Profiler()


__all__ = [
    "EndpointStats",
    "Profiler",
    "profiler",
]
