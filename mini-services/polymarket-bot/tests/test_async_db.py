"""
Unit tests for the W16-7 async SQLite pool + async repository layer.

W16-7 — ``core/db_pool.py`` + ``core/async_repositories.py``.

Covers:

  1. ``AsyncDBPool.get_connection`` — same db_path returns the same
     ``aiosqlite.Connection`` (pooling contract); different db_paths
     return different connections.
  2. ``execute`` — SELECT returns rows as ``list[dict]`` (JSON-able).
  3. ``execute_many`` — INSERTs N rows; returns affected row count.
  4. ``execute_scalar`` — returns the first column of the first row,
     ``None`` for empty result sets.
  5. ``transaction`` — commits on clean exit, rolls back on exception
     (the post-exception state must NOT include the un-committed row).
  6. ``AsyncDecisionRepository`` — ``get_recent`` / ``get_by_token`` /
     ``count_by_stage`` against a hand-seeded ``decision_events``
     table mirroring the sync recorder's schema.
  7. ``AsyncObservabilityRepository`` — ``get_latest_metrics`` returns
     the highest-id row per ``(category, name)`` group;
     ``get_metric_history`` returns the most-recent-N samples for a
     single metric name.
  8. ``AsyncExecutionQualityRepository`` — ``get_recent_fills`` returns
     the most-recent N rows; ``get_stats`` aggregates AVG(slippage_bps)
     over non-NULL rows + total row count.
  9. ``close_all`` — closes every open connection + clears the pool;
     idempotent on second call.

Each test creates its own temp DB under ``tmp_path`` so tests are
hermetic to each other. A module-level autouse fixture clears the
module-level singleton ``db_pool``'s connection cache BEFORE each
test so the singleton (imported by ``core.async_repositories``)
doesn't carry over a connection opened by a prior test — this
matters because ``AsyncDBPool.get_connection`` is memoised per
db_path, and a stale connection to a deleted temp file would raise
``aiosqlite.OperationalError`` on the next call.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (pytest-asyncio is already a
project dependency — see ``tests/test_decision_ledger.py`` for the
same idiom).
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

from core.async_repositories import (
    AsyncDecisionRepository,
    AsyncExecutionQualityRepository,
    AsyncObservabilityRepository,
)
from core.db_pool import AsyncDBPool
from core.db_pool import db_pool as _singleton_db_pool

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the S9 / decision_ledger test pattern.
pytestmark = pytest.mark.asyncio


# ── Fixture: reset the module-level singleton before each test ──────────────
@pytest.fixture(autouse=True)
def _reset_singleton_db_pool():
    """Drop the singleton ``db_pool``'s connection cache BEFORE each test.

    ``core.async_repositories`` imports the module-level ``db_pool``
    singleton (``from core.db_pool import db_pool``). Without this
    reset, a test that exercised a repository (opening a connection to
    its tmp_path DB) would leave that connection cached in the
    singleton; the next test's ``AsyncDecisionRepository`` would call
    ``get_connection`` against a NEW tmp_path, hit the cache miss, and
    open a fresh connection — that part is fine — BUT the stale
    connection from the prior test would also still be open, holding
    a file descriptor to a tmp_path the OS has already cleaned up.

    The reset closes the singleton's cached connections + clears the
    dict so each test starts with a cold pool. The pool itself (the
    ``AsyncDBPool`` instance) is NOT replaced — production code holds
    a reference to it via the module-level ``db_pool`` symbol, and
    re-binding the symbol would break that reference. Clearing the
    internal ``_pools`` dict is the correct, surgical reset.
    """
    # Close any cached connections synchronously-ish: the close_all
    # coroutine is awaited via a fresh event loop. Each test function
    # is itself running inside its own asyncio event loop (pytest-
    # asyncio's strict mode), so we can't ``await`` here directly —
    # we use ``asyncio.run`` to drive the close in a throwaway loop.
    # This is safe because ``close_all`` is idempotent + has no
    # ordering requirement against the test's own loop.
    try:
        asyncio.run(_singleton_db_pool.close_all())
    except RuntimeError:
        # ``asyncio.run`` can't be called from inside a running loop —
        # if a future pytest-asyncio mode runs the autouse fixture
        # inside the test loop, fall back to scheduling the close on
        # the test loop. The close is best-effort; the test's own
        # AsyncDBPool instance is independent of the singleton anyway.
        pass
    yield


# ── Schema setup helpers ─────────────────────────────────────────────────────
# The async repositories target the SAME schema the sync recorders create
# (``decision_events`` / ``metrics`` / ``execution_quality``). We re-create
# the tables via the standard sync ``sqlite3`` module so the tests exercise
# the actual production schema shape, not a stub. Mirror the column lists
# from ``core/decision_ledger.py::_init_db`` / ``core/observability.py::
# _init_db`` / ``core/execution_quality.py::_init_db`` so a future schema
# drift surfaces as a test failure here.


def _seed_decision_events(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
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
        conn.executemany(
            "INSERT INTO decision_events "
            "(timestamp, decision_id, stage, token_id, strategy, pnl, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (1000.0, "dec-1", "PREDICTION", "TOK_A", "ml_sig_v1", 0.0, "{}"),
                (1001.0, "dec-1", "SIGNAL", "TOK_A", "ml_sig_v1", 0.0, "{}"),
                (1002.0, "dec-1", "ORDER", "TOK_A", "ml_sig_v1", 0.0, "{}"),
                (1003.0, "dec-1", "FILL", "TOK_A", "ml_sig_v1", 1.5, "{}"),
                (1004.0, "dec-2", "PREDICTION", "TOK_B", "mm_avellaneda", 0.0, "{}"),
                (1005.0, "dec-2", "RISK_REJECTED", "TOK_B", "mm_avellaneda", 0.0, "{}"),
            ],
        )
        conn.commit()


def _seed_metrics(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                category TEXT NOT NULL,
                name TEXT NOT NULL,
                value REAL NOT NULL,
                metadata_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO metrics (timestamp, category, name, value, metadata_json) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (1.0, "system", "cpu_percent", 12.0, "{}"),
                (2.0, "system", "cpu_percent", 18.0, "{}"),
                (3.0, "system", "cpu_percent", 22.0, "{}"),  # latest for system/cpu
                (4.0, "ml", "inference_latency", 0.042, "{}"),
                (5.0, "ml", "inference_latency", 0.038, "{}"),  # latest for ml/inference_latency
                (6.0, "execution", "fills", 1.0, "{}"),
            ],
        )
        conn.commit()


def _seed_execution_quality(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS execution_quality (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                order_id TEXT,
                decision_id TEXT,
                token_id TEXT,
                strategy TEXT,
                side TEXT,
                signal_price REAL,
                decision_price REAL,
                submitted_price REAL,
                best_bid REAL,
                best_ask REAL,
                expected_fill REAL,
                actual_fill REAL,
                spread REAL,
                slippage REAL,
                slippage_bps REAL,
                latency_ms REAL,
                realized_edge REAL,
                paper INTEGER DEFAULT 1,
                data_json TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO execution_quality "
            "(timestamp, order_id, token_id, strategy, side, signal_price, "
            " decision_price, submitted_price, best_bid, best_ask, expected_fill, "
            " actual_fill, spread, slippage, slippage_bps, latency_ms, realized_edge) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (1.0, "o-1", "TOK_A", "ml_sig_v1", "BUY", 0.50, 0.51, 0.51, 0.50, 0.52, 0.52, 0.525, 0.02, 0.005, 96.15, 12.0, -0.025),
                (2.0, "o-2", "TOK_A", "ml_sig_v1", "SELL", 0.55, 0.54, 0.54, 0.52, 0.56, 0.52, 0.515, 0.04, -0.005, -96.15, 15.0, 0.035),
                (3.0, "o-3", "TOK_B", "mm_avellaneda", "BUY", 0.30, 0.30, 0.30, 0.29, 0.31, 0.31, 0.31, 0.02, 0.0, None, 8.0, -0.01),
            ],
        )
        conn.commit()


# ── 1. get_connection — same db → same conn, different db → different conn ──
async def test_get_connection_returns_same_conn_for_same_db(tmp_path):
    """``get_connection`` must memoise per db_path so the pool opens
    exactly one connection per database."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    # Touch the file so aiosqlite can connect (aiosqlite.connect creates
    # the file, but the WAL pragma needs the parent dir to exist —
    # ``tmp_path`` already exists).
    c1 = await pool.get_connection(db)
    c2 = await pool.get_connection(db)
    assert c1 is c2, "Same db_path must return the same connection object"
    await pool.close_all()


async def test_get_connection_returns_distinct_conns_for_distinct_dbs(tmp_path):
    """Two different db_paths must yield two different connections."""
    pool = AsyncDBPool()
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    c_a = await pool.get_connection(db_a)
    c_b = await pool.get_connection(db_b)
    assert c_a is not c_b
    await pool.close_all()


async def test_get_connection_creates_parent_dir(tmp_path):
    """If the parent directory doesn't exist, ``get_connection`` must
    create it (mirrors the sync recorder's ``mkdir(parents=True,
    exist_ok=True)``)."""
    pool = AsyncDBPool()
    db = str(tmp_path / "nested" / "deeper" / "db.sqlite")
    conn = await pool.get_connection(db)
    assert conn is not None
    # The file itself is created lazily by aiosqlite when the first
    # statement runs; verify the parent dir was created.
    assert (tmp_path / "nested" / "deeper").is_dir()
    await pool.close_all()


# ── 2. execute — SELECT returns rows as list[dict] ──────────────────────────
async def test_execute_returns_rows_as_dicts(tmp_path):
    """``execute`` must return rows as plain dicts (JSON-able) keyed
    by column name, not raw ``aiosqlite.Row`` instances."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT)")
        c.execute("INSERT INTO t (name) VALUES (?), (?)", ("alpha", "beta"))
        c.commit()
    rows = await pool.execute(db, "SELECT * FROM t ORDER BY id")
    assert rows == [
        {"id": 1, "name": "alpha"},
        {"id": 2, "name": "beta"},
    ]
    assert all(isinstance(r, dict) for r in rows), "Rows must be plain dicts"
    await pool.close_all()


# ── 3. execute_many — returns affected row count ────────────────────────────
async def test_execute_many_inserts_multiple_rows(tmp_path):
    """``execute_many`` must insert every tuple in the params list
    and return the affected-row count (sqlite reports ``1`` per
    successful INSERT in an executemany — the cumulative total)."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val INTEGER)")
        c.commit()
    n = await pool.execute_many(
        db,
        "INSERT INTO t (val) VALUES (?)",
        [(1,), (2,), (3,), (4,)],
    )
    assert n == 4, f"Expected 4 inserted rows, got {n}"
    rows = await pool.execute(db, "SELECT COUNT(*) AS n FROM t")
    assert rows[0]["n"] == 4
    await pool.close_all()


# ── 4. execute_scalar — first column of first row, None for empty set ───────
async def test_execute_scalar_returns_first_column(tmp_path):
    """``execute_scalar`` returns the first column of the first row."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val REAL)")
        c.executemany("INSERT INTO t (val) VALUES (?)", [(1.0,), (2.0,), (3.0,)])
        c.commit()
    avg = await pool.execute_scalar(db, "SELECT AVG(val) FROM t")
    assert avg == 2.0
    await pool.close_all()


async def test_execute_scalar_returns_none_for_empty_result(tmp_path):
    """``execute_scalar`` must return ``None`` when the query yields
    zero rows (so callers can use ``or 0`` defaults without
    distinguishing between NULL and no rows)."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.commit()
    result = await pool.execute_scalar(db, "SELECT id FROM t WHERE id = ?", (999,))
    assert result is None
    await pool.close_all()


# ── 5. transaction — commit on clean exit, rollback on exception ───────────
async def test_transaction_commits_on_clean_exit(tmp_path):
    """A transaction that exits cleanly must commit its writes."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        c.commit()
    async with pool.transaction(db) as conn:
        await conn.execute("INSERT INTO t (val) VALUES (?)", ("committed",))
    rows = await pool.execute(db, "SELECT * FROM t")
    assert len(rows) == 1
    assert rows[0]["val"] == "committed"
    await pool.close_all()


async def test_transaction_rolls_back_on_exception(tmp_path):
    """A transaction that raises must roll back its writes — the
    post-exception DB state must NOT include the un-committed row."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
        c.commit()
    with pytest.raises(ValueError, match="rollback test"):
        async with pool.transaction(db) as conn:
            await conn.execute("INSERT INTO t (val) VALUES (?)", ("transient",))
            raise ValueError("rollback test")
    rows = await pool.execute(db, "SELECT * FROM t")
    assert rows == [], "Rolled-back row must not persist"
    await pool.close_all()


async def test_transaction_propagates_exception(tmp_path):
    """The original exception must propagate out of the ``async with``
    block (the rollback must not swallow it)."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        c.commit()
    sentinel = RuntimeError("boom")
    try:
        async with pool.transaction(db) as _conn:
            raise sentinel
    except RuntimeError as exc:
        assert exc is sentinel
    else:  # pragma: no cover — defensive
        pytest.fail("Exception was swallowed by transaction context manager")
    await pool.close_all()


# ── 6. AsyncDecisionRepository ───────────────────────────────────────────────
async def test_decision_repo_get_recent_all_stages(tmp_path):
    """``get_recent`` with no ``stage`` filter returns the most-recent
    N rows (newest first) across all stages."""
    db = str(tmp_path / "decision.db")
    _seed_decision_events(db)
    repo = AsyncDecisionRepository(db)
    rows = await repo.get_recent(limit=10)
    assert len(rows) == 6
    # Newest first → timestamps must be non-increasing.
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)
    assert rows[0]["timestamp"] == 1005.0
    assert rows[-1]["timestamp"] == 1000.0


async def test_decision_repo_get_recent_filtered_by_stage(tmp_path):
    """``get_recent(stage=...)`` narrows to a single stage."""
    db = str(tmp_path / "decision.db")
    _seed_decision_events(db)
    repo = AsyncDecisionRepository(db)
    rows = await repo.get_recent(limit=10, stage="PREDICTION")
    assert len(rows) == 2
    assert all(r["stage"] == "PREDICTION" for r in rows)


async def test_decision_repo_get_recent_respects_limit(tmp_path):
    """``get_recent(limit=N)`` must return at most N rows."""
    db = str(tmp_path / "decision.db")
    _seed_decision_events(db)
    repo = AsyncDecisionRepository(db)
    rows = await repo.get_recent(limit=2)
    assert len(rows) == 2
    # Top-2 newest timestamps
    assert rows[0]["timestamp"] == 1005.0
    assert rows[1]["timestamp"] == 1004.0


async def test_decision_repo_get_by_token(tmp_path):
    """``get_by_token`` returns only the rows for the named token,
    newest first."""
    db = str(tmp_path / "decision.db")
    _seed_decision_events(db)
    repo = AsyncDecisionRepository(db)
    rows = await repo.get_by_token("TOK_A", limit=10)
    assert len(rows) == 4
    assert all(r["token_id"] == "TOK_A" for r in rows)
    assert rows[0]["timestamp"] == 1003.0
    assert rows[-1]["timestamp"] == 1000.0


async def test_decision_repo_count_by_stage(tmp_path):
    """``count_by_stage`` returns the integer count of rows for a stage."""
    db = str(tmp_path / "decision.db")
    _seed_decision_events(db)
    repo = AsyncDecisionRepository(db)
    assert await repo.count_by_stage("PREDICTION") == 2
    assert await repo.count_by_stage("FILL") == 1
    assert await repo.count_by_stage("RISK_REJECTED") == 1
    assert await repo.count_by_stage("NONEXISTENT") == 0


# ── 7. AsyncObservabilityRepository ──────────────────────────────────────────
async def test_observability_repo_get_latest_metrics(tmp_path):
    """``get_latest_metrics`` returns the highest-id row per
    ``(category, name)`` group, ordered for stable dashboard rendering."""
    db = str(tmp_path / "obs.db")
    _seed_metrics(db)
    repo = AsyncObservabilityRepository(db)
    rows = await repo.get_latest_metrics()
    # Three (category, name) pairs were seeded: system/cpu_percent,
    # ml/inference_latency, execution/fills.
    assert len(rows) == 3
    # Stable order: alphabetical by (category, name).
    assert rows[0]["category"] == "execution"
    assert rows[0]["name"] == "fills"
    assert rows[0]["value"] == 1.0
    assert rows[1]["category"] == "ml"
    assert rows[1]["name"] == "inference_latency"
    assert rows[1]["value"] == 0.038  # latest sample, not the first
    assert rows[2]["category"] == "system"
    assert rows[2]["name"] == "cpu_percent"
    assert rows[2]["value"] == 22.0  # latest sample (id=3)


async def test_observability_repo_get_metric_history(tmp_path):
    """``get_metric_history`` returns the most-recent-N samples for a
    single metric name, newest first."""
    db = str(tmp_path / "obs.db")
    _seed_metrics(db)
    repo = AsyncObservabilityRepository(db)
    rows = await repo.get_metric_history("cpu_percent", limit=2)
    assert len(rows) == 2
    # Newest first.
    assert rows[0]["value"] == 22.0
    assert rows[1]["value"] == 18.0


async def test_observability_repo_get_metric_history_unknown_name(tmp_path):
    """``get_metric_history`` for an unknown name returns an empty list
    (no exception, no None)."""
    db = str(tmp_path / "obs.db")
    _seed_metrics(db)
    repo = AsyncObservabilityRepository(db)
    rows = await repo.get_metric_history("nonexistent_metric")
    assert rows == []


# ── 8. AsyncExecutionQualityRepository ───────────────────────────────────────
async def test_execution_quality_repo_get_recent_fills(tmp_path):
    """``get_recent_fills`` returns the most-recent N rows, newest first."""
    db = str(tmp_path / "eq.db")
    _seed_execution_quality(db)
    repo = AsyncExecutionQualityRepository(db)
    rows = await repo.get_recent_fills(limit=10)
    assert len(rows) == 3
    assert rows[0]["timestamp"] == 3.0
    assert rows[1]["timestamp"] == 2.0
    assert rows[2]["timestamp"] == 1.0
    assert rows[0]["token_id"] == "TOK_B"


async def test_execution_quality_repo_get_recent_fills_respects_limit(tmp_path):
    """``get_recent_fills(limit=N)`` must return at most N rows."""
    db = str(tmp_path / "eq.db")
    _seed_execution_quality(db)
    repo = AsyncExecutionQualityRepository(db)
    rows = await repo.get_recent_fills(limit=2)
    assert len(rows) == 2
    assert rows[0]["timestamp"] == 3.0
    assert rows[1]["timestamp"] == 2.0


async def test_execution_quality_repo_get_stats(tmp_path):
    """``get_stats`` returns the AVG of non-NULL slippage_bps + the
    total fill count. The NULL ``slippage_bps`` row (seeded third)
    must be excluded from the AVG but counted in ``total_fills``."""
    db = str(tmp_path / "eq.db")
    _seed_execution_quality(db)
    repo = AsyncExecutionQualityRepository(db)
    stats = await repo.get_stats()
    # AVG(96.15, -96.15) over the 2 non-NULL rows = 0.0
    assert stats["avg_slippage_bps"] == 0.0
    assert stats["total_fills"] == 3


async def test_execution_quality_repo_get_stats_empty_table(tmp_path):
    """``get_stats`` against an empty table returns zeros (not NULL /
    not an exception) — the ``or 0`` defaults in ``get_stats`` guard
    against a ``None`` AVG / COUNT return for empty result sets."""
    db = str(tmp_path / "eq.db")
    # Create the table but no rows.
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE execution_quality (id INTEGER PRIMARY KEY, "
            "slippage_bps REAL, timestamp REAL)"
        )
        c.commit()
    repo = AsyncExecutionQualityRepository(db)
    stats = await repo.get_stats()
    assert stats == {"avg_slippage_bps": 0.0, "total_fills": 0}


# ── 9. close_all — closes connections + clears the pool + idempotent ────────
async def test_close_all_clears_pool(tmp_path):
    """``close_all`` must remove every cached connection so the next
    ``get_connection`` opens a fresh one."""
    pool = AsyncDBPool()
    db = str(tmp_path / "p.db")
    c1 = await pool.get_connection(db)
    await pool.close_all()
    assert pool._pools == {}, "Pool dict must be empty after close_all"
    # New connection must be a different object than the closed one.
    c2 = await pool.get_connection(db)
    assert c2 is not c1
    await pool.close_all()


async def test_close_all_is_idempotent(tmp_path):
    """``close_all`` must be safe to call when no connections are open
    (and safe to call twice in a row)."""
    pool = AsyncDBPool()
    # No connections opened — must not raise.
    await pool.close_all()
    await pool.close_all()
    assert pool._pools == {}


async def test_close_all_logs_errors_but_continues(tmp_path, caplog):
    """If a single ``conn.close()`` raises, ``close_all`` must log
    the error and continue closing the remaining connections rather
    than aborting the teardown."""
    pool = AsyncDBPool()
    db_a = str(tmp_path / "a.db")
    db_b = str(tmp_path / "b.db")
    c_a = await pool.get_connection(db_a)
    await pool.get_connection(db_b)

    # Poison connection A's close() so it raises.
    async def _boom():
        raise RuntimeError("close failed")
    c_a.close = _boom  # type: ignore[method-assign]

    with caplog.at_level("ERROR", logger="core.db_pool"):
        await pool.close_all()
    # Connection B must still have been closed + the pool cleared.
    assert pool._pools == {}
    # The error must have been logged.
    assert any("Error closing" in rec.message for rec in caplog.records), (
        "close_all must log the per-connection error"
    )
    # Cleanup connection B manually in case the test loop reuses it
    # (close_all already invoked c_b.close(); the underlying aiosqlite
    # connection should already be torn down).
