"""
core/alerting.py — Threshold-based alerting system.

Evaluates metrics and risk conditions against configurable thresholds.
When a threshold is crossed, fires an alert (logged + stored in SQLite).

Schema (SQLite, separate db at ``ALERT_DB_PATH`` defaulting to
``/app/data/alerts.db`` so the audit-trail / observability / decision-ledger
DBs are not perturbed — same convention as ``core/observability.py`` and
``core/audit_logger.py``):

  alerts  (alert_id, timestamp, category, name, severity, message,
           value, threshold, metadata, acknowledged)

Indexes:
  (timestamp DESC)   — recent-first lookup for the operator dashboard

Default rule set covers 4 categories / 7 rules:

  ┌──────────┬──────────────────────────────────────────────────────┐
  │ category │ rule name                                            │
  ├──────────┼──────────────────────────────────────────────────────┤
  │ risk     │ max_drawdown_exceeded   (daily_pnl < -$2.00)         │
  │          │ kill_switch_activated                                │
  │ ml       │ model_drift_detected    (PSI > 0.25)                 │
  │          │ model_stale            (age > 24h)                    │
  │ system   │ high_latency           (api_latency_ms > 1000)       │
  │          │ backend_unhealthy                                    │
  │ data     │ data_stale             (staleness > 60s)             │
  └──────────┴──────────────────────────────────────────────────────┘

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at
startup to expose:

  GET  /api/alerts                     list recent alerts + stats
  GET  /api/alerts/stats               alert counts (total / unacked / critical)
  POST /api/alerts/{alert_id}/acknowledge   acknowledge one alert
  POST /api/alerts/acknowledge-all          acknowledge every unacked alert
  POST /api/alerts/evaluate                  trigger immediate evaluation
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ALERT_DB_PATH = Path(os.environ.get("ALERT_DB_PATH", "/app/data/alerts.db"))


# ── W11-9: query timing decorator ───────────────────────────────────────────
# Lightweight instrumentation for the most commonly-called read paths. Wraps
# sync query methods and emits a WARNING when a single call exceeds
# ``_SLOW_QUERY_THRESHOLD`` (100 ms — the dashboard SLO for the
# ``/api/alerts`` and ``/api/alerts/stats`` endpoints). Failed queries
# (the underlying methods swallow their own persistence errors and return
# [] / zeroed stats) are still timed so a slow failure path surfaces in
# the log alongside the exception traceback. The decorator is import-safe,
# never re-raises, and preserves the wrapped function's return value /
# exception semantics verbatim.
import functools  # noqa: E402  (kept next to its consumer for readability)

_SLOW_QUERY_THRESHOLD = 0.100  # seconds


def timed_query(func):
    """Log a warning when ``func`` takes longer than ``_SLOW_QUERY_THRESHOLD``.

    Supports both ``def`` and ``async def`` callables — the wrapper branches
    on ``asyncio.iscoroutinefunction`` so sync query functions and async
    ones (e.g. the FastAPI route handlers) can share the same decorator.
    """
    import asyncio as _asyncio

    if _asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def _async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.time() - start
                if duration > _SLOW_QUERY_THRESHOLD:
                    logger.warning(
                        "[alerting] slow query %s: %.3fs",
                        func.__name__,
                        duration,
                    )

        return _async_wrapper

    @functools.wraps(func)
    def _sync_wrapper(*args, **kwargs):
        start = time.time()
        try:
            return func(*args, **kwargs)
        finally:
            duration = time.time() - start
            if duration > _SLOW_QUERY_THRESHOLD:
                logger.warning(
                    "[alerting] slow query %s: %.3fs",
                    func.__name__,
                    duration,
                )

    return _sync_wrapper

# ── Alert severity levels ─────────────────────────────────────────────────
SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"
SEVERITY_ERROR = "error"


@dataclass
class Alert:
    """A single threshold-crossing event."""

    alert_id: str
    timestamp: float
    category: str  # risk, ml, system, execution, data
    name: str
    severity: str  # info, warning, critical, error
    message: str
    value: Optional[float] = None
    threshold: Optional[float] = None
    metadata: dict = field(default_factory=dict)
    acknowledged: bool = False


class AlertEngine:
    """Evaluates conditions and fires alerts.

    The engine owns its SQLite store and a rule list. ``evaluate(metrics)``
    runs every rule against the supplied dict of metric values; each rule
    whose ``condition`` callable returns truthy fires an ``Alert`` that is
    logged at WARNING level + persisted to SQLite (so the dashboard can
    surface unacknowledged alerts across process restarts).

    All persistence is fire-and-forget from the caller's perspective:
    storage errors are logged at ERROR level and swallowed so an alerting
    hiccup can never break the trading pipeline (mirrors the
    ``observability`` / ``decision_ledger`` contract).
    """

    def __init__(self, db_path: Path = ALERT_DB_PATH):
        self._db_path = db_path
        self._init_db()
        self._rules = self._default_rules()

    def _init_db(self):
        """Create the alerts table + index if they don't already exist.

        Idempotent so repeated ``AlertEngine()`` constructions against the
        same db file are safe. Parent directory is auto-created so a fresh
        sandbox with no ``/app/data`` directory works.
        """
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS alerts (
                        alert_id TEXT PRIMARY KEY,
                        timestamp REAL NOT NULL,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        severity TEXT NOT NULL,
                        message TEXT NOT NULL,
                        value REAL,
                        threshold REAL,
                        metadata TEXT,
                        acknowledged INTEGER DEFAULT 0
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_alerts_timestamp
                    ON alerts(timestamp DESC)
                """)
                # ── W11-9: additional indexes for common query patterns ──
                # (severity, acknowledged, timestamp DESC) — the dashboard's
                # "active critical alerts" view filters
                # ``WHERE severity = 'critical' AND acknowledged = 0
                # ORDER BY timestamp DESC``. The existing
                # ``(timestamp DESC)`` index can't service a query with a
                # compound WHERE clause without a full scan + sort.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_alerts_sev_ack_ts
                    ON alerts(severity, acknowledged, timestamp DESC)
                """)
                # (category, timestamp DESC) — per-category alert feed
                # (e.g. "show me recent ml alerts"). Surfaced by the
                # dashboard's per-category drill-down.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_alerts_cat_ts
                    ON alerts(category, timestamp DESC)
                """)
                # (acknowledged, timestamp DESC) — ``get_recent`` with
                # ``unacknowledged_only=True`` filters on
                # ``acknowledged = 0`` only. The compound
                # ``(severity, acknowledged, timestamp DESC)`` index above
                # can't service this query when ``severity`` is unconstrained.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_alerts_ack_ts
                    ON alerts(acknowledged, timestamp DESC)
                """)
                # (name) — ``get_stats``'s per-rule-count view (e.g. "how
                # many times has model_drift_detected fired this week?")
                # filters on ``name = ?`` only.
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_alerts_name
                    ON alerts(name)
                """)
        except Exception as e:  # noqa: BLE001 — defensive: storage must not break callers
            logger.error("[alerting] _init_db failed (path=%s): %s", self._db_path, e)

    def _default_rules(self) -> list[dict[str, Any]]:
        """Default alert rules.

        Each rule is a dict with keys:
          - ``name`` (str)        — unique rule identifier
          - ``category`` (str)    — risk / ml / system / data
          - ``severity`` (str)    — info / warning / critical / error
          - ``condition`` (callable[dict] -> bool) — fires when truthy
          - ``message`` (str)     — ``str.format(**metrics)`` template
          - ``threshold`` (float|None) — documented threshold value
          - ``metric_key`` (str|None)   — which metric to read for ``value``

        The message template MUST reference keys that exist in the
        metrics dict passed to ``evaluate()`` — otherwise the format call
        raises KeyError and the alert is skipped (logged at ERROR).
        """
        return [
            # ── Risk alerts ───────────────────────────────────────────────
            {
                "name": "max_drawdown_exceeded",
                "category": "risk",
                "severity": SEVERITY_CRITICAL,
                "condition": lambda metrics: metrics.get("daily_pnl", 0) < -2.0,
                "message": "Daily loss limit exceeded: ${daily_pnl:.2f}",
                "threshold": -2.0,
                "metric_key": "daily_pnl",
            },
            {
                "name": "kill_switch_activated",
                "category": "risk",
                "severity": SEVERITY_CRITICAL,
                "condition": lambda metrics: bool(metrics.get("kill_switch_active", False)),
                "message": "Kill switch is active — all trading halted",
                "threshold": None,
                "metric_key": "kill_switch_active",
            },
            # ── ML alerts ─────────────────────────────────────────────────
            {
                "name": "model_drift_detected",
                "category": "ml",
                "severity": SEVERITY_WARNING,
                "condition": lambda metrics: metrics.get("psi", 0) > 0.25,
                "message": "Model drift detected (PSI={psi:.3f})",
                "threshold": 0.25,
                "metric_key": "psi",
            },
            {
                "name": "model_stale",
                "category": "ml",
                "severity": SEVERITY_WARNING,
                "condition": lambda metrics: metrics.get("model_age_hours", 0) > 24,
                "message": "Model is {model_age_hours:.0f}h old — consider retraining",
                "threshold": 24,
                "metric_key": "model_age_hours",
            },
            # ── System alerts ────────────────────────────────────────────
            {
                "name": "high_latency",
                "category": "system",
                "severity": SEVERITY_WARNING,
                "condition": lambda metrics: metrics.get("api_latency_ms", 0) > 1000,
                "message": "API latency high: {api_latency_ms:.0f}ms",
                "threshold": 1000,
                "metric_key": "api_latency_ms",
            },
            {
                "name": "backend_unhealthy",
                "category": "system",
                "severity": SEVERITY_CRITICAL,
                "condition": lambda metrics: metrics.get("backend_healthy", True) is False,
                "message": "Backend health check failed",
                "threshold": None,
                "metric_key": "backend_healthy",
            },
            # ── Data alerts ──────────────────────────────────────────────
            {
                "name": "data_stale",
                "category": "data",
                "severity": SEVERITY_WARNING,
                "condition": lambda metrics: metrics.get("data_staleness_seconds", 0) > 60,
                "message": "Market data is {data_staleness_seconds:.0f}s stale",
                "threshold": 60,
                "metric_key": "data_staleness_seconds",
            },
        ]

    def evaluate(self, metrics: dict[str, Any]) -> list[Alert]:
        """Evaluate all rules against current metrics.

        Returns the list of fired alerts (also persisted to SQLite). A
        rule whose ``condition`` returns truthy fires an ``Alert`` whose
        message is formatted from the metrics dict via ``str.format``.

        Per-rule failures (e.g. a message template referencing a metric
        key that isn't in ``metrics``) are logged at ERROR and swallowed
        so a single bad rule can't prevent sibling rules from firing.
        """
        fired: list[Alert] = []
        for rule in self._rules:
            try:
                if not rule["condition"](metrics):
                    continue
                # Coerce ``value`` to float when the metric is numeric;
                # bool / None fall through to None.
                metric_key = rule.get("metric_key")
                raw_value = metrics.get(metric_key) if metric_key else None
                value: Optional[float]
                if isinstance(raw_value, bool):
                    value = 1.0 if raw_value else 0.0
                elif isinstance(raw_value, (int, float)):
                    value = float(raw_value)
                else:
                    value = None
                alert = Alert(
                    alert_id=f"{rule['name']}_{int(time.time() * 1000)}",
                    timestamp=time.time(),
                    category=rule["category"],
                    name=rule["name"],
                    severity=rule["severity"],
                    message=rule["message"].format(**metrics),
                    value=value,
                    threshold=rule.get("threshold"),
                    metadata={"metrics": metrics},
                )
                self._store(alert)
                fired.append(alert)
                logger.warning("Alert fired: %s — %s", alert.name, alert.message)
            except Exception as e:  # noqa: BLE001 — single bad rule shouldn't break siblings
                logger.error("Error evaluating rule %s: %s", rule["name"], e)
        return fired

    def _store(self, alert: Alert) -> None:
        """Persist an alert row (INSERT OR REPLACE on alert_id PK)."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO alerts
                    (alert_id, timestamp, category, name, severity, message,
                     value, threshold, metadata, acknowledged)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert.alert_id,
                        alert.timestamp,
                        alert.category,
                        alert.name,
                        alert.severity,
                        alert.message,
                        alert.value,
                        alert.threshold,
                        json.dumps(alert.metadata),
                        int(alert.acknowledged),
                    ),
                )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] _store failed: %s", e)

    def fire_alert(self, alert: Alert) -> bool:
        """Persist + log a one-off alert (NOT driven by an evaluation rule).

        W18-6 (P0-C06) — used by risk gates that need to surface a
        CRITICAL alert outside the periodic ``evaluate(metrics)`` cycle.
        The canonical caller is
        ``risk/manager.py::InstitutionalRiskEngine._check_order_impl``
        (section 6e): when the MTM risk gate itself fails (broken price
        feed / broken MTM module / unhandled exception) and FAILS CLOSED,
        it constructs an ``Alert`` and calls ``fire_alert`` so an operator
        sees the halt immediately on the dashboard rather than waiting
        for the next ``evaluate()`` tick to (maybe) catch the side-effect.

        Mirrors the per-rule persistence path used inside ``evaluate``:
        the alert is stored via ``_store`` (which swallows its own
        persistence errors so an SQLite hiccup can never break the
        caller) AND logged at WARNING level so a tail of the bot's log
        surfaces every fired alert even when the dashboard is down.

        W23-3 — schedules a fire-and-forget broadcast on the ``alerts``
        WS channel so any dashboard subscribed to ``alerts`` flashes the
        new alert immediately. ``fire_alert`` is SYNC (callers from the
        risk gate's sync ``_check_order_impl`` path can't await), so the
        broadcast is dispatched via ``asyncio.create_task``. If no event
        loop is running (e.g. unit test without a loop), the broadcast
        is silently skipped — the persistence + log still happened, so
        the alert is durable even without the live push.

        Returns ``True`` to signal the alert was dispatched (the
        underlying ``_store`` swallows storage errors so this never
        raises — callers can chain ``fire_alert`` after a critical
        decision without a try/except wrapper).
        """
        # W24-6 — duplicate-alert prevention. ``fire_alert`` is the
        # canonical "one-off alert outside the periodic evaluate() cycle"
        # entry point (the risk gate's MTM-fail-closed path calls it
        # directly). A risk gate that fires the same ``alert_id`` twice
        # within a short window (e.g. the gate is re-evaluated before the
        # operator acknowledges the first alert) would otherwise land two
        # rows in SQLite + log two warnings + broadcast two WS envelopes.
        # Block the duplicate here so the operator sees exactly one alert
        # card per distinct (alert_id, 5-min window). The 300s TTL matches
        # the default dedup window. Best-effort: a registry exception
        # must NEVER break the alert fire path (mirrors the fail-soft
        # contract of every other audit singleton in the bot).
        try:
            from core.dedup import dedup_registry
            if not dedup_registry.check_and_add("alert", alert.alert_id, ttl_seconds=300):
                logger.debug(
                    "[alerting] Duplicate alert blocked by dedup registry: %s",
                    alert.alert_id,
                )
                return False
        except Exception as e:  # noqa: BLE001 — dedup must never break alerts
            logger.debug(
                "[alerting] dedup_registry check failed (continuing): %s", e
            )
        self._store(alert)
        logger.warning("Alert fired: %s — %s", alert.name, alert.message)
        # W23-3 — fire-and-forget broadcast. ``asyncio.create_task``
        # requires a running loop; in contexts without one (sync unit
        # tests, scripts), the broadcast is silently skipped — the
        # alert has already been persisted to SQLite + logged.
        try:
            import asyncio

            asyncio.create_task(self._broadcast_alert(alert))
        except RuntimeError:
            # No running event loop — fall back to a debug log so the
            # broadcast is observable without crashing the caller. The
            # ``ws_manager.broadcast`` coroutine itself is safe to
            # construct (no I/O until awaited) so we don't need to
            # worry about a leaked coroutine here.
            logger.debug(
                "[alerting] no event loop — alerts broadcast skipped for %s",
                alert.name,
            )
        except Exception as e:  # noqa: BLE001 — broadcast must never break the caller
            logger.debug(
                "[alerting] alerts broadcast schedule failed: %s", e
            )
        return True

    def record_alert(
        self,
        name: str,
        category: str,
        severity: str,
        message: str,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
        metadata: Optional[dict] = None,
    ) -> Alert:
        """Construct + fire a one-off alert from primitive fields.

        W24-8 — convenience wrapper around ``fire_alert`` so callers
        (e.g. the strategy health monitor's
        ``StrategyHealthMonitor._disable``) don't have to construct an
        ``Alert`` dataclass + generate an ``alert_id`` + timestamp
        themselves. The canonical caller is the strategy health
        monitor: when a strategy fails its win-rate / expectancy /
        drawdown / error-rate thresholds, the monitor calls
        ``record_alert(name="strategy_auto_disabled", category="strategy",
        severity=SEVERITY_WARNING, message=...)`` so an operator sees
        the auto-disable on the dashboard immediately (W23-3 WS push
        via ``_broadcast_alert``) AND as a durable row in the alerts
        SQLite store.

        The method:

        1. Generates a fresh ``alert_id`` (UUID4 hex) + ``timestamp``
           (now).
        2. Constructs an ``Alert`` with the supplied fields + the
           generated id/timestamp + ``acknowledged=False``.
        3. Delegates to ``fire_alert`` which persists + logs + schedules
           the WS broadcast (fire-and-forget — if no event loop is
           running the broadcast is silently skipped, persistence +
           log still happen).

        Returns the constructed ``Alert`` so the caller can log / inspect
        it (e.g. record the ``alert_id`` against the strategy's
        ``StrategyHealth`` dataclass for cross-correlation).

        ``category`` is typically one of ``risk`` / ``ml`` / ``system``
        / ``data`` (the 4 default rule categories) but the engine does
        NOT enforce this — a caller can introduce a new category
        (``strategy``, ``execution`` …) without a schema migration
        because ``category`` is a free-form ``TEXT`` column in SQLite.
        The dashboard's per-category drill-down will simply include the
        new category once a row with that value exists.
        """
        import uuid

        alert = Alert(
            alert_id=uuid.uuid4().hex,
            timestamp=time.time(),
            category=category,
            name=name,
            severity=severity,
            message=message,
            value=value,
            threshold=threshold,
            metadata=metadata or {},
            acknowledged=False,
        )
        self.fire_alert(alert)
        return alert

    async def _broadcast_alert(self, alert: Alert) -> None:
        """Push a single alert on the ``alerts`` WS channel.

        W23-3 — the payload follows the spec'd shape::

            {"type": "alert", "alert": <asdict(alert)>}

        so a subscriber can dispatch on ``data.type`` (consistent with
        the ``kill_switch`` / ``observation_mode`` alert envelopes that
        ``api/server.py`` already emits on the same channel). The
        ``alert`` field carries the full dataclass dict (mirroring the
        shape returned by ``GET /api/alerts``) so the subscriber doesn't
        need a second fetch to render the alert card.

        Defensive: a broadcast failure (no WS clients, broken
        ``ws_manager`` import, send error) is swallowed at debug level
        so the alert fire path is never broken by the broadcast
        subsystem.
        """
        try:
            from core.ws_broadcast import ws_manager

            await ws_manager.broadcast(
                "alerts",
                {"type": "alert", "alert": asdict(alert)},
            )
        except Exception as e:  # noqa: BLE001 — broadcast must never break
            logger.debug(
                "[alerting] alerts broadcast failed: %s", e
            )

    @timed_query
    def get_recent(
        self, limit: int = 50, unacknowledged_only: bool = False
    ) -> list[dict[str, Any]]:
        """Get recent alerts (newest first).

        ``unacknowledged_only=True`` filters to ``acknowledged = 0`` rows
        so the dashboard's "active alerts" view doesn't surface historical
        acknowledged noise.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                query = "SELECT * FROM alerts"
                if unacknowledged_only:
                    query += " WHERE acknowledged = 0"
                query += " ORDER BY timestamp DESC LIMIT ?"
                rows = conn.execute(query, (limit,)).fetchall()
                results = [dict(r) for r in rows]
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] get_recent failed: %s", e)
            return []
        # Decode the JSON metadata column for caller convenience.
        for r in results:
            raw_meta = r.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    r["metadata"] = json.loads(raw_meta)
                except (TypeError, ValueError):
                    pass
        return results

    @timed_query
    def get_recent_page(
        self,
        limit: int = 50,
        unacknowledged_only: bool = False,
        cursor: str | None = None,
    ) -> "Page":
        """Cursor-paginated fetch of recent alerts (newest first).

        W16-5 — wraps :func:`core.pagination.paginate_query` against the
        ``alerts`` table. ``SELECT *`` includes the ``alert_id`` TEXT
        PRIMARY KEY column (used as the tiebreaker for rows that share a
        ``timestamp``). The wire payload carries the same shape as
        :meth:`get_recent` plus the new ``next_cursor`` / ``has_more``
        fields.

        Args:
            limit:                Page size (clamped to ``[1, 100]`` by
                                  :func:`paginate_query`).
            unacknowledged_only:  Filter to ``acknowledged = 0`` rows.
            cursor:               Opaque cursor from a previous
                                  response's ``next_cursor`` field.
                                  ``None`` returns the first page.

        Returns:
            :class:`core.pagination.Page` whose ``items`` are the alert
            rows (newest first) with the JSON ``metadata`` column
            decoded back to a dict (mirroring
            :meth:`get_recent`).
        """
        from core.pagination import Page, paginate_query

        if unacknowledged_only:
            base_query = "SELECT * FROM alerts WHERE acknowledged = 0"
            base_params: tuple = ()
        else:
            base_query = "SELECT * FROM alerts WHERE 1=1"
            base_params = ()

        try:
            with sqlite3.connect(self._db_path) as conn:
                conn.row_factory = sqlite3.Row
                page = paginate_query(
                    conn,
                    base_query,
                    base_params,
                    cursor=cursor,
                    limit=limit,
                    cursor_column="timestamp",
                    id_column="alert_id",
                    reverse=True,
                )
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] get_recent_page failed: %s", e)
            return Page(items=[], next_cursor=None, has_more=False)

        # Decode the JSON metadata column for caller convenience (mirrors
        # the legacy ``get_recent`` post-processing).
        for r in page.items:
            if isinstance(r, dict):
                raw_meta = r.get("metadata")
                if isinstance(raw_meta, str):
                    try:
                        r["metadata"] = json.loads(raw_meta)
                    except (TypeError, ValueError):
                        pass
        return page

    def acknowledge(self, alert_id: str) -> bool:
        """Mark a single alert acknowledged. Returns True if a row was updated."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    "UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?",
                    (alert_id,),
                )
                return cursor.rowcount > 0
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] acknowledge failed: %s", e)
            return False

    def acknowledge_all(self) -> int:
        """Mark every unacknowledged alert acknowledged. Returns rows updated."""
        try:
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.execute(
                    "UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0"
                )
                return cursor.rowcount
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] acknowledge_all failed: %s", e)
            return 0

    @timed_query
    def get_stats(self) -> dict[str, int]:
        """Return aggregate counts: total / unacked / critical-unacked.

        W11-9: Combined the original three separate COUNT queries into a
        single SUM(CASE WHEN ...) aggregate — one table scan instead of
        three. The result shape is unchanged so the API endpoint contract
        is preserved verbatim. The new ``idx_alerts_ack_ts`` and
        ``idx_alerts_sev_ack_ts`` indexes also let the query planner
        short-circuit to a covering-index scan if it deems that faster
        than a full table walk.
        """
        try:
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*)                                                    AS total,
                        SUM(CASE WHEN acknowledged = 0 THEN 1 ELSE 0 END)           AS unacked,
                        SUM(CASE WHEN severity = ? AND acknowledged = 0
                                 THEN 1 ELSE 0 END)                                AS critical
                    FROM alerts
                    """,
                    (SEVERITY_CRITICAL,),
                ).fetchone()
                total = int(row[0] if row else 0) or 0
                unacked = int(row[1] if row and row[1] is not None else 0) or 0
                critical = int(row[2] if row and row[2] is not None else 0) or 0
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("[alerting] get_stats failed: %s", e)
            return {
                "total_alerts": 0,
                "unacknowledged": 0,
                "critical_unacknowledged": 0,
            }
        return {
            "total_alerts": total,
            "unacknowledged": unacked,
            "critical_unacknowledged": critical,
        }


# Module-level singleton — mirrors the ``observability`` / ``audit_logger``
# convention so importers can grab the engine at module import time.
alert_engine = AlertEngine()


# ── FastAPI route registration ─────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """Append alerting endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET  /api/alerts                list recent alerts + aggregate stats
      GET  /api/alerts/               (trailing-slash alias for the above)
      GET  /api/alerts/stats          total / unacked / critical-unacked counts
      POST /api/alerts/{alert_id}/acknowledge   mark one alert acknowledged
      POST /api/alerts/acknowledge-all         mark every unacked alert acknowledged
      POST /api/alerts/evaluate                  trigger immediate evaluation
                                                 against the latest metrics
    """
    from fastapi import APIRouter, HTTPException
    from fastapi.responses import JSONResponse

    router = APIRouter(prefix="/api/alerts", tags=["alerts"])

    @router.get("")
    @router.get("/")
    async def get_alerts(
        limit: int = 50,
        unacknowledged_only: bool = False,
        cursor: str | None = None,
    ):
        """Return recent alerts (newest first) + aggregate stats.

        W16-5 — supports cursor-based pagination via the optional
        ``cursor`` query param. When omitted, the first page is
        returned — fully backward compatible with the pre-pagination
        wire shape (``{alerts, stats}`` plus the new ``next_cursor`` /
        ``has_more`` fields).
        """
        page = alert_engine.get_recent_page(
            limit=limit,
            unacknowledged_only=unacknowledged_only,
            cursor=cursor,
        )
        return {
            "alerts": page.items,
            "stats": alert_engine.get_stats(),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    @router.get("/stats")
    async def get_alert_stats():
        """Return total / unacked / critical-unacked alert counts."""
        return alert_engine.get_stats()

    @router.post("/{alert_id}/acknowledge")
    async def acknowledge_alert(alert_id: str):
        """Mark a single alert acknowledged by ``alert_id``."""
        if not alert_engine.acknowledge(alert_id):
            raise HTTPException(status_code=404, detail="Alert not found")
        return {"ok": True}

    @router.post("/acknowledge-all")
    async def acknowledge_all_alerts():
        """Mark every unacknowledged alert acknowledged."""
        count = alert_engine.acknowledge_all()
        return {"ok": True, "acknowledged": count}

    @router.post("/evaluate")
    async def evaluate_now():
        """Trigger immediate evaluation against current system state.

        Pulls the latest metrics snapshot from ``core.observability``
        (best-effort — if the import fails or the observability DB has
        not yet been populated by the background collector, an empty
        metrics dict is used and zero alerts fire).
        """
        metrics: dict[str, Any] = {}
        try:
            # The collector persists metrics into the observability
            # store under canonical names (data_source / execution /
            # ml / system). Best-effort: pull the latest value per
            # metric name out of the health-report shape and flatten
            # into a single name→value dict so rule conditions can
            # read directly.
            from core.observability import observability as _obs

            report = await _obs.get_health_report()
            for cat_metrics in report.get("categories", {}).values():
                if not isinstance(cat_metrics, dict):
                    continue
                for name, entry in cat_metrics.items():
                    if isinstance(entry, dict) and "value" in entry:
                        metrics[name] = entry["value"]
        except Exception as e:  # noqa: BLE001 — best-effort
            logger.debug("[alerting] evaluate_now metrics gather failed: %s", e)
            metrics = {}
        fired = alert_engine.evaluate(metrics)
        # W14-1 — broadcast each fired alert on the ``alerts`` WS channel
        # so a dashboard subscribed to the channel flashes the new alert
        # immediately rather than waiting for the next /api/alerts poll.
        # Lazy import keeps ``core.alerting`` importable even if the
        # ws_broadcast module is unavailable (it never is — same package
        # — but the pattern is consistent with the ``core.observability``
        # lazy import above). One broadcast per fired alert so a client
        # can dedupe / acknowledge them individually.
        #
        # W23-3 — payload shape ``{"type": "alert", "alert": <asdict(a)>}``
        # so a subscriber can dispatch on ``data.type`` (consistent with
        # the ``kill_switch`` / ``observation_mode`` alert envelopes that
        # ``api/server.py`` already emits on the same channel). Mirrors
        # the shape used by ``AlertEngine._broadcast_alert`` /
        # ``AlertEngine.fire_alert`` so a single client-side parser
        # handles every alerts-channel push.
        if fired:
            try:
                from core.ws_broadcast import ws_manager

                for a in fired:
                    await ws_manager.broadcast(
                        "alerts",
                        {"type": "alert", "alert": asdict(a)},
                    )
            except Exception as e:  # noqa: BLE001 — broadcast must never break the API
                logger.debug("[alerting] alerts broadcast failed: %s", e)
        return {"fired": len(fired), "alerts": [asdict(a) for a in fired]}

    app.include_router(router)


__all__ = [
    "ALERT_DB_PATH",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "SEVERITY_CRITICAL",
    "SEVERITY_ERROR",
    "Alert",
    "AlertEngine",
    "alert_engine",
    "register_routes",
    "timed_query",
]
