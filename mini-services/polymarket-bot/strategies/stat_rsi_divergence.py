"""
strategies/stat_rsi_divergence.py — RSI Divergence Mean Reversion Trader.

W45-1 — implements the unified strategy contract for the
``stat_rsi_divergence`` catalog entry.

Signal logic
------------
RSI (Relative Strength Index) is a momentum oscillator ranging [0, 100]:
  RSI = 100 - 100 / (1 + RS)
  RS = avg_gain / avg_loss over the window (typically 14 cycles)

Trading rules:
  * RSI < 20 ⇒ oversold → BUY (expect bounce)
  * RSI > 80 ⇒ overbought → SELL (expect pullback)

DIVERGENCE: When price makes a new high but RSI does not (bearish
divergence), or price makes a new low but RSI does not (bullish
divergence), the divergence signal STRENGTHENS the trade conviction.

Edge = expected reversion to RSI ≈ 50, scaled by divergence_strength.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

RSI_WINDOW = 14                  # standard 14-cycle RSI
OVERBOUGHT = 80                  # RSI > 80 ⇒ SELL
OVERSOLD = 20                    # RSI < 20 ⇒ BUY
NEUTRAL_LOW = 40                 # RSI 40-60 = neutral, no signal
NEUTRAL_HIGH = 60
SCAN_INTERVAL = 30.0


class RsiDivergenceTrader(BaseStrategy):
    """RSI divergence mean-reversion trader."""

    name = "stat_rsi_divergence"

    def __init__(self) -> None:
        super().__init__()
        self.rsi_window: int = RSI_WINDOW
        self.overbought: float = OVERBOUGHT
        self.oversold: float = OVERSOLD
        self._interval: float = SCAN_INTERVAL
        self._rsi_history: dict[str, list[float]] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_rsi] Active (window=%d, OB=%.0f, OS=%.0f)",
            self.rsi_window, self.overbought, self.oversold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_rsi] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "RSI divergence mean-reversion trader — identifies "
                "overbought (RSI>80) and oversold (RSI<20) exhaustion "
                "with price/RSI divergence confirmation."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "rsi_divergence",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "rsi_window" in config:
            self.rsi_window = int(config["rsi_window"])
        for k in ("overbought", "oversold"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.rsi_window < 2:
            return False, "rsi_window must be >= 2"
        if not 50 < self.overbought <= 100:
            return False, "overbought must be in (50, 100]"
        if not 0 <= self.oversold < 50:
            return False, "oversold must be in [0, 50)"
        if self.oversold >= self.overbought:
            return False, "oversold must be < overbought"
        return True, "OK"

    def _compute_rsi(self, prices: list[float]) -> Optional[float]:
        if len(prices) < self.rsi_window + 1:
            return None
        window = prices[-(self.rsi_window + 1):]
        gains, losses = [], []
        for i in range(1, len(window)):
            change = window[i] - window[i - 1]
            gains.append(max(change, 0.0))
            losses.append(max(-change, 0.0))
        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)
        if avg_loss < 1e-9:
            return 100.0  # all gains → RSI = 100
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _detect_divergence(self, prices: list[float], rsi_history: list[float]) -> str:
        """Return 'bullish', 'bearish', or 'none'."""
        if len(prices) < 5 or len(rsi_history) < 5:
            return "none"
        # Look at the last 5 cycles.
        p_recent = prices[-5:]
        r_recent = rsi_history[-5:]
        # Bearish divergence: price makes higher high, RSI makes lower high.
        if p_recent[-1] == max(p_recent) and r_recent[-1] < max(r_recent[:-1]):
            return "bearish"
        # Bullish divergence: price makes lower low, RSI makes higher low.
        if p_recent[-1] == min(p_recent) and r_recent[-1] > min(r_recent[:-1]):
            return "bullish"
        return "none"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        if not token_id or not prices or not isinstance(prices, list):
            return None
        try:
            price_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None

        rsi = self._compute_rsi(price_list)
        if rsi is None:
            return None

        # Track RSI history for divergence detection.
        history = self._rsi_history.setdefault(token_id, [])
        history.append(rsi)
        if len(history) > 100:
            history.pop(0)

        divergence = self._detect_divergence(price_list, history)

        if rsi <= self.oversold:
            action = "BUY"
            # Bullish divergence strengthens the BUY signal.
            confidence = 0.7 if divergence == "bullish" else 0.5
            target_price = round(min(price_list[-1] + 0.01, 0.98), 4)
            reason = f"RSI BUY: rsi={rsi:.1f} < {self.oversold} (div={divergence})"
        elif rsi >= self.overbought:
            action = "SELL"
            confidence = 0.7 if divergence == "bearish" else 0.5
            target_price = round(max(price_list[-1] - 0.01, 0.02), 4)
            reason = f"RSI SELL: rsi={rsi:.1f} > {self.overbought} (div={divergence})"
        else:
            return None

        # Edge = expected reversion to RSI ≈ 50, scaled by |rsi - 50|/50.
        edge = abs(rsi - 50) / 100.0 * (1.5 if divergence != "none" else 1.0)

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
                "model": "rsi_divergence",
                "rsi": rsi,
                "divergence": divergence,
                "rsi_window": self.rsi_window,
                "overbought": self.overbought,
                "oversold": self.oversold,
                "current_price": price_list[-1],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        rsi = float(signal.metadata.get("rsi", 50))
        # Bigger position when RSI is more extreme.
        extremity = abs(rsi - 50) / 50.0
        size_factor = min(2.0, 0.5 + extremity)
        if signal.metadata.get("divergence") != "none":
            size_factor *= 1.25
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

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
                "model": "rsi_divergence",
                "rsi": signal.metadata.get("rsi"),
                "divergence": signal.metadata.get("divergence"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when RSI reverts to the neutral zone (40-60)."""
        if not position:
            return None
        current_rsi = float(market_context.get("rsi", 50))
        if NEUTRAL_LOW <= current_rsi <= NEUTRAL_HIGH:
            return {
                "reason": "RSI reverted to neutral — take profit",
                "current_rsi": current_rsi,
                "type": "limit",
                "price": float(market_context.get("current_price", 0.5)),
            }
        # Stop-loss: RSI went further into the extreme by 10 points.
        entry_rsi = float(position.get("entry_rsi", 50))
        entry_action = position.get("entry_action", "BUY")
        if entry_action == "BUY" and current_rsi < entry_rsi - 10:
            return {
                "reason": "RSI kept falling — stop-loss",
                "entry_rsi": entry_rsi,
                "current_rsi": current_rsi,
                "type": "market",
            }
        if entry_action == "SELL" and current_rsi > entry_rsi + 10:
            return {
                "reason": "RSI kept rising — stop-loss",
                "entry_rsi": entry_rsi,
                "current_rsi": current_rsi,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "rsi_window": self.rsi_window,
            "overbought": self.overbought,
            "oversold": self.oversold,
            "tracked_tokens": len(self._rsi_history),
        })
        return base
