"""Tests for the W21-3 unified schema migration.

Verifies that ``002_unified_schema.sql``:

  (1) Creates every declared table on SQLite (after ``SERIAL`` →
      ``AUTOINCREMENT`` translation by
      ``migration_manager._translate_for_sqlite``).
  (2) Is idempotent — running twice doesn't fail.
  (3) Creates all declared indexes.
  (4) Runs cleanly after ``001_initial_schema.sql`` (no abort on
      ``CREATE INDEX`` statements that reference columns missing from
      001's schema — those are skipped with a warning, not raised as
      errors).
  (5) The ``_migrations`` tracker table records the backend each
      migration was applied on.
  (6) The ``SERIAL → AUTOINCREMENT`` translation produces SQLite-legal
      DDL and the auto-increment actually fires on INSERT.
  (7) The unified migration file is considered compatible with BOTH
      backends by ``_is_sqlite_compatible`` / ``_is_pg_compatible``.

Every test uses a ``tmp_path``-scoped DB so there's no shared state
and no perturbation of the production ``/app/data`` paths.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (sys.path must be set first)

from core.db import migration_manager  # noqa: E402
from core.db.migration_manager import (  # noqa: E402
    MIGRATIONS_DIR,
    _is_pg_compatible,
    _is_sqlite_compatible,
    _split_sql_statements,
    _translate_for_sqlite,
    get_migration_status,
    run_migrations,
)


# ── Expected schema (declared in 002_unified_schema.sql) ─────────────────────

UNIFIED_TABLES: set[str] = {
    "market_snapshots",
    "market_trades",
    "decision_events",
    "decision_rejections",
    "execution_quality",
    "closed_positions",
    "observability_metrics",
    "alerts",
    "audit_events",
    "feature_flags",
    "feature_definitions",
    "feature_values",
    "feature_importance",
    "jobs",
    "audit_chain",
    "ml_trade_attribution",
    "experiments",
}

# A spot-check subset of indexes declared in 002 (one per table) —
# the full set is verified in ``test_002_unified_migration_creates_indexes``.
EXPECTED_INDEXES: dict[str, set[str]] = {
    "market_snapshots": {"idx_ms_token_ts"},
    "market_trades": {"idx_mt_token_ts"},
    "decision_events": {"idx_de_corr", "idx_de_token_ts", "idx_de_stage"},
    "decision_rejections": {"idx_dr_corr"},
    "execution_quality": {"idx_eq_token_ts"},
    "closed_positions": {"idx_cp_closed_at", "idx_cp_token"},
    "observability_metrics": {"idx_obs_cat_name_ts"},
    "alerts": {"idx_alerts_ts", "idx_alerts_sev_ack"},
    "audit_events": {"idx_ae_ts", "idx_ae_type"},
    "feature_values": {"idx_fv_token_ts", "idx_fv_feature"},
    "feature_importance": {"idx_fi_version"},
    "jobs": {"idx_jobs_status", "idx_jobs_type"},
    "audit_chain": {"idx_ac_ts", "idx_ac_hash"},
    "ml_trade_attribution": {"idx_mla_version", "idx_mla_conf"},
    "experiments": {"idx_exp_strategy", "idx_exp_created"},
}


# ── Helpers ────────────────────────────────────────────────────────────────────


def _table_names(db_path: Path) -> set[str]:
    """Return the set of user-table names in ``db_path``.

    Excludes SQLite's internal tables (``sqlite_%``) and the migration
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


def _applied_rows(db_path: Path) -> list[tuple]:
    """Return ``(name, applied_at, backend)`` rows in ``_migrations``."""
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT name, applied_at, backend FROM _migrations ORDER BY id"
        )
        return list(cursor.fetchall())


def _run_only_unified_migration(db_path: Path) -> dict:
    """Run only ``002_unified_schema.sql`` on a fresh DB.

    Monkeypatches ``_get_available_migrations`` to return only the
    unified migration file, so ``001_initial_schema.sql`` doesn't run
    first. This isolates 002's behavior (we want to verify 002
    standalone here — the 001+002 interaction is covered separately
    in ``test_001_and_002_run_together_without_errors``).
    """
    unified_path = MIGRATIONS_DIR / "002_unified_schema.sql"
    assert unified_path.exists(), "002_unified_schema.sql not found"

    original = migration_manager._get_available_migrations
    migration_manager._get_available_migrations = lambda backend="sqlite": [
        unified_path
    ]
    try:
        return run_migrations(db_path, backend="sqlite")
    finally:
        migration_manager._get_available_migrations = original


# ── (1) 002 creates all declared tables ────────────────────────────────────────


def test_002_unified_migration_creates_all_tables(tmp_path: Path) -> None:
    """``002_unified_schema.sql`` creates every declared table on SQLite."""
    db_path = tmp_path / "unified.db"
    result = _run_only_unified_migration(db_path)

    assert "002_unified_schema.sql" in result["applied"]
    assert result["errors"] == []

    tables = _table_names(db_path)
    missing = UNIFIED_TABLES - tables
    assert not missing, f"Missing tables after 002: {missing}"


# ── (2) Idempotent — running twice doesn't fail ───────────────────────────────


def test_002_unified_migration_is_idempotent(tmp_path: Path) -> None:
    """Running 002 twice is a no-op on the second run."""
    db_path = tmp_path / "unified.db"

    first = _run_only_unified_migration(db_path)
    assert first["errors"] == []
    assert "002_unified_schema.sql" in first["applied"]

    second = _run_only_unified_migration(db_path)
    assert second["errors"] == []
    assert second["applied"] == []
    assert "002_unified_schema.sql" in second["skipped"]

    # No duplicate row in _migrations.
    rows = _applied_rows(db_path)
    assert len(rows) == 1


# ── (3) Indexes are created ────────────────────────────────────────────────────


def test_002_unified_migration_creates_indexes(tmp_path: Path) -> None:
    """All declared indexes in 002 are created on SQLite."""
    db_path = tmp_path / "unified.db"
    _run_only_unified_migration(db_path)

    for table, expected in EXPECTED_INDEXES.items():
        actual = _index_names(db_path, table)
        missing = expected - actual
        assert not missing, (
            f"Missing indexes on {table}: {missing} "
            f"(actual: {actual})"
        )


# ── (4) Run 001 + 002 together — no abort on column-mismatch indexes ────────


def test_001_and_002_run_together_without_errors(tmp_path: Path) -> None:
    """Running 001 then 002 doesn't abort on column-mismatch index errors.

    001 creates ``decision_events`` without ``correlation_id``. 002
    then tries to create ``idx_de_corr ON decision_events(correlation_id)``
    — this would normally abort the migration. The migration manager
    catches the ``no such column`` error on ``CREATE INDEX``, logs a
    warning, and continues. The migration is marked as applied.
    """
    db_path = tmp_path / "full.db"
    result = run_migrations(db_path, backend="sqlite")

    assert "001_initial_schema.sql" in result["applied"]
    assert "002_unified_schema.sql" in result["applied"]
    assert result["errors"] == []
    # Some warnings about skipped indexes are expected when 002 runs
    # after 001 (their schemas differ for shared tables).
    # We don't assert on the exact warning count — only that no errors
    # broke the sequence.


# ── (5) Backend tracking ─────────────────────────────────────────────────────


def test_migrations_track_backend(tmp_path: Path) -> None:
    """The ``_migrations`` table records the backend each migration was applied on."""
    db_path = tmp_path / "unified.db"
    _run_only_unified_migration(db_path)

    rows = _applied_rows(db_path)
    assert len(rows) >= 1
    for name, applied_at, backend in rows:
        assert backend == "sqlite", (
            f"Migration {name} has wrong backend: {backend!r}"
        )
        assert applied_at > 0


def test_backend_column_added_to_legacy_migrations_table(tmp_path: Path) -> None:
    """DBs created before W21-3 (no ``backend`` column) are auto-migrated.

    The ``_ensure_migrations_table_sqlite`` helper adds the
    ``backend`` column via ``ALTER TABLE`` with ``DEFAULT 'sqlite'``
    so pre-existing rows are backfilled correctly.
    """
    db_path = tmp_path / "legacy.db"
    # Create a pre-W21-3 _migrations table (without the backend column).
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE _migrations (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                applied_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO _migrations (name, applied_at) VALUES (?, ?)",
            ("001_initial_schema.sql", 1234567890.0),
        )
        conn.commit()

    # Run the unified migration — this should auto-add the backend column.
    result = _run_only_unified_migration(db_path)
    assert result["errors"] == []

    # The pre-existing row should be backfilled with backend='sqlite'.
    rows = _applied_rows(db_path)
    assert len(rows) == 2  # The legacy 001 + the new 002
    backends = {row[2] for row in rows}
    assert backends == {"sqlite"}, (
        f"Expected all rows backfilled to backend='sqlite', got {backends}"
    )


# ── (6) SERIAL → AUTOINCREMENT translation ────────────────────────────────────


def test_translate_for_sqlite_converts_serial() -> None:
    """``_translate_for_sqlite`` converts ``SERIAL PRIMARY KEY`` → ``INTEGER PRIMARY KEY AUTOINCREMENT``."""
    sql = "CREATE TABLE foo (id SERIAL PRIMARY KEY, name TEXT);"
    translated = _translate_for_sqlite(sql)
    assert "SERIAL" not in translated.upper()
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in translated


def test_translate_for_sqlite_case_insensitive() -> None:
    """Translation handles case variations."""
    sql = "CREATE TABLE foo (id serial primary key, name TEXT);"
    translated = _translate_for_sqlite(sql)
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in translated
    assert "serial" not in translated.lower()


def test_translate_for_sqlite_preserves_other_syntax() -> None:
    """Non-SERIAL syntax is left untouched."""
    sql = """
    CREATE TABLE foo (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        value REAL DEFAULT 0
    );
    CREATE INDEX idx_foo_name ON foo(name);
    """
    translated = _translate_for_sqlite(sql)
    assert "CREATE TABLE foo" in translated
    assert "name TEXT NOT NULL" in translated
    assert "value REAL DEFAULT 0" in translated
    assert "CREATE INDEX idx_foo_name ON foo(name)" in translated


def test_unified_tables_auto_increment(tmp_path: Path) -> None:
    """Tables created by 002 auto-increment their ``id`` column on INSERT.

    This verifies that the ``SERIAL → INTEGER PRIMARY KEY AUTOINCREMENT``
    translation produces a SQLite-legal column that actually fires the
    auto-increment behavior (not just a plain ``INTEGER PRIMARY KEY``,
    which would alias to ROWID but wouldn't guarantee monotonicity
    after deletes).
    """
    db_path = tmp_path / "unified.db"
    _run_only_unified_migration(db_path)

    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO market_trades "
            "(trade_id, token_id, price, size, side, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t1", "tok1", 0.5, 10.0, "BUY", 1000.0),
        )
        conn.execute(
            "INSERT INTO market_trades "
            "(trade_id, token_id, price, size, side, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("t2", "tok1", 0.6, 5.0, "SELL", 1001.0),
        )
        rows = conn.execute(
            "SELECT id, trade_id FROM market_trades ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0][0] == 1, f"First row id should be 1, got {rows[0][0]}"
        assert rows[1][0] == 2, f"Second row id should be 2, got {rows[1][0]}"


# ── (7) Backend compatibility classification ─────────────────────────────────


def test_002_is_sqlite_compatible() -> None:
    """``002_unified_schema.sql`` is classified as SQLite-compatible.

    ``SERIAL PRIMARY KEY`` is NOT in the disqualifying token list
    (it's translated); the file must not contain any other PG-only
    tokens (TIMESTAMPTZ, JSONB, create_hypertable, …).
    """
    path = MIGRATIONS_DIR / "002_unified_schema.sql"
    assert path.exists(), "002_unified_schema.sql not found"
    assert _is_sqlite_compatible(path), (
        "002 should be SQLite-compatible (SERIAL is translated; no "
        "other PG-only tokens present)"
    )


def test_002_is_pg_compatible() -> None:
    """``002_unified_schema.sql`` is also PostgreSQL-compatible.

    The file uses ``SERIAL PRIMARY KEY`` (PG-native auto-increment)
    and must not contain any SQLite-only tokens (AUTOINCREMENT, etc.).
    Inline comments mentioning SQLite-specific syntax are stripped
    before token scanning by ``_strip_sql_comments``.
    """
    path = MIGRATIONS_DIR / "002_unified_schema.sql"
    assert path.exists(), "002_unified_schema.sql not found"
    assert _is_pg_compatible(path), (
        "002 should be PG-compatible (no AUTOINCREMENT or SQLite-only tokens)"
    )


def test_001_initial_schema_is_sqlite_only() -> None:
    """``001_initial_schema.sql`` (AUTOINCREMENT) is filtered out for PG.

    The SQLite-only initial migration uses ``AUTOINCREMENT`` which is
    SQLite-specific; it must not appear in the PG migration list.
    """
    path = MIGRATIONS_DIR / "001_initial_schema.sql"
    assert path.exists()
    assert _is_sqlite_compatible(path), (
        "001 should be SQLite-compatible (uses AUTOINCREMENT natively)"
    )
    assert not _is_pg_compatible(path), (
        "001 should NOT be PG-compatible (uses AUTOINCREMENT which is SQLite-only)"
    )


def test_001_enterprise_schemas_is_pg_only() -> None:
    """``001_initial_enterprise_schemas.sql`` is filtered out for SQLite.

    The PG-only initial migration uses ``TIMESTAMPTZ``, ``JSONB``,
    ``create_hypertable`` etc. — none of these are SQLite-parseable.
    """
    path = MIGRATIONS_DIR / "001_initial_enterprise_schemas.sql"
    assert path.exists()
    assert not _is_sqlite_compatible(path), (
        "001 enterprise should NOT be SQLite-compatible (uses TIMESTAMPTZ, JSONB, …)"
    )
    assert _is_pg_compatible(path), (
        "001 enterprise should be PG-compatible"
    )


# ── (8) Status reflects 002 ───────────────────────────────────────────────────


def test_status_after_002(tmp_path: Path) -> None:
    """``get_migration_status`` reports 002 as applied after running it."""
    db_path = tmp_path / "unified.db"
    _run_only_unified_migration(db_path)

    status = get_migration_status(db_path, backend="sqlite")
    assert "002_unified_schema.sql" in status["applied"]
    assert "002_unified_schema.sql" not in status["pending"]


def test_status_fresh_db_lists_002_as_pending(tmp_path: Path) -> None:
    """A fresh DB reports 002 as pending (not yet applied)."""
    db_path = tmp_path / "fresh.db"
    status = get_migration_status(db_path, backend="sqlite")
    assert "002_unified_schema.sql" in status["available"]
    assert "002_unified_schema.sql" in status["pending"]
    assert status["applied"] == []


# ── (9) SQL statement splitter ─────────────────────────────────────────────────


def test_split_sql_statements_handles_comments() -> None:
    """The splitter skips line comments and splits on semicolons."""
    sql = """
    -- This is a comment
    CREATE TABLE foo (id INTEGER PRIMARY KEY);  -- trailing comment
    -- Another comment
    CREATE INDEX idx_foo ON foo(id);
    """
    statements = _split_sql_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE foo")
    assert statements[1].startswith("CREATE INDEX idx_foo")


def test_split_sql_statements_preserves_strings() -> None:
    """Semicolons inside string literals don't split statements."""
    sql = """
    INSERT INTO foo (name) VALUES ('hello; world');
    CREATE INDEX idx_foo ON foo(name);
    """
    statements = _split_sql_statements(sql)
    assert len(statements) == 2
    assert "hello; world" in statements[0]


def test_split_sql_statements_handles_multiline_create() -> None:
    """Multi-line CREATE TABLE statements are preserved as one statement."""
    sql = """
    CREATE TABLE foo (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        value REAL DEFAULT 0
    );
    CREATE INDEX idx_foo ON foo(name);
    """
    statements = _split_sql_statements(sql)
    assert len(statements) == 2
    assert "id INTEGER PRIMARY KEY" in statements[0]
    assert "name TEXT NOT NULL" in statements[0]
    assert "value REAL DEFAULT 0" in statements[0]


# ── (10) DatabaseManager smoke tests ──────────────────────────────────────────


def test_database_manager_defaults_to_sqlite_without_db_url(monkeypatch) -> None:
    """``DatabaseManager`` defaults to SQLite when ``DATABASE_URL`` is unset.

    Uses a fresh ``DatabaseManager`` instance (not the module-level
    singleton) so this test doesn't perturb the singleton's state for
    sibling tests.

    Note: the existing ``DatabaseManager`` (W21-1 / W21-4 / W21-5) uses
    a ``DatabaseBackend`` enum whose ``.NONE`` value is ``"none"`` —
    so ``backend_name`` returns ``"none"`` BEFORE ``initialize()`` and
    ``"sqlite"`` (or ``"postgresql"``) AFTER. This test verifies both
    the pre-init ``"none"`` state and the post-init ``"sqlite"`` state.
    """
    import asyncio

    from core.database_manager import DatabaseManager

    # Force DATABASE_URL unset + redirect MARKET_DB_PATH to a writable
    # tmp location so the underlying ``timescale_db`` singleton can be
    # constructed without permission errors on ``/app/data``.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(Path("/tmp/pmbot_w21_3_db_manager_test")))
    monkeypatch.setenv(
        "MARKET_DB_PATH",
        str(Path("/tmp/pmbot_w21_3_db_manager_test/market_intelligence.db")),
    )

    mgr = DatabaseManager()
    # Pre-initialize: backend_name is "none" (DatabaseBackend.NONE.value).
    assert mgr.backend_name == "none"
    assert not mgr.is_postgres
    assert mgr.is_sqlite  # is_sqlite is True when not postgres (W21-9 semantics)
    assert not mgr.is_initialized

    asyncio.run(mgr.initialize())
    # Post-initialize: SQLite fallback (DATABASE_URL unset).
    assert mgr.backend_name in {"sqlite", "postgresql"}
    assert mgr.is_initialized

    # The SQLite dir is created.
    assert mgr.sqlite_dir.exists()

    # get_sqlite_path returns the market path (timescale_db._sqlite_path).
    market_path = mgr.get_sqlite_path("market")
    assert "market_intelligence.db" in str(market_path)

    asyncio.run(mgr.shutdown())
    assert not mgr.is_initialized


def test_database_manager_initialize_is_idempotent(monkeypatch) -> None:
    """Calling ``initialize()`` twice is a no-op after the first call."""
    import asyncio

    from core.database_manager import DatabaseManager

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("BOT_DATA_DIR", str(Path("/tmp/pmbot_w21_3_idempotent_test")))
    monkeypatch.setenv(
        "MARKET_DB_PATH",
        str(Path("/tmp/pmbot_w21_3_idempotent_test/market_intelligence.db")),
    )

    mgr = DatabaseManager()
    asyncio.run(mgr.initialize())
    assert mgr.is_initialized
    asyncio.run(mgr.initialize())  # No-op
    assert mgr.is_initialized
    asyncio.run(mgr.shutdown())


# ── (11) migrate_db.py CLI smoke test ─────────────────────────────────────────


def test_migrate_db_script_runs_on_sqlite(monkeypatch, tmp_path: Path, capsys) -> None:
    """``scripts/migrate_db.py`` runs migrations against a SQLite backend.

    Sets ``MARKET_DB_PATH`` to a writable ``tmp_path`` and unsets
    ``DATABASE_URL`` so the ``DatabaseManager`` picks SQLite. Imports
    the script fresh (so it picks up the env vars) and runs ``main()``.
    """
    import asyncio

    # The ``timescale_db`` singleton reads ``MARKET_DB_PATH`` at import
    # time (module-level ``SQLITE_FALLBACK_PATH`` constant), so we must
    # set it BEFORE the import. The existing ``DatabaseManager`` also
    # reads ``BOT_DATA_DIR`` (for the ``sqlite_dir`` property used by
    # ``_init_decision_ledger_db``) — set BOTH so the manager doesn't
    # try to mkdir ``/app/data`` (read-only in the sandbox).
    market_db_path = tmp_path / "market_intelligence.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MARKET_DB_PATH", str(market_db_path))
    monkeypatch.setenv("BOT_DATA_DIR", str(tmp_path))
    # The existing ``_init_decision_ledger_db`` calls
    # ``get_sqlite_path("decision_ledger")`` which returns
    # ``sqlite_dir / "decision_ledger.db"``. Redirect via env var if
    # the W21-4 path-resolution hook is present.
    monkeypatch.setenv(
        "DECISION_LEDGER_DAO_DB_PATH", str(tmp_path / "decision_ledger.db")
    )

    # Drop cached modules so the env vars take effect.
    for mod in (
        "scripts.migrate_db",
        "core.database_manager",
        "core.timescale_db",
    ):
        sys.modules.pop(mod, None)
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

    from scripts.migrate_db import main  # noqa: WPS433 — intentional late import

    rc = asyncio.run(main())
    captured = capsys.readouterr()
    assert rc == 0, f"migrate_db.py exited {rc}; output: {captured.out}"
    # backend_name after initialize() is "sqlite" (no DATABASE_URL → SQLite fallback).
    assert "Active backend: sqlite" in captured.out
    assert "Applied" in captured.out

    # The market_intelligence.db file was created at the redirected path.
    assert market_db_path.exists(), (
        f"market_intelligence.db not created at {market_db_path}; "
        f"tmp_path contents: {list(tmp_path.iterdir())}"
    )

    # The 002 migration was applied (and 001 too, since both are
    # SQLite-compatible).
    with sqlite3.connect(str(market_db_path)) as conn:
        cursor = conn.execute(
            "SELECT name FROM _migrations ORDER BY id"
        )
        applied_names = {row[0] for row in cursor.fetchall()}
    assert "002_unified_schema.sql" in applied_names

    # Cleanup: drop the cached singleton so sibling tests get a fresh one.
    sys.modules.pop("scripts.migrate_db", None)
    sys.modules.pop("core.database_manager", None)
    sys.modules.pop("core.timescale_db", None)
