"""
strategies/stat_pair_cointegration.py — Pair Cointegration Trader.

W45-1 — implements the unified strategy contract for the
``stat_pair_cointegration`` catalog entry.

Signal logic
------------
Identifies cointegrated pairs (using a simplified Augmented
Dickey-Fuller test on the spread series). When two markets are
cointegrated, their spread reverts to a long-run mean — buy the
underpriced leg, sell the overpriced leg when the spread
dislocates by ≥2σ.

The on-chain cointegration test is approximated by a stationary
residual check: if the spread's recent mean reverts faster than
a random walk (half_life < window / 2), the pair is cointegrated.

Edge = expected spread reversion × entry probability.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

WINDOW = 100                     # 100-tick cointegration lookback
Z_THRESHOLD = 2.0                # |spread z| > 2.0 triggers entry
HALF_LIFE_MAX = 50                # spread must mean-revert within 50 ticks
MIN_HALF_LIFE = 2                # but not too fast (noise)
SCAN_INTERVAL = 60.0


class PairCointegrationTrader(BaseStrategy):
    """Cointegrated pairs trading (ADF-style spread reversion)."""

    name = "stat_pair_cointegration"

    def __init__(self) -> None:
        super().__init__()
        self.window: int = WINDOW
        self.z_threshold: float = Z_THRESHOLD
        self.half_life_max: int = HALF_LIFE_MAX
        self.min_half_life: int = MIN_HALF_LIFE
        self._interval: float = SCAN_INTERVAL
        self._spread_history: dict[str, deque] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_coint] Active (window=%d, z_threshold=%.1f)",
            self.window, self.z_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_coint] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Pair cointegration trader — ADF-style spread mean-reversion "
                "across cointegrated market pairs (half-life calibrated)."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "pair_cointegration",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "window" in config:
            self.window = int(config["window"])
        if "z_threshold" in config:
            self.z_threshold = float(config["z_threshold"])
        for k in ("half_life_max", "min_half_life"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.window < 30:
            return False, "window must be >= 30"
        if self.z_threshold <= 0:
            return False, "z_threshold must be > 0"
        if self.half_life_max < self.min_half_life:
            return False, "half_life_max must be >= min_half_life"
        if self.min_half_life < 1:
            return False, "min_half_life must be >= 1"
        return True, "OK"

    def _compute_spread_stats(
        self, spreads: list[float]
    ) -> tuple[float, float, float]:
        """Return (mean, stdev, half_life) of the spread series."""
        n = len(spreads)
        if n < 5:
            return 0.0, 1.0, 0.0
        mean = sum(spreads) / n
        var = sum((s - mean) ** 2 for s in spreads) / n
        sigma = max(var ** 0.5, 1e-6)
        # Ornstein-Uhlenbeck half-life approximation:
        #   half_life ≈ -ln(2) / ln(ρ), where ρ is the lag-1 autocorrelation.
        if n < 10:
            return mean, sigma, 0.0
        lag1_corr_num = sum(
            (spreads[i] - mean) * (spreads[i - 1] - mean) for i in range(1, n)
        )
        lag1_corr_den = sum((s - mean) ** 2 for s in spreads)
        if lag1_corr_den <= 0:
            return mean, sigma, 0.0
        rho = lag1_corr_num / lag1_corr_den
        if rho <= 0 or rho >= 1:
            return mean, sigma, 0.0  # non-stationary or no autocorrelation
        import math
        half_life = -math.log(2) / math.log(rho)
        return mean, sigma, half_life

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        pair_id = market_context.get("pair_id")
        token_a = market_context.get("token_a")
        token_b = market_context.get("token_b")
        spreads = market_context.get("spreads")
        if not pair_id or not token_a or not token_b:
            return None
        if not spreads or not isinstance(spreads, list):
            return None
        try:
            s_list = [float(s) for s in spreads]
        except (TypeError, ValueError):
            return None
        if len(s_list) < self.window:
            return None

        window = s_list[-self.window:]
        mean, sigma, half_life = self._compute_spread_stats(window)
        # Track for diagnostics.
        history = self._spread_history.setdefault(pair_id, deque(maxlen=200))
        history.append(s_list[-1])

        # Cointegration gate: spread must mean-revert within half_life_max.
        if half_life <= 0 or half_life > self.half_life_max:
            return None
        if half_life < self.min_half_life:
            return None  # noise — reverts too fast

        current_spread = s_list[-1]
        z = (current_spread - mean) / sigma
        if abs(z) < self.z_threshold:
            return None

        # Direction: when z > 0, spread is too wide → BUY B, SELL A.
        if z > 0:
            action = "BUY"
            primary_token = token_b
            counter_token = token_a
            counter_side = "SELL"
        else:
            action = "BUY"
            primary_token = token_a
            counter_token = token_b
            counter_side = "SELL"

        # Edge = expected spread reversion to mean, capped at the
        # z-score × σ magnitude.
        edge = abs(z) * sigma * 0.5
        confidence = min(0.85, 0.5 + abs(z) / 10.0 + (1.0 - half_life / self.half_life_max) * 0.2)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=primary_token,
            size=1.0,
            price=float(market_context.get("primary_price", 0.5)),
            confidence=confidence,
            edge=edge,
            reason=(
                f"Cointegration {action}: z={z:+.2f}, spread={current_spread:.4f}, "
                f"mean={mean:.4f}, σ={sigma:.4f}, half_life={half_life:.1f}"
            ),
            metadata={
                "model": "pair_cointegration",
                "pair_id": pair_id,
                "token_a": token_a,
                "token_b": token_b,
                "z_score": z,
                "spread_mean": mean,
                "spread_sigma": sigma,
                "half_life": half_life,
                "current_spread": current_spread,
                "counter_leg": {
                    "token_id": counter_token,
                    "side": counter_side,
                },
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        z = float(signal.metadata.get("z_score", 0.0))
        half_life = float(signal.metadata.get("half_life", 10.0))
        # Size scales with |z| and inverse with half_life.
        z_factor = min(2.0, abs(z) / 2.0)
        hl_factor = max(0.5, 1.0 - half_life / self.half_life_max)
        base_size = float(risk_params.get("base_size_usdc", 3.0))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(base_size * z_factor * hl_factor, max_pct * capital, capital)

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
                "model": "pair_cointegration",
                "pair_id": signal.metadata.get("pair_id"),
                "counter_leg": signal.metadata.get("counter_leg"),
                "z_score": signal.metadata.get("z_score"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when z reverts to 0 (spread normalized)."""
        if not position:
            return None
        current_z = float(market_context.get("current_z", 0.0))
        if abs(current_z) < 0.5:
            return {
                "reason": "spread reverted to mean — close pair",
                "current_z": current_z,
                "type": "market",
            }
        # Stop-loss: z doubled beyond entry.
        entry_z = float(position.get("entry_z", 0.0))
        if abs(current_z) > 2.0 * abs(entry_z) and entry_z * current_z > 0:
            return {
                "reason": "spread kept dislocating — stop-loss",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "market",
            }
        # Time-stop: held past 2× expected half_life.
        ticks_held = int(position.get("ticks_held", 0))
        half_life = float(position.get("half_life", 10.0))
        if ticks_held > 2 * half_life:
            return {
                "reason": "time-stop — held past 2× half_life",
                "ticks_held": ticks_held,
                "half_life": half_life,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "window": self.window,
            "z_threshold": self.z_threshold,
            "half_life_max": self.half_life_max,
            "tracked_pairs": len(self._spread_history),
        })
        return base
