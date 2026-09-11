"""
strategies/stat_half_life_decay.py — Half-Life Decay Reverter.

W45-1 — implements the unified strategy contract for the
``stat_half_life_decay`` catalog entry.

Signal logic
------------
Calibrates the trade horizon to the statistical mean-reversion
half-life of the price series. The half-life (HL) is the expected
time for a dislocation from the mean to decay by 50%.

Computation (Ornstein-Uhlenbeck approximation):
  Δp_t = -λ (p_{t-1} - μ) + ε_t
  half_life = ln(2) / λ

Trading rules:
  * Only act on series with 2 ≤ HL ≤ 50 (mean-reverts fast enough
    to trade but not noise).
  * Enter when |price - SMA| × decay_probability > threshold.
  * Exit when half the half-life has elapsed (50% decay expected).

Edge = expected decay × entry probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

WINDOW = 100                     # 100-cycle window for half-life estimation
MIN_HALF_LIFE = 2
MAX_HALF_LIFE = 50
ENTRY_THRESHOLD = 0.02            # 2% deviation required
SCAN_INTERVAL = 30.0


class HalfLifeDecayReverter(BaseStrategy):
    """Half-life-calibrated mean reversion trader."""

    name = "stat_half_life_decay"

    def __init__(self) -> None:
        super().__init__()
        self.window: int = WINDOW
        self.min_half_life: int = MIN_HALF_LIFE
        self.max_half_life: int = MAX_HALF_LIFE
        self.entry_threshold: float = ENTRY_THRESHOLD
        self._interval: float = SCAN_INTERVAL
        self._half_life_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_halflife] Active (window=%d, HL=[%d-%d])",
            self.window, self.min_half_life, self.max_half_life,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_halflife] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Half-life calibrated reverter — calibrates trade horizon "
                "to statistical mean-reversion half-life (Ornstein-Uhlenbeck)."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "half_life_decay",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "window" in config:
            self.window = int(config["window"])
        for k in ("min_half_life", "max_half_life"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "entry_threshold" in config:
            self.entry_threshold = float(config["entry_threshold"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.window < 30:
            return False, "window must be >= 30"
        if self.min_half_life < 1:
            return False, "min_half_life must be >= 1"
        if self.max_half_life <= self.min_half_life:
            return False, "max_half_life must be > min_half_life"
        if self.entry_threshold <= 0:
            return False, "entry_threshold must be > 0"
        return True, "OK"

    def _estimate_half_life(self, prices: list[float]) -> Optional[float]:
        """OU half-life via OLS regression of Δp on (p - mean)."""
        import math
        n = len(prices)
        if n < 10:
            return None
        window = prices[-self.window:] if n >= self.window else prices
        m = len(window)
        if m < 10:
            return None
        mean = sum(window) / m
        # Δp_t = p_t - p_{t-1}, lagged = p_{t-1} - mean.
        deltas = [window[i] - window[i - 1] for i in range(1, m)]
        lagged = [window[i - 1] - mean for i in range(1, m)]
        if not deltas:
            return None
        # OLS slope: λ = -Σ(lagged × delta) / Σ(lagged²)
        num = sum(l * d for l, d in zip(lagged, deltas))
        den = sum(l * l for l in lagged)
        if den <= 0:
            return None
        # Negative slope = mean-reverting. We expect λ > 0 from the regression
        # of Δp on -(p - mean); here we regress Δp on (p - mean), so the slope
        # should be negative (mean reversion). |slope| = λ.
        slope = num / den
        if slope >= 0:
            return None  # not mean-reverting
        lam = -slope  # λ = |slope|
        if lam <= 0:
            return None
        half_life = math.log(2) / lam
        return half_life

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        if not token_id or not prices or not isinstance(prices, list):
            return None
        try:
            price_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(price_list) < self.window:
            return None

        window_prices = price_list[-self.window:]
        mean = sum(window_prices) / self.window
        current = price_list[-1]
        deviation = current - mean

        if abs(deviation) < self.entry_threshold:
            return None

        half_life = self._estimate_half_life(price_list)
        if half_life is None:
            return None
        if not self.min_half_life <= half_life <= self.max_half_life:
            return None
        self._half_life_cache[token_id] = half_life

        # Decay probability: 1 - exp(-1/half_life) per cycle.
        # Over half_life cycles, the dislocation should decay ~50%.
        import math
        decay_prob_per_cycle = 1.0 - math.exp(-1.0 / half_life)
        expected_decay = deviation * 0.5  # half-life decay target
        if abs(expected_decay) < 0.005:
            return None

        if deviation < 0:
            action = "BUY"
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"HalfLife BUY: HL={half_life:.1f}, dev={deviation:+.4f} "
                f"(decay_prob={decay_prob_per_cycle:.3f}/cycle)"
            )
        else:
            action = "SELL"
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"HalfLife SELL: HL={half_life:.1f}, dev={deviation:+.4f} "
                f"(decay_prob={decay_prob_per_cycle:.3f}/cycle)"
            )

        edge = abs(expected_decay) * decay_prob_per_cycle
        confidence = min(0.85, 0.4 + (1.0 - half_life / self.max_half_life) * 0.4)

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
                "model": "half_life_decay",
                "half_life": half_life,
                "decay_prob_per_cycle": decay_prob_per_cycle,
                "mean": mean,
                "deviation": deviation,
                "current_price": current,
                "expected_decay": expected_decay,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        hl = float(signal.metadata.get("half_life", 10.0))
        dev = abs(float(signal.metadata.get("deviation", 0.0)))
        # Bigger position when HL is shorter (faster reversion).
        hl_factor = max(0.5, 1.5 - hl / self.max_half_life)
        dev_factor = min(2.0, dev / self.entry_threshold)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * hl_factor * dev_factor, max_pct * capital, capital)

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
                "model": "half_life_decay",
                "half_life": signal.metadata.get("half_life"),
                "expected_hold_cycles": float(signal.metadata.get("half_life", 10.0)),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when half the half-life has elapsed (50% decay expected)
        OR when deviation reverts below threshold."""
        if not position:
            return None
        cycles_held = int(position.get("cycles_held", 0))
        half_life = float(position.get("half_life", 10.0))
        # Time-stop: held past half_life (reversion should be complete).
        if cycles_held >= half_life:
            return {
                "reason": "half-life elapsed — exit on time-stop",
                "cycles_held": cycles_held,
                "half_life": half_life,
                "type": "market",
            }
        current_dev = float(market_context.get("current_deviation", 0.0))
        if abs(current_dev) < self.entry_threshold * 0.5:
            return {
                "reason": "deviation reverted — take profit",
                "current_deviation": current_dev,
                "type": "limit",
                "price": float(market_context.get("current_price", 0.5)),
            }
        # Stop-loss: deviation doubled since entry.
        entry_dev = float(position.get("entry_deviation", 0.0))
        if (abs(current_dev) > 2.0 * abs(entry_dev)
                and entry_dev * current_dev > 0):
            return {
                "reason": "deviation doubled — stop-loss",
                "entry_deviation": entry_dev,
                "current_deviation": current_dev,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "window": self.window,
            "min_half_life": self.min_half_life,
            "max_half_life": self.max_half_life,
            "half_life_cache_size": len(self._half_life_cache),
        })
        return base
