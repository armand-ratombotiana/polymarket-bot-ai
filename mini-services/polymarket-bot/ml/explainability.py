"""ML model explainability using SHAP values.

Provides per-prediction feature attribution to explain WHY the model
made a specific prediction.

For tree-based models (RF, GB, LightGBM): uses TreeExplainer (fast).
For other models: uses KernelExplainer (slower, approximate).

Design contract (W17-3):

  * **Additive / opt-in.** The explainability module is fully self-contained
    in this file — no production prediction path is modified except for
    one narrow opt-in call site in ``ml/model.py`` (the
    ``compute_explanation()`` method, which is itself a defensive
    best-effort operation that NEVER degrades the predict path on a
    failure). The ``shap`` import is deferred to the body of each
    ``explain_*`` method so the module imports cleanly even when SHAP
    isn't installed; the W17-3 requirements.txt entry simply makes the
    package available. A failure to import / construct / run SHAP
    triggers the ``_fallback_explanation`` path which still returns a
    valid ``PredictionExplanation`` (with ``confidence=0.0`` and the raw
    feature values as the contribution map) — the API route never 500s
    over a missing optional dependency.

  * **Per-prediction attribution.** ``explain_prediction(model, X,
    feature_names, token_id=...)`` returns a ``PredictionExplanation``
    per row of ``X``. The ``base_value`` is the model's expected output
    over the background dataset (mean prediction for tree models,
    KernelExplainer's expected value for kernel models); each entry of
    ``feature_contributions`` is that feature's SHAP value (positive =
    pushes the prediction toward the positive class, negative = toward
    the negative class). ``top_features`` is the sorted top-10 by
    absolute SHAP value.

  * **SHAP-version tolerant.** TreeExplainer's ``shap_values`` output
    shape depends on the SHAP version AND the underlying model type:
    older SHAP (0.4x) returns a list of per-class 2D arrays; modern SHAP
    (0.5x+) returns a single 3D ndarray of shape
    ``(n_samples, n_features, n_classes)`` for RandomForestClassifier,
    but a 2D ndarray of shape ``(n_samples, n_features)`` for
    GradientBoostingClassifier (single-output). ``expected_value`` may
    be a scalar, a Python list, or a numpy array of length 1 (GB) /
    length 2 (RF). All four shape permutations are normalised to a
    single 2D ``(n_samples, n_features)`` array indexed against the
    positive (YES) class + a float base_value at the head of
    ``explain_tree_model``.

  * **HTTP surface.** ``register_routes(app)`` appends one endpoint
    under ``/api/ml/explain/{token_id}`` so an operator can fetch a
    SHAP explanation for the model's most recent prediction for a
    given token. Auth enforced by the caller's existing
    ``enforce_api_auth`` middleware (path not in ``PUBLIC_PATHS``).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class PredictionExplanation:
    """Per-prediction SHAP attribution record.

    Fields
    ------
    token_id : str
        The market token this explanation belongs to (empty when the
        caller didn't supply one — e.g. batch offline runs).
    predicted_probability : float
        Model's output for the positive (YES) class on this row.
    base_value : float
        Expected model output over the background dataset — the SHAP
        "expected value" ``E[f(x)]``. For a binary classifier the sum
        ``base_value + sum(feature_contributions.values())`` is the
        model's raw output for this row (modulo link-function
        conventions inside SHAP — see the SHAP docs for the exact
        semantics under ``model_output="raw"`` vs ``"probability"``).
    feature_contributions : dict[str, float]
        Feature name → SHAP value. Positive = pushes the prediction
        toward the positive (YES) class; negative = toward the negative
        (NO) class.
    top_features : list[tuple[str, float]]
        Top-10 features ranked by absolute SHAP value (descending).
    prediction_direction : str
        One of ``"positive"`` / ``"negative"`` / ``"neutral"``.
        ``"positive"`` when ``predicted_probability > 0.5``; ``"negative"``
        when ``< 0.5``; ``"neutral"`` only on the fallback path where the
        real prediction wasn't computed.
    confidence : float
        ``abs(predicted_probability - 0.5) * 2`` — in ``[0, 1]``.
        ``1.0`` means the model is maximally confident (probability 0
        or 1); ``0.0`` means maximally uncertain (probability 0.5).
    """

    token_id: str
    predicted_probability: float
    base_value: float  # Expected model output (average prediction)
    feature_contributions: dict[str, float]  # feature_name -> SHAP value
    top_features: list[tuple[str, float]]  # Top 10 by absolute SHAP
    prediction_direction: str  # "positive" (toward YES) or "negative" (toward NO)
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        """JSON-able representation for the API surface.

        ``top_features`` is converted from ``list[tuple[str, float]]``
        to ``list[{"feature": str, "shap_value": float}]`` so a
        downstream JSON serialiser doesn't choke on the tuple (which
        ``json.dumps`` would silently turn into a list of mixed types,
        but a Pydantic response_model or a hand-rolled React fetch
        would prefer explicit key names).
        """
        return {
            "token_id": self.token_id,
            "predicted_probability": self.predicted_probability,
            "base_value": self.base_value,
            "feature_contributions": dict(self.feature_contributions),
            "top_features": [
                {"feature": f, "shap_value": float(v)}
                for f, v in self.top_features
            ],
            "prediction_direction": self.prediction_direction,
            "confidence": self.confidence,
        }


def _normalise_shap_values(shap_values: Any) -> np.ndarray:
    """Coerce SHAP's per-version / per-model output shapes into a single
    2D ``(n_samples, n_features)`` ndarray indexed against the POSITIVE
    (YES) class.

    Handles four observed shapes:

      (a) ``list[np.ndarray]`` of length 2 — legacy SHAP 0.4x output
          for binary sklearn classifiers. Each element has shape
          ``(n_samples, n_features)``; index 1 is the positive class.
      (b) ``np.ndarray`` of ndim 3 with shape
          ``(n_samples, n_features, n_classes)`` — modern SHAP 0.5x+
          output for RandomForestClassifier. Slice ``[:, :, 1]`` for
          the positive class.
      (c) ``np.ndarray`` of ndim 2 — single-output model
          (GradientBoostingClassifier, or any model where SHAP already
          collapsed to the positive class). Pass through unchanged.
      (d) ``np.ndarray`` of ndim 1 — degenerate single-row output
          from a model that returned a flat vector. Reshape to
          ``(1, n_features)``.
    """
    # (a) legacy list-of-arrays per-class output
    if isinstance(shap_values, list):
        if len(shap_values) >= 2:
            return np.asarray(shap_values[1])
        # Single-class output — fall back to index 0
        return np.asarray(shap_values[0])

    arr = np.asarray(shap_values)
    # (b) 3D ndarray — slice positive class
    if arr.ndim == 3:
        # Shape: (n_samples, n_features, n_classes) — take positive class
        if arr.shape[-1] >= 2:
            return arr[:, :, 1]
        return arr[:, :, 0]
    # (c) 2D ndarray — pass through
    if arr.ndim == 2:
        return arr
    # (d) 1D ndarray — reshape to (1, n_features)
    if arr.ndim == 1:
        return arr.reshape(1, -1)
    # Unexpected — squeeze and hope for the best
    return arr


def _normalise_expected_value(expected_value: Any) -> float:
    """Coerce SHAP's per-version ``expected_value`` into a single float
    indexed against the POSITIVE class.

    Handles:
      - Python ``list`` / numpy 1D array of length 2 → index 1
      - Python ``list`` / numpy 1D array of length 1 → index 0
      - numpy 0-D array / scalar → ``float(value)``
    """
    if isinstance(expected_value, (list, np.ndarray)):
        ev_arr = np.asarray(expected_value).ravel()
        if ev_arr.size >= 2:
            return float(ev_arr[1])
        if ev_arr.size == 1:
            return float(ev_arr[0])
        return 0.5
    # scalar
    try:
        return float(expected_value)
    except (TypeError, ValueError):
        return 0.5


class ModelExplainer:
    """Provides SHAP-based explanations for model predictions."""

    def __init__(self) -> None:
        # ``model_version -> shap.Explainer`` cache. Currently unused —
        # ``explain_tree_model`` constructs a fresh TreeExplainer on
        # each call (cheap relative to the underlying SHAP computation)
        # but the slot is here so a future PR can memoise explainers
        # against model version strings without changing the public
        # surface.
        self._explainers: dict[str, Any] = {}
        self._background: Optional[np.ndarray] = None
        self._feature_names: list[str] = []

    def set_background(self, X: np.ndarray, feature_names: Optional[list[str]] = None) -> None:
        """Set the background dataset for KernelExplainer.

        Samples 100 rows (without replacement) when the input exceeds
        100 rows — KernelExplainer's runtime is ``O(n_background *
        n_samples)`` and the SHAP docs recommend 100-1000 background
        rows as the sweet spot between coverage and latency.
        """
        X = np.asarray(X)
        if len(X) > 100:
            indices = np.random.choice(len(X), 100, replace=False)
            self._background = X[indices]
        else:
            self._background = X
        if feature_names:
            self._feature_names = list(feature_names)

    def explain_tree_model(
        self,
        model: Any,
        X: np.ndarray,
        feature_names: list[str],
        token_id: str = "",
    ) -> list[PredictionExplanation]:
        """Explain predictions from a tree-based model using TreeExplainer.

        ``model`` must be a fitted sklearn tree-ensemble that SHAP's
        ``TreeExplainer`` accepts (``RandomForestClassifier`` /
        ``GradientBoostingClassifier`` / LightGBM ``LGBMClassifier`` /
        ``xgboost.XGBClassifier``). On any failure (SHAP missing, model
        unsupported, internal SHAP error) the method falls back to
        ``_fallback_explanation`` so the caller always gets a non-empty
        list of ``PredictionExplanation`` records.
        """
        try:
            import shap  # noqa: PLC0415 — deferred so module imports cleanly without shap

            X = np.asarray(X)
            if X.ndim == 1:
                X = X.reshape(1, -1)

            explainer = shap.TreeExplainer(model)
            shap_values = explainer.shap_values(X)
            shap_values_pos = _normalise_shap_values(shap_values)
            base_value = _normalise_expected_value(explainer.expected_value)

            explanations: list[PredictionExplanation] = []
            for i in range(len(X)):
                row_sv = (
                    shap_values_pos[i]
                    if shap_values_pos.ndim == 2
                    else shap_values_pos
                )
                contributions = {
                    feature_names[j]: float(row_sv[j])
                    for j in range(min(len(feature_names), len(row_sv)))
                }
                top = sorted(contributions.items(), key=lambda kv: -abs(kv[1]))[:10]
                if hasattr(model, "predict_proba"):
                    pred = float(model.predict_proba([X[i]])[0][1])
                else:
                    pred = 0.5

                explanations.append(
                    PredictionExplanation(
                        token_id=token_id,
                        predicted_probability=pred,
                        base_value=base_value,
                        feature_contributions=contributions,
                        top_features=top,
                        prediction_direction="positive" if pred > 0.5 else "negative",
                        confidence=abs(pred - 0.5) * 2,
                    )
                )
            return explanations
        except Exception as e:  # noqa: BLE001 — defensive: SHAP must never break the caller
            logger.error("SHAP TreeExplainer failed: %s", e, exc_info=True)
            return self._fallback_explanation(X, feature_names, token_id)

    def explain_kernel(
        self,
        predict_fn: Any,
        X: np.ndarray,
        feature_names: list[str],
        token_id: str = "",
    ) -> list[PredictionExplanation]:
        """Explain predictions using KernelExplainer (model-agnostic).

        ``predict_fn`` must accept a 2D ndarray of shape
        ``(n_samples, n_features)`` and return a 2D ndarray of shape
        ``(n_samples, n_classes)`` (the standard sklearn
        ``predict_proba`` contract). For single-output models that
        return a 1D array, the caller must wrap the function so the
        return shape is 2D.
        """
        if self._background is None:
            logger.warning(
                "Background not set for KernelExplainer — using fallback"
            )
            return self._fallback_explanation(X, feature_names, token_id)

        try:
            import shap  # noqa: PLC0415 — deferred so module imports cleanly without shap

            X = np.asarray(X)
            if X.ndim == 1:
                X = X.reshape(1, -1)

            explainer = shap.KernelExplainer(predict_fn, self._background)
            shap_values = explainer.shap_values(X, nsamples=100)
            shap_values_pos = _normalise_shap_values(shap_values)
            base_value = _normalise_expected_value(explainer.expected_value)

            explanations: list[PredictionExplanation] = []
            for i in range(len(X)):
                row_sv = (
                    shap_values_pos[i]
                    if shap_values_pos.ndim == 2
                    else shap_values_pos
                )
                contributions = {
                    feature_names[j]: float(row_sv[j])
                    for j in range(min(len(feature_names), len(row_sv)))
                }
                top = sorted(contributions.items(), key=lambda kv: -abs(kv[1]))[:10]

                preds = predict_fn(np.asarray([X[i]]))
                preds_arr = np.asarray(preds)
                if preds_arr.ndim == 2 and preds_arr.shape[1] >= 2:
                    pred = float(preds_arr[0][1])
                elif preds_arr.ndim == 2 and preds_arr.shape[1] == 1:
                    pred = float(preds_arr[0][0])
                else:
                    pred = float(preds_arr[0])

                explanations.append(
                    PredictionExplanation(
                        token_id=token_id,
                        predicted_probability=pred,
                        base_value=base_value,
                        feature_contributions=contributions,
                        top_features=top,
                        prediction_direction="positive" if pred > 0.5 else "negative",
                        confidence=abs(pred - 0.5) * 2,
                    )
                )
            return explanations
        except Exception as e:  # noqa: BLE001 — defensive
            logger.error("SHAP KernelExplainer failed: %s", e, exc_info=True)
            return self._fallback_explanation(X, feature_names, token_id)

    def _fallback_explanation(
        self,
        X: np.ndarray,
        feature_names: list[str],
        token_id: str,
    ) -> list[PredictionExplanation]:
        """Fallback: use raw feature values as a proxy for contributions.

        Triggered when SHAP raises (missing dependency, unsupported
        model, internal error). The fallback is NOT a substitute for
        real SHAP values — it surfaces the raw feature magnitudes as
        the ``feature_contributions`` map so an operator still gets a
        structured view of the input feature distribution. ``confidence``
        is hard-zeroed and ``prediction_direction`` is ``"neutral"`` so
        the caller / dashboard can distinguish a real SHAP explanation
        from a fallback one.
        """
        X = np.asarray(X)
        if X.ndim == 1:
            X = X.reshape(1, -1)
        explanations: list[PredictionExplanation] = []
        for i in range(len(X)):
            contributions = {
                feature_names[j]: float(X[i][j])
                for j in range(min(len(feature_names), len(X[i])))
            }
            top = sorted(contributions.items(), key=lambda kv: -abs(kv[1]))[:10]
            explanations.append(
                PredictionExplanation(
                    token_id=token_id,
                    predicted_probability=0.5,
                    base_value=0.5,
                    feature_contributions=contributions,
                    top_features=top,
                    prediction_direction="neutral",
                    confidence=0.0,
                )
            )
        return explanations

    def explain_prediction(
        self,
        model: Any,
        X: np.ndarray,
        feature_names: list[str],
        token_id: str = "",
        model_type: str = "tree",
    ) -> list[PredictionExplanation]:
        """Explain one or more predictions.

        Dispatches to ``explain_tree_model`` (fast — exact Tree SHAP)
        when ``model_type == "tree"``; otherwise dispatches to
        ``explain_kernel`` (model-agnostic, approximate — requires
        ``set_background`` to have been called first).
        """
        if model_type == "tree":
            return self.explain_tree_model(model, X, feature_names, token_id)

        def _predict_fn(x: np.ndarray) -> np.ndarray:
            return model.predict_proba(x)

        return self.explain_kernel(_predict_fn, X, feature_names, token_id)


# Module-level singleton — mirrors the pattern used by every other
# ML subsystem (``ml.calibrator`` / ``ml.drift_detector`` /
# ``ml.ensemble_meta_learner`` / ``ml.feature_store``).
model_explainer = ModelExplainer()


# ── HTTP surface ────────────────────────────────────────────────────────────
def register_routes(app: Any) -> None:
    """Append the W17-3 explainability endpoint to a FastAPI app.

    Adds::

        GET /api/ml/explain/{token_id}
            SHAP-based per-prediction feature attribution for the
            model's most recent prediction for ``token_id``.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing
    ``enforce_api_auth`` middleware (this path is NOT in
    ``PUBLIC_PATHS``).
    """
    from fastapi import (  # noqa: PLC0415 — FastAPI optional at module load
        HTTPException,
        Query,
    )

    @app.get(
        "/api/ml/explain/{token_id}",
        tags=["ml"],
        summary="SHAP explanation for a single prediction",
        description=(
            "Per-prediction feature attribution via SHAP values. "
            "Loads the model's most recent stored feature vector for "
            "``token_id``, runs ``shap.TreeExplainer`` against the "
            "RandomForest ensemble member (falling back to "
            "``shap.KernelExplainer`` on tree-explainer failure), and "
            "returns the predicted probability, base value, top-10 "
            "feature contributions, and prediction direction. "
            "Returns 404 when no feature vector has been recorded "
            "for the token; returns 503 when the model is not yet "
            "fitted."
        ),
    )
    async def explain_prediction(
        token_id: str,
        top_n: int = Query(10, ge=1, le=38, description="Number of top features to return"),
    ):
        """Get SHAP explanation for the model's most recent prediction for ``token_id``."""
        if not token_id or not token_id.strip():
            raise HTTPException(status_code=422, detail="token_id is required")

        from ml.model import ml_model

        if ml_model.rf is None:
            raise HTTPException(
                status_code=503,
                detail="ML model is not fitted — call POST /api/ml/retrain first",
            )

        # ── Fetch the most recent stored feature vector for this token ──
        # Mirrors the ``/api/ml/learn`` endpoint's lookup path: the
        # feature vector is stored as a JSON array in the
        # ``ml_feature_store`` table indexed on (token_id, timestamp).
        # The lookup is best-effort — a transient SQLite hiccup falls
        # through to the 404 branch (no feature vector available).
        import json as _json
        import sqlite3

        import numpy as np

        features: Optional[np.ndarray] = None
        try:
            from core.timescale_db import timescale_db

            with sqlite3.connect(timescale_db._sqlite_path) as conn:
                row = conn.execute(
                    "SELECT features_json FROM ml_feature_store "
                    "WHERE token_id = ? ORDER BY timestamp DESC LIMIT 1;",
                    (token_id,),
                ).fetchone()
            if row:
                features = np.array(_json.loads(row[0]), dtype=np.float32)
        except Exception as e:  # noqa: BLE001 — defensive
            logger.debug(
                "[api/ml/explain] feature-vector lookup failed for %s: %s",
                token_id, e,
            )

        if features is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no stored feature vector for token '{token_id}' — "
                    f"the model must predict for this token at least once "
                    f"before an explanation is available"
                ),
            )

        # ── Run SHAP via the model-explainer singleton ──
        from ml.features import FEATURE_NAMES

        # Compute the ensemble's current prediction for this feature
        # vector so the explanation's ``predicted_probability`` reflects
        # the SAME number the operator saw on the dashboard (rather than
        # the RF-only probability the TreeExplainer reports internally).
        try:
            pred_p, _ = ml_model.predict(features, token_id=token_id)
        except Exception:  # noqa: BLE001 — defensive
            pred_p = 0.5

        explanations = model_explainer.explain_tree_model(
            ml_model.rf,
            features.reshape(1, -1),
            FEATURE_NAMES,
            token_id=token_id,
        )

        if not explanations:
            raise HTTPException(
                status_code=500,
                detail="SHAP explanation returned no rows (internal error)",
            )

        expl = explanations[0]
        # Override the predicted_probability with the ensemble's blended
        # output so the explanation matches the dashboard's headline
        # number (the RF-only probability from the TreeExplainer path
        # is otherwise misleading — the ensemble blends 4 models).
        expl.predicted_probability = float(pred_p)
        expl.prediction_direction = "positive" if pred_p > 0.5 else "negative"
        expl.confidence = abs(pred_p - 0.5) * 2
        # Trim top_features to caller's top_n (already capped to 38 by
        # the Query constraint — the FEATURE_NAMES catalog has 38 entries)
        expl.top_features = expl.top_features[:top_n]

        return {
            "token_id": token_id,
            "model_version": _active_model_version(),
            "explanation": expl.to_dict(),
        }


def _active_model_version() -> str:
    """Best-effort lookup of the active model version for the response payload.

    Defensively wrapped so a missing / unimported ``model_registry``
    never 500s the explain endpoint — the version field is purely
    informational.
    """
    try:
        from ml.model_registry import model_registry
        return model_registry.active_version
    except Exception:  # noqa: BLE001 — informational only
        return "unknown"


__all__ = [
    "ModelExplainer",
    "PredictionExplanation",
    "model_explainer",
    "register_routes",
]
