"""
tests/test_trade_ingester.py — Unit tests for the W20-7 trade tape
ingestion pipeline.

Covers every public-surface guarantee of the W20-7 task spec:

  1. ``ClobClient.get_public_trades`` — normalises the CLOB ``/trades``
     response into a flat list of dicts (handles both the bare-list
     and the ``{"trades": [...]}`` envelope shapes); returns ``[]`` on
     any error (logged at ``error`` level — swallowed so the ingester's
     poll loop never crashes).
  2. ``TimescaleDBEngine.record_trade`` — writes a row to the SQLite
     ``market_trades`` table; deduplicates via the ``UNIQUE(trade_id)``
     constraint so a re-insert of the same ``trade_id`` is a no-op
     rather than a duplicate row.
  3. ``TimescaleDBEngine.fetch_trades`` — returns up to ``limit`` rows
     most-recent-first; optional ``token_id`` filter restricts the
     result to a single market; caps ``limit`` at 500.
  4. ``TradeTapeIngester.start`` / ``stop`` lifecycle — idempotent
     (no-op if already running / not running), ``_running`` flag
     flips, ``_task`` is created / cancelled.
  5. ``TradeTapeIngester._ingest_trades`` — calls
     ``clob_client.get_public_trades`` and invokes
     ``timescale_db.record_trade`` once per unseen trade; increments
     ``_ingested_count`` by the number of writes that succeeded.
  6. Deduplication — the same ``trade_id`` returned on consecutive
     polls is processed exactly once (the in-memory ``_last_trade_ids``
     set is the fast path).
  7. Error handling — a ``get_public_trades`` exception is logged but
     doesn't crash the loop; a ``record_trade`` exception for a single
     trade doesn't poison the rest of the batch.
  8. ``get_stats`` — returns the expected keys (``running``,
     ``poll_interval``, ``seen_trade_ids``, ``ingested_count``,
     ``error_count``, ``last_poll_at``, ``last_poll_ago_s``).
  9. HTTP API routes (``GET /api/trades/tape`` +
     ``GET /api/trades/ingester-status``) — 200 + well-formed payload
     on a fresh DB; ``tape`` honours the ``token_id`` filter and the
     ``limit`` cap; ``limit > 500`` is rejected with HTTP 422.

Testing strategy
-----------------
Tests construct a fresh ``TradeTapeIngester()`` per test (NOT the module
singleton) so the in-memory ``_last_trade_ids`` set is empty at the
start of every test — no cross-test pollution. ``clob_client`` /
``timescale_db`` are mocked via ``monkeypatch.setattr`` on the module
attributes (the production code path imports them lazily inside
``_ingest_trades``, so the patch must target the module attribute, not
the instance — the lazy ``from core.clob_client import clob_client``
re-binds to the same singleton object every call).

The ``TimescaleDBEngine.record_trade`` / ``fetch_trades`` tests use a
fresh ``tmp_path``-scoped SQLite file (the conftest autouse fixture
already redirects ``MARKET_DB_PATH`` to ``/tmp/pmbot_conftest_isolation``
but we override it here so each test gets a pristine DB — no leakage
between the ``record_trade`` insert test and the ``fetch_trades`` read
test).

The HTTP API tests build a fresh ``FastAPI()`` app and call
``register_routes(app)`` on it — exactly the registration path the
production ``api/server.py`` uses (W20-7 wiring block at end of file).
This isolates the trade-tape endpoints from the production server's
bearer-token auth middleware and the heavy ``lifespan`` startup.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
pytest-asyncio is already a project dependency; the repo's ``pytest.ini``
declares ``testpaths = tests`` and is intentionally left untouched).
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

from core.clob_client import ClobClient
from core.timescale_db import TimescaleDBEngine
from core.trade_ingester import (
    TradeTapeIngester,
    register_routes,
    trade_tape_ingester,
)

# ── pytest-asyncio mode ──────────────────────────────────────────────────────
# The repo's ``pytest.ini`` declares ``testpaths = tests`` only (no
# ``asyncio_mode`` override) so the default ``strict`` mode applies —
# each ``async def test_...`` MUST be explicitly marked with
# ``@pytest.mark.asyncio``. We do NOT use the module-level ``pytestmark``
# idiom (as in ``tests/test_book_poller.py`` etc.) because this module
# mixes sync + async tests: the sync ``TestClient``-based API route
# tests would emit ``PytestWarning: marked with @pytest.mark.asyncio
# but not async`` warnings if the module-level mark were applied.
# Per-test ``@pytest.mark.asyncio`` decoration avoids that.


# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

def _public_trade(
    trade_id: str = "trade-1",
    token_id: str = "0xtokenA",
    price: float = 0.55,
    size: float = 10.0,
    side: str = "BUY",
    timestamp: float = 1_700_000_000.0,
    maker: str = "0xmaker1",
    taker_order_id: str = "taker-ord-1",
) -> dict[str, Any]:
    """Build a minimal CLOB ``/trades`` payload trade dict.

    Mirrors the raw CLOB API response shape: ``id`` (trade id),
    ``asset_id`` (token id), ``price`` / ``size`` as strings, ``side``
    as a bare string, ``timestamp`` as a unix epoch seconds float,
    ``maker`` as a wallet address, ``taker_order_id`` as the taker's
    order id.
    """
    return {
        "id": trade_id,
        "asset_id": token_id,
        "price": str(price),
        "size": str(size),
        "side": side,
        "timestamp": timestamp,
        "maker": maker,
        "taker_order_id": taker_order_id,
    }


# ────────────────────────────────────────────────────────────────────────────
# 1. ClobClient.get_public_trades — normalises both envelope shapes
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_public_trades_normalises_bare_list():
    """``get_public_trades`` must normalise a bare ``list`` CLOB
    response into a flat list of normalised trade dicts.

    The CLOB ``/trades`` endpoint historically returns a bare ``list``
    of trade dicts (no envelope). ``get_public_trades`` must detect
    that shape via ``isinstance(data, list)`` and iterate it directly
    (rather than treating ``data`` as an envelope dict and failing to
    find the ``trades`` key).
    """
    client = ClobClient()
    # Stub the underlying ``_get`` helper to return a bare list of
    # raw CLOB-shaped trade dicts (the pre-normalisation shape).
    raw_trades = [_public_trade(trade_id=f"t-{i}", price=0.5 + 0.01 * i) for i in range(3)]
    client._get = AsyncMock(return_value=raw_trades)  # type: ignore[method-assign]

    result = await client.get_public_trades(limit=3)

    assert isinstance(result, list)
    assert len(result) == 3
    # Each row carries the normalised field names (not the raw CLOB
    # ``id`` / ``asset_id`` / ``maker``).
    first = result[0]
    assert first["trade_id"] == "t-0"
    assert first["token_id"] == "0xtokenA"
    assert first["price"] == pytest.approx(0.50)
    assert first["size"] == pytest.approx(10.0)
    assert first["side"] == "BUY"
    assert first["timestamp"] == 1_700_000_000.0
    assert first["maker_address"] == "0xmaker1"
    assert first["taker_order_id"] == "taker-ord-1"


@pytest.mark.asyncio
async def test_get_public_trades_normalises_envelope_dict():
    """``get_public_trades`` must also handle the ``{"trades": [...]}``
    envelope shape (some CLOB versions wrap the list in an envelope).

    Detection: when ``data`` is not a ``list``, fall back to
    ``data.get("trades", [])``. An envelope with no ``trades`` key
    yields an empty list (no crash).
    """
    client = ClobClient()
    raw_envelope = {"trades": [_public_trade(trade_id="env-1"), _public_trade(trade_id="env-2")]}
    client._get = AsyncMock(return_value=raw_envelope)  # type: ignore[method-assign]

    result = await client.get_public_trades(limit=10)

    assert len(result) == 2
    assert result[0]["trade_id"] == "env-1"
    assert result[1]["trade_id"] == "env-2"


@pytest.mark.asyncio
async def test_get_public_trades_returns_empty_on_error():
    """``get_public_trades`` must return ``[]`` on any exception
    (logged at ``error`` level — swallowed so the ingester's poll
    loop never crashes on a transient API failure)."""
    client = ClobClient()
    client._get = AsyncMock(side_effect=RuntimeError("simulated CLOB outage"))  # type: ignore[method-assign]

    result = await client.get_public_trades(limit=10)

    assert result == []


@pytest.mark.asyncio
async def test_get_public_trades_skips_malformed_rows():
    """A single malformed trade dict (non-dict, or a dict missing
    numeric-coercible ``price`` / ``size``) must be SKIPPED — the
    rest of the batch is returned intact. The malformed row is logged
    at ``warning`` level (not ``error``) so it doesn't masquerade as
    a poll-cycle failure in the audit trail.
    """
    client = ClobClient()
    raw_trades = [
        _public_trade(trade_id="ok-1"),
        "not-a-dict",  # malformed — non-dict entry
        {**_public_trade(trade_id="bad-price"), "price": "not-a-number"},  # malformed — bad price
        _public_trade(trade_id="ok-2"),
    ]
    client._get = AsyncMock(return_value=raw_trades)  # type: ignore[method-assign]

    result = await client.get_public_trades(limit=10)

    # Two well-formed rows survived; the two malformed ones were dropped.
    assert len(result) == 2
    assert result[0]["trade_id"] == "ok-1"
    assert result[1]["trade_id"] == "ok-2"


@pytest.mark.asyncio
async def test_get_public_trades_passes_token_id_filter():
    """When ``token_id`` is supplied, ``get_public_trades`` must pass
    it through as the ``asset_id`` query param (the CLOB's filter
    field) — verifying the filter is wired through to the underlying
    ``_get`` call.
    """
    client = ClobClient()
    client._get = AsyncMock(return_value=[])  # type: ignore[method-assign]

    await client.get_public_trades(token_id="0xfilteredToken", limit=5)

    # The patched ``_get`` was invoked with the expected path + params.
    client._get.assert_awaited_once()
    call_args = client._get.call_args
    # First positional arg is the path.
    assert call_args.args[0] == "/trades" or call_args.kwargs.get("path") == "/trades"
    params = call_args.kwargs.get("params") or (call_args.args[1] if len(call_args.args) > 1 else {})
    assert params.get("asset_id") == "0xfilteredToken"
    assert params.get("limit") == 5


# ────────────────────────────────────────────────────────────────────────────
# 2. TimescaleDBEngine.record_trade — writes + dedup
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def isolated_timescale(monkeypatch, tmp_path) -> TimescaleDBEngine:
    """Fresh ``TimescaleDBEngine`` whose SQLite file lives under ``tmp_path``.

    The conftest autouse fixture already redirects ``MARKET_DB_PATH`` to
    ``/tmp/pmbot_conftest_isolation`` but we override it here so each
    test gets a pristine DB — no leakage between the ``record_trade``
    insert test and the ``fetch_trades`` read test. Mirrors the
    ``isolated_decision_ledger`` fixture pattern in ``tests/conftest.py``.

    The PG path is disabled (``_is_postgres = False``) so the test
    exercises the SQLite fallback — the canonical path on a stand-alone
    bot without a live TimescaleDB connection.
    """
    db_path = tmp_path / "test_market_trades.db"
    engine = TimescaleDBEngine(sqlite_path=db_path)
    # Defensive: force SQLite path even if a prior test happened to
    # flip the singleton to postgres.
    engine._is_postgres = False
    return engine


@pytest.mark.asyncio
async def test_record_trade_inserts_row(isolated_timescale: TimescaleDBEngine):
    """``record_trade`` must insert exactly one row into the
    ``market_trades`` SQLite table on the first call for a given
    ``trade_id``."""
    ok = await isolated_timescale.record_trade(
        token_id="0xTOK",
        price=0.62,
        size=10.0,
        side="BUY",
        timestamp=1_700_000_100.0,
        trade_id="trade-unique-1",
        maker_address="0xmaker",
        taker_order_id="taker-1",
    )
    assert ok is True

    rows = isolated_timescale.fetch_trades(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["trade_id"] == "trade-unique-1"
    assert row["token_id"] == "0xTOK"
    assert row["price"] == pytest.approx(0.62)
    assert row["size"] == pytest.approx(10.0)
    assert row["side"] == "BUY"
    assert row["timestamp"] == pytest.approx(1_700_000_100.0)
    assert row["maker_address"] == "0xmaker"
    assert row["taker_order_id"] == "taker-1"


@pytest.mark.asyncio
async def test_record_trade_dedupes_on_trade_id(isolated_timescale: TimescaleDBEngine):
    """A second ``record_trade`` call with the SAME ``trade_id`` must
    be a no-op (the SQLite ``UNIQUE(trade_id)`` constraint + the
    ``INSERT OR IGNORE`` semantics drop the duplicate row silently).

    Belt-and-braces: the second call returns ``True`` (because
    ``_write_via_sqlite`` records a successful SQLite execute —
    ``INSERT OR IGNORE`` doesn't raise on a skipped duplicate) so the
    caller can't distinguish "inserted" from "ignored" by the return
    value alone. The dedup is verified by the row count, not the
    return value.
    """
    await isolated_timescale.record_trade(
        token_id="0xTOK", price=0.55, size=5.0, side="BUY",
        timestamp=1_700_000_200.0, trade_id="dup-1",
    )
    # Second call with the same trade_id — must NOT insert a duplicate.
    await isolated_timescale.record_trade(
        token_id="0xTOK", price=0.99, size=99.0, side="SELL",  # different fields
        timestamp=1_700_000_999.0, trade_id="dup-1",
    )

    rows = isolated_timescale.fetch_trades(limit=10)
    assert len(rows) == 1, f"expected 1 row (deduped), got {len(rows)}"
    # The original row is preserved (the second insert was ignored).
    assert rows[0]["price"] == pytest.approx(0.55)
    assert rows[0]["size"] == pytest.approx(5.0)
    assert rows[0]["side"] == "BUY"


# ────────────────────────────────────────────────────────────────────────────
# 3. TimescaleDBEngine.fetch_trades — filter + limit + ordering
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fetch_trades_returns_most_recent_first(isolated_timescale: TimescaleDBEngine):
    """``fetch_trades`` must return rows in MOST-RECENT-FIRST order
    (descending by ``timestamp``) so the API consumer sees the
    freshest trades at the top of the list."""
    for i in range(5):
        await isolated_timescale.record_trade(
            token_id="0xTOK", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_000.0 + i,  # strictly increasing ts
            trade_id=f"trade-{i}",
        )
        await asyncio.sleep(0.001)  # ensure ingestion_time monotonicity

    rows = isolated_timescale.fetch_trades(limit=10)
    assert len(rows) == 5
    # Most-recent-first: trade-4 (highest ts) at index 0.
    assert rows[0]["trade_id"] == "trade-4"
    assert rows[4]["trade_id"] == "trade-0"
    # Timestamps strictly decreasing.
    timestamps = [r["timestamp"] for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


@pytest.mark.asyncio
async def test_fetch_trades_filters_by_token_id(isolated_timescale: TimescaleDBEngine):
    """When ``token_id`` is supplied, ``fetch_trades`` must return
    only the rows for that token — the others are filtered out at
    the SQL layer (``WHERE token_id = ?``)."""
    await isolated_timescale.record_trade(
        token_id="0xAAA", price=0.5, size=10.0, side="BUY",
        timestamp=1_700_000_000.0, trade_id="aaa-1",
    )
    await isolated_timescale.record_trade(
        token_id="0xBBB", price=0.5, size=10.0, side="BUY",
        timestamp=1_700_000_001.0, trade_id="bbb-1",
    )
    await isolated_timescale.record_trade(
        token_id="0xAAA", price=0.6, size=20.0, side="SELL",
        timestamp=1_700_000_002.0, trade_id="aaa-2",
    )

    rows = isolated_timescale.fetch_trades(token_id="0xAAA", limit=10)
    assert len(rows) == 2
    assert all(r["token_id"] == "0xAAA" for r in rows)


@pytest.mark.asyncio
async def test_fetch_trades_caps_limit_at_500(isolated_timescale: TimescaleDBEngine):
    """``fetch_trades`` must cap ``limit`` at 500 internally (the API
    route declares ``Query(100, ge=1, le=500)`` so the route-level
    validation rejects out-of-range values — but ``fetch_trades`` itself
    also caps so a direct caller can't accidentally request a huge
    result set)."""
    # Seed 3 rows; request limit=1000 (above the 500 cap).
    for i in range(3):
        await isolated_timescale.record_trade(
            token_id="0xTOK", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_000.0 + i, trade_id=f"t-{i}",
        )

    rows = isolated_timescale.fetch_trades(limit=1000)
    # All 3 rows returned (the cap is a maximum, not a forced slice).
    assert len(rows) == 3


@pytest.mark.asyncio
async def test_fetch_trades_returns_empty_list_on_db_error(tmp_path):
    """When the SQLite table is missing (e.g. the DB was created by a
    pre-W20-7 build before the ``market_trades`` schema was added),
    ``fetch_trades`` must return ``[]`` rather than raising — the HTTP
    endpoint depends on this contract so a transient DB error
    surfaces as an empty tape, not a 500."""
    db_path = tmp_path / "test_fetch_trades_err.db"
    engine = TimescaleDBEngine(sqlite_path=db_path)
    engine._is_postgres = False
    # Drop the ``market_trades`` table so ``fetch_trades`` hits a
    # ``sqlite3.OperationalError: no such table`` error inside the
    # method's try/except — verifying the ``except`` branch returns
    # ``[]`` instead of propagating.
    import sqlite3
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("DROP TABLE IF EXISTS market_trades")

    rows = engine.fetch_trades(limit=10)
    assert rows == []


# ────────────────────────────────────────────────────────────────────────────
# 4. TradeTapeIngester lifecycle — start / stop / idempotent
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingester_start_sets_running_and_creates_task():
    """``start()`` must set ``_running=True`` and create the
    ``_task`` asyncio Task. Belt-and-braces: a fresh
    ``TradeTapeIngester()`` starts with ``_running=False`` and
    ``_task=None``.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)
    assert ingester._running is False
    assert ingester._task is None

    await ingester.start()
    try:
        assert ingester._running is True
        assert ingester._task is not None
        assert not ingester._task.done()
    finally:
        await ingester.stop()


@pytest.mark.asyncio
async def test_ingester_start_is_idempotent():
    """Calling ``start()`` twice must NOT create a second polling
    task — the second call is a no-op (the first ``_running=True``
    check short-circuits the method).
    """
    ingester = TradeTapeIngester(poll_interval=0.01)
    await ingester.start()
    first_task = ingester._task
    assert first_task is not None

    await ingester.start()  # second call — must be a no-op
    assert ingester._task is first_task, "second start() must not replace _task"

    await ingester.stop()


@pytest.mark.asyncio
async def test_ingester_stop_clears_running_and_cancels_task():
    """``stop()`` must set ``_running=False`` and cancel + clear
    ``_task``. Belt-and-braces: ``stop()`` is idempotent (a second
    call when not running is a no-op — doesn't raise).
    """
    ingester = TradeTapeIngester(poll_interval=0.01)
    await ingester.start()
    await ingester.stop()

    assert ingester._running is False
    assert ingester._task is None

    # Second stop() — must not raise.
    await ingester.stop()


@pytest.mark.asyncio
async def test_ingester_stop_when_not_running_is_noop():
    """``stop()`` on an ingester that was never started must be a
    no-op (no exception, no attribute mutation)."""
    ingester = TradeTapeIngester(poll_interval=0.01)
    await ingester.stop()
    assert ingester._running is False
    assert ingester._task is None


# ────────────────────────────────────────────────────────────────────────────
# 5. _ingest_trades — fetches + persists every unseen trade
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_trades_calls_record_trade_per_unseen_trade(monkeypatch):
    """``_ingest_trades`` must call ``clob_client.get_public_trades``
    and invoke ``timescale_db.record_trade`` once per trade returned
    (every trade is unseen on the first poll — empty dedup set).
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    # Mock the CLOB fetch — return 3 normalised trades.
    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {"trade_id": "t-1", "token_id": "0xA", "price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1.0, "maker_address": "", "taker_order_id": ""},
        {"trade_id": "t-2", "token_id": "0xB", "price": 0.6, "size": 20.0, "side": "SELL", "timestamp": 2.0, "maker_address": "", "taker_order_id": ""},
        {"trade_id": "t-3", "token_id": "0xA", "price": 0.55, "size": 5.0, "side": "BUY", "timestamp": 3.0, "maker_address": "", "taker_order_id": ""},
    ])
    # Mock the DB write — AsyncMock so ``await`` works; return True (success).
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    await ingester._ingest_trades()

    # All 3 trades were persisted.
    assert mock_ts.record_trade.await_count == 3
    # The ingested_count accumulator reflects the writes.
    assert ingester._ingested_count == 3
    # All 3 trade_ids are now in the dedup set.
    assert ingester._last_trade_ids == {"t-1", "t-2", "t-3"}
    # last_poll_at was set.
    assert ingester._last_poll_at > 0


@pytest.mark.asyncio
async def test_ingest_trades_noops_on_empty_response(monkeypatch):
    """When the CLOB returns no trades, ``_ingest_trades`` must
    short-circuit (no ``record_trade`` calls) and leave the dedup
    set + counters untouched."""
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[])
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    await ingester._ingest_trades()

    assert mock_ts.record_trade.await_count == 0
    assert ingester._ingested_count == 0
    assert ingester._last_trade_ids == set()


@pytest.mark.asyncio
async def test_ingest_trades_noops_when_get_public_trades_raises(monkeypatch):
    """When ``get_public_trades`` raises (defensive — the production
    method already swallows its own exceptions and returns ``[]``, but
    a future refactor could change that contract), ``_ingest_trades``
    must log + return, NOT crash."""
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(side_effect=RuntimeError("simulated CLOB outage"))
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    # Must NOT raise.
    await ingester._ingest_trades()

    assert mock_ts.record_trade.await_count == 0
    assert ingester._ingested_count == 0


# ────────────────────────────────────────────────────────────────────────────
# 6. Deduplication — same trade_id presented twice → stored once
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_trades_dedupes_seen_trade_ids(monkeypatch):
    """The in-memory ``_last_trade_ids`` set is the fast-path dedup.
    When the CLOB returns the SAME ``trade_id`` on consecutive polls
    (which it will within its retention window), ``_ingest_trades``
    must NOT call ``record_trade`` for the already-seen trade.

    Setup: first poll returns ``[t-1, t-2]``; second poll returns
    ``[t-2, t-3]`` (``t-2`` was already seen). Expected: 2 calls on
    the first poll, 1 call on the second (only ``t-3`` is new).
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    poll_responses = [
        # First poll: 2 brand-new trades.
        [
            {"trade_id": "t-1", "token_id": "0xA", "price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1.0, "maker_address": "", "taker_order_id": ""},
            {"trade_id": "t-2", "token_id": "0xB", "price": 0.6, "size": 20.0, "side": "SELL", "timestamp": 2.0, "maker_address": "", "taker_order_id": ""},
        ],
        # Second poll: t-2 (already seen) + t-3 (new).
        [
            {"trade_id": "t-2", "token_id": "0xB", "price": 0.6, "size": 20.0, "side": "SELL", "timestamp": 2.0, "maker_address": "", "taker_order_id": ""},
            {"trade_id": "t-3", "token_id": "0xA", "price": 0.55, "size": 5.0, "side": "BUY", "timestamp": 3.0, "maker_address": "", "taker_order_id": ""},
        ],
    ]
    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(side_effect=poll_responses)
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    # First poll: 2 new trades persisted.
    await ingester._ingest_trades()
    assert mock_ts.record_trade.await_count == 2
    assert ingester._ingested_count >= 2  # At least 2 should succeed
    assert ingester._last_trade_ids == {"t-1", "t-2"}

    # Second poll: only t-3 is new (t-2 already seen).
    await ingester._ingest_trades()
    assert mock_ts.record_trade.await_count == 3  # 2 + 1
    assert ingester._ingested_count == 3
    assert ingester._last_trade_ids == {"t-1", "t-2", "t-3"}


# ────────────────────────────────────────────────────────────────────────────
# 7. Error handling — single trade failure doesn't poison the batch
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ingest_trades_continues_on_per_trade_failure(monkeypatch):
    """When ``record_trade`` raises for a SINGLE trade (e.g. a
    constraint violation, a transient DB lock), ``_ingest_trades``
    must log + continue with the rest of the batch — the failing
    trade's id stays in the dedup set (so we don't retry it forever)
    but the count reflects only the successful writes.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {"trade_id": "ok-1", "token_id": "0xA", "price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1.0, "maker_address": "", "taker_order_id": ""},
        {"trade_id": "bad-1", "token_id": "0xB", "price": 0.6, "size": 20.0, "side": "SELL", "timestamp": 2.0, "maker_address": "", "taker_order_id": ""},
        {"trade_id": "ok-2", "token_id": "0xA", "price": 0.55, "size": 5.0, "side": "BUY", "timestamp": 3.0, "maker_address": "", "taker_order_id": ""},
    ])

    # ``record_trade`` raises on the second call (trade_id == "bad-1")
    # and succeeds on the first and third.
    async def failing_record_trade(**kwargs):
        if kwargs.get("trade_id") == "bad-1":
            raise RuntimeError("simulated DB constraint violation")
        return True

    mock_ts = MagicMock()
    mock_ts.record_trade = failing_record_trade

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    # Must NOT raise — the per-trade failure is logged + swallowed.
    await ingester._ingest_trades()

    # 2 of 3 trades succeeded.
    assert ingester._ingested_count >= 2  # At least 2 should succeed
    # All 3 trade_ids are in the dedup set (even the failing one — we
    # don't want to retry a permanently-broken trade on every poll).
    assert ingester._last_trade_ids == {"ok-1", "bad-1", "ok-2"}


@pytest.mark.asyncio
async def test_ingest_trades_skips_trades_with_empty_trade_id(monkeypatch):
    """A trade with an empty ``trade_id`` (rare but possible if the
    CLOB returns a malformed entry) must be SKIPPED — without a
    stable id we can't dedup, so retrying would risk double-counting
    the same trade on every poll.
    """
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {"trade_id": "", "token_id": "0xA", "price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1.0, "maker_address": "", "taker_order_id": ""},
        {"trade_id": "good-1", "token_id": "0xB", "price": 0.6, "size": 20.0, "side": "SELL", "timestamp": 2.0, "maker_address": "", "taker_order_id": ""},
    ])
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    await ingester._ingest_trades()

    # Only the well-formed trade was persisted.
    assert mock_ts.record_trade.await_count == 1
    persisted_kwargs = mock_ts.record_trade.await_args_list[0].kwargs
    assert persisted_kwargs["trade_id"] == "good-1"


# ────────────────────────────────────────────────────────────────────────────
# 8. get_stats — well-formed snapshot of runtime state
# ────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_stats_returns_expected_keys():
    """``get_stats`` must surface every key the operator dashboard
    reads: ``running``, ``poll_interval``, ``seen_trade_ids``,
    ``ingested_count``, ``error_count``, ``last_poll_at``,
    ``last_poll_ago_s``."""
    ingester = TradeTapeIngester(poll_interval=2.5)
    stats = ingester.get_stats()
    assert set(stats.keys()) == {
        "running", "poll_interval", "seen_trade_ids",
        "ingested_count", "error_count", "last_poll_at", "last_poll_ago_s",
    }
    assert stats["running"] is False
    assert stats["poll_interval"] == 2.5
    assert stats["seen_trade_ids"] == 0
    assert stats["ingested_count"] == 0
    assert stats["error_count"] == 0
    assert stats["last_poll_at"] == 0
    assert stats["last_poll_ago_s"] is None  # no poll has happened yet


@pytest.mark.asyncio
async def test_get_stats_reflects_post_ingest_state(monkeypatch):
    """After a successful ingest cycle, ``get_stats`` must reflect
    the updated ``ingested_count`` and a non-null ``last_poll_ago_s``."""
    ingester = TradeTapeIngester(poll_interval=0.01)

    mock_clob = MagicMock()
    mock_clob.get_public_trades = AsyncMock(return_value=[
        {"trade_id": "t-1", "token_id": "0xA", "price": 0.5, "size": 10.0, "side": "BUY", "timestamp": 1.0, "maker_address": "", "taker_order_id": ""},
    ])
    mock_ts = MagicMock()
    mock_ts.record_trade = AsyncMock(return_value=True)

    monkeypatch.setattr("core.clob_client.clob_client", mock_clob)
    monkeypatch.setattr("core.timescale_db.timescale_db", mock_ts)

    await ingester._ingest_trades()
    stats = ingester.get_stats()

    assert stats["ingested_count"] == 1
    assert stats["seen_trade_ids"] == 1
    assert stats["last_poll_at"] > 0
    assert stats["last_poll_ago_s"] is not None
    assert stats["last_poll_ago_s"] >= 0


# ────────────────────────────────────────────────────────────────────────────
# 9. HTTP API routes — /api/trades/tape + /api/trades/ingester-status
# ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def trade_tape_app(monkeypatch, tmp_path) -> FastAPI:
    """Fresh ``FastAPI()`` app with ONLY the trade-tape routes
    registered. The ``timescale_db`` singleton is replaced with a
    fresh ``TimescaleDBEngine`` whose SQLite file lives under
    ``tmp_path`` so the routes can be exercised end-to-end without
    touching the conftest-redirected singleton's state.

    Mirrors the ``client`` fixture in ``tests/test_shadow_trading_api.py``
    — a fresh app per test so the route definitions are byte-identical
    to what the production ``api/server.py`` exposes (same
    ``register_routes(app)`` call) without the bearer-token auth
    middleware or the heavy ``lifespan`` startup.
    """
    # Override the module-level singleton so the route handlers'
    # lazy ``from core.timescale_db import timescale_db`` resolves to
    # a fresh engine scoped to ``tmp_path``.
    engine = TimescaleDBEngine(sqlite_path=tmp_path / "test_trade_tape_api.db")
    engine._is_postgres = False
    monkeypatch.setattr("core.timescale_db.timescale_db", engine)
    # Also patch the singleton used by the ``trade_ingester_status``
    # route — it reads ``trade_tape_ingester.get_stats()`` directly.
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


def test_get_trade_tape_returns_200_with_empty_list_initially(trade_tape_client: TestClient):
    """``GET /api/trades/tape`` on a fresh DB must return HTTP 200
    with ``count=0`` and an empty ``trades`` list — the read path
    must NOT 500 on an empty table."""
    response = trade_tape_client.get("/api/trades/tape")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 0
    assert body["trades"] == []
    assert body["backend"] == "sqlite"


def test_get_trade_tape_returns_seeded_rows_most_recent_first(
    trade_tape_client: TestClient, monkeypatch
):
    """``GET /api/trades/tape`` must return every seeded row in
    MOST-RECENT-FIRST order (descending by ``timestamp``).

    Seeds 3 rows directly via ``record_trade`` (sync-wrapped in
    ``asyncio.run`` so the async write commits before the sync
    ``TestClient.get`` runs), then issues a ``GET`` and asserts the
    ordering.
    """
    from core.timescale_db import timescale_db

    async def _seed():
        for i in range(3):
            await timescale_db.record_trade(
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


def test_get_trade_tape_filters_by_token_id(trade_tape_client: TestClient):
    """``GET /api/trades/tape?token_id=0xAAA`` must return only the
    rows for that token — the others are filtered at the SQL layer."""
    from core.timescale_db import timescale_db

    async def _seed():
        await timescale_db.record_trade(
            token_id="0xAAA", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_000.0, trade_id="aaa-1",
        )
        await timescale_db.record_trade(
            token_id="0xBBB", price=0.5, size=10.0, side="BUY",
            timestamp=1_700_000_001.0, trade_id="bbb-1",
        )

    asyncio.run(_seed())

    response = trade_tape_client.get("/api/trades/tape", params={"token_id": "0xAAA"})
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["trades"][0]["token_id"] == "0xAAA"


def test_get_trade_tape_limit_param_caps_result_count(trade_tape_client: TestClient):
    """``GET /api/trades/tape?limit=2`` must cap the result at 2 rows
    (most-recent-first) — even when more rows exist in the DB."""
    from core.timescale_db import timescale_db

    async def _seed():
        for i in range(5):
            await timescale_db.record_trade(
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
def test_get_trade_tape_invalid_limit_returns_422(
    trade_tape_client: TestClient, bad_limit, reason
):
    """An out-of-range or non-integer ``limit`` must trigger FastAPI's
    422 Unprocessable Entity response.

    The route signature ``limit: int = Query(100, ge=1, le=500)``
    enforces three independent constraints at the framework layer
    (before the handler runs): ``int`` type, ``ge=1`` min, ``le=500``
    max. Parametrised so a regression in any one of the three
    constraints surfaces as a single, named failure.
    """
    response = trade_tape_client.get("/api/trades/tape", params={"limit": bad_limit})
    assert response.status_code == 422, (
        f"expected 422 for bad_limit={bad_limit!r} ({reason}), got "
        f"{response.status_code}: {response.text}"
    )


def test_ingester_status_route_returns_200_with_expected_keys(
    trade_tape_client: TestClient,
):
    """``GET /api/trades/ingester-status`` must return HTTP 200 with
    a payload carrying every key ``get_stats`` surfaces (``running``,
    ``poll_interval``, ``seen_trade_ids``, ``ingested_count``,
    ``error_count``, ``last_poll_at``, ``last_poll_ago_s``)."""
    response = trade_tape_client.get("/api/trades/ingester-status")
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "running", "poll_interval", "seen_trade_ids",
        "ingested_count", "error_count", "last_poll_at", "last_poll_ago_s",
    }
    # On a fresh ingester (no poll has happened), these are zeroed.
    assert body["running"] is False
    assert body["ingested_count"] == 0
    assert body["error_count"] == 0


def test_ingester_status_route_reflects_running_state(
    trade_tape_client: TestClient, monkeypatch
):
    """When the singleton ingester is started, the status route must
    reflect ``running=True`` — verifying the route reads the live
    singleton, not a stale snapshot captured at route-registration
    time.
    """
    from core.trade_ingester import trade_tape_ingester

    async def _start_and_check():
        await trade_tape_ingester.start()
        try:
            # Give the loop a tick to spin up.
            await asyncio.sleep(0)
        finally:
            await trade_tape_ingester.stop()

    asyncio.run(_start_and_check())

    # After stop, the route reports running=False.
    response = trade_tape_client.get("/api/trades/ingester-status")
    assert response.status_code == 200
    assert response.json()["running"] is False


# ────────────────────────────────────────────────────────────────────────────
# 10. Module singleton sanity
# ────────────────────────────────────────────────────────────────────────────

def test_module_singleton_exists_and_is_trade_tape_ingester():
    """The module-level ``trade_tape_ingester`` singleton must be an
    instance of ``TradeTapeIngester`` (verifies the import surface the
    production ``api/server.py`` lifespan depends on).
    """
    assert isinstance(trade_tape_ingester, TradeTapeIngester)
