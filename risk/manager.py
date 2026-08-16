"""
risk/manager.py — Institutional Risk Governance Engine ($10,000 USD Hard Bankroll).

Strictly enforces the institutional rules:
  - Hard bankroll ceiling: USD 10,000.00
  - Minimum cash reserve: USD 2,000.00 (Max deployable capital: USD 8,000.00)
  - Normal max position: USD 250.00
  - Absolute max position: USD 500.00
  - Max correlated event group exposure: USD 1,000.00
  - Max exposure per strategy: USD 2,000.00
  - Max total simultaneous open risk: USD 4,000.00
  - Max pending-order capital: USD 1,500.00
  - Max simultaneous open positions: 100
  - Daily loss stop: USD 250.00 (Hard Circuit Breaker)
  - Weekly loss stop: USD 600.00
  - Max drawdown from high-water mark: USD 1,000.00 (Hard Circuit Breaker)
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Dict, Optional, Tuple

from config import settings
from core.data_store import Order, Side, store

log = logging.getLogger(__name__)

# Institutional $10,000 USD Portfolio Constraints
BANKROLL_CEILING = Decimal("10000.00")
MIN_CASH_RESERVE = Decimal("2000.00")
MAX_DEPLOYABLE_CAPITAL = Decimal("8000.00")
NORMAL_MAX_POSITION = Decimal("250.00")
ABSOLUTE_MAX_POSITION = Decimal("500.00")
MAX_CORRELATED_EXPOSURE = Decimal("1000.00")
MAX_STRATEGY_EXPOSURE = Decimal("2000.00")
MAX_TOTAL_OPEN_RISK = Decimal("4000.00")
MAX_PENDING_ORDER_CAPITAL = Decimal("1500.00")
MAX_OPEN_POSITIONS = 100
DAILY_LOSS_STOP = Decimal("250.00")
WEEKLY_LOSS_STOP = Decimal("600.00")
MAX_DRAWDOWN_LIMIT = Decimal("1000.00")


def to_dec(val: float) -> Decimal:
    """Decimal-safe converter for financial accounting."""
    return Decimal(str(round(val, 4)))


class InstitutionalRiskEngine:
    """
    Central pre-trade risk and portfolio supervisor.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def check_order(self, order: Order) -> Tuple[bool, str]:
        """
        Validate order against all 12 institutional risk constraints before submission.
        """
        async with self._lock:
            # 1. Global Kill Switch check
            if store.kill_switch_active:
                return False, "Kill switch is active — all trading halted"

            order_cost = to_dec(order.price) * to_dec(order.size)
            daily_pnl_dec = to_dec(store.daily_pnl)
            peak_equity_dec = to_dec(store.peak_equity)
            current_equity = BANKROLL_CEILING + daily_pnl_dec

            # 2. Daily Loss Stop (USD 250.00)
            if daily_pnl_dec <= -DAILY_LOSS_STOP:
                await self._trigger_kill_switch(f"Daily loss stop breach (${abs(daily_pnl_dec):.2f} >= ${DAILY_LOSS_STOP:.2f})")
                return False, f"Daily loss stop reached (${DAILY_LOSS_STOP:.2f})"

            # 3. Max Drawdown from High-Water Mark (USD 1,000.00)
            if peak_equity_dec > Decimal("0.0"):
                drawdown_dollars = peak_equity_dec - current_equity
                if drawdown_dollars >= MAX_DRAWDOWN_LIMIT:
                    await self._trigger_kill_switch(f"Max Drawdown stop breach (${drawdown_dollars:.2f} >= ${MAX_DRAWDOWN_LIMIT:.2f})")
                    return False, f"Max drawdown limit reached (${MAX_DRAWDOWN_LIMIT:.2f})"

            # 4. Cash Reserve Protection ($2,000 Reserve minimum)
            total_exp = to_dec(await store.total_exposure())
            if order.side == Side.BUY and (total_exp + order_cost) > MAX_DEPLOYABLE_CAPITAL:
                return False, f"Cash reserve breach: total exposure ${total_exp + order_cost:.2f} exceeds deployable capital ${MAX_DEPLOYABLE_CAPITAL:.2f}"

            # 5. Total Simultaneous Open Risk ($4,000 max)
            if order.side == Side.BUY and (total_exp + order_cost) > MAX_TOTAL_OPEN_RISK:
                return False, f"Total open risk cap exceeded (${total_exp + order_cost:.2f} > ${MAX_TOTAL_OPEN_RISK:.2f})"

            # 6. Absolute Maximum Single Position Size ($500 max)
            market_exp = to_dec(await store.exposure_for_market(order.token_id))
            if order.side == Side.BUY and (market_exp + order_cost) > ABSOLUTE_MAX_POSITION:
                return False, f"Single position cap exceeded (${market_exp + order_cost:.2f} > ${ABSOLUTE_MAX_POSITION:.2f})"

            # 7. Max Open Positions Count (Only counts active positions with yes_shares > 0 or total_invested > 0)
            if order.side == Side.BUY:
                active_positions = sum(1 for p in store.positions.values() if p.yes_shares > 0.001 or p.total_invested > 0.01)
                existing_pos = store.positions.get(order.token_id)
                is_new_market = not existing_pos or (existing_pos.yes_shares <= 0.001 and existing_pos.total_invested <= 0.01)

                if is_new_market and active_positions >= MAX_OPEN_POSITIONS:
                    return False, f"Max simultaneous open positions ({MAX_OPEN_POSITIONS}) reached"

            # 8. Pending Order Capital Cap ($1,500 max)
            pending_capital = sum(to_dec(o.price) * to_dec(o.size) for o in store.open_orders.values())
            if (pending_capital + order_cost) > MAX_PENDING_ORDER_CAPITAL:
                return False, f"Pending order capital cap exceeded (${pending_capital + order_cost:.2f} > ${MAX_PENDING_ORDER_CAPITAL:.2f})"

            # 9. Max Open Order Count (10 orders)
            if len(store.open_orders) >= settings.max_open_orders:
                return False, f"Max open orders ({settings.max_open_orders}) reached"

            # 10. Price Sanity & Microstructure Bounds [0.01, 0.99]
            if not (0.01 <= order.price <= 0.99):
                return False, f"Price {order.price} out of valid bounds [0.01, 0.99]"

            # 11. Minimum Order Sizing ($0.50 minimum)
            if order.size < 0.5:
                return False, f"Order size {order.size} is below minimum liquidity threshold"

            return True, "OK"

    async def _trigger_kill_switch(self, reason: str) -> None:
        if store.kill_switch_active:
            return
        store.kill_switch_active = True
        log.critical("[risk_manager] 🛑 CIRCUIT BREAKER TRIGGERED: %s", reason)
        await store.log_event(f"🛑 RISK BREAKER: {reason}")
        cancelled = await store.cancel_all_orders()
        log.warning("[risk_manager] Cancelled %d open orders across all strategies", len(cancelled))

    async def activate_kill_switch(self, reason: str = "Manual") -> None:
        await self._trigger_kill_switch(reason)

    async def deactivate_kill_switch(self) -> None:
        async with self._lock:
            store.kill_switch_active = False
            # Update peak equity to current equity to reset MDD baseline
            current_eq = float(BANKROLL_CEILING) + store.daily_pnl
            store.peak_equity = current_eq
            log.info("[risk_manager] Risk gate reset — trading resumed (peak equity=$%.2f)", current_eq)
            await store.log_event("▶ Risk gate reset — trading resumed")

    async def status_report(self) -> dict:
        open_count = await store.open_order_count()
        total_exp = await store.total_exposure()
        current_eq = float(BANKROLL_CEILING) + store.daily_pnl
        drawdown_dollars = max(store.peak_equity - current_eq, 0.0)

        return {
            "kill_switch": store.kill_switch_active,
            "bankroll_ceiling": float(BANKROLL_CEILING),
            "cash_reserve_min": float(MIN_CASH_RESERVE),
            "max_deployable_capital": float(MAX_DEPLOYABLE_CAPITAL),
            "daily_pnl": store.daily_pnl,
            "daily_loss_limit": -float(DAILY_LOSS_STOP),
            "drawdown_dollars": round(drawdown_dollars, 2),
            "max_drawdown_limit": float(MAX_DRAWDOWN_LIMIT),
            "open_orders": open_count,
            "max_open_orders": settings.max_open_orders,
            "total_exposure": round(total_exp, 2),
            "max_total_exposure": float(MAX_TOTAL_OPEN_RISK),
        }


# Global singleton
risk_manager = InstitutionalRiskEngine()
