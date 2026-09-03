"""Async data access objects using the async DB pool.

W16-7 — async read-side repositories layered on top of
``core.db_pool.AsyncDBPool``. Each repository wraps a single SQLite
database and exposes the most common read queries used by the FastAPI
v2 endpoints (and, future-wave, by WS broadcast loops + dashboard
pollers).

Schema alignment
----------------
The queries below target the **actual** production schema created by
the sync recorders (``core.decision_ledger``, ``core.observability``,
``core.execution_quality``) — not the schema names referenced in the
original task spec (``decisions`` / ``observability_metrics``). The
sync recorders create:

* ``decision_events``   in ``decision_ledger.db``
  (columns: id, timestamp, decision_id, stage, token_id, strategy,
  pnl, data_json)
* ``metrics``            in ``observability.db``
  (columns: id, timestamp, category, name, value, metadata_json)
* ``execution_quality``  in ``execution_quality.db``
  (columns: id, timestamp, order_id, decision_id, token_id, strategy,
  side, signal_price, decision_price, submitted_price, best_bid,
  best_ask, expected_fill, actual_fill, spread, slippage,
  slippage_bps, latency_ms, realized_edge, paper, data_json)

The task spec referenced ``decisions`` / ``observability_metrics`` as
the table names; the actual sync recorder code uses the names above
(see ``core/decision_ledger.py::_init_db`` and
``core/observability.py::_init_db``). Using the spec's literal names
would mean the async endpoints return empty results against the
production DBs — fixing the names so the repos actually work.

Additive
--------
This module is **purely additive** — it does NOT touch the sync
recorders. The sync paths continue to be the source of truth for
writes; these async repos are the new read-side fast path for the
``/api/v2/*`` endpoints.
"""
from __future__ import annotations

from typing import Any, Optional

from core.db_pool import db_pool


class AsyncDecisionRepository:
    """Async read access to the decision ledger.

    Targets ``decision_events`` (the ordered stage chain) — the same
    table the sync ``core.decision_ledger.DecisionLedger.get_chain``
    reads from. Reads only — writes still go through the sync
    ``DecisionLedger`` recorder (which is the source of truth for the
    immutable audit trail).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def get_recent(
        self, limit: int = 50, stage: Optional[str] = None
    ) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` decision events (oldest-first
        callers should reverse; the table's natural ORDER BY is
        ``timestamp DESC`` so the dashboard gets newest-first by
        default). Optional ``stage`` filter narrows to a single stage
        (e.g. ``PREDICTION`` / ``FILL``)."""
        query = "SELECT * FROM decision_events"
        params: list[Any] = []
        if stage:
            query += " WHERE stage = ?"
            params.append(stage)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return await db_pool.execute(self._db_path, query, tuple(params))

    async def get_by_token(
        self, token_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return the most-recent decision events for a single token.

        Uses the ``(token_id, timestamp DESC)`` index
        (``idx_dec_token``) created by the sync recorder's
        ``_init_db`` — the same fast-path the sync ``get_chain_by_token``
        uses."""
        query = (
            "SELECT * FROM decision_events WHERE token_id = ? "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        return await db_pool.execute(self._db_path, query, (token_id, limit))

    async def count_by_stage(self, stage: str) -> int:
        """Return the number of decision events for a given stage."""
        result = await db_pool.execute_scalar(
            self._db_path,
            "SELECT COUNT(*) FROM decision_events WHERE stage = ?",
            (stage,),
        )
        return int(result) if result is not None else 0


class AsyncObservabilityRepository:
    """Async read access to observability metrics.

    Targets the ``metrics`` table (the single, generic metric store)
    — the same table the sync ``core.observability.Observability``
    recorder writes to. Reads only — writes still go through the
    sync ``Observability.record_metric`` path.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def get_latest_metrics(self) -> list[dict[str, Any]]:
        """Return the latest value for each ``(category, name)`` pair.

        Mirrors the sync ``Observability.get_health_report`` query
        shape — picks the row with the highest ``id`` per
        ``(category, name)`` group, ordered for stable dashboard
        rendering. The ``id`` proxy for "latest" is correct because
        ``id`` is an ``AUTOINCREMENT`` PRIMARY KEY.
        """
        query = """
            SELECT category, name, value, timestamp, metadata_json AS metadata
            FROM metrics
            WHERE id IN (
                SELECT MAX(id) FROM metrics GROUP BY category, name
            )
            ORDER BY category, name
        """
        return await db_pool.execute(self._db_path, query)

    async def get_metric_history(
        self, name: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """Return the most-recent N samples for a single metric name.

        Uses the ``(name, timestamp DESC)`` index
        (``idx_metrics_name_time``) — the same fast-path the sync
        ``get_metric_history`` uses."""
        query = (
            "SELECT value, timestamp, metadata_json AS metadata "
            "FROM metrics WHERE name = ? "
            "ORDER BY timestamp DESC LIMIT ?"
        )
        return await db_pool.execute(self._db_path, query, (name, limit))


class AsyncExecutionQualityRepository:
    """Async read access to execution-quality data.

    Targets the ``execution_quality`` table — the same table the
    sync ``core.execution_quality.ExecutionQuality`` recorder writes
    to. Reads only.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` per-fill execution-quality
        records (newest first)."""
        query = (
            "SELECT * FROM execution_quality ORDER BY timestamp DESC LIMIT ?"
        )
        return await db_pool.execute(self._db_path, query, (limit,))

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate execution-quality stats.

        ``avg_slippage_bps`` is the mean of the non-NULL
        ``slippage_bps`` column (NULL when the simulator couldn't
        compute slippage — e.g. zero-notional fills). ``total_fills``
        is the full row count.
        """
        avg_slippage = await db_pool.execute_scalar(
            self._db_path,
            "SELECT AVG(slippage_bps) FROM execution_quality "
            "WHERE slippage_bps IS NOT NULL",
        )
        total_fills = await db_pool.execute_scalar(
            self._db_path,
            "SELECT COUNT(*) FROM execution_quality",
        )
        return {
            "avg_slippage_bps": float(avg_slippage) if avg_slippage is not None else 0.0,
            "total_fills": int(total_fills) if total_fills is not None else 0,
        }
