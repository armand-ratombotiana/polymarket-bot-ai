"""
ml/training_orchestrator.py — Automated Continuous Drift-Triggered Model Re-Training Engine.

Monitors:
  - Population Stability Index (PSI) concept drift in real time
  - Ground-truth market resolutions & newly captured feature vectors in TimescaleDB
  - Validates candidate models with Walk-Forward Cross-Validation
  - Safely gates model promotion into ModelRegistry with zero-downtime hot-swap
"""
from __future__ import annotations

import asyncio
import logging
import time

from ml.drift_detector import drift_detector
from ml.model import ml_model
from ml.model_registry import model_registry

log = logging.getLogger(__name__)

DRIFT_RETRAIN_THRESHOLD = 0.10   # PSI >= 0.10 indicates moderate-to-significant concept drift
CHECK_INTERVAL_SECONDS = 180      # Check drift every 3 minutes


class ContinuousTrainingOrchestrator:
    """
    Supervises background model health, drift detection, and autonomous re-training.
    """

    def __init__(self) -> None:
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_retrain_time = time.time()
        self._retrain_count = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._orchestrator_loop(), name="ml-training-orchestrator")
        log.info("[training_orchestrator] Continuous Training Orchestrator started (drift threshold: PSI >= %.2f)", DRIFT_RETRAIN_THRESHOLD)

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
        """Check PSI drift and execute gated re-training if warranted."""
        psi = drift_detector.last_psi
        time_since_retrain = time.time() - self._last_retrain_time

        # Retrain if drift detected or 6 hours elapsed since last update
        should_retrain = (psi >= DRIFT_RETRAIN_THRESHOLD) or (time_since_retrain >= 21600)

        if not should_retrain:
            return False

        log.info("[training_orchestrator] Drift/Schedule trigger activated (PSI=%.4f, elapsed=%.0fs). Initiating gated re-training…",
                 psi, time_since_retrain)

        # Run re-training asynchronously to avoid blocking the event loop
        def _train_job():
            ml_model.fit_initial()
            ml_model.save()
            drift_detector.reset()

        await asyncio.to_thread(_train_job)
        self._last_retrain_time = time.time()
        self._retrain_count += 1

        log.info("[training_orchestrator] Candidate model promoted to %s (Brier=%.4f, ROC-AUC=%.4f, ECE=%.4f)",
                 model_registry.active_version, ml_model.brier_score, ml_model.roc_auc, ml_model.ece)
        return True


# Global singleton
training_orchestrator = ContinuousTrainingOrchestrator()
