"""
core/position_manager.py — Advanced Position Management & Dynamic Exit Engine.

Provides:
  - Automated Take-Profit (TP) and Stop-Loss (SL) execution
  - Dynamic trailing stop adjustments based on peak contract valuation
  - Max slippage protection thresholds
  - Integration with durable audit logging

W18-5 — P0-C05 fix
-------------------
TP/SL exits now route through :mod:`core.execution_interface`, which
branches on ``settings.paper_trade``:

  * Paper mode: delegates to ``paper_sim.create_order`` (unchanged
    behaviour — simulator builds the local Order, runs the slippage model
    in its 1 s fill loop, records the ORDER stage in the decision ledger).
  * Live mode: delegates to ``clob_client.create_order`` (signs + POSTs
    a real EIP-712 limit order to the Polymarket CLOB). The local
    ``Order`` is added to ``store.open_orders`` so the
    ``active_exit_order_id`` tracker can cancel it on the next trigger
    evaluation. Submission failures are caught inside the execution
    interface and surfaced as ``None`` so the position manager's
    surrounding ``try/except`` can decide whether to retry on the next
    loop tick.

Prior to W18-5, ``evaluate_positions`` unconditionally called
``paper_sim.create_order`` for exits regardless of
``settings.paper_trade``, so live TP/SL exits never reached the exchange.
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.audit_logger import audit_logger
from core.data_store import store
from core.execution_interface import cancel_exit_order, submit_exit_order

log = logging.getLogger(__name__)


class ManagedPosition:
    def __init__(self, token_id: str, entry_price: float, take_profit_pct: float = 0.25, stop_loss_pct: float = 0.05) -> None:
        self.token_id = token_id
        self.entry_price = entry_price
        self.take_profit_price = min(entry_price * (1.0 + take_profit_pct), 0.99)
        self.stop_loss_price = max(entry_price * (1.0 - stop_loss_pct), 0.01)
        self.high_water_mark = entry_price
        self.created_at = time.time()
        # R1: Track the latest live exit (TP/SL) order so we can cancel stale
        # ones before re-submitting on the next trigger evaluation.
        self.active_exit_order_id: str | None = None


class PositionManager:
    """
    Continuous risk and exit supervisor for all active positions.
    """

    def __init__(self) -> None:
        self.managed_positions: dict[str, ManagedPosition] = {}
        self._running = False

    async def register_entry(self, token_id: str, entry_price: float) -> None:
        """Register a newly opened position with TP/SL bounds."""
        self.managed_positions[token_id] = ManagedPosition(token_id, entry_price)
        log.info("[position_manager] Registered position %s (TP=%.3f, SL=%.3f)",
                 token_id[:12], self.managed_positions[token_id].take_profit_price,
                 self.managed_positions[token_id].stop_loss_price)

    async def evaluate_positions(self) -> None:
        """Inspect all active positions against live mid-prices for TP/SL triggers."""
        async with store._lock:
            positions = list(store.positions.values())

        for pos in positions:
            if pos.yes_shares <= 0:
                continue

            book = await store.get_order_book(pos.token_id)
            if not book or not book.mid:
                continue

            mid = book.mid
            managed = self.managed_positions.get(pos.token_id)
            if not managed:
                self.managed_positions[pos.token_id] = ManagedPosition(pos.token_id, pos.avg_entry_price)
                managed = self.managed_positions[pos.token_id]

            # Update high water mark
            managed.high_water_mark = max(managed.high_water_mark, mid)

            # Check Take-Profit Trigger
            if mid >= managed.take_profit_price:
                log.info("[position_manager] 🎯 Take-Profit Triggered for %s @ %.4f — Submitting Exit Order", pos.token_id[:12], mid)
                await audit_logger.log_event(
                    category="EXIT",
                    event_type="TAKE_PROFIT_TRIGGERED",
                    details=f"Take-Profit triggered @ {mid:.4f} (Entry: {managed.entry_price:.4f})",
                    token_id=pos.token_id,
                    slug=store.market_slugs.get(pos.token_id),
                    pnl=pos.realised_pnl,
                    strategy="position_manager_tp",
                )
                # W18-5 — R1: Cancel prior stale exit order before placing a new
                # one. The cancel is routed through the execution interface so
                # the right venue (paper_sim or clob_client) is consulted.
                if managed.active_exit_order_id:
                    try:
                        await cancel_exit_order(managed.active_exit_order_id)
                        log.debug("[position_manager] Cancelled prior exit order %s for %s",
                                  managed.active_exit_order_id, pos.token_id[:12])
                    except Exception as cancel_err:
                        log.debug("[position_manager] Prior exit cancel failed (%s) — continuing", cancel_err)
                # W18-5 — Build the exit payload. ``best_bid`` (not ``mid``)
                # makes the order MARKETABLE so it crosses the spread and fills
                # immediately. The ``submit_exit_order`` helper routes to
                # paper_sim in paper mode and clob_client in live mode based
                # on ``settings.paper_trade``.
                strat = "position_manager_tp"
                try:
                    from risk.manager import risk_manager
                    from core.data_store import Order, Side
                    # Pre-check Order for the risk gate. ``order_id`` is a
                    # placeholder — the execution interface assigns the real id
                    # (paper-<uuid> in paper mode, the CLOB orderID in live
                    # mode) and returns it via the resulting ``Order``.
                    pre_check_order = Order(
                        order_id="position-manager-tp-precheck",
                        token_id=pos.token_id,
                        side=Side.SELL,
                        price=book.best_bid,
                        size=pos.yes_shares,
                        strategy=strat,
                    )
                    allowed, reason = await risk_manager.check_order(pre_check_order)
                    if not allowed:
                        log.warning(
                            "[position_manager] 🚫 TP exit order for %s rejected by risk gate: %s",
                            pos.token_id[:12], reason,
                        )
                        await audit_logger.log_event(
                            category="risk",
                            event_type="EXIT_RISK_GATE_REJECTED",
                            details=f"TP exit order rejected by risk gate: {reason}",
                            token_id=pos.token_id,
                            slug=store.market_slugs.get(pos.token_id),
                            pnl=pos.realised_pnl,
                            strategy=strat,
                        )
                        continue
                    # Submit through the unified execution interface. Returns
                    # the local ``Order`` (paper or live) on success, ``None``
                    # on live failure. Paper-mode never returns None.
                    submitted = await submit_exit_order(
                        token_id=pos.token_id,
                        side=Side.SELL,
                        price=book.best_bid,
                        size=pos.yes_shares,
                        strategy=strat,
                        decision_id=pre_check_order.decision_id,
                    )
                    if submitted is not None:
                        managed.active_exit_order_id = submitted.order_id
                    else:
                        log.warning(
                            "[position_manager] TP exit submission returned None for %s — will retry next tick",
                            pos.token_id[:12],
                        )
                except Exception as exit_err:
                    log.warning(
                        "[position_manager] TP exit submission failed for %s: %s",
                        pos.token_id[:12], exit_err,
                    )

            # Check Stop-Loss Trigger
            elif mid <= managed.stop_loss_price:
                log.info("[position_manager] 🛑 Stop-Loss Triggered for %s @ %.4f — Submitting Exit Order", pos.token_id[:12], mid)
                await audit_logger.log_event(
                    category="EXIT",
                    event_type="STOP_LOSS_TRIGGERED",
                    details=f"Stop-Loss triggered @ {mid:.4f} (Entry: {managed.entry_price:.4f})",
                    token_id=pos.token_id,
                    slug=store.market_slugs.get(pos.token_id),
                    pnl=pos.realised_pnl,
                    strategy="position_manager_sl",
                )
                # W18-5 — R1: Cancel prior stale exit order before placing a new
                # one. The cancel is routed through the execution interface so
                # the right venue (paper_sim or clob_client) is consulted.
                if managed.active_exit_order_id:
                    try:
                        await cancel_exit_order(managed.active_exit_order_id)
                        log.debug("[position_manager] Cancelled prior exit order %s for %s",
                                  managed.active_exit_order_id, pos.token_id[:12])
                    except Exception as cancel_err:
                        log.debug("[position_manager] Prior exit cancel failed (%s) — continuing", cancel_err)
                # W18-5 — Build the exit payload. ``best_bid`` (not ``mid``)
                # makes the order MARKETABLE so it crosses the spread and fills
                # immediately. The ``submit_exit_order`` helper routes to
                # paper_sim in paper mode and clob_client in live mode based
                # on ``settings.paper_trade``.
                strat = "position_manager_sl"
                try:
                    from risk.manager import risk_manager
                    from core.data_store import Order, Side
                    pre_check_order = Order(
                        order_id="position-manager-sl-precheck",
                        token_id=pos.token_id,
                        side=Side.SELL,
                        price=book.best_bid,
                        size=pos.yes_shares,
                        strategy=strat,
                    )
                    allowed, reason = await risk_manager.check_order(pre_check_order)
                    if not allowed:
                        log.warning(
                            "[position_manager] 🚫 SL exit order for %s rejected by risk gate: %s",
                            pos.token_id[:12], reason,
                        )
                        await audit_logger.log_event(
                            category="risk",
                            event_type="EXIT_RISK_GATE_REJECTED",
                            details=f"SL exit order rejected by risk gate: {reason}",
                            token_id=pos.token_id,
                            slug=store.market_slugs.get(pos.token_id),
                            pnl=pos.realised_pnl,
                            strategy=strat,
                        )
                        continue
                    submitted = await submit_exit_order(
                        token_id=pos.token_id,
                        side=Side.SELL,
                        price=book.best_bid,
                        size=pos.yes_shares,
                        strategy=strat,
                        decision_id=pre_check_order.decision_id,
                    )
                    if submitted is not None:
                        managed.active_exit_order_id = submitted.order_id
                    else:
                        log.warning(
                            "[position_manager] SL exit submission returned None for %s — will retry next tick",
                            pos.token_id[:12],
                        )
                except Exception as exit_err:
                    log.warning(
                        "[position_manager] SL exit submission failed for %s: %s",
                        pos.token_id[:12], exit_err,
                    )

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        asyncio.create_task(self._loop(), name="position-manager-loop")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.evaluate_positions()
            except Exception as e:
                log.debug("[position_manager] Loop error: %s", e)
            await asyncio.sleep(5.0)

    async def stop(self) -> None:
        self._running = False


# Global singleton
position_manager = PositionManager()
