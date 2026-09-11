"""
strategies/ml_bayesian_belief.py — Bayesian Belief Updater Trader.

W45-1 — implements the unified strategy contract for the
``ml_bayesian_belief`` catalog entry.

Signal logic
------------
Bayesian posterior probability updates based on new evidence arrival.
The strategy maintains a Beta prior on the YES probability:

  prior: Beta(α, β)
  evidence: each new "yes-leaning" piece of evidence → α += 1
            each new "no-leaning" piece of evidence → β += 1

Posterior P(YES) = α / (α + β)
Confidence = posterior concentration (α + β) — higher = more confident.

Trading rules:
  * P(YES) > 0.65 AND concentration > 10 → BUY
  * P(YES) < 0.35 AND concentration > 10 → SELL
  * Otherwise → no signal (insufficient conviction)

Edge = |P(YES) - 0.5| × concentration_weight.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

PRIOR_ALPHA = 1.0                # Beta(1, 1) = uniform prior
PRIOR_BETA = 1.0
BUY_THRESHOLD = 0.65
SELL_THRESHOLD = 0.35
MIN_CONCENTRATION = 10           # need ≥10 evidence points to act
SCAN_INTERVAL = 30.0


class BayesianBeliefUpdater(BaseStrategy):
    """Bayesian posterior belief trader."""

    name = "ml_bayesian_belief"

    def __init__(self) -> None:
        super().__init__()
        self.prior_alpha: float = PRIOR_ALPHA
        self.prior_beta: float = PRIOR_BETA
        self.buy_threshold: float = BUY_THRESHOLD
        self.sell_threshold: float = SELL_THRESHOLD
        self.min_concentration: int = MIN_CONCENTRATION
        self._interval: float = SCAN_INTERVAL
        # Per-token Beta state.
        self._beta_state: dict[str, tuple[float, float]] = {}

    async def _run(self) -> None:
        log.info(
            "[ml_bayes] Active (prior=Beta(%.0f,%.0f), buy>=%.2f, sell<=%.2f)",
            self.prior_alpha, self.prior_beta, self.buy_threshold, self.sell_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_bayes] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Bayesian belief updater — Beta-distribution posterior "
                "probability updates from new evidence; trades when "
                "posterior crosses thresholds with sufficient concentration."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "bayesian_belief",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("prior_alpha", "prior_beta", "buy_threshold", "sell_threshold"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "min_concentration" in config:
            self.min_concentration = int(config["min_concentration"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.prior_alpha <= 0:
            return False, "prior_alpha must be > 0"
        if self.prior_beta <= 0:
            return False, "prior_beta must be > 0"
        if not 0.5 < self.buy_threshold < 1:
            return False, "buy_threshold must be in (0.5, 1)"
        if not 0 < self.sell_threshold < 0.5:
            return False, "sell_threshold must be in (0, 0.5)"
        if self.min_concentration < 1:
            return False, "min_concentration must be >= 1"
        return True, "OK"

    def _update_evidence(self, token_id: str, evidence_side: str) -> tuple[float, float]:
        """Update Beta state with a new piece of evidence.
        evidence_side: 'yes' or 'no' (or 'neutral' for no-op)."""
        state = self._beta_state.get(token_id)
        if state is None:
            state = (self.prior_alpha, self.prior_beta)
            self._beta_state[token_id] = state
        alpha, beta = state
        if evidence_side == "yes":
            alpha += 1.0
        elif evidence_side == "no":
            beta += 1.0
        self._beta_state[token_id] = (alpha, beta)
        return alpha, beta

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        evidence_side = market_context.get("evidence_side")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None
        if not 0 < mid_f < 1:
            return None

        # Update Beta state if new evidence arrived.
        if evidence_side in ("yes", "no"):
            alpha, beta = self._update_evidence(token_id, evidence_side)
        else:
            state = self._beta_state.get(token_id)
            if state is None:
                # No prior evidence — use uniform prior.
                alpha, beta = self.prior_alpha, self.prior_beta
            else:
                alpha, beta = state

        concentration = alpha + beta
        if concentration < self.min_concentration:
            return None

        p_yes = alpha / concentration
        if p_yes > self.buy_threshold:
            action = "BUY"
            target_price = round(min(mid_f + 0.005, 0.98), 4)
            reason = (
                f"Bayes BUY: P(YES)={p_yes:.3f} > {self.buy_threshold} "
                f"(α={alpha:.0f}, β={beta:.0f}, n={concentration:.0f})"
            )
        elif p_yes < self.sell_threshold:
            action = "SELL"
            target_price = round(max(mid_f - 0.005, 0.02), 4)
            reason = (
                f"Bayes SELL: P(YES)={p_yes:.3f} < {self.sell_threshold} "
                f"(α={alpha:.0f}, β={beta:.0f}, n={concentration:.0f})"
            )
        else:
            return None

        edge = min(abs(p_yes - mid_f), 0.05)
        # Confidence grows with concentration.
        confidence = min(0.9, 0.4 + min(concentration / 50.0, 0.4))

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=reason,
            metadata={
                "model": "bayesian_belief",
                "alpha": alpha,
                "beta": beta,
                "concentration": concentration,
                "p_yes": p_yes,
                "evidence_side": evidence_side,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        p_yes = float(signal.metadata.get("p_yes", 0.5))
        concentration = float(signal.metadata.get("concentration", 0.0))
        size_factor = min(2.5, abs(p_yes - 0.5) * 4 + concentration / 100.0)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no Bayesian signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "bayesian_belief",
                "alpha": signal.metadata.get("alpha"),
                "beta": signal.metadata.get("beta"),
                "p_yes": signal.metadata.get("p_yes"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when P(YES) reverts back into the neutral zone [0.35, 0.65]."""
        if not position:
            return None
        current_p_yes = float(market_context.get("current_p_yes", 0.5))
        if self.sell_threshold <= current_p_yes <= self.buy_threshold:
            return {
                "reason": "posterior reverted to neutral — exit",
                "current_p_yes": current_p_yes,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "prior_alpha": self.prior_alpha,
            "prior_beta": self.prior_beta,
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
            "tracked_tokens": len(self._beta_state),
        })
        return base
