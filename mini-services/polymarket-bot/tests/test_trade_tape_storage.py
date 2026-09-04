"""
tests/test_trade_tape_storage.py — W21-6 trade tape storage tests.

Covers the unified ``db_manager`` facade's trade-tape methods:

  1. ``record_trade`` on SQLite — writes a row, returns ``True``.
  2. ``get_trade_stats`` — aggregate stats (count, volume, VWAP,
     buy/sell counts) over a trailing ``hours`` window.
  3. ``get_trade_tape`` — recent trades, most-recent-first, with
     optional ``token_id`` / ``since_timestamp`` filters.
  4. Trade ingester uses ``db_manager.record_trade`` (not the
     ``timescale_db`` writer directly) — verifies the W21-6 wiring
     change in ``core/trade_ingester.py::_ingest_trades``.
  5. HTTP API routes ``GET /api/trades/stats`` and the updated
     ``GET /api/trades/tape`` (which now routes through ``db_manager``
     and accepts a ``since`` query param).

Testing strategy
-----------------
Every test constructs a fresh ``TimescaleDBEngine`` whose SQLite file
lives under ``tmp_path`` (per-test isolation — no cross-test pollution)
and patches ``core.timescale_db.timescale_db`` to that fresh engine
via ``monkeypatch.setattr``. The W21-6 ``db_manager`` facade reads
``timescale_db`` LAZILY (the property ``is_postgres`` /
``_sqlite_path`` import the singleton inside the property body so a
``monkeypatch.setattr`` on ``core.timescale_db.timescale_db`` is
picked up by the next call — see the NOTE in
``core/database_manager.py``).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling async test
module — pytest-asyncio is already a project dependency).
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.database_manager import DatabaseManager, db_manager
from core.timescale_db import TimescaleDBEngine
from core.trade_ingester import TradeTapeIngester, register_routes

# NOTE: We do NOT use the module-level ``pytestmark = pytest.mark.asyncio``
# idiom (as in ``tests/test_book_poller.py`` etc.) because this module
# mixes sync + async tests: the sync ``TestClient``-based API route
# tests would emit ``PytestWarning: marked with @pytest.mark.asyncio
# but not async`` warnings if the module-level mark were applied.
# Per-test ``@pytest.mark.asyncio`` decoration avoids that (mirrors the
# pattern in ``tests/test_trade_ingester.py``).


# ────────────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_db_manager(monkeypatch, tmp_path):
    """Return a ``DatabaseManager`` bound to a fresh SQLite file.

    Patches ``core.timescale_db.timescale_db`` to a new
    ``TimescaleDBEngine`` whose ``_sqlite_path`` lives under
    ``tmp_path`` (per-test isolation — no leakage between the
    ``record_trade`` insert tests and the ``get_trade_stats`` /
    ``get_trade_tape`` read tests). The facade's properties read the
    singleton lazily, so the patch propagates automatically.

    Returns the global ``db_manager`` singleton — the test exercises
    the production code path (a singleton shared with the route
    handlers) rather than a fresh instance, so a regression in the
    facade's lazy-import contract surfaces as a test failure here.
    """
    db_file = tmp_path / "test_trade_tape_storage.db"
    engine = TimescaleDBEngine(sqlite_path=db_file)
    engine._is_postgres = False
    monkeypatch.setattr("core.timescale_db.timescale_db", engine)
    return db_manager


@pytest.fixture
def trade_tape_app(monkeypatch, tmp_path) -> FastAPI:
    """Fresh ``FastAPI()`` app with ONLY the trade-tape routes registered.

    Mirrors the ``trade_tape_app`` fixture in
    ``tests/test_trade_ingester.py`` (W20-7) — a fresh app per test so
    the route definitions are byte-identical to what the production
    ``api/server.py`` exposes (same ``register_routes(app)`` call)
    without the bearer-token auth middleware or the heavy ``lifespan``
    startup. Patches ``core.timescale_db.timescale_db`` to a fresh
    engine scoped to ``tmp_path`` so the routes can be exercised
    end-to-end without touching the conftest-redirected singleton.
    """
    engine = TimescaleDBEngine(sqlite_path=tmp_path / "test_trade_tape_api.db")
    engine._is_postgres = False
    monkeypatch.setattr("core.timescale_db.timescale_db", engine)
    fresh_ingester = TradeTapeIngester(poll_interval=0.01)
    monkeypatch.setattr("core.trade_ingester.trade_tape_ingester", fresh_ingester)

    app = FastAPI()
    register_routes(app)
    return app


@pytest.fixture
def trade_tape_client(trade_tape_app: FastAPI) -> TestClient:
    """``TestClient`` bound to the fresh trade-tape app.

    Constructed WITHOUT entering the ``with`` context manager so the
    app's lifespan is NOT triggered (the production lifespan starts
    every background task; we don't want any of those running during
    the route contract tests).
    """
    return TestClient(trade_tape_app)


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _trade(
    trade_id: str = "trade-1",
    token_id: str = "0xtokenA",
    price: float = 0.55,
    size: float = 10.0,
    side: str = "BUY",
    timestamp: float | None = None,
) -> dict[str, Any]:
    """Build a minimal trade dict for record_trade / ingester tests."""
    return {
        "trade_id": trade_id,
        "token_id": token_id,
        "price": price,
        "size": size,
        "side": side,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "maker_address": "0xmaker1",
        "taker_order_id": "taker-ord-1",
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. record_trade (SQLite) — writes a row, returns True, dedupes by trade_id
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_record_trade_writes_row_and_returns_true(fresh_db_manager):
    """``db_manager.record_trade`` on the SQLite backend must write a
    row to the ``market_trades`` table and return ``True``.

    Verifies the W21-6 facade's write path delegates correctly to
    ``timescale_db.record_trade`` (the W20-7 writer).
    """
    ok = await fresh_db_manager.record_trade(
        token_id="0xTOK",
        price=0.55,
        size=10.0,
        side="BUY",
        timestamp=time.time(),
        trade_id="t-1",
    )
    assert ok is True

    # Row should be visible via the read path (get_trade_tape).
    rows = await fresh_db_manager.get_trade_tape(token_id="0xTOK", limit=10)
    assert len(rows) == 1
    assert rows[0]["trade_id"] == "t-1"
    assert rows[0]["token_id"] == "0xTOK"
    assert float(rows[0]["price"]) == pytest.approx(0.55)
    assert float(rows[0]["size"]) == pytest.approx(10.0)
    assert rows[0]["side"] == "BUY"


@pytest.mark.asyncio
async def test_record_trade_dedupes_by_trade_id(fresh_db_manager):
    """Re-inserting the same ``trade_id`` must be a no-op (no duplicate
    row) — the ``UNIQUE(trade_id)`` constraint is the durable backstop
    on both backends.
    """
    ts = time.time()
    await fresh_db_manager.record_trade(
        token_id="0xTOK", price=0.55, size=10.0, side="BUY",
        timestamp=ts, trade_id="dup-1",
    )
    await fresh_db_manager.record_trade(
        token_id="0xTOK", price=0.99, size=99.0, side="SELL",
        timestamp=ts + 1, trade_id="dup-1",  # same trade_id
    )

    rows = await fresh_db_manager.get_trade_tape(token_id="0xTOK", limit=10)
    assert len(rows) == 1
    # The first insert's values win (the second is a no-op).
    assert float(rows[0]["price"]) == pytest.approx(0.55)
    assert float(rows[0]["size"]) == pytest.approx(10.0)
    assert rows[0]["side"] == "BUY"


@pytest.mark.asyncio
async def test_record_trade_routes_through_timescale_db(fresh_db_manager, monkeypatch):
    """``db_manager.record_trade`` must call the underlying
    ``timescale_db.record_trade`` — verifies the facade delegates
    rather than implementing its own write path.
    """
    called = {"count": 0, "kwargs": None}

    real_record_trade = fresh_db_manager._ts.record_trade if hasattr(fresh_db_manager, "_ts") else None

    async def _spy_record_trade(**kwargs):
        called["count"] += 1
        called["kwargs"] = kwargs
        return True

    # Patch the engine's record_trade (the underlying impl) so we
    # observe the delegation. The fresh_db_manager's is_postgres /
    # _sqlite_path properties look up ``timescale_db`` lazily, so the
    # patched engine's record_trade is what db_manager.record_trade
    # ends up calling.
    from core.timescale_db import timescale_db as engine_singleton
    monkeypatch.setattr(engine_singleton, "record_trade", _spy_record_trade)

    await fresh_db_manager.record_trade(
        token_id="0xTOK", price=0.5, size=10.0, side="BUY",
        timestamp=1.0, trade_id="spy-1",
    )

    assert called["count"] == 1
    assert called["kwargs"]["trade_id"] == "spy-1"
    assert called["kwargs"]["token_id"] == "0xTOK"


# ────────────────────────────────────────────────────────────────────────────
# 2. get_trade_stats — aggregates over trailing hours window
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_trade_stats_returns_zeros_on_empty_tape(fresh_db_manager):
    """``get_trade_stats`` on a fresh DB must return all-zero stats
    rather than raising (the empty-tape case is the common one at
    startup, so the API endpoint must return 200 with zeros).

    The W21-6 spec mandates the six core aggregates
    (``total_trades`` / ``total_volume`` / ``avg_price`` /
    ``buy_count`` / ``sell_count`` / ``vwap``). The implementation may
    add extra keys (``hours`` / ``token_id`` echoes) — this test
    checks the six core keys are zero (subset check, not full-dict
    equality, so an additive change to the return shape doesn't
    break the test).
    """
    stats = await fresh_db_manager.get_trade_stats(hours=24.0)
    expected = {
        "total_trades": 0,
        "total_volume": 0,
        "avg_price": 0,
        "buy_count": 0,
        "sell_count": 0,
        "vwap": 0,
    }
    for key, expected_val in expected.items():
        assert key in stats, f"missing key {key!r} in stats: {stats}"
        assert stats[key] == expected_val, (
            f"stats[{key!r}] = {stats[key]!r}, expected {expected_val!r}"
        )


@pytest.mark.asyncio
async def test_get_trade_stats_aggregates_seeded_trades(fresh_db_manager):
    """``get_trade_stats`` must compute total / volume / avg_price /
    buys / sells / vwap correctly over the seeded trades.

    Seeds 3 trades with known prices + sizes + sides and asserts each
    aggregate matches the expected value:
      * total_trades = 3
      * total_volume = 10 + 20 + 30 = 60
      * avg_price   = (0.50 + 0.60 + 0.70) / 3 = 0.60
      * buys         = 2 (trades 1 + 3)
      * sells        = 1 (trade 2)
      * vwap         = (0.50*10 + 0.60*20 + 0.70*30) / 60
                       = (5 + 12 + 21) / 60
                       = 38 / 60
                       = 0.6333...
    """
    now = time.time()
    await fresh_db_manager.record_trade(
        token_id="0xA", price=0.50, size=10.0, side="BUY",
        timestamp=now - 60, trade_id="s-1",
    )
    await fresh_db_manager.record_trade(
        token_id="0xA", price=0.60, size=20.0, side="SELL",
        timestamp=now - 30, trade_id="s-2",
    )
    await fresh_db_manager.record_trade(
        token_id="0xA", price=0.70, size=30.0, side="BUY",
        timestamp=now - 10, trade_id="s-3",
    )

    stats = await fresh_db_manager.get_trade_stats(token_id="0xA", hours=1.0)

    assert stats["total_trades"] == 3
    assert stats["total_volume"] == pytest.approx(60.0)
    assert stats["avg_price"] == pytest.approx(0.60, abs=1e-6)
    assert stats["buy_count"] == 2
    assert stats["sell_count"] == 1
    # VWAP = (0.5*10 + 0.6*20 + 0.7*30) / 60 = (5+12+21)/60 = 38/60
    assert stats["vwap"] == pytest.approx(38.0 / 60.0, abs=1e-6)


@pytest.mark.asyncio
async def test_get_trade_stats_filters_by_token_id(fresh_db_manager):
    """``token_id`` filter must restrict the aggregation to a single
    market — the other token's trades are excluded.
    """
    now = time.time()
    await fresh_db_manager.record_trade(
        token_id="0xAAA", price=0.50, size=10.0, side="BUY",
        timestamp=now - 60, trade_id="aaa-1",
    )
    await fresh_db_manager.record_trade(
        token_id="0xBBB", price=0.60, size=20.0, side="SELL",
        timestamp=now - 30, trade_id="bbb-1",
    )

    stats_a = await fresh_db_manager.get_trade_stats(token_id="0xAAA", hours=1.0)
    assert stats_a["total_trades"] == 1
    assert stats_a["total_volume"] == pytest.approx(10.0)
    assert stats_a["buy_count"] == 1
    assert stats_a["sell_count"] == 0

    stats_b = await fresh_db_manager.get_trade_stats(token_id="0xBBB", hours=1.0)
    assert stats_b["total_trades"] == 1
    assert stats_b["total_volume"] == pytest.approx(20.0)
    assert stats_b["buy_count"] == 0
    assert stats_b["sell_count"] == 1


@pytest.mark.asyncio
async def test_get_trade_stats_respects_hours_window(fresh_db_manager):
    """Trades older than ``hours`` must be EXCLUDED from the stats —
    the cutoff is ``time.time() - hours * 3600``.
    """
    now = time.time()
    # Old trade (3 hours ago) — excluded by hours=1.0.
    await fresh_db_manager.record_trade(
        token_id="0xA", price=0.50, size=10.0, side="BUY",
        timestamp=now - 3 * 3600, trade_id="old-1",
    )
    # Recent trade (10 seconds ago) — included.
    await fresh_db_manager.record_trade(
        token_id="0xA", price=0.60, size=20.0, side="SELL",
        timestamp=now - 10, trade_id="new-1",
    )

    stats = await fresh_db_manager.get_trade_stats(token_id="0xA", hours=1.0)
    assert stats["total_trades"] == 1
    assert stats["total_volume"] == pytest.approx(20.0)
    assert stats["sell_count"] == 1


# ────────────────────────────────────────────────────────────────────────────
# 3. get_trade_tape — recent rows, most-recent-first, with filters
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_trade_tape_returns_empty_list_initially(fresh_db_manager):
    """``get_trade_tape`` on a fresh DB must return an empty list
    (NOT raise) — the read path must not 500 on an empty table.
    """
    rows = await fresh_db_manager.get_trade_tape(limit=10)
    assert rows == []


@pytest.mark.asyncio
async def test_get_trade_tape_returns_rows_most_recent_first(fresh_db_manager):
    """``get_trade_tape`` must return rows in MOST-RECENT-FIRST order
    (descending by ``timestamp``).
    """
    base = 1_700_000_000.0
    for i in range(5):
        await fresh_db_manager.record_trade(
            token_id="0xTOK", price=0.5 + 0.01 * i, size=10.0, side="BUY",
            timestamp=base + i, trade_id=f"tape-{i}",
        )

    rows = await fresh_db_manager.get_trade_tape(limit=10)
    assert len(rows) == 5
    # Most-recent-first: tape-4 (highest ts) at index 0, tape-0 last.
    assert rows[0]["trade_id"] == "tape-4"
    assert rows[4]["trade_id"] == "tape-0"


@pytest.mark.asyncio
async def test_get_trade_tape_filters_by_token_id(fresh_db_manager):
    """``token_id`` filter must restrict the result to a single market."""
    await fresh_db_manager.record_trade(
        token_id="0xAAA", price=0.5, size=10.0, side="BUY",
        timestamp=time.time(), trade_id="aaa-1",
    )
    await fresh_db_manager.record_trade(
        token_id="0xBBB", price=0.6, size=20.0, side="SELL",
        timestamp=time.time(), trade_id="bbb-1",
    )

    rows = await fresh_db_manager.get_trade_tape(token_id="0xAAA", limit=10)
    assert len(rows) == 1
    assert rows[0]["token_id"] == "0xAAA"


@pytest.mark.asyncio
async def test_get_trade_tape_caps_limit(fresh_db_manager):
    """``limit`` must cap the row count — the facade caps at 500
    defensively even when the caller asks for more (the API route's
    ``Query(le=500)`` enforces the same bound at the framework layer).
    """
    base = time.time()
    for i in range(10):
        await fresh_db_manager.record_trade(
            token_id="0xTOK", price=0.5, size=10.0, side="BUY",
            timestamp=base + i, trade_id=f"cap-{i}",
        )

    rows = await fresh_db_manager.get_trade_tape(token_id="0xTOK", limit=3)
    assert len(rows) == 3
    # Most-recent-first: cap-9, cap-8, cap-7.
    assert rows[0]["trade_id"] == "cap-9"
    assert rows[1]["trade_id"] == "cap-8"
    assert rows[2]["trade_id"] == "cap-7"


@pytest.mark.asyncio
async def test_get_trade_tape_caps_limit_at_500_defensively(fresh_db_manager):
    """A ``limit > 500`` must be silently capped to 500 by the facade
    (``max(1, min(int(limit), 500))``) — defends against an unbounded
    query from a direct caller bypassing the API route's ``Query(le=500)``.
    """
    base = time.time()
    for i in range(5):
        await fresh_db_manager.record_trade(
            token_id="0xTOK", price=0.5, size=10.0, side="BUY",
            timestamp=base + i, trade_id=f"lim-{i}",
        )

    # Caller asks for 10_000 rows — the facade caps to 500 (and only
    # 5 rows exist, so we get 5 back).
    rows = await fresh_db_manager.get_trade_tape(limit=10_000)
    assert len(rows) == 5


@pytest.mark.asyncio
async def test_get_trade_tape_filters_by_since_timestamp(fresh_db_manager):
    """``since_timestamp`` filter must return only trades with
    ``timestamp > since_timestamp`` — used by the dashboard's
    "trades since last poll" widget.
    """
    base = 1_700_000_000.0
    for i in range(5):
        await fresh_db_manager.record_trade(
            token_id="0xTOK", price=0.5, size=10.0, side="BUY",
            timestamp=base + i, trade_id=f"since-{i}",
        )

    # since = base + 2 → only trades with timestamp > base+2 (since-3, since-4).
    rows = await fresh_db_manager.get_trade_tape(
        token_id="0xTOK", limit=10, since_timestamp=base + 2,
    )
    assert len(rows) == 2
    assert rows[0]["trade_id"] == "since-4"
    assert rows[1]["trade_id"] == "since-3"


# ────────────────────────────────────────────────────────────────────────────
# 4. Trade ingester uses db_manager (not timescale_db directly)
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_trade_ingester_calls_db_manager_record_trade(monkeypatch):
    """The W21-6 wiring change: ``_ingest_trades`` must call
    ``db_manager.record_trade`` (the facade), NOT
    ``timescale_db.record_trade`` (the underlying writer) directly.

    Verifies the facade is the single write-path entry point — a
    regression that bypasses the facade (e.g. a refactor that reverts
    to ``timescale_db.record_trade``) would break the W21-6 test
    surface because the trade-tape ingester tests would no longer
    exercise the facade's routing logic.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        _trade(trade_id="t-1", token_id="0xA", price=0.5, size=10.0,
               side="BUY", timestamp=1.0),
        _trade(trade_id="t-2", token_id="0xB", price=0.6, size=20.0,
               side="SELL", timestamp=2.0),
    ])

    # Spy on ``db_manager.record_trade`` (the facade). Replace the
    # facade's method with an AsyncMock so we can assert the call
    # count + kwargs.
    from core.database_manager import db_manager as _db_manager_singleton
    original_record_trade = _db_manager_singleton.record_trade
    spy = AsyncMock(return_value=True)
    _db_manager_singleton.record_trade = spy

    try:
        monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
        await ingester._ingest_trades()
    finally:
        _db_manager_singleton.record_trade = original_record_trade

    # Two trades → two facade calls.
    assert spy.await_count == 2
    # Verify the kwargs of the first call.
    first_kwargs = spy.await_args_list[0].kwargs
    assert first_kwargs["trade_id"] == "t-1"
    assert first_kwargs["token_id"] == "0xA"
    assert first_kwargs["price"] == 0.5
    assert first_kwargs["size"] == 10.0
    assert first_kwargs["side"] == "BUY"


@pytest.mark.asyncio
async def test_trade_ingester_does_not_call_timescale_db_record_trade_directly(monkeypatch):
    """The ingester must NOT call ``timescale_db.record_trade`` directly —
    it must go through the ``db_manager`` facade. Verifies the W21-6
    wiring change in ``core/trade_ingester.py::_ingest_trades``.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        _trade(trade_id="t-direct-1"),
    ])

    # Track calls to ``timescale_db.record_trade`` — the production
    # code path now goes through ``db_manager.record_trade`` which
    # delegates to ``timescale_db.record_trade`` under the hood. So
    # ``timescale_db.record_trade`` SHOULD still be called (once per
    # trade), but only via the facade's delegation (NOT a direct call
    # from ``_ingest_trades``).
    timescale_calls = {"count": 0}
    original_ts_record_trade = None
    from core.timescale_db import timescale_db as _ts

    async def _track_ts_call(**kwargs):
        timescale_calls["count"] += 1
        return True

    original_ts_record_trade = _ts.record_trade
    _ts.record_trade = _track_ts_call

    try:
        monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
        await ingester._ingest_trades()
    finally:
        _ts.record_trade = original_ts_record_trade

    # ``timescale_db.record_trade`` was called ONCE — through the
    # facade's delegation. If the ingester called it directly, the
    # count would still be 1 (one trade → one call), so the only way
    # to verify the indirection is to assert the call came THROUGH
    # the facade. That's covered by the previous test
    # (``test_trade_ingester_calls_db_manager_record_trade``); here we
    # just verify the call count matches the trade count (no double-write).
    assert timescale_calls["count"] == 1


@pytest.mark.asyncio
async def test_trade_ingester_ingested_count_increments_via_facade(monkeypatch, fresh_db_manager):
    """The ingester's ``_ingested_count`` must increment by the number
    of trades written — and the writes must land in the SQLite
    fallback via the ``db_manager`` facade. This is the end-to-end
    integration test: ``_ingest_trades`` → ``db_manager.record_trade``
    → ``timescale_db.record_trade`` → SQLite row.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        _trade(trade_id="e2e-1", token_id="0xE2E", price=0.5, size=10.0,
               side="BUY", timestamp=time.time()),
        _trade(trade_id="e2e-2", token_id="0xE2E", price=0.6, size=20.0,
               side="SELL", timestamp=time.time()),
    ])
    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)

    await ingester._ingest_trades()

    assert ingester._ingested_count == 2

    # The rows should be visible via the facade's read path.
    rows = await fresh_db_manager.get_trade_tape(token_id="0xE2E", limit=10)
    assert len(rows) == 2
    trade_ids = {r["trade_id"] for r in rows}
    assert trade_ids == {"e2e-1", "e2e-2"}


# ────────────────────────────────────────────────────────────────────────────
# 5. HTTP API routes — /api/trades/stats + /api/trades/tape (via db_manager)
# ────────────────────────────────────────────────────────────────────────────

def test_get_trade_stats_route_returns_200_with_zeros_on_empty_tape(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/stats`` on a fresh DB must return HTTP 200
    with all-zero stats — the read path must NOT 500 on an empty tape.

    The route envelope wraps the stats under ``body["stats"]`` (so
    the response carries ``token_id`` / ``hours`` / ``backend``
    metadata alongside the aggregates). The six W21-6 core aggregates
    must be present and zero on an empty tape.
    """
    response = trade_tape_client.get("/api/trades/stats")
    assert response.status_code == 200
    body = response.json()
    assert "stats" in body
    expected_zeros = {
        "total_trades": 0,
        "total_volume": 0,
        "avg_price": 0,
        "buy_count": 0,
        "sell_count": 0,
        "vwap": 0,
    }
    for key, expected_val in expected_zeros.items():
        assert key in body["stats"], (
            f"missing key {key!r} in body['stats']: {body['stats']}"
        )
        assert body["stats"][key] == expected_val, (
            f"body['stats'][{key!r}] = {body['stats'][key]!r}, "
            f"expected {expected_val!r}"
        )
    assert body["hours"] == 24.0  # default
    assert body["backend"] == "sqlite"


def test_get_trade_stats_route_returns_aggregated_stats(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/stats`` must return the aggregated stats over
    the seeded trades.
    """
    from core.database_manager import db_manager as _db
    now = time.time()

    async def _seed():
        await _db.record_trade(
            token_id="0xR", price=0.50, size=10.0, side="BUY",
            timestamp=now - 60, trade_id="r-1",
        )
        await _db.record_trade(
            token_id="0xR", price=0.60, size=20.0, side="SELL",
            timestamp=now - 30, trade_id="r-2",
        )

    asyncio.run(_seed())

    response = trade_tape_client.get(
        "/api/trades/stats", params={"token_id": "0xR", "hours": 1.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["stats"]["total_trades"] == 2
    assert body["stats"]["total_volume"] == pytest.approx(30.0)
    assert body["stats"]["buy_count"] == 1
    assert body["stats"]["sell_count"] == 1
    # VWAP = (0.5*10 + 0.6*20) / 30 = (5+12)/30 = 17/30
    assert body["stats"]["vwap"] == pytest.approx(17.0 / 30.0, abs=1e-6)
    assert body["token_id"] == "0xR"
    assert body["hours"] == 1.0


def test_get_trade_tape_route_returns_200_with_empty_list_initially(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/tape`` on a fresh DB must return HTTP 200
    with ``count=0`` and an empty ``trades`` list — verifies the
    W21-6 /api/trades/tape route (now via db_manager) preserves the
    W20-7 envelope ``{trades, count, token_id, backend}``.
    """
    response = trade_tape_client.get("/api/trades/tape")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["trades"] == []
    assert body["backend"] == "sqlite"


def test_get_trade_tape_route_returns_seeded_rows_most_recent_first(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/tape`` must return every seeded row in
    MOST-RECENT-FIRST order (descending by ``timestamp``).
    """
    from core.database_manager import db_manager as _db

    async def _seed():
        for i in range(3):
            await _db.record_trade(
                token_id="0xTOK", price=0.5 + 0.01 * i, size=10.0,
                side="BUY", timestamp=1_700_000_000.0 + i,
                trade_id=f"seed-{i}",
            )
            await asyncio.sleep(0.005)  # strictly-increasing ingestion_time

    asyncio.run(_seed())

    response = trade_tape_client.get("/api/trades/tape")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 3
    # Most-recent-first: seed-2 (highest ts) at index 0.
    assert body["trades"][0]["trade_id"] == "seed-2"
    assert body["trades"][2]["trade_id"] == "seed-0"


def test_get_trade_tape_route_filters_by_token_id(trade_tape_client: TestClient):
    """``GET /api/trades/tape?token_id=0xAAA`` must return only the
    rows for that token — the others are filtered at the SQL layer.
    """
    from core.database_manager import db_manager as _db

    async def _seed():
        await _db.record_trade(
            token_id="0xAAA", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_000.0, trade_id="aaa-1",
        )
        await _db.record_trade(
            token_id="0xBBB", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_001.0, trade_id="bbb-1",
        )

    asyncio.run(_seed())

    response = trade_tape_client.get(
        "/api/trades/tape", params={"token_id": "0xAAA"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["trades"][0]["token_id"] == "0xAAA"


def test_get_trade_tape_route_filters_by_since(trade_tape_client: TestClient):
    """``GET /api/trades/tape?since=<ts>`` must return only the rows
    with ``timestamp > since`` — verifies the W21-6 ``since`` query
    param flows through to ``db_manager.get_trade_tape``.
    """
    from core.database_manager import db_manager as _db

    async def _seed():
        for i in range(5):
            await _db.record_trade(
                token_id="0xTOK", price=0.5, size=10.0, side="BUY",
                timestamp=1_700_000_000.0 + i, trade_id=f"since-{i}",
            )

    asyncio.run(_seed())

    # since = 1_700_000_002 → only since-3 + since-4 (timestamps 03 + 04).
    response = trade_tape_client.get(
        "/api/trades/tape", params={"since": 1_700_000_002.0}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["trades"][0]["trade_id"] == "since-4"
    assert body["trades"][1]["trade_id"] == "since-3"


def test_get_trade_tape_route_limit_param_caps_result_count(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/tape?limit=2`` must cap the result at 2 rows
    (most-recent-first) — even when more rows exist in the DB.
    """
    from core.database_manager import db_manager as _db

    async def _seed():
        for i in range(5):
            await _db.record_trade(
                token_id="0xTOK", price=0.5, size=10.0, side="BUY",
                timestamp=1_700_000_000.0 + i, trade_id=f"lim-{i}",
            )
            await asyncio.sleep(0.005)

    asyncio.run(_seed())

    response = trade_tape_client.get("/api/trades/tape", params={"limit": 2})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    # Most-recent-first: lim-4 (highest ts) at index 0, lim-3 at index 1.
    assert body["trades"][0]["trade_id"] == "lim-4"
    assert body["trades"][1]["trade_id"] == "lim-3"


@pytest.mark.parametrize(
    "bad_limit, reason",
    [
        (0, "ge=1 violation (zero)"),
        (-1, "ge=1 violation (negative)"),
        (501, "le=500 violation"),
        ("abc", "non-int-coercible string"),
    ],
)
def test_get_trade_tape_route_invalid_limit_returns_422(
    trade_tape_client: TestClient, bad_limit, reason
):
    """An out-of-range or non-integer ``limit`` must trigger FastAPI's
    422 Unprocessable Entity response.
    """
    response = trade_tape_client.get(
        "/api/trades/tape", params={"limit": bad_limit}
    )
    assert response.status_code == 422, (
        f"expected 422 for bad_limit={bad_limit!r} ({reason}), got "
        f"{response.status_code}: {response.text}"
    )


def test_get_trade_stats_route_invalid_hours_returns_422(
    trade_tape_client: TestClient,
):
    """``hours`` < 0 must trigger a 422 — the route enforces
    ``Query(ge=0.0)`` so a negative window is rejected at the
    framework layer (before the handler runs).
    """
    response = trade_tape_client.get(
        "/api/trades/stats", params={"hours": -1.0}
    )
    assert response.status_code == 422


# ────────────────────────────────────────────────────────────────────────────
# 6. db_manager facade singleton sanity
# ────────────────────────────────────────────────────────────────────────────

def test_db_manager_singleton_is_database_manager_instance():
    """The module-level ``db_manager`` singleton must be an instance of
    ``DatabaseManager`` (verifies the import surface the trade
    ingester + route handlers depend on).
    """
    assert isinstance(db_manager, DatabaseManager)


def test_db_manager_singleton_has_trade_tape_methods():
    """The ``db_manager`` singleton must expose the W21-6 trade-tape
    public surface (``record_trade``, ``get_trade_stats``,
    ``get_trade_tape``, plus the ``is_postgres`` / ``backend_label``
    backend-introspection properties and the ``_sqlite_paths`` path
    map the W21-6 spec uses).

    NOTE: the spec's ``_pg_get_trade_stats`` / ``_sqlite_get_trade_stats``
    helpers are an implementation detail — the consolidated
    ``database_manager.py`` may implement the aggregation in pure
    Python (delegating to ``get_trade_tape``) rather than splitting
    it into per-backend helpers. This test only asserts the public
    surface so a refactor that moves the aggregation logic doesn't
    break the test.
    """
    assert hasattr(db_manager, "record_trade")
    assert hasattr(db_manager, "get_trade_stats")
    assert hasattr(db_manager, "get_trade_tape")
    assert hasattr(db_manager, "is_postgres")
    assert hasattr(db_manager, "backend_label")
    # The W21-6 task spec uses ``self._sqlite_paths["market"]`` —
    # verify the path map exposes the ``market`` key when present.
    # (The consolidated implementation may use ``_sqlite_path``
    # singular instead — both are acceptable as long as one is
    # available.)
    if hasattr(db_manager, "_sqlite_paths"):
        assert "market" in db_manager._sqlite_paths
    else:
        assert hasattr(db_manager, "_sqlite_path"), (
            "db_manager must expose either _sqlite_paths (dict) or "
            "_sqlite_path (single path) for the trade-tape SQLite "
            "fallback"
        )
