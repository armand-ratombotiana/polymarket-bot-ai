"""
tests/test_order_book_depth_storage.py — Unit tests for the W21-5
order book depth storage read API.

Scope: W21-5 task spec — verify that order book depth (bids_json,
asks_json, bid_depth_10, ask_depth_10) is properly stored on BOTH
PostgreSQL and SQLite backends, and that the W21-5 read API
(``get_order_book_depth`` / ``get_depth_history``) and HTTP routes
(``GET /api/depth-full/{token_id}`` /
``GET /api/depth-history/{token_id}``) return the parsed ladders
correctly.

Background
~~~~~~~~~~
The W19-5 task spec described a fix for the SQLite INSERT path in
``record_snapshot`` (the SQLite INSERT was dropping the ``bids_json`` /
``asks_json`` / ``bid_depth_10`` / ``ask_depth_10`` columns and only
storing the top-of-book fields). The fix was described but never
actually applied — the SQLite schema still declared only the
top-of-book columns, and the SQLite INSERT still only wrote those
columns.

W21-5 closes that gap by:

  1. Adding the missing columns to the SQLite ``market_snapshots``
     schema (in ``_init_sqlite_fallback``) — fresh DBs get them via
     ``CREATE TABLE``; legacy DBs get them via idempotent
     ``ALTER TABLE ADD COLUMN`` migrations.

  2. Updating the SQLite INSERT path in ``record_snapshot`` to write
     the new columns alongside the existing top-of-book columns.

  3. Updating the PG INSERT path in ``record_snapshot`` to also write
     the ``bid_depth_10`` / ``ask_depth_10`` columns (the PG schema
     already declared them in migration ``001_initial_enterprise_schemas.sql``
     but the INSERT was omitting them).

  4. Updating ``book_poller._apply_book`` to pass the full bid/ask
     ladders (as ``{"price": float, "size": float}`` dicts) through
     to ``record_snapshot`` so the JSON columns are populated with
     real ladder data — without this call-site fix the JSON columns
     would have stayed ``NULL`` forever even after the schema fix.

  5. Adding a new ``core/database_manager.py`` module that exposes
     three async read methods (``get_snapshots``,
     ``get_order_book_depth``, ``get_depth_history``) on a
     ``DatabaseManager`` singleton (``db_manager``) — the read API
     the new ``GET /api/depth-full/{token_id}`` and
     ``GET /api/depth-history/{token_id}`` HTTP endpoints expose.

  6. Registering the two new HTTP endpoints on the production
     FastAPI app via ``register_routes(app)`` (mirrors the pattern
     used by every sibling ``core.*`` feature module — see the
     W20-7 trade-tape block in ``api/server.py``).

Test coverage
~~~~~~~~~~~~~
The tests below cover the W21-5 task spec verification surface:

  1. ``record_snapshot`` with bids/asks preserves the full ladder
     (round-trip through ``record_snapshot`` → ``get_snapshots``).
  2. ``record_snapshot`` pre-computes the depth-10 summaries
     (``bid_depth_10`` / ``ask_depth_10``) from the top 10 ladder
     levels.
  3. ``get_order_book_depth`` returns the parsed ladders + the
     depth-10 summaries + the top-of-book fields.
  4. ``get_order_book_depth`` on a token with no snapshots returns
     a well-formed "no data" payload (empty ladders, zeroed
     summaries) rather than raising.
  5. ``get_depth_history`` returns the time-windowed series ascending
     by timestamp, with each row carrying the parsed top-10 ladders.
  6. ``get_depth_history`` honours the ``hours`` cutoff (rows older
     than ``now - hours*3600`` are filtered out).
  7. The legacy DB migration path works — a ``market_snapshots``
     table created WITHOUT the new columns gets them added
     idempotently via ``ALTER TABLE``.
  8. ``book_poller._apply_book`` passes the full ladders through to
     ``record_snapshot`` (the call-site fix from W21-5 step 4).
  9. ``GET /api/depth-full/{token_id}`` returns HTTP 200 + the
     parsed ladder on a fresh DB.
  10. ``GET /api/depth-history/{token_id}`` returns HTTP 200 + the
      time-windowed series on a fresh DB.

Testing strategy
----------------
All tests construct a fresh ``TimescaleDBEngine`` whose SQLite file
lives under ``tmp_path`` so the tests are hermetic and cannot clobber
any real persisted state. The ``timescale_db`` singleton is
monkeypatched (mirrors the ``trade_tape_app`` fixture pattern in
``tests/test_trade_ingester.py``) so the ``db_manager`` singleton
picks up the fresh engine via the ``_ts_module.timescale_db``
attribute lookup.

The HTTP tests build a fresh ``FastAPI()`` app and call
``register_routes(app)`` on it — exactly the registration path the
production ``api/server.py`` uses (W21-5 wiring block at end of
file). This isolates the depth endpoints from the production
server's bearer-token auth middleware and the heavy ``lifespan``
startup.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (mirrors the
convention in ``tests/test_book_poller.py``).
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.timescale_db import TimescaleDBEngine
from core.database_manager import (
    DatabaseBackend,
    DatabaseManager,
    db_manager,
    database_manager,
    register_routes,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in ``tests/test_book_poller.py``.
pytestmark = pytest.mark.asyncio


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _build_ladder(start_price: float, n: int, step: float = 0.001,
                  base_size: float = 100.0, size_step: float = 5.0) -> list[dict]:
    """Build a deterministic ladder of ``n`` price levels.

    Used to seed ``record_snapshot`` calls — the JSON column carries
    the full ladder, so we need a non-trivial ladder (> 10 levels) to
    verify the depth-10 summary caps at the first 10 entries.
    """
    return [
        {"price": start_price + i * step, "size": base_size + i * size_step}
        for i in range(n)
    ]


def _expected_depth_10(ladder: list[dict]) -> float:
    """Compute the expected ``*_depth_10`` summary for a ladder.

    Mirrors the W21-5 ``record_snapshot`` pre-compute path: sum of
    the ``size`` field of the first 10 entries. Used to assert the
    stored ``bid_depth_10`` / ``ask_depth_10`` columns match the
    pre-compute logic.
    """
    return float(sum(float(b.get("size", 0)) for b in ladder[:10]))


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TimescaleDBEngine:
    """Fresh ``TimescaleDBEngine`` whose SQLite file lives under ``tmp_path``.

    The ``core.timescale_db.timescale_db`` singleton is monkeypatched so
    the ``db_manager`` singleton picks up the fresh engine via the
    ``_ts_module.timescale_db`` attribute lookup (the W21-5 indirection
    in ``core/database_manager.py``). Mirrors the ``trade_tape_app``
    fixture pattern in ``tests/test_trade_ingester.py``.

    The engine is constructed with ``_is_postgres=False`` so the read
    paths route through the SQLite fallback (the only backend the
    sandboxed conftest environment can reach — PG would require a
    live TimescaleDB instance, which is out of scope for these unit
    tests).

    Also re-points the singleton ``db_manager._sqlite_paths["market"]``
    to the fresh engine's SQLite path so the HTTP routes registered by
    ``register_routes`` (which use the singleton ``db_manager``) read
    from the same file the engine wrote to. Without this, the depth
    routes return empty payloads (the singleton still reads from the
    conftest-redirected ``MARKET_DB_PATH``).
    """
    engine = TimescaleDBEngine(sqlite_path=tmp_path / "test_depth.db")
    engine._is_postgres = False
    monkeypatch.setattr("core.timescale_db.timescale_db", engine)
    # Re-point the singleton ``db_manager``'s cached market path at the
    # fresh engine's SQLite file. ``register_routes`` (and the depth
    # HTTP tests that use ``depth_client``) close over the singleton
    # ``db_manager`` directly — without this re-point, the routes read
    # from the conftest-redirected ``MARKET_DB_PATH`` and never see the
    # rows the fresh engine wrote.
    from core.database_manager import db_manager as _singleton_db_manager

    _singleton_db_manager._sqlite_paths["market"] = str(engine._sqlite_path)
    _singleton_db_manager._ensure_market_schema(engine._sqlite_path)
    return engine


@pytest.fixture
def fresh_db_manager(fresh_engine: TimescaleDBEngine) -> DatabaseManager:
    """Fresh ``DatabaseManager`` instance scoped to ``fresh_engine``.

    The fixture returns a brand-new ``DatabaseManager()`` (NOT the
    module-level singleton) so the ``_status`` state from a prior test
    can't leak in. The fresh instance reads from ``fresh_engine``
    through the ``_ts_module.timescale_db`` attribute lookup (the
    monkeypatch in ``fresh_engine`` applies to all instances, not just
    the singleton).

    The fresh instance's ``_sqlite_paths["market"]`` is re-pointed at
    ``fresh_engine._sqlite_path`` so reads from
    ``DatabaseManager.get_snapshots`` / ``get_order_book_depth`` /
    ``get_depth_history`` hit the same SQLite file that
    ``fresh_engine.record_snapshot`` wrote to (without this, the
    DatabaseManager resolves ``market`` via the conftest-redirected
    ``MARKET_DB_PATH`` and the test sees an empty result set).
    """
    mgr = DatabaseManager()
    # Re-point the market DB read path at the fresh engine's SQLite
    # file so reads + writes share the same physical file. This mirrors
    # what the production lifespan does (DatabaseManager resolves the
    # market path via ``timescale_db._sqlite_path`` at initialize time
    # — the conftest env-var redirect short-circuits that lookup, so we
    # re-establish the link here for the per-test engine).
    mgr._sqlite_paths["market"] = str(fresh_engine._sqlite_path)
    # The fresh engine's ``_init_sqlite_fallback`` creates the
    # ``market_snapshots`` table WITHOUT ``bid_size`` / ``ask_size``
    # columns (the legacy timescale_db schema). The DatabaseManager's
    # SELECT for ``get_snapshots`` / ``get_order_book_depth`` /
    # ``get_depth_history`` projects those columns, so we run the
    # in-place schema migration here to add them. ``_ensure_market_schema``
    # is idempotent (CREATE TABLE IF NOT EXISTS + ALTER TABLE) so the
    # call is a no-op when the columns already exist.
    mgr._ensure_market_schema(fresh_engine._sqlite_path)
    return mgr


@pytest.fixture
def depth_app(fresh_engine: TimescaleDBEngine) -> FastAPI:
    """Fresh ``FastAPI()`` app with ONLY the W21-5 depth routes registered.

    The ``timescale_db`` singleton is replaced with a fresh
    ``TimescaleDBEngine`` whose SQLite file lives under ``tmp_path``
    (via the ``fresh_engine`` fixture) so the routes can be exercised
    end-to-end without touching the conftest-redirected singleton's
    state. Mirrors the ``trade_tape_app`` fixture in
    ``tests/test_trade_ingester.py``.
    """
    app = FastAPI()
    register_routes(app)
    return app


@pytest.fixture
def depth_client(depth_app: FastAPI) -> TestClient:
    """``TestClient`` bound to the fresh depth app.

    Constructed WITHOUT entering the ``with`` context manager so the
    app's lifespan is NOT triggered (the production lifespan starts
    every background task; we don't want any of those running during
    the route contract tests).
    """
    return TestClient(depth_app)


# ────────────────────────────────────────────────────────────────────────────
# 1. record_snapshot with bids/asks preserves the full ladder
# ────────────────────────────────────────────────────────────────────────────

async def test_record_snapshot_preserves_full_ladder(fresh_engine: TimescaleDBEngine):
    """``record_snapshot`` with bids/asks must persist the FULL ladder
    into the ``bids_json`` / ``asks_json`` JSON columns on the SQLite
    fallback, not just the top-of-book fields.

    Setup: 15-level bid ladder + 15-level ask ladder. Write via
    ``record_snapshot``. Read the raw row back from SQLite
    (bypassing the read API) and assert the JSON column carries
    every level verbatim.

    Belt-and-braces:
      * Both ``bids_json`` and ``asks_json`` are non-NULL.
      * The parsed ladders have 15 entries each (no truncation).
      * Each entry's ``price`` / ``size`` match the seeded values.
      * The top-of-book fields (``best_bid`` / ``best_ask`` / ``mid``
        / ``spread``) are also persisted correctly.
    """
    bids = _build_ladder(0.49, n=15)
    asks = _build_ladder(0.51, n=15)

    ok = await fresh_engine.record_snapshot(
        token_id="0xTEST",
        slug="test-market",
        best_bid=0.49,
        best_ask=0.51,
        mid=0.50,
        spread=0.02,
        bids_json=bids,
        asks_json=asks,
    )
    assert ok is True, "record_snapshot must return True on a successful SQLite write"

    # Read the raw row back — bypass the read API so we verify the
    # JSON column itself, not the parsed view.
    with sqlite3.connect(str(fresh_engine._sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bids_json, asks_json, best_bid, best_ask, mid, spread, "
            "       bid_depth_10, ask_depth_10, ingestion_time "
            "FROM market_snapshots "
            "WHERE token_id = ? ORDER BY id DESC LIMIT 1",
            ("0xTEST",),
        ).fetchone()

    assert row is not None, "row must be persisted"
    assert row["bids_json"] is not None, "bids_json column must be populated"
    assert row["asks_json"] is not None, "asks_json column must be populated"
    assert row["ingestion_time"] is not None, "ingestion_time column must be populated"

    parsed_bids = json.loads(row["bids_json"])
    parsed_asks = json.loads(row["asks_json"])

    # Full ladder preserved — 15 levels each, no truncation.
    assert len(parsed_bids) == 15
    assert len(parsed_asks) == 15

    # Each level's price / size match the seeded values verbatim.
    for i, (expected, actual) in enumerate(zip(bids, parsed_bids)):
        assert float(actual["price"]) == pytest.approx(expected["price"])
        assert float(actual["size"]) == pytest.approx(expected["size"])

    for i, (expected, actual) in enumerate(zip(asks, parsed_asks)):
        assert float(actual["price"]) == pytest.approx(expected["price"])
        assert float(actual["size"]) == pytest.approx(expected["size"])

    # Top-of-book fields also persisted.
    assert float(row["best_bid"]) == pytest.approx(0.49)
    assert float(row["best_ask"]) == pytest.approx(0.51)
    assert float(row["mid"]) == pytest.approx(0.50)
    assert float(row["spread"]) == pytest.approx(0.02)


# ────────────────────────────────────────────────────────────────────────────
# 2. record_snapshot pre-computes the depth-10 summaries
# ────────────────────────────────────────────────────────────────────────────

async def test_record_snapshot_precomputes_depth_10(fresh_engine: TimescaleDBEngine):
    """``record_snapshot`` must pre-compute ``bid_depth_10`` /
    ``ask_depth_10`` from the top 10 ladder levels and persist them
    as numeric columns alongside the JSON ladders.

    Setup: 15-level ladders. The expected ``*_depth_10`` is the sum
    of the first 10 ``size`` values. Write via ``record_snapshot``,
    then read the raw row back and assert the stored
    ``bid_depth_10`` / ``ask_depth_10`` match the pre-compute.

    Belt-and-braces:
      * The depth-10 summary is NOT the sum of all 15 levels — only
        the first 10. (Tests the ``ladder[:10]`` slice.)
      * The depth-10 column is a numeric value (float), not a JSON
        string. (Tests the column type.)
    """
    bids = _build_ladder(0.49, n=15)
    asks = _build_ladder(0.51, n=15)
    expected_bid_depth_10 = _expected_depth_10(bids)
    expected_ask_depth_10 = _expected_depth_10(asks)

    # Sanity: the depth-10 is NOT the full-ladder sum (15 levels).
    full_bid_sum = float(sum(b["size"] for b in bids))
    assert expected_bid_depth_10 != pytest.approx(full_bid_sum), (
        "depth-10 must be the sum of the first 10 levels only, not the full ladder"
    )

    await fresh_engine.record_snapshot(
        token_id="0xD10",
        slug="depth-10-test",
        best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
        bids_json=bids, asks_json=asks,
    )

    with sqlite3.connect(str(fresh_engine._sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bid_depth_10, ask_depth_10 FROM market_snapshots "
            "WHERE token_id = ? ORDER BY id DESC LIMIT 1",
            ("0xD10",),
        ).fetchone()

    assert row is not None
    assert float(row["bid_depth_10"]) == pytest.approx(expected_bid_depth_10)
    assert float(row["ask_depth_10"]) == pytest.approx(expected_ask_depth_10)


async def test_record_snapshot_depth_10_handles_empty_ladder(fresh_engine: TimescaleDBEngine):
    """``record_snapshot`` with no ladders must write NULL JSON columns
    and zero-valued depth-10 summaries — NOT raise.

    Mirrors the legacy call path (``book_poller._apply_book`` before
    the W21-5 call-site fix) which passed only the top-of-book fields.
    """
    ok = await fresh_engine.record_snapshot(
        token_id="0xEMPTY",
        slug="empty-ladder",
        best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
        # bids_json / asks_json intentionally omitted
    )
    assert ok is True

    with sqlite3.connect(str(fresh_engine._sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bids_json, asks_json, bid_depth_10, ask_depth_10 "
            "FROM market_snapshots WHERE token_id = ?",
            ("0xEMPTY",),
        ).fetchone()

    assert row is not None
    assert row["bids_json"] is None, "empty ladder must persist as NULL JSON"
    assert row["asks_json"] is None
    assert float(row["bid_depth_10"]) == pytest.approx(0.0)
    assert float(row["ask_depth_10"]) == pytest.approx(0.0)


# ────────────────────────────────────────────────────────────────────────────
# 3. get_order_book_depth returns the parsed ladders
# ────────────────────────────────────────────────────────────────────────────

async def test_get_order_book_depth_returns_parsed_ladders(
    fresh_db_manager: DatabaseManager,
    fresh_engine: TimescaleDBEngine,
):
    """``get_order_book_depth`` must return the latest snapshot's full
    bid/ask ladders (parsed from the JSON column) alongside the
    depth-10 summaries and the top-of-book fields.

    Setup: seed one snapshot with 15-level ladders. Call
    ``get_order_book_depth(token_id, limit=5)``. Assert the returned
    dict carries the top-5 ladder levels (not the full 15 — the
    ``limit`` param caps the ladder length in the response).
    """
    bids = _build_ladder(0.49, n=15)
    asks = _build_ladder(0.51, n=15)
    await fresh_engine.record_snapshot(
        token_id="0xGOD", slug="god-test",
        best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
        bids_json=bids, asks_json=asks,
    )

    depth = await fresh_db_manager.get_order_book_depth("0xGOD", limit=5)

    assert depth["token_id"] == "0xGOD"
    assert depth["timestamp"] is not None
    assert len(depth["bids"]) == 5, "limit=5 must cap the ladder at 5 levels"
    assert len(depth["asks"]) == 5

    # First bid level matches the seeded top-of-book.
    assert float(depth["bids"][0]["price"]) == pytest.approx(0.49)
    assert float(depth["bids"][0]["size"]) == pytest.approx(100.0)

    # Depth-10 summary carried through.
    assert float(depth["bid_depth_10"]) == pytest.approx(_expected_depth_10(bids))
    assert float(depth["ask_depth_10"]) == pytest.approx(_expected_depth_10(asks))

    # Top-of-book fields.
    assert float(depth["best_bid"]) == pytest.approx(0.49)
    assert float(depth["best_ask"]) == pytest.approx(0.51)
    assert float(depth["mid"]) == pytest.approx(0.50)
    assert float(depth["spread"]) == pytest.approx(0.02)

    # Backend label.
    assert depth["backend"] == "sqlite"


async def test_get_order_book_depth_returns_empty_payload_when_no_snapshots(
    fresh_db_manager: DatabaseManager,
):
    """``get_order_book_depth`` on a token with NO snapshots must return
    a well-formed "no data" payload rather than raising.

    The payload shape mirrors the populated case (same keys) but with
    empty ladders, zeroed summaries, and ``None`` for the
    top-of-book fields. The dashboard renders this as the "no data"
    state without an error boundary.
    """
    depth = await fresh_db_manager.get_order_book_depth("0xNONE")

    assert depth["token_id"] == "0xNONE"
    assert depth["timestamp"] is None
    assert depth["bids"] == []
    assert depth["asks"] == []
    assert float(depth["bid_depth_10"]) == pytest.approx(0.0)
    assert float(depth["ask_depth_10"]) == pytest.approx(0.0)
    assert float(depth["spread"]) == pytest.approx(0.0)
    assert float(depth["mid"]) == pytest.approx(0.5)
    assert depth["best_bid"] is None
    assert depth["best_ask"] is None
    assert depth["backend"] == "sqlite"


# ────────────────────────────────────────────────────────────────────────────
# 4. get_depth_history returns the time-windowed series
# ────────────────────────────────────────────────────────────────────────────

async def test_get_depth_history_returns_windowed_series_ascending(
    fresh_db_manager: DatabaseManager,
    fresh_engine: TimescaleDBEngine,
):
    """``get_depth_history`` must return every snapshot in the last
    ``hours`` window, ascending by timestamp (oldest first).

    Setup: seed 3 snapshots for the same token at 0s, 30s, 60s ago
    (with strictly-increasing timestamps so the SQLite ORDER BY is
    deterministic). Call ``get_depth_history(token_id, hours=1.0)``.
    Assert all 3 rows are returned, in ascending timestamp order.

    Belt-and-braces:
      * Each row carries the parsed top-10 ladders (``bids`` /
        ``asks`` keys, NOT just the raw JSON column).
      * The depth-10 summaries + top-of-book fields are present.
    """
    token_id = "0xHIST"
    base_ts = time.time() - 60.0  # 60s ago
    for i in range(3):
        ts = base_ts + i * 30.0  # 0s, 30s, 60s offsets
        bids = _build_ladder(0.49 + i * 0.001, n=12)
        asks = _build_ladder(0.51 + i * 0.001, n=12)
        # Patch time.time so ingestion_time and timestamp land at ts
        # — record_snapshot uses time.time() internally, so we patch.
        import core.timescale_db as _ts_mod
        original_time = _ts_mod.time.time
        _ts_mod.time.time = lambda: ts
        try:
            await fresh_engine.record_snapshot(
                token_id=token_id, slug="hist-test",
                best_bid=0.49 + i * 0.001, best_ask=0.51 + i * 0.001,
                mid=0.50 + i * 0.001, spread=0.02,
                bids_json=bids, asks_json=asks,
            )
        finally:
            _ts_mod.time.time = original_time
        await asyncio.sleep(0)  # yield

    history = await fresh_db_manager.get_depth_history(token_id, hours=1.0)

    assert len(history) == 3
    # Ascending by timestamp.
    timestamps = [row["timestamp"] for row in history]
    assert timestamps == sorted(timestamps), "history must be ascending by timestamp"

    # Each row carries parsed ladders (top-10 capped).
    for row in history:
        assert "bids" in row, "row must carry parsed bids (not raw JSON)"
        assert "asks" in row
        assert len(row["bids"]) <= 10, "history rows cap bids at 10 levels"
        assert len(row["asks"]) <= 10
        assert "bid_depth_10" in row
        assert "ask_depth_10" in row
        assert "mid" in row
        assert "spread" in row


async def test_get_depth_history_honours_hours_cutoff(
    fresh_db_manager: DatabaseManager,
    fresh_engine: TimescaleDBEngine,
):
    """``get_depth_history`` with a small ``hours`` window must filter
    out snapshots older than ``now - hours*3600``.

    Setup: seed one snapshot at ``now - 2 hours`` (outside the 1h
    window) and one at ``now`` (inside the window). Call
    ``get_depth_history(token_id, hours=1.0)``. Assert only the
    recent snapshot is returned.
    """
    token_id = "0xCUT"
    # Old snapshot — 2 hours ago, outside the 1h window.
    old_ts = time.time() - 2 * 3600.0
    import core.timescale_db as _ts_mod
    original_time = _ts_mod.time.time
    _ts_mod.time.time = lambda: old_ts
    try:
        await fresh_engine.record_snapshot(
            token_id=token_id, slug="cut-test",
            best_bid=0.40, best_ask=0.60, mid=0.50, spread=0.20,
            bids_json=_build_ladder(0.40, n=5),
            asks_json=_build_ladder(0.60, n=5),
        )
    finally:
        _ts_mod.time.time = original_time

    # Recent snapshot — now, inside the 1h window.
    await fresh_engine.record_snapshot(
        token_id=token_id, slug="cut-test",
        best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
        bids_json=_build_ladder(0.49, n=5),
        asks_json=_build_ladder(0.51, n=5),
    )

    history = await fresh_db_manager.get_depth_history(token_id, hours=1.0)
    assert len(history) == 1, "only the recent snapshot (within 1h) must be returned"
    # The recent snapshot's best_bid is 0.49 (not the old 0.40).
    assert float(history[0]["best_bid"]) == pytest.approx(0.49)


async def test_get_depth_history_returns_empty_list_for_unknown_token(
    fresh_db_manager: DatabaseManager,
):
    """``get_depth_history`` for a token with no snapshots must return
    an empty list (not raise)."""
    history = await fresh_db_manager.get_depth_history("0xUNKNOWN", hours=1.0)
    assert history == []


# ────────────────────────────────────────────────────────────────────────────
# 5. Legacy DB migration — schema upgrade adds the new columns
# ────────────────────────────────────────────────────────────────────────────

async def test_legacy_db_migration_adds_depth_columns(tmp_path: Path):
    """A legacy ``market_snapshots`` table created WITHOUT the W21-5
    depth columns must have them added idempotently when a fresh
    ``TimescaleDBEngine`` is constructed against the legacy DB file.

    Setup: hand-create a legacy SQLite DB with the pre-W21-5 schema
    (only the top-of-book columns). Construct a fresh
    ``TimescaleDBEngine`` pointing at the legacy file. Verify the
    table now has all 5 new columns (``bids_json``, ``asks_json``,
    ``bid_depth_10``, ``ask_depth_10``, ``ingestion_time``) and the
    pre-existing row is still there.

    Belt-and-braces: a subsequent ``record_snapshot`` call must
    succeed and write a row with all the new columns populated.
    """
    legacy_db = tmp_path / "legacy.db"
    # Pre-W21-5 schema — only the top-of-book columns.
    with sqlite3.connect(str(legacy_db)) as conn:
        conn.execute(
            """
            CREATE TABLE market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                token_id TEXT NOT NULL,
                slug TEXT,
                best_bid REAL,
                best_ask REAL,
                mid REAL,
                spread REAL,
                volume_24h REAL,
                liquidity REAL
            )
            """
        )
        conn.execute(
            "CREATE INDEX idx_snap_token ON market_snapshots(token_id, timestamp DESC)"
        )
        conn.execute(
            "INSERT INTO market_snapshots (timestamp, token_id, slug) "
            "VALUES (1.0, 'legacy-row', 'legacy')"
        )
        conn.commit()

    # Construct a fresh engine — the schema migration in
    # ``_init_sqlite_fallback`` must add the new columns idempotently.
    engine = TimescaleDBEngine(sqlite_path=legacy_db)
    engine._is_postgres = False

    with sqlite3.connect(str(legacy_db)) as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(market_snapshots)").fetchall()]
    for expected_col in (
        "bids_json", "asks_json", "bid_depth_10", "ask_depth_10", "ingestion_time",
    ):
        assert expected_col in cols, (
            f"legacy DB migration must add column {expected_col!r} (got {cols})"
        )

    # Pre-existing row is still there.
    with sqlite3.connect(str(legacy_db)) as conn:
        n_legacy = conn.execute(
            "SELECT COUNT(*) FROM market_snapshots WHERE token_id = 'legacy-row'"
        ).fetchone()[0]
    assert n_legacy == 1, "pre-migration rows must survive the schema upgrade"

    # A subsequent record_snapshot call must succeed and populate the
    # new columns (this is the regression test for the W19-5 fix that
    # W21-5 actually applies).
    ok = await engine.record_snapshot(
        token_id="0xPOSTMIG", slug="post-migration",
        best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
        bids_json=_build_ladder(0.49, n=5),
        asks_json=_build_ladder(0.51, n=5),
    )
    assert ok is True

    with sqlite3.connect(str(legacy_db)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bids_json, asks_json, bid_depth_10, ask_depth_10, ingestion_time "
            "FROM market_snapshots WHERE token_id = ?",
            ("0xPOSTMIG",),
        ).fetchone()
    assert row is not None
    assert row["bids_json"] is not None
    assert row["ingestion_time"] is not None
    assert float(row["bid_depth_10"]) == pytest.approx(_expected_depth_10(_build_ladder(0.49, n=5)))


# ────────────────────────────────────────────────────────────────────────────
# 6. book_poller._apply_book passes the full ladders through
# ────────────────────────────────────────────────────────────────────────────

async def test_book_poller_passes_ladders_to_record_snapshot(
    fresh_engine: TimescaleDBEngine,
    monkeypatch: pytest.MonkeyPatch,
):
    """``book_poller._apply_book`` must pass the full bid/ask ladders
    through to ``db_manager.record_snapshot`` (W26-3 — the W21-5
    call-site fix is now routed through the unified ``db_manager``
    facade rather than calling ``timescale_db.record_snapshot``
    directly, and the new ``bid_size`` / ``ask_size`` / ``volume`` /
    ``bid_depth_10`` / ``ask_depth_10`` kwargs are populated so the
    SQLite fallback INSERT writes real values into the depth columns
    rather than ``NULL`` / ``0``).

    Setup: build a stub CLOB ``/book`` payload with 3 bid levels and
    3 ask levels. Call ``_apply_book`` directly with the stub payload.
    Assert ``db_manager.record_snapshot`` was called with the
    ``bids_json`` / ``asks_json`` keyword arguments populated as JSON
    strings (the W26-3 contract — ``record_snapshot`` accepts
    ``Optional[str]``; the PG path JSON-parses them back to dicts
    before forwarding to ``timescale_db.record_snapshot``, the SQLite
    path writes them as-is into the ``bids_json`` / ``asks_json``
    TEXT columns). Also assert the depth-10 summaries + top-of-book
    sizes are computed correctly.

    The book poller's downstream singletons (``raw_vault``,
    ``source_registry``, ``store``, ``timescale_db``) are mocked so
    the call path doesn't trigger any other side effects.
    """
    from core.book_poller import BookPoller
    from core.data_store import store
    from core.database_manager import db_manager
    from core.timescale_db import timescale_db as ts_singleton

    poller = BookPoller()

    # Stub CLOB /book payload — 3 bid levels, 3 ask levels.
    book_payload = {
        "market": "0xPOLL",
        "asset_id": "0xPOLL",
        "bids": [
            {"price": "0.48", "size": "100"},
            {"price": "0.47", "size": "200"},
            {"price": "0.46", "size": "300"},
        ],
        "asks": [
            {"price": "0.52", "size": "150"},
            {"price": "0.53", "size": "250"},
            {"price": "0.54", "size": "350"},
        ],
        "hash": "0xdeadbeef",
        "timestamp": "1700000000000",
    }

    # Mock the downstream singletons.
    from unittest.mock import AsyncMock, MagicMock
    mock_rv = MagicMock()
    mock_rv.record_observation = AsyncMock(return_value=None)
    mock_sr = MagicMock()
    mock_sr.record_metric = AsyncMock(return_value=None)
    monkeypatch.setattr("core.ingestion.raw_vault.raw_vault", mock_rv)
    monkeypatch.setattr("core.ingestion.source_registry.source_registry", mock_sr)

    # ``record_tick`` is also fired-and-forgotten by ``_apply_book``.
    # Stub it on the timescale_db singleton so the fire-and-forget
    # coroutine completes without touching the fresh engine's tick
    # table (the fresh engine's schema doesn't carry the tick columns
    # the W21-3 ``record_tick`` SQLite path expects).
    mock_tick = AsyncMock(return_value=None)
    monkeypatch.setattr(ts_singleton, "record_tick", mock_tick)

    # Capture the ``db_manager.record_snapshot`` call args via a
    # wrapping mock. W26-3 — the book poller now routes through the
    # ``db_manager`` facade (not ``timescale_db.record_snapshot``
    # directly), so we capture on the singleton ``db_manager``
    # rather than on ``fresh_engine``. The wrapper delegates to the
    # real method so the SQLite row is actually persisted (so the
    # belt-and-braces SQLite assertion below still holds).
    captured: dict[str, Any] = {}
    real_record_snapshot = db_manager.record_snapshot

    async def capturing_record_snapshot(**kwargs):
        captured.update(kwargs)
        return await real_record_snapshot(**kwargs)

    monkeypatch.setattr(db_manager, "record_snapshot", capturing_record_snapshot)

    await poller._apply_book("0xPOLL", book_payload)
    # Drain the fire-and-forget ``asyncio.create_task`` calls so the
    # record_snapshot coroutine actually runs before assertions.
    await asyncio.sleep(0)

    # The full ladders were passed through as JSON strings.
    assert "bids_json" in captured, "bids_json kwarg must be passed"
    assert "asks_json" in captured
    assert captured["bids_json"] is not None, "bids_json must not be None"
    assert isinstance(captured["bids_json"], str), (
        "bids_json must be a JSON string (the W26-3 / W21-5 contract)"
    )

    parsed_bids = json.loads(captured["bids_json"])
    parsed_asks = json.loads(captured["asks_json"])
    assert len(parsed_bids) == 3, "all 3 bid levels must be passed"
    assert len(parsed_asks) == 3, "all 3 ask levels must be passed"

    # The ladder entries are ``{"price": float, "size": float}`` dicts
    # (the W26-3 / W21-5 conversion from ``PriceLevel`` dataclass
    # instances — the bids are sorted high → low, so the first entry
    # is the best bid at 0.48).
    first_bid = parsed_bids[0]
    assert isinstance(first_bid, dict)
    assert set(first_bid.keys()) == {"price", "size"}
    assert float(first_bid["price"]) == pytest.approx(0.48)
    assert float(first_bid["size"]) == pytest.approx(100.0)

    # W26-3 — the new ``bid_size`` / ``ask_size`` / ``volume`` /
    # ``bid_depth_10`` / ``ask_depth_10`` kwargs are populated so the
    # SQLite fallback INSERT writes real values into the depth columns.
    assert captured["bid_size"] == pytest.approx(100.0), (
        "bid_size must be the best bid's size (first ladder level)"
    )
    assert captured["ask_size"] == pytest.approx(150.0), (
        "ask_size must be the best ask's size (first ladder level)"
    )
    assert captured["volume"] == pytest.approx(0.0), (
        "volume defaults to 0 — the CLOB /book response carries no volume"
    )
    assert captured["bid_depth_10"] == pytest.approx(100.0 + 200.0 + 300.0), (
        "bid_depth_10 must be the sum of the top 10 bid sizes"
    )
    assert captured["ask_depth_10"] == pytest.approx(150.0 + 250.0 + 350.0), (
        "ask_depth_10 must be the sum of the top 10 ask sizes"
    )

    # Belt-and-braces: the snapshot row is actually persisted with
    # the ladder (the JSON column is populated in the SQLite fallback
    # — the wrapper above delegated to the real ``db_manager.record_snapshot``
    # which routed to ``_sqlite_record_snapshot`` and wrote to the
    # ``fresh_engine._sqlite_path`` the fixture re-pointed the
    # singleton at).
    with sqlite3.connect(str(fresh_engine._sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT bids_json, bid_size, bid_depth_10 FROM market_snapshots WHERE token_id = ?",
            ("0xPOLL",),
        ).fetchone()
    assert row is not None
    parsed = json.loads(row["bids_json"])
    assert len(parsed) == 3
    assert float(row["bid_size"]) == pytest.approx(100.0)
    assert float(row["bid_depth_10"]) == pytest.approx(600.0)


# ────────────────────────────────────────────────────────────────────────────
# 7. HTTP route — GET /api/depth-full/{token_id}
# ────────────────────────────────────────────────────────────────────────────

def test_depth_full_returns_200_with_empty_payload_initially(depth_client: TestClient):
    """``GET /api/depth-full/{token_id}`` on a fresh DB must return
    HTTP 200 with the well-formed "no data" payload — empty ladders,
    zeroed summaries, ``None`` for the top-of-book fields.

    The read path must NOT 500 on an empty table.
    """
    response = depth_client.get("/api/depth-full/0xNONE")
    assert response.status_code == 200
    body = response.json()
    assert body["token_id"] == "0xNONE"
    assert body["bids"] == []
    assert body["asks"] == []
    assert body["bid_depth_10"] == 0.0
    assert body["ask_depth_10"] == 0.0
    assert body["best_bid"] is None
    assert body["best_ask"] is None
    assert body["backend"] == "sqlite"


def test_depth_full_returns_parsed_ladder_for_seeded_snapshot(
    depth_client: TestClient,
    fresh_engine: TimescaleDBEngine,
):
    """``GET /api/depth-full/{token_id}`` after a snapshot is seeded
    must return HTTP 200 with the parsed bid/ask ladder (top ``limit``
    levels), the depth-10 summaries, and the top-of-book fields.

    Setup: seed a 15-level ladder via ``record_snapshot``. Call the
    endpoint with ``limit=5``. Assert the response carries the top-5
    levels (not the full 15).
    """
    bids = _build_ladder(0.49, n=15)
    asks = _build_ladder(0.51, n=15)

    async def _seed():
        await fresh_engine.record_snapshot(
            token_id="0xAPI", slug="api-test",
            best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
            bids_json=bids, asks_json=asks,
        )
    asyncio.run(_seed())

    response = depth_client.get("/api/depth-full/0xAPI", params={"limit": 5})
    assert response.status_code == 200
    body = response.json()

    assert body["token_id"] == "0xAPI"
    assert len(body["bids"]) == 5, "limit=5 must cap the ladder at 5 levels"
    assert len(body["asks"]) == 5
    assert float(body["bids"][0]["price"]) == pytest.approx(0.49)
    assert float(body["bids"][0]["size"]) == pytest.approx(100.0)
    assert float(body["bid_depth_10"]) == pytest.approx(_expected_depth_10(bids))
    assert float(body["ask_depth_10"]) == pytest.approx(_expected_depth_10(asks))
    assert float(body["best_bid"]) == pytest.approx(0.49)
    assert float(body["best_ask"]) == pytest.approx(0.51)
    assert body["backend"] == "sqlite"


def test_depth_full_limit_query_param_caps_ladder(
    depth_client: TestClient,
    fresh_engine: TimescaleDBEngine,
):
    """``GET /api/depth-full/{token_id}?limit=N`` must cap the ladder
    at N levels — even when the stored ladder has more levels.

    Setup: seed a 15-level ladder. Call with ``limit=3``. Assert the
    response carries exactly 3 bid levels and 3 ask levels.
    """
    async def _seed():
        await fresh_engine.record_snapshot(
            token_id="0xLIM", slug="limit-test",
            best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
            bids_json=_build_ladder(0.49, n=15),
            asks_json=_build_ladder(0.51, n=15),
        )
    asyncio.run(_seed())

    response = depth_client.get("/api/depth-full/0xLIM", params={"limit": 3})
    assert response.status_code == 200
    body = response.json()
    assert len(body["bids"]) == 3
    assert len(body["asks"]) == 3


def test_depth_full_limit_over_100_rejected_with_422(depth_client: TestClient):
    """``GET /api/depth-full/{token_id}?limit=101`` must be rejected
    with HTTP 422 — the ``Query(le=100)`` constraint caps the ladder
    length defensively (a 1000-level ladder would be a 100 KB JSON
    response, which is the upper bound for a single API call).
    """
    response = depth_client.get("/api/depth-full/0xX", params={"limit": 101})
    assert response.status_code == 422


# ────────────────────────────────────────────────────────────────────────────
# 8. HTTP route — GET /api/depth-history/{token_id}
# ────────────────────────────────────────────────────────────────────────────

def test_depth_history_returns_200_with_empty_list_initially(depth_client: TestClient):
    """``GET /api/depth-history/{token_id}`` on a fresh DB must return
    HTTP 200 with ``count=0`` and an empty ``history`` list.
    """
    response = depth_client.get("/api/depth-history/0xNONE")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["history"] == []
    assert body["token_id"] == "0xNONE"
    assert body["backend"] == "sqlite"


def test_depth_history_returns_seeded_rows_ascending(
    depth_client: TestClient,
    fresh_engine: TimescaleDBEngine,
):
    """``GET /api/depth-history/{token_id}`` must return every seeded
    snapshot in ASCENDING timestamp order (oldest first).

    Setup: seed 3 snapshots at strictly-increasing timestamps. Call
    the endpoint with ``hours=1``. Assert all 3 rows are returned,
    in ascending timestamp order, with the parsed top-10 ladders.
    """
    token_id = "0xHAPI"
    base_ts = time.time() - 60.0
    import core.timescale_db as _ts_mod

    async def _seed():
        for i in range(3):
            ts = base_ts + i * 30.0
            original_time = _ts_mod.time.time
            _ts_mod.time.time = lambda: ts
            try:
                await fresh_engine.record_snapshot(
                    token_id=token_id, slug="hapi-test",
                    best_bid=0.49 + i * 0.001, best_ask=0.51 + i * 0.001,
                    mid=0.50 + i * 0.001, spread=0.02,
                    bids_json=_build_ladder(0.49 + i * 0.001, n=12),
                    asks_json=_build_ladder(0.51 + i * 0.001, n=12),
                )
            finally:
                _ts_mod.time.time = original_time
    asyncio.run(_seed())

    response = depth_client.get("/api/depth-history/0xHAPI", params={"hours": 1.0})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    history = body["history"]
    assert len(history) == 3

    # Ascending by timestamp.
    timestamps = [row["timestamp"] for row in history]
    assert timestamps == sorted(timestamps)

    # Each row carries the parsed top-10 ladders.
    for row in history:
        assert "bids" in row
        assert "asks" in row
        assert len(row["bids"]) <= 10
        assert len(row["asks"]) <= 10


def test_depth_history_hours_query_param_filters_window(
    depth_client: TestClient,
    fresh_engine: TimescaleDBEngine,
):
    """``GET /api/depth-history/{token_id}?hours=0.001`` (3.6s window)
    must filter out any snapshot older than 3.6s ago.

    Setup: seed one snapshot at ``now - 10s`` (outside the window)
    and one at ``now`` (inside). Call with ``hours=0.001``. Assert
    only the recent snapshot is returned.
    """
    token_id = "0xHCUT"
    import core.timescale_db as _ts_mod

    # Old snapshot — 10s ago, outside the 0.001h (3.6s) window.
    old_ts = time.time() - 10.0
    async def _seed_old():
        original_time = _ts_mod.time.time
        _ts_mod.time.time = lambda: old_ts
        try:
            await fresh_engine.record_snapshot(
                token_id=token_id, slug="hcut-test",
                best_bid=0.40, best_ask=0.60, mid=0.50, spread=0.20,
                bids_json=_build_ladder(0.40, n=3),
                asks_json=_build_ladder(0.60, n=3),
            )
        finally:
            _ts_mod.time.time = original_time
    asyncio.run(_seed_old())

    # Recent snapshot — now, inside the window.
    async def _seed_recent():
        await fresh_engine.record_snapshot(
            token_id=token_id, slug="hcut-test",
            best_bid=0.49, best_ask=0.51, mid=0.50, spread=0.02,
            bids_json=_build_ladder(0.49, n=3),
            asks_json=_build_ladder(0.51, n=3),
        )
    asyncio.run(_seed_recent())

    response = depth_client.get("/api/depth-history/0xHCUT", params={"hours": 0.001})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1, "only the recent snapshot (within 3.6s) must be returned"
    assert float(body["history"][0]["best_bid"]) == pytest.approx(0.49)


# ────────────────────────────────────────────────────────────────────────────
# 9. Module-level singleton contract
# ────────────────────────────────────────────────────────────────────────────

def test_database_manager_singleton_contract():
    """The module-level singletons ``db_manager`` and ``database_manager``
    must be the SAME instance (the W21-1 / W21-2 names point to one
    object so the lifespan startup's
    ``await database_manager.initialize()`` followed by
    ``await db_manager.initialize()`` is idempotent).

    Also verifies the ``DatabaseBackend`` enum exposes the three
    expected values (``POSTGRESQL`` / ``SQLITE`` / ``NONE``) so the
    W21-1 ``retry-pg`` endpoint can construct
    ``DatabaseBackend.POSTGRESQL`` for the status transition.
    """
    assert database_manager is db_manager, (
        "database_manager and db_manager must be the SAME singleton instance "
        "(two names, one object — the lifespan startup calls both initialize())"
    )
    assert isinstance(db_manager, DatabaseManager)
    assert DatabaseBackend.POSTGRESQL.value == "postgresql"
    assert DatabaseBackend.SQLITE.value == "sqlite"
    assert DatabaseBackend.NONE.value == "none"


def test_db_manager_get_status_returns_well_formed_payload():
    """``db_manager.get_status()`` must return a JSON-able dict with the
    expected keys (the ``GET /api/database/status`` endpoint returns
    this dict directly)."""
    status = db_manager.get_status()
    assert isinstance(status, dict)
    for key in (
        "backend", "pg_available", "sqlite_available",
        "last_pg_check", "last_pg_check_ago_s",
        "retry_interval_s", "fallback_count", "recent_errors",
    ):
        assert key in status, f"status dict must carry key {key!r}"
    assert status["recent_errors"] == [] or isinstance(status["recent_errors"], list)
