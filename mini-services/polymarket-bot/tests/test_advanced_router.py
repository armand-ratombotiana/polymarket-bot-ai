"""
W15-8 — Unit + integration tests for ``execution/advanced_router.py`` and
the ``/api/execution/plan`` FastAPI endpoint.

Coverage matrix
---------------

  Unit tests (``TestTWAP``, ``TestVWAP``, ``TestIceberg``,
  ``TestImmediate``, ``TestRecommendStrategy``, ``TestEstimateSlippage``,
  ``TestSelectVenue``, ``TestAdaptiveSlippage``, ``TestPlanDispatch``):
    * TWAP — equal slice sizes, correct sequential delays, n_slices
      honored, duration clamped to ``max_duration``.
    * VWAP — slice sizes proportional to volume_profile, empty / zero
      profile falls back to TWAP, slice delays spaced across 60 s.
    * Iceberg — visible_size honored, last slice carries the remainder,
      caller-supplied visible_size larger than total collapses to a
      single slice (no negative remainder).
    * Immediate — single slice, zero delay, duration 0.
    * recommend_strategy — small order → immediate; urgent → immediate;
      wide spread + large → iceberg; large normal → twap; default →
      immediate.
    * estimate_slippage — zero-depth returns spread*2; small order
      returns ~spread/2; large order returns spread/2 + linear impact.
    * select_venue — BUY picks lowest price+fee; SELL picks highest
      price-fee; depth tiebreak; empty raises ValueError.
    * adaptive_slippage_tolerance — base at zero vol; linear slope;
      floor and ceiling enforced.
    * plan dispatch — every strategy name routes correctly; "auto"
      dispatches through recommend_strategy; unknown strategy falls
      back to immediate.

  Integration tests (``TestExecutionPlanEndpoint``):
    * POST /api/execution/plan with explicit TWAP → 200, 4 equal slices
      at 15 s spacing, strategy echoed back.
    * POST /api/execution/plan with explicit VWAP → 200, slices
      proportional to the supplied profile.
    * POST /api/execution/plan with explicit iceberg → 200, slices
      capped at visible_size, sum to total_size.
    * POST /api/execution/plan with strategy="auto" + wide spread +
      large size → 200, strategy resolved to "iceberg".
    * POST /api/execution/plan with strategy="auto" + small size → 200,
      strategy resolved to "immediate".
    * POST /api/execution/plan with NO Authorization header → 401
      (auth middleware short-circuits before the route handler runs).
    * POST /api/execution/plan with strategy="immediate" → 200, single
      slice, total_size echoed.

Sync tests
~~~~~~~~~~
All tests are SYNC ``def test_...``. ``TestClient`` bridges each request
into the ASGI app via its own ``anyio`` portal; ``pytest.mark.asyncio``
would compete with that portal. Mirrors the convention in
``tests/test_security.py`` and ``tests/test_integration.py``.

The conftest's autouse ``_reset_store_factory_defaults`` fixture pins
the global ``store`` / ``risk_manager`` / ``paper_sim`` singletons to a
factory baseline before every test; the integration tests don't depend
on store state but rely on the autouse fixture to keep import-time
side effects from leaking between tests.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from execution.advanced_router import (
    ADAPTIVE_SLOPE,
    DEFAULT_BASE_SLIPPAGE_BPS,
    MAX_ADAPTIVE_SLIPPAGE_BPS,
    MIN_ADAPTIVE_SLIPPAGE_BPS,
    AdvancedOrderRouter,
    OrderPlan,
)

# ── Bearer token used by every authenticated request ────────────────────────
# conftest.py sets ``API_TOKEN=test-token-conftest`` via
# ``os.environ.setdefault`` BEFORE any project module is imported, so
# ``settings.api_token`` resolves to this string at runtime. The
# ``enforce_api_auth`` middleware compares the header against this value
# via ``hmac.compare_digest``.
VALID_TOKEN = "test-token-conftest"


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def router() -> AdvancedOrderRouter:
    """Fresh ``AdvancedOrderRouter`` per test (no shared state)."""
    return AdvancedOrderRouter()


@pytest.fixture
def client() -> TestClient:
    """``TestClient`` bound to the production ``api.server.app``.

    ``raise_server_exceptions=False`` mirrors the convention in
    ``tests/test_security.py`` / ``tests/test_integration.py`` so the
    auth-middleware 401 path doesn't re-raise inside the test process.
    """
    from api.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    """The ``Authorization: Bearer <VALID_TOKEN>`` header every
    authenticated request must carry."""
    return {"Authorization": f"Bearer {VALID_TOKEN}"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. TWAP
# ═══════════════════════════════════════════════════════════════════════════


class TestTWAP:
    """Time-Weighted Average Price — equal slices over time."""

    def test_twap_equal_slice_sizes(self, router):
        """``plan_twap(1000, duration=60, n_slices=5)`` must produce 5
        slices each of size 200 (1000 / 5) — the defining property of
        TWAP is uniform slice sizing so the time-weighted average fill
        price converges to the period's average print."""
        plan = router.plan_twap(1000.0, duration=60.0, n_slices=5)

        assert plan.strategy == "twap"
        assert plan.total_size == pytest.approx(1000.0)
        assert len(plan.slices) == 5
        # Every slice is exactly total_size / n_slices = 200.
        for s in plan.slices:
            assert s["size"] == pytest.approx(200.0)
        # Sum of slice sizes equals the parent (no rounding loss).
        total = sum(s["size"] for s in plan.slices)
        assert total == pytest.approx(1000.0)

    def test_twap_delays_are_sequential_and_uniform(self, router):
        """Slice delays must be ``i * (duration / n_slices)`` so they
        are evenly spaced across the requested duration. With duration=60
        and n_slices=4, delays are 0, 15, 30, 45."""
        plan = router.plan_twap(400.0, duration=60.0, n_slices=4)

        delays = [s["delay_seconds"] for s in plan.slices]
        assert delays == pytest.approx([0.0, 15.0, 30.0, 45.0])
        assert plan.duration_seconds == pytest.approx(60.0)

    def test_twap_first_slice_has_zero_delay(self, router):
        """The first slice must execute immediately (delay=0) so the
        parent order starts filling as soon as the plan begins."""
        plan = router.plan_twap(500.0, duration=30.0, n_slices=10)

        assert plan.slices[0]["delay_seconds"] == pytest.approx(0.0)
        assert plan.slices[0]["index"] == 0

    def test_twap_n_slices_honored(self, router):
        """``n_slices=7`` must produce exactly 7 slices regardless of
        total_size or duration."""
        plan = router.plan_twap(700.0, duration=70.0, n_slices=7)

        assert len(plan.slices) == 7
        # Indices are 0-based and sequential.
        assert [s["index"] for s in plan.slices] == list(range(7))

    def test_twap_duration_clamped_to_max(self):
        """A caller requesting a duration longer than the router's
        ``max_duration`` cap must have the duration CLAMPED — a 24-hour
        TWAP would outlive the operator's attention span and is rejected
        at the planner level rather than allowed to run silently."""
        r = AdvancedOrderRouter(max_duration=120.0)
        plan = r.plan_twap(500.0, duration=3600.0, n_slices=5)

        # Duration clamped to 120 s; 5 slices at 24 s spacing.
        assert plan.duration_seconds == pytest.approx(120.0)
        assert plan.slices[-1]["delay_seconds"] == pytest.approx(96.0)

    def test_twap_n_slices_at_least_one(self, router):
        """``n_slices=0`` (or negative) must be coerced to 1 — the
        planner never returns an empty plan, which would force every
        caller to handle a special case."""
        plan = router.plan_twap(100.0, duration=60.0, n_slices=0)

        assert len(plan.slices) == 1
        assert plan.slices[0]["size"] == pytest.approx(100.0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. VWAP
# ═══════════════════════════════════════════════════════════════════════════


class TestVWAP:
    """Volume-Weighted Average Price — slices proportional to volume."""

    def test_vwap_proportional_slices(self, router):
        """``plan_vwap(1000, [10, 30, 60])`` must produce slices of sizes
        100, 300, 600 — proportional to the supplied volume profile
        (total volume 100, fractions 0.1, 0.3, 0.6)."""
        plan = router.plan_vwap(1000.0, volume_profile=[10.0, 30.0, 60.0])

        assert plan.strategy == "vwap"
        assert plan.total_size == pytest.approx(1000.0)
        assert len(plan.slices) == 3
        sizes = [s["size"] for s in plan.slices]
        assert sizes == pytest.approx([100.0, 300.0, 600.0])
        # Total preserved.
        assert sum(sizes) == pytest.approx(1000.0)

    def test_vwap_uniform_profile_yields_uniform_slices(self, router):
        """A uniform volume profile [1,1,1,1] must produce four equal
        slices — VWAP degenerates to TWAP under a uniform profile."""
        plan = router.plan_vwap(800.0, volume_profile=[1.0, 1.0, 1.0, 1.0])

        sizes = [s["size"] for s in plan.slices]
        assert sizes == pytest.approx([200.0, 200.0, 200.0, 200.0])

    def test_vwap_empty_profile_falls_back_to_twap(self, router):
        """An empty volume profile must fall back to a 5-slice TWAP over
        60 s — the planner never returns an empty plan."""
        plan = router.plan_vwap(500.0, volume_profile=[])

        assert plan.strategy == "twap"
        assert len(plan.slices) == 5
        # TWAP fallback uses 60 s duration.
        assert plan.duration_seconds == pytest.approx(60.0)

    def test_vwap_zero_volume_profile_falls_back_to_twap(self, router):
        """A profile of all-zeros must fall back to a TWAP over the same
        number of slices as the profile length — total_volume == 0 is a
        degenerate market (no observable volume) and the planner refuses
        to divide by zero."""
        plan = router.plan_vwap(300.0, volume_profile=[0.0, 0.0, 0.0])

        assert plan.strategy == "twap"
        assert len(plan.slices) == 3
        # Uniform slices: 100 each.
        for s in plan.slices:
            assert s["size"] == pytest.approx(100.0)

    def test_vwap_delays_spread_across_60s(self, router):
        """Slice delays must be spaced uniformly across a 60-second
        window — bin_interval = 60 / n_bins. With 4 bins: delays 0, 15,
        30, 45."""
        plan = router.plan_vwap(400.0, volume_profile=[1.0, 1.0, 1.0, 1.0])

        delays = [s["delay_seconds"] for s in plan.slices]
        assert delays == pytest.approx([0.0, 15.0, 30.0, 45.0])

    def test_vwap_indices_sequential(self, router):
        """Slice indices must be 0-based sequential so the caller can
        track slice ordinals across the plan."""
        plan = router.plan_vwap(500.0, volume_profile=[5.0, 3.0, 2.0])

        assert [s["index"] for s in plan.slices] == [0, 1, 2]


# ═══════════════════════════════════════════════════════════════════════════
# 3. Iceberg
# ═══════════════════════════════════════════════════════════════════════════


class TestIceberg:
    """Iceberg — fixed visible quantum released on a short cadence."""

    def test_iceberg_default_visible_size(self, router):
        """Without an explicit ``visible_size``, the iceberg must default
        to ``min(max_slice_size, total_size / 5)``. With max_slice_size
        =50 and total_size=1000 → visible = min(50, 200) = 50, so the
        plan has 20 slices of 50 each."""
        plan = router.plan_iceberg(1000.0)

        assert plan.strategy == "iceberg"
        # 1000 / 50 = 20 slices.
        assert len(plan.slices) == 20
        for s in plan.slices:
            assert s["size"] == pytest.approx(50.0)
        # Sum preserved.
        assert sum(s["size"] for s in plan.slices) == pytest.approx(1000.0)

    def test_iceberg_explicit_visible_size(self, router):
        """An explicit ``visible_size=100`` on a 500-size parent must
        produce 5 slices of exactly 100 each."""
        plan = router.plan_iceberg(500.0, visible_size=100.0)

        assert len(plan.slices) == 5
        for s in plan.slices:
            assert s["size"] == pytest.approx(100.0)

    def test_iceberg_last_slice_carries_remainder(self, router):
        """When the parent is not evenly divisible by visible_size, the
        last slice must carry the remainder — never a full visible_size
        that would overshoot the parent."""
        # 500 / 100 = 5 exactly even; use 550/100 → 5 full + 1 of 50.
        plan = router.plan_iceberg(550.0, visible_size=100.0)

        assert len(plan.slices) == 6
        # First 5 slices are full visible (100), last slice is 50.
        for s in plan.slices[:-1]:
            assert s["size"] == pytest.approx(100.0)
        assert plan.slices[-1]["size"] == pytest.approx(50.0)
        # Total preserved.
        assert sum(s["size"] for s in plan.slices) == pytest.approx(550.0)

    def test_iceberg_delays_two_seconds_apart(self, router):
        """Iceberg slices must be released 2 s apart so the visible
        quantum is exposed on the book at a human-paced cadence."""
        plan = router.plan_iceberg(200.0, visible_size=50.0)

        # 4 slices, delays 0, 2, 4, 6.
        delays = [s["delay_seconds"] for s in plan.slices]
        assert delays == pytest.approx([0.0, 2.0, 4.0, 6.0])
        assert plan.duration_seconds == pytest.approx(8.0)

    def test_iceberg_visible_size_larger_than_total(self, router):
        """A caller-supplied ``visible_size`` larger than the parent
        itself must collapse to a single slice equal to the parent —
        there's nothing to conceal, so the plan is just an immediate
        fill rebranded as a 1-slice iceberg."""
        plan = router.plan_iceberg(100.0, visible_size=500.0)

        assert len(plan.slices) == 1
        assert plan.slices[0]["size"] == pytest.approx(100.0)

    def test_iceberg_visible_size_zero_uses_default(self, router):
        """``visible_size=0`` (or negative) must fall back to the default
        rather than divide by zero."""
        plan = router.plan_iceberg(1000.0, visible_size=0.0)

        # Default visible = min(50, 200) = 50, so 20 slices.
        assert len(plan.slices) == 20
        assert sum(s["size"] for s in plan.slices) == pytest.approx(1000.0)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Immediate
# ═══════════════════════════════════════════════════════════════════════════


class TestImmediate:
    """Immediate — single slice, no slicing."""

    def test_immediate_single_slice(self, router):
        """``plan_immediate`` must return a single-slice plan with zero
        delay — the parent order is routed in one shot."""
        plan = router.plan_immediate(75.0)

        assert plan.strategy == "immediate"
        assert len(plan.slices) == 1
        assert plan.slices[0]["size"] == pytest.approx(75.0)
        assert plan.slices[0]["delay_seconds"] == 0.0
        assert plan.slices[0]["index"] == 0
        assert plan.duration_seconds == 0.0
        assert plan.total_size == pytest.approx(75.0)


# ═══════════════════════════════════════════════════════════════════════════
# 5. recommend_strategy
# ═══════════════════════════════════════════════════════════════════════════


class TestRecommendStrategy:
    """Strategy recommendation decision tree."""

    def test_small_order_recommends_immediate(self, router):
        """``total_size < 0.1 % ADV`` → immediate. A 5-USDC order on a
        market with 10k ADV is 0.05 % — tiny, slicing adds latency
        without reducing impact."""
        rec = router.recommend_strategy(
            total_size=5.0,
            avg_daily_volume=10_000.0,
            spread_bps=20.0,
            urgency="normal",
        )
        assert rec == "immediate"

    def test_urgent_recommends_immediate_regardless(self, router):
        """``urgency="urgent"`` must override every other consideration
        — a trader who flagged urgency is willing to pay the slippage to
        get the fill now, even on a large order."""
        rec = router.recommend_strategy(
            total_size=5000.0,  # 50 % ADV — very large
            avg_daily_volume=10_000.0,
            spread_bps=80.0,  # wide spread
            urgency="urgent",
        )
        assert rec == "immediate"

    def test_wide_spread_large_size_recommends_iceberg(self, router):
        """``spread_bps > 50 AND total_size > 1 % ADV`` → iceberg. A
        5000-USDC order on a 10k-ADV market with an 80-BPS spread is a
        costly crossing — concealment lets each slice rest as a passive
        limit rather than pay the spread repeatedly."""
        rec = router.recommend_strategy(
            total_size=5000.0,  # 50 % ADV
            avg_daily_volume=10_000.0,
            spread_bps=80.0,
            urgency="normal",
        )
        assert rec == "iceberg"

    def test_large_size_normal_spread_recommends_twap(self, router):
        """``total_size > 0.5 % ADV`` with a normal spread → twap. A
        100-USDC order on a 10k-ADV market with a 20-BPS spread is 1 %
        ADV — large enough to benefit from time-slicing but spread is
        normal so simple uniform slices are the right default."""
        rec = router.recommend_strategy(
            total_size=100.0,
            avg_daily_volume=10_000.0,
            spread_bps=20.0,
            urgency="normal",
        )
        assert rec == "twap"

    def test_default_recommends_immediate(self, router):
        """The default (none of the above conditions fire) → immediate.
        E.g. a 30-USDC order on a 10k-ADV market with a 20-BPS spread is
        0.3 % ADV — too small to warrant slicing but above the 0.1 %
        threshold, so it falls through to the default branch."""
        rec = router.recommend_strategy(
            total_size=30.0,
            avg_daily_volume=10_000.0,
            spread_bps=20.0,
            urgency="normal",
        )
        assert rec == "immediate"

    def test_wide_spread_small_size_does_not_trigger_iceberg(self, router):
        """A wide spread alone is not enough — the iceberg branch
        requires ``total_size > 1 % ADV`` too. A 50-USDC order on a 10k
        ADV market (0.5 % ADV) with an 80-BPS spread should NOT pick
        iceberg; it should fall through to the immediate default
        (smaller than the 0.5 %-ADV TWAP threshold AND below the 1 %
        iceberg threshold)."""
        rec = router.recommend_strategy(
            total_size=50.0,
            avg_daily_volume=10_000.0,
            spread_bps=80.0,
            urgency="normal",
        )
        assert rec == "immediate"


# ═══════════════════════════════════════════════════════════════════════════
# 6. estimate_slippage
# ═══════════════════════════════════════════════════════════════════════════


class TestEstimateSlippage:
    """Linear-impact slippage estimator."""

    def test_zero_book_depth_returns_worst_case(self, router):
        """``book_depth <= 0`` must return ``spread_bps * 2`` — a
        degenerate book with zero observable depth. The planner assumes
        the worst case rather than silently returning zero."""
        result = router.estimate_slippage(
            order_size=100.0, book_depth=0.0, spread_bps=20.0
        )
        assert result == pytest.approx(40.0)  # 20 * 2

    def test_negative_book_depth_returns_worst_case(self, router):
        """A negative ``book_depth`` (defensive — should not happen but
        the planner guards against it) is treated the same as zero."""
        result = router.estimate_slippage(
            order_size=100.0, book_depth=-50.0, spread_bps=10.0
        )
        assert result == pytest.approx(20.0)  # 10 * 2

    def test_small_order_returns_half_spread(self, router):
        """A small order relative to book depth pays only half the
        spread (the cost of crossing the spread on a marketable order)
        — the impact term ``order_size / book_depth * 100`` is
        negligible when ``order_size << book_depth``."""
        # order_size=1, book_depth=10000 → impact = 0.01 BPS.
        result = router.estimate_slippage(
            order_size=1.0, book_depth=10_000.0, spread_bps=20.0
        )
        # 20 / 2 + (1 / 10000) * 100 = 10 + 0.01 = 10.01.
        assert result == pytest.approx(10.01, abs=0.01)

    def test_large_order_adds_linear_impact(self, router):
        """A deep-eating order adds a linear impact term. An order equal
        to the full book depth pays 100 BPS of impact on top of half the
        spread."""
        # order_size=1000, book_depth=1000 → impact = 100 BPS.
        result = router.estimate_slippage(
            order_size=1000.0, book_depth=1000.0, spread_bps=20.0
        )
        # 20 / 2 + (1000 / 1000) * 100 = 10 + 100 = 110.
        assert result == pytest.approx(110.0)

    def test_impact_scales_linearly_with_order_size(self, router):
        """Doubling the order size (with constant book depth and spread)
        must add exactly the impact term's slope (100 BPS per
        full-depth)."""
        s1 = router.estimate_slippage(
            order_size=100.0, book_depth=1000.0, spread_bps=20.0
        )
        s2 = router.estimate_slippage(
            order_size=200.0, book_depth=1000.0, spread_bps=20.0
        )
        # Both share the 10-BPS spread/2 base; impact difference is
        # (200 - 100) / 1000 * 100 = 10 BPS.
        assert s2 - s1 == pytest.approx(10.0)


# ═══════════════════════════════════════════════════════════════════════════
# 7. select_venue — multi-venue routing
# ═══════════════════════════════════════════════════════════════════════════


class TestSelectVenue:
    """Multi-venue routing — pick the venue with the best executable
    price (price adjusted for fee)."""

    def test_buy_picks_lowest_price(self, router):
        """For a BUY, the router must pick the venue with the LOWEST
        all-in price (price + fee)."""
        venues = [
            {"venue": "clob", "price": 0.55, "depth": 1000, "fee_bps": 0},
            {"venue": "amm", "price": 0.50, "depth": 500, "fee_bps": 0},
        ]
        best = router.select_venue(venues, side="BUY")
        assert best["venue"] == "amm"
        assert best["price"] == pytest.approx(0.50)

    def test_sell_picks_highest_price(self, router):
        """For a SELL, the router must pick the venue with the HIGHEST
        net price (price - fee)."""
        venues = [
            {"venue": "clob", "price": 0.55, "depth": 1000, "fee_bps": 0},
            {"venue": "amm", "price": 0.50, "depth": 500, "fee_bps": 0},
        ]
        best = router.select_venue(venues, side="SELL")
        assert best["venue"] == "clob"
        assert best["price"] == pytest.approx(0.55)

    def test_buy_chooses_lower_price_after_fee(self, router):
        """The router must factor in fees: a venue with a higher quoted
        price but ZERO fee can beat a venue with a lower quoted price
        but a steep fee. Here CLOB quotes 0.55 + 0 BPS fee = 0.55 all-in;
        AMM quotes 0.54 + 200 BPS = 0.54 + 0.0108 = 0.5508. CLOB wins
        (0.55 < 0.5508)."""
        venues = [
            {"venue": "clob", "price": 0.55, "depth": 1000, "fee_bps": 0},
            {"venue": "amm", "price": 0.54, "depth": 500, "fee_bps": 200},
        ]
        best = router.select_venue(venues, side="BUY")
        # CLOB all-in = 0.55; AMM all-in = 0.54 * (1 + 200/10000) = 0.5508.
        # CLOB wins.
        assert best["venue"] == "clob"

    def test_sell_chooses_higher_price_after_fee(self, router):
        """For a SELL, the router picks the HIGHEST net price
        (price − fee). CLOB @ 0.60 − 0 fee = 0.60; AMM @ 0.61 − 100 BPS
        fee = 0.61 * (1 - 0.01) = 0.6039. AMM wins."""
        venues = [
            {"venue": "clob", "price": 0.60, "depth": 1000, "fee_bps": 0},
            {"venue": "amm", "price": 0.61, "depth": 500, "fee_bps": 100},
        ]
        best = router.select_venue(venues, side="SELL")
        assert best["venue"] == "amm"

    def test_tie_prefers_deeper_venue(self, router):
        """When two venues quote the SAME all-in price, the router must
        prefer the deeper venue (higher probability of filling at the
        quoted price)."""
        # Both venues at 0.50 with no fee → all-in 0.50 (tie). CLOB has
        # deeper liquidity.
        venues = [
            {"venue": "amm", "price": 0.50, "depth": 100, "fee_bps": 0},
            {"venue": "clob", "price": 0.50, "depth": 5000, "fee_bps": 0},
        ]
        best = router.select_venue(venues, side="BUY")
        assert best["venue"] == "clob"
        assert best["depth"] == 5000

    def test_empty_venues_raises_value_error(self, router):
        """An empty ``venues`` list must raise ``ValueError`` — the
        caller has no venue to route to, which is a programming error
        rather than a recoverable state."""
        with pytest.raises(ValueError, match="at least one venue"):
            router.select_venue([], side="BUY")

    def test_single_venue_returned_unchanged(self, router):
        """A single-venue list must return that venue verbatim — the
        router doesn't synthesize a new dict, the caller gets the same
        reference back so non-price fields are accessible."""
        single = [{"venue": "clob", "price": 0.50, "depth": 100, "fee_bps": 0}]
        best = router.select_venue(single, side="BUY")
        assert best is single[0]

    def test_buy_case_insensitive_side(self, router):
        """``side`` is case-insensitive — ``"buy"`` and ``"BUY"`` must
        behave identically."""
        venues = [
            {"venue": "clob", "price": 0.55, "depth": 1000, "fee_bps": 0},
            {"venue": "amm", "price": 0.50, "depth": 500, "fee_bps": 0},
        ]
        best_lower = router.select_venue(venues, side="buy")
        best_upper = router.select_venue(venues, side="BUY")
        assert best_lower["venue"] == best_upper["venue"] == "amm"


# ═══════════════════════════════════════════════════════════════════════════
# 8. adaptive_slippage_tolerance
# ═══════════════════════════════════════════════════════════════════════════


class TestAdaptiveSlippage:
    """Adaptive slippage tolerance — scales with realized volatility."""

    def test_zero_vol_returns_base(self, router):
        """At zero volatility, the tolerance must equal the base (15 BPS
        by default) — a calm market keeps a tight tolerance."""
        result = router.adaptive_slippage_tolerance(0.0)
        assert result == pytest.approx(DEFAULT_BASE_SLIPPAGE_BPS)

    def test_linear_slope(self, router):
        """At volatility ``v`` BPS, the tolerance must be
        ``base + ADAPTIVE_SLOPE * v`` (before clamping). At v=100:
        15 + 0.1 * 100 = 25 BPS."""
        result = router.adaptive_slippage_tolerance(100.0)
        assert result == pytest.approx(
            DEFAULT_BASE_SLIPPAGE_BPS + ADAPTIVE_SLOPE * 100.0
        )

    def test_floor_enforced(self, router):
        """A negative ``base_bps`` cannot collapse the tolerance below
        ``MIN_ADAPTIVE_SLIPPAGE_BPS`` (7.5 BPS) — the floor is the
        minimum adverse-selection buffer regardless of how calm the
        market is."""
        result = router.adaptive_slippage_tolerance(
            0.0, base_bps=1.0  # base 1 BPS would underflow without floor
        )
        assert result == pytest.approx(MIN_ADAPTIVE_SLIPPAGE_BPS)

    def test_ceiling_enforced(self, router):
        """An extreme vol spike cannot open the gate beyond
        ``MAX_ADAPTIVE_SLIPPAGE_BPS`` (60 BPS) — the ceiling is the
        "stop and re-evaluate" threshold."""
        # 1000 BPS vol → 15 + 0.1*1000 = 115, clamped to 60.
        result = router.adaptive_slippage_tolerance(1000.0)
        assert result == pytest.approx(MAX_ADAPTIVE_SLIPPAGE_BPS)

    def test_negative_volatility_treated_as_zero(self, router):
        """A negative ``volatility_bps`` (defensive — should not happen)
        must be treated as zero so the tolerance does not collapse
        below the base."""
        result = router.adaptive_slippage_tolerance(-50.0)
        assert result == pytest.approx(DEFAULT_BASE_SLIPPAGE_BPS)

    def test_custom_base_bps(self, router):
        """A caller can override the base (e.g. to 8 BPS for a drift-
        tightened regime) — the slope and clamps still apply."""
        # base=8, vol=100 → 8 + 0.1*100 = 18, within [7.5, 60].
        result = router.adaptive_slippage_tolerance(100.0, base_bps=8.0)
        assert result == pytest.approx(18.0)

    def test_tolerance_monotonic_in_volatility(self, router):
        """The tolerance must be monotonically non-decreasing in
        volatility (within the unclamped range) — a higher vol regime
        must never produce a tighter tolerance than a lower one."""
        vols = [0.0, 10.0, 50.0, 100.0, 200.0, 300.0]
        tols = [router.adaptive_slippage_tolerance(v) for v in vols]
        # Strictly increasing up to the ceiling.
        for a, b in zip(tols, tols[1:]):
            assert b >= a


# ═══════════════════════════════════════════════════════════════════════════
# 9. plan dispatch (strategy-name → planner)
# ═══════════════════════════════════════════════════════════════════════════


class TestPlanDispatch:
    """``plan(strategy, total_size, ...)`` dispatches to the right
    planner based on the strategy name."""

    def test_plan_twap_dispatch(self, router):
        """``plan("twap", ...)`` delegates to ``plan_twap``."""
        plan = router.plan("twap", 1000.0, duration=60.0, n_slices=5)
        assert plan.strategy == "twap"
        assert len(plan.slices) == 5

    def test_plan_vwap_dispatch(self, router):
        """``plan("vwap", ...)`` delegates to ``plan_vwap``. When no
        ``volume_profile`` is supplied, the dispatcher synthesizes a
        uniform profile of ``n_slices`` ones so VWAP degenerates to
        TWAP."""
        plan = router.plan("vwap", 500.0, n_slices=5)
        assert plan.strategy == "vwap"
        assert len(plan.slices) == 5

    def test_plan_iceberg_dispatch(self, router):
        """``plan("iceberg", ...)`` delegates to ``plan_iceberg``."""
        plan = router.plan("iceberg", 1000.0, visible_size=100.0)
        assert plan.strategy == "iceberg"
        assert len(plan.slices) == 10

    def test_plan_immediate_dispatch(self, router):
        """``plan("immediate", ...)`` delegates to ``plan_immediate``."""
        plan = router.plan("immediate", 75.0)
        assert plan.strategy == "immediate"
        assert len(plan.slices) == 1

    def test_plan_unknown_strategy_falls_back_to_immediate(self, router):
        """An unknown strategy name must fall back to ``plan_immediate``
        rather than raising — the planner is fail-safe and produces a
        usable plan even on a typo."""
        plan = router.plan("nonexistent", 75.0)
        assert plan.strategy == "immediate"
        assert len(plan.slices) == 1

    def test_plan_none_strategy_defaults_to_immediate(self, router):
        """A ``None`` strategy must default to immediate — the planner
        never raises on a missing strategy."""
        plan = router.plan(None, 75.0)  # type: ignore[arg-type]
        assert plan.strategy == "immediate"

    def test_plan_auto_dispatches_through_recommend_strategy(self, router):
        """``plan("auto", ...)`` runs ``recommend_strategy`` with the
        caller-supplied context — when total_size is small relative to
        ADV (the conservative auto-defaults), it resolves to
        ``immediate``."""
        # Default auto ADV is 10000; total_size 5 is 0.05 % → immediate.
        plan = router.plan("auto", 5.0)
        assert plan.strategy == "immediate"


# ═══════════════════════════════════════════════════════════════════════════
# 10. OrderPlan dataclass shape
# ═══════════════════════════════════════════════════════════════════════════


class TestOrderPlanShape:
    """``OrderPlan`` is a ``@dataclass`` — its default-constructed shape
    must match the documented contract so callers that inspect a plan
    before filling its estimated_* fields don't see missing keys."""

    def test_orderplan_default_construction(self):
        """A default-constructed ``OrderPlan`` (with only ``strategy``
        supplied — that field has no default per the W15-8 spec) must
        have the documented zero / empty defaults so callers can
        construct one incrementally without a KeyError."""
        plan = OrderPlan(strategy="")
        assert plan.strategy == ""  # No default; caller must set.
        assert plan.slices == []
        assert plan.total_size == 0.0
        assert plan.estimated_cost == 0.0
        assert plan.estimated_slippage_bps == 0.0
        assert plan.duration_seconds == 0.0

    def test_orderplan_strategy_is_required_positional(self):
        """``OrderPlan.__init__`` requires ``strategy`` as a positional
        argument (no default) — the W15-8 spec declares it as a
        required field with no default. A missing strategy must raise
        ``TypeError`` so a caller cannot accidentally construct a plan
        with no execution strategy."""
        with pytest.raises(TypeError, match="strategy"):
            OrderPlan()  # type: ignore[call-arg]

    def test_orderplan_slice_shape(self, router):
        """Every slice dict produced by any planner must carry the four
        documented keys: ``index``, ``size``, ``price_target``,
        ``delay_seconds`` — a missing key would crash the dashboard's
        slice-table renderer."""
        for plan_fn, kwargs in [
            (router.plan_twap, {"total_size": 100.0, "duration": 60.0, "n_slices": 3}),
            (router.plan_vwap, {"total_size": 100.0, "volume_profile": [1.0, 2.0, 3.0]}),
            (router.plan_iceberg, {"total_size": 100.0, "visible_size": 25.0}),
            (router.plan_immediate, {"total_size": 100.0}),
        ]:
            plan = plan_fn(**kwargs)
            for s in plan.slices:
                assert set(s.keys()) >= {"index", "size", "price_target", "delay_seconds"}
                assert s["price_target"] is None  # Planner leaves market fill to caller.


# ═══════════════════════════════════════════════════════════════════════════
# 11. Integration: POST /api/execution/plan
# ═══════════════════════════════════════════════════════════════════════════


class TestExecutionPlanEndpoint:
    """Integration tests for the ``/api/execution/plan`` FastAPI endpoint.

    Drives the production ``api.server.app`` via ``TestClient`` so the
    full middleware chain (CORS, auth, security headers, request
    logging) is exercised on every request.
    """

    def test_twap_endpoint_returns_200_with_equal_slices(
        self, client, auth_headers
    ):
        """POST /api/execution/plan with strategy=twap, total_size=1000,
        duration=60, n_slices=4 → 200 with 4 equal slices of 250 each at
        delays 0, 15, 30, 45."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 1000.0,
                "strategy": "twap",
                "duration": 60.0,
                "n_slices": 4,
            },
            headers=auth_headers,
        )

        assert response.status_code == 200, (
            f"expected 200, got {response.status_code}; body={response.text!r}"
        )
        body = response.json()
        assert body["strategy"] == "twap"
        assert body["total_size"] == pytest.approx(1000.0)
        assert body["duration_seconds"] == pytest.approx(60.0)
        assert len(body["slices"]) == 4
        for s in body["slices"]:
            assert s["size"] == pytest.approx(250.0)
        delays = [s["delay_seconds"] for s in body["slices"]]
        assert delays == pytest.approx([0.0, 15.0, 30.0, 45.0])

    def test_vwap_endpoint_returns_proportional_slices(
        self, client, auth_headers
    ):
        """POST /api/execution/plan with strategy=vwap + a 10/30/60
        volume profile → slices proportional to 0.1/0.3/0.6 of the
        parent size."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 1000.0,
                "strategy": "vwap",
                "volume_profile": [10.0, 30.0, 60.0],
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["strategy"] == "vwap"
        sizes = [s["size"] for s in body["slices"]]
        assert sizes == pytest.approx([100.0, 300.0, 600.0])

    def test_iceberg_endpoint_caps_slice_size(self, client, auth_headers):
        """POST /api/execution/plan with strategy=iceberg +
        visible_size=100 on a 500 parent → 5 slices of exactly 100."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 500.0,
                "strategy": "iceberg",
                "visible_size": 100.0,
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["strategy"] == "iceberg"
        assert len(body["slices"]) == 5
        for s in body["slices"]:
            assert s["size"] == pytest.approx(100.0)

    def test_auto_resolves_to_iceberg_for_wide_spread_large_size(
        self, client, auth_headers
    ):
        """POST /api/execution/plan with strategy=auto, total_size=5000,
        ADV=10000, spread=80 → recommend_strategy resolves to "iceberg"
        (spread > 50 AND size > 1 % ADV)."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 5000.0,
                "strategy": "auto",
                "avg_daily_volume": 10_000.0,
                "spread_bps": 80.0,
                "urgency": "normal",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        # Resolved strategy is echoed back.
        assert body["strategy"] == "iceberg"
        # Slices sum to the parent.
        assert sum(s["size"] for s in body["slices"]) == pytest.approx(5000.0)

    def test_auto_resolves_to_immediate_for_small_size(
        self, client, auth_headers
    ):
        """POST /api/execution/plan with strategy=auto + total_size=5 +
        ADV=10000 → recommend_strategy resolves to "immediate"
        (size < 0.1 % ADV)."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 5.0,
                "strategy": "auto",
                "avg_daily_volume": 10_000.0,
                "spread_bps": 20.0,
                "urgency": "normal",
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["strategy"] == "immediate"
        assert len(body["slices"]) == 1
        assert body["slices"][0]["size"] == pytest.approx(5.0)

    def test_immediate_endpoint_returns_single_slice(
        self, client, auth_headers
    ):
        """POST /api/execution/plan with strategy=immediate → a single
        slice equal to the parent, zero delay."""
        response = client.post(
            "/api/execution/plan",
            json={"total_size": 75.0, "strategy": "immediate"},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        assert body["strategy"] == "immediate"
        assert len(body["slices"]) == 1
        assert body["slices"][0]["size"] == pytest.approx(75.0)
        assert body["slices"][0]["delay_seconds"] == 0.0
        assert body["duration_seconds"] == 0.0

    def test_unauthenticated_request_returns_401(self, client):
        """POST /api/execution/plan WITHOUT an Authorization header →
        401 — the auth middleware short-circuits before the route
        handler runs."""
        response = client.post(
            "/api/execution/plan",
            json={"total_size": 100.0, "strategy": "twap"},
        )

        assert response.status_code == 401, (
            f"missing Authorization header must return 401; got "
            f"{response.status_code}. Body: {response.text!r}"
        )

    def test_invalid_token_returns_401(self, client):
        """POST /api/execution/plan with an INVALID bearer token → 401
        (hmac.compare_digest rejects the mismatch)."""
        response = client.post(
            "/api/execution/plan",
            json={"total_size": 100.0, "strategy": "twap"},
            headers={"Authorization": "Bearer definitely-not-the-right-token"},
        )

        assert response.status_code == 401

    def test_default_strategy_is_auto(self, client, auth_headers):
        """POST /api/execution/plan WITHOUT a ``strategy`` field → the
        endpoint defaults to "auto" (resolves via recommend_strategy)."""
        response = client.post(
            "/api/execution/plan",
            json={
                "total_size": 5.0,  # Tiny → auto resolves to immediate.
                "avg_daily_volume": 10_000.0,
                "spread_bps": 20.0,
            },
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        # Auto resolved to immediate because total_size << 0.1 % ADV.
        assert body["strategy"] == "immediate"

    def test_slice_dicts_carry_documented_keys(self, client, auth_headers):
        """Every slice in the response must carry the four documented
        keys (``index``, ``size``, ``price_target``, ``delay_seconds``)
        so the dashboard's slice-table renderer doesn't KeyError."""
        response = client.post(
            "/api/execution/plan",
            json={"total_size": 1000.0, "strategy": "twap", "n_slices": 3},
            headers=auth_headers,
        )

        assert response.status_code == 200
        body = response.json()
        for s in body["slices"]:
            assert "index" in s
            assert "size" in s
            assert "price_target" in s
            assert "delay_seconds" in s
