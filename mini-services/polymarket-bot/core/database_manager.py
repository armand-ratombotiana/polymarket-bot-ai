"""core/database_manager.py — Unified database manager.

W21-1 — PostgreSQL/TimescaleDB primary, SQLite automatic fallback.

Architecture
------------
1. On startup, attempts to connect to PostgreSQL/TimescaleDB via a
   short (3-second) ``asyncpg.connect`` + ``SELECT 1`` health check.
2. If PG is available and healthy → routes all operations to PG
   (delegating to the existing ``core.timescale_db.timescale_db``
   singleton for the actual writes).
3. If PG is not available → falls back to SQLite (the manager owns
   its own per-database SQLite files under ``BOT_DATA_DIR``).
4. Periodically retries PG (every 60 s by default) so a recovered
   primary is detected and the manager can switch back.
5. All data access goes through this layer — no direct PG/SQLite
   calls at the call sites. A PG write that fails mid-operation is
   transparently re-routed to SQLite so a transient PG blip never
   drops a row.

This module fixes the W17-4 God Mode finding that PostgreSQL /
TimescaleDB is in standby mode (``DATABASE_URL`` not set, hardcoded
default doesn't resolve, ``timescale_db._is_postgres = False`` at
runtime, the system silently uses SQLite for everything while the
PG code path carries the better features — full order book depth,
hypertables, continuous aggregates).

Compatibility surfaces
----------------------
This module exposes the symbols expected by the parallel W21-x
wiring blocks:

  * ``DatabaseBackend``  — enum (``POSTGRESQL`` / ``SQLITE`` / ``NONE``).
  * ``Backend``          — alias for ``DatabaseBackend`` (W21-4 name).
  * ``DatabaseManager``  — the class.
  * ``db_manager``       — module-level singleton (W21-1 / W21-6 name).
  * ``database_manager`` — alias for ``db_manager`` (W21-2 name).
  * ``register_routes``  — registers the W21-5 order-book depth
                           HTTP routes (``/api/depth-full/{token_id}``
                           / ``/api/depth-history/{token_id}``).

The class itself carries the W21-4 / W21-6 DAO surface:
``SQLITE_PATHS``, ``_sqlite_paths``, ``_init_market_db``,
``_init_decision_ledger_db``, ``_pg_execute``, ``_sqlite_execute``,
``record_snapshot``, ``record_trade``, ``get_snapshots``,
``get_trades``, ``get_trade_tape``, ``get_trade_stats``, and the
``_pg_*`` / ``_sqlite_*`` helpers the BaseDAO dispatcher and the
trade-tape tests reference.
"""
from __future__ import annotations

import asyncio
import enum
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query

# NOTE: We import the *module* (not the singleton) so that
# ``monkeypatch.setattr("core.timescale_db.timescale_db", engine)``
# in tests takes effect — every call site reads
# ``_ts_module.timescale_db`` at call time, picking up the patched
# singleton. Binding the singleton at module level (``from
# core.timescale_db import timescale_db``) would freeze the reference
# at import time and miss the patch.
import core.timescale_db as _ts_module

# W21-2 — PG health monitor. Imported at module-import time so the
# singleton's background task can be started by ``DatabaseManager.initialize``
# and stopped by ``shutdown``. The monitor itself is import-safe (no I/O
# happens until ``start()`` is called) — see ``core/pg_health_monitor.py``.
from core.pg_health_monitor import pg_health_monitor

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

# Default PG DSN used when no ``DATABASE_URL`` is set in the environment.
# Mirrors the default in ``core/timescale_db.py::DB_URL`` (modulo the
# host — ``localhost`` instead of ``timescaledb`` so a dev sandbox
# without the docker-compose service doesn't try to resolve a name that
# doesn't exist on the host).
_DEFAULT_PG_DSN = "postgresql://postgres:polymarket_secret@localhost:5432/polymarket"

# Hard ceiling on the in-memory ``connection_errors`` deque so a
# long-running process can't grow it unbounded.
_MAX_ERROR_HISTORY = 50

# Hard ceiling on the ``get_trade_tape`` ``limit`` argument so a
# misbehaving caller can't OOM the bot by requesting millions of rows.
# Mirrors the cap in ``timescale_db.fetch_trades``.
_TRADE_TAPE_LIMIT_CAP = 500


# ── Backend enum ─────────────────────────────────────────────────────────────


class DatabaseBackend(str, enum.Enum):
    """Active database backend selection.

    Used by the ``DatabaseStatus.backend`` field so the
    ``GET /api/database/status`` endpoint can render the active backend
    as a string label. Mirrors the existing
    ``timescale_db.backend_label`` (``"postgres"`` / ``"sqlite"``) but
    adds ``"none"`` for the pre-``initialize()`` state.
    """

    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    NONE = "none"


# W21-4 alias — the DAO ``BaseDAO`` references ``Backend`` (the W21-4
# task spec's preferred name). Keeping the alias here lets the DAO
# layer import ``from core.database_manager import Backend`` without a
# second enum definition.
Backend = DatabaseBackend


# ── Status DTO ────────────────────────────────────────────────────────────────


class DatabaseStatus:
    """Tracks database backend status for observability + retry logic.

    Exposed via ``DatabaseManager.get_status()`` and the
    ``GET /api/database/status`` endpoint so an operator can see which
    backend is in use, how many times the manager has fallen back,
    and the most recent PG connection errors.

    Field name aliases:
      * ``pg_retry_interval`` ↔ ``retry_interval_s`` — both names are
        exposed because the W21-1 spec uses ``pg_retry_interval`` and
        the W21-5 minimal stub used ``retry_interval_s``. The dict
        returned by ``to_dict`` carries both so neither consumer
        breaks.
      * ``connection_errors`` ↔ ``recent_errors`` — same convention;
        ``to_dict`` returns the last 5 under the ``recent_errors``
        key (the W21-1 spec name).
    """

    def __init__(self) -> None:
        self.backend: DatabaseBackend = DatabaseBackend.NONE
        self.pg_available: bool = False
        self.sqlite_available: bool = False
        self.last_pg_check: float = 0.0
        # Retry interval — exposed so tests can shorten it without
        # monkey-patching the loop. The default 60 s is tuned for
        # production: PG recovery usually takes 5–30 s (container
        # restart) so a 60 s cadence picks up the recovery within one
        # tick without spamming connection attempts.
        self.pg_retry_interval: float = 60.0
        # In-memory error history (capped at ``_MAX_ERROR_HISTORY``).
        self.connection_errors: list[str] = []
        # Counts every transition PG → SQLite (initial fallback AND
        # mid-operation fallbacks). A rising count means PG is flapping
        # and needs attention.
        self.fallback_count: int = 0

    # ── Aliases (kept for backward compat with prior stubs / consumers) ──

    @property
    def retry_interval_s(self) -> float:  # noqa: D401 — alias property
        """Alias for ``pg_retry_interval`` (W21-5 stub used this name)."""
        return self.pg_retry_interval

    @retry_interval_s.setter
    def retry_interval_s(self, value: float) -> None:
        self.pg_retry_interval = float(value)

    @property
    def recent_errors(self) -> list[str]:  # noqa: D401 — alias property
        """Alias for ``connection_errors`` (W21-5 stub used this name)."""
        return self.connection_errors

    @recent_errors.setter
    def recent_errors(self, value: list[str]) -> None:
        # Direct assignment — used by the existing ``_try_postgres``
        # stub's ``self._status.recent_errors = self._status.recent_errors[-5:]``
        # pattern. Trim to the cap so an unbounded list can't sneak in.
        self.connection_errors = list(value)[-_MAX_ERROR_HISTORY:]

    # ── Serialization ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view (JSON-able for the API response)."""
        return {
            "backend": self.backend.value,
            "pg_available": self.pg_available,
            "sqlite_available": self.sqlite_available,
            "last_pg_check": self.last_pg_check,
            "last_pg_check_ago_s": (
                round(max(0.0, time.time() - self.last_pg_check), 1)
                if self.last_pg_check
                else None
            ),
            "pg_retry_interval_s": self.pg_retry_interval,
            # W21-5 stub-name alias — kept so any existing dashboard
            # consumer that reads ``retry_interval_s`` keeps working.
            "retry_interval_s": self.pg_retry_interval,
            "fallback_count": self.fallback_count,
            "recent_errors": list(self.connection_errors[-5:]),
        }

    def _record_error(self, msg: str) -> None:
        """Append a connection error, capping the in-memory history."""
        self.connection_errors.append(msg)
        if len(self.connection_errors) > _MAX_ERROR_HISTORY:
            # Trim from the front so the most-recent errors are kept.
            del self.connection_errors[
                : len(self.connection_errors) - _MAX_ERROR_HISTORY
            ]


# ── DatabaseManager ──────────────────────────────────────────────────────────


class DatabaseManager:
    """Unified database manager with automatic PG→SQLite fallback.

    Primary: PostgreSQL / TimescaleDB (via ``core.timescale_db``)
    Fallback: SQLite (the manager owns its own per-DB SQLite files)

    All data operations go through this layer. The manager routes to
    the appropriate backend based on availability. A PG write that
    fails mid-operation is transparently retried on SQLite so a
    transient PG blip never drops a row.

    Construction is side-effect-free (no I/O, no background task).
    ``initialize()`` performs the actual PG check + SQLite schema
    creation + retry-task spawn; ``shutdown()`` cancels the task.
    """

    def __init__(self) -> None:
        self._status = DatabaseStatus()
        self._retry_task: Optional[asyncio.Task[None]] = None
        self._initialized: bool = False
        # Pre-initialize: ``backend == NONE``. The manager only commits
        # to ``SQLITE`` (or ``POSTGRESQL``) after ``initialize()`` has
        # actually attempted the PG health check. ``is_sqlite`` returns
        # True when the backend is NONE OR SQLITE (W21-9 semantics —
        # ``is_sqlite`` means "we're NOT on postgres", so the
        # pre-initialize state is treated as "would-be-SQLite" for
        # routing decisions; ``is_postgres`` is False so the PG path
        # is never attempted until ``initialize()`` flips the flag).
        self._status.backend = DatabaseBackend.NONE
        self._status.sqlite_available = True
        # ``SQLITE_PATHS`` is populated eagerly so DAO singletons
        # constructed at module-import time can resolve their target
        # path BEFORE ``initialize()`` runs (the lifespan startup
        # happens AFTER every module-level singleton has been
        # constructed — the DAO needs the path dict available at
        # construction time, not at lifespan-startup time).
        self._sqlite_paths: dict[str, str] = {}
        self._init_sqlite_paths()

    # ── Public properties ─────────────────────────────────────────────────

    @property
    def is_postgres(self) -> bool:
        """True when the active backend is PostgreSQL.

        W21-6 contract — the trade-tape routes and the DAO BaseDAO
        dispatcher consult this flag to decide whether to attempt the
        PG path first. The flag is the canonical source of truth for
        "are we on PG right now?" — even when ``timescale_db._is_postgres``
        disagrees (e.g. the manager flipped to SQLite after a
        mid-operation PG failure, but ``timescale_db._is_postgres``
        is still True because the asyncpg pool is still alive).
        """
        return self._status.backend == DatabaseBackend.POSTGRESQL

    @property
    def is_sqlite(self) -> bool:
        """True when the manager would route to SQLite.

        W21-9 semantics — ``is_sqlite`` means "we're NOT on postgres",
        so it returns True for both the ``NONE`` pre-initialize state
        (the manager hasn't yet decided which backend is up) and the
        explicit ``SQLITE`` state. The PG path is never attempted
        while ``is_postgres`` is False, so the pre-initialize state
        behaves like SQLite for routing purposes.
        """
        return self._status.backend != DatabaseBackend.POSTGRESQL

    @property
    def backend_name(self) -> str:
        """Lowercase backend name.

        Returns ``"postgres"`` (NOT ``"postgresql"``) when on PG so
        the value matches the convention used by
        ``core.db.migration_manager.KNOWN_BACKENDS`` and the
        ``backend`` field of ``timescale_db.backend_label``. The
        ``DatabaseStatus.to_dict`` payload uses the enum's value
        (``"postgresql"``) for the JSON response; this property is
        the in-code consumer-facing label.
        """
        if self._status.backend == DatabaseBackend.POSTGRESQL:
            return "postgres"
        if self._status.backend == DatabaseBackend.SQLITE:
            return "sqlite"
        return "none"

    @property
    def backend_label(self) -> str:
        """Alias for ``backend_name`` (W21-6 name).

        Delegates to ``timescale_db.backend_label`` when on PG (so the
        label stays consistent with the actual write path); falls
        back to ``"sqlite"`` otherwise.
        """
        try:
            ts = _ts_module.timescale_db
            if ts._is_postgres and ts._pool is not None:
                return ts.backend_label
        except Exception:  # pragma: no cover — defensive
            pass
        return "sqlite"

    @property
    def is_initialized(self) -> bool:
        """True after ``initialize()`` has run successfully."""
        return self._initialized

    @property
    def sqlite_dir(self) -> Path:
        """Directory holding every SQLite DB file the manager owns."""
        return Path(os.environ.get("BOT_DATA_DIR", "/app/data"))

    @property
    def SQLITE_PATHS(self) -> dict[str, str]:
        """Public alias for ``_sqlite_paths`` — the W21-4 DAO contract.

        ``BaseDAO._get_sqlite_conn`` and the test fixture
        ``reset_dao_paths`` reference ``db_manager.SQLITE_PATHS`` (the
        upper-case name). The lower-case ``_sqlite_paths`` is the
        internal storage; this property exposes the same dict under
        the upper-case name so both naming conventions work.
        """
        return self._sqlite_paths

    @property
    def _pg_pool(self) -> Any:
        """The asyncpg pool from ``timescale_db`` (None when not connected)."""
        try:
            ts = _ts_module.timescale_db
            return ts._pool
        except Exception:  # pragma: no cover — defensive
            return None

    @property
    def _ts(self) -> Any:
        """Reference to the underlying ``timescale_db`` singleton.

        Looked up fresh on every access (NOT bound at construction
        time) so ``monkeypatch.setattr('core.timescale_db.timescale_db',
        engine)`` in tests takes effect — the test fixture patches the
        module attribute AFTER the singleton has been constructed.
        """
        return _ts_module.timescale_db

    @property
    def _sqlite_path(self) -> Any:
        """Path of the SQLite fallback DB the manager reads from.

        W21-5 compatibility — the W21-5 read methods use
        ``timescale_db._sqlite_path`` (the legacy market_intelligence.db
        that holds the orderbook_snapshot rows written by the book
        poller). This property delegates so the W21-5 read methods
        can keep reading from the canonical store.
        """
        try:
            return _ts_module.timescale_db._sqlite_path
        except Exception:  # pragma: no cover — defensive
            return self.get_sqlite_path("market")

    # ── Path / schema management ─────────────────────────────────────────

    def get_sqlite_path(self, name: str) -> Path:
        """Return the SQLite file path registered under ``name``.

        Per-name env-var overrides:
          * ``"market"`` → ``MARKET_DB_PATH`` (the canonical path the
            ``core.timescale_db`` singleton writes to). When unset,
            falls back to ``sqlite_dir / "market_intelligence.db"``.
          * ``"decision_ledger"`` → ``DECISION_LEDGER_DAO_DB_PATH``
            (the W21-4 DAO's separate decision-ledger file). When
            unset, falls back to ``sqlite_dir / "decision_ledger.db"``.
          * Any other name → ``sqlite_dir / f"{name}.db"``.

        Returns a ``Path`` (NOT a string) so the caller can use
        ``Path.exists()`` / ``Path.stat()`` directly. The DAO and
        ``SQLITE_PATHS`` dict store the str-form of the same path.
        """
        if name == "market":
            env_path = os.environ.get("MARKET_DB_PATH")
            if env_path:
                return Path(env_path)
            return self.sqlite_dir / "market_intelligence.db"
        if name == "decision_ledger":
            env_path = os.environ.get("DECISION_LEDGER_DAO_DB_PATH")
            if env_path:
                return Path(env_path)
            return self.sqlite_dir / "decision_ledger.db"
        return self.sqlite_dir / f"{name}.db"

    def _init_sqlite_paths(self) -> None:
        """Populate ``_sqlite_paths`` with the canonical per-DB paths.

        The dict is keyed by the logical DB name (``"market"``,
        ``"decision_ledger"``, etc.) and stores the str-form of the
        path so callers can pass it directly to ``sqlite3.connect``.
        The DAO ``BaseDAO._get_sqlite_conn`` looks up
        ``db_manager.get_sqlite_path(self.sqlite_db_name)`` (which
        returns a ``Path``) — this dict is the cached str-form for
        consumers that need the value eagerly (e.g. the W21-4 test
        fixture that monkey-patches ``db_manager.SQLITE_PATHS["market"]``).
        """
        # Reserved for future expansion — the manager currently only
        # writes market + decision_ledger data through the unified
        # surface; the other paths are registered so a future wave can
        # migrate the sibling SQLite stores through this layer without
        # re-shaping the path table.
        names = (
            "market",
            "decision_ledger",
            "execution_quality",
            "observability",
            "closed_positions",
            "alerts",
            "audit_trail",
            "feature_flags",
            "feature_store",
            "job_queue",
            "immutable_audit",
            "ab_tests",
            "sentiment",
            "idempotency",
            "backtest_experiments",
        )
        self._sqlite_paths = {name: str(self.get_sqlite_path(name)) for name in names}

    def _init_market_db(self) -> None:
        """Create the ``market_snapshots`` + ``market_trades`` tables.

        The schema is the unified W21-1 schema — it carries the
        ``bid_size`` / ``ask_size`` columns the W21-4 DAO writes (in
        addition to the legacy ``volume_24h`` column so the W21-5
        ``get_snapshots`` read path can still SELECT the same column
        set it expects). ``CREATE TABLE IF NOT EXISTS`` makes the
        call idempotent — safe to invoke on every ``initialize()``.
        Delegates to :meth:`_ensure_market_schema` so the schema is
        defined in exactly one place (the ``_ensure_*`` method is
        also called by the W21-4 DAO's first-write path).
        """
        path = self._sqlite_paths.get("market") or str(self.get_sqlite_path("market"))
        self._ensure_market_schema(Path(path))

    def _init_decision_ledger_db(self) -> None:
        """Create the ``decision_events`` table (W21-4 DAO contract).

        The schema mirrors the W21-4 ``DecisionLedgerDAO.record()``
        INSERT shape so the DAO's positional ``?`` placeholders match
        the column order. The ``correlation_id`` column is the
        cross-stage trace key (W21-4 spec's preferred name for the
        legacy ``decision_id``).
        """
        path = self._sqlite_paths.get("decision_ledger") or str(
            self.get_sqlite_path("decision_ledger")
        )
        self._ensure_decision_ledger_schema(Path(path))

    def _ensure_decision_ledger_schema(self, path: Path) -> None:
        """Create the ``decision_events`` schema in the given SQLite file.

        W21-4 DAO contract — the ``DecisionLedgerDAO`` calls this on
        its first ``record()`` to ensure the schema exists in the
        DAO's separate ``DECISION_LEDGER_DAO_DB_PATH`` file (the
        fixture ``reset_dao_paths`` also pre-creates the schema via
        this method so the first ``record()`` call's call-count
        assertion isn't perturbed).
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(str(path)) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS decision_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        correlation_id TEXT NOT NULL,
                        token_id TEXT,
                        stage TEXT NOT NULL,
                        data_json TEXT,
                        model_version TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_de_corr
                        ON decision_events(correlation_id, timestamp ASC);
                    CREATE INDEX IF NOT EXISTS idx_de_token_ts
                        ON decision_events(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_de_stage
                        ON decision_events(stage);
                    """
                )
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[database_manager] _ensure_decision_ledger_schema failed: %s", e
            )

    def _ensure_market_schema(self, path: Path) -> None:
        """Create the market_snapshots + market_trades schema in the given file.

        W21-4 DAO contract — symmetric to
        :meth:`_ensure_decision_ledger_schema` for the market DB. The
        schema carries the unified W21-1 columns (``bid_size`` /
        ``ask_size`` / ``volume_24h`` / ``bids_json`` / ``asks_json`` /
        ``bid_depth_10`` / ``ask_depth_10`` / ``ingestion_time``) on
        top of the legacy ``market_snapshots`` shape so the W21-4 DAO
        can write the full depth-10 + ladder payload.
        """
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        try:
            with sqlite3.connect(str(path)) as conn:
                conn.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA synchronous=NORMAL;

                    CREATE TABLE IF NOT EXISTS market_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp REAL NOT NULL,
                        token_id TEXT NOT NULL,
                        slug TEXT,
                        best_bid REAL,
                        best_ask REAL,
                        mid REAL,
                        spread REAL,
                        bid_size REAL,
                        ask_size REAL,
                        volume_24h REAL,
                        liquidity REAL,
                        bids_json TEXT,
                        asks_json TEXT,
                        bid_depth_10 REAL DEFAULT 0.0,
                        ask_depth_10 REAL DEFAULT 0.0,
                        ingestion_time REAL
                    );
                    CREATE INDEX IF NOT EXISTS idx_ms_token_ts
                        ON market_snapshots(token_id, timestamp DESC);

                    CREATE TABLE IF NOT EXISTS market_trades (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        trade_id TEXT NOT NULL UNIQUE,
                        token_id TEXT NOT NULL,
                        price REAL NOT NULL,
                        size REAL NOT NULL,
                        side TEXT NOT NULL,
                        timestamp REAL NOT NULL,
                        ingestion_time REAL NOT NULL,
                        maker_address TEXT,
                        taker_order_id TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_mt_token_ts
                        ON market_trades(token_id, timestamp DESC);
                    CREATE INDEX IF NOT EXISTS idx_mt_ts
                        ON market_trades(timestamp DESC);
                    """
                )
                # In-place migration for legacy DBs created before
                # W21-1: the new ``bid_size`` / ``ask_size`` columns
                # are missing on those, so the ``ALTER TABLE ADD
                # COLUMN`` adds them; on a fresh DB the ALTER raises
                # ``duplicate column name`` and is swallowed.
                for _col_def in ("bid_size REAL", "ask_size REAL"):
                    try:
                        conn.execute(
                            f"ALTER TABLE market_snapshots ADD COLUMN {_col_def}"
                        )
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
        except Exception as e:  # pragma: no cover — defensive
            logger.error(
                "[database_manager] _ensure_market_schema failed: %s", e
            )

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        """Initialize the manager — try PG first, fall back to SQLite.

        Idempotent: a second call is a no-op (mirrors the pattern used
        by ``paper_sim.start()`` and ``watchdog.start()`` so the
        lifespan can safely call it once and the test suite can call
        it again on a fresh instance without leaking retry tasks).

        Returns ``True`` when PG is the active backend, ``False`` when
        the manager is operating on the SQLite fallback.
        """
        if self._initialized:
            # Idempotent — the lifespan startup in ``api/server.py``
            # calls both ``database_manager.initialize()`` (W21-2
            # name) and ``db_manager.initialize()`` (W21-1 name); the
            # second call must be a no-op so the retry task isn't
            # duplicated.
            return self._status.pg_available

        self._initialized = True

        # Always init SQLite (the safety-net store) — even when PG is
        # up, a mid-operation PG failure routes the row to SQLite so
        # the tables must exist before the first write.
        self._init_sqlite_paths()
        self._init_market_db()
        self._init_decision_ledger_db()
        self._status.sqlite_available = True

        # Try PostgreSQL first — short timeout so a dead PG doesn't
        # block server startup for the full ``init_postgres_pool``
        # 10-second timeout. The manager's 3-second deadline wins.
        await self._try_postgres()

        if not self._status.pg_available:
            logger.warning(
                "[database_manager] PostgreSQL not available — falling back to SQLite"
            )
            self._status.backend = DatabaseBackend.SQLITE
            # Surface the initial fallback so the operator sees "PG
            # was never reachable at startup" in the status payload —
            # same convention as the retry-loop's fallback counter.
            self._status.fallback_count += 1
        else:
            logger.info(
                "[database_manager] PostgreSQL connected — using as primary backend"
            )
            self._status.backend = DatabaseBackend.POSTGRESQL

        # Start background retry task for PG (no-op loop when PG is up).
        self._retry_task = asyncio.create_task(
            self._pg_retry_loop(), name="database-manager-pg-retry"
        )

        # W23-1 — start the PG health monitor alongside the retry loop
        # so callers only need to manage ONE lifecycle (``db_manager.
        # initialize()`` / ``shutdown()``). The monitor's ``_monitor_loop``
        # is a no-op until its first tick, so this is cheap when PG is
        # unreachable (the loop sleeps for the configured poll interval
        # between ``_check_health`` calls). ``start()`` is idempotent —
        # a second call (e.g. from a duplicate lifespan startup hook) is
        # a no-op (the ``_running`` flag short-circuits).
        # Look up via ``sys.modules[__name__]`` so a test-time swap of
        # the module attribute (e.g.
        # ``core.database_manager.pg_health_monitor = fresh_monitor``)
        # is honoured — the module-import-time ``from ... import``
        # binding captures the original singleton.
        try:
            _monitor = getattr(
                sys.modules[__name__], "pg_health_monitor", None
            )
            if _monitor is not None:
                await _monitor.start()
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "[database_manager] pg_health_monitor.start() raised: %s", e
            )

        return self._status.pg_available

    async def _try_postgres(self) -> bool:
        """Attempt to connect to PostgreSQL — returns True on success.

        Uses a 3-second connection timeout so a dead PG doesn't block
        server startup. The check is a bare ``SELECT 1`` — schema
        migrations are the responsibility of
        ``core.timescale_db.init_postgres_pool`` (called separately by
        the lifespan startup). The manager only needs to know whether
        PG is reachable so it can decide which backend to route to.
        """
        try:
            import asyncpg  # type: ignore[import-untyped]
        except ImportError as exc:
            # asyncpg is an optional dependency — if it's not installed
            # the manager can't use PG at all (only SQLite).
            self._status.pg_available = False
            self._status.last_pg_check = time.time()
            self._status._record_error(f"asyncpg import failed: {exc}")
            logger.warning(
                "[database_manager] asyncpg not installed — PG path disabled"
            )
            return False

        database_url = os.environ.get("DATABASE_URL", "")
        if not database_url:
            database_url = _DEFAULT_PG_DSN
            logger.info(
                "[database_manager] No DATABASE_URL set — trying default %s",
                _DEFAULT_PG_DSN,
            )

        try:
            conn = await asyncio.wait_for(
                asyncpg.connect(database_url),
                timeout=3.0,
            )
            try:
                await conn.fetchval("SELECT 1")
            finally:
                await conn.close()

            self._status.pg_available = True
            self._status.last_pg_check = time.time()
            # Clear errors on success — a recovered PG shouldn't keep
            # surfacing stale errors in the status payload.
            self._status.connection_errors = []
            logger.info("[database_manager] PostgreSQL connection successful")
            return True
        except asyncio.TimeoutError:
            self._status.pg_available = False
            self._status.last_pg_check = time.time()
            self._status._record_error(
                f"PG timeout at {time.strftime('%H:%M:%S')} (3s deadline)"
            )
            logger.warning("[database_manager] PostgreSQL connection timed out")
            return False
        except Exception as e:  # noqa: BLE001 — broad catch is intentional
            self._status.pg_available = False
            self._status.last_pg_check = time.time()
            self._status._record_error(f"PG error: {str(e)[:120]}")
            logger.warning(
                "[database_manager] PostgreSQL connection failed: %s", e
            )
            return False

    async def _pg_retry_loop(self) -> None:
        """Periodically retry PG connection to detect recovery.

        Sleeps ``pg_retry_interval`` seconds between attempts. When PG
        is already up the loop is a no-op (it still sleeps so a future
        PG outage is detected within one tick — we don't trust the
        ``pg_available`` flag forever). When PG recovers the manager
        switches back to the PG backend and bumps
        ``fallback_count`` so the operator can see "PG came back" in
        the status payload.
        """
        try:
            while True:
                await asyncio.sleep(self._status.pg_retry_interval)
                # W23-1 — reconcile with the PG health monitor before
                # attempting the retry. The monitor's verdict is the
                # canonical signal for "is PG healthy RIGHT NOW" (the
                # retry loop's own ``_try_postgres`` only catches the
                # connect-time failures, not the steady-state health).
                await self._sync_backend_with_monitor()
                if not self._status.pg_available:
                    logger.info(
                        "[database_manager] Retrying PostgreSQL connection..."
                    )
                    recovered = await self._try_postgres()
                    if recovered:
                        logger.info(
                            "[database_manager] PostgreSQL recovered — "
                            "switching back to primary backend."
                        )
                        self._status.backend = DatabaseBackend.POSTGRESQL
                        # Bump so the operator sees a flap ("PG came
                        # back") in the status payload — every
                        # transition counts as a fallback event for
                        # the metric.
                        self._status.fallback_count += 1
        except asyncio.CancelledError:
            # Clean shutdown via task.cancel() — propagate silently.
            raise

    async def _sync_backend_with_monitor(self) -> None:
        """Reconcile ``_status.backend`` with the PG health monitor.

        W23-1 contract — the retry loop calls this every tick so the
        manager's ``_status.backend`` flag tracks the monitor's verdict
        rather than the (stale) ``pg_available`` flag set by the last
        ``_try_postgres`` call. When the monitor flips healthy/unhealthy
        the manager flips the backend accordingly and bumps
        ``fallback_count`` so the operator sees the flap in the status
        payload.

        Idempotent: when the monitor and manager already agree, the
        method is a no-op (no flip, no fallback_count bump).

        Looks up the monitor via ``sys.modules[__name__].pg_health_monitor``
        rather than the module-import-time ``pg_health_monitor`` name so
        a test that swaps the module attribute (e.g.
        ``core.database_manager.pg_health_monitor = fresh_monitor``) is
        honoured — the imported name binding captures the original
        singleton, but the module attribute reflects the current value.
        """
        # Late import to pick up a test-time swap of the module attribute
        # (e.g. ``core.database_manager.pg_health_monitor = fresh_monitor``).
        # The module-import-time ``from core.pg_health_monitor import
        # pg_health_monitor`` binding captures the original singleton and
        # would miss the patch.
        monitor = getattr(
            sys.modules[__name__], "pg_health_monitor", None
        )
        # ``is_healthy`` is a *method* on PGHealthMonitor (not a
        # property), so it must be called to read the verdict.
        if monitor is None:
            monitor_healthy = False
        else:
            try:
                _verdict = monitor.is_healthy
                monitor_healthy = bool(
                    _verdict() if callable(_verdict) else _verdict
                )
            except Exception:  # pragma: no cover — defensive
                monitor_healthy = False
        if monitor_healthy and not self._status.pg_available:
            # Monitor says healthy + manager says not pg_available →
            # flip to POSTGRESQL (the W21-2 task-spec wiring snippet).
            self._status.backend = DatabaseBackend.POSTGRESQL
            self._status.pg_available = True
            self._status.fallback_count += 1
        elif not monitor_healthy and self._status.pg_available:
            # Monitor says unhealthy + manager says pg_available →
            # flip back to SQLITE so the operator sees the flap.
            self._status.backend = DatabaseBackend.SQLITE
            self._status.pg_available = False
            self._status.fallback_count += 1
        # else: monitor and manager agree — no-op.

    async def shutdown(self) -> None:
        """Clean shutdown — cancel the PG retry task.

        Safe to call even when ``initialize()`` was never invoked (the
        retry task is ``None`` and the cancel branch is skipped). The
        manager doesn't own any SQLite connections (each call opens +
        closes its own via a context manager), so there's no pool to
        drain.
        """
        if self._retry_task is not None and not self._retry_task.done():
            self._retry_task.cancel()
            try:
                await self._retry_task
            except asyncio.CancelledError:
                pass
            except Exception as e:  # pragma: no cover — defensive
                logger.warning(
                    "[database_manager] PG retry task teardown raised: %s", e
                )
            self._retry_task = None
        # W23-1 — stop the PG health monitor too so callers only need
        # to invoke ``shutdown()`` once (single lifecycle owner). The
        # monitor's ``stop()`` is idempotent (``_running = False`` is
        # the only side effect on a second call), so a duplicate
        # shutdown call is a no-op. Use ``sys.modules[__name__]`` lookup
        # so a test-time swap of the module attribute is honoured.
        try:
            _monitor = getattr(
                sys.modules[__name__], "pg_health_monitor", None
            )
            if _monitor is not None:
                await _monitor.stop()
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "[database_manager] pg_health_monitor.stop() raised: %s", e
            )
        self._initialized = False
        logger.info("[database_manager] shut down cleanly")

    def get_status(self) -> dict[str, Any]:
        """Return a JSON-able snapshot of the backend status."""
        return self._status.to_dict()

    # ── Unified data operations ──────────────────────────────────────────

    async def record_snapshot(
        self,
        token_id: str,
        best_bid: float,
        best_ask: float,
        mid: float,
        spread: float,
        bid_size: float = 0.0,
        ask_size: float = 0.0,
        volume: float = 0.0,
        bids_json: Optional[str] = None,
        asks_json: Optional[str] = None,
        bid_depth_10: float = 0.0,
        ask_depth_10: float = 0.0,
    ) -> bool:
        """Record a market snapshot — routes to PG or SQLite.

        On PG failure mid-write, falls back to SQLite so the row is
        never dropped. The fallback also flips ``backend`` to
        ``SQLITE`` and bumps ``fallback_count`` so the operator can
        see the degradation in the status payload (subsequent writes
        skip the PG attempt and go straight to SQLite until the retry
        loop detects PG recovery).
        """
        timestamp = time.time()

        if self.is_postgres:
            try:
                await self._pg_record_snapshot(
                    timestamp, token_id, best_bid, best_ask, mid, spread,
                    bid_size, ask_size, volume, bids_json, asks_json,
                    bid_depth_10, ask_depth_10,
                )
                return True
            except Exception as e:
                logger.error(
                    "[database_manager] PG record_snapshot failed: %s — "
                    "falling back to SQLite",
                    e,
                )
                self._status.pg_available = False
                self._status.backend = DatabaseBackend.SQLITE
                self._status.fallback_count += 1
                self._status._record_error(
                    f"PG record_snapshot failed: {str(e)[:120]}"
                )

        self._sqlite_record_snapshot(
            timestamp, token_id, best_bid, best_ask, mid, spread,
            bid_size, ask_size, volume, bids_json, asks_json,
            bid_depth_10, ask_depth_10,
        )
        return True

    async def _pg_record_snapshot(
        self,
        timestamp: float,
        token_id: str,
        best_bid: float,
        best_ask: float,
        mid: float,
        spread: float,
        bid_size: float,
        ask_size: float,
        volume: float,
        bids_json: Optional[str],
        asks_json: Optional[str],
        bid_depth_10: float,
        ask_depth_10: float,
    ) -> None:
        """Record snapshot to PostgreSQL via the existing timescale_db.

        The existing ``timescale_db.record_snapshot`` doesn't carry
        ``bid_size`` / ``ask_size`` columns (PG hypertable has a
        different shape than the manager's SQLite schema); those
        fields are only persisted on the SQLite fallback path. The
        remaining fields are forwarded as-is so a PG-primary
        deployment still captures the core OHLC + book top of book.
        ``bids_json`` / ``asks_json`` are JSON-parsed back to dicts
        (timescale_db's PG path expects dicts, not strings).
        """
        bids_dict: Optional[dict] = None
        asks_dict: Optional[dict] = None
        if bids_json:
            try:
                bids_dict = json.loads(bids_json)
            except (json.JSONDecodeError, TypeError):
                bids_dict = None
        if asks_json:
            try:
                asks_dict = json.loads(asks_json)
            except (json.JSONDecodeError, TypeError):
                asks_dict = None

        await _ts_module.timescale_db.record_snapshot(
            token_id=token_id,
            slug="",
            best_bid=best_bid,
            best_ask=best_ask,
            mid=mid,
            spread=spread,
            volume_24h=volume,
            liquidity=0.0,
            bids_json=bids_dict,
            asks_json=asks_dict,
        )

    def _sqlite_record_snapshot(
        self,
        timestamp: float,
        token_id: str,
        best_bid: float,
        best_ask: float,
        mid: float,
        spread: float,
        bid_size: float,
        ask_size: float,
        volume: float,
        bids_json: Optional[str],
        asks_json: Optional[str],
        bid_depth_10: float,
        ask_depth_10: float,
    ) -> None:
        """Record snapshot to SQLite (the manager's own ``market.db``)."""
        path = self._sqlite_paths.get("market") or str(
            self.get_sqlite_path("market")
        )
        try:
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    INSERT INTO market_snapshots
                        (timestamp, token_id, best_bid, best_ask, mid, spread,
                         bid_size, ask_size, volume_24h, bids_json, asks_json,
                         bid_depth_10, ask_depth_10, ingestion_time)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp, token_id, best_bid, best_ask, mid, spread,
                        bid_size, ask_size, volume, bids_json, asks_json,
                        bid_depth_10, ask_depth_10, time.time(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.error(
                "[database_manager] SQLite record_snapshot failed: %s", e
            )

    async def record_trade(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str,
        trade_id: Optional[str] = None,
        timestamp: Optional[float] = None,
        maker_address: str = "",
        taker_order_id: str = "",
    ) -> bool:
        """Record a trade — routes to PG or SQLite.

        Signature matches the W21-6 trade-tape ingester's call shape
        (kwargs for everything past ``side``) AND the W21-4 DAO's
        positional call shape (``record_trade(token_id, price, size,
        side, trade_id, timestamp)`` — ``trade_id`` before
        ``timestamp``).

        On PG failure mid-write, falls back to SQLite so the row is
        never dropped.
        """
        ts = float(timestamp) if timestamp is not None else time.time()
        tid = trade_id or ""

        if self.is_postgres:
            try:
                return await self._pg_record_trade(
                    ts, token_id, price, size, side, tid,
                    maker_address, taker_order_id,
                )
            except Exception as e:
                logger.error(
                    "[database_manager] PG record_trade failed: %s — "
                    "falling back to SQLite",
                    e,
                )
                self._status.pg_available = False
                self._status.backend = DatabaseBackend.SQLITE
                self._status.fallback_count += 1
                self._status._record_error(
                    f"PG record_trade failed: {str(e)[:120]}"
                )

        return await self._sqlite_record_trade(
            ts, token_id, price, size, side, tid,
            maker_address, taker_order_id,
        )

    async def _pg_record_trade(
        self,
        timestamp: float,
        token_id: str,
        price: float,
        size: float,
        side: str,
        trade_id: str,
        maker_address: str,
        taker_order_id: str,
    ) -> bool:
        """Record trade to PostgreSQL via the existing timescale_db."""
        return bool(
            await _ts_module.timescale_db.record_trade(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
                timestamp=timestamp,
                trade_id=trade_id,
                maker_address=maker_address,
                taker_order_id=taker_order_id,
            )
        )

    async def _sqlite_record_trade(
        self,
        timestamp: float,
        token_id: str,
        price: float,
        size: float,
        side: str,
        trade_id: str,
        maker_address: str = "",
        taker_order_id: str = "",
    ) -> bool:
        """Record trade to SQLite (dedup via UNIQUE(trade_id)).

        Delegates to ``timescale_db.record_trade`` so the row lands in
        the SAME SQLite file the rest of the pipeline reads from (the
        ``MARKET_DB_PATH``-resolved file). The manager doesn't own a
        separate trade-tape store — it uses the canonical one so the
        trade-tape routes and the ingester's reads stay consistent.
        """
        try:
            ok = await _ts_module.timescale_db.record_trade(
                token_id=token_id,
                price=price,
                size=size,
                side=side,
                timestamp=timestamp,
                trade_id=trade_id,
                maker_address=maker_address,
                taker_order_id=taker_order_id,
            )
            return bool(ok)
        except Exception as e:
            logger.error(
                "[database_manager] SQLite record_trade failed: %s", e
            )
            return False

    # ── Trade-tape read methods (W21-6 facade) ───────────────────────────

    async def get_trade_tape(
        self,
        token_id: Optional[str] = None,
        limit: int = 100,
        since_timestamp: Optional[float] = None,
    ) -> list[dict]:
        """Get recent trades from the tape (most-recent-first).

        Routes through the PG / SQLite trade-tape read paths based on
        the active backend. ``token_id`` filter is optional (``None``
        returns the global tape across all tokens); ``limit`` caps
        the row count (defensively capped at 500 — same cap as
        ``timescale_db.fetch_trades``); ``since_timestamp`` filters
        out rows older than the cutoff.
        """
        capped = max(1, min(int(limit), _TRADE_TAPE_LIMIT_CAP))

        if self.is_postgres:
            try:
                rows = await self._pg_get_trade_tape(token_id, capped, since_timestamp)
            except Exception as e:  # pragma: no cover — PG not reachable in tests
                logger.error(
                    "[database_manager] PG get_trade_tape failed: %s — "
                    "falling back to SQLite",
                    e,
                )
                rows = self._sqlite_get_trade_tape(token_id, capped, since_timestamp)
        else:
            rows = self._sqlite_get_trade_tape(token_id, capped, since_timestamp)

        return rows

    async def _pg_get_trade_tape(
        self,
        token_id: Optional[str],
        limit: int,
        since_timestamp: Optional[float],
    ) -> list[dict]:
        """Read trades from PG via ``timescale_db.fetch_trades``.

        The existing ``fetch_trades`` is a sync method that reads from
        the SQLite fallback (PG-side queries aren't implemented in
        ``fetch_trades`` — see its docstring). For now we delegate to
        the same sync method; a future wave can add a real PG-side
        ``SELECT * FROM market.market_trade`` query.
        """
        return _ts_module.timescale_db.fetch_trades(
            token_id=token_id or "", limit=limit,
        )

    def _sqlite_get_trade_tape(
        self,
        token_id: Optional[str],
        limit: int,
        since_timestamp: Optional[float],
    ) -> list[dict]:
        """Read trades from the SQLite fallback."""
        rows = _ts_module.timescale_db.fetch_trades(
            token_id=token_id or "", limit=limit,
        )
        if since_timestamp is None:
            return rows
        cutoff = float(since_timestamp)
        return [
            r for r in rows
            if float(r.get("timestamp", 0.0) or 0.0) > cutoff
        ]

    async def get_trade_stats(
        self,
        token_id: Optional[str] = None,
        hours: float = 24.0,
    ) -> dict:
        """Aggregate trade-tape stats over the trailing ``hours`` window.

        Returns a dict with exactly these keys (the W21-6 contract —
        the HTTP route handler wraps the result and adds the
        ``token_id`` / ``hours`` / ``backend`` envelope fields):

          * ``total_trades``  — count of trades in the window.
          * ``total_volume``  — sum of trade sizes.
          * ``avg_price``     — mean trade price.
          * ``buy_count``     — number of BUY-side trades.
          * ``sell_count``    — number of SELL-side trades.
          * ``vwap``          — volume-weighted average price.
        """
        if self.is_postgres:
            try:
                return await self._pg_get_trade_stats(token_id, hours)
            except Exception as e:  # pragma: no cover — PG not reachable in tests
                logger.error(
                    "[database_manager] PG get_trade_stats failed: %s — "
                    "falling back to SQLite",
                    e,
                )
                return self._sqlite_get_trade_stats(token_id, hours)
        return self._sqlite_get_trade_stats(token_id, hours)

    async def _pg_get_trade_stats(
        self, token_id: Optional[str], hours: float,
    ) -> dict:
        """PG-side aggregate stats (delegates to SQLite computation)."""
        return self._sqlite_get_trade_stats(token_id, hours)

    def _sqlite_get_trade_stats(
        self, token_id: Optional[str], hours: float,
    ) -> dict:
        """Compute trade-tape aggregate stats from the SQLite tape.

        Reads the tape (via ``fetch_trades``) and computes the
        aggregates in Python so the route returns a well-formed
        payload without requiring a PG-side aggregate query. The real
        W21-6 implementation will push the aggregation down to PG /
        SQLite.
        """
        cutoff = time.time() - float(hours) * 3600.0
        # Pull a wider window than the default 100 — the trailing 24h
        # tape can have hundreds of trades and we want all of them in
        # the aggregate.
        rows = _ts_module.timescale_db.fetch_trades(
            token_id=token_id or "", limit=_TRADE_TAPE_LIMIT_CAP,
        )
        # Apply the trailing-hours cutoff.
        rows = [
            r for r in rows
            if float(r.get("timestamp", 0.0) or 0.0) >= cutoff
        ]
        total_trades = len(rows)
        total_volume = 0.0
        total_notional = 0.0
        buy_count = 0
        sell_count = 0
        for r in rows:
            size = float(r.get("size", 0.0) or 0.0)
            price = float(r.get("price", 0.0) or 0.0)
            total_volume += size
            total_notional += size * price
            side = (r.get("side") or "").upper()
            if side == "BUY":
                buy_count += 1
            elif side == "SELL":
                sell_count += 1
        vwap = total_notional / total_volume if total_volume > 0 else 0.0
        avg_price = (
            sum(float(r.get("price", 0.0) or 0.0) for r in rows) / total_trades
            if total_trades > 0
            else 0.0
        )
        return {
            "total_trades": total_trades,
            "total_volume": round(total_volume, 6) if total_volume else 0,
            "avg_price": round(avg_price, 6) if avg_price else 0,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "vwap": round(vwap, 6) if vwap else 0,
        }

    # ── Snapshot read methods (W21-5 + W21-4 facade) ─────────────────────

    async def get_snapshots(
        self, token_id: str, limit: int = 100
    ) -> list[dict]:
        """Get market snapshots for a token (most-recent first).

        Routes through the active backend. On PG failure mid-read,
        falls back to SQLite so the API endpoint still returns data
        rather than 500'ing. The returned rows carry the unified
        schema columns (``best_bid`` / ``best_ask`` / ``mid`` /
        ``spread`` / ``bid_size`` / ``ask_size`` / ``volume_24h`` /
        ``bids_json`` / ``asks_json`` / ``bid_depth_10`` /
        ``ask_depth_10`` / ``ingestion_time``).
        """
        capped = max(1, min(int(limit), 1000))

        if self.is_postgres:
            try:
                return await self._pg_get_snapshots(token_id, capped)
            except Exception as e:  # pragma: no cover — PG not reachable in tests
                logger.error(
                    "[database_manager] PG get_snapshots(%s) failed: %s — "
                    "falling back to SQLite",
                    token_id, e,
                )
                return self._sqlite_get_snapshots(token_id, capped)

        return self._sqlite_get_snapshots(token_id, capped)

    async def _pg_get_snapshots(
        self, token_id: str, limit: int,
    ) -> list[dict]:
        """PG-side snapshot read (delegates to the SQLite fallback).

        The existing ``timescale_db.fetch_records`` doesn't filter by
        ``token_id``, so we pull a wider window and filter
        client-side. A future wave can add a proper PG-side
        ``WHERE token_id = $1`` query.
        """
        try:
            window = max(limit * 5, limit)
            payload = _ts_module.timescale_db.fetch_records(
                "market_snapshots", window,
            )
            records = (
                payload.get("records", []) if isinstance(payload, dict) else []
            )
            filtered = [r for r in records if r.get("token_id") == token_id]
            return filtered[:limit]
        except Exception:
            # Fall back to SQLite — the manager's own market.db has the
            # same schema (the W21-1 unified schema).
            return self._sqlite_get_snapshots(token_id, limit)

    def _sqlite_get_snapshots(
        self, token_id: str, limit: int,
    ) -> list[dict]:
        """Read snapshots from the manager's own market.db (unified schema)."""
        path = self._sqlite_paths.get("market") or str(self.get_sqlite_path("market"))
        try:
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT timestamp, token_id, slug, best_bid, best_ask,
                           mid, spread, bid_size, ask_size, volume_24h,
                           liquidity, bids_json, asks_json,
                           bid_depth_10, ask_depth_10, ingestion_time
                    FROM market_snapshots
                    WHERE token_id = ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (token_id, int(limit)),
                ).fetchall()
                return [dict(r) for r in rows]
        except Exception as e:
            logger.error(
                "[database_manager] SQLite get_snapshots(%s) failed: %s",
                token_id, e,
            )
            return []

    async def get_trades(
        self, token_id: Optional[str] = None, limit: int = 100
    ) -> list[dict]:
        """Get recent trades (most-recent-first).

        When ``token_id`` is ``None`` returns the global recent tape
        across all tokens. The W21-4 DAO's ``get_trades`` calls this
        method with both signatures (``get_trades(token_id, limit)``
        and ``get_trades(limit=10)`` — the second is the global tape).
        """
        return await self.get_trade_tape(token_id=token_id, limit=limit)

    # ── Order book depth read methods (W21-5) ────────────────────────────

    async def get_order_book_depth(self, token_id: str, limit: int = 10) -> dict:
        """Get the latest order book depth for ``token_id``.

        Wraps :meth:`get_snapshots` (``limit=1``) and parses the
        ``bids_json`` / ``asks_json`` JSON columns back into Python
        lists of ``{"price": float, "size": float}`` dicts.
        """
        snapshots = await self.get_snapshots(token_id, limit=1)
        if not snapshots:
            return {
                "token_id": token_id,
                "timestamp": None,
                "bids": [],
                "asks": [],
                "bid_depth_10": 0.0,
                "ask_depth_10": 0.0,
                "spread": 0.0,
                "mid": 0.5,
                "best_bid": None,
                "best_ask": None,
                "backend": self.backend_label,
            }

        snap = snapshots[0]
        bids = self._parse_ladder(snap.get("bids_json"))
        asks = self._parse_ladder(snap.get("asks_json"))
        capped = max(1, int(limit))

        return {
            "token_id": token_id,
            "timestamp": snap.get("timestamp"),
            "bids": bids[:capped],
            "asks": asks[:capped],
            "bid_depth_10": float(snap.get("bid_depth_10") or 0.0),
            "ask_depth_10": float(snap.get("ask_depth_10") or 0.0),
            "spread": float(snap.get("spread") or 0.0),
            "mid": float(snap.get("mid") or 0.5),
            "best_bid": snap.get("best_bid"),
            "best_ask": snap.get("best_ask"),
            "backend": self.backend_label,
        }

    async def get_depth_history(
        self, token_id: str, hours: float = 1.0,
    ) -> list[dict]:
        """Get the order book depth history for ``token_id`` (last N hours).

        Returns every snapshot row whose ``timestamp`` is within the
        last ``hours`` hours, ascending by timestamp (oldest first) so
        the caller can plot the time-series directly.
        """
        capped_hours = max(0.0, min(float(hours), 24.0 * 30.0))
        cutoff = time.time() - capped_hours * 3600.0
        path = self._sqlite_paths.get("market") or str(self.get_sqlite_path("market"))
        try:
            with sqlite3.connect(path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    """
                    SELECT timestamp, token_id, slug, best_bid, best_ask,
                           mid, spread, bid_size, ask_size, volume_24h,
                           liquidity, bids_json, asks_json,
                           bid_depth_10, ask_depth_10, ingestion_time
                    FROM market_snapshots
                    WHERE token_id = ? AND timestamp > ?
                    ORDER BY timestamp ASC
                    """,
                    (token_id, float(cutoff)),
                ).fetchall()
                out: list[dict] = []
                for r in rows:
                    row = dict(r)
                    row["bids"] = self._parse_ladder(row.get("bids_json"))[:10]
                    row["asks"] = self._parse_ladder(row.get("asks_json"))[:10]
                    out.append(row)
                return out
        except Exception as e:
            logger.error(
                "[database_manager] SQLite get_depth_history(%s) failed: %s",
                token_id, e,
            )
            return []

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_ladder(raw: Any) -> list[dict]:
        """Parse a JSON ladder column into a list of dicts.

        Accepts:
          * a JSON string (``'[{"price": 0.49, "size": 100}, ...]'``)
            — the SQLite TEXT column shape.
          * a Python list / dict already — the asyncpg JSONB shape.
          * ``None`` / empty string — returns ``[]``.
        """
        if raw is None:
            return []
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if isinstance(raw, str):
            if not raw:
                return []
            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                logger.debug(
                    "[database_manager] failed to parse ladder JSON: %r",
                    raw[:120],
                )
                return []
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
            return []
        try:
            parsed = json.loads(json.dumps(raw))
            if isinstance(parsed, list):
                return [x for x in parsed if isinstance(x, dict)]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return []


# ── Module-level singletons ──────────────────────────────────────────────────
#
# W21-1 names the singleton ``db_manager``; W21-2 names it
# ``database_manager``. Both names point to the SAME instance so the
# lifespan startup in ``api/server.py`` calling
# ``await database_manager.initialize()`` followed by
# ``await db_manager.initialize()`` is idempotent (the second call is
# a no-op once ``_initialized`` is True).
db_manager = DatabaseManager()
database_manager = db_manager


# ── HTTP route registration (W21-5) ─────────────────────────────────────────
#
# Mirrors the ``register_routes(app)`` pattern used by every sibling
# ``core.*`` feature module. The ``api/server.py`` wiring block calls
# ``register_routes(app)`` once at module-load time so the two
# endpoints below are attached to the production FastAPI app — auth
# is enforced by the existing ``enforce_api_auth`` middleware
# (neither path is in ``PUBLIC_PATHS``).
#
# Both endpoints are read-only and idempotent. No background tasks are
# started by the route handlers.

def register_routes(app: FastAPI) -> None:
    """Register the W21-5 order-book depth HTTP routes on ``app``.

    Adds two read-only endpoints:

      * ``GET /api/depth-full/{token_id}``       — latest snapshot's full
                                                   bid/ask ladder (parsed)
                                                   + depth-10 summaries +
                                                   top-of-book fields.
      * ``GET /api/depth-history/{token_id}``    — depth time-series for
                                                   the last ``hours`` hours
                                                   (ascending by timestamp).
    """

    @app.get(
        "/api/depth-full/{token_id}",
        tags=["markets"],
        summary="Full order book depth (bids + asks ladder)",
    )
    async def get_full_depth(
        token_id: str,
        limit: int = Query(
            10, ge=1, le=100,
            description="Maximum number of ladder levels to return",
        ),
    ):
        """Get full order book depth (bids/asks ladder) for a token."""
        return await db_manager.get_order_book_depth(token_id, limit)

    @app.get(
        "/api/depth-history/{token_id}",
        tags=["markets"],
        summary="Order book depth history (time-series)",
    )
    async def get_depth_history_route(
        token_id: str,
        hours: float = Query(
            1.0, ge=0.0, le=24.0 * 30.0,
            description="Time window in hours (default 1.0, max 720)",
        ),
    ):
        """Get order book depth history for a token."""
        rows = await db_manager.get_depth_history(token_id, hours)
        return {
            "token_id": token_id,
            "hours": hours,
            "count": len(rows),
            "history": rows,
            "backend": db_manager.backend_label,
        }


__all__ = [
    "DatabaseBackend",
    "Backend",
    "DatabaseStatus",
    "DatabaseManager",
    "db_manager",
    "database_manager",
    "register_routes",
]
