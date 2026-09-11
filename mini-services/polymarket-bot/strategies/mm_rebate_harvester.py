"""
strategies/mm_rebate_harvester.py — Rebate Harvester Market Maker.

W45-1 — implements the unified strategy contract for the
``mm_rebate_harvester`` catalog entry.

Signal logic
------------
Top-of-book queue-priority market maker: posts passive limit orders
at the best bid/ask to capture maker fee rebates. Profits come from:

  * maker fee rebate per fill (positive when exchange rebates liquidity)
  * spread capture: each round-trip nets the half-spread minus fees

The harvester only quotes when:
  * the queue position is favorable (low queue_ahead count)
  * the spread exceeds the breakeven threshold (rebate > taker_fee × 2)
  * inventory is within tolerance

Edge = rebate - (effective_spread_cost / 2) - adverse_selection_cost
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MAKER_REBATE_BPS = 2.0          # 2 bps rebate per filled maker order
TAKER_FEE_BPS = 5.0             # 5 bps taker fee (round-trip exit)
MIN_QUEUE_AHEAD = 5             # don't quote if > 5 orders ahead in queue
BREAKEVEN_SPREAD_BPS = 6.0      # need >6 bps spread for rebate to pay
MAX_INVENTORY_SHARES = 300
SCAN_INTERVAL = 5.0


class RebateHarvester(BaseStrategy):
    """Top-of-book queue-priority rebate harvester."""

    name = "mm_rebate_harvester"

    def __init__(self) -> None:
        super().__init__()
        self.maker_rebate_bps: float = MAKER_REBATE_BPS
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_queue_ahead: int = MIN_QUEUE_AHEAD
        self.breakeven_spread_bps: float = BREAKEVEN_SPREAD_BPS
        self.max_inventory_shares: float = MAX_INVENTORY_SHARES
        self._interval: float = SCAN_INTERVAL
        self._queued_tokens: set[str] = set()

    async def _run(self) -> None:
        log.info(
            "[mm_rebate] Active (rebate=%.1fbps, taker_fee=%.1fbps, BE=%dbps)",
            self.maker_rebate_bps, self.taker_fee_bps, int(self.breakeven_spread_bps),
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mm_rebate] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Top-of-book rebate harvester — posts passive limit "
                "orders at best bid/ask to maximize maker fee rebates "
                "with queue priority."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "rebate_harvester",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("maker_rebate_bps", "taker_fee_bps", "breakeven_spread_bps",
                  "max_inventory_shares"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "min_queue_ahead" in config:
            self.min_queue_ahead = int(config["min_queue_ahead"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.maker_rebate_bps < 0:
            return False, "maker_rebate_bps must be >= 0"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.breakeven_spread_bps <= 0:
            return False, "breakeven_spread_bps must be > 0"
        if self.min_queue_ahead < 0:
            return False, "min_queue_ahead must be >= 0"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        best_bid = market_context.get("best_bid")
        best_ask = market_context.get("best_ask")
        queue_ahead_bid = market_context.get("queue_ahead_bid", 0)
        queue_ahead_ask = market_context.get("queue_ahead_ask", 0)
        if not token_id or mid is None or best_bid is None or best_ask is None:
            return None

        try:
            mid_f = float(mid)
            bb = float(best_bid)
            ba = float(best_ask)
        except (TypeError, ValueError):
            return None

        spread_bps = (ba - bb) / mid_f * 10000.0 if mid_f > 0 else 0.0
        if spread_bps < self.breakeven_spread_bps:
            return None  # spread too tight to overcome taker fee

        inventory = float(market_context.get("inventory", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return None  # inventory at cap

        # Decide which side to quote — the one with lower queue ahead.
        side_ahead = min(queue_ahead_bid, queue_ahead_ask)
        if side_ahead > self.min_queue_ahead:
            return None  # queue unfavorable

        if queue_ahead_bid <= queue_ahead_ask:
            action = "BUY"
            price = round(bb + 0.001, 4)  # improve the bid by 1 tick
        else:
            action = "SELL"
            price = round(ba - 0.001, 4)

        # Edge per round-trip (per side): rebate + half_spread - taker_fee
        edge_bps = (
            self.maker_rebate_bps
            + spread_bps / 2.0
            - self.taker_fee_bps / 2.0
        ) / 10000.0
        edge = max(0.0, edge_bps)
        if edge <= 0:
            return None

        confidence = 0.7 if side_ahead == 0 else 0.4
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._queued_tokens.add(token_id)
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"Rebate {action}: spread={spread_bps:.1f}bps, "
                f"queue_ahead={side_ahead}, edge={edge_bps:.1f}bps"
            ),
            metadata={
                "model": "rebate_harvester",
                "spread_bps": spread_bps,
                "maker_rebate_bps": self.maker_rebate_bps,
                "taker_fee_bps": self.taker_fee_bps,
                "queue_ahead": side_ahead,
                "best_bid": bb,
                "best_ask": ba,
                "inventory": inventory,
                "edge_bps": edge_bps,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 3.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        # Larger size when queue position is best (queue_ahead == 0).
        queue_ahead = float(signal.metadata.get("queue_ahead", 10))
        size_factor = 1.5 if queue_ahead == 0 else 1.0
        return min(quote_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no actionable signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": True,  # never cross — must be maker
            "metadata": {
                "model": "rebate_harvester",
                "queue_ahead": signal.metadata.get("queue_ahead"),
                "spread_bps": signal.metadata.get("spread_bps"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        if not position:
            return None
        inventory = float(position.get("inventory_shares", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return {
                "reason": "inventory at cap — flatten via taker order",
                "inventory": inventory,
                "type": "market",
                "fee_bps": self.taker_fee_bps,
            }
        # Queue degraded — cancel and re-queue.
        new_queue_ahead = int(market_context.get("queue_ahead", 0))
        if new_queue_ahead > self.min_queue_ahead * 2:
            return {
                "reason": "queue position degraded — re-queue",
                "new_queue_ahead": new_queue_ahead,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "maker_rebate_bps": self.maker_rebate_bps,
            "taker_fee_bps": self.taker_fee_bps,
            "breakeven_spread_bps": self.breakeven_spread_bps,
            "queued_tokens": len(self._queued_tokens),
        })
        return base
