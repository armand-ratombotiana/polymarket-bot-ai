"""
strategies/arb_temporal_expiry.py — Temporal Expiry Curve Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_temporal_expiry`` catalog entry.

Signal logic
------------
Same-underlying contracts with different expiry dates should trade at
related probabilities. The "term structure" of implied probabilities
typically exhibits smooth curvature (the far-dated contract carries
more uncertainty and therefore higher IV). When the curve develops a
kink — e.g. the near-dated contract trades ABOVE the far-dated one
for the same YES outcome (which is economically irrational for a
non-mean-reverting claim) — the strategy buys the cheaper contract
and sells the richer one, capturing the spread as the curve reverts
to its smooth shape.

Edge = |p_near - p_far| - fees, scaled by the curve-reversion
probability (high when the kink is statistically anomalous).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_SPREAD = 0.015                # 1.5% price spread required
TAKER_FEE_BPS = 1.0
MIN_LIQUIDITY_USDC = 30.0
MAX_DAYS_TO_EXPIRY = 365          # ignore contracts > 1y out
SCAN_INTERVAL = 30.0


class TemporalExpiryArb(BaseStrategy):
    """Term-structure expiry curve arbitrage."""

    name = "arb_temporal_expiry"

    def __init__(self) -> None:
        super().__init__()
        self.min_spread: float = MIN_SPREAD
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self.max_days_to_expiry: int = MAX_DAYS_TO_EXPIRY
        self._interval: float = SCAN_INTERVAL
        self._curve_history: dict[str, list[tuple[float, float]]] = {}

    async def _run(self) -> None:
        log.info(
            "[arb_temporal] Active (min_spread=%.2f%%)",
            self.min_spread * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_temporal] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Temporal expiry curve arbitrage — captures relative value "
                "across same-underlying contracts with differing expiry dates "
                "when the term structure develops a kink."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "temporal_expiry",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_spread", "taker_fee_bps", "min_liquidity_usdc"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "max_days_to_expiry" in config:
            self.max_days_to_expiry = int(config["max_days_to_expiry"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_spread < 0:
            return False, "min_spread must be >= 0"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.min_liquidity_usdc <= 0:
            return False, "min_liquidity_usdc must be > 0"
        if self.max_days_to_expiry < 1:
            return False, "max_days_to_expiry must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        underlying = market_context.get("underlying")
        contracts = market_context.get("contracts")
        if not underlying or not contracts or not isinstance(contracts, list):
            return None
        if len(contracts) < 2:
            return None

        # Filter & sort by days_to_expiry ascending.
        valid = [
            c for c in contracts
            if "token_id" in c and "price" in c and "days_to_expiry" in c
            and 0 < int(c["days_to_expiry"]) <= self.max_days_to_expiry
        ]
        if len(valid) < 2:
            return None
        valid.sort(key=lambda c: int(c["days_to_expiry"]))
        near, far = valid[0], valid[-1]

        try:
            p_near = float(near["price"])
            p_far = float(far["price"])
        except (TypeError, ValueError):
            return None

        liquidity = float(market_context.get("liquidity_usdc", 0.0))
        if liquidity < self.min_liquidity_usdc:
            return None

        # For a non-mean-reverting claim, far-dated should be ≥ near-dated
        # (more uncertainty ⇒ higher IV ⇒ higher price for the same YES).
        # If near > far by a meaningful spread, it's a curve kink → arb.
        spread = p_near - p_far
        fee_cost = 2.0 * self.taker_fee_bps / 10000.0
        net_edge = abs(spread) - fee_cost - self.min_spread
        if net_edge <= 0:
            return None

        # Track the curve history for diagnostics.
        self._curve_history.setdefault(underlying, []).append(
            (int(near["days_to_expiry"]), p_near)
        )
        if len(self._curve_history[underlying]) > 60:
            self._curve_history[underlying].pop(0)

        # Direction: if near > far, sell near, buy far (curve reverts to
        # the natural shape where far > near).
        if spread > 0:
            # Sell near, buy far.
            action = "BUY"
            primary_token = far["token_id"]
            primary_price = p_far
            counter_token = near["token_id"]
            counter_price = p_near
            counter_side = "SELL"
        else:
            # Buy near, sell far (inverted kink).
            action = "BUY"
            primary_token = near["token_id"]
            primary_price = p_near
            counter_token = far["token_id"]
            counter_price = p_far
            counter_side = "SELL"

        edge_pct = net_edge / max(p_near, p_far)
        confidence = min(0.85, 0.5 + abs(spread) * 5)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=primary_token,
            size=liquidity,
            price=primary_price,
            confidence=confidence,
            edge=edge_pct,
            reason=(
                f"Temporal arb: near(p={p_near:.3f}, d={near['days_to_expiry']}) "
                f"vs far(p={p_far:.3f}, d={far['days_to_expiry']}), "
                f"spread={spread:+.4f}, edge={edge_pct*100:.2f}%"
            ),
            metadata={
                "model": "temporal_expiry",
                "underlying": underlying,
                "near_contract": near,
                "far_contract": far,
                "spread": spread,
                "fee_cost": fee_cost,
                "edge_pct": edge_pct,
                "counter_leg": {
                    "token_id": counter_token,
                    "side": counter_side,
                    "price": counter_price,
                },
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_pct = float(risk_params.get("max_arb_pct", 0.15))
        per_arb_cap = float(risk_params.get("per_arb_cap_usdc", 75.0))
        return min(signal.size, max_pct * capital, per_arb_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no arb signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "temporal_expiry",
                "underlying": signal.metadata.get("underlying"),
                "counter_leg": signal.metadata.get("counter_leg"),
                "spread": signal.metadata.get("spread"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the curve kink reverts (spread compresses below
        the fee cost) OR when the near contract enters its expiry window."""
        if not position:
            return None
        current_spread = float(market_context.get("current_spread", 0.0))
        breakeven = self.taker_fee_bps / 10000.0
        if abs(current_spread) < breakeven:
            return {
                "reason": "curve kink reverted — close at market",
                "current_spread": current_spread,
                "breakeven": breakeven,
                "type": "market",
            }
        days_to_near_expiry = int(market_context.get("days_to_near_expiry", 30))
        if days_to_near_expiry <= 1:
            return {
                "reason": "near contract enters expiry window — flatten",
                "days_to_expiry": days_to_near_expiry,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_spread": self.min_spread,
            "taker_fee_bps": self.taker_fee_bps,
            "curve_history_underlyings": len(self._curve_history),
        })
        return base
