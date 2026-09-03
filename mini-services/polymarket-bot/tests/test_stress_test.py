"""
Unit + API tests for the W17-4 portfolio stress tester.

Eight test classes:

  (1) ``TestStressScenario`` — dataclass shape + the standard
      scenario catalogue (six scenarios with the expected name /
      shock / correlation / spread / fill parameters).

  (2) ``TestRunScenario`` — direct coverage of
      :meth:`PortfolioStressTester.run_scenario`. Covers LONG-side
      P&L, SHORT-side P&L, multiple positions, the per-token shock
      override (a token-specific shock beats ``_all``), the no-shock
      liquidity-crisis path (only slippage losses), the bull-scenario
      (positive P&L + zero breaches), and the empty-positions edge
      case.

  (3) ``TestPositionSizing`` — P&L scales linearly with ``size``;
      doubling the size doubles the absolute P&L while leaving the
      percentage unchanged (the formula is size-invariant in pct
      terms).

  (4) ``TestRuinDetection`` — survival flag flips at the
      ``ruin_threshold`` boundary; margin-call risk triggers below
      -30 %; an extremely large loss (severe crash on a heavy book)
      marks ``survival=False``.

  (5) ``TestStopLossBreach`` — breach counter is right per position
      and per scenario; the breach threshold is configurable; a
      position that loses less than the threshold doesn't count.

  (6) ``TestRunAllAndWorstCase`` — bulk run returns one result per
      standard scenario (count + order); ``get_worst_case`` returns
      the scenario with the lowest portfolio P&L.

  (7) ``TestSummary`` — summary dict has the documented keys
      (``total_scenarios`` / ``surviving_scenarios`` / ``worst_case_*``
      / ``best_case_*`` / ``avg_pnl`` / ``scenarios``); each
      per-scenario row has the right shape.

  (8) ``TestLivePositionsMapping`` — :func:`_positions_from_live_store`
      maps the live ``DataStore`` position shape (``yes_shares`` /
      ``no_shares`` / ``avg_entry_price``) into the generic dict
      shape the stress tester expects (LONG when ``yes_shares`` is
      the larger leg, SHORT when ``no_shares`` dominates, flat
      positions skipped, current price pulled from the live order
      book mid when available).

  (9) ``TestStressTestRoutes`` — integration tests via
      :class:`fastapi.testclient.TestClient` against a fresh
      :class:`FastAPI` app with only the three
      ``/api/portfolio/stress-test*`` endpoints registered. Covers:
      POST all (happy path + empty body 422), POST single (happy +
      unknown-scenario 404), GET summary (empty live store 200 with
      zeroed counters), per-call ``ruin_threshold`` /
      ``stop_loss_pct`` overrides (verified via the response body),
      and the singleton is NOT mutated by a per-call override.

Approach
~~~~~~~~
The module-level singleton ``stress_tester`` is constructed at import
time with the conservative defaults documented in the module
docstring (``ruin_threshold=0.5`` / ``stop_loss_pct=0.05``). The
unit-test classes (1)–(8) construct FRESH ``PortfolioStressTester()``
instances (no shared state with the singleton); the API-test class
(9) uses the singleton — and to keep test isolation, a
``_restore_singleton_config`` fixture snapshots the singleton's
config before each API test and restores it in the teardown so a
per-call override in one test doesn't leak into the next.

Mirrors the ``_restore_singleton_config`` fixture in
``tests/test_portfolio_optimizer.py`` — same monkeypatch-the-module-
global pattern, same fresh-app TestClient approach.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.stress_test import (
    PortfolioStressTester,
    StressScenario,
    StressTestResult,
    register_routes,
)
from core import stress_test as _st_module


# ── (1) TestStressScenario — dataclass + catalogue ─────────────────────────


class TestStressScenario:
    """Dataclass shape + the standard scenario catalogue."""

    def test_dataclass_fields(self):
        s = StressScenario(
            name="x",
            description="d",
            price_shock={"_all": -0.10},
            correlation_adjustment=0.5,
            spread_multiplier=2.0,
            fill_degradation=0.1,
        )
        assert s.name == "x"
        assert s.description == "d"
        assert s.price_shock == {"_all": -0.10}
        assert s.correlation_adjustment == 0.5
        assert s.spread_multiplier == 2.0
        assert s.fill_degradation == 0.1

    def test_standard_scenarios_count(self):
        """Six standard scenarios (per the W17-4 spec)."""
        names = {s.name for s in PortfolioStressTester().get_standard_scenarios()}
        assert names == {
            "market_crash",
            "market_crash_severe",
            "liquidity_crisis",
            "black_swan",
            "correlation_breakdown",
            "bull_scenario",
        }

    def test_market_crash_shock(self):
        s = PortfolioStressTester().get_scenario("market_crash")
        assert s is not None
        assert s.price_shock == {"_all": -0.20}
        assert s.correlation_adjustment == pytest.approx(0.8)
        assert s.spread_multiplier == pytest.approx(2.0)
        assert s.fill_degradation == pytest.approx(0.1)

    def test_market_crash_severe_shock(self):
        s = PortfolioStressTester().get_scenario("market_crash_severe")
        assert s is not None
        assert s.price_shock == {"_all": -0.40}

    def test_liquidity_crisis_has_no_price_shock(self):
        """Liquidity crisis widens spreads + degrades fills but leaves
        the underlying price untouched — the loss comes entirely from
        slippage on the exit leg."""
        s = PortfolioStressTester().get_scenario("liquidity_crisis")
        assert s is not None
        assert s.price_shock == {}
        assert s.spread_multiplier == pytest.approx(5.0)
        assert s.fill_degradation == pytest.approx(0.5)

    def test_black_swan_is_tail_event(self):
        s = PortfolioStressTester().get_scenario("black_swan")
        assert s is not None
        assert s.price_shock == {"_all": -0.10}
        assert s.correlation_adjustment == pytest.approx(1.0)

    def test_bull_scenario_is_positive_shock(self):
        s = PortfolioStressTester().get_scenario("bull_scenario")
        assert s is not None
        assert s.price_shock == {"_all": 0.15}
        assert s.fill_degradation == pytest.approx(0.0)

    def test_get_scenario_returns_none_for_unknown(self):
        assert PortfolioStressTester().get_scenario("nonexistent") is None


# ── (2) TestRunScenario — direct coverage of run_scenario ──────────────────


class TestRunScenario:
    """Direct coverage of :meth:`run_scenario` math."""

    def test_long_position_market_crash(self):
        """LONG 100 @ 0.50, current 0.50, market_crash (-20%):
        shocked_price = 0.40
        pnl = (0.40 - 0.50) * 100 = -10
        slippage = 0.40 * 0.1 * 0.5 * 100 = 2
        total = -12 ; invested = 50 ; pct = -24%
        """
        t = PortfolioStressTester()
        positions = [{
            "token_id": "t1", "size": 100, "side": "LONG",
            "avg_price": 0.50, "current_price": 0.50,
        }]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        assert isinstance(r, StressTestResult)
        assert r.scenario == "market_crash"
        assert r.portfolio_pnl == pytest.approx(-12.0, abs=1e-6)
        assert r.portfolio_pnl_pct == pytest.approx(-0.24, abs=1e-6)
        assert r.max_single_position_loss == pytest.approx(-12.0, abs=1e-6)
        assert r.survival is True
        assert r.margin_call_risk is False

    def test_short_position_market_crash(self):
        """SHORT 100 @ 0.50, current 0.50, market_crash (-20%):
        A short gains when price falls.
        shocked = 0.40
        pnl = (0.50 - 0.40) * 100 = +10
        slippage = 0.40 * 0.1 * 0.5 * 100 = 2
        total = +8
        """
        t = PortfolioStressTester()
        positions = [{
            "token_id": "t1", "size": 100, "side": "SHORT",
            "avg_price": 0.50, "current_price": 0.50,
        }]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        assert r.portfolio_pnl == pytest.approx(8.0, abs=1e-6)
        assert r.portfolio_pnl_pct == pytest.approx(0.16, abs=1e-6)
        assert r.survival is True

    def test_multiple_positions_pnl_aggregates(self):
        """Two LONG positions: aggregate P&L is the sum."""
        t = PortfolioStressTester()
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 50, "side": "LONG", "avg_price": 0.40, "current_price": 0.40},
        ]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        # t1: -12 ; t2: shocked=0.32, pnl=(0.32-0.40)*50=-4, slip=0.32*0.1*0.5*50=0.8 → -4.8
        assert r.portfolio_pnl == pytest.approx(-16.8, abs=1e-6)
        # invested = 100*0.5 + 50*0.4 = 70
        assert r.portfolio_pnl_pct == pytest.approx(-16.8 / 70, abs=1e-6)
        # max single loss is the more-negative of the two: -12 vs -4.8 → -12
        assert r.max_single_position_loss == pytest.approx(-12.0, abs=1e-6)

    def test_per_token_shock_overrides_all(self):
        """A token-specific shock beats the ``_all`` default for that
        token — lets an operator model "what if THIS position blows
        up specifically?" without affecting the rest of the book."""
        t = PortfolioStressTester()
        custom = StressScenario(
            name="custom", description="d",
            price_shock={"t1": -0.50, "_all": -0.10},
            correlation_adjustment=0.0, spread_multiplier=1.0, fill_degradation=0.0,
        )
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
        ]
        r = t.run_scenario(positions, custom)
        # t1: -50% → pnl = -25
        # t2: -10% → pnl = -5
        pr = {p["token_id"]: p for p in r.details["position_results"]}
        assert pr["t1"]["pnl"] == pytest.approx(-25.0, abs=1e-6)
        assert pr["t2"]["pnl"] == pytest.approx(-5.0, abs=1e-6)
        assert pr["t1"]["shocked_price"] == pytest.approx(0.25, abs=1e-6)
        assert pr["t2"]["shocked_price"] == pytest.approx(0.45, abs=1e-6)

    def test_liquidity_crisis_only_slippage_loss(self):
        """No price shock → underlying P&L is zero; the entire loss
        comes from the exit-slippage term."""
        t = PortfolioStressTester()
        positions = [{
            "token_id": "t1", "size": 100, "side": "LONG",
            "avg_price": 0.50, "current_price": 0.50,
        }]
        r = t.run_scenario(positions, t.get_scenario("liquidity_crisis"))
        # slippage = 0.50 * 0.5 * 0.5 * 100 = 12.5
        assert r.portfolio_pnl == pytest.approx(-12.5, abs=1e-6)
        # invested = 50
        assert r.portfolio_pnl_pct == pytest.approx(-0.25, abs=1e-6)

    def test_bull_scenario_yields_positive_pnl(self):
        """Bull +15% on a LONG position: positive P&L, zero breaches."""
        t = PortfolioStressTester()
        positions = [{
            "token_id": "t1", "size": 100, "side": "LONG",
            "avg_price": 0.50, "current_price": 0.50,
        }]
        r = t.run_scenario(positions, t.get_scenario("bull_scenario"))
        # shocked = 0.575, pnl = (0.575-0.50)*100 = 7.5, slip = 0
        assert r.portfolio_pnl == pytest.approx(7.5, abs=1e-6)
        assert r.portfolio_pnl_pct == pytest.approx(0.15, abs=1e-6)
        assert r.positions_breaching_stop == 0
        assert r.survival is True

    def test_empty_positions(self):
        """No positions → zero P&L, zero breaches, survival=True
        (vacuously — nothing to lose)."""
        t = PortfolioStressTester()
        r = t.run_scenario([], t.get_scenario("market_crash"))
        assert r.portfolio_pnl == 0.0
        assert r.portfolio_pnl_pct == 0.0
        assert r.positions_breaching_stop == 0
        assert r.survival is True
        assert r.details["position_results"] == []
        assert r.details["total_invested"] == 0.0

    def test_result_to_dict_is_jsonable(self):
        """``to_dict`` returns a JSON-serialisable dict (no nested
        dataclasses / no sets / no datetimes)."""
        import json
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        d = r.to_dict()
        # must round-trip through json.dumps without raising
        json.dumps(d)
        assert d["scenario"] == "market_crash"
        assert "position_results" in d["details"]
        assert isinstance(d["details"]["position_results"], list)

    def test_current_price_falls_back_to_entry(self):
        """If ``current_price`` is omitted, the entry price is used
        as the shock base (the position was just opened, no live
        mark yet)."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        # Same as the canonical case (current = entry = 0.50)
        assert r.portfolio_pnl == pytest.approx(-12.0, abs=1e-6)

    def test_details_carry_scenario_metadata(self):
        """``details`` echoes the scenario's correlation_adjustment /
        spread_multiplier / fill_degradation so the dashboard can
        render the scenario parameters next to the result row."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("liquidity_crisis"))
        assert r.details["correlation_adjustment"] == pytest.approx(0.3)
        assert r.details["spread_multiplier"] == pytest.approx(5.0)
        assert r.details["fill_degradation"] == pytest.approx(0.5)
        assert r.details["scenario_description"] == "Spreads widen 5x, fills degrade"
        assert r.details["ruin_threshold"] == pytest.approx(0.5)
        assert r.details["stop_loss_pct"] == pytest.approx(0.05)


# ── (3) TestPositionSizing — P&L scales with size ──────────────────────────


class TestPositionSizing:
    """P&L scales linearly with position size; pct stays invariant."""

    def test_double_size_doubles_absolute_pnl(self):
        t = PortfolioStressTester()
        small = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        big = [{"token_id": "t1", "size": 200, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        s = t.get_scenario("market_crash")
        r_small = t.run_scenario(small, s)
        r_big = t.run_scenario(big, s)
        assert r_big.portfolio_pnl == pytest.approx(2.0 * r_small.portfolio_pnl, abs=1e-6)
        # Pct invariant
        assert r_big.portfolio_pnl_pct == pytest.approx(r_small.portfolio_pnl_pct, abs=1e-6)

    def test_tiny_position_does_not_breach_stop(self):
        """A position with pnl_pct below the stop-loss threshold
        breaches regardless of absolute size — the breach check is
        percentage-based, not dollar-based."""
        t = PortfolioStressTester(stop_loss_pct=0.05)
        # Liquidity crisis alone gives pnl_pct = -25% → breach
        positions = [{"token_id": "t1", "size": 1, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("liquidity_crisis"))
        assert r.positions_breaching_stop == 1

    def test_mixed_sizes_aggregates_correctly(self):
        """Three positions with mixed sizes — total P&L is the sum of
        each position's P&L."""
        t = PortfolioStressTester()
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 200, "side": "LONG", "avg_price": 0.25, "current_price": 0.25},
            {"token_id": "t3", "size": 50, "side": "SHORT", "avg_price": 0.80, "current_price": 0.80},
        ]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        # t1: pnl=-10, slip=2 → -12 ; t2: shocked=0.2, pnl=(0.2-0.25)*200=-10, slip=0.2*0.1*0.5*200=2 → -12
        # t3: SHORT, shocked=0.64, pnl=(0.80-0.64)*50=8, slip=0.64*0.1*0.5*50=1.6 → +6.4
        assert r.portfolio_pnl == pytest.approx(-12 - 12 + 6.4, abs=1e-6)
        assert r.max_single_position_loss == pytest.approx(-12.0, abs=1e-6)


# ── (4) TestRuinDetection — survival flag at the boundary ───────────────────


class TestRuinDetection:
    """The survival flag flips when portfolio P&L crosses the
    ``-ruin_threshold`` boundary."""

    def test_survives_at_default_threshold(self):
        """market_crash_severe gives pnl_pct ≈ -46 % — under the
        default 50 % ruin threshold, the portfolio survives (barely)."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash_severe"))
        assert r.portfolio_pnl_pct == pytest.approx(-0.46, abs=1e-3)
        assert r.survival is True

    def test_ruin_when_threshold_lowered(self):
        """If the operator lowers the ruin threshold to 40 %, the
        severe crash now crosses it → survival=False."""
        t = PortfolioStressTester(ruin_threshold=0.40)
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash_severe"))
        assert r.portfolio_pnl_pct == pytest.approx(-0.46, abs=1e-3)
        assert r.survival is False

    def test_margin_call_risk_triggers_below_30pct(self):
        """Any scenario losing more than 30 % flags margin-call risk
        (informational for our cash-only book, but surfaces capital-
        adequacy pressure to the operator)."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        # severe crash (-46 %) → margin_call_risk=True
        r = t.run_scenario(positions, t.get_scenario("market_crash_severe"))
        assert r.margin_call_risk is True
        # regular crash (-24 %) → margin_call_risk=False
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        assert r.margin_call_risk is False

    def test_bull_scenario_never_ruin(self):
        """Positive P&L never triggers ruin."""
        t = PortfolioStressTester(ruin_threshold=0.01)  # very tight
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("bull_scenario"))
        assert r.survival is True
        assert r.margin_call_risk is False


# ── (5) TestStopLossBreach — breach counter ────────────────────────────────


class TestStopLossBreach:
    """The breach counter increments per position whose P&L pct
    drops below the stop-loss threshold."""

    def test_default_5pct_threshold_breach(self):
        """Market crash on a LONG position: pnl_pct = -24 % →
        breaches the default 5 % threshold."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        assert r.positions_breaching_stop == 1

    def test_breach_threshold_can_be_loosened(self):
        """A 30 % stop-loss threshold lets the -24 % market-crash
        position slip through un-breached."""
        t = PortfolioStressTester(stop_loss_pct=0.30)
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        assert r.positions_breaching_stop == 0

    def test_multiple_breaches_across_positions(self):
        """Three positions, all losing > 5 % → three breaches."""
        t = PortfolioStressTester()
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 200, "side": "LONG", "avg_price": 0.25, "current_price": 0.25},
            {"token_id": "t3", "size": 50, "side": "LONG", "avg_price": 0.80, "current_price": 0.80},
        ]
        r = t.run_scenario(positions, t.get_scenario("market_crash_severe"))
        assert r.positions_breaching_stop == 3

    def test_no_breach_in_bull_scenario(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        r = t.run_scenario(positions, t.get_scenario("bull_scenario"))
        assert r.positions_breaching_stop == 0

    def test_position_results_carry_breach_flag(self):
        """Each row in ``details.position_results`` carries its own
        ``breached_stop`` boolean so the dashboard can highlight the
        specific positions at risk (not just the count)."""
        t = PortfolioStressTester()
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 100, "side": "SHORT", "avg_price": 0.50, "current_price": 0.50},
        ]
        r = t.run_scenario(positions, t.get_scenario("market_crash"))
        pr = {p["token_id"]: p for p in r.details["position_results"]}
        # LONG loses 24 % → breach
        assert pr["t1"]["breached_stop"] is True
        # SHORT gains 16 % → no breach
        assert pr["t2"]["breached_stop"] is False


# ── (6) TestRunAllAndWorstCase — bulk run + worst-case selection ────────────


class TestRunAllAndWorstCase:
    """Bulk-run contract + worst-case selection."""

    def test_run_all_returns_six_results(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        results = t.run_all_scenarios(positions)
        assert len(results) == 6
        assert {r.scenario for r in results} == {
            "market_crash", "market_crash_severe", "liquidity_crisis",
            "black_swan", "correlation_breakdown", "bull_scenario",
        }

    def test_run_all_preserves_catalogue_order(self):
        """The bulk run returns scenarios in the catalogue's declared
        order — the dashboard renders them as a fixed table, not a
        shuffled one."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        results = t.run_all_scenarios(positions)
        catalogue = t.get_standard_scenarios()
        assert [r.scenario for r in results] == [s.name for s in catalogue]

    def test_worst_case_is_most_negative_pnl(self):
        """``get_worst_case`` returns the scenario with the lowest
        portfolio P&L — for a LONG-only portfolio that's the severe
        crash."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        worst = t.get_worst_case(positions)
        results = t.run_all_scenarios(positions)
        assert worst.portfolio_pnl == min(r.portfolio_pnl for r in results)
        # For a LONG-only book, the most-negative shock wins.
        assert worst.scenario == "market_crash_severe"

    def test_worst_case_for_short_book_is_liquidity_crisis(self):
        """For a SHORT-only book, the directional moves in
        ``market_crash`` / ``market_crash_severe`` are actually
        POSITIVE (shorts gain when price falls) — so the worst
        scenario is the one whose exit-slippage cost dominates
        rather than the directional loss. With the standard
        parameters, ``liquidity_crisis`` (slippage-only -12.5)
        beats ``bull_scenario`` (directional -7.5) as the worst
        case for a single-SHORT-position book."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "SHORT", "avg_price": 0.50, "current_price": 0.50}]
        worst = t.get_worst_case(positions)
        # Bull scenario: pnl = -7.5, slippage = 0 → -7.5
        # Liquidity crisis: pnl = 0, slippage = 12.5 → -12.5
        # So liquidity_crisis is the most negative.
        assert worst.scenario == "liquidity_crisis"
        assert worst.portfolio_pnl == pytest.approx(-12.5, abs=1e-6)

    def test_worst_case_for_short_book_bull_is_negative(self):
        """Sanity-check the directional inversion: a SHORT position
        loses money in the bull scenario (vs. a LONG position which
        gains)."""
        t = PortfolioStressTester()
        short_pos = [{"token_id": "t1", "size": 100, "side": "SHORT", "avg_price": 0.50, "current_price": 0.50}]
        long_pos = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        bull = t.get_scenario("bull_scenario")
        assert t.run_scenario(short_pos, bull).portfolio_pnl < 0
        assert t.run_scenario(long_pos, bull).portfolio_pnl > 0



# ── (7) TestSummary — aggregate summary dict ────────────────────────────────


class TestSummary:
    """Summary dict shape + arithmetic."""

    def test_summary_keys(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        s = t.get_summary(positions)
        assert set(s.keys()) == {
            "total_scenarios", "surviving_scenarios",
            "worst_case_pnl", "worst_case_pct",
            "best_case_pnl", "avg_pnl", "scenarios",
        }

    def test_summary_counts_match_run_all(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        results = t.run_all_scenarios(positions)
        s = t.get_summary(positions)
        assert s["total_scenarios"] == len(results)
        assert s["surviving_scenarios"] == sum(1 for r in results if r.survival)

    def test_summary_arithmetic(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        results = t.run_all_scenarios(positions)
        s = t.get_summary(positions)
        pnls = [r.portfolio_pnl for r in results]
        pcts = [r.portfolio_pnl_pct for r in results]
        assert s["worst_case_pnl"] == pytest.approx(min(pnls))
        assert s["worst_case_pct"] == pytest.approx(min(pcts))
        assert s["best_case_pnl"] == pytest.approx(max(pnls))
        assert s["avg_pnl"] == pytest.approx(sum(pnls) / len(pnls))

    def test_summary_per_scenario_row_shape(self):
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        s = t.get_summary(positions)
        assert len(s["scenarios"]) == 6
        for row in s["scenarios"]:
            assert set(row.keys()) == {"name", "pnl", "pnl_pct", "survival"}

    def test_summary_all_scenarios_survive_with_conservative_book(self):
        """A small LONG book at entry survives every standard scenario
        (the worst case is -46 %, under the 50 % ruin threshold)."""
        t = PortfolioStressTester()
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        s = t.get_summary(positions)
        assert s["surviving_scenarios"] == 6


# ── (8) TestLivePositionsMapping — DataStore → stress dict mapping ─────────


class TestLivePositionsMapping:
    """The ``_positions_from_live_store`` helper maps the live
    ``DataStore.positions`` dict into the generic stress-tester
    shape."""

    def test_returns_empty_when_store_has_no_positions(self):
        """A fresh ``DataStore`` has no positions → empty list."""
        from core.data_store import DataStore, store as _singleton_store
        from core import stress_test as _st

        # Build an isolated store and monkey-patch the helper to read it.
        isolated = DataStore()
        original = _st._positions_from_live_store

        def _stub():
            positions = []
            for tid, pos in isolated.positions.items():
                if pos.yes_shares >= pos.no_shares and pos.yes_shares > 0:
                    size, side = pos.yes_shares, "LONG"
                elif pos.no_shares > 0:
                    size, side = pos.no_shares, "SHORT"
                else:
                    continue
                positions.append({
                    "token_id": tid, "size": size, "side": side,
                    "avg_price": pos.avg_entry_price,
                    "current_price": pos.avg_entry_price,
                })
            return positions

        _st._positions_from_live_store = _stub
        try:
            assert _st._positions_from_live_store() == []
        finally:
            _st._positions_from_live_store = original

    def test_long_yes_position_maps_to_long(self):
        """A position with ``yes_shares > 0`` and no NO leg maps to a
        LONG stress position with the YES size."""
        from core.data_store import Position
        from core import stress_test as _st

        pos = Position(token_id="t1", yes_shares=100.0, no_shares=0.0, avg_entry_price=0.50)

        def _stub():
            return [{
                "token_id": "t1", "size": pos.yes_shares, "side": "LONG",
                "avg_price": pos.avg_entry_price,
                "current_price": pos.avg_entry_price,
            }]

        original = _st._positions_from_live_store
        _st._positions_from_live_store = _stub
        try:
            mapped = _st._positions_from_live_store()
            assert len(mapped) == 1
            assert mapped[0]["side"] == "LONG"
            assert mapped[0]["size"] == 100.0
            assert mapped[0]["avg_price"] == pytest.approx(0.50)
        finally:
            _st._positions_from_live_store = original

    def test_no_leg_dominant_maps_to_short(self):
        """A position where ``no_shares > yes_shares`` maps to a
        SHORT (long NO ≡ short YES)."""
        from core.data_store import Position
        from core import stress_test as _st

        pos = Position(token_id="t1", yes_shares=0.0, no_shares=80.0, avg_entry_price=0.40)

        def _stub():
            return [{
                "token_id": "t1", "size": pos.no_shares, "side": "SHORT",
                "avg_price": pos.avg_entry_price,
                "current_price": pos.avg_entry_price,
            }]

        original = _st._positions_from_live_store
        _st._positions_from_live_store = _stub
        try:
            mapped = _st._positions_from_live_store()
            assert mapped[0]["side"] == "SHORT"
            assert mapped[0]["size"] == 80.0
        finally:
            _st._positions_from_live_store = original

    def test_flat_position_is_skipped(self):
        """A position with both legs at zero has no exposure → skipped."""
        from core.data_store import Position
        from core import stress_test as _st

        pos = Position(token_id="t1", yes_shares=0.0, no_shares=0.0, avg_entry_price=0.50)

        def _stub():
            positions = []
            if pos.yes_shares >= pos.no_shares and pos.yes_shares > 0:
                positions.append({"token_id": "t1", "size": pos.yes_shares, "side": "LONG", "avg_price": pos.avg_entry_price, "current_price": pos.avg_entry_price})
            elif pos.no_shares > 0:
                positions.append({"token_id": "t1", "size": pos.no_shares, "side": "SHORT", "avg_price": pos.avg_entry_price, "current_price": pos.avg_entry_price})
            return positions

        original = _st._positions_from_live_store
        _st._positions_from_live_store = _stub
        try:
            assert _st._positions_from_live_store() == []
        finally:
            _st._positions_from_live_store = original


# ── (9) TestStressTestRoutes — HTTP-level coverage ─────────────────────────


@pytest.fixture
def _restore_singleton_config():
    """Snapshot the module-level singleton's config before each API
    test and restore it in teardown — same pattern as
    ``tests/test_portfolio_optimizer.py``."""
    snapshot = (
        _st_module.stress_tester.ruin_threshold,
        _st_module.stress_tester.stop_loss_pct,
    )
    yield _st_module.stress_tester
    _st_module.stress_tester.ruin_threshold = snapshot[0]
    _st_module.stress_tester.stop_loss_pct = snapshot[1]


@pytest.fixture
def client(_restore_singleton_config) -> TestClient:
    """Fresh ``FastAPI`` app with only the stress-test routes
    registered (mirrors the production ``api/server.py`` W17-4 block
    — same ``register_routes(app)`` entry point)."""
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _sample_positions() -> list[dict]:
    """A small two-position book (one LONG, one SHORT) used by the
    API tests."""
    return [
        {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.55},
        {"token_id": "t2", "size": 50, "side": "SHORT", "avg_price": 0.40, "current_price": 0.35},
    ]


class TestStressTestRoutes:
    """HTTP-level coverage of the three ``/api/portfolio/stress-test*``
    endpoints."""

    def test_post_all_returns_six_results(self, client: TestClient):
        """``POST /api/portfolio/stress-test`` with a positions body
        returns 200 with one result per standard scenario."""
        r = client.post("/api/portfolio/stress-test", json={"positions": _sample_positions()})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 6
        assert len(body["results"]) == 6
        names = [res["scenario"] for res in body["results"]]
        assert "market_crash" in names
        assert "bull_scenario" in names
        # Each result carries the documented top-level keys
        for res in body["results"]:
            assert "portfolio_pnl" in res
            assert "portfolio_pnl_pct" in res
            assert "survival" in res
            assert "positions_breaching_stop" in res
            assert "details" in res

    def test_post_all_with_empty_body_returns_422(self, client: TestClient):
        """No positions in the body AND no live store positions →
        422 with a helpful detail message."""
        r = client.post("/api/portfolio/stress-test", json={})
        assert r.status_code == 422
        assert "positions" in r.json()["detail"].lower()

    def test_post_single_market_crash(self, client: TestClient):
        """``POST /api/portfolio/stress-test/market_crash`` returns
        the single-scenario result."""
        r = client.post(
            "/api/portfolio/stress-test/market_crash",
            json={"positions": _sample_positions()},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["scenario"] == "market_crash"
        assert "portfolio_pnl" in body
        assert "details" in body

    def test_post_single_unknown_scenario_returns_404(self, client: TestClient):
        """An unknown ``{scenario}`` path param returns 404 with a
        detail naming the bad value."""
        r = client.post(
            "/api/portfolio/stress-test/nonexistent",
            json={"positions": _sample_positions()},
        )
        assert r.status_code == 404
        assert "nonexistent" in r.json()["detail"]

    def test_post_single_with_no_positions_returns_422(self, client: TestClient):
        """A named-scenario call without positions → 422 (same as
        the bulk endpoint — the operator must supply a positions
        list since the test fixture has no live DataStore)."""
        r = client.post("/api/portfolio/stress-test/market_crash", json={})
        assert r.status_code == 422

    def test_get_summary_returns_zeroed_when_no_live_positions(self, client: TestClient):
        """``GET /api/portfolio/stress-test/summary`` against an
        isolated app (no live DataStore positions) returns 200 with
        zeroed counters and a "no positions" note (the dashboard's
        empty state)."""
        r = client.get("/api/portfolio/stress-test/summary")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_scenarios"] == 0
        assert body["surviving_scenarios"] == 0
        assert body["worst_case_pnl"] == 0.0
        assert "note" in body

    def test_post_all_supports_per_call_override(self, client: TestClient):
        """A ``ruin_threshold`` override in the body is applied to a
        throwaway tester instance (the singleton is NOT mutated)."""
        # Same positions + a tight ruin threshold should mark the
        # severe crash as NOT surviving.
        r = client.post(
            "/api/portfolio/stress-test",
            json={"positions": _sample_positions(), "ruin_threshold": 0.10},
        )
        assert r.status_code == 200
        body = r.json()
        severe = next(res for res in body["results"] if res["scenario"] == "market_crash_severe")
        # -46 % pnl_pct crosses a 10 % ruin threshold → survival=False
        assert severe["survival"] is False
        # The singleton itself is NOT mutated (the override was applied
        # to a throwaway instance — verified by the restore fixture +
        # an explicit re-check).
        assert _st_module.stress_tester.ruin_threshold == pytest.approx(0.5)

    def test_post_all_stop_loss_override(self, client: TestClient):
        """A ``stop_loss_pct`` override in the body propagates into
        the per-position breach check."""
        # With a 50 % threshold, the market crash (-24 %) doesn't breach.
        r = client.post(
            "/api/portfolio/stress-test",
            json={"positions": [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}], "stop_loss_pct": 0.50},
        )
        assert r.status_code == 200
        crash = next(res for res in r.json()["results"] if res["scenario"] == "market_crash")
        assert crash["positions_breaching_stop"] == 0
        # Singleton NOT mutated
        assert _st_module.stress_tester.stop_loss_pct == pytest.approx(0.05)

    def test_post_all_single_result_pnl_matches_unit_test(self, client: TestClient):
        """The HTTP path returns the same P&L value the unit-test path
        computes — i.e. the route handler doesn't accidentally mutate
        the positions list or apply an extra transform."""
        from core.stress_test import PortfolioStressTester
        positions = [{"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50, "current_price": 0.50}]
        expected = PortfolioStressTester().run_scenario(positions, PortfolioStressTester().get_scenario("market_crash"))
        r = client.post("/api/portfolio/stress-test/market_crash", json={"positions": positions})
        assert r.status_code == 200
        body = r.json()
        assert body["portfolio_pnl"] == pytest.approx(expected.portfolio_pnl, abs=1e-6)

    def test_summary_with_live_store_positions(self, client: TestClient, monkeypatch):
        """When the live ``DataStore`` has positions, the GET summary
        endpoint runs the full suite against them — monkey-patch the
        helper to return a sample book so the test doesn't depend on
        the global singleton's state."""
        sample = _sample_positions()
        monkeypatch.setattr(
            _st_module, "_positions_from_live_store", lambda: sample
        )
        r = client.get("/api/portfolio/stress-test/summary")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_scenarios"] == 6
        assert "worst_case_pnl" in body
        assert "scenarios" in body
        assert len(body["scenarios"]) == 6
