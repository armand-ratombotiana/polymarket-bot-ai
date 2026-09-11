"""
W9-5 — Unit tests for ``execution/smart_router.py``.

Covers the Smart Order Router's slippage calculator, slicer selection,
and slippage-tolerance gate:

  1. ``calculate_slippage`` on a single-level book returns effective_price
     equal to the best level's price and slippage_bps == 0 (no depth
     traversal beyond best).
  2. ``calculate_slippage`` over a multi-level book walks the depth until
     the requested capital is consumed — effective_price is the size-weighted
     average cost across the consumed levels.
  3. ``calculate_slippage`` for an empty book falls back to ``mid or 0.50``
     and a default 10 BPS slippage.
  4. ``calculate_slippage`` returns ``(mid, 0.0)`` when ``size_usdc == 0``
     (zero capital consumes zero shares — the total_shares==0 guard).
  5. ``plan_execution`` selects ``strategy="direct"`` (single slice) when
     ``total_size_usdc <= 50``.
  6. ``plan_execution`` selects ``strategy="twap"`` when
     ``50 < total_size_usdc <= 250`` — three slices, total sizing sums to
     within ±15% of the requested capital.
  7. ``plan_execution`` selects ``strategy="vwap"`` when
     ``total_size_usdc > 250`` — slices are weighted by book depth.
  8. ``plan_execution`` with ``force_iceberg=True`` overrides the size-based
     decision and selects ``strategy="iceberg"``.
  9. ``plan_execution`` REJECTS the plan (``approved=False``,
     ``rejection_reason`` set) when the computed slippage exceeds the
     healthy tolerance (15 BPS by default).
 10. ``generate_twap_schedule`` legacy alias returns a single direct slice
     when ``total_size_usdc <= 50``, otherwise delegates to ``_twap_slices``.
 11. Slippage tolerance tightens (8 BPS) when the drift_detector reports
     ``SIGNIFICANT_DRIFT`` or ``MODERATE_SHIFT`` (monkeypatched — no live
     drift detector dependency in the test).
 12. ``calculate_slippage`` for Side.SELL walks the BID ladder (not the ask
     ladder) — confirming the side-aware depth selection.

Isolation
----------
All tests construct a fresh ``SmartOrderRouter()`` instance — no module-level
singleton is touched. The drift-detector dependency is monkeypatched in the
test that exercises the drift-tightened tolerance path so the suite has no
dependency on a live drift detector's state.

All async tests are collected via the module-level ``pytestmark =
pytest.mark.asyncio`` declaration (mirrors every sibling test module — even
though these tests are sync, the mark is harmless and keeps collection
consistent).
"""
from __future__ import annotations

import pytest

from core.data_store import OrderBook, PriceLevel, Side
from execution.smart_router import (
    SLIPPAGE_TOLERANCE_DRIFT_BPS,
    SLIPPAGE_TOLERANCE_HEALTHY_BPS,
    ExecutionPlan,
    ExecutionSlice,
    SmartOrderRouter,
)

pytestmark = pytest.mark.asyncio


def _book(
    token_id: str = "TOK_X",
    bids: list[tuple[float, float]] | None = None,
    asks: list[tuple[float, float]] | None = None,
) -> OrderBook:
    """Construct an ``OrderBook`` from a list of (price, size) tuples."""
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=p, size=s) for p, s in (bids or [])],
        asks=[PriceLevel(price=p, size=s) for p, s in (asks or [])],
    )


# ── 1. calculate_slippage single-level: effective=best, 0 BPS ─────────────────
def test_calculate_slippage_single_level_returns_zero_bps():
    """A single-level book means the entire order is consumed at one price
    — effective_price == best level's price and slippage_bps == 0
    (no adverse price movement)."""
    router = SmartOrderRouter()
    # BUY side walks the ASK ladder. One level at 0.60, size 1000 — plenty
    # of depth for a $100 order.
    book = _book(asks=[(0.60, 1000.0)], bids=[(0.59, 1000.0)])

    eff_price, slippage_bps = router.calculate_slippage(book, Side.BUY, 100.0)
    assert eff_price == pytest.approx(0.60, abs=1e-4)
    assert slippage_bps == pytest.approx(0.0, abs=1e-4)


# ── 2. calculate_slippage multi-level: size-weighted average cost ─────────────
def test_calculate_slippage_multi_level_walks_depth():
    """When the order size exceeds the top level's depth, the router must
    walk the ladder until the capital is consumed. effective_price is the
    size-weighted average cost across the consumed levels; slippage_bps is
    the adverse move vs the top level, in BPS."""
    router = SmartOrderRouter()
    # BUY walks ASK ladder. Top level: 0.60 × 100 shares ($60 depth).
    # Second level: 0.65 × 1000 ($650 depth).
    # Order size: $100 → consumes all of top level ($60) + $40 of second.
    book = _book(
        asks=[(0.60, 100.0), (0.65, 1000.0)],
        bids=[(0.59, 100.0)],
    )

    # $60 / 0.60 = 100 shares at top level (cost $60)
    # $40 / 0.65 ≈ 61.538 shares at second level (cost $40)
    # Total: 161.538 shares, $100 total cost
    # Effective (raw) = 100 / 161.538 ≈ 0.619048
    # Best = 0.60
    # Slippage = (0.619048 - 0.60) / 0.60 * 10000 = 317.46 BPS
    eff_price, slippage_bps = router.calculate_slippage(book, Side.BUY, 100.0)

    # NOTE: ``calculate_slippage`` computes slippage from the RAW effective
    # price (before rounding) and only rounds at the return boundary. The
    # ``eff_price`` field returned to the caller is the ROUNDED value
    # (4dp). To match, we compute expected_slippage from the RAW eff price
    # — NOT from the rounded ``eff_price`` we got back.
    expected_eff_raw = 100.0 / (100.0 + (40.0 / 0.65))
    assert eff_price == pytest.approx(expected_eff_raw, abs=1e-4)
    assert slippage_bps > 0.0
    expected_slippage_raw = abs((expected_eff_raw - 0.60) / 0.60) * 10_000.0
    assert slippage_bps == pytest.approx(expected_slippage_raw, abs=0.2)


# ── 3. calculate_slippage empty book falls back to mid / 10 BPS ────────────────
def test_calculate_slippage_empty_book_falls_back_to_mid_and_default_bps():
    """An empty-side book must fall back to ``mid or 0.50`` for the effective
    price and a default 10 BPS slippage (the documented fallback)."""
    router = SmartOrderRouter()
    # Empty ASK ladder (BUY side walks ASKs); best_bid exists so mid is 0.50.
    book = _book(bids=[(0.45, 100.0)], asks=[])

    eff_price, slippage_bps = router.calculate_slippage(book, Side.BUY, 100.0)
    assert eff_price == pytest.approx(0.50, abs=1e-4)  # mid = (0.45+0.55)/2 → 0.45 alone → mid=None → 0.50
    assert slippage_bps == pytest.approx(10.0, abs=1e-4)


# ── 4. calculate_slippage with size_usdc=0 returns (mid, 0.0) ─────────────────
def test_calculate_slippage_zero_size_returns_mid_zero_bps():
    """``size_usdc == 0`` means zero capital to consume — the loop never
    accrues any shares, the ``total_shares <= 0`` guard returns
    ``(book.mid or 0.50, 0.0)``."""
    router = SmartOrderRouter()
    book = _book(asks=[(0.60, 100.0)], bids=[(0.58, 100.0)])
    eff_price, slippage_bps = router.calculate_slippage(book, Side.BUY, 0.0)

    # mid = (0.60 + 0.58) / 2 = 0.59
    assert eff_price == pytest.approx(0.59, abs=1e-4)
    assert slippage_bps == pytest.approx(0.0, abs=1e-4)


# ── 5. plan_execution selects "direct" for size <= $50 ────────────────────────
def test_plan_execution_selects_direct_for_small_size():
    """``size <= $50`` must select ``strategy="direct"`` — a single slice
    with no child decomposition."""
    router = SmartOrderRouter()
    # Deep single-level book so slippage stays at 0 BPS (within tolerance).
    book = _book(
        asks=[(0.60, 100_000.0)],
        bids=[(0.59, 100_000.0)],
    )

    plan = router.plan_execution(book, Side.BUY, 50.0)

    assert plan.approved is True
    assert plan.strategy == "direct"
    assert len(plan.slices) == 1
    assert plan.slices[0].notes == "direct"
    assert plan.slices[0].size_usdc == pytest.approx(50.0, abs=1e-2)


# ── 6. plan_execution selects "twap" for $50 < size <= $250 ──────────────────
def test_plan_execution_selects_twap_for_medium_size():
    """``$50 < size <= $250`` must select ``strategy="twap"`` with 3 slices
    whose sizes sum to roughly the requested total (each slice ±15% jitter)."""
    router = SmartOrderRouter()
    book = _book(
        asks=[(0.60, 100_000.0)],
        bids=[(0.59, 100_000.0)],
    )

    plan = router.plan_execution(book, Side.BUY, 200.0)

    assert plan.approved is True
    assert plan.strategy == "twap"
    assert len(plan.slices) == 3
    # All slices carry the "twap" note.
    assert all(s.notes == "twap" for s in plan.slices)
    # Slice sizes sum to within ±15% of the requested total (jitter is ±15%
    # per slice, but with three slices the central-limit variance shrinks).
    total = sum(s.size_usdc for s in plan.slices)
    assert 200.0 * 0.85 <= total <= 200.0 * 1.15


# ── 7. plan_execution selects "vwap" for size > $250 ──────────────────────────
def test_plan_execution_selects_vwap_for_large_size():
    """``size > $250`` must select ``strategy="vwap"`` — slices weighted by
    book depth.

    The book is constructed with DEEP top levels so the entire $500 order
    is consumable within the top-of-book level — effective_price == best,
    slippage_bps == 0, the plan is APPROVED. The five depth levels still
    give the VWAP slicer five slices to weight proportionally.
    """
    router = SmartOrderRouter()
    # Deep top levels so slippage stays at 0 BPS (entire order fills at top).
    # 5 distinct depth levels so the VWAP slicer has 5 slices to produce.
    book = _book(
        asks=[(0.60, 10_000.0), (0.61, 10_000.0), (0.62, 10_000.0),
              (0.63, 10_000.0), (0.64, 10_000.0)],
        bids=[(0.59, 10_000.0), (0.58, 10_000.0), (0.57, 10_000.0)],
    )

    plan = router.plan_execution(book, Side.BUY, 500.0)

    assert plan.approved is True
    assert plan.strategy == "vwap"
    # VWAP consumes up to 5 depth levels → up to 5 slices.
    assert 1 <= len(plan.slices) <= 5
    # Every slice is tagged with the per-level note.
    assert all(s.notes.startswith("vwap_level_") for s in plan.slices)


# ── 8. plan_execution force_iceberg overrides size-based selection ───────────
def test_plan_execution_force_iceberg_overrides_size_selection():
    """``force_iceberg=True`` must select ``strategy="iceberg"`` regardless
    of the order size — even when size is small enough that the default
    decision would be "direct"."""
    router = SmartOrderRouter()
    book = _book(
        asks=[(0.60, 100_000.0)],
        bids=[(0.59, 100_000.0)],
    )

    # Small size (would normally pick "direct"), but force_iceberg=True.
    plan = router.plan_execution(book, Side.BUY, 30.0, force_iceberg=True, num_slices=4)

    assert plan.approved is True
    assert plan.strategy == "iceberg"
    assert len(plan.slices) == 4
    assert all(s.notes == "iceberg" for s in plan.slices)


# ── 9. plan_execution rejects when slippage exceeds tolerance ─────────────────
def test_plan_execution_rejects_when_slippage_exceeds_tolerance():
    """When the computed slippage exceeds the healthy tolerance (15 BPS by
    default), the plan must be REJECTED — ``approved=False``,
    ``rejection_reason`` set, no slices."""
    router = SmartOrderRouter()
    # Sparse ladder that forces multi-level traversal and > 15 BPS slippage
    # for a $100 BUY order.
    # Top level: 0.60 × 50 shares ($30 depth).
    # Second level: 0.90 × 1000 shares ($900 depth).
    # $100 order → consumes all $30 of top + $70 of second.
    # Effective = 100 / (50 + 70/0.90) = 100 / (50 + 77.78) = 100/127.78 ≈ 0.7826
    # Slippage = (0.7826 - 0.60) / 0.60 * 10000 ≈ 3043 BPS — far above 15 BPS.
    book = _book(
        asks=[(0.60, 50.0), (0.90, 1000.0)],
        bids=[(0.59, 50.0)],
    )

    plan = router.plan_execution(book, Side.BUY, 100.0)

    assert plan.approved is False
    assert "Slippage" in plan.rejection_reason
    assert "exceeds" in plan.rejection_reason
    assert plan.slices == []
    # Tolerance value echoed for visibility.
    assert SLIPPAGE_TOLERANCE_HEALTHY_BPS == 15.0


# ── 10. generate_twap_schedule legacy alias ──────────────────────────────────
def test_generate_twap_schedule_legacy_alias_direct_for_small_size():
    """The legacy alias returns a single ``direct`` slice for ``size <= 50``;
    for larger sizes it delegates to ``_twap_slices`` (3+ slices)."""
    router = SmartOrderRouter()

    # Small: direct slice.
    direct = router.generate_twap_schedule(40.0, price=0.60, duration_seconds=60, num_slices=5)
    assert len(direct) == 1
    assert direct[0].notes == "direct"
    assert direct[0].size_usdc == pytest.approx(40.0, abs=1e-2)

    # Larger: TWAP slices.
    twap = router.generate_twap_schedule(200.0, price=0.60, duration_seconds=60, num_slices=4)
    assert len(twap) == 4
    assert all(s.notes == "twap" for s in twap)


# ── 11. slippage tolerance tightens when drift detected ───────────────────────
def test_slippage_tolerance_tightens_under_drift(monkeypatch):
    """When the drift_detector reports ``SIGNIFICANT_DRIFT`` or
    ``MODERATE_SHIFT``, the tolerance tightens from 15 BPS to 8 BPS — a
    plan that would just barely pass under the healthy tolerance is REJECTED
    under the drift-tightened tolerance."""
    router = SmartOrderRouter()

    # Build a ladder whose slippage is between 8 and 15 BPS — passes healthy
    # tolerance, fails drift-tightened tolerance.
    # Top: 0.60 × 1000 ($600). Second: 0.6054 × 1000.
    # For a $1200 order, consumes $600 at 0.60 + $600 at 0.6054.
    # Effective = 1200 / (1000 + 600/0.6054) = 1200 / (1000 + 991.08) = 0.6027
    # Slippage = (0.6027 - 0.60) / 0.60 * 10000 = 45 BPS — too high.
    # Use a tighter ladder so slippage lands ~10 BPS.
    # Top: 0.60 × 1000 ($600). Second: 0.6006 × 1000.
    # $1200 → $600 at 0.60 + $600 at 0.6006.
    # Effective = 1200 / (1000 + 600/0.6006) = 1200 / 1998.998 ≈ 0.6003
    # Slippage = (0.6003 - 0.60) / 0.60 * 10000 = 5 BPS — under both.
    # Let me redo with a slightly wider second level: 0.6012.
    # Effective = 1200 / (1000 + 600/0.6012) = 1200 / 1998.001 ≈ 0.6006
    # Slippage = (0.6006 - 0.60) / 0.60 * 10000 = 10 BPS — passes healthy (15),
    # fails drift-tightened (8). Perfect.
    book = _book(
        asks=[(0.60, 1000.0), (0.6012, 1000.0)],
        bids=[(0.59, 1000.0)],
    )

    # First: under healthy tolerance (no drift) — plan is APPROVED.
    plan_healthy = router.plan_execution(book, Side.BUY, 1200.0)
    assert plan_healthy.approved is True
    # The computed slippage is ~10 BPS, under the 15 BPS healthy tolerance.
    assert 8.0 < plan_healthy.slippage_bps < 15.0

    # Now patch drift_detector to report SIGNIFICANT_DRIFT — tolerance
    # tightens to 8 BPS, and the same plan is now REJECTED.
    class _FakeDriftDetector:
        drift_status = "SIGNIFICANT_DRIFT"

    import ml.drift_detector as drift_mod
    monkeypatch.setattr(drift_mod, "drift_detector", _FakeDriftDetector())

    plan_drift = router.plan_execution(book, Side.BUY, 1200.0)
    assert plan_drift.approved is False
    assert "exceeds" in plan_drift.rejection_reason
    assert SLIPPAGE_TOLERANCE_DRIFT_BPS == 8.0


# ── 12. calculate_slippage for SELL walks the BID ladder ─────────────────────
def test_calculate_slippage_sell_walks_bid_ladder():
    """For ``Side.SELL``, the router must walk the BID ladder (not the ask
    ladder) — the side-aware depth selection. We verify by constructing an
    asymmetric book where the BID ladder has a different top-of-book price
    than the ASK ladder; the effective price must equal the BID top."""
    router = SmartOrderRouter()
    # BID top is 0.58; ASK top is 0.62.
    # A small SELL (size $50) consumes only the top BID level.
    book = _book(
        bids=[(0.58, 1000.0), (0.57, 1000.0)],
        asks=[(0.62, 1000.0), (0.63, 1000.0)],
    )

    eff_price, slippage_bps = router.calculate_slippage(book, Side.SELL, 50.0)
    # All shares consumed at the top BID (0.58), no adverse move.
    assert eff_price == pytest.approx(0.58, abs=1e-4)
    assert slippage_bps == pytest.approx(0.0, abs=1e-4)
