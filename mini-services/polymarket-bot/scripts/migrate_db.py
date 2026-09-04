#!/usr/bin/env python3
"""Run database migrations on the active backend.

W21-3 — unified migration runner CLI.

Detects the active backend (PostgreSQL or SQLite) via the
``DatabaseManager`` singleton and applies pending migrations from
``core/db/migrations/``. The same ``.sql`` files run on either backend
(the migration manager translates ``SERIAL PRIMARY KEY`` →
``INTEGER PRIMARY KEY AUTOINCREMENT`` for SQLite).

Usage::

    python scripts/migrate_db.py

Set ``DATABASE_URL`` to target PostgreSQL; otherwise the script runs
against the local SQLite databases in ``BOT_DATA_DIR`` (default
``/app/data``).

Exit codes:
  * 0 — migrations applied (or already up-to-date) with no errors.
  * 1 — one or more migrations failed (see the printed ``errors`` list).
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd the CLI was launched from — mirrors
# the bootstrap pattern in ``scripts/migrate.py`` and ``tests/conftest.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database_manager import db_manager  # noqa: E402  (sys.path first)
from core.db.migration_manager import (  # noqa: E402
    get_migration_status,
    run_migrations,
)


async def main() -> int:
    """Initialize the backend, run migrations, print status, shut down."""
    await db_manager.initialize()
    print(f"Active backend: {db_manager.backend_name}")

    # The SQLite path is always available (even in PG mode — used as
    # the fallback file by TimescaleDBEngine).
    sqlite_path = db_manager.get_sqlite_path("market")
    print(f"SQLite path: {sqlite_path}")

    # Run migrations on the active backend. The second arg is the
    # backend name (resolved by ``run_migrations`` to the appropriate
    # execution path — SQLite via sqlite3, PG via asyncpg).
    result = run_migrations(str(sqlite_path), db_manager.backend_name)
    print("\nMigration results:")
    print(
        f"  Applied ({len(result.get('applied', []))}): "
        f"{result.get('applied', []) or '<none>'}"
    )
    print(
        f"  Skipped ({len(result.get('skipped', []))}): "
        f"{result.get('skipped', []) or '<none>'}"
    )
    if result.get("errors"):
        print(f"  Errors: {result['errors']}")
    if result.get("warnings"):
        print(
            f"  Warnings: {len(result['warnings'])} "
            "(CREATE INDEX skipped — column missing from prior schema)"
        )

    # Status check on the SQLite path (always available, even in PG mode
    # — for PG, this returns available + pending without querying the
    # tracker table; the ``applied`` count above is the source of truth).
    status = get_migration_status(
        str(sqlite_path), backend=db_manager.backend_name
    )
    print("\nMigration status:")
    print(f"  Applied: {len(status.get('applied', []))}")
    print(f"  Pending: {len(status.get('pending', []))}")
    for p in status.get("pending", [])[:5]:
        print(f"    - {p}")
    if len(status.get("pending", [])) > 5:
        print(f"    ... and {len(status['pending']) - 5} more")

    await db_manager.shutdown()
    return 0 if not result.get("errors") else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
