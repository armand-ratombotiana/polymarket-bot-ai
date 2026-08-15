"""
risk/manager.py — Global risk gate consulted before every order submission.
Provides kill-switch, exposure limits, and daily-loss monitoring.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import settings
from core.data_store import Order, Side, store

log = logging.getLogger(__name__)


class RiskManager:
    """
    Centralised risk checks. Every strategy must call `check_order` before
    submitting any order. The kill-switch halts all new orders instantly.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    # ── Primary gate ──────────────────────────────────────────────────────

    async def check_order(self, order: Order) -> tuple[bool, str]:
        """
        Return (allowed: bool, reason: str).
        Strategies should abort if allowed is False.
        """
        async with self._lock:
            # 1. Kill switch
            if store.kill_switch_active:
                return False, "Kill switch is active — all trading halted"

            # 2. Daily loss limit
            if store.daily_pnl <= -abs(settings.daily_loss_limit_usdc):
                await self._trigger_kill_switch("Daily loss limit reached")
                return False, f"Daily loss limit of ${settings.daily_loss_limit_usdc:.2f} breached"

            # 3. Open order count
            open_count = len(store.open_orders)
            if open_count >= settings.max_open_orders:
                return False, f"Max open orders ({settings.max_open_orders}) reached"

            # 4. Per-market exposure
            market_exposure = await store.exposure_for_market(order.token_id)
            order_cost = order.price * order.size
            if market_exposure + order_cost > settings.max_position_per_market_usdc:
                return (
                    False,
                    f"Market exposure ${market_exposure + order_cost:.2f} > "
                    f"limit ${settings.max_position_per_market_usdc:.2f}",
                )

            # 5. Total portfolio exposure
            total_exp = await store.total_exposure()
            if total_exp + order_cost > settings.max_total_exposure_usdc:
                return (
                    False,
                    f"Total exposure ${total_exp + order_cost:.2f} > "
                    f"limit ${settings.max_total_exposure_usdc:.2f}",
                )

            # 6. Price sanity check (Polymarket prices are in (0, 1))
            if not (0.01 <= order.price <= 0.99):
                return False, f"Price {order.price} out of valid range [0.01, 0.99]"

            # 7. Size sanity
            if order.size < 1.0:
                return False, f"Order size {order.size} USDC is below minimum (1.0)"

            return True, "OK"

    # ── Kill switch ───────────────────────────────────────────────────────

    async def _trigger_kill_switch(self, reason: str) -> None:
        """Activate kill switch and cancel all open orders."""
        if store.kill_switch_active:
            return
        store.kill_switch_active = True
        log.critical("KILL SWITCH ACTIVATED: %s", reason)
        await store.log_event(f"🛑 KILL SWITCH: {reason}")
        cancelled = await store.cancel_all_orders()
        log.warning("Cancelled %d open orders in data store", len(cancelled))

    async def activate_kill_switch(self, reason: str = "Manual") -> None:
        """Manually activate the kill switch."""
        await self._trigger_kill_switch(reason)

    async def deactivate_kill_switch(self) -> None:
        """Re-arm trading after resolving the kill-switch condition."""
        async with self._lock:
            store.kill_switch_active = False
            log.info("Kill switch deactivated — trading resumed")
            await store.log_event("✅ Kill switch deactivated — trading resumed")

    # ── Status helpers ────────────────────────────────────────────────────

    async def status_report(self) -> dict:
        open_count = await store.open_order_count()
        total_exp = await store.total_exposure()
        return {
            "kill_switch": store.kill_switch_active,
            "daily_pnl": store.daily_pnl,
            "daily_loss_limit": -settings.daily_loss_limit_usdc,
            "open_orders": open_count,
            "max_open_orders": settings.max_open_orders,
            "total_exposure": total_exp,
            "max_total_exposure": settings.max_total_exposure_usdc,
        }


# Module-level singleton
risk_manager = RiskManager()
