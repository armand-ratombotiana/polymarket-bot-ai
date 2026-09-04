"""Base Data Access Object — routes to PG or SQLite via DatabaseManager."""
import logging
from typing import Any, Optional

from core.database_manager import db_manager

logger = logging.getLogger(__name__)


class BaseDAO:
    """Base DAO that routes operations to the appropriate backend.

    Subclasses bind to a specific logical SQLite database (``market`` /
    ``decision_ledger`` / future ones) by passing ``sqlite_db_name`` to
    ``super().__init__()``. The ``table_name`` is informational — used in
    error messages and ``__repr__``; the actual SQL queries are owned by
    the subclass methods.

    Routing semantics:

      * ``db_manager.is_postgres`` → try PG via ``_pg_execute`` first.
      * On any PG exception, flip ``db_manager._status`` to SQLite
        (``pg_available = False`` / ``backend = DatabaseBackend.SQLITE``)
        so subsequent calls short-circuit straight to SQLite without
        paying the PG retry cost on every request, then run the query
        against SQLite via ``_sqlite_execute``.
      * When ``db_manager.is_postgres`` is already ``False``, the PG
        attempt is skipped entirely and SQLite is used directly.

    The PG path uses ``asyncpg.connect`` per-call (no pool reuse at the
    DAO level — the pool lives in ``core.database_manager.db_manager`` /
    ``core.timescale_db.timescale_db``; the BaseDAO's ``_pg_execute`` is
    a thin fallback for ad-hoc queries that don't have a dedicated
    ``db_manager`` helper yet).
    """

    def __init__(self, table_name: str, sqlite_db_name: str = "market"):
        self.table_name = table_name
        self.sqlite_db_name = sqlite_db_name

    @property
    def backend(self) -> str:
        """Convenience accessor mirroring ``db_manager.backend_name``.

        Returns ``"postgresql"`` / ``"sqlite"`` / ``"none"``. Subclasses
        that need to switch SQL dialect based on the active backend
        (e.g. the DecisionLedgerDAO's ``?`` → ``$N`` placeholder
        rewrite) read this property so the dialect logic lives in one
        place.
        """
        return db_manager.backend_name

    def _get_sqlite_conn(self):
        """Open a fresh SQLite connection for ``self.sqlite_db_name``.

        Each call opens a new connection — SQLite's WAL mode makes this
        cheap (sub-millisecond open), and the per-call scope keeps the
        connection's transaction lifecycle identical to the DAO method
        that opened it (no shared state, no prepared-statement cache
        leaking across calls).
        """
        import sqlite3

        path = db_manager.get_sqlite_path(self.sqlite_db_name)
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        return conn

    async def _pg_execute(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a query on PostgreSQL.

        Uses ``asyncpg.connect`` per-call (no pool reuse). The DAO base
        class is a low-volume facade — high-volume write paths still go
        through ``timescale_db.timescale_db`` with its own dedicated
        asyncpg pool. Failure of this method is the canonical signal
        the ``execute()`` dispatcher uses to flip the runtime backend
        to SQLite.
        """
        try:
            import asyncpg
            import os

            database_url = os.environ.get(
                "DATABASE_URL",
                "postgresql://postgres:polymarket_secret@localhost:5432/polymarket",
            )
            conn = await asyncpg.connect(database_url)
            try:
                rows = await conn.fetch(query, *params)
                # ``fetch`` returns a list of Record objects; ``dict(r)``
                # materialises them into plain dicts so the DAO's callers
                # don't need to know about asyncpg's Record type.
                return [dict(r) for r in rows]
            finally:
                await conn.close()
        except Exception as e:
            logger.error("PG execute failed: %s", e)
            raise

    def _sqlite_execute(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a query on SQLite.

        Commits the transaction before returning so INSERT/UPDATE
        writes are durable. SELECT queries are also committed (a no-op
        on a read-only transaction) so the connection's lifecycle is
        uniform regardless of the query type.
        """
        try:
            with self._get_sqlite_conn() as conn:
                cursor = conn.execute(query, params)
                rows = cursor.fetchall()
                conn.commit()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error("SQLite execute failed: %s", e)
            raise

    async def execute(self, query: str, params: tuple = ()) -> list[dict]:
        """Execute a query on the active backend.

        Routing logic:

          1. If ``db_manager.is_postgres`` is True, try ``_pg_execute``.
          2. On any exception, flip ``db_manager._status`` to SQLite
             (``pg_available = False`` / ``backend = SQLITE``) so the
             next call short-circuits straight to SQLite.
          3. Run the query against SQLite via ``_sqlite_execute``.

        Returns the list of rows (as dicts) returned by the query.
        """
        if db_manager.is_postgres:
            try:
                return await self._pg_execute(query, params)
            except Exception as e:
                logger.warning("PG failed, falling back to SQLite: %s", e)
                db_manager._status.pg_available = False
                # ``db_manager._status.backend.SQLITE`` resolves to
                # ``DatabaseBackend.SQLITE`` regardless of the current
                # ``backend`` value (enum members expose class-level
                # attributes), so this works whether the runtime was
                # ``POSTGRESQL`` or already ``SQLITE``.
                db_manager._status.backend = db_manager._status.backend.SQLITE
        return self._sqlite_execute(query, params)

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} table={self.table_name} "
            f"db={self.sqlite_db_name} backend={self.backend}>"
        )
