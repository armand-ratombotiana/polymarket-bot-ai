"""
Unit + API tests for the W16-3 portfolio optimizer.

Three test classes:

  (1) ``TestComputeKelly`` — unit tests for
      :meth:`PortfolioOptimizer.compute_kelly`. Covers: price
      boundaries (0 / 1) returning 0, edge / confidence below
      threshold returning 0, normal-case Kelly value, the Kelly cap
      at 1.0, the safety-fraction multiplier, and negative-edge
      rejection.

  (2) ``TestOptimize`` — unit tests for
      :meth:`PortfolioOptimizer.optimize`. Covers: empty input,
      all-below-threshold input, the bet-selection + sort-by-Sharpe
      contract, max-single-bet enforcement, max-total-exposure
      enforcement (incl. last-bet scaling), the diversification ratio,
      and total return / risk aggregation.

  (3) ``TestSuggestRebalance`` — unit tests for
      :meth:`PortfolioOptimizer.suggest_rebalance`. Covers: adding a
      new position, closing a position no longer in the suggestion
      set, growing an undersized position, shrinking an oversized
      position, holding within the 20 % tolerance, and the two empty-
      input edge cases.

  (4) ``TestConfig`` — unit tests for :meth:`get_config` /
      :meth:`update_config`. Covers: default snapshot shape, partial
      update, full update, unknown-key rejection, and out-of-range
      rejection (one assertion per bounded key).

  (5) ``TestPortfolioOptimizerRoutes`` — integration tests via
      :class:`fastapi.testclient.TestClient` against a fresh
      :class:`FastAPI` app with only the four ``/api/portfolio/*``
      endpoints registered. Covers: POST optimize (happy path +
      empty body + Pydantic 422 on out-of-range price), POST
      rebalance (add + close + hold paths), GET config, PUT config
      (partial + unknown-key 422 + out-of-range 422 + the
      affects-subsequent-optimize side-effect).

Approach
~~~~~~~~
The module-level singleton ``portfolio_optimizer`` is constructed at
import time with the conservative defaults documented in the module
docstring. The unit-test classes (1)–(4) construct FRESH
``PortfolioOptimizer()`` instances (no shared state with the
singleton); the API-test class (5) uses the singleton — and to keep
test isolation, a ``_restore_singleton_config`` fixture snapshots
the singleton's config before each API test and restores it in the
teardown so a PUT in one test doesn't leak into the next.

Mirrors the ``isolated_flags`` fixture in
``tests/test_feature_flags.py`` — same monkeypatch-the-module-global
pattern, same fresh-app TestClient approach.
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.portfolio_optimizer import (
    ConfigUpdate,  # noqa: F401  (imported for "is pydantic wired" sanity)
    KellyBet,
    PortfolioOptimization,
    PortfolioOptimizer,
    Opportunity,  # noqa: F401
    register_routes,
)
from core import portfolio_optimizer as _po_module


# ── (1) Unit tests: compute_kelly ────────────────────────────────────────────


class TestComputeKelly:
    """Direct coverage of :meth:`PortfolioOptimizer.compute_kelly`."""

    def test_price_at_zero_returns_zero(self):
        """A price of 0 is degenerate (infinite odds, undefined Kelly);
        the optimizer refuses to size such a bet."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=0.0, edge=0.10, confidence=0.7) == 0.0

    def test_price_at_one_returns_zero(self):
        """A price of 1 means the market is already resolved; no edge
        can be captured so Kelly returns 0."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=1.0, edge=0.10, confidence=0.7) == 0.0

    def test_price_out_of_range_returns_zero(self):
        """A negative or > 1 price is invalid; refuse to size."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=-0.5, edge=0.10, confidence=0.7) == 0.0
        assert opt.compute_kelly(price=1.5, edge=0.10, confidence=0.7) == 0.0

    def test_edge_below_threshold_returns_zero(self):
        """``edge < min_edge`` (default 0.03) is not worth the spread
        cost — return 0 so the optimizer skips this opportunity."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=0.5, edge=0.02, confidence=0.7) == 0.0

    def test_confidence_below_threshold_returns_zero(self):
        """``confidence < min_confidence`` (default 0.55) means the model
        isn't sure enough to bet — return 0."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=0.5, edge=0.10, confidence=0.50) == 0.0

    def test_zero_edge_returns_zero(self):
        """An edge of exactly 0 carries no signal — return 0."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=0.5, edge=0.0, confidence=0.7) == 0.0

    def test_negative_edge_returns_zero(self):
        """A negative edge means the model thinks the market price is
        too HIGH (would short the YES side); this Kelly formulation
        sizes only YES-side bets, so a negative edge returns 0 (the
        caller should re-formulate with the complementary price /
        edge to size the NO side)."""
        opt = PortfolioOptimizer()
        assert opt.compute_kelly(price=0.5, edge=-0.10, confidence=0.7) == 0.0

    def test_normal_case_calculates_expected_kelly(self):
        """A textbook 10 % edge at 50/50 with 70 % model confidence
        yields the textbook quarter-Kelly fraction.

        kelly = edge / (1 - price) * safety_fraction
              = 0.10 / 0.50 * 0.25
              = 0.05
        """
        opt = PortfolioOptimizer()
        kelly = opt.compute_kelly(price=0.5, edge=0.10, confidence=0.7)
        assert kelly == pytest.approx(0.05, abs=1e-9)

    def test_kelly_safety_fraction_applied(self):
        """The ``kelly_fraction`` ctor arg is the safety multiplier
        (default 0.25 = quarter-Kelly). Doubling it doubles the Kelly
        fraction (linear in the multiplier)."""
        opt_quarter = PortfolioOptimizer(kelly_fraction=0.25)
        opt_half = PortfolioOptimizer(kelly_fraction=0.50)
        k_q = opt_quarter.compute_kelly(price=0.5, edge=0.10, confidence=0.7)
        k_h = opt_half.compute_kelly(price=0.5, edge=0.10, confidence=0.7)
        assert k_h == pytest.approx(2 * k_q, abs=1e-9)

    def test_kelly_capped_at_one(self):
        """Even at a near-certain outcome with a huge edge, the Kelly
        fraction is capped at 1.0 (cannot bet more than 100 % of
        capital on a single position).

        Constructed edge case: price=0.05, edge=0.50, confidence=0.99.
          kelly_raw = 0.50 / max(0.95, 0.01) ≈ 0.5263
          kelly *= 0.25 → 0.1316 (well under 1.0)

        To force the cap, use ``kelly_fraction=4.0`` so 0.5263 * 4 ≈
        2.10, which clips to 1.0.
        """
        opt = PortfolioOptimizer(kelly_fraction=4.0)
        kelly = opt.compute_kelly(price=0.05, edge=0.50, confidence=0.99)
        assert kelly == 1.0

    def test_low_price_floor_prevents_div_by_zero(self):
        """``max(1 - price, 0.01)`` guards against division by ~0
        when the price is very close to 1. At price=0.9999 the
        denominator is 0.01 (the floor), not 0.0001 — so a 3 % edge
        yields Kelly = 0.03 / 0.01 * 0.25 = 0.75 rather than the
        nonsensical 75.0 a naive division would produce."""
        opt = PortfolioOptimizer()
        kelly = opt.compute_kelly(price=0.9999, edge=0.03, confidence=0.6)
        # 0.03 / 0.01 = 3.0; * 0.25 = 0.75.
        assert kelly == pytest.approx(0.75, abs=1e-9)


# ── (2) Unit tests: optimize ─────────────────────────────────────────────────


def _make_opp(token_id: str, price: float, edge: float, confidence: float,
              strategy: str = "test_strat") -> dict:
    """Helper: build an opportunity dict in the canonical shape."""
    return {
        "token_id": token_id,
        "strategy": strategy,
        "price": price,
        "edge": edge,
        "confidence": confidence,
    }


class TestOptimize:
    """Direct coverage of :meth:`PortfolioOptimizer.optimize`."""

    def test_optimize_with_empty_opportunities_returns_empty_portfolio(self):
        """An empty opportunity list yields an empty PortfolioOptimization
        with all-zero totals and a default diversification_ratio of 1.0
        (no diversification possible with zero bets)."""
        opt = PortfolioOptimizer()
        result = opt.optimize([])
        assert isinstance(result, PortfolioOptimization)
        assert result.bets == []
        assert result.total_allocated_usdc == 0.0
        assert result.total_expected_return == 0.0
        assert result.total_expected_risk == 0.0
        assert result.diversification_ratio == 1.0
        assert result.constraint_violations == []

    def test_optimize_with_all_below_threshold_returns_empty_portfolio(self):
        """Every opportunity with edge < min_edge OR confidence <
        min_confidence is filtered out by ``compute_kelly``; the
        returned PortfolioOptimization is empty."""
        opt = PortfolioOptimizer()
        opps = [
            _make_opp("low_edge", price=0.5, edge=0.01, confidence=0.7),
            _make_opp("low_conf", price=0.5, edge=0.10, confidence=0.50),
            _make_opp("negative_edge", price=0.5, edge=-0.10, confidence=0.9),
        ]
        result = opt.optimize(opps)
        assert result.bets == []
        assert result.total_allocated_usdc == 0.0

    def test_optimize_selects_bets_above_threshold(self):
        """A single opportunity above threshold produces one bet whose
        size matches ``kelly * operating_capital`` and whose
        expected_return / expected_risk are computed from the edge /
        confidence."""
        opt = PortfolioOptimizer()
        result = opt.optimize([_make_opp("t1", price=0.5, edge=0.10, confidence=0.7)])
        assert len(result.bets) == 1
        bet = result.bets[0]
        assert bet.token_id == "t1"
        # kelly = 0.05; kelly_adjusted = 0.05; size_usdc = 0.05 * 100 = 5.0
        assert bet.kelly_fraction == pytest.approx(0.05, abs=1e-9)
        assert bet.kelly_fraction_adjusted == pytest.approx(0.05, abs=1e-9)
        assert bet.suggested_size_usdc == pytest.approx(5.0, abs=1e-6)
        # expected_return = edge * size = 0.10 * 5.0 = 0.50
        assert bet.expected_return == pytest.approx(0.50, abs=1e-6)
        # expected_risk = sqrt(0.7*0.3) * 5.0
        assert bet.expected_risk == pytest.approx(math.sqrt(0.21) * 5.0, abs=1e-6)
        # Single bet → no diversification (ratio = 1.0).
        assert result.diversification_ratio == pytest.approx(1.0, abs=1e-9)
        assert result.total_allocated_usdc == pytest.approx(5.0, abs=1e-6)
        assert result.total_expected_return == pytest.approx(0.50, abs=1e-6)
        assert result.total_expected_risk == pytest.approx(math.sqrt(0.21) * 5.0, abs=1e-6)

    def test_optimize_sorts_bets_by_risk_adjusted_return(self):
        """When two opportunities both pass the threshold, the optimizer
        selects both but returns them sorted by ``expected_return /
        expected_risk`` (Sharpe-like) DESCENDING. The higher-Sharpe bet
        is processed first (so it's allocated first against the
        max-total-exposure budget).

        opp_a (price=0.5, edge=0.10, conf=0.70):
          kelly = 0.05, size = 5.0, ret = 0.50, risk = sqrt(0.21)*5 ≈ 2.291
          sharpe = 0.50 / 2.291 ≈ 0.218

        opp_b (price=0.4, edge=0.15, conf=0.80):
          kelly = 0.15/0.6 * 0.25 = 0.0625, size = 6.25, ret = 0.9375,
          risk = sqrt(0.16)*6.25 = 2.5
          sharpe = 0.9375 / 2.5 = 0.375

        So opp_b comes first.
        """
        opt = PortfolioOptimizer()
        opps = [
            _make_opp("a", price=0.5, edge=0.10, confidence=0.70),
            _make_opp("b", price=0.4, edge=0.15, confidence=0.80),
        ]
        result = opt.optimize(opps)
        assert len(result.bets) == 2
        assert result.bets[0].token_id == "b"  # higher Sharpe first
        assert result.bets[1].token_id == "a"

    def test_optimize_respects_max_single_bet(self):
        """When the raw Kelly fraction exceeds ``max_single_bet``
        (default 0.15), ``kelly_fraction_adjusted`` is clipped to that
        cap and the USD size is correspondingly capped at
        ``max_single_bet * operating_capital`` = $15.

        opp: price=0.5, edge=0.40, confidence=0.90
          kelly = 0.40 / 0.5 * 0.25 = 0.20
          kelly_adjusted = min(0.20, 0.15) = 0.15
          size_usdc = 0.15 * 100 = 15.0
        """
        opt = PortfolioOptimizer()
        result = opt.optimize([_make_opp("big", price=0.5, edge=0.40, confidence=0.90)])
        assert len(result.bets) == 1
        bet = result.bets[0]
        assert bet.kelly_fraction == pytest.approx(0.20, abs=1e-9)
        assert bet.kelly_fraction_adjusted == 0.15
        assert bet.suggested_size_usdc == pytest.approx(15.0, abs=1e-6)
        assert result.total_allocated_usdc == pytest.approx(15.0, abs=1e-6)

    def test_optimize_respects_max_total_exposure(self):
        """When the sum of bet sizes exceeds ``max_total_exposure *
        operating_capital`` (default 0.80 * 100 = $80), the optimizer
        scales the last-selected bet down to fit the remaining budget
        and stops processing further bets.

        Six opportunities at price=0.5, edge=0.40, confidence=0.90
        each yield kelly_adjusted = 0.15 (capped) → $15 each.
        Cumulative: 6 × $15 = $90 > $80 cap. Expected selection:
          - 5 full bets at $15 each ($75 total)
          - 6th bet scaled to remaining $5 (size_usdc = 5.0)
          - total_allocated == $80 exactly
        """
        opt = PortfolioOptimizer()
        opps = [
            _make_opp(f"t{i}", price=0.5, edge=0.40, confidence=0.90)
            for i in range(6)
        ]
        result = opt.optimize(opps)
        # All 6 bets selected (5 full + 1 scaled) — the loop only
        # breaks AFTER processing the bet that didn't fit.
        assert len(result.bets) == 6
        # First 5 are full-size ($15); 6th is scaled to $5.
        for i in range(5):
            assert result.bets[i].suggested_size_usdc == pytest.approx(15.0, abs=1e-6)
        assert result.bets[5].suggested_size_usdc == pytest.approx(5.0, abs=1e-6)
        # Total exactly at the cap.
        assert result.total_allocated_usdc == pytest.approx(80.0, abs=1e-6)

    def test_optimize_skips_bets_when_remaining_too_small(self):
        """When the remaining budget under the max-total-exposure cap
        is less than $1.00 (the ``remaining > 1.0`` floor), the bet is
        dropped entirely (not added with a tiny dust size) and the loop
        breaks.

        Constructed: 6 full-size $15 bets (cap reached at $80, $5
        remaining for the 6th — large enough to add). 7th bet would
        have $5 left → also large enough to add, but the loop already
        broke after the 6th. To exercise the ``remaining <= 1.0``
        branch we need a scenario where the cumulative size leaves
        less than $1 between full bets.

        Easiest path: lower operating_capital so the cap is small.
        With operating_capital=10, max_total_exposure=0.80 → cap $8.
        Two $15-equivalent bets at scale 0.1 of capital → max single
        bet = $1.50 each. Cumulative $3.00, then $4.50, then $6.00,
        then $7.50, then $8.00 (last bet scaled to $0.50 — under
        the $1 floor → dropped).
        """
        opt = PortfolioOptimizer(operating_capital=10.0)
        opps = [
            _make_opp(f"t{i}", price=0.5, edge=0.40, confidence=0.90)
            for i in range(6)
        ]
        result = opt.optimize(opps)
        # max_single_bet = 0.15 * 10 = $1.50 per bet; max_total = 0.80*10 = $8.00
        # 5 full bets = $7.50; 6th would scale to $0.50 → under $1 floor → dropped.
        assert len(result.bets) == 5
        for bet in result.bets:
            assert bet.suggested_size_usdc == pytest.approx(1.50, abs=1e-6)
        assert result.total_allocated_usdc == pytest.approx(7.50, abs=1e-6)

    def test_optimize_computes_diversification_ratio(self):
        """The implementation's diversification ratio is
        ``weighted_avg_risk / portfolio_risk`` where:

          * ``weighted_avg_risk = sum(risk_i * size_i) / total_size``
            (size-weighted average of per-bet risks)

          * ``portfolio_risk = sqrt(sum(risk_i**2))``
            (assuming independence — NOT size-weighted, just the
            Euclidean norm of the per-bet risk vector)

        By Cauchy-Schwarz, the ratio is in (0, 1] — equal to 1.0
        only when the portfolio has a single bet (verified by
        :meth:`test_optimize_selects_bets_above_threshold`).
        For 2+ bets the ratio is strictly less than 1.0.

        NOTE: this is the spec's formula verbatim — it is NOT the
        textbook Sharpe-style diversification ratio (which would be
        ``sum(risk_i) / portfolio_risk`` and would be > 1.0 under
        independence). The implementation's ratio measures
        "size-weighted per-dollar risk relative to total risk",
        which tends DOWNWARD as more bets are added.
        """
        opt = PortfolioOptimizer()
        opps = [
            _make_opp("a", price=0.5, edge=0.10, confidence=0.70),
            _make_opp("b", price=0.4, edge=0.15, confidence=0.80),
        ]
        result = opt.optimize(opps)
        assert len(result.bets) == 2
        # Ratio is in (0, 1] — strictly < 1 with 2+ bets.
        assert 0.0 < result.diversification_ratio < 1.0
        # Sanity: the ratio is the formula the spec defines.
        bet_a, bet_b = result.bets[0], result.bets[1]
        total_alloc = bet_a.suggested_size_usdc + bet_b.suggested_size_usdc
        weighted_avg = (
            bet_a.expected_risk * bet_a.suggested_size_usdc
            + bet_b.expected_risk * bet_b.suggested_size_usdc
        ) / total_alloc
        port_risk = math.sqrt(bet_a.expected_risk**2 + bet_b.expected_risk**2)
        assert result.diversification_ratio == pytest.approx(
            weighted_avg / port_risk, abs=1e-6
        )

    def test_optimize_diversification_ratio_decreases_with_more_bets(self):
        """Adding a third independent bet REDUCES the spec's ratio
        (because the portfolio-risk denominator grows like
        sqrt(n) under equal-risk bets while the weighted-average
        numerator stays constant at the per-bet risk)."""
        opt = PortfolioOptimizer()
        # 2-bet portfolio.
        two = opt.optimize(
            [
                _make_opp("a", price=0.5, edge=0.10, confidence=0.70),
                _make_opp("b", price=0.4, edge=0.15, confidence=0.80),
            ]
        )
        # 3-bet portfolio (same two + a third).
        three = opt.optimize(
            [
                _make_opp("a", price=0.5, edge=0.10, confidence=0.70),
                _make_opp("b", price=0.4, edge=0.15, confidence=0.80),
                _make_opp("c", price=0.3, edge=0.20, confidence=0.85),
            ]
        )
        assert three.diversification_ratio < two.diversification_ratio

    def test_optimize_constraint_violations_empty_under_normal_operation(self):
        """Under the implementation's own caps (max_single_bet +
        max_total_exposure enforced BEFORE the violation check), no
        violation is ever recorded — the violation checks are
        defensive belt-and-braces that fire only if the upstream
        scaling logic is buggy. This test pins that the defensive
        path is currently a no-op so a future regression that DOES
        surface a violation isn't silently masked."""
        opt = PortfolioOptimizer()
        opps = [
            _make_opp(f"t{i}", price=0.5, edge=0.40, confidence=0.90)
            for i in range(6)
        ]
        result = opt.optimize(opps)
        assert result.constraint_violations == []

    def test_optimize_to_dict_is_json_serialisable(self):
        """The ``to_dict()`` view returns plain JSON-compatible types
        (no dataclass instances leaked) so the FastAPI route handler
        can return it directly without an explicit serialiser."""
        import json

        opt = PortfolioOptimizer()
        opps = [_make_opp("t1", price=0.5, edge=0.10, confidence=0.7)]
        result = opt.optimize(opps)
        d = result.to_dict()
        # The dict round-trips through json.dumps with no TypeError.
        s = json.dumps(d)
        assert '"bets"' in s
        assert '"total_allocated_usdc"' in s
        assert '"diversification_ratio"' in s

    def test_optimize_handles_missing_fields_with_defaults(self):
        """Opportunities missing ``price`` / ``edge`` / ``confidence``
        fall back to the defaults (0.5 / 0 / 0.5) — the default
        edge=0 is below ``min_edge`` so the bet is rejected."""
        opt = PortfolioOptimizer()
        result = opt.optimize([{"token_id": "incomplete"}])
        assert result.bets == []
        # Sanity: with edge default = 0, the bet is filtered out by
        # compute_kelly's ``edge < min_edge`` gate.


# ── (3) Unit tests: suggest_rebalance ────────────────────────────────────────


class TestSuggestRebalance:
    """Direct coverage of :meth:`PortfolioOptimizer.suggest_rebalance`."""

    def test_rebalance_adds_new_position(self):
        """When the optimizer suggests a bet whose ``token_id`` is NOT
        in the current positions, the rebalance output adds it to the
        ``add`` list with the suggested USD size + a Kelly/edge reason
        string."""
        opt = PortfolioOptimizer()
        opps = [_make_opp("new_token", price=0.5, edge=0.10, confidence=0.7)]
        actions = opt.suggest_rebalance(current_positions=[], opportunities=opps)
        assert len(actions["add"]) == 1
        assert actions["add"][0]["token_id"] == "new_token"
        assert actions["add"][0]["size_usdc"] == pytest.approx(5.0, abs=1e-6)
        assert "Kelly=" in actions["add"][0]["reason"]

    def test_rebalance_closes_position_not_in_suggestions(self):
        """When a current position's ``token_id`` is NOT in the
        optimizer's suggestion set (no edge / below threshold), the
        rebalance output adds it to the ``close`` list with a
        'No edge or below threshold' reason."""
        opt = PortfolioOptimizer()
        current = [{"token_id": "stale_token", "size_usdc": 5.0}]
        # Empty opportunities → optimizer suggests nothing → every
        # current position is closed.
        actions = opt.suggest_rebalance(current_positions=current, opportunities=[])
        assert len(actions["close"]) == 1
        assert actions["close"][0]["token_id"] == "stale_token"

    def test_rebalance_increases_undersized_position(self):
        """When a current position exists with a size MUCH smaller
        than the Kelly target (> 20 % diff), the rebalance output
        adds a delta-add entry to grow it.

        Kelly target for price=0.5, edge=0.10, conf=0.7 is $5.00.
        Current size $1.00 → diff is 80 % (> 20 %) → grow by $4.00.
        """
        opt = PortfolioOptimizer()
        opps = [_make_opp("t1", price=0.5, edge=0.10, confidence=0.7)]
        current = [{"token_id": "t1", "size_usdc": 1.0}]
        actions = opt.suggest_rebalance(current_positions=current, opportunities=opps)
        # No 'add new' (the token already exists in current_positions
        # so the "is not in current_tokens" branch is skipped) — the
        # rebalance path produces a 'add' (delta) entry instead.
        delta_adds = [a for a in actions["add"] if a["token_id"] == "t1"]
        assert len(delta_adds) == 1
        assert delta_adds[0]["size_usdc"] == pytest.approx(4.0, abs=1e-6)
        assert delta_adds[0]["reason"] == "Increase to Kelly target"

    def test_rebalance_reduces_oversized_position(self):
        """When a current position exists with a size MUCH larger than
        the Kelly target (> 20 % diff), the rebalance output adds a
        'reduce' entry to shrink it.

        Kelly target $5.00; current size $10.00 → diff 50 % (> 20 %)
        → reduce by $5.00.
        """
        opt = PortfolioOptimizer()
        opps = [_make_opp("t1", price=0.5, edge=0.10, confidence=0.7)]
        current = [{"token_id": "t1", "size_usdc": 10.0}]
        actions = opt.suggest_rebalance(current_positions=current, opportunities=opps)
        assert len(actions["reduce"]) == 1
        assert actions["reduce"][0]["token_id"] == "t1"
        assert actions["reduce"][0]["size_usdc"] == pytest.approx(5.0, abs=1e-6)
        assert actions["reduce"][0]["reason"] == "Reduce to Kelly target"

    def test_rebalance_holds_within_tolerance(self):
        """When a current position exists with a size within 20 % of
        the Kelly target, the rebalance output adds it to the
        ``hold`` list (no action needed).

        Kelly target $5.00; current size $5.50 → diff 10 % (< 20 %) →
        hold.
        """
        opt = PortfolioOptimizer()
        opps = [_make_opp("t1", price=0.5, edge=0.10, confidence=0.7)]
        current = [{"token_id": "t1", "size_usdc": 5.5}]
        actions = opt.suggest_rebalance(current_positions=current, opportunities=opps)
        assert len(actions["hold"]) == 1
        assert actions["hold"][0]["token_id"] == "t1"
        assert actions["add"] == []
        assert actions["reduce"] == []

    def test_rebalance_handles_empty_current_positions(self):
        """No current positions → every suggestion becomes an 'add'."""
        opt = PortfolioOptimizer()
        opps = [
            _make_opp("t1", price=0.5, edge=0.10, confidence=0.7),
            _make_opp("t2", price=0.4, edge=0.15, confidence=0.8),
        ]
        actions = opt.suggest_rebalance(current_positions=[], opportunities=opps)
        assert len(actions["add"]) == 2
        assert {a["token_id"] for a in actions["add"]} == {"t1", "t2"}
        assert actions["close"] == []
        assert actions["reduce"] == []
        assert actions["hold"] == []

    def test_rebalance_handles_empty_opportunities(self):
        """No opportunities → optimizer suggests nothing → every
        current position is closed."""
        opt = PortfolioOptimizer()
        current = [
            {"token_id": "t1", "size_usdc": 5.0},
            {"token_id": "t2", "size_usdc": 3.0},
        ]
        actions = opt.suggest_rebalance(current_positions=current, opportunities=[])
        assert len(actions["close"]) == 2
        assert {a["token_id"] for a in actions["close"]} == {"t1", "t2"}
        assert actions["add"] == []
        assert actions["reduce"] == []
        assert actions["hold"] == []

    def test_rebalance_mixed_actions(self):
        """A full mix: one new position to add, one to close, one to
        reduce, one to hold — all in one rebalance output."""
        opt = PortfolioOptimizer()
        opps = [
            # 'add_new' — new position (not in current_positions).
            _make_opp("add_new", price=0.5, edge=0.10, confidence=0.7),
            # 'oversized' — current $20, Kelly target $5 → reduce by $15.
            _make_opp("oversized", price=0.5, edge=0.10, confidence=0.7),
            # 'within_tol' — current $5.5, Kelly target $5 → hold.
            _make_opp("within_tol", price=0.5, edge=0.10, confidence=0.7),
        ]
        current = [
            {"token_id": "oversized", "size_usdc": 20.0},
            {"token_id": "within_tol", "size_usdc": 5.5},
            {"token_id": "stale", "size_usdc": 5.0},  # not in opps → close
        ]
        actions = opt.suggest_rebalance(current_positions=current, opportunities=opps)
        # 'add_new' is the only "new" bet — goes into 'add' with the
        # full Kelly size + the "Kelly=…" reason.
        assert len(actions["add"]) == 1
        assert actions["add"][0]["token_id"] == "add_new"
        # 'oversized' is in 'reduce' (current > Kelly target by > 20 %).
        assert len(actions["reduce"]) == 1
        assert actions["reduce"][0]["token_id"] == "oversized"
        assert actions["reduce"][0]["size_usdc"] == pytest.approx(15.0, abs=1e-6)
        # 'within_tol' is in 'hold' (within 20 % of the Kelly target).
        assert len(actions["hold"]) == 1
        assert actions["hold"][0]["token_id"] == "within_tol"
        # 'stale' is in 'close' (not in the opportunity set).
        assert len(actions["close"]) == 1
        assert actions["close"][0]["token_id"] == "stale"


# ── (4) Unit tests: get_config / update_config ──────────────────────────────


class TestConfig:
    """Direct coverage of :meth:`get_config` / :meth:`update_config`."""

    def test_get_config_returns_all_defaults(self):
        """A freshly-constructed ``PortfolioOptimizer`` returns the
        six documented defaults verbatim from ``get_config``."""
        opt = PortfolioOptimizer()
        cfg = opt.get_config()
        assert set(cfg.keys()) == {
            "operating_capital",
            "kelly_fraction",
            "max_single_bet",
            "max_total_exposure",
            "min_edge",
            "min_confidence",
        }
        assert cfg["operating_capital"] == 100.0
        assert cfg["kelly_fraction"] == 0.25
        assert cfg["max_single_bet"] == 0.15
        assert cfg["max_total_exposure"] == 0.80
        assert cfg["min_edge"] == 0.03
        assert cfg["min_confidence"] == 0.55

    def test_update_config_partial_update(self):
        """Supplying only one field mutates ONLY that field; the
        others keep their prior value. Returns the post-update full
        config so the caller can echo it back."""
        opt = PortfolioOptimizer()
        new_cfg = opt.update_config(kelly_fraction=0.50)
        assert new_cfg["kelly_fraction"] == 0.50
        # Untouched fields keep their defaults.
        assert new_cfg["operating_capital"] == 100.0
        assert new_cfg["max_single_bet"] == 0.15
        # The mutation persists on the instance.
        assert opt.kelly_fraction == 0.50

    def test_update_config_full_update(self):
        """Supplying every field mutates them all at once."""
        opt = PortfolioOptimizer()
        new_cfg = opt.update_config(
            operating_capital=250.0,
            kelly_fraction=0.40,
            max_single_bet=0.20,
            max_total_exposure=0.90,
            min_edge=0.05,
            min_confidence=0.60,
        )
        assert new_cfg == {
            "operating_capital": 250.0,
            "kelly_fraction": 0.40,
            "max_single_bet": 0.20,
            "max_total_exposure": 0.90,
            "min_edge": 0.05,
            "min_confidence": 0.60,
        }

    def test_update_config_unknown_key_raises(self):
        """An unknown config key raises ``ValueError`` so a malformed
        PUT body surfaces clearly (the route handler maps this to a
        422 response)."""
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="unknown config keys"):
            opt.update_config(totally_made_up_key=42)

    def test_update_config_rejects_non_positive_operating_capital(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="operating_capital"):
            opt.update_config(operating_capital=0.0)
        with pytest.raises(ValueError, match="operating_capital"):
            opt.update_config(operating_capital=-10.0)

    def test_update_config_rejects_invalid_kelly_fraction(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="kelly_fraction"):
            opt.update_config(kelly_fraction=0.0)
        with pytest.raises(ValueError, match="kelly_fraction"):
            opt.update_config(kelly_fraction=1.5)

    def test_update_config_rejects_invalid_max_single_bet(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="max_single_bet"):
            opt.update_config(max_single_bet=0.0)
        with pytest.raises(ValueError, match="max_single_bet"):
            opt.update_config(max_single_bet=2.0)

    def test_update_config_rejects_invalid_max_total_exposure(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="max_total_exposure"):
            opt.update_config(max_total_exposure=0.0)
        with pytest.raises(ValueError, match="max_total_exposure"):
            opt.update_config(max_total_exposure=1.5)

    def test_update_config_rejects_invalid_min_edge(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="min_edge"):
            opt.update_config(min_edge=-0.1)
        with pytest.raises(ValueError, match="min_edge"):
            opt.update_config(min_edge=1.0)

    def test_update_config_rejects_invalid_min_confidence(self):
        opt = PortfolioOptimizer()
        with pytest.raises(ValueError, match="min_confidence"):
            opt.update_config(min_confidence=-0.1)
        with pytest.raises(ValueError, match="min_confidence"):
            opt.update_config(min_confidence=1.5)

    def test_update_config_coerces_int_to_float(self):
        """A JSON ``int`` (e.g. ``100``) lands as ``100.0`` so
        downstream arithmetic stays float-consistent."""
        opt = PortfolioOptimizer()
        new_cfg = opt.update_config(operating_capital=200)  # int, not float
        assert new_cfg["operating_capital"] == 200.0
        assert isinstance(new_cfg["operating_capital"], float)


# ── (5) API tests: register_routes ───────────────────────────────────────────


@pytest.fixture
def _restore_singleton_config():
    """Snapshot the module-level singleton's config before each API
    test and restore it in teardown so a ``PUT /api/portfolio/config``
    in one test doesn't leak into the next.

    The route handlers in ``register_routes`` reference the module
    global ``portfolio_optimizer`` at call time (closure over the
    module namespace), so mutating its attributes is picked up by
    every subsequent handler without re-registration.
    """
    snapshot = _po_module.portfolio_optimizer.get_config()
    yield _po_module.portfolio_optimizer
    # Restore — assign each attribute back so any in-test mutation
    # is fully reverted even if the test crashed mid-way.
    _po_module.portfolio_optimizer.operating_capital = snapshot["operating_capital"]
    _po_module.portfolio_optimizer.kelly_fraction = snapshot["kelly_fraction"]
    _po_module.portfolio_optimizer.max_single_bet = snapshot["max_single_bet"]
    _po_module.portfolio_optimizer.max_total_exposure = snapshot["max_total_exposure"]
    _po_module.portfolio_optimizer.min_edge = snapshot["min_edge"]
    _po_module.portfolio_optimizer.min_confidence = snapshot["min_confidence"]


@pytest.fixture
def client(_restore_singleton_config) -> TestClient:
    """Fresh ``FastAPI`` app with only the portfolio-optimizer routes
    registered.

    Uses the same ``register_routes(app)`` entry point as the
    production ``api/server.py`` (W16-3 block) so the route definitions
    / Pydantic validation annotations exercised here are byte-identical
    to what the live server exposes — without the bearer-token auth
    middleware or the heavy ``lifespan`` startup.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestPortfolioOptimizerRoutes:
    """HTTP-level coverage of the four ``/api/portfolio/*`` endpoints."""

    def test_post_optimize_returns_bets_and_metrics(self, client: TestClient):
        """``POST /api/portfolio/optimize`` with one above-threshold
        opportunity returns 200 with the bet, total allocated, expected
        return / risk, and a diversification ratio."""
        response = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "t1",
                        "strategy": "test",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["bets"]) == 1
        assert body["bets"][0]["token_id"] == "t1"
        assert body["total_allocated_usdc"] == pytest.approx(5.0, abs=1e-6)
        assert body["total_expected_return"] == pytest.approx(0.50, abs=1e-6)
        assert body["diversification_ratio"] == pytest.approx(1.0, abs=1e-6)
        assert body["constraint_violations"] == []

    def test_post_optimize_with_empty_body(self, client: TestClient):
        """An empty ``opportunities`` list yields an empty
        optimization (HTTP 200, not 4xx — empty is a valid request)."""
        response = client.post(
            "/api/portfolio/optimize",
            json={"opportunities": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["bets"] == []
        assert body["total_allocated_usdc"] == 0.0
        assert body["diversification_ratio"] == 1.0

    def test_post_optimize_with_omitted_opportunities(self, client: TestClient):
        """Omitting the ``opportunities`` field entirely falls back to
        the model's ``default_factory=list`` — same empty-optimization
        response as an explicit empty list."""
        response = client.post("/api/portfolio/optimize", json={})
        assert response.status_code == 200
        assert response.json()["bets"] == []

    def test_post_optimize_rejects_out_of_range_price(self, client: TestClient):
        """A price > 1.0 violates the Pydantic ``Field(ge=0, le=1)``
        constraint on :class:`Opportunity` → HTTP 422."""
        response = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "bad",
                        "price": 1.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert response.status_code == 422

    def test_post_optimize_rejects_out_of_range_edge(self, client: TestClient):
        """An edge outside [-1, 1] is rejected with 422."""
        response = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "bad",
                        "price": 0.5,
                        "edge": 2.0,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert response.status_code == 422

    def test_post_rebalance_returns_add_action(self, client: TestClient):
        """``POST /api/portfolio/rebalance`` with one opportunity that
        is NOT in the current positions returns 200 with one 'add'
        entry."""
        response = client.post(
            "/api/portfolio/rebalance",
            json={
                "current_positions": [],
                "opportunities": [
                    {
                        "token_id": "new",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["add"]) == 1
        assert body["add"][0]["token_id"] == "new"
        assert body["reduce"] == []
        assert body["close"] == []
        assert body["hold"] == []

    def test_post_rebalance_returns_close_action(self, client: TestClient):
        """``POST /api/portfolio/rebalance`` with one current position
        that has NO matching opportunity returns 200 with one 'close'
        entry."""
        response = client.post(
            "/api/portfolio/rebalance",
            json={
                "current_positions": [{"token_id": "stale", "size_usdc": 5.0}],
                "opportunities": [],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["close"]) == 1
        assert body["close"][0]["token_id"] == "stale"

    def test_post_rebalance_returns_hold_action(self, client: TestClient):
        """A current position within 20 % of its Kelly target lands in
        the 'hold' list (no action)."""
        response = client.post(
            "/api/portfolio/rebalance",
            json={
                "current_positions": [{"token_id": "t1", "size_usdc": 5.5}],
                "opportunities": [
                    {
                        "token_id": "t1",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ],
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert len(body["hold"]) == 1
        assert body["hold"][0]["token_id"] == "t1"
        assert body["add"] == []
        assert body["reduce"] == []

    def test_get_config_returns_current_config(self, client: TestClient):
        """``GET /api/portfolio/config`` returns 200 with the six
        documented config scalars at their default values."""
        response = client.get("/api/portfolio/config")
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {
            "operating_capital",
            "kelly_fraction",
            "max_single_bet",
            "max_total_exposure",
            "min_edge",
            "min_confidence",
        }
        assert body["operating_capital"] == 100.0
        assert body["kelly_fraction"] == 0.25
        assert body["max_single_bet"] == 0.15
        assert body["max_total_exposure"] == 0.80
        assert body["min_edge"] == 0.03
        assert body["min_confidence"] == 0.55

    def test_put_config_updates_partial(self, client: TestClient, _restore_singleton_config):
        """``PUT /api/portfolio/config`` with one field mutates ONLY
        that field and returns 200 with the post-update full config."""
        response = client.put(
            "/api/portfolio/config",
            json={"kelly_fraction": 0.50},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["config"]["kelly_fraction"] == 0.50
        # Untouched fields keep their defaults.
        assert body["config"]["operating_capital"] == 100.0
        assert body["config"]["max_single_bet"] == 0.15

    def test_put_config_rejects_unknown_field(self, client: TestClient):
        """An unknown config field yields 422. The Pydantic
        ``extra="forbid"`` config on :class:`ConfigUpdate` rejects
        the unknown key at the model-validation layer (BEFORE the
        route handler is reached) — so the response body carries a
        Pydantic validation-error list whose ``type`` is
        ``extra_forbidden``, not the route handler's plain-string
        ``ValueError`` detail.

        This is the defensive layer: it surfaces a typo'd key (e.g.
        ``kelley_fraction``) up-front so the operator's intended
        change isn't silently dropped as a no-op.
        """
        response = client.put(
            "/api/portfolio/config",
            json={"totally_made_up_key": 42},
        )
        assert response.status_code == 422
        body = response.json()
        assert "detail" in body
        # Pydantic v2 validation error body is a list of error dicts.
        assert isinstance(body["detail"], list)
        assert len(body["detail"]) >= 1
        # The first error references the unknown key + the
        # ``extra_forbidden`` type.
        first = body["detail"][0]
        assert "totally_made_up_key" in first.get("loc", [])
        assert first.get("type") == "extra_forbidden"

    def test_put_config_rejects_out_of_range(self, client: TestClient):
        """An out-of-range value (e.g. ``kelly_fraction=2.0``) yields
        422."""
        response = client.put(
            "/api/portfolio/config",
            json={"kelly_fraction": 2.0},
        )
        assert response.status_code == 422

    def test_put_config_affects_subsequent_optimize(
        self, client: TestClient, _restore_singleton_config
    ):
        """A ``PUT`` that lowers ``kelly_fraction`` from 0.25 to 0.10
        must reduce the suggested bet size on the NEXT ``POST
        /optimize`` call — proving the singleton mutation is picked
        up live (no restart needed)."""
        # Baseline: kelly_fraction = 0.25 → size = 5.0 for our test opp.
        baseline = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "t1",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert baseline.status_code == 200
        baseline_size = baseline.json()["bets"][0]["suggested_size_usdc"]
        assert baseline_size == pytest.approx(5.0, abs=1e-6)

        # Halve the Kelly fraction (0.25 → 0.125).
        put = client.put("/api/portfolio/config", json={"kelly_fraction": 0.125})
        assert put.status_code == 200

        # Re-run optimize → size should halve (linear in kelly_fraction).
        after = client.post(
            "/api/portfolio/optimize",
            json={
                "opportunities": [
                    {
                        "token_id": "t1",
                        "price": 0.5,
                        "edge": 0.10,
                        "confidence": 0.7,
                    }
                ]
            },
        )
        assert after.status_code == 200
        after_size = after.json()["bets"][0]["suggested_size_usdc"]
        assert after_size == pytest.approx(baseline_size / 2.0, abs=1e-6)

    def test_put_config_with_empty_body_is_noop(self, client: TestClient):
        """An empty JSON body (no fields to update) is a no-op: the
        route handler filters ``None`` values out before calling
        ``update_config``, so an empty update dict reaches the
        optimizer → no mutation, 200 response with the unchanged
        config."""
        # Snapshot before.
        before = client.get("/api/portfolio/config").json()
        # Empty PUT.
        response = client.put("/api/portfolio/config", json={})
        assert response.status_code == 200
        assert response.json()["config"] == before

    def test_routes_tagged_under_portfolio(self, client: TestClient):
        """The four routes carry the ``portfolio`` tag (visible in the
        FastAPI OpenAPI schema so the dashboard can group them)."""
        # Use the OpenAPI schema to introspect tags without parsing
        # the source. The TestClient exposes the app via .app.
        schema = client.app.openapi()  # type: ignore[attr-defined]
        paths = schema.get("paths", {})
        for path in (
            "/api/portfolio/optimize",
            "/api/portfolio/rebalance",
            "/api/portfolio/config",
        ):
            assert path in paths, f"missing path {path} in OpenAPI schema"
            for method_meta in paths[path].values():
                assert "portfolio" in method_meta.get("tags", []), (
                    f"path {path} not tagged 'portfolio': {method_meta.get('tags')}"
                )
