"""
strategies/mom_parabolic_sar.py — Parabolic SAR Trend Follower.

W45-1 — implements the unified strategy contract for the
``mom_parabolic_sar`` catalog entry.

Signal logic
------------
Parabolic Stop-and-Reverse (SAR) — a trend-following indicator that
sets trailing stop points that accelerate as the trend extends. When
price crosses the SAR, the position flips:
  * Price > SAR → BUY (uptrend intact)
  * Price < SAR → SELL (downtrend intact, stop-and-reverse)

SAR update (Wilder's classic formula):
  SAR_t = SAR_{t-1} + AF × (EP - SAR_{t-1})
  where AF starts at 0.02 and increments by 0.02 per new EP (max 0.20),
  and EP = extreme point (highest high in uptrend / lowest low in
  downtrend).

Edge = expected trend persistence × distance from SAR to price.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

AF_START = 0.02                   # initial acceleration factor
AF_STEP = 0.02                    # AF increment per new EP
AF_MAX = 0.20                     # AF cap
MIN_HISTORY = 10
SCAN_INTERVAL = 30.0


class ParabolicSarFollower(BaseStrategy):
    """Parabolic SAR trend follower."""

    name = "mom_parabolic_sar"

    def __init__(self) -> None:
        super().__init__()
        self.af_start: float = AF_START
        self.af_step: float = AF_STEP
        self.af_max: float = AF_MAX
        self.min_history: int = MIN_HISTORY
        self._interval: float = SCAN_INTERVAL
        # Per-token SAR state: {sar, af, ep, trend}
        self._sar_state: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_psar] Active (AF=%.2f→%.2f, step=%.2f)",
            self.af_start, self.af_max, self.af_step,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_psar] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Parabolic SAR trend follower — uses Wilder's stop-and-reverse "
                "indicator with accelerating trailing stops to capture trends."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "parabolic_sar",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("af_start", "af_step", "af_max"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "min_history" in config:
            self.min_history = int(config["min_history"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.af_start <= 0:
            return False, "af_start must be > 0"
        if self.af_step <= 0:
            return False, "af_step must be > 0"
        if self.af_max <= self.af_start:
            return False, "af_max must be > af_start"
        if self.min_history < 5:
            return False, "min_history must be >= 5"
        return True, "OK"

    def _update_sar(self, token_id: str, high: float, low: float, close: float) -> tuple[float, str]:
        """Update the SAR state and return (sar_value, trend)."""
        state = self._sar_state.get(token_id)
        if state is None:
            # Initialize: assume uptrend, SAR = first low, EP = first high.
            state = {
                "sar": low,
                "af": self.af_start,
                "ep": high,
                "trend": "up",
            }
            self._sar_state[token_id] = state
            return state["sar"], state["trend"]

        if state["trend"] == "up":
            # New EP → bump AF.
            if high > state["ep"]:
                state["ep"] = high
                state["af"] = min(state["af"] + self.af_step, self.af_max)
            # SAR update.
            new_sar = state["sar"] + state["af"] * (state["ep"] - state["sar"])
            # SAR can't go below the prior two lows (safety clamp).
            new_sar = min(new_sar, low)
            # Trend reversal: price dropped below SAR.
            if close < new_sar:
                state["trend"] = "down"
                state["sar"] = state["ep"]  # reset SAR to prior EP
                state["ep"] = low
                state["af"] = self.af_start
            else:
                state["sar"] = new_sar
        else:  # trend == "down"
            if low < state["ep"]:
                state["ep"] = low
                state["af"] = min(state["af"] + self.af_step, self.af_max)
            new_sar = state["sar"] + state["af"] * (state["ep"] - state["sar"])
            new_sar = max(new_sar, high)
            if close > new_sar:
                state["trend"] = "up"
                state["sar"] = state["ep"]
                state["ep"] = high
                state["af"] = self.af_start
            else:
                state["sar"] = new_sar
        return state["sar"], state["trend"]

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

        # Compute OHLC-lite: use prior cycle close as "low"/"high" (single
        # price series; we approximate high/low with ±0.5% bounds).
        high = price_list[-1] * 1.005
        low = price_list[-1] * 0.995
        close = price_list[-1]

        sar, trend = self._update_sar(token_id, high, low, close)

        # Detect trend flip: prior trend was opposite of current.
        prev_trend = self._sar_state[token_id].get("prev_trend", trend)
        self._sar_state[token_id]["prev_trend"] = trend
        if prev_trend == trend:
            # No flip — only emit signal on reversal events.
            return None

        if trend == "up":
            action = "BUY"
            target_price = round(min(close + 0.005, 0.98), 4)
            reason = (
                f"PSAR BUY: trend flipped up, SAR={sar:.4f}, "
                f"close={close:.4f}, AF={self._sar_state[token_id]['af']:.2f}"
            )
        else:
            action = "SELL"
            target_price = round(max(close - 0.005, 0.02), 4)
            reason = (
                f"PSAR SELL: trend flipped down, SAR={sar:.4f}, "
                f"close={close:.4f}, AF={self._sar_state[token_id]['af']:.2f}"
            )

        # Edge = expected trend persistence × |close - SAR|.
        distance = abs(close - sar)
        edge = min(distance * 0.5, 0.05)
        confidence = min(0.85, 0.5 + distance * 5)

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
                "model": "parabolic_sar",
                "sar": sar,
                "trend": trend,
                "af": self._sar_state[token_id]["af"],
                "ep": self._sar_state[token_id]["ep"],
                "current_price": close,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        distance = abs(float(signal.metadata.get("current_price", 0.5)) -
                       float(signal.metadata.get("sar", 0.5)))
        size_factor = min(2.0, distance * 20 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no SAR reversal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "parabolic_sar",
                "sar": signal.metadata.get("sar"),
                "trend": signal.metadata.get("trend"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit on SAR trend reversal (stop-and-reverse)."""
        if not position:
            return None
        entry_action = position.get("entry_action", "BUY")
        current_trend = market_context.get("current_trend", "up")
        # Stop-and-reverse: exit on trend flip.
        if entry_action == "BUY" and current_trend == "down":
            return {
                "reason": "SAR flipped to down — stop-and-reverse",
                "current_trend": current_trend,
                "type": "market",
            }
        if entry_action == "SELL" and current_trend == "up":
            return {
                "reason": "SAR flipped to up — stop-and-reverse",
                "current_trend": current_trend,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "af_start": self.af_start,
            "af_max": self.af_max,
            "af_step": self.af_step,
            "tracked_tokens": len(self._sar_state),
        })
        return base
