"""Multi-strategy portfolio optimizer using Kelly criterion.

The Kelly criterion determines optimal bet size for maximizing long-term
growth: f* = (bp - q) / b, where:
  b = net odds (profit per unit wagered)
  p = probability of winning
  q = probability of losing (1 - p)

For prediction markets: b = (1/price - 1) for a YES share at `price`.

This module computes optimal capital allocation across multiple strategies
and markets, accounting for:
- Correlation between positions
- Risk constraints (max exposure, max drawdown)
- Kelly fraction (typically 0.25-0.5 of full Kelly for safety)

W16-3 — Multi-strategy portfolio optimizer (Kelly criterion).

This module is intentionally pure-Python with no I/O — it operates only
on the ``opportunities`` list and ``current_positions`` list passed by the
caller. The singleton ``portfolio_optimizer`` (constructed at import time
against the conservative defaults documented below) is the production
entry point; tests and the ``PUT /api/portfolio/config`` endpoint mutate
its attributes in place. The four ``/api/portfolio/*`` HTTP endpoints
(optimize / rebalance / get-config / put-config) are registered through
the same ``register_routes(app)`` pattern used by every other ``core.*``
feature module (see the W12-1 / W10-7 / T5 sibling blocks in
``api/server.py``).

Relationship to ``core/capital_allocator.py``
---------------------------------------------
The sibling ``core/capital_allocator.py`` module sizes a SINGLE position
for a single signal (a per-trade sizing engine, returning a USD size in
``[$0.50, $3.00]``). This module, by contrast, sizes a PORTFOLIO of
opportunities simultaneously — picking the best subset that fits within
the operator's max-total-exposure budget, scaling the last selected bet
down to fit, and computing a diversification ratio so the operator can
see how much risk was shaved off by holding multiple positions. The two
modules are complementary: ``capital_allocator`` decides "how big should
this one trade be", ``portfolio_optimizer`` decides "which of these N
trades should we take and how much capital should each one get".
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class KellyBet:
    """A single Kelly-sized position suggestion."""

    token_id: str
    strategy: str
    price: float  # Current price (0-1)
    edge: float  # Expected edge (model_p - market_p)
    confidence: float  # Model confidence (0-1)
    kelly_fraction: float  # Optimal fraction (0-1)
    kelly_fraction_adjusted: float  # After safety scaling
    suggested_size_usdc: float
    expected_return: float
    expected_risk: float


@dataclass
class PortfolioOptimization:
    """The full output of :meth:`PortfolioOptimizer.optimize`."""

    bets: list[KellyBet]
    total_allocated_usdc: float
    total_expected_return: float
    total_expected_risk: float
    diversification_ratio: float
    constraint_violations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view (mirrors ``dataclasses.asdict`` but kept
        explicit so callers don't depend on the dataclass internals)."""
        return {
            "bets": [asdict(b) for b in self.bets],
            "total_allocated_usdc": self.total_allocated_usdc,
            "total_expected_return": self.total_expected_return,
            "total_expected_risk": self.total_expected_risk,
            "diversification_ratio": self.diversification_ratio,
            "constraint_violations": list(self.constraint_violations),
        }


# ── Optimizer ───────────────────────────────────────────────────────────────


class PortfolioOptimizer:
    """Optimizes capital allocation across multiple opportunities."""

    # Defaults mirror the institutional operating-capital model in
    # ``risk/manager.py``: $100 operating capital, conservative sizing.
    DEFAULT_OPERATING_CAPITAL = 100.0
    DEFAULT_KELLY_FRACTION = 0.25  # Quarter Kelly — safety against over-betting
    DEFAULT_MAX_SINGLE_BET = 0.15  # Max 15% of capital on one bet
    DEFAULT_MAX_TOTAL_EXPOSURE = 0.80  # Max 80% deployed
    DEFAULT_MIN_EDGE = 0.03  # Min 3% edge to bet
    DEFAULT_MIN_CONFIDENCE = 0.55  # Min 55% confidence

    def __init__(
        self,
        operating_capital: float = DEFAULT_OPERATING_CAPITAL,
        kelly_fraction: float = DEFAULT_KELLY_FRACTION,
        max_single_bet: float = DEFAULT_MAX_SINGLE_BET,
        max_total_exposure: float = DEFAULT_MAX_TOTAL_EXPOSURE,
        min_edge: float = DEFAULT_MIN_EDGE,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    ):
        self.operating_capital = operating_capital
        self.kelly_fraction = kelly_fraction
        self.max_single_bet = max_single_bet
        self.max_total_exposure = max_total_exposure
        self.min_edge = min_edge
        self.min_confidence = min_confidence

    # ── Public config introspection ─────────────────────────────────────────

    def get_config(self) -> dict[str, float]:
        """Return the current config as a plain dict (JSON-serialisable).

        Used by ``GET /api/portfolio/config`` so the dashboard can render
        the live config and the operator can verify a ``PUT`` took effect.
        """
        return {
            "operating_capital": float(self.operating_capital),
            "kelly_fraction": float(self.kelly_fraction),
            "max_single_bet": float(self.max_single_bet),
            "max_total_exposure": float(self.max_total_exposure),
            "min_edge": float(self.min_edge),
            "min_confidence": float(self.min_confidence),
        }

    def update_config(self, **updates: float) -> dict[str, float]:
        """Apply a partial config update in place.

        Only the six whitelisted keys are accepted; unknown keys raise
        ``ValueError`` so a malformed ``PUT`` body surfaces clearly rather
        than silently being dropped. Each value is coerced to ``float`` so
        a JSON ``int`` (e.g. ``100``) lands as ``100.0`` and downstream
        arithmetic stays float-consistent.

        Bounds are enforced per-key to prevent an operator (or a buggy
        caller) from setting a nonsensical value that would let the
        optimizer suggest a 200% allocation:

          * ``operating_capital``  : > 0
          * ``kelly_fraction``     : 0 < f <= 1
          * ``max_single_bet``     : 0 < f <= 1
          * ``max_total_exposure`` : 0 < f <= 1
          * ``min_edge``           : 0 <= f < 1
          * ``min_confidence``     : 0 <= f <= 1

        Returns the post-update full config dict (same shape as
        :meth:`get_config`) so the route handler can echo it back to the
        caller in the 200 response body.
        """
        allowed = {
            "operating_capital",
            "kelly_fraction",
            "max_single_bet",
            "max_total_exposure",
            "min_edge",
            "min_confidence",
        }
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")

        # Bounds validation per key. Each branch raises ValueError on a
        # violation so the route handler can map it to a 422 response.
        if "operating_capital" in updates:
            v = float(updates["operating_capital"])
            if v <= 0:
                raise ValueError("operating_capital must be > 0")
            self.operating_capital = v
        if "kelly_fraction" in updates:
            v = float(updates["kelly_fraction"])
            if not (0 < v <= 1):
                raise ValueError("kelly_fraction must be in (0, 1]")
            self.kelly_fraction = v
        if "max_single_bet" in updates:
            v = float(updates["max_single_bet"])
            if not (0 < v <= 1):
                raise ValueError("max_single_bet must be in (0, 1]")
            self.max_single_bet = v
        if "max_total_exposure" in updates:
            v = float(updates["max_total_exposure"])
            if not (0 < v <= 1):
                raise ValueError("max_total_exposure must be in (0, 1]")
            self.max_total_exposure = v
        if "min_edge" in updates:
            v = float(updates["min_edge"])
            if not (0 <= v < 1):
                raise ValueError("min_edge must be in [0, 1)")
            self.min_edge = v
        if "min_confidence" in updates:
            v = float(updates["min_confidence"])
            if not (0 <= v <= 1):
                raise ValueError("min_confidence must be in [0, 1]")
            self.min_confidence = v
        return self.get_config()

    # ── Core Kelly sizing ───────────────────────────────────────────────────

    def compute_kelly(self, price: float, edge: float, confidence: float) -> float:
        """Compute Kelly fraction for a single bet.

        Args:
            price: Market price (0-1), represents cost per share
            edge: Expected edge (model_prob - market_prob)
            confidence: Model confidence in the edge

        Returns:
            Kelly fraction (0-1), or 0 if not worth betting
        """
        if price <= 0 or price >= 1:
            return 0.0

        # Kelly for binary outcome: f = edge / odds
        # For YES at price p: if win, get (1-p)/p profit; if lose, lose p
        # Kelly = (win_prob * net_odds - loss_prob) / net_odds
        #       = (confidence * (1-price)/price - (1-confidence)) / ((1-price)/price)

        if edge < self.min_edge or confidence < self.min_confidence:
            return 0.0

        # Simplified Kelly: f = edge / (1 - price) for YES bets
        # This is the standard formula for binary markets
        kelly = edge / max(1 - price, 0.01)

        # Apply safety fraction
        kelly *= self.kelly_fraction

        # Cap at 1.0
        return min(kelly, 1.0)

    # ── Portfolio optimization ───────────────────────────────────────────────

    def optimize(self, opportunities: list[dict]) -> PortfolioOptimization:
        """Optimize portfolio allocation across multiple opportunities.

        Args:
            opportunities: List of dicts with keys:
                - token_id, strategy, price, edge, confidence

        Returns:
            PortfolioOptimization with suggested bets
        """
        bets: list[KellyBet] = []
        constraint_violations: list[str] = []

        for opp in opportunities:
            price = opp.get("price", 0.5)
            edge = opp.get("edge", 0)
            confidence = opp.get("confidence", 0.5)

            kelly = self.compute_kelly(price, edge, confidence)
            if kelly <= 0:
                continue

            # Apply max single bet constraint
            kelly_adjusted = min(kelly, self.max_single_bet)

            # Compute size in USD
            size_usdc = kelly_adjusted * self.operating_capital

            # Expected return and risk
            # Expected return = edge * size (approximately)
            expected_return = edge * size_usdc
            # Risk = standard deviation = sqrt(p*(1-p)) * size
            expected_risk = np.sqrt(confidence * (1 - confidence)) * size_usdc

            bets.append(
                KellyBet(
                    token_id=opp.get("token_id", ""),
                    strategy=opp.get("strategy", ""),
                    price=price,
                    edge=edge,
                    confidence=confidence,
                    kelly_fraction=kelly,
                    kelly_fraction_adjusted=kelly_adjusted,
                    suggested_size_usdc=size_usdc,
                    expected_return=expected_return,
                    expected_risk=expected_risk,
                )
            )

        # Sort by expected return (Sharpe-like: return/risk)
        bets.sort(key=lambda b: b.expected_return / (b.expected_risk + 1e-8), reverse=True)

        # Apply total exposure constraint
        max_total = self.max_total_exposure * self.operating_capital
        total_allocated = 0.0
        selected_bets: list[KellyBet] = []

        for bet in bets:
            if total_allocated + bet.suggested_size_usdc > max_total:
                # Scale down to fit
                remaining = max_total - total_allocated
                if remaining > 1.0:  # Only add if meaningful
                    scale = remaining / bet.suggested_size_usdc
                    bet.suggested_size_usdc = remaining
                    bet.kelly_fraction_adjusted *= scale
                    bet.expected_return *= scale
                    bet.expected_risk *= scale
                    selected_bets.append(bet)
                    total_allocated = max_total
                break
            else:
                selected_bets.append(bet)
                total_allocated += bet.suggested_size_usdc

        # Check constraints
        if total_allocated > max_total:
            constraint_violations.append(
                f"Total exposure {total_allocated:.2f} exceeds max {max_total:.2f}"
            )

        for bet in selected_bets:
            if bet.suggested_size_usdc > self.max_single_bet * self.operating_capital:
                constraint_violations.append(
                    f"Single bet {bet.token_id} exceeds max single bet"
                )

        # Compute portfolio metrics
        total_return = sum(b.expected_return for b in selected_bets)
        total_risk = np.sqrt(sum(b.expected_risk**2 for b in selected_bets))  # Assuming independence
        # Diversification ratio: weighted average risk / portfolio risk
        if selected_bets and total_risk > 0:
            weighted_avg_risk = (
                sum(b.expected_risk * b.suggested_size_usdc for b in selected_bets)
                / total_allocated
            )
            diversification_ratio = weighted_avg_risk / total_risk
        else:
            diversification_ratio = 1.0

        return PortfolioOptimization(
            bets=selected_bets,
            total_allocated_usdc=total_allocated,
            total_expected_return=total_return,
            total_expected_risk=float(total_risk),
            diversification_ratio=float(diversification_ratio),
            constraint_violations=constraint_violations,
        )

    # ── Rebalance suggestion ─────────────────────────────────────────────────

    def suggest_rebalance(
        self, current_positions: list[dict], opportunities: list[dict]
    ) -> dict:
        """Suggest rebalancing actions.

        Returns:
            Dict with 'add', 'reduce', 'close', 'hold' lists
        """
        optimization = self.optimize(opportunities)

        current_tokens = {p["token_id"]: p for p in current_positions}
        suggested_tokens = {b.token_id: b for b in optimization.bets}

        actions: dict[str, list[dict]] = {"add": [], "reduce": [], "close": [], "hold": []}

        # Add new positions
        for bet in optimization.bets:
            if bet.token_id not in current_tokens:
                actions["add"].append(
                    {
                        "token_id": bet.token_id,
                        "size_usdc": bet.suggested_size_usdc,
                        "reason": f"Kelly={bet.kelly_fraction:.3f}, edge={bet.edge:.3f}",
                    }
                )

        # Close positions not in suggestions
        for token_id, pos in current_tokens.items():
            if token_id not in suggested_tokens:
                actions["close"].append(
                    {
                        "token_id": token_id,
                        "reason": "No edge or below threshold",
                    }
                )

        # Adjust existing positions
        for bet in optimization.bets:
            if bet.token_id in current_tokens:
                # NOTE (W16-3 bug-fix): the original task spec snippet read
                # ``current_positions[token_id]`` here, but ``current_positions``
                # is the raw LIST parameter (not a dict) — indexing it by a
                # string ``token_id`` raises ``TypeError: list indices must be
                # integers or slices, not str``. The correct lookup is into the
                # ``current_tokens`` dict (built from the list above). Fixed
                # here so the API contract on the rebalance endpoint holds.
                current_size = current_tokens[bet.token_id].get("size_usdc", 0)
                target_size = bet.suggested_size_usdc
                if abs(target_size - current_size) / max(current_size, 1) > 0.2:  # >20% diff
                    if target_size > current_size:
                        actions["add"].append(
                            {
                                "token_id": bet.token_id,
                                "size_usdc": target_size - current_size,
                                "reason": "Increase to Kelly target",
                            }
                        )
                    else:
                        actions["reduce"].append(
                            {
                                "token_id": bet.token_id,
                                "size_usdc": current_size - target_size,
                                "reason": "Reduce to Kelly target",
                            }
                        )
                else:
                    actions["hold"].append({"token_id": bet.token_id})

        return actions


# ── Module-level singleton ──────────────────────────────────────────────────
# Production callers do ``from core.portfolio_optimizer import portfolio_optimizer``
# then ``portfolio_optimizer.optimize(opps)``. The ``PUT /api/portfolio/config``
# endpoint mutates this singleton's attributes in place so a config change is
# picked up by every subsequent ``optimize`` / ``suggest_rebalance`` call
# without a restart.
portfolio_optimizer = PortfolioOptimizer()


# ── FastAPI route registration ──────────────────────────────────────────────
# The Pydantic request models are declared at module scope (NOT inside
# ``register_routes``) because this file uses ``from __future__ import
# annotations`` (PEP 563) — every annotation is a string at runtime, and
# FastAPI resolves the string by looking up the handler's ``__globals__``
# (the module namespace). A locally-scoped model would resolve to ``None``
# and FastAPI would fall back to treating ``body`` as a query parameter
# (returning 422 "Field required" on a JSON POST). Same pattern as
# ``core/feature_flags.py``'s ``FlagUpdate`` model.
try:  # Pydantic v2 — optional at module load if FastAPI is not installed.
    from pydantic import BaseModel, ConfigDict, Field

    class Opportunity(BaseModel):
        """Single opportunity in an optimize / rebalance request body."""

        # ``extra="forbid"`` so a malformed body (typo'd field name,
        # stray boolean) surfaces as a 422 instead of silently being
        # dropped — the optimizer would otherwise size the bet with
        # default values for the missing real field, masking the
        # caller's bug.
        model_config = ConfigDict(extra="forbid")

        token_id: str
        strategy: str = ""
        price: float = Field(0.5, ge=0.0, le=1.0)
        edge: float = Field(0.0, ge=-1.0, le=1.0)
        confidence: float = Field(0.5, ge=0.0, le=1.0)

    class OptimizeRequest(BaseModel):
        """Body for ``POST /api/portfolio/optimize``."""

        opportunities: list[Opportunity] = Field(default_factory=list)

    class CurrentPosition(BaseModel):
        """Single current position in a rebalance request body."""

        model_config = ConfigDict(extra="forbid")

        token_id: str
        size_usdc: float = 0.0

    class RebalanceRequest(BaseModel):
        """Body for ``POST /api/portfolio/rebalance``."""

        current_positions: list[CurrentPosition] = Field(default_factory=list)
        opportunities: list[Opportunity] = Field(default_factory=list)

    class ConfigUpdate(BaseModel):
        """Body for ``PUT /api/portfolio/config`` — every field optional.

        ``extra="forbid"`` so a typo'd key (e.g. ``kelley_fraction``)
        surfaces as a 422 from Pydantic BEFORE the route handler is
        reached — without this, an unknown field would silently be
        dropped by Pydantic, the route handler would call
        :meth:`PortfolioOptimizer.update_config` with an empty update
        dict, and the operator's intended change would be a silent
        no-op. With ``extra="forbid"``, the typo'd key is rejected
        up-front with a clear field-level 422 error message.

        Each declared field carries its own range validator
        (``gt=0``, ``le=1``, etc.) so out-of-range values are also
        rejected at the Pydantic layer (422) rather than reaching the
        route handler's ``ValueError``-to-422 translation in
        :meth:`update_config`.
        """

        model_config = ConfigDict(extra="forbid")

        operating_capital: float | None = Field(None, gt=0)
        kelly_fraction: float | None = Field(None, gt=0, le=1)
        max_single_bet: float | None = Field(None, gt=0, le=1)
        max_total_exposure: float | None = Field(None, gt=0, le=1)
        min_edge: float | None = Field(None, ge=0, lt=1)
        min_confidence: float | None = Field(None, ge=0, le=1)

except ImportError:  # pragma: no cover — defensive: pydantic is required
    # by FastAPI; if it's missing the routes can't be registered anyway.
    Opportunity = None  # type: ignore[assignment,misc]
    OptimizeRequest = None  # type: ignore[assignment,misc]
    CurrentPosition = None  # type: ignore[assignment,misc]
    RebalanceRequest = None  # type: ignore[assignment,misc]
    ConfigUpdate = None  # type: ignore[assignment,misc]


def register_routes(app: Any) -> None:
    """Append the four portfolio-optimizer endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      POST /api/portfolio/optimize    run the optimizer on a list of
                                       opportunities; returns the selected
                                       bets + portfolio metrics
      POST /api/portfolio/rebalance   suggest rebalancing actions
                                       (add / reduce / close / hold) given
                                       the current open positions and the
                                       latest opportunity set
      GET  /api/portfolio/config       return the live optimizer config
      PUT  /api/portfolio/config       partial-update the optimizer config
                                       (mutates the singleton in place)
    """
    from fastapi import HTTPException  # local import — FastAPI is optional at module load

    @app.post("/api/portfolio/optimize", tags=["portfolio"])
    async def _optimize(body: OptimizeRequest):
        """Run the Kelly optimizer across the supplied opportunities.

        Returns the selected bets (sorted by risk-adjusted return, scaled
        to fit the max-total-exposure budget) plus the aggregate metrics
        the dashboard renders (total allocated / expected return /
        expected risk / diversification ratio / any constraint
        violations).
        """
        opps = [op.model_dump() for op in body.opportunities]
        result = portfolio_optimizer.optimize(opps)
        return result.to_dict()

    @app.post("/api/portfolio/rebalance", tags=["portfolio"])
    async def _rebalance(body: RebalanceRequest):
        """Suggest rebalancing actions against the current open positions.

        Returns a dict with four lists — ``add`` (new positions to open
                        existing positions to grow), ``reduce`` (existing
                        positions to shrink), ``close`` (existing positions
                        to exit because the latest opportunity set no
                        longer has an edge), ``hold`` (existing positions
                        within 20 % of their Kelly target — no action
                        needed).
        """
        positions = [p.model_dump() for p in body.current_positions]
        opps = [op.model_dump() for op in body.opportunities]
        return portfolio_optimizer.suggest_rebalance(positions, opps)

    @app.get("/api/portfolio/config", tags=["portfolio"])
    async def _get_config():
        """Return the live optimizer config (six scalars).

        Used by the dashboard to render the current Kelly fraction /
        max-exposure / etc., and to verify a ``PUT`` took effect.
        """
        return portfolio_optimizer.get_config()

    @app.put("/api/portfolio/config", tags=["portfolio"])
    async def _put_config(body: ConfigUpdate):
        """Partial-update the optimizer config in place.

        Only the supplied fields are mutated; omitted fields keep their
        current value. Unknown fields raise ``ValueError`` (mapped to a
        422 response by the route handler). Returns the post-update full
        config so the caller can echo it back without a follow-up GET.
        """
        updates = {k: v for k, v in body.model_dump().items() if v is not None}
        try:
            new_config = portfolio_optimizer.update_config(**updates)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return {"ok": True, "config": new_config}


__all__ = [
    "KellyBet",
    "PortfolioOptimization",
    "PortfolioOptimizer",
    "portfolio_optimizer",
    "register_routes",
]
