"""
tests/test_data_store.py — X10 unit tests for ``core/data_store.py``.

Scope: pure-Python unit coverage of the 8 in-memory + persistence
guarantees of ``core.data_store.DataStore`` required by the X10 task spec:

  1. ``add_order`` stores the order in ``open_orders`` keyed by ``order_id``.
  2. ``update_order`` mutates the stored order's ``status`` and migrates
     the order to ``order_history`` once it reaches a terminal state
     (FILLED / CANCELLED).
  3. ``record_fill`` updates ``daily_pnl`` (by ``trade.pnl``) and
     ``paper_balance`` (by ``±price*size`` — BUY subtracts cost, SELL
     adds revenue).
  4. ``get_order_book`` returns the stored ``OrderBook`` for a known
     token_id, and ``None`` for an unknown one.
  5. The ``positions`` dict tracks ``yes_shares`` (long-YES share count)
     across multiple BUY fills via weighted-average entry-price update.
  6. ``log_event`` appends a timestamped entry to the ``event_log``
     list (consumed by ``get_recent_events``).
  7. ``total_exposure`` returns the sum of ``Position.current_exposure``
     (= ``yes_shares * avg_entry_price``) across all positions.
  8. ``save_to_disk`` → ``load_from_disk`` round-trips the persisted
     state (daily_pnl, paper_balance, peak_equity, positions, trades,
     equity_history) into a fresh ``DataStore`` instance.

Each test constructs a brand-new ``DataStore()`` (NOT the module-level
``store`` singleton) so the global singleton referenced by production
code paths is left untouched. The module-level singleton is still reset
between tests by the autouse ``_reset_store_factory_defaults`` fixture
in ``tests/conftest.py`` — that fixture is harmless for these tests
because every test here operates on its own fresh instance.

Environment strategy
--------------------
``core/data_store.py`` reads ``STORE_STATE_PATH`` from ``os.environ`` at
module-import time and binds it to the module-level ``STATE_FILE``
constant. The shared ``tests/conftest.py`` already calls
``os.environ.setdefault("STORE_STATE_PATH", ...)`` BEFORE the first
import of ``core.data_store`` (it imports ``DataStore`` / ``store`` at
line 110), so the module-level ``STATE_FILE`` always points at a
writable ``/tmp/pmbot_conftest_isolation/store_state.json``. This test
file mirrors the same env-var redirect idiom (defensively, in case a
sibling test file is collected first and triggers the import before
conftest's setdefault has run) and additionally monkeypatches
``core.data_store.STATE_FILE`` to a per-test ``tmp_path`` for the
save/load round-trip test so the round-trip file is fully isolated from
any other ``DataStore.save_to_disk`` caller in the suite.

All async tests are collected via the module-level
``pytestmark = pytest.mark.asyncio`` declaration (the repo's
``pytest.ini`` declares ``testpaths = tests`` but does NOT set
``asyncio_mode = "auto"`` — pytest-asyncio defaults to strict mode, so
the mark is required on every ``async def test_...`` function).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing any
# project module that reads ``os.environ`` at module-import time. ────────────
# ``setdefault`` lets the shared ``tests/conftest.py`` (which is imported
# by pytest BEFORE this file) win when it has already set these — and lets
# a CI runner override them. Otherwise this file is hermetic to ``/tmp``
# and cannot clobber any real persisted state in the repo's ``data/`` dir.
_TMP_ROOT = Path("/tmp/pmbot_data_store_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``) when pytest is invoked from a different cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_store import (  # noqa: E402
    BANKROLL_BASELINE,
    DataStore,
    Order,
    OrderBook,
    OrderStatus,
    PriceLevel,
    Side,
    Trade,
)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the X10 task constraint ("Do NOT edit existing files"), so we use
# the module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``.
# (Mirrors the convention already adopted by every sibling test module —
# see ``tests/test_decision_ledger.py``, ``tests/test_paper_simulator.py``,
# ``tests/test_book_poller.py``, ``tests/test_risk_manager.py``, etc.)
pytestmark = pytest.mark.asyncio


# ── Fixture: fresh DataStore per test ──────────────────────────────────────
#
# This fixture intentionally does NOT use the shared ``isolated_store``
# fixture from ``tests/conftest.py``: that fixture monkeypatches
# ``DataStore.load_from_disk`` to a no-op for the duration of the test
# (see ``tests/conftest.py:281``), which would break the
# save_to_disk / load_from_disk round-trip test (#8) below. Instead we
# construct a brand-new ``DataStore()`` directly — the constructor does
# NOT call ``load_from_disk`` (that call lives at module level on the
# singleton: ``store = DataStore(); store.load_from_disk()``), so the
# returned instance starts with empty containers and factory-default
# scalars (``daily_pnl=0``, ``paper_balance=BANKROLL_BASELINE``,
# ``peak_equity=BANKROLL_BASELINE``, ``equity_history`` = single
# bootstrap point).
@pytest.fixture
def fresh_store() -> DataStore:
    """Brand-new ``DataStore`` instance, no on-disk state loaded."""
    return DataStore()


# ── 1. add_order stores order in open_orders ───────────────────────────────
async def test_add_order_stores_order_in_open_orders(fresh_store: DataStore) -> None:
    """``add_order(order)`` must store the order in ``open_orders`` keyed
    by ``order.order_id`` and must NOT touch ``order_history``."""
    order = Order(
        order_id="x10-add-1",
        token_id="TOK_X10_1",
        side=Side.BUY,
        price=0.55,
        size=10.0,
        strategy="ml_sig_v1",
        paper=True,
    )

    # Pre-condition: empty store.
    assert fresh_store.open_orders == {}
    assert fresh_store.order_history == []

    await fresh_store.add_order(order)

    # (a) The order is retrievable from open_orders by its order_id.
    assert "x10-add-1" in fresh_store.open_orders
    stored = fresh_store.open_orders["x10-add-1"]

    # (b) The stored object IS the same Order instance (no copy / clone).
    assert stored is order

    # (c) All identity fields round-trip.
    assert stored.order_id == "x10-add-1"
    assert stored.token_id == "TOK_X10_1"
    assert stored.side == Side.BUY
    assert stored.price == pytest.approx(0.55)
    assert stored.size == pytest.approx(10.0)
    assert stored.strategy == "ml_sig_v1"
    assert stored.paper is True

    # (d) Default status is OPEN (untouched by add_order).
    assert stored.status == OrderStatus.OPEN

    # (e) order_history is NOT touched by add_order — only terminal-state
    #     transitions in update_order migrate orders there.
    assert fresh_store.order_history == []

    # (f) Adding a second, distinct order keeps both.
    order_b = Order(
        order_id="x10-add-2",
        token_id="TOK_X10_1",
        side=Side.SELL,
        price=0.60,
        size=4.0,
    )
    await fresh_store.add_order(order_b)
    assert len(fresh_store.open_orders) == 2
    assert fresh_store.open_orders["x10-add-2"] is order_b


# ── 2. update_order changes status ─────────────────────────────────────────
async def test_update_order_changes_status(fresh_store: DataStore) -> None:
    """``update_order`` must mutate the stored order's status (and any
    other supplied kwargs) in place. When the new status is a terminal
    one (FILLED or CANCELLED), the order must be migrated from
    ``open_orders`` to ``order_history``. A miss (unknown order_id)
    must return ``None`` and leave state untouched."""
    order = Order(
        order_id="x10-upd-1",
        token_id="TOK_X10_2",
        side=Side.BUY,
        price=0.50,
        size=10.0,
        paper=True,
    )
    await fresh_store.add_order(order)
    assert fresh_store.open_orders["x10-upd-1"].status == OrderStatus.OPEN

    # ── (a) Partial fill: status → PARTIALLY_FILLED, order stays open.
    updated = await fresh_store.update_order(
        "x10-upd-1",
        size_matched=4.0,
        status=OrderStatus.PARTIALLY_FILLED,
    )
    assert updated is not None
    assert updated.size_matched == pytest.approx(4.0)
    assert updated.status == OrderStatus.PARTIALLY_FILLED
    # Non-terminal status ⇒ order remains in open_orders, NOT in history.
    assert "x10-upd-1" in fresh_store.open_orders
    assert fresh_store.order_history == []

    # The mutation was in-place on the SAME Order object.
    assert fresh_store.open_orders["x10-upd-1"] is order
    assert fresh_store.open_orders["x10-upd-1"].size_matched == pytest.approx(4.0)

    # ── (b) Terminal state: FILLED → migrated to order_history.
    updated = await fresh_store.update_order(
        "x10-upd-1",
        size_matched=10.0,
        status=OrderStatus.FILLED,
    )
    assert updated is not None
    assert updated.status == OrderStatus.FILLED
    assert updated.size_matched == pytest.approx(10.0)

    # Order removed from open_orders, present in order_history.
    assert "x10-upd-1" not in fresh_store.open_orders
    assert len(fresh_store.order_history) == 1
    assert fresh_store.order_history[0].order_id == "x10-upd-1"
    assert fresh_store.order_history[0].status == OrderStatus.FILLED

    # ── (c) CANCELLED also triggers migration: exercise that path too.
    order_b = Order(
        order_id="x10-upd-2",
        token_id="TOK_X10_2",
        side=Side.SELL,
        price=0.55,
        size=6.0,
    )
    await fresh_store.add_order(order_b)
    cancelled = await fresh_store.update_order(
        "x10-upd-2", status=OrderStatus.CANCELLED
    )
    assert cancelled is not None
    assert cancelled.status == OrderStatus.CANCELLED
    assert "x10-upd-2" not in fresh_store.open_orders
    assert len(fresh_store.order_history) == 2

    # ── (d) Unknown order_id → None, no state mutation.
    snapshot_history = list(fresh_store.order_history)
    snapshot_open = dict(fresh_store.open_orders)
    result = await fresh_store.update_order(
        "does-not-exist", status=OrderStatus.FILLED
    )
    assert result is None
    assert fresh_store.order_history == snapshot_history
    assert fresh_store.open_orders == snapshot_open


# ── 3. record_fill updates daily_pnl and paper_balance ─────────────────────
async def test_record_fill_updates_daily_pnl_and_paper_balance(
    fresh_store: DataStore,
) -> None:
    """``record_fill(trade)`` must:
        * add ``trade.pnl`` to ``daily_pnl`` (and ``weekly_pnl``),
        * adjust ``paper_balance`` by ``±trade.price * trade.size``
          (BUY subtracts the cost basis, SELL adds the sale revenue),
        * append the Trade to the ``trades`` list.
    """
    # Pre-conditions: pristine post-ctor state.
    assert fresh_store.daily_pnl == pytest.approx(0.0)
    assert fresh_store.weekly_pnl == pytest.approx(0.0)
    assert fresh_store.paper_balance == pytest.approx(BANKROLL_BASELINE)
    assert fresh_store.trades == []

    # ── BUY trade: pnl=+5.0, cost = 0.50 * 20 = 10.0.
    buy_trade = Trade(
        trade_id="x10-fill-1",
        token_id="TOK_X10_3",
        side=Side.BUY,
        price=0.50,
        size=20.0,
        pnl=5.0,
        strategy="ml_sig_v1",
        paper=True,
    )
    await fresh_store.record_fill(buy_trade)

    # daily_pnl & weekly_pnl accumulate trade.pnl.
    assert fresh_store.daily_pnl == pytest.approx(5.0)
    assert fresh_store.weekly_pnl == pytest.approx(5.0)

    # paper_balance: BANKROLL_BASELINE - (0.50 * 20) = 90.0.
    assert fresh_store.paper_balance == pytest.approx(BANKROLL_BASELINE - 10.0)

    # Trade is appended.
    assert len(fresh_store.trades) == 1
    assert fresh_store.trades[0].trade_id == "x10-fill-1"

    # ── SELL trade: pnl=+8.0, revenue = 0.60 * 10 = 6.0.
    sell_trade = Trade(
        trade_id="x10-fill-2",
        token_id="TOK_X10_3",
        side=Side.SELL,
        price=0.60,
        size=10.0,
        pnl=8.0,
        paper=True,
    )
    await fresh_store.record_fill(sell_trade)

    # daily_pnl: 5.0 + 8.0 = 13.0.
    assert fresh_store.daily_pnl == pytest.approx(13.0)
    assert fresh_store.weekly_pnl == pytest.approx(13.0)

    # paper_balance: 90.0 + (0.60 * 10) = 96.0.
    assert fresh_store.paper_balance == pytest.approx(BANKROLL_BASELINE - 10.0 + 6.0)

    assert len(fresh_store.trades) == 2
    assert fresh_store.trades[1].trade_id == "x10-fill-2"


# ── 4. get_order_book returns book or None ──────────────────────────────────
async def test_get_order_book_returns_book_or_none(fresh_store: DataStore) -> None:
    """``get_order_book(token_id)`` returns the stored ``OrderBook`` for a
    known token and ``None`` for an unknown one."""
    book = OrderBook(
        token_id="TOK_X10_4",
        bids=[PriceLevel(price=0.48, size=20.0)],
        asks=[PriceLevel(price=0.52, size=15.0)],
    )

    # Unknown token → None.
    assert await fresh_store.get_order_book("TOK_X10_4") is None
    assert await fresh_store.get_order_book("UNKNOWN_TOKEN") is None

    # Store the book.
    await fresh_store.update_order_book(book)

    # Known token → the stored OrderBook.
    fetched = await fresh_store.get_order_book("TOK_X10_4")
    assert fetched is not None
    assert fetched is book  # identity, no copy
    assert fetched.token_id == "TOK_X10_4"
    assert fetched.best_bid == pytest.approx(0.48)
    assert fetched.best_ask == pytest.approx(0.52)
    assert fetched.mid == pytest.approx(0.50)
    assert fetched.spread == pytest.approx(0.04)

    # Other tokens remain unknown.
    assert await fresh_store.get_order_book("STILL_UNKNOWN") is None

    # Storing a NEW book for the same token_id replaces the prior one.
    replacement = OrderBook(
        token_id="TOK_X10_4",
        bids=[PriceLevel(price=0.45, size=10.0)],
        asks=[PriceLevel(price=0.55, size=10.0)],
    )
    await fresh_store.update_order_book(replacement)
    fetched_after = await fresh_store.get_order_book("TOK_X10_4")
    assert fetched_after is replacement
    assert fetched_after.best_bid == pytest.approx(0.45)
    assert fetched_after.best_ask == pytest.approx(0.55)


# ── 5. positions dict tracks yes_shares ─────────────────────────────────────
async def test_positions_dict_tracks_yes_shares(fresh_store: DataStore) -> None:
    """The ``positions`` dict must track ``yes_shares`` across multiple
    BUY fills, updating ``avg_entry_price`` as a share-count-weighted
    running average and accumulating ``total_invested``."""
    # No position before any fill.
    assert "TOK_X10_5" not in fresh_store.positions

    # ── First BUY: 25 shares @ 0.40 → yes_shares=25, avg_entry=0.40.
    await fresh_store.record_fill(Trade(
        trade_id="x10-pos-buy-1",
        token_id="TOK_X10_5",
        side=Side.BUY,
        price=0.40,
        size=25.0,
        strategy="ml_sig_v1",
        paper=True,
    ))

    pos = fresh_store.positions["TOK_X10_5"]
    assert pos.yes_shares == pytest.approx(25.0)
    assert pos.avg_entry_price == pytest.approx(0.40)
    assert pos.total_invested == pytest.approx(25.0 * 0.40)  # 10.0
    assert pos.strategy == "ml_sig_v1"

    # ── Second BUY: 15 shares @ 0.60 → yes_shares=40,
    #    avg_entry = (0.40*25 + 0.60*15) / 40 = (10 + 9) / 40 = 0.475.
    await fresh_store.record_fill(Trade(
        trade_id="x10-pos-buy-2",
        token_id="TOK_X10_5",
        side=Side.BUY,
        price=0.60,
        size=15.0,
        strategy="ml_sig_v1",
        paper=True,
    ))

    pos = fresh_store.positions["TOK_X10_5"]
    assert pos.yes_shares == pytest.approx(40.0)
    assert pos.avg_entry_price == pytest.approx(0.475)
    assert pos.total_invested == pytest.approx(10.0 + 9.0)  # 19.0

    # ── SELL: 10 shares @ 0.55 → yes_shares drops to 30.
    await fresh_store.record_fill(Trade(
        trade_id="x10-pos-sell",
        token_id="TOK_X10_5",
        side=Side.SELL,
        price=0.55,
        size=10.0,
        pnl=1.50,
        paper=True,
    ))

    pos = fresh_store.positions["TOK_X10_5"]
    assert pos.yes_shares == pytest.approx(30.0)
    # avg_entry_price is NOT recomputed on SELL (cost basis preserved).
    assert pos.avg_entry_price == pytest.approx(0.475)
    # realised_pnl accumulates trade.pnl on SELL only.
    assert pos.realised_pnl == pytest.approx(1.50)


# ── 6. log_event appends to events list ────────────────────────────────────
async def test_log_event_appends_to_events_list(fresh_store: DataStore) -> None:
    """``log_event(msg)`` must append a timestamped entry to the
    ``event_log`` list; ``get_recent_events(n)`` returns the latest N
    entries (newest-last ordering preserved)."""
    assert fresh_store.event_log == []

    await fresh_store.log_event("order placed")
    await fresh_store.log_event("fill received")

    # Both entries were appended (order preserved).
    assert len(fresh_store.event_log) == 2

    # Each entry contains the original message verbatim, prefixed with a
    # ``[HH:MM:SS]`` timestamp.
    assert "order placed" in fresh_store.event_log[0]
    assert "fill received" in fresh_store.event_log[1]
    for entry in fresh_store.event_log:
        assert entry.startswith("[")
        assert "]" in entry
        # The bracketed prefix is exactly 9 chars: "[HH:MM:SS]".
        assert entry.index("]") == 9

    # ``get_recent_events(n)`` returns the latest n entries.
    recent_one = await fresh_store.get_recent_events(1)
    assert len(recent_one) == 1
    assert "fill received" in recent_one[0]

    recent_two = await fresh_store.get_recent_events(5)
    assert len(recent_two) == 2
    assert "order placed" in recent_two[0]
    assert "fill received" in recent_two[1]

    # ── Event-log cap: at most 500 entries (oldest evicted FIFO).
    for i in range(600):
        await fresh_store.log_event(f"bulk-event-{i}")
    assert len(fresh_store.event_log) == 500
    # Oldest entries were evicted — the first kept entry is bulk-event-100.
    assert fresh_store.event_log[0].startswith("[")
    assert "bulk-event-100" in fresh_store.event_log[0]
    # Newest entry is the last appended bulk-event-599.
    assert "bulk-event-599" in fresh_store.event_log[-1]


# ── 7. total_exposure sums current_exposure ────────────────────────────────
async def test_total_exposure_sums_current_exposure(fresh_store: DataStore) -> None:
    """``total_exposure()`` must return the sum of
    ``Position.current_exposure`` (= ``yes_shares * avg_entry_price``)
    across every entry in ``positions``. An empty store has zero
    exposure."""
    # Empty store → zero exposure.
    assert await fresh_store.total_exposure() == pytest.approx(0.0)

    # Position A: 100 shares @ 0.50 → exposure = 50.0.
    await fresh_store.record_fill(Trade(
        trade_id="x10-exp-1",
        token_id="TOK_A",
        side=Side.BUY,
        price=0.50,
        size=100.0,
        paper=True,
    ))
    assert await fresh_store.total_exposure() == pytest.approx(50.0)
    assert await fresh_store.exposure_for_market("TOK_A") == pytest.approx(50.0)
    assert await fresh_store.exposure_for_market("UNKNOWN") == pytest.approx(0.0)

    # Position B: 50 shares @ 0.40 → exposure = 20.0.
    await fresh_store.record_fill(Trade(
        trade_id="x10-exp-2",
        token_id="TOK_B",
        side=Side.BUY,
        price=0.40,
        size=50.0,
        paper=True,
    ))
    # Aggregate: 50.0 + 20.0 = 70.0.
    assert await fresh_store.total_exposure() == pytest.approx(70.0)

    # ── SELL reduces exposure: 30 shares sold from TOK_A (avg_entry unchanged
    #    at 0.50) → yes_shares=70, exposure=35.0.
    await fresh_store.record_fill(Trade(
        trade_id="x10-exp-3",
        token_id="TOK_A",
        side=Side.SELL,
        price=0.55,
        size=30.0,
        pnl=1.0,
        paper=True,
    ))
    assert await fresh_store.exposure_for_market("TOK_A") == pytest.approx(35.0)
    # Aggregate: 35.0 + 20.0 = 55.0.
    assert await fresh_store.total_exposure() == pytest.approx(55.0)


# ── 8. save_to_disk / load_from_disk round-trip ────────────────────────────
async def test_save_to_disk_load_from_disk_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``save_to_disk()`` followed by ``load_from_disk()`` on a fresh
    ``DataStore`` instance must faithfully round-trip the persisted
    portfolio state: ``daily_pnl``, ``paper_balance``, ``peak_equity``,
    ``equity_history``, ``positions``, and ``trades``.

    ``STATE_FILE`` is monkeypatched to a per-test ``tmp_path`` JSON file
    so the round-trip is fully isolated from any other ``DataStore``
    caller in the suite (including the module-level ``store`` singleton
    whose ``STATE_FILE`` is bound at import time to the shared
    ``/tmp/pmbot_conftest_isolation/store_state.json``).
    """
    state_path = tmp_path / "round_trip_state.json"
    monkeypatch.setattr("core.data_store.STATE_FILE", state_path)

    # Source store: fresh DataStore populated with a single BUY fill.
    # ``DataStore.__init__`` does NOT call ``load_from_disk`` (that call
    # lives at module level on the singleton), so the returned instance
    # starts with empty containers and factory-default scalars.
    store_src = DataStore()
    await store_src.record_fill(Trade(
        trade_id="x10-rt-1",
        token_id="TOK_RT",
        side=Side.BUY,
        price=0.50,
        size=20.0,
        strategy="ml_sig_v1",
        paper=True,
        pnl=5.0,
    ))

    # Snapshot the post-fill state we expect to round-trip.
    expected_daily_pnl = store_src.daily_pnl             # 5.0
    expected_paper_balance = store_src.paper_balance      # 100 - 10 = 90.0
    expected_peak_equity = store_src.peak_equity          # max(100, 105) = 105.0
    expected_equity_len = len(store_src.equity_history)   # 2 (bootstrap + fill)
    expected_yes_shares = store_src.positions["TOK_RT"].yes_shares           # 20.0
    expected_avg_entry = store_src.positions["TOK_RT"].avg_entry_price      # 0.50
    expected_total_invested = store_src.positions["TOK_RT"].total_invested   # 10.0
    expected_strategy = store_src.positions["TOK_RT"].strategy               # "ml_sig_v1"
    expected_trade_count = len(store_src.trades)         # 1

    # NOTE: ``weekly_pnl`` is intentionally NOT persisted by
    # ``save_to_disk`` — it is a session-scoped figure reset by
    # ``roll_weekly_window`` every 7 days, so it should NOT round-trip.
    # We assert that contract explicitly below (after load).

    # Sanity: the BUY trade landed the expected accounting.
    assert expected_daily_pnl == pytest.approx(5.0)
    assert expected_paper_balance == pytest.approx(BANKROLL_BASELINE - 10.0)
    assert expected_peak_equity == pytest.approx(BANKROLL_BASELINE + 5.0)

    # ── Save. The tmp file must appear on disk.
    store_src.save_to_disk()
    assert state_path.exists(), "save_to_disk did not create the state file"
    assert state_path.stat().st_size > 0

    # ── Load into a fresh instance (no in-memory shared state).
    store_dst = DataStore()
    # Pre-load: pristine factory defaults.
    assert store_dst.daily_pnl == pytest.approx(0.0)
    assert store_dst.paper_balance == pytest.approx(BANKROLL_BASELINE)
    assert store_dst.positions == {}
    assert store_dst.trades == []

    store_dst.load_from_disk()

    # ── Scalar ledger fields round-trip.
    assert store_dst.daily_pnl == pytest.approx(expected_daily_pnl)
    assert store_dst.paper_balance == pytest.approx(expected_paper_balance)
    assert store_dst.peak_equity == pytest.approx(expected_peak_equity)

    # ── ``weekly_pnl`` is NOT persisted by ``save_to_disk`` (it is
    #    session-scoped and reset every 7 days by ``roll_weekly_window``),
    #    so it stays at the factory default 0.0 after load.
    assert store_dst.weekly_pnl == pytest.approx(0.0)

    # ── Equity-history length and trailing point round-trip.
    assert len(store_dst.equity_history) == expected_equity_len
    # Latest equity-history point matches the post-fill snapshot.
    last_pt = store_dst.equity_history[-1]
    assert last_pt["equity"] == pytest.approx(BANKROLL_BASELINE + expected_daily_pnl)
    assert last_pt["pnl"] == pytest.approx(expected_daily_pnl)

    # ── Trades round-trip with full identity.
    assert len(store_dst.trades) == expected_trade_count
    rt_trade = store_dst.trades[0]
    assert rt_trade.trade_id == "x10-rt-1"
    assert rt_trade.token_id == "TOK_RT"
    assert rt_trade.side == Side.BUY
    assert rt_trade.price == pytest.approx(0.50)
    assert rt_trade.size == pytest.approx(20.0)
    assert rt_trade.pnl == pytest.approx(5.0)
    assert rt_trade.strategy == "ml_sig_v1"
    assert rt_trade.paper is True

    # ── Positions round-trip with full Position dataclass state.
    assert "TOK_RT" in store_dst.positions
    pos = store_dst.positions["TOK_RT"]
    assert pos.token_id == "TOK_RT"
    assert pos.yes_shares == pytest.approx(expected_yes_shares)
    assert pos.avg_entry_price == pytest.approx(expected_avg_entry)
    assert pos.total_invested == pytest.approx(expected_total_invested)
    assert pos.strategy == expected_strategy

    # ── Round-trip preserves the exposure computation (derived from
    #    yes_shares * avg_entry_price).
    assert await store_dst.total_exposure() == pytest.approx(
        expected_yes_shares * expected_avg_entry
    )
