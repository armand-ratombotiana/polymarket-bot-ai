"""
ml/shadow_inference.py — Shadow Model Challenger Inference Engine.

Lets "challenger" models be evaluated in parallel with the production ML
predictor (`ml.model.MLModel.predict`) WITHOUT affecting any trading
decision. Each registered challenger receives the same feature vector +
token_id + production prediction (p_yes) and returns its own p_yes
estimate. Disagreements are recorded for offline analysis (retraining /
A-B promotion decisions).

Design contract (T13 / W19-8):
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
  - **W19-8 — Promotion gate.** `record_outcome(token_id, outcome_yes)`
    back-fills ground-truth labels onto the comparison rows so
    `evaluate_and_promote(min_samples, alpha)` can compute per-challenger
    Brier scores and run a paired t-test against the champion. A
    significantly-better challenger is flagged
    ``is_significantly_better=True`` and ``_promote(name)`` records the
    promotion decision in the engine's promotion ledger (surfaced via
    ``/api/ml/shadow`` + ``/api/ml/shadow/evaluate``).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

import numpy as np

log = logging.getLogger(__name__)

# Maximum number of recent shadow comparisons retained in-memory per
# challenger. Bound to keep memory predictable; older entries age out.
_MAX_HISTORY_PER_MODEL = 500

# W19-8 — promotion-gate defaults. ``evaluate_and_promote`` requires at
# least this many outcome-stamped comparison rows before it will flag a
# challenger as ``is_significantly_better`` (avoids promoting on noise).
_DEFAULT_MIN_PROMOTION_SAMPLES = 30
_DEFAULT_PROMOTION_ALPHA = 0.05


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
        # W19-8 — promotion ledger. Maps challenger name -> most-recent
        # promotion decision dict (``{"promoted_at": float, "verdict": dict}``)
        # so ``/api/ml/shadow`` can surface which challengers have been
        # flagged for promotion and when.
        self._promotions: Dict[str, Dict[str, Any]] = {}

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
    def predict_all(self, features: Any) -> Dict[str, float]:
        """
        Invoke every registered challenger with ``features`` and return
        a ``{name: p_yes}`` dict.

        W19-8 — split out from ``run_shadow`` so the caller (production
        ``predict()``) can place the champion prediction + shadow
        predictions on the same logical record without re-invoking any
        challenger. Challengers that raise are skipped (per-challenger
        ``total_errors`` is incremented) and omitted from the returned
        dict.

        Side-effect free with respect to the caller: never raises. A
        challenger that raises is logged at DEBUG and skipped.

        Parameters
        ----------
        features : array-like
            The same feature vector passed to the production predict() call.
            Challengers receive it unchanged.

        Returns
        -------
        dict[str, float]
            ``{challenger_name: p_yes}`` for every challenger that
            returned a finite float. Each p_yes is clipped to ``[0.01,
            0.99]`` to match the production model's output band. Empty
            dict when no challengers are registered.
        """
        # Snapshot of registered challengers to avoid holding the lock
        # during challenger invocation (which may be slow / non-deterministic).
        with self._lock:
            challengers = [(name, entry) for name, entry in self._models.items()]
        if not challengers:
            return {}

        preds: Dict[str, float] = {}
        for name, entry in challengers:
            try:
                p = float(entry["fn"](features))
            except Exception as e:  # noqa: BLE001 — challengers are untrusted
                self.total_errors += 1
                log.debug(
                    "[shadow_inference] challenger %r raised: %s",
                    name, e,
                )
                continue
            # Clip challenger output to the same [0.01, 0.99] band as the
            # production model — keeps comparison metrics sane.
            p = max(0.01, min(0.99, p))
            preds[name] = p
        return preds

    def record_predictions(
        self,
        token_id: str,
        champion_pred: float,
        shadow_preds: Dict[str, float],
    ) -> None:
        """
        Record a comparison row for each shadow prediction.

        W19-8 — companion to ``predict_all``. Appends a comparison record
        to each challenger's history ring buffer + bumps per-challenger
        ``calls`` and the aggregate ``total_calls`` counter. Challengers
        named in ``shadow_preds`` that are no longer registered are
        silently skipped (defensive — registration state can change
        between ``predict_all`` and ``record_predictions``).

        Parameters
        ----------
        token_id : str
            Market token identifier (used for attribution in the
            history ring buffer + for outcome back-fill via
            ``record_outcome``).
        champion_pred : float
            Production model's P(YES) prediction (already clipped to
            [0.01, 0.99] by the production path).
        shadow_preds : dict[str, float]
            Output of ``predict_all`` — ``{challenger_name: p_yes}``.
            Empty dict is a no-op (defensive — caller does not need to
            guard).
        """
        if not shadow_preds:
            return
        ts = time.time()
        with self._lock:
            for name, p_shadow in shadow_preds.items():
                entry = self._models.get(name)
                if entry is None:
                    continue
                self.total_calls += 1
                entry["calls"] += 1
                delta = abs(float(p_shadow) - float(champion_pred))
                entry["history"].append({
                    "ts": ts,
                    "token_id": token_id,
                    "p_production": round(float(champion_pred), 4),
                    "p_shadow": round(float(p_shadow), 4),
                    "abs_delta": round(delta, 4),
                    # W19-8 — outcome column. ``None`` until
                    # ``record_outcome(token_id, outcome_yes)`` back-fills
                    # the realized YES/NO label. ``evaluate_and_promote``
                    # only considers rows where ``outcome`` is non-None.
                    "outcome": None,
                })

    def run_shadow(
        self,
        features: Any,
        token_id: str,
        p_yes: float,
    ) -> None:
        """
        Convenience: invoke every registered challenger with `features`
        and record its p_yes estimate alongside the production model's
        `p_yes`.

        Composed of ``predict_all`` + ``record_predictions`` so the
        contract is identical to calling those two methods in sequence.
        Kept for backwards compatibility with the T13 wiring
        (``ml/model.py::predict`` historical call site, integration tests).

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
        preds = self.predict_all(features)
        if preds:
            self.record_predictions(token_id, p_yes, preds)

    # ── Outcome back-fill (W19-8) ───────────────────────────────────────────
    def record_outcome(self, token_id: str, outcome_yes: bool) -> int:
        """
        Back-fill a realized YES/NO outcome onto every comparison row
        matching ``token_id`` that does not already have an outcome.

        W19-8 — called by the live settlement path (or a label-backfill
        job) once a market resolves. The stamped ``outcome`` field is
        what ``evaluate_and_promote`` consumes to compute per-challenger
        Brier scores vs the champion.

        Only rows whose ``outcome`` is currently ``None`` are updated —
        idempotent across re-invocations (a re-broadcast of the same
        resolution will not double-count or overwrite a previously-stamped
        outcome).

        Parameters
        ----------
        token_id : str
            Market token identifier whose market just resolved.
        outcome_yes : bool
            ``True`` if the market resolved YES, ``False`` if NO.

        Returns
        -------
        int
            Number of comparison rows newly stamped with the outcome
            (across all challengers). Zero when ``token_id`` has no
            pending comparison rows.
        """
        outcome_int = 1 if outcome_yes else 0
        n_updated = 0
        with self._lock:
            for entry in self._models.values():
                for row in entry["history"]:
                    if row.get("token_id") == token_id and row.get("outcome") is None:
                        row["outcome"] = outcome_int
                        n_updated += 1
        if n_updated:
            log.info(
                "[shadow_inference] stamped %d comparison rows with "
                "outcome=%s for token=%s",
                n_updated, "YES" if outcome_yes else "NO", token_id,
            )
        return n_updated

    # ── Promotion gate (W19-8) ──────────────────────────────────────────────
    def evaluate_and_promote(
        self,
        min_samples: int = _DEFAULT_MIN_PROMOTION_SAMPLES,
        alpha: float = _DEFAULT_PROMOTION_ALPHA,
    ) -> Dict[str, Dict[str, Any]]:
        """
        Evaluate every shadow challenger with ≥ ``min_samples``
        outcome-stamped comparison rows and flag significantly-better
        challengers for promotion.

        For each qualifying challenger:

          1. Collect every comparison row with ``outcome`` not ``None``.
          2. Compute per-row Brier scores for the champion
             (``(p_production - outcome) ** 2``) and the challenger
             (``(p_shadow - outcome) ** 2``).
          3. Run a two-sided paired t-test on the per-row Brier
             difference (``scipy.stats.ttest_rel``) — paired because the
             same set of tokens contributes to both arms.
          4. Mark ``is_significantly_better=True`` when the challenger's
             mean Brier is LOWER than the champion's AND the t-test
             p-value is below ``alpha``.
          5. When ``is_significantly_better`` is True, call
             ``_promote(name)`` to record the promotion in the engine's
             ``_promotions`` ledger.

        Returns
        -------
        dict[str, dict]
            ``{challenger_name: comparison_dict}`` for every challenger
            that had ≥ ``min_samples`` outcome-stamped rows.
            ``comparison_dict`` carries ``n_samples``,
            ``champion_brier``, ``challenger_brier``,
            ``brier_improvement``, ``t_statistic``, ``p_value``,
            ``is_significantly_better``, and ``promoted`` (bool — True
            when ``_promote`` was invoked this call). Challengers with
            insufficient data are omitted from the returned dict (a
            separate ``insufficient_data`` summary is logged).
        """
        try:
            from scipy import stats as _stats
        except ImportError:  # pragma: no cover — scipy is a hard dep
            log.warning(
                "[shadow_inference] scipy unavailable — evaluate_and_promote "
                "cannot run significance tests"
            )
            return {}

        results: Dict[str, Dict[str, Any]] = {}
        with self._lock:
            # Snapshot the histories so the (potentially slow) t-test
            # computation runs OUTSIDE the lock — keeps registration /
            # reporting responsive while evaluate_and_promote runs.
            snapshot = [
                (name, [dict(r) for r in entry["history"]])
                for name, entry in self._models.items()
            ]

        insufficient: list[str] = []
        for name, history in snapshot:
            stamped = [r for r in history if r.get("outcome") is not None]
            if len(stamped) < min_samples:
                insufficient.append(name)
                continue

            champ_briers = np.array(
                [(float(r["p_production"]) - float(r["outcome"])) ** 2 for r in stamped],
                dtype=np.float64,
            )
            chall_briers = np.array(
                [(float(r["p_shadow"]) - float(r["outcome"])) ** 2 for r in stamped],
                dtype=np.float64,
            )
            champ_mean = float(np.mean(champ_briers))
            chall_mean = float(np.mean(chall_briers))
            brier_improvement = champ_mean - chall_mean  # >0 means challenger better

            try:
                t_stat, p_value = _stats.ttest_rel(chall_briers, champ_briers)
                t_stat = float(t_stat)
                p_value = float(p_value)
            except Exception as e:  # noqa: BLE001 — defensive
                log.debug(
                    "[shadow_inference] ttest_rel failed for %r: %s",
                    name, e,
                )
                t_stat, p_value = float("nan"), 1.0

            # W19-8 — scipy's paired t-test can return ``+/-inf`` or
            # ``NaN`` when the per-row Brier differences are identical
            # (zero variance — "catastrophic cancellation"). JSON
            # serialization rejects ``inf`` / ``-inf`` / ``NaN`` (the
            # standard JSONEncoder raises ``ValueError: Out of range
            # float values are not JSON compliant``), so coerce any
            # non-finite ``t_statistic`` to ``None`` and bump
            # ``p_value`` to a conservative ``1.0`` so a degenerate
            # challenger can NEVER be flagged ``is_significantly_better``.
            if not np.isfinite(t_stat):
                log.info(
                    "[shadow_inference] non-finite t_statistic for %r "
                    "(likely zero-variance Brier differences) — "
                    "coercing to None + p_value=1.0",
                    name,
                )
                t_stat = None  # type: ignore[assignment]
                p_value = 1.0

            is_better = bool(
                brier_improvement > 0.0
                and t_stat is not None
                and p_value < alpha
            )
            comparison = {
                "n_samples": len(stamped),
                "champion_brier": round(champ_mean, 6),
                "challenger_brier": round(chall_mean, 6),
                "brier_improvement": round(brier_improvement, 6),
                "t_statistic": t_stat,
                "p_value": p_value,
                "alpha": float(alpha),
                "min_samples": int(min_samples),
                "is_significantly_better": is_better,
                "promoted": False,
            }
            results[name] = comparison

            if is_better:
                log.info(
                    "[shadow_inference] Promoting %r to champion "
                    "(champion_brier=%.4f → challenger_brier=%.4f, p=%.4f, n=%d)",
                    name, champ_mean, chall_mean, p_value, len(stamped),
                )
                self._promote(name)
                comparison["promoted"] = True

        if insufficient:
            log.info(
                "[shadow_inference] evaluate_and_promote: insufficient data for "
                "%d challenger(s): %s",
                len(insufficient), insufficient,
            )
        return results

    def _promote(self, name: str) -> None:
        """Record a promotion decision in the engine's ledger.

        W19-8 — the engine does NOT swap the production model in-process
        (production model swaps go through ``model_registry`` +
        ``training_orchestrator`` to preserve the version lineage + audit
        trail). This method records the promotion decision so the
        ``/api/ml/shadow`` status endpoint can surface "challenger X was
        flagged for promotion at T" to the operator, who then initiates
        the formal promotion via the training orchestrator.

        Parameters
        ----------
        name : str
            Challenger name to mark as promoted.
        """
        with self._lock:
            self._promotions[name] = {
                "promoted_at": time.time(),
                "description": (
                    self._models.get(name, {}).get("description", "")
                    if self._models.get(name)
                    else ""
                ),
            }
        log.info(
            "[shadow_inference] promotion recorded for %r "
            "(formal production swap deferred to training_orchestrator)",
            name,
        )

    # ── Reporting ──────────────────────────────────────────────────────────
    def get_status_report(self) -> dict[str, Any]:
        """
        Snapshot of the shadow-inference registry for observability.

        Each registered challenger reports its call count, the rolling
        mean absolute disagreement vs. the production model over its
        history window, and its most recent comparison record. The
        W19-8 ``promotions`` ledger is also surfaced so an operator can
        see which challengers have been flagged for promotion and when.

        This is the surface the ``/api/ml/shadow`` endpoint exposes —
        kept self-contained here so the production predict() path can
        be exercised without depending on it.
        """
        with self._lock:
            models = []
            for name, entry in self._models.items():
                history = list(entry["history"])
                if history:
                    mean_delta = float(np.mean([h["abs_delta"] for h in history]))
                else:
                    mean_delta = 0.0
                # W19-8 — outcome coverage: how many of the challenger's
                # comparison rows have a stamped YES/NO label? Surfaced so
                # the operator can see at a glance when a challenger is
                # ready for ``evaluate_and_promote``.
                n_outcome_stamped = sum(
                    1 for h in history if h.get("outcome") is not None
                )
                models.append({
                    "name": name,
                    "description": entry["description"],
                    "calls": entry["calls"],
                    "mean_abs_delta_vs_production": round(mean_delta, 4),
                    "n_outcome_stamped": n_outcome_stamped,
                    "last_comparison": history[-1] if history else None,
                })
            return {
                "registered_models": models,
                "total_calls": self.total_calls,
                "total_errors": self.total_errors,
                "registered_at": self.registered_at,
                "max_history_per_model": _MAX_HISTORY_PER_MODEL,
                "promotions": dict(self._promotions),
            }


# Global singleton — mirrors the pattern used by `drift_detector`,
# `audit_logger`, `closed_positions`, `execution_quality`, etc.
shadow_inference = ShadowInferenceEngine()


# ── HTTP surface (W19-8) ────────────────────────────────────────────────────
def register_routes(app: Any) -> None:
    """Append shadow-inference inspection + promotion endpoints to a FastAPI app.

    Pure addition — does not touch any existing route, middleware, or
    decorator. Auth is enforced by the caller's existing ``enforce_api_auth``
    middleware (these paths are not in ``PUBLIC_PATHS``).

    Endpoints:

      GET  /api/ml/shadow
          Return the current shadow-inference registry snapshot —
          registered challenger list (name / description / calls /
          mean-abs-delta vs production / outcome-stamped count /
          last comparison record), aggregate counters
          (``total_calls`` / ``total_errors`` / ``registered_at``), and
          the W19-8 promotion ledger.

      POST /api/ml/shadow/evaluate
          Run ``evaluate_and_promote`` against every registered
          challenger and return ``{challenger_name: comparison_dict}``.
          Each comparison dict carries ``n_samples``,
          ``champion_brier``, ``challenger_brier``,
          ``brier_improvement``, ``t_statistic``, ``p_value``,
          ``is_significantly_better``, and ``promoted``. Challengers
          with insufficient outcome-stamped data are omitted from the
          response (a separate ``insufficient_data`` list names them).
          Query params:
            ``min_samples`` (default 30) — minimum outcome-stamped
              comparison rows required to evaluate a challenger.
            ``alpha`` (default 0.05) — paired t-test significance
              threshold.
    """
    try:  # pragma: no cover — exercised only when the web framework is installed
        from fastapi import Query
    except ImportError:  # pragma: no cover — fastapi absent in non-server envs
        return

    @app.get("/api/ml/shadow", tags=["ml"])
    async def _shadow_status():
        """Return the shadow-inference registry snapshot + promotion ledger."""
        return shadow_inference.get_status_report()

    @app.post("/api/ml/shadow/evaluate", tags=["ml"])
    async def _shadow_evaluate(
        min_samples: int = Query(
            _DEFAULT_MIN_PROMOTION_SAMPLES,
            ge=1,
            le=_MAX_HISTORY_PER_MODEL,
            description=(
                "Minimum outcome-stamped comparison rows required "
                "before a challenger is evaluated."
            ),
        ),
        alpha: float = Query(
            _DEFAULT_PROMOTION_ALPHA,
            gt=0.0,
            lt=1.0,
            description="Paired t-test significance threshold.",
        ),
    ):
        """Evaluate shadow models and optionally promote.

        Runs ``shadow_inference.evaluate_and_promote`` against every
        registered challenger and returns the per-challenger
        comparison dict. Significantly-better challengers are flagged
        ``is_significantly_better=True`` and recorded in the engine's
        promotion ledger.
        """
        evaluated = shadow_inference.evaluate_and_promote(
            min_samples=min_samples,
            alpha=alpha,
        )
        # Surface the challengers that DIDN'T have enough data so the
        # operator can see "n more samples needed before X can be
        # evaluated" rather than just an empty dict entry.
        with shadow_inference._lock:
            all_names = list(shadow_inference._models.keys())
        insufficient = sorted(n for n in all_names if n not in evaluated)
        return {
            "evaluated": evaluated,
            "insufficient_data": insufficient,
            "min_samples": min_samples,
            "alpha": alpha,
        }


__all__ = [
    "ShadowInferenceEngine",
    "shadow_inference",
    "register_routes",
]
