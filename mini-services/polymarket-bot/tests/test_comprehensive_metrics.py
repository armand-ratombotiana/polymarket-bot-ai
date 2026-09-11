"""tests/test_comprehensive_metrics.py — W37-5 comprehensive metrics tests.

Verifies the four headline metrics exposed by
``backtesting/metrics.py``:

  1. **Adverse selection** — post-trade mid drift signed against the
     trade direction.
  2. **Edge decay** — cumulative mean realised edge by time-since-signal
     bin, plus half-life.
  3. **Calibration** — predicted-vs-actual outcome frequency by
     probability bucket + ECE.
  4. **Regime performance** — P&L / win-rate / Sharpe breakdown by
     a regime dimension (market_type, liquidity_regime, etc.).

Each metric is a *pure* function — no I/O, no DB, no network. The
tests feed the helpers deterministic synthetic inputs whose expected
outputs are hand-computed so a regression in the metric math is
caught by an exact-value assertion, not just a "is it positive?"
sanity check.

The full surface also includes:
  - fill/cancel ratios
  - latency distribution
  - slippage distribution
  - fee impact
  - the aggregate ``compute_comprehensive_metrics`` helper

These are covered by lightweight tests that verify the contract
shape (dataclass fields present, zeroed-on-empty input, correct
counters for known inputs) so the four headline metrics above get
the bulk of the assertion depth.
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Any

# ── Redirect persisted-state env vars BEFORE importing any bot module. ────
# Mirrors ``tests/conftest.py`` so a sibling test file invoked directly
# boots hermetic to ``/tmp`` rather than clobbering any real persisted
# state. ``setdefault`` lets the conftest's redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_comprehensive_metrics_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "FLAGS_DB_PATH": str(_TMP_ROOT / "feature_flags.db"),
    "AB_TEST_DB_PATH": str(_TMP_ROOT / "ab_tests.db"),
    "FEATURE_STORE_DB": str(_TMP_ROOT / "feature_store.db"),
    "IMMUTABLE_AUDIT_DB": str(_TMP_ROOT / "immutable_audit.db"),
    "JOB_QUEUE_DB": str(_TMP_ROOT / "job_queue.db"),
    "ML_VALUE_DB": str(_TMP_ROOT / "ml_economic_value.db"),
    "EXPERIMENT_DB": str(_TMP_ROOT / "backtest_experiments.db"),
    "ORDER_STATE_MACHINE_DB_PATH": str(_TMP_ROOT / "order_state_machine.db"),
    "RECOVERY_STATE_PATH": str(_TMP_ROOT / "recovery_state.json"),
    "RECON_REPORT_DIR": str(_TMP_ROOT / "reports"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-comprehensive-metrics",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``core.*``, ``ml.*``) regardless of the cwd pytest
# was launched from. Mirrors the bootstrap pattern in every existing
# ``tests/test_*.py`` sibling.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402

from backtesting.metrics import (  # noqa: E402
    AdverseSelectionResult,
    CalibrationResult,
    ComprehensiveMetrics,
    EdgeDecayResult,
    FeeImpactResult,
    FillCancelRatios,
    LatencyDistribution,
    RegimePerformanceResult,
    SlippageDistribution,
    compute_adverse_selection,
    compute_calibration,
    compute_comprehensive_metrics,
    compute_edge_decay,
    compute_fee_impact,
    compute_fill_cancel_ratios,
    compute_latency_distribution,
    compute_regime_performance,
    compute_slippage_distribution,
    comprehensive_metrics_to_dict,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Adverse selection
# ═══════════════════════════════════════════════════════════════════════════


def test_adverse_selection_buy_loses_when_mid_falls() -> None:
    """A BUY whose post-fill mid drifts DOWN is adversely selected.

    Construct: BUY at mid=0.50; 60 s later mid=0.495. The post-trade
    drift is -10 bps (0.495 / 0.50 - 1 = -0.01 = -100 bps).

    Wait — let me re-derive: drift_bps = (mid_horizon - mid_fill) /
    mid_fill * 10_000 = (0.495 - 0.50) / 0.50 * 10_000 = -100 bps.

    Signed against the BUY direction: cost_bps = -drift_bps = +100 bps
    (BUY loses when mid falls). The mean_cost_bps must equal +100.
    """
    trade = {
        "side": "BUY",
        "fill_mid": 0.50,
        "post_fill_mid_series": [
            {"t": 0.0, "mid": 0.50},
            {"t": 30.0, "mid": 0.497},
            {"t": 60.0, "mid": 0.495},  # 60 s exactly → selected
        ],
    }
    result = compute_adverse_selection([trade], horizon_s=60.0)
    assert isinstance(result, AdverseSelectionResult)
    assert result.n_analysed == 1
    # 100 bps adverse-selection cost (BUY lost money on mid drift).
    assert result.mean_cost_bps == pytest.approx(100.0, abs=0.5)
    assert result.median_cost_bps == pytest.approx(100.0, abs=0.5)
    assert result.adverse_rate == 1.0
    assert result.favourable_rate == 0.0
    assert result.horizon_s == 60.0


def test_adverse_selection_sell_loses_when_mid_rises() -> None:
    """A SELL whose post-fill mid drifts UP is adversely selected.

    Construct: SELL at mid=0.50; 60 s later mid=0.505.
    drift_bps = (0.505 - 0.50) / 0.50 * 10_000 = +100 bps.
    Signed against SELL: cost_bps = +drift_bps = +100 bps
    (SELL loses when mid rises — would have sold higher later).
    """
    trade = {
        "side": "SELL",
        "fill_mid": 0.50,
        "post_fill_mid_series": [
            {"t": 0.0, "mid": 0.50},
            {"t": 60.0, "mid": 0.505},
        ],
    }
    result = compute_adverse_selection([trade], horizon_s=60.0)
    assert result.n_analysed == 1
    assert result.mean_cost_bps == pytest.approx(100.0, abs=0.5)
    assert result.adverse_rate == 1.0


def test_adverse_selection_buy_benefits_when_mid_rises() -> None:
    """A BUY whose post-fill mid drifts UP has favourable selection
    (negative cost_bps).

    Construct: BUY at mid=0.50; 60 s later mid=0.515.
    drift_bps = +300 bps. Signed against BUY: cost_bps = -300 bps
    (BUY benefits when mid rises).
    """
    trade = {
        "side": "BUY",
        "fill_mid": 0.50,
        "post_fill_mid_series": [
            {"t": 0.0, "mid": 0.50},
            {"t": 60.0, "mid": 0.515},
        ],
    }
    result = compute_adverse_selection([trade], horizon_s=60.0)
    assert result.mean_cost_bps == pytest.approx(-300.0, abs=0.5)
    assert result.adverse_rate == 0.0
    assert result.favourable_rate == 1.0


def test_adverse_selection_empty_trades_returns_zeroed() -> None:
    """No trades → zeroed result with ``n_analysed == 0``."""
    result = compute_adverse_selection([], horizon_s=60.0)
    assert result.n_analysed == 0
    assert result.mean_cost_bps == 0.0
    assert result.adverse_rate == 0.0
    assert result.favourable_rate == 0.0


def test_adverse_selection_skips_trades_without_series() -> None:
    """Trades lacking ``post_fill_mid_series`` are skipped (counted
    in the input length but NOT in ``n_analysed``)."""
    trades = [
        {"side": "BUY", "fill_mid": 0.50},  # no series → skipped
        {
            "side": "BUY",
            "fill_mid": 0.50,
            "post_fill_mid_series": [
                {"t": 0.0, "mid": 0.50},
                {"t": 60.0, "mid": 0.495},
            ],
        },
    ]
    result = compute_adverse_selection(trades, horizon_s=60.0)
    assert result.n_analysed == 1  # only the second trade analysed
    assert result.mean_cost_bps == pytest.approx(100.0, abs=0.5)


def test_adverse_selection_invalid_horizon_raises() -> None:
    """A non-positive ``horizon_s`` must raise ``ValueError`` so a
    caller mistake (e.g. ``horizon_s=0``) doesn't silently produce
    a meaningless result."""
    with pytest.raises(ValueError):
        compute_adverse_selection([], horizon_s=0.0)
    with pytest.raises(ValueError):
        compute_adverse_selection([], horizon_s=-1.0)


# ═══════════════════════════════════════════════════════════════════════════
# 2. Edge decay
# ═══════════════════════════════════════════════════════════════════════════


def test_edge_decay_cumulative_edge_grows_then_plateaus() -> None:
    """A signal whose edge captures +50 bps in 1 s, +100 bps in 60 s,
    +100 bps at terminal (300 s) shows the canonical "edge captured
    fast, then plateaued" pattern.

    The half-life is the first bin whose cumulative mean edge ≥ 50%
    of the terminal edge (50 bps).
    """
    trade = {
        "post_signal_pnl_series": [
            {"t": 0.0, "pnl_bps": 0.0},
            {"t": 1.0, "pnl_bps": 50.0},
            {"t": 60.0, "pnl_bps": 100.0},
            {"t": 300.0, "pnl_bps": 100.0},
        ],
    }
    bins = [0.0, 1.0, 60.0, 300.0]
    result = compute_edge_decay([trade], bins_s=bins)
    assert isinstance(result, EdgeDecayResult)
    assert result.n_trades == 1
    # Terminal edge = 100 bps (the final bin's cumulative mean).
    assert result.terminal_edge_bps == pytest.approx(100.0)
    # Half-life: first bin whose cumulative mean ≥ 50% of 100 = 50.
    # At bin 1.0 the cumulative mean is 50 → half-life = 1.0 s.
    assert result.half_life_s == pytest.approx(1.0)
    # Per-bin means: [0, 50, 100, 100].
    means = [b["mean_realised_edge_bps"] for b in result.bins]
    assert means == pytest.approx([0.0, 50.0, 100.0, 100.0])


def test_edge_decay_flat_zero_edge_has_no_half_life() -> None:
    """A signal whose edge is flat-zero (no signal) has no
    half-life — the cumulative-edge curve never crosses 50% of the
    terminal (0% of 0 is undefined; we return ``None``)."""
    trade = {
        "post_signal_pnl_series": [
            {"t": 0.0, "pnl_bps": 0.0},
            {"t": 60.0, "pnl_bps": 0.0},
            {"t": 300.0, "pnl_bps": 0.0},
        ],
    }
    result = compute_edge_decay([trade], bins_s=[0.0, 60.0, 300.0])
    assert result.half_life_s is None
    assert result.terminal_edge_bps == 0.0
    # Per-bin means: [0, 0, 0].
    means = [b["mean_realised_edge_bps"] for b in result.bins]
    assert means == pytest.approx([0.0, 0.0, 0.0])


def test_edge_decay_negative_terminal_uses_loss_half_life() -> None:
    """A losing strategy (terminal edge < 0) has its half-life defined
    as the first bin reaching <= 50% of the terminal (i.e. lost
    half as much)."""
    trade = {
        "post_signal_pnl_series": [
            {"t": 0.0, "pnl_bps": 0.0},
            {"t": 1.0, "pnl_bps": -50.0},  # half of -100
            {"t": 60.0, "pnl_bps": -100.0},
        ],
    }
    result = compute_edge_decay([trade], bins_s=[0.0, 1.0, 60.0])
    assert result.terminal_edge_bps == pytest.approx(-100.0)
    # Half-life: first bin <= 0.5 * -100 = -50. Bin 1.0 = -50 → -50 <= -50 ✓.
    assert result.half_life_s == pytest.approx(1.0)


def test_edge_decay_empty_trades_returns_zeroed() -> None:
    """No trades → zeroed result with ``n_trades == 0``."""
    result = compute_edge_decay([], bins_s=[0.0, 60.0, 300.0])
    assert result.n_trades == 0
    assert result.terminal_edge_bps == 0.0
    assert result.half_life_s is None


def test_edge_decay_invalid_bins_raise() -> None:
    """An empty ``bins_s`` or a single-edge ``bins_s`` must raise
    ``ValueError``."""
    with pytest.raises(ValueError):
        compute_edge_decay([], bins_s=[])
    with pytest.raises(ValueError):
        compute_edge_decay([], bins_s=[0.0])


# ═══════════════════════════════════════════════════════════════════════════
# 3. Calibration
# ═══════════════════════════════════════════════════════════════════════════


def test_calibration_perfect_calibration_has_zero_ece() -> None:
    """A perfectly calibrated model — predicted == empirical in every
    bucket — has ECE = 0.

    Construct: 100 predictions per bucket, each bucket's predictions
    equal to the bucket midpoint, and outcomes sampled so the
    empirical frequency equals the midpoint exactly. We use 100
    samples per bucket so the midpoint × 100 produces a clean integer
    positive count (0.05 × 100 = 5, 0.95 × 100 = 95, etc.).
    """
    predictions: list[float] = []
    outcomes: list[int] = []
    # 10 buckets × 100 samples each = 1000 samples.
    for i in range(10):
        bucket_mid = (i + 0.5) / 10.0  # 0.05, 0.15, ..., 0.95
        # Generate 100 samples whose empirical frequency == bucket_mid.
        n_positive = int(round(bucket_mid * 100))
        for j in range(100):
            predictions.append(bucket_mid)
            outcomes.append(1 if j < n_positive else 0)

    result = compute_calibration(predictions, outcomes, n_buckets=10)
    assert isinstance(result, CalibrationResult)
    assert result.n_predictions == 1000
    assert result.n_buckets == 10
    # Perfect calibration → ECE == 0.
    assert result.ece == pytest.approx(0.0, abs=1e-9)
    # Each bucket's residual ≈ 0.
    for b in result.buckets:
        if b["n"] > 0:
            assert abs(b["residual"]) < 1e-9


def test_calibration_overconfident_model_has_positive_residuals() -> None:
    """A model that always predicts 0.85 but only wins 50% of the
    time has +0.35 residual in the [0.8, 0.9] bucket, and ECE = 0.35.
    """
    predictions = [0.85] * 10
    outcomes = [1, 0] * 5  # 5 wins / 5 losses = 50% empirical
    result = compute_calibration(predictions, outcomes, n_buckets=10)
    # Find the [0.8, 0.9) bucket (index 8).
    target_bucket = result.buckets[8]
    assert target_bucket["n"] == 10
    assert target_bucket["mean_predicted"] == pytest.approx(0.85)
    assert target_bucket["mean_actual"] == pytest.approx(0.50)
    assert target_bucket["residual"] == pytest.approx(0.35)
    # ECE = (10 / 10) * 0.35 = 0.35.
    assert result.ece == pytest.approx(0.35)


def test_calibration_empty_predictions_returns_zeroed() -> None:
    """No predictions → zeroed result with empty buckets list."""
    result = compute_calibration([], [], n_buckets=10)
    assert result.n_predictions == 0
    assert result.buckets == []
    assert result.ece == 0.0


def test_calibration_invalid_bucket_count_raises() -> None:
    """A non-positive ``n_buckets`` must raise ``ValueError``."""
    with pytest.raises(ValueError):
        compute_calibration([0.5], [1], n_buckets=0)
    with pytest.raises(ValueError):
        compute_calibration([0.5], [1], n_buckets=-1)


def test_calibration_mismatched_lengths_raise() -> None:
    """Predictions and outcomes of different lengths must raise
    ``ValueError`` so a caller bug doesn't silently misalign the
    arrays."""
    with pytest.raises(ValueError):
        compute_calibration([0.5, 0.6], [1], n_buckets=10)


def test_calibration_supports_bool_outcomes() -> None:
    """``outcomes`` accepts True / False in addition to 1 / 0 — the
    helper coerces both forms internally."""
    predictions = [0.5, 0.5, 0.5, 0.5]
    outcomes = [True, False, True, False]  # 50% empirical
    result = compute_calibration(predictions, outcomes, n_buckets=10)
    bucket = result.buckets[4]  # [0.4, 0.5) → 0.5 falls in next bucket
    # Actually 0.5 falls in [0.5, 0.6) → bucket index 5.
    bucket = result.buckets[5]
    assert bucket["n"] == 4
    assert bucket["mean_actual"] == pytest.approx(0.5)
    assert bucket["mean_predicted"] == pytest.approx(0.5)
    assert bucket["residual"] == pytest.approx(0.0, abs=1e-6)


# ═══════════════════════════════════════════════════════════════════════════
# 4. Regime performance breakdown
# ═══════════════════════════════════════════════════════════════════════════


def test_regime_performance_breaks_down_by_market_type() -> None:
    """Trades tagged with ``market_type`` are grouped + aggregated
    correctly: count / total_pnl / avg_pnl / win_rate / sharpe per
    regime."""
    trades = [
        # Binary regime: 3 winning, 1 losing.
        {"pnl": 10.0, "market_type": "binary", "return_pct": 0.10},
        {"pnl": 5.0, "market_type": "binary", "return_pct": 0.05},
        {"pnl": 8.0, "market_type": "binary", "return_pct": 0.08},
        {"pnl": -3.0, "market_type": "binary", "return_pct": -0.03},
        # Scalar regime: 1 winning, 1 losing.
        {"pnl": 12.0, "market_type": "scalar", "return_pct": 0.12},
        {"pnl": -5.0, "market_type": "scalar", "return_pct": -0.05},
    ]
    result = compute_regime_performance(trades, regime_key="market_type")
    assert isinstance(result, RegimePerformanceResult)
    assert result.regime_key == "market_type"
    assert result.n_trades == 6
    # Two regimes: binary + scalar.
    assert len(result.regimes) == 2

    # Sorted by total_pnl desc — binary ($20 total) first, scalar ($7) second.
    assert result.regimes[0]["regime"] == "binary"
    assert result.regimes[0]["count"] == 4
    assert result.regimes[0]["total_pnl"] == pytest.approx(20.0)
    assert result.regimes[0]["avg_pnl"] == pytest.approx(5.0)
    assert result.regimes[0]["wins"] == 3
    assert result.regimes[0]["losses"] == 1
    assert result.regimes[0]["win_rate"] == pytest.approx(0.75)
    # Sharpe is mean/std * sqrt(n) on the return_pct series.
    # returns = [0.10, 0.05, 0.08, -0.03]
    # mean = 0.05, std = ~0.0496, n = 4
    # Sharpe = 0.05 / 0.0496 * sqrt(4) ≈ 2.016
    assert result.regimes[0]["sharpe"] > 0  # positive — winning regime

    assert result.regimes[1]["regime"] == "scalar"
    assert result.regimes[1]["count"] == 2
    assert result.regimes[1]["total_pnl"] == pytest.approx(7.0)


def test_regime_performance_empty_trades_returns_zeroed() -> None:
    """No trades → zeroed result with empty regimes list."""
    result = compute_regime_performance([], regime_key="market_type")
    assert result.n_trades == 0
    assert result.regimes == []
    assert result.regime_key == "market_type"


def test_regime_performance_unknown_regime_bucket() -> None:
    """Trades missing the regime_key field bucket as ``"unknown"`` —
    they're NOT dropped, so an operator can see "X trades had no
    regime classification" rather than the metric silently
    under-counting."""
    trades = [
        {"pnl": 5.0},  # no market_type → "unknown"
        {"pnl": -2.0, "market_type": "binary"},
    ]
    result = compute_regime_performance(trades, regime_key="market_type")
    assert len(result.regimes) == 2
    regimes_by_label = {r["regime"]: r for r in result.regimes}
    assert "unknown" in regimes_by_label
    assert regimes_by_label["unknown"]["count"] == 1
    assert regimes_by_label["unknown"]["total_pnl"] == pytest.approx(5.0)
    assert "binary" in regimes_by_label
    assert regimes_by_label["binary"]["count"] == 1


def test_regime_performance_alternative_regime_key() -> None:
    """The helper supports any regime dimension — verify with
    ``liquidity_regime``."""
    trades = [
        {"pnl": 5.0, "liquidity_regime": "thin"},
        {"pnl": 10.0, "liquidity_regime": "high"},
        {"pnl": -3.0, "liquidity_regime": "high"},
    ]
    result = compute_regime_performance(trades, regime_key="liquidity_regime")
    assert result.regime_key == "liquidity_regime"
    assert len(result.regimes) == 2
    # Sorted by total_pnl desc — high ($7) first, thin ($5) second.
    assert result.regimes[0]["regime"] == "high"
    assert result.regimes[0]["count"] == 2
    assert result.regimes[1]["regime"] == "thin"


# ═══════════════════════════════════════════════════════════════════════════
# 5–8: Secondary metrics (lighter coverage — shape contract only)
# ═══════════════════════════════════════════════════════════════════════════


def test_fill_cancel_ratios_counts_terminal_states() -> None:
    """A mixed order stream — 5 FILLED + 3 CANCELLED + 1 REJECTED +
    1 EXPIRED out of 10 — produces the expected ratios."""
    orders = (
        [{"state": "FILLED"}] * 5
        + [{"state": "CANCELLED"}] * 3
        + [{"state": "REJECTED"}] * 1
        + [{"state": "EXPIRED"}] * 1
    )
    result = compute_fill_cancel_ratios(orders)
    assert isinstance(result, FillCancelRatios)
    assert result.total == 10
    assert result.filled == 5
    assert result.cancelled == 3
    assert result.rejected == 1
    assert result.expired == 1
    assert result.fill_ratio == pytest.approx(0.5)
    assert result.cancel_ratio == pytest.approx(0.3)
    assert result.reject_ratio == pytest.approx(0.1)
    assert result.partial_fill_ratio == pytest.approx(0.0)


def test_fill_cancel_ratios_empty_returns_zeroed() -> None:
    result = compute_fill_cancel_ratios([])
    assert result.total == 0
    assert result.fill_ratio == 0.0


def test_latency_distribution_percentiles() -> None:
    """Latencies [10, 20, 30, 40, 50, 60, 70, 80, 90, 100] ms.
    p50 = 55, p90 = 91, p99 = 99.1, max = 100, mean = 55."""
    orders = [
        {"signal_to_fill_ms": float(v)} for v in range(10, 101, 10)
    ]
    result = compute_latency_distribution(orders)
    assert isinstance(result, LatencyDistribution)
    assert result.samples_used == 10
    assert result.mean_ms == pytest.approx(55.0)
    assert result.p50_ms == pytest.approx(55.0)
    assert result.max_ms == pytest.approx(100.0)
    # p90 / p99 should be in the upper tail.
    assert result.p90_ms >= 80.0
    assert result.p99_ms >= 90.0


def test_latency_distribution_empty_returns_zeroed() -> None:
    result = compute_latency_distribution([])
    assert result.samples_used == 0
    assert result.mean_ms == 0.0


def test_slippage_distribution_buy_above_mid_is_unfavourable() -> None:
    """A BUY filled at 0.51 against a decision_mid of 0.50 pays
    +200 bps over mid → cost-to-trade = +200 bps (unfavourable).

    Two trades:
      - BUY at 0.51 vs mid 0.50 → raw = +200 bps → cost = +200.
      - BUY at 0.495 vs mid 0.50 → raw = -100 bps → cost = -100.
    Mean cost = (200 + -100) / 2 = +50 bps.
    """
    trades = [
        {"side": "BUY", "decision_mid": 0.50, "fill_price": 0.51},
        {"side": "BUY", "decision_mid": 0.50, "fill_price": 0.495},
    ]
    result = compute_slippage_distribution(trades, signed_against_side=True)
    assert isinstance(result, SlippageDistribution)
    assert result.n_samples == 2
    # Mean cost = (200 + -100) / 2 = +50 bps.
    assert result.mean_bps == pytest.approx(50.0, abs=0.5)
    # 1 unfavourable / 2 = 0.5.
    assert result.unfavourable_rate == pytest.approx(0.5)


def test_slippage_distribution_empty_returns_zeroed() -> None:
    result = compute_slippage_distribution([])
    assert result.n_samples == 0
    assert result.mean_bps == 0.0


def test_fee_impact_computes_drag_pct() -> None:
    """A strategy that grosses $100 in P&L but pays $20 in fees has
    fee_drag = 20% of gross."""
    trades = [
        {"pnl": 60.0, "fees": 12.0, "notional": 1000.0},
        {"pnl": 40.0, "fees": 8.0, "notional": 800.0},
    ]
    result = compute_fee_impact(trades)
    assert isinstance(result, FeeImpactResult)
    assert result.gross_pnl == pytest.approx(100.0)
    assert result.total_fees == pytest.approx(20.0)
    assert result.net_pnl == pytest.approx(80.0)
    assert result.fee_drag_pct == pytest.approx(0.20)
    assert result.fee_pct_of_notional == pytest.approx(
        20.0 / 1800.0, rel=1e-3
    )
    assert result.n_trades == 2


def test_fee_impact_empty_returns_zeroed() -> None:
    result = compute_fee_impact([])
    assert result.n_trades == 0
    assert result.gross_pnl == 0.0
    assert result.fee_drag_pct == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# 9. Comprehensive aggregate
# ═══════════════════════════════════════════════════════════════════════════


def test_compute_comprehensive_metrics_assembles_all_dimensions() -> None:
    """A backtest_result dict carrying trades + orders + predictions +
    outcomes yields a ComprehensiveMetrics object whose every field is
    populated (no ``None`` placeholders)."""
    backtest_result = {
        "trades": [
            {
                "side": "BUY",
                "pnl": 10.0,
                "decision_mid": 0.50,
                "fill_price": 0.505,
                "fill_mid": 0.50,
                "market_type": "binary",
                "post_fill_mid_series": [
                    {"t": 0.0, "mid": 0.50},
                    {"t": 60.0, "mid": 0.495},
                ],
                "post_signal_pnl_series": [
                    {"t": 0.0, "pnl_bps": 0.0},
                    {"t": 60.0, "pnl_bps": 100.0},
                ],
                "fees": 0.5,
                "notional": 100.0,
                "return_pct": 0.10,
            },
            {
                "side": "SELL",
                "pnl": -5.0,
                "decision_mid": 0.50,
                "fill_price": 0.51,
                "fill_mid": 0.50,
                "market_type": "binary",
                "post_fill_mid_series": [
                    {"t": 0.0, "mid": 0.50},
                    {"t": 60.0, "mid": 0.505},
                ],
                "post_signal_pnl_series": [
                    {"t": 0.0, "pnl_bps": 0.0},
                    {"t": 60.0, "pnl_bps": -50.0},
                ],
                "fees": 0.3,
                "notional": 50.0,
                "return_pct": -0.05,
            },
        ],
        "orders": [
            {"state": "FILLED", "signal_to_fill_ms": 50.0},
            {"state": "CANCELLED", "signal_to_fill_ms": 200.0},
            {"state": "REJECTED", "signal_to_fill_ms": 30.0},
        ],
        "predictions": [0.85, 0.85, 0.50, 0.50],
        "outcomes": [1, 0, 1, 0],
    }

    metrics = compute_comprehensive_metrics(backtest_result)
    assert isinstance(metrics, ComprehensiveMetrics)

    # Every dimension populated.
    assert metrics.adverse_selection is not None
    assert metrics.edge_decay is not None
    assert metrics.calibration is not None
    assert metrics.fill_cancel_ratios is not None
    assert metrics.latency_distribution is not None
    assert metrics.regime_performance is not None
    assert metrics.slippage_distribution is not None
    assert metrics.fee_impact is not None

    # Spot-check a few headline values.
    assert metrics.adverse_selection.n_analysed == 2  # both trades analysed
    assert metrics.edge_decay.n_trades == 2
    assert metrics.calibration.n_predictions == 4
    assert metrics.fill_cancel_ratios.total == 3
    assert metrics.latency_distribution.samples_used == 3
    assert metrics.regime_performance.n_trades == 2
    assert metrics.slippage_distribution.n_samples == 2
    assert metrics.fee_impact.n_trades == 2


def test_compute_comprehensive_metrics_returns_none_for_missing_data() -> None:
    """A minimal ``{"trades": [...]}`` dict yields None for the
    metrics that require richer per-trade context (adverse selection,
    edge decay, calibration) and a real value for the metrics that
    can be computed from trades alone (slippage, fee impact, regime)."""
    backtest_result = {
        "trades": [
            {
                "side": "BUY",
                "pnl": 5.0,
                "decision_mid": 0.50,
                "fill_price": 0.505,
                "market_type": "binary",
            },
        ],
    }
    metrics = compute_comprehensive_metrics(backtest_result)

    # These metrics require data the trade doesn't carry.
    assert metrics.adverse_selection is None  # no post_fill_mid_series
    assert metrics.edge_decay is None  # no post_signal_pnl_series
    assert metrics.calibration is None  # no predictions / outcomes
    assert metrics.fill_cancel_ratios is None  # no orders
    assert metrics.latency_distribution is None  # no orders

    # These metrics are still computable from trades alone.
    assert metrics.slippage_distribution is not None
    assert metrics.fee_impact is not None
    assert metrics.regime_performance is not None


def test_compute_comprehensive_metrics_empty_input_all_none() -> None:
    """An empty backtest_result yields a ComprehensiveMetrics whose
    every field is None — the caller can render "insufficient data"
    notices for every metric."""
    metrics = compute_comprehensive_metrics({})
    assert isinstance(metrics, ComprehensiveMetrics)
    assert metrics.adverse_selection is None
    assert metrics.edge_decay is None
    assert metrics.calibration is None
    assert metrics.fill_cancel_ratios is None
    assert metrics.latency_distribution is None
    assert metrics.regime_performance is None
    assert metrics.slippage_distribution is None
    assert metrics.fee_impact is None


def test_comprehensive_metrics_to_dict_is_json_serialisable() -> None:
    """The dict form of ComprehensiveMetrics must round-trip through
    ``json.dumps`` without a custom encoder — ``None`` fields surface
    as JSON null, populated fields surface as plain dicts."""
    import json

    backtest_result = {
        "trades": [
            {
                "side": "BUY",
                "pnl": 5.0,
                "decision_mid": 0.50,
                "fill_price": 0.505,
                "market_type": "binary",
            },
        ],
    }
    metrics = compute_comprehensive_metrics(backtest_result)
    out_dict = comprehensive_metrics_to_dict(metrics)

    # Round-trips through json.dumps.
    serialized = json.dumps(out_dict)
    assert isinstance(serialized, str)
    parsed = json.loads(serialized)
    # Missing-data fields surface as JSON null (None).
    assert parsed["adverse_selection"] is None
    # Populated fields surface as plain dicts.
    assert isinstance(parsed["slippage_distribution"], dict)
    assert isinstance(parsed["fee_impact"], dict)
    assert isinstance(parsed["regime_performance"], dict)


def test_comprehensive_metrics_to_dict_empty_metrics_all_none() -> None:
    """An all-None ComprehensiveMetrics serialises to a dict whose
    every value is None — the canonical "insufficient data"
    surface."""
    import json

    metrics = ComprehensiveMetrics()  # all fields default to None
    out_dict = comprehensive_metrics_to_dict(metrics)
    # Every value is None.
    for k, v in out_dict.items():
        assert v is None, f"{k} should be None, got {v!r}"
    # Round-trips through json.dumps.
    json.dumps(out_dict)
