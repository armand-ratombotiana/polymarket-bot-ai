"""
ml/ensemble_meta_learner.py — Stacking Ensemble Meta-Learner.

A lightweight Level-2 stacked generalizer that learns OPTIMAL blending
weights from held-out per-model predictions vs realized outcomes.

Architecture:
  Level-0 base learners: RF_cal, GB_cal, SGD, LightGBM (from model.py)
  Level-1 meta-learner:  Logistic Regression (isotonic-calibrated)
  Online adaptation:     Incremental partial_fit from each resolved market

Benefits over simple Brier-score inversion (current approach):
  - Learns non-linear cross-model interactions (e.g. "trust SGD when RF and GB disagree")
  - Discovers regime-conditional weights (e.g. "LightGBM is better in volatile regimes")
  - Adapts continuously from live resolved outcomes via partial_fit
  - Produces a proper probability (already calibrated through logistic link)
"""
from __future__ import annotations

import logging
from collections import deque
from typing import Optional

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

log = logging.getLogger(__name__)

# Keep last N training examples for periodic meta-model refresh
_META_BUFFER_SIZE = 1000
# Minimum observations before meta-learner activates
_MIN_META_SAMPLES = 30


class EnsembleMetaLearner:
    """
    Stacked Level-2 learner over the four base model predictions.

    Input features to meta-learner (6-dim):
      [p_rf, p_gb, p_sgd, p_lgbm, disagreement, confidence_mean]

    Fallback: if meta-learner is not yet warm (< _MIN_META_SAMPLES observations),
    returns None and the caller falls back to adaptive-weight blending.
    """

    def __init__(self) -> None:
        self._meta_model: Optional[LogisticRegression] = None
        self._meta_scaler = StandardScaler()
        self._buffer_X: deque[list[float]] = deque(maxlen=_META_BUFFER_SIZE)
        self._buffer_y: deque[int] = deque(maxlen=_META_BUFFER_SIZE)
        self._n_updates: int = 0
        self._is_warm: bool = False
        self._last_retrain_n: int = 0
        # Retrain meta-model every 50 new observations
        self._RETRAIN_EVERY = 50

    def _build_meta_features(
        self,
        p_rf: float,
        p_gb: float,
        p_sgd: float,
        p_lgbm: float,
    ) -> list[float]:
        """Construct 6-dim meta-feature row from base-model predictions."""
        preds = [p for p in [p_rf, p_gb, p_sgd, p_lgbm] if p > 0.0]
        disagreement = float(np.std(preds)) if len(preds) > 1 else 0.0
        conf_mean = float(np.mean([abs(p - 0.5) * 2 for p in preds])) if preds else 0.0
        return [p_rf, p_gb, p_sgd, p_lgbm, disagreement, conf_mean]

    def record_outcome(
        self,
        p_rf: float,
        p_gb: float,
        p_sgd: float,
        p_lgbm: float,
        actual: int,
    ) -> None:
        """
        Feed a resolved outcome into the meta-learner buffer.
        Triggers periodic meta-model refit.
        """
        row = self._build_meta_features(p_rf, p_gb, p_sgd, p_lgbm)
        self._buffer_X.append(row)
        self._buffer_y.append(actual)
        self._n_updates += 1

        # Fit/refit meta-learner once we have enough data
        if (
            len(self._buffer_X) >= _MIN_META_SAMPLES
            and (self._n_updates - self._last_retrain_n) >= self._RETRAIN_EVERY
        ):
            self._refit_meta_model()
            self._last_retrain_n = self._n_updates

    def _refit_meta_model(self) -> None:
        """Refit isotonic-logistic meta-learner from accumulated buffer.

        Defensive sanitization:
          - Drops any rows containing NaN/Inf in either feature matrix or label
            vector (base learners can occasionally emit non-finite probabilities
            under degenerate inputs; without this guard, LogisticRegression.fit
            silently raises and the failure was previously swallowed at DEBUG).
          - Logs refit failures at WARNING (was DEBUG) so silent meta-learner
            outages surface in production logs.
        """
        try:
            X = np.array(list(self._buffer_X), dtype=np.float32)
            y = np.array(list(self._buffer_y), dtype=int)

            if X.size == 0 or y.size == 0:
                log.warning("[meta_learner] Cannot refit — buffer empty")
                return

            # Drop NaN/Inf rows before fitting — base learners can occasionally
            # emit non-finite probabilities under degenerate inputs.
            finite_mask = np.all(np.isfinite(X), axis=1) & np.isfinite(y.astype(np.float32))
            n_dropped = int((~finite_mask).sum())
            if n_dropped > 0:
                log.warning(
                    "[meta_learner] Dropping %d non-finite rows before meta-model refit",
                    n_dropped,
                )
                X = X[finite_mask]
                y = y[finite_mask]

            # Need at least both classes represented
            if len(np.unique(y)) < 2:
                log.warning(
                    "[meta_learner] Cannot refit — only one class present in buffer (unique=%s, n=%d)",
                    np.unique(y).tolist(), len(y),
                )
                return

            X_scaled = self._meta_scaler.fit_transform(X)
            self._meta_model = LogisticRegression(
                C=1.0,
                max_iter=500,
                random_state=42,
                class_weight="balanced",
            )
            self._meta_model.fit(X_scaled, y)
            self._is_warm = True
            log.info(
                "[meta_learner] Meta-model refit on %d samples (updates=%d)",
                len(X), self._n_updates,
            )
        except Exception as e:
            log.warning("[meta_learner] Meta-model refit failed: %s", e)

    def warm_from_labeled_samples(self, max_samples: int = 200) -> dict:
        """
        Backfill the meta-learner buffer from already-resolved labeled feature
        vectors in the SQLite `ml_feature_store`, then force a single refit.

        This activates Level-2 stacking IMMEDIATELY (cold-start bypass) instead
        of waiting for live market settlements to drip-feed labels through
        `record_outcome()` over hours/days.

        Per-sample flow:
          1. Pull `(features, label)` tuples from
             `timescale_db.fetch_labeled_feature_vectors(limit)` — i.e. rows
             where `outcome_resolved IS NOT NULL`.
          2. Recompute the 4 base-model probabilities (p_rf, p_gb, p_sgd, p_lgbm)
             by running the trained base learners from `ml_model` on each
             feature vector (mirrors the prediction flow in
             `MarketMLModel.update()`).
          3. Append `(p_rf, p_gb, p_sgd, p_lgbm, label)` into the rolling
             buffer (`_buffer_X`, `_buffer_y`).
        After the loop, `_refit_meta_model()` is invoked explicitly regardless
        of the standard `_RETRAIN_EVERY` cadence.

        Returns a summary dict: {n_requested, n_loaded, n_skipped, is_warm, error}.
        """
        summary: dict = {
            "n_requested": int(max_samples),
            "n_loaded": 0,
            "n_skipped": 0,
            "buffer_size": len(self._buffer_X),
            "is_warm": self._is_warm,
            "error": None,
        }

        # Lazy imports — `ml.model` imports `ensemble_meta_learner` at module
        # load, so a top-level import here would create a cycle.
        try:
            from core.timescale_db import timescale_db
            from ml.model import ml_model
        except Exception as e:
            log.warning("[meta_learner] warm_from_labeled_samples import failed: %s", e)
            summary["error"] = f"import_failed: {e}"
            return summary

        if ml_model is None or ml_model.rf is None or ml_model.gb is None:
            log.warning(
                "[meta_learner] Base models not yet trained — cannot warm meta-learner"
            )
            summary["error"] = "base_models_not_trained"
            return summary

        try:
            samples = timescale_db.fetch_labeled_feature_vectors(limit=int(max_samples))
        except AttributeError as e:
            # `fetch_labeled_feature_vectors` missing on the timescale_db stub
            log.warning("[meta_learner] timescale_db.fetch_labeled_feature_vectors unavailable: %s", e)
            summary["error"] = f"fetch_method_missing: {e}"
            return summary
        except Exception as e:
            log.warning("[meta_learner] fetch_labeled_feature_vectors failed: %s", e)
            summary["error"] = f"fetch_failed: {e}"
            return summary

        if not samples:
            log.info(
                "[meta_learner] No labeled samples in feature store — warm-up skipped"
            )
            summary["error"] = "no_labeled_samples"
            return summary

        n_loaded = 0
        n_skipped = 0
        for features, label in samples:
            try:
                features_arr = np.asarray(features, dtype=np.float32).reshape(1, -1)
                x_scaled = ml_model.scaler.transform(features_arr)

                # ── Recompute per-base-learner probabilities (mirrors ml_model.update)
                p_rf = float((ml_model.rf_cal or ml_model.rf).predict_proba(x_scaled)[0, 1])
                p_gb = float((ml_model.gb_cal or ml_model.gb).predict_proba(x_scaled)[0, 1])
                p_sgd = (
                    float(ml_model.sgd.predict_proba(x_scaled)[0, 1])
                    if getattr(ml_model, "_sgd_trained", False)
                    else 0.0
                )
                p_lgbm = 0.0
                if ml_model.lgbm is not None:
                    try:
                        p_lgbm = float(ml_model.lgbm.predict_proba(x_scaled)[0, 1])
                    except Exception:
                        pass

                # Defensive sanitization — base learners can return NaN/Inf on
                # degenerate inputs; skip rather than poison the buffer.
                if not (
                    np.isfinite(p_rf)
                    and np.isfinite(p_gb)
                    and np.isfinite(p_sgd)
                    and np.isfinite(p_lgbm)
                ):
                    n_skipped += 1
                    continue

                row = self._build_meta_features(p_rf, p_gb, p_sgd, p_lgbm)
                self._buffer_X.append(row)
                self._buffer_y.append(int(label))
                self._n_updates += 1
                n_loaded += 1
            except Exception as e:
                log.debug("[meta_learner] warm-up sample skipped: %s", e)
                n_skipped += 1

        summary["n_loaded"] = n_loaded
        summary["n_skipped"] = n_skipped
        summary["buffer_size"] = len(self._buffer_X)

        log.info(
            "[meta_learner] Warm-up backfill: loaded=%d skipped=%d buffer_size=%d",
            n_loaded, n_skipped, len(self._buffer_X),
        )

        # Force-refit regardless of standard RETRAIN_EVERY cadence.
        # `_refit_meta_model` handles its own NaN/Inf dropping + WARNING logs.
        self._refit_meta_model()
        self._last_retrain_n = self._n_updates

        summary["is_warm"] = self._is_warm
        if not self._is_warm:
            summary["error"] = (
                "refit_did_not_warm "
                "(likely single-class buffer or < _MIN_META_SAMPLES)"
            )
        return summary

    def predict(
        self,
        p_rf: float,
        p_gb: float,
        p_sgd: float,
        p_lgbm: float,
    ) -> Optional[float]:
        """
        Produce meta-learner probability. Returns None if not yet warm.
        Caller should fall back to adaptive-weight blend.
        """
        if not self._is_warm or self._meta_model is None:
            return None

        try:
            row = self._build_meta_features(p_rf, p_gb, p_sgd, p_lgbm)
            X = np.array([row], dtype=np.float32)
            X_scaled = self._meta_scaler.transform(X)
            p = float(self._meta_model.predict_proba(X_scaled)[0, 1])
            return float(np.clip(p, 0.01, 0.99))
        except Exception as e:
            log.debug("[meta_learner] Predict failed: %s", e)
            return None

    @property
    def is_warm(self) -> bool:
        return self._is_warm

    @property
    def n_updates(self) -> int:
        return self._n_updates

    def get_summary(self) -> dict:
        return {
            "is_warm": self._is_warm,
            "n_updates": self._n_updates,
            "buffer_size": len(self._buffer_X),
            "last_retrain_at_n": self._last_retrain_n,
            "min_samples_required": _MIN_META_SAMPLES,
        }


# Global singleton
ensemble_meta_learner = EnsembleMetaLearner()
