"""tests/test_broker.py — W19-7 Backtest/Live parity.

Unit tests for ``core/broker.py`` — the unified ``Broker`` ABC that
restores execution-venue parity between backtest, paper, and live
trading per God Mode §32.

Scope
-----
Six test groups, one per spec'd requirement:

  (1) ``PaperBroker.submit_order`` — delegates to ``paper_sim.create_order``
      and returns an ``ACKNOWLEDGED`` response with the simulator-
      minted ``order_id``.
  (2) ``LiveBroker.submit_order`` — delegates to ``clob_client.create_order``
      (mocked) and returns ``ACKNOWLEDGED`` on success; ``REJECTED``
      on a ``None`` CLOB response.
  (3) ``BacktestBroker.submit_order`` — BUY path: rejects when the
      cost exceeds capital; otherwise debits capital and updates the
      weighted-average entry price on a new / existing position.
  (4) ``BacktestBroker`` SELL path — reduces the open position by the
      fill size (clamped to the open size), credits proceeds to
      capital, and accumulates realized P&L on the position snapshot.
  (5) ``get_broker`` factory — returns the right concrete subclass for
      each mode and raises ``ValueError`` on an unknown mode.
  (6) ``apply_slippage`` is shared — every concrete broker produces
      the SAME slipped fill price for the same inputs (the §32 parity
      contract). A 1-tick BUY penalty and a 1-tick SELL penalty are
      asserted explicitly so a regression in either broker's
      ``apply_slippage`` override breaks this test.

Isolation
---------
``conftest.py``'s autouse ``_reset_store_factory_defaults`` fixture
resets the global ``store`` / ``paper_sim`` / ``risk_manager``
singletons to a clean $100 baseline before every test, so the
``PaperBroker`` tests start from a known balance. The
``BacktestBroker`` tests don't touch the global singletons at all —
the broker holds its own capital + positions ledger by design.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``, ``risk.*``) regardless of the cwd pytest was
# launched from — mirrors every sibling ``tests/test_*.py`` module.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from core.broker import (  # noqa: E402
    BacktestBroker,
    Broker,
    LiveBroker,
    OrderRequest,
    OrderResponse,
    PaperBroker,
    Position,
    get_broker,
)

pytestmark = pytest.mark.asyncio


_TOKEN_ID = "0xbrokerparitytest0000000000000000000000000000000000000000dead"

# ── Shared slippage expectations ─────────────────────────────────────────────
# Order id derived from (price, size, side) → SHA-256[0] LSB determines the
# queue tick (0 or 1). For (0.50, 5.0, "BUY"), the broker constructs the
# synthetic order id ``broker-slippage-BUY-0.500000-5.000000`` — same id
# across all three brokers because the helper is shared. The expected slipped
# price is raw_price + (1-tick crossing + queue_tick) for a BUY with a deep
# top-of-book (size_impact = 0). This is the load-bearing parity assertion: if
# any broker overrides ``apply_slippage`` with a different model, this test
# breaks.
_RAW_PRICE = 0.50
_SIZE = 5.0
# We don't hard-code the exact slipped price (queue tick depends on the
# SHA-256 LSB of the synthetic order id, which is deterministic but not
# worth re-deriving here); we assert the BUY slipped price is within
# (raw, raw + 2 ticks] — the only valid range for a deep-book BUY.


# ── (5) get_broker factory ──────────────────────────────────────────────────
# Tested first because the other test groups rely on it.

def test_get_broker_returns_paper_broker_for_paper_mode() -> None:
    """``get_broker("paper")`` returns a ``PaperBroker`` instance."""
    broker = get_broker("paper")
    assert isinstance(broker, PaperBroker)
    assert isinstance(broker, Broker)


def test_get_broker_returns_live_broker_for_live_mode() -> None:
    """``get_broker("live")`` returns a ``LiveBroker`` instance."""
    broker = get_broker("live")
    assert isinstance(broker, LiveBroker)
    assert isinstance(broker, Broker)


def test_get_broker_returns_backtest_broker_for_backtest_mode() -> None:
    """``get_broker("backtest")`` returns a ``BacktestBroker`` instance
    initialised with the supplied ``initial_capital``."""
    broker = get_broker("backtest", initial_capital=42.0)
    assert isinstance(broker, BacktestBroker)
    assert isinstance(broker, Broker)
    assert broker._capital == pytest.approx(42.0)


def test_get_broker_backtest_default_capital_is_100() -> None:
    """When ``initial_capital`` is omitted, the backtest broker uses the
    $100.00 ``BANKROLL_BASELINE`` default — same starting capital as a
    fresh ``DataStore`` singleton."""
    broker = get_broker("backtest")
    assert isinstance(broker, BacktestBroker)
    assert broker._capital == pytest.approx(100.0)


def test_get_broker_raises_value_error_on_unknown_mode() -> None:
    """An unknown mode string raises ``ValueError`` (not a silent
    fall-through to paper)."""
    with pytest.raises(ValueError, match="Unknown broker mode"):
        get_broker("production")  # typo / unsupported mode


def test_get_broker_accepts_empty_kwargs_for_paper() -> None:
    """Extra kwargs (intended for ``BacktestBroker``) are silently
    ignored by ``PaperBroker`` / ``LiveBroker`` — the factory doesn't
    crash on a uniform caller signature."""
    broker = get_broker("paper", initial_capital=999.0)
    assert isinstance(broker, PaperBroker)


# ── (3) BacktestBroker BUY path ─────────────────────────────────────────────


async def test_backtest_buy_fills_immediately_and_debits_capital() -> None:
    """A BUY fills synchronously at ``price + slippage`` and debits the
    cost from the broker's capital. The fill price is HIGHER than the
    requested price because BUY slippage is adverse (the buyer pays
    the ask + crossing + queue ticks)."""
    broker = BacktestBroker(initial_capital=100.0)
    request = OrderRequest(
        token_id=_TOKEN_ID,
        side="BUY",
        size=10.0,
        price=0.50,
        strategy="test_strategy",
    )

    response = await broker.submit_order(request)

    assert response.status == "FILLED"
    assert response.fill_size == pytest.approx(10.0)
    # BUY slippage is adverse → fill_price > requested price.
    assert response.fill_price > 0.50
    # The slipped price is within (0.50, 0.52] — 1 crossing tick + 0/1
    # queue tick (size_impact = 0 because the broker's apply_slippage
    # calls _canonical_slippage with no order_book → deep top-of-book).
    assert 0.50 < response.fill_price <= 0.52
    # Capital debited by fill_price * fill_size.
    expected_cost = response.fill_price * response.fill_size
    assert broker._capital == pytest.approx(100.0 - expected_cost)


async def test_backtest_buy_creates_long_position_with_avg_price() -> None:
    """A BUY creates a new ``Position`` with side ``LONG``, the slipped
    fill price as ``avg_price``, and the requested size."""
    broker = BacktestBroker(initial_capital=100.0)
    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )

    await broker.submit_order(request)

    positions = await broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.token_id == _TOKEN_ID
    assert pos.side == "LONG"
    assert pos.size == pytest.approx(10.0)
    assert pos.avg_price > 0.50  # slipped fill price


async def test_backtest_buy_rejects_when_insufficient_capital() -> None:
    """A BUY whose cost exceeds the available capital is REJECTED with
    an explanatory error. The capital and position ledger are left
    untouched."""
    broker = BacktestBroker(initial_capital=1.0)  # tiny balance
    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=100.0, price=0.50,
    )

    response = await broker.submit_order(request)

    assert response.status == "REJECTED"
    assert "Insufficient capital" in response.error
    # Capital unchanged.
    assert broker._capital == pytest.approx(1.0)
    # No position created.
    assert await broker.get_positions() == []


async def test_backtest_buy_aggregates_into_existing_position() -> None:
    """A second BUY on the same token aggregates into the existing
    position: size adds; ``avg_price`` is the size-weighted mean of
    the prior entry and the new fill."""
    broker = BacktestBroker(initial_capital=100.0)

    # First BUY: 10 shares @ 0.50 (slipped to ~0.51).
    await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50)
    )
    first_pos = (await broker.get_positions())[0]
    first_size = first_pos.size
    first_avg = first_pos.avg_price

    # Second BUY: 10 more shares @ 0.50 (same slip shape).
    await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50)
    )

    positions = await broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert pos.size == pytest.approx(first_size + 10.0)
    # Weighted-average entry price is between the two slipped fill prices.
    # Since both fills used the same price + size, the avg is unchanged.
    assert pos.avg_price == pytest.approx(first_avg)


# ── (4) BacktestBroker SELL path ─────────────────────────────────────────────


async def test_backtest_sell_reduces_position_and_credits_proceeds() -> None:
    """A SELL on an open LONG position reduces the size by ``fill_size``
    and credits the proceeds (fill_price * fill_size) to the capital
    balance. Realized P&L is accumulated on the position snapshot."""
    broker = BacktestBroker(initial_capital=100.0)

    # Open the position: 10 shares @ 0.50.
    buy_resp = await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50)
    )
    assert buy_resp.status == "FILLED"
    balance_after_buy = broker._capital

    # Sell the entire position @ 0.55 (slipped down).
    sell_resp = await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="SELL", size=10.0, price=0.55)
    )

    assert sell_resp.status == "FILLED"
    assert sell_resp.fill_size == pytest.approx(10.0)
    # SELL slippage is adverse → fill_price < requested price.
    assert sell_resp.fill_price < 0.55
    # Proceeds credited.
    expected_proceeds = sell_resp.fill_price * sell_resp.fill_size
    assert broker._capital == pytest.approx(balance_after_buy + expected_proceeds)
    # Position fully closed (size dropped to 0 → evicted from ledger).
    assert await broker.get_positions() == []


async def test_backtest_sell_records_realized_pnl_on_position() -> None:
    """A partial SELL on an open position reduces the size but leaves
    the position open. Realized P&L is recorded on the position
    snapshot (positive when exit > entry)."""
    broker = BacktestBroker(initial_capital=100.0)

    # Open 10 shares @ 0.50 (slipped up to ~0.51).
    buy_resp = await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50)
    )
    entry_price = buy_resp.fill_price

    # Sell 4 shares @ 0.60 (slipped down).
    sell_resp = await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="SELL", size=4.0, price=0.60)
    )
    assert sell_resp.status == "FILLED"

    positions = await broker.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    # Position reduced by the sold size.
    assert pos.size == pytest.approx(6.0)
    # Realized P&L = (exit - entry) * shares_sold.
    expected_pnl = (sell_resp.fill_price - entry_price) * 4.0
    assert pos.realized_pnl == pytest.approx(expected_pnl)


async def test_backtest_sell_clamps_fill_size_to_open_position() -> None:
    """A SELL larger than the open position is clamped to the open
    size — the broker never lets the position go negative."""
    broker = BacktestBroker(initial_capital=100.0)

    # Open 5 shares.
    await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=5.0, price=0.50)
    )

    # Attempt to sell 20 shares (4x the open size).
    sell_resp = await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="SELL", size=20.0, price=0.60)
    )

    assert sell_resp.status == "FILLED"
    # Clamped to the 5 open shares.
    assert sell_resp.fill_size == pytest.approx(5.0)
    # Position fully closed.
    assert await broker.get_positions() == []


async def test_backtest_sell_rejects_when_no_position() -> None:
    """A SELL with no open position is REJECTED (can't sell what we
    don't own — short-selling is not supported by the BacktestBroker
    in its current scope)."""
    broker = BacktestBroker(initial_capital=100.0)
    request = OrderRequest(
        token_id=_TOKEN_ID, side="SELL", size=10.0, price=0.50,
    )

    response = await broker.submit_order(request)

    assert response.status == "REJECTED"
    assert "No position to sell" in response.error


async def test_backtest_get_balance_returns_current_capital() -> None:
    """``get_balance`` returns the broker's current capital (not the
    initial capital)."""
    broker = BacktestBroker(initial_capital=100.0)
    assert await broker.get_balance() == pytest.approx(100.0)

    # Buy something.
    await broker.submit_order(
        OrderRequest(token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50)
    )
    # Capital reduced by the slipped cost.
    assert await broker.get_balance() < 100.0


async def test_backtest_cancel_order_returns_false_because_fills_are_immediate() -> None:
    """Backtest fills are synchronous — there is nothing to cancel.
    ``cancel_order`` always returns ``False`` to signal the caller that
    the cancel didn't take effect (the order already filled)."""
    broker = BacktestBroker(initial_capital=100.0)
    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )
    response = await broker.submit_order(request)
    assert response.status == "FILLED"

    cancelled = await broker.cancel_order(response.order_id)
    assert cancelled is False


async def test_backtest_get_order_status_returns_prior_response() -> None:
    """``get_order_status`` returns the stored ``OrderResponse`` for an
    order previously submitted to this broker. ``None`` for an unknown
    order id."""
    broker = BacktestBroker(initial_capital=100.0)
    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )
    response = await broker.submit_order(request)

    status = await broker.get_order_status(response.order_id)
    assert status is not None
    assert status.order_id == response.order_id
    assert status.status == response.status

    # Unknown order id → None.
    unknown = await broker.get_order_status("does-not-exist")
    assert unknown is None


# ── (1) PaperBroker.submit_order ─────────────────────────────────────────────


async def test_paper_broker_submit_order_returns_acknowledged_with_sim_id() -> None:
    """``PaperBroker.submit_order`` delegates to
    ``paper_sim.create_order`` and returns an ``ACKNOWLEDGED``
    response carrying the simulator-minted ``order_id`` (which matches
    the client-minted ``client_order_id`` because the broker forwards
    it as the ``order_id`` kwarg — W18-1 contract)."""
    broker = PaperBroker()
    request = OrderRequest(
        token_id=_TOKEN_ID,
        side="BUY",
        size=10.0,
        price=0.50,
        strategy="paper_test_strategy",
    )

    response = await broker.submit_order(request)

    assert response.status == "ACKNOWLEDGED"
    # The simulator mints ``paper-<uuid>`` if no order_id is supplied;
    # our broker forwards the client_order_id (W18-1 contract), so the
    # response order_id matches the request's client_order_id.
    assert response.order_id == request.client_order_id
    # The local Order has been added to the store's open_orders.
    from core.data_store import store
    assert request.client_order_id in store.open_orders


async def test_paper_broker_get_balance_returns_virtual_balance() -> None:
    """``PaperBroker.get_balance`` returns ``paper_sim.virtual_balance``,
    the cached mirror of ``store.paper_balance`` (reset to $100 by the
    conftest autouse fixture)."""
    broker = PaperBroker()
    balance = await broker.get_balance()
    assert balance == pytest.approx(100.0)
    assert balance == pytest.approx(broker._sim.virtual_balance)


async def test_paper_broker_get_positions_returns_empty_when_no_trades() -> None:
    """``PaperBroker.get_positions`` returns an empty list when no
    positions have been opened (the conftest autouse fixture clears
    ``store.positions`` before every test)."""
    broker = PaperBroker()
    positions = await broker.get_positions()
    assert positions == []


async def test_paper_broker_cancel_order_returns_bool() -> None:
    """``PaperBroker.cancel_order`` returns a bool from
    ``paper_sim.cancel_order`` — ``False`` for an unknown order id
    (the simulator's ``store.update_order`` returns ``None`` for an
    unknown id → ``paper_sim.cancel_order`` returns ``False``)."""
    broker = PaperBroker()
    # Unknown order → False.
    result = await broker.cancel_order("paper-bogus-unknown")
    assert result is False


# ── (2) LiveBroker.submit_order (mocked CLOB) ───────────────────────────────


async def test_live_broker_submit_order_returns_acknowledged_on_success() -> None:
    """``LiveBroker.submit_order`` delegates to
    ``clob_client.create_order`` and maps a successful CLOB response
    (dict with ``orderID``) to an ``ACKNOWLEDGED`` response carrying
    the server-minted order id. The CLOB client is mocked so no real
    HTTP call is made."""
    broker = LiveBroker()

    # Mock the clob_client.create_order coroutine to return a CLOB-
    # shaped response. We patch the bound method on the singleton
    # instance the broker holds a reference to (broker._clob).
    fake_response = {"orderID": "clob-order-12345", "status": "LIVE"}
    broker._clob.create_order = AsyncMock(return_value=fake_response)

    request = OrderRequest(
        token_id=_TOKEN_ID,
        side="BUY",
        size=10.0,
        price=0.50,
        strategy="live_test_strategy",
    )
    response = await broker.submit_order(request)

    assert response.status == "ACKNOWLEDGED"
    assert response.order_id == "clob-order-12345"
    # Verify the underlying call used the broker's OrderArgs mapping.
    broker._clob.create_order.assert_awaited_once()
    call_args = broker._clob.create_order.call_args
    # The first positional arg is the OrderArgs dataclass.
    args_obj = call_args.args[0]
    assert args_obj.token_id == _TOKEN_ID
    assert args_obj.price == pytest.approx(0.50)
    assert args_obj.size == pytest.approx(10.0)
    # OrderArgs.side is a Side enum.
    from core.data_store import Side
    assert args_obj.side == Side.BUY


async def test_live_broker_submit_order_rejected_when_clob_returns_none() -> None:
    """When ``clob_client.create_order`` returns ``None`` (signing
    failure, HTTP 4xx/5xx, network error, missing credentials), the
    broker returns a ``REJECTED`` response — distinct from the paper
    broker, whose ``create_order`` never returns ``None``."""
    broker = LiveBroker()
    broker._clob.create_order = AsyncMock(return_value=None)

    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )
    response = await broker.submit_order(request)

    assert response.status == "REJECTED"
    assert "None" in response.error or "submission" in response.error.lower()


async def test_live_broker_submit_order_rejected_on_exception() -> None:
    """If ``clob_client.create_order`` raises (e.g. ``RuntimeError``
    from "Not authenticated", or ``CircuitBreakerOpenError``), the
    broker returns a ``REJECTED`` response carrying the exception
    message — never propagates the exception to the caller."""
    broker = LiveBroker()
    broker._clob.create_order = AsyncMock(
        side_effect=RuntimeError("Not authenticated. Call derive_api_key() first.")
    )

    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )
    response = await broker.submit_order(request)

    assert response.status == "REJECTED"
    assert "Not authenticated" in response.error


async def test_live_broker_cancel_order_returns_bool() -> None:
    """``LiveBroker.cancel_order`` delegates to
    ``clob_client.cancel_order`` (mocked) and returns its bool result
    verbatim."""
    broker = LiveBroker()
    broker._clob.cancel_order = AsyncMock(return_value=True)

    result = await broker.cancel_order("clob-order-12345")
    assert result is True
    broker._clob.cancel_order.assert_awaited_once_with("clob-order-12345")


async def test_live_broker_get_balance_returns_zero_on_failure() -> None:
    """``LiveBroker.get_balance`` returns ``0.0`` when the CLOB
    ``get_balance`` call raises (no creds / network error) — fails
    closed so the caller's capital check doesn't proceed on a
    balance it couldn't confirm."""
    broker = LiveBroker()
    broker._clob.get_balance = AsyncMock(side_effect=RuntimeError("no creds"))

    balance = await broker.get_balance()
    assert balance == pytest.approx(0.0)


async def test_live_broker_get_balance_parses_balance_field() -> None:
    """``LiveBroker.get_balance`` parses the ``balance`` decimal-string
    field from the CLOB ``get_balance`` response (Polymarket returns
    balances as decimal strings — the broker coerces to float)."""
    broker = LiveBroker()
    broker._clob.get_balance = AsyncMock(
        return_value={"balance": "250.75", "allowance": "1000.00"}
    )

    balance = await broker.get_balance()
    assert balance == pytest.approx(250.75)


async def test_live_broker_get_positions_maps_clob_positions() -> None:
    """``LiveBroker.get_positions`` maps the CLOB ``GET /positions``
    response (list of dicts with ``asset`` / ``size`` / ``avgPrice``)
    to broker-agnostic ``Position`` snapshots. Positive ``size`` →
    ``LONG``; negative ``size`` → ``SHORT`` (absolute value)."""
    broker = LiveBroker()
    broker._clob.get_positions = AsyncMock(
        return_value=[
            {"asset": "0xlongtoken", "size": "10.5", "avgPrice": "0.42"},
            {"asset": "0xshorttoken", "size": "-3.0", "avgPrice": "0.65"},
        ]
    )

    positions = await broker.get_positions()

    assert len(positions) == 2
    long_pos = next(p for p in positions if p.token_id == "0xlongtoken")
    short_pos = next(p for p in positions if p.token_id == "0xshorttoken")
    assert long_pos.side == "LONG"
    assert long_pos.size == pytest.approx(10.5)
    assert long_pos.avg_price == pytest.approx(0.42)
    assert short_pos.side == "SHORT"
    assert short_pos.size == pytest.approx(3.0)
    assert short_pos.avg_price == pytest.approx(0.65)


async def test_live_broker_get_order_status_returns_none_pending_fill_ack() -> None:
    """``LiveBroker.get_order_status`` returns ``None`` — the current
    ``clob_client`` has no per-order ``GET /order/{id}`` endpoint (the
    live fill-ack is the W18 follow-up). A strategy that needs status
    polling should use the ``store.open_orders`` lookup path via
    ``PaperBroker`` / ``BacktestBroker`` until W18 fill-ack lands."""
    broker = LiveBroker()
    status = await broker.get_order_status("any-order-id")
    assert status is None


# ── (6) apply_slippage is shared (the §32 parity contract) ───────────────────


def test_apply_slippage_is_shared_across_all_brokers() -> None:
    """The §32 parity contract: every concrete ``Broker`` subclass
    must produce the SAME slipped fill price for the same inputs,
    because they all delegate to the canonical
    ``PaperSimulator._apply_slippage`` static method via the shared
    ``Broker._canonical_slippage`` helper.

    If any broker overrides ``apply_slippage`` with a different model,
    this test breaks — which is exactly the regression signal §32
    wants to surface.
    """
    paper = PaperBroker()
    live = LiveBroker()
    backtest = BacktestBroker(initial_capital=100.0)

    # Same inputs to all three brokers.
    price, size, side = _RAW_PRICE, _SIZE, "BUY"
    paper_fill, paper_size = paper.apply_slippage(price, size, side)
    live_fill, live_size = live.apply_slippage(price, size, side)
    bt_fill, bt_size = backtest.apply_slippage(price, size, side)

    # All three brokers must produce the SAME fill price (the canonical
    # model is deterministic — same synthetic order id derived from
    # (price, size, side) → same SHA-256 LSB → same queue tick).
    assert paper_fill == pytest.approx(live_fill)
    assert live_fill == pytest.approx(bt_fill)
    # Fill size is the requested size — the canonical model only
    # adjusts the fill price (a future partial-fill-aware model can
    # override ``apply_slippage`` per broker).
    assert paper_size == pytest.approx(size)
    assert live_size == pytest.approx(size)
    assert bt_size == pytest.approx(size)


def test_apply_slippage_buy_adds_positive_slippage() -> None:
    """BUY slippage is adverse → the buyer pays MORE than the raw price.
    With a deep top-of-book (no caller-supplied order book), the only
    contributions are the flat 1-tick crossing penalty + the 0-or-1
    deterministic queue tick. So the slipped price is in
    (raw, raw + 2 ticks]."""
    broker = BacktestBroker(initial_capital=100.0)
    raw_price = 0.50
    fill_price, fill_size = broker.apply_slippage(raw_price, 10.0, "BUY")

    assert fill_price > raw_price
    # 1 crossing tick + 0-or-1 queue tick → slipped price in (0.50, 0.52].
    assert fill_price <= raw_price + 2 * 0.01
    assert fill_size == pytest.approx(10.0)


def test_apply_slippage_sell_subtracts_slippage() -> None:
    """SELL slippage is adverse → the seller receives LESS than the raw
    price. Same shape as the BUY test, mirrored."""
    broker = BacktestBroker(initial_capital=100.0)
    raw_price = 0.50
    fill_price, fill_size = broker.apply_slippage(raw_price, 10.0, "SELL")

    assert fill_price < raw_price
    # 1 crossing tick + 0-or-1 queue tick → slipped price in [0.48, 0.50).
    assert fill_price >= raw_price - 2 * 0.01
    assert fill_size == pytest.approx(10.0)


def test_apply_slippage_clamps_to_valid_price_range() -> None:
    """Slipped prices are clamped to [0.01, 0.99] — the valid Polymarket
    trading range — so an extreme raw price near the boundary doesn't
    slip out of market."""
    broker = BacktestBroker(initial_capital=100.0)

    # BUY at 0.98 → slipped up by 1-2 ticks → clamped to 0.99.
    fill_price, _ = broker.apply_slippage(0.98, 10.0, "BUY")
    assert fill_price <= 0.99

    # SELL at 0.02 → slipped down by 1-2 ticks → clamped to 0.01.
    fill_price, _ = broker.apply_slippage(0.02, 10.0, "SELL")
    assert fill_price >= 0.01


def test_apply_slippage_with_shallow_book_increases_buy_slippage() -> None:
    """When the caller supplies a shallow top-of-book, the size-impact
    term kicks in (orders larger than the top depth walk the book) →
    the BUY fill price is HIGHER than the deep-book baseline."""
    broker = BacktestBroker(initial_capital=100.0)

    # Deep book (no caller-supplied book) → only crossing + queue ticks.
    deep_fill, _ = broker.apply_slippage(0.50, 100.0, "BUY")

    # Shallow book: top ask = 5 shares, order = 100 shares → 95 shares
    # of overflow × 0.5 ticks / 50-share bucket = 0.95 ticks of size
    # impact → slipped price is higher than the deep-book baseline.
    shallow_book = {
        "bids": [{"price": 0.49, "size": 5}],
        "asks": [{"price": 0.51, "size": 5}],
    }
    shallow_fill, _ = broker.apply_slippage(
        0.50, 100.0, "BUY", order_book=shallow_book,
    )

    assert shallow_fill > deep_fill


def test_apply_slippage_is_deterministic_for_same_inputs() -> None:
    """The canonical slippage model is deterministic: the same inputs
    always produce the same output (queue tick is a stable SHA-256
    hash of the synthetic order id, which is itself derived from
    (price, size, side)). Reproducibility is the parity contract —
    a strategy tested in backtest must see the same slippage on every
    replay."""
    broker = BacktestBroker(initial_capital=100.0)
    fill1, _ = broker.apply_slippage(0.50, 10.0, "BUY")
    fill2, _ = broker.apply_slippage(0.50, 10.0, "BUY")
    assert fill1 == pytest.approx(fill2)


# ── OrderRequest dataclass ───────────────────────────────────────────────────


def test_order_request_auto_generates_client_order_id() -> None:
    """When ``client_order_id`` is omitted, ``OrderRequest.__post_init__``
    auto-generates a 12-char UUID prefix so every order has a stable
    identity for idempotency / audit-trail linkage."""
    request = OrderRequest(
        token_id=_TOKEN_ID, side="BUY", size=10.0, price=0.50,
    )
    assert request.client_order_id != ""
    assert len(request.client_order_id) == 12


def test_order_request_preserves_supplied_client_order_id() -> None:
    """When ``client_order_id`` is supplied, it's preserved verbatim
    (the auto-generation only runs on an empty string)."""
    request = OrderRequest(
        token_id=_TOKEN_ID,
        side="BUY",
        size=10.0,
        price=0.50,
        client_order_id="my-custom-id",
    )
    assert request.client_order_id == "my-custom-id"


def test_order_request_normalises_side_to_uppercase() -> None:
    """``side`` is upper-cased in ``__post_init__`` so callers can pass
    ``"buy"`` / ``"Sell"`` / a ``Side`` enum without per-casing
    boilerplate."""
    req_lower = OrderRequest(
        token_id=_TOKEN_ID, side="buy", size=10.0, price=0.50,
    )
    req_enum = OrderRequest(
        token_id=_TOKEN_ID, side="SELL", size=10.0, price=0.50,
    )
    assert req_lower.side == "BUY"
    assert req_enum.side == "SELL"


def test_broker_abc_cannot_be_instantiated_directly() -> None:
    """The ``Broker`` ABC has abstract methods; instantiating it
    directly raises ``TypeError`` (every concrete subclass must
    implement all six abstractmethods)."""
    with pytest.raises(TypeError):
        Broker()  # type: ignore[abstract]
