"""
strategies/stat_bollinger_reversion.py — Bollinger Bands Reversion Trader.

W45-1 — implements the unified strategy contract for the
``stat_bollinger_reversion`` catalog entry.

Signal logic
------------
Standard Bollinger Bands (2.5σ) reversion trader:
  * BUY when price touches/breaches the lower band (oversold).
  * SELL when price touches/breaches the upper band (overbought).

Differs from ``mean_reversion.py`` (which uses 2σ bands and a 2%
MIN_DEVIATION filter) by using the wider 2.5σ band (more selective,
fewer false signals) and a tighter deviation floor of 1% to catch
genuine reversion opportunities earlier.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

WINDOW = 20                       # 20-cycle SMA window
K_SIGMA = 2.5                     # 2.5σ bands (tighter than mean_reversion)
MIN_DEVIATION = 0.01              # 1% deviation floor
MAX_LOOKBACK = 100                # max prices to retain per token
SCAN_INTERVAL = 30.0


class BollingerBandsReversion(BaseStrategy):
    """2.5σ Bollinger Bands reversion trader."""

    name = "stat_bollinger_reversion"

    def __init__(self) -> None:
        super().__init__()
        self.window: int = WINDOW
        self.k_sigma: float = K_SIGMA
        self.min_deviation: float = MIN_DEVIATION
        self._price_history: dict[str, deque] = {}
        self._interval: float = SCAN_INTERVAL

    async def _run(self) -> None:
        log.info(
            "[stat_bollinger] Active (window=%d, k=%.1f)",
            self.window, self.k_sigma,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_bollinger] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Bollinger Bands reversion trader — BUY when price breaches "
                "lower 2.5σ band, SELL when price breaches upper 2.5σ band."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "bollinger_bands_reversion",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "window" in config:
            self.window = int(config["window"])
        for k in ("k_sigma", "min_deviation"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.window < 5:
            return False, "window must be >= 5"
        if self.k_sigma <= 0:
            return False, "k_sigma must be > 0"
        if self.min_deviation < 0:
            return False, "min_deviation must be >= 0"
        return True, "OK"

    def _update_history(self, token_id: str, price: float) -> list[float]:
        hist = self._price_history.get(token_id)
        if hist is None:
            hist = deque(maxlen=MAX_LOOKBACK)
            self._price_history[token_id] = hist
        hist.append(price)
        return list(hist)

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

        window = price_list[-self.window:]
        ma = sum(window) / self.window
        variance = sum((p - ma) ** 2 for p in window) / self.window
        sigma = variance ** 0.5
        if sigma < 1e-6:
            return None  # zero-vol regime

        upper = ma + self.k_sigma * sigma
        lower = ma - self.k_sigma * sigma
        current = price_list[-1]
        deviation = current - ma
        if abs(deviation) < self.min_deviation:
            return None

        if current <= lower:
            action = "BUY"
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"Bollinger BUY: p={current:.4f} ≤ lower={lower:.4f} "
                f"(MA={ma:.4f}, σ={sigma:.4f})"
            )
        elif current >= upper:
            action = "SELL"
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"Bollinger SELL: p={current:.4f} ≥ upper={upper:.4f} "
                f"(MA={ma:.4f}, σ={sigma:.4f})"
            )
        else:
            return None

        # Edge = expected deviation to revert to MA, capped at the band width.
        edge = abs(deviation) / max(sigma, 0.01) * 0.01
        confidence = min(0.9, abs(deviation) / (self.k_sigma * sigma))

        self._update_history(token_id, current)
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
                "model": "bollinger_bands_reversion",
                "upper_band": upper,
                "lower_band": lower,
                "ma": ma,
                "sigma": sigma,
                "k_sigma": self.k_sigma,
                "current_price": current,
                "deviation": deviation,
                "window": self.window,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Size scales with |deviation/σ| — bigger dislocation = bigger bet.
        deviation_sigma = abs(signal.metadata.get("deviation", 0.0)) / max(
            signal.metadata.get("sigma", 0.01), 0.001
        )
        size_factor = min(2.0, max(0.5, deviation_sigma / 2.0))
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
                "model": "bollinger_bands_reversion",
                "ma": signal.metadata.get("ma"),
                "sigma": signal.metadata.get("sigma"),
                "upper_band": signal.metadata.get("upper_band"),
                "lower_band": signal.metadata.get("lower_band"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when price reverts to the MA (mean reversion complete)."""
        if not position:
            return None
        entry_action = position.get("entry_action", "BUY")
        current_price = float(market_context.get("current_price", 0.0))
        ma = float(market_context.get("ma", 0.5))
        # Exit when price has reverted back to within 0.5σ of MA.
        sigma = float(market_context.get("sigma", 0.02))
        if abs(current_price - ma) < 0.5 * sigma:
            return {
                "reason": "price reverted to MA — take profit",
                "current_price": current_price,
                "ma": ma,
                "type": "limit",
                "price": round(current_price, 4),
            }
        # Stop-loss: price kept dislocating beyond entry by 1σ.
        entry_price = float(position.get("entry_price", 0.5))
        stop_distance = max(0.02, sigma)
        if (entry_action == "BUY" and current_price < entry_price - stop_distance) or \
           (entry_action == "SELL" and current_price > entry_price + stop_distance):
            return {
                "reason": "stop-loss hit — dislocation widened",
                "entry_price": entry_price,
                "current_price": current_price,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "window": self.window,
            "k_sigma": self.k_sigma,
            "min_deviation": self.min_deviation,
            "tracked_tokens": len(self._price_history),
        })
        return base
