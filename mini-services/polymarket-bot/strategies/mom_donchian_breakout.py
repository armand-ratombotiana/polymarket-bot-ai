"""
strategies/mom_donchian_breakout.py — Donchian Channel Breakout Trader.

W45-1 — implements the unified strategy contract for the
``mom_donchian_breakout`` catalog entry.

Signal logic
------------
Classic Donchian channel breakout (a.k.a. "turtle trading"):
  * BUY when price > max(high[-N:]) — bullish breakout
  * SELL when price < min(low[-N:]) — bearish breakout

Default N=20 (industry standard). The strategy rides the breakout
with a trailing stop at the opposite channel edge.

Edge = expected channel expansion × breakout persistence probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

CHANNEL_PERIOD = 20
MIN_HISTORY = 25
STOP_LOSS_PCT = 0.05              # 5% stop-loss from entry
SCAN_INTERVAL = 30.0


class DonchianBreakoutTrader(BaseStrategy):
    """20-period Donchian channel breakout trader."""

    name = "mom_donchian_breakout"

    def __init__(self) -> None:
        super().__init__()
        self.channel_period: int = CHANNEL_PERIOD
        self.min_history: int = MIN_HISTORY
        self.stop_loss_pct: float = STOP_LOSS_PCT
        self._interval: float = SCAN_INTERVAL
        self._channel_cache: dict[str, tuple[float, float]] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_donchian] Active (channel=%d, stop=%.0f%%)",
            self.channel_period, self.stop_loss_pct * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_donchian] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Donchian channel breakout trader — buys when price breaks "
                "above N-period high, sells when below N-period low, with "
                "trailing stop at the opposite channel edge."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "donchian_breakout",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("channel_period", "min_history"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "stop_loss_pct" in config:
            self.stop_loss_pct = float(config["stop_loss_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.channel_period < 5:
            return False, "channel_period must be >= 5"
        if self.min_history < self.channel_period:
            return False, "min_history must be >= channel_period"
        if not 0 < self.stop_loss_pct <= 0.20:
            return False, "stop_loss_pct must be in (0, 0.20]"
        return True, "OK"

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

        # Donchian channel over the prior N periods (excluding current).
        lookback = price_list[-(self.channel_period + 1):-1]
        channel_high = max(lookback)
        channel_low = min(lookback)
        self._channel_cache[token_id] = (channel_high, channel_low)

        current = price_list[-1]
        if current > channel_high:
            action = "BUY"
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"Donchian BUY: p={current:.4f} > high={channel_high:.4f} "
                f"(N={self.channel_period}, low={channel_low:.4f})"
            )
        elif current < channel_low:
            action = "SELL"
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"Donchian SELL: p={current:.4f} < low={channel_low:.4f} "
                f"(N={self.channel_period}, high={channel_high:.4f})"
            )
        else:
            return None

        # Edge = expected channel expansion magnitude.
        channel_width = channel_high - channel_low
        edge = min(channel_width * 0.3, 0.05)
        confidence = min(0.85, 0.5 + channel_width * 5)

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
                "model": "donchian_breakout",
                "channel_high": channel_high,
                "channel_low": channel_low,
                "channel_width": channel_width,
                "current_price": current,
                "channel_period": self.channel_period,
                "stop_loss_pct": self.stop_loss_pct,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        width = float(signal.metadata.get("channel_width", 0.02))
        size_factor = min(2.0, width * 20 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no breakout signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "donchian_breakout",
                "channel_high": signal.metadata.get("channel_high"),
                "channel_low": signal.metadata.get("channel_low"),
                "stop_loss_pct": signal.metadata.get("stop_loss_pct"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit on opposite channel break or stop-loss hit."""
        if not position:
            return None
        entry_action = position.get("entry_action", "BUY")
        current_price = float(market_context.get("current_price", 0.0))
        entry_price = float(position.get("entry_price", current_price))
        # Stop-loss check.
        if entry_action == "BUY":
            if current_price < entry_price * (1 - self.stop_loss_pct):
                return {
                    "reason": "stop-loss hit — exit long",
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "type": "market",
                }
            # Exit on opposite channel break.
            current_low = float(market_context.get("current_channel_low", 0.0))
            if current_low > 0 and current_price < current_low:
                return {
                    "reason": "broke below channel low — exit long",
                    "current_price": current_price,
                    "channel_low": current_low,
                    "type": "market",
                }
        else:  # SELL
            if current_price > entry_price * (1 + self.stop_loss_pct):
                return {
                    "reason": "stop-loss hit — exit short",
                    "entry_price": entry_price,
                    "current_price": current_price,
                    "type": "market",
                }
            current_high = float(market_context.get("current_channel_high", 1.0))
            if current_high < 1 and current_price > current_high:
                return {
                    "reason": "broke above channel high — exit short",
                    "current_price": current_price,
                    "channel_high": current_high,
                    "type": "market",
                }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "channel_period": self.channel_period,
            "stop_loss_pct": self.stop_loss_pct,
            "tracked_tokens": len(self._channel_cache),
        })
        return base
