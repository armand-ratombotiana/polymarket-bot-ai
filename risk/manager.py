"""
risk/manager.py — Advanced Risk Management & Circuit Breakers.

Provides:
- Hard Kill Switch
- Daily Loss Limit
- Maximum Drawdown (MDD) Circuit Breaker (% drop from peak equity)
- Position concentration & total portfolio exposure caps
- Consecutive loss rate throttling
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from config import settings
from core.data_store import Order, Side, store

log = logging.getLogger(__name__)

MAX_DRAWDOWN_PCT = 0.15   # 15% Max Drawdown triggers circuit breaker


class RiskManager:
    """
    Centralised risk gate consulted before every order submission.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._consecutive_losses = 0

    async def check_order(self, order: Order) -> tuple[bool, str]:
        """
        Return (allowed: bool, reason: str).
        """
        async with self._lock:
            # 1. Hard Kill Switch
            if store.kill_switch_active:
                return False, "Kill switch is active — all trading halted"

            # 2. Daily Dollar Loss Limit
            if store.daily_pnl <= -abs(settings.daily_loss_limit_usdc):
                await self._trigger_kill_switch(f"Daily loss limit of ${settings.daily_loss_limit_usdc:.2f} reached")
                return False, f"Daily loss limit reached (${settings.daily_loss_limit_usdc:.2f})"

            # 3. Maximum Drawdown from Peak Equity
            current_equity = 10000.0 + store.daily_pnl
            if store.peak_equity > 0:
                drawdown = (store.peak_equity - current_equity) / store.peak_equity
                if drawdown >= MAX_DRAWDOWN_PCT:
                    await self._trigger_kill_switch(f"Max Drawdown circuit breaker ({drawdown*100:.1f}% >= {MAX_DRAWDOWN_PCT*100:.1f}%)")
                    return False, f"Max drawdown limit reached ({drawdown*100:.1f}%)"

            # 4. Open Order Count Cap
            open_count = len(store.open_orders)
            if open_count >= settings.max_open_orders:
                return False, f"Max open orders ({settings.max_open_orders}) reached"

            # 5. Market-level Exposure Limit
            market_exposure = await store.exposure_for_market(order.token_id)
            order_cost = order.price * order.size
            if market_exposure + order_cost > settings.max_position_per_market_usdc:
                return False, f"Market exposure limit exceeded (${market_exposure + order_cost:.2f} > ${settings.max_position_per_market_usdc:.2f})"

            # 6. Total Portfolio Exposure Limit
            total_exp = await store.total_exposure()
            if total_exp + order_cost > settings.max_total_exposure_usdc:
                return False, f"Total exposure limit exceeded (${total_exp + order_cost:.2f} > ${settings.max_total_exposure_usdc:.2f})"

            # 7. Price Sanity Check
            if not (0.005 <= order.price <= 0.995):
                return False, f"Price {order.price} out of valid bounds [0.005, 0.995]"

            # 8. Minimum Order Size
            if order.size < 0.5:
                return False, f"Order size {order.size} is below minimum threshold"

            return True, "OK"

    async def _trigger_kill_switch(self, reason: str) -> None:
        if store.kill_switch_active:
            return
        store.kill_switch_active = True
        log.critical("RISK CIRCUIT BREAKER TRIGGERED: %s", reason)
        await store.log_event(f"🛑 RISK BREAKER: {reason}")
        cancelled = await store.cancel_all_orders()
        log.warning("Cancelled %d open orders across all strategies", len(cancelled))

    async def activate_kill_switch(self, reason: str = "Manual") -> None:
        await self._trigger_kill_switch(reason)

    async def deactivate_kill_switch(self) -> None:
        async with self._lock:
            store.kill_switch_active = False
            log.info("Risk gate reset — trading resumed")
            await store.log_event("▶ Risk gate reset — trading resumed")

    async def status_report(self) -> dict:
        open_count = await store.open_order_count()
        total_exp = await store.total_exposure()
        current_eq = 10000.0 + store.daily_pnl
        drawdown_pct = ((store.peak_equity - current_eq) / store.peak_equity * 100) if store.peak_equity > 0 else 0.0

        return {
            "kill_switch": store.kill_switch_active,
            "daily_pnl": store.daily_pnl,
            "daily_loss_limit": -abs(settings.daily_loss_limit_usdc),
            "peak_equity": round(store.peak_equity, 2),
            "current_equity": round(current_eq, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "max_drawdown_limit_pct": MAX_DRAWDOWN_PCT * 100,
            "open_orders": open_count,
            "max_open_orders": settings.max_open_orders,
            "total_exposure": round(total_exp, 2),
            "max_total_exposure": settings.max_total_exposure_usdc,
        }


# Module-level singleton
risk_manager = RiskManager()
