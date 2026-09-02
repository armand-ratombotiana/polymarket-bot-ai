"""
ml/training_orchestrator.py — Automated Continuous Drift-Triggered Model Re-Training Engine.

Monitors:
  - Population Stability Index (PSI) + KS concept drift in real time
  - Rolling Brier score degradation (> 0.22 with ≥ 20 resolved samples)
  - Ground-truth market resolutions & newly captured feature vectors in TimescaleDB
  - Validates candidate models with champion/challenger Brier score comparison
  - Safely gates model promotion with zero-downtime hot-swap, preserving SGD state
    and Brier rolling windows for continuity of adaptive weighting
"""
from __future__ import annotations

import asyncio
import copy
import logging
import random
import time

from ml.drift_detector import BRIER_DRIFT_THRESHOLD, drift_detector
from ml.model import MarketMLModel, ml_model
from ml.model_registry import model_registry

log = logging.getLogger(__name__)

DRIFT_RETRAIN_THRESHOLD = 0.10   # PSI >= 0.10 indicates moderate-to-significant concept drift
CHECK_INTERVAL_SECONDS = 180      # Check drift every 3 minutes
MIN_IMPROVEMENT_RATIO = 0.98     # Challenger must be ≥2% better (Brier lower = better)

# Hyperparameter search space for diverse challenger generation
_RF_MAX_DEPTH_OPTIONS = [6, 7, 8, 9, 10]
_GB_LR_RANGE = (0.05, 0.10)
_RF_N_ESTIMATORS_OPTIONS = [120, 150, 180]
_GB_N_ESTIMATORS_OPTIONS = [80, 100, 120]


class ContinuousTrainingOrchestrator:
    """
    Supervises background model health, drift detection, and autonomous re-training
    with champion/challenger gating.

    Trigger conditions (any one is sufficient):
      1. PSI >= 0.10  — distribution drift
      2. Rolling Brier > 0.22 with >= 20 resolved outcomes  — live calibration failure
      3. 6-hour schedule interval  — routine refresh
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_retrain_time = time.time()
        self._retrain_count = 0
        self._last_champion_brier: float = 1.0

    async def start(self) -> None:
        self._running = True
        self._last_champion_brier = ml_model.brier_score
        self._task = asyncio.create_task(self._orchestrator_loop(), name="ml-training-orchestrator")
        log.info(
            "[training_orchestrator] Continuous Training Orchestrator started "
            "(drift_psi>=%.2f | brier>%.2f | 6h schedule, champion Brier=%.4f)",
            DRIFT_RETRAIN_THRESHOLD, BRIER_DRIFT_THRESHOLD, self._last_champion_brier,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _orchestrator_loop(self) -> None:
        """Periodically evaluate model drift and trigger safe gated re-training."""
        await asyncio.sleep(60)  # Initial warm-up
        while self._running:
            try:
                await self.evaluate_and_retrain_if_needed()
            except Exception as e:
                log.warning("[training_orchestrator] Error during evaluation cycle: %s", e)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def evaluate_and_retrain_if_needed(self) -> bool:
        """
        Check PSI/KS drift, rolling Brier, and schedule — execute champion/challenger
        gated re-training if any trigger fires.

        Challenger diversity: each challenger is trained with randomly sampled
        hyperparameters from the approved search space so champion/challenger
        comparisons reflect genuine model variance, not re-training the same model.

        SGD online state AND Brier rolling windows are transplanted from champion
        to challenger on promotion to preserve all accumulated real-market learning.
        """
        psi = drift_detector.last_psi
        rolling_brier = drift_detector.rolling_brier or 0.0
        n_outcomes = len(drift_detector.recent_actuals)
        time_since_retrain = time.time() - self._last_retrain_time

        # Three independent triggers
        psi_trigger = psi >= DRIFT_RETRAIN_THRESHOLD
        brier_trigger = (rolling_brier > BRIER_DRIFT_THRESHOLD) and (n_outcomes >= 20)
        schedule_trigger = time_since_retrain >= 21600

        should_retrain = psi_trigger or brier_trigger or schedule_trigger

        if not should_retrain:
            return False

        trigger_reason = (
            f"PSI={psi:.4f}" if psi_trigger
            else f"Brier={rolling_brier:.4f}" if brier_trigger
            else f"schedule={time_since_retrain:.0f}s"
        )
        log.info(
            "[training_orchestrator] Retrain trigger: %s. Initiating gated re-training…",
            trigger_reason,
        )

        # Sample diverse hyperparameters for this challenger
        hp = {
            "rf_max_depth": random.choice(_RF_MAX_DEPTH_OPTIONS),
            "gb_learning_rate": round(random.uniform(*_GB_LR_RANGE), 3),
            "n_estimators_rf": random.choice(_RF_N_ESTIMATORS_OPTIONS),
            "n_estimators_gb": random.choice(_GB_N_ESTIMATORS_OPTIONS),
        }
        log.info("[training_orchestrator] Challenger hyperparameters: %s", hp)

        def _train_challenger() -> MarketMLModel:
            """Build and train a fresh candidate model (challenger) with diverse hyperparams."""
            challenger = MarketMLModel()
            challenger.fit_initial(**hp)
            return challenger

        # Run challenger training off the event loop to avoid blocking
        challenger = await asyncio.to_thread(_train_challenger)

        # Champion/challenger comparison — only promote if challenger is meaningfully better
        current_brier = ml_model.brier_score
        challenger_brier = challenger.brier_score

        log.info(
            "[training_orchestrator] Champion Brier=%.4f vs Challenger Brier=%.4f (threshold=%.4f)",
            current_brier, challenger_brier, current_brier * MIN_IMPROVEMENT_RATIO,
        )

        if challenger_brier < current_brier * MIN_IMPROVEMENT_RATIO:
            # PROMOTE: transplant SGD online state + Brier rolling windows
            # to preserve all accumulated real-market learning.
            challenger.sgd = copy.deepcopy(ml_model.sgd)
            challenger._sgd_trained = ml_model._sgd_trained
            challenger._n_updates = ml_model._n_updates
            # Transplant rolling Brier windows so adaptive weights continue from
            # the accumulated history rather than starting cold.
            challenger._rf_brier_window = copy.copy(ml_model._rf_brier_window)
            challenger._gb_brier_window = copy.copy(ml_model._gb_brier_window)
            challenger._sgd_brier_window = copy.copy(ml_model._sgd_brier_window)
            challenger._lgbm_brier_window = copy.copy(ml_model._lgbm_brier_window)

            # Atomic hot-swap into live singleton
            ml_model.__dict__.update(challenger.__dict__)
            challenger.save()

            drift_detector.reset()
            self._last_retrain_time = time.time()
            self._retrain_count += 1
            self._last_champion_brier = challenger_brier

            log.info(
                "[training_orchestrator] ✅ Challenger PROMOTED to champion "
                "(Brier=%.4f → %.4f, ROC-AUC=%.4f, ECE=%.4f, retrain #%d, trigger=%s, hp=%s)",
                current_brier, challenger_brier, ml_model.roc_auc, ml_model.ece,
                self._retrain_count, trigger_reason, hp,
            )
            model_registry.register_version(
                version=f"v{self._retrain_count}.champion",
                brier_score=challenger_brier,
                roc_auc=challenger.roc_auc,
                ece=challenger.ece,
                sharpe_ratio=0.0,
                n_samples=challenger.n_real_samples + challenger.n_synthetic_samples,
                parameters={"retrain_trigger": trigger_reason, "hyperparameters": hp},
            )
            return True
        else:
            log.info(
                "[training_orchestrator] ⏭ Challenger REJECTED — not sufficiently better "
                "(Brier %.4f vs champion %.4f, required < %.4f)",
                challenger_brier, current_brier, current_brier * MIN_IMPROVEMENT_RATIO,
            )
            self._last_retrain_time = time.time()  # reset timer to avoid thrashing
            return False

    @property
    def stats(self) -> dict:
        return {
            "retrain_count": self._retrain_count,
            "last_champion_brier": self._last_champion_brier,
            "seconds_since_retrain": round(time.time() - self._last_retrain_time),
            "drift_threshold_psi": DRIFT_RETRAIN_THRESHOLD,
            "brier_drift_threshold": BRIER_DRIFT_THRESHOLD,
            "min_improvement_ratio": MIN_IMPROVEMENT_RATIO,
            "schedule_hours": 6,
        }


# Global singleton
training_orchestrator = ContinuousTrainingOrchestrator()
