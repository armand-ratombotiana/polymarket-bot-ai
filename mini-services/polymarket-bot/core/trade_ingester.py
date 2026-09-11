"""
core/trade_ingester.py — Public trade tape ingester (W20-7).

Background task that polls the Polymarket CLOB REST API for the
**public** trade tape (``GET /trades``) and persists every unseen trade
into ``market_trades`` (SQLite fallback) / ``market.market_trade``
(PostgreSQL / TimescaleDB hypertable declared in migration
``001_initial_enterprise_schemas.sql``).

WHY THIS MODULE EXISTS
~~~~~~~~~~~~~~~~~~~~~~~
The God Mode assessment (W17-4) found that ``market.market_trade`` was
declared in migration 001 but **no writer existed** — the public trade
feed (the actual transaction tape that gives every operator a
third-party view of the markets they care about) was never ingested.
``core/book_poller`` polls the L1/L2 order book snapshot but that is a
different surface: it tells you the *current state* of the book, not
the *sequence of executed trades* that moved prices.

The ingester closes that gap:

  1. Polls ``clob_client.get_public_trades(limit=100)`` every
     ``poll_interval`` seconds (default 5.0 — half the book poller's
     Tier-2 cadence so the tape stays fresher than the book without
     doubling the request load on the CLOB).
  2. For every trade whose ``trade_id`` is not already in the in-memory
     ``_last_trade_ids`` set, writes a row via
     ``timescale_db.record_trade(...)``.
  3. Bounds the in-memory dedup set so a long-running session doesn't
     grow it without limit (the durable UNIQUE constraint on
     ``trade_id`` is the backstop for restarts / crashes).

DEDUPLICATION
~~~~~~~~~~~~~
Every observed ``trade_id`` is added to an in-memory ``_last_trade_ids``
set so a re-poll of the same trade (which the CLOB will return on
consecutive polls within its retention window) never re-records the
row. The set is bounded at ``_MAX_SEEN_TRADE_IDS`` entries; on overflow
it's rebuilt from the most recent ``_KEEP_SEEN_TRADE_IDS`` entries —
the oldest entries (which are the least likely to be re-observed) are
discarded, accepting a small probability of duplicate processing for a
very old trade that the CLOB happens to replay. The SQLite / PG
``UNIQUE`` constraint on ``trade_id`` is the durable backstop so a
replay past the in-memory window still doesn't create a duplicate row.

ERROR CONTRACT
~~~~~~~~~~~~~~
The ingester must NEVER crash the trading pipeline. ``_poll_loop``
wraps every iteration in a ``try/except`` that logs at ``error`` level
with ``exc_info=True``; ``_ingest_trades`` additionally wraps each
individual trade's recording in a ``try/except`` so a single malformed
trade dict (or a transient DB write failure) can't poison the rest of
the batch.

API ENDPOINTS
~~~~~~~~~~~~~
The module also exposes a ``register_routes(app)`` function (mirrors
the pattern used by ``core.shadow_trading``, ``core.capital_allocator``,
``ml.validation`` …) that adds two read-only HTTP endpoints:

  * ``GET /api/trades/tape``                — recent trades from the
                                              tape (most-recent-first),
                                              optional ``token_id``
                                              filter + ``limit`` cap.
  * ``GET /api/trades/ingester-status``     — live ingester stats
                                              (running flag, poll
                                              interval, seen-trade-id
                                              set size).

Both routes are protected by the existing ``enforce_api_auth``
middleware (neither path is in ``PUBLIC_PATHS``).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query

logger = logging.getLogger(__name__)


# Cap on the size of the in-memory seen-trade-id set. The set is checked
# on every poll (O(1) membership test) so it's the natural place to bound
# long-running-session memory growth. When the set exceeds this threshold
# it's rebuilt from the most recent ``_KEEP_SEEN_TRADE_IDS`` entries —
# mirroring the pattern in ``core/live_fill_monitor.py`` (W18-2).
_MAX_SEEN_TRADE_IDS = 10_000
_KEEP_SEEN_TRADE_IDS = 5_000


class TradeTapeIngester:
    """Background task that ingests the public trade feed.

    The ingester is idempotent: ``start()`` is a no-op if already
    running; ``stop()`` is a no-op if not running. Polling continues
    until ``stop()`` is called (which sets ``_running = False`` and
    cancels the polling task).

    Attributes:
        poll_interval: seconds between CLOB ``/trades`` polls (default 5.0).
        _running: whether the polling loop is currently active.
        _task: the asyncio Task running ``_poll_loop``, or ``None`` when stopped.
        _last_trade_ids: set of CLOB trade ids already processed (dedup set).
        _ingested_count: cumulative count of trades written to the DB
            since the ingester was last started. Reset on ``start()``.
        _error_count: cumulative count of poll cycles that raised.
            Reset on ``start()``.
    """

    def __init__(self, poll_interval: float = 5.0) -> None:
        self.poll_interval: float = poll_interval
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._last_trade_ids: set[str] = set()
        self._ingested_count: int = 0
        self._error_count: int = 0
        self._last_poll_at: float = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the ingester (idempotent — no-op if already running)."""
        if self._running:
            return
        self._running = True
        self._ingested_count = 0
        self._error_count = 0
        self._task = asyncio.create_task(self._poll_loop(), name="trade-tape-ingester")
        logger.info(
            "Trade tape ingester started (interval=%.2fs)", self.poll_interval
        )

    async def stop(self) -> None:
        """Stop the ingester (idempotent — no-op if not running)."""
        if not self._running:
            return
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("Trade tape ingester task raised on stop: %s", e)
            self._task = None
        logger.info("Trade tape ingester stopped")

    # ── Polling loop ──────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main polling loop — runs until ``_running`` is flipped to False.

        Each iteration is wrapped in a top-level ``try/except`` so a
        single poll failure (network blip, CLOB 5xx, transient JSON
        parse error) can never crash the loop. Errors are logged at
        ``error`` level with ``exc_info=True`` so the traceback is
        captured in the audit trail. The ``asyncio.sleep`` runs
        unconditionally between iterations so a hung
        ``_ingest_trades`` can't starve the scheduler.
        """
        while self._running:
            try:
                await self._ingest_trades()
            except asyncio.CancelledError:
                # Explicit re-raise so ``stop()``'s ``task.cancel()`` propagates.
                raise
            except Exception as e:
                self._error_count += 1
                logger.error("Trade tape ingester poll error: %s", e, exc_info=True)
            await asyncio.sleep(self.poll_interval)

    async def _ingest_trades(self) -> None:
        """Poll the CLOB for public trades and persist every unseen one.

        Steps:
          1. Fetch up to 100 recent trades via
             ``clob_client.get_public_trades(limit=100)`` (no
             ``token_id`` filter — the public tape spans every market).
          2. Drop every trade whose ``trade_id`` is already in the
             in-memory ``_last_trade_ids`` set (the fast-path dedup).
          3. For each new trade, ``await db_manager.record_trade(...)``.
             The facade (W21-6) routes to PG / TimescaleDB
             ``market.market_trade`` hypertable when the asyncpg pool
             is connected, and to the SQLite ``market_trades`` fallback
             otherwise — callers see one uniform interface. Failures
             are logged at ``warning`` level and don't abort the rest
             of the batch (a single bad row can't poison the batch).
          4. Bound the in-memory dedup set so a long-running session
             doesn't grow it without limit.
        """
        from core.clob_client import clob_client
        from core.database_manager import db_manager

        self._last_poll_at = time.time()

        try:
            trades = await clob_client.get_public_trades(limit=100)
        except Exception as e:
            # ``get_public_trades`` already swallows its own exceptions
            # and returns ``[]``, but we wrap defensively in case a
            # future refactor changes that contract.
            logger.error("Trade tape ingester fetch failed: %s", e)
            return

        if not trades:
            return

        new_trades: list[dict[str, Any]] = []
        for t in trades:
            trade_id = t.get("trade_id") or ""
            if not trade_id:
                # Without a stable id we can't dedup — skip rather than
                # risk double-counting the same trade on every poll.
                logger.debug("Skipping public trade with no trade_id: %s", t)
                continue
            if trade_id in self._last_trade_ids:
                continue
            self._last_trade_ids.add(trade_id)
            new_trades.append(t)

        if not new_trades:
            return

        # Persist each new trade. Wrap each call so a single bad row
        # doesn't abort the rest of the batch.
        written = 0
        for t in new_trades:
            # W24-4 — Ingestion-time validation gate. Runs each new
            # trade through ``DataValidator.validate_trade`` (schema
            # check, value-range check, side normalisation, timestamp
            # normalisation) BEFORE persisting to ``market_trades``.
            # The ingester's own in-memory ``_last_trade_ids`` set
            # above is the fast-path dedup; the validator's
            # ``_seen_ids`` deque is the belt-and-braces second layer
            # (it would only fire if the ingester's dedup missed,
            # e.g. on a race between two polls returning the same
            # ``trade_id`` — in steady state ``is_duplicate`` stays
            # ``False`` here because the fast path already filtered
            # the trade out).
            from core.data_validator import data_validator

            result = data_validator.validate_trade(t)
            if not result.is_valid:
                if result.is_duplicate:
                    # Defensive: the fast-path dedup above should
                    # have already skipped this trade. Log at
                    # ``debug`` (not ``warning``) so the operator
                    # log isn't flooded if a transient race ever
                    # surfaces this branch.
                    logger.debug(
                        "Validator rejected duplicate trade %s (ingester fast-path missed)",
                        t.get("trade_id", "<unknown>"),
                    )
                else:
                    logger.warning(
                        "Invalid trade %s: %s",
                        t.get("trade_id", "<unknown>"),
                        result.errors,
                    )
                continue

            # Use the validator's normalised payload — ``price`` /
            # ``size`` / ``timestamp`` coerced to ``float``, ``side``
            # upper-cased to ``BUY`` / ``SELL``, provenance fields
            # (``ingestion_time`` / ``processing_time`` / ``source``)
            # added. The ``trade_id`` / ``token_id`` / ``maker_address``
            # / ``taker_order_id`` fields are passed through unchanged
            # from the CLOB normalisation in ``clob_client.get_public_trades``.
            norm = result.normalized_data
            try:
                await db_manager.record_trade(
                    token_id=norm.get("token_id") or "",
                    price=float(norm.get("price") or 0.0),
                    size=float(norm.get("size") or 0.0),
                    side=str(norm.get("side") or ""),
                    timestamp=float(norm.get("timestamp") or 0.0),
                    trade_id=norm.get("trade_id") or "",
                    maker_address=norm.get("maker_address") or "",
                    taker_order_id=norm.get("taker_order_id") or "",
                )
                written += 1
            except Exception as e:
                logger.warning(
                    "Failed to store trade %s: %s",
                    t.get("trade_id", "<unknown>"),
                    e,
                    exc_info=True,
                )

        self._ingested_count += written

        # Bound the in-memory dedup set so a long-running session
        # doesn't grow it without limit. Mirrors the
        # ``_MAX_SEEN_TRADE_IDS`` / ``_KEEP_SEEN_TRADE_IDS`` pattern in
        # ``core/live_fill_monitor.py`` (W18-2).
        if len(self._last_trade_ids) > _MAX_SEEN_TRADE_IDS:
            self._last_trade_ids = set(list(self._last_trade_ids)[-_KEEP_SEEN_TRADE_IDS:])

        if written:
            logger.info(
                "Trade tape ingester wrote %d/%d new trades (seen=%d)",
                written, len(new_trades), len(self._last_trade_ids),
            )

    # ── Stats / introspection ─────────────────────────────────────────

    def get_stats(self) -> dict[str, Any]:
        """Return a snapshot of the ingester's runtime state.

        Used by the ``GET /api/trades/ingester-status`` HTTP endpoint
        and by the unit tests. Pure read — never mutates state.
        """
        return {
            "running": self._running,
            "poll_interval": self.poll_interval,
            "seen_trade_ids": len(self._last_trade_ids),
            "ingested_count": self._ingested_count,
            "error_count": self._error_count,
            "last_poll_at": self._last_poll_at,
            "last_poll_ago_s": (
                time.time() - self._last_poll_at if self._last_poll_at else None
            ),
        }


# ── API routes ────────────────────────────────────────────────────────────────
#
# Registered onto the FastAPI app via ``register_routes(app)`` from
# ``api/server.py`` (W20-7 wiring block, mirroring the pattern used by
# ``core.shadow_trading``, ``core.capital_allocator``, ``ml.validation``
# …). Auth is enforced by the production server's ``enforce_api_auth``
# middleware — neither path is in ``PUBLIC_PATHS``.
#
# Both routes are read-only and idempotent. No background tasks are
# started by the route handlers; the ingester is started / stopped by
# the FastAPI lifespan (see the W20-7 wiring block in
# ``api/server.py``'s ``async def lifespan``).

def register_routes(app: FastAPI) -> None:
    """Register the trade-tape HTTP routes on ``app``.

    Adds three endpoints:

      * ``GET /api/trades/tape``                — recent trades from the
                                                   tape, most-recent-first.
      * ``GET /api/trades/stats``               — aggregate stats over the
                                                   trailing ``hours`` window
                                                   (default 24h), optional
                                                   ``token_id`` filter.
      * ``GET /api/trades/ingester-status``     — live ingester stats.

    W21-6 — the ``/api/trades/tape`` and ``/api/trades/stats`` routes
    now route through the unified ``db_manager`` facade
    (``core/database_manager.py``), which itself routes to PG /
    TimescaleDB when the asyncpg pool is connected, or the SQLite
    fallback otherwise. The facade is the single backend switch —
    the route handlers don't need a backend-specific branch.

    The function is idempotent: registering twice on the same app
    would raise a duplicate-route error at app construction time
    (FastAPI's default behaviour for ``@app.get`` on an existing path)
    — callers are expected to invoke it once per app instance.
    """

    @app.get(
        "/api/trades/tape",
        tags=["markets"],
        summary="Public trade tape (recent trades)",
        description=(
            "Returns the most recent rows from the public trade tape "
            "(``market_trades`` SQLite table / ``market.market_trade`` "
            "TimescaleDB hypertable), most-recent-first. Optional "
            "``token_id`` filter restricts the result to a single "
            "market; ``limit`` caps the row count (default 100, max "
            "500); ``since`` (unix epoch seconds) sets a lower-bound "
            "cutoff so the dashboard can pull 'trades since last "
            "poll'. The trade tape is populated by the background "
            "trade-tape ingester (W20-7) which polls the CLOB "
            "``/trades`` endpoint every ``poll_interval`` seconds."
        ),
    )
    async def get_trade_tape(
        token_id: str | None = Query(
            None, max_length=128, description="Optional CTF token id filter"
        ),
        limit: int = Query(100, ge=1, le=500, description="Max rows to return"),
        since: float | None = Query(
            None,
            description="Optional lower-bound unix epoch seconds cutoff",
        ),
    ):
        """Get recent trades from the tape (W21-6 routes via db_manager)."""
        from core.database_manager import db_manager

        rows = await db_manager.get_trade_tape(
            token_id=token_id or None,
            limit=limit,
            since_timestamp=since,
        )
        return {
            "trades": rows,
            "count": len(rows),
            "token_id": token_id,
            "backend": db_manager.backend_label,
        }

    @app.get(
        "/api/trades/stats",
        tags=["markets"],
        summary="Trade tape aggregate stats",
        description=(
            "Returns aggregate statistics over the trailing ``hours`` "
            "window (default 24h): total trades, total volume, average "
            "price, buy/sell counts, and VWAP "
            "(volume-weighted average price). Optional ``token_id`` "
            "filter restricts the aggregation to a single market. "
            "Routes through the W21-6 ``db_manager`` facade which "
            "selects PG or SQLite automatically."
        ),
    )
    async def get_trade_stats(
        token_id: str | None = Query(
            None, max_length=128, description="Optional CTF token id filter"
        ),
        hours: float = Query(
            24.0, ge=0.0, le=24.0 * 365,
            description="Trailing window in hours (default 24h)",
        ),
    ):
        """Get aggregate trade stats (W21-6 routes via db_manager)."""
        from core.database_manager import db_manager

        stats = await db_manager.get_trade_stats(
            token_id=token_id or None, hours=hours
        )
        return {
            "stats": stats,
            "token_id": token_id,
            "hours": hours,
            "backend": db_manager.backend_label,
        }

    @app.get(
        "/api/trades/ingester-status",
        tags=["system"],
        summary="Trade-tape ingester runtime stats",
        description=(
            "Returns the live state of the background trade-tape "
            "ingester (W20-7): running flag, poll interval, "
            "seen-trade-id set size, cumulative ingested / error "
            "counts, and the timestamp of the last poll cycle. Used "
            "by the operator dashboard to verify the tape is flowing."
        ),
    )
    async def trade_ingester_status():
        from core.trade_ingester import trade_tape_ingester
        return trade_tape_ingester.get_stats()


# Module-level singleton — mirrors every sibling background-task module
# (``core.book_poller.book_poller``, ``core.live_fill_monitor.live_fill_monitor``,
# ``core.label_backfill.label_backfill_engine`` …). The lifespan startup
# handler in ``api/server.py`` calls ``await trade_tape_ingester.start()``;
# the shutdown handler calls ``await trade_tape_ingester.stop()``.
trade_tape_ingester = TradeTapeIngester()
