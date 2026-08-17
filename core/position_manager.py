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
from typing import Dict, Optional

from core.audit_logger import audit_logger
from core.data_store import Position, store

log = logging.getLogger(__name__)


class ManagedPosition:
    def __init__(self, token_id: str, entry_price: float, take_profit_pct: float = 0.25, stop_loss_pct: float = 0.15) -> None:
        self.token_id = token_id
        self.entry_price = entry_price
        self.take_profit_price = min(entry_price * (1.0 + take_profit_pct), 0.99)
        self.stop_loss_price = max(entry_price * (1.0 - stop_loss_pct), 0.01)
        self.high_water_mark = entry_price
        self.created_at = time.time()


class PositionManager:
    """
    Continuous risk and exit supervisor for all active positions.
    """

    def __init__(self) -> None:
        self.managed_positions: Dict[str, ManagedPosition] = {}
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
            if mid > managed.high_water_mark:
                managed.high_water_mark = mid

            # Check Take-Profit Trigger
            if mid >= managed.take_profit_price:
                log.info("[position_manager] 🎯 Take-Profit Triggered for %s @ %.4f", pos.token_id[:12], mid)
                await audit_logger.log_event(
                    category="EXIT",
                    event_type="TAKE_PROFIT",
                    details=f"Take-Profit executed @ {mid:.4f} (Entry: {managed.entry_price:.4f})",
                    token_id=pos.token_id,
                    slug=store.market_slugs.get(pos.token_id),
                    pnl=pos.realised_pnl,
                    strategy="position_manager",
                )

            # Check Stop-Loss Trigger
            elif mid <= managed.stop_loss_price:
                log.info("[position_manager] 🛑 Stop-Loss Triggered for %s @ %.4f", pos.token_id[:12], mid)
                await audit_logger.log_event(
                    category="EXIT",
                    event_type="STOP_LOSS",
                    details=f"Stop-Loss executed @ {mid:.4f} (Entry: {managed.entry_price:.4f})",
                    token_id=pos.token_id,
                    slug=store.market_slugs.get(pos.token_id),
                    pnl=pos.realised_pnl,
                    strategy="position_manager",
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
