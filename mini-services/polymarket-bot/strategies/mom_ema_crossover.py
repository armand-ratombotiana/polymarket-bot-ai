"""
strategies/mom_ema_crossover.py — EMA Crossover Trend Trader.

W45-1 — implements the unified strategy contract for the
``mom_ema_crossover`` catalog entry.

Signal logic
------------
Fast/Slow Exponential Moving Average crossover trend capture:
  * Fast EMA(8) crosses above Slow EMA(21) → BUY (bullish trend)
  * Fast EMA(8) crosses below Slow EMA(21) → SELL (bearish trend)

EMA weights recent prices more heavily than SMA, making it more
responsive to recent price changes — well-suited for trend-following
in fast-moving prediction markets.

A "cross" only fires when the fast/slow gap flips sign between
consecutive cycles (not on every cycle that the fast is above the
slow — only on the actual cross event).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

FAST_PERIOD = 8
SLOW_PERIOD = 21
MIN_HISTORY = 25                 # need ≥25 prices to compute slow EMA
CONFIRMATION_CYCLES = 1          # 1-cycle confirmation (default)
SCAN_INTERVAL = 30.0


class EmaCrossoverTrend(BaseStrategy):
    """Fast/Slow EMA crossover trend trader (8/21)."""

    name = "mom_ema_crossover"

    def __init__(self) -> None:
        super().__init__()
        self.fast_period: int = FAST_PERIOD
        self.slow_period: int = SLOW_PERIOD
        self.min_history: int = MIN_HISTORY
        self.confirmation_cycles: int = CONFIRMATION_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._prev_gap: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_ema] Active (fast=%d, slow=%d)",
            self.fast_period, self.slow_period,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_ema] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Fast/Slow EMA crossover trend trader (8/21) — enters on "
                "EMA cross events, exits on opposite cross or stop-loss."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "ema_crossover",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("fast_period", "slow_period", "min_history", "confirmation_cycles"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.fast_period < 2:
            return False, "fast_period must be >= 2"
        if self.slow_period <= self.fast_period:
            return False, "slow_period must be > fast_period"
        if self.min_history < self.slow_period:
            return False, "min_history must be >= slow_period"
        if self.confirmation_cycles < 0:
            return False, "confirmation_cycles must be >= 0"
        return True, "OK"

    def _compute_ema(self, prices: list[float], period: int) -> Optional[float]:
        if len(prices) < period:
            return None
        # Seed with SMA of the first `period` prices.
        seed = sum(prices[:period]) / period
        alpha = 2.0 / (period + 1)
        ema = seed
        for p in prices[period:]:
            ema = alpha * p + (1 - alpha) * ema
        return ema

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        if not token_id or not prices or not isinstance(prices, list):
            return None
        try:
            price_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(price_list) < self.min_history:
            return None

        fast_ema = self._compute_ema(price_list, self.fast_period)
        slow_ema = self._compute_ema(price_list, self.slow_period)
        if fast_ema is None or slow_ema is None:
            return None

        current_gap = fast_ema - slow_ema
        prev_gap = self._prev_gap.get(token_id, current_gap)
        self._prev_gap[token_id] = current_gap

        # Detect cross: gap flipped sign between consecutive cycles.
        cross_up = prev_gap <= 0 and current_gap > 0
        cross_down = prev_gap >= 0 and current_gap < 0

        if not (cross_up or cross_down):
            return None  # no cross this cycle

        current_price = price_list[-1]
        if cross_up:
            action = "BUY"
            target_price = round(min(current_price + 0.005, 0.98), 4)
            reason = (
                f"EMA cross UP: fast={fast_ema:.4f} > slow={slow_ema:.4f} "
                f"(prev_gap={prev_gap:+.5f}, cur_gap={current_gap:+.5f})"
            )
        else:
            action = "SELL"
            target_price = round(max(current_price - 0.005, 0.02), 4)
            reason = (
                f"EMA cross DOWN: fast={fast_ema:.4f} < slow={slow_ema:.4f} "
                f"(prev_gap={prev_gap:+.5f}, cur_gap={current_gap:+.5f})"
            )

        # Edge = expected gap persistence × gap magnitude.
        gap_magnitude = abs(current_gap)
        edge = min(gap_magnitude, 0.05)  # cap at 5%
        confidence = min(0.85, 0.5 + gap_magnitude * 10)

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
                "model": "ema_crossover",
                "fast_ema": fast_ema,
                "slow_ema": slow_ema,
                "gap": current_gap,
                "prev_gap": prev_gap,
                "cross_type": "up" if cross_up else "down",
                "current_price": current_price,
                "fast_period": self.fast_period,
                "slow_period": self.slow_period,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        gap = abs(float(signal.metadata.get("gap", 0.0)))
        size_factor = min(2.0, gap * 50 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no cross signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "ema_crossover",
                "fast_ema": signal.metadata.get("fast_ema"),
                "slow_ema": signal.metadata.get("slow_ema"),
                "cross_type": signal.metadata.get("cross_type"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit on opposite EMA cross (trend reversal) or stop-loss."""
        if not position:
            return None
        entry_action = position.get("entry_action", "BUY")
        current_gap = float(market_context.get("current_gap", 0.0))
        entry_gap = float(position.get("entry_gap", 0.0))
        # Exit on opposite cross.
        if entry_action == "BUY" and current_gap < 0:
            return {
                "reason": "opposite EMA cross — exit long",
                "current_gap": current_gap,
                "type": "market",
            }
        if entry_action == "SELL" and current_gap > 0:
            return {
                "reason": "opposite EMA cross — exit short",
                "current_gap": current_gap,
                "type": "market",
            }
        # Stop-loss: gap collapsed below 25% of entry gap magnitude.
        if abs(current_gap) < 0.25 * abs(entry_gap) and entry_gap * current_gap > 0:
            return {
                "reason": "EMA gap collapsed — stop-loss",
                "entry_gap": entry_gap,
                "current_gap": current_gap,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "tracked_tokens": len(self._prev_gap),
        })
        return base
