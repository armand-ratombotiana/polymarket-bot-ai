"""
core/closed_positions.py — SQLite-backed Closed Position Journal.

Tracks every closed position with entry/exit prices, realised P&L, holding
period, and the originating strategy + model version so the full lifecycle of
any round-trip trade (BUY entry → SELL exit, or the inverse) can be
reconstructed for performance attribution and post-hoc analytics.

This module is the canonical source for ``core/attribution.py``, which slices
the P&L roll-up across seven dimensions (strategy, confidence bucket, edge
bucket, probability band, liquidity level, holding period, trade direction).

Schema (additive — independent SQLite db at ``CLOSED_POSITIONS_DB_PATH`` so
``audit_trail.db``'s immutability contract is not perturbed; mirrors the
``core/audit_logger.py`` + ``core/decision_ledger.py`` async + ``asyncio.to_thread``
convention so the three databases coexist without schema contention)::

    closed_positions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp       REAL    NOT NULL,           -- close time (epoch seconds)
        position_id     TEXT    NOT NULL UNIQUE,    -- idempotency key
        token_id        TEXT    NOT NULL,
        strategy        TEXT,
        entry_price     REAL,
        exit_price      REAL,
        shares          REAL,
        pnl             REAL    DEFAULT 0.0,
        holding_seconds REAL    DEFAULT 0.0,
        model_version   TEXT,
        decision_id     TEXT,                        -- cross-ref to decision_ledger
        direction       TEXT,                        -- BUY / SELL of opening trade
        confidence      REAL,                        -- ML confidence at signal time [0..1]
        predicted_edge  REAL,                        -- p_yes − market_mid
        p_yes           REAL,                        -- raw model probability
        market_mid      REAL,                        -- market mid at signal time
        liquidity       REAL,                        -- market liquidity at signal time (USD)
        metadata_json   TEXT                         -- catch-all for extras
    )

Indexes:
    (token_id, timestamp DESC)       — recent-closes-for-token lookup
    (strategy, timestamp DESC)       — per-strategy feed
    (timestamp DESC)                 — most-recent-first global feed

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose::

    GET /api/positions/closed            recent closed positions (filterable)
    GET /api/positions/closed/stats      aggregate P&L / win-rate / profit-factor
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("CLOSED_POSITIONS_DB_PATH", "/app/data/closed_positions.db"))


# ── W11-9: query timing decorator ───────────────────────────────────────────
# Lightweight instrumentation for the most commonly-called read paths. Wraps
# async query methods and emits a WARNING when a single call exceeds
# ``_SLOW_QUERY_THRESHOLD`` (100 ms — the dashboard's SLO for the
# ``/api/positions/closed`` and ``/api/positions/closed/stats`` endpoints).
# Failed queries (the underlying methods swallow their own persistence
# errors and return [] / ``_empty_stats()``) are still timed so a slow
# failure path surfaces in the log alongside the exception traceback. The
# decorator is import-safe, never re-raises, and preserves the wrapped
# function's return value / exception semantics verbatim.
import functools  # noqa: E402  (kept next to its consumer for readability)

_SLOW_QUERY_THRESHOLD = 0.100  # seconds


def timed_query(func):
    """Log a warning when ``func`` takes longer than ``_SLOW_QUERY_THRESHOLD``.

    Supports both ``def`` and ``async def`` callables — the wrapper branches
    on ``asyncio.iscoroutinefunction`` so sync query functions and async
    ones can share the same decorator.
    """

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def _async_wrapper(*args, **kwargs):
            start = time.time()
            try:
                return await func(*args, **kwargs)
            finally:
                duration = time.time() - start
                if duration > _SLOW_QUERY_THRESHOLD:
                    log.warning(
                        "[closed_positions] slow query %s: %.3fs",
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
                log.warning(
                    "[closed_positions] slow query %s: %.3fs",
                    func.__name__,
                    duration,
                )

    return _sync_wrapper


# ── Attribution dimension columns ───────────────────────────────────────────
# These are first-class columns (not buried in metadata_json) so SQLite can
# GROUP BY them directly via CASE expressions in ``core/attribution.py``.
_ATTR_COLUMNS = (
    "decision_id",
    "direction",
    "confidence",
    "predicted_edge",
    "p_yes",
    "market_mid",
    "liquidity",
)


class ClosedPositionsStore:
    """
    Asynchronous, SQLite-backed closed-position journal.

    All writes swallow their own persistence errors (logged at ``error``
    level) so a journal hiccup can never break the trading pipeline. Reads
    return plain ``list[dict]`` rows (most recent first where applicable).
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else DB_PATH
        self._init_db()

    # ── Schema ────────────────────────────────────────────────────────────

    def _init_db(self) -> None:
        """Create tables + indexes if absent. Safe to call on every boot."""
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(self._db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("""
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
                    )
                """)
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_token "
                    "ON closed_positions(token_id, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_strategy "
                    "ON closed_positions(strategy, timestamp DESC)"
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_time "
                    "ON closed_positions(timestamp DESC)"
                )
                # ── W11-9: additional indexes for common query patterns ──
                # (decision_id) — cross-ref lookup against the decision
                # ledger (e.g. "find me the closed position for this
                # decision_id"). The existing ``(token_id, timestamp DESC)``
                # and ``(strategy, timestamp DESC)`` indexes can't service a
                # ``WHERE decision_id = ?`` query.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_decision "
                    "ON closed_positions(decision_id)"
                )
                # (direction) — long-YES (BUY) vs long-NO (SELL) split
                # surfaced by ``attribute_by_trade_direction``. Group-by
                # queries on a non-indexed column force a full scan.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_direction "
                    "ON closed_positions(direction)"
                )
                # (pnl) — ``get_closed_stats`` runs
                # ``SELECT pnl FROM closed_positions ORDER BY pnl`` for the
                # median calculation. Without this index SQLite must sort
                # the entire table on every call. Also services
                # "best trade / worst trade" sort paths.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_pnl "
                    "ON closed_positions(pnl)"
                )
                # (model_version, timestamp DESC) — model-version lineage
                # queries (e.g. "show me recent trades from model v2.3.1")
                # surfaced by the dashboard's model-comparison view.
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_model_ts "
                    "ON closed_positions(model_version, timestamp DESC)"
                )
                # (exit_price) — exit-price-quantile queries used by the
                # post-hoc analytics module (``core/deep_analysis.py``).
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_cp_exit_price "
                    "ON closed_positions(exit_price)"
                )
                conn.commit()
        except Exception as e:
            log.error("[closed_positions] Init failed (%s): %s", self._db_path, e)

    # ── Writes ────────────────────────────────────────────────────────────

    async def record_closed_position(
        self,
        token_id: str,
        strategy: str,
        entry_price: float,
        exit_price: float,
        shares: float,
        pnl: float,
        holding_seconds: float,
        model_version: str = "",
        **metadata: Any,
    ) -> str:
        """
        Persist a closed position. Returns the ``position_id``.

        Required fields mirror the call signature in the task spec. Optional
        ``metadata`` kwargs are promoted to first-class columns when they
        match one of the attribution dimension names (``decision_id``,
        ``direction``, ``confidence``, ``predicted_edge``, ``p_yes``,
        ``market_mid``, ``liquidity``); everything else lands in
        ``metadata_json`` for round-tripping extra context (slug, side, etc.).

        Idempotency: an explicit ``position_id`` kwarg is honoured as the
        unique key (``INSERT OR IGNORE``). Without one, a fresh
        ``pos-{uuid4.hex}`` is generated so repeated calls produce distinct
        rows — callers that need exactly-once semantics must pass the same
        ``position_id`` (e.g. derived from the originating ``decision_id``).
        """
        position_id = str(metadata.pop("position_id", None) or f"pos-{uuid.uuid4().hex}")
        ts = float(metadata.pop("timestamp", None) or time.time())

        # Promote attribution-dimension kwargs to dedicated columns; bundle
        # everything else into metadata_json.
        attr_values: dict[str, Any] = {}
        extras: dict[str, Any] = {}
        for k, v in metadata.items():
            if k in _ATTR_COLUMNS:
                attr_values[k] = v
            else:
                extras[k] = v

        payload = json.dumps(extras, default=str) if extras else None

        # Build the INSERT in a stable column order (so the placeholder
        # tuple aligns with the column list regardless of which optional
        # kwargs the caller supplied).
        cols = [
            "timestamp", "position_id", "token_id", "strategy",
            "entry_price", "exit_price", "shares", "pnl",
            "holding_seconds", "model_version",
            "decision_id", "direction", "confidence", "predicted_edge",
            "p_yes", "market_mid", "liquidity", "metadata_json",
        ]
        vals: list[Any] = [
            ts,
            position_id,
            token_id,
            strategy,
            float(entry_price or 0.0),
            float(exit_price or 0.0),
            float(shares or 0.0),
            float(pnl or 0.0),
            float(holding_seconds or 0.0),
            model_version or "",
            attr_values.get("decision_id"),
            attr_values.get("direction"),
            _safe_float(attr_values.get("confidence")),
            _safe_float(attr_values.get("predicted_edge")),
            _safe_float(attr_values.get("p_yes")),
            _safe_float(attr_values.get("market_mid")),
            _safe_float(attr_values.get("liquidity")),
            payload,
        ]
        placeholders = ",".join(["?"] * len(cols))
        col_list = ",".join(cols)
        sql = (
            f"INSERT OR IGNORE INTO closed_positions ({col_list}) "
            f"VALUES ({placeholders})"
        )

        def _insert() -> None:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    cursor = conn.cursor()
                    cursor.execute(sql, vals)
                    conn.commit()
            except Exception as e:
                log.error(
                    "[closed_positions] record failed token=%s strategy=%s: %s",
                    token_id,
                    strategy,
                    e,
                )

        await asyncio.to_thread(_insert)
        return position_id

    # ── Reads ──────────────────────────────────────────────────────────────

    @timed_query
    async def get_closed_positions(
        self,
        limit: int = 50,
        strategy: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Return recent closed positions (most recent first).

        ``strategy=None`` (or empty string) returns across all strategies; a
        non-empty value filters to that strategy only. ``limit`` is clamped
        to ``[1, 1000]`` for safety.
        """
        limit = max(1, min(1000, int(limit)))

        def _fetch() -> list[dict[str, Any]]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    if strategy:
                        cursor.execute(
                            """
                            SELECT * FROM closed_positions
                            WHERE strategy = ?
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            (strategy, limit),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT * FROM closed_positions
                            ORDER BY timestamp DESC
                            LIMIT ?
                            """,
                            (limit,),
                        )
                    rows = [dict(r) for r in cursor.fetchall()]
            except Exception as e:
                log.error("[closed_positions] get_closed_positions failed: %s", e)
                return []

            for r in rows:
                r["data"] = _safe_json(r.pop("metadata_json", None))
            return rows

        return await asyncio.to_thread(_fetch)

    @timed_query
    async def get_closed_positions_page(
        self,
        limit: int = 50,
        strategy: str | None = None,
        cursor: str | None = None,
    ) -> "Page":
        """
        Cursor-paginated fetch of recent closed positions.

        W16-5 — wraps :func:`core.pagination.paginate_query` against the
        ``closed_positions`` table. ``SELECT *`` includes the
        ``INTEGER PRIMARY KEY`` ``id`` column, which
        :func:`paginate_query` uses as the tiebreaker for rows that
        share a ``timestamp``.

        Args:
            limit:    Page size (clamped to ``[1, 100]`` by
                      :func:`paginate_query`). The route-level ``Query``
                      constraint allows up to 500 for backward compat
                      with pre-pagination callers; the clamp protects
                      the database from a hostile caller.
            strategy: Optional strategy filter. ``None`` / empty
                      string returns rows across every strategy.
            cursor:   Opaque cursor from a previous response's
                      ``next_cursor`` field. ``None`` returns the
                      first page.

        Returns:
            :class:`core.pagination.Page` whose ``items`` carry the
            same shape as :meth:`get_closed_positions`'s rows (the
            ``metadata_json`` column is decoded into ``data`` so the
            wire payload matches the existing contract modulo the new
            ``next_cursor`` / ``has_more`` fields).
        """
        from core.pagination import Page, paginate_query

        if strategy:
            base_query = "SELECT * FROM closed_positions WHERE strategy = ?"
            base_params: tuple = (strategy,)
        else:
            base_query = "SELECT * FROM closed_positions WHERE 1=1"
            base_params = ()

        def _fetch() -> Page:
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
                        id_column="id",
                        reverse=True,
                    )
            except Exception as e:
                log.error(
                    "[closed_positions] get_closed_positions_page failed: %s",
                    e,
                )
                return Page(items=[], next_cursor=None, has_more=False)

            # Decode the ``metadata_json`` column on each row so the wire
            # payload matches the legacy ``get_closed_positions`` shape.
            for r in page.items:
                if isinstance(r, dict) and "metadata_json" in r:
                    r["data"] = _safe_json(r.pop("metadata_json", None))
            return page

        return await asyncio.to_thread(_fetch)

    @timed_query
    async def get_closed_stats(self) -> dict[str, Any]:
        """
        Aggregate P&L stats across all recorded closed positions.

        Returns::

            {
                "count":               int,
                "total_pnl":           float,
                "avg_pnl":             float,
                "median_pnl":          float,
                "win_rate":            float,   # 0..1
                "wins":                int,
                "losses":              int,
                "breakeven":           int,     # pnl == 0
                "avg_holding_seconds":float,
                "gross_profit":        float,   # sum of +pnl
                "gross_loss":          float,   # sum of |−pnl|
                "profit_factor":       float,   # gross_profit / gross_loss (None if no losses)
                "best_trade":          float,    # max pnl
                "worst_trade":         float,   # min pnl
                "avg_entry_price":     float,
                "avg_exit_price":      float,
                "total_volume_shares": float,
                "strategies_count":    int,
            }

        Empty store returns a zeroed-out dict (count=0, win_rate=0.0,
        profit_factor=None) so the API never returns ``null`` for a fresh
        deployment.
        """
        def _fetch() -> dict[str, Any]:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute(
                        """
                        SELECT
                            COUNT(*)                                      AS count,
                            COALESCE(SUM(pnl), 0.0)                       AS total_pnl,
                            COALESCE(AVG(pnl), 0.0)                       AS avg_pnl,
                            COALESCE(AVG(holding_seconds), 0.0)           AS avg_holding_seconds,
                            COALESCE(SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END), 0.0)  AS gross_profit,
                            COALESCE(SUM(CASE WHEN pnl < 0 THEN -pnl ELSE 0 END), 0.0) AS gross_loss,
                            COALESCE(MAX(pnl), 0.0)                       AS best_trade,
                            COALESCE(MIN(pnl), 0.0)                       AS worst_trade,
                            COALESCE(AVG(entry_price), 0.0)               AS avg_entry_price,
                            COALESCE(AVG(exit_price), 0.0)                AS avg_exit_price,
                            COALESCE(SUM(shares), 0.0)                    AS total_volume_shares,
                            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)      AS wins,
                            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END)      AS losses,
                            SUM(CASE WHEN pnl = 0 THEN 1 ELSE 0 END)      AS breakeven,
                            COUNT(DISTINCT strategy)                      AS strategies_count
                        FROM closed_positions
                        """
                    )
                    agg = dict(cursor.fetchone() or {})

                    # Median P&L via separate query (SQLite has no MEDIAN()).
                    cursor.execute("SELECT pnl FROM closed_positions ORDER BY pnl")
                    pnls = [r["pnl"] for r in cursor.fetchall() if r["pnl"] is not None]
                    median = _median(pnls)
            except Exception as e:
                log.error("[closed_positions] get_closed_stats failed: %s", e)
                return _empty_stats()

            count = int(agg.get("count") or 0)
            if count == 0:
                return _empty_stats()

            gross_profit = float(agg.get("gross_profit") or 0.0)
            gross_loss = float(agg.get("gross_loss") or 0.0)
            wins = int(agg.get("wins") or 0)
            losses = int(agg.get("losses") or 0)
            profit_factor = (
                None if gross_loss <= 0 else round(gross_profit / gross_loss, 4)
            )

            return {
                "count": count,
                "total_pnl": round(float(agg.get("total_pnl") or 0.0), 4),
                "avg_pnl": round(float(agg.get("avg_pnl") or 0.0), 4),
                "median_pnl": round(median, 4),
                "win_rate": round(wins / count, 4) if count else 0.0,
                "wins": wins,
                "losses": losses,
                "breakeven": int(agg.get("breakeven") or 0),
                "avg_holding_seconds": round(float(agg.get("avg_holding_seconds") or 0.0), 2),
                "gross_profit": round(gross_profit, 4),
                "gross_loss": round(gross_loss, 4),
                "profit_factor": profit_factor,
                "best_trade": round(float(agg.get("best_trade") or 0.0), 4),
                "worst_trade": round(float(agg.get("worst_trade") or 0.0), 4),
                "avg_entry_price": round(float(agg.get("avg_entry_price") or 0.0), 4),
                "avg_exit_price": round(float(agg.get("avg_exit_price") or 0.0), 4),
                "total_volume_shares": round(float(agg.get("total_volume_shares") or 0.0), 4),
                "strategies_count": int(agg.get("strategies_count") or 0),
            }

        return await asyncio.to_thread(_fetch)


# Module-level singleton (mirrors ``audit_logger`` / ``decision_ledger`` /
# ``timescale_db`` so importers can grab the instance at module import time).
closed_positions = ClosedPositionsStore()


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append closed-position inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/positions/closed
          Recent closed positions (most recent first). Query params:

          - ``limit``  (1..500, default 50)  — max rows to return
          - ``strategy`` (optional)         — filter to a single strategy

          Returns ``{count, positions[]}``. Each position row carries the
          raw attribution columns (``strategy``, ``entry_price``,
          ``exit_price``, ``shares``, ``pnl``, ``holding_seconds``,
          ``model_version``, ``decision_id``, ``direction``, ``confidence``,
          ``predicted_edge``, ``p_yes``, ``market_mid``, ``liquidity``) plus
          a decoded ``data`` key with whatever extras were stored in
          ``metadata_json``.

      GET /api/positions/closed/stats
          Aggregate P&L / win-rate / profit-factor roll-up across all
          recorded closed positions. Returns the dict shape documented on
          ``ClosedPositionsStore.get_closed_stats``.
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/positions/closed", tags=["positions"])
    async def _list_closed_positions(
        limit: int = Query(50, ge=1, le=500, description="Max positions to return"),
        strategy: str | None = Query(None, description="Filter by strategy name"),
        cursor: str | None = Query(
            None,
            description=(
                "Opaque base64 cursor from a previous response's "
                "``next_cursor`` field. Omit for the first page (newest "
                "positions). W16-5."
            ),
        ),
    ):
        """Return recent closed positions (most recent first).

        W16-5 — supports cursor-based pagination via the optional
        ``cursor`` query param. When omitted, the first page is
        returned — fully backward compatible with the pre-pagination
        wire shape (``{count, positions}`` plus the new
        ``next_cursor`` / ``has_more`` fields).
        """
        page = await closed_positions.get_closed_positions_page(
            limit=limit,
            strategy=strategy,
            cursor=cursor,
        )
        return {
            "count": len(page.items),
            "positions": page.items,
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    @app.get("/api/positions/closed/stats", tags=["positions"])
    async def _closed_positions_stats():
        """Aggregate P&L stats across all recorded closed positions."""
        return await closed_positions.get_closed_stats()


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    """Coerce to float; return None on failure (so SQLite stores NULL)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN check
        return None
    return f


def _safe_json(raw: str | None) -> Any:
    """Best-effort JSON decode for the ``metadata_json`` column."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _median(values: list[float]) -> float:
    """Compute the median of a sorted-or-unsorted list of floats."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2.0
    return s[mid]


def _empty_stats() -> dict[str, Any]:
    """Zeroed-out stats payload for an empty store / failure path."""
    return {
        "count": 0,
        "total_pnl": 0.0,
        "avg_pnl": 0.0,
        "median_pnl": 0.0,
        "win_rate": 0.0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "avg_holding_seconds": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "profit_factor": None,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "avg_entry_price": 0.0,
        "avg_exit_price": 0.0,
        "total_volume_shares": 0.0,
        "strategies_count": 0,
    }


__all__ = [
    "DB_PATH",
    "ClosedPositionsStore",
    "closed_positions",
    "register_routes",
    "timed_query",
]
