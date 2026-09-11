"""core/memory_monitor.py — Memory usage monitor for the polymarket-bot process.

W22-8 — Provides a singleton ``MemoryMonitor`` that wraps ``psutil.Process``
and exposes:

  * ``get_usage()`` — current RSS / VMS / percent / timestamp, recorded
    into a bounded history (last 100 samples) for trend analysis.

  * ``get_stats()`` — aggregate over the history: avg / max / min RSS,
    plus a coarse "increasing" / "stable" trend label.

The singleton is consumed by ``GET /api/system/memory`` (registered in
``api/server.py``) so the dashboard can render a process-level memory tile
alongside the per-cache tiles produced by ``core.cache.TTLCache.memory_usage``.

Design notes
------------

* **Bounded history** — the in-process ``_history`` list is capped at
  ``_max_history`` (100) samples via the same FIFO drop-oldest pattern
  used by ``core.cache.TTLCache._size_history`` and
  ``core.latency_tracker._pending_size_history``. Without the cap, a
  long-running bot would accumulate the full process-lifetime series
  (a sample every 30s = ~2880 samples/day = ~1 MB/day at 400 bytes
  per dict — not catastrophic, but unnecessary when the trend window
  only cares about the last hour or two).

* **psutil optional** — the module imports cleanly even when psutil
  isn't installed (``get_usage()`` returns ``{"available": False}`` in
  that case). The dashboard endpoint degrades to a "memory monitoring
  unavailable" tile rather than crashing. psutil IS declared in
  ``requirements.txt`` (W22-8) so the unavailable path is for defensive
  parity with ``core.observability_collector._collect_system_metrics``
  rather than a real production scenario.

* **Thread-safe** — ``_history`` is mutated under a ``threading.Lock``
  so concurrent ``get_usage()`` calls (e.g. the observability collector
  + the API endpoint hitting at the same time) can't corrupt the list.
  The lock is held only for the duration of the append + slice —
  sub-microsecond, same rationale as ``core.cache.TTLCache``.

* **No asyncio dependency** — every method is sync so the API endpoint
  wraps the call in ``asyncio.to_thread`` if it ever blocks (it
  doesn't — psutil's ``memory_info()`` is a syscall, not a network
  round-trip). Keeping the module sync lets it be called from the
  synchronous ``core.cache.TTLCache.set`` path if a future hardening
  pass wants to log RSS on every cache eviction.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# W22-8 — Try to import psutil. If unavailable, the monitor degrades to a
# no-op (every method returns ``{"available": False}``) rather than
# crashing the import. The dashboard endpoint handles the unavailable
# path so the rest of the system memory tiles still render.
try:
    import psutil  # type: ignore[import-untyped]
    _PSUTIL_AVAILABLE = True
except ImportError:  # pragma: no cover — defensive: psutil is in requirements.txt
    psutil = None  # type: ignore[assignment]
    _PSUTIL_AVAILABLE = False
    logger.debug(
        "[memory_monitor] psutil not installed — memory monitoring disabled"
    )


class MemoryMonitor:
    """Tracks the polymarket-bot process's memory usage over time.

    The monitor is intentionally simple: it snapshots ``psutil.Process
    .memory_info()`` on every ``get_usage()`` call and retains the last
    ``_max_history`` (100) samples in a FIFO list. ``get_stats()``
    computes avg / max / min / trend over the history so the dashboard
    can render a sparkline + trend label without the bot holding the
    full process-lifetime series in memory.

    Thread-safe via a single ``threading.Lock`` — coarse-grained but
    the per-call critical section is sub-microsecond (append + slice),
    so contention is negligible even at 1k RPS (same rationale as
    ``core.cache.TTLCache``).
    """

    def __init__(self, max_history: int = 100) -> None:
        self._max_history = max(10, int(max_history))
        self._history: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # W22-8 — Cache the ``psutil.Process`` instance so we don't pay
        # the PID-lookup cost on every ``get_usage()`` call. psutil
        # ``Process`` objects are cheap to construct but the cache
        # avoids the ``os.getpid`` syscall (1 µs vs 0.1 µs — negligible
        # in practice, but the cache documents intent).
        self._process: Optional[Any] = (
            psutil.Process(os.getpid()) if _PSUTIL_AVAILABLE else None
        )

    def get_usage(self) -> dict[str, Any]:
        """Get current memory usage.

        Returns a dict with ``rss_mb`` (resident set size in MB),
        ``vms_mb`` (virtual memory size in MB), ``percent`` (the
        process's share of total system memory), and ``timestamp``
        (unix seconds). When psutil is unavailable, returns
        ``{"available": False}`` so callers can degrade gracefully.
        """
        if not _PSUTIL_AVAILABLE or self._process is None:
            return {"available": False}

        try:
            mem = self._process.memory_info()
            usage: dict[str, Any] = {
                "rss_mb": round(float(mem.rss) / 1024.0 / 1024.0, 2),
                "vms_mb": round(float(mem.vms) / 1024.0 / 1024.0, 2),
                "percent": float(self._process.memory_percent()),
                "timestamp": time.time(),
            }
        except Exception as e:
            # psutil raises ``NoSuchProcess`` if the process exited
            # between construction and the call (impossible for ``self``
            # in normal operation, but defensive). Also catches
            # ``AccessDenied`` on locked-down containers.
            logger.debug("[memory_monitor] get_usage failed: %s", e)
            return {"available": False, "error": str(e)}

        # Append under the lock so concurrent callers can't interleave
        # the append + slice (which would leak a >_max_history list if
        # the slice ran between two appends).
        with self._lock:
            self._history.append(usage)
            if len(self._history) > self._max_history:
                # FIFO drop — keep the most recent ``_max_history`` samples.
                self._history = self._history[-self._max_history:]

        return usage

    def get_stats(self) -> dict[str, Any]:
        """Get memory statistics over the bounded history window.

        Returns a dict with:

          * ``current`` — the latest ``get_usage()`` sample (so the
            caller doesn't need a second round-trip).
          * ``avg_rss_mb`` / ``max_rss_mb`` / ``min_rss_mb`` —
            aggregate stats over the history.
          * ``sample_count`` — number of samples in the history.
          * ``trend`` — ``"increasing"`` if the latest RSS is greater
            than the oldest in the history, ``"stable"`` otherwise
            (a more sophisticated diff / slope would be overkill for
            a 100-sample window at 30s cadence = ~50 min of history).
          * ``available`` — False when psutil isn't installed.
        """
        if not _PSUTIL_AVAILABLE or self._process is None:
            return {"available": False}

        # Take a fresh sample so ``current`` reflects the moment of the
        # call (the history is updated as a side effect).
        current = self.get_usage()
        if not current.get("available", True) and "available" in current:
            # psutil unavailable OR a transient failure — propagate.
            return current

        with self._lock:
            # Snapshot the history under the lock so the avg / max / min
            # computation sees a consistent view (a concurrent
            # ``get_usage()`` could otherwise append mid-aggregate).
            history = list(self._history)

        if not history:
            return {
                "available": True,
                "current": current,
                "sample_count": 0,
                "trend": "stable",
            }

        rss_values = [float(h["rss_mb"]) for h in history if "rss_mb" in h]
        if not rss_values:
            return {
                "available": True,
                "current": current,
                "sample_count": len(history),
                "trend": "stable",
            }

        trend = "stable"
        if len(rss_values) >= 2:
            # Compare the latest sample to the oldest in the window.
            # A 5% noise band is treated as "stable" so the trend label
            # doesn't flicker on natural RSS oscillation (GC, allocator
            # fragmentation, etc.).
            first = rss_values[0]
            last = rss_values[-1]
            if first > 0:
                delta_pct = (last - first) / first
                if delta_pct > 0.05:
                    trend = "increasing"
                elif delta_pct < -0.05:
                    trend = "decreasing"

        return {
            "available": True,
            "current": current,
            "avg_rss_mb": round(sum(rss_values) / len(rss_values), 2),
            "max_rss_mb": round(max(rss_values), 2),
            "min_rss_mb": round(min(rss_values), 2),
            "sample_count": len(rss_values),
            "trend": trend,
            "history_max_samples": self._max_history,
        }

    def reset(self) -> None:
        """Clear the history (test helper)."""
        with self._lock:
            self._history.clear()


# Module-level singleton — mirrors the convention used by every other
# ``core.*`` module (``book_poller`` / ``store`` / ``ml_model`` …) so
# importers can grab the instance at module import time without dragging
# in a class.
memory_monitor = MemoryMonitor()


__all__ = ["MemoryMonitor", "memory_monitor"]
