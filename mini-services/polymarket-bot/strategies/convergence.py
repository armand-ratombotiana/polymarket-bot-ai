"""
strategies/convergence.py — Resolution Convergence Sniper.

W22-3 — implements the unified strategy contract for the third of
five high-value strategies promoted from the PLANNED catalog. Maps
to catalog id ``event_resolution_sniper`` — "Resolution Expiry
Sniper — High-conviction sniper executing in final 24h of
near-certain events".

Signal logic
------------
Prediction markets converge to their resolution price (0.00 or 1.00)
as the resolution time approaches and the outcome becomes certain.
A market priced at 0.95 with 6 hours to resolution and a 99%
certain outcome is "almost free money": buy YES at 0.95, hold to
resolution, collect 1.00 — a 5% return in 6 hours (annualized
~146x). The convergence sniper identifies these near-certain
end-of-life markets and snipes them.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to trade.
  * ``mid`` (float ∈ (0, 1), required) — the current market mid.
  * ``hours_to_resolution`` (float > 0, required) — time until the
    market resolves.
  * ``resolution_certainty`` (float ∈ [0, 1], required) — the model's
    estimated probability that the outcome will resolve in the
    token's favor (e.g. 0.99 for "near-certain YES").
  * ``spread`` (float > 0, optional) — the current bid-ask spread.
    Wide spreads near resolution signal poor liquidity; the strategy
    skips markets with spreads ≥ ``MAX_SPREAD``.
  * ``liquidity_usdc`` (float > 0, optional) — the market's available
    liquidity (depth at the inside quote). Skips markets with
    liquidity < ``MIN_LIQUIDITY_USDC`` to avoid slippage blowing the
    edge.

Edge estimation
---------------
The expected edge is the gap between ``resolution_certainty`` and
``mid`` (i.e. ``certainty - mid`` for a BUY, ``(1 - certainty) -
(1 - mid)`` for a SELL), annualized for comparison with other
strategies.

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
MAX_HOURS_TO_RESOLUTION = 24.0  # only act in the final 24h
MIN_RESOLUTION_CERTAINTY = 0.85  # only act when outcome ≥ 85% certain
MIN_EDGE = 0.03                  # 3% gap required (covers spread + fees)
MAX_SPREAD = 0.05                # skip markets with ≥ 5% spreads
MIN_LIQUIDITY_USDC = 50.0        # need ≥ $50 liquidity to enter
MAX_POSITION_PCT = 0.10           # higher than stat-arb — these are
                                   # "near-certain" plays, so 10% is OK
SCAN_INTERVAL = 60.0


class Convergence(BaseStrategy):
    """Resolution-convergence sniper — buys near-certain end-of-life markets."""

    name = "convergence"

    def __init__(self) -> None:
        super().__init__()
        self.max_hours_to_resolution: float = MAX_HOURS_TO_RESOLUTION
        self.min_resolution_certainty: float = MIN_RESOLUTION_CERTAINTY
        self.min_edge: float = MIN_EDGE
        self.max_spread: float = MAX_SPREAD
        self.min_liquidity_usdc: float = MIN_LIQUIDITY_USDC
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Track which markets we've already entered so we don't double-up.
        self._entered_tokens: dict[str, float] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def _run(self) -> None:
        """Async scan loop stub.

        W22-3 — the live trading wiring (paper-sim create_order /
        clob_client.create_order) is intentionally deferred; the
        strategy is catalog-IMPLEMENTED so the dashboard surfaces it,
        but its canonical signal surface is the SYNC contract method
        ``generate_signal``. The loop polls and logs so the registry
        lifecycle's ``start`` / ``stop`` plumbing works end-to-end.
        """
        log.info(
            "[convergence] Active (hours<=%.1f, certainty>=%.2f, edge>=%.2f%%)",
            self.max_hours_to_resolution,
            self.min_resolution_certainty,
            self.min_edge * 100,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[convergence] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W22-3 — StrategyContract implementations ─────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Resolution-convergence sniper — buys near-certain "
                "end-of-life prediction markets in the final 24h for "
                "low-risk near-1.00 (or near-0.00) payouts."
            ),
            "author": "polymarket-bot",
            "category": "event_driven",
            "model": "resolution_convergence_sniper",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "max_hours_to_resolution" in config:
            self.max_hours_to_resolution = float(config["max_hours_to_resolution"])
        if "min_resolution_certainty" in config:
            self.min_resolution_certainty = float(config["min_resolution_certainty"])
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
        if self.max_hours_to_resolution <= 0.0:
            return False, (
                f"max_hours_to_resolution={self.max_hours_to_resolution} "
                f"must be > 0"
            )
        if not 0.0 <= self.min_resolution_certainty <= 1.0:
            return False, (
                f"min_resolution_certainty={self.min_resolution_certainty} "
                f"must be in [0, 1]"
            )
        if self.min_edge < 0.0:
            return False, f"min_edge={self.min_edge} must be >= 0"
        if self.max_spread <= 0.0:
            return False, f"max_spread={self.max_spread} must be > 0"
        if self.min_liquidity_usdc < 0.0:
            return False, (
                f"min_liquidity_usdc={self.min_liquidity_usdc} must be >= 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a convergence Signal for a near-certain end-of-life market.

        Returns ``None`` when:
          * ``token_id`` / ``mid`` / ``hours_to_resolution`` /
            ``resolution_certainty`` are missing,
          * the market is outside the final-24h window,
          * the outcome isn't certain enough (< ``min_resolution_certainty``),
          * the edge (certainty − mid) is below ``min_edge``,
          * the spread is too wide (≥ ``max_spread``),
          * the liquidity is too thin (< ``min_liquidity_usdc``),
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

        if hours_f <= 0 or hours_f > self.max_hours_to_resolution:
            return None
        if certainty_f < self.min_resolution_certainty:
            return None
        # Spread & liquidity regime filters.
        spread = float(market_context.get("spread", 0.01))
        if spread >= self.max_spread:
            return None
        liquidity = float(market_context.get("liquidity_usdc", 1e9))
        if liquidity < self.min_liquidity_usdc:
            return None

        # Already entered? Skip — one position per token.
        if token_id in self._entered_tokens:
            return None

        # Edge = |certainty - mid| — the gap we expect to capture when
        # the market resolves in our favor.
        edge = certainty_f - mid_f
        if abs(edge) < self.min_edge:
            return None

        # Direction: BUY when certainty > mid (market underpriced the
        # likely outcome), SELL when certainty < mid (market overpriced).
        if edge > 0:
            action = "BUY"
            # Enter near the ask (slightly above mid) to improve fill odds.
            target_price = round(min(0.99, mid_f + 0.005), 4)
            direction = "long_yes_outcome"
        else:
            action = "SELL"
            target_price = round(max(0.01, mid_f - 0.005), 4)
            direction = "short_yes_outcome"

        # Annualized edge for diagnostics — a 5% edge in 6 hours = 73x/year.
        annualized = (abs(edge) / max(hours_f / 24.0 / 365.0, 1e-9))
        # Confidence = the certainty itself — a 99%-certain outcome is
        # 99% likely to resolve in our favor. Capped at 0.99 (no strategy
        # is ever 100% confident — black swans happen).
        confidence = min(0.99, certainty_f)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        self._entered_tokens[token_id] = hours_f

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,  # sized in size_position
            price=target_price,
            confidence=confidence,
            edge=abs(edge),
            reason=(
                f"Convergence {action}: certainty={certainty_f:.3f}, "
                f"mid={mid_f:.3f}, edge={edge * 100:+.2f}%, "
                f"hours_left={hours_f:.1f}h, "
                f"annualized~{annualized:.1f}x"
            ),
            metadata={
                "direction": direction,
                "resolution_certainty": certainty_f,
                "hours_to_resolution": hours_f,
                "market_mid": mid_f,
                "raw_edge": edge,
                "annualized_edge": annualized,
                "spread": spread,
                "liquidity_usdc": liquidity,
                "model": "resolution_convergence_sniper",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected P&L per dollar at trade entry.

        For convergence trades, the edge is the gap between the
        resolution certainty and the current mid — a near-certain
        outcome priced at 0.95 has an edge of 0.05 (5% return on
        capital at resolution).
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size via edge-scaled fractional sizing capped at max_position_pct.

        Convergence trades have very high expected win rate (the
        strategy only acts when certainty ≥ 0.85), so the sizing is
        more aggressive than stat-arb (1.0× edge multiplier = full
        Kelly on the certainty-based edge estimate). Bounded by
        ``max_position_pct`` of capital.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        max_size = self.max_position_pct * capital
        # For convergence, the edge IS the expected return at resolution
        # (a 5% edge = 5% return on capital). So size = edge × capital
        # is a reasonable approximation (full-Kelly on a binary outcome
        # with p=certainty is f* = edge / odds, where odds ≈ 1/p).
        certainty = signal.metadata.get("resolution_certainty", 0.9)
        odds = max(1.0, 1.0 / max(certainty, 0.01))
        kelly_fraction = signal.edge / odds if odds > 0 else 0.0
        kelly_size = kelly_fraction * capital
        risk_cap = float(risk_params.get("max_position_per_market", max_size))
        return min(max_size, kelly_size, risk_cap, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — limit order at signal.price.

        Convergence entries are typically limit orders at or just
        inside the ask (for BUY) to capture the spread and improve
        fill odds in the final-24h thin-liquidity regime.
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
                "model": "resolution_convergence_sniper",
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
        """Exit at resolution — convergence trades hold to expiry.

        The exit rule is straightforward: hold the position to
        resolution (the strategy's thesis is "the market will resolve
        in our favor"). The only early-exit triggers are:
          * the certainty drops below ``min_resolution_certainty`` (the
            thesis broke — new information arrived that flipped the
            outcome probability), or
          * the position has been open longer than ``hours_to_resolution``
            (the market should have resolved by now — exit at market
            to free up capital).
        """
        if not position:
            return None
        entry_certainty = float(position.get("entry_certainty", 0.0))
        # Certainty dropped below the floor — thesis broke.
        current_certainty = float(market_context.get("resolution_certainty", 1.0))
        if current_certainty < self.min_resolution_certainty:
            return {
                "reason": "certainty dropped below floor — thesis broke",
                "entry_certainty": entry_certainty,
                "current_certainty": current_certainty,
                "type": "market",
            }
        # Market should have resolved — exit at market to free capital.
        hours_left = float(market_context.get("hours_to_resolution", 24.0))
        if hours_left <= 0.0:
            return {
                "reason": "market resolution overdue — exit at market",
                "entry_certainty": entry_certainty,
                "hours_left": hours_left,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "max_hours_to_resolution": self.max_hours_to_resolution,
            "min_resolution_certainty": self.min_resolution_certainty,
            "min_edge": self.min_edge,
            "max_spread": self.max_spread,
            "min_liquidity_usdc": self.min_liquidity_usdc,
            "entered_tokens": len(self._entered_tokens),
        })
        return base
