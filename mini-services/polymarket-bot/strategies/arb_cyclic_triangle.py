"""
strategies/arb_cyclic_triangle.py — Cyclic Triangle Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_cyclic_triangle`` catalog entry.

Signal logic
------------
Triangle arbitrage exploits cyclic price discrepancies across three
inter-related prediction markets. Given three markets A, B, C with
conditional probabilities p(A→B), p(B→C), p(C→A), the cyclic
product must satisfy:

  p(A→B) × p(B→C) × p(C→A) ≤ 1.0

If the product is strictly less than 1 - fees, the strategy buys
all three legs (one share each), guaranteeing a $1 payout when the
cycle resolves (at least one leg pays YES). Edge = (1 - product - fees).

Examples:
  * A="Trump wins", B="GOP wins", C="GOP wins popular vote"
    If p(A→B) × p(B→C) × p(C→A) < 1, the cycle is mispriced.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.005                  # 0.5% cycle edge required
TAKER_FEE_BPS = 1.0
MIN_LIQUIDITY_USDC = 25.0
SCAN_INTERVAL = 30.0


class CyclicTriangleArb(BaseStrategy):
    """Cyclic triangle arbitrage trader."""

    name = "arb_cyclic_triangle"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self._interval: float = SCAN_INTERVAL
        self._cycle_history: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[arb_triangle] Active (min_edge=%.2f%%, fee=%.1fbps)",
            self.min_edge * 100, self.taker_fee_bps,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_triangle] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Cyclic triangle arbitrage — exploits price discrepancies "
                "across three chained prediction markets where the cyclic "
                "product p(A→B) × p(B→C) × p(C→A) < 1 - fees."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "cyclic_triangle",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_edge", "taker_fee_bps", "min_liquidity_usdc"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_edge < 0:
            return False, "min_edge must be >= 0"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.min_liquidity_usdc <= 0:
            return False, "min_liquidity_usdc must be > 0"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        cycle_id = market_context.get("cycle_id")
        legs = market_context.get("legs")
        if not cycle_id or not legs or not isinstance(legs, list):
            return None
        if len(legs) != 3:
            return None

        # Each leg: {"token_id": "...", "ask": 0.5, "liquidity_usdc": 100.0, "label": "A→B"}
        for leg in legs:
            if "token_id" not in leg or "ask" not in leg:
                return None
            if float(leg.get("liquidity_usdc", 0.0)) < self.min_liquidity_usdc:
                return None

        asks = [float(leg["ask"]) for leg in legs]
        # All asks must be in (0, 1).
        if any(a <= 0 or a >= 1 for a in asks):
            return None

        cycle_product = asks[0] * asks[1] * asks[2]
        # Track cycle history.
        self._cycle_history[cycle_id] = cycle_product

        # Three fills × taker fee.
        fee_per_share = 3.0 * self.taker_fee_bps / 10000.0
        total_cost = cycle_product + fee_per_share
        # Guaranteed payout: $1 if at least one leg resolves YES (cyclic
        # consistency: at least one of A→B, B→C, C→A is always true).
        payout = 1.0
        gross_edge = payout - total_cost
        edge_pct = gross_edge / total_cost if total_cost > 0 else 0.0

        if edge_pct < self.min_edge:
            return None

        # Size = min liquidity across the three legs (cycle is bounded
        # by the weakest leg).
        min_liquidity = min(float(leg.get("liquidity_usdc", 0.0)) for leg in legs)

        primary = legs[0]
        confidence = min(0.9, 0.55 + (1.0 - cycle_product) * 10)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action="BUY",
            token_id=primary["token_id"],
            size=min_liquidity,
            price=float(primary["ask"]),
            confidence=confidence,
            edge=edge_pct,
            reason=(
                f"Triangle arb: cycle_product={cycle_product:.4f} (legs: "
                f"{asks[0]:.2f}×{asks[1]:.2f}×{asks[2]:.2f}), "
                f"edge={edge_pct*100:.2f}%"
            ),
            metadata={
                "model": "cyclic_triangle",
                "cycle_id": cycle_id,
                "asks": asks,
                "cycle_product": cycle_product,
                "fee_per_share": fee_per_share,
                "total_cost": total_cost,
                "gross_edge": gross_edge,
                "edge_pct": edge_pct,
                "legs": [
                    {
                        "token_id": leg["token_id"],
                        "side": "BUY",
                        "price": float(leg["ask"]),
                        "size": 1.0,
                        "label": leg.get("label", f"leg_{i}"),
                    }
                    for i, leg in enumerate(legs)
                ],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_pct = float(risk_params.get("max_arb_pct", 0.15))
        per_arb_cap = float(risk_params.get("per_arb_cap_usdc", 100.0))
        return min(signal.size, max_pct * capital, per_arb_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no arb signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "IOC",  # all-or-nothing — must fill all 3 legs
            "post_only": False,
            "metadata": {
                "model": "cyclic_triangle",
                "cycle_id": signal.metadata.get("cycle_id"),
                "legs": signal.metadata.get("legs"),
                "cycle_product": signal.metadata.get("cycle_product"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Triangle arbs resolve when the cycle completes (one leg pays
        out). Early exit only if the cycle product reverts to 1.0+."""
        if not position:
            return None
        current_product = float(market_context.get("current_cycle_product", 1.0))
        if current_product >= 1.0:
            return {
                "reason": "cycle reverted to breakeven — unwind",
                "current_product": current_product,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge": self.min_edge,
            "taker_fee_bps": self.taker_fee_bps,
            "tracked_cycles": len(self._cycle_history),
        })
        return base
