"""Comprehensive backtest metrics.

Beyond basic P&L/Sharpe, tracks:

  - **Adverse selection** — how much edge is lost to informed flow. A
    strategy that buys at the bid and sees the mid move *against* it
    within a short horizon is being adversely selected by counterparties
    who know more. We measure this as the post-trade mid drift: the
    difference between the mid at ``fill_time`` and the mid
    ``adverse_horizon_seconds`` later, signed against the trade
    direction. A long that drifts down → adverse selection cost > 0.

  - **Edge decay** — how quickly a strategy's edge erodes after the
    signal fires. A signal that captures a transient mispricing should
    see its edge shrink toward zero as the market reverts; a signal
    whose edge persists for hours/days is fundamentally different (and
    capacity-wise, more valuable) from one whose edge evaporates in
    seconds. We measure this as the cumulative realised-edge percentile
    curve binned by time-since-signal.

  - **Calibration** — predicted vs actual outcome frequency, bucketed
    by the model's predicted probability. A perfectly calibrated model
    has ``bucket_predicted == bucket_actual`` for every bucket; we
    compute the per-bucket residual and an aggregate ECE (expected
    calibration error).

  - **Fill / cancellation ratios** — by-stage counts + ratios so a
    strategy that submits 100 orders, fills 40, cancels 60 has
    ``fill_ratio=0.40`` / ``cancel_ratio=0.60``.

  - **Latency distribution** — wall-clock from signal-fire to
    fill-acknowledgement, binned into p50/p90/p99 + max. A
    strategy whose edge decays in 200 ms but whose p99 latency is
    500 ms is structurally unprofitable.

  - **Performance by regime** — breakdown of P&L / win-rate / Sharpe
    across market_type / horizon / liquidity buckets so a strategy
    that only makes money in thin illiquid markets can be identified
    (and sized accordingly).

  - **Slippage distribution** — signed difference between
    decision-time mid and actual fill price, expressed in bps. The
    p50 / p90 / p99 + mean expose whether slippage is symmetric or
    one-tailed (one-tailed slippage = consistent adverse fill).

  - **Fee impact** — total fees paid as a fraction of gross P&L so
    a strategy that grosses +$100 but pays $30 in fees has
    ``fee_drag = 30%``.

Public surface
~~~~~~~~~~~~~~

  * :class:`AdverseSelectionResult`         — dataclass result of
    :func:`compute_adverse_selection`.
  * :class:`EdgeDecayResult`                — dataclass result of
    :func:`compute_edge_decay`.
  * :class:`CalibrationResult`              — dataclass result of
    :func:`compute_calibration`.
  * :class:`FillCancelRatios`               — dataclass result of
    :func:`compute_fill_cancel_ratios`.
  * :class:`LatencyDistribution`             — dataclass result of
    :func:`compute_latency_distribution`.
  * :class:`RegimePerformanceResult`        — dataclass result of
    :func:`compute_regime_performance`.
  * :class:`SlippageDistribution`           — dataclass result of
    :func:`compute_slippage_distribution`.
  * :class:`FeeImpactResult`                — dataclass result of
    :func:`compute_fee_impact`.
  * :class:`ComprehensiveMetrics`           — aggregate of the eight
    above (returned by :func:`compute_comprehensive_metrics`).

  * :func:`compute_adverse_selection(trades, horizon_s)`
  * :func:`compute_edge_decay(trades, bins_s)`
  * :func:`compute_calibration(predictions, outcomes, n_buckets)`
  * :func:`compute_fill_cancel_ratios(orders)`
  * :func:`compute_latency_distribution(orders)`
  * :func:`compute_regime_performance(trades, regime_key)`
  * :func:`compute_slippage_distribution(trades)`
  * :func:`compute_fee_impact(trades)`
  * :func:`compute_comprehensive_metrics(backtest_result)`

Design notes
~~~~~~~~~~~~

Every metric is a *pure* function — no I/O, no DB, no network. The
caller is responsible for shaping the trade / order / prediction
records into the documented dict-shape each helper expects (the
shape mirrors what :mod:`backtesting.engine.run_realistic_backtest`
already produces). All numeric fields are Python ``float`` / ``int``
(not ``np.float64``) so the dataclasses round-trip through
``json.dumps`` without a custom encoder — same convention as
:mod:`backtesting.report`.

The module is intentionally additive — :mod:`backtesting.report` is
NOT modified; the comprehensive metrics are an opt-in layer that
operators can request via the existing ``POST /api/backtest/report``
endpoint (which now passes ``include_comprehensive=True`` through to
:func:`compute_comprehensive_metrics` and embeds the result under the
``comprehensive`` key).
"""
from __future__ import annotations

import logging
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


# ── Adverse selection ────────────────────────────────────────────────────────


@dataclass
class AdverseSelectionResult:
    """Outcome of :func:`compute_adverse_selection`.

    All cost fields are expressed in **basis points** (bps) of the
    notional at trade time. A positive value means the trade was
    adversely selected (the mid moved against the trade direction
    within the horizon). The ``n_analysed`` field surfaces how many
    of the supplied trades actually carried the post-trade mid series
    required to compute the metric (trades without the
    ``post_fill_mid_series`` field are skipped, not counted as zero).
    """

    n_analysed: int
    mean_cost_bps: float
    median_cost_bps: float
    p90_cost_bps: float
    p99_cost_bps: float
    max_cost_bps: float
    # Adverse selection rate: fraction of trades whose post-fill mid
    # moved *against* the trade direction by more than 1 bps.
    adverse_rate: float
    # Mean benefit (negative cost) — the *favourable* selection rate.
    # A strategy whose mean_cost_bps < 0 has favourable selection
    # (the mid moved in the trade's favour post-fill).
    favourable_rate: float
    horizon_s: float


def compute_adverse_selection(
    trades: list[dict[str, Any]],
    horizon_s: float = 60.0,
) -> AdverseSelectionResult:
    """Measure adverse-selection cost per trade.

    For each trade carrying a ``post_fill_mid_series`` field (a list
    of ``{"t": float, "mid": float}`` snapshots covering the
    ``horizon_s`` window after the fill), compute the signed mid drift
    over the horizon:

        drift_bps = (mid_at_horizon - mid_at_fill) / mid_at_fill * 10_000

    The drift is then **signed against the trade direction**: a BUY
    (``side == "BUY"``) loses money when the mid drifts DOWN, so the
    adverse-selection cost for a BUY is ``-drift_bps``; for a SELL
    it's ``+drift_bps``. A positive ``cost_bps`` means the trade was
    adversely selected.

    Args:
        trades: List of trade dicts. Each may carry:
            - ``side``: "BUY" / "SELL" (default "BUY" when absent).
            - ``fill_mid``: mid at fill time (required if
              ``post_fill_mid_series`` is absent — used as the
              reference).
            - ``post_fill_mid_series``: list of ``{"t", "mid"}``
              snapshots covering the post-fill horizon. If absent,
              the trade is skipped (counted in ``n_total`` but NOT
              in ``n_analysed``).
        horizon_s: Lookahead horizon in seconds. The mid sample
            closest to (but not exceeding) ``fill_t + horizon_s`` is
            taken as the "horizon mid". Defaults to 60 s — the
            canonical high-frequency adverse-selection window.

    Returns:
        :class:`AdverseSelectionResult` with the per-trade cost
        distribution statistics. ``n_analysed == 0`` returns a
        zeroed result so callers can render an "insufficient data"
        notice rather than NaN-out their dashboard.
    """
    if horizon_s <= 0:
        raise ValueError(
            f"horizon_s must be positive, got {horizon_s}"
        )

    costs_bps: list[float] = []
    for trade in trades:
        series = trade.get("post_fill_mid_series")
        if not series or not isinstance(series, (list, tuple)):
            continue
        fill_mid = trade.get("fill_mid")
        if fill_mid is None or float(fill_mid) <= 0:
            continue

        # Pick the sample closest to fill_t + horizon_s without
        # exceeding it. If no sample falls within the horizon, skip
        # the trade (counted in n_total but not n_analysed).
        target_t_offset = horizon_s
        # Series entries are {"t": t_offset_from_fill, "mid": m}.
        # The caller may also supply absolute timestamps — handle both.
        candidates = [
            (float(s.get("t", 0.0)), float(s.get("mid", 0.0)))
            for s in series
            if isinstance(s, dict)
        ]
        if not candidates:
            continue
        # If the t-values look like absolute Unix timestamps (very
        # large), rebase against the first sample's t to get offsets.
        t0 = candidates[0][0]
        if t0 > 1e9:
            candidates = [(t - t0, m) for (t, m) in candidates]

        within_horizon = [(t, m) for (t, m) in candidates if t <= target_t_offset]
        if not within_horizon:
            continue
        # Closest to horizon_s without exceeding it → max t in window.
        horizon_t, horizon_mid = max(within_horizon, key=lambda x: x[0])
        if horizon_mid <= 0 or float(fill_mid) <= 0:
            continue

        drift_bps = (horizon_mid - float(fill_mid)) / float(fill_mid) * 10_000.0
        # Sign against trade direction.
        side = str(trade.get("side", "BUY")).upper()
        if side in ("SELL", "SHORT", "LONG_NO"):
            cost_bps = drift_bps  # SELL benefits when mid rises
        else:
            cost_bps = -drift_bps  # BUY benefits when mid falls
        costs_bps.append(cost_bps)

    n = len(costs_bps)
    if n == 0:
        return AdverseSelectionResult(
            n_analysed=0,
            mean_cost_bps=0.0,
            median_cost_bps=0.0,
            p90_cost_bps=0.0,
            p99_cost_bps=0.0,
            max_cost_bps=0.0,
            adverse_rate=0.0,
            favourable_rate=0.0,
            horizon_s=float(horizon_s),
        )

    arr = np.asarray(costs_bps, dtype=float)
    adverse_rate = float(np.mean(arr > 1.0))
    favourable_rate = float(np.mean(arr < -1.0))

    return AdverseSelectionResult(
        n_analysed=n,
        mean_cost_bps=float(np.mean(arr)),
        median_cost_bps=float(np.median(arr)),
        p90_cost_bps=float(np.percentile(arr, 90)),
        p99_cost_bps=float(np.percentile(arr, 99)),
        max_cost_bps=float(np.max(arr)),
        adverse_rate=adverse_rate,
        favourable_rate=favourable_rate,
        horizon_s=float(horizon_s),
    )


# ── Edge decay ───────────────────────────────────────────────────────────────


@dataclass
class EdgeDecayResult:
    """Outcome of :func:`compute_edge_decay`.

    The ``bins`` list carries one dict per time-since-signal bin,
    each with the cumulative mean realised edge (signed P&L in bps)
    at that horizon. A signal whose edge is captured instantly and
    then decays to zero shows ``bins[0].mean_realised_edge_bps > 0``
    and ``bins[-1].mean_realised_edge_bps ≈ 0``; a signal whose edge
    *grows* over time (rare but possible — a fundamental thesis that
    takes hours to play out) shows ``bins[-1] > bins[0]``.

    The ``half_life_s`` field is the horizon at which the cumulative
    edge reaches 50% of its terminal value (``None`` when the
    cumulative-edge curve never crosses 50% — e.g. flat-zero edge).
    """

    bins: list[dict[str, Any]]
    # The first bin whose cumulative mean edge is ≥ 50% of the
    # terminal cumulative edge. None if the curve never crosses.
    half_life_s: Optional[float]
    # Final cumulative mean realised edge (the terminal value).
    terminal_edge_bps: float
    n_trades: int


def compute_edge_decay(
    trades: list[dict[str, Any]],
    bins_s: Optional[list[float]] = None,
) -> EdgeDecayResult:
    """Measure how quickly a signal's edge decays after fire-time.

    For each trade carrying a ``post_signal_pnl_series`` field (a
    list of ``{"t": float, "pnl_bps": float}`` snapshots of the
    cumulative P&L from the signal fire-time, expressed in bps of
    the notional at signal time), bin the trades by their
    time-since-signal offset and compute the per-bin mean cumulative
    edge.

    The default bins are [0, 1, 5, 15, 30, 60, 300, 900, 3600] seconds
    — a logarithmic schedule that captures intraday edge decay.

    Args:
        trades: List of trade dicts. Each may carry:
            - ``post_signal_pnl_series``: list of ``{"t", "pnl_bps"}``
              snapshots. ``t`` is seconds since signal fire; ``pnl_bps``
              is the cumulative P&L from fire-time to ``t``, in bps.
        bins_s: Custom bin edges (seconds since signal). Defaults to
            ``[0, 1, 5, 15, 30, 60, 300, 900, 3600]``. Must be
            non-empty and monotonically increasing.

    Returns:
        :class:`EdgeDecayResult` with per-bin mean realised edge and
        the half-life. ``n_trades == 0`` returns a zeroed result.
    """
    if bins_s is None:
        bins_s = [0.0, 1.0, 5.0, 15.0, 30.0, 60.0, 300.0, 900.0, 3600.0]
    if not bins_s:
        raise ValueError("bins_s must be non-empty")
    bins_s = sorted(float(b) for b in bins_s)
    if len(bins_s) < 2:
        raise ValueError("bins_s must have at least 2 edges")

    n_trades = 0
    # bucket[bin_idx] = list of pnl_bps values at that horizon.
    bucket: dict[int, list[float]] = defaultdict(list)

    for trade in trades:
        series = trade.get("post_signal_pnl_series")
        if not series or not isinstance(series, (list, tuple)):
            continue
        n_trades += 1
        # Find the latest sample within each bin's upper edge.
        # Pre-sort the series by t so we can walk a pointer.
        samples = sorted(
            (float(s.get("t", 0.0)), float(s.get("pnl_bps", 0.0)))
            for s in series
            if isinstance(s, dict)
        )
        # For each bin edge, take the latest sample with t <= edge.
        # If no sample qualifies (bin edge < first sample's t), the
        # bin is left empty for this trade.
        ptr = 0
        last_val: Optional[float] = None
        for bin_idx, edge in enumerate(bins_s):
            while ptr < len(samples) and samples[ptr][0] <= edge:
                last_val = samples[ptr][1]
                ptr += 1
            if last_val is not None:
                bucket[bin_idx].append(last_val)

    bins_out: list[dict[str, Any]] = []
    for bin_idx, edge in enumerate(bins_s):
        vals = bucket.get(bin_idx, [])
        bins_out.append(
            {
                "bin_edge_s": float(edge),
                "n": len(vals),
                "mean_realised_edge_bps": float(np.mean(vals)) if vals else 0.0,
                "median_realised_edge_bps": float(np.median(vals)) if vals else 0.0,
            }
        )

    # Half-life: first bin whose cumulative mean edge is ≥ 50% of the
    # terminal cumulative mean edge. Walk the cumulative-mean curve
    # forward; if it crosses 50% of its final value, record the bin
    # edge. None when the curve never crosses (e.g. flat-zero edge or
    # strictly decreasing curve that doesn't reach 50% from above).
    means = [b["mean_realised_edge_bps"] for b in bins_out]
    if not means or all(m == 0.0 for m in means):
        half_life_s: Optional[float] = None
        terminal_edge = 0.0
    else:
        terminal_edge = means[-1]
        # Sign-aware half-life: if terminal is positive, the half-life
        # is the first bin reaching >= 0.5 * terminal. If terminal is
        # negative (losing strategy), the half-life is the first bin
        # reaching <= 0.5 * terminal (i.e. lost half as much).
        threshold = 0.5 * terminal_edge
        half_life_s = None
        for b, m in zip(bins_out, means):
            if terminal_edge > 0 and m >= threshold:
                half_life_s = b["bin_edge_s"]
                break
            if terminal_edge < 0 and m <= threshold:
                half_life_s = b["bin_edge_s"]
                break

    return EdgeDecayResult(
        bins=bins_out,
        half_life_s=half_life_s,
        terminal_edge_bps=float(terminal_edge),
        n_trades=n_trades,
    )


# ── Calibration ──────────────────────────────────────────────────────────────


@dataclass
class CalibrationResult:
    """Outcome of :func:`compute_calibration`.

    The ``buckets`` list carries one dict per probability bucket
    (default 10 buckets of width 0.1 covering [0.0, 1.0]). Each
    bucket records:

      - ``bucket_lower`` / ``bucket_upper``: the probability range.
      - ``n``: count of predictions that fell in this bucket.
      - ``mean_predicted``: mean of predicted probabilities in bucket.
      - ``mean_actual``: fraction of positive outcomes in bucket
        (the empirical frequency).
      - ``residual``: ``mean_predicted - mean_actual`` (positive =
        over-confident; negative = under-confident).
      - ``abs_residual``: ``|residual|`` for ECE aggregation.

    The ``ece`` (expected calibration error) is the sample-size-
    weighted mean of ``abs_residual`` across buckets — the canonical
    summary statistic for "how well-calibrated is this model?".
    """

    buckets: list[dict[str, Any]]
    ece: float
    n_predictions: int
    n_buckets: int


def compute_calibration(
    predictions: list[float],
    outcomes: list[int | bool],
    n_buckets: int = 10,
) -> CalibrationResult:
    """Compute predicted-vs-actual calibration by probability bucket.

    Splits the [0, 1] probability range into ``n_buckets`` equal-width
    buckets and reports the per-bucket mean predicted probability,
    empirical outcome frequency, and residual.

    Args:
        predictions: Model predicted probabilities in [0, 1].
        outcomes: Binary ground-truth outcomes (0 / 1 or False /
            True). Must be the same length as ``predictions``.
        n_buckets: Number of equal-width probability buckets.
            Defaults to 10 (the canonical reliability-diagram
            resolution). Must be ≥ 1.

    Returns:
        :class:`CalibrationResult` with per-bucket stats and the
        aggregate ECE. ``n_predictions == 0`` returns a zeroed result
        with empty buckets list.
    """
    if n_buckets < 1:
        raise ValueError(f"n_buckets must be ≥ 1, got {n_buckets}")
    if len(predictions) != len(outcomes):
        raise ValueError(
            f"predictions ({len(predictions)}) and outcomes "
            f"({len(outcomes)}) must have the same length"
        )

    if not predictions:
        return CalibrationResult(
            buckets=[],
            ece=0.0,
            n_predictions=0,
            n_buckets=n_buckets,
        )

    edges = np.linspace(0.0, 1.0, n_buckets + 1)
    buckets_out: list[dict[str, Any]] = []
    pred_arr = np.asarray(predictions, dtype=float)
    outcome_arr = np.asarray(
        [1 if (o is True or o == 1) else 0 for o in outcomes],
        dtype=int,
    )

    bucket_total_n = 0
    weighted_abs_residual_sum = 0.0

    for i in range(n_buckets):
        lo = float(edges[i])
        hi = float(edges[i + 1])
        # Last bucket is inclusive on the right; others are [lo, hi).
        if i == n_buckets - 1:
            mask = (pred_arr >= lo) & (pred_arr <= hi)
        else:
            mask = (pred_arr >= lo) & (pred_arr < hi)
        n = int(mask.sum())
        if n == 0:
            buckets_out.append(
                {
                    "bucket_lower": lo,
                    "bucket_upper": hi,
                    "n": 0,
                    "mean_predicted": 0.0,
                    "mean_actual": 0.0,
                    "residual": 0.0,
                    "abs_residual": 0.0,
                }
            )
            continue
        mean_pred = float(pred_arr[mask].mean())
        mean_actual = float(outcome_arr[mask].mean())
        residual = mean_pred - mean_actual
        buckets_out.append(
            {
                "bucket_lower": lo,
                "bucket_upper": hi,
                "n": n,
                "mean_predicted": mean_pred,
                "mean_actual": mean_actual,
                "residual": residual,
                "abs_residual": abs(residual),
            }
        )
        bucket_total_n += n
        weighted_abs_residual_sum += abs(residual) * n

    ece = (
        weighted_abs_residual_sum / bucket_total_n
        if bucket_total_n > 0
        else 0.0
    )

    return CalibrationResult(
        buckets=buckets_out,
        ece=float(ece),
        n_predictions=len(predictions),
        n_buckets=n_buckets,
    )


# ── Fill / cancel ratios ─────────────────────────────────────────────────────


@dataclass
class FillCancelRatios:
    """Outcome of :func:`compute_fill_cancel_ratios`.

    Each count is the number of orders that terminated in the named
    state. The ratios are the count divided by ``total`` (0.0 when
    ``total == 0``).
    """

    total: int
    filled: int
    partially_filled: int
    cancelled: int
    rejected: int
    expired: int
    fill_ratio: float
    cancel_ratio: float
    reject_ratio: float
    partial_fill_ratio: float


def compute_fill_cancel_ratios(
    orders: list[dict[str, Any]],
) -> FillCancelRatios:
    """Tally terminal order states into fill/cancel/reject ratios.

    Each order dict is inspected for a ``final_state`` (or ``state``)
    field. Recognised terminal states (case-insensitive):

      - ``FILLED``         — full fill
      - ``PARTIALLY_FILLED`` — partial fill (terminal — order
                               cancelled post-partial)
      - ``CANCELLED``       — operator / system cancel
      - ``REJECTED``        — exchange reject
      - ``EXPIRED``         — TIF elapsed

    Unknown states are counted under ``total`` but not under any
    ratio numerator (so the ratios may sum to < 1 when the order
    stream includes non-terminal states — that's a feature, not a
    bug; the caller can detect it via ``total != filled +
    partially_filled + cancelled + rejected + expired``).
    """
    total = len(orders)
    if total == 0:
        return FillCancelRatios(
            total=0,
            filled=0,
            partially_filled=0,
            cancelled=0,
            rejected=0,
            expired=0,
            fill_ratio=0.0,
            cancel_ratio=0.0,
            reject_ratio=0.0,
            partial_fill_ratio=0.0,
        )

    counts: dict[str, int] = defaultdict(int)
    for o in orders:
        state = str(
            o.get("final_state") or o.get("state") or o.get("status") or ""
        ).upper()
        counts[state] += 1

    filled = counts.get("FILLED", 0)
    partial = counts.get("PARTIALLY_FILLED", 0)
    cancelled = counts.get("CANCELLED", 0)
    rejected = counts.get("REJECTED", 0)
    expired = counts.get("EXPIRED", 0)

    return FillCancelRatios(
        total=total,
        filled=filled,
        partially_filled=partial,
        cancelled=cancelled,
        rejected=rejected,
        expired=expired,
        fill_ratio=filled / total,
        cancel_ratio=cancelled / total,
        reject_ratio=rejected / total,
        partial_fill_ratio=partial / total,
    )


# ── Latency distribution ────────────────────────────────────────────────────


@dataclass
class LatencyDistribution:
    """Outcome of :func:`compute_latency_distribution`.

    All latencies are expressed in **milliseconds**. The
    ``samples_used`` field surfaces how many of the supplied orders
    actually carried the ``signal_to_fill_ms`` field (orders without
    it are skipped — they may pre-date the latency-tracking wiring).
    """

    samples_used: int
    mean_ms: float
    p50_ms: float
    p90_ms: float
    p99_ms: float
    max_ms: float


def compute_latency_distribution(
    orders: list[dict[str, Any]],
) -> LatencyDistribution:
    """Compute wall-clock latency from signal-fire to fill-acknowledgement.

    Each order dict is inspected for a ``signal_to_fill_ms`` (or
    ``latency_ms``) field carrying the wall-clock latency in
    milliseconds. The distribution's p50/p90/p99/max are reported
    alongside the mean so the operator can see whether the
    distribution is symmetric (mean ≈ p50) or one-tailed (max ≫
    p99 ≫ p90 — a long-tail of slow fills).

    Args:
        orders: List of order dicts. Orders without a latency field
            are skipped (counted in the input length but NOT in
            ``samples_used``).

    Returns:
        :class:`LatencyDistribution`. ``samples_used == 0`` returns
        a zeroed result.
    """
    latencies: list[float] = []
    for o in orders:
        lat = o.get("signal_to_fill_ms")
        if lat is None:
            lat = o.get("latency_ms")
        if lat is None:
            continue
        try:
            latencies.append(float(lat))
        except (TypeError, ValueError):
            continue

    n = len(latencies)
    if n == 0:
        return LatencyDistribution(
            samples_used=0,
            mean_ms=0.0,
            p50_ms=0.0,
            p90_ms=0.0,
            p99_ms=0.0,
            max_ms=0.0,
        )

    arr = np.asarray(latencies, dtype=float)
    return LatencyDistribution(
        samples_used=n,
        mean_ms=float(np.mean(arr)),
        p50_ms=float(np.percentile(arr, 50)),
        p90_ms=float(np.percentile(arr, 90)),
        p99_ms=float(np.percentile(arr, 99)),
        max_ms=float(np.max(arr)),
    )


# ── Performance by regime ────────────────────────────────────────────────────


@dataclass
class RegimePerformanceResult:
    """Outcome of :func:`compute_regime_performance`.

    The ``regimes`` list carries one dict per distinct value of the
    ``regime_key`` field across the supplied trades. Each regime's
    stats are the standard P&L roll-up: count / total_pnl / avg_pnl /
    win_rate / sharpe (computed from per-trade returns; ``sharpe=0.0``
    when n < 2).
    """

    regime_key: str
    regimes: list[dict[str, Any]]
    n_trades: int


def _sharpe_from_returns(returns: list[float]) -> float:
    """Annualisation-free Sharpe (mean / std * sqrt(n)) on a sample of returns."""
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    sd = float(np.std(arr))
    if sd < 1e-12:
        return 0.0
    return float(np.mean(arr) / sd * math.sqrt(len(arr)))


def compute_regime_performance(
    trades: list[dict[str, Any]],
    regime_key: str = "market_type",
) -> RegimePerformanceResult:
    """Break down P&L / win-rate / Sharpe by a regime dimension.

    Groups trades by ``trade[regime_key]`` (default ``"market_type"``)
    and reports per-regime stats. Common regime keys:

      - ``market_type``        — binary / scalar / multi-outcome
      - ``horizon``            — intraday / swing / multi-day
      - ``liquidity_regime``   — thin / low / medium / high / very_high
      - ``volatility_regime``  — calm / normal / volatile

    Each regime dict carries ``count`` / ``total_pnl`` / ``avg_pnl`` /
    ``win_rate`` / ``wins`` / ``losses`` / ``sharpe``. Regimes with
    zero trades are omitted (the dashboard can render an "empty
    regime" row separately if desired).

    Args:
        trades: List of trade dicts. Each must carry a ``pnl`` field
            (numeric); ``regime_key`` defaults to ``"unknown"`` when
            absent.
        regime_key: Field name to group by. Defaults to
            ``"market_type"``.

    Returns:
        :class:`RegimePerformanceResult`. ``n_trades == 0`` returns
        a zeroed result with an empty regimes list.
    """
    if not trades:
        return RegimePerformanceResult(
            regime_key=regime_key,
            regimes=[],
            n_trades=0,
        )

    by_regime: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in trades:
        label = str(t.get(regime_key) or "unknown")
        by_regime[label].append(t)

    regimes_out: list[dict[str, Any]] = []
    for label, subset in by_regime.items():
        pnls = [float(t.get("pnl") or 0.0) for t in subset]
        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        n = len(subset)
        # Convert raw P&L to a per-trade return series for Sharpe.
        # When trades carry an explicit ``return_pct`` field, use that;
        # otherwise fall back to pnl / capital (capital defaults to
        # 1.0 when absent so the per-trade return equals pnl — this
        # makes the Sharpe interpretable as "per-unit-capital" Sharpe
        # rather than per-dollar-pnl).
        rets: list[float] = []
        for t in subset:
            r = t.get("return_pct")
            if r is not None:
                rets.append(float(r))
            else:
                capital = float(t.get("capital_deployed") or 1.0)
                rets.append(float(t.get("pnl") or 0.0) / capital if capital > 0 else 0.0)
        regimes_out.append(
            {
                "regime": label,
                "count": n,
                "total_pnl": float(sum(pnls)),
                "avg_pnl": float(sum(pnls) / n) if n else 0.0,
                "wins": wins,
                "losses": losses,
                "win_rate": float(wins / n) if n else 0.0,
                "sharpe": _sharpe_from_returns(rets),
            }
        )

    # Sort regimes by total_pnl descending so the most profitable
    # regime surfaces first (matches the attribution engine's
    # convention).
    regimes_out.sort(key=lambda r: r["total_pnl"], reverse=True)

    return RegimePerformanceResult(
        regime_key=regime_key,
        regimes=regimes_out,
        n_trades=len(trades),
    )


# ── Slippage distribution ──────────────────────────────────────────────────


@dataclass
class SlippageDistribution:
    """Outcome of :func:`compute_slippage_distribution`.

    Slippage is signed in basis points (bps) of the decision-time
    mid. Positive slippage = paid MORE than mid (worse for BUY);
    negative slippage = paid LESS than mid (better for BUY). The
    ``signed_against_side`` flag determines whether the helper
    re-signs the slippage to be "cost-to-trade" (always positive =
    bad). The default cost-to-trade convention makes the metric
    directly comparable across BUYs and SELLs.
    """

    n_samples: int
    mean_bps: float
    median_bps: float
    p90_bps: float
    p99_bps: float
    max_bps: float
    # Fraction of trades with positive (unfavourable) slippage.
    unfavourable_rate: float
    # Mean slippage on the favourable side (negative cost = good).
    favourable_mean_bps: float
    # Convention used to sign the slippage.
    signed_against_side: bool


def compute_slippage_distribution(
    trades: list[dict[str, Any]],
    signed_against_side: bool = True,
) -> SlippageDistribution:
    """Compute signed slippage distribution.

    For each trade carrying a ``decision_mid`` and a ``fill_price``
    (and optionally ``side``), compute the raw slippage in bps:

        raw_bps = (fill_price - decision_mid) / decision_mid * 10_000

    When ``signed_against_side=True`` (default), re-sign so positive =
    cost-to-trade (unfavourable):

        BUY: cost_bps = +raw_bps   (paid above mid = bad)
        SELL: cost_bps = -raw_bps  (sold below mid = bad)

    A positive ``mean_bps`` means the strategy systematically pays
    the spread; a negative ``mean_bps`` means it captures it.

    Args:
        trades: List of trade dicts. Each must carry ``decision_mid``
            and ``fill_price``; ``side`` is optional (defaults to
            ``"BUY"``). Trades missing the required fields are
            skipped (counted in the input length but NOT in
            ``n_samples``).
        signed_against_side: When True (default), re-sign slippage
            to cost-to-trade convention. When False, return the raw
            ``fill_price - decision_mid`` sign (positive = filled
            above mid regardless of side).

    Returns:
        :class:`SlippageDistribution`. ``n_samples == 0`` returns
        a zeroed result.
    """
    slips: list[float] = []
    for t in trades:
        dmid = t.get("decision_mid")
        fp = t.get("fill_price")
        if dmid is None or fp is None:
            continue
        try:
            dmid_f = float(dmid)
            fp_f = float(fp)
        except (TypeError, ValueError):
            continue
        if dmid_f <= 0:
            continue
        raw_bps = (fp_f - dmid_f) / dmid_f * 10_000.0
        if signed_against_side:
            side = str(t.get("side", "BUY")).upper()
            if side in ("SELL", "SHORT", "LONG_NO"):
                cost_bps = -raw_bps
            else:
                cost_bps = raw_bps
        else:
            cost_bps = raw_bps
        slips.append(cost_bps)

    n = len(slips)
    if n == 0:
        return SlippageDistribution(
            n_samples=0,
            mean_bps=0.0,
            median_bps=0.0,
            p90_bps=0.0,
            p99_bps=0.0,
            max_bps=0.0,
            unfavourable_rate=0.0,
            favourable_mean_bps=0.0,
            signed_against_side=signed_against_side,
        )

    arr = np.asarray(slips, dtype=float)
    fav = arr[arr < 0]
    return SlippageDistribution(
        n_samples=n,
        mean_bps=float(np.mean(arr)),
        median_bps=float(np.median(arr)),
        p90_bps=float(np.percentile(arr, 90)),
        p99_bps=float(np.percentile(arr, 99)),
        max_bps=float(np.max(arr)),
        unfavourable_rate=float(np.mean(arr > 0)),
        favourable_mean_bps=float(np.mean(fav)) if len(fav) > 0 else 0.0,
        signed_against_side=signed_against_side,
    )


# ── Fee impact ───────────────────────────────────────────────────────────────


@dataclass
class FeeImpactResult:
    """Outcome of :func:`compute_fee_impact`.

    ``fee_drag_pct`` is the total fees paid as a fraction of the
    GROSS profit (only meaningful when ``gross_profit > 0``; returns
    0.0 when the strategy grossed negative or zero). A strategy
    whose ``fee_drag_pct = 0.30`` pays 30% of its gross edge in fees
    — that's a serious concern for any high-frequency strategy.
    """

    gross_pnl: float
    total_fees: float
    net_pnl: float
    fee_drag_pct: float
    fee_pct_of_notional: float
    n_trades: int


def compute_fee_impact(
    trades: list[dict[str, Any]],
) -> FeeImpactResult:
    """Measure total fee drag on gross P&L.

    Each trade dict may carry:
      - ``pnl`` (signed P&L in dollars; required for gross).
      - ``fees`` (fees paid in dollars for this trade; default 0.0).
      - ``notional`` (dollar notional of the trade; default 0.0).

    Args:
        trades: List of trade dicts. Trades without ``pnl`` default
            to 0.0; trades without ``fees`` default to 0.0.

    Returns:
        :class:`FeeImpactResult`. ``n_trades == 0`` returns a zeroed
        result.
    """
    if not trades:
        return FeeImpactResult(
            gross_pnl=0.0,
            total_fees=0.0,
            net_pnl=0.0,
            fee_drag_pct=0.0,
            fee_pct_of_notional=0.0,
            n_trades=0,
        )

    gross = sum(float(t.get("pnl") or 0.0) for t in trades)
    fees = sum(float(t.get("fees") or 0.0) for t in trades)
    notional = sum(float(t.get("notional") or 0.0) for t in trades)

    # Gross P&L here means the raw sum of trade pnls BEFORE fees
    # are netted out (the trade's ``pnl`` field is assumed to be
    # gross — i.e. the markout-vs-entry at exit time, before fees).
    # If the caller's ``pnl`` field is already net of fees, the
    # ``net_pnl`` we compute here will be double-subtracting; that's
    # a caller contract issue, not a metric-correctness issue.
    net = gross - fees
    fee_drag = (fees / gross) if gross > 0 else 0.0
    fee_pct_of_notional = (fees / notional) if notional > 0 else 0.0

    return FeeImpactResult(
        gross_pnl=float(gross),
        total_fees=float(fees),
        net_pnl=float(net),
        fee_drag_pct=float(fee_drag),
        fee_pct_of_notional=float(fee_pct_of_notional),
        n_trades=len(trades),
    )


# ── Comprehensive aggregate ─────────────────────────────────────────────────


@dataclass
class ComprehensiveMetrics:
    """Aggregate of all eight comprehensive metrics.

    Returned by :func:`compute_comprehensive_metrics` so the caller
    can render every comprehensive dimension from a single object.
    Each field is the dataclass returned by the corresponding
    ``compute_*`` helper (or ``None`` when the input lacked the data
    required for that metric — e.g. ``adverse_selection=None`` when
    no trade carried a ``post_fill_mid_series``).
    """

    adverse_selection: Optional[AdverseSelectionResult] = None
    edge_decay: Optional[EdgeDecayResult] = None
    calibration: Optional[CalibrationResult] = None
    fill_cancel_ratios: Optional[FillCancelRatios] = None
    latency_distribution: Optional[LatencyDistribution] = None
    regime_performance: Optional[RegimePerformanceResult] = None
    slippage_distribution: Optional[SlippageDistribution] = None
    fee_impact: Optional[FeeImpactResult] = None


def compute_comprehensive_metrics(
    backtest_result: dict[str, Any],
    *,
    adverse_horizon_s: float = 60.0,
    edge_decay_bins_s: Optional[list[float]] = None,
    calibration_buckets: int = 10,
    regime_key: str = "market_type",
) -> ComprehensiveMetrics:
    """Compute every comprehensive metric from a backtest result dict.

    The result dict may carry any of the following optional keys
    (each feeds a different metric; missing keys surface as ``None``
    on the returned :class:`ComprehensiveMetrics` so the caller can
    render an "insufficient data" notice per metric):

      - ``trades``                       — list of trade dicts.
      - ``orders``                       — list of order dicts.
      - ``predictions``                  — list of float probabilities.
      - ``outcomes``                     — list of binary outcomes.

    The function is intentionally tolerant: a caller passing a
    minimal ``{"trades": [...]}`` dict still gets a meaningful
    :class:`ComprehensiveMetrics` with the metrics that can be
    computed from the supplied data (slippage, fee impact, regime
    performance) and ``None`` for the metrics that need richer
    per-trade context (adverse selection, edge decay).
    """
    trades = backtest_result.get("trades") or []
    orders = backtest_result.get("orders") or []
    predictions = backtest_result.get("predictions") or []
    outcomes = backtest_result.get("outcomes") or []

    out = ComprehensiveMetrics()

    # Adverse selection — only meaningful when at least one trade
    # carries a post_fill_mid_series. If none do, surface ``None`` so
    # the caller knows the metric was not computable.
    has_adverse_data = any(
        isinstance(t, dict) and t.get("post_fill_mid_series")
        for t in trades
    )
    if has_adverse_data:
        out.adverse_selection = compute_adverse_selection(
            trades, horizon_s=adverse_horizon_s
        )

    # Edge decay — only meaningful when at least one trade carries
    # a post_signal_pnl_series.
    has_edge_decay_data = any(
        isinstance(t, dict) and t.get("post_signal_pnl_series")
        for t in trades
    )
    if has_edge_decay_data:
        out.edge_decay = compute_edge_decay(
            trades, bins_s=edge_decay_bins_s
        )

    # Calibration — requires both predictions and outcomes, of equal
    # length, and at least 1 sample.
    if predictions and outcomes and len(predictions) == len(outcomes):
        out.calibration = compute_calibration(
            list(predictions), list(outcomes), n_buckets=calibration_buckets
        )

    # Fill/cancel ratios — requires orders.
    if orders:
        out.fill_cancel_ratios = compute_fill_cancel_ratios(orders)
        out.latency_distribution = compute_latency_distribution(orders)

    # Regime performance — requires trades.
    if trades:
        out.regime_performance = compute_regime_performance(
            trades, regime_key=regime_key
        )

    # Slippage distribution — requires trades with decision_mid + fill_price.
    has_slippage_data = any(
        isinstance(t, dict) and t.get("decision_mid") is not None
        and t.get("fill_price") is not None
        for t in trades
    )
    if has_slippage_data:
        out.slippage_distribution = compute_slippage_distribution(trades)

    # Fee impact — always computable when trades are present (defaults
    # to 0.0 fees when the field is absent).
    if trades:
        out.fee_impact = compute_fee_impact(trades)

    return out


def comprehensive_metrics_to_dict(metrics: ComprehensiveMetrics) -> dict[str, Any]:
    """Serialise a :class:`ComprehensiveMetrics` to a JSON-safe dict.

    ``None`` fields are emitted as ``None`` (not omitted) so the
    caller can distinguish "metric was not computed" from "metric
    returned a zero-valued result" — the former is an insufficiency
    signal, the latter is a legitimate zero.
    """
    out: dict[str, Any] = {}
    for f in (
        "adverse_selection",
        "edge_decay",
        "calibration",
        "fill_cancel_ratios",
        "latency_distribution",
        "regime_performance",
        "slippage_distribution",
        "fee_impact",
    ):
        val = getattr(metrics, f)
        out[f] = asdict(val) if val is not None else None
    return out


__all__ = [
    "AdverseSelectionResult",
    "EdgeDecayResult",
    "CalibrationResult",
    "FillCancelRatios",
    "LatencyDistribution",
    "RegimePerformanceResult",
    "SlippageDistribution",
    "FeeImpactResult",
    "ComprehensiveMetrics",
    "compute_adverse_selection",
    "compute_edge_decay",
    "compute_calibration",
    "compute_fill_cancel_ratios",
    "compute_latency_distribution",
    "compute_regime_performance",
    "compute_slippage_distribution",
    "compute_fee_impact",
    "compute_comprehensive_metrics",
    "comprehensive_metrics_to_dict",
]
