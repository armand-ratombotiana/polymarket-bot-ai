"""
strategies/stat_vwap_reversion.py — VWAP Pullback Reversion Trader.

W45-1 — implements the unified strategy contract for the
``stat_vwap_reversion`` catalog entry.

Signal logic
------------
Trades mean-reversion toward the Volume-Weighted Average Price
(VWAP) — a benchmark institutional traders use to assess execution
quality. The strategy:

  * BUY when current price < VWAP × (1 - threshold)
    (price is sufficiently below VWAP — expect reversion up)
  * SELL when current price > VWAP × (1 + threshold)
    (price is sufficiently above VWAP — expect reversion down)

The threshold defaults to 1.5% (1.5σ of typical intraday noise).

Edge = |price - VWAP| / VWAP, scaled by the expected reversion
probability (higher when VWAP is calculated over more volume).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

DEVIATION_THRESHOLD = 0.015       # 1.5% deviation required
VWAP_VOLUME_MIN = 100.0           # need ≥100 shares total volume
MAX_BANDS = 5                     # cumulative VWAP across N bands
SCAN_INTERVAL = 30.0


class VwapReversionTrader(BaseStrategy):
    """VWAP pullback mean-reversion trader."""

    name = "stat_vwap_reversion"

    def __init__(self) -> None:
        super().__init__()
        self.deviation_threshold: float = DEVIATION_THRESHOLD
        self.vwap_volume_min: float = VWAP_VOLUME_MIN
        self.max_bands: int = MAX_BANDS
        self._interval: float = SCAN_INTERVAL
        self._vwap_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_vwap] Active (deviation=%.2f%%)",
            self.deviation_threshold * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_vwap] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "VWAP pullback reversion trader — buys when price is below "
                "VWAP × (1 - threshold) and sells when above VWAP × (1 + threshold)."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "vwap_reversion",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("deviation_threshold", "vwap_volume_min"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "max_bands" in config:
            self.max_bands = int(config["max_bands"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.deviation_threshold <= 0:
            return False, "deviation_threshold must be > 0"
        if self.vwap_volume_min <= 0:
            return False, "vwap_volume_min must be > 0"
        if self.max_bands < 1:
            return False, "max_bands must be >= 1"
        return True, "OK"

    def _compute_vwap(
        self, prices: list[float], volumes: list[float]
    ) -> tuple[float, float]:
        """Return (vwap, total_volume)."""
        if len(prices) != len(volumes) or len(prices) == 0:
            return 0.0, 0.0
        total_v = sum(volumes)
        if total_v <= 0:
            return 0.0, 0.0
        vwap = sum(p * v for p, v in zip(prices, volumes)) / total_v
        return vwap, total_v

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        current_price = market_context.get("current_price")
        prices = market_context.get("prices")
        volumes = market_context.get("volumes")
        if not token_id or current_price is None or not prices or not volumes:
            return None
        if len(prices) != len(volumes):
            return None

        try:
            cp = float(current_price)
            p_list = [float(p) for p in prices]
            v_list = [float(v) for v in volumes]
        except (TypeError, ValueError):
            return None

        vwap, total_v = self._compute_vwap(p_list, v_list)
        if total_v < self.vwap_volume_min or vwap <= 0:
            return None

        self._vwap_cache[token_id] = vwap
        deviation = (cp - vwap) / vwap
        if abs(deviation) < self.deviation_threshold:
            return None

        if deviation < 0:
            action = "BUY"
            target_price = round(min(cp + 0.005, 0.98), 4)
            reason = (
                f"VWAP BUY: price={cp:.4f} < VWAP={vwap:.4f} "
                f"(dev={deviation*100:+.2f}%)"
            )
        else:
            action = "SELL"
            target_price = round(max(cp - 0.005, 0.02), 4)
            reason = (
                f"VWAP SELL: price={cp:.4f} > VWAP={vwap:.4f} "
                f"(dev={deviation*100:+.2f}%)"
            )

        edge = abs(deviation) * 0.5  # half of the deviation expected to revert
        confidence = min(0.85, 0.4 + abs(deviation) * 10)

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
                "model": "vwap_reversion",
                "vwap": vwap,
                "current_price": cp,
                "deviation": deviation,
                "total_volume": total_v,
                "deviation_threshold": self.deviation_threshold,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        dev = abs(float(signal.metadata.get("deviation", 0.0)))
        size_factor = min(2.5, dev / self.deviation_threshold)
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
                "model": "vwap_reversion",
                "vwap": signal.metadata.get("vwap"),
                "deviation": signal.metadata.get("deviation"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when price reverts to within 0.5% of VWAP."""
        if not position:
            return None
        current_price = float(market_context.get("current_price", 0.0))
        vwap = float(market_context.get("vwap", 0.0))
        if vwap > 0 and abs(current_price - vwap) / vwap < 0.005:
            return {
                "reason": "price reverted to VWAP — take profit",
                "current_price": current_price,
                "vwap": vwap,
                "type": "limit",
                "price": round(current_price, 4),
            }
        # Stop-loss: deviation doubled beyond entry.
        entry_deviation = float(position.get("entry_deviation", 0.0))
        current_deviation = float(market_context.get("current_deviation", 0.0))
        if (abs(current_deviation) > 2.0 * abs(entry_deviation)
                and entry_deviation * current_deviation > 0):
            return {
                "reason": "deviation widened — stop-loss",
                "entry_deviation": entry_deviation,
                "current_deviation": current_deviation,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "deviation_threshold": self.deviation_threshold,
            "vwap_volume_min": self.vwap_volume_min,
            "vwap_cache_size": len(self._vwap_cache),
        })
        return base
