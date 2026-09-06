"""
strategies/ensemble.py — Multi-Strategy Signal Ensemble with Fractional Kelly Sizing.

W44-1 — implements the unified strategy contract for the second of
five high-value strategies promoted from the PLANNED catalog in
this wave. Maps to catalog id ``ml_fractional_kelly`` — originally
"Fractional Kelly Sizing — Quant strategy sizing all trades with
dynamic Kelly Criterion f*".

Signal logic
------------
The ensemble strategy aggregates BUY/SELL signals from multiple
upstream strategies and outputs a single consensus Signal. Each
upstream signal carries an ``edge`` estimate (expected P&L per
dollar) and a ``confidence`` (probability the edge estimate is
correct). The ensemble:

  1. Filters out signals whose confidence is below ``min_confidence``.
  2. Weights each surviving signal by ``weight`` (per-strategy
     historical-accuracy weight, default 1.0 = equal weighting).
  3. Computes the weighted-vote direction: BUY if the weighted BUY
     mass exceeds the weighted SELL mass by ≥ ``min_vote_margin``,
     SELL in the symmetric case, HOLD otherwise.
  4. Aggregates the edge estimate as the weighted-mean edge of the
     concurring signals (only signals whose direction matches the
     ensemble's chosen direction contribute).
  5. Sizes the position via fractional Kelly: ``f* = edge / odds``
     where ``odds ≈ 1 / mean_confidence`` (the standard Kelly formula
     for a binary outcome), scaled by ``kelly_fraction`` (default
     0.25 = quarter-Kelly for safety).

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to trade.
  * ``mid`` (float ∈ (0, 1), required) — the current market mid.
  * ``sub_signals`` (list[dict], required) — each dict carries:
      - ``action`` (str: "BUY" / "SELL" / "HOLD")
      - ``edge`` (float, expected P&L per dollar)
      - ``confidence`` (float ∈ [0, 1])
      - ``weight`` (float, optional; default 1.0)
      - ``source`` (str, optional; the contributing strategy name)
  * ``spread`` (float > 0, optional) — current bid-ask spread;
    skips ≥ ``max_spread`` (no point ensembling on an untradeable book).

Edge estimation
---------------
The aggregated edge is the weighted-mean of the concurring signals'
edges. This naturally discounts ensembles where the concurring
signals have weak edges and amplifies ensembles where they have
strong edges.

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
MIN_CONFIDENCE = 0.40         # individual signals must be ≥ 40% confident
MIN_VOTE_MARGIN = 0.10         # weighted BUY − SELL margin must be ≥ 10%
KELLY_FRACTION = 0.25          # quarter-Kelly for safety (full Kelly is volatile)
MIN_EDGE = 0.015               # aggregated edge must be ≥ 1.5% to act
MAX_SPREAD = 0.05              # skip markets with ≥ 5% spreads
MAX_POSITION_PCT = 0.04        # 4% of capital per ensemble trade
MIN_SUB_SIGNALS = 2            # need ≥ 2 sub-signals to ensemble
SCAN_INTERVAL = 30.0


class Ensemble(BaseStrategy):
    """Multi-strategy signal ensemble with fractional Kelly sizing.

    Aggregates BUY/SELL signals from N upstream strategies, votes on
    a single consensus direction, and sizes the resulting position
    via quarter-Kelly on the aggregated edge estimate.
    """

    name = "ensemble"

    def __init__(self) -> None:
        super().__init__()
        self.min_confidence: float = MIN_CONFIDENCE
        self.min_vote_margin: float = MIN_VOTE_MARGIN
        self.kelly_fraction: float = KELLY_FRACTION
        self.min_edge: float = MIN_EDGE
        self.max_spread: float = MAX_SPREAD
        self.max_position_pct: float = MAX_POSITION_PCT
        self.min_sub_signals: int = MIN_SUB_SIGNALS
        self._interval: float = SCAN_INTERVAL
        # Per-token cooldown: don't re-ensemble the same token too soon.
        self._last_act_at: dict[str, float] = {}
        self._cooldown_seconds: float = 60.0
        # Rolling stats: total sub-signals seen, ensemble decisions made.
        self._sub_signal_count: int = 0
        self._ensemble_decisions: dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}

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
            "[ensemble] Active (min_conf>=%.2f, vote_margin>=%.2f, kelly×%.2f)",
            self.min_confidence, self.min_vote_margin, self.kelly_fraction,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ensemble] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W44-1 — StrategyContract implementations ────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Multi-strategy signal ensemble — aggregates BUY/SELL "
                "signals from N upstream strategies and sizes the "
                "consensus position via fractional Kelly on the "
                "aggregated edge estimate."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "weighted_vote_ensemble_with_fractional_kelly",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "min_confidence" in config:
            self.min_confidence = float(config["min_confidence"])
        if "min_vote_margin" in config:
            self.min_vote_margin = float(config["min_vote_margin"])
        if "kelly_fraction" in config:
            self.kelly_fraction = float(config["kelly_fraction"])
        if "min_edge" in config:
            self.min_edge = float(config["min_edge"])
        if "max_spread" in config:
            self.max_spread = float(config["max_spread"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "min_sub_signals" in config:
            self.min_sub_signals = int(config["min_sub_signals"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])
        if "cooldown_seconds" in config:
            self._cooldown_seconds = float(config["cooldown_seconds"])

    def validate(self) -> tuple[bool, str]:
        if not 0.0 <= self.min_confidence <= 1.0:
            return False, (
                f"min_confidence={self.min_confidence} must be in [0, 1]"
            )
        if not 0.0 <= self.min_vote_margin <= 1.0:
            return False, (
                f"min_vote_margin={self.min_vote_margin} must be in [0, 1]"
            )
        if not 0.0 < self.kelly_fraction <= 1.0:
            return False, (
                f"kelly_fraction={self.kelly_fraction} must be in (0, 1]"
            )
        if self.min_edge < 0:
            return False, f"min_edge={self.min_edge} must be >= 0"
        if self.max_spread <= 0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        if self.min_sub_signals < 1:
            return False, (
                f"min_sub_signals={self.min_sub_signals} must be >= 1"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Aggregate ``sub_signals`` into a single ensemble Signal.

        Returns ``None`` when:
          * ``token_id`` or ``mid`` is missing,
          * the spread is too wide,
          * the number of qualifying sub-signals is < ``min_sub_signals``,
          * no signal meets the ``min_confidence`` threshold,
          * the weighted vote margin is below ``min_vote_margin``,
          * the aggregated edge is below ``min_edge``,
          * the token is on cooldown.
        """
        import time

        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None

        # Spread regime filter.
        spread = float(market_context.get("spread", 0.01))
        if spread >= self.max_spread:
            return None

        sub_signals = market_context.get("sub_signals") or []
        if not isinstance(sub_signals, list) or len(sub_signals) < self.min_sub_signals:
            return None

        # Per-token cooldown.
        now = float(market_context.get("now", time.time()))
        last_act = self._last_act_at.get(token_id, 0.0)
        if now - last_act < self._cooldown_seconds:
            return None

        # Filter low-confidence signals and tally weighted votes.
        buy_mass = 0.0
        sell_mass = 0.0
        buy_edges: list[tuple[float, float, float]] = []  # (edge, confidence, weight)
        sell_edges: list[tuple[float, float, float]] = []
        qualifying = 0
        for sub in sub_signals:
            if not isinstance(sub, dict):
                continue
            action = sub.get("action", "HOLD")
            confidence = float(sub.get("confidence", 0.0))
            edge = float(sub.get("edge", 0.0))
            weight = float(sub.get("weight", 1.0))
            if confidence < self.min_confidence:
                continue
            qualifying += 1
            self._sub_signal_count += 1
            if action == "BUY":
                buy_mass += weight * confidence
                buy_edges.append((edge, confidence, weight))
            elif action == "SELL":
                sell_mass += weight * confidence
                sell_edges.append((edge, confidence, weight))

        if qualifying < self.min_sub_signals:
            return None

        total_mass = buy_mass + sell_mass
        if total_mass <= 0:
            return None

        # Weighted vote margin: |buy_share - sell_share|.
        buy_share = buy_mass / total_mass
        sell_share = sell_mass / total_mass
        margin = buy_share - sell_share

        if abs(margin) < self.min_vote_margin:
            self._ensemble_decisions["HOLD"] += 1
            return None

        if margin > 0:
            action = "BUY"
            concurring = buy_edges
            concurring_mass = buy_mass
        else:
            action = "SELL"
            concurring = sell_edges
            concurring_mass = sell_mass

        # Aggregated edge = weighted-mean of the concurring signals' edges.
        # Weight = (sub.weight × sub.confidence) — high-confidence, heavily-
        # weighted sub-signals dominate the aggregated edge.
        total_weight = sum(w * c for (_, c, w) in concurring)
        if total_weight <= 0:
            return None
        aggregated_edge = sum(e * c * w for (e, c, w) in concurring) / total_weight
        if aggregated_edge < self.min_edge:
            self._ensemble_decisions["HOLD"] += 1
            return None

        # Aggregated confidence = concurring_mass / total_mass — the
        # share of the total weighted vote that concurs with the
        # chosen direction. Bounded to [0, 0.95].
        aggregated_confidence = min(0.95, concurring_mass / total_mass)

        mid_f = float(mid)
        if action == "BUY":
            target_price = round(min(0.99, mid_f + 0.005), 4)
        else:
            target_price = round(max(0.01, mid_f - 0.005), 4)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._ensemble_decisions[action] += 1
        self._last_act_at[token_id] = now

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,  # sized in size_position
            price=target_price,
            confidence=aggregated_confidence,
            edge=aggregated_edge,
            reason=(
                f"Ensemble {action}: buy_mass={buy_mass:.2f}, "
                f"sell_mass={sell_mass:.2f}, margin={margin * 100:+.1f}%, "
                f"edge={aggregated_edge * 100:.2f}%, "
                f"conf={aggregated_confidence:.2f}, n={qualifying}"
            ),
            metadata={
                "model": "weighted_vote_ensemble_with_fractional_kelly",
                "buy_mass": buy_mass,
                "sell_mass": sell_mass,
                "vote_margin": margin,
                "qualifying_signals": qualifying,
                "concurring_signals": len(concurring),
                "aggregated_confidence": aggregated_confidence,
                "kelly_fraction": self.kelly_fraction,
                "spread": spread,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = aggregated expected P&L per dollar (already computed)."""
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via fractional Kelly: ``f* = edge / odds × kelly_fraction``.

        For a binary outcome, ``odds ≈ 1 / mean_confidence`` (the
        standard Kelly approximation). We use ``kelly_fraction``
        (default 0.25 = quarter-Kelly) for safety — full Kelly is
        notoriously volatile on small samples.

        The result is bounded by ``max_position_pct × capital`` and
        any per-market risk cap from ``risk_params``.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        # Kelly fraction: edge × confidence / (1 - confidence) is the
        # binary-outcome Kelly formula (f = p - q/b where b = odds).
        # For a binary outcome with win prob = confidence and odds ≈ 1,
        # f = confidence - (1 - confidence) = 2 * confidence - 1.
        # We approximate edge × confidence / (1 - confidence) (edge-
        # scaled Kelly) which is the more conservative form.
        conf = max(signal.confidence, 1e-3)
        if conf >= 0.99:
            # Near-certainty — full Kelly would be enormous; cap at
            # the max position.
            kelly_f = self.kelly_fraction
        else:
            kelly_f = (signal.edge * conf / (1.0 - conf)) * self.kelly_fraction
        kelly_size = kelly_f * capital
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — limit order at signal.price."""
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        price = signal.price if signal.price is not None else float(
            market_context.get("mid", 0.5)
        )
        return {
            "token_id": signal.token_id,
            "price": price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "weighted_vote_ensemble_with_fractional_kelly",
                "aggregated_confidence": signal.metadata.get(
                    "aggregated_confidence"
                ),
                "vote_margin": signal.metadata.get("vote_margin"),
                "qualifying_signals": signal.metadata.get("qualifying_signals"),
                "kelly_fraction": signal.metadata.get("kelly_fraction"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the ensemble flips or the position has aged out.

        Exit triggers:
          * a fresh ensemble call on the current ``sub_signals``
            produces the OPPOSITE direction (consensus flipped),
          * the position has been open longer than ``max_hold_seconds``
            (the ensemble thesis has aged out).
        """
        if not position:
            return None
        held_seconds = float(position.get("held_seconds", 0.0))
        max_hold = float(position.get("max_hold_seconds", 600.0))
        if held_seconds >= max_hold:
            return {
                "reason": "max_hold_seconds reached — ensemble thesis aged out",
                "held_seconds": held_seconds,
                "type": "market",
            }
        # Re-evaluate the ensemble on the current market context to
        # check if the consensus has flipped.
        entry_action = position.get("entry_action", "")
        fresh = self.generate_signal(market_context)
        if fresh is None:
            return None
        if (entry_action == "BUY" and fresh.action == "SELL") or \
           (entry_action == "SELL" and fresh.action == "BUY"):
            return {
                "reason": "ensemble consensus flipped — opposite signal",
                "entry_action": entry_action,
                "new_action": fresh.action,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_confidence": self.min_confidence,
            "min_vote_margin": self.min_vote_margin,
            "kelly_fraction": self.kelly_fraction,
            "min_edge": self.min_edge,
            "max_spread": self.max_spread,
            "min_sub_signals": self.min_sub_signals,
            "cooldown_seconds": self._cooldown_seconds,
            "tokens_in_cooldown": len(self._last_act_at),
            "sub_signal_count": self._sub_signal_count,
            "ensemble_decisions": dict(self._ensemble_decisions),
        })
        return base
