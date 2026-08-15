"""
ml/model.py — Lightweight prediction market ML model.

Architecture:
  - Primary:  RandomForestClassifier  (interpretable, no GPU, ~5MB serialised)
  - Online:   SGDClassifier           (incremental, updates from every fill)
  - Ensemble: weighted average of both predictions

The model predicts P(YES resolves) for a given market feature vector.
Output confidence ∈ [0, 1] — used directly as the signal-trader score.

Persistence: model is saved to MODEL_PATH on every retrain so it survives
container restarts.
"""
from __future__ import annotations

import logging
import os
import pickle
import time
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
from sklearn.calibration import CalibratedClassifierCV
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler

from ml.features import N_FEATURES, FEATURE_NAMES

log = logging.getLogger(__name__)

MODEL_PATH = Path(os.environ.get("MODEL_PATH", "/app/data/model.pkl"))
SEED = 42


def _synthetic_training_data(n: int = 2000) -> Tuple[np.ndarray, np.ndarray]:
    """
    Generate synthetic training data calibrated to prediction market behaviour.

    Label = 1 (YES resolves) when:
      - mid price is high (strong market signal)
      - volume momentum is high (informed traders piling in)
      - close to expiry (price discovery complete)
    """
    rng = np.random.RandomState(SEED)
    X = rng.uniform(-1, 1, (n, N_FEATURES)).astype(np.float32)

    # Map feature cols back to meaningful ranges
    X[:, 0] = rng.uniform(0.05, 0.95, n)   # mid_price
    X[:, 1] = rng.uniform(0.00, 0.15, n)   # spread_norm
    X[:, 4] = rng.uniform(0.00, 1.00, n)   # vol_momentum
    X[:, 6] = rng.uniform(0.00, 1.00, n)   # days_left_norm
    X[:, 7] = rng.uniform(0.00, 1.00, n)   # urgency
    X[:, 8] = (X[:, 0] - 0.5) * 2          # price_extremity

    # Probabilistic label based on market micro-structure intuition
    mid = X[:, 0]
    vol_m = X[:, 4]
    urgency = X[:, 7]

    log_odds = (
        4.0 * (mid - 0.5)        # price is the strongest signal
        + 0.5 * vol_m             # volume confirms direction
        + 0.3 * urgency           # urgency: near-resolution = higher conviction
        + rng.normal(0, 0.5, n)  # noise
    )
    prob_yes = 1.0 / (1.0 + np.exp(-log_odds))
    y = (rng.uniform(0, 1, n) < prob_yes).astype(int)

    return X, y


class MarketMLModel:
    """
    Wraps a RandomForest (batch) + SGDClassifier (online) ensemble.
    Call predict(features) to get (direction, confidence).
    Call update(features, outcome) to incrementally learn from resolved markets.
    """

    def __init__(self) -> None:
        self.scaler = StandardScaler()
        self.rf: Optional[RandomForestClassifier] = None
        self.sgd = SGDClassifier(
            loss="log_loss",
            learning_rate="optimal",
            eta0=0.01,
            max_iter=1,
            warm_start=True,
            random_state=SEED,
        )
        self._sgd_trained = False
        self._n_updates = 0
        self._last_trained = 0.0
        self.feature_importances: dict = {}

    # ── Training ──────────────────────────────────────────────────────────────

    def fit_initial(self) -> None:
        """Bootstrap the model on synthetic data — runs once at startup."""
        log.info("[ML] Training initial model on synthetic data…")
        X, y = _synthetic_training_data(n=3000)
        X_scaled = self.scaler.fit_transform(X)

        self.rf = RandomForestClassifier(
            n_estimators=100,
            max_depth=6,
            min_samples_leaf=10,
            n_jobs=-1,
            random_state=SEED,
            class_weight="balanced",
        )
        self.rf.fit(X_scaled, y)

        # Seed the SGD with the same data for a warm start
        classes = np.array([0, 1])
        for _ in range(3):
            self.sgd.partial_fit(X_scaled, y, classes=classes)
        self._sgd_trained = True

        # Store feature importances for the API/UI
        self.feature_importances = {
            name: float(imp)
            for name, imp in zip(FEATURE_NAMES, self.rf.feature_importances_)
        }

        self._last_trained = time.time()
        log.info("[ML] Initial model trained. RF accuracy on training data: %.3f",
                 self.rf.score(X_scaled, y))

    def update(self, features: np.ndarray, resolved_yes: bool) -> None:
        """Incremental online update from a resolved market outcome."""
        if not self._sgd_trained or self.scaler is None:
            return
        label = 1 if resolved_yes else 0
        X = features.reshape(1, -1)
        X_scaled = self.scaler.transform(X)
        self.sgd.partial_fit(X_scaled, np.array([label]), classes=np.array([0, 1]))
        self._n_updates += 1
        log.debug("[ML] Online update #%d (label=%d)", self._n_updates, label)

    # ── Prediction ────────────────────────────────────────────────────────────

    def predict(self, features: np.ndarray) -> Tuple[float, float]:
        """
        Returns (p_yes: float, confidence: float) where:
          - p_yes ∈ [0, 1] — estimated probability of YES resolution
          - confidence ∈ [0, 1] — how far from 0.5 the model is (signal strength)
        """
        if self.rf is None:
            return 0.5, 0.0   # untrained — neutral

        X = features.reshape(1, -1)
        X_scaled = self.scaler.transform(X)

        # Random forest probability
        rf_proba = self.rf.predict_proba(X_scaled)[0]
        p_yes_rf = float(rf_proba[1]) if len(rf_proba) > 1 else 0.5

        # SGD probability (blended only after enough online updates)
        p_yes_sgd = 0.5
        if self._sgd_trained and self._n_updates >= 5:
            try:
                sgd_proba = self.sgd.predict_proba(X_scaled)[0]
                p_yes_sgd = float(sgd_proba[1]) if len(sgd_proba) > 1 else 0.5
            except Exception:
                pass

        # Ensemble: 80% RF, 20% SGD (RF dominates until SGD has enough data)
        sgd_weight = min(self._n_updates / 50.0, 0.3)
        rf_weight = 1.0 - sgd_weight
        p_yes = rf_weight * p_yes_rf + sgd_weight * p_yes_sgd

        # Confidence = distance from 0.5 (max = 0.5, normalise to [0,1])
        confidence = abs(p_yes - 0.5) * 2.0

        return p_yes, confidence

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self) -> None:
        MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(MODEL_PATH, "wb") as f:
                pickle.dump(self, f, protocol=pickle.HIGHEST_PROTOCOL)
            log.info("[ML] Model saved to %s", MODEL_PATH)
        except Exception as e:
            log.error("[ML] Save failed: %s", e)

    @classmethod
    def load(cls) -> "MarketMLModel":
        """Load from disk if available, else create and train fresh."""
        if MODEL_PATH.exists():
            try:
                with open(MODEL_PATH, "rb") as f:
                    model = pickle.load(f)
                log.info("[ML] Model loaded from %s (updates=%d)",
                         MODEL_PATH, model._n_updates)
                return model
            except Exception as e:
                log.warning("[ML] Could not load model: %s — retraining", e)
        model = cls()
        model.fit_initial()
        model.save()
        return model


# Module-level singleton — loaded/trained once at import time
ml_model = MarketMLModel.load()
