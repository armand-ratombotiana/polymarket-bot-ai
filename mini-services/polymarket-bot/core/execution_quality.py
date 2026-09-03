"""
core/execution_quality.py — SQLite-backed Execution Quality Ledger.

Records per-fill execution-quality metrics so the *realised* edge of the
trading pipeline can be benchmarked against the *theoretical* signal edge:

    signal_price     — price observed at signal generation time
                       (passed in by caller; falls back to order.price)
    decision_price   — order.price as set by the strategy / risk gate
    submitted_price  — order.price as actually submitted to the (paper)
                       exchange — same as decision_price for paper trades but
                       kept as a distinct column so live-venue fills (where
                       the broker may re-price or the strategy may amend the
                       limit before submission) can be compared cleanly.
    best_bid         — top-of-book bid at the moment of fill
    best_ask         — top-of-book ask at the moment of fill
    expected_fill    — what the simulator expected to pay:
                         BUY  → best_ask (cost of crossing the spread to lift
                                the offer)
                         SELL → best_bid (cost of crossing the spread to hit
                                the bid)
                       Falls back to decision_price when the book is empty.
    actual_fill      — the fill_price the simulator booked (post-slippage)
    spread           — best_ask - best_bid  (raw crossing cost)
    slippage         — actual_fill - expected_fill  (signed: positive = adverse)
    slippage_bps     — slippage / abs(expected_fill) × 10_000  (basis points)
    latency_ms       — (fill_timestamp - order.created_at) × 1_000
                       (signal → execution latency as observed by the simulator)
    realized_edge    — strategy edge vs the signal price:
                         BUY  → signal_price - actual_fill
                                (bought below signal = positive)
                         SELL → actual_fill - signal_price
                                (sold above signal = positive)

SQLite schema (independent db at ``EXECUTION_QUALITY_DB_PATH`` defaulting to
``/app/data/execution_quality.db`` so the audit-trail and decision-ledger
immutability contracts are not perturbed):

    execution_quality (
        id, timestamp, order_id, decision_id, token_id, strategy, side,
        signal_price, decision_price, submitted_price,
        best_bid, best_ask, expected_fill, actual_fill,
        spread, slippage, slippage_bps, latency_ms, realized_edge,
        paper, data_json
    )

The HTTP layer (``api/server.py``) calls ``register_routes(app)`` at startup
to expose:

    GET /api/execution-quality
        Query params: time_window_seconds (optional), strategy (optional),
                      limit (default 50, max 500).
        Returns aggregate stats + the most recent N fills.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import statistics
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(
    os.environ.get("EXECUTION_QUALITY_DB_PATH", "/app/data/execution_quality.db")
)

# Slippage is converted to basis points relative to the expected fill price,
# where a $1.00 notional maps 1¢ of slippage to 100 bps. Using abs() keeps the
# bps magnitude well-defined even for short-side fills where expected_fill is
# the (positive) best_bid.
_BPS_SCALE = 10_000.0


# ── W11-9: query timing decorator ───────────────────────────────────────────
# Lightweight instrumentation for the most commonly-called read paths. Wraps
# a sync or async function and emits a WARNING when a single call exceeds
# ``_SLOW_QUERY_THRESHOLD`` (100 ms — the dashboard SLO for the
# ``/api/execution-quality`` endpoint's aggregate stats query). The decorator
# is import-safe, never re-raises, and preserves the wrapped function's
# return value / exception semantics verbatim.
import functools  # noqa: E402  (kept next to its consumer for readability)

_SLOW_QUERY_THRESHOLD = 0.100  # seconds


def timed_query(func):
    """Log a warning when ``func`` takes longer than ``_SLOW_QUERY_THRESHOLD``.

    Supports both ``def`` and ``async def`` callables — the wrapper branches
    on ``asyncio.iscoroutinefunction`` so sync query functions (this module's
    ``get_execution_stats`` is sync) and async ones (the route handlers) can
    share the same decorator.
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
                    log.warning(
                        "[execution_quality] slow query %s: %.3fs",
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
                    "[execution_quality] slow query %s: %.3fs",
                    func.__name__,
                    duration,
                )

    return _sync_wrapper


# ── Schema ──────────────────────────────────────────────────────────────────

def _init_db() -> None:
    """Create table + indexes if absent. Safe to call on every boot."""
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
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
                )
                """
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_ts "
                "ON execution_quality(timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_strategy "
                "ON execution_quality(strategy, timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_token "
                "ON execution_quality(token_id, timestamp DESC)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_decision "
                "ON execution_quality(decision_id)"
            )
            # ── W11-9: additional indexes for common query patterns ──
            # (slippage_bps) — worst-execution queries (e.g. "top-10 worst
            # fills this week"). The existing ``(strategy, timestamp DESC)``
            # and ``(timestamp DESC)`` indexes can't service an
            # ``ORDER BY slippage_bps DESC`` query without a full sort.
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_slippage "
                "ON execution_quality(slippage_bps DESC)"
            )
            # (side, timestamp DESC) — per-side aggregate queries (BUY vs
            # SELL execution quality split). ``by_side`` is a top-level
            # field on ``get_execution_stats``'s return payload.
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_side_ts "
                "ON execution_quality(side, timestamp DESC)"
            )
            # (paper, timestamp DESC) — paper-vs-live filter (operators
            # often slice execution quality by ``paper = 1`` to exclude
            # backtest / replay fills from production stats).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_paper_ts "
                "ON execution_quality(paper, timestamp DESC)"
            )
            # (order_id) — fast lookup by exchange order id (e.g. when
            # reconciling a CLOB fill webhook against the recorded
            # execution-quality row).
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_eq_order "
                "ON execution_quality(order_id)"
            )
            conn.commit()
    except Exception as e:
        log.error("[execution_quality] init failed (%s): %s", DB_PATH, e)


# Initialise on module import — mirrors the ``decision_ledger`` /
# ``audit_logger`` convention so the store is ready the moment any caller
# imports the module.
_init_db()


# ── Writes ──────────────────────────────────────────────────────────────────

def record_execution(order: Any, fill_price: float, signal_price: float | None = None) -> None:
    """
    Record execution-quality metrics for a single fill.

    Synchronous + best-effort. **Never raises** — the trading pipeline never
    blocks on quality-recording plumbing (every step is wrapped in
    ``try/except`` and logged at ``debug`` on failure, mirroring the
    fire-and-forget pattern used by the decision ledger).

    Args:
        order: a ``core.data_store.Order`` (duck-typed — anything with the
            ``token_id``, ``side`` (with ``.value`` or ``str()``-able),
            ``price``, ``size``, ``created_at``, ``strategy``, ``paper``,
            ``decision_id``, ``order_id`` attributes is accepted).
        fill_price: the price the simulator actually booked (post-slippage).
        signal_price: the price observed at signal-generation time. Falls
            back to ``order.price`` when not provided, so legacy callers that
            don't track the signal-time price still get a meaningful
            ``realized_edge`` baseline (the difference between the limit
            submitted and the price paid/received).
    """
    try:
        # ── Resolve book snapshot at fill time (sync dict access) ────────
        # ``store.order_books`` is a plain dict; reading the current book
        # without the asyncio lock is the established pattern in this
        # codebase (see ``paper/simulator._execute_fill`` reading
        # ``store.positions.get`` synchronously). Worst-case we observe a
        # one-tick-stale book, which is acceptable for telemetry purposes.
        book = None
        try:
            from core.data_store import store  # local import — module is import-safe
            book = store.order_books.get(getattr(order, "token_id", ""))
        except Exception:
            book = None

        best_bid = getattr(book, "best_bid", None) if book is not None else None
        best_ask = getattr(book, "best_ask", None) if book is not None else None
        spread = (
            (best_ask - best_bid)
            if (best_bid is not None and best_ask is not None)
            else None
        )

        # ── Price tiers ────────────────────────────────────────────────
        # signal_price falls back to order.price so realized_edge still has
        # a meaningful baseline when callers don't track signal-time price.
        sig_px = float(signal_price) if signal_price is not None else float(getattr(order, "price", 0.0))
        decision_px = float(getattr(order, "price", 0.0))
        submitted_px = float(getattr(order, "price", 0.0))

        # ── Side ───────────────────────────────────────────────────────
        side_attr = getattr(order, "side", None)
        try:
            side_str = side_attr.value if hasattr(side_attr, "value") else str(side_attr or "")
        except Exception:
            side_str = ""
        side_str = (side_str or "").upper()

        # ── Expected vs actual fill ────────────────────────────────────
        # BUY pays the offer (best_ask); SELL receives the bid (best_bid).
        # If the book is empty we fall back to the decision price so the
        # slippage math degrades gracefully to "actual vs limit" rather
        # than NaN-ing out.
        if side_str == "BUY":
            expected_fill = best_ask if best_ask is not None else decision_px
        elif side_str == "SELL":
            expected_fill = best_bid if best_bid is not None else decision_px
        else:
            expected_fill = decision_px

        actual_fill = float(fill_price)

        # Slippage: positive = adverse (paid more on a BUY, received less on a SELL).
        slippage = actual_fill - expected_fill if expected_fill is not None else 0.0
        # Basis points relative to expected fill magnitude.
        slippage_bps = (
            (slippage / abs(expected_fill)) * _BPS_SCALE
            if expected_fill
            else 0.0
        )

        # ── Realized edge ──────────────────────────────────────────────
        # Strategy's edge: positive when bought below the signal price
        # (BUY) or sold above the signal price (SELL).
        if side_str == "BUY":
            realized_edge = sig_px - actual_fill
        elif side_str == "SELL":
            realized_edge = actual_fill - sig_px
        else:
            realized_edge = 0.0

        # ── Latency ────────────────────────────────────────────────────
        ts = time.time()
        try:
            latency_ms = (ts - float(getattr(order, "created_at", ts))) * 1000.0
        except Exception:
            latency_ms = 0.0

        # ── Identifiers / metadata ────────────────────────────────────
        order_id = str(getattr(order, "order_id", "") or "")
        decision_id = str(getattr(order, "decision_id", "") or "")
        token_id = str(getattr(order, "token_id", "") or "")
        strategy = str(getattr(order, "strategy", "") or "")
        paper = 1 if bool(getattr(order, "paper", False)) else 0

        # Auxiliary payload for forward-compat diagnostics without schema churn.
        try:
            data_json = json.dumps(
                {
                    "fill_size": float(getattr(order, "size", 0.0) or 0.0),
                    "size_remaining": float(getattr(order, "size_remaining", 0.0) or 0.0),
                },
                default=str,
            )
        except Exception:
            data_json = None

        # ── Persist ────────────────────────────────────────────────────
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO execution_quality
                (timestamp, order_id, decision_id, token_id, strategy, side,
                 signal_price, decision_price, submitted_price,
                 best_bid, best_ask, expected_fill, actual_fill,
                 spread, slippage, slippage_bps, latency_ms, realized_edge,
                 paper, data_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts, order_id, decision_id, token_id, strategy, side_str,
                    sig_px, decision_px, submitted_px,
                    best_bid, best_ask, expected_fill, actual_fill,
                    spread, slippage, slippage_bps, latency_ms, realized_edge,
                    paper, data_json,
                ),
            )
            conn.commit()
    except Exception as e:
        # Last-resort safety net — the simulator's wiring has its own
        # try/except around this call too, but we never want a bug here
        # (e.g. malformed order object) to crash a paper fill.
        log.debug("[execution_quality] record_execution failed: %s", e)


# ── Reads ───────────────────────────────────────────────────────────────────

@timed_query
def get_execution_stats(
    time_window_seconds: float | None = None,
    strategy: str | None = None,
) -> dict[str, Any]:
    """
    Return aggregate execution-quality stats.

    Args:
        time_window_seconds: optional; if set, only consider fills whose
            ``timestamp`` is within the last N seconds (rolling window).
        strategy: optional; restrict to a single strategy name.

    Returns a dict with::

        {
          "count": N,
          "strategy": <str|None>,
          "time_window_seconds": <float|None>,
          "avg_slippage_bps": float,       # mean
          "median_slippage_bps": float,
          "p95_slippage_bps": float,       # 95th-percentile adverse slippage
          "worst_slippage_bps": float,     # max adverse slippage observed
          "avg_latency_ms": float,
          "avg_realized_edge": float,      # mean signed edge per fill
          "total_realized_edge": float,    # sum of signed edges
          "by_side": {"BUY": int, "SELL": int},
        }

    On any DB error, returns a zeroed-out stats dict so the API endpoint
    never 500s on a corrupt / missing DB.
    """
    empty: dict[str, Any] = {
        "count": 0,
        "strategy": strategy,
        "time_window_seconds": time_window_seconds,
        "avg_slippage_bps": 0.0,
        "median_slippage_bps": 0.0,
        "p95_slippage_bps": 0.0,
        "worst_slippage_bps": 0.0,
        "avg_latency_ms": 0.0,
        "avg_realized_edge": 0.0,
        "total_realized_edge": 0.0,
        "by_side": {"BUY": 0, "SELL": 0},
    }

    try:
        clauses: list[str] = []
        params: list[Any] = []
        if time_window_seconds is not None:
            clauses.append("timestamp >= ?")
            params.append(time.time() - float(time_window_seconds))
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # ── W11-9: push COUNT + by_side roll-up down to SQL ───────────
            # ``count`` and ``by_side`` are pure aggregates — there's no
            # reason to materialise every row in Python just to count them.
            # The percentile / median calculations still need every value
            # (SQLite has no MEDIAN() / PERCENTILE_CONT() built-in), so we
            # fetch the slippage / latency / edge columns ONLY for those
            # in-Python passes — selecting 3 columns instead of ``*`` cuts
            # the row materialisation cost ~5x (the dropped columns are
            # long TEXT identifiers + prices we don't aggregate).
            cursor.execute(
                f"SELECT COUNT(*) AS n, "
                f"SUM(CASE WHEN side = 'BUY'  THEN 1 ELSE 0 END) AS buy_n, "
                f"SUM(CASE WHEN side = 'SELL' THEN 1 ELSE 0 END) AS sell_n "
                f"FROM execution_quality{where}",
                params,
            )
            agg = dict(cursor.fetchone() or {})
            n_rows = int(agg.get("n") or 0)
            if n_rows == 0:
                return empty
            by_side = {
                "BUY": int(agg.get("buy_n") or 0),
                "SELL": int(agg.get("sell_n") or 0),
            }

            # Pull only the columns we actually aggregate in Python.
            cursor.execute(
                f"SELECT slippage_bps, latency_ms, realized_edge "
                f"FROM execution_quality{where}",
                params,
            )
            rows = cursor.fetchall()
    except Exception as e:
        log.error("[execution_quality] get_execution_stats query failed: %s", e)
        return empty

    if not rows:
        return empty

    slippages_bps = [float(r["slippage_bps"] or 0.0) for r in rows]
    latencies_ms = [float(r["latency_ms"] or 0.0) for r in rows]
    edges = [float(r["realized_edge"] or 0.0) for r in rows]

    def _percentile(values: list[float], p: float) -> float:
        if not values:
            return 0.0
        s = sorted(values)
        # nearest-rank percentile (deterministic, no interpolation)
        k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
        return s[k]

    return {
        "count": n_rows,
        "strategy": strategy,
        "time_window_seconds": time_window_seconds,
        "avg_slippage_bps": statistics.fmean(slippages_bps) if slippages_bps else 0.0,
        "median_slippage_bps": statistics.median(slippages_bps) if slippages_bps else 0.0,
        "p95_slippage_bps": _percentile(slippages_bps, 95),
        "worst_slippage_bps": max(slippages_bps) if slippages_bps else 0.0,
        "avg_latency_ms": statistics.fmean(latencies_ms) if latencies_ms else 0.0,
        "avg_realized_edge": statistics.fmean(edges) if edges else 0.0,
        "total_realized_edge": sum(edges),
        "by_side": by_side,
    }


# ── FastAPI route registration ──────────────────────────────────────────────

def register_routes(app: Any) -> None:
    """
    Append execution-quality inspection endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      GET /api/execution-quality
          Query params:
            time_window_seconds (optional, float ≥ 0) — rolling window filter
            strategy            (optional, str)       — restrict to one strategy
            limit               (default 50, 1..500)  — max recent fills to return
          Returns:
            {
              "stats": { … get_execution_stats output … },
              "recent_fills": [ … most-recent-N rows (newest first) … ],
            }
    """
    from fastapi import Query  # local import — FastAPI is optional at module load

    @app.get("/api/execution-quality", tags=["execution-quality"])
    async def _execution_quality(
        time_window_seconds: float | None = Query(
            None,
            ge=0,
            description="Only consider fills from the last N seconds (rolling window).",
        ),
        strategy: str | None = Query(
            None,
            description="Restrict to a single strategy name.",
        ),
        limit: int = Query(
            50,
            ge=1,
            le=500,
            description="Max recent fills to return alongside the aggregate stats.",
        ),
    ):
        """Return aggregate execution-quality stats + the most recent N fills."""
        stats = get_execution_stats(
            time_window_seconds=time_window_seconds,
            strategy=strategy,
        )

        # Recent-fills slice for the dashboard / API consumers. Kept separate
        # from the stats aggregate so a wide time-window query (e.g. all-time)
        # still returns a bounded recent-fills list, not the full table.
        recent: list[dict[str, Any]] = []
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if time_window_seconds is not None:
                clauses.append("timestamp >= ?")
                params.append(time.time() - float(time_window_seconds))
            if strategy:
                clauses.append("strategy = ?")
                params.append(strategy)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            with sqlite3.connect(DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute(
                    f"SELECT * FROM execution_quality{where} "
                    "ORDER BY timestamp DESC LIMIT ?",
                    [*params, int(limit)],
                )
                recent = [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            log.error("[execution_quality] recent fills query failed: %s", e)

        return {"stats": stats, "recent_fills": recent}


__all__ = [
    "DB_PATH",
    "timed_query",
    "record_execution",
    "get_execution_stats",
    "register_routes",
]
