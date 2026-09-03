#!/usr/bin/env python3
"""Run database optimisation — creates indexes via re-invoking each
module's ``_init_db`` and then runs ``ANALYZE`` on every SQLite database
in the polymarket-bot ``data/`` directory to refresh the query planner's
statistics.

W11-9 — Database optimisation script.

Background
----------
The polymarket-bot service runs five independent SQLite databases (one
per persistence module — see ``core/decision_ledger.py``,
``core/execution_quality.py``, ``core/closed_positions.py``,
``core/observability.py``, ``core/alerting.py``). Each module's
``_init_db`` method uses ``CREATE INDEX IF NOT EXISTS`` so adding a new
index to the schema is automatically picked up on the next boot — but
existing deployments don't get the query-planner benefit until the
planner's per-table statistics are refreshed via ``ANALYZE``. SQLite's
query planner falls back to "full table scan" estimates when its
statistics are stale, even when a perfectly-good covering index exists.

What this script does
---------------------
1. **Re-runs each module's ``_init_db``** — this is a no-op when the
   indexes already exist (the ``IF NOT EXISTS`` clause makes the call
   safe to repeat), but it picks up any newly-added indexes from the
   W11-9 work without requiring a service restart.
2. **Runs ``ANALYZE``** on each ``*.db`` file in the ``data/``
   directory — this rebuilds the ``sqlite_stat1`` table the query
   planner consults when choosing between candidate indexes for a given
   query.
3. **Reports per-database index counts** — a quick sanity check that
   the indexes were actually created (and lets the operator spot
   truncated schema migrations at a glance).

Usage
-----
Run from the project root:

    python -m scripts.optimize_db

Or directly:

    python scripts/optimize_db.py [--data-dir /path/to/data] [--vacuum]

The optional ``--vacuum`` flag additionally runs ``VACUUM`` on each
database to reclaim free pages left by ``DELETE`` / ``INSERT OR REPLACE``
operations. VACUUM requires a writable filesystem and a brief write
lock — only run it during a maintenance window.

Exit codes
----------
* ``0`` — every database was successfully optimised.
* ``1`` — at least one database failed (the per-DB error is printed to
  stderr; the script continues with the remaining DBs so a single
  corrupt file doesn't block the rest).
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Iterable

# Make the polymarket-bot package root importable so the module imports
# below succeed when this script is run directly (the import path differs
# from the pytest-launched-via-conftest path).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

log = logging.getLogger("optimize_db")

# Default data directory — the in-container mount point. Override via
# the ``--data-dir`` CLI flag or the ``PMBOT_DATA_DIR`` env var.
_DEFAULT_DATA_DIR = Path(
    os.environ.get("PMBOT_DATA_DIR", "/app/data")
)


# ── Module re-init ────────────────────────────────────────────────────────────
def _reinit_modules(data_dir: Path | None = None) -> list[str]:
    """Re-invoke each persistence module's ``_init_db`` so the W11-9
    indexes get created against any pre-existing database file.

    If ``data_dir`` is supplied, the relevant ``*_DB_PATH`` env vars are
    set BEFORE the modules are imported so the module-level ``DB_PATH``
    constants pick up the caller-specified location. (Each module's
    ``DB_PATH`` is read once at import time — setting the env vars
    before import is the only way to redirect the singleton.)

    Returns the list of module names successfully re-initialised. Each
    module's ``_init_db`` is wrapped in ``try/except`` so a single
    module's failure (e.g. read-only sandbox) doesn't block the others.
    """
    reinitialised: list[str] = []

    # If the caller supplied a data_dir, redirect every module's DB_PATH
    # via env vars BEFORE the first import (the module-level ``DB_PATH``
    # constants are evaluated at import time).
    if data_dir is not None:
        data_dir.mkdir(parents=True, exist_ok=True)
        for env_var in (
            "DECISION_LEDGER_DB_PATH",
            "EXECUTION_QUALITY_DB_PATH",
            "CLOSED_POSITIONS_DB_PATH",
            "OBSERVABILITY_DB_PATH",
            "ALERT_DB_PATH",
            "AUDIT_DB_PATH",
        ):
            file_name = {
                "DECISION_LEDGER_DB_PATH": "decision_ledger.db",
                "EXECUTION_QUALITY_DB_PATH": "execution_quality.db",
                "CLOSED_POSITIONS_DB_PATH": "closed_positions.db",
                "OBSERVABILITY_DB_PATH": "observability.db",
                "ALERT_DB_PATH": "alerts.db",
                "AUDIT_DB_PATH": "audit_trail.db",
            }[env_var]
            os.environ[env_var] = str(data_dir / file_name)

    # ``decision_ledger`` — DecisionLedger singleton + per-instance
    # _init_db (the singleton's instance method is fine — it just re-runs
    # the IF NOT EXISTS statements).
    try:
        from core.decision_ledger import decision_ledger

        decision_ledger._init_db()
        reinitialised.append("decision_ledger")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] decision_ledger _init_db failed: %s", e)

    # ``execution_quality`` — module-level ``_init_db`` function (not a
    # method on a class).
    try:
        from core import execution_quality as _eq

        _eq._init_db()
        reinitialised.append("execution_quality")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] execution_quality _init_db failed: %s", e)

    # ``closed_positions`` — ClosedPositionsStore singleton.
    try:
        from core.closed_positions import closed_positions

        closed_positions._init_db()
        reinitialised.append("closed_positions")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] closed_positions _init_db failed: %s", e)

    # ``observability`` — Observability singleton.
    try:
        from core.observability import observability

        observability._init_db()
        reinitialised.append("observability")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] observability _init_db failed: %s", e)

    # ``alerting`` — AlertEngine singleton.
    try:
        from core.alerting import alert_engine

        alert_engine._init_db()
        reinitialised.append("alerting")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] alerting _init_db failed: %s", e)

    return reinitialised


# ── Per-DB ANALYZE ────────────────────────────────────────────────────────────
def _list_db_files(data_dir: Path) -> list[Path]:
    """Return every ``*.db`` file in ``data_dir`` (sorted for deterministic
    log output). Sidecar WAL / SHM files (``*-wal`` / ``*-shm``) are
    excluded — they're SQLite-internal journal files, not standalone
    databases.
    """
    if not data_dir.exists():
        log.warning("[optimize_db] data dir does not exist: %s", data_dir)
        return []
    return sorted(p for p in data_dir.glob("*.db") if p.is_file())


def _count_indexes(db_path: Path) -> int:
    """Return the number of indexes defined in ``db_path`` (across all
    tables). Used as a sanity-check report at the end of ``optimize_db``.
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] index-count query failed (%s): %s", db_path, e)
        return 0


def _count_tables(db_path: Path) -> int:
    """Return the number of tables in ``db_path`` (excluding SQLite's
    internal ``sqlite_*`` tables and ``sqlite_stat*`` ANALYZE outputs).
    """
    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
            row = cursor.fetchone()
            return int(row[0]) if row else 0
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] table-count query failed (%s): %s", db_path, e)
        return 0


def optimize_db(db_path: Path, vacuum: bool = False) -> bool:
    """Run ``ANALYZE`` on ``db_path`` (and optionally ``VACUUM``).

    Returns True on success, False on failure (the error is logged at
    ERROR level — the caller decides whether to continue with the
    remaining DBs).
    """
    start = time.time()
    try:
        with sqlite3.connect(db_path) as conn:
            # ``ANALYZE`` refreshes the sqlite_stat1 table — the planner
            # consults this to choose between candidate indexes.
            conn.execute("ANALYZE")
            if vacuum:
                # ``VACUUM`` reclaims free pages left by DELETEs. It can't
                # run inside a transaction (sqlite3.connect's context
                # manager commits at the end), but sqlite3-python opens an
                # implicit transaction only on DML — DDL like VACUUM runs
                # in autocommit mode automatically.
                conn.execute("VACUUM")
    except Exception as e:  # noqa: BLE001 — defensive
        log.error("[optimize_db] failed (%s): %s", db_path, e)
        return False

    duration = time.time() - start
    n_idx = _count_indexes(db_path)
    n_tbl = _count_tables(db_path)
    log.info(
        "[optimize_db] %s — %d tables, %d indexes, ANALYZE%s in %.2fs",
        db_path.name,
        n_tbl,
        n_idx,
        "+VACUUM" if vacuum else "",
        duration,
    )
    print(
        f"  ✓ {db_path.name}: {n_tbl} tables, {n_idx} indexes "
        f"(ANALYZE{'+VACUUM' if vacuum else ''} in {duration:.2f}s)"
    )
    return True


# ── Main ──────────────────────────────────────────────────────────────────────
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run database optimisation on every polymarket-bot SQLite "
            "database (creates indexes via module _init_db re-invocation "
            "and runs ANALYZE to refresh the query planner statistics)."
        )
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=_DEFAULT_DATA_DIR,
        help=(
            "Directory containing the *.db files to optimise "
            f"(default: {_DEFAULT_DATA_DIR})"
        ),
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "Additionally run VACUUM on each database to reclaim free "
            "pages (requires writable filesystem + brief write lock)."
        ),
    )
    parser.add_argument(
        "--skip-module-reinit",
        action="store_true",
        help=(
            "Skip re-running the persistence modules' _init_db methods "
            "(indexes are assumed to already be in place). Useful when "
            "running against a read-only DB snapshot."
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(levelname)s] %(message)s",
    )

    print(
        f"polymarket-bot database optimisation — data dir: {args.data_dir}"
    )

    # ── Step 1: re-run each module's _init_db so new indexes are created ──
    if args.skip_module_reinit:
        print("  • Skipping module _init_db re-invocation (--skip-module-reinit)")
    else:
        print("\nStep 1: re-running module _init_db methods (idempotent)...")
        reinitialised = _reinit_modules(data_dir=args.data_dir)
        if reinitialised:
            print(f"  ✓ Re-initialised {len(reinitialised)} module(s): "
                  f"{', '.join(reinitialised)}")
        else:
            print("  ! No modules were re-initialised — see logs above.")

    # ── Step 2: ANALYZE (+ optional VACUUM) on every *.db file ────────────
    print(f"\nStep 2: ANALYZE{' + VACUUM' if args.vacuum else ''} on *.db files...")
    db_files = _list_db_files(args.data_dir)
    if not db_files:
        print(f"  ! No *.db files found in {args.data_dir} — nothing to optimise.")
        return 0

    print(f"  Found {len(db_files)} database file(s).")
    successes = 0
    failures = 0
    for db_path in db_files:
        ok = optimize_db(db_path, vacuum=args.vacuum)
        if ok:
            successes += 1
        else:
            failures += 1

    print(f"\nDone. {successes}/{len(db_files)} optimised successfully"
          f"{f', {failures} failed' if failures else ''}.")

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
