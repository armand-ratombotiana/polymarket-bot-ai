"""
tests/test_ml_copilot.py — Unit tests for ``ml/copilot.py``.

X6 — GenAI Market Intelligence & Copilot Engine unit tests.

Covers the five behaviours required by the X6 task spec:

  (1) ``answer_query`` returns a string response — the ``reply`` field
      of the returned dict is a ``str`` (the human-readable natural-
      language surface contract for chat-style consumers).
  (2) ``analyze_market`` returns a dict carrying the documented
      analysis-field schema (``token_id``, ``slug``, ``mid_price``,
      ``spread``, ``ml_probability``, ``net_edge``, ``confidence``,
      ``regime``, ``regime_tag``, ``sentiment``, ``recommendation``,
      ``risk_score``, ``feature_drivers``, ``model_metadata``,
      ``rationale``, ``generated_at``).
  (3) ``answer_query`` handles an empty query gracefully — no
      exception is raised, the returned dict has a non-empty ``reply``
      string, and the empty ``query`` is preserved verbatim in the
      response envelope.
  (4) ``analyze_market`` handles an unknown token gracefully — when
      ``store.get_order_book(token_id)`` returns ``None`` (token never
      seen by the book poller / Gamma client), the engine's documented
      "initializing" fallback branch returns a payload with neutral
      sentiment, Hold recommendation, and the model-metadata envelope
      — never raises.
  (5) Response contains market context — both ``answer_query``'s
      ``reply`` (which must reference the top matched market's TITLE
      when semantic search returns hits) and ``analyze_market``'s
      ``rationale`` (which must reference the market SLUG and the
      price/spread context) carry market-identifying context so the
      caller can correlate the response with the right contract.

Test isolation strategy
-----------------------
* Each test constructs a fresh ``AICopilotEngine()`` instance via the
  class constructor — the module-level singleton ``copilot_engine``
  (constructed at import time) is NEVER touched. Its ``_history``
  list therefore remains pristine across tests.
* The LLM / model dependencies consumed by ``ml/copilot.py`` at call
  time — ``vector_store``, ``ml_model``, ``model_registry``,
  ``drift_detector`` — are mocked via ``unittest.mock.patch`` so the
  tests are fully hermetic to the real on-disk model state (the real
  singletons read ``/app/data/model.pkl`` / ``vector_index.json`` /
  ``model_registry.json`` at import time, which are not writable in
  the sandbox and whose fit state is non-deterministic across
  environments).
* The ``store`` singleton (used by ``analyze_market`` for
  ``get_order_book`` and by ``answer_query``'s fallback market scan)
  is reset to a clean factory baseline before every test by the
  autouse ``_reset_store_factory_defaults`` fixture in
  ``tests/conftest.py`` — ``store.order_books`` and
  ``store.market_slugs`` are guaranteed empty at the start of every
  test.
* ``analyze_market``'s happy path injects a real ``OrderBook`` into
  the global ``store`` singleton via ``store.update_order_book`` —
  the same async write path production uses (``book_poller``,
  ``gamma_client`` …). This exercises the full happy-path code path
  through ``extract_features`` → ``ml_model.is_fitted`` (mocked False)
  → ``deep_analysis_engine.classify_regime`` without requiring any
  network or DB I/O.
* The repo's ``pytest.ini`` declares ``testpaths = tests``; this file
  is collected automatically. The sibling ``tests/conftest.py`` is
  imported BEFORE this module so its env-var redirects
  (``MODEL_PATH`` → ``/tmp/pmbot_conftest_isolation/model.pkl``, etc.)
  + ``sys.path`` bootstrap are already in effect — no inline
  redirection needed here, only an inline ``sys.path`` insertion as
  a belt-and-braces measure for IDE / direct
  ``pytest tests/test_ml_copilot.py`` runs.
* The X6 task spec forbids editing existing files; this module is
  strictly additive. All ``private``-member touches (none in this
  file) would be permitted by the repo's ``pyproject.toml``
  ``[tool.ruff.lint.per-file-ignores] "tests/*" = ["SLF001"]``.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# Inline sys.path bootstrap — mirrors the pattern in test_features.py /
# test_ml_model.py / tests/conftest.py so ``from ml.copilot import ...``
# resolves regardless of the cwd pytest was launched from (monorepo root,
# CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.data_store import OrderBook, PriceLevel, store  # noqa: E402  (sys.path first)
from ml.copilot import AICopilotEngine  # noqa: E402  (sys.path first)

# Apply ``@pytest.mark.asyncio`` to every ``async def test_...`` in this
# module. Mirrors the pattern in tests/test_decision_ledger.py — the
# repo's ``pytest.ini`` cannot be edited per the X6 task constraint, so
# we use the module-level ``pytestmark`` idiom instead of
# ``asyncio_mode = "auto"``.
pytestmark = pytest.mark.asyncio


# ── Fakes for the model-side singletons ─────────────────────────────────────


class _FakeMLModel:
    """Minimal stand-in for ``ml.model.ml_model`` used by ``copilot.py``.

    ``copilot.analyze_market`` consults:
      * ``ml_model.is_fitted``       — ``@property`` returning a ``bool``
      * ``ml_model.predict(feats, token_id=...)`` — ``(p_yes, conf)`` tuple
      * ``ml_model.feature_importances`` — ``dict[str, float]``
      * ``ml_model.brier_score``     — ``float``
      * ``ml_model.ece``              — ``float``
      * ``ml_model.adaptive_weights`` — ``@property`` → ``dict[str, float]``

    The fake defaults to ``fitted=False`` so ``analyze_market`` skips the
    ``predict`` path entirely (the documented cold-start contract:
    ``p_yes=0.5``, ``conf=0.0``). Tests that need the fitted branch can
    flip ``fitted=True`` on the instance after construction.
    """

    def __init__(
        self,
        *,
        fitted: bool = False,
        p_yes: float = 0.5,
        conf: float = 0.0,
    ) -> None:
        self._fitted = fitted
        self._p_yes = p_yes
        self._conf = conf
        # Empty dict → ``if ml_model.feature_importances:`` short-circuits to
        # False, skipping the per-feature driver scoring loop in
        # ``analyze_market``. Keeps the happy-path dict payload minimal.
        self.feature_importances: dict[str, float] = {}
        self.brier_score: float = 0.18
        self.ece: float = 0.04
        self._adaptive_weights = {
            "rf": 0.50,
            "gb": 0.45,
            "sgd": 0.05,
            "lgbm": 0.0,
        }

    @property
    def is_fitted(self) -> bool:
        return self._fitted

    @property
    def adaptive_weights(self) -> dict[str, float]:
        return self._adaptive_weights

    def predict(self, features, token_id: str = "") -> tuple[float, float]:
        return (self._p_yes, self._conf)


class _FakeModelRegistry:
    """Minimal stand-in for ``ml.model_registry.model_registry``.

    ``copilot.analyze_market`` reads ``model_registry.active_version``
    (a ``str`` like ``"v1.0.0"``) into the ``model_metadata`` envelope.
    """

    def __init__(self) -> None:
        self.active_version: str = "v1.0.0-x6-test"


class _FakeDriftDetector:
    """Minimal stand-in for ``ml.drift_detector.drift_detector``.

    ``copilot.analyze_market`` reads ``drift_detector.drift_status``
    (a ``str`` like ``"HEALTHY"``) into the ``model_metadata`` envelope.
    """

    def __init__(self) -> None:
        self.drift_status: str = "HEALTHY"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def engine() -> AICopilotEngine:
    """Return a brand-new ``AICopilotEngine()`` instance per test.

    The module-level singleton ``copilot_engine`` (constructed at
    import time and shared across the whole pytest session) is never
    perturbed by these tests — every test gets a fresh instance whose
    ``_history`` list starts empty.
    """
    return AICopilotEngine()


@pytest.fixture
def fake_ml_model():
    """Patch the module-level ``ml_model`` reference in ``ml/copilot.py``.

    The copilot module binds ``ml_model`` at import time via
    ``from ml.model import ml_model``. ``unittest.mock.patch`` replaces
    the module-level binding inside ``ml.copilot`` for the duration of
    the test, so any access inside ``analyze_market`` (e.g.
    ``ml_model.is_fitted``) resolves to the fake. The fake is yielded
    so the test can flip ``fitted=True`` / ``p_yes`` if it wants to
    exercise the predict branch.
    """
    fake = _FakeMLModel()
    with patch("ml.copilot.ml_model", fake):
        yield fake


@pytest.fixture
def fake_model_registry():
    """Patch the module-level ``model_registry`` reference in ``ml/copilot.py``."""
    fake = _FakeModelRegistry()
    with patch("ml.copilot.model_registry", fake):
        yield fake


@pytest.fixture
def fake_drift_detector():
    """Patch the module-level ``drift_detector`` reference in ``ml/copilot.py``."""
    fake = _FakeDriftDetector()
    with patch("ml.copilot.drift_detector", fake):
        yield fake


@pytest.fixture
def fake_dependencies(
    fake_ml_model,
    fake_model_registry,
    fake_drift_detector,
):
    """Convenience fixture: patches all three model-side dependencies at once.

    Returns a ``SimpleNamespace`` so a test that needs to interact with
    the fakes (e.g. flip ``fitted=True``) can do so via
    ``fake_dependencies.ml_model._fitted = True`` without re-acquiring
    the fixture.
    """
    return SimpleNamespace(
        ml_model=fake_ml_model,
        model_registry=fake_model_registry,
        drift_detector=fake_drift_detector,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_book(
    token_id: str,
    *,
    mid: float = 0.55,
    spread: float = 0.02,
    bid_size: float = 100.0,
    ask_size: float = 120.0,
) -> OrderBook:
    """Build a minimal valid ``OrderBook`` for ``analyze_market``.

    The book has a single best-bid and single best-ask level so
    ``book.mid`` / ``book.spread`` / ``book.bids[0].size`` /
    ``book.asks[0].size`` all resolve to non-None values — exactly
    what ``extract_features`` and the depth-imbalance heuristic in
    ``analyze_market`` require.
    """
    best_bid = mid - spread / 2.0
    best_ask = mid + spread / 2.0
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(price=best_bid, size=bid_size)],
        asks=[PriceLevel(price=best_ask, size=ask_size)],
    )


# ── (1) answer_query returns a string response ──────────────────────────────


async def test_answer_query_returns_string_reply(engine, fake_dependencies):
    """``answer_query`` must return a dict whose ``reply`` value is a ``str``.

    The copilot's natural-language surface contract: callers consume
    ``result["reply"]`` as the human-readable response string. The dict
    container also carries ``matched_markets`` / ``timestamp`` / ``query``,
    but the ``reply`` field is the load-bearing return for chat-style
    consumers. We exercise the matched-markets branch (vector_store.search
    returns a non-empty result list) so the reply is the rich
    semantic-match narrative rather than the fallback scan string.
    """
    fake_vector_results = [
        (
            {
                "token_id": "TOK_X6_STR_1",
                "title": "Will BTC close above $100k in 2026?",
                "slug": "btc-100k-2026",
            },
            0.82,
        ),
    ]
    with patch("ml.copilot.vector_store") as vs_mock:
        vs_mock.search.return_value = fake_vector_results
        result = await engine.answer_query("Tell me about Bitcoin markets")

    # (a) Top-level return type is a dict.
    assert isinstance(result, dict), (
        f"answer_query must return a dict, got {type(result).__name__}"
    )
    # (b) The ``reply`` field exists and is a string.
    assert "reply" in result, "answer_query result must contain 'reply' key"
    assert isinstance(result["reply"], str), (
        f"'reply' must be a str, got {type(result['reply']).__name__}"
    )
    # (c) The reply is non-empty (not the empty string).
    assert len(result["reply"]) > 0, "'reply' string must be non-empty"

    # (d) The response envelope also carries the standard structural keys
    #     so callers consuming the dict-level contract don't break.
    for envelope_key in ("query", "matched_markets", "timestamp"):
        assert envelope_key in result, (
            f"answer_query result must carry envelope key {envelope_key!r}"
        )


# ── (2) analyze_market returns dict with analysis fields ─────────────────────


async def test_analyze_market_returns_dict_with_analysis_fields(
    engine,
    fake_dependencies,
):
    """``analyze_market`` must return a dict carrying the documented
    analysis-field schema.

    The happy-path return payload (when ``store.get_order_book`` returns
    a non-None ``OrderBook``) enumerates:
      ``token_id``, ``slug``, ``mid_price``, ``spread``, ``ml_probability``,
      ``net_edge``, ``confidence``, ``regime``, ``regime_tag``,
      ``sentiment``, ``recommendation``, ``risk_score``,
      ``feature_drivers``, ``model_metadata``, ``rationale``,
      ``generated_at``.

    The fake ``ml_model`` is unfitted (``is_fitted=False``) so the
    ``predict`` branch is skipped — ``p_yes=0.5`` / ``conf=0.0`` — but
    every other field is populated by the post-predict code path.
    """
    token_id = "TOK_X6_ANALYSIS"
    slug = "test-market-analysis"
    book = _make_book(token_id, mid=0.55, spread=0.02)
    await store.update_order_book(book)
    store.market_slugs[token_id] = slug

    result = await engine.analyze_market(
        token_id,
        market_dict={"slug": slug, "volume24hr": 5000.0},
    )

    # (a) Top-level return type is a dict.
    assert isinstance(result, dict), (
        f"analyze_market must return a dict, got {type(result).__name__}"
    )

    # (b) Every documented analysis field is present.
    expected_fields = {
        "token_id",
        "slug",
        "mid_price",
        "spread",
        "ml_probability",
        "net_edge",
        "confidence",
        "regime",
        "regime_tag",
        "sentiment",
        "recommendation",
        "risk_score",
        "feature_drivers",
        "model_metadata",
        "rationale",
        "generated_at",
    }
    missing = expected_fields - set(result.keys())
    assert not missing, (
        "analyze_market result is missing documented analysis fields: "
        f"{sorted(missing)}"
    )

    # (c) Schema sanity — the analysis-specific fields carry the right
    #     types so downstream API serializers don't blow up.
    assert isinstance(result["feature_drivers"], list), (
        "feature_drivers must be a list"
    )
    assert isinstance(result["model_metadata"], dict), (
        "model_metadata must be a dict"
    )
    assert isinstance(result["rationale"], str), "rationale must be a str"
    assert isinstance(result["generated_at"], float), (
        "generated_at must be a float (unix timestamp)"
    )
    assert isinstance(result["ml_probability"], float), (
        "ml_probability must be a float"
    )
    assert isinstance(result["net_edge"], float), "net_edge must be a float"
    assert isinstance(result["confidence"], float), "confidence must be a float"

    # (d) Identity fields propagated verbatim from the call args.
    assert result["token_id"] == token_id
    assert result["slug"] == slug

    # (e) ``recommendation`` / ``sentiment`` / ``risk_score`` come from the
    #     bounded enum the engine's regime logic produces — assert they're
    #     non-empty strings (the exact value depends on the mid/spread
    #     regime classification, which we don't pin here to avoid coupling
    #     the test to the regime-classifier thresholds).
    for enum_field in ("recommendation", "sentiment", "risk_score",
                       "regime", "regime_tag"):
        assert isinstance(result[enum_field], str), (
            f"{enum_field} must be a str, got {type(result[enum_field]).__name__}"
        )
        assert len(result[enum_field]) > 0, (
            f"{enum_field} must be a non-empty str"
        )

    # (f) The model_metadata envelope carries the governance trio the
    #     engine interpolates from ``model_registry`` / ``ml_model`` /
    #     ``drift_detector``.
    for md_field in ("version", "brier_score", "ece", "drift_status",
                     "adaptive_weights"):
        assert md_field in result["model_metadata"], (
            f"model_metadata missing required field: {md_field}"
        )


# ── (3) handles empty query gracefully ─────────────────────────────────────


async def test_answer_query_handles_empty_query(engine, fake_dependencies):
    """``answer_query`` must not raise on an empty / blank query.

    An empty query string is a degenerate-but-plausible caller input
    (UI text-box submitted before the user types anything). The
    vector_store's semantic search returns ``[]`` for an empty query
    (the tokenizer yields zero tokens), and the engine's documented
    fallback branch returns the general "actively monitoring N
    markets" scan reply — never raises.

    The mocked ``vector_store.search`` returns ``[]`` explicitly to
    make the test deterministic regardless of the real vector store's
    on-disk index state (which the conftest env redirect may or may
    not populate).
    """
    with patch("ml.copilot.vector_store") as vs_mock:
        vs_mock.search.return_value = []
        # The load-bearing assertion: no exception is raised.
        result = await engine.answer_query("")

    # (a) Return type stays a dict — empty query must NOT collapse the
    #     contract to None / raise.
    assert isinstance(result, dict), (
        f"answer_query must return a dict even on empty query, "
        f"got {type(result).__name__}"
    )

    # (b) ``reply`` is a non-empty string (the fallback narrative).
    assert "reply" in result
    assert isinstance(result["reply"], str)
    assert len(result["reply"]) > 0, (
        "reply on empty query must be a non-empty fallback string"
    )

    # (c) The empty query is preserved verbatim in the response envelope.
    assert result["query"] == ""

    # (d) ``matched_markets`` is an empty list (no semantic hits).
    assert isinstance(result["matched_markets"], list)
    assert result["matched_markets"] == []

    # (e) ``timestamp`` is a float (unix epoch seconds).
    assert isinstance(result["timestamp"], float)


# ── (4) handles unknown token gracefully ────────────────────────────────────


async def test_analyze_market_handles_unknown_token(
    engine,
    fake_dependencies,
):
    """``analyze_market`` must not raise on an unknown ``token_id``.

    When ``store.get_order_book(token_id)`` returns ``None`` (the token
    has never been seen by the book poller / Gamma client), the engine's
    documented "initializing" fallback branch returns a payload with
    ``summary``, neutral ``sentiment``, ``Hold`` recommendation, and the
    model-metadata envelope — never raises.

    The conftest autouse ``_reset_store_factory_defaults`` fixture
    already clears ``store.order_books`` / ``store.market_slugs`` before
    every test, so the unknown token is genuinely absent. The defensive
    ``assert unknown_token not in store.order_books`` documents that
    assumption explicitly.
    """
    unknown_token = "TOK_X6_UNKNOWN_DOES_NOT_EXIST_4242"
    # Belt-and-braces: assert the token is genuinely absent from the store
    # (the conftest autouse reset already guarantees this, but the
    # explicit assertion documents the precondition for the fallback
    # branch we're about to exercise).
    assert unknown_token not in store.order_books
    assert unknown_token not in store.market_slugs

    # The load-bearing assertion: no exception is raised.
    result = await engine.analyze_market(unknown_token)

    # (a) Return type stays a dict.
    assert isinstance(result, dict), (
        f"analyze_market must return a dict even for unknown token, "
        f"got {type(result).__name__}"
    )

    # (b) The "initializing" fallback payload carries the documented
    #     fallback schema — a strict subset of the happy-path schema
    #     (no ``mid_price`` / ``spread`` / ``rationale`` / ``generated_at``
    #     because there is no book to derive them from).
    fallback_required_fields = {
        "token_id",
        "slug",
        "summary",
        "sentiment",
        "ml_probability",
        "net_edge",
        "confidence",
        "regime",
        "regime_tag",
        "recommendation",
        "risk_score",
        "feature_drivers",
        "model_metadata",
    }
    missing = fallback_required_fields - set(result.keys())
    assert not missing, (
        "analyze_market fallback payload missing required fields: "
        f"{sorted(missing)}"
    )

    # (c) Identity propagation — the unknown token is echoed back so the
    #     caller can correlate the response with the request.
    assert result["token_id"] == unknown_token

    # (d) The slug fallback is ``token_id[:16]`` when the token is not in
    #     ``store.market_slugs`` (the documented lookup default).
    assert result["slug"] == unknown_token[:16]

    # (e) The fallback explicitly returns neutral / Hold / Unknown
    #     defaults — the cold-start safety contract.
    assert result["sentiment"] == "Neutral"
    assert result["recommendation"] == "Hold"
    assert result["confidence"] == 0.0
    assert result["ml_probability"] == 0.5
    assert result["net_edge"] == 0.0
    assert result["regime"] == "Unknown"
    assert result["regime_tag"] == "unknown"
    assert result["risk_score"] == "Medium"
    assert result["feature_drivers"] == []

    # (f) ``summary`` is a non-empty string carrying the "initializing"
    #     narrative — the human-readable fallback explanation.
    assert isinstance(result["summary"], str)
    assert len(result["summary"]) > 0

    # (g) The model_metadata envelope still carries the governance trio
    #     even in the fallback path (the engine interpolates the live
    #     ``model_registry.active_version`` / ``ml_model.brier_score`` /
    #     ``ml_model.ece`` / ``drift_detector.drift_status`` regardless
    #     of whether the book is present).
    for md_field in ("version", "brier_score", "ece", "drift_status"):
        assert md_field in result["model_metadata"], (
            f"model_metadata (fallback path) missing required field: {md_field}"
        )


# ── (5) response contains market context ────────────────────────────────────


async def test_answer_query_reply_contains_market_context(
    engine,
    fake_dependencies,
):
    """``answer_query``'s ``reply`` must reference market context.

    Both branches of the reply are exercised here:

      (a) Matched-markets branch — when ``vector_store.search`` returns
          a non-empty result list, the reply template interpolates the
          TOP matched market's TITLE verbatim. The caller correlates
          the response with the right contract via that title.
      (b) Fallback scan branch — when ``vector_store.search`` returns
          ``[]`` (or the store is empty), the reply template emits the
          "actively monitoring N prediction markets" string. The reply
          must reference the word "market" so the response is
          recognisably about prediction markets rather than a generic
          LLM hallucination.
    """
    # ── (a) Matched-markets branch ─────────────────────────────────────────
    top_title = "Will the Fed cut rates in Q1 2026?"
    fake_vector_results = [
        (
            {
                "token_id": "TOK_X6_CTX_MATCH",
                "title": top_title,
                "slug": "fed-cut-q1-2026",
            },
            0.91,
        ),
        (
            {
                "token_id": "TOK_X6_CTX_MATCH_2",
                "title": "Will CPI print above 3% in Jan 2026?",
                "slug": "cpi-jan-2026",
            },
            0.78,
        ),
    ]
    with patch("ml.copilot.vector_store") as vs_mock:
        vs_mock.search.return_value = fake_vector_results
        result_matched = await engine.answer_query(
            "Tell me about Fed rate decisions"
        )

    reply_matched = result_matched["reply"]
    assert isinstance(reply_matched, str)
    # The TOP matched market's TITLE appears verbatim in the reply — the
    # load-bearing context cue the engine's matched-branch template
    # interpolates.
    assert top_title in reply_matched, (
        f"matched-branch reply must reference the top market's title "
        f"{top_title!r}; got: {reply_matched!r}"
    )
    # The structured ``matched_markets`` envelope also carries the context.
    assert len(result_matched["matched_markets"]) == 2
    assert result_matched["matched_markets"][0]["title"] == top_title
    assert result_matched["matched_markets"][0]["token_id"] == "TOK_X6_CTX_MATCH"

    # ── (b) Fallback scan branch ───────────────────────────────────────────
    with patch("ml.copilot.vector_store") as vs_mock:
        vs_mock.search.return_value = []
        result_fallback = await engine.answer_query(
            "Show me something interesting"
        )

    reply_fallback = result_fallback["reply"]
    assert isinstance(reply_fallback, str)
    # The fallback reply references the word "market" (from
    # "actively monitoring N prediction markets") — the general
    # market-context cue the fallback template emits.
    assert "market" in reply_fallback.lower(), (
        f"fallback reply must reference 'market' (the general context "
        f"cue); got: {reply_fallback!r}"
    )


async def test_analyze_market_rationale_contains_market_context(
    engine,
    fake_dependencies,
):
    """``analyze_market``'s ``rationale`` must embed market context.

    The rationale string is the human-readable analytical narrative the
    engine interpolates from the market's slug, mid price, spread, and
    regime. The load-bearing context cues:

      * The market SLUG appears verbatim in the rationale so the caller
        can correlate the analysis back to the right market.
      * The rationale references the price/spread dimension (the ``¢``
        unit on the mid price and the literal word "spread") — the
        primary quant context the engine interpolates into the template.
    """
    token_id = "TOK_X6_RATIONALE"
    slug = "btc-etf-approval-jan-2026"
    book = _make_book(token_id, mid=0.62, spread=0.015)
    await store.update_order_book(book)
    store.market_slugs[token_id] = slug

    result = await engine.analyze_market(
        token_id,
        market_dict={"slug": slug, "volume24hr": 10000.0},
    )

    rationale = result["rationale"]
    assert isinstance(rationale, str)
    assert len(rationale) > 0

    # (a) The market slug appears verbatim — the load-bearing
    #     correlation cue.
    assert slug in rationale, (
        f"rationale must reference the market slug {slug!r}; "
        f"got: {rationale!r}"
    )

    # (b) The rationale references the price/spread dimension — either
    #     the ``¢`` unit on the mid price or the literal word "spread"
    #     (the template interpolates both: ``{mid*100:.1f}¢`` and
    #     ``spread: {spread*100:.1f}¢``).
    price_context_present = (
        "¢" in rationale
        or "spread" in rationale.lower()
        or "pricing" in rationale.lower()
    )
    assert price_context_present, (
        f"rationale must reference price/spread context; got: {rationale!r}"
    )

    # (c) The ``model_metadata`` envelope also carries the model-version
    #     context — verified here as a belt-and-braces assertion so a
    #     future refactor that drops the slug from the rationale would
    #     still leave the caller with the model-version correlation
    #     channel.
    assert "version" in result["model_metadata"]
    assert isinstance(result["model_metadata"]["version"], str)
