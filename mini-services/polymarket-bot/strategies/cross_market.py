"""
strategies/cross_market.py — Cluster Dislocation Cross-Market Trader.

W44-1 — implements the unified strategy contract for the fifth of
five high-value strategies promoted from the PLANNED catalog in
this wave. Maps to catalog id ``arb_cluster_dislocation`` —
originally "Cluster Dislocation Arb — Divergence capture in
clustered multi-market question groups".

Signal logic
------------
A "cluster" is a group of N ≥ 3 prediction-market contracts whose
outcomes should move together (e.g. 4 state-level election markets
in the same national election, or 3 bitcoin price-threshold markets
at different strike prices). When the cluster's price-distribution
diverges from its historical norm — typically because one member
dislocated (driven by idiosyncratic flow) — the strategy trades the
dislocated member back toward the cluster's central tendency.

Distinct from ``strategies/stat_arb.py`` (the W22-3
``StatisticalArbitrage`` strategy that maps to
``arb_cross_correlation``): StatisticalArbitrage is a 2-market pair
trade on the spread; CrossMarket is an N-market cluster trade on the
dislocated member's deviation from the cluster's central tendency.
The two are complementary — pairs-trading captures symmetric
divergences; cluster-dislocation captures asymmetric ones.

Inputs (via ``market_context``)
-------------------------------
  * ``cluster_members`` (list[dict], required) — each dict carries:
      - ``token_id`` (str)
      - ``mid`` (float ∈ (0, 1))
      - ``historical_mean`` (float ∈ (0, 1), optional — the rolling
        mean for this member; if absent, the cluster's central
        tendency is used)
      - ``historical_std`` (float > 0, optional — the rolling σ
        for this member; if absent, the cluster's σ is used)
  * ``cluster_correlation`` (float ∈ (0, 1], optional) — the
    average pairwise correlation across cluster members. Below
    ``min_cluster_correlation`` the cluster isn't tightly coupled
    enough to trade the dislocation.
  * ``spread`` (float > 0, optional) — current bid-ask spread of
    the dislocated member; skips ≥ ``max_spread``.
  * ``target_token_id`` (str, optional) — the token to trade. When
    omitted, the strategy picks the most-dislocated member.

Edge estimation
---------------
Edge = ``|z_score| × cluster_correlation × inverse_rank`` where
``z_score`` is the dislocated member's deviation from the cluster's
central tendency (z-scored against the cluster's σ) and
``inverse_rank = 1.0 / member_rank`` so the most-dislocated member
contributes the highest edge.

Order routing
-------------
The async ``_run`` loop is intentionally a no-op stub; the strategy
is driven by the sync contract surface (``generate_signal``) for
backtest / dashboard introspection. Live trading wiring (paper /
CLOB order submission via ``BaseStrategy.submit_order``) is left to a
future wave — the strategy is honest about its status.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
MIN_CLUSTER_SIZE = 3             # need ≥ 3 members to form a cluster
MIN_CLUSTER_CORRELATION = 0.55   # |corr| ≥ 0.55 ⇒ cluster is tightly coupled
Z_SCORE_THRESHOLD = 1.5          # |z| ≥ 1.5σ ⇒ member is dislocated
MAX_Z_SCORE = 6.0                # |z| > 6σ ⇒ data error, skip
MAX_SPREAD = 0.05                # skip markets with ≥ 5% spreads
MAX_POSITION_PCT = 0.05          # 5% of capital per cross-market trade
SCAN_INTERVAL = 60.0


class CrossMarket(BaseStrategy):
    """Cluster-dislocation cross-market trader.

    Identifies the most-dislocated member of a tightly-coupled
    cluster of ≥ 3 prediction-market contracts and trades it back
    toward the cluster's central tendency (mean / median).
    """

    name = "cross_market"

    def __init__(self) -> None:
        super().__init__()
        self.min_cluster_size: int = MIN_CLUSTER_SIZE
        self.min_cluster_correlation: float = MIN_CLUSTER_CORRELATION
        self.z_score_threshold: float = Z_SCORE_THRESHOLD
        self.max_z_score: float = MAX_Z_SCORE
        self.max_spread: float = MAX_SPREAD
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-cluster open positions: one position per cluster at a time.
        self._open_clusters: dict[str, dict] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Async scan loop stub.

        W44-1 — the live trading wiring (paper-sim create_order /
        clob_client.create_order) is intentionally deferred; the
        strategy is catalog-IMPLEMENTED so the dashboard surfaces it,
        but its canonical signal surface is the SYNC contract method
        ``generate_signal``. The loop polls and logs so the registry
        lifecycle's ``start`` / ``stop`` plumbing works end-to-end.
        """
        log.info(
            "[cross_market] Active (cluster_size>=%d, corr>=%.2f, |z|>=%.1fσ)",
            self.min_cluster_size, self.min_cluster_correlation,
            self.z_score_threshold,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[cross_market] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W44-1 — StrategyContract implementations ────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Cluster-dislocation cross-market trader — "
                "identifies the most-dislocated member of a "
                "tightly-coupled cluster of N ≥ 3 markets and "
                "trades it back toward the cluster's central tendency."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "cluster_zscore_dislocation",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "min_cluster_size" in config:
            self.min_cluster_size = int(config["min_cluster_size"])
        if "min_cluster_correlation" in config:
            self.min_cluster_correlation = float(config["min_cluster_correlation"])
        if "z_score_threshold" in config:
            self.z_score_threshold = float(config["z_score_threshold"])
        if "max_z_score" in config:
            self.max_z_score = float(config["max_z_score"])
        if "max_spread" in config:
            self.max_spread = float(config["max_spread"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_cluster_size < 3:
            return False, (
                f"min_cluster_size={self.min_cluster_size} must be >= 3 "
                "(a pair is a stat-arb, not a cluster)"
            )
        if not 0.0 < self.min_cluster_correlation <= 1.0:
            return False, (
                f"min_cluster_correlation={self.min_cluster_correlation} "
                f"must be in (0, 1]"
            )
        if self.z_score_threshold <= 0:
            return False, (
                f"z_score_threshold={self.z_score_threshold} must be > 0"
            )
        if self.max_z_score <= self.z_score_threshold:
            return False, (
                f"max_z_score={self.max_z_score} must be > z_score_threshold"
            )
        if self.max_spread <= 0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    @staticmethod
    def _mean_std(values: list[float]) -> tuple[float, float]:
        """Population mean + σ for a non-empty list. Returns (0, 0) if empty."""
        n = len(values)
        if n == 0:
            return 0.0, 0.0
        mean = sum(values) / n
        variance = sum((v - mean) ** 2 for v in values) / n
        return mean, variance ** 0.5

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a Signal trading the most-dislocated cluster member.

        Returns ``None`` when:
          * the cluster has fewer than ``min_cluster_size`` members,
          * any member is missing ``token_id`` or ``mid``,
          * the cluster correlation is below the floor,
          * the cluster σ is zero (no variance — can't z-score),
          * no member's z-score exceeds ``z_score_threshold``,
          * the dislocated member's spread is too wide,
          * the cluster already has an open position.
        """
        members = market_context.get("cluster_members") or []
        if not isinstance(members, list) or len(members) < self.min_cluster_size:
            return None

        # Parse + validate members.
        parsed: list[dict] = []
        for m in members:
            if not isinstance(m, dict):
                continue
            tid = m.get("token_id")
            mid = m.get("mid")
            if not tid or mid is None:
                continue
            try:
                mid_f = float(mid)
            except (TypeError, ValueError):
                continue
            if not 0.0 < mid_f < 1.0:
                continue
            parsed.append({
                "token_id": tid,
                "mid": mid_f,
                "historical_mean": m.get("historical_mean"),
                "historical_std": m.get("historical_std"),
                "spread": float(m.get("spread", 0.01)),
            })
        if len(parsed) < self.min_cluster_size:
            return None

        # Cluster correlation regime filter.
        cluster_corr = float(market_context.get("cluster_correlation", 1.0))
        if cluster_corr < self.min_cluster_correlation:
            return None

        # Compute the cluster's central tendency (mean of mids) and
        # cross-sectional σ (how dispersed the cluster is right now).
        mids = [m["mid"] for m in parsed]
        cluster_mean, cluster_std = self._mean_std(mids)
        if cluster_std < 1e-6:
            return None  # zero variance — cluster is perfectly aligned

        # Compute each member's z-score relative to the cluster mean.
        # A positive z = member is above the cluster mean (over-priced
        # relative to its peers); a negative z = below (under-priced).
        scored: list[tuple[float, dict, float]] = []
        for m in parsed:
            z = (m["mid"] - cluster_mean) / cluster_std
            scored.append((z, m, abs(z)))
        # Sort by |z| descending — the most-dislocated member is the
        # one we want to trade.
        scored.sort(key=lambda t: t[2], reverse=True)

        target_z, target, target_abs_z = scored[0]
        # Sanity: if the most-dislocated member isn't dislocated
        # enough, no signal.
        if target_abs_z < self.z_score_threshold:
            return None
        # Defensive: if |z| is absurdly large (data error / halt),
        # skip rather than trading into a broken book.
        if target_abs_z > self.max_z_score:
            return None

        # Spread regime filter on the target member.
        if target["spread"] >= self.max_spread:
            return None

        # Cluster key for open-position tracking — sorted token_ids
        # so the same cluster always hashes to the same key.
        cluster_key = "|".join(sorted(m["token_id"] for m in parsed))
        if cluster_key in self._open_clusters:
            return None

        # Direction: BUY when the target is below the cluster mean
        # (under-priced, expect reversion UP), SELL when above
        # (over-priced, expect reversion DOWN).
        if target_z < 0:
            action = "BUY"
            target_price = round(min(0.99, target["mid"] + 0.005), 4)
            direction = "long_underpriced_member"
        else:
            action = "SELL"
            target_price = round(max(0.01, target["mid"] - 0.005), 4)
            direction = "short_overpriced_member"

        # Edge = |z| × cluster_correlation / 10 (scaled to ~0-1).
        # A high-z dislocation in a tightly-coupled cluster has a
        # much stronger reversion expectation than the same z in a
        # loosely-coupled cluster.
        edge = target_abs_z * cluster_corr / 10.0
        # Confidence scales with |z| and cluster correlation.
        confidence = min(0.92, 0.5 + target_abs_z * 0.1 + cluster_corr * 0.15)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._open_clusters[cluster_key] = {
            "target_token": target["token_id"],
            "entry_z": target_z,
            "cluster_mean": cluster_mean,
            "cluster_std": cluster_std,
        }

        # Build the cluster_members metadata for diagnostics + routing.
        cluster_snapshot = [
            {
                "token_id": m["token_id"],
                "mid": m["mid"],
                "z_score": (m["mid"] - cluster_mean) / cluster_std,
            }
            for m in parsed
        ]

        return Signal(
            action=action,
            token_id=target["token_id"],
            size=1.0,  # sized in size_position
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"CrossMarket {action}: target={target['token_id'][:12]}, "
                f"z={target_z:+.2f}σ, cluster_mean={cluster_mean:.3f}, "
                f"cluster_σ={cluster_std:.3f}, corr={cluster_corr:+.2f}, "
                f"n={len(parsed)}"
            ),
            metadata={
                "direction": direction,
                "target_z_score": target_z,
                "cluster_mean": cluster_mean,
                "cluster_std": cluster_std,
                "cluster_correlation": cluster_corr,
                "cluster_key": cluster_key,
                "cluster_size": len(parsed),
                "cluster_members": cluster_snapshot,
                "model": "cluster_zscore_dislocation",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar (|z| × cluster_corr / 10)."""
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        Cross-market dislocation trades have moderate expected win
        rate (the strategy only acts on |z| ≥ 1.5σ in clusters with
        correlation ≥ 0.55 and ≥ 3 members). The 0.5× edge multiplier
        approximates half-Kelly on the edge estimate.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        kelly_size = signal.edge * capital * 0.5
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — limit order at signal.price.

        Cross-market entries are typically limit orders at or just
        inside the dislocated member's ask (for BUY) / bid (for SELL)
        to improve fill odds while waiting for cluster reversion.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        price = signal.price if signal.price is not None else 0.5
        return {
            "token_id": signal.token_id,
            "price": price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "cluster_zscore_dislocation",
                "target_z_score": signal.metadata.get("target_z_score"),
                "cluster_mean": signal.metadata.get("cluster_mean"),
                "cluster_std": signal.metadata.get("cluster_std"),
                "cluster_correlation": signal.metadata.get(
                    "cluster_correlation"
                ),
                "cluster_key": signal.metadata.get("cluster_key"),
                "cluster_size": signal.metadata.get("cluster_size"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the dislocation reverts or the cluster breaks.

        Exit triggers:
          * the target member's z-score has reverted inside ±0.5σ
            (the cluster has re-converged — dislocation resolved),
          * the cluster correlation has dropped below
            ``min_cluster_correlation`` (the cluster relationship
            broke — exit at market to free capital),
          * the position has been open longer than ``max_hold_seconds``
            (cluster dislocations typically resolve in hours, not
            days).
        """
        if not position:
            return None
        held_seconds = float(position.get("held_seconds", 0.0))
        max_hold = float(position.get("max_hold_seconds", 3600.0))
        if held_seconds >= max_hold:
            return {
                "reason": "max_hold_seconds reached — dislocation aged out",
                "held_seconds": held_seconds,
                "type": "market",
            }

        members = market_context.get("cluster_members") or []
        if not isinstance(members, list) or len(members) < self.min_cluster_size:
            return None

        parsed: list[float] = []
        for m in members:
            if not isinstance(m, dict):
                continue
            mid = m.get("mid")
            if mid is None:
                continue
            try:
                parsed.append(float(mid))
            except (TypeError, ValueError):
                continue
        if len(parsed) < self.min_cluster_size:
            return None

        cluster_mean, cluster_std = self._mean_std(parsed)
        if cluster_std < 1e-6:
            return None  # cluster perfectly aligned — dislocation resolved

        # Cluster broke: correlation dropped below the floor.
        cluster_corr = float(market_context.get("cluster_correlation", 1.0))
        if cluster_corr < self.min_cluster_correlation:
            return {
                "reason": "cluster correlation broke below floor",
                "current_corr": cluster_corr,
                "type": "market",
            }

        # Find the target token's current z-score.
        target_token = position.get("target_token", "")
        target_mid = None
        for m in members:
            if isinstance(m, dict) and m.get("token_id") == target_token:
                target_mid = m.get("mid")
                break
        if target_mid is None:
            return None
        try:
            target_mid_f = float(target_mid)
        except (TypeError, ValueError):
            return None

        current_z = (target_mid_f - cluster_mean) / cluster_std
        entry_z = float(position.get("entry_z", 0.0))

        # Reversion: z has reverted to inside ±0.5σ.
        if abs(current_z) <= 0.5:
            return {
                "reason": "cluster re-converged — dislocation resolved",
                "entry_z": entry_z,
                "current_z": current_z,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_cluster_size": self.min_cluster_size,
            "min_cluster_correlation": self.min_cluster_correlation,
            "z_score_threshold": self.z_score_threshold,
            "max_z_score": self.max_z_score,
            "max_spread": self.max_spread,
            "open_clusters": len(self._open_clusters),
            "open_cluster_keys": list(self._open_clusters.keys())[:5],
        })
        return base
