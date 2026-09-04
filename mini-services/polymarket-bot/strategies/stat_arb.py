"""
strategies/stat_arb.py — Cross-Category Statistical Arbitrage.

W22-3 — implements the unified strategy contract for the first of five
high-value strategies promoted from the PLANNED catalog (alongside
``event_driven.py``, ``convergence.py``, ``spread_capture.py``, and
``liquidity.py``). Maps to catalog id ``arb_cross_correlation`` —
"Cross-Category Arb — Pairs trading on economically correlated event
groups (crypto/macro)".

Signal logic
------------
Statistical arbitrage finds mispriced *correlated* markets — two
prediction-market contracts whose outcomes historically move together
(e.g. "BTC > $100k by Dec 31" and "ETH > $5k by Dec 31"). When the
two markets' implied probabilities diverge by more than the historical
spread threshold, the strategy trades the under-priced leg long and
the over-priced leg short, expecting the spread to revert to its
historical mean as the correlation reasserts itself.

Inputs (via ``market_context``)
-------------------------------
  * ``market1`` / ``market2`` (dict) — each carries ``token_id``,
    ``mid``, ``spread`` (optional), and ``volume`` (optional).
  * ``correlation`` (float ∈ (-1, 1)) — the rolling Pearson
    correlation between the two markets' mid-price series. The
    strategy only acts when ``|correlation| >= correlation_threshold``.
  * ``historical_spread_mean`` / ``historical_spread_std`` (float) —
    the rolling mean and standard deviation of the price-difference
    series. Used to z-score the current spread.
  * ``token_id`` (str, optional) — if only a single market is
    provided (no pair), the strategy returns ``None`` (stat-arb is
    inherently a paired strategy).

Edge estimation
---------------
The expected P&L per dollar at trade entry is the z-score of the
current spread (how many σ the spread is from its mean) times the
correlation strength — a high-z deviation in a tightly-correlated
pair has a much stronger reversion expectation than the same
deviation in a loosely-correlated pair.

Order routing
-------------
The async ``_run`` loop is intentionally a no-op stub; the strategy
is driven by the sync contract surface (``generate_signal``) for
backtest / dashboard introspection. Live trading wiring (paper /
CLOB order submission via ``BaseStrategy.submit_order``) is left
to a future wave — the strategy is honest about its status.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
CORRELATION_THRESHOLD = 0.70   # |corr| ≥ 0.70 ⇒ markets are tightly coupled
SPREAD_ZSCORE_THRESHOLD = 1.5  # spread must be ≥ 1.5σ from its mean to act
MAX_POSITION_PCT = 0.05        # never risk more than 5% of capital per pair
SCAN_INTERVAL = 60.0            # re-scan cadence (the async loop is a stub)


class StatisticalArbitrage(BaseStrategy):
    """Cross-category statistical arbitrage — pairs-trading correlated markets.

    BUY the under-priced leg and SELL the over-priced leg when the
    spread between two correlated markets exceeds a z-score threshold,
    expecting mean reversion as the correlation reasserts itself.
    """

    name = "stat_arb"

    def __init__(self) -> None:
        super().__init__()
        self.correlation_threshold: float = CORRELATION_THRESHOLD
        self.spread_zscore_threshold: float = SPREAD_ZSCORE_THRESHOLD
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-pair state — keyed by ``f"{token1}:{token2}"``.
        self._open_pairs: dict[str, dict] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Async scan loop stub.

        W22-3 — the live trading wiring (paper-sim create_order /
        clob_client.create_order) is intentionally deferred; the
        strategy is catalog-IMPLEMENTED so the dashboard surfaces it,
        but its canonical signal surface is the SYNC contract method
        ``generate_signal`` so backtest engines and dashboards can
        introspect it without spinning up an event loop. The loop
        below merely polls and logs so the registry lifecycle's
        ``start`` / ``stop`` plumbing works end-to-end.
        """
        log.info(
            "[stat_arb] Active (corr≥%.2f, z≥%.1fσ, max_pos=%.1f%%)",
            self.correlation_threshold,
            self.spread_zscore_threshold,
            self.max_position_pct * 100,
        )
        while self._running:
            try:
                # Real trading wiring deferred to a future wave; the
                # ``generate_signal`` contract method is the canonical
                # signal surface for now.
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[stat_arb] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W22-3 — StrategyContract implementations ─────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Cross-category statistical arbitrage — pairs-trades "
                "mispriced correlated markets (z-score reversion)."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "z_score_pair_reversion",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "correlation_threshold" in config:
            self.correlation_threshold = float(config["correlation_threshold"])
        if "spread_zscore_threshold" in config:
            self.spread_zscore_threshold = float(config["spread_zscore_threshold"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if not 0.0 < self.correlation_threshold <= 1.0:
            return False, (
                f"correlation_threshold={self.correlation_threshold} must be "
                f"in (0, 1]"
            )
        if self.spread_zscore_threshold <= 0.0:
            return False, (
                f"spread_zscore_threshold={self.spread_zscore_threshold} "
                f"must be > 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a stat-arb Signal representing the desired paired trade.

        Recognised ``market_context`` keys:

          * ``market1`` / ``market2`` (dict, required) — each carries
            ``token_id``, ``mid``, and optionally ``spread`` / ``volume``.
          * ``correlation`` (float ∈ (-1, 1), required) — rolling Pearson
            correlation between the two markets' mid-price series.
          * ``historical_spread_mean`` / ``historical_spread_std``
            (float, optional) — rolling mean & σ of the price-diff series.
            When ``std`` is 0 or missing, the strategy falls back to a
            raw-difference threshold (``SPREAD_THRESHOLD`` = 0.05).

        Returns ``None`` when:
          * either market dict is missing or lacks a mid price,
          * |correlation| < ``correlation_threshold`` (markets not coupled),
          * the spread z-score is inside the action band (no mispricing),
          * the pair is already open (one position per pair at a time).

        The returned ``Signal`` carries the under-priced leg's ``token_id``
        and a BUY action (the over-priced leg's SELL is the symmetric
        counterpart and is recorded in ``metadata`` for the order router).
        """
        m1 = market_context.get("market1") or {}
        m2 = market_context.get("market2") or {}
        token1 = m1.get("token_id")
        token2 = m2.get("token_id")
        mid1 = m1.get("mid")
        mid2 = m2.get("mid")
        if not token1 or not token2 or mid1 is None or mid2 is None:
            return None

        correlation = float(market_context.get("correlation", 0.0))
        if abs(correlation) < self.correlation_threshold:
            # Markets are not coupled tightly enough for stat-arb.
            return None

        # One position per pair — if the pair key already exists in the
        # open-pairs dict, skip (the contract test never starts the
        # async loop, so this guard only matters for live trading).
        pair_key = f"{token1}:{token2}" if token1 < token2 else f"{token2}:{token1}"
        if pair_key in self._open_pairs:
            return None

        # Compute the z-score of the current spread vs its historical
        # distribution. Fall back to raw difference if no history.
        spread = mid1 - mid2
        hist_mean = float(market_context.get("historical_spread_mean", 0.0))
        hist_std = float(market_context.get("historical_spread_std", 0.0))
        if hist_std > 1e-9:
            z = (spread - hist_mean) / hist_std
        else:
            # No history yet — use a raw 5% difference threshold.
            raw_threshold = 0.05
            z = abs(spread) / raw_threshold if raw_threshold > 0 else 0.0
            hist_mean = 0.0
            hist_std = raw_threshold

        if abs(z) < self.spread_zscore_threshold:
            # Spread is inside the action band — no mispricing.
            return None

        # BUY the under-priced leg, SELL the over-priced leg.
        if z > 0:
            # market1 is over-priced relative to market2 ⇒ BUY market2.
            action_token = token2
            action_price = float(mid2)
            action = "BUY"
            direction = "long_underpriced"
        else:
            # market2 is over-priced relative to market1 ⇒ BUY market1.
            action_token = token1
            action_price = float(mid1)
            action = "BUY"
            direction = "long_underpriced"

        # Edge estimate: |z| × |correlation| — a high-z deviation in a
        # tightly-coupled pair has a much stronger reversion expectation.
        edge = abs(z) * abs(correlation) / 10.0  # scale to ~0-1 range
        # Confidence is a sigmoid of |z| — higher z = higher confidence
        # the spread will revert (but capped to avoid over-betting).
        confidence = min(0.9, 0.5 + abs(z) * 0.1)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        # Reserve the pair so the next cycle doesn't double-fire.
        self._open_pairs[pair_key] = {
            "long_token": action_token,
            "entry_z": z,
            "entry_spread": spread,
        }

        return Signal(
            action=action,
            token_id=action_token,
            size=1.0,  # placeholder — sizing happens in size_position
            price=round(max(0.01, min(0.99, action_price)), 4),
            confidence=confidence,
            edge=edge,
            reason=(
                f"StatArb {direction}: z={z:+.2f}σ, corr={correlation:+.2f}, "
                f"spread={spread:+.4f}, pair={pair_key[:24]}"
            ),
            metadata={
                "pair_key": pair_key,
                "long_token": action_token,
                "short_token": token2 if action_token == token1 else token1,
                "z_score": z,
                "correlation": correlation,
                "spread": spread,
                "historical_spread_mean": hist_mean,
                "historical_spread_std": hist_std,
                "direction": direction,
                "model": "z_score_pair_reversion",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar at trade entry.

        For stat-arb, edge is the z-score × |correlation| / 10 (already
        computed in ``generate_signal`` and stored on ``signal.edge``).
        Falls back to 0 when the signal is None (defensive contract).
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size the paired position via edge-scaled fractional sizing.

        Position size = min(max_position_pct × capital, edge × capital × 0.5)
        bounded by available capital. The 0.5 multiplier is a
        fractional-Kelly conservative scaling: edge × 2 would be the
        full-Kelly fraction (assuming binary outcome), so 0.5 = quarter-
        Kelly.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        kelly_size = signal.edge * capital * 0.5
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params for the paired trade.

        The strategy enters BOTH legs simultaneously — the long leg
        via a limit at the current mid (the under-priced leg) and the
        short leg via a limit at the over-priced leg's mid. The
        contract return value encodes only the long-leg entry params
        (the canonical "primary" leg); the short-leg params are
        surfaced in ``metadata.short_leg`` for the order router.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        long_price = signal.price if signal.price is not None else 0.5
        short_token = signal.metadata.get("short_token", "")
        short_price = float(
            (market_context.get("market1") or {}).get("mid", 0.5)
            if short_token == (market_context.get("market1") or {}).get("token_id")
            else (market_context.get("market2") or {}).get("mid", 0.5)
        )
        return {
            "token_id": signal.token_id,
            "price": long_price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "z_score_pair_reversion",
                "pair_key": signal.metadata.get("pair_key"),
                "z_score": signal.metadata.get("z_score"),
                "short_leg": {
                    "token_id": short_token,
                    "price": round(max(0.01, min(0.99, short_price)), 4),
                    "side": "SELL",
                },
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the spread reverts to within ±0.5σ of the mean.

        The exit rule is symmetric to the entry rule: enter when
        |z| ≥ 1.5σ, exit when |z| ≤ 0.5σ. A time-based exit
        (``max_hold_seconds``) forces a flush if the pair doesn't
        converge within the configured horizon.
        """
        if not position:
            return None
        entry_z = position.get("entry_z", 0.0)
        if abs(entry_z) < 1e-9:
            return None
        # Compute the current z from the market context.
        m1 = market_context.get("market1") or {}
        m2 = market_context.get("market2") or {}
        mid1 = m1.get("mid")
        mid2 = m2.get("mid")
        if mid1 is None or mid2 is None:
            return None
        current_spread = mid1 - mid2
        hist_mean = float(market_context.get("historical_spread_mean", 0.0))
        hist_std = float(market_context.get("historical_spread_std", 0.0))
        if hist_std < 1e-9:
            return None
        current_z = (current_spread - hist_mean) / hist_std
        # Exit when the spread has reverted to within ±0.5σ of the mean.
        if abs(current_z) <= 0.5:
            return {
                "reason": "spread converged",
                "current_z": current_z,
                "entry_z": entry_z,
                "type": "limit",
            }
        # Time-based flush: if the position has been open longer than
        # ``max_hold_seconds``, exit regardless of z.
        max_hold = float(position.get("max_hold_seconds", 3600))
        held = float(position.get("held_seconds", 0.0))
        if held >= max_hold:
            return {
                "reason": "max_hold_seconds reached",
                "current_z": current_z,
                "entry_z": entry_z,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "correlation_threshold": self.correlation_threshold,
            "spread_zscore_threshold": self.spread_zscore_threshold,
            "max_position_pct": self.max_position_pct,
            "open_pairs": len(self._open_pairs),
            "open_pair_keys": list(self._open_pairs.keys())[:10],
        })
        return base
