"""
strategies/event_election_momentum.py — Election Momentum Tracker.

W45-1 — implements the unified strategy contract for the
``event_election_momentum`` catalog entry.

Signal logic
------------
Tracks polling momentum shifts in political & election markets:

  * Rolling polling average over N days
  * Momentum = slope of recent polls (linear regression)
  * BUY when momentum is strongly positive AND market underprices
  * SELL when momentum is strongly negative AND market overprices

The trade horizon is medium-term (days to weeks) — polls shift
slowly but persistently when momentum is established.

Edge = expected polling correction × momentum_persistence_probability.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MOMENTUM_WINDOW = 14              # 14-day polling lookback
MOMENTUM_THRESHOLD = 0.005       # 0.5%/day polling slope required
MIN_MARKET_GAP = 0.03            # 3% market vs poll gap required
MAX_HOLD_DAYS = 30
SCAN_INTERVAL = 3600.0           # 1-hour polling


class ElectionMomentumTracker(BaseStrategy):
    """Election polling momentum trader."""

    name = "event_election_momentum"

    def __init__(self) -> None:
        super().__init__()
        self.momentum_window: int = MOMENTUM_WINDOW
        self.momentum_threshold: float = MOMENTUM_THRESHOLD
        self.min_market_gap: float = MIN_MARKET_GAP
        self.max_hold_days: int = MAX_HOLD_DAYS
        self._interval: float = SCAN_INTERVAL
        self._momentum_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[event_election] Active (window=%d days, threshold=%.3f/day)",
            self.momentum_window, self.momentum_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_election] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Election momentum tracker — trades polling momentum shifts "
                "in political markets using linear regression slope on "
                "rolling poll averages."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "election_momentum",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("momentum_window", "max_hold_days"):
            if k in config:
                setattr(self, k, int(config[k]))
        for k in ("momentum_threshold", "min_market_gap"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.momentum_window < 5:
            return False, "momentum_window must be >= 5"
        if self.momentum_threshold <= 0:
            return False, "momentum_threshold must be > 0"
        if self.min_market_gap <= 0:
            return False, "min_market_gap must be > 0"
        if self.max_hold_days < 1:
            return False, "max_hold_days must be >= 1"
        return True, "OK"

    def _compute_momentum(self, poll_history: list[float]) -> Optional[float]:
        """Linear regression slope of poll values vs day index."""
        n = len(poll_history)
        if n < self.momentum_window:
            return None
        window = poll_history[-self.momentum_window:]
        m = len(window)
        x_mean = (m - 1) / 2.0
        y_mean = sum(window) / m
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(window))
        den = sum((i - x_mean) ** 2 for i in range(m))
        if den <= 0:
            return None
        slope = num / den
        return slope  # change in poll value per day

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        poll_history = market_context.get("poll_history")
        market_price = market_context.get("market_price")
        if not token_id or not poll_history or market_price is None:
            return None
        if not isinstance(poll_history, list):
            return None
        try:
            polls = [float(p) for p in poll_history]
            mp = float(market_price)
        except (TypeError, ValueError):
            return None
        if len(polls) < self.momentum_window:
            return None
        if not 0 < mp < 1:
            return None

        momentum = self._compute_momentum(polls)
        if momentum is None:
            return None
        self._momentum_cache[token_id] = momentum

        if abs(momentum) < self.momentum_threshold:
            return None  # not enough momentum

        current_poll = polls[-1]
        market_gap = current_poll - mp
        if abs(market_gap) < self.min_market_gap:
            return None

        # Direction: positive momentum + market underpriced → BUY
        # Negative momentum + market overpriced → SELL
        if momentum > 0 and market_gap > 0:
            action = "BUY"
            target_price = round(min(mp + 0.01, 0.98), 4)
            reason = (
                f"ElectionMom BUY: momentum={momentum:+.4f}/day, "
                f"poll={current_poll:.3f} > market={mp:.3f} (gap={market_gap:+.3f})"
            )
        elif momentum < 0 and market_gap < 0:
            action = "SELL"
            target_price = round(max(mp - 0.01, 0.02), 4)
            reason = (
                f"ElectionMom SELL: momentum={momentum:+.4f}/day, "
                f"poll={current_poll:.3f} < market={mp:.3f} (gap={market_gap:+.3f})"
            )
        else:
            return None  # momentum and gap disagree — no clear trade

        # Edge = expected momentum persistence × market gap.
        edge = min(abs(momentum) * 5 + abs(market_gap) * 0.5, 0.05)
        confidence = min(0.85, 0.4 + abs(momentum) * 30 + abs(market_gap) * 3)

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
                "model": "election_momentum",
                "momentum": momentum,
                "current_poll": current_poll,
                "market_price": mp,
                "market_gap": market_gap,
                "max_hold_days": self.max_hold_days,
                "momentum_window": self.momentum_window,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        mom = abs(float(signal.metadata.get("momentum", 0.0)))
        gap = abs(float(signal.metadata.get("market_gap", 0.0)))
        size_factor = min(2.5, mom * 100 + gap * 5)
        base_size = float(risk_params.get("base_size_usdc", 5.0))
        max_pct = float(risk_params.get("max_position_pct", 0.06))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no momentum signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "election_momentum",
                "momentum": signal.metadata.get("momentum"),
                "market_gap": signal.metadata.get("market_gap"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when momentum stalls OR after max_hold_days."""
        if not position:
            return None
        days_held = int(position.get("days_held", 0))
        if days_held >= self.max_hold_days:
            return {
                "reason": "max hold days elapsed — exit",
                "days_held": days_held,
                "type": "market",
            }
        # Momentum reversed sign.
        entry_momentum = float(position.get("entry_momentum", 0.0))
        current_momentum = float(market_context.get("current_momentum", 0.0))
        if entry_momentum * current_momentum < 0:
            return {
                "reason": "momentum reversed — exit",
                "entry_momentum": entry_momentum,
                "current_momentum": current_momentum,
                "type": "market",
            }
        # Momentum decayed below threshold.
        if abs(current_momentum) < self.momentum_threshold * 0.5:
            return {
                "reason": "momentum decayed — exit",
                "current_momentum": current_momentum,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "momentum_window": self.momentum_window,
            "momentum_threshold": self.momentum_threshold,
            "max_hold_days": self.max_hold_days,
            "tracked_tokens": len(self._momentum_cache),
        })
        return base
