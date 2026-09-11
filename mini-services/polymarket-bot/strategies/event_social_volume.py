"""
strategies/event_social_volume.py — Social Volume Spike Trader.

W45-1 — implements the unified strategy contract for the
``event_social_volume`` catalog entry.

Signal logic
------------
Detects sudden surges in social media mention velocity to trade
news early before the market fully prices it in.

  * Tracks rolling mention count per market over N-minute windows
  * Fires BUY when mention velocity > 3× baseline AND sentiment > 0
  * Fires SELL when mention velocity > 3× baseline AND sentiment < 0

Edge = expected price impact × 1 - (already_priced_in_prob).
"""
from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

BASELINE_WINDOW = 60              # 60-minute baseline (in minutes)
SURGE_MULTIPLIER = 3.0
MIN_SENTIMENT = 0.3               # |sentiment| > 0.3 required
MAX_HOLD_CYCLES = 30              # 30-minute hold window
SCAN_INTERVAL = 60.0              # 1-minute polling


class SocialVolumeSpike(BaseStrategy):
    """Social mention velocity spike trader."""

    name = "event_social_volume"

    def __init__(self) -> None:
        super().__init__()
        self.baseline_window: int = BASELINE_WINDOW
        self.surge_multiplier: float = SURGE_MULTIPLIER
        self.min_sentiment: float = MIN_SENTIMENT
        self.max_hold_cycles: int = MAX_HOLD_CYCLES
        self._interval: float = SCAN_INTERVAL
        self._mention_history: dict[str, deque] = {}

    async def _run(self) -> None:
        log.info(
            "[event_social] Active (baseline=%dmin, surge=%.1f×)",
            self.baseline_window, self.surge_multiplier,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_social] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Social volume spike trader — detects sudden surges in "
                "social mention velocity with directional sentiment to "
                "trade news events early."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "social_volume_spike",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("baseline_window", "max_hold_cycles"):
            if k in config:
                setattr(self, k, int(config[k]))
        for k in ("surge_multiplier", "min_sentiment"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.baseline_window < 5:
            return False, "baseline_window must be >= 5"
        if self.surge_multiplier < 1.5:
            return False, "surge_multiplier must be >= 1.5"
        if not 0 <= self.min_sentiment <= 1:
            return False, "min_sentiment must be in [0, 1]"
        if self.max_hold_cycles < 1:
            return False, "max_hold_cycles must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mentions = market_context.get("mentions_per_minute")
        sentiment = market_context.get("sentiment_score")
        if not token_id or mentions is None or sentiment is None:
            return None
        try:
            current_mentions = float(mentions)
            current_sentiment = float(sentiment)
        except (TypeError, ValueError):
            return None

        # Track mention history for baseline.
        history = self._mention_history.setdefault(token_id, deque(maxlen=self.baseline_window))
        history.append(current_mentions)
        if len(history) < self.baseline_window:
            return None  # need full baseline

        # Baseline = average of all history except the most recent cycle.
        baseline_window = list(history)[:-1]
        baseline = sum(baseline_window) / len(baseline_window)
        if baseline <= 0:
            return None

        surge_ratio = current_mentions / baseline
        if surge_ratio < self.surge_multiplier:
            return None

        if abs(current_sentiment) < self.min_sentiment:
            return None

        # Direction: positive sentiment → BUY (expect price up); negative → SELL.
        if current_sentiment > 0:
            action = "BUY"
            target_price = round(min(float(market_context.get("mid", 0.5)) + 0.01, 0.98), 4)
            reason = (
                f"SocialVol BUY: mentions={current_mentions:.0f} vs baseline={baseline:.0f} "
                f"({surge_ratio:.1f}×); sentiment={current_sentiment:+.2f}"
            )
        else:
            action = "SELL"
            target_price = round(max(float(market_context.get("mid", 0.5)) - 0.01, 0.02), 4)
            reason = (
                f"SocialVol SELL: mentions={current_mentions:.0f} vs baseline={baseline:.0f} "
                f"({surge_ratio:.1f}×); sentiment={current_sentiment:+.2f}"
            )

        # Edge = surge magnitude × sentiment magnitude (capped).
        edge = min(surge_ratio * abs(current_sentiment) * 0.01, 0.05)
        confidence = min(0.85, 0.4 + surge_ratio / 20.0 + abs(current_sentiment) * 0.3)

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
                "model": "social_volume_spike",
                "surge_ratio": surge_ratio,
                "baseline_mentions": baseline,
                "current_mentions": current_mentions,
                "sentiment": current_sentiment,
                "max_hold_cycles": self.max_hold_cycles,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        surge = float(signal.metadata.get("surge_ratio", 1.0))
        sent = abs(float(signal.metadata.get("sentiment", 0.0)))
        size_factor = min(2.5, surge / 2.0 + sent)
        base_size = float(risk_params.get("base_size_usdc", 3.0))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no social signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "IOC",  # news — fill fast
            "post_only": False,
            "metadata": {
                "model": "social_volume_spike",
                "surge_ratio": signal.metadata.get("surge_ratio"),
                "sentiment": signal.metadata.get("sentiment"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when mention velocity normalizes OR after max_hold_cycles."""
        if not position:
            return None
        cycles_held = int(position.get("cycles_held", 0))
        if cycles_held >= self.max_hold_cycles:
            return {
                "reason": "max hold cycles elapsed — exit",
                "cycles_held": cycles_held,
                "type": "market",
            }
        # Mention velocity normalized back to baseline.
        current_surge = float(market_context.get("current_surge_ratio", 1.0))
        if current_surge < 1.2:
            return {
                "reason": "mention surge normalized — exit",
                "current_surge_ratio": current_surge,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "baseline_window": self.baseline_window,
            "surge_multiplier": self.surge_multiplier,
            "min_sentiment": self.min_sentiment,
            "tracked_tokens": len(self._mention_history),
        })
        return base
