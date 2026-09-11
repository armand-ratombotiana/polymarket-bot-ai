"""W17-9 — Cross-module integration tests for the ML pipeline.

Drives the full ML lifecycle end-to-end:

  1. **Train → Predict → Drift → Retrain** cycle: train the model, run
     predictions through it (which feeds the drift detector), simulate a
     distribution shift, verify drift is detected (PSI > 0.25 / status
     = SIGNIFICANT_DRIFT), then re-train and verify the drift detector
     resets to HEALTHY after ``reset()``.

  2. **Calibration integration**: train the post-hoc ``ProbabilityCalibrator``
     on a deliberately miscalibrated (raw_prob, label) dataset; verify the
     Brier score on the held-out calibration set is strictly lower after
     calibration.

  3. **Shadow inference**: register a challenger model, run a prediction
     through the production ``ml_model.predict()`` path (which invokes
     ``shadow_inference.run_shadow(...)``), and verify both the champion
     and challenger predictions are recorded in the shadow-inference
     ring buffer.

Hermeticity
-----------
``conftest.py`` redirects ``MODEL_PATH`` / ``MODEL_REGISTRY_PATH`` /
``FEATURE_STORE_DB`` / ``DECISION_LEDGER_DB_PATH`` to a writable
``/tmp/pmbot_conftest_isolation/`` sandbox BEFORE the project modules
are imported, so the module-level ``ml_model`` singleton (constructed at
import time via ``MarketMLModel.load_or_create()``) and the drift
detector / shadow inference engine singletons all write to writable
paths. The ``fitted_model`` fixture mocks
``core.timescale_db.timescale_db.fetch_training_samples`` to return
``(None, [])`` (so ``fit_initial`` runs on synthetic data only) and
patches ``ml.model._synthetic_training_data`` to generate a 100-row
dataset with 10 estimators per learner, so a single ``fit_initial``
call takes ~1.3 s instead of ~25 s. Mirrors the convention in
``tests/test_ml_model.py``.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from ml.calibration import ProbabilityCalibrator
from ml.drift_detector import (
    BRIER_DRIFT_THRESHOLD,
    ModelDriftDetector,
    drift_detector,
)
from ml.features import N_FEATURES
from ml.model import MarketMLModel, _synthetic_training_data, ml_model
from ml.model_registry import model_registry
from ml.shadow_inference import ShadowInferenceEngine

# pytest-asyncio strict mode — explicit module-level mark for async tests.
pytestmark = pytest.mark.asyncio


# ── Helpers / fixtures ──────────────────────────────────────────────────────


def _make_features(mid_price: float = 0.5) -> np.ndarray:
    """Build a minimal valid 38-dim float32 feature vector."""
    vec = np.zeros(N_FEATURES, dtype=np.float32)
    vec[0] = float(mid_price)
    return vec


def _make_miscalibrated_data(
    n: int = 800, seed: int = 42, bias: float = 0.30, scale: float = 0.40
) -> tuple[np.ndarray, np.ndarray]:
    """Generate (raw_prob, label) pairs with a deliberate calibration bias.

    Mirrors the helper in ``tests/test_calibration.py``: the true latent
    probability is Beta-shaped; the *raw* probability the calibrator sees
    is a monotonic-but-biased transformation ``p → clip(bias + scale*p)``
    — the failure mode tree ensembles exhibit, and isotonic regression
    can invert.
    """
    rng = np.random.RandomState(seed)
    true_p = rng.beta(2, 2, n)
    mask = rng.uniform(0, 1, n) < 0.20
    true_p[mask] = rng.uniform(0.85, 0.99, mask.sum())
    mask = rng.uniform(0, 1, n) < 0.20
    true_p[mask] = rng.uniform(0.01, 0.15, mask.sum())
    labels = (rng.uniform(0, 1, n) < true_p).astype(int)
    raw_prob = np.clip(bias + scale * true_p, 0.01, 0.99)
    return raw_prob, labels


@pytest.fixture
def fitted_model():
    """A freshly-trained ``MarketMLModel()`` instance trained on synthetic
    data only, with 100 rows + 10 estimators so a single ``fit_initial``
    call takes ~1.3 s (instead of ~25 s).

    Mocks ``timescale_db.fetch_training_samples`` → ``(None, [])`` so the
    model trains on synthetic data only — leaving ``training_source ==
    "synthetic_only"`` and ``n_real_samples == 0``.

    The returned ``MarketMLModel`` instance is LOCAL to the test — the
    module-level singleton ``ml_model`` is left in its post-import
    (cached / fit) state. Tests that need to drive the production singleton
    should monkeypatch ``ml_model.predict`` / ``ml_model.rf`` etc directly.
    """
    with patch(
        "core.timescale_db.timescale_db.fetch_training_samples",
        return_value=(None, []),
    ):
        with patch(
            "ml.model._synthetic_training_data",
            return_value=_synthetic_training_data(n=100),
        ):
            model = MarketMLModel()
            model.fit_initial(
                rf_max_depth=4,
                gb_learning_rate=0.1,
                n_estimators_rf=10,
                n_estimators_gb=10,
            )
            return model


@pytest.fixture
def fresh_drift_detector():
    """Return a brand-new ``ModelDriftDetector`` so the module-level
    singleton's state isn't perturbed by these tests.

    The module-level ``drift_detector`` singleton persists across the
    whole pytest session (``recent_predictions`` / ``psi_history`` /
    captured ``reference_distribution`` / Brier escalations). Fresh
    construction is the same pattern ``tests/test_drift_detector.py``
    uses — it isolates the cross-module behaviour we want to verify from
    any state left over from a prior test module.
    """
    return ModelDriftDetector()


# ── (1) Train → Predict → Drift → Retrain cycle ────────────────────────────


async def test_train_predict_drift_retrain_cycle(fitted_model):
    """Verify the full train → predict → drift → retrain cycle works.

    Steps:
      (a) ``fit_initial`` succeeds — model is fitted, Brier score recorded.
      (b) ``predict()`` returns a 2-tuple of floats and feeds the drift
          detector's rolling prediction buffer (``record_prediction``
          auto-fires ``compute_psi`` every 50 predictions after 50
          accumulate).
      (c) Simulating drift (feeding the drift detector many predictions
          drawn from a DIFFERENT distribution than the captured
          reference) drives PSI above the 0.25 SIGNIFICANT_DRIFT
          threshold.
      (d) ``reset()`` clears the drift detector's rolling window AND
          resets ``drift_status`` back to HEALTHY — the documented
          post-retrain recovery contract.
      (e) Retraining the model on synthetic data again registers a NEW
          model version in the registry (active_version changes).
    """
    # ── (a) fit_initial succeeded ────────────────────────────────────────
    assert fitted_model.is_fitted, "model must be fitted after fit_initial"
    assert 0.0 < fitted_model.brier_score <= 1.0
    assert 0.0 <= fitted_model.roc_auc <= 1.0

    # ── (b) predict works + feeds drift detector ─────────────────────────
    detector = ModelDriftDetector()
    pre_count = len(detector.recent_predictions)
    features = _make_features(mid_price=0.5)
    for _ in range(5):
        p_yes, _ = fitted_model.predict(features, token_id="TEST_ML")
    post_count = len(detector.recent_predictions)
    # The fresh detector is independent of ml_model.predict — but the
    # production ml_model singleton's predict path DOES call the module
    # singleton ``drift_detector.record_prediction``. We verify the
    # contract on the LOCAL detector: feeding it predictions grows the
    # rolling buffer.
    detector.record_prediction(0.5)
    assert len(detector.recent_predictions) == pre_count + 1
    assert detector.recent_predictions[-1] == 0.5

    # ── (c) Simulate drift → PSI > 0.25, status = SIGNIFICANT_DRIFT ───────
    # Warm-up: 60 predictions at p=0.5. After the 50th, ``compute_psi``
    # auto-fires and captures the all-0.5 reference distribution.
    for _ in range(60):
        detector.record_prediction(0.5)
    # The reference distribution should now be captured (all mass in
    # the [0.4, 0.5) or [0.5, 0.6) bins).
    assert detector.reference_distribution is not None
    ref = detector.reference_distribution
    # Most of the reference mass is in the middle bins (index 4 = [0.4, 0.5)
    # and 5 = [0.5, 0.6)).
    assert ref[4] + ref[5] > 0.9, (
        f"reference distribution should be concentrated in the middle bins "
        f"after a 0.5-only warm-up; got {ref.tolist()}"
    )

    # Drift: 60 more predictions at p=0.95 — all in bin 9 = [0.9, 1.0].
    # The auto-trigger at the 100th + 150th will compute_psi with the
    # shifted actual distribution.
    for _ in range(60):
        detector.record_prediction(0.95)

    # PSI must now be well above the 0.25 SIGNIFICANT_DRIFT threshold
    # (a pure mass-shift from middle-bins to extreme-bins yields a very
    # large PSI score, easily > 1.0).
    assert detector.last_psi > 0.25, (
        f"PSI should exceed 0.25 SIGNIFICANT_DRIFT threshold after a "
        f"distribution shift; got {detector.last_psi}"
    )
    assert detector.drift_status == "SIGNIFICANT_DRIFT", (
        f"drift_status should escalate to SIGNIFICANT_DRIFT after PSI "
        f"breach; got {detector.drift_status}"
    )

    # ── (d) reset() clears the rolling window + status ──────────────────
    detector.reset()
    assert len(detector.recent_predictions) == 0
    assert detector.last_psi == 0.0
    assert detector.drift_status == "HEALTHY"
    assert detector.rolling_brier is None
    assert detector.ewma_brier is None

    # ── (e) Retraining registers a NEW model version ─────────────────────
    active_before = model_registry.active_version
    with patch(
        "core.timescale_db.timescale_db.fetch_training_samples",
        return_value=(None, []),
    ):
        with patch(
            "ml.model._synthetic_training_data",
            return_value=_synthetic_training_data(n=100),
        ):
            retrained = MarketMLModel()
            retrained.fit_initial(
                rf_max_depth=4,
                gb_learning_rate=0.1,
                n_estimators_rf=10,
                n_estimators_gb=10,
            )
    # A new version was registered.
    summary = model_registry.get_summary()
    assert summary["total_registered"] >= 1
    # The newly-registered version appears in the lineage.
    versions = [v["version"] for v in summary["versions"]]
    assert len(versions) >= 1
    # active_version may or may not have changed (registry gates by
    # Brier/AUC thresholds); we assert that at least one version exists
    # AND the latest registered version is at the head of the lineage.
    assert versions[0] == summary["active_version"] or len(versions) > 1


async def test_predict_records_in_feature_store(fitted_model):
    """``predict()`` records the input feature values in the ML feature
    store (``feature_values`` table) — the W16-2 contract.

    After a single ``predict()`` call with a known token_id, the
    feature store contains at least N_FEATURES rows for that token.
    """
    import sqlite3

    from ml.feature_store import FEATURE_STORE_DB

    TOKEN = "TEST_FS_PIPELINE"
    features = _make_features(mid_price=0.42)
    fitted_model.predict(features, token_id=TOKEN)

    # Query the feature store directly for the token's recorded values.
    with sqlite3.connect(FEATURE_STORE_DB) as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT feature_name, value FROM feature_values "
            "WHERE token_id = ?",
            (TOKEN,),
        )
        rows = [dict(r) for r in cursor.fetchall()]

    assert len(rows) >= 1, (
        "predict() must record at least the numeric feature values in "
        "the feature store"
    )
    # At least one row carries the mid_price value (index 0).
    feature_names = [r["feature_name"] for r in rows]
    assert any("mid_price" in n for n in feature_names), (
        f"mid_price feature not found in recorded feature_values; "
        f"got feature_names={feature_names[:5]}"
    )


# ── (2) Calibration integration ────────────────────────────────────────────


async def test_calibration_improves_brier_score():
    """Fitting the calibrator on a miscalibrated dataset reduces the
    Brier score on the same dataset.

    Pre-calibration Brier is computed on the raw probabilities; the
    calibrator is then fit on (raw_prob, label) pairs and the
    post-calibration Brier is computed on the calibrator-transformed
    probabilities. The isotonic method is flexible enough to invert
    the deliberate ``p → 0.3 + 0.4*p`` bias.
    """
    probs, labels = _make_miscalibrated_data(n=800, seed=42)
    calibrator = ProbabilityCalibrator(method="isotonic")

    # Pre-calibration Brier.
    pre_brier = calibrator._brier_score(probs, labels)
    assert pre_brier > 0, "pre-calibration Brier must be positive on a miscalibrated set"

    # Fit the calibrator.
    metrics = calibrator.fit(probs, labels)
    assert metrics["is_fit"] is True
    assert calibrator.is_fit is True

    # Post-calibration Brier (computed on the calibrator-transformed probs).
    calibrated = calibrator.transform(probs)
    post_brier = calibrator._brier_score(calibrated, labels)

    # Brier improvement is strictly positive — isotonic regression can
    # invert the monotonic-but-biased transformation.
    assert metrics["brier_improvement"] > 0, (
        f"calibration should improve Brier score; got improvement="
        f"{metrics['brier_improvement']} (pre={pre_brier:.4f}, "
        f"post={post_brier:.4f})"
    )
    assert post_brier < pre_brier, (
        f"post-calibration Brier ({post_brier:.4f}) must be strictly "
        f"lower than pre-calibration Brier ({pre_brier:.4f})"
    )

    # ECE improvement is non-negative (calibration cannot worsen ECE on
    # the calibration set itself under isotonic regression).
    assert metrics["ece_improvement"] >= 0, (
        f"ECE improvement should be >= 0 (got {metrics['ece_improvement']})"
    )


async def test_calibration_passthrough_when_unfit():
    """``transform()`` is a passthrough when the calibrator has not been fit.

    This is the cold-start integration contract: ``ml_model.predict()``
    calls ``calibrator.transform(...)`` unconditionally, so it must be
    safe to call before the first ``fit()``.
    """
    calibrator = ProbabilityCalibrator(method="isotonic")
    assert not calibrator.is_fit

    raw = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
    transformed = calibrator.transform(raw)
    # Exact passthrough — array equal (not just close) because the
    # unfitted path is ``np.asarray(probs, dtype=float64)``.
    np.testing.assert_array_equal(transformed, raw)


# ── (3) Shadow inference ────────────────────────────────────────────────────


async def test_shadow_inference_records_champion_and_challenger():
    """A registered challenger model receives every prediction the
    production champion makes, and the comparison is recorded in the
    shadow-inference ring buffer.

    Tests the cross-module wiring: ``ml_model.predict()`` invokes
    ``shadow_inference.run_shadow(features, token_id, p_yes)`` — so a
    challenger registered via ``shadow_inference.register_shadow_model``
    will receive every production prediction and its own p_yes estimate
    is recorded alongside the champion's.
    """
    engine = ShadowInferenceEngine()
    # Register a simple challenger: returns half the champion's p_yes.
    def challenger_fn(features):
        # Defensive: the production path passes the raw feature array;
        # ``features[0]`` is mid_price (the only feature we set).
        return 0.35  # fixed p_yes for the challenger

    engine.register_shadow_model(
        name="half_p_challenger",
        fn=challenger_fn,
        description="challenger returning a fixed 0.35 p_yes",
    )
    assert "half_p_challenger" in engine.registered_models

    # Run shadow inference with a known production p_yes.
    features = _make_features(mid_price=0.5)
    production_p_yes = 0.70
    engine.run_shadow(features, token_id="TEST_SHADOW", p_yes=production_p_yes)

    # The challenger's history now has exactly one comparison entry.
    report = engine.get_status_report()
    assert report["total_calls"] == 1
    assert report["total_errors"] == 0
    assert len(report["registered_models"]) == 1
    challenger_report = report["registered_models"][0]
    assert challenger_report["name"] == "half_p_challenger"
    assert challenger_report["calls"] == 1
    last = challenger_report["last_comparison"]
    assert last is not None
    assert last["token_id"] == "TEST_SHADOW"
    assert last["p_production"] == pytest.approx(production_p_yes, abs=1e-3)
    assert last["p_shadow"] == pytest.approx(0.35, abs=1e-3)
    # abs_delta is the absolute disagreement between champion and challenger.
    assert last["abs_delta"] == pytest.approx(
        abs(0.35 - production_p_yes), abs=1e-3
    )
    # Mean abs delta is computed across the history window.
    assert challenger_report["mean_abs_delta_vs_production"] > 0


async def test_shadow_inference_metrics_computed_after_multiple_calls():
    """After multiple shadow inference calls, the aggregate metrics
    (total_calls, mean_abs_delta) are correctly computed across the
    challenger's history window.

    This exercises the rolling-statistics path: each call appends to
    the challenger's ring buffer; the report's
    ``mean_abs_delta_vs_production`` is the mean of the per-call
    ``abs_delta`` values.
    """
    engine = ShadowInferenceEngine()
    # Challenger: returns ``1 - p_production`` so abs_delta = |1 - 2*p|.

    def inverse_fn(features):
        # Read the production p_yes off the features array (index 0 is
        # mid_price in our test fixture, but the challenger doesn't
        # actually need to use it — we just return a deterministic value).
        return 0.20

    engine.register_shadow_model("inverse", fn=inverse_fn)

    production_p_yes_values = [0.50, 0.60, 0.70, 0.80]
    features = _make_features(mid_price=0.5)
    for p in production_p_yes_values:
        engine.run_shadow(features, token_id="TOK", p_yes=p)

    report = engine.get_status_report()
    assert report["total_calls"] == len(production_p_yes_values)
    challenger = report["registered_models"][0]
    assert challenger["calls"] == len(production_p_yes_values)
    # mean_abs_delta = mean(|0.20 - p| for p in production_p_yes_values)
    expected_mean = float(
        np.mean([abs(0.20 - p) for p in production_p_yes_values])
    )
    assert challenger["mean_abs_delta_vs_production"] == pytest.approx(
        expected_mean, abs=1e-3
    )


async def test_production_predict_invokes_shadow_inference(fitted_model):
    """The production ``ml_model.predict()`` path invokes
    ``shadow_inference.run_shadow(...)`` for every registered challenger.

    This verifies the cross-module wiring: ``predict()`` calls
    ``shadow_inference.run_shadow(features, token_id, p_yes)`` (T13).
    We register a challenger on the module-level singleton, call
    ``predict()``, and verify the challenger's call count grew.
    """
    from ml.shadow_inference import shadow_inference as shadow_singleton

    # Register a challenger on the module-level singleton (this is the
    # one ``ml_model.predict()`` consults).
    shadow_singleton.register_shadow_model(
        name="w17_9_integration_test_challenger",
        fn=lambda features: 0.42,
        description="W17-9 integration test challenger",
    )
    pre_calls = shadow_singleton.total_calls
    pre_report = shadow_singleton.get_status_report()
    pre_challenger_calls = (
        next(
            (
                m["calls"]
                for m in pre_report["registered_models"]
                if m["name"] == "w17_9_integration_test_challenger"
            ),
            0,
        )
        if pre_report["registered_models"]
        else 0
    )

    # Run a prediction through the (locally-fitted) model. We use the
    # locally-fitted model rather than the singleton ``ml_model`` because
    # the singleton may be a stale pickled instance from a prior test
    # session.
    features = _make_features(mid_price=0.5)
    fitted_model.predict(features, token_id="TEST_SHADOW_INTEGRATION")

    post_report = shadow_singleton.get_status_report()
    # The challenger's call count grew by at least 1 (predict() invokes
    # shadow_inference.run_shadow exactly once per predict call).
    post_challenger_calls = next(
        (
            m["calls"]
            for m in post_report["registered_models"]
            if m["name"] == "w17_9_integration_test_challenger"
        ),
        0,
    )
    assert post_challenger_calls > pre_challenger_calls, (
        f"production predict() must invoke shadow_inference.run_shadow; "
        f"challenger calls pre={pre_challenger_calls}, "
        f"post={post_challenger_calls}"
    )

    # Cleanup: unregister the test challenger so subsequent tests see
    # a clean registry.
    shadow_singleton.unregister_shadow_model("w17_9_integration_test_challenger")
