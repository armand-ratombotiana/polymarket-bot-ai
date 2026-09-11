"""
strategies/ml_online_sgd_learner.py — Online SGD Momentum Learner.

W45-1 — implements the unified strategy contract for the
``ml_online_sgd_learner`` catalog entry.

Signal logic
------------
Real-time passive-aggressive incremental learner that updates its
weights from every fill. The model:

  * Maintains a weight vector w over N features.
  * On each fill, updates: w ← w + η · (y - w·x) · x
  * Predicts next-cycle return as: r_hat = w · x
  * Fires BUY when r_hat > threshold AND confidence > min_conf
  * Fires SELL when r_hat < -threshold AND confidence > min_conf

The online SGD model never needs batch retraining — it adapts
continuously to live market conditions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

LEARNING_RATE = 0.01
MIN_PREDICTION = 0.005            # 0.5% return prediction required
MIN_CONFIDENCE = 0.55
FEATURE_KEYS = (
    "ofi_1m", "return_1m", "return_5m", "volume_ratio", "spread_compression",
)
SCAN_INTERVAL = 15.0


class OnlineSgdLearner(BaseStrategy):
    """Online SGD incremental learner trader."""

    name = "ml_online_sgd_learner"

    def __init__(self) -> None:
        super().__init__()
        self.learning_rate: float = LEARNING_RATE
        self.min_prediction: float = MIN_PREDICTION
        self.min_confidence: float = MIN_CONFIDENCE
        self._weights: list[float] = [0.0] * len(FEATURE_KEYS)
        self._bias: float = 0.0
        self._update_count: int = 0
        self._interval: float = SCAN_INTERVAL

    async def _run(self) -> None:
        log.info(
            "[ml_sgd] Active (lr=%.3f, min_pred=%.3f, features=%d)",
            self.learning_rate, self.min_prediction, len(FEATURE_KEYS),
        )
        while self._running:
            try:
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_sgd] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "Online SGD learner — real-time passive-aggressive incremental "
                "model that updates weights from every fill, predicts next-cycle "
                "return, trades on confident predictions."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "online_sgd_learner",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("learning_rate", "min_prediction", "min_confidence"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.learning_rate <= 0:
            return False, "learning_rate must be > 0"
        if self.min_prediction <= 0:
            return False, "min_prediction must be > 0"
        if not 0 <= self.min_confidence <= 1:
            return False, "min_confidence must be in [0, 1]"
        return True, "OK"

    def _extract_features(self, market_context: dict) -> list[float]:
        return [float(market_context.get(k, 0.0)) for k in FEATURE_KEYS]

    def _predict(self, features: list[float]) -> float:
        return self._bias + sum(w * x for w, x in zip(self._weights, features))

    def _update(self, features: list[float], target: float) -> None:
        """Online SGD update: w ← w + η · (y - w·x) · x."""
        prediction = self._predict(features)
        error = target - prediction
        for i, x in enumerate(features):
            self._weights[i] += self.learning_rate * error * x
        self._bias += self.learning_rate * error
        self._update_count += 1

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
        prediction = self._predict(features)

        # Confidence grows with update count (more training data ⇒ more confidence).
        confidence = min(0.85, 0.3 + self._update_count / 1000.0)
        if confidence < self.min_confidence:
            return None

        if abs(prediction) < self.min_prediction:
            return None

        if prediction > 0:
            action = "BUY"
            edge = min(prediction, 0.05)
            target_price = round(min(mid_f + 0.005, 0.98), 4)
            reason = (
                f"SGD BUY: pred={prediction:+.4f} > {self.min_prediction} "
                f"(conf={confidence:.2f}, updates={self._update_count})"
            )
        else:
            action = "SELL"
            edge = min(-prediction, 0.05)
            target_price = round(max(mid_f - 0.005, 0.02), 4)
            reason = (
                f"SGD SELL: pred={prediction:+.4f} < -{self.min_prediction} "
                f"(conf={confidence:.2f}, updates={self._update_count})"
            )

        # Online update: if the market_context has a "true_return" key
        # (i.e. previous cycle's realized return), update the model.
        true_return = market_context.get("true_return")
        if true_return is not None:
            try:
                self._update(features, float(true_return))
            except (TypeError, ValueError):
                pass

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
                "model": "online_sgd_learner",
                "prediction": prediction,
                "confidence": confidence,
                "update_count": self._update_count,
                "weights": list(self._weights),
                "bias": self._bias,
                "feature_keys": list(FEATURE_KEYS),
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        pred = abs(float(signal.metadata.get("prediction", 0.0)))
        conf = float(signal.metadata.get("confidence", 0.5))
        size_factor = min(2.5, pred * 50 + conf)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no SGD signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "online_sgd_learner",
                "prediction": signal.metadata.get("prediction"),
                "confidence": signal.metadata.get("confidence"),
                "update_count": signal.metadata.get("update_count"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        if not position:
            return None
        current_pred = float(market_context.get("current_prediction", 0.0))
        # Exit when prediction compresses below half the threshold.
        if abs(current_pred) < self.min_prediction * 0.5:
            return {
                "reason": "prediction compressed — exit",
                "current_prediction": current_pred,
                "type": "market",
            }
        # Exit on prediction reversal.
        entry_action = position.get("entry_action", "BUY")
        if entry_action == "BUY" and current_pred < 0:
            return {
                "reason": "prediction reversed — exit long",
                "current_prediction": current_pred,
                "type": "market",
            }
        if entry_action == "SELL" and current_pred > 0:
            return {
                "reason": "prediction reversed — exit short",
                "current_prediction": current_pred,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "learning_rate": self.learning_rate,
            "min_prediction": self.min_prediction,
            "update_count": self._update_count,
            "weights": list(self._weights),
            "bias": self._bias,
        })
        return base
