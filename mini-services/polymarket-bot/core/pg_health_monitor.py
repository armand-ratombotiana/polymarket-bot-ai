"""PostgreSQL health monitor — checks connection health and triggers fallback.

W21-2 — background-task-driven PG health checker. Runs an asyncio task
that:

1. Pings PostgreSQL every ``check_interval`` seconds (default 15 s).
2. If a ping fails ``failure_threshold`` consecutive times → marks PG as
   unhealthy (signals the database manager to fall back to SQLite).
3. While unhealthy, ping cadence slows to ``recovery_interval`` seconds
   (default 60 s) so a transiently-down PG instance isn't hammered
   with connection attempts.
4. If PG recovers (``recovery_success_threshold`` consecutive successes
   by default 2), marks PG as healthy again (signals the database
   manager to switch back to PG).
5. Records metrics about PG health (uptime %, avg latency, total
   checks / failures) for the ``/api/database/pg-health`` dashboard
   endpoint.
6. Emits Prometheus gauges (``polymarket_pg_health_status``,
   ``polymarket_pg_health_latency_ms``,
   ``polymarket_pg_health_consecutive_failures``) on every check so a
   Grafana panel can alert on the health signal without polling the
   HTTP endpoint.

Design notes
------------
* **Singleton ``pg_health_monitor``** — the module-level instance is
  imported by ``core.database_manager`` (the W21-1 wiring layer) and
  by ``api/server.py`` (the API endpoints). Constructing a fresh
  monitor per consumer would spawn duplicate background tasks against
  the same PG instance — the singleton contract keeps the polling
  cadence deterministic.
* **Import-safe.** Importing this module does NOT start the background
  task; ``start()`` is invoked explicitly by the FastAPI lifespan
  startup handler (mirrors the ``book_poller.start()`` /
  ``paper_sim.start()`` pattern in ``api/server.py::lifespan``).
* **Fail-soft.** Every ping is wrapped in ``asyncio.wait_for(...,
  timeout=3.0)``; a hung connection does not stall the monitor loop.
  Ping failures are recorded as ``HealthCheck(healthy=False)`` and
  the loop continues — the monitor never raises into the lifespan
  task. (Mirrors the ``circuit_breaker`` defensive contract.)
* **Test-friendly.** ``_check_health`` delegates the actual ``SELECT 1``
  ping to ``_ping_postgres``, which can be monkeypatched in tests to
  inject synthetic success / failure without standing up a real PG
  instance.
* **Prometheus emission is best-effort.** Any error in the prometheus
  client call path is swallowed at the call site (matches the
  ``core.prometheus_metrics.record_request`` defensive contract — a
  metrics pipeline hiccup can never break a health check).

Public surface
--------------
``pg_health_monitor`` — module-level singleton instance.
``PGHealthMonitor`` — class (advanced callers can instantiate their own).
``HealthCheck`` / ``PGHealthStatus`` — dataclasses (status DTOs).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ── Status DTOs ────────────────────────────────────────────────────────────

@dataclass
class HealthCheck:
    """One ping attempt's outcome (timestamped)."""

    timestamp: float
    healthy: bool
    latency_ms: float
    error: str = ""

    def to_dict(self) -> dict:
        """Plain-dict view (JSON-able for the API response)."""
        return {
            "timestamp": self.timestamp,
            "healthy": self.healthy,
            "latency_ms": round(self.latency_ms, 3),
            "error": self.error,
        }


@dataclass
class PGHealthStatus:
    """Roll-up state derived from the last N health checks.

    ``consecutive_failures`` / ``consecutive_successes`` are the
    state-machine counters that drive the unhealthy / healthy
    transitions in ``_record_check``; ``total_checks`` /
    ``total_failures`` drive the lifetime ``uptime_pct`` so a Grafana
    panel can correlate a degraded window with the overall uptime
    SLO (99.5 % over 24 h etc.).
    """

    is_healthy: bool = False
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_check: Optional[HealthCheck] = None
    checks: list[HealthCheck] = field(default_factory=list)  # last 100
    total_checks: int = 0
    total_failures: int = 0
    uptime_pct: float = 0.0
    avg_latency_ms: float = 0.0

    def to_dict(self) -> dict:
        """Plain-dict view (JSON-able for the API response).

        The ``checks`` list is rendered via ``HealthCheck.to_dict`` so
        the historical samples are JSON-able too (the dataclass
        ``__dict__`` would not expose the nested fields cleanly to a
        JSON serialiser without a custom encoder).
        """
        return {
            "is_healthy": self.is_healthy,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_successes": self.consecutive_successes,
            "last_check": self.last_check.to_dict() if self.last_check else None,
            "checks": [c.to_dict() for c in self.checks],
            "total_checks": self.total_checks,
            "total_failures": self.total_failures,
            "uptime_pct": round(self.uptime_pct, 3),
            "avg_latency_ms": round(self.avg_latency_ms, 3),
        }


# ── Monitor ─────────────────────────────────────────────────────────────────

# Default success threshold before a recovering PG is re-marked healthy
# (matches the ``CircuitBreakerConfig.success_threshold=2`` convention in
# ``core/circuit_breaker.py`` so a single flapping success doesn't bounce
# the backend flag).
_DEFAULT_RECOVERY_SUCCESS_THRESHOLD = 2


class PGHealthMonitor:
    """Monitors PostgreSQL connection health.

    The monitor runs a single asyncio background task that pings PG
    every ``check_interval`` seconds when healthy, every
    ``recovery_interval`` seconds when unhealthy (slower cadence so a
    down PG isn't hammered). State transitions are logged at INFO /
    WARNING level so the operator sees them in the server log.
    """

    def __init__(
        self,
        check_interval: float = 15.0,
        failure_threshold: int = 3,
        recovery_interval: float = 60.0,
        recovery_success_threshold: int = _DEFAULT_RECOVERY_SUCCESS_THRESHOLD,
        ping_timeout: float = 3.0,
        database_url: Optional[str] = None,
    ) -> None:
        self.check_interval = check_interval
        self.failure_threshold = failure_threshold
        self.recovery_interval = recovery_interval
        self.recovery_success_threshold = recovery_success_threshold
        self.ping_timeout = ping_timeout
        # Allow tests / operators to override the URL via constructor
        # (defaults to the ``DATABASE_URL`` env var, then the local
        # docker-compose default — mirrors ``core.timescale_db.DB_URL``).
        self._database_url = database_url
        self._status = PGHealthStatus()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background monitor task (idempotent)."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(
            "PG health monitor started (check every %.1fs, failure_threshold=%d, "
            "recovery_interval=%.1fs)",
            self.check_interval, self.failure_threshold, self.recovery_interval,
        )

    async def stop(self) -> None:
        """Cancel the background monitor task (idempotent)."""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PG health monitor stopped")

    # ── Monitor loop ──────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Main monitoring loop.

        Pings PG on each iteration; uses ``check_interval`` while
        healthy (frequent — surface a flapping instance ASAP) and
        ``recovery_interval`` while unhealthy (slow — give the
        recovering PG some breathing room before the next probe).
        """
        while self._running:
            try:
                await self._check_health()
            except Exception as e:  # pragma: no cover — defensive
                # The check itself swallows its own errors; this guard
                # catches unexpected exceptions in the recording path
                # so a bug in ``_record_check`` can never kill the
                # monitor loop.
                logger.error("PG health check error: %s", e)

            interval = (
                self.check_interval
                if self._status.is_healthy
                else self.recovery_interval
            )
            await asyncio.sleep(interval)

    async def _check_health(self) -> HealthCheck:
        """Perform a single health check and record the result.

        Returns the ``HealthCheck`` (also accessible via
        ``self._status.last_check``). The actual ``SELECT 1`` ping is
        delegated to ``_ping_postgres`` so tests can monkeypatch that
        one method without re-implementing the recording / state-machine
        logic.
        """
        check = HealthCheck(timestamp=time.time(), healthy=False, latency_ms=0.0)
        try:
            latency_ms = await self._ping_postgres()
            check.healthy = True
            check.latency_ms = latency_ms
        except Exception as e:
            check.healthy = False
            check.error = str(e)[:200]

        self._record_check(check)
        self._emit_prometheus_metrics(check)
        return check

    async def _ping_postgres(self) -> float:
        """Ping PostgreSQL by opening a connection + ``SELECT 1``.

        Returns the round-trip latency in milliseconds. Raises on any
        connection / query error so the caller can record the failure
        outcome. Wrapped in ``asyncio.wait_for`` so a hung TCP connect
        cannot stall the monitor loop indefinitely.

        The URL defaults to the ``DATABASE_URL`` env var; if unset,
        falls back to the local docker-compose default (mirrors
        ``core.timescale_db.DB_URL``).
        """
        import asyncpg  # local import — keeps the module import-safe even
                        # if asyncpg is not installed (the singleton is
                        # constructed at module import time, but the
                        # background task only runs in the live server).

        database_url = self._database_url or os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:polymarket_secret@localhost:5432/polymarket",
        )

        start = time.time()
        conn = await asyncio.wait_for(asyncpg.connect(database_url), timeout=self.ping_timeout)
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
        return (time.time() - start) * 1000.0

    # ── State machine ────────────────────────────────────────────────────

    def _record_check(self, check: HealthCheck) -> None:
        """Record a health check result + update roll-up state.

        State transitions:
          * healthy → unhealthy: after ``failure_threshold``
            consecutive failures.
          * unhealthy → healthy: after ``recovery_success_threshold``
            consecutive successes.
          * healthy → healthy: reset failure counter (so a single
            success restarts the failure window).

        ``uptime_pct`` and ``avg_latency_ms`` are recomputed on every
        check so the dashboard reads consistent values immediately
        after a state flip (no async refresh needed).
        """
        self._status.last_check = check
        self._status.checks.append(check)
        if len(self._status.checks) > 100:
            # Trim to the last 100 samples so long-running bots don't
            # accumulate an unbounded history list (mirrors the
            # ``order_history`` / ``event_log`` cap pattern in
            # ``core.data_store``).
            self._status.checks = self._status.checks[-100:]

        self._status.total_checks += 1

        if check.healthy:
            self._status.consecutive_failures = 0
            self._status.consecutive_successes += 1

            # Mark healthy after N consecutive successes (default 2 —
            # avoids bouncing back to PG on a single flapping success).
            if (
                self._status.consecutive_successes >= self.recovery_success_threshold
                and not self._status.is_healthy
            ):
                self._status.is_healthy = True
                logger.info(
                    "PostgreSQL healthy (latency: %.1fms)",
                    check.latency_ms,
                )
        else:
            self._status.consecutive_successes = 0
            self._status.consecutive_failures += 1
            self._status.total_failures += 1

            # Mark unhealthy after threshold consecutive failures
            # (default 3 — a single flapping failure shouldn't trigger
            # an immediate SQLite fallback).
            if (
                self._status.consecutive_failures >= self.failure_threshold
                and self._status.is_healthy
            ):
                self._status.is_healthy = False
                logger.warning(
                    "PostgreSQL unhealthy after %d failures: %s",
                    self._status.consecutive_failures,
                    check.error,
                )

        # Compute uptime percentage over the lifetime of the monitor
        # (total_checks - total_failures) / total_checks. Excludes the
        # bootstrap window where total_checks == 0 (avoids 0/0).
        if self._status.total_checks > 0:
            healthy_checks = self._status.total_checks - self._status.total_failures
            self._status.uptime_pct = (healthy_checks / self._status.total_checks) * 100.0

        # Compute average latency from the last 100 healthy checks (so
        # the dashboard reports a stable latency that isn't perturbed
        # by the initial connect-then-fail window).
        healthy = [c for c in self._status.checks if c.healthy]
        if healthy:
            self._status.avg_latency_ms = sum(c.latency_ms for c in healthy) / len(healthy)

    # ── Prometheus emission ──────────────────────────────────────────────

    def _emit_prometheus_metrics(self, check: HealthCheck) -> None:
        """Emit Prometheus gauges for the latest health check.

        Best-effort: any error in the prometheus client call path is
        swallowed so a metrics pipeline hiccup can never break the
        health check (mirrors the defensive pattern in
        ``core.prometheus_metrics.record_request``). The actual metric
        singletons are defined in ``core.prometheus_metrics`` — this
        method is the only emitter so the gauge labels stay consistent
        across the codebase.
        """
        try:
            from core.prometheus_metrics import (
                db_size_bytes,
                pg_health_consecutive_failures,
                pg_health_latency_ms,
                pg_health_status,
            )

            # 1.0 = healthy, 0.0 = unhealthy. Mirrors the standard
            # ``up`` metric convention Prometheus scrapers expect.
            pg_health_status.set(1.0 if check.healthy else 0.0)
            pg_health_latency_ms.set(check.latency_ms if check.healthy else 0.0)
            pg_health_consecutive_failures.set(self._status.consecutive_failures)

            # Reuse the existing ``db_size_bytes`` gauge to surface the
            # ACTIVE backend — PG gets the value when healthy, SQLite
            # gets the value when PG is unhealthy (the task spec asks
            # for this dual-label emission so a Grafana panel can
            # correlate backend-active with the underlying file size).
            # The PG DB size is NOT fetched here (would require an
            # extra ``SELECT pg_database_size(...)`` round-trip on every
            # check); we emit a placeholder ``-1`` so the gauge series
            # exists even when PG is down.
            if check.healthy:
                db_size_bytes.labels(db_name="postgres").set(-1.0)
            else:
                # Surface the SQLite fallback file size so the operator
                # sees the active store growing. Mirrors the
                # ``timescale_db.get_stats()`` size_mb field.
                try:
                    from pathlib import Path

                    sqlite_path = Path(
                        os.environ.get(
                            "MARKET_DB_PATH",
                            "/app/data/market_intelligence.db",
                        )
                    )
                    if sqlite_path.exists():
                        db_size_bytes.labels(db_name="sqlite").set(
                            sqlite_path.stat().st_size
                        )
                except Exception:  # pragma: no cover — defensive
                    pass
        except Exception:  # pragma: no cover — metrics must never break a check
            logger.debug("[pg_health] prometheus emission failed", exc_info=True)

    # ── Public introspection ─────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return the current health status as a JSON-able dict."""
        return self._status.to_dict()

    def is_healthy(self) -> bool:
        """Return ``True`` iff PG is currently marked healthy.

        The database manager consults this flag to decide whether to
        route writes to PG or SQLite (the W21-1 wiring layer is the
        consumer; ``core.database_manager.DatabaseManager._pg_retry_loop``
        polls this method on every retry tick).
        """
        return self._status.is_healthy


# ── Module-level singleton ──────────────────────────────────────────────────
#
# Constructed at import time (cheap — no I/O, no background task). The
# background task is started explicitly via ``await pg_health_monitor.start()``
# from the FastAPI lifespan startup handler so the monitor does NOT run
# during test imports / CLI invocations / etc.
pg_health_monitor = PGHealthMonitor()


__all__ = [
    "HealthCheck",
    "PGHealthStatus",
    "PGHealthMonitor",
    "pg_health_monitor",
]
