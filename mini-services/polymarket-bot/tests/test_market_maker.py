"""
tests/test_market_maker.py — Unit tests for ``strategies/market_maker.py``.

X12 — Market Maker strategy unit tests.

Covers the six contracts enumerated in the X12 task spec:

  (1) ``MarketMakerStrategy._is_quoteable`` returns ``True`` for a book
      whose ``mid`` is a valid probability inside ``[0.02, 0.98]``.
  (2) ``_is_quoteable`` returns ``False`` when the book's ``mid`` is
      ``None`` (one-sided / empty book) — the documented "skip this
      market" fallback used by ``_refresh_markets`` and the main quote
      review loop.
  (3) ``_place_skewed_quotes`` places BOTH a BUY and a SELL order when
      the book is two-sided, the trader can both buy (under the
      inventory cap) and sell (holds YES shares), and both prices fall
      inside the ``(0.01, 0.99)`` quoteable band.
  (4) ``_ml_spread_adjustment`` returns a 2-tuple of floats
      ``(spread_multiplier, reservation_skew)`` — the documented
      public return contract. The exception fallback ``(1.0, 0.0)``
      is the load-bearing branch exercised here (no real ML model in
      the test sandbox).
  (5) ``_cancel_quotes`` cancels every existing resting order for a
      token (both BUY and SELL slots) and resets the stored order ids
      to ``None`` so the next quote cycle re-places from a clean slate.
  (6) ``_flush_stale_inventory`` only fires after the YES inventory has
      been held strictly longer than the 60-second grace window. Within
      the grace window (or with no inventory / no first-observation
      timestamp yet), no flush order is placed and the method returns
      ``False``.

Mocking strategy
~~~~~~~~~~~~~~~~
  * ``store`` / ``gamma_client`` — the global singletons are NOT
    replaced. The strategy module's ``__init__`` reads
    ``settings.*`` synchronously and never touches the network; the
    methods under test only read/write the in-memory ``store`` dict
    containers (``positions``, ``market_slugs``) and the strategy's
    own ``_quotes`` / ``_inventory_since`` dicts. The autouse
    ``_reset_store_factory_defaults`` fixture in ``tests/conftest.py``
    resets the global ``store`` to a clean baseline before every test,
    so per-test mutation of ``store.positions`` is hermetic.
  * ``BaseStrategy.submit_order`` — patched on the strategy instance
    with an ``AsyncMock`` returning a canned ``Order`` so the A-S
    quote-placement path can be exercised end-to-end without going
    through the risk manager / paper simulator / live CLOB. This is
    the contract the X12 spec asks for ("mock store and
    gamma_client"); the strategy's own ``submit_order`` is the single
    network-touching seam, so intercepting there is sufficient.
  * ``BaseStrategy.cancel_order`` — likewise patched with an
    ``AsyncMock`` for test (5) so the cancel path is exercised
    without touching the real CLOB client.
  * ``_cancel_quotes`` — patched with an ``AsyncMock`` for tests (3)
    and (6) so the place-quote path can be tested in isolation from
    the cancel path (the cancel path is exercised separately in
    test 5).
  * ``ml.features.extract_features`` — monkeypatched to return
    ``None`` in test (4) so ``_ml_spread_adjustment`` exercises its
    documented exception-fallback branch and returns the deterministic
    ``(1.0, 0.0)`` default. In test (3), the whole
    ``_ml_spread_adjustment`` method is replaced with a lambda
    returning ``(1.0, 0.0)`` so the A-S math is deterministic and the
    test can assert on the BUY/SELL prices indirectly via the
    ``OrderArgs`` captured by the ``submit_order`` mock.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration — mirrors the convention already
used by every sibling test module (``tests/test_decision_ledger.py``,
``tests/test_gamma_client.py``, ``tests/test_settlement.py``, …). The
repo's ``pytest.ini`` cannot be edited per the X12 "Do NOT edit
existing files" constraint, so ``asyncio_mode = "auto"`` cannot be
enabled via config; the module-level ``pytestmark`` idiom is the
canonical pytest-asyncio escape hatch under strict mode.
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from core.data_store import (
    Order,
    OrderBook,
    Position,
    PriceLevel,
    Side,
)
from strategies.market_maker import MarketMakerStrategy


# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` cannot be edited
# per the X12 task constraint ("Do NOT edit existing files"), so we use
# the module-level ``pytestmark`` idiom instead of ``asyncio_mode =
# "auto"`` (mirrors every sibling test module — ``test_attribution.py``,
# ``test_decision_ledger.py``, ``test_gamma_client.py``, …).
pytestmark = pytest.mark.asyncio


# ── Test-local constants & helpers ─────────────────────────────────────────

_TOKEN_ID = "0xdeadbeef00000000000000000000000000000000000000000000000000cafe"


def _two_sided_book(
    best_bid: float = 0.49,
    best_ask: float = 0.51,
    bid_size: float = 10.0,
    ask_size: float = 10.0,
) -> OrderBook:
    """Build a minimal two-sided ``OrderBook`` for the test token.

    ``mid`` is computed by ``OrderBook.mid`` as ``(best_bid + best_ask) / 2``
    so the default (0.49 / 0.51) yields ``mid == 0.5`` — comfortably inside
    the ``[0.02, 0.98]`` quoteable band.
    """
    return OrderBook(
        token_id=_TOKEN_ID,
        bids=[PriceLevel(price=best_bid, size=bid_size)],
        asks=[PriceLevel(price=best_ask, size=ask_size)],
    )


def _one_sided_book_no_mid() -> OrderBook:
    """A book with no best_bid (asks-only) — ``mid`` is ``None``."""
    return OrderBook(
        token_id=_TOKEN_ID,
        bids=[],
        asks=[PriceLevel(price=0.5, size=10.0)],
    )


def _order(order_id: str, side: Side, price: float, size: float = 3.0) -> Order:
    """Build a minimal ``Order`` for the ``submit_order`` mock to return."""
    return Order(
        order_id=order_id,
        token_id=_TOKEN_ID,
        side=side,
        price=price,
        size=size,
        paper=True,
    )


# ── Fixture: fresh MarketMakerStrategy per test ────────────────────────────


@pytest.fixture
def mm_strategy() -> MarketMakerStrategy:
    """Freshly-constructed ``MarketMakerStrategy`` (no network, no I/O).

    The strategy's ``__init__`` is fully synchronous — it reads
    ``settings.*`` scalars (``mm_spread_bps``, ``mm_quote_size_usdc``,
    ``mm_max_inventory_usdc``) and initializes the in-memory
    ``_quotes`` / ``_last_mid`` / ``_inventory_since`` / ``_token_ids``
    / ``_market_info`` containers. No discovery, no Gamma API, no
    CLOB auth — those only run inside ``_run()`` / ``_discover_markets()``
    / ``_refresh_markets()``, none of which are invoked here.

    The autouse ``_reset_store_factory_defaults`` fixture from
    ``tests/conftest.py`` runs BEFORE this fixture and resets the
    global ``store`` singleton to a clean baseline (empty
    ``positions``, ``market_slugs``, ``open_orders`` dicts;
    ``paper_balance`` at ``BANKROLL_BASELINE``), so per-test mutation
    of ``store.positions[_TOKEN_ID]`` is hermetic and cannot leak into
    a sibling test.
    """
    return MarketMakerStrategy()


# ── (1) _is_quoteable returns True for valid mid ───────────────────────────


async def test_is_quoteable_true_for_valid_mid():
    """``_is_quoteable`` returns ``True`` for a two-sided book whose
    ``mid`` falls inside the ``[0.02, 0.98]`` quoteable band.

    The ``_is_quoteable`` predicate is a ``@staticmethod`` on
    ``MarketMakerStrategy`` — no instance state is consulted, so the
    call can be made directly on the class without constructing a
    strategy instance. This is the contract used by both
    ``_refresh_markets`` (which drops tokens whose books are
    unquoteable) and the main quote-review loop's pre-flight check.

    Three mid checkpoints are exercised:
      * ``mid == 0.5``  (interior of the band — the canonical case)
      * ``mid == 0.02`` (lower inclusive boundary)
      * ``mid == 0.98`` (upper inclusive boundary)

    The boundary check is ``0.02 <= book.mid <= 0.98`` (inclusive on
    both ends), so the extreme-but-valid mids must return ``True``.
    """
    # Interior — canonical case.
    book_interior = _two_sided_book(best_bid=0.49, best_ask=0.51)
    assert book_interior.mid == 0.5
    assert MarketMakerStrategy._is_quoteable(book_interior) is True

    # Lower boundary — inclusive (mid == 0.02 must still be quoteable).
    book_lower = _two_sided_book(best_bid=0.015, best_ask=0.025)
    assert book_lower.mid == pytest.approx(0.02)
    assert MarketMakerStrategy._is_quoteable(book_lower) is True

    # Upper boundary — inclusive (mid == 0.98 must still be quoteable).
    book_upper = _two_sided_book(best_bid=0.975, best_ask=0.985)
    assert book_upper.mid == pytest.approx(0.98)
    assert MarketMakerStrategy._is_quoteable(book_upper) is True


# ── (2) _is_quoteable returns False for None mid ───────────────────────────


async def test_is_quoteable_false_for_none_mid():
    """``_is_quoteable`` returns ``False`` when the book's ``mid`` is
    ``None``.

    ``OrderBook.mid`` is ``None`` whenever the book is missing one or
    both sides (``best_bid`` or ``best_ask`` is ``None``). The
    ``_is_quoteable`` guard's first non-``None`` check is
    ``book.mid is not None`` — a one-sided book (or an entirely empty
    book) must therefore short-circuit to ``False`` so the caller
    skips two-sided quoting on a market that cannot be priced.

    Two scenarios are exercised:
      * Asks-only book (no best_bid) — common when a market is freshly
        listed and only sell-side liquidity has arrived.
      * Entirely empty book (no bids AND no asks) — the cold-start
        case observed at strategy boot before the book poller has
        populated the order_books dict.
    """
    # Asks-only book → mid is None.
    book_one_sided = _one_sided_book_no_mid()
    assert book_one_sided.best_bid is None
    assert book_one_sided.mid is None
    assert MarketMakerStrategy._is_quoteable(book_one_sided) is False

    # Entirely empty book → mid is None.
    book_empty = OrderBook(token_id=_TOKEN_ID, bids=[], asks=[])
    assert book_empty.mid is None
    assert MarketMakerStrategy._is_quoteable(book_empty) is False


# ── (3) _place_skewed_quotes places bid and ask ────────────────────────────


async def test_place_skewed_quotes_places_bid_and_ask(mm_strategy: MarketMakerStrategy):
    """``_place_skewed_quotes`` submits BOTH a BUY and a SELL order when
    the trader can both buy (under the inventory cap) and sell (holds
    YES shares with invested capital), and both computed prices fall
    inside the ``(0.01, 0.99)`` quoteable band.

    Setup
    ~~~~~
      * ``store.positions[_TOKEN_ID]`` — a position with
        ``yes_shares=10.0`` and ``total_invested=5.0``. This satisfies
        the A-S ``can_sell`` gate (``q > 0.0 and invested > 0.0``) AND
        leaves room under the ``can_buy`` gate
        (``invested + quote_size <= max_inv`` ⇒ ``5.0 + 1.5 <= 15.0``).
      * ``book`` — two-sided with ``best_bid=0.49``, ``best_ask=0.51``
        (``mid=0.5``, ``spread=0.02``), so the reservation-price
        formula yields bid/ask prices well inside ``(0.01, 0.99)``.
      * ``mm_strategy.submit_order`` — ``AsyncMock`` returning a canned
        ``Order`` per call. The mock inspects the ``OrderArgs`` it
        receives so the test can assert on the actual side / price
        produced by the A-S formula.
      * ``mm_strategy._cancel_quotes`` — ``AsyncMock`` so the place
        path is exercised in isolation from the cancel path (the
        cancel path is exercised separately in test 5).
      * ``mm_strategy._ml_spread_adjustment`` — replaced with a lambda
        returning ``(1.0, 0.0)`` so the A-S math is deterministic
        (no real ML model needed; the reservation-skew is 0 and the
        spread multiplier is the identity).

    Assertions
    ~~~~~~~~~~
      * ``submit_order`` is awaited exactly twice.
      * The first call's ``OrderArgs.side == Side.BUY`` and its price
        is strictly less than ``book.mid`` (a BUY quote is below mid).
      * The second call's ``OrderArgs.side == Side.SELL`` and its
        price is strictly greater than ``book.mid`` (a SELL quote is
        above mid).
      * The strategy's ``_quotes[_TOKEN_ID]`` dict is populated with
        the BUY order id under the ``"BUY"`` slot and the SELL order
        id under the ``"SELL"`` slot — the canonical quote-tracking
        shape that ``_cancel_quotes`` and the re-quote logic in
        ``_review_quotes`` both rely on.
    """
    # ── Position setup (can both buy and sell) ──
    store_position = Position(
        token_id=_TOKEN_ID,
        yes_shares=10.0,
        total_invested=5.0,
    )
    # Importing the global store lazily here so the test module's
    # import time is not burdened by the core.data_store singleton's
    # load_from_disk side effects (the autouse conftest fixture has
    # already redirected STORE_STATE_PATH to /tmp).
    from core.data_store import store

    store.positions[_TOKEN_ID] = store_position
    store.market_slugs[_TOKEN_ID] = "test-market"

    # ── Book setup (two-sided, mid=0.5) ──
    book = _two_sided_book(best_bid=0.49, best_ask=0.51)

    # ── Mocks ──
    # _ml_spread_adjustment is replaced with a deterministic lambda so
    # the A-S reservation-price math is predictable (ml_adj=1.0,
    # ml_skew=0.0).
    mm_strategy._ml_spread_adjustment = lambda token_id, book: (1.0, 0.0)

    # _cancel_quotes is a no-op AsyncMock — the cancel path is tested
    # separately in test 5.
    mm_strategy._cancel_quotes = AsyncMock()

    # submit_order returns a fresh canned Order per call (BUY first,
    # SELL second — matching the call order inside _place_skewed_quotes).
    fake_orders = [
        _order("bid_oid", Side.BUY, 0.4892),
        _order("ask_oid", Side.SELL, 0.5092),
    ]
    fake_orders_iter = iter(fake_orders)
    mm_strategy.submit_order = AsyncMock(side_effect=lambda *a, **kw: next(fake_orders_iter))

    # ── Action ──
    await mm_strategy._place_skewed_quotes(_TOKEN_ID, book)

    # ── Assertions ──
    assert mm_strategy.submit_order.await_count == 2, (
        "Both a BUY and a SELL order must be placed when the trader can "
        "both buy (under the inventory cap) and sell (holds YES shares)."
    )

    first_call_args = mm_strategy.submit_order.call_args_list[0].args[0]
    second_call_args = mm_strategy.submit_order.call_args_list[1].args[0]

    # First call: BUY below mid.
    assert first_call_args.side == Side.BUY
    assert first_call_args.token_id == _TOKEN_ID
    assert 0.01 < first_call_args.price < book.mid, (
        f"BUY price {first_call_args.price} must be strictly below mid "
        f"{book.mid} and inside the (0.01, 0.99) quoteable band."
    )

    # Second call: SELL above mid.
    assert second_call_args.side == Side.SELL
    assert second_call_args.token_id == _TOKEN_ID
    assert book.mid < second_call_args.price < 0.99, (
        f"SELL price {second_call_args.price} must be strictly above mid "
        f"{book.mid} and inside the (0.01, 0.99) quoteable band."
    )

    # Quote-tracking dict populated correctly.
    assert mm_strategy._quotes[_TOKEN_ID]["BUY"] == "bid_oid"
    assert mm_strategy._quotes[_TOKEN_ID]["SELL"] == "ask_oid"


# ── (4) _ml_spread_adjustment returns tuple ────────────────────────────────


async def test_ml_spread_adjustment_returns_tuple(
    mm_strategy: MarketMakerStrategy,
    monkeypatch: pytest.MonkeyPatch,
):
    """``_ml_spread_adjustment`` returns a ``(spread_multiplier,
    reservation_skew)`` 2-tuple of floats — the documented public
    return contract.

    The method is wrapped in a top-level ``try/except Exception``
    whose fallback is ``return 1.0, 0.0`` (the neutral default: 1.0×
    spread multiplier, 0.0 reservation-skew). To exercise the
    fallback branch deterministically — and avoid loading the real
    ML model (which trains on synthetic data at module-import time
    and would slow the test by ~1 s) — the test monkeypatches
    ``ml.features.extract_features`` to return ``None``. The
    production code's first internal check is::

        feats = extract_features(market, book)
        if feats is None:
            return 1.0, 0.0

    so the ``None``-features branch is the load-bearing path here.

    Assertions
    ~~~~~~~~~~
      * The return value is a ``tuple`` (not a list, not a numpy
        array, not a dataclass).
      * The tuple has exactly 2 elements — the documented
        ``(spread_multiplier, reservation_skew)`` shape.
      * Both elements are ``float`` instances — the contract callers
        like ``_place_skewed_quotes`` rely on (they do arithmetic on
        ``ml_adj`` and ``ml_skew`` directly).
      * The values are the documented fallback ``(1.0, 0.0)`` — the
        neutral "no ML signal" default.
    """
    book = _two_sided_book(best_bid=0.49, best_ask=0.51)

    # Force the documented "no features" early-return branch.
    monkeypatch.setattr("ml.features.extract_features", lambda *a, **kw: None)

    result = mm_strategy._ml_spread_adjustment(_TOKEN_ID, book)

    assert isinstance(result, tuple), (
        f"_ml_spread_adjustment must return a tuple; got {type(result).__name__}."
    )
    assert len(result) == 2, (
        f"_ml_spread_adjustment must return a 2-tuple; got {len(result)}-tuple."
    )
    assert all(isinstance(v, float) for v in result), (
        f"Both tuple elements must be floats; got {[type(v).__name__ for v in result]}."
    )
    # The documented fallback when ML features can't be extracted.
    assert result == (1.0, 0.0)


# ── (5) _cancel_quotes cancels existing orders ─────────────────────────────


async def test_cancel_quotes_cancels_existing_orders(mm_strategy: MarketMakerStrategy):
    """``_cancel_quotes`` cancels every resting quote for a token —
    both the ``BUY`` and the ``SELL`` slot — and resets the stored
    order ids to ``None`` so the next quote cycle re-places from a
    clean slate.

    Setup
    ~~~~~
      * ``mm_strategy._quotes[_TOKEN_ID]`` pre-populated with
        ``{"BUY": "bid_oid", "SELL": "ask_oid"}`` — the canonical
        post-quote state established by ``_place_skewed_quotes``.
      * ``mm_strategy.cancel_order`` is an ``AsyncMock`` so the test
        can assert on the order ids passed to the cancel path without
        touching the real CLOB / paper-sim cancel implementations.

    Assertions
    ~~~~~~~~~~
      * ``cancel_order`` is awaited exactly twice (once per side).
      * The first await receives ``"bid_oid"`` (the BUY slot's
        stored order id) and the second receives ``"ask_oid"`` (the
        SELL slot's).
      * After the call, ``_quotes[_TOKEN_ID]`` has both slots reset
        to ``None`` — the canonical "no active quotes" shape that
        ``_review_quotes`` checks via ``any(quotes.get(s) for s in
        ("BUY", "SELL"))`` to decide whether to re-quote.
    """
    # ── Pre-state: two resting quotes ──
    mm_strategy._quotes[_TOKEN_ID] = {"BUY": "bid_oid", "SELL": "ask_oid"}
    mm_strategy.cancel_order = AsyncMock(return_value=True)

    # ── Action ──
    await mm_strategy._cancel_quotes(_TOKEN_ID)

    # ── Assertions ──
    assert mm_strategy.cancel_order.await_count == 2, (
        "Both the BUY and SELL order must be cancelled when "
        "_cancel_quotes runs against a fully-quoted token."
    )

    # Order ids passed to cancel_order, in iteration order.
    cancelled_ids = [
        call.args[0] for call in mm_strategy.cancel_order.call_args_list
    ]
    assert cancelled_ids == ["bid_oid", "ask_oid"], (
        f"Expected cancel_order to be called with ['bid_oid', 'ask_oid'] "
        f"in BUY-then-SELL iteration order; got {cancelled_ids}."
    )

    # Post-state: both slots reset to None.
    assert mm_strategy._quotes[_TOKEN_ID] == {"BUY": None, "SELL": None}, (
        "After _cancel_quotes, both BUY and SELL slots must be reset to "
        "None so the next quote cycle re-places from a clean slate."
    )


# ── (6) _flush_stale_inventory checks age > 60s ────────────────────────────


async def test_flush_stale_inventory_checks_age_over_60s(mm_strategy: MarketMakerStrategy):
    """``_flush_stale_inventory`` only fires a marketable SELL flush
    order when the YES inventory has been held strictly longer than
    the 60-second grace window.

    The method's age-threshold contract (production lines 359-374):

      1. If ``q <= 0.0``              → no inventory, clear timestamp, return False.
      2. If ``since is None``         → first observation of this inventory,
                                       start the clock, return False.
      3. If ``now - since <= 60.0``   → still inside the grace window,
                                       return False (no flush).
      4. Else (``now - since > 60.0``) → cancel resting quotes, place a
                                       single marketable SELL at
                                       ``best_bid``, reset the clock,
                                       return True.

    This test exercises branches (4) — the fire path — and (3) — the
    grace-window short-circuit — to prove the threshold check is the
    load-bearing gate.

    Setup
    ~~~~~
      * ``store.positions[_TOKEN_ID]`` — a position with
        ``yes_shares=10.0`` (so the ``q <= 0.0`` early-exit is
        bypassed).
      * ``mm_strategy._inventory_since[_TOKEN_ID]`` — set explicitly
        to either ``time.time() - 100`` (branch 4: held 100 s,
        > 60 s) or ``time.time() - 30`` (branch 3: held 30 s,
        ≤ 60 s).
      * ``book`` — two-sided with ``best_bid=0.49`` (well above the
        ``0.01`` flush floor) so the flush order can be priced.
      * ``mm_strategy.submit_order`` — ``AsyncMock`` returning a
        canned ``Order`` so the flush SELL can be recorded without
        touching the real CLOB.
      * ``mm_strategy._cancel_quotes`` — ``AsyncMock`` so the cancel
        path is exercised separately (test 5) and isolated here.

    Assertions
    ~~~~~~~~~~
      * Branch (4) — age > 60 s:
          - return value is ``True``
          - ``submit_order`` awaited exactly once
          - the single ``OrderArgs`` passed has ``side == SELL`` and
            ``price == book.best_bid`` (marketable at the top bid)
          - ``_quotes[_TOKEN_ID]["SELL"]`` is the flush order's id
          - ``_inventory_since[_TOKEN_ID]`` is reset to ~now so the
            next cycle does not immediately re-flush
      * Branch (3) — age ≤ 60 s:
          - return value is ``False``
          - ``submit_order`` is NOT awaited (no flush attempted)
    """
    # ── Common setup ──
    from core.data_store import store

    store.positions[_TOKEN_ID] = Position(
        token_id=_TOKEN_ID,
        yes_shares=10.0,
        total_invested=5.0,
    )
    store.market_slugs[_TOKEN_ID] = "test-market"
    book = _two_sided_book(best_bid=0.49, best_ask=0.51)
    assert book.best_bid == 0.49

    # Mock the cancel path so the place path can be observed in isolation.
    mm_strategy._cancel_quotes = AsyncMock()

    # ── Branch (4): held > 60 s → flush fires ──
    mm_strategy._inventory_since[_TOKEN_ID] = time.time() - 100.0  # 100 s ago
    flush_order = _order("flush_oid", Side.SELL, price=0.49, size=10.0)
    mm_strategy.submit_order = AsyncMock(return_value=flush_order)

    fired = await mm_strategy._flush_stale_inventory(_TOKEN_ID, book)

    assert fired is True, (
        "When inventory has been held for > 60 s and a marketable "
        "best_bid is available, _flush_stale_inventory must return True."
    )
    assert mm_strategy.submit_order.await_count == 1, (
        "Exactly one flush SELL order must be placed when the grace "
        "window has elapsed."
    )
    flush_args = mm_strategy.submit_order.call_args.args[0]
    assert flush_args.side == Side.SELL, (
        "The flush order must be a SELL (dumping YES inventory)."
    )
    assert flush_args.price == book.best_bid, (
        f"The flush order must be priced at best_bid ({book.best_bid}) "
        f"so it crosses the spread and is marketable; got {flush_args.price}."
    )
    assert mm_strategy._quotes[_TOKEN_ID]["SELL"] == "flush_oid", (
        "The flush order's id must be recorded under the SELL slot so "
        "the next _review_quotes cycle sees it as a resting order."
    )
    # Clock is reset to ~now so the next cycle does not immediately re-flush.
    assert mm_strategy._inventory_since[_TOKEN_ID] >= time.time() - 5.0, (
        "After a successful flush, the inventory-since clock must be "
        "reset to ~now so we don't immediately re-flush next cycle."
    )

    # ── Branch (3): held ≤ 60 s → no flush ──
    # Reset the mock so we can assert "not awaited" cleanly.
    mm_strategy.submit_order = AsyncMock(return_value=flush_order)
    mm_strategy._inventory_since[_TOKEN_ID] = time.time() - 30.0  # 30 s ago

    fired_within_grace = await mm_strategy._flush_stale_inventory(_TOKEN_ID, book)

    assert fired_within_grace is False, (
        "When inventory has been held for ≤ 60 s, _flush_stale_inventory "
        "must return False without attempting a flush."
    )
    mm_strategy.submit_order.assert_not_awaited(), (
        "No flush order may be placed while inventory is still inside "
        "the 60-second grace window."
    )
