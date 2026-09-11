"""
strategies/arb_synthetic_straddle.py — Synthetic Straddle Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_synthetic_straddle`` catalog entry.

Signal logic
------------
A binary event has two outcome tokens (YES and NO) which both settle
to either $1 or $0 — and YES + NO must always equal $1 (since exactly
one outcome resolves YES). The synthetic straddle arb:

  * Buy 1 YES + 1 NO when (ask_YES + ask_NO) < 1 - fees
  * Guaranteed $1 payout regardless of resolution

This is the "long straddle" of the binary options world — except here
the straddle is *synthetic* because the two legs are economically
complementary (one MUST resolve YES). The "edge" is the discount of
the combined ask below the risk-free $1 payout.

Implied volatility dimension
----------------------------
In liquid markets, the combined YES+NO ask equals exactly 1.0 (or
slightly above to cover fees). When implied volatility is mispriced
—one leg's IV is too low relative to the other — the combined ask
dips below 1.0, creating the arb.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.005                  # 0.5% combined edge required
TAKER_FEE_BPS = 1.0               # 1 bps per fill × 2 legs = 2 bps round-trip
MIN_LIQUIDITY_USDC = 25.0
SCAN_INTERVAL = 15.0


class SyntheticStraddleArb(BaseStrategy):
    """Long YES + long NO synthetic straddle arbitrage."""

    name = "arb_synthetic_straddle"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self._interval: float = SCAN_INTERVAL
        self._iv_history: dict[str, list[float]] = {}

    async def _run(self) -> None:
        log.info(
            "[arb_straddle] Active (min_edge=%.2f%%, fee=%.1fbps)",
            self.min_edge * 100, self.taker_fee_bps,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_straddle] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Synthetic straddle arbitrage — buys YES + NO on the same "
                "binary event when the combined ask < 1 - fees, exploiting "
                "implied volatility mispricing on paired outcomes."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "synthetic_straddle",
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
        yes_token = market_context.get("yes_token_id")
        no_token = market_context.get("no_token_id")
        yes_ask = market_context.get("yes_ask")
        no_ask = market_context.get("no_ask")
        if not yes_token or not no_token or yes_ask is None or no_ask is None:
            return None

        try:
            ya = float(yes_ask)
            na = float(no_ask)
        except (TypeError, ValueError):
            return None

        if ya <= 0 or na <= 0 or ya >= 1 or na >= 1:
            return None

        liquidity = float(market_context.get("liquidity_usdc", 0.0))
        if liquidity < self.min_liquidity_usdc:
            return None

        # Round-trip cost: 2 fills (YES + NO), each paying taker_fee_bps.
        fee_per_share = 2.0 * self.taker_fee_bps / 10000.0
        total_cost = ya + na + fee_per_share
        payout = 1.0  # one leg pays $1, the other pays $0 — guaranteed $1 sum
        gross_edge = payout - total_cost
        edge_pct = gross_edge / total_cost if total_cost > 0 else 0.0

        # Implied vol mispricing: log the IV spread (heuristic — in a
        # fully-priced market, ya + na == 1.0 exactly).
        iv_mispricing = 1.0 - (ya + na)
        self._iv_history.setdefault(yes_token, []).append(iv_mispricing)
        if len(self._iv_history[yes_token]) > 30:
            self._iv_history[yes_token].pop(0)

        if edge_pct < self.min_edge:
            return None

        confidence = min(0.95, 0.6 + abs(iv_mispricing) * 20)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action="BUY",
            token_id=yes_token,
            size=liquidity,
            price=ya,
            confidence=confidence,
            edge=edge_pct,
            reason=(
                f"Straddle arb: ask(YES)+ask(NO)={ya+na:.4f} < 1 - fees, "
                f"iv_mispricing={iv_mispricing:+.4f}, edge={edge_pct*100:.2f}%"
            ),
            metadata={
                "model": "synthetic_straddle",
                "yes_token_id": yes_token,
                "no_token_id": no_token,
                "yes_ask": ya,
                "no_ask": na,
                "total_cost": total_cost,
                "gross_edge": gross_edge,
                "edge_pct": edge_pct,
                "iv_mispricing": iv_mispricing,
                "legs": [
                    {"token_id": yes_token, "side": "BUY", "price": ya, "size": 1.0},
                    {"token_id": no_token, "side": "BUY", "price": na, "size": 1.0},
                ],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_pct = float(risk_params.get("max_arb_pct", 0.20))
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
            "time_in_force": "IOC",
            "post_only": False,
            "metadata": {
                "model": "synthetic_straddle",
                "legs": signal.metadata.get("legs"),
                "total_cost": signal.metadata.get("total_cost"),
                "iv_mispricing": signal.metadata.get("iv_mispricing"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Straddle arbs resolve at event expiry. Exit early only if the
        combined ask reverts to 1.0+ (no edge left to capture)."""
        if not position:
            return None
        current_combined_ask = float(market_context.get("combined_ask", 1.0))
        breakeven = 1.0 - self.taker_fee_bps / 10000.0
        if current_combined_ask >= breakeven:
            return {
                "reason": "straddle spread reverted — unwind at market",
                "current_combined_ask": current_combined_ask,
                "breakeven": breakeven,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge": self.min_edge,
            "taker_fee_bps": self.taker_fee_bps,
            "iv_history_tokens": len(self._iv_history),
        })
        return base
