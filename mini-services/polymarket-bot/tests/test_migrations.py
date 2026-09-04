"""
Unit tests for the W13-7 SQLite migration system.

W13-7 — Backend migration system.

Covers:

  (1) **Discovery** — ``_get_available_migrations`` finds the SQLite
      ``001_initial_schema.sql`` file but filters out the
      PostgreSQL-only ``001_initial_enterprise_schemas.sql`` sibling
      (verified by token-level + end-to-end checks).
  (2) **Application** — ``run_migrations`` against a fresh tmp_path
      creates every table + index declared in the initial migration,
      records the migration name in ``_migrations``, and reports
      ``applied``.
  (3) **Idempotency** — re-running ``run_migrations`` against the same
      DB skips the migration and reports ``skipped`` (no duplicate row
      in ``_migrations``, no schema changes).
  (4) **Status** — ``get_migration_status`` returns a correct
      ``applied`` / ``available`` / ``pending`` partition for both a
      fresh DB (no ``_migrations`` table) and a migrated DB.
  (5) **create_migration** — the helper creates a new file with the
      next sequential number, sanitises the name, and writes a
      header comment that the migration manager can later read back.
  (6) **Coexistence with _init_db** — when a DB has already been
      bootstrapped by a module's ``_init_db()`` method, the migration
      system still records the migration as applied (the ``IF NOT
      EXISTS`` clauses make the migration a no-op on the DDL side).
  (7) **Error handling** — a migration file with broken SQL surfaces
      in ``result["errors"]`` and stops the sequence (no half-applied
      state in ``_migrations``).
  (8) **Server startup wiring** — the lifespan function in
      ``api/server.py`` invokes the migration runner (smoke-import
      check that the wiring is syntactically valid and importable).

Every test builds its own ``tmp_path``-scoped DB / migrations
directory so there's no shared state and no perturbation of the
production ``/app/data`` paths.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)

from core.db import migration_manager  # noqa: E402
from core.db.migration_manager import (  # noqa: E402
    MIGRATIONS_DIR,
    _get_applied_migrations,
    _get_available_migrations,
    _is_sqlite_compatible,
    _ensure_migrations_table,
    create_migration,
    get_migration_status,
    run_migrations,
)


# ── Helpers ────────────────────────────────────────────────────────────────────


def _table_names(db_path: Path) -> set[str]:
    """Return the set of user-table names in ``db_path``.

    Excludes SQLite's internal tables (``sqlite_*``) and the migration
    tracker (``_migrations``) so the assertion count reflects only the
    schema declared by the migration.
    """
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' "
            "AND name != '_migrations'"
        )
        return {row[0] for row in cursor.fetchall()}


def _index_names(db_path: Path, table: str | None = None) -> set[str]:
    """Return the set of explicitly-declared index names."""
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' "
            "AND name NOT LIKE 'sqlite_%'"
            + (" AND tbl_name = ?" if table else ""),
            ([table] if table else []),
        )
        return {row[0] for row in cursor.fetchall()}


def _applied_rows(db_path: Path) -> list[tuple[str, float]]:
    """Return the ``(name, applied_at)`` rows in ``_migrations``."""
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name, applied_at FROM _migrations ORDER BY id"
        )
        return list(cursor.fetchall())


def _write_migration(directory: Path, name: str, sql: str) -> Path:
    """Write a synthetic migration file for an isolated test."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


# ── (1) Discovery ──────────────────────────────────────────────────────────────


def test_get_available_migrations_finds_sqlite_initial_schema() -> None:
    """The SQLite ``001_initial_schema.sql`` file is discovered."""
    available = [p.name for p in _get_available_migrations()]
    assert "001_initial_schema.sql" in available, (
        "SQLite initial migration not discovered — _get_available_migrations "
        f"returned {available}"
    )


def test_postgres_migration_is_filtered_out() -> None:
    """The PostgreSQL-only ``001_initial_enterprise_schemas.sql`` is skipped.

    The migrations directory is shared with the PostgreSQL / TimescaleDB
    enterprise runner (``core/db/migration_runner.py``). The PostgreSQL
    file uses ``TIMESTAMPTZ`` / ``JSONB`` / ``create_hypertable`` —
    syntax that ``sqlite3`` cannot parse. The SQLite runner must filter
    it out so a fresh deployment doesn't crash on the very first
    migration.
    """
    pg_path = MIGRATIONS_DIR / "001_initial_enterprise_schemas.sql"
    # The file is present in the repo.
    assert pg_path.exists(), "PostgreSQL migration fixture missing"
    # But it's flagged as incompatible with sqlite3.
    assert _is_sqlite_compatible(pg_path) is False
    # And the discovery list excludes it.
    available = [p.name for p in _get_available_migrations()]
    assert "001_initial_enterprise_schemas.sql" not in available


def test_is_sqlite_compatible_accepts_plain_ddl() -> None:
    """A typical SQLite DDL file is correctly identified as compatible."""
    tmp = Path("/tmp/pmbot_w13_7_compat_test.sql")
    try:
        tmp.write_text(
            "CREATE TABLE IF NOT EXISTS foo (id INTEGER PRIMARY KEY, name TEXT);\n"
            "CREATE INDEX IF NOT EXISTS idx_foo_name ON foo(name);\n",
            encoding="utf-8",
        )
        assert _is_sqlite_compatible(tmp) is True
    finally:
        tmp.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "token",
    [
        "timestamptz",
        "jsonb",
        "create_hypertable",
        "create_extension",
        "create schema",
        "materialized view",
        "uuid_generate_v4",
        "time_bucket",
    ],
)
def test_is_sqlite_compatible_rejects_pg_tokens(tmp_path: Path, token: str) -> None:
    """Each PostgreSQL-specific token alone disqualifies a file."""
    p = tmp_path / f"test_{token}.sql"
    p.write_text(f"-- comment\nCREATE TABLE foo (x {token});\n", encoding="utf-8")
    assert _is_sqlite_compatible(p) is False


# ── (2) Application ─────────────────────────────────────────────────────────────


def test_run_migrations_creates_tables_and_records(tmp_path: Path) -> None:
    """``run_migrations`` against a fresh DB applies the initial schema."""
    db_path = tmp_path / "fresh.db"
    result = run_migrations(db_path, "fresh")

    # The initial migration was applied (not skipped, not errored).
    assert "001_initial_schema.sql" in result["applied"]
    assert result["skipped"] == []
    assert result["errors"] == []

    # Every declared table is present (spot-check the canonical set).
    tables = _table_names(db_path)
    expected_tables = {
        "decision_events",
        "decision_rejections",
        "execution_quality",
        "metrics",
        "closed_positions",
        "alerts",
        "feature_flags",
        "audit_events",
        "order_transitions",
        "shadow_trades",
        "market_snapshots",
        "orderbook_ticks",
        "fundamental_news",
        "ml_feature_store",
    }
    missing = expected_tables - tables
    assert not missing, f"Missing tables after migration: {missing}"

    # The migration was recorded in the _migrations tracker.
    # W21-3 — both 001 and 002 are applied (002 is the unified schema
    # migration, SQLite-compatible after the SERIAL → AUTOINCREMENT
    # translation). 002 runs after 001 and tolerates CREATE INDEX
    # failures on columns missing from 001's schema (logged as
    # warnings, not errors).
    rows = _applied_rows(db_path)
    assert len(rows) == 2
    assert rows[0][0] == "001_initial_schema.sql"
    assert rows[1][0] == "002_unified_schema.sql"
    assert rows[0][1] > 0  # applied_at timestamp is set
    assert rows[1][1] > 0


def test_run_migrations_creates_indexes(tmp_path: Path) -> None:
    """The initial migration's indexes are created (spot-check)."""
    db_path = tmp_path / "fresh.db"
    run_migrations(db_path, "fresh")

    # A sample of indexes across modules.
    decision_indexes = _index_names(db_path, "decision_events")
    assert "idx_dec_id" in decision_indexes
    assert "idx_dec_token" in decision_indexes
    assert "idx_dec_stage_ts" in decision_indexes

    eq_indexes = _index_names(db_path, "execution_quality")
    assert "idx_eq_token" in eq_indexes
    assert "idx_eq_slippage" in eq_indexes

    metrics_indexes = _index_names(db_path, "metrics")
    assert "idx_metrics_cat_name_time" in metrics_indexes

    alerts_indexes = _index_names(db_path, "alerts")
    assert "idx_alerts_sev_ack_ts" in alerts_indexes


def test_run_migrations_creates_db_parent_dir(tmp_path: Path) -> None:
    """``run_migrations`` creates missing parent directories."""
    db_path = tmp_path / "nested" / "deep" / "fresh.db"
    assert not db_path.parent.exists()
    result = run_migrations(db_path, "fresh")
    assert db_path.parent.exists()
    assert "001_initial_schema.sql" in result["applied"]


# ── (3) Idempotency ─────────────────────────────────────────────────────────────


def test_run_migrations_is_idempotent(tmp_path: Path) -> None:
    """Re-running ``run_migrations`` skips already-applied migrations.

    W21-3 — the unified ``002_unified_schema.sql`` is now applied
    alongside ``001_initial_schema.sql`` on a fresh DB (both are
    SQLite-compatible after the SERIAL → AUTOINCREMENT translation).
    The migration sequence is still idempotent — re-running skips
    both migrations.
    """
    db_path = tmp_path / "fresh.db"

    first = run_migrations(db_path, "fresh")
    second = run_migrations(db_path, "fresh")

    # Both 001 and 002 are applied on the first run.
    assert first["applied"] == [
        "001_initial_schema.sql",
        "002_unified_schema.sql",
    ]
    assert first["skipped"] == []

    # Second run: nothing applied, both migrations skipped, no errors.
    assert second["applied"] == []
    assert second["skipped"] == [
        "001_initial_schema.sql",
        "002_unified_schema.sql",
    ]
    assert second["errors"] == []

    # No duplicate rows in _migrations.
    rows = _applied_rows(db_path)
    assert len(rows) == 2


def test_run_migrations_idempotent_alongside_init_db(tmp_path: Path) -> None:
    """Migration + a module's ``_init_db()`` coexist without errors.

    The bot's existing modules create their own tables on import via
    ``_init_db()``. The migration system must NOT break when a table
    already exists (every statement uses ``IF NOT EXISTS``).
    """
    db_path = tmp_path / "fresh.db"
    # Simulate the legacy init path: create a table the way
    # ``DecisionLedger._init_db`` does.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decision_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                decision_id TEXT NOT NULL,
                stage TEXT NOT NULL,
                token_id TEXT,
                strategy TEXT,
                pnl REAL DEFAULT 0.0,
                data_json TEXT
            )
            """
        )
        conn.commit()

    # The migration runs without complaint against the pre-bootstrapped DB.
    result = run_migrations(db_path, "fresh")
    assert "001_initial_schema.sql" in result["applied"]
    assert result["errors"] == []

    # And re-running records both migrations as skipped.
    second = run_migrations(db_path, "fresh")
    assert second["applied"] == []
    assert second["skipped"] == [
        "001_initial_schema.sql",
        "002_unified_schema.sql",
    ]


# ── (4) Status ─────────────────────────────────────────────────────────────────


def test_get_migration_status_fresh_db(tmp_path: Path) -> None:
    """Status for a non-existent DB reports everything as pending."""
    db_path = tmp_path / "does_not_exist.db"
    status = get_migration_status(db_path)
    assert status["applied"] == []
    assert "001_initial_schema.sql" in status["available"]
    assert "001_initial_schema.sql" in status["pending"]
    # PostgreSQL file is never reported as available (filtered out).
    assert "001_initial_enterprise_schemas.sql" not in status["available"]


def test_get_migration_status_after_apply(tmp_path: Path) -> None:
    """Status reflects a fully-migrated DB."""
    db_path = tmp_path / "fresh.db"
    run_migrations(db_path, "fresh")
    status = get_migration_status(db_path)
    assert "001_initial_schema.sql" in status["applied"]
    assert status["pending"] == []
    # Available still lists the migration (it exists on disk).
    assert "001_initial_schema.sql" in status["available"]


def test_get_migration_status_does_not_write(tmp_path: Path) -> None:
    """``get_migration_status`` is read-only — it must not create the DB.

    The status check should be safe to run against a path that doesn't
    exist yet (returns the full pending list) without side-effecting
    the filesystem (so a monitoring dashboard can poll without
    inadvertently provisioning DBs).
    """
    db_path = tmp_path / "never_created.db"
    get_migration_status(db_path)
    assert not db_path.exists(), (
        "get_migration_status created the DB file — must be read-only"
    )


# ── (5) create_migration ───────────────────────────────────────────────────────


def test_create_migration_picks_next_number(monkeypatch, tmp_path: Path) -> None:
    """``create_migration`` increments the sequence number correctly."""
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    # Seed with an existing migration file.
    (fake_dir / "001_initial_schema.sql").write_text(
        "-- existing\n", encoding="utf-8"
    )
    monkeypatch.setattr(migration_manager, "MIGRATIONS_DIR", fake_dir)

    path = create_migration("add_users_table")
    assert path.name == "002_add_users_table.sql"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "-- Migration: add_users_table" in content
    assert "-- Created:" in content


def test_create_migration_sanitises_name(monkeypatch, tmp_path: Path) -> None:
    """Unsafe characters in the name are replaced with underscores."""
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    monkeypatch.setattr(migration_manager, "MIGRATIONS_DIR", fake_dir)

    path = create_migration("add users-table.v2")
    # Spaces, hyphens, and dots all become underscores.
    assert path.name == "001_add_users_table_v2.sql"


def test_create_migration_skips_pg_files_for_numbering(
    monkeypatch, tmp_path: Path
) -> None:
    """Sequence numbering counts PostgreSQL files too.

    A PostgreSQL-only file (e.g. ``001_initial_enterprise_schemas.sql``)
    still occupies its sequence slot so the next SQLite migration is
    numbered correctly without colliding.
    """
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    (fake_dir / "001_initial_enterprise_schemas.sql").write_text(
        "-- postgres\nCREATE SCHEMA raw;\n", encoding="utf-8"
    )
    monkeypatch.setattr(migration_manager, "MIGRATIONS_DIR", fake_dir)

    path = create_migration("sqlite_only")
    assert path.name == "002_sqlite_only.sql"


# ── (6) Multi-migration sequencing ─────────────────────────────────────────────


def test_run_migrations_applies_in_order(monkeypatch, tmp_path: Path) -> None:
    """Multiple migrations are applied in filename order."""
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    _write_migration(
        fake_dir,
        "001_first.sql",
        "CREATE TABLE IF NOT EXISTS t1 (id INTEGER PRIMARY KEY);\n",
    )
    _write_migration(
        fake_dir,
        "002_second.sql",
        "CREATE TABLE IF NOT EXISTS t2 (id INTEGER PRIMARY KEY);\n",
    )
    monkeypatch.setattr(migration_manager, "MIGRATIONS_DIR", fake_dir)

    db_path = tmp_path / "fresh.db"
    result = run_migrations(db_path, "fresh")
    assert result["applied"] == ["001_first.sql", "002_second.sql"]
    assert result["errors"] == []
    assert _table_names(db_path) == {"t1", "t2"}


def test_run_migrations_stops_on_first_error(monkeypatch, tmp_path: Path) -> None:
    """A broken migration halts the sequence — no half-applied state."""
    fake_dir = tmp_path / "migrations"
    fake_dir.mkdir()
    _write_migration(
        fake_dir,
        "001_ok.sql",
        "CREATE TABLE IF NOT EXISTS good (id INTEGER PRIMARY KEY);\n",
    )
    _write_migration(
        fake_dir,
        "002_broken.sql",
        "CREATE TABLE broken (this is not valid SQL);\n",
    )
    _write_migration(
        fake_dir,
        "003_should_not_run.sql",
        "CREATE TABLE IF NOT EXISTS skipped (id INTEGER PRIMARY KEY);\n",
    )
    monkeypatch.setattr(migration_manager, "MIGRATIONS_DIR", fake_dir)

    db_path = tmp_path / "fresh.db"
    result = run_migrations(db_path, "fresh")

    # 001 applied; 002 errored; 003 never attempted (sequence halts).
    assert result["applied"] == ["001_ok.sql"]
    assert len(result["errors"]) == 1
    assert result["errors"][0]["name"] == "002_broken.sql"
    # The broken migration is NOT recorded as applied.
    rows = _applied_rows(db_path)
    assert [r[0] for r in rows] == ["001_ok.sql"]
    # The post-error table does not exist.
    assert "skipped" not in _table_names(db_path)


# ── (7) _ensure_migrations_table / _get_applied_migrations ────────────────────


def test_get_applied_migrations_handles_missing_table(tmp_path: Path) -> None:
    """``_get_applied_migrations`` returns ``set()`` when the table is absent."""
    db_path = tmp_path / "fresh.db"
    # Create the DB file but no _migrations table.
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("CREATE TABLE other (x INTEGER)")
        conn.commit()
    with sqlite3.connect(str(db_path)) as conn:
        applied = _get_applied_migrations(conn)
    assert applied == set()


def test_ensure_migrations_table_is_idempotent(tmp_path: Path) -> None:
    """Calling ``_ensure_migrations_table`` twice is a no-op."""
    db_path = tmp_path / "fresh.db"
    with sqlite3.connect(str(db_path)) as conn:
        _ensure_migrations_table(conn)
        _ensure_migrations_table(conn)
        cursor = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='_migrations'"
        )
        assert cursor.fetchone()[0] == 1


# ── (8) Server startup wiring ─────────────────────────────────────────────────


def test_lifespan_invokes_migration_runner() -> None:
    """The lifespan function in ``api/server.py`` calls ``run_migrations``.

    Verifies the W13-7 wiring is present without spinning up the full
    FastAPI app (which would require a working bot env). Inspects the
    source of the lifespan function for the migration runner import
    and invocation.
    """
    import inspect

    from api import server as server_mod

    lifespan_src = inspect.getsource(server_mod.lifespan)
    assert "from core.db.migration_manager import run_migrations" in lifespan_src, (
        "lifespan must import run_migrations from core.db.migration_manager"
    )
    assert "BOT_DATA_DIR" in lifespan_src, (
        "lifespan must consult the BOT_DATA_DIR env var for the data dir"
    )
    # The migration runner is invoked for the canonical SQLite DB names.
    assert "decision_ledger" in lifespan_src
    assert "observability" in lifespan_src
    assert "alerts" in lifespan_src


# ── (9) CLI smoke test ─────────────────────────────────────────────────────────


def test_migrate_cli_status_with_no_data_dir(tmp_path: Path, capsys) -> None:
    """``scripts/migrate.py status`` against an empty data dir exits 0."""
    # Use a separate sys.modules reload so the env var takes effect.
    monkey_env = {"BOT_DATA_DIR": str(tmp_path / "no_such_dir")}
    saved_env = dict(os.environ)
    try:
        os.environ.update(monkey_env)
        # Import the CLI module fresh so it picks up the env var.
        if "scripts.migrate" in sys.modules:
            del sys.modules["scripts.migrate"]
        # Ensure the scripts package root is importable.
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
        from scripts.migrate import main  # noqa: WPS433 — intentional late import

        rc = main(["status"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "does not exist" in captured.out or "No .db files" in captured.out
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        # Drop the cached module so subsequent tests don't see the
        # patched env var.
        sys.modules.pop("scripts.migrate", None)


def test_migrate_cli_run_creates_schema(tmp_path: Path, capsys) -> None:
    """``scripts/migrate.py run`` applies the migration to a fresh DB."""
    db_path = tmp_path / "decision_ledger.db"
    db_path.touch()  # Create empty DB file so the CLI picks it up.
    monkey_env = {"BOT_DATA_DIR": str(tmp_path)}
    saved_env = dict(os.environ)
    try:
        os.environ.update(monkey_env)
        if "scripts.migrate" in sys.modules:
            del sys.modules["scripts.migrate"]
        if str(_PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(_PROJECT_ROOT))
        from scripts.migrate import main  # noqa: WPS433

        rc = main(["run"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "decision_ledger.db" in captured.out
        # W21-3 — both 001 and 002 are applied (002 is the unified schema
        # migration, SQLite-compatible after the SERIAL → AUTOINCREMENT
        # translation). The CLI prints ``Applied (2):`` reflecting both.
        assert "Applied (2)" in captured.out
        # Schema is actually in place.
        assert "decision_events" in _table_names(db_path)
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        sys.modules.pop("scripts.migrate", None)
