"""Probability calibration for ML model predictions.

Raw model outputs (especially from tree ensembles like RF/GB) are often
poorly calibrated — e.g., a predicted P(YES)=0.7 may only be correct 60%
of the time. This module applies Platt scaling (logistic) or isotonic
regression to calibrate predictions so they match observed frequencies.

Calibration is fit on a held-out calibration set (separate from train/test)
to avoid overfitting.

The module exposes a process-global singleton ``calibrator`` that the ML
ensemble's ``predict()`` path consults as a post-processing step:

    >>> from ml.calibration import calibrator
    >>> if calibrator.is_fit:
    ...     p = float(calibrator.transform(np.array([raw_p]))[0])
    ... else:
    ...     p = float(raw_p)

When the calibrator has not been fit yet, ``transform()`` is a passthrough,
so the integration is safe at cold-start.
"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Literal, Optional

import numpy as np
from sklearn.calibration import calibration_curve
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression

logger = logging.getLogger(__name__)

CalibrationMethod = Literal["platt", "isotonic", "none"]


class ProbabilityCalibrator:
    """Calibrates model probabilities using Platt scaling or isotonic regression.

    Two calibration strategies are supported:

    * ``"platt"``     — parametric logistic regression on the log-odds of
                        the raw probability. Works well with small
                        calibration sets (a handful of free parameters).
    * ``"isotonic"``  — non-parametric monotonic mapping. More flexible
                        (can fit arbitrary S-curves) but needs ≥ ~500
                        calibration samples to avoid overfitting.

    Both strategies fit on (raw_probability, observed_label) pairs from a
    *held-out* calibration set that the base learners never saw during
    training — the same discipline sklearn's ``CalibratedClassifierCV``
    enforces internally.
    """

    def __init__(self, method: CalibrationMethod = "isotonic") -> None:
        self.method: CalibrationMethod = method
        self._calibrator: Optional[object] = None
        self._is_fit: bool = False
        self._n_samples: int = 0
        # Cached metrics from the last ``fit()`` call — surfaced in the
        # ``/api/ml/metrics`` payload so operators can verify calibration
        # health at a glance.
        self.last_fit_metrics: dict = {"is_fit": False}

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_fit(self) -> bool:
        """``True`` once ``fit()`` has been called with a non-trivial set."""
        return self._is_fit

    @property
    def n_samples(self) -> int:
        return self._n_samples

    def fit(self, probs: np.ndarray, labels: np.ndarray) -> dict:
        """Fit the calibrator on (probability, label) pairs.

        Args:
            probs:  Raw model probabilities (n_samples,).
            labels: True binary labels (n_samples,) — 0 or 1.

        Returns:
            Dict with calibration metrics (pre/post Brier + ECE) describing
            the calibration improvement. Also cached on ``self.last_fit_metrics``.
        """
        if len(probs) != len(labels):
            raise ValueError(
                f"Length mismatch: probs={len(probs)}, labels={len(labels)}"
            )
        if len(probs) < 50:
            logger.warning(
                "Calibration set small (%d samples) — may overfit",
                len(probs),
            )

        probs = np.asarray(probs, dtype=np.float64).ravel()
        labels = np.asarray(labels, dtype=np.int64).ravel()

        # ── Pre-calibration metrics ────────────────────────────────────────────
        pre_brier = self._brier_score(probs, labels)
        pre_ece = self._expected_calibration_error(probs, labels)

        # ── Fit calibrator ────────────────────────────────────────────────────
        probs_clipped = np.clip(probs, 1e-6, 1 - 1e-6)

        if self.method == "platt":
            # Platt scaling: fit logistic regression on log-odds.
            log_odds = np.log(probs_clipped / (1 - probs_clipped)).reshape(-1, 1)
            self._calibrator = LogisticRegression(C=1e10, solver="lbfgs")
            self._calibrator.fit(log_odds, labels)
        elif self.method == "isotonic":
            # Isotonic regression: non-parametric monotonic mapping.
            self._calibrator = IsotonicRegression(
                out_of_bounds="clip", y_min=0, y_max=1
            )
            self._calibrator.fit(probs_clipped, labels)
        elif self.method == "none":
            # Explicit passthrough — useful for A/B comparisons.
            self._calibrator = None
        else:
            raise ValueError(
                f"Unknown calibration method: {self.method!r} "
                "(expected 'platt', 'isotonic', or 'none')"
            )

        self._is_fit = True
        self._n_samples = int(len(probs))

        # ── Post-calibration metrics ──────────────────────────────────────────
        calibrated = self.transform(probs)
        post_brier = self._brier_score(calibrated, labels)
        post_ece = self._expected_calibration_error(calibrated, labels)

        metrics = {
            "method": self.method,
            "n_samples": self._n_samples,
            "pre_brier": float(pre_brier),
            "post_brier": float(post_brier),
            "brier_improvement": float(pre_brier - post_brier),
            "pre_ece": float(pre_ece),
            "post_ece": float(post_ece),
            "ece_improvement": float(pre_ece - post_ece),
            "is_fit": True,
        }
        self.last_fit_metrics = metrics
        logger.info("Calibration fit: %s", metrics)
        return metrics

    def transform(self, probs: np.ndarray) -> np.ndarray:
        """Apply calibration to raw probabilities.

        Passthrough when not fit (so the integration is safe at cold-start
        and when calibration is disabled).
        """
        if not self._is_fit or self._calibrator is None:
            return np.asarray(probs, dtype=np.float64)
        probs_arr = np.asarray(probs, dtype=np.float64).ravel()
        probs_clipped = np.clip(probs_arr, 1e-6, 1 - 1e-6)
        if self.method == "platt":
            log_odds = np.log(probs_clipped / (1 - probs_clipped)).reshape(-1, 1)
            return self._calibrator.predict_proba(log_odds)[:, 1]
        elif self.method == "isotonic":
            return self._calibrator.transform(probs_clipped)
        return probs_arr

    # ── Metric helpers ────────────────────────────────────────────────────────

    def _brier_score(self, probs: np.ndarray, labels: np.ndarray) -> float:
        """Mean squared error of probabilities — lower is better."""
        return float(np.mean((np.asarray(probs, dtype=np.float64) -
                              np.asarray(labels, dtype=np.float64)) ** 2))

    def _expected_calibration_error(
        self, probs: np.ndarray, labels: np.ndarray, n_bins: int = 10
    ) -> float:
        """ECE: weighted average of bin-wise |confidence - accuracy|.

        Bins predictions into ``n_bins`` equal-width [0, 1] buckets; for each
        bucket computes ``|mean(predicted_prob) - mean(true_label)|`` and
        averages weighted by bucket occupancy. Lower is better; 0.0 means a
        perfectly reliable model.
        """
        probs = np.asarray(probs, dtype=np.float64).ravel()
        labels = np.asarray(labels, dtype=np.float64).ravel()
        bin_edges = np.linspace(0, 1, n_bins + 1)
        ece = 0.0
        n = len(probs)
        if n == 0:
            return 0.0
        for i in range(n_bins):
            if i == n_bins - 1:
                # Include the right edge for the last bin so p==1.0 isn't dropped.
                mask = (probs >= bin_edges[i]) & (probs <= bin_edges[i + 1])
            else:
                mask = (probs >= bin_edges[i]) & (probs < bin_edges[i + 1])
            if mask.sum() == 0:
                continue
            bin_conf = float(probs[mask].mean())
            bin_acc = float(labels[mask].mean())
            ece += (mask.sum() / n) * abs(bin_conf - bin_acc)
        return float(ece)

    def reliability_curve(
        self, probs: np.ndarray, labels: np.ndarray, n_bins: int = 10
    ) -> dict:
        """Compute reliability-diagram data via sklearn's ``calibration_curve``.

        Returns ``{prob_true, prob_pred, n_bins}`` where ``prob_true`` is the
        empirical YES-frequency in each bucket and ``prob_pred`` is the mean
        predicted probability in that bucket. Plotting ``prob_pred`` vs
        ``prob_true`` yields the reliability diagram.
        """
        labels = np.asarray(labels, dtype=np.int64).ravel()
        probs = np.asarray(probs, dtype=np.float64).ravel()
        prob_true, prob_pred = calibration_curve(
            labels, probs, n_bins=n_bins, strategy="uniform"
        )
        return {
            "prob_true": prob_true.tolist(),
            "prob_pred": prob_pred.tolist(),
            "n_bins": int(n_bins),
        }

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(self, path: Path) -> None:
        """Pickle the calibrator state to ``path`` (atomic write)."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "wb") as f:
            pickle.dump(
                {
                    "method": self.method,
                    "calibrator": self._calibrator,
                    "is_fit": self._is_fit,
                    "n_samples": self._n_samples,
                    "last_fit_metrics": self.last_fit_metrics,
                },
                f,
            )
        tmp.replace(path)

    def load(self, path: Path) -> None:
        """Restore calibrator state previously written by ``save``."""
        with open(path, "rb") as f:
            data = pickle.load(f)
        self.method = data["method"]
        self._calibrator = data["calibrator"]
        self._is_fit = data["is_fit"]
        self._n_samples = data.get("n_samples", 0)
        self.last_fit_metrics = data.get("last_fit_metrics", {"is_fit": self._is_fit})


# Global singleton — defaulted to isotonic regression (the more flexible
# non-parametric method). The ML ensemble's ``predict()`` path consults this
# singleton as a post-processing step; if no calibration has been fit yet,
# ``transform()`` is a passthrough.
calibrator = ProbabilityCalibrator(method="isotonic")
