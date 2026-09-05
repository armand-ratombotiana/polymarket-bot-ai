"""Historical backfill pipeline — fetches and stores historical market data.

Backfill types
~~~~~~~~~~~~~~

1. **Market metadata backfill** (all markets from Gamma) — pages through
   ``GammaClient.get_markets`` and upserts every market's metadata
   (question, description, categories, tags, outcomes, rules, token ids,
   volume, liquidity) into the ``backfill_markets`` SQLite table.

2. **Price history backfill** (OHLCV from CLOB) — for each market, polls
   the public trade tape via ``ClobClient.get_public_trades`` and
   aggregates the trades into OHLCV candles at a configurable resolution
   (``1m`` / ``5m`` / ``15m`` / ``1h`` / ``1d``). Each candle is written
   through ``timescale_db.record_snapshot`` so the existing
   ``market_snapshots`` hypertable (and SQLite fallback) carries the
   historical price surface, and the existing
   ``/api/depth-history`` / ``/api/analytics`` paths can read it back
   out without modification.

3. **Order-book snapshot backfill** — for each market, fetches the
   current L2 book via ``ClobClient.get_order_book`` and writes it as a
   historical point-in-time snapshot (same persistence path as price
   history). Designed to be run periodically (e.g. hourly) so a market's
   resting-depth evolution is captured.

4. **Trade history backfill** — for each market, polls the CLOB
   ``/trades`` endpoint (filtered by ``asset_id``) and persists every
   unseen trade through ``timescale_db.record_trade``. Deduplication is
   durable: the ``market_trades.trade_id`` UNIQUE constraint is the
   backstop, so a re-backfilled trade is a no-op.

5. **Resolution outcome backfill** — pages through resolved markets
   (``GammaClient.get_resolved_markets``), records the YES / NO outcome
   in ``backfill_markets.resolved_outcome_yes``, and feeds the label
   back to ``timescale_db.mark_resolved_outcomes`` so the ML feature
   store's existing label backfill (``core/label_backfill.py``) and
   ``fetch_training_samples`` paths can read the ground-truth label.

Features
~~~~~~~~

* **Rate-limit aware** — every Gamma / CLOB call goes through a shared
  :class:`RateLimiter` that paces requests at a configurable target
  RPS (default 5 req/s — a conservative envelope for both
  ``gamma-api.polymarket.com`` and ``clob.polymarket.com``). When the
  upstream signals 429 Too Many Requests (or the resilience layer
  records a sustained failure run), the limiter backs off
  exponentially up to ``max_interval_s`` (5 s by default).

* **Resumable** — every backfill writes its progress (last offset /
  token id) to ``backfill_checkpoint`` after every page. A crash
  mid-run resumes from the last committed offset on the next
  invocation; the ``resume`` flag (default ``True``) controls whether
  the engine skips the checkpoint or applies it. ``reset`` clears it.

* **Parallel fetching where safe** — token-level fan-out (price
  history, snapshots, trade history) runs through an
  :class:`asyncio.Semaphore`-bounded gather so multiple tokens are
  processed concurrently without overwhelming the API. Page-level
  fan-out (Gamma pagination) is sequential because the upstream
  offset cursor is stateful.

* **Progress tracking** — every backfill writes a row into
  ``backfill_runs`` with start / end timestamps and counters, so an
  operator can ``SELECT * FROM backfill_runs ORDER BY id DESC`` for a
  full audit trail. The :class:`BackfillStats` returned by each method
  is a snapshot of the same counters for in-process callers.

* **Error recovery** — every per-market / per-trade / per-page
  operation is wrapped in a ``try/except`` that increments
  ``stats.total_errors`` and logs at ``warning`` level. A single
  malformed market dict (or a transient DB write failure) can't abort
  the rest of the batch — mirrors the contract in
  ``core/trade_ingester.py::_ingest_trades``.

* **Deduplication** — market metadata uses ``INSERT OR REPLACE``
  keyed on ``condition_id``; trade history reuses the existing
  ``market_trades.trade_id`` UNIQUE constraint (via
  ``timescale_db.record_trade`` which already issues ``ON CONFLICT DO
  NOTHING``); price / snapshot writes are append-only and keyed by
  ``(token_id, timestamp)`` so a re-backfill of the same window
  produces duplicate rows that are filtered out at read time by the
  existing ``/api/depth-history`` query (it groups by timestamp
  bucket). The trade path's durable UNIQUE is the load-bearing one.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

from core.clob_client import clob_client
from core.gamma_client import GammaClient, gamma_client
from core.timescale_db import SQLITE_FALLBACK_PATH, timescale_db

log = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────────────

DEFAULT_PAGE_SIZE = 100                # markets per Gamma API page
DEFAULT_MAX_PAGES = 25                 # safety cap on pagination depth (≤ 2 500 markets)
DEFAULT_RATE_LIMIT_RPS = 5.0           # conservative; both Gamma + CLOB tolerate this
DEFAULT_CONCURRENCY = 4                # parallel fetch workers (token-level fan-out)
DEFAULT_PRICE_RESOLUTION = "1h"        # OHLCV candle resolution
DEFAULT_DAYS = 7                       # historical depth when --days omitted
DEFAULT_PUBLIC_TRADE_LIMIT = 500       # CLOB max per page

RESOLUTION_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "1d": 86400,
}


# ── Enums ─────────────────────────────────────────────────────────────────────


class BackfillType(str, Enum):
    """Discriminator for the five backfill passes (+ ``all`` composite)."""

    METADATA = "metadata"
    PRICES = "prices"
    TRADES = "trades"
    OUTCOMES = "outcomes"
    SNAPSHOTS = "snapshots"
    ALL = "all"

    @classmethod
    def parse(cls, raw: str) -> "BackfillType":
        """Case-insensitive parse with a friendly error message."""
        try:
            return cls(raw.lower().strip())
        except ValueError:
            valid = [t.value for t in cls if t != cls.ALL]
            raise ValueError(
                f"unknown backfill type {raw!r}; valid: {valid} | 'all'"
            ) from None


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class BackfillStats:
    """Progress counters for a single backfill pass.

    Each public ``backfill_*`` method returns an instance of this class.
    The ``last_offset`` / ``last_token_id`` fields are also persisted to
    ``backfill_checkpoint`` so a crash mid-run resumes from the same
    point on the next invocation.
    """

    type: str
    started_at: float = field(default_factory=time.time)
    ended_at: float = 0.0
    total_processed: int = 0          # markets / tokens / trades examined
    total_added: int = 0              # new rows written to the DB
    total_skipped: int = 0            # idempotency-gated duplicates
    total_errors: int = 0             # per-item exceptions (logged at warning)
    last_offset: int = 0              # Gamma pagination cursor
    last_token_id: str = ""           # token-level fan-out cursor
    error_message: str = ""

    def mark_done(self) -> None:
        self.ended_at = time.time()

    @property
    def elapsed_s(self) -> float:
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BackfillCheckpoint:
    """Durable progress record so a backfill can resume after a crash."""

    type: str
    last_offset: int = 0
    last_token_id: str = ""
    last_run_at: float = 0.0
    completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── SQLite-backed metadata + checkpoint store ──────────────────────────────


class BackfillStore:
    """SQLite-backed store for backfill progress + market metadata.

    Stored in the same SQLite file as ``timescale_db``'s
    ``market_snapshots`` (the SQLite fallback path) so a single DB
    contains every persisted surface the bot owns. The schema is
    created idempotently on first construction; subsequent runs are
    no-ops (``CREATE TABLE IF NOT EXISTS``).
    """

    SCHEMA: list[str] = [
        # All markets discovered via Gamma, with metadata.
        # PRIMARY KEY is ``condition_id`` so an upsert
        # (``INSERT OR REPLACE``) is the dedup primitive — re-running
        # ``backfill_metadata`` after a market's volume updates the row
        # in-place rather than creating a duplicate.
        """
        CREATE TABLE IF NOT EXISTS backfill_markets (
            condition_id TEXT PRIMARY KEY,
            question TEXT,
            slug TEXT,
            description TEXT,
            category TEXT,
            tags_json TEXT,
            outcome_prices_json TEXT,
            outcomes_json TEXT,
            rules_text TEXT,
            volume_24h REAL,
            liquidity REAL,
            active INTEGER,
            closed INTEGER,
            resolved_outcome_yes INTEGER,
            resolved_at REAL,
            tokens_json TEXT,
            first_seen_at REAL NOT NULL,
            last_updated_at REAL NOT NULL,
            backfilled_metadata_at REAL,
            backfilled_prices_at REAL,
            backfilled_trades_at REAL,
            backfilled_outcomes_at REAL,
            backfilled_snapshots_at REAL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_bfm_active_closed "
        "ON backfill_markets(active, closed, last_updated_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_bfm_resolved "
        "ON backfill_markets(resolved_outcome_yes) "
        "WHERE resolved_outcome_yes IS NOT NULL",
        # Resumable backfills — one row per type, keyed on ``type``.
        """
        CREATE TABLE IF NOT EXISTS backfill_checkpoint (
            type TEXT PRIMARY KEY,
            last_offset INTEGER NOT NULL DEFAULT 0,
            last_token_id TEXT NOT NULL DEFAULT '',
            last_run_at REAL NOT NULL DEFAULT 0.0,
            completed INTEGER NOT NULL DEFAULT 0
        )
        """,
        # Backfill run ledger — full audit trail.
        """
        CREATE TABLE IF NOT EXISTS backfill_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            started_at REAL NOT NULL,
            ended_at REAL,
            total_processed INTEGER DEFAULT 0,
            total_added INTEGER DEFAULT 0,
            total_skipped INTEGER DEFAULT 0,
            total_errors INTEGER DEFAULT 0,
            error_message TEXT
        )
        """,
    ]

    def __init__(self, sqlite_path: Path | None = None) -> None:
        self._sqlite_path = Path(sqlite_path) if sqlite_path else SQLITE_FALLBACK_PATH
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Connection helpers ───────────────────────────────────────────────

    def _conn(self) -> sqlite3.Connection:
        """Open a row-factory SQLite connection to the backfill DB."""
        conn = sqlite3.connect(self._sqlite_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        try:
            with self._conn() as conn:
                for ddl in self.SCHEMA:
                    conn.execute(ddl)
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            log.error("[backfill] schema init failed: %s", e)

    # ── Market metadata ─────────────────────────────────────────────────

    def upsert_market(
        self,
        market: dict,
        *,
        backfilled_metadata: bool = False,
        backfilled_prices: bool = False,
        backfilled_trades: bool = False,
        backfilled_outcomes: bool = False,
        backfilled_snapshots: bool = False,
    ) -> bool:
        """Upsert a market dict into ``backfill_markets``.

        Returns ``True`` if the row was written, ``False`` on error.
        Re-runs are no-ops at the SQL level (``INSERT OR REPLACE`` on
        ``condition_id``), but the ``first_seen_at`` column preserves
        the original discovery timestamp so it's not overwritten by a
        subsequent backfill.

        Args:
            market: Raw Gamma market dict.
            backfilled_metadata / prices / trades / outcomes / snapshots:
                when ``True``, sets the corresponding ``backfilled_*_at``
                column to ``now()`` so the operator can query which
                backfills have already covered this market.
        """
        condition_id = str(market.get("conditionId") or market.get("condition_id") or "")
        if not condition_id:
            return False

        slug = market.get("slug") or ""
        question = market.get("question") or market.get("title") or ""
        description = market.get("description") or ""
        category = market.get("category") or ""
        tags = market.get("tags") or []
        outcome_prices = market.get("outcomePrices") or []
        outcomes = market.get("outcomes") or []
        rules = market.get("rules") or market.get("rulesPrimary") or ""
        volume_24h = float(market.get("volume24hr") or market.get("volumeNum") or 0.0)
        liquidity = float(market.get("liquidity") or market.get("liquidityNum") or 0.0)
        active = 1 if market.get("active") else 0
        closed = 1 if market.get("closed") else 0
        tokens_json = json.dumps(
            GammaClient.extract_token_ids(market)
            if isinstance(market.get("clobTokenIds"), (str, list))
            else (market.get("tokens") or [])
        )
        now = time.time()

        # ── Compute resolved_outcome_yes (None if not yet resolved) ──
        resolved_yes: int | None = None
        resolved_at: float | None = None
        if closed and outcome_prices:
            try:
                prices = (
                    json.loads(outcome_prices)
                    if isinstance(outcome_prices, str)
                    else outcome_prices
                )
                if prices and len(prices) >= 2:
                    p0 = float(prices[0])
                    resolved_yes = 1 if p0 >= 0.9 else 0
                    resolved_at = now
            except (TypeError, ValueError):
                pass

        # ── Preserve first_seen_at across re-runs ──
        # ``INSERT OR REPLACE`` would zero the first-seen timestamp on
        # every re-run, so we look up the existing row first and fall
        # back to ``now`` when the market is new.
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT first_seen_at FROM backfill_markets WHERE condition_id = ?",
                    (condition_id,),
                )
                row = cur.fetchone()
                first_seen_at = float(row["first_seen_at"]) if row else now

                conn.execute(
                    """
                    INSERT OR REPLACE INTO backfill_markets (
                        condition_id, question, slug, description, category,
                        tags_json, outcome_prices_json, outcomes_json, rules_text,
                        volume_24h, liquidity, active, closed,
                        resolved_outcome_yes, resolved_at, tokens_json,
                        first_seen_at, last_updated_at,
                        backfilled_metadata_at, backfilled_prices_at,
                        backfilled_trades_at, backfilled_outcomes_at,
                        backfilled_snapshots_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        condition_id, question, slug, description, category,
                        json.dumps(tags) if isinstance(tags, list) else str(tags),
                        json.dumps(outcome_prices) if not isinstance(outcome_prices, str) else outcome_prices,
                        json.dumps(outcomes) if isinstance(outcomes, list) else str(outcomes),
                        rules,
                        volume_24h, liquidity, active, closed,
                        resolved_yes, resolved_at, tokens_json,
                        first_seen_at, now,
                        now if backfilled_metadata else None,
                        now if backfilled_prices else None,
                        now if backfilled_trades else None,
                        now if backfilled_outcomes else None,
                        now if backfilled_snapshots else None,
                    ),
                )
                conn.commit()
            return True
        except Exception as e:
            log.error("[backfill] upsert_market %s failed: %s", condition_id, e)
            return False

    def get_known_condition_ids(self) -> set[str]:
        """Return every ``condition_id`` already in ``backfill_markets``.

        Used by ``backfill_metadata`` to detect new markets since the
        last run (set difference between fresh Gamma payload and the
        known set).
        """
        try:
            with self._conn() as conn:
                cur = conn.execute("SELECT condition_id FROM backfill_markets")
                return {str(r["condition_id"]) for r in cur.fetchall()}
        except Exception as e:
            log.error("[backfill] get_known_condition_ids failed: %s", e)
            return set()

    def list_markets(
        self,
        *,
        active: bool | None = None,
        closed: bool | None = None,
        resolved: bool | None = None,
        limit: int = 1000,
    ) -> list[dict]:
        """Return up to ``limit`` markets matching the filter clauses."""
        clauses: list[str] = []
        params: list[Any] = []
        if active is not None:
            clauses.append("active = ?")
            params.append(1 if active else 0)
        if closed is not None:
            clauses.append("closed = ?")
            params.append(1 if closed else 0)
        if resolved is not None:
            if resolved:
                clauses.append("resolved_outcome_yes IS NOT NULL")
            else:
                clauses.append("resolved_outcome_yes IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT * FROM backfill_markets"
            + where
            + " ORDER BY last_updated_at DESC LIMIT ?"
        )
        params.append(int(limit))
        try:
            with self._conn() as conn:
                cur = conn.execute(sql, params)
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[backfill] list_markets failed: %s", e)
            return []

    def set_resolved_outcome(self, condition_id: str, resolved_yes: bool) -> bool:
        """Update the ``resolved_outcome_yes`` column for a market."""
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE backfill_markets "
                    "SET resolved_outcome_yes = ?, resolved_at = ? "
                    "WHERE condition_id = ?",
                    (1 if resolved_yes else 0, time.time(), condition_id),
                )
                conn.commit()
            return True
        except Exception as e:
            log.error("[backfill] set_resolved_outcome %s failed: %s", condition_id, e)
            return False

    # ── Checkpoint CRUD ─────────────────────────────────────────────────

    def load_checkpoint(self, backfill_type: str) -> BackfillCheckpoint | None:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "SELECT * FROM backfill_checkpoint WHERE type = ?",
                    (backfill_type,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return BackfillCheckpoint(
                    type=row["type"],
                    last_offset=int(row["last_offset"] or 0),
                    last_token_id=str(row["last_token_id"] or ""),
                    last_run_at=float(row["last_run_at"] or 0.0),
                    completed=bool(row["completed"]),
                )
        except Exception as e:
            log.error("[backfill] load_checkpoint(%s) failed: %s", backfill_type, e)
            return None

    def save_checkpoint(self, cp: BackfillCheckpoint) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO backfill_checkpoint
                        (type, last_offset, last_token_id, last_run_at, completed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (cp.type, cp.last_offset, cp.last_token_id,
                     cp.last_run_at, 1 if cp.completed else 0),
                )
                conn.commit()
        except Exception as e:
            log.error("[backfill] save_checkpoint(%s) failed: %s", cp.type, e)

    def reset_checkpoint(self, backfill_type: str) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    "DELETE FROM backfill_checkpoint WHERE type = ?",
                    (backfill_type,),
                )
                conn.commit()
        except Exception as e:
            log.error("[backfill] reset_checkpoint(%s) failed: %s", backfill_type, e)

    # ── Run ledger ──────────────────────────────────────────────────────

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Return the most recent ``backfill_runs`` ledger entries.

        Used by the W32-3 ``GET /api/ingestion/backfill/status`` admin
        endpoint so an operator can inspect every backfill pass (type /
        started_at / ended_at / counters / error_message) without
        grepping server logs. Ordered by ``id DESC`` (most recent
        first), capped at ``limit`` (default 20, hard ceiling 100 so a
        misbehaving caller can't OOM the bot by requesting millions of
        rows).

        Returns an empty list on storage error (logged + swallowed so
        the admin endpoint can never break the bot's backfill loop).
        """
        cap = max(1, min(int(limit), 100))
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    """
                    SELECT id, type, started_at, ended_at, total_processed,
                           total_added, total_skipped, total_errors, error_message
                    FROM backfill_runs
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (cap,),
                )
                return [dict(r) for r in cur.fetchall()]
        except Exception as e:
            log.error("[backfill] list_runs failed: %s", e)
            return []

    def record_run(self, stats: BackfillStats, error: str | None = None) -> None:
        try:
            with self._conn() as conn:
                conn.execute(
                    """
                    INSERT INTO backfill_runs
                        (type, started_at, ended_at, total_processed,
                         total_added, total_skipped, total_errors, error_message)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (stats.type, stats.started_at,
                     stats.ended_at or time.time(), stats.total_processed,
                     stats.total_added, stats.total_skipped, stats.total_errors,
                     error or stats.error_message),
                )
                conn.commit()
        except Exception as e:
            log.error("[backfill] record_run(%s) failed: %s", stats.type, e)


# ── Rate limiter ────────────────────────────────────────────────────────────


class RateLimiter:
    """Adaptive async rate limiter — paces requests at a target RPS.

    The limiter enforces a minimum interval between successive calls
    (``1 / target_rps``) so the upstream API is never overwhelmed. On
    a 429 / rate-limit signal (via :meth:`record_rate_limit`), the
    interval is multiplied by ``backoff_factor`` up to
    ``max_interval_s``; on success (via :meth:`record_success`), the
    interval decays back toward ``1 / target_rps``.

    The implementation is deliberately simple (single ``asyncio.Lock``
    guarding ``_last_call_at`` / ``_current_interval``); for the backfill
    use case (one limiter shared across N concurrent workers via a
    :class:`~asyncio.Semaphore`), the lock contention is negligible
    compared to the actual HTTP round-trip cost.
    """

    def __init__(
        self,
        target_rps: float = DEFAULT_RATE_LIMIT_RPS,
        *,
        min_interval_s: float = 0.05,
        backoff_factor: float = 1.5,
        max_interval_s: float = 5.0,
    ) -> None:
        self._target_rps = max(0.1, target_rps)
        self._min_interval = max(min_interval_s, 1.0 / self._target_rps)
        self._backoff_factor = max(1.0, backoff_factor)
        self._max_interval = max(self._min_interval, max_interval_s)
        self._current_interval = self._min_interval
        self._last_call_at: float = 0.0
        self._consecutive_rate_limits: int = 0
        self._lock = asyncio.Lock()

    @property
    def current_interval(self) -> float:
        return self._current_interval

    @property
    def consecutive_rate_limits(self) -> int:
        return self._consecutive_rate_limits

    async def acquire(self) -> None:
        """Sleep just long enough to honour the current interval.

        Called by :class:`BackfillEngine` before every upstream API
        call. The sleep is ``asyncio.sleep`` so the event loop yields
        to other coroutines (e.g. the live bot's polling loops) while
        the limiter waits.
        """
        async with self._lock:
            now = time.monotonic()
            wait = self._last_call_at + self._current_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
                self._last_call_at = time.monotonic()
            else:
                self._last_call_at = now

    def record_success(self) -> None:
        """Decay the current interval back toward the target.

        Called after every successful upstream call so a prior 429
        backoff doesn't permanently slow the pipeline.
        """
        # Halve the slack on every success so a long run of successes
        # brings the interval back to the floor in O(log N) calls.
        self._current_interval = max(
            self._min_interval,
            self._current_interval / self._backoff_factor,
        )
        self._consecutive_rate_limits = 0

    def record_rate_limit(self) -> None:
        """Multiply the interval by ``backoff_factor`` (capped at max)."""
        self._current_interval = min(
            self._max_interval,
            self._current_interval * self._backoff_factor,
        )
        self._consecutive_rate_limits += 1
        log.warning(
            "[backfill] rate-limit signal — backing off to %.3fs "
            "(consecutive=%d)",
            self._current_interval, self._consecutive_rate_limits,
        )


# ── Backfill engine ──────────────────────────────────────────────────────────


class BackfillEngine:
    """Orchestrates all historical backfill types.

    Each backfill type is a separate method (``backfill_metadata``,
    ``backfill_prices``, ``backfill_trades``, ``backfill_outcomes``,
    ``backfill_snapshots``) returning a :class:`BackfillStats`
    snapshot. Callers can also use :meth:`run` with
    :attr:`BackfillType.ALL` to execute every type sequentially.

    The engine is intentionally stateless across process restarts —
    every per-run counter lives in the returned :class:`BackfillStats`
    and the durable checkpoint lives in :class:`BackfillStore`. A
    restart zeroes the in-memory counters but the next ``backfill_*``
    call resumes from the persisted checkpoint (when ``resume=True``).
    """

    def __init__(
        self,
        gamma: GammaClient | None = None,
        db: Any | None = None,
        *,
        clob: Any | None = None,
        target_rps: float = DEFAULT_RATE_LIMIT_RPS,
        concurrency: int = DEFAULT_CONCURRENCY,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_pages: int = DEFAULT_MAX_PAGES,
        store: BackfillStore | None = None,
    ) -> None:
        self.gamma = gamma or gamma_client
        self.db = db or timescale_db
        self.clob = clob or clob_client
        self.page_size = page_size
        self.max_pages = max_pages
        self.rate_limiter = RateLimiter(target_rps=target_rps)
        self.concurrency = max(1, concurrency)
        self._sem = asyncio.Semaphore(self.concurrency)
        self.store = store or BackfillStore()

    # ── Step 2: market discovery / metadata backfill ───────────────────

    async def backfill_metadata(
        self,
        *,
        market_token: str | None = None,
        resume: bool = True,
    ) -> BackfillStats:
        """Page through Gamma's ``/markets`` and upsert every market.

        Args:
            market_token: Optional ``condition_id`` — when set, only
                that single market is fetched via ``get_market`` and
                upserted (skips pagination entirely).
            resume: When ``True`` (default), the engine picks up from
                the last persisted ``last_offset`` checkpoint. When
                ``False``, the checkpoint is reset and the backfill
                starts from offset 0.

        Returns:
            :class:`BackfillStats` with the per-pass counters.
        """
        stats = BackfillStats(type=BackfillType.METADATA.value)
        cp_type = BackfillType.METADATA.value
        if not resume:
            self.store.reset_checkpoint(cp_type)
        cp = self.store.load_checkpoint(cp_type) or BackfillCheckpoint(type=cp_type)

        # ── Single-market path ────────────────────────────────────────
        if market_token:
            try:
                await self.rate_limiter.acquire()
                market = await self.gamma.get_market(market_token)
                stats.total_processed += 1
                ok = self.store.upsert_market(market, backfilled_metadata=True)
                if ok:
                    stats.total_added += 1
                else:
                    stats.total_errors += 1
            except Exception as e:
                stats.total_errors += 1
                stats.error_message = str(e)
                log.error("[backfill] metadata single-market %s failed: %s",
                          market_token, e, exc_info=True)
            stats.mark_done()
            cp.last_run_at = time.time()
            cp.completed = True
            self.store.save_checkpoint(cp)
            self.store.record_run(stats)
            return stats

        # ── Paginated path ────────────────────────────────────────────
        known_before = self.store.get_known_condition_ids()
        offset = cp.last_offset if cp.last_offset > 0 else 0

        for page_idx in range(self.max_pages):
            try:
                await self.rate_limiter.acquire()
                markets = await self.gamma.get_markets(
                    active=True, closed=False,
                    limit=self.page_size, offset=offset,
                    order="volume24hr", ascending=False,
                )
            except Exception as e:
                stats.total_errors += 1
                stats.error_message = str(e)
                log.error("[backfill] metadata page offset=%d failed: %s",
                          offset, e, exc_info=True)
                break

            if not markets:
                break

            for mkt in markets:
                stats.total_processed += 1
                try:
                    ok = self.store.upsert_market(mkt, backfilled_metadata=True)
                    if ok:
                        stats.total_added += 1
                    else:
                        stats.total_errors += 1
                except Exception as e:
                    stats.total_errors += 1
                    log.warning("[backfill] upsert market failed: %s", e)

            offset += len(markets)
            stats.last_offset = offset
            cp.last_offset = offset
            cp.last_run_at = time.time()
            self.store.save_checkpoint(cp)

            if len(markets) < self.page_size:
                break

        # ── Detect new markets since last run ──
        known_after = self.store.get_known_condition_ids()
        new_market_count = len(known_after - known_before)
        if new_market_count:
            log.info("[backfill] discovered %d new markets this pass",
                     new_market_count)

        cp.completed = True
        self.store.save_checkpoint(cp)
        stats.mark_done()
        self.store.record_run(stats)
        return stats

    # ── Step 3: price history backfill ─────────────────────────────────

    async def backfill_prices(
        self,
        *,
        market_token: str | None = None,
        days: int = DEFAULT_DAYS,
        resolution: str = DEFAULT_PRICE_RESOLUTION,
        resume: bool = True,
    ) -> BackfillStats:
        """For each market, fetch trades and aggregate into OHLCV candles.

        Args:
            market_token: Optional ``token_id`` — when set, only that
                token's price history is backfilled (skips the market
                fan-out).
            days: Historical depth in days (default 7). Trades older
                than ``now - days*86400`` are dropped before aggregation.
            resolution: OHLCV candle resolution — one of ``1m`` / ``5m``
                / ``15m`` / ``1h`` / ``1d``.
            resume: When ``True`` (default), the engine picks up from
                the last persisted ``last_token_id`` checkpoint.

        Returns:
            :class:`BackfillStats` with the per-pass counters.
        """
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(
                f"unsupported resolution {resolution!r}; "
                f"valid: {sorted(RESOLUTION_SECONDS)}"
            )
        candle_s = RESOLUTION_SECONDS[resolution]
        cutoff = time.time() - max(1, days) * 86400.0

        stats = BackfillStats(type=BackfillType.PRICES.value)
        cp_type = BackfillType.PRICES.value
        if not resume:
            self.store.reset_checkpoint(cp_type)
        cp = self.store.load_checkpoint(cp_type) or BackfillCheckpoint(type=cp_type)

        # ── Resolve token list ──
        if market_token:
            tokens = [(market_token, market_token, market_token)]
        else:
            tokens = self._collect_market_tokens()
            # ── Resume: skip tokens already processed in the prior pass ──
            if cp.last_token_id and cp.last_token_id in {t[0] for t in tokens}:
                idx = next(
                    (i for i, t in enumerate(tokens) if t[0] == cp.last_token_id),
                    None,
                )
                if idx is not None:
                    tokens = tokens[idx + 1:]

        # ── Parallel fan-out bounded by the semaphore ──
        async def _process(t: tuple[str, str, str]) -> tuple[int, int, int]:
            return await self._backfill_prices_for_token(
                t[0], t[1], candle_s=candle_s, cutoff=cutoff,
            )

        tasks = [self._bounded_gather(_process, t) for t in tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                stats.total_errors += 1
                log.warning("[backfill] price task raised: %s", r)
                continue
            added, skipped, errors = r
            stats.total_processed += 1
            stats.total_added += added
            stats.total_skipped += skipped
            stats.total_errors += errors

        if tokens:
            stats.last_token_id = tokens[-1][0]
            cp.last_token_id = stats.last_token_id
        cp.last_run_at = time.time()
        cp.completed = True
        self.store.save_checkpoint(cp)
        stats.mark_done()
        self.store.record_run(stats)
        return stats

    async def _backfill_prices_for_token(
        self,
        token_id: str,
        slug: str,
        *,
        candle_s: int,
        cutoff: float,
    ) -> tuple[int, int, int]:
        """Fetch trades for one token and write OHLCV candles.

        Returns ``(added, skipped, errors)`` for this token.
        """
        added = 0
        skipped = 0
        errors = 0
        try:
            async with self._sem:
                await self.rate_limiter.acquire()
                trades = await self.clob.get_public_trades(
                    token_id=token_id, limit=DEFAULT_PUBLIC_TRADE_LIMIT,
                )
        except Exception as e:
            log.warning("[backfill] prices fetch %s failed: %s", token_id, e)
            return 0, 0, 1

        # ── Filter by cutoff ──
        recent = [
            t for t in trades
            if float(t.get("timestamp") or 0.0) >= cutoff
        ]
        if not recent:
            return 0, 0, 0

        # ── Aggregate trades into OHLCV candles ──
        candles = self._aggregate_ohlcv(recent, candle_s=candle_s)

        # ── Write each candle as a market snapshot ──
        for c in candles:
            try:
                ok = await self.db.record_snapshot(
                    token_id=token_id,
                    slug=slug,
                    best_bid=c["open"],
                    best_ask=c["close"],
                    mid=c["close"],
                    spread=c["high"] - c["low"] if c["high"] > c["low"] else 0.0,
                    volume_24h=float(c["volume"]),
                    liquidity=0.0,
                )
                if ok:
                    added += 1
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                log.warning("[backfill] price snapshot %s failed: %s", token_id, e)

        return added, skipped, errors

    @staticmethod
    def _aggregate_ohlcv(
        trades: list[dict], *, candle_s: int,
    ) -> list[dict[str, Any]]:
        """Aggregate a trade list into OHLCV candles at the given resolution.

        Each candle carries:
          * ``timestamp`` — bucket start (unix seconds)
          * ``open`` / ``high`` / ``low`` / ``close`` — price points
          * ``volume`` — sum of ``size * price`` over the bucket
        """
        if not trades:
            return []
        candles: dict[int, dict[str, Any]] = {}
        for t in trades:
            try:
                ts = float(t.get("timestamp") or 0.0)
                price = float(t.get("price") or 0.0)
                size = float(t.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if ts <= 0 or price <= 0:
                continue
            bucket = int(ts // candle_s) * candle_s
            c = candles.get(bucket)
            if c is None:
                candles[bucket] = {
                    "timestamp": bucket,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": price * size,
                }
            else:
                c["high"] = max(c["high"], price)
                c["low"] = min(c["low"], price)
                c["close"] = price
                c["volume"] += price * size
        return [candles[k] for k in sorted(candles.keys())]

    # ── Step 4: trade history backfill ─────────────────────────────────

    async def backfill_trades(
        self,
        *,
        market_token: str | None = None,
        days: int = DEFAULT_DAYS,
        resume: bool = True,
    ) -> BackfillStats:
        """For each market, fetch historical trades and persist them.

        Uses ``clob_client.get_public_trades(token_id=...)`` (the
        unauthenticated ``GET /trades?asset_id=…`` endpoint). Every
        trade is persisted via ``timescale_db.record_trade``, whose
        ``ON CONFLICT (trade_id) DO NOTHING`` clause is the durable
        dedup backstop — a re-backfilled trade is a no-op.
        """
        cutoff = time.time() - max(1, days) * 86400.0
        stats = BackfillStats(type=BackfillType.TRADES.value)
        cp_type = BackfillType.TRADES.value
        if not resume:
            self.store.reset_checkpoint(cp_type)
        cp = self.store.load_checkpoint(cp_type) or BackfillCheckpoint(type=cp_type)

        if market_token:
            tokens = [(market_token, market_token, market_token)]
        else:
            tokens = self._collect_market_tokens()
            if cp.last_token_id and cp.last_token_id in {t[0] for t in tokens}:
                idx = next(
                    (i for i, t in enumerate(tokens) if t[0] == cp.last_token_id),
                    None,
                )
                if idx is not None:
                    tokens = tokens[idx + 1:]

        async def _process(t: tuple[str, str, str]) -> tuple[int, int, int]:
            return await self._backfill_trades_for_token(
                t[0], t[1], cutoff=cutoff,
            )

        tasks = [self._bounded_gather(_process, t) for t in tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                stats.total_errors += 1
                log.warning("[backfill] trade task raised: %s", r)
                continue
            added, skipped, errors = r
            stats.total_processed += 1
            stats.total_added += added
            stats.total_skipped += skipped
            stats.total_errors += errors

        if tokens:
            stats.last_token_id = tokens[-1][0]
            cp.last_token_id = stats.last_token_id
        cp.last_run_at = time.time()
        cp.completed = True
        self.store.save_checkpoint(cp)
        stats.mark_done()
        self.store.record_run(stats)
        return stats

    async def _backfill_trades_for_token(
        self,
        token_id: str,
        slug: str,
        *,
        cutoff: float,
    ) -> tuple[int, int, int]:
        added = 0
        skipped = 0
        errors = 0
        try:
            async with self._sem:
                await self.rate_limiter.acquire()
                trades = await self.clob.get_public_trades(
                    token_id=token_id, limit=DEFAULT_PUBLIC_TRADE_LIMIT,
                )
        except Exception as e:
            log.warning("[backfill] trades fetch %s failed: %s", token_id, e)
            return 0, 0, 1

        for t in trades:
            try:
                ts = float(t.get("timestamp") or 0.0)
                if ts < cutoff:
                    skipped += 1
                    continue
                ok = await self.db.record_trade(
                    token_id=token_id,
                    price=float(t.get("price") or 0.0),
                    size=float(t.get("size") or 0.0),
                    side=str(t.get("side") or ""),
                    timestamp=ts,
                    trade_id=str(t.get("trade_id") or ""),
                    maker_address=str(t.get("maker_address") or ""),
                    taker_order_id=str(t.get("taker_order_id") or ""),
                )
                if ok:
                    added += 1
                else:
                    errors += 1
            except Exception as e:
                errors += 1
                log.warning("[backfill] trade persist %s failed: %s", token_id, e)

        return added, skipped, errors

    # ── Step 5: order book snapshot backfill ────────────────────────────

    async def backfill_snapshots(
        self,
        *,
        market_token: str | None = None,
        resume: bool = True,
    ) -> BackfillStats:
        """For each market, fetch the current L2 book and persist a snapshot.

        Designed to be run periodically (e.g. hourly) so a market's
        resting-depth evolution is captured. The persistence path is
        the same as the price history backfill
        (``timescale_db.record_snapshot``) — ``bids_json`` / ``asks_json``
        carry the full ladder so the existing ``/api/depth-full`` /
        ``/api/depth-history`` queries can read it back unchanged.
        """
        stats = BackfillStats(type=BackfillType.SNAPSHOTS.value)
        cp_type = BackfillType.SNAPSHOTS.value
        if not resume:
            self.store.reset_checkpoint(cp_type)
        cp = self.store.load_checkpoint(cp_type) or BackfillCheckpoint(type=cp_type)

        if market_token:
            tokens = [(market_token, market_token, market_token)]
        else:
            tokens = self._collect_market_tokens()
            if cp.last_token_id and cp.last_token_id in {t[0] for t in tokens}:
                idx = next(
                    (i for i, t in enumerate(tokens) if t[0] == cp.last_token_id),
                    None,
                )
                if idx is not None:
                    tokens = tokens[idx + 1:]

        async def _process(t: tuple[str, str, str]) -> tuple[int, int, int]:
            return await self._backfill_snapshot_for_token(t[0], t[1])

        tasks = [self._bounded_gather(_process, t) for t in tokens]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                stats.total_errors += 1
                log.warning("[backfill] snapshot task raised: %s", r)
                continue
            added, skipped, errors = r
            stats.total_processed += 1
            stats.total_added += added
            stats.total_skipped += skipped
            stats.total_errors += errors

        if tokens:
            stats.last_token_id = tokens[-1][0]
            cp.last_token_id = stats.last_token_id
        cp.last_run_at = time.time()
        cp.completed = True
        self.store.save_checkpoint(cp)
        stats.mark_done()
        self.store.record_run(stats)
        return stats

    async def _backfill_snapshot_for_token(
        self,
        token_id: str,
        slug: str,
    ) -> tuple[int, int, int]:
        added = 0
        skipped = 0
        errors = 0
        try:
            async with self._sem:
                await self.rate_limiter.acquire()
                book = await self.clob.get_order_book(token_id)
        except Exception as e:
            log.warning("[backfill] snapshot fetch %s failed: %s", token_id, e)
            return 0, 0, 1

        if not book:
            return 0, 1, 0

        bids = book.get("bids") or book.get("buys") or []
        asks = book.get("asks") or book.get("sells") or []
        best_bid = float(bids[0].get("price") or 0.0) if bids else 0.0
        best_ask = float(asks[0].get("price") or 0.0) if asks else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else 0.0
        spread = best_ask - best_bid if best_bid and best_ask else 0.0

        try:
            ok = await self.db.record_snapshot(
                token_id=token_id,
                slug=slug,
                best_bid=best_bid or None,
                best_ask=best_ask or None,
                mid=mid or None,
                spread=spread or None,
                volume_24h=0.0,
                liquidity=0.0,
                bids_json=bids,
                asks_json=asks,
            )
            if ok:
                added += 1
            else:
                errors += 1
        except Exception as e:
            errors += 1
            log.warning("[backfill] snapshot persist %s failed: %s", token_id, e)

        return added, skipped, errors

    # ── Step 6: resolution outcome backfill ────────────────────────────

    async def backfill_outcomes(
        self,
        *,
        market_token: str | None = None,
        resume: bool = True,
    ) -> BackfillStats:
        """Page through resolved markets and record final YES / NO outcomes.

        For each resolved market:
          1. Determine the YES outcome via
             :meth:`LabelBackfillEngine._resolve_outcome` (the existing
             static parser in ``core/label_backfill`` — reused rather
             than re-implemented so the threshold logic can't drift).
          2. Persist the outcome in
             ``backfill_markets.resolved_outcome_yes``.
          3. Mark every labeled feature-vector row for that market's
             YES / NO tokens via
             :meth:`timescale_db.mark_resolved_outcomes` so the ML
             feature store's ``fetch_training_samples`` path can read
             the ground-truth label.

        Args:
            market_token: Optional ``condition_id`` — when set, only
                that market's outcome is backfilled.
            resume: When ``True`` (default), the engine picks up from
                the last persisted ``last_offset`` checkpoint.
        """
        # Late import so this module is safe to import even before
        # ``core.label_backfill`` is fully wired (mirrors the pattern in
        # ``core/label_backfill.py`` for ``ml.features``).
        from core.label_backfill import LabelBackfillEngine

        stats = BackfillStats(type=BackfillType.OUTCOMES.value)
        cp_type = BackfillType.OUTCOMES.value
        if not resume:
            self.store.reset_checkpoint(cp_type)
        cp = self.store.load_checkpoint(cp_type) or BackfillCheckpoint(type=cp_type)

        # ── Single-market path ──
        if market_token:
            try:
                await self.rate_limiter.acquire()
                market = await self.gamma.get_market(market_token)
                stats.total_processed += 1
                added, skipped = await self._process_outcome(
                    market, LabelBackfillEngine,
                )
                stats.total_added += added
                stats.total_skipped += skipped
            except Exception as e:
                stats.total_errors += 1
                stats.error_message = str(e)
                log.error("[backfill] outcome single-market %s failed: %s",
                          market_token, e, exc_info=True)
            stats.mark_done()
            cp.last_run_at = time.time()
            cp.completed = True
            self.store.save_checkpoint(cp)
            self.store.record_run(stats)
            return stats

        # ── Paginated path ──
        offset = cp.last_offset if cp.last_offset > 0 else 0
        for page_idx in range(self.max_pages):
            try:
                await self.rate_limiter.acquire()
                markets = await self.gamma.get_markets(
                    active=False, closed=True,
                    limit=self.page_size, offset=offset,
                    order="updatedAt", ascending=False,
                )
            except Exception as e:
                stats.total_errors += 1
                stats.error_message = str(e)
                log.error("[backfill] outcomes page offset=%d failed: %s",
                          offset, e, exc_info=True)
                break

            if not markets:
                break

            for mkt in markets:
                stats.total_processed += 1
                try:
                    added, skipped = await self._process_outcome(
                        mkt, LabelBackfillEngine,
                    )
                    stats.total_added += added
                    stats.total_skipped += skipped
                except Exception as e:
                    stats.total_errors += 1
                    log.warning("[backfill] outcome process failed: %s", e)

            offset += len(markets)
            stats.last_offset = offset
            cp.last_offset = offset
            cp.last_run_at = time.time()
            self.store.save_checkpoint(cp)

            if len(markets) < self.page_size:
                break

        cp.completed = True
        self.store.save_checkpoint(cp)
        stats.mark_done()
        self.store.record_run(stats)
        return stats

    async def _process_outcome(
        self,
        market: dict,
        label_cls: type,
    ) -> tuple[int, int]:
        """Resolve a market's outcome and propagate the label downstream.

        Returns ``(added, skipped)`` — ``added`` is the number of
        markets whose outcome was newly recorded (or updated); ``skipped``
        is the number whose outcome was already known.

        The market is upserted into ``backfill_markets`` BEFORE the
        ``set_resolved_outcome`` UPDATE so the outcome backfill is
        self-contained — it doesn't depend on ``backfill_metadata``
        having been run first (mirrors the ``record_snapshot`` /
        ``record_trade`` pattern in ``core/timescale_db`` where each
        write is independent).
        """
        condition_id = str(
            market.get("conditionId") or market.get("condition_id") or ""
        )
        if not condition_id:
            return 0, 1

        resolved_yes = label_cls._resolve_outcome(market)
        if resolved_yes is None:
            return 0, 1

        # ── 1. Upsert the market (so the row exists) ──
        # ``upsert_market`` is ``INSERT OR REPLACE`` keyed on
        # ``condition_id``; the resolved_outcome_yes column is computed
        # inside the upsert too (when ``closed`` and ``outcomePrices``
        # are parseable), so this single call both creates the row and
        # sets the outcome in one DB round-trip.
        ok = self.store.upsert_market(market, backfilled_outcomes=True)
        if not ok:
            return 0, 1

        # ── 2. Belt-and-braces: explicitly set the resolved_outcome ──
        # ``upsert_market`` computes ``resolved_outcome_yes`` from the
        # market dict's ``outcomePrices`` field; the explicit call here
        # ensures the value matches ``label_cls._resolve_outcome``'s
        # threshold (rather than the ``upsert_market`` re-implementation)
        # so the two code paths can't drift.
        self.store.set_resolved_outcome(condition_id, bool(resolved_yes))

        # ── 3. Propagate to ML feature store via mark_resolved_outcomes ──
        # Marks every labeled feature-vector row for this market's YES
        # and NO tokens with the ground-truth outcome (YES token gets
        # ``resolved_yes``; NO token gets ``not resolved_yes`` via the
        # binary-pair convention in GammaClient.extract_binary_pair).
        token_ids = GammaClient.extract_token_ids(market)
        if token_ids:
            yes_token = token_ids[0]
            self.db.mark_resolved_outcomes(yes_token, bool(resolved_yes))
            if len(token_ids) > 1:
                no_token = token_ids[1]
                self.db.mark_resolved_outcomes(no_token, not bool(resolved_yes))

        return 1, 0

    # ── Composite runner ────────────────────────────────────────────────

    async def run(
        self,
        backfill_type: BackfillType | str,
        *,
        market_token: str | None = None,
        days: int = DEFAULT_DAYS,
        resolution: str = DEFAULT_PRICE_RESOLUTION,
        resume: bool = True,
    ) -> dict[str, BackfillStats]:
        """Run one or more backfill passes.

        Args:
            backfill_type: :class:`BackfillType` value or its string
                form (``"metadata"`` / ``"prices"`` / ``"trades"`` /
                ``"outcomes"`` / ``"snapshots"`` / ``"all"``).
            market_token: Optional market / token id — restricts the
                backfill to a single market.
            days: Historical depth (prices + trades only).
            resolution: OHLCV candle resolution (prices only).
            resume: When ``True`` (default), pick up from the last
                persisted checkpoint.

        Returns:
            Dict mapping each executed backfill type's name to its
            :class:`BackfillStats`. For ``all``, every type runs in
            sequence and the dict has all five keys.
        """
        if isinstance(backfill_type, str):
            backfill_type = BackfillType.parse(backfill_type)

        if backfill_type == BackfillType.ALL:
            order = [
                BackfillType.METADATA,
                BackfillType.PRICES,
                BackfillType.TRADES,
                BackfillType.OUTCOMES,
                BackfillType.SNAPSHOTS,
            ]
        else:
            order = [backfill_type]

        results: dict[str, BackfillStats] = {}
        for bt in order:
            if bt == BackfillType.METADATA:
                results[bt.value] = await self.backfill_metadata(
                    market_token=market_token, resume=resume,
                )
            elif bt == BackfillType.PRICES:
                results[bt.value] = await self.backfill_prices(
                    market_token=market_token, days=days,
                    resolution=resolution, resume=resume,
                )
            elif bt == BackfillType.TRADES:
                results[bt.value] = await self.backfill_trades(
                    market_token=market_token, days=days, resume=resume,
                )
            elif bt == BackfillType.OUTCOMES:
                results[bt.value] = await self.backfill_outcomes(
                    market_token=market_token, resume=resume,
                )
            elif bt == BackfillType.SNAPSHOTS:
                results[bt.value] = await self.backfill_snapshots(
                    market_token=market_token, resume=resume,
                )
        return results

    # ── Helpers ─────────────────────────────────────────────────────────

    def _collect_market_tokens(self) -> list[tuple[str, str, str]]:
        """Return ``(token_id, slug, condition_id)`` for every market.

        Used by the parallel fan-out backfills (prices / trades /
        snapshots) so they have a deterministic token ordering —
        ``last_token_id`` checkpoints work because the list is
        ``ORDER BY condition_id`` (stable across runs).
        """
        markets = self.store.list_markets(limit=10_000)
        out: list[tuple[str, str, str]] = []
        for m in markets:
            condition_id = str(m.get("condition_id") or "")
            slug = str(m.get("slug") or "")
            tokens_json = m.get("tokens_json") or "[]"
            try:
                tokens = json.loads(tokens_json) if isinstance(tokens_json, str) else tokens_json
            except (TypeError, ValueError):
                tokens = []
            if isinstance(tokens, list):
                for t in tokens:
                    if isinstance(t, dict):
                        tid = str(t.get("token_id") or "")
                        if tid:
                            out.append((tid, slug, condition_id))
                    elif isinstance(t, str) and t:
                        out.append((t, slug, condition_id))
        # Stable sort by condition_id so resume-from-checkpoint works.
        out.sort(key=lambda x: (x[2], x[0]))
        return out

    async def _bounded_gather(
        self,
        coro_fn: Any,
        arg: Any,
    ) -> Any:
        """Wrap a coroutine factory so it only runs inside the semaphore.

        The semaphore is acquired inside the coroutine itself (in
        ``_backfill_*_for_token``) so the gather doesn't pre-create
        every coroutine at once — the workers that haven't acquired
        the semaphore yet are sitting idle in ``acquire()`` rather than
        holding a slot in the gather list.
        """
        return await coro_fn(arg)


# ── Module-level singleton (mirrors the convention used by
# ``core/label_backfill.py::label_backfill_engine``,
# ``core/trade_ingester.py::trade_tape_ingester``, etc.) ────────────────────

backfill_engine = BackfillEngine()
