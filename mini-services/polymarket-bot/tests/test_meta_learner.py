"""
tests/test_meta_learner.py — Unit tests for ``ml/ensemble_meta_learner.py``.

W4 — Stacking Ensemble Meta-Learner unit tests.

Covers the seven behaviours required by the W4 task spec:

  (1) ``record_outcome()`` appends a row to the rolling training buffer
      (``_buffer_X`` / ``_buffer_y``) and increments ``n_updates``.
  (2) ``predict()`` returns ``None`` when the meta-learner is NOT yet
      warm — the documented cold-start contract (caller falls back to
      adaptive-weight blending).
  (3) ``predict()`` returns a ``float`` (clipped into ``[0.01, 0.99]``)
      when warm — i.e. after a successful ``_refit_meta_model()``.
  (4) ``is_warm`` is ``False`` on a freshly-constructed
      ``EnsembleMetaLearner()`` instance.
  (5) ``warm_from_labeled_samples()`` returns a summary dict whose
      ``n_loaded`` field equals the count of samples actually loaded
      into the buffer (mocked ``timescale_db`` + ``ml_model``
      singletons — no real DB / base-learner fit required).
  (6) ``_refit_meta_model()`` drops rows containing NaN/Inf in either
      the feature matrix OR the label vector before fitting
      ``LogisticRegression`` — the defensive sanitization block — and
      emits a WARNING log enumerating the dropped count. Without this
      guard, sklearn's ``fit`` would raise and the failure was
      previously swallowed at DEBUG level (silent meta-learner outage).
  (7) ``get_summary()`` returns a dict containing the ``is_warm``,
      ``n_updates``, and ``buffer_size`` keys.

Test isolation strategy
-----------------------
* Each test constructs a fresh ``EnsembleMetaLearner()`` instance via
  the class constructor — the module-level singleton
  ``ensemble_meta_learner`` (constructed at import time) is NEVER
  touched. The singleton's buffer / ``_n_updates`` / ``_is_warm``
  state therefore remains pristine across tests.
* Tests that need a "warm" learner ((3), (6)) bypass the
  ``_RETRAIN_EVERY = 50`` cadence gate inside ``record_outcome`` by
  calling the private ``_refit_meta_model()`` directly after seeding
  the buffer with ≥ ``_MIN_META_SAMPLES`` samples spanning both
  classes — the documented force-refit code path used at the tail of
  ``warm_from_labeled_samples``.
* Test (5) mocks ``core.timescale_db.timescale_db`` and
  ``ml.model.ml_model`` via ``unittest.mock.patch`` so the lazy
  imports inside ``warm_from_labeled_samples`` resolve to fakes. The
  ``ml_model`` fake exposes ``rf``, ``gb``, ``rf_cal``, ``gb_cal``,
  ``scaler`` with ``predict_proba`` / ``transform`` stubs returning
  finite per-sample probabilities derived from the input feature
  vector's first column (``mid_price``). The ``timescale_db`` fake
  exposes ``fetch_labeled_feature_vectors`` returning a controlled
  list of ``(features, label)`` tuples.
* The repo's ``pytest.ini`` declares ``testpaths = tests``; this file
  is collected automatically. The sibling ``tests/conftest.py`` is
  imported BEFORE this module so its env-var redirects (``MODEL_PATH``
  → ``/tmp/pmbot_conftest_isolation/model.pkl``, etc.) +
  ``sys.path`` bootstrap are already in effect — no inline redirection
  needed here, only an inline ``sys.path`` insertion as a
  belt-and-braces measure for IDE / direct
  ``pytest tests/test_meta_learner.py`` runs.
* The W4 task spec forbids editing existing files; this module is
  strictly additive. All accesses to ``learner._buffer_X`` /
  ``learner._buffer_y`` / ``learner._refit_meta_model()`` are
  private-member touches that the repo's ``pyproject.toml`` permits
  for ``tests/*`` (``[tool.ruff.lint.per-file-ignores] "tests/*" =
  ["SLF001"]``).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

# Inline sys.path bootstrap — mirrors the pattern in test_features.py /
# test_ml_model.py / tests/conftest.py so ``from ml.ensemble_meta_learner
# import ...`` resolves regardless of the cwd pytest was launched from
# (monorepo root, CI checkout, IDE runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ml.ensemble_meta_learner import EnsembleMetaLearner  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeProba:
    """Minimal ``predict_proba`` stub mirroring sklearn's contract.

    Returns a single ``[1-p, p]`` row whose ``p`` is derived from the
    input feature matrix's first column (``mid_price``) so that
    different feature vectors yield different class-1 probabilities
    (otherwise LogisticRegression would see 50 identical feature rows
    with mixed labels and only emit a useless ``ConvergenceWarning``).
    """

    def __init__(self, base_p: float = 0.5, slope: float = 0.5) -> None:
        self._base_p = float(base_p)
        self._slope = float(slope)

    def predict_proba(self, X):
        if hasattr(X, "shape") and X.ndim == 2 and X.shape[0] >= 1:
            mid = float(X[0, 0])
            p = self._base_p + self._slope * (mid - 0.5)
            # Clip into a valid probability range so np.isfinite() passes
            # inside warm_from_labeled_samples' sanitization gate.
            p = max(0.01, min(0.99, p))
            return np.array([[1.0 - p, p]], dtype=np.float32)
        return np.array([[1.0 - self._base_p, self._base_p]], dtype=np.float32)


class _FakeScaler:
    """No-op ``transform`` that passes the input through unchanged.

    Real ``StandardScaler.transform`` would re-centre / rescale the
    feature vector, but for the W4 contract (count the loaded samples)
    the per-sample scaled values are irrelevant — only that the call
    succeeds and yields a finite probability downstream.
    """

    def transform(self, X):
        return X


def _make_fake_ml_model() -> SimpleNamespace:
    """Construct a fake ``ml_model`` singleton exposing every attribute
    accessed by ``EnsembleMetaLearner.warm_from_labeled_samples``.

    The fakes return per-sample probabilities that vary with the input
    feature vector's ``mid_price`` (index 0) so the resulting meta
    feature rows are NOT identical across samples — let the tail-end
    LogisticRegression fit converge cleanly without sklearn
    ConvergenceWarnings polluting the test stderr.
    """
    return SimpleNamespace(
        rf=_FakeProba(base_p=0.5, slope=0.4),
        gb=_FakeProba(base_p=0.5, slope=0.5),
        rf_cal=_FakeProba(base_p=0.5, slope=0.4),
        gb_cal=_FakeProba(base_p=0.5, slope=0.5),
        sgd=None,  # unused — _sgd_trained=False short-circuits
        _sgd_trained=False,  # p_sgd stays at 0.0
        lgbm=None,  # p_lgbm stays at 0.0
        scaler=_FakeScaler(),
    )


def _make_fake_timescale_db(samples):
    """Construct a fake ``timescale_db`` whose
    ``fetch_labeled_feature_vectors`` returns ``samples`` (a list of
    ``(features, label)`` tuples). The ``limit`` kwarg is accepted but
    ignored — the caller already capped the sample count before
    constructing ``samples``.
    """

    def _fetch(limit: int = 200):
        return list(samples)

    return SimpleNamespace(fetch_labeled_feature_vectors=_fetch)


def _seed_two_class_buffer(learner: EnsembleMetaLearner, n_per_class: int = 20) -> None:
    """Append ``n_per_class`` class-0 + ``n_per_class`` class-1 rows to
    the learner's buffer (total ``2 * n_per_class`` rows).

    Class-0 rows have low base-prediction values (0.05–0.45); class-1
    rows have high values (0.55–0.95). This makes the two classes
    linearly separable along the first meta-feature axis
    (``p_rf``), so ``LogisticRegression.fit`` converges in a handful
    of iterations without warnings.

    Bypasses ``record_outcome`` (which would recompute the 6-dim meta
    feature row from the 4 base predictions) and appends rows directly
    to the deques — we're testing ``_refit_meta_model``, not
    ``record_outcome``'s buffer-append path (test (1) covers that).
    """
    rng = np.random.RandomState(42)
    for _ in range(n_per_class):
        learner._buffer_X.append([float(rng.uniform(0.05, 0.45)) for _ in range(6)])
        learner._buffer_y.append(0)
    for _ in range(n_per_class):
        learner._buffer_X.append([float(rng.uniform(0.55, 0.95)) for _ in range(6)])
        learner._buffer_y.append(1)


# ── (1) record_outcome adds to buffer ────────────────────────────────────────


def test_record_outcome_adds_to_buffer():
    """``record_outcome`` must append a row to ``_buffer_X`` /
    ``_buffer_y`` and increment ``n_updates``.

    The method's documented contract: "Feed a resolved outcome into the
    meta-learner buffer. Triggers periodic meta-model refit." On a
    fresh learner (buffer empty, ``n_updates=0``), a single call must
    leave the buffer with exactly one 6-dim feature row + one int
    label, and bump ``n_updates`` to 1. The cadence gate (refit when
    ``n_updates - _last_retrain_n >= 50``) is NOT crossed by a single
    call, so no refit is triggered — ``is_warm`` stays False.
    """
    learner = EnsembleMetaLearner()

    # Pre-state — fresh learner.
    assert len(learner._buffer_X) == 0
    assert len(learner._buffer_y) == 0
    assert learner.n_updates == 0
    assert learner.is_warm is False

    learner.record_outcome(
        p_rf=0.55,
        p_gb=0.60,
        p_sgd=0.50,
        p_lgbm=0.58,
        actual=1,
    )

    # Buffer absorbed exactly one row.
    assert len(learner._buffer_X) == 1
    assert len(learner._buffer_y) == 1

    # n_updates bumped by exactly 1.
    assert learner.n_updates == 1

    # Appended feature row is the 6-dim meta-feature vector
    # [p_rf, p_gb, p_sgd, p_lgbm, disagreement, conf_mean].
    assert len(learner._buffer_X[0]) == 6

    # Label persisted verbatim (int 1, not float 1.0).
    assert learner._buffer_y[0] == 1

    # Spot-check: the first two meta-feature entries are the raw p_rf /
    # p_gb caller-supplied probabilities (no transformation in
    # _build_meta_features — they're returned positionally).
    assert learner._buffer_X[0][0] == pytest.approx(0.55)
    assert learner._buffer_X[0][1] == pytest.approx(0.60)
    assert learner._buffer_X[0][2] == pytest.approx(0.50)
    assert learner._buffer_X[0][3] == pytest.approx(0.58)

    # is_warm is still False — single record_outcome can't cross the
    # 50-update retrain cadence, so no refit was triggered.
    assert learner.is_warm is False


# ── (2) predict returns None when not warm ───────────────────────────────────


def test_predict_returns_none_when_not_warm():
    """``predict`` must return ``None`` while the meta-learner is not
    yet warm.

    The documented contract: "Returns None if not yet warm. Caller
    should fall back to adaptive-weight blend." The guard at the head
    of ``predict`` is ``if not self._is_warm or self._meta_model is
    None: return None`` — both conditions are true on a fresh
    instance, so the early return fires before the predict_proba path
    is reached.
    """
    learner = EnsembleMetaLearner()

    # Pre-state: cold learner.
    assert learner.is_warm is False

    result = learner.predict(p_rf=0.55, p_gb=0.60, p_sgd=0.50, p_lgbm=0.58)

    # Returned None — the caller (ml_model.predict) is expected to fall
    # back to adaptive-weight blending when this happens.
    assert result is None, f"predict must return None when not warm, got {result!r}"


# ── (3) predict returns float when warm ──────────────────────────────────────


def test_predict_returns_float_when_warm():
    """After a successful ``_refit_meta_model()``, ``predict`` must
    return a finite ``float`` clipped into the closed interval
    ``[0.01, 0.99]``.

    The documented contract: the success-path return is
    ``float(np.clip(p, 0.01, 0.99))`` where ``p`` is the meta-model's
    ``predict_proba(X)[0, 1]``. We bypass the ``_RETRAIN_EVERY = 50``
    cadence gate (which fires inside ``record_outcome``) by calling
    ``_refit_meta_model()`` directly after seeding the buffer with 40
    samples spanning both classes — the same force-refit pattern used
    at the tail of ``warm_from_labeled_samples``.
    """
    learner = EnsembleMetaLearner()
    _seed_two_class_buffer(learner, n_per_class=20)  # 40 samples, 2 classes

    # Force-refit bypassing the cadence gate.
    learner._refit_meta_model()

    # Refit succeeded — buffer had both classes, so the single-class
    # short-circuit at len(np.unique(y)) < 2 didn't fire.
    assert learner.is_warm is True, (
        "Refit failed to warm the meta-learner — buffer state unexpected"
    )

    p = learner.predict(p_rf=0.55, p_gb=0.60, p_sgd=0.50, p_lgbm=0.58)

    # Returned a float (not None, not a numpy scalar).
    assert isinstance(p, float), (
        f"predict must return a float when warm, got {type(p).__name__}"
    )

    # Clipped into the canonical [0.01, 0.99] probability range — the
    # explicit ``np.clip(p, 0.01, 0.99)`` guard at the tail of predict.
    assert 0.01 <= p <= 0.99, f"predict must be clipped into [0.01, 0.99], got {p!r}"

    # Finite (no NaN/Inf leaked through the meta-model).
    assert np.isfinite(p), f"predict must be finite, got {p!r}"


# ── (4) is_warm is False initially ───────────────────────────────────────────


def test_is_warm_is_false_initially():
    """``is_warm`` must be ``False`` on a freshly-constructed
    ``EnsembleMetaLearner()`` instance — the ``@property`` returns
    ``self._is_warm`` and ``_is_warm`` is initialized to ``False`` in
    ``__init__``. Only a successful ``_refit_meta_model()`` flips it
    to ``True``.
    """
    learner = EnsembleMetaLearner()
    assert learner.is_warm is False, (
        "is_warm must be False on a fresh EnsembleMetaLearner() before "
        "any refit has run"
    )

    # Belt-and-braces: type is bool (not numpy / int / truthy object).
    assert isinstance(learner.is_warm, bool), (
        f"is_warm must be a bool, got {type(learner.is_warm).__name__}"
    )

    # The underlying private flag is also False (the @property is a
    # passthrough — no transformation).
    assert learner._is_warm is False


# ── (5) warm_from_labeled_samples returns count of samples loaded ────────────


def test_warm_from_labeled_samples_returns_count_loaded():
    """``warm_from_labeled_samples`` returns a summary dict whose
    ``n_loaded`` field equals the count of samples actually loaded
    into the buffer.

    The method's documented contract: it pulls ``(features, label)``
    tuples from ``timescale_db.fetch_labeled_feature_vectors(limit)``,
    recomputes the 4 base-model probabilities per sample (via the
    ``ml_model`` singleton's calibrated classifiers), appends each
    finite-probability row to the buffer, then force-refits. The
    returned dict has keys ``n_requested`` / ``n_loaded`` /
    ``n_skipped`` / ``buffer_size`` / ``is_warm`` / ``error``.

    Mocking strategy: ``unittest.mock.patch`` replaces both lazy-imported
    singletons (``core.timescale_db.timescale_db`` and
    ``ml.model.ml_model``) for the duration of the call. The fake DB
    returns 50 samples (25 class-0 with ``mid_price=0.30``, 25
    class-1 with ``mid_price=0.70``); the fake ``ml_model`` returns
    per-sample finite probabilities derived from ``mid_price``, so
    every sample passes the finite-probability sanitization gate and
    ``n_loaded == 50``.
    """
    learner = EnsembleMetaLearner()

    # Build 50 labeled samples spanning both classes — class-0 with
    # mid_price=0.30, class-1 with mid_price=0.70. The fake base
    # learners return mid_price-dependent probabilities, so the
    # resulting meta-features differ across classes and the tail refit
    # converges cleanly.
    samples: list[tuple[np.ndarray, int]] = []
    for _ in range(25):
        feats = np.zeros(38, dtype=np.float32)
        feats[0] = 0.30
        samples.append((feats, 0))
    for _ in range(25):
        feats = np.zeros(38, dtype=np.float32)
        feats[0] = 0.70
        samples.append((feats, 1))

    fake_ml_model = _make_fake_ml_model()
    fake_db = _make_fake_timescale_db(samples)

    # Both patches are string-targeted, so ``patch`` imports the
    # module (cached if already imported by another test file) and
    # swaps the singleton attribute for the duration of the ``with``
    # block. Inside ``warm_from_labeled_samples``, the lazy
    # ``from core.timescale_db import timescale_db`` /
    # ``from ml.model import ml_model`` statements look up the
    # (patched) module attributes — they bind to our fakes.
    with (
        patch("ml.model.ml_model", fake_ml_model),
        patch("core.timescale_db.timescale_db", fake_db),
    ):
        summary = learner.warm_from_labeled_samples(max_samples=50)

    # Return type is a dict.
    assert isinstance(summary, dict), (
        f"warm_from_labeled_samples must return a dict, got {type(summary).__name__}"
    )

    # The n_loaded field equals the count of samples actually loaded
    # — the W4 contract under test. All 50 fake samples have finite
    # base-model probabilities (the fakes never return NaN/Inf), so
    # none are skipped.
    assert "n_loaded" in summary, f"summary missing 'n_loaded' key: {summary!r}"
    assert summary["n_loaded"] == 50, (
        f"Expected n_loaded=50 (all 50 fake samples are finite), got "
        f"n_loaded={summary['n_loaded']!r}, n_skipped="
        f"{summary.get('n_skipped')!r}, error={summary.get('error')!r}"
    )

    # Belt-and-braces: the buffer actually contains 50 rows.
    assert len(learner._buffer_X) == 50
    assert len(learner._buffer_y) == 50

    # n_updates tracked the same count — every loaded sample increments
    # _n_updates inside the per-sample loop.
    assert learner.n_updates == 50

    # n_skipped is zero (no NaN/Inf in any fake probability).
    assert summary["n_skipped"] == 0

    # The tail-end force-refit warmed the meta-learner (50 samples
    # spanning both classes ⇒ LogisticRegression fits ⇒ _is_warm=True).
    assert summary["is_warm"] is True, (
        f"Force-refit at tail of warm_from_labeled_samples should have "
        f"warmed the learner, got is_warm={summary['is_warm']!r}, "
        f"error={summary.get('error')!r}"
    )
    assert learner.is_warm is True

    # No error string set on the happy path.
    assert summary["error"] is None, (
        f"Happy-path warm_from_labeled_samples should set error=None, "
        f"got error={summary['error']!r}"
    )


# ── (6) _refit_meta_model drops NaN/Inf rows ─────────────────────────────────


def test_refit_meta_model_drops_non_finite_rows(caplog):
    """``_refit_meta_model`` must drop rows containing NaN/Inf in
    either the feature matrix OR the label vector before fitting
    ``LogisticRegression``, and emit a WARNING log enumerating the
    dropped count.

    Without this sanitization guard, sklearn's ``fit`` raises (e.g.
    ``ValueError: Input contains NaN``) and the failure was previously
    swallowed at DEBUG level — a silent meta-learner outage. The
    guard: ``finite_mask = np.all(np.isfinite(X), axis=1) &
    np.isfinite(y.astype(np.float32))`` selects only the finite rows
    before passing to ``LogisticRegression.fit``.

    Strategy: seed 40 valid rows (20 class-0 + 20 class-1) then
    directly append 3 non-finite rows to the buffer (bypassing
    ``record_outcome``, which builds rows via the deterministic
    ``_build_meta_features`` — that path can't synthesize NaN/Inf from
    finite caller inputs). After refit:
      (a) the 3 non-finite rows are dropped from the fit input,
      (b) a WARNING is logged enumerating the count,
      (c) ``is_warm`` flips to True (40 valid rows with both classes
          still satisfy the refit gate).
    The buffer itself is NOT mutated — the drop is in-place on the
    local ``X`` / ``y`` NumPy arrays, not on the rolling deque.
    """
    learner = EnsembleMetaLearner()
    _seed_two_class_buffer(learner, n_per_class=20)  # 40 valid rows

    # Inject 3 non-finite rows directly into the buffer.
    # Each row is a 6-dim meta-feature vector [p_rf, p_gb, p_sgd,
    # p_lgbm, disagreement, conf_mean] — the same shape as
    # _build_meta_features' output. We poison one entry per row with
    # NaN, +Inf, and -Inf respectively to exercise all three
    # non-finite variants.
    learner._buffer_X.append([float("nan"), 0.5, 0.5, 0.5, 0.0, 0.0])
    learner._buffer_y.append(0)
    learner._buffer_X.append([float("inf"), 0.5, 0.5, 0.5, 0.0, 0.0])
    learner._buffer_y.append(1)
    learner._buffer_X.append([0.5, float("-inf"), 0.5, 0.5, 0.0, 0.0])
    learner._buffer_y.append(0)

    n_expected_dropped = 3
    total_buffer_size = len(learner._buffer_X)
    assert total_buffer_size == 43, (
        f"Buffer should have 40 valid + 3 non-finite = 43 rows, got {total_buffer_size}"
    )

    # Capture WARNING-and-above records from the ensemble_meta_learner
    # logger for the duration of the refit call.
    with caplog.at_level(
        logging.WARNING,
        logger="ml.ensemble_meta_learner",
    ):
        learner._refit_meta_model()

    # (a) Refit succeeded despite the 3 non-finite rows — they were
    #     dropped from the fit input, leaving 40 valid rows with both
    #     classes present, so LogisticRegression.fit converged and
    #     _is_warm flipped to True.
    assert learner.is_warm is True, (
        "Refit failed despite non-finite rows being dropped — the "
        "remaining 40 valid rows should have produced a successful fit"
    )

    # (b) Exactly one WARNING record was emitted enumerating the
    #     dropped count. The record's formatted message contains both
    #     the word "Dropping" and "non-finite" (the canonical log
    #     template at ensemble_meta_learner.py:120).
    drop_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING
        and "Dropping" in r.getMessage()
        and "non-finite" in r.getMessage()
    ]
    assert len(drop_records) == 1, (
        f"Expected exactly one 'Dropping N non-finite rows' WARNING, "
        f"got {len(drop_records)}: "
        f"{[r.getMessage() for r in caplog.records]}"
    )

    # The dropped count enumerated in the message matches the 3 rows
    # we injected (defensive against off-by-one in the finite_mask).
    drop_msg = drop_records[0].getMessage()
    assert str(n_expected_dropped) in drop_msg, (
        f"WARNING did not enumerate the dropped count "
        f"({n_expected_dropped}): {drop_msg!r}"
    )

    # (c) The buffer itself was NOT mutated by the drop — the
    #     sanitization operates on a local NumPy copy (X[finite_mask]),
    #     not on the rolling deque. The 3 non-finite rows remain in
    #     the buffer (they'll be re-dropped on the next refit too).
    assert len(learner._buffer_X) == total_buffer_size, (
        f"Buffer was mutated by _refit_meta_model — expected "
        f"{total_buffer_size} rows, got {len(learner._buffer_X)} "
        f"(the sanitization should operate on a local copy, not the "
        f"rolling deque)"
    )


# ── (7) get_summary returns is_warm / n_updates / buffer_size ────────────────


def test_get_summary_returns_required_keys():
    """``get_summary`` must return a dict containing the ``is_warm``,
    ``n_updates``, and ``buffer_size`` keys — the three fields
    surfaced to the operational dashboard / observability stack.

    The method's documented return shape:
      {
        "is_warm": bool,
        "n_updates": int,
        "buffer_size": int,
        "last_retrain_at_n": int,
        "min_samples_required": int,
      }

    We assert the three W4-required keys are present, that their
    values reflect the fresh-learner state (``is_warm=False``,
    ``n_updates=0``, ``buffer_size=0``), and that the types are the
    primitive ``bool`` / ``int`` (not numpy / float).
    """
    learner = EnsembleMetaLearner()

    summary = learner.get_summary()

    # Return type.
    assert isinstance(summary, dict), (
        f"get_summary must return a dict, got {type(summary).__name__}"
    )

    # Required keys present.
    assert "is_warm" in summary, f"get_summary missing 'is_warm' key: {summary!r}"
    assert "n_updates" in summary, f"get_summary missing 'n_updates' key: {summary!r}"
    assert "buffer_size" in summary, (
        f"get_summary missing 'buffer_size' key: {summary!r}"
    )

    # Fresh-learner values.
    assert summary["is_warm"] is False
    assert summary["n_updates"] == 0
    assert summary["buffer_size"] == 0

    # Belt-and-braces: types are bool / int / int (not numpy / float —
    # the dict is built from plain Python literals).
    assert isinstance(summary["is_warm"], bool), (
        f"is_warm must be a bool, got {type(summary['is_warm']).__name__}"
    )
    assert isinstance(summary["n_updates"], int), (
        f"n_updates must be an int, got {type(summary['n_updates']).__name__}"
    )
    assert isinstance(summary["buffer_size"], int), (
        f"buffer_size must be an int, got {type(summary['buffer_size']).__name__}"
    )

    # After one record_outcome call, the dict reflects the new state —
    # get_summary is a live snapshot, not a cached value.
    learner.record_outcome(
        p_rf=0.55,
        p_gb=0.60,
        p_sgd=0.50,
        p_lgbm=0.58,
        actual=1,
    )
    summary2 = learner.get_summary()
    assert summary2["is_warm"] is False  # still cold (1 update << 50 cadence)
    assert summary2["n_updates"] == 1
    assert summary2["buffer_size"] == 1
