"""
strategies/ml_xgboost_directional.py — XGBoost Directional Trader.

W45-1 — implements the unified strategy contract for the
``ml_xgboost_directional`` catalog entry.

Signal logic
------------
Regularized gradient boosting model on order flow & volume dynamics.
The XGBoost model predicts next-cycle price direction (UP/DOWN/FLAT)
and produces a calibrated P(UP) probability:

  * P(UP) > 0.65 AND edge > MIN_EDGE → BUY
  * P(UP) < 0.35 AND edge > MIN_EDGE → SELL
  * Otherwise → no signal (low-conviction regime)

The strategy falls back to a logistic surrogate when the XGBoost
model isn't loaded (cold-start / testing environment).
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from strategies.base import BaseStrategy, Signal

log = logging.getLogger(__name__)

MIN_EDGE = 0.04
BUY_THRESHOLD = 0.65
SELL_THRESHOLD = 0.35
MODEL_PATH_DEFAULT = "data/models/xgboost_directional.joblib"
SCAN_INTERVAL = 30.0


class XGBoostDirectional(BaseStrategy):
    """XGBoost directional model trader."""

    name = "ml_xgboost_directional"

    def __init__(self) -> None:
        super().__init__()
        self.min_edge: float = MIN_EDGE
        self.buy_threshold: float = BUY_THRESHOLD
        self.sell_threshold: float = SELL_THRESHOLD
        self.model_path: str = MODEL_PATH_DEFAULT
        self._model = None
        self._interval: float = SCAN_INTERVAL
        self._prediction_cache: dict[str, float] = {}

    async def _run(self) -> None:
        log.info(
            "[ml_xgboost] Active (min_edge=%.0f%%, buy>=%.2f, sell<=%.2f)",
            self.min_edge * 100, self.buy_threshold, self.sell_threshold,
        )
        while self._running:
            try:
                self._ensure_model_loaded()
                await asyncio.sleep(self._interval)
            except Exception as e:
                log.error("[ml_xgboost] Cycle error: %s", e)
                self._last_error = str(e)
                self._stats["errors"] = self._stats.get("errors", 0) + 1

    def metadata(self) -> dict:
        return {
            "name": self.name,
            "version": "1.0.0",
            "description": (
                "XGBoost directional trader — regularized gradient boosting "
                "on order flow & volume dynamics with calibrated P(UP)."
            ),
            "author": "polymarket-bot",
            "category": "machine_learning",
            "model": "xgboost_directional",
        }

    def configure(self, config: dict) -> None:
        super().configure(config)
        for k in ("min_edge", "buy_threshold", "sell_threshold"):
            if k in config:
                setattr(self, k, float(config[k]))
        if "model_path" in config:
            self.model_path = str(config["model_path"])
        if "scan_interval" in config:
            self._interval = float(config["scan_interval"])

    def validate(self) -> tuple[bool, str]:
        if self.min_edge <= 0:
            return False, "min_edge must be > 0"
        if not 0.5 < self.buy_threshold < 1:
            return False, "buy_threshold must be in (0.5, 1)"
        if not 0 < self.sell_threshold < 0.5:
            return False, "sell_threshold must be in (0, 0.5)"
        return True, "OK"

    def _ensure_model_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            if not os.path.exists(self.model_path):
                return
            import joblib
            self._model = joblib.load(self.model_path)
            log.info("[ml_xgboost] Loaded model from %s", self.model_path)
        except Exception as e:
            log.debug("[ml_xgboost] Model load failed: %s", e)
            self._model = None

    def _surrogate_predict(self, features: dict) -> float:
        """Hand-coded surrogate when model isn't loaded."""
        ofi = float(features.get("ofi_1m", 0.0))
        ret_5m = float(features.get("return_5m", 0.0))
        vol = float(features.get("volume_5m", 0.0))
        # Logistic-style: combine features and squash via sigmoid.
        score = 0.5 + 1.5 * ofi + 2.0 * ret_5m + 0.0001 * vol
        p_up = 1.0 / (1.0 + pow(2.71828, -score))
        return max(0.01, min(0.99, p_up))

    def _predict(self, features: dict) -> float:
        token_id = features.get("token_id", "")
        cached = self._prediction_cache.get(token_id)
        if cached is not None:
            return cached
        if self._model is None:
            p_up = self._surrogate_predict(features)
        else:
            try:
                import numpy as np
                fv = np.array([[
                    float(features.get(k, 0.0))
                    for k in ("mid", "ofi_1m", "return_1m", "return_5m", "volume_5m")
                ]])
                proba = self._model.predict_proba(fv)[0]
                p_up = float(proba[1] if len(proba) > 1 else proba[0])
            except Exception as e:
                log.debug("[ml_xgboost] predict failed, using surrogate: %s", e)
                p_up = self._surrogate_predict(features)
        self._prediction_cache[token_id] = p_up
        return p_up

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

        p_up = self._predict(market_context)
        # Confidence = distance from 0.5.
        confidence = abs(p_up - 0.5) * 2.0

        if p_up > self.buy_threshold:
            action = "BUY"
            edge = p_up - mid_f
            target_price = round(min(mid_f + 0.005, 0.98), 4)
            reason = (
                f"XGBoost BUY: P(UP)={p_up:.3f} > {self.buy_threshold} "
                f"(edge={edge*100:+.2f}%, conf={confidence:.2f})"
            )
        elif p_up < self.sell_threshold:
            action = "SELL"
            edge = mid_f - (1 - p_up)
            target_price = round(max(mid_f - 0.005, 0.02), 4)
            reason = (
                f"XGBoost SELL: P(UP)={p_up:.3f} < {self.sell_threshold} "
                f"(edge={edge*100:+.2f}%, conf={confidence:.2f})"
            )
        else:
            return None

        if abs(edge) < self.min_edge:
            return None

        self._stats["signals"] = self._stats.get("signals", 0) + 1
        return Signal(
            action=action,
            token_id=token_id,
            size=1.0,
            price=target_price,
            confidence=confidence,
            edge=abs(edge),
            reason=reason,
            metadata={
                "model": "xgboost_directional",
                "predicted_p_up": p_up,
                "market_mid": mid_f,
                "edge": edge,
                "confidence": confidence,
                "model_loaded": self._model is not None,
            },
        )

    def estimate_edge(self, signal: Signal) -> float:
        return signal.edge if signal is not None else 0.0

    def size_position(self, signal: Signal, capital: float, risk_params: dict) -> float:
        if signal is None or signal.action == "HOLD":
            return 0.0
        edge = abs(float(signal.metadata.get("edge", 0.0)))
        conf = float(signal.metadata.get("confidence", 0.5))
        size_factor = min(2.5, edge * 10 + conf * 0.5)
        base_size = float(risk_params.get("base_size_usdc", 2.0))
        max_pct = float(risk_params.get("max_position_pct", 0.03))
        return min(base_size * size_factor, max_pct * capital, capital)

    def entry_logic(self, signal: Signal, market_context: dict) -> dict:
        if signal is None or signal.action == "HOLD":
            return {"skip": True, "reason": "no ML signal"}
        return {
            "token_id": signal.token_id,
            "price": signal.price,
            "side": signal.action,
            "type": "limit",
            "time_in_force": "GTC",
            "post_only": False,
            "metadata": {
                "model": "xgboost_directional",
                "predicted_p_up": signal.metadata.get("predicted_p_up"),
                "edge": signal.metadata.get("edge"),
            },
        }

    def exit_logic(self, position: dict, market_context: dict) -> Optional[dict]:
        if not position:
            return None
        current_p_up = float(market_context.get("current_p_up", 0.5))
        # Exit when P(UP) crosses back into the neutral zone.
        if self.sell_threshold <= current_p_up <= self.buy_threshold:
            return {
                "reason": "P(UP) reverted to neutral — exit",
                "current_p_up": current_p_up,
                "type": "market",
            }
        return None

    def diagnostics(self) -> dict:
        base = super().diagnostics()
        base.update({
            "min_edge": self.min_edge,
            "buy_threshold": self.buy_threshold,
            "sell_threshold": self.sell_threshold,
            "model_loaded": self._model is not None,
            "prediction_cache_size": len(self._prediction_cache),
        })
        return base
