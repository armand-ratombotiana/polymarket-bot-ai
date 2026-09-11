"""
strategies/event_whale_follower.py — Whale Block Order Follower.

W45-1 — implements the unified strategy contract for the
``event_whale_follower`` catalog entry.

Signal logic
------------
Detects institutional block orders (single fills > $5,000) and rides
the market impact for 1-3 cycles. The premise: large informed flow
typically persists in the same direction for several cycles before
the market absorbs it.

  * Whale BUY (large aggressive buy) → follow with BUY
  * Whale SELL (large aggressive sell) → follow with SELL

A "whale" is defined as a single fill ≥ min_whale_usdc AND
≥ whale_size_multiplier × median recent fill size.

Edge = expected continuation × whale_persistence_probability.
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_WHALE_USDC = 5000.0          # $5K minimum to qualify as whale
WHALE_SIZE_MULTIPLIER = 5.0       # 5× median recent fill size
RECENT_FILLS_WINDOW = 50          # 50-fill window for median
MAX_FOLLOW_CYCLES = 3             # follow for 3 cycles max
SCAN_INTERVAL = 5.0               # 5-second polling for fast detection


class WhaleFollower(BaseStrategy):
    """Whale block order follower."""

    name = "event_whale_follower"

    def __init__(self) -> None:
        super().__init__()
        self.min_whale_usdc: float = MIN_WHALE_USDC
        self.whale_size_multiplier: float = WHALE_SIZE_MULTIPLIER
        self.recent_fills_window: int = RECENT_FILLS_WINDOW
        self.max_follow_cycles: int = MAX_FOLLOW_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._recent_fills: dict[str, deque] = {}
        self._last_whale: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[event_whale] Active (min_whale=$%.0f, multiplier=%.1f×)",
            self.min_whale_usdc, self.whale_size_multiplier,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_whale] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Whale block order follower — detects institutional block "
                "orders (>$5K) and rides market impact over short horizons."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "whale_follower",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_whale_usdc", "whale_size_multiplier"):
            if k in config:
                setattr(self, k, float(config[k]))
        for k in ("recent_fills_window", "max_follow_cycles"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_whale_usdc <= 0:
            return False, "min_whale_usdc must be > 0"
        if self.whale_size_multiplier < 2.0:
            return False, "whale_size_multiplier must be >= 2"
        if self.recent_fills_window < 10:
            return False, "recent_fills_window must be >= 10"
        if self.max_follow_cycles < 1:
            return False, "max_follow_cycles must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        recent_fills = market_context.get("recent_fills")
        if not token_id or not recent_fills or not isinstance(recent_fills, list):
            return None
        if len(recent_fills) < 2:
            return None

        # Each fill: {"size_usdc": float, "side": "BUY"/"SELL", "price": float}
        try:
            fills = [
                {
                    "size_usdc": float(f.get("size_usdc", 0.0)),
                    "side": str(f.get("side", "")),
                    "price": float(f.get("price", 0.5)),
                }
                for f in recent_fills
            ]
        except (TypeError, ValueError):
            return None

        # Track recent fills for median calculation.
        history = self._recent_fills.setdefault(token_id, deque(maxlen=self.recent_fills_window))
        for f in fills[-self.recent_fills_window:]:
            history.append(f)
        if len(history) < 5:
            return None

        # Median fill size.
        sizes = sorted([f["size_usdc"] for f in history])
        median_size = sizes[len(sizes) // 2]
        if median_size <= 0:
            return None

        # Latest fill is the candidate whale.
        latest = fills[-1]
        if latest["size_usdc"] < self.min_whale_usdc:
            return None
        if latest["size_usdc"] < self.whale_size_multiplier * median_size:
            return None

        # Side: follow the whale's direction.
        if latest["side"] == "BUY":
            action = "BUY"
            target_price = round(min(latest["price"] + 0.005, 0.98), 4)
            reason = (
                f"Whale BUY: size=${latest['size_usdc']:.0f} vs median=${median_size:.0f} "
                f"({latest['size_usdc'] / median_size:.1f}×) at {latest['price']:.4f}"
            )
        elif latest["side"] == "SELL":
            action = "SELL"
            target_price = round(max(latest["price"] - 0.005, 0.02), 4)
            reason = (
                f"Whale SELL: size=${latest['size_usdc']:.0f} vs median=${median_size:.0f} "
                f"({latest['size_usdc'] / median_size:.1f}×) at {latest['price']:.4f}"
            )
        else:
            return None

        # Track the whale so we can detect continuation.
        self._last_whale[token_id] = {
            "size_usdc": latest["size_usdc"],
            "side": latest["side"],
            "price": latest["price"],
            "median_size": median_size,
        }

        # Edge = expected continuation × whale persistence probability.
        size_ratio = latest["size_usdc"] / median_size
        edge = min(size_ratio / 50.0, 0.03)
        confidence = min(0.85, 0.4 + size_ratio / 30.0)

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
                "model": "whale_follower",
                "whale_size_usdc": latest["size_usdc"],
                "median_fill_size": median_size,
                "size_ratio": size_ratio,
                "whale_side": latest["side"],
                "whale_price": latest["price"],
                "max_follow_cycles": self.max_follow_cycles,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        size_ratio = float(signal.metadata.get("size_ratio", 1.0))
        size_factor = min(2.5, size_ratio / 3.0)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no whale signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "IOC",  # follow fast or skip
            "post_only": False,
            "metadata": {
                "model": "whale_follower",
                "whale_size_usdc": signal.metadata.get("whale_size_usdc"),
                "size_ratio": signal.metadata.get("size_ratio"),
                "max_follow_cycles": signal.metadata.get("max_follow_cycles"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit after max_follow_cycles OR when whale activity subsides."""
        if not position:
            return None
        cycles_held = int(position.get("cycles_held", 0))
        if cycles_held >= self.max_follow_cycles:
            return {
                "reason": "max follow cycles elapsed — exit",
                "cycles_held": cycles_held,
                "type": "market",
            }
        # Whale flow reversed direction — exit immediately.
        entry_side = position.get("entry_action", "BUY")
        current_whale_side = market_context.get("current_whale_side", "")
        if current_whale_side and current_whale_side != entry_side:
            return {
                "reason": "whale flow reversed — exit",
                "entry_side": entry_side,
                "current_whale_side": current_whale_side,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_whale_usdc": self.min_whale_usdc,
            "whale_size_multiplier": self.whale_size_multiplier,
            "max_follow_cycles": self.max_follow_cycles,
            "tracked_tokens": len(self._recent_fills),
        })
        return base
