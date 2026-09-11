"""
strategies/mm_volatility_adaptive.py — Volatility Adaptive Market Maker.

W45-1 — implements the unified strategy contract for the
``mm_volatility_adaptive`` catalog entry.

Signal logic
------------
ATR-driven dynamic spread: spread widens in high-volatility regimes
(toxic adverse selection risk) and narrows in calm regimes (capture
more spread without getting run over).

Inputs (via ``market_context``)
-------------------------------
  * ``token_id``       — outcome token id
  * ``mid``            — current market mid
  * ``true_range``     — current cycle's True Range (|high - low| or
                         |prev_close - high| / |prev_close - low|)
  * ``prev_closes``    — list of prior N close prices for ATR calc
  * ``inventory``      — signed YES shares held

Edge
----
Expected edge per side = half_spread × fill_probability where
fill_probability falls as volatility rises (toxic flow risk).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

ATR_WINDOW = 14                # 14-cycle ATR (industry standard)
BASE_HALF_SPREAD = 0.015       # 1.5% baseline half-spread
VOL_SCALAR = 2.0               # half_spread += VOL_SCALAR × ATR
MIN_HALF_SPREAD = 0.005        # floor at 0.5%
MAX_HALF_SPREAD = 0.12         # cap at 12% (extreme vol regime)
MAX_INVENTORY_SHARES = 250
SCAN_INTERVAL = 15.0


class VolatilityAdaptiveMM(BaseStrategy):
    """ATR-driven volatility-adaptive market maker."""

    name = "mm_volatility_adaptive"

    def __init__(self) -> None:
        super().__init__()
        self.atr_window: int = ATR_WINDOW
        self.base_half_spread: float = BASE_HALF_SPREAD
        self.vol_scalar: float = VOL_SCALAR
        self.min_half_spread: float = MIN_HALF_SPREAD
        self.max_half_spread: float = MAX_HALF_SPREAD
        self.max_inventory_shares: float = MAX_INVENTORY_SHARES
        self._interval: float = SCAN_INTERVAL
        self._atr_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[mm_vol_adaptive] Active (atr_w=%d, vol_scalar=%.2f)",
            self.atr_window, self.vol_scalar,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mm_vol_adaptive] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Volatility-adaptive market maker — ATR-driven dynamic "
                "spread that widens in toxic regimes, narrows in calm."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "volatility_adaptive",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "atr_window" in config:
            self.atr_window = int(config["atr_window"])
        for k in ("base_half_spread", "vol_scalar", "min_half_spread",
                  "max_half_spread", "max_inventory_shares"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.atr_window < 2:
            return False, "atr_window must be >= 2"
        if self.base_half_spread <= 0:
            return False, "base_half_spread must be > 0"
        if self.vol_scalar < 0:
            return False, "vol_scalar must be >= 0"
        if self.min_half_spread <= 0:
            return False, "min_half_spread must be > 0"
        if self.max_half_spread <= self.min_half_spread:
            return False, "max_half_spread must be > min_half_spread"
        return True, "OK"

    def _compute_atr(self, true_range: float, prev_closes: list[float]) -> float:
        """Compute Wilder-style ATR using the prior window."""
        if not prev_closes:
            return max(true_range, self.min_half_spread)
        window = prev_closes[-self.atr_window:]
        if len(window) < 2:
            return max(true_range, self.min_half_spread)
        # True Range per cycle: max(|high-low|, |prev_close - high|, |prev_close - low|)
        trs = []
        for i in range(1, len(window)):
            hi, lo = max(window[i], window[i - 1]), min(window[i], window[i - 1])
            trs.append(abs(hi - lo))
        atr = sum(trs) / len(trs) if trs else true_range
        # Smooth with the current-cycle TR.
        atr = (atr * (self.atr_window - 1) + true_range) / self.atr_window
        return max(atr, 0.0001)

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        true_range = float(market_context.get("true_range", 0.01))
        prev_closes = list(market_context.get("prev_closes", []))
        atr = self._compute_atr(true_range, prev_closes)
        self._atr_cache[token_id] = atr

        half_spread = self.base_half_spread + self.vol_scalar * atr
        half_spread = max(self.min_half_spread, min(self.max_half_spread, half_spread))

        inventory = float(market_context.get("inventory", 0.0))
        # Inventory skew (à la A-S): long inventory ⇒ shift quotes down.
        reservation = mid_f - 0.5 * inventory * 0.001

        bid = round(max(0.01, reservation - half_spread), 4)
        ask = round(min(0.99, reservation + half_spread), 4)
        if ask - bid < 2 * self.min_half_spread:
            return None

        # Fill probability falls as ATR rises — toxic flow risk.
        fill_prob = max(0.15, 0.55 - 1.5 * atr)
        edge = half_spread * fill_prob

        action = "BUY"
        if inventory > self.max_inventory_shares:
            action = "SELL"
        elif inventory < -self.max_inventory_shares:
            action = "BUY"

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=bid if action == "BUY" else ask,
            confidence=0.5,
            edge=edge,
            reason=(
                f"VolAdaptive quote: ATR={atr:.4f}, half={half_spread:.4f}, "
                f"fill_p={fill_prob:.2f}, inv={inventory:+.1f}"
            ),
            metadata={
                "model": "volatility_adaptive",
                "atr": atr,
                "half_spread": half_spread,
                "bid_price": bid,
                "ask_price": ask,
                "fill_probability": fill_prob,
                "inventory": inventory,
                "reservation_price": reservation,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 5.0))
        # In high-vol regimes, reduce size proportionally.
        atr = float(signal.metadata.get("atr", 0.01))
        vol_factor = max(0.25, 1.0 - 5.0 * atr)
        max_pct = float(risk_params.get("max_position_pct", 0.05))
        return min(quote_size * vol_factor, max_pct * capital, capital)

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
                "model": "volatility_adaptive",
                "atr": signal.metadata.get("atr"),
                "half_spread": signal.metadata.get("half_spread"),
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
        token_id = position.get("token_id", "")
        current_atr = self._atr_cache.get(token_id, 0.0)
        if current_atr > 0.10:
            return {
                "reason": "extreme vol regime — cancel quotes",
                "current_atr": current_atr,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "atr_window": self.atr_window,
            "base_half_spread": self.base_half_spread,
            "vol_scalar": self.vol_scalar,
            "atr_cache_size": len(self._atr_cache),
        })
        return base
