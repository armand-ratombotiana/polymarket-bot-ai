"""
strategies/event_oracle_dispute.py — Oracle Dispute Sniper.

W45-1 — implements the unified strategy contract for the
``event_oracle_dispute`` catalog entry.

Signal logic
------------
UMA (Universal Market Access) optimistic oracle disputes provide
high-conviction trading opportunities. When a UMA proposal is made:
  * If the proposal is likely WRONG (per independent verification),
    the strategy positions for the dispute bond to pay out.
  * If the proposal is likely RIGHT but the market is mispricing the
    outcome, the strategy trades the market dislocation.

Inputs (via ``market_context``):
  * ``proposal_value`` — the proposed UMA resolution value
  * ``true_value`` — the strategy's independent verification
  * ``liveness_period_hours`` — time remaining before proposal finalizes
  * ``dispute_bond_usdc`` — bond required to dispute
  * ``market_price`` — current CLOB price for the disputed market

Edge = expected dispute payout × dispute_success_probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_DISPUTE_EDGE = 0.05          # 5% dislocation required
MIN_LIVENESS_HOURS = 1.0         # need ≥1 hour before finalization
MAX_LIVENESS_HOURS = 168.0       # ignore if > 7 days out
MAX_DISPUTE_BOND_USDC = 1000.0
SCAN_INTERVAL = 60.0              # 1-minute polling


class OracleDisputeSniper(BaseStrategy):
    """UMA optimistic oracle dispute sniper."""

    name = "event_oracle_dispute"

    def __init__(self) -> None:
        super().__init__()
        self.min_dispute_edge: float = MIN_DISPUTE_EDGE
        self.min_liveness_hours: float = MIN_LIVENESS_HOURS
        self.max_liveness_hours: float = MAX_LIVENESS_HOURS
        self.max_dispute_bond_usdc: float = MAX_DISPUTE_BOND_USDC
        self._interval: float = SCAN_INTERVAL
        self._active_disputes: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[event_oracle] Active (min_edge=%.0f%%, liveness=[%.0f-%.0fh])",
            self.min_dispute_edge * 100, self.min_liveness_hours, self.max_liveness_hours,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_oracle] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "UMA oracle dispute sniper — positions ahead of resolution "
                "disputes and bond challenges when independent verification "
                "disagrees with the proposed oracle value."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "oracle_dispute_sniper",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_dispute_edge", "min_liveness_hours",
                  "max_liveness_hours", "max_dispute_bond_usdc"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_dispute_edge < 0:
            return False, "min_dispute_edge must be >= 0"
        if self.min_liveness_hours <= 0:
            return False, "min_liveness_hours must be > 0"
        if self.max_liveness_hours <= self.min_liveness_hours:
            return False, "max_liveness_hours must be > min_liveness_hours"
        if self.max_dispute_bond_usdc <= 0:
            return False, "max_dispute_bond_usdc must be > 0"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        proposal_value = market_context.get("proposal_value")
        true_value = market_context.get("true_value")
        liveness_hours = market_context.get("liveness_period_hours")
        dispute_bond = market_context.get("dispute_bond_usdc")
        market_price = market_context.get("market_price")
        if not token_id or proposal_value is None or true_value is None:
            return None
        if liveness_hours is None or dispute_bond is None or market_price is None:
            return None

        try:
            pv = float(proposal_value)
            tv = float(true_value)
            lh = float(liveness_hours)
            db = float(dispute_bond)
            mp = float(market_price)
        except (TypeError, ValueError):
            return None

        # Liveness gate.
        if not self.min_liveness_hours <= lh <= self.max_liveness_hours:
            return None

        # Bond size gate.
        if db > self.max_dispute_bond_usdc:
            return None  # bond too large to dispute safely

        # Discrepancy check: does the proposal disagree with our truth?
        # Handle both boolean outcomes (0/1) and continuous values.
        discrepancy = abs(pv - tv)
        # Normalize to probability space (max discrepancy = 1.0).
        discrepancy_pct = discrepancy if 0 <= discrepancy <= 1.0 else 0.0

        if discrepancy_pct < self.min_dispute_edge:
            return None

        # The market is pricing the proposal value (since UMA is optimistic
        # oracle — proposal defaults to truth until disputed). When we
        # believe the proposal is WRONG, we trade AGAINST the market price.
        if tv > pv:
            # True value higher than proposal → BUY (market underpriced).
            action = "BUY"
            target_price = round(min(mp + 0.02, 0.98), 4)
            reason = (
                f"OracleDispute BUY: proposal={pv:.3f} < true={tv:.3f} "
                f"(disc={discrepancy_pct*100:.2f}%, liveness={lh:.1f}h)"
            )
        else:
            action = "SELL"
            target_price = round(max(mp - 0.02, 0.02), 4)
            reason = (
                f"OracleDispute SELL: proposal={pv:.3f} > true={tv:.3f} "
                f"(disc={discrepancy_pct*100:.2f}%, liveness={lh:.1f}h)"
            )

        # Edge = expected dispute payout × success probability (heuristic).
        success_prob = min(0.8, discrepancy_pct * 5)
        edge = discrepancy_pct * success_prob * 0.5
        confidence = success_prob

        self._active_disputes[token_id] = {
            "proposal_value": pv,
            "true_value": tv,
            "liveness_hours": lh,
            "dispute_bond": db,
        }
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=db,  # size = dispute bond (we'd post this much to dispute)
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=reason,
            metadata={
                "model": "oracle_dispute_sniper",
                "proposal_value": pv,
                "true_value": tv,
                "discrepancy_pct": discrepancy_pct,
                "liveness_hours": lh,
                "dispute_bond_usdc": db,
                "market_price": mp,
                "success_probability": success_prob,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Cap by max bond × success probability.
        bond = float(signal.metadata.get("dispute_bond_usdc", 0.0))
        success_prob = float(signal.metadata.get("success_probability", 0.5))
        max_pct = float(risk_params.get("max_dispute_pct", 0.10))
        return min(bond * success_prob, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no dispute signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "oracle_dispute_sniper",
                "proposal_value": signal.metadata.get("proposal_value"),
                "true_value": signal.metadata.get("true_value"),
                "dispute_bond_usdc": signal.metadata.get("dispute_bond_usdc"),
                "liveness_hours": signal.metadata.get("liveness_hours"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when liveness expires (dispute window closes) OR when the
        proposal is updated to match the true value (dispute moot)."""
        if not position:
            return None
        current_liveness = float(market_context.get("current_liveness_hours", 0.0))
        if current_liveness <= 0:
            return {
                "reason": "liveness expired — dispute window closed",
                "current_liveness_hours": current_liveness,
                "type": "market",
            }
        # Proposal corrected to match true value — exit (no edge left).
        current_proposal = float(market_context.get("current_proposal_value", 0.0))
        true_value = float(position.get("true_value", 0.0))
        if abs(current_proposal - true_value) < 0.01:
            return {
                "reason": "proposal corrected to true value — exit",
                "current_proposal": current_proposal,
                "true_value": true_value,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_dispute_edge": self.min_dispute_edge,
            "min_liveness_hours": self.min_liveness_hours,
            "max_liveness_hours": self.max_liveness_hours,
            "active_disputes": len(self._active_disputes),
        })
        return base
