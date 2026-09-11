"""
strategies/mom_volatility_expansion.py — Volatility Expansion Trend Trader.

W45-1 — implements the unified strategy contract for the
``mom_volatility_expansion`` catalog entry.

Signal logic
------------
ATR-based volatility squeeze breakout: when ATR compresses below a
historical threshold (low-vol "squeeze"), the market is coiling for
a directional expansion. The strategy enters when:
  * ATR compressed (squeeze) for ≥3 cycles
  * Price breaks out of the squeeze range (high/low of squeeze window)
  * Direction confirmed by ATR expansion (current ATR > 1.5 × squeeze ATR)

Edge = expected expansion magnitude × breakout persistence probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

ATR_PERIOD = 14
SQUEEZE_LOOKBACK = 20            # 20 cycles to compute historical ATR baseline
SQUEEZE_RATIO = 0.6              # ATR < 60% of historical ATR ⇒ squeeze
EXPANSION_RATIO = 1.5            # ATR must expand to 1.5× squeeze ATR
MIN_SQUEEZE_CYCLES = 3
SCAN_INTERVAL = 30.0


class VolatilityExpansionTrader(BaseStrategy):
    """ATR volatility-squeeze breakout trader."""

    name = "mom_volatility_expansion"

    def __init__(self) -> None:
        super().__init__()
        self.atr_period: int = ATR_PERIOD
        self.squeeze_lookback: int = SQUEEZE_LOOKBACK
        self.squeeze_ratio: float = SQUEEZE_RATIO
        self.expansion_ratio: float = EXPANSION_RATIO
        self.min_squeeze_cycles: int = MIN_SQUEEZE_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._squeeze_state: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_vol_exp] Active (atr=%d, squeeze=%.2f, expand=%.2f)",
            self.atr_period, self.squeeze_ratio, self.expansion_ratio,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_vol_exp] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Volatility expansion trend trader — enters explosive trend "
                "regimes following ATR volatility squeeze breakouts."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "volatility_expansion",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("atr_period", "squeeze_lookback", "min_squeeze_cycles"):
            if k in config:
                setattr(self, k, int(config[k]))
        for k in ("squeeze_ratio", "expansion_ratio"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.atr_period < 5:
            return False, "atr_period must be >= 5"
        if self.squeeze_lookback < self.atr_period:
            return False, "squeeze_lookback must be >= atr_period"
        if not 0 < self.squeeze_ratio < 1:
            return False, "squeeze_ratio must be in (0, 1)"
        if self.expansion_ratio <= 1.0:
            return False, "expansion_ratio must be > 1"
        if self.min_squeeze_cycles < 1:
            return False, "min_squeeze_cycles must be >= 1"
        return True, "OK"

    def _compute_atr(self, prices: list[float], period: int) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        trs = []
        for i in range(1, len(prices)):
            hi, lo = max(prices[i], prices[i - 1]), min(prices[i], prices[i - 1])
            trs.append(abs(hi - lo))
        if len(trs) < period:
            return None
        # Wilder smoothing.
        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return atr

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        if not token_id or not prices or not isinstance(prices, list):
            return None
        try:
            price_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(price_list) < self.atr_period + self.squeeze_lookback:
            return None

        # Current ATR vs historical ATR baseline.
        current_atr = self._compute_atr(price_list[-self.atr_period - 5:], self.atr_period)
        hist_window = price_list[-(self.atr_period + self.squeeze_lookback):-self.squeeze_lookback]
        hist_atr = self._compute_atr(hist_window + price_list[-self.atr_period - 5:-self.squeeze_lookback] if hist_window else price_list[-self.atr_period - 5:], self.atr_period)
        if current_atr is None or hist_atr is None or hist_atr <= 0:
            return None

        # Track squeeze state per token.
        state = self._squeeze_state.setdefault(token_id, {"squeeze_cycles": 0, "squeeze_atr": 0.0, "squeeze_range": (0.0, 1.0)})
        in_squeeze = current_atr < hist_atr * self.squeeze_ratio
        if in_squeeze:
            state["squeeze_cycles"] += 1
            state["squeeze_atr"] = current_atr
            # Track the squeeze range (high/low during squeeze).
            recent = price_list[-self.atr_period:]
            state["squeeze_range"] = (min(recent), max(recent))
            return None  # still in squeeze, no signal yet

        # Not in squeeze — check for expansion after squeeze.
        if state["squeeze_cycles"] < self.min_squeeze_cycles:
            state["squeeze_cycles"] = 0
            return None

        # Expansion: current ATR must exceed squeeze ATR × expansion_ratio.
        if current_atr < state["squeeze_atr"] * self.expansion_ratio:
            return None

        # Breakout direction: current price vs squeeze range.
        current_price = price_list[-1]
        squeeze_low, squeeze_high = state["squeeze_range"]
        if current_price > squeeze_high:
            action = "BUY"
            target_price = round(min(current_price + 0.005, 0.98), 4)
            reason = (
                f"VolExp BUY: ATR expanded from {state['squeeze_atr']:.4f} to "
                f"{current_atr:.4f} ({current_atr / state['squeeze_atr']:.2f}×); "
                f"price broke above squeeze high {squeeze_high:.4f}"
            )
        elif current_price < squeeze_low:
            action = "SELL"
            target_price = round(max(current_price - 0.005, 0.02), 4)
            reason = (
                f"VolExp SELL: ATR expanded from {state['squeeze_atr']:.4f} to "
                f"{current_atr:.4f} ({current_atr / state['squeeze_atr']:.2f}×); "
                f"price broke below squeeze low {squeeze_low:.4f}"
            )
        else:
            return None

        # Edge = expected expansion magnitude (squeeze ATR × expansion factor).
        edge = min(current_atr * 0.5, 0.05)
        confidence = min(0.85, 0.5 + (current_atr / max(state["squeeze_atr"], 1e-6) - 1) * 0.3)

        # Reset squeeze state — we just consumed it.
        state["squeeze_cycles"] = 0
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
                "model": "volatility_expansion",
                "current_atr": current_atr,
                "hist_atr": hist_atr,
                "squeeze_atr": state["squeeze_atr"],
                "squeeze_cycles": state["squeeze_cycles"],
                "squeeze_range": state["squeeze_range"],
                "expansion_ratio": current_atr / max(state["squeeze_atr"], 1e-6),
                "current_price": current_price,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        expansion_ratio = float(signal.metadata.get("expansion_ratio", 1.5))
        size_factor = min(2.5, expansion_ratio - 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.5))
        max_pct = float(risk_params.get("max_position_pct", 0.05))
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
                "model": "volatility_expansion",
                "current_atr": signal.metadata.get("current_atr"),
                "expansion_ratio": signal.metadata.get("expansion_ratio"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when ATR collapses back (expansion over) or stop-loss hit."""
        if not position:
            return None
        current_atr = float(market_context.get("current_atr", 0.0))
        entry_atr = float(position.get("entry_atr", 0.01))
        # Exit when ATR compresses back below 80% of entry ATR.
        if entry_atr > 0 and current_atr < entry_atr * 0.8:
            return {
                "reason": "ATR collapsed — expansion over",
                "entry_atr": entry_atr,
                "current_atr": current_atr,
                "type": "market",
            }
        # Stop-loss: price moved 2× entry ATR against position.
        entry_action = position.get("entry_action", "BUY")
        entry_price = float(position.get("entry_price", 0.5))
        current_price = float(market_context.get("current_price", entry_price))
        stop_distance = 2.0 * entry_atr
        if entry_action == "BUY" and current_price < entry_price - stop_distance:
            return {
                "reason": "stop-loss — price moved against position",
                "entry_price": entry_price,
                "current_price": current_price,
                "type": "market",
            }
        if entry_action == "SELL" and current_price > entry_price + stop_distance:
            return {
                "reason": "stop-loss — price moved against position",
                "entry_price": entry_price,
                "current_price": current_price,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "atr_period": self.atr_period,
            "squeeze_lookback": self.squeeze_lookback,
            "squeeze_ratio": self.squeeze_ratio,
            "expansion_ratio": self.expansion_ratio,
            "tracked_tokens": len(self._squeeze_state),
        })
        return base
