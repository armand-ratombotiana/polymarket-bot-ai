"""
Unit + API tests for the W17-1 market sentiment analyzer.

Five test classes:

  (1) ``TestAnalyzeNews`` — keyword-based news sentiment analyzer
      coverage. Covers bullish / bearish / neutral text, the
      confidence scaling (more keywords → higher confidence), and
      the metadata envelope.

  (2) ``TestAnalyzePriceAction`` — momentum-based sentiment
      analyzer coverage. Covers bullish / bearish / flat price
      series, the ±5 % normalisation clamp, sample-count-driven
      confidence scaling, the empty / single-sample edge case, and
      the divide-by-zero guard when ``past`` price is 0.

  (3) ``TestAnalyzeVolume`` — volume-trend analyzer coverage.
      Covers the unusual-volume (≥ 2× past) positive signal, the
      dry-up (≤ 0.5× past) negative signal, the neutral middle
      band, sample-count-driven confidence scaling, the empty /
      single-sample edge case, and the divide-by-zero guard when
      past average is 0.

  (4) ``TestAggregate`` — the weighted aggregation pipeline.
      Covers: empty token (zeroed envelope), single signal,
      confidence-weighted aggregation, recency decay, breakdown-by-
      source averaging, the persisted aggregated_sentiment row, and
      the ``get_all_sentiment`` listing (sorted by overall score
      descending).

  (5) ``TestTrendDetection`` — the improving / declining / stable
      trend label. Covers all three branches plus the < 4 signal
      short-circuit (always ``stable``).

  (6) ``TestSentimentRoutes`` — HTTP-level coverage of the three
      ``/api/sentiment*`` endpoints via ``fastapi.testclient.
      TestClient`` against a fresh ``FastAPI`` app with only the
      sentiment routes registered. Covers: POST analyze (happy
      path + empty-text 422 + missing-field 422), GET single-token
      (with + without prior signals), GET list (sorted by score).

Approach
~~~~~~~~
Every test constructs a FRESH ``SentimentAnalyzer(tmp_path / ...)`` so
there is zero shared state with the module-level singleton
(``core.sentiment.sentiment_analyzer``) — mirrors the
``isolated_decision_ledger`` fixture in ``tests/conftest.py``. The
module-level singleton is constructed at import time against the
conftest-redirected ``SENTIMENT_DB_PATH`` so the import itself
succeeds even when ``/app/data`` is read-only; the API-test class
patches the singleton's ``_db_path`` to a per-test ``tmp_path`` so a
prior test's recorded signals never leak into the next test's
``aggregate`` call.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core import sentiment as _sentiment_module
from core.sentiment import (
    AggregatedSentiment,
    SentimentAnalyzer,
    SentimentSignal,
    register_routes,
)

# ── (1) Unit tests: analyze_news ────────────────────────────────────────────


class TestAnalyzeNews:
    """Direct coverage of :meth:`SentimentAnalyzer.analyze_news`."""

    def test_bullish_text_yields_positive_score(self, tmp_path: Path):
        """A headline with only bullish keywords scores +1.0."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_news(
            "Bitcoin surges on bullish rally as prices rally and bulls soar",
            "btc",
        )
        assert signal.source == "news"
        assert signal.token_id == "btc"
        assert signal.score == pytest.approx(1.0)
        assert signal.confidence > 0.0
        assert signal.metadata["bull_count"] >= 4
        assert signal.metadata["bear_count"] == 0

    def test_bearish_text_yields_negative_score(self, tmp_path: Path):
        """A headline with only bearish keywords scores -1.0."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_news(
            "Markets crash as bearish outlook plummets and losses mount",
            "btc",
        )
        assert signal.score == pytest.approx(-1.0)
        assert signal.confidence > 0.0
        assert signal.metadata["bear_count"] >= 3
        assert signal.metadata["bull_count"] == 0

    def test_neutral_text_yields_zero_score(self, tmp_path: Path):
        """A headline with no sentiment keywords scores 0.0 at the
        minimum (0.1) confidence — the analyzer has no signal."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_news("Federal Reserve announces interest rate decision", "fed")
        assert signal.score == 0.0
        assert signal.confidence == pytest.approx(0.1)
        assert signal.metadata["bull_count"] == 0
        assert signal.metadata["bear_count"] == 0
        assert signal.metadata["text_length"] > 0

    def test_mixed_text_yields_signed_score(self, tmp_path: Path):
        """A headline with both bullish and bearish keywords yields a
        score strictly inside (-1.0, +1.0) — the difference normalised
        by total keyword hits. ``"rally"`` and ``"crash"`` both match
        (single keyword each) → score = (1 - 1) / 2 = 0.0."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_news("Bitcoin rally turns into crash", "btc")
        assert -1.0 < signal.score < 1.0
        assert signal.metadata["bull_count"] == 1  # "rally"
        assert signal.metadata["bear_count"] == 1  # "crash"
        assert signal.score == pytest.approx(0.0, abs=1e-6)

    def test_mixed_text_with_bull_majority_yields_positive_score(
        self, tmp_path: Path
    ):
        """A headline with more bullish keywords than bearish yields
        a positive score < 1.0 (not the saturated +1.0 of a pure-bull
        headline)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        # "surge" + "bullish" + ("bull" inside "bullish") = 3 bull hits,
        # "crash" = 1 bear hit. Score = (3-1) / 4 = 0.5.
        signal = sa.analyze_news(
            "Bitcoin surges on bullish news despite an early crash", "btc"
        )
        assert 0.0 < signal.score < 1.0
        assert signal.metadata["bull_count"] >= 2
        assert signal.metadata["bear_count"] >= 1
        assert signal.score == pytest.approx(0.5, abs=1e-6)

    def test_confidence_scales_with_keyword_density(self, tmp_path: Path):
        """More keyword hits → higher confidence, capped at 1.0."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        sparse = sa.analyze_news("Bitcoin surge", "btc")  # 1 keyword
        dense = sa.analyze_news(
            "Bitcoin surge rally gain bullish positive upside breakout support buy long",
            "btc",
        )
        assert dense.confidence > sparse.confidence
        assert dense.confidence == pytest.approx(1.0)  # 10+ keywords → capped

    def test_signal_records_underlying_counts(self, tmp_path: Path):
        """The metadata envelope exposes the underlying bull / bear
        counts + text length so the dashboard can render a
        "why this score" breakdown without re-running the scan."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        text = "Bitcoin surges on bullish news"
        signal = sa.analyze_news(text, "btc")
        assert signal.metadata["text_length"] == len(text)
        assert signal.metadata["bull_count"] >= 2
        assert signal.metadata["bear_count"] == 0


# ── (2) Unit tests: analyze_price_action ────────────────────────────────────


class TestAnalyzePriceAction:
    """Direct coverage of :meth:`SentimentAnalyzer.analyze_price_action`."""

    def test_bullish_price_series_yields_positive_score(self, tmp_path: Path):
        """A 5 % upward move clamps to +1.0 sentiment."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100, 102, 104, 106, 105], "tk")
        assert signal.source == "price"
        assert signal.token_id == "tk"
        assert signal.score > 0.0
        assert signal.confidence > 0.0
        assert signal.metadata["n_prices"] == 5

    def test_bearish_price_series_yields_negative_score(self, tmp_path: Path):
        """A 5 % downward move clamps to -1.0 sentiment."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100, 98, 96, 94, 95], "tk")
        assert signal.score < 0.0
        assert signal.metadata["price_change_pct"] < 0

    def test_flat_price_series_yields_zero_score(self, tmp_path: Path):
        """An unchanged series yields 0.0 sentiment."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100, 100, 100, 100, 100], "tk")
        assert signal.score == 0.0
        assert signal.metadata["price_change_pct"] == 0.0

    def test_score_clamped_at_plus_one_for_large_moves(self, tmp_path: Path):
        """A 20 % upward move is clamped to +1.0 (not +4.0)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100, 120], "tk")
        assert signal.score == pytest.approx(1.0)

    def test_score_clamped_at_minus_one_for_large_drops(self, tmp_path: Path):
        """A 50 % downward move is clamped to -1.0 (not -10.0)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100, 50], "tk")
        assert signal.score == pytest.approx(-1.0)

    def test_confidence_scales_with_sample_count(self, tmp_path: Path):
        """More samples → higher confidence, capped at 1.0 at 20 samples."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        short = sa.analyze_price_action([100, 105], "tk")
        long_ = sa.analyze_price_action([100] + [105] * 19, "tk")
        assert long_.confidence > short.confidence
        assert long_.confidence == pytest.approx(1.0)  # 20 samples → capped

    def test_empty_prices_yields_zero_signal(self, tmp_path: Path):
        """An empty price list yields a zeroed signal at the floor
        confidence."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([], "tk")
        assert signal.score == 0.0
        assert signal.confidence == 0.0
        assert signal.token_id == "tk"

    def test_single_price_yields_zero_signal(self, tmp_path: Path):
        """A single price (no momentum measurable) yields a zeroed
        signal — the analyzer refuses to extrapolate from one point."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([100], "tk")
        assert signal.score == 0.0
        assert signal.confidence == 0.0

    def test_zero_past_price_does_not_crash(self, tmp_path: Path):
        """A past price of 0 (degenerate / stale market) yields a
        zeroed signal — the divide-by-zero guard engages."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_price_action([0, 100], "tk")
        assert signal.score == 0.0


# ── (3) Unit tests: analyze_volume ──────────────────────────────────────────


class TestAnalyzeVolume:
    """Direct coverage of :meth:`SentimentAnalyzer.analyze_volume`."""

    def test_volume_spike_yields_mild_positive(self, tmp_path: Path):
        """Recent avg ≥ 2 × past avg → +0.3 (unusual conviction)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        # First 5 volumes low, last 5 volumes 5× higher → vol_ratio ≈ 5
        signal = sa.analyze_volume(
            [100, 100, 100, 100, 100, 500, 500, 500, 500, 500], "tk"
        )
        assert signal.source == "volume"
        assert signal.token_id == "tk"
        assert signal.score == pytest.approx(0.3)
        assert signal.metadata["vol_ratio"] >= 2.0

    def test_volume_dry_up_yields_mild_negative(self, tmp_path: Path):
        """Recent avg ≤ 0.5 × past avg → -0.3 (apathy / unwind)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_volume(
            [500, 500, 500, 500, 500, 100, 100, 100, 100, 100], "tk"
        )
        assert signal.score == pytest.approx(-0.3)
        assert signal.metadata["vol_ratio"] <= 0.5

    def test_neutral_volume_yields_zero_score(self, tmp_path: Path):
        """A volume series with no significant change yields 0.0 — the
        analyzer treats a flat volume profile as amplifying nothing."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_volume([100, 110, 105, 95, 100, 100, 110, 105, 95, 100], "tk")
        assert signal.score == 0.0
        assert 0.5 <= signal.metadata["vol_ratio"] <= 2.0

    def test_confidence_scales_with_sample_count(self, tmp_path: Path):
        """More samples → higher confidence, capped at 1.0 at 10 samples."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        short = sa.analyze_volume([100, 500], "tk")  # 2 samples
        long_ = sa.analyze_volume([100] * 5 + [500] * 5, "tk")  # 10 samples
        assert long_.confidence > short.confidence
        assert long_.confidence == pytest.approx(1.0)

    def test_empty_volumes_yields_zero_signal(self, tmp_path: Path):
        """An empty volume list yields a zeroed signal at the floor
        confidence."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_volume([], "tk")
        assert signal.score == 0.0
        assert signal.confidence == 0.0

    def test_single_volume_yields_zero_signal(self, tmp_path: Path):
        """A single volume sample (no trend measurable) yields a
        zeroed signal."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_volume([100], "tk")
        assert signal.score == 0.0
        assert signal.confidence == 0.0

    def test_zero_past_volume_does_not_crash(self, tmp_path: Path):
        """When past volumes are all 0 (degenerate / fresh market),
        the divide-by-zero guard engages and yields a neutral signal."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        signal = sa.analyze_volume([0, 0, 0, 0, 0, 100, 200, 300], "tk")
        assert signal.score == 0.0
        assert signal.metadata["vol_ratio"] == 1.0  # safe default


# ── (4) Unit tests: aggregate ───────────────────────────────────────────────


class TestAggregate:
    """Direct coverage of :meth:`SentimentAnalyzer.aggregate` +
    :meth:`get_all_sentiment`."""

    def test_aggregate_for_unknown_token_yields_zeroed_envelope(self, tmp_path: Path):
        """Aggregating a token with no recorded signals returns a
        zeroed envelope (HTTP 200, not 404)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        result = sa.aggregate("nope", lookback_hours=24)
        assert isinstance(result, AggregatedSentiment)
        assert result.token_id == "nope"
        assert result.overall_score == 0.0
        assert result.confidence == 0.0
        assert result.signal_count == 0
        assert result.breakdown == {}
        assert result.trend == "stable"

    def test_aggregate_single_signal(self, tmp_path: Path):
        """One recorded signal drives the aggregate score directly."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        sa.record_signal(SentimentSignal("news", "tk", 0.8, 1.0, time.time()))
        result = sa.aggregate("tk")
        assert result.overall_score == pytest.approx(0.8, abs=1e-3)
        assert result.signal_count == 1
        assert result.breakdown["news"] == pytest.approx(0.8, abs=1e-3)

    def test_aggregate_weights_by_confidence(self, tmp_path: Path):
        """Two signals at the same timestamp: the higher-confidence
        signal pulls the aggregate closer to its own score."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        now = time.time()
        # Signal A: score +1.0, confidence 1.0
        sa.record_signal(SentimentSignal("news", "tk", 1.0, 1.0, now))
        # Signal B: score -1.0, confidence 0.0 (zero weight)
        sa.record_signal(SentimentSignal("news", "tk", -1.0, 0.0, now))
        result = sa.aggregate("tk")
        # Weighted: (1.0 * 1.0 * 1.0 + -1.0 * 1.0 * 0.0) / (1.0 + 0.0) = 1.0
        assert result.overall_score == pytest.approx(1.0, abs=1e-3)

    def test_aggregate_weights_by_recency(self, tmp_path: Path):
        """Older signals carry less weight than recent ones even at
        equal confidence."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        # Use a 1-hour lookback so a 50-minute-old signal still has weight
        # but a 55-minute-old signal has more recency decay than a 5-min-old one.
        now = time.time()
        sa.record_signal(SentimentSignal("news", "tk", -1.0, 1.0, now - 3000))  # 50 min old
        sa.record_signal(SentimentSignal("news", "tk", 1.0, 1.0, now - 300))   # 5 min old
        result = sa.aggregate("tk", lookback_hours=1.0)
        # Recency weights: 5-min-old ~ (1 - 5/60) = 0.917
        #                  50-min-old ~ (1 - 50/60) = 0.167
        # Weighted: (1.0 * 0.917 + -1.0 * 0.167) / (0.917 + 0.167) = 0.75 / 1.083 ≈ 0.692
        assert result.overall_score > 0.0  # Recent positive dominates

    def test_aggregate_breakdown_averages_per_source(self, tmp_path: Path):
        """The ``breakdown`` dict exposes the un-weighted per-source
        average so the dashboard can render a "what's driving this"
        panel without re-running the weighted pipeline."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        now = time.time()
        sa.record_signal(SentimentSignal("news", "tk", 1.0, 1.0, now))
        sa.record_signal(SentimentSignal("news", "tk", -1.0, 1.0, now))
        sa.record_signal(SentimentSignal("price", "tk", 0.5, 1.0, now))
        result = sa.aggregate("tk")
        assert result.breakdown["news"] == pytest.approx(0.0, abs=1e-3)  # (1 + -1) / 2
        assert result.breakdown["price"] == pytest.approx(0.5, abs=1e-3)

    def test_aggregate_persists_to_db(self, tmp_path: Path):
        """A successful aggregate call UPSERTs the result into
        ``aggregated_sentiment`` so a subsequent ``get_all_sentiment``
        call returns it."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, time.time()))
        sa.aggregate("tk")
        rows = sa.get_all_sentiment()
        assert len(rows) == 1
        assert rows[0]["token_id"] == "tk"
        assert rows[0]["overall_score"] == pytest.approx(0.5, abs=1e-3)
        assert rows[0]["signal_count"] == 1

    def test_get_all_sentiment_sorted_by_score_descending(self, tmp_path: Path):
        """``get_all_sentiment`` returns the highest-scoring token
        first so a dashboard can render the most-bullish tokens at
        the top of the list."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        now = time.time()
        sa.record_signal(SentimentSignal("news", "low", -0.5, 1.0, now))
        sa.record_signal(SentimentSignal("news", "high", 0.9, 1.0, now))
        sa.record_signal(SentimentSignal("news", "mid", 0.1, 1.0, now))
        sa.aggregate("low")
        sa.aggregate("high")
        sa.aggregate("mid")
        rows = sa.get_all_sentiment()
        scores = [r["overall_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)
        assert rows[0]["token_id"] == "high"

    def test_aggregate_lookback_filters_old_signals(self, tmp_path: Path):
        """Signals older than ``lookback_hours`` are excluded from
        the aggregate."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        now = time.time()
        # 48-hour-old signal — outside the default 24h lookback
        sa.record_signal(SentimentSignal("news", "tk", -1.0, 1.0, now - 48 * 3600))
        # Fresh signal
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, now))
        result = sa.aggregate("tk", lookback_hours=24)
        # Only the fresh signal counts → score = 0.5
        assert result.signal_count == 1
        assert result.overall_score == pytest.approx(0.5, abs=1e-3)


# ── (5) Unit tests: trend detection ─────────────────────────────────────────


class TestTrendDetection:
    """Direct coverage of the trend label (improving / declining /
    stable) embedded in :meth:`aggregate`."""

    def test_improving_trend_when_recent_signals_higher(self, tmp_path: Path):
        """Older signals bearish, newer signals bullish → improving."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        base = time.time() - 1000
        # Older half (negative)
        sa.record_signal(SentimentSignal("news", "tk", -0.8, 1.0, base))
        sa.record_signal(SentimentSignal("news", "tk", -0.7, 1.0, base + 1))
        # Recent half (positive)
        sa.record_signal(SentimentSignal("news", "tk", 0.6, 1.0, base + 100))
        sa.record_signal(SentimentSignal("news", "tk", 0.8, 1.0, base + 101))
        result = sa.aggregate("tk")
        assert result.trend == "improving"

    def test_declining_trend_when_recent_signals_lower(self, tmp_path: Path):
        """Older signals bullish, newer signals bearish → declining."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        base = time.time() - 1000
        sa.record_signal(SentimentSignal("news", "tk", 0.8, 1.0, base))
        sa.record_signal(SentimentSignal("news", "tk", 0.7, 1.0, base + 1))
        sa.record_signal(SentimentSignal("news", "tk", -0.6, 1.0, base + 100))
        sa.record_signal(SentimentSignal("news", "tk", -0.8, 1.0, base + 101))
        result = sa.aggregate("tk")
        assert result.trend == "declining"

    def test_stable_trend_when_scores_unchanged(self, tmp_path: Path):
        """Older + recent signals both positive at the same level →
        the recent-vs-older delta is < 0.1 → stable."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        base = time.time() - 1000
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, base))
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, base + 1))
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, base + 100))
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, base + 101))
        result = sa.aggregate("tk")
        assert result.trend == "stable"

    def test_fewer_than_four_signals_yields_stable_trend(self, tmp_path: Path):
        """The trend short-circuit: with fewer than 4 signals the
        analyzer refuses to label a trend (returns stable)."""
        sa = SentimentAnalyzer(tmp_path / "s.db")
        sa.record_signal(SentimentSignal("news", "tk", 0.5, 1.0, time.time()))
        sa.record_signal(SentimentSignal("news", "tk", 0.6, 1.0, time.time()))
        result = sa.aggregate("tk")
        assert result.trend == "stable"


# ── (6) API tests: register_routes ──────────────────────────────────────────


@pytest.fixture
def _isolated_singleton_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Patch the module-level singleton ``sentiment_analyzer``'s
    ``_db_path`` to a per-test ``tmp_path`` so recorded signals never
    leak across API tests.

    Mirrors the ``isolated_decision_ledger`` fixture in
    ``tests/conftest.py`` — same monkeypatch-the-module-global pattern.
    """
    db_path = tmp_path / "api_sentiment.db"
    monkeypatch.setattr(_sentiment_module.sentiment_analyzer, "_db_path", db_path)
    # Re-initialise the schema at the new path (the singleton's
    # original __init__ ran against the conftest-redirected path;
    # at the per-test path the tables don't exist yet).
    _sentiment_module.sentiment_analyzer._init_db()
    _sentiment_module.sentiment_analyzer._cache.clear()
    yield _sentiment_module.sentiment_analyzer


@pytest.fixture
def client(_isolated_singleton_db) -> TestClient:
    """Fresh ``FastAPI`` app with only the sentiment routes registered.

    Uses the same ``register_routes(app)`` entry point as the
    production ``api/server.py`` (W17-1 block) so the route
    definitions / Pydantic validation annotations exercised here are
    byte-identical to what the live server exposes — without the
    bearer-token auth middleware or the heavy ``lifespan`` startup.
    """
    app = FastAPI()
    register_routes(app)
    return TestClient(app)


class TestSentimentRoutes:
    """HTTP-level coverage of the three ``/api/sentiment*`` endpoints."""

    def test_post_analyze_returns_signal_and_aggregate(self, client: TestClient):
        """``POST /api/sentiment/analyze`` with a bullish text blob
        returns 200 with the freshly-recorded signal + the aggregated
        sentiment envelope (overall_score > 0, signal_count == 1)."""
        response = client.post(
            "/api/sentiment/analyze",
            json={"text": "Bitcoin surges on bullish rally", "token_id": "btc"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_id"] == "btc"
        assert body["signal_count"] == 1
        assert body["overall_score"] > 0.0
        assert body["signal"]["source"] == "news"
        assert body["signal"]["score"] > 0.0
        assert body["signal"]["metadata"]["bull_count"] >= 2

    def test_post_analyze_rejects_empty_text(self, client: TestClient):
        """An empty ``text`` field is rejected with 422 (Pydantic
        ``min_length=1`` constraint)."""
        response = client.post(
            "/api/sentiment/analyze",
            json={"text": "", "token_id": "btc"},
        )
        assert response.status_code == 422

    def test_post_analyze_rejects_whitespace_only_text(self, client: TestClient):
        """A whitespace-only ``text`` field is rejected with 422 at
        the handler level (the ``strip()`` check fires before the
        analyzer runs)."""
        response = client.post(
            "/api/sentiment/analyze",
            json={"text": "   \n\t   ", "token_id": "btc"},
        )
        assert response.status_code == 422

    def test_post_analyze_rejects_missing_fields(self, client: TestClient):
        """Omitting ``text`` or ``token_id`` triggers Pydantic's 422
        required-field validation."""
        resp_no_text = client.post(
            "/api/sentiment/analyze", json={"token_id": "btc"}
        )
        assert resp_no_text.status_code == 422
        resp_no_token = client.post(
            "/api/sentiment/analyze", json={"text": "Bitcoin surges"}
        )
        assert resp_no_token.status_code == 422

    def test_get_single_token_returns_zeroed_envelope_when_unknown(
        self, client: TestClient
    ):
        """``GET /api/sentiment/{token_id}`` for a token with no
        recorded signals returns 200 with a zeroed envelope (not
        404)."""
        response = client.get("/api/sentiment/unknown_token")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_id"] == "unknown_token"
        assert body["overall_score"] == 0.0
        assert body["confidence"] == 0.0
        assert body["signal_count"] == 0
        assert body["trend"] == "stable"

    def test_get_single_token_returns_aggregate_after_write(
        self, client: TestClient
    ):
        """``GET /api/sentiment/{token_id}`` after a POST analyze
        returns 200 with the freshly-aggregated sentiment."""
        client.post(
            "/api/sentiment/analyze",
            json={"text": "Markets crash as bearish outlook plummets", "token_id": "tk"},
        )
        response = client.get("/api/sentiment/tk")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["token_id"] == "tk"
        assert body["signal_count"] == 1
        assert body["overall_score"] < 0.0  # Bearish
        assert body["trend"] == "stable"  # Single signal → stable

    def test_get_list_returns_aggregates_sorted_by_score(
        self, client: TestClient
    ):
        """``GET /api/sentiment`` returns every persisted aggregate,
        highest score first."""
        client.post(
            "/api/sentiment/analyze",
            json={"text": "Bitcoin surges on bullish rally", "token_id": "high"},
        )
        client.post(
            "/api/sentiment/analyze",
            json={"text": "Markets crash as bearish outlook plummets", "token_id": "low"},
        )
        response = client.get("/api/sentiment")
        assert response.status_code == 200, response.text
        body = response.json()
        rows = body["sentiments"]
        assert len(rows) == 2
        scores = [r["overall_score"] for r in rows]
        assert scores == sorted(scores, reverse=True)
        assert rows[0]["token_id"] == "high"

    def test_post_analyze_then_get_single_token_round_trip(
        self, client: TestClient
    ):
        """End-to-end round-trip: POST analyze writes the signal +
        aggregate, GET single-token reads it back."""
        # Write a bullish signal
        post_resp = client.post(
            "/api/sentiment/analyze",
            json={"text": "Surge rally gain bullish breakout", "token_id": "rt"},
        )
        assert post_resp.status_code == 200
        post_body = post_resp.json()
        assert post_body["overall_score"] > 0.0
        # GET reads it back
        get_resp = client.get("/api/sentiment/rt")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["token_id"] == "rt"
        assert get_body["signal_count"] == 1
        assert get_body["overall_score"] == pytest.approx(
            post_body["overall_score"], abs=1e-6
        )

    def test_multiple_signals_accumulate_via_repeated_posts(
        self, client: TestClient
    ):
        """Multiple POST analyze calls for the same token accumulate
        signals — the aggregate's ``signal_count`` grows on each
        call."""
        for _ in range(3):
            client.post(
                "/api/sentiment/analyze",
                json={"text": "Bitcoin surges", "token_id": "acc"},
            )
        response = client.get("/api/sentiment/acc")
        body = response.json()
        assert body["signal_count"] == 3
