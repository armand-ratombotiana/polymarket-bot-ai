"""
strategies/arb_gamma_clob_parity.py — Gamma-CLOB Parity Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_gamma_clob_parity`` catalog entry.

Signal logic
------------
Polymarket operates two execution venues for the same outcome tokens:
the Gamma AMM (automated market maker with a bonding curve) and the
CLOB (central-limit order book). When the Gamma AMM price diverges
from the CLOB best quote by more than the round-trip fee + slippage,
the strategy buys on the cheaper venue and sells on the more expensive
venue — locking in the price differential as edge.

Edge = (clob_price - gamma_price - fees) / gamma_price (when gamma < clob)
Edge = (gamma_price - clob_price - fees) / clob_price  (when clob < gamma)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.008                  # 0.8% edge required to overcome fees
TAKER_FEE_BPS = 1.0               # 1 bps per fill (per venue)
MIN_LIQUIDITY_USDC = 30.0
MAX_SLIPPAGE_BPS = 50             # don't act if expected slippage > 50 bps
SCAN_INTERVAL = 15.0


class GammaClobParityArb(BaseStrategy):
    """Cross-venue Gamma AMM vs CLOB parity arbitrage."""

    name = "arb_gamma_clob_parity"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self.max_slippage_bps: float = MAX_SLIPPAGE_BPS
        self._interval: float = SCAN_INTERVAL
        self._last_divergence: float = 0.0

    async def _run(self) -> None:
        log.info(
            "[arb_gamma_clob] Active (min_edge=%.2f%%)",
            self.min_edge * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_gamma_clob] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Cross-venue parity arbitrage — exploits pricing "
                "dislocations between Polymarket's Gamma AMM and the "
                "CLOB order book for the same outcome token."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "gamma_clob_parity",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_edge", "taker_fee_bps", "min_liquidity_usdc",
                  "max_slippage_bps"):
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
        if self.max_slippage_bps < 0:
            return False, "max_slippage_bps must be >= 0"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        gamma_price = market_context.get("gamma_price")
        clob_bid = market_context.get("clob_bid")
        clob_ask = market_context.get("clob_ask")
        if not token_id or gamma_price is None or clob_bid is None or clob_ask is None:
            return None

        try:
            gp = float(gamma_price)
            cb = float(clob_bid)
            ca = float(clob_ask)
        except (TypeError, ValueError):
            return None

        liquidity = float(market_context.get("liquidity_usdc", 0.0))
        if liquidity < self.min_liquidity_usdc:
            return None

        clob_mid = (cb + ca) / 2.0
        divergence = gp - clob_mid
        self._last_divergence = divergence

        # Round-trip cost = taker fee × 2 (one per venue) + slippage.
        fee_per_share = 2.0 * self.taker_fee_bps / 10000.0
        slippage = self.max_slippage_bps / 10000.0
        total_cost = fee_per_share + slippage

        # Direction: if Gamma < CLOB (divergence < 0), buy Gamma, sell CLOB.
        # If Gamma > CLOB (divergence > 0), buy CLOB, sell Gamma.
        if abs(divergence) <= total_cost + self.min_edge:
            return None

        if divergence < 0:
            # Buy on Gamma, sell on CLOB.
            action = "BUY"
            buy_price = gp
            sell_price = cb  # hit the CLOB bid
            edge = (sell_price - buy_price - total_cost) / buy_price
        else:
            # Buy on CLOB, sell on Gamma.
            action = "BUY"
            buy_price = ca  # take the CLOB ask
            sell_price = gp
            edge = (sell_price - buy_price - total_cost) / buy_price

        if edge < self.min_edge:
            return None

        confidence = min(0.9, 0.5 + abs(divergence) * 10)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=liquidity,
            price=buy_price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"GammaCLOB arb: γ={gp:.4f} vs CLOB[{cb:.4f}/{ca:.4f}], "
                f"div={divergence:+.4f}, edge={edge*100:.2f}%"
            ),
            metadata={
                "model": "gamma_clob_parity",
                "gamma_price": gp,
                "clob_bid": cb,
                "clob_ask": ca,
                "clob_mid": clob_mid,
                "divergence": divergence,
                "buy_price": buy_price,
                "sell_price": sell_price,
                "buy_venue": "gamma" if divergence < 0 else "clob",
                "sell_venue": "clob" if divergence < 0 else "gamma",
                "total_cost": total_cost,
                "liquidity_usdc": liquidity,
                "counter_leg": {
                    "token_id": token_id,
                    "side": "SELL",
                    "price": sell_price,
                    "venue": "clob" if divergence < 0 else "gamma",
                },
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_pct = float(risk_params.get("max_arb_pct", 0.15))
        per_arb_cap = float(risk_params.get("per_arb_cap_usdc", 50.0))
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
                "model": "gamma_clob_parity",
                "buy_venue": signal.metadata.get("buy_venue"),
                "sell_venue": signal.metadata.get("sell_venue"),
                "counter_leg": signal.metadata.get("counter_leg"),
                "divergence": signal.metadata.get("divergence"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Cross-venue arbs close immediately on both legs — no overnight
        exposure. Exit signal fires only when divergence compresses below
        breakeven before both legs are filled."""
        if not position:
            return None
        current_div = float(market_context.get("divergence", 0.0))
        if abs(current_div) < self.taker_fee_bps / 10000.0:
            return {
                "reason": "divergence compressed — unwind at market",
                "current_divergence": current_div,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge": self.min_edge,
            "taker_fee_bps": self.taker_fee_bps,
            "max_slippage_bps": self.max_slippage_bps,
            "last_divergence": self._last_divergence,
        })
        return base
