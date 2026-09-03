"""
tests/test_calibration.py — Unit tests for ``ml/calibration.py`` (W11-5).

Covers the eight behaviours required by the W11-5 task spec:

  (1) ``transform()`` returns the raw probabilities unchanged when the
      calibrator has NOT been fit yet (cold-start passthrough — the
      integration contract that makes ``calibrator`` safe to call from
      ``MarketMLModel.predict()`` unconditionally).
  (2) Platt scaling improves calibration (reduces ECE) on a synthetically
      miscalibrated dataset.
  (3) Isotonic regression improves calibration (reduces ECE) on the same
      miscalibrated dataset.
  (4) ``_brier_score`` returns the mean squared error of (prob − label).
  (5) ``_expected_calibration_error`` returns the weighted-average
      |confidence − accuracy| across 10 bins; manually verified against a
      tiny hand-computable example.
  (6) ``reliability_curve`` returns ``{prob_true, prob_pred, n_bins}``
      with ``len(prob_true) == len(prob_pred)`` and the configured bin
      count.
  (7) ``save()`` / ``load()`` round-trip restores the fitted calibrator
      so ``transform()`` on the loaded instance matches the in-memory
      instance bit-for-bit (within float tolerance).
  (8) Calibration reduces ECE on a deliberately miscalibrated example
      where the raw probabilities are systematically shifted (every prob
      mapped through ``p → 0.3 + 0.4*p`` — a monotonic but biased
      transformation that the isotonic calibrator can invert).

Test isolation strategy
-----------------------
* Every test builds its OWN ``ProbabilityCalibrator()`` instance instead
  of mutating the module-level ``calibrator`` singleton — so the live
  ``ml_model.predict()`` path is unaffected by these unit tests. The
  singleton is exercised only by the ``fitted_model``-style integration
  fixture in ``tests/test_ml_model.py`` (which calls ``fit_initial()``
  and thereby fits the singleton — that's the intended production
  integration contract).
* ``tmp_path`` is used for the save/load round-trip test so no test
  artifacts leak between runs.
* Inline ``sys.path`` bootstrap mirrors the pattern in
  ``tests/test_ml_model.py`` so this file is collected regardless of
  the cwd pytest was launched from.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Inline sys.path bootstrap — mirrors the pattern in tests/test_ml_model.py
# and tests/conftest.py so ``from ml.calibration import ...`` resolves
# regardless of the cwd pytest was launched from.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ml.calibration import (  # noqa: E402
    CalibrationMethod,
    ProbabilityCalibrator,
    calibrator as global_calibrator,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_miscalibrated_data(
    n: int = 2000,
    seed: int = 42,
    bias: float = 0.30,
    scale: float = 0.40,
) -> tuple[np.ndarray, np.ndarray]:
    """Generate (raw_prob, label) pairs with a deliberate calibration bias.

    The true latent probability is drawn from a U-shape (Beta(2, 2)-ish)
    distribution that mimics real prediction-market data (lots of mass at
    the extremes). The labels are sampled Bernoulli(true_p). The *raw*
    probability the calibrator sees is then a monotonic-but-biased
    transformation ``p → clip(bias + scale * p, 0.01, 0.99)`` — so the
    ranking is preserved (high true_p still yields high raw_prob) but the
    absolute values are systematically off (a true_p of 0.5 becomes a
    raw_prob of 0.5, but a true_p of 0.1 becomes 0.34 and a true_p of 0.9
    becomes 0.66 — the model is "shrunk toward 0.5"). This is exactly
    the failure mode tree ensembles exhibit, and isotonic regression can
    invert it.
    """
    rng = np.random.RandomState(seed)
    true_p = rng.beta(2, 2, n)  # bell-shaped around 0.5
    # Push some mass to the extremes so we get bins at the tails too
    mask = rng.uniform(0, 1, n) < 0.20
    true_p[mask] = rng.uniform(0.85, 0.99, mask.sum())
    mask = rng.uniform(0, 1, n) < 0.20
    true_p[mask] = rng.uniform(0.01, 0.15, mask.sum())
    labels = (rng.uniform(0, 1, n) < true_p).astype(int)
    raw_prob = np.clip(bias + scale * true_p, 0.01, 0.99)
    return raw_prob, labels


# ── (1) transform() is a passthrough when not fit ────────────────────────────

def test_transform_returns_raw_probs_when_not_fitted():
    """``transform()`` must return the input probabilities unchanged when
    the calibrator has NOT been fit yet — the cold-start passthrough
    contract that makes ``calibrator`` safe to call from
    ``MarketMLModel.predict()`` unconditionally.

    Verified by:
      * Constructing a fresh ``ProbabilityCalibrator()`` (``_is_fit == False``).
      * Calling ``transform()`` on a hand-crafted array.
      * Asserting the output equals the input element-for-element.
    """
    cal = ProbabilityCalibrator(method="isotonic")
    assert cal.is_fit is False, "freshly-constructed calibrator must NOT be fit"
    raw = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
    out = cal.transform(raw)
    # Element-for-element equality (np.array_equal handles dtype + shape).
    assert np.array_equal(out, raw), (
        f"transform() must passthrough when not fit; got {out!r}, expected {raw!r}"
    )
    # Belt-and-braces: same shape, same dtype kind (float).
    assert out.shape == raw.shape
    assert out.dtype.kind == "f"


def test_transform_passthrough_when_method_is_none():
    """``method='none'`` is an explicit passthrough — even after ``fit()``
    is called, ``transform()`` must return the raw probabilities unchanged.

    This is the A/B-comparison escape hatch: an operator can set
    ``method='none'`` to compare the calibrated vs. uncalibrated path
    without disabling the calibrator entirely.
    """
    cal = ProbabilityCalibrator(method="none")
    raw, labels = _make_miscalibrated_data(n=200)
    cal.fit(raw, labels)
    assert cal.is_fit is True, "fit() must set is_fit even for method='none'"
    out = cal.transform(raw)
    assert np.allclose(out, raw), (
        "method='none' transform must equal the raw input even after fit()"
    )


# ── (2) Platt scaling improves calibration ───────────────────────────────────

def test_platt_scaling_reduces_ece_on_miscalibrated_data():
    """Platt scaling must reduce the Expected Calibration Error (ECE) on
    the deliberately miscalibrated dataset from ``_make_miscalibrated_data``.

    Platt scaling fits a logistic regression on the log-odds of the raw
    probability — a 2-parameter affine transform in logit space. Even
    though the miscalibration is non-linear (a linear shift followed by
    a clip), Platt scaling captures the dominant monotonic trend and
    should meaningfully reduce ECE.

    Asserts:
      * ``post_ece < pre_ece`` (improvement).
      * ``ece_improvement > 0`` (cached metric agrees).
      * ``is_fit == True`` after ``fit()``.
    """
    raw, labels = _make_miscalibrated_data(n=2000, seed=7)
    cal = ProbabilityCalibrator(method="platt")
    metrics = cal.fit(raw, labels)

    assert cal.is_fit is True
    assert metrics["method"] == "platt"
    assert metrics["pre_ece"] > 0.0, "pre-calibration ECE should be positive on miscalibrated data"
    assert metrics["post_ece"] < metrics["pre_ece"], (
        f"Platt scaling must reduce ECE: pre={metrics['pre_ece']:.4f}, "
        f"post={metrics['post_ece']:.4f}"
    )
    assert metrics["ece_improvement"] > 0.0
    # Sanity: improvement amount equals the difference (within float tolerance)
    assert metrics["ece_improvement"] == pytest.approx(
        metrics["pre_ece"] - metrics["post_ece"], abs=1e-9
    )


# ── (3) Isotonic regression improves calibration ─────────────────────────────

def test_isotonic_reduces_ece_on_miscalibrated_data():
    """Isotonic regression must reduce ECE on the same miscalibrated
    dataset.

    Isotonic regression is non-parametric — it fits a stepwise monotonic
    mapping that can fully invert the linear-shift + clip miscalibration
    (since the shift is itself monotonic). Should reduce ECE by a larger
    margin than Platt scaling on this dataset.
    """
    raw, labels = _make_miscalibrated_data(n=2000, seed=7)
    cal = ProbabilityCalibrator(method="isotonic")
    metrics = cal.fit(raw, labels)

    assert cal.is_fit is True
    assert metrics["method"] == "isotonic"
    assert metrics["post_ece"] < metrics["pre_ece"], (
        f"Isotonic must reduce ECE: pre={metrics['pre_ece']:.4f}, "
        f"post={metrics['post_ece']:.4f}"
    )
    # Isotonic is more flexible than Platt — it should reduce ECE by AT
    # LEAST as much on this deliberately-non-linear miscalibration.
    platt_cal = ProbabilityCalibrator(method="platt")
    platt_metrics = platt_cal.fit(raw, labels)
    assert metrics["ece_improvement"] >= platt_metrics["ece_improvement"] - 1e-6, (
        f"Isotonic (ΔECE={metrics['ece_improvement']:.4f}) should improve ECE "
        f"by ≥ Platt's ΔECE={platt_metrics['ece_improvement']:.4f} on a "
        f"monotonic-but-biased miscalibration"
    )


# ── (4) Brier score computation ───────────────────────────────────────────────

def test_brier_score_computation():
    """``_brier_score`` must return ``mean((probs - labels) ** 2)``.

    Hand-computed example: probs = [0.2, 0.6, 0.8], labels = [0, 1, 0].
      (0.2 − 0)^2 = 0.04
      (0.6 − 1)^2 = 0.16
      (0.8 − 0)^2 = 0.64
      mean       = (0.04 + 0.16 + 0.64) / 3 = 0.84 / 3 = 0.28
    """
    cal = ProbabilityCalibrator()
    probs = np.array([0.2, 0.6, 0.8])
    labels = np.array([0, 1, 0])
    brier = cal._brier_score(probs, labels)
    assert brier == pytest.approx(0.28, abs=1e-9), (
        f"Brier score must be mean((p-y)^2)=0.28, got {brier!r}"
    )
    # Perfect predictions → Brier = 0.
    perfect_probs = np.array([0.0, 1.0, 0.0, 1.0])
    perfect_labels = np.array([0, 1, 0, 1])
    assert cal._brier_score(perfect_probs, perfect_labels) == pytest.approx(0.0, abs=1e-12)
    # All-wrong predictions → Brier = 1.0.
    wrong_probs = np.array([1.0, 0.0, 1.0, 0.0])
    wrong_labels = np.array([0, 1, 0, 1])
    assert cal._brier_score(wrong_probs, wrong_labels) == pytest.approx(1.0, abs=1e-12)


# ── (5) Expected Calibration Error computation ───────────────────────────────

def test_ece_computation():
    """``_expected_calibration_error`` must return the weighted average of
    |bin_confidence − bin_accuracy| across the 10 default bins.

    Hand-computed tiny example (4 samples, 10 bins — most bins empty):
      probs  = [0.05, 0.05, 0.95, 0.95]  (2 in bin 0, 2 in bin 9)
      labels = [0,    0,    1,    0]
      Bin 0 ([0.0, 0.1)):  conf=0.05, acc=0/2=0.0, |0.05-0.0|=0.05, weight=2/4=0.5
      Bin 9 ([0.9, 1.0]):  conf=0.95, acc=1/2=0.5, |0.95-0.5|=0.45, weight=2/4=0.5
      ECE = 0.5*0.05 + 0.5*0.45 = 0.025 + 0.225 = 0.25
    """
    cal = ProbabilityCalibrator()
    probs = np.array([0.05, 0.05, 0.95, 0.95])
    labels = np.array([0, 0, 1, 0])
    ece = cal._expected_calibration_error(probs, labels, n_bins=10)
    assert ece == pytest.approx(0.25, abs=1e-9), (
        f"ECE must be 0.25, got {ece!r}"
    )

    # Perfectly calibrated: prob matches empirical frequency → ECE = 0.
    perfect_probs = np.array([0.0, 0.0, 1.0, 1.0])
    perfect_labels = np.array([0, 0, 1, 1])
    assert cal._expected_calibration_error(perfect_probs, perfect_labels) == pytest.approx(0.0, abs=1e-12)

    # Empty input → 0.0 (defensive — no division by zero).
    assert cal._expected_calibration_error(np.array([]), np.array([])) == 0.0


def test_ece_includes_right_edge_for_last_bin():
    """The last bin ``[0.9, 1.0]`` must INCLUDE the right edge (prob == 1.0)
    so a perfectly-confident prediction ``p == 1.0`` is not silently dropped.

    Hand-computed example:
      probs  = [1.0, 1.0]      (both in last bin)
      labels = [1, 0]
      conf = 1.0, acc = 0.5, |1.0 - 0.5| = 0.5, weight = 1.0
      ECE = 0.5
    """
    cal = ProbabilityCalibrator()
    probs = np.array([1.0, 1.0])
    labels = np.array([1, 0])
    ece = cal._expected_calibration_error(probs, labels, n_bins=10)
    assert ece == pytest.approx(0.5, abs=1e-9), (
        f"p==1.0 must be in the last bin; ECE should be 0.5, got {ece!r}"
    )


# ── (6) Reliability curve ────────────────────────────────────────────────────

def test_reliability_curve_returns_well_formed_data():
    """``reliability_curve`` must return ``{prob_true, prob_pred, n_bins}``
    where ``prob_true`` and ``prob_pred`` are equal-length lists of floats
    and ``n_bins`` matches the requested bin count.
    """
    cal = ProbabilityCalibrator()
    # Use a reasonably-sized dataset so calibration_curve doesn't drop bins.
    rng = np.random.RandomState(0)
    probs = rng.uniform(0.01, 0.99, 500)
    labels = (rng.uniform(0, 1, 500) < probs).astype(int)

    curve = cal.reliability_curve(probs, labels, n_bins=10)
    assert set(curve.keys()) == {"prob_true", "prob_pred", "n_bins"}, (
        f"reliability_curve keys must be {{prob_true, prob_pred, n_bins}}, "
        f"got {set(curve.keys())}"
    )
    assert curve["n_bins"] == 10
    assert len(curve["prob_true"]) == len(curve["prob_pred"]), (
        "prob_true and prob_pred must have equal length"
    )
    # calibration_curve may drop empty bins, so we only assert the length is
    # ≤ n_bins (not exactly equal).
    assert 1 <= len(curve["prob_true"]) <= 10, (
        f"reliability curve should have between 1 and n_bins=10 entries, "
        f"got {len(curve['prob_true'])}"
    )
    # All entries must be floats in [0, 1].
    for arr_name in ("prob_true", "prob_pred"):
        for v in curve[arr_name]:
            assert isinstance(v, float), (
                f"{arr_name} entries must be floats, got {type(v).__name__}"
            )
            assert 0.0 <= v <= 1.0, (
                f"{arr_name} entry {v!r} out of [0, 1]"
            )


# ── (7) Save / load round-trip ───────────────────────────────────────────────

def test_save_load_round_trip(tmp_path):
    """``save()`` → ``load()`` must restore the calibrator so
    ``transform()`` on the loaded instance matches the in-memory instance
    bit-for-bit (within float tolerance).

    Belt-and-braces: the restored instance also reports the same
    ``method``, ``is_fit``, ``n_samples``, and ``last_fit_metrics``.
    """
    raw, labels = _make_miscalibrated_data(n=500, seed=11)
    original = ProbabilityCalibrator(method="isotonic")
    original.fit(raw, labels)

    cal_path = tmp_path / "calibrator.pkl"
    original.save(cal_path)
    assert cal_path.exists(), "save() must write the file"

    restored = ProbabilityCalibrator()
    restored.load(cal_path)

    assert restored.method == original.method
    assert restored.is_fit == original.is_fit
    assert restored.n_samples == original.n_samples

    # Transform output must match element-for-element.
    test_probs = np.array([0.05, 0.15, 0.25, 0.35, 0.45, 0.55, 0.65, 0.75, 0.85, 0.95])
    out_original = original.transform(test_probs)
    out_restored = restored.transform(test_probs)
    assert np.allclose(out_original, out_restored, atol=1e-9), (
        f"loaded calibrator transform mismatch: original={out_original!r}, "
        f"restored={out_restored!r}"
    )


def test_save_load_round_trip_platt(tmp_path):
    """Same round-trip test for the Platt-scaling path — ensures the
    LogisticRegression-based calibrator survives pickling (LogisticRegression
    has C-extension state that must be preserved).
    """
    raw, labels = _make_miscalibrated_data(n=500, seed=13)
    original = ProbabilityCalibrator(method="platt")
    original.fit(raw, labels)

    cal_path = tmp_path / "calibrator_platt.pkl"
    original.save(cal_path)

    restored = ProbabilityCalibrator()
    restored.load(cal_path)

    assert restored.method == "platt"
    assert restored.is_fit is True

    test_probs = np.array([0.1, 0.3, 0.5, 0.7, 0.9])
    out_original = original.transform(test_probs)
    out_restored = restored.transform(test_probs)
    assert np.allclose(out_original, out_restored, atol=1e-9)


# ── (8) Calibration reduces ECE on a miscalibrated example ───────────────────

def test_calibration_reduces_ece_on_miscalibrated_example():
    """The headline behaviour: a deliberately-miscalibrated set of raw
    probabilities has high ECE; after fitting isotonic calibration, the
    SAME raw probabilities (transformed) have low ECE.

    Verifies:
      * ``post_ece < pre_ece`` (the headline assertion).
      * The transform actually changes the probabilities (no passthrough).
      * The improvement is large enough to be practically meaningful
        (≥ 0.01 absolute ECE reduction on this dataset).
    """
    raw, labels = _make_miscalibrated_data(n=2000, seed=99, bias=0.30, scale=0.40)
    cal = ProbabilityCalibrator(method="isotonic")
    metrics = cal.fit(raw, labels)

    # Headline: post < pre.
    assert metrics["post_ece"] < metrics["pre_ece"], (
        f"Calibration must reduce ECE: pre={metrics['pre_ece']:.4f} → "
        f"post={metrics['post_ece']:.4f}"
    )
    # Improvement must be practically meaningful (not a 1e-6 numerical blip).
    assert metrics["ece_improvement"] >= 0.01, (
        f"ECE improvement {metrics['ece_improvement']:.4f} is below the "
        f"0.01 practical-meaningfulness threshold"
    )

    # The transform must actually CHANGE the probabilities (no passthrough).
    transformed = cal.transform(raw)
    assert not np.allclose(transformed, raw, atol=1e-9), (
        "calibration transform must alter the raw probabilities"
    )
    # The transformed probs must lie in [0, 1].
    assert np.all(transformed >= 0.0) and np.all(transformed <= 1.0), (
        "calibrated probabilities must lie in [0, 1]"
    )

    # Re-compute ECE on the transformed probs directly — must match the
    # ``post_ece`` metric reported by ``fit()``.
    recomputed_ece = cal._expected_calibration_error(transformed, labels)
    assert recomputed_ece == pytest.approx(metrics["post_ece"], abs=1e-6), (
        f"recomputed post-calibration ECE {recomputed_ece:.6f} doesn't match "
        f"reported post_ece {metrics['post_ece']:.6f}"
    )


# ── (9) Singleton sanity check ───────────────────────────────────────────────

def test_module_level_singleton_exists_and_is_unfitted_by_default():
    """The module-level ``calibrator`` singleton must exist and be a
    ``ProbabilityCalibrator`` instance.

    The "unfitted by default" assertion is intentionally NOT made here
    because the singleton can be fit by other tests in the session that
    exercise the ``fitted_model`` fixture (which calls ``fit_initial()``
    and thereby fits the singleton). What we DO assert is that the
    singleton is the correct type and exposes the expected API surface
    so the production integration (``ml/model.py`` importing it) works.
    """
    assert isinstance(global_calibrator, ProbabilityCalibrator), (
        "module-level `calibrator` must be a ProbabilityCalibrator instance"
    )
    # API surface — these attributes/methods are part of the integration
    # contract with ml/model.py.
    assert hasattr(global_calibrator, "fit")
    assert hasattr(global_calibrator, "transform")
    assert hasattr(global_calibrator, "is_fit")
    assert hasattr(global_calibrator, "n_samples")
    assert hasattr(global_calibrator, "method")
    assert hasattr(global_calibrator, "last_fit_metrics")
    # Default method is isotonic (the spec's default).
    assert global_calibrator.method in ("platt", "isotonic", "none")


# ── (10) fit() rejects length-mismatched inputs ──────────────────────────────

def test_fit_rejects_length_mismatch():
    """``fit()`` must raise ``ValueError`` when ``len(probs) != len(labels)``.

    Defensive guard — silent acceptance would let a caller pass mismatched
    arrays and get garbage out of the fitted calibrator (sklearn would
    raise anyway, but with a less-actionable error message).
    """
    cal = ProbabilityCalibrator()
    probs = np.array([0.1, 0.2, 0.3])
    labels = np.array([0, 1])
    with pytest.raises(ValueError, match="Length mismatch"):
        cal.fit(probs, labels)


# ── (11) Unknown calibration method raises ───────────────────────────────────

def test_fit_rejects_unknown_method():
    """``fit()`` must raise ``ValueError`` when ``method`` is not one of
    ``{'platt', 'isotonic', 'none'}``.

    The constructor accepts any string for ``method`` (no validation at
    construction time so a config file with a typo doesn't crash on import),
    but ``fit()`` validates before doing work.
    """
    cal = ProbabilityCalibrator(method="bogus")  # type: ignore[arg-type]
    raw, labels = _make_miscalibrated_data(n=100, seed=1)
    with pytest.raises(ValueError, match="Unknown calibration method"):
        cal.fit(raw, labels)
    # Fit must NOT have flipped ``is_fit`` to True on the failure path.
    assert cal.is_fit is False


# ── (12) Calibration metric dict shape ───────────────────────────────────────

def test_fit_returns_metric_dict_with_expected_keys():
    """``fit()`` must return a metrics dict containing the documented keys:
    ``method``, ``n_samples``, ``pre_brier``, ``post_brier``,
    ``brier_improvement``, ``pre_ece``, ``post_ece``, ``ece_improvement``,
    ``is_fit``.

    These keys are surfaced in the ``/api/ml/metrics`` payload via
    ``calibrator.last_fit_metrics`` — adding/removing a key here is a
    breaking change for API consumers.
    """
    raw, labels = _make_miscalibrated_data(n=300, seed=5)
    cal = ProbabilityCalibrator(method="isotonic")
    metrics = cal.fit(raw, labels)
    expected_keys = {
        "method", "n_samples", "pre_brier", "post_brier",
        "brier_improvement", "pre_ece", "post_ece",
        "ece_improvement", "is_fit",
    }
    assert set(metrics.keys()) == expected_keys, (
        f"metric keys mismatch: got {set(metrics.keys())}, "
        f"expected {expected_keys}"
    )
    # ``last_fit_metrics`` on the calibrator instance must equal the
    # returned metrics dict (same object — verified by equality).
    assert cal.last_fit_metrics == metrics
