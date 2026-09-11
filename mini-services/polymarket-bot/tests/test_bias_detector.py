"""Unit tests for ``backtesting/bias_detector.py``.

W37-3 — Backtest bias and leakage detector.

Covers the ten detection rules (``BL_01``..``BL_10``) plus the
top-level ``BiasDetector.analyze()`` aggregator and the FastAPI route
``POST /api/backtest/bias-check``.

The detector is **pure-Python + synchronous** — no DB, no singleton
state mutation, no async. Every test is a plain ``def`` (no
``async def``) so the file does NOT carry ``pytestmark =
pytest.mark.asyncio`` — keeps pytest-asyncio collection cost off this
file entirely. The one exception is the API-route test, which uses
``fastapi.testclient.TestClient`` (sync wrapper around an ASGI portal
that owns its own event loop — no ``await`` needed in the test body).

Test groups, in spec order:

  (1) ``detect_look_ahead_bias`` — BL_01 fires when any feature
      timestamp > prediction_time; returns None when timestamps are
      all <= prediction_time.
  (2) ``detect_data_leakage`` — BL_02 fires when train and test index
      sets overlap; returns None when they're disjoint.
  (3) ``detect_optimistic_fills`` — BL_03 fires when BUY fills below
      the period best_bid or SELL fills above the period best_ask;
      returns None when all fills are achievable.
  (4) ``detect_survivorship_bias`` — BL_05 fires when only a small
      subset of all markets was tested; returns None when the test
      set covers >= the threshold ratio of the universe.
  (5) ``detect_duplicate_participation`` — BL_09 fires when the same
      record appears in both train and test sets; returns None when
      they're record-disjoint.
  (6) ``detect_future_information`` — BL_04 fires when any feature
      column's as_of timestamp > decision_time.
  (7) ``detect_selection_bias`` — BL_06 fires when the strategy_id
      contains a positive-selection marker.
  (8) ``detect_hindsight_filtering`` — BL_07 fires when the
      signal/outcome match rate exceeds the threshold over the
      minimum sample size.
  (9) ``detect_timestamp_leakage`` — BL_08 fires when train and test
      timestamp windows overlap.
  (10) ``detect_unrealistic_capital_reuse`` — BL_10 fires when a BUY
       is entered while a prior BUY is still within the settlement
       window.
  (11) ``BiasDetector.analyze`` aggregates findings, populates the
       per-severity counters, and ``has_critical`` /
       ``critical_findings`` reflect the critical subset.
  (12) The FastAPI route ``POST /api/backtest/bias-check`` returns 200
       with the full :class:`BiasReport` payload when given a
       well-formed request, 422 when the ``backtest_result`` field is
       missing.
  (13) The ``HistoricalReplayEngine.replay`` end-to-end path
       populates ``ReplayResult.bias_report`` after every run.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ── Defensive env-var redirect (mirrors ``tests/test_out_of_sample.py``).
# ``setdefault`` lets the conftest's redirect win when both run.
_TMP_ROOT = Path("/tmp/pmbot_bias_detector_tests")
_TMP_ROOT.mkdir(parents=True, exist_ok=True)

_ENV_REDIRECTS: dict[str, str] = {
    "STORE_STATE_PATH": str(_TMP_ROOT / "store_state.json"),
    "DECISION_LEDGER_DB_PATH": str(_TMP_ROOT / "decision_ledger.db"),
    "AUDIT_DB_PATH": str(_TMP_ROOT / "audit_trail.db"),
    "MARKET_DB_PATH": str(_TMP_ROOT / "market_intelligence.db"),
    "KILL_SWITCH_PATH": str(_TMP_ROOT / "kill_switch"),
    "KILL_SWITCH_REASON_PATH": str(_TMP_ROOT / "kill_switch.reason"),
    "VECTOR_STORE_PATH": str(_TMP_ROOT / "vector_index.json"),
    "MODEL_PATH": str(_TMP_ROOT / "model.pkl"),
    "MODEL_REGISTRY_PATH": str(_TMP_ROOT / "model_registry.json"),
    "CLOSED_POSITIONS_DB_PATH": str(_TMP_ROOT / "closed_positions.db"),
    "EXECUTION_QUALITY_DB_PATH": str(_TMP_ROOT / "execution_quality.db"),
    "OBSERVABILITY_DB_PATH": str(_TMP_ROOT / "observability.db"),
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
    "API_TOKEN": "test-token-conftest",
    "CORS_ORIGINS": "http://localhost",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``backtesting.*``, ``api.*``) regardless of the cwd pytest was launched
# from. Mirrors the bootstrap pattern in tests/test_out_of_sample.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import pytest  # noqa: E402  (env must be set first)

from backtesting.bias_detector import (  # noqa: E402
    SEVERITY_CRITICAL,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    BiasDetector,
    BiasFinding,
    BiasReport,
    bias_detector,
    register_routes,
)


# ════════════════════════════════════════════════════════════════════════════
# (1) BL_01 — detect_look_ahead_bias
# ════════════════════════════════════════════════════════════════════════════


class TestDetectLookAheadBias:
    """``BiasDetector.detect_look_ahead_bias`` — BL_01."""

    def test_fires_when_any_timestamp_exceeds_prediction_time(self) -> None:
        """A feature row whose timestamp > ``prediction_time`` is a
        look-ahead violation. The finding must carry ``rule='BL_01'``,
        ``severity='critical'``, the count of offenders, and the
        first offender's index + timestamp in ``detail``."""
        features = [[0.1], [0.2], [0.3], [0.4]]
        timestamps = [10.0, 20.0, 30.0, 40.0]
        prediction_time = 25.0

        finding = BiasDetector.detect_look_ahead_bias(
            features, timestamps, prediction_time,
        )

        assert finding is not None
        assert finding.rule == "BL_01"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.type == "look_ahead_bias"
        # Two timestamps exceed prediction_time=25.0: 30.0 and 40.0.
        assert finding.detail["n_offenders"] == 2
        assert finding.detail["n_total"] == 4
        assert finding.detail["first_offender_index"] == 2
        assert finding.detail["first_offender_timestamp"] == 30.0
        assert finding.detail["prediction_time"] == 25.0
        # Recommendation must be non-empty (operator-facing).
        assert finding.recommendation
        assert "as_of" in finding.recommendation or "point-in-time" in finding.recommendation

    def test_returns_none_when_all_timestamps_within_prediction_time(self) -> None:
        """When every feature timestamp <= ``prediction_time``, no
        violation fires — the finding must be ``None``."""
        features = [[0.1], [0.2], [0.3], [0.4]]
        timestamps = [10.0, 20.0, 30.0, 40.0]
        prediction_time = 50.0  # all timestamps are <= 50.0

        finding = BiasDetector.detect_look_ahead_bias(
            features, timestamps, prediction_time,
        )

        assert finding is None

    def test_handles_feature_as_of_kwarg_shape(self) -> None:
        """The ``feature_as_of`` kwarg lets the caller check per-column
        timestamps (the ``ml.feature_store`` schema shape) rather than
        per-row timestamps. Any column whose as_of > decision_time
        fires."""
        # 4 columns of features, each with an as_of timestamp.
        features = [[0.1, 0.2, 0.3, 0.4]]
        feature_as_of = [10.0, 20.0, 30.0, 40.0]
        prediction_time = 25.0

        finding = BiasDetector.detect_look_ahead_bias(
            features, None, prediction_time, feature_as_of=feature_as_of,
        )

        assert finding is not None
        assert finding.rule == "BL_01"
        # Two columns have as_of > 25.0: 30.0 and 40.0.
        assert finding.detail["n_offenders"] == 2
        assert finding.detail["first_offender_index"] == 2

    def test_returns_none_for_empty_inputs(self) -> None:
        """Empty features / timestamps return ``None`` (no false positive
        on a degenerate input)."""
        assert BiasDetector.detect_look_ahead_bias([], [], 100.0) is None
        assert BiasDetector.detect_look_ahead_bias(None, None, 100.0) is None

    def test_reports_shape_mismatch_when_features_and_timestamps_differ(self) -> None:
        """When the feature matrix has N rows but the timestamps array
        has M != N entries, the check is ambiguous — the detector must
        fire a CRITICAL finding explaining the shape mismatch (rather
        than silently no-op'ing)."""
        features = [[0.1], [0.2], [0.3]]            # 3 rows
        timestamps = [10.0, 20.0, 30.0, 40.0]         # 4 entries
        finding = BiasDetector.detect_look_ahead_bias(
            features, timestamps, prediction_time=100.0,
        )
        assert finding is not None
        assert finding.rule == "BL_01"
        assert "shape mismatch" in finding.evidence.lower()


# ════════════════════════════════════════════════════════════════════════════
# (2) BL_02 — detect_data_leakage
# ════════════════════════════════════════════════════════════════════════════


class TestDetectDataLeakage:
    """``BiasDetector.detect_data_leakage`` — BL_02."""

    def test_fires_when_train_and_test_overlap(self) -> None:
        """A non-empty intersection between train_indices and
        test_indices is a data leakage violation. The finding must
        carry ``rule='BL_02'``, ``severity='critical'``, the overlap
        count, and a sample of the overlapping ids."""
        train = [0, 1, 2, 3, 4, 5]
        test = [4, 5, 6, 7, 8, 9]  # overlap: 4, 5

        finding = BiasDetector.detect_data_leakage(train, test)

        assert finding is not None
        assert finding.rule == "BL_02"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.detail["n_overlap"] == 2
        assert finding.detail["train_size"] == 6
        assert finding.detail["test_size"] == 6
        assert set(finding.detail["sample_overlap"]) == {4, 5}

    def test_returns_none_when_partitions_are_disjoint(self) -> None:
        """Disjoint train / test index sets must return ``None``."""
        train = [0, 1, 2, 3]
        test = [4, 5, 6, 7]
        assert BiasDetector.detect_data_leakage(train, test) is None

    def test_handles_string_and_composite_ids(self) -> None:
        """The detector must accept any hashable id type — string
        token_ids, composite ``(timestamp, token_id)`` tuples, etc."""
        train = ["alpha", "beta", ("ts1", "tok1")]
        test = ["beta", "gamma", ("ts1", "tok1")]  # overlap: "beta", ("ts1","tok1")

        finding = BiasDetector.detect_data_leakage(train, test)
        assert finding is not None
        assert finding.detail["n_overlap"] == 2

    def test_returns_none_for_empty_inputs(self) -> None:
        """Empty train or test sets return ``None`` (no false positive)."""
        assert BiasDetector.detect_data_leakage([], [1, 2, 3]) is None
        assert BiasDetector.detect_data_leakage([1, 2, 3], []) is None
        assert BiasDetector.detect_data_leakage(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (3) BL_03 — detect_optimistic_fills
# ════════════════════════════════════════════════════════════════════════════


class TestDetectOptimisticFills:
    """``BiasDetector.detect_optimistic_fills`` — BL_03."""

    def test_fires_when_buy_fill_below_best_bid(self) -> None:
        """A BUY fill below the period ``best_bid`` is structurally
        impossible in a CLOB (you'd have to cross the spread to buy
        at the ask). The finding must list the offending trade index
        and the fill / bid / ask prices in ``detail.offenders``."""
        trades = [
            {"action": "BUY", "price": 0.40, "timestamp": 100.0, "token_id": "T1"},
            {"action": "SELL", "price": 0.55, "timestamp": 200.0, "token_id": "T1"},
        ]
        books = [
            {"timestamp": 100.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
            {"timestamp": 200.0, "token_id": "T1", "best_bid": 0.55, "best_ask": 0.60},
        ]

        finding = BiasDetector.detect_optimistic_fills(trades, books)

        assert finding is not None
        assert finding.rule == "BL_03"
        assert finding.severity == SEVERITY_WARNING
        assert finding.detail["n_offenders"] == 1
        assert finding.detail["offenders"][0]["trade_index"] == 0
        assert finding.detail["offenders"][0]["fill_price"] == 0.40
        assert finding.detail["offenders"][0]["best_bid"] == 0.50
        assert "cannot buy below bid" in finding.detail["offenders"][0]["reason"]

    def test_fires_when_sell_fill_above_best_ask(self) -> None:
        """A SELL fill above the period ``best_ask`` is symmetric —
        structurally impossible without crossing the spread."""
        trades = [
            {"action": "BUY", "price": 0.55, "timestamp": 100.0, "token_id": "T1"},
            {"action": "SELL", "price": 0.65, "timestamp": 200.0, "token_id": "T1"},
        ]
        books = [
            {"timestamp": 100.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
            {"timestamp": 200.0, "token_id": "T1", "best_bid": 0.55, "best_ask": 0.60},
        ]

        finding = BiasDetector.detect_optimistic_fills(trades, books)

        assert finding is not None
        assert finding.detail["n_offenders"] == 1
        assert finding.detail["offenders"][0]["trade_index"] == 1
        assert "cannot sell above ask" in finding.detail["offenders"][0]["reason"]

    def test_returns_none_when_all_fills_achievable(self) -> None:
        """A BUY at the ask + SELL at the bid (the realistic
        crossing-spread fill pattern) must NOT fire a violation."""
        trades = [
            {"action": "BUY", "price": 0.55, "timestamp": 100.0, "token_id": "T1"},
            {"action": "SELL", "price": 0.50, "timestamp": 200.0, "token_id": "T1"},
        ]
        books = [
            {"timestamp": 100.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
            {"timestamp": 200.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
        ]
        assert BiasDetector.detect_optimistic_fills(trades, books) is None

    def test_returns_none_when_no_order_books_supplied(self) -> None:
        """Without the order-book snapshots the check is meaningless —
        return ``None`` rather than fire a false positive."""
        trades = [{"action": "BUY", "price": 0.40, "timestamp": 100.0}]
        assert BiasDetector.detect_optimistic_fills(trades, None) is None
        assert BiasDetector.detect_optimistic_fills(trades, []) is None

    def test_returns_none_for_empty_trades(self) -> None:
        """Empty trades list returns ``None`` (no false positive)."""
        assert BiasDetector.detect_optimistic_fills([], []) is None
        assert BiasDetector.detect_optimistic_fills(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (4) BL_04 — detect_future_information
# ════════════════════════════════════════════════════════════════════════════


class TestDetectFutureInformation:
    """``BiasDetector.detect_future_information`` — BL_04."""

    def test_fires_when_any_feature_column_as_of_exceeds_decision_time(self) -> None:
        features = [[0.1, 0.2, 0.3, 0.4]]
        feature_ts = [10.0, 20.0, 30.0, 40.0]
        decision_time = 25.0

        finding = BiasDetector.detect_future_information(
            features, feature_ts, decision_time,
        )

        assert finding is not None
        assert finding.rule == "BL_04"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.detail["n_offenders"] == 2
        assert finding.detail["first_offender_column"] == 2

    def test_returns_none_when_all_as_of_within_decision_time(self) -> None:
        features = [[0.1, 0.2, 0.3, 0.4]]
        feature_ts = [10.0, 20.0, 30.0, 40.0]
        # Decision time is after every as_of — no future information.
        finding = BiasDetector.detect_future_information(
            features, feature_ts, decision_time=50.0,
        )
        assert finding is None

    def test_reports_shape_mismatch(self) -> None:
        """A feature vector with N columns but an as_of array of M != N
        entries must fire a CRITICAL finding describing the mismatch."""
        features = [[0.1, 0.2, 0.3]]
        feature_ts = [10.0, 20.0, 30.0, 40.0]
        finding = BiasDetector.detect_future_information(
            features, feature_ts, decision_time=100.0,
        )
        assert finding is not None
        assert finding.rule == "BL_04"
        assert "shape mismatch" in finding.evidence.lower()


# ════════════════════════════════════════════════════════════════════════════
# (5) BL_05 — detect_survivorship_bias
# ════════════════════════════════════════════════════════════════════════════


class TestDetectSurvivorshipBias:
    """``BiasDetector.detect_survivorship_bias`` — BL_05."""

    def test_fires_when_tested_subset_is_small_fraction_of_universe(self) -> None:
        """When the tested set covers < 50% (default threshold) of the
        all-markets universe, the backtest universe was curated —
        likely only markets that survived / resolved YES were kept."""
        tested = {"A", "B", "C", "D"}                  # 4 markets tested
        all_markets = {f"M{i}" for i in range(20)}     # 20 markets total
        all_markets.update(tested)

        finding = BiasDetector.detect_survivorship_bias(tested, all_markets)

        assert finding is not None
        assert finding.rule == "BL_05"
        assert finding.severity == SEVERITY_WARNING
        # 4 tested / 24 total = ~16.7% < 50%.
        assert finding.detail["n_tested"] == 4
        assert finding.detail["n_all"] == 24
        assert finding.detail["ratio"] == pytest.approx(4 / 24, abs=1e-3)
        assert finding.detail["ratio"] < 0.5

    def test_returns_none_when_tested_covers_majority_of_universe(self) -> None:
        """When the tested set covers >= 50% (default threshold) of the
        universe, no survivorship-bias violation fires."""
        tested = {f"M{i}" for i in range(15)}          # 15 markets tested
        all_markets = {f"M{i}" for i in range(20)}     # 20 markets total

        finding = BiasDetector.detect_survivorship_bias(tested, all_markets)
        assert finding is None

    def test_respects_custom_threshold(self) -> None:
        """A caller can tighten the threshold (``min_tested_ratio=0.9``)
        to flag any backtest covering < 90% of the universe."""
        tested = {f"M{i}" for i in range(15)}
        all_markets = {f"M{i}" for i in range(20)}
        finding = BiasDetector.detect_survivorship_bias(
            tested, all_markets, min_tested_ratio=0.9,
        )
        assert finding is not None
        assert finding.detail["threshold"] == 0.9

    def test_returns_none_for_empty_universe(self) -> None:
        """An empty all_markets set returns ``None`` (no universe to
        compare against)."""
        assert BiasDetector.detect_survivorship_bias({"A"}, set()) is None
        assert BiasDetector.detect_survivorship_bias(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (6) BL_06 — detect_selection_bias
# ════════════════════════════════════════════════════════════════════════════


class TestDetectSelectionBias:
    """``BiasDetector.detect_selection_bias`` — BL_06."""

    @pytest.mark.parametrize("strategy_id", [
        "winners_only",
        "best_performers_v2",
        "cherry_pick_top",
        "curated_subset",
        "hand_picked_set",
        "top_n_universe",
    ])
    def test_fires_on_positive_selection_markers(self, strategy_id: str) -> None:
        """Each default marker must trigger the finding."""
        finding = BiasDetector.detect_selection_bias(strategy_id)
        assert finding is not None
        assert finding.rule == "BL_06"
        assert finding.severity == SEVERITY_WARNING
        assert strategy_id in finding.detail["strategy_id"]

    def test_returns_none_for_neutral_strategy_id(self) -> None:
        """A neutral strategy_id (no markers) returns ``None``."""
        assert BiasDetector.detect_selection_bias("mm_v1") is None
        assert BiasDetector.detect_selection_bias("ml_ensemble") is None
        assert BiasDetector.detect_selection_bias("") is None
        assert BiasDetector.detect_selection_bias(None) is None

    def test_match_is_case_insensitive(self) -> None:
        """The marker match is case-insensitive — ``WINNERS_ONLY`` and
        ``winners_only`` both fire."""
        assert BiasDetector.detect_selection_bias("WINNERS_ONLY") is not None
        assert BiasDetector.detect_selection_bias("Winners_Only") is not None


# ════════════════════════════════════════════════════════════════════════════
# (7) BL_07 — detect_hindsight_filtering
# ════════════════════════════════════════════════════════════════════════════


class TestDetectHindsightFiltering:
    """``BiasDetector.detect_hindsight_filtering`` — BL_07."""

    def test_fires_when_match_rate_exceeds_threshold(self) -> None:
        """50 trades where the signal matches the outcome 49/50 times
        (98% match rate, > 95% threshold, n >= 30 minimum) must fire
        a CRITICAL finding."""
        # 50 trades: 49 perfect matches, 1 mismatch.
        predictions = [0.9] * 49 + [0.6]    # all bet YES
        outcomes = [1.0] * 49 + [0.0]       # 49 win, 1 loss → 98% match

        finding = BiasDetector.detect_hindsight_filtering(predictions, outcomes)

        assert finding is not None
        assert finding.rule == "BL_07"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.detail["n_trades"] == 50
        assert finding.detail["n_matches"] == 49
        assert finding.detail["match_rate"] == pytest.approx(0.98, abs=1e-3)

    def test_returns_none_when_match_rate_below_threshold(self) -> None:
        """A 70% match rate over 50 trades is below the 95% threshold —
        no violation fires."""
        predictions = [0.9] * 35 + [0.6] * 15
        outcomes = [1.0] * 35 + [0.0] * 15  # 35 matches / 50 = 70%
        finding = BiasDetector.detect_hindsight_filtering(predictions, outcomes)
        assert finding is None

    def test_returns_none_when_sample_too_small(self) -> None:
        """Below the ``min_trades=30`` threshold, the match rate is not
        statistically meaningful — no violation fires."""
        # 25 trades, all perfect matches — still too few to fire.
        predictions = [0.9] * 25
        outcomes = [1.0] * 25
        finding = BiasDetector.detect_hindsight_filtering(predictions, outcomes)
        assert finding is None

    def test_returns_none_for_empty_inputs(self) -> None:
        assert BiasDetector.detect_hindsight_filtering([], []) is None
        assert BiasDetector.detect_hindsight_filtering(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (8) BL_08 — detect_timestamp_leakage
# ════════════════════════════════════════════════════════════════════════════


class TestDetectTimestampLeakage:
    """``BiasDetector.detect_timestamp_leakage`` — BL_08."""

    def test_fires_when_train_max_exceeds_test_min(self) -> None:
        """When ``max(train_timestamps) >= min(test_timestamps)``, the
        windows overlap in time and a label / feature computed on the
        train tail could leak into the test head."""
        train_ts = [10.0, 20.0, 30.0, 40.0]  # max = 40
        test_ts = [35.0, 45.0, 55.0, 65.0]   # min = 35  → overlap

        finding = BiasDetector.detect_timestamp_leakage(train_ts, test_ts)

        assert finding is not None
        assert finding.rule == "BL_08"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.detail["train_max"] == 40.0
        assert finding.detail["test_min"] == 35.0
        assert finding.detail["overlap_width"] == pytest.approx(5.0, abs=1e-6)

    def test_returns_none_when_windows_disjoint(self) -> None:
        """When ``max(train) < min(test)`` (with tolerance), the
        windows are time-disjoint — no leakage."""
        train_ts = [10.0, 20.0, 30.0]
        test_ts = [40.0, 50.0, 60.0]
        finding = BiasDetector.detect_timestamp_leakage(train_ts, test_ts)
        assert finding is None

    def test_returns_none_for_empty_inputs(self) -> None:
        assert BiasDetector.detect_timestamp_leakage([], [1.0, 2.0]) is None
        assert BiasDetector.detect_timestamp_leakage([1.0, 2.0], []) is None
        assert BiasDetector.detect_timestamp_leakage(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (9) BL_09 — detect_duplicate_participation
# ════════════════════════════════════════════════════════════════════════════


class TestDetectDuplicateParticipation:
    """``BiasDetector.detect_duplicate_participation`` — BL_09."""

    def test_fires_when_same_record_in_both_sets(self) -> None:
        """Record-content overlap (not index overlap) between train and
        test sets is a duplicate-participation violation."""
        train = [("A", 1.0), ("B", 2.0), ("C", 3.0)]
        test = [("C", 3.0), ("D", 4.0), ("E", 5.0)]  # overlap: ("C", 3.0)

        finding = BiasDetector.detect_duplicate_participation(train, test)

        assert finding is not None
        assert finding.rule == "BL_09"
        assert finding.severity == SEVERITY_CRITICAL
        assert finding.detail["n_overlap"] == 1
        assert finding.detail["train_size"] == 3
        assert finding.detail["test_size"] == 3
        assert ("C", 3.0) in finding.detail["sample_overlap"]

    def test_returns_none_when_record_disjoint(self) -> None:
        """Record-disjoint train / test sets return ``None``."""
        train = [("A", 1.0), ("B", 2.0)]
        test = [("C", 3.0), ("D", 4.0)]
        assert BiasDetector.detect_duplicate_participation(train, test) is None

    def test_returns_none_for_empty_inputs(self) -> None:
        assert BiasDetector.detect_duplicate_participation([], [1, 2, 3]) is None
        assert BiasDetector.detect_duplicate_participation(None, None) is None


# ════════════════════════════════════════════════════════════════════════════
# (10) BL_10 — detect_unrealistic_capital_reuse
# ════════════════════════════════════════════════════════════════════════════


class TestDetectUnrealisticCapitalReuse:
    """``BiasDetector.detect_unrealistic_capital_reuse`` — BL_10."""

    def test_fires_when_second_buy_arrives_before_first_settles(self) -> None:
        """Two BUYs within the settlement window (default 8h) where
        neither has been closed by a SELL — capital was implicitly
        reused before settlement."""
        # 1h between BUYs — well inside the default 8h settlement window.
        trades = [
            {"action": "BUY", "timestamp": 100.0, "size": 1.0},
            {"action": "BUY", "timestamp": 4600.0, "size": 1.0},  # +1h
        ]
        finding = BiasDetector.detect_unrealistic_capital_reuse(trades)

        assert finding is not None
        assert finding.rule == "BL_10"
        assert finding.severity == SEVERITY_WARNING
        assert finding.detail["n_offenders"] == 1
        assert finding.detail["offenders"][0]["trade_index"] == 1
        assert finding.detail["offenders"][0]["prior_buy_index"] == 0
        assert finding.detail["offenders"][0]["gap_seconds"] == pytest.approx(4500.0)

    def test_returns_none_when_buys_separated_by_more_than_settlement(self) -> None:
        """When the second BUY arrives after the settlement window has
        elapsed, no capital-reuse violation fires."""
        # 10h between BUYs — outside the default 8h settlement window.
        trades = [
            {"action": "BUY", "timestamp": 100.0, "size": 1.0},
            {"action": "BUY", "timestamp": 100.0 + 10 * 3600.0, "size": 1.0},
        ]
        finding = BiasDetector.detect_unrealistic_capital_reuse(trades)
        assert finding is None

    def test_returns_none_when_prior_buy_closed_before_second_buy(self) -> None:
        """When a SELL closes the prior BUY before the second BUY
        arrives, no capital-reuse violation fires — the capital was
        released by the SELL."""
        trades = [
            {"action": "BUY", "timestamp": 100.0, "size": 1.0},
            {"action": "SELL", "timestamp": 200.0, "size": 1.0},  # close
            {"action": "BUY", "timestamp": 300.0, "size": 1.0},  # fresh entry
        ]
        finding = BiasDetector.detect_unrealistic_capital_reuse(trades)
        assert finding is None

    def test_respects_custom_settlement_window(self) -> None:
        """A caller can tighten the settlement window (e.g. 60s for
        minute-resolution markets)."""
        trades = [
            {"action": "BUY", "timestamp": 100.0, "size": 1.0},
            {"action": "BUY", "timestamp": 130.0, "size": 1.0},  # +30s
        ]
        # Default window (8h) would NOT fire (30s << 8h, BUT capital was
        # reused before settlement of the first BUY — the default window
        # treats +30s as within settlement, so fires).
        finding_default = BiasDetector.detect_unrealistic_capital_reuse(trades)
        assert finding_default is not None

        # A 20s window — +30s exceeds the window → no violation.
        finding_tight = BiasDetector.detect_unrealistic_capital_reuse(
            trades, settlement_seconds=20.0,
        )
        assert finding_tight is None

    def test_returns_none_for_empty_trades(self) -> None:
        assert BiasDetector.detect_unrealistic_capital_reuse([]) is None
        assert BiasDetector.detect_unrealistic_capital_reuse(None) is None


# ════════════════════════════════════════════════════════════════════════════
# (11) BiasDetector.analyze + BiasReport aggregation
# ════════════════════════════════════════════════════════════════════════════


class TestBiasDetectorAnalyze:
    """``BiasDetector.analyze`` aggregates findings from every enabled
    rule and populates the per-severity counters on :class:`BiasReport`."""

    def test_analyze_aggregates_findings_from_multiple_rules(self) -> None:
        """A backtest payload that triggers multiple rules must
        produce a :class:`BiasReport` with one finding per fired rule,
        and the per-severity counters must reflect the union."""
        # Craft a backtest payload that fires BL_06 (selection bias via
        # the strategy_id) + BL_10 (capital reuse via two close BUYs).
        backtest = {
            "strategy_id": "winners_only_strategy",
            "trades": [
                {"action": "BUY", "timestamp": 100.0, "size": 1.0},
                {"action": "BUY", "timestamp": 200.0, "size": 1.0},  # +100s
            ],
        }

        report = bias_detector.analyze(backtest)

        assert isinstance(report, BiasReport)
        rules_fired = {f.rule for f in report.findings}
        assert "BL_06" in rules_fired
        assert "BL_10" in rules_fired
        # BL_06 is a warning; BL_10 is a warning. has_critical should be False.
        assert report.has_critical is False
        assert report.n_warning >= 2
        assert report.n_critical == 0
        assert report.n_total >= 2

    def test_analyze_populates_critical_subset_correctly(self) -> None:
        """When at least one finding is critical, ``has_critical`` must
        be True and ``critical_findings`` must list only the critical
        subset (not the warnings)."""
        backtest = {
            "strategy_id": "mm_neutral",  # neutral — BL_06 doesn't fire
            "trades": [
                {"action": "BUY", "timestamp": 100.0, "size": 1.0},
                {"action": "BUY", "timestamp": 200.0, "size": 1.0},
            ],
        }
        # Add explicit train/test indices that overlap → BL_02 fires
        # as critical.
        report = bias_detector.analyze(
            backtest,
            train_indices=[0, 1, 2, 3],
            test_indices=[3, 4, 5, 6],  # overlap on index 3
        )
        rules_fired = {f.rule for f in report.findings}
        assert "BL_02" in rules_fired
        assert report.has_critical is True
        assert any(f.rule == "BL_02" for f in report.critical_findings)
        # BL_10 still fires as warning, but is NOT in critical_findings.
        assert all(f.severity == SEVERITY_CRITICAL for f in report.critical_findings)

    def test_analyze_returns_empty_report_for_clean_backtest(self) -> None:
        """A clean backtest (no bias markers, achievable fills, no
        capital reuse, no train/test overlap) returns an empty report
        with ``has_critical=False``."""
        backtest = {
            "strategy_id": "mm_v1",  # neutral
            "trades": [
                {"action": "BUY", "timestamp": 100.0, "size": 1.0, "price": 0.55, "token_id": "T1"},
                {"action": "SELL", "timestamp": 100.0 + 10 * 3600.0, "size": 1.0, "price": 0.50, "token_id": "T1"},
            ],
            "order_books": [
                {"timestamp": 100.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
                {"timestamp": 100.0 + 10 * 3600.0, "token_id": "T1", "best_bid": 0.50, "best_ask": 0.55},
            ],
        }
        report = bias_detector.analyze(backtest)
        assert report.findings == []
        assert report.has_critical is False
        assert report.n_total == 0

    def test_analyze_never_raises_on_malformed_input(self) -> None:
        """``analyze`` must never raise — even on a malformed input
        (missing keys, wrong types, None values). Every rule is
        individually guarded so a single bad field doesn't blow up
        the whole report."""
        # Pass a non-dict, non-to_dict-able input.
        report = bias_detector.analyze(None)  # type: ignore[arg-type]
        assert isinstance(report, BiasReport)
        assert report.findings == []

        # Pass a list (also not a dict — should be coerced to {} via
        # the dict() fallback in analyze).
        report2 = bias_detector.analyze(["not", "a", "dict"])  # type: ignore[arg-type]
        assert isinstance(report2, BiasReport)

    def test_analyze_respects_rules_subset_constructor(self) -> None:
        """The ``rules=`` constructor arg lets a caller restrict the
        scan to a subset of rules — only the listed rules fire."""
        # Restrict to BL_06 + BL_10 only.
        detector = BiasDetector(rules=["BL_06", "BL_10"])
        backtest = {
            "strategy_id": "winners_only",
            "trades": [
                {"action": "BUY", "timestamp": 100.0, "size": 1.0},
                {"action": "BUY", "timestamp": 200.0, "size": 1.0},
            ],
        }
        report = detector.analyze(backtest)
        rules_fired = {f.rule for f in report.findings}
        assert rules_fired == {"BL_06", "BL_10"}

    def test_constructor_rejects_unknown_rule_ids(self) -> None:
        """Passing an unknown rule id to the constructor must raise
        ValueError."""
        with pytest.raises(ValueError, match="Unknown rule"):
            BiasDetector(rules=["BL_99"])


# ════════════════════════════════════════════════════════════════════════════
# (12) API route — POST /api/backtest/bias-check
# ════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def bias_app():
    """Fresh ``FastAPI`` app with only the bias-check route registered.

    Mirrors the ``oos_app`` fixture in ``tests/test_out_of_sample.py`` —
    a fresh FastAPI app with only the bias_detector routes registered,
    so the route definitions exercised here are byte-identical to what
    the live server exposes, without the bearer-token auth middleware.
    """
    from fastapi import FastAPI

    app = FastAPI()
    register_routes(app)
    return app


class TestBiasCheckRoute:
    """``POST /api/backtest/bias-check`` HTTP surface."""

    def test_returns_200_with_full_payload_on_clean_backtest(
        self, bias_app,
    ) -> None:
        """A well-formed request with a clean backtest must return 200
        with the full :class:`BiasReport` payload (``findings`` /
        ``summary`` / ``has_critical`` / ``critical_findings``)."""
        from fastapi.testclient import TestClient

        client = TestClient(bias_app)
        resp = client.post("/api/backtest/bias-check", json={
            "backtest_result": {
                "strategy_id": "mm_v1",
                "trades": [
                    {"action": "BUY", "timestamp": 100.0, "size": 1.0, "price": 0.55},
                    {"action": "SELL", "timestamp": 100.0 + 10 * 3600.0, "size": 1.0, "price": 0.50},
                ],
            },
        })

        assert resp.status_code == 200, (
            f"expected 200, got {resp.status_code}: {resp.text}"
        )
        payload = resp.json()
        # Top-level keys.
        for key in ("findings", "summary", "has_critical", "critical_findings"):
            assert key in payload, f"missing top-level key {key!r}"
        # Clean backtest → no findings.
        assert payload["findings"] == []
        assert payload["has_critical"] is False
        assert payload["critical_findings"] == []
        # Summary fields.
        summary = payload["summary"]
        assert summary["n_total"] == 0
        assert summary["n_critical"] == 0
        assert summary["has_critical"] is False
        assert "checked_at" in summary

    def test_returns_200_with_findings_on_biased_backtest(
        self, bias_app,
    ) -> None:
        """A backtest that triggers BL_06 (selection bias) + BL_10
        (capital reuse) must return 200 with both findings listed
        and ``has_critical=False`` (both are warnings)."""
        from fastapi.testclient import TestClient

        client = TestClient(bias_app)
        resp = client.post("/api/backtest/bias-check", json={
            "backtest_result": {
                "strategy_id": "winners_only_strategy",
                "trades": [
                    {"action": "BUY", "timestamp": 100.0, "size": 1.0},
                    {"action": "BUY", "timestamp": 200.0, "size": 1.0},
                ],
            },
        })

        assert resp.status_code == 200
        payload = resp.json()
        rules_fired = {f["rule"] for f in payload["findings"]}
        assert "BL_06" in rules_fired
        assert "BL_10" in rules_fired
        assert payload["has_critical"] is False
        assert payload["summary"]["n_warning"] >= 2

    def test_returns_200_with_critical_finding_on_data_leakage(
        self, bias_app,
    ) -> None:
        """When the request supplies train/test indices that overlap,
        the response must carry the BL_02 critical finding AND
        ``has_critical=True``."""
        from fastapi.testclient import TestClient

        client = TestClient(bias_app)
        resp = client.post("/api/backtest/bias-check", json={
            "backtest_result": {
                "strategy_id": "mm_v1",
                "trades": [],
            },
            "train_indices": [0, 1, 2, 3],
            "test_indices": [3, 4, 5, 6],  # overlap on index 3
        })

        assert resp.status_code == 200
        payload = resp.json()
        rules_fired = {f["rule"] for f in payload["findings"]}
        assert "BL_02" in rules_fired
        assert payload["has_critical"] is True
        assert any(
            f["rule"] == "BL_02" and f["severity"] == "critical"
            for f in payload["critical_findings"]
        )

    def test_returns_422_when_backtest_result_missing(self, bias_app) -> None:
        """A request body without the ``backtest_result`` field must
        return 422 (Unprocessable Entity) — the field is required."""
        from fastapi.testclient import TestClient

        client = TestClient(bias_app)
        resp = client.post("/api/backtest/bias-check", json={
            "train_indices": [0, 1, 2],
            "test_indices": [3, 4, 5],
        })

        assert resp.status_code == 422
        detail = resp.json().get("detail", "")
        assert "backtest_result" in detail

    def test_returns_422_when_body_is_not_a_dict(self, bias_app) -> None:
        """A non-JSON-object body (e.g. a bare JSON array) must return
        422 — the handler expects a JSON object with the documented
        fields."""
        from fastapi.testclient import TestClient

        client = TestClient(bias_app)
        # Pass a bare JSON array — FastAPI's pydantic body coercion
        # should reject it.
        resp = client.post("/api/backtest/bias-check", json=[1, 2, 3])

        # FastAPI returns 422 for body validation errors. The exact
        # status code may be 422 OR (in some FastAPI versions) 400 —
        # accept either.
        assert resp.status_code in (400, 422), (
            f"expected 400/422 on non-object body, got {resp.status_code}: "
            f"{resp.text}"
        )

    def test_route_is_registered_under_backtesting_tag(self, bias_app) -> None:
        """The OpenAPI schema must list the route under the
        ``backtesting`` tag so it's grouped with the other
        ``/api/backtest/*`` routes in the swagger UI."""
        schema = bias_app.openapi()
        path_spec = schema.get("paths", {}).get("/api/backtest/bias-check", {})
        post_spec = path_spec.get("post", {})
        tags = post_spec.get("tags", [])
        assert "backtesting" in tags, (
            f"route should be tagged 'backtesting'; got tags={tags!r}"
        )


# ════════════════════════════════════════════════════════════════════════════
# (13) End-to-end — HistoricalReplayEngine.replay populates bias_report
# ════════════════════════════════════════════════════════════════════════════


class TestHistoricalReplayBiasReportIntegration:
    """The ``HistoricalReplayEngine.replay`` end-to-end path must
    populate ``ReplayResult.bias_report`` after every run, so callers
    don't need to invoke the detector separately.

    The replay engine is the canonical integration point — every
    historical-replay backtest automatically surfaces its bias
    findings on the result. Mirrors the post-backtest hook called out
    in the W37-3 task spec.
    """

    def test_replay_result_carries_bias_report_field(self) -> None:
        """``ReplayResult.bias_report`` is always present (default ``{}``
        when no scan ran). The replay() method must populate it with
        a non-empty dict (the BiasReport.to_dict() payload)."""
        import sqlite3

        from backtesting.historical_replay import (
            HistoricalReplayEngine,
            HistoricalSnapshot,
            SimpleStrategy,
        )

        # Use a fresh in-memory SQLite DB so the test doesn't depend on
        # any persisted state.
        db_path = str(_TMP_ROOT / "bias_replay_test.db")
        # Clean any prior DB so the test is deterministic.
        if Path(db_path).exists():
            Path(db_path).unlink()

        # Seed a small snapshot series directly into the SQLite
        # ``market_snapshots`` table (the schema the engine queries).
        token_id = "TKN_BIAS_TEST"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS market_snapshots (
                    timestamp REAL NOT NULL,
                    token_id TEXT NOT NULL,
                    best_bid REAL,
                    best_ask REAL,
                    mid REAL,
                    spread REAL,
                    volume_24h REAL,
                    liquidity REAL
                )
            """)
            # 25 snapshots with a clear dip so SimpleStrategy fires
            # BUY at the trough + SELL at the recovery.
            base_ts = 1_000_000.0
            mids = [0.50] * 5 + [0.45, 0.40, 0.35, 0.30, 0.35,
                                  0.40, 0.45, 0.50] + [0.50] * 7
            for i, m in enumerate(mids):
                conn.execute(
                    "INSERT INTO market_snapshots (timestamp, token_id, "
                    "best_bid, best_ask, mid, spread, volume_24h, liquidity) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (base_ts + i, token_id, m - 0.005, m + 0.005, m, 0.01, 100.0, 1000.0),
                )
            conn.commit()

        engine = HistoricalReplayEngine(db_path)
        strategy = SimpleStrategy(window=5, threshold=0.01)

        result = engine.replay(
            token_id=token_id,
            strategy=strategy,
            start_time=base_ts - 1.0,
            end_time=base_ts + 100.0,
            initial_capital=100.0,
        )

        # bias_report must be a non-empty dict (the BiasReport.to_dict()
        # payload — at minimum it carries ``findings`` and ``summary``).
        assert isinstance(result.bias_report, dict)
        assert "findings" in result.bias_report
        assert "summary" in result.bias_report
        assert "has_critical" in result.bias_report
        # Even a clean backtest should populate summary (with n_total=0).
        assert result.bias_report["summary"]["n_total"] >= 0
