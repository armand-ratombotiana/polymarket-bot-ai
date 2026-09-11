"""
strategies/stat_zscore_anomaly.py — Z-Score Anomaly Trader.

W45-1 — implements the unified strategy contract for the
``stat_zscore_anomaly`` catalog entry.

Signal logic
------------
Outlier detection on price deviation from a volume-weighted mean:

  z = (current_price - VWAP) / σ_vol_weighted

When |z| > 2.5, the strategy fires a reversion signal:
  * z < -2.5 ⇒ BUY (price is anomalously below VWAP — revert up)
  * z > +2.5 ⇒ SELL (price is anomalously above VWAP — revert down)

The volume-weighted mean and stdev are computed over the last N
trade ticks. Volume weighting gives more weight to high-volume ticks
(a more accurate fair-value estimate than a simple average).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

Z_THRESHOLD = 2.5                 # |z| > 2.5 triggers signal
VWAP_WINDOW = 50                  # 50-tick VWAP lookback
MIN_VOLUME_TOTAL = 100.0          # need ≥100 shares total volume
SCAN_INTERVAL = 30.0


class ZScoreAnomalyTrader(BaseStrategy):
    """Z-score outlier detection trader (volume-weighted)."""

    name = "stat_zscore_anomaly"

    def __init__(self) -> None:
        super().__init__()
        self.z_threshold: float = Z_THRESHOLD
        self.vwap_window: int = VWAP_WINDOW
        self.min_volume_total: float = MIN_VOLUME_TOTAL
        self._interval: float = SCAN_INTERVAL
        self._last_z: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[stat_zscore] Active (z_threshold=%.1f, vwap_window=%d)",
            self.z_threshold, self.vwap_window,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_zscore] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Z-score anomaly trader — outlier detection on price deviation "
                "from volume-weighted mean; fires reversion signals at |z|>2.5."
            ),
            "author": "polymarket-bot",
            "category": "statistical",
            "model": "zscore_anomaly",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "vwap_window" in config:
            self.vwap_window = int(config["vwap_window"])
        for k in ("z_threshold", "min_volume_total"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.z_threshold <= 0:
            return False, "z_threshold must be > 0"
        if self.vwap_window < 5:
            return False, "vwap_window must be >= 5"
        if self.min_volume_total <= 0:
            return False, "min_volume_total must be > 0"
        return True, "OK"

    def _compute_vwap_and_sigma(
        self, prices: list[float], volumes: list[float]
    ) -> tuple[float, float, float]:
        """Return (vwap, weighted_stdev, total_volume)."""
        n = min(len(prices), len(volumes), self.vwap_window)
        if n < 2:
            return 0.0, 1.0, 0.0
        p = prices[-n:]
        v = volumes[-n:]
        total_v = sum(v)
        if total_v <= 0:
            return 0.0, 1.0, 0.0
        vwap = sum(pi * vi for pi, vi in zip(p, v)) / total_v
        # Volume-weighted variance.
        var = sum(vi * (pi - vwap) ** 2 for pi, vi in zip(p, v)) / total_v
        sigma = max(var ** 0.5, 1e-6)
        return vwap, sigma, total_v

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        prices = market_context.get("prices")
        volumes = market_context.get("volumes")
        if not token_id or not prices or not volumes:
            return None
        if not isinstance(prices, list) or not isinstance(volumes, list):
            return None
        if len(prices) != len(volumes):
            return None

        try:
            p_list = [float(p) for p in prices]
            v_list = [float(v) for v in volumes]
        except (TypeError, ValueError):
            return None

        vwap, sigma, total_v = self._compute_vwap_and_sigma(p_list, v_list)
        if total_v < self.min_volume_total:
            return None

        current = p_list[-1]
        z = (current - vwap) / sigma
        self._last_z[token_id] = z

        if abs(z) < self.z_threshold:
            return None

        if z < 0:
            action = "BUY"
            target_price = round(min(current + 0.005, 0.98), 4)
            reason = (
                f"Z-Score BUY: z={z:+.2f} < -{self.z_threshold}, "
                f"price={current:.4f} vs VWAP={vwap:.4f} (σ={sigma:.4f})"
            )
        else:
            action = "SELL"
            target_price = round(max(current - 0.005, 0.02), 4)
            reason = (
                f"Z-Score SELL: z={z:+.2f} > +{self.z_threshold}, "
                f"price={current:.4f} vs VWAP={vwap:.4f} (σ={sigma:.4f})"
            )

        # Edge = expected reversion to VWAP, scaled by |z|.
        edge = abs(z) * 0.005  # 0.5% per σ of dislocation
        confidence = min(0.9, 0.4 + abs(z) / 5.0)

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
                "model": "zscore_anomaly",
                "z_score": z,
                "vwap": vwap,
                "sigma": sigma,
                "total_volume": total_v,
                "current_price": current,
                "vwap_window": self.vwap_window,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        z = float(signal.metadata.get("z_score", 0.0))
        # Bigger position when |z| is larger.
        size_factor = min(2.5, abs(z) / 2.0)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no actionable signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "zscore_anomaly",
                "z_score": signal.metadata.get("z_score"),
                "vwap": signal.metadata.get("vwap"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when z reverts below 0.5σ (anomaly normalized)."""
        if not position:
            return None
        current_z = float(market_context.get("current_z", 0.0))
        if abs(current_z) < 0.5:
            return {
                "reason": "z-score reverted to neutral — take profit",
                "current_z": current_z,
                "type": "limit",
                "price": float(market_context.get("current_price", 0.5)),
            }
        # Stop-loss: |z| doubled since entry.
        entry_z = float(position.get("entry_z", 0.0))
        if abs(current_z) > 2.0 * abs(entry_z) and entry_z * current_z > 0:
            return {
                "reason": "anomaly worsened — stop-loss",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "z_threshold": self.z_threshold,
            "vwap_window": self.vwap_window,
            "tracked_tokens": len(self._last_z),
        })
        return base
