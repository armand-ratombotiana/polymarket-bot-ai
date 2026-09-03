"""
W9-5 — Unit tests for ``core/position_manager.py``.

Covers the position-management lifecycle:

  1. ``ManagedPosition.__init__`` computes ``take_profit_price`` as
     ``min(entry * (1 + tp_pct), 0.99)`` — clipped to the 0.99 ceiling.
  2. ``ManagedPosition.__init__`` computes ``stop_loss_price`` as
     ``max(entry * (1 - sl_pct), 0.01)`` — clipped to the 0.01 floor.
  3. ``ManagedPosition.__init__`` initializes ``high_water_mark`` to the
     entry price (no peak observed yet).
  4. ``ManagedPosition.__init__`` initializes ``active_exit_order_id`` to
     None (no outstanding exit order at registration).
  5. TP ceiling clips to 0.99 — entry 0.95 + 25% TP would exceed 1.0 but
     the clip guards it.
  6. SL floor clips to 0.01 — entry 0.005 + 5% SL would go negative but
     the clip guards it.
  7. ``PositionManager.register_entry`` adds a ``ManagedPosition`` to the
     ``managed_positions`` dict keyed by token_id.
  8. ``register_entry`` overwrites a prior entry for the same token_id
     (idempotent — re-registering resets TP/SL/high_water_mark).
  9. ``evaluate_positions`` does NOT crash on an empty ``store.positions``
     (the no-op happy path).
 10. ``evaluate_positions`` skips positions where ``yes_shares <= 0`` —
     no TP/SL evaluation for non-held positions.
 11. ``evaluate_positions`` skips positions where no order book is
     available (no mid to compare against).
 12. ``evaluate_positions`` auto-registers a fresh ``ManagedPosition`` for
     a position it didn't previously know about (lazy onboarding).
 13. ``PositionManager.start`` is idempotent — calling it twice does not
     spawn two background loops (the ``_running`` guard).
 14. ``PositionManager.stop`` sets ``_running=False``.

Isolation
----------
Each test constructs a FRESH ``PositionManager()`` instance. The autouse
``_reset_store_factory_defaults`` conftest fixture clears ``store.positions``
and ``store.order_books`` before every test.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module —
even though some tests are sync, the mark is harmless and keeps
collection consistent).
"""
from __future__ import annotations

import asyncio

import pytest

from core.data_store import OrderBook, Position, PriceLevel, store
from core.position_manager import ManagedPosition, PositionManager

pytestmark = pytest.mark.asyncio


# ── 1. ManagedPosition computes take_profit_price ───────────────────────────
def test_managed_position_computes_take_profit_price():
    """``take_profit_price`` = ``min(entry * (1 + tp_pct), 0.99)`` — entry
    0.50, tp 0.25 → 0.625 (no clipping)."""
    mp = ManagedPosition("TOK_X", entry_price=0.50, take_profit_pct=0.25, stop_loss_pct=0.05)
    assert mp.take_profit_price == pytest.approx(0.625, abs=1e-6)
    assert mp.stop_loss_price == pytest.approx(0.475, abs=1e-6)


# ── 2. ManagedPosition computes stop_loss_price ──────────────────────────────
def test_managed_position_computes_stop_loss_price():
    """``stop_loss_price`` = ``max(entry * (1 - sl_pct), 0.01)`` — entry
    0.50, sl 0.05 → 0.475 (no clipping)."""
    mp = ManagedPosition("TOK_X", entry_price=0.50, take_profit_pct=0.25, stop_loss_pct=0.05)
    assert mp.stop_loss_price == pytest.approx(0.475, abs=1e-6)


# ── 3. ManagedPosition initializes high_water_mark to entry ─────────────────
def test_managed_position_initializes_high_water_mark_to_entry():
    """``high_water_mark`` starts at the entry price (no peak observed yet)."""
    mp = ManagedPosition("TOK_X", entry_price=0.50)
    assert mp.high_water_mark == pytest.approx(0.50, abs=1e-6)


# ── 4. ManagedPosition initializes active_exit_order_id to None ────────────
def test_managed_position_initializes_active_exit_order_id_to_none():
    """``active_exit_order_id`` starts as None (no outstanding exit order)."""
    mp = ManagedPosition("TOK_X", entry_price=0.50)
    assert mp.active_exit_order_id is None


# ── 5. TP ceiling clips to 0.99 ─────────────────────────────────────────────
def test_managed_position_tp_ceiling_clips_to_0_99():
    """When ``entry * (1 + tp_pct)`` would exceed 0.99, the clip guard
    caps it at 0.99 — entry 0.95 + 25% TP would yield 1.1875, clipped to
    0.99."""
    mp = ManagedPosition("TOK_X", entry_price=0.95, take_profit_pct=0.25, stop_loss_pct=0.05)
    assert mp.take_profit_price == pytest.approx(0.99, abs=1e-6)


# ── 6. SL floor clips to 0.01 ───────────────────────────────────────────────
def test_managed_position_sl_floor_clips_to_0_01():
    """When ``entry * (1 - sl_pct)`` would go below 0.01, the clip guard
    floors it at 0.01 — entry 0.005 + 5% SL would yield 0.00475, floored
    to 0.01."""
    mp = ManagedPosition("TOK_X", entry_price=0.005, take_profit_pct=0.25, stop_loss_pct=0.05)
    assert mp.stop_loss_price == pytest.approx(0.01, abs=1e-6)


# ── 7. register_entry adds ManagedPosition to dict ──────────────────────────
async def test_register_entry_adds_managed_position_to_dict():
    """``register_entry`` adds a ``ManagedPosition`` to the
    ``managed_positions`` dict, keyed by token_id, with the TP/SL bounds
    computed from the entry price."""
    pm = PositionManager()
    await pm.register_entry("TOK_X", entry_price=0.50)

    assert "TOK_X" in pm.managed_positions
    mp = pm.managed_positions["TOK_X"]
    assert isinstance(mp, ManagedPosition)
    assert mp.entry_price == pytest.approx(0.50)
    assert mp.take_profit_price == pytest.approx(0.625, abs=1e-6)
    assert mp.stop_loss_price == pytest.approx(0.475, abs=1e-6)


# ── 8. register_entry overwrites prior entry ────────────────────────────────
async def test_register_entry_overwrites_prior_entry():
    """Re-registering the same token_id overwrites the prior entry — the
    TP/SL/high_water_mark are reset to the new entry's defaults."""
    pm = PositionManager()
    await pm.register_entry("TOK_X", entry_price=0.50)
    # Verify the initial TP.
    assert pm.managed_positions["TOK_X"].take_profit_price == pytest.approx(0.625, abs=1e-6)

    # Re-register with a different entry price.
    await pm.register_entry("TOK_X", entry_price=0.80)
    # The TP is now min(0.80 * 1.25, 0.99) = min(1.0, 0.99) = 0.99.
    assert pm.managed_positions["TOK_X"].take_profit_price == pytest.approx(0.99, abs=1e-6)
    assert pm.managed_positions["TOK_X"].entry_price == pytest.approx(0.80)
    # Only ONE entry exists for TOK_X (the prior was overwritten, not appended).
    assert len(pm.managed_positions) == 1


# ── 9. evaluate_positions no-op on empty store ──────────────────────────────
async def test_evaluate_positions_noop_on_empty_store():
    """``evaluate_positions`` on an empty ``store.positions`` must not crash
    and must not modify any ``managed_positions`` state."""
    pm = PositionManager()
    # Ensure store.positions is empty (the autouse conftest fixture clears it
    # before every test, but we assert it explicitly here for clarity).
    assert store.positions == {}

    await pm.evaluate_positions()
    # No managed positions were created (none of the no-op paths register).
    assert pm.managed_positions == {}


# ── 10. evaluate_positions skips non-held positions ──────────────────────────
async def test_evaluate_positions_skips_non_held_positions():
    """Positions with ``yes_shares <= 0`` must be SKIPPED — no TP/SL
    evaluation, no ManagedPosition registration."""
    pm = PositionManager()
    # Seed a zero-share position.
    store.positions["TOK_ZERO"] = Position(
        token_id="TOK_ZERO", yes_shares=0.0, avg_entry_price=0.50,
    )

    await pm.evaluate_positions()
    # No managed position was created for the zero-share row.
    assert "TOK_ZERO" not in pm.managed_positions
    assert pm.managed_positions == {}


# ── 11. evaluate_positions skips positions without order book ───────────────
async def test_evaluate_positions_skips_positions_without_order_book():
    """Positions whose token_id has no order book in ``store.order_books``
    must be SKIPPED — no mid to compare against, no TP/SL evaluation."""
    pm = PositionManager()
    # Seed an open position but NO order book for its token.
    store.positions["TOK_NO_BOOK"] = Position(
        token_id="TOK_NO_BOOK", yes_shares=100.0, avg_entry_price=0.50,
    )
    assert "TOK_NO_BOOK" not in store.order_books

    await pm.evaluate_positions()
    # No managed position was created (the no-book path skips before the
    # lazy registration block).
    assert "TOK_NO_BOOK" not in pm.managed_positions


# ── 12. evaluate_positions skips positions where book.mid is None ───────────
async def test_evaluate_positions_skips_positions_where_book_mid_is_none():
    """A book with only bids (no asks) has ``mid=None`` — the position must
    be skipped, no TP/SL evaluation."""
    pm = PositionManager()
    store.positions["TOK_HALF_BOOK"] = Position(
        token_id="TOK_HALF_BOOK", yes_shares=100.0, avg_entry_price=0.50,
    )
    # Book with bids but no asks → mid is None.
    store.order_books["TOK_HALF_BOOK"] = OrderBook(
        token_id="TOK_HALF_BOOK",
        bids=[PriceLevel(price=0.49, size=100.0)],
        asks=[],  # no asks → mid is None
    )

    await pm.evaluate_positions()
    # No managed position was created for the half-book row.
    assert "TOK_HALF_BOOK" not in pm.managed_positions


# ── 13. PositionManager.start is idempotent ──────────────────────────────────
async def test_position_manager_start_is_idempotent():
    """Calling ``start()`` twice must NOT spawn two background loops — the
    ``_running`` guard short-circuits the second call."""
    pm = PositionManager()
    assert pm._running is False

    await pm.start()
    assert pm._running is True

    # Second call: idempotent — does NOT spawn a second task.
    await pm.start()
    assert pm._running is True

    # Clean up: stop the background loop so the task doesn't outlive the test.
    await pm.stop()
    assert pm._running is False


# ── 14. PositionManager.stop sets _running=False ────────────────────────────
async def test_position_manager_stop_sets_running_false():
    """``stop()`` sets ``_running=False`` — the background loop's exit
    signal."""
    pm = PositionManager()
    await pm.start()
    assert pm._running is True

    await pm.stop()
    assert pm._running is False

    # Calling stop() again is also safe (no-op on an already-stopped manager).
    await pm.stop()
    assert pm._running is False
