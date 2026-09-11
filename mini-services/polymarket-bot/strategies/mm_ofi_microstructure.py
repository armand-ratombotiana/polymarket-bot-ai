"""
strategies/mm_ofi_microstructure.py — Order Flow Imbalance Market Maker.

W45-1 — implements the unified strategy contract for the
``mm_ofi_microstructure`` catalog entry.

Signal logic
------------
Real-time micro-depth order-flow-imbalance (OFI) quoting. The strategy
reads the top-N levels of the book and computes:

  OFI = Σ (bid_depth - ask_depth) over top-N levels,
  normalized by total depth to range [-1, +1]

A strongly positive OFI (more aggressive buyers) signals informed flow
is hitting asks — the strategy widens the ask side and tightens the
bid (avoid being run over by buyers). The reverse for negative OFI.

Edge = half_spread × fill_probability, where fill_probability is
discounted by |OFI| (skewed quotes fill less often than balanced ones).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

DEPTH_LEVELS = 5                # top-5 levels of the book
BASE_HALF_SPREAD = 0.015        # 1.5% baseline half-spread
OFI_SKEW_FACTOR = 0.6           # skew = OFI_SKEW × OFI × half_spread
MAX_HALF_SPREAD = 0.08
MIN_HALF_SPREAD = 0.004
TOXIC_OFI_THRESHOLD = 0.7       # |OFI| > 0.7 ⇒ skip (too informed)
MAX_INVENTORY_SHARES = 200
SCAN_INTERVAL = 2.0             # microstructure ⇒ fast polling


class OfiMicrostructureMM(BaseStrategy):
    """Order-flow-imbalance market maker."""

    name = "mm_ofi_microstructure"

    def __init__(self) -> None:
        super().__init__()
        self.depth_levels: int = DEPTH_LEVELS
        self.base_half_spread: float = BASE_HALF_SPREAD
        self.ofi_skew_factor: float = OFI_SKEW_FACTOR
        self.max_half_spread: float = MAX_HALF_SPREAD
        self.min_half_spread: float = MIN_HALF_SPREAD
        self.toxic_ofi_threshold: float = TOXIC_OFI_THRESHOLD
        self.max_inventory_shares: float = MAX_INVENTORY_SHARES
        self._interval: float = SCAN_INTERVAL
        self._ofi_history: dict[str, list[float]] = {}

    async def _run(self) -> None:
        log.info(
            "[mm_ofi] Active (levels=%d, toxic_ofi=%.2f)",
            self.depth_levels, self.toxic_ofi_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mm_ofi] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Microstructure market maker — quotes against real-time "
                "order-flow imbalance (OFI) computed from top-N book "
                "depth to mitigate toxic adverse selection."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "ofi_microstructure",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "depth_levels" in config:
            self.depth_levels = int(config["depth_levels"])
        for k in ("base_half_spread", "ofi_skew_factor", "max_half_spread",
                  "min_half_spread", "toxic_ofi_threshold",
                  "max_inventory_shares"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.depth_levels < 1:
            return False, "depth_levels must be >= 1"
        if self.base_half_spread <= 0:
            return False, "base_half_spread must be > 0"
        if not 0.0 <= self.ofi_skew_factor <= 2.0:
            return False, "ofi_skew_factor must be in [0, 2]"
        if self.max_half_spread <= self.min_half_spread:
            return False, "max_half_spread must be > min_half_spread"
        if not 0.0 < self.toxic_ofi_threshold <= 1.0:
            return False, "toxic_ofi_threshold must be in (0, 1]"
        return True, "OK"

    def _compute_ofi(self, bid_depth: list[float], ask_depth: list[float]) -> float:
        """Normalized OFI in [-1, +1]: +1 = all bid depth, -1 = all ask."""
        n = min(len(bid_depth), len(ask_depth), self.depth_levels)
        if n == 0:
            return 0.0
        total_bid = sum(bid_depth[:n])
        total_ask = sum(ask_depth[:n])
        total = total_bid + total_ask
        if total <= 0:
            return 0.0
        return (total_bid - total_ask) / total

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        bid_depth = list(market_context.get("bid_depth", []))
        ask_depth = list(market_context.get("ask_depth", []))
        if not bid_depth or not ask_depth:
            return None

        ofi = self._compute_ofi(bid_depth, ask_depth)
        # Track recent OFI to detect persistent toxic flow.
        history = self._ofi_history.setdefault(token_id, [])
        history.append(ofi)
        if len(history) > 20:
            history.pop(0)

        if abs(ofi) >= self.toxic_ofi_threshold:
            # Regime too toxic — informed flow dominates, skip.
            return None

        inventory = float(market_context.get("inventory", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return None

        # Skew the quotes: positive OFI ⇒ tighter bid, wider ask.
        half_spread = self.base_half_spread
        ofi_skew = self.ofi_skew_factor * ofi * half_spread
        # Inventory skew: long ⇒ shift quotes down to attract sellers.
        inv_skew = -0.001 * inventory

        bid = round(max(0.01, mid_f - half_spread - ofi_skew + inv_skew), 4)
        ask = round(min(0.99, mid_f + half_spread + ofi_skew + inv_skew), 4)
        if ask - bid < 2 * self.min_half_spread:
            return None

        # Fill probability discounted by |OFI| (skewed quotes fill less).
        fill_prob = 0.55 - 0.25 * abs(ofi)
        edge = half_spread * fill_prob

        # Side to quote: when OFI > 0 (buyers), prefer the bid (BUY) side.
        action = "BUY" if ofi >= 0 else "SELL"
        price = bid if action == "BUY" else ask

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=price,
            confidence=0.6,
            edge=edge,
            reason=(
                f"OFI quote: ofi={ofi:+.3f}, half={half_spread:.4f}, "
                f"skew={ofi_skew:+.4f}, inv={inventory:+.1f}"
            ),
            metadata={
                "model": "ofi_microstructure",
                "ofi": ofi,
                "half_spread": half_spread,
                "ofi_skew": ofi_skew,
                "bid_price": bid,
                "ask_price": ask,
                "fill_probability": fill_prob,
                "inventory": inventory,
                "bid_depth_total": sum(bid_depth[:self.depth_levels]),
                "ask_depth_total": sum(ask_depth[:self.depth_levels]),
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 4.0))
        # Smaller size when OFI magnitude is high (toxic risk).
        ofi = float(signal.metadata.get("ofi", 0.0))
        size_factor = max(0.3, 1.0 - abs(ofi))
        max_pct = float(risk_params.get("max_position_pct", 0.04))
        return min(quote_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no actionable signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": True,
            "metadata": {
                "model": "ofi_microstructure",
                "ofi": signal.metadata.get("ofi"),
                "counter_quote_price": signal.metadata.get("ask_price") if signal.action == "BUY"
                else signal.metadata.get("bid_price"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        if not position:
            return None
        inventory = float(position.get("inventory_shares", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return {
                "reason": "inventory at cap — flatten",
                "inventory": inventory,
                "type": "market",
            }
        # OFI flipped sign since quote placed — regime change.
        entry_ofi = float(position.get("entry_ofi", 0.0))
        current_ofi = float(market_context.get("ofi", 0.0))
        if entry_ofi * current_ofi < 0 and abs(current_ofi) > 0.3:
            return {
                "reason": "OFI flipped — re-quote",
                "entry_ofi": entry_ofi,
                "current_ofi": current_ofi,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "depth_levels": self.depth_levels,
            "toxic_ofi_threshold": self.toxic_ofi_threshold,
            "ofi_history_tokens": len(self._ofi_history),
        })
        return base
