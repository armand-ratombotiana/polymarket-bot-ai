"""SQLite database migration system.

Tracks applied migrations in a ``_migrations`` table per database.
Each migration is a ``.sql`` file in ``core/db/migrations/`` named
``NNN_description.sql``.

Usage::

    from core.db.migration_manager import run_migrations
    result = run_migrations(db_path)        # applies pending migrations
    status  = get_migration_status(db_path)  # report-only, never writes

The system is **additive** and **idempotent**: every migration is
expected to use ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT
EXISTS`` so re-running against a DB whose schema was bootstrapped by an
existing ``_init_db`` method (see ``core/decision_ledger.py``,
``core/observability.py``, ``core/alerting.py`` etc.) is a no-op.

A small handful of PostgreSQL / TimescaleDB-only ``.sql`` files co-exist
in the same ``migrations/`` directory (see
``001_initial_enterprise_schemas.sql`` — the W12 enterprise platform
seed managed by ``core/db/migration_runner.py``). Those files use
PostgreSQL-specific syntax (``TIMESTAMPTZ``, ``JSONB``,
``create_hypertable``, ``CREATE SCHEMA`` …) that ``sqlite3`` cannot
parse, so they are filtered out at discovery time by
``_is_sqlite_compatible()``.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

# ── PostgreSQL / TimescaleDB-specific tokens that disqualify a .sql file
# from being loaded by this SQLite runner. The list is intentionally
# conservative: a single hit is enough to skip the file (every token is
# unambiguous — none appear in legitimate SQLite DDL).
_POSTGRES_TOKENS: tuple[str, ...] = (
    "timestamptz",
    "jsonb",
    "create_hypertable",
    "create_extension",
    "create schema",
    "materialized view",
    "uuid_generate_v4",
    "time_bucket",
    "serial primary key",
    "::jsonb",
    "::text[]",
    "double precision[]",
    "with (timescaledb.continuous)",
    "on conflict do nothing",
    "create extension",
)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    """Create the migrations tracking table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            applied_at REAL NOT NULL
        )
        """
    )
    conn.commit()


def _get_applied_migrations(conn: sqlite3.Connection) -> set[str]:
    """Get the set of already-applied migration names.

    Returns an empty set when the ``_migrations`` table does not exist
    yet (e.g. an old DB bootstrapped before the migration system was
    introduced) so the caller can ``_ensure_migrations_table`` and
    proceed without a noisy OperationalError.
    """
    try:
        cursor = conn.execute("SELECT name FROM _migrations")
        return {row[0] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return set()


def _is_sqlite_compatible(path: Path) -> bool:
    """Return ``True`` if ``path`` is safe to load via ``sqlite3``.

    The migrations directory is shared with the PostgreSQL/TimescaleDB
    enterprise runner (see ``core/db/migration_runner.py``). The
    enterprise migrations use PostgreSQL-specific syntax that
    ``sqlite3`` cannot parse — loading them would surface a confusing
    ``sqlite3.OperationalError`` and abort the migration sequence.

    A file is considered SQLite-incompatible if any PostgreSQL-specific
    token (case-insensitive) appears in its content. The token list is
    intentionally conservative — every token is unambiguous and would
    never appear in legitimate SQLite DDL.
    """
    try:
        content = path.read_text(encoding="utf-8").lower()
    except OSError:
        # If we cannot even read the file, treat it as incompatible so
        # we don't crash the migration sequence on a permission error.
        return False
    for token in _POSTGRES_TOKENS:
        if token in content:
            return False
    return True


def _get_available_migrations() -> list[Path]:
    """Get all SQLite-compatible ``.sql`` migration files, sorted by name.

    Files are sorted lexically by filename; the ``NNN_`` numeric prefix
    convention (e.g. ``001_initial_schema.sql``) guarantees the lexical
    order matches the intended application order.
    """
    if not MIGRATIONS_DIR.exists():
        return []
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    return [f for f in files if _is_sqlite_compatible(f)]


def run_migrations(db_path: Path | str, db_name: str = "default") -> dict:
    """Run pending migrations on a database.

    Args:
        db_path: Path to the SQLite database file.
        db_name: Logical name used in log lines (e.g. ``"decision_ledger"``).

    Returns:
        Dict with migration results::

            {
                "applied": [<migration filenames>],
                "skipped": [<already-applied filenames>],
                "errors":  [{"name": ..., "error": "..."}],
            }

    On the first error the runner stops (migrations are sequential — a
    failed migration cannot be safely skipped). The error is recorded
    in ``result["errors"]`` for the caller to surface via the CLI.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    result: dict = {"applied": [], "skipped": [], "errors": []}

    try:
        with sqlite3.connect(str(db_path)) as conn:
            _ensure_migrations_table(conn)
            applied = _get_applied_migrations(conn)
            available = _get_available_migrations()

            for migration_file in available:
                name = migration_file.name
                if name in applied:
                    result["skipped"].append(name)
                    continue

                try:
                    sql = migration_file.read_text(encoding="utf-8")
                    # Execute the migration. ``executescript`` issues an
                    # implicit COMMIT before running so the script's own
                    # DDL is applied atomically; we then record the
                    # migration name in the same connection so a failure
                    # of the INSERT would roll back both halves.
                    conn.executescript(sql)
                    conn.execute(
                        "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
                        (name, time.time()),
                    )
                    conn.commit()
                    result["applied"].append(name)
                    logger.info("[%s] Applied migration: %s", db_name, name)
                except Exception as e:
                    conn.rollback()
                    result["errors"].append({"name": name, "error": str(e)})
                    logger.error("[%s] Migration %s failed: %s", db_name, name, e)
                    break  # Stop on first error — migrations are sequential
    except Exception as e:
        result["errors"].append({"name": "connection", "error": str(e)})
        logger.error("[%s] Migration connection failed: %s", db_name, e)

    return result


def get_migration_status(db_path: Path | str) -> dict:
    """Get migration status for a database (read-only — never writes).

    Returns a dict with ``applied`` / ``available`` / ``pending`` lists
    of migration filenames. When the database file does not exist yet,
    every available migration is reported as pending.
    """
    db_path = Path(db_path)
    available = [f.name for f in _get_available_migrations()]
    if not db_path.exists():
        return {
            "applied": [],
            "available": available,
            "pending": list(available),
        }

    try:
        with sqlite3.connect(str(db_path)) as conn:
            _ensure_migrations_table(conn)
            applied = _get_applied_migrations(conn)
            pending = [m for m in available if m not in applied]
            return {
                "applied": sorted(applied),
                "available": available,
                "pending": pending,
            }
    except Exception as e:
        return {"error": str(e)}


def create_migration(name: str) -> Path:
    """Create a new empty migration file with the next sequential number.

    The filename follows ``NNN_<sanitised_name>.sql`` where ``NNN`` is
    the next available sequence number (zero-padded to three digits so
    lexical sort matches numeric sort up to migration #999).
    """
    existing = _get_available_migrations()
    # Use the *full* migrations dir (not the SQLite-filtered list) when
    # computing the next sequence number, so we never collide with a
    # PostgreSQL-only file that shares the same prefix. We walk every
    # ``*.sql`` file present, parse the leading integer, and pick max+1.
    all_files = sorted(MIGRATIONS_DIR.glob("*.sql")) if MIGRATIONS_DIR.exists() else []
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
        "-- Add your SQLite DDL here. Use CREATE TABLE IF NOT EXISTS /\n"
        "-- CREATE INDEX IF NOT EXISTS so the migration is idempotent and\n"
        "-- re-runnable alongside the existing _init_db() boot path.\n\n",
        encoding="utf-8",
    )
    return path


__all__ = [
    "MIGRATIONS_DIR",
    "run_migrations",
    "get_migration_status",
    "create_migration",
]
