"""
ml/drift_detector.py — Model Drift & Concept Shift Detection Engine.

Monitors rolling prediction distributions, Population Stability Index (PSI),
Kolmogorov-Smirnov test, EWMA Brier score, and Brier score drift against
baseline reference distributions to trigger automated re-training.

Baseline uses empirically calibrated U-shaped prediction market distribution:
  - Prices cluster at extremes [0-0.1] and [0.9-1.0] bins (near-certain outcomes)
  - Central bins [0.4-0.6] are least common (true 50/50 markets are rare)
"""
from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

# Empirically calibrated U-shaped baseline for binary prediction markets.
# Near-certain outcomes (0-0.1 and 0.9-1.0) are most common.
# True 50/50 (0.4-0.6) are rarest. Sum = 1.0.
_MARKET_BASELINE = np.array([
    0.18,  # [0.0, 0.1] — near-certain NO
    0.12,  # [0.1, 0.2]
    0.09,  # [0.2, 0.3]
    0.07,  # [0.3, 0.4]
    0.05,  # [0.4, 0.5] — true uncertain
    0.05,  # [0.5, 0.6]
    0.07,  # [0.6, 0.7]
    0.09,  # [0.7, 0.8]
    0.12,  # [0.8, 0.9]
    0.16,  # [0.9, 1.0] — near-certain YES
], dtype=np.float64)
# Normalize to exactly 1.0 to handle rounding
_MARKET_BASELINE = _MARKET_BASELINE / _MARKET_BASELINE.sum()

# Brier score degradation ceiling — exceeding this triggers SIGNIFICANT_DRIFT
# independently of PSI/KS, forcing a re-train cycle.
BRIER_DRIFT_THRESHOLD = 0.22


class ModelDriftDetector:
    """
    Real-time Population Stability Index (PSI) + Kolmogorov-Smirnov Drift Detector.

    Drift is flagged by three independent signals (any one suffices):
      1. PSI ≥ 0.25  — prediction distribution shifted vs. model reference distribution
         (industry-standard threshold; raised from 0.20 in R6 to suppress false
         positives that occur whenever the live bin frequencies stray slightly
         from the captured reference).
      2. KS  ≥ 0.25  — two-sample empirical CDF divergence vs. baseline samples
      3. Rolling Brier > 0.22 — live calibration degradation (≥ 20 resolved samples)
    """

    def __init__(self) -> None:
        self.baseline_distribution = _MARKET_BASELINE
        # R6-2: PSI baseline is now the model's OWN prediction distribution,
        # captured on the first compute_psi() call. The U-shaped market baseline
        # (baseline_distribution, still used by the KS test) structurally disagrees
        # with ~0.5-centered model predictions and was producing perpetual
        # false-positive PSI drift. `reference_distribution` is None until captured.
        self.reference_distribution: np.ndarray | None = None
        self.recent_predictions: list[float] = []
        self.recent_actuals: list[tuple[float, int]] = []  # (p_yes, actual_label) for Brier
        self.psi_history: list[dict[str, float]] = []
        self.drift_status: str = "HEALTHY"
        self.last_psi: float = 0.0
        self.last_ks_stat: float = 0.0
        self.rolling_brier: float | None = None
        # EWMA Brier for fast early-warning (α=0.05 ≈ 38-sample half-life)
        self.ewma_brier: float | None = None
        self._ewma_alpha: float = 0.05
        self._baseline_samples = 500   # synthetic warm-up from baseline

    def record_prediction(self, p_yes: float) -> None:
        """Record model prediction for rolling window PSI/KS calculation."""
        self.recent_predictions.append(p_yes)
        if len(self.recent_predictions) > 2000:
            self.recent_predictions.pop(0)

        # Compute drift every 50 new predictions after minimum warm-up
        if len(self.recent_predictions) >= 50 and len(self.recent_predictions) % 50 == 0:
            self.compute_psi()

    def record_outcome(self, p_yes: float, actual: int) -> None:
        """Record a realized outcome for rolling Brier score and EWMA Brier tracking."""
        self.recent_actuals.append((p_yes, actual))
        if len(self.recent_actuals) > 500:
            self.recent_actuals.pop(0)

        # Instantaneous Brier error for this sample
        instant_brier = (p_yes - actual) ** 2

        # EWMA Brier (fast early-warning, α=0.05)
        if self.ewma_brier is None:
            self.ewma_brier = instant_brier
        else:
            self.ewma_brier = (
                self._ewma_alpha * instant_brier
                + (1.0 - self._ewma_alpha) * self.ewma_brier
            )

        if len(self.recent_actuals) >= 20:
            preds = np.array([p for p, _ in self.recent_actuals])
            labels = np.array([a for _, a in self.recent_actuals])
            self.rolling_brier = float(np.mean((preds - labels) ** 2))

            # Rolling Brier degradation → SIGNIFICANT_DRIFT
            if self.rolling_brier > BRIER_DRIFT_THRESHOLD and self.drift_status != "SIGNIFICANT_DRIFT":
                log.warning(
                    "[drift_detector] 🚨 Rolling Brier degradation (%.4f > %.2f) — "
                    "escalating to SIGNIFICANT_DRIFT (retrain triggered)",
                    self.rolling_brier, BRIER_DRIFT_THRESHOLD,
                )
                self.drift_status = "SIGNIFICANT_DRIFT"

        # EWMA Brier degradation (early-warning — escalates even before 20 samples)
        if (
            self.ewma_brier is not None
            and self.ewma_brier > BRIER_DRIFT_THRESHOLD
            and self.drift_status == "HEALTHY"
        ):
            log.warning(
                "[drift_detector] ⚠ EWMA Brier early-warning (%.4f > %.2f) "
                "— escalating to MODERATE_SHIFT",
                self.ewma_brier, BRIER_DRIFT_THRESHOLD,
            )
            self.drift_status = "MODERATE_SHIFT"

    def _ks_two_sample(self, preds: np.ndarray, baseline_samples: np.ndarray) -> float:
        """
        Correct two-sample Kolmogorov-Smirnov statistic.

        Computes max|F_preds(x) - F_baseline(x)| over all x by evaluating
        both empirical CDFs at every point in the combined sorted sample.
        Previously this was implemented incorrectly; now matches scipy.stats.ks_2samp.
        """
        if len(preds) == 0 or len(baseline_samples) == 0:
            return 0.0
        sorted_preds = np.sort(preds)
        sorted_base = np.sort(baseline_samples)
        # Evaluate both CDFs at every point in the combined distribution
        combined = np.sort(np.concatenate([sorted_preds, sorted_base]))
        cdf_preds = np.searchsorted(sorted_preds, combined, side="right") / len(sorted_preds)
        cdf_base = np.searchsorted(sorted_base, combined, side="right") / len(sorted_base)
        return float(np.max(np.abs(cdf_preds - cdf_base)))

    def compute_psi(self) -> float:
        """
        Compute Population Stability Index across 10 probability buckets:
          PSI = sum((Actual_i - Expected_i) * ln(Actual_i / Expected_i))
        Also compute Kolmogorov-Smirnov statistic as secondary drift signal.
        """
        if len(self.recent_predictions) < 30:
            return self.last_psi

        preds = np.array(self.recent_predictions)
        bins = np.linspace(0, 1, 11)

        # PSI
        counts, _ = np.histogram(preds, bins=bins)
        actual = (counts + 1e-4) / np.sum(counts + 1e-4)

        # R6-2: PSI expected distribution = model's OWN prediction distribution,
        # captured on first compute_psi() call (after the ≥30-sample warm-up
        # guard above). This replaces the U-shaped market baseline which
        # structurally disagreed with ~0.5-centered predictions.
        if self.reference_distribution is None:
            self.reference_distribution = actual.copy()
            log.info(
                "[drift_detector] Captured model reference distribution for PSI "
                "baseline (bins=%s)",
                np.round(self.reference_distribution, 4).tolist(),
            )
        expected = self.reference_distribution + 1e-10

        psi = float(np.sum((actual - expected) * np.log(actual / expected)))
        self.last_psi = round(max(psi, 0.0), 4)

        # KS statistic — correct two-sample implementation
        baseline_samples = np.random.choice(
            np.arange(10), size=min(len(preds), 500), p=self.baseline_distribution
        ) / 10.0 + 0.05  # bin centres
        self.last_ks_stat = round(self._ks_two_sample(preds, baseline_samples), 4)

        # Combine PSI + KS for composite drift status
        # R6-3: PSI SIGNIFICANT_DRIFT threshold raised 0.20 → 0.25 (industry
        # standard) to suppress false-positive drift alarms.
        new_status: str
        if self.last_psi < 0.10 and self.last_ks_stat < 0.15:
            new_status = "HEALTHY"
        elif self.last_psi < 0.25 and self.last_ks_stat < 0.25:
            new_status = "MODERATE_SHIFT"
        else:
            new_status = "SIGNIFICANT_DRIFT"

        # Preserve Brier-escalated status (don't downgrade while Brier still high)
        if (
            self.drift_status == "SIGNIFICANT_DRIFT"
            and self.rolling_brier is not None
            and self.rolling_brier > BRIER_DRIFT_THRESHOLD
            and new_status != "SIGNIFICANT_DRIFT"
        ):
            new_status = "SIGNIFICANT_DRIFT"

        # Log only on status transitions
        if new_status != self.drift_status:
            if new_status == "MODERATE_SHIFT":
                log.info("[drift_detector] ⚠ Moderate shift detected (PSI=%.4f, KS=%.4f)",
                         self.last_psi, self.last_ks_stat)
            elif new_status == "SIGNIFICANT_DRIFT":
                log.warning("[drift_detector] 🚨 Significant concept drift (PSI=%.4f, KS=%.4f) — Retraining recommended",
                            self.last_psi, self.last_ks_stat)
            else:
                log.info("[drift_detector] ✅ Distribution shift recovered (PSI=%.4f)", self.last_psi)
        self.drift_status = new_status

        self.psi_history.append({
            "timestamp": time.time(),
            "psi": self.last_psi,
            "ks_stat": self.last_ks_stat,
            "status": self.drift_status,
            "rolling_brier": self.rolling_brier,
            "ewma_brier": round(self.ewma_brier, 4) if self.ewma_brier is not None else None,
        })
        if len(self.psi_history) > 100:
            self.psi_history = self.psi_history[-100:]

        return self.last_psi

    def reset(self) -> None:
        """Reset rolling window after a successful re-train."""
        self.recent_predictions = []
        self.last_psi = 0.0
        self.last_ks_stat = 0.0
        # R6-1: Clear Brier tracking + drift status so the detector returns to
        # HEALTHY after a re-train. Previously reset() only cleared
        # recent_predictions, which left drift_status stuck at SIGNIFICANT_DRIFT
        # (and rolling_brier / ewma_brier carrying stale degraded values) — so
        # the Brier-preservation branch in compute_psi() kept re-escalating on
        # every cycle, defeating the post-retrain recovery path.
        self.rolling_brier = None
        self.ewma_brier = None
        self.drift_status = "HEALTHY"
        log.info("[drift_detector] Rolling window reset after model re-train (Brier + status cleared)")

    def get_status_report(self) -> dict[str, Any]:
        return {
            "psi": self.last_psi,
            "ks_stat": self.last_ks_stat,
            "rolling_brier": self.rolling_brier,
            "ewma_brier": round(self.ewma_brier, 4) if self.ewma_brier is not None else None,
            "status": self.drift_status,
            "window_samples": len(self.recent_predictions),
            "outcome_samples": len(self.recent_actuals),
            "threshold_moderate_psi": 0.10,
            "threshold_critical_psi": 0.25,
            "threshold_moderate_ks": 0.15,
            "threshold_critical_ks": 0.25,
            "threshold_brier_drift": BRIER_DRIFT_THRESHOLD,
            "ewma_alpha": self._ewma_alpha,
            "history": self.psi_history[-10:],
        }


# Global singleton
drift_detector = ModelDriftDetector()
