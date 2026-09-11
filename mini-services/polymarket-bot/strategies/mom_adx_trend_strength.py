"""
strategies/mom_adx_trend_strength.py — ADX Trend Strength Trader.

W45-1 — implements the unified strategy contract for the
``mom_adx_trend_strength`` catalog entry.

Signal logic
------------
ADX (Average Directional Index) measures trend STRENGTH (not direction):
  * ADX > 25 ⇒ strong trend
  * ADX < 20 ⇒ no trend (chop / range)

Direction is determined by +DI vs -DI:
  * +DI > -DI ⇒ bullish trend
  * -DI > +DI ⇒ bearish trend

The strategy trades only when ADX > 25 (strong trend regime):
  * +DI > -DI and ADX > 25 → BUY
  * -DI > +DI and ADX > 25 → SELL

Edge = expected trend continuation × ADX strength.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

ADX_PERIOD = 14
ADX_THRESHOLD = 25.0             # ADX > 25 = strong trend
MIN_HISTORY = 30
SCAN_INTERVAL = 30.0


class AdxTrendStrength(BaseStrategy):
    """ADX > 25 trend strength filter trader."""

    name = "mom_adx_trend_strength"

    def __init__(self) -> None:
        super().__init__()
        self.adx_period: int = ADX_PERIOD
        self.adx_threshold: float = ADX_THRESHOLD
        self.min_history: int = MIN_HISTORY
        self._interval: float = SCAN_INTERVAL
        self._adx_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_adx] Active (period=%d, threshold=%.0f)",
            self.adx_period, self.adx_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_adx] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "ADX trend strength trader — enters only when ADX > 25 "
                "indicating a strong directional trend; direction from +DI/-DI."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "adx_trend_strength",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("adx_period", "min_history"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "adx_threshold" in config:
            self.adx_threshold = float(config["adx_threshold"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.adx_period < 5:
            return False, "adx_period must be >= 5"
        if not 0 < self.adx_threshold <= 50:
            return False, "adx_threshold must be in (0, 50]"
        if self.min_history < self.adx_period * 2:
            return False, "min_history must be >= 2 * adx_period"
        return True, "OK"

    def _compute_adx(self, prices: list[float]) -> Optional[tuple[float, float, float]]:
        """Compute (ADX, +DI, -DI) using Wilder's method on price series."""
        if len(prices) < self.adx_period * 2:
            return None
        # Approximate high/low from consecutive prices.
        highs = [max(prices[i], prices[i - 1]) for i in range(1, len(prices))]
        lows = [min(prices[i], prices[i - 1]) for i in range(1, len(prices))]
        closes = prices[1:]

        # Compute +DM and -DM per cycle.
        plus_dm, minus_dm, trs = [], [], []
        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            pdm = up_move if (up_move > down_move and up_move > 0) else 0.0
            mdm = down_move if (down_move > up_move and down_move > 0) else 0.0
            plus_dm.append(pdm)
            minus_dm.append(mdm)
            tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
            trs.append(tr)

        if len(trs) < self.adx_period:
            return None

        # Wilder smoothing for +DM, -DM, TR.
        def wilder(values: list[float], period: int) -> list[float]:
            if len(values) < period:
                return []
            smoothed = [sum(values[:period])]
            for v in values[period:]:
                smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
            return smoothed

        plus_dm_s = wilder(plus_dm, self.adx_period)
        minus_dm_s = wilder(minus_dm, self.adx_period)
        tr_s = wilder(trs, self.adx_period)
        if not plus_dm_s or not minus_dm_s or not tr_s:
            return None

        # +DI = 100 * +DM_smooth / TR_smooth
        plus_di = 100.0 * plus_dm_s[-1] / max(tr_s[-1], 1e-9)
        minus_di = 100.0 * minus_dm_s[-1] / max(tr_s[-1], 1e-9)

        # DX = |+DI - -DI| / (+DI + -DI) * 100
        di_sum = plus_di + minus_di
        dx = abs(plus_di - minus_di) / max(di_sum, 1e-9) * 100.0

        # ADX = Wilder-smoothed average of DX over `period` cycles.
        # We use DX values from the smoothed DM/TR series — simplify by
        # using the current DX as a proxy for ADX in the short series case.
        adx = dx  # approximation; full ADX would require N DX values

        return adx, plus_di, minus_di

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

        result = self._compute_adx(price_list)
        if result is None:
            return None
        adx, plus_di, minus_di = result
        self._adx_cache[token_id] = adx

        if adx < self.adx_threshold:
            return None  # weak trend regime — don't trade

        # Direction from +DI vs -DI.
        if plus_di > minus_di:
            action = "BUY"
            target_price = round(min(price_list[-1] + 0.005, 0.98), 4)
            reason = (
                f"ADX BUY: ADX={adx:.1f} > {self.adx_threshold}, "
                f"+DI={plus_di:.1f} > -DI={minus_di:.1f}"
            )
        elif minus_di > plus_di:
            action = "SELL"
            target_price = round(max(price_list[-1] - 0.005, 0.02), 4)
            reason = (
                f"ADX SELL: ADX={adx:.1f} > {self.adx_threshold}, "
                f"-DI={minus_di:.1f} > +DI={plus_di:.1f}"
            )
        else:
            return None

        # Edge = expected trend continuation × ADX strength.
        edge = min((adx - self.adx_threshold) / 100.0, 0.05)
        confidence = min(0.85, 0.4 + (adx - self.adx_threshold) / 50.0)

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
                "model": "adx_trend_strength",
                "adx": adx,
                "plus_di": plus_di,
                "minus_di": minus_di,
                "adx_threshold": self.adx_threshold,
                "current_price": price_list[-1],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        adx = float(signal.metadata.get("adx", self.adx_threshold))
        size_factor = min(2.5, (adx - self.adx_threshold) / 10.0 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no ADX signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "adx_trend_strength",
                "adx": signal.metadata.get("adx"),
                "plus_di": signal.metadata.get("plus_di"),
                "minus_di": signal.metadata.get("minus_di"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when ADX drops below 20 (trend exhausted) or DI flips."""
        if not position:
            return None
        current_adx = float(market_context.get("current_adx", 0.0))
        if current_adx < 20.0:
            return {
                "reason": "ADX dropped below 20 — trend exhausted",
                "current_adx": current_adx,
                "type": "market",
            }
        entry_action = position.get("entry_action", "BUY")
        current_plus_di = float(market_context.get("current_plus_di", 0.0))
        current_minus_di = float(market_context.get("current_minus_di", 0.0))
        if entry_action == "BUY" and current_minus_di > current_plus_di:
            return {
                "reason": "DI flipped — trend direction reversed",
                "current_plus_di": current_plus_di,
                "current_minus_di": current_minus_di,
                "type": "market",
            }
        if entry_action == "SELL" and current_plus_di > current_minus_di:
            return {
                "reason": "DI flipped — trend direction reversed",
                "current_plus_di": current_plus_di,
                "current_minus_di": current_minus_di,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "adx_period": self.adx_period,
            "adx_threshold": self.adx_threshold,
            "tracked_tokens": len(self._adx_cache),
        })
        return base
