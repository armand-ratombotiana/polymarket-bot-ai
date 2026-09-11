"""
strategies/mm_poisson_arrival.py — Poisson Arrival Quoter.

W45-1 — implements the unified strategy contract for the
``mm_poisson_arrival`` catalog entry.

Signal logic
------------
Market maker whose quoting is calibrated to a Poisson trade-arrival
intensity λ(p) — the expected number of executions per cycle when a
limit order is posted at price p:

  λ(p) = A × exp(-κ × |p - mid|)

The expected per-cycle capture is:

  capture(p) = λ(p) × (p - mid) - taker_fee × λ(p)

The optimal quote price maximizes the per-cycle capture, which yields:

  p* = mid + (1/κ) (closed-form)

Quotes are scaled by the base intensity A (more aggressive posting
when liquidity demand is high) and floored by min_half_spread to
avoid degenerate quoting in low-A regimes.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

LAMBDA_BASE_A = 1.5       # base arrival intensity A (trades/cycle at mid)
KAPPA = 80.0              # arrival decay (higher ⇒ tighter quotes)
MIN_HALF_SPREAD = 0.004   # floor at 0.4%
MAX_HALF_SPREAD = 0.08    # cap at 8%
TAKER_FEE_BPS = 2.0       # 2 bps taker fee (round-trip cost)
MAX_INVENTORY_SHARES = 250
SCAN_INTERVAL = 8.0


class PoissonArrivalQuoter(BaseStrategy):
    """Poisson-arrival-calibrated market maker."""

    name = "mm_poisson_arrival"

    def __init__(self) -> None:
        super().__init__()
        self.lambda_base_a: float = LAMBDA_BASE_A
        self.kappa: float = KAPPA
        self.min_half_spread: float = MIN_HALF_SPREAD
        self.max_half_spread: float = MAX_HALF_SPREAD
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.max_inventory_shares: float = MAX_INVENTORY_SHARES
        self._interval: float = SCAN_INTERVAL
        self._lambda_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[mm_poisson] Active (A=%.2f, κ=%.1f, fee=%.1fbps)",
            self.lambda_base_a, self.kappa, self.taker_fee_bps,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[mm_poisson] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Poisson arrival-calibrated quoter — posts at the optimal "
                "price p* = mid + 1/κ derived from trade-arrival intensity "
                "λ(p) = A × exp(-κ × |p - mid|)."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "poisson_arrival",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("lambda_base_a", "kappa", "min_half_spread",
                  "max_half_spread", "taker_fee_bps", "max_inventory_shares"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.lambda_base_a <= 0:
            return False, "lambda_base_a must be > 0"
        if self.kappa <= 0:
            return False, "kappa must be > 0"
        if self.min_half_spread <= 0:
            return False, "min_half_spread must be > 0"
        if self.max_half_spread <= self.min_half_spread:
            return False, "max_half_spread must be > min_half_spread"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        return True, "OK"

    def _arrival_intensity(self, half_spread: float) -> float:
        """λ(p) at distance half_spread from mid."""
        return self.lambda_base_a * math.exp(-self.kappa * half_spread)

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        # Optimal half-spread (closed form): 1/κ.
        optimal_half = 1.0 / self.kappa
        # Floor / cap.
        half_spread = max(self.min_half_spread, min(self.max_half_spread, optimal_half))

        # Arrival intensity at the optimal quote.
        lam = self._arrival_intensity(half_spread)
        self._lambda_cache[token_id] = lam

        # Expected per-cycle capture (per side):
        #   capture = λ × half_spread - taker_fee × λ
        taker_fee = self.taker_fee_bps / 10000.0
        capture = lam * (half_spread - taker_fee)
        if capture <= 0:
            return None  # fees dominate the spread — skip

        inventory = float(market_context.get("inventory", 0.0))
        if abs(inventory) >= self.max_inventory_shares:
            return None

        # Inventory skew: long ⇒ shift quotes down to flatten.
        inv_skew = -0.001 * inventory
        bid = round(max(0.01, mid_f - half_spread + inv_skew), 4)
        ask = round(min(0.99, mid_f + half_spread + inv_skew), 4)
        if ask - bid < 2 * self.min_half_spread:
            return None

        action = "BUY"
        price = bid
        if inventory > self.max_inventory_shares / 2:
            action = "SELL"
            price = ask

        # Confidence: higher when λ is healthy (>0.5 trades/cycle).
        confidence = min(0.9, lam / 2.0)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=price,
            confidence=confidence,
            edge=capture,
            reason=(
                f"Poisson quote: λ={lam:.3f}, half={half_spread:.4f}, "
                f"capture={capture:.5f}, inv={inventory:+.1f}"
            ),
            metadata={
                "model": "poisson_arrival",
                "lambda": lam,
                "kappa": self.kappa,
                "half_spread": half_spread,
                "bid_price": bid,
                "ask_price": ask,
                "capture_per_cycle": capture,
                "inventory": inventory,
                "taker_fee_bps": self.taker_fee_bps,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 4.0))
        # Scale by arrival intensity (more aggressive when λ is high).
        lam = float(signal.metadata.get("lambda", 1.0))
        size_factor = min(2.0, max(0.5, lam))
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
                "model": "poisson_arrival",
                "lambda": signal.metadata.get("lambda"),
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
        # Arrival intensity collapsed (λ → 0) — no point quoting.
        token_id = position.get("token_id", "")
        current_lambda = self._lambda_cache.get(token_id, 0.0)
        if current_lambda < 0.05:
            return {
                "reason": "arrival intensity collapsed — cancel quotes",
                "current_lambda": current_lambda,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "lambda_base_a": self.lambda_base_a,
            "kappa": self.kappa,
            "taker_fee_bps": self.taker_fee_bps,
            "lambda_cache_size": len(self._lambda_cache),
        })
        return base
