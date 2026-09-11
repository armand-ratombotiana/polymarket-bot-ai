"""Async data access objects using the async DB pool.

W16-7 — async read-side repositories layered on top of
``core.db_pool.AsyncDBPool``. Each repository wraps a single SQLite
database and exposes the most common read queries used by the FastAPI
v2 endpoints (and, future-wave, by WS broadcast loops + dashboard
pollers).

W23-6 — async write paths. The async pool now covers EVERY write path
the bot issues (decision events + rejections, observability metrics,
execution-quality rows, closed positions, alerts, feature-store
values + importance). Each write-capable repository carries an
``_ensure_schema()`` helper that mirrors the sync recorder's
``_init_db()`` shape (``CREATE TABLE IF NOT EXISTS`` + the W11-9
indexes), so the async repo works against a fresh DB without depending
on the sync recorder having been imported first.

Schema alignment
----------------
The queries below target the **actual** production schema created by
the sync recorders (``core.decision_ledger``, ``core.observability``,
``core.execution_quality``, ``core.closed_positions``,
``core.alerting``, ``ml.feature_store``) — not the schema names
referenced in the original task spec (``decisions`` /
``observability_metrics``). The sync recorders create:

* ``decision_events``   in ``decision_ledger.db``
  (columns: id, timestamp, decision_id, stage, token_id, strategy,
  pnl, data_json)
* ``decision_rejections`` in ``decision_ledger.db``
  (columns: id, timestamp, decision_id, token_id, strategy,
  predicted_edge, confidence, reason, market_mid)
* ``metrics``            in ``observability.db``
  (columns: id, timestamp, category, name, value, metadata_json)
* ``execution_quality``  in ``execution_quality.db``
  (columns: id, timestamp, order_id, decision_id, token_id, strategy,
  side, signal_price, decision_price, submitted_price, best_bid,
  best_ask, expected_fill, actual_fill, spread, slippage,
  slippage_bps, latency_ms, realized_edge, paper, data_json)
* ``closed_positions``   in ``closed_positions.db``
  (columns: id, timestamp, position_id, token_id, strategy,
  entry_price, exit_price, shares, pnl, holding_seconds,
  model_version, decision_id, direction, confidence, predicted_edge,
  p_yes, market_mid, liquidity, metadata_json)
* ``alerts``             in ``alerts.db``
  (columns: alert_id, timestamp, category, name, severity, message,
  value, threshold, metadata, acknowledged)
* ``feature_definitions`` / ``feature_values`` / ``feature_importance``
  in ``feature_store.db`` (mirrors ``ml.feature_store.FeatureStore``)

The task spec referenced ``decisions`` / ``observability_metrics`` as
the table names; the actual sync recorder code uses the names above
(see ``core/decision_ledger.py::_init_db`` and
``core/observability.py::_init_db``). Using the spec's literal names
would mean the async endpoints return empty results against the
production DBs — fixing the names so the repos actually work.

Parameter → column mapping
--------------------------
The W23-6 task spec uses generic parameter names (``correlation_id``,
``side``, ``size``, ``realized_pnl``) that don't always match the
sync schema's column names. Each async write method maps the spec's
parameter names to the canonical sync-schema column names so the
async writes are observable by the existing sync read paths (e.g.
``ClosedPositionsStore.get_closed_positions`` reads rows written by
``AsyncClosedPositionsRepository.record_close``):

  * ``correlation_id``        → ``decision_events.decision_id``
  * ``side`` (closed pos)      → ``closed_positions.direction``
  * ``size`` (closed pos)      → ``closed_positions.shares``
  * ``realized_pnl``           → ``closed_positions.pnl``
  * ``intended_price`` (eq)   → ``execution_quality.signal_price``
  * ``fill_price``    (eq)    → ``execution_quality.actual_fill``
  * ``exit_reason`` (closed)   → ``closed_positions.metadata_json``
                                (no dedicated column — serialised)

The catch-all ``data`` / ``risk_data`` / ``metadata`` dicts are
JSON-serialised (with ``default=str`` so dataclasses / Decimals /
numpy scalars don't blow up) and stored in the appropriate ``*_json``
/ ``metadata`` column.

Additive
--------
This module is **additive to the W16-7 read surface** — the existing
read methods are unchanged. The W23-6 write methods + new repos
(ClosedPositions / Alert / FeatureStore) are NEW surfaces layered
on top of the same ``AsyncDBPool``. The sync recorders continue to
be the source of truth for production writes; the async write paths
are an opt-in alternative for FastAPI v2 endpoints + WS broadcast
loops that want to issue writes without blocking the event loop on
``sqlite3.connect``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from core.db_pool import db_pool

logger = logging.getLogger(__name__)


# ─── Shared schema-ensurement helpers ─────────────────────────────────────
# Each ``_ensure_schema`` method runs at __init__ time and creates the
# target table + indexes via the sync ``sqlite3`` module (NOT via the async
# pool — schema creation is a one-shot, fast, idempotent operation that
# doesn't benefit from aiosqlite's cooperative locking). ``CREATE TABLE IF
# NOT EXISTS`` makes the call safe against a DB that already has the
# schema (the sync recorder may have created it first).


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float coercion — returns None on failure.

    Used to coerce ``risk_data`` dict values (which may be None / str /
    Decimal / numpy scalars) into the ``REAL`` columns the sync schema
    expects. Mirrors the ``_safe_float`` helper in
    ``core/closed_positions.py``.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AsyncDecisionRepository:
    """Async read + write access to the decision ledger.

    Targets ``decision_events`` (the ordered stage chain) AND
    ``decision_rejections`` (the fast-filtered rejection listing) —
    the same two tables the sync ``core.decision_ledger.DecisionLedger``
    recorder reads from / writes to.

    W23-6 — write methods ``record_event`` + ``record_rejection``
    issue INSERTs through the async pool so FastAPI v2 endpoints
    can append stage events (e.g. ``PREDICTION`` / ``SIGNAL`` /
    ``ORDER`` / ``FILL``) without blocking the event loop on
    ``sqlite3.connect``.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create ``decision_events`` + ``decision_rejections`` if absent.

        Mirrors the sync ``core.decision_ledger.DecisionLedger._init_db``
        schema (columns + indexes). Safe to call against a DB that already
        has the tables (``CREATE TABLE IF NOT EXISTS``).
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS decision_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        decision_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        token_id TEXT,
                        strategy TEXT,
                        pnl REAL DEFAULT 0.0,
                        data_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_dec_id
                        ON decision_events(decision_id, timestamp ASC);
                    CREATE INDEX IF NOT EXISTS idx_dec_token
                        ON decision_events(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_dec_stage
                        ON decision_events(stage);
                    CREATE INDEX IF NOT EXISTS idx_dec_stage_ts
                        ON decision_events(stage, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_dec_ts
                        ON decision_events(timestamp DESC);

                    CREATE TABLE IF NOT EXISTS decision_rejections (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        decision_id TEXT,
                        token_id TEXT,
                        strategy TEXT,
                        predicted_edge REAL,
                        confidence REAL,
                        reason TEXT,
                        market_mid REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_rej_token
                        ON decision_rejections(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_rej_decision
                        ON decision_rejections(decision_id);
                    CREATE INDEX IF NOT EXISTS idx_rej_ts
                        ON decision_rejections(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_rej_reason_ts
                        ON decision_rejections(reason, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_rej_strategy_ts
                        ON decision_rejections(strategy, timestamp DESC);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] decision _ensure_schema failed: %s", e
            )

    # ── Reads ────────────────────────────────────────────────────────────

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

    # ── Writes (W23-6) ────────────────────────────────────────────────────

    async def record_event(
        self,
        correlation_id: str,
        token_id: str,
        stage: str,
        data: Optional[dict[str, Any]] = None,
        model_version: Optional[str] = None,
    ) -> None:
        """Append a single stage event to the ``decision_events`` chain.

        ``correlation_id`` is the cross-stage trace key (the sync
        schema's ``decision_id`` column — same key the sync recorder
        writes when it calls ``record(decision_id=..., stage=...)``).
        ``data`` is JSON-serialised (with ``default=str`` so dataclasses
        / Decimals / numpy scalars don't blow up) and stored in the
        ``data_json`` column. ``model_version`` is merged into the
        ``data`` dict (mirrors the sync recorder's V14 auto-stamp on
        ``PREDICTION`` stage events) so the per-event model lineage
        is queryable via ``json_extract(data_json, '$.model_version')``.

        ``token_id`` is stored as a top-level column (the sync schema
        carries it separately from ``data_json`` so the ``(token_id,
        timestamp DESC)`` index can service per-token queries without
        a JSON scan). ``stage`` is stored as a top-level column for
        the same reason.

        Persistence failures are logged at ERROR and swallowed — the
        async write path must never break the trading pipeline (same
        fire-and-forget contract as the sync recorder).
        """
        if not correlation_id:
            # Skip silently — a missing correlation_id means the caller
            # didn't participate in the unified ledger (e.g. legacy /
            # manual orders). Mirrors the sync recorder's guard.
            return
        ts = time.time()
        payload_dict: dict[str, Any] = dict(data) if data else {}
        if model_version is not None and "model_version" not in payload_dict:
            payload_dict["model_version"] = model_version
        # ``strategy`` is hoisted to a top-level column when present in
        # the data dict (the sync schema's ``strategy`` column is indexed
        # via ``idx_dec_stage`` etc; the JSON column is not).
        strategy = payload_dict.pop("strategy", None)
        pnl = _safe_float(payload_dict.pop("pnl", 0.0)) or 0.0
        payload = json.dumps(payload_dict, default=str) if payload_dict else None

        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT INTO decision_events
                (timestamp, decision_id, stage, token_id, strategy, pnl, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, correlation_id, stage, token_id, strategy, pnl, payload),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_event failed corr=%s stage=%s: %s",
                correlation_id, stage, e,
            )

    async def record_rejection(
        self,
        correlation_id: str,
        token_id: str,
        stage: str,
        reason: str,
        risk_data: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a single rejection row to ``decision_rejections``.

        ``correlation_id`` → ``decision_id`` column (the cross-stage
        trace key). ``stage`` is stored as-is (the sync schema has no
        ``stage`` column on the rejections table — we propagate it via
        ``risk_data`` so a downstream query that wants to filter by
        stage can read it from ``json_extract(metadata, '$.stage')``
        once a future wave adds a ``metadata`` column; for now we
        stash it in the ``strategy`` column is NOT appropriate, so
        ``stage`` is simply not persisted on the rejections row — the
        sync ``record_rejection`` doesn't either).

        ``risk_data`` is the rejection-time risk snapshot (predicted
        edge, confidence, market mid, strategy, etc.). The well-known
        keys are extracted to dedicated columns; the rest are dropped
        (the sync schema has no ``metadata_json`` column on
        ``decision_rejections``).

        Mirrors the sync ``DecisionLedger.record_rejection`` write
        path — but ONLY writes the ``decision_rejections`` row. The
        sync recorder additionally emits a ``RISK_REJECTED`` stage
        event on the main ``decision_events`` chain; this async method
        does NOT (callers that want the chain event should call
        ``record_event`` separately with ``stage='RISK_REJECTED'``).
        """
        ts = time.time()
        risk_data = risk_data or {}
        strategy = risk_data.get("strategy")
        predicted_edge = _safe_float(risk_data.get("predicted_edge"))
        confidence = _safe_float(risk_data.get("confidence"))
        market_mid = _safe_float(risk_data.get("market_mid"))

        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT INTO decision_rejections
                (timestamp, decision_id, token_id, strategy,
                 predicted_edge, confidence, reason, market_mid)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    correlation_id or "",
                    token_id,
                    strategy,
                    predicted_edge,
                    confidence,
                    reason,
                    market_mid,
                ),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_rejection failed corr=%s reason=%s: %s",
                correlation_id, reason, e,
            )


class AsyncObservabilityRepository:
    """Async read + write access to observability metrics.

    Targets the ``metrics`` table (the single, generic metric store)
    — the same table the sync ``core.observability.Observability``
    recorder writes to.

    W23-6 — write methods ``record_metric`` + ``record_metrics_batch``
    issue INSERTs through the async pool so FastAPI v2 endpoints can
    record metrics without blocking the event loop on
    ``sqlite3.connect``. The batch path uses ``execute_many`` for a
    single transaction (vs N round-trips for individual records).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create the ``metrics`` table + indexes if absent.

        Mirrors the sync ``core.observability.Observability._init_db``
        schema. Safe to call against a DB that already has the table.
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS metrics (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        category TEXT NOT NULL,
                        name TEXT NOT NULL,
                        value REAL NOT NULL,
                        metadata_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_metrics_cat_name_time
                        ON metrics(category, name, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_metrics_name_time
                        ON metrics(name, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_metrics_cat
                        ON metrics(category);
                    CREATE INDEX IF NOT EXISTS idx_metrics_ts
                        ON metrics(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_metrics_cat_ts
                        ON metrics(category, timestamp DESC);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] observability _ensure_schema failed: %s", e
            )

    # ── Reads ────────────────────────────────────────────────────────────

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

    # ── Writes (W23-6) ────────────────────────────────────────────────────

    async def record_metric(
        self,
        category: str,
        name: str,
        value: float | int | bool,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a single metric sample.

        ``value`` is coerced to ``float`` (bool → 0.0/1.0). ``metadata``
        is JSON-serialised with ``default=str`` (handles Decimals /
        enums / dataclasses / numpy scalars) and stored in
        ``metadata_json``. Empty ``category`` or ``name`` is skipped
        silently — bad call-site inputs never propagate as schema
        noise. Mirrors the sync ``Observability.record_metric`` write
        path.
        """
        if not category or not name:
            return
        ts = time.time()
        try:
            v = float(value)
        except (TypeError, ValueError) as e:
            logger.debug(
                "[async_repositories] record_metric: coercing value failed "
                "cat=%s name=%s value=%r (%s) — defaulting to 0.0",
                category, name, value, e,
            )
            v = 0.0
        payload = json.dumps(metadata, default=str) if metadata else None

        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT INTO metrics
                (timestamp, category, name, value, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (ts, str(category), str(name), v, payload),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_metric failed cat=%s name=%s: %s",
                category, name, e,
            )

    async def record_metrics_batch(
        self, metrics: list[dict[str, Any]]
    ) -> int:
        """Persist N metric samples in a single transaction.

        Each entry in ``metrics`` is a dict with keys ``category``,
        ``name``, ``value``, and optional ``metadata``. Entries with
        an empty ``category`` / ``name`` are skipped. Returns the
        number of rows actually inserted (which may be less than
        ``len(metrics)`` if some entries were skipped). Uses
        ``AsyncDBPool.execute_many`` so the whole batch commits in
        one transaction (vs N round-trips for individual records).
        """
        if not metrics:
            return 0
        ts = time.time()
        rows: list[tuple] = []
        for entry in metrics:
            category = entry.get("category")
            name = entry.get("name")
            if not category or not name:
                continue
            try:
                v = float(entry.get("value", 0.0))
            except (TypeError, ValueError):
                v = 0.0
            meta = entry.get("metadata")
            payload = json.dumps(meta, default=str) if meta else None
            rows.append((ts, str(category), str(name), v, payload))
        if not rows:
            return 0
        try:
            return await db_pool.execute_many(
                self._db_path,
                """
                INSERT INTO metrics
                (timestamp, category, name, value, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_metrics_batch failed (%d entries): %s",
                len(metrics), e,
            )
            return 0


class AsyncExecutionQualityRepository:
    """Async read + write access to execution-quality data.

    Targets the ``execution_quality`` table — the same table the
    sync ``core.execution_quality.ExecutionQuality`` recorder writes
    to. Reads only in W16-7; W23-6 adds the ``record_execution``
    write path.

    W23-6 — ``get_stats`` now accepts an optional ``hours`` window
    parameter so callers can request stats over the last N hours
    (default ``None`` = full history, preserving the W16-7 contract).
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create the ``execution_quality`` table + indexes if absent.

        Mirrors the sync ``core.execution_quality._init_db`` schema.
        Safe to call against a DB that already has the table. Note
        the sync schema marks ``order_id`` as ``NOT NULL`` — the async
        ``record_execution`` write path passes an empty string for
        ``order_id`` when the caller doesn't supply one, so the NOT
        NULL constraint is honoured.
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS execution_quality (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        order_id TEXT NOT NULL,
                        decision_id TEXT,
                        token_id TEXT,
                        strategy TEXT,
                        side TEXT,
                        signal_price REAL,
                        decision_price REAL,
                        submitted_price REAL,
                        best_bid REAL,
                        best_ask REAL,
                        expected_fill REAL,
                        actual_fill REAL,
                        spread REAL,
                        slippage REAL,
                        slippage_bps REAL,
                        latency_ms REAL,
                        realized_edge REAL,
                        paper INTEGER DEFAULT 0,
                        data_json TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_eq_ts
                        ON execution_quality(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_strategy
                        ON execution_quality(strategy, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_token
                        ON execution_quality(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_decision
                        ON execution_quality(decision_id);
                    CREATE INDEX IF NOT EXISTS idx_eq_slippage
                        ON execution_quality(slippage_bps DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_side_ts
                        ON execution_quality(side, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_paper_ts
                        ON execution_quality(paper, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_eq_order
                        ON execution_quality(order_id);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] execution_quality _ensure_schema failed: %s", e
            )

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_recent_fills(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` per-fill execution-quality
        records (newest first)."""
        query = (
            "SELECT * FROM execution_quality ORDER BY timestamp DESC LIMIT ?"
        )
        return await db_pool.execute(self._db_path, query, (limit,))

    async def get_stats(
        self, hours: Optional[float] = None
    ) -> dict[str, Any]:
        """Return aggregate execution-quality stats.

        W23-6 — the optional ``hours`` parameter windowed stats to the
        last N hours. Default ``None`` = full history (preserves the
        W16-7 contract so existing callers that don't pass ``hours``
        see the same AVG / COUNT over every row).

        ``avg_slippage_bps`` is the mean of the non-NULL
        ``slippage_bps`` column (NULL when the simulator couldn't
        compute slippage — e.g. zero-notional fills). ``total_fills``
        is the row count (optionally windowed).
        """
        cutoff: Optional[float] = None
        if hours is not None and hours > 0:
            cutoff = time.time() - float(hours) * 3600.0

        avg_query = (
            "SELECT AVG(slippage_bps) FROM execution_quality "
            "WHERE slippage_bps IS NOT NULL"
        )
        count_query = "SELECT COUNT(*) FROM execution_quality"
        avg_params: tuple = ()
        count_params: tuple = ()
        if cutoff is not None:
            avg_query += " AND timestamp > ?"
            count_query += " WHERE timestamp > ?"
            avg_params = (cutoff,)
            count_params = (cutoff,)

        avg_slippage = await db_pool.execute_scalar(
            self._db_path, avg_query, avg_params
        )
        total_fills = await db_pool.execute_scalar(
            self._db_path, count_query, count_params
        )
        return {
            "avg_slippage_bps": float(avg_slippage) if avg_slippage is not None else 0.0,
            "total_fills": int(total_fills) if total_fills is not None else 0,
        }

    # ── Writes (W23-6) ────────────────────────────────────────────────────

    async def record_execution(
        self,
        token_id: str,
        side: str,
        intended_price: float,
        fill_price: float,
        slippage_bps: Optional[float],
        latency_ms: float,
        order_id: Optional[str] = None,
    ) -> None:
        """Persist a single execution-quality row.

        Maps the spec's parameter names to the sync schema:

          * ``intended_price`` → ``signal_price`` (the price observed
            at signal-generation time; the sync recorder's
            ``record_execution`` uses ``signal_price`` for the same
            concept).
          * ``fill_price``       → ``actual_fill`` (the post-slippage
            booked price).
          * ``slippage_bps``     → ``slippage_bps`` (NULL allowed — the
            sync recorder also stores NULL when slippage can't be
            computed, e.g. zero-notional fills).
          * ``latency_ms``       → ``latency_ms``.
          * ``order_id``         → ``order_id`` (NOT NULL column — the
            async path passes an empty string when the caller doesn't
            supply one, mirroring the sync recorder's contract).

        ``decision_price`` / ``submitted_price`` / ``best_bid`` /
        ``best_ask`` / ``expected_fill`` / ``spread`` / ``slippage``
        / ``realized_edge`` / ``paper`` / ``strategy`` /
        ``decision_id`` columns are left NULL / default — the async
        path only persists the spec's parameters. Callers that need
        the full execution-quality snapshot should continue to use the
        sync ``core.execution_quality.record_execution`` path.
        """
        ts = time.time()
        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT INTO execution_quality
                (timestamp, order_id, token_id, side, signal_price,
                 actual_fill, slippage_bps, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    order_id or "",
                    token_id,
                    side,
                    float(intended_price) if intended_price is not None else None,
                    float(fill_price) if fill_price is not None else None,
                    _safe_float(slippage_bps),
                    float(latency_ms) if latency_ms is not None else None,
                ),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_execution failed token=%s side=%s: %s",
                token_id, side, e,
            )


# ─── W23-6 new repositories ──────────────────────────────────────────────


class AsyncClosedPositionsRepository:
    """Async read + write access to the closed-positions journal.

    Targets the ``closed_positions`` table — the same table the sync
    ``core.closed_positions.ClosedPositionsStore`` recorder writes to.
    The async repo writes through the async pool so FastAPI v2
    endpoints can persist closed positions without blocking the event
    loop on ``sqlite3.connect``.

    Schema mirror — the table carries attribution-dimension columns
    (``decision_id``, ``direction``, ``confidence``, ``predicted_edge``,
    ``p_yes``, ``market_mid``, ``liquidity``) as first-class columns
    (not buried in ``metadata_json``) so SQLite can ``GROUP BY`` them
    directly via the ``CASE`` expressions in ``core.attribution``.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create ``closed_positions`` + indexes if absent.

        Mirrors ``core.closed_positions.ClosedPositionsStore._init_db``
        (W11-9 indexes included). Safe to call against a DB that
        already has the table.
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS closed_positions (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp       REAL    NOT NULL,
                        position_id     TEXT    NOT NULL UNIQUE,
                        token_id        TEXT    NOT NULL,
                        strategy        TEXT,
                        entry_price     REAL,
                        exit_price      REAL,
                        shares          REAL,
                        pnl             REAL    DEFAULT 0.0,
                        holding_seconds REAL    DEFAULT 0.0,
                        model_version   TEXT,
                        decision_id     TEXT,
                        direction       TEXT,
                        confidence      REAL,
                        predicted_edge  REAL,
                        p_yes           REAL,
                        market_mid      REAL,
                        liquidity       REAL,
                        metadata_json   TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_cp_token
                        ON closed_positions(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_cp_strategy
                        ON closed_positions(strategy, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_cp_time
                        ON closed_positions(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_cp_decision
                        ON closed_positions(decision_id);
                    CREATE INDEX IF NOT EXISTS idx_cp_direction
                        ON closed_positions(direction);
                    CREATE INDEX IF NOT EXISTS idx_cp_pnl
                        ON closed_positions(pnl);
                    CREATE INDEX IF NOT EXISTS idx_cp_model_ts
                        ON closed_positions(model_version, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_cp_exit_price
                        ON closed_positions(exit_price);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] closed_positions _ensure_schema failed: %s", e
            )

    # ── Writes ────────────────────────────────────────────────────────────

    async def record_close(
        self,
        token_id: str,
        side: str,
        entry_price: float,
        exit_price: float,
        size: float,
        realized_pnl: float,
        exit_reason: str,
        *,
        strategy: Optional[str] = None,
        position_id: Optional[str] = None,
        holding_seconds: Optional[float] = None,
        model_version: Optional[str] = None,
        decision_id: Optional[str] = None,
        confidence: Optional[float] = None,
        predicted_edge: Optional[float] = None,
        p_yes: Optional[float] = None,
        market_mid: Optional[float] = None,
        liquidity: Optional[float] = None,
        timestamp: Optional[float] = None,
        **extra_metadata: Any,
    ) -> str:
        """Persist a closed position. Returns the ``position_id``.

        Spec parameter → schema column mapping:

          * ``side``         → ``direction`` (the sync schema's column
            name; ``BUY`` / ``SELL`` of the opening trade).
          * ``size``         → ``shares``.
          * ``realized_pnl`` → ``pnl``.
          * ``exit_reason``  → ``metadata_json`` (no dedicated column).

        The remaining keyword arguments populate the attribution-
        dimension columns (``decision_id``, ``confidence``,
        ``predicted_edge``, ``p_yes``, ``market_mid``, ``liquidity``)
        and the lineage columns (``strategy``, ``model_version``,
        ``holding_seconds``). Any extra ``**extra_metadata`` kwargs
        are serialised into ``metadata_json`` alongside ``exit_reason``.

        Idempotency: an explicit ``position_id`` kwarg is honoured as
        the unique key (``INSERT OR IGNORE``). Without one, a fresh
        ``pos-{uuid4.hex}`` is generated so repeated calls produce
        distinct rows — callers that need exactly-once semantics must
        pass the same ``position_id`` (e.g. derived from the
        originating ``decision_id``).
        """
        pid = position_id or f"pos-{uuid.uuid4().hex}"
        ts = float(timestamp) if timestamp is not None else time.time()

        # Bundle exit_reason + any extra kwargs into metadata_json.
        extras: dict[str, Any] = dict(extra_metadata)
        if exit_reason:
            extras["exit_reason"] = exit_reason
        payload = json.dumps(extras, default=str) if extras else None

        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT OR IGNORE INTO closed_positions
                (timestamp, position_id, token_id, strategy, entry_price,
                 exit_price, shares, pnl, holding_seconds, model_version,
                 decision_id, direction, confidence, predicted_edge,
                 p_yes, market_mid, liquidity, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts,
                    pid,
                    token_id,
                    strategy,
                    float(entry_price) if entry_price is not None else None,
                    float(exit_price) if exit_price is not None else None,
                    float(size) if size is not None else None,
                    float(realized_pnl) if realized_pnl is not None else 0.0,
                    float(holding_seconds) if holding_seconds is not None else 0.0,
                    model_version,
                    decision_id,
                    side,
                    _safe_float(confidence),
                    _safe_float(predicted_edge),
                    _safe_float(p_yes),
                    _safe_float(market_mid),
                    _safe_float(liquidity),
                    payload,
                ),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_close failed token=%s side=%s: %s",
                token_id, side, e,
            )
        return pid

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` closed positions (newest first)."""
        return await db_pool.execute(
            self._db_path,
            "SELECT * FROM closed_positions ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )

    async def get_stats(self) -> dict[str, Any]:
        """Return aggregate closed-position stats.

        Mirrors the headline fields of the sync
        ``ClosedPositionsStore.get_closed_stats`` payload (count, total
        PnL, win rate, gross profit / loss, profit factor). The full
        sync stats payload includes median PnL + best / worst trade +
        avg holding period — those require a per-row fetch (median
        isn't a built-in SQLite aggregate); this async path keeps the
        query side lean and returns only the SQL-aggregatable fields.
        Callers that need the full payload should call the sync
        ``get_closed_stats`` path.
        """
        row = await db_pool.execute(
            self._db_path,
            """
            SELECT
                COUNT(*)                                          AS count,
                COALESCE(SUM(pnl), 0.0)                          AS total_pnl,
                COALESCE(AVG(pnl), 0.0)                          AS avg_pnl,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)         AS wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)         AS losses,
                SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END)         AS breakeven,
                COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0.0)
                    AS gross_profit,
                COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0.0)
                    AS gross_loss
            FROM closed_positions
            """,
        )
        if not row:
            return {
                "count": 0, "total_pnl": 0.0, "avg_pnl": 0.0,
                "win_rate": 0.0, "wins": 0, "losses": 0, "breakeven": 0,
                "gross_profit": 0.0, "gross_loss": 0.0, "profit_factor": None,
            }
        r = row[0]
        count = int(r["count"] or 0)
        wins = int(r["wins"] or 0)
        losses = int(r["losses"] or 0)
        gross_profit = float(r["gross_profit"] or 0.0)
        gross_loss = float(r["gross_loss"] or 0.0)
        win_rate = (wins / count) if count > 0 else 0.0
        profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else None
        )
        return {
            "count": count,
            "total_pnl": float(r["total_pnl"] or 0.0),
            "avg_pnl": float(r["avg_pnl"] or 0.0),
            "win_rate": win_rate,
            "wins": wins,
            "losses": losses,
            "breakeven": int(r["breakeven"] or 0),
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": profit_factor,
        }


class AsyncAlertRepository:
    """Async read + write access to the alerts store.

    Targets the ``alerts`` table — the same table the sync
    ``core.alerting.AlertEngine`` recorder writes to. The async repo
    writes through the async pool so FastAPI v2 endpoints can fire
    alerts without blocking the event loop on ``sqlite3.connect``.

    The async ``acknowledge`` paths are non-blocking equivalents of
    the sync ``AlertEngine.acknowledge`` / ``acknowledge_all`` UPDATE
    queries.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create ``alerts`` + indexes if absent.

        Mirrors ``core.alerting.AlertEngine._init_db`` (W11-9 indexes
        included). Safe to call against a DB that already has the table.
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

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
                    );
                    CREATE INDEX IF NOT EXISTS idx_alerts_timestamp
                        ON alerts(timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_alerts_sev_ack_ts
                        ON alerts(severity, acknowledged, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_alerts_cat_ts
                        ON alerts(category, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_alerts_ack_ts
                        ON alerts(acknowledged, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_alerts_name
                        ON alerts(name);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] alerts _ensure_schema failed: %s", e
            )

    # ── Writes ────────────────────────────────────────────────────────────

    async def record_alert(
        self,
        alert_id: str,
        category: str,
        name: str,
        severity: str,
        message: str,
        value: Optional[float] = None,
        threshold: Optional[float] = None,
    ) -> str:
        """Persist a single alert row. Returns the ``alert_id``.

        ``INSERT OR REPLACE`` so re-firing the same ``alert_id`` updates
        the existing row (mirrors the sync ``AlertEngine._store``
        pattern). ``acknowledged`` defaults to 0 (unacknowledged) on
        fresh inserts; an ``INSERT OR REPLACE`` against an existing
        acknowledged row will reset it to 0 — callers that want to
        preserve the acknowledged flag should fetch + patch instead.
        """
        ts = time.time()
        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT OR REPLACE INTO alerts
                (alert_id, timestamp, category, name, severity, message,
                 value, threshold, metadata, acknowledged)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    alert_id,
                    ts,
                    category,
                    name,
                    severity,
                    message,
                    _safe_float(value),
                    _safe_float(threshold),
                    None,  # metadata — async path doesn't carry rule context
                    0,
                ),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_alert failed id=%s name=%s: %s",
                alert_id, name, e,
            )
        return alert_id

    async def acknowledge(self, alert_id: str) -> bool:
        """Mark a single alert acknowledged. Returns True if a row was updated."""
        try:
            # ``execute_many`` returns rowcount; ``execute`` doesn't.
            # Use the transaction context manager so we can read
            # cursor.rowcount from the underlying connection.
            conn = await db_pool.get_connection(self._db_path)
            cursor = await conn.execute(
                "UPDATE alerts SET acknowledged = 1 WHERE alert_id = ?",
                (alert_id,),
            )
            await conn.commit()
            return cursor.rowcount > 0
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] acknowledge failed id=%s: %s",
                alert_id, e,
            )
            return False

    async def acknowledge_all(self) -> int:
        """Mark every unacknowledged alert acknowledged. Returns rows updated."""
        try:
            conn = await db_pool.get_connection(self._db_path)
            cursor = await conn.execute(
                "UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0"
            )
            await conn.commit()
            return cursor.rowcount or 0
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] acknowledge_all failed: %s", e
            )
            return 0

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_recent(
        self, limit: int = 50, unacknowledged_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return the most-recent ``limit`` alerts (newest first).

        ``unacknowledged_only=True`` filters to ``acknowledged = 0`` so
        the dashboard's "active alerts" view doesn't surface historical
        acknowledged noise.
        """
        query = "SELECT * FROM alerts"
        params: list[Any] = []
        if unacknowledged_only:
            query += " WHERE acknowledged = 0"
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        rows = await db_pool.execute(self._db_path, query, tuple(params))
        # Decode the JSON metadata column for caller convenience —
        # mirrors the sync ``AlertEngine.get_recent`` post-processing.
        for r in rows:
            raw_meta = r.get("metadata")
            if isinstance(raw_meta, str):
                try:
                    r["metadata"] = json.loads(raw_meta)
                except (TypeError, ValueError):
                    pass
        return rows


class AsyncFeatureStoreRepository:
    """Async read + write access to the ML feature store.

    Targets the ``feature_definitions`` / ``feature_values`` /
    ``feature_importance`` tables — the same three tables the sync
    ``ml.feature_store.FeatureStore`` recorder writes to. The async
    repo writes through the async pool so the training orchestrator
    (which already runs in an async context) can record feature
    values + importance snapshots without a thread-pool hop.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    # ── Schema ────────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        """Create the three feature-store tables if absent.

        Mirrors ``ml.feature_store.FeatureStore._init_db``. Safe to
        call against a DB that already has the tables.
        """
        try:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS feature_definitions (
                        name TEXT PRIMARY KEY,
                        type TEXT NOT NULL,
                        description TEXT,
                        min_value REAL,
                        max_value REAL,
                        created_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS feature_values (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        token_id TEXT,
                        feature_name TEXT NOT NULL,
                        value REAL,
                        timestamp REAL NOT NULL,
                        prediction_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_fv_token
                        ON feature_values(token_id);
                    CREATE INDEX IF NOT EXISTS idx_fv_feature
                        ON feature_values(feature_name);
                    CREATE INDEX IF NOT EXISTS idx_fv_ts
                        ON feature_values(timestamp DESC);

                    CREATE TABLE IF NOT EXISTS feature_importance (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        feature_name TEXT NOT NULL,
                        model_version TEXT NOT NULL,
                        importance REAL NOT NULL,
                        rank INTEGER NOT NULL,
                        timestamp REAL NOT NULL,
                        UNIQUE(feature_name, model_version, timestamp)
                    );
                    CREATE INDEX IF NOT EXISTS idx_fi_version
                        ON feature_importance(model_version);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] feature_store _ensure_schema failed: %s", e
            )

    # ── Writes ────────────────────────────────────────────────────────────

    async def register_feature(
        self,
        name: str,
        type: str,  # noqa: A002 — shadowing ``type`` is the sync recorder's contract
        description: str = "",
    ) -> None:
        """Register (or upsert) a feature definition.

        Mirrors ``ml.feature_store.FeatureStore.register_feature``
        (``INSERT OR REPLACE`` against the ``feature_definitions`` PK).
        ``min_value`` / ``max_value`` are left NULL — the sync recorder
        accepts them as kwargs, but the W23-6 task spec doesn't surface
        them, so the async path keeps the signature lean.
        """
        ts = time.time()
        try:
            await db_pool.execute(
                self._db_path,
                """
                INSERT OR REPLACE INTO feature_definitions
                (name, type, description, min_value, max_value, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, type, description, None, None, ts),
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] register_feature failed name=%s: %s",
                name, e,
            )

    async def record_values(
        self,
        token_id: str,
        features: dict[str, Any],
        prediction_id: Optional[str] = None,
    ) -> int:
        """Record feature values for a prediction.

        Writes one row per numeric feature value (mirrors the sync
        recorder — non-numeric values are skipped silently because the
        ``value`` column is ``REAL``). Returns the number of rows
        actually inserted (which may be less than ``len(features)``
        if some values were non-numeric).
        """
        if not features:
            return 0
        ts = time.time()
        rows: list[tuple] = []
        for fname, value in features.items():
            if isinstance(value, bool):
                # ``bool`` is a subclass of ``int``; coerce explicitly
                # so ``True`` → 1.0 (matches the sync recorder's
                # ``float(value)`` coercion).
                value = float(int(value))
            elif isinstance(value, (int, float)):
                value = float(value)
            else:
                continue
            rows.append((token_id, fname, value, ts, prediction_id))
        if not rows:
            return 0
        try:
            return await db_pool.execute_many(
                self._db_path,
                """
                INSERT INTO feature_values
                (token_id, feature_name, value, timestamp, prediction_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_values failed token=%s (%d features): %s",
                token_id, len(features), e,
            )
            return 0

    async def record_importance(
        self,
        model_version: str,
        importance_dict: dict[str, float],
    ) -> int:
        """Record a feature-importance snapshot for a model version.

        Sorts the dict by descending importance, assigns a rank per
        feature (1-based), and persists one row per feature. Returns
        the number of rows persisted. Uses ``INSERT OR REPLACE`` so a
        re-record against the same ``(feature_name, model_version,
        timestamp)`` tuple updates in place (mirrors the sync recorder).
        """
        if not importance_dict:
            return 0
        ts = time.time()
        sorted_features = sorted(
            importance_dict.items(),
            key=lambda x: -float(x[1] if x[1] is not None else 0.0),
        )
        rows: list[tuple] = []
        for rank, (fname, imp) in enumerate(sorted_features, 1):
            rows.append((fname, model_version, float(imp or 0.0), rank, ts))
        try:
            return await db_pool.execute_many(
                self._db_path,
                """
                INSERT OR REPLACE INTO feature_importance
                (feature_name, model_version, importance, rank, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                rows,
            )
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[async_repositories] record_importance failed version=%s (%d features): %s",
                model_version, len(importance_dict), e,
            )
            return 0

    # ── Reads ────────────────────────────────────────────────────────────

    async def get_top_features(
        self, model_version: str, top_n: int = 20
    ) -> list[dict[str, Any]]:
        """Return the top-N most important features for a model version.

        Mirrors ``ml.feature_store.FeatureStore.get_top_features`` —
        ordered by ``rank ASC`` so the most-important feature is first.
        """
        return await db_pool.execute(
            self._db_path,
            """
            SELECT feature_name, importance, rank, timestamp
            FROM feature_importance
            WHERE model_version = ? AND rank <= ?
            ORDER BY rank ASC
            """,
            (model_version, top_n),
        )
