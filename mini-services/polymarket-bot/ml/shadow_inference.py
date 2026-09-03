"""
ml/shadow_inference.py — Shadow Model Challenger Inference Engine.

Lets "challenger" models be evaluated in parallel with the production ML
predictor (`ml.model.MLModel.predict`) WITHOUT affecting any trading
decision. Each registered challenger receives the same feature vector +
token_id + production prediction (p_yes) and returns its own p_yes
estimate. Disagreements are recorded for offline analysis (retraining /
A-B promotion decisions).

Design contract (T13):
  - **Fully additive / opt-in.** Zero impact on the production prediction
    path: every challenger invocation is wrapped in try/except so a
    raising / slow / buggy challenger cannot degrade predict() latency or
    correctness.
  - **Challenger functions are simple callables:** `fn(features) -> float`.
    The production predict() passes the raw `features` array unchanged.
  - **Idempotent registration:** `register_shadow_model(name, fn, ...)` is
    safe to call multiple times from lifespan startup; the same `name`
    overwrites the previous callable.
  - **Bounded memory:** each challenger keeps a `deque(maxlen=500)` ring
    buffer of recent comparisons. Older comparisons age out automatically.
  - **Thread-safe:** all registry mutations are guarded by an internal
    `threading.Lock`. Challenger invocation is performed *outside* the
    lock so a slow challenger cannot block registration / reporting.

This module is a NEW file (T13) — it does not modify any existing source.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict

import numpy as np

log = logging.getLogger(__name__)

# Maximum number of recent shadow comparisons retained in-memory per
# challenger. Bound to keep memory predictable; older entries age out.
_MAX_HISTORY_PER_MODEL = 500


class ShadowInferenceEngine:
    """
    Shadow model challenger registry + comparison engine.

    Singleton instance: `shadow_inference` (instantiated at module bottom).

    The engine is intentionally side-effect-free with respect to the
    production prediction pipeline: every challenger invocation is wrapped
    in try/except so a buggy / raising challenger cannot degrade predict()
    latency or correctness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # name -> {"fn": callable, "description": str, "calls": int,
        #          "history": deque[(dict comparison record)]}
        self._models: Dict[str, Dict[str, Any]] = {}
        # Aggregate counters
        self.total_calls: int = 0
        self.total_errors: int = 0
        self.registered_at: float | None = None

    # ── Registration ────────────────────────────────────────────────────────
    def register_shadow_model(
        self,
        name: str,
        fn: Callable[..., float],
        description: str | None = None,
    ) -> None:
        """
        Register (or replace) a shadow challenger model.

        Idempotent: re-registering the same `name` overwrites the previous
        callable. The challenger `fn` is invoked as `fn(features)` and is
        expected to return a float in [0, 1] (the challenger's p_yes).

        Parameters
        ----------
        name : str
            Unique challenger identifier (e.g. ``"logistic_baseline"``).
        fn : callable
            Challenger prediction function: ``fn(features) -> float``.
        description : str, optional
            Human-readable description surfaced in status reports.
        """
        if not name or not callable(fn):
            log.debug("[shadow_inference] rejected registration name=%r", name)
            return
        with self._lock:
            self._models[name] = {
                "fn": fn,
                "description": description or "",
                "calls": 0,
                "history": deque(maxlen=_MAX_HISTORY_PER_MODEL),
            }
            if self.registered_at is None:
                self.registered_at = time.time()
            log.info(
                "[shadow_inference] registered shadow model %r (%s) — "
                "%d challenger(s) active",
                name,
                description or "no description",
                len(self._models),
            )

    def unregister_shadow_model(self, name: str) -> bool:
        """Remove a registered shadow challenger. Returns True if removed."""
        with self._lock:
            return self._models.pop(name, None) is not None

    @property
    def registered_models(self) -> list[str]:
        """List of currently-registered challenger names (snapshot)."""
        with self._lock:
            return list(self._models.keys())

    # ── Shadow inference ───────────────────────────────────────────────────
    def run_shadow(
        self,
        features: Any,
        token_id: str,
        p_yes: float,
    ) -> None:
        """
        Invoke every registered challenger with `features` and record its
        p_yes estimate alongside the production model's `p_yes`.

        Side-effect free with respect to callers: never raises. A challenger
        that raises is logged at DEBUG and skipped (per-challenger error
        counter incremented).

        Parameters
        ----------
        features : array-like
            The same feature vector passed to the production predict() call.
            Challengers receive it unchanged.
        token_id : str
            Market token identifier (used for attribution in the history
            ring buffer).
        p_yes : float
            Production model's P(YES) prediction (already clipped to
            [0.01, 0.99] by the production path).
        """
        # Snapshot of registered challengers to avoid holding the lock
        # during challenger invocation (which may be slow / non-deterministic).
        with self._lock:
            challengers = [(name, entry) for name, entry in self._models.items()]
        if not challengers:
            return  # fast-path: nothing to do

        ts = time.time()
        for name, entry in challengers:
            try:
                p_shadow = float(entry["fn"](features))
            except Exception as e:  # noqa: BLE001 — challengers are untrusted
                self.total_errors += 1
                log.debug(
                    "[shadow_inference] challenger %r raised: %s (token=%s)",
                    name, e, token_id,
                )
                continue

            self.total_calls += 1
            # Clip challenger output to the same [0.01, 0.99] band as the
            # production model — keeps comparison metrics sane.
            p_shadow = max(0.01, min(0.99, p_shadow))
            delta = abs(p_shadow - float(p_yes))
            with self._lock:
                entry["calls"] += 1
                entry["history"].append({
                    "ts": ts,
                    "token_id": token_id,
                    "p_production": round(float(p_yes), 4),
                    "p_shadow": round(p_shadow, 4),
                    "abs_delta": round(delta, 4),
                })

    # ── Reporting ──────────────────────────────────────────────────────────
    def get_status_report(self) -> dict[str, Any]:
        """
        Snapshot of the shadow-inference registry for observability.

        Each registered challenger reports its call count, the rolling
        mean absolute disagreement vs. the production model over its
        history window, and its most recent comparison record. This is
        the surface a future ``/api/shadow-inference`` endpoint would
        expose — kept self-contained here so the production predict()
        path can be exercised without depending on it.
        """
        with self._lock:
            models = []
            for name, entry in self._models.items():
                history = list(entry["history"])
                if history:
                    mean_delta = float(np.mean([h["abs_delta"] for h in history]))
                else:
                    mean_delta = 0.0
                models.append({
                    "name": name,
                    "description": entry["description"],
                    "calls": entry["calls"],
                    "mean_abs_delta_vs_production": round(mean_delta, 4),
                    "last_comparison": history[-1] if history else None,
                })
            return {
                "registered_models": models,
                "total_calls": self.total_calls,
                "total_errors": self.total_errors,
                "registered_at": self.registered_at,
                "max_history_per_model": _MAX_HISTORY_PER_MODEL,
            }


# Global singleton — mirrors the pattern used by `drift_detector`,
# `audit_logger`, `closed_positions`, `execution_quality`, etc.
shadow_inference = ShadowInferenceEngine()
