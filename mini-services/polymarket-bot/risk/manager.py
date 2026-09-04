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
import time
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

# Per-trade circuit breaker: a single trade losing >= PER_TRADE_MAX_LOSS USD
# pauses the responsible strategy for STRATEGY_COOLDOWN seconds. Prevents a
# degenerate strategy from compounding losses before the daily/weekly/MDD
# breakers trip. Reported via report_trade_pnl() / inspected via
# is_strategy_paused().
PER_TRADE_MAX_LOSS = 0.50  # USD — single-trade loss threshold (per-strategy pause)
STRATEGY_COOLDOWN = 300.0  # seconds — strategy pause window after per-trade breach


def recognized_operating_capital(verified_equity: float) -> Decimal:
    """recognized_operating_capital = min(verified equity, USD 100)."""
    return min(to_dec(verified_equity), OPERATING_CAPITAL)


def to_dec(val: float) -> Decimal:
    """Decimal-safe converter for financial accounting."""
    return Decimal(str(round(val, 4)))


def dynamic_model_risk_multiplier() -> Decimal:
    """
    Computes a risk multiplier [0.30, 1.00] based on live ML model calibration and concept drift:
      - Healthy (PSI < 0.10, Brier <= 0.16) -> 1.00 (100% standard capacity)
      - Moderate shift / elevated Brier (PSI >= 0.10 or Brier > 0.16) -> 0.60 (60% capacity)
      - Significant drift / degraded Brier (PSI >= 0.20 or Brier > 0.22) -> 0.30 (30% capacity)
    """
    try:
        from ml.drift_detector import drift_detector
        from ml.model import ml_model
        status = drift_detector.drift_status
        brier = drift_detector.rolling_brier if drift_detector.rolling_brier is not None else ml_model.brier_score

        if status == "SIGNIFICANT_DRIFT" or brier > 0.22:
            return Decimal("0.30")
        elif status == "MODERATE_SHIFT" or brier > 0.16:
            return Decimal("0.60")
        return Decimal("1.00")
    except Exception:
        return Decimal("1.00")


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
        # Per-trade max-loss circuit breaker state: strategy -> monotonic clock
        # timestamp at which its cooldown expires. Populated by
        # report_trade_pnl(); consulted by is_strategy_paused() and the
        # check_order() gate so a paused strategy cannot open new positions.
        self._strategy_cooldowns: dict[str, float] = {}

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

        On any rejection (any ``return False, reason`` path inside the
        delegated ``_check_order_impl``), records a counterfactual shadow
        trade via ``core.shadow_trading.record_shadow_trade`` so the shadow
        trading journal captures "what would have been traded" entries for
        every risk-rejected order (God Mode §75). The recording is
        fire-and-forget (``asyncio.create_task``) and wrapped in
        ``try/except: pass`` so it can never alter the rejection return
        value or block the caller — the trading pipeline never blocks on
        shadow-journal writes (mirrors the contract on
        ``core.decision_ledger.record`` and
        ``core.closed_positions.record_closed_position``).
        """
        result = await self._check_order_impl(order)
        if not result[0]:
            # Order rejected — record counterfactual shadow trade so the
            # shadow journal captures "what would have been traded" entries
            # on every risk rejection. Fire-and-forget: schedules the
            # coroutine on the event loop without blocking the return path.
            try:
                from core.shadow_trading import record_shadow_trade
                import asyncio
                asyncio.create_task(record_shadow_trade(
                    decision_id=getattr(order, 'decision_id', ''),
                    token_id=order.token_id,
                    strategy=order.strategy,
                    side=order.side.value if hasattr(order.side, 'value') else str(order.side),
                    price=order.price,
                    size=order.size,
                    predicted_edge=0.0,
                    confidence=0.0,
                ))
            except Exception:
                pass
            # W22-4 — also record a rejected-opportunity row so the
            # operator dashboard surfaces "what was rejected + why" in
            # the analytics roll-up. Mirrors the shadow-trade wiring:
            # fire-and-forget, wrapped in try/except: pass so it can
            # never alter the rejection return value or block the caller.
            try:
                from core.rejected_opportunities import (
                    record_rejected_opportunity,
                )
                import asyncio as _asyncio_mod
                _asyncio_mod.create_task(record_rejected_opportunity(
                    token_id=order.token_id,
                    strategy=order.strategy or "",
                    signal_action=(
                        order.side.value
                        if hasattr(order.side, 'value') else str(order.side)
                    ),
                    signal_price=float(order.price) if order.price else 0.0,
                    signal_size=float(order.size) if order.size else 0.0,
                    predicted_edge=0.0,
                    confidence=0.0,
                    rejection_reason=result[1],
                    rejection_details={
                        "raw_message": result[1],
                        "paper": bool(getattr(order, "paper", False)),
                    },
                    correlation_id=getattr(order, 'decision_id', None),
                ))
            except Exception:
                pass
        return result

    async def _check_order_impl(self, order: Order) -> tuple[bool, str]:
        """
        Validate order against all institutional risk constraints before submission.

        Internal implementation — the public ``check_order`` wrapper delegates
        here and records a shadow trade on any rejection path. The existing
        rejection logic is preserved verbatim under this private name so the
        shadow-trade recording could be added as a single additive wrapper
        (no existing gate logic modified).
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

            # 0d. Per-trade max-loss circuit breaker: a strategy that recently
            # lost >= PER_TRADE_MAX_LOSS on a single closed trade is paused for
            # STRATEGY_COOLDOWN seconds and may not open new positions.
            if order.side == Side.BUY and order.strategy and self.is_strategy_paused(order.strategy):
                cooldown_until = self._strategy_cooldowns.get(order.strategy, 0.0)
                remaining = max(cooldown_until - time.monotonic(), 0.0)
                return False, (
                    f"Strategy '{order.strategy}' is in per-trade-loss cooldown "
                    f"({remaining:.0f}s remaining) — new orders blocked"
                )

            # 1. Global Kill Switch check
            if store.kill_switch_active:
                return False, "Kill switch is active — all trading halted"

            order_cost = to_dec(order.price) * to_dec(order.size)
            daily_pnl_dec = to_dec(store.daily_pnl)
            peak_equity_dec = to_dec(store.peak_equity)
            # MDD baseline must mirror the peak-equity tracker, which is
            # measured against the USD 100 operating capital (not the USD 200
            # hard ceiling). Using BANKROLL_CEILING here made drawdown always
            # negative, so the MDD breaker never tripped.
            current_equity = OPERATING_CAPITAL + daily_pnl_dec

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

            # Dynamic ML-health sizing scaling
            ml_risk_mult = dynamic_model_risk_multiplier()
            effective_mkt_cap = MAX_POSITION_PER_MARKET * ml_risk_mult
            effective_norm_cap = NORMAL_MAX_POSITION * ml_risk_mult

            # 6. Max position per market (scaled dynamically with ML calibration)
            market_exp = to_dec(await store.exposure_for_market(order.token_id))
            if order.side == Side.BUY and (market_exp + order_cost) > effective_mkt_cap:
                return False, f"Per-market position cap exceeded (${market_exp + order_cost:.2f} > ${effective_mkt_cap:.2f}, scale={ml_risk_mult*100:.0f}%)"
            if order.side == Side.BUY and (market_exp + order_cost) > ABSOLUTE_MAX_POSITION:
                return False, f"Absolute position cap exceeded (${market_exp + order_cost:.2f} > ${ABSOLUTE_MAX_POSITION:.2f})"

            # 6b. Normal position size guidance (scaled dynamically with ML calibration)
            if order.side == Side.BUY and market_exp <= 0 and order_cost > effective_norm_cap:
                return False, f"Normal position cap exceeded for new position (${order_cost:.2f} > ${effective_norm_cap:.2f}, scale={ml_risk_mult*100:.0f}%)"

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

            # 6e. Mark-to-market exposure cap ($25 max). The section-5 cap
            # above is enforced on cost-basis exposure (`store.total_exposure()`),
            # which does NOT move when an open position's market value rises.
            # A profitable position can therefore silently widen true risk past
            # the $25 ceiling simply because its mark has appreciated. This gate
            # re-checks the same $25 cap on a mark-to-market basis so unrealized
            # gains cannot outflank the cap.
            #
            # P0-C06 (W18-6) — FAIL CLOSED. The previous implementation wrapped
            # the MTM call in `try: ... except: pass`, silently failing OPEN
            # (allowing every order through whenever the MTM computation
            # raised). That meant a broken price feed, a broken MTM module, or
            # even a simple type error would let orders pass with NO mark-to-
            # market supervision — the exact opposite of the gate's purpose.
            # Worse: the legacy import path
            # (`from core.portfolio import compute_mark_to_market_exposure`)
            # never resolved (the function lives in
            # `core.portfolio_mark_to_market`), so every call raised
            # ImportError, was swallowed by the bare except, and the MTM gate
            # was effectively a no-op for every order since the gate was added.
            # On top of that, the function is SYNC but the call site
            # `await`-ed it — TypeError on the await was also swallowed by
            # the bare except.
            #
            # The gate now:
            #   * imports from the canonical module path;
            #   * calls the (sync) function WITHOUT await;
            #   * on ANY exception, logs at ERROR, fires a CRITICAL alert,
            #     increments a Prometheus counter, and returns False — every
            #     order is blocked until the price feed / MTM module is
            #     repaired.
            # Operators MUST treat an MTM gate failure as a hard trading halt,
            # not a degraded mode — the W17-8 P0-C06 assessment explicitly
            # classified silent fail-open as a P0 risk.
            try:
                from core.portfolio_mark_to_market import compute_mark_to_market_exposure
                mtm = compute_mark_to_market_exposure()  # SYNC — do NOT await
                mtm_total = to_dec(float(mtm.get('total_exposure_mark', 0.0)))
                if mtm_total + order_cost > Decimal('25.0'):
                    return False, (
                        f'Mark-to-market exposure ${float(mtm_total):.2f} '
                        f'+ order ${float(order_cost):.2f} exceeds $25.00 cap'
                    )
            except Exception as mtm_err:
                # ── FAIL CLOSED — block every order until the price feed is repaired.
                log.error(
                    "[risk_manager] MTM gate FAILED CLOSED: %r — blocking ALL "
                    "trades. RECOMMENDATION: check order_books / price feeds, "
                    "the MTM module (core.portfolio_mark_to_market), and "
                    "position integrity before resuming trading.",
                    mtm_err, exc_info=True,
                )
                await store.log_event(
                    f"🛑 MTM gate FAILED CLOSED: {mtm_err!r} — all trades "
                    f"blocked; check price feeds (order_books) and "
                    f"core.portfolio_mark_to_market"
                )
                # Prometheus metric (best-effort).
                try:
                    from core.prometheus_metrics import mtm_gate_failures_total
                    mtm_gate_failures_total.inc()
                except Exception:
                    log.debug(
                        "[risk_manager] prometheus_metrics unavailable while "
                        "recording MTM gate failure",
                        exc_info=True,
                    )
                # CRITICAL alert (best-effort).
                try:
                    import time as _time
                    from core.alerting import (
                        Alert,
                        SEVERITY_CRITICAL,
                        alert_engine,
                    )
                    alert = Alert(
                        alert_id=f"mtm_gate_fail_closed_{int(_time.time() * 1000)}",
                        timestamp=_time.time(),
                        category="risk",
                        name="mtm_gate_fail_closed",
                        severity=SEVERITY_CRITICAL,
                        message=(
                            f"MTM risk gate failed closed: {mtm_err!r}. All "
                            f"trading halted until the price feed / MTM module "
                            f"is repaired. Check core.portfolio_mark_to_market "
                            f"and store.order_books for missing mid quotes."
                        ),
                        value=None,
                        threshold=None,
                        metadata={"exception": repr(mtm_err)},
                    )
                    alert_engine.fire_alert(alert)
                except Exception:
                    log.debug(
                        "[risk_manager] alerting unavailable while firing MTM "
                        "fail-closed alert",
                        exc_info=True,
                    )
                return False, (
                    f"MTM risk gate failed closed ({mtm_err!r}) — all trades "
                    f"blocked; check price feeds and the MTM module before "
                    f"resuming"
                )

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

    def is_strategy_paused(self, strategy: str | None) -> bool:
        """
        Return True if `strategy` is currently in its per-trade-loss cooldown
        window (i.e. a recent closed trade lost >= PER_TRADE_MAX_LOSS USD and
        STRATEGY_COOLDOWN seconds have not yet elapsed). Expired cooldowns are
        lazily cleared on read.
        """
        if not strategy:
            return False
        cooldown_until = self._strategy_cooldowns.get(strategy)
        if cooldown_until is None:
            return False
        if time.monotonic() >= cooldown_until:
            # Cooldown elapsed — clear it and report unpaused.
            self._strategy_cooldowns.pop(strategy, None)
            return False
        return True

    async def report_trade_pnl(self, strategy: str | None, pnl: float) -> None:
        """
        Record realized PnL for a closed trade. If the loss breaches
        PER_TRADE_MAX_LOSS, the responsible strategy is paused for
        STRATEGY_COOLDOWN seconds (subsequent BUY orders for that strategy are
        rejected by check_order until the cooldown elapses).
        """
        if not strategy:
            return
        pnl_dec = to_dec(pnl)
        if pnl_dec <= -to_dec(PER_TRADE_MAX_LOSS):
            until = time.monotonic() + STRATEGY_COOLDOWN
            self._strategy_cooldowns[strategy] = until
            log.warning(
                "[risk_manager] Strategy '%s' paused for %.0fs after per-trade loss $%.2f "
                "(threshold $%.2f)",
                strategy, STRATEGY_COOLDOWN, float(pnl_dec), float(PER_TRADE_MAX_LOSS),
            )
            await store.log_event(
                f"⏸ Strategy '{strategy}' paused {STRATEGY_COOLDOWN:.0f}s "
                f"(per-trade loss ${float(pnl_dec):.2f})"
            )
            try:
                from core.audit_logger import audit_logger
                await audit_logger.log_event(
                    category="risk",
                    event_type="strategy_cooldown_activated",
                    details=(
                        f"Strategy '{strategy}' paused {STRATEGY_COOLDOWN:.0f}s "
                        f"after per-trade loss ${float(pnl_dec):.2f}"
                    ),
                )
            except Exception:
                log.debug("[risk_manager] audit_logger unavailable while reporting strategy cooldown", exc_info=True)

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
            "dynamic_risk_multiplier": float(dynamic_model_risk_multiplier()),
            "effective_max_position_per_market": float(MAX_POSITION_PER_MARKET * dynamic_model_risk_multiplier()),
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
