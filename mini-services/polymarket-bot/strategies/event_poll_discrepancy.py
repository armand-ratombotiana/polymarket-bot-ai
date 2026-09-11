"""
strategies/event_poll_discrepancy.py — Polling Gap Exploiter.

W45-1 — implements the unified strategy contract for the
``event_poll_discrepancy`` catalog entry.

Signal logic
------------
Exploits statistical gaps between real-world polling aggregates
(e.g. FiveThirtyEight, RealClearPolitics) and market prices:

  * poll_p = aggregated polling probability
  * market_p = CLOB mid price (implied probability)
  * gap = poll_p - market_p
  * gap_pct = gap / market_p

Trading rules:
  * gap_pct > +5% → BUY (polls say market underpriced)
  * gap_pct < -5% → SELL (polls say market overpriced)

A polling-source confidence weight gates the trade (only act when
the polling aggregate has ≥ 1000 sample size and MoE < 4%).

Edge = expected polling-driven price correction × confidence.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_GAP_PCT = 0.05               # 5% gap required
MIN_SAMPLE_SIZE = 1000
MAX_MARGIN_OF_ERROR = 0.04        # MoE must be < 4%
MAX_HOLD_HOURS = 72               # exit if not resolved within 3 days
SCAN_INTERVAL = 600.0             # 10-minute polling


class PollDiscrepancyTrader(BaseStrategy):
    """Polling-vs-market gap exploiter."""

    name = "event_poll_discrepancy"

    def __init__(self) -> None:
        super().__init__()
        self.min_gap_pct: float = MIN_GAP_PCT
        self.min_sample_size: int = MIN_SAMPLE_SIZE
        self.max_margin_of_error: float = MAX_MARGIN_OF_ERROR
        self.max_hold_hours: int = MAX_HOLD_HOURS
        self._interval: float = SCAN_INTERVAL
        self._gap_history: dict[str, list[float]] = {}

    async def _run(self) -> None:
        log.info(
            "[event_poll] Active (min_gap=%.0f%%, sample>=%d)",
            self.min_gap_pct * 100, self.min_sample_size,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_poll] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Polling discrepancy exploiter — trades statistical gaps "
                "between aggregated polling probabilities and market prices "
                "with sample-size and MoE confidence gates."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "poll_discrepancy",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_gap_pct", "max_margin_of_error"):
            if k in config:
                setattr(self, k, float(config[k]))
        for k in ("min_sample_size", "max_hold_hours"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_gap_pct <= 0:
            return False, "min_gap_pct must be > 0"
        if self.min_sample_size < 100:
            return False, "min_sample_size must be >= 100"
        if not 0 < self.max_margin_of_error <= 0.10:
            return False, "max_margin_of_error must be in (0, 0.10]"
        if self.max_hold_hours < 1:
            return False, "max_hold_hours must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        poll_p = market_context.get("poll_probability")
        market_p = market_context.get("market_price")
        sample_size = market_context.get("sample_size")
        margin_of_error = market_context.get("margin_of_error")
        if not token_id or poll_p is None or market_p is None:
            return None
        if sample_size is None or margin_of_error is None:
            return None

        try:
            pp = float(poll_p)
            mp = float(market_p)
            ss = float(sample_size)
            moe = float(margin_of_error)
        except (TypeError, ValueError):
            return None
        if not 0 < pp < 1 or not 0 < mp < 1:
            return None

        # Confidence gates: sample size + MoE.
        if ss < self.min_sample_size:
            return None
        if moe > self.max_margin_of_error:
            return None

        gap = pp - mp
        gap_pct = gap / mp if mp > 0 else 0.0

        # Track for diagnostics.
        history = self._gap_history.setdefault(token_id, [])
        history.append(gap_pct)
        if len(history) > 100:
            history.pop(0)

        if abs(gap_pct) < self.min_gap_pct:
            return None

        if gap > 0:
            action = "BUY"
            target_price = round(min(mp + 0.01, 0.98), 4)
            reason = (
                f"PollGap BUY: poll={pp:.3f} > market={mp:.3f} "
                f"(gap={gap_pct*100:+.2f}%, n={ss:.0f}, MoE={moe:.3f})"
            )
        else:
            action = "SELL"
            target_price = round(max(mp - 0.01, 0.02), 4)
            reason = (
                f"PollGap SELL: poll={pp:.3f} < market={mp:.3f} "
                f"(gap={gap_pct*100:+.2f}%, n={ss:.0f}, MoE={moe:.3f})"
            )

        # Edge = expected price correction × (1 - MoE buffer).
        edge = abs(gap_pct) * (1.0 - moe * 5) * 0.5
        # Confidence = sample size weight × (1 - MoE buffer).
        confidence = min(0.85, 0.4 + min(ss / 5000, 0.3) + (1 - moe * 5) * 0.15)

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
                "model": "poll_discrepancy",
                "poll_probability": pp,
                "market_price": mp,
                "gap": gap,
                "gap_pct": gap_pct,
                "sample_size": ss,
                "margin_of_error": moe,
                "max_hold_hours": self.max_hold_hours,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        gap_pct = abs(float(signal.metadata.get("gap_pct", 0.0)))
        moe = float(signal.metadata.get("margin_of_error", 0.04))
        size_factor = min(2.5, gap_pct * 10) * (1 - moe * 5)
        size_factor = max(0.3, size_factor)
        base_size = float(risk_params.get("base_size_usdc", 3.0))
        max_pct = float(risk_params.get("max_position_pct", 0.05))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no poll gap signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "poll_discrepancy",
                "poll_probability": signal.metadata.get("poll_probability"),
                "gap_pct": signal.metadata.get("gap_pct"),
                "sample_size": signal.metadata.get("sample_size"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the gap closes (market corrects to polls) OR after
        max_hold_hours (polls outdated)."""
        if not position:
            return None
        current_gap = float(market_context.get("current_gap_pct", 0.0))
        if abs(current_gap) < self.min_gap_pct * 0.3:
            return {
                "reason": "poll gap closed — market corrected",
                "current_gap_pct": current_gap,
                "type": "market",
            }
        hours_held = int(position.get("hours_held", 0))
        if hours_held >= self.max_hold_hours:
            return {
                "reason": "max hold hours elapsed — polls may be stale",
                "hours_held": hours_held,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_gap_pct": self.min_gap_pct,
            "min_sample_size": self.min_sample_size,
            "max_margin_of_error": self.max_margin_of_error,
            "tracked_tokens": len(self._gap_history),
        })
        return base
