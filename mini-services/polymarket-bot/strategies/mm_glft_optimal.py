"""
strategies/mm_glft_optimal.py — GLFT Optimal Quoter.

W45-1 — implements the unified strategy contract for the
``mm_glft_optimal`` catalog entry.

Signal logic
------------
Gueant-Lehalle-Fernandez-Tapia (GLFT) intensity-based optimal market
making. Quotes symmetric around a reservation price with optimal
half-spread derived from trade arrival intensity.

Closed-form approximations (single-asset, no jump risk):
  * reservation_price = mid - γ * σ² * inventory
  * optimal_half_spread = γ * σ² / 2 + (1 / κ) * log(1 + γ * A / κ)

where:
  * γ  = inventory risk-aversion coefficient
  * σ² = mid price variance (per cycle)
  * κ  = arrival intensity decay (higher ⇒ tighter quotes)
  * A  = base arrival intensity (λ(0) at mid)
  * inventory = signed YES shares held

Quotes are clamped to (0.01, 0.99) and floored by min_half_spread
so the strategy never quotes inside the actual book spread.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── GLFT model parameters ────────────────────────────────────────────────────
GAMMA = 0.10                # inventory risk-aversion (γ)
KAPPA = 100.0               # arrival intensity decay (κ)
ALPHA = 1.5                 # base arrival intensity A (trades/cycle)
SIGMA_BASE = 0.02           # baseline mid-price σ (per cycle)
MIN_HALF_SPREAD = 0.005     # floor at 0.5%
MAX_HALF_SPREAD = 0.10      # cap at 10%
MAX_INVENTORY_SHARES = 200  # flatten if |inventory| exceeds this
SCAN_INTERVAL = 10.0


class GlftOptimalQuoter(BaseStrategy):
    """GLFT intensity-based optimal market maker."""

    name = "mm_glft_optimal"

    def __init__(self) -> None:
        super().__init__()
        self.gamma: float = GAMMA
        self.kappa: float = KAPPA
        self.alpha: float = ALPHA
        self.sigma_base: float = SIGMA_BASE
        self.min_half_spread: float = MIN_HALF_SPREAD
        self.max_half_spread: float = MAX_HALF_SPREAD
        self.max_inventory_shares: float = MAX_INVENTORY_SHARES
        self._interval: float = SCAN_INTERVAL
        self._open_quotes: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[mm_glft_optimal] Active (γ=%.3f, κ=%.1f, A=%.2f)",
            self.gamma, self.kappa, self.alpha,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mm_glft_optimal] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Gueant-Lehalle-Fernandez-Tapia intensity-based optimal "
                "market maker — symmetric quotes around reservation price "
                "with spread derived from arrival intensity λ(p)."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "glft_optimal",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in (
            "gamma", "kappa", "alpha", "sigma_base",
            "min_half_spread", "max_half_spread",
            "max_inventory_shares", "scan_interval",
        ):
            if k in config:
                setattr(self, k.replace("scan_interval", "_interval")
                        if k == "scan_interval" else k, float(config[k]))

    def validate(self) -> tuple[bool, str]:
        if self.gamma <= 0.0:
            return False, f"gamma={self.gamma} must be > 0"
        if self.kappa <= 0.0:
            return False, f"kappa={self.kappa} must be > 0"
        if self.alpha <= 0.0:
            return False, f"alpha={self.alpha} must be > 0"
        if self.sigma_base < 0.0:
            return False, f"sigma_base={self.sigma_base} must be >= 0"
        if self.min_half_spread <= 0.0:
            return False, f"min_half_spread must be > 0"
        if self.max_half_spread <= self.min_half_spread:
            return False, "max_half_spread must be > min_half_spread"
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        inventory = float(market_context.get("inventory", 0.0))
        # Variance per cycle — caller may pass realized vol, else fall back
        # to baseline σ² to keep quoting in low-info regimes.
        realized_vol = float(market_context.get("volatility", self.sigma_base))
        sigma_sq = max(self.sigma_base ** 2, realized_vol ** 2)

        # Reservation price: skew away from inventory (long ⇒ lower r_t).
        reservation = mid_f - self.gamma * sigma_sq * inventory

        # GLFT optimal half-spread (single-asset closed form):
        #   half = γ σ² / 2 + (1/κ) ln(1 + γ A / κ)
        import math
        half_spread = (
            self.gamma * sigma_sq / 2.0
            + (1.0 / self.kappa) * math.log1p(self.gamma * self.alpha / self.kappa)
        )
        half_spread = max(self.min_half_spread, min(self.max_half_spread, half_spread))

        bid = round(max(0.01, reservation - half_spread), 4)
        ask = round(min(0.99, reservation + half_spread), 4)

        if bid >= ask or ask - bid < 2 * self.min_half_spread:
            return None  # degenerate regime

        # Inventory at cap → flatten, don't quote the toxic side.
        action = "BUY"
        if abs(inventory) >= self.max_inventory_shares:
            # If long, only quote ask (SELL side) to flatten.
            if inventory > 0:
                action = "SELL"
                bid = None  # signal only the ask leg
            else:
                action = "BUY"
                ask = None

        edge = half_spread * 0.5  # half-spread capture per side
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._open_quotes[token_id] = {
            "bid": bid, "ask": ask, "half_spread": half_spread,
            "reservation": reservation, "inventory": inventory,
        }

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=bid if action == "BUY" else ask,
            confidence=0.6,
            edge=edge,
            reason=(
                f"GLFT quote: r_t={reservation:.4f}, half={half_spread:.4f}, "
                f"inv={inventory:+.1f}, σ²={sigma_sq:.5f}"
            ),
            metadata={
                "model": "glft_optimal",
                "reservation_price": reservation,
                "half_spread": half_spread,
                "bid_price": bid,
                "ask_price": ask,
                "inventory": inventory,
                "gamma": self.gamma,
                "kappa": self.kappa,
                "alpha": self.alpha,
                "sigma_sq": sigma_sq,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 5.0))
        max_pct = float(risk_params.get("max_position_pct", 0.05))
        return min(quote_size, max_pct * capital, capital)

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
                "model": "glft_optimal",
                "reservation_price": signal.metadata.get("reservation_price"),
                "half_spread": signal.metadata.get("half_spread"),
                "counter_quote": signal.metadata.get("ask_price") if signal.action == "BUY"
                else signal.metadata.get("bid_price"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        if not position:
            return None
        inventory = float(position.get("inventory_shares", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return {
                "reason": "inventory at cap — flatten via market order",
                "inventory": inventory,
                "type": "market",
            }
        # Volatility regime shift — quotes must be cancelled and re-priced.
        realized_vol = float(market_context.get("volatility", 0.0))
        if realized_vol > 3 * self.sigma_base:
            return {
                "reason": "volatility regime shift — cancel and re-price",
                "realized_vol": realized_vol,
                "baseline_sigma": self.sigma_base,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "gamma": self.gamma,
            "kappa": self.kappa,
            "alpha": self.alpha,
            "sigma_base": self.sigma_base,
            "open_quotes": len(self._open_quotes),
            "open_quote_tokens": list(self._open_quotes.keys())[:10],
        })
        return base
