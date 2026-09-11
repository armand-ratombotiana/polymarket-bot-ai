"""
strategies/arb_cluster_dislocation.py — Cluster Dislocation Arbitrage.

W45-1 — implements the unified strategy contract for the
``arb_cluster_dislocation`` catalog entry.

Signal logic
------------
A "cluster" is a set of economically-related markets that share a
common driver (e.g. "Will Trump win?" + "Will Republicans win?" +
"Will GOP win popular vote?"). The cluster's prices should be
correlated: when one dislocates from the cluster mean, the strategy
buys the dislocated market (if undervalued) or sells it (if
overvalued), expecting the dislocation to mean-revert.

Dislocation metric:
  z-score = (p_i - cluster_mean) / cluster_stdev

A |z| > 2 (2-sigma dislocation) triggers the trade.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

Z_THRESHOLD = 2.0                # 2-sigma dislocation trigger
MIN_CLUSTER_SIZE = 3
MAX_CLUSTER_SIZE = 20
TAKER_FEE_BPS = 1.0
MIN_EDGE = 0.01                   # 1% edge floor
SCAN_INTERVAL = 30.0


class ClusterDislocationArb(BaseStrategy):
    """Cluster mean-reversion arbitrage trader."""

    name = "arb_cluster_dislocation"

    def __init__(self) -> None:
        super().__init__()
        self.z_threshold: float = Z_THRESHOLD
        self.min_cluster_size: int = MIN_CLUSTER_SIZE
        self.max_cluster_size: int = MAX_CLUSTER_SIZE
        self.taker_fee_bps: float = TAKER_FEE_BPS
        self.min_edge: float = MIN_EDGE
        self._interval: float = SCAN_INTERVAL
        self._cluster_stats: dict[str, dict] = {}

    async def _run(self) -> None:
        log.info(
            "[arb_cluster] Active (z_threshold=%.1f, cluster_size=%d-%d)",
            self.z_threshold, self.min_cluster_size, self.max_cluster_size,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[arb_cluster] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Cluster dislocation arbitrage — captures divergence in "
                "clustered multi-market question groups via z-score mean "
                "reversion trading."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "cluster_dislocation",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("z_threshold", "taker_fee_bps", "min_edge"):
            if k in config:
                setattr(self, k, float(config[k]))
        for k in ("min_cluster_size", "max_cluster_size"):
            if k in config:
                setattr(self, k, int(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.z_threshold <= 0:
            return False, "z_threshold must be > 0"
        if self.min_cluster_size < 2:
            return False, "min_cluster_size must be >= 2"
        if self.max_cluster_size < self.min_cluster_size:
            return False, "max_cluster_size must be >= min_cluster_size"
        if self.taker_fee_bps < 0:
            return False, "taker_fee_bps must be >= 0"
        if self.min_edge < 0:
            return False, "min_edge must be >= 0"
        return True, "OK"

    def _compute_cluster_stats(self, prices: list[float]) -> tuple[float, float]:
        n = len(prices)
        if n < 2:
            return 0.0, 1.0
        mean = sum(prices) / n
        var = sum((p - mean) ** 2 for p in prices) / n
        return mean, max(var ** 0.5, 1e-6)

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        cluster_id = market_context.get("cluster_id")
        markets = market_context.get("markets")
        if not cluster_id or not markets or not isinstance(markets, list):
            return None
        if not self.min_cluster_size <= len(markets) <= self.max_cluster_size:
            return None

        # Validate each market has a token_id + price.
        valid = [m for m in markets if "token_id" in m and "price" in m]
        if len(valid) < self.min_cluster_size:
            return None

        prices = [float(m["price"]) for m in valid]
        mean, stdev = self._compute_cluster_stats(prices)
        self._cluster_stats[cluster_id] = {"mean": mean, "stdev": stdev, "n": len(prices)}

        # Find the most-dislocated market.
        best_z = 0.0
        best_market = None
        for m, p in zip(valid, prices):
            z = (p - mean) / stdev
            if abs(z) > abs(best_z):
                best_z = z
                best_market = m

        if best_market is None or abs(best_z) < self.z_threshold:
            return None

        # Edge: dislocation × reversion_prob (approximated by z-score
        # magnitude, capped at 0.8).
        fee_cost = self.taker_fee_bps / 10000.0
        gross_edge = abs(best_z) * stdev
        net_edge = gross_edge - fee_cost
        edge_pct = net_edge / max(best_market["price"], 0.01)
        if edge_pct < self.min_edge:
            return None

        # Direction: positive z (above mean) ⇒ SELL; negative z ⇒ BUY.
        if best_z > 0:
            action = "SELL"
        else:
            action = "BUY"

        confidence = min(0.85, 0.4 + abs(best_z) / 5.0)
        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=best_market["token_id"],
            size=float(best_market.get("liquidity_usdc", 50.0)),
            price=float(best_market["price"]),
            confidence=confidence,
            edge=edge_pct,
            reason=(
                f"Cluster arb: z={best_z:+.2f} on cluster_id={cluster_id} "
                f"(mean={mean:.3f}, σ={stdev:.4f}, p={best_market['price']:.3f})"
            ),
            metadata={
                "model": "cluster_dislocation",
                "cluster_id": cluster_id,
                "z_score": best_z,
                "cluster_mean": mean,
                "cluster_stdev": stdev,
                "cluster_size": len(valid),
                "dislocated_market": best_market,
                "gross_edge": gross_edge,
                "fee_cost": fee_cost,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        # Smaller size for higher-z dislocations (more likely to revert
        # but also more likely to keep dislocating in adverse direction).
        z = float(signal.metadata.get("z_score", 0.0))
        size_factor = max(0.3, 1.0 - abs(z) / 10.0)
        max_pct = float(risk_params.get("max_arb_pct", 0.10))
        return min(signal.size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no arb signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "cluster_dislocation",
                "z_score": signal.metadata.get("z_score"),
                "cluster_id": signal.metadata.get("cluster_id"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the z-score reverts below 0.5σ (cluster normalized)."""
        if not position:
            return None
        current_z = float(market_context.get("current_z_score", 0.0))
        if abs(current_z) < 0.5:
            return {
                "reason": "z-score reverted — close at market",
                "current_z": current_z,
                "type": "market",
            }
        # Hard stop: z-score doubled since entry (dislocation worsened).
        entry_z = float(position.get("entry_z_score", 0.0))
        if (entry_z != 0 and
                abs(current_z) > 2.0 * abs(entry_z) and
                entry_z * current_z > 0):
            return {
                "reason": "dislocation worsened — stop-loss at market",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "z_threshold": self.z_threshold,
            "min_cluster_size": self.min_cluster_size,
            "max_cluster_size": self.max_cluster_size,
            "tracked_clusters": len(self._cluster_stats),
        })
        return base
