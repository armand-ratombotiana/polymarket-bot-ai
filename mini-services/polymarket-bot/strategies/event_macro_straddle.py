"""
strategies/event_macro_straddle.py — Macro Announcement Straddle.

W45-1 — implements the unified strategy contract for the
``event_macro_straddle`` catalog entry.

Signal logic
------------
Pre-positions ahead of major macroeconomic announcements (CPI, FOMC,
NFP, GDP, etc.) using a straddle execution: buy YES + NO on the
binary outcome market so that the position profits from large
post-announcement moves in EITHER direction.

The strategy:
  * Detects upcoming macro announcements ≤ 1 hour ahead
  * Computes expected post-announcement volatility (from historical
    surprise magnitudes)
  * Buys the straddle when the combined ask < expected_volatility - fees

Edge = expected_post_announcement_move - combined_ask_price.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MAX_HOURS_BEFORE_ANNOUNCEMENT = 1.0
MIN_EXPECTED_VOL = 0.04           # 4% expected post-announcement move
TAKER_FEE_BPS = 1.0
MAX_HOLD_HOURS = 4                # exit within 4 hours post-announcement
SCAN_INTERVAL = 60.0


class MacroStraddleTrader(BaseStrategy):
    """Macro-announcement straddle pre-positioner."""

    name = "event_macro_straddle"

    def __init__(self) -> None:
        super().__init__()
        self.max_hours_before_announcement: float = MAX_HOURS_BEFORE_ANNOUNCEMENT
        self.min_expected_vol: float = MIN_EXPECTED_VOL
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.max_hold_hours: int = MAX_HOLD_HOURS
        self._interval: float = SCAN_INTERVAL
        self._upcoming_announcements: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[event_macro] Active (window=%.1fh, min_vol=%.0f%%)",
            self.max_hours_before_announcement, self.min_expected_vol * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[event_macro] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Macro announcement straddle trader — pre-positions ahead of "
                "CPI/FOMC/jobs reports using YES+NO straddle execution to "
                "capture post-announcement volatility in either direction."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "macro_straddle",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("max_hours_before_announcement", "min_expected_vol", "taker_fee_bps"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "max_hold_hours" in config:
            self.max_hold_hours = int(config["max_hold_hours"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.max_hours_before_announcement <= 0:
            return False, "max_hours_before_announcement must be > 0"
        if self.min_expected_vol <= 0:
            return False, "min_expected_vol must be > 0"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.max_hold_hours < 1:
            return False, "max_hold_hours must be >= 1"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        no_token_id = market_context.get("no_token_id")
        yes_ask = market_context.get("yes_ask")
        no_ask = market_context.get("no_ask")
        hours_to_announcement = market_context.get("hours_to_announcement")
        expected_vol = market_context.get("expected_post_announcement_vol")
        announcement_type = market_context.get("announcement_type")
        if not token_id or not no_token_id:
            return None
        if yes_ask is None or no_ask is None:
            return None
        if hours_to_announcement is None or expected_vol is None:
            return None

        try:
            ya = float(yes_ask)
            na = float(no_ask)
            hta = float(hours_to_announcement)
            ev = float(expected_vol)
        except (TypeError, ValueError):
            return None

        # Timing gate: must be within max_hours_before_announcement of the event.
        if not 0 <= hta <= self.max_hours_before_announcement:
            return None
        if ev < self.min_expected_vol:
            return None

        # Combined straddle cost.
        fee_cost = 2.0 * self.taker_fee_bps / 10000.0
        combined_cost = ya + na + fee_cost
        # Edge = expected move - combined cost.
        # If YES+NO cost < expected vol, the straddle is cheap.
        edge = ev - combined_cost
        if edge <= 0:
            return None

        # Track for diagnostics.
        self._upcoming_announcements[token_id] = {
            "announcement_type": announcement_type,
            "hours_to_announcement": hta,
            "expected_vol": ev,
            "combined_cost": combined_cost,
        }

        edge_pct = edge / combined_cost if combined_cost > 0 else 0.0
        confidence = min(0.85, 0.4 + ev * 5)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action="BUY",
            token_id=token_id,
            size=combined_cost,
            price=ya,
            confidence=confidence,
            edge=edge_pct,
            reason=(
                f"MacroStraddle: {announcement_type or 'macro'} in {hta:.1f}h, "
                f"combined_cost={combined_cost:.4f} < expected_vol={ev:.4f}, "
                f"edge={edge_pct*100:.2f}%"
            ),
            metadata={
                "model": "macro_straddle",
                "yes_token_id": token_id,
                "no_token_id": no_token_id,
                "yes_ask": ya,
                "no_ask": na,
                "combined_cost": combined_cost,
                "expected_post_announcement_vol": ev,
                "hours_to_announcement": hta,
                "announcement_type": announcement_type,
                "edge_pct": edge_pct,
                "max_hold_hours": self.max_hold_hours,
                "legs": [
                    {"token_id": token_id, "side": "BUY", "price": ya, "size": 1.0},
                    {"token_id": no_token_id, "side": "BUY", "price": na, "size": 1.0},
                ],
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Straddles are speculative — cap at lower max_pct than arbs.
        max_pct = float(risk_params.get("max_straddle_pct", 0.05))
        per_event_cap = float(risk_params.get("per_event_cap_usdc", 25.0))
        return min(signal.size, max_pct * capital, per_event_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no macro signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "macro_straddle",
                "legs": signal.metadata.get("legs"),
                "announcement_type": signal.metadata.get("announcement_type"),
                "hours_to_announcement": signal.metadata.get("hours_to_announcement"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit after max_hold_hours post-announcement OR when expected
        volatility collapses (event priced in)."""
        if not position:
            return None
        hours_post = float(market_context.get("hours_post_announcement", 0.0))
        if hours_post >= self.max_hold_hours:
            return {
                "reason": "max hold hours post-announcement — exit",
                "hours_post_announcement": hours_post,
                "type": "market",
            }
        # Volatility collapsed (event passed, market priced in).
        current_combined = float(market_context.get("current_combined_cost", 1.0))
        entry_combined = float(position.get("entry_combined_cost", 1.0))
        # Exit if combined cost reverted to ≥1.0 (no edge left).
        if current_combined >= 0.99:
            return {
                "reason": "combined ask reverted to ~1.0 — exit",
                "current_combined": current_combined,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "max_hours_before_announcement": self.max_hours_before_announcement,
            "min_expected_vol": self.min_expected_vol,
            "taker_fee_bps": self.taker_fee_bps,
            "upcoming_announcements": len(self._upcoming_announcements),
        })
        return base
