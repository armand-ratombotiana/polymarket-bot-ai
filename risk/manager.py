"""
risk/manager.py — Institutional Risk Governance Engine (USD 100 operating / USD 200 ceiling).

Operating capital model:
  recognized_operating_capital = min(verified equity, USD 100)
  Hard bankroll ceiling (never auto-increased): USD 200
  Automated LIVE sizing operates from USD 100 only.

Conservative defaults (never increased without explicit manual authorization):
  - Operating capital: USD 100.00
  - Hard ceiling: USD 200.00
  - Minimum cash reserve: USD 40.00 (Max deployable capital: USD 60.00)
  - Default experimental trade: USD 1.00
  - Normal trade: USD 1-2
  - Max per market: USD 3.00
  - Absolute exceptional maximum: USD 5.00
  - Max correlated event group exposure: USD 8.00
  - Max exposure per strategy: USD 15.00
  - Max simultaneous worst-case open risk: USD 25.00
  - Max pending-order capital: USD 10.00
  - Max simultaneous open positions: 8
  - Daily loss stop: USD 2.00 (Hard Circuit Breaker)
  - Weekly loss stop: USD 5.00
  - Max drawdown from high-water mark: USD 8.00 (Hard Circuit Breaker)

Live trading is DISABLED by default. The strictest relevant limit always applies.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from config import settings
from core.data_store import Order, Side, store

log = logging.getLogger(__name__)

# Approved USD 100 operating / USD 200 ceiling Portfolio Constraints.
# recognized_operating_capital = min(verified equity, OPERATING_CAPITAL)
OPERATING_CAPITAL = Decimal("100.00")
BANKROLL_CEILING = Decimal("200.00")
MIN_CASH_RESERVE = Decimal("40.00")
MAX_DEPLOYABLE_CAPITAL = Decimal("60.00")
DEFAULT_EXPERIMENTAL_POSITION = Decimal("1.00")
NORMAL_MAX_POSITION = Decimal("2.00")
MAX_POSITION_PER_MARKET = Decimal("3.00")
ABSOLUTE_MAX_POSITION = Decimal("5.00")
MAX_CORRELATED_EXPOSURE = Decimal("8.00")
MAX_STRATEGY_EXPOSURE = Decimal("15.00")
MAX_TOTAL_OPEN_RISK = Decimal("25.00")
MAX_PENDING_ORDER_CAPITAL = Decimal("10.00")
MAX_OPEN_POSITIONS = 8
DAILY_LOSS_STOP = Decimal("2.00")
WEEKLY_LOSS_STOP = Decimal("5.00")
MAX_DRAWDOWN_LIMIT = Decimal("8.00")


def recognized_operating_capital(verified_equity: float) -> Decimal:
    """recognized_operating_capital = min(verified equity, USD 100)."""
    return min(to_dec(verified_equity), OPERATING_CAPITAL)


def to_dec(val: float) -> Decimal:
    """Decimal-safe converter for financial accounting."""
    return Decimal(str(round(val, 4)))


class InstitutionalRiskEngine:
    """
    Central pre-trade risk and portfolio supervisor.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Observation-only mode: when open exposure is not reconciled, new live
        # orders are disabled and the platform operates read-only.
        self.observation_only = False
        self.observation_reason = ""

    async def set_observation_mode(self, active: bool, reason: str = "") -> dict:
        async with self._lock:
            self.observation_only = bool(active)
            self.observation_reason = reason if active else ""
            status = "ENABLED" if active else "DISABLED"
            log.warning("[risk_manager] Observation-only mode %s — %s", status, reason or "manual")
            await store.log_event(f"👁 Observation-only mode {status}: {reason or 'manual'}")
            return {"observation_only": self.observation_only, "reason": self.observation_reason}

    async def check_order(self, order: Order) -> tuple[bool, str]:
        """
        Validate order against all institutional risk constraints before submission.
        """
        async with self._lock:
            # 0. Shadow mode gate: shadow = evaluation only, no orders at all.
            if settings.trading_mode == "shadow":
                return False, (
                    "Shadow trading mode active — evaluation only, no orders "
                    "(see /api/system/mode)"
                )

            # 0. Durable kill switch (file-backed, survives restarts) + in-memory flag.
            from core.safety import kill_switch_file_exists
            if store.kill_switch_active or kill_switch_file_exists():
                return False, "Kill switch is active — all trading halted"

            # 0. Observation-only gate: no new LIVE orders until exposure is reconciled.
            if self.observation_only and not order.paper:
                return False, (
                    f"Observation-only mode active ({self.observation_reason or 'exposure not reconciled'}) "
                    f"— new live orders disabled"
                )

            # 0b. Self-enforcing reconciliation gate: live orders are disabled while
            # open exposure exceeds the deployable ceiling (exposure not reconciled).
            if not order.paper:
                current_exp = to_dec(await store.total_exposure())
                if current_exp > MAX_DEPLOYABLE_CAPITAL:
                    return False, (
                        f"Exposure ${float(current_exp):.2f} not reconciled against the "
                        f"${float(MAX_DEPLOYABLE_CAPITAL):.2f} deployable ceiling — operating "
                        f"observation-only; reconcile exposure before resuming live trading"
                    )

            # 0c. Live trading disabled by default — requires explicit manual authorization.
            if not order.paper and not settings.live_trading_enabled:
                return False, "Live trading is disabled by default — enable explicitly to trade real funds"

            # 1. Global Kill Switch check
            if store.kill_switch_active:
                return False, "Kill switch is active — all trading halted"

            order_cost = to_dec(order.price) * to_dec(order.size)
            daily_pnl_dec = to_dec(store.daily_pnl)
            peak_equity_dec = to_dec(store.peak_equity)
            current_equity = BANKROLL_CEILING + daily_pnl_dec

            # 2. Daily Loss Stop (USD 2.00)
            if daily_pnl_dec <= -DAILY_LOSS_STOP:
                await self._trigger_kill_switch(f"Daily loss stop breach (${abs(daily_pnl_dec):.2f} >= ${DAILY_LOSS_STOP:.2f})")
                return False, f"Daily loss stop reached (${DAILY_LOSS_STOP:.2f})"

            # 2b. Weekly Loss Stop (USD 5.00) — enforced per P0-GOV-01.
            store.roll_weekly_window()
            weekly_pnl_dec = to_dec(store.weekly_pnl)
            if weekly_pnl_dec <= -WEEKLY_LOSS_STOP:
                await self._trigger_kill_switch(f"Weekly loss stop breach (${abs(weekly_pnl_dec):.2f} >= ${WEEKLY_LOSS_STOP:.2f})")
                return False, f"Weekly loss stop reached (${WEEKLY_LOSS_STOP:.2f})"

            # 3. Max Drawdown from High-Water Mark (USD 8.00)
            if peak_equity_dec > Decimal("0.0"):
                drawdown_dollars = peak_equity_dec - current_equity
                if drawdown_dollars >= MAX_DRAWDOWN_LIMIT:
                    await self._trigger_kill_switch(f"Max Drawdown stop breach (${drawdown_dollars:.2f} >= ${MAX_DRAWDOWN_LIMIT:.2f})")
                    return False, f"Max drawdown limit reached (${MAX_DRAWDOWN_LIMIT:.2f})"

            # 4. Cash Reserve Protection ($40 Reserve minimum / $60 deployable)
            total_exp = to_dec(await store.total_exposure())
            if order.side == Side.BUY and (total_exp + order_cost) > MAX_DEPLOYABLE_CAPITAL:
                return False, f"Cash reserve breach: total exposure ${total_exp + order_cost:.2f} exceeds deployable capital ${MAX_DEPLOYABLE_CAPITAL:.2f}"

            # 5. Total Simultaneous Open Risk ($25 max)
            if order.side == Side.BUY and (total_exp + order_cost) > MAX_TOTAL_OPEN_RISK:
                return False, f"Total open risk cap exceeded (${total_exp + order_cost:.2f} > ${MAX_TOTAL_OPEN_RISK:.2f})"

            # 6. Max position per market ($3) and absolute exceptional max ($5).
            market_exp = to_dec(await store.exposure_for_market(order.token_id))
            if order.side == Side.BUY and (market_exp + order_cost) > MAX_POSITION_PER_MARKET:
                return False, f"Per-market position cap exceeded (${market_exp + order_cost:.2f} > ${MAX_POSITION_PER_MARKET:.2f})"
            if order.side == Side.BUY and (market_exp + order_cost) > ABSOLUTE_MAX_POSITION:
                return False, f"Absolute position cap exceeded (${market_exp + order_cost:.2f} > ${ABSOLUTE_MAX_POSITION:.2f})"

            # 6b. Normal position size guidance ($2 max for new/experimental positions)
            if order.side == Side.BUY and market_exp <= 0 and order_cost > NORMAL_MAX_POSITION:
                return False, f"Normal position cap exceeded for new position (${order_cost:.2f} > ${NORMAL_MAX_POSITION:.2f})"

            # 6c. Per-strategy exposure cap ($15 max)
            if order.side == Side.BUY and order.strategy:
                strat_exp = to_dec(sum(
                    p.current_exposure for p in store.positions.values() if p.strategy == order.strategy
                ))
                if (strat_exp + order_cost) > MAX_STRATEGY_EXPOSURE:
                    return False, f"Strategy exposure cap exceeded (${strat_exp + order_cost:.2f} > ${MAX_STRATEGY_EXPOSURE:.2f})"

            # 6d. Correlated event-group exposure cap ($8 max). Positions are grouped
            # by market slug; positions sharing a slug share the same underlying risk.
            if order.side == Side.BUY:
                slug = store.market_slugs.get(order.token_id, "")
                if slug:
                    group_exp = to_dec(sum(
                        p.current_exposure for p in store.positions.values()
                        if p.market_slug == slug
                    ))
                    if (group_exp + order_cost) > MAX_CORRELATED_EXPOSURE:
                        return False, f"Correlated exposure cap exceeded (${group_exp + order_cost:.2f} > ${MAX_CORRELATED_EXPOSURE:.2f})"

            # 7. Max Open Positions Count (8) — only active positions count
            if order.side == Side.BUY:
                active_positions = sum(1 for p in store.positions.values() if p.yes_shares > 0.001 or p.total_invested > 0.01)
                existing_pos = store.positions.get(order.token_id)
                is_new_market = not existing_pos or (existing_pos.yes_shares <= 0.001 and existing_pos.total_invested <= 0.01)

                if is_new_market and active_positions >= MAX_OPEN_POSITIONS:
                    return False, f"Max simultaneous open positions ({MAX_OPEN_POSITIONS}) reached"

            # 8. Pending Order Capital Cap ($10 max)
            pending_capital = sum(to_dec(o.price) * to_dec(o.size) for o in store.open_orders.values())
            if (pending_capital + order_cost) > MAX_PENDING_ORDER_CAPITAL:
                return False, f"Pending order capital cap exceeded (${pending_capital + order_cost:.2f} > ${MAX_PENDING_ORDER_CAPITAL:.2f})"

            # 9. Max Open Order Count
            if len(store.open_orders) >= settings.max_open_orders:
                return False, f"Max open orders ({settings.max_open_orders}) reached"

            # 10. Price Sanity & Microstructure Bounds [0.01, 0.99]
            if not (0.01 <= order.price <= 0.99):
                return False, f"Price {order.price} out of valid bounds [0.01, 0.99]"

            # 11. Minimum Order Sizing ($0.50 minimum)
            if order.size < 0.5:
                return False, f"Order size {order.size} is below minimum liquidity threshold"

            # 12. Bankroll ceiling never auto-increases; expose max loss vs reserve.
            max_loss = BANKROLL_CEILING - MIN_CASH_RESERVE
            if order.side == Side.BUY and (total_exp + order_cost) > max_loss:
                return False, f"Order would put max possible loss ${total_exp + order_cost:.2f} above the deployable bankroll ceiling ${max_loss:.2f}"

            return True, "OK"

    async def _trigger_kill_switch(self, reason: str) -> None:
        if store.kill_switch_active:
            return
        from core.safety import write_kill_switch
        store.kill_switch_active = True
        write_kill_switch(reason)
        log.critical("[risk_manager] 🛑 CIRCUIT BREAKER TRIGGERED: %s", reason)
        await store.log_event(f"🛑 RISK BREAKER: {reason}")
        from core.audit_logger import audit_logger
        await audit_logger.log_event(
            category="risk", event_type="kill_switch_activated",
            details=f"CIRCUIT BREAKER: {reason}",
        )
        cancelled = await store.cancel_all_orders()
        log.warning("[risk_manager] Cancelled %d open orders across all strategies", len(cancelled))

    async def activate_kill_switch(self, reason: str = "Manual") -> None:
        await self._trigger_kill_switch(reason)

    async def deactivate_kill_switch(self) -> None:
        async with self._lock:
            from core.safety import clear_kill_switch
            store.kill_switch_active = False
            clear_kill_switch()
            # Update peak equity to current equity to reset MDD baseline
            current_eq = float(BANKROLL_CEILING) + store.daily_pnl
            store.peak_equity = current_eq
            log.info("[risk_manager] Risk gate reset — trading resumed (peak equity=$%.2f)", current_eq)
            await store.log_event("▶ Risk gate reset — trading resumed")
            from core.audit_logger import audit_logger
            await audit_logger.log_event(
                category="risk", event_type="kill_switch_deactivated",
                details="Manual risk gate reset — trading resumed",
            )

    async def status_report(self) -> dict:
        from core.safety import kill_switch_file_exists
        open_count = await store.open_order_count()
        total_exp = await store.total_exposure()
        current_eq = float(BANKROLL_CEILING) + store.daily_pnl
        drawdown_dollars = max(store.peak_equity - current_eq, 0.0)
        pending_capital = sum(
            to_dec(o.price) * to_dec(o.size) for o in store.open_orders.values()
        )
        max_loss = float(BANKROLL_CEILING - MIN_CASH_RESERVE)
        # Exposure must stay within the deployable bankroll; if it exceeds the
        # absolute worst-case ceiling, the reconciliation gate is open (non-compliant).
        exposure_reconciled = float(total_exp) <= max_loss
        store.roll_weekly_window()

        return {
            "kill_switch": store.kill_switch_active,
            "kill_switch_durable": bool(kill_switch_file_exists()),
            "observation_only": self.observation_only,
            "observation_reason": self.observation_reason,
            "operating_capital": float(OPERATING_CAPITAL),
            "recognized_operating_capital": float(recognized_operating_capital(float(BANKROLL_CEILING) + store.daily_pnl)),
            "bankroll_ceiling": float(BANKROLL_CEILING),
            "cash_reserve_min": float(MIN_CASH_RESERVE),
            "max_deployable_capital": float(MAX_DEPLOYABLE_CAPITAL),
            "live_trading_enabled": bool(settings.live_trading_enabled),
            "daily_pnl": store.daily_pnl,
            "daily_loss_limit": -float(DAILY_LOSS_STOP),
            "weekly_pnl": round(store.weekly_pnl, 2),
            "weekly_loss_limit": -float(WEEKLY_LOSS_STOP),
            "drawdown_dollars": round(drawdown_dollars, 2),
            "max_drawdown_limit": float(MAX_DRAWDOWN_LIMIT),
            "open_orders": open_count,
            "max_open_orders": settings.max_open_orders,
            "total_exposure": round(float(total_exp), 2),
            "max_total_exposure": float(MAX_TOTAL_OPEN_RISK),
            "max_position_per_market": float(MAX_POSITION_PER_MARKET),
            "absolute_max_position": float(ABSOLUTE_MAX_POSITION),
            "max_correlated_exposure": float(MAX_CORRELATED_EXPOSURE),
            "max_strategy_exposure": float(MAX_STRATEGY_EXPOSURE),
            "pending_order_capital": round(float(pending_capital), 2),
            "max_pending_order_capital": float(MAX_PENDING_ORDER_CAPITAL),
            "max_loss_if_all_zero": round(float(total_exp), 2),
            "deployable_ceiling": round(float(MAX_DEPLOYABLE_CAPITAL), 2),
            "exposure_reconciled": exposure_reconciled,
        }


# Global singleton
risk_manager = InstitutionalRiskEngine()
