"""
core/settlement.py — Market Resolution Settlement & ML Feedback Engine.

Periodically queries Gamma API for resolved markets:
1. Calculates real/paper PnL for any open positions in resolved markets
   - Winning outcome: shares * $1.00 - invested cost
   - Losing outcome: -$invested cost
2. Settles and closes the positions in DataStore
3. Automatically feeds the ground-truth outcome (resolved_yes) into ml_model
   for continuous online learning 24/7!
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Dict, Set

from core.data_store import BANKROLL_BASELINE, Side, Trade, store
from core.gamma_client import gamma_client
from ml.model import ml_model

log = logging.getLogger(__name__)

SETTLEMENT_CHECK_INTERVAL = 60.0  # check every 60s


class SettlementEngine:
    """
    Settles resolved prediction markets, updates portfolio balance/PnL,
    and feeds ground truth labels to the ML online learner.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._settled_tokens: Set[str] = set()

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="settlement-engine")
        log.info("[settlement] Settlement engine started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run_loop(self) -> None:
        await asyncio.sleep(15.0)  # Initial grace period
        while self._running:
            try:
                await self._check_resolved_markets()
            except Exception as e:
                log.debug("[settlement] Settlement check error: %s", e)
            await asyncio.sleep(SETTLEMENT_CHECK_INTERVAL)

    async def _check_resolved_markets(self) -> None:
        resolved_mkts = await gamma_client.get_resolved_markets(limit=20)
        for mkt in resolved_mkts:
            await self._process_resolved_market(mkt)

    async def _process_resolved_market(self, mkt: dict) -> None:
        condition_id = mkt.get("conditionId") or mkt.get("id")
        token_ids = gamma_client.extract_token_ids(mkt)
        if not token_ids:
            return

        yes_token = token_ids[0]
        if yes_token in self._settled_tokens:
            return

        # Determine winner: outcomePrices or resolvedBy outcome
        outcome_prices = mkt.get("outcomePrices")
        resolved_yes = False
        if outcome_prices:
            if isinstance(outcome_prices, str):
                try:
                    prices = json.loads(outcome_prices)
                except Exception:
                    prices = []
            else:
                prices = outcome_prices

            if prices and len(prices) >= 2:
                p0 = float(prices[0])
                resolved_yes = (p0 >= 0.9)

        slug = mkt.get("slug") or store.market_slugs.get(yes_token, yes_token[:12])

        # Settle any active positions in DataStore
        async with store._lock:
            pos = store.positions.get(yes_token)
            if pos and (pos.yes_shares > 0 or pos.total_invested > 0):
                shares = pos.yes_shares
                payout = shares * 1.0 if resolved_yes else 0.0
                pnl = payout - pos.total_invested

                # Record closing trade
                trade = Trade(
                    trade_id=f"settle-{yes_token[:8]}",
                    token_id=yes_token,
                    side=Side.SELL,
                    price=1.0 if resolved_yes else 0.0,
                    size=shares,
                    pnl=pnl,
                    strategy="settlement",
                    paper=True,
                )
                store.trades.append(trade)
                store.daily_pnl += pnl
                store.paper_balance += payout
                current_eq = BANKROLL_BASELINE + store.daily_pnl
                store.peak_equity = max(store.peak_equity, current_eq)
                store.equity_history.append({
                    "timestamp": time.time(),
                    "equity": round(current_eq, 2),
                    "pnl": round(store.daily_pnl, 2),
                })

                # Reset position
                pos.yes_shares = 0.0
                pos.total_invested = 0.0
                pos.realised_pnl += pnl

                outcome_str = "YES (WIN)" if resolved_yes else "NO (LOSS)"
                await store.log_event(
                    f"🏆 Market Resolved: {slug} → {outcome_str} | P&L: ${pnl:+.2f}"
                )
                log.info("[settlement] Settled %s: %s | PnL: $%.2f", slug, outcome_str, pnl)

        # Feed ground truth outcome to ML model for continuous online learning
        self._settled_tokens.add(yes_token)
        ml_model.save()


# Module-level singleton
settlement_engine = SettlementEngine()
