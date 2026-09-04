"""Unified database migration system — supports both SQLite and PostgreSQL.

W21-3 — extended from the W13-7 SQLite-only migration runner to a
unified backend-aware system that applies the same ``.sql`` migration
files on either PostgreSQL or SQLite.

Tracking
--------
Each backend maintains its own migration-tracker table:

  * **SQLite** — ``_migrations`` (per-DB file). Columns: ``id``,
    ``name``, ``applied_at``, ``backend``. The ``backend`` column was
    added in W21-3 (defaults to ``"sqlite"`` for pre-existing rows).
  * **PostgreSQL** — ``operations.schema_migration`` (shared across
    the cluster). Columns: ``version``, ``name``, ``applied_at``,
    ``checksum``, ``execution_time_ms``, ``backend``. Same shape as
    the W12 ``migration_runner`` schema; the W21-3 ``backend`` column
    defaults to ``"postgres"``.

Translation
-----------
Migration files are written in the PostgreSQL-canonical form
(``SERIAL PRIMARY KEY`` for auto-increment). The SQLite runner
translates this to ``INTEGER PRIMARY KEY AUTOINCREMENT`` at execution
time via ``_translate_for_sqlite``. This lets the same ``.sql`` file
run on either backend without per-backend forks.

Discovery / filtering
---------------------
The migrations directory is shared with the W12 PostgreSQL/TimescaleDB
enterprise runner (``001_initial_enterprise_schemas.sql``) and the W13-7
SQLite-only runner (``001_initial_schema.sql``). Each backend filters
out files it cannot execute:

  * SQLite skips files containing PG-only tokens (``TIMESTAMPTZ``,
    ``JSONB``, ``create_hypertable``, ``CREATE SCHEMA``, etc.).
    Note: ``SERIAL PRIMARY KEY`` is NOT in this list — it's translated.
  * PostgreSQL skips files containing SQLite-only tokens
    (``AUTOINCREMENT``). The unified ``002_unified_schema.sql`` is
    compatible with both.

Conflict tolerance
------------------
When a migration runs after another migration that created the same
table with a different schema, ``CREATE TABLE IF NOT EXISTS`` is a
no-op (the existing table is preserved). ``CREATE INDEX`` on a column
that doesn't exist (because the prior migration didn't create it) is
logged as a warning and skipped — the migration continues. This allows
the unified ``002_unified_schema.sql`` to run after the SQLite-only
``001_initial_schema.sql`` without aborting on column-mismatch index
errors (e.g. ``idx_de_corr ON decision_events(correlation_id)`` when
``correlation_id`` wasn't a column in 001's schema).

API
---
The primary entry points are:

  * ``run_migrations(db_path, backend_or_name="default", backend=None)``
    — sync; dispatches to SQLite or PostgreSQL based on the second
    argument / explicit ``backend`` kwarg.
  * ``run_migrations_async(db_path, backend="sqlite", db_name="default")``
    — async; supports both backends. Used by the
    ``scripts/migrate_db.py`` runner.
  * ``get_migration_status(db_path, backend="sqlite")`` — read-only.

Backward compatibility
----------------------
The pre-W21-3 signature ``run_migrations(db_path, "fresh")`` (where
``"fresh"`` was a logical db_name used in log lines) is preserved: if
the second positional argument is not a known backend name
(``"sqlite"`` / ``"postgres"``), it's treated as the db_name and the
backend defaults to ``"sqlite"``. Existing callers in
``api/server.py``'s lifespan and ``scripts/migrate.py`` continue to
work without modification.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# Backends supported by the unified runner.
KNOWN_BACKENDS: tuple[str, ...] = ("sqlite", "postgres")

# Aliases — callers may pass the ``DatabaseBackend`` enum's ``.value``
# form (``"postgresql"`` from W21-1's ``DatabaseBackend.POSTGRESQL.value``)
# rather than the shorter ``"postgres"`` label. Normalize them so the
# dispatcher sees a canonical backend name.
_BACKEND_ALIASES: dict[str, str] = {
    "postgresql": "postgres",
    "postgres": "postgres",
    "sqlite": "sqlite",
}

# ── PostgreSQL / TimescaleDB-specific tokens that disqualify a .sql file
# from being loaded by the SQLite runner. The list is intentionally
# conservative: a single hit is enough to skip the file (every token is
# unambiguous — none appear in legitimate SQLite DDL).
# Note: ``"serial primary key"`` was REMOVED in W21-3 — the unified
# migration (002) uses SERIAL as the canonical auto-increment syntax,
# which is now translated to ``INTEGER PRIMARY KEY AUTOINCREMENT`` for
# SQLite by ``_translate_for_sqlite()``. The PG-only files that use
# SERIAL natively (e.g. ``001_initial_enterprise_schemas.sql``) are
# still filtered out because they also use other PG tokens
# (TIMESTAMPTZ, JSONB, create_hypertable, …) that remain in the list.
_POSTGRES_TOKENS: tuple[str, ...] = (
    "timestamptz",
    "jsonb",
    "create_hypertable",
    "create_extension",
    "create schema",
    "materialized view",
    "uuid_generate_v4",
    "time_bucket",
    "::jsonb",
    "::text[]",
    "double precision[]",
    "with (timescaledb.continuous)",
    "on conflict do nothing",
    "create extension",
)

# SQLite-specific tokens that disqualify a .sql file from being loaded
# by the PostgreSQL runner. ``AUTOINCREMENT`` is the SQLite-only
# auto-increment syntax; PG uses ``SERIAL`` instead (translated for
# SQLite by ``_translate_for_sqlite``).
_SQLITE_TOKENS: tuple[str, ...] = (
    "autoincrement",
    "integer primary key autoincrement",
)


# ── Comment stripping (for token scanning) ─────────────────────────────────────


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL line and block comments for token scanning.

    Used by ``_is_sqlite_compatible`` / ``_is_pg_compatible`` so that
    inline comments mentioning PG-specific syntax (e.g. ``-- SQLite:
    INTEGER PRIMARY KEY AUTOINCREMENT`` in the unified migration 002)
    don't cause false-positive disqualification.
    """
    # Block comments (/* ... */) — non-greedy, DOTALL so newlines match.
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    # Line comments — strip from ``--`` to end of line, but only when
    # the ``--`` is not inside a single-quoted string literal. Naive
    # but adequate for migration DDL (no ``--`` inside string defaults).
    out_lines: list[str] = []
    for line in sql.splitlines():
        in_str = False
        cut_at: Optional[int] = None
        i = 0
        while i < len(line):
            ch = line[i]
            if ch == "'":
                in_str = not in_str
            elif not in_str and ch == "-" and i + 1 < len(line) and line[i + 1] == "-":
                cut_at = i
                break
            i += 1
        if cut_at is not None:
            line = line[:cut_at]
        out_lines.append(line)
    return "\n".join(out_lines)


# ── Compatibility checks ──────────────────────────────────────────────────────


def _is_sqlite_compatible(path: Path) -> bool:
    """Return ``True`` if ``path`` is safe to load via ``sqlite3``.

    A file is considered SQLite-incompatible if any PostgreSQL-specific
    token (case-insensitive, AFTER stripping comments) appears in its
    content. The token list excludes ``"serial primary key"`` because
    the unified migration system translates it for SQLite at execution
    time (see ``_translate_for_sqlite``).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        # If we cannot even read the file, treat it as incompatible so
        # we don't crash the migration sequence on a permission error.
        return False
    stripped = _strip_sql_comments(content).lower()
    for token in _POSTGRES_TOKENS:
        if token in stripped:
            return False
    return True


def _is_pg_compatible(path: Path) -> bool:
    """Return ``True`` if ``path`` is safe to load via ``asyncpg`` (PG).

    A file is considered PG-incompatible if any SQLite-specific token
    (case-insensitive, AFTER stripping comments) appears in its content.
    The SQLite-only ``001_initial_schema.sql`` uses ``AUTOINCREMENT``
    and is therefore filtered out for PG; the unified
    ``002_unified_schema.sql`` uses ``SERIAL`` (PG-native) and passes.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    stripped = _strip_sql_comments(content).lower()
    for token in _SQLITE_TOKENS:
        if token in stripped:
            return False
    return True


def _get_available_migrations(backend: str = "sqlite") -> list[Path]:
    """Get all ``.sql`` migration files compatible with the given backend.

    Files are sorted lexically by filename; the ``NNN_`` numeric prefix
    convention (e.g. ``001_initial_schema.sql``) guarantees the lexical
    order matches the intended application order.
    """
    if not MIGRATIONS_DIR.exists():
        return []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if backend == "sqlite":
        return [f for f in files if _is_sqlite_compatible(f)]
    elif backend == "postgres":
        return [f for f in files if _is_pg_compatible(f)]
    else:
        raise ValueError(f"Unknown backend: {backend!r}")


# ── SQLite translation ────────────────────────────────────────────────────────


def _translate_for_sqlite(sql: str) -> str:
    """Translate PostgreSQL-specific syntax to SQLite-compatible equivalents.

    Currently translates:
      * ``SERIAL PRIMARY KEY`` → ``INTEGER PRIMARY KEY AUTOINCREMENT``

    This allows migration files to be written in the PostgreSQL-canonical
    form while remaining executable on SQLite. Future backend-specific
    translations (e.g. ``TIMESTAMPTZ`` → ``TEXT``) can be added here.
    """
    # Translate SERIAL PRIMARY KEY (case-insensitive, word-bounded).
    sql = re.sub(
        r"\bSERIAL\s+PRIMARY\s+KEY\b",
        "INTEGER PRIMARY KEY AUTOINCREMENT",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


# ── SQL statement splitter ────────────────────────────────────────────────────


def _split_sql_statements(sql: str) -> list[str]:
    """Split SQL into individual statements.

    Handles:
      * Line comments (``--`` to end of line) — skipped.
      * Single-quoted string literals — ``;`` inside strings is preserved.
      * Statement separator ``;`` (outside strings).

    Does NOT handle:
      * Block comments (``/* */``) — callers should pre-strip if needed.
      * Double-quoted identifiers — adequate for migration DDL.

    Returns the list of non-empty, stripped statements.
    """
    statements: list[str] = []
    current: list[str] = []
    in_string = False
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        # Line comment — skip to end of line.
        if not in_string and c == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            continue
        # Single-quoted string literal — toggle in_string on each unescaped quote.
        if c == "'":
            in_string = not in_string
            current.append(c)
            i += 1
            continue
        # Statement separator (outside strings).
        if c == ";" and not in_string:
            stmt = "".join(current).strip()
            if stmt:
                statements.append(stmt)
            current = []
            i += 1
            continue
        current.append(c)
        i += 1
    # Trailing statement (no semicolon).
    last = "".join(current).strip()
    if last:
        statements.append(last)
    return statements


# ── SQLite migration tracker ──────────────────────────────────────────────────


def _ensure_migrations_table_sqlite(conn: sqlite3.Connection) -> None:
    """Create the ``_migrations`` table with the ``backend`` column.

    For databases created before W21-3 (which have ``_migrations``
    without the ``backend`` column), the column is added via
    ``ALTER TABLE`` with ``DEFAULT 'sqlite'`` so pre-existing rows are
    backfilled correctly.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at REAL NOT NULL,
            backend TEXT NOT NULL DEFAULT 'sqlite'
        )
        """
    )
    # Add the backend column if missing (DBs created before W21-3).
    try:
        cols = [
            row[1]
            for row in conn.execute("PRAGMA table_info(_migrations)").fetchall()
        ]
        if "backend" not in cols:
            conn.execute(
                "ALTER TABLE _migrations "
                "ADD COLUMN backend TEXT NOT NULL DEFAULT 'sqlite'"
            )
    except sqlite3.OperationalError:
        # Column already exists (race with concurrent runner) — no-op.
        pass
    conn.commit()


# W13-7 backward-compat alias — the pre-W21-3 name was
# ``_ensure_migrations_table(conn)``. Sibling test modules
# (``tests/test_migrations.py``) import this name directly. Delegate to
# the renamed function so existing imports keep working without
# modification.
def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Backward-compat alias for :func:`_ensure_migrations_table_sqlite`."""
    return _ensure_migrations_table_sqlite(conn)


def _get_applied_migrations_sqlite(
    conn: sqlite3.Connection, backend: str = "sqlite"
) -> set[str]:
    """Get the set of migration names already applied on the given backend.

    Returns an empty set when the ``_migrations`` table does not exist
    yet so the caller can ``_ensure_migrations_table_sqlite`` and
    proceed without a noisy OperationalError.

    Pre-W21-3 rows (with ``backend IS NULL`` after the ALTER TABLE
    backfill) are treated as ``backend = 'sqlite'`` (the only backend
    that existed before W21-3).
    """
    try:
        cursor = conn.execute(
            "SELECT name FROM _migrations WHERE backend = ? OR backend IS NULL",
            (backend,),
        )
        return {row[0] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return set()


# W13-7 backward-compat alias — the pre-W21-3 signature was
# ``_get_applied_migrations(conn)`` (no backend arg). Sibling test
# modules (``tests/test_migrations.py``) import this name directly.
# Delegate to the renamed function with the default backend so existing
# imports keep working without modification.
def _get_applied_migrations(conn: sqlite3.Connection) -> set[str]:
    """Backward-compat alias for :func:`_get_applied_migrations_sqlite`."""
    return _get_applied_migrations_sqlite(conn, "sqlite")


def _execute_sqlite_migration(
    conn: sqlite3.Connection, sql: str, name: str
) -> list[dict]:
    """Execute a migration's SQL statement-by-statement on SQLite.

    Translates ``SERIAL PRIMARY KEY`` → ``INTEGER PRIMARY KEY AUTOINCREMENT``
    before splitting into statements.

    Tolerates ``CREATE INDEX`` failures caused by missing columns
    (which arise when the migration runs after another migration that
    created the same table with a different schema, e.g. 002 after
    001). Such failures are logged as warnings and the migration
    continues; the warning dicts are returned so the caller can surface
    them in ``result["warnings"]``.

    Any other OperationalError is re-raised so the caller can mark the
    migration as failed and halt the sequence.
    """
    translated = _translate_for_sqlite(sql)
    statements = _split_sql_statements(translated)
    warnings: list[dict] = []
    for stmt in statements:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            err_msg = str(e).lower()
            stmt_upper = stmt.upper().lstrip()
            # Tolerate CREATE INDEX on a column that doesn't exist
            # (because a prior migration created the table with a
            # different schema). This is a warning, not an error.
            if (
                stmt_upper.startswith("CREATE INDEX")
                or stmt_upper.startswith("CREATE UNIQUE INDEX")
            ) and "no such column" in err_msg:
                logger.warning(
                    "[migration:%s] Skipping index (column missing from prior schema): %s",
                    name,
                    stmt.split("\n")[0][:100],
                )
                warnings.append(
                    {"statement": stmt[:200], "error": str(e)}
                )
                continue
            raise
    return warnings


# ── Argument resolution ───────────────────────────────────────────────────────


def _resolve_backend_and_name(
    db_name_or_backend: str, backend: Optional[str]
) -> tuple[str, str]:
    """Resolve the backend and db_name from the call arguments.

    Supports three call styles:

      1. **Legacy** (W13-7): ``run_migrations(db_path, "fresh")`` — the
         second positional arg is a logical db_name used in log lines;
         backend defaults to ``"sqlite"``.
      2. **Modern positional**: ``run_migrations(db_path, "sqlite")`` —
         the second positional arg is the backend name; db_name defaults
         to ``"default"``.
      3. **Explicit kwarg**: ``run_migrations(db_path, db_name="fresh",
         backend="postgres")`` — the explicit ``backend`` kwarg wins.

    Auto-detection: if the second positional arg matches a known backend
    name (``"sqlite"`` / ``"postgres"`` / ``"postgresql"``), it's
    treated as the backend; otherwise it's treated as the db_name.

    Aliases: ``"postgresql"`` (the ``DatabaseBackend.POSTGRESQL.value``
    form used by W21-1's enum) is normalized to ``"postgres"`` so the
    dispatcher sees a canonical name.
    """
    # Explicit kwarg wins.
    if backend is not None:
        normalized = _BACKEND_ALIASES.get(backend.lower(), backend.lower())
        return normalized, db_name_or_backend
    # Check if the positional arg is a known backend name (or alias).
    lowered = db_name_or_backend.lower()
    if lowered in _BACKEND_ALIASES:
        return _BACKEND_ALIASES[lowered], "default"
    # Legacy: treat as db_name, default to SQLite.
    return "sqlite", db_name_or_backend


# ── Sync entry point ──────────────────────────────────────────────────────────


def run_migrations(
    db_path: Path | str,
    db_name_or_backend: str = "default",
    backend: Optional[str] = None,
) -> dict:
    """Run pending migrations on a database (SQLite or PostgreSQL).

    Args:
        db_path: Path to the SQLite database file. Used when backend is
            ``"sqlite"``; ignored when backend is ``"postgres"`` (PG uses
            ``DATABASE_URL``).
        db_name_or_backend: Either the backend name (``"sqlite"`` /
            ``"postgres"`` — auto-detected) or a logical db_name used in
            log lines (legacy API).
        backend: Explicit backend override. Takes precedence over
            ``db_name_or_backend``.

    Returns:
        Dict with migration results::

            {
                "applied":   [<migration filenames>],
                "skipped":   [<already-applied filenames>],
                "errors":    [{"name": ..., "error": "..."}],
                "warnings":  [{"name": ..., "statement": ..., "error": "..."}],
                "backend":   "sqlite" | "postgres",
            }

    On the first **error** the runner stops (migrations are sequential
    — a failed migration cannot be safely skipped). The error is
    recorded in ``result["errors"]`` for the caller to surface via the
    CLI. **Warnings** (e.g. CREATE INDEX skipped due to a missing
    column from a prior migration) do NOT halt the sequence — the
    migration is still marked as applied.
    """
    backend_name, db_name = _resolve_backend_and_name(db_name_or_backend, backend)

    if backend_name == "sqlite":
        result = _run_sqlite_migrations(Path(db_path), db_name)
    elif backend_name == "postgres":
        result = _run_postgres_migrations_sync(Path(db_path), db_name)
    else:
        result = {
            "applied": [],
            "skipped": [],
            "errors": [
                {"name": "backend", "error": f"Unknown backend: {backend_name!r}"}
            ],
            "warnings": [],
        }
    result.setdefault("backend", backend_name)
    return result


def _run_sqlite_migrations(db_path: Path, db_name: str) -> dict:
    """Run pending SQLite migrations against ``db_path``.

    Sequential + idempotent. Stops on the first error. Warnings
    (CREATE INDEX on missing column) are recorded but do not halt.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    result: dict = {"applied": [], "skipped": [], "errors": [], "warnings": []}

    try:
        with sqlite3.connect(str(db_path)) as conn:
            _ensure_migrations_table_sqlite(conn)
            applied = _get_applied_migrations_sqlite(conn, "sqlite")
            available = _get_available_migrations("sqlite")

            for migration_file in available:
                name = migration_file.name
                if name in applied:
                    result["skipped"].append(name)
                    continue

                try:
                    sql = migration_file.read_text(encoding="utf-8")
                    warns = _execute_sqlite_migration(conn, sql, name)
                    if warns:
                        result["warnings"].extend(
                            {"name": name, **w} for w in warns
                        )
                    conn.execute(
                        "INSERT INTO _migrations (name, applied_at, backend) "
                        "VALUES (?, ?, ?)",
                        (name, time.time(), "sqlite"),
                    )
                    conn.commit()
                    result["applied"].append(name)
                    logger.info("[%s] Applied migration: %s", db_name, name)
                except Exception as e:
                    conn.rollback()
                    result["errors"].append({"name": name, "error": str(e)})
                    logger.error(
                        "[%s] Migration %s failed: %s", db_name, name, e
                    )
                    break  # Stop on first error — migrations are sequential
    except Exception as e:
        result["errors"].append({"name": "connection", "error": str(e)})
        logger.error("[%s] Migration connection failed: %s", db_name, e)

    return result


# ── Async entry point ─────────────────────────────────────────────────────────


async def run_migrations_async(
    db_path: Path | str,
    backend: str = "sqlite",
    db_name: str = "default",
) -> dict:
    """Async version of ``run_migrations`` — supports both backends.

    For SQLite, runs the sync path in a thread executor (sqlite3 is
    blocking). For PostgreSQL, uses asyncpg directly (no thread).
    Used by ``scripts/migrate_db.py`` so the runner can be invoked from
    an async context (e.g. a FastAPI lifespan or an asyncio CLI).
    """
    if backend == "sqlite":
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: run_migrations(db_path, db_name, backend="sqlite")
        )
    elif backend == "postgres":
        return await _run_postgres_migrations_async(Path(db_path), db_name)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")


def _run_postgres_migrations_sync(db_path: Path, db_name: str) -> dict:
    """Sync wrapper for PG migrations — uses ``asyncio.run`` with thread fallback.

    When called from inside a running event loop (e.g. from
    ``run_migrations_async`` or a FastAPI lifespan), spins up a fresh
    event loop in a worker thread to avoid the "asyncio.run() cannot
    be called from a running event loop" error.
    """
    try:
        asyncio.get_running_loop()
        # We're inside an event loop — run asyncpg in a worker thread
        # with a fresh loop.
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                asyncio.run, _run_postgres_migrations_async(db_path, db_name)
            )
            return future.result()
    except RuntimeError:
        # No running loop — use asyncio.run directly.
        return asyncio.run(_run_postgres_migrations_async(db_path, db_name))


async def _run_postgres_migrations_async(db_path: Path, db_name: str) -> dict:
    """Run pending PostgreSQL migrations via asyncpg.

    Uses the existing ``operations.schema_migration`` table (same as
    the W12 ``MigrationRunner``) with the W21-3 ``backend`` column
    added. Migration files are executed statement-by-statement (split
    by ``_split_sql_statements``) so CREATE INDEX failures on missing
    columns can be tolerated the same way as on SQLite.
    """
    result: dict = {"applied": [], "skipped": [], "errors": [], "warnings": []}

    try:
        import asyncpg
    except ImportError:
        result["errors"].append(
            {"name": "asyncpg", "error": "asyncpg not installed"}
        )
        logger.warning(
            "[%s] asyncpg not installed — skipping PostgreSQL migrations",
            db_name,
        )
        return result

    db_url = os.environ.get(
        "DATABASE_URL",
        "postgresql://postgres:polymarket_secret@timescaledb:5432/polymarket",
    )
    try:
        conn = await asyncpg.connect(db_url, timeout=10.0)
    except Exception as e:
        result["errors"].append({"name": "connection", "error": str(e)})
        logger.error("[%s] PG migration connection failed: %s", db_name, e)
        return result

    try:
        # Ensure the operations schema + tracker table exist.
        await conn.execute("CREATE SCHEMA IF NOT EXISTS operations;")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS operations.schema_migration (
                version VARCHAR(64) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                checksum VARCHAR(64) NOT NULL,
                execution_time_ms DOUBLE PRECISION NOT NULL,
                backend TEXT NOT NULL DEFAULT 'postgres'
            );
            """
        )
        # Add the backend column if missing (DBs migrated before W21-3).
        try:
            cols = [
                r["column_name"]
                for r in await conn.fetch(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'operations' "
                    "AND table_name = 'schema_migration'"
                )
            ]
            if "backend" not in cols:
                await conn.execute(
                    "ALTER TABLE operations.schema_migration "
                    "ADD COLUMN backend TEXT NOT NULL DEFAULT 'postgres'"
                )
        except Exception:
            # Column exists or table doesn't — defensive no-op.
            pass

        rows = await conn.fetch(
            "SELECT version, name FROM operations.schema_migration "
            "WHERE backend = 'postgres' OR backend IS NULL"
        )
        applied = {r["version"]: r["name"] for r in rows}
        available = _get_available_migrations("postgres")

        for migration_file in available:
            version = migration_file.stem.split("_")[0]
            name = migration_file.name
            if version in applied:
                result["skipped"].append(name)
                continue

            try:
                sql = migration_file.read_text(encoding="utf-8")
                start = time.perf_counter()
                warns: list[dict] = []
                async with conn.transaction():
                    statements = _split_sql_statements(sql)
                    for stmt in statements:
                        try:
                            await conn.execute(stmt)
                        except asyncpg.PostgresError as e:
                            stmt_upper = stmt.upper().lstrip()
                            err_msg = str(e).lower()
                            if (
                                stmt_upper.startswith("CREATE INDEX")
                                or stmt_upper.startswith("CREATE UNIQUE INDEX")
                            ) and (
                                "does not exist" in err_msg
                                or "column" in err_msg
                            ):
                                logger.warning(
                                    "[migration:%s] Skipping PG index (column missing): %s",
                                    name,
                                    stmt.split("\n")[0][:100],
                                )
                                warns.append(
                                    {"statement": stmt[:200], "error": str(e)}
                                )
                                continue
                            raise
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                    await conn.execute(
                        "INSERT INTO operations.schema_migration "
                        "(version, name, checksum, execution_time_ms, backend) "
                        "VALUES ($1, $2, $3, $4, $5)",
                        version,
                        name,
                        checksum,
                        elapsed_ms,
                        "postgres",
                    )
                if warns:
                    result["warnings"].extend(
                        {"name": name, **w} for w in warns
                    )
                result["applied"].append(name)
                logger.info("[%s] Applied PG migration: %s", db_name, name)
            except Exception as e:
                result["errors"].append({"name": name, "error": str(e)})
                logger.error(
                    "[%s] PG migration %s failed: %s", db_name, name, e
                )
                break
    except Exception as e:
        result["errors"].append({"name": "execution", "error": str(e)})
        logger.error("[%s] PG migration execution failed: %s", db_name, e)
    finally:
        try:
            await conn.close()
        except Exception:
            pass

    return result


# ── Status (read-only) ────────────────────────────────────────────────────────


def get_migration_status(
    db_path: Path | str, backend: str = "sqlite"
) -> dict:
    """Get migration status for a database (read-only — never writes).

    Returns a dict with ``applied`` / ``available`` / ``pending`` lists
    of migration filenames. When the database file does not exist yet,
    every available migration is reported as pending.

    For ``backend="postgres"``, this returns the available + pending
    lists only (querying PG requires asyncpg + an async context —
    callers that need the PG applied list should use
    ``run_migrations_async(backend="postgres")`` instead).
    """
    db_path = Path(db_path)
    available = [f.name for f in _get_available_migrations(backend)]

    if backend == "sqlite":
        if not db_path.exists():
            return {
                "applied": [],
                "available": available,
                "pending": list(available),
            }
        try:
            with sqlite3.connect(str(db_path)) as conn:
                _ensure_migrations_table_sqlite(conn)
                applied = _get_applied_migrations_sqlite(conn, "sqlite")
                pending = [m for m in available if m not in applied]
                return {
                    "applied": sorted(applied),
                    "available": available,
                    "pending": pending,
                }
        except Exception as e:
            return {"error": str(e)}
    elif backend == "postgres":
        # PG status is read-only at the sync layer — return available +
        # pending (callers needing the applied list should use the
        # async path).
        return {
            "applied": [],
            "available": available,
            "pending": list(available),
        }
    else:
        return {"error": f"Unknown backend: {backend!r}"}


def create_migration(name: str) -> Path:
    """Create a new empty migration file with the next sequential number.

    The filename follows ``NNN_<sanitised_name>.sql`` where ``NNN`` is
    the next available sequence number (zero-padded to three digits so
    lexical sort matches numeric sort up to migration #999).
    """
    # Use the *full* migrations dir (not the backend-filtered list) when
    # computing the next sequence number, so we never collide with a
    # backend-only file that shares the same prefix. We walk every
    # ``*.sql`` file present, parse the leading integer, and pick max+1.
    all_files = (
        sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
    )
    next_num = 1
    for f in all_files:
        prefix = f.stem.split("_", 1)[0]
        if prefix.isdigit():
            next_num = max(next_num, int(prefix) + 1)

    # Sanitise the name: keep alphanumerics + underscores only.
    safe_name = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)
    filename = f"{next_num:03d}_{safe_name}.sql"
    path = MIGRATIONS_DIR / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"-- Migration: {safe_name}\n"
        f"-- Created: {datetime.now().isoformat()}\n\n"
        "-- Add your DDL here. Use CREATE TABLE IF NOT EXISTS /\n"
        "-- CREATE INDEX IF NOT EXISTS so the migration is idempotent.\n"
        "-- Use ``SERIAL PRIMARY KEY`` for auto-increment (translated to\n"
        "-- ``INTEGER PRIMARY KEY AUTOINCREMENT`` for SQLite automatically).\n\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "MIGRATIONS_DIR",
    "KNOWN_BACKENDS",
    "run_migrations",
    "run_migrations_async",
    "get_migration_status",
    "create_migration",
    "_is_sqlite_compatible",
    "_is_pg_compatible",
    "_translate_for_sqlite",
    "_split_sql_statements",
    "_get_available_migrations",
    "_resolve_backend_and_name",
]
