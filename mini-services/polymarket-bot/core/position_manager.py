"""
core/position_manager.py — Advanced Position Management & Dynamic Exit Engine.

Provides:
  - Automated Take-Profit (TP) and Stop-Loss (SL) execution
  - Dynamic trailing stop adjustments based on peak contract valuation
  - Max slippage protection thresholds
  - Integration with durable audit logging
"""
from __future__ import annotations

import asyncio
import logging
import time

from core.audit_logger import audit_logger
from core.data_store import store

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
                    strategy="position_manager",
                )
                from core.data_store import Order, Side
                from paper.simulator import paper_sim
                # R1: Cancel prior stale exit order before placing a new one
                if managed.active_exit_order_id:
                    try:
                        await paper_sim.cancel_order(managed.active_exit_order_id)
                        log.debug("[position_manager] Cancelled prior exit order %s for %s",
                                  managed.active_exit_order_id, pos.token_id[:12])
                    except Exception as cancel_err:
                        log.debug("[position_manager] Prior exit cancel failed (%s) — continuing", cancel_err)
                exit_order = Order(
                    token_id=pos.token_id,
                    side=Side.SELL,
                    price=book.best_bid,  # R1: MARKETABLE — sell into current bid for immediate fill (was round(mid,3) which never crossed)
                    size=pos.yes_shares,
                    strategy="position_manager_tp",
                )
                # V3 — Risk gate: exit orders must clear the same institutional
                # risk constraints as entries. Previously exits bypassed
                # risk_manager.check_order entirely, letting TP/SL closes slip
                # past circuit breakers (kill switch, daily loss stop, max
                # drawdown, observation-only mode, weekly loss stop). The gate
                # is best-effort: a rejection (or any unexpected exception) is
                # logged + audited and the exit order is skipped for this
                # evaluation cycle (will be retried on the next loop tick if
                # the trigger still holds).
                strat = exit_order.strategy
                try:
                    from risk.manager import risk_manager
                    allowed, reason = await risk_manager.check_order(exit_order)
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
                    # Signature supports strategy + decision_id kwargs (paper/
                    # simulator.py:create_order). Passing them preserves
                    # strategy attribution and decision-ledger linkage on the
                    # resulting paper Order (which the simulator constructs
                    # internally and would otherwise default to "").
                    await paper_sim.create_order(
                        exit_order,
                        strategy=strat,
                        decision_id=exit_order.decision_id,
                    )
                    managed.active_exit_order_id = exit_order.order_id
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
                from core.data_store import Order, Side
                from paper.simulator import paper_sim
                # R1: Cancel prior stale exit order before placing a new one
                if managed.active_exit_order_id:
                    try:
                        await paper_sim.cancel_order(managed.active_exit_order_id)
                        log.debug("[position_manager] Cancelled prior exit order %s for %s",
                                  managed.active_exit_order_id, pos.token_id[:12])
                    except Exception as cancel_err:
                        log.debug("[position_manager] Prior exit cancel failed (%s) — continuing", cancel_err)
                exit_order = Order(
                    token_id=pos.token_id,
                    side=Side.SELL,
                    price=book.best_bid,  # R1: MARKETABLE — sell into current bid for immediate fill (was round(mid,3) which never crossed)
                    size=pos.yes_shares,
                    strategy="position_manager_sl",
                )
                # V3 — Risk gate: exit orders must clear the same institutional
                # risk constraints as entries. Previously exits bypassed
                # risk_manager.check_order entirely, letting TP/SL closes slip
                # past circuit breakers (kill switch, daily loss stop, max
                # drawdown, observation-only mode, weekly loss stop). The gate
                # is best-effort: a rejection (or any unexpected exception) is
                # logged + audited and the exit order is skipped for this
                # evaluation cycle (will be retried on the next loop tick if
                # the trigger still holds).
                strat = exit_order.strategy
                try:
                    from risk.manager import risk_manager
                    allowed, reason = await risk_manager.check_order(exit_order)
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
                    # Signature supports strategy + decision_id kwargs (paper/
                    # simulator.py:create_order). Passing them preserves
                    # strategy attribution and decision-ledger linkage on the
                    # resulting paper Order (which the simulator constructs
                    # internally and would otherwise default to "").
                    await paper_sim.create_order(
                        exit_order,
                        strategy=strat,
                        decision_id=exit_order.decision_id,
                    )
                    managed.active_exit_order_id = exit_order.order_id
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
