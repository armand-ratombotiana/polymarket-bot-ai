"""
tests/test_paper_simulator.py — Unit tests for paper/simulator.py.

Scope: pure-Python checks of ``PaperSimulator._can_fill`` and
``PaperSimulator._apply_slippage``. No event loop, no DB I/O, no live order
book — every fixture is constructed inline from the ``core.data_store``
dataclasses.

The simulator module instantiates a singleton ``paper_sim = PaperSimulator()``
at import time, which in turn reads ``store.paper_balance`` from the
module-level ``DataStore`` singleton. That singleton calls ``load_from_disk()``
on import, reading ``STORE_STATE_PATH`` (and the decision-ledger / audit / risk
modules each look at their own env-var-configured DB path on import). To keep
this test file hermetic and prevent clobbering any real persisted state in the
repo's ``data/`` directory, all DB / state env vars are redirected to ``/tmp``
*before* the bot package is imported.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Redirect every persisted-state path to /tmp BEFORE importing the bot. ──
# ``setdefault`` lets an outer runner (CI / pytest invocation) override these
# if it ever needs to; otherwise the tests run fully hermetic to ``/tmp``.
_TMP_ROOT = Path("/tmp/paper_sim_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS = {
    "STORE_STATE_PATH": _TMP_ROOT / "store_state.json",
    "DECISION_LEDGER_DB_PATH": _TMP_ROOT / "decision_ledger.db",
    "AUDIT_DB_PATH": _TMP_ROOT / "audit_trail.db",
    "MARKET_DB_PATH": _TMP_ROOT / "market_intelligence.db",
    "KILL_SWITCH_PATH": _TMP_ROOT / "kill_switch",
    "KILL_SWITCH_REASON_PATH": _TMP_ROOT / "kill_switch.reason",
    "VECTOR_STORE_PATH": _TMP_ROOT / "vector_index.json",
    "MODEL_PATH": _TMP_ROOT / "model.pkl",
    "MODEL_REGISTRY_PATH": _TMP_ROOT / "model_registry.json",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, str(_val))

# Make the polymarket-bot package root importable as top-level modules
# (``core.*``, ``paper.*``) when pytest is invoked from a different cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402  (env must be set first)

from core.data_store import Order, OrderBook, PriceLevel, Side  # noqa: E402
from paper.simulator import PaperSimulator  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────
_TOKEN_ID = "0xdeadbeefcafe000000000000000000000000000000000000000000000000beef"


def _book(ask_price=None, ask_size=10.0, bid_price=None, bid_size=10.0) -> OrderBook:
    """Build a minimal two-sided OrderBook. ``None`` price ⇒ side is empty."""
    asks = [PriceLevel(price=ask_price, size=ask_size)] if ask_price is not None else []
    bids = [PriceLevel(price=bid_price, size=bid_size)] if bid_price is not None else []
    return OrderBook(token_id=_TOKEN_ID, bids=bids, asks=asks)


def _order(
    side: Side,
    price: float,
    size: float = 5.0,
    order_id: str = "paper-test-default",
) -> Order:
    return Order(
        order_id=order_id,
        token_id=_TOKEN_ID,
        side=side,
        price=price,
        size=size,
        paper=True,
    )


@pytest.fixture
def sim() -> PaperSimulator:
    """Fresh ``PaperSimulator`` per test — no shared mutable state."""
    return PaperSimulator()


# ── (1) _can_fill — BUY fills at best_ask when best_ask <= order.price ─────
def test_can_fill_buy_returns_best_ask_when_marketable(sim: PaperSimulator) -> None:
    """A BUY whose limit >= best_ask crosses the book and fills at best_ask."""
    order = _order(Side.BUY, price=0.55, size=5.0)
    book = _book(ask_price=0.50, ask_size=10.0)  # best_ask 0.50 ≤ 0.55

    fill_price = sim._can_fill(order, book)

    assert fill_price == pytest.approx(0.50)


# ── (2) _can_fill — SELL fills at best_bid when best_bid >= order.price ────
def test_can_fill_sell_returns_best_bid_when_marketable(sim: PaperSimulator) -> None:
    """A SELL whose limit <= best_bid crosses the book and fills at best_bid."""
    order = _order(Side.SELL, price=0.45, size=5.0)
    book = _book(bid_price=0.50, bid_size=10.0)  # best_bid 0.50 ≥ 0.45

    fill_price = sim._can_fill(order, book)

    assert fill_price == pytest.approx(0.50)


# ── (3) _can_fill — returns None when fill conditions aren't met ──────────
@pytest.mark.parametrize(
    "side, order_price, ask_price, bid_price, label",
    [
        # BUY: best_ask strictly above the limit — not marketable.
        (Side.BUY, 0.45, 0.50, None, "buy_ask_above_limit"),
        # BUY: no asks on the book at all.
        (Side.BUY, 0.50, None, None, "buy_empty_asks"),
        # SELL: best_bid strictly below the limit — not marketable.
        (Side.SELL, 0.55, None, 0.50, "sell_bid_below_limit"),
        # SELL: no bids on the book at all.
        (Side.SELL, 0.50, None, None, "sell_empty_bids"),
    ],
)
def test_can_fill_returns_none_when_conditions_not_met(
    sim: PaperSimulator,
    side: Side,
    order_price: float,
    ask_price: float | None,
    bid_price: float | None,
    label: str,
) -> None:
    """Every non-marketable configuration must return ``None``."""
    order = _order(side, price=order_price, size=5.0, order_id=f"paper-test-nofill-{label}")
    book = _book(ask_price=ask_price, bid_price=bid_price)

    assert sim._can_fill(order, book) is None


# ── (4) _apply_slippage — BUY pays more (positive slippage) ───────────────
def test_apply_slippage_buy_adds_positive_slippage(sim: PaperSimulator) -> None:
    """For a BUY, the slipped fill price must be > the raw crossing price.

    Slippage on a BUY is always adverse ⇒ the buyer pays at least the flat
    crossing penalty (1 tick) on top of the raw ask. We pick an order_id whose
    SHA-256 LSB is 0 (queue_ticks = 0) and a top-of-book depth that fully
    absorbs the small order (size_impact = 0), so the only contribution is the
    flat 1-tick crossing penalty.
    """
    # 'paper-test-buy-6' → SHA-256[0] = 0x28 → LSB 0 → queue_ticks = 0
    order = _order(Side.BUY, price=0.55, size=5.0, order_id="paper-test-buy-6")
    book = _book(ask_price=0.50, ask_size=10.0)  # top depth 10 ≥ size 5
    raw_price = 0.50

    slipped = PaperSimulator._apply_slippage(order, raw_price, book)

    assert slipped > raw_price
    # Exactly the crossing penalty (1 tick = 0.01) above the raw price.
    assert slipped == pytest.approx(raw_price + 0.01)


# ── (5) _apply_slippage — SELL receives less (negative slippage) ───────────
def test_apply_slippage_sell_adds_negative_slippage(sim: PaperSimulator) -> None:
    """For a SELL, the slipped fill price must be < the raw crossing price.

    Same fixture shape as the BUY test, mirrored: a queue=0 order_id, full
    top-of-book absorption, raw_price in the middle of the band so the clamp
    to [0.01, 0.99] doesn't mask the slip.
    """
    # 'paper-test-sell-0' → SHA-256[0] = 0x6c → LSB 0 → queue_ticks = 0
    order = _order(Side.SELL, price=0.45, size=5.0, order_id="paper-test-sell-0")
    book = _book(bid_price=0.50, bid_size=10.0)
    raw_price = 0.50

    slipped = PaperSimulator._apply_slippage(order, raw_price, book)

    assert slipped < raw_price
    # Exactly the 1-tick crossing penalty below the raw price.
    assert slipped == pytest.approx(raw_price - 0.01)


# ── (6) slippage is deterministic (same order_id ⇒ same slip) ───────────────
def test_apply_slippage_is_deterministic_for_same_order_id(sim: PaperSimulator) -> None:
    """Recomputing slippage with identical inputs must return the same value.

    The queue-position component is derived from a stable SHA-256 hash of
    ``order.order_id``; everything else is a pure function of (order, book,
    raw_price). Two calls with the same inputs must therefore agree to the
    bit.
    """
    order = _order(Side.BUY, price=0.55, size=5.0, order_id="paper-test-deterministic-7")
    book = _book(ask_price=0.50, ask_size=10.0)
    raw_price = 0.50

    first = PaperSimulator._apply_slippage(order, raw_price, book)
    second = PaperSimulator._apply_slippage(order, raw_price, book)

    assert first == second


def test_apply_slippage_queue_component_varies_with_order_id(sim: PaperSimulator) -> None:
    """Different order_ids should produce the *family* of slippage values that
    differ only in the queue_position component (0 or 1 tick).

    This is the natural complement of the determinism test: the queue hash is
    the *only* input-driven randomisation, so we should be able to observe both
    bucket-0 and bucket-1 order_ids and confirm they differ by exactly one tick
    when crossing and size impact are held fixed.
    """
    raw_price = 0.50
    book = _book(ask_price=0.50, ask_size=10.0)  # top depth 10 ≥ size 5 → size_impact 0

    # Scan order_ids until we find one with queue=0 and one with queue=1.
    queue_zero_price = queue_one_price = None
    for i in range(500):
        oid = f"paper-test-queue-scan-{i}"
        price = PaperSimulator._apply_slippage(
            _order(Side.BUY, price=0.55, size=5.0, order_id=oid),
            raw_price,
            book,
        )
        if price == pytest.approx(raw_price + 0.01):
            queue_zero_price = price
        elif price == pytest.approx(raw_price + 0.02):
            queue_one_price = price
        if queue_zero_price is not None and queue_one_price is not None:
            break

    assert queue_zero_price is not None, "no queue=0 order_id found in scan"
    assert queue_one_price is not None, "no queue=1 order_id found in scan"
    assert queue_one_price > queue_zero_price
    assert queue_one_price - queue_zero_price == pytest.approx(0.01)


# ── (7) large orders over book depth get more slippage ─────────────────────
def test_large_orders_over_book_depth_get_more_slippage(sim: PaperSimulator) -> None:
    """Order size in excess of top-of-book depth must increase the slipped fill.

    The size-impact component is ``(overflow / SLIPPAGE_DEPTH_BUCKET) * 0.5``
    ticks. With ``SLIPPAGE_DEPTH_BUCKET = 50`` and a top-of-book depth of 10:
      - Small order (size 5)   → overflow 0   → size_impact 0.0 ticks
      - Large order (size 110) → overflow 100 → size_impact 1.0 ticks

    Holding order_id constant (⇒ identical queue_ticks) and raw_price constant
    (⇒ identical crossing penalty), the large-order fill must end up exactly
    one tick worse than the small-order fill, and strictly worse in price.
    """
    # Same order_id for both orders → queue_ticks identical (and = 0).
    order_id = "paper-test-overflow-4"  # SHA-256[0] = 0x76 → LSB 0 → queue 0
    raw_price = 0.50
    # Top-of-book ask depth is only 10 shares.
    book = _book(ask_price=0.50, ask_size=10.0)

    small_order = _order(Side.BUY, price=0.55, size=5.0, order_id=order_id)
    large_order = _order(Side.BUY, price=0.55, size=110.0, order_id=order_id)

    small_fill = PaperSimulator._apply_slippage(small_order, raw_price, book)
    large_fill = PaperSimulator._apply_slippage(large_order, raw_price, book)

    # Large order pays strictly more slippage ⇒ worse fill for the buyer.
    assert large_fill > small_fill
    # Difference is exactly the extra 1.0 tick of size impact.
    assert large_fill - small_fill == pytest.approx(PaperSimulator.TICK_SIZE)
