"""
core/execution_interface.py — Unified execution interface.

Abstracts paper vs live order submission so callers (position_manager,
strategies/base, future entry-exit supervisors) can route an order to the
correct venue without each caller re-implementing the
``settings.paper_trade`` branch.

P0-C05 fix (W18-5)
-------------------
Prior to W18-5, ``core/position_manager.py`` lines 135 and 209
unconditionally called ``paper_sim.create_order(...)`` for TP/SL exits,
even when ``settings.paper_trade`` was False (live mode). The result:
live positions had no automated exit management — TP/SL orders were
recorded only in the paper simulator's local state and never reached the
real Polymarket CLOB.

This module exposes two async helpers — :func:`submit_exit_order` and
:func:`cancel_exit_order` — that branch on ``settings.paper_trade``:

  * Paper mode: delegate to ``paper_sim.create_order`` /
    ``paper_sim.cancel_order`` (unchanged behaviour — the simulator
    builds the local ``Order``, runs the slippage model in its 1 s fill
    loop, and records the ORDER stage in the decision ledger).

  * Live mode: delegate to ``clob_client.create_order`` /
    ``clob_client.cancel_order``. The live response dict is mapped to a
    local ``Order`` (added to ``store.open_orders`` so the
    ``active_exit_order_id`` tracker in ``PositionManager`` can find and
    cancel it). Submission failures (CLOB 4xx/5xx, signing failure,
    network error, missing creds) are caught, logged, and surfaced as a
    ``None`` return so the caller's existing ``try/except`` handler can
    decide whether to retry on the next loop tick.

The interface is intentionally narrow: it submits ONE order at a time,
returns the local ``Order`` (or ``None``), and never raises. Risk-gate
clearance is the caller's responsibility (``position_manager`` runs the
institutional risk gate before calling ``submit_exit_order`` — the same
pattern as ``strategies/base.submit_order``).
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional

from config import settings
from core.clob_client import OrderArgs, clob_client
from core.data_store import Order, Side, store

log = logging.getLogger(__name__)


async def submit_exit_order(
    *,
    token_id: str,
    side: Side,
    price: float,
    size: float,
    strategy: str = "",
    decision_id: str = "",
) -> Optional[Order]:
    """Submit an exit order (TP/SL) to the appropriate execution venue.

    Paper mode: route through ``paper_sim.create_order`` (simulator builds
    the local Order, runs the slippage model in the 1 s fill loop, and
    records the ORDER stage in the decision ledger).

    Live mode: sign an EIP-712 limit order via ``clob_client.create_order``
    and submit it to the Polymarket CLOB. On success, the server response
    is mapped to a local ``Order`` (paper=False) added to
    ``store.open_orders`` so the position manager's
    ``active_exit_order_id`` tracker can find and cancel it later.

    Parameters
    ----------
    token_id
        ERC-1155 token ID for the market outcome (YES or NO).
    side
        ``Side.SELL`` for a long-position close, ``Side.BUY`` for a
        short-position close (current production code only closes longs).
    price
        Marktable limit price — for a SELL exit, this should be the
        current ``book.best_bid`` so the order crosses the spread and
        fills immediately; for a BUY exit, ``book.best_ask``.
    size
        Number of shares to exit (matches ``Position.yes_shares`` /
        ``no_shares``).
    strategy
        Strategy attribution propagated to the resulting ``Order`` (so
        P&L attribution / decision-ledger linkage works in both modes).
    decision_id
        Decision-ledger chain ID (R11). Propagated to the resulting
        ``Order`` so the ORDER / FILL stages can be linked to the
        originating PREDICTION → SIGNAL → RISK_APPROVED chain. Empty
        string for legacy / manual exits.

    Returns
    -------
    Optional[Order]
        The local ``Order`` (paper or live) on success, or ``None`` if
        the live submission failed (CLOB rejection, signing failure,
        missing credentials, network error). Paper-mode submissions
        do not return ``None`` — ``paper_sim.create_order`` always
        succeeds (it just records the order locally).
    """
    args = OrderArgs(token_id=token_id, price=price, side=side, size=size)

    if settings.paper_trade:
        # Paper mode: delegate to the simulator. The simulator builds the
        # local ``Order`` (with its own ``paper-<uuid>`` id), records it in
        # ``store.open_orders``, logs the ORDER stage in the decision
        # ledger, and returns it. The caller uses the returned order_id to
        # populate ``active_exit_order_id`` so the next exit cycle can
        # cancel the prior stale order before re-submitting (R1).
        from paper.simulator import paper_sim

        return await paper_sim.create_order(
            args, strategy=strategy, decision_id=decision_id,
        )

    # Live mode: submit a real signed EIP-712 order to the CLOB. The
    # ``clob_client.create_order`` helper:
    #   * signs the typed-data payload with the configured wallet key,
    #   * POSTs the wrapped order to ``/order`` on the CLOB host,
    #   * returns the server response dict (containing ``orderID``) on
    #     success, or ``None`` on any signing / HTTP / network error.
    # We wrap the call in a defensive try/except so an unexpected
    # exception class (e.g. ``RuntimeError`` from "Not authenticated" if
    # ``clob_client._creds`` is None, or ``CircuitBreakerOpenError`` if
    # the CLOB breaker is OPEN) surfaces as a logged ``None`` rather than
    # crashing the position manager's evaluation loop.
    try:
        resp = await clob_client.create_order(args)
    except Exception as exc:
        log.error(
            "[execution_interface] Live exit order raised (token=%s, side=%s, "
            "size=%.2f, price=%.4f): %s",
            token_id[:12], side.value, size, price, exc,
        )
        return None

    if resp is None:
        log.warning(
            "[execution_interface] Live exit order rejected by CLOB "
            "(token=%s, side=%s, size=%.2f, price=%.4f) — see clob_client log",
            token_id[:12], side.value, size, price,
        )
        return None

    # Map the server response to a local ``Order`` so the position
    # manager's ``active_exit_order_id`` tracker can find it for a future
    # cancel. The CLOB response typically contains ``orderID`` (canonical
    # Polymarket CLOB field); we fall back to the legacy ``order_id`` key
    # and finally to a synthesised ``live-<uuid>`` placeholder so the
    # local Order is never unidentifiable even if the response shape
    # changes upstream.
    order_id = (
        resp.get("orderID")
        or resp.get("order_id")
        or f"live-{uuid.uuid4().hex[:12]}"
    )
    order = Order(
        order_id=order_id,
        token_id=token_id,
        side=side,
        price=price,
        size=size,
        strategy=strategy,
        paper=False,
        decision_id=decision_id,
    )
    await store.add_order(order)
    log.info(
        "[execution_interface] Live exit order submitted: id=%s side=%s "
        "size=%.2f price=%.4f token=%s",
        order_id, side.value, size, price, token_id[:12],
    )
    return order


async def cancel_exit_order(order_id: str) -> bool:
    """Cancel an outstanding exit order in the appropriate venue.

    Paper mode: ``paper_sim.cancel_order`` updates the local store
    (status=CANCELLED, evict from ``open_orders``, append to
    ``order_history``, best-effort state-machine transition).

    Live mode: ``clob_client.cancel_order`` DELETEs the order from the
    CLOB. The local ``store.open_orders`` entry is left untouched on a
    live cancel (live fill-ack — C-02 — is a separate fix); the position
    manager only tracks the ``active_exit_order_id`` for de-duplication
    of re-submission, not for local state coherence, so a stale local
    entry is harmless until C-02 lands.

    Returns
    -------
    bool
        ``True`` on success, ``False`` on failure (unknown order,
        CLOB rejection, network error, or any unexpected exception).
    """
    if settings.paper_trade:
        from paper.simulator import paper_sim

        return await paper_sim.cancel_order(order_id)

    try:
        return await clob_client.cancel_order(order_id)
    except Exception as exc:
        log.error(
            "[execution_interface] Live cancel failed for %s: %s",
            order_id, exc,
        )
        return False


__all__ = ["submit_exit_order", "cancel_exit_order"]
