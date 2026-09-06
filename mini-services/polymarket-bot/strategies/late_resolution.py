"""
strategies/late_resolution.py — Late-Resolution Decay-Curve Trader.

W44-1 — implements the unified strategy contract for the first of five
high-value strategies promoted from the PLANNED catalog in this wave.
Maps to catalog id ``arb_temporal_expiry`` — originally "Temporal
Expiry Curve — Relative value across same-underlying contracts with
differing expiries".

Signal logic
------------
As a prediction market approaches resolution its mid-price should
decay toward its terminal value (0.00 for a NO outcome, 1.00 for a
YES outcome). The closer to resolution and the higher the modeled
outcome certainty, the faster the decay should be. When the actual
mid is lagging the modeled decay curve (the market is "asleep at the
wheel"), the strategy buys the under-priced outcome; when the mid has
overshot the curve (the market is pricing in certainty that isn't
warranted by the remaining time-to-resolution), the strategy sells.

This is distinct from ``strategies/convergence.py`` which simply
checks ``|certainty - mid|`` against a static 3% threshold without
modelling how that gap should evolve as time-to-resolution shrinks.
Late Resolution uses a logistic decay model::

    fair(t) = certainty / (1 + exp(-k * (t_remaining - t_inflection)))

where ``t_inflection`` is the half-life (default 6h) and ``k`` is
the steepness (default 0.5). A market 24h out has barely decayed; a
market 1h out has nearly fully decayed. A signal fires only when the
observed mid is materially off the modeled fair value.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to trade.
  * ``mid`` (float ∈ (0, 1), required) — current market mid.
  * ``hours_to_resolution`` (float > 0, required) — time until the
    market resolves.
  * ``resolution_certainty`` (float ∈ (0, 1), required) — model's
    estimated probability the outcome resolves in the token's favor.
  * ``spread`` (float > 0, optional) — current bid-ask spread;
    skips ≥ ``max_spread``.
  * ``liquidity_usdc`` (float > 0, optional) — inside-book liquidity;
    skips < ``min_liquidity_usdc``.

Edge estimation
---------------
Edge is the gap between the modeled fair-price decay-curve value at
``hours_to_resolution`` and the observed mid — annualized for
comparison with other strategies' edges.

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
import math
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

# ── Strategy parameters ───────────────────────────────────────────────────────
MAX_HOURS_TO_RESOLUTION = 72.0   # only act in the final 72h (3 days)
MIN_HOURS_TO_RESOLUTION = 0.5    # don't act in the final 30 min (settlement noise)
MIN_RESOLUTION_CERTAINTY = 0.70  # only act when outcome is ≥ 70% certain
DECAY_HALF_LIFE_HOURS = 6.0     # logistic inflection point (50% decay at 6h)
DECAY_STEEPNESS = 0.5            # logistic steepness k
MIN_EDGE = 0.025                 # 2.5% gap required (covers spread + fees + slippage)
MAX_SPREAD = 0.06                # skip markets with ≥ 6% spreads
MIN_LIQUIDITY_USDC = 100.0       # need ≥ $100 inside liquidity
MAX_POSITION_PCT = 0.08          # 8% of capital per late-resolution trade
SCAN_INTERVAL = 60.0


def _logistic_fair(certainty: float, hours_left: float,
                   half_life: float, k: float) -> float:
    """Compute the modeled fair price at ``hours_left`` to resolution.

    The logistic form ``certainty / (1 + exp(-k * (t - half_life)))``
    is centred so that ``t = half_life`` ⇒ fair = certainty / 2 (half
    decayed). As ``t`` grows (lots of time left), the denominator
    blows up and fair → 0; as ``t`` shrinks (near resolution), the
    denominator → 1 and fair → certainty.

    The returned value is bounded to ``[0, certainty]``.
    """
    if hours_left <= 0:
        return certainty
    exponent = -k * (hours_left - half_life)
    # Numerical guard: exp overflow when exponent is very negative.
    try:
        denom = 1.0 + math.exp(exponent)
    except OverflowError:
        denom = float("inf")
    fair = certainty / denom if denom != float("inf") else 0.0
    return max(0.0, min(certainty, fair))


class LateResolution(BaseStrategy):
    """Late-resolution decay-curve trader.

    BUY when the observed mid is below the modeled fair-price decay
    value (market is under-pricing the near-certain outcome). SELL
    when the observed mid is above the modeled fair value (market is
    over-pricing the outcome relative to its remaining time-to-decay).
    """

    name = "late_resolution"

    def __init__(self) -> None:
        super().__init__()
        self.max_hours_to_resolution: float = MAX_HOURS_TO_RESOLUTION
        self.min_hours_to_resolution: float = MIN_HOURS_TO_RESOLUTION
        self.min_resolution_certainty: float = MIN_RESOLUTION_CERTAINTY
        self.decay_half_life_hours: float = DECAY_HALF_LIFE_HOURS
        self.decay_steepness: float = DECAY_STEEPNESS
        self.min_edge: float = MIN_EDGE
        self.max_spread: float = MAX_SPREAD
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Per-token entered tracking — one position per token at a time.
        self._entered_tokens: dict[str, float] = {}

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
            "[late_resolution] Active (hours<=%.1f, certainty>=%.2f, edge>=%.2f%%)",
            self.max_hours_to_resolution,
            self.min_resolution_certainty,
            self.min_edge * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[late_resolution] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W44-1 — StrategyContract implementations ────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Late-resolution decay-curve trader — models the price "
                "decay curve as resolution approaches and trades when "
                "the observed mid deviates from the modeled fair value."
            ),
            "author": "polymarket-bot",
            "category": "arbitrage",
            "model": "logistic_decay_curve",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "max_hours_to_resolution" in config:
            self.max_hours_to_resolution = float(config["max_hours_to_resolution"])
        if "min_hours_to_resolution" in config:
            self.min_hours_to_resolution = float(config["min_hours_to_resolution"])
        if "min_resolution_certainty" in config:
            self.min_resolution_certainty = float(config["min_resolution_certainty"])
        if "decay_half_life_hours" in config:
            self.decay_half_life_hours = float(config["decay_half_life_hours"])
        if "decay_steepness" in config:
            self.decay_steepness = float(config["decay_steepness"])
        if "min_edge" in config:
            self.min_edge = float(config["min_edge"])
        if "max_spread" in config:
            self.max_spread = float(config["max_spread"])
        if "min_liquidity_usdc" in config:
            self.min_liquidity_usdc = float(config["min_liquidity_usdc"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.max_hours_to_resolution <= 0:
            return False, (
                f"max_hours_to_resolution={self.max_hours_to_resolution} "
                f"must be > 0"
            )
        if not 0 <= self.min_hours_to_resolution < self.max_hours_to_resolution:
            return False, (
                f"min_hours_to_resolution={self.min_hours_to_resolution} "
                f"must be in [0, max_hours_to_resolution)"
            )
        if not 0.0 <= self.min_resolution_certainty <= 1.0:
            return False, (
                f"min_resolution_certainty={self.min_resolution_certainty} "
                f"must be in [0, 1]"
            )
        if self.decay_half_life_hours <= 0:
            return False, (
                f"decay_half_life_hours={self.decay_half_life_hours} must be > 0"
            )
        if self.decay_steepness <= 0:
            return False, (
                f"decay_steepness={self.decay_steepness} must be > 0"
            )
        if self.min_edge < 0:
            return False, f"min_edge={self.min_edge} must be >= 0"
        if self.max_spread <= 0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if self.min_liquidity_usdc < 0:
            return False, (
                f"min_liquidity_usdc={self.min_liquidity_usdc} must be >= 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a Signal representing a late-resolution decay-curve trade.

        Returns ``None`` when:
          * required inputs are missing or non-numeric,
          * the market is outside the [min_hours, max_hours] window,
          * the outcome certainty is below ``min_resolution_certainty``,
          * the spread / liquidity regime filters trip,
          * the gap between modeled fair and observed mid is below
            ``min_edge``,
          * the token was already entered (one position per token).
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        hours = market_context.get("hours_to_resolution")
        certainty = market_context.get("resolution_certainty")
        if not token_id or mid is None or hours is None or certainty is None:
            return None

        try:
            hours_f = float(hours)
            certainty_f = float(certainty)
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        # Resolution-window regime filters.
        if hours_f <= self.min_hours_to_resolution:
            return None
        if hours_f > self.max_hours_to_resolution:
            return None
        if certainty_f < self.min_resolution_certainty:
            return None

        # Spread / liquidity regime filters.
        spread = float(market_context.get("spread", 0.01))
        if spread >= self.max_spread:
            return None
        liquidity = float(market_context.get("liquidity_usdc", 1e9))
        if liquidity < self.min_liquidity_usdc:
            return None

        # Already entered? Skip — one position per token at a time.
        if token_id in self._entered_tokens:
            return None

        # Modeled fair price given the remaining time-to-resolution.
        fair = _logistic_fair(
            certainty_f, hours_f,
            self.decay_half_life_hours, self.decay_steepness,
        )
        # Edge = observed - fair (signed). Positive means market is
        # over-priced relative to its decay trajectory (SELL); negative
        # means under-priced (BUY).
        signed_edge = mid_f - fair
        if abs(signed_edge) < self.min_edge:
            return None

        # Direction: BUY when mid < fair (under-priced), SELL when
        # mid > fair (over-priced).
        if signed_edge < 0:
            action = "BUY"
            target_price = round(min(0.99, mid_f + 0.005), 4)
            direction = "long_underpriced_outcome"
        else:
            action = "SELL"
            target_price = round(max(0.01, mid_f - 0.005), 4)
            direction = "short_overpriced_outcome"

        # Annualized edge for diagnostics (e.g. 5% in 6h ≈ 73x/yr).
        annualized = abs(signed_edge) / max(hours_f / 24.0 / 365.0, 1e-9)
        # Confidence scales with both the outcome certainty and the
        # gap magnitude — a high-certainty, large-gap trade is more
        # confident than a marginal-certainty, small-gap trade.
        confidence = min(0.95, 0.5 + certainty_f * 0.3 + abs(signed_edge) * 0.5)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._entered_tokens[token_id] = hours_f

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,  # sized in size_position
            price=target_price,
            confidence=confidence,
            edge=abs(signed_edge),
            reason=(
                f"LateResolution {action}: fair={fair:.3f}, mid={mid_f:.3f}, "
                f"gap={signed_edge * 100:+.2f}%, hours_left={hours_f:.1f}h, "
                f"certainty={certainty_f:.2f}, ann~{annualized:.1f}x"
            ),
            metadata={
                "direction": direction,
                "modeled_fair": fair,
                "observed_mid": mid_f,
                "signed_edge": signed_edge,
                "resolution_certainty": certainty_f,
                "hours_to_resolution": hours_f,
                "decay_half_life_hours": self.decay_half_life_hours,
                "annualized_edge": annualized,
                "spread": spread,
                "liquidity_usdc": liquidity,
                "model": "logistic_decay_curve",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar at trade entry.

        For late-resolution trades, the edge is the gap between the
        modeled decay-curve fair value and the observed mid — a 5%
        gap means a 5% expected return on capital as the market
        decays to its fair value before resolution.
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        Late-resolution trades have high expected win rate (the
        strategy only acts when certainty ≥ 0.70 AND the gap is ≥
        2.5%), so the sizing is moderate (0.75× edge multiplier =
        three-quarter Kelly on the certainty-based edge estimate).
        Bounded by ``max_position_pct`` of capital.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        # Three-quarter Kelly: edge × 0.75 (vs full Kelly at 1.0).
        kelly_size = signal.edge * capital * 0.75
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — limit order at signal.price.

        Late-resolution entries are typically limit orders at or just
        inside the ask (for BUY) / bid (for SELL) to improve fill
        odds in the thin-liquidity near-resolution regime.
        """
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
                "model": "logistic_decay_curve",
                "modeled_fair": signal.metadata.get("modeled_fair"),
                "hours_to_resolution": signal.metadata.get(
                    "hours_to_resolution"
                ),
                "resolution_certainty": signal.metadata.get(
                    "resolution_certainty"
                ),
                "annualized_edge": signal.metadata.get("annualized_edge"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when the gap closes or the thesis breaks.

        Exit triggers:
          * the gap between observed mid and modeled fair has closed
            below ``min_edge / 2`` (the trade thesis has played out),
          * the certainty drops below ``min_resolution_certainty`` (the
            underlying outcome-probability thesis broke),
          * the market has resolved (``hours_to_resolution`` <= 0).
        """
        if not position:
            return None
        mid = market_context.get("mid")
        hours = market_context.get("hours_to_resolution")
        certainty = market_context.get("resolution_certainty")
        if mid is None or hours is None or certainty is None:
            return None
        try:
            mid_f = float(mid)
            hours_f = float(hours)
            certainty_f = float(certainty)
        except (TypeError, ValueError):
            return None

        # Thesis-broke: certainty dropped below the floor.
        if certainty_f < self.min_resolution_certainty:
            return {
                "reason": "certainty dropped below floor — thesis broke",
                "current_certainty": certainty_f,
                "type": "market",
            }

        # Market resolved — exit at market to free capital.
        if hours_f <= 0:
            return {
                "reason": "market resolved — exit at market",
                "hours_left": hours_f,
                "type": "market",
            }

        # Gap closed: re-compute fair at current hours and check the
        # remaining gap.
        fair = _logistic_fair(
            certainty_f, hours_f,
            self.decay_half_life_hours, self.decay_steepness,
        )
        current_gap = abs(mid_f - fair)
        if current_gap < self.min_edge / 2.0:
            return {
                "reason": "gap closed — decay-curve thesis played out",
                "current_fair": fair,
                "current_mid": mid_f,
                "current_gap": current_gap,
                "type": "limit",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "max_hours_to_resolution": self.max_hours_to_resolution,
            "min_hours_to_resolution": self.min_hours_to_resolution,
            "min_resolution_certainty": self.min_resolution_certainty,
            "decay_half_life_hours": self.decay_half_life_hours,
            "decay_steepness": self.decay_steepness,
            "min_edge": self.min_edge,
            "max_spread": self.max_spread,
            "min_liquidity_usdc": self.min_liquidity_usdc,
            "entered_tokens": len(self._entered_tokens),
        })
        return base
