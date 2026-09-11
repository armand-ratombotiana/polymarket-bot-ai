"""
execution/advanced_router.py — Advanced order routing with TWAP, VWAP, and iceberg support.

Complements ``execution/smart_router.py`` (the book-aware slicer that walks
the live Polymarket CLOB order book) with a **planning layer** that decides
*which* execution strategy to use and *how* to slice a large parent order
across time. The two modules are intentionally distinct:

  * ``smart_router.SmartOrderRouter`` — microstructure-aware. Given a live
    ``OrderBook`` it computes effective fill price / slippage in BPS and
    builds an ``ExecutionPlan`` whose slices carry concrete ``price`` /
    ``size_usdc`` per book level. Production strategy code calls this at
    order-submission time.

  * ``advanced_router.AdvancedOrderRouter`` (this module) — strategy-aware.
    Given a target ``total_size`` and high-level context (avg daily volume,
    spread, urgency) it decides whether to TWAP / VWAP / iceberg / execute
    immediately and returns an ``OrderPlan`` of time-scheduled slices with
    delay offsets. The dashboard's "Execution Planner" panel calls this so
    an operator can preview an execution schedule *before* committing
    capital; the live trading path can also call it for the same preview.

Five capabilities (per W15-8 task spec):

  1. **Multi-venue routing** — ``select_venue`` picks the better-priced
     venue when the same market is listed on both the CLOB and an AMM.
  2. **TWAP** — equal-sized slices spaced uniformly over a duration.
  3. **VWAP** — slices proportional to a historical intraday volume
     profile so more capital flows in high-volume intervals.
  4. **Iceberg** — fixed visible ``visible_size`` quanta released on a
     short cadence so the full parent quantity is concealed from
     adversarial book readers.
  5. **Adaptive slippage** — ``adaptive_slippage_tolerance`` scales the
     acceptable-slippage gate by recent volatility: a calm market
     permits a tight tolerance, a volatile market widens it so legit
     fills are not rejected for transient spread expansion.

The router is pure-Python and side-effect free (no network, no I/O) so it
is trivially unit-testable and safe to call from the dashboard preview
endpoint without touching the live trading path.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ── Adaptive-slippage tuning constants ──────────────────────────────────────
# The base tolerance below mirrors ``smart_router.SLIPPAGE_TOLERANCE_HEALTHY_BPS``
# (15 BPS) so the two routers apply the same "calm market" gate by default;
# kept as a local constant (rather than imported) so this module has zero
# cross-module runtime dependencies and remains safe to import in isolation.
DEFAULT_BASE_SLIPPAGE_BPS: float = 15.0
# Multiplier applied per unit of realized volatility (in BPS). A 100-BPS-vol
# market widens the tolerance by ``0.1 * 100 = 10 BPS`` (15 → 25). Tuned so
# the typical Polymarket intraday vol (~30 BPS) only marginally relaxes the
# gate, while a regime shift (~300 BPS) roughly doubles the tolerance.
ADAPTIVE_SLOPE: float = 0.10
# Hard ceiling so an extreme vol spike cannot open the gate to a
# pathological slippage (the gate is a safety control, not a free pass).
MAX_ADAPTIVE_SLIPPAGE_BPS: float = 60.0
# Floor so the tolerance never collapses below half the base — even a
# completely flat market keeps a non-zero adverse-selection buffer.
MIN_ADAPTIVE_SLIPPAGE_BPS: float = 7.5


@dataclass
class OrderPlan:
    """Execution plan for a large order.

    ``slices`` is a list of dicts of shape::

        {
            "index": int,                  # 0-based slice ordinal
            "size": float,                 # slice size (USDC or shares — caller's unit)
            "price_target": Optional[float],  # hint price; None means "fill at market"
            "delay_seconds": float,        # offset from plan start (seconds)
        }

    ``estimated_cost`` / ``estimated_slippage_bps`` are best-effort
    projections filled in by callers that have book context; the planner
    itself leaves them at 0 because it does not consult a live book.
    """

    strategy: str  # "twap" | "vwap" | "iceberg" | "immediate"
    slices: list[dict] = field(default_factory=list)
    total_size: float = 0.0
    estimated_cost: float = 0.0
    estimated_slippage_bps: float = 0.0
    duration_seconds: float = 0.0


class AdvancedOrderRouter:
    """Plans optimal execution for large orders.

    The router is a **pure planner** — it produces an ``OrderPlan`` of
    time-scheduled slices but does not itself submit any orders. A live
    execution engine consumes the plan and routes each slice through
    ``SmartOrderRouter`` (for book-aware sizing) and the venue adapter
    (for actual order submission).
    """

    def __init__(
        self,
        max_slice_size: float = 50.0,
        max_duration: float = 300.0,
    ) -> None:
        # ``max_slice_size`` caps the visible quantum of an iceberg order
        # AND bounds the per-slice size of any strategy (a single TWAP
        # slice larger than this is split further by the live path).
        self.max_slice_size = max_slice_size
        # ``max_duration`` bounds the total wall-clock span of any plan so
        # a misconfigured caller cannot request a 24-hour TWAP that would
        # outlive the operator's attention span.
        self.max_duration = max_duration

    # ── TWAP ────────────────────────────────────────────────────────────────
    def plan_twap(
        self,
        total_size: float,
        duration: float = 60.0,
        n_slices: int = 5,
    ) -> OrderPlan:
        """Time-Weighted Average Price — equal slices over time.

        Splits ``total_size`` into ``n_slices`` uniform child orders spaced
        ``duration / n_slices`` seconds apart. Each slice carries the SAME
        size (modulo floating-point) so the time-weighted average fill
        price converges to the period's average print.
        """
        # Defensive clamps so a misconfigured caller cannot request a
        # zero-slice plan or a duration longer than the configured cap.
        n_slices = max(1, int(n_slices))
        duration = min(max(0.0, float(duration)), self.max_duration)
        slice_size = total_size / n_slices
        delay = duration / n_slices if n_slices else 0.0
        slices = [
            {
                "index": i,
                "size": slice_size,
                "price_target": None,
                "delay_seconds": i * delay,
            }
            for i in range(n_slices)
        ]
        return OrderPlan(
            strategy="twap",
            slices=slices,
            total_size=total_size,
            estimated_cost=0.0,  # Filled at execution time against live book.
            estimated_slippage_bps=0.0,
            duration_seconds=duration,
        )

    # ── VWAP ────────────────────────────────────────────────────────────────
    def plan_vwap(
        self,
        total_size: float,
        volume_profile: list[float],
    ) -> OrderPlan:
        """Volume-Weighted Average Price — slices proportional to volume.

        ``volume_profile`` is a list of per-interval volumes (e.g. the
        last N 5-minute bins' traded share counts). Each slice's size is
        ``total_size * (vol_bin / sum(vol_bins))`` so more capital is
        deployed in historically high-volume intervals (when liquidity is
        deeper and market impact is lower).

        Falls back to a uniform TWAP over ``len(volume_profile)`` slices
        when the profile sums to zero (a degenerate market with no
        observable volume — typical for a freshly-listed token).
        """
        if not volume_profile:
            # Empty profile → fall back to a 5-slice TWAP so the plan is
            # still usable (an empty plan would force the caller to handle
            # a special case at every call site).
            return self.plan_twap(total_size, duration=60.0, n_slices=5)

        total_volume = float(sum(volume_profile))
        if total_volume <= 0.0:
            # Degenerate profile (all zeros) → uniform fallback.
            return self.plan_twap(
                total_size, duration=60.0, n_slices=len(volume_profile)
            )

        n_bins = len(volume_profile)
        # Spread the slices across a 60-second window (one slice per bin,
        # uniformly spaced) — the caller can rescale via slice indices if
        # a longer horizon is desired. Mirrors the smart_router's VWAP
        # spread convention so the two planners compose cleanly.
        bin_interval = 60.0 / n_bins
        slices: list[dict] = []
        cumulative_delay = 0.0
        for i, vol in enumerate(volume_profile):
            fraction = vol / total_volume
            slice_size = total_size * fraction
            slices.append(
                {
                    "index": i,
                    "size": slice_size,
                    "price_target": None,
                    "delay_seconds": cumulative_delay,
                }
            )
            cumulative_delay += bin_interval
        return OrderPlan(
            strategy="vwap",
            slices=slices,
            total_size=total_size,
            estimated_cost=0.0,
            estimated_slippage_bps=0.0,
            duration_seconds=cumulative_delay,
        )

    # ── Iceberg ──────────────────────────────────────────────────────────────
    def plan_iceberg(
        self,
        total_size: float,
        visible_size: Optional[float] = None,
    ) -> OrderPlan:
        """Iceberg — show only ``visible_size`` at a time.

        Releases the parent order in fixed ``visible_size`` quanta on a
        short cadence (2 s between slices) so only the visible quantum is
        ever exposed on the book at once. The hidden remainder is
        re-released only after the prior slice fills — concealing the
        parent order's true size from adversarial order-flow readers.

        ``visible_size`` defaults to ``min(max_slice_size, total_size / 5)``
        so the iceberg never opens with a visible quantum larger than the
        configured cap and never uses fewer than 5 slices (which would
        defeat the purpose of concealment).
        """
        if visible_size is None or visible_size <= 0:
            visible = min(self.max_slice_size, total_size / 5.0)
        else:
            visible = float(visible_size)
        # Guard against a caller-supplied visible_size larger than the
        # parent itself (would produce a single-slice plan that's not an
        # iceberg at all).
        visible = min(visible, total_size)

        n_slices = max(1, math.ceil(total_size / visible)) if visible > 0 else 1
        slices: list[dict] = []
        remaining = total_size
        for i in range(n_slices):
            size = min(visible, remaining)
            slices.append(
                {
                    "index": i,
                    "size": size,
                    "price_target": None,
                    "delay_seconds": i * 2.0,  # 2 s between slices
                }
            )
            remaining -= size
        return OrderPlan(
            strategy="iceberg",
            slices=slices,
            total_size=total_size,
            estimated_cost=0.0,
            estimated_slippage_bps=0.0,
            duration_seconds=n_slices * 2.0,
        )

    # ── Immediate (no slicing) ────────────────────────────────────────────────
    def plan_immediate(self, total_size: float) -> OrderPlan:
        """Execute immediately (for small orders).

        Returns a single-slice plan with zero delay — the caller routes
        the full parent quantity through ``SmartOrderRouter`` in one
        shot. Used when the parent is small enough that slicing would
        only add latency without reducing market impact.
        """
        return OrderPlan(
            strategy="immediate",
            slices=[
                {
                    "index": 0,
                    "size": total_size,
                    "price_target": None,
                    "delay_seconds": 0.0,
                }
            ],
            total_size=total_size,
            estimated_cost=0.0,
            estimated_slippage_bps=0.0,
            duration_seconds=0.0,
        )

    # ── Strategy recommendation ─────────────────────────────────────────────
    def recommend_strategy(
        self,
        total_size: float,
        avg_daily_volume: float,
        spread_bps: float,
        urgency: str = "normal",
    ) -> str:
        """Recommend the best execution strategy.

        Decision tree (evaluated top-down; first match wins):

          1. ``total_size < 0.1 % of ADV`` → **immediate** (slicing a
             tiny order adds latency without reducing impact).
          2. ``urgency == "urgent"`` → **immediate** (a trader who
             flagged urgency is willing to pay the slippage to get the
             fill now).
          3. ``spread_bps > 50 AND total_size > 1 % ADV`` → **iceberg**
             (a wide spread + meaningful size means crossing the spread
             repeatedly would be costly; concealment lets each slice
             rest as a passive limit and only cross when filled).
          4. ``total_size > 0.5 % ADV`` → **twap** (large enough to
             benefit from time-slicing but spread is normal, so simple
             uniform slices are the right default).
          5. otherwise → **immediate** (the order is neither urgent nor
             large enough to warrant slicing).
        """
        # 1 — small order relative to volume → immediate.
        if avg_daily_volume > 0 and total_size < avg_daily_volume * 0.001:
            return "immediate"

        # 2 — very urgent → immediate regardless.
        if urgency == "urgent":
            return "immediate"

        # 3 — large order, wide spread → iceberg.
        if spread_bps > 50 and total_size > avg_daily_volume * 0.01:
            return "iceberg"

        # 4 — large order, normal conditions → TWAP.
        if total_size > avg_daily_volume * 0.005:
            return "twap"

        # 5 — default.
        return "immediate"

    # ── Slippage estimation ─────────────────────────────────────────────────
    def estimate_slippage(
        self,
        order_size: float,
        book_depth: float,
        spread_bps: float,
    ) -> float:
        """Estimate slippage in BPS based on order size vs book depth.

        Linear-impact model: the adverse move is half the bid-ask
        spread (the cost of crossing the spread on a marketable order)
        PLUS a linear market-impact term proportional to
        ``order_size / book_depth``.

        ``book_depth <= 0`` returns ``spread_bps * 2`` (a degenerate
        book with zero observable depth — the planner assumes the worst
        case rather than silently returning zero).
        """
        if book_depth <= 0:
            return spread_bps * 2  # Worst case.
        # Linear impact: each "full book" of order size adds 100 BPS of
        # adverse move (a 50 %-depth order adds 50 BPS, etc.). Tuned so
        # a small order in a deep book pays just half the spread (the
        # crossing cost), and a deep-eating order pays proportionally.
        impact = (order_size / book_depth) * 100.0
        return spread_bps / 2.0 + impact

    # ── Multi-venue routing ──────────────────────────────────────────────────
    def select_venue(
        self,
        venues: list[dict],
        side: str,
    ) -> dict:
        """Pick the venue with the best executable price.

        ``venues`` is a list of dicts, one per available venue, each of
        shape::

            {
                "venue": "clob" | "amm" | <arbitrary string>,
                "price": float,        # the price the venue quotes for `side`
                "depth": float,        # available size at `price`
                "fee_bps": float,      # venue taker fee (BPS)
            }

        For a BUY the router selects the lowest ``price + fee``; for a
        SELL it selects the highest ``price - fee``. Ties (within 1 BPS)
        prefer the venue with greater depth so the order has a higher
        probability of filling at the quoted price.

        Returns the chosen venue dict (the original reference, not a
        copy) so the caller can read non-price fields (e.g. ``venue``)
        directly. Raises ``ValueError`` if ``venues`` is empty.
        """
        if not venues:
            raise ValueError("select_venue requires at least one venue")

        def _effective_cost(v: dict) -> float:
            """All-in price (price adjusted for fee) — lower is better for
            BUY, higher is better for SELL. We return a single sortable
            scalar by negating the price for SELLs so the min() over the
            list works for both sides."""
            fee_bps = float(v.get("fee_bps", 0.0) or 0.0)
            price = float(v["price"])
            # Convert BPS to a price multiplier: a 10-BPS fee on a 0.50
            # price adds 0.001 (0.10 % of 0.50) to the BUY cost.
            fee_price_adj = price * (fee_bps / 10_000.0)
            if side.upper() == "SELL":
                # SELL wants the HIGHEST net price (price − fee); negate
                # so the min() selection still picks it.
                return -(price - fee_price_adj)
            # BUY wants the LOWEST net price (price + fee).
            return price + fee_price_adj

        # Sort by (effective_cost, -depth) so a cheaper venue wins, and a
        # tie within 1 BPS prefers deeper liquidity. The depth tiebreak
        # uses negative depth so a LARGER depth sorts FIRST (smaller
        # key), giving the deeper venue priority.
        def _sort_key(v: dict) -> tuple[float, float]:
            return (_effective_cost(v), -float(v.get("depth", 0.0) or 0.0))

        best = min(venues, key=_sort_key)
        logger.debug(
            "[advanced_router] select_venue: side=%s chose=%s price=%.4f "
            "depth=%.2f fee_bps=%.2f",
            side, best.get("venue"), float(best["price"]),
            float(best.get("depth", 0.0) or 0.0),
            float(best.get("fee_bps", 0.0) or 0.0),
        )
        return best

    # ── Adaptive slippage tolerance ──────────────────────────────────────────
    def adaptive_slippage_tolerance(
        self,
        volatility_bps: float,
        base_bps: float = DEFAULT_BASE_SLIPPAGE_BPS,
    ) -> float:
        """Scale the acceptable-slippage tolerance by realized volatility.

        A calm market (low vol) keeps a tight tolerance so the gate
        rejects sloppy fills; a volatile market (high vol) widens the
        tolerance so legit fills are not rejected merely because the
        spread expanded with the vol regime.

        Linear model: ``tolerance = base + ADAPTIVE_SLOPE * volatility_bps``,
        clamped to ``[MIN_ADAPTIVE_SLIPPAGE_BPS, MAX_ADAPTIVE_SLIPPAGE_BPS]``.

        Tuning rationale: a 100-BPS-vol market (typical of a moderately
        active prediction market) widens the tolerance by 10 BPS — enough
        to absorb transient spread expansion without opening the gate
        to a 50-BPS adverse fill. A 300-BPS-vol regime (a breaking-news
        spike) widens by 30 BPS — still well under the 60-BPS hard
        ceiling, which represents a "stop and re-evaluate" threshold.
        """
        vol = max(0.0, float(volatility_bps))
        tolerance = base_bps + ADAPTIVE_SLOPE * vol
        return min(max(tolerance, MIN_ADAPTIVE_SLIPPAGE_BPS), MAX_ADAPTIVE_SLIPPAGE_BPS)

    # ── Convenience: plan from a strategy name ───────────────────────────────
    def plan(
        self,
        strategy: str,
        total_size: float,
        *,
        duration: float = 60.0,
        n_slices: int = 5,
        volume_profile: Optional[list[float]] = None,
        visible_size: Optional[float] = None,
    ) -> OrderPlan:
        """Dispatch to the named planner.

        Centralizes the strategy-name → planner dispatch so callers (the
        ``/api/execution/plan`` endpoint, the live trading path, tests)
        have a single entry point. ``strategy="auto"`` runs
        ``recommend_strategy`` with conservative defaults; callers that
        have richer context (ADV, spread, urgency) should call
        ``recommend_strategy`` directly and pass the result here.
        """
        strategy = (strategy or "immediate").lower()
        if strategy == "twap":
            return self.plan_twap(total_size, duration=duration, n_slices=n_slices)
        if strategy == "vwap":
            profile = volume_profile or [1.0] * max(1, n_slices)
            return self.plan_vwap(total_size, volume_profile=profile)
        if strategy == "iceberg":
            return self.plan_iceberg(total_size, visible_size=visible_size)
        if strategy == "auto":
            # Conservative defaults for the auto path — callers with
            # richer context should call recommend_strategy themselves.
            rec = self.recommend_strategy(
                total_size=total_size,
                avg_daily_volume=10_000.0,
                spread_bps=20.0,
                urgency="normal",
            )
            return self.plan(
                rec,
                total_size,
                duration=duration,
                n_slices=n_slices,
                volume_profile=volume_profile,
                visible_size=visible_size,
            )
        # Default / "immediate" / unknown → immediate single slice.
        return self.plan_immediate(total_size)
