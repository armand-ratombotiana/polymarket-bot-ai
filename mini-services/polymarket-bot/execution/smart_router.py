"""
execution/smart_router.py — Smart Order Router (SOR) with TWAP, VWAP & Iceberg Slicing.

Features:
  - Smart Order Routing (SOR) with dynamic liquidity scanning
  - TWAP Slicer for large blocks (> $250 USDC) with randomized jitter
  - VWAP Execution: volume-proportional slice sizing using book depth levels
  - Iceberg Slicer to conceal order depth from adversarial detectors
  - Fill Latency & Dynamic Slippage Estimator (basis-points against best)
  - Adaptive slippage tolerance gated on drift-detector model health
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field

from core.data_store import OrderBook, Side

log = logging.getLogger(__name__)

# Default slippage tolerances (basis points)
SLIPPAGE_TOLERANCE_HEALTHY_BPS = 15.0
SLIPPAGE_TOLERANCE_DRIFT_BPS = 8.0    # Tighter limits when model is under drift


@dataclass
class ExecutionSlice:
    slice_index: int
    total_slices: int
    size_usdc: float
    price: float
    delay_seconds: float
    notes: str = ""


@dataclass
class ExecutionPlan:
    """Full execution plan returned by the router."""
    slices: list[ExecutionSlice] = field(default_factory=list)
    effective_price: float = 0.0
    slippage_bps: float = 0.0
    total_size_usdc: float = 0.0
    strategy: str = "direct"       # "direct" | "twap" | "vwap" | "iceberg"
    approved: bool = True
    rejection_reason: str = ""


class SmartOrderRouter:
    """
    Institutional Smart Order Router (SOR) executing orders efficiently on Polymarket.

    Supports three execution strategies:
      1. Direct:   Single slice (size <= $250)
      2. TWAP:     Time-weighted uniform slices with jitter
      3. VWAP:     Volume-proportional slices weighted by book depth levels
      4. Iceberg:  Randomized size concealment across slices
    """

    def __init__(self) -> None:
        self._active_slices: list[ExecutionSlice] = []

    def _get_slippage_tolerance_bps(self) -> float:
        """Return tighter slippage tolerance when model drift is detected."""
        try:
            from ml.drift_detector import drift_detector
            if drift_detector.drift_status in ("SIGNIFICANT_DRIFT", "MODERATE_SHIFT"):
                return SLIPPAGE_TOLERANCE_DRIFT_BPS
        except Exception:
            pass
        return SLIPPAGE_TOLERANCE_HEALTHY_BPS

    def calculate_slippage(
        self, book: OrderBook, side: Side, size_usdc: float
    ) -> tuple[float, float]:
        """
        Estimate effective fill price and slippage in basis points using book depth.

        Returns:
            (effective_price, slippage_bps)
        """
        levels = book.asks if side == Side.BUY else book.bids
        if not levels:
            base_p = book.mid or 0.50
            return base_p, 10.0  # fallback 10 BPS

        remaining_capital = size_usdc
        total_shares = 0.0
        weighted_cost = 0.0

        for lvl in levels:
            lvl_val = lvl.price * lvl.size
            if remaining_capital <= lvl_val:
                shares = remaining_capital / lvl.price
                total_shares += shares
                weighted_cost += remaining_capital
                remaining_capital = 0.0
                break
            else:
                total_shares += lvl.size
                weighted_cost += lvl_val
                remaining_capital -= lvl_val

        if total_shares <= 0.0:
            return book.mid or 0.50, 0.0

        effective_price = weighted_cost / total_shares
        best_p = levels[0].price
        slippage_bps = (
            abs((effective_price - best_p) / best_p) * 10_000.0 if best_p > 0 else 0.0
        )
        return round(effective_price, 4), round(slippage_bps, 1)

    def plan_execution(
        self,
        book: OrderBook,
        side: Side,
        total_size_usdc: float,
        *,
        duration_seconds: int = 180,
        num_slices: int = 5,
        force_iceberg: bool = False,
    ) -> ExecutionPlan:
        """
        Build a complete execution plan: slippage check + appropriate slicer selection.

        Decision logic:
          - size <= $50:  direct order
          - $50 < size <= $250: TWAP (3 slices)
          - size > $250: VWAP (volume-weighted slices)
          - force_iceberg=True: iceberg concealment regardless of size
        """
        eff_price, slippage_bps = self.calculate_slippage(book, side, total_size_usdc)

        # Slippage gate: reject plan if slippage exceeds tolerance
        tolerance_bps = self._get_slippage_tolerance_bps()
        if slippage_bps > tolerance_bps:
            log.warning(
                "[smart_router] ⚠ Execution plan REJECTED: slippage %.1f BPS > tolerance %.1f BPS "
                "(size=%.2f, side=%s)",
                slippage_bps, tolerance_bps, total_size_usdc, side.value,
            )
            return ExecutionPlan(
                effective_price=eff_price,
                slippage_bps=slippage_bps,
                total_size_usdc=total_size_usdc,
                approved=False,
                rejection_reason=f"Slippage {slippage_bps:.1f} BPS exceeds {tolerance_bps:.0f} BPS tolerance",
            )

        if force_iceberg:
            slices = self._iceberg_slices(total_size_usdc, eff_price, num_slices, duration_seconds)
            strategy = "iceberg"
        elif total_size_usdc > 250.0:
            slices = self._vwap_slices(book, side, total_size_usdc, eff_price, duration_seconds)
            strategy = "vwap"
        elif total_size_usdc > 50.0:
            slices = self._twap_slices(total_size_usdc, eff_price, min(3, num_slices), duration_seconds)
            strategy = "twap"
        else:
            slices = [ExecutionSlice(
                slice_index=1, total_slices=1,
                size_usdc=total_size_usdc, price=eff_price,
                delay_seconds=0.0, notes="direct",
            )]
            strategy = "direct"

        log.info(
            "[smart_router] Plan: %s | size=%.2f | slices=%d | slippage=%.1f BPS | price=%.4f",
            strategy, total_size_usdc, len(slices), slippage_bps, eff_price,
        )
        return ExecutionPlan(
            slices=slices,
            effective_price=eff_price,
            slippage_bps=slippage_bps,
            total_size_usdc=total_size_usdc,
            strategy=strategy,
            approved=True,
        )

    def _twap_slices(
        self,
        total_size_usdc: float,
        price: float,
        num_slices: int,
        duration_seconds: int,
    ) -> list[ExecutionSlice]:
        """Uniform time-weighted child slices with ±15% randomized jitter."""
        slices = []
        base_chunk = total_size_usdc / num_slices
        base_interval = duration_seconds / num_slices

        for i in range(num_slices):
            jitter = random.uniform(0.85, 1.15)
            chunk_sz = round(base_chunk * jitter, 2)
            delay = round(i * base_interval + random.uniform(0, 3.0), 1)
            slices.append(ExecutionSlice(
                slice_index=i + 1,
                total_slices=num_slices,
                size_usdc=chunk_sz,
                price=price,
                delay_seconds=delay,
                notes="twap",
            ))
        return slices

    def _vwap_slices(
        self,
        book: OrderBook,
        side: Side,
        total_size_usdc: float,
        price: float,
        duration_seconds: int,
    ) -> list[ExecutionSlice]:
        """
        Volume-proportional slices: each child slice is weighted by the available
        liquidity depth at each book level. Larger slices are executed into deeper
        levels to minimize market impact.
        """
        levels = book.asks if side == Side.BUY else book.bids
        if not levels or len(levels) < 2:
            # Fallback to TWAP if book is too thin
            return self._twap_slices(total_size_usdc, price, 5, duration_seconds)

        # Use up to 5 book levels for proportional slicing
        depth_levels = levels[:min(5, len(levels))]
        total_depth = sum(lvl.size * lvl.price for lvl in depth_levels)

        slices = []
        base_interval = duration_seconds / len(depth_levels)

        for i, lvl in enumerate(depth_levels):
            lvl_weight = (lvl.size * lvl.price) / max(total_depth, 1.0)
            chunk_sz = round(total_size_usdc * lvl_weight, 2)
            # Add slight timing jitter so slices don't arrive at predictable intervals
            delay = round(i * base_interval + random.uniform(-1.5, 1.5), 1)
            delay = max(0.0, delay)
            slices.append(ExecutionSlice(
                slice_index=i + 1,
                total_slices=len(depth_levels),
                size_usdc=max(0.50, chunk_sz),
                price=round(lvl.price, 4),
                delay_seconds=delay,
                notes=f"vwap_level_{i+1}",
            ))

        log.debug(
            "[smart_router] VWAP plan: %d slices, weights=%s",
            len(slices), [round(s.size_usdc, 2) for s in slices],
        )
        return slices

    def _iceberg_slices(
        self,
        total_size_usdc: float,
        price: float,
        num_slices: int,
        duration_seconds: int,
    ) -> list[ExecutionSlice]:
        """
        Iceberg concealment: random slice sizes summing to total,
        concealing true order intent from adversarial order-book readers.
        """
        # Generate random proportions that sum to 1
        raw = [random.random() for _ in range(num_slices)]
        total_raw = sum(raw)
        weights = [r / total_raw for r in raw]

        slices = []
        base_interval = duration_seconds / num_slices
        for i, w in enumerate(weights):
            chunk_sz = round(total_size_usdc * w, 2)
            delay = round(i * base_interval + random.uniform(-2, 2), 1)
            delay = max(0.0, delay)
            slices.append(ExecutionSlice(
                slice_index=i + 1,
                total_slices=num_slices,
                size_usdc=max(0.50, chunk_sz),
                price=price,
                delay_seconds=delay,
                notes="iceberg",
            ))
        return slices

    # Backward compat alias
    def generate_twap_schedule(
        self,
        total_size_usdc: float,
        price: float,
        duration_seconds: int = 180,
        num_slices: int = 5,
    ) -> list[ExecutionSlice]:
        """Legacy alias: generate TWAP schedule directly."""
        if total_size_usdc <= 50.0:
            return [ExecutionSlice(
                slice_index=1, total_slices=1,
                size_usdc=total_size_usdc, price=price,
                delay_seconds=0.0, notes="direct",
            )]
        return self._twap_slices(total_size_usdc, price, num_slices, duration_seconds)


# Global singleton
smart_router = SmartOrderRouter()
