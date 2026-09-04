"""
tests/test_live_fill_monitor.py — Unit tests for ``core/live_fill_monitor.py``.

W18-2 — Live fill acknowledgement loop (P0-C02 fix).

Covers the public-surface guarantees of the live fill monitor:

  1. ``start()`` / ``stop()`` lifecycle: idempotent, ``_running`` flag,
     ``_task`` is created / cancelled.
  2. ``_check_for_new_fills`` calls ``clob_client.get_trades()`` and
     processes each trade dict — recording the fill in the data store,
     transitioning the OSM, recording execution quality, and recording
     a FILL stage in the decision ledger.
  3. Deduplication: the same ``trade_id`` is never processed twice, even
     if the CLOB returns it on consecutive polls.
  4. Error handling: a malformed trade dict (missing ``id``, bogus price,
     raising ``record_fill`` …) is logged but never crashes the loop.
  5. Paper-mode short-circuit: when ``settings.paper_trade`` is True,
     ``clob_client.get_trades()`` is never called.
  6. ``_resolve_order_id`` correctly extracts the local order_id from
     every CLOB trade dict shape (taker fills, maker fills with
     ``maker_orders`` array, missing-order-id fallback).

Testing strategy
-----------------
Tests construct a fresh ``LiveFillMonitor()`` per test (NOT the module
singleton) so the in-memory ``_last_trade_ids`` set is empty at the
start of every test — no cross-test pollution. ``clob_client.get_trades``
is patched via ``monkeypatch.setattr`` on the singleton instance (the
production code path imports ``clob_client`` lazily inside
``_check_for_new_fills``, so the patch must target the attribute on the
instance, not the class — the lazy ``from core.clob_client import
clob_client`` re-binds to the same singleton object every call).

``settings.paper_trade`` is overridden to ``False`` in every test that
exercises the polling path (the conftest autouse fixture pins
``TRADING_MODE=paper``, so the global ``settings.paper_trade`` is
``True`` by default — without the override the monitor would
short-circuit before calling ``get_trades``).

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
pytest-asyncio is already a project dependency; the repo's ``pytest.ini``
declares ``testpaths = tests`` and is intentionally left untouched).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from config import settings
from core.clob_client import clob_client
from core.data_store import Order, OrderStatus, Side, store
from core.live_fill_monitor import (
    LiveFillMonitor,
    _is_terminal_state,
    live_fill_monitor,
)
from core.order_state_machine import (
    OrderState,
    OrderStateMachine,
    create_order,
    order_state_machine,
    transition,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the convention in every sibling test file.
pytestmark = pytest.mark.asyncio


# ── Helpers ────────────────────────────────────────────────────────────────

_TOKEN_ID = "0xdeadbeefcafe000000000000000000000000000000000000000000000000beef"


def _trade(
    trade_id: str = "trade-1",
    order_id: str = "order-1",
    side: str = "BUY",
    price: float = 0.55,
    size: float = 10.0,
    token_id: str = _TOKEN_ID,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a minimal CLOB-shaped trade dict.

    ``extra`` lets a test override / add fields (e.g. ``maker_orders`` for
    a maker-fill test, or ``asset_id`` instead of ``token_id``).
    """
    base: dict[str, Any] = {
        "id": trade_id,
        "taker_order_id": order_id,
        "asset_id": token_id,
        "side": side,
        "price": str(price),
        "size": str(size),
        "status": "MATCHED",
    }
    if extra:
        base.update(extra)
    return base


@pytest.fixture
def live_mode(monkeypatch):
    """Force ``settings.paper_trade = False`` for the duration of the test.

    The conftest autouse fixture pins ``TRADING_MODE=paper`` (so the
    global ``settings.paper_trade`` is ``True`` by default). Tests that
    exercise the live fill monitor's polling path must override this so
    the monitor doesn't short-circuit before calling ``get_trades``.
    """
    monkeypatch.setattr(settings, "paper_trade", False)
    return monkeypatch


@pytest.fixture
def monitor():
    """Fresh ``LiveFillMonitor`` per test — no shared ``_last_trade_ids``.

    Using a fresh instance (rather than the module singleton) keeps the
    dedup set empty at the start of every test. The singleton is left
    untouched so any production code path that imports it (e.g.
    ``api/server.py``'s lifespan startup hook) still sees a stable
    object.
    """
    return LiveFillMonitor(poll_interval=0.01)


@pytest.fixture
def mock_get_trades(monkeypatch):
    """Patch ``clob_client.get_trades`` with an AsyncMock.

    Returns the mock so the test can configure its return value via
    ``mock_get_trades.return_value = [...]`` or assert on call count
    via ``mock_get_trades.assert_awaited()``.
    """
    mock = AsyncMock()
    monkeypatch.setattr(clob_client, "get_trades", mock)
    return mock


# ── 1. start/stop lifecycle ─────────────────────────────────────────────────


async def test_start_sets_running_flag_and_creates_task(monitor: LiveFillMonitor):
    """``start()`` must set ``_running=True`` and create an asyncio Task."""
    assert monitor._running is False
    assert monitor._task is None

    await monitor.start()

    assert monitor._running is True
    assert monitor._task is not None
    assert not monitor._task.done()

    await monitor.stop()


async def test_start_is_idempotent(monitor: LiveFillMonitor):
    """Calling ``start()`` twice must not create a second task."""
    await monitor.start()
    first_task = monitor._task

    await monitor.start()
    second_task = monitor._task

    assert first_task is second_task  # same task, not a new one

    await monitor.stop()


async def test_stop_is_idempotent_when_not_running(monitor: LiveFillMonitor):
    """``stop()`` on a never-started monitor is a no-op (no error)."""
    assert monitor._running is False
    assert monitor._task is None
    await monitor.stop()  # must not raise
    assert monitor._running is False
    assert monitor._task is None


async def test_stop_cancels_running_task(monitor: LiveFillMonitor):
    """``stop()`` on a running monitor cancels the polling task."""
    await monitor.start()
    task = monitor._task
    assert task is not None

    await monitor.stop()

    assert monitor._running is False
    assert monitor._task is None
    # The cancelled task is awaited to completion (CancelledError swallowed).
    assert task.cancelled() or task.done()


# ── 2. _check_for_new_fills processes a trade end-to-end ────────────────────


async def test_check_for_new_fills_records_trade_in_data_store(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """A new CLOB trade is recorded in ``store.trades`` with the correct
    price / size / side / token_id."""
    # Seed the local data store with the matching open order so the
    # monitor can find it and propagate ``strategy`` / ``decision_id``.
    order = Order(
        order_id="order-1",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
        strategy="signal_trader",
        decision_id="dec-1234",
    )
    store.open_orders["order-1"] = order

    mock_get_trades.return_value = [_trade(trade_id="trade-1", order_id="order-1")]

    await monitor._check_for_new_fills()

    # The trade was recorded in store.trades with paper=False (live fill).
    assert len(store.trades) == 1
    recorded = store.trades[0]
    assert recorded.trade_id == "trade-1"
    assert recorded.token_id == _TOKEN_ID
    assert recorded.side == Side.BUY
    assert recorded.price == pytest.approx(0.55)
    assert recorded.size == pytest.approx(10.0)
    assert recorded.paper is False
    assert recorded.strategy == "signal_trader"


async def test_check_for_new_fills_updates_local_order_status(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """The local ``data_store.Order`` is moved from ``open_orders`` to
    ``order_history`` with status FILLED."""
    order = Order(
        order_id="order-2",
        token_id=_TOKEN_ID,
        side=Side.SELL,
        price=0.45,
        size=5.0,
        paper=False,
        strategy="signal_trader",
    )
    store.open_orders["order-2"] = order

    mock_get_trades.return_value = [_trade(
        trade_id="trade-2",
        order_id="order-2",
        side="SELL",
        price=0.45,
        size=5.0,
    )]

    await monitor._check_for_new_fills()

    # open_orders no longer holds the order; order_history does.
    assert "order-2" not in store.open_orders
    assert any(o.order_id == "order-2" for o in store.order_history)
    filled = next(o for o in store.order_history if o.order_id == "order-2")
    assert filled.status == OrderStatus.FILLED
    assert filled.size_matched == pytest.approx(5.0)


async def test_check_for_new_fills_transitions_osm_to_filled(
    monitor: LiveFillMonitor, live_mode, mock_get_trades, tmp_path, monkeypatch
):
    """The order_state_machine audit trail records the OPEN → FILLED
    transition (a snapshot with ``state == FILLED`` is persisted)."""
    # Use a fresh OSM pointed at a tmp_path db so the test is hermetic.
    osm = OrderStateMachine(tmp_path / "test_lfm_osm.db")

    # Seed an OPEN snapshot for the order so transition() has a starting
    # state to move from. The monitor loads via the singleton
    # ``order_state_machine``, so patch the singleton's load + save to
    # delegate to our fresh OSM.
    order = create_order(
        strategy="signal_trader",
        token_id=_TOKEN_ID,
        side="BUY",
        price=0.55,
        size=10.0,
        order_id="order-3",
        decision_id="dec-3",
    )
    order_open = transition(order, OrderState.VALIDATED)
    order_open = transition(order_open, OrderState.SUBMITTED)
    order_open = transition(order_open, OrderState.ACKNOWLEDGED)
    order_open = transition(order_open, OrderState.OPEN)
    osm.save(order_open)

    # Patch the module-level singleton's load + save to delegate to our
    # fresh OSM (the monitor imports the singleton lazily).
    monkeypatch.setattr(order_state_machine, "load", osm.load)
    monkeypatch.setattr(order_state_machine, "save", osm.save)

    # Seed the local data store so the monitor finds the order_id.
    store.open_orders["order-3"] = Order(
        order_id="order-3",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
        strategy="signal_trader",
        decision_id="dec-3",
    )

    mock_get_trades.return_value = [_trade(
        trade_id="trade-3", order_id="order-3",
    )]

    await monitor._check_for_new_fills()

    # The OSM should now have a FILLED snapshot for this order.
    final = osm.load("order-3")
    assert final is not None
    assert final.state == OrderState.FILLED


async def test_check_for_new_fills_records_execution_quality(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """The execution_quality ledger records a row for the live fill."""
    from core.execution_quality import DB_PATH as EQ_DB_PATH
    import sqlite3

    store.open_orders["order-4"] = Order(
        order_id="order-4",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
        strategy="signal_trader",
    )

    # Snapshot the row count before the fill so we can verify exactly one
    # new row was added (rather than asserting on an absolute count that
    # would break if a sibling test left rows in the shared conftest DB).
    with sqlite3.connect(EQ_DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM execution_quality")
        before = cur.fetchone()[0]

    mock_get_trades.return_value = [_trade(
        trade_id="trade-4", order_id="order-4",
    )]

    await monitor._check_for_new_fills()

    with sqlite3.connect(EQ_DB_PATH) as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM execution_quality")
        after = cur.fetchone()[0]
        # Verify a row was added carrying the live order_id.
        cur.execute(
            "SELECT order_id, paper, actual_fill FROM execution_quality "
            "WHERE order_id = ? ORDER BY id DESC LIMIT 1",
            ("order-4",),
        )
        row = cur.fetchone()

    assert after == before + 1
    assert row is not None
    assert row[0] == "order-4"
    assert row[1] == 0  # paper=0 ⇒ live fill (not paper)
    assert row[2] == pytest.approx(0.55)  # actual_fill = fill_price


async def test_check_for_new_fills_records_decision_ledger_fill_stage(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """When the local order has a ``decision_id``, the decision ledger
    records a FILL stage with the fill price / size."""
    from core.decision_ledger import DecisionLedger, STAGE_FILL, STAGE_ORDER
    from core.decision_ledger import decision_ledger as _dl_singleton

    # Use the singleton decision_ledger (it's pointed at /tmp via
    # conftest's DECISION_LEDGER_DB_PATH env override). Record an ORDER
    # stage first so the FILL has a chain to attach to.
    did = _dl_singleton.new_decision_id()
    await _dl_singleton.record(
        decision_id=did,
        stage=STAGE_ORDER,
        token_id=_TOKEN_ID,
        strategy="signal_trader",
        order_id="order-5",
        side="BUY",
        price=0.55,
        size=10.0,
        paper=False,
    )

    store.open_orders["order-5"] = Order(
        order_id="order-5",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
        strategy="signal_trader",
        decision_id=did,
    )

    mock_get_trades.return_value = [_trade(
        trade_id="trade-5", order_id="order-5",
    )]

    await monitor._check_for_new_fills()

    chain = await _dl_singleton.get_chain(did)
    stages = [e["stage"] for e in chain]
    assert STAGE_ORDER in stages
    assert STAGE_FILL in stages
    fill_event = next(e for e in chain if e["stage"] == STAGE_FILL)
    # ``order_id`` / ``trade_id`` are stored in ``data_json`` (decoded into
    # the ``data`` dict on read), not as top-level columns.
    fill_data = fill_event.get("data") or {}
    assert fill_data.get("order_id") == "order-5"
    assert fill_data.get("trade_id") == "trade-5"
    assert fill_data.get("paper") in (False, 0)  # live fill, not paper


# ── 3. Deduplication ────────────────────────────────────────────────────────


async def test_duplicate_trade_id_is_not_processed_twice(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """A trade with the same ``id`` on consecutive polls is processed once."""
    store.open_orders["order-dup"] = Order(
        order_id="order-dup",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
    )

    trade = _trade(trade_id="dup-1", order_id="order-dup")
    mock_get_trades.return_value = [trade]

    await monitor._check_for_new_fills()
    # First poll: 1 trade recorded.
    assert len(store.trades) == 1

    # Reset the local order state so a second fill would actually
    # re-add to store.trades (otherwise the order's already in
    # order_history and ``store.update_order`` would no-op).
    order = Order(
        order_id="order-dup",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
    )
    store.open_orders["order-dup"] = order

    await monitor._check_for_new_fills()
    # Second poll: same trade_id → deduplicated, no new trade recorded.
    assert len(store.trades) == 1
    assert "dup-1" in monitor._last_trade_ids


async def test_seen_trade_id_set_is_bounded(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """When the seen-id set exceeds ``_MAX_SEEN_TRADE_IDS`` it's trimmed.

    We can't easily generate 1001 real CLOB trades, but we CAN verify the
    trim path directly by pre-populating the set past the threshold and
    asserting it shrinks after one ``_process_trade`` call.
    """
    from core.live_fill_monitor import _KEEP_SEEN_TRADE_IDS, _MAX_SEEN_TRADE_IDS

    # Pre-populate the set above the threshold.
    monitor._last_trade_ids = {f"old-{i}" for i in range(_MAX_SEEN_TRADE_IDS + 50)}
    assert len(monitor._last_trade_ids) > _MAX_SEEN_TRADE_IDS

    store.open_orders["order-trim"] = Order(
        order_id="order-trim",
        token_id=_TOKEN_ID,
        side=Side.BUY,
        price=0.55,
        size=10.0,
        paper=False,
    )

    mock_get_trades.return_value = [_trade(
        trade_id="new-1", order_id="order-trim",
    )]

    await monitor._check_for_new_fills()

    # After processing, the set has been trimmed to ~_KEEP_SEEN_TRADE_IDS
    # entries (plus the new trade_id we just added).
    assert len(monitor._last_trade_ids) <= _KEEP_SEEN_TRADE_IDS + 5


# ── 4. Error handling ───────────────────────────────────────────────────────


async def test_check_for_new_fills_swallows_clob_errors(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """A CLOB ``get_trades()`` failure is logged but doesn't crash the
    monitor — the call returns silently and ``store.trades`` stays empty."""
    mock_get_trades.side_effect = RuntimeError("CLOB 503 service unavailable")

    # Must not raise.
    await monitor._check_for_new_fills()

    assert len(store.trades) == 0
    mock_get_trades.assert_awaited_once()


async def test_check_for_new_fills_swallows_malformed_trade(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """A single malformed trade dict in a batch doesn't poison the rest.

    Trades with no ``id`` are skipped (logged at debug) without raising.
    Trades with a bogus ``price`` (non-numeric) fall back to ``0.0`` and
    are still recorded.
    """
    good_trade = _trade(trade_id="good-1", order_id="order-good")
    bad_trade_no_id = {"side": "BUY", "price": "0.50", "size": "5"}  # no id
    bad_trade_bad_price = _trade(
        trade_id="bad-price", order_id="order-bad", price="not-a-number"
    )

    store.open_orders["order-good"] = Order(
        order_id="order-good", token_id=_TOKEN_ID,
        side=Side.BUY, price=0.55, size=10.0, paper=False,
    )
    store.open_orders["order-bad"] = Order(
        order_id="order-bad", token_id=_TOKEN_ID,
        side=Side.BUY, price=0.55, size=10.0, paper=False,
    )

    mock_get_trades.return_value = [bad_trade_no_id, bad_trade_bad_price, good_trade]

    await monitor._check_for_new_fills()

    # The good trade was processed; the bad ones were skipped / degraded
    # but didn't crash the batch.
    trade_ids = {t.trade_id for t in store.trades}
    assert "good-1" in trade_ids
    # ``bad-price`` has a valid id so it goes through; price falls back to 0.0.
    assert "bad-price" in trade_ids
    bad_recorded = next(t for t in store.trades if t.trade_id == "bad-price")
    assert bad_recorded.price == pytest.approx(0.0)


async def test_check_for_new_fills_handles_empty_response(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """An empty list from ``get_trades()`` is a no-op (no trades recorded)."""
    mock_get_trades.return_value = []
    await monitor._check_for_new_fills()
    assert len(store.trades) == 0


async def test_check_for_new_fills_handles_none_response(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """A ``None`` response (defensive — shouldn't happen but CLOB client
    guards against it) is treated as empty."""
    mock_get_trades.return_value = None
    await monitor._check_for_new_fills()
    assert len(store.trades) == 0


async def test_poll_loop_swallows_iterative_errors(
    monitor: LiveFillMonitor, live_mode, mock_get_trades
):
    """The polling loop logs a single iteration's error but keeps running.

    We configure ``get_trades`` to raise on the first call and return
    a trade on the second; the loop must survive the first failure and
    process the trade on the second.
    """
    good_trade = _trade(trade_id="poll-1", order_id="order-poll")
    store.open_orders["order-poll"] = Order(
        order_id="order-poll", token_id=_TOKEN_ID,
        side=Side.BUY, price=0.55, size=10.0, paper=False,
    )

    call_count = {"n": 0}

    async def _flaky_get_trades(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("transient network blip")
        return [good_trade]

    mock_get_trades.side_effect = _flaky_get_trades

    await monitor.start()
    # Give the loop two poll cycles to fail-then-succeed.
    await asyncio.sleep(0.05)
    await monitor.stop()

    # The good trade from the second iteration was processed.
    assert any(t.trade_id == "poll-1" for t in store.trades)


# ── 5. Paper-mode short-circuit ─────────────────────────────────────────────


async def test_paper_mode_skips_clob_call(
    monitor: LiveFillMonitor, mock_get_trades
):
    """When ``settings.paper_trade`` is True, ``get_trades`` is NEVER
    called — the paper simulator's own ``_fill_loop`` handles paper
    fills, so the live monitor would just duplicate the work."""
    # conftest pins settings.paper_trade=True, so we don't override it.
    assert settings.paper_trade is True

    await monitor._check_for_new_fills()

    mock_get_trades.assert_not_awaited()
    assert len(store.trades) == 0


# ── 6. _resolve_order_id field-extraction ──────────────────────────────────


def test_resolve_order_id_taker_order_id():
    """``taker_order_id`` is the first field tried — when present, it's
    returned (we were the taker)."""
    trade = {
        "taker_order_id": "ord-taker-1",
        "asset_id": _TOKEN_ID,
        "side": "BUY",
    }
    assert LiveFillMonitor._resolve_order_id(trade) == "ord-taker-1"


def test_resolve_order_id_order_id_fallback():
    """When ``taker_order_id`` is absent, ``order_id`` is the next try."""
    trade = {"order_id": "ord-1", "asset_id": _TOKEN_ID, "side": "BUY"}
    assert LiveFillMonitor._resolve_order_id(trade) == "ord-1"


def test_resolve_order_id_client_order_id_fallback():
    """``client_order_id`` is the last single-field fallback."""
    trade = {"client_order_id": "cid-1", "asset_id": _TOKEN_ID, "side": "BUY"}
    assert LiveFillMonitor._resolve_order_id(trade) == "cid-1"


def test_resolve_order_id_maker_orders_array():
    """For maker fills where the taker_order_id belongs to someone else,
    the order_id is recovered from the ``maker_orders`` array (the
    taker_order_id is absent / empty in this case so the maker-orders
    fallback kicks in)."""
    trade = {
        # No top-level taker_order_id / order_id / client_order_id —
        # the only order-id-bearing field is the maker_orders array.
        "maker_orders": [
            {"order_id": "ord-maker-1", "matched_amount": "5.0"},
            {"order_id": "ord-maker-2", "matched_amount": "2.5"},
        ],
        "asset_id": _TOKEN_ID,
        "side": "SELL",
    }
    # The first maker order_id is returned.
    assert LiveFillMonitor._resolve_order_id(trade) == "ord-maker-1"


def test_resolve_order_id_empty_when_no_fields():
    """Returns ``""`` when no order-id-bearing field is present."""
    trade = {"asset_id": _TOKEN_ID, "side": "BUY", "price": "0.50", "size": "10"}
    assert LiveFillMonitor._resolve_order_id(trade) == ""


def test_resolve_order_id_skips_non_dict_maker_orders():
    """Non-dict entries in ``maker_orders`` are skipped without raising."""
    trade = {
        "maker_orders": ["not-a-dict", 42, {"order_id": "ord-x"}],
    }
    assert LiveFillMonitor._resolve_order_id(trade) == "ord-x"


# ── 7. _is_terminal_state helper ────────────────────────────────────────────


def test_is_terminal_state_recognises_filled():
    """FILLED is a terminal state."""
    assert _is_terminal_state(OrderState.FILLED) is True


def test_is_terminal_state_recognises_cancelled():
    """CANCELLED is a terminal state."""
    assert _is_terminal_state(OrderState.CANCELLED) is True


def test_is_terminal_state_recognises_open_as_non_terminal():
    """OPEN is not a terminal state."""
    assert _is_terminal_state(OrderState.OPEN) is False


def test_is_terminal_state_accepts_string():
    """A plain ``str`` is accepted (compared against canonical names)."""
    assert _is_terminal_state("FILLED") is True
    assert _is_terminal_state("OPEN") is False
    assert _is_terminal_state("EXPIRED") is True


# ── 8. Singleton is constructible ────────────────────────────────────────────


def test_module_singleton_is_a_live_fill_monitor():
    """The module-level ``live_fill_monitor`` is an instance of
    ``LiveFillMonitor`` (so the lifespan startup hook in
    ``api/server.py`` can ``await live_fill_monitor.start()``)."""
    assert isinstance(live_fill_monitor, LiveFillMonitor)
    # Default poll interval.
    assert live_fill_monitor.poll_interval > 0
