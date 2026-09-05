"""Ingestion health monitor — tracks pipeline health.

Metrics:
- Events per second (throughput)
- Ingestion latency (event_time → processing_time)
- Failed records count
- Dead-letter queue depth
- Data gaps detected
- Source availability
- Last successful event per source
- Replay capability status

Alerts:
- No data received in 60s
- Error rate > 5%
- Dead-letter queue > 100 items
- Latency > 5s

W31-4 — Ingestion pipeline health monitor.

The monitor is IN-MEMORY (no SQLite persistence) because:

  * The metrics are short-horizon — the throughput window is 60s,
    the latency window is the most recent event, the error-rate is
    a running total since process start. None of these are useful
    across restarts (a restart zeroes them, which is the right
    behaviour — a fresh process is a fresh observation).
  * The dashboard polls ``GET /api/ingestion/health`` once per
    second; in-memory reads are sub-microsecond, which is what the
    dashboard's 1Hz polling cycle requires.
  * Persistence for the DLQ + checkpoint side is already covered by
    the SQLite-backed ``DeadLetterQueue`` and ``CheckpointManager``
    (this module only mirrors their depths into the metrics view).

Alerts are fired via ``core.alerting.alert_engine.record_alert`` so
they appear on the operator dashboard exactly like the existing
``max_drawdown_exceeded`` / ``model_drift_detected`` alerts. Each
alert is debounced per (source, alert_name) — the same alert fires
at most once per ``ALERT_DEBOUNCE`` window (default 60s) so a
sustained threshold breach doesn't flood the dashboard.

Contract
--------
``record_event(source, event_time=None, success=True, error='') -> None``
    Record one processed event. ``event_time`` is the event-side
    timestamp (Unix epoch float). When ``None``, defaults to
    ``time.time()`` (the processing time). ``success=False`` flips
    the event to a failure (``error`` is stored on the per-source
    health record so the next ``get_metrics`` snapshot surfaces the
    most recent error message).

``record_failure(source, error='') -> None``
    Record one failure WITHOUT updating the last-event timestamp.
    Use this when the failure happened BEFORE the event was
    received (e.g. the source's API returned a 5xx and no record
    was processed). The health monitor's ``no_data_received`` alert
    then fires on the absence of successful events.

``record_dlq_depth(source, depth) -> None``
    Update the per-source DLQ depth. The DLQ itself is owned by
    ``ingestion.dead_letter``; this method mirrors the depth into
    the health view so the ``check_alerts`` cycle can fire the
    ``dlq_depth_high`` alert without an extra SQLite query.

``mark_available(source) / mark_unavailable(source) -> None``
    Flip the per-source ``available`` flag. Used by the source
    connector's circuit-breaker hook so the dashboard surfaces a
    "source down" state immediately (rather than waiting for the
    ``no_data_received`` threshold to fire after 60s of silence).

``get_metrics(source=None) -> dict``
    Return the live per-source metrics (or every source when
    ``source=None``). JSON-serialisable — exposed via the
    ``GET /api/ingestion/health`` endpoint.

``check_alerts() -> list[dict]``
    Evaluate every alert threshold against the live metrics. Fires
    any newly-crossed alerts (debounced) and returns the list of
    alerts fired in this evaluation cycle. The dashboard's
    ``POST /api/ingestion/health/evaluate`` endpoint triggers this
    cycle on demand; the background ``asyncio`` task in
    ``main.py`` triggers it every 10s.

Thread-safety
-------------
Every public method acquires ``self._lock`` so the monitor is safe
to call from sync and async code paths alike. The underlying
``dict`` and ``deque`` are not thread-safe by themselves — the
``threading.Lock`` wrapper provides the atomicity guarantee.

The throughput ``deque(maxlen=1000)`` per source is bounded so a
long-running session can't grow it without limit — 1000 events at
10 EPS = 100s of history, which is well beyond the 60s throughput
window.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Alert thresholds ───────────────────────────────────────────────────────
# Tunable constants — exposed at module level so tests can patch them
# without constructing a custom monitor subclass. The values match the
# W31-4 task spec.
NO_DATA_THRESHOLD: float = 60.0  # seconds with no successful events
ERROR_RATE_THRESHOLD: float = 0.05  # 5%
DLQ_DEPTH_THRESHOLD: int = 100  # records
LATENCY_THRESHOLD: float = 5.0  # seconds
ALERT_DEBOUNCE: float = 60.0  # don't re-fire same alert within 60s

# Severity constants (mirror ``core.alerting`` so we don't have to
# import the whole module at file-load time).
_SEVERITY_WARNING = "warning"
_SEVERITY_CRITICAL = "critical"


@dataclass
class SourceHealth:
    """Per-source health metrics.

    Attributes:
        source: Source identifier (e.g. ``"clob_rest"``).
        events_received: Total events seen (success + failure).
        events_failed: Subset of ``events_received`` that failed.
        last_event_at: Wall-clock time of the last successful event.
        last_event_time: Event-side timestamp of the last event.
        last_processing_time: Wall-clock processing time of the
            last event.
        last_latency: ``last_processing_time - last_event_time``.
            Negative values are clamped to 0 (clock skew between
            the source and the bot's wall clock).
        last_error: The most recent error message (empty string if
            the last event succeeded). Surfaced in the dashboard's
            per-source drill-down.
        latencies: Bounded ``deque`` of recent processing-time
            timestamps (used by ``throughput()`` to compute
            events-per-second over a rolling window).
        dlq_depth: Dead-letter queue depth for this source.
            Mirrored from ``ingestion.dead_letter.DeadLetterQueue.depth``
            by the connector's periodic poll.
        available: Liveness flag. ``False`` when the source's
            circuit breaker is open. Surfaced immediately on the
            dashboard — don't wait for the ``no_data_received``
            60s threshold to fire.
    """

    source: str
    events_received: int = 0
    events_failed: int = 0
    last_event_at: float = 0.0
    last_event_time: float = 0.0
    last_processing_time: float = 0.0
    last_latency: float = 0.0
    last_error: str = ""
    dlq_depth: int = 0
    available: bool = True
    latencies: deque = field(default_factory=lambda: deque(maxlen=1000))

    def throughput(self, window: float = 60.0) -> float:
        """Events per second over the last ``window`` seconds.

        Returns 0.0 if no events have been received in the window.
        The window filters on ``last_processing_time`` (wall clock)
        — not on ``last_event_time`` (which may be in the past for
        a back-fill replay).
        """
        if not self.latencies:
            return 0.0
        now = time.time()
        recent = [t for t in self.latencies if (now - t) <= window]
        if not recent:
            return 0.0
        span = max(now - min(recent), 1.0)
        return len(recent) / span


class IngestionHealthMonitor:
    """Ingestion pipeline health monitor.

    Tracks per-source health metrics and fires alerts when
    thresholds are crossed. The monitor is IN-MEMORY — metrics are
    zeroed on process restart (which is the right behaviour for
    short-horizon observability; long-horizon provenance lives in
    the SQLite-backed ``DeadLetterQueue`` and ``CheckpointManager``).
    """

    def __init__(self) -> None:
        self._sources: dict[str, SourceHealth] = {}
        self._lock = threading.Lock()
        # ``(source, alert_name) -> last_fire_time`` — debounce map
        # so the same alert doesn't fire more than once per
        # ``ALERT_DEBOUNCE`` window.
        self._last_alert: dict[tuple[str, str], float] = {}
        # W32-2 — lifecycle flag flipped by ``start()`` / ``stop()``
        # (called from the FastAPI lifespan). The monitor's per-source
        # bookkeeping is event-driven (every ``record_event`` call
        # mutates the relevant ``SourceHealth``), so ``start()`` does
        # NOT spin up a background task — it just flips ``_running``
        # so the ``/api/status`` endpoint's ``health.is_running``
        # field reflects "the lifespan has run" rather than "the
        # module has been imported". The alert-evaluation cycle is
        # driven by the operator dashboard's
        # ``POST /api/ingestion/health/evaluate`` trigger and the
        # background ``asyncio`` task in ``main.py`` (W31-4) — this
        # flag does NOT gate that.
        self._running: bool = False

    # ── Recording ──────────────────────────────────────────────────────────

    def _get_or_create(self, source: str) -> SourceHealth:
        """Return the ``SourceHealth`` for ``source`` (creating if absent).

        MUST be called under ``self._lock`` — the caller is
        responsible for taking the lock before invoking this helper.
        """
        if source not in self._sources:
            self._sources[source] = SourceHealth(source=source)
        return self._sources[source]

    def record_event(
        self,
        source: str,
        event_time: float | None = None,
        success: bool = True,
        error: str = "",
    ) -> None:
        """Record one processed event.

        Args:
            source: Source identifier.
            event_time: Event-side timestamp (Unix epoch float).
                When ``None``, defaults to ``time.time()`` (the
                processing time). The latency metric is
                ``processing_time - event_time``.
            success: Whether the event was processed successfully.
                ``False`` flips the event to a failure (still
                counted in ``events_received`` so the error rate
                reflects the true ratio).
            error: Error message on failure (empty on success).
        """
        now = time.time()
        if event_time is None:
            event_time = now
        with self._lock:
            h = self._get_or_create(source)
            h.events_received += 1
            if not success:
                h.events_failed += 1
                h.last_error = error
            else:
                h.last_error = ""
            h.last_event_at = now
            h.last_event_time = float(event_time)
            h.last_processing_time = now
            h.last_latency = max(0.0, now - float(event_time))
            h.latencies.append(now)

    def record_failure(self, source: str, error: str = "") -> None:
        """Record one failure WITHOUT updating the last-event timestamp.

        Use this when the failure happened BEFORE the event was
        received (e.g. the source's API returned a 5xx and no record
        was processed). The health monitor's ``no_data_received``
        alert then fires on the absence of successful events.
        """
        with self._lock:
            h = self._get_or_create(source)
            h.events_received += 1
            h.events_failed += 1
            h.last_error = error

    def record_dlq_depth(self, source: str, depth: int) -> None:
        """Update the per-source DLQ depth."""
        with self._lock:
            h = self._get_or_create(source)
            h.dlq_depth = int(depth)

    def mark_unavailable(self, source: str) -> None:
        """Flip the per-source ``available`` flag to ``False``."""
        with self._lock:
            h = self._get_or_create(source)
            h.available = False

    def mark_available(self, source: str) -> None:
        """Flip the per-source ``available`` flag to ``True``."""
        with self._lock:
            h = self._get_or_create(source)
            h.available = True

    # ── Reads ──────────────────────────────────────────────────────────────

    def get_metrics(self, source: str | None = None) -> dict[str, Any]:
        """Return the live per-source metrics.

        Args:
            source: When supplied, returns a single source's
                metrics dict (or ``{}`` if the source is unknown).
                When ``None``, returns a dict keyed by source name.

        Returns:
            JSON-serialisable dict. Per-source shape:

            .. code-block:: python

                {
                    "source": "clob_rest",
                    "events_received": 1234,
                    "events_failed": 12,
                    "error_rate": 0.0097,
                    "last_event_at": 1717283400.5,
                    "last_event_time": 1717283400.4,
                    "last_processing_time": 1717283400.5,
                    "last_latency": 0.1,
                    "throughput_eps": 8.2,
                    "dlq_depth": 0,
                    "available": True,
                }
        """
        with self._lock:
            if source is not None:
                h = self._sources.get(source)
                if h is None:
                    return {}
                return self._format_source(h)
            return {s: self._format_source(h) for s, h in self._sources.items()}

    # ── W32-2 — Lifecycle + cross-source summary ──────────────────────────

    async def start(self) -> None:
        """Mark the monitor as running.

        ``start()`` does NOT spin up a background task — the monitor's
        per-source bookkeeping is event-driven (every ``record_event``
        call mutates the relevant ``SourceHealth``). The alert-eval
        cycle is driven by the operator dashboard's
        ``POST /api/ingestion/health/evaluate`` trigger and the
        background ``asyncio`` task in ``main.py`` (W31-4); this flag
        only gates ``is_running`` so ``/api/status`` can surface
        "lifespan has run" vs "module imported". Idempotent.

        Wrapped as ``async`` so the FastAPI lifespan can ``await``
        every subsystem uniformly (mirrors ``live_fill_monitor.start``
        / ``trade_tape_ingester.start`` / ``paper_sim.start``).
        """
        with self._lock:
            if self._running:
                logger.debug(
                    "[health] start() called but already running — no-op"
                )
                return
            self._running = True
        logger.info("Ingestion health monitor started")

    async def stop(self) -> None:
        """Mark the monitor as stopped.

        Does NOT clear the per-source metrics (the post-shutdown
        ``get_metrics()`` snapshot is read by the operator before the
        process exits — mirrors the pipeline's ``stop()`` contract).
        Idempotent: calling ``stop()`` when not running is a no-op.
        """
        with self._lock:
            if not self._running:
                logger.debug(
                    "[health] stop() called but not running — no-op"
                )
                return
            self._running = False
        logger.info("Ingestion health monitor stopped")

    @property
    def is_running(self) -> bool:
        """``True`` iff ``start()`` has been called and ``stop()`` hasn't."""
        with self._lock:
            return self._running

    def get_summary(self) -> dict[str, Any]:
        """Cross-source aggregate health summary.

        Used by ``/api/status`` to surface a single ingestion-health
        block without forcing the operator to drill into the per-
        source view. The shape mirrors the per-source
        ``_format_source`` dict but cross-source-aggregated:

        - ``sources``: count of distinct sources seen.
        - ``available_sources``: count whose ``available`` flag is
          ``True``.
        - ``events_received``: total across every source.
        - ``events_failed``: total across every source.
        - ``error_rate``: total failed / total received (or 0.0 when
          no events yet — mirrors ``_format_source``).
        - ``throughput_eps``: sum of per-source EPS (an aggregate
          pipeline throughput).
        - ``avg_latency_ms``: average of per-source ``last_latency``
          (in ms) across sources that have at least one event.
          Returns 0.0 when no events have been received.
        - ``dlq_depth``: total DLQ depth across every source.
        - ``last_event_at``: max ``last_event_at`` across sources (so
          a single busy source keeps the freshness signal alive even
          when every other source is silent).
        - ``is_running``: lifecycle flag.
        - ``alerts``: count of alerts currently fired in the most
          recent ``check_alerts()`` cycle (best-effort — recorded
          under the lock so a concurrent ``check_alerts`` doesn't
          race; the value is the count from the last cycle, NOT a
          fresh evaluation).
        """
        with self._lock:
            sources = list(self._sources.values())
            running = self._running
        if not sources:
            return {
                "sources": 0,
                "available_sources": 0,
                "events_received": 0,
                "events_failed": 0,
                "error_rate": 0.0,
                "throughput_eps": 0.0,
                "avg_latency_ms": 0.0,
                "dlq_depth": 0,
                "last_event_at": 0.0,
                "is_running": running,
                "alerts": 0,
            }
        total_received = sum(h.events_received for h in sources)
        total_failed = sum(h.events_failed for h in sources)
        err_rate = (total_failed / total_received) if total_received > 0 else 0.0
        total_eps = sum(h.throughput() for h in sources)
        latencies_ms = [
            h.last_latency * 1000.0 for h in sources if h.last_latency > 0.0
        ]
        avg_latency_ms = (
            sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
        )
        total_dlq = sum(h.dlq_depth for h in sources)
        last_event_at = max(h.last_event_at for h in sources)
        available = sum(1 for h in sources if h.available)
        return {
            "sources": len(sources),
            "available_sources": available,
            "events_received": total_received,
            "events_failed": total_failed,
            "error_rate": err_rate,
            "throughput_eps": total_eps,
            "avg_latency_ms": avg_latency_ms,
            "dlq_depth": total_dlq,
            "last_event_at": last_event_at,
            "is_running": running,
            "alerts": 0,
        }

    # ── Alerts ─────────────────────────────────────────────────────────────

    def check_alerts(self) -> list[dict[str, Any]]:
        """Evaluate every alert threshold against the live metrics.

        Fires any newly-crossed alerts (debounced per
        ``(source, alert_name)``) via
        ``core.alerting.alert_engine.record_alert`` and returns the
        list of alerts fired in this evaluation cycle.

        The dashboard's ``POST /api/ingestion/health/evaluate``
        endpoint triggers this cycle on demand; the background
        ``asyncio`` task in ``main.py`` triggers it every 10s.

        Returns:
            List of dicts with keys: ``source``, ``alert``,
            ``value``, ``threshold``. One entry per alert fired.
        """
        fired: list[dict[str, Any]] = []
        now = time.time()
        # Snapshot the sources under the lock so the evaluation
        # loop doesn't need to re-acquire the lock for each rule.
        with self._lock:
            sources_snapshot = list(self._sources.values())
        for h in sources_snapshot:
            # 1. No data received in NO_DATA_THRESHOLD seconds.
            #    Skip if no event has ever been received
            #    (``last_event_at == 0``) — the source hasn't started
            #    producing yet, which is different from "stalled".
            if h.last_event_at > 0 and (now - h.last_event_at) > NO_DATA_THRESHOLD:
                if self._should_fire(h.source, "no_data_received"):
                    elapsed = now - h.last_event_at
                    self._fire(
                        h.source,
                        "no_data_received",
                        _SEVERITY_WARNING,
                        (
                            f"No data received from source {h.source} in "
                            f"{NO_DATA_THRESHOLD:.0f}s (last event "
                            f"{elapsed:.0f}s ago)"
                        ),
                        value=elapsed,
                        threshold=NO_DATA_THRESHOLD,
                    )
                    fired.append(
                        {
                            "source": h.source,
                            "alert": "no_data_received",
                            "value": elapsed,
                            "threshold": NO_DATA_THRESHOLD,
                        }
                    )
            # 2. Error rate > threshold (only fire when there's been
            #    enough traffic to make the ratio meaningful — at
            #    least 10 events so a single failure out of 1 event
            #    doesn't trip the alert).
            total = h.events_received
            if total >= 10:
                err_rate = h.events_failed / total
                if err_rate > ERROR_RATE_THRESHOLD:
                    if self._should_fire(h.source, "high_error_rate"):
                        self._fire(
                            h.source,
                            "high_error_rate",
                            _SEVERITY_WARNING,
                            (
                                f"Error rate for source {h.source}: "
                                f"{err_rate*100:.1f}% "
                                f"({h.events_failed}/{total} failed)"
                            ),
                            value=err_rate,
                            threshold=ERROR_RATE_THRESHOLD,
                        )
                        fired.append(
                            {
                                "source": h.source,
                                "alert": "high_error_rate",
                                "value": err_rate,
                                "threshold": ERROR_RATE_THRESHOLD,
                            }
                        )
            # 3. DLQ depth > threshold.
            if h.dlq_depth > DLQ_DEPTH_THRESHOLD:
                if self._should_fire(h.source, "dlq_depth_high"):
                    self._fire(
                        h.source,
                        "dlq_depth_high",
                        _SEVERITY_CRITICAL,
                        (
                            f"Dead-letter queue depth for source "
                            f"{h.source}: {h.dlq_depth} (threshold "
                            f"{DLQ_DEPTH_THRESHOLD})"
                        ),
                        value=float(h.dlq_depth),
                        threshold=float(DLQ_DEPTH_THRESHOLD),
                    )
                    fired.append(
                        {
                            "source": h.source,
                            "alert": "dlq_depth_high",
                            "value": h.dlq_depth,
                            "threshold": DLQ_DEPTH_THRESHOLD,
                        }
                    )
            # 4. Latency > threshold.
            if h.last_latency > LATENCY_THRESHOLD:
                if self._should_fire(h.source, "high_latency"):
                    self._fire(
                        h.source,
                        "high_latency",
                        _SEVERITY_WARNING,
                        (
                            f"Ingestion latency for source {h.source}: "
                            f"{h.last_latency:.2f}s (threshold "
                            f"{LATENCY_THRESHOLD:.0f}s)"
                        ),
                        value=h.last_latency,
                        threshold=LATENCY_THRESHOLD,
                    )
                    fired.append(
                        {
                            "source": h.source,
                            "alert": "high_latency",
                            "value": h.last_latency,
                            "threshold": LATENCY_THRESHOLD,
                        }
                    )
            # 5. Source unavailable (circuit breaker open).
            if not h.available:
                if self._should_fire(h.source, "source_unavailable"):
                    self._fire(
                        h.source,
                        "source_unavailable",
                        _SEVERITY_CRITICAL,
                        f"Source {h.source} is unavailable (circuit open)",
                        value=0.0,
                        threshold=0.0,
                    )
                    fired.append(
                        {
                            "source": h.source,
                            "alert": "source_unavailable",
                            "value": 0.0,
                            "threshold": 0.0,
                        }
                    )
        return fired

    def reset_alert_debounce(self, source: str | None = None) -> None:
        """Clear the per-alert debounce map (testing helper).

        With no args, clears every entry; with ``source``, clears
        only that source's entries. Used by tests so a second
        ``check_alerts`` call immediately after the first re-fires
        the alert (rather than waiting for ``ALERT_DEBOUNCE``).
        """
        with self._lock:
            if source is None:
                self._last_alert.clear()
            else:
                self._last_alert = {
                    (s, n): t
                    for (s, n), t in self._last_alert.items()
                    if s != source
                }

    # ── Internals ──────────────────────────────────────────────────────────

    def _should_fire(self, source: str, alert_name: str) -> bool:
        """Return True iff the alert should fire (debounce check).

        Records the current time as the last-fire time so a
        subsequent call within ``ALERT_DEBOUNCE`` returns False.
        MUST be called outside ``self._lock`` (the debounce map is
        mutated here) — or, equivalently, this method takes its
        own lock.
        """
        key = (source, alert_name)
        now = time.time()
        with self._lock:
            last = self._last_alert.get(key, 0.0)
            if (now - last) < ALERT_DEBOUNCE:
                return False
            self._last_alert[key] = now
            return True

    def _fire(
        self,
        source: str,
        name: str,
        severity: str,
        message: str,
        value: float | None = None,
        threshold: float | None = None,
    ) -> None:
        """Fire an alert via ``core.alerting.alert_engine.record_alert``.

        Lazy-imports ``core.alerting`` so the monitor can be imported
        in environments where the alert engine is not yet ready
        (e.g. unit tests). The alert is fire-and-forget — any
        exception is swallowed so an alerting hiccup can never break
        the health monitor's evaluation cycle.
        """
        try:
            from core.alerting import alert_engine

            alert_engine.record_alert(
                name=name,
                category="data",
                severity=severity,
                message=message,
                value=value,
                threshold=threshold,
                metadata={"source": source},
            )
        except Exception as e:  # noqa: BLE001 — alerting must never break callers
            logger.debug(
                "[health] alert fire failed (source=%s alert=%s): %s",
                source,
                name,
                e,
            )

    @staticmethod
    def _format_source(h: SourceHealth) -> dict[str, Any]:
        """Format a ``SourceHealth`` as a JSON-serialisable dict."""
        total = h.events_received
        err_rate = (h.events_failed / total) if total > 0 else 0.0
        return {
            "source": h.source,
            "events_received": h.events_received,
            "events_failed": h.events_failed,
            "error_rate": err_rate,
            "last_event_at": h.last_event_at,
            "last_event_time": h.last_event_time,
            "last_processing_time": h.last_processing_time,
            "last_latency": h.last_latency,
            "last_error": h.last_error,
            "throughput_eps": h.throughput(),
            "dlq_depth": h.dlq_depth,
            "available": h.available,
        }


# ── Module-level singleton ─────────────────────────────────────────────────
# Mirrors the convention used by every sibling ingestion / observability
# module. Importers grab it at module-import time; the constructor allocates
# the in-memory dicts and counters — no I/O at construction time.
ingestion_health_monitor = IngestionHealthMonitor()


__all__ = [
    "IngestionHealthMonitor",
    "SourceHealth",
    "ingestion_health_monitor",
    # Threshold constants (exported so tests / dashboards can read them).
    "NO_DATA_THRESHOLD",
    "ERROR_RATE_THRESHOLD",
    "DLQ_DEPTH_THRESHOLD",
    "LATENCY_THRESHOLD",
    "ALERT_DEBOUNCE",
]
