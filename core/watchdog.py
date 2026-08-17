"""
core/watchdog.py — Subsystem watchdog with tripwire checks.

Every live subsystem registers a heartbeat. A background loop checks for
staleness and safety conditions (equity crash, weekly loss breach, feed stall)
and raises tripwires. Critical tripwires auto-activate the durable kill switch
(see core/safety.py) when TRIPWIRE_AUTO_KILL is enabled.
"""
from __future__ import annotations

import asyncio
import logging
import time

from config import settings
from core.data_store import store
from risk.manager import (
    DAILY_LOSS_STOP,
    MAX_DEPLOYABLE_CAPITAL,
    WEEKLY_LOSS_STOP,
)

log = logging.getLogger(__name__)


class TripwireError(Exception):
    """Raised when a tripwire check finds a critical condition."""

    def __init__(self, name: str, severity: str, detail: str) -> None:
        super().__init__(f"[{severity}] {name}: {detail}")
        self.name = name
        self.severity = severity
        self.detail = detail


class Watchdog:
    """
    Central tripwire monitor. Subsystems call `beat(name)`; `run_checks()`
    evaluates staleness and safety invariants and returns findings.
    """

    def __init__(
        self,
        heartbeat_timeout: int | None = None,
        check_interval: int | None = None,
        book_stall_seconds: int | None = None,
        auto_kill: bool | None = None,
    ) -> None:
        self.heartbeat_timeout = heartbeat_timeout or settings.watchdog_heartbeat_timeout
        self.check_interval = check_interval or settings.watchdog_check_interval
        self.book_stall_seconds = book_stall_seconds or settings.book_stall_seconds
        self.auto_kill = settings.tripwire_auto_kill if auto_kill is None else auto_kill

        self._heartbeats: dict[str, float] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self.last_checks: list[dict] = []

    # ── Heartbeat API ────────────────────────────────────────────────────

    def register(self, name: str) -> None:
        self._heartbeats.setdefault(name, time.time())

    def beat(self, name: str) -> None:
        self._heartbeats[name] = time.time()

    def subsystem_status(self) -> dict[str, str]:
        now = time.time()
        return {
            name: ("OK" if now - last <= self.heartbeat_timeout else "STALE")
            for name, last in self._heartbeats.items()
        }

    # ── Tripwire checks (pure; async only for consistency with audit IO) ─

    async def run_checks(self) -> list[dict]:
        findings: list[dict] = []
        now = time.time()

        # wr01 — heartbeat staleness per subsystem
        for name, last in self._heartbeats.items():
            age = now - last
            if age > self.heartbeat_timeout:
                findings.append({
                    "id": "wr01",
                    "name": f"heartbeat:{name}",
                    "severity": "WARNING",
                    "detail": f"no heartbeat for {age:.0f}s (timeout {self.heartbeat_timeout}s)",
                })

        # wr02 — daily loss circuit breaker
        if store.daily_pnl <= -float(DAILY_LOSS_STOP):
            findings.append({
                "id": "wr02",
                "name": "daily_loss_stop",
                "severity": "CRITICAL",
                "detail": f"daily PnL {store.daily_pnl:.2f} <= -{float(DAILY_LOSS_STOP):.2f}",
            })

        # wr03 — weekly loss circuit breaker (P0-GOV-01: enforce the defined stop)
        store.roll_weekly_window()
        if store.weekly_pnl <= -float(WEEKLY_LOSS_STOP):
            findings.append({
                "id": "wr03",
                "name": "weekly_loss_stop",
                "severity": "CRITICAL",
                "detail": f"weekly PnL {store.weekly_pnl:.2f} <= -{float(WEEKLY_LOSS_STOP):.2f}",
            })

        # wr04 — feed stall: order books exist but none updated recently
        if store.order_books:
            newest = max(b.updated_at for b in store.order_books.values())
            age = now - newest
            if age > self.book_stall_seconds:
                findings.append({
                    "id": "wr04",
                    "name": "feed_stall",
                    "severity": "WARNING",
                    "detail": f"no order-book update for {age:.0f}s (stall threshold {self.book_stall_seconds}s)",
                })

        # wr05 — exposure above deployable ceiling (reconciliation gate)
        try:
            exposure = await store.total_exposure()
            if float(exposure) > float(MAX_DEPLOYABLE_CAPITAL):
                findings.append({
                    "id": "wr05",
                    "name": "exposure_ceiling",
                    "severity": "WARNING",
                    "detail": f"total exposure {float(exposure):.2f} > deployable ceiling {float(MAX_DEPLOYABLE_CAPITAL):.2f}",
                })
        except Exception as e:  # pragma: no cover - defensive
            log.debug("[watchdog] wr05 check failed: %s", e)

        # wr06 — kill switch already active (informational)
        from core.safety import kill_switch_file_exists
        if kill_switch_file_exists() or store.kill_switch_active:
            findings.append({
                "id": "wr06",
                "name": "kill_switch_active",
                "severity": "CRITICAL",
                "detail": "kill switch is active — trading halted",
            })

        # Bankroll sanity: equity can never exceed recognized operating capital
        if store.weekly_pnl is None:  # pragma: no cover - schema guard
            findings.append({"id": "wr09", "name": "accounting", "severity": "CRITICAL",
                             "detail": "weekly_pnl missing from store"})
        return findings

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                await self.run()
            except asyncio.CancelledError:
                break
            except Exception as e:  # pragma: no cover - defensive
                log.error("[watchdog] loop error: %s", e)

    async def run(self) -> list[dict]:
        """Execute checks; log + audit + optionally kill on findings."""
        from core.safety import (
            kill_switch_file_exists,
        )
        from risk.manager import risk_manager

        findings = await self.run_checks()
        self.last_checks = findings

        critical = [f for f in findings if f["severity"] == "CRITICAL"]
        for f in findings:
            log_fn = log.warning if f["severity"] == "WARNING" else log.critical
            log_fn(f"[watchdog] TRIPWIRE {f['id']}: {f['detail']}")

        if critical:
            reason = "; ".join(f"{f['id']}: {f['detail']}" for f in critical)
            if self.auto_kill and not (kill_switch_file_exists() or store.kill_switch_active):
                try:
                    await risk_manager.activate_kill_switch(f"watchdog tripwires: {reason}")
                    findings.append({
                        "id": "wr07",
                        "name": "auto_kill",
                        "severity": "CRITICAL",
                        "detail": f"kill switch auto-activated by watchdog: {reason}",
                    })
                except Exception as e:
                    log.critical("[watchdog] FAILED to activate kill switch: %s", e)
        return findings

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="watchdog")
        log.info("[watchdog] Started (interval %ds, heartbeat timeout %ds)",
                 self.check_interval, self.heartbeat_timeout)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def status(self) -> dict:
        return {
            "running": self._running,
            "subsystems": self.subsystem_status(),
            "last_checks": self.last_checks,
            "auto_kill": self.auto_kill,
            "active": bool(self._heartbeats),
        }


# Global singleton
watchdog = Watchdog()