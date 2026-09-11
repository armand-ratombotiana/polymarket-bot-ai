"""
strategies/ml_fractional_kelly.py — Fractional Kelly Position Sizer.

W45-1 — implements the unified strategy contract for the
``ml_fractional_kelly`` catalog entry.

Signal logic
------------
Quant strategy sizing all trades with the dynamic Kelly Criterion:

  f* = (p · b - q) / b

where:
  p = probability of winning (model estimate)
  q = 1 - p
  b = net odds (potential gain / potential loss)

The strategy uses FRACTIONAL Kelly (default 0.25× = quarter-Kelly)
to reduce variance. It also enforces a max-position cap so a single
confident signal can't blow up the bankroll.

This strategy is a SIZING strategy — its `generate_signal` produces
a Signal that downstream sizing uses; the `size_position` method is
the canonical Kelly calculator.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.05                   # 5% edge required to act
KELLY_FRACTION = 0.25             # quarter-Kelly for variance reduction
MAX_POSITION_PCT = 0.10           # cap at 10% of bankroll per trade
MIN_PROB = 0.30
MAX_PROB = 0.95
SCAN_INTERVAL = 30.0


class FractionalKellySizing(BaseStrategy):
    """Fractional Kelly criterion position sizer."""

    name = "ml_fractional_kelly"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.kelly_fraction: float = KELLY_FRACTION
        self.max_position_pct: float = MAX_POSITION_PCT
        self.min_prob: float = MIN_PROB
        self.max_prob: float = MAX_PROB
        self._interval: float = SCAN_INTERVAL
        self._kelly_history: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[ml_kelly] Active (fraction=%.2f, min_edge=%.0f%%, max_pct=%.0f%%)",
            self.kelly_fraction, self.min_edge * 100, self.max_position_pct * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_kelly] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Fractional Kelly sizing — dynamic position sizing using "
                "Kelly Criterion f* = (pb - q) / b with variance reduction."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "fractional_kelly",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_edge", "kelly_fraction", "max_position_pct",
                  "min_prob", "max_prob"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_edge < 0:
            return False, "min_edge must be >= 0"
        if not 0 < self.kelly_fraction <= 1.0:
            return False, "kelly_fraction must be in (0, 1]"
        if not 0 < self.max_position_pct <= 1.0:
            return False, "max_position_pct must be in (0, 1]"
        if not 0 < self.min_prob < self.max_prob < 1:
            return False, "min_prob < max_prob required, both in (0, 1)"
        return True, "OK"

    def _compute_kelly(self, p: float, b: float) -> float:
        """Compute full Kelly fraction f* = (p·b - q) / b."""
        if b <= 0:
            return 0.0
        q = 1.0 - p
        f_star = (p * b - q) / b
        return max(0.0, f_star)

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prob = market_context.get("win_probability")
        odds = market_context.get("net_odds")  # b = gain/loss ratio
        mid = market_context.get("mid")
        if not token_id or prob is None or odds is None or mid is None:
            return None
        try:
            p = float(prob)
            b = float(odds)
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None
        if not self.min_prob <= p <= self.max_prob:
            return None
        if b <= 0:
            return None
        if not 0 < mid_f < 1:
            return None

        # Full Kelly f* and fractional Kelly.
        f_star = self._compute_kelly(p, b)
        if f_star <= 0:
            return None  # negative Kelly — no edge
        f_actual = f_star * self.kelly_fraction

        # Edge = expected value per dollar.
        q = 1.0 - p
        expected_value = p * b - q
        edge = expected_value / max(b, 0.01)
        if edge < self.min_edge:
            return None

        # Track for diagnostics.
        self._kelly_history[token_id] = f_actual

        # Direction: BUY if model says up, SELL if model says down.
        if p > 0.5:
            action = "BUY"
            target_price = round(min(mid_f + 0.005, 0.98), 4)
            reason = (
                f"Kelly BUY: p={p:.3f}, b={b:.2f}, f*={f_star:.3f}, "
                f"f_actual={f_actual:.3f} (frac={self.kelly_fraction:.2f})"
            )
        else:
            action = "SELL"
            target_price = round(max(mid_f - 0.005, 0.02), 4)
            reason = (
                f"Kelly SELL: p={p:.3f}, b={b:.2f}, f*={f_star:.3f}, "
                f"f_actual={f_actual:.3f} (frac={self.kelly_fraction:.2f})"
            )

        confidence = min(0.9, 0.4 + abs(p - 0.5) * 1.5 + f_star * 0.5)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=f_actual,  # size = Kelly fraction (will be × capital in size_position)
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=reason,
            metadata={
                "model": "fractional_kelly",
                "win_probability": p,
                "net_odds": b,
                "kelly_f_star": f_star,
                "kelly_fraction": self.kelly_fraction,
                "kelly_f_actual": f_actual,
                "edge": edge,
                "expected_value": expected_value,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Apply Kelly sizing: position = f_actual × capital, capped."""
        if signal is None or signal.action == "HOLD":
            return 0.0
        f_actual = float(signal.metadata.get("kelly_f_actual", 0.0))
        # Position = fractional Kelly × capital.
        position = f_actual * capital
        # Cap at max_position_pct × capital.
        max_cap = self.max_position_pct * capital
        position = min(position, max_cap)
        # Also honor any external cap from risk_params.
        external_cap = float(risk_params.get("max_position_pct", 1.0)) * capital
        return min(position, external_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no Kelly signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "fractional_kelly",
                "kelly_f_star": signal.metadata.get("kelly_f_star"),
                "kelly_f_actual": signal.metadata.get("kelly_f_actual"),
                "expected_value": signal.metadata.get("expected_value"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when Kelly f* goes negative (no edge left)."""
        if not position:
            return None
        current_p = float(market_context.get("current_win_probability", 0.5))
        current_b = float(market_context.get("current_net_odds", 1.0))
        current_f_star = self._compute_kelly(current_p, current_b)
        if current_f_star <= 0:
            return {
                "reason": "Kelly f* went negative — exit",
                "current_f_star": current_f_star,
                "current_p": current_p,
                "current_b": current_b,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "kelly_fraction": self.kelly_fraction,
            "max_position_pct": self.max_position_pct,
            "min_prob": self.min_prob,
            "max_prob": self.max_prob,
            "tracked_tokens": len(self._kelly_history),
        })
        return base
