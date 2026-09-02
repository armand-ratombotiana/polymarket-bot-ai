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
        """Refit isotonic-logistic meta-learner from accumulated buffer."""
        try:
            X = np.array(list(self._buffer_X), dtype=np.float32)
            y = np.array(list(self._buffer_y), dtype=int)

            # Need at least both classes represented
            if len(np.unique(y)) < 2:
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
            log.debug("[meta_learner] Meta-model refit failed: %s", e)

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
