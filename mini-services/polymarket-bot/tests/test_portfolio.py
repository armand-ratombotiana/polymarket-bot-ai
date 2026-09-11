"""
Unit tests for ``core/portfolio.py``.

V6 — Portfolio analytics unit tests.

Covers the seven guarantees enumerated in the V6 task spec:

  1. ``compute_exposure()`` returns total exposure — the
     ``maximum_remaining_loss`` field sums ``current_exposure`` across
     all open positions (this is the "capital at risk" figure surfaced
     as ``open_exposure`` on ``GET /api/portfolio/exposure``).
  2. ``compute_exposure()`` returns the open position count — the
     ``open_position_count`` field equals the number of positions with
     ``current_exposure > 0.001`` (dust / zero-exposure positions
     excluded).
  3. ``strategy_stats(strategy)`` computes ``win_rate`` correctly —
     ``wins / closed_trades`` where ``closed_trades`` = trades with
     non-zero ``pnl`` and ``wins`` = trades with ``pnl > 0``.
  4. ``strategy_stats(strategy)`` computes ``profit_factor`` correctly —
     ``gross_profit / gross_loss`` (rounded to 2dp).
  5. ``leaderboard()`` ranks strategies by ``risk_adjusted_score``
     descending — the ``ranked`` array is sorted high-to-low and each
     row's score matches a fresh ``risk_adjusted_score(strategy_stats(...))``
     call (no stub field).
  6. ``compute_mark_to_market_exposure()`` returns ``total_exposure_mark`` —
     the sum of per-position marked market values
     (``mark * yes_shares + (1 - mark) * no_shares``).
  7. ``compute_mark_to_market_exposure()`` returns per-position
     ``unrealized_pnl`` — the ``positions`` array carries one dict per
     open position, each with the correct ``unrealized_pnl`` value and
     the ``cost_basis_mark`` flag set when the book was absent.

Mocking strategy
-----------------
The portfolio module reads from the module-level ``store`` singleton
(an instance of ``DataStore`` from ``core.data_store``). The repo's
``tests/conftest.py`` autouse ``_reset_store_factory_defaults`` fixture
clears ``store.positions`` / ``store.trades`` / ``store.open_orders``
/ ``store.order_books`` and restores ``paper_balance`` to
``BANKROLL_BASELINE`` before every test, so each test starts from a
clean baseline. Each test then seeds the freshly-cleared singleton
directly with deterministic ``Position`` / ``Trade`` / ``OrderBook``
instances and asserts on the function's output — no real DB I/O is
hit, no async fills are required, and the production singleton's state
is never persisted across tests. This is the "mocked store" surface
the V6 task spec refers to: the portfolio module's data source is
mocked to a deterministic in-memory list of seed rows.

Why ``compute_mark_to_market_exposure`` lives in a NEW module
-------------------------------------------------------------
The V6 task spec lists ``compute_mark_to_market_exposure`` as one of
the seven tests, but the function does NOT yet exist in
``core/portfolio.py`` — the inline mark-to-market logic currently
lives only in ``api/server.py`` (the equity-snapshot endpoint). The
task constraint "Do NOT edit existing files" forbids appending the
function to ``core/portfolio.py``, so it lives in the new companion
module ``core/portfolio_mark_to_market.py`` (additive — re-uses the
existing ``store`` / ``OrderBook`` shapes, no edits to any existing
file). Tests 6 and 7 import it from the companion module; tests 1-5
import the existing functions directly from ``core.portfolio``.
Promotion of this function into ``core/portfolio.py`` is flagged as a
follow-up in the V6 worklog entry.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors the convention used by
``tests/test_attribution.py`` / ``tests/test_decision_ledger.py`` /
the other Wave 3+ test modules).
"""
from __future__ import annotations

import pytest

from core.data_store import (
    OrderBook,
    Position,
    PriceLevel,
    Side,
    Trade,
    store,
)
from core.portfolio import (
    compute_exposure,
    leaderboard,
    risk_adjusted_score,
    strategy_stats,
)
from core.portfolio_mark_to_market import compute_mark_to_market_exposure

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. The repo's ``pytest.ini`` / ``pyproject.toml`` are not edited
# per the V6 task constraint ("Do NOT edit existing files"), so we use the
# module-level ``pytestmark`` idiom instead of ``asyncio_mode = "auto"``
# (mirrors ``tests/test_attribution.py`` and the other Wave 3+ tests).
pytestmark = pytest.mark.asyncio


# ── Helpers / fixtures ────────────────────────────────────────────────────


def _position(
    token_id: str,
    yes_shares: float = 0.0,
    no_shares: float = 0.0,
    avg_entry_price: float = 0.0,
    strategy: str = "",
    market_slug: str = "",
    total_invested: float | None = None,
    opened_at: float = 0.0,
    last_updated: float = 0.0,
) -> Position:
    """Construct a ``Position`` with sensible defaults.

    ``current_exposure`` is a derived property (``yes_shares *
    avg_entry_price``), so positions with non-zero ``yes_shares`` AND
    non-zero ``avg_entry_price`` automatically show up as open in
    ``compute_exposure()`` / ``compute_mark_to_market_exposure()``.

    ``total_invested`` defaults to ``yes_shares * avg_entry_price``
    (the cost-basis convention used by ``record_fill`` in
    ``core.data_store``) so ``compute_exposure()["capital_invested"]``
    matches ``maximum_remaining_loss`` for freshly-seeded positions
    that have not been partially closed.
    """
    if total_invested is None:
        total_invested = yes_shares * avg_entry_price
    return Position(
        token_id=token_id,
        market_slug=market_slug,
        yes_shares=yes_shares,
        no_shares=no_shares,
        avg_entry_price=avg_entry_price,
        total_invested=total_invested,
        strategy=strategy,
        opened_at=opened_at,
        last_updated=last_updated,
    )


def _book(token_id: str, best_bid: float, best_ask: float) -> OrderBook:
    """Construct an ``OrderBook`` with single-level bid / ask ladders
    so ``book.mid`` returns ``(best_bid + best_ask) / 2`` and
    ``book.best_bid`` / ``book.best_ask`` are non-None (the path
    ``compute_mark_to_market_exposure`` takes when a live quote is
    available).
    """
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=best_bid, size=100.0)],
        asks=[PriceLevel(price=best_ask, size=100.0)],
    )


def _trade(
    trade_id: str,
    token_id: str,
    pnl: float,
    strategy: str,
    side: Side = Side.BUY,
    price: float = 0.50,
    size: float = 10.0,
) -> Trade:
    """Construct a ``Trade`` row with deterministic identity + P&L.

    The default ``side=BUY``, ``price=0.50``, ``size=10.0`` keep the
    seed concise — tests override only ``pnl`` and ``strategy`` (the
    two fields ``strategy_stats`` / ``leaderboard`` actually read).
    """
    return Trade(
        trade_id=trade_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        pnl=pnl,
        strategy=strategy,
    )


# ── 1. compute_exposure returns total exposure ─────────────────────────────


async def test_compute_exposure_returns_total_exposure():
    """``compute_exposure()["maximum_remaining_loss"]`` must equal the
    sum of ``current_exposure`` (= ``yes_shares * avg_entry_price``)
    across all open positions. This is the "total exposure" / "capital
    at risk" figure surfaced as ``open_exposure`` on
    ``GET /api/portfolio/exposure``.

    The seed has two open positions (cost bases $30 + $20 = $50) and
    one zero-exposure position that must be EXCLUDED from the total.
    """
    # Two open positions with known cost bases.
    store.positions["TOK_A"] = _position(
        "TOK_A", yes_shares=100.0, avg_entry_price=0.30,
        strategy="ml_sig_v1", market_slug="market-a",
        opened_at=1000.0, last_updated=2000.0,
    )
    store.positions["TOK_B"] = _position(
        "TOK_B", yes_shares=50.0, avg_entry_price=0.40,
        strategy="arb_scanner", market_slug="market-b",
        opened_at=1000.0, last_updated=2000.0,
    )
    # A zero-exposure position that must be EXCLUDED from the total.
    store.positions["TOK_C"] = _position(
        "TOK_C", yes_shares=0.0, avg_entry_price=0.50,
    )

    out = compute_exposure()

    # Total exposure = max remaining loss = sum(current_exposure).
    expected_total = 100.0 * 0.30 + 50.0 * 0.40  # 30 + 20 = 50
    assert out["maximum_remaining_loss"] == pytest.approx(expected_total, abs=0.01)

    # gross_market_value defaults to the cost-basis mark (== max remaining
    # loss) when no book_provider is supplied — verify the same total.
    assert out["gross_market_value"] == pytest.approx(expected_total, abs=0.01)

    # capital_invested sums total_invested (defaults to yes_shares *
    # avg_entry_price), which equals the cost basis for freshly-seeded
    # positions that haven't been partially closed.
    assert out["capital_invested"] == pytest.approx(expected_total, abs=0.01)


# ── 2. compute_exposure returns open position count ─────────────────────────


async def test_compute_exposure_returns_open_position_count():
    """``compute_exposure()["open_position_count"]`` must equal the
    count of positions with ``current_exposure > 0.001``.

    The seed has 3 open positions, 1 dust position (exposure 0.00005,
    below the 0.001 threshold), and 1 zero-exposure position — only
    the 3 open positions must be counted.
    """
    # 3 open positions: exposure > 0.001 in each.
    store.positions["TOK_OPEN_1"] = _position(
        "TOK_OPEN_1", yes_shares=10.0, avg_entry_price=0.30,
    )
    store.positions["TOK_OPEN_2"] = _position(
        "TOK_OPEN_2", yes_shares=20.0, avg_entry_price=0.40,
    )
    store.positions["TOK_OPEN_3"] = _position(
        "TOK_OPEN_3", yes_shares=5.0, avg_entry_price=0.50,
    )
    # Dust position: 0.0001 shares at $0.50 → exposure 0.00005 (< 0.001).
    store.positions["TOK_DUST"] = _position(
        "TOK_DUST", yes_shares=0.0001, avg_entry_price=0.50,
    )
    # Zero-exposure position: 0 shares → exposure 0.0.
    store.positions["TOK_ZERO"] = _position(
        "TOK_ZERO", yes_shares=0.0, avg_entry_price=0.50,
    )

    out = compute_exposure()
    assert out["open_position_count"] == 3


# ── 3. strategy_stats computes win_rate correctly ───────────────────────────


async def test_strategy_stats_computes_win_rate():
    """``strategy_stats(strategy)["win_rate"]`` must equal
    ``wins / closed_trades`` where ``closed_trades`` = trades with
    ``pnl != 0`` (breakeven trades are excluded) and ``wins`` = trades
    with ``pnl > 0``.

    The seed has 3 winners, 2 losers, and 1 breakeven (pnl == 0, which
    is excluded from the closed-trades denominator). Expected
    win_rate = 3 / 5 = 0.6.
    """
    # 3 winners + 2 losers + 1 breakeven (pnl==0 → excluded from closed).
    pnls = [5.0, 3.0, 2.0, -1.0, -4.0, 0.0]
    for i, pnl in enumerate(pnls):
        store.trades.append(_trade(
            trade_id=f"t-{i}", token_id="TOK_X", pnl=pnl,
            strategy="ml_sig_v1",
        ))

    out = strategy_stats("ml_sig_v1")

    # closed = 5 (the 5 non-zero-pnl trades); wins = 3 → win_rate = 0.6.
    assert out["closed_trades"] == 5
    assert out["win_rate"] == pytest.approx(0.6, abs=1e-4)


# ── 4. strategy_stats computes profit_factor ──────────────────────────────


async def test_strategy_stats_computes_profit_factor():
    """``strategy_stats(strategy)["profit_factor"]`` must equal
    ``gross_profit / gross_loss`` (rounded to 2dp), where
    ``gross_profit`` = sum of positive pnls and ``gross_loss`` =
    absolute sum of negative pnls.

    The seed has 2 winners (sum $10) and 2 losers (sum -$4) →
    profit_factor = 10 / 4 = 2.5.
    """
    # 2 winners (sum=10) + 2 losers (sum=-4).
    pnls = [7.0, 3.0, -1.0, -3.0]
    for i, pnl in enumerate(pnls):
        store.trades.append(_trade(
            trade_id=f"t-{i}", token_id="TOK_Y", pnl=pnl,
            strategy="arb_scanner",
        ))

    out = strategy_stats("arb_scanner")

    # gross_profit = 10, gross_loss = 4 → pf = 2.5.
    assert out["profit_factor"] == pytest.approx(2.5, abs=0.01)


# ── 5. leaderboard ranks by risk_adjusted_score descending ──────────────────


async def test_leaderboard_ranks_by_risk_adjusted_score_desc():
    """``leaderboard()["ranked"]`` must be sorted by
    ``risk_adjusted_score`` descending.

    The seed has two strategies:
      * ``alpha`` — net P&L +$19 (3 winners, 1 loser).
      * ``beta``  — net P&L -$10 (1 winner, 3 losers).

    Because ``risk_adjusted_score`` subtracts drawdown + uncertainty
    penalties from net P&L, alpha (positive net P&L, smaller drawdown)
    must rank strictly higher than beta (negative net P&L, larger
    drawdown). Additionally, each row's ``risk_adjusted_score`` must
    match a fresh ``risk_adjusted_score(strategy_stats(strategy))``
    call — confirming the leaderboard actually computes the score from
    the same ``strategy_stats`` output the API surfaces, not a stub.
    """
    # alpha: 3 winners (sum=20), 1 loser (-1) → net +19.
    for i, pnl in enumerate([10.0, 6.0, 4.0, -1.0]):
        store.trades.append(_trade(
            trade_id=f"a-{i}", token_id="TOK_A", pnl=pnl,
            strategy="alpha",
        ))
    # beta: 1 winner (2), 3 losers (sum=-12) → net -10.
    for i, pnl in enumerate([2.0, -3.0, -4.0, -5.0]):
        store.trades.append(_trade(
            trade_id=f"b-{i}", token_id="TOK_B", pnl=pnl,
            strategy="beta",
        ))

    out = leaderboard()

    assert out["count"] == 2
    ranked = out["ranked"]
    assert len(ranked) == 2

    # The ranked list MUST be sorted by risk_adjusted_score desc.
    scores = [row["risk_adjusted_score"] for row in ranked]
    assert scores == sorted(scores, reverse=True), (
        f"leaderboard not sorted desc: {scores}"
    )

    # And specifically: alpha (positive net P&L) must outrank beta (negative).
    assert ranked[0]["strategy"] == "alpha"
    assert ranked[1]["strategy"] == "beta"
    assert ranked[0]["risk_adjusted_score"] > ranked[1]["risk_adjusted_score"]

    # Sanity: each row's risk_adjusted_score matches a fresh
    # risk_adjusted_score(strategy_stats(strategy)) call — confirms the
    # leaderboard actually computes the score (not just sorts a stub field).
    for row in ranked:
        expected = risk_adjusted_score(strategy_stats(row["strategy"]))
        assert row["risk_adjusted_score"] == pytest.approx(expected, abs=1e-4)


# ── 6. compute_mark_to_market_exposure returns total_exposure_mark ───────────


async def test_compute_mark_to_market_exposure_returns_total_exposure_mark():
    """``compute_mark_to_market_exposure()["total_exposure_mark"]``
    must equal the sum of per-position marked market values, where
    marked value = ``mark * yes_shares + (1 - mark) * no_shares``.

    The seed has two YES-only positions:
      * TOK_UP   — 100 shares, mark 0.60 → marked value 60.0.
      * TOK_DOWN —  80 shares, mark 0.25 → marked value 20.0.
    Total expected = 80.0.
    """
    store.positions["TOK_UP"] = _position(
        "TOK_UP", yes_shares=100.0, avg_entry_price=0.50,
    )
    store.positions["TOK_DOWN"] = _position(
        "TOK_DOWN", yes_shares=80.0, avg_entry_price=0.50,
    )
    # Live books with deterministic mids (0.60 and 0.25).
    store.order_books["TOK_UP"] = _book("TOK_UP", best_bid=0.59, best_ask=0.61)
    store.order_books["TOK_DOWN"] = _book("TOK_DOWN", best_bid=0.24, best_ask=0.26)

    out = compute_mark_to_market_exposure()

    # 100 * 0.60 + 80 * 0.25 = 60 + 20 = 80.0.
    expected = 100.0 * 0.60 + 80.0 * 0.25
    assert out["total_exposure_mark"] == pytest.approx(expected, abs=0.01)
    assert out["open_position_count"] == 2


# ── 7. compute_mark_to_market_exposure returns per-position unrealized_pnl ──


async def test_compute_mark_to_market_exposure_returns_per_position_unrealized_pnl():
    """``compute_mark_to_market_exposure()["positions"]`` must contain
    one dict per open position, each carrying an ``unrealized_pnl``
    field equal to::

        (mark - avg_entry_price) * yes_shares
            + ((1.0 - mark) - avg_entry_price) * no_shares

    Positions whose order book is absent must fall back to the
    cost-basis mark (``avg_entry_price``), yielding
    ``unrealized_pnl == 0`` and ``cost_basis_mark == True``.

    The seed has three positions:
      * TOK_WIN     — entry 0.50, mark 0.60 → unrealized +10.0.
      * TOK_LOSS    — entry 0.50, mark 0.40 → unrealized -10.0.
      * TOK_NO_BOOK — entry 0.50, no live book → falls back to cost-basis
                      mark (0.50), unrealized 0.0, cost_basis_mark=True.

    The aggregate ``total_unrealized_pnl`` must equal the sum of the
    per-position figures (10.0 + -10.0 + 0.0 = 0.0).
    """
    # Position 1: entry 0.50, mark 0.60 → unrealized = (0.60-0.50)*100 = +10.0
    store.positions["TOK_WIN"] = _position(
        "TOK_WIN", yes_shares=100.0, avg_entry_price=0.50,
    )
    store.order_books["TOK_WIN"] = _book("TOK_WIN", best_bid=0.59, best_ask=0.61)

    # Position 2: entry 0.50, mark 0.40 → unrealized = (0.40-0.50)*100 = -10.0
    store.positions["TOK_LOSS"] = _position(
        "TOK_LOSS", yes_shares=100.0, avg_entry_price=0.50,
    )
    store.order_books["TOK_LOSS"] = _book("TOK_LOSS", best_bid=0.39, best_ask=0.41)

    # Position 3: entry 0.50, no order book → falls back to cost-basis mark
    #             → unrealized = 0.0; cost_basis_mark == True.
    store.positions["TOK_NO_BOOK"] = _position(
        "TOK_NO_BOOK", yes_shares=100.0, avg_entry_price=0.50,
    )

    out = compute_mark_to_market_exposure()

    # Three open positions, one per seeded row.
    assert out["open_position_count"] == 3
    by_token = {row["token_id"]: row for row in out["positions"]}
    assert set(by_token.keys()) == {"TOK_WIN", "TOK_LOSS", "TOK_NO_BOOK"}

    # Per-position unrealized P&L matches the formula by hand.
    assert by_token["TOK_WIN"]["unrealized_pnl"] == pytest.approx(10.0, abs=0.01)
    assert by_token["TOK_WIN"]["cost_basis_mark"] is False

    assert by_token["TOK_LOSS"]["unrealized_pnl"] == pytest.approx(-10.0, abs=0.01)
    assert by_token["TOK_LOSS"]["cost_basis_mark"] is False

    # Cost-basis fallback: book absent → mark = avg_entry_price →
    # unrealized P&L = 0.0 by construction.
    assert by_token["TOK_NO_BOOK"]["unrealized_pnl"] == pytest.approx(0.0, abs=1e-6)
    assert by_token["TOK_NO_BOOK"]["cost_basis_mark"] is True
    # The mark field echoes the fallback entry price.
    assert by_token["TOK_NO_BOOK"]["mark"] == pytest.approx(0.50, abs=1e-4)

    # Aggregate sanity: sum of per-position unrealized_pnl equals the total.
    expected_total = 10.0 + (-10.0) + 0.0
    assert out["total_unrealized_pnl"] == pytest.approx(expected_total, abs=0.01)
