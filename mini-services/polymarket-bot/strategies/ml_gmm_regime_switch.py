"""
strategies/ml_gmm_regime_switch.py — GMM Regime Switching Trader.

W45-1 — implements the unified strategy contract for the
``ml_gmm_regime_switch`` catalog entry.

Signal logic
------------
Gaussian Mixture Model identifying high-vol vs low-vol market regimes.
The strategy:

  * Fits a 2-component GMM to the recent volatility series.
  * Component 0 = low-vol regime (calm), Component 1 = high-vol regime (toxic).
  * Computes the posterior probability of being in each regime.
  * Trades ONLY when the high-vol regime posterior > 0.7 (regime confirmed)
    AND the directional signal (from a simple momentum indicator) is strong.

In low-vol regimes the strategy stays flat (no edge in chop).

Edge = expected regime-persistent return × confidence.
"""
from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

REGIME_WINDOW = 100               # 100-cycle regime estimation
HIGH_VOL_PROB_THRESHOLD = 0.7     # P(high-vol regime) > 0.7
MIN_MOMENTUM = 0.01               # 1% momentum required
SCAN_INTERVAL = 30.0


class GmmRegimeSwitch(BaseStrategy):
    """GMM regime-switching trader."""

    name = "ml_gmm_regime_switch"

    def __init__(self) -> None:
        super().__init__()
        self.regime_window: int = REGIME_WINDOW
        self.high_vol_prob_threshold: float = HIGH_VOL_PROB_THRESHOLD
        self.min_momentum: float = MIN_MOMENTUM
        self._interval: float = SCAN_INTERVAL
        # GMM parameters (2 components, 1-D Gaussian).
        self._means: list[float] = [0.005, 0.030]   # low-vol, high-vol
        self._variances: list[float] = [0.0001, 0.0010]
        self._weights: list[float] = [0.7, 0.3]
        self._vol_history: dict[str, deque] = {}

    async def _run(self) -> None:
        log.info(
            "[ml_gmm] Active (window=%d, high_vol_threshold=%.2f)",
            self.regime_window, self.high_vol_prob_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_gmm] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "GMM regime-switching trader — Gaussian Mixture Model "
                "identifies high-vol vs low-vol regimes; trades only when "
                "high-vol regime posterior > 0.7 with strong momentum."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "gmm_regime_switch",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "regime_window" in config:
            self.regime_window = int(config["regime_window"])
        for k in ("high_vol_prob_threshold", "min_momentum"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.regime_window < 30:
            return False, "regime_window must be >= 30"
        if not 0.5 < self.high_vol_prob_threshold < 1.0:
            return False, "high_vol_prob_threshold must be in (0.5, 1.0)"
        if self.min_momentum <= 0:
            return False, "min_momentum must be > 0"
        return True, "OK"

    def _gaussian_pdf(self, x: float, mean: float, var: float) -> float:
        if var <= 0:
            return 0.0
        return math.exp(-(x - mean) ** 2 / (2 * var)) / math.sqrt(2 * math.pi * var)

    def _compute_regime_posteriors(self, vol: float) -> list[float]:
        """Compute P(regime=k | vol) for k in {0=low-vol, 1=high-vol}."""
        likelihoods = [
            self._weights[k] * self._gaussian_pdf(vol, self._means[k], self._variances[k])
            for k in range(2)
        ]
        total = sum(likelihoods)
        if total <= 0:
            return [0.5, 0.5]
        return [l / total for l in likelihoods]

    def _update_gmm(self, volatilities: list[float]) -> None:
        """Simple EM update for the 2-component GMM (single iteration)."""
        if len(volatilities) < 10:
            return
        # E-step: compute posteriors for each observation.
        posteriors = []
        for v in volatilities:
            post = self._compute_regime_posteriors(v)
            posteriors.append(post)
        # M-step: update means / variances / weights.
        for k in range(2):
            n_k = sum(p[k] for p in posteriors)
            if n_k <= 0:
                continue
            new_mean = sum(p[k] * v for p, v in zip(posteriors, volatilities)) / n_k
            new_var = sum(p[k] * (v - new_mean) ** 2 for p, v in zip(posteriors, volatilities)) / n_k
            new_var = max(new_var, 1e-6)
            self._means[k] = new_mean
            self._variances[k] = new_var
            self._weights[k] = n_k / len(volatilities)

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        volatilities = market_context.get("volatilities")
        if not token_id or not prices:
            return None
        try:
            price_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(price_list) < self.regime_window:
            return None

        # Compute per-cycle volatility (|return|) if not provided.
        if volatilities and isinstance(volatilities, list):
            vols = [float(v) for v in volatilities[-self.regime_window:]]
        else:
            vols = [abs(price_list[i] - price_list[i - 1])
                    for i in range(1, len(price_list))][-self.regime_window:]
        if len(vols) < 10:
            return None

        # Track for diagnostics.
        history = self._vol_history.setdefault(token_id, deque(maxlen=200))
        history.append(vols[-1])

        # Update GMM.
        self._update_gmm(vols)
        # Compute current regime posterior.
        current_vol = vols[-1]
        posteriors = self._compute_regime_posteriors(current_vol)
        high_vol_prob = posteriors[1]

        # Trade only when high-vol regime confirmed.
        if high_vol_prob < self.high_vol_prob_threshold:
            return None

        # Directional signal: momentum over last 5 cycles.
        recent = price_list[-5:]
        momentum = (recent[-1] - recent[0]) / max(recent[0], 0.01)
        if abs(momentum) < self.min_momentum:
            return None

        mid = float(market_context.get("mid", price_list[-1]))
        if momentum > 0:
            action = "BUY"
            target_price = round(min(mid + 0.005, 0.98), 4)
            reason = (
                f"GMM BUY: high-vol regime (P={high_vol_prob:.2f}), "
                f"momentum={momentum:+.3f}, vol={current_vol:.4f}"
            )
        else:
            action = "SELL"
            target_price = round(max(mid - 0.005, 0.02), 4)
            reason = (
                f"GMM SELL: high-vol regime (P={high_vol_prob:.2f}), "
                f"momentum={momentum:+.3f}, vol={current_vol:.4f}"
            )

        edge = min(abs(momentum) * 0.5, 0.04)
        confidence = min(0.85, 0.4 + high_vol_prob * 0.3 + abs(momentum) * 10)

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
                "model": "gmm_regime_switch",
                "high_vol_prob": high_vol_prob,
                "low_vol_prob": posteriors[0],
                "current_volatility": current_vol,
                "momentum": momentum,
                "gmm_means": list(self._means),
                "gmm_variances": list(self._variances),
                "gmm_weights": list(self._weights),
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Smaller size in high-vol regimes (more risk per unit).
        mom = abs(float(signal.metadata.get("momentum", 0.0)))
        size_factor = min(2.0, mom * 50 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no GMM signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "gmm_regime_switch",
                "high_vol_prob": signal.metadata.get("high_vol_prob"),
                "momentum": signal.metadata.get("momentum"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when regime flips back to low-vol OR momentum reverses."""
        if not position:
            return None
        current_high_vol_prob = float(market_context.get("current_high_vol_prob", 0.0))
        if current_high_vol_prob < 0.3:
            return {
                "reason": "regime flipped to low-vol — exit",
                "current_high_vol_prob": current_high_vol_prob,
                "type": "market",
            }
        current_mom = float(market_context.get("current_momentum", 0.0))
        entry_action = position.get("entry_action", "BUY")
        if entry_action == "BUY" and current_mom < 0:
            return {
                "reason": "momentum reversed — exit long",
                "current_momentum": current_mom,
                "type": "market",
            }
        if entry_action == "SELL" and current_mom > 0:
            return {
                "reason": "momentum reversed — exit short",
                "current_momentum": current_mom,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "regime_window": self.regime_window,
            "high_vol_prob_threshold": self.high_vol_prob_threshold,
            "gmm_means": list(self._means),
            "gmm_variances": list(self._variances),
            "gmm_weights": list(self._weights),
            "tracked_tokens": len(self._vol_history),
        })
        return base
