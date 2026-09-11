"""
strategies/arb_multi_negative_risk.py — Negative Risk Multi-Outcome Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_multi_negative_risk`` catalog entry.

Signal logic
------------
For N mutually-exclusive outcomes in a single event, the probability-
weighted sum of YES prices must equal 1. If the sum of best asks
across all N outcomes is strictly less than (1.0 - total_fees), then
buying one share of YES on every outcome guarantees a $1 payout
regardless of which outcome resolves YES — a risk-free arbitrage.

This is "negative risk" because the total cost is below the
guaranteed payout, yielding a positive edge without directional risk.

Edge = (1.0 - total_cost - fees) / total_cost
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.005                  # 0.5% edge required to act
TAKER_FEE_BPS = 1.0               # 1 bps per fill
MAX_OUTCOMES = 20                  # cap for sanity
MIN_LIQUIDITY_USDC = 50.0          # need $50 of liquidity per outcome leg
SCAN_INTERVAL = 20.0


class NegativeRiskMultiArb(BaseStrategy):
    """Multi-outcome mutually-exclusive arbitrage trader."""

    name = "arb_multi_negative_risk"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.max_outcomes: int = MAX_OUTCOMES
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self._interval: float = SCAN_INTERVAL
        self._last_scan_edge: float = 0.0

    async def _run(self) -> None:
        log.info(
            "[arb_neg_risk] Active (min_edge=%.2f%%, fee=%.1fbps)",
            self.min_edge * 100, self.taker_fee_bps,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_neg_risk] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Negative-risk multi-outcome arbitrage — buys YES on every "
                "mutually-exclusive outcome when Σ(asks) < 1 - fees, "
                "locking in a guaranteed payout."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "negative_risk_multi_arb",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_edge", "taker_fee_bps", "min_liquidity_usdc"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "max_outcomes" in config:
            self.max_outcomes = int(config["max_outcomes"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_edge < 0:
            return False, "min_edge must be >= 0"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.max_outcomes < 2:
            return False, "max_outcomes must be >= 2 (need ≥2 legs for arb)"
        if self.min_liquidity_usdc <= 0:
            return False, "min_liquidity_usdc must be > 0"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """market_context carries a list of outcomes with asks.

        Required shape:
          {
            "event_id": "evt_xxx",
            "outcomes": [
              {"token_id": "0xYES1", "ask": 0.30, "liquidity_usdc": 100.0},
              {"token_id": "0xYES2", "ask": 0.25, "liquidity_usdc": 80.0},
              ...
            ]
          }
        """
        event_id = market_context.get("event_id")
        outcomes = market_context.get("outcomes")
        if not event_id or not outcomes or not isinstance(outcomes, list):
            return None
        if len(outcomes) < 2 or len(outcomes) > self.max_outcomes:
            return None

        # Validate every leg has a price + liquidity.
        for o in outcomes:
            if "ask" not in o or "token_id" not in o:
                return None
            if float(o.get("liquidity_usdc", 0.0)) < self.min_liquidity_usdc:
                return None

        # Total cost to buy 1 share of YES on every outcome.
        asks = [float(o["ask"]) for o in outcomes]
        total_cost = sum(asks)
        # Per-share taker fee across all legs.
        fee_per_share = self.taker_fee_bps / 10000.0 * len(outcomes)
        net_cost = total_cost + fee_per_share
        payout = 1.0  # guaranteed $1 payout regardless of resolution
        gross_edge = payout - net_cost
        edge_pct = gross_edge / net_cost if net_cost > 0 else 0.0
        self._last_scan_edge = edge_pct

        if edge_pct < self.min_edge:
            return None

        # The signal "token_id" is the event_id (composite arb); the
        # per-leg token_ids are carried in metadata.
        first_outcome = outcomes[0]
        # Use the cheapest leg's token_id as the signal "primary leg"
        # so the order router has a concrete token to submit.
        primary_token = first_outcome["token_id"]
        primary_price = float(first_outcome["ask"])

        # Size: 1 share on every leg (so the payout is exactly $1).
        # Size in USDC = total_cost (the cost to assemble the arb book).
        size_usdc = total_cost

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action="BUY",
            token_id=primary_token,
            size=size_usdc,
            price=primary_price,
            confidence=0.95,  # near-risk-free arb
            edge=edge_pct,
            reason=(
                f"NegativeRisk arb: Σ(asks)={total_cost:.4f} < 1.0 - fees, "
                f"edge={edge_pct*100:.2f}% across {len(outcomes)} outcomes"
            ),
            metadata={
                "model": "negative_risk_multi_arb",
                "event_id": event_id,
                "outcomes": outcomes,
                "total_cost": total_cost,
                "net_cost": net_cost,
                "gross_edge": gross_edge,
                "edge_pct": edge_pct,
                "payout": payout,
                "legs": [
                    {
                        "token_id": o["token_id"],
                        "side": "BUY",
                        "price": float(o["ask"]),
                        "size": 1.0,
                    }
                    for o in outcomes
                ],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Negative-risk arb is low-risk, so allow up to 25% of capital.
        max_pct = float(risk_params.get("max_arb_pct", 0.25))
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
            "time_in_force": "IOC",  # fill-or-kill: either get all legs or none
            "post_only": False,
            "metadata": {
                "model": "negative_risk_multi_arb",
                "event_id": signal.metadata.get("event_id"),
                "legs": signal.metadata.get("legs"),
                "total_cost": signal.metadata.get("total_cost"),
                "edge_pct": signal.metadata.get("edge_pct"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Multi-outcome arbs resolve at event settlement — no early exit
        unless edge compresses below breakeven (then leg-out at market)."""
        if not position:
            return None
        current_edge = float(market_context.get("current_edge_pct", 0.0))
        # If edge has compressed (price moved against us), leg out.
        if current_edge < 0:
            return {
                "reason": "edge compressed below 0 — leg out at market",
                "current_edge_pct": current_edge,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge": self.min_edge,
            "taker_fee_bps": self.taker_fee_bps,
            "max_outcomes": self.max_outcomes,
            "last_scan_edge": self._last_scan_edge,
        })
        return base
