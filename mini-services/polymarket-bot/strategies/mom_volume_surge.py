"""
strategies/mom_volume_surge.py — Volume Surge Momentum Trader.

W45-1 — implements the unified strategy contract for the
``mom_volume_surge`` catalog entry.

Signal logic
------------
Follows sudden 3× volume spikes with directional price breakout:
  * Surge = current_volume > 3× avg_volume(N)
  * Direction = sign(price change over surge period)
  * BUY when surge + price up; SELL when surge + price down

The premise: institutional block orders / news catalysts often
manifest as sudden volume spikes, and the price direction during
the spike typically persists for 1-3 cycles.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

VOLUME_WINDOW = 20                # 20-cycle volume baseline
SURGE_MULTIPLIER = 3.0            # current_vol > 3× avg triggers
MIN_PRICE_CHANGE = 0.01          # 1% price move required for direction
MAX_HOLD_CYCLES = 5              # short-term momentum trade
SCAN_INTERVAL = 15.0


class VolumeSurgeMomentum(BaseStrategy):
    """Volume-spike momentum trader."""

    name = "mom_volume_surge"

    def __init__(self) -> None:
        super().__init__()
        self.volume_window: int = VOLUME_WINDOW
        self.surge_multiplier: float = SURGE_MULTIPLIER
        self.min_price_change: float = MIN_PRICE_CHANGE
        self.max_hold_cycles: int = MAX_HOLD_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._volume_history: dict[str, list[float]] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_vol_surge] Active (window=%d, surge=%.1f×)",
            self.volume_window, self.surge_multiplier,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_vol_surge] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Volume surge momentum trader — follows sudden 3× volume "
                "spikes with directional price breakouts over short horizons."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "volume_surge",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("volume_window", "max_hold_cycles"):
            if k in config:
                setattr(self, k, int(config[k]))
        for k in ("surge_multiplier", "min_price_change"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.volume_window < 5:
            return False, "volume_window must be >= 5"
        if self.surge_multiplier < 1.5:
            return False, "surge_multiplier must be >= 1.5"
        if self.min_price_change <= 0:
            return False, "min_price_change must be > 0"
        if self.max_hold_cycles < 1:
            return False, "max_hold_cycles must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        volumes = market_context.get("volumes")
        prices = market_context.get("prices")
        if not token_id or not volumes or not prices:
            return None
        if not isinstance(volumes, list) or not isinstance(prices, list):
            return None
        if len(volumes) != len(prices):
            return None
        try:
            v_list = [float(v) for v in volumes]
            p_list = [float(p) for p in prices]
        except (TypeError, ValueError):
            return None
        if len(v_list) < self.volume_window + 1:
            return None

        # Track volume history for diagnostics.
        history = self._volume_history.setdefault(token_id, [])
        history.append(v_list[-1])
        if len(history) > 100:
            history.pop(0)

        # Baseline volume (avg of prior N cycles, excluding current).
        baseline = sum(v_list[-(self.volume_window + 1):-1]) / self.volume_window
        if baseline <= 0:
            return None

        current_vol = v_list[-1]
        surge_ratio = current_vol / baseline
        if surge_ratio < self.surge_multiplier:
            return None

        # Direction = sign of price change over the surge cycle.
        prev_price = p_list[-2]
        current_price = p_list[-1]
        price_change = current_price - prev_price
        if abs(price_change) < self.min_price_change:
            return None  # volume spike but no clear direction

        if price_change > 0:
            action = "BUY"
            target_price = round(min(current_price + 0.005, 0.98), 4)
            reason = (
                f"VolSurge BUY: vol={current_vol:.0f} vs avg={baseline:.0f} "
                f"({surge_ratio:.1f}×); price {prev_price:.4f}→{current_price:.4f}"
            )
        else:
            action = "SELL"
            target_price = round(max(current_price - 0.005, 0.02), 4)
            reason = (
                f"VolSurge SELL: vol={current_vol:.0f} vs avg={baseline:.0f} "
                f"({surge_ratio:.1f}×); price {prev_price:.4f}→{current_price:.4f}"
            )

        # Edge = expected momentum persistence × surge magnitude.
        edge = min(surge_ratio / 10.0, 0.05)
        confidence = min(0.85, 0.4 + surge_ratio / 15.0 + abs(price_change) * 5)

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
                "model": "volume_surge",
                "surge_ratio": surge_ratio,
                "baseline_volume": baseline,
                "current_volume": current_vol,
                "price_change": price_change,
                "current_price": current_price,
                "max_hold_cycles": self.max_hold_cycles,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        surge = float(signal.metadata.get("surge_ratio", 1.0))
        size_factor = min(3.0, surge / 1.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no surge signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "IOC",  # momentum — fill fast or skip
            "post_only": False,
            "metadata": {
                "model": "volume_surge",
                "surge_ratio": signal.metadata.get("surge_ratio"),
                "max_hold_cycles": signal.metadata.get("max_hold_cycles"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit after max_hold_cycles OR when volume collapses."""
        if not position:
            return None
        cycles_held = int(position.get("cycles_held", 0))
        if cycles_held >= self.max_hold_cycles:
            return {
                "reason": "max hold cycles elapsed — exit",
                "cycles_held": cycles_held,
                "type": "market",
            }
        # Volume collapsed below baseline — momentum exhausted.
        current_vol = float(market_context.get("current_volume", 0.0))
        baseline_vol = float(position.get("baseline_volume", current_vol))
        if baseline_vol > 0 and current_vol < baseline_vol * 0.5:
            return {
                "reason": "volume collapsed — momentum exhausted",
                "current_volume": current_vol,
                "baseline_volume": baseline_vol,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "volume_window": self.volume_window,
            "surge_multiplier": self.surge_multiplier,
            "max_hold_cycles": self.max_hold_cycles,
            "tracked_tokens": len(self._volume_history),
        })
        return base
