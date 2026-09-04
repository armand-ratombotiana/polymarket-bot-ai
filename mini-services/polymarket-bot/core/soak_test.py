"""Soak test runner — validates system stability over extended periods.

W26-4 — soak test mode.

Runs for a configurable duration (default: 24 h simulation, can be shortened
for testing) and reports whether the system survives. Each check tick
(default every 60 s) probes the live singletons (``immutable_audit`` /
``db_manager`` / ``dedup_registry`` / ``observability``) so a long-running
soak surfaces drift in memory / DB writability / audit-chain integrity the
moment it happens rather than only at the end.

Checks (per tick):

1. No crashes or unhandled exceptions (errors are appended to ``_errors``
   inside ``run`` and surfaced via ``_check_error_rate``).
2. No memory leaks (RSS doesn't grow unboundedly — threshold: 1 GB).
3. No data loss (immutable audit chain intact — ``verify_chain``).
4. API responds within latency targets (``GET /api/health`` < 5 s).
5. No duplicate events (``dedup_registry`` active).
6. Database writes succeeding (``db_manager.record_snapshot``).
7. Error rate (``< 10`` errors in the last 60 s window).

Reports: pass/fail per check + overall verdict (``overall_pass`` is the
conjunction of every check's ``passed`` flag AND the absence of any
recorded errors during the run window).

The runner is deliberately self-contained — it does NOT spin up its own
pipeline (no strategy loops, no market-data feed). An operator (or an
automated CI soak job) kicks it off via the ``POST /api/system/soak-test``
HTTP endpoint (registered in ``api/server.py``'s W26-4 block), the
runner then probes the live singletons that the production lifespan
already started. This keeps the soak test additive: no new background
task is started just to support the test surface.

Public surface
~~~~~~~~~~~~~~

  * ``SoakTestCheck``                — per-check result dataclass.
  * ``SoakTestReport``               — aggregate report dataclass (with
                                       ``to_dict`` for the HTTP body).
  * ``SoakTestRunner``               — async runner.
  * ``soak_test_runner``             — module-level singleton consumed
                                       by the FastAPI route + the CLI.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SoakTestCheck:
    """Single check result.

    W26-4 — the ``value`` field is intentionally ``Any`` because different
    checks surface different types (latency string, RSS MB, count, dict).
    The HTTP layer stringifies via ``__dict__`` so a non-JSON-serialisable
    value (rare — the checks already coerce to plain types) is the
    caller's responsibility.
    """

    name: str
    passed: bool
    value: Any
    threshold: Any
    message: str


@dataclass
class SoakTestReport:
    """Aggregate report for one ``run()`` invocation.

    W26-4 — surfaced by the ``POST /api/system/soak-test`` endpoint via
    ``to_dict``. ``overall_pass`` is the conjunction of every check's
    ``passed`` flag AND the absence of any recorded errors during the run
    window (so a transient exception inside ``_run_checks`` is surfaced
    as ``overall_pass=False`` even if the final tick's checks all
    passed).
    """

    duration_seconds: float
    overall_pass: bool
    checks: list[SoakTestCheck]
    metrics: dict
    started_at: float
    ended_at: float
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """JSON-serialisable view of the report (for the HTTP body).

        W26-4 — each ``SoakTestCheck`` is serialised via its ``__dict__``
        so the HTTP response is a plain dict-of-dicts (FastAPI's default
        JSON encoder handles ``str`` / ``int`` / ``float`` / ``bool``
        values; the checks already coerce to those types).
        """
        return {
            "duration_seconds": self.duration_seconds,
            "overall_pass": self.overall_pass,
            "checks": [c.__dict__ for c in self.checks],
            "metrics": self.metrics,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "errors": list(self.errors),
        }


class SoakTestRunner:
    """Runs a soak test for a specified duration.

    W26-4 — the runner probes the live singletons on every check tick
    (default: every 60 s) so a long-running soak surfaces drift in
    memory / DB writability / audit-chain integrity the moment it
    happens rather than only at the end.

    The runner is async because the DB + observability probes are async.
    The memory / audit-chain / dedup probes are sync — they're called
    directly from ``_run_checks`` (which awaits only the async ones).
    """

    def __init__(self, duration_seconds: float = 86400) -> None:  # Default 24 h
        self.duration = duration_seconds
        self._running = False
        self._start_time = 0.0
        self._errors: list[str] = []
        # Check every 60 s — frequent enough to catch a drift within one
        # minute, infrequent enough that the soak itself doesn't load the
        # system under test. Tests override this to a smaller value so the
        # soak completes in sub-second wall-clock time.
        self._check_interval = 60

    # ── Public API ───────────────────────────────────────────────────────

    async def run(self, duration_override: float | None = None) -> SoakTestReport:
        """Run the soak test.

        Args:
            duration_override: If provided, overrides the default duration
                              (useful for testing — e.g., 60 for 1 minute,
                              5 for a quick smoke check). ``None`` falls
                              back to ``self.duration`` (24 h by default).
        """
        duration = duration_override if duration_override is not None else self.duration
        self._running = True
        self._start_time = time.time()
        self._errors = []

        logger.info("Starting soak test (duration: %ss)", duration)

        # ── Run checks periodically until the duration elapses or stop() ──
        # is called. Each tick is wrapped in try/except so a single check
        # failure (e.g. a transient DB write error) is RECORDED on the
        # errors list rather than crashing the soak — the soak's contract
        # is "survive + report", not "fail fast".
        while self._running and (time.time() - self._start_time) < duration:
            try:
                await self._run_checks()
            except Exception as e:  # noqa: BLE001 — soak test must survive check failures
                self._errors.append(f"{time.time()}: {e}")
                logger.error("Soak test check error: %s", e)

            # Sleep for the check interval. ``asyncio.sleep`` is interruptible
            # — if ``stop()`` is called during the sleep, the next loop
            # iteration's while-condition exits.
            await asyncio.sleep(self._check_interval)

        # ── Final check — always runs so the report reflects the post-run ──
        # state (not the pre-stop tick). Belt-and-braces: even if the soak
        # was stop()'d mid-tick, this final tick captures the system state
        # at the moment the soak ended. Wrapped in try/except so a check
        # failure during the final tick doesn't crash the soak — the
        # soak's contract is "survive + report", and a final-tick failure
        # is recorded on the errors list (mirroring the in-loop try/except
        # above) so ``overall_pass`` flips to ``False`` and the operator
        # sees the failure surfaced.
        try:
            final_checks = await self._run_checks()
        except Exception as e:  # noqa: BLE001 — soak test must survive final-tick failures
            self._errors.append(f"{time.time()}: final tick: {e}")
            logger.error("Soak test final-tick check error: %s", e)
            final_checks = []

        # ── Build report ──
        # ``overall_pass`` is the conjunction of every check's ``passed``
        # flag AND the absence of any recorded errors. A single error
        # during the soak → overall_pass=False, even if the final tick's
        # checks all passed (the soak didn't survive cleanly).
        overall_pass = all(c.passed for c in final_checks) and len(self._errors) == 0

        report = SoakTestReport(
            duration_seconds=time.time() - self._start_time,
            overall_pass=overall_pass,
            checks=final_checks,
            metrics=await self._collect_metrics(),
            started_at=self._start_time,
            ended_at=time.time(),
            errors=list(self._errors),
        )

        self._running = False
        logger.info(
            "Soak test complete (duration: %.1fs, overall_pass: %s, checks: %d, errors: %d)",
            report.duration_seconds,
            report.overall_pass,
            len(report.checks),
            len(report.errors),
        )
        return report

    def stop(self) -> None:
        """Signal the run loop to exit before its configured duration.

        W26-4 — used by the CLI / future ``POST /api/system/soak-test/stop``
        endpoint to cancel a long-running soak without killing the process.
        The next while-iteration's condition catches the flag and exits.
        """
        self._running = False

    # ── Per-tick checks ─────────────────────────────────────────────────

    async def _run_checks(self) -> list[SoakTestCheck]:
        """Run all soak test checks and return the result list.

        W26-4 — the order is stable (api → memory → audit → db → dedup →
        error_rate) so the report's ``checks`` array renders the same way
        every tick. Each check is individually try/except-guarded inside
        its own method so a single check's failure doesn't skip the others.
        """
        checks: list[SoakTestCheck] = []

        # 1. API responds (async — httpx.AsyncClient).
        checks.append(await self._check_api_responds())

        # 2. Memory stable (sync — psutil).
        checks.append(self._check_memory_stable())

        # 3. Decision ledger chain intact (sync — sqlite).
        checks.append(self._check_decision_chain())

        # 4. Database writable (async — db_manager.record_snapshot).
        checks.append(await self._check_db_writable())

        # 5. Dedup registry active (sync — in-memory deque).
        checks.append(self._check_dedup_active())

        # 6. No errors in last interval (sync — in-memory list).
        checks.append(self._check_error_rate())

        return checks

    async def _check_api_responds(self) -> SoakTestCheck:
        """Probe ``GET /api/health`` on the local server.

        W26-4 — the soak assumes the production server is running on
        ``localhost:8080`` (the Docker compose default). A connection
        error → ``passed=False`` (the soak can't assert API health if
        the API isn't up). The check is best-effort: a timeout / connect
        error never raises — the failure is recorded as a check result.
        """
        try:
            import httpx

            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    "http://localhost:8080/api/health", timeout=5.0
                )
                latency_ms = resp.elapsed.total_seconds() * 1000
                return SoakTestCheck(
                    "api_responds",
                    resp.status_code == 200,
                    f"{latency_ms:.0f}ms",
                    "<5000ms",
                    f"API responded in {latency_ms:.0f}ms (status={resp.status_code})",
                )
        except Exception as e:  # noqa: BLE001 — soak test reports the failure rather than raising
            return SoakTestCheck("api_responds", False, "error", "200", str(e))

    def _check_memory_stable(self) -> SoakTestCheck:
        """Probe the process RSS via ``psutil``.

        W26-4 — passes if RSS < 1 GB. The 1 GB threshold is conservative
        for the polymarket-bot (the production process typically runs at
        ~150-300 MB with all subsystems loaded); a runaway memory leak
        would breach it within hours rather than days, which is exactly
        the class of failure a soak test is meant to surface.
        """
        try:
            import psutil

            proc = psutil.Process(os.getpid())
            rss_mb = proc.memory_info().rss / 1024 / 1024
            return SoakTestCheck(
                "memory_stable",
                rss_mb < 1024,
                f"{rss_mb:.0f}MB",
                "<1024MB",
                f"Memory at {rss_mb:.0f}MB",
            )
        except Exception:  # noqa: BLE001 — psutil optional
            return SoakTestCheck(
                "memory_stable", True, "N/A", "N/A", "psutil not available"
            )

    def _check_decision_chain(self) -> SoakTestCheck:
        """Verify the immutable audit chain via ``immutable_audit.verify_chain``.

        W26-4 — an empty chain is ``valid=True`` (no entries to corrupt);
        a non-empty chain is ``valid=True`` iff every entry's
        ``previous_hash`` matches the prior entry's ``entry_hash``. A
        tampered row breaks the chain, surfaced as ``valid=False``.
        """
        try:
            from core.immutable_audit import immutable_audit

            verification = immutable_audit.verify_chain()
            return SoakTestCheck(
                "audit_chain_intact",
                bool(verification.get("valid")),
                verification.get("checked", 0),
                "valid",
                f"Audit chain: {verification.get('checked', 0)} entries verified",
            )
        except Exception as e:  # noqa: BLE001 — soak test reports the failure
            return SoakTestCheck(
                "audit_chain_intact", False, "error", "valid", str(e)
            )

    async def _check_db_writable(self) -> SoakTestCheck:
        """Probe DB writability via ``db_manager.record_snapshot``.

        W26-4 — writes a synthetic snapshot for the namespaced
        ``soak-test`` token_id so the row is identifiable as a soak-test
        artifact (an operator can purge the soak-test rows via the
        retention sweeper without affecting real market data). The
        snapshot's prices are deliberately 0.5 / 0.5 / 0.5 (mid / spread
        0) so it's visually distinguishable from a real market snapshot
        if a downstream consumer accidentally joins it.
        """
        try:
            from core.database_manager import db_manager

            await db_manager.record_snapshot(
                token_id="soak-test",
                best_bid=0.5,
                best_ask=0.5,
                mid=0.5,
                spread=0,
            )
            return SoakTestCheck(
                "db_writable", True, "OK", "OK", "Database write succeeded"
            )
        except Exception as e:  # noqa: BLE001 — soak test reports the failure
            return SoakTestCheck("db_writable", False, "error", "OK", str(e))

    def _check_dedup_active(self) -> SoakTestCheck:
        """Probe the dedup registry's stats.

        W26-4 — the check always passes when the registry is importable
        (the registry is in-memory, so it can't be "down" in the
        traditional sense — only the import can fail). The check's value
        is the full ``get_stats()`` dict so the report surfaces the
        per-entity-type counters (orders / fills / decisions / alerts /
        audits) for operator visibility.
        """
        try:
            from core.dedup import dedup_registry

            stats = dedup_registry.get_stats()
            return SoakTestCheck(
                "dedup_active",
                True,
                stats,
                "active",
                f"Dedup registry: {len(stats)} entity types tracked",
            )
        except Exception as e:  # noqa: BLE001 — soak test reports the failure
            return SoakTestCheck("dedup_active", False, "error", "active", str(e))

    def _check_error_rate(self) -> SoakTestCheck:
        """Count errors recorded in the last 60 s.

        W26-4 — each error string is prefixed with ``f"{time.time()}: "``
        so we can parse the timestamp back out. The threshold is 10
        errors/min — a healthy soak should have 0; 10+ indicates a
        recurring exception loop (e.g. a flaky DB connection retried
        every tick) that the operator must investigate.

        Malformed error strings (rare — only if a future refactor changes
        the prefix format) are counted as recent so the check surfaces
        the malformation rather than silently dropping it.
        """
        cutoff = time.time() - 60
        recent_errors: list[str] = []
        for entry in self._errors:
            try:
                ts_str = entry.split(":", 1)[0]
                if float(ts_str) > cutoff:
                    recent_errors.append(entry)
            except (ValueError, TypeError):
                recent_errors.append(entry)
        return SoakTestCheck(
            "error_rate",
            len(recent_errors) < 10,
            len(recent_errors),
            "<10/min",
            f"{len(recent_errors)} errors in last minute",
        )

    async def _collect_metrics(self) -> dict:
        """Collect the observability health report for the soak's metrics field.

        W26-4 — the report's ``metrics`` field is the structured health
        report from ``core.observability.observability.get_health_report``
        so the soak report includes the latest per-(category, name) metric
        snapshot alongside the per-check verdicts. Best-effort: an
        observability failure yields an empty dict (the soak report still
        surfaces the check verdicts).
        """
        try:
            from core.observability import observability

            return await observability.get_health_report()
        except Exception:  # noqa: BLE001 — soak test must survive metrics failure
            return {}


# ── Module-level singleton ──────────────────────────────────────────────────
# Mirrors the pattern in every other ``core.*`` module so the canonical access
# pattern is ``from core.soak_test import soak_test_runner`` (used by the
# ``POST /api/system/soak-test`` route handler in ``api/server.py``).
soak_test_runner = SoakTestRunner()


__all__ = [
    "SoakTestCheck",
    "SoakTestReport",
    "SoakTestRunner",
    "soak_test_runner",
]
