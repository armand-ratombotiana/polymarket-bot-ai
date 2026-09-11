"""Strategy health monitor — auto-disables strategies that fail validation.

W24-8 — out-of-sample / risk-limit failure → automatic strategy disable.

Checks (every 5 minutes):
1. Win rate below threshold (e.g., <30% over last 20 trades)
2. Expectancy negative over last 20 trades
3. Max drawdown exceeded for the strategy
4. No trades in last 24 hours (strategy may be broken)
5. Error rate too high (exceptions per hour)

When a strategy fails, it's automatically disabled (via
``strategies.registry.StrategyRegistry.disable``) and an alert is fired
(via ``core.alerting.AlertEngine.record_alert``). The auto-disable is
the load-bearing safety primitive: a strategy that fails out-of-sample
validation CANNOT keep trading live capital — the monitor's whole
purpose is to flip the strategy to ``StrategyHealthStatus.DISABLED``
and prevent ``start_strategy`` from silently restarting it.

The monitor is SYNC (no ``async``) so it can be invoked from either
the periodic ``check_strategy`` sweep (scheduled by the main loop's
``asyncio.to_thread``) or directly from a sync test/CLI. The
``disable`` call on the registry is itself sync (it cancels the
strategy's task without awaiting — see
``strategies.registry.StrategyRegistry.disable``).

Schema (in-memory only — the canonical record of WHY a strategy was
disabled lives on the ``StrategyHealth`` dataclass here AND on the
durable alert row in the alerts SQLite store fired by ``record_alert``)::

    StrategyHealth (
        strategy_name     str
        status            StrategyHealthStatus   — HEALTHY / DEGRADED /
                                                  DISABLED / INACTIVE
        win_rate          float                  — wins / total trades
        expectancy        float                  — mean P&L per trade
        max_drawdown      float                  — peak-to-trough / peak
        n_trades          int
        n_errors          int                    — exceptions in last hour
        last_trade_time   float                  — epoch seconds
        last_check        float                  — epoch seconds
        disable_reason    str
        disable_time      float                  — epoch seconds (0 if not disabled)
    )
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


class StrategyHealthStatus(Enum):
    """W24-8 — four-state lifecycle for a monitored strategy.

    The enum intentionally distinguishes ``INACTIVE`` (never seen by the
    monitor — e.g. a PLANNED catalog entry with no trading loop) from
    ``DEGRADED`` (the monitor HAS seen the strategy but its metrics are
    below the eval threshold OR it's gone stale). ``DISABLED`` is the
    terminal state the monitor drives a strategy to when its risk limits
    are breached; once disabled, the only path back to ``HEALTHY`` is
    for the operator to explicitly ``enable()`` the strategy in the
    registry (after which the next ``check_strategy`` call refreshes
    the metrics).
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"  # Some metrics below threshold
    DISABLED = "disabled"  # Auto-disabled due to failure
    INACTIVE = "inactive"  # Not running


@dataclass
class StrategyHealth:
    """Per-strategy health snapshot.

    W24-8 — held in ``StrategyHealthMonitor._health`` keyed by
    ``strategy_name``. The dict is the canonical in-memory record;
    durable evidence of a disable lives in the alerts SQLite store
    (fired via ``alert_engine.record_alert``). The dataclass fields
    double as the JSON-serialisable payload returned by
    ``GET /api/strategies/health`` (via ``__dict__``).
    """

    strategy_name: str
    status: StrategyHealthStatus = StrategyHealthStatus.INACTIVE
    win_rate: float = 0.0
    expectancy: float = 0.0
    max_drawdown: float = 0.0
    n_trades: int = 0
    n_errors: int = 0
    last_trade_time: float = 0.0
    last_check: float = 0.0
    disable_reason: str = ""
    disable_time: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view of this health snapshot.

        ``__dict__`` would surface ``status`` as a ``StrategyHealthStatus``
        enum which FastAPI's default JSON encoder can't serialise; this
        helper stringifies the enum so ``GET /api/strategies/health``
        returns a plain dict.
        """
        return {
            "strategy_name": self.strategy_name,
            "status": self.status.value,
            "win_rate": self.win_rate,
            "expectancy": self.expectancy,
            "max_drawdown": self.max_drawdown,
            "n_trades": self.n_trades,
            "n_errors": self.n_errors,
            "last_trade_time": self.last_trade_time,
            "last_check": self.last_check,
            "disable_reason": self.disable_reason,
            "disable_time": self.disable_time,
        }


class StrategyHealthMonitor:
    """Monitors strategy health and auto-disables failing strategies.

    W24-8 — the monitor is invoked periodically (the spec calls for
    every 5 minutes; the caller schedules the cadence via
    ``asyncio.to_thread`` from the main loop, mirroring the existing
    ``live_risk_metrics`` / ``attribution`` periodic sweep pattern).
    Each invocation calls ``check_strategy`` per strategy with the
    last N closed trades + the per-strategy error count.

    The trade list is supplied by the CALLER — typically sourced from
    ``core.closed_positions.ClosedPositions.get_recent(strategy=...)``
    — so the monitor stays decoupled from the persistence layer (a
    test can construct a synthetic trade list directly without
    spinning up SQLite).
    """

    def __init__(self) -> None:
        self._health: dict[str, StrategyHealth] = {}
        self._thresholds: dict[str, float] = {
            "min_win_rate": 0.30,  # 30% minimum win rate
            "min_expectancy": -0.05,  # Max -$0.05 per-trade loss
            "max_drawdown": 0.15,  # 15% max drawdown
            "min_trades_for_eval": 10,  # Need ≥10 trades to evaluate
            "max_errors_per_hour": 10,
            "stale_strategy_hours": 24,  # No trades in 24h = stale
        }

    # ── Public API ───────────────────────────────────────────────────

    def check_strategy(
        self,
        strategy_name: str,
        trades: list[dict],
        errors: int = 0,
    ) -> StrategyHealth:
        """Check a strategy's health and auto-disable if needed.

        W24-8 — computes the four headline metrics (win rate /
        expectancy / max drawdown / error rate) from the supplied
        trade list, compares against the configured thresholds, and
        drives the strategy to ``StrategyHealthStatus.DISABLED`` +
        calls ``StrategyRegistry.disable`` if any threshold is
        breached.

        ``trades`` is a list of dicts each carrying at least a ``pnl``
        key (float) and optionally a ``closed_at`` key (epoch seconds).
        Missing keys default to ``0`` so a minimal
        ``[{"pnl": 0.10}, {"pnl": -0.05}, ...]`` list works.

        Once disabled, the strategy's status stays at ``DISABLED`` —
        subsequent ``check_strategy`` calls refresh the metrics but do
        NOT clear the disabled state. The operator must explicitly
        ``strategy_registry.enable(strategy_name)`` to clear the flag,
        after which the next ``check_strategy`` call will re-evaluate.
        """
        # ── Load (or create) the per-strategy health snapshot. ──────
        health = self._health.get(
            strategy_name,
            StrategyHealth(strategy_name=strategy_name),
        )
        health.last_check = time.time()

        # ── Once disabled, stay disabled — operator must enable(). ──
        # Metrics are still refreshed so the dashboard can render the
        # final values, but the status flag is preserved.
        was_disabled = health.status == StrategyHealthStatus.DISABLED

        if not trades:
            # No trades ever observed for this strategy — mark inactive
            # (unless it was previously disabled, in which case we
            # preserve the DISABLED flag so an operator querying the
            # dashboard sees the terminal state).
            if not was_disabled:
                health.status = StrategyHealthStatus.INACTIVE
            self._health[strategy_name] = health
            return health

        # ── Compute metrics from the trade list. ────────────────────
        pnls = [float(t.get("pnl", 0) or 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        health.n_trades = len(pnls)
        health.win_rate = (len(wins) / len(pnls)) if pnls else 0.0
        health.expectancy = (sum(pnls) / len(pnls)) if pnls else 0.0
        health.n_errors = errors

        # ── Max drawdown on the cumulative-PnL equity curve. ────────
        # W24-8 — proper peak-to-trough drawdown: track the running
        # peak (not the global peak) so a monotonically-increasing
        # equity curve yields ``max_drawdown = 0`` (not ``1.0`` as the
        # naive ``max((peak - e) / peak for e in equity)`` would). The
        # naive approach confuses "starting from 0" with "drawdown",
        # flagging every profitable strategy with drawdown = 100%.
        # The running-peak approach is the standard finance definition:
        # for every equity point, find the highest peak observed SO
        # FAR; the drawdown at that point is ``(peak_so_far - e) /
        # peak_so_far`` (or 0 if peak_so_far <= 0).
        equity: list[float] = [0.0]
        for p in pnls:
            equity.append(equity[-1] + p)
        running_peak = equity[0] if equity else 0.0
        max_dd = 0.0
        for e in equity:
            if e > running_peak:
                running_peak = e
            if running_peak > 0:
                dd = (running_peak - e) / running_peak
                if dd > max_dd:
                    max_dd = dd
        health.max_drawdown = max_dd

        # ── Last trade time (for staleness check). ──────────────────
        timestamps = [
            float(t.get("closed_at", 0) or 0) for t in trades if t.get("closed_at")
        ]
        health.last_trade_time = max(timestamps) if timestamps else 0.0

        # ── Threshold checks (only if we have enough trades). ──────
        if health.n_trades >= int(self._thresholds["min_trades_for_eval"]):
            if health.win_rate < self._thresholds["min_win_rate"]:
                self._disable(
                    strategy_name,
                    health,
                    f"Win rate {health.win_rate:.1%} below "
                    f"{self._thresholds['min_win_rate']:.0%}",
                )
                health.status = StrategyHealthStatus.DISABLED
            elif health.expectancy < self._thresholds["min_expectancy"]:
                self._disable(
                    strategy_name,
                    health,
                    f"Expectancy ${health.expectancy:.4f} below "
                    f"${self._thresholds['min_expectancy']:.4f}",
                )
                health.status = StrategyHealthStatus.DISABLED
            elif health.max_drawdown > self._thresholds["max_drawdown"]:
                self._disable(
                    strategy_name,
                    health,
                    f"Drawdown {health.max_drawdown:.1%} above "
                    f"{self._thresholds['max_drawdown']:.0%}",
                )
                health.status = StrategyHealthStatus.DISABLED
            elif health.n_errors > int(self._thresholds["max_errors_per_hour"]):
                self._disable(
                    strategy_name,
                    health,
                    f"Error rate {health.n_errors}/h above "
                    f"{int(self._thresholds['max_errors_per_hour'])}/h",
                )
                health.status = StrategyHealthStatus.DISABLED
            elif was_disabled:
                # Was disabled, now passes thresholds — preserve
                # DISABLED until the operator explicitly ``enable()``s
                # the strategy. The monitor's contract is "auto-disable
                # on failure"; the re-enable path is operator-driven.
                health.status = StrategyHealthStatus.DISABLED
            else:
                health.status = StrategyHealthStatus.HEALTHY
        else:
            # Not enough trades to evaluate — mark as degraded (the
            # strategy is running but we don't have enough data to
            # assert it's healthy). The DISABLED flag is preserved if
            # the strategy was already auto-disabled.
            if was_disabled:
                health.status = StrategyHealthStatus.DISABLED
            else:
                health.status = StrategyHealthStatus.DEGRADED

        # ── Staleness check (independent of the eval thresholds). ──
        # A strategy that hasn't traded in 24h is suspect — it may
        # be broken (stuck in an exception loop, market data feed
        # died, etc.). Surface as DEGRADED (NOT DISABLED) so the
        # operator sees the warning without forcing a disable.
        if health.last_trade_time > 0:
            hours_since_trade = (time.time() - health.last_trade_time) / 3600.0
            if hours_since_trade > self._thresholds["stale_strategy_hours"]:
                # Don't downgrade a DISABLED strategy — its terminal
                # state takes precedence.
                if health.status != StrategyHealthStatus.DISABLED:
                    health.status = StrategyHealthStatus.DEGRADED
                logger.warning(
                    "Strategy %s stale — no trades in %.1fh",
                    strategy_name, hours_since_trade,
                )

        self._health[strategy_name] = health
        return health

    def _disable(
        self,
        strategy_name: str,
        health: StrategyHealth,
        reason: str,
    ) -> None:
        """Disable a strategy and fire an alert.

        W24-8 — idempotent: if the strategy is already DISABLED, this
        is a no-op (avoids re-firing the alert on every periodic
        check while the operator hasn't yet cleared the flag).

        The disable itself is delegated to
        ``StrategyRegistry.disable`` (sync — cancels the strategy's
        task without awaiting + adds to the ``_disabled`` set so
        ``start_strategy`` short-circuits).

        The alert is delegated to ``AlertEngine.record_alert`` which
        constructs an ``Alert`` dataclass + fires via
        ``fire_alert`` (durable SQLite store + WS broadcast).
        """
        if health.status == StrategyHealthStatus.DISABLED:
            # Already disabled — don't re-fire the alert.
            return

        logger.warning(
            "Auto-disabling strategy %s: %s", strategy_name, reason
        )

        # ── Actually disable the strategy. ─────────────────────────
        # Lazy import so a broken registry import doesn't crash the
        # monitor at module-load time (the registry is constructed at
        # import time and pulls in concrete strategy modules).
        try:
            from strategies.registry import strategy_registry

            strategy_registry.disable(strategy_name, reason=reason)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error(
                "Failed to disable strategy %s: %s", strategy_name, e
            )

        # ── Fire the alert. ─────────────────────────────────────────
        # ``record_alert`` constructs an ``Alert`` from the primitive
        # fields + delegates to ``fire_alert`` (durable SQLite store +
        # WS broadcast). Best-effort: an alerting failure must NOT
        # undo the disable above — the strategy is already disabled
        # by this point.
        try:
            from core.alerting import (
                SEVERITY_WARNING,
                alert_engine,
            )

            alert_engine.record_alert(
                name="strategy_auto_disabled",
                category="strategy",
                severity=SEVERITY_WARNING,
                message=(
                    f"Strategy '{strategy_name}' auto-disabled: {reason}"
                ),
            )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "Alert fire failed for %s disable: %s",
                strategy_name, e,
            )

        health.disable_reason = reason
        health.disable_time = time.time()

    # ── Read APIs (used by the FastAPI routes + dashboard) ────────

    def get_all_health(self) -> list[dict[str, Any]]:
        """Health status of every strategy the monitor has seen.

        Returns a list of dicts (one per strategy) so the
        ``GET /api/strategies/health`` route can return a JSON array
        directly. The dict shape mirrors ``StrategyHealth.to_dict``.
        """
        return [h.to_dict() for h in self._health.values()]

    def get_summary(self) -> dict[str, int]:
        """Aggregated health counts (healthy / degraded / disabled / inactive).

        W24-8 — the summary is used by the
        ``GET /api/strategies/health/summary`` route as the dashboard's
        headline KPI strip ("X healthy, Y degraded, Z disabled"). The
        counts are derived from ``self._health`` so the summary always
        reflects the latest ``check_strategy`` calls.
        """
        healthy = sum(
            1 for h in self._health.values()
            if h.status == StrategyHealthStatus.HEALTHY
        )
        degraded = sum(
            1 for h in self._health.values()
            if h.status == StrategyHealthStatus.DEGRADED
        )
        disabled = sum(
            1 for h in self._health.values()
            if h.status == StrategyHealthStatus.DISABLED
        )
        inactive = sum(
            1 for h in self._health.values()
            if h.status == StrategyHealthStatus.INACTIVE
        )
        return {
            "total_strategies": len(self._health),
            "healthy": healthy,
            "degraded": degraded,
            "disabled": disabled,
            "inactive": inactive,
        }


# ── Module-level singleton (mirrors the pattern in every other core/ module) ─
strategy_health_monitor = StrategyHealthMonitor()
