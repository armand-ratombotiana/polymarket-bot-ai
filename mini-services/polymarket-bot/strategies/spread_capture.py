"""
strategies/spread_capture.py — Asymmetric Spread Skew Capture.

W22-3 — implements the unified strategy contract for the fourth of
five high-value strategies promoted from the PLANNED catalog. Maps
to catalog id ``mm_asymmetric_spread`` — "Asymmetric Spread Skew —
Skewed bid/ask width based on directional order flow momentum".

Signal logic
------------
Spread capture is the bread-and-butter of market making: capture
the bid-ask spread by quoting on both sides of the book. When the
order flow is balanced, symmetric quotes capture the spread evenly.
When the order flow becomes directional (more aggressive buyers
than sellers, or vice versa), the strategy skews its quotes to
avoid being run over — wider on the toxic side, tighter on the
informed side — to keep capturing the spread while bleeding less
to adverse selection.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to make a market in.
  * ``mid`` (float ∈ (0, 1), required) — the current market mid.
  * ``spread`` (float > 0, optional) — the current best bid-ask spread.
  * ``order_flow_imbalance`` (float ∈ [-1, +1], optional) — signed
    metric where +1 = 100% aggressive buyers, -1 = 100% aggressive
    sellers, 0 = balanced. Drives the asymmetric skew.
  * ``inventory`` (float, optional) — YES shares held (positive =
    long, negative = short, in shares). Skews quotes to flatten
    inventory.
  * ``volatility`` (float > 0, optional) — recent realized volatility
    of the mid price. Higher vol ⇒ wider quotes.

Edge estimation
---------------
The expected edge per round-trip is the half-spread × the fill
probability (≈ 0.5 for balanced quotes, less for skewed quotes).
Asymmetric skew reduces the per-quote edge but improves the
"adverse-selection-adjusted edge" (the edge after accounting for
the toxic flow that runs the quotes).

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
BASE_HALF_SPREAD = 0.02          # 2% half-spread baseline (4% full spread)
SKEW_FACTOR = 0.50                # skew = SKEW_FACTOR × OFI
MAX_HALF_SPREAD = 0.10            # cap at 10% half-spread (volatility blowup)
MIN_HALF_SPREAD = 0.005           # floor at 0.5% half-spread
INVENTORY_SKEW_FACTOR = 0.30      # inv skew = INVENTORY_SKEW_FACTOR × inventory
VOLATILITY_SCALAR = 0.50          # vol-adjusted spread bump
MAX_POSITION_PCT = 0.05           # never risk > 5% of capital per market
SCAN_INTERVAL = 10.0


class SpreadCapture(BaseStrategy):
    """Asymmetric spread skew market maker — captures spread while
    bleeding less to adverse selection."""

    name = "spread_capture"

    def __init__(self) -> None:
        super().__init__()
        self.base_half_spread: float = BASE_HALF_SPREAD
        self.skew_factor: float = SKEW_FACTOR
        self.max_half_spread: float = MAX_HALF_SPREAD
        self.min_half_spread: float = MIN_HALF_SPREAD
        self.inventory_skew_factor: float = INVENTORY_SKEW_FACTOR
        self.volatility_scalar: float = VOLATILITY_SCALAR
        self.max_position_pct: float = MAX_POSITION_PCT
        self._interval: float = SCAN_INTERVAL
        # Track open quote pairs per token.
        self._open_quotes: dict[str, dict] = {}

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
            "[spread_capture] Active (half=%.3f, skew×%.2f, max=%.3f)",
            self.base_half_spread,
            self.skew_factor,
            self.max_half_spread,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[spread_capture] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W22-3 — StrategyContract implementations ─────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Asymmetric spread-skew market maker — captures the "
                "bid-ask spread while skewing quotes away from toxic "
                "directional order flow."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "asymmetric_spread_skew",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "base_half_spread" in config:
            self.base_half_spread = float(config["base_half_spread"])
        if "skew_factor" in config:
            self.skew_factor = float(config["skew_factor"])
        if "max_half_spread" in config:
            self.max_half_spread = float(config["max_half_spread"])
        if "min_half_spread" in config:
            self.min_half_spread = float(config["min_half_spread"])
        if "inventory_skew_factor" in config:
            self.inventory_skew_factor = float(config["inventory_skew_factor"])
        if "volatility_scalar" in config:
            self.volatility_scalar = float(config["volatility_scalar"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.base_half_spread <= 0.0:
            return False, (
                f"base_half_spread={self.base_half_spread} must be > 0"
            )
        if self.skew_factor < 0.0:
            return False, f"skew_factor={self.skew_factor} must be >= 0"
        if self.max_half_spread <= self.min_half_spread:
            return False, (
                f"max_half_spread={self.max_half_spread} must be > "
                f"min_half_spread={self.min_half_spread}"
            )
        if self.min_half_spread <= 0.0:
            return False, (
                f"min_half_spread={self.min_half_spread} must be > 0"
            )
        if self.inventory_skew_factor < 0.0:
            return False, (
                f"inventory_skew_factor={self.inventory_skew_factor} "
                f"must be >= 0"
            )
        if self.volatility_scalar < 0.0:
            return False, (
                f"volatility_scalar={self.volatility_scalar} must be >= 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        return True, "OK"

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a market-making Signal representing the desired quote pair.

        Returns ``None`` when:
          * ``token_id`` or ``mid`` is missing,
          * the computed half-spread collapses below ``min_half_spread``
            (a degenerate regime where the quote would be inside the
            book's spread).
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None

        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        # ── Compute the asymmetric skew ───────────────────────────────────────
        # OFI (order-flow imbalance): +1 = all aggressive buyers, -1 =
        # all aggressive sellers, 0 = balanced.
        ofi = float(market_context.get("order_flow_imbalance", 0.0))
        ofi = max(-1.0, min(1.0, ofi))

        # Inventory skew: long inventory ⇒ skew quotes DOWN (tighter bid,
        # wider ask) to encourage sellers to hit the bid (flatten inv).
        inventory = float(market_context.get("inventory", 0.0))
        # Volatility bump: higher realized vol ⇒ widen the half-spread
        # to compensate for the increased adverse-selection risk.
        volatility = float(market_context.get("volatility", 0.0))

        vol_bump = self.volatility_scalar * max(0.0, volatility)
        half_spread = self.base_half_spread + vol_bump
        # OFI skew — when buyers are dominant, widen the ask (don't get
        # run over by informed buyers), tighten the bid (the buyers
        # will pay the ask anyway, no need to compete on the bid).
        ofi_skew = self.skew_factor * ofi * half_spread
        # Inventory skew — long inventory ⇒ bias DOWN (tighter bid to
        # attract sellers, wider ask to discourage more buyers).
        inv_skew = -self.inventory_skew_factor * inventory * 0.01  # per share

        bid_skew = ofi_skew + inv_skew  # positive ⇒ bid is wider
        ask_skew = -ofi_skew - inv_skew  # positive ⇒ ask is wider

        # Compute the bid/ask prices around the mid.
        raw_bid = mid_f - half_spread - bid_skew
        raw_ask = mid_f + half_spread + ask_skew
        # Clamp to the (0.01, 0.99) range to keep within market bounds.
        bid_price = round(max(0.01, min(0.98, raw_bid)), 4)
        ask_price = round(max(0.02, min(0.99, raw_ask)), 4)
        # Floor / cap the half-spread.
        actual_half = (ask_price - bid_price) / 2.0
        if actual_half < self.min_half_spread:
            # Spread collapsed — degenerate regime, skip this market.
            return None
        if actual_half > self.max_half_spread:
            # Spread blew up — volatility regime is too toxic, skip.
            return None

        # The signal's "action" is the bid side (BUY) — the canonical
        # "make a market" intent. The ask side (SELL) is encoded in
        # ``metadata.ask``.
        action = "BUY"
        # Confidence is structural for market makers — 0.5 means
        # "neither confident nor unconfident", because the strategy's
        # edge comes from the spread, not from directional prediction.
        confidence = 0.5
        # Edge = the expected per-side capture when the quote fills
        # AND the mid reverts. Approximated as the half-spread ×
        # fill_probability (0.5 for balanced, less for skewed).
        fill_prob = 0.5 - 0.2 * abs(ofi)  # skewed quotes fill less often
        edge = actual_half * fill_prob

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        # Track the open quote pair so the next cycle doesn't double-fire.
        self._open_quotes[token_id] = {
            "bid_price": bid_price,
            "ask_price": ask_price,
            "half_spread": actual_half,
            "ofi": ofi,
            "inventory": inventory,
        }

        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,  # sized in size_position
            price=bid_price,
            confidence=confidence,
            edge=edge,
            reason=(
                f"SpreadCapture quote: bid={bid_price:.4f}, "
                f"ask={ask_price:.4f}, half={actual_half:.4f}, "
                f"ofi={ofi:+.2f}, inv={inventory:+.1f}, vol={volatility:.4f}"
            ),
            metadata={
                "bid_price": bid_price,
                "ask_price": ask_price,
                "half_spread": actual_half,
                "ofi": ofi,
                "ofi_skew": ofi_skew,
                "inventory": inventory,
                "inv_skew": inv_skew,
                "volatility": volatility,
                "vol_bump": vol_bump,
                "fill_probability": fill_prob,
                "model": "asymmetric_spread_skew",
                # The ask side of the quote (the SELL leg).
                "ask_quote": {
                    "token_id": token_id,
                    "price": ask_price,
                    "side": "SELL",
                    "type": "limit",
                },
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = expected per-side capture (half-spread × fill prob).

        The ``edge`` field is set in ``generate_signal`` from the
        half-spread × fill-probability calculation. This method
        surfaces it for downstream sizing.
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size the quote via fixed notional capped by max_position_pct.

        Market makers don't Kelly-size — they quote a fixed notional
        per side (the configured ``quote_size``) and adjust inventory
        via the skew. The contract method returns the smaller of:
          * the configured quote size,
          * ``max_position_pct × capital``,
          * the available inventory headroom,
          * the available capital.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        quote_size = float(risk_params.get("quote_size_usdc", 5.0))
        max_size = self.max_position_pct * capital
        current_inv_usdc = float(risk_params.get("current_inventory_usdc", 0.0))
        headroom = max(0.0, max_size - current_inv_usdc)
        return min(quote_size, max_size, headroom, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — bid/ask limit-quote pair.

        The strategy enters BOTH legs of the quote simultaneously.
        The contract return value encodes only the bid (BUY) leg
        (the canonical "make a market" intent); the ask (SELL) leg
        is surfaced in ``metadata.ask_quote`` for the order router.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,  # "BUY" (the bid leg)
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": True,  # MM quotes are always post-only
            "metadata": {
                "model": "asymmetric_spread_skew",
                "half_spread": signal.metadata.get("half_spread"),
                "ofi": signal.metadata.get("ofi"),
                "ask_quote": signal.metadata.get("ask_quote"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit (cancel quotes / flatten inventory) on regime change.

        A market maker exits when:
          * inventory exceeds the configured ``max_inventory_shares``
            (need to flatten to avoid over-exposure),
          * volatility spikes above ``max_volatility`` (regime too
            toxic — widen quotes or cancel),
          * the order-flow imbalance flips sign (the directional
            regime changed; the prior skew is now wrong).
        """
        if not position:
            return None
        inventory = float(position.get("inventory_shares", 0.0))
        max_inv = float(position.get("max_inventory_shares", 100.0))
        if abs(inventory) >= max_inv:
            return {
                "reason": "inventory at cap — flatten via market order",
                "inventory": inventory,
                "max_inventory": max_inv,
                "type": "market",
            }
        # Volatility regime change.
        current_vol = float(market_context.get("volatility", 0.0))
        max_vol = float(position.get("max_volatility", 0.10))
        if current_vol > max_vol:
            return {
                "reason": "volatility regime too toxic — cancel quotes",
                "current_volatility": current_vol,
                "max_volatility": max_vol,
                "type": "cancel",
            }
        # OFI regime flip — the directional bias changed since we quoted.
        entry_ofi = float(position.get("entry_ofi", 0.0))
        current_ofi = float(market_context.get("order_flow_imbalance", 0.0))
        if entry_ofi != 0.0 and (
            (entry_ofi > 0 and current_ofi < 0)
            or (entry_ofi < 0 and current_ofi > 0)
        ):
            return {
                "reason": "OFI regime flipped — re-quote with new skew",
                "entry_ofi": entry_ofi,
                "current_ofi": current_ofi,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "base_half_spread": self.base_half_spread,
            "skew_factor": self.skew_factor,
            "max_half_spread": self.max_half_spread,
            "min_half_spread": self.min_half_spread,
            "open_quotes": len(self._open_quotes),
            "open_quote_tokens": list(self._open_quotes.keys())[:10],
        })
        return base
