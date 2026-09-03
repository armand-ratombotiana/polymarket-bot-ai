#!/usr/bin/env python3
"""Run database migrations against the polymarket-bot SQLite databases.

W13-7 — Backend migration system CLI.

Subcommands
-----------

``status``
    Print the migration status of every ``*.db`` file in the data
    directory: how many migrations have been applied, how many are
    pending, and the list of pending filenames.

``run``
    Apply pending migrations to every ``*.db`` file in the data
    directory. Migrations are applied sequentially per database; the
    runner stops on the first error (a failed migration cannot be
    safely skipped). Already-applied migrations are skipped.

``create <name>``
    Create a new empty migration file with the next sequential number.
    The filename follows ``NNN_<sanitised_name>.sql``.

Usage
-----

Run from the project root::

    python scripts/migrate.py status
    python scripts/migrate.py run
    python scripts/migrate.py create add_users_table

The data directory defaults to ``./data``; override via the
``BOT_DATA_DIR`` environment variable (same variable the bot's
``lifespan`` startup consults).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd the CLI was launched from — mirrors
# the bootstrap pattern in ``tests/conftest.py``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.db.migration_manager import (  # noqa: E402  (sys.path must be set first)
    create_migration,
    get_migration_status,
    run_migrations,
)


def _data_dir() -> Path:
    """Return the data directory, defaulting to ``./data``.

    Honours the ``BOT_DATA_DIR`` env var so a deployment-wide invocation
    can target the same data dir the bot itself uses.
    """
    return Path(os.environ.get("BOT_DATA_DIR", "data"))


def _cmd_status() -> int:
    """Print migration status for every ``*.db`` file in the data dir."""
    data_dir = _data_dir()
    if not data_dir.exists():
        print(f"Data directory does not exist: {data_dir}")
        return 0
    db_files = sorted(data_dir.glob("*.db"))
    if not db_files:
        print(f"No .db files found in {data_dir}")
        return 0
    for db_file in db_files:
        status = get_migration_status(db_file)
        if "error" in status:
            print(f"\n{db_file.name}: ERROR — {status['error']}")
            continue
        applied = status.get("applied", [])
        pending = status.get("pending", [])
        print(f"\n{db_file.name}:")
        print(f"  Applied: {len(applied)}")
        print(f"  Pending: {len(pending)}")
        for p in pending:
            print(f"    - {p}")
    return 0


def _cmd_run() -> int:
    """Apply pending migrations to every ``*.db`` file in the data dir."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    db_files = sorted(data_dir.glob("*.db"))
    if not db_files:
        print(f"No .db files found in {data_dir} — nothing to migrate.")
        return 0
    exit_code = 0
    for db_file in db_files:
        result = run_migrations(db_file, db_file.stem)
        print(f"\n{db_file.name}:")
        print(f"  Applied ({len(result['applied'])}): {result['applied'] or '<none>'}")
        print(f"  Skipped ({len(result['skipped'])}): {result['skipped'] or '<none>'}")
        if result["errors"]:
            print(f"  Errors: {result['errors']}")
            exit_code = 1
    return exit_code


def _cmd_create(name: str) -> int:
    """Create a new migration file with the next sequential number."""
    path = create_migration(name)
    print(f"Created migration: {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). The
            explicit ``argv`` parameter exists so tests can drive the
            CLI without monkeypatching ``sys.argv``.
    """
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__)
        print("Usage: python scripts/migrate.py [status|run|create <name>]")
        return 1

    cmd = args[0]
    if cmd == "status":
        return _cmd_status()
    if cmd == "run":
        return _cmd_run()
    if cmd == "create":
        if len(args) < 2:
            print("Usage: python scripts/migrate.py create <name>")
            return 1
        return _cmd_create(args[1])
    print(f"Unknown command: {cmd}")
    print("Usage: python scripts/migrate.py [status|run|create <name>]")
    return 1


if __name__ == "__main__":
    sys.exit(main())
