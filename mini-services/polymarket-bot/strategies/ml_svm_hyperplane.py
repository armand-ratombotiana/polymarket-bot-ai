"""
strategies/ml_svm_hyperplane.py — SVM Hyperplane Classifier Trader.

W45-1 — implements the unified strategy contract for the
``ml_svm_hyperplane`` catalog entry.

Signal logic
------------
Non-linear RBF kernel Support Vector Machine classifier that learns
the optimal hyperplane separating "price-up" from "price-down"
market states.

The strategy:
  * Extracts a feature vector from market_context.
  * Predicts the price-up vs price-down label.
  * Uses the signed distance to the hyperplane as the confidence.
  * Fires BUY when label=UP and distance > min_distance.
  * Fires SELL when label=DOWN and distance > min_distance.

Falls back to a linear surrogate when the RBF SVM isn't loaded.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_DISTANCE = 0.5                # min hyperplane distance required
MODEL_PATH_DEFAULT = "data/models/svm_hyperplane.joblib"
FEATURE_KEYS = ("ofi_1m", "return_1m", "return_5m", "volatility_1m", "volume_ratio")
SCAN_INTERVAL = 30.0


class SvmHyperplaneClassifier(BaseStrategy):
    """RBF-kernel SVM hyperplane classifier trader."""

    name = "ml_svm_hyperplane"

    def __init__(self) -> None:
        super().__init__()
        self.min_distance: float = MIN_DISTANCE
        self.model_path: str = MODEL_PATH_DEFAULT
        self._model = None
        self._weights: list[float] = [0.0] * len(FEATURE_KEYS)
        self._bias: float = 0.0
        self._interval: float = SCAN_INTERVAL
        self._prediction_cache: dict[str, tuple[int, float]] = {}

    async def _run(self) -> None:
        log.info(
            "[ml_svm] Active (min_dist=%.2f, features=%d)",
            self.min_distance, len(FEATURE_KEYS),
        )
        while self._running:
            try:
                self._ensure_model_loaded()
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_svm] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "SVM hyperplane classifier — non-linear RBF kernel "
                "hyperplane separator for market state classification "
                "(price-up vs price-down)."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "svm_hyperplane",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        if "min_distance" in config:
            self.min_distance = float(config["min_distance"])
        if "model_path" in config:
            self.model_path = str(config["model_path"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_distance <= 0:
            return False, "min_distance must be > 0"
        if not self.model_path:
            return False, "model_path must be non-empty"
        return True, "OK"

    def _ensure_model_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            if not os.path.exists(self.model_path):
                return
            import joblib
            self._model = joblib.load(self.model_path)
            log.info("[ml_svm] Loaded model from %s", self.model_path)
        except Exception as e:
            log.debug("[ml_svm] Model load failed: %s", e)
            self._model = None

    def _extract_features(self, market_context: dict) -> list[float]:
        return [float(market_context.get(k, 0.0)) for k in FEATURE_KEYS]

    def _surrogate_predict(self, features: list[float]) -> tuple[int, float]:
        """Linear surrogate: signed distance to a learned hyperplane."""
        score = self._bias + sum(w * x for w, x in zip(self._weights, features))
        label = 1 if score > 0 else -1
        distance = abs(score)
        return label, distance

    def _predict(self, features: list[float], token_id: str) -> tuple[int, float]:
        cached = self._prediction_cache.get(token_id)
        if cached is not None:
            return cached
        if self._model is None:
            label, distance = self._surrogate_predict(features)
        else:
            try:
                import numpy as np
                fv = np.array([features])
                label = int(self._model.predict(fv)[0])
                # RBF SVM decision_function returns signed distance.
                if hasattr(self._model, "decision_function"):
                    distance = abs(float(self._model.decision_function(fv)[0]))
                else:
                    distance = 1.0
            except Exception as e:
                log.debug("[ml_svm] predict failed, using surrogate: %s", e)
                label, distance = self._surrogate_predict(features)
        self._prediction_cache[token_id] = (label, distance)
        return label, distance

    def generate_signal(self, market_context: dict) -> Optional[Signal]:
        token_id = market_context.get("token_id")
        mid = market_context.get("mid")
        if not token_id or mid is None:
            return None
        try:
            mid_f = float(mid)
        except (TypeError, ValueError):
            return None
        if not 0 < mid_f < 1:
            return None

        features = self._extract_features(market_context)
        label, distance = self._predict(features, token_id)

        if distance < self.min_distance:
            return None  # too close to hyperplane — low confidence

        if label == 1:
            action = "BUY"
            target_price = round(min(mid_f + 0.005, 0.98), 4)
            reason = (
                f"SVM BUY: label=UP, distance={distance:.2f} > {self.min_distance}"
            )
        else:
            action = "SELL"
            target_price = round(max(mid_f - 0.005, 0.02), 4)
            reason = (
                f"SVM SELL: label=DOWN, distance={distance:.2f} > {self.min_distance}"
            )

        edge = min(distance / 10.0, 0.04)
        confidence = min(0.85, 0.4 + distance / 5.0)

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=target_price,
            confidence=confidence,
            edge=edge,
            reason=reason,
            metadata={
                "model": "svm_hyperplane",
                "label": label,
                "distance": distance,
                "feature_values": features,
                "feature_keys": list(FEATURE_KEYS),
                "model_loaded": self._model is not None,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        distance = float(signal.metadata.get("distance", 0.0))
        size_factor = min(2.5, distance / 2.0)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no SVM signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "svm_hyperplane",
                "label": signal.metadata.get("label"),
                "distance": signal.metadata.get("distance"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        """Exit when label flips OR distance collapses below threshold."""
        if not position:
            return None
        current_label = int(market_context.get("current_label", 0))
        current_distance = float(market_context.get("current_distance", 0.0))
        entry_action = position.get("entry_action", "BUY")
        # Exit when label flips.
        if entry_action == "BUY" and current_label == -1:
            return {
                "reason": "SVM label flipped to DOWN — exit long",
                "current_label": current_label,
                "type": "market",
            }
        if entry_action == "SELL" and current_label == 1:
            return {
                "reason": "SVM label flipped to UP — exit short",
                "current_label": current_label,
                "type": "market",
            }
        # Exit when distance collapses (low confidence).
        if current_distance < self.min_distance * 0.5:
            return {
                "reason": "SVM distance collapsed — exit",
                "current_distance": current_distance,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_distance": self.min_distance,
            "model_loaded": self._model is not None,
            "weights": list(self._weights),
            "bias": self._bias,
            "prediction_cache_size": len(self._prediction_cache),
        })
        return base
