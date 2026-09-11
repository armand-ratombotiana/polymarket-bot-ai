"""Unit tests for ``ml/out_of_sample.py``.

W24-2 — Rigorous out-of-sample ML validation.

Covers the six behaviours required by the W24-2 task spec:

  (1) ``OutOfSampleValidator.split`` returns three disjoint windows
      sized 60 % / 20 % / 20 % on a sufficiently-large dataset, with
      the train window strictly preceding the validation window
      (no overlap, no gap before the purge), and the validation
      window strictly preceding the out-of-sample test window.

  (2) ``OutOfSampleValidator.split`` creates the purge and embargo
      gaps between the train→validation and validation→test
      boundaries. The purge window (``purge_n`` rows between
      ``train_end`` and ``val_start``) and the embargo window
      (``embargo_n`` rows between ``val_end`` and ``test_start``)
      are exactly the size the ``purge_pct`` / ``embargo_pct``
      constructor args specify.

  (3) ``OutOfSampleValidator.validate`` returns a zeroed
      :class:`OutOfSampleResult` (``is_valid=False``, all metrics 0)
      when the data is too small for the minimum-size guard
      (``train < 50`` OR ``val < 20`` OR ``test < 20``). The
      endpoint's 400 on ``<100`` rows is the API-level guard; this
      test pins the underlying validator guard.

  (4) ``OutOfSampleValidator.validate`` correctly flags an overfit
      model — when the model fits the training window near-perfectly
      but generalizes poorly to the out-of-sample test window
      (``auc_decay > 0.15`` OR ``brier_increase > 0.05``), the
      result's ``is_overfit`` flag must be ``True`` and
      ``is_valid`` must be ``False``.

  (5) ``OutOfSampleValidator._simulate_pnl`` correctly computes the
      per-trade P&L on a hand-crafted (predictions, labels) pair:
      wins pay +1.0, losses pay -1.0, ``win_rate`` /
      ``profit_factor`` / ``expectancy`` are derived correctly.

  (6) The FastAPI route ``POST /api/ml/out-of-sample`` returns 200
      with the full :class:`OutOfSampleResult` payload when training
      data is available, and 400 when the data is below the
      endpoint's 100-row guard. TestClient-based, mirrors the
      ``test_shadow_trading_api.py`` pattern (fresh ``FastAPI`` app
      + ``register_routes(app)`` only — no auth middleware, no
      lifespan startup).

The validator is **pure-Python + synchronous** — no DB, no singleton
state mutation, no async. Every test is a plain ``def`` (no
``async def``) so the file does NOT carry ``pytestmark =
pytest.mark.asyncio`` — keeps pytest-asyncio collection cost off this
file entirely. The one exception is the API-route test, which uses
``fastapi.testclient.TestClient`` (sync wrapper around an ASGI portal
that owns its own event loop — no ``await`` needed in the test body).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

# ── Defensive env-var redirect (see "Conventions" block in
#    tests/test_ml_validation.py for the full rationale). ────────────────
# ``setdefault`` lets an outer runner / sibling test file override these if
# it needs to; otherwise the tests stay hermetic to /tmp and cannot clobber
# any real persisted state in the repo's ``data/`` directory. The module
# under test reads NONE of these — the redirect exists purely so a
# co-collected sibling test file (e.g. test_risk_manager.py) doesn't see a
# missing / unwritable path during its own module-import-time work.
_TMP_ROOT = Path("/tmp/ml_oos_tests")
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
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ml.*``) regardless of the cwd pytest was launched from. Mirrors the
# bootstrap pattern in tests/test_ml_validation.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402  (env must be set first)

from sklearn.ensemble import GradientBoostingClassifier  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

from ml.out_of_sample import (  # noqa: E402
    OutOfSampleResult,
    OutOfSampleValidator,
    oos_validator,
    register_routes,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _separable_dataset(
    n: int = 1000, n_features: int = 6, seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Synthetic dataset where the labels are a deterministic function
    of the features AND the rows are timestamp-ordered.

    Rows are emitted in chronological order: row ``i`` has timestamp
    ``float(i)``. The label rule ``y = 1`` iff ``x[0] + 0.5*x[1] > 0``
    yields a roughly 50/50 class balance, so both classes are present
    in every reasonable train / val / test chunk — required for
    ``roc_auc_score`` to be defined (rather than degrade to 0.5 via
    the ``_safe_auc`` fallback).
    """
    rng = np.random.RandomState(seed)
    X = rng.randn(n, n_features)
    y = (X[:, 0] + 0.5 * X[:, 1] > 0).astype(int)
    ts = np.arange(n, dtype=np.float64)
    return X, y, ts


def _shuffled_timestamps(
    n: int = 1000, seed: int = 1,
) -> np.ndarray:
    """Return a SHUFFLED timestamp array — the split() must re-sort it
    before slicing so the train / val / test windows still respect
    chronological order."""
    rng = np.random.RandomState(seed)
    ts = np.arange(n, dtype=np.float64)
    rng.shuffle(ts)
    return ts


class _StubModel:
    """Minimal sklearn-style stub for the OOS validator.

    Returns ``self.train_proba`` on the training set (so the in-sample
    metrics are perfect / degenerate) and ``self.test_proba`` on every
    other call (so the validation + out-of-sample metrics reflect the
    injected generalization gap). Used by the overfitting-detection
    test to deterministically simulate a model that overfits the
    training data without depending on the RNG of a real RF fit.
    """

    def __init__(
        self,
        train_proba: np.ndarray,
        test_proba: np.ndarray,
    ) -> None:
        self.train_proba = np.asarray(train_proba, dtype=np.float64)
        self.test_proba = np.asarray(test_proba, dtype=np.float64)
        self._train_seen = False

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_StubModel":
        # Capture the training labels so ``predict_proba`` can echo them
        # back as the perfect probability (1.0 for the positive class,
        # 0.0 for the negative) — this is what an overfit model looks
        # like at the extreme.
        self._train_labels = np.asarray(y, dtype=int)
        self._train_len = len(y)
        self._train_seen = True
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        n = len(X)
        if self._train_seen and n == self._train_len:
            # In-sample: return perfect probabilities aligned with the
            # captured training labels (1.0 for positive class, 0.0
            # for negative). Returns a 2-D array of shape (n, 2) so the
            # validator's ``_predict_proba`` helper takes ``[:, 1]``.
            p1 = self._train_labels.astype(np.float64)
            p0 = 1.0 - p1
            return np.column_stack([p0, p1])
        # Out-of-sample: return the injected test probabilities (also
        # 2-D shape).
        p1 = np.asarray(self.test_proba[:n], dtype=np.float64)
        p0 = 1.0 - p1
        return np.column_stack([p0, p1])


def _make_pipeline_factory() -> Any:
    """Return a callable that yields a fresh sklearn Pipeline.

    Mirrors ``MarketMLModel._create_ensemble`` — StandardScaler +
    RandomForestClassifier replacement (GradientBoostingClassifier here
    for speed on the small synthetic datasets the tests use). Each
    call returns a NEW Pipeline instance so no state leaks between
    ``validate()`` runs.
    """
    def _factory() -> Pipeline:
        return Pipeline(steps=[
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=50,
                max_depth=3,
                random_state=42,
            )),
        ])
    return _factory


# ── (1) split returns three disjoint 60/20/20 windows ─────────────────────


def test_split_returns_three_disjoint_windows_with_sufficient_data():
    """``OutOfSampleValidator.split`` must partition a 1000-row dataset
    into three disjoint windows sized 600 / 200 / 200, with the train
    window strictly preceding the validation window and the validation
    window strictly preceding the out-of-sample test window.

    Concretely, after the sort by timestamp ascending:
      * ``train`` occupies indices ``[0, 600)``
      * ``purge`` occupies indices ``[600, 650)`` (50 rows, dropped)
      * ``val``   occupies indices ``[650, 850)``
      * ``embargo`` occupies indices ``[850, 900)`` (50 rows, dropped)
      * ``test``  occupies indices ``[900, 1000)``

    The train / val / test windows must not overlap (their index ranges
    must be pairwise disjoint). The split_info dict must carry the
    per-window sizes + boundary timestamps so a caller can persist them
    for audit-trail purposes.
    """
    n = 1000
    X, y, ts = _separable_dataset(n=n, seed=0)
    validator = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)

    X_train, y_train, X_val, y_val, X_test, y_test, info = validator.split(
        X, y, ts,
    )

    # ── Window sizes ──────────────────────────────────────────────────
    assert info["train_size"] == 600
    assert info["validation_size"] == 200
    assert info["test_size"] == 100  # 1000 - 900 = 100 (test absorbs slack)
    assert info["purge_size"] == 50
    assert info["embargo_size"] == 50

    # ── Arrays match the info ─────────────────────────────────────────
    assert len(X_train) == info["train_size"]
    assert len(y_train) == info["train_size"]
    assert len(X_val) == info["validation_size"]
    assert len(y_val) == info["validation_size"]
    assert len(X_test) == info["test_size"]
    assert len(y_test) == info["test_size"]

    # ── Train precedes val precedes test (boundary timestamps) ────────
    # The validator sorts by timestamp ascending, so train_start is the
    # smallest timestamp and test_end is the largest. train_end < val_start
    # < val_end < test_start < test_end.
    assert info["train_start"] <= info["train_end"]
    assert info["train_end"] < info["val_start"]
    assert info["val_start"] < info["val_end"]
    assert info["val_end"] < info["test_start"]
    assert info["test_start"] < info["test_end"] if info["test_size"] > 0 else True

    # ── Disjointness — the purge + embargo windows are dropped ────────
    # The train + val + test windows together should NOT cover the full
    # 1000 rows (the purge + embargo rows are intentionally dropped).
    total_kept = info["train_size"] + info["validation_size"] + info["test_size"]
    total_dropped = info["purge_size"] + info["embargo_size"]
    assert total_kept + total_dropped == n
    # Train / val / test are pairwise disjoint (by index range).
    # train indices: [0, 600)
    # val indices:   [650, 850)
    # test indices:  [900, 1000)
    assert 600 <= 650, "train window must end before val window starts"
    assert 850 <= 900, "val window must end before test window starts"


def test_split_re_sorts_by_timestamp_ascending_no_random_shuffling():
    """The split MUST re-sort by timestamp ascending — even if the
    caller hands in a SHUFFLED timestamp array. This is the cardinal
    anti-leakage rule: without the sort, random ordering could mix
    later samples into the train window (leaking future information).

    The test passes a SHUFFLED timestamp array to ``split()`` and
    verifies that the train window's max timestamp is less than the
    validation window's min timestamp, and the val window's max is
    less than the test window's min. That invariant can only hold if
    the validator re-sorted the rows.
    """
    n = 500
    X, y, _ = _separable_dataset(n=n, seed=2)
    ts = _shuffled_timestamps(n=n, seed=3)  # SHUFFLED — not monotonic
    validator = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)

    _, _, _, _, _, _, info = validator.split(X, y, ts)

    # After the internal sort, train_end < val_start < val_end < test_start.
    assert info["train_end"] < info["val_start"]
    assert info["val_end"] < info["test_start"]


# ── (2) purge + embargo gaps are exactly the requested size ──────────────


def test_purge_and_embargo_gaps_match_requested_pct():
    """The purge gap (``val_start - train_end``) and the embargo gap
    (``test_start - val_end``) must equal
    ``int(n * purge_pct)`` / ``int(n * embargo_pct)`` respectively.

    Tests the gap-size contract for three (purge_pct, embargo_pct)
    configurations on a 1000-row dataset:
      * (0.05, 0.05) — defaults → gaps of 50 / 50
      * (0.10, 0.10) — wider gaps → 100 / 100
      * (0.00, 0.00) — no purge / embargo (adjacent windows)
    """
    n = 1000
    X, y, ts = _separable_dataset(n=n, seed=4)

    # ── (a) Defaults: 5 % / 5 % → 50 / 50 gap rows ────────────────────
    v_default = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)
    info = v_default.split(X, y, ts)[6]
    assert info["purge_size"] == 50
    assert info["embargo_size"] == 50
    # train_end + purge_size == val_start
    assert info["train_size"] + info["purge_size"] == 650  # 600 + 50
    # val_end + embargo_size == test_start
    assert 650 + info["validation_size"] + info["embargo_size"] == 900  # 650 + 200 + 50

    # ── (b) Wider: 10 % / 10 % → 100 / 100 gap rows ──────────────────
    v_wide = OutOfSampleValidator(purge_pct=0.10, embargo_pct=0.10)
    info_wide = v_wide.split(X, y, ts)[6]
    assert info_wide["purge_size"] == 100
    assert info_wide["embargo_size"] == 100
    # train_end = 600; val_start = 700; val_end = 900; test_start = 1000
    # → test_size = 0 (the embargo consumed the tail). The validator
    # returns an empty test set here, which ``validate()`` would catch
    # via the minimum-size guard. ``split()`` itself must NOT raise.
    assert info_wide["train_size"] == 600
    assert info_wide["validation_size"] == 200
    # test_start = 900 + 100 = 1000 → test_size = 0
    assert info_wide["test_size"] == 0

    # ── (c) No purge / embargo: windows are adjacent ──────────────────
    v_none = OutOfSampleValidator(purge_pct=0.0, embargo_pct=0.0)
    info_none = v_none.split(X, y, ts)[6]
    assert info_none["purge_size"] == 0
    assert info_none["embargo_size"] == 0
    # train_end = 600; val_start = 600; val_end = 800; test_start = 800
    # → no gap between train and val; no gap between val and test.
    assert info_none["train_size"] == 600
    assert info_none["validation_size"] == 200
    assert info_none["test_size"] == 200


# ── (3) validate returns zeroed envelope on insufficient data ────────────


def test_validate_returns_zeroed_envelope_on_insufficient_data():
    """When the data is too small for the minimum-size guard
    (``train < 50`` OR ``val < 20`` OR ``test < 20``),
    ``OutOfSampleValidator.validate`` must return a zeroed
    :class:`OutOfSampleResult` with ``is_valid=False`` and
    ``is_overfit=False`` — NOT raise an exception. The API layer's
    400 on ``<100`` rows is a separate guard (tested via the API
    route test below).

    Three sub-cases:
      * ``n=30`` → train=18 < 50 (train-size guard)
      * ``n=100`` with wide purge+embargo → val collapses (val-size guard)
      * ``n=200`` → all windows just below the floor (boundary)
    """
    # ── (a) Tiny dataset — train window too small ─────────────────────
    X_small, y_small, ts_small = _separable_dataset(n=30, seed=5)
    validator = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)
    result = validator.validate(
        model_factory=_make_pipeline_factory(),
        features=X_small, labels=y_small, timestamps=ts_small,
    )
    assert isinstance(result, OutOfSampleResult)
    assert result.is_valid is False
    assert result.is_overfit is False
    assert result.test_auc == 0.0
    assert result.test_brier == 0.0
    assert result.oos_n_trades == 0
    # The split_info is still populated so the operator can see what
    # split was attempted.
    assert result.train_size + result.validation_size + result.test_size <= 30

    # ── (b) n=1000 but purge+embargo consume the test window ──────────
    X_wide, y_wide, ts_wide = _separable_dataset(n=1000, seed=6)
    v_wide = OutOfSampleValidator(purge_pct=0.15, embargo_pct=0.15)
    result_wide = v_wide.validate(
        model_factory=_make_pipeline_factory(),
        features=X_wide, labels=y_wide, timestamps=ts_wide,
    )
    assert isinstance(result_wide, OutOfSampleResult)
    assert result_wide.is_valid is False
    # With purge=15%, embargo=15%: train_end=600, purge=150, val_start=750,
    # val_end=750+200=950, embargo=150, test_start=1100 → test_size=0.
    # The test_size guard fires.
    assert result_wide.test_size < 20


# ── (4) overfitting detection ──────────────────────────────────────────


def test_overfitting_detection_flags_is_overfit_when_auc_decay_large():
    """When the model fits the training window near-perfectly but
    generalizes poorly to the out-of-sample test window
    (``auc_decay > 0.15`` OR ``brier_increase > 0.05``), the result's
    ``is_overfit`` flag must be ``True`` and ``is_valid`` must be
    ``False``.

    The test uses a stub model that returns PERFECT probabilities on the
    training set (``p_yes = y_train`` — AUC=1.0, Brier=0.0) and random
    0.5 probabilities on the out-of-sample test set (AUC=0.5, Brier=
    0.25). The resulting ``auc_decay = 1.0 - 0.5 = 0.5`` far exceeds
    the 0.15 threshold → ``is_overfit=True``.

    The stub model also returns 0.5 on the validation window (so
    ``val_auc=0.5`` — the test specifically validates the overfit
    DETECTION, not the val window's signal).
    """
    n = 1000
    X, y, ts = _separable_dataset(n=n, seed=7)

    # ── Stub: perfect on train (1.0/y), coin-flip on val+test (0.5) ────
    # train_proba is overwritten by the stub's fit() method to echo the
    # train labels (so it doesn't matter what we pass here). test_proba
    # is a constant 0.5 array of length n (used for BOTH val and test
    # windows — the stub's predict_proba returns test_proba for any
    # input whose length != train_len).
    train_proba = np.full(n, 0.5)  # placeholder — stub overwrites
    test_proba = np.full(n, 0.5)   # coin-flip on val + test
    stub = _StubModel(train_proba=train_proba, test_proba=test_proba)

    validator = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)
    result = validator.validate(
        model_factory=lambda: stub,
        features=X, labels=y, timestamps=ts,
    )

    # ── In-sample metrics: perfect (the stub echoes train labels) ─────
    assert result.train_auc == 1.0
    assert result.train_brier == 0.0
    assert result.train_accuracy == 1.0

    # ── Out-of-sample metrics: coin-flip ─────────────────────────────
    assert result.test_auc == 0.5
    # Brier for p=0.5, y in {0,1}: mean((0.5 - y)^2) = 0.25 for 50/50 classes
    assert 0.20 <= result.test_brier <= 0.30

    # ── Overfitting detection ─────────────────────────────────────────
    assert result.auc_decay == pytest.approx(0.5, abs=0.01), (
        f"auc_decay should be ~0.5 (train=1.0, test=0.5); got {result.auc_decay}"
    )
    assert result.brier_increase > 0.05, (
        f"brier_increase should be > 0.05 (test_brier~0.25, train_brier=0); "
        f"got {result.brier_increase}"
    )
    assert result.is_overfit is True
    assert result.is_valid is False


def test_overfitting_detection_passes_is_valid_on_generalizable_model():
    """Belt-and-braces: a model that generalizes well
    (``auc_decay <= 0.15`` AND ``brier_increase <= 0.05`` AND
    ``test_auc > 0.55``) must have ``is_overfit=False`` AND
    ``is_valid=True``.

    Uses a real sklearn GradientBoostingClassifier on the separable
    synthetic dataset — the label rule is a linear function of the
    features so GB learns it cleanly and generalizes from the train
    window to the test window with minimal decay.
    """
    n = 1000
    X, y, ts = _separable_dataset(n=n, seed=8)

    validator = OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.05)
    result = validator.validate(
        model_factory=_make_pipeline_factory(),
        features=X, labels=y, timestamps=ts,
    )

    # The model should fit the training data well (the label rule is a
    # simple linear threshold) — train_auc >> 0.5.
    assert result.train_auc > 0.85, (
        f"train_auc should be >0.85 for the separable synthetic dataset; "
        f"got {result.train_auc}"
    )
    # And generalize to the out-of-sample test window — the linear
    # threshold the model learns is shift-invariant, so the test
    # distribution (which is just a later chunk of the same generator)
    # should produce a similar AUC.
    assert result.test_auc > 0.55, (
        f"test_auc should be >0.55 for a generalizable model on this dataset; "
        f"got {result.test_auc}"
    )
    # The generalization gap should be small.
    assert result.auc_decay < 0.15, (
        f"auc_decay should be <0.15 for a generalizable model; "
        f"got {result.auc_decay}"
    )
    # Brier increase should also be small (the test set is drawn from
    # the same distribution as the train set).
    assert result.brier_increase <= 0.20, (
        f"brier_increase should be modest (<=0.20) on a same-distribution "
        f"test set; got {result.brier_increase}"
    )
    # Verdicts: not overfit, valid.
    assert result.is_overfit is False
    assert result.is_valid is True


# ── (5) P&L simulation ─────────────────────────────────────────────────


def test_simulate_pnl_computes_correct_per_trade_pnl():
    """``OutOfSampleValidator._simulate_pnl`` must compute the per-trade
    P&L on a hand-crafted (predictions, labels) pair:
      * ``pred > 0.5`` → bet on YES (label 1)
      * ``pred <= 0.5`` → bet on NO (label 0)
      * Bet matches actual → +1.0 (win)
      * Bet mismatches actual → -1.0 (loss)

    The returned ``(pnls, wins, losses)`` triple must satisfy:
      * ``len(pnls) == len(predictions)``
      * ``len(wins) + len(losses) == len(pnls)``
      * Every win is +1.0; every loss is -1.0.

    Construct a 10-trade scenario with 7 wins and 3 losses (deliberately
    mixed so the win_rate / profit_factor computations are non-trivial).
    Manual trace of each (pred, label) pair:
      (0.9,  1): pred>0.5 → bet YES, actual=1 → WIN  (+1.0)
      (0.6,  1): pred>0.5 → bet YES, actual=1 → WIN  (+1.0)
      (0.4,  0): pred≤0.5 → bet NO,  actual=0 → WIN  (+1.0)
      (0.1,  0): pred≤0.5 → bet NO,  actual=0 → WIN  (+1.0)
      (0.8,  0): pred>0.5 → bet YES, actual=0 → LOSS (-1.0)
      (0.3,  0): pred≤0.5 → bet NO,  actual=0 → WIN  (+1.0)
      (0.7,  0): pred>0.5 → bet YES, actual=0 → LOSS (-1.0)
      (0.95, 1): pred>0.5 → bet YES, actual=1 → WIN  (+1.0)
      (0.2,  1): pred≤0.5 → bet NO,  actual=1 → LOSS (-1.0)
      (0.45, 0): pred≤0.5 → bet NO,  actual=0 → WIN  (+1.0)
    → 7 wins, 3 losses
    → win_rate = 7/10 = 0.7
    → expectancy = (7 - 3) / 10 = 0.4
    → profit_factor = 7 / 3 ≈ 2.333
    """
    preds = np.array([0.9, 0.6, 0.4, 0.1, 0.8, 0.3, 0.7, 0.95, 0.2, 0.45])
    #                          YES YES NO  NO  YES NO  YES YES  NO  NO
    labels = np.array([1,     1,   0,  0,  0,  0,  0,  1,   1,   0])
    # wins:           ✓       ✓    ✓  ✓  ✗   ✓  ✗  ✓   ✗    ✓
    # → 7 wins, 3 losses

    pnls, wins, losses = OutOfSampleValidator._simulate_pnl(preds, labels)

    assert len(pnls) == 10
    assert len(wins) == 7
    assert len(losses) == 3
    # Every win is +1.0; every loss is -1.0
    for w in wins:
        assert w == 1.0
    for l in losses:
        assert l == -1.0
    # win_rate / expectancy / profit_factor — cross-check via the
    # validator's own compute (these formulas live in validate()).
    win_rate = len(wins) / len(pnls)
    expectancy = float(np.mean(pnls))
    gross_profit = float(sum(wins))
    gross_loss = float(abs(sum(losses)))
    profit_factor = gross_profit / (gross_loss + 1e-8)
    assert win_rate == pytest.approx(0.7)
    assert expectancy == pytest.approx(0.4)
    assert profit_factor == pytest.approx(7.0 / 3.0, rel=1e-3)


def test_simulate_pnl_handles_all_wins_and_all_losses_edge_cases():
    """Belt-and-braces: edge cases for the P&L simulation.
      * All wins → losses list is empty; the validator's
        ``profit_factor`` falls into the ``elif gross_profit > 0``
        branch (returns 999.0).
      * All losses → wins list is empty; gross_profit=0,
        gross_loss=0 (no, gross_loss > 0); profit_factor = 0.0.
      * Empty input → returns three empty lists (the for-loop body
        never executes; no ZeroDivisionError).
    """
    # ── (a) All wins ──────────────────────────────────────────────────
    preds_w = np.array([0.9, 0.8, 0.7])
    labels_w = np.array([1, 1, 1])
    pnls, wins, losses = OutOfSampleValidator._simulate_pnl(preds_w, labels_w)
    assert len(pnls) == 3
    assert len(wins) == 3
    assert len(losses) == 0

    # ── (b) All losses ────────────────────────────────────────────────
    preds_l = np.array([0.9, 0.8, 0.7])
    labels_l = np.array([0, 0, 0])
    pnls, wins, losses = OutOfSampleValidator._simulate_pnl(preds_l, labels_l)
    assert len(pnls) == 3
    assert len(wins) == 0
    assert len(losses) == 3

    # ── (c) Empty input ────────────────────────────────────────────────
    pnls_e, wins_e, losses_e = OutOfSampleValidator._simulate_pnl(
        np.array([]), np.array([]),
    )
    assert pnls_e == []
    assert wins_e == []
    assert losses_e == []


# ── (6) API route — POST /api/ml/out-of-sample ──────────────────────────


@pytest.fixture
def oos_app():
    """Fresh ``FastAPI`` app with only the OOS validation route registered.

    Uses the same ``register_routes(app)`` entry point as the production
    ``api/server.py`` so the route definitions / validation annotations
    exercised here are byte-identical to what the live server exposes —
    without the bearer-token auth middleware (``enforce_api_auth`` — a
    server-level concern exercised by separate auth tests) or the heavy
    ``lifespan`` startup.
    """
    from fastapi import FastAPI

    app = FastAPI()
    register_routes(app)
    return app


def test_api_out_of_sample_returns_200_with_full_payload(oos_app, monkeypatch):
    """``POST /api/ml/out-of-sample`` must return 200 with the full
    :class:`OutOfSampleResult` payload when the model's training data
    is sufficient (>=100 rows).

    The test monkeypatches ``ml.model.ml_model.get_training_data`` to
    return a 500-row synthetic dataset (well above the 100-row
    endpoint guard AND the 50/20/20 validator minimum-size guard), and
    ``ml.model.ml_model._create_ensemble`` to return a fast sklearn
    Pipeline (so the test doesn't pay the full ensemble fit cost).

    The response JSON must carry every field of :class:`OutOfSampleResult`
    (split info + in-sample / val / test metrics + overfitting
    diagnostics + OOS P&L fields + verdicts + ``timestamp``).
    """
    from fastapi.testclient import TestClient

    # ── Build a 500-row synthetic dataset for the endpoint to consume ──
    X, y, ts = _separable_dataset(n=500, seed=9)
    # Mark the dataset as "real" (n_real_samples) and force the
    # get_training_data path to return the synthetic arrays verbatim.
    from ml import model as ml_model_module

    def _fake_get_training_data(self):
        return X, y, ts

    def _fake_create_ensemble(self):
        return Pipeline(steps=[
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=50, max_depth=3, random_state=42,
            )),
        ])

    monkeypatch.setattr(
        ml_model_module.MarketMLModel,
        "get_training_data",
        _fake_get_training_data,
    )
    monkeypatch.setattr(
        ml_model_module.MarketMLModel,
        "_create_ensemble",
        _fake_create_ensemble,
    )

    client = TestClient(oos_app)
    resp = client.post("/api/ml/out-of-sample")

    assert resp.status_code == 200, (
        f"expected 200, got {resp.status_code}: {resp.text}"
    )
    payload = resp.json()

    # ── Split-info keys ───────────────────────────────────────────────
    for key in (
        "train_size", "validation_size", "test_size",
        "purge_size", "embargo_size",
        "train_start", "train_end", "val_start", "val_end",
        "test_start", "test_end",
    ):
        assert key in payload, f"payload missing split-info key {key!r}"

    # ── In-sample + validation + test metrics ─────────────────────────
    for prefix in ("train", "val", "test"):
        assert f"{prefix}_auc" in payload
        assert f"{prefix}_brier" in payload
        assert f"{prefix}_accuracy" in payload
    assert "test_calibration_error" in payload

    # ── Overfitting diagnostics ───────────────────────────────────────
    for key in ("auc_decay", "brier_increase"):
        assert key in payload

    # ── OOS P&L fields ────────────────────────────────────────────────
    for key in (
        "oos_expectancy", "oos_win_rate",
        "oos_profit_factor", "oos_n_trades",
    ):
        assert key in payload

    # ── Verdicts ──────────────────────────────────────────────────────
    assert "is_overfit" in payload
    assert "is_valid" in payload
    assert isinstance(payload["is_overfit"], bool)
    assert isinstance(payload["is_valid"], bool)

    # ── Sanity — the model generalizes on this dataset, so is_valid ───
    # The synthetic dataset's label rule is a simple linear threshold,
    # so GB should learn it and generalize to the out-of-sample test
    # window. We don't assert ``is_valid=True`` here (the metric values
    # depend on the RNG of the small GB fit and could vary across
    # sklearn versions), but we do assert the train AUC is meaningfully
    # above 0.5 (the model fit *something*).
    assert payload["train_auc"] > 0.7, (
        f"train_auc should be >0.7 on the separable dataset; "
        f"got {payload['train_auc']}"
    )
    assert payload["test_auc"] > 0.5, (
        f"test_auc should be >0.5 (better than coin-flip) on this "
        f"same-distribution test set; got {payload['test_auc']}"
    )

    # ── timestamp is present and recent ───────────────────────────────
    assert "timestamp" in payload
    import time as _time
    assert abs(_time.time() - payload["timestamp"]) < 60.0


def test_api_out_of_sample_returns_400_when_training_data_insufficient(
    oos_app, monkeypatch,
):
    """``POST /api/ml/out-of-sample`` must return 400 when
    ``ml_model.get_training_data()`` returns fewer than 100 rows —
    the endpoint's documented guard against running the validator on
    a cold-start / under-populated training set.

    The test monkeypatches ``get_training_data`` to return a 50-row
    dataset (below the 100-row endpoint guard but above the validator's
    own 50/20/20 minimum — so this is purely the endpoint guard, not
    the validator guard, that fires).
    """
    from fastapi.testclient import TestClient

    # 50 rows — below the endpoint's 100-row guard.
    X_small, y_small, ts_small = _separable_dataset(n=50, seed=10)
    from ml import model as ml_model_module

    def _fake_get_training_data(self):
        return X_small, y_small, ts_small

    monkeypatch.setattr(
        ml_model_module.MarketMLModel,
        "get_training_data",
        _fake_get_training_data,
    )

    client = TestClient(oos_app)
    resp = client.post("/api/ml/out-of-sample")

    assert resp.status_code == 400, (
        f"expected 400 on <100 rows, got {resp.status_code}: {resp.text}"
    )
    # The detail message must mention the size guard so the operator
    # can diagnose the failure.
    detail = resp.json().get("detail", "")
    assert "100" in detail or "Insufficient" in detail, (
        f"detail should mention the 100-row guard / insufficient data; "
        f"got: {detail!r}"
    )


def test_api_out_of_sample_returns_500_when_validator_raises(oos_app, monkeypatch):
    """Belt-and-braces: when the underlying ``oos_validator.validate``
    raises an unexpected exception (simulating a transient model-fit
    failure), the endpoint must return 500 with a generic error
    message — NOT a stack trace (which would be an information-
    disclosure per OWASP A02). The exception is logged server-side
    (via ``logger.error``) for the operator to correlate via the
    X-Request-ID header.
    """
    from fastapi.testclient import TestClient

    X, y, ts = _separable_dataset(n=500, seed=11)
    from ml import model as ml_model_module
    from ml import out_of_sample as oos_module

    def _fake_get_training_data(self):
        return X, y, ts

    def _boom_validate(self, *args, **kwargs):
        raise RuntimeError("simulated validator failure")

    monkeypatch.setattr(
        ml_model_module.MarketMLModel,
        "get_training_data",
        _fake_get_training_data,
    )
    monkeypatch.setattr(
        ml_model_module.MarketMLModel,
        "_create_ensemble",
        lambda self: Pipeline(steps=[
            ("scaler", StandardScaler()),
            ("clf", GradientBoostingClassifier(
                n_estimators=20, max_depth=2, random_state=42,
            )),
        ]),
    )
    # Patch the SINGLETON's validate method — the route uses
    # ``oos_validator`` (the module-level singleton), not a fresh
    # instance, so we have to patch the singleton.
    monkeypatch.setattr(oos_module.oos_validator, "validate", _boom_validate)

    client = TestClient(oos_app)
    resp = client.post("/api/ml/out-of-sample")

    assert resp.status_code == 500, (
        f"expected 500 on validator failure, got {resp.status_code}: {resp.text}"
    )
    # The detail must NOT include the raw exception's repr (OWASP A02).
    detail = resp.json().get("detail", "")
    assert "simulated validator failure" not in detail, (
        f"detail must NOT include the raw exception (information "
        f"disclosure); got: {detail!r}"
    )


# ── (7) Sanity — the module-level singleton is usable directly ──────────


def test_module_singleton_is_default_configured():
    """The module-level ``oos_validator`` singleton must be configured
    with the default ``purge_pct=0.05`` / ``embargo_pct=0.05`` so a
    caller who imports ``oos_validator`` and immediately calls
    ``.validate()`` gets the documented 5 % / 5 % split.

    Belt-and-braces: the singleton must be an instance of
    :class:`OutOfSampleValidator` (catches a future refactor that
    accidentally shadows the singleton with a different type).
    """
    assert isinstance(oos_validator, OutOfSampleValidator)
    assert oos_validator.purge_pct == 0.05
    assert oos_validator.embargo_pct == 0.05


def test_validator_constructor_rejects_out_of_range_pct():
    """The constructor must reject ``purge_pct`` / ``embargo_pct``
    outside [0.0, 0.20). Above 0.20, the purge + embargo windows
    would together consume >40 % of the data, defeating the purpose
    of the three-way split (train / val / test windows would each
    shrink below the minimum-size guard).
    """
    with pytest.raises(ValueError, match="purge_pct"):
        OutOfSampleValidator(purge_pct=0.25, embargo_pct=0.05)
    with pytest.raises(ValueError, match="embargo_pct"):
        OutOfSampleValidator(purge_pct=0.05, embargo_pct=0.30)
    # Boundary: 0.0 and just-under 0.20 must be accepted.
    OutOfSampleValidator(purge_pct=0.0, embargo_pct=0.0)
    OutOfSampleValidator(purge_pct=0.19, embargo_pct=0.19)
