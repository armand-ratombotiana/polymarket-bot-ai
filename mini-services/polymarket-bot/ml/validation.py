"""
ml/validation.py — Time-Series Walk-Forward Cross-Validation & Leakage Auditing.

Three independent validation primitives for the ML prediction pipeline, plus a
FastAPI route that exposes them over HTTP.

  1. ``time_series_cv(model, X, y, n_splits=5, min_train_size=200)``
     Expanding-window walk-forward CV. Fold ``k`` trains on ``[0 : t_k]`` and
     validates on the next chunk ``[t_k : t_{k+1}]`` — i.e. the model is *always*
     trained on strictly-prior observations and evaluated on the immediately
     following chunk. Each fold retrains a fresh clone of the input model so
     no fold's training state leaks into another fold's validation, and no
     future information ever enters a training set (the cardinal sin for
     time-series data). Returns per-fold + aggregate metrics (Brier, ROC-AUC,
     log-loss, accuracy) plus a pooled out-of-sample metric across all folds.

     When ``n`` is just above ``min_train_size`` the validation chunk
     degenerates to a single sample (``val_size = 1``) — exactly the
     ``train on [0:t], validate on [t:t+1]`` literal walk-forward described in
     the task spec. When ``n`` is larger, ``val_size`` grows to
     ``(n - min_train_size) // n_splits`` so each fold gets a statistically
     meaningful validation chunk.

  2. ``out_of_time_test(model, X_train, y_train, X_test, y_test)``
     Temporal holdout: fit on the train split, evaluate on a temporally-later
     test split. The test split must come from a *later* time period than the
     train split — the caller is responsible for ordering (this module does
     not re-sort). Returns the same metric suite + the raw prediction
     probabilities (capped at 1000 rows) for downstream calibration analysis.

  3. ``validate_no_leakage(features, labels)``
     Static data-quality audit that flags common leakage / quality signals
     *before* a model is trained:
       - shape & length contract (features ↔ labels)
       - NaN / Inf scan (per-matrix + per-feature)
       - exact-duplicate feature vectors (suspicious if duplicates span a
         train/test boundary — the caller should split *before* dedup)
       - label-domain check (binary {0, 1} expected for this classifier)
       - label-balance ratio (warns on severe imbalance)
       - near-duplicate features with *conflicting* labels (the strongest
         leakage signal — identical inputs producing different outputs means
         hidden state is leaking through the features)
     Returns ``{is_valid, issues, warnings, stats}``. ``issues`` are blocking
     (``is_valid = False`` if any); ``warnings`` are advisory.

Model contract
--------------
The validation functions accept any sklearn-style classifier:
  - ``model.fit(X, y)`` trains it
  - ``model.predict_proba(X)`` returns a 2-D array; column ``[:, 1]`` is taken
    as the positive-class probability
  - models with only ``predict(X)`` (e.g. a regressor returning probabilities)
    are handled via a graceful fallback

Each fold calls ``sklearn.base.clone(model)`` to get a fresh unfitted copy
(works for any sklearn estimator). If clone fails (non-sklearn model), the
function falls back to ``copy.deepcopy``; if that also fails, the original
model is reused across folds and a warning is logged (state will leak between
folds in that case — documented behaviour, not a silent failure).

HTTP layer
----------
``api/server.py`` calls ``register_routes(app)`` at startup to expose:

    POST /api/ml/validate
        JSON body (``ValidationRequest``):
            X                 : list[list[float]]  — feature matrix
            y                 : list[int]           — binary labels {0, 1}
            X_test?           : list[list[float]]   — required for validation_type='oot'
            y_test?           : list[int]           — required for validation_type='oot'
            validation_type   : 'cv' | 'oot' | 'both'   (default 'cv')
            n_splits          : int (1..50, default 5)
            min_train_size    : int (>=10, default 200)
            model_class       : str — one of
                                 {GradientBoostingClassifier (default),
                                  RandomForestClassifier,
                                  LogisticRegression,
                                  SGDClassifier}
            model_params?     : dict — kwargs passed to the model constructor
            run_leakage_check?: bool (default True) — also run
                                 validate_no_leakage and append the result
        Returns: the CV and/or OOT result dict(s) + the leakage audit.
        Auth-protected by the caller's existing fail-closed bearer middleware
        (every route except /api/health requires ``Authorization: Bearer
        <API_TOKEN>``).
"""
from __future__ import annotations

import copy
import logging
import time
from typing import Any

import numpy as np
from pydantic import BaseModel, Field
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import LogisticRegression, SGDClassifier
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

log = logging.getLogger(__name__)

# ── Defaults & guards ───────────────────────────────────────────────────────
DEFAULT_MODEL_CLASS = "GradientBoostingClassifier"
DEFAULT_N_SPLITS = 5
DEFAULT_MIN_TRAIN_SIZE = 200
# Hard payload cap so a malicious / runaway caller cannot OOM the API by
# POSTing a multi-GB feature matrix. 50k rows × 38 features ≈ 15 MB JSON —
# well within FastAPI's default body limits but bounded.
MAX_PAYLOAD_ROWS = 50_000
# Cap on the number of raw predictions returned by out_of_time_test / the API
# so the response stays tractable. The aggregate metrics are computed on the
# full test set; only the raw ``predictions`` / ``actuals`` arrays are sliced.
MAX_RAW_PREDICTIONS = 1_000
# Nearest-duplicate conflict scan is O(n) via a hash but allocates one entry
# per unique rounded row — skip it above this threshold to bound memory.
NEAR_DUP_SCAN_ROW_LIMIT = 10_000
# Rounding precision for the near-duplicate heuristic. 4 dp matches the
# feature-pipeline's effective float32 precision (extract_features emits
# float32 → ~7 significant digits → 4 dp after the decimal is conservative).
NEAR_DUP_ROUND_DP = 4

# Whitelist of sklearn classifiers the public API may instantiate. Kept tight
# so a caller cannot construct arbitrary classes via the request body — only
# the four estimators the production ensemble is built from are permitted.
_MODEL_WHITELIST: dict[str, type] = {
    "GradientBoostingClassifier": GradientBoostingClassifier,
    "RandomForestClassifier": RandomForestClassifier,
    "LogisticRegression": LogisticRegression,
    "SGDClassifier": SGDClassifier,
}

# Random seed applied to every whitelisted estimator that accepts one — makes
# CV results reproducible across runs for the same (X, y) + model_class.
_SEED = 42


# ── Pydantic request schema (module-level so FastAPI's get_type_hints can
#    resolve the ``req: ValidationRequest`` annotation when the route is
#    registered inside register_routes). Mirrors the api/server.py convention
#    of module-level BaseModel declarations for request bodies.
class ValidationRequest(BaseModel):
    """Body schema for ``POST /api/ml/validate``."""

    X: list[list[float]] = Field(
        ...,
        description="Feature matrix (n_samples × n_features). Must be 2-D.",
    )
    y: list[int] = Field(
        ...,
        description="Binary labels in {0, 1}; same length as X.",
    )
    X_test: list[list[float]] | None = Field(
        None,
        description=(
            "Out-of-time test features — required when validation_type is "
            "'oot' or 'both'. Must have the same feature dimensionality as X."
        ),
    )
    y_test: list[int] | None = Field(
        None,
        description="Out-of-time test labels — required with X_test.",
    )
    validation_type: str = Field(
        "cv",
        description="One of 'cv' (walk-forward), 'oot' (out-of-time), or 'both'.",
    )
    n_splits: int = Field(
        DEFAULT_N_SPLITS,
        ge=1,
        le=50,
        description="Number of walk-forward folds (validation_type='cv' / 'both').",
    )
    min_train_size: int = Field(
        DEFAULT_MIN_TRAIN_SIZE,
        ge=10,
        description="Minimum training-window size for the first walk-forward fold.",
    )
    model_class: str | None = Field(
        None,
        description=(
            f"sklearn class to validate. One of {sorted(_MODEL_WHITELIST)}. "
            f"Defaults to {DEFAULT_MODEL_CLASS} (mirrors the production ensemble)."
        ),
    )
    model_params: dict[str, Any] | None = Field(
        None,
        description="Optional kwargs passed to the model constructor (e.g. learning_rate).",
    )
    run_leakage_check: bool = Field(
        True,
        description="Also run validate_no_leakage(features, labels) and append the result.",
    )


# ── Internal helpers ────────────────────────────────────────────────────────


def _fresh_model(model: Any) -> Any:
    """Return a fresh, unfitted copy of ``model`` for this CV fold.

    Tries ``sklearn.base.clone`` (works for any sklearn estimator — resets
    fitted state without copying data). If clone fails (non-sklearn model or
    a custom estimator without ``get_params``), falls back to
    ``copy.deepcopy``. If both fail (e.g. model contains an unpicklable
    handle), returns the original model — in which case state WILL leak
    between folds (logged at WARNING so it is never a silent failure).
    """
    try:
        return clone(model)
    except Exception:
        pass
    try:
        return copy.deepcopy(model)
    except Exception:
        log.warning(
            "[validation] cannot clone or deepcopy model %r — reusing the "
            "same instance across folds (fold state will leak between folds)",
            type(model).__name__,
        )
        return model


def _predict_proba(model: Any, X: np.ndarray) -> np.ndarray:
    """Return the positive-class probability vector for ``X``.

    Prefers ``model.predict_proba`` (sklearn API); takes column ``[:, 1]``
    for 2-D outputs. Falls back to ``model.predict`` for regressor-style
    estimators that emit probabilities directly. Raises ``TypeError`` if
    neither method exists.
    """
    if hasattr(model, "predict_proba"):
        proba = model.predict_proba(X)
        arr = np.asarray(proba, dtype=np.float64)
        if arr.ndim == 2 and arr.shape[1] >= 2:
            return arr[:, 1]
        return arr.ravel()
    if hasattr(model, "predict"):
        return np.asarray(model.predict(X), dtype=np.float64).ravel()
    raise TypeError(
        f"Model {type(model).__name__} exposes neither predict_proba nor predict"
    )


def _classification_metrics(y_true: np.ndarray, y_prob: np.ndarray) -> dict[str, Any]:
    """Compute the canonical binary-classification metric suite.

    All metrics degrade gracefully to ``None`` when undefined for the given
    (y_true, y_prob) — e.g. AUC is ``None`` when only one class is present in
    ``y_true``. Never raises.
    """
    y_true = np.asarray(y_true, dtype=np.int64).ravel()
    y_prob = np.asarray(y_prob, dtype=np.float64).ravel()
    n = int(y_true.shape[0])
    out: dict[str, Any] = {
        "n_samples": n,
        "mean_pred": round(float(np.mean(y_prob)), 6) if n else 0.0,
        "mean_actual": round(float(np.mean(y_true)), 6) if n else 0.0,
    }
    if n == 0:
        out.update({"brier": None, "auc": None, "log_loss": None, "accuracy": None})
        return out
    # Brier is defined for any binary y + probs in [0, 1].
    try:
        out["brier"] = round(float(brier_score_loss(y_true, y_prob)), 6)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("[validation] brier_score_loss failed: %s", e)
        out["brier"] = None
    # AUC requires both classes present.
    if len(np.unique(y_true)) >= 2:
        try:
            out["auc"] = round(float(roc_auc_score(y_true, y_prob)), 6)
        except Exception as e:  # pragma: no cover — defensive
            log.debug("[validation] roc_auc_score failed: %s", e)
            out["auc"] = None
    else:
        out["auc"] = None
    # log_loss needs the prob clipped away from {0, 1} to avoid log(0).
    try:
        p_clip = np.clip(y_prob, 1e-10, 1.0 - 1e-10)
        out["log_loss"] = round(float(log_loss(y_true, p_clip, labels=[0, 1])), 6)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("[validation] log_loss failed: %s", e)
        out["log_loss"] = None
    # Accuracy at the canonical 0.5 threshold.
    try:
        y_pred = (y_prob >= 0.5).astype(np.int64)
        out["accuracy"] = round(float(accuracy_score(y_true, y_pred)), 6)
    except Exception as e:  # pragma: no cover — defensive
        log.debug("[validation] accuracy_score failed: %s", e)
        out["accuracy"] = None
    return out


def _aggregate_metrics(
    per_fold: list[dict[str, Any]],
    pooled_y_true: np.ndarray,
    pooled_y_prob: np.ndarray,
) -> dict[str, Any]:
    """Roll up per-fold metrics into mean/std summaries + a pooled OOS metric.

    The pooled metric concatenates every fold's out-of-sample predictions
    and recomputes the suite once — this is the single-number headline metric
    that's most resistant to per-fold noise (a fold with 1 sample has a
    degenerate per-fold AUC; the pooled AUC over all folds is meaningful).
    """
    briers = [f["brier"] for f in per_fold if f.get("brier") is not None]
    aucs = [f["auc"] for f in per_fold if f.get("auc") is not None]
    lls = [f["log_loss"] for f in per_fold if f.get("log_loss") is not None]
    accs = [f["accuracy"] for f in per_fold if f.get("accuracy") is not None]

    pooled = (
        _classification_metrics(pooled_y_true, pooled_y_prob)
        if pooled_y_true.size
        else {}
    )

    def _mean(vals: list[float]) -> float | None:
        return round(float(np.mean(vals)), 6) if vals else None

    def _std(vals: list[float]) -> float | None:
        return round(float(np.std(vals, ddof=0)), 6) if vals else None

    return {
        "n_folds_evaluated": len(per_fold),
        "mean_brier": _mean(briers),
        "std_brier": _std(briers),
        "mean_auc": _mean(aucs),
        "std_auc": _std(aucs),
        "mean_log_loss": _mean(lls),
        "mean_accuracy": _mean(accs),
        "total_train_samples": int(sum(f["train_size"] for f in per_fold)),
        "total_val_samples": int(sum(f["val_size"] for f in per_fold)),
        "pooled": pooled,
    }


def _build_model(cls_name: str, params: dict[str, Any]) -> Any:
    """Instantiate a whitelisted sklearn classifier with sensible defaults.

    The defaults ensure every model exposes ``predict_proba`` and is
    deterministic (``random_state=42``) so the same (X, y) + model_class
    yields identical metrics across runs — critical for reproducible
    validation reports.
    """
    if cls_name not in _MODEL_WHITELIST:
        raise ValueError(
            f"model_class {cls_name!r} not in whitelist: {sorted(_MODEL_WHITELIST)}"
        )
    cls = _MODEL_WHITELIST[cls_name]
    p = dict(params or {})
    # Per-class defaults — only set if the caller didn't override.
    if cls_name == "SGDClassifier":
        # log_loss → has predict_proba; default loss ('hinge') does not.
        p.setdefault("loss", "log_loss")
        p.setdefault("eta0", 0.01)
        p.setdefault("max_iter", 5)
        p.setdefault("tol", 1e-3)
        p.setdefault("random_state", _SEED)
    elif cls_name == "LogisticRegression":
        p.setdefault("max_iter", 1000)
        p.setdefault("random_state", _SEED)
    elif cls_name in ("GradientBoostingClassifier", "RandomForestClassifier"):
        p.setdefault("random_state", _SEED)
    return cls(**p)


# ── Public API ──────────────────────────────────────────────────────────────


def time_series_cv(
    model: Any,
    X: Any,
    y: Any,
    n_splits: int = DEFAULT_N_SPLITS,
    min_train_size: int = DEFAULT_MIN_TRAIN_SIZE,
) -> dict[str, Any]:
    """Expanding-window walk-forward cross-validation.

    Fold ``k`` (0-indexed) trains on ``X[0 : t_k]`` (``t_k = min_train_size +
    k * val_size``) and validates on ``X[t_k : t_k + val_size]``. Each fold
    retrains a fresh clone of ``model`` so no fold's fitted state leaks into
    another fold's evaluation. ``val_size = max(1, (n - min_train_size) //
    n_splits)`` — when ``n`` is close to ``min_train_size`` this degenerates
    to a single-sample validation fold (the literal ``[t:t+1]`` walk-forward).

    Args:
        model: any sklearn-style classifier (``.fit`` + ``.predict_proba``).
        X: 2-D feature matrix (list-of-lists or ndarray).
        y: 1-D integer label vector in {0, 1}.
        n_splits: number of walk-forward folds (>= 1).
        min_train_size: minimum training-window size for fold 0 (>= 1).

    Returns:
        ``{n_splits, min_train_size, total_samples, val_size, per_fold, aggregate}``.
        ``per_fold`` is a list of per-fold metric dicts; ``aggregate`` is the
        mean/std roll-up + a pooled out-of-sample metric across all folds.

    Raises:
        ValueError: if ``X``/``y`` shapes mismatch or there isn't enough data
            for the requested ``n_splits`` + ``min_train_size``.
    """
    X_arr = np.asarray(X, dtype=np.float64)
    y_arr = np.asarray(y, dtype=np.int64).ravel()
    if X_arr.ndim != 2:
        raise ValueError(f"X must be 2-D, got shape {X_arr.shape}")
    n = int(X_arr.shape[0])
    if y_arr.shape[0] != n:
        raise ValueError(
            f"X/y length mismatch: X has {n} rows, y has {y_arr.shape[0]}"
        )
    n_splits = max(1, int(n_splits))
    min_train_size = max(1, int(min_train_size))
    # Need at least min_train_size + 1 sample to do a single fold (train on
    # min_train_size, validate on >= 1). Each additional fold needs val_size
    # more samples.
    if n < min_train_size + 1:
        raise ValueError(
            f"need at least min_train_size + 1 = {min_train_size + 1} samples "
            f"for one walk-forward fold, got {n}"
        )
    val_size = max(1, (n - min_train_size) // n_splits)

    per_fold: list[dict[str, Any]] = []
    pooled_y_true: list[np.ndarray] = []
    pooled_y_prob: list[np.ndarray] = []

    for k in range(n_splits):
        train_end = min_train_size + k * val_size
        val_start = train_end
        if val_start >= n:
            # Ran out of data — fewer folds than requested.
            log.debug(
                "[validation] walk-forward fold %d skipped: val_start=%d >= n=%d",
                k, val_start, n,
            )
            break
        val_end = min(val_start + val_size, n)
        X_tr, y_tr = X_arr[:train_end], y_arr[:train_end]
        X_va, y_va = X_arr[val_start:val_end], y_arr[val_start:val_end]

        fold_model = _fresh_model(model)
        fold_model.fit(X_tr, y_tr)
        y_prob = _predict_proba(fold_model, X_va)
        m = _classification_metrics(y_va, y_prob)
        m.update({
            "fold": k,
            "train_size": int(train_end),
            "val_size": int(val_end - val_start),
            "train_end_index": int(train_end),
            "val_start_index": int(val_start),
            "val_end_index": int(val_end),
        })
        per_fold.append(m)
        pooled_y_true.append(np.asarray(y_va, dtype=np.int64))
        pooled_y_prob.append(np.asarray(y_prob, dtype=np.float64))

    if not per_fold:
        raise ValueError(
            f"no walk-forward folds could be produced from n={n} samples "
            f"with min_train_size={min_train_size}"
        )

    y_concat = (
        np.concatenate(pooled_y_true) if pooled_y_true else np.array([], dtype=np.int64)
    )
    p_concat = (
        np.concatenate(pooled_y_prob) if pooled_y_prob else np.array([], dtype=np.float64)
    )
    aggregate = _aggregate_metrics(per_fold, y_concat, p_concat)

    return {
        "method": "walk_forward_expanding_window",
        "n_splits_requested": n_splits,
        "n_splits_evaluated": len(per_fold),
        "min_train_size": min_train_size,
        "val_size": val_size,
        "total_samples": n,
        "per_fold": per_fold,
        "aggregate": aggregate,
    }


def out_of_time_test(
    model: Any,
    X_train: Any,
    y_train: Any,
    X_test: Any,
    y_test: Any,
) -> dict[str, Any]:
    """Train on ``(X_train, y_train)``, evaluate on a temporally-later test set.

    Out-of-time validation: the test split must come from a *later* time
    period than the train split. This module does not re-sort — the caller
    is responsible for temporal ordering. The test split is never shuffled
    or mixed into training.

    Args:
        model: sklearn-style classifier (``.fit`` + ``.predict_proba``).
        X_train, y_train: training split (2-D features + 1-D labels).
        X_test, y_test: temporally-later test split.

    Returns:
        ``{metrics, predictions, actuals}`` where ``metrics`` is the
        classification-metric suite (Brier / AUC / log-loss / accuracy /
        n_samples / mean_pred / mean_actual + split sizes) and
        ``predictions`` / ``actuals`` are the raw per-row probabilities /
        labels capped at ``MAX_RAW_PREDICTIONS`` rows for response tractability.
    """
    X_tr = np.asarray(X_train, dtype=np.float64)
    y_tr = np.asarray(y_train, dtype=np.int64).ravel()
    X_te = np.asarray(X_test, dtype=np.float64)
    y_te = np.asarray(y_test, dtype=np.int64).ravel()
    if X_tr.ndim != 2 or X_te.ndim != 2:
        raise ValueError(
            f"X_train and X_test must be 2-D; got {X_tr.shape} and {X_te.shape}"
        )
    if X_tr.shape[0] != y_tr.shape[0]:
        raise ValueError(
            f"train length mismatch: X={X_tr.shape[0]} y={y_tr.shape[0]}"
        )
    if X_te.shape[0] != y_te.shape[0]:
        raise ValueError(
            f"test length mismatch: X={X_te.shape[0]} y={y_te.shape[0]}"
        )
    if X_tr.shape[1] != X_te.shape[1]:
        raise ValueError(
            f"feature-dim mismatch: train={X_tr.shape[1]} test={X_te.shape[1]}"
        )
    fresh = _fresh_model(model)
    fresh.fit(X_tr, y_tr)
    y_prob = _predict_proba(fresh, X_te)
    metrics = _classification_metrics(y_te, y_prob)
    metrics.update({
        "train_size": int(X_tr.shape[0]),
        "test_size": int(X_te.shape[0]),
        "n_features": int(X_tr.shape[1]),
    })
    return {
        "method": "out_of_time_holdout",
        "metrics": metrics,
        # Clip probabilities to [0, 1] defensively (some sklearn paths can
        # emit tiny negatives / >1 under numerical edge cases) before exposing.
        "predictions": np.clip(y_prob, 0.0, 1.0).tolist()[:MAX_RAW_PREDICTIONS],
        "actuals": y_te.tolist()[:MAX_RAW_PREDICTIONS],
        "predictions_truncated": bool(len(y_prob) > MAX_RAW_PREDICTIONS),
    }


def validate_no_leakage(features: Any, labels: Any) -> dict[str, Any]:
    """Audit ``(features, labels)`` for common data-leakage / quality issues.

    Static checks (no model is trained):
      - shape & length contract (features ↔ labels)
      - NaN / Inf scan (whole-matrix + per-feature counts)
      - exact-duplicate feature vectors (suspicious if duplicates span a
        train/test boundary — the caller should split *before* dedup)
      - label-domain check (binary {0, 1} expected for this classifier)
      - label-balance ratio (warns on severe imbalance < 0.1)
      - near-duplicate features with *conflicting* labels — the strongest
        leakage signal: identical inputs producing different outputs means
        hidden state is leaking through the features. Uses a rounded-hash
        heuristic (O(n), skips for n > ``NEAR_DUP_SCAN_ROW_LIMIT``).

    Returns:
        ``{is_valid, n_samples, n_features, issues, warnings, stats}``.
        ``issues`` are blocking (``is_valid = False`` if non-empty);
        ``warnings`` are advisory.
    """
    issues: list[str] = []
    warnings: list[str] = []

    X = np.asarray(features, dtype=np.float64)
    y = np.asarray(labels)

    # ── Shape contract ──────────────────────────────────────────────────
    if X.ndim != 2:
        issues.append(f"features must be 2-D, got shape {X.shape}")
        # Best-effort reshape so the rest of the function can run.
        X = (
            X.reshape(-1, 1)
            if X.size
            else np.zeros((0, 1), dtype=np.float64)
        )
    n, d = int(X.shape[0]), int(X.shape[1])

    if y.ndim != 1:
        y = y.ravel()
    if y.shape[0] != n:
        issues.append(
            f"length mismatch: features has {n} rows, labels has {y.shape[0]}"
        )

    # ── NaN / Inf scan ───────────────────────────────────────────────────
    n_nan = int(np.isnan(X).sum()) if n else 0
    n_inf = int(np.isinf(X).sum()) if n else 0
    if n_nan:
        warnings.append(f"features contain {n_nan} NaN values — will be treated as 0.0 by most sklearn estimators")
    if n_inf:
        warnings.append(f"features contain {n_inf} Inf values — will break most sklearn estimators")

    per_feature_nan: dict[str, int] = {}
    if n and d and n_nan:
        col_nan = np.isnan(X).sum(axis=0)
        for idx in np.where(col_nan > 0)[0]:
            per_feature_nan[str(int(idx))] = int(col_nan[idx])

    # ── Label domain ─────────────────────────────────────────────────────
    unique_labels = sorted({int(v) for v in y.tolist()}) if n else []
    if unique_labels and not set(unique_labels).issubset({0, 1}):
        issues.append(
            f"labels must be binary {{0, 1}} for this classifier; "
            f"got unique={unique_labels[:10]}"
        )

    # ── Exact-duplicate feature vectors ─────────────────────────────────
    n_dup = 0
    if n > 0:
        seen: set[bytes] = set()
        for row in X:
            key = row.tobytes()
            if key in seen:
                n_dup += 1
            else:
                seen.add(key)
    if n_dup:
        ratio = n_dup / n if n else 0.0
        warnings.append(
            f"{n_dup} duplicate feature vectors ({ratio:.2%} of {n}) — "
            f"possible leakage if duplicates span a train/test boundary "
            f"(split BEFORE dedup to be safe)"
        )

    # ── Label balance ────────────────────────────────────────────────────
    label_dist: dict[str, int] = {}
    if n:
        values, counts = np.unique(y, return_counts=True)
        label_dist = {str(int(v)): int(c) for v, c in zip(values, counts)}
    balance_ratio: float | None = None
    if len(label_dist) >= 2:
        counts = list(label_dist.values())
        balance_ratio = min(counts) / max(counts)
        if balance_ratio < 0.1:
            warnings.append(
                f"severe label imbalance: balance_ratio={balance_ratio:.3f} "
                f"({label_dist}) — minority class may be under-learned"
            )
    elif len(label_dist) == 1:
        warnings.append(f"only one label class present: {label_dist} — AUC / log-loss undefined")

    # ── Near-duplicate features with conflicting labels ─────────────────
    # The strongest leakage signal: identical (to 4dp) feature vectors
    # producing different labels. Genuine identical inputs should always map
    # to the same label — a conflict means hidden state (timestamp, ID,
    # future leakage) is determining the outcome.
    near_dup_conflicts = 0
    if 0 < n <= NEAR_DUP_SCAN_ROW_LIMIT and d > 0:
        rounded = np.round(X, NEAR_DUP_ROUND_DP)
        first_label_for_row: dict[bytes, int] = {}
        for i in range(n):
            key = rounded[i].tobytes()
            if key in first_label_for_row:
                if int(y[i]) != first_label_for_row[key]:
                    near_dup_conflicts += 1
            else:
                first_label_for_row[key] = int(y[i])
        if near_dup_conflicts:
            issues.append(
                f"{near_dup_conflicts} near-duplicate feature vectors (rounded "
                f"to {NEAR_DUP_ROUND_DP}dp) with CONFLICTING labels — strongest "
                f"leakage signal: identical inputs producing different outputs "
                f"means hidden state is leaking through the features"
            )
    elif n > NEAR_DUP_SCAN_ROW_LIMIT:
        warnings.append(
            f"near-duplicate conflict scan skipped: n={n} > limit "
            f"{NEAR_DUP_SCAN_ROW_LIMIT} (O(n) hash scan; re-run on a sample "
            f"to check for leakage signals)"
        )

    is_valid = len(issues) == 0
    return {
        "is_valid": is_valid,
        "n_samples": n,
        "n_features": d,
        "issues": issues,
        "warnings": warnings,
        "stats": {
            "n_nan": n_nan,
            "n_inf": n_inf,
            "n_duplicate_rows": n_dup,
            "n_near_dup_label_conflicts": near_dup_conflicts,
            "label_distribution": label_dist,
            "label_balance_ratio": (
                round(float(balance_ratio), 6) if balance_ratio is not None else None
            ),
            "per_feature_nan_counts": per_feature_nan,
        },
    }


# ── FastAPI route registration ──────────────────────────────────────────────


def register_routes(app: Any) -> None:
    """Append ``POST /api/ml/validate`` to a FastAPI app.

    Endpoint (auth-protected by the caller's existing fail-closed bearer
    middleware — every route except ``/api/health`` requires
    ``Authorization: Bearer <API_TOKEN>``):

      POST /api/ml/validate
          Body: ``ValidationRequest`` (see the model for field docs).
          Returns:
            {
              "model_class": <str>,
              "model_params": <dict>,
              "n_samples": <int>,
              "n_features": <int>,
              "validation_type": <'cv'|'oot'|'both'>,
              "generated_at": <epoch float>,
              "leakage_check": <validate_no_leakage result>,  # if run
              "cv": <time_series_cv result>,                 # if cv/both
              "oot": <out_of_time_test result>,               # if oot/both
            }
    """
    # Lazy import — FastAPI is optional at module load (keeps the module
    # importable in environments that only need the pure-Python validation
    # primitives, e.g. unit tests / notebooks without the API server).
    from fastapi import HTTPException

    @app.post("/api/ml/validate", tags=["ml-validation"])
    async def _ml_validate(req: ValidationRequest):
        """Run walk-forward CV and/or out-of-time validation on the posted data."""
        n_rows = len(req.X)
        if n_rows > MAX_PAYLOAD_ROWS:
            raise HTTPException(
                413,
                f"payload too large: {n_rows} rows > {MAX_PAYLOAD_ROWS} max",
            )

        # ── Build the model ──────────────────────────────────────────────
        cls_name = req.model_class or DEFAULT_MODEL_CLASS
        if cls_name not in _MODEL_WHITELIST:
            raise HTTPException(
                400,
                f"model_class {cls_name!r} not in whitelist: "
                f"{sorted(_MODEL_WHITELIST)}",
            )
        params = dict(req.model_params or {})
        try:
            model = _build_model(cls_name, params)
        except TypeError as e:
            # Bad kwarg for the chosen estimator.
            raise HTTPException(400, f"model_params rejected by {cls_name}: {e}")

        # ── Coerce inputs to numpy once ──────────────────────────────────
        X = np.asarray(req.X, dtype=np.float64)
        y = np.asarray(req.y, dtype=np.int64).ravel()
        if X.ndim != 2:
            raise HTTPException(400, f"X must be 2-D, got shape {X.shape}")
        if X.shape[0] != y.shape[0]:
            raise HTTPException(
                400,
                f"X/y length mismatch: X={X.shape[0]} y={y.shape[0]}",
            )

        response: dict[str, Any] = {
            "model_class": cls_name,
            "model_params": params,
            "n_samples": int(X.shape[0]),
            "n_features": int(X.shape[1]),
            "validation_type": req.validation_type,
            "generated_at": time.time(),
        }

        # ── Leakage audit (always-on unless explicitly disabled) ────────
        if req.run_leakage_check:
            response["leakage_check"] = validate_no_leakage(X, y)

        # ── Run the requested validation(s) ─────────────────────────────
        try:
            if req.validation_type in ("cv", "both"):
                response["cv"] = time_series_cv(
                    model,
                    X,
                    y,
                    n_splits=req.n_splits,
                    min_train_size=req.min_train_size,
                )
            if req.validation_type in ("oot", "both"):
                if req.X_test is None or req.y_test is None:
                    raise HTTPException(
                        400,
                        "validation_type='oot'/'both' requires X_test and y_test",
                    )
                response["oot"] = out_of_time_test(
                    model, X, y, req.X_test, req.y_test
                )
            if req.validation_type not in ("cv", "oot", "both"):
                raise HTTPException(
                    400,
                    f"validation_type must be 'cv', 'oot', or 'both'; "
                    f"got {req.validation_type!r}",
                )
        except HTTPException:
            raise
        except ValueError as e:
            # W15-6 (OWASP A02 — Information Disclosure): log the raw
            # ValueError server-side (it may include internal context like
            # column names or shape mismatches that are useful for the
            # operator), but return a generic 400 to the client so an
            # attacker can't probe the ML stack's internal structure.
            log.warning(
                "[ml_validation] /api/ml/validate ValueError: %s",
                e,
                exc_info=True,
            )
            raise HTTPException(
                400,
                "Validation request rejected — check the request schema "
                "(feature / label array shapes, validation_type value) and retry.",
            )
        except Exception as e:  # pragma: no cover — defensive last net
            # W15-6 (OWASP A02 — Information Disclosure): same posture as
            # the ValueError branch above — full traceback to the log, a
            # generic 500 to the client. The X-Request-ID response header
            # lets the operator correlate the client-visible 500 with the
            # server-side log entry.
            log.error("[ml_validation] /api/ml/validate failed: %s", e, exc_info=True)
            raise HTTPException(
                500,
                "Validation failed — see server logs for details "
                "(correlate via the X-Request-ID response header).",
            )

        return response


__all__ = [
    "DEFAULT_MODEL_CLASS",
    "DEFAULT_N_SPLITS",
    "DEFAULT_MIN_TRAIN_SIZE",
    "MAX_PAYLOAD_ROWS",
    "MAX_RAW_PREDICTIONS",
    "NEAR_DUP_SCAN_ROW_LIMIT",
    "NEAR_DUP_ROUND_DP",
    "MODEL_WHITELIST",
    "ValidationRequest",
    "time_series_cv",
    "out_of_time_test",
    "validate_no_leakage",
    "register_routes",
]

# Public alias for the whitelist (tests / introspection may import it by name).
MODEL_WHITELIST = _MODEL_WHITELIST
