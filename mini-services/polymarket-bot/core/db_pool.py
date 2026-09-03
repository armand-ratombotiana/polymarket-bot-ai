"""Async SQLite connection pool.

W16-7 — async database access layer for the bot's SQLite databases
(decision_ledger / observability / execution_quality / closed_positions /
audit_trail …). The pool maintains one ``aiosqlite.Connection`` per
database, enabling FastAPI async endpoints to read the audit trail /
metrics without blocking the event loop on a ``sqlite3.connect`` call.

Design
------
* **One connection per database path.** SQLite serialises writes via
  the file lock, so a pool of N connections to the same file would just
  contend on the same lock. A single async connection per DB lets
  aiosqlite's internal lock serialise writes cooperatively while reads
  (the hot path for dashboards) interleave with the WAL journal.
* **WAL journal mode + NORMAL synchronous** — same write-throughput
  wins as the existing ``core.observability._init_db`` /
  ``core.decision_ledger._init_db`` calls (which already enable WAL on
  their own sync ``sqlite3`` connections). WAL is essential here
  because the async pool may interleave reads with the sync writes the
  rest of the pipeline still issues (the sync ledger / observability /
  execution-quality recorders continue to use ``sqlite3`` directly
  during this wave — they're not migrated to the async pool).
* **Async-context-manager transactions** with explicit commit /
  rollback so callers can compose multi-statement units without
  holding the connection lock for longer than necessary.
* **Singleton ``db_pool`` instance** so the FastAPI shutdown handler
  can call ``close_all`` once at the end of the process. Constructing
  a fresh ``AsyncDBPool`` per request would defeat the pooling benefit
  (and would leak connections — aiosqlite's ``connect`` opens a real
  file handle).
* **Import-safe.** Importing this module does NOT open any
  connections — they're created lazily on first ``get_connection``
  call, mirroring the lazy-init pattern used by the TimescaleDB pool
  in ``core.timescale_db``.

The pool is additive — the existing sync ``core.decision_ledger`` /
``core.observability`` / ``core.execution_quality`` recorders continue
to use ``sqlite3`` directly. A future wave can migrate the write paths
to the async pool; for now only the new ``/api/v2/*`` read endpoints
use it.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger(__name__)


class AsyncDBPool:
    """Manages async SQLite connections per database.

    A single instance (the module-level ``db_pool`` singleton) is
    shared across the FastAPI app. The first call to
    ``get_connection(db_path)`` opens a new aiosqlite connection,
    configures WAL + Row factory, and caches it under the str-form of
    the path; subsequent calls with the same path return the cached
    connection.
    """

    def __init__(self) -> None:
        # ``dict[str, aiosqlite.Connection]`` keyed by ``str(db_path)``.
        # We use the str-form because callers may pass either a ``str``
        # or a ``pathlib.Path`` (the ledger / observability modules
        # expose their DB path as a ``Path`` constant); hashing on the
        # str-form normalises both call shapes.
        self._pools: dict[str, aiosqlite.Connection] = {}
        # Serialises the get-or-create critical section so two
        # concurrent ``get_connection`` calls for the SAME db_path
        # don't both race past the ``if key not in self._pools`` check
        # and open two connections to the same file.
        self._lock = asyncio.Lock()

    async def get_connection(self, db_path: str | Path) -> aiosqlite.Connection:
        """Get or create an aiosqlite connection for a database.

        Lazy: the connection is opened on the first call for a given
        ``db_path``. WAL journal mode + ``synchronous=NORMAL`` are
        enabled on creation (matching the sync ``sqlite3`` init paths
        in ``core.observability`` / ``core.decision_ledger`` so the
        async pool can safely co-exist with the sync recorders against
        the same file). The parent directory is created if absent.
        """
        key = str(db_path)
        async with self._lock:
            if key not in self._pools:
                Path(db_path).parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(db_path))
                conn.row_factory = aiosqlite.Row
                # Enable WAL for better concurrent read performance —
                # without this, the async pool's reads would block
                # behind the sync recorders' writes (and vice-versa).
                try:
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                except aiosqlite.OperationalError as _wal_err:
                    # WAL is not supported on some backends (e.g.
                    # ``:memory:`` dbs used in tests). Fall through —
                    # the default journal mode is still correct, just
                    # less concurrent.
                    logger.debug(
                        "WAL not available for %s (%s) — using default journal mode",
                        key,
                        _wal_err,
                    )
                await conn.commit()
                self._pools[key] = conn
                logger.info("Created async DB connection for %s", key)
            return self._pools[key]

    @asynccontextmanager
    async def transaction(self, db_path: str | Path):
        """Async context manager for a single-connection transaction.

        Yields the connection so the caller can issue multiple
        statements; commits on clean exit, rolls back on exception.
        The connection is NOT closed — it returns to the pool for the
        next caller (matching the per-DB pooling contract).
        """
        conn = await self.get_connection(db_path)
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def execute(
        self, db_path: str | Path, query: str, params: tuple = ()
    ) -> list[dict[str, Any]]:
        """Execute a query and return rows as a list of dicts.

        Always commits — even for SELECTs — so any pending writes from
        a prior ``execute_many`` on the same connection are flushed.
        ``aiosqlite.Row`` makes each row indexable by column name; we
        convert to plain ``dict`` so the result is JSON-serialisable
        for FastAPI response models.
        """
        conn = await self.get_connection(db_path)
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
        await conn.commit()
        return [dict(r) for r in rows]

    async def execute_many(
        self, db_path: str | Path, query: str, params_list: list[tuple]
    ) -> int:
        """Execute a query multiple times with different params.

        Returns the affected row count (``cursor.rowcount``). Always
        commits — the caller can't roll back through this method; use
        the ``transaction`` context manager for atomic multi-statement
        units.
        """
        conn = await self.get_connection(db_path)
        cursor = await conn.executemany(query, params_list)
        await conn.commit()
        return cursor.rowcount

    async def execute_scalar(
        self, db_path: str | Path, query: str, params: tuple = ()
    ) -> Optional[Any]:
        """Execute a query and return a single scalar value.

        Returns ``None`` when the query yields no rows. The first
        column of the first row is returned (the conventional shape
        for ``COUNT(*)`` / ``AVG(...)`` / ``MAX(...)`` queries).
        """
        conn = await self.get_connection(db_path)
        cursor = await conn.execute(query, params)
        row = await cursor.fetchone()
        await conn.commit()
        return row[0] if row else None

    async def close_all(self) -> None:
        """Close every open connection. Safe to call multiple times.

        Closes each connection individually and clears the pool dict.
        Errors are logged but do not abort the shutdown — a single
        stuck ``conn.close()`` should not prevent the rest of the
        connections from being closed (mirrors the defensive pattern
        used by the TimescaleDB pool teardown).
        """
        async with self._lock:
            for key, conn in self._pools.items():
                try:
                    await conn.close()
                    logger.info("Closed async DB connection for %s", key)
                except Exception as e:  # pragma: no cover — defensive
                    logger.error("Error closing %s: %s", key, e)
            self._pools.clear()


# Module-level singleton. The FastAPI shutdown handler in
# ``api/server.py`` calls ``await db_pool.close_all()`` once at process
# exit; importing this module elsewhere is side-effect-free (the pool
# opens connections lazily on first ``get_connection``).
db_pool = AsyncDBPool()
