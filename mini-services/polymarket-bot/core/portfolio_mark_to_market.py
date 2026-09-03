"""
core/portfolio_mark_to_market.py — Mark-to-market exposure decomposition.

Companion module to ``core/portfolio.py`` that exposes the
``compute_mark_to_market_exposure`` function — a per-position
mark-to-market view the parent module does NOT yet expose publicly.

Background
----------
The parent ``core/portfolio.py`` module ships ``compute_exposure()``,
which decomposes cost-basis exposure (capital invested, max remaining
loss, net directional, per-group / per-strategy breakdowns, exposure
duration, exposure-dollar-days). It does NOT, however, expose a
marked-to-market view that re-values open positions at the live
order-book mid — even though that exact computation already exists
inline in ``api/server.py`` (the equity-snapshot endpoint, lines
~631-640):

    unrealized_pnl = 0.0
    for p in store.positions.values():
        if p.current_exposure <= 0.001:
            continue
        book = store.order_books.get(p.token_id)
        mark = book.mid if book and book.mid is not None else None
        if mark is None:
            mark = p.avg_entry_price  # cost-basis mark (no live quote)
        unrealized_pnl += (mark - p.avg_entry_price) * p.yes_shares
        unrealized_pnl += ((1.0 - mark) - p.avg_entry_price) * p.no_shares

This module lifts that inline loop into a reusable function so the
strategy leaderboard, the API, and any future consumer can read a
consistent marked view. Mirrors the inline semantics exactly:

  * positions with ``current_exposure <= 0.001`` are excluded (dust);
  * the mark is ``book.mid`` when a live book is available, otherwise
    the position's ``avg_entry_price`` (cost-basis fallback);
  * YES-side marked value is ``mark * yes_shares``;
  * NO-side marked value is ``(1.0 - mark) * no_shares``;
  * per-position ``unrealized_pnl`` is
    ``(mark - avg_entry_price) * yes_shares
       + ((1.0 - mark) - avg_entry_price) * no_shares``.

Why a new module (not an edit of ``core/portfolio.py``)?
-------------------------------------------------------
The V6 task spec ("unit tests for ``core/portfolio.py``") calls for a
``compute_mark_to_market_exposure`` test surface, but constrains the
implementation to "Do NOT edit existing files." This module is the
additive answer: it imports the existing ``store`` singleton and
``OrderBook`` shape from ``core.data_store`` (re-using, not duplicating)
and exposes the new function in its own namespace so the test file
can import it without modifying ``core/portfolio.py``. Promotion of
this function into ``core/portfolio.py`` is flagged as a follow-up in
the V6 worklog entry.

API
---
``compute_mark_to_market_exposure(book_provider=None) -> dict``

  * ``book_provider`` — optional SYNC callable ``token_id -> OrderBook | None``.
    When omitted (the default), the function reads from the global
    ``store.order_books`` mapping (mirroring the production
    ``api/server.py`` pattern). When supplied, it replaces the default
    source — useful for unit tests that want to inject deterministic
    books without touching the global store.

  * Returns a dict with:
      ``total_exposure_mark``  — sum of per-position marked market values;
      ``total_unrealized_pnl`` — sum of per-position unrealized P&L;
      ``positions``            — one dict per open position, each carrying
                                  ``token_id``, ``mark``, ``yes_shares``,
                                  ``no_shares``, ``avg_entry_price``,
                                  ``marked_value_yes``, ``marked_value_no``,
                                  ``unrealized_pnl``, ``cost_basis_mark``;
      ``open_position_count``  — number of open positions included in the sum.
"""
from __future__ import annotations

from typing import Callable, Optional

from core.data_store import OrderBook, store


def compute_mark_to_market_exposure(
    book_provider: Optional[Callable[[str], Optional[OrderBook]]] = None,
) -> dict:
    """Mark open positions to their live order-book mid (with cost-basis
    fallback) and decompose total marked exposure plus per-position
    unrealized P&L.

    Mirrors the inline mark-to-market loop in ``api/server.py`` (the
    production equity snapshot), lifted into a reusable function so the
    API, the strategy leaderboard, and tests can consume a single
    canonical marked view.

    Parameters
    ----------
    book_provider : callable, optional
        Sync ``token_id -> OrderBook | None`` callable. When omitted,
        the function reads from the global ``store.order_books`` mapping
        (the same source ``api/server.py`` uses today). When supplied,
        the callable fully replaces the default — return ``None`` for a
        token to trigger the cost-basis fallback for that position.

    Returns
    -------
    dict
        See the module docstring for the full response schema. All
        monetary fields are rounded to 2dp; share / price / unrealized
        P&L fields are rounded to 4dp to mirror the precision the API
        snapshot endpoint publishes.
    """
    # Filter dust positions exactly like compute_exposure() does so the
    # ``open_position_count`` figures agree across the two views.
    positions = [p for p in store.positions.values() if p.current_exposure > 0.001]

    total_exposure_mark = 0.0
    total_unrealized_pnl = 0.0
    rows: list[dict] = []

    for p in positions:
        # Resolve the order book — either via the injected provider or
        # the global store. The try/except guards against a misbehaving
        # provider raising (defensive: production should never see this,
        # but a buggy provider in a test harness must not crash the whole
        # snapshot).
        book: Optional[OrderBook] = None
        if book_provider is not None:
            try:
                book = book_provider(p.token_id)
            except Exception:
                book = None
        else:
            book = store.order_books.get(p.token_id)

        # Mark selection: live mid when available, cost-basis fallback otherwise.
        cost_basis_mark = False
        if book is not None and book.mid is not None:
            mark = float(book.mid)
        else:
            mark = float(p.avg_entry_price)
            cost_basis_mark = True

        # Marked market values — YES shares pay out at `mark` per share,
        # NO shares pay out at (1 - mark) per share at resolution.
        marked_value_yes = mark * p.yes_shares
        marked_value_no = (1.0 - mark) * p.no_shares

        # Unrealized P&L vs the average entry price. Mirrors the inline
        # formula in api/server.py exactly.
        unrealized_pnl = (
            (mark - p.avg_entry_price) * p.yes_shares
            + ((1.0 - mark) - p.avg_entry_price) * p.no_shares
        )

        marked_exposure = marked_value_yes + marked_value_no
        total_exposure_mark += marked_exposure
        total_unrealized_pnl += unrealized_pnl

        rows.append({
            "token_id": p.token_id,
            "mark": round(mark, 4),
            "yes_shares": round(p.yes_shares, 4),
            "no_shares": round(p.no_shares, 4),
            "avg_entry_price": round(p.avg_entry_price, 4),
            "marked_value_yes": round(marked_value_yes, 2),
            "marked_value_no": round(marked_value_no, 2),
            "unrealized_pnl": round(unrealized_pnl, 4),
            "cost_basis_mark": cost_basis_mark,
        })

    return {
        "total_exposure_mark": round(total_exposure_mark, 2),
        "total_unrealized_pnl": round(total_unrealized_pnl, 2),
        "positions": rows,
        "open_position_count": len(rows),
    }
