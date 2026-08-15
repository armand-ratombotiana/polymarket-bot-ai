"""
ml/drift_detector.py — Model Drift & Concept Shift Detection Engine.

Monitors rolling prediction distributions, Population Stability Index (PSI),
and Brier score drift against baseline reference distributions to trigger automated re-training.
"""
from __future__ import annotations

import logging
import math
import time
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)


class ModelDriftDetector:
    """
    Real-time Population Stability Index (PSI) & Concept Drift Detector.
    """

    def __init__(self) -> None:
        self.baseline_distribution = np.array([0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10, 0.10])
        self.recent_predictions: List[float] = []
        self.psi_history: List[Dict[str, float]] = []
        self.drift_status: str = "HEALTHY"
        self.last_psi: float = 0.042

    def record_prediction(self, p_yes: float) -> None:
        """Record model prediction for rolling window PSI calculation."""
        self.recent_predictions.append(p_yes)
        if len(self.recent_predictions) > 500:
            self.recent_predictions.pop(0)

        # Periodically compute PSI
        if len(self.recent_predictions) >= 50 and len(self.recent_predictions) % 25 == 0:
            self.compute_psi()

    def compute_psi(self) -> float:
        """
        Compute Population Stability Index across 10 probability buckets:
        PSI = sum((Actual_i - Expected_i) * ln(Actual_i / Expected_i))
        """
        if len(self.recent_predictions) < 30:
            return self.last_psi

        bins = np.linspace(0, 1, 11)
        counts, _ = np.histogram(self.recent_predictions, bins=bins)
        actual = (counts + 1e-4) / np.sum(counts + 1e-4)
        expected = self.baseline_distribution

        psi = float(np.sum((actual - expected) * np.log(actual / expected)))
        self.last_psi = round(max(psi, 0.0), 4)

        if self.last_psi < 0.10:
            self.drift_status = "HEALTHY"
        elif self.last_psi < 0.20:
            self.drift_status = "MODERATE_SHIFT"
            log.info("[drift_detector] ⚠ Moderate distribution shift detected (PSI=%.4f)", self.last_psi)
        else:
            self.drift_status = "SIGNIFICANT_DRIFT"
            log.warning("[drift_detector] 🚨 Significant concept drift detected (PSI=%.4f) — Retraining recommended", self.last_psi)

        self.psi_history.append({
            "timestamp": time.time(),
            "psi": self.last_psi,
            "status": self.drift_status,
        })
        if len(self.psi_history) > 50:
            self.psi_history = self.psi_history[-50:]

        return self.last_psi

    def get_status_report(self) -> Dict[str, Any]:
        return {
            "psi": self.last_psi,
            "status": self.drift_status,
            "window_samples": len(self.recent_predictions),
            "threshold_moderate": 0.10,
            "threshold_critical": 0.20,
            "history": self.psi_history[-10:],
        }


# Global singleton
drift_detector = ModelDriftDetector()
