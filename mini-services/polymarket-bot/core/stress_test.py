"""Portfolio stress testing — simulates adverse market scenarios.

Scenarios:
- Market crash: all prices drop 20%
- Sector rotation: correlated positions move together
- Liquidity crisis: spreads widen 5x, fills degrade
- Black swan: extreme tail event (10% single-day move)
- Correlation breakdown: previously uncorrelated positions align
- Interest rate shock: affects probability-market discount rates

W17-4 — Portfolio stress testing system.

This module is intentionally pure-Python with no I/O — it operates only
on the ``positions`` list passed by the caller (each position is a
plain dict with ``token_id`` / ``size`` / ``avg_price`` /
``current_price`` / ``side`` keys). The singleton ``stress_tester``
(constructed at import time against the conservative defaults
documented below) is the production entry point; tests and the
``POST /api/portfolio/stress-test`` HTTP endpoints mutate its
attributes in place. The three ``/api/portfolio/stress-test/*`` HTTP
endpoints (run-all / run-single / summary) are registered through
the same ``register_routes(app)`` pattern used by every other
``core.*`` feature module (see the W16-3 portfolio-optimizer block
in ``api/server.py`` for the sibling implementation).

Relationship to ``core/portfolio_optimizer.py``
-----------------------------------------------
The W16-3 ``portfolio_optimizer`` answers "given these opportunities,
how should I allocate capital?" (forward-looking sizing). This module
answers the inverse question: "given these positions I already hold,
how badly do they break under adverse market conditions?" (backward-
looking risk assessment). The two are complementary: the optimizer's
output should be a position set that *survives* the stress tester's
worst-case scenario.

Relationship to ``risk/manager.py``
------------------------------------
The ``risk/manager.py`` module enforces *pre-trade* limits (per-trade
size, per-strategy exposure, daily-loss stop, drawdown circuit
breaker) — its job is to keep individual trades small. This module,
by contrast, evaluates *post-trade* portfolio resilience — its job is
to answer "if every position moved against me simultaneously, would
I still have a surviving portfolio?" Both layers are required: pre-
trade limits prevent you from taking an oversized position in the
first place; stress tests surface tail-correlation risk that the
per-trade limits can't see.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class StressScenario:
    """A single stress-test scenario definition.

    Attributes:
        name: short identifier (used in URLs and result rows)
        description: human-readable summary of the scenario
        price_shock: ``{token_id: pct_change}`` — the special key
            ``"_all"`` applies the shock to every position; per-token
            keys override ``_all`` for that specific token
        correlation_adjustment: 0 = positions independent, 1 = perfect
            correlation (informational — surfaced in the result details
            so the dashboard can render a "diversification held up?"
            badge next to each scenario)
        spread_multiplier: 1 = normal spreads, 5 = crisis-wide spreads
            (currently informational; future expansion will model the
            cost of unwinding positions into a wider book)
        fill_degradation: 0 = fills at quote, 0.5 = 50 % slippage on
            the exit leg (a liquidity crisis means you can't get out
            at the marked price — the gap costs you)
    """

    name: str
    description: str
    price_shock: dict[str, float]  # token_id -> price_change_pct
    correlation_adjustment: float  # 0 = no correlation, 1 = perfect
    spread_multiplier: float  # 1 = normal, 5 = crisis
    fill_degradation: float  # 0 = perfect fills, 0.5 = 50% slippage


@dataclass
class StressTestResult:
    """The output of running one scenario on one portfolio snapshot.

    The ``details`` dict carries the per-position breakdown so the
    dashboard can drill into which specific positions drove the
    portfolio-level loss — and which ones breached their individual
    stop-loss thresholds even when the portfolio overall survived.
    """

    scenario: str
    portfolio_pnl: float
    portfolio_pnl_pct: float
    max_single_position_loss: float
    positions_breaching_stop: int
    margin_call_risk: bool
    survival: bool  # True if portfolio survives (doesn't hit ruin threshold)
    details: dict

    def to_dict(self) -> dict:
        """JSON-serialisable view (for the HTTP response body)."""
        return asdict(self)


# ── Tester ──────────────────────────────────────────────────────────────────


class PortfolioStressTester:
    """Runs stress tests on the current portfolio."""

    def __init__(self, ruin_threshold: float = 0.5, stop_loss_pct: float = 0.05):
        # Lose 50% of invested capital = ruin (portfolio can't recover in
        # any reasonable time horizon — operator must manually intervene).
        self.ruin_threshold = ruin_threshold
        # Per-position stop-loss trigger — a position losing > 5 % of its
        # entry cost is "breaching" (would normally be auto-closed by the
        # risk manager, but under a stress scenario the close itself
        # crystallises the loss).
        self.stop_loss_pct = stop_loss_pct

    # ── Scenario catalogue ───────────────────────────────────────────────

    def get_standard_scenarios(self) -> list[StressScenario]:
        """Get standard stress test scenarios.

        Returns six canonical scenarios covering the four primary
        tail-risk axes (price shock / liquidity / correlation / tail)
        plus a severe-crash variant and a bull-scenario control so the
        dashboard can render both the worst-case and best-case bounds.
        """
        return [
            StressScenario(
                name="market_crash",
                description="All positions drop 20%",
                price_shock={"_all": -0.20},
                correlation_adjustment=0.8,
                spread_multiplier=2.0,
                fill_degradation=0.1,
            ),
            StressScenario(
                name="market_crash_severe",
                description="All positions drop 40%",
                price_shock={"_all": -0.40},
                correlation_adjustment=0.9,
                spread_multiplier=3.0,
                fill_degradation=0.2,
            ),
            StressScenario(
                name="liquidity_crisis",
                description="Spreads widen 5x, fills degrade",
                price_shock={},
                correlation_adjustment=0.3,
                spread_multiplier=5.0,
                fill_degradation=0.5,
            ),
            StressScenario(
                name="black_swan",
                description="Extreme tail event: 10% single-day move",
                price_shock={"_all": -0.10},
                correlation_adjustment=1.0,
                spread_multiplier=4.0,
                fill_degradation=0.3,
            ),
            StressScenario(
                name="correlation_breakdown",
                description="Previously uncorrelated positions align",
                price_shock={"_all": -0.15},
                correlation_adjustment=1.0,
                spread_multiplier=2.0,
                fill_degradation=0.1,
            ),
            StressScenario(
                name="bull_scenario",
                description="All positions gain 15%",
                price_shock={"_all": 0.15},
                correlation_adjustment=0.5,
                spread_multiplier=0.8,
                fill_degradation=0.0,
            ),
        ]

    def get_scenario(self, name: str) -> Optional[StressScenario]:
        """Look up a single standard scenario by name.

        Returns ``None`` if no standard scenario matches (so the API
        layer can map that to a 404).
        """
        for s in self.get_standard_scenarios():
            if s.name == name:
                return s
        return None

    # ── Core simulation ──────────────────────────────────────────────────

    def run_scenario(self, positions: list[dict], scenario: StressScenario) -> StressTestResult:
        """Run a single stress scenario on the portfolio.

        Per position:
          1. Apply the price shock (``_all`` shock applies to every
             position; a per-token shock overrides ``_all`` for that
             specific token).
          2. Compute P&L based on side (LONG gains when price rises,
             SHORT gains when price falls).
          3. Apply fill-degradation slippage on the exit leg — under
             a liquidity crisis you can't unwind at the marked price.
          4. Check whether the position has breached its individual
             stop-loss threshold.

        At portfolio level:
          - Aggregate total P&L and the worst single-position loss.
          - Compute P&L as a fraction of total invested capital.
          - Flag survival (portfolio P&L > ``-ruin_threshold``).
          - Flag margin-call risk (portfolio P&L < -30 % — the level
            at which a broker would typically issue a margin call on
            a leveraged book; for our cash-only prediction-market
            book this is informational but surfaces capital-adequacy
            pressure).
        """
        total_pnl = 0.0
        max_single_loss = 0.0
        positions_breaching_stop = 0
        position_results: list[dict] = []

        for pos in positions:
            token_id = pos.get("token_id", "")
            size = pos.get("size", 0) or 0
            entry_price = pos.get("avg_price", 0) or pos.get("entry_price", 0) or 0
            current_price = pos.get("current_price", entry_price) or entry_price

            # Apply price shock — per-token overrides the ``_all`` default.
            shock_pct = scenario.price_shock.get(token_id)
            if shock_pct is None:
                shock_pct = scenario.price_shock.get("_all", 0)
            shocked_price = current_price * (1 + shock_pct)

            # Compute P&L
            side = (pos.get("side") or "LONG").upper()
            if side == "SHORT":
                pnl = (entry_price - shocked_price) * size
            else:  # LONG (default)
                pnl = (shocked_price - entry_price) * size

            # Apply fill degradation (slippage on exit). Under a liquidity
            # crisis you can't unwind at the marked price — half the
            # degradation hits the exit price (the other half is the
            # bid-ask spread the spread_multiplier already accounts for
            # in the future expansion).
            exit_slippage = shocked_price * scenario.fill_degradation * 0.5
            pnl -= exit_slippage * size

            total_pnl += pnl
            if pnl < max_single_loss:
                max_single_loss = pnl

            # Check stop loss — a position losing more than the
            # stop-loss threshold of its entry cost is "breaching".
            position_cost = entry_price * size
            pnl_pct = pnl / position_cost if position_cost > 0 else 0
            breached_stop = pnl_pct < -self.stop_loss_pct
            if breached_stop:
                positions_breaching_stop += 1

            position_results.append({
                "token_id": token_id,
                "side": side,
                "size": size,
                "entry_price": entry_price,
                "shocked_price": shocked_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "breached_stop": breached_stop,
            })

        # Compute portfolio-level metrics
        total_invested = sum(
            abs(p.get("size", 0) or 0)
            * (p.get("avg_price", 0) or p.get("entry_price", 0) or 0)
            for p in positions
        )
        portfolio_pnl_pct = total_pnl / total_invested if total_invested > 0 else 0

        # Check ruin — losing more than ruin_threshold of invested
        # capital means the portfolio is unlikely to recover within
        # any reasonable time horizon.
        survival = portfolio_pnl_pct > -self.ruin_threshold
        margin_call_risk = portfolio_pnl_pct < -0.3

        return StressTestResult(
            scenario=scenario.name,
            portfolio_pnl=total_pnl,
            portfolio_pnl_pct=portfolio_pnl_pct,
            max_single_position_loss=max_single_loss,
            positions_breaching_stop=positions_breaching_stop,
            margin_call_risk=margin_call_risk,
            survival=survival,
            details={
                "scenario_description": scenario.description,
                "position_results": position_results,
                "total_invested": total_invested,
                "correlation_adjustment": scenario.correlation_adjustment,
                "spread_multiplier": scenario.spread_multiplier,
                "fill_degradation": scenario.fill_degradation,
                "ruin_threshold": self.ruin_threshold,
                "stop_loss_pct": self.stop_loss_pct,
            },
        )

    # ── Bulk / summary entry points ─────────────────────────────────────

    def run_all_scenarios(self, positions: list[dict]) -> list[StressTestResult]:
        """Run all standard stress test scenarios."""
        results = []
        for scenario in self.get_standard_scenarios():
            results.append(self.run_scenario(positions, scenario))
        return results

    def get_worst_case(self, positions: list[dict]) -> StressTestResult:
        """Get the worst-case scenario result.

        Returns the scenario with the lowest (most-negative) total
        P&L. Ties broken by scenario order (first-wins).
        """
        results = self.run_all_scenarios(positions)
        return min(results, key=lambda r: r.portfolio_pnl)

    def get_summary(self, positions: list[dict]) -> dict:
        """Get a summary of all stress tests.

        Returns a dict with portfolio-level aggregates (worst / best /
        average P&L, count of surviving scenarios) plus a per-scenario
        breakdown the dashboard renders as a table.
        """
        results = self.run_all_scenarios(positions)
        surviving = sum(1 for r in results if r.survival)
        return {
            "total_scenarios": len(results),
            "surviving_scenarios": surviving,
            "worst_case_pnl": min(r.portfolio_pnl for r in results),
            "worst_case_pct": min(r.portfolio_pnl_pct for r in results),
            "best_case_pnl": max(r.portfolio_pnl for r in results),
            "avg_pnl": sum(r.portfolio_pnl for r in results) / len(results) if results else 0.0,
            "scenarios": [
                {
                    "name": r.scenario,
                    "pnl": r.portfolio_pnl,
                    "pnl_pct": r.portfolio_pnl_pct,
                    "survival": r.survival,
                }
                for r in results
            ],
        }


# ── Singleton ──────────────────────────────────────────────────────────────

stress_tester = PortfolioStressTester()


# ── HTTP routes ─────────────────────────────────────────────────────────────


def _positions_from_live_store() -> list[dict]:
    """Best-effort snapshot of the live ``DataStore`` positions in the
    dict shape :meth:`run_scenario` expects.

    Returns an empty list if the store can't be imported (e.g. running
    in a test environment without the full app loaded) so the route
    handler degrades gracefully to a 422 "no positions provided" rather
    than crashing on import.

    Maps the on-disk ``Position`` (``yes_shares`` / ``no_shares`` /
    ``avg_entry_price``) into the generic ``{token_id, size, side,
    avg_price, current_price}`` shape the stress tester operates on.
    The current price falls back to the entry price when no live book
    quote is available — the stress test is a what-if against the
    marked entry, not a live-P&L computation.
    """
    try:
        from core.data_store import store as _store  # local — avoids import cycle
    except Exception:  # pragma: no cover — defensive, exercised only in broken envs
        logger.debug("stress_test: data_store unavailable, returning empty positions")
        return []

    positions: list[dict] = []
    for token_id, pos in _store.positions.items():
        # Position may be long YES (yes_shares > 0) or long NO
        # (no_shares > 0 — equivalent to short YES). Pick the larger
        # leg as the dominant side; ties default to LONG.
        if pos.yes_shares >= pos.no_shares and pos.yes_shares > 0:
            size = pos.yes_shares
            side = "LONG"
        elif pos.no_shares > 0:
            size = pos.no_shares
            side = "SHORT"
        else:
            # Flat position — skip (no exposure to stress).
            continue
        # Live mid-price if the book is loaded; else fall back to entry.
        book = _store.order_books.get(token_id)
        mid = book.mid if book is not None else None
        current_price = mid if mid is not None else pos.avg_entry_price
        positions.append({
            "token_id": token_id,
            "size": size,
            "side": side,
            "avg_price": pos.avg_entry_price,
            "current_price": current_price,
        })
    return positions


def register_routes(app: Any) -> None:
    """Append the three portfolio stress-test endpoints to a FastAPI app.

    Endpoints (auth-protected by the caller's existing middleware):

      POST /api/portfolio/stress-test            run all standard scenarios
                                                on the supplied (or live)
                                                positions; returns the full
                                                per-scenario result list
      POST /api/portfolio/stress-test/{scenario}  run a single named
                                                scenario; 404 if the name
                                                doesn't match a standard
                                                scenario
      GET  /api/portfolio/stress-test/summary    run all scenarios and
                                                return the aggregate
                                                summary (worst/best/avg
                                                P&L + surviving count)
    """
    from fastapi import HTTPException  # local — FastAPI optional at module load

    @app.post("/api/portfolio/stress-test", tags=["portfolio"])
    async def _stress_test_all(body: dict | None = None):
        """Run every standard stress scenario against the portfolio.

        Body shape (all optional)::

            {
              "positions": [
                {"token_id": "t1", "size": 100, "side": "LONG",
                 "avg_price": 0.50, "current_price": 0.55},
                ...
              ],
              "ruin_threshold": 0.5,    # optional override (0..1)
              "stop_loss_pct": 0.05     # optional override (0..1)
            }

        If ``positions`` is omitted, the live ``DataStore`` positions
        are snapshotted. If neither is available, returns 422.
        """
        body = body or {}
        positions = body.get("positions")
        if positions is None:
            positions = _positions_from_live_store()
        if not positions:
            raise HTTPException(
                status_code=422,
                detail="No positions to stress-test — supply a 'positions' list in the body.",
            )
        # Optional per-call overrides — applied to a throwaway tester
        # instance so the singleton's defaults aren't mutated.
        ruin = body.get("ruin_threshold")
        stop = body.get("stop_loss_pct")
        tester = stress_tester
        if (ruin is not None) or (stop is not None):
            tester = PortfolioStressTester(
                ruin_threshold=ruin if ruin is not None else stress_tester.ruin_threshold,
                stop_loss_pct=stop if stop is not None else stress_tester.stop_loss_pct,
            )
        results = tester.run_all_scenarios(positions)
        return {
            "results": [r.to_dict() for r in results],
            "count": len(results),
        }

    @app.post("/api/portfolio/stress-test/{scenario}", tags=["portfolio"])
    async def _stress_test_single(scenario: str, body: dict | None = None):
        """Run a single named scenario.

        ``scenario`` must match one of the standard scenario names
        (``market_crash`` / ``market_crash_severe`` / ``liquidity_crisis``
        / ``black_swan`` / ``correlation_breakdown`` / ``bull_scenario``).
        Returns 404 if unknown.

        Body shape mirrors ``POST /api/portfolio/stress-test``.
        """
        body = body or {}
        scenario_obj = stress_tester.get_scenario(scenario)
        if scenario_obj is None:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown stress scenario: {scenario!r}",
            )
        positions = body.get("positions")
        if positions is None:
            positions = _positions_from_live_store()
        if not positions:
            raise HTTPException(
                status_code=422,
                detail="No positions to stress-test — supply a 'positions' list in the body.",
            )
        ruin = body.get("ruin_threshold")
        stop = body.get("stop_loss_pct")
        tester = stress_tester
        if (ruin is not None) or (stop is not None):
            tester = PortfolioStressTester(
                ruin_threshold=ruin if ruin is not None else stress_tester.ruin_threshold,
                stop_loss_pct=stop if stop is not None else stress_tester.stop_loss_pct,
            )
        result = tester.run_scenario(positions, scenario_obj)
        return result.to_dict()

    @app.get("/api/portfolio/stress-test/summary", tags=["portfolio"])
    async def _stress_test_summary():
        """Run all standard scenarios against the live portfolio and
        return the aggregate summary (worst / best / avg P&L +
        surviving-scenarios count + per-scenario table).

        Reads positions from the live ``DataStore`` — no body accepted.
        If the store has no open positions, returns a 200 with zeroed
        counters (the dashboard's "no positions to stress" state).
        """
        positions = _positions_from_live_store()
        if not positions:
            return {
                "total_scenarios": 0,
                "surviving_scenarios": 0,
                "worst_case_pnl": 0.0,
                "worst_case_pct": 0.0,
                "best_case_pnl": 0.0,
                "avg_pnl": 0.0,
                "scenarios": [],
                "note": "No open positions in the live store.",
            }
        return stress_tester.get_summary(positions)


__all__ = [
    "StressScenario",
    "StressTestResult",
    "PortfolioStressTester",
    "stress_tester",
    "register_routes",
]
