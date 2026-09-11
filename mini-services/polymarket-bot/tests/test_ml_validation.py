"""
tests/test_ml_validation.py — Unit tests for ``ml/validation.py``.

U5 — ML validation unit tests.

Covers the six behaviours required by the task spec:

  (1) ``time_series_cv`` returns per-fold Brier / AUC — every fold dict
      in ``per_fold`` carries a numeric ``brier`` and (when both classes
      are present in the validation chunk) a numeric ``auc`` key. The
      per-fold metrics are the load-bearing contract for diagnosing
      fold-by-fold quality (e.g. spotting a single fold where the model
      collapsed vs. a uniform degradation across all folds).
  (2) Train indices precede validation indices in each fold — the
      walk-forward contract is that the model is *always* trained on
      strictly-prior observations and evaluated on the immediately-
      following chunk. Concretely, for every fold ``k`` the training
      indices ``[0, train_end_index)`` and validation indices
      ``[val_start_index, val_end_index)`` are disjoint with
      ``train_end_index <= val_start_index``; across folds the
      ``val_start_index`` is strictly increasing (expanding-window
      property — later folds train on strictly more data, never re-
      using a validation chunk as training data).
  (3) ``out_of_time_test`` returns a ``metrics`` dict with the
      canonical binary-classification suite (Brier / ROC-AUC / log-loss
      / accuracy) plus split-size metadata (train_size / test_size /
      n_features / n_samples) and the raw per-row ``predictions`` /
      ``actuals`` arrays for downstream calibration analysis.
  (4) ``validate_no_leakage`` flags exact-duplicate feature vectors —
      exact row bytes reappearing in the matrix is the canonical data-
      duplication signal (often caused by upstream re-emission of the
      same observation under different timestamps, or by a join that
      fans out one row into many). Implementation puts the flag in
      ``warnings`` (advisory — duplicates are suspicious only if they
      span a train/test boundary) and bumps ``stats.n_duplicate_rows``.
  (5) ``validate_no_leakage`` flags near-duplicate features (rounded to
      4 dp) with CONFLICTING labels — the strongest leakage signal:
      identical inputs producing different outputs means hidden state
      (timestamp, ID, future-leaked feature) is determining the label.
      Implementation puts the flag in ``issues`` (blocking —
      ``is_valid = False``) and bumps
      ``stats.n_near_dup_label_conflicts``.
  (6) ``validate_no_leakage`` passes (``is_valid = True``) on clean
      synthetic data — distinct feature rows, binary labels in ``{0, 1}``,
      balanced classes, no NaN/Inf. The audit emits NO issues and the
      advisory warnings list is empty (or contains only the expected
      ``severe label imbalance`` / ``only one class`` style entries —
      which clean data does NOT trip).

The validation module is **pure-Python + synchronous** — no DB, no
singleton, no async. Every test is a plain ``def`` (no ``async def``) and
runs without an event loop. The repo's ``pytest.ini`` declares
``testpaths = tests``; this file is collected automatically.

Conventions
-----------
* ``sys.path`` is bootstrapped so the test runs regardless of the cwd
  pytest was launched from (mirrors the bootstrap pattern in
  ``tests/test_capital_allocator.py``, ``tests/test_decision_ledger.py``).
* The env-var redirect block at module top is **defensive only** —
  ``ml/validation.py`` itself reads no env vars and imports no other
  project module (only ``numpy`` + ``sklearn`` + ``pydantic`` + stdlib
  ``logging``/``copy``/``time``). But the sibling test files in the same
  pytest session *do* read env vars at import time (``core.data_store``,
  ``risk.manager``, …), and the ``pytest.ini::testpaths = tests``
  discovery pattern means pytest imports the whole ``tests/`` package
  before running any single file. Setting the redirects here (with
  ``setdefault`` so an outer runner can override) keeps the file's
  *neighbours* hermetic even if a future test run happens to import this
  file alongside a stateful one.
* The constants ``DEFAULT_N_SPLITS``, ``DEFAULT_MIN_TRAIN_SIZE``,
  ``MAX_RAW_PREDICTIONS``, ``NEAR_DUP_ROUND_DP`` are imported from the
  module under test so the assertions stay in lock-step with the
  implementation (a future re-tune of the threshold moves the test
  automatically, rather than silently breaking it).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Defensive env-var redirect (see "Conventions" in the module docstring). ──
# ``setdefault`` lets an outer runner / sibling test file override these if it
# needs to; otherwise the tests stay hermetic to /tmp and cannot clobber any
# real persisted state in the repo's ``data/`` directory. The module under
# test reads NONE of these — the redirect exists purely so a co-collected
# sibling test file (e.g. test_risk_manager.py) doesn't see a missing /
# unwritable path during its own module-import-time work.
_TMP_ROOT = Path("/tmp/ml_validation_tests")
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
    # Force the canonical trading mode to paper + live disabled so any
    # co-collected stateful test doesn't trip a shadow / live-trading gate.
    "TRADING_MODE": "paper",
    "LIVE_TRADING_ENABLED": "false",
}
for _key, _val in _ENV_REDIRECTS.items():
    os.environ.setdefault(_key, _val)

# Make the polymarket-bot package root importable as top-level modules
# (``ml.*``) regardless of the cwd pytest was launched from. Mirrors the
# bootstrap pattern in tests/test_features.py / test_paper_simulator.py.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import numpy as np  # noqa: E402  (env must be set first)
import pytest  # noqa: E402  (env must be set first)

from sklearn.linear_model import LogisticRegression  # noqa: E402

from ml.validation import (  # noqa: E402
    DEFAULT_MIN_TRAIN_SIZE,
    DEFAULT_N_SPLITS,
    MAX_RAW_PREDICTIONS,
    NEAR_DUP_ROUND_DP,
    out_of_time_test,
    time_series_cv,
    validate_no_leakage,
)

# NOTE: no module-level ``pytestmark = pytest.mark.asyncio`` here — every
# test in this file is a plain synchronous ``def``. The validation module
# is pure-Python (no I/O, no awaits) so there is nothing for the asyncio
# event loop to schedule. Skipping the asyncio marker keeps pytest-asyncio
# collection cost off this file entirely.


# ── Helpers ────────────────────────────────────────────────────────────────
def _make_classifier() -> LogisticRegression:
    """Return a fast, deterministic sklearn classifier for CV / OOT tests.

    ``LogisticRegression`` is the fastest of the 4 whitelisted estimators
    and exposes ``predict_proba`` by default (so the validation module's
    primary ``_predict_proba`` code path is exercised, not the
    ``predict``-only fallback). ``random_state=42`` pins the solver to a
    deterministic outcome so the same (X, y) yields identical metrics
    across runs — critical for reproducible test assertions.
    """
    return LogisticRegression(max_iter=1000, random_state=42)


def _make_separable_dataset(
    n: int = 300,
    n_features: int = 6,
    seed: int = 0,
) -> tuple[list[list[float]], list[int]]:
    """Return a synthetic dataset where the labels are a deterministic
    function of the features (so the model can actually learn something
    and produce well-defined, non-degenerate metrics).

    Label rule: ``y = 1`` iff ``x[0] + 0.5 * x[1] > 0``. With a 6-feature
    standard-normal feature matrix and ~50 % base rate, both classes are
    present in every reasonable validation chunk, which is required for
    ``roc_auc_score`` to be defined (rather than degrade to ``None``).

    Returns plain Python lists (the loose-typed input contract documented
    in ``ml/validation.py`` — the functions accept ``list[list[float]]``
    and ``list[int]``, not just numpy arrays, so we exercise that path).
    """
    rng = np.random.RandomState(seed)
    X = rng.randn(n, n_features)
    y = (X[:, 0] + 0.5 * X[:, 1] > 0).astype(int)
    return X.tolist(), y.tolist()


# ── (1) time_series_cv returns per-fold Brier / AUC ──────────────────────
def test_time_series_cv_returns_per_fold_brier_and_auc():
    """``time_series_cv`` must return a ``per_fold`` list of dicts where
    each fold carries numeric ``brier`` and ``auc`` keys (the two
    headline metrics named in the task spec).

    Implementation detail: AUC degrades to ``None`` when only one class
    is present in the validation chunk, and Brier is undefined for empty
    validation chunks. The synthetic dataset here is class-balanced and
    large enough (n=300, val_size=66) that every fold contains both
    classes — so per-fold AUC and Brier are guaranteed numeric. We
    additionally pin the value ranges: Brier ∈ [0, 1] (it's a mean
    squared error of probabilities); AUC ∈ [0, 1] by construction.

    Belt-and-braces: the result envelope must carry the top-level keys
    documented in the module (``per_fold``, ``aggregate``, ``val_size``,
    ``n_splits_evaluated`` …) so a future refactor that reshapes the
    return value fails this test loudly rather than silently breaking
    callers that destructured it.
    """
    X, y = _make_separable_dataset(n=300, seed=0)
    model = _make_classifier()

    result = time_series_cv(
        model,
        X,
        y,
        n_splits=3,
        min_train_size=100,
    )

    # ── Top-level envelope ───────────────────────────────────────────────
    # These are the keys callers depend on; a missing key is a contract
    # break, not a silent quality regression.
    for key in (
        "method",
        "n_splits_requested",
        "n_splits_evaluated",
        "min_train_size",
        "val_size",
        "total_samples",
        "per_fold",
        "aggregate",
    ):
        assert key in result, (
            f"time_series_cv result must carry top-level key {key!r}; "
            f"got {sorted(result.keys())}"
        )

    # ── per_fold structure ──────────────────────────────────────────────
    assert isinstance(result["per_fold"], list)
    assert len(result["per_fold"]) == result["n_splits_evaluated"]
    assert result["n_splits_evaluated"] >= 1, (
        "must produce at least one fold when n >= min_train_size + 1"
    )
    # Sanity: with n=300, min_train_size=100, n_splits=3, val_size = (300-100)//3 = 66.
    # The 3 folds fit comfortably (last fold val_start = 100 + 2*66 = 232 < 300),
    # so n_splits_evaluated must equal the requested 3.
    assert result["n_splits_evaluated"] == 3
    assert result["val_size"] == 66

    # ── Per-fold Brier / AUC ────────────────────────────────────────────
    for fold in result["per_fold"]:
        # Required structural keys for diagnosing fold-by-fold quality.
        assert "fold" in fold
        assert "brier" in fold
        assert "auc" in fold
        assert "train_end_index" in fold
        assert "val_start_index" in fold
        assert "val_end_index" in fold

        # The synthetic dataset is class-balanced (≈50 % positive) and
        # val_size=66 is large enough that BOTH classes are present in
        # every validation chunk — so brier and auc must be defined
        # numeric floats, not None.
        assert fold["brier"] is not None, (
            f"fold {fold['fold']}: brier must be numeric for a class-balanced "
            f"validation chunk of size {fold['val_size']}, got None"
        )
        assert fold["auc"] is not None, (
            f"fold {fold['fold']}: auc must be numeric when both classes are "
            f"present in the validation chunk, got None"
        )
        assert isinstance(fold["brier"], float)
        assert isinstance(fold["auc"], float)
        # Brier ∈ [0, 1] (mean squared error of a probability).
        assert 0.0 <= fold["brier"] <= 1.0, (
            f"fold {fold['fold']}: brier {fold['brier']} outside [0, 1]"
        )
        # AUC ∈ [0, 1] by construction of roc_auc_score.
        assert 0.0 <= fold["auc"] <= 1.0, (
            f"fold {fold['fold']}: auc {fold['auc']} outside [0, 1]"
        )

    # ── Aggregate roll-up ───────────────────────────────────────────────
    # The aggregate dict must carry the mean of each metric across folds
    # plus the pooled OOS metric — these are the headline single-number
    # summaries the dashboard surfaces.
    assert "mean_brier" in result["aggregate"]
    assert "mean_auc" in result["aggregate"]
    assert "pooled" in result["aggregate"]
    assert result["aggregate"]["mean_brier"] is not None
    assert result["aggregate"]["mean_auc"] is not None
    # Pooled metric recomputes the suite over the concatenation of every
    # fold's OOS predictions — the single-number headline most resistant
    # to per-fold noise.
    assert result["aggregate"]["pooled"]["brier"] is not None
    assert result["aggregate"]["pooled"]["auc"] is not None


# ── (2) Train indices precede validation indices in each fold ────────────
def test_time_series_cv_train_indices_precede_validation_indices():
    """The walk-forward contract: for every fold, ALL training indices
    must precede ALL validation indices (no overlap, no gap, no future
    leakage into the training window).

    Concretely:
      * Within a fold: ``train_end_index == val_start_index`` — the
        training chunk ``[0, train_end)`` and the validation chunk
        ``[val_start, val_end)`` are adjacent and disjoint, so the
        maximum training index (``train_end - 1``) is strictly less than
        the minimum validation index (``val_start``).
      * Across folds: ``val_start_index`` is strictly monotonically
        increasing — each subsequent fold trains on strictly more data
        and validates on the *next* unseen chunk, never re-using a
        previously-validated chunk as training data (which would be a
        forward-leakage of evaluation signal).

    This test is the canonical "no look-ahead bias" assertion for
    walk-forward CV — the cardinal sin for time-series modelling is
    training on data that wasn't yet known at prediction time, and
    this exact-indices check catches that failure mode deterministically
    (rather than relying on a statistical proxy like AUC drift).
    """
    X, y = _make_separable_dataset(n=300, seed=1)
    model = _make_classifier()

    result = time_series_cv(
        model,
        X,
        y,
        n_splits=4,
        min_train_size=80,
    )

    assert result["n_splits_evaluated"] >= 2, (
        "test needs >= 2 folds to assert monotonic val_start progression"
    )

    prev_val_start = -1
    prev_val_end = -1
    for fold in result["per_fold"]:
        train_end = fold["train_end_index"]
        val_start = fold["val_start_index"]
        val_end = fold["val_end_index"]

        # ── Within-fold: train indices precede val indices ─────────────
        # train chunk = [0, train_end), val chunk = [val_start, val_end).
        # No overlap (train_end <= val_start) AND no gap (train_end ==
        # val_start) for the expanding-window variant — the model is
        # trained on every observation strictly prior to the validation
        # chunk, with no unused rows in between.
        assert train_end <= val_start, (
            f"fold {fold['fold']}: train_end_index ({train_end}) must be <= "
            f"val_start_index ({val_start}) — training and validation "
            f"chunks must not overlap"
        )
        # Belt-and-braces: the maximum training index (train_end - 1) is
        # strictly less than the minimum validation index (val_start).
        assert train_end - 1 < val_start, (
            f"fold {fold['fold']}: max train index ({train_end - 1}) must be "
            f"strictly less than min val index ({val_start})"
        )
        # Val chunk is non-empty (val_end > val_start).
        assert val_end > val_start, (
            f"fold {fold['fold']}: val chunk must be non-empty "
            f"(val_start={val_start}, val_end={val_end})"
        )
        # Val chunk must fit inside the dataset.
        assert val_end <= result["total_samples"], (
            f"fold {fold['fold']}: val_end_index ({val_end}) exceeds "
            f"total_samples ({result['total_samples']})"
        )
        # Train chunk must be >= the requested min_train_size (the
        # expanding window only grows; it never shrinks below the floor).
        assert train_end >= result["min_train_size"], (
            f"fold {fold['fold']}: train_end_index ({train_end}) must be >= "
            f"min_train_size ({result['min_train_size']})"
        )

        # ── Across folds: val_start strictly increasing ───────────────
        # Each subsequent fold validates on the NEXT unseen chunk — never
        # re-using a previously-validated chunk as training data, and never
        # re-validating a chunk that an earlier fold already evaluated.
        assert val_start > prev_val_start, (
            f"fold {fold['fold']}: val_start_index ({val_start}) must be "
            f"strictly greater than the previous fold's val_start_index "
            f"({prev_val_start}) — expanding-window walk-forward requires "
            f"monotonically increasing val_start across folds"
        )
        # Belt-and-braces: val_end is also monotonically non-decreasing
        # (the last fold's val_end may be clipped by total_samples, so we
        # allow equality here — the strictness is on val_start above).
        assert val_end >= prev_val_end, (
            f"fold {fold['fold']}: val_end_index ({val_end}) must be "
            f"non-decreasing across folds (previous was {prev_val_end})"
        )
        # And the next fold's training chunk must INCLUDE this fold's
        # validation chunk (expanding window — the val chunk becomes
        # training data for the next fold, not the other way round).
        if prev_val_end >= 0:
            # The current fold's train_end must be >= the previous fold's
            # val_end (i.e. the prior validation chunk is now in training).
            assert train_end >= prev_val_end, (
                f"fold {fold['fold']}: train_end_index ({train_end}) must "
                f"include the previous fold's validation chunk (prev val_end "
                f"= {prev_val_end}) — expanding-window requires the val "
                f"chunk to become training data in the next fold"
            )

        prev_val_start = val_start
        prev_val_end = val_end


# ── (3) out_of_time_test returns metrics ──────────────────────────────────
def test_out_of_time_test_returns_metrics():
    """``out_of_time_test`` must return a dict containing a ``metrics``
    block with the canonical binary-classification suite (Brier / AUC /
    log-loss / accuracy) plus split-size metadata (train_size /
    test_size / n_features / n_samples) and the raw per-row
    ``predictions`` / ``actuals`` arrays.

    The test split is temporally later than the train split (caller
    responsibility — the function does not re-sort). We split the
    synthetic dataset at index 200: train on [0:200], test on [200:300].
    Both halves contain both classes (~50 % positive base rate), so AUC
    is defined and numeric.

    Belt-and-braces: the ``predictions`` / ``actuals`` arrays must be
    parallel (same length), the probabilities must be in [0, 1], and the
    actuals must be in {0, 1} — these are the downstream contracts a
    calibration analysis (e.g. a reliability diagram) would rely on.
    """
    X, y = _make_separable_dataset(n=300, seed=2)
    split = 200
    X_train, y_train = X[:split], y[:split]
    X_test, y_test = X[split:], y[split:]

    model = _make_classifier()
    result = out_of_time_test(model, X_train, y_train, X_test, y_test)

    # ── Top-level envelope ─────────────────────────────────────────────
    for key in ("method", "metrics", "predictions", "actuals", "predictions_truncated"):
        assert key in result, (
            f"out_of_time_test result must carry top-level key {key!r}; "
            f"got {sorted(result.keys())}"
        )
    assert result["method"] == "out_of_time_holdout"

    # ── metrics block ──────────────────────────────────────────────────
    metrics = result["metrics"]
    # Canonical binary-classification suite — every key here is required
    # for downstream calibration / drift analysis.
    for key in (
        "brier",
        "auc",
        "log_loss",
        "accuracy",
        "n_samples",
        "mean_pred",
        "mean_actual",
        "train_size",
        "test_size",
        "n_features",
    ):
        assert key in metrics, (
            f"out_of_time_test metrics must carry key {key!r}; "
            f"got {sorted(metrics.keys())}"
        )

    # Split-size metadata must match the inputs.
    assert metrics["train_size"] == split
    assert metrics["test_size"] == 100
    assert metrics["n_features"] == 6
    assert metrics["n_samples"] == 100

    # The test split is class-balanced (≈50 % positive), so AUC is
    # defined and numeric — not None.
    assert metrics["brier"] is not None
    assert metrics["auc"] is not None
    assert isinstance(metrics["brier"], float)
    assert isinstance(metrics["auc"], float)
    assert 0.0 <= metrics["brier"] <= 1.0
    assert 0.0 <= metrics["auc"] <= 1.0
    # Accuracy ∈ [0, 1].
    assert metrics["accuracy"] is not None
    assert 0.0 <= metrics["accuracy"] <= 1.0
    # log-loss ≥ 0 (it's a KL divergence; 0 = perfect).
    assert metrics["log_loss"] is not None
    assert metrics["log_loss"] >= 0.0
    # mean_pred / mean_actual ∈ [0, 1] (probabilities / binary rates).
    assert 0.0 <= metrics["mean_pred"] <= 1.0
    assert 0.0 <= metrics["mean_actual"] <= 1.0

    # ── predictions / actuals ──────────────────────────────────────────
    predictions = result["predictions"]
    actuals = result["actuals"]
    assert isinstance(predictions, list)
    assert isinstance(actuals, list)
    # Parallel arrays (calibration analysis zips them).
    assert len(predictions) == len(actuals), (
        "predictions and actuals must be parallel arrays of equal length"
    )
    # Capped at MAX_RAW_PREDICTIONS for response tractability — here
    # test_size=100 < MAX_RAW_PREDICTIONS=1000, so no truncation.
    assert len(predictions) == 100
    assert result["predictions_truncated"] is False
    # Probabilities are in [0, 1] (the module clips defensively).
    for p in predictions:
        assert 0.0 <= p <= 1.0, (
            f"prediction {p} outside [0, 1] — module must clip probabilities"
        )
    # Actuals are binary {0, 1}.
    for a in actuals:
        assert a in (0, 1), f"actual label {a} not in {{0, 1}}"


def test_out_of_time_test_truncates_large_raw_predictions():
    """Belt-and-braces: when the test split exceeds ``MAX_RAW_PREDICTIONS``
    (1000), the raw ``predictions`` / ``actuals`` arrays are capped at
    1000 rows and ``predictions_truncated`` is set to ``True``.

    This is the response-tractability guard documented in the module —
    the aggregate metrics are STILL computed on the full test set; only
    the raw per-row arrays are sliced. This test pins that contract so a
    future regression that drops the cap (and emits a multi-MB JSON
    response on every OOT call) fails loudly.
    """
    # Large enough to trip the cap: 1200 test rows > MAX_RAW_PREDICTIONS=1000.
    X, y = _make_separable_dataset(n=1500, seed=3)
    X_train, y_train = X[:300], y[:300]
    X_test, y_test = X[300:], y[300:]

    model = _make_classifier()
    result = out_of_time_test(model, X_train, y_train, X_test, y_test)

    # Metrics reflect the FULL test set (1200 rows), not the truncated
    # raw arrays (1000 rows) — this is the load-bearing distinction.
    assert result["metrics"]["n_samples"] == 1200
    assert result["metrics"]["test_size"] == 1200
    # But the raw arrays are capped.
    assert len(result["predictions"]) == MAX_RAW_PREDICTIONS
    assert len(result["actuals"]) == MAX_RAW_PREDICTIONS
    assert result["predictions_truncated"] is True


# ── (4) validate_no_leakage flags exact-duplicate rows ──────────────────
def test_validate_no_leakage_flags_exact_duplicate_rows():
    """``validate_no_leakage`` must flag exact-duplicate feature vectors
    — rows whose byte-level representation is identical to a previously-
    seen row.

    Implementation contract (per ``ml/validation.py``):
      * ``stats.n_duplicate_rows > 0``
      * a warning containing the word ``duplicate`` appears in
        ``warnings`` (advisory — duplicates are suspicious only if they
        span a train/test boundary; the caller is expected to split
        BEFORE dedup, so the audit does not block on duplicates alone)
      * ``is_valid`` stays ``True`` (exact duplicates alone are NOT a
        blocking issue — only conflicting labels on near-duplicates are)

    Synthetic scenario: a 5-row dataset where row 1 is a byte-level
    copy of row 0. Both duplicates carry the same label (so the near-
    duplicate-conflict check does NOT fire — that's tested separately in
    test 5). We assert the duplicate is counted exactly once (the second
    occurrence of the row, not the first).
    """
    # 5 rows, 2 features. Row [1] is a byte-level copy of row [0].
    # Both carry label 0 (so the near-dup-conflict check does NOT fire
    # — duplicates with the same label are advisory, not blocking).
    X = [
        [1.0, 2.0],
        [1.0, 2.0],  # exact duplicate of row 0
        [3.0, 4.0],
        [5.0, 6.0],
        [7.0, 8.0],
    ]
    y = [0, 0, 1, 0, 1]

    result = validate_no_leakage(X, y)

    # ── Structural envelope ────────────────────────────────────────────
    for key in ("is_valid", "n_samples", "n_features", "issues", "warnings", "stats"):
        assert key in result, (
            f"validate_no_leakage result must carry key {key!r}; "
            f"got {sorted(result.keys())}"
        )
    assert result["n_samples"] == 5
    assert result["n_features"] == 2

    # ── The duplicate is counted ──────────────────────────────────────
    # Exactly one duplicate (row [1] is a copy of row [0]); the first
    # occurrence is the "canonical" copy and the second is the duplicate.
    assert result["stats"]["n_duplicate_rows"] == 1, (
        f"expected exactly 1 duplicate row, got "
        f"{result['stats']['n_duplicate_rows']}"
    )

    # ── A warning containing 'duplicate' is emitted ───────────────────
    dup_warnings = [w for w in result["warnings"] if "duplicate" in w.lower()]
    assert len(dup_warnings) >= 1, (
        f"expected a warning mentioning 'duplicate'; got warnings="
        f"{result['warnings']}"
    )
    # Belt-and-braces: the warning text calls out the train/test boundary
    # concern (the caller is expected to split BEFORE dedup so a row that
    # appears in both train and test is caught at split time, not masked
    # by an upstream dedup step).
    assert any("train/test" in w or "boundary" in w for w in dup_warnings), (
        f"duplicate warning should mention the train/test boundary concern; "
        f"got {dup_warnings}"
    )

    # ── Duplicates alone do NOT block ──────────────────────────────────
    # Exact duplicates with the SAME label are advisory, not blocking —
    # the strongest leakage signal (and the only blocking one for
    # duplicates) is near-duplicate features with CONFLICTING labels,
    # which is tested separately below. Here ``is_valid`` stays True.
    assert result["is_valid"] is True, (
        "exact-duplicate rows with the SAME label are advisory, not "
        "blocking — is_valid must remain True. (Blocking behaviour is "
        "reserved for near-duplicates with conflicting labels, tested "
        "in test_validate_no_leakage_flags_near_duplicate_conflicting_labels.)"
    )
    assert result["issues"] == [], (
        f"duplicates with matching labels must produce NO issues; got "
        f"{result['issues']}"
    )
    # And the near-duplicate conflict counter must be zero (no conflicting
    # labels among the duplicates).
    assert result["stats"]["n_near_dup_label_conflicts"] == 0


# ── (5) validate_no_leakage flags near-duplicates with conflicting labels ─
def test_validate_no_leakage_flags_near_duplicate_conflicting_labels():
    """``validate_no_leakage`` must flag near-duplicate feature vectors
    (rows that are identical when rounded to ``NEAR_DUP_ROUND_DP`` = 4
    decimal places) with CONFLICTING labels — the strongest leakage
    signal.

    Intuition: if two inputs are identical (to 4 dp) but produce
    different labels, hidden state (timestamp, ID, future-leaked
    feature) must be determining the outcome — the model cannot learn a
    consistent rule from these features alone, and any apparent skill on
    the training split is leakage that will evaporate out-of-sample.

    Implementation contract (per ``ml/validation.py``):
      * ``is_valid = False`` (blocking — this is the cardinal leakage
        signal)
      * an issue mentioning ``near-duplicate`` + ``CONFLICTING`` appears
        in ``issues``
      * ``stats.n_near_dup_label_conflicts > 0``

    Synthetic scenario: 5 rows where row [1] differs from row [0] only
    in the 6th decimal place (well below the 4 dp rounding threshold) —
    so the rounded-hash scan sees them as identical. Row [0] has label 0
    and row [1] has label 1 (conflict). The remaining 3 rows are
    distinct and serve as a baseline so the test isn't tripped by an
    empty-data edge case.
    """
    # Row [1] differs from row [0] only in the 6th decimal place —
    # rounds to the same 4 dp key, so the near-duplicate scan sees them
    # as identical features. Different labels (0 vs 1) → conflict.
    X = [
        [1.000010, 2.000010],
        [1.000020, 2.000020],  # near-dup of row 0 (4 dp), conflicting label
        [3.0, 4.0],
        [5.0, 6.0],
        [7.0, 8.0],
    ]
    y = [0, 1, 1, 0, 1]

    result = validate_no_leakage(X, y)

    # ── The conflict is counted ────────────────────────────────────────
    assert result["stats"]["n_near_dup_label_conflicts"] >= 1, (
        f"expected >= 1 near-duplicate label conflict; got "
        f"{result['stats']['n_near_dup_label_conflicts']}"
    )

    # ── A blocking issue is emitted ───────────────────────────────────
    assert len(result["issues"]) >= 1, (
        "near-duplicate conflicting labels must produce at least one "
        "blocking issue"
    )
    conflict_issues = [
        i for i in result["issues"]
        if "near-duplicate" in i.lower() and "conflict" in i.lower()
    ]
    assert len(conflict_issues) >= 1, (
        f"expected an issue mentioning 'near-duplicate' + 'conflict'; "
        f"got issues={result['issues']}"
    )

    # ── is_valid is False (blocking) ──────────────────────────────────
    # This is the cardinal leakage signal — the audit MUST block.
    assert result["is_valid"] is False, (
        "near-duplicate features with conflicting labels must set "
        "is_valid=False (the strongest leakage signal — blocking). "
        f"Got is_valid={result['is_valid']}, issues={result['issues']}"
    )

    # Belt-and-braces: the conflict count is exactly 1 (only row [1]
    # conflicts with row [0] — the other 3 rows are distinct).
    assert result["stats"]["n_near_dup_label_conflicts"] == 1, (
        f"expected exactly 1 near-duplicate conflict (only row [1] "
        f"conflicts with row [0]); got "
        f"{result['stats']['n_near_dup_label_conflicts']}"
    )

    # And the rounding precision used by the scan is the documented
    # constant (NEAR_DUP_ROUND_DP = 4) — a future re-tune that changed
    # the precision would silently change which conflicts are caught.
    # The issue text mentions the precision so callers can interpret
    # the flag correctly.
    assert any(str(NEAR_DUP_ROUND_DP) in i for i in conflict_issues), (
        f"conflict issue should mention the rounding precision "
        f"({NEAR_DUP_ROUND_DP} dp); got {conflict_issues}"
    )


# ── (6) validate_no_leakage passes on clean data ─────────────────────────
def test_validate_no_leakage_passes_on_clean_data():
    """``validate_no_leakage`` must return ``is_valid = True`` with NO
    blocking issues and NO advisory warnings on a clean synthetic
    dataset:
      * distinct feature rows (no exact duplicates)
      * binary labels in ``{0, 1}`` only
      * balanced classes (no severe imbalance warning)
      * no NaN / Inf values
      * no near-duplicate-with-conflicting-labels

    This is the "happy path" baseline — a regression that flipped
    ``is_valid`` to ``False`` on clean data (e.g. by tightening the
    near-duplicate heuristic to flag any matching rounded row regardless
    of label conflict) would break every downstream training run, since
    the leakage audit is the gate the training pipeline checks before
    fitting a model.
    """
    # Distinct, well-separated rows. Labels alternate so both classes
    # are present and ~balanced (3 zeros, 2 ones — balance_ratio = 2/3,
    # well above the 0.1 severe-imbalance threshold).
    X = [
        [1.0, 2.0],
        [3.0, 4.0],
        [5.0, 6.0],
        [7.0, 8.0],
        [9.0, 10.0],
    ]
    y = [0, 1, 0, 1, 0]

    result = validate_no_leakage(X, y)

    # ── Happy-path contract ────────────────────────────────────────────
    assert result["is_valid"] is True, (
        f"clean data must pass the leakage audit (is_valid=True). "
        f"issues={result['issues']}, warnings={result['warnings']}"
    )
    assert result["issues"] == [], (
        f"clean data must produce NO blocking issues; got {result['issues']}"
    )
    assert result["warnings"] == [], (
        f"clean data must produce NO advisory warnings; got "
        f"{result['warnings']}"
    )

    # ── Structural envelope ───────────────────────────────────────────
    assert result["n_samples"] == 5
    assert result["n_features"] == 2

    # ── Stats: clean across every dimension ───────────────────────────
    stats = result["stats"]
    assert stats["n_nan"] == 0
    assert stats["n_inf"] == 0
    assert stats["n_duplicate_rows"] == 0
    assert stats["n_near_dup_label_conflicts"] == 0
    # Label distribution is binary {0, 1} with counts {0: 3, 1: 2}.
    assert stats["label_distribution"] == {"0": 3, "1": 2}
    # Balance ratio = min(2, 3) / max(2, 3) = 2/3 ≈ 0.667 — well above
    # the 0.1 severe-imbalance threshold, so no imbalance warning.
    assert stats["label_balance_ratio"] is not None
    assert stats["label_balance_ratio"] > 0.1
    # No per-feature NaN counts (no NaNs in the matrix).
    assert stats["per_feature_nan_counts"] == {}


# ── Sanity: defaults are exported and match the documented contract ──────
def test_documented_defaults_are_exported():
    """Belt-and-braces: the constants the tests above depend on
    (``DEFAULT_N_SPLITS``, ``DEFAULT_MIN_TRAIN_SIZE``,
    ``MAX_RAW_PREDICTIONS``, ``NEAR_DUP_ROUND_DP``) must be importable
    from ``ml.validation``. A future refactor that renamed or dropped
    one of these would silently break the tests above; this explicit
    import-check fails loudly on rename.
    """
    assert DEFAULT_N_SPLITS == 5
    assert DEFAULT_MIN_TRAIN_SIZE == 200
    assert MAX_RAW_PREDICTIONS == 1_000
    assert NEAR_DUP_ROUND_DP == 4
