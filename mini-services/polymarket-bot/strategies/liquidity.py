"""
strategies/liquidity.py — Grid Trading Liquidity Provider.

W22-3 — implements the unified strategy contract for the fifth of
five high-value strategies promoted from the PLANNED catalog. Maps
to catalog id ``mm_grid_liquidity`` — "Grid Trading Liquidity —
Multi-level layered limit orders with step-ladder profit taking".

Signal logic
------------
Liquidity provision is the practice of placing passive limit orders
on both sides of the book at multiple price levels (a "grid"), so
the strategy captures the spread at each level as price oscillates
within the grid. Unlike spread-capture (which quotes only the inside
of the book), grid liquidity quotes at many price levels (e.g. ±1%,
±3%, ±5%, ±7%) — capturing spread at every oscillation while the
mean-reverting price wanders within the grid.

Inputs (via ``market_context``)
-------------------------------
  * ``token_id`` (str, required) — the outcome token to provide
    liquidity on.
  * ``mid`` (float ∈ (0, 1), required) — the current market mid
    (grid center).
  * ``volatility`` (float > 0, optional) — recent realized volatility;
    drives the grid step size (higher vol ⇒ wider grid spacing).
  * ``liquidity_usdc`` (float > 0, optional) — the market's available
    liquidity. Skips markets below ``MIN_MARKET_LIQUIDITY_USDC``
    (the strategy can't make a market in a dead book).
  * ``mean_reversion_score`` (float ∈ [0, 1], optional) — the model's
    confidence that the price will oscillate within the grid rather
    than trend out of it. Below ``MIN_MEAN_REVERSION_SCORE``, skip
    (a trending market runs over the grid).

Edge estimation
---------------
The expected edge per grid level is the level's half-spread × the
mean-reversion score (a non-mean-reverting market runs the grid;
only mean-reverting markets let the grid harvest oscillation
capture).

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
GRID_LEVELS = 5                  # number of grid levels per side (5 bids + 5 asks)
GRID_STEP_PCT = 0.02             # 2% step between grid levels (default)
GRID_STEP_VOL_SCALAR = 1.0      # vol-adjusted step = vol × scalar
MAX_GRID_STEP_PCT = 0.10         # cap at 10% step (avoid degenerate grids)
MIN_GRID_STEP_PCT = 0.005        # floor at 0.5% step (avoid penny-quotes)
LEVEL_SIZE_USDC = 1.0            # quote $1 USDC per level (default)
MAX_POSITION_PCT = 0.08           # grid uses more capital than MM (8% per market)
MIN_MARKET_LIQUIDITY_USDC = 100.0  # don't grid a dead book
MIN_MEAN_REVERSION_SCORE = 0.45    # below 45% MR score, skip (trending market)
SCAN_INTERVAL = 30.0


class LiquidityProvision(BaseStrategy):
    """Grid trading liquidity provider — places passive multi-level
    limit orders around the mid to harvest oscillation capture."""

    name = "liquidity_provision"

    def __init__(self) -> None:
        super().__init__()
        self.grid_levels: int = GRID_LEVELS
        self.grid_step_pct: float = GRID_STEP_PCT
        self.grid_step_vol_scalar: float = GRID_STEP_VOL_SCALAR
        self.max_grid_step_pct: float = MAX_GRID_STEP_PCT
        self.min_grid_step_pct: float = MIN_GRID_STEP_PCT
        self.level_size_usdc: float = LEVEL_SIZE_USDC
        self.max_position_pct: float = MAX_POSITION_PCT
        self.min_market_liquidity_usdc: float = MIN_MARKET_LIQUIDITY_USDC
        self.min_mean_reversion_score: float = MIN_MEAN_REVERSION_SCORE
        self._interval: float = SCAN_INTERVAL
        # Track open grid placements per token.
        self._open_grids: dict[str, dict] = {}

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
            "[liquidity] Active (levels=%d, step=%.2f%%, min_liq=$%.0f)",
            self.grid_levels,
            self.grid_step_pct * 100,
            self.min_market_liquidity_usdc,
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[liquidity] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    # ── W22-3 — StrategyContract implementations ─────────────────────────────

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Grid-trading liquidity provider — places passive multi-"
                "level limit orders on both sides of the book to "
                "harvest oscillation capture in mean-reverting markets."
            ),
            "author": "polymarket-bot",
            "category": "market_making",
            "model": "grid_trading_liquidity",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "grid_levels" in config:
            self.grid_levels = int(config["grid_levels"])
        if "grid_step_pct" in config:
            self.grid_step_pct = float(config["grid_step_pct"])
        if "grid_step_vol_scalar" in config:
            self.grid_step_vol_scalar = float(config["grid_step_vol_scalar"])
        if "max_grid_step_pct" in config:
            self.max_grid_step_pct = float(config["max_grid_step_pct"])
        if "min_grid_step_pct" in config:
            self.min_grid_step_pct = float(config["min_grid_step_pct"])
        if "level_size_usdc" in config:
            self.level_size_usdc = float(config["level_size_usdc"])
        if "max_position_pct" in config:
            self.max_position_pct = float(config["max_position_pct"])
        if "min_market_liquidity_usdc" in config:
            self.min_market_liquidity_usdc = float(
                config["min_market_liquidity_usdc"]
            )
        if "min_mean_reversion_score" in config:
            self.min_mean_reversion_score = float(
                config["min_mean_reversion_score"]
            )
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.grid_levels <= 0:
            return False, f"grid_levels={self.grid_levels} must be > 0"
        if self.grid_step_pct <= 0.0:
            return False, f"grid_step_pct={self.grid_step_pct} must be > 0"
        if self.max_grid_step_pct <= self.min_grid_step_pct:
            return False, (
                f"max_grid_step_pct={self.max_grid_step_pct} must be > "
                f"min_grid_step_pct={self.min_grid_step_pct}"
            )
        if self.level_size_usdc <= 0.0:
            return False, (
                f"level_size_usdc={self.level_size_usdc} must be > 0"
            )
        if not 0.0 < self.max_position_pct <= 1.0:
            return False, (
                f"max_position_pct={self.max_position_pct} must be in (0, 1]"
            )
        if self.min_market_liquidity_usdc < 0.0:
            return False, (
                f"min_market_liquidity_usdc={self.min_market_liquidity_usdc} "
                f"must be >= 0"
            )
        if not 0.0 <= self.min_mean_reversion_score <= 1.0:
            return False, (
                f"min_mean_reversion_score={self.min_mean_reversion_score} "
                f"must be in [0, 1]"
            )
        return True, "OK"

    def _compute_step(self, volatility: float) -> float:
        """Volatility-adjusted grid step (capped to [min, max])."""
        vol_step = self.grid_step_vol_scalar * max(0.0, volatility)
        step = self.grid_step_pct + vol_step
        return max(self.min_grid_step_pct, min(self.max_grid_step_pct, step))

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        """Build a Signal representing the desired grid placement.

        Returns ``None`` when:
          * ``token_id`` or ``mid`` is missing,
          * market liquidity is below ``min_market_liquidity_usdc``,
          * mean-reversion score is below ``min_mean_reversion_score``
            (the market is trending, not oscillating — grid would get
            run over),
          * the grid would extend beyond the [0.01, 0.99] market
            bounds (degenerate regime).
        """
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None

        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None

        # Liquidity regime filter — skip dead markets.
        liquidity = float(market_context.get("liquidity_usdc", 1e9))
        if liquidity < self.min_market_liquidity_usdc:
            return None
        # Mean-reversion filter — skip trending markets (the grid
        # would get run over by the trend).
        mr_score = float(market_context.get("mean_reversion_score", 0.5))
        if mr_score < self.min_mean_reversion_score:
            return None

        volatility = float(market_context.get("volatility", 0.0))
        step = self._compute_step(volatility)

        # Build the grid: ``grid_levels`` bids below mid, ``grid_levels``
        # asks above mid, each spaced ``step`` apart.
        bids: list[dict] = []
        asks: list[dict] = []
        for i in range(1, self.grid_levels + 1):
            offset = step * i
            bid_price = round(mid_f - offset, 4)
            ask_price = round(mid_f + offset, 4)
            # Skip levels that fall outside the (0.01, 0.99) market
            # bounds — these can't be quoted.
            if bid_price >= 0.01:
                bids.append({
                    "level": i,
                    "price": bid_price,
                    "side": "BUY",
                    "size_usdc": self.level_size_usdc,
                })
            if ask_price <= 0.99:
                asks.append({
                    "level": i,
                    "price": ask_price,
                    "side": "SELL",
                    "size_usdc": self.level_size_usdc,
                })

        if not bids or not asks:
            # Grid collapsed — the mid is too close to the bounds to
            # place any meaningful grid. Skip.
            return None

        # The signal's "action" is BUY (the nearest bid level is the
        # canonical "make a market" intent). The full grid (all levels,
        # both sides) is encoded in ``metadata``.
        action = "BUY"
        # Edge per level = step/2 × mr_score (the half-step capture
        # weighted by how confident we are that the price oscillates).
        # Total edge across the grid = sum of per-level edges.
        per_level_edge = (step / 2.0) * mr_score
        total_edge = per_level_edge * (len(bids) + len(asks))
        # Confidence ≈ mr_score (the mean-reversion score IS our
        # confidence that the grid will harvest oscillation rather
        # than get run over).
        confidence = min(0.95, mr_score)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        # Track the grid so the next cycle can refresh / cancel.
        self._open_grids[token_id] = {
            "mid": mid_f,
            "step": step,
            "n_bid_levels": len(bids),
            "n_ask_levels": len(asks),
            "mr_score": mr_score,
        }

        return Signal(
            action=action,
            token_id=token_id,
            size=self.level_size_usdc,
            price=bids[0]["price"],  # nearest bid
            confidence=confidence,
            edge=total_edge,
            reason=(
                f"Liquidity grid: mid={mid_f:.4f}, step={step * 100:.2f}%, "
                f"levels={len(bids)}b/{len(asks)}a, "
                f"mr={mr_score:.2f}, liq=${liquidity:.0f}, "
                f"vol={volatility:.4f}"
            ),
            metadata={
                "grid_center": mid_f,
                "grid_step": step,
                "n_bid_levels": len(bids),
                "n_ask_levels": len(asks),
                "mean_reversion_score": mr_score,
                "volatility": volatility,
                "liquidity_usdc": liquidity,
                "per_level_edge": per_level_edge,
                "level_size_usdc": self.level_size_usdc,
                "bids": bids,  # full bid grid
                "asks": asks,  # full ask grid
                "model": "grid_trading_liquidity",
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        """Edge = total expected capture across the grid.

        The ``edge`` field is set in ``generate_signal`` from the
        per-level edge × total grid levels. This method surfaces it
        for downstream sizing.
        """
        if signal is None:
            return 0.0
        return signal.edge

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        """Size the grid via per-level notional × grid levels.

        The grid uses ``level_size_usdc × (n_bid_levels + n_ask_levels)``
        total capital, bounded by ``max_position_pct × capital`` and
        the available capital. If the grid would exceed the position
        cap, the level size is shrunk proportionally to fit.
        """
        if signal is None or signal.action == "HOLD":
            return 0.0
        n_levels = (
            signal.metadata.get("n_bid_levels", 0)
            + signal.metadata.get("n_ask_levels", 0)
        )
        if n_levels <= 0:
            return 0.0
        level_size = float(
            risk_params.get("level_size_usdc", self.level_size_usdc)
        )
        max_size = self.max_position_pct * capital
        desired = level_size * n_levels
        # Shrink per-level size if the grid would exceed the cap.
        if desired > max_size:
            scale = max_size / desired
            return desired * scale
        return min(desired, max_size, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        """Return entry execution params — passive limit orders at each level.

        The strategy enters ALL grid levels simultaneously (each as a
        separate post-only limit order). The contract return value
        encodes only the nearest bid (the canonical "first level"
        quote); the full grid (all levels, both sides) is surfaced
        in ``metadata.bids`` and ``metadata.asks`` for the order
        router to fan out.
        """
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "signal action is HOLD or None"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,  # "BUY" (nearest bid)
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": True,
            "size": signal.metadata.get("level_size_usdc", 1.0),
            "metadata": {
                "model": "grid_trading_liquidity",
                "grid_center": signal.metadata.get("grid_center"),
                "grid_step": signal.metadata.get("grid_step"),
                "bids": signal.metadata.get("bids"),  # full bid grid
                "asks": signal.metadata.get("asks"),  # full ask grid
                "level_size_usdc": signal.metadata.get("level_size_usdc"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Cancel the grid on regime change.

        The grid strategy exits (cancels all open orders) when:
          * the mean-reversion score drops below the floor (the market
            is now trending — the grid would get run over),
          * volatility spikes (the step size has become unreliable —
            re-grid with the new vol),
          * the mid moves more than one grid step from the original
            grid center (the grid is now misaligned — cancel and
            re-center).
        """
        if not position:
            return None
        # MR regime change.
        current_mr = float(market_context.get("mean_reversion_score", 1.0))
        if current_mr < self.min_mean_reversion_score:
            return {
                "reason": "MR score below floor — market trending",
                "current_mr": current_mr,
                "min_mr": self.min_mean_reversion_score,
                "type": "cancel",
            }
        # Volatility regime change.
        current_vol = float(market_context.get("volatility", 0.0))
        entry_vol = float(position.get("entry_volatility", 0.0))
        # If vol has moved more than 50% from entry, the grid step is stale.
        if entry_vol > 0 and abs(current_vol - entry_vol) / entry_vol > 0.5:
            return {
                "reason": "volatility regime shifted — re-grid with new step",
                "entry_vol": entry_vol,
                "current_vol": current_vol,
                "type": "cancel",
            }
        # Grid misalignment — mid moved more than one step from center.
        grid_center = float(position.get("grid_center", 0.5))
        grid_step = float(position.get("grid_step", 0.02))
        current_mid = float(market_context.get("mid", grid_center))
        if abs(current_mid - grid_center) > grid_step:
            return {
                "reason": "mid moved > 1 step from center — re-center grid",
                "grid_center": grid_center,
                "current_mid": current_mid,
                "grid_step": grid_step,
                "type": "cancel",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "grid_levels": self.grid_levels,
            "grid_step_pct": self.grid_step_pct,
            "level_size_usdc": self.level_size_usdc,
            "min_market_liquidity_usdc": self.min_market_liquidity_usdc,
            "min_mean_reversion_score": self.min_mean_reversion_score,
            "open_grids": len(self._open_grids),
            "open_grid_tokens": list(self._open_grids.keys())[:10],
        })
        return base
