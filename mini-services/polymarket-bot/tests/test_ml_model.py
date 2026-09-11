"""
tests/test_ml_model.py — Unit tests for ``ml/model.py`` (V10).

Covers the eight behaviours required by the V10 task spec:

  (1) ``predict()`` returns a ``(p_yes, confidence)`` 2-tuple of two
      floats — the documented public return contract.
  (2) ``p_yes`` is always clipped into the closed interval
      ``[0.01, 0.99]`` — the explicit ``np.clip(p_yes, 0.01, 0.99)``
      guard at the tail of ``MarketMLModel.predict()``.
  (3) ``confidence == abs(p_yes - 0.5) * 2`` — the documented
      calibration formula inside ``predict()``.
  (4) ``is_fitted`` is ``False`` on a freshly-constructed
      ``MarketMLModel()`` before any training — the ``@property``
      returns ``self.rf is not None`` and ``rf`` starts as ``None``.
  (5) ``_compute_sharpe_from_equity()`` returns ``0.0`` when
      ``store.equity_history`` has fewer than 2 points (degenerate /
      cold-start short-circuit).
  (6) ``_compute_sharpe_from_equity()`` returns a strictly-positive
      value when the equity series is monotonically increasing
      (every per-bar return is positive ⇒ mean > 0 and std finite ⇒
      Sharpe > 0).
  (7) ``training_source == "synthetic_only"`` when ``fit_initial()``
      is invoked with no real DB data
      (``timescale_db.fetch_training_samples`` mocked to return
      ``(None, [])``) — the documented fallback branch at the head
      of ``fit_initial()``.
  (8) ``n_real_samples`` starts at ``0`` on a fresh
      ``MarketMLModel()`` instance (documented initial value in
      ``__init__``).

Test isolation strategy
-----------------------
* ``conftest.py`` already redirects every persisted-state path
  (``MODEL_PATH``, ``MODEL_REGISTRY_PATH``, ``MARKET_DB_PATH``, …) into
  a writable ``/tmp/pmbot_conftest_isolation`` sandbox and exposes the
  autouse ``_reset_store_factory_defaults`` fixture that resets the
  global ``store`` singleton (incl. ``equity_history``) to a 1-point
  factory baseline before every test. Tests (5) and (6) build on that
  baseline by either trusting the 1-point default (test 5) or explicitly
  overriding ``store.equity_history`` with a controlled multi-point
  series (test 6).
* The ``fitted_model`` fixture mocks
  ``core.timescale_db.timescale_db.fetch_training_samples`` to return
  ``(None, [])`` so ``fit_initial()`` exercises its synthetic-only branch
  (the V10 "no real data" scenario). It additionally patches
  ``ml.model._synthetic_training_data`` to generate a 100-row dataset
  (instead of the production 3000) and shrinks the RF / GB estimator
  counts to 10 each so the per-test fit wall-time is ~1.3 s instead of
  ~25 s. These hyperparameter overrides do NOT affect any of the V10
  assertions, which inspect only the ``predict()`` return contract and
  the ``training_source`` / ``n_real_samples`` provenance fields.
* Tests (4) and (8) construct a bare ``MarketMLModel()`` (no
  ``fit_initial``) — the ``__init__`` defaults are the load-bearing
  observation. They do NOT use the ``fitted_model`` fixture.

The repo's ``pytest.ini`` declares ``testpaths = tests``; this file is
collected automatically. The sibling ``tests/conftest.py`` is imported
BEFORE this module so its env-var redirects + ``sys.path`` bootstrap
are already in effect — no inline redirection needed here, only an
inline ``sys.path`` insertion as a belt-and-braces measure for IDE /
direct ``pytest tests/test_ml_model.py`` runs.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

# Inline sys.path bootstrap — mirrors the pattern in test_features.py
# and tests/conftest.py so ``from ml.model import ...`` resolves regardless
# of the cwd pytest was launched from (monorepo root, CI checkout, IDE
# runner, …).
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from core.data_store import store  # noqa: E402
from ml.features import N_FEATURES  # noqa: E402
from ml.model import (  # noqa: E402
    MarketMLModel,
    _synthetic_training_data,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_features(mid_price: float = 0.5) -> np.ndarray:
    """Build a minimal valid 38-dim float32 feature vector.

    The only entry that affects the ``predict()`` fast path on an UNFITTED
    model is index 0 (mid_price) — that's what ``predict()`` falls back to
    via ``float(features[0])`` when ``rf``/``gb`` are ``None``. On a FITTED
    model every entry is fed through ``scaler.transform`` so the vector
    must be the right length; we zero everything except ``mid_price`` so
    the scaled input is well-defined.
    """
    vec = np.zeros(N_FEATURES, dtype=np.float32)
    vec[0] = float(mid_price)
    return vec


@pytest.fixture
def fitted_model():
    """A freshly-trained ``MarketMLModel()`` instance trained on synthetic
    data only.

    Mocks ``core.timescale_db.timescale_db.fetch_training_samples`` to
    return ``(None, [])`` so the model trains on synthetic data only —
    leaving ``training_source == "synthetic_only"`` and
    ``n_real_samples == 0`` (the V10 "no real data" scenario used by
    tests 1, 2, 3, 7).

    Patches ``ml.model._synthetic_training_data`` to generate a 100-row
    dataset (instead of the production 3000) and shrinks RF / GB
    estimator counts to 10 each so the per-test ``fit_initial`` wall
    time is ~1.3 s (the production 3000-sample / 150+100-estimator config
    takes ~25 s and would dominate the test session).

    The patches use the ``with patch(...):`` context-manager form so
    they are automatically reverted when the fixture's setup block
    exits — the returned ``m`` is a fully-trained standalone instance
    whose ``predict()`` path does NOT depend on the patched mocks
    (``timescale_db.record_prediction`` is wrapped in try/except
    inside ``predict()`` so the real singleton is safe to invoke).
    """
    # The patched synth generator accepts the same `n` positional/kw arg
    # signature as the real one but ignores it and always returns 100
    # rows. This lets us patch the module-level reference
    # `_synthetic_training_data` inside `ml.model` (which `fit_initial`
    # calls as a bare name) without breaking callers that pass `n`
    # positionally.
    def _small_synth(n: int = 100) -> tuple[np.ndarray, np.ndarray]:
        return _synthetic_training_data(100)

    with patch("ml.model._synthetic_training_data", _small_synth), \
         patch("core.timescale_db.timescale_db") as mock_db:
        # fetch_training_samples returns (X_db, y_db); (None, []) forces
        # the synthetic-only branch in fit_initial.
        mock_db.fetch_training_samples.return_value = (None, [])
        m = MarketMLModel()
        # Smaller estimator counts keep the fit wall-time tractable in
        # CI without affecting any of the V10 assertions (which only
        # inspect the predict() return contract, not prediction quality).
        m.fit_initial(n_estimators_rf=10, n_estimators_gb=10)
    return m


# ── (1) predict() returns a (p_yes, confidence) tuple of floats ──────────────

def test_predict_returns_p_yes_confidence_tuple(fitted_model):
    """``predict()`` must return a 2-tuple ``(p_yes: float, confidence: float)``.

    The ``predict()`` signature declares ``-> tuple[float, float]`` and
    every return path (success, unfitted fallback, exception fallback)
    yields a 2-tuple of floats. We exercise the success path on the
    ``fitted_model`` fixture and assert both the container type and the
    inner element types.
    """
    features = _make_features(mid_price=0.5)
    result = fitted_model.predict(features, token_id="V10_T1")

    assert isinstance(result, tuple), (
        f"predict must return a tuple, got {type(result).__name__}"
    )
    assert len(result) == 2, (
        f"predict tuple must have exactly 2 elements, got {len(result)}"
    )

    p_yes, confidence = result
    assert isinstance(p_yes, float), (
        f"p_yes must be a float, got {type(p_yes).__name__}"
    )
    assert isinstance(confidence, float), (
        f"confidence must be a float, got {type(confidence).__name__}"
    )


# ── (2) p_yes is clipped into [0.01, 0.99] ───────────────────────────────────

@pytest.mark.parametrize(
    "mid_price",
    [0.05, 0.25, 0.50, 0.75, 0.95],
)
def test_p_yes_is_in_01_99_range(fitted_model, mid_price):
    """``p_yes`` must always lie in the closed interval ``[0.01, 0.99]``.

    The explicit ``np.clip(p_yes, 0.01, 0.99)`` guard at the tail of
    ``predict()`` enforces this regardless of the meta-learner's / base
    learners' raw probability output. Probed across a spread of feature
    vectors (varying ``mid_price``) to exercise the clip boundary even
    on extreme inputs.
    """
    features = _make_features(mid_price=mid_price)
    p_yes, _ = fitted_model.predict(features, token_id="V10_T2")

    assert 0.01 <= p_yes <= 0.99, (
        f"p_yes={p_yes!r} for mid_price={mid_price} is outside [0.01, 0.99]"
    )


# ── (3) confidence == abs(p_yes - 0.5) * 2 ───────────────────────────────────

@pytest.mark.parametrize(
    "mid_price",
    [0.05, 0.25, 0.50, 0.75, 0.95],
)
def test_confidence_equals_abs_p_yes_minus_half_times_two(fitted_model, mid_price):
    """``confidence`` must equal ``|p_yes - 0.5| * 2`` — the documented
    calibration formula inside ``MarketMLModel.predict()``.

    ``confidence = abs(p_yes - 0.5) * 2.0`` is set on the success path
    (right before the ``return``). A predict exception would yield the
    fallback tuple ``(float(features[0]), 0.5)`` — at ``mid_price=0.5``
    that fallback's confidence ``0.5`` would NOT match
    ``abs(0.5 - 0.5) * 2 = 0.0``, so this test also implicitly guards
    against the exception path firing silently.
    """
    features = _make_features(mid_price=mid_price)
    p_yes, confidence = fitted_model.predict(features, token_id="V10_T3")

    expected = abs(p_yes - 0.5) * 2.0
    assert confidence == pytest.approx(expected, abs=1e-9), (
        f"confidence={confidence!r} != |p_yes-0.5|*2={expected!r} "
        f"for mid_price={mid_price} (p_yes={p_yes!r})"
    )

    # Belt-and-braces: confidence is bounded in [0, 1] by construction.
    assert 0.0 <= confidence <= 1.0, (
        f"confidence={confidence!r} outside [0.0, 1.0]"
    )


# ── (4) is_fitted is False before training ───────────────────────────────────

def test_is_fitted_false_before_training():
    """A freshly-constructed ``MarketMLModel()`` (no ``fit_initial`` call)
    must report ``is_fitted == False`` — the ``@property`` returns
    ``self.rf is not None`` and ``rf`` starts as ``None`` in
    ``__init__``.
    """
    m = MarketMLModel()
    assert m.is_fitted is False, (
        "is_fitted must be False on a fresh MarketMLModel() before any "
        "fit_initial() call"
    )
    # Belt-and-braces: rf / gb / rf_cal / gb_cal are explicitly None on a
    # fresh instance (the @property only inspects rf, but gb is the other
    # gate in predict()'s fast-fallback path).
    assert m.rf is None, "rf must be None on a fresh MarketMLModel()"
    assert m.gb is None, "gb must be None on a fresh MarketMLModel()"


# ── (5) _compute_sharpe_from_equity returns 0.0 for <2 points ────────────────

def test_compute_sharpe_from_equity_returns_zero_for_fewer_than_two_points():
    """``_compute_sharpe_from_equity()`` must return ``0.0`` when
    ``store.equity_history`` has fewer than 2 points — the documented
    short-circuit at the head of the method
    (``if not history or len(history) < 2: return 0.0``).

    The ``conftest.py`` autouse ``_reset_store_factory_defaults``
    fixture already resets ``store.equity_history`` to a single-point
    list before every test, but we explicitly set the value here to make
    the test's intent unambiguous and resilient to any future conftest
    changes.
    """
    # 1-point history → short-circuit.
    store.equity_history = [
        {"timestamp": time.time(), "equity": 100.0, "pnl": 0.0}
    ]
    assert len(store.equity_history) < 2
    sharpe = MarketMLModel._compute_sharpe_from_equity()
    assert sharpe == 0.0, (
        f"Sharpe for 1-point history must be 0.0, got {sharpe!r}"
    )

    # Empty history → short-circuit.
    store.equity_history = []
    sharpe_empty = MarketMLModel._compute_sharpe_from_equity()
    assert sharpe_empty == 0.0, (
        f"Sharpe for empty history must be 0.0, got {sharpe_empty!r}"
    )

    # None history (defensive — store.equity_history is normally a list).
    store.equity_history = None  # type: ignore[assignment]
    sharpe_none = MarketMLModel._compute_sharpe_from_equity()
    assert sharpe_none == 0.0, (
        f"Sharpe for None history must be 0.0, got {sharpe_none!r}"
    )


# ── (6) _compute_sharpe_from_equity returns positive for upward trend ────────

def test_compute_sharpe_from_equity_returns_positive_for_upward_trend():
    """When ``store.equity_history`` is a monotonically-increasing series,
    ``_compute_sharpe_from_equity()`` must return a strictly-positive
    value — every per-bar simple return is positive, so ``mean(rets) > 0``
    and ``std(rets, ddof=1)`` is finite and non-zero, giving
    ``sharpe_bar = mu / sigma > 0``.

    The annualisation multiplier ``sqrt(bars_per_year)`` is strictly
    positive, so it cannot flip the sign.
    """
    t0 = time.time()
    # 10 monotonically-increasing equity points spaced 1 second apart.
    # The per-bar returns (1/100, 1/101, 1/102, …) are all strictly
    # positive AND non-constant, so sigma > 1e-12 (no degenerate-zero
    # short-circuit) while mu > 0.
    equities = [100.0, 101.0, 102.0, 103.0, 104.0,
                105.0, 106.0, 107.0, 108.0, 109.0]
    store.equity_history = [
        {"timestamp": t0 + i, "equity": eq, "pnl": eq - 100.0}
        for i, eq in enumerate(equities)
    ]
    sharpe = MarketMLModel._compute_sharpe_from_equity()
    assert sharpe > 0.0, (
        f"Sharpe for monotonic upward trend must be > 0.0, got {sharpe!r}"
    )
    # Belt-and-braces: must be a finite, finite-precision float.
    assert isinstance(sharpe, float), (
        f"Sharpe must be a float, got {type(sharpe).__name__}"
    )
    assert np.isfinite(sharpe), (
        f"Sharpe must be finite, got {sharpe!r}"
    )


# ── (7) training_source is "synthetic_only" when no real data ────────────────

def test_training_source_is_synthetic_only_when_no_real_data(fitted_model):
    """When ``fit_initial()`` is called and ``timescale_db`` returns no
    real samples (mocked), ``training_source`` must be ``"synthetic_only"``
    — the documented fallback branch at the head of ``fit_initial()``.

    The ``fitted_model`` fixture patches
    ``timescale_db.fetch_training_samples`` to return ``(None, [])`` and
    then calls ``fit_initial()``. The synthetic-only branch executes,
    setting ``self.training_source = "synthetic_only"`` and leaving
    ``self.n_real_samples`` at its ``__init__`` default of ``0``.
    """
    assert fitted_model.training_source == "synthetic_only", (
        f"training_source must be 'synthetic_only' when no real DB data, "
        f"got {fitted_model.training_source!r}"
    )
    # Belt-and-braces: the synthetic-only branch also leaves
    # n_real_samples untouched at its __init__ default of 0, and
    # n_synthetic_samples reflects the synth dataset size.
    assert fitted_model.n_real_samples == 0, (
        f"n_real_samples must remain 0 on synthetic-only fit, got "
        f"{fitted_model.n_real_samples!r}"
    )
    assert fitted_model.n_synthetic_samples > 0, (
        f"n_synthetic_samples must be > 0 after a synthetic-only fit, "
        f"got {fitted_model.n_synthetic_samples!r}"
    )


# ── (8) n_real_samples starts at 0 ───────────────────────────────────────────

def test_n_real_samples_starts_at_zero():
    """A freshly-constructed ``MarketMLModel()`` (no ``fit_initial`` call)
    must report ``n_real_samples == 0`` — the documented initial value
    in ``__init__``.

    ``self.n_real_samples: int = 0`` is set in ``__init__`` and only
    mutated inside ``fit_initial()``'s real-DB branch
    (``self.n_real_samples = len(X_db)``). The cold-start / no-real-data
    path leaves it at 0.
    """
    m = MarketMLModel()
    assert m.n_real_samples == 0, (
        f"n_real_samples must start at 0 on a fresh MarketMLModel(), "
        f"got {m.n_real_samples!r}"
    )
    # Belt-and-braces: type is int, not numpy / float.
    assert isinstance(m.n_real_samples, int), (
        f"n_real_samples must be an int, got {type(m.n_real_samples).__name__}"
    )
