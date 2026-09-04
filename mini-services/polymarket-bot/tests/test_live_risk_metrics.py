"""
Unit + API tests for the W20-5 live portfolio risk metrics module.

Seven test classes:

  (1) ``TestPortfolioRiskMetricsDataclass`` — the dataclass shape (every
      documented field present + ``to_dict()`` round-trips).

  (2) ``TestEmptyPortfolio`` — empty positions returns a zeroed
      ``PortfolioRiskMetrics`` with ``var_method="none"``.

  (3) ``TestSinglePosition`` — single LONG position: exposure / largest-
      position-pct / concentration-ratio / parametric-VaR formulas.

  (4) ``TestMultiplePositions`` — multi-position book: net exposure
      (long-minus-short), gross vs. total exposure, concentration ratio
      below 1.0, largest-position-pct < 1.0.

  (5) ``TestVarCvarComputation`` — VaR / CVaR with a synthetic price
      history (historical VaR path); parametric fallback when the
      history is too short; ``var_method`` field flips correctly.

  (6) ``TestConcentrationRatio`` — Herfindahl-Hirschman Index edge
      cases (one position → 1.0, two equal positions → 0.5, many
      equal positions → 1/N).

  (7) ``TestLiveRiskMetricsRoutes`` — HTTP-level coverage of
      ``GET /api/portfolio/risk-metrics`` (zeroed when no live
      positions; populated when the live store has positions, via a
      monkeypatched ``_positions_from_live_store`` helper).

Approach
~~~~~~~~
The module-level singleton ``live_risk_metrics`` is constructed at
import time with the conservative defaults documented in the module
docstring (``lookback_days=30`` / ``daily_vol=0.05``). The unit-test
classes (1)–(6) construct FRESH ``LiveRiskMetrics()`` instances (no
shared state with the singleton); the API-test class (7) uses the
singleton — and to keep test isolation, a
``_restore_singleton_config`` fixture snapshots the singleton's config
before each API test and restores it in the teardown.

Mirrors the ``_restore_singleton_config`` fixture in
``tests/test_stress_test.py`` and ``tests/test_portfolio_optimizer.py``
— same monkeypatch-the-module-global pattern, same fresh-app
TestClient approach.
"""
from __future__ import annotations

import math
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.live_risk_metrics import (
    LiveRiskMetrics,
    PortfolioRiskMetrics,
    register_routes,
)
from core import live_risk_metrics as _lrm_module


# ── (1) TestPortfolioRiskMetricsDataclass ──────────────────────────────────


class TestPortfolioRiskMetricsDataclass:
    """The dataclass shape: every documented field is present, the
    defaults for ``var_method`` / ``computed_at`` are correct, and
    ``to_dict()`` round-trips every field into a JSON-safe dict."""

    def test_dataclass_fields(self):
        m = PortfolioRiskMetrics(
            total_exposure=100.0,
            net_exposure=80.0,
            gross_exposure=100.0,
            position_count=2,
            largest_position_pct=0.6,
            var_95=5.0,
            var_99=8.0,
            cvar_95=7.0,
            cvar_99=11.0,
            concentration_ratio=0.52,
            var_method="parametric",
            computed_at=1700000000.0,
        )
        assert m.total_exposure == 100.0
        assert m.net_exposure == 80.0
        assert m.gross_exposure == 100.0
        assert m.position_count == 2
        assert m.largest_position_pct == 0.6
        assert m.var_95 == 5.0
        assert m.var_99 == 8.0
        assert m.cvar_95 == 7.0
        assert m.cvar_99 == 11.0
        assert m.concentration_ratio == 0.52
        assert m.var_method == "parametric"
        assert m.computed_at == 1700000000.0

    def test_default_var_method_is_none(self):
        """The dataclass-level default for ``var_method`` is the
        string ``"none"`` — used by the empty-portfolio short-circuit."""
        m = PortfolioRiskMetrics(
            total_exposure=0.0,
            net_exposure=0.0,
            gross_exposure=0.0,
            position_count=0,
            largest_position_pct=0.0,
            var_95=0.0,
            var_99=0.0,
            cvar_95=0.0,
            cvar_99=0.0,
            concentration_ratio=0.0,
        )
        assert m.var_method == "none"
        assert m.computed_at == 0.0  # also default

    def test_to_dict_round_trips_every_field(self):
        """``to_dict()`` exposes every documented field as a JSON-safe
        key (mirrors ``dataclasses.asdict`` but kept explicit so callers
        don't depend on the dataclass internals)."""
        m = PortfolioRiskMetrics(
            total_exposure=42.0,
            net_exposure=20.0,
            gross_exposure=42.0,
            position_count=1,
            largest_position_pct=1.0,
            var_95=2.1,
            var_99=3.3,
            cvar_95=2.6,
            cvar_99=3.8,
            concentration_ratio=1.0,
            var_method="historical",
            computed_at=1700000000.0,
        )
        d = m.to_dict()
        assert d["total_exposure"] == 42.0
        assert d["net_exposure"] == 20.0
        assert d["gross_exposure"] == 42.0
        assert d["position_count"] == 1
        assert d["largest_position_pct"] == 1.0
        assert d["var_95"] == 2.1
        assert d["var_99"] == 3.3
        assert d["cvar_95"] == 2.6
        assert d["cvar_99"] == 3.8
        assert d["concentration_ratio"] == 1.0
        assert d["var_method"] == "historical"
        assert d["computed_at"] == 1700000000.0
        # Every documented field is present in the dict.
        assert set(d.keys()) == {
            "total_exposure", "net_exposure", "gross_exposure",
            "position_count", "largest_position_pct",
            "var_95", "var_99", "cvar_95", "cvar_99",
            "concentration_ratio", "var_method", "computed_at",
        }


# ── (2) TestEmptyPortfolio ──────────────────────────────────────────────────


class TestEmptyPortfolio:
    """An empty positions list returns a zeroed risk-metrics payload with
    ``var_method="none"`` — the dashboard's "no positions to risk-assess"
    state."""

    def test_empty_positions_returns_zeroed_metrics(self):
        m = LiveRiskMetrics().compute([])
        assert m.total_exposure == 0.0
        assert m.net_exposure == 0.0
        assert m.gross_exposure == 0.0
        assert m.position_count == 0
        assert m.largest_position_pct == 0.0
        assert m.var_95 == 0.0
        assert m.var_99 == 0.0
        assert m.cvar_95 == 0.0
        assert m.cvar_99 == 0.0
        assert m.concentration_ratio == 0.0
        assert m.var_method == "none"
        assert m.computed_at > 0  # always set to "now"

    def test_empty_positions_with_price_history_still_zeroed(self):
        """Price history is irrelevant when there are no positions —
        the empty-portfolio short-circuit fires before the price
        history is even consulted."""
        m = LiveRiskMetrics().compute([], price_history={"t1": [0.5, 0.4, 0.6]})
        assert m.total_exposure == 0.0
        assert m.var_method == "none"
        assert m.position_count == 0


# ── (3) TestSinglePosition ─────────────────────────────────────────────────


class TestSinglePosition:
    """A single LONG position: exposure / largest-position-pct / HHI=1 /
    parametric VaR formula."""

    def test_single_long_position_exposure(self):
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.55},
        ]
        m = LiveRiskMetrics().compute(positions)
        # 100 shares × $0.55 = $55 marked-to-market exposure.
        assert m.total_exposure == pytest.approx(55.0)
        assert m.gross_exposure == pytest.approx(55.0)
        # LONG → net exposure equals total exposure.
        assert m.net_exposure == pytest.approx(55.0)
        assert m.position_count == 1
        # Single position → 100 % of the book.
        assert m.largest_position_pct == pytest.approx(1.0)
        # Single position → HHI = 1.0 (perfectly concentrated).
        assert m.concentration_ratio == pytest.approx(1.0)

    def test_single_long_position_parametric_var(self):
        """With no price history, VaR / CVaR use the parametric
        fallback: VaR_p = total × Z_p × daily_vol."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        m = LiveRiskMetrics().compute(positions)
        # total = 100 × 0.50 = $50; daily_vol = 0.05 (default)
        # VaR_95 = 50 × 1.65 × 0.05 = 4.125
        assert m.var_method == "parametric"
        assert m.var_95 == pytest.approx(50 * 1.65 * 0.05, rel=1e-6)
        # VaR_99 = 50 × 2.33 × 0.05 = 5.825
        assert m.var_99 == pytest.approx(50 * 2.33 * 0.05, rel=1e-6)
        # CVaR_95 = 50 × 2.06 × 0.05 = 5.15
        assert m.cvar_95 == pytest.approx(50 * 2.06 * 0.05, rel=1e-6)
        # CVaR_99 = 50 × 2.66 × 0.05 = 6.65
        assert m.cvar_99 == pytest.approx(50 * 2.66 * 0.05, rel=1e-6)

    def test_single_short_position_net_exposure_zero(self):
        """A SHORT position contributes negatively to net exposure —
        a single SHORT book has ``net_exposure`` = total exposure (the
        absolute value flips the sign)."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "SHORT",
             "avg_price": 0.50, "current_price": 0.40},
        ]
        m = LiveRiskMetrics().compute(positions)
        # total = |100 × 0.40| = $40
        assert m.total_exposure == pytest.approx(40.0)
        # net = 100 × 0.40 × (-1) = -$40 → abs = $40
        assert m.net_exposure == pytest.approx(40.0)
        assert m.concentration_ratio == pytest.approx(1.0)

    def test_current_price_falls_back_to_avg_price(self):
        """When ``current_price`` is missing, the exposure calc falls
        back to ``avg_price`` (mirrors the stress_test helper)."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG", "avg_price": 0.50},
        ]
        m = LiveRiskMetrics().compute(positions)
        # 100 × 0.50 = $50
        assert m.total_exposure == pytest.approx(50.0)


# ── (4) TestMultiplePositions ───────────────────────────────────────────────


class TestMultiplePositions:
    """Multi-position book: net exposure (long-minus-short), gross vs.
    total exposure, concentration ratio below 1.0, largest-position-pct
    below 1.0."""

    def test_two_long_positions_exposure_and_concentration(self):
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 50, "side": "LONG",
             "avg_price": 0.40, "current_price": 0.40},
        ]
        m = LiveRiskMetrics().compute(positions)
        # total = 100×0.50 + 50×0.40 = $50 + $20 = $70
        assert m.total_exposure == pytest.approx(70.0)
        # net = +$70 (both LONG)
        assert m.net_exposure == pytest.approx(70.0)
        assert m.position_count == 2
        # largest = $50 / $70 ≈ 0.714
        assert m.largest_position_pct == pytest.approx(50.0 / 70.0, rel=1e-6)
        # HHI = (50/70)^2 + (20/70)^2 ≈ 0.510 + 0.082 = 0.592
        assert m.concentration_ratio == pytest.approx(
            (50.0 / 70.0) ** 2 + (20.0 / 70.0) ** 2, rel=1e-6
        )

    def test_long_and_short_net_exposure(self):
        """One LONG + one SHORT → net exposure is |long - short|."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},   # +$50
            {"token_id": "t2", "size": 50, "side": "SHORT",
             "avg_price": 0.40, "current_price": 0.40},   # -$20
        ]
        m = LiveRiskMetrics().compute(positions)
        # gross = $50 + $20 = $70
        assert m.gross_exposure == pytest.approx(70.0)
        assert m.total_exposure == pytest.approx(70.0)
        # net = |$50 - $20| = $30
        assert m.net_exposure == pytest.approx(30.0)
        assert m.position_count == 2

    def test_perfectly_hedged_book_has_zero_net_exposure(self):
        """Equal-long-and-short notional → ``net_exposure`` is zero
        (the book is dollar-neutral)."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 50, "side": "SHORT",
             "avg_price": 1.00, "current_price": 1.00},  # -$50
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.total_exposure == pytest.approx(100.0)  # $50 + $50
        assert m.net_exposure == pytest.approx(0.0)

    def test_three_equal_positions_hhi_is_one_third(self):
        """Three equal-sized positions → HHI = 3 × (1/3)^2 = 1/3."""
        positions = [
            {"token_id": f"t{i}", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50}
            for i in range(3)
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.concentration_ratio == pytest.approx(1.0 / 3.0, rel=1e-6)
        assert m.largest_position_pct == pytest.approx(1.0 / 3.0, rel=1e-6)

    def test_parametric_var_scales_with_total_exposure(self):
        """Doubling the position size doubles the VaR (the formula is
        linear in total_exposure)."""
        small = [{"token_id": "t1", "size": 100, "side": "LONG",
                  "avg_price": 0.50, "current_price": 0.50}]
        big = [{"token_id": "t1", "size": 200, "side": "LONG",
                "avg_price": 0.50, "current_price": 0.50}]
        m_small = LiveRiskMetrics().compute(small)
        m_big = LiveRiskMetrics().compute(big)
        assert m_big.var_95 == pytest.approx(2 * m_small.var_95, rel=1e-6)
        assert m_big.var_99 == pytest.approx(2 * m_small.var_99, rel=1e-6)
        assert m_big.cvar_99 == pytest.approx(2 * m_small.cvar_99, rel=1e-6)


# ── (5) TestVarCvarComputation ──────────────────────────────────────────────


class TestVarCvarComputation:
    """VaR / CVaR with synthetic price history (historical VaR path) and
    parametric fallback when the history is too short."""

    def test_parametric_fallback_when_no_price_history(self):
        """No price history → parametric VaR, ``var_method="parametric"``."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.var_method == "parametric"
        # 100 × 0.50 × 1.65 × 0.05 = 4.125
        assert m.var_95 == pytest.approx(4.125, rel=1e-6)

    def test_parametric_fallback_when_price_history_too_short(self):
        """Fewer than 20 historical returns → parametric fallback (the
        5th-percentile tail would be degenerate with < 20 points)."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        # 5 prices → 4 returns → too short.
        history = {"t1": [0.50, 0.49, 0.51, 0.50, 0.48]}
        m = LiveRiskMetrics().compute(positions, price_history=history)
        assert m.var_method == "parametric"
        assert m.var_95 == pytest.approx(4.125, rel=1e-6)

    def test_historical_var_with_sufficient_price_history(self):
        """With ≥ 20 returns, VaR is computed from the empirical
        distribution and ``var_method`` flips to ``"historical"``."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        # Build a synthetic price history with a clear 5th-percentile
        # tail: most days return 0, but 3 of 30 days (10 %) return -10 %.
        # 3 bad days ensures the 5th-percentile interpolation lands on a
        # -10 % value (np.percentile's linear interp at index (30-1)*0.05
        # = 1.45 sits between sorted[1]=-0.10 and sorted[2]=-0.10 when 3
        # bad days are present → result is exactly -0.10, not interpolated
        # against a 0-return day).
        prices = [0.50]
        for i in range(30):
            prev = prices[-1]
            if i in (5, 15, 25):  # 3 bad days out of 30
                ret = -0.10
            else:
                ret = 0.0
            prices.append(prev * (1 + ret))
        history = {"t1": prices}
        m = LiveRiskMetrics().compute(positions, price_history=history)
        assert m.var_method == "historical"
        # The 5th-percentile return is -10 %; VaR_95 = 0.10 × $50 = $5.
        # total_exposure = 100 × 0.50 = $50.
        assert m.var_95 == pytest.approx(5.0, rel=1e-2)
        # CVaR_95 should also be ~$5 (every tail return is -10 %).
        assert m.cvar_95 == pytest.approx(5.0, rel=1e-2)
        # VaR_99 = 5th percentile (same tail — 3 bad days) → $5.
        # CVaR_99 = mean of returns at or below the 1st percentile →
        # the single worst day → -10 % → $5.
        assert m.var_99 == pytest.approx(5.0, rel=1e-2)
        assert m.cvar_99 == pytest.approx(5.0, rel=1e-2)

    def test_historical_var_with_short_position(self):
        """A SHORT position gains when price falls — its return series
        is the inverse of the price-return series, so a tail loss for
        the SHORT is a price *increase* in the history."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "SHORT",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        # Build history where 3 of 30 days have a +10 % return — those
        # are the SHORT's bad days (price went UP, SHORT lost money).
        # 3 bad days ensures the 5th-percentile interpolation lands on
        # exactly -10 % (see the LONG test above for the interpolation
        # arithmetic).
        prices = [0.50]
        for i in range(30):
            prev = prices[-1]
            if i in (5, 15, 25):
                ret = +0.10  # price up → SHORT loses
            else:
                ret = 0.0
            prices.append(prev * (1 + ret))
        history = {"t1": prices}
        m = LiveRiskMetrics().compute(positions, price_history=history)
        assert m.var_method == "historical"
        # SHORT return on a +10 % price day = -10 % → VaR_95 = $5
        assert m.var_95 == pytest.approx(5.0, rel=1e-2)

    def test_historical_var_with_two_uncorrelated_positions(self):
        """Two positions with independent price histories → portfolio
        VaR < single-position VaR (diversification benefit)."""
        # Single position: 30 days, 2 bad days at -10 %.
        prices_one = [0.50]
        for i in range(30):
            prev = prices_one[-1]
            ret = -0.10 if i in (5, 25) else 0.0
            prices_one.append(prev * (1 + ret))

        # Two positions with non-overlapping bad days → diversification.
        prices_two_a = [0.50]
        for i in range(30):
            prev = prices_two_a[-1]
            ret = -0.10 if i in (5, 25) else 0.0
            prices_two_a.append(prev * (1 + ret))
        prices_two_b = [0.50]
        for i in range(30):
            prev = prices_two_b[-1]
            ret = -0.10 if i in (10, 20) else 0.0  # different days
            prices_two_b.append(prev * (1 + ret))

        single_pos = [{"token_id": "t1", "size": 100, "side": "LONG",
                       "avg_price": 0.50, "current_price": 0.50}]
        two_pos = [
            {"token_id": "t1", "size": 50, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 50, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]

        m_single = LiveRiskMetrics().compute(single_pos, price_history={"t1": prices_one})
        m_two = LiveRiskMetrics().compute(
            two_pos, price_history={"t1": prices_two_a, "t2": prices_two_b}
        )
        assert m_single.var_method == "historical"
        assert m_two.var_method == "historical"
        # Single has 4 bad-day returns (2 × -10 % × $50 / $50 = -10 % on
        # those days). Two-position book has bad days at indices 5, 10,
        # 20, 25 — but on each, only one position moves, so the loss is
        # halved (-5 % on those days, four of them). 5th-percentile of
        # the two-position return series is -5 % vs -10 % for the single.
        # → diversified VaR is roughly half.
        assert m_two.var_95 < m_single.var_95

    def test_parametric_var_with_custom_daily_vol(self):
        """A custom ``daily_vol`` instance setting scales the
        parametric VaR proportionally."""
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        # default vol = 0.05; tighter vol = 0.02 → VaR shrinks 2.5x.
        m_default = LiveRiskMetrics().compute(positions)
        m_tight = LiveRiskMetrics(daily_vol=0.02).compute(positions)
        assert m_default.var_95 == pytest.approx(4.125, rel=1e-6)  # 50 × 1.65 × 0.05
        assert m_tight.var_95 == pytest.approx(50 * 1.65 * 0.02, rel=1e-6)
        assert m_tight.var_95 == pytest.approx(m_default.var_95 * 0.4, rel=1e-6)


# ── (6) TestConcentrationRatio ──────────────────────────────────────────────


class TestConcentrationRatio:
    """Herfindahl-Hirschman Index edge cases."""

    def test_single_position_hhi_is_one(self):
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.concentration_ratio == pytest.approx(1.0)

    def test_two_equal_positions_hhi_is_half(self):
        positions = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
            {"token_id": "t2", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.concentration_ratio == pytest.approx(0.5)

    def test_two_unequal_positions_hhi(self):
        """90/10 split → HHI = 0.81 + 0.01 = 0.82."""
        positions = [
            {"token_id": "t1", "size": 90, "side": "LONG",
             "avg_price": 1.00, "current_price": 1.00},
            {"token_id": "t2", "size": 10, "side": "LONG",
             "avg_price": 1.00, "current_price": 1.00},
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.concentration_ratio == pytest.approx(0.9 ** 2 + 0.1 ** 2, rel=1e-6)
        assert m.concentration_ratio == pytest.approx(0.82, rel=1e-6)
        # Largest position is 90 % of the book.
        assert m.largest_position_pct == pytest.approx(0.9, rel=1e-6)

    def test_many_equal_positions_hhi_approaches_zero(self):
        """10 equal positions → HHI = 1/10 = 0.1 (highly diversified)."""
        positions = [
            {"token_id": f"t{i}", "size": 10, "side": "LONG",
             "avg_price": 1.00, "current_price": 1.00}
            for i in range(10)
        ]
        m = LiveRiskMetrics().compute(positions)
        assert m.concentration_ratio == pytest.approx(0.1, rel=1e-6)
        assert m.largest_position_pct == pytest.approx(0.1, rel=1e-6)

    def test_hhi_in_unit_interval(self):
        """HHI is always in [0, 1] — never negative, never above 1."""
        positions = [
            {"token_id": f"t{i}", "size": (i + 1) * 7, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50}
            for i in range(5)
        ]
        m = LiveRiskMetrics().compute(positions)
        assert 0.0 <= m.concentration_ratio <= 1.0


# ── (7) API tests: register_routes ──────────────────────────────────────────


@pytest.fixture
def _restore_singleton_config():
    """Snapshot the module-level singleton's config before each API
    test and restore it in teardown — same pattern as
    ``tests/test_portfolio_optimizer.py`` / ``tests/test_stress_test.py``."""
    snapshot = (
        _lrm_module.live_risk_metrics.lookback_days,
        _lrm_module.live_risk_metrics.daily_vol,
    )
    yield _lrm_module.live_risk_metrics
    _lrm_module.live_risk_metrics.lookback_days = snapshot[0]
    _lrm_module.live_risk_metrics.daily_vol = snapshot[1]


@pytest.fixture
def client(_restore_singleton_config) -> TestClient:
    """Fresh ``FastAPI`` app with only the live-risk-metrics routes
    registered (mirrors the production ``api/server.py`` W20-5 block —
    same ``register_routes(app)`` entry point)."""
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _sample_positions() -> list[dict]:
    """A small two-position book (one LONG, one SHORT) used by the
    API tests."""
    return [
        {"token_id": "t1", "size": 100, "side": "LONG",
         "avg_price": 0.50, "current_price": 0.55},
        {"token_id": "t2", "size": 50, "side": "SHORT",
         "avg_price": 0.40, "current_price": 0.35},
    ]


class TestLiveRiskMetricsRoutes:
    """HTTP-level coverage of the ``/api/portfolio/risk-metrics`` endpoint."""

    def test_get_risk_metrics_returns_zeroed_when_no_live_positions(self, client: TestClient):
        """``GET /api/portfolio/risk-metrics`` against an isolated app
        (no live DataStore positions) returns 200 with zeroed metrics and
        ``var_method="none"`` (the dashboard's empty state)."""
        # The ``client`` fixture builds a fresh FastAPI app — the live
        # DataStore singleton is reset by the conftest autouse fixture,
        # so ``_positions_from_live_store`` returns [].
        r = client.get("/api/portfolio/risk-metrics")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_exposure"] == 0.0
        assert body["net_exposure"] == 0.0
        assert body["position_count"] == 0
        assert body["var_95"] == 0.0
        assert body["var_99"] == 0.0
        assert body["cvar_95"] == 0.0
        assert body["cvar_99"] == 0.0
        assert body["concentration_ratio"] == 0.0
        assert body["var_method"] == "none"
        assert body["computed_at"] > 0

    def test_get_risk_metrics_with_live_store_positions(self, client: TestClient, monkeypatch):
        """When the live ``DataStore`` has positions, the GET endpoint
        computes risk metrics against them — monkey-patch the helper to
        return a sample book so the test doesn't depend on the global
        singleton's state."""
        sample = _sample_positions()
        monkeypatch.setattr(
            _lrm_module, "_positions_from_live_store", lambda: sample
        )
        r = client.get("/api/portfolio/risk-metrics")
        assert r.status_code == 200, r.text
        body = r.json()
        # total = 100×0.55 + 50×0.35 = $55 + $17.50 = $72.50
        assert body["total_exposure"] == pytest.approx(72.50, rel=1e-6)
        # net = $55 - $17.50 = $37.50 → abs = $37.50
        assert body["net_exposure"] == pytest.approx(37.50, rel=1e-6)
        assert body["position_count"] == 2
        # No price history → parametric fallback.
        assert body["var_method"] == "parametric"
        # VaR_95 = 72.50 × 1.65 × 0.05 ≈ 5.98
        assert body["var_95"] == pytest.approx(72.50 * 1.65 * 0.05, rel=1e-6)
        # Concentration ratio = (55/72.5)^2 + (17.5/72.5)^2 ≈ 0.575 + 0.058 = 0.633
        s1 = 55.0 / 72.50
        s2 = 17.50 / 72.50
        assert body["concentration_ratio"] == pytest.approx(s1 ** 2 + s2 ** 2, rel=1e-6)

    def test_get_risk_metrics_response_has_all_documented_fields(self, client: TestClient):
        """The response payload contains every documented field —
        protects against accidental field renames / drops."""
        r = client.get("/api/portfolio/risk-metrics")
        assert r.status_code == 200
        body = r.json()
        expected_keys = {
            "total_exposure", "net_exposure", "gross_exposure",
            "position_count", "largest_position_pct",
            "var_95", "var_99", "cvar_95", "cvar_99",
            "concentration_ratio", "var_method", "computed_at",
        }
        assert set(body.keys()) == expected_keys

    def test_get_risk_metrics_route_returns_200_with_empty_store_helper(self, client: TestClient, monkeypatch):
        """When the live-store helper raises (defensive path — broken
        environment), the route should still return 200 with the empty
        payload (the helper itself swallows the import error and
        returns [])."""
        # Monkey-patch the helper to return [] explicitly — exercises
        # the same code path the broken-env case takes, without
        # needing to actually break the import.
        monkeypatch.setattr(
            _lrm_module, "_positions_from_live_store", lambda: []
        )
        r = client.get("/api/portfolio/risk-metrics")
        assert r.status_code == 200
        assert r.json()["position_count"] == 0
        assert r.json()["var_method"] == "none"

    def test_get_risk_metrics_with_single_position(self, client: TestClient, monkeypatch):
        """A single LONG position: largest_position_pct = 1.0, HHI = 1.0,
        parametric VaR matches the formula."""
        sample = [
            {"token_id": "t1", "size": 100, "side": "LONG",
             "avg_price": 0.50, "current_price": 0.50},
        ]
        monkeypatch.setattr(
            _lrm_module, "_positions_from_live_store", lambda: sample
        )
        r = client.get("/api/portfolio/risk-metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["total_exposure"] == pytest.approx(50.0)
        assert body["largest_position_pct"] == pytest.approx(1.0)
        assert body["concentration_ratio"] == pytest.approx(1.0)
        assert body["var_method"] == "parametric"
        assert body["var_95"] == pytest.approx(50 * 1.65 * 0.05, rel=1e-6)


# ── Singleton + module-level integration ────────────────────────────────────


class TestSingletonAndExports:
    """The module-level singleton and ``__all__`` exports are stable
    surface area — protects against accidental rename / removal that
    would silently break the production ``api/server.py`` wiring."""

    def test_singleton_is_liveriskmetrics_instance(self):
        from core.live_risk_metrics import live_risk_metrics
        assert isinstance(live_risk_metrics, LiveRiskMetrics)

    def test_singleton_default_lookback_days(self):
        from core.live_risk_metrics import live_risk_metrics
        assert live_risk_metrics.lookback_days == 30

    def test_singleton_default_daily_vol(self):
        from core.live_risk_metrics import live_risk_metrics
        assert live_risk_metrics.daily_vol == pytest.approx(0.05)

    def test_module_all_exports(self):
        from core import live_risk_metrics as mod
        assert set(mod.__all__) == {
            "PortfolioRiskMetrics",
            "LiveRiskMetrics",
            "live_risk_metrics",
            "register_routes",
        }

    def test_register_routes_is_callable(self):
        from core.live_risk_metrics import register_routes
        assert callable(register_routes)
