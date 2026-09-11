"""
strategies/mom_micro_price_accel.py — Micro-Price Acceleration Trader.

W45-1 — implements the unified strategy contract for the
``mom_micro_price_accel`` catalog entry.

Signal logic
------------
Micro-price (P_micro) is a volume-weighted mid-price that accounts
for order-book imbalance at the top of the book:

  P_micro = (best_bid × ask_size + best_ask × bid_size) / (bid_size + ask_size)

The strategy detects micro-price ACCELERATION (second derivative):
  * velocity = P_micro_t - P_micro_{t-1}
  * acceleration = velocity_t - velocity_{t-1}

When acceleration exceeds a threshold and velocity is in the same
direction, the strategy enters expecting the momentum to persist
for the next few cycles.

Edge = expected momentum × acceleration magnitude.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

ACCEL_THRESHOLD = 0.001          # 0.1% micro-price acceleration
MIN_VELOCITY = 0.0005            # 0.05% velocity required
MAX_HOLD_CYCLES = 3              # very short-term momentum
SCAN_INTERVAL = 2.0              # microstructure ⇒ fast polling


class MicroPriceAcceleration(BaseStrategy):
    """Micro-price acceleration momentum trader."""

    name = "mom_micro_price_accel"

    def __init__(self) -> None:
        super().__init__()
        self.accel_threshold: float = ACCEL_THRESHOLD
        self.min_velocity: float = MIN_VELOCITY
        self.max_hold_cycles: int = MAX_HOLD_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._micro_price_history: dict[str, deque] = {}

    async def _run(self) -> None:
        log.info(
            "[mom_micro_accel] Active (accel=%.4f%%, hold_max=%d cycles)",
            self.accel_threshold * 100, self.max_hold_cycles,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mom_micro_accel] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Micro-price acceleration trader — detects fast micro-price "
                "momentum (volume-weighted mid × book imbalance × acceleration)."
            ),
            "author": "polymarket-bot",
            "category": "momentum",
            "model": "micro_price_accel",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("accel_threshold", "min_velocity"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "max_hold_cycles" in config:
            self.max_hold_cycles = int(config["max_hold_cycles"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.accel_threshold <= 0:
            return False, "accel_threshold must be > 0"
        if self.min_velocity < 0:
            return False, "min_velocity must be >= 0"
        if self.max_hold_cycles < 1:
            return False, "max_hold_cycles must be >= 1"
        return True, "OK"

    def _compute_micro_price(self, best_bid: float, best_ask: float,
                              bid_size: float, ask_size: float) -> Optional[float]:
        """Volume-weighted micro-price."""
        total_size = bid_size + ask_size
        if total_size <= 0:
            return None
        return (best_bid * ask_size + best_ask * bid_size) / total_size

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        best_bid = market_context.get("best_bid")
        best_ask = market_context.get("best_ask")
        bid_size = market_context.get("bid_size")
        ask_size = market_context.get("ask_size")
        if not token_id or best_bid is None or best_ask is None:
            return None
        if bid_size is None or ask_size is None:
            return None

        try:
            bb = float(best_bid)
            ba = float(best_ask)
            bs = float(bid_size)
            asz = float(ask_size)
        except (TypeError, ValueError):
            return None
        if bb <= 0 or ba <= 0 or bb >= ba:
            return None

        micro_price = self._compute_micro_price(bb, ba, bs, asz)
        if micro_price is None:
            return None

        history = self._micro_price_history.setdefault(token_id, deque(maxlen=10))
        history.append(micro_price)
        if len(history) < 3:
            return None  # need ≥3 to compute acceleration

        # Compute velocity + acceleration.
        prices = list(history)
        v1 = prices[-2] - prices[-3]
        v2 = prices[-1] - prices[-2]
        accel = v2 - v1

        # Both velocity AND acceleration must be in the same direction.
        if abs(accel) < self.accel_threshold:
            return None
        if abs(v2) < self.min_velocity:
            return None
        if (accel > 0) != (v2 > 0):
            return None  # decelerating — skip

        if accel > 0:
            action = "BUY"
            target_price = round(min(micro_price + 0.003, 0.98), 4)
            reason = (
                f"MicroAccel BUY: P_micro={micro_price:.4f}, "
                f"v={v2:+.5f}, a={accel:+.5f}"
            )
        else:
            action = "SELL"
            target_price = round(max(micro_price - 0.003, 0.02), 4)
            reason = (
                f"MicroAccel SELL: P_micro={micro_price:.4f}, "
                f"v={v2:+.5f}, a={accel:+.5f}"
            )

        edge = min(abs(accel) * 5, 0.02)
        confidence = min(0.85, 0.5 + abs(accel) * 100)

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
                "model": "micro_price_accel",
                "micro_price": micro_price,
                "velocity": v2,
                "acceleration": accel,
                "best_bid": bb,
                "best_ask": ba,
                "bid_size": bs,
                "ask_size": asz,
                "max_hold_cycles": self.max_hold_cycles,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        accel = abs(float(signal.metadata.get("acceleration", 0.0)))
        size_factor = min(2.5, accel * 500 + 0.5)
        base_size = float(risk_params.get("base_size_usdc", 1.5))
        max_pct = float(risk_params.get("max_position_pct", 0.02))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no micro-accel signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "IOC",
            "post_only": False,  # need fast fills
            "metadata": {
                "model": "micro_price_accel",
                "micro_price": signal.metadata.get("micro_price"),
                "acceleration": signal.metadata.get("acceleration"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit after max_hold_cycles (very short-term) or on accel reversal."""
        if not position:
            return None
        cycles_held = int(position.get("cycles_held", 0))
        if cycles_held >= self.max_hold_cycles:
            return {
                "reason": "max hold cycles elapsed — exit",
                "cycles_held": cycles_held,
                "type": "market",
            }
        current_accel = float(market_context.get("current_acceleration", 0.0))
        entry_accel = float(position.get("entry_acceleration", 0.0))
        # Acceleration reversed sign — momentum stalled.
        if entry_accel * current_accel < 0:
            return {
                "reason": "acceleration reversed — exit",
                "entry_acceleration": entry_accel,
                "current_acceleration": current_accel,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "accel_threshold": self.accel_threshold,
            "min_velocity": self.min_velocity,
            "max_hold_cycles": self.max_hold_cycles,
            "tracked_tokens": len(self._micro_price_history),
        })
        return base
