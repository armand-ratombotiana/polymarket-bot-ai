"""
tests/test_analysis_engine.py — Unit tests for ``core/analysis_engine.py``.

X5 — DeepMarketAnalysisEngine unit tests.

Covers the five behaviours required by X5:

  (1) ``analyze_market`` returns a dict with the expected top-level fields
      (``token_id``, ``slug``, ``status``, ``market_implied_prob``,
      ``ml_forecast_prob``, ``uncertainty_interval``, ``raw_edge``,
      ``net_edge``, ``confidence_score``, ``suggested_action``,
      ``action_reasons``, ``regime``, ``regime_tag``, ``model_metadata``,
      ``generation_time_ms`` …) for a valid, liquid order book.
  (2) ``analyze_market`` handles a **missing market** (``token_id`` not
      present in ``store.order_books``) gracefully — returns an
      ``INSUFFICIENT_DATA`` dict without raising.
  (3) ``analyze_market`` handles a **missing / empty book** (book entry
      exists in ``store.order_books`` but has no bids or asks, so
      ``best_bid`` / ``best_ask`` are ``None``) gracefully — same
      ``INSUFFICIENT_DATA`` short-circuit, no exception.
  (4) ``analyze_market`` returns the confidence / edge / prediction
      fields (``confidence_score``, ``raw_edge``, ``net_edge``,
      ``ml_forecast_prob``, ``market_implied_prob``) as numeric floats
      in their documented ranges, with ``ml_forecast_prob`` bounded by
      ``uncertainty_interval``.
  (5) ``analyze_market`` handles **empty features** (``extract_features``
      returns ``None`` — the documented "no usable feature vector"
      case) gracefully — falls back to ``p_ml = mid_p`` and
      ``confidence = 0.50`` and still returns a ``VALIDATED`` dict.

Module-selection note
~~~~~~~~~~~~~~~~~~~~~
Both ``core/analysis_engine.py`` and ``core/deep_analysis.py`` exist in
the repo. The X5 task asks for tests targeting whichever module is the
primary "analysis engine". ``core/analysis_engine.py`` is the one that
exposes the ``analyze_market(token_id) -> dict`` entry point used by the
trading pipeline (``strategies/signal_trader._ml_signal`` calls it
indirectly via the opportunity ranker, and ``api/server.py`` exposes it
on the ``/api/analysis/<token_id>`` route). ``core/deep_analysis.py`` is
a supporting module (whale tracking + regime classification) whose
``classify_regime`` is consumed by ``analysis_engine.analyze_market``.
This test file targets ``core/analysis_engine.py`` — the module that
matches the test-file name and the task's "analysis engine" scope.

Isolation strategy
~~~~~~~~~~~~~~~~~~
The global ``store`` singleton is reset to factory defaults before every
test by the autouse ``_reset_store_factory_defaults`` fixture in
``tests/conftest.py`` (clears ``store.order_books`` and
``store.market_slugs``). Each test repopulates ``store.order_books`` /
``store.market_slugs`` with the book it needs — no monkeypatching of
``store`` itself is required.

``DeepMarketAnalysisEngine`` (in ``core/analysis_engine.py``) is a
stateless class (no ``__init__``, no instance attributes), so the
module-level ``deep_analysis_engine`` singleton is used directly —
fresh construction would be equivalent but adds nothing.

The module-level ``ml_model`` singleton is loaded from the
conftest-redirected ``MODEL_PATH`` (``/tmp/pmbot_conftest_isolation/model.pkl``)
at import time and is already fitted, so the ML-predict code path is
exercised for real — no mocking of ``predict_proba`` /
``predict_confidence``. (Test 5 mocks ``extract_features`` to return
``None`` to hit the empty-features fallback branch; this is the only
monkeypatch in the file.)

All tests are **synchronous** — ``analyze_market`` is a sync method (no
``await``), so no ``pytestmark = pytest.mark.asyncio`` declaration is
needed (unlike ``tests/test_decision_ledger.py`` which targets the
async ``record`` / ``get_chain`` API).
"""
from __future__ import annotations

import sys
from pathlib import Path

# Inline sys.path bootstrap — mirrors the pattern in test_features.py /
# test_paper_simulator.py. conftest.py also does this, but the inline
# bootstrap keeps this module self-contained if it is ever run outside the
# pytest collection that imports conftest first (e.g. ``python -m pytest
# tests/test_analysis_engine.py`` from a fresh process where conftest's
# sys.path insert may not have run before this module's top-level imports).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from core.analysis_engine import deep_analysis_engine  # noqa: E402
from core.data_store import OrderBook, PriceLevel, store  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_book(
    token_id: str,
    bid_price: float = 0.49,
    bid_size: float = 500.0,
    ask_price: float = 0.51,
    ask_size: float = 500.0,
    levels: int = 5,
) -> OrderBook:
    """Build a multi-level ``OrderBook`` with a tight, liquid book.

    A 5-level symmetric book around mid ≈ 0.50 keeps the spread (2¢)
    well under the 4¢ rejection threshold and the cumulative depth
    (~$2,500 USDC per side) well above the $200 liquidity floor, so
    ``analyze_market`` reaches the ``VALIDATED`` return path rather
    than the ``REJECT_RISK`` branch.
    """
    bids = [
        PriceLevel(price=round(bid_price - i * 0.01, 4), size=bid_size)
        for i in range(levels)
    ]
    asks = [
        PriceLevel(price=round(ask_price + i * 0.01, 4), size=ask_size)
        for i in range(levels)
    ]
    return OrderBook(token_id=token_id, bids=bids, asks=asks)


# Expected top-level keys on a VALIDATED analysis result. Mirrors the
# return dict in ``DeepMarketAnalysisEngine.analyze_market`` (lines
# 139–175 of ``core/analysis_engine.py``). Used by tests (1) and (5) to
# verify the full schema is present (catches silent field omissions).
_EXPECTED_VALIDATED_KEYS = {
    "token_id",
    "slug",
    "status",
    "market_implied_prob",
    "ml_forecast_prob",
    "uncertainty_interval",
    "raw_edge",
    "net_edge",
    "confidence_score",
    "best_bid",
    "best_ask",
    "spread_dollars",
    "spread_pct",
    "total_liquidity_usdc",
    "bid_depth_usdc",
    "ask_depth_usdc",
    "order_flow_imbalance",
    "slippage_bps",
    "fundamental_sentiment",
    "supporting_evidence",
    "contradicting_evidence",
    "suggested_action",
    "action_reasons",
    "regime",
    "regime_tag",
    "model_metadata",
    "data_freshness_seconds",
    "generation_time_ms",
}

# Expected top-level keys on the INSUFFICIENT_DATA short-circuit return
# (lines 40–46 of ``core/analysis_engine.py``). Used by tests (2) and (3)
# to verify the early-return schema is exactly the documented set — no
# partial VALIDATED fields leak into the short-circuit path.
_EXPECTED_INSUFFICIENT_KEYS = {
    "token_id",
    "slug",
    "status",
    "reason",
    "generation_time_ms",
}


# ── (1) Returns dict with expected fields ────────────────────────────────────

def test_analyze_market_returns_dict_with_expected_fields():
    """``analyze_market`` must return a dict carrying every documented
    top-level field when the order book is valid and liquid."""
    token_id = "TOK_FIELDS_X5"
    store.order_books[token_id] = _make_book(token_id)
    store.market_slugs[token_id] = "test-market-fields"

    result = deep_analysis_engine.analyze_market(token_id)

    # Return type is a dict.
    assert isinstance(result, dict)

    # Status is VALIDATED (we got past the INSUFFICIENT_DATA gate).
    assert result["status"] == "VALIDATED"

    # Every documented top-level field is present — catches silent
    # schema regressions where a field is accidentally dropped from the
    # return dict.
    missing = _EXPECTED_VALIDATED_KEYS - set(result.keys())
    assert not missing, f"Missing expected fields: {sorted(missing)}"

    # No unexpected extra top-level keys — catches silent schema drift
    # where a field is accidentally added (e.g. a debug leftover). If
    # this assertion fires because a new field was intentionally added
    # to ``analyze_market``'s return dict, update
    # ``_EXPECTED_VALIDATED_KEYS`` above to include it.
    extra = set(result.keys()) - _EXPECTED_VALIDATED_KEYS
    assert not extra, f"Unexpected extra fields: {sorted(extra)}"

    # Identity fields round-trip the inputs.
    assert result["token_id"] == token_id
    assert result["slug"] == "test-market-fields"

    # uncertainty_interval is a 2-element list with lo <= hi in (0, 1).
    ui = result["uncertainty_interval"]
    assert isinstance(ui, list) and len(ui) == 2
    assert 0.0 < ui[0] <= ui[1] < 1.0

    # model_metadata is itself a dict with the documented sub-keys.
    md = result["model_metadata"]
    assert isinstance(md, dict)
    for sub_key in (
        "version",
        "brier_score",
        "ece",
        "roc_auc",
        "features_used",
        "adaptive_weights",
    ):
        assert sub_key in md, f"model_metadata missing '{sub_key}'"

    # supporting_evidence / contradicting_evidence are lists (possibly
    # empty — the fundamental engine has no live news in the test
    # sandbox).
    assert isinstance(result["supporting_evidence"], list)
    assert isinstance(result["contradicting_evidence"], list)

    # action_reasons is a non-empty list (every branch in the
    # recommendation block appends at least one reason).
    assert isinstance(result["action_reasons"], list)
    assert len(result["action_reasons"]) >= 1

    # generation_time_ms is a non-negative number.
    assert isinstance(result["generation_time_ms"], (int, float))
    assert result["generation_time_ms"] >= 0.0


# ── (2) Handles missing market gracefully ────────────────────────────────────

def test_analyze_market_handles_missing_market_gracefully():
    """When ``token_id`` is absent from ``store.order_books`` (i.e. the
    market was never discovered / has no book), ``analyze_market`` must
    return an ``INSUFFICIENT_DATA`` dict — NOT raise."""
    # The conftest autouse fixture has already cleared store.order_books,
    # so "UNKNOWN_TOKEN_X5" genuinely does not exist in the store.
    token_id = "UNKNOWN_TOKEN_X5"

    result = deep_analysis_engine.analyze_market(token_id)

    assert isinstance(result, dict)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["token_id"] == token_id

    # Slug falls back to ``token_id[:18]`` when no slug is registered
    # (the ``store.market_slugs.get(token_id, token_id[:18])`` default).
    assert result["slug"] == token_id[:18]

    # A human-readable reason is supplied.
    assert isinstance(result["reason"], str) and result["reason"]

    # Only the INSUFFICIENT_DATA keys are present — no partial VALIDATED
    # fields leaked into the short-circuit return.
    assert set(result.keys()) == _EXPECTED_INSUFFICIENT_KEYS

    # generation_time_ms is still reported (perf instrumentation runs
    # even on the early-return path).
    assert isinstance(result["generation_time_ms"], (int, float))
    assert result["generation_time_ms"] >= 0.0


# ── (3) Handles missing / empty book gracefully ──────────────────────────────

def test_analyze_market_handles_missing_book_gracefully():
    """When the book entry exists in ``store.order_books`` but has no
    bids or asks (``best_bid`` / ``best_ask`` are ``None``),
    ``analyze_market`` must return ``INSUFFICIENT_DATA`` — NOT raise.

    This is the ``not book.best_bid or not book.best_ask`` branch of
    the INSUFFICIENT_DATA gate (line 39 of ``core/analysis_engine.py``),
    distinct from test (2) which exercises the ``not book`` (None book)
    branch. Both branches share the same return dict.
    """
    token_id = "TOK_EMPTY_BOOK_X5"
    # Book exists in the store but is empty — no levels on either side.
    store.order_books[token_id] = OrderBook(token_id=token_id, bids=[], asks=[])
    store.market_slugs[token_id] = "empty-book-market"

    result = deep_analysis_engine.analyze_market(token_id)

    assert isinstance(result, dict)
    assert result["status"] == "INSUFFICIENT_DATA"
    assert result["token_id"] == token_id
    assert result["slug"] == "empty-book-market"

    # Reason string is non-empty and explanatory.
    assert isinstance(result["reason"], str) and result["reason"]

    # Only the short-circuit keys are present.
    assert set(result.keys()) == _EXPECTED_INSUFFICIENT_KEYS


# ── (4) Returns confidence / edge / prediction fields ───────────────────────

def test_analyze_market_returns_confidence_edge_prediction_fields():
    """The confidence, edge, and prediction fields must be present as
    numeric floats in their documented ranges on a VALIDATED result."""
    token_id = "TOK_CONF_EDGE_X5"
    store.order_books[token_id] = _make_book(token_id)
    store.market_slugs[token_id] = "confidence-edge-market"

    result = deep_analysis_engine.analyze_market(token_id)

    assert result["status"] == "VALIDATED"

    # ── Prediction fields ──────────────────────────────────────────────────
    # ``market_implied_prob`` = mid price; ``ml_forecast_prob`` = calibrated
    # ensemble output. Both must be numeric floats in [0, 1].
    for key in ("market_implied_prob", "ml_forecast_prob"):
        assert key in result, f"missing prediction field '{key}'"
        assert isinstance(result[key], (int, float)), \
            f"'{key}' must be numeric, got {type(result[key]).__name__}"
        assert 0.0 <= result[key] <= 1.0, \
            f"'{key}'={result[key]} out of [0, 1]"

    # ── Edge fields ────────────────────────────────────────────────────────
    # ``raw_edge`` = p_ml - mid_p; ``net_edge`` = raw_edge adjusted for
    # fees + slippage. Both are probability differences, bounded in [-1, 1].
    for key in ("raw_edge", "net_edge"):
        assert key in result, f"missing edge field '{key}'"
        assert isinstance(result[key], (int, float)), \
            f"'{key}' must be numeric, got {type(result[key]).__name__}"
        assert -1.0 <= result[key] <= 1.0, \
            f"'{key}'={result[key]} out of [-1, 1]"

    # ── Confidence field ──────────────────────────────────────────────────
    # ``confidence_score`` = |p_ml - 0.5| * 2, in [0, 1].
    assert "confidence_score" in result
    conf = result["confidence_score"]
    assert isinstance(conf, (int, float)), \
        f"'confidence_score' must be numeric, got {type(conf).__name__}"
    assert 0.0 <= conf <= 1.0, \
        f"confidence_score={conf} out of [0, 1]"

    # ── uncertainty_interval bounds the ML forecast ──────────────────────
    # Documented semantics (lines 72–74): p_lower <= p_ml <= p_upper,
    # where p_lower / p_upper = p_ml ± uncertainty_margin and
    # uncertainty_margin = (1 - confidence) * 0.12.
    p_ml = result["ml_forecast_prob"]
    p_lo, p_hi = result["uncertainty_interval"]
    assert p_lo <= p_ml <= p_hi, \
        f"ml_forecast_prob={p_ml} outside uncertainty_interval [{p_lo}, {p_hi}]"

    # ── market_implied_prob equals the book mid ───────────────────────────
    # Sanity check: ``market_implied_prob`` is ``round(mid_p, 4)`` where
    # ``mid_p = book.mid``. For the default _make_book (bid=0.49,
    # ask=0.51), mid = 0.50.
    book = store.order_books[token_id]
    assert result["market_implied_prob"] == pytest.approx(book.mid or 0.5)


# ── (5) Handles empty features ───────────────────────────────────────────────

def test_analyze_market_handles_empty_features(monkeypatch):
    """When ``extract_features`` returns ``None`` (the documented
    "empty features" case — e.g. mid price out of [0.001, 0.999], or
    any other future guard inside ``extract_features``),
    ``analyze_market`` must fall back to ``p_ml = mid_p`` and
    ``confidence = 0.50`` and still return a ``VALIDATED`` dict — NOT
    raise.

    This is the ``else`` branch at lines 67–69 of
    ``core/analysis_engine.py``. We force the branch by monkeypatching
    ``core.analysis_engine.extract_features`` to return ``None`` —
    this is the exact contract the analysis engine's fallback depends
    on, and it isolates the test from ``ml.features.extract_features``'s
    internal rejection logic (which could change independently).
    """
    token_id = "TOK_NO_FEAT_X5"
    store.order_books[token_id] = _make_book(token_id)
    store.market_slugs[token_id] = "no-features-market"

    # Force the empty-features branch.
    monkeypatch.setattr(
        "core.analysis_engine.extract_features",
        lambda market, book: None,
    )

    result = deep_analysis_engine.analyze_market(token_id)

    assert isinstance(result, dict)
    assert result["status"] == "VALIDATED"

    # Fallback semantics: p_ml falls back to mid_p, confidence to 0.50.
    mid_p = result["market_implied_prob"]
    assert result["ml_forecast_prob"] == pytest.approx(mid_p), \
        "ml_forecast_prob must equal market_implied_prob (mid_p) when " \
        "features are empty — the documented fallback"
    assert result["confidence_score"] == pytest.approx(0.50), \
        "confidence_score must fall back to 0.50 when features are empty"

    # raw_edge = p_ml - mid_p = 0 when features are empty (p_ml == mid_p).
    assert result["raw_edge"] == pytest.approx(0.0), \
        "raw_edge must be 0 when features are empty (p_ml == mid_p)"

    # The VALIDATED schema is still complete (no fields dropped on the
    # fallback path).
    missing = _EXPECTED_VALIDATED_KEYS - set(result.keys())
    assert not missing, f"Missing expected fields: {sorted(missing)}"

    # uncertainty_interval still bounds the (fallback) ml_forecast_prob.
    p_ml = result["ml_forecast_prob"]
    p_lo, p_hi = result["uncertainty_interval"]
    assert p_lo <= p_ml <= p_hi, \
        f"ml_forecast_prob={p_ml} outside uncertainty_interval [{p_lo}, {p_hi}]"
