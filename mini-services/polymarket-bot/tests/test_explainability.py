"""
tests/test_explainability.py — Unit + integration tests for ``ml/explainability.py``.

W17-3 — ML model explainability using SHAP values.

Covers the behaviours required by the W17-3 task spec:

  (1) ``PredictionExplanation`` dataclass — field contract, ``to_dict``
      JSON-able representation, top-features serialisation shape.

  (2) ``ModelExplainer.explain_tree_model`` — end-to-end SHAP attribution
      against a fitted ``RandomForestClassifier`` (real SHAP path),
      top-features sorted by absolute SHAP value, token_id propagation,
      prediction_direction matches ``predicted_probability``,
      ``confidence`` in ``[0, 1]``, 1-D input is reshaped to 2-D.

  (3) ``ModelExplainer._fallback_explanation`` — neutral direction,
      zero confidence, raw feature values as the contributions map,
      top-features sorted by absolute value.

  (4) SHAP-version-tolerant shape normalisation helpers —
      ``_normalise_shap_values`` (list / 3-D ndarray / 2-D ndarray /
      1-D ndarray) and ``_normalise_expected_value`` (list length 2 /
      list length 1 / scalar / numpy 0-D array).

  (5) ``ModelExplainer.explain_kernel`` — returns fallback when no
      background dataset has been set; returns real KernelExplainer
      output when ``set_background`` was called.

  (6) ``ModelExplainer.explain_prediction`` dispatcher — ``model_type=
      "tree"`` dispatches to ``explain_tree_model``; any other value
      dispatches to ``explain_kernel``.

  (7) SHAP-not-installed fallback — when ``import shap`` raises
      ``ImportError``, both ``explain_tree_model`` and ``explain_kernel``
      return the fallback explanation (rather than raising). Verified
      by monkey-patching ``sys.modules['shap'] = None`` (the standard
      trick for forcing an ImportError on ``import shap``).

  (8) ``MarketMLModel.compute_explanation`` integration — returns a
      ``PredictionExplanation`` for a fitted model, raises
      ``RuntimeError`` on an unfitted model, returns ``None`` on
      internal SHAP failure (mocked).

  (9) HTTP route ``GET /api/ml/explain/{token_id}`` — 404 for an
      unknown token (no stored feature vector), 422 for an empty
      token_id, 200 with the explanation payload for a known token
      whose feature vector has been seeded into the
      ``ml_feature_store`` table.

Module isolation
----------------
``ml/explainability.py`` is pure-Python + synchronous at the explainer
layer. The SHAP computation is delegated to the ``shap`` package which
is imported lazily inside each ``explain_*`` method — the module
imports cleanly even when ``shap`` is not installed (verified by the
SHAP-missing fallback tests).

For the API integration tests, a minimal ``FastAPI`` app is
constructed with only the explainability routes registered (mirrors
the ``client`` fixture pattern in ``tests/test_feature_store.py`` and
``tests/test_ab_testing.py``) so there's zero state leakage between
tests.

Sync ``def`` tests throughout — ``TestClient``'s sync portal manages
the FastAPI event loop. No ``pytestmark = pytest.mark.asyncio`` is
needed (mirrors ``tests/test_feature_store.py`` /
``tests/test_ab_testing.py`` sync-test convention).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

# ── Bootstrap project root on sys.path (defensive; conftest.py also does this). ──
# Lets this file be run in isolation via
# ``python -m pytest tests/test_explainability.py`` — the project root is
# always importable as top-level modules (``ml.*``) regardless of cwd.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (sys.path must be set first)
import pytest  # noqa: E402  (sys.path must be set first)

from ml.explainability import (  # noqa: E402
    ModelExplainer,
    PredictionExplanation,
    _normalise_expected_value,
    _normalise_shap_values,
    model_explainer,
    register_routes,
)


# =============================================================================
# Shared fixtures
# =============================================================================
def _make_rf(n_estimators: int = 10, n_features: int = 5) -> Any:
    """Build a tiny fitted ``RandomForestClassifier`` for unit tests.

    Returns a model trained on a 50-row synthetic dataset with a
    hand-crafted target (``y = (X[:, 0] > 0.5).astype(int)``) so the
    SHAP attributions concentrate on feature 0 — making the
    ``top_features[0]`` assertion deterministic.
    """
    from sklearn.ensemble import RandomForestClassifier

    rng = np.random.RandomState(0)
    X = rng.rand(50, n_features)
    y = (X[:, 0] > 0.5).astype(int)
    return RandomForestClassifier(
        n_estimators=n_estimators, random_state=0,
    ).fit(X, y)


def _make_gb(n_estimators: int = 10, n_features: int = 5) -> Any:
    """Build a tiny fitted ``GradientBoostingClassifier`` for unit tests.

    The GB path exercises a different SHAP output shape (2-D ndarray
    of shape ``(n_samples, n_features)`` vs the RF's 3-D
    ``(n_samples, n_features, n_classes)``) so we can verify the
    normalisation helper handles both shapes.
    """
    from sklearn.ensemble import GradientBoostingClassifier

    rng = np.random.RandomState(0)
    X = rng.rand(50, n_features)
    y = (X[:, 0] > 0.5).astype(int)
    return GradientBoostingClassifier(
        n_estimators=n_estimators, random_state=0,
    ).fit(X, y)


@pytest.fixture
def explainer() -> ModelExplainer:
    """Return a fresh ``ModelExplainer`` per test (no shared state).

    The module-level ``model_explainer`` singleton is intentionally
    NOT used here so a test that calls ``set_background`` doesn't leak
    background data into a sibling test.
    """
    return ModelExplainer()


@pytest.fixture
def fitted_model():
    """A freshly-trained ``MarketMLModel()`` instance trained on a
    small synthetic dataset (100 rows, 10 estimators per ensemble
    member) so per-test ``fit_initial`` wall-time is ~1s rather than
    ~25s.

    Mirrors the ``fitted_model`` fixture in ``tests/test_ml_model.py``
    (mocks ``timescale_db.fetch_training_samples`` to return
    ``(None, [])`` so the model trains on synthetic data only).
    """
    from ml.model import MarketMLModel, _synthetic_training_data

    def _small_synth(n: int = 100) -> tuple[np.ndarray, np.ndarray]:
        return _synthetic_training_data(100)

    with patch("ml.model._synthetic_training_data", _small_synth), \
         patch("core.timescale_db.timescale_db") as mock_db:
        mock_db.fetch_training_samples.return_value = (None, [])
        m = MarketMLModel()
        m.fit_initial(n_estimators_rf=10, n_estimators_gb=10)
    return m


@pytest.fixture
def client(tmp_path):
    """Return a ``TestClient`` against a minimal FastAPI app with only
    the explainability routes registered.

    The explainability route handlers close over the module-level
    ``ml_model`` / ``model_explainer`` singletons, so this fixture
    doesn't need to patch them — the singletons are constructed at
    import time (``ml_model`` is loaded by ``ml.model``'s module-level
    ``MarketMLModel.load_or_create()`` call which the conftest env
    redirect points at ``/tmp/pmbot_conftest_isolation/model.pkl``).

    A fresh ``TestClient`` per test means no shared route table state
    between tests (mirrors the ``client`` fixture in
    ``tests/test_feature_store.py``).
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    register_routes(app)
    return TestClient(app)


def _seed_ml_feature_store(token_id: str, features: np.ndarray) -> None:
    """Insert one row into the ``ml_feature_store`` table so the
    explainability route's feature-vector lookup finds it.

    Writes directly to the SQLite file the route reads from
    (``timescale_db._sqlite_path``) so the route handler's
    ``sqlite3.connect(timescale_db._sqlite_path)`` call sees the
    seeded row. Schema mirrors the production ``ml_feature_store``
    table (id / timestamp / token_id / features_json / p_pred /
    confidence / outcome_resolved).
    """
    import json
    import sqlite3
    import time

    from core.timescale_db import timescale_db

    features_json = json.dumps([float(x) for x in features])
    with sqlite3.connect(timescale_db._sqlite_path) as conn:
        conn.execute(
            "INSERT INTO ml_feature_store "
            "(timestamp, token_id, features_json, p_pred, confidence, outcome_resolved) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), token_id, features_json, 0.55, 0.1, None),
        )
        conn.commit()


# =============================================================================
# (1) PredictionExplanation dataclass
# =============================================================================
class TestPredictionExplanation:
    """Verify the ``PredictionExplanation`` dataclass contract."""

    def test_dataclass_fields_match_contract(self):
        """The dataclass exposes the seven documented fields with the
        documented types — ``token_id: str`` / ``predicted_probability:
        float`` / ``base_value: float`` / ``feature_contributions:
        dict[str, float]`` / ``top_features: list[tuple[str, float]]`` /
        ``prediction_direction: str`` / ``confidence: float``."""
        expl = PredictionExplanation(
            token_id="tok1",
            predicted_probability=0.7,
            base_value=0.5,
            feature_contributions={"a": 0.1, "b": -0.05},
            top_features=[("a", 0.1), ("b", -0.05)],
            prediction_direction="positive",
            confidence=0.4,
        )
        assert expl.token_id == "tok1"
        assert expl.predicted_probability == 0.7
        assert expl.base_value == 0.5
        assert isinstance(expl.feature_contributions, dict)
        assert expl.feature_contributions == {"a": 0.1, "b": -0.05}
        assert isinstance(expl.top_features, list)
        assert all(isinstance(t, tuple) and len(t) == 2 for t in expl.top_features)
        assert expl.prediction_direction == "positive"
        assert expl.confidence == 0.4

    def test_to_dict_returns_json_able_representation(self):
        """``to_dict`` returns a plain dict (no tuple / numpy / dataclass
        values) so ``json.dumps`` doesn't choke on the inner types."""
        expl = PredictionExplanation(
            token_id="tok1",
            predicted_probability=0.7,
            base_value=0.5,
            feature_contributions={"a": 0.1, "b": -0.05},
            top_features=[("a", 0.1), ("b", -0.05)],
            prediction_direction="positive",
            confidence=0.4,
        )
        d = expl.to_dict()
        assert isinstance(d, dict)
        # Every value must be JSON-serialisable — verify by attempting the dump
        import json
        json.dumps(d)  # raises TypeError if anything isn't serialisable

    def test_to_dict_top_features_serialised_as_dicts(self):
        """``top_features`` is converted from ``list[tuple[str, float]]``
        to ``list[{"feature": str, "shap_value": float}]`` so a downstream
        JSON serialiser / Pydantic response model gets explicit key names
        (a tuple would silently become a list of mixed types under
        ``json.dumps``)."""
        expl = PredictionExplanation(
            token_id="t",
            predicted_probability=0.5,
            base_value=0.5,
            feature_contributions={"a": 0.1, "b": -0.05},
            top_features=[("a", 0.1), ("b", -0.05)],
            prediction_direction="positive",
            confidence=0.0,
        )
        d = expl.to_dict()
        assert d["top_features"] == [
            {"feature": "a", "shap_value": 0.1},
            {"feature": "b", "shap_value": -0.05},
        ]

    def test_to_dict_preserves_feature_contributions(self):
        """``feature_contributions`` is preserved verbatim in ``to_dict``
        (a shallow copy so a caller mutating the dict doesn't back-propagate
        to the dataclass)."""
        contribs = {"mid_price": 0.3, "spread": -0.1}
        expl = PredictionExplanation(
            token_id="t", predicted_probability=0.6, base_value=0.4,
            feature_contributions=contribs, top_features=[("mid_price", 0.3)],
            prediction_direction="positive", confidence=0.2,
        )
        d = expl.to_dict()
        assert d["feature_contributions"] == contribs
        # Mutating the dict from to_dict should NOT affect the dataclass
        d["feature_contributions"]["mid_price"] = 999.0
        assert expl.feature_contributions["mid_price"] == 0.3


# =============================================================================
# (2) explain_tree_model — real SHAP path
# =============================================================================
class TestExplainTreeModel:
    """Verify ``ModelExplainer.explain_tree_model`` end-to-end."""

    def test_explain_tree_model_returns_one_explanation_per_row(self, explainer):
        """A call with ``X.shape == (3, n_features)`` returns a list of
        3 ``PredictionExplanation`` records — one per input row."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(3, 5)
        feat_names = ["a", "b", "c", "d", "e"]
        results = explainer.explain_tree_model(rf, X, feat_names, token_id="tok1")
        assert isinstance(results, list)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, PredictionExplanation)

    def test_explain_tree_model_predicted_probability_in_unit_interval(self, explainer):
        """``predicted_probability`` is the model's ``predict_proba`` for
        the positive class — always in ``[0, 1]``."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(5, 5)
        results = explainer.explain_tree_model(rf, X, ["a", "b", "c", "d", "e"])
        for r in results:
            assert 0.0 <= r.predicted_probability <= 1.0

    def test_explain_tree_model_base_value_is_finite(self, explainer):
        """``base_value`` is the SHAP expected value (mean model output
        over the background dataset) — a finite float."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(3, 5)
        results = explainer.explain_tree_model(rf, X, ["a", "b", "c", "d", "e"])
        for r in results:
            assert isinstance(r.base_value, float)
            assert np.isfinite(r.base_value)

    def test_explain_tree_model_feature_contributions_named_correctly(self, explainer):
        """``feature_contributions`` keys are the ``feature_names`` list
        passed by the caller — not positional indices."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(2, 5)
        feat_names = ["mid_price", "spread", "ofi", "depth", "vol"]
        results = explainer.explain_tree_model(rf, X, feat_names)
        for r in results:
            assert set(r.feature_contributions.keys()) == set(feat_names)

    def test_explain_tree_model_top_features_sorted_by_abs_shap_desc(self, explainer):
        """``top_features`` is sorted by ABSOLUTE SHAP value descending
        — the feature with the largest magnitude attribution is first,
        regardless of sign."""
        rf = _make_rf(n_features=5)
        # Pick a row where feature 0 dominates (the model was trained on
        # ``X[:, 0] > 0.5`` so SHAP should attribute most of the
        # probability mass to feature 0).
        X = np.array([[0.9, 0.5, 0.5, 0.5, 0.5]])
        feat_names = ["a", "b", "c", "d", "e"]
        results = explainer.explain_tree_model(rf, X, feat_names)
        r = results[0]
        # Verify descending-sort invariant: |top[i]| >= |top[i+1]|
        abs_values = [abs(v) for _, v in r.top_features]
        assert abs_values == sorted(abs_values, reverse=True), (
            f"top_features not sorted by abs: {r.top_features}"
        )

    def test_explain_tree_model_top_features_capped_at_10(self, explainer):
        """``top_features`` is capped at 10 entries regardless of the
        feature-count of the underlying model (matches the spec)."""
        rf = _make_rf(n_features=15)
        X = np.random.RandomState(1).rand(2, 15)
        feat_names = [f"f{i}" for i in range(15)]
        results = explainer.explain_tree_model(rf, X, feat_names)
        for r in results:
            assert len(r.top_features) <= 10

    def test_explain_tree_model_token_id_propagated(self, explainer):
        """The ``token_id`` argument is propagated into every returned
        ``PredictionExplanation`` record verbatim."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(3, 5)
        results = explainer.explain_tree_model(
            rf, X, ["a", "b", "c", "d", "e"], token_id="abc-123",
        )
        for r in results:
            assert r.token_id == "abc-123"

    def test_explain_tree_model_prediction_direction_matches_probability(self, explainer):
        """``prediction_direction`` is ``"positive"`` when
        ``predicted_probability > 0.5`` and ``"negative"`` otherwise."""
        rf = _make_rf(n_features=5)
        # Row 1 has feature 0 = 0.9 → probability > 0.5 (positive)
        # Row 2 has feature 0 = 0.1 → probability < 0.5 (negative)
        X = np.array([
            [0.9, 0.5, 0.5, 0.5, 0.5],
            [0.1, 0.5, 0.5, 0.5, 0.5],
        ])
        results = explainer.explain_tree_model(
            rf, X, ["a", "b", "c", "d", "e"],
        )
        assert len(results) == 2
        # Verify the direction invariant against the predicted probability
        for r in results:
            if r.predicted_probability > 0.5:
                assert r.prediction_direction == "positive"
            else:
                assert r.prediction_direction == "negative"

    def test_explain_tree_model_confidence_in_zero_one_range(self, explainer):
        """``confidence == abs(predicted_probability - 0.5) * 2`` —
        always in ``[0, 1]``."""
        rf = _make_rf(n_features=5)
        X = np.random.RandomState(1).rand(4, 5)
        results = explainer.explain_tree_model(
            rf, X, ["a", "b", "c", "d", "e"],
        )
        for r in results:
            assert 0.0 <= r.confidence <= 1.0
            expected_conf = abs(r.predicted_probability - 0.5) * 2
            assert abs(r.confidence - expected_conf) < 1e-9

    def test_explain_tree_model_1d_input_reshaped_to_2d(self, explainer):
        """A 1-D ndarray input (single row, no batch dimension) is
        reshaped to ``(1, n_features)`` internally so the explainer
        returns a 1-element list rather than raising."""
        rf = _make_rf(n_features=5)
        x_1d = np.array([0.7, 0.5, 0.5, 0.5, 0.5])
        results = explainer.explain_tree_model(
            rf, x_1d, ["a", "b", "c", "d", "e"], token_id="tok1",
        )
        assert len(results) == 1
        assert results[0].token_id == "tok1"

    def test_explain_tree_model_works_with_gradient_boosting(self, explainer):
        """``explain_tree_model`` accepts ``GradientBoostingClassifier``
        too — SHAP's TreeExplainer returns a 2-D ndarray for GB (single
        output) and the normalisation helper collapses it correctly."""
        gb = _make_gb(n_features=5)
        X = np.random.RandomState(1).rand(3, 5)
        results = explainer.explain_tree_model(
            gb, X, ["a", "b", "c", "d", "e"], token_id="gb_tok",
        )
        assert len(results) == 3
        for r in results:
            assert set(r.feature_contributions.keys()) == {"a", "b", "c", "d", "e"}


# =============================================================================
# (3) _fallback_explanation
# =============================================================================
class TestFallbackExplanation:
    """Verify the fallback path (used when SHAP isn't available / fails)."""

    def test_fallback_returns_neutral_direction(self, explainer):
        """Fallback explanations use ``"neutral"`` as the
        ``prediction_direction`` — distinguishing them from real SHAP
        explanations (which are always ``"positive"`` or ``"negative"``)."""
        X = np.array([[0.5, 0.6, 0.7]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        assert len(results) == 1
        assert results[0].prediction_direction == "neutral"

    def test_fallback_returns_zero_confidence(self, explainer):
        """Fallback explanations hard-zero ``confidence`` so the caller /
        dashboard can distinguish a real SHAP explanation from a fallback."""
        X = np.array([[0.5, 0.6, 0.7]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        assert results[0].confidence == 0.0

    def test_fallback_predicted_probability_is_0_5(self, explainer):
        """Fallback explanations report ``predicted_probability = 0.5``
        (maximally uncertain) rather than fabricating a probability."""
        X = np.array([[0.5, 0.6, 0.7]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        assert results[0].predicted_probability == 0.5

    def test_fallback_base_value_is_0_5(self, explainer):
        """Fallback ``base_value`` is ``0.5`` — matches the neutral
        ``predicted_probability``."""
        X = np.array([[0.5, 0.6, 0.7]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        assert results[0].base_value == 0.5

    def test_fallback_uses_raw_feature_values_as_contributions(self, explainer):
        """The fallback uses the raw feature VALUES as the contributions
        map — not ideal as a SHAP substitute but provides a structured
        view of the input feature distribution."""
        X = np.array([[0.1, 0.2, 0.3]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        assert results[0].feature_contributions == {"a": 0.1, "b": 0.2, "c": 0.3}

    def test_fallback_top_features_sorted_by_abs_value(self, explainer):
        """Fallback ``top_features`` is sorted by absolute value descending
        — same invariant as the real SHAP path."""
        X = np.array([[0.1, -0.5, 0.3]])
        results = explainer._fallback_explanation(X, ["a", "b", "c"], "tok")
        r = results[0]
        abs_values = [abs(v) for _, v in r.top_features]
        assert abs_values == sorted(abs_values, reverse=True)

    def test_fallback_1d_input_reshaped_to_2d(self, explainer):
        """A 1-D ndarray input is reshaped to ``(1, n_features)``."""
        x_1d = np.array([0.1, 0.2, 0.3])
        results = explainer._fallback_explanation(x_1d, ["a", "b", "c"], "tok")
        assert len(results) == 1
        assert results[0].token_id == "tok"

    def test_fallback_token_id_propagated(self, explainer):
        """The ``token_id`` argument is propagated verbatim."""
        X = np.array([[0.5, 0.6]])
        results = explainer._fallback_explanation(X, ["a", "b"], "fallback-tok")
        for r in results:
            assert r.token_id == "fallback-tok"


# =============================================================================
# (4) Shape-normalisation helpers
# =============================================================================
class TestNormaliseHelpers:
    """Verify the SHAP-version-tolerant shape normalisation helpers."""

    def test_normalise_shap_values_list_form_legacy(self):
        """Legacy SHAP 0.4x output: ``list[np.ndarray]`` of length 2 —
        index 1 is the positive class. Normaliser returns ``shap_values[1]``."""
        sv = [np.zeros((3, 5)), np.ones((3, 5))]  # class 0 zeros, class 1 ones
        result = _normalise_shap_values(sv)
        assert isinstance(result, np.ndarray)
        assert result.shape == (3, 5)
        # All values should be 1.0 (we took index 1)
        assert np.all(result == 1.0)

    def test_normalise_shap_values_3d_array_modern_rf(self):
        """Modern SHAP 0.5x output for ``RandomForestClassifier``:
        3-D ndarray of shape ``(n_samples, n_features, n_classes)`` —
        normaliser slices ``[:, :, 1]`` for the positive class."""
        sv = np.zeros((3, 5, 2))
        sv[:, :, 1] = 1.0  # positive class
        result = _normalise_shap_values(sv)
        assert isinstance(result, np.ndarray)
        assert result.shape == (3, 5)
        assert np.all(result == 1.0)

    def test_normalise_shap_values_2d_array_gb(self):
        """Single-output model (``GradientBoostingClassifier``):
        2-D ndarray of shape ``(n_samples, n_features)`` — normaliser
        passes through unchanged."""
        sv = np.ones((3, 5))
        result = _normalise_shap_values(sv)
        assert isinstance(result, np.ndarray)
        assert result.shape == (3, 5)
        assert np.all(result == 1.0)

    def test_normalise_shap_values_1d_array_degenerate(self):
        """Degenerate single-row output (1-D ndarray): reshaped to
        ``(1, n_features)`` so the downstream row-indexing logic works."""
        sv = np.array([0.1, 0.2, 0.3])
        result = _normalise_shap_values(sv)
        assert isinstance(result, np.ndarray)
        assert result.shape == (1, 3)

    def test_normalise_expected_value_list_len_2(self):
        """``expected_value`` as a length-2 list/array (RF case):
        normaliser returns ``ev[1]`` (positive class)."""
        ev = [0.4, 0.6]
        assert _normalise_expected_value(ev) == 0.6

    def test_normalise_expected_value_list_len_1(self):
        """``expected_value`` as a length-1 list/array (GB case):
        normaliser returns ``ev[0]``."""
        ev = [0.5]
        assert _normalise_expected_value(ev) == 0.5

    def test_normalise_expected_value_scalar(self):
        """``expected_value`` as a scalar: normaliser returns
        ``float(value)``."""
        assert _normalise_expected_value(0.42) == 0.42

    def test_normalise_expected_value_numpy_0d_array(self):
        """``expected_value`` as a numpy 0-D array: normaliser returns
        ``float(value)``."""
        ev = np.float64(0.33)
        assert _normalise_expected_value(ev) == 0.33

    def test_normalise_expected_value_empty_returns_default(self):
        """An empty list/array returns ``0.5`` (neutral default) so the
        fallback never raises a ``ValueError``."""
        assert _normalise_expected_value([]) == 0.5
        assert _normalise_expected_value(np.array([])) == 0.5


# =============================================================================
# (5) explain_kernel (KernelExplainer)
# =============================================================================
class TestExplainKernel:
    """Verify the model-agnostic KernelExplainer path."""

    def test_explain_kernel_without_background_returns_fallback(self, explainer):
        """When ``set_background`` has NOT been called, ``explain_kernel``
        logs a warning and returns the fallback explanation (so a caller
        that forgot to set the background still gets a valid response
        rather than a ``ValueError``)."""
        rf = _make_rf(n_features=5)

        def predict_fn(x):
            return rf.predict_proba(x)

        X = np.random.RandomState(1).rand(2, 5)
        results = explainer.explain_kernel(
            predict_fn, X, ["a", "b", "c", "d", "e"], token_id="tok1",
        )
        assert len(results) == 2
        # All fallback records have direction "neutral" and confidence 0.0
        for r in results:
            assert r.prediction_direction == "neutral"
            assert r.confidence == 0.0

    def test_explain_kernel_with_background_returns_explanations(self, explainer):
        """When ``set_background`` HAS been called, ``explain_kernel``
        runs the real KernelExplainer and returns one
        ``PredictionExplanation`` per input row with non-fallback
        direction / confidence."""
        rf = _make_rf(n_features=5)
        bg = np.random.RandomState(0).rand(20, 5)
        explainer.set_background(bg, feature_names=["a", "b", "c", "d", "e"])

        def predict_fn(x):
            return rf.predict_proba(x)

        X = np.random.RandomState(1).rand(2, 5)
        results = explainer.explain_kernel(
            predict_fn, X, ["a", "b", "c", "d", "e"], token_id="k-tok",
        )
        assert len(results) == 2
        for r in results:
            # Real KernelExplainer output should NOT be the fallback
            assert r.prediction_direction in ("positive", "negative")
            assert r.token_id == "k-tok"
            assert set(r.feature_contributions.keys()) == {"a", "b", "c", "d", "e"}

    def test_set_background_samples_down_to_100_rows(self, explainer):
        """``set_background`` samples down to 100 rows (without
        replacement) when the input exceeds 100 — KernelExplainer's
        runtime is ``O(n_background * n_samples)`` so capping the
        background at 100 keeps the latency tractable."""
        X = np.random.RandomState(0).rand(250, 5)
        explainer.set_background(X)
        assert explainer._background is not None
        assert explainer._background.shape == (100, 5)

    def test_set_background_passes_through_when_under_100(self, explainer):
        """``set_background`` passes the input through unchanged when
        the input has fewer than 100 rows (no subsampling needed)."""
        X = np.random.RandomState(0).rand(40, 5)
        explainer.set_background(X)
        assert explainer._background is not None
        assert explainer._background.shape == (40, 5)


# =============================================================================
# (6) explain_prediction dispatcher
# =============================================================================
class TestExplainPredictionDispatcher:
    """Verify ``explain_prediction`` dispatches to the right explainer
    based on the ``model_type`` argument."""

    def test_explain_prediction_tree_model_type_dispatches_to_tree(self, explainer):
        """``model_type="tree"`` dispatches to ``explain_tree_model``.
        Verified by checking the result is a real SHAP explanation
        (non-neutral direction) for a fitted RF."""
        rf = _make_rf(n_features=5)
        X = np.array([[0.9, 0.5, 0.5, 0.5, 0.5]])
        results = explainer.explain_prediction(
            rf, X, ["a", "b", "c", "d", "e"],
            token_id="t", model_type="tree",
        )
        assert len(results) == 1
        # Real SHAP path → direction is positive or negative, not neutral
        assert results[0].prediction_direction in ("positive", "negative")

    def test_explain_prediction_kernel_model_type_dispatches_to_kernel(self, explainer):
        """``model_type="kernel"`` (or any non-"tree" value) dispatches
        to ``explain_kernel``. Verified by setting a background and
        checking the result is a real KernelExplainer explanation."""
        rf = _make_rf(n_features=5)
        bg = np.random.RandomState(0).rand(20, 5)
        explainer.set_background(bg, feature_names=["a", "b", "c", "d", "e"])
        X = np.array([[0.9, 0.5, 0.5, 0.5, 0.5]])
        results = explainer.explain_prediction(
            rf, X, ["a", "b", "c", "d", "e"],
            token_id="t", model_type="kernel",
        )
        assert len(results) == 1
        # KernelExplainer path → direction is positive or negative
        assert results[0].prediction_direction in ("positive", "negative")


# =============================================================================
# (7) SHAP-not-installed fallback
# =============================================================================
class TestSHAPMissingFallback:
    """Verify graceful degradation when the ``shap`` package isn't installed.

    Uses ``sys.modules['shap'] = None`` — the standard CPython idiom for
    forcing ``import shap`` to raise ``ImportError`` (verified in the
    Python docs: when ``sys.modules[name]`` is ``None``, the import
    system raises ``ImportError`` rather than re-attempting the import).
    """

    def test_module_imports_without_shap(self, monkeypatch):
        """The ``ml.explainability`` module imports cleanly even when
        ``shap`` isn't installed — the ``import shap`` statements are
        deferred to method bodies so module-load never triggers the
        ``ImportError``."""
        # Force ``import shap`` to raise ImportError
        monkeypatch.setitem(sys.modules, "shap", None)
        # Re-import the module — if any module-level ``import shap``
        # existed, this would raise ImportError.
        import importlib

        import ml.explainability as expl_module
        importlib.reload(expl_module)
        # The module's public symbols must still be accessible
        assert hasattr(expl_module, "ModelExplainer")
        assert hasattr(expl_module, "PredictionExplanation")
        assert hasattr(expl_module, "model_explainer")
        assert hasattr(expl_module, "register_routes")

    def test_explain_tree_model_falls_back_when_shap_import_fails(self, explainer, monkeypatch):
        """When ``import shap`` raises ``ImportError`` inside
        ``explain_tree_model``, the method catches it and returns the
        fallback explanation (rather than propagating the ImportError)."""
        monkeypatch.setitem(sys.modules, "shap", None)
        rf = _make_rf(n_features=5)
        X = np.array([[0.9, 0.5, 0.5, 0.5, 0.5]])
        results = explainer.explain_tree_model(
            rf, X, ["a", "b", "c", "d", "e"], token_id="t",
        )
        assert len(results) == 1
        # Fallback records have direction "neutral" and confidence 0.0
        assert results[0].prediction_direction == "neutral"
        assert results[0].confidence == 0.0

    def test_explain_kernel_falls_back_when_shap_import_fails(self, explainer, monkeypatch):
        """When ``import shap`` raises ``ImportError`` inside
        ``explain_kernel``, the method catches it and returns the
        fallback explanation."""
        monkeypatch.setitem(sys.modules, "shap", None)
        rf = _make_rf(n_features=5)
        bg = np.random.RandomState(0).rand(20, 5)
        explainer.set_background(bg, feature_names=["a", "b", "c", "d", "e"])

        def predict_fn(x):
            return rf.predict_proba(x)

        X = np.array([[0.9, 0.5, 0.5, 0.5, 0.5]])
        results = explainer.explain_kernel(
            predict_fn, X, ["a", "b", "c", "d", "e"], token_id="t",
        )
        assert len(results) == 1
        assert results[0].prediction_direction == "neutral"
        assert results[0].confidence == 0.0


# =============================================================================
# (8) MarketMLModel.compute_explanation integration
# =============================================================================
class TestMarketMLModelComputeExplanation:
    """Verify the integration with ``ml.model.MarketMLModel``."""

    def test_compute_explanation_returns_prediction_explanation(self, fitted_model):
        """``MarketMLModel.compute_explanation()`` returns a
        ``PredictionExplanation`` for a fitted model — end-to-end SHAP
        attribution against the production ensemble's RF member."""
        from ml.explainability import PredictionExplanation
        from ml.features import FEATURE_NAMES, N_FEATURES

        features = np.zeros(N_FEATURES, dtype=np.float32)
        features[0] = 0.5  # mid_price
        result = fitted_model.compute_explanation(features, token_id="t1", top_n=5)
        assert isinstance(result, PredictionExplanation)
        # The model's blended prediction overwrites the RF-only prob
        assert 0.01 <= result.predicted_probability <= 0.99
        assert result.token_id == "t1"
        # All 38 features named in the contributions
        assert set(result.feature_contributions.keys()) == set(FEATURE_NAMES)
        # top_features capped at the requested ``top_n``
        assert len(result.top_features) <= 5

    def test_compute_explanation_raises_on_unfitted_model(self):
        """``compute_explanation()`` raises ``RuntimeError`` on an
        unfitted model (``self.rf is None``) — the same contract as
        ``predict_proba_raw()``."""
        from ml.model import MarketMLModel

        m = MarketMLModel()
        # Don't call fit_initial — leave rf / gb as None
        with pytest.raises(RuntimeError, match="unfitted"):
            m.compute_explanation(np.zeros(38, dtype=np.float32))

    def test_compute_explanation_returns_none_on_shap_failure(self, fitted_model, monkeypatch):
        """When SHAP raises an unexpected exception (e.g. an internal
        ValueError on a malformed input), ``compute_explanation`` returns
        ``None`` rather than propagating the exception — defensive
        contract so a transient SHAP issue never 500s the API route."""
        from ml.features import N_FEATURES

        # Patch ``model_explainer.explain_tree_model`` to raise — simulates
        # an internal SHAP error (e.g. a corrupted model state).
        def _raise(*args, **kwargs):
            raise RuntimeError("simulated SHAP failure")

        with patch(
            "ml.explainability.model_explainer.explain_tree_model",
            side_effect=_raise,
        ):
            features = np.zeros(N_FEATURES, dtype=np.float32)
            features[0] = 0.5
            result = fitted_model.compute_explanation(features, token_id="t2")
        # The method catches the exception and returns None
        assert result is None


# =============================================================================
# (9) HTTP route GET /api/ml/explain/{token_id}
# =============================================================================
class TestAPIRoute:
    """Verify the ``GET /api/ml/explain/{token_id}`` HTTP route."""

    def test_route_returns_422_for_empty_token_id(self, client):
        """An empty ``token_id`` (or whitespace-only) is rejected with
        HTTP 422 — the route's defensive ``if not token_id.strip()``
        guard short-circuits before any DB lookup."""
        # FastAPI's path matching treats ``/api/ml/explain/`` as a 404
        # (no path parameter); the empty-string check is exercised by
        # a whitespace-only token_id (which FastAPI accepts as a valid
        # path param).
        resp = client.get("/api/ml/explain/%20")
        assert resp.status_code == 422

    def test_route_returns_404_for_unknown_token(self, client):
        """A token_id with no stored feature vector returns HTTP 404
        — the route can only explain a prediction that has been
        recorded (the model must predict for this token at least once
        before an explanation is available)."""
        # Use a token_id that definitely doesn't exist in the
        # ml_feature_store table (UUID-style suffix guarantees uniqueness
        # even when sibling tests seed other token_ids).
        unknown_token = "no-such-token-1234567890-abcdef"
        resp = client.get(f"/api/ml/explain/{unknown_token}")
        assert resp.status_code == 404
        assert "no stored feature vector" in resp.json()["detail"].lower()

    def test_route_returns_explanation_for_known_token(self, client):
        """A token_id whose feature vector has been seeded into the
        ``ml_feature_store`` table returns HTTP 200 with the
        explanation payload — the happy path."""
        from ml.features import FEATURE_NAMES, N_FEATURES

        # Seed the feature store with a row for a unique token_id
        token_id = "test-token-explain-known-12345"
        features = np.zeros(N_FEATURES, dtype=np.float32)
        features[0] = 0.5
        _seed_ml_feature_store(token_id, features)

        resp = client.get(f"/api/ml/explain/{token_id}")
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        assert body["token_id"] == token_id
        assert "explanation" in body
        expl = body["explanation"]
        assert expl["token_id"] == token_id
        assert "predicted_probability" in expl
        assert "base_value" in expl
        assert "feature_contributions" in expl
        assert "top_features" in expl
        assert "prediction_direction" in expl
        assert "confidence" in expl
        # All 38 features named in the contributions
        assert set(expl["feature_contributions"].keys()) == set(FEATURE_NAMES)
        # top_features is a list of {"feature": str, "shap_value": float} dicts
        assert isinstance(expl["top_features"], list)
        if expl["top_features"]:
            assert "feature" in expl["top_features"][0]
            assert "shap_value" in expl["top_features"][0]

    def test_route_top_n_param_trims_top_features(self, client):
        """The ``top_n`` query parameter (1 ≤ N ≤ 38) trims the
        ``top_features`` list to the requested count."""
        from ml.features import N_FEATURES

        token_id = "test-token-explain-topn-67890"
        features = np.zeros(N_FEATURES, dtype=np.float32)
        features[0] = 0.5
        _seed_ml_feature_store(token_id, features)

        resp = client.get(f"/api/ml/explain/{token_id}?top_n=3")
        assert resp.status_code == 200
        body = resp.json()
        # top_features trimmed to at most 3 entries
        assert len(body["explanation"]["top_features"]) <= 3

    def test_route_predicted_probability_matches_ensemble_blend(self, client):
        """The ``predicted_probability`` in the response matches the
        ensemble's blended output (via ``ml_model.predict()``) — NOT
        the RF-only TreeExplainer probability. This is the W17-3
        correctness invariant: the explanation's headline number must
        match the dashboard's headline number for the same token."""
        from ml.features import N_FEATURES
        from ml.model import ml_model

        token_id = "test-token-explain-ensemble-match-abc"
        features = np.zeros(N_FEATURES, dtype=np.float32)
        features[0] = 0.5
        _seed_ml_feature_store(token_id, features)

        # Compute the ensemble's blended prediction for the same feature
        # vector — must match the route's response.
        expected_p, _ = ml_model.predict(features, token_id=token_id)

        resp = client.get(f"/api/ml/explain/{token_id}")
        assert resp.status_code == 200
        actual_p = resp.json()["explanation"]["predicted_probability"]
        # Allow a tiny epsilon for float precision / clip differences
        assert abs(actual_p - expected_p) < 1e-6, (
            f"route predicted_probability {actual_p} != ensemble {expected_p}"
        )
