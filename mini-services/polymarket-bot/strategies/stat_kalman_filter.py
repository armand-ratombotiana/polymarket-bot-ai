"""
strategies/stat_kalman_filter.py — Kalman Filter Fair Value Tracker.

W45-1 — implements the unified strategy contract for the
``stat_kalman_filter`` catalog entry.

Signal logic
------------
A state-space Kalman filter tracks the latent "fair value" of a market
by combining noisy price observations with a constant-velocity model.
When the observed price dislocates from the filter's posterior fair
value estimate by more than 2σ (the posterior uncertainty), the
strategy trades the dislocation expecting it to revert to the
Kalman estimate.

State model (1-D, constant value):
  x_k = x_{k-1} + w_k       (state: fair value)
  z_k = x_k + v_k            (observation: market mid)

where w_k ~ N(0, Q) is process noise and v_k ~ N(0, R) is observation
noise.

Edge = |observed - kalman_estimate| × reversion_probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

DEFAULT_Q = 0.0001               # process noise variance (low — fair value is sticky)
DEFAULT_R = 0.0025               # observation noise variance (5% mid σ)
Z_THRESHOLD = 2.0                # |z| > 2σ triggers entry
MIN_OBSERVATIONS = 10            # need ≥10 obs before signal fires
SCAN_INTERVAL = 30.0


class KalmanFilterTrader(BaseStrategy):
    """Kalman filter fair-value reversion trader."""

    name = "stat_kalman_filter"

    def __init__(self) -> None:
        super().__init__()
        self.default_q: float = DEFAULT_Q
        self.default_r: float = DEFAULT_R
        self.z_threshold: float = Z_THRESHOLD
        self.min_observations: int = MIN_OBSERVATIONS
        self._interval: float = SCAN_INTERVAL
        # Per-token Kalman state: {x_hat, P, obs_count}
        self._state: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_kalman] Active (Q=%.5f, R=%.5f, z=%.1f)",
            self.default_q, self.default_r, self.z_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_kalman] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Kalman filter fair-value trader — state-space estimator "
                "tracks latent fair value; trades dislocations ≥2σ from "
                "the posterior estimate."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "kalman_filter",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("default_q", "default_r", "z_threshold"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "min_observations" in config:
            self.min_observations = int(config["min_observations"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.default_q < 0:
            return False, "default_q must be >= 0"
        if self.default_r <= 0:
            return False, "default_r must be > 0"
        if self.z_threshold <= 0:
            return False, "z_threshold must be > 0"
        if self.min_observations < 2:
            return False, "min_observations must be >= 2"
        return True, "OK"

    def _kalman_update(self, token_id: str, observation: float) -> tuple[float, float, int]:
        """Run one Kalman prediction + update step.
        Returns (posterior_mean, posterior_variance, obs_count)."""
        state = self._state.get(token_id)
        if state is None:
            # Cold-start: initialize with the first observation.
            state = {
                "x_hat": observation,
                "P": self.default_r,  # initial uncertainty = observation noise
                "obs_count": 1,
            }
            self._state[token_id] = state
            return state["x_hat"], state["P"], state["obs_count"]

        # Predict step: x_prior = x_hat, P_prior = P + Q.
        x_prior = state["x_hat"]
        P_prior = state["P"] + self.default_q
        # Update step: K = P_prior / (P_prior + R); x_hat = x_prior + K(obs - x_prior).
        innovation = observation - x_prior
        innovation_var = P_prior + self.default_r
        K = P_prior / innovation_var if innovation_var > 0 else 0.0
        x_post = x_prior + K * innovation
        P_post = (1.0 - K) * P_prior

        state["x_hat"] = x_post
        state["P"] = P_post
        state["obs_count"] = state["obs_count"] + 1
        return x_post, P_post, state["obs_count"]

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        observation = market_context.get("price")
        if not token_id or observation is None:
            return None
        try:
            obs = float(observation)
        except (TypeError, ValueError):
            return None
        if obs <= 0 or obs >= 1:
            return None

        x_post, P_post, n_obs = self._kalman_update(token_id, obs)
        if n_obs < self.min_observations:
            return None

        sigma = max(P_post ** 0.5, 1e-6)
        z = (obs - x_post) / sigma
        if abs(z) < self.z_threshold:
            return None

        if z < 0:
            action = "BUY"
            target_price = round(min(obs + 0.005, 0.98), 4)
            reason = (
                f"Kalman BUY: obs={obs:.4f} < x̂={x_post:.4f} "
                f"(z={z:+.2f}, σ={sigma:.4f}, n={n_obs})"
            )
        else:
            action = "SELL"
            target_price = round(max(obs - 0.005, 0.02), 4)
            reason = (
                f"Kalman SELL: obs={obs:.4f} > x̂={x_post:.4f} "
                f"(z={z:+.2f}, σ={sigma:.4f}, n={n_obs})"
            )

        edge = abs(z) * sigma * 0.5
        confidence = min(0.9, 0.4 + abs(z) / 5.0 + min(n_obs / 100, 0.2))

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
                "model": "kalman_filter",
                "kalman_estimate": x_post,
                "kalman_variance": P_post,
                "kalman_sigma": sigma,
                "z_score": z,
                "observation": obs,
                "obs_count": n_obs,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        z = float(signal.metadata.get("z_score", 0.0))
        n_obs = int(signal.metadata.get("obs_count", 0))
        # Size grows with |z| and observation count (more confidence).
        z_factor = min(2.0, abs(z) / 2.0)
        n_factor = min(1.5, 0.5 + n_obs / 50.0)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * z_factor * n_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no actionable signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "kalman_filter",
                "kalman_estimate": signal.metadata.get("kalman_estimate"),
                "z_score": signal.metadata.get("z_score"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when price reverts to the Kalman estimate (|z| < 0.5)."""
        if not position:
            return None
        current_z = float(market_context.get("current_z", 0.0))
        if abs(current_z) < 0.5:
            return {
                "reason": "price reverted to Kalman estimate — take profit",
                "current_z": current_z,
                "type": "limit",
                "price": float(market_context.get("current_price", 0.5)),
            }
        # Stop-loss: |z| doubled beyond entry.
        entry_z = float(position.get("entry_z", 0.0))
        if abs(current_z) > 2.0 * abs(entry_z) and entry_z * current_z > 0:
            return {
                "reason": "dislocation worsened — stop-loss",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "default_q": self.default_q,
            "default_r": self.default_r,
            "z_threshold": self.z_threshold,
            "tracked_tokens": len(self._state),
        })
        return base
